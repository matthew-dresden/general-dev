"""Azure Relay transport: the hybrid-connection tunnel carrying the docker API.

This module is the third transport `make connect-start` opens, routed by the
engine's own kind, beside `devcontainer_config.transport`'s SSM port forward
and `devcontainer_config.transport_ssh`'s plain `ssh -N -L` forward. Where
those reach an EC2 instance over SSM and a generic engine over ssh, this one
reaches an azure virtual machine over an azure Relay hybrid connection: the
VM holds the relay's listener end (the `gd-relay-listener.py` unit the azure
layer ships), and this module's daemon is the sender half, one sender
WebSocket per docker API connection.

The wire contract is the one the live spike proved end to end, and this
module is deliberately literal about it:

- The sender dials `wss://<namespace>/$hc/<hybrid-connection>` with
  `sb-hc-action=connect` and the Entra token in the `ServiceBusAuthorization`
  request header. The token never rides the query string, and no
  `sb-hc-id` is ever sent by a sender: a custom id makes the couple
  uncouplable server-side (the accept dial answers
  "Sender Gateway: Listener accepted connection too late", the sender times
  out on "None of the connected listeners accepted"), a live-verified azure
  defect the spike measured. `build_handshake_request` and `relay_endpoint`
  are the only two places a sender URL or request head is composed, and
  neither carries the parameter at all.
- Once the upgrade completes, the socket is a raw bidirectional byte pipe:
  binary WebSocket frames carry the payload, the service relays them
  transparently, and each sender connection is one coupled pipe. An empty
  binary frame is the in-band EOF sentinel: the bridge whose TCP side read
  EOF sends exactly one (never a close frame), and the bridge that receives
  one shuts its own TCP write half down -- EOF toward `docker exec -i` --
  while the reverse direction keeps running. A later close frame (1000/1001)
  or TCP FIN still tears the whole pipe down. The service may send pings
  (answered with pongs), and client frames are always masked (RFC 6455).
- The Entra token comes from `az account get-access-token --resource
  https://relay.azure.net`, lives roughly an hour, and is cached with a
  five-minute refresh margin; a 401 on a new connection invalidates the
  cache and retries once with a fresh token before failing with the
  `az login` remedy. The fetch is bounded by `AZURE_TOKEN_FETCH_TIMEOUT`
  (default 30 seconds) so a hung `az` fails this one token attempt instead
  of wedging every accepted connection behind the cache's lock; a failed
  fetch leaves the cache empty, so the next `get` retries.

`connect` mirrors the other transports' connect flow with a relay pipe in
place of their tunnel: it loads the instance's relay address record
(`instances.engine_address`, the file `make instance-deploy` writes), then
reuses `transport.ensure_context` wholesale for the client-certificate
precondition, the refusal of an existing `ssh://` context, and the mTLS
context create-or-update at `tcp://127.0.0.1:<port>` -- one implementation
of that flow, not a second copy of its certificate check, its legacy
refusal or its context argv builders. The port comes from
`transport.allocate_local_port` (the same record-is-the-context rule, so
the context stays stable across reconnects), the context is activated with
`docker context use`, the local TCP acceptor binds on `127.0.0.1:<port>`,
the `Connected:` line `devcontainer_config.forwards` polls for prints --
only after the acceptor is bound and the context is usable -- and the
daemon serves until SIGTERM/SIGINT: every accepted loopback connection
opens a fresh relay sender (fresh header token) and is piped
bidirectionally until either side closes. Connections are independent: one
that fails is logged to stderr and never kills the daemon. SIGINT arrives
as the interpreter's own `KeyboardInterrupt`; SIGTERM is trapped into the
same clean stop, so the daemon lifecycle's process-group signal tears the
daemon down to exit 0 with the acceptor and every open pipe closed.

Before binding the local port, `connect` ensures the VM-side listener is
running: one synchronous `az vm run-command invoke` runs `systemctl start
gd-relay; systemctl is-active --quiet gd-relay` on the VM (the resource
group and VM name come from the same address record). This exists because
the listener is the thing that self-stops when idle -- azure bills a
hybrid connection while a listener is connected, so the VM-side unit
stops itself after `GD_RELAY_IDLE_TIMEOUT_SECONDS` with no open bridge --
which means a forward reopened after an idle gap reaches a listener that
is not running. The invoke is synchronous by design: `az` waits for the
VM agent to run the script, which on a freshly started VM can take tens of
seconds, and the connect flow is deliberately serialized behind it rather
than letting the first docker call die on a 404 no-listener answer; the
wait is bounded by `AZURE_LISTENER_START_TIMEOUT` (default 240 seconds,
read through `hostprobe.read_positive_seconds`), never unbounded. A
failed or timed-out start names the journalctl run command that shows why
the unit is not active.

`verify` is the one-shot reachability proof: the same flow through the
context activation, then one relay sender opened by one real docker
version call through the bound acceptor, the verdict line, and every
socket torn down on every path out -- never a listener or daemon left
behind. It does not start the listener: a 404 no-listener refusal there
is diagnosed with the `make connect-refresh` remedy, which re-runs the
daemon's recorded command including the listener start that brings the
unit up.

Nothing here waits on time. The daemon's liveness is the blocking
`accept()` in its own thread; the bridging reads block on real sockets
once the handshake returns them to fully blocking mode, idleness being a
pipe's normal state; the token cache refreshes on demand, not on a
schedule; and the two deadlines the module keeps (the dial and the
verify's version call) are read through `hostprobe.read_positive_seconds`
from `RELAY_DIAL_TIMEOUT`/`DOCKER_HANDSHAKE_TIMEOUT`, never a fixed sleep.
Every az and docker invocation is issued through an injected
`devcontainer_config.hostprobe.CommandRunner`, the same seam the other
transports inject; `transport.subprocess_command_runner` is this module's
production runner, so the unit suite drives every path with a fake and
never runs a real `az`, `docker`, or network dial -- the one socket work
the tests do is loopback, the same allowance `tests/test_transport.py`
uses for port probes.
"""

from __future__ import annotations

import argparse
import base64
import datetime
import hashlib
import json
import os
import re
import secrets
import signal
import socket
import ssl
import struct
import sys
import tempfile
import threading
import time
import urllib.parse
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import NoReturn

from devcontainer_config import certs, instances, repo, transport
from devcontainer_config.hostprobe import (
    DOCKER_HANDSHAKE_TIMEOUT_DEFAULT_SECONDS,
    DOCKER_HANDSHAKE_TIMEOUT_ENV_VAR,
    CommandResult,
    CommandRunner,
    HostProbeError,
    read_positive_seconds,
)

# The Entra audience the relay hybrid-connection tokens are minted for; the
# same literal `az account get-access-token --resource` takes.
RELAY_TOKEN_RESOURCE = "https://relay.azure.net"

_AZ_BINARY = "az"

# The token cache's refresh margin: a cached token is trusted only while more
# than this many seconds of life remain, so a daemon serving connections
# across the token's whole lifetime never presents one that expires mid-swap.
# Fixed by the wire contract the spike verified, not configurable: a margin
# shorter than the round of use it guards would reintroduce the mid-use 401.
RELAY_TOKEN_REFRESH_MARGIN_SECONDS = 300.0

RELAY_DIAL_TIMEOUT_ENV_VAR = "RELAY_DIAL_TIMEOUT"
_RELAY_DIAL_TIMEOUT_DEFAULT_SECONDS = 30.0

# One token fetch's deadline: the cache holds its lock across the `az` call,
# so an unbounded fetch -- a hung `az` waiting on a browser prompt or a dead
# network -- would wedge every accepted connection behind it while
# `connect-status` kept reporting the forward listening. The deadline is
# read through `read_positive_seconds` like every other bounded wait here.
AZURE_TOKEN_FETCH_TIMEOUT_ENV_VAR = "AZURE_TOKEN_FETCH_TIMEOUT"
_AZURE_TOKEN_FETCH_TIMEOUT_DEFAULT_SECONDS = 30.0

_LOCALHOST = "127.0.0.1"

# RFC 6455: the fixed GUID appended to the client key for the accept answer,
# and the frame opcodes this client speaks. Client-to-server frames are
# ALWAYS masked; server frames arrive unmasked, and `recv_frame` handles both.
_WEBSOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_OP_CONTINUATION = 0x0
_OP_TEXT = 0x1
_OP_BINARY = 0x2
_OP_CLOSE = 0x8
_OP_PING = 0x9
_OP_PONG = 0xA

# One frame-length ceiling, enforced before a declared payload is read: a
# corrupt or hostile 64-bit length field must fail one connection, never
# drive this reader into buffering an unbounded frame. Relay payloads are
# docker API traffic, well under this bound.
_MAX_FRAME_BYTES = 16 * 1024 * 1024

# The one header the relay's authentication rides on. The token is a bearer
# credential, so it is never logged, never echoed into an error, and scrubbed
# from every handshake failure body this module surfaces.
_AUTH_HEADER_NAME = "ServiceBusAuthorization"

# The relay service's two rendezvous actions; this module, the laptop's
# daemon, is always the sender half.
_ACTION_CONNECT = "connect"

_CLOSE_NORMAL = 1000

# The record keys this transport reads out of the instance's relay address
# record. Everything else in the record (vm_resource_id, vault_name,
# resource_group) belongs to other slices and is deliberately ignored here.
_RELAY_NAMESPACE_KEY = "relay_namespace"
_HYBRID_CONNECTION_KEY = "hybrid_connection"

# The strict shapes the three record values this module composes into URLs
# and shell strings must match, checked before anything is composed with
# them. The namespace is the deploy's FQDN form ("<name>.
# servicebus.windows.net", what `make instance-deploy` writes) or its bare
# label; the hybrid connection is the `gd-<instance>` name this repo
# mints; the vault URI is the standard azure Key Vault REST host. Anything
# outside these -- a scheme, a path, a query, a fragment, a shell
# metacharacter -- would compose a dial aimed somewhere else or a shell
# string the VM would interpret, so the record is refused outright rather
# than escaped.
_RELAY_NAMESPACE_PATTERN = re.compile(r"^[a-z0-9-]+(\.servicebus\.windows\.net)?$")
_HYBRID_CONNECTION_PATTERN = re.compile(r"^[A-Za-z0-9-]{1,64}$")
_VAULT_URI_PATTERN = re.compile(r"^https://[A-Za-z0-9-]+\.vault\.azure\.net/?$")

# The record keys the install-material surface below reads, and the one
# optional key that names the vault's REST endpoint outright. A record
# carrying `vault_uri` is answered from that value; one carrying only
# `vault_name` has the URL composed; one carrying neither is refused.
_VM_NAME_KEY = "vm_name"
_RESOURCE_GROUP_KEY = "resource_group"
_VAULT_URI_KEY = "vault_uri"

