"""Tests for devcontainer_config.transport_ssh: the ssh transport.

The `devcontainer_config.transport_ssh` import is deferred into function
bodies (via `_import_transport_ssh`), the same convention
`tests/test_transport.py` documents for its own module: a module-level
import would fail COLLECTION for the whole file instead of failing the one
test for the real reason.

The suite is hermetic in the same ways `tests/test_transport.py` is:
`FakeRunner` (from `tests/conftest.py`) answers every docker command from a
fixed fixture map keyed by the exact command tuple and records every call;
the forward process is a fake whose stdout is a real `os.pipe()` pair, so
`wait_ready`'s selectors-based wait blocks on a genuine file descriptor
without spawning a process; and the one readiness condition that must be
real -- the local port accepting a connection -- is a loopback listener the
test binds and closes itself. No test runs a real `ssh` or `docker` binary
and nothing reaches beyond loopback.

The argv pins are byte-exact where the contract is exact (the forward
vector, the plain context vector) and behavioral where the contract is the
diagnosis text (the Windows guidance for a socket-path destination, the
distinct context/handshake and API-floor failures, teardown on every
failure path).
"""

from __future__ import annotations

import ast
import importlib
import inspect
import os
import socket
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from types import ModuleType
from typing import Any, BinaryIO

import pytest
from conftest import FakeRunner

_CONTEXT = "general-dev-engine-x"
_HOST = "engine-x.example.invalid"
_USER = "dev-x"
_PORT = 2222
_SOCKET_ADDR = "/var/run/docker-x.sock"
_TCP_ADDR = "tcp://localhost:2375"

# An upper-bound safety guard for the one test whose readiness wait runs
# against a real (sub-second) selector cadence -- a bound on how long the
# test may take, never a synchronization mechanism.
_HANG_GUARD_SECONDS = 5.0


def _import_transport_ssh() -> ModuleType:
    """Import devcontainer_config.transport_ssh from inside a function body."""
    return importlib.import_module("devcontainer_config.transport_ssh")


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _readiness_pipe() -> tuple[BinaryIO, BinaryIO]:
    """A real `os.pipe()` pair standing in for the forward process's stdout."""
    read_fd, write_fd = os.pipe()
    return os.fdopen(read_fd, "rb"), os.fdopen(write_fd, "wb")


