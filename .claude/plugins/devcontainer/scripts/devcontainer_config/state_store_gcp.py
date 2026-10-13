"""The gcp state backend's ensure behind the make Terragrunt targets.

The gcp twin of `devcontainer_config.state_store` (the azure bootstrap) and
`devcontainer_config.state_bucket` (the s3 one): every Terragrunt-running
make target inits the instance's backend first (`tg_init` in the Makefile),
and the gcp kind delegates that decision to this module -- does the
instance's state bucket exist yet? The answer picks the form: a bucket that
exists gets nothing but a probe report, a missing one gets created.
Terragrunt's gcs backend bootstraps nothing (it has no
`--backend-bootstrap` form), so `ensure` replaces bootstrap outright, the
same way the azure module does.

The bucket's name is never duplicated here: `remote-instances/root-gcp.hcl`
is the single declaration of both the name template and the committed
suffix, and this module parses that file with the same interpolation
grammar and the same full-line-assignment patterns the azure module parses
`root-azure.hcl` with (both grammars are imported from there rather than
restated, so the two roots cannot drift apart). The composition is
`gdstate-<sanitized-instance>-<8 hex digest>`: the instance component runs
through `instances.sanitize_gcp_name` -- the ONE sanitizer the gcp kind's
derived names go through, the same one that named the VM -- and the digest
is the first eight hex characters of SHA-256 over the four composition
components joined with `|` (instance, zone, project, suffix). The digest,
never a random value, so a fresh clone composes the same name and finds the
bucket an earlier ensure provisioned; the zone and project separate the
same instance name deployed twice; and the committed suffix is digested
too, so a rotated suffix changes every name at once. One bucket per
instance lives in the deployment's own project, standing outside the
instance lifecycle exactly as the s3 bucket and the azure account do:
instance-destroy's terragrunt destroy can never touch it.

Because the composed name cannot be recomputed in HCL (Terragrunt's
configuration language has no sha256 to re-derive the digest), `ensure`
hands it to Terragrunt through the same side-car file the azure kind uses
(`remote-instances/<instance>/.state-store.json`, one file per instance
directory, and an instance directory is exactly one kind), carrying the
bucket name the gcp root's remote_state block reads. The write is atomic,
and `delete_store` removes the side-car with the store.

The project is never guessed: the bucket is created in, probed in, and
listed from the project id the instance's engine address record carries
(`instances.gcp_project_id`, written by the deploy), and the location the
create call names is the region the same record carries
(`instances.gcp_region`) -- the bucket the bootstrap provisions and the
bucket Terragrunt's backend later writes state to can never disagree about
placement because both come from the one record. Every gcloud call is
issued through an injected `Runner` (the shape
`devcontainer_config.instance_ops` defines), so the unit suite runs
hermetically -- no gcloud, no network -- and no test patches this module.
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
from collections.abc import Callable, Sequence
from pathlib import Path

from devcontainer_config import instances, repo, state_store
from devcontainer_config.instance_ops import Runner, subprocess_runner
from devcontainer_config.state_store import _INTERPOLATION_TOKEN, _SUFFIX_PATTERN

# The one external command this module asks a runner to invoke; every argv
# below starts with it.
GCLOUD_EXECUTABLE = "gcloud"

# The gcp root file this module parses -- the single declaration of the
# bucket name template and the committed suffix. One constant, so the
# filename changes in exactly one place.
ROOT_GCP_HCL_FILENAME = "root-gcp.hcl"

# The `${local.NAME}` token grammar the gcp root's template uses, and the
# `state_store_suffix = "..."` full-line assignment it declares: both are
# the azure root parser's own patterns, imported rather than restated so
# the two roots' grammars cannot drift (the suffix local's name is the same
# on both roots by declaration).
_GCP_TEMPLATE_PATTERN = re.compile(r'^\s*state_bucket_template\s*=\s*"([^"]*)"', re.MULTILINE)

# GCS's own law for a bucket name: 3-63 characters of lowercase letters,
# digits, hyphens, underscores and dots, starting and ending with an
# alphanumeric. The composed name is validated against it once, at
# composition, so an illegal name is a composition failure here instead of
# a gcloud error at create time.
BUCKET_NAME_MIN = 3
BUCKET_NAME_MAX = 63
_BUCKET_NAME_PATTERN = re.compile(r"[a-z0-9][a-z0-9._-]{1,61}[a-z0-9]")

# The digest that disambiguates sanitized instance components across
# instances, zones and projects: the first eight hex characters of SHA-256
# over the four composition components joined with '|'.
DIGEST_LENGTH = 8
_DIGEST_INPUT_SEPARATOR = "|"

# Substrings gcloud's own error output carries when the bucket a probe
# names does not exist -- the live wording is
# `ERROR: (gcloud.storage.buckets.describe) gs://<name> not found: 404.`
# Anything else a probe answers is raised with the command and its raw
# output, never interpreted as "missing".
_MISSING_BUCKET_MARKERS: tuple[str, ...] = ("not found", "404")


class StateStoreGCPError(RuntimeError):
    """The bucket probe could not decide, or the name could not be composed.

    Raised for every outcome the caller must see rather than act on: an
    unreadable root-gcp.hcl, a missing template or suffix declaration, a
    template referencing an unsupplied component, an engine address record
    missing its zone, project or region, a gcloud call that failed for any
    reason other than a missing bucket, or a create that failed. Every
    message names the offending file, command or component and carries the
    raw command output, so nothing is hidden.
    """


def declared_template(root_hcl_text: str) -> str:
    """The raw `state_bucket_template` interpolation string in the gcp root."""
    match = _GCP_TEMPLATE_PATTERN.search(root_hcl_text)
    if match is None:
        raise StateStoreGCPError(
            f"no state_bucket_template declaration found in "
            f"remote-instances/{ROOT_GCP_HCL_FILENAME}; "
            "the bucket name template is the declaration this module composes from"
        )
    return match.group(1)


def declared_suffix(root_hcl_text: str) -> str:
    """The committed `state_store_suffix` value.

    A missing or empty suffix is a real declaration gap, not a value to
    invent: a replacement suffix would silently point the composed name at
    a different bucket than the one an earlier ensure provisioned.
    """
    match = _SUFFIX_PATTERN.search(root_hcl_text)
    if match is None or not match.group(1):
        raise StateStoreGCPError(
            f"no committed state_store_suffix found in "
            f"remote-instances/{ROOT_GCP_HCL_FILENAME}; a missing suffix means a "
            "fresh ensure would mint a new bucket instead of finding the existing one"
        )
    return match.group(1)


def state_digest(instance_name: str, zone: str, project_id: str, suffix: str) -> str:
    """The deterministic 8-hex disambiguator over the four composition components.

    The raw instance name (not the sanitized one) is digested, so two names
    that sanitize to the same component still separate; the zone and the
    project separate the same name deployed twice; and the committed suffix
    is digested too, so a rotated suffix changes every name at once.
    """
    material = _DIGEST_INPUT_SEPARATOR.join((instance_name, zone, project_id, suffix))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:DIGEST_LENGTH]


def compose_from_root_hcl(
    root_hcl_text: str, instance_name: str, zone: str, project_id: str
) -> str:
    """The bucket name the gcp root's template composes to for these components.

    Substitutes every `${local.NAME}` token in the declared template: the
    instance component is the sanitized instance name (the ONE sanitizer
    the gcp kind names its VM with) joined to the deterministic digest with
    a hyphen, the shape the naming contract fixes as
    `gdstate-<sanitized>-<8 hex>`. Raises naming the unresolved reference
    when the template names a component these values do not supply, and
    naming the template when the composed name cannot be made charset- and
    length-legal for GCS.

    Raises:
        StateStoreGCPError: the gcp root is missing a declaration, the
            template references an unknown component, the instance name has
            no legal sanitized form, or the composed name breaks GCS's
            naming law.
    """
    # The template is read first: it is the primary declaration, and a file
    # missing both must report the missing template rather than the suffix.
    template = declared_template(root_hcl_text)
    suffix = declared_suffix(root_hcl_text)
    try:
        sanitized = instances.sanitize_gcp_name(instance_name)
    except instances.InvalidGCPNameError as exc:
        raise StateStoreGCPError(f"ERROR: {exc}") from exc
    values = {
        "instance_name": f"{sanitized}-{state_digest(instance_name, zone, project_id, suffix)}",
        "state_store_suffix": suffix,
    }

    def _substitute(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in values:
            raise StateStoreGCPError(
                f"remote-instances/{ROOT_GCP_HCL_FILENAME}'s state_bucket_template "
                f"references local.{name}, which is not one of the composed "
                f"components {sorted(values)}"
            )
        return values[name]

    name = _INTERPOLATION_TOKEN.sub(_substitute, template)
    if not (
        BUCKET_NAME_MIN <= len(name) <= BUCKET_NAME_MAX and _BUCKET_NAME_PATTERN.fullmatch(name)
    ):
        raise StateStoreGCPError(
            f"the composed bucket name {name!r} breaks GCS's law of "
            f"{BUCKET_NAME_MIN}-{BUCKET_NAME_MAX} lowercase letters, digits, "
            "hyphens, underscores and dots, starting and ending "
            "alphanumerically.\n"
            f"Fix the template or suffix in remote-instances/{ROOT_GCP_HCL_FILENAME}."
        )
    return name


def root_gcp_hcl_path(root: Path) -> Path:
    """The `remote-instances/root-gcp.hcl` this repository's gcp instances include."""
    return root / instances.INSTANCES_DIR_NAME / ROOT_GCP_HCL_FILENAME


