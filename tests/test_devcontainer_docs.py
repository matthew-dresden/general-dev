"""Pinning tests for the `docs/devcontainer.md` provisioning-flow documentation
of the hostcreds credential-startup block.

`configure_shell_env` in `.devcontainer/.devcontainer.postcreate.sh`
renders that block once through `devcontainer_config.cli shell-block`
(`render_hostcreds_shell_block`) and appends it to both startup files,
aborting provisioning via `exit_with_error` on a non-zero render. This
module owns the two facts of the 'Provisioning flow (postCreate)' section
that describe that step: the step-table row for `shell.env` sourcing
(AC-DOC-001, AC-DOC-002), and the sentence above the table enumerating
every `required` step that aborts the build through `exit_with_error`
(AC-DOC-003).

Every fact-presence assertion below reads the rendered document text, the
same discipline `tests/test_docs_environment_files.py` documents, so a
future edit that drops any one of these facts fails the assertion that
names it, instead of surviving as an unnoticed drift.

The two `_function_body`-based tests do not stop at the substring
`exit_with_error` appearing anywhere in `configure_shell_env` -- that
substring is already true for the unrelated, pre-existing
`shell.env`-missing precondition, so asserting only its presence would stay
green whether or not the render call existed at all (the defect a prior
round of this task shipped: its cross-check test read `assert
"exit_with_error" in _configure_shell_env_body()` and passed against a
script that had no render step, per that round's own TDD Cycle Log showing
it as the '1 passed' case in an otherwise-red run). Instead these tests
assert the render invocation and its abort branch specifically: that
`configure_shell_env` routes its `render_hostcreds_shell_block`
call through `|| exit_with_error` on the same statement, and that
`render_hostcreds_shell_block` itself sets `PYTHONPATH` and invokes
`python3 -m devcontainer_config.cli shell-block`.

`_function_body`, `_provisioning_flow_table` and
`_provisioning_flow_required_steps_prose` are imported from
`tests/conftest.py` rather than defined here: `tests/test_postcreate_hooks.py`
needs the identical script reader, the identical brace-depth
function-body scan, and the identical provisioning-flow doc regions, so
this module builds its row lookup and its enumeration-prose assertion on
the same shared extractors that module's own `_git_hooks_step`, `_main_body`
and required-step-count assertion use, instead of a second, divergent
implementation of each.
"""

from __future__ import annotations

import re

import pytest
from conftest import (
    _function_body,
    _provisioning_flow_required_steps_prose,
    _provisioning_flow_table,
)

_DOCS_RELATIVE_PATH = "docs/devcontainer.md"

# One case per fact the provisioning-flow row must state (AC-DOC-001,
# AC-DOC-002). Parametrized over a single tuple, rather than three
# near-identical test bodies (AC-TEST-003), so each case fails
# independently naming the missing fact.
_REQUIRED_ROW_SUBSTRINGS: tuple[tuple[str, str], ...] = (
    ("startup_block_named", "hostcreds credential-startup block"),
    (
        "pythonpath_dependency_named",
        "`python3` + `devcontainer_config` on `PYTHONPATH`",
    ),
    # The trailing ", required |" shape, not the bare word "required": a
    # tighter needle than the word alone, which could be satisfied by an
    # unrelated occurrence anywhere in the row.
    ("required_marker_present", ", required |"),
)


def _provisioning_flow_row() -> str:
    """The step-table row documenting `shell.env` sourcing.

    Located by its stable row-opening prefix (`| `shell.env` sourcing`)
    within the shared `_provisioning_flow_table()` text, rather than a
    fixed line number, so a row inserted or removed elsewhere in the table
    does not move this assertion off its target. A table row is always
    exactly one line, so no whitespace normalization is needed the way it
    is for the wrapped prose `_provisioning_flow_required_steps_prose`
    returns.
    """
    match = re.search(r"^ *\| `shell\.env` sourcing.*\|$", _provisioning_flow_table(), re.MULTILINE)
    assert match is not None, (
        f"no 'shell.env sourcing' row found in the provisioning-flow table of {_DOCS_RELATIVE_PATH}"
    )
    return match.group(0)


@pytest.mark.parametrize(
    "fact_id,needle",
    _REQUIRED_ROW_SUBSTRINGS,
    ids=[fact_id for fact_id, _ in _REQUIRED_ROW_SUBSTRINGS],
)
def test_provisioning_flow_row_states_startup_block_fact(fact_id: str, needle: str) -> None:
    row = _provisioning_flow_row()
    assert needle in row, (
        f"provisioning-flow row is missing fact {fact_id!r}: expected {needle!r} in {row!r}"
    )


def test_exit_with_error_enumeration_names_the_startup_block_render() -> None:
    prose = _provisioning_flow_required_steps_prose()
    assert "hostcreds startup block render" in prose, (
        f"{_DOCS_RELATIVE_PATH}'s exit_with_error enumeration does not name the hostcreds "
        "startup block render (AC-DOC-003)."
    )
    assert "worse than a container that failed to create" in prose, (
        f"{_DOCS_RELATIVE_PATH}'s exit_with_error enumeration no longer states why a non-zero "
        "render is fatal (AC-DOC-003)."
    )


def test_configure_shell_env_routes_the_render_call_through_exit_with_error() -> None:
    """AC-DOC-004 / AC-TEST-002: `configure_shell_env` actually calls the
    hostcreds block renderer, and the call's failure routes to
    `exit_with_error` on the same statement -- not merely the substring
    `exit_with_error` occurring anywhere in the function for the unrelated
    `shell.env`-missing precondition.
    """
    body = _function_body("configure_shell_env")
    pattern = re.compile(r'render_hostcreds_shell_block\)"\s*\|\|\s*exit_with_error')
    assert pattern.search(body), (
        "configure_shell_env does not route a failed render_hostcreds_shell_block "
        "call to exit_with_error; the provisioning-flow section's fatal-render "
        "contract would then describe an abort path the script does not implement "
        "(AC-DOC-004)."
    )


def test_render_hostcreds_shell_block_invokes_python3_module_with_pythonpath_set() -> None:
    """AC-DOC-002 / AC-DOC-004: the row's `python3` + `devcontainer_config`
    on `PYTHONPATH` dependency matches the actual invocation."""
    body = _function_body("render_hostcreds_shell_block")
    assert "PYTHONPATH=" in body, (
        "render_hostcreds_shell_block does not set PYTHONPATH; docs/devcontainer.md's "
        "provisioning-flow row would then document a dependency the script does not have "
        "(AC-DOC-004)."
    )
    assert "python3 -m devcontainer_config.cli shell-block" in body, (
        "render_hostcreds_shell_block does not invoke "
        "'python3 -m devcontainer_config.cli shell-block'; "
        "docs/devcontainer.md's provisioning-flow row would then document a dependency "
        "the script does not have (AC-DOC-004)."
    )
