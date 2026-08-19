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
"""

from __future__ import annotations

from typing import Any

import pytest
import yaml
from gitignore_check import repo_root

# Mirrors `[tool.coverage.run] source` in pyproject.toml (E1-F1-S1-T1) -- the
# CI coverage step and the coverage tool's own configuration must name the
# same directory, or the gate in CI measures a different tree than the one
# `make test` and local `pytest --cov` runs measure.
_PACKAGE_COVERAGE_PATH = ".claude/plugins/devcontainer/scripts/devcontainer_config"

# Mirrors `[tool.coverage.report] fail_under` in pyproject.toml.
_COVERAGE_FAIL_UNDER_FLAG = "--cov-fail-under=90"

_MAKE_TEST_COMMAND = "make test"


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