# The azure IMDS and Key Vault REST constants the VM-side install script
# fetches its material through. Both are platform-fixed shapes, named here
# once so the script builder composes them rather than inlining literals
# per line.
_KEYVAULT_RESOURCE = "https://vault.azure.net"
_IDENTITY_TOKEN_API_VERSION = "2018-02-01"
_KEYVAULT_API_VERSION = "7.4"
_IDENTITY_ENDPOINT = "http://169.254.169.254"

# The deadline the VM has to pull its material and start its daemon in,
# read through `read_positive_seconds` from the environment rather than a
# literal, the same shape `transport`'s own MATERIAL_INSTALL_TIMEOUT takes.
AZURE_MATERIAL_INSTALL_TIMEOUT_ENV_VAR = "AZURE_MATERIAL_INSTALL_TIMEOUT"
_AZURE_MATERIAL_INSTALL_TIMEOUT_DEFAULT_SECONDS = 300.0

# The connect-time listener start: the run-command script is the start plus
# the activity assert (the assert is what makes the script's own exit
# non-zero when the unit is not active after the start), and the invoke is
# synchronous -- az waits for the VM agent -- so the whole start is bounded
# by one deadline read through `read_positive_seconds`, the same shape
# every other bounded wait in this module keeps. On a freshly started VM
# the run command can take tens of seconds (the VM agent boots with the
# instance); 240 covers a cold agent without hanging a misconfigured one.
AZURE_LISTENER_START_TIMEOUT_ENV_VAR = "AZURE_LISTENER_START_TIMEOUT"
_AZURE_LISTENER_START_TIMEOUT_DEFAULT_SECONDS = 240.0
_LISTENER_START_SCRIPT = "systemctl start gd-relay; systemctl is-active --quiet gd-relay"

# The provisioning code the synchronous install invoke must answer with
# (the agent ran the script) plus the sentinel the script prints as its
# own final line (every step inside it succeeded) -- the answer carries
# no script exit code, so the script signs its own success.
_RUN_COMMAND_SUCCESS_CODE = "ProvisioningState/succeeded"
_INSTALL_OK_SENTINEL = "gd-install-ok"

# One accept backlog, one pipe chunk: both plain stdlib defaults, named so
# the bridge loop reads as the pipe it is.
_ACCEPT_BACKLOG = 8
_PIPE_CHUNK_BYTES = 65536


class RelayTransportError(transport.TransportError):
    """Base for every operational failure this module raises.

    Every failure this module can produce is a `RelayTransportError` (or a
    more specific subclass below), and every one of those is also a
    `transport.TransportError`, so a caller that handles the transports
    uniformly can catch the ssm module's base class across all three.
    """


class RelayHandshakeError(RelayTransportError):
    """The relay service refused a WebSocket upgrade.

    Carries the HTTP status the upgrade answered with and the body redacted
    of the token, classified into the remedy that names the actual cause:
    a 401 is the operator's Entra session (`az login`, then a forward
    refresh), a 404 naming no connected listener is the VM-side listener
    being down, and anything else is surfaced as the service stated it.
    """

    def __init__(self, *, status: int | None, body: str, instance: str) -> None:
        self.status = status
        self.body = body
        super().__init__(_diagnose_handshake(status=status, body=body, instance=instance))


class RelayConfigError(RelayTransportError):
    """The instance's relay address record or local credentials are unusable.

    Raised for a missing or malformed `engine-address.json` (the record
    `make instance-deploy` writes), a record missing its relay keys, an
    unusable `az` token answer, or a `--context` that does not name the
    instance's own docker context.
    """


class MaterialInstallError(RelayTransportError):
    """The VM's TLS-material install did not reach `Succeeded`.

    Raised for a run command that ended in a terminal failure state, one
    that outlived `AZURE_MATERIAL_INSTALL_TIMEOUT`, or an answer this
    module cannot read. The message carries the instance view's own error,
    the deadline variable that would extend the run, or the raw answer,
    whichever distinguishes the cause.
    """


class RelayProtocolError(RelayTransportError):
    """A relay WebSocket spoke a frame this module's RFC 6455 reader refuses."""


class _HandshakeRefusal(Exception):
    """The relay service refused a WebSocket upgrade, before the diagnosis exists.

    The relay WebSocket does not know which instance it dialed for, so it
    raises this status-and-body carrier; `open_relay_sender` is the one
    place that turns it into a `RelayHandshakeError` whose remedy names the
    instance, keeping the diagnosis wording single-sited without threading
    an instance name through the framing class.
    """

    def __init__(self, *, status: int | None, body: str) -> None:
        self.status = status
        self.body = body
        super().__init__(body)


def _diagnose_handshake(*, status: int | None, body: str, instance: str) -> str:
    """The handshake refusal diagnosis: the status, the redacted body, the remedy.

    The classification is the module's one statement of the three causes the
    live spike verified, worded identically wherever a sender handshake can
    fail: the connect daemon's per-connection openers and the verify path
    all raise through this one construction site, so the operator never sees
    two wordings for one cause.
    """
    detail = body.strip()
    if status == 401:
        return (
            f"ERROR: the relay service refused the WebSocket upgrade with {status}\n"
            f"{detail}\n"
            "The Entra token was rejected. Run 'az login' to refresh the credentials, "
            f"then: make connect-refresh INSTANCE={instance}"
        )
    if status == 404 and (
        "no listeners connected" in detail.lower()
        or "none of the connected listeners" in detail.lower()
    ):
        return (
            f"ERROR: the relay service refused the WebSocket upgrade with {status}\n"
            f"{detail}\n"
            f"The relay listener is not running; run: make connect-refresh INSTANCE={instance} "
            "which re-runs the daemon's recorded command, listener start included."
        )
    return (
        "ERROR: the relay service refused the WebSocket upgrade "
        f"(status {status if status is not None else 'unknown'})\n"
        f"{detail}\n"
        "Confirm the relay namespace and hybrid connection in the instance's address "
        "record, then retry; refresh the forward with: make connect-refresh "
        f"INSTANCE={instance}"
    )


# ---------------------------------------------------------------------------
# The Entra token: one argv builder, one parser, one cache. Every `az` call
# goes through the injected `CommandRunner` seam; the token itself never
# reaches a log, an error message, or a test assertion.
# ---------------------------------------------------------------------------


def build_az_token_argv() -> list[str]:
    """The `az account get-access-token` argument vector for a relay token.

    `--output json` is what makes the answer parsable; the resource is the
    relay audience the hybrid connection's gateway checks.
    """
    return [
        _AZ_BINARY,
        "account",
        "get-access-token",
        "--resource",
        RELAY_TOKEN_RESOURCE,
        "--output",
        "json",
    ]


def parse_token_expiry(value: object) -> float:
    """`expiresOn` as a UTC epoch second, from either shape azure prints.

    The CLI answers with an integer epoch (`expires_on`) for some credential
    flows and a `YYYY-MM-DD HH:MM:SS.000000` string (`expiresOn`) for
    others (the string is wall-clock in the machine's local zone, which
    `datetime.timestamp()` on a naive datetime applies). Anything else is a
    payload this module refuses rather than guesses at, so a future shape
    change fails loudly here instead of silently caching a token to the
    wrong deadline.
    """
    if isinstance(value, bool):
        raise RelayConfigError(
            "ERROR: az account get-access-token returned a boolean expiresOn\n"
            "Run the command manually and investigate why its output does not match the "
            "expected shape."
        )
    if isinstance(value, int):
        return float(value)
    if isinstance(value, str):
        try:
            parsed = datetime.datetime.strptime(value, "%Y-%m-%d %H:%M:%S.%f")
        except ValueError as exc:
            raise RelayConfigError(
                f"ERROR: az account get-access-token returned an unparsable expiresOn "
                f"{value!r}\n"
                "Run the command manually and investigate why its output does not match the "
                "expected shape."
            ) from exc
        return parsed.timestamp()
    raise RelayConfigError(
        "ERROR: az account get-access-token returned an unparsable expiresOn "
        f"{type(value).__name__}\n"
        "Run the command manually and investigate why its output does not match the "
        "expected shape."
    )


def parse_token_payload(stdout: str) -> tuple[str, float]:
    """The token answer's `(accessToken, expiry epoch)` pair, validated.

    Raises:
        RelayConfigError: the output is not valid JSON, is missing either
            field, or carries an unusable value; the message never quotes
            the token itself.
    """
    try:
        payload: object = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RelayConfigError(
            "ERROR: az account get-access-token produced unparsable output\n"
            f"{exc}\n"
            "Run the command manually and investigate why its output does not match the "
            "expected shape."
        ) from exc
    if not isinstance(payload, dict):
        raise RelayConfigError(
            "ERROR: az account get-access-token produced a payload that is not one JSON "
            "object\n"
            "Run the command manually and investigate why its output does not match the "
            "expected shape."
        )
    token = payload.get("accessToken")
    if not isinstance(token, str) or not token:
        raise RelayConfigError(
            "ERROR: az account get-access-token returned no accessToken\n"
            "Run 'az login' to refresh the credentials, then retry."
        )
    # The epoch field wins when the CLI prints both shapes: it is absolute,
    # where the deprecated string is wall-clock in the local zone.
    if "expires_on" in payload:
        return token, parse_token_expiry(payload["expires_on"])
    if "expiresOn" not in payload:
        raise RelayConfigError(
            "ERROR: az account get-access-token returned no expiresOn\n"
            "Run the command manually and investigate why its output does not match the "
            "expected shape."
        )
    return token, parse_token_expiry(payload["expiresOn"])


def _token_fetch_timeout_seconds() -> float:
    """`AZURE_TOKEN_FETCH_TIMEOUT`, read through the shared hostprobe reader.

    Raises:
        RelayTransportError: the variable is set to something that is not a
            finite positive number of seconds.
    """
    try:
        return read_positive_seconds(
            AZURE_TOKEN_FETCH_TIMEOUT_ENV_VAR, _AZURE_TOKEN_FETCH_TIMEOUT_DEFAULT_SECONDS
        )
    except HostProbeError as exc:
        raise RelayTransportError(
            f"ERROR: {exc}\n"
            "Set it to a positive number of seconds, or unset it to use the default of "
            f"{_AZURE_TOKEN_FETCH_TIMEOUT_DEFAULT_SECONDS:g}."
        ) from exc


