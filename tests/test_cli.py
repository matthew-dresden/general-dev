"""Tests for devcontainer_config.cli: the `lint-secrets --range`, `hooks-install`,

`hooks-check` and `hooks-pre-push` entry points (E2-F1-S2-T1, E2-F2-S1-T1).

`tests/test_lint_secrets_cli.py` already covers the `lint-secrets` command's
staged-mode behavior in full (E2-F1-S1-T2). This file covers what E2-F1-S2-T1
added to the CLI (the `--range <a>..<b>` flag, its mutual exclusivity with
`--staged`, and its help text -- spec Section 4.1.2, AC-DOC-002) and what
E2-F2-S1-T1 adds on top: the `hooks-install`, `hooks-check` and
`hooks-pre-push` subcommands that wrap `devcontainer_config.githooks`.

The `devcontainer_config` import is deferred into function bodies (via
`import_cli` / `import_secrets`), for the same reason documented in
`tests/test_lint_secrets_cli.py`: the TDD RED gate stashes this unit's
production-source files and re-runs a single named test node, and a
module-level `from devcontainer_config.cli import ...` would fail
COLLECTION for the whole file instead of failing the one test for the real
reason.

Every fixture repository here is a real, disposable git repository created
under `tmp_path` by shelling out to the actual `git` binary. No
credential-shaped literal is ever stored pre-assembled: a positive sample is
built at run time from `devcontainer_config.secrets.SAMPLE_PREFIXES` plus a
`uuid.uuid4()` suffix, the same discipline every other test file in this
suite documents.

Every one of those primitives lives in `tests/gitfixtures.py` (shared with
`tests/test_secrets_range.py` and `tests/test_lint_secrets_cli.py`) rather
than being redefined here; see that module's docstring for why. `run_cli`
does not feed anything to stdin, so `_run_cli_with_stdin` below is a local,
minimal variant used only by the `hooks-pre-push` tests, which need to feed
git's own pre-push stdin contract; it stays local rather than joining
`gitfixtures.py` because that file is outside this task's Changes Manifest.

E3-F2-S1-T1 adds coverage for the `devsecret` entry point (`get`, `list`,
`set`, `rm` -- spec Section 4.3), the second console entry point this module
exposes. `_FakeCatalogRunner`, `_ok`, `_err` and `_seeded_value` below are a
local duplicate of the doubles `tests/test_catalog.py` already defines for
`devcontainer_config.catalog`: that file is outside this task's Changes
Manifest (the same reason `_run_cli_with_stdin` above stays local), and
`tests/gitfixtures.py` is reserved for the git-repository primitives every
other consumer shares, not for a catalog double only this section needs. No
seeded value here is a real credential; every one is a deterministically
generated placeholder built from `uuid.uuid4()` at test time, the same
discipline `tests/test_catalog.py` documents for its own case table.
"""

from __future__ import annotations

import importlib
import io
import json
import subprocess
import tomllib
import uuid
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest
from gitfixtures import (
    commit_text,
    credential_line,
    generated_root,
    import_cli,
    init_repo,
    rev_parse,
    run_cli,
)


def _run_cli_with_stdin(
    monkeypatch: pytest.MonkeyPatch, root: Path, args: list[str], stdin_text: str
) -> int:
    """Like `gitfixtures.run_cli`, but also feeds `stdin_text` to `cli.main` via stdin."""
    cli = import_cli()
    monkeypatch.chdir(root)
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin_text))
    with pytest.raises(SystemExit) as exc_info:
        cli.main(args)
    code = exc_info.value.code
    if not isinstance(code, int):
        raise AssertionError(f"cli.main exited with a non-integer code: {code!r}")
    return code


def test_range_flag_reports_finding_attributed_to_commit_and_exits_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC-FUNC-001 / AC-FUNC-006: `--range` scans the named range and exits 1 on a finding."""
    root = generated_root(tmp_path)
    init_repo(root)
    commit_text(root, "README.md", "base\n", "base commit")
    base_commit = rev_parse(root, "HEAD")
    line = credential_line()
    commit_text(root, "src/config.py", line, "add credential")
    credential_commit = rev_parse(root, "HEAD")

    exit_code = run_cli(monkeypatch, root, ["lint-secrets", "--range", f"{base_commit}..HEAD"])

    out = capsys.readouterr().out
    assert exit_code == 1
    assert credential_commit in out
    assert "src/config.py:1" in out
    assert "AWS access key identifier" in out
    assert "The value is in history, so removing it now is not enough." in out


def test_range_with_no_commits_exits_zero_and_reports_zero_commits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC-TEST-006 / AC-FUNC-006: an empty range exits 0 and says zero commits were scanned."""
    root = generated_root(tmp_path)
    init_repo(root)
    commit_text(root, "README.md", "base\n", "base commit")
    head = rev_parse(root, "HEAD")

    exit_code = run_cli(monkeypatch, root, ["lint-secrets", "--range", f"{head}..{head}"])

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "commits scanned: 0" in out


