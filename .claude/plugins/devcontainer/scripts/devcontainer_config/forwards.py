"""Detached forward lifecycle behind the make connect-* targets.

`make connect-start` opens an engine's forward as a detached daemon
and returns as soon as the connection is confirmed, instead of holding the
terminal: this module spawns the transport's `connect` command in a
new session (its stdout and stderr stream into a per-instance log), waits
by polling for the transport's own "Connected:" announcement, which is
printed only after the docker context was created or updated and answered a
handshake through the tunnel, and records the daemon's pid, command,
log path and forwarded port in a per-instance JSON record. The record lives
beside the instance's certificate material (`instances.certs_dir`), so
`make instance-destroy`'s cleanup removes it with everything else the
instance scattered.

The transport is chosen by kind, and the record remembers which: the `ssm`
kind (the default) spawns `devcontainer_config.transport connect` against
an EC2 instance id, and the `ssh` kind spawns
`devcontainer_config.transport_ssh connect` against an SSH engine's
manifest values (`instances.engine_manifest`). Both print the same
readiness marker, so the open path cannot tell the difference and does not
need to; `ForwardRecord.engine_kind` and the record's engine identifier
(the EC2 id, or the `ssh://` endpoint) carry the distinction for status,
stop and refresh.

`make connect-start` is the open operation. The rest of the lifecycle
reads and mutates that record: `status` reports
process aliveness and whether the forwarded port is listening (exit
non-zero when the forward is down), `stop` terminates the daemon with
SIGINT first -- the transport tears its `aws ssm start-session` child down
cleanly on the interrupt, which a SIGTERM would bypass -- verifies the port
actually closed, and removes the record; `refresh` stops and re-opens from
the record's own stored command; `list` renders one row per configured
instance.

Readiness and shutdown are poll-based, never a fixed wait: the caller
injects the clock and the limits, and the timeouts are configurable through
`FORWARD_OPEN_TIMEOUT_SECONDS` and `FORWARD_STOP_TIMEOUT_SECONDS` (read in
exactly one place, through `hostprobe.read_positive_seconds`). Every
unexpected answer -- a child that dies before announcing readiness, a port
that never closes, an unreadable record -- is raised with the log tail or
the probe's own output, so nothing is hidden and nothing is retried behind
a failure.
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from devcontainer_config import instances, repo, transport, transport_ssh
from devcontainer_config.hostprobe import CommandResult, CommandRunner, read_positive_seconds

# The transport commands this module spawns and manages. The child composes
# its own aws/docker calls; this module only decides when and whether it ran.
_TRANSPORT_MODULE = "devcontainer_config.transport"
_TRANSPORT_SSH_MODULE = "devcontainer_config.transport_ssh"
_TRANSPORT_COMMAND = "connect"

# The ssm transport kind, the default, and what every record written
# before the field existed reads back as. The ssh kind is
# `instances.ENGINE_KIND_SSH` (the same "ssh" string the engine manifest and
# the resolved address block use).
KIND_SSM = "ssm"

# The line the transport prints only after the docker context answered a
# TLS handshake through the freshly opened tunnel: the readiness marker
# the open poll waits for.
READINESS_MARKER = "Connected:"

# Per-instance state, beside the certificate material the instance already
# owns (spec Section 5.5's per-instance state dir): one JSON record and the
# daemon's own log, both removed by `make instance-destroy`'s cleanup with
# the rest of the directory.
FORWARD_RECORD_FILENAME = "forward.json"
FORWARD_LOG_FILENAME = "forward.log"

# The open poll cadence and its bounds: at most `_OPEN_POLL_LIMIT_DEFAULT`
# polls, `OPEN_POLL_SECONDS` apart, before an unannounced child is stopped
# and reported. `FORWARD_OPEN_TIMEOUT_SECONDS` (read through
# `read_positive_seconds`, the one shared reader of this variable shape)
# overrides the timeout main converts into the poll count.
OPEN_POLL_SECONDS = 0.5
_OPEN_POLL_LIMIT_DEFAULT = 120  # 60 seconds at the default cadence
OPEN_TIMEOUT_ENV_VAR = "FORWARD_OPEN_TIMEOUT_SECONDS"

# The stop poll: SIGINT first, then SIGTERM to a holdout, then SIGKILL, and
# and finally the port-closed verification, at most `_STOP_POLL_LIMIT_DEFAULT`
# polls of each wait. `FORWARD_STOP_TIMEOUT_SECONDS` names the same shape of
# deadline for the stop path.
STOP_POLL_SECONDS = 0.5
_STOP_POLL_LIMIT_DEFAULT = 30  # 15 seconds at the default cadence
STOP_TIMEOUT_ENV_VAR = "FORWARD_STOP_TIMEOUT_SECONDS"

# The SIGTERM-to-SIGKILL escalation reuses one limit for both waits; declared
# for the tests that pin the escalation order.
SIGKILL_POLL_LIMIT = 10

# How long a single port probe may block before it counts as "cannot
# answer" rather than listening or closed.
PORT_PROBE_SECONDS = 0.5

# The tail length of a daemon log quoted into an error message.
_LOG_TAIL_LINES = 10


class ForwardError(RuntimeError):
    """A forward lifecycle operation failed, with everything the operator needs.

    Raised when a child dies before announcing readiness (the message
    carries the log tail), when a stopped daemon's port refuses to close,
    when a record is unreadable, or when the instance name or its inputs
    are invalid. No caller catches this to retry it: every raise is final
    for the operation that hit it.
    """


# Spawns the detached child: given the full argv and the log path the
# child's stdout and stderr stream into, return a handle carrying `.pid`
# and `poll()`. Injected rather than called internally, so the unit suite
# substitutes a fake spawner instead of patching this module.
Spawner = Callable[[Sequence[str], Path], subprocess.Popen[bytes]]

# Signals the stop path sends: injected so tests record them instead of
# signalling real processes.
SignalSender = Callable[[int, int], None]

# The process-aliveness probe the status and stop paths consult; injected
# for the same reason.
AliveProbe = Callable[[int], bool]


# The production poll clock: a real sleep between poll attempts. Defined
# before every default that references it.
def time_sleep(seconds: float) -> None:
    time.sleep(seconds)


@dataclasses.dataclass(frozen=True)
class ForwardRecord:
    """One running (or last-run) forward daemon, as recorded at open time.

    `instance_id` is the engine identifier: the EC2 instance id for an ssm
    forward, or the `ssh://<user>@<host>:<port>` endpoint string for an ssh
    forward. `engine_kind` says which: "ssm" (the default, and what every
    record written before the field existed reads back as) or "ssh".
    """

    pid: int
    instance: str
    instance_id: str
    context: str
    port: int
    argv: tuple[str, ...]
    log: str
    started_at: str
    engine_kind: str = KIND_SSM

    def to_json(self) -> str:
        return json.dumps(dataclasses.asdict(self), indent=2, sort_keys=True) + "\n"

    @classmethod
    def from_json(cls, text: str) -> ForwardRecord:
        try:
            payload: object = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ForwardError(
                "the forward record is not valid JSON; delete it and re-open the forward"
            ) from exc
        if not isinstance(payload, dict):
            raise ForwardError("the forward record is not a JSON object")
        try:
            return cls(
                pid=int(payload["pid"]),
                instance=str(payload["instance"]),
                instance_id=str(payload["instance_id"]),
                context=str(payload["context"]),
                port=int(payload["port"]),
                argv=tuple(str(arg) for arg in payload["argv"]),
                log=str(payload["log"]),
                started_at=str(payload["started_at"]),
                engine_kind=str(payload.get("engine_kind", KIND_SSM)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ForwardError(
                f"the forward record is missing or malformed ({exc}); delete it and "
                "re-open the forward"
            ) from exc


@dataclasses.dataclass(frozen=True)
class ForwardStatus:
    """What one instance's forward looks like right now."""

    instance: str
    record: ForwardRecord | None
    process_alive: bool
    port_listening: bool | None

    @property
    def listening(self) -> bool:
        """The state an operator and automation both want: the tunnel answers."""
        return self.record is not None and self.process_alive and bool(self.port_listening)


