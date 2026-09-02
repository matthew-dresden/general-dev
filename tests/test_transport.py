"""Tests for devcontainer_config.transport: the SSM port forward manager (E6-F2-S1-T1)
and the docker context / handshake it hands off to (E6-F2-S1-T2).

The `devcontainer_config.transport` import is deferred into function bodies
(via `_import_transport`), the same convention `tests/test_hostprobe.py` and
`tests/test_catalog.py` document: the TDD RED gate stashes this unit's
production-source files and re-runs a single named test node, and a
module-level `from devcontainer_config import transport` would fail
COLLECTION for the whole file (pytest exit 2, no test outcome recorded)
instead of failing the one test that actually exercises the missing module
(pytest exit 1, a real FAILED result). `devcontainer_config.certs` (E6-F1-S1-T1,
E6-F1-S1-T2) is not stashed by this task's own gate -- it already exists on
disk, unchanged by this task's own Changes Manifest -- so it is imported at
module scope below, the same way `tests/test_certs.py` itself imports it.

`FakeRunner` (imported from `tests/conftest.py`) fakes the same
`devcontainer_config.hostprobe.CommandRunner` seam `transport.py` itself
imports that runner type from `hostprobe`. This file imports the one
shared definition `tests/test_hostprobe.py` also imports, rather than
declaring a second copy of the same fake. It never shells out, it answers
from a fixed fixture map keyed by the exact command tuple, and it records
every call it received so a test can assert the exact sequence issued.
`_SequencedRunner`, declared below, is a second, deliberately different
fake: `handshake`'s own retry-until-it-answers loop issues the identical
command tuple more than once with a *different* outcome each time, which
`FakeRunner`'s one-fixed-result-per-command-tuple map cannot express
without changing that map's contract for every other consumer of it,
so it stays local to this file rather than being folded into
`tests/conftest.py`. No test in this file invokes a real `aws` or `docker`
binary (AC-TEST-005): every AWS/docker call in this file goes through
`FakeRunner` or `_SequencedRunner`, and the one test that exercises the
module's real subprocess-backed seams (`subprocess_command_runner`,
`default_process_launcher`) drives them with harmless, network-free host
binaries (`echo`, `tail -f /dev/null`), never a real `aws` or `docker`. The
two tests that need a real TCP peer create and control that peer
themselves, as a loopback listener this file opens and closes -- never a
connection to anything outside this process. Every `x509`/TLS stderr
string this file feeds `diagnose_handshake_failure` and `handshake` is
copied verbatim from a real `docker version`/`docker --context ... version`
invocation this task's author ran against a real docker CLI and a real,
deliberately mis-issued certificate (never invented prose), so a drift
between this file's fixtures and what the real docker CLI actually prints
would show up as a real production diagnosis failing to match, not merely
as a synthetic string this file made up agreeing with itself.
"""

from __future__ import annotations

import ast
import datetime
import importlib
import inspect
import os
import socket
import subprocess
import threading
import time
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType
from typing import BinaryIO

import pytest
from conftest import FakeRunner, _synthetic_instance_id
from devcontainer_config import certs

# A single, runtime-generated, production-shaped (`i-` plus 17 lowercase hex)
# instance id shared by every test in this file, rather than a hand-typed
# literal repeated at each call site (AC-TEST-002 through AC-TEST-005 all
# exercise it indirectly).
_INSTANCE_ID = _synthetic_instance_id(17)

# An upper-bound safety guard for tests that assert a condition-based wait
# (a thread join, an already-bounded `ForwardTimeoutError` path) completes
# promptly rather than hanging the suite -- not a sleep and not a
# synchronization mechanism, so it does not violate the no-time-based-wait
# rule, but a single named constant instead of the same literal typed twice.
_HANG_GUARD_SECONDS = 5.0


def _import_transport() -> ModuleType:
    """Import devcontainer_config.transport from inside a function body.

    See the module docstring for why this is not a module-level import.
    """
    return importlib.import_module("devcontainer_config.transport")


