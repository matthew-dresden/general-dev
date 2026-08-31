"""Contract tests for five roots the `.gitignore` allowlist re-includes.

`.gitignore` line 12 is `/*`, an allowlist root (spec Section 1.8): every
path is ignored unless re-included by name below it. The allowlist
re-includes more paths than these; this module pins five of them --
`.claude/`, `provider/`, `remote-instances/`, `pyproject.toml` and `tests/`
-- as genuinely trackable, and pins the negative case a broader allowlist
entry must never accidentally trip: `.claude/settings.local.json` and every
entry of `repo.PRIVATE_FILES` must stay ignored.

Every assertion goes through `git check-ignore` and the pattern (or exit
code, for a root queried in directory form) it reports, never a text match
on `.gitignore`: a re-include whose parent directory is still excluded has
no effect and produces no error, so the file would read as if it worked
even though nothing changed. `tests/gitignore_check.py` is the single
implementation of that query, and `directory_form_required` there is the
single decision of which form a given root needs -- computed from the
root's own presence in the checkout, never a name hard-coded here -- and
this module is its only caller. `tests/test_project_config.py` shares only
the repository-root resolution from `tests/gitignore_check.py`; it reads
`pyproject.toml` from that root and does not itself call
`git check-ignore`. The repository root is resolved through
`devcontainer_config.repo.find_root` before any `git check-ignore` call, so
a missing `git` binary or a working directory outside any repository
surfaces as a `RepoError` naming the cause, rather than as a
`git check-ignore` exit code that only looks like a false pass.

One test is a deliberate exception to "never a text match": the ignore
*behavior* of `!/provider/` and `!/remote-instances/` is always verified
through `git check-ignore`, never through their comment, but the comment's
own *content* -- whether it still claims a point-in-time fact about
whether either tree currently exists, rather than the durable fact that
both are directory-only re-includes -- has no other way to be pinned than
reading `.gitignore` itself (AC-DOC-002, AC-DOC-003). The forbidden-phrase
table that test drives, `STALE_EXISTENCE_PHRASES`, lives in
`tests/data/gitignore_stale_existence_phrases.py`, not in this module: this
module is itself one of the three files AC-DOC-003's own grep scans, so a
copy of the phrases here would make that criterion permanently unsatisfiable
against its own enforcement mechanism.
"""

from __future__ import annotations

from pathlib import Path

import gitignore_check
import pytest
from data.gitignore_stale_existence_phrases import STALE_EXISTENCE_PHRASES
from devcontainer_config import repo
from gitfixtures import generated_root, init_repo, stage_text
from gitignore_check import GitIgnoreQueryError, check_ignore

NOT_IGNORED_PATHS: tuple[str, ...] = (
    ".claude",
    "provider",
    "remote-instances",
    "pyproject.toml",
    "tests",
)

# A path no `gitignore_check.DIRECTORY_ONLY_REINCLUDES` member can ever equal
# (it is not a valid `.gitignore` root-level name), used only by
# `_directory_only_reinclude_roots`'s guarded fallback below.
_MISSING_REINCLUDES_SENTINEL = "__gitignore_check_missing_DIRECTORY_ONLY_REINCLUDES__"


def _directory_form_required(path: str) -> bool:
    """`gitignore_check.directory_form_required`, resolved by attribute lookup.

    Looked up on the module at call time rather than imported at module
    scope, so this test module still collects against a `gitignore_check`
    that has not yet defined the function; the assertion below, not a bare
    `ImportError` during collection, is then what reports the gap.
    """
    form_required = getattr(gitignore_check, "directory_form_required", None)
    assert form_required is not None, (
        "gitignore_check.directory_form_required is not defined; check_ignore's "
        "directory-form decision must be computed from the queried path's live "
        "presence under repo_root(), not a frozen membership list"
    )
    return form_required(path)


def _directory_only_reinclude_roots() -> tuple[str, ...]:
    """`sorted(gitignore_check.DIRECTORY_ONLY_REINCLUDES)`, the single surviving
    declaration of which `NOT_IGNORED_PATHS` roots are directory-only
    re-includes (AC-FUNC-002) -- no second, hand-maintained copy of that
    membership lives in this module.

    Resolved by attribute lookup at collection time, the same guard
    `_directory_form_required` uses for `directory_form_required` itself: if
    `gitignore_check` has not yet defined `DIRECTORY_ONLY_REINCLUDES`, this
    returns a single sentinel case instead of raising `AttributeError` during
    collection for the whole module, so `test_directory_form_selected_only_when_root_is_absent`'s
    own assertion, not a bare collection error, is what reports the gap.
    """
    reincludes = getattr(gitignore_check, "DIRECTORY_ONLY_REINCLUDES", None)
    if reincludes is None:
        return (_MISSING_REINCLUDES_SENTINEL,)
    return tuple(sorted(reincludes))


@pytest.mark.parametrize("path", NOT_IGNORED_PATHS)
def test_allowlisted_root_is_not_ignored(path: str) -> None:
    """Each of the five tracked roots is re-included, so check-ignore reports it not ignored.

    `directory_form_required` decides the query form per call from `path`'s
    live presence in the checkout, so this assertion is true regardless of
    whether `provider/` or `remote-instances/` has landed yet (AC-FUNC-003).
    """
    result = check_ignore(path, as_directory=_directory_form_required(path))
    assert not result.ignored, (
        f"{path!r} expected git check-ignore to report not ignored; evidence={result.evidence!r}"
    )


