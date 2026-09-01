"""Tests for devcontainer_config.hostprobe (spec Section 4.5).

The `devcontainer_config.hostprobe` import is deferred into function bodies
(via `_import_hostprobe`) instead of done once at module scope, for the same
reason `tests/test_verify.py` defers `devcontainer_config.verify`: the TDD
RED gate stashes this unit's production-source files and re-runs a single
named test node, and a module-level `from devcontainer_config import
hostprobe` would fail COLLECTION for the whole file (pytest exit code 2, no
test outcome recorded) instead of failing the one test that actually
exercises the missing module (pytest exit 1, a real FAILED result).

`FakeRunner` is this file's only command source. It never shells out: it
looks a recorded `CommandResult` up by the exact command tuple it was
called with, and records every call (command and timeout) it received, so a
test can assert the *exact* sequence of commands a probe issued (AC-TEST-002)
rather than merely that the probe "did something". A command not present in
the fixture map is a test-authoring bug, not a hermetic-suite escape hatch,
so `FakeRunner` raises `AssertionError` naming the unexpected command rather
than falling back to a default result.

No test in this file invokes a real `docker`, `aws` or `git` binary
(AC-TEST-001): every probe is driven exclusively through `FakeRunner`, which
is the only command source any test in this file uses. Hermeticity rests on
that fact, not on the static-import check alone:
`test_no_probe_ever_imports_subprocess` asserts the production module
contains no static `import subprocess` statement, which rules out shelling
out by ordinary means, but (as that test's own docstring notes) does not
rule out a dynamically constructed reference such as `os.system` or
`importlib.import_module('subprocess')`.
"""

from __future__ import annotations

import ast
import importlib
import inspect
from types import ModuleType

import pytest
from conftest import FakeRunner, _synthetic_account_id


def _import_hostprobe() -> ModuleType:
    """Import devcontainer_config.hostprobe from inside a function body.

    See the module docstring for why this is not a module-level import.
    """
    return importlib.import_module("devcontainer_config.hostprobe")


def _synthetic_dev_user_arn(account_id: str | None = None) -> str:
    """An IAM user ARN shaped like `probe_aws_identity`'s fixtures, account id generated at runtime.

    The account-id segment is exactly the shape
    `devcontainer_config.secrets._ACCOUNT_ID_PATTERN` keys on (twelve
    digits), so it is generated fresh here instead of hardcoded, the same
    way `conftest._synthetic_account_id` and
    `tests/test_secrets.py._sample_account_id` already do for the same
    detector -- no AWS-account-shaped digit run ever appears as a literal in
    this file's source text. `account_id` is accepted so a caller that also
    needs the bare id (to embed in a recorded `Account` field) can generate
    it once and reuse it.
    """
    return f"arn:aws:iam::{account_id or _synthetic_account_id()}:user/dev"


# ---------------------------------------------------------------------------
# AC-FUNC-001 / AC-TEST-001 / AC-TEST-002: the runner seam itself.
# ---------------------------------------------------------------------------


def test_no_probe_ever_imports_subprocess() -> None:
    """`hostprobe.py` contains no static `import subprocess` statement anywhere.

    Parsed via `ast` rather than grepped, so a conditional `import
    subprocess` nested inside a function body or an `if` branch still fails
    this test the same way a plain module-level `import subprocess` would.
    This asserts only that no `ast.Import`/`ast.ImportFrom` node names
    `subprocess`; a dynamically constructed reference such as
    `__import__('sub' + 'process')` or `importlib.import_module(name)`
    would not be caught by this specific check. Every external command must
    reach the module through the injected `CommandRunner` parameter instead
    (AC-FUNC-001); a module with no static import of `subprocess` cannot
    shell out from any probe site by ordinary means.
    """
    hostprobe = _import_hostprobe()
    tree = ast.parse(inspect.getsource(hostprobe))
    imported_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported_names.add(node.module)
    assert "subprocess" not in imported_names


def test_module_imports_nothing_outside_the_standard_library() -> None:
    """AC-FUNC-011: only standard-library modules are imported.

    A hardcoded allowlist would drift silently as the standard library
    grows; `sys.stdlib_module_names` (Python 3.10+) is the interpreter's own
    authoritative answer to "is this a standard-library top-level module",
    so this test stays correct without maintenance as new stdlib modules are
    added upstream.
    """
    import sys

    hostprobe = _import_hostprobe()
    tree = ast.parse(inspect.getsource(hostprobe))
    top_level_imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            top_level_imports.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            if node.level == 0:
                top_level_imports.add(node.module.split(".")[0])
    non_stdlib = top_level_imports - set(sys.stdlib_module_names) - {"__future__"}
    assert non_stdlib == set()


