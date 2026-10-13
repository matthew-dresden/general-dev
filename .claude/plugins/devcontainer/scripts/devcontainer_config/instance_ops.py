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
supplies one). Writing the directory also appends `remote-instances/<name>/`
to the repository `.gitignore`, so the scaffold stays invisible to git
status until a developer promotes it deliberately by deleting that line.
`scaffold_engine` is the SSH engine kind's counterpart: it writes only
`remote-instances/<name>/engine.json` (the manifest
`devcontainer_config.instances.validate_engine_values` validates, so the
write side can never produce a manifest the read side refuses), appends the
same `.gitignore` entry, and runs no aws/docker command at all. An SSH
engine has no AMI to resolve, no CIDRs to allocate and no deployment to
apply.

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
bucket is deliberately out of scope for cleanup: each instance's bucket is
derived in `remote-instances/root.hcl` from its own name, so destroying it
is a Terragrunt/backend concern, never an instance-lifecycle one.

`engine_remove` is the SSH engine kind's teardown counterpart: it refuses
anything that is not an ssh-kind instance, refuses while a project container
still exists on the engine's docker context, and only then stops the
engine's forward, removes its docker context (an absent one is the desired
state, never a failure), deletes its certificate-material directory (the
forward record and the log live there, so they die with it), and removes
`remote-instances/<name>`, the registration itself. Like `cleanup` it
attempts every removal even after an earlier one failed and raises the
failures together at the end.

