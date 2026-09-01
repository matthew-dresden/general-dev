"""SSM port forward manager: the transport that carries the docker API (spec Section 4.5).

This is the functional replacement for
`.devcontainer/remote-docker/docker-tunnel.sh`, which opens SSH inside an SSM
session and writes a `ProxyCommand` invoking `AWS-StartSSHSession` (spec
Section 1.4). That script is not deleted here -- phase 4 deletes it in
E7-F1-S1-T1 (spec Section 11.5) -- so it keeps working unchanged for the
length of phase 3. This module never touches it and imports nothing from it.

The rootless daemon on the instance listens on `127.0.0.1:DOCKER_TLS_PORT`
and on nothing else, and the security group carries zero ingress rules (spec
Section 5.6), so no packet from the network reaches the daemon at all.
`aws ssm start-session --document-name AWS-StartPortForwardingSession` maps
that loopback listener to a local port on the laptop; no SSH element is ever
built into the argument vector this module constructs (AC-10.3), which
`build_start_session_argv` asserts is possible to verify directly rather than
by trusting prose.

Two independent factors gate the daemon (spec Section 3.6.2): `ssm:StartSession`
scoped to the port-forwarding document decides who may open this tunnel at
all -- and, since Docker has neither CRL nor OCSP, revoking that permission is
also the only revocation mechanism this platform has (spec Section 3.6.3) --
and mTLS, added in E6-F2-S1-T2, decides who may command the daemon once the
tunnel is open. Neither layer substitutes for the other.

The local port is allocated per instance, recorded, and never a fixed number
(spec Section 9). The record is the docker context endpoint itself (spec
Section 1.1's "the active docker context is the only source of truth"):
`allocate_local_port` reuses the port already recorded in an existing
context's endpoint so the context is stable across reconnects, and obtains a
free port by binding port zero when no context exists yet. A recorded port
already held by a foreign listener is a fail-fast naming the port
(`PortOccupiedError`), never a silent move to a different one, because a
silent move would strand every context and configuration that recorded the
old value.

Nothing here waits on time (spec Section 7.2). A plain connect-and-retry
loop against the local port cannot pace itself the way its own docstring
once claimed: connecting to a definitely-refused loopback port returns in
microseconds, not after any bounded delay, so a loop built on that alone is
an unthrottled busy spin, not a paced poll -- exactly what code review
measured (40,825 real `tcp_ready_probe` iterations per second). `wait_ready`
instead blocks on the session process's own `stdout` (`session_output`)
through `selectors`, a real I/O-readiness wait with no fixed interval and no
busy loop, until the session-manager-plugin prints its own "Port ... opened
for sessionId ..." announcement (a real, external event, not an elapsed
duration); only then does it call the caller-supplied readiness probe --
production code connects to the local port (`tcp_ready_probe`) -- to confirm
the port actually answers before returning, so readiness is still decided by
a connection attempt (AC-FUNC-004), never by the announcement or the process
merely being alive. The whole wait remains bounded by `SSM_FORWARD_TIMEOUT`,
defaulting to 30 seconds (spec Section 7.3): each blocking read is bounded by
the time remaining until that deadline, and this module contains no
`time.sleep`/`asyncio.sleep` call anywhere.

Every AWS and docker CLI invocation is issued through an injected
`devcontainer_config.hostprobe.CommandRunner`, reused from that module
instead of a second, independent runner type (DRY): the unit suite drives
every function below with a fake runner and never shells out, never opens a
network connection, and opens no socket beyond a loopback listener a test
creates for itself (AC-TEST-005). `subprocess_command_runner` and
`default_process_launcher` are this module's own production implementations
of that seam and of the long-lived session-process launcher, following the
same pattern `devcontainer_config.catalog.subprocess_runner` already
establishes for its own domain: the seam is injected everywhere else in this
module, and the real, subprocess-backed implementation lives in exactly one
function that nothing else in this module calls internally.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import selectors
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from typing import IO

from devcontainer_config.hostprobe import CommandResult, CommandRunner

DOCKER_TLS_PORT_ENV_VAR = "DOCKER_TLS_PORT"
_DOCKER_TLS_PORT_DEFAULT = 2376

SSM_FORWARD_TIMEOUT_ENV_VAR = "SSM_FORWARD_TIMEOUT"
_SSM_FORWARD_TIMEOUT_DEFAULT_SECONDS = 30.0

# spec Section 13, decision D2: the port-forwarding document, never the SSH
# document (`AWS-StartSSHSession`) the replaced transport used.
PORT_FORWARDING_DOCUMENT = "AWS-StartPortForwardingSession"

ONLINE_PING_STATUS = "Online"

_LOCALHOST = "127.0.0.1"

_TCP_ENDPOINT_PATTERN = re.compile(r"^tcp://(?P<host>[^:/]+):(?P<port>\d+)$")

# The substring the real `session-manager-plugin` prints to stdout once the
# local end of an `AWS-StartPortForwardingSession` forward is listening, for
# example "Port 54321 opened for sessionId session-01234567890abcdef.".
# `wait_ready` waits on this real, external announcement instead of busy
# spinning a connect attempt against a port that is, until this line
# appears, definitely not listening.
_PORT_OPENED_MARKER = b"opened for sessionId"

# Substrings the AWS CLI's stderr carries for an unusable credential --
# expired SSO session or none resolved at all -- matched literally against
# plain-text CLI error output, the same technique
# `devcontainer_config.hostprobe.probe_aws_identity` and
# `devcontainer_config.catalog` each already use for their own AWS call
# sites (independent copies by design: each module owns translating the
# identical AWS-CLI-observable condition for its own call sites, rather than
# sharing a private constant across module boundaries).
_EXPIRED_CREDENTIAL_MARKERS: tuple[str, ...] = (
    "Error loading SSO Token",
    "session associated with this profile has expired",
    "ExpiredToken",
    "Token has expired",
    "security token included in the request is expired",
    "Unable to locate credentials",
)


class TransportError(RuntimeError):
    """Base for every operational failure this module raises.

    Every failure this module can produce is a `TransportError` (or a more
    specific subclass below), never a generic `Exception`, so `main` has one
    `except` clause that turns any of them into a clean, non-zero exit
    instead of a stack trace.
    """


class AgentNotOnlineError(TransportError):
    """Raised when the target instance's SSM agent is not reporting `Online`."""


