"""Contract tests for the repository root's pyproject.toml and .gitignore.

These tests pin two things a later change could silently break:

* the .gitignore allowlist re-includes pyproject.toml, tests/ and .claude/ at
  the repository root, while .claude/settings.local.json stays ignored by its
  own later, more specific entry;
* the pytest and coverage values pyproject.toml configures are the ones this
  program depends on: the pythonpath entry that makes `import
  devcontainer_config` resolve, and the coverage fail_under = 90 gate that
  spec decision D15 authorizes.
"""

from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path
from typing import Any

import pytest


def _repo_root() -> Path:
    """Resolve the repository root through git, never a hard-coded path."""
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=True,
    )
    return Path(result.stdout.strip())


def _check_ignore_pattern(path: str) -> str | None:
    """Return the pattern that last matched `path` in git's exclude rules.

    `git check-ignore --no-index -v` prints
    `<source>:<linenum>:<pattern><TAB><path>` for the last matching rule,
    whether that rule excludes or (via a leading "!") re-includes the path.
    None means no rule matched at all.

    `--no-index` is required, not optional. Without it, git skips exclude
    evaluation entirely for any path already tracked in the index and
    prints nothing for it, unconditionally, regardless of what .gitignore
    says. pyproject.toml and tests/test_project_config.py are staged by
    this same task, so a plain `git check-ignore -v` on them would report
    "no match" in both the pre-fix and post-fix .gitignore, unable to
    discriminate one from the other. `--no-index` forces pure pattern
    matching against .gitignore content, independent of index state, which
    is also the only way to observe .claude/'s status at all: git never
    tracks an empty directory, so .claude can never reach the "tracked,
    skip evaluation" path in the first place.
    """
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", "-v", path],
        cwd=_repo_root(),
        capture_output=True,
        text=True,
        check=False,
    )
    if not result.stdout.strip():
        return None
    source_and_pattern, _, _ = result.stdout.rstrip("\n").partition("\t")
    _, _, pattern = source_and_pattern.rpartition(":")
    return pattern


def _is_excluded(path: str) -> bool:
    """True when `.gitignore` excludes `path` from being tracked."""
    pattern = _check_ignore_pattern(path)
    assert pattern is not None, (
        f"no .gitignore rule matched {path!r}; expected at least the "
        "repository root's /* allowlist rule to match every root-level path"
    )
    return not pattern.startswith("!")


@pytest.mark.parametrize("path", ["pyproject.toml", "tests", ".claude"])
def test_root_path_is_trackable(path: str) -> None:
    """Re-included root paths must not be excluded by the /* allowlist root."""
    assert _is_excluded(path) is False


def test_local_settings_still_ignored() -> None:
    """The broader /.claude/ re-include must not expose local settings."""
    assert _is_excluded(".claude/settings.local.json") is True


def _load_pyproject() -> dict[str, Any]:
    return tomllib.loads((_repo_root() / "pyproject.toml").read_text())


def test_coverage_fail_under_is_90() -> None:
    data = _load_pyproject()
    assert data["tool"]["coverage"]["report"]["fail_under"] == 90


def test_coverage_branch_is_enabled() -> None:
    data = _load_pyproject()
    assert data["tool"]["coverage"]["run"]["branch"] is True


def test_coverage_source_targets_devcontainer_config() -> None:
    data = _load_pyproject()
    assert data["tool"]["coverage"]["run"]["source"] == [
        ".claude/plugins/devcontainer/scripts/devcontainer_config"
    ]


def test_pytest_testpaths_is_tests_dir() -> None:
    data = _load_pyproject()
    assert data["tool"]["pytest"]["ini_options"]["testpaths"] == ["tests"]


def test_pytest_pythonpath_targets_plugin_scripts() -> None:
    data = _load_pyproject()
    assert data["tool"]["pytest"]["ini_options"]["pythonpath"] == [
        ".claude/plugins/devcontainer/scripts"
    ]


def test_requires_python_floor_is_311() -> None:
    data = _load_pyproject()
    assert data["project"]["requires-python"] == ">=3.11"