def child_argv(
    instance_id: str,
    context: str,
    profile: str,
    region: str,
    *,
    kind: str = KIND_SSM,
    engine: Mapping[str, object] | None = None,
    python: str | None = None,
) -> tuple[str, ...]:
    """The transport command one forward daemon runs, in full.

    The interpreter is this module's own (`sys.executable`), so the daemon
    runs under the same Python the make recipe invoked this module with; the
    transport is invoked as a module, the same form the make recipes use,
    with unbuffered stdout (`-u`): the readiness poll reads the daemon's
    log, and a block-buffered "Connected:" announcement would sit in the
    child's stdout buffer indefinitely -- the plugin's inherited stderr
    lines would reach the log first, and the poll would time out on a
    forward that was actually open.

    `kind` selects the transport: the default `KIND_SSM` spawns
    `devcontainer_config.transport connect` against the EC2 `instance_id`
    exactly as before, while `instances.ENGINE_KIND_SSH` spawns
    `devcontainer_config.transport_ssh connect` against the SSH engine
    values in `engine` (a validated manifest dict, from
    `instances.engine_manifest` or the caller). For the ssh form, the first
    positional slot carries the INSTANCE NAME (the `--name` value) rather
    than an EC2 id, and `profile`/`region` are unused (an ssh engine opens
    no AWS session), but stay positional so every caller passes the same
    five leading arguments for either kind.
    """
    if kind == instances.ENGINE_KIND_SSH:
        if engine is None:
            raise ForwardError(
                "kind 'ssh' requires the engine manifest values "
                "(instances.engine_manifest); none were given"
            )
        return (
            python or sys.executable,
            "-u",
            "-m",
            _TRANSPORT_SSH_MODULE,
            _TRANSPORT_COMMAND,
            "--name",
            instance_id,
            "--host",
            str(engine["host"]),
            "--port",
            str(engine["port"]),
            "--user",
            str(engine["user"]),
            "--docker-addr",
            str(engine["docker_addr"]),
            "--context",
            context,
        )
    return (
        python or sys.executable,
        "-u",
        "-m",
        _TRANSPORT_MODULE,
        _TRANSPORT_COMMAND,
        "--instance-id",
        instance_id,
        "--context",
        context,
        "--profile",
        profile,
        "--region",
        region,
    )


