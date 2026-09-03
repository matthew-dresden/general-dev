"""Instance discovery, naming, and the Section 9 addressing table (spec Section 4.5).

Section 4.5 assigns this module exactly one job: "discovery, naming, and the
addressing table in Section 9." Section 9's table has six rows -- Terragrunt
directory, state key, docker context, parameter prefix, certificate
directory, forwarded port -- and this module is the only place any of them
is derived. Before this module existed, every consumer that needed one of
those six values (a skill, a Makefile recipe, a Python module) built its own
copy of the interpolation, and a second instance an operator added silently
collided with the first one's transport state, docker context or Parameter
Store prefix, with nothing failing at the point of the mistake (AC-9.1).

An instance name keys a filesystem path, a docker context name and a
Parameter Store path all at once, so `validate_name` is called at the top of
every function in this module that composes one of those from a name (spec
Section 9, AC-FUNC-008): a name carrying a path separator or `..` is a
path-traversal vector on the first two and a parameter-injection vector on
the third, and an unvalidated name is rejected before it reaches any of
them. Validation delegates its character-class rule to
`devcontainer_config.catalog._validate_scope`, the control that module
carries for the identical `/devcontainer/<scope>/` path interpolation,
rather than a second, independent copy of the same rule; this module adds
only the length bound `catalog._validate_scope` does not enforce, since
spec Section 9 has no scope concept and no per-secret name to bound
instead. `devcontainer_config.certs._validate_instance` delegates to this
module's own `validate_name` rather than to `catalog._validate_scope`
directly, so the length bound applies uniformly wherever an instance name
is validated (E8-F1-S1-T1 round 2, code_review WARN: DRY). Reaching into
`catalog._validate_scope`, a private symbol, is a pre-existing cross-module
encapsulation gap `certs._validate_instance` already set precedent for;
code_review round 2 filed promoting it to a public `catalog.validate_scope`
as a non-blocking observation for a future unit, not this one.

`forwarded_port` is the sixth artifact, and the only one that is not a pure
string/path derivation: spec Section 9 fixes the local forwarded port as
"allocated per instance, recorded, never a fixed number," and
`devcontainer_config.transport`'s own module docstring records that the
record IS the docker context endpoint itself. This module therefore reads
that endpoint through `devcontainer_config.hostprobe.docker_context_forwarded_port`,
the single `docker context inspect <name> --format {{json .Endpoints}}`
reader (parsed as `tcp://<host>:<port>`) that `devcontainer_config.transport`
(`allocate_local_port`, `ensure_context`) also drives (E8-F1-S1-T1 round 2):
`hostprobe` is the one module both this module and `transport` already
import, so extracting the reader there is what lets `transport` depend on
this module's `docker_context`/`docker_context_prefix` for the addressing
row (AC-FUNC-001, AC-FUNC-002) without the two modules importing each
other. Every command this needs is issued through an injected
`devcontainer_config.hostprobe.CommandRunner`, the same seam
`devcontainer_config.transport` and `devcontainer_config.hostprobe` already
share, so this module's own unit suite never shells out to a real `docker`
binary (AC-TEST-004): no docker, no AWS, no network.

`resolve` implements the instance-resolution order spec Section 4.1.1 fixes,
evaluated once per call: `INSTANCE` from the environment, then
`DEFAULT_REMOTE_INSTANCE` from the environment, then the sole directory
under `remote-instances/` when exactly one is configured, otherwise
failure. Both environment variables are read directly from `os.environ`
inside this function, the same convention
`devcontainer_config.transport.resolve_transport` already establishes for
`DEVCONTAINER_TRANSPORT` rather than an injected mapping parameter, so a
caller (or a test, through `monkeypatch.setenv`/`delenv`) needs nothing
beyond the real environment. Whether the local backend is active is passed
in explicitly as `local_backend_active` rather than discovered here: spec
Section 1.1 records that the active docker context is the only source of
truth for that distinction, discovering it needs a docker call this module
has no other reason to make, and the caller (the `Makefile`, or this
module's own `resolve-instance` entry point on `cli.py`) already knows the
answer by the time it calls `resolve`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from devcontainer_config import catalog, repo
from devcontainer_config.hostprobe import (
    CommandRunner,
    HostProbeError,
    docker_context_forwarded_port,
)

# spec Section 9: "Terragrunt directory | remote-instances/<name>/". The
# directory under the repository root that holds one subdirectory per
# instance, plus the two entries `discover` must ignore: `root.hcl` (a
# file, filtered out by the `is_dir()` check alone) and `_envcommon/`
# (a directory, filtered out by name because it is shared configuration,
# never an instance).
INSTANCES_DIR_NAME = "remote-instances"

_ENVCOMMON_DIR_NAME = "_envcommon"

# spec Section 9: "State key | <name>/terraform.tfstate".
STATE_KEY_FILENAME = "terraform.tfstate"

# spec Section 5.5: certificate material lives under `~/.docker/certs/<name>/`
# by default, but honors `DOCKER_CONFIG` (the real docker CLI's own
# environment variable for relocating `~/.docker`) when an operator has
# moved their docker configuration home. `devcontainer_config.certs.
# DEFAULT_CERTS_ROOT` is sourced from this module's own `certs_root` (see
# that function), so the addressing-table row spec Section 9 assigns this
# module and certs' own on-disk material root are one derivation, not two.
DOCKER_CONFIG_ENV_VAR = "DOCKER_CONFIG"
_DEFAULT_DOCKER_HOME_DIRNAME = ".docker"
_CERTS_SUBDIRNAME = "certs"

# The two environment variables spec Section 4.1.1's resolution order names,
# read directly from `os.environ` inside `resolve` (see the module
# docstring for why this is not an injected parameter).
INSTANCE_ENV_VAR = "INSTANCE"
DEFAULT_REMOTE_INSTANCE_ENV_VAR = "DEFAULT_REMOTE_INSTANCE"

# An instance name is a non-empty path segment (delegated to
# `catalog._validate_scope`, see the module docstring) bounded to 63
# characters: the DNS-label length limit, chosen because this same value is
# interpolated into a docker context name and a Parameter Store path
# segment, neither of which documents its own ceiling, and the DNS-label
# bound is the narrowest one this module's own consumers are known to touch
# (a docker context name is frequently rendered into a hostname-shaped
# label by tooling downstream of this project).
MAX_INSTANCE_NAME_LENGTH = 63


class InstancesError(RuntimeError):
    """Base class for every failure this module raises.

    `devcontainer_config.cli`'s `main` catches this one class, not each
    subclass individually, so a new failure mode added here is caught by
    `main` without also having to edit its `except` clause (open/closed).
    """


class InvalidInstanceNameError(InstancesError):
    """Raised when a name fails `validate_name` before it reaches any path, context or prefix."""


class UnknownInstanceError(InstancesError):
    """Raised when `INSTANCE` or `DEFAULT_REMOTE_INSTANCE` names no configured directory."""


class NoInstancesConfiguredError(InstancesError):
    """Raised when `remote-instances/` configures no instance at all on a remote backend."""


class AmbiguousInstanceError(InstancesError):
    """Raised when more than one instance is configured and neither selector chose one."""


class ForwardedPortNotRecordedError(InstancesError):
    """Raised when `forwarded_port` finds no docker context recording an allocation."""


@dataclass(frozen=True)
class Resolution:
    """The outcome of `resolve`: the chosen instance, and any warning to surface.

    `instance` is `None` only when the local backend is active (spec
    Section 4.1.1's edge case): there is no instance to choose on that
    backend, and `INSTANCE`, if set, names nothing on it. `warning` is set
    only for that one edge case -- `INSTANCE` set while the local backend is
    active -- carrying the text a caller prints to stderr; every other
    resolution either raises or returns a `Resolution` with `warning=None`.
    """

    instance: str | None
    warning: str | None = None


def validate_name(name: str) -> None:
    """Raise `InvalidInstanceNameError` unless `name` is safe to use in a path, context or prefix.

    Delegates the character-class rule to `catalog._validate_scope`
    (see the module docstring), then adds the length bound that check does
    not enforce. Every derivation function in this module calls this first,
    so a rejected name never reaches a filesystem path, a docker context
    name or a Parameter Store prefix (AC-FUNC-008).

    Raises:
        InvalidInstanceNameError: `name` is empty, contains a path separator
            or another character outside `catalog._validate_scope`'s
            allowed set, or exceeds `MAX_INSTANCE_NAME_LENGTH` characters.
    """
    try:
        catalog._validate_scope(name)
    except catalog.InvalidScopeError as exc:
        raise InvalidInstanceNameError(
            f"ERROR: invalid instance name {name!r}\n"
            "An instance name must be a non-empty path segment: letters, digits, "
            "hyphens and underscores only. It keys a Terragrunt directory, a "
            "docker context and a Parameter Store prefix at once, so a path "
            "separator or an empty value is rejected before any of them is built.\n"
            "Use a valid instance name."
        ) from exc
    if len(name) > MAX_INSTANCE_NAME_LENGTH:
        raise InvalidInstanceNameError(
            f"ERROR: invalid instance name {name!r}\n"
            f"An instance name must be at most {MAX_INSTANCE_NAME_LENGTH} characters; "
            f"this one is {len(name)}.\n"
            "Shorten the instance name."
        )


def discover(root: Path) -> tuple[str, ...]:
    """Every configured instance name under `root/remote-instances/`, sorted.

    Only directories count (spec Section 9): `root.hcl` is a file and is
    filtered out by the `is_dir()` check alone; `_envcommon/` is a
    directory but shared configuration, not an instance, and is filtered
    out by name (AC-FUNC-004). Returns an empty tuple, never an error, when
    `remote-instances/` itself does not exist yet -- `resolve` is what
    turns "nothing configured" into `NoInstancesConfiguredError` on a
    remote backend, not this function, so a caller that only wants a
    listing is not forced to handle an exception for the empty case.
    """
    instances_dir = root / INSTANCES_DIR_NAME
    if not instances_dir.is_dir():
        return ()
    names = [
        entry.name
        for entry in instances_dir.iterdir()
        if entry.is_dir() and entry.name != _ENVCOMMON_DIR_NAME
    ]
    return tuple(sorted(names))


def terragrunt_dir(root: Path, name: str) -> Path:
    """spec Section 9: `remote-instances/<name>/`, the Terragrunt directory for `name`."""
    validate_name(name)
    return root / INSTANCES_DIR_NAME / name


def state_key(name: str) -> str:
    """spec Section 9: `<name>/terraform.tfstate`, the Terragrunt state key for `name`."""
    validate_name(name)
    return f"{name}/{STATE_KEY_FILENAME}"


def docker_context_prefix(root: Path) -> str:
    """The `<repo-slug>-` prefix `docker_context` composes every context name from.

    `<repo-slug>` is `repo.repo_slug(root)`, the identical value
    `remote-instances/root.hcl`'s own `local.repo_slug` derives (the state
    bucket name, spec Section 5.7, depends on the same value) -- never a
    literal, so a fork of this repository under a different name gets its
    own, non-colliding docker context prefix without editing this module.
    In this repository, `repo_slug` resolves to `general-dev`, matching
    spec Section 9's own worked example of `general-dev-<name>`. Public so
    `devcontainer_config.transport.instance_from_context_name` can recover
    the bare instance name from a full context name without declaring a
    second, independently drifting copy of this derivation (AC-FUNC-001,
    AC-FUNC-002).
    """
    return f"{repo.repo_slug(root)}-"


def docker_context(root: Path, name: str) -> str:
    """spec Section 9: `<repo-slug>-<name>`, the docker context name for `name`."""
    validate_name(name)
    return f"{docker_context_prefix(root)}{name}"


def parameter_prefix(name: str) -> str:
    """spec Section 5.3 / 9: `/devcontainer/<name>/`, the Parameter Store prefix for `name`.

    `catalog.PATH_ROOT` supplies the leading `/devcontainer` segment (the
    same reuse `devcontainer_config.certs`'s own `PARAMETER_ROOT` already
    documents), so the literal exists in exactly one place across both
    modules.
    """
    validate_name(name)
    return f"{catalog.PATH_ROOT}/{name}/"


def certs_root() -> Path:
    """spec Section 5.5: `~/.docker/certs`, honoring `DOCKER_CONFIG` when set.

    Docker itself relocates its entire configuration home, `~/.docker` by
    default, when `DOCKER_CONFIG` is set; this reads the identical
    variable so an operator who has moved that home also gets certificate
    material addressed under the same, moved location, rather than a
    silently stale `~/.docker/certs` no docker command ever reads.
    `devcontainer_config.certs.DEFAULT_CERTS_ROOT` is this same value: that
    module's default certificate root and this module's Section 9
    addressing row are one derivation, not two, so an operator who sets
    `DOCKER_CONFIG` never has certificates written to one directory while
    this addressing table points at another.
    """
    docker_config = os.environ.get(DOCKER_CONFIG_ENV_VAR)
    base = Path(docker_config) if docker_config else Path.home() / _DEFAULT_DOCKER_HOME_DIRNAME
    return base / _CERTS_SUBDIRNAME


def certs_dir(name: str) -> Path:
    """spec Section 5.5 / 9: `~/.docker/certs/<name>/`, honoring `DOCKER_CONFIG` when set.

    Unlike the other five derivations, this one takes no `root`:
    certificate material addresses the operator's home, not the
    repository.
    """
    validate_name(name)
    return certs_root() / name


def _recorded_local_port(runner: CommandRunner, context_name: str) -> int | None:
    """The local TCP port `context_name`'s docker context endpoint records, or `None` if absent.

    Delegates to `hostprobe.docker_context_forwarded_port`, the single
    `docker context inspect` reader `devcontainer_config.transport`
    (`allocate_local_port`) also drives, rather than a second, independent
    copy of the same command tuple, "context not found" detection, JSON
    parse and `tcp://` pattern (E8-F1-S1-T1 round 2). `hostprobe.HostProbeError`
    is re-raised as `InstancesError` here so no exception type foreign to
    this module crosses its boundary, the same convention
    `hostprobe.read_positive_seconds` documents for its own callers.
    """
    try:
        return docker_context_forwarded_port(runner, context_name)
    except HostProbeError as exc:
        raise InstancesError(f"ERROR: {exc}") from exc


def forwarded_port(root: Path, name: str, runner: CommandRunner) -> int:
    """spec Section 9: the local forwarded port already allocated and recorded for `name`.

    Reads `docker_context(root, name)`'s own endpoint (spec Section 9;
    `devcontainer_config.transport`'s module docstring: "The record is the
    docker context endpoint itself"), the same record
    `devcontainer_config.transport.allocate_local_port` writes to and reuses
    from. Never invents a port and never falls back to a fixed number: two
    instances sharing a forwarded port is precisely the collision AC-9.1
    forbids.

    Raises:
        InvalidInstanceNameError: `name` fails `validate_name`.
        ForwardedPortNotRecordedError: no docker context named
            `docker_context(root, name)` exists yet, so no port has been
            recorded for `name`.
        InstancesError: the context exists but its endpoint is unreadable,
            unparsable, or not a `tcp://` endpoint.
    """
    context = docker_context(root, name)
    port = _recorded_local_port(runner, context)
    if port is None:
        raise ForwardedPortNotRecordedError(
            f"ERROR: no forwarded port recorded for instance {name!r}\n"
            f"Docker context {context!r} does not exist yet, so no port has been "
            "allocated. Run /devcontainer:setup-remote, or connect once, to record one."
        )
    return port


def _ambiguous_instance_error(candidates: tuple[str, ...]) -> AmbiguousInstanceError:
    """spec Section 4.1.1's worked example: every configured name, plus both remedies."""
    listed = "\n".join(f"          {candidate}" for candidate in candidates)
    return AmbiguousInstanceError(
        f"ERROR: Which instance? {len(candidates)} are configured and "
        f"{DEFAULT_REMOTE_INSTANCE_ENV_VAR} is unset.\n"
        f"{listed}\n"
        f"Name one:            make <target> {INSTANCE_ENV_VAR}=<name>\n"
        f"Or set a default:    {DEFAULT_REMOTE_INSTANCE_ENV_VAR}='<name>' in shell.env"
    )