`state` is kind-aware: an EC2 instance answers the probes it always answered,
while an SSH engine (no AWS, no Parameter Store, no certificates behind it
-- skips the EC2 describe and the describe-parameters probe entirely and
instead answers one reachability probe, a plain `ssh ... docker version`
through the engine's own manifest values. An ssh row's `ec2_state` and
`params_present` are None because the probes do not apply (the same
does-not-apply meaning None already carries), and `engine_reachable` carries
the reachability answer.

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

from devcontainer_config import certs, containers, forwards, instances, transport_azure
from devcontainer_config.hostprobe import (
    CommandResult,
    CommandRunner,
    HostProbeError,
    read_positive_seconds,
)

# The external commands this module asks a runner to invoke. Every aws
# argv below starts with AWS_EXECUTABLE; every docker argv with
# DOCKER_EXECUTABLE; every ssh reachability probe with SSH_EXECUTABLE; and
# every azure VM argv the azure arms below build starts with AZ_EXECUTABLE;
# nothing else names an executable.
AWS_EXECUTABLE = "aws"
DOCKER_EXECUTABLE = "docker"
SSH_EXECUTABLE = "ssh"
AZ_EXECUTABLE = "az"

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

# The default azure VM size the azure scaffold writes when the caller names
# none, the one sizing default the azure kind carries. Sourced by the cli's
# `--vm-size` default the same way the manifest defaults are, so a changed
# default moves everywhere at once.
DEFAULT_AZURE_VM_SIZE = "Standard_D2pls_v5"

# The region default the EC2 scaffold applies when the caller names none:
# the same default the make instance-* targets and the cli's other
# instance subcommands document. It drives only the scaffold-time AMI
# lookup and the written availability zone -- never where anything
# deploys -- and is deliberately `None`-able in `scaffold`'s signature, so
# "no region was given" is a state the azure refusal can see: the azure
# kind takes its region from its location, and a silently applied default
# would be a refused input made to look accepted.
DEFAULT_REGION = "us-east-1"

# Root and data volume size (GiB) a scaffold starts with; both are commonly
# edited inputs the generated file's guidance message points at.
DEFAULT_VOLUME_SIZE_GB = 30

# The docker context name length limit the scaffold must respect: the
# context name is `docker_context(root, name)` (the repo slug plus the
# instance name), and a name that would exceed this bound is refused at
# scaffold time, when choosing a shorter name is still cheap, rather
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
# The azure arms poll on the same cadence: their per-poll state reads go
# through `transport_azure`'s builders, and the stop poll below reuses these
# two constants for its bounded deallocation wait.
POWER_POLL_LIMIT = 60
POWER_POLL_SECONDS = 5
STOPPED_STATE = "stopped"
RUNNING_STATE = "running"
SSM_ONLINE_STATUS = "Online"

# The `--query` that turns one `aws ec2 describe-instances` call straight
# into the instance's `State.Name` text.
_EC2_STATE_QUERY = "Reservations[0].Instances[0].State.Name"

# The two engine-address record keys the azure arms act on, and the
# `--query` that turns one `az vm show -d` call into the power and
# provisioning pair the azure status row renders from.
_VM_NAME_KEY = "vm_name"
_RESOURCE_GROUP_KEY = "resource_group"
_AZURE_VM_SHOW_QUERY = "{power:powerState, provisioning:provisioningState}"

# The committed `.gitignore` rule every scaffold's per-instance entry is
# appended below: the blanket lock-file ignore marks the per-instance block,
# and each new scaffold's own directory entry lands under it, and under
# any earlier scaffold's entry, so the entries stay contiguous and the
# rest of the file is untouched. Derived from `instances.INSTANCES_DIR_NAME`
# so the anchor can never drift from the directory this module addresses.
GITIGNORE_SCAFFOLD_ANCHOR = f"{instances.INSTANCES_DIR_NAME}/*/.terraform.lock.hcl"


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


class EngineRemoveError(InstanceOpsError):
    """An SSH engine removal was refused, or one or more removals failed.

    The refusal cases (not an ssh engine, a project container still on the
    engine) raise this before anything is removed; the removal cases raise
    it after every removal was attempted, the message naming each failure.
    """


# The reachability probe's connection budget. An unreachable engine must not
# stall a fleet listing on the network's own timeout, and the budget is
# configurable (CLAUDE.md: no hard-coded timeout values) through the same
# positive-seconds reader every other timeout in this package uses.
ENGINE_PROBE_CONNECT_TIMEOUT_ENV_VAR = "ENGINE_PROBE_CONNECT_TIMEOUT_SECONDS"
ENGINE_PROBE_CONNECT_TIMEOUT_DEFAULT_SECONDS = 5.0

# The `docker version --format` template the reachability probe runs on the
# engine: a daemon that answers it is reachable, and its answer proves the
# forwarded docker path, not merely the ssh login path.
_ENGINE_REACH_DOCKER_FORMAT = "{{.Server.Version}}"


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
    into the file -- the azure kind writes the same allocation under its
    `vnet_cidr` input); `ami` is the AMI the file pins (the caller's
    override, or the one resolved from SSM), and None for the azure kind,
    which pins no image at scaffold time (the does-not-apply meaning None
    already carries elsewhere in this package); `messages` is the guidance
    a caller prints verbatim.
    """

    path: Path
    vpc_cidr: str
    ami: str | None
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
        "hyphens and underscores only; it keys a Terragrunt directory, a "
        "docker context and a Parameter Store prefix at once, and instance "
        "names are project names (e.g. acme).\n"
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


def _unknown_cloud_message(cloud: str) -> str:
    return (
        f"ERROR: unknown cloud {cloud!r}\n"
        "The scaffold writes an ec2 or an azure deployment; nothing else "
        "exists yet.\n"
        "Retry with CLOUD=ec2 (the default) or CLOUD=azure."
    )


def _azure_location_missing_message() -> str:
    return (
        "ERROR: an azure scaffold requires a location\n"
        "The --location input is where the VM and its vnet deploy, and it "
        "has no default: a silently chosen location would deploy the engine "
        "somewhere the operator never asked for.\n"
        "Pass one: make instance-init INSTANCE=<name> CLOUD=azure LOCATION=<location>"
    )


def _azure_region_given_message() -> str:
    return (
        "ERROR: refusing an aws region on an azure scaffold\n"
        "The azure kind has no aws region: --region is an EC2-only input, "
        "and an azure deployment goes to the --location the scaffold's "
        "location input names, which no aws call reads.\n"
        "Pass that instead: make instance-init INSTANCE=<name> CLOUD=azure "
        "LOCATION=<location>"
    )


def _azure_ami_given_message() -> str:
    return (
        "ERROR: refusing an AMI on an azure scaffold\n"
        "The azure kind pins no machine image at scaffold time: the image is "
        "fixed by the shared azure layer, not chosen per deployment.\n"
        "Retry without AMI=/--ami."
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
        "# project names (e.g. acme).",
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
        "  # 8 GiB ARM instance in us-east-1 (~$0.12/hr), the default for a real",
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


def _render_azure_terragrunt_hcl(
    name: str, location: str, vm_size: str, vpc_cidr: str, subnet_cidr: str
) -> str:
    """The per-instance terragrunt.hcl text for the azure kind.

    The same three parts as the EC2 template, honoring
    `remote-instances/README.md`'s conventions: the root include, the
    envcommon include (targeting `_envcommon/remote-azure.hcl`, the file
    `instances.read_kind` keys the azure kind on), and an `inputs` block
    carrying only what differs for this one deployment. The header keeps
    the EC2 template's conventions -- the directory name is the instance's
    identity, every input is the operator's to edit, and the deploy command
    is the follow-on step. The include targets land with the azure provider
    slice and are deliberately not validated here: scaffolding writes the
    declaration, it does not check the layer it includes.
    """
    lines = [
        f"# remote-instances/{name}/terragrunt.hcl",
        "#",
        "# The directory name IS this instance's identity, and instance names are",
        "# project names (e.g. acme).",
        "#",
        "# Everything in the inputs block below is yours to edit freely: location,",
        "# VM size, volume sizes and tags. Apply any edit with:",
        f"#   make instance-deploy INSTANCE={name}",
        "",
        'include "root" {',
        '  path = find_in_parent_folders("root.hcl")',
        "}",
        "",
        'include "envcommon" {',
        '  path = "${dirname(find_in_parent_folders("root.hcl"))}/_envcommon/remote-azure.hcl"',
        "}",
        "",
        "inputs = {",
        f'  instance_name = "{name}"',
        f'  name_prefix   = "{name}"',
        "",
        f'  location = "{location}"',
        f'  vm_size  = "{vm_size}"',
        "",
        f"  root_volume_size_gb = {DEFAULT_VOLUME_SIZE_GB}",
        f"  data_volume_size_gb = {DEFAULT_VOLUME_SIZE_GB}",
        "",
        f'  vnet_cidr   = "{vpc_cidr}"',
        f'  subnet_cidr = "{subnet_cidr}"',
        "",
        "  tags = {",
        f'    Environment = "{name}"',
        "  }",
        "}",
    ]
    return "\n".join(lines) + "\n"


def _append_scaffold_ignore(root: Path, name: str) -> Path:
    """Hide `remote-instances/<name>/` from git by appending it to `.gitignore`.

    The entry is appended in the same operation that writes the scaffold
    directory, so the generated `terragrunt.hcl` -- and the
    `.terraform.lock.hcl` a first `terragrunt init` creates -- never show up
    in `git status` and cannot be committed by accident. Idempotent: an
    entry already present (a scaffold re-created after its directory was
    removed) is left alone rather than duplicated. The insert point is below
    the committed `.terraform.lock.hcl` blanket rule -- the per-instance
    block's anchor -- and below any earlier scaffold's entry, so the
    per-instance entries stay contiguous and the rest of the file is
    untouched. Promoting a scaffold into tracked scope is the developer's
    own deliberate edit: delete the appended line, then commit the
    directory.

    Returns:
        The `.gitignore` path the entry was (or already was) recorded in.

    Raises:
        ScaffoldError: `.gitignore` is missing, or it carries no anchor
            rule -- this checkout predates the per-instance scaffold block,
            and guessing an insert point would be a fallback, not a
            behavior. The message names what to update.
    """
    gitignore = root / ".gitignore"
    entry = f"{instances.INSTANCES_DIR_NAME}/{name}/"
    try:
        text = gitignore.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ScaffoldError(
            f"ERROR: no .gitignore at {gitignore}\n"
            f"A scaffold must hide {entry} from git status in the same "
            "operation that writes it, and there is no .gitignore to append "
            "to.\n"
            "Restore this repository's committed .gitignore, then retry."
        ) from exc
    lines = text.splitlines()
    if any(line.strip() == entry for line in lines):
        return gitignore
    try:
        insert_at = lines.index(GITIGNORE_SCAFFOLD_ANCHOR) + 1
    except ValueError as exc:
        raise ScaffoldError(
            f"ERROR: {gitignore} carries no {GITIGNORE_SCAFFOLD_ANCHOR} rule\n"
            "Per-instance scaffold entries are appended below that committed "
            "blanket rule, and this checkout's .gitignore predates the "
            "per-instance scaffold block.\n"
            "Update the checkout's .gitignore to the current committed one, "
            "then retry."
        ) from exc
    per_instance_entry = re.compile(rf"^{re.escape(instances.INSTANCES_DIR_NAME)}/[^/]+/$")
    while insert_at < len(lines) and per_instance_entry.match(lines[insert_at]):
        insert_at += 1
    lines.insert(insert_at, entry)
    gitignore.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return gitignore


def scaffold(
    root: Path,
    name: str,
    *,
    cloud: str = instances.ENGINE_KIND_EC2,
    region: str | None = None,
    ami: str | None = None,
    location: str | None = None,
    vm_size: str = DEFAULT_AZURE_VM_SIZE,
    runner: Runner,
) -> ScaffoldResult:
    """Write `remote-instances/<name>/terragrunt.hcl` for a new instance.

    Refuses anything that would make the file a lie: a name that fails
    `instances.validate_name`, a docker context name that would exceed
    `DOCKER_CONTEXT_NAME_LIMIT`, or a directory that already exists. The
    `cloud` parameter selects the template: the EC2 kind the target has
    always scaffolded, or the azure kind whose envcommon include targets
    `_envcommon/remote-azure.hcl` (the include `instances.read_kind` keys
    the azure kind on; the file itself lands with the azure provider slice
    and is deliberately not validated here). The EC2 default AMI is
    Canonical's current Ubuntu 24.04 arm64 image, resolved through SSM via
    `runner` unless the caller passes `ami` explicitly; the azure kind pins
    no image and issues no command at all, so its refusals are the only
    azure-specific surface: `region`/`ami`, the EC2 inputs, are refused
    when given (the azure equivalents are the `location` input; there is no
    AMI input), and `location` is required. `region` is None when the
    caller named none, and the EC2 path applies `DEFAULT_REGION`; the
    azure path never reads it.
    Never runs Terragrunt: applying the file is `make instance-deploy`'s
    job, and the returned guidance says so. Writing the directory also
    appends `remote-instances/<name>/` to the repository `.gitignore`
    (`_append_scaffold_ignore`), so the scaffold never shows up in
    `git status`; the gitignore append runs before anything is created, so a
    checkout whose `.gitignore` predates the per-instance block is refused
    with nothing written.

    Raises:
        ScaffoldError: the name is invalid, the cloud is unknown, an azure
            input is missing or is an EC2-only one, the context name would
            be too long, the directory already exists, the default AMI
            could not be resolved (naming the manual AMI= override), or
            `.gitignore` is missing or lacks the per-instance scaffold
            block's anchor rule (naming what to update).
    """
    try:
        instances.validate_name(name)
    except instances.InvalidInstanceNameError as exc:
        raise ScaffoldError(_invalid_name_message(name)) from exc
    if cloud not in (instances.ENGINE_KIND_EC2, instances.ENGINE_KIND_AZURE):
        raise ScaffoldError(_unknown_cloud_message(cloud))
    context = instances.docker_context(root, name)
    if len(context) > DOCKER_CONTEXT_NAME_LIMIT:
        raise ScaffoldError(_context_length_message(context))
    directory = instances.terragrunt_dir(root, name)
    if directory.exists():
        raise ScaffoldError(_exists_message(directory, name))

    if cloud == instances.ENGINE_KIND_AZURE:
        if location is None:
            raise ScaffoldError(_azure_location_missing_message())
        if region is not None:
            raise ScaffoldError(_azure_region_given_message())
        if ami is not None:
            raise ScaffoldError(_azure_ami_given_message())
        vpc_cidr, subnet_cidr = _allocate_cidrs(root)
        _append_scaffold_ignore(root, name)
        hcl_path = directory / "terragrunt.hcl"
        directory.mkdir(parents=True)
        hcl_path.write_text(
            _render_azure_terragrunt_hcl(name, location, vm_size, vpc_cidr, subnet_cidr),
            encoding="utf-8",
        )
        return ScaffoldResult(
            path=hcl_path,
            vpc_cidr=vpc_cidr,
            ami=None,
            messages=(
                f"created {hcl_path}",
                "Commonly edited inputs: location, vm_size, root_volume_size_gb, "
                "data_volume_size_gb and tags.",
                f"Deploy with: make instance-deploy INSTANCE={name}",
            ),
        )

    resolved_region = region if region is not None else DEFAULT_REGION
    resolved_ami = ami if ami is not None else _default_ami(runner, resolved_region)
    vpc_cidr, subnet_cidr = _allocate_cidrs(root)
    _append_scaffold_ignore(root, name)
    hcl_path = directory / "terragrunt.hcl"
    directory.mkdir(parents=True)
    hcl_path.write_text(
        _render_terragrunt_hcl(
            name,
            resolved_region,
            resolved_ami,
            vpc_cidr,
            subnet_cidr,
            ami_from_ssm=ami is None,
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


@dataclass(frozen=True)
class EngineScaffoldResult:
    """What `scaffold_engine` wrote and decided, for the caller to render.

    The same `path`-plus-`messages` shape `ScaffoldResult` renders from,
    minus the EC2-only fields (`vpc_cidr`, `ami`) an SSH engine has no
    counterpart for: an SSH engine's declaration is one manifest with no
    network allocation and no image to pin, and inventing empty values for
    fields it does not have would make the result a lie.
    """

    path: Path
    messages: tuple[str, ...]


def _engine_exists_message(directory: Path, name: str) -> str:
    """The existing-directory refusal for an SSH engine scaffold.

    The same refusal `scaffold` makes, naming the manifest an edit would
    go to instead of the terragrunt.hcl an EC2 scaffold's message names.
    """
    return (
        f"ERROR: {directory} already exists\n"
        f"An engine directory is scaffolded once; scaffolding over it could "
        f"silently replace edits already made to {name!r}'s engine manifest.\n"
        f"Edit the existing {directory / instances.ENGINE_MANIFEST_FILENAME} "
        "instead, or remove the directory deliberately first if a fresh "
        "scaffold is truly intended."
    )


def scaffold_engine(
    root: Path,
    name: str,
    *,
    host: str,
    port: int = instances.DEFAULT_ENGINE_PORT,
    user: str,
    docker_addr: str = instances.DEFAULT_ENGINE_DOCKER_ADDR,
    runner: Runner,
) -> EngineScaffoldResult:
    """Write `remote-instances/<name>/engine.json` for a new SSH engine.

    The SSH engine kind's counterpart of `scaffold`, refusing everything
    `scaffold` refuses: a name that fails `instances.validate_name`, a
    docker context name that would exceed `DOCKER_CONTEXT_NAME_LIMIT`, a
    directory that already exists, plus anything
    `instances.validate_engine_values` refuses, so the manifest written is
    one the read side accepts verbatim. The defaults for `port` and
    `docker_addr` are this module's sourcing of `instances`' constants, so
    a changed default moves everywhere at once. Writing the directory
    appends the same `remote-instances/<name>/` `.gitignore` entry
    `scaffold` appends (reusing `_append_scaffold_ignore`), and runs before
    anything is created, so a checkout whose `.gitignore` predates the
    per-instance block is refused with nothing written. No aws or docker
    command is ever issued: an SSH engine has no AMI to resolve, no CIDRs
    to allocate, and no Terragrunt deployment to apply.

    `runner` is accepted for call-site uniformity with `scaffold` (the CLI
    wiring passes it everywhere) and is deliberately unused on this path.

    Returns:
        An `EngineScaffoldResult` whose `messages` a caller prints
        verbatim.

    Raises:
        ScaffoldError: the name is invalid, the context name would be too
            long, the directory already exists, a manifest field breaks
            its rule, or `.gitignore` is missing or lacks the
            per-instance scaffold block's anchor rule.
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
        raise ScaffoldError(_engine_exists_message(directory, name))
    try:
        values = instances.validate_engine_values(
            {
                "kind": instances.ENGINE_KIND_SSH,
                "host": host,
                "user": user,
                "port": port,
                "docker_addr": docker_addr,
            }
        )
    except instances.EngineManifestError as exc:
        raise ScaffoldError(str(exc)) from exc
    _append_scaffold_ignore(root, name)
    manifest_path = instances.engine_manifest_path(root, name)
    directory.mkdir(parents=True)
    manifest_path.write_text(json.dumps(values, indent=2) + "\n", encoding="utf-8")
    messages = (
        f"created {manifest_path}",
        f"engine endpoint {instances.engine_endpoint(root, name)}",
    )
    return EngineScaffoldResult(path=manifest_path, messages=messages)


