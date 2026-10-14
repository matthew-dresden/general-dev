"""GCP IAP transport: the `gcloud start-iap-tunnel` daemon carrying the docker API.

This module is the fourth transport `make connect-start` opens, routed by
the engine's own kind, beside `devcontainer_config.transport`'s SSM port
forward, `devcontainer_config.transport_ssh`'s plain `ssh -N -L` forward,
and `devcontainer_config.transport_azure`'s relay daemon. Where those reach
an EC2 instance over SSM, a generic engine over ssh, and an azure VM over a
hybrid connection, this one reaches a gcp virtual machine over Identity-Aware
Proxy: `gcloud compute start-iap-tunnel` dials the VM's nic0 internal IPv4
through google's IAP front end, and the local half of that tunnel is a
loopback listener gcloud itself binds and multiplexes -- one listener serves
every concurrent docker API connection the daemon's clients open, a property
the live spike verified past eight simultaneous connections, so there is no
per-connection acceptor here and no bridge to write.

The wire contract is the one the live spike proved end to end, and this
module is deliberately literal about it:

- The tunnel is gcloud's process, start to finish. This module composes one
  argument vector (`gcloud compute start-iap-tunnel <vm> 2376
  --local-host-port=127.0.0.1:<port> --zone <zone> --project <project_id>`),
  spawns it through the injected launcher seam, and supervises it; there is
  no token cache, no RFC 6455 framing, and no stdin bridging here, because
  none of the relay's problems are this transport's: gcloud owns the IAP
  handshake, the token refresh, and the listener.
- gcloud announces its own startup ("Testing if tunnel connection works."
  then "Listening on port [N]."), but readiness is never parsed from those
  lines: it is the local port accepting a real TCP connection, the same
  decided-by-a-connection-attempt contract every other transport keeps.
  Parsing a log line would couple this module to gcloud's wording; a
  connect attempt answers the only question that matters.
- IAP reaches the VM's nic0 internal address, so the docker daemon binds
  `0.0.0.0:2376` behind a VPC whose sole ingress rule is
  `35.235.240.0/20 -> tcp:2376` (the IAP range; documented doctrine the gcp
  layer enforces), and the mTLS docker context carries the client
  authentication -- the tunnel is transport, the certificates are trust.
- IAP drops idle sessions at roughly one hour, and gcloud reestablishes the
  listener on its own; the daemon's job during such a drop is simply to
  keep the process alive, which supervision below does.
- Half-close caveat, verified live: return traffic sent after a client's
  FIN is not guaranteed to arrive. A protocol that half-closes and then
  waits for a final reply (some TLS renegotiations, some database drivers)
  may see that reply lost. Docker's HTTP/1.1 and gRPC traffic closes
  cleanly after the response it needed, so the daemon carries the caveat
  here rather than working around it -- there is no workaround below the
  tunnel gcloud owns.

`connect` mirrors the other transports' connect flow with a supervised child
in place of their tunnel machinery: it loads the instance's engine address
record (`instances.engine_address`, the file `make instance-deploy` writes),
validates the record's `instance_name` against the gcp name sanitizer's
shape and its `zone`/`project_id` against the strict argument shapes (fail
fast naming the field, the record file and the deploy that rewrites it),
reuses `transport.ensure_context` wholesale for the client-certificate
freshness precondition, the refusal of an existing `ssh://` legacy context,
and the mTLS context create-or-update at `tcp://127.0.0.1:<port>` -- one
implementation of that flow, not a second copy of its certificate check,
its legacy refusal or its context argv builders. The port comes from
`transport.allocate_local_port` (the same record-is-the-context rule, so
the context stays stable across reconnects), the context is activated with
`docker context use`, the tunnel child is spawned, the readiness poll waits
-- bounded by `GCP_TUNNEL_CONNECT_TIMEOUT` (default 60 seconds, read through
`hostprobe.read_positive_seconds`), paced by a bounded selector wait like
`transport_ssh`'s, never a bare sleep -- for the local port to accept a
connection, and only then prints the `Connected:` line
`devcontainer_config.forwards` polls for. The daemon then supervises until
SIGINT/SIGTERM: a child that exits is a dropped tunnel, and its error
carries the child's stderr tail classified into the cause -- an expired
gcloud session names `gcloud auth login` (then a forward refresh), a
refused or unreachable VM names `make instance-start` -- while an operator
interrupt tears the child down, verifies the port actually closed, and
exits 0. The child's stderr is drained continuously by a reader thread
(gcloud writes its progress and errors there; a full pipe would wedge the
child) whose lines are scrubbed and echoed, and its most recent lines are
kept for those diagnoses; no credential is known to ride that stream, and
the scrub keeps it that way by construction.

`verify` is the one-shot reachability proof: the same setup, the same
spawn and readiness wait, one real `docker --context ... version --format
{{json .Server}}` call through the freshly ensured context, the verdict
line, and the child torn down on every path out -- never a tunnel left
behind. The context is left created (it is the record of the port
allocation, and the next connect reuses the same port through it) but
never activated: a proof must not switch the machine-wide docker context
as a side effect, the same rule the ssh transport's verify applies.

gcloud authenticates ambiently (`gcloud auth login`); there is no token
cache to refresh here because gcloud holds the token. When the ambient
session expires, the child exits and the diagnosis above names the login
and the refresh remedy.

Every docker invocation is issued through an injected
`devcontainer_config.hostprobe.CommandRunner`, and the child through an
injected `ProcessLauncher`; `transport.subprocess_command_runner` is this
module's production runner and `default_process_launcher` below its
production launcher, so the unit suite drives every path with fakes and
never runs a real `gcloud`, `docker`, or network dial -- the one socket
work the tests do is loopback, the same allowance `tests/test_transport.py`
uses for port probes.
"""

from __future__ import annotations

import argparse
import collections
import datetime
import json
import re
import selectors
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import NoReturn

from devcontainer_config import certs, instances, repo, transport
from devcontainer_config.hostprobe import (
    DOCKER_HANDSHAKE_TIMEOUT_DEFAULT_SECONDS,
    DOCKER_HANDSHAKE_TIMEOUT_ENV_VAR,
    CommandRunner,
    HostProbeError,
    read_positive_seconds,
)

GCLOUD_BINARY = "gcloud"

# The docker API port the tunnel dials on the VM: the daemon binds 0.0.0.0
# there behind the IAP-only ingress rule, and the local end is whatever
# `transport.allocate_local_port` allocated for the context.
_TUNNEL_TARGET_PORT = "2376"

# ---------------------------------------------------------------------------
# The boot-applied install contract (shared with slice 5's startup script).
# The VM's startup script fetches the TLS secrets and the checksum secret
# on every boot, applies the material only on drift, restarts the daemon,
# and records what it did in guest attributes under the namespace below:
# `tls-checksum` carries the checksum of the material it applied (written
# only after the apply and the daemon restart succeeded), `provisioned`
# carries TRUE once a boot finished its success path at all (material or
# not -- a first boot that precedes the first publish is a success), and
# `error` carries the last failure's diagnostic. Every wait this module
# exposes gates on these attributes, never on `instances describe`'s
# status alone: a VM that reports RUNNING has merely been scheduled, and
# the marker attributes are the only evidence the provisioning the
# listener needs actually ran.
# ---------------------------------------------------------------------------

