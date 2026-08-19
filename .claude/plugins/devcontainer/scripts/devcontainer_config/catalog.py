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
never appears in argv and therefore never in the process table
(E3-F1-S1-T1 AC-FUNC-004).
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
region is hard-coded anywhere in this file (E3-F1-S1-T1 AC-FUNC-008).

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
from dataclasses import dataclass, replace
from re import Pattern
from typing import NoReturn

# The one external command this module ever asks a runner to invoke. Every
# argv built below starts with this constant; nothing else in this file
# names an executable, which is what lets a test prove there is exactly one
# backend (E3-F1-S1-T1 AC-FUNC-007) by asserting this literal appears
# exactly once in the module's source.
AWS_EXECUTABLE = "aws"

# The Parameter Store layout (spec Section 5.3), declared once so
# `parameter_path` and the listing path-filter share a single definition
# instead of each composing the shape independently.
PATH_ROOT = "/devcontainer"
SECRETS_SEGMENT = "secrets"

# The shared-tier scope name (spec Section 5.3, 5.4). `scope_set` returns
# this alone when no instance name is supplied, and appends it after the
# instance scope otherwise, so it is declared once instead of the literal
# "shared" drifting between `scope_set` and the tests that pin it.
SHARED_SCOPE = "shared"

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
# appearing in argv (E3-F1-S1-T1 AC-FUNC-004).
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

# A valid scope segment: one or more letters, digits, hyphens or
# underscores. `scope` is interpolated directly into the parameter path
# (spec Section 5.3), so this rejects the empty string and any separator
# character (notably "/") before that interpolation ever happens; without
# it, an unvalidated scope could compose a path outside
# `/devcontainer/<scope>/secrets/` (code_review and security_review,
# E3-F1-S1-T1, deferred to this unit because this is where an
# environment-derived instance name first reaches `scope`).
_SCOPE_PATTERN: Pattern[str] = re.compile(r"^[A-Za-z0-9_-]+$")


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

    Raised before any subprocess starts (E3-F1-S1-T1 AC-FUNC-002): an
    exported secret becomes a shell variable (spec Section 4.3), so a name
    that cannot be one is a usage error the store never needs to be
    consulted to detect.
    """


class InvalidScopeError(CatalogError):
    """A scope is not a valid path segment: empty, or containing a separator.

    Raised before any subprocess starts, by `parameter_path`, the listing
    path-filter builder, or `scope_set`, whichever sees the scope first
    (spec Section 5.3). `scope` is interpolated directly into the parameter
    path, so an empty or separator-bearing value (for example an instance
    name carrying "/" or "..") would otherwise compose a path outside
    `/devcontainer/<scope>/secrets/`, escaping the IAM-enforced prefix
    boundary this module relies on for the two-tier scoping in spec
    Section 5.4.
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
    """The store answered `ParameterNotFound` for a parameter path.

    Also raised by `resolve` (spec Section 5.4) when a name is present in
    none of the scopes in the resolution set: the message names every
    scope that was searched, in search order, so a caller never has to
    guess which tiers were consulted (E3-F1-S2-T1 AC-FUNC-005).
    """


class UnknownScopeError(CatalogError):
    """An explicitly requested scope is not in the caller's resolution set.

    Raised by `list_resolved` before any subprocess starts, when a caller
    narrows a listing to a `scope` that `scope_set` did not include for the
    supplied instance: neither the shared scope nor the resolved instance
    scope. Naming the scopes that are in effect lets the caller correct the
    request instead of guessing.
    """


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
    """One entry from a listing: metadata only, never a value
    (E3-F1-S1-T1 AC-FUNC-005).

    `exported` reflects the metadata `write` recorded in the parameter's
    Description when it created or last overwrote the record, which is the
    only field `describe-parameters` returns that this module can carry
    that flag in without a second API call per record.

    `in_effect` is `True` for every record from a single-scope listing
    (`CatalogClient.list_secrets`): read on its own, one scope has no other
    tier to be shadowed by. `list_resolved` (spec Section 5.4) is the only
    caller that ever sets it `False`, and only for a record whose name also
    exists in a scope earlier in the resolution order that `list_resolved`
    actually queried (E3-F1-S2-T1 AC-FUNC-006): the record is still
    returned, so `devsecret list` can render it, but marked as shadowed
    rather than omitted, so a name that exists in both tiers is never
    silently hidden from the four-column table in goal G4.

    `in_effect` is computed only across the scopes a given `list_resolved`
    call actually queried, never across the full resolution set implied by
    `instance`. When `list_resolved` is called with its `scope` argument
    narrowing the query to one scope, a record can carry `in_effect=True`
    even though a scope earlier in the full resolution order -- one that
    this narrowed call never queried -- holds the same name and would
    shadow it in an unnarrowed listing. A caller that needs the answer
    "in effect across the whole resolution set" must call `list_resolved`
    without `scope`.
    """

    name: str
    scope: str
    last_modified: str
    exported: bool
    in_effect: bool = True


