"""Shared `git check-ignore` and repository-root helpers for the tests that
pin the `.gitignore` allowlist (`.gitignore` line 12 `/*`, spec Section 1.8).

`tests/test_gitignore_allowlist.py` asks git, through `git check-ignore`,
whether a root-level path is excluded by that allowlist; `check_ignore`
below is the single implementation of that query, so no other test module
can reintroduce a divergent copy.

Two query forms exist, chosen per path by the `as_directory` argument, and
they are not interchangeable:

* The real-path form (`as_directory=False`, the default) queries `path`
  exactly as given -- the same pathspec `git add path` would use. This is
  correct for every path this module queries, including for paths that do
  not exist in the checkout (a private file such as
  `.claude/settings.local.json` is queried this way too) and including the
  two directory roots described below once either has an entry on disk;
  the one exception is a directory-only re-include with nothing on disk
  for git to stat, which needs the directory form below instead. Appending
  a trailing slash to a query for a path that is, or should be, a file is
  a defect, not a convenience: a trailing slash makes directory-only
  `.gitignore` patterns eligible to match a query that a real `git add` on
  that file would never present to them.
  Measured on git 2.50.1: changing the ignore entry
  `.claude/settings.local.json` to `.claude/settings.local.json/` (a
  directory-only pattern) leaves a real `git add .claude/settings.local.json`
  free to succeed, because the mutated pattern no longer applies to a
  file -- but a query for `.claude/settings.local.json/` still matches that
  same mutated pattern, since both are now directory-shaped, and reports
  the file ignored regardless. Only a query in the file's own bare form
  avoids that hole.
* The directory form (`as_directory=True`, a trailing slash appended to
  the query) is required whenever the queried path is a directory-only
  re-include (`!/provider/`, `!/remote-instances/`, spec Sections 5.6,
  5.7) with no entry on disk for git to stat and classify as a directory.
  `directory_form_required` below decides this per call from the path's
  own presence under `repo_root()`, never from a record of which of these
  trees happened to be missing when this module was written, so its
  answer changes on its own the moment either tree's first commit lands.
  Without the slash, a query for a directory-only re-include with nothing
  on disk falls through straight to the `/*` root and reports a false
  ignored.

`repo_root` is also shared with `tests/test_project_config.py`, which reads
`pyproject.toml` from the checkout root but does not itself run
`git check-ignore`, so both modules resolve the repository root the same
way -- through `devcontainer_config.repo.find_root` -- rather than one of
them keeping an independent copy of that resolution.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from devcontainer_config import repo


class GitIgnoreQueryError(RuntimeError):
    """Raised when a real-path `check_ignore` query cannot be answered.

    Real-path queries this module makes fall into two groups. A root-level
    path (`.claude`, `pyproject.toml`, `tests`) is decided by `.gitignore`
    line 12's `/*` rule at minimum. A nested private file
    (`.claude/settings.local.json` and `.devcontainer/aws-profile-map.json`,
    each listed by its own explicit entry in `.gitignore`'s "Per-developer
    identity and secrets" block) is decided by that entry instead, since
    `/*` only governs root-level paths, not paths nested inside an already
    re-included directory.
    Either way, a query that matches no rule at all means the query itself
    is broken (for example, a path queried in directory form when it
    should have been queried in its real, bare form, or a nested path with
    no entry of its own deciding it) rather than a genuine "not ignored"
    result. Raising here, instead of guessing, is what keeps that class of
    defect from silently reading as a pass.
    """


@dataclass(frozen=True)
class CheckIgnoreResult:
    """Whether `.gitignore` ignores a path, and the evidence for the answer.

    `evidence` is the `.gitignore` pattern that decided the answer for a
    real-path query, or the raw `git check-ignore` stdout for a directory
    query (see `check_ignore`'s docstring for why a directory query cannot
    always name a deciding pattern). Callers attach it to failure messages
    so a failing assertion names the rule that matched, or its absence,
    instead of reporting a bare boolean mismatch.
    """

    ignored: bool
    evidence: str


def repo_root() -> Path:
    """The checkout root, resolved through `find_root` rather than assumed.

    Resolving through `devcontainer_config.repo.find_root` is what turns a
    missing `git` binary or a working directory outside any repository into
    a `RepoError` naming the cause, instead of a `git check-ignore` exit
    code that is non-zero for a reason unrelated to ignoring and would
    otherwise read as a false pass.
    """
    return repo.find_root(Path(__file__).resolve().parent)


# Root-level `.gitignore` re-includes that name a directory, never a file.
# Durable, unlike a record of which of these trees currently holds
# content: `!/provider/` and `!/remote-instances/` (spec Sections 5.6, 5.7)
# are directory-only re-includes by construction, a fact fixed by their
# `.gitignore` entry that the working tree's own contents can never change.
# `directory_form_required` combines this declaration with the queried
# path's live presence under `repo_root()` to decide, per call, whether
# that path's `check_ignore` query needs the trailing slash.
DIRECTORY_ONLY_REINCLUDES: frozenset[str] = frozenset({"provider", "remote-instances"})


def directory_form_required(path: str) -> bool:
    """Whether `check_ignore(path, as_directory=...)` must use the directory form.

    True exactly when `path` is one of `DIRECTORY_ONLY_REINCLUDES` and has
    no entry on disk under `repo_root()` -- the state in which git cannot
    stat it to tell `check_ignore` it is a directory, so the query must say
    so itself with a trailing slash. False for every other path, and False
    once a `DIRECTORY_ONLY_REINCLUDES` member is present on disk, because
    the real-path form already works once git can stat the path directly.
    Recomputed at every call from the live checkout, so callers never pass
    a literal boolean sourced from a hand-maintained membership list.
    """
    return path in DIRECTORY_ONLY_REINCLUDES and not (repo_root() / path).exists()


def _run_check_ignore(query: str, *, root: Path) -> subprocess.CompletedProcess[str]:
    """`git check-ignore --no-index -v <query>`, run from `root`.

    `--no-index` is required, not optional. Without it, git skips exclude
    evaluation entirely for any path already tracked in the index and
    prints nothing for it, unconditionally, regardless of what `.gitignore`
    says. `.claude/` and `tests/` both contain tracked files in this
    repository (confirmed with `git ls-files`), so a query without
    `--no-index` would report them "not ignored" even with their re-include
    entry deleted from `.gitignore` -- a case that could never fail.
    """
    return subprocess.run(
        ["git", "check-ignore", "--no-index", "-v", query],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )


def check_ignore(path: str, *, as_directory: bool = False) -> CheckIgnoreResult:
    """Whether `.gitignore` ignores `path`. See the module docstring for
    when `as_directory` must be set.

    Real-path queries (`as_directory=False`) are read from the matched
    pattern, not the exit code: under `-v`, git prints the last matching
    pattern whether it excludes or (via a leading `!`) re-includes the
    path, and exits 0 for either kind of match, so the exit code alone
    cannot tell them apart. A leading `!` on the printed pattern means
    re-included, i.e. not ignored.

    Directory queries (`as_directory=True`) are read from the exit code,
    because they carry their own quirk that makes the pattern
    unavailable: measured on git 2.50.1, when the sole matching pattern
    for a slash-qualified query is a negation, git reports no match at all
    (confirmed with `--non-matching`, which prints an empty pattern field)
    rather than printing it, the same output as when nothing matches at
    all. So for a directory query, exit 0 means a genuine, non-negated
    exclude pattern applied (ignored), and exit 1 covers both "nothing
    matched" and "the only match was a negation" (both mean not ignored).

    Raises:
        GitIgnoreQueryError: if git itself failed (an exit code other than
            0 or 1), or if a real-path query matched no rule at all.
    """
    root = repo_root()
    query = f"{path}/" if as_directory else path
    result = _run_check_ignore(query, root=root)
    if result.returncode not in (0, 1):
        raise GitIgnoreQueryError(
            f"ERROR: git check-ignore exited {result.returncode} for {path!r}\n"
            f"Query: {query!r} from {root}. Stderr: {result.stderr.strip()}\n"
            "Expected exit 0 or 1 (a matched or unmatched pathspec); any "
            "other code means git itself failed, most likely because the "
            "query ran outside a git repository."
        )
    if as_directory:
        return CheckIgnoreResult(
            ignored=result.returncode == 0,
            evidence=result.stdout.strip(),
        )
    matched = result.stdout.strip()
    if not matched:
        raise GitIgnoreQueryError(
            f"ERROR: no .gitignore rule matched {path!r}\n"
            f"Query: {query!r} from {root}. Expected a root-level path to "
            "match at least .gitignore line 12's /* allowlist rule, and a "
            "nested private file to match its own explicit entry.\n"
            "Check that the path is spelled correctly, that it does not "
            "need as_directory=True instead, or that the .gitignore entry "
            "that used to decide this path was removed or narrowed to a "
            "directory-only pattern."
        )
    source_and_pattern, _, _ = matched.partition("\t")
    _, _, pattern = source_and_pattern.rpartition(":")
    return CheckIgnoreResult(ignored=not pattern.startswith("!"), evidence=pattern)
