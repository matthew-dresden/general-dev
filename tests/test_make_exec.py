"""`make exec` opens a shell in the container; `make shell` reports its removal.

The cutover deleted the SSH transport, and with it the host-shell script and
any interactive path to the EC2 host. That is deliberate: the remote engine is now
reached over an SSM port forward carrying the docker API only, and the host has
no human access path by design.

Two things follow, and this module pins both.

`make exec` is the replacement people actually need -- a shell inside the
container, on whichever engine the active context points at. The shell itself
is an input (`CONTAINER_SHELL`), not a literal, so an image shipping something
other than zsh needs no edit.

`make shell` is kept as a notice that exits non-zero rather than deleted
outright. An operator with the old command in muscle memory, or a script still
calling it, gets a specific explanation and a pointer to `make exec` instead of
make's bare "No rule to make target", which says nothing about why. It is
declared in `tests/data/help-unadvertised.txt` so `make help` does not offer a
capability that no longer exists.

The notice is asserted by running it in a real subprocess, bounded by a
configurable timeout per the no-hardcoded-timeouts rule, because its whole value
is the behaviour a caller observes. Everything else is asserted against source.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_MAKEFILE = _REPO_ROOT / "Makefile"
_CONTAINER_SH = _REPO_ROOT / ".devcontainer" / "remote-docker" / "container.sh"
_CONFIG_ENV = _REPO_ROOT / ".devcontainer" / "remote-docker" / "config.env"
_UNADVERTISED = _REPO_ROOT / "tests" / "data" / "help-unadvertised.txt"

_TIMEOUT_ENV_VAR = "MAKE_SHELL_NOTICE_TEST_TIMEOUT_SECONDS"
_DEFAULT_TIMEOUT_SECONDS = 30.0


def _timeout_seconds() -> float:
    raw = os.environ.get(_TIMEOUT_ENV_VAR)
    if raw is None:
        return _DEFAULT_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        pytest.fail(f"{_TIMEOUT_ENV_VAR}={raw!r} is not a number")
    if value <= 0:
        pytest.fail(f"{_TIMEOUT_ENV_VAR}={raw!r} must be positive")
    return value


def _makefile_text() -> str:
    return _MAKEFILE.read_text(encoding="utf-8")


def _recipe(target: str) -> str:
    text = _makefile_text()
    match = re.search(rf"^{re.escape(target)}:.*?\n((?:\t.*\n)+)", text, re.MULTILINE)
    assert match is not None, f"no recipe found for target {target!r}"
    return match.group(1)


def test_make_shell_reports_its_removal_and_exits_non_zero() -> None:
    """The observable behaviour, run for real: a specific message, not make's default."""
    result = subprocess.run(
        ["make", "shell"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=_timeout_seconds(),
    )
    assert result.returncode != 0, "make shell must fail; the capability is gone"
    combined = result.stdout + result.stderr
    assert "No rule to make target" not in combined, (
        "the target must exist and explain itself, not fall through to make's default error"
    )
    assert "make exec" in combined, "the notice must point at the replacement"
    for token in ("SSH", "SSM"):
        assert token in combined, f"the notice should say why: expected {token!r} in the message"


@pytest.mark.parametrize("target", ["exec", "shell"])
def test_target_is_declared_phony(target: str) -> None:
    phony = re.search(r"^\.PHONY:(.*?)(?<!\\)\n", _makefile_text(), re.MULTILINE | re.DOTALL)
    assert phony is not None, "no .PHONY declaration found"
    assert target in phony.group(1).split(), f"{target} must be .PHONY; it produces no file"


def test_exec_target_delegates_to_container_sh() -> None:
    assert "$(CONTAINER_SH) exec" in _recipe("exec"), (
        "make exec must delegate to container.sh rather than duplicating docker exec logic"
    )


def test_container_sh_dispatches_exec_to_a_distinct_helper() -> None:
    """`exec` dispatches to rdc_exec_shell, not the pre-existing rdc_exec helper.

    rdc_exec already existed as the non-interactive helper rdc_check and others
    use to run one command and capture output. Reusing that name would have
    silently replaced it, so the interactive shell has its own.
    """
    text = _CONTAINER_SH.read_text(encoding="utf-8")
    assert "rdc_exec_shell()" in text
    assert re.search(r"^\s*exec\)\s+rdc_require_docker && rdc_exec_shell ;;", text, re.MULTILINE), (
        "the exec case must dispatch to rdc_exec_shell"
    )
    assert re.search(r"^rdc_exec\(\) \{", text, re.MULTILINE), (
        "the pre-existing rdc_exec helper must survive; other callers depend on it"
    )


def test_the_shell_is_an_input_not_a_literal() -> None:
    """CONTAINER_SHELL is configurable, per the input-driven configuration rule."""
    config = _CONFIG_ENV.read_text(encoding="utf-8")
    assert re.search(r'^: "\$\{CONTAINER_SHELL:=', config, re.MULTILINE), (
        "CONTAINER_SHELL must be declared in config.env's overridable form"
    )
    text = _CONTAINER_SH.read_text(encoding="utf-8")
    body = re.search(r"rdc_exec_shell\(\) \{(.*?)\n\}", text, re.DOTALL)
    assert body is not None
    assert "$CONTAINER_SHELL" in body.group(1), "rdc_exec_shell must honour CONTAINER_SHELL"
    assert "/bin/zsh" not in body.group(1), "the shell must not be hardcoded in the function body"


def test_exec_is_advertised_and_shell_is_declared_unadvertised() -> None:
    """help offers the replacement and does not offer the removed capability."""
    text = _makefile_text()
    assert '"make exec"' in text, "make exec must appear in the help output"
    declarations = _UNADVERTISED.read_text(encoding="utf-8")
    match = re.search(r"^shell:\s*(\S.*)$", declarations, re.MULTILINE)
    assert match is not None, "shell must be declared in help-unadvertised.txt"
    assert match.group(1).strip(), "the declaration must carry a non-empty reason"


def test_no_recipe_reintroduces_a_host_shell_path() -> None:
    """Neither target may reach the host: that path was closed at cutover.

    The check is against invocations, not against the word. The `shell` notice
    legitimately says "SSH" and "SSM" in its explanatory text -- that prose is
    the whole point of keeping the target -- so lines that only print are
    excluded and the executable remainder is what gets asserted.
    """
    for target in ("exec", "shell"):
        commands = [
            line
            for line in _recipe(target).splitlines()
            if line.strip() and "printf" not in line
        ]
        remainder = "\n".join(commands)
        assert not re.search(r"(^|[\s;|&])ssh\b", remainder), (
            f"{target} must not invoke ssh; the host has no interactive path"
        )
        assert "start-session" not in remainder, (
            f"{target} must not open an SSM session to the host; the container is the boundary"
        )


def test_the_shell_notice_explains_itself_in_prose() -> None:
    """The exclusion above must not let the notice lose its explanation.

    Excluding printf lines from the invocation check would also hide the notice
    going silent, so the prose is asserted separately and directly.
    """
    printed = [line for line in _recipe("shell").splitlines() if "printf" in line]
    assert printed, "the shell notice must print something"
    text = "\n".join(printed)
    assert "SSH" in text and "SSM" in text, "the notice must say what replaced what"
    assert "make exec" in text, "the notice must point at the replacement"