# ---------------------------------------------------------------------------
# State.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InstanceState:
    """What one instance looks like right now, per probe.

    A `None` probe field means the probe could not answer (its reason is in
    `lookup_error`) or does not apply (no recorded id, no certificate file,
    or, for the EC2-only probes on an SSH engine, no EC2 surface behind
    the instance at all). `lookup_error` is a sanitized one-line reason for
    the first probe that failed, or None when every probe answered. `state`
    never raises for an aws/docker/az failure: a listing of many instances
    degrades per row.

    `engine_reachable` is the SSH engine kind's own probe (`True`/`False`)
    and None for every other kind, whose kind has no such probe.

    `azure_state` is the azure kind's power column (the `powerState` the
    instance view reports, e.g. `VM running`) and None for every other
    kind; an EC2 row answers in `ec2_state` exactly as it always did.
    """

    name: str
    directory: bool
    recorded_id: str | None
    ec2_state: str | None
    azure_state: str | None
    params_present: bool | None
    certs_present: bool | None
    client_cert_days_left: int | None
    context_exists: bool | None
    forward_port: int | None
    engine_reachable: bool | None
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


def _azure_record_targets(address: dict[str, object], name: str) -> tuple[str, str]:
    """The `(resource_group, vm_name)` pair the engine address record must carry.

    The same deploy-remedy discipline `relay_values` applies to the relay
    keys: a record present but missing a key this surface needs reads as the
    not-yet-deployed state it effectively is, and the probe failure says so.

    Raises:
        _ProbeFailure: either key is missing or is not a non-empty string.
    """
    values: list[str] = []
    for key in (_RESOURCE_GROUP_KEY, _VM_NAME_KEY):
        value = address.get(key)
        if not isinstance(value, str) or not value:
            raise _ProbeFailure(
                f"the engine address record for instance {name!r} carries no usable "
                f"{key!r}; redeploy to write it: make instance-deploy INSTANCE={name}"
            )
        values.append(value)
    return values[0], values[1]


