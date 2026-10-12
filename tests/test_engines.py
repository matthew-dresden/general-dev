"""Tests for the SSH engine kind: the engine.json manifest surface in
`devcontainer_config.instances`, the `scaffold_engine` write path in
`devcontainer_config.instance_ops`, and the kind-specific resolve-instance
address block in `devcontainer_config.cli`.

The `devcontainer_config` imports are deferred into function bodies (via
`_import_instances` / `_import_instance_ops`), the same convention
`tests/test_instances.py` and `tests/test_instance_ops.py` document: a
module-level import would fail COLLECTION for the whole file instead of
failing the one test for the real reason.

No test here touches docker, AWS, a keychain or the network. `scaffold_engine`
is the one production path this file drives that takes a `Runner`, and it
must issue no command at all (an SSH engine has no AMI to resolve and no
CIDRs to allocate), so every test hands it `_NoCommandRunner`, which FAILS
the test if any argv ever reaches it -- a stronger guarantee than a queue
that merely answers nothing.

Every fixture repository is a real, disposable git repository built under
`tmp_path` by shelling out to the actual `git` binary, via
`tests/gitfixtures.py`'s `generated_root`/`init_repo` plus a local `_git_root`.
`_git_root` mirrors the identically named helper in `tests/test_instance_ops.py`
(origin slug `general-dev`, and the real checkout's committed `.gitignore`
read from HEAD so a scratch repo exercises the genuine per-instance scaffold
anchor) rather than importing it: the shared module `tests/gitfixtures.py` is
outside this unit's change manifest, and the helper's docstring there records
the same stay-local reasoning `tests/test_instances.py` documents for its own
`_init_repo_with_origin`.

Every generated value -- instance names, engine hosts, users and ports -- is
built from `uuid.uuid4()` at test time, so no host- or endpoint-shaped
literal is stored in this file.
"""

from __future__ import annotations

import importlib
import json
import re
import subprocess
import uuid
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from types import ModuleType

import pytest
from gitfixtures import generated_root, import_cli, init_repo, run_cli


def _import_instances() -> ModuleType:
    """Import devcontainer_config.instances from inside a function body."""
    return importlib.import_module("devcontainer_config.instances")


def _import_instance_ops() -> ModuleType:
    """Import devcontainer_config.instance_ops from inside a function body."""
    return importlib.import_module("devcontainer_config.instance_ops")


def _import_repo() -> ModuleType:
    """Import devcontainer_config.repo from inside a function body."""
    return importlib.import_module("devcontainer_config.repo")


@pytest.fixture(autouse=True)
def _docker_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point DOCKER_CONFIG at a per-test directory.

    The address-block tests assert `CERTS_DIR=`, which resolves through
    `instances.certs_dir` and its `DOCKER_CONFIG` honor; setting it here
    keeps every assertion on `tmp_path` and away from the operator's real
    `~/.docker/certs`. `certs` is imported BEFORE the env var is set, for
    the same module-freezing reason `tests/test_instance_ops.py`'s
    identical fixture documents: when this file runs before
    `tests/test_certs.py` in one session, that first import would otherwise
    freeze `certs.DEFAULT_CERTS_ROOT` to this test's tmp path.
    """
    importlib.import_module("devcontainer_config.certs")
    docker_config = tmp_path / "docker-config"
    docker_config.mkdir()
    monkeypatch.setenv("DOCKER_CONFIG", str(docker_config))
    return docker_config


class _NoCommandRunner:
    """A Runner double that records and refuses: an SSH engine scaffold issues no command.

    `scaffold_engine` takes a `runner` for call-site uniformity with
    `scaffold` and must never use it. An invocation is therefore a test
    failure naming the argv, not a queued answer that would let a stray aws
    or docker call pass silently.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def __call__(
        self, argv: Sequence[str], stdin: str | None, *, env: Mapping[str, str] | None = None
    ) -> object:
        self.calls.append(tuple(argv))
        raise AssertionError(f"an SSH engine scaffold issued a command: {tuple(argv)!r}")


