"""Host-resolved credentials: manifest loading, the three host resolvers, and the
shell-startup text that exports what they found.

A devcontainer has no access to the developer's macOS keychain, to their git
credential store, or to their AWS SSO session -- and the point of this
mechanism is that it never gains any. Instead, 'make push-creds' (wired in a
later unit) runs on the host, resolves each credential named in
.devcontainer/hostcreds.map.json from one of three sources -- the macOS
keychain, git's own credential helper, or 'aws configure export-credentials'
-- and pushes the resolved values into the container as one <NAME>.env
fragment per credential under ~/.hostcreds/, plus an entry in
~/.git-credentials for a git-source credential. The container's shell
startup sources those fragments; it never resolves anything itself.

This module is the core of that mechanism: the manifest contract
(`load_manifest`), the three resolvers (`resolve_keychain`, `resolve_git`,
`resolve_aws_export`, dispatched by `resolve`), and the two renderers
(`render_startup_block`, `render_env_fragment`) whose text later units
install. The CLI, Makefile target and postCreate wiring -- and the deletion
of the devsecret SSM catalog this mechanism replaces -- land in later
units; nothing here imports or modifies them.

Argv discipline shapes every resolver: a resolved value travels only on a
subprocess's stdout, never in its argv, so it never appears in the process
table. The git resolver is the strictest case -- the hostname is fed to
'git credential fill' on stdin, not passed as an argument. Only labels
appear on argv: the keychain service and account, and the aws profile,
none of which is a secret. Conversely, a failing command's diagnostic is
its stderr, so `ResolutionError` quotes stderr and never stdout: an error
message that repeated stdout would quote the very secret the call was made
to fetch.

The manifest is validated fail-fast and completely: `load_manifest`
collects every problem in the file into one `ManifestError` rather than
reporting only the first, because an operator fixing a multi-entry
manifest one error at a time re-runs push-creds once per mistake.

Runner injection follows `devcontainer_config.catalog` exactly: every
resolver takes the runner as an argument, so the whole module is testable
with no keychain, no git, no aws and no network, and no test has to patch
this module. Every function takes the repository root (or a rendered
credential) as data rather than discovering the checkout itself, for the
same reason `repo`'s docstring gives: a test points the whole package at a
temporary directory instead of the real checkout.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from re import Pattern

# The manifest and its committed example live under the repo root's
# .devcontainer/ directory, the same private-configuration location
# .devcontainer/aws-profile-map.json already occupies (that path is spelled
# by `repo.AWS_PROFILE_MAP`; this module declares its own constant because
# `repo` offers no helper for arbitrary .devcontainer paths, and duplicating
# one string here is cheaper than growing `repo`'s fixed PRIVATE_FILES
# surface for a later unit to rewire).
DEVCONTAINER_DIRECTORY = ".devcontainer"
MANIFEST_FILENAME = "hostcreds.map.json"
MANIFEST_EXAMPLE_FILENAME = "hostcreds.map.json.example"

# The three sources a manifest entry may name. Declared once each so the
# validation error that lists the allowed values, the resolver dispatch and
# the tests all quote identical spellings instead of drifting literals.
SOURCE_KEYCHAIN = "keychain"
SOURCE_GIT = "git"
SOURCE_AWS_EXPORT = "aws-export"

# The label keys each source understands. An unknown label key is a
# ManifestError rather than a warning (a "servcie" typo would otherwise
# silently mean the default service is used and the wrong keychain item
# read), which is why the allowed set per source is declared here once and
# enforced for every entry.
KEYCHAIN_SERVICE_LABEL = "service"
KEYCHAIN_ACCOUNT_LABEL = "account"
GIT_HOST_LABEL = "host"
AWS_PROFILE_LABEL = "profile"

_ALLOWED_LABEL_KEYS: Mapping[str, frozenset[str]] = {
    SOURCE_KEYCHAIN: frozenset({KEYCHAIN_SERVICE_LABEL, KEYCHAIN_ACCOUNT_LABEL}),
    SOURCE_GIT: frozenset({GIT_HOST_LABEL}),
    SOURCE_AWS_EXPORT: frozenset({AWS_PROFILE_LABEL}),
}

# The default keychain service derives from the repo directory's basename
# and the credential's name, so one project's keychain items cannot collide
# with another's without the operator choosing explicit 'service' labels.
KEYCHAIN_SERVICE_PREFIX = "devcontainer"

# The aws profile used when an aws-export entry sets none, matching the aws
# CLI's own default so the resolver and the CLI agree on which profile a
# bare 'aws configure export-credentials' would read.
DEFAULT_AWS_PROFILE = "default"

# The three executables this module ever asks a runner to invoke. Declared
# once each so the argv builders and the missing-binary diagnostics quote
# identical names instead of drifting literals.
SECURITY_EXECUTABLE = "security"
GIT_EXECUTABLE = "git"
AWS_EXECUTABLE = "aws"

# The environment variables an aws-export fragment exports. Declared here
# once because two decisions share the set: an aws-export credential whose
# manifest name is itself one of these three skips the additional raw-
# document export (it would collide with a parsed export), and the fragment
# renderer needs the spellings in the first place.
AWS_ACCESS_KEY_ID_VAR = "AWS_ACCESS_KEY_ID"
AWS_SECRET_ACCESS_KEY_VAR = "AWS_SECRET_ACCESS_KEY"
AWS_SESSION_TOKEN_VAR = "AWS_SESSION_TOKEN"
_AWS_ENVIRONMENT_VARS: frozenset[str] = frozenset(
    {AWS_ACCESS_KEY_ID_VAR, AWS_SECRET_ACCESS_KEY_VAR, AWS_SESSION_TOKEN_VAR}
)

# The AWS response fields the aws-export resolver and fragment renderer
# read, declared once so both quote identical spellings.
AWS_ACCESS_KEY_FIELD = "AccessKeyId"
AWS_SECRET_KEY_FIELD = "SecretAccessKey"
AWS_SESSION_TOKEN_FIELD = "SessionToken"
AWS_EXPIRATION_FIELD = "Expiration"

# A valid credential name (the manifest keys): starts with an uppercase
# letter, then uppercase letters, digits or underscores. Stricter than
# `catalog`'s identifier rule on purpose: a name becomes both a shell
# variable at startup and a <NAME>.env fragment filename in the store
# directory, and the store directory is case-insensitive on macOS, where
# Token and token would collide as filenames long before any shell saw
# them. Non-empty is implied: the pattern cannot match the empty string.
_CREDENTIAL_NAME_PATTERN: Pattern[str] = re.compile(r"^[A-Z][A-Z0-9_]*$")

# Credential names this module refuses no matter how well-formed they look.
# A manifest name becomes a shell variable the startup block exports, so a
# name that is itself a shell or loader variable (PATH, LD_PRELOAD,
# DYLD_INSERT_LIBRARIES) or one of the AWS variables an aws-export fragment
# exports would silently shadow the real thing -- a keychain entry named
# AWS_SECRET_ACCESS_KEY would otherwise clobber the value parsed out of the
# CLI's JSON document at startup. Checked for every source: the collision
# is in the name, not in the source that produced the value.
_RESERVED_CREDENTIAL_NAMES: frozenset[str] = frozenset(
    {"PATH", "LD_PRELOAD", "DYLD_INSERT_LIBRARIES", *_AWS_ENVIRONMENT_VARS}
)

# A bare hostname (the git source's 'host' label): alphanumeric first
# character, then alphanumerics, dots, hyphens or underscores. The pattern
# structurally excludes a scheme (https://...), any path separator, a port
# (host:22) and a user part (user@host), because 'git credential fill'
# wants a bare host on its stdin and anything else would silently match no
# stored credential.
_HOSTNAME_PATTERN: Pattern[str] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

# The store directory the rendered startup block reads. A single directory
# name under $HOME, not a path: interpolating a name into the block's shell
# text is only safe if the name cannot carry a separator, a space or a
# quote, which `_STORE_DIR_NAME_PATTERN` and the two dot-name rejections in
# `_validate_store_dir_name` enforce before any interpolation happens.
DEFAULT_STORE_DIR_NAME = ".hostcreds"
_STORE_DIR_NAME_PATTERN: Pattern[str] = re.compile(r"^[A-Za-z0-9._-]+$")

# The characters a label value must never carry. Labels reach shell text and
# argv -- they are embedded in a fragment's comment header and passed to the
# source commands -- and a newline would let a value escape the comment line
# it was written onto (the rest of the line would then execute as shell),
# while a quote of either kind would terminate the quoting the surrounding
# text relies on.
_UNSAFE_LABEL_CHARACTERS: frozenset[str] = frozenset({"\n", "\r", "'", '"'})


class HostCredsError(RuntimeError):
    """Base class for every error this module raises.

    A caller that only needs to know "the hostcreds operation failed" can
    catch this one class; a caller that needs to react differently to a bad
    manifest versus a failed source command catches the specific subclass
    below. Every raise site names the file or command involved and the
    operator's next step, never a resolved value.
    """


class ManifestError(HostCredsError):
    """The manifest is missing, unreadable, unparseable or invalid.

    Raised by `load_manifest` only, always before any subprocess starts: a
    manifest problem is a usage error the keychain, git and the aws CLI
    never need to be consulted to detect. The message lists every problem
    in the file at once, each naming the offending entry, so one push-creds
    run surfaces the whole mistake, not the first of several.
    """


class ResolutionError(HostCredsError):
    """A source command failed, or answered something unusable.

    Covers a non-zero exit from 'security', 'git credential fill' or 'aws
    configure export-credentials'; a missing binary (the runner raises
    FileNotFoundError, translated here); an empty resolved value; and, for
    the aws source, an exit-zero response that is not the JSON object with
    the fields this module needs. The message quotes the command and its
    stderr, and never its stdout -- stdout is where the secret travels.
    """


@dataclass(frozen=True)
class CredentialSpec:
    """One validated manifest entry: a name, its source and its labels.

    `labels` carries the source-specific keys with all defaults already
    applied by `load_manifest` -- a keychain spec always holds a 'service'
    (its own, or the devcontainer/<project>/<name> default) and may hold an
    'account'; a git spec always holds a 'host'; an aws-export spec always
    holds a 'profile'. Only labels ever appear on a resolver's argv, which
    is why they are ordinary strings rather than anything protected: none
    of them is a secret value.
    """

    name: str
    source: str
    labels: Mapping[str, str]


@dataclass(frozen=True)
class ResolvedCredential:
    """One spec plus the value its source produced on the host.

    `value` is the secret. For the git source it is the password git's
    credential helper returned (the username travels separately, in
    `username`, because git needs both and an environment variable needs
    only the password); for the aws-export source it is the raw JSON
    document the aws CLI printed, parsed again by `render_env_fragment`
    into the three AWS variables, and kept whole so the raw-document export
    and the expiry field survive; for the keychain source it is the stored
    password.

    `expires_at` is the ISO 8601 string the aws CLI's Expiration field
    carried, or None for the sources that do not expire. It is stored as
    the original string, not a parsed datetime, so the fragment renderer
    sees exactly what the CLI printed.
    """

    spec: CredentialSpec
    value: str
    username: str | None
    expires_at: str | None


# The Runner every resolver is handed: given the full argv and an optional
# stdin document, return a completed process. Injected rather than called
# internally via `subprocess.run` directly, so every test substitutes a
# fake runner instead of patching this module -- the same seam
# `devcontainer_config.catalog` defines and its suite exercises.
Runner = Callable[[Sequence[str], "str | None"], subprocess.CompletedProcess[str]]


def subprocess_runner(argv: Sequence[str], stdin: str | None) -> subprocess.CompletedProcess[str]:
    """The production Runner: a real subprocess, fed `stdin` on its stdin.

    This is what a caller outside the test suite passes a resolver. The
    resolvers themselves never import or call `subprocess.run`, so nothing
    here needs patching to be tested hermetically. Decoding is pinned to
    UTF-8, errors left strict: a secret must not fail through a
    locale-dependent UnicodeDecodeError path, so the child's bytes decode
    the same way on every host regardless of the ambient locale.
    """
    return subprocess.run(
        list(argv), input=stdin, capture_output=True, text=True, encoding="utf-8", check=False
    )


# ---------------------------------------------------------------------------
# The manifest (load_manifest and its validators). Everything above this
# point is vocabulary; everything below reads and checks the one file the
# whole mechanism is driven by, collecting every problem in one pass.
# ---------------------------------------------------------------------------


def manifest_path(root: Path) -> Path:
    """The manifest's location under `root`: .devcontainer/hostcreds.map.json.

    The single definition of the path, so `load_manifest`, the missing-file
    diagnostic and the later CLI wiring all agree on where the file lives
    instead of each recomposing it. Takes `root` as data rather than
    discovering the checkout (no `repo.find_root`, no subprocess), so a
    test points it at a temporary directory.
    """
    return root / DEVCONTAINER_DIRECTORY / MANIFEST_FILENAME


def _missing_manifest_message(path: Path) -> str:
    example_path = path.parent / MANIFEST_EXAMPLE_FILENAME
    return (
        f"ERROR: no hostcreds manifest at {path}\n"
        "Every credential 'make push-creds' resolves is named there.\n"
        f"Copy {example_path} to {path} and edit it for this checkout, "
        "then retry."
    )


def _unreadable_manifest_message(path: Path, reason: str) -> str:
    return (
        f"ERROR: cannot read the hostcreds manifest at {path}\n"
        f"The read failed: {reason}.\n"
        "Fix the file's permissions or path, then retry."
    )


def _unparseable_manifest_message(path: Path, detail: str) -> str:
    example_path = path.parent / MANIFEST_EXAMPLE_FILENAME
    return (
        f"ERROR: cannot parse the hostcreds manifest at {path}\n"
        f"The file is not valid JSON: {detail}.\n"
        f"Compare {example_path}, which shows the expected shape, and fix "
        "the JSON syntax, then retry."
    )


def _non_object_manifest_message(path: Path) -> str:
    example_path = path.parent / MANIFEST_EXAMPLE_FILENAME
    return (
        f"ERROR: the hostcreds manifest at {path} is not a JSON object\n"
        "The top level must map a credential name to an object with a "
        "'source' key and optional labels.\n"
        f"Compare {example_path}, which shows the expected shape."
    )


def _problems_manifest_message(path: Path, problems: Sequence[str]) -> str:
    example_path = path.parent / MANIFEST_EXAMPLE_FILENAME
    listed = "\n".join(f"- {problem}" for problem in problems)
    return (
        f"ERROR: {len(problems)} problem(s) in the hostcreds manifest at {path}\n"
        f"{listed}\n"
        f"Fix every problem listed (the committed example {example_path} "
        "shows the expected shape), then run 'make push-creds' again."
    )


def _invalid_name_problem(position: int, name: str) -> str:
    return (
        f"entry {position} (name {name!r}): the name must match "
        f"[A-Z][A-Z0-9_]* -- a name becomes both a shell variable and a "
        f"<NAME>.env fragment filename, and the store directory is "
        f"case-insensitive on macOS, where differently-cased names would "
        f"collide as filenames"
    )


def _reserved_name_problem(name: str) -> str:
    return (
        f"{name}: the name is reserved -- it is a shell or loader variable "
        f"(PATH, LD_PRELOAD, DYLD_INSERT_LIBRARIES) or one of the AWS "
        f"environment variables an aws-export fragment exports, and a "
        f"credential with this name would silently shadow the real one at "
        f"shell startup; choose a name outside the reserved set"
    )


def _non_object_entry_problem(name: str) -> str:
    return (
        f"{name}: the entry must be a JSON object with a 'source' key and "
        f"optional labels, not a bare string, number or list"
    )


def _missing_source_problem(name: str) -> str:
    allowed = ", ".join(_ALLOWED_LABEL_KEYS)
    return f"{name}: no 'source' key; one of {allowed} is required"


def _unknown_source_problem(name: str, source: str) -> str:
    allowed = ", ".join(_ALLOWED_LABEL_KEYS)
    return f"{name}: the source {source!r} is not one of: {allowed}"


def _non_string_label_problem(name: str, key: str) -> str:
    return f"{name}: the '{key}' label must be a non-empty string"


def _unsafe_label_problem(name: str, key: str) -> str:
    return (
        f"{name}: the '{key}' label must not contain a newline, carriage "
        f"return or quote -- labels are written into fragment comment text "
        f"and passed on argv, where a newline would escape the line the "
        f"label was written onto and a quote would end the quoting around it"
    )


def _unknown_label_problem(name: str, source: str, key: str) -> str:
    allowed = ", ".join(sorted(_ALLOWED_LABEL_KEYS[source]))
    return (
        f"{name}: the label '{key}' is not valid for the {source} source "
        f"(a typo?); allowed labels: {allowed}"
    )


def _missing_host_problem(name: str) -> str:
    return (
        f"{name}: the git source requires a 'host' label naming the host "
        f"its credential is stored for"
    )


def _invalid_host_problem(name: str, host: str) -> str:
    return (
        f"{name}: 'host' must be a bare hostname, not {host!r} -- no "
        f"scheme, no slash, no path, no port, no user part"
    )


def _spec_from_entry(
    name: str, entry: object, project: str
) -> tuple[CredentialSpec | None, list[str]]:
    """Validate one manifest entry and build its spec, defaults applied.

    Returns the spec with no problems, or None plus every problem found in
    the entry -- both, never a half-valid spec -- because `load_manifest`
    reports all problems across all entries in one `ManifestError`, and a
    spec whose labels half-passed validation would let a later resolver
    run on labels the operator never intended.

    `project` is the repository directory's basename (the caller derives
    it from the `root` parameter; nothing here shells out to git), used
    only for the default keychain service.
    """
    if not isinstance(entry, dict):
        return None, [_non_object_entry_problem(name)]
    source = entry.get("source")
    if not isinstance(source, str):
        return None, [_missing_source_problem(name)]
    if source not in _ALLOWED_LABEL_KEYS:
        return None, [_unknown_source_problem(name, source)]

    problems: list[str] = []
    labels: dict[str, str] = {}
    for key, value in entry.items():
        if key == "source":
            continue
        if key not in _ALLOWED_LABEL_KEYS[source]:
            problems.append(_unknown_label_problem(name, source, key))
            continue
        if not isinstance(value, str) or not value:
            problems.append(_non_string_label_problem(name, key))
            continue
        if _UNSAFE_LABEL_CHARACTERS.intersection(value):
            problems.append(_unsafe_label_problem(name, key))
            continue
        labels[key] = value

    if source == SOURCE_KEYCHAIN:
        # The default service names this project and this credential, so
        # push-creds creates and reads items in a namespace no other
        # checkout's default collides with; an explicit 'service' label
        # overrides it for credentials that already live somewhere else.
        labels.setdefault(KEYCHAIN_SERVICE_LABEL, f"{KEYCHAIN_SERVICE_PREFIX}/{project}/{name}")
    elif source == SOURCE_GIT:
        host = labels.get(GIT_HOST_LABEL)
        if host is None:
            problems.append(_missing_host_problem(name))
        elif _HOSTNAME_PATTERN.fullmatch(host) is None:
            problems.append(_invalid_host_problem(name, host))
    else:
        # The default profile matches the aws CLI's own, so a manifest
        # entry with no 'profile' label and a bare 'aws configure
        # export-credentials' read the same profile.
        labels.setdefault(AWS_PROFILE_LABEL, DEFAULT_AWS_PROFILE)

    if problems:
        return None, problems
    return CredentialSpec(name=name, source=source, labels=labels), []


def load_manifest(root: Path) -> tuple[CredentialSpec, ...]:
    """Every validated credential spec named by `root`'s manifest, in file order.

    Reads `.devcontainer/hostcreds.map.json` (the location `manifest_path`
    defines). The file must be a JSON object mapping a credential name to
    an entry with a 'source' key and optional per-source labels; every
    default is applied here, so a returned spec's labels are always
    complete. An empty object is valid and yields an empty tuple: it is
    the explicit statement that this checkout pushes no host credentials,
    as distinct from the file being absent entirely, which is an error.

    Fail-fast and complete: every problem in the file -- an invalid or
    reserved name, an unknown source, a missing or invalid label, an
    unknown label key -- is collected into one `ManifestError` listing
    them all, each naming the offending entry, rather than the first
    problem aborting the rest of the file's validation.

    Raises:
        ManifestError: the manifest is missing, unreadable, not valid
            JSON, not a JSON object, or any entry is invalid (the message
            lists every problem at once, each naming its entry).
    """
    path = manifest_path(root)
    if not path.is_file():
        raise ManifestError(_missing_manifest_message(path))
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ManifestError(_unreadable_manifest_message(path, str(exc.strerror or exc))) from exc
    try:
        payload: object = json.loads(text)
    except json.JSONDecodeError as exc:
        detail = f"{exc.msg} at line {exc.lineno}"
        raise ManifestError(_unparseable_manifest_message(path, detail)) from exc
    if not isinstance(payload, dict):
        raise ManifestError(_non_object_manifest_message(path))

    project = root.name
    problems: list[str] = []
    specs: list[CredentialSpec] = []
    for position, (name, entry) in enumerate(payload.items(), start=1):
        if _CREDENTIAL_NAME_PATTERN.fullmatch(name) is None:
            problems.append(_invalid_name_problem(position, name))
            continue
        if name in _RESERVED_CREDENTIAL_NAMES:
            problems.append(_reserved_name_problem(name))
            continue
        spec, entry_problems = _spec_from_entry(name, entry, project)
        problems.extend(entry_problems)
        if spec is not None:
            specs.append(spec)
    if problems:
        raise ManifestError(_problems_manifest_message(path, problems))
    return tuple(specs)


# ---------------------------------------------------------------------------
# The three resolvers. Each builds exactly one argv (labels only), hands it
# to the injected runner, and turns a failure into a ResolutionError that
# quotes stderr and never stdout.
# ---------------------------------------------------------------------------


def _missing_binary_message(name: str, source: str, binary: str) -> str:
    return (
        f"ERROR: cannot resolve {name} from the {source} source\n"
        f"The {binary!r} binary is not on PATH, so the source command could "
        f"not run at all.\n"
        f"Install {binary!r} on the host and ensure it is on PATH, then run "
        f"'make push-creds' again."
    )


def _command_failed_message(
    name: str, source: str, argv: Sequence[str], returncode: int, stderr: str
) -> str:
    diagnostic = f"\nThe command's stderr: {stderr!r}" if stderr else ""
    return (
        f"ERROR: cannot resolve {name} from the {source} source\n"
        f"The command '{shlex.join(argv)}' exited with status {returncode}."
        f"{diagnostic}\n"
        f"The command's stdout is not repeated here: it can carry the "
        f"secret this call was made to fetch.\n"
        f"Re-run '{shlex.join(argv)}' on the host to see the failure "
        f"directly, fix it, then run 'make push-creds' again."
    )


def _empty_value_message(name: str, source: str, argv: Sequence[str], remedy: str) -> str:
    return (
        f"ERROR: cannot resolve {name} from the {source} source\n"
        f"The command '{shlex.join(argv)}' succeeded but produced an empty "
        f"value, which is never treated as a resolved credential.\n"
        f"{remedy}"
    )


def _malformed_output_message(name: str, source: str, argv: Sequence[str], reason: str) -> str:
    return (
        f"ERROR: cannot resolve {name} from the {source} source\n"
        f"The command '{shlex.join(argv)}' succeeded but its output is not "
        f"usable: {reason}.\n"
        f"The command's stdout is not repeated here: it can carry the "
        f"secret this call was made to fetch.\n"
        f"Re-run '{shlex.join(argv)}' on the host to see the raw output, "
        f"then run 'make push-creds' again."
    )


def _invoke(
    name: str, source: str, argv: Sequence[str], stdin: str | None, runner: Runner
) -> subprocess.CompletedProcess[str]:
    """Run `argv` through the injected runner and translate any failure.

    The one place every resolver's subprocess handling passes through, so
    the translation from a missing binary or a non-zero exit to a
    `ResolutionError` quoting stderr (never stdout) exists once, not once
    per resolver.
    """
    try:
        result = runner(argv, stdin)
    except FileNotFoundError as exc:
        raise ResolutionError(_missing_binary_message(name, source, argv[0])) from exc
    if result.returncode != 0:
        raise ResolutionError(
            _command_failed_message(name, source, argv, result.returncode, result.stderr)
        )
    return result


def _stdout_value(stdout: str) -> str:
    """The resolved value: `stdout` minus exactly the one trailing newline.

    Each source command prints the value followed by a line terminator;
    removing exactly one trailing newline (rather than rstripping all of
    them) keeps the removal from eating characters that are genuinely part
    of a value ending in newline bytes.
    """
    return stdout[:-1] if stdout.endswith("\n") else stdout


def _require_label(spec: CredentialSpec, key: str) -> str:
    """The label `key` from `spec`, or a named failure if it is absent.

    A spec returned by `load_manifest` always carries its source's labels
    (defaults applied), so an absent label means a hand-built spec reached
    a resolver half-configured; failing fast with the spec's name beats a
    KeyError the caller cannot act on.
    """
    value = spec.labels.get(key)
    if value is None:
        raise HostCredsError(
            f"ERROR: cannot resolve {spec.name}\n"
            f"The spec's labels have no '{key}' entry; load_manifest "
            f"applies every default, so a spec missing this label was "
            f"built by hand. Build it through load_manifest, or add the "
            f"label, then retry."
        )
    return value


def resolve_keychain(spec: CredentialSpec, runner: Runner) -> ResolvedCredential:
    """The keychain password for `spec`, from `security find-generic-password -w`.

    Only labels appear on the argv: the service (and the account, only
    when the manifest set one -- the flag is omitted entirely otherwise,
    not passed empty, because an empty `-a` matches a different item than
    no account constraint at all). The password arrives on stdout, is
    stripped of the CLI's one trailing newline, and is never placed in any
    argument or message.

    Raises:
        HostCredsError: `spec` carries no 'service' label.
        ResolutionError: `security` is not on PATH; exits non-zero (the
            message quotes its stderr); or prints an empty value.
    """
    service = _require_label(spec, KEYCHAIN_SERVICE_LABEL)
    argv = [SECURITY_EXECUTABLE, "find-generic-password", "-w", "-s", service]
    account = spec.labels.get(KEYCHAIN_ACCOUNT_LABEL)
    if account is not None:
        argv += ["-a", account]
    result = _invoke(spec.name, spec.source, argv, None, runner)
    value = _stdout_value(result.stdout)
    if not value:
        raise ResolutionError(
            _empty_value_message(
                spec.name,
                spec.source,
                argv,
                "The keychain item exists but holds an empty password; "
                "store a non-empty value in it, then run 'make push-creds' again.",
            )
        )
    return ResolvedCredential(spec=spec, value=value, username=None, expires_at=None)


def _parse_git_fill_output(name: str, argv: Sequence[str], stdout: str) -> dict[str, str]:
    """The key=value attributes `git credential fill` printed, up to its blank line.

    git terminates the attribute list with one blank line; anything after
    it is not an attribute, so parsing stops there. Before that
    terminator the protocol is key=value lines only: a non-empty line
    without '=' is malformed output and raises rather than being skipped,
    because silently skipping one would truncate a password containing
    raw newlines into a plausible-looking but wrong value.

    Raises:
        ResolutionError: a line before the blank terminator carries no
            '=' (the offending line is never quoted in the message: the
            output can carry the password).
    """
    fields: dict[str, str] = {}
    for line in stdout.splitlines():
        if not line:
            break
        key, separator, value = line.partition("=")
        if not separator:
            raise ResolutionError(
                _malformed_output_message(
                    name,
                    SOURCE_GIT,
                    argv,
                    "the answer has a line without '=' before the blank terminator",
                )
            )
        fields[key] = value
    return fields


def resolve_git(spec: CredentialSpec, runner: Runner) -> ResolvedCredential:
    """The password git's credential helper holds for `spec`'s host.

    The host never appears on the argv: it is fed to `git credential fill`
    on stdin as the credential description (`protocol=https`, the host,
    then the blank line that ends the query), so the process table never
    names which host a developer has credentials for either. The answer
    arrives on stdout as `key=value` lines; the password becomes `value`
    and the username, when git returned one, becomes `username` (git
    needs both to match a stored credential; an environment variable
    needs only the password).

    Raises:
        HostCredsError: `spec` carries no 'host' label.
        ResolutionError: `git` is not on PATH; the command exits non-zero
            (the message quotes its stderr, never the stdout that may
            carry the password); the answer carries a line without '='
            before the blank terminator; the answer has no password line;
            or the password line is empty.
    """
    host = _require_label(spec, GIT_HOST_LABEL)
    argv = [GIT_EXECUTABLE, "credential", "fill"]
    stdin = f"protocol=https\nhost={host}\n\n"
    result = _invoke(spec.name, spec.source, argv, stdin, runner)
    fields = _parse_git_fill_output(spec.name, argv, result.stdout)
    password = fields.get("password")
    if password is None or not password:
        raise ResolutionError(
            _malformed_output_message(
                spec.name,
                spec.source,
                argv,
                f"the answer has no password line for host {host!r}",
            )
        )
    return ResolvedCredential(
        spec=spec,
        value=password,
        username=fields.get("username"),
        expires_at=None,
    )


def _aws_export_payload(name: str, argv: Sequence[str], stdout: str) -> dict[str, object]:
    """The JSON object `aws configure export-credentials` printed for one call.

    The resolver-side json boundary: exit-zero output that is not a JSON
    object raises a named ResolutionError here instead of a bare
    `json.JSONDecodeError` escaping. (`render_env_fragment` re-parses the
    stored document through its own boundary, raising HostCredsError: by
    then the resolution succeeded and a damaged store file is a render
    problem, not a source-command failure.) The output itself is never
    repeated in any message: it is the secret.
    """
    try:
        payload: object = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise _aws_payload_error(name, argv, "the output is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise _aws_payload_error(name, argv, "the output is not a JSON object")
    return payload


def _aws_payload_error(name: str, argv: Sequence[str], reason: str) -> ResolutionError:
    """The aws-export resolution failure for unusable output (not a failed exit).

    Raised via `from exc` at this module's `json` boundaries, like every
    other boundary here: the chain keeps the decoder's detail in the
    traceback for debugging, while the rendered message names the failure
    without ever repeating the output itself.
    """
    return ResolutionError(_malformed_output_message(name, SOURCE_AWS_EXPORT, argv, reason))


def resolve_aws_export(spec: CredentialSpec, runner: Runner) -> ResolvedCredential:
    """The aws session credentials for `spec`'s profile, as the CLI's JSON document.

    Only the profile (a label) appears on the argv. The CLI prints a JSON
    object with AccessKeyId and SecretAccessKey (always), SessionToken and
    Expiration (session credentials; a static access key has neither); the
    whole document becomes `value` -- `render_env_fragment` parses it into
    the three AWS variables -- and the Expiration string, when present,
    becomes `expires_at`. A document missing either required field, or not
    parseable at all, is a resolution failure, not a partial result.

    Raises:
        ResolutionError: `aws` is not on PATH; the command exits non-zero
            (the message quotes its stderr); the output is empty, not
            valid JSON, not a JSON object, or missing AccessKeyId or
            SecretAccessKey.
    """
    profile = _require_label(spec, AWS_PROFILE_LABEL)
    argv = [AWS_EXECUTABLE, "configure", "export-credentials", "--profile", profile]
    result = _invoke(spec.name, spec.source, argv, None, runner)
    value = _stdout_value(result.stdout)
    if not value:
        raise ResolutionError(
            _empty_value_message(
                spec.name,
                spec.source,
                argv,
                f"Run 'aws sso login --profile {profile}' (or export static "
                f"credentials for that profile), then run 'make push-creds' again.",
            )
        )
    payload = _aws_export_payload(spec.name, argv, value)
    for field in (AWS_ACCESS_KEY_FIELD, AWS_SECRET_KEY_FIELD):
        field_value = payload.get(field)
        if not isinstance(field_value, str) or not field_value:
            raise ResolutionError(
                _malformed_output_message(
                    spec.name, spec.source, argv, f"the JSON object has no '{field}' field"
                )
            )
    expiration = payload.get(AWS_EXPIRATION_FIELD)
    if expiration is not None and not isinstance(expiration, str):
        raise ResolutionError(
            _malformed_output_message(
                spec.name, spec.source, argv, f"the '{AWS_EXPIRATION_FIELD}' field is not a string"
            )
        )
    return ResolvedCredential(
        spec=spec,
        value=value,
        username=None,
        expires_at=expiration,
    )


# The per-source resolvers, dispatched by `resolve`. Declared once here so
# the mapping from a source name to its resolver exists once; a source
# outside the three `load_manifest` validates against is absent from the
# mapping, which is exactly how the unknown-source failure detects it.
_SOURCE_RESOLVERS: Mapping[str, Callable[[CredentialSpec, Runner], ResolvedCredential]] = {
    SOURCE_KEYCHAIN: resolve_keychain,
    SOURCE_GIT: resolve_git,
    SOURCE_AWS_EXPORT: resolve_aws_export,
}


def _unknown_source_message(name: str, source: str, operation: str) -> str:
    """The unknown-source failure text shared by `resolve` and `render_env_fragment`.

    Both call sites used to hand-roll near-identical text that had already
    drifted; this is the one copy, parameterized by the operation that
    cannot proceed ("resolve", "render the fragment for").
    """
    allowed = ", ".join(_ALLOWED_LABEL_KEYS)
    return (
        f"ERROR: cannot {operation} {name}\n"
        f"The spec's source {source!r} is not one of: {allowed}. "
        f"load_manifest rejects unknown sources, so a spec carrying one "
        f"was built by hand; use one of the three sources, then retry."
    )


def resolve(spec: CredentialSpec, runner: Runner) -> ResolvedCredential:
    """The credential for `spec`, dispatched to its source's resolver.

    The single dispatch point a later unit's push-creds wiring calls per
    manifest entry, reading `_SOURCE_RESOLVERS` so the mapping from a
    source name to a resolver exists once. A source outside the three
    `load_manifest` validates against cannot arrive from a manifest;
    reaching this raise means a hand-built spec, and the message says so.

    Raises:
        HostCredsError: `spec.source` is not one of the three sources.
        ResolutionError: the source's command failed (see the resolver
            for the source's specific conditions).
    """
    resolver = _SOURCE_RESOLVERS.get(spec.source)
    if resolver is None:
        raise HostCredsError(_unknown_source_message(spec.name, spec.source, "resolve"))
    return resolver(spec, runner)


# ---------------------------------------------------------------------------
# The renderers. Both are pure functions of their inputs -- deterministic,
# same text for same input -- because a caller detects a prior application
# by searching for MARKER / FRAGMENT_MARKER, which only works if rendering
# twice is byte-identical.
# ---------------------------------------------------------------------------

# Present once per rendered startup block, at the top: the idempotence
# anchor a caller greps for before appending the block a second time, the
# same pattern `devcontainer_config.shellrc.MARKER` established.
MARKER = "# hostcreds-credential-startup-block"

# The first line of every rendered fragment: identifies the file as
# hostcreds-written (a hand-edited or foreign file in the store directory
# is then distinguishable at a glance) and gives later tooling one stable
# line to detect fragments by.
FRAGMENT_MARKER = "# hostcreds-credential-fragment"

_STORE_DIR_TOKEN = "__HOSTCREDS_STORE_DIR__"

# One shared template, the store directory's name confined to a single
# token substituted with plain `str.replace` in `render_startup_block`.
# The block's shell text is full of '$' expansions and a here-document
# body; formatting it with `str.format` would mean naming and escaping
# every brace-shaped piece instead of substituting the one thing that
# actually varies. (The template is a plain, non-raw f-string on purpose:
# `MARKER` interpolates at module load, and the one backslash sequence the
# shell text needs -- printf's '%s\n' -- is spelled '\\n' so the rendered
# text carries a literal backslash-n, exactly what printf wants.)
#
# The block's shape is driven by one measured constraint: in zsh, a `for`
# word list whose glob matches nothing does not merely skip the loop, it
# prints 'no matches found' and aborts the rest of the script (verified:
# `zsh -c 'for f in /empty/*.env; do :; done; echo after'` never echoes
# 'after' and exits 1), while bash passes the unmatched pattern through
# literally. So the file list is acquired inside a command substitution
# whose enclosing brace group redirects stderr to /dev/null: in zsh the
# swallowed nomatch diagnostic leaves the substitution empty and the
# script running; in bash the literal pattern reaches the `[ -e ... ]`
# guard and is skipped. The loop itself is a `while read` over a here-
# document -- never a pipeline, whose `while` half would run in a subshell
# and strand every fragment's exports there -- so sourcing happens in the
# caller's own shell, and the fragments' own stderr (their expiry
# notices) passes through untouched: only the glob probe is silenced.
_BLOCK_TEMPLATE = f"""\
{MARKER}
# Sources every hostcreds credential fragment under "$HOME/{_STORE_DIR_TOKEN}/"
# (written there by 'make push-creds' on the host). Each fragment exports its
# credential's value through the export builtin -- no new process ever carries
# a value in its argv -- or records that a git-source credential was stored in
# ~/.git-credentials instead. Fragments print their own expiry notices on
# stderr; this block itself never prints anything, because a missing store
# directory, or an empty one, is the ordinary state of a checkout whose
# credentials have not been pushed yet, and shell startup must stay silent and
# non-fatal there. No value is ever echoed.
if [ -d "$HOME/{_STORE_DIR_TOKEN}" ]; then
  {{ __hostcreds_fragments="$(printf '%s\\n' "$HOME"/{_STORE_DIR_TOKEN}/*.env)"; }} 2>/dev/null
  while IFS= read -r __hostcreds_fragment; do
    [ -e "$__hostcreds_fragment" ] || continue
    . "$__hostcreds_fragment" || :
  done <<__HOSTCREDS_LIST__
