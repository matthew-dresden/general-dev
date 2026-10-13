#!/usr/bin/env python3
"""The VM-side azure Relay hybrid-connection listener for the docker API.

This script runs on the azure virtual machine the shared azure layer deploys,
under the `gd-relay.service` systemd unit beside it. It is the listener half
of the relay couple the laptop's `devcontainer_config.transport_azure` is the
sender half of, and it obeys the same live-verified wire contract:

- The control channel dials `wss://<namespace>/$hc/<hybrid-connection>` with
  `sb-hc-action=listen` and the managed-identity token in the
  `ServiceBusAuthorization` request header. No `sb-hc-id` is ever sent: a
  custom id makes the couple uncouplable server-side (the accept dial
  answers "Sender Gateway: Listener accepted connection too late"), a
  live-verified azure defect.
- The service pushes `{"accept": {"address": ...}}` text frames over the
  control channel; this script dials that address AS-IS -- no auth header,
  no extra headers -- and once upgraded, that socket is a raw bidirectional
  byte pipe to the sender, bridged to the docker daemon on the loopback
  target.
- The service drops the control channel when the token expires, so every
  reconnect fetches a fresh token from the instance identity endpoint first.

The RFC 6455 client here deliberately restates the one in
`devcontainer_config.transport_azure` instead of importing it: this script
ships to a VM that has no checkout of that package, and both halves staying
stdlib-only (Ubuntu 24.04's system python3, no pip) is the constraint the
azure layer installs under. One line per state change goes to stdout, where
systemd captures it.

The idle self-stop is the billing guard. Azure bills a hybrid connection
while a listener is connected, so a unit left running with no rendezvous
bridge open accrues listener-hours for nothing. When no rendezvous socket
has been open for `GD_RELAY_IDLE_TIMEOUT_SECONDS` (default 900; 0 disables
the self-stop entirely), the listener stops its own unit -- `systemctl stop
gd-relay` -- and exits 0, which systemd's `Restart=on-failure` does not
restart. A bridging socket counts as active even when it is quiet: the
relay accrues listener-hours while the control channel is connected, and a
quiet bridge's pipe is exactly what a connected-but-working forward looks
like. The idle check never busy-loops: it rides a monotonic deadline
checked inside the control channel's read loop, which is bounded by a read
timeout rather than left blocking forever when a timeout is configured.

No secret is ever taken from argv: the three configuration values are
resource names, and the credential comes from IMDS, never from the command
line.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import secrets
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import NoReturn

RELAY_RESOURCE = "https://relay.azure.net"
IMDS_TOKEN_URL = (
    "http://169.254.169.254/metadata/identity/oauth2/token"
    "?api-version=2018-02-01&resource=https://relay.azure.net"
)
IMDS_METADATA_HEADER = "Metadata"
DEFAULT_TARGET = "127.0.0.1:2376"

# The idle self-stop (the billing guard): after this many seconds with no
# open rendezvous bridge, the listener stops its own unit and exits 0. The
# default covers a workday coffee break, not an overnight left-open forward;
# 0 disables the self-stop entirely, for an operator who prefers the unit to
# run until it is stopped by hand.
IDLE_TIMEOUT_ENV_VAR = "GD_RELAY_IDLE_TIMEOUT_SECONDS"
DEFAULT_IDLE_TIMEOUT_SECONDS = 900.0

# The longest one control-channel read may block while the idle deadline is
# being watched. The idle check rides these read timeouts, so the deadline
# is noticed within this window of expiring instead of the read blocking
# forever; every wake is one monotonic comparison, never a poll loop of its
# own.
IDLE_CHECK_MAX_SECONDS = 60.0

# The unit this script runs under; stopping it is the self-stop. Named once
# because the systemd unit name and the unit file's own name must agree.
UNIT_STOP_COMMAND = ("systemctl", "stop", "gd-relay")

# IMDS answers in well under a second on a healthy instance; the timeout
# exists so a wedged metadata endpoint fails the attempt and enters the
# bounded backoff instead of hanging the daemon. Attempts and backoff are
# env-overridable so a slow instance can be accommodated without editing
# this file.
IMDS_TIMEOUT_SECONDS = 2.0
IMDS_ATTEMPTS = int(os.environ.get("GD_RELAY_IMDS_ATTEMPTS", "8"))
IMDS_BACKOFF_INITIAL_SECONDS = 1.0
IMDS_BACKOFF_MAX_SECONDS = 30.0

# The control channel's reconnect backoff: exponential from this floor up to
# the cap, reset on every successful establishment. The service drops the
# control channel at token expiry, so a reconnect is routine, and the cap
# keeps a long outage from pinning a CPU while systemd's own Restart guard
# bounds a crash loop.
RECONNECT_BACKOFF_INITIAL_SECONDS = 1.0
RECONNECT_BACKOFF_MAX_SECONDS = 60.0

# RFC 6455: the accept-answer GUID and the opcodes this client speaks.
WEBSOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
OP_CONTINUATION = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA

AUTH_HEADER_NAME = "ServiceBusAuthorization"
ACTION_LISTEN = "listen"
CLOSE_NORMAL = 1000

CHUNK_BYTES = 65536


def log(message: str) -> None:
    """One state-change line to stdout; systemd captures it."""
    print(f"[gd-relay] {message}", flush=True)


class RelayWebSocket:
    """The RFC 6455 client both relay roles dial through.

    Client-to-server frames are always masked; the handshake rides the
    `ServiceBusAuthorization` header exactly when a token is given, and the
    rendezvous address is dialed with `token=None` -- the service's accept
    endpoint authenticates the couple, not the dial.
    """

    def __init__(
        self,
        url: str,
        token: str | None,
        *,
        timeout_seconds: float,
        dial: Callable[[str, int, float], socket.socket] | None = None,
    ) -> None:
        self._send_lock = threading.Lock()
        self._buffer = bytearray()
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != "wss" or not parts.hostname:
            raise ValueError(f"not a dialable wss:// URL: {url!r}")
        host, port, path = parts.hostname, parts.port or 443, parts.path
        if parts.query:
            path = f"{path}?{parts.query}"
        connect = dial if dial is not None else _dial_wss
        self._socket = connect(host, port, timeout_seconds)
        try:
            self._socket.settimeout(timeout_seconds)
            key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
            host_header = host if port == 443 else f"{host}:{port}"
            lines = [
                f"GET {path} HTTP/1.1",
                f"Host: {host_header}",
                "Upgrade: websocket",
                "Connection: Upgrade",
                f"Sec-WebSocket-Key: {key}",
                "Sec-WebSocket-Version: 13",
            ]
            if token is not None:
                lines.append(f"{AUTH_HEADER_NAME}: {token}")
            self._socket.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("ascii"))
            buffer = bytearray()
            while b"\r\n\r\n" not in buffer:
                chunk = self._socket.recv(CHUNK_BYTES)
                if not chunk:
                    raise ConnectionError(f"closed during handshake for {url}")
                buffer.extend(chunk)
            head, _, rest = bytes(buffer).partition(b"\r\n\r\n")
            status_line = head.split(b"\r\n", 1)[0].decode("ascii", errors="replace")
            if " 101" not in status_line:
                raise RelayHandshakeError(status_line=status_line, body=head.decode("latin-1"))
            expected = base64.b64encode(
                hashlib.sha1((key + WEBSOCKET_GUID).encode("ascii")).digest()
            ).decode("ascii")
            if expected.encode("ascii") not in head:
                raise ValueError("bad Sec-WebSocket-Accept in handshake answer")
            self._buffer.extend(rest)
            # The pipe idles whenever docker idles: after the bounded
            # handshake, reads block until data or close, never on a timer.
            self._socket.settimeout(None)
        except BaseException:
            self._socket.close()
            raise

    def set_read_timeout(self, timeout_seconds: float | None) -> None:
        """Bound (or unbound) this socket's read waits after the handshake.

        The handshake returns the socket fully blocking; the control
        channel's owner tightens it when it needs the read loop to wake for
        the idle check, and passes `None` to leave it blocking forever. The
        buffer survives a timed-out read, so a frame that arrives across a
        wake continues where it left off.
        """
        self._socket.settimeout(timeout_seconds)

    def _read_exact(self, count: int) -> bytes:
        while len(self._buffer) < count:
            chunk = self._socket.recv(CHUNK_BYTES)
            if not chunk:
                raise ConnectionError("the relay connection closed")
            self._buffer.extend(chunk)
        out = bytes(self._buffer[:count])
        del self._buffer[:count]
        return out

    def recv_frame(self) -> tuple[bool, int, bytes]:
        first, second = self._read_exact(2)
        fin = bool(first & 0x80)
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F
        if length == 126:
            (length,) = struct.unpack("!H", self._read_exact(2))
        elif length == 127:
            (length,) = struct.unpack("!Q", self._read_exact(8))
        mask = self._read_exact(4) if masked else b""
        payload = self._read_exact(length) if length else b""
        if masked:
            payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        return fin, opcode, payload

    def send_frame(self, opcode: int, payload: bytes) -> None:
        mask = secrets.token_bytes(4)
        first = 0x80 | opcode
        length = len(payload)
        if length <= 125:
            header = struct.pack("!BB", first, 0x80 | length)
        elif length <= 0xFFFF:
            header = struct.pack("!BBH", first, 0x80 | 126, length)
        else:
            header = struct.pack("!BBQ", first, 0x80 | 127, length)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        with self._send_lock:
            self._socket.sendall(header + mask + masked)

    def send_close(self, code: int = CLOSE_NORMAL) -> None:
        try:
            self.send_frame(OP_CLOSE, struct.pack("!H", code))
        except OSError:
            pass

    def recv_message(self) -> tuple[int, bytes] | None:
        """The next complete message, or `None` once the peer closed.

        Pings are answered with pongs; a peer close is answered with our own
        normal close, so both halves wind down together.
        """
        fragments = bytearray()
        fragment_opcode: int | None = None
        while True:
            fin, opcode, payload = self.recv_frame()
            if opcode == OP_PING:
                self.send_frame(OP_PONG, payload)
                continue
            if opcode == OP_PONG:
                continue
            if opcode == OP_CLOSE:
                self.send_close(CLOSE_NORMAL)
                return None
            if opcode in (OP_TEXT, OP_BINARY):
                fragment_opcode = opcode
                fragments = bytearray(payload)
            elif opcode == OP_CONTINUATION and fragment_opcode is not None:
                fragments.extend(payload)
            else:
                raise ValueError(f"unexpected relay frame (opcode {opcode}, fin {fin})")
            if fin and fragment_opcode is not None:
                return fragment_opcode, bytes(fragments)

    def close(self) -> None:
        try:
            self._socket.close()
        except OSError:
            pass


class RelayHandshakeError(Exception):
    """The relay service refused the WebSocket upgrade; the status line says why."""


def _dial_wss(host: str, port: int, timeout_seconds: float) -> socket.socket:
    raw = socket.create_connection((host, port), timeout=timeout_seconds)
    try:
        return ssl.create_default_context().wrap_socket(raw, server_hostname=host)
    except BaseException:
        raw.close()
        raise


def control_url(namespace: str, hybrid_connection: str) -> str:
    """The listener's control-channel URL: only the action parameter, no sender id."""
    return f"wss://{namespace}/$hc/{hybrid_connection}?sb-hc-action={ACTION_LISTEN}"