GUEST_ATTRIBUTE_NAMESPACE = "general-dev"
GUEST_ATTRIBUTE_CHECKSUM_KEY = "tls-checksum"
GUEST_ATTRIBUTE_PROVISIONED_KEY = "provisioned"
_GUEST_ATTRIBUTE_ERROR_KEY = "error"
GUEST_ATTRIBUTE_PROVISIONED_VALUE = "TRUE"

# The deadline the material-apply wait may run to: a reboot plus a startup
# script that fetches four secrets, rewrites material and restarts the
# daemon, bounded and configurable the same way every other wait in this
# package is (read through `hostprobe.read_positive_seconds`).
MATERIAL_APPLY_TIMEOUT_ENV_VAR = "GCP_MATERIAL_APPLY_TIMEOUT"
_MATERIAL_APPLY_TIMEOUT_DEFAULT_SECONDS = 900.0

# The deadline the provisioned-marker wait (the start path's readiness
# gate) may run to -- the gcp counterpart of the azure agent-ready wait's
# `AZURE_AGENT_READY_TIMEOUT` (default 300), read through the same shared
# reader.
PROVISIONED_TIMEOUT_ENV_VAR = "GCP_PROVISIONED_TIMEOUT"
_PROVISIONED_TIMEOUT_DEFAULT_SECONDS = 300.0

# One attribute read every this many seconds inside either deadline --
# never a fixed wait outside the loop, the same pacing discipline the
# azure poll keeps.
_GUEST_ATTRIBUTE_POLL_SECONDS = 5.0

# The gcloud command this module's attribute and secret reads are issued
# through, declared once beside the runner seam below.
GCLOUD_GUEST_ATTRIBUTES_OP = "instances get-guest-attributes"

_TUNNEL_CONNECT_TIMEOUT_ENV_VAR = "GCP_TUNNEL_CONNECT_TIMEOUT"
_TUNNEL_CONNECT_TIMEOUT_DEFAULT_SECONDS = 60.0

_LOCALHOST = "127.0.0.1"

# The pace of the readiness probes against the local port. The total wait
# is bounded by `GCP_TUNNEL_CONNECT_TIMEOUT`; between probes this module
# blocks on a selector for at most this many seconds (a bounded I/O-wait,
# never a sleep), the same cadence shape `devcontainer_config.transport_ssh`
# paces its own probes with. gcloud's tunnel came up in about two seconds
# live, so the default budget is generous headroom, not the expected case.
_TUNNEL_POLL_SECONDS = 0.5

# How long a stopped child's stderr reader may be given to flush its last
# lines into the tail before a diagnosis is built from it. A bound on one
# join, never a synchronization mechanism: the reader hits EOF the moment
# the child's pipe closes, so the wait is normally zero.
_STDERR_TAIL_JOIN_SECONDS = 2.0

# How many recent stderr lines the tail keeps for a child-death diagnosis.
# gcloud's failure narratives are short; this is display headroom, not
# storage.
_STDERR_TAIL_LINES = 50

# The stderr line shapes scrubbed before they are echoed or kept. No
# credential is known to ride gcloud's stderr on any flow this transport
# drives -- the token lives inside gcloud -- and the scrub keeps an
# unexpected bearer-shaped line from ever reaching a log or an error.
_REDACT_PATTERN = re.compile(r"(?i)(authorization:\s*bearer\s+)\S+")

# Every C0/C1 control byte (DEL included), the class escape sequences open
# with. The VM-writable `general-dev/error` guest attribute is stripped of
# this class at its one read site before any error message renders it, so
# an attribute an attacker wrote can decorate a diagnosis but never move a
# cursor: the class is removed, and the callers quote what remains.
_CONTROL_BYTES_PATTERN = re.compile(r"[\x00-\x1f\x7f-\x9f]")

# The child-exit classifications, matched case-insensitively against the
# stderr tail. The auth set is the expired-ambient-session wording gcloud's
# own help names (`gcloud auth login`) plus its consistent classmates; the
# terminated-VM set is the refused/unreachable wording an IAP dial answers
# with when the VM is deallocated or its firewall does not answer. A tail
# matching neither class carries the generic retry remedy.
_AUTH_FAILURE_MARKERS = (
    "your credentials have expired",
    "reauthentication required",
    "could not obtain an access token",
    "no active account",
)
_TERMINATED_VM_MARKERS = (
    "permission denied",
    "could not connect",
)


class GCPTransportError(transport.TransportError):
    """Base for every operational failure this module raises.

    Every failure this module can produce is a `GCPTransportError` (or a
    more specific subclass below), and every one of those is also a
    `transport.TransportError`, so a caller that handles the transports
    uniformly can catch the ssm module's base class across all of them.
    """


class GCPConfigError(GCPTransportError):
    """The instance's engine address record or local inputs are unusable.

    Raised for a missing or malformed `engine-address.json` (the record
    `make instance-deploy` writes), a record whose gcp keys are missing or
    break their strict shapes, or a `--context` that does not name the
    instance's own docker context.
    """


class GAPTunnelError(GCPTransportError):
    """The tunnel child never opened, or died while the forward was serving."""


# ---------------------------------------------------------------------------
# Production seams: the real launcher, and the stderr tail its child feeds.
# ---------------------------------------------------------------------------


# Spawns the long-lived tunnel child. Injected rather than called
# internally, so the unit suite substitutes a fake instead of patching this
# module; `default_process_launcher` is the production implementation.
ProcessLauncher = Callable[[Sequence[str]], "subprocess.Popen[bytes]"]


def default_process_launcher(command: Sequence[str]) -> subprocess.Popen[bytes]:
    """The production `ProcessLauncher`: the gcloud child with both pipes held.

    `stdout` is piped and echoed by the supervisor, and `stderr` is piped
    because it MUST be drained continuously -- gcloud writes its progress
    and its errors there, and a child blocked on a full stderr pipe would
    wedge the tunnel with nothing listening yet -- so the launcher hands
    both back and `_StderrTail` below owns the drain.
    """
    return subprocess.Popen(list(command), stdout=subprocess.PIPE, stderr=subprocess.PIPE)


class _StderrTail:
    """The child's stderr, drained by one thread, kept as a bounded tail.

    The reader thread runs for the child's whole life: every line is
    scrubbed (`_REDACT_PATTERN`), echoed to this process's stderr -- which
    is the daemon log under `make connect-start`, the same surface the
    other transports' children write their diagnostics to -- and appended
    to a bounded deque. `snapshot` joins the reader briefly and returns
    the tail, so a child-death diagnosis is built from the child's own
    last words rather than from whatever had landed by the time the death
    was noticed.
    """

    def __init__(self, stream: object) -> None:
        self._lines: collections.deque[str] = collections.deque(maxlen=_STDERR_TAIL_LINES)
        self._thread = threading.Thread(target=self._drain, args=(stream,), daemon=True)
        self._thread.start()

    def _drain(self, stream: object) -> None:
        readline = getattr(stream, "readline", None)
        if readline is None:  # pragma: no cover - launcher contract violation
            return
        for raw_line in iter(readline, b""):
            line = _REDACT_PATTERN.sub(r"\1***", raw_line.decode("utf-8", errors="replace"))
            self._lines.append(line)
            sys.stderr.write(line)
            sys.stderr.flush()

    def snapshot(self) -> str:
        """The tail's lines, after giving the reader a bounded moment to flush."""
        self._thread.join(timeout=_STDERR_TAIL_JOIN_SECONDS)
        return "".join(self._lines)


