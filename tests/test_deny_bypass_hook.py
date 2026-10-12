"""Tests for `.claude/hooks/deny_bypass.py` (spec Section 4.6.1, AC-4.6).

`.claude/hooks/deny_bypass.py` lives under `.claude/hooks/`, outside the
`pythonpath` entry `pyproject.toml` sets for `devcontainer_config`
(`.claude/plugins/devcontainer/scripts`), and it is deliberately not part
of that package: the hook has to run from a bare `python3` invocation
Claude Code makes directly, with no `PYTHONPATH` of its own. So it cannot
be imported by dotted name at all; `_import_deny_bypass` below loads it
from its file path with `importlib.util.spec_from_file_location` instead
of `importlib.import_module`.

That load is deferred into a function body, called by every test below,
rather than done once at module scope, for the reason this suite's sibling
test modules document (see `tests/test_repo.py`): the TDD RED gate stashes
this unit's production-source files and re-runs a single named test node,
and a module-level load of a file that does not exist yet would raise
during COLLECTION for the whole file (pytest exit 2, no test outcome
recorded) instead of failing only the one test for the real reason.

`tests/data/bypass_denial_cases.json` is loaded with plain `json.loads` at
collection time, the same as `tests/data/secret_scanner_cases.json` in
`tests/test_secrets.py`: reading a static JSON file never touches
`deny_bypass.py`, so collection never depends on the module under test,
even before that module exists.

Every denial case is driven from the table by
`test_denial_case_is_denied_with_expected_rule_and_reason`
(AC-TEST-001); `test_every_registered_rule_has_a_denial_case`
(AC-TEST-005) is the hygiene check that keeps a rule added to the registry
later from silently going uncovered. The remaining tests each pin one
behavior the table cannot express on its own: the discrimination between
`git commit -n` and `git push -n` (Approach step 4), the two directions of
the injected `is_ignored` callable (AC-TEST-003), purity (AC-FUNC-008), and
the three Error Handling Contract fail-closed paths (AC-TEST-004).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import cast

import pytest
from gitfixtures import commit_text, generated_root, init_repo


def _hook_module_path() -> Path:
    return Path(__file__).resolve().parent.parent / ".claude" / "hooks" / "deny_bypass.py"


def _settings_path() -> Path:
    return Path(__file__).resolve().parent.parent / ".claude" / "settings.json"


def _import_deny_bypass() -> ModuleType:
    """Load `.claude/hooks/deny_bypass.py` from its file path.

    See the module docstring for why this is not `importlib.import_module`,
    and why the load itself is deferred into this function body rather than
    done once at module scope.

    Registered in `sys.modules` under its own name before `exec_module`
    runs, the same as a normal import would: `deny_bypass.py`'s
    `@dataclass` fields carry string annotations
    (`from __future__ import annotations`), and dataclass resolves those
    against `sys.modules[cls.__module__]`, so a module executed without
    ever being registered there fails at class-definition time with an
    `AttributeError` unrelated to any real defect in the hook.

    Raises:
        FileNotFoundError: if the hook module does not exist yet (the TDD
            RED gate's stashed state).
        ImportError: if a spec or loader cannot be built for the path.
    """
    path = _hook_module_path()
    spec = importlib.util.spec_from_file_location("deny_bypass", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot build an import spec for {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_cases() -> tuple[dict[str, object], ...]:
    path = Path(__file__).resolve().parent / "data" / "bypass_denial_cases.json"
    return tuple(json.loads(path.read_text(encoding="utf-8")))


_CASES: tuple[dict[str, object], ...] = _load_cases()
_CASE_IDS: list[str] = [f"{case['rule']}:{case['spelling']}" for case in _CASES]


def _always_ignored(_path: str) -> bool:
    """An `is_ignored` stand-in that treats every path as gitignored.

    Used by the denial-table test: only the `git-add-force-ignored` rule
    ever calls this, and its two table cases need the path they name to
    read as gitignored to prove the denial; every other rule ignores this
    argument entirely, so treating every path as ignored cannot cause a
    false denial anywhere else in the table.
    """
    return True


@pytest.mark.parametrize("case", _CASES, ids=_CASE_IDS)
def test_denial_case_is_denied_with_expected_rule_and_reason(case: dict[str, object]) -> None:
    """AC-TEST-001 / AC-TEST-002: every Section 4.6.1 pattern is denied by the rule it names."""
    deny_bypass = _import_deny_bypass()

    decision = deny_bypass.evaluate(str(case["command"]), _always_ignored)

    assert decision is not None, f"expected a denial for: {case['command']!r}"
    assert decision.rule == case["rule"]
    for substring in cast(list[object], case["reason_contains"]):
        assert str(substring) in decision.reason


def test_every_registered_rule_has_a_denial_case() -> None:
    """AC-TEST-005: a rule added to the registry without a table case fails here."""
    deny_bypass = _import_deny_bypass()

    rule_ids_in_table = {str(case["rule"]) for case in _CASES}
    registered_rule_ids = {rule.id for rule in deny_bypass.RULES}

    assert registered_rule_ids == rule_ids_in_table


def test_short_n_under_push_does_not_trigger_the_commit_rule() -> None:
    """Approach step 4: -n means dry run under push, not the commit skip-hooks alias."""
    deny_bypass = _import_deny_bypass()

    decision = deny_bypass.evaluate("git push -n origin main", _always_ignored)

    assert decision is None


def test_git_global_bare_flag_before_subcommand_is_skipped() -> None:
    """`_skip_git_global_options`: a bare global flag (`--no-pager`) is skipped too.

    `-C`/`-c`/`--git-dir=`/`--work-tree=` are already pinned by the
    dash-c-dir/git-dir-equals/work-tree-equals table cases; this pins the
    remaining branch, the value-free globals (`-p`, `--paginate`,
    `--no-pager`), which take a different code path (advance by one token,
    not two).
    """
    deny_bypass = _import_deny_bypass()

    decision = deny_bypass.evaluate(
        'git --no-pager commit --no-verify -m "release notes"', _always_ignored
    )

    assert decision is not None
    assert decision.rule == "git-commit-no-verify"


def test_git_with_only_global_options_and_no_subcommand_is_not_denied() -> None:
    """`_git_subcommand_and_rest` returns `None` for a `git` segment with no subcommand.

    `git -C .` (a real, if useless, invocation) exercises the branch where
    every argument is consumed as a global option and nothing is left to
    read as a subcommand; a rule keyed on a subcommand must not fire on it.
    """
    deny_bypass = _import_deny_bypass()

    decision = deny_bypass.evaluate("git -C .", _always_ignored)

    assert decision is None


def test_bare_git_with_no_arguments_is_not_denied() -> None:
    """`_skip_git_global_options` on an empty argument tuple returns empty, not an error."""
    deny_bypass = _import_deny_bypass()

    decision = deny_bypass.evaluate("git", _always_ignored)

    assert decision is None


def test_segments_splits_a_two_line_command_into_two_segments() -> None:
    """`segments`: a bare newline is a statement boundary, the same as `;`.

    Before this newline handling was added, `shlex` folded the newline
    into ordinary whitespace and the two lines collapsed into a single
    `Segment` whose program was `make`, so `git commit --no-verify` on the
    second line never reached any rule. Pinned directly against `segments`
    rather than only through `evaluate`, so a future regression fails here
    with the exact wrong segment count, not just a missing denial.
    """
    deny_bypass = _import_deny_bypass()

    parsed = deny_bypass.segments("make lint\ngit commit --no-verify -m x")

    assert len(parsed) == 2
    assert parsed[0].program == "make"
    assert parsed[1].program == "git"
    assert parsed[1].args == ("commit", "--no-verify", "-m", "x")


def test_segments_splits_on_a_bare_ampersand() -> None:
    """`segments`: a bare `&` background operator is a boundary, distinct from `&&`.

    `_CHAIN_BOUNDARY_RE` recognizes single-character `&` as well as the
    two-character `&&`; before this fix only `&&` was in the fixed
    operator set, so `echo hi & git commit --no-verify` folded into one
    segment whose program was `echo`.
    """
    deny_bypass = _import_deny_bypass()

    parsed = deny_bypass.segments("echo hi & git commit --no-verify -m x")

    assert len(parsed) == 2
    assert parsed[0].program == "echo"
    assert parsed[1].program == "git"


def test_segments_treats_a_merged_operator_and_newline_token_as_one_boundary() -> None:
    """`segments`: `&&` immediately followed by a newline is still one boundary, not two.

    `shlex`'s punctuation-aware tokenizer merges any run of adjacent
    punctuation characters into a single token, so `git commit -n &&\\ngit
    push` yields the merged token `"&&\\n"`, not separate `"&&"` and
    `"\\n"` tokens. A fixed-string membership test would miss this merged
    spelling entirely; `_CHAIN_BOUNDARY_RE`'s character-class match
    recognizes it as one boundary and still produces exactly two segments,
    not three (which a naive per-character split would produce) and not
    one (which the pre-fix code produced).
    """
    deny_bypass = _import_deny_bypass()

    parsed = deny_bypass.segments("git commit -n &&\ngit push")

    assert len(parsed) == 2
    assert parsed[0].program == "git"
    assert parsed[0].args == ("commit", "-n")
    assert parsed[1].program == "git"
    assert parsed[1].args == ("push",)


def test_segments_keeps_an_embedded_newline_inside_a_quoted_argument() -> None:
    """`segments`: a literal newline inside a quoted string is not a boundary.

    Removing `\\n` from `whitespace_split` handling must not disturb
    `shlex`'s own quote-awareness: a newline the user actually typed inside
    a commit message stays part of that one argument.
    """
    deny_bypass = _import_deny_bypass()

    parsed = deny_bypass.segments('git commit -m "line one\nline two" && git push')

    assert len(parsed) == 2
    assert parsed[0].args == ("commit", "-m", "line one\nline two")


def test_segments_treats_a_subshell_grouping_token_as_a_boundary() -> None:
    """`segments`: a leading `(` is a boundary, not part of the segment's program.

    Before `(` and `)` were added to `_CHAIN_BOUNDARY_RE`, `(git commit
    --no-verify -m x)` produced a single `Segment` whose `program` was the
    literal token `"("`, with `git`, `commit`, `--no-verify`, `-m`, `x` and
    `")"` all sliding into `args`, where no rule reads them. Pinned
    directly against `segments` so a regression fails with the exact wrong
    program, not just a missing denial several layers away.
    """
    deny_bypass = _import_deny_bypass()

    parsed = deny_bypass.segments("(git commit --no-verify -m x)")

    assert len(parsed) == 3
    assert parsed[0].program == ""
    assert parsed[1].program == "git"
    assert parsed[1].args == ("commit", "--no-verify", "-m", "x")
    assert parsed[2].program == ""


def test_segments_treats_a_brace_group_as_a_boundary() -> None:
    """`segments`: a brace group's `{` and `}` are boundaries too, the same as `( )`.

    A brace group is a valid alternative to a subshell for the identical
    evasion, so it has to be pinned separately: fixing only `(`/`)` and
    leaving `{`/`}` unhandled would still let `{ git commit --no-verify -m
    x; }` evade every rule. `;` and `}` are separate, space-delimited
    tokens here, so each is its own boundary and produces its own empty
    segment (unlike the merged `");"` token `_CHAIN_BOUNDARY_RE`'s own
    docstring describes for an unspaced subshell-then-semicolon spelling).
    """
    deny_bypass = _import_deny_bypass()

    parsed = deny_bypass.segments("{ git commit --no-verify -m x; }")

    assert len(parsed) == 4
    assert parsed[0].program == ""
    assert parsed[1].program == "git"
    assert parsed[1].args == ("commit", "--no-verify", "-m", "x")
    assert parsed[2].program == ""
    assert parsed[3].program == ""


def test_every_launcher_prefix_is_walked_past_before_the_program() -> None:
    """`_parse_segment`: every entry in `_LAUNCHER_PREFIXES` is skipped, not a few.

    Looped over the module's own `_LAUNCHER_PREFIXES` registry rather than a
    duplicated literal list, so a launcher word added to that set later
    without matching this behavior fails here automatically instead of
    silently going untested (the same reasoning as
    `test_every_registered_rule_has_a_denial_case`). Pins both `segments`
    directly (the exact parsed program) and `evaluate` (the actual denial),
    for every one of `env`, `sudo`, `command`, `nohup`, `time`, `doas`,
    `nice`, `ionice` and `setsid`.
    """
    deny_bypass = _import_deny_bypass()

    for launcher in sorted(deny_bypass._LAUNCHER_PREFIXES):
        command = f"{launcher} git commit --no-verify -m x"
        parsed = deny_bypass.segments(command)[0]
        assert parsed.program == "git", f"launcher {launcher!r} was not walked past"

        decision = deny_bypass.evaluate(command, _always_ignored)
        assert decision is not None, f"launcher {launcher!r} evaded the commit rule"
        assert decision.rule == "git-commit-no-verify"


def test_every_shell_reserved_word_is_walked_past_before_the_program() -> None:
    """`_parse_segment`: every entry in `_SHELL_RESERVED_WORDS` is skipped too.

    The reserved-word equivalent of the launcher test above, for the same
    reason: `if`, `then`, `elif`, `else`, `fi`, `do`, `done`, `while`,
    `until`, `case`, `esac`, `in`, `function` and `!`.
    """
    deny_bypass = _import_deny_bypass()

    for word in sorted(deny_bypass._SHELL_RESERVED_WORDS):
        command = f"{word} git commit --no-verify -m x"
        parsed = deny_bypass.segments(command)[0]
        assert parsed.program == "git", f"reserved word {word!r} was not walked past"

        decision = deny_bypass.evaluate(command, _always_ignored)
        assert decision is not None, f"reserved word {word!r} evaded the commit rule"
        assert decision.rule == "git-commit-no-verify"


def test_launcher_prefix_does_not_over_deny_an_ordinary_command() -> None:
    """A launcher ahead of an unrelated, non-denied command must still allow.

    Proves the launcher-prefix walk only changes which token is read as the
    program; it does not turn every launched command into a denial.
    """
    deny_bypass = _import_deny_bypass()

    decision = deny_bypass.evaluate("sudo git status", _always_ignored)

    assert decision is None


def test_reserved_word_does_not_over_deny_an_ordinary_command() -> None:
    """A reserved word ahead of an unrelated, non-denied command must still allow."""
    deny_bypass = _import_deny_bypass()

    decision = deny_bypass.evaluate("if true; then git status; fi", _always_ignored)

    assert decision is None


def test_launcher_then_assignment_then_program_is_parsed_correctly() -> None:
    """`_parse_segment`'s combined walk resolves a launcher-then-assignment order too.

    `sudo HUSKY=0 git commit -m y` is not itself one of the review's exact
    verified findings, but it is the same interleaving `env sudo git commit
    --no-verify` already tests in the other direction (launcher, launcher,
    program): this one is launcher, assignment, program, proving the single
    combined loop -- not two separate fixed-order passes -- is what resolves
    it.
    """
    deny_bypass = _import_deny_bypass()

    parsed = deny_bypass.segments("sudo HUSKY=0 git commit -m y")[0]
    assert parsed.assignments == ("HUSKY=0",)
    assert parsed.program == "git"

    decision = deny_bypass.evaluate("sudo HUSKY=0 git commit -m y", _always_ignored)
    assert decision is not None
    assert decision.rule == "env-husky-disable"


def test_program_name_reads_the_basename_of_a_path_prefixed_program() -> None:
    """`_program_name`: `/usr/bin/git` and `git` compare equal through this helper.

    Pinned directly against the helper, not only through `evaluate`, so a
    future regression that reintroduces a raw `segment.program` comparison
    somewhere fails with a clear basename mismatch rather than only a
    missing denial several layers away.
    """
    deny_bypass = _import_deny_bypass()
    segment = deny_bypass.segments("/usr/bin/git commit --no-verify")[0]

    assert deny_bypass._program_name(segment) == "git"


def test_program_name_does_not_match_a_program_that_merely_contains_the_name() -> None:
    """`_program_name`: basename comparison, not a substring match.

    `gitk` and `rmdir` must not be mistaken for `git` and `rm` just because
    one name contains a prefix of the other; `_program_name` returns the
    exact basename, so an equality check against `"git"`/`"rm"` still
    requires an exact match.
    """
    deny_bypass = _import_deny_bypass()

    gitk_segment = deny_bypass.segments("gitk --all")[0]
    rmdir_segment = deny_bypass.segments("rmdir build")[0]

    assert deny_bypass._program_name(gitk_segment) == "gitk"
    assert deny_bypass._program_name(rmdir_segment) == "rmdir"
    assert deny_bypass.evaluate("gitk --all", _always_ignored) is None
    assert deny_bypass.evaluate("rmdir build", _always_ignored) is None


def test_add_force_of_ignored_path_is_denied() -> None:
    """AC-TEST-003 (ignored direction), AC-FUNC-004."""
    deny_bypass = _import_deny_bypass()

    decision = deny_bypass.evaluate("git add -f shell.env", lambda path: path == "shell.env")

    assert decision is not None
    assert decision.rule == "git-add-force-ignored"
    assert "shell.env" in decision.reason


def test_add_force_of_tracked_path_is_not_denied() -> None:
    """AC-TEST-003 (not-ignored direction), AC-FUNC-004."""
    deny_bypass = _import_deny_bypass()

    decision = deny_bypass.evaluate("git add -f README.md", lambda _path: False)

    assert decision is None


def test_add_force_ignore_check_that_raises_denies() -> None:
    """Error Handling Contract: an ignore check that cannot run denies, not allows."""
    deny_bypass = _import_deny_bypass()

    def _raising(_path: str) -> bool:
        raise RuntimeError("git check-ignore is not available")

    decision = deny_bypass.evaluate("git add -f README.md", _raising)

    assert decision is not None
    assert decision.rule == "git-add-force-ignored"
    assert "README.md" in decision.reason
    assert "RuntimeError" in decision.reason


def test_untokenizable_command_denies() -> None:
    """Error Handling Contract: an unparseable command line denies, not allows."""
    deny_bypass = _import_deny_bypass()

    decision = deny_bypass.evaluate('git commit -m "unterminated', _always_ignored)

    assert decision is not None
    assert decision.rule == "unparseable-command"
    assert "tokeniz" in decision.reason.lower()
    assert "unterminated" in decision.reason


def test_evaluate_never_calls_is_ignored_for_a_command_with_no_force_add() -> None:
    """AC-FUNC-008: is_ignored is only ever consulted by the git-add-force rule.

    `git status` reaches every rule in the registry without matching any of
    them (unlike a denied command, which returns as soon as the first rule
    matches and so would never reach the later rules regardless of whether
    they call `is_ignored`), so this is the case that actually proves the
    other nine rules never touch the callable.
    """
    deny_bypass = _import_deny_bypass()
    calls: list[str] = []

    def _tracking(path: str) -> bool:
        calls.append(path)
        return True

    decision = deny_bypass.evaluate("git status", _tracking)

    assert decision is None
    assert calls == []


def test_main_denies_on_malformed_json_stdin(capsys: pytest.CaptureFixture[str]) -> None:
    """Error Handling Contract: invalid JSON on stdin fails closed."""
    deny_bypass = _import_deny_bypass()

    exit_code = deny_bypass.main(stdin_text="{not valid json", is_ignored=_always_ignored)

    assert exit_code != 0
    captured = capsys.readouterr()
    assert "ERROR" in captured.err


def test_main_denies_on_missing_command_field(capsys: pytest.CaptureFixture[str]) -> None:
    """Error Handling Contract: an event with no command to evaluate fails closed."""
    deny_bypass = _import_deny_bypass()

    exit_code = deny_bypass.main(
        stdin_text=json.dumps({"tool_name": "Bash", "tool_input": {}}),
        is_ignored=_always_ignored,
    )

    assert exit_code != 0
    captured = capsys.readouterr()
    assert "ERROR" in captured.err


def test_main_allows_ordinary_command() -> None:
    """`main` exits 0 for a command `evaluate` returns no decision for."""
    deny_bypass = _import_deny_bypass()

    exit_code = deny_bypass.main(
        stdin_text=json.dumps({"tool_name": "Bash", "tool_input": {"command": "git status"}}),
        is_ignored=_always_ignored,
    )

    assert exit_code == 0


def test_main_denies_bypass_command(capsys: pytest.CaptureFixture[str]) -> None:
    """`main` exits non-zero and names the rule on stderr for a denied command."""
    deny_bypass = _import_deny_bypass()

    exit_code = deny_bypass.main(
        stdin_text=json.dumps(
            {"tool_name": "Bash", "tool_input": {"command": 'git commit --no-verify -m "x"'}}
        ),
        is_ignored=_always_ignored,
    )

    assert exit_code != 0
    captured = capsys.readouterr()
    assert "git-commit-no-verify" in captured.err


def test_check_ignore_returns_true_for_a_gitignored_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_check_ignore` (the real `git check-ignore` boundary): the ignored direction.

    Every other test in this file injects a stand-in `is_ignored`; this one
    exercises `_check_ignore` itself against a real, disposable git
    repository built by `tests/gitfixtures.py`, so an inversion of the
    `returncode == 0` check this function makes is something the suite can
    actually catch.
    """
    deny_bypass = _import_deny_bypass()
    root = generated_root(tmp_path)
    init_repo(root)
    commit_text(root, ".gitignore", "secret.env\n", "add gitignore")
    (root / "secret.env").write_text("SECRET=1\n", encoding="utf-8")
    monkeypatch.chdir(root)

    assert deny_bypass._check_ignore("secret.env") is True


def test_check_ignore_returns_false_for_a_tracked_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_check_ignore`: the not-ignored direction, against a real, tracked path."""
    deny_bypass = _import_deny_bypass()
    root = generated_root(tmp_path)
    init_repo(root)
    commit_text(root, "README.md", "seed\n", "seed")
    monkeypatch.chdir(root)

    assert deny_bypass._check_ignore("README.md") is False


def test_check_ignore_raises_when_directory_is_not_a_work_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Error Handling Contract: an ignore check outside any work tree fails closed."""
    deny_bypass = _import_deny_bypass()
    outside = generated_root(tmp_path)
    monkeypatch.chdir(outside)

    with pytest.raises(deny_bypass.DenyBypassError, match="gitignored"):
        deny_bypass._check_ignore("shell.env")


def test_check_ignore_raises_when_git_is_not_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Error Handling Contract: an ignore check with no git binary on PATH fails closed."""
    deny_bypass = _import_deny_bypass()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", "")

    with pytest.raises(deny_bypass.DenyBypassError, match="git is not installed"):
        deny_bypass._check_ignore("shell.env")


def test_main_denies_forced_add_of_gitignored_path_with_default_is_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`main` with no injected `is_ignored`: a real forced add of a gitignored path denies.

    Every other `main` test above passes `is_ignored=_always_ignored`; this
    one calls `main` exactly as `__main__` does, so it proves the wiring
    from `main`'s default parameter through to the real `_check_ignore`
    subprocess, not just `_check_ignore` in isolation.
    """
    deny_bypass = _import_deny_bypass()
    root = generated_root(tmp_path)
    init_repo(root)
    commit_text(root, ".gitignore", "secret.env\n", "add gitignore")
    (root / "secret.env").write_text("SECRET=1\n", encoding="utf-8")
    monkeypatch.chdir(root)

    exit_code = deny_bypass.main(
        stdin_text=json.dumps(
            {"tool_name": "Bash", "tool_input": {"command": "git add -f secret.env"}}
        )
    )

    assert exit_code != 0
    captured = capsys.readouterr()
    assert "git-add-force-ignored" in captured.err


def test_git_add_without_force_flag_is_not_denied() -> None:
    """Allowed direction of `_match_add_force_ignored`: no `-f`/`--force`, no denial.

    Proves the unforced branch (line 305) is reachable and returns an
    allow, not merely that a forced add of an ignored path is denied.
    """
    deny_bypass = _import_deny_bypass()

    decision = deny_bypass.evaluate("git add shell.env", _always_ignored)

    assert decision is None


def test_rm_of_ordinary_path_outside_git_hooks_is_not_denied() -> None:
    """Allowed direction of `rm-git-hooks`: an ordinary `rm` outside `.git/hooks`."""
    deny_bypass = _import_deny_bypass()

    decision = deny_bypass.evaluate("rm build/output.tmp", _always_ignored)

    assert decision is None


def test_chmod_of_ordinary_path_outside_git_hooks_is_not_denied() -> None:
    """Allowed direction of `chmod-git-hooks`: an ordinary `chmod` outside `.git/hooks`."""
    deny_bypass = _import_deny_bypass()

    decision = deny_bypass.evaluate("chmod +x scripts/build.sh", _always_ignored)

    assert decision is None


def test_main_denies_on_non_object_json_payload(capsys: pytest.CaptureFixture[str]) -> None:
    """Error Handling Contract: valid JSON that is not an object fails closed."""
    deny_bypass = _import_deny_bypass()

    exit_code = deny_bypass.main(
        stdin_text=json.dumps(["not", "an", "object"]), is_ignored=_always_ignored
    )

    assert exit_code != 0
    captured = capsys.readouterr()
    assert "ERROR" in captured.err


def test_main_denies_on_non_string_command_value(capsys: pytest.CaptureFixture[str]) -> None:
    """Error Handling Contract: a non-string `tool_input.command` fails closed."""
    deny_bypass = _import_deny_bypass()

    exit_code = deny_bypass.main(
        stdin_text=json.dumps({"tool_name": "Bash", "tool_input": {"command": 123}}),
        is_ignored=_always_ignored,
    )

    assert exit_code != 0
    captured = capsys.readouterr()
    assert "ERROR" in captured.err


def test_settings_json_registers_hook_on_bash_matcher() -> None:
    """AC-TEST-006 / AC-FUNC-009: `.claude/settings.json` registers the hook on `Bash`."""
    settings = json.loads(_settings_path().read_text(encoding="utf-8"))

    bash_entries = [
        entry for entry in settings["hooks"]["PreToolUse"] if entry["matcher"] == "Bash"
    ]
    assert bash_entries, "no PreToolUse hook registered on the Bash matcher"

    commands = [hook["command"] for entry in bash_entries for hook in entry["hooks"]]
    assert any("deny_bypass.py" in command for command in commands)
    assert any("CLAUDE_PROJECT_DIR" in command for command in commands)
