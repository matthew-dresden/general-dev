"""Instance lifecycle operations behind the make instance-* targets (spec Section 4.5).

This module is the Python engine behind the remote-instance operations the
Makefile exposes -- list-instances, instance-init, instance-deploy,
instance-status, instance-plan, instance-stop, instance-start,
instance-destroy and instance-link (the Makefile wiring itself is a later
unit). It owns four concerns, all built on the addressing derivations
`devcontainer_config.instances` already owns and the certificate-material
layout `devcontainer_config.certs` documents:

`scaffold` writes the one file a new instance requires,
`remote-instances/<name>/terragrunt.hcl`, from an embedded template honoring
the contract `remote-instances/README.md` fixes: the root and envcommon
includes, and an `inputs` block carrying only what genuinely differs for the
one deployment. It never runs Terragrunt -- applying the file is
`make instance-deploy`'s job -- and it never touches the network beyond the
one SSM read that pins the default AMI (skipped entirely when the caller
supplies one).

`link_id`/`unlink_id`/`recorded_id` are the per-instance id store: a file
named `instance-id` inside the instance's certificate-material directory
(`instances.certs_dir(name)`, spec Section 5.5's per-instance state dir).
This replaces `.devcontainer/record-instance.py`, which edited the shared
`shell.env` in place; an id recorded per instance dies with the instance
(`cleanup` removes the whole directory) instead of leaking into the one file
every remote target sources.

`state` reports what one instance actually looks like right now -- recorded
id, EC2 power state, Parameter Store material, certificate expiry, docker
context and forwarded port -- as an `InstanceState` value. Every aws/docker
probe failure is folded into `lookup_error` as a sanitized one-line reason
rather than raised, so a listing of many instances degrades per row instead
of aborting at the first unreachable one.

`stop`/`start` are the power operations, polling EC2 (and, for start, the
SSM agent's ping status) to their target states with an injected `sleep`, so
the whole path is testable without a clock. `cleanup` tears down everything
an instance scattered outside its Terragrunt directory -- its SSM
parameters, its docker context, its certificate directory. The remote-state
bucket is deliberately out of scope for cleanup: the fleet shares one
bucket, derived once in `remote-instances/root.hcl`, so destroying it is a
Terragrunt/backend concern, never an instance-lifecycle one.

Every aws/docker command is issued through an injected `Runner` (the same
shape `devcontainer_config.hostcreds` defines), so the unit suite runs
hermetically -- no AWS, no docker, no network -- and no test patches this
module.
"""

from __future__ import annotations

import datetime
import json
import re
import shutil
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from devcontainer_config import certs, instances
from devcontainer_config.hostprobe import CommandResult, CommandRunner

# The two external commands this module asks a runner to invoke. Every aws
# argv below starts with AWS_EXECUTABLE; every docker argv with
# DOCKER_EXECUTABLE; nothing else names an executable.
AWS_EXECUTABLE = "aws"
DOCKER_EXECUTABLE = "docker"

# The SSM public parameter holding Canonical's current Ubuntu 24.04 arm64
# image for the region the scaffold targets. Read once per scaffold when the
# caller supplies no AMI; the response is pinned into the generated
# terragrunt.hcl together with its provenance (parameter path, region, date),
# so a deployment's image is reproducible from the file alone.
AMI_SSM_PARAMETER_PATH = (
    "/aws/service/canonical/ubuntu/server/24.04/stable/current/arm64/hvm/ebs-gp3/ami-id"
)

# The default sizing for a real engine (module docstring): 4 vCPU / 8 GiB
# Graviton4, the cheapest 4 vCPU / 8 GiB ARM instance in us-east-1
# (~$0.12/hr). The generated file's own comment names t4g.medium as the
# proven cheapest size for a throwaway test engine.
DEFAULT_INSTANCE_TYPE = "c8g.xlarge"

# Root and data volume size (GiB) a scaffold starts with; both are commonly
# edited inputs the generated file's guidance message points at.
DEFAULT_VOLUME_SIZE_GB = 30

# The docker context name length limit the scaffold must respect: the
# context name is `docker_context(root, name)` (the repo slug plus the
# instance name), and a name that would exceed this bound is refused at
# scaffold time -- when choosing a shorter name is still cheap -- rather
# than at `docker context create` time, when the directory already exists.
DOCKER_CONTEXT_NAME_LIMIT = 63

# The per-instance id store: one file inside `instances.certs_dir(name)`
# (spec Section 5.5's per-instance state dir), created on write. Replaces
# `.devcontainer/record-instance.py`'s edit of the shared shell.env.
INSTANCE_ID_FILENAME = "instance-id"

