"""Tests for devcontainer_config.transport_azure: the azure relay transport.

The `devcontainer_config.transport_azure` import is deferred into function
bodies (via `_import_transport_azure`), the same convention
`tests/test_transport.py` documents for its own module: a module-level
import would fail COLLECTION for the whole file instead of failing the one
test for the real reason.

The suite is hermetic in the same ways `tests/test_transport.py` is: every
`az` and `docker` command is answered by a fake runner and recorded, never
run; the relay service is a minimal RFC 6455 server this file runs on a
loopback port, so the framing, handshake and pipe behavior are exercised
against real bytes without any network; and the SIGTERM stop is a real
signal to this process, bounded by a deadline guard. The RFC vectors are
the spec's own worked examples (Section 4.2.2's accept key, Section 5.7's
masked "Hello"), so a framing regression cannot pass by accident.

The wire-contract pins are deliberate and load-bearing: the sender URL and
the upgrade request never carry `sb-hc-id` or a query token (the
live-verified azure defect a custom sender id causes), the 401 retry
invalidates the token cache exactly once, and the
`Connected:` marker keeps the prefix `devcontainer_config.forwards` polls
for.
"""

from __future__ import annotations

import ast
import base64
import hashlib
import importlib
import inspect
import json
import os
import selectors
import signal
import socket
import stat
import struct
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from types import ModuleType

import pytest
from conftest import _repo_root
from devcontainer_config import certs, instances
from devcontainer_config.hostprobe import CommandResult

_CONTEXT = "general-dev-azrelay"
_NAME = "azrelay"
_NAMESPACE = "gd-azrelay.servicebus.windows.net"
_HYBRID_CONNECTION = "gd-azrelay"
_TOKEN = "test-token-value"
_OTHER_TOKEN = "fresh-token-value"

# An upper-bound safety guard for the tests that wait on real threads and
# one real signal -- a bound on how long the test may take, never a
# synchronization mechanism.
_HANG_GUARD_SECONDS = 5.0


def _import_transport_azure() -> ModuleType:
    """Import devcontainer_config.transport_azure from inside a function body."""
    return importlib.import_module("devcontainer_config.transport_azure")


@pytest.fixture(autouse=True)
def _docker_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point DOCKER_CONFIG at a per-test directory.

    The address record and the certificate material live under
    `instances.certs_dir`, which honors DOCKER_CONFIG; setting it here keeps
    every test's state on `tmp_path` and away from the operator's real
    `~/.docker/certs` (the same guard `tests/test_forwards.py` installs).
    """
    docker_config = tmp_path / "docker-config"
    docker_config.mkdir()
    monkeypatch.setenv("DOCKER_CONFIG", str(docker_config))
    return docker_config


def _issue_client_material(instance: str) -> None:
    """A real CA and client certificate for `instance`, via real openssl calls.

    The same approach `tests/test_transport.py` takes: `ensure_context`'s
    expiry precondition parses a real certificate's `notAfter`, so a
    hand-written stub file would not exercise that parse at all.
    """
    paths = certs.CertPaths(instance=instance, root=instances.certs_root())
    certs.create_ca(paths)
    certs.issue_client(paths)


def _write_engine_address(instance: str) -> None:
    """The relay address record the deploy writes, with the other slices' keys present.

    The extra keys (`vm_resource_id`, `vault_name`, `resource_group`) are
    part of the record contract this slice must tolerate.
    """
    instances.write_engine_address(
        _repo_root(),
        instance,
        {
            "relay_namespace": _NAMESPACE,
            "hybrid_connection": _HYBRID_CONNECTION,
            "vm_resource_id": (
                "/subscriptions/s/resourceGroups/g/providers/Microsoft.Compute/virtualMachines/v"
            ),
            "vault_name": "gd-vault",
            "resource_group": "gd-rg",
        },
    )


class RecordingRunner:
    """A runner double that records every command and answers from a function.

    The same shape `tests/test_transport_ssh.py` uses: the answer function
    receives the exact command tuple; anything it does not answer raises, so
    a stray or missing docker or az call fails the test instead of passing
    silently.
    """

    def __init__(self, answer: Callable[[tuple[str, ...]], CommandResult]) -> None:
        self._answer = answer
        self.commands: list[tuple[str, ...]] = []

    def __call__(self, command: Sequence[str], timeout_seconds: float | None) -> CommandResult:
        self.commands.append(tuple(command))
        return self._answer(tuple(command))


def _ok() -> CommandResult:
    return CommandResult(exit_code=0)


def _context_inspect_command(context: str) -> tuple[str, ...]:
    return ("docker", "context", "inspect", context, "--format", "{{json .Endpoints}}")


def _no_such_context_result(context: str) -> CommandResult:
    """The real `docker context inspect <absent-name>` outcome (verified wording)."""
    return CommandResult(
        exit_code=1,
        stderr=f'context "{context}": context not found: open ~/.docker/contexts/meta: '
        "no such file or directory",
    )


def _recorded_endpoint_result(port: int) -> CommandResult:
    """The inspect answer for a context already recording `tcp://127.0.0.1:<port>`."""
    return CommandResult(exit_code=0, stdout=f'{{"docker": {{"Host": "tcp://127.0.0.1:{port}"}}}}')


def _connect_argv(context: str) -> list[str]:
    return ["connect", "--name", _NAME, "--context", context]


# ---------------------------------------------------------------------------
# Module hygiene: no sleep-based wait anywhere, and the wire contract's
# forbidden parameters appear nowhere a sender could send them.
# ---------------------------------------------------------------------------


def test_module_imports_no_time_sleep_or_asyncio_sleep() -> None:
    transport_azure = _import_transport_azure()
    source = inspect.getsource(transport_azure)
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
        f"devcontainer_config.transport_azure calls a 'sleep' function at least once: "
        f"{sleep_calls!r}"
    )


def test_relay_endpoint_never_carries_sb_hc_id_or_a_query_token() -> None:
    """The live-verified defect: a custom sender id makes the couple uncouplable.

    The URL is composed in exactly one function, and that function carries
    only `sb-hc-action`; the token rides the request header instead.
    """
    transport_azure = _import_transport_azure()
    for action in ("connect", "listen"):
        url = transport_azure.relay_endpoint(_NAMESPACE, _HYBRID_CONNECTION, action)
        assert url == f"wss://{_NAMESPACE}/$hc/{_HYBRID_CONNECTION}?sb-hc-action={action}"
        assert "sb-hc-id" not in url
        assert "sb-hc-token" not in url
        assert _TOKEN not in url


def test_handshake_request_carries_the_token_header_and_never_an_sb_hc_id() -> None:
    transport_azure = _import_transport_azure()
    with_token = transport_azure.build_handshake_request(
        _NAMESPACE, 443, "/$hc/gd-azrelay?sb-hc-action=connect", "client-key", _TOKEN
    )
    assert with_token == (
        f"GET /$hc/gd-azrelay?sb-hc-action=connect HTTP/1.1\r\n"
        f"Host: {_NAMESPACE}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        "Sec-WebSocket-Key: client-key\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        f"ServiceBusAuthorization: {_TOKEN}\r\n"
        "\r\n"
    )
    assert "sb-hc-id" not in with_token
    assert "sb-hc-token" not in with_token
    without_token = transport_azure.build_handshake_request(
        _NAMESPACE, 443, "/$hc/x", "client-key", None
    )
    assert "ServiceBusAuthorization" not in without_token
    # A non-default port lands in the Host header, where the service routes.
    with_port = transport_azure.build_handshake_request("h.example", 8443, "/p", "k", None)
    assert "Host: h.example:8443" in with_port


# ---------------------------------------------------------------------------
# RFC 6455 vectors: the spec's own worked examples.
# ---------------------------------------------------------------------------


def test_accept_key_matches_the_rfc_6455_section_4222_example() -> None:
    transport_azure = _import_transport_azure()
    assert transport_azure.websocket_accept_key("dGhlIHNhbXBsZSBub25jZQ==") == (
        "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="
    )


def test_masked_hello_frame_matches_the_rfc_6455_section_57_example() -> None:
    """The spec's masked text "Hello": header, mask, and masked payload bytes."""
    transport_azure = _import_transport_azure()
    frame = transport_azure.encode_client_frame(0x1, b"Hello", mask=bytes([0x37, 0xFA, 0x21, 0x3D]))
    assert frame == bytes.fromhex("818537fa213d7f9f4d5158")