def test_operating_system_probe_issues_exactly_the_expected_commands() -> None:
    """AC-TEST-002: the exact command list a probe issues, no more, no fewer.

    A probe that shells out to something unexpected -- an extra `uname -a`,
    a stray `sw_vers` call -- fails this assertion even though the returned
    `ProbeResult` might still look plausible.
    """
    hostprobe = _import_hostprobe()
    runner = FakeRunner(
        {
            ("uname", "-r"): hostprobe.CommandResult(exit_code=0, stdout="23.1.0\n"),
            ("uname", "-s"): hostprobe.CommandResult(exit_code=0, stdout="Darwin\n"),
        }
    )
    hostprobe.probe_operating_system(runner)
    assert runner.calls == [
        (("uname", "-r"), None),
        (("uname", "-s"), None),
    ]


# ---------------------------------------------------------------------------
# AC-FUNC-002: operating system probe, including the WSL case.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("uname_r", "uname_s", "expected_family"),
    [
        pytest.param("23.1.0\n", "Darwin\n", "macos", id="macos"),
        pytest.param("6.5.0-generic\n", "Linux\n", "linux", id="linux"),
        pytest.param(
            "5.15.90.1-microsoft-standard-WSL2\n", "Linux\n", "wsl", id="wsl-lowercase-kernel"
        ),
        pytest.param(
            "5.15.90.1-MICROSOFT-standard-WSL2\n", "Linux\n", "wsl", id="wsl-uppercase-kernel"
        ),
    ],
)
def test_operating_system_probe_reports_the_host_family(
    uname_r: str, uname_s: str, expected_family: str
) -> None:
    hostprobe = _import_hostprobe()
    runner = FakeRunner(
        {
            ("uname", "-r"): hostprobe.CommandResult(exit_code=0, stdout=uname_r),
            ("uname", "-s"): hostprobe.CommandResult(exit_code=0, stdout=uname_s),
        }
    )
    result = hostprobe.probe_operating_system(runner)
    assert result.ok is True
    assert result.found == expected_family


def test_operating_system_probe_does_not_call_uname_s_when_wsl_is_detected() -> None:
    """WSL is decided from `uname -r` alone; `uname -s` there would also say Linux."""
    hostprobe = _import_hostprobe()
    runner = FakeRunner(
        {
            ("uname", "-r"): hostprobe.CommandResult(
                exit_code=0, stdout="5.15.90.1-microsoft-standard-WSL2\n"
            ),
        }
    )
    result = hostprobe.probe_operating_system(runner)
    assert result.ok is True
    assert result.found == "wsl"
    assert runner.calls == [(("uname", "-r"), None)]


def test_operating_system_probe_reports_uname_r_binary_missing() -> None:
    hostprobe = _import_hostprobe()
    runner = FakeRunner(
        {("uname", "-r"): hostprobe.CommandResult(exit_code=127, binary_missing=True)}
    )
    result = hostprobe.probe_operating_system(runner)
    assert result.ok is False
    assert "uname" in result.found


def test_operating_system_probe_reports_uname_r_failure() -> None:
    hostprobe = _import_hostprobe()
    runner = FakeRunner(
        {
            ("uname", "-r"): hostprobe.CommandResult(exit_code=1, stderr="permission denied"),
        }
    )
    result = hostprobe.probe_operating_system(runner)
    assert result.ok is False
    assert "permission denied" in result.found


def test_operating_system_probe_reports_uname_s_failure() -> None:
    hostprobe = _import_hostprobe()
    runner = FakeRunner(
        {
            ("uname", "-r"): hostprobe.CommandResult(exit_code=0, stdout="6.5.0\n"),
            ("uname", "-s"): hostprobe.CommandResult(exit_code=1, stderr="boom"),
        }
    )
    result = hostprobe.probe_operating_system(runner)
    assert result.ok is False
    assert "boom" in result.found


def test_operating_system_probe_rejects_an_unrecognized_family() -> None:
    hostprobe = _import_hostprobe()
    runner = FakeRunner(
        {
            ("uname", "-r"): hostprobe.CommandResult(exit_code=0, stdout="5.10.0\n"),
            ("uname", "-s"): hostprobe.CommandResult(exit_code=0, stdout="SunOS\n"),
        }
    )
    result = hostprobe.probe_operating_system(runner)
    assert result.ok is False
    assert "SunOS" in result.found


# ---------------------------------------------------------------------------
# AC-FUNC-003 / AC-FUNC-004 / AC-TEST-003: the tool probe.
# ---------------------------------------------------------------------------