class FakeLauncher:
    """Records the argument vector it was launched with and returns a stub process."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, command: Sequence[str]) -> object:
        self.calls.append(tuple(command))
        return object()


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _readiness_pipe() -> tuple[BinaryIO, BinaryIO]:
    """A real `os.pipe()` pair standing in for a session process's `stdout`.

    `wait_ready` blocks on the read end becoming readable through
    `selectors`, exactly as it would on a real subprocess's `stdout` pipe;
    this hermetic pair (AC-TEST-005: no network, no subprocess) gives it a
    real file descriptor to select on without spawning a process. The
    caller owns closing both ends.
    """
    read_fd, write_fd = os.pipe()
    return os.fdopen(read_fd, "rb"), os.fdopen(write_fd, "wb")


# The exact line the real `session-manager-plugin` prints to stdout once the
# local end of an `AWS-StartPortForwardingSession` forward is listening
# (verified wording: "Port <n> opened for sessionId <id>."). `wait_ready`
# matches on the "opened for sessionId" substring, not the full line, so
# these fixtures need only be realistic, not byte-exact.
_PORT_OPENED_LINE = b"Port 54321 opened for sessionId session-01.\n"


def _context_inspect_command(context_name: str) -> tuple[str, ...]:
    return ("docker", "context", "inspect", context_name, "--format", "{{json .Endpoints}}")


def _no_such_context_result(transport: ModuleType, context_name: str) -> object:
    """The real `docker context inspect <absent-name>` outcome (verified against docker 28.6.0).

    Positively identifies "this context does not exist" by the exact
    stderr docker itself prints, naming `context_name`, rather than a
    generic non-zero exit a test might confuse with any other docker
    failure.
    """
    return transport.CommandResult(
        exit_code=1,
        stderr=(
            f'context "{context_name}": context not found: open '
            "/home/user/.docker/contexts/meta/deadbeef/meta.json: no such file or directory"
        ),
    )


def _identity_command(instance_id: str, profile: str, region: str) -> tuple[str, ...]:
    return (
        "aws",
        "ssm",
        "describe-instance-information",
        "--filters",
        f"Key=InstanceIds,Values={instance_id}",
        "--profile",
        profile,
        "--region",
        region,
        "--output",
        "json",
    )


# ---------------------------------------------------------------------------
# AC-FUNC-006 / AC-TEST-004: no sleep-based wait anywhere in this module.
# ---------------------------------------------------------------------------


def test_module_imports_no_time_sleep_or_asyncio_sleep() -> None:
    transport = _import_transport()
    source = inspect.getsource(transport)
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
        f"devcontainer_config.transport calls a 'sleep' function at least once: {sleep_calls!r}"
    )


# ---------------------------------------------------------------------------
# Preconditions: agent status and credential expiry (AC-FUNC-005).
# ---------------------------------------------------------------------------


def test_agent_status_returns_the_reported_ping_status() -> None:
    transport = _import_transport()
    command = _identity_command(_INSTANCE_ID, "sandbox", "us-east-1")
    runner = FakeRunner(
        {
            command: transport.CommandResult(
                exit_code=0,
                stdout='{"InstanceInformationList": [{"PingStatus": "Online"}]}',
            )
        }
    )

    status = transport.agent_status(
        runner, instance_id=_INSTANCE_ID, profile="sandbox", region="us-east-1"
    )

    assert status == "Online"
    assert runner.calls == [(command, None)]


def test_agent_status_returns_none_when_ssm_has_no_record() -> None:
    transport = _import_transport()
    command = _identity_command(_INSTANCE_ID, "sandbox", "us-east-1")
    runner = FakeRunner(
        {command: transport.CommandResult(exit_code=0, stdout='{"InstanceInformationList": []}')}
    )

    status = transport.agent_status(
        runner, instance_id=_INSTANCE_ID, profile="sandbox", region="us-east-1"
    )

    assert status is None


def test_ensure_agent_online_raises_naming_the_status_when_not_online() -> None:
    transport = _import_transport()
    command = _identity_command(_INSTANCE_ID, "sandbox", "us-east-1")
    runner = FakeRunner(
        {
            command: transport.CommandResult(
                exit_code=0,
                stdout='{"InstanceInformationList": [{"PingStatus": "ConnectionLost"}]}',
            )
        }
    )

    with pytest.raises(transport.AgentNotOnlineError, match="ConnectionLost"):
        transport.ensure_agent_online(
            runner, instance_id=_INSTANCE_ID, profile="sandbox", region="us-east-1"
        )


def test_ensure_agent_online_raises_naming_none_when_ssm_has_no_record() -> None:
    transport = _import_transport()
    command = _identity_command(_INSTANCE_ID, "sandbox", "us-east-1")
    runner = FakeRunner(
        {command: transport.CommandResult(exit_code=0, stdout='{"InstanceInformationList": []}')}
    )

    with pytest.raises(transport.AgentNotOnlineError, match="None"):
        transport.ensure_agent_online(
            runner, instance_id=_INSTANCE_ID, profile="sandbox", region="us-east-1"
        )


def test_ensure_agent_online_raises_credentials_expired_naming_sso_login() -> None:
    transport = _import_transport()
    command = _identity_command(_INSTANCE_ID, "sandbox", "us-east-1")
    runner = FakeRunner(
        {
            command: transport.CommandResult(
                exit_code=254,
                stderr="Error loading SSO Token: Token has expired",
            )
        }
    )

    with pytest.raises(transport.CredentialsExpiredError, match="aws sso login --profile sandbox"):
        transport.ensure_agent_online(
            runner, instance_id=_INSTANCE_ID, profile="sandbox", region="us-east-1"
        )


def test_ensure_agent_online_passes_when_online() -> None:
    transport = _import_transport()
    command = _identity_command(_INSTANCE_ID, "sandbox", "us-east-1")
    runner = FakeRunner(
        {
            command: transport.CommandResult(
                exit_code=0,
                stdout='{"InstanceInformationList": [{"PingStatus": "Online"}]}',
            )
        }
    )

    transport.ensure_agent_online(
        runner, instance_id=_INSTANCE_ID, profile="sandbox", region="us-east-1"
    )


def test_agent_status_raises_transport_error_when_aws_is_not_on_path() -> None:
    transport = _import_transport()
    command = _identity_command(_INSTANCE_ID, "sandbox", "us-east-1")
    runner = FakeRunner({command: transport.CommandResult(exit_code=127, binary_missing=True)})

    with pytest.raises(transport.TransportError, match="aws is not on PATH"):
        transport.agent_status(
            runner, instance_id=_INSTANCE_ID, profile="sandbox", region="us-east-1"
        )


def test_agent_status_raises_transport_error_on_an_unclassified_aws_failure() -> None:
    transport = _import_transport()
    command = _identity_command(_INSTANCE_ID, "sandbox", "us-east-1")
    runner = FakeRunner(
        {command: transport.CommandResult(exit_code=254, stderr="InternalServerError")}
    )

    with pytest.raises(transport.TransportError, match="InternalServerError"):
        transport.agent_status(
            runner, instance_id=_INSTANCE_ID, profile="sandbox", region="us-east-1"
        )


def test_agent_status_raises_transport_error_on_unparsable_json() -> None:
    transport = _import_transport()
    command = _identity_command(_INSTANCE_ID, "sandbox", "us-east-1")
    runner = FakeRunner({command: transport.CommandResult(exit_code=0, stdout="not json")})

    with pytest.raises(transport.TransportError, match="unparsable output"):
        transport.agent_status(
            runner, instance_id=_INSTANCE_ID, profile="sandbox", region="us-east-1"
        )


# ---------------------------------------------------------------------------
# Port allocation and the record (AC-FUNC-002, AC-FUNC-003, AC-TEST-002).
# ---------------------------------------------------------------------------


def test_allocate_local_port_reuses_the_port_recorded_in_an_existing_context() -> None:
    transport = _import_transport()
    recorded_port = _free_loopback_port()
    command = _context_inspect_command("general-dev-sandbox")
    runner = FakeRunner(
        {
            command: transport.CommandResult(
                exit_code=0, stdout=f'{{"docker":{{"Host":"tcp://127.0.0.1:{recorded_port}"}}}}'
            )
        }
    )

    allocated = transport.allocate_local_port(runner, "general-dev-sandbox")

    assert allocated == recorded_port


def test_allocate_local_port_binds_a_fresh_port_when_no_context_exists() -> None:
    transport = _import_transport()
    command = _context_inspect_command("general-dev-sandbox")
    runner = FakeRunner({command: _no_such_context_result(transport, "general-dev-sandbox")})

    allocated = transport.allocate_local_port(runner, "general-dev-sandbox")

    assert 0 < allocated <= 65535


def test_allocate_local_port_raises_when_docker_is_not_on_path() -> None:
    """FAIL_FAST: a missing docker CLI must never be read as 'nothing recorded to reuse'."""
    transport = _import_transport()
    command = _context_inspect_command("general-dev-sandbox")
    runner = FakeRunner({command: transport.CommandResult(exit_code=127, binary_missing=True)})

    with pytest.raises(transport.TransportError, match="docker is not on PATH"):
        transport.allocate_local_port(runner, "general-dev-sandbox")


def test_allocate_local_port_raises_on_a_generic_docker_context_inspect_failure() -> None:
    """FAIL_FAST: a docker failure that is not 'no such context' must never allocate a fresh port.

    An unreadable or misdirected `DOCKER_CONFIG`, a permission error, or a
    corrupted context store all exit non-zero without ever mentioning
    'context not found', and each would silently strand an existing
    context's recorded port if read as 'nothing to reuse'.
    """
    transport = _import_transport()
    command = _context_inspect_command("general-dev-sandbox")
    runner = FakeRunner(
        {
            command: transport.CommandResult(
                exit_code=1,
                stderr="permission denied: /home/user/.docker/contexts/meta/deadbeef/meta.json",
            )
        }
    )

    with pytest.raises(transport.TransportError, match="general-dev-sandbox") as excinfo:
        transport.allocate_local_port(runner, "general-dev-sandbox")

    assert "permission denied" in str(excinfo.value)


def test_allocate_local_port_raises_when_the_recorded_port_is_occupied() -> None:
    transport = _import_transport()
    occupied = _free_loopback_port()
    command = _context_inspect_command("general-dev-sandbox")
    runner = FakeRunner(
        {
            command: transport.CommandResult(
                exit_code=0, stdout=f'{{"docker":{{"Host":"tcp://127.0.0.1:{occupied}"}}}}'
            )
        }
    )

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as holder:
        holder.bind(("127.0.0.1", occupied))
        holder.listen(1)

        with pytest.raises(transport.PortOccupiedError, match=str(occupied)):
            transport.allocate_local_port(runner, "general-dev-sandbox")


def test_allocate_local_port_reuses_a_recorded_port_left_in_time_wait_by_a_previous_forward() -> (
    None
):
    """Regression test for the missing `SO_REUSEADDR` on the occupancy probe socket.

    A prior forward's connection through this exact local port winds down
    in `TIME_WAIT` after this module tears the session down; no process
    holds the port anymore, so `allocate_local_port` must still reuse it,
    the same as it would an idle, never-used port, rather than raising
    `PortOccupiedError` with a "foreign process" remedy that would find
    nothing.
    """
    transport = _import_transport()
    port = _free_loopback_port()

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", port))
    server.listen(1)
    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    client.connect(("127.0.0.1", port))
    conn, _ = server.accept()
    server.close()
    conn.close()  # the server side actively closes first: it enters TIME_WAIT
    client.close()

    command = _context_inspect_command("general-dev-sandbox")
    runner = FakeRunner(
        {
            command: transport.CommandResult(
                exit_code=0, stdout=f'{{"docker":{{"Host":"tcp://127.0.0.1:{port}"}}}}'
            )
        }
    )

    assert transport.allocate_local_port(runner, "general-dev-sandbox") == port


def test_allocate_local_port_gives_two_instances_distinct_ports() -> None:
    transport = _import_transport()

    runner_a = FakeRunner(
        {
            _context_inspect_command("general-dev-alpha"): _no_such_context_result(
                transport, "general-dev-alpha"
            )
        }
    )
    port_a = transport.allocate_local_port(runner_a, "general-dev-alpha")

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as holder:
        holder.bind(("127.0.0.1", port_a))
        holder.listen(1)

        runner_b = FakeRunner(
            {
                _context_inspect_command("general-dev-beta"): _no_such_context_result(
                    transport, "general-dev-beta"
                )
            }
        )
        port_b = transport.allocate_local_port(runner_b, "general-dev-beta")

        assert port_b != port_a


def test_allocate_local_port_raises_on_an_unparsable_endpoint() -> None:
    transport = _import_transport()
    command = _context_inspect_command("general-dev-sandbox")
    runner = FakeRunner({command: transport.CommandResult(exit_code=0, stdout="not json")})

    with pytest.raises(transport.TransportError, match="general-dev-sandbox"):
        transport.allocate_local_port(runner, "general-dev-sandbox")


def test_allocate_local_port_raises_on_a_non_tcp_endpoint() -> None:
    transport = _import_transport()
    command = _context_inspect_command("general-dev-sandbox")
    runner = FakeRunner(
        {
            command: transport.CommandResult(
                exit_code=0, stdout='{"docker":{"Host":"npipe:////./pipe/docker_engine"}}'
            )
        }
    )

    with pytest.raises(transport.TransportError, match="non-TCP endpoint"):
        transport.allocate_local_port(runner, "general-dev-sandbox")


# ---------------------------------------------------------------------------
# Readiness (AC-FUNC-004, AC-TEST-003).
# ---------------------------------------------------------------------------


def test_wait_ready_returns_once_the_session_announces_the_port_opened() -> None:
    transport = _import_transport()
    reader, writer = _readiness_pipe()
    calls: list[float] = []

    def probe(timeout_seconds: float) -> bool:
        calls.append(timeout_seconds)
        return True

    writer.write(b"Starting session with SessionId: session-01\n")
    writer.write(_PORT_OPENED_LINE)
    writer.close()

    transport.wait_ready(probe, reader, instance_id=_INSTANCE_ID, port=54321)

    assert len(calls) == 1
    reader.close()


def test_wait_ready_ignores_lines_that_do_not_announce_the_port_opened() -> None:
    """Only 'opened for sessionId' triggers a probe attempt; other output is not readiness."""
    transport = _import_transport()
    reader, writer = _readiness_pipe()
    calls: list[float] = []

    def probe(timeout_seconds: float) -> bool:
        calls.append(timeout_seconds)
        return True

    writer.write(b"Starting session with SessionId: session-01\n")
    writer.write(b"Waiting for connections...\n")
    writer.write(_PORT_OPENED_LINE)
    writer.close()

    transport.wait_ready(probe, reader, instance_id=_INSTANCE_ID, port=54321)

    assert len(calls) == 1
    reader.close()


def test_wait_ready_does_not_probe_before_the_session_announces_readiness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression test for the busy-spin code_review measured (40,825 iterations/second).

    `probe` must never run while the session process has said nothing:
    that is the condition that produced the busy spin, since the
    production `tcp_ready_probe` returns instantly on `ECONNREFUSED`.
    """
    transport = _import_transport()
    monkeypatch.setenv("SSM_FORWARD_TIMEOUT", "0.3")
    reader, writer = _readiness_pipe()
    calls: list[float] = []

    def probe(timeout_seconds: float) -> bool:
        calls.append(timeout_seconds)
        return False

    with pytest.raises(transport.ForwardTimeoutError):
        transport.wait_ready(probe, reader, instance_id="i-x", port=1)

    assert calls == []
    writer.close()
    reader.close()