def _root_hcl_text(root: Path, why: str) -> str:
    """The gcp root's text, raised as a `StateStoreGCPError` when unreadable."""
    path = root_gcp_hcl_path(root)
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise StateStoreGCPError(f"could not read {path}: {why}") from exc


def _validated_instance(instance: str) -> None:
    """`instances.validate_name`, wrapped in this module's error class."""
    try:
        instances.validate_name(instance)
    except instances.InvalidInstanceNameError as exc:
        raise StateStoreGCPError(
            f"invalid instance name {instance!r}: the state store composes the bucket "
            "name from the instance name, so it must be a valid one"
        ) from exc


def _record_component(accessor: Callable[[Path, str], str], root: Path, instance: str) -> str:
    """One engine-address record value, wrapped in this module's error class.

    The accessor is passed as the function itself (never fetched by name at
    run time), so a rename on `instances` fails at import instead of
    degrading into an attribute error mid-run.
    """
    try:
        return accessor(root, instance)
    except instances.InstancesError as exc:
        raise StateStoreGCPError(f"ERROR: {exc}") from exc


def bucket_name(root: Path, instance: str) -> str:
    """The state bucket name the gcp root composes for `instance`.

    Reads `remote-instances/root-gcp.hcl` from the repository root, reads
    the zone and project id the instance's engine address record carries
    (both written by the deploy), and composes the name from the gcp root's
    own template and suffix.

    Raises:
        StateStoreGCPError: `instance` fails `instances.validate_name`,
            the gcp root is missing or unreadable, its declarations are
            missing, the instance name has no legal sanitized form, or the
            record carries no usable zone or project id.
    """
    _validated_instance(instance)
    root_hcl_text = _root_hcl_text(
        root, "the bucket name is composed from the template and suffix that file declares"
    )
    zone = _record_component(instances.gcp_zone, root, instance)
    project_id = _record_component(instances.gcp_project_id, root, instance)
    return compose_from_root_hcl(root_hcl_text, instance, zone, project_id)


