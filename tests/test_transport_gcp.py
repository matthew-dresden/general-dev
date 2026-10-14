"""Tests for devcontainer_config.transport_gcp: the gcp IAP tunnel transport.

The `devcontainer_config.transport_gcp` import is deferred into function
bodies (via `_import_transport_gcp`), the same convention
`tests/test_transport_azure.py` documents for its own module: a module-level
import would fail COLLECTION for the whole file instead of failing the one
test for the real reason.

The suite is hermetic in the same ways `tests/test_transport_azure.py` is:
every `docker` command is answered by a fake runner and recorded, never
run; the tunnel child is a fake whose stand-in for gcloud binds a REAL
loopback listener parsed out of the argv it received -- the one socket work
the suite does, the allowance the other transport suites keep for port
probes -- so the readiness poll is exercised against a genuine listener
rather than a stubbed probe; and the SIGTERM stop is a real signal to this
process, armed only once the supervise loop's own handler is provably
installed and bounded by a deadline guard.

The argv pins are deliberate and load-bearing: the gcloud vector is pinned
element-for-element (no credential flag, no shell string, and no ssh element
anywhere -- this transport shares nothing with the ssh one), and the
`Connected:` marker keeps the exact shape `devcontainer_config.forwards`
polls for.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import io
import os
import signal
import socket
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from types import ModuleType

import pytest
from conftest import _repo_root
from devcontainer_config import certs, instances
from devcontainer_config.hostprobe import CommandResult

_CONTEXT = "general-dev-gcpvm"
_NAME = "gcpvm"
_ZONE = "us-east1-b"
_PROJECT = "proj-123456"

# An upper-bound safety guard for the tests that wait on real threads and
# one real signal -- a bound on how long the test may take, never a
# synchronization mechanism.
_HANG_GUARD_SECONDS = 5.0


def _import_transport_gcp() -> ModuleType:
    """Import devcontainer_config.transport_gcp from inside a function body."""
    return importlib.import_module("devcontainer_config.transport_gcp")


@pytest.fixture(autouse=True)
def _docker_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point DOCKER_CONFIG at a per-test directory.

    The address record and the certificate material live under
    `instances.certs_dir`, which honors DOCKER_CONFIG; setting it here keeps
    every test's state on `tmp_path` and away from the operator's real
    `~/.docker/certs` (the same guard `tests/test_transport_azure.py`
    installs).
    """
    docker_config = tmp_path / "docker-config"
    docker_config.mkdir()
    monkeypatch.setenv("DOCKER_CONFIG", str(docker_config))
    return docker_config


def _issue_client_material(instance: str) -> None:
    """A real CA and client certificate for `instance`, via real openssl calls.

    The same approach `tests/test_transport_azure.py` takes:
    `ensure_context`'s expiry precondition parses a real certificate's
    `notAfter`, so a hand-written stub file would not exercise that parse at
    all.
    """
    paths = certs.CertPaths(instance=instance, root=instances.certs_root())
    certs.create_ca(paths)
    certs.issue_client(paths)


def _write_engine_address(instance: str) -> None:
    """The engine address record the deploy writes, with the six gcp keys.

    The keys beyond the triple the transport reads (`region`,
    `secrets_prefix`, `state_bucket`) are part of the record contract this
    slice must tolerate, exactly as the azure record's extra keys are.
    """
    instances.write_engine_address(
        _repo_root(),
        instance,
        {
            "instance_name": instance,
            "zone": _ZONE,
            "project_id": _PROJECT,
            "region": "us-east1",
            "secrets_prefix": instance,
            "state_bucket": f"{instance}-state",
        },
    )


class RecordingRunner:
    """A runner double that records every command and answers from a function.

    The same shape `tests/test_transport_azure.py` uses: the answer function
    receives the exact command tuple; anything it does not answer raises, so
    a stray or missing docker call fails the test instead of passing
    silently.
    """

    def __init__(self, answer: Callable[[tuple[str, ...]], CommandResult]) -> None:
        self._answer = answer
        self.commands: list[tuple[str, ...]] = []

    def __call__(self, command: Sequence[str], timeout_seconds: float | None) -> CommandResult:
        self.commands.append(tuple(command))
        return self._answer(tuple(command))


class _FakeChild:
    """A tunnel-child stand-in: pipes, poll(), terminate(), and a real listener.

    When `bind_port` is given, the child holds a REAL loopback listener on
    that port -- what a live gcloud does from the moment its listener is
    up -- and closes it on terminate, so the readiness poll and the
    port-closed verification both run against a genuine socket.
    `blocking_stdout` swaps the immediate-EOF stdout for a real pipe whose
    write end the caller holds, so the supervise loop blocks in a real read
    the way it does against a live gcloud.
    """

    def __init__(self, *, bind_port: int | None = None, stderr_text: bytes = b"") -> None:
        self.pid = 4242
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False
        self.argv_received: tuple[str, ...] | None = None
        self.stderr = io.BytesIO(stderr_text)
        self.stdout: object = io.BytesIO(b"")
        self._listener: socket.socket | None = None
        if bind_port is not None:
            self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._listener.bind(("127.0.0.1", bind_port))
            self._listener.listen(4)

    def block_reads(self) -> None:
        """Replace stdout with a real pipe that never delivers data.

        The read end belongs to the supervise loop's echo; the write end is
        deliberately left open (and leaked to process exit) so the read
        blocks the way it does against a live gcloud that has written
        nothing yet.
        """
        read_fd, write_fd = os.pipe()
        self.stdout = os.fdopen(read_fd, "rb", buffering=0)
        self._write_fd = write_fd

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0
        if self._listener is not None:
            self._listener.close()
            self._listener = None

    def kill(self) -> None:
        self.killed = True
        self.terminate()

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        return 0