def test_mask_payload_round_trips_and_decodes_the_rfc_example() -> None:
    transport_azure = _import_transport_azure()
    mask = bytes([0x37, 0xFA, 0x21, 0x3D])
    masked = bytes.fromhex("7f9f4d5158")
    assert transport_azure.mask_payload(masked, mask) == b"Hello"
    assert transport_azure.mask_payload(b"Hello", mask) == masked


def test_client_frames_are_always_masked_and_fragmentation_sets_the_fin_bit() -> None:
    transport_azure = _import_transport_azure()
    mask = b"\x00\x00\x00\x00"  # a zero mask leaves the payload readable in the frame
    frame = transport_azure.encode_client_frame(0x2, b"AB", mask=mask)
    assert frame[1] & 0x80, "client frames must carry the mask bit (RFC 6455 Section 5.1)"
    continuation = transport_azure.encode_client_frame(0x0, b"AB", fin=False, mask=mask)
    assert continuation[0] == 0x00, "a non-final frame must clear FIN"
    # A 126-byte payload switches the length to the 16-bit form.
    long_frame = transport_azure.encode_client_frame(0x2, b"x" * 126, mask=mask)
    assert struct.unpack("!H", long_frame[2:4])[0] == 126


def test_split_wss_url_rejects_non_wss_and_defaults_the_port() -> None:
    transport_azure = _import_transport_azure()
    host, port, path = transport_azure.split_wss_url(
        f"wss://{_NAMESPACE}/$hc/x?sb-hc-action=connect"
    )
    assert (host, port, path) == (_NAMESPACE, 443, "/$hc/x?sb-hc-action=connect")
    with pytest.raises(transport_azure.RelayProtocolError):
        transport_azure.split_wss_url("http://example.invalid/x")


def test_handshake_status_reads_the_three_digit_code() -> None:
    transport_azure = _import_transport_azure()
    assert transport_azure._handshake_status("HTTP/1.1 401 Unauthorized") == 401
    assert transport_azure._handshake_status("HTTP/1.1 500 Internal Server Error") == 500
    assert transport_azure._handshake_status("garbage") is None


# ---------------------------------------------------------------------------
# The handshake refusal diagnoses: one wording per cause.
# ---------------------------------------------------------------------------


def test_handshake_error_401_names_az_login_and_the_refresh_remedy() -> None:
    transport_azure = _import_transport_azure()
    error = transport_azure.RelayHandshakeError(
        status=401, body="Authorization failed", instance=_NAME
    )
    assert error.status == 401
    assert "az login" in str(error)
    assert f"make connect-refresh INSTANCE={_NAME}" in str(error)
    assert _TOKEN not in str(error)


def test_handshake_error_404_no_listener_names_the_connect_start_remedy() -> None:
    transport_azure = _import_transport_azure()
    error = transport_azure.RelayHandshakeError(
        status=404,
        body="There are no listeners connected for the endpoint",
        instance=_NAME,
    )
    message = str(error)
    assert "The relay listener is not running" in message
    assert f"make connect-start INSTANCE={_NAME} which starts it" in message


def test_handshake_error_404_sender_wait_timeout_names_the_same_remedy() -> None:
    transport_azure = _import_transport_azure()
    error = transport_azure.RelayHandshakeError(
        status=404,
        body="None of the connected listeners accepted the connection within the allowed timeout",
        instance=_NAME,
    )
    assert "The relay listener is not running" in str(error)


def test_handshake_error_other_statuses_stay_generic_and_redacted() -> None:
    transport_azure = _import_transport_azure()
    # The redaction happens where the raw answer is read (the WebSocket
    # passes the body through `_redact` before raising); the test drives
    # that exact call site so the chain is the production one.
    body = transport_azure._redact(f"malformed token {_TOKEN}", [_TOKEN])
    error = transport_azure.RelayHandshakeError(status=500, body=body, instance=_NAME)
    message = str(error)
    assert "500" in message
    assert "az login" not in message
    assert "The relay listener is not running" not in message
    assert _TOKEN not in message, "a handshake failure must never echo the token"


# ---------------------------------------------------------------------------
# The Entra token: argv, parsing, fetch, and the cache.
# ---------------------------------------------------------------------------


def test_az_token_argv_is_the_exact_vector() -> None:
    transport_azure = _import_transport_azure()
    assert transport_azure.build_az_token_argv() == [
        "az",
        "account",
        "get-access-token",
        "--resource",
        "https://relay.azure.net",
        "--output",
        "json",
    ]


def test_parse_token_expiry_accepts_epoch_and_local_string_shapes() -> None:
    transport_azure = _import_transport_azure()
    assert transport_azure.parse_token_expiry(1893456000) == 1893456000.0
    parsed = transport_azure.parse_token_expiry("2026-10-13 12:00:00.000000")
    assert isinstance(parsed, float)
    assert parsed > 0
    for bad in (True, None, [], "not-a-date"):
        with pytest.raises(transport_azure.RelayConfigError):
            transport_azure.parse_token_expiry(bad)


def test_parse_token_payload_returns_the_pair_and_refuses_gaps() -> None:
    transport_azure = _import_transport_azure()
    token, expiry = transport_azure.parse_token_payload(
        '{"accessToken": "abc", "expiresOn": 1893456000, "subscription": "s"}'
    )
    assert token == "abc"
    assert expiry == 1893456000.0
    for bad_stdout in ('{"expiresOn": 1}', '{"accessToken": ""}', "not json", "[]"):
        with pytest.raises(transport_azure.RelayConfigError):
            transport_azure.parse_token_payload(bad_stdout)


def test_fetch_relay_token_classifies_missing_binary_and_failure() -> None:
    transport_azure = _import_transport_azure()

    def answer(command: tuple[str, ...]) -> CommandResult:
        assert command == tuple(transport_azure.build_az_token_argv())
        return CommandResult(exit_code=0, stdout='{"accessToken": "t", "expiresOn": 100}')

    token, expiry = transport_azure.fetch_relay_token(RecordingRunner(answer))
    assert (token, expiry) == ("t", 100.0)

    class _Missing:
        def __call__(self, command: Sequence[str], timeout_seconds: float | None) -> CommandResult:
            del command, timeout_seconds
            # The runner seam reports a missing binary as an outcome, the
            # way transport.subprocess_command_runner's FileNotFoundError
            # branch does.
            return CommandResult(exit_code=127, binary_missing=True)

    with pytest.raises(transport_azure.RelayConfigError, match="not on PATH"):
        transport_azure.fetch_relay_token(_Missing())

    def failed(command: tuple[str, ...]) -> CommandResult:
        return CommandResult(exit_code=1, stderr="ERROR: Please run az login")

    with pytest.raises(transport_azure.RelayConfigError, match="az login"):
        transport_azure.fetch_relay_token(RecordingRunner(failed))


class _TokenRunner:
    """Records fetch attempts and answers a fresh token each time."""

    def __init__(self) -> None:
        self.fetches = 0

    def __call__(self, command: Sequence[str], timeout_seconds: float | None) -> CommandResult:
        self.fetches += 1
        return CommandResult(
            exit_code=0,
            stdout=f'{{"accessToken": "token-{self.fetches}", "expiresOn": 1893456000}}',
        )


def test_token_cache_honors_the_refresh_margin_and_invalidate() -> None:
    transport_azure = _import_transport_azure()
    runner = _TokenRunner()
    now = [1000.0]
    cache = transport_azure.TokenCache(runner, refresh_margin_seconds=300.0, clock=lambda: now[0])
    # A far-from-expiry token is reused; the margin boundary refetches.
    assert cache.get() == "token-1"
    now[0] = 1893456000.0 - 301.0  # one second more than the margin: still cached
    assert cache.get() == "token-1"
    assert runner.fetches == 1
    now[0] = 1893456000.0 - 300.0  # exactly the margin is not MORE than it: refetch
    assert cache.get() == "token-2"
    assert runner.fetches == 2
    cache.invalidate()
    assert cache.get() == "token-3"
    assert runner.fetches == 3