# ---------------------------------------------------------------------------
# The engine address record: the three keys this transport reads, and the
# one deploy remedy for a record that does not carry them.
# ---------------------------------------------------------------------------


def load_gcp_address(name: str) -> dict[str, object]:
    """The instance's engine address record, as `instances.engine_address` reads it.

    A missing or malformed record surfaces as `GCPConfigError` carrying
    `instances`' own remedy verbatim (the record's built-in message names
    the deploy that writes it), so no exception type foreign to this module
    crosses its boundary.

    Raises:
        GCPConfigError: the record is missing, unreadable, or not one
            JSON object, or the instance name is invalid.
    """
    try:
        return instances.engine_address(repo.find_root(Path.cwd()), name)
    except instances.InstancesError as exc:
        raise GCPConfigError(str(exc)) from exc


def gcp_values(address: dict[str, object], *, instance: str) -> tuple[str, str, str]:
    """The `(instance_name, zone, project_id)` triple the record must carry.

    Extra keys belong to later slices and are ignored. A record present but
    missing a key fails fast here, naming the same deploy remedy a missing
    record names: the operator's next step is identical, and a half-written
    record should read as the not-yet-deployed state it effectively is.
    Each value is shape-checked before anything composes it into a gcloud
    argument vector: `instance_name` must round-trip through
    `instances.sanitize_gcp_name` unchanged (the RFC1035 label the VM is
    deployed as), and `zone`/`project_id` must match the strict patterns
    `instances.ZONE_PATTERN`/`instances.PROJECT_ID_PATTERN` -- the same
    shapes the `instances.gcp_zone` accessor enforces, so a scheme, path or
    shell metacharacter smuggled into the record could never aim the dial
    somewhere else.

    Raises:
        GCPConfigError: any key is missing, is not a non-empty string, or
            breaks its shape.
    """
    values: list[str] = []
    for key, value in (
        (instances.GCP_INSTANCE_NAME_KEY, address.get(instances.GCP_INSTANCE_NAME_KEY)),
        (instances.ZONE_KEY, address.get(instances.ZONE_KEY)),
        (instances.PROJECT_ID_KEY, address.get(instances.PROJECT_ID_KEY)),
    ):
        if not isinstance(value, str) or not value:
            raise GCPConfigError(
                f"ERROR: the engine address record for instance {instance!r} carries "
                f"no usable {key!r}\n"
                f"The IAP transport reads '{instances.GCP_INSTANCE_NAME_KEY}', "
                f"'{instances.ZONE_KEY}' and '{instances.PROJECT_ID_KEY}' from "
                f"{instances.engine_address_path(instance)}.\n"
                f"Redeploy to write it: make instance-deploy INSTANCE={instance}"
            )
        values.append(value)
    instance_name, zone, project_id = values
    try:
        round_tripped = instances.sanitize_gcp_name(instance_name)
    except instances.InvalidGCPNameError as exc:
        raise GCPConfigError(
            f"{exc}\n"
            f"The value comes from {instances.engine_address_path(instance)}; the VM "
            "is deployed under the sanitized name, so the record must carry one the "
            "sanitizer accepts.\n"
            f"Redeploy to rewrite it: make instance-deploy INSTANCE={instance}"
        ) from exc
    if round_tripped != instance_name:
        raise GCPConfigError(
            f"ERROR: the engine address record for instance {instance!r} carries a "
            f"malformed {instances.GCP_INSTANCE_NAME_KEY!r}\n"
            f"{instance_name!r} is not already in its sanitized form "
            f"({round_tripped!r}); the VM is deployed under the sanitized value, and "
            f"the record must carry it verbatim.\n"
            f"Redeploy to rewrite it: make instance-deploy INSTANCE={instance}"
        )
    if instances.ZONE_PATTERN.fullmatch(zone) is None:
        raise GCPConfigError(
            f"ERROR: the engine address record for instance {instance!r} carries a "
            f"malformed {instances.ZONE_KEY!r}\n"
            f"{zone!r} is not a gcp zone of the form <region>-<zone-letter> "
            "(us-east1-b); the value comes from "
            f"{instances.engine_address_path(instance)}.\n"
            f"Redeploy to rewrite it: make instance-deploy INSTANCE={instance}"
        )
    if instances.PROJECT_ID_PATTERN.fullmatch(project_id) is None:
        raise GCPConfigError(
            f"ERROR: the engine address record for instance {instance!r} carries a "
            f"malformed {instances.PROJECT_ID_KEY!r}\n"
            f"{project_id!r} is not a gcp project id of 6-30 lowercase letters, "
            f"digits and hyphens; the value comes from "
            f"{instances.engine_address_path(instance)}.\n"
            f"Redeploy to rewrite it: make instance-deploy INSTANCE={instance}"
        )
    return instance_name, zone, project_id


def build_tunnel_argv(
    *, instance_name: str, zone: str, project_id: str, local_port: int
) -> list[str]:
    """The `gcloud compute start-iap-tunnel` argument vector, in full.

    The one place the tunnel's invocation is composed. Every element is a
    separate list entry, never a shell string; `--local-host-port` carries
    the loopback address so gcloud's listener binds it and nothing wider.
    No credential flag is ever built: gcloud authenticates ambiently, and
    the operator's `gcloud auth login` session is the identity.
    """
    return [
        GCLOUD_BINARY,
        "compute",
        "start-iap-tunnel",
        instance_name,
        _TUNNEL_TARGET_PORT,
        f"--local-host-port={_LOCALHOST}:{local_port}",
        "--zone",
        zone,
        "--project",
        project_id,
    ]


# ---------------------------------------------------------------------------
# The boot-applied install: the compare and wait logic the cert-install
# flow and the start path drive. GCP has no run-command channel, so the
# material is applied by the VM's own startup script at the next boot, and
# everything this module offers the laptop side is a read: the store's
# checksum, the guest attributes recording what the VM applied, and the
# bounded waits over both.
# ---------------------------------------------------------------------------


def _pace(seconds: float) -> None:
    """One bounded wait between probes.

    The same bounded-wait primitive `transport_ssh._pace` rests on: an
    empty selector blocks for at most `seconds` (a real I/O wait, never a
    sleep) and returns once the slice has passed. Defined here, ahead of
    both the readiness poll below and the boot-applied waits above it in
    call order, so the module has one pacer and no ordering surprise.
    """
    selector = selectors.DefaultSelector()
    try:
        selector.select(timeout=seconds)
    finally:
        selector.close()


# The Runner the boot-applied helpers issue gcloud through: given the full
# argv and an optional stdin document, return a completed process. The same
# shape `instance_ops.Runner` takes; declared as its own alias here rather
# than imported because `instance_ops` imports this module for the wait
# helpers below, and one direction of that import edge must stay empty.
GcloudRunner = Callable[..., "subprocess.CompletedProcess[str]"]


