"""SSH transport: a managed `ssh -N -L` tunnel carrying the docker API.

This module is the second transport `DEVCONTAINER_TRANSPORT` selects at
`make connect-start`, beside `devcontainer_config.transport`'s SSM port
forward: where that one reaches an EC2 instance's loopback daemon over
`aws ssm start-session`, this one reaches a generic SSH engine -- the kind
`remote-instances/<name>/engine.json` declares (see
`devcontainer_config.instances`) -- over a plain local forward. The forward
is `ssh -N -L`: `-N` executes no remote command, so the remote shell type is
irrelevant, and the tunnel rides the operator's own ssh configuration,
identity and agent untouched -- no key-file flag is ever built, and no SSH
configuration of any kind is read or written by this module. The destination
after the local port differs by platform: a Linux endpoint typically serves
an absolute Unix socket path (or a tcp address), while a Windows endpoint
REQUIRES a tcp destination -- Windows OpenSSH cannot forward to named pipes
or Unix sockets -- normally Docker Desktop's `tcp://localhost:2375` ("Expose
daemon on tcp://localhost:2375 without TLS"). `diagnose_forward_not_opened`
states that guidance explicitly when a forward whose destination is a Unix
socket path never opens, and `cli.py`'s engine-init `--docker-addr` help
states it at scaffold time, so the platform rule is visible both when the
manifest is written and when the forward fails.

`connect` mirrors the ssm transport's connect flow with a plain docker
context in place of the mTLS one: allocate (or reuse) the local port through
`transport.allocate_local_port` -- the same record-is-the-context rule, so
the context stays stable across reconnects -- launch the forward through the
same `ProcessLauncher` seam the ssm module uses
(`transport.default_process_launcher`), wait until the local TCP port
actually accepts a connection (`transport.tcp_ready_probe`, the same
readiness-decided-by-a-connection-attempt contract), create or update the
docker context named `--context` with a PLAIN endpoint
`tcp://127.0.0.1:<port>` -- no `ca`, `cert` or `key`: the ssh engine names
no certificate material -- activate it with `docker context use`, and
confirm the whole path with a `docker version` handshake whose API floor and
rootless-probe semantics are identical to `transport.handshake`'s. It then
prints a `Connected:` line shaped like the ssm transport's -- the readiness
marker `devcontainer_config.forwards` polls for, so the daemon lifecycle
works unchanged -- and runs until interrupted. The two interrupt paths are
deliberately different, and the module traps exactly one of them: SIGINT is
handled in this process, where the interrupted wait tears the ssh child down
cleanly; SIGTERM is never trapped here, and reaches the ssh child through
the daemon lifecycle's process-group signal (`forwards.stop_forward` signals
the daemon's whole group, SIGINT first, then SIGTERM, then SIGKILL), so the
child dies with the daemon without this module carrying a second signal
handler. Every failure path after the launch stops the forward before the
error propagates, never orphaning a child holding the allocated port.

Nothing here waits on time. The module contains no `time.sleep` call
anywhere, the same rule `transport.py`'s own suite enforces: readiness is
the port accepting a connection, bounded by `SSH_FORWARD_TIMEOUT` (default
30 seconds, read fresh through `hostprobe.read_positive_seconds`, the one
shared reader of that variable shape), and the wait between probe attempts
blocks on the forward process's own stdout through `selectors` -- a real
I/O-readiness wait, the same primitive `transport.wait_ready` rests on. An
`ssh -N` forward announces nothing on stdout, so the stream is silent in
normal operation and the bounded wait simply paces the probes; the moment
the stream has data or hits EOF (the ssh process exited, its inherited
stderr having already reached the operator) the wait reacts at once instead
of waiting out the deadline. The retry loop of the handshake is paced the
same way `transport.handshake`'s is: every attempt shells out to a real
`docker` CLI process, whose own fork/exec and dial cost paces the loop
without this module adding a fixed delay, and the deadline check bounds the
total regardless.

Every docker invocation is issued through an injected
`devcontainer_config.hostprobe.CommandRunner`, the same seam the ssm
transport injects; `transport.subprocess_command_runner` is this module's
production runner and `transport.default_process_launcher` its production
launcher, so neither module declares a second, independent copy of the
subprocess-backed seams. The manifest values this module forwards -- host,
user, port, docker address -- are validated through
`instances.validate_engine_values`, the same single validation surface the
manifest's read and write sides use, so a value the manifest would refuse
never reaches an ssh argument vector at all. The handshake's failure
diagnoses are this module's own, ssh-worded: a context or handshake failure
names the context and the underlying docker stderr (never the SSM forward
the ssm transport's diagnosis would name), and an API version below the
shared floor is its own distinct failure.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import selectors
import subprocess
import sys
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import IO, NoReturn, cast

from devcontainer_config import instances, transport
from devcontainer_config.hostprobe import (
    DOCKER_HANDSHAKE_TIMEOUT_DEFAULT_SECONDS,
    DOCKER_HANDSHAKE_TIMEOUT_ENV_VAR,
    CommandRunner,
    HostProbeError,
    docker_context_endpoint_host,
    read_positive_seconds,
)

SSH_FORWARD_TIMEOUT_ENV_VAR = "SSH_FORWARD_TIMEOUT"
_SSH_FORWARD_TIMEOUT_DEFAULT_SECONDS = 30.0

_LOCALHOST = "127.0.0.1"
_SSH_BINARY = "ssh"

# The pace of the readiness probes against the local port. The total wait is
# bounded by `SSH_FORWARD_TIMEOUT`; between probes this module blocks on the
# forward process's stdout through `selectors` for at most this many seconds
# -- a bounded I/O-readiness wait, never a sleep -- so a forward that opens
# is noticed within one wait, and a child that dies or writes is noticed
# immediately. The same cadence shape `devcontainer_config.forwards` uses
# for its own log poll.
_FORWARD_POLL_SECONDS = 0.5

# The `tcp://` scheme a manifest `docker_addr` may carry instead of an
# absolute Unix socket path; the same wire-format literal
# `instances.validate_engine_values` validates, restated here because this
# module strips it rather than importing the private constant.
_TCP_ADDR_PREFIX = "tcp://"

# The `docker info --format {{json .SecurityOptions}}` entry a rootless
# daemon reports. An independent copy of the ssm transport's own constant,
# by the same design that keeps each transport's diagnoses module-local
# (transport.py's `_EXPIRED_CREDENTIAL_MARKERS` documents the precedent).
_ROOTLESS_SECURITY_OPTION = "name=rootless"


class SSHTransportError(transport.TransportError):
    """Base for every operational failure this module raises.

    Every failure this module can produce is an `SSHTransportError` (or a
    more specific subclass below), and every one of those is also a
    `transport.TransportError`, so a caller that handles the transports
    uniformly can catch the ssm module's base class across both.
    """


class SSHForwardError(SSHTransportError):
    """The ssh forward never opened, or died before the docker path was ready."""


class SSHForwardTimeoutError(SSHForwardError):
    """The local port did not accept a connection within `SSH_FORWARD_TIMEOUT`."""


class DockerVersionFloorError(SSHTransportError):
    """The daemon behind the ssh-forwarded context reports an API version below the floor."""


class DockerHandshakeTimeoutError(SSHTransportError):
    """The docker version call behind the ssh-forwarded context never answered in time."""


# ---------------------------------------------------------------------------
# Manifest values: the single validation surface, and the strings composed
# from validated values.
# ---------------------------------------------------------------------------


def validated_engine_values(
    *, host: str, user: str, port: int, docker_addr: str
) -> dict[str, object]:
    """The five manifest fields as `instances.validate_engine_values` accepts them.

    The one validation seam this module uses: every entry path (`connect`'s
    CLI arguments, `build_forward_argv`) funnels the caller-supplied values
    through it, so a value the manifest would refuse is refused here, before
    any ssh or docker argument vector is built. `instances.EngineManifestError`
    is wrapped as this module's own error so no exception type foreign to
    this module crosses its boundary.

    Raises:
        SSHTransportError: any field breaks its manifest rule (the message
            carries `instances`' own field-and-rule refusal verbatim).
    """
    try:
        return instances.validate_engine_values(
            {
                "kind": instances.ENGINE_KIND_SSH,
                "host": host,
                "user": user,
                "port": port,
                "docker_addr": docker_addr,
            }
        )
    except instances.EngineManifestError as exc:
        raise SSHTransportError(f"ERROR: invalid SSH engine values\n{exc}") from exc


def engine_endpoint(validated: Mapping[str, object]) -> str:
    """`ssh://<user>@<host>:<port>`, composed from already-validated values.

    The same composition `instances.engine_endpoint` performs from a
    manifest on disk; this module's values arrive validated from
    `validated_engine_values` rather than from a manifest read, so the
    composition is restated here rather than reaching for a repository root
    this module deliberately never touches.
    """
    return f"ssh://{validated['user']}@{validated['host']}:{validated['port']}"


# ---------------------------------------------------------------------------
# Argument vectors: the forward, and the plain docker context.
# ---------------------------------------------------------------------------


def _forward_destination(validated: Mapping[str, object]) -> str:
    """The `-L` destination: the bare socket path, or the tcp address's host:port.

    An absolute Unix socket path passes through unchanged; a `tcp://` value
    is reduced to its `<host>:<port>` remainder. The tcp shape is guaranteed
    by `validated_engine_values`, which every entry path runs before this.
    """
    docker_addr = str(validated["docker_addr"])
    if not docker_addr.startswith(_TCP_ADDR_PREFIX):
        return docker_addr
    return docker_addr[len(_TCP_ADDR_PREFIX) :]


def _forward_argv_for(validated: Mapping[str, object], local_port: int) -> list[str]:
    """The `ssh -N -L` argument vector from already-validated values.

    `BatchMode=yes` fails fast instead of prompting (a daemon has no tty to
    answer one), and `ExitOnForwardFailure=yes` makes ssh exit non-zero when
    the forward itself cannot be established rather than sitting there
    looking alive. `-p` appears only when the port differs from the default,
    and the destination `<user>@<host>` is the final element -- the operator's
    default identity, agent and configuration decide everything this vector
    deliberately does not mention.
    """
    argv = [
        _SSH_BINARY,
        "-N",
        "-o",
        "BatchMode=yes",
        "-o",
        "ExitOnForwardFailure=yes",
    ]
    # `validated` comes from `instances.validate_engine_values`, which
    # guarantees an `int` port; the cast records that guarantee for mypy.
    port_value = cast(int, validated["port"])
    if port_value != instances.DEFAULT_ENGINE_PORT:
        argv.extend(["-p", str(port_value)])
    argv.extend(["-L", f"{_LOCALHOST}:{local_port}:{_forward_destination(validated)}"])
    argv.append(f"{validated['user']}@{validated['host']}")
    return argv


def build_forward_argv(
    *, host: str, port: int, user: str, docker_addr: str, local_port: int
) -> list[str]:
    """The `ssh -N -L` argument vector for one forward, values validated first.

    Every element is a separate list entry, never a shell string. No
    key-file flag and no remote command is ever built: `-N` opens no
    channel but the forward, and the identity is the operator's own. A
    `docker_addr` starting with '/' forwards to the bare Unix socket path;
    a `tcp://<host>:<port>` value forwards to `<host>:<port>`.

    Raises:
        SSHTransportError: any value `instances.validate_engine_values`
            would refuse.
    """
    validated = validated_engine_values(host=host, user=user, port=port, docker_addr=docker_addr)
    return _forward_argv_for(validated, local_port)


def build_context_argv(*, context_name: str, port: int, update: bool) -> list[str]:
    """The `docker context create`/`update` vector for the plain endpoint.

    The endpoint carries `host=` alone: an ssh engine names no certificate
    material, so unlike the ssm transport's context there is no `ca`, `cert`
    or `key` component to compose.
    """
    return [
        "docker",
        "context",
        "update" if update else "create",
        context_name,
        "--docker",
        f"host=tcp://{_LOCALHOST}:{port}",
    ]


# ---------------------------------------------------------------------------
# Failure diagnosis: three distinct texts, per the Error Handling Contract.
# ---------------------------------------------------------------------------


def _windows_unix_socket_guidance(docker_addr: str) -> str:
    """The platform guidance for a Unix socket destination, or '' for a tcp one.

    A Windows ssh endpoint cannot forward to a Unix socket (or a named
    pipe): Windows OpenSSH supports neither as a forward destination. A
    forward whose destination is a socket path and that runs against a
    Windows endpoint therefore cannot open, and the failure must say what
    to do about it rather than leaving the operator with a bare timeout.
    """
    if docker_addr.startswith(_TCP_ADDR_PREFIX):
        return ""
    return (
        "The manifest's docker_addr is a Unix socket path, and a Windows SSH "
        "endpoint cannot forward to one: Windows OpenSSH supports neither "
        "named pipes nor Unix sockets as forward destinations.\n"
        "On a Windows endpoint, enable Docker Desktop's \"Expose daemon on "
        'tcp://localhost:2375 without TLS" and set docker_addr to '
        "tcp://localhost:2375 in the engine manifest.\n"
    )


def diagnose_forward_not_opened(
    *, instance: str, endpoint: str, docker_addr: str, port: int, summary: str
) -> str:
    """The forward-never-opened diagnosis: the endpoint, the port, the platform rule.

    `summary` states the specific fact the caller observed (the deadline
    elapsed, or the ssh process exited); everything else -- the instance,
    the endpoint, the local port and, for a socket-path destination, the
    Windows guidance -- is stated here, once.
    """
    return (
        f"ERROR: the ssh forward for instance {instance!r} ({endpoint}) did not open\n"
        f"Local port {_LOCALHOST}:{port} {summary}.\n"
        f"{_windows_unix_socket_guidance(docker_addr)}"
        "Confirm the endpoint is reachable with plain ssh and that the docker "
        "destination named by docker_addr exists on it, then retry."
    )


def _diagnose_handshake_failure(
    stderr: str, *, context_name: str, failure_summary: str = "failed"
) -> str:
    """The context/handshake diagnosis: the context, the docker stderr, the tunnel.

    Distinct from the forward diagnosis by design: a daemon that does not
    answer behind an open forward is a docker-destination problem, not a
    tunnel problem, and the remedy names the ssh forward only as the thing
    to confirm -- never the ssm forward `transport.diagnose_handshake_failure`
    speaks of, and never the certificate reissue that transport's
    certificate-bearing context would suggest.
    """
    return (
        f"ERROR: docker version handshake for context {context_name!r} {failure_summary}\n"
        + (f"{stderr.strip()}\n" if stderr.strip() else "")
        + "Confirm the ssh forward is still established and the daemon behind the "
        "docker_addr is running, then retry."
    )


# ---------------------------------------------------------------------------
# Timeouts: read fresh through the shared hostprobe reader, never hard-coded.
# ---------------------------------------------------------------------------


def _positive_seconds_from_env(env_var: str, default_seconds: float) -> float:
    """Read `env_var` via `hostprobe.read_positive_seconds`, wrapping failure.

    The same shape `transport._positive_seconds_from_env` establishes: the
    shared reader owns the parse and the validation, and this module wraps
    the rejection as its own error so `main`'s single `except` clause holds.
    """
    try:
        return read_positive_seconds(env_var, default_seconds)
    except HostProbeError as exc:
        raise SSHTransportError(
            f"ERROR: {exc}\n"
            "Set it to a positive number of seconds, or unset it to use the default of "
            f"{default_seconds:g}."
        ) from exc


def _forward_timeout_seconds() -> float:
    """`SSH_FORWARD_TIMEOUT`, read fresh, exactly once per readiness wait."""
    return _positive_seconds_from_env(
        SSH_FORWARD_TIMEOUT_ENV_VAR, _SSH_FORWARD_TIMEOUT_DEFAULT_SECONDS
    )


def _handshake_timeout_seconds() -> float:
    """`DOCKER_HANDSHAKE_TIMEOUT`, read through the same shared reader the ssm transport uses.

    The variable's name, default and validation live in `hostprobe`; this
    module reads it the way `transport._handshake_timeout_seconds` does
    rather than declaring a second copy of any of the three.
    """
    return _positive_seconds_from_env(
        DOCKER_HANDSHAKE_TIMEOUT_ENV_VAR, DOCKER_HANDSHAKE_TIMEOUT_DEFAULT_SECONDS
    )


# ---------------------------------------------------------------------------
# Readiness: the port accepting a connection, paced by a real I/O wait.
# ---------------------------------------------------------------------------


def _readline_with_deadline(stream: IO[bytes], budget_seconds: float) -> bytes | None:
    """Block until `stream` has a line ready, hits EOF, or `budget_seconds` passes.

    The same bounded I/O-readiness wait `transport._readline_with_deadline`
    performs: `selectors.DefaultSelector` blocks on the stream's own file
    descriptor becoming readable, so waiting costs nothing until an event
    arrives and never spins. Returns the raw line (bytes, newline
    included), an empty `bytes` object on EOF (the forward process closed
    stdout), or `None` if the budget passed with nothing to read.
    """
    if budget_seconds <= 0:
        return None
    selector = selectors.DefaultSelector()
    try:
        selector.register(stream, selectors.EVENT_READ)
        if not selector.select(timeout=budget_seconds):
            return None
    finally:
        selector.close()
    return stream.readline()


def wait_ready(
    probe: transport.ReadinessProbe,
    forward_output: IO[bytes],
    *,
    instance: str,
    endpoint: str,
    docker_addr: str,
    port: int,
) -> None:
    """Confirm the forward is ready, bounded by `SSH_FORWARD_TIMEOUT`.

    Readiness is decided exactly as the ssm transport decides it: only a
    successful connection attempt (`probe`) counts, never the process being
    alive. Between attempts this blocks on `forward_output` -- the ssh
    process's stdout -- for at most the smaller of the remaining deadline
    and one poll cadence; `ssh -N` writes nothing there in normal operation,
    so the wait simply spaces the probes, while data (an abnormal stdout) or
    EOF (the ssh process exited, its inherited stderr having already shown
    the operator why) is reacted to immediately instead of waiting out the
    deadline. A line that is neither is read and ignored, the same
    discipline `transport.wait_ready` applies to its own process's startup
    output.

    Raises:
        SSHForwardTimeoutError: the port never accepted a connection within
            the deadline.
        SSHForwardError: the forward process exited before the port
            accepted a connection.
        SSHTransportError: `SSH_FORWARD_TIMEOUT` is set to a value
            `hostprobe.read_positive_seconds` refuses.
    """
    timeout = _forward_timeout_seconds()
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SSHForwardTimeoutError(
                diagnose_forward_not_opened(
                    instance=instance,
                    endpoint=endpoint,
                    docker_addr=docker_addr,
                    port=port,
                    summary=(
                        f"did not accept a connection within "
                        f"{SSH_FORWARD_TIMEOUT_ENV_VAR}={timeout:g}s"
                    ),
                )
            )
        line = _readline_with_deadline(forward_output, min(remaining, _FORWARD_POLL_SECONDS))
        if line is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                continue  # the loop head raises the timeout diagnosis
            if probe(remaining):
                return
            continue
        if line == b"":
            raise SSHForwardError(
                diagnose_forward_not_opened(
                    instance=instance,
                    endpoint=endpoint,
                    docker_addr=docker_addr,
                    port=port,
                    summary=(
                        "the ssh process exited before it accepted a connection "
                        "(its own diagnostics are above)"
                    ),
                )
            )


# ---------------------------------------------------------------------------
# The docker context, its activation, and the handshake behind it.
# ---------------------------------------------------------------------------


def _existing_endpoint_host(runner: CommandRunner, context_name: str) -> str | None:
    """The endpoint host `context_name` is recorded with, or `None` if absent.

    Delegates to `hostprobe.docker_context_endpoint_host`, the single
    `docker context inspect` reader the ssm transport also drives.
    `hostprobe.HostProbeError` is re-raised as this module's own error so
    no exception type foreign to this module crosses its boundary.
    """
    try:
        return docker_context_endpoint_host(runner, context_name)
    except HostProbeError as exc:
        raise SSHTransportError(f"ERROR: {exc}") from exc


def ensure_context(runner: CommandRunner, *, context_name: str, port: int) -> str:
    """Create or update `context_name` to address `tcp://127.0.0.1:<port>`.

    An absent context is created; an existing context is updated in place,
    whatever its current endpoint -- the context is this transport's record
    of the forward, so updating it is the record being kept current, not a
    mutation of someone else's state. Raises naming the context and the
    underlying docker stderr, never a bare exit code.
    """
    update = _existing_endpoint_host(runner, context_name) is not None
    argv = build_context_argv(context_name=context_name, port=port, update=update)
    result = runner(tuple(argv), None)
    if result.binary_missing:
        raise SSHTransportError(
            "ERROR: docker is not on PATH\nInstall Docker Desktop or the Docker CLI, then retry."
        )
    if result.exit_code != 0:
        raise SSHTransportError(
            f"ERROR: docker context {'update' if update else 'create'} failed for "
            f"context {context_name!r}\n"
            f"{result.stderr.strip()}\n"
            "Confirm docker is installed and working, then retry."
        )
    return context_name


def use_context(runner: CommandRunner, context_name: str) -> None:
    """Activate `context_name` as the docker CLI's active context.

    The daemon a subsequent `make build` targets is whichever context is
    active, so the context this module just created or updated is activated
    here rather than left to a caller who might not know it is required.

    Raises:
        SSHTransportError: docker is not on PATH, or the `context use`
            command itself failed. The context already exists at this
            point; only activating it failed.
    """
    result = runner(("docker", "context", "use", context_name), None)
    if result.binary_missing:
        raise SSHTransportError(
            "ERROR: docker is not on PATH\nInstall Docker Desktop or the Docker CLI, then retry."
        )
    if result.exit_code != 0:
        raise SSHTransportError(
            f"ERROR: docker context use {context_name!r} failed\n"
            f"{result.stderr.strip()}\n"
            f"The context was created or updated successfully; only activating it failed. "
            f"Run 'docker context use {context_name}' by hand, then retry."
        )


@dataclass(frozen=True)
class HandshakeResult:
    """The two daemon facts a completed handshake confirms."""

    server_version: str
    api_version: str
    rootless: bool


def _api_version_tuple(value: str) -> tuple[int, ...]:
    """The API version as a tuple of integers, so "1.9" < "1.44" compares numerically."""
    try:
        return tuple(int(part) for part in value.split("."))
    except ValueError as exc:
        raise SSHTransportError(
            f"ERROR: docker reported an unparsable API version {value!r}\n"
            "Confirm the daemon behind this context is a genuine docker engine, then retry."
        ) from exc


def _ensure_api_version_floor(observed_api_version: str, context_name: str) -> None:
    """Raise `DockerVersionFloorError` when the daemon's API version is below the floor.

    The floor is `transport.DOCKER_API_VERSION_FLOOR`, the same value the
    ssm transport enforces -- the floor belongs to the API this platform
    requires, not to any one transport.
    """
    floor = transport.DOCKER_API_VERSION_FLOOR
    if _api_version_tuple(observed_api_version) < _api_version_tuple(floor):
        raise DockerVersionFloorError(
            f"ERROR: docker API version {observed_api_version} for context "
            f"{context_name!r} is below the required floor of {floor}\n"
            f"The docker API this platform is built against requires at least "
            f"version {floor}.\n"
            "Upgrade the docker engine on the SSH endpoint, then retry."
        )


def _parse_server_payload(stdout: str, context_name: str) -> dict[str, str]:
    """The `docker version --format {{json .Server}}` payload's two fields."""
    try:
        payload = json.loads(stdout)
        return {"Version": str(payload["Version"]), "ApiVersion": str(payload["ApiVersion"])}
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise SSHTransportError(
            f"ERROR: docker version for context {context_name!r} produced unparsable "
            f"output: {stdout.strip()!r}\n"
            "Run the command manually and investigate why its output does not match the "
            "expected shape."
        ) from exc


def _probe_rootless(runner: CommandRunner, context_name: str, *, timeout_seconds: float) -> bool:
    """Whether the daemon behind `context_name` reports running rootless.

    Called only after the version call has already answered, so a failure
    here is its own distinct error rather than a readiness condition to
    retry: the daemon is already known reachable.
    """
    command = ("docker", "--context", context_name, "info", "--format", "{{json .SecurityOptions}}")
    result = runner(command, timeout_seconds)
    if result.binary_missing:
        raise SSHTransportError(
            "ERROR: docker is not on PATH\nInstall Docker Desktop or the Docker CLI, then retry."
        )
    if result.exit_code != 0:
        raise SSHTransportError(
            f"ERROR: docker info for context {context_name!r} failed after a successful "
            "version handshake\n"
            f"{result.stderr.strip()}\n"
            "Confirm the daemon behind this context is still reachable, then retry."
        )
    try:
        options = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise SSHTransportError(
            f"ERROR: docker info for context {context_name!r} produced unparsable "
            f"security options: {result.stdout.strip()!r}\n"
            "Run the command manually and investigate why its output does not match the "
            "expected shape."
        ) from exc
    return _ROOTLESS_SECURITY_OPTION in (options or [])


def _raise_handshake_timeout(last_stderr: str, *, context_name: str, timeout: float) -> NoReturn:
    """The one construction site for the handshake-deadline error."""
    raise DockerHandshakeTimeoutError(
        _diagnose_handshake_failure(
            last_stderr,
            context_name=context_name,
            failure_summary=(
                f"did not answer within {DOCKER_HANDSHAKE_TIMEOUT_ENV_VAR}={timeout:g}s"
            ),
        )
    )


def handshake(runner: CommandRunner, *, context_name: str) -> HandshakeResult:
    """Complete a docker version handshake against `context_name` (spec Section 4.2.1).

    The version call is retried until it answers, bounded overall by
    `DOCKER_HANDSHAKE_TIMEOUT` -- every attempt shells out to a real docker
    CLI process, whose own fork/exec and dial cost paces the loop without
    this module adding a fixed delay, and the deadline check bounds the
    total regardless, exactly as `transport.handshake` documents for its
    own loop. Once the version call answers, the API version is checked
    against the same `DOCKER_API_VERSION_FLOOR` the ssm transport enforces,
    and the rootless probe runs with whatever budget remains until the same
    deadline, never the full timeout again.

    Raises:
        DockerVersionFloorError: the daemon answered but its API version is
            below the floor.
        DockerHandshakeTimeoutError: the version call never answered within
            `DOCKER_HANDSHAKE_TIMEOUT`, or the deadline expired before the
            rootless probe could run.
        SSHTransportError: docker is not on PATH, or a response could not
            be parsed.
    """
    timeout = _handshake_timeout_seconds()
    deadline = time.monotonic() + timeout
    version_command = (
        "docker",
        "--context",
        context_name,
        "version",
        "--format",
        "{{json .Server}}",
    )
    last_stderr = ""
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _raise_handshake_timeout(last_stderr, context_name=context_name, timeout=timeout)
        result = runner(version_command, remaining)
        if result.binary_missing:
            raise SSHTransportError(
                "ERROR: docker is not on PATH\nInstall Docker Desktop or the Docker CLI, "
                "then retry."
            )
        if result.exit_code == 0:
            server = _parse_server_payload(result.stdout, context_name)
            _ensure_api_version_floor(server["ApiVersion"], context_name)
            rootless_budget = deadline - time.monotonic()
            if rootless_budget <= 0:
                _raise_handshake_timeout(last_stderr, context_name=context_name, timeout=timeout)
            rootless = _probe_rootless(runner, context_name, timeout_seconds=rootless_budget)
            return HandshakeResult(
                server_version=server["Version"],
                api_version=server["ApiVersion"],
                rootless=rootless,
            )
        last_stderr = result.stderr
    # Unreachable: the loop above returns or raises.


# ---------------------------------------------------------------------------
# The connect flow: launch, confirm, wire the context, hold until
# interrupted, and tear the forward down on every path out.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _EstablishedForward:
    """A ready ssh forward: the launched process, its readable stdout, and the port."""

    process: subprocess.Popen[bytes]
    forward_output: IO[bytes]
    local_port: int


def _establish_forward(
    *,
    validated: Mapping[str, object],
    context_name: str,
    instance: str,
    endpoint: str,
    docker_addr: str,
) -> _EstablishedForward | None:
    """Allocate the port, launch the ssh forward, and wait until the port answers.

    The port is allocated (or reused from the context's own record) through
    `transport.allocate_local_port` -- the same single allocation site the
    ssm transport uses -- and the forward argument vector is built from the
    already-validated values at that port.

    Returns `None` only when a `KeyboardInterrupt` arrived before the
    forward was confirmed ready, the forward having already been torn down:
    the caller treats that as the operator exiting cleanly.

    Raises:
        SSHForwardError: the forward process exited before the port
            accepted a connection, or never opened within the deadline --
            in both cases after being torn down.
        SSHTransportError: the launched process was not given a readable
            stdout pipe (a `transport.default_process_launcher` bug).
    """
    local_port = transport.allocate_local_port(transport.subprocess_command_runner, context_name)
    process = transport.default_process_launcher(_forward_argv_for(validated, local_port))
    if process.stdout is None:
        raise SSHTransportError(
            "ERROR: the forward process was launched without a readable stdout pipe\n"
            "This is a bug in transport.default_process_launcher, which must always pipe "
            "stdout so wait_ready can react to the child's exit."
        )
    forward_output: IO[bytes] = process.stdout
    try:
        wait_ready(
            transport.tcp_ready_probe(local_port),
            forward_output,
            instance=instance,
            endpoint=endpoint,
            docker_addr=docker_addr,
            port=local_port,
        )
    except SSHForwardError:
        # A forward that never opened is torn down before the error
        # propagates, the same discipline `_establish_forward` on the ssm
        # side applies to its own ForwardTimeoutError: a dead or timed-out
        # child must never be left holding the allocated loopback port.
        transport.stop_forward(process)
        raise
    except KeyboardInterrupt:
        transport.stop_forward(process)
        return None
    return _EstablishedForward(
        process=process, forward_output=forward_output, local_port=local_port
    )


@contextlib.contextmanager
def _teardown_forward_on_error(process: subprocess.Popen[bytes]) -> Iterator[None]:
    """Stop `process` if the wrapped block raises anything, then re-raise.

    The same discipline `transport._teardown_forward_on_error` enforces for
    the ssm forward: every failure after the launch -- a docker context that
    cannot be written, a handshake that cannot answer, an operator
    interrupt -- stops the already-launched ssh child rather than orphaning
    it holding the allocated loopback port. Catches `BaseException`, not
    `Exception`, so a `KeyboardInterrupt` raised anywhere in the wrapped
    block is covered too; the caught exception is always re-raised
    unchanged, never swallowed.
    """
    try:
        yield
    except BaseException:
        transport.stop_forward(process)
        raise


def _run_until_interrupted(process: subprocess.Popen[bytes]) -> None:
    """Hold the forward open until it exits or the operator interrupts, then stop it.

    Mirrors `transport._run_until_interrupted`: the child's stdout is
    drained (an `ssh -N` forward normally writes nothing, but a blocked
    pipe would eventually deadlock the child) for as long as the process
    keeps running, and the process is stopped on the way out regardless of
    which of the two ended the wait. The ssh child's stderr is inherited,
    so its own diagnostics reach the operator's terminal -- or the daemon
    log, under `make connect-start` -- directly.
    """
    try:
        if process.stdout is not None:
            for line in process.stdout:
                sys.stdout.buffer.write(line)
                sys.stdout.buffer.flush()
        process.wait()
    except KeyboardInterrupt:
        pass
    finally:
        transport.stop_forward(process)


def _run_connect(args: argparse.Namespace) -> int:
    """Handler for `connect`: the ssh build path's context-creation entry point.

    Validates the engine values, establishes the forward, creates or
    updates the plain `tcp://` context at the port just allocated, activates
    it, confirms it with the version handshake, prints the `Connected:`
    line `devcontainer_config.forwards` polls for, and holds the forward
    open until interrupted. Every failure after the launch runs inside
    `_teardown_forward_on_error` so the ssh child is stopped before the
    error propagates.
    """
    try:
        instances.validate_name(args.name)
    except instances.InvalidInstanceNameError as exc:
        raise SSHTransportError(f"ERROR: {exc}") from exc
    validated = validated_engine_values(
        host=args.host, user=args.user, port=args.port, docker_addr=args.docker_addr
    )
    endpoint = engine_endpoint(validated)
    established = _establish_forward(
        validated=validated,
        context_name=args.context,
        instance=args.name,
        endpoint=endpoint,
        docker_addr=args.docker_addr,
    )
    if established is None:
        return 0
    runner = transport.subprocess_command_runner
    with _teardown_forward_on_error(established.process):
        ensure_context(runner, context_name=args.context, port=established.local_port)
        use_context(runner, args.context)
        result = handshake(runner, context_name=args.context)
    rootless_note = ", rootless" if result.rootless else ""
    print(
        f"Connected: docker context {args.context!r} -> {endpoint} over ssh forward "
        f"(server {result.server_version}, api {result.api_version}{rootless_note})."
    )
    _run_until_interrupted(established.process)
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="devcontainer_config.transport_ssh",
        description=(
            "SSH transport: a managed 'ssh -N -L' forward to an engine declared by "
            "remote-instances/<name>/engine.json."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    connect_parser = subparsers.add_parser(
        "connect",
        help=(
            "Establish the ssh forward and create/update the plain tcp:// docker "
            "context behind it, then hold until interrupted."
        ),
    )
    connect_parser.add_argument(
        "--name", required=True, help="The instance name (the remote-instances/<name> directory)."
    )
    connect_parser.add_argument("--host", required=True, help="The engine's SSH host.")
    connect_parser.add_argument("--port", type=int, required=True, help="The engine's SSH port.")
    connect_parser.add_argument("--user", required=True, help="The user the engine is reached as.")
    connect_parser.add_argument(
        "--docker-addr",
        required=True,
        help=(
            "The docker destination: an absolute Unix socket path, or "
            "'tcp://<host>:<port>' (the only form a Windows endpoint can forward to)."
        ),
    )
    connect_parser.add_argument("--context", required=True, help="The docker context name.")
    connect_parser.set_defaults(handler=_run_connect)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Parse `argv`, run the selected command, and return an exit code.

    Every handler raises an `SSHTransportError` on a real failure instead of
    exiting itself; this is the one place that exception becomes a non-zero
    exit code, printed with an `ERROR:` prefix to stderr, never a stack
    trace. `SSHTransportError` subclasses `transport.TransportError`, so the
    ssm module's base class covers this module's failures too.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except transport.TransportError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
