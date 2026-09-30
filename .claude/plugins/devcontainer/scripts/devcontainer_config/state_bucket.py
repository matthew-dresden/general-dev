"""The state backend's existence check behind the make Terragrunt targets.

Every Terragrunt-running make target inits the instance's backend first
(`tg_init` in the Makefile), and `tg_init` delegates one decision to this
module: does the backend bucket exist yet? The answer picks the init form --
a bucket that exists gets the plain `terragrunt init`, a missing one gets
`terragrunt init --backend-bootstrap`, which provisions it (versioning,
encryption and TLS enforcement included). Deciding before init, from one
explicit probe, replaces error-driven branching: no failure is ever caught
and retried under a different flag, and every probe failure that is not
"the bucket is missing" is raised with the command and its raw output, so
nothing is hidden.

The bucket's name is never duplicated here: `remote-instances/root.hcl` is
the single declaration of both the name template and the committed suffix,
and this module parses that file with the same interpolation grammar
Terragrunt itself resolves (`${local.NAME}` tokens), composing the name from
the instance name, the region in `REMOTE_AWS_REGION` and the ambient AWS
account -- the same three components root.hcl's own locals resolve. Every
aws call is issued through an injected `Runner` (the shape
`devcontainer_config.instance_ops` defines), so the unit suite runs
hermetically -- no AWS, no network -- and no test patches this module.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import sys
from collections.abc import Sequence
from pathlib import Path

from devcontainer_config import instances, repo
from devcontainer_config.instance_ops import Runner, subprocess_runner

# The one external command this module asks a runner to invoke; every argv
# below starts with it.
AWS_EXECUTABLE = "aws"

# The `${local.NAME}` token grammar root.hcl's template uses -- the same
# interpolation Terragrunt resolves at run time.
_INTERPOLATION_TOKEN = re.compile(r"\$\{local\.([A-Za-z0-9_]+)\}")

# The two declarations this module reads out of root.hcl: the name template
# and the committed suffix. Both are matched as full-line assignments so a
# commented-out or renamed declaration reads as missing rather than
# half-parsed.
_TEMPLATE_PATTERN = re.compile(r'^\s*state_bucket_name\s*=\s*"([^"]*)"', re.MULTILINE)
_SUFFIX_PATTERN = re.compile(r'^\s*state_bucket_suffix\s*=\s*"([^"]*)"', re.MULTILINE)

# Terragrunt's own error wording for a backend bucket that is not there --
# "An error occurred (404) when calling the HeadBucket operation: Not Found"
# from the aws CLI this module probes with. The parenthesized code and the
# service's own error name are matched exactly, so a request id that
# happens to contain "404" can never read as a missing bucket.
_MISSING_BUCKET_MARKERS: tuple[str, ...] = ("(404)", "NoSuchBucket")


class StateBucketError(RuntimeError):
    """The backend probe could not decide, or the name could not be composed.

    Raised for every outcome the caller must see rather than act on: an
    unreadable root.hcl, a missing template or suffix declaration, a name
    template referencing an unsupplied component, an aws call that failed
    for any reason other than a missing bucket, or an account lookup that
    answered nothing usable. Every message names the offending file, command
    or component and carries the raw command output, so nothing is hidden.
    """


def declared_template(root_hcl_text: str) -> str:
    """The raw `state_bucket_name` interpolation string committed in root.hcl."""
    match = _TEMPLATE_PATTERN.search(root_hcl_text)
    if match is None:
        raise StateBucketError(
            "no state_bucket_name declaration found in remote-instances/root.hcl; "
            "the bucket name template is the declaration this module composes from"
        )
    return match.group(1)


def declared_suffix(root_hcl_text: str) -> str:
    """The committed `state_bucket_suffix` value.

    A missing or empty suffix is a real declaration gap, not a value to
    invent: a replacement suffix would silently point the composed name at a
    different bucket than the one an earlier bootstrap provisioned.
    """
    match = _SUFFIX_PATTERN.search(root_hcl_text)
    if match is None or not match.group(1):
        raise StateBucketError(
            "no committed state_bucket_suffix found in remote-instances/root.hcl; a missing "
            "suffix means a fresh bootstrap would mint a new bucket instead of finding the "
            "existing one"
        )
    return match.group(1)


def compose_from_root_hcl(
    root_hcl_text: str, instance_name: str, aws_region: str, account_id: str
) -> str:
    """The bucket name root.hcl's template composes to for these components.

    Substitutes every `${local.NAME}` token in the declared template from the
    four components root.hcl's own locals resolve (instance name, region,
    account, committed suffix), in the order the template states. Raises
    naming the unresolved reference when the template names a component
    these values do not supply, rather than leaving a literal `${local...}`
    token embedded in the returned name.
    """
    # The template is read first: it is the primary declaration, and a file
    # missing both must report the missing template rather than the suffix.
    template = declared_template(root_hcl_text)
    values = {
        "instance_name": instance_name,
        "aws_region": aws_region,
        "account_id": account_id,
        "state_bucket_suffix": declared_suffix(root_hcl_text),
    }

    def _substitute(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in values:
            raise StateBucketError(
                f"remote-instances/root.hcl's state_bucket_name template references "
                f"local.{name}, which is not one of the composed components "
                f"{sorted(values)}"
            )
        return values[name]

    return _INTERPOLATION_TOKEN.sub(_substitute, template)


def account_id(runner: Runner) -> str:
    """The ambient AWS identity's account id, the same chain root.hcl resolves.

    `get_aws_account_id()` in root.hcl resolves through the ambient AWS SDK
    credential chain with no profile parameter, so the bucket name's account
    component and the account that creates and writes the bucket can never
    diverge; this lookup issues the CLI's equivalent against that same
    ambient chain -- no `--profile` -- for the same reason.

    Raises:
        StateBucketError: the command could not run, exited non-zero, or
            answered nothing usable.
    """
    argv = [AWS_EXECUTABLE, "sts", "get-caller-identity", "--query", "Account", "--output", "text"]
    return _probe_text(runner, argv, "the account id lookup")


def _probe_text(runner: Runner, argv: Sequence[str], what: str) -> str:
    """Run one aws command and return its stripped stdout, loudly on any failure."""
    try:
        result = runner(list(argv), None)
    except OSError as exc:
        raise StateBucketError(f"could not run {argv[0]} for {what}: {exc}") from exc
    value = result.stdout.strip()
    if result.returncode != 0 or not value or value == "None":
        raise StateBucketError(
            f"{what} failed: {' '.join(argv)} exited {result.returncode}\n"
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    return value


def root_hcl_path(root: Path) -> Path:
    """The `remote-instances/root.hcl` this repository's instances include."""
    return root / instances.INSTANCES_DIR_NAME / "root.hcl"


