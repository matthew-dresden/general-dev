"""The azure state backend's bootstrap behind the make Terragrunt targets.

Every Terragrunt-running make target inits the instance's backend first
(`tg_init` in the Makefile), and the azure kind delegates that decision to
this module: does the instance's state storage account exist yet? The answer
picks the form -- an account that exists (and its `tfstate` container) gets
nothing but a probe report, a missing one gets created: the account, then
the container. Deciding before init, from explicit probes, replaces
error-driven branching: no failure is ever caught and retried, and every
probe failure that is not "the account is missing" is raised with the
command and its raw output, so nothing is hidden.

The storage account's name is never duplicated here:
`remote-instances/root-azure.hcl` is the single declaration of both the
name template and the committed suffix, and this module parses that file
with the same interpolation grammar Terragrunt itself resolves
(`${local.NAME}` tokens), composing the name from the instance name
(sanitized), the location its scaffold pins, the ambient azure subscription
and the committed suffix. The sanitization exists because azure's own law
is narrower than an instance name: a storage account name is 3-24 lowercase
alphanumeric characters, globally unique, so the instance component is
lowercased, stripped to alphanumerics, capped, and made collision-proof by
a deterministic digest of the four composition components -- the digest,
never a random value, so a fresh clone composes the same name and finds the
account an earlier bootstrap provisioned. One storage account per instance
(mirroring the per-instance S3 bucket isolation) lives in one dedicated
`rg-<instance>-state` resource group, so the state store outlives the
instance deployment's own resource group cleanly and instance-destroy's
terragrunt destroy can never touch it -- the same standing-outside-the-
lifecycle property the S3 bucket has.

The azure root is a separate file (`root-azure.hcl`), not the s3 `root.hcl`
extended: the azurerm backend shares no shape with the s3 one, and the two
roots keep each cloud's backend byte-honest. This module parses only the
azure root; `state_bucket` parses only the s3 one.

Because the composed names cannot be recomputed in HCL (terragrunt's
configuration language has no sha256 to re-derive the digest), `ensure`
hands them to Terragrunt through one side-car file:
`remote-instances/<instance>/.state-store.json`, written atomically after
the group, account and container are ensured, carrying exactly the two
backend names the azure root's `remote_state` block reads. One naming
authority -- this module's composition -- and one dumb-but-honest handoff;
the root fails with a plain file-not-found when the side-car is absent,
which is the not-yet-bootstrapped state, and the remedy is the same
deploy/init flow that runs `ensure` first. `delete_store` removes the
side-car with the store, so a deleted instance's directory carries no
pointer to an account that no longer exists.

The subscription is resolved from the ambient azure login (`az account
show`), the same no-profile discipline `state_bucket` applies to the AWS
account: the subscription the name embeds and the subscription that creates
and writes the account can never diverge because they are the same lookup.
Every az call is issued through an injected `Runner` (the shape
`devcontainer_config.instance_ops` defines), so the unit suite runs
hermetically -- no azure, no network -- and no test patches this module.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

from devcontainer_config import instances, repo
from devcontainer_config.instance_ops import Runner, subprocess_runner

# The one external command this module asks a runner to invoke; every argv
# below starts with it.
AZ_EXECUTABLE = "az"

# The azure root file this module parses -- the single declaration of the
# storage account name template and the committed suffix. One constant, so
# the filename changes in exactly one place.
ROOT_AZURE_HCL_FILENAME = "root-azure.hcl"

# The side-car file `ensure` writes and the azure root's remote_state block
# reads: the two backend names (resource group, storage account) composed
# here, handed to Terragrunt as one small JSON object beside the instance's
# terragrunt.hcl. The per-instance scaffold's `.gitignore` entry already
# covers it.
STATE_STORE_SIDECAR_FILENAME = ".state-store.json"

# The `${local.NAME}` token grammar the azure root's template uses, the same
# interpolation Terragrunt resolves at run time.
_INTERPOLATION_TOKEN = re.compile(r"\$\{local\.([A-Za-z0-9_]+)\}")

# The two declarations this module reads out of the azure root: the name
# template and the committed suffix. Both are matched as full-line
# assignments so a commented-out or renamed declaration reads as missing
# rather than half-parsed.
_TEMPLATE_PATTERN = re.compile(r'^\s*state_storage_name_template\s*=\s*"([^"]*)"', re.MULTILINE)
_SUFFIX_PATTERN = re.compile(r'^\s*state_store_suffix\s*=\s*"([^"]*)"', re.MULTILINE)

# Azure's own law for a storage account name: lowercase alphanumerics only,
# between these lengths, globally unique. The template-derived fullmatch
# pattern below enforces the charset and the length once, at composition, so
# an illegal name is a composition failure here instead of an azure error at
# create time.
STORAGE_ACCOUNT_NAME_MIN = 3
STORAGE_ACCOUNT_NAME_MAX = 24
_STORAGE_ACCOUNT_NAME_PATTERN = re.compile(r"[a-z0-9]+")

# The instance component of the name: lowercased, stripped to alphanumerics,
# and capped at this many characters before the digest is appended. The cap
# is a ceiling, never a padding: short instance names stay short.
SANITIZED_INSTANCE_MAX = 15

# The digest that disambiguates sanitized instance components across
# instances, locations and subscriptions: the first eight hex characters of
# SHA-256 over the four composition components joined with '|'.
DIGEST_LENGTH = 8
_DIGEST_INPUT_SEPARATOR = "|"

# The blob container every instance's Terragrunt state lives in, inside its
# own storage account.
STATE_CONTAINER_NAME = "tfstate"

# The dedicated resource group every instance's state account lives in,
# outside the deployment resource group Terragrunt manages, so instance
# destroy never reaches it.
STATE_GROUP_PREFIX = "rg-"
STATE_GROUP_SUFFIX = "-state"

# Substrings the az CLI's own error output carries when the thing a probe
# names does not exist. Anything else a probe answers is raised with the
# command and its raw output, never interpreted as "missing".
_MISSING_ACCOUNT_MARKERS: tuple[str, ...] = ("ResourceNotFound", "(404)", "404")
_MISSING_GROUP_MARKERS: tuple[str, ...] = ("ResourceGroupNotFound", "(404)", "404")
_MISSING_CONTAINER_MARKERS: tuple[str, ...] = ("ContainerNotFound", "(404)", "404")

# The provisioning state a created account must answer with before the
# container step may run against it.
_ACCOUNT_SUCCEEDED_STATE = "Succeeded"


class StateStoreError(RuntimeError):
    """The store probe could not decide, or the name could not be composed.

    Raised for every outcome the caller must see rather than act on: an
    unreadable root-azure.hcl, a missing template or suffix declaration, a name
    template referencing an unsupplied component, an az call that failed
    for any reason other than a missing resource, or a subscription lookup
    that answered nothing usable. Every message names the offending file,
    command or component and carries the raw command output, so nothing is
    hidden.
    """


def declared_template(root_hcl_text: str) -> str:
    """The raw `state_storage_name_template` interpolation string in the azure root."""
    match = _TEMPLATE_PATTERN.search(root_hcl_text)
    if match is None:
        raise StateStoreError(
            "no state_storage_name_template declaration found in remote-instances/root-azure.hcl; "
            "the storage account name template is the declaration this module composes from"
        )
    return match.group(1)


def declared_suffix(root_hcl_text: str) -> str:
    """The committed `state_store_suffix` value.

    A missing or empty suffix is a real declaration gap, not a value to
    invent: a replacement suffix would silently point the composed name at a
    different account than the one an earlier bootstrap provisioned.
    """
    match = _SUFFIX_PATTERN.search(root_hcl_text)
    if match is None or not match.group(1):
        raise StateStoreError(
            "no committed state_store_suffix found in remote-instances/root-azure.hcl; a missing "
            "suffix means a fresh bootstrap would mint a new account instead of finding the "
            "existing one"
        )
    return match.group(1)


def sanitize_instance(instance_name: str) -> str:
    """`instance_name` lowercased and stripped to the alphanumerics azure allows."""
    return re.sub(r"[^a-z0-9]", "", instance_name.lower())


def state_digest(instance_name: str, location: str, subscription_id: str, suffix: str) -> str:
    """The deterministic 8-hex disambiguator over the four composition components.

    The raw instance name (not the sanitized one) is digested, so two names
    that sanitize to the same component still separate; the location and
    subscription separate the same name deployed twice; and the committed
    suffix is digested too, so a rotated suffix changes every name at once.
    """
    material = _DIGEST_INPUT_SEPARATOR.join((instance_name, location, subscription_id, suffix))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:DIGEST_LENGTH]


def compose_from_root_hcl(
    root_hcl_text: str, instance_name: str, location: str, subscription_id: str
) -> str:
    """The storage account name the azure root's template composes to for these components.

    Substitutes every `${local.NAME}` token in the declared template: the
    instance component (sanitized per azure's lowercase-alphanumeric law,
    with the deterministic digest appended) and the committed suffix. The
    digest makes the name collision-proof, so the sanitized component is
    only shortened when the template's own fixed characters leave fewer
    characters inside azure's 24-character law than the
    `SANITIZED_INSTANCE_MAX` cap allows -- the digest and the suffix are
    never shortened first. Raises naming the unresolved reference when the
    template names a component these values do not supply, and naming the
    template when its fixed parts alone cannot fit the law.

    Raises:
        StateStoreError: the azure root is missing a declaration, the template
            references an unknown component, or the composed name cannot be
            made charset- and length-legal.
    """
    # The template is read first: it is the primary declaration, and a file
    # missing both must report the missing template rather than the suffix.
    template = declared_template(root_hcl_text)
    suffix = declared_suffix(root_hcl_text)
    tokens = list(_INTERPOLATION_TOKEN.finditer(template))
    fixed_length = len(template) - sum(len(match.group(0)) for match in tokens)
    budget = STORAGE_ACCOUNT_NAME_MAX - fixed_length - len(suffix) - DIGEST_LENGTH
    if budget < 1:
        raise StateStoreError(
            f"remote-instances/root-azure.hcl's state_storage_name_template {template!r} carries "
            f"{fixed_length} fixed characters plus the {len(suffix)}-character suffix, leaving "
            f"no room for the instance component inside azure's "
            f"{STORAGE_ACCOUNT_NAME_MAX}-character name law.\n"
            "Shorten the template's fixed parts or the suffix in root-azure.hcl."
        )
    sanitized = sanitize_instance(instance_name)[: min(SANITIZED_INSTANCE_MAX, budget)]
    values = {
        "instance_name": sanitized + state_digest(instance_name, location, subscription_id, suffix),
        "state_store_suffix": suffix,
    }

    def _substitute(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in values:
            raise StateStoreError(
                f"remote-instances/root-azure.hcl's state_storage_name_template references "
                f"local.{name}, which is not one of the composed components "
                f"{sorted(values)}"
            )
        return values[name]

    name = _INTERPOLATION_TOKEN.sub(_substitute, template)
    if not (
        STORAGE_ACCOUNT_NAME_MIN <= len(name) <= STORAGE_ACCOUNT_NAME_MAX
        and _STORAGE_ACCOUNT_NAME_PATTERN.fullmatch(name)
    ):
        raise StateStoreError(
            f"the composed storage account name {name!r} breaks azure's law of "
            f"{STORAGE_ACCOUNT_NAME_MIN}-{STORAGE_ACCOUNT_NAME_MAX} lowercase alphanumeric "
            "characters.\n"
            "Fix the template or suffix in remote-instances/root-azure.hcl."
        )
    return name


def subscription_id(runner: Runner) -> str:
    """The ambient azure login's subscription id, the component the azure root digests.

    No subscription is named on the command: the CLI resolves the ambient
    login the way every other az call on this host does, so the subscription
    embedded in the account NAME and the subscription that creates and
    writes the account can never diverge -- the same discipline
    `state_bucket.account_id` documents for the AWS account.

    Raises:
        StateStoreError: the command could not run, exited non-zero, or
            answered nothing usable.
    """
    argv = [AZ_EXECUTABLE, "account", "show", "--query", "id", "--output", "tsv"]
    return _probe_text(runner, argv, "the subscription id lookup")


def _probe_text(runner: Runner, argv: Sequence[str], what: str) -> str:
    """Run one az command and return its stripped stdout, loudly on any failure."""
    try:
        result = runner(list(argv), None)
    except OSError as exc:
        raise StateStoreError(f"could not run {argv[0]} for {what}: {exc}") from exc
    value = result.stdout.strip()
    if result.returncode != 0 or not value or value == "None":
        raise StateStoreError(
            f"{what} failed: {' '.join(argv)} exited {result.returncode}\n"
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    return value


def root_azure_hcl_path(root: Path) -> Path:
    """The `remote-instances/root-azure.hcl` this repository's azure instances include."""
    return root / instances.INSTANCES_DIR_NAME / ROOT_AZURE_HCL_FILENAME