def record_path(instance: str) -> Path:
    """The per-instance forward record, beside the instance's certificates."""
    return instances.certs_dir(instance) / FORWARD_RECORD_FILENAME


def log_path(instance: str) -> Path:
    """The per-instance daemon log, beside the instance's certificates."""
    return instances.certs_dir(instance) / FORWARD_LOG_FILENAME


def read_record(instance: str) -> ForwardRecord | None:
    """The instance's forward record, or None when none was ever written."""
    path = record_path(instance)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ForwardError(f"could not read {path}: {exc}") from exc
    return ForwardRecord.from_json(text)


def _write_record(record: ForwardRecord) -> None:
    path = record_path(record.instance)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(record.to_json(), encoding="utf-8")


def _remove_record(instance: str) -> bool:
    try:
        record_path(instance).unlink()
    except FileNotFoundError:
        return False
    return True


def process_alive(pid: int) -> bool:
    """Whether a process with `pid` is running (`os.kill(pid, 0)` raises when not)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def port_listening(port: int) -> bool | None:
    """Whether something accepts TCP connections on 127.0.0.1:`port`.

    None means the probe itself failed (the answer is unknown), never
    "closed": a caller must treat None as a loud problem, not as absence.
    """
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=PORT_PROBE_SECONDS):
            return True
    except ConnectionRefusedError:
        return False
    except OSError:
        return None


def _log_tail(log_file: Path) -> str:
    """The last lines of the daemon log, for an error message."""
    try:
        text = log_file.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"(the daemon log {log_file} could not be read: {exc})"
    stripped = [line for line in text.splitlines() if line.strip()]
    return "\n".join(stripped[-_LOG_TAIL_LINES:]) if stripped else "(the daemon log is empty)"


def _default_spawner(argv: Sequence[str], log_file: Path) -> subprocess.Popen[bytes]:
    """Spawn the daemon detached: its own session, stdin closed, output to the log.

    The log's parent directory is created here, at the one place the log is
    opened: a first-ever forward on a machine has no per-instance state
    directory yet (`~/.docker/certs/<name>/` springs into existence with the
    daemon, not before it), and an append-open without it would end the open
    with a bare traceback instead of a daemon.
    """
    log_file.parent.mkdir(parents=True, exist_ok=True)
    handle = open(log_file, "ab")
    try:
        return subprocess.Popen(
            list(argv),
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    finally:
        # The child inherited (and now owns) the descriptor through exec;
        # this process's copy is only a duplicate of the one it holds.
        handle.close()


def _poll_open(
    process: subprocess.Popen[bytes],
    log_file: Path,
    *,
    poll_clock: Callable[[float], None],
    poll_seconds: float,
    poll_limit: int,
) -> None:
    """Wait for the readiness marker, polling the process and the log.

    Raises ForwardError with the log tail if the child exits first or the
    limit is exhausted; in the exhausted case the child is stopped before
    raising, so a hung spawn never orphans a half-open AWS session.
    """
    for _ in range(poll_limit):
        if process.poll() is not None:
            raise ForwardError(
                f"the forward daemon exited before announcing readiness "
                f"(exit {process.returncode}); its log, {log_file}:\n"
                f"{_log_tail(log_file)}"
            )
        try:
            text = log_file.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            # The child has not written anything yet: no marker is not an
            # error this early, and the poll limit still bounds the wait.
            poll_clock(poll_seconds)
            continue
        except OSError as exc:
            raise ForwardError(f"could not read the daemon log {log_file}: {exc}") from exc
        if READINESS_MARKER in text:
            return
        poll_clock(poll_seconds)
    _stop_child(process)
    raise ForwardError(
        f"the forward daemon announced no readiness within {poll_limit} polls at "
        f"{poll_seconds}s intervals; it was stopped. Its log, {log_file}:\n"
        f"{_log_tail(log_file)}"
    )


def _stop_child(process: subprocess.Popen[bytes]) -> None:
    """Stop a live child handle: SIGTERM then SIGKILL, never waiting forever."""
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def instance_from_context_name(root: Path, context: str) -> str:
    """The bare instance name a full docker context name carries.

    Delegates to the transport's own reader so the `<repo-slug>-` prefix is
    stripped in exactly one place.
    """
    return transport.instance_from_context_name(root, context)


def _context_port_runner(command: Sequence[str], timeout_seconds: float | None) -> CommandResult:
    """The production port-probe runner: transport's own bounded runner.

    Kept as this module's own name so a test can substitute it without
    patching transport.
    """
    return transport.subprocess_command_runner(command, timeout_seconds)


def _recorded_port(root: Path, instance: str, runner: CommandRunner | None) -> int:
    """The port the docker context carries after the child's own handshake."""
    probe = runner if runner is not None else _context_port_runner
    try:
        return instances.forwarded_port(root, instance, probe)
    except instances.InstancesError as exc:
        raise ForwardError(
            "the forward daemon announced readiness, but the forwarded port could not be "
            f"read from the docker context: {exc}"
        ) from exc


