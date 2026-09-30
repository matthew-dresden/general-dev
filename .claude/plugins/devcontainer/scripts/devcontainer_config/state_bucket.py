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

import re
import sys
from collections.abc import Sequence
from pathlib import Path

from devcontainer_config import instances
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


def main(argv: Sequence[str] | None = None) -> int:
    """Print the init form for the three positional arguments to stdout.

    `python3 -m devcontainer_config.state_bucket <repo-root> <instance-name> <region>`
    prints exactly one word -- `plain` or `bootstrap` -- for the Makefile's
    `tg_init` helper to branch on. Any failure prints the reason to stderr
    and exits 1; there is no other exit path to hide behind.
    """
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 3:
        print(
            "ERROR: expected exactly three arguments: REPO_ROOT INSTANCE_NAME REGION\n"
            "This is the state backend's existence probe; the Makefile's tg_init "
            "helper supplies the three values.",
            file=sys.stderr,
        )
        return 2
    try:
        form = init_form(Path(arguments[0]), arguments[1], arguments[2], subprocess_runner)
    except StateBucketError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(form)
    return 0


if __name__ == "__main__":
    sys.exit(main())