def _root_hcl_text(root: Path, why: str) -> str:
    """The azure root's text, raised as a `StateStoreError` when unreadable."""
    path = root_azure_hcl_path(root)
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise StateStoreError(f"could not read {path}: {why}") from exc


def _validated_instance(instance: str) -> None:
    """`instances.validate_name`, wrapped in this module's error class."""
    try:
        instances.validate_name(instance)
    except instances.InvalidInstanceNameError as exc:
        raise StateStoreError(
            f"invalid instance name {instance!r}: the state store composes the account "
            "name from the instance name, so it must be a valid one"
        ) from exc


def _instance_location(root: Path, instance: str) -> str:
    """`instances.azure_instance_location`, wrapped in this module's error class."""
    try:
        return instances.azure_instance_location(root, instance)
    except instances.InstancesError as exc:
        raise StateStoreError(f"ERROR: {exc}") from exc


def storage_account_name(root: Path, instance: str, runner: Runner) -> str:
    """The state storage account name the azure root composes for `instance`.

    Reads `remote-instances/root-azure.hcl` from the repository root,
    resolves the subscription through the ambient login, reads the location
    the instance's scaffold pins, and composes the name from the azure
    root's own template and suffix.

    Raises:
        StateStoreError: `instance` fails `instances.validate_name`,
            the azure root is missing or unreadable, its declarations are missing,
            the scaffold carries no location, or the subscription lookup
            failed.
    """
    _validated_instance(instance)
    root_hcl_text = _root_hcl_text(
        root, "the storage account name is composed from the template and suffix that file declares"
    )
    location = _instance_location(root, instance)
    return compose_from_root_hcl(root_hcl_text, instance, location, subscription_id(runner))