def fetch_identity_token() -> str:
    """A fresh relay token from the instance identity endpoint, with bounded retries.

    IMDS is a link-local service that can be briefly unreachable while the
    instance networking settles; the retry loop is bounded in attempts and
    in per-attempt backoff, and exhaustion raises so systemd's restart
    takes over rather than this loop spinning forever.
    """
    backoff = IMDS_BACKOFF_INITIAL_SECONDS
    last_error: Exception | None = None
    for attempt in range(1, IMDS_ATTEMPTS + 1):
        request = urllib.request.Request(IMDS_TOKEN_URL, headers={IMDS_METADATA_HEADER: "true"})
        try:
            with urllib.request.urlopen(request, timeout=IMDS_TIMEOUT_SECONDS) as answer:
                payload = json.loads(answer.read().decode("utf-8"))
            token = payload["access_token"]
            if not isinstance(token, str) or not token:
                raise ValueError("IMDS returned an empty access_token")
            return token
        except (OSError, ValueError, KeyError) as exc:
            last_error = exc
            log(f"identity token attempt {attempt}/{IMDS_ATTEMPTS} failed: {exc}")
        if attempt < IMDS_ATTEMPTS:
            time.sleep(min(backoff, IMDS_BACKOFF_MAX_SECONDS))
            backoff = min(backoff * 2, IMDS_BACKOFF_MAX_SECONDS)
    raise RuntimeError(f"identity token unavailable after {IMDS_ATTEMPTS} attempts: {last_error}")


