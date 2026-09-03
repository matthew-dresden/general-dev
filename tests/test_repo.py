"""Tests for devcontainer_config.repo.

Every fixture tree here is built under tmp_path with a root directory name
generated per test, so no assertion depends on the name or layout of the real
checkout. `find_root` is exercised against real git repositories created by
subprocess calls to the actual `git` binary, not a mocked filesystem, since
its whole job is answering a question only git can answer correctly.

The `devcontainer_config` import is deferred into function bodies (the
helpers below) instead of done once at module scope. The TDD RED gate stashes
this unit's production-source files and re-runs a single named test node; a
module-level `from devcontainer_config.repo import ...` would make the whole
file fail to COLLECT in that state (ModuleNotFoundError raised while
importing the test module), which pytest reports as exit code 2 with no test
outcome recorded, not a genuine test failure. Deferring the import lets
collection succeed either way, so each test instead fails for the real
reason -- the module is missing -- rather than erroring out during
collection.

`_PARAMETRIZE_PRIVATE_FILES` freezes `PRIVATE_FILES` exactly once, at
collection time, into a module-scope constant. Both parametrize decorators
below consume that frozen constant instead of calling
`_private_files_for_parametrize` again, and
`test_parametrize_source_is_the_real_module` asserts the frozen constant
against `repo.PRIVATE_FILES` read again at run time. Because the two sides of
that assertion are captured at different times, a real divergence between the
parametrize source and the live module is something the assertion can
detect. Comparing two same-time reads of the same cached module object
would not be: both reads always agree with each other regardless of what
PRIVATE_FILES actually holds, so the frozen constant is what makes the
comparison meaningful.

`generated_root`, `init_repo` and `commit_text` are imported from the shared
`tests/gitfixtures.py` module rather than redefined here: every fixture
repository in this file, whether or not the test goes on to commit, is built
by the shared helper, so exactly one definition of each primitive exists
across the test suite.
"""

from __future__ import annotations

import importlib
import re
import subprocess
import uuid
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest
from gitfixtures import commit_text, generated_root, init_repo


def _import_repo() -> ModuleType:
    """Import devcontainer_config.repo from inside a function body.

    Called by every test below instead of a module-level import, so a
    missing package fails only the test that calls this (pytest exit 1,
    reported FAILED) rather than failing collection for the whole file
    (pytest exit 2). See the module docstring for why this matters to the
    TDD RED gate.
    """
    return importlib.import_module("devcontainer_config.repo")


def _private_files_for_parametrize() -> tuple[str, ...]:
    """PRIVATE_FILES sourced from the real module, for parametrize decorators.

    Decorator arguments run at collection time (module import), so this
    cannot let a ModuleNotFoundError caused by devcontainer_config itself
    being absent propagate -- that would fail collection for the whole file
    exactly like a module-level import would. Only that specific case is
    swallowed: the except clause below re-raises unless the missing module
    name is devcontainer_config or devcontainer_config.repo, so a
    ModuleNotFoundError raised from code inside repo.py itself (a real bug,
    not the RED-gate's module-absent state) still surfaces instead of being
    absorbed here. On the narrow case it does catch, this falls back to a
    single placeholder case; the parametrized test body then calls
    `_import_repo` itself and fails for the real reason, module not found,
    instead of a parametrize-time collection error.

    Called exactly once, at collection time, to build the module-scope
    constant `_PARAMETRIZE_PRIVATE_FILES` below. That constant, not a fresh
    call to this function, is what the parametrize decorators consume and
    what `test_parametrize_source_is_the_real_module` compares against
    `repo.PRIVATE_FILES` read again at run time -- see the module docstring
    for why the constant has to be frozen for that comparison to be able to
    fail.
    """
    try:
        return tuple(_import_repo().PRIVATE_FILES)
    except ModuleNotFoundError as exc:
        if exc.name not in ("devcontainer_config", "devcontainer_config.repo"):
            raise
        return ("<devcontainer_config unavailable>",)


_PARAMETRIZE_PRIVATE_FILES: tuple[str, ...] = _private_files_for_parametrize()


def test_parametrize_source_is_the_real_module() -> None:
    repo = _import_repo()
    assert _PARAMETRIZE_PRIVATE_FILES == tuple(repo.PRIVATE_FILES)


_PRESENT_SELECTORS: dict[str, Callable[[tuple[str, ...]], tuple[str, ...]]] = {
    "all-present": lambda examples: examples,
    "some-present": lambda examples: examples[:1],
    "none-present": lambda examples: (),
}