def fetch_relay_token(runner: CommandRunner) -> tuple[str, float]:
    """One live `az account get-access-token` through the runner seam.

    The call is bounded by `AZURE_TOKEN_FETCH_TIMEOUT`: a hung `az` (a
    browser prompt nobody is watching, a dead network) must fail this one
    attempt rather than hold the token cache's lock forever.

    Raises:
        RelayTransportError: the fetch outlived the deadline.
        RelayConfigError: `az` is not on PATH, the command failed (the
            operator's remedy is `az login`), or the answer could not be
            parsed.
    """
    timeout_seconds = _token_fetch_timeout_seconds()
    result = runner(tuple(build_az_token_argv()), timeout_seconds)
    if result.binary_missing:
        raise RelayConfigError(
            f"ERROR: {_AZ_BINARY} is not on PATH\nInstall the Azure CLI, then retry."
        )
    if result.timed_out:
        raise RelayTransportError(
            "ERROR: az account get-access-token did not answer within "
            f"{timeout_seconds:g}s\n"
            "The token cache's lock is held only for bounded fetches, so every waiting "
            "connection retries with its own attempt.\n"
            f"Raise {AZURE_TOKEN_FETCH_TIMEOUT_ENV_VAR} if this machine is simply slow, or "
            "run 'az login' to refresh the credentials, then retry."
        )
    if result.exit_code != 0:
        raise RelayConfigError(
            "ERROR: az account get-access-token failed\n"
            f"{result.stderr.strip()}\n"
            "Run 'az login' to refresh the credentials, then retry."
        )
    return parse_token_payload(result.stdout)