class CredentialsExpiredError(TransportError):
    """Raised when the AWS CLI reports an expired or absent credential."""


class PortOccupiedError(TransportError):
    """Raised when the port recorded for a docker context is held by another process."""


class ForwardTimeoutError(TransportError):
    """Raised when the local port does not accept a connection within `SSM_FORWARD_TIMEOUT`."""


# ---------------------------------------------------------------------------
# Production seams: real subprocesses. Nothing else in this module calls
# either of these two functions; every other function takes its runner or
# launcher as a parameter instead (spec Section 3.4's dependency-injection
# rule, and the module docstring).
# ---------------------------------------------------------------------------


def subprocess_command_runner(
    command: Sequence[str], timeout_seconds: float | None
) -> CommandResult:
    """The production `CommandRunner`: a real, bounded subprocess.

    Shaped exactly like `devcontainer_config.hostprobe.CommandRunner` so this
    module's own callers (and `hostprobe`'s) can share one runner instance.
    `FileNotFoundError` (the binary is not on PATH) and
    `subprocess.TimeoutExpired` (the process did not exit within
    `timeout_seconds`) are both real outcomes, not exceptions a caller of
    this seam should ever have to catch: each becomes its own distinct
    `CommandResult` field, the same three-outcomes contract
    `hostprobe.CommandResult`'s own docstring establishes.
    """
    try:
        completed = subprocess.run(
            list(command), capture_output=True, text=True, timeout=timeout_seconds, check=False
        )
    except FileNotFoundError:
        return CommandResult(exit_code=127, binary_missing=True)
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        return CommandResult(exit_code=-1, stdout=stdout, stderr=stderr, timed_out=True)
    return CommandResult(
        exit_code=completed.returncode, stdout=completed.stdout, stderr=completed.stderr
    )


ProcessLauncher = Callable[[Sequence[str]], "subprocess.Popen[bytes]"]


