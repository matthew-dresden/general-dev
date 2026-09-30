"""Tests for devcontainer_config.hostcreds: the host-side credential mechanism's
core (manifest loading, the three resolvers, the startup block and the
per-credential fragments).

The `devcontainer_config` import is deferred into function bodies (via
`_import_hostcreds`) instead of done once at module scope, for the same
reason `tests/test_repo.py` documents: the
TDD RED gate stashes this unit's own production-source files and re-runs
a single named test node, and a module-level import would fail COLLECTION
for the whole file (pytest exit 2, no test outcome recorded) instead of
failing the one test for the real reason.

`_FakeRunner` stands in for the injected subprocess runner the way the
deleted catalog suite's double did: it never spawns a process, records
every argv/stdin pair it is handed, and answers from a queue the test
fills beforehand. `_RaisingRunner` stands in for the one case a real
runner can never return from normally: the source binary (security, git,
aws) is not on PATH, which `subprocess.run` reports by raising
FileNotFoundError, not by returning a non-zero exit code. No test here
touches the real keychain, the real git config, the real aws CLI, or any
network.

No seeded value in this file is a real credential; every one is generated
from `uuid.uuid4()` at test time, so nothing here is itself a secret this
repository's own scanner would need to flag. The same values double as
leak probes: a failing source command's stdout can carry the secret, so
the failure tests queue a generated value on stdout and then assert that
value never appears in the exception's text.

The end-to-end section is the only place this file executes real shell
processes, following the discipline this suite's deleted shell-startup
tests established:
the rendered startup block runs for real under `bash -c` and `zsh -c`,
with HOME pointed at a directory under tmp_path, so the block's
store-directory probe reads only files this test wrote. Both
interpreters are required, not skipped when absent: `make test` checks
uv and zsh as prerequisites before pytest runs (TEST_PREREQUISITE_TOOLS
in the Makefile), so a missing binary is a loud precondition failure of
the test environment (via `_require_interpreter`), never a silently narrowed matrix. The
empty-store-directory silence case is asserted for both shells because
the block's glob probe exists precisely for zsh: a bare `for f in
<dir>/*.env` loop aborts zsh outright when the glob matches nothing
(verified during this module's design), so the block acquires its file
list through a stderr-silenced command substitution instead, and this
suite pins that both shells stay silent, exit zero and keep sourcing.
"""

from __future__ import annotations

import importlib
import json
import os
import re
import shutil
import subprocess
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    # Type-only: erased at runtime, so the TDD RED gate's stash of this
    # unit's own production source cannot break this file's COLLECTION
    # (the runtime import stays deferred, per the module docstring); it
    # exists only so the helpers below take the real types instead of
    # object and every call site needs no cast.
    from devcontainer_config.hostcreds import CredentialSpec, ResolvedCredential

# The shells the end-to-end cases run the rendered block under: bash (the
# devcontainer's interactive default) and zsh (the container's login shell).
# Both are required, matching `make test`'s prerequisite tools
# (TEST_PREREQUISITE_TOOLS in the Makefile: uv, zsh); a missing binary is a
# loud precondition failure via `_require_interpreter`, never a silently
# narrowed matrix.
E2E_SHELLS: tuple[str, ...] = ("bash", "zsh")


def _import_hostcreds() -> ModuleType:
    """Import devcontainer_config.hostcreds from inside a function body.

    See the module docstring for why this is not a module-level import.
    """
    return importlib.import_module("devcontainer_config.hostcreds")


def _seeded_value(prefix: str = "value") -> str:
    """A generated placeholder value, unique per call, never a real credential."""
    return f"{prefix}-{uuid.uuid4().hex}"


def _write_manifest(root: Path, filename: str, text: str) -> Path:
    """Write `text` as `root`'s .devcontainer/<filename> and return its path.

    Creates the .devcontainer directory when absent, mirroring the layout
    load_manifest reads, so a test's tmp_path stands in for a checkout
    root without any further setup.
    """
    directory = root / ".devcontainer"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / filename
    path.write_text(text, encoding="utf-8")
    return path


