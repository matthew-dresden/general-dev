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
"""

from __future__ import annotations

import io
from pathlib import Path

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