def default_process_launcher(command: Sequence[str]) -> subprocess.Popen[bytes]:
    """The production `ProcessLauncher`: a real, long-lived child process.

    `stdout` is piped so `wait_ready` can block on it for the session-
    manager-plugin's own readiness announcement (`_PORT_OPENED_MARKER`)
    instead of busy spinning a connect attempt against a port that is not
    listening yet; `stderr` is left inherited so an operator watching the
    terminal still sees the plugin's own diagnostics directly. `start_forward`
    never calls this itself in production code paths that are unit-tested;
    `main`'s `start` command is the only call site, so a test drives
    `start_forward` with a fake launcher instead of ever spawning a real
    `aws` process.
    """
    return subprocess.Popen(list(command), stdout=subprocess.PIPE)


# ---------------------------------------------------------------------------
# Configuration: DOCKER_TLS_PORT and SSM_FORWARD_TIMEOUT, each read from the
# environment in exactly this one function apiece (AC-FUNC-006). No other
# line in this module calls `os.environ.get` for either name.
# ---------------------------------------------------------------------------


def _docker_tls_port() -> int:
    raw = os.environ.get(DOCKER_TLS_PORT_ENV_VAR)
    if raw is None:
        return _DOCKER_TLS_PORT_DEFAULT
    try:
        value = int(raw)
    except ValueError as exc:
        raise TransportError(
            f"ERROR: {DOCKER_TLS_PORT_ENV_VAR}={raw!r} is not an integer\n"
            "Set it to a TCP port number between 1 and 65535, or unset it to use the "
            f"default of {_DOCKER_TLS_PORT_DEFAULT}."
        ) from exc
    if not (0 < value <= 65535):
        raise TransportError(
            f"ERROR: {DOCKER_TLS_PORT_ENV_VAR}={raw!r} must be between 1 and 65535\n"
            "Set it to a TCP port number between 1 and 65535, or unset it to use the "
            f"default of {_DOCKER_TLS_PORT_DEFAULT}."
        )
    return value


def _forward_timeout_seconds() -> float:
    raw = os.environ.get(SSM_FORWARD_TIMEOUT_ENV_VAR)
    if raw is None:
        return _SSM_FORWARD_TIMEOUT_DEFAULT_SECONDS
    try:
        value = float(raw)
    except ValueError as exc:
        raise TransportError(
            f"ERROR: {SSM_FORWARD_TIMEOUT_ENV_VAR}={raw!r} is not a number\n"
            "Set it to a positive number of seconds, or unset it to use the default of "
            f"{_SSM_FORWARD_TIMEOUT_DEFAULT_SECONDS:g}."
        ) from exc
    if not math.isfinite(value) or value <= 0:
        raise TransportError(
            f"ERROR: {SSM_FORWARD_TIMEOUT_ENV_VAR}={raw!r} must be a finite positive number\n"
            "Set it to a positive number of seconds, or unset it to use the default of "
            f"{_SSM_FORWARD_TIMEOUT_DEFAULT_SECONDS:g}."
        )
    return value


# ---------------------------------------------------------------------------
# Preconditions: the SSM agent must be Online, and the AWS credential behind
# `profile` must be live, before this module ever attempts to open a
# session (spec Section 4.1.4).
# ---------------------------------------------------------------------------


def _run_aws_cli(
    runner: CommandRunner, args: Sequence[str], *, profile: str, region: str
) -> CommandResult:
    """Issue one `aws` subcommand with `profile`/`region` attached, translating credential failure.

    The single site every AWS CLI invocation in this module funnels through
    (Approach step 6, DRY): a `profile`/`region` pair is appended exactly
    once here instead of at every call site, and an expired or absent
    credential is translated into `CredentialsExpiredError` naming
    `aws sso login --profile <profile>` exactly once here instead of a
    second, independent copy of that translation existing anywhere else in
    this module.
    """
    command = ("aws", *args, "--profile", profile, "--region", region, "--output", "json")
    result = runner(command, None)
    if result.binary_missing:
        raise TransportError("ERROR: aws is not on PATH\nInstall the AWS CLI v2, then retry.")
    if result.exit_code == 0:
        return result
    if any(marker in result.stderr for marker in _EXPIRED_CREDENTIAL_MARKERS):
        raise CredentialsExpiredError(
            f"ERROR: AWS credentials for profile {profile!r} are not usable\n"
            f"{result.stderr.strip()}\n"
            f"Run 'aws sso login --profile {profile}' to refresh the session, then retry."
        )
    raise TransportError(f"ERROR: aws {' '.join(args)} failed\n{result.stderr.strip()}")