def test_range_and_staged_flags_are_mutually_exclusive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC-FUNC-007: `--range` combined with `--staged` fails naming the conflict."""
    root = generated_root(tmp_path)
    init_repo(root)

    exit_code = run_cli(monkeypatch, root, ["lint-secrets", "--staged", "--range", "main..HEAD"])

    err = capsys.readouterr().err
    assert exit_code != 0
    assert "--range" in err
    assert "--staged" in err


def test_range_help_states_every_commit_scanned_and_why_tip_insufficient(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC-DOC-002: the `--range` help text states every commit is scanned, and why."""
    cli = import_cli()

    with pytest.raises(SystemExit) as exc_info:
        cli.main(["lint-secrets", "--help"])

    out = capsys.readouterr().out
    normalized = " ".join(out.lower().split())
    assert exc_info.value.code == 0
    assert "every commit" in normalized
    # A bare "tip" substring would still pass if the causal clause explaining
    # why the tip alone is insufficient were deleted; assert the fuller
    # phrase from _LINT_SECRETS_RANGE_HELP so that clause cannot be dropped
    # without this test noticing. argparse wraps its help text, so the
    # comparison text is whitespace-normalized first.
    assert "removed later, which still reaches the remote in history" in normalized


def test_hooks_install_writes_both_hooks_and_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC-FUNC-002: `hooks-install` writes both hooks under .git/hooks and exits 0."""
    root = generated_root(tmp_path)
    init_repo(root)

    exit_code = run_cli(monkeypatch, root, ["hooks-install"])

    out = capsys.readouterr().out
    assert exit_code == 0
    assert (root / ".git" / "hooks" / "pre-commit").is_file()
    assert (root / ".git" / "hooks" / "pre-push").is_file()
    assert "pre-commit" in out
    assert "pre-push" in out


def test_hooks_check_reports_match_after_install_and_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC-FUNC-003: `hooks-check` exits 0 and reports a match right after install."""
    root = generated_root(tmp_path)
    init_repo(root)
    run_cli(monkeypatch, root, ["hooks-install"])
    capsys.readouterr()

    exit_code = run_cli(monkeypatch, root, ["hooks-check"])

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "pre-commit: match" in out
    assert "pre-push: match" in out


def test_hooks_check_reports_drift_and_exits_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC-FUNC-003: `hooks-check` exits 1 and reports drift after a hook is edited."""
    root = generated_root(tmp_path)
    init_repo(root)
    run_cli(monkeypatch, root, ["hooks-install"])
    capsys.readouterr()
    hook_path = root / ".git" / "hooks" / "pre-commit"
    hook_path.write_text(hook_path.read_text(encoding="utf-8") + "# edited\n", encoding="utf-8")

    exit_code = run_cli(monkeypatch, root, ["hooks-check"])

    out = capsys.readouterr().out
    assert exit_code == 1
    assert "pre-commit: drift" in out
    assert "pre-push: match" in out


def test_hooks_pre_push_reports_finding_attributed_to_commit_and_exits_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC-FUNC-004 / AC-FUNC-005: `hooks-pre-push` scans a derived range and exits 1."""
    root = generated_root(tmp_path)
    init_repo(root)
    commit_text(root, "README.md", "base\n", "base commit")
    base_commit = rev_parse(root, "HEAD")
    line = credential_line()
    commit_text(root, "src/config.py", line, "add credential")
    tip_commit = rev_parse(root, "HEAD")
    stdin_text = f"refs/heads/feature {tip_commit} refs/heads/feature {base_commit}\n"

    exit_code = _run_cli_with_stdin(monkeypatch, root, ["hooks-pre-push"], stdin_text)

    out = capsys.readouterr().out
    assert exit_code == 1
    assert tip_commit in out
    assert "src/config.py:1" in out


def test_hooks_pre_push_exits_zero_when_pushed_range_is_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC-FUNC-005: `hooks-pre-push` exits 0 when the derived range has no findings."""
    root = generated_root(tmp_path)
    init_repo(root)
    commit_text(root, "README.md", "base\n", "base commit")
    head = rev_parse(root, "HEAD")
    stdin_text = f"refs/heads/main {head} refs/heads/main {head}\n"

    exit_code = _run_cli_with_stdin(monkeypatch, root, ["hooks-pre-push"], stdin_text)

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "commits scanned: 0" in out


def test_hooks_pre_push_scans_a_new_branch_the_remote_has_never_seen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC-FUNC-005: an all-zero remote id scans every commit reachable from the local tip."""
    root = generated_root(tmp_path)
    init_repo(root)
    line = credential_line()
    commit_text(root, "src/config.py", line, "root commit with credential")
    tip_commit = rev_parse(root, "HEAD")
    zero_sha = "0" * 40
    stdin_text = f"refs/heads/new-branch {tip_commit} refs/heads/new-branch {zero_sha}\n"

    exit_code = _run_cli_with_stdin(monkeypatch, root, ["hooks-pre-push"], stdin_text)

    out = capsys.readouterr().out
    assert exit_code == 1
    assert tip_commit in out
    assert "src/config.py:1" in out