def gcp_material_record_values(
    record: dict[str, object], *, instance: str
) -> tuple[str, str, str, str]:
    """The `(instance_name, zone, project_id, secrets_prefix)` quadruple the record carries.

    `gcp_values` validates the triple the tunnel dials by; this extends the
    same record with `SECRETS_PREFIX_KEY`, the prefix the VM's startup
    script fetches its secrets by, validated by the identical round-trip
    through `instances.sanitize_gcp_name` -- a record prefix the sanitizer
    does not accept back unchanged would compose secret ids no publish ever
    wrote, and reads as the not-yet-deployed state it effectively is.

    Raises:
        GCPConfigError: any key is missing, is not a non-empty string, or
            breaks its shape (the message names the deploy that rewrites
            the record).
    """
    instance_name, zone, project_id = gcp_values(record, instance=instance)
    prefix = record.get(instances.SECRETS_PREFIX_KEY)
    if not isinstance(prefix, str) or not prefix:
        raise GCPConfigError(
            f"ERROR: the engine address record for instance {instance!r} carries "
            f"no usable {instances.SECRETS_PREFIX_KEY!r}\n"
            f"The boot-applied install fetches the TLS secrets under the prefix "
            f"the record records, reading '{instances.SECRETS_PREFIX_KEY}' from "
            f"{instances.engine_address_path(instance)}.\n"
            f"Redeploy to write it: make instance-deploy INSTANCE={instance}"
        )
    try:
        round_tripped = instances.sanitize_gcp_name(prefix)
    except instances.InvalidGCPNameError as exc:
        raise GCPConfigError(
            f"{exc}\n"
            f"The value comes from {instances.engine_address_path(instance)}; the "
            "startup script fetches under it verbatim, so the record must carry one "
            "the sanitizer accepts.\n"
            f"Redeploy to rewrite it: make instance-deploy INSTANCE={instance}"
        ) from exc
    if round_tripped != prefix:
        raise GCPConfigError(
            f"ERROR: the engine address record for instance {instance!r} carries a "
            f"malformed {instances.SECRETS_PREFIX_KEY!r}\n"
            f"{prefix!r} is not already in its sanitized form "
            f"({round_tripped!r}); the startup script fetches under the value "
            "verbatim.\n"
            f"Redeploy to rewrite it: make instance-deploy INSTANCE={instance}"
        )
    return instance_name, zone, project_id, prefix


def build_guest_attribute_argv(
    *, instance_name: str, zone: str, project_id: str, key: str
) -> list[str]:
    """The `gcloud compute instances get-guest-attributes` argv for one key.

    The format flag is not optional: without it gcloud prints a table, and
    every reader of this output compares plain values. `--query-path`
    names the key under `GUEST_ATTRIBUTE_NAMESPACE`, the one namespace the
    startup script and this module share.
    """
    return [
        GCLOUD_BINARY,
        "compute",
        "instances",
        "get-guest-attributes",
        instance_name,
        f"--query-path={GUEST_ATTRIBUTE_NAMESPACE}/{key}",
        "--zone",
        zone,
        "--project",
        project_id,
        "--format",
        "value(value)",
    ]


def build_instance_status_argv(*, instance_name: str, zone: str, project_id: str) -> list[str]:
    """The `gcloud compute instances describe` argv answering the status alone."""
    return [
        GCLOUD_BINARY,
        "compute",
        "instances",
        "describe",
        instance_name,
        "--zone",
        zone,
        "--project",
        project_id,
        "--format",
        "value(status)",
    ]


# The substrings that mark a failed guest-attribute or secret read as "the
# thing read does not exist", and the ones that mark a failing session as
# one no amount of waiting can fix (the same auth set the tunnel-exit
# diagnosis classifies). Absent is an answer the waits keep polling past;
# auth is an answer they stop on.
_ATTRIBUTE_ABSENT_MARKERS: tuple[str, ...] = ("not found", "NOT_FOUND", "404")


def _run_gcloud(
    runner: GcloudRunner, argv: Sequence[str], *, what: str
) -> subprocess.CompletedProcess[str]:
    """Run one gcloud argv, wrapping a spawn failure in this module's error."""
    try:
        return runner(list(argv), None)
    except OSError as exc:
        raise GCPTransportError(
            f"ERROR: could not run {GCLOUD_BINARY} for {what}\n"
            f"{exc}\n"
            "Install the Google Cloud CLI (brew install google-cloud-sdk), "
            "authenticate with 'gcloud auth login', then retry."
        ) from exc


def read_guest_attribute(
    record: dict[str, object], runner: GcloudRunner, *, instance: str, key: str
) -> str | None:
    """One guest attribute's value, or None when it is confirmed absent.

    Absent is exactly one of two answers: the read failed with a
    not-found-shaped diagnostic (the attribute was never written), or the
    VM answered successfully with nothing (an attribute read before the
    first boot has finished writing it). Every other failure -- an expired
    session, a wrong zone, a missing VM -- raises with gcloud's remedy,
    never misread as "not applied yet".

    Raises:
        GCPConfigError: the record is missing or malformed, or the
            instance is not found where the record says it is.
        GCPTransportError: the read failed for any other reason.
    """
    instance_name, zone, project_id, _ = gcp_material_record_values(record, instance=instance)
    argv = build_guest_attribute_argv(
        instance_name=instance_name, zone=zone, project_id=project_id, key=key
    )
    result = _run_gcloud(
        runner, argv, what=f"the guest attribute {GUEST_ATTRIBUTE_NAMESPACE}/{key}"
    )
    if result.returncode == 0:
        return result.stdout.strip() or None
    if any(marker in result.stderr for marker in _ATTRIBUTE_ABSENT_MARKERS):
        return None
    raise GCPTransportError(
        f"ERROR: reading guest attribute {GUEST_ATTRIBUTE_NAMESPACE}/{key} of "
        f"{instance_name!r} failed\n"
        f"{' '.join(argv)} exited {result.returncode}\n"
        f"{result.stderr.strip()}\n"
        "Resolve the failure above, then retry."
    )


def read_published_checksum(
    record: dict[str, object], runner: GcloudRunner, *, instance: str
) -> str:
    """The checksum secret's value, exactly as `certs.publish_to_secret_manager` wrote it.

    `versions access latest` prints the stored bytes raw (verified live);
    both sides of the applied comparison are hex digests, so whitespace
    carries no information and is trimmed before the compare -- the TLS
    bytes themselves are never compared here.

    Raises:
        GCPConfigError: the record is missing or malformed, or the
            checksum secret does not exist yet (the remedy names the
            `cert-publish` that writes it).
        GCPTransportError: the read failed for any other reason.
    """
    _instance_name, _zone, project_id, prefix = gcp_material_record_values(
        record, instance=instance
    )
    secret_id = certs.gcp_secret_id(prefix, certs.GCP_CHECKSUM_SECRET)
    argv = [
        GCLOUD_BINARY,
        "secrets",
        "versions",
        "access",
        "latest",
        "--secret",
        secret_id,
        "--project",
        project_id,
    ]
    result = _run_gcloud(runner, argv, what=f"the checksum secret {secret_id}")
    if result.returncode != 0:
        if any(marker in result.stderr for marker in _ATTRIBUTE_ABSENT_MARKERS):
            raise GCPConfigError(
                f"ERROR: no checksum secret {secret_id!r} in project '{project_id}'\n"
                "The boot-applied install compares the VM's applied material against "
                "the checksum the publish writes beside it, and this publish has not "
                "run yet.\n"
                f"Publish first: make cert-publish INSTANCE={instance}"
            )
        raise GCPTransportError(
            f"ERROR: reading the checksum secret {secret_id!r} failed\n"
            f"{' '.join(argv)} exited {result.returncode}\n"
            f"{result.stderr.strip()}\n"
            "If gcloud reported an authentication failure, run 'gcloud auth login', "
            "then retry."
        )
    return result.stdout.strip()