def agent_status(
    runner: CommandRunner, *, instance_id: str, profile: str, region: str
) -> str | None:
    """The SSM ping status AWS reports for `instance_id`, or `None` if SSM has no record of it.

    `None` is a distinct, meaningful answer (an empty `InstanceInformationList`),
    not an error: it means SSM has never heard from this instance, because it
    is stopped, terminated, or the configured id names the wrong instance
    entirely (`ensure_agent_online` is what turns either outcome into a
    fail-fast).
    """
    result = _run_aws_cli(
        runner,
        (
            "ssm",
            "describe-instance-information",
            "--filters",
            f"Key=InstanceIds,Values={instance_id}",
        ),
        profile=profile,
        region=region,
    )
    try:
        payload = json.loads(result.stdout)
        entries = payload["InstanceInformationList"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise TransportError(
            "ERROR: aws ssm describe-instance-information produced unparsable output: "
            f"{result.stdout.strip()!r}\n"
            "Run the command manually and investigate why its output does not match the "
            "expected shape."
        ) from exc
    if not entries:
        return None
    return str(entries[0]["PingStatus"])


def ensure_agent_online(
    runner: CommandRunner, *, instance_id: str, profile: str, region: str
) -> None:
    """Raise `AgentNotOnlineError` unless `instance_id`'s SSM agent reports `Online`.

    Called before this module ever attempts `start_forward`: a forward
    against an instance whose agent is not `Online` cannot succeed, and
    attempting it anyway would report the wrong cause (a forward timeout)
    for what is actually an agent-state problem.
    """
    status = agent_status(runner, instance_id=instance_id, profile=profile, region=region)
    if status == ONLINE_PING_STATUS:
        return
    if status is None:
        raise AgentNotOnlineError(
            f"ERROR: SSM has no record of instance {instance_id!r}\n"
            "PingStatus is None, meaning the instance is stopped, terminated, or the "
            "configured instance id names the wrong instance.\n"
            "Confirm the instance id and that the instance is running, then retry."
        )
    raise AgentNotOnlineError(
        f"ERROR: SSM agent for instance {instance_id!r} is not Online\n"
        f"Reported ping status: {status}.\n"
        "Wait for the SSM agent to report Online, then retry."
    )


# ---------------------------------------------------------------------------
# Port allocation and the record (spec Section 9).
# ---------------------------------------------------------------------------


def _existing_context_port(runner: CommandRunner, context_name: str) -> int | None:
    """The local TCP port already recorded in `context_name`'s docker endpoint, or `None`.

    `None` is returned ONLY for a positively identified absence: docker's
    own `context "<name>": context not found` stderr, naming this exact
    context. Every other non-zero exit -- the docker CLI missing from PATH
    (`result.binary_missing`), an unreadable or misdirected `DOCKER_CONFIG`,
    a permission error, or a corrupted context store -- raises `TransportError`
    naming the context and docker's own stderr instead, the same fail-fast
    `_run_aws_cli` already applies to its own `binary_missing` case: reading
    any of those as "nothing recorded to reuse" would let `allocate_local_port`
    silently bind a brand-new port while the context on disk may still
    record the old one, which is exactly the port-stranding failure mode
    `PortOccupiedError` exists to prevent. A context that DOES exist but
    whose endpoint this function cannot parse as `tcp://<host>:<port>` is a
    distinct, louder failure for the same reason: the record itself is
    unusable, and silently treating it as "nothing to reuse" would allocate
    a second, different port for a context a caller believes already has
    one.
    """
    command = ("docker", "context", "inspect", context_name, "--format", "{{json .Endpoints}}")
    result = runner(command, None)
    if result.binary_missing:
        raise TransportError(
            "ERROR: docker is not on PATH\nInstall Docker Desktop or the Docker CLI, then retry."
        )
    if result.exit_code != 0:
        if f'context "{context_name}": context not found' in result.stderr:
            return None
        raise TransportError(
            f"ERROR: docker context inspect {context_name!r} failed\n"
            f"{result.stderr.strip()}\n"
            "Confirm docker is installed, DOCKER_CONFIG points at a readable context store, "
            "and the store is not corrupted, then retry."
        )
    try:
        endpoints = json.loads(result.stdout)
        host = endpoints["docker"]["Host"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise TransportError(
            f"ERROR: docker context {context_name!r} has an unparsable endpoint: "
            f"{result.stdout.strip()!r}\n"
            "Recreate the context with a valid tcp:// docker endpoint, then retry."
        ) from exc
    match = _TCP_ENDPOINT_PATTERN.match(host)
    if match is None:
        raise TransportError(
            f"ERROR: docker context {context_name!r} has a non-TCP endpoint: {host!r}\n"
            "This module only reuses a tcp:// endpoint's port; recreate the context with "
            "one, then retry."
        )
    return int(match.group("port"))


def _bind_free_port() -> int:
    """A local TCP port nothing is currently using, obtained by binding port zero.

    The socket is closed as soon as the OS-assigned port number is read, so
    the port is free again for `start_forward`'s session process to bind for
    real; an unavoidable, documented race exists between that close and the
    session process's own bind, the inherent limitation of "ask the OS for a
    free port" this module accepts rather than working around with a second
    mechanism.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe_socket:
        probe_socket.bind((_LOCALHOST, 0))
        return int(probe_socket.getsockname()[1])


def _ensure_port_free(port: int, context_name: str) -> None:
    """Raise `PortOccupiedError` if `port` is currently bound by another process.

    A bind-and-release probe, not a connect probe: a port with nothing
    listening on it would report "closed" to a connect attempt just as a
    genuinely free port would, so only a bind attempt actually distinguishes
    "free" from "occupied by a foreign listener" here. `SO_REUSEADDR` is set
    on the probe socket so a `TIME_WAIT` connection left behind by this
    module's *own* previous forward through this exact port (this module
    always tears the session down through `stop_forward`, which closes the
    connection actively) does not masquerade as a foreign listener: without
    it, the OS refuses the bind for any socket still winding down in
    `TIME_WAIT` on this address, even though no process actually holds the
    port anymore. A genuinely occupied port (another process actively
    listening) still fails this bind either way, `SO_REUSEADDR` does not
    permit binding over a live listener.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe_socket:
            probe_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe_socket.bind((_LOCALHOST, port))
    except OSError as exc:
        raise PortOccupiedError(
            f"ERROR: local port {port} recorded for docker context {context_name!r} is "
            "already in use\n"
            "A foreign process is bound to this port. This module never silently moves a "
            "recorded port to a different value: doing so would strand every docker "
            "context and configuration that already recorded it.\n"
            f"Free port {port} (identify the process holding it, then stop it), then retry."
        ) from exc


def allocate_local_port(runner: CommandRunner, context_name: str) -> int:
    """The local port to bind the SSM forward's local end to, for `context_name`.

    Reuses the port already recorded in `context_name`'s docker context
    endpoint when one exists, so the context stays stable across reconnects
    (spec Section 9); otherwise obtains a free port by binding port zero.
    Two different contexts allocated in sequence never collide: each reuses
    its own distinct recorded port, or binds its own distinct free one.
    """
    existing = _existing_context_port(runner, context_name)
    if existing is None:
        return _bind_free_port()
    _ensure_port_free(existing, context_name)
    return existing


# ---------------------------------------------------------------------------
# Readiness (spec Section 7.2, 7.3): a real event, then a connection --
# never time.
# ---------------------------------------------------------------------------

# A readiness probe attempt: given the time remaining until the overall
# deadline (used to bound this one attempt's own blocking call), report
# whether the forward is ready. `wait_ready` calls this only once it has
# already observed the session process announce its port opened, never in
# a tight retry loop.
ReadinessProbe = Callable[[float], bool]


def tcp_ready_probe(port: int) -> ReadinessProbe:
    """The production `ReadinessProbe`: a real, bounded TCP connect to `127.0.0.1:port`.

    Connection refused (nothing listening yet) and any other `OSError`
    (including the connect timing out) both mean "not ready yet"; only a
    successful connect means the forward answered. The connection is closed
    immediately: this probe exists to detect readiness, not to hold the
    forward open.
    """

    def probe(timeout_seconds: float) -> bool:
        try:
            with socket.create_connection((_LOCALHOST, port), timeout=timeout_seconds):
                return True
        except OSError:
            return False

    return probe


def _readline_with_deadline(stream: IO[bytes], deadline: float) -> bytes | None:
    """Block until `stream` has a line ready, hits EOF, or `deadline` (monotonic) passes.

    Waits on `stream`'s own file descriptor becoming readable through
    `selectors.DefaultSelector` -- a real I/O-readiness event, the same
    primitive an event-driven server's own accept loop rests on -- so this
    never busy-spins waiting for the session process to say something and
    never sleeps for a fixed duration: `deadline` bounds how long it is
    willing to wait for that event, it does not pace anything on its own.
    Returns the raw line read (bytes, with its trailing newline), an empty
    `bytes` object on EOF (the session process closed `stdout`), or `None`
    if `deadline` passed with nothing to read.
    """
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return None
    selector = selectors.DefaultSelector()
    try:
        selector.register(stream, selectors.EVENT_READ)
        if not selector.select(timeout=remaining):
            return None
    finally:
        selector.close()
    return stream.readline()


def wait_ready(
    probe: ReadinessProbe, session_output: IO[bytes], *, instance_id: str, port: int
) -> None:
    """Confirm the forward is ready, bounded by `SSM_FORWARD_TIMEOUT`.

    `SSM_FORWARD_TIMEOUT` is read exactly once, at the start of this call, so
    a case that shortens it (via the environment) observes its own value
    (AC-FUNC-006). This blocks on `session_output` (the session process's
    `stdout`) for its `_PORT_OPENED_MARKER` announcement -- a real, external
    event, never a busy spin or a fixed delay -- and only once that
    announcement arrives does it call `probe`, bounded by the time
    remaining until the deadline, to confirm the port actually accepts a
    connection. A line that is not the announcement (or the process's other,
    unrelated startup output) is read and ignored, then waited on again. If
    the process closes `stdout` (EOF) before announcing, or the deadline
    passes with nothing more to read -- including immediately after an
    announcement whose confirming `probe` call did not succeed, since the
    plugin announces only once -- this raises `ForwardTimeoutError` naming
    `instance_id`, `port`, the environment variable and its resolved value.
    Readiness is never reported because the announcement arrived or the
    session process is merely still alive; only `probe` answering `True`
    does that.
    """
    timeout = _forward_timeout_seconds()
    deadline = time.monotonic() + timeout
    while True:
        line = _readline_with_deadline(session_output, deadline)
        if not line:
            break
        if _PORT_OPENED_MARKER in line:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if probe(remaining):
                return
    raise ForwardTimeoutError(
        f"ERROR: SSM port forward to instance {instance_id!r} did not become ready\n"
        f"Local port {port} did not accept a connection within "
        f"{SSM_FORWARD_TIMEOUT_ENV_VAR}={timeout:g}s.\n"
        "Confirm the forward's session process is still alive and reporting a port opened for "
        "its session, then retry."
    )


# ---------------------------------------------------------------------------
# The session itself: no SSH anywhere (AC-10.3).
# ---------------------------------------------------------------------------


def build_start_session_argv(
    *, instance_id: str, local_port: int, profile: str, region: str
) -> list[str]:
    """The `aws ssm start-session` argument vector for one port forward.

    Every element is a separate list entry, never a shell string (spec
    Section 3.4); `DOCKER_TLS_PORT` is read exactly once, here, and appears
    only inside the `--parameters` value, never as a second literal
    elsewhere in this module (AC-FUNC-006). No element of the returned list
    is `ssh`, and the document named is `AWS-StartPortForwardingSession`,
    never `AWS-StartSSHSession` (AC-10.3, AC-FUNC-001): this function is
    what `tests/test_transport.py` inspects directly to prove it, rather
    than trusting this docstring's own claim.
    """
    remote_port = _docker_tls_port()
    return [
        "aws",
        "ssm",
        "start-session",
        "--target",
        instance_id,
        "--document-name",
        PORT_FORWARDING_DOCUMENT,
        "--parameters",
        f"portNumber={remote_port},localPortNumber={local_port}",
        "--profile",
        profile,
        "--region",
        region,
    ]


def start_forward(
    launcher: ProcessLauncher, *, instance_id: str, local_port: int, profile: str, region: str
) -> subprocess.Popen[bytes]:
    """Launch the `aws ssm start-session` process for one port forward and return its handle.

    Building the argument vector (`build_start_session_argv`) is separated
    from launching it so a test can assert on the exact vector without ever
    spawning a real process: this function's own test drives it with a fake
    `launcher` that records the vector it received.
    """
    argv = build_start_session_argv(
        instance_id=instance_id, local_port=local_port, profile=profile, region=region
    )
    return launcher(argv)


def stop_forward(process: subprocess.Popen[bytes]) -> None:
    """Terminate `process` and wait for it to exit, escalating to a kill if it does not.

    `SSM_FORWARD_TIMEOUT` bounds the grace period given to a clean shutdown
    (the same single-read function every other timeout consumer in this
    module calls); a process that does not exit within it is killed
    outright rather than left to leak past this call's return.
    """
    process.terminate()
    try:
        process.wait(timeout=_forward_timeout_seconds())
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


# ---------------------------------------------------------------------------
# CLI entry point.
# ---------------------------------------------------------------------------


def _drain_session_output(session_output: IO[bytes]) -> None:
    """Echo `session_output` to this process's own stdout until EOF.

    `subprocess.Popen.wait()` alone deadlocks once a `stdout=PIPE` child
    fills the OS pipe buffer with output nobody reads: the child blocks
    inside its own `write()` call forever, so it never exits, so `wait()`
    never returns -- a real, reachable failure mode here, not a theoretical
    one, since `session-manager-plugin` prints a line per connection an
    `AWS-StartPortForwardingSession` forward accepts, and this forward
    carries one docker API call per accepted connection. Draining
    `session_output` in a loop for as long as the session process keeps
    writing to it (past `wait_ready`'s single readiness announcement) keeps
    the pipe empty so the session process is never blocked writing to it;
    the loop ends only at EOF (the session process closed `stdout`, meaning
    it exited) or when a `KeyboardInterrupt` propagates out of the blocking
    read, which `_run_start` catches around this call exactly as it did
    around the bare `process.wait()` this replaces.
    """
    for line in session_output:
        sys.stdout.buffer.write(line)
        sys.stdout.buffer.flush()


def _run_start(args: argparse.Namespace) -> int:
    ensure_agent_online(
        subprocess_command_runner,
        instance_id=args.instance_id,
        profile=args.profile,
        region=args.region,
    )
    local_port = allocate_local_port(subprocess_command_runner, args.context)
    process = start_forward(
        default_process_launcher,
        instance_id=args.instance_id,
        local_port=local_port,
        profile=args.profile,
        region=args.region,
    )
    if process.stdout is None:
        raise TransportError(
            "ERROR: the session process was launched without a readable stdout pipe\n"
            "This is a bug in default_process_launcher, which must always pipe stdout so "
            "wait_ready can observe the session-manager-plugin's readiness announcement."
        )
    session_output: IO[bytes] = process.stdout
    try:
        wait_ready(
            tcp_ready_probe(local_port),
            session_output,
            instance_id=args.instance_id,
            port=local_port,
        )
    except ForwardTimeoutError:
        stop_forward(process)
        raise
    except KeyboardInterrupt:
        # An operator interrupting the wait itself (before the forward is even
        # confirmed ready) gets the same treatment as an interrupt during the
        # drain loop below: a clean teardown and exit 0, never a launched `aws`
        # child left running because the interrupt propagated past this point
        # without ever reaching `stop_forward`.
        stop_forward(process)
        return 0
    print(
        f"Port forward ready: 127.0.0.1:{local_port} -> {args.instance_id} over "
        f"{PORT_FORWARDING_DOCUMENT}."
    )
    try:
        _drain_session_output(session_output)
        process.wait()
    except KeyboardInterrupt:
        pass
    finally:
        stop_forward(process)
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="devcontainer_config.transport",
        description=(
            "SSM port forward manager: the transport that carries the docker API "
            "(spec Section 4.5)."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    start_parser = subparsers.add_parser(
        "start", help="Establish the SSM port forward and block until interrupted."
    )
    start_parser.add_argument("--instance-id", required=True, help="The EC2 instance id (i-...).")
    start_parser.add_argument(
        "--context", required=True, help="The docker context name (general-dev-<name>)."
    )
    start_parser.add_argument("--profile", required=True, help="The AWS SSO profile.")
    start_parser.add_argument("--region", required=True, help="The AWS region.")
    start_parser.set_defaults(handler=_run_start)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Parse `argv`, run the selected command, and return an exit code.

    Every handler raises a `TransportError` on a real failure instead of
    exiting itself; this is the one place that exception becomes a
    non-zero exit code, printed with an `ERROR:` prefix to stderr, never a
    stack trace.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except TransportError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