# CIDR allocation: the scaffold scans sibling terragrunt.hcl files for
# `vpc_cidr = "..."` assignments and hands the new deployment the first free
# 10.x.0.0/16 block, x from FIRST_FREE_CIDR_OCTET_MIN through
# FIRST_FREE_CIDR_OCTET_MAX, with the instance subnet at 10.x.1.0/24. Pure
# file scanning: no network call decides an address.
FIRST_FREE_CIDR_OCTET_MIN = 100
FIRST_FREE_CIDR_OCTET_MAX = 254
VPC_CIDR_PATTERN = re.compile(r'vpc_cidr\s*=\s*"([^"]*)"')

# Power-op polling: after the stop/start call, describe the instance (and,
# for start, the SSM agent's ping status) at most POWER_POLL_LIMIT times,
# sleeping POWER_POLL_SECONDS between polls. Both bounds are module
# constants rather than literals at the call sites, so a test (or an
# operator) reads and drives the exact same numbers the polling loop does.
POWER_POLL_LIMIT = 60
POWER_POLL_SECONDS = 5
STOPPED_STATE = "stopped"
RUNNING_STATE = "running"
SSM_ONLINE_STATUS = "Online"

# The `--query` that turns one `aws ec2 describe-instances` call straight
# into the instance's `State.Name` text.
_EC2_STATE_QUERY = "Reservations[0].Instances[0].State.Name"


class InstanceOpsError(RuntimeError):
    """Base class for every failure this module raises.

    A caller that only needs "the instance operation failed" catches this
    one class; a caller that needs to react differently to a refused
    scaffold versus a timed-out stop catches the specific subclass below.
    Every raise site names the offending name, path or command and states
    the operator's next step.
    """


class ScaffoldError(InstanceOpsError):
    """`scaffold` refused to write, or could not resolve, a new instance's file."""


class StateError(InstanceOpsError):
    """An instance name reached `state`/`list_state` in a form nothing can address."""


class CleanupError(InstanceOpsError):
    """One or more cleanup operations failed; the message names every one of them."""


class PowerError(InstanceOpsError):
    """A power operation could not run, or the instance never reached its target state."""


# The Runner every command is handed: given the full argv, an optional stdin
# document and, optionally, a replacement environment, return a completed
# process. Mirrors `devcontainer_config.hostcreds.Runner` exactly (including
# the optional keyword-only `env`), so a runner built for either module
# works for both. Injected rather than called internally via `subprocess.run`
# directly, so every test substitutes a fake runner instead of patching this
# module.
Runner = Callable[..., subprocess.CompletedProcess[str]]