def _combined_output(result: subprocess.CompletedProcess[str]) -> str:
    """The probe's stderr and stdout joined, the way gcloud reports its errors."""
    return f"{result.stderr.strip()}\n{result.stdout.strip()}".strip()


def _probe_result(
    runner: Runner, argv: Sequence[str], what: str
) -> subprocess.CompletedProcess[str]:
    """Run one probe argv, turning only a spawn failure into an error."""
    try:
        return runner(list(argv), None)
    except OSError as exc:
        raise StateStoreGCPError(f"could not run {argv[0]} for {what}: {exc}") from exc


def _probe_bucket(runner: Runner, name: str, project_id: str) -> str:
    """`"exists"` when the bucket is there, `"missing"` when confirmed absent.

    One probe decides: `gcloud storage buckets describe` answers exit 0 for
    an existing bucket, and gcloud's own `not found: 404` wording for a
    missing one. Any other answer -- a 403 on a bucket this identity cannot
    see, an unreachable endpoint -- raises with the command and its raw
    output, so the caller never guesses and nothing is retried behind the
    failure.

    Raises:
        StateStoreGCPError: the probe could not decide.
    """
    argv = [
        GCLOUD_EXECUTABLE,
        "storage",
        "buckets",
        "describe",
        f"gs://{name}",
        "--project",
        project_id,
    ]
    result = _probe_result(runner, argv, f"the bucket probe for {name!r}")
    if result.returncode == 0:
        return "exists"
    output = _combined_output(result)
    if any(marker in output for marker in _MISSING_BUCKET_MARKERS):
        return "missing"
    raise StateStoreGCPError(
        f"the bucket probe for {name!r} failed: {' '.join(argv)} exited "
        f"{result.returncode}\n{output}\n"
        "The bucket is not confirmed missing, so no step after the probe runs; "
        "resolve the failure above, then retry."
    )


