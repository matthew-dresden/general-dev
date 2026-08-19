"""Tests for the container half of git hook installation (spec Section 4.6, AC-4.7).

`.devcontainer/.devcontainer.postcreate.sh` is asserted on as text here, never
executed: the script's very first non-comment lines require a real
`CONTAINER_USER` resolvable through `getent passwd`, which the hermetic suite
cannot guarantee, and Section 10.2 says outright that this suite cannot start
docker. What AC-4.7 actually cares about -- that the container installs hooks
through the identical `Makefile` entry point the host uses, so the two
cannot render different content -- is a property of the script's text, not
of running it, so every assertion below is a text-level consistency check.
A live container build, the only way to observe the hooks actually land in
`.git/hooks`, is covered separately by the live suites; this suite does not
attempt one (see the work unit's Approach, "Verification is a consistency
test rather than a live container build").

Nothing here needs the module-body-deferred-import discipline
`tests/test_githooks.py`'s docstring documents: this task's Changes Manifest
touches no Python source file, so the TDD RED gate's stash of this unit's
own production files never touches `devcontainer_config`, the same reasoning
`tests/test_makefile_contract.py` already relies on for its own top-level
`from devcontainer_config.repo import find_root`.
"""

from __future__ import annotations

import re
from pathlib import Path

from devcontainer_config.githooks import HOOK_NAMES, hook_body
from devcontainer_config.repo import find_root

_FORGIVING_SUFFIXES: tuple[str, ...] = ("|| true", "|| :", "; true", "2>/dev/null", "> /dev/null")


def _repo_root() -> Path:
    """The repository root, resolved from this test file's own location."""
    return find_root(Path(__file__).resolve().parent)


def _postcreate_text() -> str:
    """`.devcontainer/.devcontainer.postcreate.sh`, read fresh for every call."""
    return (_repo_root() / ".devcontainer" / ".devcontainer.postcreate.sh").read_text(
        encoding="utf-8"
    )


def _makefile_text() -> str:
    """The repository root `Makefile`, read fresh for every call."""
    return (_repo_root() / "Makefile").read_text(encoding="utf-8")


def _hooks_install_target() -> str:
    """The `Makefile` target name the host uses to install hooks.

    Parsed rather than hard-coded (AC-TEST-001): a rename of the target in
    the `Makefile` would silently desync a literal repeated here instead of
    failing this test.
    """
    match = re.search(r"^(hooks-install):", _makefile_text(), re.MULTILINE)
    assert match is not None, "no hooks-install target found in Makefile"
    return match.group(1)


def _git_hooks_step() -> str:
    """The `install_git_hooks` function body from `.devcontainer.postcreate.sh`.

    Isolated by name so the fatal-on-failure and workspace-naming assertions
    below examine only the step this task adds, not some unrelated step that
    happens to contain similar text elsewhere in the script.
    """
    match = re.search(
        r"^install_git_hooks\(\)\s*\{(.*?)^\}", _postcreate_text(), re.MULTILINE | re.DOTALL
    )
    assert match is not None, (
        "no install_git_hooks function found in .devcontainer/.devcontainer.postcreate.sh"
    )
    return match.group(1)


def _main_body() -> str:
    """The `main()` function body from `.devcontainer.postcreate.sh`.

    Isolated so step-order assertions read the actual call sequence rather
    than any incidental ordering of the step functions' own definitions.
    """
    match = re.search(r"^main\(\)\s*\{(.*?)^\}", _postcreate_text(), re.MULTILINE | re.DOTALL)
    assert match is not None, (
        "no main() function found in .devcontainer/.devcontainer.postcreate.sh"
    )
    return match.group(1)


def _provisioning_flow_table() -> str:
    """The `| Step | Depends on |` table inside `docs/devcontainer.md`.

    Bounded to the table's own rows (from its header row to the next blank
    line) so a `make hooks-install` mention in the section's surrounding
    prose -- the introductory paragraph and the `## Git hooks` section both
    describe the same command -- cannot satisfy an assertion meant for the
    step table itself.
    """
    doc_text = (_repo_root() / "docs" / "devcontainer.md").read_text(encoding="utf-8")
    match = re.search(
        r"^   \| Step \| Depends on \|\n(.*?)(?=^\n)", doc_text, re.MULTILINE | re.DOTALL
    )
    assert match is not None, "no '| Step | Depends on |' table found in docs/devcontainer.md"
    return match.group(1)