def subprocess_runner(
    argv: Sequence[str], stdin: str | None, *, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """The production Runner: a real subprocess, fed `stdin` on its stdin.

    This is what a caller outside the test suite passes to every function in
    this module. Decoding is pinned to UTF-8 with strict errors, so command
    output decodes identically on every host regardless of the ambient
    locale (the same convention `devcontainer_config.hostcreds`
    .subprocess_runner establishes).
    """
    return subprocess.run(
        list(argv),
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        env=env,
    )


# ---------------------------------------------------------------------------
# The per-instance id store.
# ---------------------------------------------------------------------------


def _id_store_path(name: str) -> Path:
    """The `instance-id` file inside `name`'s certificate-material directory.

    `instances.certs_dir(name)` validates the name on the way in, so an
    unvalidated name never composes this path.
    """
    return instances.certs_dir(name) / INSTANCE_ID_FILENAME


def link_id(root: Path, name: str, instance_id: str) -> str:
    """Record `instance_id` as `name`'s EC2 instance id; idempotent.

    Writes (or replaces) the `instance-id` file inside
    `instances.certs_dir(name)`, creating that directory on write. A second
    call replaces the previous id rather than accumulating a second one, so
    re-provisioning an instance never leaves two ids for the power and
    state operations to disagree over. `root` is accepted for call-site
    uniformity with every other operation in this module (the make wiring
    passes it everywhere) and is deliberately unused: the store addresses
    the operator's certificate-material directory, not the repository.

    Returns:
        A message naming what was recorded and where.

    Raises:
        InstanceOpsError: `instance_id` is empty or whitespace.
    """
    trimmed = instance_id.strip()
    if not trimmed:
        raise InstanceOpsError(
            "ERROR: refusing to record an empty instance id\n"
            f"link_id was asked to record an empty id for instance {name!r}.\n"
            "Pass the EC2 instance id Terragrunt output at deploy time."
        )
    path = _id_store_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(trimmed + "\n", encoding="utf-8")
    return f"recorded instance id {trimmed} for {name!r} in {path}"


def unlink_id(root: Path, name: str) -> int:
    """Remove `name`'s recorded id file, if present.

    Returns:
        1 when a file was removed, 0 when none was recorded.
    """
    path = _id_store_path(name)
    try:
        path.unlink()
    except FileNotFoundError:
        return 0
    return 1


def recorded_id(root: Path, name: str) -> str | None:
    """The EC2 instance id recorded for `name`, or None when none is.

    A missing store file -- or a missing certificate-material directory
    around it -- is the ordinary not-yet-linked state and returns None
    rather than raising; only a real read error (permissions, for example)
    propagates.
    """
    path = _id_store_path(name)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    return text.strip() or None


# ---------------------------------------------------------------------------
# Scaffold.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScaffoldResult:
    """What `scaffold` wrote and decided, for the caller to render.

    `path` is the generated terragrunt.hcl; `vpc_cidr` is the freshly
    allocated block (its instance subnet is 10.x.1.0/24, and is written
    into the file); `ami` is the AMI the file pins (the caller's override,
    or the one resolved from SSM); `messages` is the guidance a caller
    prints verbatim.
    """

    path: Path
    vpc_cidr: str
    ami: str
    messages: tuple[str, ...]


def _utc_today() -> str:
    """Today's UTC date, for the AMI provenance comment.

    Read through this one seam so a test can pin the rendered date.
    """
    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d")


def _utc_now() -> datetime.datetime:
    """The real clock, read through this one seam so `state`'s expiry
    arithmetic is testable with a fixed reference time (the same pattern
    `devcontainer_config.certs._current_time` establishes)."""
    return datetime.datetime.now(datetime.UTC)


def _invalid_name_message(name: str) -> str:
    return (
        f"ERROR: invalid instance name {name!r}\n"
        "An instance name must be a non-empty path segment of at most "
        f"{instances.MAX_INSTANCE_NAME_LENGTH} characters: letters, digits, "
        "hyphens and underscores only -- it keys a Terragrunt directory, a "
        "docker context and a Parameter Store prefix at once, and instance "
        "names are project names (e.g. brimbooks), never geographies or "
        "stages.\n"
        "Choose a valid instance name and retry."
    )


def _context_length_message(context: str) -> str:
    return (
        f"ERROR: docker context name {context!r} would exceed "
        f"{DOCKER_CONTEXT_NAME_LIMIT} characters\n"
        f"The context name is the repository slug plus the instance name "
        f"({len(context)} characters here), and docker caps a context name "
        f"at {DOCKER_CONTEXT_NAME_LIMIT}.\n"
        "Choose a shorter instance name and retry."
    )


def _exists_message(directory: Path, name: str) -> str:
    return (
        f"ERROR: {directory} already exists\n"
        f"An instance directory is scaffolded once; scaffolding over it could "
        f"silently replace edits already made to {name!r}'s deployment.\n"
        f"Edit the existing {directory / 'terragrunt.hcl'} instead, or remove "
        "the directory deliberately first if a fresh scaffold is truly intended."
    )


def _ami_unresolved_message(region: str) -> str:
    return (
        f"ERROR: could not resolve the default AMI from SSM parameter "
        f"{AMI_SSM_PARAMETER_PATH} in {region}\n"
        "scaffold pins Canonical's current Ubuntu 24.04 arm64 image by "
        "default and needs that parameter to answer.\n"
        f"Retry with an explicit image instead: make instance-init "
        f"INSTANCE=<name> AMI=<ami-id> (the manual AMI= override)."
    )


def _cidrs_exhausted_message() -> str:
    return (
        "ERROR: no free 10.x.0.0/16 block remains for a new instance\n"
        f"Every block from 10.{FIRST_FREE_CIDR_OCTET_MIN}.0.0/16 through "
        f"10.{FIRST_FREE_CIDR_OCTET_MAX}.0.0/16 is claimed by an existing "
        "deployment's terragrunt.hcl.\n"
        "Retire an instance (make instance-destroy) or allocate the new "
        "deployment's CIDRs by hand in its terragrunt.hcl."
    )


def _default_ami(runner: Runner, region: str) -> str:
    """Canonical's current Ubuntu 24.04 arm64 AMI id for `region`, via SSM.

    Raises:
        ScaffoldError: the aws call failed, or answered nothing usable --
            the message names the manual AMI= override, so an operator is
            never stuck behind a transient SSM failure.
    """
    argv = [
        AWS_EXECUTABLE,
        "ssm",
        "get-parameter",
        "--name",
        AMI_SSM_PARAMETER_PATH,
        "--region",
        region,
        "--query",
        "Parameter.Value",
        "--output",
        "text",
    ]
    try:
        result = runner(argv, None)
    except OSError as exc:
        raise ScaffoldError(
            f"ERROR: could not run {argv[0]} to resolve the default AMI\n"
            f"{exc}\n"
            f"Retry with an explicit image: make instance-init "
            f"INSTANCE=<name> AMI=<ami-id>."
        ) from exc
    value = result.stdout.strip()
    if result.returncode != 0 or not value or value == "None":
        raise ScaffoldError(_ami_unresolved_message(region))
    return value


def _allocate_cidrs(root: Path) -> tuple[str, str]:
    """The first free 10.x.0.0/16 block and its 10.x.1.0/24 instance subnet.

    Scans every sibling terragrunt.hcl under `remote-instances/` (the same
    directory set `instances.discover` enumerates) for `vpc_cidr = "..."`
    assignments and returns the first block in the reserved range no sibling
    claims. Pure file scanning: no network call decides an address.

    Raises:
        ScaffoldError: every block in the range is claimed.
    """
    used: set[str] = set()
    for sibling in instances.discover(root):
        hcl = instances.terragrunt_dir(root, sibling) / "terragrunt.hcl"
        try:
            text = hcl.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        used.update(VPC_CIDR_PATTERN.findall(text))
    for octet in range(FIRST_FREE_CIDR_OCTET_MIN, FIRST_FREE_CIDR_OCTET_MAX + 1):
        vpc_cidr = f"10.{octet}.0.0/16"
        if vpc_cidr not in used:
            return vpc_cidr, f"10.{octet}.1.0/24"
    raise ScaffoldError(_cidrs_exhausted_message())


def _render_terragrunt_hcl(
    name: str, region: str, ami: str, vpc_cidr: str, subnet_cidr: str, *, ami_from_ssm: bool
) -> str:
    """The per-instance terragrunt.hcl text, honoring `remote-instances/README.md`.

    The include block resolves root.hcl and `_envcommon/remote-ec2.hcl`
    exactly as the README's example does; the `inputs` block carries only
    what differs for this one deployment. The header tells the operator the
    directory name is the instance's identity (a project name), that every
    input is theirs to edit, and how to apply an edit.
    """
    lines = [
        f"# remote-instances/{name}/terragrunt.hcl",
        "#",
        "# The directory name IS this instance's identity, and instance names are",
        "# project names (e.g. brimbooks), never geographies or stages.",
        "#",
        "# Everything in the inputs block below is yours to edit freely: instance",
        "# type, volume sizes, availability zone, tags and AMI. Apply any edit with:",
        f"#   make instance-deploy INSTANCE={name}",
        "",
        'include "root" {',
        '  path = find_in_parent_folders("root.hcl")',
        "}",
        "",
        'include "envcommon" {',
        '  path = "${dirname(find_in_parent_folders("root.hcl"))}/_envcommon/remote-ec2.hcl"',
        "}",
        "",
        "inputs = {",
        f'  instance_name = "{name}"',
        f'  name_prefix   = "{name}"',
        "",
    ]
    if ami_from_ssm:
        lines += [
            f"  # Pinned at scaffold time from SSM parameter {AMI_SSM_PARAMETER_PATH}",
            f"  # (region {region}, {_utc_today()}).",
        ]
    else:
        lines += [
            "  # AMI pinned by hand at scaffold time (no default could be resolved);",
            "  # replace with the current Ubuntu 24.04 arm64 AMI whenever you like.",
        ]
    lines += [
        f'  ami           = "{ami}"',
        "",
        f"  # {DEFAULT_INSTANCE_TYPE}: 4 vCPU / 8 GiB Graviton4, the cheapest 4 vCPU /",
        "  # 8 GiB ARM instance in us-east-1 (~$0.12/hr) -- the default for a real",
        "  # engine. t4g.medium is the proven cheapest size for a throwaway test",
        "  # engine.",
        f'  instance_type = "{DEFAULT_INSTANCE_TYPE}"',
        "",
        f"  root_volume_size_gb = {DEFAULT_VOLUME_SIZE_GB}",
        f"  data_volume_size_gb = {DEFAULT_VOLUME_SIZE_GB}",
        "",
        f'  vpc_cidr           = "{vpc_cidr}"',
        f'  subnet_cidr        = "{subnet_cidr}"',
        f'  availability_zone  = "{region}a"',
        '  egress_cidr_blocks = ["0.0.0.0/0"]',
        "",
        "  # Both protection flags are false so a test engine can be torn down",
        "  # without ceremony. Set them to true on an engine whose accidental stop",
        "  # or termination would hurt (a real, long-lived project engine).",
        "  disable_api_termination = false",
        "  disable_api_stop        = false",
        "",
        "  tags = {",
        f'    Environment = "{name}"',
        "  }",
        "}",
    ]
    return "\n".join(lines) + "\n"


def scaffold(
    root: Path, name: str, *, region: str, ami: str | None, runner: Runner
) -> ScaffoldResult:
    """Write `remote-instances/<name>/terragrunt.hcl` for a new instance.

    Refuses anything that would make the file a lie: a name that fails
    `instances.validate_name`, a docker context name that would exceed
    `DOCKER_CONTEXT_NAME_LIMIT`, or a directory that already exists. The
    default AMI is Canonical's current Ubuntu 24.04 arm64 image, resolved
    through SSM via `runner` unless the caller passes `ami` explicitly.
    Never runs Terragrunt: applying the file is `make instance-deploy`'s
    job, and the returned guidance says so.

    Raises:
        ScaffoldError: the name is invalid, the context name would be too
            long, the directory already exists, or the default AMI could
            not be resolved (naming the manual AMI= override).
    """
    try:
        instances.validate_name(name)
    except instances.InvalidInstanceNameError as exc:
        raise ScaffoldError(_invalid_name_message(name)) from exc
    context = instances.docker_context(root, name)
    if len(context) > DOCKER_CONTEXT_NAME_LIMIT:
        raise ScaffoldError(_context_length_message(context))
    directory = instances.terragrunt_dir(root, name)
    if directory.exists():
        raise ScaffoldError(_exists_message(directory, name))

    resolved_ami = ami if ami is not None else _default_ami(runner, region)
    vpc_cidr, subnet_cidr = _allocate_cidrs(root)
    hcl_path = directory / "terragrunt.hcl"
    directory.mkdir(parents=True)
    hcl_path.write_text(
        _render_terragrunt_hcl(
            name, region, resolved_ami, vpc_cidr, subnet_cidr, ami_from_ssm=ami is None
        ),
        encoding="utf-8",
    )
    messages = (
        f"created {hcl_path}",
        "Commonly edited inputs: instance_type, root_volume_size_gb, "
        "data_volume_size_gb, availability_zone, tags and ami.",
        f"Deploy with: make instance-deploy INSTANCE={name}",
    )
    return ScaffoldResult(
        path=hcl_path,
        vpc_cidr=vpc_cidr,
        ami=resolved_ami,
        messages=messages,
    )


# ---------------------------------------------------------------------------
# State.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InstanceState:
    """What one instance looks like right now, per probe.

    A `None` probe field means the probe could not answer (its reason is in
    `lookup_error`) or does not apply (no recorded id, no certificate file).
    `lookup_error` is a sanitized one-line reason for the first probe that
    failed, or None when every probe answered. `state` never raises for an
    aws/docker failure: a listing of many instances degrades per row.
    """

    name: str
    directory: bool
    recorded_id: str | None
    ec2_state: str | None
    params_present: bool | None
    certs_present: bool | None
    client_cert_days_left: int | None
    context_exists: bool | None
    forward_port: int | None
    lookup_error: str | None


class _ProbeFailure(Exception):
    """Internal: one aws/docker probe in `state` failed.

    Carries a sanitized one-line reason. Never escapes `state`/`list_state`:
    the caller folds the first failure into `InstanceState.lookup_error`.
    """


def _one_line(text: str) -> str:
    """`text` collapsed onto one line, so a probe reason stays sanitized.

    Probe failures carry command output that can be arbitrarily multi-line;
    `lookup_error` renders one line per row.
    """
    return " ".join(text.split())


def _probe_run(runner: Runner, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Run one probe argv, turning every failure mode into `_ProbeFailure`."""
    try:
        result = runner(argv, None)
    except OSError as exc:
        raise _ProbeFailure(_one_line(str(exc))) from exc
    if result.returncode != 0:
        raise _ProbeFailure(
            f"{' '.join(argv)} exited {result.returncode}: {_one_line(result.stderr)}"
        )
    return result


def _ec2_state(runner: Runner, instance_id: str) -> str | None:
    """The instance's `State.Name`, or None when AWS answers nothing usable."""
    argv = [
        AWS_EXECUTABLE,
        "ec2",
        "describe-instances",
        "--instance-ids",
        instance_id,
        "--query",
        _EC2_STATE_QUERY,
        "--output",
        "text",
    ]
    observed = _probe_run(runner, argv).stdout.strip()
    if observed in ("", "None"):
        return None
    return observed


def _parameters_present(runner: Runner, name: str) -> bool:
    """Whether any SSM parameter exists under `name`'s prefix."""
    argv = [
        AWS_EXECUTABLE,
        "ssm",
        "describe-parameters",
        "--parameter-filters",
        f"Key=Name,Option=BeginsWith,Values={instances.parameter_prefix(name)}",
        "--output",
        "json",
    ]
    stdout = _probe_run(runner, argv).stdout
    try:
        payload: object = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise _ProbeFailure("describe-parameters printed output that is not valid JSON") from exc
    parameters = payload.get("Parameters") if isinstance(payload, dict) else None
    if not isinstance(parameters, list):
        raise _ProbeFailure("describe-parameters response carries no 'Parameters' list")
    return bool(parameters)


def _command_adapter(runner: Runner) -> CommandRunner:
    """`runner` viewed as a `hostprobe.CommandRunner`.

    `instances.forwarded_port` reads the docker context endpoint through
    `hostprobe.docker_context_forwarded_port`, whose runner returns a
    `CommandResult`; this adapter forwards this module's `CompletedProcess`
    -shaped runner to it so both worlds share one command surface. The
    timeout argument is accepted for shape and unused: this module issues
    unbounded local docker calls, the same as everywhere else here.
    """

    def adapted(command: Sequence[str], timeout_seconds: float | None) -> CommandResult:
        result = runner(command, None)
        return CommandResult(
            exit_code=result.returncode, stdout=result.stdout, stderr=result.stderr
        )

    return adapted


def state(root: Path, name: str, *, runner: Runner) -> InstanceState:
    """Everything knowable about `name` right now, as one `InstanceState`.

    Every aws/docker probe failure is folded into `lookup_error` (the first
    failure wins; later probes still run) instead of raising, so one
    unreachable surface never blanks the whole report.

    Raises:
        StateError: `name` fails `instances.validate_name` -- a malformed
            name is a usage error, not a probe outcome.
    """
    try:
        instances.validate_name(name)
    except instances.InvalidInstanceNameError as exc:
        raise StateError(_invalid_name_message(name)) from exc

    lookup_error: str | None = None

    def record(exc: Exception) -> None:
        nonlocal lookup_error
        if lookup_error is None:
            lookup_error = _one_line(str(exc))

    directory = instances.terragrunt_dir(root, name).is_dir()
    instance_id = recorded_id(root, name)

    ec2_state: str | None = None
    if instance_id is not None:
        try:
            ec2_state = _ec2_state(runner, instance_id)
        except _ProbeFailure as exc:
            record(exc)

    params_present: bool | None = None
    try:
        params_present = _parameters_present(runner, name)
    except _ProbeFailure as exc:
        record(exc)

    client_cert_path = instances.certs_dir(name) / certs.CLIENT_CERT_FILENAME
    certs_present = client_cert_path.is_file()
    client_cert_days_left: int | None = None
    if certs_present:
        try:
            client_cert_days_left = certs.days_remaining(
                certs.not_after(client_cert_path), _utc_now()
            )
        except (certs.CertsError, OSError) as exc:
            record(exc)

    context_exists: bool | None = None
    forward_port: int | None = None
    context_name = instances.docker_context(root, name)
    try:
        inspected = runner([DOCKER_EXECUTABLE, "context", "inspect", context_name], None)
    except OSError as exc:
        record(exc)
    else:
        context_exists = inspected.returncode == 0
        if context_exists:
            try:
                forward_port = instances.forwarded_port(root, name, _command_adapter(runner))
            except (instances.InstancesError, OSError) as exc:
                record(exc)

    return InstanceState(
        name=name,
        directory=directory,
        recorded_id=instance_id,
        ec2_state=ec2_state,
        params_present=params_present,
        certs_present=certs_present,
        client_cert_days_left=client_cert_days_left,
        context_exists=context_exists,
        forward_port=forward_port,
        lookup_error=lookup_error,
    )


def list_state(root: Path, *, runner: Runner) -> tuple[InstanceState, ...]:
    """`InstanceState` for every configured instance, in `instances.discover` order.

    An empty `remote-instances/` yields an empty tuple, not an error.
    """
    return tuple(state(root, name, runner=runner) for name in instances.discover(root))


# ---------------------------------------------------------------------------
# Power operations.
# ---------------------------------------------------------------------------


def _validated(name: str, error_cls: type[InstanceOpsError]) -> None:
    """Validate `name`, wrapping a rejection in this module's `error_cls`."""
    try:
        instances.validate_name(name)
    except instances.InvalidInstanceNameError as exc:
        raise error_cls(_invalid_name_message(name)) from exc


def _require_instance_id(root: Path, name: str) -> str:
    """The recorded EC2 id `name`'s power operation acts on.

    Raises:
        PowerError: nothing is recorded -- the remedy names `make
            instance-link`, the operation that records one.
    """
    instance_id = recorded_id(root, name)
    if instance_id is None:
        raise PowerError(
            f"ERROR: no EC2 instance id is recorded for instance {name!r}\n"
            "Power operations act on the id recorded when the instance was "
            "provisioned, and this instance has none.\n"
            f"Link the provisioned instance first: make instance-link INSTANCE={name}"
        )
    return instance_id


def _run_power_aws(
    runner: Runner, operation_args: Sequence[str]
) -> subprocess.CompletedProcess[str]:
    """Run one aws command in the power path, raising `PowerError` on any failure."""
    argv = [AWS_EXECUTABLE, *operation_args]
    try:
        result = runner(argv, None)
    except OSError as exc:
        raise PowerError(
            f"ERROR: could not run {' '.join(argv)}\n"
            f"{_one_line(str(exc))}\n"
            "Install the AWS CLI v2 and resolve the session "
            "(aws sso login), then retry."
        ) from exc
    if result.returncode != 0:
        raise PowerError(
            f"ERROR: {' '.join(argv)} exited {result.returncode}\n"
            f"{_one_line(result.stderr)}\n"
            "Resolve the failure above, then retry the power operation."
        )
    return result


def _poll_ec2_state(
    runner: Runner,
    instance_id: str,
    region: str,
    *,
    target: str,
    sleep: Callable[[float], None],
) -> None:
    """Poll the instance's `State.Name` until it equals `target`.

    At most `POWER_POLL_LIMIT` describes, `POWER_POLL_SECONDS` apart (via
    the injected `sleep`). Raises `PowerError` naming the last observed
    state when the limit is exhausted, and on any describe failure.
    """
    describe = [
        "ec2",
        "describe-instances",
        "--instance-ids",
        instance_id,
        "--region",
        region,
        "--query",
        _EC2_STATE_QUERY,
        "--output",
        "text",
    ]
    observed = ""
    for _ in range(POWER_POLL_LIMIT):
        result = _run_power_aws(runner, describe)
        observed = result.stdout.strip()
        if observed == target:
            return
        sleep(POWER_POLL_SECONDS)
    raise PowerError(
        f"ERROR: instance {instance_id} did not reach {target!r} within "
        f"{POWER_POLL_LIMIT} polls at {POWER_POLL_SECONDS}s intervals\n"
        f"The last observed state was {observed!r}.\n"
        f"Check the instance directly: aws ec2 describe-instances "
        f"--instance-ids {instance_id} --region {region}"
    )


def _poll_ssm_online(
    runner: Runner, instance_id: str, region: str, sleep: Callable[[float], None]
) -> None:
    """Poll the instance's SSM agent ping status until it reports `Online`."""
    ping = [
        "ssm",
        "describe-instance-information",
        "--filters",
        f"Key=InstanceIds,Values={instance_id}",
        "--query",
        "InstanceInformationList[0].PingStatus",
        "--output",
        "text",
        "--region",
        region,
    ]
    observed = ""
    for _ in range(POWER_POLL_LIMIT):
        result = _run_power_aws(runner, ping)
        observed = result.stdout.strip()
        if observed == SSM_ONLINE_STATUS:
            return
        sleep(POWER_POLL_SECONDS)
    raise PowerError(
        f"ERROR: the SSM agent on {instance_id} did not report "
        f"{SSM_ONLINE_STATUS!r} within {POWER_POLL_LIMIT} polls at "
        f"{POWER_POLL_SECONDS}s intervals\n"
        f"The last observed ping status was {observed!r}.\n"
        f"Check the agent directly: aws ssm describe-instance-information "
        f"--filters Key=InstanceIds,Values={instance_id} --region {region}"
    )


def stop(
    root: Path,
    name: str,
    *,
    region: str,
    runner: Runner,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """Stop `name`'s instance and wait until EC2 reports it `stopped`.

    Idempotent: stopping an already-stopped instance succeeds, and the poll
    observes the target state on its first read, so no waiting happens.

    Raises:
        PowerError: `name` is invalid, no id is recorded (naming `make
            instance-link`), an aws call fails, or the instance never
            reached `stopped` within the polling limit.
    """
    _validated(name, PowerError)
    instance_id = _require_instance_id(root, name)
    _run_power_aws(
        runner,
        ["ec2", "stop-instances", "--instance-ids", instance_id, "--region", region],
    )
    _poll_ec2_state(runner, instance_id, region, target=STOPPED_STATE, sleep=sleep)
    return (
        f"Instance {name!r} ({instance_id}) is stopped.\n"
        f"Start it again with: make instance-start INSTANCE={name}"
    )


def start(
    root: Path,
    name: str,
    *,
    region: str,
    runner: Runner,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """Start `name`'s instance, then wait for its SSM agent to report `Online`.

    Raises:
        PowerError: `name` is invalid, no id is recorded (naming `make
            instance-link`), an aws call fails, or either poll timed out.
    """
    _validated(name, PowerError)
    instance_id = _require_instance_id(root, name)
    _run_power_aws(
        runner,
        ["ec2", "start-instances", "--instance-ids", instance_id, "--region", region],
    )
    _poll_ec2_state(runner, instance_id, region, target=RUNNING_STATE, sleep=sleep)
    _poll_ssm_online(runner, instance_id, region, sleep)
    return (
        f"Instance {name!r} ({instance_id}) is running and its SSM agent "
        f"reports {SSM_ONLINE_STATUS}.\n"
        f"If connecting later fails, reinstall the daemon's client material: "
        f"make cert-install INSTANCE={name}"
    )


# ---------------------------------------------------------------------------
# Cleanup.
# ---------------------------------------------------------------------------


def _parse_parameter_names(stdout: str) -> list[str]:
    """Every parameter `Name` in a describe-parameters response.

    Raises:
        CleanupError: the response is not valid JSON or carries no
            `Parameters` list -- a malformed store answer must not pass for
            "nothing to delete".
    """
    try:
        payload: object = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise CleanupError(
            "ERROR: describe-parameters printed output that is not valid JSON\n"
            "The parameters under the instance prefix cannot be enumerated "
            "safely from this response.\n"
            "Re-run the destroy and inspect the aws CLI output directly."
        ) from exc
    parameters = payload.get("Parameters") if isinstance(payload, dict) else None
    if not isinstance(parameters, list):
        raise CleanupError(
            "ERROR: describe-parameters response carries no 'Parameters' list\n"
            "The parameters under the instance prefix cannot be enumerated "
            "safely from this response.\n"
            "Re-run the destroy and inspect the aws CLI output directly."
        )
    return [entry["Name"] for entry in parameters if isinstance(entry, dict) and "Name" in entry]


# docker's own wording when `context inspect` names a context that does not
# exist: `no such context: <name>` (or `context <name> not found` on some
# versions). Any other nonzero inspect -- a stopped or unreachable daemon,
# say -- is a failure, not evidence the context is gone.
_CONTEXT_ABSENT_PATTERN = re.compile(
    r"no such context|context\s+\S+\s+not found",
    re.IGNORECASE,
)


def _context_absent(stderr: str) -> bool:
    """True when docker's inspect failure means the context does not exist."""
    return _CONTEXT_ABSENT_PATTERN.search(stderr) is not None


def cleanup(root: Path, name: str, *, region: str, runner: Runner) -> tuple[str, ...]:
    """Tear down everything `name` scattered outside its Terragrunt directory.

    Deletes every SSM parameter under the instance's prefix, removes the
    instance's docker context, and deletes its certificate-material
    directory -- `instances.certs_dir(name)` in full, the recorded
    `instance-id` file included. Every operation is attempted even after an
    earlier one failed, and all failures are raised together in ONE
    `CleanupError` at the end, so a docker failure never hides SSM
    deletions that still needed to happen.

    A nonzero `docker context inspect` counts as "the context is absent,
    nothing to remove" only when docker's own answer says so (`no such
    context`, or `context <name> not found`); any other failure -- a stopped
    or unreachable daemon, say -- is recorded as a failed operation, so
    cleanup never reports success while the context survives.

    The remote-state bucket is deliberately out of scope: the fleet shares
    one bucket derived in `remote-instances/root.hcl`, so its lifecycle is
    a Terragrunt/backend concern, never an instance's.

    Raises:
        CleanupError: `name` is invalid, or any aws/docker/filesystem
            operation failed (the message names every failed operation).
    """
    _validated(name, CleanupError)
    failures: list[str] = []
    messages: list[str] = []

    def attempt(operation: str, argv: Sequence[str]) -> subprocess.CompletedProcess[str] | None:
        """Run one cleanup command; record a failure and return None on any failure."""
        try:
            result = runner(list(argv), None)
        except OSError as exc:
            failures.append(f"{operation}: {_one_line(str(exc))}")
            return None
        if result.returncode != 0:
            failures.append(
                f"{operation}: {' '.join(argv)} exited {result.returncode}: "
                f"{_one_line(result.stderr)}"
            )
            return None
        return result

    prefix = instances.parameter_prefix(name)
    listed = attempt(
        f"list SSM parameters under {prefix}",
        [
            AWS_EXECUTABLE,
            "ssm",
            "describe-parameters",
            "--parameter-filters",
            f"Key=Name,Option=BeginsWith,Values={prefix}",
            "--output",
            "json",
            "--region",
            region,
        ],
    )
    if listed is not None:
        try:
            parameter_names = _parse_parameter_names(listed.stdout)
        except CleanupError as exc:
            failures.append(_one_line(str(exc)))
        else:
            for parameter_name in parameter_names:
                deleted = attempt(
                    f"delete SSM parameter {parameter_name}",
                    [
                        AWS_EXECUTABLE,
                        "ssm",
                        "delete-parameter",
                        "--name",
                        parameter_name,
                        "--region",
                        region,
                    ],
                )
                if deleted is not None:
                    messages.append(f"deleted parameter {parameter_name}")

    context = instances.docker_context(root, name)
    inspect_argv = [DOCKER_EXECUTABLE, "context", "inspect", context]
    try:
        inspected = runner(list(inspect_argv), None)
    except OSError as exc:
        failures.append(f"inspect docker context {context}: {_one_line(str(exc))}")
    else:
        if inspected.returncode == 0:
            removed = attempt(
                f"remove docker context {context}",
                [DOCKER_EXECUTABLE, "context", "rm", "-f", context],
            )
            if removed is not None:
                messages.append(f"removed docker context {context!r}")
        elif _context_absent(inspected.stderr):
            messages.append(f"docker context {context!r} is absent; nothing to remove")
        else:
            failures.append(
                f"inspect docker context {context}: {' '.join(inspect_argv)} "
                f"exited {inspected.returncode}: {_one_line(inspected.stderr)}"
            )

    cert_directory = instances.certs_dir(name)
    if cert_directory.is_dir():
        try:
            shutil.rmtree(cert_directory)
        except OSError as exc:
            failures.append(f"remove certificate directory {cert_directory}: {_one_line(str(exc))}")
        else:
            messages.append(
                f"removed certificate directory {cert_directory} "
                f"(including the recorded {INSTANCE_ID_FILENAME} file)"
            )
    else:
        messages.append(f"no certificate directory at {cert_directory}; nothing to remove")

    if failures:
        raise CleanupError(
            f"ERROR: cleanup of instance {name!r} did not complete\n"
            + "\n".join(f"- {failure}" for failure in failures)
            + "\n"
            "Resolve every failure listed above, then re-run the destroy for "
            f"this instance: make instance-destroy INSTANCE={name}"
        )
    return tuple(messages)