@dataclass(frozen=True)
class ResolvedSecret:
    """The value a `resolve` call found, paired with the scope that answered.

    Returning the scope alongside the value (rather than the value alone)
    is what lets `devsecret get` report which tier answered without the
    caller re-deriving it (E3-F1-S2-T1 AC-FUNC-003, spec Section 5.4).
    """

    value: str
    scope: str


# The Runner a CatalogClient is constructed with: given the full argv and an
# optional stdin document, return a completed process. Injected rather than
# called internally via `subprocess.run` directly, so every test substitutes
# a fake runner instead of patching this module (E3-F1-S1-T1 AC-FUNC-008).
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


def _validate_scope(scope: str) -> None:
    if _SCOPE_PATTERN.fullmatch(scope) is None:
        raise InvalidScopeError(
            f"ERROR: invalid scope {scope!r}\n"
            "A scope must be a non-empty path segment: letters, digits, "
            "hyphens and underscores only, per spec Section 5.3.\n"
            "Use a valid instance name, or omit the instance entirely to "
            "reach the shared scope."
        )


def parameter_path(scope: str, name: str) -> str:
    """The absolute Parameter Store path for `name` in `scope` (spec Section 5.3).

    Validates `scope` and `name` before composing anything, so neither an
    invalid scope nor an invalid name ever reaches a caller that would
    otherwise use the returned path to start a subprocess
    (E3-F1-S1-T1 AC-FUNC-002). Validating `scope` here, once, is what keeps
    every direct caller of this function -- `read`, `write`, `delete` --
    from having to validate it again. `resolve` does not call this function
    directly; it reaches the same validation indirectly through
    `CatalogClient.read`.

    Raises:
        InvalidScopeError: if `scope` is empty or contains a path
            separator or another character outside the allowed set.
        InvalidSecretNameError: if `name` is not a valid environment-variable
            identifier.
    """
    _validate_scope(scope)
    _validate_name(name)
    return f"{PATH_ROOT}/{scope}/{SECRETS_SEGMENT}/{name}"


def _secrets_prefix(scope: str) -> str:
    """The path-filter prefix `list_secrets` searches under, for `scope`.

    Raises:
        InvalidScopeError: if `scope` is empty or contains a path
            separator or another character outside the allowed set.
    """
    _validate_scope(scope)
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


def _not_found_in_scopes_message(name: str, scopes: Sequence[str]) -> str:
    """E3-F1-S2-T1 AC-FUNC-005: `resolve` names every scope it searched, in search order."""
    searched = ", ".join(scopes)
    return (
        f"ERROR: no secret named {name!r} in any scope\n"
        f"Searched, in order: {searched}.\n"
        "List what exists in these scopes: devsecret list"
    )


def _unknown_scope_message(requested_scope: str, scopes_in_effect: Sequence[str]) -> str:
    effective = ", ".join(scopes_in_effect)
    return (
        f"ERROR: unknown scope {requested_scope!r}\n"
        f"The scopes in effect are: {effective}.\n"
        "Pass one of these scopes, or omit --scope to list every scope in effect."
    )


def _malformed_response_message(path: str, operation: str, reason: str) -> str:
    """A top-level `aws ssm` response is missing a field this client needs to read.

    Distinct from `_malformed_listing_message`: that one is for a single
    listing entry under a scope prefix; this one is for the envelope of the
    response itself (for example a `get-parameter` response with no
    `Parameter.Value`, or a `describe-parameters` response with no
    `Parameters` field), which would otherwise escape as a bare `KeyError`
    or `json.JSONDecodeError` instead of a named `CatalogError` (code_review,
    E3-F1-S1-T1, deferred to this unit).
    """
    return (
        f"ERROR: cannot complete '{operation}' for {path}\n"
        f"The store's response is malformed: {reason}.\n"
        "Retry the operation; if this persists, the aws CLI version may be "
        "incompatible with this client."
    )