def _provisioning_flow_intro() -> str:
    """The sentence enumerating which provisioning steps abort the build.

    Bounded from "below marks `required`:" to "Each of those aborts the
    build", so a `required` mention anywhere else in the file (the table
    rows themselves, or unrelated prose) cannot satisfy an assertion meant
    for this one enumeration. Line wraps are collapsed to single spaces so
    the semicolon-delimited items can be split without embedded newlines.
    """
    doc_text = (_repo_root() / "docs" / "devcontainer.md").read_text(encoding="utf-8")
    match = re.search(
        r"below marks `required`:(.*?)\. Each of those aborts the build",
        doc_text,
        re.DOTALL,
    )
    assert match is not None, "no provisioning-flow exception-step enumeration found"
    return " ".join(match.group(1).split())


def test_postcreate_keeps_set_euo_pipefail() -> None:
    """The step relies on the script's own failure semantics (Section 7.1), not its own."""
    assert "set -euo pipefail" in _postcreate_text()


def test_postcreate_defines_a_git_hooks_step() -> None:
    """AC-FUNC-001: the postCreate script defines a hook-installation step."""
    assert "install_git_hooks" in _postcreate_text()


def test_git_hooks_step_is_called_from_main() -> None:
    """AC-FUNC-001 / AC-FUNC-002: main() actually invokes the step, not just defines it."""
    assert re.search(r"^\s*install_git_hooks\s*$", _main_body(), re.MULTILINE)


def test_git_hooks_step_invokes_the_host_installation_target() -> None:
    """AC-FUNC-001 / AC-TEST-001: the step runs the same target the host uses."""
    target = _hooks_install_target()
    assert re.search(rf"\bmake\s+{re.escape(target)}\b", _git_hooks_step())


def test_git_hooks_step_uses_the_shared_printers() -> None:
    """AC-FUNC-002 / AC-3.1: reports through log_section / log_section_done, no new printer."""
    step = _git_hooks_step()
    assert re.search(r"\blog_section\b", step)
    assert re.search(r"\blog_section_done\b", step)


def test_git_hooks_step_does_not_swallow_a_non_zero_status() -> None:
    """AC-TEST-002: nothing discards the installation command's exit status."""
    step = _git_hooks_step()
    for suffix in _FORGIVING_SUFFIXES:
        assert suffix not in step, f"{suffix!r} would swallow a non-zero status"


def test_git_hooks_step_propagates_the_install_command_failure() -> None:
    """AC-TEST-002: the install command's own failure reaches an explicit handler."""
    step = _git_hooks_step()
    target = _hooks_install_target()
    install_idx = step.index(f"make {target}")
    tail = step[install_idx : install_idx + 200]
    assert "||" in tail, "the install command's failure is not wired to a handler"


def test_git_hooks_step_aborts_through_exit_with_error_naming_the_workspace() -> None:
    """AC-FUNC-003: a failing install names the workspace and the rerun command."""
    step = _git_hooks_step()
    target = _hooks_install_target()
    install_idx = step.index(f"make {target}")
    error_idx = step.index("exit_with_error", install_idx)
    handler = step[error_idx:]
    assert "${WORK_DIR}" in handler
    assert f"make {target}" in handler


def test_git_hooks_step_checks_for_the_git_directory_before_installing() -> None:
    """AC-FUNC-004: no .git directory aborts, naming the resolved workspace path."""
    step = _git_hooks_step()
    target = _hooks_install_target()
    git_dir_check = '"${WORK_DIR}/.git"'
    assert git_dir_check in step
    guard_idx = step.index(git_dir_check)
    install_idx = step.index(f"make {target}")
    assert guard_idx < install_idx, "the .git check must run before the install command"
    error_idx = step.index("exit_with_error", guard_idx)
    assert error_idx < install_idx, "the .git guard must reach exit_with_error before installing"
    handler = step[error_idx:install_idx]
    assert "${WORK_DIR}" in handler