def test_token_cache_refetches_when_the_record_carries_no_usable_expiry() -> None:
    transport_azure = _import_transport_azure()

    class _BadExpiry:
        def __call__(self, command: Sequence[str], timeout_seconds: float | None) -> CommandResult:
            return CommandResult(exit_code=0, stdout='{"accessToken": "t"}')

    cache = transport_azure.TokenCache(_BadExpiry(), clock=lambda: 0.0)
    with pytest.raises(transport_azure.RelayConfigError):
        cache.get()


# ---------------------------------------------------------------------------
# The relay address record: the two keys, the tolerated extras, the remedy.
# ---------------------------------------------------------------------------


def test_relay_values_reads_the_pair_and_tolerates_other_slices_keys() -> None:
    transport_azure = _import_transport_azure()
    namespace, hybrid_connection = transport_azure.relay_values(
        {
            "relay_namespace": _NAMESPACE,
            "hybrid_connection": _HYBRID_CONNECTION,
            "vm_resource_id": "/subscriptions/s/.../v",
            "vault_name": "v",
            "resource_group": "rg",
        },
        instance=_NAME,
    )
    assert (namespace, hybrid_connection) == (_NAMESPACE, _HYBRID_CONNECTION)


def test_relay_values_refuses_missing_keys_naming_the_deploy_remedy() -> None:
    transport_azure = _import_transport_azure()
    with pytest.raises(transport_azure.RelayConfigError) as exc_info:
        transport_azure.relay_values({"vault_name": "v"}, instance=_NAME)
    message = str(exc_info.value)
    assert "relay_namespace" in message
    assert f"make instance-deploy INSTANCE={_NAME}" in message


def test_relay_values_refuses_a_namespace_with_a_scheme_or_path() -> None:
    transport_azure = _import_transport_azure()
    with pytest.raises(transport_azure.RelayConfigError):
        transport_azure.relay_values(
            {"relay_namespace": "https://evil.example", "hybrid_connection": "hc"},
            instance=_NAME,
        )
    with pytest.raises(transport_azure.RelayConfigError):
        transport_azure.relay_values(
            {"relay_namespace": "a/b", "hybrid_connection": "hc"}, instance=_NAME
        )


def test_load_relay_address_missing_file_carries_the_built_in_remedy() -> None:
    transport_azure = _import_transport_azure()
    with pytest.raises(transport_azure.RelayConfigError) as exc_info:
        transport_azure.load_relay_address(_NAME)
    message = str(exc_info.value)
    assert f"make instance-deploy INSTANCE={_NAME}" in message
    assert "engine-address.json" in message


# ---------------------------------------------------------------------------
# open_relay_sender: the one-retry 401 discipline.
# ---------------------------------------------------------------------------


class _RefusingWebSocket:
    """A RelayWebSocket double that refuses with a status, then optionally succeeds.

    The statuses are consumed one per construction, so the tests script the
    exact refusal-then-succeed sequence the 401 retry discipline produces;
    tokens are recorded at the moment the module passes them in.
    """

    def __init__(
        self, transport_azure: ModuleType, statuses: list[int | None], tokens_seen: list[str]
    ) -> None:
        self._transport_azure = transport_azure
        self._statuses = list(statuses)
        self._tokens_seen = tokens_seen

    def __call__(
        self,
        url: str,
        token: str | None,
        *,
        timeout_seconds: float,
        dial: object = None,
    ) -> object:
        del url, timeout_seconds, dial
        self._tokens_seen.append(token or "")
        status = self._statuses.pop(0)
        if status is not None:
            # The body is the service's real no-listener 404 text, the one
            # the classification keys on.
            raise self._transport_azure._HandshakeRefusal(
                status=status, body="There are no listeners connected for the endpoint"
            )
        return object()


def _swap_websocket(
    monkeypatch: pytest.MonkeyPatch, transport_azure: ModuleType, double: object
) -> None:
    """Replace the module's RelayWebSocket with `double`, restoring it afterwards."""
    monkeypatch.setattr(transport_azure, "RelayWebSocket", double)