def open_forward(
    instance_id: str,
    context: str,
    profile: str,
    region: str,
    root: Path,
    *,
    kind: str = KIND_SSM,
    engine: Mapping[str, object] | None = None,
    runner: CommandRunner | None = None,
    spawner: Spawner | None = None,
    poll_clock: Callable[[float], None] = time_sleep,
    poll_seconds: float = OPEN_POLL_SECONDS,
    poll_limit: int = _OPEN_POLL_LIMIT_DEFAULT,
) -> str:
    """Open one forward daemon and confirm it before returning.

    Spawns the transport's connect command detached (the ssm transport for
    the default kind, the ssh transport for an SSH engine), polls for its
    "Connected:" announcement (printed only after the docker context
    answered a handshake through the tunnel), records the daemon -- pid,
    full command, log, and the forwarded port the context now carries --
    and returns a message naming what is open. Idempotent: a forward whose
    daemon is alive and whose port answers is left untouched and reported;
    a stale record (daemon dead) is replaced by the new daemon and noted.

    For `kind == instances.ENGINE_KIND_SSH`, `engine` carries the SSH
    engine's values; when it is `None`, the manifest is read from `root`
    (`instances.engine_manifest`). Either way the values are validated
    through `instances.validate_engine_values` before any daemon is
    spawned, and the record's engine identifier is the
    `ssh://<user>@<host>:<port>` endpoint string rather than the EC2 id.

    Returns:
        A message naming what is open and where its log is.

    Raises:
        ForwardError: the docker context name carries no valid instance
            name, the engine values are missing or invalid, the child died
            before announcing readiness, readiness was not announced within
            the limit, or the forwarded port could not be read from the
            docker context afterwards.
    """
    instance = instance_from_context_name(root, context)
    _validated(instance)
    stale = read_record(instance)
    if stale is not None and process_alive(stale.pid):
        return (
            f"forward for {instance!r} is already open (pid {stale.pid}, port "
            f"{stale.port}); nothing to do. Refresh it with: make connect-refresh "
            f"INSTANCE={instance}"
        )
    if kind == instances.ENGINE_KIND_SSH:
        values = engine if engine is not None else instances.engine_manifest(root, instance)
        try:
            validated = instances.validate_engine_values(
                {
                    "kind": instances.ENGINE_KIND_SSH,
                    "host": values["host"],
                    "user": values["user"],
                    "port": values["port"],
                    "docker_addr": values["docker_addr"],
                }
            )
        except instances.InstancesError as exc:
            raise ForwardError(f"the ssh engine values are unusable: {exc}") from exc
        # The endpoint string is composed once, by the ssh transport's own
        # composer over the same validated values, rather than a third,
        # independently drifting f-string copy here.
        identifier = transport_ssh.engine_endpoint(validated)
        argv = list(child_argv(instance, context, "", "", kind=kind, engine=validated))
    else:
        identifier = instance_id
        argv = list(child_argv(instance_id, context, profile, region))
    spawn = spawner if spawner is not None else _default_spawner
    log_file = log_path(instance)
    process = spawn(argv, log_file)
    _poll_open(
        process, log_file, poll_clock=poll_clock, poll_seconds=poll_seconds, poll_limit=poll_limit
    )
    port = _recorded_port(root, instance, runner)
    record = ForwardRecord(
        pid=process.pid,
        instance=instance,
        instance_id=identifier,
        context=context,
        port=port,
        argv=tuple(argv),
        log=str(log_file),
        started_at=datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
        engine_kind=kind,
    )
    _write_record(record)
    note = ""
    if stale is not None:
        note = " (a stale record from a daemon that was not running was replaced)"
    return (
        f"forward for {instance!r} is open: pid {record.pid}, 127.0.0.1:{port} -> "
        f"{identifier}, log {log_file}{note}"
    )