class FakeLauncher:
    """Records the argument vector it was launched with and returns a stub process."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, command: Sequence[str]) -> object:
        self.calls.append(tuple(command))
        return object()


class _FakeForwardProcess:
    """A stand-in for the launched `ssh` process.

    `.stdout` is a real pipe read end: silent while the write end stays
    open (what `ssh -N` looks like in normal operation), EOF once the write
    end is closed (the child exiting). `wait_ready` selects on it exactly
    as it would a real subprocess's. The write end is kept referenced for
    the object's lifetime so garbage collection never closes it early.
    """

    def __init__(self, *, close_output: bool = False) -> None:
        self.terminated = False
        self.stdout, self._writer = _readiness_pipe()
        if close_output:
            self._writer.close()

    def wait(self, timeout: float | None = None) -> int:
        return 0

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.terminated = True

    def poll(self) -> int | None:
        return 0 if self.terminated else None


def _version_command(context: str) -> tuple[str, ...]:
    return ("docker", "--context", context, "version", "--format", "{{json .Server}}")


def _info_command(context: str) -> tuple[str, ...]:
    return ("docker", "--context", context, "info", "--format", "{{json .SecurityOptions}}")


def _no_such_context_result(transport_ssh: ModuleType, context: str) -> object:
    """The real `docker context inspect <absent-name>` outcome (verified wording)."""
    return transport_ssh.transport.CommandResult(
        exit_code=1,
        stderr=(
            f'context "{context}": context not found: open '
            "/home/user/.docker/contexts/meta/deadbeef/meta.json: no such file or directory"
        ),
    )


def _context_inspect_command(context: str) -> tuple[str, ...]:
    return ("docker", "context", "inspect", context, "--format", "{{json .Endpoints}}")


# ---------------------------------------------------------------------------
# No sleep-based wait anywhere in this module, and no SSH configuration
# file named anywhere in it.
# ---------------------------------------------------------------------------


def test_module_imports_no_time_sleep_or_asyncio_sleep() -> None:
    transport_ssh = _import_transport_ssh()
    source = inspect.getsource(transport_ssh)
    tree = ast.parse(source)
    sleep_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Attribute) and node.func.attr == "sleep")
            or (isinstance(node.func, ast.Name) and node.func.id == "sleep")
        )
    ]
    assert not sleep_calls, (
        f"devcontainer_config.transport_ssh calls a 'sleep' function at least once: {sleep_calls!r}"
    )


def test_module_never_names_an_ssh_configuration_path() -> None:
    """The operator's own ssh configuration is never read, written or named.

    The transport rides the operator's default identity and host-key policy
    untouched; naming either file even in prose would invite a future edit
    to start reading them. Asserted against the whole module source.
    """
    transport_ssh = _import_transport_ssh()
    source = inspect.getsource(transport_ssh)
    for forbidden in (".ssh/config", "known_hosts"):
        assert forbidden not in source, (
            f"devcontainer_config.transport_ssh names {forbidden!r}; the module "
            "must never read, write or reference SSH configuration state"
        )


# ---------------------------------------------------------------------------
# build_forward_argv: the exact vector, per destination form.
# ---------------------------------------------------------------------------


def test_build_forward_argv_unix_destination_is_the_exact_vector() -> None:
    transport_ssh = _import_transport_ssh()

    argv = transport_ssh.build_forward_argv(
        host=_HOST, port=22, user=_USER, docker_addr=_SOCKET_ADDR, local_port=54321
    )

    assert argv == [
        "ssh",
        "-N",
        "-o",
        "BatchMode=yes",
        "-o",
        "ExitOnForwardFailure=yes",
        "-L",
        f"127.0.0.1:54321:{_SOCKET_ADDR}",
        f"{_USER}@{_HOST}",
    ]


def test_build_forward_argv_tcp_destination_forwards_the_tcp_host_and_port() -> None:
    transport_ssh = _import_transport_ssh()

    argv = transport_ssh.build_forward_argv(
        host=_HOST, port=22, user=_USER, docker_addr=_TCP_ADDR, local_port=54321
    )

    forward = argv[argv.index("-L") + 1]
    assert forward == "127.0.0.1:54321:localhost:2375"


def test_build_forward_argv_carries_the_port_flag_only_when_port_differs_from_default() -> None:
    transport_ssh = _import_transport_ssh()

    default_port_argv = transport_ssh.build_forward_argv(
        host=_HOST, port=22, user=_USER, docker_addr=_SOCKET_ADDR, local_port=1
    )
    assert "-p" not in default_port_argv

    custom_port_argv = transport_ssh.build_forward_argv(
        host=_HOST, port=_PORT, user=_USER, docker_addr=_SOCKET_ADDR, local_port=1
    )
    assert custom_port_argv[custom_port_argv.index("-p") + 1] == str(_PORT)


def test_build_forward_argv_never_carries_a_key_flag_or_a_remote_command() -> None:
    transport_ssh = _import_transport_ssh()

    for docker_addr in (_SOCKET_ADDR, _TCP_ADDR):
        argv = transport_ssh.build_forward_argv(
            host=_HOST, port=_PORT, user=_USER, docker_addr=docker_addr, local_port=1
        )
        assert "-N" in argv, "-N is what makes the forward execute no remote command"
        assert not any(
            element.startswith("-i") or "identity" in element.lower() for element in argv
        ), f"no key-file flag may appear: {argv}"
        assert not any(element.endswith(".pem") for element in argv), f"a key path leaked: {argv}"


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("host", f"dev@{_HOST}"),
        ("host", "::1"),
        ("host", f"{_HOST} extra"),
        ("host", f"ssh://{_HOST}"),
        ("user", ""),
        ("user", "de v"),
        ("user", "de@v"),
        ("docker_addr", "var/run/docker.sock"),
        ("docker_addr", ""),
    ],
)
def test_build_forward_argv_rejects_what_the_manifest_validator_rejects(
    field: str, bad_value: str
) -> None:
    """Values are validated through instances.validate_engine_values before any argv."""
    transport_ssh = _import_transport_ssh()
    values: dict[str, Any] = {
        "host": _HOST,
        "port": 22,
        "user": _USER,
        "docker_addr": _SOCKET_ADDR,
    }
    values[field] = bad_value

    with pytest.raises(transport_ssh.SSHTransportError, match=field) as excinfo:
        transport_ssh.build_forward_argv(local_port=1, **values)

    assert "Fix the field named above" in str(excinfo.value)


def test_build_forward_argv_rejects_a_non_integer_port() -> None:
    transport_ssh = _import_transport_ssh()

    with pytest.raises(transport_ssh.SSHTransportError, match="port"):
        transport_ssh.build_forward_argv(
            host=_HOST, port="22", user=_USER, docker_addr=_SOCKET_ADDR, local_port=1
        )


# ---------------------------------------------------------------------------
# The plain docker context vector.
# ---------------------------------------------------------------------------


def test_build_context_argv_is_the_plain_endpoint_with_no_certificate_material() -> None:
    transport_ssh = _import_transport_ssh()

    create = transport_ssh.build_context_argv(context_name=_CONTEXT, port=54321, update=False)
    update = transport_ssh.build_context_argv(context_name=_CONTEXT, port=54321, update=True)

    assert create == [
        "docker",
        "context",
        "create",
        _CONTEXT,
        "--docker",
        "host=tcp://127.0.0.1:54321",
    ]
    assert update[2] == "update"
    assert create[4] == update[4], "the update must carry the identical endpoint value"
    for argv in (create, update):
        assert "ca=" not in argv[4] and "cert=" not in argv[4] and "key=" not in argv[4], (
            "an ssh engine names no certificate material; the endpoint must stay plain"
        )


# ---------------------------------------------------------------------------
# wait_ready: the port accepting a connection, bounded by SSH_FORWARD_TIMEOUT.
# ---------------------------------------------------------------------------


def test_wait_ready_returns_once_the_local_port_accepts_a_connection() -> None:
    transport_ssh = _import_transport_ssh()
    reader, writer = _readiness_pipe()
    port = _free_loopback_port()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", port))
        listener.listen(1)

        transport_ssh.wait_ready(
            transport_ssh.transport.tcp_ready_probe(port),
            reader,
            instance="engine-x",
            endpoint=f"ssh://{_USER}@{_HOST}:{_PORT}",
            docker_addr=_SOCKET_ADDR,
            port=port,
        )

    writer.close()
    reader.close()


def test_wait_ready_raises_the_timeout_naming_endpoint_port_and_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport_ssh = _import_transport_ssh()
    monkeypatch.setenv("SSH_FORWARD_TIMEOUT", "0.3")
    reader, writer = _readiness_pipe()
    port = _free_loopback_port()

    with pytest.raises(transport_ssh.SSHForwardTimeoutError) as excinfo:
        transport_ssh.wait_ready(
            transport_ssh.transport.tcp_ready_probe(port),
            reader,
            instance="engine-x",
            endpoint=f"ssh://{_USER}@{_HOST}:{_PORT}",
            docker_addr=_SOCKET_ADDR,
            port=port,
        )

    message = str(excinfo.value)
    assert f"ssh://{_USER}@{_HOST}:{_PORT}" in message
    assert str(port) in message
    assert "SSH_FORWARD_TIMEOUT" in message
    writer.close()
    reader.close()


def test_wait_ready_states_the_windows_guidance_for_a_socket_path_destination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport_ssh = _import_transport_ssh()
    monkeypatch.setenv("SSH_FORWARD_TIMEOUT", "0.3")
    reader, writer = _readiness_pipe()
    port = _free_loopback_port()

    with pytest.raises(transport_ssh.SSHForwardTimeoutError) as excinfo:
        transport_ssh.wait_ready(
            transport_ssh.transport.tcp_ready_probe(port),
            reader,
            instance="engine-x",
            endpoint=f"ssh://{_USER}@{_HOST}:{_PORT}",
            docker_addr=_SOCKET_ADDR,
            port=port,
        )

    message = str(excinfo.value)
    assert "Windows" in message
    assert "Expose daemon on tcp://localhost:2375 without TLS" in message
    assert "tcp://localhost:2375" in message
    writer.close()
    reader.close()


def test_wait_ready_omits_the_windows_guidance_for_a_tcp_destination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport_ssh = _import_transport_ssh()
    monkeypatch.setenv("SSH_FORWARD_TIMEOUT", "0.3")
    reader, writer = _readiness_pipe()
    port = _free_loopback_port()

    with pytest.raises(transport_ssh.SSHForwardTimeoutError) as excinfo:
        transport_ssh.wait_ready(
            transport_ssh.transport.tcp_ready_probe(port),
            reader,
            instance="engine-x",
            endpoint=f"ssh://{_USER}@{_HOST}:{_PORT}",
            docker_addr=_TCP_ADDR,
            port=port,
        )

    assert "Windows" not in str(excinfo.value)
    writer.close()
    reader.close()


def test_wait_ready_raises_the_death_diagnosis_when_the_child_exits_first() -> None:
    """EOF on the forward process's stdout is the child exiting, a distinct failure."""
    transport_ssh = _import_transport_ssh()
    reader, writer = _readiness_pipe()
    writer.close()

    with pytest.raises(transport_ssh.SSHForwardError, match="exited") as excinfo:
        transport_ssh.wait_ready(
            lambda timeout_seconds: False,
            reader,
            instance="engine-x",
            endpoint=f"ssh://{_USER}@{_HOST}:{_PORT}",
            docker_addr=_SOCKET_ADDR,
            port=1,
        )

    assert f"ssh://{_USER}@{_HOST}:{_PORT}" in str(excinfo.value)
    reader.close()