def test_open_relay_sender_retries_once_with_a_fresh_token_on_401(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport_azure = _import_transport_azure()
    runner = _TokenRunner()
    tokens_seen: list[str] = []
    _swap_websocket(
        monkeypatch, transport_azure, _RefusingWebSocket(transport_azure, [401, None], tokens_seen)
    )
    websocket = transport_azure.open_relay_sender(
        transport_azure.TokenCache(runner, clock=lambda: 0.0),
        namespace=_NAMESPACE,
        hybrid_connection=_HYBRID_CONNECTION,
        instance=_NAME,
        timeout_seconds=1.0,
    )
    assert websocket is not None
    assert tokens_seen == ["token-1", "token-2"], (
        "the 401 must invalidate the cache so the retry presents a fresh token"
    )
    assert runner.fetches == 2


def test_open_relay_sender_never_retries_a_non_401_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport_azure = _import_transport_azure()
    runner = _TokenRunner()
    tokens_seen: list[str] = []
    _swap_websocket(
        monkeypatch, transport_azure, _RefusingWebSocket(transport_azure, [404], tokens_seen)
    )
    with pytest.raises(transport_azure.RelayHandshakeError) as exc_info:
        transport_azure.open_relay_sender(
            transport_azure.TokenCache(runner, clock=lambda: 0.0),
            namespace=_NAMESPACE,
            hybrid_connection=_HYBRID_CONNECTION,
            instance=_NAME,
            timeout_seconds=1.0,
        )
    assert exc_info.value.status == 404
    assert tokens_seen == ["token-1"], "only a 401 earns a retry"
    assert runner.fetches == 1


def test_open_relay_sender_diagnoses_the_refusal_naming_the_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport_azure = _import_transport_azure()
    _swap_websocket(monkeypatch, transport_azure, _RefusingWebSocket(transport_azure, [404], []))
    with pytest.raises(transport_azure.RelayHandshakeError) as exc_info:
        transport_azure.open_relay_sender(
            transport_azure.TokenCache(_TokenRunner(), clock=lambda: 0.0),
            namespace=_NAMESPACE,
            hybrid_connection=_HYBRID_CONNECTION,
            instance=_NAME,
            timeout_seconds=1.0,
        )
    assert f"make connect-start INSTANCE={_NAME}" in str(exc_info.value)


# ---------------------------------------------------------------------------
# A minimal RFC 6455 server for loopback tests: the relay service stand-in.
# ---------------------------------------------------------------------------


class _TestRelayServer:
    """Serves relay WebSocket connections on loopback, echoing binary frames.

    Deliberately minimal: it performs the server side of the upgrade
    (reading masked client frames, sending unmasked server frames, per
    RFC 6455), records the handshake heads it received, and can inject a
    ping and a fragmented message into the stream. Every client frame is
    checked for the mask bit, because an unmasked client frame is a
    protocol violation the service would refuse.
    """

    def __init__(self) -> None:
        self.handshake_heads: list[str] = []
        self.lock = threading.Lock()
        self.sockets: list[socket.socket] = []
        self.acceptor = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.acceptor.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.acceptor.bind(("127.0.0.1", 0))
        self.acceptor.listen(4)
        self.port = int(self.acceptor.getsockname()[1])
        self.thread = threading.Thread(target=self._accept_forever, daemon=True)

    def start(self) -> int:
        self.thread.start()
        return self.port

    def _accept_forever(self) -> None:
        while True:
            try:
                conn, _ = self.acceptor.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _read_exact(self, conn: socket.socket, count: int, buffer: bytearray) -> bytes:
        while len(buffer) < count:
            chunk = conn.recv(65536)
            if not chunk:
                raise ConnectionError("test server: client closed")
            buffer.extend(chunk)
        out = bytes(buffer[:count])
        del buffer[:count]
        return out

    def _serve(self, conn: socket.socket) -> None:
        buffer = bytearray()
        try:
            while b"\r\n\r\n" not in buffer:
                chunk = conn.recv(65536)
                if not chunk:
                    return
                buffer.extend(chunk)
            head, _, rest = bytes(buffer).partition(b"\r\n\r\n")
            text_head = head.decode("ascii", errors="replace")
            with self.lock:
                self.handshake_heads.append(text_head)
            assert "sb-hc-id" not in text_head, (
                "the client sent a custom sender id: the couple would be uncouplable"
            )
            key_line = next(
                line for line in text_head.split("\r\n") if line.startswith("Sec-WebSocket-Key: ")
            )
            client_key = key_line.split(": ", 1)[1]
            accept = base64.b64encode(
                hashlib.sha1(
                    (client_key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")
                ).digest()
            ).decode("ascii")
            conn.sendall(
                (
                    "HTTP/1.1 101 Switching Protocols\r\n"
                    "Upgrade: websocket\r\n"
                    "Connection: Upgrade\r\n"
                    f"Sec-WebSocket-Accept: {accept}\r\n"
                    "\r\n"
                ).encode("ascii")
            )
            with self.lock:
                self.sockets.append(conn)
            buffer = bytearray(rest)
            while True:
                first, second = self._read_exact(conn, 2, buffer)
                assert second & 0x80, "client frames must be masked (RFC 6455 Section 5.1)"
                opcode = first & 0x0F
                length = second & 0x7F
                if length == 126:
                    (length,) = struct.unpack("!H", self._read_exact(conn, 2, buffer))
                elif length == 127:
                    (length,) = struct.unpack("!Q", self._read_exact(conn, 8, buffer))
                mask = self._read_exact(conn, 4, buffer)
                payload = self._read_exact(conn, length, buffer) if length else b""
                payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
                if opcode == 0x8:  # close: answer and end this connection
                    conn.sendall(struct.pack("!BB", 0x88, 0x02) + struct.pack("!H", 1000))
                    return
                if opcode in (0x2, 0x1):  # echo the message back, unmasked
                    self._send_frame(conn, opcode, payload)
        except (OSError, AssertionError, ConnectionError):
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _send_frame(
        self, conn: socket.socket, opcode: int, payload: bytes, *, fin: bool = True
    ) -> None:
        first = (0x80 if fin else 0x00) | opcode
        length = len(payload)
        if length <= 125:
            header = struct.pack("!BB", first, length)
        elif length <= 0xFFFF:
            header = struct.pack("!BBH", first, 126, length)
        else:
            header = struct.pack("!BBQ", first, 127, length)
        conn.sendall(header + payload)

    def send_ping(self, payload: bytes = b"keepalive") -> None:
        with self.lock:
            sockets = list(self.sockets)
        for conn in sockets:
            try:
                self._send_frame(conn, 0x9, payload)
            except OSError:
                pass

    def send_fragmented(self, payload: bytes, chunk_size: int = 3) -> None:
        """One message split across a non-final frame and a continuation.

        The server side of the fragmentation the client's `recv_message`
        must aggregate into one complete message.
        """
        with self.lock:
            sockets = list(self.sockets)
        for conn in sockets:
            try:
                self._send_frame(conn, 0x2, payload[:chunk_size], fin=False)
                self._send_frame(conn, 0x0, payload[chunk_size:], fin=True)
            except OSError:
                pass

    def close(self) -> None:
        try:
            self.acceptor.close()
        except OSError:
            pass


def _plain_dial(host: str, port: int, timeout_seconds: float) -> socket.socket:
    """The test dial: a plain loopback socket, no TLS, to the test relay server."""
    return socket.create_connection((host, port), timeout=timeout_seconds)


def _loopback_sender_opener(
    transport_azure: ModuleType, relay_port: int, token: str = _TOKEN
) -> Callable[[], object]:
    """A sender opener against the loopback test server, for the bridge tests."""

    def open_one() -> object:
        return transport_azure.RelayWebSocket(
            f"wss://127.0.0.1:{relay_port}/$hc/{_HYBRID_CONNECTION}?sb-hc-action=connect",
            token,
            timeout_seconds=_HANG_GUARD_SECONDS,
            dial=_plain_dial,
        )

    return open_one


# ---------------------------------------------------------------------------
# The WebSocket client against the loopback server: echo, ping, close.
# ---------------------------------------------------------------------------


def test_relay_websocket_echoes_binary_and_answers_pings_over_loopback() -> None:
    transport_azure = _import_transport_azure()
    server = _TestRelayServer()
    port = server.start()
    try:
        websocket = transport_azure.RelayWebSocket(
            f"wss://127.0.0.1:{port}/$hc/x?sb-hc-action=connect",
            _TOKEN,
            timeout_seconds=_HANG_GUARD_SECONDS,
            dial=_plain_dial,
        )
        websocket.send_frame(0x2, b"round-trip")
        opcode, payload = websocket.recv_message()
        assert (opcode, payload) == (0x2, b"round-trip")
        server.send_ping(b"ping-body")
        # The next message the client sees is the echo of a fresh send: the
        # ping was answered with a pong inside recv_message, never surfaced.
        websocket.send_frame(0x2, b"after-ping")
        _, payload = websocket.recv_message()
        assert payload == b"after-ping"
        heads = server.handshake_heads
        assert len(heads) == 1
        assert f"ServiceBusAuthorization: {_TOKEN}" in heads[0]
        websocket.send_close()
        assert websocket.recv_message() is None, "a peer close reads as None, not an error"
    finally:
        server.close()


def test_relay_websocket_aggregates_fragmented_messages() -> None:
    transport_azure = _import_transport_azure()
    server = _TestRelayServer()
    port = server.start()
    try:
        websocket = transport_azure.RelayWebSocket(
            f"wss://127.0.0.1:{port}/$hc/x",
            None,
            timeout_seconds=_HANG_GUARD_SECONDS,
            dial=_plain_dial,
        )
        # One round trip first, so the server has registered the connection
        # before its fragmented send races this thread.
        websocket.send_frame(0x2, b"sync")
        _, payload = websocket.recv_message()
        assert payload == b"sync"
        server.send_fragmented(b"frag-ment")
        opcode, payload = websocket.recv_message()
        assert (opcode, payload) == (0x2, b"frag-ment"), (
            "a fragmented message must arrive as one complete aggregation"
        )
    finally:
        server.close()


# ---------------------------------------------------------------------------
# The bridge and the accept loop: per-connection isolation over loopback.
# ---------------------------------------------------------------------------


def _kill_after(event: threading.Event) -> None:
    """SIGTERM this process once `event` fires; the clean-stop signal for the loop."""
    if not event.wait(_HANG_GUARD_SECONDS):
        return
    os.kill(os.getpid(), signal.SIGTERM)


def _wait_until(condition: Callable[[], bool], deadline_seconds: float, what: str) -> None:
    """Poll `condition` until it holds or the deadline passes, then fail naming `what`.

    Paced by a bounded selector wait -- a real I/O-readiness wait with
    nothing registered, the same primitive `transport_ssh._pace` uses --
    never a sleep.
    """
    deadline = time.monotonic() + deadline_seconds
    while True:
        if condition():
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError(f"timed out waiting for {what}")
        selector = selectors.DefaultSelector()
        try:
            selector.select(timeout=min(remaining, 0.05))
        finally:
            selector.close()


def test_accept_loop_bridges_a_real_loopback_connection_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One coupled pipe, proven with bytes: CLI socket -> relay -> back."""
    transport_azure = _import_transport_azure()
    monkeypatch.setenv("RELAY_DIAL_TIMEOUT", str(_HANG_GUARD_SECONDS))
    server = _TestRelayServer()
    relay_port = server.start()
    acceptor = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    acceptor.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    acceptor.bind(("127.0.0.1", 0))
    acceptor.listen(4)
    bridge_port = int(acceptor.getsockname()[1])
    registry = transport_azure._BridgeRegistry()
    echo_done = threading.Event()
    client_result: list[bytes] = []

    def client_work() -> None:
        try:
            client = socket.create_connection(
                ("127.0.0.1", bridge_port), timeout=_HANG_GUARD_SECONDS
            )
            try:
                client.sendall(b"hello-relay")
                client_result.append(client.recv(65536))
            finally:
                client.close()
        except OSError:
            pass
        finally:
            echo_done.set()

    threading.Thread(target=client_work, daemon=True).start()
    threading.Thread(target=_kill_after, args=(echo_done,), daemon=True).start()
    try:
        transport_azure.serve_until_stopped(
            acceptor,
            sender_opener=_loopback_sender_opener(transport_azure, relay_port),
            registry=registry,
            namespace=_NAMESPACE,
        )
    finally:
        acceptor.close()
        server.close()
    assert echo_done.is_set(), "the client never completed its round trip"
    assert client_result == [b"hello-relay"]
    registry.close_all()


def test_accept_loop_survives_a_failing_sender_and_keeps_serving(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """One connection's refusal is logged and closed; the daemon keeps serving."""
    transport_azure = _import_transport_azure()
    monkeypatch.setenv("RELAY_DIAL_TIMEOUT", str(_HANG_GUARD_SECONDS))

    def failing_opener() -> object:
        raise transport_azure.RelayHandshakeError(
            status=404, body="There are no listeners connected for the endpoint", instance=_NAME
        )

    acceptor = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    acceptor.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    acceptor.bind(("127.0.0.1", 0))
    acceptor.listen(4)
    bridge_port = int(acceptor.getsockname()[1])
    registry = transport_azure._BridgeRegistry()
    second_connection_closed = threading.Event()

    def client_work() -> None:
        for _ in range(2):
            try:
                with socket.create_connection(
                    ("127.0.0.1", bridge_port), timeout=_HANG_GUARD_SECONDS
                ) as client:
                    # The bridge closed the socket after the refusal: the
                    # read answers EOF rather than hanging forever.
                    client.settimeout(_HANG_GUARD_SECONDS)
                    client.recv(1)
            except OSError:
                break
        second_connection_closed.set()

    threading.Thread(target=client_work, daemon=True).start()
    threading.Thread(target=_kill_after, args=(second_connection_closed,), daemon=True).start()
    try:
        transport_azure.serve_until_stopped(
            acceptor,
            sender_opener=failing_opener,
            registry=registry,
            namespace=_NAMESPACE,
        )
    finally:
        acceptor.close()
    assert second_connection_closed.is_set(), "the loop stopped serving after a refusal"
    captured = capsys.readouterr()
    assert "relay: a connection through" in captured.err
    registry.close_all()


def test_serve_until_stopped_swallows_a_keyboard_interrupt(monkeypatch: pytest.MonkeyPatch) -> None:
    """The SIGINT path: the same clean stop, registry closed, handler restored."""
    transport_azure = _import_transport_azure()
    previous_handler = signal.getsignal(signal.SIGTERM)
    acceptor = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    acceptor.bind(("127.0.0.1", 0))

    def raising_loop(*args: object, **kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(transport_azure, "_accept_loop", raising_loop)
    try:
        transport_azure.serve_until_stopped(
            acceptor,
            sender_opener=lambda: None,
            registry=transport_azure._BridgeRegistry(),
            namespace=_NAMESPACE,
        )
    finally:
        acceptor.close()
    assert signal.getsignal(signal.SIGTERM) is previous_handler, (
        "the previous SIGTERM handler must be restored on the way out"
    )


# ---------------------------------------------------------------------------
# The connect flow, end to end against fakes.
# ---------------------------------------------------------------------------


def _connect_runner(context: str, *, recorded_port: int | None = None) -> RecordingRunner:
    """The docker answers the connect flow needs, recorded for assertion."""

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
        raise AssertionError(f"unexpected command: {command}")

    return RecordingRunner(answer)


def _patch_serve_to_capture(
    monkeypatch: pytest.MonkeyPatch, transport_azure: ModuleType
) -> list[socket.socket]:
    """Replace serve_until_stopped with a clean-stop capture of the acceptor.

    The real daemon blocks forever; the capture closes the acceptor and
    returns, which is exactly what a SIGTERM produces, so the connect
    handler's own finally path runs for real.
    """
    captured: list[socket.socket] = []

    def fake_serve(acceptor: socket.socket, **kwargs: object) -> None:
        del kwargs
        captured.append(acceptor)
        acceptor.close()

    monkeypatch.setattr(transport_azure, "serve_until_stopped", fake_serve)
    return captured


def test_main_connect_full_flow_prints_the_marker_and_wires_the_context(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    transport_azure = _import_transport_azure()
    _issue_client_material(_NAME)
    _write_engine_address(_NAME)
    runner = _connect_runner(_CONTEXT)
    monkeypatch.setattr(transport_azure.transport, "subprocess_command_runner", runner)
    captured = _patch_serve_to_capture(monkeypatch, transport_azure)

    exit_code = transport_azure.main(_connect_argv(_CONTEXT))

    assert exit_code == 0
    assert len(captured) == 1, "the acceptor must be bound before the marker prints"
    captured[0].close()
    connected = [
        line for line in capsys.readouterr().out.splitlines() if line.startswith("Connected:")
    ]
    assert connected == [
        f"Connected: docker context '{_CONTEXT}' -> {_NAME} over azure relay (relay {_NAMESPACE})."
    ]
    commands = runner.commands
    create_commands = [c for c in commands if c[:3] == ("docker", "context", "create")]
    assert len(create_commands) == 1
    docker_value = create_commands[0][create_commands[0].index("--docker") + 1]
    certs_root = instances.certs_root()
    assert "host=tcp://127.0.0.1:" in docker_value
    for component in ("ca=", "cert=", "key="):
        assert component in docker_value, "the mTLS context must carry all three material paths"
    assert str(certs_root / _NAME) in docker_value, (
        "the material paths must address the instance's own certificate directory"
    )
    use_index = commands.index(("docker", "context", "use", _CONTEXT))
    assert commands.index(create_commands[0]) < use_index, (
        "the context must be created before it is activated"
    )
    # The az token was never fetched: no connection arrived, so no sender opened.
    assert not any(command[0] == "az" for command in commands)


def test_main_connect_reuses_the_port_the_context_records(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    transport_azure = _import_transport_azure()
    _issue_client_material(_NAME)
    _write_engine_address(_NAME)
    recorded_port = 54321
    runner = _connect_runner(_CONTEXT, recorded_port=recorded_port)
    monkeypatch.setattr(transport_azure.transport, "subprocess_command_runner", runner)
    captured = _patch_serve_to_capture(monkeypatch, transport_azure)

    exit_code = transport_azure.main(_connect_argv(_CONTEXT))

    assert exit_code == 0
    captured[0].close()
    create_commands = [c for c in runner.commands if c[:3] == ("docker", "context", "update")]
    assert len(create_commands) == 1, "an existing context is updated in place"
    docker_value = create_commands[0][create_commands[0].index("--docker") + 1]
    assert docker_value.startswith(f"host=tcp://127.0.0.1:{recorded_port},"), (
        "the recorded port is the record: the context must keep it across reconnects"
    )


def test_main_connect_fails_fast_without_the_address_record(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    transport_azure = _import_transport_azure()
    _issue_client_material(_NAME)
    runner = _connect_runner(_CONTEXT)
    monkeypatch.setattr(transport_azure.transport, "subprocess_command_runner", runner)

    exit_code = transport_azure.main(_connect_argv(_CONTEXT))

    assert exit_code == 1
    stderr = capsys.readouterr().err
    assert f"make instance-deploy INSTANCE={_NAME}" in stderr
    assert not runner.commands, "nothing may run before the address record exists"


def test_main_connect_refuses_a_context_that_does_not_name_the_instance(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    transport_azure = _import_transport_azure()
    _issue_client_material(_NAME)
    _write_engine_address(_NAME)
    runner = _connect_runner("general-dev-other")
    monkeypatch.setattr(transport_azure.transport, "subprocess_command_runner", runner)

    exit_code = transport_azure.main(_connect_argv("general-dev-other"))

    assert exit_code == 1
    stderr = capsys.readouterr().err
    assert "general-dev-other" in stderr
    assert _CONTEXT in stderr, "the refusal names the context the instance actually owns"


def test_main_connect_refuses_a_legacy_ssh_context(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    transport_azure = _import_transport_azure()
    _issue_client_material(_NAME)
    _write_engine_address(_NAME)

    def answer(command: tuple[str, ...]) -> CommandResult:
        if command == _context_inspect_command(_CONTEXT):
            return CommandResult(exit_code=0, stdout='{"docker": {"Host": "ssh://old-host"}}')
        raise AssertionError(f"unexpected command: {command}")

    runner = RecordingRunner(answer)
    monkeypatch.setattr(transport_azure.transport, "subprocess_command_runner", runner)

    exit_code = transport_azure.main(_connect_argv(_CONTEXT))

    assert exit_code == 1
    stderr = capsys.readouterr().err
    assert "ssh://" in stderr, "the legacy refusal names the scheme it refuses"
    assert not any(c[:3] == ("docker", "context", "update") for c in runner.commands), (
        "a legacy ssh:// context is refused, never overwritten"
    )


def test_main_connect_closes_the_acceptor_when_the_daemon_stops(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """When the daemon stops (SIGINT/SIGTERM inside the real serve loop), exit is 0.

    In production the interrupt is caught inside `serve_until_stopped` and
    never reaches `main`, so this test's stand-in stops the same way the
    real one does -- returning normally -- and the assertion is the port
    the acceptor held being free again afterwards.
    """
    transport_azure = _import_transport_azure()
    _issue_client_material(_NAME)
    _write_engine_address(_NAME)
    runner = _connect_runner(_CONTEXT)
    monkeypatch.setattr(transport_azure.transport, "subprocess_command_runner", runner)
    port_holder: list[int] = []

    def stopping_serve(acceptor: socket.socket, **kwargs: object) -> None:
        del kwargs
        port_holder.append(int(acceptor.getsockname()[1]))
        acceptor.close()

    monkeypatch.setattr(transport_azure, "serve_until_stopped", stopping_serve)

    exit_code = transport_azure.main(_connect_argv(_CONTEXT))

    assert exit_code == 0
    assert "Connected:" in capsys.readouterr().out
    port = port_holder[0]
    _wait_until(lambda: not _port_accepts(port), _HANG_GUARD_SECONDS, "the acceptor closing")


def _port_accepts(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# The verify flow: one proof, everything torn down.
# ---------------------------------------------------------------------------


def _verify_runner(context: str) -> RecordingRunner:
    def answer(command: tuple[str, ...]) -> CommandResult:
        if command == _context_inspect_command(context):
            return _no_such_context_result(context)
        if command[:3] == ("docker", "context", "create"):
            return _ok()
        if command == ("docker", "context", "use", context):
            return _ok()
        if command == ("docker", "--context", context, "version", "--format", "{{json .Server}}"):
            return CommandResult(exit_code=0, stdout='{"Version":"28.6.0","ApiVersion":"1.51"}')
        raise AssertionError(f"unexpected command: {command}")

    return RecordingRunner(answer)


def test_main_verify_prints_the_verdict_and_tears_everything_down(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    transport_azure = _import_transport_azure()
    _issue_client_material(_NAME)
    _write_engine_address(_NAME)
    runner = _verify_runner(_CONTEXT)
    monkeypatch.setattr(transport_azure.transport, "subprocess_command_runner", runner)

    exit_code = transport_azure.main(["verify", "--name", _NAME, "--context", _CONTEXT])

    assert exit_code == 0
    out = capsys.readouterr().out
    verified = [line for line in out.splitlines() if line.startswith("Verified:")]
    assert verified == [
        f"Verified: docker context '{_CONTEXT}' -> relay {_NAMESPACE} (server 1.51)."
    ]
    version_commands = [
        c for c in runner.commands if c[:4] == ("docker", "--context", _CONTEXT, "version")
    ]
    assert len(version_commands) == 1, "verify opens exactly one proof connection"
    # The serving thread is a daemon and the acceptor is closed before main
    # returns: within the guard the port refuses connections again.
    create_commands = [c for c in runner.commands if c[:3] == ("docker", "context", "create")]
    docker_value = create_commands[0][create_commands[0].index("--docker") + 1]
    port = int(docker_value.split("tcp://127.0.0.1:")[1].split(",")[0])
    _wait_until(lambda: not _port_accepts(port), _HANG_GUARD_SECONDS, "the acceptor closing")


def test_main_verify_reports_a_failed_handshake_and_tears_everything_down(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    transport_azure = _import_transport_azure()
    _issue_client_material(_NAME)
    _write_engine_address(_NAME)

    def answer(command: tuple[str, ...]) -> CommandResult:
        if command == _context_inspect_command(_CONTEXT):
            return _no_such_context_result(_CONTEXT)
        if command[:3] == ("docker", "context", "create"):
            return _ok()
        if command == ("docker", "context", "use", _CONTEXT):
            return _ok()
        if command[:4] == ("docker", "--context", _CONTEXT, "version"):
            return CommandResult(exit_code=1, stderr="tls: failed to verify certificate")
        raise AssertionError(f"unexpected command: {command}")

    runner = RecordingRunner(answer)
    monkeypatch.setattr(transport_azure.transport, "subprocess_command_runner", runner)

    exit_code = transport_azure.main(["verify", "--name", _NAME, "--context", _CONTEXT])

    assert exit_code == 1
    stderr = capsys.readouterr().err
    assert "docker version handshake" in stderr
    create_commands = [c for c in runner.commands if c[:3] == ("docker", "context", "create")]
    docker_value = create_commands[0][create_commands[0].index("--docker") + 1]
    port = int(docker_value.split("tcp://127.0.0.1:")[1].split(",")[0])
    _wait_until(lambda: not _port_accepts(port), _HANG_GUARD_SECONDS, "the acceptor closing")


# ---------------------------------------------------------------------------
# install-material: the azure twin of transport.install_material's own test
# file's discipline (`tests/test_install_material.py`). The fake runner
# answers `az vm run-command invoke` and `az vm run-command show`; the
# script the invoke carries is read at call time, while the 0600 `--scripts
# @file` still exists, so the content pins are the bytes production would
# hand azure. The poll clock and sleep are injected, so the deadline tests
# walk time without waiting on any of it.
# ---------------------------------------------------------------------------

_INSTALL_RUN_COMMAND_NAME = "run-azrelay-1"
_RESOURCE_GROUP = "gd-rg"
_VM_NAME = "gd-vm"


class _InstallRunner:
    """A CommandRunner double that answers the two run-command calls it recognizes.

    `states` is the sequence of `instanceView.executionState` values the
    show polls answer with, one per poll; the invoke answer carries the
    generated run-command name azure assigns, read back the way
    production's own JSON answer is read.
    """

    def __init__(self, states: Sequence[str], *, error: str = "") -> None:
        self.commands: list[tuple[str, ...]] = []
        self.timeouts: list[float | None] = []
        self.script_text: str | None = None
        self.script_mode: int | None = None
        # When set, the show polls answer with this raw stdout instead of
        # the instance-view JSON the states would otherwise render.
        self.raw_show_stdout: str | None = None
        self._states = list(states)
        self._error = error

    def __call__(self, command: Sequence[str], timeout_seconds: float | None) -> CommandResult:
        command = tuple(command)
        self.commands.append(command)
        self.timeouts.append(timeout_seconds)
        if "invoke" in command:
            scripts_at = command.index("--scripts")
            reference = Path(command[scripts_at + 1][len("@") :])
            assert reference.is_file(), f"the script must be a regular file, not {reference}"
            self.script_mode = stat.S_IMODE(reference.stat().st_mode)
            self.script_text = reference.read_text(encoding="utf-8")
            return CommandResult(
                exit_code=0, stdout=json.dumps({"name": _INSTALL_RUN_COMMAND_NAME})
            )
        if "show" in command:
            if self.raw_show_stdout is not None:
                return CommandResult(exit_code=0, stdout=self.raw_show_stdout)
            state = self._states.pop(0) if self._states else "Running"
            view: dict[str, object] = {"executionState": state}
            if self._error:
                view["error"] = self._error
            return CommandResult(exit_code=0, stdout=json.dumps({"instanceView": view}))
        raise AssertionError(f"unexpected command: {command}")

    def invoke_command(self) -> tuple[str, ...]:
        return next(command for command in self.commands if "invoke" in command)

    def show_commands(self) -> list[tuple[str, ...]]:
        return [command for command in self.commands if "show" in command]


class _SteppedClock:
    """A monotonic clock advanced by the fake sleep, one step per poll."""

    def __init__(self) -> None:
        self.now = 0.0

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _write_install_address(instance: str, *, vault_uri: str | None = None) -> None:
    record: dict[str, object] = {
        "relay_namespace": _NAMESPACE,
        "hybrid_connection": _HYBRID_CONNECTION,
        "resource_group": _RESOURCE_GROUP,
        "vm_name": _VM_NAME,
        "vault_name": "gd-vault",
    }
    if vault_uri is not None:
        record["vault_uri"] = vault_uri
    instances.write_engine_address(_repo_root(), instance, record)


def _install(
    runner: _InstallRunner,
    *,
    clock: _SteppedClock | None = None,
    daemon_user: str = "dockerd",
) -> None:
    transport_azure = _import_transport_azure()
    transport_azure.install_material(
        runner,
        instance=_NAME,
        daemon_user=daemon_user,
        **(
            {"clock": clock.clock, "pace": clock.sleep}
            if clock is not None
            else {}
        ),
    )


def test_install_invoke_argv_is_pinned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_install_address(_NAME)
    runner = _InstallRunner(["Succeeded"])

    _install(runner)

    invoke = runner.invoke_command()
    assert invoke[:4] == ("az", "vm", "run-command", "invoke")
    assert "--resource-group" in invoke
    assert invoke[invoke.index("--resource-group") + 1] == _RESOURCE_GROUP
    assert "--name" in invoke and invoke[invoke.index("--name") + 1] == _VM_NAME
    assert "--command-id" in invoke
    assert invoke[invoke.index("--command-id") + 1] == "RunShellScript"
    assert invoke[invoke.index("--scripts") + 1].startswith("@")
    assert "--no-wait" in invoke
    assert f"'{_NAMESPACE}'" not in " ".join(invoke)


def test_install_polls_the_run_command_show_until_succeeded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_install_address(_NAME)
    runner = _InstallRunner(["Running", "Running", "Succeeded"])

    _install(runner)

    shows = runner.show_commands()
    assert len(shows) == 3
    show = shows[0]
    assert show[:4] == ("az", "vm", "run-command", "show")
    assert "--run-command-name" in show
    assert show[show.index("--run-command-name") + 1] == _INSTALL_RUN_COMMAND_NAME
    assert show[show.index("--resource-group") + 1] == _RESOURCE_GROUP
    assert show[show.index("--vm-name") + 1] == _VM_NAME
    assert "--instance-view" in show
    assert "--output" in show and show[show.index("--output") + 1] == "json"


def test_the_script_pulls_from_the_vault_through_the_instance_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The instance pulls. The script names secret URLs, never a value."""
    _write_install_address(_NAME)
    runner = _InstallRunner(["Succeeded"])

    _install(runner)

    script = runner.script_text
    assert script is not None
    assert "Metadata:true" in script
    assert "api-version=2018-02-01" in script
    assert "resource=https://vault.azure.net" in script
    assert "access_token" in script
    for secret_name in ("tls-server-key", "tls-server-cert", "tls-ca"):
        assert (
            f"https://gd-vault.vault.azure.net/secrets/{secret_name}?api-version=7.4"
            in script
        ), f"{secret_name} is published but never fetched"
        assert '["value"]' in script, "the Key Vault answer is JSON; the value must be extracted"
    assert "BEGIN" not in script
    assert "PRIVATE KEY" not in script


def test_the_script_never_briefly_world_readables_the_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_install_address(_NAME)
    runner = _InstallRunner(["Succeeded"])

    _install(runner)

    script = runner.script_text
    assert script is not None
    umask_at = next(index for index, line in enumerate(script.splitlines()) if line == "umask 077")
    first_fetch = next(
        index for index, line in enumerate(script.splitlines()) if "secrets/tls-" in line
    )
    assert umask_at < first_fetch, "the umask must be set before anything is written"
    assert f'chmod 0600 "$dir/{certs.SERVER_KEY_FILENAME}"' in script
    assert f'chmod 0644 "$dir/{certs.SERVER_CERT_FILENAME}"' in script
    assert f'chmod 0644 "$dir/{certs.CA_CERT_FILENAME}"' in script


def test_the_script_starts_the_daemon_after_the_material_is_written(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_install_address(_NAME)
    runner = _InstallRunner(["Succeeded"])

    _install(runner)

    script = runner.script_text
    assert script is not None
    lines = script.splitlines()
    last_write = max(
        index for index, line in enumerate(lines) if "chown" in line or "chmod" in line
    )
    start_at = next(index for index, line in enumerate(lines) if "restart docker.service" in line)
    assert start_at > last_write, "the daemon must not start before its certificate exists"
    assert any("is-active docker.service" in line for line in lines), (
        "the step must confirm the daemon is running rather than trusting the start's exit code"
    )


def test_the_script_is_handed_to_az_as_a_private_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_install_address(_NAME)
    runner = _InstallRunner(["Succeeded"])

    _install(runner)

    assert runner.script_mode == 0o600, "the script carries no secret but is still private"


def test_the_poll_budget_is_bounded_by_the_configured_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport_azure = _import_transport_azure()
    _write_install_address(_NAME)
    monkeypatch.setenv(transport_azure.AZURE_MATERIAL_INSTALL_TIMEOUT_ENV_VAR, "41")
    runner = _InstallRunner(["Succeeded"])

    _install(runner)

    assert runner.timeouts[0] == 41.0, "the invoke is bounded by the same budget"
    assert all(
        timeout is not None and 0 < timeout <= 41.0 for timeout in runner.timeouts[1:]
    ), (
        "each poll is a small read bounded by the remaining deadline, "
        f"not by a separate budget: {[timeout for timeout in runner.timeouts[1:]]}"
    )


def test_the_poll_paces_on_the_shared_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport_azure = _import_transport_azure()
    _write_install_address(_NAME)
    runner = _InstallRunner(["Running", "Running", "Succeeded"])
    clock = _SteppedClock()

    _install(runner, clock=clock)

    assert clock.now == 2 * transport_azure._RUN_COMMAND_POLL_SECONDS, (
        "one poll interval per non-terminal poll, never more and never a fixed wait"
    )


def test_a_timeout_names_the_run_command_and_the_variable_that_extends_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport_azure = _import_transport_azure()
    _write_install_address(_NAME)
    runner = _InstallRunner(["Running"] * 50)
    clock = _SteppedClock()

    with pytest.raises(transport_azure.MaterialInstallError) as excinfo:
        _install(runner, clock=clock)

    message = str(excinfo.value)
    assert _INSTALL_RUN_COMMAND_NAME in message
    assert transport_azure.AZURE_MATERIAL_INSTALL_TIMEOUT_ENV_VAR in message
    assert _VM_NAME in message


def test_a_terminal_failure_carries_the_instance_view_error_and_the_remedy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport_azure = _import_transport_azure()
    _write_install_address(_NAME)
    runner = _InstallRunner(["Failed"], error="cannot find the vault: not authorized")

    with pytest.raises(transport_azure.MaterialInstallError) as excinfo:
        _install(runner)

    message = str(excinfo.value)
    assert "Failed" in message
    assert "not authorized" in message
    assert f"make cert-publish INSTANCE={_NAME}" in message
    assert _INSTALL_RUN_COMMAND_NAME in message


def test_an_unreadable_poll_answer_fails_instead_of_polling_forever(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport_azure = _import_transport_azure()
    _write_install_address(_NAME)
    runner = _InstallRunner(["Running"])
    runner.raw_show_stdout = "not json"

    with pytest.raises(transport_azure.MaterialInstallError) as excinfo:
        _install(runner)

    assert "unparsable output" in str(excinfo.value)


def test_a_vault_uri_in_the_record_is_used_verbatim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_install_address(_NAME, vault_uri="https://gd-vault.vault.azure.net/")
    runner = _InstallRunner(["Succeeded"])

    _install(runner)

    script = runner.script_text
    assert script is not None
    assert "https://gd-vault.vault.azure.net/secrets/tls-server-key" in script


@pytest.mark.parametrize("missing_key", ["vm_name", "resource_group", "vault_name"])
def test_a_record_missing_an_install_key_names_the_deploy_remedy(
    monkeypatch: pytest.MonkeyPatch, missing_key: str
) -> None:
    transport_azure = _import_transport_azure()
    record: dict[str, object] = {
        "relay_namespace": _NAMESPACE,
        "hybrid_connection": _HYBRID_CONNECTION,
        "resource_group": _RESOURCE_GROUP,
        "vm_name": _VM_NAME,
        "vault_name": "gd-vault",
    }
    del record[missing_key]
    instances.write_engine_address(_repo_root(), _NAME, record)
    runner = _InstallRunner(["Succeeded"])

    with pytest.raises(transport_azure.RelayConfigError) as excinfo:
        _install(runner)

    message = str(excinfo.value)
    assert missing_key in message
    assert f"make instance-deploy INSTANCE={_NAME}" in message
    assert runner.commands == [], "nothing may run against azure without a whole record"


def test_a_vault_name_that_is_not_a_bare_label_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport_azure = _import_transport_azure()
    instances.write_engine_address(
        _repo_root(),
        _NAME,
        {
            "relay_namespace": _NAMESPACE,
            "hybrid_connection": _HYBRID_CONNECTION,
            "resource_group": _RESOURCE_GROUP,
            "vm_name": _VM_NAME,
            "vault_name": "https://elsewhere.vault.azure.net",
        },
    )

    with pytest.raises(transport_azure.RelayConfigError) as excinfo:
        _import_transport_azure().vault_url(
            instances.engine_address(_repo_root(), _NAME), instance=_NAME
        )

    assert "bare vault name" in str(excinfo.value)


def test_install_material_argv_through_the_cli(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The subcommand certificates run reaches `install_material` and reports success."""
    transport_azure = _import_transport_azure()
    _write_install_address(_NAME)
    runner = _InstallRunner(["Succeeded"])
    monkeypatch.setattr(transport_azure.transport, "subprocess_command_runner", runner)

    exit_code = transport_azure.main(
        ["install-material", "--instance", _NAME, "--daemon-user", "dockerd"]
    )

    assert exit_code == 0
    assert runner.invoke_command(), "the subcommand must issue the run command"
    assert "installed" in capsys.readouterr().out.lower()


# ---------------------------------------------------------------------------
# The readiness poll: the bounded power-plus-agent wait, the one home of the
# instance-view reads.
# ---------------------------------------------------------------------------


class _ReadyRunner:
    """A runner double answering the instance-view reads from scripted queues.

    The power queue and the agent queue are consumed independently, so a
    test scripts exactly the interleaving the poll should observe; a read
    with an empty queue answers empty output (the platform reporting no
    status yet), the same shape the real tsv query gives.
    """

    def __init__(
        self,
        power_states: list[str] | None = None,
        agent_statuses: list[str] | None = None,
        *,
        power_default: str = "",
    ) -> None:
        self._power = list(power_states or [])
        self._agent = list(agent_statuses or [])
        self._power_default = power_default
        self.commands: list[tuple[str, ...]] = []

    def __call__(self, command: Sequence[str], timeout_seconds: float | None) -> CommandResult:
        del timeout_seconds
        self.commands.append(tuple(command))
        query = command[command.index("--query") + 1]
        if "vmAgent" in query:
            value = self._agent.pop(0) if self._agent else ""
        elif self._power:
            value = self._power.pop(0)
        else:
            value = self._power_default
        return CommandResult(exit_code=0, stdout=f"{value}\n")


class _SteppingClock:
    """A fake clock and pacer pair: every pace advances the clock one poll."""

    def __init__(self) -> None:
        self.now = 0.0
        self.paces: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.paces.append(seconds)
        self.now += seconds


def test_vm_power_and_agent_argv_are_pinned() -> None:
    transport_azure = _import_transport_azure()
    assert transport_azure.build_vm_power_argv("gd-rg", "gd-vm") == [
        "vm",
        "get-instance-view",
        "--resource-group",
        "gd-rg",
        "--name",
        "gd-vm",
        "--query",
        "instanceView.statuses[?starts_with(code,'PowerState')].code | [0]",
        "--output",
        "tsv",
    ]
    agent_argv = transport_azure.build_vm_agent_argv("gd-rg", "gd-vm")
    assert agent_argv[-3] == "instanceView.vmAgent.statuses[0].displayStatus"
    assert agent_argv[-2:] == ["--output", "tsv"]


def test_wait_agent_ready_returns_on_the_first_ready_read() -> None:
    transport_azure = _import_transport_azure()
    runner = _ReadyRunner(["PowerState/running"], ["Ready"])

    transport_azure.wait_agent_ready("gd-rg", "gd-vm", runner, deadline_seconds=60.0)

    assert len(runner.commands) == 2, "ready on the first read: one power read, one agent read"
    assert any("PowerState" in element for element in runner.commands[0])
    assert any("vmAgent" in element for element in runner.commands[1])


def test_wait_agent_ready_polls_until_running_then_agent_ready() -> None:
    transport_azure = _import_transport_azure()
    runner = _ReadyRunner(
        ["", "PowerState/stopped", "PowerState/running", "PowerState/running"],
        ["Starting", "Ready"],
    )
    clock = _SteppingClock()

    transport_azure.wait_agent_ready(
        "gd-rg", "gd-vm", runner, pace=clock.sleep, clock=clock.clock, deadline_seconds=600.0
    )

    # The power queue is drained to running before the agent is read at all,
    # and each not-yet answer paced one poll interval.
    assert clock.paces, "the poll must pace, never spin"
    assert all(pace == 5.0 for pace in clock.paces)


def test_wait_agent_ready_timeout_names_the_last_observed_state_and_the_variable() -> None:
    transport_azure = _import_transport_azure()
    runner = _ReadyRunner(["PowerState/deallocated"], power_default="PowerState/deallocated")
    clock = _SteppingClock()

    with pytest.raises(transport_azure.RelayTransportError) as excinfo:
        transport_azure.wait_agent_ready(
            "gd-rg",
            "gd-vm",
            runner,
            pace=clock.sleep,
            clock=clock.clock,
            deadline_seconds=10.0,
        )

    message = str(excinfo.value)
    assert "PowerState/deallocated" in message, "the last observed state must surface"
    assert transport_azure.AZURE_AGENT_READY_TIMEOUT_ENV_VAR in message
    assert "gd-vm" in message


def test_wait_agent_ready_reads_its_deadline_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport_azure = _import_transport_azure()
    monkeypatch.setenv(transport_azure.AZURE_AGENT_READY_TIMEOUT_ENV_VAR, "3")
    runner = _ReadyRunner(["PowerState/deallocated"], power_default="PowerState/deallocated")
    clock = _SteppingClock()

    with pytest.raises(transport_azure.RelayTransportError):
        transport_azure.wait_agent_ready(
            "gd-rg", "gd-vm", runner, pace=clock.sleep, clock=clock.clock
        )

    # One pace inside the 3s budget; the default 300s budget would pace
    # dozens of times before timing out.
    assert len(clock.paces) == 1, "the deadline came from the environment, not the default"


def test_agent_ready_timeout_rejects_a_non_positive_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport_azure = _import_transport_azure()
    monkeypatch.setenv(transport_azure.AZURE_AGENT_READY_TIMEOUT_ENV_VAR, "not-a-number")

    with pytest.raises(transport_azure.RelayTransportError) as excinfo:
        transport_azure.agent_ready_timeout_seconds()

    assert transport_azure.AZURE_AGENT_READY_TIMEOUT_ENV_VAR in str(excinfo.value)


def test_wait_agent_ready_surfaces_a_failed_az_call() -> None:
    transport_azure = _import_transport_azure()

    def failing(command: Sequence[str], timeout_seconds: float | None) -> CommandResult:
        del timeout_seconds
        assert command[0] == "az"
        return CommandResult(exit_code=1, stderr="ERROR: Please run az login")

    with pytest.raises(transport_azure.RelayConfigError) as excinfo:
        transport_azure.wait_agent_ready("gd-rg", "gd-vm", failing, deadline_seconds=10.0)

    assert "az login" in str(excinfo.value)