def status_forward(instance: str) -> ForwardStatus:
    """One instance's forward state: the record, the process, the port.

    Raises:
        ForwardError: the instance name is invalid.
    """
    _validated(instance)
    record = read_record(instance)
    if record is None:
        return ForwardStatus(
            instance=instance, record=None, process_alive=False, port_listening=None
        )
    alive = process_alive(record.pid)
    listening = port_listening(record.port) if alive else None
    return ForwardStatus(
        instance=instance, record=record, process_alive=alive, port_listening=listening
    )


def list_forwards(root: Path) -> tuple[ForwardStatus, ...]:
    """One `ForwardStatus` per configured instance, in `instances.discover` order."""
    return tuple(status_forward(instance) for instance in instances.discover(root))


def stop_forward(
    instance: str,
    *,
    send_signal: SignalSender | None = None,
    alive_probe: AliveProbe | None = None,
    listening_probe: Callable[[int], bool | None] | None = None,
    poll_clock: Callable[[float], None] = time_sleep,
    poll_seconds: float = STOP_POLL_SECONDS,
    poll_limit: int = _STOP_POLL_LIMIT_DEFAULT,
) -> str:
    """Stop one forward daemon and verify the tunnel is actually closed.

    The daemon is a session leader: its whole process tree -- the transport
    wrapper, its `aws ssm start-session` child, and the
    session-manager-plugin that actually holds the listening port -- lives
    in one process group. Signals go to that GROUP, never to the wrapper
    alone: SIGINT first (the transport tears its aws child down cleanly on
    the interrupt, which a SIGTERM would bypass, and the plugin exits on
    it too), then SIGTERM, then SIGKILL to a holdout, each waited out by
    polling. The stop condition is the port, not the process: a wrapper
    that died while its plugin kept the tunnel -- the orphan the previous
    per-process signalling left behind -- is stopped the same way, and no
    record is removed until nothing listens on the port anymore. No record
    is the desired state already, and reports as such.

    Returns:
        A message naming what was stopped, or that nothing was running.

    Raises:
        ForwardError: the instance name is invalid, the process group
            refused to die, or the port never closed.
    """
    _validated(instance)
    send = send_signal if send_signal is not None else _signal_group
    alive = alive_probe if alive_probe is not None else process_alive
    listening = listening_probe if listening_probe is not None else port_listening
    record = read_record(instance)
    if record is None:
        return f"no forward for {instance!r}; nothing to stop"
    if alive(record.pid):
        stopped = f"stopped forward for {instance!r} (pid {record.pid})"
    else:
        stopped = f"forward for {instance!r} was not running (pid {record.pid} is gone)"
    _terminate_tree(record.pid, send, alive, poll_clock, poll_seconds)
    _verify_port_closed(record.port, listening, poll_clock, poll_seconds, poll_limit)
    _remove_record(instance)
    return stopped