class TokenCache:
    """The daemon's shared Entra token, refreshed on demand, never on a schedule.

    One cache instance serves every connection thread the accept loop
    spawns: `get` returns the cached token while more than the refresh
    margin of life remains, and fetches a fresh one otherwise. The fetch
    runs under the lock -- single-flight, so a burst of connections shares
    one `az` call instead of racing into a token-per-connection storm --
    and the fetch is bounded by `AZURE_TOKEN_FETCH_TIMEOUT`, so a hung `az`
    delays the waiting connections by one bounded attempt instead of
    wedging them behind the lock indefinitely. A failed fetch leaves the
    cache empty (an in-flight stale token is dropped too), so the next
    `get` retries rather than re-serving a rejected or expired token.
    `invalidate` drops the cache after a 401 for the same reason. The clock
    is injected so the tests walk expiry without waiting on anything.
    """

    def __init__(
        self,
        runner: CommandRunner,
        *,
        refresh_margin_seconds: float = RELAY_TOKEN_REFRESH_MARGIN_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._runner = runner
        self._refresh_margin_seconds = refresh_margin_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._token: str | None = None
        self._expiry: float = 0.0

    def get(self) -> str:
        """A token with more than the refresh margin of life remaining."""
        with self._lock:
            now = self._clock()
            if self._token is not None and now < self._expiry - self._refresh_margin_seconds:
                return self._token
            try:
                token, expiry = fetch_relay_token(self._runner)
            except BaseException:
                # A failed fetch must not leave a stale token cached: the
                # next get() retries the fetch from an empty cache.
                self._token = None
                self._expiry = 0.0
                raise
            self._token = token
            self._expiry = expiry
            return token

    def invalidate(self) -> None:
        """Drop the cached token, after a 401, so the next `get` fetches afresh."""
        with self._lock:
            self._token = None
            self._expiry = 0.0


# ---------------------------------------------------------------------------
# The relay address record: the two keys this transport reads, and the one
# remedy for a record that does not carry them.
# ---------------------------------------------------------------------------


def load_relay_address(name: str) -> dict[str, object]:
    """The instance's relay address record, as `instances.engine_address` reads it.

    A missing or malformed record surfaces as `RelayConfigError` carrying
    `instances`' own remedy verbatim (the record's built-in message names the
    deploy that writes it), so no exception type foreign to this module
    crosses its boundary.

    Raises:
        RelayConfigError: the record is missing, unreadable, or not one
            JSON object, or the instance name is invalid.
    """
    try:
        return instances.engine_address(repo.find_root(Path.cwd()), name)
    except instances.InstancesError as exc:
        raise RelayConfigError(str(exc)) from exc


def relay_values(address: Mapping[str, object], *, instance: str) -> tuple[str, str]:
    """The `(namespace, hybrid_connection)` pair the record must carry.

    Extra keys (`vm_resource_id`, `vault_name`, `resource_group`) belong to
    other slices and are ignored. A record present but missing a relay key
    fails fast here, naming the same deploy remedy a missing record names:
    the operator's next step is identical, and a half-written record should
    read as the not-yet-deployed state it effectively is. Each key that IS
    present must match its strict pattern (`_RELAY_NAMESPACE_PATTERN`,
    `_HYBRID_CONNECTION_PATTERN`): both values are composed into the
    `wss://` dial URL, so a query, fragment, path or metacharacter smuggled
    into the record could aim the token-bearing dial somewhere else.

    Raises:
        RelayConfigError: either key is missing or is not a non-empty
            string matching its pattern.
    """
    values: list[str] = []
    for key, pattern, expected in (
        (
            _RELAY_NAMESPACE_KEY,
            _RELAY_NAMESPACE_PATTERN,
            "a relay namespace host ('<namespace>.servicebus.windows.net' or its bare label)",
        ),
        (
            _HYBRID_CONNECTION_KEY,
            _HYBRID_CONNECTION_PATTERN,
            "a bare hybrid-connection name of letters, digits and hyphens",
        ),
    ):
        value = address.get(key)
        if not isinstance(value, str) or not value:
            raise RelayConfigError(
                f"ERROR: the engine address record for instance {instance!r} carries no "
                f"usable {key!r}\n"
                f"The relay transport reads '{_RELAY_NAMESPACE_KEY}' and "
                f"'{_HYBRID_CONNECTION_KEY}' from {instances.engine_address_path(instance)}.\n"
                f"Redeploy to write it: make instance-deploy INSTANCE={instance}"
            )
        if not pattern.fullmatch(value):
            raise RelayConfigError(
                f"ERROR: the engine address record for instance {instance!r} carries a "
                f"malformed {key!r}\n"
                f"{value!r} is not {expected}; the value comes from "
                f"{instances.engine_address_path(instance)}.\n"
                f"Redeploy to rewrite it: make instance-deploy INSTANCE={instance}"
            )
        values.append(value)
    namespace, hybrid_connection = values
    return namespace, hybrid_connection


def relay_endpoint(namespace: str, hybrid_connection: str, action: str) -> str:
    """The `wss://<namespace>/$hc/<hybrid-connection>?sb-hc-action=<action>` URL.

    The one place a relay URL is composed. It never carries `sb-hc-id` or a
    query token: the parameter names are absent from this f-string on
    purpose (see the module docstring for the live-verified defect a custom
    sender id causes), and the token rides the `ServiceBusAuthorization`
    header instead.
    """
    return f"wss://{namespace}/$hc/{hybrid_connection}?sb-hc-action={action}"


# ---------------------------------------------------------------------------
# install-material: the azure twin of `transport.install_material`. The VM
# pulls its published TLS material from its own Key Vault through its
# managed identity and starts its rootless daemon; the run command names
# secret URLs and never carries a value, so no private key rides the
# command document -- the same pulls-not-pushes invariant the SSM path
# documents for its own command history.
# ---------------------------------------------------------------------------


def _address_str(address: Mapping[str, object], key: str, *, instance: str) -> str:
    """One string-valued record key, or the deploy remedy the record owes.

    A record present but missing a key this surface needs fails fast here,
    naming the same deploy remedy a missing record names: the operator's
    next step is identical, and a half-written record should read as the
    not-yet-deployed state it effectively is.

    Raises:
        RelayConfigError: the key is missing or is not a non-empty string.
    """
    value = address.get(key)
    if not isinstance(value, str) or not value:
        raise RelayConfigError(
            f"ERROR: the engine address record for instance {instance!r} carries no "
            f"usable {key!r}\n"
            f"The install-material surface reads '{key}' from "
            f"{instances.engine_address_path(instance)}.\n"
            f"Redeploy to write it: make instance-deploy INSTANCE={instance}"
        )
    return value


def vault_url(address: Mapping[str, object], *, instance: str) -> str:
    """The vault's REST base URL, from the record's `vault_uri` or its `vault_name`.

    Accepts either key: a record carrying `vault_uri` is answered from that
    value once it matches `_VAULT_URI_PATTERN` (a trailing slash trimmed, so
    secret paths compose cleanly); one carrying only `vault_name` has the
    standard `https://<vault_name>.vault.azure.net` URL composed. A record
    carrying neither is refused, as is a `vault_name` that is not a bare
    label -- the same bareness rule `relay_values` applies to the namespace,
    since a scheme or path in either would compose a malformed URL here.
    The `vault_uri` check is strict, not cosmetic: the URL is composed into
    shell the VM's root runs, so a query or path smuggled into the record
    could redirect the token-bearing secret fetch.

    Raises:
        RelayConfigError: neither key is usable, `vault_uri` does not match
            the strict pattern, or `vault_name` carries a scheme or path.
    """
    uri = address.get(_VAULT_URI_KEY)
    if isinstance(uri, str) and uri:
        if not _VAULT_URI_PATTERN.fullmatch(uri):
            raise RelayConfigError(
                f"ERROR: the engine address record for instance {instance!r} carries a "
                f"malformed {_VAULT_URI_KEY!r}\n"
                f"{uri!r} is not a Key Vault REST host ('https://<vault-name>"
                ".vault.azure.net'); the value comes from "
                f"{instances.engine_address_path(instance)}.\n"
                f"Redeploy to rewrite it: make instance-deploy INSTANCE={instance}"
            )
        return uri.rstrip("/")
    vault_name = _address_str(address, instances.VAULT_NAME_KEY, instance=instance)
    if "://" in vault_name or "/" in vault_name:
        raise RelayConfigError(
            f"ERROR: the engine address record for instance {instance!r} carries a "
            f"malformed {instances.VAULT_NAME_KEY!r}\n"
            f"{vault_name!r} is not a bare vault name; the record must name the vault "
            "label, and the URL is composed from it.\n"
            f"Redeploy to rewrite it: make instance-deploy INSTANCE={instance}"
        )
    return f"https://{vault_name}.vault.azure.net"


def build_install_commands(instance: str, *, vault_url: str, daemon_user: str) -> tuple[str, ...]:
    """The shell the VM runs to pull its published material and start its daemon.

    Every secret name comes from `certs.AZURE_PUBLISHABLE`, the same mapping
    `certs.azure_publication_set` derives the published names from, so what
    the instance fetches cannot drift from what was published. The directory
    head and the daemon restart tail are `transport`'s own shared lines, so
    both kinds' scripts prepare the directory and start the daemon the same
    way.

    The instance pulls; nothing is pushed. These commands name the vault's
    secret URLs and never carry a value, so the private key is absent from
    the run-command document and from the run-command history azure retains
    -- the token the script presents is its own managed identity's, fetched
    from the instance metadata endpoint at run time, and it reaches curl
    through stdin (`-H @-`) rather than argv, so it never sits in the VM's
    process table. The Key Vault REST answers are JSON, so each fetch
    extracts the body's `value` field with python3 (present on the Ubuntu
    image the azure layer ships) and writes it straight to its file;
    `umask 077` precedes the first fetch and each file's mode is set
    explicitly after it.
    """
    lines = list(transport.tls_dir_prepare_lines(daemon_user))
    # cloud-init writes /etc/general-dev-provisioned as its very last step,
    # so a present marker means rootless docker is installed and its user
    # service exists -- neither of which the azure VM agent's "Ready" tells
    # you (the agent answers before runcmd is halfway through apt). The
    # wait is bounded well inside this script's own run-command budget:
    # AZURE_MATERIAL_INSTALL_TIMEOUT governs the whole invoke, so a VM
    # still provisioning at ~4 minutes needs that variable raised, and the
    # failure says so.
    lines.append(
        "for _i in $(seq 1 48); do [ -f /etc/general-dev-provisioned ] && break; sleep 5; done"
    )
    lines.append(
        "[ -f /etc/general-dev-provisioned ] || { "
        'echo "cloud-init has not finished provisioning (no '
        "/etc/general-dev-provisioned marker); retry once the VM finishes "
        'bootstrapping, or raise AZURE_MATERIAL_INSTALL_TIMEOUT"; exit 1; }'
    )
    # No double quotes inside the command substitution: the strictest
    # parsers on the path -- the mac's bash 3.2 and the VM's dash -- both
    # mis-nest quotes inside "$(...)" inside "..." (the mac reuses the
    # outer quote context), so the fetch stays single-quoted and the JSON
    # key reaches python as an argv word instead of a quoted literal.
    lines.append(
        f"token=\"$(curl -sf -H 'Metadata:true' --connect-timeout 2 "
        f"'{_IDENTITY_ENDPOINT}/metadata/identity/oauth2/token"
        f"?api-version={_IDENTITY_TOKEN_API_VERSION}&resource={_KEYVAULT_RESOURCE}'"
        " | python3 -c 'import json,sys; print(json.load(sys.stdin)[sys.argv[1]])' "
        'access_token)"'
    )
    for filename, secret_name in certs.AZURE_PUBLISHABLE.items():
        mode = "0600" if filename == certs.SERVER_KEY_FILENAME else "0644"
        # The bearer header reaches curl through stdin (`-H @-`), never as
        # an argv word: a `-H "Authorization: Bearer $token"` would sit in
        # the process table for the whole fetch, readable by anything on
        # the VM that can list processes. The URL is single-quoted safely
        # because `vault_url` validated it to a shape that carries no
        # quote; under `set -eu` a failed fetch yields python3 an empty or
        # non-JSON body, so the script dies before its sentinel and the
        # invoke reports the failure.
        lines.append(
            "printf 'Authorization: Bearer %s\\n' \"$token\""
            f" | curl -sf -H @- "
            f"'{vault_url}/secrets/{secret_name}?api-version={_KEYVAULT_API_VERSION}'"
            " | python3 -c 'import json,sys; sys.stdout.write(json.load(sys.stdin)"
            f'["value"])\' > "$dir/{filename}"'
        )
        lines.append(f'chmod {mode} "$dir/{filename}"')
    lines.extend(transport.rootless_daemon_restart_lines())
    # The success sentinel: the run-command answer reports the agent's
    # provisioning state, not the script's own exit code, so the script
    # ends by printing a token whose presence in the answer's output is
    # what the caller reads as "every step above ran".
    lines.append(f"echo {_INSTALL_OK_SENTINEL}")
    return tuple(lines)


def _material_install_timeout_seconds() -> float:
    """The deadline for the VM to fetch its material and start its daemon.

    Read through `read_positive_seconds` rather than hard-coded so a slow
    instance can be accommodated without editing this file, the same shape
    `transport._material_install_timeout_seconds` gives the SSM path.

    Raises:
        RelayTransportError: the variable is set to something that is not a
            finite positive number of seconds.
    """
    try:
        return read_positive_seconds(
            AZURE_MATERIAL_INSTALL_TIMEOUT_ENV_VAR, _AZURE_MATERIAL_INSTALL_TIMEOUT_DEFAULT_SECONDS
        )
    except HostProbeError as exc:
        raise RelayTransportError(
            f"ERROR: {exc}\n"
            "Set it to a positive number of seconds, or unset it to use the default of "
            f"{_AZURE_MATERIAL_INSTALL_TIMEOUT_DEFAULT_SECONDS:g}."
        ) from exc


def _run_az(runner: CommandRunner, argv: Sequence[str]) -> CommandResult:
    """One small `az` read through the runner seam, with the module's az diagnoses.

    The binary is prefixed here, once, so every call site names only the
    subcommand it means. The reads routed through here are deliberately
    unbounded per call -- the readiness poll's own deadline governs the
    whole wait, not any single read -- so no per-call timeout is taken.

    Raises:
        RelayConfigError: `az` is not on PATH, or the call failed -- the
            authentication cause names `az login`, the same remedy every
            other az refusal in this module carries.
    """
    result = runner((_AZ_BINARY, *argv), None)
    if result.binary_missing:
        raise RelayConfigError(
            f"ERROR: {_AZ_BINARY} is not on PATH\nInstall the Azure CLI, then retry."
        )
    if result.exit_code != 0:
        raise RelayConfigError(
            f"ERROR: {' '.join((_AZ_BINARY, *argv[:3]))} failed\n"
            f"{result.stderr.strip()}\n"
            "If the failure is an authentication one, run 'az login' to refresh the "
            "credentials, then retry."
        )
    return result


def _invoke_outcome(stdout: str) -> tuple[bool, str]:
    """Whether the install invoke's answer means success, and its detail.

    The synchronous `az vm run-command invoke` answer is a `value` list of
    agent statuses whose `code` carries the provisioning state and whose
    `message` carries the script's `[stdout]`/`[stderr]` blocks. Success is
    the provisioning state reaching `ProvisioningState/succeeded` AND the
    script's own `gd-install-ok` sentinel appearing in the message -- the
    answer carries no script exit code, so the script signs its own
    success and a script that died early simply never signs.

    Raises:
        MaterialInstallError: the answer is not the shape this module
            reads, so it cannot tell success from failure.
    """
    try:
        statuses = json.loads(stdout)["value"]
        codes = [str(entry["code"]) for entry in statuses]
        messages = "\n".join(str(entry.get("message") or "") for entry in statuses)
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise MaterialInstallError(
            "ERROR: az vm run-command invoke produced unparsable output: "
            f"{stdout.strip()!r}\n"
            "Run the command manually and investigate why its output does not match the "
            "expected shape."
        ) from exc
    provisioned = _RUN_COMMAND_SUCCESS_CODE in codes
    signed = _INSTALL_OK_SENTINEL in messages
    return provisioned and signed, messages


def install_material(
    runner: CommandRunner,
    *,
    instance: str,
    daemon_user: str,
) -> None:
    """Have the VM pull its published TLS material from its vault and start its daemon.

    The azure twin of `transport.install_material`: the script is issued
    through one synchronous `az vm run-command invoke` (the script travels
    on a 0600 `--scripts @file`, removed before this function returns).
    The invoke itself is the waiter -- az blocks until the VM agent reports
    the script terminal and answers with the run command's own JSON, whose
    instance view carries the execution state -- so there is no poll loop,
    only the answer's shape to read. `AZURE_MATERIAL_INSTALL_TIMEOUT`
    bounds the call.

    Raises:
        RelayConfigError: the instance's engine address record is missing,
            malformed, or lacks the vault, resource-group or VM keys; or an
            `az` call failed outright.
        MaterialInstallError: the run command ended in a state other than
            `Succeeded` (the message carries the instance view's own error),
            outlived the deadline, or answered in an unparsable shape.
    """
    address = load_relay_address(instance)
    resource_group = _address_str(address, _RESOURCE_GROUP_KEY, instance=instance)
    vm_name = _address_str(address, _VM_NAME_KEY, instance=instance)
    url = vault_url(address, instance=instance)
    commands = build_install_commands(instance, vault_url=url, daemon_user=daemon_user)
    timeout_seconds = _material_install_timeout_seconds()

    with tempfile.TemporaryDirectory(prefix="devcontainer-azure-install-") as tmp_dir:
        script_path = Path(tmp_dir) / "install-material.sh"
        descriptor = os.open(script_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write("\n".join(commands))
            handle.write("\n")
        # The binary is prefixed here rather than routing through `_run_az`:
        # this call site carries its own diagnoses -- a timeout must name
        # the variable that extends it, not the generic az-failure one --
        # the same reason `start_listener` below bypasses the shared
        # classifier.
        completed = runner(
            (
                _AZ_BINARY,
                "vm",
                "run-command",
                "invoke",
                "--resource-group",
                resource_group,
                "--name",
                vm_name,
                "--command-id",
                "RunShellScript",
                "--scripts",
                f"@{script_path}",
                "--output",
                "json",
            ),
            timeout_seconds,
        )

    if completed.binary_missing:
        raise RelayConfigError(
            f"ERROR: {_AZ_BINARY} is not on PATH\nInstall the Azure CLI, then retry."
        )
    if completed.timed_out:
        raise MaterialInstallError(
            f"ERROR: installing TLS material on {vm_name} did not finish in "
            f"{timeout_seconds:g}s\n"
            "The run command is synchronous, so the VM agent answered nothing within the "
            "deadline.\n"
            f"Raise {AZURE_MATERIAL_INSTALL_TIMEOUT_ENV_VAR} if this instance is simply slow, "
            "then rerun with: make cert-install INSTANCE=" + instance
        )
    if completed.exit_code != 0:
        raise RelayConfigError(
            "ERROR: az vm run-command invoke failed\n"
            f"{completed.stderr.strip()}\n"
            "If the failure is an authentication one, run 'az login' to refresh the "
            "credentials, then retry."
        )

    succeeded, detail = _invoke_outcome(completed.stdout)
    if not succeeded:
        raise MaterialInstallError(
            f"ERROR: installing TLS material on {vm_name} did not complete\n"
            f"{detail}\n"
            f"Publish the material first if it is missing: "
            f"make cert-publish INSTANCE={instance}\n"
            "Rerun with: make cert-install INSTANCE=" + instance
        )


# ---------------------------------------------------------------------------
# The connect-time listener start: one synchronous run command that makes
# the VM-side unit active before the local port is bound. The listener
# self-stops when idle (the VM-side billing guard), so a forward reopened
# after an idle gap must start it again, and the start must finish before
# the daemon can accept its first connection -- hence synchronous, and
# bounded.
# ---------------------------------------------------------------------------


def listener_start_timeout_seconds() -> float:
    """`AZURE_LISTENER_START_TIMEOUT`, read through the shared hostprobe reader.

    Raises:
        RelayTransportError: the variable is set to something that is not a
            finite positive number of seconds.
    """
    try:
        return read_positive_seconds(
            AZURE_LISTENER_START_TIMEOUT_ENV_VAR, _AZURE_LISTENER_START_TIMEOUT_DEFAULT_SECONDS
        )
    except HostProbeError as exc:
        raise RelayTransportError(
            f"ERROR: {exc}\n"
            "Set it to a positive number of seconds, or unset it to use the default of "
            f"{_AZURE_LISTENER_START_TIMEOUT_DEFAULT_SECONDS:g}."
        ) from exc


def build_listener_start_argv(resource_group: str, vm_name: str) -> list[str]:
    """The synchronous run-command invoke that starts the VM-side listener.

    No `--no-wait`: the connect flow cannot bind its local port until the
    unit answers `is-active`, so the call is deliberately the waiting form,
    bounded by the deadline the caller passes rather than left unbounded.
    The `--scripts` value is `_LISTENER_START_SCRIPT`, whose trailing
    `systemctl is-active --quiet` is what makes the script itself fail when
    the unit is not active after the start.
    """
    return [
        "vm",
        "run-command",
        "invoke",
        "--resource-group",
        resource_group,
        "--name",
        vm_name,
        "--command-id",
        "RunShellScript",
        "--scripts",
        _LISTENER_START_SCRIPT,
        "--output",
        "none",
    ]


def _listener_journalctl_remedy(resource_group: str, vm_name: str) -> str:
    """The one command that shows why the VM-side unit is not active."""
    return (
        "az vm run-command invoke "
        f"--resource-group {resource_group} --name {vm_name} "
        '--command-id RunShellScript --scripts "journalctl -u gd-relay -n 50"'
    )


def start_listener(runner: CommandRunner, *, instance: str) -> None:
    """Have the VM start its `gd-relay` unit and prove it came up active.

    The resource group and VM name come from the instance's relay address
    record -- the record `make instance-deploy` writes -- and the invoke
    runs synchronously through the injected runner seam, bounded by
    `AZURE_LISTENER_START_TIMEOUT`.

    Raises:
        RelayConfigError: the address record is missing, malformed, or
            lacks the resource-group or VM keys, or `az` is not on PATH.
        RelayTransportError: the configured deadline is not a positive
            number of seconds; the invoke outlived it; or the invoke
            failed or the unit answered non-active (the message names the
            journalctl run command that shows the unit's own log, and
            `make instance-start` first when the VM may simply be
            deallocated).
    """
    address = load_relay_address(instance)
    resource_group = _address_str(address, _RESOURCE_GROUP_KEY, instance=instance)
    vm_name = _address_str(address, _VM_NAME_KEY, instance=instance)
    timeout_seconds = listener_start_timeout_seconds()
    # The binary is prefixed here rather than routing through `_run_az`:
    # this call site carries its own diagnoses (the journalctl remedy, the
    # timeout variable), not the generic az-failure one.
    result = runner(
        (_AZ_BINARY, *build_listener_start_argv(resource_group, vm_name)), timeout_seconds
    )
    if result.binary_missing:
        raise RelayConfigError(
            f"ERROR: {_AZ_BINARY} is not on PATH\nInstall the Azure CLI, then retry."
        )
    if result.timed_out:
        raise RelayTransportError(
            f"ERROR: starting the gd-relay listener on {vm_name} did not finish in "
            f"{timeout_seconds:g}s\n"
            f"The run command is synchronous, so the VM agent answered nothing within the "
            f"deadline.\n"
            f"Raise {AZURE_LISTENER_START_TIMEOUT_ENV_VAR} if this instance is simply slow; "
            "if the VM is deallocated, start it first: make instance-start "
            f"INSTANCE={instance}\n"
            f"Then check the unit's own log: {_listener_journalctl_remedy(resource_group, vm_name)}"
        )
    if result.exit_code != 0:
        raise RelayTransportError(
            f"ERROR: the gd-relay listener on {vm_name} did not come up active\n"
            f"{result.stderr.strip() or result.stdout.strip()}\n"
            "The start run command failed or its activity assert answered non-active.\n"
            f"If the VM is deallocated, start it first: make instance-start "
            f"INSTANCE={instance}\n"
            f"Then check the unit's own log: {_listener_journalctl_remedy(resource_group, vm_name)}"
        )


# ---------------------------------------------------------------------------
# The readiness poll: the bounded power-plus-agent wait a freshly started
# VM must satisfy before anything may drive it. This section is the ONE
# home of the `az vm get-instance-view` argv and its two queries: the cli's
# `azure-wait-agent` entry point, and `instance_ops`' azure start path, both
# come here rather than each composing their own copy of the poll.
# ---------------------------------------------------------------------------

# The deadline a start's readiness poll may run to, read through
# `read_positive_seconds` from the environment rather than a literal, the
# same shape every other bounded wait in this package takes.
AZURE_AGENT_READY_TIMEOUT_ENV_VAR = "AZURE_AGENT_READY_TIMEOUT"
_AZURE_AGENT_READY_TIMEOUT_DEFAULT_SECONDS = 300.0

# One state read every _AGENT_READY_POLL_SECONDS inside the deadline --
# never a fixed wait outside the loop, the same pacing discipline the
# run-command poll above keeps.
_AGENT_READY_POLL_SECONDS = 5.0

# The two instance-view answers the poll waits for, exactly the codes and
# display status the azure VM agent reports.
POWER_RUNNING_CODE = "PowerState/running"
POWER_DEALLOCATED_CODE = "PowerState/deallocated"
AGENT_READY_DISPLAY_STATUS = "Ready"

# The `--query` expressions that turn one `az vm get-instance-view` call
# into the power code, and into the VM agent's display status. Named once,
# here, because the builders below are the only places an instance view is
# read from.
_VM_POWER_QUERY = "instanceView.statuses[?starts_with(code,'PowerState')].code | [0]"
_VM_AGENT_QUERY = "instanceView.vmAgent.statuses[0].displayStatus"


def build_vm_power_argv(resource_group: str, vm_name: str) -> list[str]:
    """The instance-view argv answering the VM's `PowerState/*` code alone."""
    return [
        "vm",
        "get-instance-view",
        "--resource-group",
        resource_group,
        "--name",
        vm_name,
        "--query",
        _VM_POWER_QUERY,
        "--output",
        "tsv",
    ]


def build_vm_agent_argv(resource_group: str, vm_name: str) -> list[str]:
    """The instance-view argv answering the VM agent's display status alone."""
    return [
        "vm",
        "get-instance-view",
        "--resource-group",
        resource_group,
        "--name",
        vm_name,
        "--query",
        _VM_AGENT_QUERY,
        "--output",
        "tsv",
    ]


def vm_power_state(resource_group: str, vm_name: str, runner: CommandRunner) -> str:
    """The VM's `PowerState/*` code, or `""` while the platform reports none yet.

    Raises:
        RelayConfigError: `az` is not on PATH, or the call failed (the
            authentication cause names `az login`).
    """
    result = _run_az(runner, build_vm_power_argv(resource_group, vm_name))
    return result.stdout.strip()


def vm_agent_status(resource_group: str, vm_name: str, runner: CommandRunner) -> str:
    """The VM agent's display status, or `""` while no agent status exists yet.

    Raises:
        RelayConfigError: `az` is not on PATH, or the call failed.
    """
    result = _run_az(runner, build_vm_agent_argv(resource_group, vm_name))
    return result.stdout.strip()


def agent_ready_timeout_seconds() -> float:
    """`AZURE_AGENT_READY_TIMEOUT`, read through the shared hostprobe reader.

    Raises:
        RelayTransportError: the variable is set to something that is not a
            finite positive number of seconds.
    """
    try:
        return read_positive_seconds(
            AZURE_AGENT_READY_TIMEOUT_ENV_VAR, _AZURE_AGENT_READY_TIMEOUT_DEFAULT_SECONDS
        )
    except HostProbeError as exc:
        raise RelayTransportError(
            f"ERROR: {exc}\n"
            "Set it to a positive number of seconds, or unset it to use the default of "
            f"{_AZURE_AGENT_READY_TIMEOUT_DEFAULT_SECONDS:g}."
        ) from exc


def wait_agent_ready(
    resource_group: str,
    vm_name: str,
    runner: CommandRunner,
    *,
    pace: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    deadline_seconds: float | None = None,
) -> None:
    """Poll until the VM reports `PowerState/running` AND its VM agent `Ready`.

    The bounded readiness poll a freshly started azure VM must satisfy
    before anything may drive it -- the azure counterpart of the EC2 path's
    running-then-SSM-Online wait. One small instance-view read every
    `_AGENT_READY_POLL_SECONDS` inside the `AZURE_AGENT_READY_TIMEOUT`
    deadline (passed explicitly by tests, read from the environment
    otherwise); the clock is injected so the tests walk the deadline
    without waiting on anything.

    Args:
        resource_group: the VM's resource group, from the engine address
            record the deploy wrote.
        vm_name: the VM's name, from the same record.
        runner: the `CommandRunner` every az call is issued through.
        pace: the pacer between polls, `time.sleep` in production.
        clock: the monotonic clock the deadline is measured on.
        deadline_seconds: the total budget; read from
            `AZURE_AGENT_READY_TIMEOUT` (default 300) when not given.

    Raises:
        RelayConfigError: an az call failed outright.
        RelayTransportError: the deadline ran out before both answers
            arrived (the message carries the last observed state and names
            the environment variable that widens the budget), or a
            configured deadline is not a positive number of seconds.
    """
    if deadline_seconds is None:
        deadline_seconds = agent_ready_timeout_seconds()
    deadline = clock() + deadline_seconds
    observed = ""
    while True:
        power = vm_power_state(resource_group, vm_name, runner)
        if power == POWER_RUNNING_CODE:
            agent = vm_agent_status(resource_group, vm_name, runner)
            if agent == AGENT_READY_DISPLAY_STATUS:
                return
            observed = agent or "no agent status reported"
        else:
            observed = power or "no power state reported"
        if clock() >= deadline:
            raise RelayTransportError(
                f"ERROR: {vm_name} did not report {POWER_RUNNING_CODE} with its VM agent "
                f"{AGENT_READY_DISPLAY_STATUS!r} within {deadline_seconds:g}s\n"
                f"The last observed state was {observed!r}.\n"
                f"Raise {AZURE_AGENT_READY_TIMEOUT_ENV_VAR} if this instance is simply "
                "slow, and check the instance directly: az vm get-instance-view "
                f"--resource-group {resource_group} --name {vm_name} --output json"
            )
        pace(_AGENT_READY_POLL_SECONDS)


# ---------------------------------------------------------------------------
# RFC 6455: the framing primitives, the handshake head, and the client.
# ---------------------------------------------------------------------------


def websocket_accept_key(client_key: str) -> str:
    """RFC 6455 Section 4.2.2's accept answer: base64(SHA1(key + GUID))."""
    digest = hashlib.sha1((client_key + _WEBSOCKET_GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def mask_payload(payload: bytes, mask: bytes) -> bytes:
    """RFC 6455 Section 5.3's mask: every byte XORed with `mask[i % 4]`."""
    return bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))


def encode_client_frame(
    opcode: int, payload: bytes, *, fin: bool = True, mask: bytes | None = None
) -> bytes:
    """One masked client frame: FIN+opcode header, masked length, mask, masked payload.

    Client-to-server frames are always masked (RFC 6455 Section 5.1); `mask`
    is injectable so the tests can pin the exact bytes of the RFC's own
    worked example, and defaults to a fresh random mask so two identical
    payloads never share one on the wire. `mask=None` means "generate one".
    """
    effective_mask = secrets.token_bytes(4) if mask is None else mask
    first_byte = (0x80 if fin else 0x00) | opcode
    length = len(payload)
    if length <= 125:
        header = struct.pack("!BB", first_byte, 0x80 | length)
    elif length <= 0xFFFF:
        header = struct.pack("!BBH", first_byte, 0x80 | 126, length)
    else:
        header = struct.pack("!BBQ", first_byte, 0x80 | 127, length)
    return header + effective_mask + mask_payload(payload, effective_mask)


def split_wss_url(url: str) -> tuple[str, int, str]:
    """`(host, port, path-with-query)` from a `wss://` URL.

    Raises:
        RelayProtocolError: the URL is not `wss://`, names no host, or is
            otherwise undialable -- a caller bug or a corrupt address
            record, never something to retry.
    """
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "wss" or not parts.hostname:
        raise RelayProtocolError(f"ERROR: not a dialable wss:// URL: {url!r}")
    path = parts.path + (f"?{parts.query}" if parts.query else "")
    return parts.hostname, parts.port or 443, path


def build_handshake_request(host: str, port: int, path: str, key: str, token: str | None) -> str:
    """The HTTP upgrade request head, as one string ready to send.

    Pure so the tests can pin it byte-for-byte: the `ServiceBusAuthorization`
    header appears exactly when a token was given, and the request never
    carries an `sb-hc-id` parameter or any query token -- the sender id
    defect the module docstring records is prevented here, at the only
    place a request head is composed.
    """
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
        lines.append(f"{_AUTH_HEADER_NAME}: {token}")
    return "\r\n".join(lines) + "\r\n\r\n"


def _redact(text: str, secrets_to_scrub: Sequence[str]) -> str:
    """`text` with every secret replaced, so a failure never echoes a token."""
    for secret in secrets_to_scrub:
        if secret:
            text = text.replace(secret, "***")
    return text


def dial_wss(host: str, port: int, timeout_seconds: float) -> socket.socket:
    """The production dial: a TCP connect wrapped in TLS with SNI on `host`."""
    raw = socket.create_connection((host, port), timeout=timeout_seconds)
    try:
        return ssl.create_default_context().wrap_socket(raw, server_hostname=host)
    except BaseException:
        raw.close()
        raise


def _handshake_status(status_line: str) -> int | None:
    """The three-digit status code from an HTTP status line, or `None`."""
    parts = status_line.split()
    if len(parts) >= 2 and parts[1].isdigit():
        return int(parts[1])
    return None


class RelayWebSocket:
    """A minimal RFC 6455 client over one relay connection.

    The handshake sends `build_handshake_request`'s head under the dial
    timeout, verifies the upgrade answer's status and accept key, and then
    returns the socket to fully blocking mode: the pipe idles whenever
    docker idles, and a read blocks until data, close, or death -- never
    on a timer. `send_frame` writes a masked frame, `recv_message` reads
    one complete message (aggregating continuation frames, answering pings
    with pongs), and a peer close is answered with our own close and
    reported as `None`, so the bridge loop can treat it as the normal end
    of a pipe rather than an error.
    """

    def __init__(
        self,
        url: str,
        token: str | None,
        *,
        timeout_seconds: float,
        dial: Callable[[str, int, float], socket.socket] = dial_wss,
    ) -> None:
        self._send_lock = threading.Lock()
        self._buffer = bytearray()
        host, port, path = split_wss_url(url)
        self._socket = dial(host, port, timeout_seconds)
        try:
            self._socket.settimeout(timeout_seconds)
            key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
            request = build_handshake_request(host, port, path, key, token)
            self._socket.sendall(request.encode("ascii"))
            head, rest = self._read_handshake_answer(url, token)
            status_line = head.split(b"\r\n", 1)[0].decode("ascii", errors="replace")
            if " 101" not in status_line:
                raise _HandshakeRefusal(
                    status=_handshake_status(status_line),
                    body=_redact(head.decode("ascii", errors="replace"), [token or ""]),
                )
            expected = websocket_accept_key(key).encode("ascii")
            if expected not in head:
                raise RelayProtocolError(
                    "ERROR: the relay handshake answer carries a bad Sec-WebSocket-Accept value"
                )
            self._buffer.extend(rest)
            self._socket.settimeout(None)
        except BaseException:
            self._socket.close()
            raise

    def _read_handshake_answer(self, url: str, token: str | None) -> tuple[bytes, bytes]:
        """Read until the answer's header block is complete, EOF meaning refusal."""
        buffer = bytearray()
        while b"\r\n\r\n" not in buffer:
            try:
                chunk = self._socket.recv(_PIPE_CHUNK_BYTES)
            except OSError as exc:
                raise _HandshakeRefusal(
                    status=None,
                    body=_redact(f"{type(exc).__name__}: {exc}", [token or ""]),
                ) from exc
            if not chunk:
                raise _HandshakeRefusal(
                    status=None,
                    body=_redact(f"connection closed during handshake for {url}", [token or ""]),
                )
            buffer.extend(chunk)
        head, _, rest = bytes(buffer).partition(b"\r\n\r\n")
        return head, rest

    def _read_exact(self, count: int) -> bytes:
        """Exactly `count` bytes from the socket, buffering what arrives early."""
        while len(self._buffer) < count:
            chunk = self._socket.recv(_PIPE_CHUNK_BYTES)
            if not chunk:
                raise ConnectionError("the relay connection closed")
            self._buffer.extend(chunk)
        out = bytes(self._buffer[:count])
        del self._buffer[:count]
        return out

    def recv_frame(self) -> tuple[bool, int, bytes]:
        """One frame: `(fin, opcode, payload)`, server masks removed if present.

        A frame whose header declares more than `_MAX_FRAME_BYTES` is
        refused before any payload byte is read, naming the declared size.

        Raises:
            RelayProtocolError: the declared length exceeds the ceiling.
        """
        first, second = self._read_exact(2)
        fin = bool(first & 0x80)
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F
        if length == 126:
            (length,) = struct.unpack("!H", self._read_exact(2))
        elif length == 127:
            (length,) = struct.unpack("!Q", self._read_exact(8))
        if length > _MAX_FRAME_BYTES:
            raise RelayProtocolError(
                f"ERROR: the relay connection declared a {length}-byte frame, above the "
                f"{_MAX_FRAME_BYTES}-byte ceiling this reader buffers"
            )
        mask = self._read_exact(4) if masked else b""
        payload = self._read_exact(length) if length else b""
        if masked:
            payload = mask_payload(payload, mask)
        return fin, opcode, payload

    def send_frame(self, opcode: int, payload: bytes, *, fin: bool = True) -> None:
        """One masked frame, under the send lock so bridge threads cannot interleave."""
        frame = encode_client_frame(opcode, payload, fin=fin)
        with self._send_lock:
            self._socket.sendall(frame)

    def send_close(self, code: int = _CLOSE_NORMAL) -> None:
        """A close frame, tolerating an already-dead socket."""
        try:
            self.send_frame(_OP_CLOSE, struct.pack("!H", code))
        except OSError:
            pass

    def recv_message(self) -> tuple[int, bytes] | None:
        """The next complete text/binary message, or `None` once the peer closed.

        Pings are answered with pongs and never surfaced; unsolicited pongs
        are dropped; a peer close is answered with our own normal close
        before `None` is returned, so both halves of the pipe wind down
        together the way the relay service propagates them.
        """
        fragments = bytearray()
        fragment_opcode: int | None = None
        while True:
            fin, opcode, payload = self.recv_frame()
            if opcode == _OP_PING:
                self.send_frame(_OP_PONG, payload)
                continue
            if opcode == _OP_PONG:
                continue
            if opcode == _OP_CLOSE:
                self.send_close(_CLOSE_NORMAL)
                return None
            if opcode in (_OP_TEXT, _OP_BINARY):
                fragment_opcode = opcode
                fragments = bytearray(payload)
            elif opcode == _OP_CONTINUATION and fragment_opcode is not None:
                fragments.extend(payload)
            else:
                raise RelayProtocolError(
                    f"ERROR: the relay connection sent an unexpected frame "
                    f"(opcode {opcode}, fin {fin})"
                )
            if fin and fragment_opcode is not None:
                return fragment_opcode, bytes(fragments)

    def close(self) -> None:
        """Close the raw socket, tolerating a peer that got there first."""
        try:
            self._socket.close()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# The bridge: one loopback connection, one relay sender, both directions.
# ---------------------------------------------------------------------------


def relay_dial_timeout_seconds() -> float:
    """`RELAY_DIAL_TIMEOUT`, read fresh, exactly once per dial."""
    try:
        return read_positive_seconds(
            RELAY_DIAL_TIMEOUT_ENV_VAR, _RELAY_DIAL_TIMEOUT_DEFAULT_SECONDS
        )
    except HostProbeError as exc:
        raise RelayTransportError(
            f"ERROR: {exc}\n"
            "Set it to a positive number of seconds, or unset it to use the default of "
            f"{_RELAY_DIAL_TIMEOUT_DEFAULT_SECONDS:g}."
        ) from exc


def open_relay_sender(
    token_cache: TokenCache,
    *,
    namespace: str,
    hybrid_connection: str,
    instance: str,
    timeout_seconds: float,
) -> RelayWebSocket:
    """One fresh relay sender WebSocket, with the one-retry 401 discipline.

    The token is taken from the cache (refreshed inside it when near
    expiry); a 401 means the cached token was rejected, so the cache is
    invalidated and exactly one fresh-token attempt is made before the
    handshake refusal propagates. Any other refusal is permanent for this
    connection and propagates at once -- the bridge logs it and the daemon
    keeps serving, per-connection isolation being the accept loop's rule.
    This is also where the raw refusal becomes the diagnosed
    `RelayHandshakeError`, the only place in the module that knows the
    instance the remedy should name.
    """
    url = relay_endpoint(namespace, hybrid_connection, _ACTION_CONNECT)
    try:
        return _dial_sender(url, token_cache.get(), timeout_seconds=timeout_seconds)
    except _HandshakeRefusal as exc:
        if exc.status != 401:
            raise RelayHandshakeError(status=exc.status, body=exc.body, instance=instance) from exc
        token_cache.invalidate()
        try:
            return _dial_sender(url, token_cache.get(), timeout_seconds=timeout_seconds)
        except _HandshakeRefusal as retry_exc:
            raise RelayHandshakeError(
                status=retry_exc.status, body=retry_exc.body, instance=instance
            ) from retry_exc


def _dial_sender(url: str, token: str, *, timeout_seconds: float) -> RelayWebSocket:
    """One sender dial, as `open_relay_sender`'s single attempt shape."""
    return RelayWebSocket(url, token, timeout_seconds=timeout_seconds)


class _Bridge:
    """One coupled pipe: the accepted loopback socket and its relay sender.

    The half-close contract: either direction may end while the other keeps
    running, which is what `docker exec -i` needs (stdin reaching EOF must
    not kill the stdout still flowing back). The TCP side's EOF is forwarded
    as ONE empty-payload binary frame -- never a close frame, and never a
    shutdown of the loopback socket, whose read side IS the stdout direction
    the CLI still reads. An empty binary frame received from the peer means
    the remote stdout ended: the loopback write half shuts down (EOF toward
    the CLI) and the reverse direction keeps running. `finish` closes both
    ends exactly once, on a peer close frame, both directions having ended,
    a socket error, or a daemon stop. The two Events guard the
    one-sentinel-per-direction and one-shutdown-per-socket invariants
    against the two threads sharing the bridge. The registry holds open
    bridges so a stop can close every one of them.
    """

    def __init__(self, client_socket: socket.socket, websocket: RelayWebSocket) -> None:
        self._client_socket = client_socket
        self._websocket = websocket
        self._finished = threading.Event()
        self._eof_forwarded = threading.Event()
        self._tcp_write_shut = threading.Event()

    def forward_tcp_eof(self) -> None:
        """Send the in-band EOF sentinel toward the relay peer, exactly once.

        One empty-payload binary frame, never a close frame: a close would
        end the whole relay connection, killing the stdout direction that
        may still be mid-flight.
        """
        if self._eof_forwarded.is_set():
            return
        self._eof_forwarded.set()
        self._websocket.send_frame(_OP_BINARY, b"")

    def shut_tcp_write(self) -> None:
        """Half-close the loopback socket's write side, exactly once.

        EOF toward the local docker CLI while its socket's read side -- the
        stdout direction this bridge keeps pumping -- stays open.
        """
        if self._tcp_write_shut.is_set():
            return
        self._tcp_write_shut.set()
        try:
            self._client_socket.shutdown(socket.SHUT_WR)
        except OSError:
            pass

    def note_peer_eof(self) -> bool:
        """Record the peer's in-band EOF; True once both directions have ended.

        The peer's empty-payload sentinel means its TCP side read EOF -- the
        remote stdout has ended. Combined with a forwarded EOF of our own
        (the local stdin has ended), no more data can flow in either
        direction, so the caller ends the pipe; before that moment the
        reverse direction stays open, which is the `docker exec -i`
        contract.
        """
        self.shut_tcp_write()
        return self._eof_forwarded.is_set()

    def finish(self) -> None:
        """Close both ends once; the close frame is best-effort after a dead pipe."""
        if self._finished.is_set():
            return
        self._finished.set()
        self._websocket.send_close(_CLOSE_NORMAL)
        self._websocket.close()
        try:
            self._client_socket.close()
        except OSError:
            pass


class _BridgeRegistry:
    """The daemon's open pipes, so a stop closes every one of them."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._bridges: set[_Bridge] = set()

    def add(self, bridge: _Bridge) -> None:
        with self._lock:
            self._bridges.add(bridge)

    def remove(self, bridge: _Bridge) -> None:
        with self._lock:
            self._bridges.discard(bridge)

    def close_all(self) -> None:
        with self._lock:
            bridges = list(self._bridges)
            self._bridges.clear()
        for bridge in bridges:
            bridge.finish()


def _pump_tcp_to_websocket(
    bridge: _Bridge, client_socket: socket.socket, websocket: RelayWebSocket
) -> None:
    """The TCP-to-relay direction: every byte the docker CLI writes becomes one frame.

    The docker CLI's stdin reaching EOF is not this direction's death: it
    forwards the EOF in-band once (the empty-payload sentinel) and this
    thread ends WITHOUT `finish` and without touching the loopback socket
    -- its read side is the stdout direction the CLI still reads, and its
    write half closes when the peer's own sentinel arrives. Whatever ends
    the pipe (a peer close frame, both directions ended, a socket error, a
    daemon stop) is what tears it down. A socket error, by contrast, is a
    dead pipe: `finish` closes both ends.
    """
    try:
        while True:
            data = client_socket.recv(_PIPE_CHUNK_BYTES)
            if not data:
                bridge.forward_tcp_eof()
                return
            websocket.send_frame(_OP_BINARY, data)
    except OSError:
        bridge.finish()


def bridge_connection(
    client_socket: socket.socket,
    *,
    sender_opener: Callable[[], RelayWebSocket],
    registry: _BridgeRegistry,
    namespace: str,
) -> None:
    """Serve one accepted loopback connection over a fresh relay sender.

    A failure here is this connection's failure only: it is logged to stderr
    (the daemon log, under `make connect-start`) and both ends are closed,
    while the daemon's accept loop goes on serving. The relay-to-TCP
    direction runs in this thread; the TCP-to-relay direction runs in its
    own, so either side's close tears both down through `finish`. An empty
    binary message from the relay is the peer's in-band EOF: the loopback
    write side is half-closed (EOF toward docker) and this loop keeps
    reading, because the reverse direction is still live.
    """
    websocket: RelayWebSocket | None = None
    bridge: _Bridge | None = None
    try:
        websocket = sender_opener()
        bridge = _Bridge(client_socket, websocket)
        registry.add(bridge)
        threading.Thread(
            target=_pump_tcp_to_websocket, args=(bridge, client_socket, websocket), daemon=True
        ).start()
        while True:
            message = websocket.recv_message()
            if message is None:
                break
            opcode, payload = message
            if payload:
                client_socket.sendall(payload)
            elif opcode == _OP_BINARY and bridge.note_peer_eof():
                # Both directions have signaled EOF: the pipe's work is
                # done, and the break reaches `finish` through the finally.
                break
    except (transport.TransportError, OSError) as exc:
        print(f"relay: a connection through {namespace} failed: {exc}", file=sys.stderr, flush=True)
    finally:
        if bridge is not None:
            registry.remove(bridge)
            bridge.finish()
        else:
            # The sender never opened: only the accepted socket is ours to close.
            try:
                client_socket.close()
            except OSError:
                pass


def _accept_loop(
    acceptor: socket.socket,
    *,
    sender_opener: Callable[[], RelayWebSocket],
    registry: _BridgeRegistry,
    namespace: str,
) -> None:
    """Accept and bridge forever; only the stop signal (or acceptor close) ends it.

    Every accepted connection is handed to its own daemon thread, so one
    failing connection can never take the daemon down. The loop blocks in
    `accept()` -- a real I/O wait, never a poll -- and a stop arrives as
    the signal handler's exception, raised into this thread while it
    blocks. An `OSError` out of `accept()` on a bound, listening loopback
    socket is the acceptor being closed under this loop (macOS wakes the
    blocked accept with one; Linux closes it quietly) -- the teardown
    path, answered by returning, never by crashing the serving thread.
    """
    while True:
        try:
            client_socket, _ = acceptor.accept()
        except OSError:
            return
        threading.Thread(
            target=bridge_connection,
            args=(client_socket,),
            kwargs={
                "sender_opener": sender_opener,
                "registry": registry,
                "namespace": namespace,
            },
            daemon=True,
        ).start()


class _StopServing(BaseException):
    """Raised by the SIGTERM handler into the accept loop; the clean-stop signal."""


def serve_until_stopped(
    acceptor: socket.socket,
    *,
    sender_opener: Callable[[], RelayWebSocket],
    registry: _BridgeRegistry,
    namespace: str,
) -> None:
    """Serve the acceptor until SIGINT or SIGTERM, then close every pipe.

    SIGINT arrives as the interpreter's own `KeyboardInterrupt` inside the
    blocking `accept()`; SIGTERM is trapped into `_StopServing`, raised the
    same way, so both interrupt paths land in the same clean stop: the
    registry closes every open pipe, the caller's `finally` closes the
    acceptor, and the daemon exits 0 -- which is what
    `devcontainer_config.forwards`'s SIGINT-first process-group stop relies
    on. The previous SIGTERM handler is restored on the way out.
    """

    def _raise_stop(signum: int, frame: object) -> NoReturn:
        del signum, frame  # the signal's identity does not change the stop
        raise _StopServing

    previous = signal.signal(signal.SIGTERM, _raise_stop)
    try:
        _accept_loop(acceptor, sender_opener=sender_opener, registry=registry, namespace=namespace)
    except (_StopServing, KeyboardInterrupt):
        pass
    finally:
        registry.close_all()
        signal.signal(signal.SIGTERM, previous)


# ---------------------------------------------------------------------------
# The shared flow: the address record, the context, the acceptor.
# ---------------------------------------------------------------------------


def _validated_pair(name: str, context_name: str) -> Path:
    """The repo root, after both names check out against each other.

    The instance name is validated through `instances.validate_name` (it
    keys the address record and the certificate directory at once), and the
    context must be exactly the context `instances.docker_context` derives
    for it: the certificate paths `ensure_context` writes into the context
    belong to the instance, so a mismatched pair would aim one instance's
    daemon at another's context. The same derivation the Makefile and
    container.sh pass, checked rather than assumed.

    Raises:
        RelayConfigError: either check fails.
    """
    try:
        instances.validate_name(name)
    except instances.InvalidInstanceNameError as exc:
        raise RelayConfigError(str(exc)) from exc
    root = repo.find_root(Path.cwd())
    expected_context = instances.docker_context(root, name)
    if context_name != expected_context:
        raise RelayConfigError(
            f"ERROR: docker context {context_name!r} does not name instance {name!r}\n"
            f"The relay transport manages {expected_context!r}, the context "
            f"instances.docker_context derives for {name!r}.\n"
            f"Pass that context, or connect with: make connect-start INSTANCE={name}"
        )
    return root


def _bind_acceptor(port: int) -> socket.socket:
    """The loopback acceptor bound to `127.0.0.1:<port>` and listening."""
    acceptor = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        acceptor.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        acceptor.bind((_LOCALHOST, port))
        acceptor.listen(_ACCEPT_BACKLOG)
    except OSError as exc:
        acceptor.close()
        raise RelayTransportError(
            f"ERROR: the relay daemon could not bind 127.0.0.1:{port}\n"
            f"{exc}\n"
            "The port was just allocated or reused for this context; free it and retry."
        ) from exc
    return acceptor


def _use_context(runner: CommandRunner, context_name: str) -> None:
    """Activate `context_name` as the docker CLI's active context.

    The daemon a subsequent `make build` targets is whichever context is
    active, so the context this module just created or updated is activated
    here rather than left to a caller who might not know it is required --
    the same call the other two transports make. Raises naming the context
    and the underlying docker stderr, never a bare exit code.
    """
    result = runner(("docker", "context", "use", context_name), None)
    if result.binary_missing:
        raise RelayTransportError(
            "ERROR: docker is not on PATH\nInstall Docker Desktop or the Docker CLI, then retry."
        )
    if result.exit_code != 0:
        raise RelayTransportError(
            f"ERROR: docker context use {context_name!r} failed\n"
            f"{result.stderr.strip()}\n"
            "The context was created or updated successfully; only activating it failed. "
            f"Run 'docker context use {context_name}' by hand, then retry."
        )


def _common_setup(name: str, context_name: str) -> tuple[str, str, int, CommandRunner]:
    """Steps 1-5 of the connect flow: the record, the checks, the port, the context.

    Shared by `connect` and `verify` (DRY): load the relay address record,
    check the context pair, allocate the local port through the
    record-is-the-context rule, then satisfy the client-certificate
    precondition and refuse a legacy `ssh://` context while creating or
    updating the mTLS context at that port -- both inside
    `transport.ensure_context`, the single implementation of that flow this
    module reuses rather than a second copy of the certificate check, the
    legacy refusal, or the context argv builders.

    Returns:
        `(namespace, hybrid_connection, local_port, runner)`.
    """
    address = load_relay_address(name)
    namespace, hybrid_connection = relay_values(address, instance=name)
    root = _validated_pair(name, context_name)
    runner = transport.subprocess_command_runner
    local_port = transport.allocate_local_port(runner, context_name)
    transport.ensure_context(
        runner,
        instance=name,
        root=root,
        port=local_port,
        reference_time=datetime.datetime.now(datetime.UTC),
        # Read fresh rather than `certs.DEFAULT_CERTS_ROOT`'s import-time
        # snapshot: the address record above is read through the same
        # `instances.certs_root()` derivation, and one DOCKER_CONFIG must
        # govern both reads.
        certs_root=instances.certs_root(),
    )
    return namespace, hybrid_connection, local_port, runner


def _sender_opener(
    runner: CommandRunner, *, namespace: str, hybrid_connection: str, instance: str
) -> Callable[[], RelayWebSocket]:
    """The accept loop's sender factory: a fresh sender per call, one 401 retry each.

    Each call mints or reuses a token through the shared cache and dials a
    fresh sender WebSocket, so every coupled pipe carries its own
    connection, as the wire contract requires; the dial timeout is read
    fresh per dial from `RELAY_DIAL_TIMEOUT`.
    """
    token_cache = TokenCache(runner)

    def open_one() -> RelayWebSocket:
        return open_relay_sender(
            token_cache,
            namespace=namespace,
            hybrid_connection=hybrid_connection,
            instance=instance,
            timeout_seconds=relay_dial_timeout_seconds(),
        )

    return open_one


def _run_connect(args: argparse.Namespace) -> int:
    """Handler for `connect`: the azure relay daemon's entry point.

    Runs the shared setup, ensures the VM-side listener is running (it
    self-stops when idle, so a forward reopened after a gap reaches a unit
    that is not active), activates the context, binds the loopback
    acceptor, prints the `Connected:` marker `devcontainer_config.forwards`
    polls for -- only after the listener start succeeded, the acceptor is
    bound and the context is usable, never before -- and serves until
    SIGINT/SIGTERM. Every socket this handler opened is closed on the way
    out, including on any failure after the acceptor exists.
    """
    namespace, hybrid_connection, local_port, runner = _common_setup(args.name, args.context)
    start_listener(runner, instance=args.name)
    _use_context(runner, args.context)
    acceptor = _bind_acceptor(local_port)
    try:
        print(
            f"Connected: docker context {args.context!r} -> {args.name} over azure relay "
            f"(relay {namespace}).",
            flush=True,
        )
        serve_until_stopped(
            acceptor,
            sender_opener=_sender_opener(
                runner, namespace=namespace, hybrid_connection=hybrid_connection, instance=args.name
            ),
            registry=_BridgeRegistry(),
            namespace=namespace,
        )
    finally:
        acceptor.close()
    return 0


def _parse_server_payload(stdout: str, context_name: str) -> tuple[str, str]:
    """The `docker version --format {{json .Server}}` payload's two fields.

    An independent copy of the other transports' own parser by the same
    design that keeps each transport's diagnoses module-local: the refusal
    names this transport's context, not another's.
    """
    try:
        payload = json.loads(stdout)
        return str(payload["Version"]), str(payload["ApiVersion"])
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise RelayTransportError(
            f"ERROR: docker version for context {context_name!r} produced unparsable "
            f"output: {stdout.strip()!r}\n"
            "Run the command manually and investigate why its output does not match the "
            "expected shape."
        ) from exc


def _handshake_budget_seconds() -> float:
    """`DOCKER_HANDSHAKE_TIMEOUT`, read through the shared hostprobe reader.

    The variable's name, default and validation live in `hostprobe`; the
    verify path's version call is bounded by the same budget the other
    transports' handshakes honor, rather than a second, independently
    drifting copy of the name and default.
    """
    try:
        return read_positive_seconds(
            DOCKER_HANDSHAKE_TIMEOUT_ENV_VAR, DOCKER_HANDSHAKE_TIMEOUT_DEFAULT_SECONDS
        )
    except HostProbeError as exc:
        raise RelayTransportError(
            f"ERROR: {exc}\n"
            "Set it to a positive number of seconds, or unset it to use the default of "
            f"{DOCKER_HANDSHAKE_TIMEOUT_DEFAULT_SECONDS:g}."
        ) from exc


def _run_verify(args: argparse.Namespace) -> int:
    """Handler for `verify`: a one-shot reachability proof through the relay.

    Runs the shared setup, activates the context, binds the loopback
    acceptor, and serves it in a background thread while one real
    `docker --context ... version` call dials the bound port: the acceptor
    couples that call to a fresh relay sender, so a passing verdict proves
    the whole path -- loopback acceptor, relay service, VM listener, VM
    docker daemon. Every socket (acceptor, bridges) is closed on every path
    out, and nothing is left listening: the context survives as the record
    of the port allocation, exactly as the other transports' verify paths
    leave it.

    The serving thread is a daemon thread and is deliberately not joined:
    the acceptor and every bridge are closed before this handler returns,
    and the interpreter's own shutdown reaps the thread when the process
    exits -- a join could hang forever on the platforms where closing a
    listening socket does not wake a blocked `accept()`, a risk the
    one-shot must not carry.
    """
    namespace, hybrid_connection, local_port, runner = _common_setup(args.name, args.context)
    _use_context(runner, args.context)
    acceptor = _bind_acceptor(local_port)
    registry = _BridgeRegistry()
    try:
        threading.Thread(
            target=_accept_loop,
            args=(acceptor,),
            kwargs={
                "sender_opener": _sender_opener(
                    runner,
                    namespace=namespace,
                    hybrid_connection=hybrid_connection,
                    instance=args.name,
                ),
                "registry": registry,
                "namespace": namespace,
            },
            daemon=True,
        ).start()
        version_command = (
            "docker",
            "--context",
            args.context,
            "version",
            "--format",
            "{{json .Server}}",
        )
        result = runner(version_command, _handshake_budget_seconds())
        if result.binary_missing:
            raise RelayTransportError(
                "ERROR: docker is not on PATH\n"
                "Install Docker Desktop or the Docker CLI, then retry."
            )
        if result.exit_code != 0:
            raise RelayTransportError(
                f"ERROR: docker version handshake for context {args.context!r} failed\n"
                f"{result.stderr.strip()}\n"
                "Confirm the relay listener is running on the instance and the daemon "
                "behind it answers, then retry."
            )
        _server_version, api_version = _parse_server_payload(result.stdout, args.context)
    finally:
        acceptor.close()
        registry.close_all()
    print(f"Verified: docker context {args.context!r} -> relay {namespace} (server {api_version}).")
    return 0


def _run_install_material(args: argparse.Namespace) -> int:
    """Handler for `install-material`: one run command pulls the material.

    The instance pulls; nothing is pushed: the run command carries the
    script that fetches the vault's secrets and never a value, so no TLS
    private key rides the command document or azure's retained command
    history.
    """
    install_material(
        transport.subprocess_command_runner,
        instance=args.instance,
        daemon_user=args.daemon_user,
    )
    print(f"Installed TLS material on {args.instance} and started its daemon.")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="devcontainer_config.transport_azure",
        description=(
            "Azure Relay transport: the hybrid-connection tunnel carrying the docker API "
            "to an azure engine's relay listener."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    connect_parser = subparsers.add_parser(
        "connect",
        help=(
            "Load the relay address record, create/update the mTLS docker context behind "
            "the relay, and serve until interrupted."
        ),
    )
    connect_parser.add_argument(
        "--name", required=True, help="The instance name (the remote-instances/<name> directory)."
    )
    connect_parser.add_argument("--context", required=True, help="The docker context name.")
    connect_parser.set_defaults(handler=_run_connect)

    verify_parser = subparsers.add_parser(
        "verify",
        help=(
            "One-shot reachability proof: open the relay pipe, run docker version through "
            "it, tear everything down. Leaves the context created."
        ),
    )
    verify_parser.add_argument(
        "--name", required=True, help="The instance name (the remote-instances/<name> directory)."
    )
    verify_parser.add_argument("--context", required=True, help="The docker context name.")
    verify_parser.set_defaults(handler=_run_verify)

    install_material_parser = subparsers.add_parser(
        "install-material",
        help=(
            "Have the azure VM pull its published TLS material from its Key Vault "
            "and start its daemon, through one run command."
        ),
    )
    install_material_parser.add_argument(
        "--instance",
        required=True,
        help="The instance name (the remote-instances/<name> directory).",
    )
    install_material_parser.add_argument(
        "--daemon-user", required=True, help="The VM's rootless daemon user (REMOTE_DAEMON_USER)."
    )
    install_material_parser.set_defaults(handler=_run_install_material)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Parse `argv`, run the selected command, and return an exit code.

    Every handler raises a `RelayTransportError` (or one of its sibling
    `transport.TransportError` subclasses) on a real failure instead of
    exiting itself; this is the one place that exception becomes a
    non-zero exit code, printed with an `ERROR:` prefix to stderr, never a
    stack trace. `repo.RepoError` is caught here the same way
    `devcontainer_config.transport.main` catches it, since the handlers
    resolve the repository root the same way.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except (transport.TransportError, repo.RepoError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