def state_resource_group(instance: str) -> str:
    """The dedicated `rg-<instance>-state` group the account is ensured in.

    Deliberately outside the deployment resource group Terragrunt manages,
    so the state store outlives the deployment's lifecycle and
    instance-destroy's terragrunt destroy can never touch it -- the same
    standing-outside-the-lifecycle property the per-instance S3 bucket has.

    Raises:
        StateStoreError: `instance` fails `instances.validate_name`.
    """
    _validated_instance(instance)
    return f"{STATE_GROUP_PREFIX}{sanitize_instance(instance)}{STATE_GROUP_SUFFIX}"


def _combined_output(result: subprocess.CompletedProcess[str]) -> str:
    """The probe's stderr and stdout joined, the way az reports its errors."""
    return f"{result.stderr.strip()}\n{result.stdout.strip()}".strip()


def _probe_result(
    runner: Runner, argv: Sequence[str], what: str
) -> subprocess.CompletedProcess[str]:
    """Run one probe argv, turning only a spawn failure into an error."""
    try:
        return runner(list(argv), None)
    except OSError as exc:
        raise StateStoreError(f"could not run {argv[0]} for {what}: {exc}") from exc


def _probe_account(runner: Runner, name: str) -> str:
    """`"succeeded"` when the account exists and is usable, `"missing"` when absent.

    One probe decides: `az storage account show --query provisioningState`
    answers `Succeeded` with exit 0 for a usable account, and az's own
    ResourceNotFound wording for a missing one. Any other answer -- a
    provisioning state that is not `Succeeded`, a 403 on an account this
    identity cannot see, an unreachable endpoint -- raises with the command
    and its raw output, so the caller never guesses and nothing is retried
    behind the failure.

    Raises:
        StateStoreError: the probe could not decide.
    """
    argv = [
        AZ_EXECUTABLE,
        "storage",
        "account",
        "show",
        "--name",
        name,
        "--query",
        "provisioningState",
        "--output",
        "tsv",
    ]
    result = _probe_result(runner, argv, f"the storage account probe for {name!r}")
    if result.returncode == 0:
        state = result.stdout.strip()
        if state == _ACCOUNT_SUCCEEDED_STATE:
            return "succeeded"
        raise StateStoreError(
            f"the storage account probe for {name!r} answered provisioningState "
            f"{state!r}, not {_ACCOUNT_SUCCEEDED_STATE!r}: {' '.join(argv)}\n"
            "The account is not confirmed usable, so no step after the probe runs; "
            "resolve the state above, then retry."
        )
    output = _combined_output(result)
    if any(marker in output for marker in _MISSING_ACCOUNT_MARKERS):
        return "missing"
    raise StateStoreError(
        f"the storage account probe for {name!r} failed: {' '.join(argv)} exited "
        f"{result.returncode}\n{output}\n"
        "The account is not confirmed missing, so no step after the probe runs; "
        "resolve the failure above, then retry."
    )