def test_wait_ready_raises_forward_timeout_error_when_the_process_exits_without_announcing() -> (
    None
):
    """The session process closing its stdout (EOF) without ever announcing is a real failure."""
    transport = _import_transport()
    reader, writer = _readiness_pipe()
    writer.close()

    with pytest.raises(transport.ForwardTimeoutError, match=_INSTANCE_ID):
        transport.wait_ready(
            lambda timeout_seconds: True, reader, instance_id=_INSTANCE_ID, port=54321
        )
    reader.close()


def test_wait_ready_keeps_waiting_past_an_announcement_the_probe_does_not_confirm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rare race: the session announces readiness but the probe does not (yet) confirm it.

    No further announcement is coming (the plugin only prints it once), so
    this must still raise `ForwardTimeoutError`, bounded by
    `SSM_FORWARD_TIMEOUT`, rather than return successfully or hang.
    """
    transport = _import_transport()
    monkeypatch.setenv("SSM_FORWARD_TIMEOUT", "0.3")
    reader, writer = _readiness_pipe()
    calls: list[float] = []

    def probe(timeout_seconds: float) -> bool:
        calls.append(timeout_seconds)
        return False

    writer.write(_PORT_OPENED_LINE)
    writer.close()

    with pytest.raises(transport.ForwardTimeoutError):
        transport.wait_ready(probe, reader, instance_id="i-x", port=1)

    assert len(calls) == 1
    reader.close()


def test_wait_ready_reads_the_default_timeout_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = _import_transport()
    monkeypatch.delenv("SSM_FORWARD_TIMEOUT", raising=False)
    reader, writer = _readiness_pipe()
    recorded: list[float] = []

    def probe(timeout_seconds: float) -> bool:
        recorded.append(timeout_seconds)
        return True

    writer.write(_PORT_OPENED_LINE)
    writer.close()

    transport.wait_ready(probe, reader, instance_id=_INSTANCE_ID, port=12345)

    assert recorded[0] == pytest.approx(30.0, abs=0.5)
    reader.close()


def test_wait_ready_reads_a_shortened_timeout_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _import_transport()
    monkeypatch.setenv("SSM_FORWARD_TIMEOUT", "7")
    reader, writer = _readiness_pipe()
    recorded: list[float] = []

    def probe(timeout_seconds: float) -> bool:
        recorded.append(timeout_seconds)
        return True

    writer.write(_PORT_OPENED_LINE)
    writer.close()

    transport.wait_ready(probe, reader, instance_id=_INSTANCE_ID, port=12345)

    assert recorded[0] == pytest.approx(7.0, abs=0.5)
    reader.close()


def test_wait_ready_raises_forward_timeout_error_naming_port_and_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _import_transport()
    monkeypatch.setenv("SSM_FORWARD_TIMEOUT", "0.2")
    reader, writer = _readiness_pipe()

    def never_called(timeout_seconds: float) -> bool:
        raise AssertionError("probe should never run: the session never announced readiness")

    start = time.monotonic()
    with pytest.raises(transport.ForwardTimeoutError) as excinfo:
        transport.wait_ready(never_called, reader, instance_id=_INSTANCE_ID, port=54321)
    elapsed = time.monotonic() - start

    assert elapsed < _HANG_GUARD_SECONDS
    message = str(excinfo.value)
    assert "54321" in message
    assert "SSM_FORWARD_TIMEOUT" in message
    assert _INSTANCE_ID in message
    writer.close()
    reader.close()


def test_wait_ready_rejects_an_unparsable_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = _import_transport()
    monkeypatch.setenv("SSM_FORWARD_TIMEOUT", "not-a-number")
    reader, writer = _readiness_pipe()

    with pytest.raises(transport.TransportError, match="SSM_FORWARD_TIMEOUT"):
        transport.wait_ready(lambda timeout_seconds: True, reader, instance_id="i-x", port=1)
    writer.close()
    reader.close()


@pytest.mark.parametrize("raw_value", ["0", "-5", "nan", "inf"])
def test_wait_ready_rejects_a_non_positive_or_non_finite_timeout(
    monkeypatch: pytest.MonkeyPatch, raw_value: str
) -> None:
    transport = _import_transport()
    monkeypatch.setenv("SSM_FORWARD_TIMEOUT", raw_value)
    reader, writer = _readiness_pipe()

    with pytest.raises(transport.TransportError, match="SSM_FORWARD_TIMEOUT"):
        transport.wait_ready(lambda timeout_seconds: True, reader, instance_id="i-x", port=1)
    writer.close()
    reader.close()


def test_readline_with_deadline_returns_none_when_the_deadline_has_already_passed_on_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression test for `_readline_with_deadline`'s entry-checked deadline guard.

    If the deadline expires between two successive reads (a non-matching
    line already consumed the time budget), the *next* call must notice the
    deadline has already passed before ever registering a selector, rather
    than calling `selector.select` with a negative or zero timeout. A
    monkeypatched, deterministic clock makes this reachable without relying
    on real elapsed wall-clock time: the first call sees time remaining and
    genuinely selects on the pipe; the second call's own entry check sees
    the deadline already behind it.
    """
    transport = _import_transport()
    monkeypatch.setenv("SSM_FORWARD_TIMEOUT", "10")
    reader, writer = _readiness_pipe()
    writer.write(b"Waiting for connections...\n")
    writer.close()

    clock = iter([0.0, 1.0, 20.0, 20.0, 20.0])
    monkeypatch.setattr(transport.time, "monotonic", lambda: next(clock))

    with pytest.raises(transport.ForwardTimeoutError):
        transport.wait_ready(lambda timeout_seconds: True, reader, instance_id=_INSTANCE_ID, port=1)
    reader.close()