def _signal_group(pid: int, sig: int) -> None:
    """Signal the daemon's whole process group (the pid is its session leader)."""
    os.killpg(pid, sig)


def _terminate_tree(
    pid: int,
    send: SignalSender,
    alive: AliveProbe,
    poll_clock: Callable[[float], None],
    poll_seconds: float,
) -> None:
    """Signal the group through SIGINT, SIGTERM, then SIGKILL until the tree is gone.

    A group whose last member already exited raises ProcessLookupError from
    the signal call, which is the stopped state, not a failure.
    """
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGKILL):
        _send_to_group_quietly(pid, send, sig)
        for _ in range(SIGKILL_POLL_LIMIT):
            if not _alive_quiet(pid, alive):
                return
            poll_clock(poll_seconds)
    raise ForwardError(
        f"the forward daemon's process group (pgid {pid}) refused to stop after "
        "SIGINT, SIGTERM and SIGKILL"
    )


def _send_to_group_quietly(pid: int, send: SignalSender, sig: int) -> None:
    """Send one group signal, treating a vanished group as already stopped."""
    try:
        send(pid, sig)
    except ProcessLookupError:
        pass


def _alive_quiet(pid: int, alive: AliveProbe) -> bool:
    """The injected aliveness probe, treating a probe that cannot answer as still alive."""
    try:
        return alive(pid)
    except OSError:
        return True


def _verify_port_closed(
    port: int,
    listening: Callable[[int], bool | None],
    poll_clock: Callable[[float], None],
    poll_seconds: float,
    poll_limit: int,
) -> None:
    """Poll the forwarded port until nothing listens on it; raise if it stays live."""
    for _ in range(poll_limit):
        state = listening(port)
        if state is False:
            return
        if state is None:
            raise ForwardError(
                f"the forwarded port {port} could not be probed after stopping the daemon; "
                "whether the tunnel is closed is unknown"
            )
        poll_clock(poll_seconds)
    raise ForwardError(
        f"the forward daemon's port {port} is still listening after the stop; the tunnel "
        f"is not closed. Find the holding process: lsof -i :{port}"
    )


def refresh_forward(
    instance: str,
    root: Path,
    *,
    runner: CommandRunner | None = None,
    spawner: Spawner | None = None,
    poll_clock: Callable[[float], None] = time_sleep,
    poll_seconds: float = OPEN_POLL_SECONDS,
    open_poll_limit: int = _OPEN_POLL_LIMIT_DEFAULT,
) -> str:
    """Stop and re-open one forward from its own recorded command.

    An expired SSO session or a dropped tunnel is fixed by re-running the
    exact command the daemon was opened with -- stored in the record, so
    refresh never re-resolves ids, contexts, profiles or regions and can
    never re-open a different tunnel than the one that expired.

    Raises:
        ForwardError: there is no record to refresh, or the stop or the
            re-open failed (each with its own reason attached).
    """
    record = read_record(instance)
    if record is None:
        raise ForwardError(
            f"no forward for {instance!r} to refresh; open one with: make connect-start "
            f"INSTANCE={instance}"
        )
    stop_message = stop_forward(instance, poll_clock=poll_clock, poll_seconds=poll_seconds)
    argv = list(record.argv)
    log_file = log_path(instance)
    spawn = spawner if spawner is not None else _default_spawner
    process = spawn(argv, log_file)
    _poll_open(
        process,
        log_file,
        poll_clock=poll_clock,
        poll_seconds=poll_seconds,
        poll_limit=open_poll_limit,
    )
    port = _recorded_port(root, instance, runner)
    fresh = dataclasses.replace(record, pid=process.pid, port=port, started_at=_now())
    _write_record(fresh)
    return (
        f"refreshed forward for {instance!r}: {stop_message}; new daemon pid {fresh.pid}, "
        f"127.0.0.1:{port} -> {record.instance_id}, log {log_file}"
    )