def _account_form(runner: Runner, name: str) -> str:
    """`"plain"` when the account exists, `"bootstrap"` when confirmed missing."""
    return "plain" if _probe_account(runner, name) == "succeeded" else "bootstrap"


def init_form(root: Path, instance: str, runner: Runner) -> str:
    """The form `tg_init`'s azure arm must run: `"plain"` or `"bootstrap"`.

    Mirrors `state_bucket.init_form`: the composed account is probed once,
    an existing-and-succeeded account answers `plain` (nothing to
    bootstrap), a confirmed-missing one answers `bootstrap` (ensure will
    create it), and every other probe outcome raises with the command and
    its raw output.

    Raises:
        StateStoreError: the name could not be composed, or the probe
            could not decide.
    """
    name = storage_account_name(root, instance, runner)
    return _account_form(runner, name)


def _ensure_resource_group(runner: Runner, group: str, location: str) -> str:
    """Create `group` when confirmed missing; report the existing one otherwise.

    Raises:
        StateStoreError: the probe answered something other than "exists"
            or az's own ResourceGroupNotFound wording, or the create failed.
    """
    show_argv = [AZ_EXECUTABLE, "group", "show", "--name", group, "--output", "none"]
    result = _probe_result(runner, show_argv, f"the resource group probe for {group!r}")
    if result.returncode == 0:
        return f"resource group {group} already exists"
    output = _combined_output(result)
    if not any(marker in output for marker in _MISSING_GROUP_MARKERS):
        raise StateStoreError(
            f"the resource group probe for {group!r} failed: {' '.join(show_argv)} "
            f"exited {result.returncode}\n{output}\n"
            "The group is not confirmed missing, so no create is attempted; resolve "
            "the failure above, then retry."
        )
    create_argv = [
        AZ_EXECUTABLE,
        "group",
        "create",
        "--name",
        group,
        "--location",
        location,
        "--output",
        "none",
    ]
    created = _probe_result(runner, create_argv, f"the resource group create for {group!r}")
    if created.returncode != 0:
        raise StateStoreError(
            f"creating resource group {group!r} failed: {' '.join(create_argv)} exited "
            f"{created.returncode}\n{_combined_output(created)}"
        )
    return f"created resource group {group} in {location}"


def _ensure_storage_account(runner: Runner, name: str, group: str, location: str) -> str:
    """Create the account when confirmed missing; report the existing one otherwise."""
    form = _account_form(runner, name)
    if form == "plain":
        return f"storage account {name} already exists"
    create_argv = [
        AZ_EXECUTABLE,
        "storage",
        "account",
        "create",
        "--name",
        name,
        "--resource-group",
        group,
        "--location",
        location,
        "--sku",
        "Standard_LRS",
        "--allow-blob-public-access",
        "false",
        "--output",
        "none",
    ]
    created = _probe_result(runner, create_argv, f"the storage account create for {name!r}")
    if created.returncode != 0:
        raise StateStoreError(
            f"creating storage account {name!r} failed: {' '.join(create_argv)} exited "
            f"{created.returncode}\n{_combined_output(created)}"
        )
    return f"created storage account {name} in {group}"


