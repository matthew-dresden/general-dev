"""Contract tests for the repository root's pyproject.toml.

These tests pin the pytest and coverage values pyproject.toml configures:
the pythonpath entry that makes `import devcontainer_config` resolve, and
the coverage fail_under = 90 gate that spec decision D15 authorizes.

The `.gitignore` allowlist assertions this module used to carry for
`pyproject.toml`, `tests/` and `.claude/` (plus the `.claude/
settings.local.json` negative) now live in
`tests/test_gitignore_allowlist.py`, which asserts those same three roots
alongside `provider/` and `remote-instances/` as one allowlist instead of
splitting the same concern across two files. This module no longer runs
`git check-ignore` itself; it shares only the repository-root resolution
with `tests/test_gitignore_allowlist.py`, through `tests/gitignore_check.py`'s
`repo_root()`, so neither module keeps an independent copy of how the
checkout root is found.
"""

from __future__ import annotations

import tomllib
from typing import Any

from gitignore_check import repo_root


def _load_pyproject() -> dict[str, Any]:
    return tomllib.loads((repo_root() / "pyproject.toml").read_text())


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
