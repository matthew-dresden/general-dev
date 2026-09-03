"""Every parameter write in this repository passes its value on stdin, not in argv.

A process's arguments are readable by other processes on the same machine for
as long as the call runs, and are echoed back verbatim by this repository's own
failure translator (`rd_run` hands `"$@"` to `rd_aws_failed`). A value passed as
`--value <secret>` is therefore exposed twice: once in the process table, and
again in any error message. `devcontainer_config.catalog`'s module docstring
states the invariant that closes both -- the value travels inside a
`--cli-input-json` document on the child's stdin -- and
`tests/test_catalog.py` pins it for the Python client.

This module pins the same invariant for the shell entry points, which reach the
store directly rather than through the client. It is a source scan rather than
an execution test on purpose: the property is "no such call exists", which a
test that runs one call cannot establish, and which must hold without an AWS
account or a network to check it.

The scan reads the scripts as text, so it cannot see a value assembled at run
time through a variable holding `--value`. That construction would be a
deliberate evasion rather than the ordinary mistake this guards against, and
`test_the_scan_finds_the_calls_it_claims_to_check` keeps the scan itself honest
by requiring it to have found real `put-parameter` calls to inspect.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_RD_DIR = _REPO_ROOT / ".devcontainer" / "remote-docker"


def _shell_scripts() -> list[Path]:
    return sorted(_RD_DIR.glob("*.sh"))


def _put_parameter_invocations(text: str) -> list[str]:
    """Each `put-parameter` call in `text`, flattened across its line continuations."""
    joined = re.sub(r"\\\n\s*", " ", text)
    return [line.strip() for line in joined.splitlines() if "put-parameter" in line]


def test_shell_scripts_exist_so_this_module_cannot_pass_vacuously() -> None:
    scripts = _shell_scripts()
    assert len(scripts) >= 3, (
        f"expected the remote-docker entry scripts, found {[p.name for p in scripts]}; "
        "the glob may have drifted and every assertion below would scan nothing"
    )


def test_the_scan_finds_the_calls_it_claims_to_check() -> None:
    """A repository that stopped writing parameters would make the assertion below vacuous."""
    found = [
        (path.name, call)
        for path in _shell_scripts()
        for call in _put_parameter_invocations(path.read_text(encoding="utf-8"))
    ]
    assert found, "no put-parameter call found in any shell script; the scan checks nothing"


@pytest.mark.parametrize("script", _shell_scripts(), ids=lambda path: path.name)
def test_no_shell_script_passes_a_parameter_value_in_argv(script: Path) -> None:
    offenders = [
        call for call in _put_parameter_invocations(script.read_text(encoding="utf-8"))
        if "--value" in call
    ]
    assert not offenders, (
        f"{script.name} passes a parameter value as an argument, exposing it in the process "
        f"table and in any error this repository reports: {offenders}\n"
        "Pass it on stdin as a --cli-input-json document instead, the way "
        "devcontainer_config.catalog.write_parameter does."
    )


def test_the_stdin_form_is_actually_used_where_parameters_are_written() -> None:
    """Absence of `--value` is not enough: the calls must use the form that replaced it."""
    for script in _shell_scripts():
        calls = _put_parameter_invocations(script.read_text(encoding="utf-8"))
        for call in calls:
            assert "--cli-input-json" in call, (
                f"{script.name} writes a parameter without --cli-input-json: {call}"
            )