def _container_exists(runner: Runner, name: str) -> bool:
    """Whether the account's `tfstate` container exists, loudly on any other answer.

    Raises:
        StateStoreError: the probe failed for any reason other than az's
            own ContainerNotFound wording.
    """
    argv = [
        AZ_EXECUTABLE,
        "storage",
        "container",
        "show",
        "--account-name",
        name,
        "--name",
        STATE_CONTAINER_NAME,
        "--auth-mode",
        "login",
        "--output",
        "none",
    ]
    result = _probe_result(
        runner, argv, f"the {STATE_CONTAINER_NAME!r} container probe for {name!r}"
    )
    if result.returncode == 0:
        return True
    output = _combined_output(result)
    if any(marker in output for marker in _MISSING_CONTAINER_MARKERS):
        return False
    raise StateStoreError(
        f"the {STATE_CONTAINER_NAME!r} container probe for {name!r} failed: "
        f"{' '.join(argv)} exited {result.returncode}\n{output}\n"
        "The container is not confirmed missing, so no create is attempted; resolve "
        "the failure above, then retry."
    )


def _ensure_container(runner: Runner, name: str) -> str:
    """Create the `tfstate` container when confirmed missing (azuread auth, no keys)."""
    if _container_exists(runner, name):
        return f"blob container {STATE_CONTAINER_NAME} already exists"
    create_argv = [
        AZ_EXECUTABLE,
        "storage",
        "container",
        "create",
        "--account-name",
        name,
        "--name",
        STATE_CONTAINER_NAME,
        "--auth-mode",
        "login",
        "--output",
        "none",
    ]
    created = _probe_result(runner, create_argv, f"the container create for {name!r}")
    if created.returncode != 0:
        raise StateStoreError(
            f"creating the {STATE_CONTAINER_NAME!r} container in {name!r} failed: "
            f"{' '.join(create_argv)} exited {created.returncode}\n"
            f"{_combined_output(created)}"
        )
    return f"created blob container {STATE_CONTAINER_NAME} in {name}"


def sidecar_path(root: Path, instance: str) -> Path:
    """`remote-instances/<instance>/.state-store.json`, the Terragrunt handoff file.

    The azure root's `remote_state` block reads the two backend names from
    exactly this path (`${get_terragrunt_dir()}/.state-store.json`), so a
    rename here would strand every deployment's init until the root is
    updated in the same change. The instance's name is validated first,
    exactly like every other derivation that turns it into a path.

    Raises:
        StateStoreError: `instance` fails `instances.validate_name`.
    """
    _validated_instance(instance)
    return instances.terragrunt_dir(root, instance) / STATE_STORE_SIDECAR_FILENAME


def _write_sidecar(root: Path, instance: str, *, resource_group: str, storage_account: str) -> str:
    """Write the side-car atomically, and return the message saying so.

    The payload is the two backend names and nothing else -- the azure
    root's remote_state reads exactly these two keys. The write is the
    same temp-file-then-`os.replace` shape `instances.write_engine_address`
    uses, so a reader (the terragrunt run that follows this module in
    tg_init) can never observe a half-written file; a failed write removes
    its temp file before propagating.

    Raises:
        StateStoreError: the instance's directory does not exist, or the
            write failed for any other reason.
    """
    path = sidecar_path(root, instance)
    payload = {"resource_group": resource_group, "storage_account_name": storage_account}
    if not path.parent.is_dir():
        raise StateStoreError(
            f"ERROR: no instance directory at {path.parent}\n"
            "The side-car the azure root's remote_state reads is written beside the "
            "instance's terragrunt.hcl, which does not exist yet.\n"
            f"Scaffold it first: make instance-init INSTANCE={instance}"
        )
    descriptor, temp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise
    return f"wrote {path} (the backend names the azure root's remote_state reads)"


def _remove_sidecar(root: Path, instance: str) -> bool:
    """Remove the side-car when present; True exactly when a file was removed."""
    path = sidecar_path(root, instance)
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


def ensure(root: Path, instance: str, runner: Runner) -> tuple[str, ...]:
    """Provision `instance`'s state store: the group, the account, the container, the side-car.

    Every step probes before it creates (missing means create, existing
    means skip, any other answer raises), so the operation is idempotent:
    a second run reports what already exists and creates nothing. The
    container is created with `--auth-mode login`, so the caller's Entra
    identity authorizes every later state read and write and no account
    key is ever involved. The final step rewrites the side-car
    (`.state-store.json`) with the two backend names composed here --
    never appended-to, always replaced -- because it is the handoff the
    azure root's `remote_state` block reads, and a store that was ensured
    must leave the names it was ensured under exactly where Terragrunt
    will find them. Rewriting on every run is what keeps the file honest
    after a suffix or template change: the next ensure points terragrunt
    at the newly composed store.

    Returns:
        One message per step, in the order the steps ran.

    Raises:
        StateStoreError: the name or location could not be composed, any
            probe or create failed with its raw output, or the side-car
            could not be written.
    """
    _validated_instance(instance)
    name = storage_account_name(root, instance, runner)
    location = _instance_location(root, instance)
    group = state_resource_group(instance)
    messages = [
        _ensure_resource_group(runner, group, location),
        _ensure_storage_account(runner, name, group, location),
        _ensure_container(runner, name),
        _write_sidecar(root, instance, resource_group=group, storage_account=name),
    ]
    return tuple(messages)