@pytest.mark.parametrize("root_name", _directory_only_reinclude_roots())
def test_directory_form_selected_only_when_root_is_absent(
    root_name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`directory_form_required` tracks a root's own on-disk presence, not a
    frozen list and not git's index.

    Builds a disposable git repository under `tmp_path`, seeded with a copy
    of this checkout's own `.gitignore`, and drives `root_name` through three
    states a `.gitignore`-tracked root can be in: absent from the working
    tree, present but empty and untracked, and present with a tracked file.
    `gitignore_check.repo_root` is monkeypatched to the scratch repository so
    `directory_form_required` and `check_ignore` (which both resolve the
    checkout through that same function) answer for the scratch tree instead
    of this one, and no state ever reads the real `provider/` directory --
    so this test's result never depends on which E5 tasks have landed
    (AC-TEST-001).
    """
    assert root_name != _MISSING_REINCLUDES_SENTINEL, (
        "gitignore_check.DIRECTORY_ONLY_REINCLUDES is not defined; the set of "
        "directory-only re-includes must be declared exactly once, on "
        "gitignore_check, not duplicated in this test module"
    )
    real_gitignore = (gitignore_check.repo_root() / ".gitignore").read_text(encoding="utf-8")
    scratch_root = generated_root(tmp_path)
    init_repo(scratch_root)
    stage_text(scratch_root, ".gitignore", real_gitignore)
    monkeypatch.setattr(gitignore_check, "repo_root", lambda: scratch_root)

    # Absent state: root_name has no entry on disk for git to stat.
    assert _directory_form_required(root_name) is True
    absent_result = check_ignore(root_name, as_directory=True)
    assert not absent_result.ignored, (
        f"{root_name!r} absent: expected not ignored; evidence={absent_result.evidence!r}"
    )

    # Present-but-untracked state: root_name has an entry on disk (git can
    # stat it directly) but nothing inside it is tracked or staged.
    # directory_form_required keys on on-disk existence (repo_root() / path
    # .exists()), never on git's index, so this state must already read as
    # present -- the boundary a regression that swapped in an index lookup
    # would otherwise cross undetected.
    (scratch_root / root_name).mkdir()
    assert _directory_form_required(root_name) is False, (
        f"{root_name!r} present but untracked and empty: directory_form_required "
        "must key on on-disk existence, not git tracking"
    )
    untracked_result = check_ignore(root_name, as_directory=False)
    assert not untracked_result.ignored, (
        f"{root_name!r} present but untracked and empty: expected not ignored; "
        f"evidence={untracked_result.evidence!r}"
    )

    # Present-and-tracked state: root_name holds a tracked file git can stat directly.
    stage_text(scratch_root, f"{root_name}/tracked-file.txt", "content\n")
    assert _directory_form_required(root_name) is False
    present_result = check_ignore(root_name, as_directory=False)
    assert not present_result.ignored, (
        f"{root_name!r} present: expected not ignored; evidence={present_result.evidence!r}"
    )


STILL_IGNORED_PATHS: tuple[str, ...] = (".claude/settings.local.json", *repo.PRIVATE_FILES)


@pytest.mark.parametrize("path", STILL_IGNORED_PATHS)
def test_still_ignored_path_remains_ignored(path: str) -> None:
    """The wider allowlist must never expose local settings or a private file."""
    result = check_ignore(path)
    assert result.ignored, (
        f"{path!r} expected git check-ignore to report ignored; evidence={result.evidence!r}"
    )


def test_check_ignore_raises_when_git_itself_fails() -> None:
    """A pathspec outside the repository is a real git fatal error, not a match.

    `git check-ignore --no-index -v ../outside-the-repository`, run from
    this repository's root, exits 128 with a `fatal:` message on stderr
    (git 2.50.1), not 0 or 1, so `check_ignore` must raise rather than
    reading a fatal error as either an ignored or a not-ignored result.
    """
    with pytest.raises(GitIgnoreQueryError, match="git check-ignore exited"):
        check_ignore("../outside-the-repository")


def test_check_ignore_raises_when_no_rule_matches_a_real_path_query() -> None:
    """A real-path query that matches nothing must raise, not guess.

    `tests/gitignore_check.py` is a tracked file inside the `/tests/`
    re-include with no `.gitignore` rule of its own naming it, so
    `git check-ignore --no-index -v tests/gitignore_check.py` genuinely
    matches nothing (confirmed on git 2.50.1: exit 1, empty stdout) --
    `/*` only governs root-level entries, not paths nested inside an
    already re-included directory. `check_ignore` must raise here rather
    than defaulting to "not ignored", since a query that matches nothing
    at all is exactly the failure mode the trailing-slash defect this
    module fixes could otherwise hide.
    """
    with pytest.raises(GitIgnoreQueryError, match="no .gitignore rule matched"):
        check_ignore("tests/gitignore_check.py")


def test_gitignore_reincludes_comment_states_no_existence_claim() -> None:
    """`.gitignore`'s comment above `!/provider/` and `!/remote-instances/` states a
    durable rationale for allowlisting the two trees ahead of their first
    commit, never a point-in-time claim about whether either currently
    exists in the checkout (AC-DOC-002).

    Reading `.gitignore`'s own text is the one assertion in this module
    that is not a `git check-ignore` result -- see the module docstring for
    why the comment's *content*, unlike the allowlist's ignore *behavior*,
    has no other way to be pinned (AC-DOC-003).
    """
    gitignore_text = (gitignore_check.repo_root() / ".gitignore").read_text(encoding="utf-8")
    for phrase in STALE_EXISTENCE_PHRASES:
        assert phrase not in gitignore_text, (
            f".gitignore still contains the stale existence claim {phrase!r} in the comment "
            "above !/provider/ and !/remote-instances/; state why the trees are allowlisted "
            "ahead of their first commit instead of whether either currently exists"
        )