def test_git_hooks_step_restores_container_user_ownership_after_install() -> None:
    """Ownership invariant: postCreate runs as root (sudo, postcreate-wrapper.sh), so
    `make hooks-install` would otherwise leave `.git/hooks/pre-commit` and
    `.git/hooks/pre-push` root-owned inside a workspace that belongs to
    `CONTAINER_USER`. Every other root-side workspace writer in this script
    restores ownership explicitly (`declare_unclaimed_path` chowns
    `.gitmodules`, `configure_repo_detection` chowns `REPOS_PATH`); this step
    must follow the same pattern for the hooks it installs.
    """
    step = _git_hooks_step()
    target = _hooks_install_target()
    install_idx = step.index(f"make {target}")
    chown_idx = step.index("chown", install_idx)
    assert chown_idx > install_idx, "the chown must run after the install command"
    tail = step[chown_idx:]
    assert '"${CONTAINER_USER}:${CONTAINER_USER}"' in tail
    assert ".git/hooks" in tail


def test_postcreate_contains_no_hook_body_text() -> None:
    """AC-FUNC-006 / AC-TEST-005: the container renders content from githooks, not a copy."""
    text = _postcreate_text()
    for hook_name in HOOK_NAMES:
        assert hook_body(hook_name) not in text


def test_host_and_container_share_one_hook_body_renderer() -> None:
    """AC-TEST-004: the host's target and the container's target are the same one.

    Both the `Makefile`'s `hooks-install` recipe and the container's step
    invoke the identical target name; since that target is the only place
    either installs hooks from -- and it delegates to
    `devcontainer_config.cli`, which renders content through
    `devcontainer_config.githooks.hook_body` -- there is exactly one
    renderer either install path can reach.
    """
    target = _hooks_install_target()
    makefile_recipe_match = re.search(
        rf"^{re.escape(target)}:.*\n((?:\t.*\n?)*)", _makefile_text(), re.MULTILINE
    )
    assert makefile_recipe_match is not None
    assert "devcontainer_config.cli hooks-install" in makefile_recipe_match.group(1)
    assert re.search(rf"\bmake\s+{re.escape(target)}\b", _git_hooks_step())


def test_provisioning_flow_table_stays_in_sync_with_mains_step_order() -> None:
    """AC-DOC-001 regression: doc_review once caught this exact drift.

    `main()` calls `configure_git`, then `install_git_hooks`, then
    `configure_repo_detection`; `docs/devcontainer.md`'s provisioning-flow
    table was written to document a hooks-install row between the
    git-identity row and the `.gitmodules` row to match. Pinning both
    orderings here means a future step reordered or added in `main()` with
    no matching table update fails this test instead of surfacing only at
    doc_review time.
    """
    main_body = _main_body()
    git_call_idx = main_body.index("configure_git")
    hooks_call_idx = main_body.index("install_git_hooks")
    repo_detect_call_idx = main_body.index("configure_repo_detection")
    assert git_call_idx < hooks_call_idx < repo_detect_call_idx, (
        "main() no longer calls install_git_hooks between configure_git and "
        "configure_repo_detection"
    )

    table = _provisioning_flow_table()
    git_row_idx = table.index("git identity and credential helper")
    hooks_row_idx = table.index("make hooks-install")
    gitmodules_row_idx = table.index("`.gitmodules` per repository")
    assert git_row_idx < hooks_row_idx < gitmodules_row_idx, (
        "docs/devcontainer.md's provisioning-flow table no longer documents the "
        "hooks-install step between the git-identity and .gitmodules rows"
    )


def test_provisioning_flow_intro_enumerates_every_required_step() -> None:
    """AC-DOC-001 regression: doc_review twice caught this paragraph's claim about
    which steps abort the build drifting from the table's `required` rows --
    once naming the resmon link the lone exception when two steps aborted,
    once claiming "Two steps are the exception" when four did. The fix stopped
    stating a count and instead enumerates the steps the table itself marks
    `required`; this pins that enumeration's length to the table's actual
    `required` row count, so a step gaining or losing `required` without a
    matching prose update fails here instead of at doc_review time again.
    """
    table = _provisioning_flow_table()
    required_row_count = table.count(", required |")
    assert required_row_count > 0, "no `required` rows found in the provisioning-flow table"

    intro = _provisioning_flow_intro()
    enumerated_items = [item for item in intro.split("; ") if item.strip()]
    assert len(enumerated_items) == required_row_count, (
        f"provisioning-flow intro enumerates {len(enumerated_items)} step(s) but "
        f"the table marks {required_row_count} row(s) required"
    )