def storage_name_pattern(root: Path) -> re.Pattern[str]:
    """The fullmatch pattern for this fleet's state account names, from the azure root.

    Composed from the committed template itself -- literal parts escaped,
    the instance component replaced by the sanitized-plus-digest character
    grammar, the suffix token replaced by the committed suffix -- so the
    pattern can never drift from the name the module composes: one template
    declares both.
    """
    text = _root_hcl_text(
        root, "the fleet pattern is composed from the template and suffix that file declares"
    )
    template = declared_template(text)
    suffix = declared_suffix(text)
    tokens = list(_INTERPOLATION_TOKEN.finditer(template))
    fixed_length = len(template) - sum(len(match.group(0)) for match in tokens)
    budget = STORAGE_ACCOUNT_NAME_MAX - fixed_length - len(suffix) - DIGEST_LENGTH
    if budget < 1:
        raise StateStoreError(
            f"remote-instances/root-azure.hcl's state_storage_name_template {template!r} cannot "
            f"fit azure's {STORAGE_ACCOUNT_NAME_MAX}-character name law; the fleet pattern "
            "is composed from it"
        )
    component_bound = min(SANITIZED_INSTANCE_MAX, budget)
    component_patterns = {
        "instance_name": rf"[a-z0-9]{{1,{component_bound}}}[0-9a-f]{{{DIGEST_LENGTH}}}",
    }
    pieces: list[str] = []
    position = 0
    for match in tokens:
        pieces.append(re.escape(template[position : match.start()]))
        name = match.group(1)
        if name == "state_store_suffix":
            pieces.append(re.escape(suffix))
        else:
            try:
                pieces.append(component_patterns[name])
            except KeyError as exc:
                raise StateStoreError(
                    f"remote-instances/root-azure.hcl's state_storage_name_template references "
                    f"local.{name}, which is not one of the composed components "
                    f"{sorted(component_patterns) + ['state_store_suffix']}"
                ) from exc
        position = match.end()
    pieces.append(re.escape(template[position:]))
    return re.compile("".join(pieces))


@dataclasses.dataclass(frozen=True)
class StoreListing:
    """One state storage account this fleet's template matches.

    A configured account's components come from its own composition; an
    orphaned account -- one no configured instance composes to -- carries no
    components at all, because a composed name's split is ambiguous in
    reverse: the components are known from the composition, never from a
    parse.
    """

    name: str
    configured: bool
    instance: str = ""
    location: str = ""
    resource_group: str = ""


def list_stores(root: Path, runner: Runner) -> tuple[StoreListing, ...]:
    """Every storage account in the subscription whose name this fleet's template matches.

    Reads the subscription's account list (the ambient login, the same login
    `subscription_id` resolves the digest component from), keeps the names
    that fullmatch the template-derived pattern -- the committed suffix is
    part of it, so another tenant's state accounts are never listed -- and
    marks which azure instances are configured in this checkout. Sorted
    stable: configured rows in `instances.discover` order, then orphans by
    name, so the output is stable.

    Raises:
        StateStoreError: the azure root is missing its declarations, an azure
            instance's scaffold carries no location, or the az call failed
            or answered unusable JSON.
    """
    pattern = storage_name_pattern(root)
    subscription = subscription_id(runner)
    argv = [
        AZ_EXECUTABLE,
        "storage",
        "account",
        "list",
        "--query",
        "[].{name:name, location:location, rg:resourceGroup}",
        "--output",
        "json",
    ]
    result = _probe_result(runner, argv, "the storage account listing")
    if result.returncode != 0:
        raise StateStoreError(
            f"listing storage accounts failed: {' '.join(argv)} exited {result.returncode}\n"
            f"{_combined_output(result)}"
        )
    try:
        payload: object = json.loads(result.stdout) if result.stdout.strip() else []
    except json.JSONDecodeError as exc:
        raise StateStoreError(
            "storage account list printed output that is not valid JSON; the account "
            "list cannot be read safely from it"
        ) from exc
    if not isinstance(payload, list):
        raise StateStoreError(
            "storage account list response is not a JSON list; the account list "
            "cannot be read safely from it"
        )
    accounts: dict[str, tuple[str, str]] = {}
    for entry in payload:
        if not isinstance(entry, dict) or "name" not in entry:
            continue
        location = entry.get("location")
        resource_group = entry.get("rg")
        accounts[str(entry["name"])] = (
            str(location) if location is not None else "",
            str(resource_group) if resource_group is not None else "",
        )
    matched = {name for name in accounts if pattern.fullmatch(name)}
    root_hcl_text = _root_hcl_text(
        root, "the fleet pattern is composed from the template and suffix that file declares"
    )
    listings: list[StoreListing] = []
    claimed: set[str] = set()
    for instance in instances.discover(root):
        try:
            kind = instances.read_kind(root, instance)
        except instances.InstancesError as exc:
            raise StateStoreError(f"ERROR: {exc}") from exc
        if kind != instances.ENGINE_KIND_AZURE:
            continue
        location = _instance_location(root, instance)
        name = compose_from_root_hcl(root_hcl_text, instance, location, subscription)
        if name in matched:
            _account_location, resource_group = accounts[name]
            listings.append(
                StoreListing(
                    name=name,
                    configured=True,
                    instance=instance,
                    # The configured row reports the location the composition
                    # used -- the scaffold's -- not the account's current
                    # placement, which an out-of-band move would change.
                    location=location,
                    resource_group=resource_group,
                )
            )
            claimed.add(name)
    for name in sorted(matched - claimed):
        account_location, resource_group = accounts[name]
        listings.append(
            StoreListing(
                name=name,
                configured=False,
                location=account_location,
                resource_group=resource_group,
            )
        )
    return tuple(listings)