def _parse_response_json(stdout: str, path: str, operation: str) -> dict[str, object]:
    """Parse `stdout` as the JSON object `aws ssm <operation>` returns for `path`.

    The one place `read` and `list_secrets` turn a response body into data,
    so a response that is not valid JSON, or that parses to something other
    than a JSON object, raises a named `CatalogError` here instead of a bare
    `json.JSONDecodeError` escaping from each call site individually.

    Raises:
        CatalogError: `stdout` is not valid JSON, or does not parse to a
            JSON object.
    """
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise CatalogError(
            _malformed_response_message(path, operation, "the response is not valid JSON")
        ) from exc
    if not isinstance(payload, dict):
        raise CatalogError(
            _malformed_response_message(path, operation, "the response is not a JSON object")
        )
    return payload


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
    (E3-F1-S1-T1 AC-FUNC-009).
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
            CatalogError: the response has no `Parameter.Value` field, or is
                not valid JSON.
            CatalogUnclassifiedError: the store rejected the operation for a
                reason this client does not classify (for example
                throttling, a validation failure, or an invalid region).
        """
        path = parameter_path(scope, name)
        argv = self._argv(GET_PARAMETER_OP, "--name", path, "--with-decryption")
        result = self._invoke(argv, None, path, operation=GET_PARAMETER_OP)
        payload = _parse_response_json(result.stdout, path, GET_PARAMETER_OP)
        parameter = payload.get("Parameter")
        if not isinstance(parameter, dict) or "Value" not in parameter:
            raise CatalogError(
                _malformed_response_message(
                    path, GET_PARAMETER_OP, "no 'Parameter.Value' field in the response"
                )
            )
        return str(parameter["Value"])

    def write(self, scope: str, name: str, value: str, *, exported: bool = False) -> None:
        """Store `value` at `scope`/`name` as a SecureString, overwriting any existing version.

        `value` is handed to the child process on stdin inside a
        `--cli-input-json` document; it is never placed in argv, so it never
        reaches the process table (E3-F1-S1-T1 AC-FUNC-004).

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
        (E3-F1-S1-T1 AC-FUNC-005): a listing structurally cannot expose one.
        `describe-parameters` returns at most one page per call (the
        store's own default page size), so this follows the response's
        `NextToken` with `--next-token` until the store stops returning one;
        without that, "every secret" would be false for a scope with more
        entries than one page.

        Raises:
            CatalogUnavailableError: the store could not be reached.
            CatalogUnauthorizedError: the caller lacks access to this prefix.
            CatalogError: the response has no `Parameters` field, is not
                valid JSON, or a listing entry is missing a field this
                client needs, or carries a Description this client did not
                write.
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
            payload = _parse_response_json(result.stdout, prefix, DESCRIBE_PARAMETERS_OP)
            parameters = payload.get("Parameters")
            if not isinstance(parameters, list):
                raise CatalogError(
                    _malformed_response_message(
                        prefix, DESCRIBE_PARAMETERS_OP, "no 'Parameters' field in the response"
                    )
                )
            for entry in parameters:
                records.append(_record_from_entry(entry, scope, prefix))
            next_token_field = payload.get("NextToken")
            if next_token_field is not None and not isinstance(next_token_field, str):
                raise CatalogError(
                    _malformed_response_message(
                        prefix, DESCRIBE_PARAMETERS_OP, "the 'NextToken' field is not a string"
                    )
                )
            next_token = next_token_field
            if not next_token:
                break
        return tuple(records)


# ---------------------------------------------------------------------------
# Scope resolution (spec Section 5.4, decision D12): instance-first then
# shared. Everything above this point is the single-scope transport added
# in E3-F1-S1-T1; everything below builds the two-tier resolution rule on
# top of it, once, so `resolve` and `list_resolved` share one definition of
# the resolution order instead of each computing it independently.
# ---------------------------------------------------------------------------


def scope_set(instance: str | None) -> tuple[str, ...]:
    """The ordered scope set to search for `instance` (spec Section 5.4, decision D12).

    With an instance name, the set is the instance scope followed by the
    shared scope (E3-F1-S2-T1 AC-FUNC-001): an instance value overrides a shared one
    without duplicating the rest of the catalog. Without one, the set is
    the shared scope alone -- this is not a fallback in the sense CLAUDE.md
    forbids, it is the complete and correct scope set for an engine that
    has no instance (decision D11, the local-engine case), and a `resolve`
    that finds nothing there still fails naming exactly that one scope. An
    instance named the same as the shared scope collapses to the shared
    scope exactly once (E3-F1-S2-T1 AC-FUNC-002), never twice.

    Raises:
        InvalidScopeError: `instance` is not `None` and is empty, or
            contains a path separator or another character outside the
            allowed set (defense in depth for the environment-derived
            instance name E8 will supply here, per spec Section 9).
    """
    if instance is None:
        return (SHARED_SCOPE,)
    _validate_scope(instance)
    if instance == SHARED_SCOPE:
        return (SHARED_SCOPE,)
    return (instance, SHARED_SCOPE)


def resolve(client: CatalogClient, instance: str | None, name: str) -> ResolvedSecret:
    """The value of `name`, searching `scope_set(instance)` in order, with the answering scope.

    Stops at the first scope that holds `name` (E3-F1-S2-T1 AC-FUNC-004): once the
    instance scope answers, the shared parameter path is never requested,
    so an override in the instance scope never causes an extra call to the
    shared prefix. A `SecretNotFoundError` from one scope only means "keep
    searching"; any other `CatalogError` (in particular
    `CatalogUnauthorizedError`) is not a not-found answer and is left to
    propagate immediately, so an authorization failure on one prefix is
    never silently treated as though the other prefix was consulted
    instead (spec Section 5.4: resolution order is a caller convenience,
    not a security control).

    Raises:
        InvalidScopeError: `instance` fails `scope_set`'s validation.
        InvalidSecretNameError: `name` is not a valid identifier.
        CatalogUnavailableError: the store could not be reached.
        CatalogUnauthorizedError: the caller lacks access to a searched
            prefix.
        SecretNotFoundError: `name` is present in none of the scopes in
            `scope_set(instance)`; the message names every scope searched,
            in search order (E3-F1-S2-T1 AC-FUNC-005).
        CatalogUnclassifiedError: the store rejected an operation for a
            reason this client does not classify.
    """
    scopes = scope_set(instance)
    searched: list[str] = []
    for scope in scopes:
        searched.append(scope)
        try:
            value = client.read(scope, name)
        except SecretNotFoundError:
            continue
        return ResolvedSecret(value=value, scope=scope)
    raise SecretNotFoundError(_not_found_in_scopes_message(name, searched))


def list_resolved(
    client: CatalogClient, instance: str | None, *, scope: str | None = None
) -> tuple[SecretRecord, ...]:
    """Every stored secret across `scope_set(instance)`, or across just `scope` when given.

    Returns one `SecretRecord` per stored parameter in every scope queried
    (E3-F1-S2-T1 AC-FUNC-006): a name present in both a queried instance
    scope and a queried shared scope produces two records, not one, so the
    caller can render both -- the instance record marked `in_effect=True`,
    the shared record marked `in_effect=False` (shadowed) -- rather than
    one record silently hiding the other. Built on
    `CatalogClient.list_secrets`, which calls `describe-parameters` and
    never requests decryption, so this listing path holds no secret value
    in memory at any point (E3-F1-S2-T1 AC-FUNC-007).

    `scope`, when given, narrows the query to that one scope instead of
    every scope in the resolution set; it must already be a member of
    `scope_set(instance)`, checked before any subprocess starts. Shadowing
    is computed only across the scopes this call actually queries: a
    narrowed, single-scope call never queries any other tier, so every
    record it returns carries `in_effect=True` even when a scope outside
    this call's query set -- earlier in the full resolution order -- holds
    the same name and would shadow it there. A caller that needs shadowing
    computed across the whole resolution set must call this function
    without `scope`.

    An authorization failure on any queried scope (`CatalogUnauthorizedError`)
    is not caught here: it propagates immediately, even when an earlier
    scope in the same call already answered successfully, so a denied
    prefix is never silently omitted from what looks like a complete
    listing (E3-F1-S2-T1 AC-FUNC-008).

    Raises:
        InvalidScopeError: `instance` fails `scope_set`'s validation.
        UnknownScopeError: `scope` is given and is not in
            `scope_set(instance)`.
        CatalogUnavailableError: the store could not be reached.
        CatalogUnauthorizedError: the caller lacks access to a queried
            prefix.
        CatalogError: a listing entry, or a response envelope, is
            malformed.
        CatalogUnclassifiedError: the store rejected an operation for a
            reason this client does not classify.
    """
    scopes = scope_set(instance)
    if scope is not None and scope not in scopes:
        raise UnknownScopeError(_unknown_scope_message(scope, scopes))
    query_scopes = (scope,) if scope is not None else scopes
    seen_names: set[str] = set()
    records: list[SecretRecord] = []
    for query_scope in query_scopes:
        for record in client.list_secrets(query_scope):
            in_effect = record.name not in seen_names
            seen_names.add(record.name)
            records.append(replace(record, in_effect=in_effect))
    return tuple(records)
