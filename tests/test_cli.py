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

import ast
import importlib
import io
import json
import os
import shlex
import signal
import stat
import subprocess
import sys
import tomllib
import uuid
from collections.abc import Callable, Sequence
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
# resolve-instance (spec Section 4.1.1, 9; E8-F1-S1-T1)
#
# The business-logic scenarios (all four resolution steps, all three edge
# cases, and the address-block content) are covered in
# tests/test_instances.py per this task's own Approach; the tests below
# cover only the argparse-level wiring `test_instances.py` does not:
# `--help` text, the `--local-backend-active` flag's presence, and that an
# invalid flag is rejected the same way every other subcommand's parser
# rejects one, matching the convention this file's other subcommand
# sections (`hooks-install`, `hooks-check`) already establish above.
# ---------------------------------------------------------------------------


def test_resolve_instance_help_documents_local_backend_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cli = import_cli()
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SystemExit) as exc_info:
        cli.main(["resolve-instance", "--help"])

    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    assert "--local-backend-active" in out
    assert "spec Section 4.1.1" in out


def test_resolve_instance_rejects_an_unknown_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cli = import_cli()
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SystemExit) as exc_info:
        cli.main(["resolve-instance", "--no-such-flag"])

    assert exc_info.value.code == 2
    assert "resolve-instance" in capsys.readouterr().err