def applied_checksum_matches(
    record: dict[str, object], runner: GcloudRunner, *, instance: str
) -> bool:
    """Whether the VM's applied TLS material is exactly the published material.

    The one comparison the gcp `cert-install` flow is decided by: True only
    when the `general-dev/tls-checksum` guest attribute exists and equals
    the store's checksum secret -- meaning the startup script fetched,
    verified and applied exactly what is published and restarted the
    daemon under it. False when the attribute is absent or drifted (the
    reboot case), never when a read failed for a real reason: those raise,
    so a caller cannot mistake an unreachable VM for an out-of-date one.

    Raises:
        GCPConfigError: the record is missing or malformed, or the
            checksum secret does not exist yet.
        GCPTransportError: a read failed for any other reason.
    """
    published = read_published_checksum(record, runner, instance=instance)
    applied = read_guest_attribute(
        record, runner, instance=instance, key=GUEST_ATTRIBUTE_CHECKSUM_KEY
    )
    if applied is None:
        return False
    return applied == published


def material_apply_timeout_seconds() -> float:
    """`GCP_MATERIAL_APPLY_TIMEOUT`, read fresh, exactly once per apply wait."""
    return _positive_seconds_from_env(
        MATERIAL_APPLY_TIMEOUT_ENV_VAR, _MATERIAL_APPLY_TIMEOUT_DEFAULT_SECONDS
    )


def provisioned_timeout_seconds() -> float:
    """`GCP_PROVISIONED_TIMEOUT`, read fresh, exactly once per provisioned wait."""
    return _positive_seconds_from_env(
        PROVISIONED_TIMEOUT_ENV_VAR, _PROVISIONED_TIMEOUT_DEFAULT_SECONDS
    )


def _auth_failure_cannot_wait(diagnostic: str) -> bool:
    """Whether `diagnostic` names a cause no amount of polling can fix."""
    return any(marker in diagnostic.lower() for marker in _AUTH_FAILURE_MARKERS)


def _last_error_attribute(record: dict[str, object], runner: GcloudRunner, *, instance: str) -> str:
    """The VM's `general-dev/error` diagnostic, or a placeholder when unreadable.

    Best-effort by design: the wait's failure message is more useful with
    the VM's own failure note in it, but this read must never displace the
    timeout it decorates with an error of its own. The attribute is
    VM-writable through the guest-attribute API, so its text is never
    interpolated raw: every C0/C1 control byte (escape sequences included)
    is stripped here, at this single read site, before the value reaches
    any error message -- the callers render the result quoted, so a
    poisoned attribute can decorate a diagnosis but never drive a
    terminal.
    """
    try:
        value = (
            read_guest_attribute(record, runner, instance=instance, key=_GUEST_ATTRIBUTE_ERROR_KEY)
            or "(the VM recorded no error attribute)"
        )
    except GCPTransportError:
        return "(the VM's error attribute could not be read)"
    return _CONTROL_BYTES_PATTERN.sub("", value)


def wait_applied_checksum(
    record: dict[str, object],
    runner: GcloudRunner,
    *,
    instance: str,
    pace: Callable[[float], None] = _pace,
    clock: Callable[[], float] = time.monotonic,
    deadline_seconds: float | None = None,
) -> str:
    """Poll until the VM's applied checksum equals the store's; return the checksum.

    The bounded half of the boot-applied install: after the reboot the
    `cert-install` flow issues, the startup script fetches the secrets,
    applies them on drift, restarts the daemon and rewrites the
    `general-dev/tls-checksum` attribute -- and this poll ends only when
    the attribute says the applied material is the published one. While the
    VM is not RUNNING, or its reads answer not-yet/absent, the poll keeps
    pacing; an expired gcloud session is the one cause that stops it
    immediately, because no budget fixes it. The deadline is
    `GCP_MATERIAL_APPLY_TIMEOUT` (default 900) unless passed explicitly,
    the way tests walk it without waiting.

    Returns:
        The published checksum the VM is now confirmed to have applied.

    Raises:
        GCPConfigError: the record is missing or malformed, or the
            checksum secret does not exist yet.
        GCPTransportError: a read failed with an unfixable cause, or the
            deadline ran out (the message carries the last observed
            attribute and the VM's own `general-dev/error` diagnostic).
    """
    if deadline_seconds is None:
        deadline_seconds = material_apply_timeout_seconds()
    deadline = clock() + deadline_seconds
    observed = "no attribute read answered"
    while True:
        try:
            if applied_checksum_matches(record, runner, instance=instance):
                return read_published_checksum(record, runner, instance=instance)
            applied = read_guest_attribute(
                record, runner, instance=instance, key=GUEST_ATTRIBUTE_CHECKSUM_KEY
            )
            observed = applied if applied is not None else "(attribute absent)"
        except GCPTransportError as exc:
            if _auth_failure_cannot_wait(str(exc)):
                raise
            observed = str(exc)
        if clock() >= deadline:
            raise GCPTransportError(
                f"ERROR: instance {instance!r} did not apply the published TLS "
                f"material within {deadline_seconds:g}s\n"
                f"The last observed {GUEST_ATTRIBUTE_NAMESPACE}/"
                f"{GUEST_ATTRIBUTE_CHECKSUM_KEY} was {observed!r}; the VM's own "
                f"diagnostic: {_last_error_attribute(record, runner, instance=instance)!r}\n"
                f"Raise {MATERIAL_APPLY_TIMEOUT_ENV_VAR} if this instance is simply "
                "slow, and read the startup script's own log on the VM "
                "(serial console output) for its fetch and apply steps."
            )
        pace(_GUEST_ATTRIBUTE_POLL_SECONDS)


def wait_provisioned(
    record: dict[str, object],
    runner: GcloudRunner,
    *,
    instance: str,
    pace: Callable[[float], None] = _pace,
    clock: Callable[[], float] = time.monotonic,
    deadline_seconds: float | None = None,
) -> None:
    """Poll until the VM's `general-dev/provisioned` attribute reports TRUE.

    The marker gate every wait that assumes provisioning runs behind (the
    azure live lesson, applied to gcp): `instances describe` reporting
    RUNNING means the VM was scheduled, nothing more, and the startup
    script's own success marker is the only evidence the provisioning the
    listener needs actually ran. While the attribute is absent the poll
    keeps pacing -- a first boot has not finished yet -- and an expired
    gcloud session stops it immediately. The deadline is
    `GCP_PROVISIONED_TIMEOUT` (default 300, the azure agent-ready wait's
    default) unless passed explicitly.

    Raises:
        GCPConfigError: the record is missing or malformed.
        GCPTransportError: a read failed with an unfixable cause, or the
            deadline ran out (the message carries the VM's own
            `general-dev/error` diagnostic).
    """
    if deadline_seconds is None:
        deadline_seconds = provisioned_timeout_seconds()
    deadline = clock() + deadline_seconds
    while True:
        try:
            observed = read_guest_attribute(
                record, runner, instance=instance, key=GUEST_ATTRIBUTE_PROVISIONED_KEY
            )
            if observed == GUEST_ATTRIBUTE_PROVISIONED_VALUE:
                return
        except GCPTransportError as exc:
            if _auth_failure_cannot_wait(str(exc)):
                raise
        if clock() >= deadline:
            raise GCPTransportError(
                f"ERROR: instance {instance!r} never reported "
                f"{GUEST_ATTRIBUTE_NAMESPACE}/{GUEST_ATTRIBUTE_PROVISIONED_KEY}="
                f"{GUEST_ATTRIBUTE_PROVISIONED_VALUE} within {deadline_seconds:g}s\n"
                f"The VM's own diagnostic: "
                f"{_last_error_attribute(record, runner, instance=instance)!r}\n"
                f"Raise {PROVISIONED_TIMEOUT_ENV_VAR} if this instance is simply "
                "slow, and read the startup script's own log on the VM "
                "(serial console output) for its provisioning steps."
            )
        pace(_GUEST_ATTRIBUTE_POLL_SECONDS)


