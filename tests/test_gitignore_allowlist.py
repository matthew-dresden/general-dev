"""Contract tests for five roots the `.gitignore` allowlist re-includes.

`.gitignore` line 12 is `/*`, an allowlist root (spec Section 1.8): every
path is ignored unless re-included by name below it. The allowlist
re-includes more paths than these; this module pins five of them --
`.claude/`, `provider/`, `remote-instances/`, `pyproject.toml` and `tests/`
-- as genuinely trackable, and pins the negative case a broader allowlist
entry must never accidentally trip: `.claude/settings.local.json` and every
entry of `repo.PRIVATE_FILES` must stay ignored.

Every assertion goes through `git check-ignore` and the pattern (or exit
code, for the two roots queried in directory form) it reports, never a
text match on `.gitignore`: a re-include whose parent directory is still
excluded has no effect and produces no error, so the file would read as if
it worked even though nothing changed. `tests/gitignore_check.py` is the
single implementation of that query (see its docstring for when a path
must be queried in directory form instead of its real, bare form), and
this module is its only caller. `tests/test_project_config.py` shares only
the repository-root resolution from `tests/gitignore_check.py`; it reads
`pyproject.toml` from that root and does not itself call
`git check-ignore`. The repository root is resolved through
`devcontainer_config.repo.find_root` before any `git check-ignore` call, so
a missing `git` binary or a working directory outside any repository
surfaces as a `RepoError` naming the cause, rather than as a
`git check-ignore` exit code that only looks like a false pass.
"""

from __future__ import annotations

import pytest
from devcontainer_config import repo
from gitignore_check import GitIgnoreQueryError, check_ignore

# provider/ and remote-instances/ are the only roots this module queries
# that do not yet exist in the checkout (E5, spec Sections 5.6/5.7, on
# hold): they must be queried in directory form. Every other path below is
# queried in its real, bare form; see gitignore_check.check_ignore's
# docstring for why the two forms are not interchangeable.
DIRECTORY_ROOTS_PENDING_CONTENT: frozenset[str] = frozenset({"provider", "remote-instances"})

NOT_IGNORED_PATHS: tuple[str, ...] = (
    ".claude",
    "provider",
    "remote-instances",
    "pyproject.toml",
    "tests",
)


@pytest.mark.parametrize("path", NOT_IGNORED_PATHS)
def test_allowlisted_root_is_not_ignored(path: str) -> None:
    """Each of the five tracked roots is re-included, so check-ignore reports it not ignored."""
    result = check_ignore(path, as_directory=path in DIRECTORY_ROOTS_PENDING_CONTENT)
    assert not result.ignored, (
        f"{path!r} expected git check-ignore to report not ignored; evidence={result.evidence!r}"
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