_TOOL_VERSION_FIXTURES: dict[str, tuple[tuple[str, ...], str, str]] = {
    "docker": (
        ("docker", "--version"),
        "Docker version 28.6.0, build 3c710e0\n",
        "docker 28.6.0",
    ),
    "git": (("git", "--version"), "git version 2.43.0\n", "git 2.43.0"),
    "jq": (("jq", "--version"), "jq-1.7.1\n", "jq 1.7.1"),
    "devcontainer CLI": (
        ("devcontainer", "--version"),
        "0.71.0\n",
        "devcontainer CLI 0.71.0",
    ),
    "uv": (("uv", "--version"), "uv 0.4.18 (7660ff1f1 2024-09-05)\n", "uv 0.4.18"),
}


def _all_tools_present_responses(hostprobe: ModuleType) -> dict[tuple[str, ...], object]:
    """Every prerequisite tool's command mapped to its recorded present-and-parseable output.

    `probe_tools` always issues all five tools' commands, one per tool
    (AC-TEST-003's "parametrizes the tool probe over every prerequisite"), so
    a `FakeRunner` exercising one tool's failure scenario still needs a
    recorded response for the other four; this builds that full map once so
    each scenario test only has to override the one command it cares about.
    """
    return {
        command: hostprobe.CommandResult(exit_code=0, stdout=stdout)
        for command, stdout, _found in _TOOL_VERSION_FIXTURES.values()
    }


@pytest.mark.parametrize("tool_name", sorted(_TOOL_VERSION_FIXTURES))
def test_tool_probe_reports_a_present_and_parseable_tool(tool_name: str) -> None:
    hostprobe = _import_hostprobe()
    command, stdout, expected_found = _TOOL_VERSION_FIXTURES[tool_name]
    responses = _all_tools_present_responses(hostprobe)
    responses[command] = hostprobe.CommandResult(exit_code=0, stdout=stdout)
    runner = FakeRunner(responses)
    results = {result.check: result for result in hostprobe.probe_tools(runner)}
    result = results[f"tool: {tool_name}"]
    assert result.ok is True
    assert result.found == expected_found
    assert result.remedy == ""


@pytest.mark.parametrize("tool_name", sorted(_TOOL_VERSION_FIXTURES))
def test_tool_probe_reports_an_absent_tool_with_its_install_command(tool_name: str) -> None:
    hostprobe = _import_hostprobe()
    command, _stdout, _expected = _TOOL_VERSION_FIXTURES[tool_name]
    responses = _all_tools_present_responses(hostprobe)
    responses[command] = hostprobe.CommandResult(exit_code=127, binary_missing=True)
    runner = FakeRunner(responses)
    results = {result.check: result for result in hostprobe.probe_tools(runner)}
    result = results[f"tool: {tool_name}"]
    assert result.ok is False
    assert result.remedy != ""
    assert tool_name in result.remedy or command[0] in result.remedy


@pytest.mark.parametrize("tool_name", sorted(_TOOL_VERSION_FIXTURES))
def test_tool_probe_distinguishes_unparsable_output_from_absent(tool_name: str) -> None:
    hostprobe = _import_hostprobe()
    command, _stdout, _expected = _TOOL_VERSION_FIXTURES[tool_name]
    responses = _all_tools_present_responses(hostprobe)
    responses[command] = hostprobe.CommandResult(exit_code=0, stdout="not a version string\n")
    runner = FakeRunner(responses)
    results = {result.check: result for result in hostprobe.probe_tools(runner)}
    result = results[f"tool: {tool_name}"]
    assert result.ok is False
    assert "unparsable" in result.found
    assert "not on PATH" not in result.found


@pytest.mark.parametrize("tool_name", sorted(_TOOL_VERSION_FIXTURES))
def test_tool_probe_distinguishes_a_failed_run_from_absent_and_unparsable(
    tool_name: str,
) -> None:
    """A tool on PATH that exits non-zero is its own result, not folded into 'unparsable'."""
    hostprobe = _import_hostprobe()
    command, _stdout, _expected = _TOOL_VERSION_FIXTURES[tool_name]
    responses = _all_tools_present_responses(hostprobe)
    responses[command] = hostprobe.CommandResult(
        exit_code=1, stderr="permission denied reading configuration"
    )
    runner = FakeRunner(responses)
    results = {result.check: result for result in hostprobe.probe_tools(runner)}
    result = results[f"tool: {tool_name}"]
    assert result.ok is False
    assert "permission denied reading configuration" in result.found
    assert "not on PATH" not in result.found
    assert "unparsable" not in result.found
    assert result.remedy != ""