class _FakeLauncher:
    """A launcher double standing in for a live gcloud unless told otherwise.

    By default the child binds the REAL port its own argv names (parsed
    from `--local-host-port=127.0.0.1:<port>`, which is also an implicit
    assertion that the vector carries it) and stays alive -- what a healthy
    tunnel does. Tests needing a dying child pass `stderr_text` and
    `exit_immediately`; tests needing a silent child pass `bind=False`.
    """

    def __init__(
        self,
        *,
        bind: bool = True,
        stderr_text: bytes = b"",
        exit_immediately: bool = False,
    ) -> None:
        self.children: list[_FakeChild] = []
        self._bind = bind
        self._stderr_text = stderr_text
        self._exit_immediately = exit_immediately

    def __call__(self, argv: Sequence[str]) -> _FakeChild:
        assert isinstance(argv, (list, tuple))
        port = None
        for element in argv:
            if element.startswith("--local-host-port=127.0.0.1:"):
                port = int(element.rpartition(":")[2])
        child = _FakeChild(
            bind_port=port if self._bind else None,
            stderr_text=self._stderr_text,
        )
        child.argv_received = tuple(argv)
        if self._exit_immediately:
            child.returncode = 1
        self.children.append(child)
        return child


def _context_inspect_command(context: str) -> tuple[str, ...]:
    return ("docker", "context", "inspect", context, "--format", "{{json .Endpoints}}")


def _recorded_endpoint_result(port: int) -> CommandResult:
    """The inspect answer for a context already recording `tcp://127.0.0.1:<port>`."""
    return CommandResult(exit_code=0, stdout=f'{{"docker": {{"Host": "tcp://127.0.0.1:{port}"}}}}')


def _ok() -> CommandResult:
    return CommandResult(exit_code=0)


def _no_such_context_result(context: str) -> CommandResult:
    """The real `docker context inspect <absent-name>` outcome (verified wording)."""
    return CommandResult(
        exit_code=1,
        stderr=f'context "{context}": context not found: open ~/.docker/contexts/meta: '
        "no such file or directory",
    )


def _docker_runner(context: str, *, recorded_port: int | None = None) -> RecordingRunner:
    """The docker answers the connect/verify flows need, recorded for assertion.

    `recorded_port=None` answers the inspect with the context-absent wording,
    so `allocate_local_port` binds a fresh port and `ensure_context` takes
    its create path; a recorded port takes the reuse-and-update path. Every
    call is recorded, so the verify test can assert the never-activates rule
    by checking no `docker context use` was ever recorded.
    """

    def answer(command: tuple[str, ...]) -> CommandResult:
        if command == _context_inspect_command(context):
            if recorded_port is None:
                return _no_such_context_result(context)
            return _recorded_endpoint_result(recorded_port)
        if command[:3] == ("docker", "context", "create"):
            return _ok()
        if command[:3] == ("docker", "context", "update"):
            return _ok()
        if command == ("docker", "context", "use", context):
            return _ok()
        if command == ("docker", "--context", context, "version", "--format", "{{json .Server}}"):
            return CommandResult(exit_code=0, stdout='{"Version": "24.0.7", "ApiVersion": "1.44"}')
        raise AssertionError(f"unexpected command: {command}")

    return RecordingRunner(answer)


