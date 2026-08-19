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