def test_tool_probe_covers_exactly_the_five_prerequisite_tools() -> None:
    hostprobe = _import_hostprobe()
    responses = {
        command: hostprobe.CommandResult(exit_code=0, stdout=stdout)
        for command, stdout, _ in _TOOL_VERSION_FIXTURES.values()
    }
    runner = FakeRunner(responses)
    results = hostprobe.probe_tools(runner)
    checked = {result.check for result in results}
    assert checked == {f"tool: {name}" for name in _TOOL_VERSION_FIXTURES}


# ---------------------------------------------------------------------------
# AC-FUNC-005 / AC-FUNC-006 / AC-TEST-004: docker context probe.
# ---------------------------------------------------------------------------

_CONTEXT_LS_COMMAND = ("docker", "context", "ls", "--format", "{{.Name}}")
_CONTEXT_SHOW_COMMAND = ("docker", "context", "show")
_DOCKER_VERSION_COMMAND = ("docker", "version", "--format", "{{.Server.Version}}")


def _healthy_docker_responses(
    hostprobe: ModuleType,
    *,
    contexts: str = "orbstack\n",
    selected: str = "orbstack\n",
) -> dict[tuple[str, ...], object]:
    """The three docker commands' recorded output for an otherwise-healthy, single-context host.

    Docker context and handshake tests each exercise one failure at a time
    against a host where everything else works (AC-TEST-004's per-scenario
    cases); this builds that shared "everything else works" baseline once
    so every test only has to override the one command under test, mirroring
    `_all_tools_present_responses` for the tool probe.
    """
    return {
        _CONTEXT_LS_COMMAND: hostprobe.CommandResult(exit_code=0, stdout=contexts),
        _CONTEXT_SHOW_COMMAND: hostprobe.CommandResult(exit_code=0, stdout=selected),
        _DOCKER_VERSION_COMMAND: hostprobe.CommandResult(exit_code=0, stdout="28.6.0\n"),
    }


def test_docker_probe_reports_context_list_and_selected_context() -> None:
    hostprobe = _import_hostprobe()
    runner = FakeRunner(
        _healthy_docker_responses(hostprobe, contexts="default\norbstack\ngeneral-dev-sandbox\n")
    )
    results = {result.check: result for result in hostprobe.probe_docker(runner)}
    listing = results["docker: context list"]
    assert listing.ok is True
    assert "default" in listing.found
    assert "orbstack" in listing.found
    assert "general-dev-sandbox" in listing.found
    selected = results["docker: selected context"]
    assert selected.ok is True
    assert selected.found == "orbstack"


def test_docker_probe_reports_zero_contexts() -> None:
    hostprobe = _import_hostprobe()
    runner = FakeRunner(_healthy_docker_responses(hostprobe, contexts="", selected=""))
    results = {result.check: result for result in hostprobe.probe_docker(runner)}
    listing = results["docker: context list"]
    assert listing.ok is True
    assert "0 context(s)" in listing.found
    selected = results["docker: selected context"]
    assert selected.ok is False


@pytest.mark.parametrize(
    ("requested", "expected_ok"),
    [
        pytest.param("orbstack", True, id="context-exists"),
        pytest.param("general-dev-missing", False, id="context-does-not-exist"),
    ],
)
def test_docker_probe_reports_whether_a_named_context_exists(
    requested: str, expected_ok: bool
) -> None:
    hostprobe = _import_hostprobe()
    runner = FakeRunner(_healthy_docker_responses(hostprobe, contexts="default\norbstack\n"))
    results = {result.check: result for result in hostprobe.probe_docker(runner, requested)}
    named = results[f"docker: context '{requested}' exists"]
    assert named.ok is expected_ok
    if not expected_ok:
        assert named.remedy != ""


def test_docker_probe_reports_a_missing_named_context_naming_the_actual_config_file() -> None:
    """The remedy for a missing named context must name where LOCAL_DOCKER_CONTEXT /
    REMOTE_DOCKER_CONTEXT actually live: `.devcontainer/remote-docker/config.env`
    (`docs/environment-files.md`, `Makefile`'s own disconnect error), never `shell.env`,
    which carries no context variable at all.
    """
    hostprobe = _import_hostprobe()
    runner = FakeRunner(_healthy_docker_responses(hostprobe, contexts="default\norbstack\n"))
    results = {
        result.check: result for result in hostprobe.probe_docker(runner, "general-dev-missing")
    }
    named = results["docker: context 'general-dev-missing' exists"]
    assert named.ok is False
    assert ".devcontainer/remote-docker/config.env" in named.remedy
    assert "shell.env" not in named.remedy