def _bucket_form(runner: Runner, name: str, project_id: str) -> str:
    """`"plain"` when the bucket exists, `"bootstrap"` when confirmed missing."""
    return "plain" if _probe_bucket(runner, name, project_id) == "exists" else "bootstrap"


def init_form(root: Path, instance: str, runner: Runner) -> str:
    """The form `tg_init`'s gcp arm must run: `"plain"` or `"bootstrap"`.

    Mirrors `state_bucket.init_form` and `state_store.init_form`: the
    composed bucket is probed once, an existing bucket answers `plain`
    (nothing to ensure), a confirmed-missing one answers `bootstrap`
    (ensure will create it), and every other probe outcome raises with the
    command and its raw output. Terragrunt's gcs backend bootstraps
    nothing, so `bootstrap` names the ensure step the caller runs, never a
    `--backend-bootstrap` flag.

    Raises:
        StateStoreGCPError: the name could not be composed, or the probe
            could not decide.
    """
    _validated_instance(instance)
    name = bucket_name(root, instance)
    project_id = _record_component(instances.gcp_project_id, root, instance)
    return _bucket_form(runner, name, project_id)


def _create_bucket(runner: Runner, name: str, project_id: str, region: str) -> str:
    """Create the bucket when confirmed missing; report the existing one otherwise.

    The bucket is created with uniform bucket-level access, so access is
    controlled at the bucket's own IAM layer and no per-object ACL can
    weaken it -- the same least-surprise posture the azure account's
    disabled public access takes.

    Raises:
        StateStoreGCPError: the probe could not decide, or the create
            failed, with the gcloud output in the message.
    """
    form = _bucket_form(runner, name, project_id)
    if form == "plain":
        return f"bucket gs://{name} already exists"
    create_argv = [
        GCLOUD_EXECUTABLE,
        "storage",
        "buckets",
        "create",
        f"gs://{name}",
        "--project",
        project_id,
        "--location",
        region,
        "--uniform-bucket-level-access",
    ]
    created = _probe_result(runner, create_argv, f"the bucket create for {name!r}")
    if created.returncode != 0:
        raise StateStoreGCPError(
            f"creating bucket {name!r} failed: {' '.join(create_argv)} exited "
            f"{created.returncode}\n{_combined_output(created)}"
        )
    return f"created bucket gs://{name} in {region}"


def sidecar_path(root: Path, instance: str) -> Path:
    """`remote-instances/<instance>/.state-store.json`, the Terragrunt handoff file.

    The gcp root's `remote_state` block reads the bucket name from exactly
    this path -- the same per-instance file the azure root reads, one file
    per instance directory because a directory is exactly one kind. The
    instance's name is validated first, exactly like every other
    derivation that turns it into a path.

    Raises:
        StateStoreGCPError: `instance` fails `instances.validate_name`.
    """
    _validated_instance(instance)
    return state_store.sidecar_path(root, instance)