def _instance_name(prefix: str = "engine") -> str:
    """A valid instance name unique per call."""
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _engine_host() -> str:
    """A host-shaped value unique per call, under the reserved example.invalid domain."""
    return f"engine-{uuid.uuid4().hex[:8]}.example.invalid"


def _engine_user() -> str:
    """A user name unique per call."""
    return f"dev-{uuid.uuid4().hex[:8]}"


def _engine_port() -> int:
    """A valid port number unique per call, inside the manifest's documented bounds."""
    instances = _import_instances()
    return instances.MIN_ENGINE_PORT + 20000 + uuid.uuid4().int % 20000


def _git_root(tmp_path: Path) -> Path:
    """A disposable git checkout whose origin slug is this repository's own.

    Carries the real checkout's committed `.gitignore` -- read from HEAD,
    not the working tree -- so a scaffold's per-instance ignore append
    exercises the genuine anchor rule; see `tests/test_instance_ops.py`'s
    `_git_root` for the full reasoning this local copy mirrors.
    """
    root = generated_root(tmp_path)
    init_repo(root)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "remote",
            "add",
            "origin",
            "https://example.invalid/org/general-dev.git",
        ],
        check=True,
        capture_output=True,
    )
    real_repo_root = _import_repo().find_root(Path(__file__).resolve().parent)
    committed_gitignore = subprocess.run(
        ["git", "-C", str(real_repo_root), "show", "HEAD:.gitignore"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    (root / ".gitignore").write_text(committed_gitignore, encoding="utf-8")
    return root


def _seed_instance_dir(root: Path, name: str) -> Path:
    """A minimal EC2-kind instance directory under `remote-instances/<name>/`."""
    instance_dir = root / "remote-instances" / name
    instance_dir.mkdir(parents=True)
    (instance_dir / "terragrunt.hcl").write_text("# seeded\n", encoding="utf-8")
    return instance_dir


def _write_manifest(root: Path, name: str, payload: object) -> Path:
    """A raw engine.json under `remote-instances/<name>/`, exactly as given."""
    manifest = root / "remote-instances" / name / "engine.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    return manifest


def _valid_manifest_payload() -> dict[str, object]:
    """A complete, valid manifest payload, every value generated per call."""
    instances = _import_instances()
    return {
        "kind": instances.ENGINE_KIND_SSH,
        "host": _engine_host(),
        "user": _engine_user(),
        "port": _engine_port(),
        "docker_addr": f"/var/run/docker-{uuid.uuid4().hex[:8]}.sock",
    }


def _gitignore_lines(root: Path) -> list[str]:
    """The scratch checkout's `.gitignore`, one list entry per line."""
    return (root / ".gitignore").read_text(encoding="utf-8").splitlines()


def _clear_resolution_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop both resolution selectors, the way `tests/test_instances.py` does."""
    instances = _import_instances()
    monkeypatch.delenv(instances.INSTANCE_ENV_VAR, raising=False)
    monkeypatch.delenv(instances.DEFAULT_REMOTE_INSTANCE_ENV_VAR, raising=False)


# ---------------------------------------------------------------------------
# read_kind
# ---------------------------------------------------------------------------


def test_read_kind_defaults_to_ec2_for_a_terragrunt_directory(tmp_path: Path) -> None:
    instances = _import_instances()
    root = generated_root(tmp_path)
    name = _instance_name()
    _seed_instance_dir(root, name)

    assert instances.read_kind(root, name) == instances.ENGINE_KIND_EC2


def test_read_kind_reports_ssh_when_the_manifest_is_present(tmp_path: Path) -> None:
    instances = _import_instances()
    root = generated_root(tmp_path)
    name = _instance_name()
    _write_manifest(root, name, _valid_manifest_payload())

    assert instances.read_kind(root, name) == instances.ENGINE_KIND_SSH


def test_read_kind_reports_ec2_for_a_directory_with_neither_file(tmp_path: Path) -> None:
    """Backward compatibility of `discover`: a bare directory stays the EC2 kind."""
    instances = _import_instances()
    root = generated_root(tmp_path)
    name = _instance_name()
    (root / "remote-instances" / name).mkdir(parents=True)

    assert instances.read_kind(root, name) == instances.ENGINE_KIND_EC2


def test_read_kind_fails_fast_on_an_unparsable_manifest_naming_the_file(
    tmp_path: Path,
) -> None:
    instances = _import_instances()
    root = generated_root(tmp_path)
    name = _instance_name()
    manifest = root / "remote-instances" / name / "engine.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("not-json{", encoding="utf-8")

    with pytest.raises(instances.EngineManifestError) as exc_info:
        instances.read_kind(root, name)

    message = str(exc_info.value)
    assert str(manifest) in message
    assert "Expecting value" in message


def test_read_kind_rejects_an_invalid_name(tmp_path: Path) -> None:
    instances = _import_instances()
    root = generated_root(tmp_path)

    with pytest.raises(instances.InvalidInstanceNameError):
        instances.read_kind(root, "../escape")


# ---------------------------------------------------------------------------
# engine_manifest: round trip, defaults, refusals
# ---------------------------------------------------------------------------


def test_engine_manifest_filename_constant_is_engine_json() -> None:
    instances = _import_instances()

    assert instances.ENGINE_MANIFEST_FILENAME == "engine.json"


def test_engine_manifest_round_trips_a_full_manifest(tmp_path: Path) -> None:
    instances = _import_instances()
    root = generated_root(tmp_path)
    name = _instance_name()
    payload = _valid_manifest_payload()
    _write_manifest(root, name, payload)

    assert instances.engine_manifest(root, name) == payload


def test_engine_manifest_accepts_a_tcp_docker_addr(tmp_path: Path) -> None:
    """The `tcp://<host>:<port>` form round-trips exactly as given."""
    instances = _import_instances()
    root = generated_root(tmp_path)
    name = _instance_name()
    payload = _valid_manifest_payload()
    payload["docker_addr"] = f"tcp://{_engine_host()}:{_engine_port()}"
    _write_manifest(root, name, payload)

    assert instances.engine_manifest(root, name) == payload


def test_engine_manifest_applies_the_port_and_docker_addr_defaults(tmp_path: Path) -> None:
    instances = _import_instances()
    root = generated_root(tmp_path)
    name = _instance_name()
    payload = {"kind": "ssh", "host": _engine_host(), "user": _engine_user()}
    _write_manifest(root, name, payload)

    values = instances.engine_manifest(root, name)

    assert values["port"] == instances.DEFAULT_ENGINE_PORT
    assert values["docker_addr"] == instances.DEFAULT_ENGINE_DOCKER_ADDR
    assert values["kind"] == instances.ENGINE_KIND_SSH
    assert values["host"] == payload["host"]
    assert values["user"] == payload["user"]


def test_the_default_constants_carry_the_specified_values() -> None:
    """The defaults are sourced from constants everywhere; this pins the constants
    themselves to the values the engine manifest contract fixes."""
    instances = _import_instances()

    assert instances.DEFAULT_ENGINE_PORT == 22
    assert instances.DEFAULT_ENGINE_DOCKER_ADDR == "/var/run/docker.sock"


def test_engine_manifest_refuses_a_payload_that_is_not_an_object(tmp_path: Path) -> None:
    instances = _import_instances()
    root = generated_root(tmp_path)
    name = _instance_name()
    _write_manifest(root, name, ["not", "an", "object"])

    with pytest.raises(instances.EngineManifestError, match="JSON object"):
        instances.engine_manifest(root, name)


def test_engine_manifest_refuses_a_missing_manifest_naming_the_file(
    tmp_path: Path,
) -> None:
    instances = _import_instances()
    root = generated_root(tmp_path)
    name = _instance_name()

    with pytest.raises(instances.EngineManifestError) as exc_info:
        instances.engine_manifest(root, name)

    assert str(instances.engine_manifest_path(root, name)) in str(exc_info.value)


# Each case mutates a valid payload into one specific violation; the second
# element is the manifest field the refusal must name. The mutators run at
# test time on a generated payload, never at collection time.
_MANIFEST_REFUSALS: tuple[tuple[str, Callable[[dict[str, object]], object], str], ...] = (
    ("kind missing", lambda values: {k: values[k] for k in values if k != "kind"}, "kind"),
    ("kind is not ssh", lambda values: {**values, "kind": "ec2"}, "kind"),
    ("host missing", lambda values: {k: values[k] for k in values if k != "host"}, "host"),
    ("host empty", lambda values: {**values, "host": ""}, "host"),
    ("host not a string", lambda values: {**values, "host": 22}, "host"),
    ("host carries a scheme", lambda values: {**values, "host": "ssh://h.example.invalid"}, "host"),
    ("user missing", lambda values: {k: values[k] for k in values if k != "user"}, "user"),
    ("user empty", lambda values: {**values, "user": ""}, "user"),
    ("port below range", lambda values: {**values, "port": 0}, "port"),
    ("port above range", lambda values: {**values, "port": 65536}, "port"),
    ("port not an integer", lambda values: {**values, "port": "22"}, "port"),
    ("port is a boolean", lambda values: {**values, "port": True}, "port"),
    (
        "docker_addr is relative",
        lambda values: {**values, "docker_addr": "var/run/docker.sock"},
        "docker_addr",
    ),
    (
        "docker_addr is relative",
        lambda values: {**values, "docker_addr": "var/run/docker.sock"},
        "docker_addr",
    ),
    (
        "docker_addr is empty",
        lambda values: {**values, "docker_addr": ""},
        "docker_addr",
    ),
    (
        "docker_addr not a string",
        lambda values: {**values, "docker_addr": 2375},
        "docker_addr",
    ),
    (
        "docker_addr tcp without a port",
        lambda values: {**values, "docker_addr": "tcp://h.example.invalid"},
        "docker_addr",
    ),
    (
        "docker_addr tcp with a non-numeric port",
        lambda values: {**values, "docker_addr": "tcp://h.example.invalid:ssh"},
        "docker_addr",
    ),
    (
        "docker_addr tcp port out of range",
        lambda values: {**values, "docker_addr": "tcp://h.example.invalid:70000"},
        "docker_addr",
    ),
)


@pytest.mark.parametrize(
    ("label", "mutate", "field"),
    _MANIFEST_REFUSALS,
    ids=[label for label, _mutate, _field in _MANIFEST_REFUSALS],
)
def test_engine_manifest_refusals_name_the_field_and_the_rule(
    tmp_path: Path, label: str, mutate: Callable[[dict[str, object]], object], field: str
) -> None:
    instances = _import_instances()
    root = generated_root(tmp_path)
    name = _instance_name()
    _write_manifest(root, name, mutate(_valid_manifest_payload()))

    with pytest.raises(instances.EngineManifestError) as exc_info:
        instances.engine_manifest(root, name)

    message = str(exc_info.value)
    assert f"field {field!r}" in message, f"{label}: {message}"
    assert "Fix the field named above" in message, f"{label}: {message}"


# ---------------------------------------------------------------------------
# engine_endpoint
# ---------------------------------------------------------------------------


def test_engine_endpoint_is_ssh_user_host_port(tmp_path: Path) -> None:
    instances = _import_instances()
    root = generated_root(tmp_path)
    name = _instance_name()
    host, user, port = _engine_host(), _engine_user(), _engine_port()
    _write_manifest(root, name, {"kind": "ssh", "host": host, "user": user, "port": port})

    assert instances.engine_endpoint(root, name) == f"ssh://{user}@{host}:{port}"


def test_engine_endpoint_uses_the_default_port_when_the_manifest_omits_one(
    tmp_path: Path,
) -> None:
    instances = _import_instances()
    root = generated_root(tmp_path)
    name = _instance_name()
    host, user = _engine_host(), _engine_user()
    _write_manifest(root, name, {"kind": "ssh", "host": host, "user": user})

    assert instances.engine_endpoint(root, name) == (
        f"ssh://{user}@{host}:{instances.DEFAULT_ENGINE_PORT}"
    )


# ---------------------------------------------------------------------------
# The resolve-instance address block, per kind (via the CLI entry point)
# ---------------------------------------------------------------------------


def test_resolve_instance_ec2_block_is_exactly_the_six_pinned_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The EC2 block is byte-for-byte today's six lines, in order, unchanged."""
    instances = _import_instances()
    root = _git_root(tmp_path)
    name = _instance_name()
    _seed_instance_dir(root, name)
    _clear_resolution_env(monkeypatch)

    exit_code = run_cli(monkeypatch, root, ["resolve-instance"])

    out = capsys.readouterr().out
    assert exit_code == 0
    assert out.splitlines() == [
        f"INSTANCE={name}",
        f"TERRAGRUNT_DIR={instances.terragrunt_dir(root, name)}",
        f"STATE_KEY={instances.state_key(name)}",
        f"DOCKER_CONTEXT={instances.docker_context(root, name)}",
        f"PARAMETER_PREFIX={instances.parameter_prefix(name)}",
        f"CERTS_DIR={instances.certs_dir(name)}",
    ]


def test_resolve_instance_ssh_block_is_the_nine_engine_lines_without_ec2_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An SSH engine prints the ENGINE_-prefixed block, and none of the
    Terragrunt/state/Parameter Store rows an SSH engine has no counterpart for."""
    instances = _import_instances()
    root = _git_root(tmp_path)
    name = _instance_name()
    host, user, port = _engine_host(), _engine_user(), _engine_port()
    docker_addr = f"/var/run/docker-{uuid.uuid4().hex[:8]}.sock"
    _write_manifest(
        root,
        name,
        {"kind": "ssh", "host": host, "user": user, "port": port, "docker_addr": docker_addr},
    )
    _clear_resolution_env(monkeypatch)

    exit_code = run_cli(monkeypatch, root, ["resolve-instance"])

    out = capsys.readouterr().out
    assert exit_code == 0
    assert out.splitlines() == [
        f"INSTANCE={name}",
        f"KIND={instances.ENGINE_KIND_SSH}",
        f"DOCKER_CONTEXT={instances.docker_context(root, name)}",
        f"CERTS_DIR={instances.certs_dir(name)}",
        f"ENGINE_HOST={host}",
        f"ENGINE_PORT={port}",
        f"ENGINE_USER={user}",
        f"ENGINE_DOCKER_ADDR={docker_addr}",
        f"ENGINE_ENDPOINT=ssh://{user}@{host}:{port}",
    ]
    assert "TERRAGRUNT_DIR" not in out
    assert "STATE_KEY" not in out
    assert "PARAMETER_PREFIX" not in out


# ---------------------------------------------------------------------------
# scaffold_engine
# ---------------------------------------------------------------------------


def test_scaffold_engine_writes_the_manifest_with_the_given_inputs(tmp_path: Path) -> None:
    ops = _import_instance_ops()
    instances = _import_instances()
    root = _git_root(tmp_path)
    name = _instance_name()
    host, user, port = _engine_host(), _engine_user(), _engine_port()
    docker_addr = f"/var/run/docker-{uuid.uuid4().hex[:8]}.sock"
    runner = _NoCommandRunner()

    result = ops.scaffold_engine(
        root, name, host=host, port=port, user=user, docker_addr=docker_addr, runner=runner
    )

    expected_values = {
        "kind": instances.ENGINE_KIND_SSH,
        "host": host,
        "user": user,
        "port": port,
        "docker_addr": docker_addr,
    }
    assert result.path == instances.engine_manifest_path(root, name)
    text = result.path.read_text(encoding="utf-8")
    assert text == json.dumps(expected_values, indent=2) + "\n"
    assert json.loads(text) == expected_values
    assert any(f"created {result.path}" in message for message in result.messages)
    assert any(f"ssh://{user}@{host}:{port}" in message for message in result.messages), (
        result.messages
    )
    assert runner.calls == []


def test_scaffold_engine_defaults_land_in_the_manifest(tmp_path: Path) -> None:
    ops = _import_instance_ops()
    instances = _import_instances()
    root = _git_root(tmp_path)
    name = _instance_name()
    host, user = _engine_host(), _engine_user()

    result = ops.scaffold_engine(root, name, host=host, user=user, runner=_NoCommandRunner())

    values = json.loads(result.path.read_text(encoding="utf-8"))
    assert values["port"] == instances.DEFAULT_ENGINE_PORT
    assert values["docker_addr"] == instances.DEFAULT_ENGINE_DOCKER_ADDR


def test_scaffold_engine_refuses_an_existing_directory(tmp_path: Path) -> None:
    ops = _import_instance_ops()
    instances = _import_instances()
    root = _git_root(tmp_path)
    name = _instance_name()
    directory = _seed_instance_dir(root, name)
    manifest = directory / instances.ENGINE_MANIFEST_FILENAME
    manifest.write_text("# an operator's own manifest\n", encoding="utf-8")
    before = manifest.read_text(encoding="utf-8")

    with pytest.raises(ops.ScaffoldError) as exc_info:
        ops.scaffold_engine(
            root, name, host=_engine_host(), user=_engine_user(), runner=_NoCommandRunner()
        )

    assert str(directory) in str(exc_info.value)
    assert str(manifest) in str(exc_info.value)
    assert manifest.read_text(encoding="utf-8") == before


def test_scaffold_engine_appends_exactly_one_gitignore_entry_below_the_anchor(
    tmp_path: Path,
) -> None:
    """The same per-instance ignore append `scaffold` makes, below the same anchor."""
    ops = _import_instance_ops()
    root = _git_root(tmp_path)
    name = _instance_name()

    ops.scaffold_engine(
        root, name, host=_engine_host(), user=_engine_user(), runner=_NoCommandRunner()
    )

    lines = _gitignore_lines(root)
    entry = f"remote-instances/{name}/"
    assert lines.count(entry) == 1
    assert lines.index(entry) == lines.index(ops.GITIGNORE_SCAFFOLD_ANCHOR) + 1


def test_scaffold_engine_rejects_an_invalid_name_before_anything_runs(tmp_path: Path) -> None:
    ops = _import_instance_ops()
    root = generated_root(tmp_path)
    runner = _NoCommandRunner()

    for bad_name in ("../escape", "a/b", ""):
        with pytest.raises(ops.ScaffoldError):
            ops.scaffold_engine(
                root, bad_name, host=_engine_host(), user=_engine_user(), runner=runner
            )

    assert runner.calls == []
    assert not (root / "remote-instances").exists()


def test_scaffold_engine_context_name_length_headroom(tmp_path: Path) -> None:
    """The same 63-character docker-context bound `scaffold` enforces: the context
    name is the repository slug plus the instance name, here `general-dev-`."""
    ops = _import_instance_ops()
    root = _git_root(tmp_path)
    longest_ok = "a" * 51  # "general-dev-" + 51 chars == 63, exactly at the limit
    too_long = "a" * 52  # 64 > 63

    accepted = ops.scaffold_engine(
        root, longest_ok, host=_engine_host(), user=_engine_user(), runner=_NoCommandRunner()
    )
    assert accepted.path.is_file()

    with pytest.raises(ops.ScaffoldError) as exc_info:
        ops.scaffold_engine(
            root, too_long, host=_engine_host(), user=_engine_user(), runner=_NoCommandRunner()
        )

    message = str(exc_info.value)
    assert str(ops.DOCKER_CONTEXT_NAME_LIMIT) in message
    assert f"general-dev-{too_long}" in message


def test_scaffold_engine_refuses_a_manifest_field_violation_writing_nothing(
    tmp_path: Path,
) -> None:
    """The write side validates through the same surface the read side uses, so a
    manifest `engine_manifest` would refuse can never be written."""
    ops = _import_instance_ops()
    instances = _import_instances()
    root = _git_root(tmp_path)
    name = _instance_name()

    with pytest.raises(ops.ScaffoldError) as exc_info:
        ops.scaffold_engine(
            root,
            name,
            host=f"ssh://{_engine_host()}",
            user=_engine_user(),
            runner=_NoCommandRunner(),
        )

    message = str(exc_info.value)
    assert "field 'host'" in message
    assert not instances.engine_manifest_path(root, name).exists()
    assert not (root / "remote-instances" / name).exists()


def test_scaffold_engine_refusal_leaves_gitignore_untouched(tmp_path: Path) -> None:
    """The existing-directory refusal fires before any `.gitignore` append."""
    ops = _import_instance_ops()
    root = _git_root(tmp_path)
    name = _instance_name()
    _seed_instance_dir(root, name)
    before = (root / ".gitignore").read_text(encoding="utf-8")

    with pytest.raises(ops.ScaffoldError):
        ops.scaffold_engine(
            root, name, host=_engine_host(), user=_engine_user(), runner=_NoCommandRunner()
        )

    assert (root / ".gitignore").read_text(encoding="utf-8") == before


# ---------------------------------------------------------------------------
# cli engine-init
# ---------------------------------------------------------------------------


def test_engine_init_cli_writes_the_manifest_with_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    instances = _import_instances()
    root = _git_root(tmp_path)
    name, host, user = _instance_name(), _engine_host(), _engine_user()

    exit_code = run_cli(monkeypatch, root, ["engine-init", name, "--host", host, "--user", user])

    out = capsys.readouterr().out
    assert exit_code == 0
    manifest = instances.engine_manifest_path(root, name)
    assert manifest.is_file()
    values = json.loads(manifest.read_text(encoding="utf-8"))
    assert values == {
        "kind": instances.ENGINE_KIND_SSH,
        "host": host,
        "user": user,
        "port": instances.DEFAULT_ENGINE_PORT,
        "docker_addr": instances.DEFAULT_ENGINE_DOCKER_ADDR,
    }
    assert any(f"created {manifest}" in line for line in out.splitlines())
    assert any(str(manifest) in line for line in out.splitlines())


def test_engine_init_cli_passes_explicit_port_and_docker_addr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    instances = _import_instances()
    root = _git_root(tmp_path)
    name, host, user, port = _instance_name(), _engine_host(), _engine_user(), _engine_port()
    docker_addr = f"/var/run/docker-{uuid.uuid4().hex[:8]}.sock"

    exit_code = run_cli(
        monkeypatch,
        root,
        [
            "engine-init",
            name,
            "--host",
            host,
            "--user",
            user,
            "--port",
            str(port),
            "--docker-addr",
            docker_addr,
        ],
    )

    assert exit_code == 0
    values = json.loads(instances.engine_manifest_path(root, name).read_text(encoding="utf-8"))
    assert values["port"] == port
    assert values["docker_addr"] == docker_addr


def test_engine_init_cli_missing_host_is_a_usage_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    instances = _import_instances()
    root = _git_root(tmp_path)
    name, user = _instance_name(), _engine_user()

    exit_code = run_cli(monkeypatch, root, ["engine-init", name, "--user", user])

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "--host" in captured.err
    assert not instances.engine_manifest_path(root, name).exists()


def test_module_help_lists_engine_init(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Wiring smoke test: `engine-init` is a subcommand of `main`."""
    cli = import_cli()
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["--help"])

    assert exc_info.value.code == 0
    assert "engine-init" in capsys.readouterr().out


def test_engine_init_help_names_every_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cli = import_cli()
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["engine-init", "--help"])

    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    for flag in ("--host", "--user", "--port", "--docker-addr"):
        assert flag in out, f"{flag} is missing from engine-init's help"
    assert re.search(r"--host\b", out)