# ---------------------------------------------------------------------------
# devsecret: get, list, set, rm (spec Section 4.3; E3-F2-S1-T1)
# ---------------------------------------------------------------------------


def _import_catalog() -> ModuleType:
    """Import devcontainer_config.catalog from inside a function body.

    Deferred for the same reason `import_cli` in `gitfixtures.py` is: the
    TDD RED gate stashes this unit's production-source files (only cli.py
    for this task; catalog.py already exists from E3-F1-S2-T1) and re-runs a
    single named test, and a module-level import here would fail COLLECTION
    for the whole file instead of failing one test for the real reason.
    """
    return importlib.import_module("devcontainer_config.catalog")


def _seeded_value() -> str:
    """A generated placeholder value, unique per call, never a real credential."""
    return f"seeded-value-{uuid.uuid4().hex}"


def _ok(payload: object) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=json.dumps(payload), stderr="")


def _err(stderr: str, returncode: int = 254) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout="", stderr=stderr)


class _FakeCatalogRunner:
    """A catalog.Runner double: records every call, answers from a queue, spawns nothing.

    A local duplicate of `tests/test_catalog.py`'s `_FakeRunner` -- see this
    module's docstring for why it is redefined here instead of imported.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], str | None]] = []
        self._queue: list[subprocess.CompletedProcess[str]] = []

    def queue(self, result: subprocess.CompletedProcess[str]) -> None:
        self._queue.append(result)

    def __call__(
        self, argv: list[str] | tuple[str, ...], stdin: str | None
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((tuple(argv), stdin))
        if not self._queue:
            raise AssertionError("_FakeCatalogRunner invoked with no queued response")
        return self._queue.pop(0)


class _TTYStringIO(io.StringIO):
    """A stdin stand-in that reports itself as a terminal, for the TTY-refusal tests."""

    def isatty(self) -> bool:
        return True


def _devsecret_client(runner: _FakeCatalogRunner) -> object:
    """A CatalogClient wrapping `runner`, with no real subprocess, network or AWS."""
    catalog = _import_catalog()
    return catalog.CatalogClient(runner)


def run_devsecret(
    monkeypatch: pytest.MonkeyPatch,
    args: list[str],
    *,
    client: object,
    stdin: io.StringIO | None = None,
) -> int:
    """Run `devcontainer_config.cli.main_devsecret(args, client=client)`; the exit code."""
    cli = import_cli()
    monkeypatch.setattr("sys.stdin", stdin if stdin is not None else io.StringIO(""))
    with pytest.raises(SystemExit) as exc_info:
        cli.main_devsecret(args, client=client)
    code = exc_info.value.code
    if not isinstance(code, int):
        raise AssertionError(f"cli.main_devsecret exited with a non-integer code: {code!r}")
    return code


INVALID_SECRET_NAMES = ["notion-token", "1TOKEN", "", "FOO/BAR"]
INVALID_SECRET_NAME_IDS = ["hyphen", "leading-digit", "empty", "path-separator"]


@pytest.mark.parametrize(
    "args",
    [["bogus-command"], ["get"], ["set", "NOTION_TOKEN", "extra", "extra2"]],
    ids=["unknown-command", "get-missing-name", "set-too-many-positionals"],
)
def test_devsecret_basic_usage_mistakes_exit_two(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], args: list[str]
) -> None:
    """Approach step 1: a basic argparse-level usage mistake exits 2 with a stderr message."""
    runner = _FakeCatalogRunner()
    client = _devsecret_client(runner)

    exit_code = run_devsecret(monkeypatch, args, client=client)

    err = capsys.readouterr().err
    assert exit_code == 2
    assert err.strip() != ""
    assert runner.calls == []


def test_devsecret_rm_missing_scope_exits_two_naming_scopes_in_effect(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC-FUNC-008: a missing --scope exits 2 and names the scopes currently in effect."""
    runner = _FakeCatalogRunner()
    client = _devsecret_client(runner)

    exit_code = run_devsecret(monkeypatch, ["rm", "NOTION_TOKEN"], client=client)

    err = capsys.readouterr().err
    assert exit_code == 2
    assert "shared" in err
    assert runner.calls == []


