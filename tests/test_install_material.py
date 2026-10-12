"""`transport.install_material`: the step user-data defers to E6.

`provider/aws/modules/compute/user-data.yaml` installs the paths the daemon
reads and enables its unit, but deliberately writes no certificate and never
starts the daemon: its first start reads material the template does not
deliver, and failing there would abort the rest of cloud-init and leave the
enablement itself undone. Nothing closed that gap, so a fully provisioned
instance whose material had been published still had an empty TLS directory, an
inactive daemon, and nothing listening on its TLS port.

The tests drive a fake runner and never reach AWS. What they pin is the shape
of what would be sent -- above all that the instance *pulls*: the commands name
parameter paths and never carry a value, so no TLS private key is placed in an
SSM command document, where it would be readable from command history for as
long as AWS retains it.
"""

from __future__ import annotations

import importlib
import json
import sys
import uuid
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType

import pytest

_SCRIPTS = Path(__file__).resolve().parents[1] / ".claude" / "plugins" / "devcontainer" / "scripts"

_INSTANCE = "sandbox"
_DAEMON_USER = "dockerd"
_REGION = "us-east-1"
_PROFILE = "default"

# Generated at runtime, never written as literals: an `i-`-prefixed hex id and
# a UUID's digit runs are the exact shapes this repository's own secrets
# scanner keys on, so a hardcoded example would trip `make lint-secrets` -- the
# same reason `tests/conftest.py._synthetic_instance_id` exists.
_INSTANCE_ID = "i-" + uuid.uuid4().hex[:17]
_COMMAND_ID = str(uuid.uuid4())


def _import_transport() -> ModuleType:
    if str(_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(_SCRIPTS))
    return importlib.import_module("devcontainer_config.transport")


class _FakeRunner:
    """A CommandRunner double that answers each aws subcommand it recognizes."""

    def __init__(self, *, status: str = "Success", stderr: str = "", timed_out: bool = False):
        self.calls: list[tuple[tuple[str, ...], float | None]] = []
        self._status = status
        self._stderr = stderr
        self._timed_out = timed_out

    def __call__(self, command: Sequence[str], timeout: float | None):
        transport = _import_transport()
        command = tuple(command)
        self.calls.append((command, timeout))
        result = transport.CommandResult
        if "send-command" in command:
            return result(exit_code=0, stdout=json.dumps({"Command": {"CommandId": _COMMAND_ID}}))
        if "wait" in command:
            if self._timed_out:
                return result(exit_code=-1, timed_out=True)
            return result(exit_code=0)
        if "get-command-invocation" in command:
            return result(
                exit_code=0,
                stdout=json.dumps(
                    {
                        "Status": self._status,
                        "StandardOutputContent": "active\n",
                        "StandardErrorContent": self._stderr,
                    }
                ),
            )
        raise AssertionError(f"unexpected command: {command}")

    def sent_commands(self) -> list[str]:
        """The shell lines carried by the send-command call."""
        for command, _ in self.calls:
            if "send-command" in command:
                parameters = json.loads(command[command.index("--parameters") + 1])
                return list(parameters["commands"])
        raise AssertionError("no send-command was issued")


def _install(runner: _FakeRunner) -> str:
    transport = _import_transport()
    return transport.install_material(
        runner,
        instance=_INSTANCE,
        instance_id=_INSTANCE_ID,
        daemon_user=_DAEMON_USER,
        profile=_PROFILE,
        region=_REGION,
    )


def test_the_commands_fetch_exactly_the_published_set() -> None:
    """Derived from `publication_set`, so what is fetched cannot drift from what was published."""
    transport = _import_transport()
    certs = importlib.import_module("devcontainer_config.certs")
    commands = transport.build_install_commands(
        _INSTANCE, daemon_user=_DAEMON_USER, region=_REGION
    )
    joined = "\n".join(commands)
    published = [entry.parameter_path for entry in certs.publication_set(_INSTANCE)]
    assert published, "publication_set returned nothing; this test would check nothing"
    for path in published:
        assert f"--name {path} " in joined, f"{path} is published but never fetched"
    assert joined.count("get-parameter") == len(published)


def test_no_material_is_ever_placed_in_the_command_document() -> None:
    """The instance pulls. A push would put a private key in SSM command history."""
    transport = _import_transport()
    commands = transport.build_install_commands(
        _INSTANCE, daemon_user=_DAEMON_USER, region=_REGION
    )
    joined = "\n".join(commands)
    assert "BEGIN" not in joined
    assert "PRIVATE KEY" not in joined
    for command in commands:
        assert "get-parameter" not in command or "--name" in command


def test_the_private_key_is_never_briefly_world_readable() -> None:
    """umask precedes every fetch, and the key's own mode is set explicitly."""
    transport = _import_transport()
    certs = importlib.import_module("devcontainer_config.certs")
    commands = transport.build_install_commands(
        _INSTANCE, daemon_user=_DAEMON_USER, region=_REGION
    )
    umask_at = next(i for i, line in enumerate(commands) if line.startswith("umask "))
    first_fetch = next(i for i, line in enumerate(commands) if "get-parameter" in line)
    assert umask_at < first_fetch, "the umask must be set before anything is written"
    assert f'chmod 0600 "$dir/{certs.SERVER_KEY_FILENAME}"' in commands
    assert f'chmod 0644 "$dir/{certs.CA_CERT_FILENAME}"' in commands


def test_the_daemon_is_started_after_the_material_is_written() -> None:
    transport = _import_transport()
    commands = transport.build_install_commands(
        _INSTANCE, daemon_user=_DAEMON_USER, region=_REGION
    )
    last_write = max(i for i, line in enumerate(commands) if "chown" in line or "chmod" in line)
    start_at = next(i for i, line in enumerate(commands) if "restart docker.service" in line)
    assert start_at > last_write, "the daemon must not start before its certificate exists"
    assert any("is-active docker.service" in line for line in commands), (
        "the step must confirm the daemon is running rather than trusting the start's exit code"
    )


def test_a_successful_install_returns_the_instance_output() -> None:
    runner = _FakeRunner()
    assert _install(runner).strip() == "active"
    assert any("send-command" in command for command, _ in runner.calls)
    assert any("wait" in command for command, _ in runner.calls)


def test_the_wait_is_bounded_by_the_configured_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deadline read from the environment, not a fixed sleep."""
    transport = _import_transport()
    monkeypatch.setenv(transport.MATERIAL_INSTALL_TIMEOUT_ENV_VAR, "12")
    runner = _FakeRunner()
    _install(runner)
    waits = [timeout for command, timeout in runner.calls if "wait" in command]
    assert waits == [12.0]


def test_a_timeout_names_the_command_and_the_variable_that_extends_it() -> None:
    transport = _import_transport()
    runner = _FakeRunner(timed_out=True)
    with pytest.raises(transport.MaterialInstallError) as excinfo:
        _install(runner)
    message = str(excinfo.value)
    assert _COMMAND_ID in message
    assert transport.MATERIAL_INSTALL_TIMEOUT_ENV_VAR in message


def test_a_failed_command_carries_the_instance_stderr_and_the_remedy() -> None:
    """The causes that matter are distinguishable only from the instance's own diagnostic."""
    transport = _import_transport()
    runner = _FakeRunner(
        status="Failed", stderr="ParameterNotFound: /devcontainer/sandbox/tls/ca.pem"
    )
    with pytest.raises(transport.MaterialInstallError) as excinfo:
        _install(runner)
    message = str(excinfo.value)
    assert "ParameterNotFound" in message
    assert "Failed" in message
    assert f"make cert-publish INSTANCE={_INSTANCE}" in message