def test_wait_ready_reads_the_timeout_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A case that shortens the variable observes its own value."""
    transport_ssh = _import_transport_ssh()
    monkeypatch.setenv("SSH_FORWARD_TIMEOUT", "0.2")
    reader, writer = _readiness_pipe()
    port = _free_loopback_port()

    def never_called(timeout_seconds: float) -> bool:
        raise AssertionError("probe should never succeed: nothing listens on the port")

    start = time.monotonic()
    with pytest.raises(transport_ssh.SSHForwardTimeoutError):
        transport_ssh.wait_ready(
            never_called,
            reader,
            instance="engine-x",
            endpoint=f"ssh://{_USER}@{_HOST}:{_PORT}",
            docker_addr=_SOCKET_ADDR,
            port=port,
        )
    elapsed = time.monotonic() - start

    assert elapsed < _HANG_GUARD_SECONDS
    writer.close()
    reader.close()


@pytest.mark.parametrize("raw_value", ["not-a-number", "0", "-5", "nan", "inf"])
def test_wait_ready_rejects_an_unusable_timeout(
    monkeypatch: pytest.MonkeyPatch, raw_value: str
) -> None:
    transport_ssh = _import_transport_ssh()
    monkeypatch.setenv("SSH_FORWARD_TIMEOUT", raw_value)
    reader, writer = _readiness_pipe()

    with pytest.raises(transport_ssh.SSHTransportError, match="SSH_FORWARD_TIMEOUT"):
        transport_ssh.wait_ready(
            lambda timeout_seconds: True,
            reader,
            instance="engine-x",
            endpoint=f"ssh://{_USER}@{_HOST}:{_PORT}",
            docker_addr=_SOCKET_ADDR,
            port=1,
        )
    writer.close()
    reader.close()


def test_wait_ready_reads_the_default_timeout_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport_ssh = _import_transport_ssh()
    monkeypatch.delenv("SSH_FORWARD_TIMEOUT", raising=False)
    budgets: list[float] = []
    reader, writer = _readiness_pipe()
    port = _free_loopback_port()

    def recording_probe(timeout_seconds: float) -> bool:
        budgets.append(timeout_seconds)
        return True

    transport_ssh.wait_ready(
        recording_probe,
        reader,
        instance="engine-x",
        endpoint=f"ssh://{_USER}@{_HOST}:{_PORT}",
        docker_addr=_SOCKET_ADDR,
        port=port,
    )

    assert budgets and budgets[0] > 0
    writer.close()
    reader.close()


# ---------------------------------------------------------------------------
# The connect flow, end to end against fakes.
# ---------------------------------------------------------------------------


def _run_main_connect(
    monkeypatch: pytest.MonkeyPatch,
    transport_ssh: ModuleType,
    *,
    runner: object,
    fake_process: _FakeForwardProcess | None = None,
    docker_addr: str = _SOCKET_ADDR,
    context: str = _CONTEXT,
) -> int:
    """Wire `main(["connect", ...])` to `runner` and a fake process, and run it.

    `transport.allocate_local_port` is replaced by an allocate-and-listen
    fake: the allocated port is bound by a real loopback listener this
    helper opens, so `wait_ready`'s connection attempt genuinely answers
    without any `ssh` process ever running. The launcher seam returns
    `fake_process` (a fresh silent one when none is given).
    """
    process = fake_process if fake_process is not None else _FakeForwardProcess()

    def fake_launcher(command: Sequence[str]) -> _FakeForwardProcess:
        return process

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        real_allocate = transport_ssh.transport.allocate_local_port

        def allocate_and_listen(runner_arg: object, context_name: str) -> int:
            port = real_allocate(runner_arg, context_name)
            listener.bind(("127.0.0.1", port))
            listener.listen(1)
            return port

        monkeypatch.setattr(transport_ssh.transport, "allocate_local_port", allocate_and_listen)
        monkeypatch.setattr(transport_ssh.transport, "subprocess_command_runner", runner)
        monkeypatch.setattr(transport_ssh.transport, "default_process_launcher", fake_launcher)

        exit_code = int(transport_ssh.main(_connect_argv(context=context, docker_addr=docker_addr)))
        assert isinstance(exit_code, int)
        return exit_code


class _RecordingRunner:
    """A runner double that records every command and answers from a function.

    The answer function receives the exact command tuple; anything it does
    not answer raises, so a stray or missing docker call fails the test
    instead of passing silently.
    """

    def __init__(self, answer: Callable[[tuple[str, ...]], object]) -> None:
        self._answer = answer
        self.commands: list[tuple[str, ...]] = []

    def __call__(self, command: Sequence[str], timeout_seconds: float | None) -> object:
        self.commands.append(tuple(command))
        return self._answer(tuple(command))


def _connect_argv(context: str = _CONTEXT, docker_addr: str = _SOCKET_ADDR) -> list[str]:
    return [
        "connect",
        "--name",
        "engine-x",
        "--host",
        _HOST,
        "--port",
        str(_PORT),
        "--user",
        _USER,
        "--docker-addr",
        docker_addr,
        "--context",
        context,
    ]


def test_main_connect_full_flow_creates_the_plain_context_and_prints_the_connected_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    transport_ssh = _import_transport_ssh()
    port_holder: dict[str, int] = {}

    def answer(command: tuple[str, ...]) -> object:
        ok = transport_ssh.transport.CommandResult(exit_code=0)
        if command == _context_inspect_command(_CONTEXT):
            return _no_such_context_result(transport_ssh, _CONTEXT)
        if command[:3] == ("docker", "context", "create"):
            assert command[5] == f"host=tcp://127.0.0.1:{port_holder['port']}", (
                f"the context must address the allocated port: {command}"
            )
            return ok
        if command == ("docker", "context", "use", _CONTEXT):
            return ok
        if command == _version_command(_CONTEXT):
            return transport_ssh.transport.CommandResult(
                exit_code=0, stdout='{"Version":"28.6.0","ApiVersion":"1.51"}'
            )
        if command == _info_command(_CONTEXT):
            return transport_ssh.transport.CommandResult(exit_code=0, stdout="[]")
        raise AssertionError(f"unexpected command: {command}")

    runner = _RecordingRunner(answer)
    process = _FakeForwardProcess()
    held: list[object] = []

    def fake_hold(running_process: _FakeForwardProcess) -> None:
        held.append(running_process)
        running_process.terminate()

    monkeypatch.setattr(transport_ssh, "_run_until_interrupted", fake_hold)

    def allocate_and_listen(runner_arg: object, context_name: str) -> int:
        port = _free_loopback_port()
        listener.bind(("127.0.0.1", port))
        listener.listen(1)
        port_holder["port"] = port
        return port

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        monkeypatch.setattr(transport_ssh.transport, "allocate_local_port", allocate_and_listen)
        monkeypatch.setattr(transport_ssh.transport, "subprocess_command_runner", runner)
        monkeypatch.setattr(
            transport_ssh.transport, "default_process_launcher", lambda command: process
        )

        exit_code = transport_ssh.main(_connect_argv())

    assert exit_code == 0
    assert held == [process], "the hold phase must run with the established process"
    assert process.terminated
    captured = capsys.readouterr()
    connected = [line for line in captured.out.splitlines() if line.startswith("Connected:")]
    assert connected == [
        f"Connected: docker context '{_CONTEXT}' -> ssh://{_USER}@{_HOST}:{_PORT} "
        "over ssh forward (server 28.6.0, api 1.51)."
    ]
    commands = runner.commands
    create_commands = [c for c in commands if c[:3] == ("docker", "context", "create")]
    assert len(create_commands) == 1
    create_value = create_commands[0][5]
    assert create_value == f"host=tcp://127.0.0.1:{port_holder['port']}"
    for forbidden in ("ca=", "cert=", "key="):
        assert forbidden not in create_value, (
            "an ssh engine names no certificate material; the endpoint must stay plain"
        )
    use_index = commands.index(("docker", "context", "use", _CONTEXT))
    create_index = commands.index(create_commands[0])
    assert create_index < use_index, "the context must be created before it is activated"
    assert _version_command(_CONTEXT) in commands, "the handshake never ran"
    assert _info_command(_CONTEXT) in commands, "the rootless probe never ran"


# ---------------------------------------------------------------------------
# Teardown on every failure path: the forward never outlives the error.
# ---------------------------------------------------------------------------


def _run_connect_expecting_failure(
    monkeypatch: pytest.MonkeyPatch,
    transport_ssh: ModuleType,
    *,
    runner: object,
    fake_process: _FakeForwardProcess,
    docker_addr: str = _SOCKET_ADDR,
    timeout_env: dict[str, str] | None = None,
    bind_listener: bool = True,
) -> tuple[int, _FakeForwardProcess]:
    for name, value in (timeout_env or {}).items():
        monkeypatch.setenv(name, value)

    def allocate_and_listen(runner_arg: object, context_name: str) -> int:
        # The docker-call failures under test happen after readiness, so the
        # allocated port is bound by a real listener exactly as the happy
        # path's fake does. `bind_listener=False` serves the readiness-
        # failure test, which needs a port that never answers; it bounds
        # its own wait through SSH_FORWARD_TIMEOUT.
        port = _free_loopback_port()
        if bind_listener:
            listener.bind(("127.0.0.1", port))
            listener.listen(1)
        return port

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        monkeypatch.setattr(transport_ssh.transport, "allocate_local_port", allocate_and_listen)
        monkeypatch.setattr(transport_ssh.transport, "subprocess_command_runner", runner)
        monkeypatch.setattr(
            transport_ssh.transport, "default_process_launcher", lambda command: fake_process
        )
        exit_code = transport_ssh.main(
            [
                "connect",
                "--name",
                "engine-x",
                "--host",
                _HOST,
                "--port",
                str(_PORT),
                "--user",
                _USER,
                "--docker-addr",
                docker_addr,
                "--context",
                _CONTEXT,
            ]
        )
    return exit_code, fake_process


def test_main_connect_stops_the_forward_when_the_context_create_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    transport_ssh = _import_transport_ssh()
    context_command = _context_inspect_command(_CONTEXT)

    def runner(command: Sequence[str], timeout_seconds: float | None) -> object:
        if tuple(command) == context_command:
            return _no_such_context_result(transport_ssh, _CONTEXT)
        return transport_ssh.transport.CommandResult(exit_code=1, stderr="daemon config invalid")

    exit_code, fake_process = _run_connect_expecting_failure(
        monkeypatch, transport_ssh, runner=runner, fake_process=_FakeForwardProcess()
    )

    assert exit_code == 1
    assert fake_process.terminated, "a failed context create must not orphan the ssh child"
    captured = capsys.readouterr()
    assert _CONTEXT in captured.err
    assert "daemon config invalid" in captured.err
    assert "Windows" not in captured.err, (
        "a context failure is not a forward failure; the Windows guidance must not appear"
    )


def test_main_connect_stops_the_forward_when_the_handshake_fails_the_api_floor(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    transport_ssh = _import_transport_ssh()

    def runner(command: Sequence[str], timeout_seconds: float | None) -> object:
        key = tuple(command)
        if key == _context_inspect_command(_CONTEXT):
            return _no_such_context_result(transport_ssh, _CONTEXT)
        if key[:3] == ("docker", "context", "create"):
            return transport_ssh.transport.CommandResult(exit_code=0)
        if key == ("docker", "context", "use", _CONTEXT):
            return transport_ssh.transport.CommandResult(exit_code=0)
        if key == _version_command(_CONTEXT):
            return transport_ssh.transport.CommandResult(
                exit_code=0, stdout='{"Version":"20.10.0","ApiVersion":"1.41"}'
            )
        raise AssertionError(f"unexpected command: {key}")

    exit_code, fake_process = _run_connect_expecting_failure(
        monkeypatch, transport_ssh, runner=runner, fake_process=_FakeForwardProcess()
    )

    assert exit_code == 1
    assert fake_process.terminated
    captured = capsys.readouterr()
    assert "1.41" in captured.err
    assert transport_ssh.transport.DOCKER_API_VERSION_FLOOR in captured.err


def test_main_connect_stops_the_forward_when_the_handshake_never_answers(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    transport_ssh = _import_transport_ssh()

    def runner(command: Sequence[str], timeout_seconds: float | None) -> object:
        key = tuple(command)
        if key == _context_inspect_command(_CONTEXT):
            return _no_such_context_result(transport_ssh, _CONTEXT)
        if key[:3] == ("docker", "context", "create"):
            return transport_ssh.transport.CommandResult(exit_code=0)
        if key == ("docker", "context", "use", _CONTEXT):
            return transport_ssh.transport.CommandResult(exit_code=0)
        if key == _version_command(_CONTEXT):
            return transport_ssh.transport.CommandResult(
                exit_code=1,
                stderr=(
                    "Cannot connect to the Docker daemon at tcp://127.0.0.1:63441. "
                    "Is the docker daemon running?"
                ),
            )
        raise AssertionError(f"unexpected command: {key}")

    exit_code, fake_process = _run_connect_expecting_failure(
        monkeypatch,
        transport_ssh,
        runner=runner,
        fake_process=_FakeForwardProcess(),
        timeout_env={"DOCKER_HANDSHAKE_TIMEOUT": "0.2"},
    )

    assert exit_code == 1
    assert fake_process.terminated
    captured = capsys.readouterr()
    assert _CONTEXT in captured.err
    assert "DOCKER_HANDSHAKE_TIMEOUT" in captured.err
    assert "ssh forward is still established" in captured.err, (
        "the ssh transport's handshake diagnosis must name the ssh forward, never the ssm one"
    )


def test_main_connect_stops_the_forward_when_the_port_never_opens(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    transport_ssh = _import_transport_ssh()
    exit_code, fake_process = _run_connect_expecting_failure(
        monkeypatch,
        transport_ssh,
        runner=FakeRunner({}),
        fake_process=_FakeForwardProcess(),
        timeout_env={"SSH_FORWARD_TIMEOUT": "0.2"},
        bind_listener=False,
    )

    assert exit_code == 1
    assert fake_process.terminated
    captured = capsys.readouterr()
    assert f"ssh://{_USER}@{_HOST}:{_PORT}" in captured.err
    assert "SSH_FORWARD_TIMEOUT" in captured.err
    assert "Windows" in captured.err, (
        "a socket-path destination's failure must carry the Windows guidance"
    )


def test_main_connect_stops_the_forward_when_the_child_exits_before_readiness(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    transport_ssh = _import_transport_ssh()
    exit_code, fake_process = _run_connect_expecting_failure(
        monkeypatch,
        transport_ssh,
        runner=FakeRunner({}),
        fake_process=_FakeForwardProcess(close_output=True),
        timeout_env={"SSH_FORWARD_TIMEOUT": "5"},
    )

    assert exit_code == 1
    assert fake_process.terminated
    captured = capsys.readouterr()
    assert "exited" in captured.err


def test_main_connect_exits_cleanly_on_an_interrupt_before_readiness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same clean-exit contract `_establish_forward` on the ssm side keeps."""
    transport_ssh = _import_transport_ssh()

    def interrupting_wait_ready(*args: object, **kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(transport_ssh, "wait_ready", interrupting_wait_ready)
    exit_code, fake_process = _run_connect_expecting_failure(
        monkeypatch,
        transport_ssh,
        runner=FakeRunner({}),
        fake_process=_FakeForwardProcess(),
    )

    assert exit_code == 0
    assert fake_process.terminated, "an interrupted wait must still stop the child"


def test_main_connect_rejects_an_invalid_instance_name_before_anything_runs(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    transport_ssh = _import_transport_ssh()

    def failing_runner(command: Sequence[str], timeout_seconds: float | None) -> object:
        raise AssertionError("no docker command may run for an invalid name")

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM):
        monkeypatch.setattr(
            transport_ssh.transport,
            "allocate_local_port",
            lambda runner_arg, name: _free_loopback_port(),
        )
        monkeypatch.setattr(transport_ssh.transport, "subprocess_command_runner", failing_runner)
        monkeypatch.setattr(
            transport_ssh.transport,
            "default_process_launcher",
            lambda command: pytest.fail("no forward may launch for an invalid name"),
        )
        exit_code = transport_ssh.main(
            [
                "connect",
                "--name",
                "../escape",
                "--host",
                _HOST,
                "--port",
                str(_PORT),
                "--user",
                _USER,
                "--docker-addr",
                _SOCKET_ADDR,
                "--context",
                _CONTEXT,
            ]
        )

    assert exit_code == 1
    assert "invalid instance name" in capsys.readouterr().err


def test_main_rejects_a_non_integer_port_as_a_usage_error() -> None:
    transport_ssh = _import_transport_ssh()

    with pytest.raises(SystemExit) as excinfo:
        transport_ssh.main(
            [
                "connect",
                "--name",
                "engine-x",
                "--host",
                _HOST,
                "--port",
                "ssh",
                "--user",
                _USER,
                "--docker-addr",
                _SOCKET_ADDR,
                "--context",
                _CONTEXT,
            ]
        )

    assert excinfo.value.code == 2


# ---------------------------------------------------------------------------
# The handshake in isolation: retries, the floor, and the deadline.
# ---------------------------------------------------------------------------


class _SequencedRunner:
    """Answers the identical command with a different result on each call."""

    def __init__(self, results: Sequence[object]) -> None:
        self._results = list(results)
        self.calls: list[tuple[tuple[str, ...], float | None]] = []

    def __call__(self, command: Sequence[str], timeout_seconds: float | None) -> object:
        self.calls.append((tuple(command), timeout_seconds))
        if not self._results:
            raise AssertionError("sequenced runner exhausted its results")
        return self._results.pop(0)


def test_handshake_retries_the_version_call_until_it_answers() -> None:
    transport_ssh = _import_transport_ssh()
    runner = _SequencedRunner(
        [
            transport_ssh.transport.CommandResult(exit_code=1, stderr="connection refused"),
            transport_ssh.transport.CommandResult(
                exit_code=0, stdout='{"Version":"28.6.0","ApiVersion":"1.51"}'
            ),
            transport_ssh.transport.CommandResult(exit_code=0, stdout="[]"),
        ]
    )

    result = transport_ssh.handshake(runner, context_name=_CONTEXT)

    assert result.server_version == "28.6.0"
    assert result.api_version == "1.51"
    assert result.rootless is False
    assert len(runner.calls) == 3


def test_handshake_raises_the_floor_error_naming_both_versions() -> None:
    transport_ssh = _import_transport_ssh()
    runner = _SequencedRunner(
        [
            transport_ssh.transport.CommandResult(
                exit_code=0, stdout='{"Version":"20.10.0","ApiVersion":"1.41"}'
            )
        ]
    )

    with pytest.raises(transport_ssh.DockerVersionFloorError) as excinfo:
        transport_ssh.handshake(runner, context_name=_CONTEXT)

    message = str(excinfo.value)
    assert "1.41" in message
    assert transport_ssh.transport.DOCKER_API_VERSION_FLOOR in message
    assert _CONTEXT in message


def test_handshake_raises_the_deadline_error_naming_the_context_and_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport_ssh = _import_transport_ssh()
    monkeypatch.setenv("DOCKER_HANDSHAKE_TIMEOUT", "0.2")

    def never_answers(command: Sequence[str], timeout_seconds: float | None) -> object:
        return transport_ssh.transport.CommandResult(exit_code=1, stderr="connection refused")

    with pytest.raises(transport_ssh.DockerHandshakeTimeoutError) as excinfo:
        transport_ssh.handshake(never_answers, context_name=_CONTEXT)

    message = str(excinfo.value)
    assert _CONTEXT in message
    assert "DOCKER_HANDSHAKE_TIMEOUT" in message
    assert "ssh forward is still established" in message


def test_handshake_rejects_an_unparsable_server_payload() -> None:
    transport_ssh = _import_transport_ssh()
    runner = _SequencedRunner(
        [transport_ssh.transport.CommandResult(exit_code=0, stdout="not json")]
    )

    with pytest.raises(transport_ssh.SSHTransportError, match=_CONTEXT):
        transport_ssh.handshake(runner, context_name=_CONTEXT)


def test_handshake_rejects_an_unparsable_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    transport_ssh = _import_transport_ssh()
    monkeypatch.setenv("DOCKER_HANDSHAKE_TIMEOUT", "bogus")

    def never_called(command: Sequence[str], timeout_seconds: float | None) -> object:
        raise AssertionError("the runner must never run for an invalid timeout")

    with pytest.raises(transport_ssh.SSHTransportError, match="DOCKER_HANDSHAKE_TIMEOUT"):
        transport_ssh.handshake(never_called, context_name=_CONTEXT)


# ---------------------------------------------------------------------------
# ensure_context and use_context in isolation: the distinct failure texts.
# ---------------------------------------------------------------------------


def test_ensure_context_wraps_an_inspect_failure_as_its_own_error() -> None:
    transport_ssh = _import_transport_ssh()
    runner = FakeRunner(
        {
            _context_inspect_command(_CONTEXT): transport_ssh.transport.CommandResult(
                exit_code=127, binary_missing=True
            )
        }
    )

    with pytest.raises(transport_ssh.SSHTransportError, match="docker is not on PATH"):
        transport_ssh.ensure_context(runner, context_name=_CONTEXT, port=1)


def test_use_context_failure_names_the_context_and_the_stderr() -> None:
    transport_ssh = _import_transport_ssh()
    runner = FakeRunner(
        {
            ("docker", "context", "use", _CONTEXT): transport_ssh.transport.CommandResult(
                exit_code=1, stderr="context not found"
            )
        }
    )

    with pytest.raises(transport_ssh.SSHTransportError, match=_CONTEXT) as excinfo:
        transport_ssh.use_context(runner, _CONTEXT)

    assert "context not found" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Branch coverage: the guards and failure branches each of the reused seams
# carries, exercised directly so no silent branch survives uncovered.
# ---------------------------------------------------------------------------


def test_readline_with_deadline_returns_none_for_a_non_positive_budget() -> None:
    transport_ssh = _import_transport_ssh()
    reader, writer = _readiness_pipe()
    writer.write(b"line\n")

    assert transport_ssh._readline_with_deadline(reader, 0) is None
    assert transport_ssh._readline_with_deadline(reader, -1.0) is None

    writer.close()
    reader.close()


def test_ensure_context_raises_when_docker_is_not_on_path_for_the_create_command() -> None:
    """Distinct from the inspect binary_missing case: the create command's own branch."""
    transport_ssh = _import_transport_ssh()

    def runner(command: Sequence[str], timeout_seconds: float | None) -> object:
        if tuple(command) == _context_inspect_command(_CONTEXT):
            return _no_such_context_result(transport_ssh, _CONTEXT)
        return transport_ssh.transport.CommandResult(exit_code=127, binary_missing=True)

    with pytest.raises(transport_ssh.SSHTransportError, match="docker is not on PATH"):
        transport_ssh.ensure_context(runner, context_name=_CONTEXT, port=1)


def test_use_context_raises_when_docker_is_not_on_path() -> None:
    transport_ssh = _import_transport_ssh()
    runner = FakeRunner(
        {
            ("docker", "context", "use", _CONTEXT): transport_ssh.transport.CommandResult(
                exit_code=127, binary_missing=True
            )
        }
    )

    with pytest.raises(transport_ssh.SSHTransportError, match="docker is not on PATH"):
        transport_ssh.use_context(runner, _CONTEXT)


def test_handshake_raises_on_an_unparsable_api_version() -> None:
    transport_ssh = _import_transport_ssh()
    runner = _SequencedRunner(
        [
            transport_ssh.transport.CommandResult(
                exit_code=0, stdout='{"Version":"28.6.0","ApiVersion":"bogus"}'
            )
        ]
    )

    with pytest.raises(transport_ssh.SSHTransportError, match="bogus"):
        transport_ssh.handshake(runner, context_name=_CONTEXT)


def test_handshake_raises_when_docker_is_not_on_path() -> None:
    transport_ssh = _import_transport_ssh()
    runner = _SequencedRunner(
        [transport_ssh.transport.CommandResult(exit_code=127, binary_missing=True)]
    )

    with pytest.raises(transport_ssh.SSHTransportError, match="docker is not on PATH"):
        transport_ssh.handshake(runner, context_name=_CONTEXT)


def test_probe_rootless_raises_when_docker_is_not_on_path() -> None:
    transport_ssh = _import_transport_ssh()
    runner = _SequencedRunner(
        [
            transport_ssh.transport.CommandResult(
                exit_code=0, stdout='{"Version":"28.6.0","ApiVersion":"1.51"}'
            ),
            transport_ssh.transport.CommandResult(exit_code=127, binary_missing=True),
        ]
    )

    with pytest.raises(transport_ssh.SSHTransportError, match="docker is not on PATH"):
        transport_ssh.handshake(runner, context_name=_CONTEXT)


def test_probe_rootless_raises_when_the_info_call_fails() -> None:
    transport_ssh = _import_transport_ssh()
    runner = _SequencedRunner(
        [
            transport_ssh.transport.CommandResult(
                exit_code=0, stdout='{"Version":"28.6.0","ApiVersion":"1.51"}'
            ),
            transport_ssh.transport.CommandResult(exit_code=1, stderr="connection refused"),
        ]
    )

    with pytest.raises(transport_ssh.SSHTransportError, match=_CONTEXT):
        transport_ssh.handshake(runner, context_name=_CONTEXT)


def test_probe_rootless_raises_on_unparsable_security_options() -> None:
    transport_ssh = _import_transport_ssh()
    runner = _SequencedRunner(
        [
            transport_ssh.transport.CommandResult(
                exit_code=0, stdout='{"Version":"28.6.0","ApiVersion":"1.51"}'
            ),
            transport_ssh.transport.CommandResult(exit_code=0, stdout="not json"),
        ]
    )

    with pytest.raises(transport_ssh.SSHTransportError, match=_CONTEXT):
        transport_ssh.handshake(runner, context_name=_CONTEXT)


def test_handshake_raises_the_deadline_error_instead_of_probing_with_a_zero_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An exhausted deadline must not reach the rootless probe as a zero timeout.

    The same deterministic-clock technique `tests/test_transport.py` uses
    for its own handshake's identical guard: the version call answers, and
    the deadline is already behind the rootless probe's budget check.
    """
    transport_ssh = _import_transport_ssh()
    monkeypatch.setenv("DOCKER_HANDSHAKE_TIMEOUT", "10")
    runner = _SequencedRunner(
        [
            transport_ssh.transport.CommandResult(
                exit_code=0, stdout='{"Version":"28.6.0","ApiVersion":"1.51"}'
            )
        ]
    )
    clock = iter([0.0, 1.0, 10.0])
    monkeypatch.setattr(transport_ssh.time, "monotonic", lambda: next(clock))

    with pytest.raises(transport_ssh.DockerHandshakeTimeoutError) as excinfo:
        transport_ssh.handshake(runner, context_name=_CONTEXT)

    assert _CONTEXT in str(excinfo.value)
    assert _info_command(_CONTEXT) not in [call[0] for call in runner.calls]


class _NoStdoutFakeProcess:
    """A launcher-returned process with no piped stdout -- a launcher bug."""

    stdout = None

    def terminate(self) -> None:
        pass

    def wait(self, timeout: float | None = None) -> int:
        return 0


def test_main_connect_fails_fast_when_the_launched_process_has_no_stdout_pipe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`wait_ready` needs a real pipe to select on; a launcher without one is a bug."""
    transport_ssh = _import_transport_ssh()
    port = _free_loopback_port()

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", port))
        listener.listen(1)
        monkeypatch.setattr(
            transport_ssh.transport, "allocate_local_port", lambda runner_arg, name: port
        )
        monkeypatch.setattr(transport_ssh.transport, "subprocess_command_runner", FakeRunner({}))
        monkeypatch.setattr(
            transport_ssh.transport,
            "default_process_launcher",
            lambda command: _NoStdoutFakeProcess(),
        )

        exit_code = transport_ssh.main(_connect_argv())

    assert exit_code == 1


def test_run_until_interrupted_drains_output_and_stops_the_process(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A child that writes before exiting has its output echoed, then is stopped."""
    transport_ssh = _import_transport_ssh()
    reader, writer = _readiness_pipe()
    writer.write(b"ssh: diagnostic line\n")
    writer.close()

    class DrainingFakeProcess:
        def __init__(self) -> None:
            self.stdout = reader
            self.terminated = False

        def wait(self, timeout: float | None = None) -> int:
            return 0

        def terminate(self) -> None:
            self.terminated = True

    process = DrainingFakeProcess()
    transport_ssh._run_until_interrupted(process)

    assert "ssh: diagnostic line" in capsys.readouterr().out
    assert process.terminated


def test_run_until_interrupted_stops_the_process_on_an_interrupt() -> None:
    transport_ssh = _import_transport_ssh()

    class InterruptingFakeProcess:
        stdout = None

        def __init__(self) -> None:
            self.terminated = False
            self.wait_calls = 0

        def wait(self, timeout: float | None = None) -> int:
            # The operator's interrupt arrives once, during the hold; a
            # teardown wait that followed would be a real child exiting,
            # which is an ordinary return.
            self.wait_calls += 1
            if self.wait_calls == 1:
                raise KeyboardInterrupt
            return 0

        def terminate(self) -> None:
            self.terminated = True

    process = InterruptingFakeProcess()
    transport_ssh._run_until_interrupted(process)

    assert process.terminated
    assert process.wait_calls == 2, "the teardown wait runs after the interrupted one"


def test_module_entry_point_invokes_main_under_the_dunder_guard() -> None:
    """`python3 -m devcontainer_config.transport_ssh` must actually run `main`.

    The same guard pin `tests/test_forwards.py` applies to its own module:
    an entry point without the guard imports and exits 0 silently, and
    every make target behind it would then do nothing successfully.
    """
    transport_ssh = _import_transport_ssh()
    source = Path(transport_ssh.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    guards = [
        node
        for node in tree.body
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Compare)
        and ast.unparse(node.test).replace("'", '"') == '__name__ == "__main__"'
    ]
    assert len(guards) == 1, "exactly one __main__ guard must exist on the module"
    calls = [
        ast.unparse(statement)
        for statement in guards[0].body
        if isinstance(statement, (ast.Expr, ast.Raise))
    ]
    assert any("sys.exit(main(" in call for call in calls), (
        "the __main__ guard must exit through this module's own main()"
    )


def test_wait_ready_ignores_unexpected_stdout_output() -> None:
    """Data on the forward process's stdout is not readiness; the probe decides."""
    transport_ssh = _import_transport_ssh()
    reader, writer = _readiness_pipe()
    writer.write(b"some unexpected stdout line\n")
    port = _free_loopback_port()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", port))
        listener.listen(1)

        transport_ssh.wait_ready(
            transport_ssh.transport.tcp_ready_probe(port),
            reader,
            instance="engine-x",
            endpoint=f"ssh://{_USER}@{_HOST}:{_PORT}",
            docker_addr=_SOCKET_ADDR,
            port=port,
        )

    writer.close()
    reader.close()


def test_wait_ready_never_probes_once_the_deadline_is_exhausted_mid_slice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A budget exhausted inside the wait must reach the probe as never-negative.

    The same deterministic-clock technique `tests/test_transport.py` uses
    for its own readiness guards: the deadline expires between the stdout
    slice ending and the probe budget being computed, and the loop must
    raise the timeout through its own head check rather than hand the probe
    a negative connect timeout.
    """
    transport_ssh = _import_transport_ssh()
    monkeypatch.setenv("SSH_FORWARD_TIMEOUT", "10")
    reader, writer = _readiness_pipe()
    calls: list[float] = []

    def recording_probe(timeout_seconds: float) -> bool:
        calls.append(timeout_seconds)
        return True

    # deadline = 0 + 10 = 10; the loop head sees remaining = 10 - 0 = 10 and
    # waits one stdout slice; the post-slice recheck then sees 10 - 20 < 0,
    # continues, and the loop head raises without ever reaching the probe.
    clock = iter([0.0, 0.0, 20.0, 20.0])
    monkeypatch.setattr(transport_ssh.time, "monotonic", lambda: next(clock))

    with pytest.raises(transport_ssh.SSHForwardTimeoutError):
        transport_ssh.wait_ready(
            recording_probe,
            reader,
            instance="engine-x",
            endpoint=f"ssh://{_USER}@{_HOST}:{_PORT}",
            docker_addr=_SOCKET_ADDR,
            port=1,
        )

    assert calls == [], "a probe must never run on an exhausted deadline"
    writer.close()
    reader.close()


# ---------------------------------------------------------------------------
# The verify flow (make engine-verify): one-shot open -> handshake -> stop.
# ---------------------------------------------------------------------------


def _verify_argv(context: str = _CONTEXT, docker_addr: str = _SOCKET_ADDR) -> list[str]:
    return [
        "verify",
        "--name",
        "engine-x",
        "--host",
        _HOST,
        "--port",
        str(_PORT),
        "--user",
        _USER,
        "--docker-addr",
        docker_addr,
        "--context",
        context,
    ]


def _run_main_verify(
    monkeypatch: pytest.MonkeyPatch,
    transport_ssh: ModuleType,
    *,
    runner: object,
    fake_process: _FakeForwardProcess,
) -> int:
    """Wire `main(["verify", ...])` to `runner` and a fake forward process.

    The same seams `_run_main_connect` wires: the allocated port is bound by
    a real loopback listener so readiness genuinely answers, and the
    launcher seam returns the fake process, whose stdout is a real pipe.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:

        def allocate_and_listen(runner_arg: object, context_name: str) -> int:
            port = _free_loopback_port()
            listener.bind(("127.0.0.1", port))
            listener.listen(1)
            return port

        monkeypatch.setattr(transport_ssh.transport, "allocate_local_port", allocate_and_listen)
        monkeypatch.setattr(transport_ssh.transport, "subprocess_command_runner", runner)
        monkeypatch.setattr(
            transport_ssh.transport, "default_process_launcher", lambda command: fake_process
        )
        exit_code = int(transport_ssh.main(_verify_argv()))
        return exit_code


def _verify_runner(transport_ssh: ModuleType, context: str = _CONTEXT) -> _RecordingRunner:
    """A runner answering the docker surface a verify run touches."""

    def answer(command: tuple[str, ...]) -> object:
        ok = transport_ssh.transport.CommandResult(exit_code=0)
        if command == _context_inspect_command(context):
            return _no_such_context_result(transport_ssh, context)
        if command[:3] == ("docker", "context", "create"):
            return ok
        if command == _version_command(context):
            return transport_ssh.transport.CommandResult(
                exit_code=0, stdout='{"Version":"28.6.0","ApiVersion":"1.51"}'
            )
        if command == _info_command(context):
            return transport_ssh.transport.CommandResult(exit_code=0, stdout="[]")
        raise AssertionError(f"unexpected command: {command}")

    return _RecordingRunner(answer)


def test_main_verify_handshakes_stops_the_forward_and_never_activates_the_context(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """One shot, no daemon left, no context switch: verify creates the
    context (the record of the allocation), handshakes through it, stops the
    forward and prints one verdict line -- and never runs `context use`."""
    transport_ssh = _import_transport_ssh()
    runner = _verify_runner(transport_ssh)
    process = _FakeForwardProcess()

    exit_code = _run_main_verify(monkeypatch, transport_ssh, runner=runner, fake_process=process)

    assert exit_code == 0
    assert process.terminated, "the forward must be stopped after the handshake"
    assert ("docker", "context", "use", _CONTEXT) not in runner.commands, (
        "a proof must not switch the machine-wide docker context"
    )
    assert _version_command(_CONTEXT) in runner.commands
    captured = capsys.readouterr()
    verified = [line for line in captured.out.splitlines() if line.startswith("Verified:")]
    assert verified == [
        f"Verified: docker context '{_CONTEXT}' -> ssh://{_USER}@{_HOST}:{_PORT} "
        "over ssh forward (server 28.6.0, api 1.51)."
    ]


def test_main_verify_stops_the_forward_when_the_handshake_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Every failure path after the launch stops the forward before the
    error propagates, the same discipline connect applies."""
    transport_ssh = _import_transport_ssh()

    def answer(command: tuple[str, ...]) -> object:
        if command == _context_inspect_command(_CONTEXT):
            return _no_such_context_result(transport_ssh, _CONTEXT)
        if command[:3] == ("docker", "context", "create"):
            return transport_ssh.transport.CommandResult(exit_code=0)
        if command == _version_command(_CONTEXT):
            return transport_ssh.transport.CommandResult(
                exit_code=0, stdout='{"Version":"20.10.0","ApiVersion":"1.41"}'
            )
        raise AssertionError(f"unexpected command: {command}")

    runner = _RecordingRunner(answer)
    process = _FakeForwardProcess()
    monkeypatch.setenv("DOCKER_HANDSHAKE_TIMEOUT", "1")

    exit_code = _run_main_verify(monkeypatch, transport_ssh, runner=runner, fake_process=process)

    captured = capsys.readouterr()
    assert exit_code == 1
    assert process.terminated, "a failed verify must not leave the forward running"
    assert "Verified:" not in captured.out
    assert "ERROR" in captured.err
    assert "below the required floor" in captured.err


def test_main_verify_rejects_an_invalid_instance_name_before_anything_runs(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    transport_ssh = _import_transport_ssh()
    argv = _verify_argv()
    argv[argv.index("--name") + 1] = "../escape"
    process = _FakeForwardProcess()
    launched: list[Sequence[str]] = []

    def allocate_and_listen(runner_arg: object, context_name: str) -> int:
        raise AssertionError("no port may be allocated for an invalid name")

    monkeypatch.setattr(transport_ssh.transport, "allocate_local_port", allocate_and_listen)
    monkeypatch.setattr(
        transport_ssh.transport,
        "default_process_launcher",
        lambda command: launched.append(tuple(command)) or process,
    )
    exit_code = int(transport_ssh.main(argv))

    captured = capsys.readouterr()
    assert exit_code == 1
    assert launched == [], "an invalid name must never launch a forward"
    assert "ERROR" in captured.err
    assert "invalid instance name" in captured.err