def bridge_to_target(websocket: RelayWebSocket, target_host: str, target_port: int) -> None:
    """One coupled pipe: the rendezvous socket bridged to the daemon on `target`.

    Same pipe discipline as the laptop side, in reverse: either side's close
    tears both down through the common finally.
    """
    tcp: socket.socket | None = None
    try:
        tcp = socket.create_connection((target_host, target_port), timeout=10.0)
        tcp.settimeout(None)

        def tcp_to_websocket() -> None:
            try:
                while True:
                    data = tcp.recv(CHUNK_BYTES)
                    if not data:
                        break
                    websocket.send_frame(OP_BINARY, data)
            except OSError:
                pass
            finally:
                websocket.send_close()

        pump = threading.Thread(target=tcp_to_websocket, daemon=True)
        pump.start()
        while True:
            message = websocket.recv_message()
            if message is None:
                break
            _, payload = message
            if payload:
                tcp.sendall(payload)
    except OSError:
        pass
    finally:
        websocket.send_close()
        websocket.close()
        if tcp is not None:
            try:
                tcp.close()
            except OSError:
                pass


class BridgeTracker:
    """The open rendezvous bridges, and when one was last open.

    Idle means no OPEN rendezvous socket: a bridging socket, even a quiet
    one, counts as active, because its pipe is a live forward and the
    listener-hours it rides are being earned. Every register and unregister
    moves `last_active` to now, so the idle window is measured from the last
    bridge event, never from process start; a bridge that stays open for
    hours keeps the count above zero the whole time, which is what keeps a
    working forward from being torn down under it.

    One tracker serves every control channel the reconnect loop holds, so a
    bridge that outlives the control channel that spawned it still counts:
    the tracker is created once in `main` and passed down.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._open_count = 0
        self._last_active = time.monotonic()

    def opened(self) -> None:
        """Record one more open rendezvous, and the instant it opened."""
        with self._lock:
            self._open_count += 1
            self._last_active = time.monotonic()

    def closed(self) -> None:
        """Record one rendezvous gone, and the instant it closed."""
        with self._lock:
            self._open_count -= 1
            self._last_active = time.monotonic()

    def idle_seconds(self, now: float) -> float:
        """How long nothing has been open, at monotonic `now`; 0.0 while any is."""
        with self._lock:
            if self._open_count:
                return 0.0
            return now - self._last_active


def parse_idle_timeout(raw: str | None) -> float:
    """The idle self-stop deadline, from `GD_RELAY_IDLE_TIMEOUT_SECONDS`.

    Unset means the default; "0" disables the self-stop outright; anything
    else must be a finite positive number of seconds. A malformed value is
    a startup failure naming the variable, never a guessed fallback: a
    silently ignored typo would leave the operator believing a deadline is
    being enforced when none is.

    Raises:
        SystemExit: the value is set and is neither "0" nor a finite
            positive number.
    """
    if raw is None:
        return DEFAULT_IDLE_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        raise SystemExit(
            f"[gd-relay] {IDLE_TIMEOUT_ENV_VAR}={raw!r} is not a number; "
            "set it to a positive number of seconds, or 0 to disable the idle self-stop"
        ) from None
    if value < 0 or value != value or value in (float("inf"), float("-inf")):
        raise SystemExit(
            f"[gd-relay] {IDLE_TIMEOUT_ENV_VAR}={raw!r} is not a usable timeout; "
            "set it to a positive number of seconds, or 0 to disable the idle self-stop"
        )
    return value


def stop_unit_and_exit(idle_seconds: float, timeout_seconds: float) -> NoReturn:
    """The idle self-stop: stop this unit through systemd, then exit 0.

    Exit 0 is the point: `Restart=on-failure` restarts a failed unit, so a
    non-zero exit here would turn the billing guard into a restart loop.
    The stop is issued without `check`, because a stop that cannot be
    delivered still ends the same way -- this process exiting takes the
    unit down with it.
    """
    log(
        f"idle for {idle_seconds:.0f}s (limit {timeout_seconds:.0f}s) with no open "
        "rendezvous bridge; stopping the unit to stop the listener-hours"
    )
    subprocess.run(list(UNIT_STOP_COMMAND), check=False)
    raise SystemExit(0)


def serve_control_channel(
    namespace: str,
    hybrid_connection: str,
    target_host: str,
    target_port: int,
    *,
    tracker: BridgeTracker,
    idle_timeout_seconds: float,
) -> None:
    """Hold the control channel, bridging every accept frame, until it drops.

    Returns only when the control channel itself is lost, leaving the
    caller's reconnect loop to re-establish it with a fresh token -- unless
    the idle self-stop fires first, in which case it ends the process.

    While an idle deadline is configured (a positive
    `idle_timeout_seconds`), the control channel's reads are bounded by a
    slice no longer than `IDLE_CHECK_MAX_SECONDS`, so the read loop wakes
    regularly and compares one monotonic deadline: no open rendezvous for
    the whole idle window means `stop_unit_and_exit`. A timed-out read that
    does not end in a self-stop just continues the loop -- the frame
    buffer inside the websocket keeps whatever arrived early, so no byte is
    lost to a wake. With the self-stop disabled (`idle_timeout_seconds`
    0), the read stays fully blocking, exactly as it would without the
    guard.
    """
    websocket = RelayWebSocket(
        control_url(namespace, hybrid_connection),
        fetch_identity_token(),
        timeout_seconds=30.0,
    )
    if idle_timeout_seconds > 0:
        websocket.set_read_timeout(min(idle_timeout_seconds, IDLE_CHECK_MAX_SECONDS))
    log(f"control channel established for {namespace}/{hybrid_connection}")
    try:
        while True:
            try:
                message = websocket.recv_message()
            except TimeoutError:
                now = time.monotonic()
                idle_for = tracker.idle_seconds(now)
                if idle_timeout_seconds > 0 and idle_for >= idle_timeout_seconds:
                    stop_unit_and_exit(idle_for, idle_timeout_seconds)
                continue
            if message is None:
                log("control channel closed by the service")
                return
            opcode, payload = message
            if opcode != OP_TEXT:
                continue
            frame = json.loads(payload.decode("utf-8"))
            accept = frame.get("accept")
            if not isinstance(accept, dict):
                log(f"ignoring unexpected control frame: {list(frame)}")
                continue
            address = accept["address"]
            log("accept received; dialing the rendezvous")

            def run_bridge(accept_address: str = address) -> None:
                rendezvous: RelayWebSocket | None = None
                try:
                    # Counted from before the dial to after the close: the
                    # rendezvous counts as open the whole time it exists,
                    # so the idle deadline can never fire underneath a
                    # bridge that is being established or wound down.
                    tracker.opened()
                    # Dialed AS-IS: no auth header, no extra headers. The
                    # service authenticates the couple through the control
                    # channel, and the accept address is single-use.
                    rendezvous = RelayWebSocket(accept_address, None, timeout_seconds=30.0)
                    bridge_to_target(rendezvous, target_host, target_port)
                except (RelayHandshakeError, ConnectionError, OSError, ValueError) as exc:
                    log(f"bridge ended: {type(exc).__name__}: {exc}")
                finally:
                    if rendezvous is not None:
                        rendezvous.close()
                    tracker.closed()

            threading.Thread(target=run_bridge, daemon=True).start()
    finally:
        websocket.send_close()
        websocket.close()


def parse_target(target: str) -> tuple[str, int]:
    """`host:port` from the --target value, refusing anything else."""
    host, separator, port_text = target.rpartition(":")
    if not separator or not host or not port_text.isdigit():
        raise SystemExit(f"[gd-relay] --target {target!r} must be <host>:<port>")
    return host, int(port_text)


def main(argv: list[str] | None = None) -> int:
    """Parse the configuration and hold the reconnect loop forever.

    The env defaults are what the systemd unit's EnvironmentFile supplies
    (GD_RELAY_NAMESPACE / GD_RELAY_HYBRID_CONNECTION / GD_RELAY_TARGET), so
    the unit carries no secret and no resource name on its own ExecStart.
    The idle self-stop deadline comes from GD_RELAY_IDLE_TIMEOUT_SECONDS
    (see `parse_idle_timeout`), and one `BridgeTracker` serves every
    control channel and bridge the process ever holds, so the idle
    measurement spans reconnects.
    """
    parser = argparse.ArgumentParser(description="Azure Relay listener for the docker API.")
    parser.add_argument(
        "--namespace",
        default=os.environ.get("GD_RELAY_NAMESPACE"),
        help="The relay namespace host, e.g. <name>.servicebus.windows.net "
        "(env: GD_RELAY_NAMESPACE).",
    )
    parser.add_argument(
        "--hybrid-connection",
        default=os.environ.get("GD_RELAY_HYBRID_CONNECTION"),
        help="The hybrid connection name (env: GD_RELAY_HYBRID_CONNECTION).",
    )
    parser.add_argument(
        "--target",
        default=os.environ.get("GD_RELAY_TARGET", DEFAULT_TARGET),
        help=f"The docker daemon address to bridge to (default: {DEFAULT_TARGET}; "
        "env: GD_RELAY_TARGET).",
    )
    args = parser.parse_args(argv)
    if not args.namespace or not args.hybrid_connection:
        parser.error(
            "--namespace and --hybrid-connection are required "
            "(or set GD_RELAY_NAMESPACE and GD_RELAY_HYBRID_CONNECTION)"
        )
    target_host, target_port = parse_target(args.target)
    idle_timeout_seconds = parse_idle_timeout(os.environ.get(IDLE_TIMEOUT_ENV_VAR))
    tracker = BridgeTracker()
    backoff = RECONNECT_BACKOFF_INITIAL_SECONDS
    while True:
        try:
            serve_control_channel(
                args.namespace,
                args.hybrid_connection,
                target_host,
                target_port,
                tracker=tracker,
                idle_timeout_seconds=idle_timeout_seconds,
            )
            backoff = RECONNECT_BACKOFF_INITIAL_SECONDS
        except (RelayHandshakeError, ConnectionError, OSError, ValueError, RuntimeError) as exc:
            log(f"control channel dropped: {type(exc).__name__}: {exc}")
        log(f"reconnecting in {backoff:.0f}s (token refreshed on reconnect)")
        time.sleep(backoff)
        backoff = min(backoff * 2, RECONNECT_BACKOFF_MAX_SECONDS)


if __name__ == "__main__":
    sys.exit(main())