def bucket_name(root: Path, instance: str, aws_region: str, runner: Runner) -> str:
    """The state bucket name root.hcl composes for `instance` in `aws_region`.

    Reads `remote-instances/root.hcl` from the repository root, resolves the
    account through the ambient chain, and composes the name from root.hcl's
    own template and suffix.

    Raises:
        StateBucketError: `instance` fails `instances.validate_name`,
            root.hcl is missing or unreadable, its declarations are missing,
            or the account lookup failed.
    """
    try:
        instances.validate_name(instance)
    except instances.InvalidInstanceNameError as exc:
        raise StateBucketError(
            f"invalid instance name {instance!r}: the bucket probe composes the backend "
            "name from the instance name, so it must be a valid one"
        ) from exc
    try:
        root_hcl_text = root_hcl_path(root).read_text(encoding="utf-8")
    except OSError as exc:
        raise StateBucketError(
            f"could not read {root_hcl_path(root)}: the bucket name is composed from the "
            "template and suffix that file declares"
        ) from exc
    return compose_from_root_hcl(root_hcl_text, instance, aws_region, account_id(runner))


def init_form(root: Path, instance: str, aws_region: str, runner: Runner) -> str:
    """The init form `tg_init` must run: `"plain"` or `"bootstrap"`.

    One probe decides: `aws s3api head-bucket` against the composed name
    answers "exists" with exit 0 and "missing" with Terragrunt's own 404. A
    bucket that exists gets the plain init; a missing one gets the bootstrap
    init, which provisions it. Any other answer -- a 403 on a bucket this
    identity cannot see, a throttled or unreachable SSM endpoint, anything
    unexpected -- raises with the command and its raw output, so the caller
    never guesses and nothing is retried behind the failure.
    """
    name = bucket_name(root, instance, aws_region, runner)
    argv = [AWS_EXECUTABLE, "s3api", "head-bucket", "--bucket", name, "--region", aws_region]
    try:
        result = runner(list(argv), None)
    except OSError as exc:
        raise StateBucketError(
            f"could not run {' '.join(argv)} to probe the backend bucket: {exc}"
        ) from exc
    if result.returncode == 0:
        return "plain"
    output = f"{result.stderr.strip()}\n{result.stdout.strip()}".strip()
    if any(marker in output for marker in _MISSING_BUCKET_MARKERS):
        return "bootstrap"
    raise StateBucketError(
        f"the backend bucket probe for {name!r} failed: {' '.join(argv)} exited "
        f"{result.returncode}\n{output}\n"
        "The bucket is not confirmed missing, so no bootstrap is attempted; resolve "
        "the failure above, then retry."
    )