def test_wait_ready_breaks_without_probing_when_the_deadline_expires_right_after_announcing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression test for `wait_ready`'s post-announcement deadline guard.

    If the deadline expires in the instant between the announcement line
    being read and computing the time remaining, `wait_ready` must raise
    without ever calling `probe` -- not call `probe` with a negative or
    zero timeout. A monkeypatched, deterministic clock makes the race
    reachable without depending on real elapsed wall-clock time.
    """
    transport = _import_transport()
    monkeypatch.setenv("SSM_FORWARD_TIMEOUT", "10")
    reader, writer = _readiness_pipe()
    writer.write(_PORT_OPENED_LINE)
    writer.close()

    def never_called(timeout_seconds: float) -> bool:
        raise AssertionError("probe should never run: the deadline had already expired")

    clock = iter([0.0, 1.0, 20.0, 20.0, 20.0])
    monkeypatch.setattr(transport.time, "monotonic", lambda: next(clock))

    with pytest.raises(transport.ForwardTimeoutError):
        transport.wait_ready(never_called, reader, instance_id=_INSTANCE_ID, port=1)
    reader.close()


def test_tcp_ready_probe_reports_ready_against_a_real_loopback_listener() -> None:
    """AC-TEST-005: the only network activity in this file, a listener this test owns."""
    transport = _import_transport()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]

        probe = transport.tcp_ready_probe(port)

        assert probe(1.0) is True


def test_tcp_ready_probe_reports_not_ready_when_nothing_is_listening() -> None:
    transport = _import_transport()
    port = _free_loopback_port()

    probe = transport.tcp_ready_probe(port)

    assert probe(1.0) is False


# ---------------------------------------------------------------------------
# DOCKER_TLS_PORT: read in exactly one place (AC-FUNC-006).
# ---------------------------------------------------------------------------


def test_build_start_session_argv_uses_the_documented_default_port_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _import_transport()
    monkeypatch.delenv("DOCKER_TLS_PORT", raising=False)

    argv = transport.build_start_session_argv(
        instance_id=_INSTANCE_ID, local_port=54321, profile="sandbox", region="us-east-1"
    )

    parameters = argv[argv.index("--parameters") + 1]
    assert "portNumber=2376" in parameters


def test_build_start_session_argv_reads_docker_tls_port_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _import_transport()
    monkeypatch.setenv("DOCKER_TLS_PORT", "9999")

    argv = transport.build_start_session_argv(
        instance_id=_INSTANCE_ID, local_port=54321, profile="sandbox", region="us-east-1"
    )

    parameters = argv[argv.index("--parameters") + 1]
    assert "portNumber=9999" in parameters


def test_build_start_session_argv_rejects_a_non_integer_docker_tls_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _import_transport()
    monkeypatch.setenv("DOCKER_TLS_PORT", "not-a-port")

    with pytest.raises(transport.TransportError, match="DOCKER_TLS_PORT"):
        transport.build_start_session_argv(instance_id="i-x", local_port=1, profile="p", region="r")


@pytest.mark.parametrize("raw_value", ["0", "-1", "65536", "100000"])
def test_build_start_session_argv_rejects_an_out_of_range_docker_tls_port(
    monkeypatch: pytest.MonkeyPatch, raw_value: str
) -> None:
    transport = _import_transport()
    monkeypatch.setenv("DOCKER_TLS_PORT", raw_value)

    with pytest.raises(transport.TransportError, match="DOCKER_TLS_PORT"):
        transport.build_start_session_argv(instance_id="i-x", local_port=1, profile="p", region="r")


def test_start_forward_invokes_the_launcher_with_the_built_argv() -> None:
    transport = _import_transport()
    launcher = FakeLauncher()

    transport.start_forward(
        launcher,
        instance_id=_INSTANCE_ID,
        local_port=54321,
        profile="sandbox",
        region="us-east-1",
    )

    expected = transport.build_start_session_argv(
        instance_id=_INSTANCE_ID, local_port=54321, profile="sandbox", region="us-east-1"
    )
    assert launcher.calls == [tuple(expected)]


# ---------------------------------------------------------------------------
# AC-TEST-001 / AC-10.3: no SSH element anywhere in the constructed argument
# vector, and the document is the port-forwarding one. Named last in its
# RED cycle-log entry: this is the strongest core-behavior assertion this
# file makes, not the weaker "the module does not exist yet" symptom every
# other RED case in this file also happens to exhibit.
# ---------------------------------------------------------------------------


def test_start_session_argv_contains_no_ssh_element_and_reaches_the_daemon_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _import_transport()
    monkeypatch.delenv("DOCKER_TLS_PORT", raising=False)

    argv = transport.build_start_session_argv(
        instance_id=_INSTANCE_ID, local_port=54321, profile="sandbox", region="us-east-1"
    )

    lowered = [element.lower() for element in argv]
    assert not any("ssh" in element for element in lowered)
    assert "AWS-StartSSHSession" not in argv
    assert "start-session" in argv
    assert "--document-name" in argv
    assert transport.PORT_FORWARDING_DOCUMENT in argv
    assert _INSTANCE_ID in argv
    parameters = argv[argv.index("--parameters") + 1]
    assert "portNumber=2376" in parameters
    assert "localPortNumber=54321" in parameters


# ---------------------------------------------------------------------------
# Teardown, and the production seams (subprocess_command_runner,
# default_process_launcher, main). Real, harmless, network-free host
# binaries only (echo, tail -f /dev/null) -- never a real aws or docker invocation.
# ---------------------------------------------------------------------------


def test_subprocess_command_runner_invokes_a_real_process_and_captures_output() -> None:
    transport = _import_transport()

    result = transport.subprocess_command_runner(["echo", "hello"], None)

    assert result.exit_code == 0
    assert result.stdout.strip() == "hello"
    assert result.binary_missing is False


def test_subprocess_command_runner_reports_binary_missing() -> None:
    transport = _import_transport()

    result = transport.subprocess_command_runner(["definitely-not-a-real-binary-xyz"], None)

    assert result.binary_missing is True
    assert result.exit_code == 127


def test_subprocess_command_runner_reports_a_timeout() -> None:
    transport = _import_transport()

    result = transport.subprocess_command_runner(["tail", "-f", "/dev/null"], 0.05)

    assert result.timed_out is True


def test_default_process_launcher_and_stop_forward_terminate_a_real_process() -> None:
    transport = _import_transport()

    process = transport.default_process_launcher(["tail", "-f", "/dev/null"])
    assert process.poll() is None

    transport.stop_forward(process)

    assert process.poll() is not None


class _StubbornFakeProcess:
    """A `stop_forward` target whose `.wait()` never returns until `.kill()` is called."""

    def __init__(self) -> None:
        self.killed = False
        self.terminated = False

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout: float | None = None) -> int:
        if not self.killed:
            raise subprocess.TimeoutExpired(cmd="fake", timeout=timeout or 0)
        return -9

    def kill(self) -> None:
        self.killed = True


def test_stop_forward_kills_a_process_that_does_not_exit_within_the_timeout() -> None:
    transport = _import_transport()
    process = _StubbornFakeProcess()

    transport.stop_forward(process)

    assert process.terminated is True
    assert process.killed is True


def test_main_start_command_reports_agent_not_online_as_a_clean_exit(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    transport = _import_transport()
    command = _identity_command(_INSTANCE_ID, "sandbox", "us-east-1")
    runner = FakeRunner(
        {command: transport.CommandResult(exit_code=0, stdout='{"InstanceInformationList": []}')}
    )
    monkeypatch.setattr(transport, "subprocess_command_runner", runner)

    exit_code = transport.main(
        [
            "start",
            "--instance-id",
            _INSTANCE_ID,
            "--context",
            "general-dev-sandbox",
            "--profile",
            "sandbox",
            "--region",
            "us-east-1",
        ]
    )

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "SSM has no record of instance" in captured.err


def test_main_requires_a_command() -> None:
    transport = _import_transport()

    with pytest.raises(SystemExit):
        transport.main([])


class _FakeForwardProcess:
    """A minimal stand-in for `subprocess.Popen[bytes]` in the `main` end-to-end test.

    Duck-types the three calls `_run_start`/`stop_forward` make
    (`.wait()`, `.terminate()`, `.poll()`) without spawning a real process:
    the real subprocess-spawning seam (`default_process_launcher`) already
    has its own dedicated test above, so this test's own concern is the CLI
    glue in `main`/`_run_start`, not process-spawning itself. `.stdout` is a
    real `_readiness_pipe()` read end so `wait_ready` can select on it
    exactly as it would a real subprocess's; when `announce_ready` is
    `True` the port-opened line is written and the write end closed
    immediately, and when `False` the write end is kept open (and
    referenced for the object's lifetime, so garbage collection never
    closes it early) so a test can exercise the genuine timeout path
    rather than an immediate EOF.
    """

    def __init__(self, interrupt_on_first_wait: bool = False, announce_ready: bool = True) -> None:
        self.terminated = False
        self.wait_calls = 0
        self._interrupt_on_first_wait = interrupt_on_first_wait
        self.stdout, self._writer = _readiness_pipe()
        if announce_ready:
            self._writer.write(_PORT_OPENED_LINE)
            self._writer.close()

    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls += 1
        if self._interrupt_on_first_wait and self.wait_calls == 1:
            raise KeyboardInterrupt
        return 0

    def terminate(self) -> None:
        self.terminated = True

    def poll(self) -> int | None:
        return 0 if self.terminated else None


class _LargeOutputFakeForwardProcess:
    """A session process whose stdout emits more than one pipe buffer after announcing.

    `code_review` reproduced a genuine deadlock against a real
    `subprocess.Popen(stdout=subprocess.PIPE)` child: once the parent calls
    the bare, blocking `process.wait()` and nothing ever drains `.stdout`,
    a child that keeps writing eventually fills the OS pipe buffer (commonly
    64 KiB) and blocks inside its own `write()` call forever, so it never
    exits, so `.wait()` never returns. This fake reproduces that exact
    coupling without a real subprocess: `.wait()` blocks on a background
    thread's completion, exactly as a real `Popen.wait()` blocks on the real
    child's exit, and that thread writes the announcement line followed by
    `payload_size` bytes (larger than any common OS pipe buffer) into a real
    `os.pipe()`. If `_run_start` does not drain `.stdout` after `wait_ready`
    confirms readiness, the thread's own `write()` call blocks on the full
    pipe forever, the thread never finishes, and `.wait()` never returns --
    reproducing the deadlock inside this hermetic test (AC-TEST-005: no
    network, no real subprocess) rather than against a real `aws` process.
    """

    def __init__(self, payload_size: int) -> None:
        self.terminated = False
        self.stdout, self._writer = _readiness_pipe()
        self._thread = threading.Thread(target=self._produce, args=(payload_size,), daemon=True)
        self._thread.start()

    def _produce(self, payload_size: int) -> None:
        self._writer.write(_PORT_OPENED_LINE)
        self._writer.flush()
        self._writer.write(b"x" * payload_size)
        self._writer.close()

    def wait(self, timeout: float | None = None) -> int:
        self._thread.join(timeout=timeout)
        if self._thread.is_alive():
            raise subprocess.TimeoutExpired(cmd="fake-session-process", timeout=timeout or 0)
        return 0

    def terminate(self) -> None:
        self.terminated = True

    def poll(self) -> int | None:
        return None if self._thread.is_alive() else 0


def _run_main_start_with_fake_process(
    monkeypatch: pytest.MonkeyPatch,
    fake_process: _FakeForwardProcess | _LargeOutputFakeForwardProcess,
) -> int:
    """Wire `main(["start", ...])` to `fake_process` and a fresh-port allocation, and run it.

    Shared by the happy-path, the `KeyboardInterrupt` variant and the
    large-output-drain variant below, all of which exercise `main`/
    `_run_start`'s CLI glue rather than the already-separately-tested
    `agent_status`, `allocate_local_port`, `start_forward` or `wait_ready`
    functions it calls.
    """
    transport = _import_transport()
    instance_id = _INSTANCE_ID
    identity_command = _identity_command(instance_id, "sandbox", "us-east-1")
    context_command = _context_inspect_command("general-dev-sandbox")
    runner = FakeRunner(
        {
            identity_command: transport.CommandResult(
                exit_code=0,
                stdout='{"InstanceInformationList": [{"PingStatus": "Online"}]}',
            ),
            context_command: _no_such_context_result(transport, "general-dev-sandbox"),
        }
    )
    monkeypatch.setattr(transport, "subprocess_command_runner", runner)

    def fake_launcher(
        command: Sequence[str],
    ) -> _FakeForwardProcess | _LargeOutputFakeForwardProcess:
        return fake_process

    monkeypatch.setattr(transport, "default_process_launcher", fake_launcher)

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        real_allocate = transport.allocate_local_port

        def allocate_and_listen(runner_arg: object, context_name: str) -> int:
            port = real_allocate(runner_arg, context_name)
            listener.bind(("127.0.0.1", port))
            listener.listen(1)
            return port

        monkeypatch.setattr(transport, "allocate_local_port", allocate_and_listen)

        return int(
            transport.main(
                [
                    "start",
                    "--instance-id",
                    instance_id,
                    "--context",
                    "general-dev-sandbox",
                    "--profile",
                    "sandbox",
                    "--region",
                    "us-east-1",
                ]
            )
        )


def test_main_start_full_flow_establishes_and_tears_down(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_process = _FakeForwardProcess()

    exit_code = _run_main_start_with_fake_process(monkeypatch, fake_process)

    assert exit_code == 0
    assert fake_process.wait_calls == 2
    assert fake_process.terminated is True
    captured = capsys.readouterr()
    assert "Port forward ready" in captured.out


def test_main_start_tears_down_cleanly_on_keyboard_interrupt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_process = _FakeForwardProcess(interrupt_on_first_wait=True)

    exit_code = _run_main_start_with_fake_process(monkeypatch, fake_process)

    assert exit_code == 0
    assert fake_process.terminated is True


def test_main_start_stops_the_forward_when_a_keyboard_interrupt_arrives_during_wait_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression test: Ctrl-C while `wait_ready` is still blocked must not leak the child.

    Before this fix, `_run_start` wrapped `wait_ready` in a `try`/`except
    ForwardTimeoutError` only; a `KeyboardInterrupt` arriving while
    `wait_ready` was still blocked on the session announcement propagated
    straight out of `_run_start` without ever calling `stop_forward`,
    leaving the launched `aws` session process running.
    """
    fake_process = _FakeForwardProcess(announce_ready=False)

    def raise_keyboard_interrupt(*args: object, **kwargs: object) -> None:
        raise KeyboardInterrupt

    transport = _import_transport()
    monkeypatch.setattr(transport, "wait_ready", raise_keyboard_interrupt)

    exit_code = _run_main_start_with_fake_process(monkeypatch, fake_process)

    assert exit_code == 0
    assert fake_process.terminated is True