def _ok(stdout: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


def _err(stderr: str, *, returncode: int = 1, stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class _FakeRunner:
    """A Runner double: records every call, answers from a queue, spawns nothing.

    `calls` holds the (argv, stdin) pairs exactly as before; the optional
    keyword-only `env` the production runner now accepts is recorded in
    the parallel `envs` list, one entry per call, so a test can pin what
    environment a resolver handed the child without disturbing the
    established call-shape assertions.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], str | None]] = []
        self.envs: list[Mapping[str, str] | None] = []
        self._queue: list[subprocess.CompletedProcess[str]] = []

    def queue(self, result: subprocess.CompletedProcess[str]) -> None:
        self._queue.append(result)

    def __call__(
        self, argv: Sequence[str], stdin: str | None, *, env: Mapping[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((tuple(argv), stdin))
        self.envs.append(env)
        assert self._queue, "_FakeRunner was invoked with no queued response"
        return self._queue.pop(0)


class _RaisingRunner:
    """A Runner double standing in for a host without the source binary on PATH."""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], str | None]] = []

    def __call__(
        self, argv: Sequence[str], stdin: str | None, *, env: Mapping[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((tuple(argv), stdin))
        raise FileNotFoundError(f"no such file or directory: {argv[0]}")


def _resolved(
    hc: ModuleType,
    name: str,
    source: str,
    labels: dict[str, str],
    value: str,
    *,
    username: str | None = None,
    expires_at: str | None = None,
) -> ResolvedCredential:
    """A ResolvedCredential built from parts, for the renderer tests.

    The renderer only reads the credential's fields, so tests build them
    directly instead of routing through a resolver and a fake runner.
    """
    spec: CredentialSpec = hc.CredentialSpec(name=name, source=source, labels=labels)
    credential: ResolvedCredential = hc.ResolvedCredential(
        spec=spec, value=value, username=username, expires_at=expires_at
    )
    return credential


# ---------------------------------------------------------------------------
# subprocess_runner: the production seam, exercised against a real child
# ---------------------------------------------------------------------------


def test_subprocess_runner_feeds_stdin_to_a_real_child_and_captures_stdout() -> None:
    """The production runner, round-tripped for real -- the fakes above
    never exercise it, and the resolvers' argv/stdin contract is only
    proven if the seam itself works. Mirrors the deleted catalog suite's
    test of that module's twin runner. `sh -c cat` reads its stdin and
    writes it back verbatim, so one round trip proves both halves of the
    seam: the stdin document reaches the child, and the child's stdout
    comes back captured as text.
    """
    hc = _import_hostcreds()
    payload = _seeded_value("stdin-payload")

    result = hc.subprocess_runner(["sh", "-c", "cat"], payload)

    assert result.returncode == 0
    assert result.stdout == payload


def test_subprocess_runner_forwards_env_to_a_real_child() -> None:
    """The env half of the seam, proven against a real child the way the
    stdin half above is: a resolver that must constrain a child's
    environment (resolve_git's GIT_TERMINAL_PROMPT=0) only works if the
    production runner actually delivers the mapping to the subprocess.
    """
    hc = _import_hostcreds()
    value = _seeded_value("env-payload")

    result = hc.subprocess_runner(
        ["sh", "-c", 'printf %s "$HOSTCREDS_RUNNER_TEST_VAR"'],
        None,
        env={**os.environ, "HOSTCREDS_RUNNER_TEST_VAR": value},
    )

    assert result.returncode == 0
    assert result.stdout == value


# ---------------------------------------------------------------------------
# load_manifest: the valid paths
# ---------------------------------------------------------------------------


def test_load_manifest_returns_all_three_sources_in_file_order(tmp_path: Path) -> None:
    hc = _import_hostcreds()
    _write_manifest(
        tmp_path,
        hc.MANIFEST_FILENAME,
        json.dumps(
            {
                "GITHUB_TOKEN": {"source": "keychain"},
                "GH_PAT": {
                    "source": "keychain",
                    "service": "gh.example/service",
                    "account": "alice",
                },
                "CHARTS_PASSWORD": {"source": "git", "host": "charts.example.com"},
                "AWS_SANDBOX": {"source": "aws-export", "profile": "sandbox"},
                "AWS_DEFAULT": {"source": "aws-export"},
            }
        ),
    )

    specs = hc.load_manifest(tmp_path)

    assert [spec.name for spec in specs] == [
        "GITHUB_TOKEN",
        "GH_PAT",
        "CHARTS_PASSWORD",
        "AWS_SANDBOX",
        "AWS_DEFAULT",
    ]
    # The default keychain service derives from the root parameter's
    # basename and the credential name -- not from any git subprocess.
    assert dict(specs[0].labels) == {"service": f"devcontainer/{tmp_path.name}/GITHUB_TOKEN"}
    assert dict(specs[1].labels) == {
        "service": "gh.example/service",
        "account": "alice",
    }
    assert dict(specs[2].labels) == {"host": "charts.example.com"}
    assert dict(specs[3].labels) == {"profile": "sandbox"}
    assert dict(specs[4].labels) == {"profile": hc.DEFAULT_AWS_PROFILE}


def test_load_manifest_accepts_an_empty_mapping(tmp_path: Path) -> None:
    """An empty manifest is the explicit 'no host credentials' statement.

    Distinct from the file being absent (an error): an operator who
    commits an empty map has said this checkout pushes nothing, and
    push-creds should proceed with nothing to resolve rather than fail.
    """
    hc = _import_hostcreds()
    _write_manifest(tmp_path, hc.MANIFEST_FILENAME, "{}")

    assert hc.load_manifest(tmp_path) == ()


def test_manifest_path_places_the_manifest_under_devcontainer(tmp_path: Path) -> None:
    hc = _import_hostcreds()
    assert hc.manifest_path(tmp_path) == tmp_path / ".devcontainer" / hc.MANIFEST_FILENAME


# ---------------------------------------------------------------------------
# load_manifest: every validation failure mode
# ---------------------------------------------------------------------------


def test_load_manifest_missing_file_names_the_path_and_the_example(tmp_path: Path) -> None:
    hc = _import_hostcreds()
    with pytest.raises(hc.ManifestError) as excinfo:
        hc.load_manifest(tmp_path)
    message = str(excinfo.value)
    assert str(tmp_path / ".devcontainer" / "hostcreds.map.json") in message
    assert hc.MANIFEST_EXAMPLE_FILENAME in message


@pytest.mark.parametrize("text", ["{", "not json at all", '["unclosed"'])
def test_load_manifest_unparseable_json_is_a_manifest_error(tmp_path: Path, text: str) -> None:
    hc = _import_hostcreds()
    path = _write_manifest(tmp_path, hc.MANIFEST_FILENAME, text)
    with pytest.raises(hc.ManifestError) as excinfo:
        hc.load_manifest(tmp_path)
    assert str(path) in str(excinfo.value)


@pytest.mark.parametrize("text", ['["an", "array"]', '"a bare string"', "42", "null"])
def test_load_manifest_non_object_top_level_is_a_manifest_error(tmp_path: Path, text: str) -> None:
    hc = _import_hostcreds()
    path = _write_manifest(tmp_path, hc.MANIFEST_FILENAME, text)
    with pytest.raises(hc.ManifestError) as excinfo:
        hc.load_manifest(tmp_path)
    message = str(excinfo.value)
    assert "not a JSON object" in message
    assert str(path) in message


@pytest.mark.skipif(
    os.geteuid() == 0,
    reason="root reads files regardless of their permission bits, so a "
    "chmod-0o000 manifest is not actually unreadable and the precondition "
    "cannot be arranged under root",
)
def test_load_manifest_unreadable_file_is_a_manifest_error(tmp_path: Path) -> None:
    hc = _import_hostcreds()
    path = _write_manifest(
        tmp_path,
        hc.MANIFEST_FILENAME,
        json.dumps({"TOKEN": {"source": "keychain"}}),
    )
    path.chmod(0o000)

    with pytest.raises(hc.ManifestError) as excinfo:
        hc.load_manifest(tmp_path)
    message = str(excinfo.value)
    assert str(path) in message
    assert "cannot read" in message


@pytest.mark.parametrize(
    "name",
    ["github-token", "1TOKEN", "lower_case", "WITH SPACE", "A/B", ""],
    ids=["hyphen", "leading-digit", "lowercase", "space", "slash", "empty"],
)
def test_load_manifest_rejects_invalid_names(tmp_path: Path, name: str) -> None:
    hc = _import_hostcreds()
    _write_manifest(tmp_path, hc.MANIFEST_FILENAME, json.dumps({name: {"source": "keychain"}}))
    with pytest.raises(hc.ManifestError) as excinfo:
        hc.load_manifest(tmp_path)
    message = str(excinfo.value)
    # The entry is named by position and by its repr, so even the empty
    # name is identifiable in the message.
    assert "entry 1" in message
    assert repr(name) in message


@pytest.mark.parametrize(
    "name",
    [
        "PATH",
        "LD_PRELOAD",
        "DYLD_INSERT_LIBRARIES",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
    ],
    ids=["path", "ld-preload", "dyld-insert", "aws-key-id", "aws-secret", "aws-token"],
)
def test_load_manifest_rejects_reserved_names(tmp_path: Path, name: str) -> None:
    """A name that is a reserved shell/loader variable or one of the AWS
    variables the mechanism exports itself is rejected for every source:
    the collision is in the name, not the source, and a keychain entry
    named AWS_SECRET_ACCESS_KEY would otherwise silently shadow the value
    parsed out of the aws CLI's document at shell startup.
    """
    hc = _import_hostcreds()
    _write_manifest(tmp_path, hc.MANIFEST_FILENAME, json.dumps({name: {"source": "keychain"}}))
    with pytest.raises(hc.ManifestError) as excinfo:
        hc.load_manifest(tmp_path)
    message = str(excinfo.value)
    assert name in message
    assert "reserved" in message


def test_load_manifest_reports_a_reserved_name_alongside_another_problem(tmp_path: Path) -> None:
    """The reserved-name check feeds the same all-problems-at-once
    collection as every other validation: one push-creds run names the
    reserved entry and the unrelated mistake together.
    """
    hc = _import_hostcreds()
    path = _write_manifest(
        tmp_path,
        hc.MANIFEST_FILENAME,
        json.dumps({"PATH": {"source": "keychain"}, "FIRST": {"source": "vault"}}),
    )
    with pytest.raises(hc.ManifestError) as excinfo:
        hc.load_manifest(tmp_path)
    message = str(excinfo.value)
    assert "2 problem" in message
    assert "PATH" in message
    assert "FIRST" in message
    assert str(path) in message


def test_load_manifest_rejects_an_unknown_source(tmp_path: Path) -> None:
    hc = _import_hostcreds()
    _write_manifest(
        tmp_path,
        hc.MANIFEST_FILENAME,
        json.dumps({"VAULT_TOKEN": {"source": "vault"}}),
    )
    with pytest.raises(hc.ManifestError) as excinfo:
        hc.load_manifest(tmp_path)
    message = str(excinfo.value)
    assert "VAULT_TOKEN" in message
    assert "vault" in message
    for source in (hc.SOURCE_KEYCHAIN, hc.SOURCE_GIT, hc.SOURCE_AWS_EXPORT):
        assert source in message


def test_load_manifest_rejects_a_missing_source_key(tmp_path: Path) -> None:
    hc = _import_hostcreds()
    _write_manifest(
        tmp_path,
        hc.MANIFEST_FILENAME,
        json.dumps({"NO_SOURCE": {"host": "example.com"}}),
    )
    with pytest.raises(hc.ManifestError) as excinfo:
        hc.load_manifest(tmp_path)
    message = str(excinfo.value)
    assert "NO_SOURCE" in message
    assert "source" in message


def test_load_manifest_rejects_a_non_object_entry(tmp_path: Path) -> None:
    hc = _import_hostcreds()
    _write_manifest(
        tmp_path,
        hc.MANIFEST_FILENAME,
        json.dumps({"BARE_ENTRY": "keychain"}),
    )
    with pytest.raises(hc.ManifestError) as excinfo:
        hc.load_manifest(tmp_path)
    assert "BARE_ENTRY" in str(excinfo.value)


@pytest.mark.parametrize(
    "entry",
    [
        {},
        {"service": "devcontainer/x/Y"},
        {"profile": "sandbox"},
    ],
    ids=["no-labels", "wrong-label", "another-sources-label"],
)
def test_load_manifest_rejects_a_git_entry_without_a_host(
    tmp_path: Path, entry: dict[str, str]
) -> None:
    hc = _import_hostcreds()
    _write_manifest(
        tmp_path,
        hc.MANIFEST_FILENAME,
        json.dumps({"CHARTS_PASSWORD": {"source": "git", **entry}}),
    )
    with pytest.raises(hc.ManifestError) as excinfo:
        hc.load_manifest(tmp_path)
    assert "CHARTS_PASSWORD" in str(excinfo.value)
    assert "host" in str(excinfo.value)


@pytest.mark.parametrize(
    "host",
    ["https://example.com", "example.com/path", "user@example.com", "example.com:22", "", "e x"],
    ids=["scheme", "path", "user-part", "port", "empty", "space"],
)
def test_load_manifest_rejects_a_host_that_is_not_a_bare_hostname(
    tmp_path: Path, host: str
) -> None:
    hc = _import_hostcreds()
    _write_manifest(
        tmp_path,
        hc.MANIFEST_FILENAME,
        json.dumps({"CHARTS_PASSWORD": {"source": "git", "host": host}}),
    )
    with pytest.raises(hc.ManifestError) as excinfo:
        hc.load_manifest(tmp_path)
    assert "CHARTS_PASSWORD" in str(excinfo.value)


@pytest.mark.parametrize(
    ("source", "entry", "typo"),
    [
        ("keychain", {"source": "keychain", "servcie": "typo"}, "servcie"),
        ("git", {"source": "git", "host": "example.com", "account": "alice"}, "account"),
        ("aws-export", {"source": "aws-export", "profile": "p", "service": "s"}, "service"),
    ],
    ids=["keychain-typo", "git-foreign-label", "aws-export-foreign-label"],
)
def test_load_manifest_rejects_unknown_label_keys(
    tmp_path: Path, source: str, entry: dict[str, str], typo: str
) -> None:
    """An unknown label key is an error, not a warning: a 'servcie' typo would
    otherwise silently mean the default service is used and the wrong
    keychain item read.
    """
    hc = _import_hostcreds()
    _write_manifest(tmp_path, hc.MANIFEST_FILENAME, json.dumps({"TOKEN": entry}))
    with pytest.raises(hc.ManifestError) as excinfo:
        hc.load_manifest(tmp_path)
    message = str(excinfo.value)
    assert "TOKEN" in message
    assert typo in message
    assert source in message


@pytest.mark.parametrize("service", [42, ""], ids=["non-string", "empty-string"])
def test_load_manifest_rejects_an_invalid_label_value(tmp_path: Path, service: object) -> None:
    """A label that is not a non-empty string is rejected: an empty 'service'
    would otherwise reach `security -s ''` and match a different keychain
    item than the one the operator named.
    """
    hc = _import_hostcreds()
    _write_manifest(
        tmp_path,
        hc.MANIFEST_FILENAME,
        json.dumps({"TOKEN": {"source": "keychain", "service": service}}),
    )
    with pytest.raises(hc.ManifestError) as excinfo:
        hc.load_manifest(tmp_path)
    assert "TOKEN" in str(excinfo.value)
    assert "service" in str(excinfo.value)


@pytest.mark.parametrize(
    "service",
    ["devcontainer/x/y\nnewline", "devcontainer/x/y'quote"],
    ids=["newline", "single-quote"],
)
def test_load_manifest_rejects_label_values_with_unsafe_characters(
    tmp_path: Path, service: str
) -> None:
    """Labels reach shell text and argv: a newline in a label could escape
    the fragment comment line it is written onto (the rest of the line
    would then run as shell), and a quote would end the quoting around
    the label in the rendered text.
    """
    hc = _import_hostcreds()
    _write_manifest(
        tmp_path,
        hc.MANIFEST_FILENAME,
        json.dumps({"TOKEN": {"source": "keychain", "service": service}}),
    )
    with pytest.raises(hc.ManifestError) as excinfo:
        hc.load_manifest(tmp_path)
    message = str(excinfo.value)
    assert "TOKEN" in message
    assert "service" in message


def test_load_manifest_reports_every_problem_in_one_error(tmp_path: Path) -> None:
    """Fail fast and completely: three broken entries surface in one
    ManifestError, not three push-creds runs.
    """
    hc = _import_hostcreds()
    path = _write_manifest(
        tmp_path,
        hc.MANIFEST_FILENAME,
        json.dumps(
            {
                "FIRST": {"source": "vault"},
                "SECOND": {"source": "git"},
                "third-lowercase": {"source": "keychain"},
            }
        ),
    )
    with pytest.raises(hc.ManifestError) as excinfo:
        hc.load_manifest(tmp_path)
    message = str(excinfo.value)
    assert "3 problem" in message
    assert "FIRST" in message
    assert "SECOND" in message
    assert "third-lowercase" in message
    assert str(path) in message


# ---------------------------------------------------------------------------
# The resolvers: successes, with exact argv shape
# ---------------------------------------------------------------------------


def test_resolve_keychain_argv_carries_labels_only(tmp_path: Path) -> None:
    hc = _import_hostcreds()
    _write_manifest(
        tmp_path, hc.MANIFEST_FILENAME, json.dumps({"GITHUB_TOKEN": {"source": "keychain"}})
    )
    spec = hc.load_manifest(tmp_path)[0]
    runner = _FakeRunner()
    password = _seeded_value("kc-password")
    runner.queue(_ok(f"{password}\n"))

    credential = hc.resolve(spec, runner)

    assert credential.value == password
    assert credential.username is None
    assert credential.expires_at is None
    (argv, stdin) = runner.calls[0]
    # The exact production argv: labels only, value never present, and
    # exactly one trailing newline (the CLI's, not the password's)
    # stripped from stdout.
    assert argv == (
        "security",
        "find-generic-password",
        "-w",
        "-s",
        f"devcontainer/{tmp_path.name}/GITHUB_TOKEN",
    )
    assert stdin is None


def test_resolve_keychain_appends_account_only_when_set() -> None:
    hc = _import_hostcreds()
    spec = hc.CredentialSpec(
        name="GH_PAT",
        source=hc.SOURCE_KEYCHAIN,
        labels={"service": "gh.example/service", "account": "alice"},
    )
    runner = _FakeRunner()
    runner.queue(_ok(f"{_seeded_value()}\n"))

    hc.resolve_keychain(spec, runner)

    (argv, _stdin) = runner.calls[0]
    assert argv == (
        "security",
        "find-generic-password",
        "-w",
        "-s",
        "gh.example/service",
        "-a",
        "alice",
    )


def test_resolve_keychain_without_a_service_label_fails_fast() -> None:
    """load_manifest applies the default, so a spec missing the label was
    built by hand; the named failure beats a KeyError.
    """
    hc = _import_hostcreds()
    spec = hc.CredentialSpec(name="X", source=hc.SOURCE_KEYCHAIN, labels={})
    with pytest.raises(hc.HostCredsError) as excinfo:
        hc.resolve_keychain(spec, _FakeRunner())
    assert "X" in str(excinfo.value)
    assert "service" in str(excinfo.value)


def test_resolve_git_feeds_the_host_on_stdin_not_argv() -> None:
    hc = _import_hostcreds()
    spec = hc.CredentialSpec(
        name="CHARTS_PASSWORD", source=hc.SOURCE_GIT, labels={"host": "charts.example.com"}
    )
    runner = _FakeRunner()
    password = _seeded_value("git-password")
    username = _seeded_value("git-user")
    runner.queue(
        _ok(
            f"protocol=https\nhost=charts.example.com\nusername={username}\npassword={password}\n\n"
        )
    )

    credential = hc.resolve_git(spec, runner)

    assert credential.value == password
    assert credential.username == username
    assert credential.expires_at is None
    (argv, stdin) = runner.calls[0]
    assert argv == ("git", "credential", "fill")
    assert stdin == "protocol=https\nhost=charts.example.com\n\n"
    # The host never reaches the process table.
    assert "charts.example.com" not in " ".join(argv)
    # The child runs with GIT_TERMINAL_PROMPT forced to 0, merged over the
    # inherited environment: a tty-less host with no stored credential must
    # fail fast instead of blocking forever on a prompt no one can answer.
    env = runner.envs[0]
    assert env is not None
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    # Merged over os.environ, not a bare one-variable map: the child keeps
    # the host's environment so the credential-helper chain still resolves.
    assert env.get("PATH") == os.environ.get("PATH")


def test_resolve_git_without_a_username_line_returns_none() -> None:
    hc = _import_hostcreds()
    spec = hc.CredentialSpec(
        name="CHARTS_PASSWORD", source=hc.SOURCE_GIT, labels={"host": "charts.example.com"}
    )
    runner = _FakeRunner()
    runner.queue(_ok("protocol=https\nhost=charts.example.com\npassword=pw\n\n"))

    credential = hc.resolve_git(spec, runner)

    assert credential.value == "pw"
    assert credential.username is None


def test_resolve_aws_export_argv_carries_only_the_profile() -> None:
    hc = _import_hostcreds()
    spec = hc.CredentialSpec(
        name="AWS_SANDBOX", source=hc.SOURCE_AWS_EXPORT, labels={"profile": "sandbox"}
    )
    runner = _FakeRunner()
    document = {
        "AccessKeyId": _seeded_value("aki"),
        "SecretAccessKey": _seeded_value("sk"),
        "SessionToken": _seeded_value("st"),
        "Expiration": "2030-06-01T12:00:00+00:00",
    }
    runner.queue(_ok(json.dumps(document) + "\n"))

    credential = hc.resolve_aws_export(spec, runner)

    assert credential.value == json.dumps(document)
    assert credential.username is None
    assert credential.expires_at == "2030-06-01T12:00:00+00:00"
    (argv, stdin) = runner.calls[0]
    assert argv == ("aws", "configure", "export-credentials", "--profile", "sandbox")
    assert stdin is None


def test_resolve_aws_export_without_expiration_has_no_expiry() -> None:
    """A static access key exports no SessionToken and no Expiration; the
    resolver must treat that as a complete answer, not a malformed one.
    """
    hc = _import_hostcreds()
    spec = hc.CredentialSpec(
        name="AWS_STATIC", source=hc.SOURCE_AWS_EXPORT, labels={"profile": "default"}
    )
    runner = _FakeRunner()
    runner.queue(_ok(json.dumps({"AccessKeyId": "AKI", "SecretAccessKey": "SK"}) + "\n"))

    credential = hc.resolve_aws_export(spec, runner)

    assert credential.expires_at is None


def test_resolve_keeps_exactly_one_trailing_newline_in_the_value() -> None:
    """The exactly-one-trailing-newline contract: the source command's line
    terminator is removed, and only it -- a value genuinely ending in a
    newline byte survives the strip. An rstrip() here would eat the
    value's own trailing newline and silently change the secret.
    """
    hc = _import_hostcreds()
    spec = hc.CredentialSpec(
        name="NEWLINE_TOKEN", source=hc.SOURCE_KEYCHAIN, labels={"service": "s"}
    )
    runner = _FakeRunner()
    runner.queue(_ok("value\n\n"))

    credential = hc.resolve_keychain(spec, runner)

    assert credential.value == "value\n"


# ---------------------------------------------------------------------------
# The resolvers: failures. Every case queues a generated value on stdout
# and asserts it never reaches the exception's text: stdout is where the
# secret travels, so the diagnostics must quote stderr only.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("spec_labels", "source", "stderr"),
    [
        ({"service": "devcontainer/x/GITHUB_TOKEN"}, "keychain", "could not be found"),
        ({"host": "charts.example.com"}, "git", "fatal: could not read Username"),
        ({"profile": "default"}, "aws-export", "Unable to locate credentials"),
    ],
    ids=["keychain", "git", "aws-export"],
)
def test_resolve_nonzero_exit_quotes_stderr_never_stdout(
    tmp_path: Path, spec_labels: dict[str, str], source: str, stderr: str
) -> None:
    hc = _import_hostcreds()
    spec = hc.CredentialSpec(name="THE_NAME", source=source, labels=spec_labels)
    runner = _FakeRunner()
    leak = _seeded_value("secret")
    runner.queue(_err(stderr, returncode=44, stdout=leak))

    with pytest.raises(hc.ResolutionError) as excinfo:
        hc.resolve(spec, runner)
    message = str(excinfo.value)
    assert stderr in message
    assert "44" in message
    assert leak not in message


@pytest.mark.parametrize(
    ("source", "binary"),
    [("keychain", "security"), ("git", "git"), ("aws-export", "aws")],
)
def test_resolve_missing_binary_names_the_binary(source: str, binary: str) -> None:
    hc = _import_hostcreds()
    labels: dict[str, str] = {
        "keychain": {"service": "s"},
        "git": {"host": "example.com"},
        "aws-export": {"profile": "default"},
    }[source]
    spec = hc.CredentialSpec(name="THE_NAME", source=source, labels=labels)

    with pytest.raises(hc.ResolutionError) as excinfo:
        hc.resolve(spec, _RaisingRunner())
    message = str(excinfo.value)
    assert binary in message
    assert "PATH" in message


def test_resolve_keychain_empty_value_raises() -> None:
    hc = _import_hostcreds()
    spec = hc.CredentialSpec(name="EMPTY_TOKEN", source=hc.SOURCE_KEYCHAIN, labels={"service": "s"})
    runner = _FakeRunner()
    runner.queue(_ok("\n"))

    with pytest.raises(hc.ResolutionError) as excinfo:
        hc.resolve_keychain(spec, runner)
    assert "EMPTY_TOKEN" in str(excinfo.value)
    assert "empty" in str(excinfo.value)


def test_resolve_git_answer_without_a_password_line_raises() -> None:
    hc = _import_hostcreds()
    spec = hc.CredentialSpec(
        name="CHARTS_PASSWORD", source=hc.SOURCE_GIT, labels={"host": "charts.example.com"}
    )
    runner = _FakeRunner()
    runner.queue(_ok("protocol=https\nhost=charts.example.com\nusername=u\n\n"))

    with pytest.raises(hc.ResolutionError) as excinfo:
        hc.resolve_git(spec, runner)
    message = str(excinfo.value)
    assert "CHARTS_PASSWORD" in message
    assert "password" in message


def test_resolve_git_answer_with_an_empty_password_line_raises() -> None:
    """An empty 'password=' line is no password, not a resolution: the same
    failure as an absent line, so an empty value can never be stored as
    if it were the credential.
    """
    hc = _import_hostcreds()
    spec = hc.CredentialSpec(
        name="CHARTS_PASSWORD", source=hc.SOURCE_GIT, labels={"host": "charts.example.com"}
    )
    runner = _FakeRunner()
    runner.queue(_ok("protocol=https\nhost=charts.example.com\npassword=\n\n"))

    with pytest.raises(hc.ResolutionError) as excinfo:
        hc.resolve_git(spec, runner)
    message = str(excinfo.value)
    assert "CHARTS_PASSWORD" in message
    assert "password" in message


def test_resolve_git_answer_with_a_line_without_an_equals_sign_raises() -> None:
    """git's fill protocol is key=value lines up to the blank terminator; a
    stray line before it is malformed output, not a line to skip --
    silently skipping one would truncate a password containing raw
    newlines into a plausible but wrong value.
    """
    hc = _import_hostcreds()
    spec = hc.CredentialSpec(
        name="CHARTS_PASSWORD", source=hc.SOURCE_GIT, labels={"host": "charts.example.com"}
    )
    runner = _FakeRunner()
    runner.queue(_ok("protocol=https\nhost=charts.example.com\na stray line\npassword=pw\n\n"))

    with pytest.raises(hc.ResolutionError) as excinfo:
        hc.resolve_git(spec, runner)
    message = str(excinfo.value)
    assert "CHARTS_PASSWORD" in message
    assert "git credential fill" in message


def test_resolve_aws_export_empty_output_raises() -> None:
    hc = _import_hostcreds()
    spec = hc.CredentialSpec(
        name="AWS_EMPTY", source=hc.SOURCE_AWS_EXPORT, labels={"profile": "default"}
    )
    runner = _FakeRunner()
    runner.queue(_ok("\n"))

    with pytest.raises(hc.ResolutionError) as excinfo:
        hc.resolve_aws_export(spec, runner)
    assert "AWS_EMPTY" in str(excinfo.value)


def test_resolve_aws_export_non_json_output_raises_without_quoting_it() -> None:
    hc = _import_hostcreds()
    spec = hc.CredentialSpec(
        name="AWS_BAD", source=hc.SOURCE_AWS_EXPORT, labels={"profile": "default"}
    )
    runner = _FakeRunner()
    leak = _seeded_value("garbage")
    runner.queue(_ok(f"{leak}\n"))

    with pytest.raises(hc.ResolutionError) as excinfo:
        hc.resolve_aws_export(spec, runner)
    message = str(excinfo.value)
    assert "AWS_BAD" in message
    assert "JSON" in message
    assert leak not in message


def test_resolve_aws_export_missing_field_names_the_field() -> None:
    hc = _import_hostcreds()
    spec = hc.CredentialSpec(
        name="AWS_PARTIAL", source=hc.SOURCE_AWS_EXPORT, labels={"profile": "default"}
    )
    runner = _FakeRunner()
    leak = _seeded_value("aki")
    runner.queue(_ok(json.dumps({"AccessKeyId": leak}) + "\n"))

    with pytest.raises(hc.ResolutionError) as excinfo:
        hc.resolve_aws_export(spec, runner)
    message = str(excinfo.value)
    # The missing FIELD is named (metadata, not a secret); the value that
    # WAS present is not.
    assert "SecretAccessKey" in message
    assert leak not in message


def test_resolve_with_an_unknown_source_fails_fast() -> None:
    hc = _import_hostcreds()
    spec = hc.CredentialSpec(name="X", source="vault", labels={})
    with pytest.raises(hc.HostCredsError) as excinfo:
        hc.resolve(spec, _FakeRunner())
    message = str(excinfo.value)
    assert "vault" in message
    for source in (hc.SOURCE_KEYCHAIN, hc.SOURCE_GIT, hc.SOURCE_AWS_EXPORT):
        assert source in message


# ---------------------------------------------------------------------------
# render_env_fragment
# ---------------------------------------------------------------------------


def _keychain_credential(hc: ModuleType, name: str, value: str) -> ResolvedCredential:
    return _resolved(hc, name, hc.SOURCE_KEYCHAIN, {"service": f"devcontainer/x/{name}"}, value)


def test_fragment_starts_with_the_fragment_marker() -> None:
    hc = _import_hostcreds()
    for credential in (
        _keychain_credential(hc, "GITHUB_TOKEN", "v"),
        _resolved(hc, "CHARTS", hc.SOURCE_GIT, {"host": "charts.example.com"}, "pw"),
        _resolved(
            hc,
            "AWS_SESSION",
            hc.SOURCE_AWS_EXPORT,
            {"profile": "default"},
            json.dumps({"AccessKeyId": "AKI", "SecretAccessKey": "SK"}),
        ),
    ):
        fragment = hc.render_env_fragment(credential)
        assert fragment.splitlines()[0] == hc.FRAGMENT_MARKER
        assert fragment.endswith("\n")


def test_fragment_escapes_hostile_values_into_the_export_line() -> None:
    hc = _import_hostcreds()
    hostile = 'it\'s "double" $dollar `tick` \\slash\nnewline\ttab'
    credential = _keychain_credential(hc, "HOSTILE_VALUE", hostile)

    fragment = hc.render_env_fragment(credential)

    expected = "export HOSTILE_VALUE='" + hostile.replace("'", "'\\''") + "'"
    assert expected in fragment
    # Exactly one export statement: the value's own newline spans a second
    # physical line, so counting occurrences (not splitlines-startswith)
    # is the correct single-export invariant here.
    assert fragment.count("export ") == 1
    # No guard for a non-expiring source, and nothing that prints.
    assert "date +%s" not in fragment
    assert "echo " not in fragment


def test_fragment_for_git_source_exports_nothing() -> None:
    hc = _import_hostcreds()
    password = _seeded_value("git-password")
    credential = _resolved(
        hc, "CHARTS_PASSWORD", hc.SOURCE_GIT, {"host": "charts.example.com"}, password
    )

    fragment = hc.render_env_fragment(credential)

    assert not [line for line in fragment.splitlines() if line.startswith("export ")]
    # The password never appears anywhere in the comment-only fragment.
    assert password not in fragment
    assert "git credential" in fragment


def test_fragment_for_aws_export_emits_the_three_aws_variables() -> None:
    hc = _import_hostcreds()
    document = {
        "AccessKeyId": "AKI" + uuid.uuid4().hex[:8].upper(),
        "SecretAccessKey": _seeded_value("sk"),
        "SessionToken": _seeded_value("st"),
        "Expiration": "2030-06-01T12:00:00+00:00",
    }
    credential = _resolved(
        hc,
        "AWS_SESSION",
        hc.SOURCE_AWS_EXPORT,
        {"profile": "sandbox"},
        json.dumps(document),
        expires_at=document["Expiration"],
    )

    fragment = hc.render_env_fragment(credential)

    assert f"export AWS_ACCESS_KEY_ID='{document['AccessKeyId']}'" in fragment
    assert f"export AWS_SECRET_ACCESS_KEY='{document['SecretAccessKey']}'" in fragment
    assert f"export AWS_SESSION_TOKEN='{document['SessionToken']}'" in fragment
    # A non-AWS-var manifest name also exports the raw JSON document
    # (the only place the Expiration field travels).
    assert f"export AWS_SESSION='{json.dumps(document)}'" in fragment


def test_fragment_for_aws_export_omits_an_absent_session_token() -> None:
    hc = _import_hostcreds()
    document = {"AccessKeyId": "AKI", "SecretAccessKey": "SK"}
    credential = _resolved(
        hc, "AWS_STATIC", hc.SOURCE_AWS_EXPORT, {"profile": "default"}, json.dumps(document)
    )

    fragment = hc.render_env_fragment(credential)

    assert "export AWS_ACCESS_KEY_ID='AKI'" in fragment
    assert "export AWS_SECRET_ACCESS_KEY='SK'" in fragment
    assert "AWS_SESSION_TOKEN" not in fragment


def test_fragment_for_aws_export_with_an_aws_var_name_skips_the_raw_export() -> None:
    """Naming an aws-export entry AWS_ACCESS_KEY_ID must not clobber the
    parsed export with the whole JSON document.
    """
    hc = _import_hostcreds()
    document = {"AccessKeyId": "AKI", "SecretAccessKey": "SK"}
    credential = _resolved(
        hc,
        "AWS_ACCESS_KEY_ID",
        hc.SOURCE_AWS_EXPORT,
        {"profile": "default"},
        json.dumps(document),
    )

    fragment = hc.render_env_fragment(credential)

    assert fragment.count("export ") == 2
    assert "export AWS_ACCESS_KEY_ID='AKI'" in fragment


def test_fragment_expiry_guard_carries_the_exact_epoch() -> None:
    hc = _import_hostcreds()
    expires_at = "2030-06-01T12:00:00+00:00"
    # The expected epoch is computed here with datetime, independently of
    # the module, then also pinned to its literal so a timezone-dependent
    # machine cannot make the comparison vacuous.
    expected_epoch = int(datetime.fromisoformat(expires_at).timestamp())
    assert expected_epoch == 1906545600
    credential = _resolved(
        hc,
        "AWS_SESSION",
        hc.SOURCE_AWS_EXPORT,
        {"profile": "default"},
        json.dumps({"AccessKeyId": "AKI", "SecretAccessKey": "SK"}),
        expires_at=expires_at,
    )

    fragment = hc.render_env_fragment(credential)

    assert f'if [ "$(date +%s)" -lt {expected_epoch} ]; then' in fragment
    # Substrings, not the full notice sentence: the wording may be tuned,
    # but it must keep naming the credential, saying it expired, and
    # naming the refresh command.
    assert "notice: AWS_SESSION" in fragment
    assert "expired" in fragment
    assert "make push-creds" in fragment
    assert "echo " not in fragment


def test_fragment_expiry_guard_reads_a_naive_timestamp_as_utc() -> None:
    """The aws CLI always prints an offset; a naive string only reaches the
    renderer through a hand-built credential, and the guard's verdict
    must not then depend on the machine's timezone.
    """
    hc = _import_hostcreds()
    credential = _resolved(
        hc,
        "AWS_SESSION",
        hc.SOURCE_AWS_EXPORT,
        {"profile": "default"},
        json.dumps({"AccessKeyId": "AKI", "SecretAccessKey": "SK"}),
        expires_at="2030-06-01T12:00:00",
    )

    fragment = hc.render_env_fragment(credential)

    expected = int(datetime(2030, 6, 1, 12, 0, 0, tzinfo=UTC).timestamp())
    assert f"-lt {expected} ]; then" in fragment


def test_fragment_with_an_unparseable_expiry_raises() -> None:
    hc = _import_hostcreds()
    credential = _resolved(
        hc,
        "AWS_SESSION",
        hc.SOURCE_AWS_EXPORT,
        {"profile": "default"},
        json.dumps({"AccessKeyId": "AKI", "SecretAccessKey": "SK"}),
        expires_at="not a timestamp",
    )
    with pytest.raises(hc.HostCredsError) as excinfo:
        hc.render_env_fragment(credential)
    assert "AWS_SESSION" in str(excinfo.value)


def test_fragment_with_damaged_aws_json_raises_without_quoting_it() -> None:
    hc = _import_hostcreds()
    leak = _seeded_value("damaged")
    credential = _resolved(hc, "AWS_SESSION", hc.SOURCE_AWS_EXPORT, {"profile": "p"}, leak)
    with pytest.raises(hc.HostCredsError) as excinfo:
        hc.render_env_fragment(credential)
    message = str(excinfo.value)
    assert "AWS_SESSION" in message
    assert "make push-creds" in message
    assert leak not in message


def test_fragment_with_an_empty_aws_field_raises() -> None:
    """The render-side field check matches the resolver's: an empty string
    field is as unusable as a missing one -- an empty AccessKeyId must
    raise, never render `export AWS_ACCESS_KEY_ID=''`.
    """
    hc = _import_hostcreds()
    credential = _resolved(
        hc,
        "AWS_SESSION",
        hc.SOURCE_AWS_EXPORT,
        {"profile": "default"},
        json.dumps({"AccessKeyId": "", "SecretAccessKey": "SK"}),
    )
    with pytest.raises(hc.HostCredsError) as excinfo:
        hc.render_env_fragment(credential)
    message = str(excinfo.value)
    assert "AWS_SESSION" in message
    assert "AccessKeyId" in message


def test_fragment_with_an_unknown_source_raises() -> None:
    hc = _import_hostcreds()
    credential = _resolved(hc, "X", "vault", {}, "v")
    with pytest.raises(hc.HostCredsError):
        hc.render_env_fragment(credential)


# ---------------------------------------------------------------------------
# render_startup_block: the text-level contract
# ---------------------------------------------------------------------------


def test_render_startup_block_is_deterministic() -> None:
    hc = _import_hostcreds()
    assert hc.render_startup_block() == hc.render_startup_block()


def test_render_startup_block_first_line_is_the_marker() -> None:
    hc = _import_hostcreds()
    block = hc.render_startup_block()
    assert block.splitlines()[0] == hc.MARKER
    assert block.count(hc.MARKER) == 1


def test_render_startup_block_contains_the_sourcing_loop_and_guards() -> None:
    hc = _import_hostcreds()
    block = hc.render_startup_block()
    # The store-directory probe is guarded by -d, the per-file loop by the
    # existence check (nullglob-safe in bash), and sourcing by || : so a
    # failing fragment cannot abort the shell or trip an inherited errexit.
    assert '[ -d "$HOME/.hostcreds" ]' in block
    assert '"$HOME"/.hostcreds/*.env' in block
    assert '[ -e "$__hostcreds_fragment" ] || continue' in block
    assert '. "$__hostcreds_fragment" || :' in block


def test_render_startup_block_prints_and_aborts_nothing_by_construction() -> None:
    hc = _import_hostcreds()
    block = hc.render_startup_block()
    assert "echo " not in block
    assert ">&2" not in block, "the block never writes to stderr itself"
    assert "set -e" not in block
    assert re.search(r"(?<![-\w])exit\b", block) is None
    assert re.search(r"(?<![-\w])return\b", block) is None


def test_render_startup_block_honors_a_custom_store_dir_name() -> None:
    hc = _import_hostcreds()
    block = hc.render_startup_block(".creds-store")
    assert '[ -d "$HOME/.creds-store" ]' in block
    assert '"$HOME"/.creds-store/*.env' in block
    assert ".hostcreds" not in block


@pytest.mark.parametrize(
    "bad_name",
    ["", ".", "..", "a/b", "../evil", "a b", "a;b", ".h'x", "a$(x)"],
    ids=[
        "empty",
        "dot",
        "dotdot",
        "slash",
        "traversal",
        "space",
        "semicolon",
        "quote",
        "substitution",
    ],
)
def test_render_startup_block_rejects_unsafe_store_dir_names(bad_name: str) -> None:
    hc = _import_hostcreds()
    with pytest.raises(hc.HostCredsError) as excinfo:
        hc.render_startup_block(bad_name)
    assert repr(bad_name) in str(excinfo.value)


# ---------------------------------------------------------------------------
# End-to-end: the rendered block executed for real, HOME under tmp_path.
# ---------------------------------------------------------------------------


def _require_interpreter(interpreter: str) -> None:
    """Fail fast, with a diagnostic, if `interpreter` is not on PATH.

    The suite's fail-fast interpreter precondition check:
    `subprocess.run([interpreter, ...])` would otherwise raise a raw
    `FileNotFoundError` on a machine missing the shell, instead of failing
    with an actionable message. Not a skip: `make test` checks uv and zsh
    as prerequisites before pytest runs, so a missing interpreter is a
    real precondition failure of the test environment, not an expected
    absence, and it must be loud rather than silently shrinking the
    end-to-end matrix.
    """
    assert shutil.which(interpreter) is not None, (
        f"{interpreter!r} is not on PATH; install it (e.g. via the OS package manager "
        f"or a devcontainer feature) to run the end-to-end cases for {interpreter!r}."
    )


def _run_block(
    shell: str, block: str, home: Path, after: str = ""
) -> subprocess.CompletedProcess[str]:
    """Run `block` under `shell -c` with HOME pointed at `home`.

    A minimal environment (HOME and PATH only) keeps the run hermetic:
    the block can only reach files under `home`, and PATH is inherited
    because the expiry guard's `date` and the shells' error reporting
    need the usual coreutils. `_require_interpreter` runs first so a
    missing shell is a named precondition failure, not a raw
    `FileNotFoundError` from the spawn.
    """
    _require_interpreter(shell)
    script = block + ("\n" + after if after else "")
    env = {"HOME": str(home), "PATH": os.environ.get("PATH", "")}
    return subprocess.run(
        [shell, "-c", script], capture_output=True, text=True, env=env, check=False
    )


def _store(home: Path, hc: ModuleType) -> Path:
    directory = home / hc.DEFAULT_STORE_DIR_NAME
    directory.mkdir(parents=True)
    return directory


@pytest.mark.parametrize("shell", E2E_SHELLS)
def test_block_is_silent_and_successful_without_a_store_dir(tmp_path: Path, shell: str) -> None:
    hc = _import_hostcreds()
    home = tmp_path / "home"
    home.mkdir()

    result = _run_block(shell, hc.render_startup_block(), home)

    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


@pytest.mark.parametrize("shell", E2E_SHELLS)
def test_block_is_silent_with_an_empty_store_dir(tmp_path: Path, shell: str) -> None:
    """The zsh-specific reason the glob probe exists: a bare for-glob loop
    aborts zsh outright when nothing matches, so the block must stay
    silent here in both shells, not only in bash.
    """
    hc = _import_hostcreds()
    home = tmp_path / "home"
    home.mkdir()
    _store(home, hc)

    result = _run_block(shell, hc.render_startup_block(), home)

    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


@pytest.mark.parametrize("shell", E2E_SHELLS)
def test_block_sources_a_fragment_and_exports_its_variable(tmp_path: Path, shell: str) -> None:
    hc = _import_hostcreds()
    home = tmp_path / "home"
    home.mkdir()
    store = _store(home, hc)
    (store / "HC_E2E_VAR.env").write_text(
        "export HC_E2E_VAR=hello-from-fragment\n", encoding="utf-8"
    )

    result = _run_block(
        shell,
        hc.render_startup_block(),
        home,
        after='printf "VAR=[%s]\\n" "${HC_E2E_VAR:-UNSET}"',
    )

    assert result.returncode == 0, result.stderr
    assert "VAR=[hello-from-fragment]" in result.stdout


@pytest.mark.parametrize("shell", E2E_SHELLS)
def test_block_forwards_a_fragment_notice(tmp_path: Path, shell: str) -> None:
    """Fragments print their own notices; the block must not swallow them
    (only its internal glob probe is silenced).
    """
    hc = _import_hostcreds()
    home = tmp_path / "home"
    home.mkdir()
    store = _store(home, hc)
    (store / "NOTICE.env").write_text(
        "printf '%s\\n' 'notice: from-fragment' >&2\n", encoding="utf-8"
    )

    result = _run_block(shell, hc.render_startup_block(), home)

    assert result.returncode == 0
    assert "notice: from-fragment" in result.stderr


@pytest.mark.parametrize("shell", E2E_SHELLS)
def test_block_survives_a_broken_fragment_and_continues(tmp_path: Path, shell: str) -> None:
    hc = _import_hostcreds()
    home = tmp_path / "home"
    home.mkdir()
    store = _store(home, hc)
    # A_BROKEN sorts before Z_GOOD, so the good fragment is only reached
    # if the loop continued past the broken one.
    (store / "A_BROKEN.env").write_text("definitely_not_a_command_xyz_123\n", encoding="utf-8")
    (store / "Z_GOOD.env").write_text("export HC_GOOD=still-applied\n", encoding="utf-8")

    result = _run_block(
        shell,
        hc.render_startup_block(),
        home,
        after='printf "GOOD=[%s]\\n" "${HC_GOOD:-UNSET}"',
    )

    assert result.returncode == 0
    assert "GOOD=[still-applied]" in result.stdout


@pytest.mark.parametrize("shell", E2E_SHELLS)
def test_block_round_trips_a_hostile_value_byte_for_byte(tmp_path: Path, shell: str) -> None:
    """A rendered fragment written to the store, sourced through the block,
    sets its variable to the exact original value -- quotes, dollars,
    backslashes, newlines and all.
    """
    hc = _import_hostcreds()
    hostile = 'it\'s "double" $dollar `tick` \\slash\nnewline\ttab'
    credential = _keychain_credential(hc, "HOSTILE_VALUE", hostile)
    home = tmp_path / "home"
    home.mkdir()
    store = _store(home, hc)
    (store / "HOSTILE_VALUE.env").write_text(
        hc.render_env_fragment(credential),
        encoding="utf-8",
    )

    result = _run_block(shell, hc.render_startup_block(), home, after='printf %s "$HOSTILE_VALUE"')

    assert result.returncode == 0, result.stderr
    assert result.stdout == hostile


@pytest.mark.parametrize("shell", E2E_SHELLS)
def test_block_expired_aws_fragment_notices_and_exports_nothing(tmp_path: Path, shell: str) -> None:
    hc = _import_hostcreds()
    credential = _resolved(
        hc,
        "AWS_SESSION",
        hc.SOURCE_AWS_EXPORT,
        {"profile": "default"},
        json.dumps({"AccessKeyId": "AKI", "SecretAccessKey": "SK", "SessionToken": "ST"}),
        expires_at="2020-06-01T12:00:00+00:00",
    )
    home = tmp_path / "home"
    home.mkdir()
    store = _store(home, hc)
    (store / "AWS_SESSION.env").write_text(hc.render_env_fragment(credential), encoding="utf-8")

    result = _run_block(
        shell,
        hc.render_startup_block(),
        home,
        after='printf "AK=[%s]\\n" "${AWS_ACCESS_KEY_ID:-UNSET}"',
    )

    assert result.returncode == 0, result.stderr
    assert "AK=[UNSET]" in result.stdout
    assert "notice: AWS_SESSION" in result.stderr
    assert "expired" in result.stderr
    assert "make push-creds" in result.stderr
    assert "notice:" not in result.stdout


@pytest.mark.parametrize("shell", E2E_SHELLS)
def test_block_unexpired_aws_fragment_exports_the_credentials(tmp_path: Path, shell: str) -> None:
    hc = _import_hostcreds()
    secret = _seeded_value("sk")
    credential = _resolved(
        hc,
        "AWS_SESSION",
        hc.SOURCE_AWS_EXPORT,
        {"profile": "default"},
        json.dumps({"AccessKeyId": "AKI", "SecretAccessKey": secret, "SessionToken": "ST"}),
        expires_at="2030-06-01T12:00:00+00:00",
    )
    home = tmp_path / "home"
    home.mkdir()
    store = _store(home, hc)
    (store / "AWS_SESSION.env").write_text(hc.render_env_fragment(credential), encoding="utf-8")

    result = _run_block(
        shell,
        hc.render_startup_block(),
        home,
        after='printf "SK=[%s]\\n" "${AWS_SECRET_ACCESS_KEY:-UNSET}"',
    )

    assert result.returncode == 0, result.stderr
    assert f"SK=[{secret}]" in result.stdout