def _free_port() -> int:
    """A loopback port that is free right now, released for the test's use."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])
    finally:
        probe.close()


def _connect_argv(context: str) -> list[str]:
    return ["connect", "--name", _NAME, "--context", context]


def _verify_argv(context: str) -> list[str]:
    return ["verify", "--name", _NAME, "--context", context]


def _kill_once_handler_installed(
    monkeypatch: pytest.MonkeyPatch, transport_gcp: ModuleType
) -> None:
    """Arm a SIGTERM to this process once the supervise loop's handler is live.

    The module's own SIGTERM handler is installed inside
    `_run_until_interrupted`; signaling before that would take the default
    action and kill the test process. Interception (delegating to the real
    `signal.signal`) makes the arming deterministic, and the kill itself is
    the one real signal the suite sends.
    """
    installed = threading.Event()
    real_signal = signal.signal

    def intercept(signum: int, handler: object) -> object:
        result = real_signal(signum, handler)  # type: ignore[arg-type]
        if signum == signal.SIGTERM:
            installed.set()
        return result

    monkeypatch.setattr(transport_gcp.signal, "signal", intercept)

    def arm() -> None:
        if not installed.wait(_HANG_GUARD_SECONDS):
            return
        time.sleep(0.05)  # one poll cadence, so the read is parked before the signal
        os.kill(os.getpid(), signal.SIGTERM)

    threading.Thread(target=arm, daemon=True).start()


# ---------------------------------------------------------------------------
# The argument vector, and the module's hygiene.
# ---------------------------------------------------------------------------


def test_build_tunnel_argv_pins_the_gcloud_vector() -> None:
    transport_gcp = _import_transport_gcp()

    argv = transport_gcp.build_tunnel_argv(
        instance_name="acme", zone=_ZONE, project_id=_PROJECT, local_port=23761
    )

    assert argv == [
        "gcloud",
        "compute",
        "start-iap-tunnel",
        "acme",
        "2376",
        "--local-host-port=127.0.0.1:23761",
        "--zone",
        _ZONE,
        "--project",
        _PROJECT,
    ]


def test_tunnel_argv_never_carries_an_ssh_element() -> None:
    """This transport shares nothing with the ssh one: no ssh binary, flag,
    destination or user ever appears in the gcloud vector."""
    transport_gcp = _import_transport_gcp()

    argv = transport_gcp.build_tunnel_argv(
        instance_name="acme", zone=_ZONE, project_id=_PROJECT, local_port=23761
    )

    assert argv[0] == "gcloud"
    assert not any("ssh" in element.lower() for element in argv), argv


def test_module_imports_no_time_sleep_or_asyncio_sleep() -> None:
    """Nothing here waits on time: readiness is a port, pacing is a selector."""
    transport_gcp = _import_transport_gcp()
    source = inspect.getsource(transport_gcp)
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
        f"devcontainer_config.transport_gcp calls a 'sleep' function at least once: {sleep_calls!r}"
    )


# ---------------------------------------------------------------------------
# The address record: the triple, validated.
# ---------------------------------------------------------------------------


def test_gcp_values_reads_the_record_triple() -> None:
    transport_gcp = _import_transport_gcp()
    address = {
        "instance_name": _NAME,
        "zone": _ZONE,
        "project_id": _PROJECT,
        "region": "us-east1",
    }

    assert transport_gcp.gcp_values(address, instance=_NAME) == (_NAME, _ZONE, _PROJECT)


def test_gcp_values_refuses_a_missing_key_naming_the_deploy() -> None:
    transport_gcp = _import_transport_gcp()

    with pytest.raises(transport_gcp.GCPConfigError) as exc_info:
        transport_gcp.gcp_values({"instance_name": _NAME}, instance=_NAME)

    message = str(exc_info.value)
    assert "'zone'" in message
    assert str(instances.engine_address_path(_NAME)) in message
    assert f"make instance-deploy INSTANCE={_NAME}" in message


def test_gcp_values_refuses_a_malformed_zone_and_project() -> None:
    transport_gcp = _import_transport_gcp()

    for record in (
        {"instance_name": _NAME, "zone": "US-EAST1-B", "project_id": _PROJECT},
        {"instance_name": _NAME, "zone": "us-east1-b ssh://host", "project_id": _PROJECT},
        {"instance_name": _NAME, "zone": _ZONE, "project_id": "ab"},
    ):
        with pytest.raises(transport_gcp.GCPConfigError) as exc_info:
            transport_gcp.gcp_values(record, instance=_NAME)  # type: ignore[arg-type]

        message = str(exc_info.value)
        assert "malformed" in message
        assert f"make instance-deploy INSTANCE={_NAME}" in message


def test_gcp_values_refuses_an_unsanitized_instance_name() -> None:
    """The record must carry the sanitized name verbatim: the VM is deployed
    under it, so a raw instance name would aim the dial at nothing."""
    transport_gcp = _import_transport_gcp()

    with pytest.raises(transport_gcp.GCPConfigError) as exc_info:
        transport_gcp.gcp_values(
            {"instance_name": "My_Engine", "zone": _ZONE, "project_id": _PROJECT},
            instance=_NAME,
        )

    message = str(exc_info.value)
    assert "malformed 'instance_name'" in message
    assert "my-engine" in message
    assert f"make instance-deploy INSTANCE={_NAME}" in message


def test_main_connect_missing_record_names_the_deploy(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    transport_gcp = _import_transport_gcp()

    exit_code = transport_gcp.main(_connect_argv(_CONTEXT))

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "no engine address record" in captured.err
    assert f"make instance-deploy INSTANCE={_NAME}" in captured.err


# ---------------------------------------------------------------------------
# Readiness: a real loopback listener, child death, and the deadline.
# ---------------------------------------------------------------------------


def test_wait_tunnel_ready_answers_a_real_loopback_listener() -> None:
    transport_gcp = _import_transport_gcp()
    port = _free_port()
    child = _FakeChild(bind_port=port)

    transport_gcp.wait_tunnel_ready(
        port, child, transport_gcp._StderrTail(io.BytesIO(b"")), instance=_NAME
    )


def test_wait_tunnel_ready_surfaces_child_death_with_the_auth_remedy() -> None:
    """A child that dies before its listener opens is reported from its own
    tail, and the expired-credentials wording names the login and refresh."""
    transport_gcp = _import_transport_gcp()
    port = _free_port()
    child = _FakeChild(
        bind_port=None,
        stderr_text=(
            b"ERROR: (gcloud.auth) Your credentials have expired. "
            b"Run [gcloud auth login] to re-authenticate.\n"
        ),
    )
    child.returncode = 1
    tail = transport_gcp._StderrTail(io.BytesIO(child.stderr.getvalue()))

    with pytest.raises(transport_gcp.GAPTunnelError) as exc_info:
        transport_gcp.wait_tunnel_ready(port, child, tail, instance=_NAME)

    message = str(exc_info.value)
    assert "gcloud auth login" in message
    assert f"make connect-refresh INSTANCE={_NAME}" in message


def test_diagnose_tunnel_exit_classifies_the_terminated_vm_wording() -> None:
    transport_gcp = _import_transport_gcp()

    message = transport_gcp.diagnose_tunnel_exit(
        1,
        "ERROR: (gcloud.compute.start-iap-tunnel) Could not connect: permission denied "
        "while connecting to the instance.",
        instance=_NAME,
    )

    assert f"make instance-start INSTANCE={_NAME}" in message


def test_diagnose_tunnel_exit_carries_an_unclassified_tail_verbatim() -> None:
    transport_gcp = _import_transport_gcp()
    tail = "ERROR: something wholly new happened\n"

    message = transport_gcp.diagnose_tunnel_exit(3, tail, instance=_NAME)

    assert "something wholly new" in message
    assert f"make connect-refresh INSTANCE={_NAME}" in message


def test_wait_tunnel_ready_times_out_stops_the_child_and_names_the_env_var(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport_gcp = _import_transport_gcp()
    monkeypatch.setenv("GCP_TUNNEL_CONNECT_TIMEOUT", "1")
    port = _free_port()
    child = _FakeChild(bind_port=None)  # alive, but never opens its listener
    tail = transport_gcp._StderrTail(io.BytesIO(b""))

    with pytest.raises(transport_gcp.GAPTunnelError) as exc_info:
        transport_gcp.wait_tunnel_ready(port, child, tail, instance=_NAME)

    message = str(exc_info.value)
    assert "GCP_TUNNEL_CONNECT_TIMEOUT" in message
    assert child.terminated, "a timed-out child must be stopped, never left holding a port"


def test_wait_tunnel_ready_refuses_an_unusable_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport_gcp = _import_transport_gcp()
    monkeypatch.setenv("GCP_TUNNEL_CONNECT_TIMEOUT", "nope")

    with pytest.raises(transport_gcp.GCPTransportError) as exc_info:
        transport_gcp.wait_tunnel_ready(
            1, _FakeChild(), transport_gcp._StderrTail(io.BytesIO(b"")), instance=_NAME
        )

    assert "GCP_TUNNEL_CONNECT_TIMEOUT" in str(exc_info.value)


# ---------------------------------------------------------------------------
# The connect flow: marker, context wiring, teardown on every path.
# ---------------------------------------------------------------------------


def test_main_connect_full_flow_prints_the_marker_and_wires_the_context(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    transport_gcp = _import_transport_gcp()
    _issue_client_material(_NAME)
    _write_engine_address(_NAME)
    runner = _docker_runner(_CONTEXT, recorded_port=None)
    monkeypatch.setattr(transport_gcp.transport, "subprocess_command_runner", runner)
    launcher = _FakeLauncher(bind=True)
    monkeypatch.setattr(transport_gcp, "default_process_launcher", launcher)
    monkeypatch.setattr(
        transport_gcp, "_run_until_interrupted", lambda process, tail, **kwargs: None
    )

    exit_code = transport_gcp.main(_connect_argv(_CONTEXT))

    assert exit_code == 0
    child = launcher.children[0]
    # The argv is the pinned gcloud vector, and the port it binds is the one
    # the context was created at.
    assert child.argv_received is not None
    local_port_element = next(
        element
        for element in child.argv_received
        if element.startswith("--local-host-port=127.0.0.1:")
    )
    port = int(local_port_element.rpartition(":")[2])
    assert child.argv_received == (
        "gcloud",
        "compute",
        "start-iap-tunnel",
        _NAME,
        "2376",
        f"--local-host-port=127.0.0.1:{port}",
        "--zone",
        _ZONE,
        "--project",
        _PROJECT,
    )
    connected = [
        line for line in capsys.readouterr().out.splitlines() if line.startswith("Connected:")
    ]
    assert connected == [
        f"Connected: docker context '{_CONTEXT}' -> {_NAME} over iap (zone {_ZONE})."
    ]
    commands = runner.commands
    create_commands = [c for c in commands if c[:3] == ("docker", "context", "create")]
    assert len(create_commands) == 1
    docker_value = create_commands[0][create_commands[0].index("--docker") + 1]
    assert f"host=tcp://127.0.0.1:{port}" in docker_value
    for component in ("ca=", "cert=", "key="):
        assert component in docker_value, "the mTLS context must carry all three material paths"
    use_index = commands.index(("docker", "context", "use", _CONTEXT))
    assert commands.index(create_commands[0]) < use_index, (
        "the context must be created before it is activated"
    )
    assert child.terminated, "the handler's backstop must stop the fake-supervised child"
    try:
        socket.create_connection(("127.0.0.1", port), timeout=0.5)
        raise AssertionError(f"the port {port} is still listening after the connect returned")
    except ConnectionRefusedError:
        pass


def test_main_connect_child_death_during_readiness_names_the_remedy(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    transport_gcp = _import_transport_gcp()
    _issue_client_material(_NAME)
    _write_engine_address(_NAME)
    port = _free_port()
    runner = _docker_runner(_CONTEXT, recorded_port=port)
    monkeypatch.setattr(transport_gcp.transport, "subprocess_command_runner", runner)
    launcher = _FakeLauncher(
        bind=False,
        exit_immediately=True,
        stderr_text=(
            b"ERROR: (gcloud.auth) Your credentials have expired. "
            b"Run [gcloud auth login] to re-authenticate.\n"
        ),
    )
    monkeypatch.setattr(transport_gcp, "default_process_launcher", launcher)

    exit_code = transport_gcp.main(_connect_argv(_CONTEXT))

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "gcloud auth login" in captured.err
    assert f"make connect-refresh INSTANCE={_NAME}" in captured.err
    assert "Connected:" not in captured.out


def test_main_connect_retries_a_backend_refusal_then_connects(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A VM fresh from a power cycle answers IAP before its daemon listens.

    The child then dies in seconds with `failed to connect to backend`;
    the daemon retries the spawn bounded times and connects once the
    daemon is up, instead of surfacing a dead tunnel the operator would
    have to refresh by hand.
    """
    transport_gcp = _import_transport_gcp()
    _issue_client_material(_NAME)
    _write_engine_address(_NAME)
    port = _free_port()
    runner = _docker_runner(_CONTEXT, recorded_port=port)
    monkeypatch.setattr(transport_gcp.transport, "subprocess_command_runner", runner)
    refusal = b"ERROR: ... failed to connect to backend. (Failed to connect to port 2376)"
    children: list[_FakeChild] = []

    def refusing_then_healthy_launcher(argv: Sequence[str]) -> _FakeChild:
        if len(children) == 0:
            child = _FakeChild(bind_port=None, stderr_text=refusal)
            child.returncode = 1
        else:
            child = _FakeChild(bind_port=port)
            child.block_reads()
        children.append(child)
        return child

    monkeypatch.setattr(
        transport_gcp, "default_process_launcher", refusing_then_healthy_launcher
    )
    paced: list[float] = []
    monkeypatch.setattr(transport_gcp, "_pace", paced.append)
    monkeypatch.setenv("GCP_TUNNEL_CONNECT_TIMEOUT", str(_HANG_GUARD_SECONDS))
    _kill_once_handler_installed(monkeypatch, transport_gcp)

    exit_code = transport_gcp.main(_connect_argv(_CONTEXT))

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Connected:" in captured.out
    assert "retrying the tunnel" in captured.out
    assert len(children) == 2, "exactly one bounded retry before the connect"
    assert children[1].terminated, "the SIGTERM stop reaches the retried tunnel"
    assert paced == [5.0], "the retry paces once between attempts"