def _container_names(runner: Runner, name: str) -> list[str]:
    """Every blob container in `name`, loudly on any failure.

    Raises:
        StateStoreError: the listing failed or answered unusable output.
    """
    argv = [
        AZ_EXECUTABLE,
        "storage",
        "container",
        "list",
        "--account-name",
        name,
        "--auth-mode",
        "login",
        "--query",
        "[].name",
        "--output",
        "tsv",
    ]
    result = _probe_result(runner, argv, f"the container listing for {name!r}")
    if result.returncode != 0:
        raise StateStoreError(
            f"listing the containers of {name!r} failed: {' '.join(argv)} exited "
            f"{result.returncode}\n{_combined_output(result)}"
        )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _delete_container(runner: Runner, name: str, container: str) -> None:
    """Delete one blob container: outright when it works, purge-first otherwise.

    The container delete is preferred outright; when azure refuses it, the
    container's blobs are batch-deleted first and the delete is retried, so
    a delete that only fails because of retained blobs still converges.

    Raises:
        StateStoreError: the purge or either delete attempt failed, with
            the az output in the message.
    """
    delete_argv = [
        AZ_EXECUTABLE,
        "storage",
        "container",
        "delete",
        "--account-name",
        name,
        "--name",
        container,
        "--auth-mode",
        "login",
        "--output",
        "none",
    ]
    deleted = _probe_result(
        runner, delete_argv, f"the delete of container {container!r} in {name!r}"
    )
    if deleted.returncode == 0:
        return
    purge_argv = [
        AZ_EXECUTABLE,
        "storage",
        "blob",
        "delete-batch",
        "--account-name",
        name,
        "--container-name",
        container,
        "--auth-mode",
        "login",
        "--output",
        "none",
    ]
    purged = _probe_result(
        runner, purge_argv, f"the blob purge of container {container!r} in {name!r}"
    )
    if purged.returncode != 0:
        raise StateStoreError(
            f"purging {container!r} in {name!r} failed: {' '.join(purge_argv)} exited "
            f"{purged.returncode}\n{_combined_output(purged)}\n"
            f"The first delete attempt had answered: {_combined_output(deleted)}"
        )
    retried = _probe_result(
        runner, delete_argv, f"the retried delete of container {container!r} in {name!r}"
    )
    if retried.returncode != 0:
        raise StateStoreError(
            f"deleting {container!r} in {name!r} failed after its blobs were purged: "
            f"{' '.join(delete_argv)} exited {retried.returncode}\n"
            f"{_combined_output(retried)}"
        )


def delete_account(account: str, resource_group: str, runner: Runner) -> str:
    """Purge and delete one state account, then its empty state group.

    The account is probed first: a confirmed-missing account is already
    absent and nothing at all is touched, the same "already absent"
    handling `state_bucket.delete_bucket` gives a missing bucket. The
    resource group is deleted only after the account delete itself
    succeeded -- the group exists to hold that one account, so an account
    that is gone is the guard that makes the group empty.

    Raises:
        StateStoreError: an empty `resource_group`, or the probe, purge,
            account delete or group delete failed, with the az output in
            the message.
    """
    if not resource_group:
        raise StateStoreError(
            f"refusing to delete storage account {account!r} without a resource "
            "group\n"
            "The listing the delete was derived from carried no resource group, "
            "so the delete cannot be aimed.\n"
            "Re-run the listing; if it still names no group, inspect the account "
            f"with 'az storage account show --name {account}'."
        )
    if _probe_account(runner, account) == "missing":
        return f"{account} is already absent; nothing to delete"
    for container in _container_names(runner, account):
        _delete_container(runner, account, container)
    delete_argv = [
        AZ_EXECUTABLE,
        "storage",
        "account",
        "delete",
        "--name",
        account,
        "--resource-group",
        resource_group,
        "--yes",
        "--output",
        "none",
    ]
    deleted = _probe_result(runner, delete_argv, f"the delete of account {account!r}")
    if deleted.returncode != 0:
        raise StateStoreError(
            f"deleting storage account {account!r} failed: {' '.join(delete_argv)} "
            f"exited {deleted.returncode}\n{_combined_output(deleted)}"
        )
    messages = [f"deleted storage account {account} ({resource_group})"]
    group_argv = [AZ_EXECUTABLE, "group", "show", "--name", resource_group, "--output", "none"]
    group = _probe_result(runner, group_argv, f"the resource group probe for {resource_group!r}")
    if group.returncode == 0:
        group_delete_argv = [
            AZ_EXECUTABLE,
            "group",
            "delete",
            "--name",
            resource_group,
            "--yes",
            "--output",
            "none",
        ]
        group_deleted = _probe_result(
            runner,
            group_delete_argv,
            f"the delete of resource group {resource_group!r}",
        )
        if group_deleted.returncode != 0:
            raise StateStoreError(
                f"deleting the emptied resource group {resource_group!r} failed: "
                f"{' '.join(group_delete_argv)} exited {group_deleted.returncode}\n"
                f"{_combined_output(group_deleted)}"
            )
        messages.append(f"deleted the emptied resource group {resource_group}")
    return "; ".join(messages)


