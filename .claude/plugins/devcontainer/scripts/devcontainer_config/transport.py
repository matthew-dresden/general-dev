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

E6-F2-S1-T2 adds this module's other half: the docker context that carries
the TLS material, and the version handshake that proves the whole path
works end to end (spec Section 11). `ensure_context` creates or updates the
per-instance context (spec Section 9's addressing table; `context_name_for`
delegates to `devcontainer_config.instances.docker_context`, the single
place that row is derived, E8-F1-S1-T1 round 2) to address the port this
module already allocated, carrying the `ca`, `cert` and `key` files spec
Section 5.5 fixes under
`~/.docker/certs/<instance>/` (`devcontainer_config.certs.CertPaths`,
E6-F1-S1-T1); it refuses an existing `ssh://` context by name rather than
mutating it, since that is the legacy transport
`.devcontainer/remote-docker/docker-tunnel.sh` still uses through phase 3,
and it refuses a missing or expired client certificate before ever
touching the context, reusing `certs.classify` (E6-F1-S1-T2) for that
expiry decision rather than reimplementing its warning-window arithmetic a
second time. `handshake` then confirms the daemon behind that context
actually answers `docker version`, retried until it answers and bounded by
`DOCKER_HANDSHAKE_TIMEOUT` read through
`hostprobe.read_positive_seconds` -- the single shared reader of that spec
Section 7.3 variable's name, default and validation
(`hostprobe`'s own single-shot "does the local engine answer" check reads
it the same way, AC-FUNC-007), never a second, independently drifting copy
of that env-var name and default declared in this module. Retrying the
`docker version` call itself (rather than a single
call bounded by the whole timeout, as `hostprobe`'s own check does) is
deliberate: a context this module just created or updated may not have a
daemon ready to answer on the very first attempt, and unlike the
in-process socket connect `tcp_ready_probe` replaced above (measured at
40,825 iterations/second with nothing to throttle it), every attempt here
shells out to a real `docker` CLI process -- a real fork/exec and a real
TLS dial attempt -- which paces the loop by its own unavoidable cost
without this module ever adding a fixed delay of its own; the loop's own
deadline check still bounds the total wait, so it can never spin past
`DOCKER_HANDSHAKE_TIMEOUT` regardless. Once the version call answers,
`handshake` reports the server's API version and whether it reports
running rootless, and fails when that version is below the Section 6
floor of 1.44. A handshake failure is translated by
`diagnose_handshake_failure`: a certificate-name mismatch (the server
certificate was issued for the instance's hostname or private address
instead of the SANs spec Section 5.5 fixes) states the SAN requirement,
both required values, and the reissue invocation, rather than surfacing
the bare, opaque `x509` error, which names neither the forward nor the
SANs; every other failure is translated into a distinct connection
diagnosis instead, so the two causes are never conflated (AC-FUNC-005).

E6-F2-S1-T3 closes the last gap: before it, this module's own CLI --
"the functional replacement for docker-tunnel.sh" this docstring already
claims -- exposed only `start` (E6-F2-S1-T1's raw forward), whose handler
never called `ensure_context` or `handshake` (E6-F2-S1-T2), and the actual
build path (the `Makefile`'s `connect` target) never called into this
module at all, so nothing this module built was ever reachable from
anything a developer or CI ran. `connect` is the entry point that closes
the first half of that gap: it establishes the forward exactly as `start`
does (both now share `_establish_forward`, extracted rather than
duplicated, DRY), then creates or updates the docker context at the port
just allocated, activates it (`_use_context`), and confirms it with a
version handshake, so a caller of `connect` alone ends with a working,
mTLS-secured `tcp://` context, already active, ready to build against
(AC-FUNC-003). The `Makefile`'s `connect` target closes the second half: it
reads `DEVCONTAINER_TRANSPORT` itself and dispatches to
`python3 -m devcontainer_config.transport connect` only when it is `ssm`,
leaving `docker-tunnel.sh` untouched and still the default (AC-FUNC-001).
`connect`'s own handler stays opt-in regardless of its caller, gated by
`resolve_transport` reading the identical `DEVCONTAINER_TRANSPORT` variable
(never a file edit): unset or `ssh` -- the default -- refuses before ever
touching AWS or docker (`_require_ssm_transport_selected`), leaving
`docker-tunnel.sh` the only build path that ran, exactly as before this
task (AC-FUNC-002); only the explicit value `ssm` lets `connect` proceed.
Nothing here ever writes the caller's own SSH client configuration file, or
requires a key on the instance, in either case (AC-FUNC-004): this module
still imports nothing from `docker-tunnel.sh` and never touches SSH at all.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import json
import os
import selectors
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO, NoReturn

from devcontainer_config import certs, instances, repo
from devcontainer_config.hostprobe import (
    DOCKER_HANDSHAKE_TIMEOUT_DEFAULT_SECONDS,
    DOCKER_HANDSHAKE_TIMEOUT_ENV_VAR,
    CommandResult,
    CommandRunner,
    HostProbeError,
    docker_context_endpoint_host,
    docker_context_forwarded_port,
    read_positive_seconds,
)

DOCKER_TLS_PORT_ENV_VAR = "DOCKER_TLS_PORT"
_DOCKER_TLS_PORT_DEFAULT = 2376

SSM_FORWARD_TIMEOUT_ENV_VAR = "SSM_FORWARD_TIMEOUT"
_SSM_FORWARD_TIMEOUT_DEFAULT_SECONDS = 30.0

# E6-F2-S1-T3, Approach step 2: the build path's opt-in transport selector.
# `resolve_transport` is the one function in this module that reads this
# variable (the same one-reader convention `DOCKER_TLS_PORT_ENV_VAR` and
# `SSM_FORWARD_TIMEOUT_ENV_VAR` already establish above). Unset resolves to
# `TRANSPORT_SSH` -- the existing `docker-tunnel.sh` build path, untouched
# by this module -- so an existing caller that sets nothing new observes no
# behavior change (AC-FUNC-002); `TRANSPORT_SSM` is the only other accepted
# value, and it is what gates `connect` below.
DEVCONTAINER_TRANSPORT_ENV_VAR = "DEVCONTAINER_TRANSPORT"
TRANSPORT_SSH = "ssh"
TRANSPORT_SSM = "ssm"
_VALID_TRANSPORTS: tuple[str, ...] = (TRANSPORT_SSH, TRANSPORT_SSM)

# spec Section 13, decision D2: the port-forwarding document, never the SSH
# document (`AWS-StartSSHSession`) the replaced transport used.
PORT_FORWARDING_DOCUMENT = "AWS-StartPortForwardingSession"

ONLINE_PING_STATUS = "Online"

_LOCALHOST = "127.0.0.1"

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

# spec Section 7.3: "DOCKER_HANDSHAKE_TIMEOUT | 30 | Seconds to await the API".
# `DOCKER_HANDSHAKE_TIMEOUT_ENV_VAR` and `DOCKER_HANDSHAKE_TIMEOUT_DEFAULT_SECONDS`
# are imported from `hostprobe`, not redeclared here: `_handshake_timeout_seconds`
# below is this module's only call site for the variable (AC-FUNC-007), and it
# reads it through `hostprobe.read_positive_seconds`, the single shared reader
# both modules use, rather than a second, independently drifting copy of the
# name and default.

# spec Section 9's addressing table: the docker context name for one
# instance. There is no module constant for the prefix here (E8-F1-S1-T1
# round 2): `context_name_for`/`instance_from_context_name` below delegate
# to `devcontainer_config.instances.docker_context`/`docker_context_prefix`,
# the single place that row is derived from `repo.repo_slug`, never a
# literal (AC-FUNC-002).

# The scheme `.devcontainer/remote-docker/docker-tunnel.sh`'s legacy SSH
# context still carries; `ensure_context` refuses to mutate a context found
# with this scheme rather than overwriting the phase-3 SSH path in place.
_SSH_ENDPOINT_PREFIX = "ssh://"

# spec Section 6: the minimum docker API version mTLS on a TCP listener with
# a rootless daemon requires. Compared as a tuple of integers
# (`_api_version_tuple`), never as a bare string: "1.9" < "1.44" lexically
# even though 9 < 44 numerically.
DOCKER_API_VERSION_FLOOR = "1.44"

# The `docker info --format {{json .SecurityOptions}}` entry a rootless
# daemon reports (verified against a real docker CLI's own `SecurityOptions`
# shape); its absence from that list means the daemon is not running rootless.
_ROOTLESS_SECURITY_OPTION = "name=rootless"

# Substrings a real `docker version`/`docker --context ... version`
# invocation's stderr carries when the TLS handshake failed because the
# server certificate does not carry the name the client dialed -- verified
# against a real docker CLI (29.4.0) and a real, deliberately mis-issued
# certificate (this module's own test suite quotes both verbatim). Every
# other handshake failure (the forward not established, the daemon not
# listening) is a completely different cause and must never be reported
# with the SAN remedy (AC-FUNC-005), which is exactly what falling through
# to the generic diagnosis below, rather than matching one of these two,
# guarantees.
_CERTIFICATE_NAME_MISMATCH_MARKERS: tuple[str, ...] = (
    "x509: certificate is valid for",
    "x509: cannot validate certificate for",
)


class TransportError(RuntimeError):
    """Base for every operational failure this module raises.

    Every failure this module can produce is a `TransportError` (or a more
    specific subclass below), never a generic `Exception`, so `main` has one
    `except` clause that turns any of them into a clean, non-zero exit
    instead of a stack trace.
    """


class TransportNotSelectedError(TransportError):
    """Raised when `connect` runs without `DEVCONTAINER_TRANSPORT=ssm` ever having been selected.

    `connect` refuses to touch AWS or docker at all when this is raised: an
    unset or `TRANSPORT_SSH` selector must leave `docker-tunnel.sh` as the
    only build path that ever ran (AC-FUNC-001, AC-FUNC-002).
    """


class AgentNotOnlineError(TransportError):
    """Raised when the target instance's SSM agent is not reporting `Online`."""


class CredentialsExpiredError(TransportError):
    """Raised when the AWS CLI reports an expired or absent credential."""


class PortOccupiedError(TransportError):
    """Raised when the port recorded for a docker context is held by another process."""


class ForwardTimeoutError(TransportError):
    """Raised when the local port does not accept a connection within `SSM_FORWARD_TIMEOUT`."""


class LegacyContextError(TransportError):
    """Raised when a docker context of the target name already exists with an `ssh://` endpoint.

    That is the legacy transport `.devcontainer/remote-docker/docker-tunnel.sh`
    still uses through phase 3; `ensure_context` refuses to mutate it rather
    than overwriting a still-working path in place.
    """


class CertificateNotReadyError(TransportError):
    """Raised when the client certificate `ensure_context` needs is missing or expired."""


class DockerVersionFloorError(TransportError):
    """Raised when the daemon's reported API version is below `DOCKER_API_VERSION_FLOOR`."""


class DockerHandshakeTimeoutError(TransportError):
    """Raised when `handshake`'s version call does not answer within `DOCKER_HANDSHAKE_TIMEOUT`."""


class InvalidContextNameError(TransportError):
    """Raised when a context name does not carry `instances.docker_context_prefix(root)`.

    `connect`'s `--context` argument arrives as the full context name (spec
    Section 9's addressing table), matching `start`'s own `--context`
    argument; `instance_from_context_name` is the single place that strips
    the prefix back off to recover the bare instance name `ensure_context`
    needs for certificate lookup, rather than the caller (the `Makefile`)
    re-typing the prefix itself. A context name that does not start with
    the prefix is a caller error and fails fast instead of silently
    building a doubly-prefixed or wrong instance directory.
    """


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


def _positive_seconds_from_env(env_var: str, default_seconds: float) -> float:
    """Read `env_var` via `hostprobe.read_positive_seconds`, wrapping a failure as `TransportError`.

    `_forward_timeout_seconds` (`SSM_FORWARD_TIMEOUT`) and
    `_handshake_timeout_seconds` (`DOCKER_HANDSHAKE_TIMEOUT`) both need the
    identical "unset it to use the default of {X:g}" rejection shape but
    must raise this module's own `TransportError`, never
    `hostprobe.HostProbeError`, across the module boundary (`main`'s single
    `except TransportError` clause depends on that). Extracting the wrap
    here means both call sites share one construction site for that
    sentence instead of each retyping it, and both variables are read
    through `hostprobe.read_positive_seconds` rather than either declaring
    its own copy of the parse and validate logic.
    """
    try:
        return read_positive_seconds(env_var, default_seconds)
    except HostProbeError as exc:
        raise TransportError(
            f"ERROR: {exc}\n"
            "Set it to a positive number of seconds, or unset it to use the default of "
            f"{default_seconds:g}."
        ) from exc


def _forward_timeout_seconds() -> float:
    return _positive_seconds_from_env(
        SSM_FORWARD_TIMEOUT_ENV_VAR, _SSM_FORWARD_TIMEOUT_DEFAULT_SECONDS
    )


def resolve_transport() -> str:
    """The build path's opt-in transport selector (Approach step 2; AC-FUNC-001, AC-FUNC-002).

    Reads `DEVCONTAINER_TRANSPORT_ENV_VAR` from the environment: unset
    resolves to `TRANSPORT_SSH`, the existing `docker-tunnel.sh` build path
    this module never touches, so a caller that sets nothing new observes
    no behavior change. `TRANSPORT_SSM` is the only other accepted value,
    this module's own opt-in build path (`connect`, below). Any other value
    is a caller error and fails fast rather than silently choosing either
    transport for them.

    Raises:
        TransportError: the environment variable is set to a value that is
            neither `TRANSPORT_SSH` nor `TRANSPORT_SSM`.
    """
    raw = os.environ.get(DEVCONTAINER_TRANSPORT_ENV_VAR, TRANSPORT_SSH)
    if raw not in _VALID_TRANSPORTS:
        raise TransportError(
            f"ERROR: {DEVCONTAINER_TRANSPORT_ENV_VAR}={raw!r} is not a recognized transport\n"
            f"Set it to one of {', '.join(_VALID_TRANSPORTS)}, or unset it to use the default of "
            f"{TRANSPORT_SSH!r} (the existing docker-tunnel.sh build path)."
        )
    return raw


def _require_ssm_transport_selected() -> None:
    """Refuse to act unless `resolve_transport` selected `TRANSPORT_SSM` (AC-FUNC-001, AC-FUNC-002).

    `connect` calls this before touching AWS or docker at all: an unset or
    `TRANSPORT_SSH` selector must leave `docker-tunnel.sh` as the only build
    path that ever ran, with no forward opened and no docker context
    touched, exactly as if `connect` did not exist.

    Raises:
        TransportNotSelectedError: `resolve_transport` did not return
            `TRANSPORT_SSM`.
    """
    transport = resolve_transport()
    if transport != TRANSPORT_SSM:
        raise TransportNotSelectedError(
            f"ERROR: {DEVCONTAINER_TRANSPORT_ENV_VAR}={transport!r}; the SSM build path is opt-in\n"
            f"Set {DEVCONTAINER_TRANSPORT_ENV_VAR}={TRANSPORT_SSM!r} to select it.\n"
            f"The default, {TRANSPORT_SSH!r}, is unchanged: run make connect (docker-tunnel.sh) "
            "instead."
        )


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


def _context_endpoint_host(runner: CommandRunner, context_name: str) -> str | None:
    """The raw docker endpoint host `context_name` is recorded with, or `None` if absent.

    Delegates to `hostprobe.docker_context_endpoint_host`, the single
    `docker context inspect` reader `devcontainer_config.instances`
    (`forwarded_port`) also drives, rather than a second, independent copy
    of the same command tuple, "context not found" detection and JSON
    parse (E8-F1-S1-T1 round 2; this function itself was already extracted
    out of what was `_existing_context_port`, E6-F2-S1-T2 REFACTOR, for the
    identical reason within this module alone). `hostprobe.HostProbeError`
    is re-raised as `TransportError` here so no exception type foreign to
    this module crosses its boundary, the same convention
    `hostprobe.read_positive_seconds` documents for its own callers.
    """
    try:
        return docker_context_endpoint_host(runner, context_name)
    except HostProbeError as exc:
        raise TransportError(f"ERROR: {exc}") from exc


def _existing_context_port(runner: CommandRunner, context_name: str) -> int | None:
    """The local TCP port already recorded in `context_name`'s docker endpoint, or `None`.

    Delegates to `hostprobe.docker_context_forwarded_port`, the shared
    reader `_context_endpoint_host` above also now wraps, rather than this
    module's own copy of the `tcp://` parse.
    """
    try:
        return docker_context_forwarded_port(runner, context_name)
    except HostProbeError as exc:
        raise TransportError(f"ERROR: {exc}") from exc


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
# Docker context (spec Section 9, 5.5, 11; E6-F2-S1-T2): the context carries
# the TLS material at the port this module already allocated.
# ---------------------------------------------------------------------------


def context_name_for(instance: str, root: Path) -> str:
    """The docker context name spec Section 9's addressing table fixes for `instance`.

    Delegates to `devcontainer_config.instances.docker_context`, the single
    place Section 9's addressing table is derived (AC-FUNC-001): the
    context name is `<repo-slug>-<instance>`, `repo.repo_slug(root)`
    derived, never a literal (AC-FUNC-002, E8-F1-S1-T1 round 2).
    """
    return instances.docker_context(root, instance)


def instance_from_context_name(root: Path, context_name: str) -> str:
    """The bare instance name `context_name_for` built `context_name` from.

    The exact inverse of `context_name_for`: `connect` receives a full
    context name through `--context` (matching `start`'s own argument) and
    needs the bare instance name back to look up certificate material
    (`certs.CertPaths`). The prefix stripped here is
    `instances.docker_context_prefix(root)`, the identical value
    `context_name_for` composes with, so the two directions of the mapping
    can never drift apart; keeping the strip here, rather than in the
    `Makefile` recipe that calls `connect`, also means a caller-side strip
    of a literal never silently no-ops for a context name that does not
    start with the real prefix.

    Raises:
        InvalidContextNameError: `context_name` does not start with
            `instances.docker_context_prefix(root)`.
    """
    prefix = instances.docker_context_prefix(root)
    if not context_name.startswith(prefix):
        raise InvalidContextNameError(
            f"ERROR: docker context name {context_name!r} does not start with {prefix!r}\n"
            f"Every context this module manages is named {prefix}<instance> "
            "(spec Section 9); pass the full context name as recorded in REMOTE_DOCKER_CONTEXT."
        )
    return context_name[len(prefix) :]


def _docker_endpoint_value(*, port: int, ca_path: Path, cert_path: Path, key_path: Path) -> str:
    """The single `--docker` value string carrying all four endpoint components (spec Section 5.5).

    One construction site (TDD REFACTOR of this task's own Approach) so the
    `host`/`ca`/`cert`/`key` spelling this module's `--docker` value uses
    exists exactly once; `build_context_create_argv` and
    `build_context_update_argv` both call this rather than each formatting
    their own copy.
    """
    return f"host=tcp://{_LOCALHOST}:{port},ca={ca_path},cert={cert_path},key={key_path}"


def build_context_create_argv(
    *, context_name: str, port: int, ca_path: Path, cert_path: Path, key_path: Path
) -> list[str]:
    """The `docker context create` argument vector for one instance's context (AC-FUNC-001).

    Every element is a separate list entry, never a shell string (spec
    Section 3.4), and the endpoint scheme is always `tcp://`, never
    `ssh://`: this function is what `tests/test_transport.py` inspects
    directly to prove it (AC-TEST-001).
    """
    return [
        "docker",
        "context",
        "create",
        context_name,
        "--docker",
        _docker_endpoint_value(port=port, ca_path=ca_path, cert_path=cert_path, key_path=key_path),
    ]


def build_context_update_argv(
    *, context_name: str, port: int, ca_path: Path, cert_path: Path, key_path: Path
) -> list[str]:
    """The identical endpoint value `build_context_create_argv` builds, under `context update`.

    An existing context is updated in place rather than deleted and
    recreated (AC-FUNC-002): this function, not a `context rm` followed by
    `build_context_create_argv`, is `ensure_context`'s update path.
    """
    return [
        "docker",
        "context",
        "update",
        context_name,
        "--docker",
        _docker_endpoint_value(port=port, ca_path=ca_path, cert_path=cert_path, key_path=key_path),
    ]


def _run_docker_context_argv(runner: CommandRunner, argv: Sequence[str], context_name: str) -> None:
    """Issue a `docker context create`/`update` argument vector, translating failure.

    The single site both `ensure_context` branches funnel through (DRY),
    the same pattern `_run_aws_cli` already establishes for this module's
    AWS call sites.
    """
    result = runner(tuple(argv), None)
    if result.binary_missing:
        raise TransportError(
            "ERROR: docker is not on PATH\nInstall Docker Desktop or the Docker CLI, then retry."
        )
    if result.exit_code != 0:
        raise TransportError(
            f"ERROR: docker context {' '.join(argv[2:4])!r} failed for context {context_name!r}\n"
            f"{result.stderr.strip()}\n"
            "Confirm docker is installed and the certificate paths above are readable, then "
            "retry."
        )


def _ensure_client_certificate_ready(
    paths: certs.CertPaths, reference_time: datetime.datetime
) -> None:
    """Refuse before `ensure_context` ever creates or updates a context (Error Handling Contract).

    Calls `certs.classify` (E6-F1-S1-T2) for the expiry decision rather than
    reimplementing its warning-window arithmetic a second time (AC-TEST-004,
    DRY). `warn_days=0` is passed because only `certs.STATUS_EXPIRED` --
    which `classify` decides before it ever consults `warn_days` -- matters
    to this precondition: a certificate still inside its warning window is
    not refused here, only `make cert-status`'s own `RENEW` row surfaces
    that.

    Raises:
        CertificateNotReadyError: no client certificate exists yet for
            `paths.instance`, or the one on disk has already expired as of
            `reference_time`.
    """
    if not paths.client_cert.is_file():
        raise CertificateNotReadyError(
            f"ERROR: no client certificate exists for instance {paths.instance!r}\n"
            f"Expected {paths.client_cert} to already exist.\n"
            f"Run make cert-status, then "
            f"{certs.RENEW_INVOCATION_TEMPLATE.format(instance=paths.instance)} to issue one."
        )
    expiry = certs.not_after(paths.client_cert)
    if certs.classify(expiry, reference_time, warn_days=0) == certs.STATUS_EXPIRED:
        raise CertificateNotReadyError(
            f"ERROR: the client certificate for instance {paths.instance!r} has expired\n"
            f"{paths.client_cert} expired on {expiry.date()}.\n"
            f"Run make cert-status, then "
            f"{certs.RENEW_INVOCATION_TEMPLATE.format(instance=paths.instance)} to reissue it."
        )


def ensure_context(
    runner: CommandRunner,
    *,
    instance: str,
    root: Path,
    port: int,
    reference_time: datetime.datetime,
    certs_root: Path = certs.DEFAULT_CERTS_ROOT,
) -> str:
    """Create or update the docker context for `instance`, carrying the TLS material.

    Refuses before touching docker at all if the client certificate spec
    Section 5.5 fixes under `certs_root` is missing or expired
    (`_ensure_client_certificate_ready`, AC-FUNC-006). An absent context is
    created; an existing context is updated in place; an existing context
    whose endpoint is `ssh://` -- the legacy transport -- is refused by name
    and left completely untouched (AC-FUNC-002): neither `docker context
    create` nor `docker context update` nor `docker context rm` is ever
    issued for that case.

    Returns:
        The context name (`context_name_for(instance, root)`), so a caller
        can chain directly into `handshake`.

    Raises:
        CertificateNotReadyError: the client certificate is missing or
            expired.
        LegacyContextError: a context of this name already exists with an
            `ssh://` endpoint.
        TransportError: docker is not on PATH, or the create/update command
            itself failed.
    """
    paths = certs.CertPaths(instance=instance, root=certs_root)
    _ensure_client_certificate_ready(paths, reference_time)
    name = context_name_for(instance, root)
    existing_host = _context_endpoint_host(runner, name)
    if existing_host is not None and existing_host.startswith(_SSH_ENDPOINT_PREFIX):
        raise LegacyContextError(
            f"ERROR: docker context {name!r} already exists with an ssh:// endpoint: "
            f"{existing_host}\n"
            "This is the legacy SSH context phase 3 must leave working; refusing to "
            "overwrite it with the mTLS transport.\n"
            "Remove the legacy context by hand first if replacing it is truly intended."
        )
    build_argv = (
        build_context_update_argv if existing_host is not None else build_context_create_argv
    )
    argv = build_argv(
        context_name=name,
        port=port,
        ca_path=paths.ca_cert,
        cert_path=paths.client_cert,
        key_path=paths.client_key,
    )
    _run_docker_context_argv(runner, argv, name)
    return name


def _use_context(runner: CommandRunner, context_name: str) -> None:
    """Activate `context_name` as the docker CLI's active context (AC-FUNC-003).

    `ensure_context` only creates or updates the context; the daemon a
    subsequent `make build` targets is whichever context is *active*.
    `docker-tunnel.sh` sets that with its own `docker context use` call once
    it finishes creating or updating the legacy `ssh://` context, so
    `connect` issues the identical call for the `tcp://` context it just
    ensured, rather than leaving activation to a caller who might not know
    it is required.

    Raises:
        TransportError: docker is not on PATH, or the `context use` command
            itself failed. The context was already created or updated
            successfully at this point; only activating it failed.
    """
    result = runner(("docker", "context", "use", context_name), None)
    if result.binary_missing:
        raise TransportError(
            "ERROR: docker is not on PATH\nInstall Docker Desktop or the Docker CLI, then retry."
        )
    if result.exit_code != 0:
        raise TransportError(
            f"ERROR: docker context use {context_name!r} failed\n"
            f"{result.stderr.strip()}\n"
            f"The context was created or updated successfully; only activating it failed. Run "
            f"'docker context use {context_name}' by hand, then retry."
        )


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
# Handshake (spec Section 4.2.1, 6, 7.2, 7.3, 11; E6-F2-S1-T2): the docker
# version call that proves the whole path -- forward, context, TLS material
# -- actually works, and the failure translator that keeps a certificate
# problem from reading like a tunnel problem.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HandshakeResult:
    """The two daemon facts a completed handshake confirms (spec Section 4.2.1, 6)."""

    server_version: str
    api_version: str
    rootless: bool


def _handshake_timeout_seconds() -> float:
    """`DOCKER_HANDSHAKE_TIMEOUT` (spec Section 7.3), read fresh, exactly once per handshake call.

    The one function in this module that reads this variable (AC-FUNC-007).
    Delegates to `_positive_seconds_from_env`, the one construction site in
    this module for the "read through `hostprobe.read_positive_seconds`,
    then wrap a rejection as `TransportError`" shape that this function and
    `_forward_timeout_seconds` both need, rather than either declaring its
    own copy of the parse, validation or wrap.
    """
    return _positive_seconds_from_env(
        DOCKER_HANDSHAKE_TIMEOUT_ENV_VAR, DOCKER_HANDSHAKE_TIMEOUT_DEFAULT_SECONDS
    )


def _is_certificate_name_mismatch(stderr: str) -> bool:
    return any(marker in stderr for marker in _CERTIFICATE_NAME_MISMATCH_MARKERS)


def diagnose_handshake_failure(
    stderr: str, *, context_name: str, instance: str, failure_summary: str = "failed"
) -> str:
    """Translate one handshake failure's stderr into an actionable diagnosis (spec Section 5.5, 11).

    TLS validates the name the *client* used to dial the daemon --
    `127.0.0.1`, through the loopback forward -- never the instance's own
    hostname or private address. A server certificate issued for either of
    those produces exactly the opaque Go `crypto/tls` failure
    `_CERTIFICATE_NAME_MISMATCH_MARKERS` recognizes, which names neither the
    forward nor the SAN requirement on its own; this function is what closes
    that gap (AC-FUNC-004). A connection-level failure (the forward not
    established, the daemon not listening) is a completely different cause
    and must never be reported with the SAN remedy, which would send an
    operator to reissue a certificate that may already be correct
    (AC-FUNC-005) -- every stderr that does not match one of those two
    markers falls through to the generic diagnosis below instead.

    `failure_summary` replaces the default "failed" in the first line so a
    caller with a more specific fact to state (`handshake`'s
    `DockerHandshakeTimeoutError`, which knows the port and the deadline
    that elapsed) can say so without duplicating this function's own
    stderr-driven diagnosis text (DRY): the production timeout path and
    this function's own tests exercise the identical diagnosis body,
    instead of the timeout path repeating this function's generic
    connection remedy sentence verbatim.
    """
    if _is_certificate_name_mismatch(stderr):
        reissue = certs.RENEW_INVOCATION_TEMPLATE.format(instance=instance)
        return (
            f"ERROR: docker version handshake for context {context_name!r} {failure_summary}: "
            "certificate name mismatch\n"
            f"{stderr.strip()}\n"
            f"The server certificate must carry {certs.SERVER_SAN}: TLS validates the name "
            "the client used through the loopback forward (127.0.0.1), never the instance's "
            "own hostname or private address.\n"
            f"Reissue it: {reissue}"
        )
    return (
        f"ERROR: docker version handshake for context {context_name!r} {failure_summary}\n"
        + (f"{stderr.strip()}\n" if stderr.strip() else "")
        + "Confirm the SSM port forward is still established and the daemon behind it is "
        "running, then retry."
    )


def _api_version_tuple(value: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in value.split("."))
    except ValueError as exc:
        raise TransportError(
            f"ERROR: docker reported an unparsable API version {value!r}\n"
            "Confirm the daemon behind this context is a genuine docker engine, then retry."
        ) from exc


def _ensure_api_version_floor(observed_api_version: str, context_name: str) -> None:
    if _api_version_tuple(observed_api_version) < _api_version_tuple(DOCKER_API_VERSION_FLOOR):
        raise DockerVersionFloorError(
            f"ERROR: docker API version {observed_api_version} for context {context_name!r} "
            f"is below the required floor of {DOCKER_API_VERSION_FLOOR}\n"
            f"mTLS on a TCP listener with a rootless daemon (spec Section 6) requires at "
            f"least API version {DOCKER_API_VERSION_FLOOR}.\n"
            "Upgrade the docker engine on the instance, then retry."
        )


def _parse_server_payload(stdout: str, context_name: str) -> dict[str, str]:
    try:
        payload = json.loads(stdout)
        return {"Version": str(payload["Version"]), "ApiVersion": str(payload["ApiVersion"])}
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise TransportError(
            f"ERROR: docker version for context {context_name!r} produced unparsable output: "
            f"{stdout.strip()!r}\n"
            "Run the command manually and investigate why its output does not match the "
            "expected shape."
        ) from exc


def _probe_rootless(runner: CommandRunner, context_name: str, *, timeout_seconds: float) -> bool:
    """Whether the daemon behind `context_name` reports running rootless (spec Section 4.2.1).

    Called only after `handshake`'s own version call has already answered
    successfully, so a failure here is reported as its own distinct
    `TransportError` rather than folded into the version-call retry loop:
    the daemon is already known reachable, so this is not a readiness
    condition to wait out.
    """
    command = ("docker", "--context", context_name, "info", "--format", "{{json .SecurityOptions}}")
    result = runner(command, timeout_seconds)
    if result.binary_missing:
        raise TransportError(
            "ERROR: docker is not on PATH\nInstall Docker Desktop or the Docker CLI, then retry."
        )
    if result.exit_code != 0:
        raise TransportError(
            f"ERROR: docker info for context {context_name!r} failed after a successful "
            "version handshake\n"
            f"{result.stderr.strip()}\n"
            "Confirm the daemon behind this context is still reachable, then retry."
        )
    try:
        options = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise TransportError(
            f"ERROR: docker info for context {context_name!r} produced unparsable security "
            f"options: {result.stdout.strip()!r}\n"
            "Run the command manually and investigate why its output does not match the "
            "expected shape."
        ) from exc
    return _ROOTLESS_SECURITY_OPTION in (options or [])


def _raise_handshake_timeout(
    last_stderr: str, *, context_name: str, instance: str, port: int, timeout: float
) -> NoReturn:
    """Raise `DockerHandshakeTimeoutError`, naming the context, the port and the variable.

    The one construction site for that message: `handshake` calls this both
    when the version call never answers before `deadline` and when the
    deadline is exhausted between the version call answering and the
    rootless probe running, so the two exhaustion points cannot report two
    different message shapes for what is the same cause.
    """
    raise DockerHandshakeTimeoutError(
        diagnose_handshake_failure(
            last_stderr,
            context_name=context_name,
            instance=instance,
            failure_summary=(
                f"on local port {port} did not answer within "
                f"{DOCKER_HANDSHAKE_TIMEOUT_ENV_VAR}={timeout:g}s"
            ),
        )
    )


def handshake(runner: CommandRunner, *, instance: str, root: Path, port: int) -> HandshakeResult:
    """Complete a docker version handshake against `instance`'s docker context (spec Section 4.2.1).

    The version call is retried until it answers, bounded overall by
    `DOCKER_HANDSHAKE_TIMEOUT` (module docstring: every attempt shells out
    to a real `docker` CLI process, which paces the loop by its own
    unavoidable fork/exec and TLS-dial cost without this module ever adding
    a fixed delay). A certificate-name mismatch is a permanent cause, never
    a transient one, so it stops the loop immediately rather than spending
    the rest of the deadline retrying a call that cannot succeed
    (AC-FUNC-004); every other failure keeps retrying until the deadline,
    at which point it is reported as `DockerHandshakeTimeoutError`, naming
    the context, the port and `DOCKER_HANDSHAKE_TIMEOUT`. Once the version
    call answers, this also confirms the API version meets
    `DOCKER_API_VERSION_FLOOR` and reports whether the daemon is running
    rootless, probed with whatever time remains until the same deadline
    (never the full timeout again): passing the full timeout a second time
    would let total wall time approach 2x `DOCKER_HANDSHAKE_TIMEOUT`, which
    would contradict `docs/devcontainer.md`'s statement that the whole
    handshake is bounded by it. If the deadline is already exhausted by the
    time the version call answers, this reports the same
    `DockerHandshakeTimeoutError` instead of handing `docker info` a
    timeout of zero, which would fail instantly with a blank stderr line
    that names neither the forward nor the deadline.

    Raises:
        DockerVersionFloorError: the daemon answered but its API version is
            below `DOCKER_API_VERSION_FLOOR`.
        DockerHandshakeTimeoutError: the version call never answered within
            `DOCKER_HANDSHAKE_TIMEOUT`, or the deadline expired before the
            rootless probe could run.
        TransportError: docker is not on PATH, a certificate-name mismatch
            or other translatable handshake failure occurred
            (`diagnose_handshake_failure`), or a response could not be
            parsed.
    """
    context_name = context_name_for(instance, root)
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
            break
        result = runner(version_command, remaining)
        if result.binary_missing:
            raise TransportError(
                "ERROR: docker is not on PATH\nInstall Docker Desktop or the Docker CLI, then "
                "retry."
            )
        if result.exit_code == 0:
            server = _parse_server_payload(result.stdout, context_name)
            _ensure_api_version_floor(server["ApiVersion"], context_name)
            rootless_budget = deadline - time.monotonic()
            if rootless_budget <= 0:
                _raise_handshake_timeout(
                    last_stderr,
                    context_name=context_name,
                    instance=instance,
                    port=port,
                    timeout=timeout,
                )
            rootless = _probe_rootless(runner, context_name, timeout_seconds=rootless_budget)
            return HandshakeResult(
                server_version=server["Version"],
                api_version=server["ApiVersion"],
                rootless=rootless,
            )
        last_stderr = result.stderr
        if _is_certificate_name_mismatch(last_stderr):
            raise TransportError(
                diagnose_handshake_failure(
                    last_stderr, context_name=context_name, instance=instance
                )
            )
    _raise_handshake_timeout(
        last_stderr, context_name=context_name, instance=instance, port=port, timeout=timeout
    )


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


@dataclass(frozen=True)
class _EstablishedForward:
    """A ready SSM forward: the launched process, its readable `stdout`, and the local port.

    `session_output` is carried alongside `process` (rather than a caller
    re-reading `process.stdout`, which mypy would still see as
    `IO[bytes] | None`) because `_establish_forward` already proved it is
    not `None` before ever returning one of these.
    """

    process: subprocess.Popen[bytes]
    session_output: IO[bytes]
    local_port: int


def _establish_forward(
    *, instance_id: str, context_name: str, profile: str, region: str
) -> _EstablishedForward | None:
    """Establish and confirm-ready one SSM port forward; the shared core of `start` and `connect`.

    Extracted from what was `_run_start`'s own body (TDD REFACTOR, DRY):
    `connect` needs the identical agent-online check, port allocation,
    session launch and readiness wait `start` already had, and duplicating
    that here rather than sharing it would let the two drift. This is a
    pure extraction, not a behavior change: `start`'s own tests keep
    passing unmodified.

    Returns `None` only when a `KeyboardInterrupt` arrived before the
    forward was confirmed ready, having already been cleanly torn down:
    both callers treat that as the operator having exited cleanly, before
    either would ever print anything or touch a docker context.

    Raises:
        ForwardTimeoutError: the forward never became ready within
            `SSM_FORWARD_TIMEOUT`, having already been torn down.
        TransportError: the launched session process was not given a
            readable `stdout` pipe (a `default_process_launcher` bug).
    """
    ensure_agent_online(
        subprocess_command_runner, instance_id=instance_id, profile=profile, region=region
    )
    local_port = allocate_local_port(subprocess_command_runner, context_name)
    process = start_forward(
        default_process_launcher,
        instance_id=instance_id,
        local_port=local_port,
        profile=profile,
        region=region,
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
            tcp_ready_probe(local_port), session_output, instance_id=instance_id, port=local_port
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
        return None
    return _EstablishedForward(
        process=process, session_output=session_output, local_port=local_port
    )


def _run_until_interrupted(process: subprocess.Popen[bytes], session_output: IO[bytes]) -> None:
    """Drain `session_output` and wait for `process` to exit, tearing it down either way.

    Shared by `start` and `connect` (TDD REFACTOR, DRY): both keep the
    forward open, echoing the session process's own output, until it exits
    or the operator interrupts, and both must stop the forward on the way
    out regardless of which of those two happened.
    """
    try:
        _drain_session_output(session_output)
        process.wait()
    except KeyboardInterrupt:
        pass
    finally:
        stop_forward(process)


@contextlib.contextmanager
def _teardown_forward_on_error(process: subprocess.Popen[bytes]) -> Iterator[None]:
    """Stop `process` via `stop_forward` if the wrapped block raises anything, then re-raise.

    `connect`'s post-forward setup (`ensure_context`, `_use_context`,
    `handshake`) can fail for reasons `_establish_forward` never
    anticipates: a client certificate that went missing or expired between
    processes, a pre-existing `ssh://` context, a SAN-mismatch handshake
    failure, `docker` missing from `PATH`, even a `KeyboardInterrupt`. Every
    one of those must leave the already-launched `aws ssm start-session`
    child stopped rather than orphaned holding a live AWS session and the
    allocated loopback port, exactly as `_establish_forward` itself already
    guarantees for `ForwardTimeoutError` and a `KeyboardInterrupt` during
    the readiness wait. Catches `BaseException`, not `Exception`, so a
    `KeyboardInterrupt` raised anywhere in the wrapped block is covered
    too; the caught exception is always re-raised unchanged, never
    swallowed.
    """
    try:
        yield
    except BaseException:
        stop_forward(process)
        raise


def _run_start(args: argparse.Namespace) -> int:
    established = _establish_forward(
        instance_id=args.instance_id,
        context_name=args.context,
        profile=args.profile,
        region=args.region,
    )
    if established is None:
        return 0
    print(
        f"Port forward ready: 127.0.0.1:{established.local_port} -> {args.instance_id} over "
        f"{PORT_FORWARDING_DOCUMENT}."
    )
    _run_until_interrupted(established.process, established.session_output)
    return 0


def _run_connect(args: argparse.Namespace) -> int:
    """Handler for `connect`: the SSM build path's context-creation entry point (Changes Manifest).

    Establishes the forward exactly as `start` does, then creates or
    updates the docker context carrying the TLS material at the port just
    allocated (`ensure_context`), activates it (`_use_context`) so a
    subsequent `make build` targets it rather than whichever context was
    active before, and confirms it answers with a version handshake
    (`handshake`) -- the calls `start` alone never makes, which is the gap
    this task closes (AC-FUNC-003). Refuses before doing anything at all --
    before even checking whether the SSM agent is online -- unless the
    caller opted into `DEVCONTAINER_TRANSPORT=ssm`
    (`_require_ssm_transport_selected`, AC-FUNC-001, AC-FUNC-002): an unset
    or `ssh` selector leaves `docker-tunnel.sh` as the only build path that
    ran, with no forward opened and no docker context touched.

    Takes `--context` (the full docker context name), matching `start`'s
    own argument, rather than a bare `--instance`: the `Makefile`'s
    `connect` recipe already has the full context name in
    `REMOTE_DOCKER_CONTEXT` and passing it straight through means the
    `<repo-slug>-` prefix (`devcontainer_config.instances.docker_context_prefix`)
    is declared in exactly one place, `instances.py`, instead of being
    re-typed (and silently mis-stripped for a non-default context name) in
    the recipe. `instance_from_context_name` recovers the bare instance
    name `ensure_context` needs for certificate lookup.

    The client certificate precondition `ensure_context` would otherwise
    check only after the forward is already open is checked again here,
    first, before `_establish_forward` ever opens an AWS session: a missing
    or expired certificate is the most common reason this handler fails,
    it needs no AWS or docker call to detect, and paying for a live SSM
    session only to immediately tear it down again would violate the
    fail-fast standard's "check prerequisites before starting work". Once
    the forward is open, everything through `handshake` runs inside
    `_teardown_forward_on_error` so a certificate that expired in the
    interim, a pre-existing `ssh://` context, a SAN-mismatch handshake
    failure, `docker` going missing from `PATH`, or an operator
    `KeyboardInterrupt` all stop the forward before propagating, rather
    than orphaning the `aws ssm start-session` child and the loopback port
    it holds.
    """
    _require_ssm_transport_selected()
    root = repo.find_root(Path.cwd())
    context_name = args.context
    instance = instance_from_context_name(root, context_name)
    reference_time = datetime.datetime.now(datetime.UTC)
    _ensure_client_certificate_ready(
        certs.CertPaths(instance=instance, root=args.certs_root), reference_time
    )
    established = _establish_forward(
        instance_id=args.instance_id,
        context_name=context_name,
        profile=args.profile,
        region=args.region,
    )
    if established is None:
        return 0
    with _teardown_forward_on_error(established.process):
        ensure_context(
            subprocess_command_runner,
            instance=instance,
            root=root,
            port=established.local_port,
            reference_time=reference_time,
            certs_root=args.certs_root,
        )
        _use_context(subprocess_command_runner, context_name)
        result = handshake(
            subprocess_command_runner, instance=instance, root=root, port=established.local_port
        )
    rootless_note = ", rootless" if result.rootless else ""
    print(
        f"Connected: docker context {context_name!r} -> {args.instance_id} over "
        f"{PORT_FORWARDING_DOCUMENT} (server {result.server_version}, api {result.api_version}"
        f"{rootless_note})."
    )
    _run_until_interrupted(established.process, established.session_output)
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
        "--context",
        required=True,
        help="The docker context name (<repo-slug>-<name>, e.g. general-dev-<name> here).",
    )
    start_parser.add_argument("--profile", required=True, help="The AWS SSO profile.")
    start_parser.add_argument("--region", required=True, help="The AWS region.")
    start_parser.set_defaults(handler=_run_start)

    connect_parser = subparsers.add_parser(
        "connect",
        help=(
            "Establish the SSM forward and create/update the docker context carrying the TLS "
            f"material (opt-in: {DEVCONTAINER_TRANSPORT_ENV_VAR}={TRANSPORT_SSM})."
        ),
    )
    connect_parser.add_argument("--instance-id", required=True, help="The EC2 instance id (i-...).")
    connect_parser.add_argument(
        "--context",
        required=True,
        help="The docker context name (<repo-slug>-<name>, e.g. general-dev-<name> here).",
    )
    connect_parser.add_argument("--profile", required=True, help="The AWS SSO profile.")
    connect_parser.add_argument("--region", required=True, help="The AWS region.")
    connect_parser.add_argument(
        "--certs-root",
        type=Path,
        default=certs.DEFAULT_CERTS_ROOT,
        help=f"Certificate material root (default: {certs.DEFAULT_CERTS_ROOT}).",
    )
    connect_parser.set_defaults(handler=_run_connect)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Parse `argv`, run the selected command, and return an exit code.

    Every handler raises a `TransportError` on a real failure instead of
    exiting itself; this is the one place that exception becomes a
    non-zero exit code, printed with an `ERROR:` prefix to stderr, never a
    stack trace. `connect` also resolves the repository root
    (`repo.find_root`) to derive the docker context name
    (`devcontainer_config.instances.docker_context`, AC-FUNC-002), so
    `repo.RepoError` is caught here the same way, the same convention
    `devcontainer_config.cli.main` already establishes for that exception.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except (TransportError, repo.RepoError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