def _unknown_instance_error(
    name: str, source_env_var: str, candidates: tuple[str, ...]
) -> UnknownInstanceError:
    """Names the offending value and lists every instance actually configured."""
    listed = ", ".join(candidates) if candidates else "(none)"
    return UnknownInstanceError(
        f"ERROR: {source_env_var}={name!r} names no directory under {INSTANCES_DIR_NAME}/\n"
        f"Configured instances: {listed}\n"
        "Create the directory, or name one of the instances listed above."
    )


def resolve(root: Path, *, local_backend_active: bool) -> Resolution:
    """spec Section 4.1.1's resolution order, evaluated once: INSTANCE, then
    DEFAULT_REMOTE_INSTANCE, then a sole directory, then failure.

    Both environment variables are read directly from `os.environ` (see the
    module docstring for why). `discover(root)` is called at most once,
    exactly when a value has to be checked against or chosen from the
    configured set: the "local backend active" branch below never reaches it.

    Raises:
        InvalidInstanceNameError: `INSTANCE` or `DEFAULT_REMOTE_INSTANCE`
            fails `validate_name`.
        UnknownInstanceError: `INSTANCE` or `DEFAULT_REMOTE_INSTANCE` names
            no configured directory.
        NoInstancesConfiguredError: neither selector is set and
            `remote-instances/` configures no instance at all.
        AmbiguousInstanceError: neither selector is set and more than one
            instance is configured.
    """
    instance_arg = os.environ.get(INSTANCE_ENV_VAR) or None

    if local_backend_active:
        if instance_arg is not None:
            return Resolution(
                instance=None,
                warning=(
                    f"WARNING: {INSTANCE_ENV_VAR}={instance_arg!r} is set but the local "
                    "backend is active; it names nothing on this backend and is unused."
                ),
            )
        return Resolution(instance=None)

    candidates = discover(root)

    if instance_arg is not None:
        validate_name(instance_arg)
        if instance_arg not in candidates:
            raise _unknown_instance_error(instance_arg, INSTANCE_ENV_VAR, candidates)
        return Resolution(instance=instance_arg)

    default_instance = os.environ.get(DEFAULT_REMOTE_INSTANCE_ENV_VAR) or None
    if default_instance is not None:
        validate_name(default_instance)
        if default_instance not in candidates:
            raise _unknown_instance_error(
                default_instance, DEFAULT_REMOTE_INSTANCE_ENV_VAR, candidates
            )
        return Resolution(instance=default_instance)

    if not candidates:
        raise NoInstancesConfiguredError(
            f"ERROR: no instances are configured under {INSTANCES_DIR_NAME}/\n"
            "Run /devcontainer:setup-remote to configure the first one."
        )

    if len(candidates) == 1:
        return Resolution(instance=candidates[0])

    raise _ambiguous_instance_error(candidates)