def _validated(instance: str) -> None:
    """Validate the instance name, wrapping the rejection in this module's error."""
    try:
        instances.validate_name(instance)
    except instances.InvalidInstanceNameError as exc:
        raise ForwardError(f"invalid instance name {instance!r}: {exc}") from exc


def _now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")


def _open_poll_limit() -> int:
    timeout = read_positive_seconds(
        OPEN_TIMEOUT_ENV_VAR, float(_OPEN_POLL_LIMIT_DEFAULT * OPEN_POLL_SECONDS)
    )
    return max(1, round(timeout / OPEN_POLL_SECONDS))


def _stop_poll_limit() -> int:
    timeout = read_positive_seconds(
        STOP_TIMEOUT_ENV_VAR, float(_STOP_POLL_LIMIT_DEFAULT * STOP_POLL_SECONDS)
    )
    return max(1, round(timeout / STOP_POLL_SECONDS))


def main(argv: Sequence[str] | None = None) -> int:
    """Parse `argv`, run the selected lifecycle subcommand, print its report.

    `open` takes the resolved connection values the make recipe supplies;
    `status`, `stop` and `refresh` take one instance name; `list` takes
    none. Exit 0 means the operation produced its answer or reached its
    state: a status or list row that reports anything -- listening, down,
    or no forward -- is a success, because the report IS the outcome, and
    so is a stop that verified the port closed and an open whose handshake
    answered. Exit 1 with the reason on stderr is reserved for real
    failures: an unanswerable probe, a daemon that would not die, a port
    that would not close, an invalid name.
    """
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments:
        print(_usage(), file=sys.stderr)
        return 2
    command, rest = arguments[0], arguments[1:]
    try:
        if command == "open":
            return _run_open(rest)
        if command == "status":
            return _run_status(rest)
        if command == "stop":
            return _run_stop(rest)
        if command == "refresh":
            return _run_refresh(rest)
        if command == "list":
            return _run_list(rest)
    except ForwardError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(_usage(), file=sys.stderr)
    return 2


def _usage() -> str:
    return (
        "usage: devcontainer_config.forwards open --instance-id ID --context CTX "
        "--profile P --region R | open --name NAME --host HOST --port PORT --user USER "
        "--docker-addr ADDR --context CTX | status INSTANCE | stop INSTANCE | "
        "refresh INSTANCE | list"
    )


def _require_args(rest: Sequence[str], count: int, what: str) -> list[str]:
    if len(rest) != count:
        raise ForwardError(f"{what} takes exactly {count} argument(s); got {len(rest)}")
    return list(rest)


# The open command's two flag forms, one per transport kind. `--context` is
# the one flag both share; the ssm form names the EC2 id and the AWS
# session, the ssh form names the SSH engine's values.
_SSM_OPEN_FLAGS = ("--instance-id", "--context", "--profile", "--region")
_SSH_OPEN_FLAGS = ("--name", "--host", "--port", "--user", "--docker-addr", "--context")