@pytest.mark.parametrize("relative", _PARAMETRIZE_PRIVATE_FILES)
def test_example_for_appends_suffix(relative: str) -> None:
    repo = _import_repo()
    assert repo.example_for(relative) == f"{relative}.example"


@pytest.mark.parametrize("relative", _PARAMETRIZE_PRIVATE_FILES)
def test_private_paths_returns_absolute_path_per_entry(tmp_path: Path, relative: str) -> None:
    repo = _import_repo()
    root = generated_root(tmp_path)

    paths = repo.private_paths(root)

    assert paths[relative] == root / relative
    assert paths[relative].is_absolute()


def test_private_paths_covers_every_private_file(tmp_path: Path) -> None:
    repo = _import_repo()
    root = generated_root(tmp_path)

    paths = repo.private_paths(root)

    assert set(paths) == set(repo.PRIVATE_FILES)


def test_workspace_name_is_generated_root_basename(tmp_path: Path) -> None:
    repo = _import_repo()
    root = generated_root(tmp_path)

    assert repo.workspace_name(root) == root.name


def test_container_workspace_derives_from_generated_root(tmp_path: Path) -> None:
    repo = _import_repo()
    root = generated_root(tmp_path)

    assert repo.container_workspace(root) == f"/workspaces/{root.name}"


def test_container_workspace_accepts_explicit_workspaces_root(tmp_path: Path) -> None:
    repo = _import_repo()
    root = generated_root(tmp_path)
    custom_root = f"/custom-{uuid.uuid4().hex}"

    assert repo.container_workspace(root, custom_root) == f"{custom_root}/{root.name}"


def test_find_root_resolves_inside_a_worktree(tmp_path: Path) -> None:
    repo = _import_repo()
    primary = generated_root(tmp_path)
    init_repo(primary)
    commit_text(primary, "README.md", "seed\n", "seed")

    worktree = tmp_path / f"worktree-{uuid.uuid4().hex}"
    subprocess.run(
        ["git", "-C", str(primary), "worktree", "add", str(worktree)],
        check=True,
        capture_output=True,
    )
    nested = worktree / "nested"
    nested.mkdir()

    assert (worktree / ".git").is_file()
    assert repo.find_root(nested) == worktree.resolve()


def test_find_root_raises_when_git_is_not_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _import_repo()
    monkeypatch.setenv("PATH", "")

    with pytest.raises(repo.RepoError, match="git is not installed"):
        repo.find_root(tmp_path)


def test_find_root_raises_when_start_is_outside_any_repository(tmp_path: Path) -> None:
    repo = _import_repo()
    outside = generated_root(tmp_path)

    with pytest.raises(repo.RepoError, match=re.escape(str(outside))):
        repo.find_root(outside)


def _set_origin(root: Path, url: str) -> None:
    """Configure `root`'s `remote.origin.url` to `url`, the way `repo_slug` reads it."""
    subprocess.run(
        ["git", "-C", str(root), "remote", "add", "origin", url],
        check=True,
        capture_output=True,
    )


@pytest.mark.parametrize(
    ("url", "expected_slug"),
    [
        pytest.param("https://host/org/general-dev.git", "general-dev", id="https-with-dot-git"),
        pytest.param("https://host/org/general-dev", "general-dev", id="https-without-dot-git"),
        pytest.param("git@host:org/general-dev.git", "general-dev", id="ssh-form"),
    ],
)
def test_repo_slug_derives_from_git_remote(tmp_path: Path, url: str, expected_slug: str) -> None:
    """AC-FUNC-001 / AC-TEST-001: the same basename-plus-trimsuffix transform root.hcl applies."""
    repo = _import_repo()
    root = generated_root(tmp_path)
    init_repo(root)
    _set_origin(root, url)

    assert repo.repo_slug(root) == expected_slug