def test_resolve_instance_fails_fast_outside_a_git_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`resolve-instance` reuses `repo.find_root` (AC-FUNC-006's shared RepoError path)."""
    outside = generated_root(tmp_path)

    exit_code = run_cli(monkeypatch, outside, ["resolve-instance"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert str(outside) in captured.err


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
        self.documents: list[str] = []
        self._queue: list[subprocess.CompletedProcess[str]] = []

    def queue(self, result: subprocess.CompletedProcess[str]) -> None:
        self._queue.append(result)

    def __call__(
        self, argv: list[str] | tuple[str, ...], stdin: str | None
    ) -> subprocess.CompletedProcess[str]:
        argv = tuple(argv)
        self.calls.append((argv, stdin))
        if "--cli-input-json" in argv:
            # Read it now: write_parameter removes the document's directory
            # before returning, so it cannot be inspected afterwards.
            reference = argv[argv.index("--cli-input-json") + 1]
            assert reference.startswith("file://"), reference
            self.documents.append(Path(reference[len("file://") :]).read_text(encoding="utf-8"))
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
    (_argv, stdin) = runner.calls[0]
    assert stdin is None, "the aws CLI v2 cannot read the document from stdin"
    assert value in runner.documents[0]


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
    (argv, _stdin) = runner.calls[0]
    assert value not in " ".join(argv)
    document = json.loads(runner.documents[0])
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
    the nine named subclasses `_DEVSECRET_EXIT_CODES` lists explicitly, for
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


def test_devsecret_help_renders_six_commands_scopes_and_exit_codes(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC-FUNC-012 / AC-TEST-005: --help lists all six commands and the scopes/exit-code blocks."""
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
    assert "run <cmd> with only those secrets in its environment" in normalized
    assert "names marked exported, for shell startup" in normalized
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


# ---------------------------------------------------------------------------
# devsecret: run, export-list (spec Section 4.3; E3-F2-S1-T2)
# ---------------------------------------------------------------------------

# A small Python program, run as the child of `devsecret run` in every test
# below, that reports what it actually saw: its own environment, its own
# argv, and whether SECRET_CACHE_DIR named a directory that existed while it
# ran (it also writes a marker file into that directory, so the cleanup
# assertion after `run` returns covers "a file was written into it" per the
# Approach). Written to a report file named by its first argument rather than
# printed to stdout, so it never collides with -- or gets lost in -- whatever
# capsys captures from devsecret's own output.
_ENV_REPORTER_SCRIPT = (
    "import json, os, sys\n"
    "report_path = sys.argv[1]\n"
    "cache_dir = os.environ.get('SECRET_CACHE_DIR')\n"
    "existed_during_run = bool(cache_dir) and os.path.isdir(cache_dir)\n"
    "if cache_dir:\n"
    "    with open(os.path.join(cache_dir, 'token.txt'), 'w', encoding='utf-8') as marker:\n"
    "        marker.write('written-by-child')\n"
    "payload = {\n"
    "    'env': dict(os.environ),\n"
    "    'argv': sys.argv,\n"
    "    'cache_dir': cache_dir,\n"
    "    'existed_during_run': existed_during_run,\n"
    "}\n"
    "with open(report_path, 'w', encoding='utf-8') as fh:\n"
    "    json.dump(payload, fh)\n"
)


def _env_reporter_command(report_path: Path) -> list[str]:
    return [sys.executable, "-c", _ENV_REPORTER_SCRIPT, str(report_path)]


def _read_report(report_path: Path) -> dict[str, object]:
    data = json.loads(report_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise AssertionError(f"reporter wrote a non-object payload: {data!r}")
    return data


def run_devsecret_in_repo(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
    args: list[str],
    *,
    client: object,
    stdin: io.StringIO | None = None,
) -> int:
    """Like `run_devsecret`, but chdir's into `root` first.

    `run` resolves the repository root via `repo.find_root(Path.cwd())` (the
    same primitive `hooks-install` and `lint-secrets` already use), so its
    tests need a real, disposable git repository as cwd rather than whatever
    directory pytest happened to start in.
    """
    monkeypatch.chdir(root)
    return run_devsecret(monkeypatch, args, client=client, stdin=stdin)


@pytest.fixture
def devsecret_run_repo(tmp_path: Path) -> Path:
    """A real, disposable git repository `run`'s tests chdir into."""
    root = generated_root(tmp_path)
    init_repo(root)
    return root


@pytest.fixture
def secret_cache_base(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A dedicated SECRET_CACHE_DIR base, so cleanup can be asserted by checking it is empty.

    The mount table is faked alongside it. `catalog.secret_cache_dir` refuses a
    cache directory it cannot prove is RAM-backed, and it proves that from
    `/proc/mounts`: on Linux this base is an ordinary on-disk temporary
    directory, so the real reader classifies it correctly and every `devsecret
    run` test fails with exit 5. On macOS there is no `/proc/mounts`, the
    reader returns None, the check is skipped, and the same tests pass. That
    difference is why these twelve tests passed on the developer's machine and
    failed in CI.

    Declaring the base tmpfs here makes the platform irrelevant: the tests
    exercise `run` rather than the host's filesystem layout. The mount table is
    supplied by pointing `catalog._PROC_MOUNTS_PATH` at a fixture file, the
    same technique
    `test_devsecret_run_secret_cache_dir_not_ram_backed_exits_five` already
    uses to reach the opposite outcome, so the real reader still runs and only
    the table it reads is substituted. `tests/test_catalog.py` is where the
    refusal itself is tested, with its own tables built for the purpose.
    """
    base = tmp_path / f"secret-cache-base-{uuid.uuid4().hex}"
    base.mkdir()
    monkeypatch.setenv("SECRET_CACHE_DIR", str(base))
    catalog = importlib.import_module("devcontainer_config.catalog")
    mounts = tmp_path / f"mounts-{uuid.uuid4().hex}"
    mounts.write_text(f"tmpfs {base} tmpfs rw,relatime 0 0\n", encoding="utf-8")
    monkeypatch.setattr(catalog, "_PROC_MOUNTS_PATH", mounts)
    return base


def test_devsecret_run_child_receives_only_named_secrets(
    monkeypatch: pytest.MonkeyPatch,
    devsecret_run_repo: Path,
    secret_cache_base: Path,
    tmp_path: Path,
) -> None:
    """AC-FUNC-001 / AC-TEST-003: the child sees the named secret and not an unnamed one."""
    monkeypatch.delenv("NOTION_TOKEN", raising=False)
    monkeypatch.delenv("JENKINS_API_TOKEN", raising=False)
    runner = _FakeCatalogRunner()
    value = _seeded_value()
    runner.queue(_ok({"Parameter": {"Value": value}}))
    client = _devsecret_client(runner)
    report_path = tmp_path / "report.json"

    exit_code = run_devsecret_in_repo(
        monkeypatch,
        devsecret_run_repo,
        ["run", "--secrets", "NOTION_TOKEN", "--", *_env_reporter_command(report_path)],
        client=client,
    )

    assert exit_code == 0
    report = _read_report(report_path)
    assert report["env"]["NOTION_TOKEN"] == value
    assert "JENKINS_API_TOKEN" not in report["env"]
    assert list(secret_cache_base.iterdir()) == []


def test_devsecret_run_environment_is_otherwise_the_parents(
    monkeypatch: pytest.MonkeyPatch,
    devsecret_run_repo: Path,
    secret_cache_base: Path,
    tmp_path: Path,
) -> None:
    """AC-FUNC-001: an unrelated parent env var reaches the child unchanged."""
    marker_name = f"DEVSECRET_TEST_MARKER_{uuid.uuid4().hex}"
    marker_value = _seeded_value()
    monkeypatch.setenv(marker_name, marker_value)
    runner = _FakeCatalogRunner()
    client = _devsecret_client(runner)
    report_path = tmp_path / "report.json"

    exit_code = run_devsecret_in_repo(
        monkeypatch,
        devsecret_run_repo,
        ["run", "--secrets", "", "--", *_env_reporter_command(report_path)],
        client=client,
    )

    assert exit_code == 0
    report = _read_report(report_path)
    assert report["env"][marker_name] == marker_value


def test_devsecret_run_empty_secrets_list_runs_with_no_secrets_and_fetches_nothing(
    monkeypatch: pytest.MonkeyPatch,
    devsecret_run_repo: Path,
    secret_cache_base: Path,
    tmp_path: Path,
) -> None:
    """AC-FUNC-002: an empty --secrets list runs the command with no secrets, not all of them."""
    runner = _FakeCatalogRunner()
    client = _devsecret_client(runner)
    report_path = tmp_path / "report.json"

    exit_code = run_devsecret_in_repo(
        monkeypatch,
        devsecret_run_repo,
        ["run", "--secrets", "", "--", *_env_reporter_command(report_path)],
        client=client,
    )

    assert exit_code == 0
    assert runner.calls == []
    assert _read_report(report_path)["cache_dir"] is not None


def test_devsecret_run_name_absent_from_every_scope_exits_four_before_child_created(
    monkeypatch: pytest.MonkeyPatch,
    devsecret_run_repo: Path,
    secret_cache_base: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC-FUNC-003 / AC-TEST-004: a not-found name exits 4 and no child is ever created."""
    runner = _FakeCatalogRunner()
    runner.queue(_err("ParameterNotFound"))
    client = _devsecret_client(runner)
    report_path = tmp_path / "report.json"

    exit_code = run_devsecret_in_repo(
        monkeypatch,
        devsecret_run_repo,
        ["run", "--secrets", "MISSING_TOKEN", "--", *_env_reporter_command(report_path)],
        client=client,
    )

    err = capsys.readouterr().err
    assert exit_code == 4
    assert "shared" in err
    assert not report_path.exists()
    assert list(secret_cache_base.iterdir()) == []


def test_devsecret_run_value_never_appears_in_child_argv(
    monkeypatch: pytest.MonkeyPatch,
    devsecret_run_repo: Path,
    secret_cache_base: Path,
    tmp_path: Path,
) -> None:
    """AC-FUNC-005: the resolved value reaches the child only through its environment."""
    runner = _FakeCatalogRunner()
    value = _seeded_value()
    runner.queue(_ok({"Parameter": {"Value": value}}))
    client = _devsecret_client(runner)
    report_path = tmp_path / "report.json"

    exit_code = run_devsecret_in_repo(
        monkeypatch,
        devsecret_run_repo,
        ["run", "--secrets", "NOTION_TOKEN", "--", *_env_reporter_command(report_path)],
        client=client,
    )

    assert exit_code == 0
    report = _read_report(report_path)
    assert value not in json.dumps(report["argv"])


def test_devsecret_run_propagates_a_non_zero_child_exit_status_unchanged(
    monkeypatch: pytest.MonkeyPatch,
    devsecret_run_repo: Path,
    secret_cache_base: Path,
) -> None:
    """AC-FUNC-004: a non-zero child exit status is propagated unchanged."""
    runner = _FakeCatalogRunner()
    client = _devsecret_client(runner)
    child_exit_status = 7
    exit_script = f"import sys; sys.exit({child_exit_status})"

    exit_code = run_devsecret_in_repo(
        monkeypatch,
        devsecret_run_repo,
        ["run", "--secrets", "", "--", sys.executable, "-c", exit_script],
        client=client,
    )

    assert exit_code == child_exit_status
    assert list(secret_cache_base.iterdir()) == []


def test_devsecret_run_child_terminated_by_signal_reports_128_plus_signal_number(
    monkeypatch: pytest.MonkeyPatch,
    devsecret_run_repo: Path,
    secret_cache_base: Path,
) -> None:
    """AC-FUNC-004 / AC-TEST-004: a signal-terminated child reports 128 plus the signal number."""
    runner = _FakeCatalogRunner()
    client = _devsecret_client(runner)
    kill_script = f"import os, signal; os.kill(os.getpid(), signal.{signal.SIGTERM.name})"

    exit_code = run_devsecret_in_repo(
        monkeypatch,
        devsecret_run_repo,
        ["run", "--secrets", "", "--", sys.executable, "-c", kill_script],
        client=client,
    )

    assert exit_code == 128 + signal.SIGTERM
    assert list(secret_cache_base.iterdir()) == []


def test_devsecret_run_cache_directory_exists_during_the_child_and_is_gone_after(
    monkeypatch: pytest.MonkeyPatch,
    devsecret_run_repo: Path,
    secret_cache_base: Path,
    tmp_path: Path,
) -> None:
    """AC-FUNC-006: created before the child runs (a file can be written into it), gone after."""
    runner = _FakeCatalogRunner()
    client = _devsecret_client(runner)
    report_path = tmp_path / "report.json"

    exit_code = run_devsecret_in_repo(
        monkeypatch,
        devsecret_run_repo,
        ["run", "--secrets", "", "--", *_env_reporter_command(report_path)],
        client=client,
    )

    assert exit_code == 0
    report = _read_report(report_path)
    assert report["existed_during_run"] is True
    cache_dir = report["cache_dir"]
    assert isinstance(cache_dir, str)
    assert not Path(cache_dir).exists()
    assert list(secret_cache_base.iterdir()) == []


def test_devsecret_run_missing_command_exits_two(
    monkeypatch: pytest.MonkeyPatch,
    devsecret_run_repo: Path,
    secret_cache_base: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No command after '--' is a usage error, not a silent no-op."""
    runner = _FakeCatalogRunner()
    client = _devsecret_client(runner)

    exit_code = run_devsecret_in_repo(
        monkeypatch, devsecret_run_repo, ["run", "--secrets", "", "--"], client=client
    )

    err = capsys.readouterr().err
    assert exit_code == 2
    assert err.strip() != ""
    assert list(secret_cache_base.iterdir()) == []


def test_devsecret_run_command_without_a_leading_separator_still_runs(
    monkeypatch: pytest.MonkeyPatch,
    devsecret_run_repo: Path,
    secret_cache_base: Path,
    tmp_path: Path,
) -> None:
    """The '--' separator is how a human spells the boundary; omitting it still works.

    argparse's REMAINDER captures everything from the first unrecognized
    token onward regardless of whether it is a literal '--', so
    `_devsecret_run_command` must also accept a command that never carried
    one.
    """
    runner = _FakeCatalogRunner()
    client = _devsecret_client(runner)
    report_path = tmp_path / "report.json"

    exit_code = run_devsecret_in_repo(
        monkeypatch,
        devsecret_run_repo,
        ["run", "--secrets", "", *_env_reporter_command(report_path)],
        client=client,
    )

    assert exit_code == 0
    assert _read_report(report_path)["cache_dir"] is not None


def test_devsecret_run_secret_cache_dir_inside_repository_root_exits_five(
    monkeypatch: pytest.MonkeyPatch,
    devsecret_run_repo: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC-FUNC-007: end to end through the real CLI, not just at the catalog layer.

    `_run_devsecret_run` resolves `repository_root` from the same repo `run`
    was invoked in; pointing `SECRET_CACHE_DIR` at a directory inside it
    reaches `catalog.secret_cache_dir`'s exposure refusal via the production
    `main_devsecret` wiring, mapped to exit 5 by `_DEVSECRET_EXIT_CODES`.
    """
    inside_repo = devsecret_run_repo / "inside-repo-cache"
    inside_repo.mkdir()
    monkeypatch.setenv("SECRET_CACHE_DIR", str(inside_repo))
    runner = _FakeCatalogRunner()
    client = _devsecret_client(runner)
    report_path = tmp_path / "report.json"

    exit_code = run_devsecret_in_repo(
        monkeypatch,
        devsecret_run_repo,
        ["run", "--secrets", "", "--", *_env_reporter_command(report_path)],
        client=client,
    )

    err = capsys.readouterr().err
    assert exit_code == 5
    assert str(inside_repo) in err
    assert not report_path.exists()
    assert list(inside_repo.iterdir()) == []


def test_devsecret_run_secret_cache_dir_not_ram_backed_exits_five(
    monkeypatch: pytest.MonkeyPatch,
    devsecret_run_repo: Path,
    secret_cache_base: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC-FUNC-008: end to end through the real CLI's default mount-table reader.

    `_run_devsecret_run` never passes its own `mount_table_reader`, so
    `catalog.secret_cache_dir` falls back to `catalog.default_mount_table_reader`,
    which reads `catalog._PROC_MOUNTS_PATH`; pointing that at a fixture mount
    table naming `secret_cache_base` as a non-RAM-backed filesystem reaches
    the same refusal `test_secret_cache_dir_refuses_non_ram_backed_mount`
    exercises directly, through the real CLI this time.
    """
    catalog = _import_catalog()
    fake_mounts = tmp_path / "mounts"
    fake_mounts.write_text(
        f"/dev/sda1 {secret_cache_base} ext4 rw,relatime 0 0\n", encoding="utf-8"
    )
    monkeypatch.setattr(catalog, "_PROC_MOUNTS_PATH", fake_mounts)
    runner = _FakeCatalogRunner()
    client = _devsecret_client(runner)
    report_path = tmp_path / "report.json"

    exit_code = run_devsecret_in_repo(
        monkeypatch,
        devsecret_run_repo,
        ["run", "--secrets", "", "--", *_env_reporter_command(report_path)],
        client=client,
    )

    err = capsys.readouterr().err
    assert exit_code == 5
    assert "ext4" in err
    assert not report_path.exists()
    assert list(secret_cache_base.iterdir()) == []


def test_devsecret_run_command_not_found_exits_two_after_removing_the_transient_directory(
    monkeypatch: pytest.MonkeyPatch,
    devsecret_run_repo: Path,
    secret_cache_base: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A command that cannot be executed exits 2, naming it, after cleanup has run."""
    runner = _FakeCatalogRunner()
    client = _devsecret_client(runner)
    missing_command = f"devsecret-test-no-such-command-{uuid.uuid4().hex}"

    exit_code = run_devsecret_in_repo(
        monkeypatch,
        devsecret_run_repo,
        ["run", "--secrets", "", "--", missing_command],
        client=client,
    )

    err = capsys.readouterr().err
    assert exit_code == 2
    assert missing_command in err
    assert list(secret_cache_base.iterdir()) == []


def test_devsecret_run_outside_a_git_checkout_exits_two_with_no_traceback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`devsecret` is on PATH in the container and on the host, so a cwd
    outside any git checkout is an ordinary invocation, not a crash.
    `repo.find_root` raises `repo.RepoError` there, and `main_devsecret`
    must map it onto the exit-code contract with the standard `ERROR: ...`
    message, not let it escape as a traceback at exit 1.
    """
    not_a_repo = generated_root(tmp_path)
    monkeypatch.chdir(not_a_repo)
    runner = _FakeCatalogRunner()
    client = _devsecret_client(runner)

    exit_code = run_devsecret(
        monkeypatch, ["run", "--secrets", "", "--", "echo", "hi"], client=client
    )

    err = capsys.readouterr().err
    assert exit_code == 2
    assert err.startswith("ERROR:")
    assert "Traceback" not in err


def test_devsecret_run_command_not_executable_exits_two_after_removing_the_transient_directory(
    monkeypatch: pytest.MonkeyPatch,
    devsecret_run_repo: Path,
    secret_cache_base: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A file with the exec bit set but no valid executable format raises
    `OSError` (`Exec format error`), not `FileNotFoundError` or
    `PermissionError`; it must still map onto the same exit-2 usage-error
    contract as a missing command, after the transient directory is removed.
    """
    not_executable = tmp_path / f"devsecret-test-not-executable-{uuid.uuid4().hex}"
    not_executable.write_text("not a real executable\n", encoding="utf-8")
    not_executable.chmod(0o755)
    runner = _FakeCatalogRunner()
    client = _devsecret_client(runner)

    exit_code = run_devsecret_in_repo(
        monkeypatch,
        devsecret_run_repo,
        ["run", "--secrets", "", "--", str(not_executable)],
        client=client,
    )

    err = capsys.readouterr().err
    assert exit_code == 2
    assert str(not_executable) in err
    assert "Exec format error" in err
    assert list(secret_cache_base.iterdir()) == []


def test_devsecret_run_with_a_non_writable_secret_cache_dir_exits_five_with_no_traceback(
    monkeypatch: pytest.MonkeyPatch,
    devsecret_run_repo: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Round-3 code_review finding 2: an OSError from `secret_cache_dir`'s `mkdir`
    must map onto the documented exit-code contract with the standard
    `ERROR: ...` message, not escape as an uncaught traceback at exit 1.
    `SECRET_CACHE_DIR` is overridden to a directory this process cannot
    write to, simulating a mis-set override. The mount table is forced
    unavailable so this reproduces the same `SecretCacheUnavailableError`
    path regardless of the host platform's real `/proc/mounts`.
    """
    catalog = _import_catalog()
    monkeypatch.setattr(catalog, "_PROC_MOUNTS_PATH", tmp_path / "does-not-exist")
    unwritable_base = tmp_path / f"devsecret-unwritable-{uuid.uuid4().hex}"
    unwritable_base.mkdir(mode=0o755)
    unwritable_base.chmod(0o500)
    monkeypatch.setenv("SECRET_CACHE_DIR", str(unwritable_base))
    runner = _FakeCatalogRunner()
    client = _devsecret_client(runner)

    try:
        exit_code = run_devsecret_in_repo(
            monkeypatch,
            devsecret_run_repo,
            ["run", "--secrets", "", "--", "echo", "hi"],
            client=client,
        )
    finally:
        unwritable_base.chmod(0o755)

    err = capsys.readouterr().err
    assert exit_code == 5
    assert err.startswith("ERROR:")
    assert "Traceback" not in err
    assert "SECRET_CACHE_DIR" in err
    assert str(unwritable_base) in err


def test_devsecret_run_help_documents_the_secrets_and_command_syntax(
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = import_cli()

    with pytest.raises(SystemExit) as exc_info:
        cli.main_devsecret(["run", "--help"])

    out = capsys.readouterr().out
    assert exc_info.value.code == 0
    assert "--secrets" in out


def test_devsecret_export_list_prints_only_exported_names_no_value(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC-FUNC-009 / AC-TEST-005: only exported names, one per line, no value anywhere."""
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
                        "Value": value,
                    },
                    {
                        "Name": "/devcontainer/shared/secrets/INTERNAL_ONLY",
                        "LastModifiedDate": 1700000100.0,
                        "Description": json.dumps({"exported": False}),
                    },
                ]
            }
        )
    )
    client = _devsecret_client(runner)

    exit_code = run_devsecret(monkeypatch, ["export-list"], client=client)

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out == "NOTION_TOKEN\n"
    assert value not in captured.out
    assert value not in captured.err
    assert "INTERNAL_ONLY" not in captured.out


def test_devsecret_export_list_nothing_exported_prints_nothing_and_exits_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC-FUNC-010: a catalog with nothing exported prints nothing and exits 0."""
    runner = _FakeCatalogRunner()
    runner.queue(
        _ok(
            {
                "Parameters": [
                    {
                        "Name": "/devcontainer/shared/secrets/INTERNAL_ONLY",
                        "LastModifiedDate": 1700000000.0,
                        "Description": json.dumps({"exported": False}),
                    }
                ]
            }
        )
    )
    client = _devsecret_client(runner)

    exit_code = run_devsecret(monkeypatch, ["export-list"], client=client)

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out == ""


def test_devsecret_export_list_name_exported_in_both_tiers_is_printed_once() -> None:
    """AC-FUNC-009 / AC-TEST-005: a name exported in both queried tiers is printed once.

    Exercises `cli._export_list_names` directly against two records sharing a
    name but carrying different scopes, the shape `catalog.list_resolved`
    produces for a name present in both an instance and the shared tier
    (E3-F1-S2-T1 AC-FUNC-006). devsecret's own commands resolve against
    `catalog.scope_set(None)` -- the shared scope alone, independent of
    `devcontainer_config.instances` (this module's own docstring) -- so
    this dedup rule is asserted directly against the renderer rather than
    through a two-tier CLI invocation that the current single-scope wiring
    cannot produce.
    """
    catalog = _import_catalog()
    cli = import_cli()
    records = (
        catalog.SecretRecord(
            name="NOTION_TOKEN", scope="sandbox", last_modified="1", exported=True, in_effect=True
        ),
        catalog.SecretRecord(
            name="NOTION_TOKEN", scope="shared", last_modified="2", exported=True, in_effect=False
        ),
    )

    names = cli._export_list_names(records)

    assert names == ["NOTION_TOKEN"]


def test_devsecret_export_list_honors_in_effect_over_a_shadowed_exported_marker() -> None:
    """AC-FUNC-009: a name is named only on the authority of its in-effect record.

    The in-effect (instance) record here is NOT exported; the shadowed
    (shared) record for the same name IS exported. `list_resolved` already
    decided the instance record is the one in effect (E3-F1-S2-T1
    AC-FUNC-006), so `_export_list_names` must not print the name on the
    strength of the shadowed record's marker: E3-F2-S2-T1's shell startup
    would then export a name whose in-effect record was never marked for
    export, defeating clearing the marker at the instance tier.
    """
    catalog = _import_catalog()
    cli = import_cli()
    records = (
        catalog.SecretRecord(
            name="NOTION_TOKEN",
            scope="sandbox",
            last_modified="1",
            exported=False,
            in_effect=True,
        ),
        catalog.SecretRecord(
            name="NOTION_TOKEN", scope="shared", last_modified="2", exported=True, in_effect=False
        ),
    )

    names = cli._export_list_names(records)

    assert names == []


def test_cli_source_never_hardcodes_the_secret_cache_dir_env_var_name() -> None:
    """AC-FUNC-011: cli.py never spells out "SECRET_CACHE_DIR" as its own string literal.

    `catalog.SECRET_CACHE_DIR_ENV_VAR` is the one declaration of that name
    (asserted in `tests/test_catalog.py`); this checks cli.py never composes
    its own copy of the literal as a `str`/`bytes` constant, which a plain
    substring search cannot do without also flagging the constant's own name,
    `SECRET_CACHE_DIR_ENV_VAR`, at every reference to it.
    """
    root = Path(__file__).resolve().parents[1]
    cli_path = (
        root / ".claude" / "plugins" / "devcontainer" / "scripts" / "devcontainer_config" / "cli.py"
    )

    tree = ast.parse(cli_path.read_text(encoding="utf-8"))

    literals = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and node.value == "SECRET_CACHE_DIR"
    ]
    assert literals == []


def test_devsecret_end_to_end_export_list_then_run_narrows_to_the_named_secret(
    monkeypatch: pytest.MonkeyPatch,
    devsecret_run_repo: Path,
    secret_cache_base: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC-CYCLE-001: export-list shows the reachable exported name and hides an unreachable one;

    `run --secrets` narrows the child's catalog-derived environment to
    exactly the named secret, leaking neither the other secret's name (via
    export-list) nor its value (via run); the transient directory created
    for the `run` invocation is gone once it returns.

    devsecret's commands resolve against `catalog.scope_set(None)` -- the
    shared scope alone, independent of `devcontainer_config.instances`
    (this module's own docstring) -- so JENKINS_API_TOKEN is seeded into a scope
    (`sandbox`) this injected catalog holds but neither `export-list` nor
    `run` ever queries, in place of a live instance address. That is what
    proves both commands: `export-list` never names anything outside the
    single scope it actually queries, and `run` never exposes anything
    beyond the one name it was asked for, even though the same catalog
    instance holds another exported secret entirely.
    """
    monkeypatch.delenv("NOTION_TOKEN", raising=False)
    monkeypatch.delenv("JENKINS_API_TOKEN", raising=False)
    runner = _FakeCatalogRunner()
    catalog = _import_catalog()
    client = catalog.CatalogClient(runner)
    notion_value = _seeded_value()
    jenkins_value = _seeded_value()

    # Seeded directly at the catalog layer to establish that JENKINS_API_TOKEN
    # really does exist, exported, in this injected catalog -- in a scope
    # devsecret's current wiring never queries -- before proving neither CLI
    # command surfaces it.
    sandbox_runner = _FakeCatalogRunner()
    sandbox_runner.queue(
        _ok(
            {
                "Parameters": [
                    {
                        "Name": "/devcontainer/sandbox/secrets/JENKINS_API_TOKEN",
                        "LastModifiedDate": 1700000000.0,
                        "Description": json.dumps({"exported": True}),
                        "Value": jenkins_value,
                    }
                ]
            }
        )
    )
    sandbox_client = catalog.CatalogClient(sandbox_runner)
    sandbox_records = sandbox_client.list_secrets("sandbox")
    assert [r.name for r in sandbox_records] == ["JENKINS_API_TOKEN"]

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
    exit_code = run_devsecret_in_repo(
        monkeypatch, devsecret_run_repo, ["export-list"], client=client
    )
    export_out = capsys.readouterr().out

    assert exit_code == 0
    assert export_out == "NOTION_TOKEN\n"
    assert "JENKINS_API_TOKEN" not in export_out
    assert jenkins_value not in export_out

    runner.queue(_ok({"Parameter": {"Value": notion_value}}))
    report_path = tmp_path / "report.json"
    exit_code = run_devsecret_in_repo(
        monkeypatch,
        devsecret_run_repo,
        ["run", "--secrets", "NOTION_TOKEN", "--", *_env_reporter_command(report_path)],
        client=client,
    )

    assert exit_code == 0
    report = _read_report(report_path)
    assert report["env"]["NOTION_TOKEN"] == notion_value
    assert "JENKINS_API_TOKEN" not in report["env"]
    cache_dir = report["cache_dir"]
    assert isinstance(cache_dir, str)
    assert not Path(cache_dir).exists()
    assert list(secret_cache_base.iterdir()) == []
    assert jenkins_value not in json.dumps(report)


# ---------------------------------------------------------------------------
# hostcreds: creds-init, creds-fragments, shell-block -- the CLI half of
# 'make push-creds' and the postCreate startup-block render. The manifest
# contract, resolvers and renderers are tested in tests/test_hostcreds.py;
# what this section covers is the wiring: argparse, exit codes, the argv
# discipline of the keychain STORE path (the value rides the 'security -i'
# stdin document, never argv), fragment file modes, and stdout carrying
# exactly the names container.sh consumes.
# ---------------------------------------------------------------------------


def _import_hostcreds() -> ModuleType:
    """Import devcontainer_config.hostcreds from inside a function body.

    Deferred for the same reason `import_cli` is: a module-level import
    would fail collection for the whole file instead of failing the one
    test for the real reason under the TDD RED gate.
    """
    return importlib.import_module("devcontainer_config.hostcreds")


class _FakeKeychain:
    """A hostcreds.Runner double backing a real-shaped in-memory keychain.

    Answers 'security find-generic-password' probes from an in-memory item
    map (non-zero exit for an absent item, the value plus one trailing
    newline for a present one, exactly what the real CLI prints) and
    executes 'security -i' add-generic-password documents by parsing them
    with shlex -- which reads the same double-quoted, backslash-escaped
    forms cli._security_command_quoted emits -- so a store is observably
    readable by the next probe, like the real keychain. A probe carrying
    no '-a' matches any account for the service, like the real
    find-generic-password. Records every (argv, stdin) pair so argv
    discipline is assertable per call.
    """

    def __init__(self) -> None:
        self.items: dict[tuple[str, str | None], str] = {}
        self.calls: list[tuple[tuple[str, ...], str | None]] = []
        self.git_password = _seeded_value()
        # Flipping this to False simulates a store whose items vanish again
        # immediately, exercising the post-store re-probe failure branch.
        self.persist = True

    def __call__(
        self, argv: list[str] | tuple[str, ...], stdin: str | None
    ) -> subprocess.CompletedProcess[str]:
        argv = tuple(argv)
        self.calls.append((argv, stdin))
        if "find-generic-password" in argv:
            service = argv[argv.index("-s") + 1]
            if "-a" in argv:
                value = self.items.get((service, argv[argv.index("-a") + 1]))
            else:
                # No account on the probe: any account for the service
                # matches, like the real `security find-generic-password`.
                found = [v for (s, _a), v in self.items.items() if s == service]
                value = found[0] if found else None
            if value is None:
                return subprocess.CompletedProcess(
                    args=[],
                    returncode=44,
                    stdout="",
                    stderr="The specified item could not be found",
                )
            return subprocess.CompletedProcess(
                args=[], returncode=0, stdout=value + "\n", stderr=""
            )
        if argv == ("security", "-i"):
            document = stdin or ""
            words = shlex.split(document)
            if not words or words[0] != "add-generic-password":
                return subprocess.CompletedProcess(
                    args=[], returncode=1, stdout="", stderr=f"unknown command: {document!r}"
                )
            service = words[words.index("-s") + 1]
            account = words[words.index("-a") + 1] if "-a" in words else None
            value = words[words.index("-w") + 1]
            if self.persist:
                self.items[(service, account)] = value
            return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        if argv[0:2] == ("git", "credential"):
            return subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=f"username=dev\npassword={self.git_password}\n\n",
                stderr="",
            )
        raise AssertionError(f"_FakeKeychain invoked with an unexpected command: {argv!r}")


class _StubGetpass:
    """A getpass double: records prompts, answers from a queue.

    An empty queue raises, so a test that expects no prompt fails loudly on
    the first unexpected one instead of silently answering ''.
    """

    def __init__(self, values: Sequence[str] = ()) -> None:
        self._values = list(values)
        self.prompts: list[str] = []

    def getpass(self, prompt: str = "") -> str:
        self.prompts.append(prompt)
        return self._values.pop(0)


def _write_hostcreds_manifest(root: Path, entries: dict[str, object]) -> None:
    devcontainer = root / ".devcontainer"
    devcontainer.mkdir(exist_ok=True)
    (devcontainer / "hostcreds.map.json").write_text(json.dumps(entries), encoding="utf-8")


def _creds_repo(tmp_path: Path) -> Path:
    """A real, disposable git repository the creds commands can find_root in."""
    root = generated_root(tmp_path)
    init_repo(root)
    return root


def run_hostcreds_cli(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
    args: list[str],
    *,
    stdin: io.StringIO | None = None,
) -> int:
    """Run `devcontainer_config.cli.main(args)` chdir'd into `root`; the exit code."""
    cli = import_cli()
    monkeypatch.chdir(root)
    if stdin is not None:
        monkeypatch.setattr("sys.stdin", stdin)
    with pytest.raises(SystemExit) as exc_info:
        cli.main(args)
    code = exc_info.value.code
    if not isinstance(code, int):
        raise AssertionError(f"cli.main exited with a non-integer code: {code!r}")
    return code


def _install_fake_keychain(monkeypatch: pytest.MonkeyPatch, fake: _FakeKeychain) -> None:
    """Point hostcreds.subprocess_runner at `fake` for the current test.

    The creds handlers read the runner from the hostcreds module at call
    time precisely so this substitution needs no patching of the cli module
    itself.
    """
    hostcreds_module = _import_hostcreds()
    monkeypatch.setattr(hostcreds_module, "subprocess_runner", fake)


def test_creds_init_prompts_once_and_stores_the_value_on_stdin_never_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A missing keychain item prompts once, and the store's value rides stdin."""
    root = _creds_repo(tmp_path)
    _write_hostcreds_manifest(
        root, {"API_TOKEN": {"source": "keychain", "service": "devcontainer/test/API_TOKEN"}}
    )
    fake = _FakeKeychain()
    _install_fake_keychain(monkeypatch, fake)
    value = _seeded_value()
    stub = _StubGetpass([value])
    monkeypatch.setattr(import_cli(), "getpass", stub)

    exit_code = run_hostcreds_cli(monkeypatch, root, ["creds-init"])

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "stored: API_TOKEN" in out
    assert fake.items[("devcontainer/test/API_TOKEN", "API_TOKEN")] == value
    assert len(stub.prompts) == 1, "a missing item must prompt exactly once"
    assert "API_TOKEN" in stub.prompts[0]
    assert "devcontainer/test/API_TOKEN" in stub.prompts[0]
    assert value not in stub.prompts[0]
    stores = [(argv, stdin) for argv, stdin in fake.calls if argv == ("security", "-i")]
    assert len(stores) == 1
    (store_argv, store_stdin) = stores[0]
    assert "add-generic-password" in store_stdin
    assert '-a "API_TOKEN"' in store_stdin, (
        "add-generic-password without -a fails on current macOS; the "
        "credential's own name is the stable default account"
    )
    assert value in store_stdin
    assert value not in " ".join(store_argv)
    for argv, _stdin in fake.calls:
        assert value not in " ".join(argv), "a value must never ride any runner argv"


def test_creds_init_skips_prompting_for_an_item_already_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An existing item is reported already-present with no prompt and no store."""
    root = _creds_repo(tmp_path)
    _write_hostcreds_manifest(
        root, {"API_TOKEN": {"source": "keychain", "service": "devcontainer/test/API_TOKEN"}}
    )
    fake = _FakeKeychain()
    fake.items[("devcontainer/test/API_TOKEN", "API_TOKEN")] = _seeded_value()
    _install_fake_keychain(monkeypatch, fake)
    stub = _StubGetpass()  # empty: any prompt raises inside the stub
    monkeypatch.setattr(import_cli(), "getpass", stub)

    exit_code = run_hostcreds_cli(monkeypatch, root, ["creds-init"])

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "already present: API_TOKEN" in out
    assert "stored:" not in out
    assert stub.prompts == []
    assert all(argv != ("security", "-i") for argv, _stdin in fake.calls)


def test_creds_init_stdin_flag_stores_one_named_value_without_prompting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """--stdin NAME takes exactly that credential's value from stdin, no prompt.

    The one trailing newline is stripped (and only one: a value ending in
    real newline bytes keeps them). Other missing keychain items would
    still prompt; here the only other entry is already present, so no
    prompt is expected at all.
    """
    root = _creds_repo(tmp_path)
    _write_hostcreds_manifest(
        root,
        {
            "API_TOKEN": {"source": "keychain", "service": "devcontainer/test/API_TOKEN"},
            "SECOND_TOKEN": {"source": "keychain", "service": "devcontainer/test/SECOND_TOKEN"},
        },
    )
    fake = _FakeKeychain()
    fake.items[("devcontainer/test/SECOND_TOKEN", "SECOND_TOKEN")] = _seeded_value()
    _install_fake_keychain(monkeypatch, fake)
    stub = _StubGetpass()
    monkeypatch.setattr(import_cli(), "getpass", stub)
    value = _seeded_value()

    exit_code = run_hostcreds_cli(
        monkeypatch,
        root,
        ["creds-init", "--stdin", "API_TOKEN"],
        stdin=io.StringIO(value + "\n"),
    )

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "stored: API_TOKEN" in out
    assert fake.items[("devcontainer/test/API_TOKEN", "API_TOKEN")] == value
    assert stub.prompts == []


def test_creds_init_stdin_empty_value_is_an_error_and_stores_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _creds_repo(tmp_path)
    _write_hostcreds_manifest(
        root, {"API_TOKEN": {"source": "keychain", "service": "devcontainer/test/API_TOKEN"}}
    )
    fake = _FakeKeychain()
    _install_fake_keychain(monkeypatch, fake)

    exit_code = run_hostcreds_cli(
        monkeypatch, root, ["creds-init", "--stdin", "API_TOKEN"], stdin=io.StringIO("\n")
    )

    assert exit_code != 0
    assert fake.items == {}
    assert all(argv != ("security", "-i") for argv, _stdin in fake.calls)
    assert "empty" in capsys.readouterr().err


def test_creds_init_stdin_newline_value_is_refused_before_the_store_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A value carrying a newline would split the 'security -i' document and
    run the remainder as a second command, so it is refused on sight, before
    anything is stored."""
    root = _creds_repo(tmp_path)
    _write_hostcreds_manifest(
        root, {"API_TOKEN": {"source": "keychain", "service": "devcontainer/test/API_TOKEN"}}
    )
    fake = _FakeKeychain()
    _install_fake_keychain(monkeypatch, fake)

    exit_code = run_hostcreds_cli(
        monkeypatch,
        root,
        ["creds-init", "--stdin", "API_TOKEN"],
        stdin=io.StringIO("first\nsecond\n"),
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "API_TOKEN" in captured.err, "the error names the credential"
    assert "newline" in captured.err
    assert "first" not in captured.err, "the value is never echoed"
    assert fake.items == {}
    assert all(argv != ("security", "-i") for argv, _stdin in fake.calls)


def test_creds_init_store_failure_prints_exit_code_only_never_security_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """'security -i' can echo command tokens -- value fragments included --
    on its stderr, so a failed store reports the exit status and a generic
    remedy, never that stderr."""
    root = _creds_repo(tmp_path)
    _write_hostcreds_manifest(
        root, {"API_TOKEN": {"source": "keychain", "service": "devcontainer/test/API_TOKEN"}}
    )
    value = _seeded_value()
    fragment = value[: len(value) // 2]

    def failing_store(
        argv: list[str] | tuple[str, ...], stdin: str | None
    ) -> subprocess.CompletedProcess[str]:
        if "-i" in tuple(argv):
            return subprocess.CompletedProcess(
                args=[], returncode=45, stdout="", stderr=f"security: bad token {fragment}"
            )
        return subprocess.CompletedProcess(
            args=[], returncode=44, stdout="", stderr="The specified item could not be found"
        )

    _install_fake_keychain(monkeypatch, failing_store)

    exit_code = run_hostcreds_cli(
        monkeypatch,
        root,
        ["creds-init", "--stdin", "API_TOKEN"],
        stdin=io.StringIO(value + "\n"),
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "API_TOKEN" in captured.err, "the error names the credential"
    assert "45" in captured.err, "the exit status is named"
    assert "security -i" in captured.err, "the remedy names the command to re-run by hand"
    assert fragment not in captured.err, "no stderr fragment may be echoed"
    assert value not in captured.err


def test_creds_init_stdin_name_outside_the_manifest_is_a_usage_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """--stdin naming a non-keychain entry is refused before anything is read."""
    root = _creds_repo(tmp_path)
    _write_hostcreds_manifest(root, {"GIT_GITHUB": {"source": "git", "host": "github.com"}})
    fake = _FakeKeychain()
    _install_fake_keychain(monkeypatch, fake)

    exit_code = run_hostcreds_cli(
        monkeypatch, root, ["creds-init", "--stdin", "GIT_GITHUB"], stdin=io.StringIO("x")
    )

    assert exit_code == 2
    assert "GIT_GITHUB" in capsys.readouterr().err
    assert fake.calls == []


def test_creds_init_missing_manifest_names_the_path_and_make_init(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _creds_repo(tmp_path)

    exit_code = run_hostcreds_cli(monkeypatch, root, ["creds-init"])

    err = capsys.readouterr().err
    assert exit_code == 1
    assert "hostcreds.map.json" in err
    assert "make init" in err


def test_creds_init_store_that_does_not_persist_fails_the_reprobe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Exit 0 requires the post-store re-probe to find the item."""
    root = _creds_repo(tmp_path)
    _write_hostcreds_manifest(
        root, {"API_TOKEN": {"source": "keychain", "service": "devcontainer/test/API_TOKEN"}}
    )
    fake = _FakeKeychain()
    fake.persist = False
    _install_fake_keychain(monkeypatch, fake)
    monkeypatch.setattr(import_cli(), "getpass", _StubGetpass([_seeded_value()]))

    exit_code = run_hostcreds_cli(monkeypatch, root, ["creds-init"])

    assert exit_code == 1
    assert "probing" in capsys.readouterr().err


def test_creds_init_probe_argv_is_the_shared_hostcreds_builder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The probe's argv is `hostcreds.keychain_find_argv`'s output verbatim:
    one builder serves creds-init's probe and the push-time resolver, so
    the two can never drift into addressing different items."""
    root = _creds_repo(tmp_path)
    _write_hostcreds_manifest(root, {"API_TOKEN": {"source": "keychain"}})
    fake = _FakeKeychain()
    fake.items[(f"devcontainer/{root.name}/API_TOKEN", "API_TOKEN")] = _seeded_value()
    _install_fake_keychain(monkeypatch, fake)

    exit_code = run_hostcreds_cli(monkeypatch, root, ["creds-init"])

    hostcreds_module = _import_hostcreds()
    assert exit_code == 0
    spec = hostcreds_module.load_manifest(root)[0]
    probes = [argv for argv, _stdin in fake.calls if "find-generic-password" in argv]
    assert probes, "a present item is still probed once"
    for argv in probes:
        assert tuple(argv) == hostcreds_module.keychain_find_argv(spec)


def test_keychain_find_argv_omits_the_account_only_when_the_manifest_has_none() -> None:
    """The shared builder's exact shape for both account states: `-a`
    appears when the manifest sets an account and is omitted entirely
    otherwise (an empty `-a` matches a different item than none at all)."""
    hostcreds_module = _import_hostcreds()
    default_account_spec = hostcreds_module.CredentialSpec(
        name="API_TOKEN",
        source=hostcreds_module.SOURCE_KEYCHAIN,
        labels={"service": "devcontainer/t/API_TOKEN"},
    )
    assert hostcreds_module.keychain_find_argv(default_account_spec) == (
        "security",
        "find-generic-password",
        "-w",
        "-s",
        "devcontainer/t/API_TOKEN",
    )
    manifest_account_spec = hostcreds_module.CredentialSpec(
        name="API_TOKEN",
        source=hostcreds_module.SOURCE_KEYCHAIN,
        labels={"service": "svc.example/token", "account": "alice"},
    )
    assert hostcreds_module.keychain_find_argv(manifest_account_spec) == (
        "security",
        "find-generic-password",
        "-w",
        "-s",
        "svc.example/token",
        "-a",
        "alice",
    )


def test_creds_fragments_writes_private_fragments_and_prints_exactly_the_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """One 0600 fragment per resolved credential; stdout is only the names."""
    root = _creds_repo(tmp_path)
    _write_hostcreds_manifest(
        root,
        {
            "API_TOKEN": {"source": "keychain", "service": "devcontainer/test/API_TOKEN"},
            "GIT_GITHUB": {"source": "git", "host": "github.com"},
        },
    )
    fake = _FakeKeychain()
    value = _seeded_value()
    fake.items[("devcontainer/test/API_TOKEN", None)] = value
    _install_fake_keychain(monkeypatch, fake)
    output_dir = tmp_path / "fragments"
    output_dir.mkdir()

    exit_code = run_hostcreds_cli(
        monkeypatch, root, ["creds-fragments", "--output-dir", str(output_dir)]
    )

    captured = capsys.readouterr()
    hostcreds_module = _import_hostcreds()
    assert exit_code == 0
    assert captured.out == "API_TOKEN\nGIT_GITHUB\n"
    token_fragment = (output_dir / "API_TOKEN.env").read_text(encoding="utf-8")
    git_fragment = (output_dir / "GIT_GITHUB.env").read_text(encoding="utf-8")
    assert token_fragment.startswith(hostcreds_module.FRAGMENT_MARKER)
    assert f"export API_TOKEN='{value}'" in token_fragment
    assert git_fragment.startswith(hostcreds_module.FRAGMENT_MARKER)
    assert fake.git_password not in git_fragment
    for fragment in (output_dir / "API_TOKEN.env", output_dir / "GIT_GITHUB.env"):
        assert stat.S_IMODE(fragment.stat().st_mode) == 0o600
    assert value not in captured.err
    assert fake.git_password not in captured.err


def test_creds_fragments_refuses_to_overwrite_an_existing_fragment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """O_EXCL: a stale fragment in a reused directory fails the run."""
    root = _creds_repo(tmp_path)
    _write_hostcreds_manifest(
        root, {"API_TOKEN": {"source": "keychain", "service": "devcontainer/test/API_TOKEN"}}
    )
    fake = _FakeKeychain()
    fake.items[("devcontainer/test/API_TOKEN", None)] = _seeded_value()
    _install_fake_keychain(monkeypatch, fake)
    output_dir = tmp_path / "fragments"
    output_dir.mkdir()
    stale = output_dir / "API_TOKEN.env"
    stale.write_text("stale\n", encoding="utf-8")

    exit_code = run_hostcreds_cli(
        monkeypatch, root, ["creds-fragments", "--output-dir", str(output_dir)]
    )

    err = capsys.readouterr().err
    assert exit_code == 1
    assert "API_TOKEN.env" in err
    assert stale.read_text(encoding="utf-8") == "stale\n"


def test_creds_fragments_any_unresolved_entry_fails_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Fail fast: one unresolvable entry aborts the run with exit 1 even
    though its siblings resolved -- a container silently shipping a subset
    of the manifest is the failure this prevents."""
    root = _creds_repo(tmp_path)
    _write_hostcreds_manifest(
        root,
        {
            "API_TOKEN": {"source": "keychain", "service": "devcontainer/test/API_TOKEN"},
            "MISSING_TOKEN": {"source": "keychain", "service": "devcontainer/test/MISSING_TOKEN"},
        },
    )
    fake = _FakeKeychain()
    fake.items[("devcontainer/test/API_TOKEN", "API_TOKEN")] = _seeded_value()
    _install_fake_keychain(monkeypatch, fake)
    output_dir = tmp_path / "fragments"
    output_dir.mkdir()

    exit_code = run_hostcreds_cli(
        monkeypatch, root, ["creds-fragments", "--output-dir", str(output_dir)]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == "API_TOKEN\n"
    assert (output_dir / "API_TOKEN.env").is_file()
    assert not (output_dir / "MISSING_TOKEN.env").exists()
    assert "MISSING_TOKEN" in captured.err
    assert "aborted" in captured.err, "the summary states that the push aborted"


def test_creds_fragments_exits_one_when_nothing_resolves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _creds_repo(tmp_path)
    _write_hostcreds_manifest(
        root, {"MISSING_TOKEN": {"source": "keychain", "service": "devcontainer/test/x"}}
    )
    _install_fake_keychain(monkeypatch, _FakeKeychain())

    exit_code = run_hostcreds_cli(
        monkeypatch, root, ["creds-fragments", "--output-dir", str(tmp_path / "fragments")]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert "MISSING_TOKEN" in captured.err
    assert "aborted" in captured.err, "the summary states that the push aborted"


def test_creds_fragments_empty_manifest_exits_zero_printing_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An empty manifest is the explicit statement that nothing is pushed."""
    root = _creds_repo(tmp_path)
    _write_hostcreds_manifest(root, {})

    exit_code = run_hostcreds_cli(
        monkeypatch, root, ["creds-fragments", "--output-dir", str(tmp_path / "fragments")]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out == ""


def test_creds_fragments_requires_output_dir_without_print_git_hosts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _creds_repo(tmp_path)
    _write_hostcreds_manifest(root, {})

    exit_code = run_hostcreds_cli(monkeypatch, root, ["creds-fragments"])

    err = capsys.readouterr().err
    assert exit_code == 2
    assert "--output-dir" in err


def test_creds_fragments_print_git_hosts_prints_only_the_hosts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """--print-git-hosts writes nothing and prints only git-source hostnames."""
    root = _creds_repo(tmp_path)
    _write_hostcreds_manifest(
        root,
        {
            "GIT_GITHUB": {"source": "git", "host": "github.com"},
            "GIT_OTHER": {"source": "git", "host": "gitlab.example.com"},
            "API_TOKEN": {"source": "keychain", "service": "devcontainer/test/API_TOKEN"},
        },
    )
    _install_fake_keychain(monkeypatch, _FakeKeychain())

    exit_code = run_hostcreds_cli(monkeypatch, root, ["creds-fragments", "--print-git-hosts"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out == "github.com\ngitlab.example.com\n"


def test_creds_fragments_missing_manifest_names_the_path_and_make_init(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _creds_repo(tmp_path)

    exit_code = run_hostcreds_cli(
        monkeypatch, root, ["creds-fragments", "--output-dir", str(tmp_path / "fragments")]
    )

    err = capsys.readouterr().err
    assert exit_code == 1
    assert "hostcreds.map.json" in err
    assert "make init" in err


def test_shell_block_prints_the_startup_block_with_its_marker_first(
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = import_cli()
    hostcreds_module = _import_hostcreds()

    with pytest.raises(SystemExit) as exc_info:
        cli.main(["shell-block"])

    out = capsys.readouterr().out
    assert exc_info.value.code == 0
    assert out == hostcreds_module.render_startup_block()
    assert out.splitlines()[0] == hostcreds_module.MARKER


def test_shell_block_module_invocation_prints_the_block_via_subprocess() -> None:
    """The exact invocation postCreate uses: `-m devcontainer_config.cli shell-block`."""
    repo_root = Path(__file__).resolve().parents[1]
    scripts_dir = repo_root / ".claude" / "plugins" / "devcontainer" / "scripts"
    env = dict(os.environ)
    env["PYTHONPATH"] = str(scripts_dir)
    result = subprocess.run(
        ["python3", "-m", "devcontainer_config.cli", "shell-block"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[0] == "# hostcreds-credential-startup-block"