def test_main_start_drains_more_than_one_pipe_buffer_of_output_after_announcing_and_tears_down(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Regression for the `process.wait()` pipe-buffer deadlock `code_review` reproduced.

    Runs `main(["start", ...])` on a background thread bounded by a join
    timeout instead of asserting in the main thread directly: if `_run_start`
    regressed to the bare, non-draining `process.wait()` this replaces, the
    fake session process's writer thread would block forever on the full
    pipe and this test would need a bound to fail cleanly rather than hang
    the whole suite.
    """
    payload_size = 300_000  # comfortably larger than a common 64 KiB OS pipe buffer
    fake_process = _LargeOutputFakeForwardProcess(payload_size)
    result: dict[str, int] = {}

    def run() -> None:
        result["exit_code"] = _run_main_start_with_fake_process(monkeypatch, fake_process)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(timeout=_HANG_GUARD_SECONDS)

    assert not thread.is_alive(), "main(['start', ...]) hung: process.stdout was not drained"
    assert result.get("exit_code") == 0
    assert fake_process.terminated is True
    captured = capsys.readouterr()
    assert "Port forward ready" in captured.out


def test_main_start_stops_the_forward_and_exits_non_zero_on_a_forward_timeout(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The port is allocated but nothing ever binds it, so `wait_ready` times out."""
    transport = _import_transport()
    monkeypatch.setenv("SSM_FORWARD_TIMEOUT", "0.2")
    instance_id = _INSTANCE_ID
    identity_command = _identity_command(instance_id, "sandbox", "us-east-1")
    context_command = _context_inspect_command("general-dev-sandbox")
    runner = FakeRunner(
        {
            identity_command: transport.CommandResult(
                exit_code=0,
                stdout='{"InstanceInformationList": [{"PingStatus": "Online"}]}',
            ),
            context_command: _no_such_context_result(transport, "general-dev-sandbox"),
        }
    )
    monkeypatch.setattr(transport, "subprocess_command_runner", runner)
    fake_process = _FakeForwardProcess(announce_ready=False)
    monkeypatch.setattr(transport, "default_process_launcher", lambda command: fake_process)

    exit_code = transport.main(
        [
            "start",
            "--instance-id",
            instance_id,
            "--context",
            "general-dev-sandbox",
            "--profile",
            "sandbox",
            "--region",
            "us-east-1",
        ]
    )

    assert exit_code == 1
    assert fake_process.terminated is True
    captured = capsys.readouterr()
    assert "did not become ready" in captured.err


class _NoStdoutFakeProcess:
    """A launcher-returned process with no piped `stdout` -- a `default_process_launcher` bug."""

    stdout = None

    def terminate(self) -> None:
        pass

    def wait(self, timeout: float | None = None) -> int:
        return 0


def test_main_start_fails_fast_when_the_launched_process_has_no_stdout_pipe(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Defensive fail-fast: `wait_ready` needs a real pipe to select on to detect readiness."""
    transport = _import_transport()
    instance_id = _INSTANCE_ID
    identity_command = _identity_command(instance_id, "sandbox", "us-east-1")
    context_command = _context_inspect_command("general-dev-sandbox")
    runner = FakeRunner(
        {
            identity_command: transport.CommandResult(
                exit_code=0,
                stdout='{"InstanceInformationList": [{"PingStatus": "Online"}]}',
            ),
            context_command: _no_such_context_result(transport, "general-dev-sandbox"),
        }
    )
    monkeypatch.setattr(transport, "subprocess_command_runner", runner)
    monkeypatch.setattr(
        transport, "default_process_launcher", lambda command: _NoStdoutFakeProcess()
    )

    exit_code = transport.main(
        [
            "start",
            "--instance-id",
            instance_id,
            "--context",
            "general-dev-sandbox",
            "--profile",
            "sandbox",
            "--region",
            "us-east-1",
        ]
    )

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "stdout" in captured.err


# ---------------------------------------------------------------------------
# Docker context: creation, update-in-place, the legacy ssh:// refusal, and
# the expiry precondition (E6-F2-S1-T2; AC-FUNC-001, AC-FUNC-002, AC-FUNC-006,
# AC-TEST-001, AC-TEST-004).
# ---------------------------------------------------------------------------


def _issue_client_material(root: Path, instance: str) -> object:
    """A real CA and client certificate under `root`, for one instance, via real openssl calls.

    Uses `devcontainer_config.certs` (E6-F1-S1-T1) directly, the same public
    API `tests/test_certs.py` itself drives, rather than hand-writing PEM
    fixture bytes: `ensure_context`'s expiry precondition parses a real
    certificate's `notAfter` through `certs.not_after`, so a hand-written
    stub file would not exercise that parse at all.
    """
    paths = certs.CertPaths(instance=instance, root=root)
    certs.create_ca(paths)
    certs.issue_client(paths)
    return paths


def test_context_name_for_returns_the_general_dev_prefixed_name() -> None:
    transport = _import_transport()

    assert transport.context_name_for("sandbox") == "general-dev-sandbox"


def test_build_context_create_argv_contains_all_four_endpoint_components(tmp_path: Path) -> None:
    transport = _import_transport()
    ca_path, cert_path, key_path = tmp_path / "ca.pem", tmp_path / "cert.pem", tmp_path / "key.pem"

    argv = transport.build_context_create_argv(
        context_name="general-dev-sandbox",
        port=54321,
        ca_path=ca_path,
        cert_path=cert_path,
        key_path=key_path,
    )

    assert argv[:4] == ["docker", "context", "create", "general-dev-sandbox"]
    docker_value = argv[argv.index("--docker") + 1]
    assert "host=tcp://127.0.0.1:54321" in docker_value
    assert f"ca={ca_path}" in docker_value
    assert f"cert={cert_path}" in docker_value
    assert f"key={key_path}" in docker_value
    assert "ssh://" not in docker_value


def test_build_context_update_argv_uses_update_not_create(tmp_path: Path) -> None:
    transport = _import_transport()
    ca_path, cert_path, key_path = tmp_path / "ca.pem", tmp_path / "cert.pem", tmp_path / "key.pem"

    argv = transport.build_context_update_argv(
        context_name="general-dev-sandbox",
        port=54321,
        ca_path=ca_path,
        cert_path=cert_path,
        key_path=key_path,
    )

    assert argv[:4] == ["docker", "context", "update", "general-dev-sandbox"]


def test_ensure_context_creates_a_context_when_absent(tmp_path: Path) -> None:
    transport = _import_transport()
    paths = _issue_client_material(tmp_path, "sandbox")
    inspect_command = _context_inspect_command("general-dev-sandbox")
    create_argv = tuple(
        transport.build_context_create_argv(
            context_name="general-dev-sandbox",
            port=54321,
            ca_path=paths.ca_cert,
            cert_path=paths.client_cert,
            key_path=paths.client_key,
        )
    )
    runner = FakeRunner(
        {
            inspect_command: _no_such_context_result(transport, "general-dev-sandbox"),
            create_argv: transport.CommandResult(exit_code=0, stdout="general-dev-sandbox\n"),
        }
    )

    name = transport.ensure_context(
        runner,
        instance="sandbox",
        port=54321,
        certs_root=tmp_path,
        reference_time=datetime.datetime.now(datetime.UTC),
    )

    assert name == "general-dev-sandbox"
    assert runner.calls == [(inspect_command, None), (create_argv, None)]


def test_ensure_context_updates_an_existing_tcp_context_in_place(tmp_path: Path) -> None:
    transport = _import_transport()
    paths = _issue_client_material(tmp_path, "sandbox")
    inspect_command = _context_inspect_command("general-dev-sandbox")
    update_argv = tuple(
        transport.build_context_update_argv(
            context_name="general-dev-sandbox",
            port=54321,
            ca_path=paths.ca_cert,
            cert_path=paths.client_cert,
            key_path=paths.client_key,
        )
    )
    runner = FakeRunner(
        {
            inspect_command: transport.CommandResult(
                exit_code=0, stdout='{"docker":{"Host":"tcp://127.0.0.1:9999"}}'
            ),
            update_argv: transport.CommandResult(exit_code=0, stdout="general-dev-sandbox\n"),
        }
    )

    name = transport.ensure_context(
        runner,
        instance="sandbox",
        port=54321,
        certs_root=tmp_path,
        reference_time=datetime.datetime.now(datetime.UTC),
    )

    assert name == "general-dev-sandbox"
    # Only 'inspect' then 'update' were ever issued: FakeRunner raises on any
    # unrecorded command, so a stray 'context create' or 'context rm' here
    # would fail this test rather than silently pass (AC-FUNC-002).
    assert runner.calls == [(inspect_command, None), (update_argv, None)]


def test_ensure_context_refuses_an_existing_ssh_context_by_name(tmp_path: Path) -> None:
    transport = _import_transport()
    _issue_client_material(tmp_path, "sandbox")
    inspect_command = _context_inspect_command("general-dev-sandbox")
    runner = FakeRunner(
        {
            inspect_command: transport.CommandResult(
                exit_code=0, stdout='{"docker":{"Host":"ssh://user@10.0.0.5"}}'
            )
        }
    )

    with pytest.raises(transport.LegacyContextError, match="general-dev-sandbox") as excinfo:
        transport.ensure_context(
            runner,
            instance="sandbox",
            port=54321,
            certs_root=tmp_path,
            reference_time=datetime.datetime.now(datetime.UTC),
        )

    assert "ssh://user@10.0.0.5" in str(excinfo.value)
    assert runner.calls == [(inspect_command, None)]


def test_ensure_context_raises_when_docker_is_not_on_path(tmp_path: Path) -> None:
    transport = _import_transport()
    _issue_client_material(tmp_path, "sandbox")
    inspect_command = _context_inspect_command("general-dev-sandbox")
    runner = FakeRunner(
        {inspect_command: transport.CommandResult(exit_code=127, binary_missing=True)}
    )

    with pytest.raises(transport.TransportError, match="docker is not on PATH"):
        transport.ensure_context(
            runner,
            instance="sandbox",
            port=54321,
            certs_root=tmp_path,
            reference_time=datetime.datetime.now(datetime.UTC),
        )


def test_ensure_context_raises_naming_the_context_when_the_create_command_fails(
    tmp_path: Path,
) -> None:
    transport = _import_transport()
    paths = _issue_client_material(tmp_path, "sandbox")
    inspect_command = _context_inspect_command("general-dev-sandbox")
    create_argv = tuple(
        transport.build_context_create_argv(
            context_name="general-dev-sandbox",
            port=54321,
            ca_path=paths.ca_cert,
            cert_path=paths.client_cert,
            key_path=paths.client_key,
        )
    )
    runner = FakeRunner(
        {
            inspect_command: _no_such_context_result(transport, "general-dev-sandbox"),
            create_argv: transport.CommandResult(exit_code=1, stderr="ca.pem seems invalid"),
        }
    )

    with pytest.raises(transport.TransportError, match="general-dev-sandbox") as excinfo:
        transport.ensure_context(
            runner,
            instance="sandbox",
            port=54321,
            certs_root=tmp_path,
            reference_time=datetime.datetime.now(datetime.UTC),
        )

    assert "ca.pem seems invalid" in str(excinfo.value)


def test_ensure_context_raises_when_docker_is_not_on_path_for_the_create_command(
    tmp_path: Path,
) -> None:
    """Distinct from the 'inspect' binary_missing case: the create command's own branch."""
    transport = _import_transport()
    paths = _issue_client_material(tmp_path, "sandbox")
    inspect_command = _context_inspect_command("general-dev-sandbox")
    create_argv = tuple(
        transport.build_context_create_argv(
            context_name="general-dev-sandbox",
            port=54321,
            ca_path=paths.ca_cert,
            cert_path=paths.client_cert,
            key_path=paths.client_key,
        )
    )
    runner = FakeRunner(
        {
            inspect_command: _no_such_context_result(transport, "general-dev-sandbox"),
            create_argv: transport.CommandResult(exit_code=127, binary_missing=True),
        }
    )

    with pytest.raises(transport.TransportError, match="docker is not on PATH"):
        transport.ensure_context(
            runner,
            instance="sandbox",
            port=54321,
            certs_root=tmp_path,
            reference_time=datetime.datetime.now(datetime.UTC),
        )


def test_ensure_context_refuses_before_touching_docker_when_certificate_is_missing(
    tmp_path: Path,
) -> None:
    transport = _import_transport()
    certs.CertPaths(instance="sandbox", root=tmp_path)  # no CA, no client cert issued
    runner = FakeRunner({})

    with pytest.raises(transport.CertificateNotReadyError, match="sandbox"):
        transport.ensure_context(
            runner,
            instance="sandbox",
            port=54321,
            certs_root=tmp_path,
            reference_time=datetime.datetime.now(datetime.UTC),
        )

    assert runner.calls == []


def test_ensure_context_refuses_before_touching_docker_when_certificate_is_expired(
    tmp_path: Path,
) -> None:
    transport = _import_transport()
    paths = _issue_client_material(tmp_path, "sandbox")
    expiry = certs.not_after(paths.client_cert)
    far_future = expiry + datetime.timedelta(days=1)
    runner = FakeRunner({})

    with pytest.raises(transport.CertificateNotReadyError, match="expired"):
        transport.ensure_context(
            runner,
            instance="sandbox",
            port=54321,
            certs_root=tmp_path,
            reference_time=far_future,
        )

    assert runner.calls == []


def test_ensure_context_expiry_precondition_calls_certs_classify(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AC-TEST-004: the precondition reuses `certs.classify` rather than reimplementing it."""
    transport = _import_transport()
    paths = _issue_client_material(tmp_path, "sandbox")
    real_classify = certs.classify
    calls: list[tuple[object, object, object]] = []

    def spy_classify(cert_not_after: object, reference_time: object, warn_days: object) -> object:
        calls.append((cert_not_after, reference_time, warn_days))
        return real_classify(cert_not_after, reference_time, warn_days)

    monkeypatch.setattr(certs, "classify", spy_classify)
    inspect_command = _context_inspect_command("general-dev-sandbox")
    create_argv = tuple(
        transport.build_context_create_argv(
            context_name="general-dev-sandbox",
            port=54321,
            ca_path=paths.ca_cert,
            cert_path=paths.client_cert,
            key_path=paths.client_key,
        )
    )
    runner = FakeRunner(
        {
            inspect_command: _no_such_context_result(transport, "general-dev-sandbox"),
            create_argv: transport.CommandResult(exit_code=0, stdout="general-dev-sandbox\n"),
        }
    )
    reference_time = datetime.datetime.now(datetime.UTC)

    transport.ensure_context(
        runner,
        instance="sandbox",
        port=54321,
        certs_root=tmp_path,
        reference_time=reference_time,
    )

    assert len(calls) == 1
    # test_review (round 1, non-blocking): assert the delegated arguments too, not
    # merely that classify was called once -- a precondition that reimplemented the
    # window with the wrong certificate or the wrong reference time would still pass
    # a bare `len(calls) == 1` check but fails these.
    cert_not_after, passed_reference_time, warn_days = calls[0]
    assert cert_not_after == certs.not_after(paths.client_cert)
    assert passed_reference_time == reference_time
    assert warn_days == 0


# ---------------------------------------------------------------------------
# Handshake failure translation (E6-F2-S1-T2; AC-FUNC-004, AC-FUNC-005,
# AC-TEST-002). Every stderr string below is copied verbatim from a real
# `docker`/`docker --context ... version` invocation this task's author ran
# against a real docker CLI (29.4.0) and a real, deliberately mis-issued
# certificate -- never invented prose (see the module docstring).
# ---------------------------------------------------------------------------

_REAL_SAN_MISMATCH_WRONG_IP_STDERR = (
    'error during connect: Get "https://127.0.0.1:18444/v1.54/version": '
    "tls: failed to verify certificate: x509: certificate is valid for 10.0.0.5, not 127.0.0.1"
)
_REAL_SAN_MISMATCH_NO_IP_SAN_STDERR = (
    'error during connect: Get "https://127.0.0.1:18443/v1.54/version": '
    "tls: failed to verify certificate: x509: cannot validate certificate for 127.0.0.1 "
    "because it doesn't contain any IP SANs"
)
_REAL_CONNECTION_REFUSED_STDERR = (
    "Cannot connect to the Docker daemon at tcp://127.0.0.1:63441. Is the docker daemon running?"
)


@pytest.mark.parametrize(
    "stderr", [_REAL_SAN_MISMATCH_WRONG_IP_STDERR, _REAL_SAN_MISMATCH_NO_IP_SAN_STDERR]
)
def test_diagnose_handshake_failure_states_the_san_requirement(stderr: str) -> None:
    transport = _import_transport()

    message = transport.diagnose_handshake_failure(
        stderr, context_name="general-dev-sandbox", instance="sandbox"
    )

    assert "IP:127.0.0.1" in message
    assert "DNS:localhost" in message
    assert "/devcontainer:certs INSTANCE=sandbox" in message


def test_diagnose_handshake_failure_produces_the_forward_diagnosis_for_a_connection_failure() -> (
    None
):
    """AC-FUNC-005: a connection failure is never conflated with the SAN diagnosis."""
    transport = _import_transport()

    message = transport.diagnose_handshake_failure(
        _REAL_CONNECTION_REFUSED_STDERR, context_name="general-dev-sandbox", instance="sandbox"
    )

    assert "IP:127.0.0.1" not in message
    assert "SAN" not in message
    assert "forward" in message.lower()


# ---------------------------------------------------------------------------
# The handshake itself: readiness, the version floor, and rootless reporting
# (E6-F2-S1-T2; AC-FUNC-003, AC-FUNC-007, AC-TEST-002, AC-TEST-003).
# ---------------------------------------------------------------------------


class _SequencedRunner:
    """Answers the identical command with a different `CommandResult` on each successive call.

    `FakeRunner` (`tests/conftest.py`) maps one fixed result per exact
    command tuple, which cannot express `handshake`'s retry-until-it-answers
    loop: the version command tuple this loop issues is identical on every
    attempt, only its outcome changes as the daemon comes up. This is a
    genuinely different shape from `FakeRunner`'s fixed-response map, not a
    second copy of it, so it stays local to this file's own handshake tests
    (this file's own module docstring).
    """

    def __init__(self, results: Sequence[object]) -> None:
        self._results = list(results)
        self.calls: list[tuple[tuple[str, ...], float | None]] = []

    def __call__(self, command: Sequence[str], timeout_seconds: float | None) -> object:
        self.calls.append((tuple(command), timeout_seconds))
        if not self._results:
            raise AssertionError(
                f"_SequencedRunner exhausted its recorded results at command {tuple(command)!r}"
            )
        return self._results.pop(0)


def _version_command(context_name: str) -> tuple[str, ...]:
    return ("docker", "--context", context_name, "version", "--format", "{{json .Server}}")


def _info_command(context_name: str) -> tuple[str, ...]:
    return ("docker", "--context", context_name, "info", "--format", "{{json .SecurityOptions}}")


def test_handshake_retries_the_version_call_until_it_answers() -> None:
    transport = _import_transport()
    runner = _SequencedRunner(
        [
            transport.CommandResult(exit_code=1, stderr=_REAL_CONNECTION_REFUSED_STDERR),
            transport.CommandResult(exit_code=1, stderr=_REAL_CONNECTION_REFUSED_STDERR),
            transport.CommandResult(exit_code=0, stdout='{"Version":"28.6.0","ApiVersion":"1.51"}'),
            transport.CommandResult(exit_code=0, stdout='["name=seccomp,profile=builtin"]'),
        ]
    )

    result = transport.handshake(runner, instance="sandbox", port=54321)

    assert result.server_version == "28.6.0"
    assert result.api_version == "1.51"
    assert result.rootless is False
    expected_version_command = _version_command("general-dev-sandbox")
    version_calls = [call for call in runner.calls if call[0] == expected_version_command]
    assert len(version_calls) == 3


def test_handshake_reports_rootless_true_when_security_options_name_it() -> None:
    transport = _import_transport()
    runner = _SequencedRunner(
        [
            transport.CommandResult(exit_code=0, stdout='{"Version":"28.6.0","ApiVersion":"1.51"}'),
            transport.CommandResult(
                exit_code=0, stdout='["name=rootless","name=seccomp,profile=builtin"]'
            ),
        ]
    )

    result = transport.handshake(runner, instance="sandbox", port=54321)

    assert result.rootless is True


def test_handshake_raises_immediately_on_a_certificate_name_mismatch_without_retrying() -> None:
    """The retry loop must never spend the timeout budget on a permanent, non-transient cause."""
    transport = _import_transport()
    runner = _SequencedRunner(
        [transport.CommandResult(exit_code=1, stderr=_REAL_SAN_MISMATCH_WRONG_IP_STDERR)]
    )

    with pytest.raises(transport.TransportError, match="IP:127.0.0.1"):
        transport.handshake(runner, instance="sandbox", port=54321)

    assert len(runner.calls) == 1


def test_handshake_raises_version_floor_error_naming_both_versions() -> None:
    transport = _import_transport()
    runner = _SequencedRunner(
        [transport.CommandResult(exit_code=0, stdout='{"Version":"20.10.0","ApiVersion":"1.41"}')]
    )

    with pytest.raises(transport.DockerVersionFloorError) as excinfo:
        transport.handshake(runner, instance="sandbox", port=54321)

    message = str(excinfo.value)
    assert "1.41" in message
    assert transport.DOCKER_API_VERSION_FLOOR in message


def test_handshake_raises_on_an_unparsable_api_version() -> None:
    transport = _import_transport()
    runner = _SequencedRunner(
        [transport.CommandResult(exit_code=0, stdout='{"Version":"28.6.0","ApiVersion":"bogus"}')]
    )

    with pytest.raises(transport.TransportError, match="bogus"):
        transport.handshake(runner, instance="sandbox", port=54321)


def test_handshake_raises_on_unparsable_server_payload() -> None:
    transport = _import_transport()
    runner = _SequencedRunner([transport.CommandResult(exit_code=0, stdout="not json")])

    with pytest.raises(transport.TransportError, match="general-dev-sandbox"):
        transport.handshake(runner, instance="sandbox", port=54321)


def test_handshake_raises_when_docker_info_fails_after_a_successful_version_call() -> None:
    transport = _import_transport()
    runner = _SequencedRunner(
        [
            transport.CommandResult(exit_code=0, stdout='{"Version":"28.6.0","ApiVersion":"1.51"}'),
            transport.CommandResult(exit_code=1, stderr=_REAL_CONNECTION_REFUSED_STDERR),
        ]
    )

    with pytest.raises(transport.TransportError, match="general-dev-sandbox"):
        transport.handshake(runner, instance="sandbox", port=54321)


def test_handshake_raises_when_docker_is_not_on_path() -> None:
    transport = _import_transport()
    runner = _SequencedRunner([transport.CommandResult(exit_code=127, binary_missing=True)])

    with pytest.raises(transport.TransportError, match="docker is not on PATH"):
        transport.handshake(runner, instance="sandbox", port=54321)


def test_handshake_raises_when_docker_is_not_on_path_for_the_info_command() -> None:
    """Distinct from the version call's own binary_missing branch: the info call's own branch."""
    transport = _import_transport()
    runner = _SequencedRunner(
        [
            transport.CommandResult(exit_code=0, stdout='{"Version":"28.6.0","ApiVersion":"1.51"}'),
            transport.CommandResult(exit_code=127, binary_missing=True),
        ]
    )

    with pytest.raises(transport.TransportError, match="docker is not on PATH"):
        transport.handshake(runner, instance="sandbox", port=54321)


def test_handshake_raises_on_unparsable_security_options() -> None:
    transport = _import_transport()
    runner = _SequencedRunner(
        [
            transport.CommandResult(exit_code=0, stdout='{"Version":"28.6.0","ApiVersion":"1.51"}'),
            transport.CommandResult(exit_code=0, stdout="not json"),
        ]
    )

    with pytest.raises(transport.TransportError, match="general-dev-sandbox"):
        transport.handshake(runner, instance="sandbox", port=54321)


def test_handshake_raises_timeout_naming_context_port_and_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _import_transport()
    monkeypatch.setenv("DOCKER_HANDSHAKE_TIMEOUT", "0.2")

    def never_answers(command: Sequence[str], timeout_seconds: float | None) -> object:
        return transport.CommandResult(exit_code=1, stderr=_REAL_CONNECTION_REFUSED_STDERR)

    start = time.monotonic()
    with pytest.raises(transport.DockerHandshakeTimeoutError) as excinfo:
        transport.handshake(never_answers, instance="sandbox", port=54321)
    elapsed = time.monotonic() - start

    assert elapsed < _HANG_GUARD_SECONDS
    message = str(excinfo.value)
    assert "general-dev-sandbox" in message
    assert "54321" in message
    assert "DOCKER_HANDSHAKE_TIMEOUT" in message


def test_handshake_reads_the_timeout_through_the_shared_hostprobe_reader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-FUNC-007: DOCKER_HANDSHAKE_TIMEOUT has exactly one reader, hostprobe's shared one.

    code_review round 1 (BLOCKING, DRY): this module must not declare a
    second, independent copy of the env-var name, the default, or the
    parse/validate logic `hostprobe.read_positive_seconds` already owns.
    Spying on the name this module imports proves delegation rather than a
    hand-rolled reimplementation that merely happens to accept the same
    values.
    """
    transport = _import_transport()
    calls: list[tuple[str, float]] = []

    def spy_read_positive_seconds(env_var: str, default_seconds: float) -> float:
        calls.append((env_var, default_seconds))
        return 0.05

    monkeypatch.setattr(transport, "read_positive_seconds", spy_read_positive_seconds)

    def never_answers(command: Sequence[str], timeout_seconds: float | None) -> object:
        return transport.CommandResult(exit_code=1, stderr=_REAL_CONNECTION_REFUSED_STDERR)

    with pytest.raises(transport.DockerHandshakeTimeoutError):
        transport.handshake(never_answers, instance="sandbox", port=54321)

    assert calls == [
        (
            transport.DOCKER_HANDSHAKE_TIMEOUT_ENV_VAR,
            transport.DOCKER_HANDSHAKE_TIMEOUT_DEFAULT_SECONDS,
        )
    ]


def test_handshake_passes_the_remaining_deadline_not_the_full_timeout_to_the_rootless_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """code_review round 1 (WARN): worst-case wall time must not approach 2x the timeout.

    Passing the full timeout a second time to `_probe_rootless`'s `docker
    info` call would let total handshake wall time approach 2x
    `DOCKER_HANDSHAKE_TIMEOUT`, contradicting `docs/devcontainer.md`'s
    'bounded by DOCKER_HANDSHAKE_TIMEOUT' statement. A deterministic clock
    proves the exact remaining-deadline value reaches the runner, not
    merely that some value less than the timeout might by coincidence.
    """
    transport = _import_transport()
    monkeypatch.setenv("DOCKER_HANDSHAKE_TIMEOUT", "10")
    runner = _SequencedRunner(
        [
            transport.CommandResult(exit_code=0, stdout='{"Version":"28.6.0","ApiVersion":"1.51"}'),
            transport.CommandResult(exit_code=0, stdout="[]"),
        ]
    )
    # deadline = 0.0 + 10 = 10.0; version call sees remaining = 10.0 - 1.0 = 9.0;
    # the rootless probe must see remaining = 10.0 - 4.0 = 6.0, never the full 10.0.
    clock = iter([0.0, 1.0, 4.0])
    monkeypatch.setattr(transport.time, "monotonic", lambda: next(clock))

    transport.handshake(runner, instance="sandbox", port=54321)

    info_calls = [call for call in runner.calls if call[0] == _info_command("general-dev-sandbox")]
    assert len(info_calls) == 1
    assert info_calls[0][1] == pytest.approx(6.0)


def test_handshake_raises_timeout_instead_of_probing_rootless_with_a_zero_remaining_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """code_review round 2 (WARN): an exhausted deadline must not reach `_probe_rootless` as 0.0.

    `subprocess.run(timeout=0.0)` expires immediately, so a `docker info`
    call issued with the leftover budget from an already-exhausted deadline
    can never succeed; it would report the exhaustion as an opaque 'docker
    info ... failed after a successful version handshake' with a blank
    stderr line rather than naming the actual cause, deadline exhaustion.
    When the version call answers but no budget remains before the
    rootless probe would run, this must raise the same
    `DockerHandshakeTimeoutError` the retry loop itself raises, naming the
    context, the port and `DOCKER_HANDSHAKE_TIMEOUT`, and must never call
    `docker info` at all.
    """
    transport = _import_transport()
    monkeypatch.setenv("DOCKER_HANDSHAKE_TIMEOUT", "10")
    runner = _SequencedRunner(
        [transport.CommandResult(exit_code=0, stdout='{"Version":"28.6.0","ApiVersion":"1.51"}')]
    )
    # deadline = 0.0 + 10 = 10.0; version call sees remaining = 10.0 - 1.0 = 9.0 and answers;
    # the rootless-probe budget check then sees remaining = 10.0 - 10.0 = 0.0, exhausted.
    clock = iter([0.0, 1.0, 10.0])
    monkeypatch.setattr(transport.time, "monotonic", lambda: next(clock))

    with pytest.raises(transport.DockerHandshakeTimeoutError) as excinfo:
        transport.handshake(runner, instance="sandbox", port=54321)

    message = str(excinfo.value)
    assert "general-dev-sandbox" in message
    assert "54321" in message
    assert "DOCKER_HANDSHAKE_TIMEOUT" in message
    info_calls = [call for call in runner.calls if call[0] == _info_command("general-dev-sandbox")]
    assert info_calls == []


def test_handshake_timeout_error_message_is_produced_by_diagnose_handshake_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """code_review round 1 (WARN, DRY): the timeout message must be the same code
    diagnose_handshake_failure's own tests exercise, not a second copy of its
    generic connection-remediation sentence.
    """
    transport = _import_transport()
    monkeypatch.setenv("DOCKER_HANDSHAKE_TIMEOUT", "0.2")
    calls: list[tuple[str, str, str, str]] = []

    def spy_diagnose(
        stderr: str, *, context_name: str, instance: str, failure_summary: str = "failed"
    ) -> str:
        calls.append((stderr, context_name, instance, failure_summary))
        return "SENTINEL_DIAGNOSIS"

    monkeypatch.setattr(transport, "diagnose_handshake_failure", spy_diagnose)

    def never_answers(command: Sequence[str], timeout_seconds: float | None) -> object:
        return transport.CommandResult(exit_code=1, stderr=_REAL_CONNECTION_REFUSED_STDERR)

    with pytest.raises(transport.DockerHandshakeTimeoutError) as excinfo:
        transport.handshake(never_answers, instance="sandbox", port=54321)

    assert str(excinfo.value) == "SENTINEL_DIAGNOSIS"
    assert len(calls) == 1
    stderr, context_name, instance, failure_summary = calls[0]
    assert stderr == _REAL_CONNECTION_REFUSED_STDERR
    assert context_name == "general-dev-sandbox"
    assert instance == "sandbox"
    assert "DOCKER_HANDSHAKE_TIMEOUT" in failure_summary
    assert "54321" in failure_summary


def test_handshake_reads_the_default_timeout_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = _import_transport()
    monkeypatch.delenv("DOCKER_HANDSHAKE_TIMEOUT", raising=False)
    recorded: list[float | None] = []

    def runner(command: Sequence[str], timeout_seconds: float | None) -> object:
        recorded.append(timeout_seconds)
        if tuple(command) == _version_command("general-dev-sandbox"):
            return transport.CommandResult(
                exit_code=0, stdout='{"Version":"28.6.0","ApiVersion":"1.51"}'
            )
        return transport.CommandResult(exit_code=0, stdout="[]")

    transport.handshake(runner, instance="sandbox", port=54321)

    assert recorded[0] == pytest.approx(30.0, abs=0.5)


def test_handshake_rejects_an_unparsable_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = _import_transport()
    monkeypatch.setenv("DOCKER_HANDSHAKE_TIMEOUT", "not-a-number")

    def never_called(command: Sequence[str], timeout_seconds: float | None) -> object:
        raise AssertionError("runner should never be called: the timeout is invalid")

    with pytest.raises(transport.TransportError, match="DOCKER_HANDSHAKE_TIMEOUT"):
        transport.handshake(never_called, instance="sandbox", port=1)


@pytest.mark.parametrize("raw_value", ["0", "-5", "nan", "inf"])
def test_handshake_rejects_a_non_positive_or_non_finite_timeout(
    monkeypatch: pytest.MonkeyPatch, raw_value: str
) -> None:
    transport = _import_transport()
    monkeypatch.setenv("DOCKER_HANDSHAKE_TIMEOUT", raw_value)

    def never_called(command: Sequence[str], timeout_seconds: float | None) -> object:
        raise AssertionError("runner should never be called: the timeout is invalid")

    with pytest.raises(transport.TransportError, match="DOCKER_HANDSHAKE_TIMEOUT"):
        transport.handshake(never_called, instance="sandbox", port=1)


# ---------------------------------------------------------------------------
# The strongest core-behavior assertion in this RED cycle, named last in its
# log entry: the full seam this task exists for -- a docker context created
# with the port, ca, cert and key E6-F2-S1-T1/E6-F1-S1-T1 produced, and a
# handshake failure against that exact material translated into the SAN
# requirement rather than a bare, opaque TLS error (spec Section 11).
# ---------------------------------------------------------------------------


def test_handshake_san_diagnosis_names_the_reissue_invocation_for_the_created_instance() -> None:
    transport = _import_transport()
    runner = _SequencedRunner(
        [transport.CommandResult(exit_code=1, stderr=_REAL_SAN_MISMATCH_NO_IP_SAN_STDERR)]
    )

    with pytest.raises(transport.TransportError) as excinfo:
        transport.handshake(runner, instance="sandbox", port=54321)

    message = str(excinfo.value)
    assert "IP:127.0.0.1" in message
    assert "DNS:localhost" in message
    assert "/devcontainer:certs INSTANCE=sandbox" in message