def test_repo_slug_raises_when_git_is_not_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-FUNC-003 / AC-TEST-002: the git binary is not on PATH."""
    repo = _import_repo()
    monkeypatch.setenv("PATH", "")

    with pytest.raises(repo.RepoError, match="git is not installed"):
        repo.repo_slug(tmp_path)


def test_repo_slug_raises_when_no_origin_is_configured(tmp_path: Path) -> None:
    """AC-FUNC-003 / AC-TEST-002: a repository with no remote.origin.url configured."""
    repo = _import_repo()
    root = generated_root(tmp_path)
    init_repo(root)

    with pytest.raises(repo.RepoError, match="no remote.origin.url is configured"):
        repo.repo_slug(root)


def test_repo_slug_raises_when_remote_url_has_no_final_path_segment(tmp_path: Path) -> None:
    """AC-FUNC-003 / AC-TEST-002: a configured URL that ends in a bare '/'."""
    repo = _import_repo()
    root = generated_root(tmp_path)
    init_repo(root)
    _set_origin(root, "https://host/org/")

    with pytest.raises(repo.RepoError, match="no final path segment"):
        repo.repo_slug(root)


def test_repo_slug_raises_when_remote_url_final_segment_is_only_dot_git(tmp_path: Path) -> None:
    """AC-FUNC-003 / AC-TEST-002: a configured URL whose final path segment is exactly
    '.git' must not silently collapse to an empty slug once the suffix is removed."""
    repo = _import_repo()
    root = generated_root(tmp_path)
    init_repo(root)
    _set_origin(root, "https://host/org/.git")

    with pytest.raises(repo.RepoError, match="no repository name"):
        repo.repo_slug(root)


def test_repo_slug_honors_git_remote_timeout_env_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-FUNC-004: `REPO_SLUG_GIT_TIMEOUT_SECONDS` reaches `subprocess.run`'s `timeout` kwarg."""
    repo = _import_repo()
    root = generated_root(tmp_path)
    init_repo(root)
    monkeypatch.setenv(repo.GIT_REMOTE_TIMEOUT_ENV_VAR, "2.5")
    recorded_timeouts: list[float] = []

    def _recording_run(
        cmd: list[str],
        *,
        cwd: Path,
        capture_output: bool,
        text: bool,
        timeout: float,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        recorded_timeouts.append(timeout)
        return subprocess.CompletedProcess(
            cmd, returncode=0, stdout="https://host/org/general-dev.git\n", stderr=""
        )

    monkeypatch.setattr(repo.subprocess, "run", _recording_run)

    assert repo.repo_slug(root) == "general-dev"
    assert recorded_timeouts == [2.5]


def test_repo_slug_raises_when_git_remote_timeout_env_var_is_invalid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-FUNC-003 / AC-FUNC-004: an unparsable timeout env var raises `RepoError`, not
    `hostprobe.HostProbeError`, so no foreign exception type crosses the module boundary."""
    repo = _import_repo()
    root = generated_root(tmp_path)
    init_repo(root)
    monkeypatch.setenv(repo.GIT_REMOTE_TIMEOUT_ENV_VAR, "not-a-number")

    with pytest.raises(repo.RepoError, match=repo.GIT_REMOTE_TIMEOUT_ENV_VAR):
        repo.repo_slug(root)


def test_repo_slug_raises_when_git_config_read_times_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-FUNC-003 / AC-TEST-002: `subprocess.TimeoutExpired` is converted to a `RepoError`."""
    repo = _import_repo()
    root = generated_root(tmp_path)
    init_repo(root)

    def _timed_out_run(
        cmd: list[str],
        *,
        cwd: Path,
        capture_output: bool,
        text: bool,
        timeout: float,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout)

    monkeypatch.setattr(repo.subprocess, "run", _timed_out_run)

    with pytest.raises(repo.RepoError, match="did not answer within"):
        repo.repo_slug(root)


_WORK_UNIT_ID_PATTERN = re.compile(r"E\d+-F\d+-S\d+(-T\d+)?")


def _git_remote_timeout_comment_block() -> str:
    """The contiguous `#` comment run immediately preceding
    `GIT_REMOTE_TIMEOUT_ENV_VAR`'s assignment in `repo.py`'s own source.

    Isolating exactly this block, rather than scanning the whole module
    body, is what lets the assertions below fail specifically on this
    block's own drift instead of being satisfiable by unrelated comment
    text elsewhere in the file, such as `PRIVATE_FILES`'s own
    backlog-identifier comment, an established convention this task does
    not touch (out of scope, per this unit's Description).
    """
    repo = _import_repo()
    lines = Path(repo.__file__).read_text(encoding="utf-8").splitlines()

    assignment_needle = "GIT_REMOTE_TIMEOUT_ENV_VAR = "
    assignment_index = next(
        (index for index, line in enumerate(lines) if line.startswith(assignment_needle)),
        None,
    )
    assert assignment_index is not None, (
        "repo.py no longer declares GIT_REMOTE_TIMEOUT_ENV_VAR at module scope."
    )

    comment_lines: list[str] = []
    index = assignment_index - 1
    while index >= 0 and lines[index].startswith("#"):
        comment_lines.insert(0, lines[index])
        index -= 1

    assert comment_lines, "No comment block precedes GIT_REMOTE_TIMEOUT_ENV_VAR in repo.py."
    return "\n".join(comment_lines)


def test_git_remote_timeout_comment_names_no_backlog_identifier() -> None:
    """AC-DOC-002 / AC-TEST-001: the comment above `GIT_REMOTE_TIMEOUT_ENV_VAR` names no
    work-unit identifier and no longer claims the test module declares its own pair.

    Both assertions were observed to FAIL against the comment E8-F1-S1-T3
    committed (`... E8-F1-S1-T6 is the tracked follow-up ...` and
    `... presently declares its own ...`), the stale forward reference this
    task rewrites now that E8-F1-S1-T6 is done. Restoring that committed
    text makes this test fail again (AC-TEST-003's mutation check), so
    these assertions are not tautological.
    """
    block = _git_remote_timeout_comment_block()

    match = _WORK_UNIT_ID_PATTERN.search(block)
    assert match is None, (
        f"GIT_REMOTE_TIMEOUT_ENV_VAR's comment names a work-unit identifier "
        f"({match.group(0)!r}); operator-facing comments describe the current "
        "state of the code, not the backlog task that produced it."
    )
    assert "presently declares its own" not in block, (
        "GIT_REMOTE_TIMEOUT_ENV_VAR's comment still claims "
        "tests/test_state_bucket_name.py declares its own timeout pair, which "
        "E8-F1-S1-T6 made false by rebinding those names onto this module."
    )


def test_git_remote_timeout_comment_still_names_the_binding_test_module() -> None:
    """AC-DOC-001 / AC-TEST-002: the rewritten comment still names
    `tests/test_state_bucket_name.py` as the consumer that binds to these two
    names by importing them, not merely mentions the file's name.

    Without this assertion, GREEN could satisfy the previous test by
    deleting the cross-reference outright instead of correcting it, losing
    the operator-facing fact that another module depends on this
    declaration. The committed comment names the file too, but only as the
    module that declares its own competing pair (`... rather than importing
    these two names ...`), the opposite relationship; this test was
    observed to FAIL against that text on the second assertion below, since
    it never states the names are reached `by importing` them.
    """
    block = _git_remote_timeout_comment_block()
    assert "tests/test_state_bucket_name.py" in block, (
        "GIT_REMOTE_TIMEOUT_ENV_VAR's comment no longer names "
        "tests/test_state_bucket_name.py as the module that binds to these "
        "two names."
    )
    assert "by importing" in block, (
        "GIT_REMOTE_TIMEOUT_ENV_VAR's comment no longer states that "
        "tests/test_state_bucket_name.py binds to these two names by "
        "importing them, rather than restating either."
    )


@pytest.mark.parametrize("selector", list(_PRESENT_SELECTORS))
def test_missing_examples_reports_absent_examples(tmp_path: Path, selector: str) -> None:
    repo = _import_repo()
    root = generated_root(tmp_path)
    examples = tuple(repo.example_for(p) for p in repo.PRIVATE_FILES)
    present = _PRESENT_SELECTORS[selector](examples)
    for example in present:
        example_path = root / example
        example_path.parent.mkdir(parents=True, exist_ok=True)
        example_path.write_text("seed\n", encoding="utf-8")

    expected = sorted(example for example in examples if example not in present)

    assert sorted(repo.missing_examples(root)) == expected


def test_end_to_end_cycle_from_a_real_repository(tmp_path: Path) -> None:
    """AC-CYCLE-001: a real repo, a nested start point, and the whole chain."""
    repo = _import_repo()
    root = generated_root(tmp_path)
    init_repo(root)
    nested = root / "a" / "b" / "c"
    nested.mkdir(parents=True)

    discovered = repo.find_root(nested)

    assert discovered == root.resolve()

    paths = repo.private_paths(discovered)
    for relative in repo.PRIVATE_FILES:
        assert paths[relative] == discovered / relative

    assert repo.container_workspace(discovered) == f"/workspaces/{discovered.name}"
    assert discovered.name == root.resolve().name