# ---------------------------------------------------------------------------
# Timeouts: read fresh through the shared hostprobe reader, never hard-coded.
# ---------------------------------------------------------------------------


def _positive_seconds_from_env(env_var: str, default_seconds: float) -> float:
    """Read `env_var` via `hostprobe.read_positive_seconds`, wrapping failure.

    The same shape `transport_ssh._positive_seconds_from_env` establishes:
    the shared reader owns the parse and the validation, and this module
    wraps the rejection as its own error so `main`'s single `except` clause
    holds.
    """
    try:
        return read_positive_seconds(env_var, default_seconds)
    except HostProbeError as exc:
        raise GCPTransportError(
            f"ERROR: {exc}\n"
            "Set it to a positive number of seconds, or unset it to use the default of "
            f"{default_seconds:g}."
        ) from exc


def tunnel_connect_timeout_seconds() -> float:
    """`GCP_TUNNEL_CONNECT_TIMEOUT`, read fresh, exactly once per readiness wait."""
    return _positive_seconds_from_env(
        _TUNNEL_CONNECT_TIMEOUT_ENV_VAR, _TUNNEL_CONNECT_TIMEOUT_DEFAULT_SECONDS
    )


def _handshake_timeout_seconds() -> float:
    """`DOCKER_HANDSHAKE_TIMEOUT`, read through the shared hostprobe reader.

    The variable's name, default and validation live in `hostprobe`; the
    verify path's version call is bounded by the same budget the other
    transports' handshakes honor, rather than a second, independently
    drifting copy of the name and default.
    """
    return _positive_seconds_from_env(
        DOCKER_HANDSHAKE_TIMEOUT_ENV_VAR, DOCKER_HANDSHAKE_TIMEOUT_DEFAULT_SECONDS
    )


# ---------------------------------------------------------------------------
# Readiness: the local port accepting a connection, paced by a real I/O
# wait; child death surfaced with its own stderr tail.
# ---------------------------------------------------------------------------


def diagnose_tunnel_exit(returncode: int | None, stderr_tail: str, *, instance: str) -> str:
    """The child-exit diagnosis: the tail, classified into cause and remedy.

    The module's one statement of the three causes a tunnel exit can have,
    worded identically wherever a child can die: the readiness poll and the
    supervise loop both raise through this one construction site, so the
    operator never sees two wordings for one cause. An expired gcloud
    session names `gcloud auth login` and the forward refresh that re-runs
    the recorded command; a refused or unreachable VM (the deallocated-
    instance wording an IAP dial answers with) names `make instance-start`
    first; anything else carries the tail and the refresh remedy as it
    stands.
    """
    tail = stderr_tail.strip() or "(the tunnel child wrote nothing to stderr)"
    lowered = tail.lower()
    if any(marker in lowered for marker in _AUTH_FAILURE_MARKERS):
        return (
            f"ERROR: the IAP tunnel for instance {instance!r} exited "
            f"(exit {returncode if returncode is not None else 'unknown'})\n"
            f"{tail}\n"
            "gcloud's credentials were rejected. Run 'gcloud auth login' to refresh "
            "the credentials, then: make connect-refresh INSTANCE=" + instance
        )
    if any(marker in lowered for marker in _TERMINATED_VM_MARKERS):
        return (
            f"ERROR: the IAP tunnel for instance {instance!r} exited "
            f"(exit {returncode if returncode is not None else 'unknown'})\n"
            f"{tail}\n"
            f"IAP could not reach the VM. If it is deallocated, start it first: "
            f"make instance-start INSTANCE={instance}\n"
            f"Then retry, or refresh the forward: make connect-refresh INSTANCE=" + instance
        )
    return (
        f"ERROR: the IAP tunnel for instance {instance!r} exited "
        f"(exit {returncode if returncode is not None else 'unknown'})\n"
        f"{tail}\n"
        f"Confirm the VM is running and the record's zone and project are right, then "
        f"refresh the forward: make connect-refresh INSTANCE=" + instance
    )


_BACKEND_REFUSAL_MARKER = "failed to connect to backend"


def _tunnel_exit_is_backend_refusal(tail_text: str) -> bool:
    """Whether the child died because the VM's daemon port refused it.

    After a power cycle the VM answers IAP before rootless dockerd is
    listening; gcloud's connectivity test then fails with `failed to
    connect to backend` and the child exits. That failure is retryable
    (the daemon comes up seconds later), unlike an auth refusal or a
    wrong-zone error, which the same classifier leaves alone.
    """
    return _BACKEND_REFUSAL_MARKER in tail_text


def wait_tunnel_ready(
    port: int,
    process: subprocess.Popen[bytes],
    tail: _StderrTail,
    *,
    instance: str,
    clock: Callable[[], float] = time.monotonic,
    pace: Callable[[float], None] = _pace,
) -> None:
    """Poll the local port until it accepts a connection, or fail loudly.

    Readiness is the port accepting a real TCP connection
    (`transport.tcp_ready_probe`), never the process being alive and never
    a parsed gcloud log line. Between probes the loop paces on a bounded
    selector wait (`_pace`), never a bare sleep; each pass checks the child
    first, so a tunnel that dies before its listener opens is reported
    with its own stderr tail instead of burning the whole deadline. The
    total wait is bounded by `GCP_TUNNEL_CONNECT_TIMEOUT`.

    Raises:
        GCPTransportError: `GCP_TUNNEL_CONNECT_TIMEOUT` is set to a value
            `hostprobe.read_positive_seconds` refuses.
        GAPTunnelError: the child exited before the port accepted a
            connection (the message carries the tail, classified), or the
            deadline elapsed with nothing listening.
    """
    timeout = tunnel_connect_timeout_seconds()
    deadline = clock() + timeout
    probe = transport.tcp_ready_probe(port)
    while True:
        if process.poll() is not None:
            raise GAPTunnelError(
                diagnose_tunnel_exit(process.returncode, tail.snapshot(), instance=instance)
            )
        remaining = deadline - clock()
        if remaining <= 0:
            transport.stop_forward(process)
            raise GAPTunnelError(
                f"ERROR: the IAP tunnel's local port {_LOCALHOST}:{port} never "
                f"accepted a connection within "
                f"{_TUNNEL_CONNECT_TIMEOUT_ENV_VAR}={timeout:g}s\n"
                f"{tail.snapshot().strip() or '(the tunnel child wrote nothing to stderr)'}\n"
                f"Raise {_TUNNEL_CONNECT_TIMEOUT_ENV_VAR} if this machine or network is "
                f"simply slow; the tunnel child was stopped."
            )
        if probe(remaining):
            return
        pace(min(remaining, _TUNNEL_POLL_SECONDS))


