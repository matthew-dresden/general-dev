"""The VM-side azure relay listener: pins on the files the azure layer ships.

`provider/azure/modules/compute/gd-relay-listener.py` runs on the azure
virtual machine under `gd-relay.service`, on Ubuntu 24.04's system python3
with no pip and no checkout of `devcontainer_config` -- so nothing here
executes the script or imports it. Like `tests/test_user_data_aws_cli.py`,
this suite pins the shipped files as text and AST: the script parses, it
imports nothing outside the standard library, it carries the CLI and
environment surface the systemd unit drives, and it obeys the same
live-verified wire contract the laptop-side transport's suite pins (header
authentication, no `sb-hc-id`, the rendezvous dialed without credentials).

These assertions exist because the failure mode is silent and late: a
script that grew a third-party import or lost an environment variable
would provision cleanly and only die on the VM, where the symptom
(systemd restart loop, a relay service refusing the couple) names neither
the file nor the line that changed.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_COMPUTE = _REPO_ROOT / "provider" / "azure" / "modules" / "compute"
_LISTENER = _COMPUTE / "gd-relay-listener.py"
_UNIT = _COMPUTE / "gd-relay.service"
_ENV_EXAMPLE = _COMPUTE / "gd-relay.env.example"


def _listener_text() -> str:
    return _LISTENER.read_text(encoding="utf-8")


def _listener_tree() -> ast.Module:
    return ast.parse(_listener_text(), filename=str(_LISTENER))


# ---------------------------------------------------------------------------
# The script exists, parses, and imports nothing beyond the standard library.
# ---------------------------------------------------------------------------


def test_listener_is_readable_so_this_module_cannot_pass_vacuously() -> None:
    text = _listener_text()
    assert text.startswith("#!/usr/bin/env python3"), "not the standalone script this module pins"
    assert "hybrid" in text.lower()


def test_listener_imports_stdlib_modules_only() -> None:
    """The VM has no pip and no checkout of this repository: stdlib or nothing."""
    tree = _listener_tree()
    third_party: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name.split(".")[0] for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level or node.module is None:
                continue  # a relative import; none should exist, checked below
            names = [node.module.split(".")[0]]
        else:
            continue
        for name in names:
            if name not in sys.stdlib_module_names:
                third_party.append(name)
    assert not third_party, (
        f"gd-relay-listener.py imports non-stdlib module(s) {sorted(set(third_party))}; "
        "the VM runs Ubuntu 24.04's system python3 with no package installer"
    )


def test_listener_imports_nothing_relative_or_from_this_repository() -> None:
    """The script ships alone: no relative import and no devcontainer_config."""
    tree = _listener_tree()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert not node.level, (
                "gd-relay-listener.py carries a relative import; it ships to the VM alone"
            )
            assert node.module != "devcontainer_config", (
                "gd-relay-listener.py must not import the laptop-side package"
            )
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("devcontainer_config"), (
                    "gd-relay-listener.py must not import the laptop-side package"
                )


# ---------------------------------------------------------------------------
# The wire contract: header auth, no sb-hc-id, the rendezvous dialed bare.
# ---------------------------------------------------------------------------


def test_listener_authenticates_the_control_channel_by_header() -> None:
    text = _listener_text()
    assert "ServiceBusAuthorization" in text, (
        "the control channel's token must ride the ServiceBusAuthorization request header"
    )
    assert "sb-hc-action=listen" in text, "the listener action is the control channel's dial"
    assert "sb-hc-token" not in text, "the token rides the request header, never the query string"
    # The forbidden parameter is pinned where it could actually be sent:
    # the URL builder and the handshake head. (The module docstring names
    # it in prose, explaining why it must never appear, so the pin cannot
    # be a whole-file search.)
    control = re.search(r"def control_url\(.*?return [^\n]+", text, re.DOTALL)
    assert control is not None, "no control_url function found"
    assert "sb-hc-id" not in control.group(0)
    handshake = re.search(r"lines = \[.*?\]", text, re.DOTALL)
    assert handshake is not None, "no handshake head construction found"
    assert "sb-hc-id" not in handshake.group(0)


def test_listener_dials_the_rendezvous_bare() -> None:
    """The accept address is dialed AS-IS: no auth header, no extra headers."""
    text = _listener_text()
    match = re.search(r"RelayWebSocket\(accept_address, None", text)
    assert match is not None, (
        "the rendezvous dial must pass token=None: the accept endpoint "
        "authenticates the couple, not the dial"
    )


def test_listener_bridges_to_the_target_with_the_pipe_discipline() -> None:
    text = _listener_text()
    assert "create_connection" in text, "the rendezvous must be bridged to a real TCP target"
    assert 'DEFAULT_TARGET = "127.0.0.1:2376"' in text, (
        "the default target is the mTLS daemon's loopback listener"
    )
    # Either side's close tears both down: the bridge's finally closes the
    # websocket and the tcp socket.
    bridge = re.search(r"def bridge_to_target\(.*?\n(.*?)\n\ndef ", text, re.DOTALL)
    assert bridge is not None, "no bridge_to_target function found"
    assert "finally:" in bridge.group(1)
    assert "send_close" in bridge.group(1)
    assert "tcp.close" in bridge.group(1)


def test_listener_bridge_half_close_discipline() -> None:
    """`docker exec -i`: stdin's EOF must not kill the stdout still flowing.

    The pump forwards the target's EOF as ONE empty-payload binary frame --
    guarded to one per direction, never a close frame and never a local
    shutdown on that path -- and the peer's sentinel half-closes the target
    write side, with the pipe torn down once BOTH directions have ended.
    """
    text = _listener_text()
    bridge = re.search(r"def bridge_to_target\(.*?\n(.*?)\n\ndef ", text, re.DOTALL)
    assert bridge is not None, "no bridge_to_target function found"
    body = bridge.group(1)
    assert 'websocket.send_frame(OP_BINARY, b"")' in body, (
        "the EOF sentinel is the empty-payload binary frame"
    )
    assert body.count("eof_forwarded.is_set()") == 2, (
        "one guard on the sentinel send, one on the both-EOF completion"
    )
    assert "elif opcode == OP_BINARY:" in body, "the peer's sentinel arrives as binary"
    assert "tcp.shutdown(socket.SHUT_WR)" in body, (
        "the sentinel half-closes the write side toward docker"
    )
    assert "target_write_shut.is_set()" in body, "the shutdown is guarded to once"
    pump = re.search(r"def tcp_to_websocket\(\) -> None:\n(.*?)\n\n", body, re.DOTALL)
    assert pump is not None, "no tcp_to_websocket pump found"
    assert "send_close" not in pump.group(1), (
        "the pump's EOF path must not close the websocket: the reverse direction "
        "may still be mid-flight"
    )


# ---------------------------------------------------------------------------
# The frame-length ceiling.
# ---------------------------------------------------------------------------


def test_listener_refuses_frames_declaring_more_than_the_ceiling() -> None:
    """A corrupt or hostile 64-bit length must fail one read, never the buffer."""
    text = _listener_text()
    assert "MAX_FRAME_BYTES = 16 * 1024 * 1024" in text
    recv = re.search(r"def recv_frame\(.*?\n(.*?)\n\n    def send_frame", text, re.DOTALL)
    assert recv is not None, "no recv_frame found"
    body = recv.group(1)
    assert "if length > MAX_FRAME_BYTES:" in body, (
        "the ceiling is checked once the declared length is decoded, before any "
        "payload byte is read"
    )
    assert "raise ValueError" in body, "an oversized declaration fails the read"
    assert "declared a {length}-byte frame" in text, "the error names the declared size"


# ---------------------------------------------------------------------------
# The token never reaches the journal through a handshake failure.
# ---------------------------------------------------------------------------


def test_listener_redacts_its_token_from_logged_handshake_failures() -> None:
    """The systemd journal is permanent: the same scrubbing the laptop side
    applies keeps this script's own token out of every logged body and
    status line."""
    text = _listener_text()
    assert "def redact(text: str, token: str | None) -> str:" in text
    assert "status_line=redact(status_line, token)," in text
    assert 'body=redact(head.decode("latin-1"), token),' in text
    assert "text.replace(token" in text, "the redactor substitutes the token itself"


# ---------------------------------------------------------------------------
# The identity credential: IMDS, short timeout, bounded backoff.
# ---------------------------------------------------------------------------


def test_listener_fetches_the_token_from_imds_with_metadata_header() -> None:
    text = _listener_text()
    assert "169.254.169.254/metadata/identity/oauth2/token" in text, (
        "the credential comes from the instance identity endpoint"
    )
    assert "api-version=2018-02-01" in text
    assert "resource=https://relay.azure.net" in text
    assert '"Metadata": "true"' in text or "IMDS_METADATA_HEADER" in text, (
        "IMDS refuses a request without the Metadata header"
    )


def test_listener_imds_retries_are_bounded() -> None:
    """A wedged metadata endpoint must fail the attempt, not hang the daemon.

    The retry loop is bounded in attempts and in per-attempt backoff, and
    exhaustion raises so systemd's restart guard takes over.
    """
    text = _listener_text()
    fetch = re.search(r"def fetch_identity_token\(.*?\n(.*?)\n\ndef ", text, re.DOTALL)
    assert fetch is not None, "no fetch_identity_token function found"
    body = fetch.group(1)
    assert "for attempt in range(1, IMDS_ATTEMPTS + 1)" in body, (
        "the retry loop must be bounded by IMDS_ATTEMPTS"
    )
    assert "IMDS_BACKOFF_MAX_SECONDS" in body, "the per-attempt backoff must be capped"
    assert "raise RuntimeError" in body, "exhaustion must raise, never spin forever"


# ---------------------------------------------------------------------------
# The reconnect loop and the environment surface the systemd unit drives.
# ---------------------------------------------------------------------------


def test_listener_reconnects_with_bounded_backoff_and_a_fresh_token() -> None:
    """The service drops the control channel at token expiry: reconnect is routine."""
    text = _listener_text()
    main = re.search(r"def main\(.*?\n(.*?)\n\nif __name__", text, re.DOTALL)
    assert main is not None, "no main function found"
    body = main.group(1)
    assert "while True:" in body, "the listener reconnects forever; it is a daemon"
    assert "RECONNECT_BACKOFF_MAX_SECONDS" in body, "the reconnect backoff must be capped"
    assert "serve_control_channel" in body
    # The token is fetched inside serve_control_channel, so every
    # reconnect mints a fresh one.
    serve = re.search(r"def serve_control_channel\(.*?\n(.*?)\n\ndef ", text, re.DOTALL)
    assert serve is not None
    assert "fetch_identity_token()" in serve.group(1), (
        "every control-channel establishment must fetch a fresh identity token"
    )


def test_listener_carries_the_cli_and_env_surface_the_unit_drives() -> None:
    text = _listener_text()
    for flag in ("--namespace", "--hybrid-connection", "--target"):
        assert f'"{flag}"' in text, f"the unit's environment maps onto the {flag} flag"
    for env_var in ("GD_RELAY_NAMESPACE", "GD_RELAY_HYBRID_CONNECTION", "GD_RELAY_TARGET"):
        assert env_var in text, f"the unit's EnvironmentFile sets {env_var}"
    assert "parser.error" in text, (
        "missing namespace or hybrid connection must fail fast at startup, naming the env"
    )


def test_listener_logs_state_changes_to_stdout() -> None:
    """systemd captures stdout, so every state change is one flushed line."""
    text = _listener_text()
    assert "def log(message: str) -> None:" in text
    assert "flush=True" in text, "an unflushed log line would sit in a stdio buffer forever"


# ---------------------------------------------------------------------------
# The idle self-stop: the billing guard.
# ---------------------------------------------------------------------------


def test_listener_idle_self_stop_env_surface() -> None:
    """The deadline comes from the environment: default 900, 0 disables the guard."""
    text = _listener_text()
    assert 'IDLE_TIMEOUT_ENV_VAR = "GD_RELAY_IDLE_TIMEOUT_SECONDS"' in text
    assert "DEFAULT_IDLE_TIMEOUT_SECONDS = 900.0" in text
    parse = re.search(r"def parse_idle_timeout\(.*?\n(.*?)\n\ndef ", text, re.DOTALL)
    assert parse is not None, "no parse_idle_timeout function found"
    body = parse.group(1)
    assert "return DEFAULT_IDLE_TIMEOUT_SECONDS" in body, "unset means the default"
    assert "return value" in body, "0 parses through and disables the self-stop"
    assert "SystemExit" in body, (
        "a malformed value must fail the startup naming the variable, never a guessed fallback"
    )


def test_listener_self_stop_runs_systemctl_and_exits_zero() -> None:
    """The self-stop stops its own unit, then exits 0 so Restart=on-failure lets it stay down.

    Exit 0 is load-bearing: a non-zero exit would turn the billing guard
    into a restart loop, because the unit restarts on failure.
    """
    text = _listener_text()
    assert 'UNIT_STOP_COMMAND = ("systemctl", "stop", "gd-relay")' in text
    stop = re.search(r"def stop_unit_and_exit\(.*?\n(.*?)\n\ndef ", text, re.DOTALL)
    assert stop is not None, "no stop_unit_and_exit function found"
    body = stop.group(1)
    assert "subprocess.run(list(UNIT_STOP_COMMAND), check=False)" in body, (
        "the stop goes through systemd; a failed delivery still ends the same way, "
        "with this process exiting"
    )
    assert "SystemExit(0)" in body
    assert "check=True" not in body


def test_listener_tracks_open_rendezvous_bridges() -> None:
    """Idle means no OPEN rendezvous socket: a bridging socket, even quiet, counts as active."""
    text = _listener_text()
    tracker = re.search(r"class BridgeTracker:.*?\n(.*?)\n\ndef ", text, re.DOTALL)
    assert tracker is not None, "no BridgeTracker class found"
    body = tracker.group(1)
    assert "def opened" in body and "def closed" in body
    assert "def idle_seconds" in body
    assert "if self._open_count:" in body, (
        "any open bridge answers zero idle time, however quiet it is"
    )
    # The bridge thread registers before the dial and unregisters in its
    # finally, so the deadline can never fire underneath a live bridge.
    serve = re.search(r"def run_bridge\(.*?\n(.*?)\n\n\s+threading.Thread", text, re.DOTALL)
    assert serve is not None, "no run_bridge closure found"
    bridge_body = serve.group(1)
    assert "tracker.opened()" in bridge_body
    assert "tracker.closed()" in bridge_body
    assert bridge_body.index("tracker.opened()") < bridge_body.index(
        "RelayWebSocket(accept_address"
    ), "the rendezvous counts as open from before the dial"


def test_listener_idle_check_rides_the_control_read_loop_without_busy_waiting() -> None:
    """The deadline is checked on bounded read wakes, never on a sleep loop."""
    text = _listener_text()
    assert "IDLE_CHECK_MAX_SECONDS = 60.0" in text
    serve = re.search(r"def serve_control_channel\(.*?\n(.*?)\n\ndef ", text, re.DOTALL)
    assert serve is not None
    body = serve.group(1)
    assert (
        "websocket.set_read_timeout(min(idle_timeout_seconds, IDLE_CHECK_MAX_SECONDS))" in body
    ), "the read is bounded by one slice, so the deadline is noticed within it"
    assert "except TimeoutError:" in body
    assert "tracker.idle_seconds(now)" in body
    assert "stop_unit_and_exit(idle_for, idle_timeout_seconds)" in body
    # The deadline comparison and its timeout are the wake's only work:
    # there is no sleep in the serve loop at all.
    assert "time.sleep" not in body


def test_listener_main_wires_the_tracker_and_the_idle_deadline() -> None:
    """One tracker serves every control channel, so the idle measurement spans reconnects."""
    text = _listener_text()
    main = re.search(r"def main\(.*?\n(.*?)\n\nif __name__", text, re.DOTALL)
    assert main is not None, "no main function found"
    body = main.group(1)
    assert "parse_idle_timeout(os.environ.get(IDLE_TIMEOUT_ENV_VAR))" in body
    assert "tracker = BridgeTracker()" in body
    assert "idle_timeout_seconds=idle_timeout_seconds" in body
    assert "tracker=tracker" in body


# ---------------------------------------------------------------------------
# The systemd unit and its documented environment.
# ---------------------------------------------------------------------------


def test_systemd_unit_pins() -> None:
    text = _UNIT.read_text(encoding="utf-8")
    assert "[Unit]" in text and "[Service]" in text and "[Install]" in text
    assert "EnvironmentFile=/etc/default/gd-relay" in text, (
        "the three resource names come from the environment file, never the unit"
    )
    assert "ExecStart=/usr/bin/python3 /opt/gd-relay/gd-relay-listener.py" in text, (
        "the unit runs the shipped script with the system interpreter"
    )
    assert "Restart=on-failure" in text
    assert "RestartSec=5" in text
    assert "WantedBy=multi-user.target" in text


def test_env_example_documents_the_three_variables() -> None:
    text = _ENV_EXAMPLE.read_text(encoding="utf-8")
    for env_var in ("GD_RELAY_NAMESPACE", "GD_RELAY_HYBRID_CONNECTION", "GD_RELAY_TARGET"):
        assert env_var in text, f"the env example must document {env_var}"
    assert "managed-identity" in text.lower(), (
        "the example must say where the credential actually comes from, so nobody "
        "looks for a secret to fill in"
    )
    assert "127.0.0.1:2376" in text, "the example documents the target default"