def test_devsecret_rm_unknown_scope_exits_two_and_deletes_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A scope outside catalog.scope_set(None) exits 2 on rm, the same rule list enforces.

    Without this check a mistyped --scope would delete-parameter against a
    tier get and list can never reach, which is silent until something
    downstream breaks -- exactly the failure this task's rationale for
    requiring --scope names as the reason to require it at all.
    """
    runner = _FakeCatalogRunner()
    client = _devsecret_client(runner)

    exit_code = run_devsecret(
        monkeypatch,
        ["rm", "NOTION_TOKEN", "--scope", "sandbox"],
        client=client,
        stdin=io.StringIO("y\n"),
    )

    err = capsys.readouterr().err
    assert exit_code == 2
    assert "shared" in err
    assert runner.calls == []


@pytest.mark.parametrize(
    "build_args",
    [
        lambda name: ["get", name],
        lambda name: ["set", name],
        lambda name: ["rm", name, "--scope", "shared"],
    ],
    ids=["get", "set", "rm"],
)
@pytest.mark.parametrize("name", INVALID_SECRET_NAMES, ids=INVALID_SECRET_NAME_IDS)
def test_devsecret_invalid_secret_name_exits_two_for_every_name_taking_command(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    build_args: Callable[[str], list[str]],
) -> None:
    """AC-FUNC-009: a name that is not a valid environment-variable identifier exits 2."""
    runner = _FakeCatalogRunner()
    client = _devsecret_client(runner)

    exit_code = run_devsecret(
        monkeypatch, build_args(name), client=client, stdin=io.StringIO(_seeded_value())
    )

    assert exit_code == 2
    assert runner.calls == []


def test_devsecret_get_resolvable_name_writes_exact_value_and_exits_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC-FUNC-001: get on a resolvable name writes exactly the value and nothing else."""
    runner = _FakeCatalogRunner()
    value = _seeded_value()
    runner.queue(_ok({"Parameter": {"Value": value}}))
    client = _devsecret_client(runner)

    exit_code = run_devsecret(monkeypatch, ["get", "NOTION_TOKEN"], client=client)

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out == value
    assert captured.err == ""