def wait_port_closed(
    port: int,
    *,
    instance: str,
    clock: Callable[[], float] = time.monotonic,
    pace: Callable[[float], None] = _pace,
) -> None:
    """Poll until nothing listens on `port`; raise if it stays live.

    The interrupt path's verification: a daemon that reports itself stopped
    while its tunnel child still holds the loopback port would leave the
    next open allocating a different port and the old context pointing at
    a dead listener. Bounded by the same `GCP_TUNNEL_CONNECT_TIMEOUT`
    budget the readiness wait keeps; on exhaustion the failure names the
    holder-hunting command, the same remedy `devcontainer_config.forwards`
    stop path names.
    """
    timeout = tunnel_connect_timeout_seconds()
    deadline = clock() + timeout
    probe = transport.tcp_ready_probe(port)
    while True:
        remaining = deadline - clock()
        if remaining <= 0:
            raise GCPTransportError(
                f"ERROR: the IAP tunnel's port {port} is still listening after the "
                f"child was stopped; the tunnel is not closed\n"
                f"Instance {instance!r}'s daemon asked the child to stop and it did "
                "not release the port.\n"
                f"Find the holding process: lsof -i :{port}"
            )
        if not probe(remaining):
            return
        pace(min(remaining, _TUNNEL_POLL_SECONDS))