def delete_store(root: Path, instance: str, runner: Runner) -> str:
    """Delete the state storage account composed for `instance`, then its group.

    The side-car (`ensure`'s handoff file) is removed with the store: an
    account that is gone must not leave a pointer to itself beside a
    terragrunt.hcl the next init could follow.

    Returns:
        The delete message, with the side-car's removal appended when a
        side-car existed to remove. An absent side-car is the desired end
        state and is reported by neither message.

    Raises:
        StateStoreError: the instance name is invalid, the azure root is
            missing its declarations, or the delete failed.
    """
    _validated_instance(instance)
    account = storage_account_name(root, instance, runner)
    message = delete_account(account, state_resource_group(instance), runner)
    if _remove_sidecar(root, instance):
        message += f"; removed {sidecar_path(root, instance)}"
    return message


def delete_matching_stores(root: Path, runner: Runner) -> tuple[str, ...]:
    """Delete every account this fleet's template matches.

    Only names the template matches are touched -- another tenant's state
    is never a candidate, because the committed suffix is part of the
    pattern.

    Returns:
        One message per account (deleted or already absent), in listing
        order.
    """
    listings = list_stores(root, runner)
    return tuple(
        delete_account(listing.name, listing.resource_group, runner) for listing in listings
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run the selected subcommand; exit 0 only on a confirmed outcome.

    `init-form ROOT INSTANCE` prints `plain` or `bootstrap` for the
    Makefile's `tg_init` helper; `ensure ROOT INSTANCE` provisions the
    group, account and container idempotently, printing one message per
    step; `list` prints every fleet account with its instance, location
    and configured-here mark; `delete INSTANCE` purges and deletes one
    instance's account; `delete-all` does the same for every fleet account
    (the Makefile gates it behind `CONFIRM=delete`). Every failure prints
    its reason to stderr and exits 1.
    """
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments:
        print(_usage(), file=sys.stderr)
        return 2
    command, rest = arguments[0], arguments[1:]
    try:
        if command == "init-form":
            (root_text, instance) = _require_args(rest, 2, "init-form")
            print(init_form(Path(root_text), instance, subprocess_runner))
            return 0
        if command == "ensure":
            (root_text, instance) = _require_args(rest, 2, "ensure")
            for message in ensure(Path(root_text), instance, subprocess_runner):
                print(message)
            return 0
        if command == "list":
            _require_args(rest, 0, "list")
            root = repo.find_root(Path.cwd())
            listings = list_stores(root, subprocess_runner)
            if not listings:
                print("No state storage accounts match this fleet's naming template.")
                return 0
            for listing in listings:
                print(_render_listing(listing))
            return 0
        if command == "delete":
            (instance,) = _require_args(rest, 1, "delete")
            root = repo.find_root(Path.cwd())
            print(delete_store(root, instance, subprocess_runner))
            return 0
        if command == "delete-all":
            _require_args(rest, 0, "delete-all")
            root = repo.find_root(Path.cwd())
            messages = delete_matching_stores(root, subprocess_runner)
            if not messages:
                print("No state storage accounts matched this fleet's template.")
                return 0
            for message in messages:
                print(message)
            return 0
    except StateStoreError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(_usage(), file=sys.stderr)
    return 2


def _usage() -> str:
    return (
        "usage: devcontainer_config.state_store init-form ROOT INSTANCE | "
        "ensure ROOT INSTANCE | list | delete INSTANCE | delete-all"
    )


def _require_args(rest: Sequence[str], count: int, what: str) -> list[str]:
    if len(rest) != count:
        raise StateStoreError(f"{what} takes exactly {count} argument(s); got {len(rest)}")
    return list(rest)


def _render_listing(listing: StoreListing) -> str:
    """One aligned row: name, instance, location, resource group, configured mark."""
    mark = "configured" if listing.configured else "orphaned"
    return (
        f"{listing.name}  {listing.instance:<20} {listing.location:<16} "
        f"{listing.resource_group:<28} {mark}"
    )


if __name__ == "__main__":
    sys.exit(main())
