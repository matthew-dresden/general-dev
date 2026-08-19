"""Contract tests for the `make test` target and its wiring into `make validate`.

Three regressions here are silent failures if left unasserted: `test` falling
out of `.PHONY`, which turns it into a no-op the moment a path named exactly
`test` (a file or a directory) exists in the repository root; `validate`
losing `lint`, `test`, or both, which narrows what `make validate` verifies
without any caller noticing; and the `test` recipe reaching for something off
this machine (docker, aws, ssh, curl, an HTTP endpoint), which turns a
hermetic suite into an environment-dependent one (AC-10.14).

The Makefile is read through `devcontainer_config.repo.find_root`, resolved
from this test file's own location, so the assertions hold from any working
directory a test runner is invoked from rather than assuming the repository
root is the current directory.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from devcontainer_config.repo import find_root

# Spec Section 4.1.2 requires the `make test` row to stay "no docker, no AWS,
# no network"; this task's Approach step 2 derives the concrete tokens that
# would signal a breach of that "no network" clause: a container engine, a
# cloud CLI, a remote shell, or an HTTP call reaching off this machine.
OFF_MACHINE_TOKENS: tuple[str, ...] = ("docker", "aws", "ssh", "curl", "http")


def _makefile_text() -> str:
    """The repository root `Makefile`, read fresh for every call.

    Not cached at module scope: caching would let one test's assertion
    about the file's content leak into another's failure message instead of
    each test reading the file it is actually asserting about.
    """
    root = find_root(Path(__file__).resolve().parent)
    return (root / "Makefile").read_text(encoding="utf-8")


def _phony_targets(makefile_text: str) -> set[str]:
    """Every target named in the (possibly backslash-continued) `.PHONY` list.

    `.PHONY:` in this Makefile spans several lines joined with a trailing
    `\\`; a line-by-line split would only see the first line's targets. The
    whole block, up to the next line that starts a new statement, is
    collapsed into one string first so `.split()` sees every target once.
    """
    match = re.search(r"^\.PHONY:(.*?)(?=^\S|\Z)", makefile_text, re.MULTILINE | re.DOTALL)
    assert match is not None, "no .PHONY declaration found in Makefile"
    return set(match.group(1).replace("\\\n", " ").split())


def _validate_prerequisites(makefile_text: str) -> set[str]:
    """The prerequisite set on the `validate:` target line."""
    match = re.search(r"^validate:(.*)$", makefile_text, re.MULTILINE)
    assert match is not None, "no validate target found in Makefile"
    return set(match.group(1).split())


def _test_recipe_body(makefile_text: str) -> str:
    """The recipe lines (tab-indented) that follow the `test:` target header."""
    match = re.search(r"^test:.*\n((?:\t.*\n?)*)", makefile_text, re.MULTILINE)
    assert match is not None, "no test target found in Makefile"
    return match.group(1)


def test_test_target_is_phony() -> None:
    """AC-FUNC-003 / AC-TEST-001: `test` is declared `.PHONY`."""
    assert "test" in _phony_targets(_makefile_text())


def test_validate_requires_lint_and_test() -> None:
    """AC-FUNC-004 / AC-TEST-002: `validate`'s prerequisites are exactly these two.

    Asserted as set equality, not membership, so both a removal and an
    unexpected addition fail this test.
    """
    assert _validate_prerequisites(_makefile_text()) == {"lint", "test"}


@pytest.mark.parametrize("token", OFF_MACHINE_TOKENS)
def test_test_recipe_has_no_off_machine_token(token: str) -> None:
    """AC-FUNC-005 / AC-TEST-003: the `test` recipe reaches nothing off this machine."""
    recipe = _test_recipe_body(_makefile_text())
    assert token not in recipe.lower()