def region_from_environment() -> str:
    """`REMOTE_AWS_REGION` from the environment, loudly absent with the remedy.

    The Makefile's region guard runs before every Terragrunt call, so an
    unset region reaching this module is a direct-invocation slip; the
    error still says exactly what to set and how, per the same fail-fast
    rule the guard enforces.
    """
    region = os.environ.get("REMOTE_AWS_REGION", "").strip()
    if not region:
        raise StateBucketError(
            "REMOTE_AWS_REGION is required and has no default (root.hcl names each "
            "state bucket from it)\n"
            "Set it with: export REMOTE_AWS_REGION=<region> "
            "(for example: export REMOTE_AWS_REGION=us-east-1)"
        )
    return region


def bucket_name_pattern(root: Path) -> re.Pattern[str]:
    """The fullmatch pattern for this fleet's state bucket names, from root.hcl.

    Composed from the committed template itself -- literal parts escaped,
    each `${local.NAME}` token replaced by the character class of the
    component root.hcl resolves it from -- so the pattern can never drift
    from the name the module composes: one template declares both.
    """
    text = root_hcl_path(root).read_text(encoding="utf-8") if root_hcl_path(root).is_file() else ""
    if not text:
        raise StateBucketError(
            f"could not read {root_hcl_path(root)}: the fleet pattern is composed from the "
            "template and suffix that file declares"
        )
    template = declared_template(text)
    suffix = declared_suffix(text)
    component_patterns = {
        "instance_name": r"[A-Za-z0-9_-]+",
        "aws_region": r"[a-z0-9-]+",
        "account_id": r"\d{12}",
    }
    pieces: list[str] = []
    position = 0
    for match in _INTERPOLATION_TOKEN.finditer(template):
        pieces.append(re.escape(template[position : match.start()]))
        name = match.group(1)
        if name == "state_bucket_suffix":
            pieces.append(re.escape(suffix))
        else:
            pieces.append(component_patterns[name])
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
    region: str = ""
    account_id: str = ""