def test_main_connect_surfaces_a_persistent_backend_refusal_after_the_retries(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Three refusals in a row are a real failure, not a boot race."""
    transport_gcp = _import_transport_gcp()
    _issue_client_material(_NAME)
    _write_engine_address(_NAME)
    port = _free_port()
    runner = _docker_runner(_CONTEXT, recorded_port=port)
    monkeypatch.setattr(transport_gcp.transport, "subprocess_command_runner", runner)
    refusal = b"ERROR: ... failed to connect to backend. (Failed to connect to port 2376)"
    launcher = _FakeLauncher(bind=False, stderr_text=refusal, exit_immediately=True)
    monkeypatch.setattr(transport_gcp, "default_process_launcher", launcher)

    exit_code = transport_gcp.main(_connect_argv(_CONTEXT))

    captured = capsys.readouterr()
    assert exit_code == 1
    assert len(launcher.children) == 3, "three bounded attempts, no more"
    assert "Connected:" not in captured.out


def test_supervise_child_death_raises_the_tail_with_its_remedy() -> None:
    """A tunnel that exits while served is a dropped tunnel, whatever its code.

    `_run_until_interrupted` runs for real here: the child's stdout is at
    EOF (it exited), and its tail carries the terminated-VM wording, so the
    raised error must name `make instance-start`.
    """
    transport_gcp = _import_transport_gcp()
    child = _FakeChild(stderr_text=b"ERROR: (gcloud.compute.start-iap-tunnel) Could not connect.\n")
    child.returncode = 1
    tail = transport_gcp._StderrTail(io.BytesIO(child.stderr.getvalue()))

    with pytest.raises(transport_gcp.GAPTunnelError) as exc_info:
        transport_gcp._run_until_interrupted(child, tail, instance=_NAME, port=_free_port())

    assert f"make instance-start INSTANCE={_NAME}" in str(exc_info.value)


def test_supervise_sigterm_stops_the_child_and_verifies_the_port_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The interrupt path is real end to end: one SIGTERM, child stopped, port closed.

    The signal is armed only once the supervise loop's own SIGTERM handler
    is provably installed (the interception in `_kill_once_handler_installed`),
    and the whole test is bounded by the hang guard through the readiness
    and port-closed budgets.
    """
    transport_gcp = _import_transport_gcp()
    monkeypatch.setenv("GCP_TUNNEL_CONNECT_TIMEOUT", str(_HANG_GUARD_SECONDS))
    _issue_client_material(_NAME)
    _write_engine_address(_NAME)
    port = _free_port()
    runner = _docker_runner(_CONTEXT, recorded_port=port)
    monkeypatch.setattr(transport_gcp.transport, "subprocess_command_runner", runner)
    launcher = _FakeLauncher(bind=True)
    original_launcher = launcher

    def blocking_launcher(argv: Sequence[str]) -> _FakeChild:
        child = original_launcher(argv)
        child.block_reads()
        return child

    monkeypatch.setattr(transport_gcp, "default_process_launcher", blocking_launcher)
    _kill_once_handler_installed(monkeypatch, transport_gcp)

    exit_code = transport_gcp.main(_connect_argv(_CONTEXT))

    assert exit_code == 0, "the SIGTERM stop must exit 0, which the forwards lifecycle relies on"
    child = launcher.children[0]
    assert child.terminated, "the interrupt must stop the tunnel child"
    try:
        socket.create_connection(("127.0.0.1", port), timeout=0.5)
        raise AssertionError(f"the port {port} is still listening after the SIGTERM stop")
    except (ConnectionRefusedError, OSError):
        pass


def test_main_verify_handshakes_tears_down_and_never_activates(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The verify path proves the whole path and leaves nothing running.

    The context survives created but never activated -- a strict runner
    turns any `docker context use` into a failure -- and the tunnel child
    is torn down on every path out.
    """
    transport_gcp = _import_transport_gcp()
    _issue_client_material(_NAME)
    _write_engine_address(_NAME)
    port = _free_port()
    runner = _docker_runner(_CONTEXT, recorded_port=port)
    monkeypatch.setattr(transport_gcp.transport, "subprocess_command_runner", runner)
    launcher = _FakeLauncher(bind=True)
    monkeypatch.setattr(transport_gcp, "default_process_launcher", launcher)

    exit_code = transport_gcp.main(_verify_argv(_CONTEXT))

    captured = capsys.readouterr()
    assert exit_code == 0
    verified = [line for line in captured.out.splitlines() if line.startswith("Verified:")]
    assert verified == [
        f"Verified: docker context '{_CONTEXT}' -> {_NAME} over iap (zone {_ZONE}) (server 24.0.7)."
    ]
    assert (
        "docker",
        "--context",
        _CONTEXT,
        "version",
        "--format",
        "{{json .Server}}",
    ) in runner.commands
    assert not any(command[:3] == ("docker", "context", "use") for command in runner.commands), (
        "a proof must not switch the machine-wide docker context"
    )
    child = launcher.children[0]
    assert child.terminated, "verify must tear the tunnel down on every path out"


def test_main_verify_handshake_failure_still_tears_the_tunnel_down(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    transport_gcp = _import_transport_gcp()
    _issue_client_material(_NAME)
    _write_engine_address(_NAME)
    port = _free_port()

    def answer(command: tuple[str, ...]) -> CommandResult:
        if command == _context_inspect_command(_CONTEXT):
            return _recorded_endpoint_result(port)
        if command[:3] == ("docker", "context", "create"):
            return _ok()
        if command[:3] == ("docker", "context", "update"):
            return _ok()
        if command[:2] == ("docker", "--context"):
            return CommandResult(exit_code=1, stderr="Cannot connect to the Docker daemon")
        raise AssertionError(f"unexpected command: {command}")

    runner = RecordingRunner(answer)
    monkeypatch.setattr(transport_gcp.transport, "subprocess_command_runner", runner)
    launcher = _FakeLauncher(bind=True)
    monkeypatch.setattr(transport_gcp, "default_process_launcher", launcher)

    exit_code = transport_gcp.main(_verify_argv(_CONTEXT))

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "Cannot connect" in captured.err
    assert "Verified:" not in captured.out
    assert launcher.children[0].terminated, "a failed handshake must still tear the tunnel down"


def test_main_verify_without_client_material_names_the_cert_remedy(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The certificate precondition runs before any tunnel is spawned."""
    transport_gcp = _import_transport_gcp()
    _write_engine_address(_NAME)
    launcher = _FakeLauncher()
    monkeypatch.setattr(transport_gcp, "default_process_launcher", launcher)

    exit_code = transport_gcp.main(_verify_argv(_CONTEXT))

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "no client certificate exists" in captured.err
    assert launcher.children == [], "no tunnel may spawn behind a refused certificate"


# ---------------------------------------------------------------------------
# The boot-applied install: the compare and wait logic the cert-install
# flow and the start path drive. The runner double here is in the
# `(argv, stdin) -> CompletedProcess` shape -- the `instance_ops.Runner`
# shape these helpers take -- not the `CommandRunner` shape the tunnel
# tests above use, because the guest-attribute and secret reads are plain
# gcloud subprocess calls.
# ---------------------------------------------------------------------------


class _GcloudFake:
    """A strict runner double in the `(argv, stdin)` shape, answering from a
    fixture map with an optional function of the argv.

    A command the fixture map does not name raises, so a stray gcloud call
    fails the test instead of passing silently; a callable value is called
    with the argv so a test can answer statefully (an attribute that flips
    to TRUE on the third poll, for example).
    """

    def __init__(self, responses: dict[tuple[str, ...], object]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, argv: Sequence[str], stdin: str | None) -> subprocess.CompletedProcess[str]:
        key = tuple(argv)
        self.calls.append(key)
        answer = self.responses.get(key)
        if callable(answer):
            return answer(key)
        if answer is None:
            raise AssertionError(f"_GcloudFake received an unrecorded command: {key!r}")
        return answer


def _gcp_ok(stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


def _gcp_err(stderr: str, *, returncode: int = 1) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout="", stderr=stderr)


def _attribute_command(key: str) -> tuple[str, ...]:
    return (
        "gcloud",
        "compute",
        "instances",
        "get-guest-attributes",
        _NAME,
        f"--query-path=general-dev/{key}",
        "--zone",
        _ZONE,
        "--project",
        _PROJECT,
        "--format",
        "value(value)",
    )


def _checksum_secret_command() -> tuple[str, ...]:
    return (
        "gcloud",
        "secrets",
        "versions",
        "access",
        "latest",
        "--secret",
        f"{_NAME}-tls-checksum",
        "--project",
        _PROJECT,
    )


_CHECKSUM = "a" * 64


def _checksum_fixture(
    *, published: str = _CHECKSUM, applied: str | None = _CHECKSUM
) -> _GcloudFake:
    """The two-read fixture every comparison drives: the store's checksum and
    the VM's applied attribute, each addressable independently."""
    responses: dict[tuple[str, ...], object] = {
        _checksum_secret_command(): _gcp_ok(f"{published}\n"),
    }
    if applied is None:
        responses[_attribute_command("tls-checksum")] = _gcp_err(
            "ERROR: (gcloud.compute.instances.get-guest-attributes) general-dev/tls-checksum "
            "not found",
        )
    else:
        responses[_attribute_command("tls-checksum")] = _gcp_ok(f"{applied}\n")
    return _GcloudFake(responses)


def test_gcp_material_record_values_reads_the_quadruple() -> None:
    transport_gcp = _import_transport_gcp()
    _write_engine_address(_NAME)
    record = instances.engine_address(_repo_root(), _NAME)

    assert transport_gcp.gcp_material_record_values(record, instance=_NAME) == (
        _NAME,
        _ZONE,
        _PROJECT,
        _NAME,
    )


def test_gcp_material_record_values_refuses_a_record_without_a_prefix() -> None:
    transport_gcp = _import_transport_gcp()
    record = {
        instances.GCP_INSTANCE_NAME_KEY: _NAME,
        instances.ZONE_KEY: _ZONE,
        instances.PROJECT_ID_KEY: _PROJECT,
    }

    with pytest.raises(transport_gcp.GCPConfigError, match="make instance-deploy"):
        transport_gcp.gcp_material_record_values(record, instance=_NAME)


def test_gcp_material_record_values_refuses_an_unsanitized_prefix() -> None:
    transport_gcp = _import_transport_gcp()
    record = {
        instances.GCP_INSTANCE_NAME_KEY: _NAME,
        instances.ZONE_KEY: _ZONE,
        instances.PROJECT_ID_KEY: _PROJECT,
        instances.SECRETS_PREFIX_KEY: "Not_Sanitized",
    }

    with pytest.raises(transport_gcp.GCPConfigError, match="secrets_prefix"):
        transport_gcp.gcp_material_record_values(record, instance=_NAME)


def test_guest_attribute_argv_pins_the_format_flag() -> None:
    """The format flag is not optional: without it gcloud prints a table."""
    transport_gcp = _import_transport_gcp()

    assert transport_gcp.build_guest_attribute_argv(
        instance_name=_NAME, zone=_ZONE, project_id=_PROJECT, key="tls-checksum"
    ) == list(_attribute_command("tls-checksum"))


def test_read_guest_attribute_trims_and_answers_absent() -> None:
    transport_gcp = _import_transport_gcp()
    _write_engine_address(_NAME)
    record = instances.engine_address(_repo_root(), _NAME)

    present = _GcloudFake({_attribute_command("tls-checksum"): _gcp_ok("TRUE\n")})
    assert (
        transport_gcp.read_guest_attribute(record, present, instance=_NAME, key="tls-checksum")
        == "TRUE"
    )

    absent_stderr = _GcloudFake({_attribute_command("tls-checksum"): _gcp_err("... not found ...")})
    assert (
        transport_gcp.read_guest_attribute(
            record, absent_stderr, instance=_NAME, key="tls-checksum"
        )
        is None
    )

    empty = _GcloudFake({_attribute_command("tls-checksum"): _gcp_ok("")})
    assert (
        transport_gcp.read_guest_attribute(record, empty, instance=_NAME, key="tls-checksum")
        is None
    )


def test_read_guest_attribute_raises_a_real_failure_with_the_command() -> None:
    transport_gcp = _import_transport_gcp()
    _write_engine_address(_NAME)
    record = instances.engine_address(_repo_root(), _NAME)
    runner = _GcloudFake(
        {_attribute_command("tls-checksum"): _gcp_err("ERROR: zone does not exist")}
    )

    with pytest.raises(transport_gcp.GCPTransportError, match="zone does not exist"):
        transport_gcp.read_guest_attribute(record, runner, instance=_NAME, key="tls-checksum")


def test_read_published_checksum_pins_the_access_argv_and_trims() -> None:
    transport_gcp = _import_transport_gcp()
    _write_engine_address(_NAME)
    record = instances.engine_address(_repo_root(), _NAME)
    runner = _GcloudFake({_checksum_secret_command(): _gcp_ok(f"{_CHECKSUM}\n")})

    assert transport_gcp.read_published_checksum(record, runner, instance=_NAME) == _CHECKSUM
    assert runner.calls == [_checksum_secret_command()]


def test_read_published_checksum_refuses_a_missing_secret_naming_the_publish() -> None:
    transport_gcp = _import_transport_gcp()
    _write_engine_address(_NAME)
    record = instances.engine_address(_repo_root(), _NAME)
    runner = _GcloudFake({_checksum_secret_command(): _gcp_err("ERROR: ... not found: 404.")})

    with pytest.raises(transport_gcp.GCPConfigError, match="cert-publish"):
        transport_gcp.read_published_checksum(record, runner, instance=_NAME)


def test_applied_checksum_matches_answers_true_false_and_drifted() -> None:
    transport_gcp = _import_transport_gcp()
    _write_engine_address(_NAME)
    record = instances.engine_address(_repo_root(), _NAME)

    assert transport_gcp.applied_checksum_matches(record, _checksum_fixture(), instance=_NAME)
    assert not transport_gcp.applied_checksum_matches(
        record, _checksum_fixture(applied=None), instance=_NAME
    ), "an absent attribute is the never-applied state, not an error"
    assert not transport_gcp.applied_checksum_matches(
        record, _checksum_fixture(applied="b" * 64), instance=_NAME
    ), "a drifted attribute is the reboot case, not an error"


def test_wait_applied_checksum_returns_the_checksum_once_applied() -> None:
    transport_gcp = _import_transport_gcp()
    _write_engine_address(_NAME)
    record = instances.engine_address(_repo_root(), _NAME)
    # The attribute flips to the published checksum on the third poll; the
    # flip is decided by the poll count, never by time.
    polls = {"count": 0}

    def flip(argv: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        polls["count"] += 1
        if polls["count"] >= 3:
            return _gcp_ok(f"{_CHECKSUM}\n")
        return _gcp_ok(f"{'b' * 64}\n")

    runner = _GcloudFake(
        {
            _checksum_secret_command(): _gcp_ok(_CHECKSUM),
            _attribute_command("tls-checksum"): flip,
        }
    )
    ticks = iter(range(0, 100, 10))
    paced: list[float] = []

    checksum = transport_gcp.wait_applied_checksum(
        record,
        runner,
        instance=_NAME,
        pace=paced.append,
        clock=lambda: next(ticks),
        deadline_seconds=90.0,
    )

    assert checksum == _CHECKSUM
    assert polls["count"] == 3, (
        "the flip is decided by the poll count: two drifted reads, then the match"
    )
    assert len(paced) == 1, "one drifted loop means one paced wait, never a fixed sleep"


def test_wait_applied_checksum_times_out_naming_the_budget_and_the_vm_error() -> None:
    transport_gcp = _import_transport_gcp()
    _write_engine_address(_NAME)
    record = instances.engine_address(_repo_root(), _NAME)
    runner = _GcloudFake(
        {
            _checksum_secret_command(): _gcp_ok(_CHECKSUM),
            _attribute_command("tls-checksum"): _gcp_ok("c" * 64),
            _attribute_command("error"): _gcp_ok("fetch failed: 403"),
        }
    )
    ticks = iter(range(0, 100, 10))

    with pytest.raises(transport_gcp.GCPTransportError) as excinfo:
        transport_gcp.wait_applied_checksum(
            record,
            runner,
            instance=_NAME,
            pace=lambda _seconds: None,
            clock=lambda: next(ticks),
            deadline_seconds=15.0,
        )

    message = str(excinfo.value)
    assert transport_gcp.MATERIAL_APPLY_TIMEOUT_ENV_VAR in message
    assert "fetch failed: 403" in message, "the VM's own diagnostic decorates the timeout"


def test_wait_applied_checksum_stops_immediately_on_an_expired_session() -> None:
    """An expired gcloud session is the one cause no budget can fix: the wait
    stops on the first read instead of burning the deadline."""
    transport_gcp = _import_transport_gcp()
    _write_engine_address(_NAME)
    record = instances.engine_address(_repo_root(), _NAME)
    runner = _GcloudFake(
        {
            _checksum_secret_command(): _gcp_err(
                "Your credentials have expired. Run gcloud auth login"
            )
        }
    )
    paced: list[float] = []

    with pytest.raises(transport_gcp.GCPTransportError, match="gcloud auth login"):
        transport_gcp.wait_applied_checksum(
            record,
            runner,
            instance=_NAME,
            pace=paced.append,
            clock=lambda: 0.0,
            deadline_seconds=900.0,
        )

    assert paced == []


def test_wait_provisioned_returns_on_the_true_marker() -> None:
    transport_gcp = _import_transport_gcp()
    _write_engine_address(_NAME)
    record = instances.engine_address(_repo_root(), _NAME)
    runner = _GcloudFake({_attribute_command("provisioned"): _gcp_ok("TRUE\n")})

    transport_gcp.wait_provisioned(
        record, runner, instance=_NAME, pace=lambda _seconds: None, clock=lambda: 0.0
    )


def test_wait_provisioned_polls_past_absence_and_times_out() -> None:
    transport_gcp = _import_transport_gcp()
    _write_engine_address(_NAME)
    record = instances.engine_address(_repo_root(), _NAME)
    polls = {"count": 0}

    def absent_then_present(argv: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        polls["count"] += 1
        if polls["count"] >= 2:
            return _gcp_ok("TRUE\n")
        return _gcp_err("not found")

    runner = _GcloudFake({_attribute_command("provisioned"): absent_then_present})
    first_ticks = iter(range(0, 100, 10))

    transport_gcp.wait_provisioned(
        record,
        runner,
        instance=_NAME,
        pace=lambda _seconds: None,
        clock=lambda: next(first_ticks),
        deadline_seconds=60.0,
    )
    assert polls["count"] == 2

    always_absent = _GcloudFake(
        {
            _attribute_command("provisioned"): _gcp_err("not found"),
            _attribute_command("error"): _gcp_err("not found"),
        }
    )
    second_ticks = iter(range(0, 100, 10))
    with pytest.raises(transport_gcp.GCPTransportError) as excinfo:
        transport_gcp.wait_provisioned(
            record,
            always_absent,
            instance=_NAME,
            pace=lambda _seconds: None,
            clock=lambda: next(second_ticks),
            deadline_seconds=15.0,
        )

    assert transport_gcp.PROVISIONED_TIMEOUT_ENV_VAR in str(excinfo.value)


def test_material_timeouts_read_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    transport_gcp = _import_transport_gcp()
    monkeypatch.setenv(transport_gcp.MATERIAL_APPLY_TIMEOUT_ENV_VAR, "12.5")
    monkeypatch.setenv(transport_gcp.PROVISIONED_TIMEOUT_ENV_VAR, "45")
    assert transport_gcp.material_apply_timeout_seconds() == 12.5
    assert transport_gcp.provisioned_timeout_seconds() == 45.0

    monkeypatch.setenv(transport_gcp.MATERIAL_APPLY_TIMEOUT_ENV_VAR, "nope")
    with pytest.raises(transport_gcp.GCPTransportError, match="positive"):
        transport_gcp.material_apply_timeout_seconds()
