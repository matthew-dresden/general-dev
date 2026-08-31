"""Contract tests for `.github/workflows/ci.yml` (E1-F1-S1-T3).

CI is an inbound integration whose contract is defined entirely by this one
file (spec Section 11): `main` requires the `lint` status check today (spec
Section 1.9) and this task adds a `test` job alongside it. A text search over
the raw YAML would pass on a `test:` job commented out, nested under the
wrong key, or a step whose command merely mentions `make test` inside an
unrelated string -- none of which would actually run in CI. Every assertion
here instead parses the file with `yaml.safe_load` and indexes into the
resulting structure, so only a job and step that GitHub Actions itself would
execute can satisfy it.

`.github/workflows/ci.yml` is not Python production source under
`.claude/plugins/devcontainer/scripts/devcontainer_config` (this task's
Changes Manifest carries no rows there), so unlike `tests/test_repo.py` and
`tests/test_catalog.py` this module needs no deferred-import convention: the
TDD RED gate stashes only the workflow file itself back to its pre-change,
`lint`-only state, and the file continues to exist either way. `yaml` and
`gitignore_check` both import cleanly regardless of which state the
workflow is in, so a module-level import never risks turning a genuine test
failure into a collection error.

`_WORKFLOW_JOB_NAMES` is read once, at collection time, from whatever job
names the workflow actually declares, and drives
`test_every_run_step_declares_shell_bash`'s parametrize decorator. That is
why the `shell: bash` assertion is "one case per job" rather than a
hard-coded `["lint", "test"]" list: a future job added to the workflow
without `shell: bash` on one of its `run` steps gets its own failing case
automatically, with no second edit to this file required.

`test_ci_zsh_install_step_package_matches_makefile_prerequisites_row`
(AC-TEST-006, transferred from E3-F2-S2-T5's AC-TEST-003) is the one case in
this module that also reads `Makefile`. E3-F2-S2-T5 owns the Makefile's
`test` PREREQUISITES row -- the host-side documentation of the same `zsh`
dependency this file's `Install zsh` step satisfies in CI -- but that unit's
own suite (`tests/test_makefile_contract.py`) cannot see this file's step,
so the drift check has to live on this side of the pair. Both the CI
package name and the documented package name are parsed out of the files
that declare them (`_ci_zsh_install_package_name`,
`_makefile_test_row_linux_package_name`) rather than hardcoded, so the
comparison is a real cross-check of two independently-authored strings, not
two copies of the same literal. `_makefile_text` and `_resolve_make_refs`
are imported from `tests/conftest.py` rather than defined here
(`_make_variable`, the helper `_resolve_make_refs` calls internally, lives
in `tests/conftest.py` too but is not imported directly by any test in this
file): `tests/test_makefile_contract.py` needs the identical
Makefile-variable-resolution logic, and a private copy in each file risked
one drifting from the other while its sibling suite stayed green.
`test_makefile_parsing_helpers_are_defined_once` pins that neither this file
nor `tests/test_makefile_contract.py` locally re-declares them, mirroring
the precedent `tests/test_render.py::test_shared_fixture_helpers_are_defined_once`
already sets for `_generated_dir`/`_example_root`.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

import conftest
import pytest
import yaml
from conftest import _makefile_text, _resolve_make_refs
from gitignore_check import repo_root

# Mirrors `[tool.coverage.run] source` in pyproject.toml (E1-F1-S1-T1) -- the
# CI coverage step and the coverage tool's own configuration must name the
# same directory, or the gate in CI measures a different tree than the one
# `make test` and local `pytest --cov` runs measure.
_PACKAGE_COVERAGE_PATH = ".claude/plugins/devcontainer/scripts/devcontainer_config"

# Mirrors `[tool.coverage.report] fail_under` in pyproject.toml.
_COVERAGE_FAIL_UNDER_FLAG = "--cov-fail-under=90"

_MAKE_TEST_COMMAND = "make test"

# tests/test_shellrc.py (E3-F2-S2-T1) parametrizes its end-to-end cases over
# ("bash", "zsh"); `ubuntu-latest`'s base image does not ship zsh, so the
# test job must install it itself before `make test` runs. Both markers must
# appear in the SAME run step's command text -- a bare "zsh" substring match
# would be satisfied by a step whose command only mentions zsh in a comment
# and never installs it.
_ZSH_INSTALL_COMMAND_MARKER = "apt-get install"
_ZSH_PACKAGE_MARKER = "zsh"


def _zsh_install_step_indices(run_steps: list[tuple[int, dict[str, Any]]]) -> list[int]:
    """Indices, within `run_steps`, of steps whose command actually installs zsh.

    A step satisfies this only when its `run` text contains both an
    `apt-get install`-shaped command AND the package name `zsh`, so a step
    that merely mentions zsh in a comment or in unrelated prose does not
    satisfy it.
    """
    return [
        index
        for index, step in run_steps
        if _ZSH_INSTALL_COMMAND_MARKER in step["run"] and _ZSH_PACKAGE_MARKER in step["run"]
    ]


_APT_GET_INSTALL_PATTERN = re.compile(r"apt-get install(?:\s+-y)?\s+(\S+)")


def _apt_get_package_name(command_text: str) -> str:
    """The package name argument of an `apt-get install [-y] <package>` command.

    Applied to both the CI workflow's `Install zsh` step and to the
    Makefile PREREQUISITES row's Linux install hint
    (`_makefile_test_row_linux_package_name`), so the AC-TEST-006 cross-check
    compares two independently-parsed package names to each other instead of
    hardcoding either one as a separate literal in this file.
    """
    match = _APT_GET_INSTALL_PATTERN.search(command_text)
    assert match is not None, (
        f"expected an 'apt-get install [-y] <package>' command in {command_text!r}"
    )
    return match.group(1)


def _ci_zsh_install_package_name(workflow: dict[str, Any]) -> str:
    """The package name the CI test job's `Install zsh` step actually installs.

    Located by that step's own declared `name` (there must be exactly one),
    then read out of its `run` text with `_apt_get_package_name`.
    """
    steps = _jobs(workflow)["test"].get("steps", [])
    install_steps = [step for step in steps if step.get("name") == "Install zsh"]
    assert len(install_steps) == 1, (
        f"expected exactly one step named 'Install zsh' in the test job, found {len(install_steps)}"
    )
    return _apt_get_package_name(install_steps[0]["run"])


def _makefile_test_row_linux_package_name(makefile_text: str) -> str:
    """The package name the Makefile's `test` PREREQUISITES row installs on Linux.

    Reads the row directly out of the `help` recipe's own `@printf` line (not
    `make help`'s rendered output), resolves any `$(TEST_INSTALL_HINT_...)`
    reference against the Makefile's own variable declaration, and applies
    `_apt_get_package_name` to the resolved text -- picking out the `apt-get`
    (Linux) command specifically, even though the same row also carries a
    `brew install` (macOS) command for the same tool.
    """
    help_recipe_match = re.search(r"^help:.*\n((?:\t.*\n?)*)", makefile_text, re.MULTILINE)
    assert help_recipe_match is not None, "no help target found in Makefile"
    help_body = help_recipe_match.group(1)

    row_pattern = re.compile(r"^\t@printf '  %-23s %s\\n' \"test\"\s+\"([^\"]*)\"$", re.MULTILINE)
    row_match = row_pattern.search(help_body)
    assert row_match is not None, "no PREREQUISITES row found for 'test' in the help recipe"
    row = _resolve_make_refs(makefile_text, row_match.group(1))

    return _apt_get_package_name(row)


def _load_workflow() -> dict[str, Any]:
    """Parse `.github/workflows/ci.yml` from the checkout root."""
    workflow_path = repo_root() / ".github" / "workflows" / "ci.yml"
    loaded = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict), (
        f"expected a YAML mapping at {workflow_path}, got {type(loaded)}"
    )
    return loaded


def _jobs(workflow: dict[str, Any]) -> dict[str, Any]:
    return workflow["jobs"]


def _run_step_commands(job: dict[str, Any]) -> list[str]:
    """Every `run:` step command in `job`, in declaration order."""
    return [step["run"] for step in job.get("steps", []) if "run" in step]


def _uses_actions(job: dict[str, Any]) -> list[str]:
    """Every `uses:` action reference in `job`, in declaration order."""
    return [step["uses"] for step in job.get("steps", []) if "uses" in step]


# Read once, at collection time, from the workflow as it exists on disk --
# see the module docstring for why this is safe here and why it is what
# makes the `shell: bash` assertion below scale to a job this file has never
# heard of.
_WORKFLOW_JOB_NAMES: tuple[str, ...] = tuple(_jobs(_load_workflow()))


def test_test_job_exists_alongside_lint_on_ubuntu_latest() -> None:
    jobs = _jobs(_load_workflow())

    assert "lint" in jobs, "the pre-existing lint job must not be removed"
    assert "test" in jobs

    assert jobs["lint"]["runs-on"] == "ubuntu-latest"
    assert jobs["test"]["runs-on"] == "ubuntu-latest"


def test_test_job_checks_out_and_installs_uv_with_the_same_actions_as_lint() -> None:
    workflow = _load_workflow()
    jobs = _jobs(workflow)

    lint_actions = _uses_actions(jobs["lint"])
    test_actions = _uses_actions(jobs["test"])

    checkout_action = next(
        action for action in lint_actions if action.startswith("actions/checkout")
    )
    setup_uv_action = next(
        action for action in lint_actions if action.startswith("astral-sh/setup-uv")
    )

    assert checkout_action in test_actions
    assert setup_uv_action in test_actions


def test_test_job_runs_make_test_as_its_own_step() -> None:
    jobs = _jobs(_load_workflow())

    assert _MAKE_TEST_COMMAND in _run_step_commands(jobs["test"])


def test_test_job_runs_a_coverage_step_naming_the_package_path_and_fail_under() -> None:
    jobs = _jobs(_load_workflow())

    coverage_commands = [
        command for command in _run_step_commands(jobs["test"]) if "--cov" in command
    ]

    assert coverage_commands, (
        "expected a coverage step (a run command containing --cov) in the test job"
    )
    assert any(
        _PACKAGE_COVERAGE_PATH in command and _COVERAGE_FAIL_UNDER_FLAG in command
        for command in coverage_commands
    ), (
        f"expected a coverage step naming both {_PACKAGE_COVERAGE_PATH!r} and "
        f"{_COVERAGE_FAIL_UNDER_FLAG!r}; found commands: {coverage_commands!r}"
    )


def test_coverage_step_is_separate_from_the_make_test_step() -> None:
    """The coverage gate is its own step (Description), not folded into `make test`."""
    jobs = _jobs(_load_workflow())

    test_job_commands = _run_step_commands(jobs["test"])
    make_test_commands = [command for command in test_job_commands if command == _MAKE_TEST_COMMAND]
    coverage_commands = [command for command in test_job_commands if "--cov" in command]

    assert make_test_commands
    assert coverage_commands
    assert set(make_test_commands).isdisjoint(coverage_commands)


def test_test_job_installs_zsh_before_running_make_test() -> None:
    """`tests/test_shellrc.py`'s zsh end-to-end cases need `zsh` on PATH, and

    `ubuntu-latest`'s base image (`actions/runner-images`) does not ship it, so a run
    step naming zsh must appear in the test job before the `make test` step, or the
    zsh-parametrized cases fail with a raw `FileNotFoundError`-shaped assertion instead
    of running for real.
    """
    jobs = _jobs(_load_workflow())
    steps = jobs["test"].get("steps", [])
    run_steps = [(index, step) for index, step in enumerate(steps) if "run" in step]

    zsh_install_indices = _zsh_install_step_indices(run_steps)
    assert zsh_install_indices, (
        f"expected a run step in the test job whose command contains both "
        f"{_ZSH_INSTALL_COMMAND_MARKER!r} and {_ZSH_PACKAGE_MARKER!r} (actually "
        f"installing zsh, not merely mentioning it) so tests/test_shellrc.py's "
        f"zsh-parametrized cases have an interpreter to run against"
    )

    make_test_index = next(index for index, step in run_steps if step["run"] == _MAKE_TEST_COMMAND)
    assert zsh_install_indices[0] < make_test_index, (
        "the zsh install step must run before the make test step"
    )


def test_zsh_install_step_indices_rejects_a_comment_only_mention() -> None:
    """A run step that merely mentions `zsh` in a comment installs nothing.

    A bare substring match on `zsh` alone would be satisfied by a comment or
    unrelated prose; `_zsh_install_step_indices` must require the install
    command shape (`apt-get install`) in the SAME run block, not just the
    package name.
    """
    run_steps = [
        (0, {"run": "# this job needs zsh eventually\necho hello"}),
        (1, {"run": "echo unrelated"}),
    ]

    assert _zsh_install_step_indices(run_steps) == []


def test_zsh_install_step_indices_accepts_a_real_install_command() -> None:
    """A run step whose command actually installs zsh via apt-get is detected."""
    run_steps = [
        (0, {"run": "echo unrelated"}),
        (1, {"run": "set -euo pipefail\nsudo apt-get update\nsudo apt-get install -y zsh"}),
    ]

    assert _zsh_install_step_indices(run_steps) == [1]


def test_test_job_does_not_declare_needs_lint() -> None:
    """`test` and `lint` run in parallel; `test` must not wait on `lint`."""
    jobs = _jobs(_load_workflow())

    assert "needs" not in jobs["test"]


@pytest.mark.parametrize("job_name", _WORKFLOW_JOB_NAMES)
def test_every_run_step_declares_shell_bash(job_name: str) -> None:
    jobs = _jobs(_load_workflow())
    run_steps = [step for step in jobs[job_name].get("steps", []) if "run" in step]

    assert run_steps, f"expected at least one run step in job {job_name!r}"
    for step in run_steps:
        step_label = step.get("name", step["run"])
        assert step.get("shell") == "bash", (
            f"step {step_label!r} in job {job_name!r} does not declare shell: bash"
        )


def test_apt_get_package_name_extracts_the_trailing_token() -> None:
    """`_apt_get_package_name` reads the package that follows `-y`, verbatim."""
    assert _apt_get_package_name("sudo apt-get install -y zsh") == "zsh"


def test_apt_get_package_name_fails_fast_when_no_install_command_is_present() -> None:
    """A command text with no `apt-get install` shape raises, it does not guess."""
    with pytest.raises(AssertionError, match="apt-get install"):
        _apt_get_package_name("echo hello")


def test_makefile_test_row_linux_package_name_reads_the_apt_get_command_not_the_brew_one() -> None:
    """The Linux (`apt-get`) command is read even when the same row's macOS

    (`brew install`) command names a different-looking package immediately
    before it -- proving this reads the `apt-get` command specifically,
    rather than whichever `install <package>`-shaped text appears first in
    the row.
    """
    synthetic_makefile = (
        "TEST_INSTALL_HINT_zsh := brew install not-the-same-package (macOS) or "
        "sudo apt-get install -y zsh (Linux, WSL)\n"
        "help:\n"
        "\t@printf '  %-23s %s\\n' \"test\"                  "
        '"uv, zsh                                 uv: brew install uv   '
        'zsh: $(TEST_INSTALL_HINT_zsh)"\n'
    )

    assert _makefile_test_row_linux_package_name(synthetic_makefile) == "zsh"


def test_ci_zsh_install_step_package_matches_makefile_prerequisites_row() -> None:
    """AC-TEST-006: the CI install step and the host prerequisite docs cannot drift.

    Both halves are extracted independently -- the package name the
    workflow's `Install zsh` step actually installs, and the package name
    the Makefile's `test` PREREQUISITES row documents for a Linux host --
    and compared to each other. A future edit that installs a
    differently-named package in CI, or documents a different one for a
    developer's host, fails here instead of the two silently drifting apart.
    """
    ci_package = _ci_zsh_install_package_name(_load_workflow())
    docs_package = _makefile_test_row_linux_package_name(_makefile_text())

    assert ci_package == docs_package, (
        f"CI's 'Install zsh' step installs {ci_package!r}, but the Makefile's "
        f"'test' PREREQUISITES row documents installing {docs_package!r} on Linux; "
        f"these must name the same package so `make test`'s host prerequisite "
        f"documentation and CI's own install step cannot drift apart"
    )


def test_makefile_parsing_helpers_are_defined_once() -> None:
    """tests/conftest.py owns _makefile_text, _make_variable and _resolve_make_refs.

    This file and tests/test_makefile_contract.py both need identical
    Makefile-variable-resolution logic (AC-TEST-006's cross-check that the
    CI `Install zsh` step and the Makefile's PREREQUISITES row name the same
    package); carrying independent copies in each risked one drifting from
    the other while its sibling suite stayed green. This pins that conftest
    exposes all three helpers and that neither this file's nor
    tests/test_makefile_contract.py's own parsed source defines any of them
    locally, so a re-introduced duplicate fails this test instead of
    drifting unnoticed. Mirrors
    tests/test_render.py::test_shared_fixture_helpers_are_defined_once,
    which pins the same convention for _generated_dir/_example_root.
    """
    shared_helper_names = {"_makefile_text", "_make_variable", "_resolve_make_refs"}
    for name in shared_helper_names:
        assert hasattr(conftest, name), f"conftest must define {name}"

    this_file = Path(__file__)
    sibling_file = this_file.parent / "test_makefile_contract.py"
    for test_file in (this_file, sibling_file):
        source = test_file.read_text(encoding="utf-8")
        tree = ast.parse(source)
        local_helper_names = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name in shared_helper_names
        }
        assert local_helper_names == set(), (
            f"{test_file.name} must not locally define {local_helper_names}; "
            "import from conftest instead"
        )