def _azure_vm_state(runner: Runner, resource_group: str, vm_name: str) -> str | None:
    """The VM's `powerState` from one `az vm show -d` call, or None when unreported."""
    argv = [
        AZ_EXECUTABLE,
        "vm",
        "show",
        "-g",
        resource_group,
        "-n",
        vm_name,
        "-d",
        "--query",
        _AZURE_VM_SHOW_QUERY,
        "--output",
        "json",
    ]
    stdout = _probe_run(runner, argv).stdout
    try:
        payload: object = json.loads(stdout) if stdout.strip() else {}
    except json.JSONDecodeError as exc:
        raise _ProbeFailure("az vm show printed output that is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise _ProbeFailure("az vm show response is not a JSON object")
    power = payload.get("power")
    if power is None:
        return None
    return str(power)


def _azure_secrets_present(runner: Runner, vault_name: str) -> bool:
    """Whether every publishable TLS secret exists in `vault_name`.

    Three probes aggregated: the vault is asked for each secret the
    publication writes (`certs.AZURE_PUBLISHABLE`, the same set the publish
    path writes), every probe must answer, and one `SecretNotFound` answer
    means the material is not there.

    Raises:
        _ProbeFailure: a probe failed for any reason other than a missing
            secret.
    """
    for secret_name in certs.AZURE_PUBLISHABLE.values():
        argv = [
            AZ_EXECUTABLE,
            "keyvault",
            "secret",
            "show",
            "--vault-name",
            vault_name,
            "--name",
            secret_name,
            "--query",
            "id",
            "--output",
            "tsv",
        ]
        try:
            result = runner(argv, None)
        except OSError as exc:
            raise _ProbeFailure(_one_line(str(exc))) from exc
        if result.returncode != 0:
            if certs.SECRET_NOT_FOUND_MARKER in result.stderr:
                return False
            raise _ProbeFailure(
                f"{' '.join(argv)} exited {result.returncode}: {_one_line(result.stderr)}"
            )
    return True


def _engine_reach_argv(values: dict[str, object], timeout_seconds: float) -> list[str]:
    """The one-shot `ssh ... docker version` argv proving an engine is reachable.

    `BatchMode=yes` fails fast instead of prompting (a listing has no tty to
    answer one), `ConnectTimeout` bounds the network wait by the configured
    budget (`:g` formatting so the default renders as `5`, a form ssh
    accepts), and the remote command is a docker version call: the probe
    answers for the docker path an engine exists to serve, not merely for an
    ssh login. The values come from `instances.engine_manifest`, already
    validated, so no unvalidated value reaches an ssh argument vector.
    """
    return [
        SSH_EXECUTABLE,
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={timeout_seconds:g}",
        "-p",
        str(values["port"]),
        f"{values['user']}@{values['host']}",
        "docker",
        "version",
        "--format",
        _ENGINE_REACH_DOCKER_FORMAT,
    ]


def _engine_reachable(runner: Runner, values: dict[str, object]) -> tuple[bool | None, str | None]:
    """The SSH engine's reachability answer, and why the probe could not run.

    Returns `(True, None)` when the engine's docker answered, `(False, None)`
    when ssh ran and failed; an unreachable engine is an answer, not a
    probe error, and a listing reports it without turning the exit code --
    and `(None, reason)` when the probe could not run at all: ssh missing
    from PATH, or a timeout value the configuration reader refuses. The
    reason is folded into `lookup_error` by the caller, the same
    could-not-answer discipline every other probe failure follows.
    """
    try:
        timeout = read_positive_seconds(
            ENGINE_PROBE_CONNECT_TIMEOUT_ENV_VAR, ENGINE_PROBE_CONNECT_TIMEOUT_DEFAULT_SECONDS
        )
    except HostProbeError as exc:
        return None, _one_line(str(exc))
    argv = _engine_reach_argv(values, timeout)
    try:
        result = runner(argv, None)
    except OSError:
        # ssh itself could not be spawned (not on PATH, or unexecutable):
        # the engine could not be reached, which is the probe's answer.
        return False, None
    return result.returncode == 0, None


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


def _state_kind(root: Path, name: str, record: Callable[[object], None]) -> str:
    """The instance's engine kind, folding a corrupt manifest into the probe record.

    A manifest that exists but cannot be read or parsed is a probe failure,
    not a kind flip: the row keeps the ssh kind its manifest declares, so
    no EC2 probe ever runs against an engine that has no EC2 surface, and
    carries the reason in `lookup_error` through `record`.
    """
    try:
        return instances.read_kind(root, name)
    except instances.EngineManifestError as exc:
        record(exc)
        return instances.ENGINE_KIND_SSH


def state(root: Path, name: str, *, runner: Runner) -> InstanceState:
    """Everything knowable about `name` right now, as one `InstanceState`.

    Every aws/docker probe failure is folded into `lookup_error` (the first
    failure wins; later probes still run) instead of raising, so one
    unreachable surface never blanks the whole report.

    The probes are kind-aware: an SSH engine (`instances.ENGINE_KIND_SSH`)
    has no EC2 surface behind it, so the describe and describe-parameters
    probes do not run and their fields stay None (the does-not-apply
    meaning), and the engine reachability probe answers in their place. An
    azure engine answers its own kind's probes in the same does-not-apply
    style: the VM power state and the Key Vault TLS material (through the
    vault the engine address record names) replace the EC2 describe and the
    describe-parameters probe, the record's VM name stands in as the row's
    display id, and a missing record degrades the row with the deploy
    remedy instead of aborting the listing. An EC2 instance keeps exactly
    the probe set and order it always had.

    Raises:
        StateError: `name` fails `instances.validate_name` -- a malformed
            name is a usage error, not a probe outcome.
    """
    try:
        instances.validate_name(name)
    except instances.InvalidInstanceNameError as exc:
        raise StateError(_invalid_name_message(name)) from exc

    lookup_error: str | None = None

    def record(reason: object) -> None:
        """Fold the first probe failure's sanitized one-line reason in."""
        nonlocal lookup_error
        if lookup_error is None:
            lookup_error = _one_line(str(reason))

    directory = instances.terragrunt_dir(root, name).is_dir()
    instance_id = recorded_id(root, name)
    kind = _state_kind(root, name, record)

    ec2_state: str | None = None
    azure_state: str | None = None
    displayed_id: str | None = instance_id
    engine_reachable: bool | None = None
    params_present: bool | None = None
    if kind == instances.ENGINE_KIND_SSH:
        values: dict[str, object] | None = None
        try:
            values = instances.engine_manifest(root, name)
        except instances.EngineManifestError as exc:
            record(exc)
        if values is not None:
            engine_reachable, reach_reason = _engine_reachable(runner, values)
            if reach_reason is not None:
                record(reach_reason)
    elif kind == instances.ENGINE_KIND_AZURE:
        address: dict[str, object] | None = None
        try:
            address = instances.engine_address(root, name)
        except instances.InstancesError as exc:
            record(exc)
        if address is not None:
            try:
                resource_group, vm_name = _azure_record_targets(address, name)
            except _ProbeFailure as exc:
                record(exc)
            else:
                # The VM name is the azure row's display id: the kind has no
                # EC2 id, and the record the deploy wrote is the addressable
                # identity the power operations act on.
                displayed_id = vm_name
                try:
                    azure_state = _azure_vm_state(runner, resource_group, vm_name)
                except _ProbeFailure as exc:
                    record(exc)
                try:
                    vault_name = instances.engine_vault_name(root, name)
                except instances.InstancesError as exc:
                    record(exc)
                else:
                    try:
                        params_present = _azure_secrets_present(runner, vault_name)
                    except _ProbeFailure as exc:
                        record(exc)
    elif instance_id is not None:
        try:
            ec2_state = _ec2_state(runner, instance_id)
        except _ProbeFailure as exc:
            record(exc)

    if kind == instances.ENGINE_KIND_EC2:
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
        recorded_id=displayed_id,
        ec2_state=ec2_state,
        azure_state=azure_state,
        params_present=params_present,
        certs_present=certs_present,
        client_cert_days_left=client_cert_days_left,
        context_exists=context_exists,
        forward_port=forward_port,
        engine_reachable=engine_reachable,
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


# ---------------------------------------------------------------------------
# Power operations, azure arm.
# ---------------------------------------------------------------------------


def _power_kind(root: Path, name: str) -> str:
    """The instance's engine kind for the power dispatch, wrapped in `PowerError`.

    Raises:
        PowerError: the directory declares (however brokenly) an engine
            manifest that cannot be read -- a power operation cannot know
            which surface to act on.
    """
    try:
        return instances.read_kind(root, name)
    except instances.EngineManifestError as exc:
        raise PowerError(f"ERROR: {exc}") from exc


def _azure_power_targets(root: Path, name: str) -> tuple[str, str]:
    """The `(resource_group, vm_name)` pair the azure power operation acts on.

    Both come from the engine address record the deploy wrote
    (`instances.engine_address`, the single accessor) -- never a second,
    ad hoc read of the record's JSON.

    Raises:
        PowerError: the record is missing (its own message names the deploy
            that writes it) or carries no usable target pair.
    """
    try:
        address = instances.engine_address(root, name)
    except instances.InstancesError as exc:
        raise PowerError(f"ERROR: {exc}") from exc
    try:
        return _azure_record_targets(address, name)
    except _ProbeFailure as exc:
        raise PowerError(f"ERROR: {_one_line(str(exc))}") from exc


def _run_power_az(
    runner: Runner, operation_args: Sequence[str]
) -> subprocess.CompletedProcess[str]:
    """Run one az command in the power path, raising `PowerError` on any failure."""
    argv = [AZ_EXECUTABLE, *operation_args]
    try:
        result = runner(argv, None)
    except OSError as exc:
        raise PowerError(
            f"ERROR: could not run {' '.join(argv)}\n"
            f"{_one_line(str(exc))}\n"
            "Install the Azure CLI (brew install azure-cli) and sign in with "
            "'az login', then retry."
        ) from exc
    if result.returncode != 0:
        raise PowerError(
            f"ERROR: {' '.join(argv)} exited {result.returncode}\n"
            f"{_one_line(result.stderr)}\n"
            "Resolve the failure above, then retry the power operation."
        )
    return result


def _poll_azure_power(
    runner: Runner,
    resource_group: str,
    vm_name: str,
    *,
    target: str,
    sleep: Callable[[float], None],
) -> None:
    """Poll the VM's `PowerState/*` code until it equals `target`.

    At most `POWER_POLL_LIMIT` reads, `POWER_POLL_SECONDS` apart (via the
    injected `sleep`) -- the same cadence the EC2 poll keeps. The argv and
    the parse are `transport_azure`'s builders (the one home of the
    instance-view reads), so this loop and the readiness poll can never
    disagree about what the platform answered.

    Raises:
        PowerError: a state read failed, or the limit was exhausted naming
            the last observed state.
    """
    adapter = _command_adapter(runner)
    observed = ""
    for _ in range(POWER_POLL_LIMIT):
        try:
            observed = transport_azure.vm_power_state(resource_group, vm_name, adapter)
        except transport_azure.RelayTransportError as exc:
            raise PowerError(f"ERROR: {_one_line(str(exc))}") from exc
        if observed == target:
            return
        sleep(POWER_POLL_SECONDS)
    raise PowerError(
        f"ERROR: VM {vm_name} did not reach {target!r} within "
        f"{POWER_POLL_LIMIT} polls at {POWER_POLL_SECONDS}s intervals\n"
        f"The last observed state was {observed!r}.\n"
        f"Check the VM directly: az vm get-instance-view --resource-group "
        f"{resource_group} --name {vm_name}"
    )


def _azure_stop(root: Path, name: str, *, runner: Runner, sleep: Callable[[float], None]) -> str:
    """Deallocate `name`'s VM and wait until the platform reports it deallocated.

    Idempotent the same way the EC2 stop is: an already-deallocated VM
    answers `PowerState/deallocated` on the poll's first read.
    """
    resource_group, vm_name = _azure_power_targets(root, name)
    _run_power_az(runner, ["vm", "deallocate", "-g", resource_group, "-n", vm_name])
    _poll_azure_power(
        runner,
        resource_group,
        vm_name,
        target=transport_azure.POWER_DEALLOCATED_CODE,
        sleep=sleep,
    )
    return (
        f"Instance {name!r} ({vm_name}) is deallocated.\n"
        f"Start it again with: make instance-start INSTANCE={name}"
    )


def _azure_start(root: Path, name: str, *, runner: Runner, sleep: Callable[[float], None]) -> str:
    """Start `name`'s VM, then wait for its power state and VM agent readiness.

    The bounded readiness wait is `transport_azure.wait_agent_ready` -- the
    one home of the poll -- so the cli's `azure-wait-agent` entry point and
    this path drive the identical bounded poll, never two copies.
    """
    resource_group, vm_name = _azure_power_targets(root, name)
    _run_power_az(runner, ["vm", "start", "-g", resource_group, "-n", vm_name])
    adapter = _command_adapter(runner)
    try:
        transport_azure.wait_agent_ready(resource_group, vm_name, adapter, pace=sleep)
    except transport_azure.RelayTransportError as exc:
        raise PowerError(f"ERROR: {_one_line(str(exc))}") from exc
    return (
        f"Instance {name!r} ({vm_name}) is running and its VM agent reports "
        f"{transport_azure.AGENT_READY_DISPLAY_STATUS}.\n"
        f"If connecting later fails, reinstall the daemon's client material: "
        f"make cert-install INSTANCE={name}"
    )


def stop(
    root: Path,
    name: str,
    *,
    region: str,
    runner: Runner,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """Stop `name`'s instance and wait until its platform reports it down.

    Kind-aware: an azure instance's VM is deallocated and the poll waits
    for `PowerState/deallocated` (`region` is an EC2-only concept and is
    unused on that arm); an EC2 instance keeps exactly the flow it always
    had.

    Idempotent: stopping an already-stopped instance succeeds, and the poll
    observes the target state on its first read, so no waiting happens.

    Raises:
        PowerError: `name` is invalid, the kind's addressable identity is
            not recorded (naming `make instance-link` for EC2, the deploy
            that writes the record for azure), a platform call fails, or
            the instance never reached its target state within the polling
            limit.
    """
    _validated(name, PowerError)
    if _power_kind(root, name) == instances.ENGINE_KIND_AZURE:
        return _azure_stop(root, name, runner=runner, sleep=sleep)
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
    """Start `name`'s instance, then wait until its engine agent reports ready.

    Kind-aware: an azure VM is started and the shared readiness poll waits
    for `PowerState/running` plus the VM agent's `Ready` (the azure
    counterpart of the SSM-agent wait; `region` is unused on that arm); an
    EC2 instance keeps exactly the flow it always had.

    Raises:
        PowerError: `name` is invalid, the kind's addressable identity is
            not recorded, a platform call fails, or either wait timed out.
    """
    _validated(name, PowerError)
    if _power_kind(root, name) == instances.ENGINE_KIND_AZURE:
        return _azure_start(root, name, runner=runner, sleep=sleep)
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
# versions). Any other nonzero inspect, a stopped or unreachable daemon
# for instance, is a failure, not evidence the context is gone.
_CONTEXT_ABSENT_PATTERN = re.compile(
    r"no such context|context\s+not found|context\s+\"[^\"]+\"\s*:\s*context not found",
    re.IGNORECASE,
)


def _context_absent(stderr: str) -> bool:
    """True when docker's inspect failure means the context does not exist."""
    return _CONTEXT_ABSENT_PATTERN.search(stderr) is not None


# The shared shape of cleanup and engine_remove: both attempt every removal
# even after an earlier one failed, append each failure's sanitized reason to
# `failures` and each success report to `messages`, and raise their failures
# together at the end. The helpers below carry that discipline once, so the
# two teardown paths cannot drift apart.
#
# The one attempt helper: run one removal command, appending a sanitized
# reason to `failures` and returning None on any failure. The `runner`
# argument is the caller's Runner; `cleanup` and `engine_remove` hand their
# own in, so one helper serves both.
def _attempt(
    runner: Runner, failures: list[str], operation: str, argv: Sequence[str]
) -> subprocess.CompletedProcess[str] | None:
    """Run one removal command; record a failure and return None on any failure."""
    try:
        result = runner(list(argv), None)
    except OSError as exc:
        failures.append(f"{operation}: {_one_line(str(exc))}")
        return None
    if result.returncode != 0:
        failures.append(
            f"{operation}: {' '.join(argv)} exited {result.returncode}: {_one_line(result.stderr)}"
        )
        return None
    return result


def _remove_docker_context(
    root: Path, name: str, runner: Runner, *, failures: list[str], messages: list[str]
) -> None:
    """Remove the instance's docker context, tolerating an absent one.

    A nonzero `docker context inspect` counts as "the context is absent,
    nothing to remove" only when docker's own answer says so (`no such
    context`, or `context <name> not found`); any other failure (a stopped
    or unreachable daemon, say) is recorded as a failed operation, so a
    teardown never reports success while the context survives. Shared by
    `cleanup` and `engine_remove`, which refuse contexts in different ways
    but must remove the one they do remove identically.
    """
    context = instances.docker_context(root, name)
    inspect_argv = [DOCKER_EXECUTABLE, "context", "inspect", context]
    try:
        inspected = runner(list(inspect_argv), None)
    except OSError as exc:
        failures.append(f"inspect docker context {context}: {_one_line(str(exc))}")
        return
    if inspected.returncode == 0:
        removed = _attempt(
            runner,
            failures,
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


def _remove_certificate_directory(name: str, *, failures: list[str], messages: list[str]) -> None:
    """Delete the instance's certificate-material directory, tolerating absence.

    `instances.certs_dir(name)` in full: the client certificate, the recorded
    `instance-id` file and, for an SSH engine that ever opened a forward,
    the forward record and its log, which live in the same directory. Shared
    by `cleanup` and `engine_remove` for the same reason the context removal
    is.
    """
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


def cleanup(root: Path, name: str, *, region: str, runner: Runner) -> tuple[str, ...]:
    """Tear down everything `name` scattered outside its Terragrunt directory.

    Kind-aware: an EC2 instance's cleanup deletes every SSM parameter under
    the instance's prefix before the shared steps; an azure instance's has
    no SSM surface at all (its TLS material lives in its terraform-managed
    Key Vault and its configuration payload beside it), so the parameter
    step is skipped entirely and `region` goes unused on that arm. Both
    kinds then remove the instance's docker context and delete its
    certificate-material directory -- `instances.certs_dir(name)` in full,
    which for the azure kind carries the engine-address.json record too.
    Every operation is attempted even after an earlier one failed, and all
    failures are raised together in ONE `CleanupError` at the end, so a
    docker failure never hides deletions that still needed to happen.

    A nonzero `docker context inspect` counts as "the context is absent,
    nothing to remove" only when docker's own answer says so (`no such
    context`, or `context <name> not found`); any other failure (a stopped
    or unreachable daemon, say -- is recorded as a failed operation, so
    cleanup never reports success while the context survives.

    The remote-state bucket -- and the azure kind's storage account beside
    it -- are deliberately out of scope: each instance's store is derived
    in `remote-instances/root.hcl` from its own name, so its lifecycle is
    a Terragrunt/backend concern, never an instance's.

    Raises:
        CleanupError: `name` is invalid, or any aws/docker/filesystem
            operation failed (the message names every failed operation).
    """
    _validated(name, CleanupError)
    try:
        kind = instances.read_kind(root, name)
    except instances.EngineManifestError as exc:
        raise CleanupError(f"ERROR: {exc}") from exc
    failures: list[str] = []
    messages: list[str] = []

    if kind == instances.ENGINE_KIND_EC2:
        prefix = instances.parameter_prefix(name)
        listed = _attempt(
            runner,
            failures,
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
                    deleted = _attempt(
                        runner,
                        failures,
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

    _remove_docker_context(root, name, runner, failures=failures, messages=messages)
    _remove_certificate_directory(name, failures=failures, messages=messages)

    if failures:
        raise CleanupError(
            f"ERROR: cleanup of instance {name!r} did not complete\n"
            + "\n".join(f"- {failure}" for failure in failures)
            + "\n"
            "Resolve every failure listed above, then re-run the destroy for "
            f"this instance: make instance-destroy INSTANCE={name}"
        )
    return tuple(messages)


# The remedy every ssh-kind refusal names for an EC2-kind instance: the
# teardown that actually reaches it.
_EC2_ENGINE_REMOVAL_REMEDY = (
    "An EC2 instance is torn down by its own target, which also runs this "
    "cleanup's work: make instance-destroy INSTANCE={name}."
)


def _refuse_non_ssh_engine(kind: str, name: str) -> EngineRemoveError:
    """The refusal for `engine_remove` on an instance that is not an ssh engine."""
    return EngineRemoveError(
        f"ERROR: instance {name!r} is a {kind} engine; engine-remove only removes "
        f"{instances.ENGINE_KIND_SSH} engines\n" + _EC2_ENGINE_REMOVAL_REMEDY.format(name=name)
    )


def _project_container_refusal(name: str, container_names: Sequence[str]) -> EngineRemoveError:
    """The refusal for `engine_remove` while the engine still holds a container."""
    listed = ", ".join(container_names)
    return EngineRemoveError(
        f"ERROR: instance {name!r} still holds this project's container(s): {listed}\n"
        "Removing the engine would strand them with their volumes. Destroy them "
        "first; make clean targets this project's containers on one engine "
        "without touching the shared docker context:\n"
        f"make clean ENGINE={name}\n"
        "FORCE=1 additionally bypasses clean's unpushed-work guard.\n"
        "Then re-run: make engine-remove INSTANCE=" + name + " CONFIRM=remove"
    )


def _verify_engine_holds_no_container(root: Path, name: str, runner: Runner, context: str) -> None:
    """Refuse while any container carrying this project's label exists on `context`.

    The probe is the same filter `containers.list_containers` reports from,
    so the listing and this refusal can never disagree about what "this
    project's container" means. A probe that cannot answer is a refusal too,
    never a pass: removing an engine whose containers could not be counted
    would report success while stranding whatever was there.

    Raises:
        EngineRemoveError: the engine holds a project container, or its
            container state could not be read.
    """
    ps_argv = [
        DOCKER_EXECUTABLE,
        "--context",
        context,
        "ps",
        "-a",
        "--filter",
        containers.project_label(root),
        "--format",
        "{{.Names}}",
    ]
    try:
        result = runner(ps_argv, None)
    except OSError as exc:
        raise EngineRemoveError(
            f"ERROR: could not check the containers on engine {name!r} (context {context!r})\n"
            f"{_one_line(str(exc))}\n"
            "Removing the engine is refused while its containers cannot be counted. "
            "Make docker reachable and retry."
        ) from exc
    if result.returncode != 0:
        raise EngineRemoveError(
            f"ERROR: could not check the containers on engine {name!r} (context {context!r})\n"
            f"docker ps exited {result.returncode}: {_one_line(result.stderr)}\n"
            "The engine's forward may be down; open it with: make connect-start "
            f"INSTANCE={name}\n"
            "Then re-run: make engine-remove INSTANCE=" + name + " CONFIRM=remove"
        )
    container_names = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if container_names:
        raise _project_container_refusal(name, container_names)


def engine_remove(
    root: Path,
    name: str,
    *,
    runner: Runner,
    stop_forward: Callable[[str], str] | None = None,
) -> tuple[str, ...]:
    """Remove an SSH engine whole: forward, context, local state, registration.

    The SSH engine kind's teardown counterpart of `cleanup`, refusing
    anything it cannot remove truthfully: `name` must be an ssh-kind
    instance (an EC2 instance is refused, naming `make instance-destroy`),
    and no container carrying this project's label may still exist on the
    engine's docker context (the remedy is `make clean ENGINE=<name>`, whose
    own guards still apply). Only then, in order: the engine's forward is
    stopped (`forwards.stop_forward`, injected so a test supplies a fake);
    its docker context is removed, an absent one reported as the desired
    state, never a failure; its certificate-material directory is deleted --
    the forward record and log live there, so they die with it -- and
    `remote-instances/<name>`, the registration `engine-init` wrote, is
    removed. Every removal is attempted even after an earlier one failed,
    and all failures are raised together in ONE `EngineRemoveError` at the
    end.

    The remote-state bucket is deliberately out of scope, for the same
    reason `cleanup` excludes it: an SSH engine has no bucket at all, and an
    EC2 instance's is a Terragrunt/backend concern this function refuses to
    reach.

    Args:
        runner: the Runner every docker probe and removal is issued through.
        stop_forward: the forward stopper, `forwards.stop_forward` by
            default. Injected because stopping a daemon means signalling a
            process group; a test substitutes a recorder here the same way
            `stop`/`start` inject `sleep`.

    Raises:
        EngineRemoveError: `name` is invalid, `name` is not an ssh engine,
            a project container still exists on the engine (or its
            container state could not be read), or any removal failed (the
            message names every failed removal).
    """
    _validated(name, EngineRemoveError)
    try:
        kind = instances.read_kind(root, name)
    except instances.EngineManifestError as exc:
        raise EngineRemoveError(f"ERROR: {exc}") from exc
    if kind != instances.ENGINE_KIND_SSH:
        raise _refuse_non_ssh_engine(kind, name)

    context = instances.docker_context(root, name)
    _verify_engine_holds_no_container(root, name, runner, context)

    failures: list[str] = []
    messages: list[str] = []

    stopper = stop_forward if stop_forward is not None else forwards.stop_forward
    try:
        messages.append(stopper(name))
    except (forwards.ForwardError, OSError) as exc:
        failures.append(f"stop forward for {name!r}: {_one_line(str(exc))}")

    _remove_docker_context(root, name, runner, failures=failures, messages=messages)
    _remove_certificate_directory(name, failures=failures, messages=messages)

    directory = instances.terragrunt_dir(root, name)
    if directory.is_dir():
        try:
            shutil.rmtree(directory)
        except OSError as exc:
            failures.append(f"remove engine registration {directory}: {_one_line(str(exc))}")
        else:
            messages.append(f"removed engine registration {directory}")
    else:
        messages.append(f"no engine registration at {directory}; nothing to remove")

    if failures:
        raise EngineRemoveError(
            f"ERROR: engine removal for {name!r} did not complete\n"
            + "\n".join(f"- {failure}" for failure in failures)
            + "\n"
            "Resolve every failure listed above, then re-run: "
            f"make engine-remove INSTANCE={name} CONFIRM=remove"
        )
    return tuple(messages)