def test_devsecret_get_missing_from_every_scope_exits_four_naming_scopes_and_list_command(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC-FUNC-002: absent from every searched scope exits 4, naming scopes and `devsecret list`."""
    runner = _FakeCatalogRunner()
    runner.queue(_err("ParameterNotFound"))
    client = _devsecret_client(runner)

    exit_code = run_devsecret(monkeypatch, ["get", "MISSING_TOKEN"], client=client)

    err = capsys.readouterr().err
    assert exit_code == 4
    assert "shared" in err
    assert "devsecret list" in err


def test_devsecret_list_never_prints_a_value(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC-4.3 / AC-TEST-001: a seeded value never appears in list's combined output."""
    runner = _FakeCatalogRunner()
    value = _seeded_value()
    runner.queue(
        _ok(
            {
                "Parameters": [
                    {
                        "Name": "/devcontainer/shared/secrets/NOTION_TOKEN",
                        "LastModifiedDate": 1700000000.0,
                        "Description": json.dumps({"exported": True}),
                        # Real describe-parameters responses never carry a
                        # Value field (that is the entire premise of
                        # AC-4.3), but queuing one here means this test is
                        # not merely asserting an absence nothing produced
                        # in the first place: a handler that started
                        # echoing a response entry verbatim, or a listing
                        # path switched to a decrypting operation whose
                        # response shape carries this field, would be
                        # caught by the assertions below rather than
                        # passing vacuously.
                        "Value": value,
                    }
                ]
            }
        )
    )
    client = _devsecret_client(runner)

    exit_code = run_devsecret(monkeypatch, ["list"], client=client)

    captured = capsys.readouterr()
    assert exit_code == 0
    assert value not in captured.out
    assert value not in captured.err
    for call_argv, call_stdin in runner.calls:
        assert "--with-decryption" not in call_argv
        assert value not in " ".join(call_argv)
        assert call_stdin is None or value not in call_stdin


def test_devsecret_list_renders_four_columns(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC-FUNC-003: name, scope, last-changed and the exported flag are all rendered."""
    runner = _FakeCatalogRunner()
    runner.queue(
        _ok(
            {
                "Parameters": [
                    {
                        "Name": "/devcontainer/shared/secrets/NOTION_TOKEN",
                        "LastModifiedDate": 1700000000.0,
                        "Description": json.dumps({"exported": True}),
                    }
                ]
            }
        )
    )
    client = _devsecret_client(runner)

    exit_code = run_devsecret(monkeypatch, ["list"], client=client)

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "NOTION_TOKEN" in out
    assert "shared" in out
    assert "1700000000.0" in out
    assert "yes" in out


def test_devsecret_list_scope_narrows_the_listing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC-FUNC-004: --scope shared queries only the shared prefix."""
    runner = _FakeCatalogRunner()
    runner.queue(_ok({"Parameters": []}))
    client = _devsecret_client(runner)

    exit_code = run_devsecret(monkeypatch, ["list", "--scope", "shared"], client=client)

    assert exit_code == 0
    (argv, _stdin) = runner.calls[0]
    assert any("/devcontainer/shared/secrets" in part for part in argv)


def test_devsecret_list_unknown_scope_exits_two_naming_scopes_in_effect(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC-FUNC-004: an unrecognized --scope exits 2 and lists the scopes in effect."""
    runner = _FakeCatalogRunner()
    client = _devsecret_client(runner)

    exit_code = run_devsecret(monkeypatch, ["list", "--scope", "sandbox"], client=client)

    err = capsys.readouterr().err
    assert exit_code == 2
    assert "shared" in err
    assert runner.calls == []


def test_devsecret_set_value_as_positional_argument_exits_five_and_echoes_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC-FUNC-005 / AC-TEST-003: a value passed as an argument is refused and never echoed."""
    runner = _FakeCatalogRunner()
    client = _devsecret_client(runner)
    value = _seeded_value()

    exit_code = run_devsecret(monkeypatch, ["set", "NOTION_TOKEN", value], client=client)

    captured = capsys.readouterr()
    assert exit_code == 5
    assert value not in captured.out
    assert value not in captured.err
    assert runner.calls == []


def test_devsecret_set_unknown_scope_exits_two_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A scope outside catalog.scope_set(None) exits 2 on set, before stdin is ever read.

    catalog.parameter_path validates only a scope's character shape, not
    its membership in the resolution set; without cli.py's own membership
    check a mistyped --scope would silently write a secret into a tier get
    and list can never reach.
    """
    runner = _FakeCatalogRunner()
    client = _devsecret_client(runner)
    value = _seeded_value()

    exit_code = run_devsecret(
        monkeypatch,
        ["set", "NOTION_TOKEN", "--scope", "sandbox"],
        client=client,
        stdin=io.StringIO(value),
    )

    err = capsys.readouterr().err
    assert exit_code == 2
    assert "shared" in err
    assert runner.calls == []


def test_devsecret_set_tty_without_stdin_flag_exits_two(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC-FUNC-006 / AC-TEST-004: stdin attached to a TTY without --stdin exits 2."""
    runner = _FakeCatalogRunner()
    client = _devsecret_client(runner)

    exit_code = run_devsecret(
        monkeypatch, ["set", "NOTION_TOKEN"], client=client, stdin=_TTYStringIO(_seeded_value())
    )

    err = capsys.readouterr().err
    assert exit_code == 2
    assert "--stdin" in err
    assert runner.calls == []


def test_devsecret_set_stdin_flag_reads_the_interactive_value(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC-FUNC-006 / AC-TEST-004: --stdin accepts a value even when stdin is a TTY."""
    runner = _FakeCatalogRunner()
    runner.queue(_ok({"Version": 1}))
    client = _devsecret_client(runner)
    value = _seeded_value()

    exit_code = run_devsecret(
        monkeypatch,
        ["set", "NOTION_TOKEN", "--stdin"],
        client=client,
        stdin=_TTYStringIO(value),
    )

    assert exit_code == 0
    (_argv, stdin_document) = runner.calls[0]
    assert stdin_document is not None
    assert value in stdin_document


def test_devsecret_set_exported_records_marker_and_success_line_names_path_and_version(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC-FUNC-007: --exported records the marker; the success line names path and version."""
    runner = _FakeCatalogRunner()
    runner.queue(_ok({"Version": 3}))
    client = _devsecret_client(runner)
    value = _seeded_value()

    exit_code = run_devsecret(
        monkeypatch,
        ["set", "NOTION_TOKEN", "--scope", "shared", "--exported"],
        client=client,
        stdin=io.StringIO(value),
    )

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "/devcontainer/shared/secrets/NOTION_TOKEN" in out
    assert "version 3" in out
    assert "Exported" in out
    (argv, stdin_document) = runner.calls[0]
    assert value not in " ".join(argv)
    assert stdin_document is not None
    document = json.loads(stdin_document)
    assert document["Description"] == json.dumps({"exported": True})
    assert document["Value"] == value


def test_devsecret_set_without_exported_success_line_names_get_command(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC-FUNC-007: without --exported the success line points at `devsecret get`."""
    runner = _FakeCatalogRunner()
    runner.queue(_ok({"Version": 1}))
    client = _devsecret_client(runner)
    value = _seeded_value()

    exit_code = run_devsecret(
        monkeypatch, ["set", "NOTION_TOKEN"], client=client, stdin=io.StringIO(value)
    )

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "Not exported" in out
    assert "devsecret get NOTION_TOKEN" in out


def test_devsecret_rm_declined_confirmation_deletes_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC-FUNC-008: a declined confirmation deletes nothing and reports that it deleted nothing."""
    runner = _FakeCatalogRunner()
    client = _devsecret_client(runner)

    exit_code = run_devsecret(
        monkeypatch,
        ["rm", "NOTION_TOKEN", "--scope", "shared"],
        client=client,
        stdin=io.StringIO("n\n"),
    )

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "Not deleted" in out
    assert runner.calls == []


def test_devsecret_rm_accepted_confirmation_deletes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC-FUNC-008: an accepted confirmation issues the delete."""
    runner = _FakeCatalogRunner()
    runner.queue(_ok({}))
    client = _devsecret_client(runner)

    exit_code = run_devsecret(
        monkeypatch,
        ["rm", "NOTION_TOKEN", "--scope", "shared"],
        client=client,
        stdin=io.StringIO("y\n"),
    )

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "Deleted" in out
    (argv, _stdin) = runner.calls[0]
    assert "delete-parameter" in argv


def test_devsecret_rm_absent_name_exits_four(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC-FUNC-008: deleting an absent name exits 4."""
    runner = _FakeCatalogRunner()
    runner.queue(_err("ParameterNotFound"))
    client = _devsecret_client(runner)

    exit_code = run_devsecret(
        monkeypatch,
        ["rm", "NOTION_TOKEN", "--scope", "shared"],
        client=client,
        stdin=io.StringIO("y\n"),
    )

    assert exit_code == 4


@pytest.mark.parametrize(
    ("stderr_text", "expected_needle"),
    [
        ("Unable to locate credentials", "aws sso login"),
        ("Error loading SSO Token: session expired", "aws sso login"),
    ],
    ids=["no-credentials", "sso-expired"],
)
def test_devsecret_backend_unavailable_exits_three_naming_aws_sso_login(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    stderr_text: str,
    expected_needle: str,
) -> None:
    """AC-FUNC-010: no credential resolved exits 3 naming aws sso login."""
    runner = _FakeCatalogRunner()
    runner.queue(_err(stderr_text))
    client = _devsecret_client(runner)

    exit_code = run_devsecret(monkeypatch, ["get", "NOTION_TOKEN"], client=client)

    err = capsys.readouterr().err
    assert exit_code == 3
    assert expected_needle in err


def test_devsecret_backend_unauthorized_exits_three(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC-FUNC-010: an access-denied response exits 3."""
    runner = _FakeCatalogRunner()
    runner.queue(_err("AccessDeniedException"))
    client = _devsecret_client(runner)

    exit_code = run_devsecret(monkeypatch, ["get", "NOTION_TOKEN"], client=client)

    err = capsys.readouterr().err
    assert exit_code == 3
    assert "access denied" in err.lower()


def test_devsecret_list_malformed_response_exits_three_via_generic_catalog_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC-FUNC-011: a bare `catalog.CatalogError` (no named subclass) also exits 3.

    `catalog._record_from_entry` raises `CatalogError` directly, not one of
    the seven named subclasses `_DEVSECRET_EXIT_CODES` lists explicitly, for
    a listing entry missing a required field. This exercises
    `_devsecret_exit_code_for`'s trailing fallback row (the only row that
    can ever match a `CatalogError` none of the earlier rows recognizes),
    the branch every other exit-3 test in this module leaves uncovered
    because they all raise `CatalogUnavailableError` or
    `CatalogUnauthorizedError` instead.
    """
    runner = _FakeCatalogRunner()
    runner.queue(
        _ok(
            {
                "Parameters": [
                    {"Name": "/devcontainer/shared/secrets/NOTION_TOKEN"},
                ]
            }
        )
    )
    client = _devsecret_client(runner)

    exit_code = run_devsecret(monkeypatch, ["list"], client=client)

    err = capsys.readouterr().err
    assert exit_code == 3
    assert "LastModifiedDate" in err


def test_devsecret_help_renders_four_commands_scopes_and_exit_codes(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC-TEST-005: --help lists get/list/set/rm and renders the scopes and exit-code blocks."""
    cli = import_cli()

    with pytest.raises(SystemExit) as exc_info:
        cli.main_devsecret(["--help"])

    out = capsys.readouterr().out
    normalized = " ".join(out.lower().split())
    assert exc_info.value.code == 0
    assert "print one value on stdout. nothing else" in normalized
    assert "never prints a value" in normalized
    assert "read the value from stdin. never from an argument" in normalized
    assert "delete after confirmation" in normalized
    assert "scopes:" in normalized
    assert "every engine and instance" in normalized
    assert "one environment. resolved before shared" in normalized
    assert "exit codes:" in normalized
    assert "0 success" in normalized
    assert "2 usage" in normalized
    assert "3 backend unreachable or unauthorized" in normalized
    assert "4 not found" in normalized
    assert "5 refused because a value would have been exposed" in normalized


def test_devsecret_set_help_documents_the_no_value_as_argument_rule_against_value(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC-FUNC-005/AC-4.3: `set --help` attaches the no-value-as-argument rule

    to the `VALUE` trap positional, not to `NAME`. The rendered usage line
    must show a visible `VALUE` token (not an empty metavar collapsing into
    `[]`), and the warning text must be argparse's help for the `VALUE`
    entry, immediately following it, rather than for `NAME`.
    """
    cli = import_cli()

    with pytest.raises(SystemExit) as exc_info:
        cli.main_devsecret(["set", "--help"])

    out = capsys.readouterr().out
    assert exc_info.value.code == 0
    assert "[VALUE]" in out

    normalized = " ".join(out.split())
    assert "NAME The secret name to write." in normalized
    warning = "Never supply the value here; pipe it on stdin instead (refused with exit 5)."
    assert f"VALUE {warning}" in normalized
    assert f"NAME {warning}" not in normalized


def test_devsecret_exit_codes_are_named_constants_matching_section_4_3() -> None:
    """AC-FUNC-011: the five exit codes are declared once as named constants."""
    cli = import_cli()

    assert cli.EXIT_SUCCESS == 0
    assert cli.EXIT_USAGE_ERROR == 2
    assert cli.EXIT_BACKEND_ERROR == 3
    assert cli.EXIT_NOT_FOUND == 4
    assert cli.EXIT_VALUE_EXPOSURE_REFUSED == 5


def test_build_production_catalog_client_wires_the_real_subprocess_runner() -> None:
    """The one line of production wiring `main_devsecret` uses outside a test.

    `_build_production_catalog_client` is never given a fake runner by any
    other test in this module (every other test injects `client`
    explicitly), so nothing else exercises it. Constructing the client here
    touches no network, AWS or docker -- `catalog.CatalogClient.__init__`
    only stores its arguments -- so this stays hermetic while still proving
    the real subprocess runner is what production wiring uses.
    """
    cli = import_cli()
    catalog = _import_catalog()

    client = cli._build_production_catalog_client()

    assert isinstance(client, catalog.CatalogClient)
    assert client._runner is catalog.subprocess_runner


def test_main_devsecret_constructs_production_client_when_none_supplied(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`main_devsecret` builds a production client exactly when the test seam is unused.

    A caller that supplies `client` (every other test in this module)
    never reaches `_build_production_catalog_client`; the production
    console script never supplies `client`, so this is the only path that
    does. Stubbing `_build_production_catalog_client` to return an injected
    fake keeps the assertion hermetic while still proving `main_devsecret`
    calls it when `client=None`.
    """
    cli = import_cli()
    runner = _FakeCatalogRunner()
    runner.queue(_ok({"Parameters": []}))
    stub_client = _devsecret_client(runner)
    calls: list[None] = []

    def _stub_build_production_catalog_client() -> object:
        calls.append(None)
        return stub_client

    monkeypatch.setattr(
        cli, "_build_production_catalog_client", _stub_build_production_catalog_client
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(""))

    with pytest.raises(SystemExit) as exc_info:
        cli.main_devsecret(["list"])

    assert exc_info.value.code == cli.EXIT_SUCCESS
    assert len(calls) == 1


def test_pyproject_declares_devsecret_console_script() -> None:
    """AC-FUNC-012: pyproject.toml declares the devsecret console script."""
    root = Path(__file__).resolve().parents[1]

    data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))

    assert data["project"]["scripts"]["devsecret"] == "devcontainer_config.cli:main_devsecret"


def test_pyproject_package_discovery_resolves_to_the_real_package_directory() -> None:
    """AC-FUNC-012: the declared build backend and package-discovery setting

    resolve to a real, importable `devcontainer_config` package, not merely
    a matching string. A console script with no build backend that can find
    its module is a declaration nothing installs; this test asserts the
    discovery root named in `[tool.hatch.build.targets.wheel] packages`
    both exists on disk and is the same directory `main_devsecret` actually
    imports from at runtime.
    """
    root = Path(__file__).resolve().parents[1]

    data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))

    assert data["build-system"]["build-backend"] == "hatchling.build"
    assert "hatchling" in data["build-system"]["requires"][0]

    declared_packages = data["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]
    assert len(declared_packages) == 1
    discovery_root = (root / declared_packages[0]).resolve()

    assert discovery_root.is_dir()
    assert discovery_root.name == "devcontainer_config"
    assert (discovery_root / "__init__.py").is_file()
    assert (discovery_root / "cli.py").is_file()

    cli = import_cli()
    runtime_package_dir = Path(cli.__file__).resolve().parent
    assert discovery_root == runtime_package_dir

    # package = false keeps `uv run`/`uv sync` from ever invoking the
    # declared build backend, so hermeticity (AC-10.14) holds even though
    # the backend and discovery setting are now both declared.
    assert data["tool"]["uv"]["package"] is False


def test_devsecret_end_to_end_set_list_get_rm_then_not_found(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC-CYCLE-001: set, list, get, rm, then get again -- end to end with the injected catalog."""
    runner = _FakeCatalogRunner()
    client = _devsecret_client(runner)
    value = _seeded_value()

    runner.queue(_ok({"Version": 1}))
    exit_code = run_devsecret(
        monkeypatch,
        ["set", "NOTION_TOKEN", "--scope", "shared", "--exported"],
        client=client,
        stdin=io.StringIO(value),
    )
    set_out = capsys.readouterr().out
    assert exit_code == 0
    assert "/devcontainer/shared/secrets/NOTION_TOKEN" in set_out

    runner.queue(
        _ok(
            {
                "Parameters": [
                    {
                        "Name": "/devcontainer/shared/secrets/NOTION_TOKEN",
                        "LastModifiedDate": 1700000000.0,
                        "Description": json.dumps({"exported": True}),
                    }
                ]
            }
        )
    )
    exit_code = run_devsecret(monkeypatch, ["list"], client=client)
    list_out = capsys.readouterr().out
    assert exit_code == 0
    assert "NOTION_TOKEN" in list_out
    assert "shared" in list_out
    assert "yes" in list_out
    assert value not in list_out

    runner.queue(_ok({"Parameter": {"Value": value}}))
    exit_code = run_devsecret(monkeypatch, ["get", "NOTION_TOKEN"], client=client)
    get_out = capsys.readouterr().out
    assert exit_code == 0
    assert get_out == value

    runner.queue(_ok({}))
    exit_code = run_devsecret(
        monkeypatch,
        ["rm", "NOTION_TOKEN", "--scope", "shared"],
        client=client,
        stdin=io.StringIO("y\n"),
    )
    rm_out = capsys.readouterr().out
    assert exit_code == 0
    assert "Deleted" in rm_out

    runner.queue(_err("ParameterNotFound"))
    exit_code = run_devsecret(monkeypatch, ["get", "NOTION_TOKEN"], client=client)
    assert exit_code == 4


def _get_success(runner: _FakeCatalogRunner) -> None:
    runner.queue(_ok({"Parameter": {"Value": _seeded_value()}}))


def _get_not_found(runner: _FakeCatalogRunner) -> None:
    runner.queue(_err("ParameterNotFound"))


def _get_unauthorized(runner: _FakeCatalogRunner) -> None:
    runner.queue(_err("AccessDeniedException"))


def _backend_unavailable(runner: _FakeCatalogRunner) -> None:
    runner.queue(_err("Unable to locate credentials"))


def _set_success(runner: _FakeCatalogRunner) -> None:
    runner.queue(_ok({"Version": 1}))


def _list_success(runner: _FakeCatalogRunner) -> None:
    runner.queue(_ok({"Parameters": []}))


def _rm_success(runner: _FakeCatalogRunner) -> None:
    runner.queue(_ok({}))


def _noop(_runner: _FakeCatalogRunner) -> None:
    return None


# name, args, stdin_text, queue setup, expected exit code -- one row per
# reachable branch of the exit-code table (AC-TEST-002), spread across every
# command that can reach it.
_DEVSECRET_EXIT_CODE_MATRIX: tuple[
    tuple[str, list[str], str, Callable[[_FakeCatalogRunner], None], int], ...
] = (
    ("get-success", ["get", "NOTION_TOKEN"], "", _get_success, 0),
    ("get-not-found", ["get", "MISSING_TOKEN"], "", _get_not_found, 4),
    ("get-unauthorized", ["get", "NOTION_TOKEN"], "", _get_unauthorized, 3),
    ("get-invalid-name", ["get", "not-valid"], "", _noop, 2),
    ("list-success", ["list"], "", _list_success, 0),
    ("list-unknown-scope", ["list", "--scope", "sandbox"], "", _noop, 2),
    ("list-unavailable", ["list"], "", _backend_unavailable, 3),
    ("set-success", ["set", "NOTION_TOKEN"], "seeded-value", _set_success, 0),
    ("set-value-as-argument", ["set", "NOTION_TOKEN", "leak"], "", _noop, 5),
    ("set-unavailable", ["set", "NOTION_TOKEN"], "seeded-value", _backend_unavailable, 3),
    ("rm-missing-scope", ["rm", "NOTION_TOKEN"], "", _noop, 2),
    ("rm-accept", ["rm", "NOTION_TOKEN", "--scope", "shared"], "y\n", _rm_success, 0),
    ("rm-not-found", ["rm", "NOTION_TOKEN", "--scope", "shared"], "y\n", _get_not_found, 4),
    ("rm-unavailable", ["rm", "NOTION_TOKEN", "--scope", "shared"], "y\n", _backend_unavailable, 3),
)


@pytest.mark.parametrize(
    ("args", "stdin_text", "queue", "expected_exit"),
    [entry[1:] for entry in _DEVSECRET_EXIT_CODE_MATRIX],
    ids=[entry[0] for entry in _DEVSECRET_EXIT_CODE_MATRIX],
)
def test_devsecret_exit_code_matrix(
    monkeypatch: pytest.MonkeyPatch,
    args: list[str],
    stdin_text: str,
    queue: Callable[[_FakeCatalogRunner], None],
    expected_exit: int,
) -> None:
    """AC-TEST-002: one parametrized case per exit code, spread across every command."""
    runner = _FakeCatalogRunner()
    queue(runner)
    client = _devsecret_client(runner)

    exit_code = run_devsecret(monkeypatch, args, client=client, stdin=io.StringIO(stdin_text))

    assert exit_code == expected_exit