def test_docker_probe_skips_named_context_check_when_none_requested() -> None:
    hostprobe = _import_hostprobe()
    runner = FakeRunner(_healthy_docker_responses(hostprobe))
    results = hostprobe.probe_docker(runner)
    checks = {result.check for result in results}
    assert not any(check.startswith("docker: context '") for check in checks)


def test_docker_probe_reports_missing_cli_never_as_an_empty_context_list() -> None:
    """AC-FUNC-006: an absent docker CLI is its own result, not a zero-length list."""
    hostprobe = _import_hostprobe()
    runner = FakeRunner(
        {_CONTEXT_LS_COMMAND: hostprobe.CommandResult(exit_code=127, binary_missing=True)}
    )
    results = hostprobe.probe_docker(runner)
    assert len(results) == 1
    result = results[0]
    assert result.ok is False
    assert "not on PATH" in result.found
    assert "context list" not in result.check


def test_docker_probe_reports_a_failed_context_list_never_as_ok() -> None:
    """A non-zero `docker context ls` exit is its own failing result, not a passing '0 context(s)'.

    Reproduces the case where the docker CLI is present but the context
    store is unreadable (permission denied): the command runs, exits
    non-zero and its stdout is empty, which must never be read as "zero
    contexts configured".
    """
    hostprobe = _import_hostprobe()
    runner = FakeRunner(
        {
            _CONTEXT_LS_COMMAND: hostprobe.CommandResult(
                exit_code=1,
                stderr="permission denied while reading context store",
            )
        }
    )
    results = hostprobe.probe_docker(runner)
    assert len(results) == 1
    result = results[0]
    assert result.check == "docker: context list"
    assert result.ok is False
    assert "permission denied while reading context store" in result.found
    assert "0 context(s)" not in result.found
    assert result.remedy != ""


def test_docker_probe_reports_a_failed_selected_context_distinctly_from_none_selected() -> None:
    """A non-zero `docker context show` exit is distinct from 'no context selected'."""
    hostprobe = _import_hostprobe()
    responses = _healthy_docker_responses(hostprobe)
    responses[_CONTEXT_SHOW_COMMAND] = hostprobe.CommandResult(
        exit_code=1,
        stderr="permission denied while reading context store",
    )
    runner = FakeRunner(responses)
    results = {result.check: result for result in hostprobe.probe_docker(runner)}
    selected = results["docker: selected context"]
    assert selected.ok is False
    assert "permission denied while reading context store" in selected.found
    assert selected.found != "docker context show reported no context"
    assert selected.remedy != "Select a context: docker context use <name>"
    assert selected.remedy != ""


def test_docker_probe_reports_binary_missing_between_the_first_and_second_call() -> None:
    """docker disappearing between `context ls` and `context show` is its own result."""
    hostprobe = _import_hostprobe()
    responses = _healthy_docker_responses(hostprobe)
    responses[_CONTEXT_SHOW_COMMAND] = hostprobe.CommandResult(exit_code=127, binary_missing=True)
    runner = FakeRunner(responses)
    results = {result.check: result for result in hostprobe.probe_docker(runner)}
    selected = results["docker: selected context"]
    assert selected.ok is False
    assert "not on PATH" in selected.found


# ---------------------------------------------------------------------------
# AC-FUNC-009 / AC-FUNC-010 / AC-TEST-005: the docker engine handshake timeout.
# ---------------------------------------------------------------------------


def test_docker_handshake_reports_the_engine_answering() -> None:
    hostprobe = _import_hostprobe()
    runner = FakeRunner(_healthy_docker_responses(hostprobe))
    results = {result.check: result for result in hostprobe.probe_docker(runner)}
    handshake = results["docker: engine answers"]
    assert handshake.ok is True
    assert "28.6.0" in handshake.found


def test_docker_handshake_reports_binary_missing() -> None:
    hostprobe = _import_hostprobe()
    responses = _healthy_docker_responses(hostprobe)
    responses[_DOCKER_VERSION_COMMAND] = hostprobe.CommandResult(exit_code=127, binary_missing=True)
    runner = FakeRunner(responses)
    results = {result.check: result for result in hostprobe.probe_docker(runner)}
    handshake = results["docker: engine answers"]
    assert handshake.ok is False
    assert "not on PATH" in handshake.found


def test_docker_handshake_reports_a_nonzero_exit() -> None:
    hostprobe = _import_hostprobe()
    responses = _healthy_docker_responses(hostprobe)
    responses[_DOCKER_VERSION_COMMAND] = hostprobe.CommandResult(
        exit_code=1, stderr="Cannot connect to the Docker daemon"
    )
    runner = FakeRunner(responses)
    results = {result.check: result for result in hostprobe.probe_docker(runner)}
    handshake = results["docker: engine answers"]
    assert handshake.ok is False
    assert "Cannot connect" in handshake.found


