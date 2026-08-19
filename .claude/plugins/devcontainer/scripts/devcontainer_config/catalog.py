"""The secret catalog client: the only way this repository reaches a catalog secret.

One backend, AWS Parameter Store, per spec Section 5.4 and decision D10
(spec Section 13): there is no provider interface, no adapter registry and
no branch that could select a second implementation. The store answers or
the operation raises; nothing here degrades to a local copy, an environment
variable, or a cached value.

No secret value is ever written to any filesystem (spec Section 5.4, decision
D11). A value returned to a caller is an ordinary string held in memory for
the life of the process; this module opens no file for writing at all. A
write hands its value to `aws ssm put-parameter` on the child process's
stdin as a `--cli-input-json` document, never as an argument, so the value
never appears in argv and therefore never in the process table (AC-FUNC-004).
A listing calls `aws ssm describe-parameters`, whose response carries no
value field, rather than `get-parameters-by-path`, which does; that makes
"list never prints a value" structural rather than a discipline `list` has
to maintain (AC-4.3).

A secret lives at `/devcontainer/shared/secrets/<NAME>` or
`/devcontainer/<instance>/secrets/<NAME>` (spec Section 5.3); scoping is by
path prefix so IAM enforces the boundary, not convention. `<NAME>` is
validated as an environment-variable identifier before any subprocess starts,
because spec Section 4.3 makes an exported secret become a shell variable and
a name that cannot be one is a usage error, not a network failure.

Neither engine needs a stored credential to reach the store (decision D11):
remotely the instance role is available through IMDSv2, locally the
developer's SSO session is already valid. This client extends the
`bootstrap_secrets` / `fetch_parameter` / `imds_token` / `imds_get` pull
model named in spec Section 3.5 by invoking the `aws` CLI through a subprocess
runner injected at construction, so the whole module is testable with no
network, no AWS and no docker (AC-10.14), and so no credential, endpoint or
region is hard-coded anywhere in this file (AC-FUNC-008).

This module reads the standard `AWS_PROFILE` environment variable, if set,
to name the profile the operator should refresh when no credential resolves
(falling back to `default` when it is unset); it never sets or requires
`AWS_PROFILE` itself, and passes no profile flag to the `aws` CLI, which
resolves the profile the same way `AWS_PROFILE` already tells any other
`aws` invocation to. This module does not otherwise read or write any
credential.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from re import Pattern
from typing import NoReturn

# The one external command this module ever asks a runner to invoke. Every
# argv built below starts with this constant; nothing else in this file
# names an executable, which is what lets a test prove there is exactly one
# backend (AC-FUNC-007) by asserting this literal appears exactly once in
# the module's source.
AWS_EXECUTABLE = "aws"

# The Parameter Store layout (spec Section 5.3), declared once so
# `parameter_path` and the listing path-filter share a single definition
# instead of each composing the shape independently.
PATH_ROOT = "/devcontainer"
SECRETS_SEGMENT = "secrets"

SECURE_STRING_TYPE = "SecureString"

# The four `aws ssm` subcommands this module ever issues. Declared once so
# each is named identically in the argv it builds and in the operation name
# an error message reports for it, rather than the two spellings drifting
# apart.
GET_PARAMETER_OP = "get-parameter"
PUT_PARAMETER_OP = "put-parameter"
DELETE_PARAMETER_OP = "delete-parameter"
DESCRIBE_PARAMETERS_OP = "describe-parameters"

# describe-parameters is paginated (the store's own default page size);
# `list_secrets` follows this flag with the response's `NextToken` until the
# store stops returning one, so a scope larger than one page is not silently
# under-reported.
NEXT_TOKEN_FLAG = "--next-token"

# `--cli-input-json` accepts a `file://` URI; pointing it at the process's
# own stdin is what lets `write` hand the value to `aws` without it ever
# appearing in argv (AC-FUNC-004).
CLI_INPUT_STDIN_URI = "file:///dev/stdin"

AWS_PROFILE_ENV_VAR = "AWS_PROFILE"
DEFAULT_AWS_PROFILE = "default"

# Substrings the AWS CLI's stderr carries for the error conditions this
# module distinguishes from an unclassified failure. Matched literally, not
# parsed as JSON, because the CLI's own error rendering is plain text. Only
# these specific conditions are ever diagnosed as "no credential resolved";
# every other non-zero exit is unclassified (see `_raise_for_failure`),
# because asserting a credential cause for a failure that is not one hands
# the operator a remediation that cannot work.
PARAMETER_NOT_FOUND_MARKER = "ParameterNotFound"
ACCESS_DENIED_MARKER = "AccessDeniedException"
SSO_SESSION_MARKER = "SSO Token"
NO_CREDENTIALS_MARKER = "Unable to locate credentials"

# A valid environment-variable identifier (spec Section 4.3): starts with a
# letter or underscore, then only letters, digits or underscores. Declared
# once and reused by every name-validating call site.
_NAME_PATTERN: Pattern[str] = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class CatalogError(RuntimeError):
    """Base class for every error this module raises.

    A caller that only needs to know "the catalog operation failed" can
    catch this one class; a caller that needs to react differently to each
    condition catches the specific subclass below. Every raise site names
    the parameter path and the operator's next command, never a secret
    value (spec Section 7.1).
    """


class InvalidSecretNameError(CatalogError):
    """A secret name is not a valid environment-variable identifier.

    Raised before any subprocess starts (AC-FUNC-002): an exported secret
    becomes a shell variable (spec Section 4.3), so a name that cannot be
    one is a usage error the store never needs to be consulted to detect.
    """


class CatalogUnavailableError(CatalogError):
    """The store could not be reached: no `aws` binary, or no credential resolved.

    Covers exactly two conditions, each detected by a different mechanism.
    The `aws` binary missing from PATH is detected when the injected runner
    raises `FileNotFoundError` (`CatalogClient._invoke`) before the CLI ever
    runs, so no stderr exists to match against; no AWS credential resolving
    (no reachable instance role via IMDSv2, no valid or unexpired SSO
    session) is detected by matching `SSO_SESSION_MARKER` or
    `NO_CREDENTIALS_MARKER` against the `aws` CLI's stderr. Neither case
    falls back to a local store, an environment variable or a cached copy
    (decision D11): the operation simply fails. This class is intentionally
    narrow -- an `aws` failure that is not one of these two conditions, not
    an access-denied response and not a not-found response raises
    `CatalogUnclassifiedError` instead, so a caller is never told to
    refresh a credential that was not the actual problem.
    """


class CatalogUnauthorizedError(CatalogError):
    """The store answered `AccessDeniedException` for a parameter prefix.

    The caller's identity lacks the IAM grant for the prefix; nothing in
    this module retries with different credentials or degrades to a partial
    result.
    """


class SecretNotFoundError(CatalogError):
    """The store answered `ParameterNotFound` for a parameter path."""


class CatalogUnclassifiedError(CatalogError):
    """The store rejected the operation for a reason this module does not classify.

    Raised for every non-zero `aws` exit that is not a missing/expired
    credential, an `AccessDeniedException`, or a `ParameterNotFound`
    response (for example `ThrottlingException`, `ValidationException`, an
    invalid region, or a malformed filter). The message names the parameter
    path, the operation and the CLI exit code, but never repeats the
    store's raw stderr: a `put-parameter` failure's stderr can contain the
    submitted document, and this module never echoes store output that
    might carry a secret value (spec Section 7.1, 7.4).
    """


@dataclass(frozen=True)
class SecretRecord:
    """One entry from a listing: metadata only, never a value (AC-FUNC-005).

    `exported` reflects the metadata `write` recorded in the parameter's
    Description when it created or last overwrote the record, which is the
    only field `describe-parameters` returns that this module can carry
    that flag in without a second API call per record.
    """

    name: str
    scope: str
    last_modified: str
    exported: bool


# The Runner a CatalogClient is constructed with: given the full argv and an
# optional stdin document, return a completed process. Injected rather than
# called internally via `subprocess.run` directly, so every test substitutes
# a fake runner instead of patching this module (AC-FUNC-008).
Runner = Callable[[Sequence[str], "str | None"], subprocess.CompletedProcess[str]]


def subprocess_runner(argv: Sequence[str], stdin: str | None) -> subprocess.CompletedProcess[str]:
    """The production Runner: a real subprocess, fed `stdin` on its stdin.

    This is what a caller outside the test suite constructs a `CatalogClient`
    with. `CatalogClient` itself never imports or calls `subprocess.run`, so
    nothing here needs patching to be tested hermetically.
    """
    return subprocess.run(list(argv), input=stdin, capture_output=True, text=True, check=False)


def _validate_name(name: str) -> None:
    if _NAME_PATTERN.fullmatch(name) is None:
        raise InvalidSecretNameError(
            f"ERROR: invalid secret name {name!r}\n"
            "A secret name must be a valid environment-variable identifier: "
            "it must start with a letter or underscore, and contain only "
            "letters, digits and underscores, per spec Section 4.3.\n"
            "Rename the secret to a valid identifier, then retry."
        )


def parameter_path(scope: str, name: str) -> str:
    """The absolute Parameter Store path for `name` in `scope` (spec Section 5.3).

    Validates `name` before composing anything, so an invalid name never
    reaches a caller that would otherwise use the returned path to start a
    subprocess (AC-FUNC-002).

    Raises:
        InvalidSecretNameError: if `name` is not a valid environment-variable
            identifier.
    """
    _validate_name(name)
    return f"{PATH_ROOT}/{scope}/{SECRETS_SEGMENT}/{name}"


def _secrets_prefix(scope: str) -> str:
    """The path-filter prefix `list_secrets` searches under, for `scope`."""
    return f"{PATH_ROOT}/{scope}/{SECRETS_SEGMENT}"


def _unavailable_no_binary_message(path: str) -> str:
    return (
        f"ERROR: cannot reach the secret catalog for {path}\n"
        "The aws CLI is not on PATH.\n"
        "Install the AWS CLI v2 so this command can run 'aws ssm', then retry."
    )


def _unavailable_no_credential_message(path: str) -> str:
    profile = os.environ.get(AWS_PROFILE_ENV_VAR, DEFAULT_AWS_PROFILE)
    return (
        f"ERROR: cannot reach the secret catalog for {path}\n"
        f"No AWS credential resolved for profile '{profile}'.\n"
        f"Run 'aws sso login --profile {profile}' to refresh the session, then retry."
    )


def _unauthorized_message(path: str) -> str:
    prefix = path.rsplit("/", 1)[0]
    return (
        f"ERROR: access denied for {path}\n"
        f"The caller's identity is not authorized for the parameter prefix {prefix}.\n"
        "Ask an operator to grant the missing ssm:* permission on this prefix, "
        "then retry."
    )


def _not_found_message(path: str) -> str:
    prefix = path.rsplit("/", 1)[0]
    return (
        f"ERROR: no secret at {path}\n"
        "The store reported ParameterNotFound.\n"
        f"List what exists under this scope: aws ssm describe-parameters "
        f"--parameter-filters Key=Path,Option=Recursive,Values={prefix}"
    )


def _unclassified_failure_next_step(path: str, operation: str) -> str:
    """The remediation clause naming a command that actually runs for `operation`.

    `get-parameter` and `delete-parameter` accept `--name`, so re-running the
    same shape the client itself issued is a faithful diagnostic. Neither is
    true of the other two operations: `describe-parameters` has no `--name`
    option at all and is always given a scope prefix (not a parameter
    path) by this client, so it must be re-run with the same
    `--parameter-filters` form `list_secrets` itself uses; `put-parameter`
    requires `--value`/`--type` to run at all, and a faithful re-submission
    would mean typing the secret value on a command line, which is exactly
    what this module exists to avoid, so the hint reads back the parameter's
    current metadata instead of re-submitting the write.
    """
    if operation == DESCRIBE_PARAMETERS_OP:
        rerun = (
            "aws ssm describe-parameters --parameter-filters "
            f"Key=Path,Option=Recursive,Values={path} --output json"
        )
        return (
            "The store's diagnostic is not repeated here because it may "
            f"contain response detail this client does not echo; re-run "
            f"'{rerun}' (add '--debug' for detail) to see it directly, then "
            "retry."
        )
    if operation == PUT_PARAMETER_OP:
        rerun = f"aws ssm get-parameter --name {path} --output json"
        return (
            "The store's diagnostic is not repeated here because it may "
            "contain the submitted document; re-submitting the write would "
            f"mean typing the secret value on a command line, so read back "
            f"the parameter's current metadata instead: '{rerun}', then "
            "retry the write."
        )
    rerun = f"aws ssm {operation} --name {path} --output json"
    return (
        "The store's diagnostic is not repeated here because it may contain "
        f"response detail this client does not echo; re-run '{rerun}' (add "
        "'--debug' for detail) to see it directly, then retry."
    )


def _unclassified_failure_message(path: str, operation: str, returncode: int) -> str:
    return (
        f"ERROR: cannot complete '{operation}' for {path}\n"
        f"The aws CLI exited with status {returncode}; this is not a missing "
        "credential, an access-denied response, or a not-found response, so "
        "this client does not guess a cause.\n"
        f"{_unclassified_failure_next_step(path, operation)}"
    )


def _raise_for_failure(path: str, operation: str, returncode: int, stderr: str) -> NoReturn:
    if PARAMETER_NOT_FOUND_MARKER in stderr:
        raise SecretNotFoundError(_not_found_message(path))
    if ACCESS_DENIED_MARKER in stderr:
        raise CatalogUnauthorizedError(_unauthorized_message(path))
    if SSO_SESSION_MARKER in stderr or NO_CREDENTIALS_MARKER in stderr:
        raise CatalogUnavailableError(_unavailable_no_credential_message(path))
    raise CatalogUnclassifiedError(_unclassified_failure_message(path, operation, returncode))


def _malformed_listing_message(identifier: str, prefix: str, reason: str) -> str:
    return (
        f"ERROR: cannot list {identifier}\n"
        f"{reason}\n"
        "Inspect the store directly: aws ssm describe-parameters "
        f"--parameter-filters Key=Path,Option=Recursive,Values={prefix}"
    )


def _foreign_parameter_message(name: str, prefix: str, reason: str) -> str:
    return (
        f"ERROR: cannot list {name}\n"
        f"{reason}\n"
        f"Re-write {name} with this client's write operation, or exclude it "
        f"from the scope {prefix}, then retry."
    )


def _record_from_entry(entry: dict[str, object], scope: str, prefix: str) -> SecretRecord:
    """Build one `SecretRecord` from a `describe-parameters` entry, boundary-validated.

    A parameter under `prefix` that this client did not write (created in
    the AWS console, by an operator, or by the existing `bootstrap_secrets`
    path) has no `Description` at all -- `describe-parameters` omits the
    key rather than returning it empty -- and a `Description` this client
    did not write is not guaranteed to be the JSON document this client
    reads it as. Both conditions, and a response entry missing a field a
    record needs, are validated here so a foreign parameter under the scope
    raises a named `CatalogError` naming the parameter path and what is
    missing or unparsable, instead of a bare `KeyError` or
    `json.JSONDecodeError` escaping the `CatalogError` hierarchy
    (AC-FUNC-009).
    """
    name = entry.get("Name")
    if not isinstance(name, str):
        reason = "A record under this prefix has no 'Name' field."
        raise CatalogError(_malformed_listing_message(prefix, prefix, reason))
    if "LastModifiedDate" not in entry:
        reason = "The record has no 'LastModifiedDate' field."
        raise CatalogError(_malformed_listing_message(name, prefix, reason))
    if "Description" not in entry:
        raise CatalogError(
            _foreign_parameter_message(
                name,
                prefix,
                "The parameter has no Description field, so this client "
                "cannot read the exported flag it stores there.",
            )
        )
    try:
        metadata = json.loads(str(entry["Description"]))
    except json.JSONDecodeError as exc:
        raise CatalogError(
            _foreign_parameter_message(
                name,
                prefix,
                "The parameter's Description field is not the JSON document "
                "this client writes, so the exported flag cannot be read "
                "from it.",
            )
        ) from exc
    if not isinstance(metadata, dict) or "exported" not in metadata:
        raise CatalogError(
            _foreign_parameter_message(
                name, prefix, "The parameter's Description JSON has no 'exported' field."
            )
        )
    return SecretRecord(
        name=name.rsplit("/", 1)[-1],
        scope=scope,
        last_modified=str(entry["LastModifiedDate"]),
        exported=bool(metadata["exported"]),
    )


class CatalogClient:
    """Reads and writes individual secrets in AWS Parameter Store (spec Section 5.4).

    `runner` and `region` are the only inputs: no credential, endpoint or
    region is read from anywhere else in this class, which is what lets a
    test construct one with no network, no AWS and no docker (AC-10.14).
    """

    def __init__(self, runner: Runner, *, region: str | None = None) -> None:
        self._runner = runner
        self._region = region

    def _argv(self, *operation_args: str) -> list[str]:
        """The shared argv shell every operation builds on.

        Executable, subcommand, output format and region live here, once.
        Collapsing this into one method is what keeps the output format and
        the region flag from drifting between the four operations below.
        """
        argv = [AWS_EXECUTABLE, "ssm", *operation_args, "--output", "json"]
        if self._region is not None:
            argv += ["--region", self._region]
        return argv

    def _invoke(
        self, argv: list[str], stdin: str | None, path: str, *, operation: str
    ) -> subprocess.CompletedProcess[str]:
        """Run `argv` through the injected runner and translate any failure.

        The one place every operation's error handling passes through, so
        the translation from an `aws` exit code or a missing binary to a
        `CatalogError` subclass exists once, not once per operation.
        `operation` is the `aws ssm` subcommand this call issues, named in
        any resulting `CatalogUnclassifiedError` message so the operator
        knows which command to re-run for the store's own diagnostic.
        """
        try:
            result = self._runner(argv, stdin)
        except FileNotFoundError as exc:
            raise CatalogUnavailableError(_unavailable_no_binary_message(path)) from exc
        if result.returncode != 0:
            _raise_for_failure(path, operation, result.returncode, result.stderr)
        return result

    def read(self, scope: str, name: str) -> str:
        """The value stored at `scope`/`name`, byte for byte, including any trailing newline.

        Raises:
            InvalidSecretNameError: `name` is not a valid identifier.
            CatalogUnavailableError: the store could not be reached.
            CatalogUnauthorizedError: the caller lacks access to this prefix.
            SecretNotFoundError: no parameter exists at this path.
            CatalogUnclassifiedError: the store rejected the operation for a
                reason this client does not classify (for example
                throttling, a validation failure, or an invalid region).
        """
        path = parameter_path(scope, name)
        argv = self._argv(GET_PARAMETER_OP, "--name", path, "--with-decryption")
        result = self._invoke(argv, None, path, operation=GET_PARAMETER_OP)
        payload = json.loads(result.stdout)
        return str(payload["Parameter"]["Value"])

    def write(self, scope: str, name: str, value: str, *, exported: bool = False) -> None:
        """Store `value` at `scope`/`name` as a SecureString, overwriting any existing version.

        `value` is handed to the child process on stdin inside a
        `--cli-input-json` document; it is never placed in argv, so it never
        reaches the process table (AC-FUNC-004).

        Raises:
            InvalidSecretNameError: `name` is not a valid identifier.
            CatalogUnavailableError: the store could not be reached.
            CatalogUnauthorizedError: the caller lacks access to this prefix.
            CatalogUnclassifiedError: the store rejected the operation for a
                reason this client does not classify (for example
                throttling, a validation failure, or an invalid region).
        """
        path = parameter_path(scope, name)
        document = json.dumps(
            {
                "Name": path,
                "Value": value,
                "Type": SECURE_STRING_TYPE,
                "Overwrite": True,
                "Description": json.dumps({"exported": exported}),
            }
        )
        argv = self._argv(PUT_PARAMETER_OP, "--cli-input-json", CLI_INPUT_STDIN_URI)
        self._invoke(argv, document, path, operation=PUT_PARAMETER_OP)

    def delete(self, scope: str, name: str) -> None:
        """Delete the parameter at `scope`/`name`.

        Raises:
            InvalidSecretNameError: `name` is not a valid identifier.
            CatalogUnavailableError: the store could not be reached.
            CatalogUnauthorizedError: the caller lacks access to this prefix.
            SecretNotFoundError: no parameter exists at this path.
            CatalogUnclassifiedError: the store rejected the operation for a
                reason this client does not classify (for example
                throttling, a validation failure, or an invalid region).
        """
        path = parameter_path(scope, name)
        argv = self._argv(DELETE_PARAMETER_OP, "--name", path)
        self._invoke(argv, None, path, operation=DELETE_PARAMETER_OP)

    def list_secrets(self, scope: str) -> tuple[SecretRecord, ...]:
        """Every secret under `scope`, paginating until the store stops returning a page.

        Calls `describe-parameters`, whose response has no field that could
        carry a value, rather than `get-parameters-by-path`, which does
        (AC-FUNC-005): a listing structurally cannot expose one.
        `describe-parameters` returns at most one page per call (the
        store's own default page size), so this follows the response's
        `NextToken` with `--next-token` until the store stops returning one;
        without that, "every secret" would be false for a scope with more
        entries than one page.

        Raises:
            CatalogUnavailableError: the store could not be reached.
            CatalogUnauthorizedError: the caller lacks access to this prefix.
            CatalogError: a listing entry is missing a field this client
                needs, or carries a Description this client did not write.
            CatalogUnclassifiedError: the store rejected the operation for a
                reason this client does not classify (for example
                throttling, a validation failure, or a malformed filter).
        """
        prefix = _secrets_prefix(scope)
        path_filter = f"Key=Path,Option=Recursive,Values={prefix}"
        records: list[SecretRecord] = []
        next_token: str | None = None
        while True:
            operation_args = [DESCRIBE_PARAMETERS_OP, "--parameter-filters", path_filter]
            if next_token is not None:
                operation_args += [NEXT_TOKEN_FLAG, next_token]
            argv = self._argv(*operation_args)
            result = self._invoke(argv, None, prefix, operation=DESCRIBE_PARAMETERS_OP)
            payload = json.loads(result.stdout)
            for entry in payload["Parameters"]:
                records.append(_record_from_entry(entry, scope, prefix))
            next_token = payload.get("NextToken")
            if not next_token:
                break
        return tuple(records)