def _write_sidecar(root: Path, instance: str, *, bucket: str) -> str:
    """Write the side-car atomically, and return the message saying so.

    The payload is the bucket name and nothing else -- the gcp root's
    remote_state reads exactly this key. The write is the same
    temp-file-then-`os.replace` shape the azure module's own side-car write
    uses, so a reader (the terragrunt run that follows this module in
    tg_init) can never observe a half-written file; a failed write removes
    its temp file before propagating.

    Raises:
        StateStoreGCPError: the instance's directory does not exist, or the
            write failed for any other reason.
    """
    path = sidecar_path(root, instance)
    payload = {"bucket": bucket}
    if not path.parent.is_dir():
        raise StateStoreGCPError(
            f"ERROR: no instance directory at {path.parent}\n"
            "The side-car the gcp root's remote_state reads is written beside the "
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
    return f"wrote {path} (the bucket name the gcp root's remote_state reads)"


def _remove_sidecar(root: Path, instance: str) -> bool:
    """Remove the side-car when present; True exactly when a file was removed."""
    path = sidecar_path(root, instance)
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


def ensure(root: Path, instance: str, runner: Runner) -> tuple[str, ...]:
    """Provision `instance`'s state store: the bucket, then the side-car.

    Every step probes before it creates (missing means create, existing
    means skip, any other answer raises), so the operation is idempotent:
    a second run reports what already exists and creates nothing. The
    bucket is created in the project the record names, at the region the
    record names, with uniform bucket-level access. The final step
    rewrites the side-car (`.state-store.json`) with the bucket name
    composed here -- never appended-to, always replaced -- because it is
    the handoff the gcp root's `remote_state` block reads, and a store
    that was ensured must leave the name it was ensured under exactly
    where Terragrunt will find it.

    Returns:
        One message per step, in the order the steps ran.

    Raises:
        StateStoreGCPError: the name, zone, project or region could not be
            composed, any probe or create failed with its raw output, or
            the side-car could not be written.
    """
    _validated_instance(instance)
    name = bucket_name(root, instance)
    project_id = _record_component(instances.gcp_project_id, root, instance)
    region = _record_component(instances.gcp_region, root, instance)
    messages = [
        _create_bucket(runner, name, project_id, region),
        _write_sidecar(root, instance, bucket=name),
    ]
    return tuple(messages)


def bucket_name_pattern(root: Path) -> re.Pattern[str]:
    """The fullmatch pattern for this fleet's bucket names, from the gcp root.

    Composed from the committed template itself -- literal parts escaped,
    the instance component replaced by the sanitized-plus-digest character
    grammar -- so the pattern can never drift from the name the module
    composes: one template declares both.

    Raises:
        StateStoreGCPError: the gcp root is missing its declarations, or
            the template references an unknown component.
    """
    text = _root_hcl_text(
        root, "the fleet pattern is composed from the template and suffix that file declares"
    )
    template = declared_template(text)
    declared_suffix(text)
    tokens = list(_INTERPOLATION_TOKEN.finditer(template))
    component_patterns = {
        "instance_name": rf"[a-z][a-z0-9-]{{0,{instances.GCP_NAME_MAX_LENGTH - 1}}}"
        rf"-[0-9a-f]{{{DIGEST_LENGTH}}}",
    }
    pieces: list[str] = []
    position = 0
    for match in tokens:
        pieces.append(re.escape(template[position : match.start()]))
        name = match.group(1)
        try:
            pieces.append(component_patterns[name])
        except KeyError as exc:
            raise StateStoreGCPError(
                f"remote-instances/{ROOT_GCP_HCL_FILENAME}'s state_bucket_template "
                f"references local.{name}, which is not one of the composed "
                f"components {sorted(component_patterns)}"
            ) from exc
        position = match.end()
    pieces.append(re.escape(template[position:]))
    return re.compile("".join(pieces))


@dataclasses.dataclass(frozen=True)
class BucketListing:
    """One state bucket this fleet's template matches.

    A configured bucket's components come from its own composition; an
    orphaned bucket -- one no configured instance composes to -- carries no
    components at all, because a composed name's split is ambiguous in
    reverse: the components are known from the composition, never from a
    parse.
    """

    name: str
    configured: bool
    instance: str = ""
    project_id: str = ""


def list_buckets(root: Path, runner: Runner) -> tuple[BucketListing, ...]:
    """Every bucket in the configured instances' projects whose name this fleet's template matches.

    For each configured gcp instance's project (the record's project id),
    the project's bucket list is read once and the names that fullmatch
    the template-derived pattern are kept -- the committed suffix is inside
    the digest, so another fleet's buckets are never listed. Buckets a
    configured instance composes to are marked configured; the rest of the
    matches are orphans, sorted by name, so the output is stable. A
    project shared by several configured instances is listed once.

    Raises:
        StateStoreGCPError: the gcp root is missing its declarations, an
            instance's record carries no usable project id, or a gcloud
            call failed or answered unusable JSON.
    """
    pattern = bucket_name_pattern(root)
    projects: list[str] = []
    for instance in instances.discover(root):
        try:
            kind = instances.read_kind(root, instance)
        except instances.InstancesError as exc:
            raise StateStoreGCPError(f"ERROR: {exc}") from exc
        if kind != instances.ENGINE_KIND_GCP:
            continue
        project_id = _record_component(instances.gcp_project_id, root, instance)
        if project_id not in projects:
            projects.append(project_id)
    listed: dict[str, str] = {}
    for project_id in projects:
        argv = [
            GCLOUD_EXECUTABLE,
            "storage",
            "buckets",
            "list",
            "--project",
            project_id,
            "--format",
            "value(name)",
        ]
        result = _probe_result(runner, argv, f"the bucket listing for project {project_id!r}")
        if result.returncode != 0:
            raise StateStoreGCPError(
                f"listing the buckets of project {project_id!r} failed: "
                f"{' '.join(argv)} exited {result.returncode}\n{_combined_output(result)}"
            )
        for line in result.stdout.splitlines():
            # The listing answers the bucket id in gcloud's own address
            # forms (`gs://<name>`); trimming to the final path segment
            # normalizes whichever spelling this gcloud version prints
            # without guessing at either, since a bucket id itself never
            # carries a slash.
            name = line.strip().removeprefix("gs://").rsplit("/", 1)[-1]
            if name:
                listed[name] = project_id
    matched = {name for name in listed if pattern.fullmatch(name)}
    root_hcl_text = _root_hcl_text(
        root, "the fleet pattern is composed from the template and suffix that file declares"
    )
    listings: list[BucketListing] = []
    claimed: set[str] = set()
    for instance in instances.discover(root):
        try:
            kind = instances.read_kind(root, instance)
        except instances.InstancesError as exc:
            raise StateStoreGCPError(f"ERROR: {exc}") from exc
        if kind != instances.ENGINE_KIND_GCP:
            continue
        name = compose_from_root_hcl(
            root_hcl_text,
            instance,
            _record_component(instances.gcp_zone, root, instance),
            _record_component(instances.gcp_project_id, root, instance),
        )
        if name in matched:
            listings.append(
                BucketListing(
                    name=name, configured=True, instance=instance, project_id=listed[name]
                )
            )
            claimed.add(name)
    for name in sorted(matched - claimed):
        listings.append(BucketListing(name=name, configured=False, project_id=listed[name]))
    return tuple(listings)


def _purge_objects(runner: Runner, name: str, project_id: str) -> None:
    """Delete every object in `name`, tolerating an already-empty bucket.

    `buckets delete` refuses a non-empty bucket, so the purge runs first.
    gcloud answers `One or more URLs matched no objects` for an empty
    bucket's wildcard, which is the desired state and not a failure; any
    other purge failure raises with the command and its output.

    Raises:
        StateStoreGCPError: the purge failed for any reason other than
            there being nothing to purge.
    """
    purge_argv = [
        GCLOUD_EXECUTABLE,
        "storage",
        "rm",
        "--recursive",
        f"gs://{name}/*",
        "--project",
        project_id,
    ]
    purged = _probe_result(runner, purge_argv, f"the object purge of bucket {name!r}")
    if purged.returncode == 0:
        return
    output = _combined_output(purged)
    if "no objects" in output.lower() or "matched no objects" in output:
        return
    raise StateStoreGCPError(
        f"purging the objects of bucket {name!r} failed: {' '.join(purge_argv)} "
        f"exited {purged.returncode}\n{output}"
    )


def delete_bucket(name: str, project_id: str, runner: Runner) -> str:
    """Purge and delete one state bucket.

    The bucket is probed first: a confirmed-missing bucket is already
    absent and nothing at all is touched, the same "already absent"
    handling the azure module's `delete_account` gives a missing account.

    Raises:
        StateStoreGCPError: the probe, purge or delete failed, with the
            gcloud output in the message.
    """
    if _probe_bucket(runner, name, project_id) == "missing":
        return f"gs://{name} is already absent; nothing to delete"
    _purge_objects(runner, name, project_id)
    delete_argv = [
        GCLOUD_EXECUTABLE,
        "storage",
        "buckets",
        "delete",
        f"gs://{name}",
        "--project",
        project_id,
    ]
    deleted = _probe_result(runner, delete_argv, f"the delete of bucket {name!r}")
    if deleted.returncode != 0:
        raise StateStoreGCPError(
            f"deleting bucket {name!r} failed: {' '.join(delete_argv)} exited "
            f"{deleted.returncode}\n{_combined_output(deleted)}"
        )
    return f"deleted bucket gs://{name} (project {project_id})"


def delete_store(root: Path, instance: str, runner: Runner) -> str:
    """Delete the state bucket composed for `instance`.

    The side-car (`ensure`'s handoff file) is removed with the store: a
    bucket that is gone must not leave a pointer to itself beside a
    terragrunt.hcl the next init could follow.

    Returns:
        The delete message, with the side-car's removal appended when a
        side-car existed to remove. An absent side-car is the desired end
        state and is reported by neither message.

    Raises:
        StateStoreGCPError: the instance name is invalid, the gcp root is
            missing its declarations, the record carries no usable zone or
            project id, or the delete failed.
    """
    _validated_instance(instance)
    name = bucket_name(root, instance)
    project_id = _record_component(instances.gcp_project_id, root, instance)
    message = delete_bucket(name, project_id, runner)
    if _remove_sidecar(root, instance):
        message += f"; removed {sidecar_path(root, instance)}"
    return message


def delete_matching_buckets(root: Path, runner: Runner) -> tuple[str, ...]:
    """Delete every bucket this fleet's template matches.

    Only names the template matches are touched -- another fleet's state
    is never a candidate, because the committed suffix is inside the
    pattern's digest component.

    Returns:
        One message per bucket (deleted or already absent), in listing
        order.
    """
    listings = list_buckets(root, runner)
    return tuple(delete_bucket(listing.name, listing.project_id, runner) for listing in listings)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the selected subcommand; exit 0 only on a confirmed outcome.

    `init-form ROOT INSTANCE` prints `plain` or `bootstrap` for the
    Makefile's `tg_init` helper; `ensure ROOT INSTANCE` provisions the
    bucket idempotently, printing one message per step; `list` prints
    every fleet bucket with its instance, project and configured-here
    mark; `delete INSTANCE` purges and deletes one instance's bucket;
    `delete-all` does the same for every fleet bucket (the Makefile gates
    it behind `CONFIRM=delete`). Every failure prints its reason to stderr
    and exits 1.
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
            listings = list_buckets(root, subprocess_runner)
            if not listings:
                print("No state buckets match this fleet's naming template.")
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
            messages = delete_matching_buckets(root, subprocess_runner)
            if not messages:
                print("No state buckets matched this fleet's template.")
                return 0
            for message in messages:
                print(message)
            return 0
    except StateStoreGCPError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(_usage(), file=sys.stderr)
    return 2


def _usage() -> str:
    return (
        "usage: devcontainer_config.state_store_gcp init-form ROOT INSTANCE | "
        "ensure ROOT INSTANCE | list | delete INSTANCE | delete-all"
    )


def _require_args(rest: Sequence[str], count: int, what: str) -> list[str]:
    if len(rest) != count:
        raise StateStoreGCPError(f"{what} takes exactly {count} argument(s); got {len(rest)}")
    return list(rest)


def _render_listing(listing: BucketListing) -> str:
    """One aligned row: name, instance, project, configured mark."""
    mark = "configured" if listing.configured else "orphaned"
    return f"{listing.name}  {listing.instance:<20} {listing.project_id:<32} {mark}"


if __name__ == "__main__":
    sys.exit(main())