def test_docker_handshake_reports_a_timeout_naming_what_was_awaited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hostprobe = _import_hostprobe()
    monkeypatch.setenv("DOCKER_HANDSHAKE_TIMEOUT", "5")
    responses = _healthy_docker_responses(hostprobe)
    responses[_DOCKER_VERSION_COMMAND] = hostprobe.CommandResult(exit_code=1, timed_out=True)
    runner = FakeRunner(responses)
    results = {result.check: result for result in hostprobe.probe_docker(runner)}
    handshake = results["docker: engine answers"]
    assert handshake.ok is False
    assert "5" in handshake.found
    assert "DOCKER_HANDSHAKE_TIMEOUT" in handshake.found


def test_docker_handshake_reads_timeout_from_its_environment_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-TEST-005: the timeout passed to the runner comes from the env var."""
    hostprobe = _import_hostprobe()
    monkeypatch.setenv("DOCKER_HANDSHAKE_TIMEOUT", "7")
    runner = FakeRunner(_healthy_docker_responses(hostprobe))
    hostprobe.probe_docker(runner)
    handshake_calls = [call for call in runner.calls if call[0] == _DOCKER_VERSION_COMMAND]
    assert handshake_calls == [(_DOCKER_VERSION_COMMAND, 7.0)]


def test_docker_handshake_uses_the_documented_default_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hostprobe = _import_hostprobe()
    monkeypatch.delenv("DOCKER_HANDSHAKE_TIMEOUT", raising=False)
    runner = FakeRunner(_healthy_docker_responses(hostprobe))
    hostprobe.probe_docker(runner)
    handshake_calls = [call for call in runner.calls if call[0] == _DOCKER_VERSION_COMMAND]
    assert handshake_calls == [(_DOCKER_VERSION_COMMAND, 30.0)]


def test_docker_handshake_rejects_an_unparsable_timeout_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hostprobe = _import_hostprobe()
    monkeypatch.setenv("DOCKER_HANDSHAKE_TIMEOUT", "not-a-number")
    runner = FakeRunner(_healthy_docker_responses(hostprobe))
    with pytest.raises(hostprobe.HostProbeError, match="DOCKER_HANDSHAKE_TIMEOUT"):
        hostprobe.probe_docker(runner)


@pytest.mark.parametrize(
    "raw_value",
    ["0", "-5", "nan", "inf", "-inf"],
    ids=["zero", "negative", "nan", "positive-infinity", "negative-infinity"],
)
def test_docker_handshake_rejects_a_non_positive_or_non_finite_timeout(
    monkeypatch: pytest.MonkeyPatch, raw_value: str
) -> None:
    """`float()` accepts 0, negatives, nan and inf; none of those is a usable deadline."""
    hostprobe = _import_hostprobe()
    monkeypatch.setenv("DOCKER_HANDSHAKE_TIMEOUT", raw_value)
    runner = FakeRunner(_healthy_docker_responses(hostprobe))
    with pytest.raises(hostprobe.HostProbeError, match="DOCKER_HANDSHAKE_TIMEOUT"):
        hostprobe.probe_docker(runner)


def test_no_probe_calls_time_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Spec Section 7.2: no sleep, anywhere. Patched to raise if ever called."""
    import time

    hostprobe = _import_hostprobe()

    def _forbidden_sleep(_seconds: float) -> None:
        raise AssertionError("hostprobe must never call time.sleep")

    monkeypatch.setattr(time, "sleep", _forbidden_sleep)
    runner = FakeRunner(
        {
            ("uname", "-r"): hostprobe.CommandResult(exit_code=0, stdout="23.1.0\n"),
            ("uname", "-s"): hostprobe.CommandResult(exit_code=0, stdout="Darwin\n"),
            **_healthy_docker_responses(hostprobe),
            ("docker", "--version"): hostprobe.CommandResult(
                exit_code=0, stdout="Docker version 28.6.0, build 3c710e0\n"
            ),
            ("aws", "sts", "get-caller-identity", "--profile", "default", "--output", "json"): (
                hostprobe.CommandResult(
                    exit_code=0, stdout=f'{{"Arn": "{_synthetic_dev_user_arn()}"}}\n'
                )
            ),
        }
    )
    hostprobe.probe_operating_system(runner)
    hostprobe.probe_docker(runner)
    hostprobe.probe_aws_identity(runner, "default")


# ---------------------------------------------------------------------------
# AC-FUNC-007 / AC-TEST-004: AWS identity probe.
# ---------------------------------------------------------------------------