def list_buckets(root: Path, runner: Runner) -> tuple[BucketListing, ...]:
    """Every bucket in the account whose name this fleet's template matches.

    Reads the account's bucket list (the ambient chain, the same chain
    root.hcl resolves the account id from), keeps the names that fullmatch
    the template-derived pattern -- the committed suffix is part of it, so
    another repository's state buckets are never listed -- and marks which
    instances are configured in this checkout. Sorted by name, so the
    output is stable.

    Raises:
        StateBucketError: root.hcl is missing its declarations, or the aws
            call failed or answered unusable JSON.
    """
    pattern = bucket_name_pattern(root)
    account = account_id(runner)
    argv = [AWS_EXECUTABLE, "s3api", "list-buckets", "--output", "json"]
    try:
        result = runner(argv, None)
    except OSError as exc:
        raise StateBucketError(f"could not run {argv[0]} to list buckets: {exc}") from exc
    if result.returncode != 0:
        raise StateBucketError(
            f"listing buckets failed: {' '.join(argv)} exited {result.returncode}\n"
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise StateBucketError(
            "list-buckets printed output that is not valid JSON; the bucket list "
            "cannot be read safely from it"
        ) from exc
    buckets = payload.get("Buckets") if isinstance(payload, dict) else None
    if not isinstance(buckets, list):
        raise StateBucketError(
            "list-buckets response carries no 'Buckets' list; the bucket list "
            "cannot be read safely from it"
        )
    names = [bucket["Name"] for bucket in buckets if isinstance(bucket, dict) and "Name" in bucket]
    region = region_from_environment()
    root_hcl_file = root_hcl_path(root)
    try:
        root_hcl_text = root_hcl_file.read_text(encoding="utf-8")
    except OSError as exc:
        raise StateBucketError(
            f"could not read {root_hcl_file}: the fleet pattern is composed from the "
            "template and suffix that file declares"
        ) from exc
    pattern_names = {name for name in names if pattern.fullmatch(name)}
    listings: list[BucketListing] = []
    claimed: set[str] = set()
    for instance in instances.discover(root):
        name = compose_from_root_hcl(root_hcl_text, instance, region, account)
        if name in pattern_names:
            listings.append(
                BucketListing(
                    name=name,
                    configured=True,
                    instance=instance,
                    region=region,
                    account_id=account,
                )
            )
            claimed.add(name)
    for name in sorted(pattern_names - claimed):
        listings.append(BucketListing(name=name, configured=False))
    return tuple(listings)


def _purge_versions(bucket: str, runner: Runner) -> int:
    """Delete every version and delete marker in `bucket`; return how many went away.

    Raises:
        StateBucketError: a listing failed, or a delete batch reported
            errors -- the message names the failed keys, so a partially
            purged bucket never reads as empty.
    """
    argv = [AWS_EXECUTABLE, "s3api", "list-object-versions", "--bucket", bucket, "--output", "json"]
    try:
        result = runner(argv, None)
    except OSError as exc:
        raise StateBucketError(f"could not run {argv[0]} for {bucket!r}: {exc}") from exc
    if result.returncode != 0:
        raise StateBucketError(
            f"listing the versions of {bucket!r} failed: {' '.join(argv)} exited "
            f"{result.returncode}\n{result.stderr.strip() or result.stdout.strip()}"
        )
    try:
        payload = json.loads(result.stdout) if result.stdout.strip() else {}
    except json.JSONDecodeError as exc:
        raise StateBucketError(
            f"list-object-versions printed output that is not valid JSON for {bucket!r}; "
            "the bucket cannot be purged safely from it"
        ) from exc
    versions = payload.get("Versions") if isinstance(payload, dict) else None
    markers = payload.get("DeleteMarkers") if isinstance(payload, dict) else None
    entries = list(versions or []) + list(markers or [])
    objects = [
        {"Key": entry["Key"], "VersionId": entry["VersionId"]}
        for entry in entries
        if isinstance(entry, dict) and "Key" in entry and "VersionId" in entry
    ]
    for start in range(0, len(objects), 1000):
        batch = json.dumps({"Objects": objects[start : start + 1000], "Quiet": True})
        delete_argv = [
            AWS_EXECUTABLE,
            "s3api",
            "delete-objects",
            "--bucket",
            bucket,
            "--delete",
            batch,
            "--output",
            "json",
        ]
        try:
            delete_result = runner(delete_argv, None)
        except OSError as exc:
            raise StateBucketError(f"could not run {argv[0]} to purge {bucket!r}: {exc}") from exc
        if delete_result.returncode != 0:
            raise StateBucketError(
                f"purging {bucket!r} failed: {' '.join(delete_argv)} exited "
                f"{delete_result.returncode}\n"
                f"{delete_result.stderr.strip() or delete_result.stdout.strip()}"
            )
        try:
            report: object = (
                json.loads(delete_result.stdout) if delete_result.stdout.strip() else {}
            )
        except json.JSONDecodeError as exc:
            raise StateBucketError(
                f"delete-objects printed output that is not valid JSON for {bucket!r}; "
                "whether every version was purged is unknown"
            ) from exc
        reported = report.get("Errors") if isinstance(report, dict) else None
        errors = list(reported or [])
        if errors:
            raise StateBucketError(
                f"purging {bucket!r} left errors behind (the bucket is not empty):\n"
                + "\n".join(
                    f"- {error.get('Key')}: {error.get('Message')}"
                    for error in errors
                    if isinstance(error, dict)
                )
            )
    return len(objects)


def delete_bucket(bucket: str, region: str, runner: Runner) -> str:
    """Purge and delete one state bucket; report when it is already gone.

    The region is the one the bucket's own name embeds -- the region it was
    created in -- so the delete lands on the right endpoint regardless of
    any ambient default.

    Raises:
        StateBucketError: the purge or the delete failed, with the aws
            output in the message.
    """
    head_argv = [AWS_EXECUTABLE, "s3api", "head-bucket", "--bucket", bucket, "--region", region]
    try:
        head = runner(head_argv, None)
    except OSError as exc:
        raise StateBucketError(f"could not run {head_argv[0]} for {bucket!r}: {exc}") from exc
    if head.returncode != 0:
        output = f"{head.stderr.strip()}\n{head.stdout.strip()}".strip()
        if "(404)" in output or "NoSuchBucket" in output:
            return f"{bucket} is already absent; nothing to delete"
        if "(301)" in output or "Moved Permanently" in output:
            return (
                f"skipped {bucket}: it lives in a different region than the one this "
                "command targeted. Re-run with that region as REMOTE_AWS_REGION to "
                "delete it."
            )
        raise StateBucketError(
            f"the existence probe for {bucket!r} failed: {' '.join(head_argv)} exited "
            f"{head.returncode}\n{output}\n"
            "The bucket is not confirmed absent, so no delete is attempted."
        )
    purged = _purge_versions(bucket, runner)
    delete_argv = [
        AWS_EXECUTABLE,
        "s3api",
        "delete-bucket",
        "--bucket",
        bucket,
        "--region",
        region,
    ]
    try:
        deleted = runner(delete_argv, None)
    except OSError as exc:
        raise StateBucketError(f"could not run {delete_argv[0]} for {bucket!r}: {exc}") from exc
    if deleted.returncode != 0:
        raise StateBucketError(
            f"deleting {bucket!r} failed: {' '.join(delete_argv)} exited "
            f"{deleted.returncode}\n{deleted.stderr.strip() or deleted.stdout.strip()}"
        )
    return f"deleted {bucket} ({purged} version(s) purged)"


def delete_instance_bucket(root: Path, instance: str, region: str, runner: Runner) -> str:
    """Delete the state bucket composed for `instance` in `region`.

    Raises:
        StateBucketError: the instance name is invalid, root.hcl is missing
            its declarations, or the delete failed.
    """
    try:
        instances.validate_name(instance)
    except instances.InvalidInstanceNameError as exc:
        raise StateBucketError(f"invalid instance name {instance!r}: {exc}") from exc
    return delete_bucket(bucket_name(root, instance, region, runner), region, runner)


def delete_matching_buckets(root: Path, region: str, runner: Runner) -> tuple[str, ...]:
    """Delete every bucket this fleet's template matches, in `region`.

    Only names the template matches are touched -- another repository's
    state is never a candidate, because the committed suffix is part of the
    pattern. Each delete is aimed at `region`, the one REMOTE_AWS_REGION
    names; a bucket that answers from a different region is reported as
    skipped with the remedy, never deleted blind.

    Returns:
        One message per bucket (deleted, skipped, or already absent), in
        name order.
    """
    listings = list_buckets(root, runner)
    return tuple(delete_bucket(listing.name, region, runner) for listing in listings)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the selected subcommand; exit 0 only on a confirmed outcome.

    `init-form ROOT INSTANCE` prints `plain` or `bootstrap` for the
    Makefile's `tg_init` helper; `list` prints every fleet bucket with its
    instance, region and configured-here mark; `delete INSTANCE` purges and
    deletes one instance's bucket; `delete-all` does the same for every
    fleet bucket in `REMOTE_AWS_REGION` (the Makefile gates it behind
    `CONFIRM=delete`). All subcommands take the region from the environment
    where it is required, with the unset error naming what to set and how;
    every failure prints its reason to stderr and exits 1.
    """
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments:
        print(_usage(), file=sys.stderr)
        return 2
    command, rest = arguments[0], arguments[1:]
    try:
        if command == "init-form":
            (root_text, instance) = _require_args(rest, 2, "init-form")
            form = init_form(
                Path(root_text), instance, region_from_environment(), subprocess_runner
            )
            print(form)
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
            print(
                delete_instance_bucket(root, instance, region_from_environment(), subprocess_runner)
            )
            return 0
        if command == "delete-all":
            _require_args(rest, 0, "delete-all")
            root = repo.find_root(Path.cwd())
            messages = delete_matching_buckets(root, region_from_environment(), subprocess_runner)
            if not messages:
                print(
                    f"No state buckets matched this fleet's template in "
                    f"{os.environ.get('REMOTE_AWS_REGION', '?')}."
                )
                return 0
            for message in messages:
                print(message)
            return 0
    except StateBucketError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(_usage(), file=sys.stderr)
    return 2


def _usage() -> str:
    return (
        "usage: devcontainer_config.state_bucket init-form ROOT INSTANCE | list | "
        "delete INSTANCE | delete-all"
    )


def _require_args(rest: Sequence[str], count: int, what: str) -> list[str]:
    if len(rest) != count:
        raise StateBucketError(f"{what} takes exactly {count} argument(s); got {len(rest)}")
    return list(rest)


def _render_listing(listing: BucketListing) -> str:
    """One aligned row: name, instance, region, configured-here mark."""
    mark = "configured" if listing.configured else "orphaned"
    return (
        f"{listing.name}  {listing.instance:<20} {listing.region:<14} "
        f"{listing.account_id:<12} {mark}"
    )


if __name__ == "__main__":
    sys.exit(main())