# ---------------------------------------------------------------------------
# The shared flow: the record, the checks, the port, the context.
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
        GCPConfigError: either check fails.
    """
    try:
        instances.validate_name(name)
    except instances.InvalidInstanceNameError as exc:
        raise GCPConfigError(str(exc)) from exc
    root = repo.find_root(Path.cwd())
    expected_context = instances.docker_context(root, name)
    if context_name != expected_context:
        raise GCPConfigError(
            f"ERROR: docker context {context_name!r} does not name instance {name!r}\n"
            f"The IAP transport manages {expected_context!r}, the context "
            f"instances.docker_context derives for {name!r}.\n"
            f"Pass that context, or connect with: make connect-start INSTANCE={name}"
        )
    return root


def _use_context(runner: CommandRunner, context_name: str) -> None:
    """Activate `context_name` as the docker CLI's active context.

    The daemon a subsequent `make build` targets is whichever context is
    active, so the context this module just created or updated is activated
    here rather than left to a caller who might not know it is required --
    the same call the other transports make. Raises naming the context and
    the underlying docker stderr, never a bare exit code.
    """
    result = runner(("docker", "context", "use", context_name), None)
    if result.binary_missing:
        raise GCPTransportError(
            "ERROR: docker is not on PATH\nInstall Docker Desktop or the Docker CLI, then retry."
        )
    if result.exit_code != 0:
        raise GCPTransportError(
            f"ERROR: docker context use {context_name!r} failed\n"
            f"{result.stderr.strip()}\n"
            "The context was created or updated successfully; only activating it failed. "
            f"Run 'docker context use {context_name}' by hand, then retry."
        )


def _common_setup(name: str, context_name: str) -> tuple[str, str, str, int, CommandRunner]:
    """Steps 1-4 of the connect flow: the record, the checks, the port, the context.

    Shared by `connect` and `verify` (DRY): load the engine address record,
    validate its gcp triple, check the context pair, allocate the local
    port through the record-is-the-context rule, then satisfy the
    client-certificate freshness precondition and refuse a legacy `ssh://`
    context while creating or updating the mTLS context at that port --
    both inside `transport.ensure_context`, the single implementation of
    that flow this module reuses rather than a second copy of the
    certificate check, the legacy refusal, or the context argv builders.

    Returns:
        `(instance_name, zone, project_id, local_port, runner)`.
    """
    address = load_gcp_address(name)
    instance_name, zone, project_id = gcp_values(address, instance=name)
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
    return instance_name, zone, project_id, local_port, runner


def _spawn_tunnel(
    launcher: ProcessLauncher, argv: Sequence[str]
) -> tuple[subprocess.Popen[bytes], _StderrTail]:
    """Spawn the tunnel child and start its stderr drain.

    The launcher is injected so the unit suite substitutes a fake; the
    production launcher pipes both of the child's streams, and the tail
    owns the drain from the moment the child exists -- a line gcloud
    writes this early would otherwise sit in an undrained pipe.

    Raises:
        GCPTransportError: the child could not be spawned at all -- gcloud
            missing from PATH is this case, and the remedy names the
            install and the ambient login.
    """
    try:
        process = launcher(argv)
    except OSError as exc:
        raise GCPTransportError(
            f"ERROR: could not run {GCLOUD_BINARY}\n"
            f"{exc}\n"
            "Install the Google Cloud CLI (brew install google-cloud-sdk), "
            "authenticate with 'gcloud auth login', then retry."
        ) from exc
    if process.stderr is None:
        raise GCPTransportError(
            "ERROR: the tunnel process was launched without a readable stderr pipe\n"
            "This is a bug in the process launcher, which must always pipe stderr so "
            "the child can never wedge on a full pipe."
        )
    return process, _StderrTail(process.stderr)


class _StopServing(BaseException):
    """Raised by the SIGTERM handler into the supervise loop; the clean-stop signal."""


def _run_until_interrupted(
    process: subprocess.Popen[bytes],
    tail: _StderrTail,
    *,
    instance: str,
    port: int,
) -> None:
    """Hold the tunnel open until it exits or the operator interrupts.

    Mirrors the other transports' hold: the child's stdout is drained to
    this process's stdout (gcloud's progress lines reach the daemon log
    under `make connect-start` that way, beside the stderr the tail
    already echoes), and the child is stopped on the way out regardless of
    which of the three ends arrived. A child that exits on its own is a
    dropped tunnel, whatever its exit code -- gcloud exits only when the
    tunnel cannot be held -- so every self-exit raises through
    `diagnose_tunnel_exit` with the child's own tail. SIGINT arrives as
    the interpreter's own `KeyboardInterrupt`; SIGTERM is trapped into
    `_StopServing`, so both interrupt paths land in the same clean stop --
    the child stopped, the port verified closed, exit 0 -- which is what
    `devcontainer_config.forwards`' SIGINT-first process-group stop relies
    on. The previous SIGTERM handler is restored on the way out.

    Raises:
        GAPTunnelError: the child exited on its own (the message carries
            the tail, classified into cause and remedy).
    """

    def _raise_stop(signum: int, frame: object) -> NoReturn:
        del signum, frame  # the signal's identity does not change the stop
        raise _StopServing

    previous = signal.signal(signal.SIGTERM, _raise_stop)
    interrupted = False
    try:
        if process.stdout is not None:
            # The same scrub the stderr tail applies: no credential is known
            # to ride either of gcloud's streams, and the redact keeps an
            # unexpected bearer-shaped line from reaching the daemon log
            # through the stdout echo either.
            for raw_line in process.stdout:
                line = _REDACT_PATTERN.sub(r"\1***", raw_line.decode("utf-8", errors="replace"))
                sys.stdout.buffer.write(line.encode("utf-8", errors="replace"))
                sys.stdout.buffer.flush()
        process.wait()
    except (_StopServing, KeyboardInterrupt):
        interrupted = True
    finally:
        signal.signal(signal.SIGTERM, previous)
        if interrupted or process.poll() is None:
            transport.stop_forward(process)
            wait_port_closed(port, instance=instance)
    if interrupted:
        return
    raise GAPTunnelError(
        diagnose_tunnel_exit(process.returncode, tail.snapshot(), instance=instance)
    )


def _run_connect(args: argparse.Namespace) -> int:
    """Handler for `connect`: the IAP tunnel daemon's entry point.

    Runs the shared setup, activates the context, spawns the tunnel child,
    waits for its port to answer (bounded, child-death-aware), prints the
    `Connected:` marker `devcontainer_config.forwards` polls for -- only
    after the port accepts a connection and the context is usable, never
    before -- and supervises until SIGINT/SIGTERM or child death.

    The SIGTERM trap and the stop backstop are installed here, around the
    WHOLE spawn-retry-supervise flow, not only inside the supervise loop:
    a SIGTERM (or Ctrl-C) that lands during the boot-race retries would
    otherwise kill this process with the freshly spawned child still
    holding the port. The handler raises `_StopServing`, the same signal
    `_run_until_interrupted` traps; the `finally` restores the previous
    handler and stops the child whenever one is still alive, then verifies
    the port actually closed -- exactly the clean stop the supervise loop's
    own exit path performs, so an interrupt at any point in the flow leaves
    no tunnel child and no held port behind, and exits 0. On every path
    that already stopped the child, its own exit means `poll()` answers
    and the backstop is a no-op, so the child is never terminated twice
    and never left behind.
    """
    instance_name, zone, project_id, local_port, runner = _common_setup(args.name, args.context)
    _use_context(runner, args.context)

    def _raise_stop(signum: int, frame: object) -> NoReturn:
        del signum, frame  # the signal's identity does not change the stop
        raise _StopServing

    previous = signal.signal(signal.SIGTERM, _raise_stop)
    process: subprocess.Popen[bytes] | None = None
    try:
        # A VM freshly started answers IAP before its daemon listens; the
        # child then dies with a backend refusal seconds in. Bounded retries
        # absorb that boot race; every other child death (auth, wrong zone,
        # anything unclassified) surfaces immediately with its own remedy.
        attempts = 3
        while True:
            process, tail = _spawn_tunnel(
                default_process_launcher,
                build_tunnel_argv(
                    instance_name=instance_name,
                    zone=zone,
                    project_id=project_id,
                    local_port=local_port,
                ),
            )
            try:
                wait_tunnel_ready(local_port, process, tail, instance=args.name)
                break
            except GAPTunnelError as exc:
                attempts -= 1
                still_refusing = process.poll() is not None and _tunnel_exit_is_backend_refusal(
                    tail.snapshot()
                )
                if process.poll() is None or attempts <= 0 or not still_refusing:
                    raise
                transport.stop_forward(process)
                print(
                    f"retrying the tunnel: the VM's daemon was not listening yet "
                    f"({attempts} attempt(s) left)",
                    flush=True,
                )
                _pace(5.0)
        print(
            f"Connected: docker context {args.context!r} -> {args.name} over iap (zone {zone}).",
            flush=True,
        )
        _run_until_interrupted(process, tail, instance=args.name, port=local_port)
    except (_StopServing, KeyboardInterrupt):
        # Both interrupt paths land in the same clean stop the supervise
        # loop performs: the finally below stops the still-live child and
        # verifies the port closed, and the daemon exits 0, which
        # `devcontainer_config.forwards`' lifecycle relies on.
        pass
    finally:
        signal.signal(signal.SIGTERM, previous)
        if process is not None and process.poll() is None:
            transport.stop_forward(process)
            wait_port_closed(local_port, instance=args.name)
    return 0


def _parse_server_payload(stdout: str, context_name: str) -> str:
    """The `docker version --format {{json .Server}}` payload's version field.

    An independent copy of the other transports' own parser by the same
    design that keeps each transport's diagnoses module-local: the refusal
    names this transport's context, not another's.
    """
    try:
        payload = json.loads(stdout)
        return str(payload["Version"])
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise GCPTransportError(
            f"ERROR: docker version for context {context_name!r} produced unparsable "
            f"output: {stdout.strip()!r}\n"
            "Run the command manually and investigate why its output does not match the "
            "expected shape."
        ) from exc


def _run_verify(args: argparse.Namespace) -> int:
    """Handler for `verify`: a one-shot reachability proof through the tunnel.

    Runs the shared setup, spawns the tunnel child, waits for its port,
    and runs one real `docker --context ... version --format
    {{json .Server}}` call through the freshly ensured context -- the call
    that proves the whole path: loopback listener, IAP front end, VM
    daemon, mTLS material. The context is left created (it is the record
    of the port allocation, and the next connect reuses the same port
    through it) but never activated: a proof must not switch the
    machine-wide docker context as a side effect, the same rule the ssh
    transport's verify applies. The child is torn down on every path out,
    and nothing is left listening.
    """
    instance_name, zone, project_id, local_port, runner = _common_setup(args.name, args.context)
    process, tail = _spawn_tunnel(
        default_process_launcher,
        build_tunnel_argv(
            instance_name=instance_name,
            zone=zone,
            project_id=project_id,
            local_port=local_port,
        ),
    )
    try:
        wait_tunnel_ready(local_port, process, tail, instance=args.name)
        version_command = (
            "docker",
            "--context",
            args.context,
            "version",
            "--format",
            "{{json .Server}}",
        )
        result = runner(version_command, _handshake_timeout_seconds())
        if result.binary_missing:
            raise GCPTransportError(
                "ERROR: docker is not on PATH\n"
                "Install Docker Desktop or the Docker CLI, then retry."
            )
        if result.exit_code != 0:
            raise GCPTransportError(
                f"ERROR: docker version handshake for context {args.context!r} failed\n"
                f"{result.stderr.strip()}\n"
                "Confirm the tunnel is still established and the daemon behind it "
                "answers, then retry."
            )
        server_version = _parse_server_payload(result.stdout, args.context)
    finally:
        transport.stop_forward(process)
    print(
        f"Verified: docker context {args.context!r} -> {args.name} over iap "
        f"(zone {zone}) (server {server_version})."
    )
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="devcontainer_config.transport_gcp",
        description=(
            "GCP IAP transport: the gcloud start-iap-tunnel daemon carrying the "
            "docker API to a gcp engine's VM."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    connect_parser = subparsers.add_parser(
        "connect",
        help=(
            "Load the engine address record, create/update the mTLS docker context "
            "behind the IAP tunnel, and supervise the tunnel until interrupted."
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
            "One-shot reachability proof: open the tunnel, run docker version through "
            "it, tear everything down. Leaves the context created."
        ),
    )
    verify_parser.add_argument(
        "--name", required=True, help="The instance name (the remote-instances/<name> directory)."
    )
    verify_parser.add_argument("--context", required=True, help="The docker context name.")
    verify_parser.set_defaults(handler=_run_verify)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Parse `argv`, run the selected command, and return an exit code.

    Every handler raises a `GCPTransportError` (or one of its sibling
    `transport.TransportError` subclasses) on a real failure instead of
    exiting itself; this is the one place that exception becomes a
    non-zero exit code, printed with an `ERROR:` prefix to stderr, never a
    stack trace. `repo.RepoError` is caught here the same way the other
    transports' `main` catch it, since the handlers resolve the repository
    root the same way.
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