_AWS_IDENTITY_COMMAND = (
    "aws",
    "sts",
    "get-caller-identity",
    "--profile",
    "sandbox-profile",
    "--output",
    "json",
)


def test_aws_probe_reports_a_valid_session() -> None:
    hostprobe = _import_hostprobe()
    account_id = _synthetic_account_id()
    arn = _synthetic_dev_user_arn(account_id)
    runner = FakeRunner(
        {
            _AWS_IDENTITY_COMMAND: hostprobe.CommandResult(
                exit_code=0,
                stdout=f'{{"UserId": "AID123", "Account": "{account_id}", "Arn": "{arn}"}}\n',
            )
        }
    )
    result = hostprobe.probe_aws_identity(runner, "sandbox-profile")
    assert result.ok is True
    assert result.found == arn


def test_aws_probe_reports_unparsable_json_distinctly_instead_of_raising() -> None:
    """Exit-0 with non-JSON stdout is a `ProbeResult`, not an uncaught `JSONDecodeError`."""
    hostprobe = _import_hostprobe()
    runner = FakeRunner(
        {_AWS_IDENTITY_COMMAND: hostprobe.CommandResult(exit_code=0, stdout="not json at all\n")}
    )
    result = hostprobe.probe_aws_identity(runner, "sandbox-profile")
    assert result.ok is False
    assert "unparsable" in result.found
    assert result.remedy != ""


def test_aws_probe_reports_a_missing_arn_field_distinctly_instead_of_raising() -> None:
    """Exit-0 valid JSON with no `Arn` field is a `ProbeResult`, not an uncaught `KeyError`."""
    hostprobe = _import_hostprobe()
    runner = FakeRunner(
        {
            _AWS_IDENTITY_COMMAND: hostprobe.CommandResult(
                exit_code=0, stdout='{"UserId": "AID123"}\n'
            )
        }
    )
    result = hostprobe.probe_aws_identity(runner, "sandbox-profile")
    assert result.ok is False
    assert "Arn" in result.found
    assert result.remedy != ""


@pytest.mark.parametrize(
    "stdout",
    ["null\n", "5\n", "[]\n", '"a plain string"\n'],
    ids=["json-null", "json-number", "json-array", "json-string"],
)
def test_aws_probe_reports_a_non_object_json_payload_distinctly_instead_of_raising(
    stdout: str,
) -> None:
    """Exit-0 valid JSON that is not an object is a `ProbeResult`, never an uncaught `TypeError`.

    `json.loads` happily parses `null`, a bare number, an array or a bare
    string, none of which supports the `"Arn" not in identity` membership
    check the object case relies on; each must fail this probe cleanly
    instead of raising out of `probe_aws_identity`.
    """
    hostprobe = _import_hostprobe()
    runner = FakeRunner(
        {_AWS_IDENTITY_COMMAND: hostprobe.CommandResult(exit_code=0, stdout=stdout)}
    )
    result = hostprobe.probe_aws_identity(runner, "sandbox-profile")
    assert result.ok is False
    assert result.remedy != ""


def test_aws_probe_reports_an_expired_session_naming_the_login_command() -> None:
    hostprobe = _import_hostprobe()
    runner = FakeRunner(
        {
            _AWS_IDENTITY_COMMAND: hostprobe.CommandResult(
                exit_code=255,
                stderr=(
                    "Error loading SSO Token: Token for "
                    "https://example-sso.identitycenter.example.com/start did not "
                    "contain an access token"
                ),
            )
        }
    )
    result = hostprobe.probe_aws_identity(runner, "sandbox-profile")
    assert result.ok is False
    assert result.remedy == "aws sso login --profile sandbox-profile"


def test_aws_probe_reports_no_credentials_at_all_naming_the_login_command() -> None:
    hostprobe = _import_hostprobe()
    runner = FakeRunner(
        {
            _AWS_IDENTITY_COMMAND: hostprobe.CommandResult(
                exit_code=253, stderr="Unable to locate credentials"
            )
        }
    )
    result = hostprobe.probe_aws_identity(runner, "sandbox-profile")
    assert result.ok is False
    assert result.remedy == "aws sso login --profile sandbox-profile"


def test_aws_probe_substitutes_the_callers_profile_not_a_fixed_name() -> None:
    hostprobe = _import_hostprobe()
    command = (
        "aws",
        "sts",
        "get-caller-identity",
        "--profile",
        "a-different-profile",
        "--output",
        "json",
    )
    runner = FakeRunner(
        {command: hostprobe.CommandResult(exit_code=253, stderr="Unable to locate credentials")}
    )
    result = hostprobe.probe_aws_identity(runner, "a-different-profile")
    assert result.remedy == "aws sso login --profile a-different-profile"