$__hostcreds_fragments
__HOSTCREDS_LIST__
  unset __hostcreds_fragments __hostcreds_fragment
fi
"""


def _validate_store_dir_name(store_dir_name: str) -> None:
    """Reject a store directory name that cannot be interpolated into shell text.

    The name is embedded unquoted into the block's glob (outside the
    quoted "$HOME" prefix), so a space would split the word, a quote
    would terminate the string, and a slash would leave the store under
    $HOME entirely ('../x' would point at another user-writable area);
    '.' and '..' are rejected for the same traversal reason even though
    the pattern alone admits them.
    """
    if _STORE_DIR_NAME_PATTERN.fullmatch(store_dir_name) is None or store_dir_name in {".", ".."}:
        raise HostCredsError(
            f"ERROR: invalid hostcreds store directory name {store_dir_name!r}\n"
            f"The name must be a single directory name under $HOME: letters, "
            f"digits, dots, hyphens and underscores, and neither '.' nor '..'.\n"
            f"Pass a plain directory name (the default is "
            f"{DEFAULT_STORE_DIR_NAME!r}), then retry."
        )


def render_startup_block(store_dir_name: str = DEFAULT_STORE_DIR_NAME) -> str:
    """The deterministic shell-startup block text for `store_dir_name`.

    POSIX-sh compatible: the same text works sourced into bash and zsh
    startup files alike. Sourcing loop order is the glob's sorted order in
    both shells, so fragment application order is stable. The block never
    prints anything itself, never redirects fragment output (only the
    internal glob probe is stderr-silenced), and contains no construct
    that could abort the shell it is sourced into: no `exit`, no `return`,
    no `set -e`, and every command whose failure is expected on a quiet
    checkout is either guarded or fails inside a silenced probe.

    Deterministic: the same arguments always render the same text, which
    is what lets a caller detect a prior application by searching for
    `MARKER` instead of re-deriving idempotence from scratch.

    Raises:
        HostCredsError: `store_dir_name` is not a single safe directory
            name (empty, '.', '..', or containing a separator, space,
            quote or other character outside letters, digits, dots,
            hyphens and underscores).
    """
    _validate_store_dir_name(store_dir_name)
    return _BLOCK_TEMPLATE.replace(_STORE_DIR_TOKEN, store_dir_name)


def _sh_single_quote(value: str) -> str:
    """`value` wrapped in single quotes, POSIX-safe for any content.

    The standard shell-escaping idiom: single quotes preserve every
    character verbatim except the single quote itself, which is closed,
    escaped and reopened (`'` becomes `'\''`). Dollar signs, backticks,
    double quotes, backslashes, newlines and tabs all survive a round
    trip through the fragment file byte for byte, which the end-to-end
    tests prove by sourcing a rendered hostile-value fragment for real.
    """
    return "'" + value.replace("'", "'\\''") + "'"


def _expiry_epoch(expires_at: str, name: str) -> int:
    """The whole-second epoch `expires_at` denotes, for the expiry guard.

    A naive timestamp (no offset) is read as UTC, not as local time: the
    aws CLI always prints an offset, so a naive string only reaches this
    function through a hand-built credential, and assuming UTC keeps the
    guard's verdict from depending on the container's timezone.

    Raises:
        HostCredsError: `expires_at` is not an ISO 8601 timestamp
            `datetime.fromisoformat` can parse; the message names the
            credential whose fragment cannot be rendered (never the
            value -- the timestamp is metadata, not the secret).
    """
    try:
        moment = datetime.fromisoformat(expires_at)
    except ValueError as exc:
        raise HostCredsError(
            f"ERROR: cannot render the fragment for {name}\n"
            f"The credential's expires_at value {expires_at!r} is not an "
            f"ISO 8601 timestamp this module can parse.\n"
            f"Re-run 'make push-creds' so the expiry is read fresh from "
            f"the aws CLI, then retry."
        ) from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return int(moment.timestamp())


def _expiry_guarded(header: list[str], actions: list[str], name: str, epoch: int) -> list[str]:
    """The fragment lines with `actions` wrapped in the expiry guard.

    The header (marker and explanation comments) stays outside the guard
    so the file's identification lines are unconditional. Inside, the
    exports run only while the wall clock is strictly below the epoch; on
    or past it, one notice line goes to stderr naming the refresh command,
    nothing is exported, and the fragment still does not abort the shell:
    a failing `date` yields an empty string, the `[ ... -lt ... ]` test
    errors to status 2, the `if` takes that as false, and the notice
    branch runs instead.
    """
    return [
        *header,
        "# The credential this fragment carries expires; the guard compares that",
        "# expiry against the wall clock so an expired credential is never",
        "# silently exported. An expired fragment prints one notice line on",
        "# stderr naming the refresh command, and never aborts the shell.",
        f'if [ "$(date +%s)" -lt {epoch} ]; then',
        *(f"  {action}" for action in actions),
        "else",
        f"  printf '%s\\n' \"notice: {name} expired; refresh with: make push-creds\" >&2",
        "fi",
    ]


def _plain_fragment_lines(spec: CredentialSpec, value: str) -> tuple[list[str], list[str]]:
    """The header and export lines for a directly-exported credential.

    Used for the keychain source: one export under the manifest's own
    name, the value single-quote-escaped, nothing else.
    """
    header = [
        FRAGMENT_MARKER,
        f"# {spec.name}: exported by the hostcreds shell-startup block. 'make push-creds'",
        f"# resolved this credential from the {spec.source} source on the host and wrote this",
        "# fragment; the value below is the only place it is ever written down, and nothing",
        "# else ever echoes, prints or logs it.",
    ]
    actions = [f"export {spec.name}={_sh_single_quote(value)}"]
    return header, actions


def _git_fragment_lines(spec: CredentialSpec) -> list[str]:
    """The comment-only fragment lines for a git-source credential.

    No export line exists here on purpose: the password was stored in
    ~/.git-credentials by the host-side push, and git reads it through its
    own credential helper when it needs it. Copying it into an environment
    variable as well would only widen its exposure to every process in
    the container without giving git anything it does not already have.
    The file still exists (rather than no fragment at all) so the store
    directory inventories every resolved credential and a later listing
    or cleanup pass can find them uniformly.
    """
    return [
        FRAGMENT_MARKER,
        f"# {spec.name}: stored as a git credential (written to ~/.git-credentials by",
        "# 'make push-creds' on the host). No value is exported here: git reads the",
        "# credential through its own credential helper when it needs it, and copying it",
        "# into an environment variable would only widen its exposure.",
    ]


def _aws_export_fragment_lines(spec: CredentialSpec, value: str) -> tuple[list[str], list[str]]:
    """The header and export lines for an aws-export credential.

    Parses `value` as the JSON document `resolve_aws_export` validated on
    the host and exports the three standard AWS variables, so every AWS
    SDK and the aws CLI in the container pick the session up without any
    configuration of their own. SessionToken is omitted when the document
    has none (a static access key) rather than exported empty, which some
    SDKs treat as a malformed session.

    When the credential's own name is not one of the three AWS variables,
    the raw JSON document is additionally exported under that name: the
    document is the only place the session's Expiration field travels,
    and tooling that wants to warn before expiry needs to read it without
    re-running the aws CLI (which the container cannot do -- resolving is
    host-side by design). When the name is one of the three, the plain
    export is skipped: it would duplicate (or clobber) a parsed export of
    the same variable.

    Raises:
        HostCredsError: `value` is not a JSON object, or lacks a
            non-empty string AccessKeyId or SecretAccessKey -- the same
            check the resolver applies, so a store file damaged after the
            push cannot render `export AWS_ACCESS_KEY_ID=''`; this names
            the refresh command.
    """
    try:
        payload: object = json.loads(value)
    except json.JSONDecodeError as exc:
        raise HostCredsError(
            f"ERROR: cannot render the fragment for {spec.name}\n"
            f"The credential's value is not the JSON document "
            f"'aws configure export-credentials' prints; it may have been "
            f"damaged after the push.\n"
            f"Re-run 'make push-creds' so the fragment is written fresh "
            f"from a real resolution, then retry."
        ) from exc
    if not isinstance(payload, dict):
        raise HostCredsError(
            f"ERROR: cannot render the fragment for {spec.name}\n"
            f"The credential's value is not the JSON object "
            f"'aws configure export-credentials' prints; it may have been "
            f"damaged after the push.\n"
            f"Re-run 'make push-creds' so the fragment is written fresh "
            f"from a real resolution, then retry."
        )
    access_key = payload.get(AWS_ACCESS_KEY_FIELD)
    secret_key = payload.get(AWS_SECRET_KEY_FIELD)
    if (
        not isinstance(access_key, str)
        or not access_key
        or not isinstance(secret_key, str)
        or not secret_key
    ):
        raise HostCredsError(
            f"ERROR: cannot render the fragment for {spec.name}\n"
            f"The credential's JSON document has no non-empty string "
            f"'{AWS_ACCESS_KEY_FIELD}' or '{AWS_SECRET_KEY_FIELD}' field.\n"
            f"Re-run 'make push-creds' so the fragment is written fresh "
            f"from a real resolution, then retry."
        )
    profile = spec.labels.get(AWS_PROFILE_LABEL, DEFAULT_AWS_PROFILE)
    header = [
        FRAGMENT_MARKER,
        f"# {spec.name}: AWS session credentials for profile {profile!r}, resolved on the",
        "# host by 'make push-creds' from 'aws configure export-credentials' and exported",
        "# here as the three standard AWS variables so every SDK picks them up.",
    ]
    actions = [
        f"export {AWS_ACCESS_KEY_ID_VAR}={_sh_single_quote(access_key)}",
        f"export {AWS_SECRET_ACCESS_KEY_VAR}={_sh_single_quote(secret_key)}",
    ]
    session_token = payload.get(AWS_SESSION_TOKEN_FIELD)
    if isinstance(session_token, str) and session_token:
        actions.append(f"export {AWS_SESSION_TOKEN_VAR}={_sh_single_quote(session_token)}")
    if spec.name not in _AWS_ENVIRONMENT_VARS:
        header += [
            "# The raw JSON document is exported under the manifest's own name as well, so",
            "# tooling can read the session's expiry without re-running the aws CLI (which",
            "# only the host can do).",
        ]
        actions.append(f"export {spec.name}={_sh_single_quote(value)}")
    return header, actions


# The per-source fragment renderers, dispatched by `render_env_fragment`.
# Each maps a resolved credential to its (header, action) line pairs; the
# git source carries no actions (its fragment is comment-only, see
# `_git_fragment_lines`) and the mapping exists so the source-to-renderer
# wiring is declared once, next to `_SOURCE_RESOLVERS`, instead of once per
# if-chain.
_SOURCE_FRAGMENT_RENDERERS: Mapping[
    str, Callable[[ResolvedCredential], tuple[list[str], list[str]]]
] = {
    SOURCE_KEYCHAIN: lambda credential: _plain_fragment_lines(credential.spec, credential.value),
    SOURCE_GIT: lambda credential: (_git_fragment_lines(credential.spec), []),
    SOURCE_AWS_EXPORT: lambda credential: _aws_export_fragment_lines(
        credential.spec, credential.value
    ),
}


def render_env_fragment(credential: ResolvedCredential) -> str:
    """The per-credential fragment text, newline-terminated.

    Every fragment starts with `FRAGMENT_MARKER` and explains itself in
    comments; values appear only inside single-quote-escaped export
    assignments, never in a comment, an echo or a printf. The git source
    renders a comment-only file (its value lives in ~/.git-credentials,
    not the environment). An `expires_at` wraps the fragment's exports in
    the expiry guard (see `_expiry_guarded` for the guard's own failure
    behavior). Dispatch reads `_SOURCE_FRAGMENT_RENDERERS`, mirroring
    `resolve`, so the two source-to-function mappings live side by side.

    Deterministic: the same credential always renders the same text, so a
    caller detects a fragment's origin by its first line instead of
    re-deriving it.

    Raises:
        HostCredsError: the spec's source is not one of the three sources;
            an aws-export value is not a JSON object with the required
            fields; or an expires_at is not a parseable ISO 8601
            timestamp.
    """
    spec = credential.spec
    renderer = _SOURCE_FRAGMENT_RENDERERS.get(spec.source)
    if renderer is None:
        raise HostCredsError(
            _unknown_source_message(spec.name, spec.source, "render the fragment for")
        )
    header, actions = renderer(credential)
    if credential.expires_at is not None:
        lines = _expiry_guarded(
            header, actions, spec.name, _expiry_epoch(credential.expires_at, spec.name)
        )
    else:
        lines = [*header, *actions]
    return "\n".join(lines) + "\n"
