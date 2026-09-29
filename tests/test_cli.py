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

import importlib
import io
import json
import os
import shlex
import stat
import subprocess
import uuid
from collections.abc import Mapping, Sequence
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


def _seeded_value() -> str:
    """A generated placeholder value, unique per call, never a real credential."""
    return f"seeded-value-{uuid.uuid4().hex}"


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
    discipline is assertable per call. Accepts (and ignores) the optional
    keyword-only `env` resolve_git forwards to its runner, so a git-source
    manifest entry resolves through the same double.
    """

    def __init__(self) -> None:
        self.items: dict[tuple[str, str | None], str] = {}
        self.calls: list[tuple[tuple[str, ...], str | None]] = []
        self.git_password = _seeded_value()
        # Flipping this to False simulates a store whose items vanish again
        # immediately, exercising the post-store re-probe failure branch.
        self.persist = True

    def __call__(
        self,
        argv: list[str] | tuple[str, ...],
        stdin: str | None,
        *,
        env: Mapping[str, str] | None = None,
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


# ---------------------------------------------------------------------------
# instance subcommands (spec Section 4.5, U2) -- the thin handlers over
# devcontainer_config.instance_ops that `make list-instances` and the
# instance-* make targets drive. The engine's own behavior is covered in
# tests/test_instance_ops.py; what this section pins is the wiring: argparse
# (a missing NAME exits 2 with usage), the subprocess_runner seam (read from
# the instance_ops module at call time so a queued fake substitutes
# cleanly), the instance-id shape refusal, and --json's single-object shape.
# No test here touches AWS, docker, a keychain or the network.
# ---------------------------------------------------------------------------


def _import_instance_ops() -> ModuleType:
    """Import devcontainer_config.instance_ops from inside a function body."""
    return importlib.import_module("devcontainer_config.instance_ops")


# The region the power and cleanup tests pass; a fixture value, not
# configuration (the functions under test take their region explicitly).
_INSTANCE_REGION = "us-east-1"


def _instance_ok(stdout: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


def _instance_err(stderr: str, *, returncode: int = 1) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout="", stderr=stderr)


class _InstanceFakeRunner:
    """A queued Runner double in the instance_ops shape: records, then pops.

    The same strict-queue discipline `tests/test_instance_ops.py`'s double
    applies: an invocation with no queued response fails the test instead of
    being answered with a default that would let an unexpected aws/docker
    call pass silently.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self._queue: list[subprocess.CompletedProcess[str]] = []

    def queue(self, *results: subprocess.CompletedProcess[str]) -> None:
        self._queue.extend(results)

    def __call__(
        self, argv: Sequence[str], stdin: str | None, *, env: Mapping[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(tuple(argv))
        assert self._queue, (
            f"_InstanceFakeRunner was invoked with no queued response: {tuple(argv)!r}"
        )
        return self._queue.pop(0)


@pytest.fixture()
def _instance_docker_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point DOCKER_CONFIG at a per-test directory.

    The id store and the certificate material both live under
    `instances.certs_dir(name)`, which honors DOCKER_CONFIG; keeping that on
    `tmp_path` away from the operator's real `~/.docker/certs`. A named
    fixture rather than autouse: the pre-existing suites in this file never
    read certificate paths, and they should not inherit an environment
    change they had no say in.
    """
    docker_config_dir = tmp_path / "docker-config"
    docker_config_dir.mkdir()
    monkeypatch.setenv("DOCKER_CONFIG", str(docker_config_dir))
    return docker_config_dir


def _instance_name(prefix: str = "inst") -> str:
    """A valid instance name unique per call."""
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _instance_id() -> str:
    """An i-prefixed, 17-hex instance-id-shaped value, generated per call."""
    return "i-" + uuid.uuid4().hex[:17]


def _git_root_with_origin(tmp_path: Path) -> Path:
    """A disposable git checkout whose origin slug `repo.repo_slug` can read.

    Every derivation in `instances` that names a docker context goes through
    `repo.repo_slug`, which reads `remote.origin.url`, so a bare repo is not
    enough for the handlers that touch one.
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
    return root


@pytest.mark.parametrize(
    "argv",
    [
        ("instance-status",),
        ("instance-stop",),
        ("instance-start",),
        ("instance-cleanup",),
        ("instance-link",),
    ],
)
def test_instance_commands_without_a_name_exit_two_with_usage(
    argv: list[str] | tuple[str, ...],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Missing NAME is a usage error: argparse prints usage and exits 2."""
    cli = import_cli()
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SystemExit) as exc_info:
        cli.main(list(argv))

    assert exc_info.value.code == 2
    assert "usage" in capsys.readouterr().err.lower()


def test_instance_link_requires_an_instance_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cli = import_cli()
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SystemExit) as exc_info:
        cli.main(["instance-link", _instance_name()])

    assert exc_info.value.code == 2
    assert "usage" in capsys.readouterr().err.lower()


def test_instance_link_rejects_a_malformed_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A typo'd id would misdirect every power operation at once; refuse at exit 2."""
    cli = import_cli()
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SystemExit) as exc_info:
        cli.main(["instance-link", _instance_name(), "--instance-id", "not-an-instance-id"])

    assert exc_info.value.code == 2
    err = capsys.readouterr().err
    assert "ERROR" in err
    assert "i-" in err, "the refusal must show the expected shape"


def test_instance_link_records_the_id_in_the_per_instance_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    _instance_docker_config: Path,
) -> None:
    root = _git_root_with_origin(tmp_path)
    instance_ops = _import_instance_ops()
    name = _instance_name()
    instance_id = _instance_id()

    exit_code = run_cli(monkeypatch, root, ["instance-link", name, "--instance-id", instance_id])

    out = capsys.readouterr().out
    assert exit_code == 0
    assert instance_id in out
    assert instance_ops.recorded_id(root, name) == instance_id


def test_instance_init_delegates_to_scaffold_and_prints_its_guidance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The handler prints scaffold's messages verbatim and never calls Terragrunt."""
    root = _git_root_with_origin(tmp_path)
    instance_ops = _import_instance_ops()
    name = _instance_name()
    ami_id = "ami-" + uuid.uuid4().hex
    runner = _InstanceFakeRunner()
    runner.queue(_instance_ok(f"{ami_id}\n"))  # the one SSM read scaffold makes
    monkeypatch.setattr(instance_ops, "subprocess_runner", runner)

    exit_code = run_cli(monkeypatch, root, ["instance-init", name])

    out = capsys.readouterr().out
    assert exit_code == 0
    scaffolded = root / "remote-instances" / name / "terragrunt.hcl"
    assert scaffolded.is_file()
    assert ami_id in scaffolded.read_text(encoding="utf-8")
    assert f"Deploy with: make instance-deploy INSTANCE={name}" in out
    assert len(runner.calls) == 1, "scaffold's only external call is the SSM AMI read"
    assert runner.calls[0][0] == "aws"


def test_instance_stop_delegates_with_the_recorded_id_and_region(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    _instance_docker_config: Path,
) -> None:
    root = _git_root_with_origin(tmp_path)
    instance_ops = _import_instance_ops()
    name = _instance_name()
    instance_id = _instance_id()
    instance_ops.link_id(root, name, instance_id)
    runner = _InstanceFakeRunner()
    runner.queue(_instance_ok("{}\n"))  # stop-instances
    runner.queue(_instance_ok("stopped\n"))  # first poll observes the target state
    monkeypatch.setattr(instance_ops, "subprocess_runner", runner)

    exit_code = run_cli(monkeypatch, root, ["instance-stop", name, "--region", _INSTANCE_REGION])

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "is stopped" in out
    assert runner.calls[0] == (
        "aws",
        "ec2",
        "stop-instances",
        "--instance-ids",
        instance_id,
        "--region",
        _INSTANCE_REGION,
    )


def test_instance_status_json_prints_one_object(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    _instance_docker_config: Path,
) -> None:
    """--json prints exactly one single-line object with the documented fields."""
    root = _git_root_with_origin(tmp_path)
    instance_ops = _import_instance_ops()
    name = _instance_name()
    directory = root / "remote-instances" / name
    directory.mkdir(parents=True)
    (directory / "terragrunt.hcl").write_text("# seeded\n", encoding="utf-8")
    runner = _InstanceFakeRunner()
    runner.queue(_instance_ok('{"Parameters": []}\n'))  # describe-parameters
    runner.queue(_instance_err("no such context: anything"))  # docker context inspect
    monkeypatch.setattr(instance_ops, "subprocess_runner", runner)

    exit_code = run_cli(monkeypatch, root, ["instance-status", name, "--json"])

    out_lines = capsys.readouterr().out.splitlines()
    assert exit_code == 0
    assert len(out_lines) == 1
    row = json.loads(out_lines[0])
    assert row["name"] == name
    assert row["directory"] is True
    assert row["params_present"] is False
    assert row["certs_present"] is False
    assert row["context_exists"] is False
    assert row["lookup_error"] is None


def test_instance_cleanup_delegates_and_prints_each_surface(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    _instance_docker_config: Path,
) -> None:
    root = _git_root_with_origin(tmp_path)
    instance_ops = _import_instance_ops()
    name = _instance_name()
    parameter_name = f"/devcontainer/{name}/shell.env"
    context = instance_ops.instances.docker_context(root, name)
    # Seed the certificate-material directory so its removal (the recorded
    # instance-id file included) is exercised, not just the absent case.
    certs_dir = instance_ops.instances.certs_dir(name)
    certs_dir.mkdir(parents=True)
    (certs_dir / "client-cert.pem").write_text("# seeded\n", encoding="utf-8")
    runner = _InstanceFakeRunner()
    runner.queue(_instance_ok(json.dumps({"Parameters": [{"Name": parameter_name}]})))
    runner.queue(_instance_ok("{}\n"))  # delete-parameter
    runner.queue(_instance_ok("{}\n"))  # docker context inspect: exists
    runner.queue(_instance_ok("{}\n"))  # docker context rm
    monkeypatch.setattr(instance_ops, "subprocess_runner", runner)

    exit_code = run_cli(monkeypatch, root, ["instance-cleanup", name, "--region", _INSTANCE_REGION])

    out = capsys.readouterr().out
    assert exit_code == 0
    assert parameter_name in out
    assert context in out
    assert "removed certificate directory" in out
    assert not certs_dir.exists()
    assert runner.calls[1] == (
        "aws",
        "ssm",
        "delete-parameter",
        "--name",
        parameter_name,
        "--region",
        _INSTANCE_REGION,
    )


def test_instance_cleanup_failure_exits_nonzero_with_the_cli_error_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    _instance_docker_config: Path,
) -> None:
    """CleanupError reaches main's dispatch: ERROR text on stderr, exit 1, no traceback."""
    root = _git_root_with_origin(tmp_path)
    instance_ops = _import_instance_ops()
    name = _instance_name()
    runner = _InstanceFakeRunner()
    runner.queue(_instance_err("simulated SSM outage"))  # describe-parameters
    runner.queue(_instance_err("simulated docker outage"))  # docker context inspect
    monkeypatch.setattr(instance_ops, "subprocess_runner", runner)

    exit_code = run_cli(monkeypatch, root, ["instance-cleanup", name, "--region", _INSTANCE_REGION])

    err = capsys.readouterr().err
    assert exit_code == 1
    assert "ERROR" in err
    assert name in err
    assert "Traceback" not in err