def test_aws_probe_reports_binary_missing() -> None:
    hostprobe = _import_hostprobe()
    runner = FakeRunner(
        {_AWS_IDENTITY_COMMAND: hostprobe.CommandResult(exit_code=127, binary_missing=True)}
    )
    result = hostprobe.probe_aws_identity(runner, "sandbox-profile")
    assert result.ok is False
    assert "not on PATH" in result.found


def test_aws_probe_reports_a_generic_failure_distinctly() -> None:
    hostprobe = _import_hostprobe()
    runner = FakeRunner(
        {
            _AWS_IDENTITY_COMMAND: hostprobe.CommandResult(
                exit_code=254,
                stderr="AccessDenied: not authorized to perform sts:GetCallerIdentity",
            )
        }
    )
    result = hostprobe.probe_aws_identity(runner, "sandbox-profile")
    assert result.ok is False
    assert "AccessDenied" in result.found
    assert result.remedy == "aws sso login --profile sandbox-profile"


# ---------------------------------------------------------------------------
# AC-CYCLE-001: an end-to-end run over every probe, healthy host then
# unhealthy host, each failing result naming its remedy.
# ---------------------------------------------------------------------------


def test_end_to_end_every_probe_passes_against_a_recorded_healthy_host() -> None:
    """Every probe, run together against one self-consistent, fully healthy host."""
    hostprobe = _import_hostprobe()
    responses: dict[tuple[str, ...], object] = {
        ("uname", "-r"): hostprobe.CommandResult(exit_code=0, stdout="23.1.0\n"),
        ("uname", "-s"): hostprobe.CommandResult(exit_code=0, stdout="Darwin\n"),
        **_healthy_docker_responses(hostprobe, contexts="default\norbstack\n"),
        (
            "aws",
            "sts",
            "get-caller-identity",
            "--profile",
            "default",
            "--output",
            "json",
        ): hostprobe.CommandResult(
            exit_code=0, stdout=f'{{"Arn": "{_synthetic_dev_user_arn()}"}}\n'
        ),
    }
    for command, stdout, _found in _TOOL_VERSION_FIXTURES.values():
        responses[command] = hostprobe.CommandResult(exit_code=0, stdout=stdout)
    runner = FakeRunner(responses)

    results: list[object] = [hostprobe.probe_operating_system(runner)]
    results.extend(hostprobe.probe_tools(runner))
    results.extend(hostprobe.probe_docker(runner, "orbstack"))
    results.append(hostprobe.probe_aws_identity(runner, "default"))

    for result in results:
        assert result.ok is True, f"{result.check} unexpectedly failed: {result.found}"
        assert result.remedy == ""
        assert result.prevents != "", f"{result.check} carries no 'prevents' text"


def test_end_to_end_every_failure_names_its_remedy_against_a_recorded_unhealthy_host() -> None:
    """Docker CLI absent, AWS session expired, and the named context missing.

    A single unhealthy host, probed the same way as the healthy one above:
    every failing result must name a concrete remedy, never an empty string
    or a bare "something is wrong".
    """
    hostprobe = _import_hostprobe()
    docker_absent_runner = FakeRunner(
        {_CONTEXT_LS_COMMAND: hostprobe.CommandResult(exit_code=127, binary_missing=True)}
    )
    docker_results = hostprobe.probe_docker(docker_absent_runner, "orbstack")
    assert len(docker_results) == 1
    assert docker_results[0].ok is False
    assert docker_results[0].remedy != ""
    assert docker_results[0].prevents != "", f"{docker_results[0].check} carries no 'prevents' text"

    aws_runner = FakeRunner(
        {
            (
                "aws",
                "sts",
                "get-caller-identity",
                "--profile",
                "default",
                "--output",
                "json",
            ): hostprobe.CommandResult(
                exit_code=255, stderr="ExpiredToken: the security token has expired"
            )
        }
    )
    aws_result = hostprobe.probe_aws_identity(aws_runner, "default")
    assert aws_result.ok is False
    assert aws_result.remedy == "aws sso login --profile default"
    assert aws_result.prevents != "", f"{aws_result.check} carries no 'prevents' text"

    context_runner = FakeRunner(
        _healthy_docker_responses(hostprobe, contexts="default\norbstack\n")
    )
    context_results = {
        result.check: result
        for result in hostprobe.probe_docker(context_runner, "general-dev-sandbox")
    }
    named = context_results["docker: context 'general-dev-sandbox' exists"]
    assert named.ok is False
    assert named.remedy != ""
    assert named.prevents != "", f"{named.check} carries no 'prevents' text"