def _parse_open_flags(rest: Sequence[str]) -> tuple[dict[str, str], str]:
    """`rest` parsed per kind: the ssm form or the ssh form, and which it was.

    The kind is decided by which form's identifying flag is present:
    `--instance-id` selects the ssm form, `--name` the ssh form, and mixing
    the two, or supplying neither, is refused before anything is
    spawned. Missing flags of the chosen form are refused the same way.

    Raises:
        ForwardError: a flag is unknown, the forms are mixed, or the chosen
            form is missing required flags.
    """
    known = {*_SSM_OPEN_FLAGS, *_SSH_OPEN_FLAGS}
    values: dict[str, str] = {}
    index = 0
    while index < len(rest):
        flag = rest[index]
        if flag not in known or index + 1 >= len(rest):
            raise ForwardError(
                f"open takes only {'/'.join(sorted(known))} each with a value; got {flag!r}"
            )
        values[flag[2:]] = rest[index + 1]
        index += 2
    has_instance_id = "instance-id" in values
    has_name = "name" in values
    if has_instance_id == has_name:
        raise ForwardError(
            "open takes exactly one transport form: the ssm form "
            f"({'/'.join(_SSM_OPEN_FLAGS)}) or the ssh form "
            f"({'/'.join(_SSH_OPEN_FLAGS)})"
        )
    flags = _SSM_OPEN_FLAGS if has_instance_id else _SSH_OPEN_FLAGS
    missing = sorted({flag[2:] for flag in flags} - set(values))
    if missing:
        raise ForwardError(f"open is missing required flags: {missing}")
    return values, (KIND_SSM if has_instance_id else instances.ENGINE_KIND_SSH)


def _positive_port(raw: str) -> int:
    """`raw` as a positive integer port, refusing anything else by name."""
    try:
        value = int(raw)
    except ValueError as exc:
        raise ForwardError(f"--port must be an integer; got {raw!r}") from exc
    if value <= 0:
        raise ForwardError(f"--port must be a positive integer; got {raw!r}")
    return value


def _run_open(rest: Sequence[str]) -> int:
    values, kind = _parse_open_flags(rest)
    root = repo.find_root(Path.cwd())
    if kind == instances.ENGINE_KIND_SSH:
        engine = {
            "host": values["host"],
            "user": values["user"],
            "port": _positive_port(values["port"]),
            "docker_addr": values["docker-addr"],
        }
        print(
            open_forward(
                values["name"],
                values["context"],
                "",
                "",
                root,
                kind=kind,
                engine=engine,
            )
        )
        return 0
    print(
        open_forward(
            values["instance-id"], values["context"], values["profile"], values["region"], root
        )
    )
    return 0


def _run_status(rest: Sequence[str]) -> int:
    (instance,) = _require_args(rest, 1, "status")
    print(_render_status_row(status_forward(instance)))
    # A correct report, whether listening, down, or no forward at all, is a
    # success: the row IS the answer. Only a real failure (an unanswerable
    # probe, an invalid name) is an error, and main turns those into exit 1.
    return 0


def _run_stop(rest: Sequence[str]) -> int:
    (instance,) = _require_args(rest, 1, "stop")
    print(stop_forward(instance))
    return 0


def _run_refresh(rest: Sequence[str]) -> int:
    (instance,) = _require_args(rest, 1, "refresh")
    root = repo.find_root(Path.cwd())
    print(refresh_forward(instance, root))
    return 0


def _run_list(rest: Sequence[str]) -> int:
    _require_args(rest, 0, "list")
    root = repo.find_root(Path.cwd())
    statuses = list_forwards(root)
    if not statuses:
        print("No instances configured under remote-instances/; no forwards to list.")
        return 0
    for status in statuses:
        print(_render_status_row(status))
    # A forward that is simply down is a valid answer, not an error; only a
    # probe on a LIVE daemon that could not answer leaves the state unknown,
    # and that fails. A stale record's port is never probed at all.
    probe_failed = any(
        status.record is not None and status.process_alive and status.port_listening is None
        for status in statuses
    )
    return 1 if probe_failed else 0


def _render_status_row(status: ForwardStatus) -> str:
    """One aligned row: instance, pid, port, state, context."""
    if status.record is None:
        return f"{status.instance:<20} {'-':>7}  {'-':>6}  {'no forward':<14} -"
    if status.listening:
        state = "listening"
    elif status.process_alive:
        state = "port not answering"
    else:
        state = "stale record"
    # A probe that could not answer is only reportable while the daemon is
    # alive: for a dead one nothing was probed, so there is nothing failed.
    note = (
        ""
        if not status.process_alive or status.port_listening is not None
        else (" (port probe failed)")
    )
    return (
        f"{status.instance:<20} {status.record.pid:>7}  {status.record.port:>6}  "
        f"{state:<14} {status.record.context}{note}"
    )


if __name__ == "__main__":
    sys.exit(main())
