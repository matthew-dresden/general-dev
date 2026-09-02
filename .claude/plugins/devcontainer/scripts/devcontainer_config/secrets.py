"""The secret scanner: every detector named in spec Section 4.6, in one place.

Detects, in scanned line content: AWS access key identifiers (`AKIA` and
`ASIA` prefixes), AWS secret access key assignments, private key blocks,
GitHub tokens (`ghp_`, `gho_`, `github_pat_`), Slack tokens (`xoxb-`,
`xoxp-`), bearer tokens, SSO portal URLs, twelve-digit account identifiers,
EC2 instance identifiers, every catalog secret name, and any line also
present in the developer's own `shell.env` -- excluding a line that also
appears, verbatim, in both the git index and `HEAD` version of the tracked
`shell.env.example` (E2-F1-S1-T4): a line this commit does not introduce or
change. A line a developer stages into `shell.env.example` itself, whatever
its shape -- an assignment, a comment, anything -- has no counterpart in
`HEAD` yet, so it can never enter that exclusion set and can never blind the
detector for that same value leaking anywhere else in the same commit
(E2-F1-S1-T4 security review round 4).

There is no ignore list and no suppression mechanism (spec Section 4.6). A
finding is either real, and fixed, or a false positive, which needs human
approval per `CLAUDE.md`. Nothing in this module accepts an allowlist, an
inline suppression marker, or an environment variable that disables a
detector.

`scan_lines` is pure: it opens no file, spawns no subprocess and reaches no
network. Its only inputs are the lines handed to it and the `ScanSources`
value carrying the developer's `shell.env` lines and the catalog secret
names. Both sources are injected because the catalog client does not exist
until E3 and because the Scanner suite (spec Section 10.2) has to run with no
AWS, no docker and no network at all (AC-10.14).

Every finding also carries a rendering decision. The SSO portal URL, the
account identifier and the EC2 instance identifier are printable, exactly as
the worked report in spec Section 4.6 prints an `awsapps.com/start` URL in
full; every other detector is credential-bearing and is redacted:
`render_finding` emits only the detector-declared safe prefix of the match
plus the length of what it withheld, so a report stays actionable without
ever returning a full credential value.

`staged_paths`, `staged_blob`, `shell_env_lines` and `shell_env_example_lines`
(E2-F1-S1-T2, E2-F1-S1-T4) are the impure shell around `scan_lines`:
`staged_paths` and `staged_blob` read the git index, `shell_env_example_lines`
reads both the git index and `HEAD` (never the working tree -- only tracked
`shell.env.example` content can suppress a `shell-env-line` finding) and
keeps only the lines present in both, and `shell_env_lines` reads the
developer's gitignored `shell.env` from disk. Intersecting the index and
`HEAD` versions, rather than trusting either one alone or matching on line
shape, means a developer who stages a real, filled-in value into
`shell.env.example` itself cannot suppress that same value leaking anywhere
else in the same commit: that value has no counterpart in `HEAD` yet (it was
never committed before this staged edit), so it is never excluded no matter
what it looks like -- an assignment, a comment, or anything else
(E2-F1-S1-T4 security review round 4). `run_staged_scan` composes all four
into one `LintReport` that `render_lint_report` turns into the text
`devcontainer_config.cli` prints.
None of these raise on a caller's behalf silently: an unreadable or
undecodable source is a `SecretScanError`, never an empty result standing in
for one.

`commits_in_range`, `added_lines`, `_first_parent` and
`empty_tree_object_id` (E2-F1-S2-T1) are the equally impure shell for
range mode: each spawns `git` to walk a pushed range instead of the index,
so a credential introduced early and removed later is still scanned rather
than missed because the tip is clean. `scan_range` composes them into one
`RangeReport` that `render_range_report` turns into the text Section 4.6's
worked example shows, reusing `scan_lines` and `PATTERNS` unchanged so
staged mode and range mode can never drift onto two different pattern
sets.

`run_git` (E2-F2-S1-T3) is the single `subprocess` wrapper every function
above spawns `git` through, in both staged mode and range mode. It is
public, taking a keyword-only `error_type: type[Exception] = SecretScanError`
so a caller outside this module -- the git-hooks installer, for one -- gets
its own exception type back on a not-installed or nonzero-exit failure
instead of always receiving a `SecretScanError`. `empty_tree_object_id`
forwards its own `error_type` into `run_git` unchanged, for the same reason.
`run_git` also takes a keyword-only `tolerated_exit_code: int | None = None`;
`_committed_text` (E2-F1-S1-T4) is the one caller that sets it, so it can
tell `git rev-parse --verify --quiet`'s own "this object does not resolve"
exit code apart from every other git failure without a second, undocumented
`subprocess` call site.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from re import Pattern
from types import MappingProxyType
from typing import Protocol, overload

from devcontainer_config import repo

# The number of leading characters of a redacted match that are safe to
# print. Declared once and reused by every credential-bearing detector below,
# rather than repeated at each detector's construction, so the redaction
# policy is one value instead of several that could drift apart.
_REDACTED_SAFE_PREFIX_LEN = 4

# Printable detectors show the whole match, so their safe-prefix length is
# unused; it is still set explicitly (never left to a default) so every
# detector construction states its redaction policy in the same place.
_PRINTABLE_SAFE_PREFIX_LEN = 0


class SecretScanError(RuntimeError):
    """Raised when a caller misuses the scanner in a way that must not proceed silently.

    Every raise site in this module names the offending value and, where
    relevant, what the caller should pass instead, so the operator does not
    have to guess which input was wrong.
    """


class Detector(Protocol):
    """What every detector record carries, regardless of how it matches.

    `PatternDetector` matches with a compiled expression; `ShellEnvLineDetector`
    and `CatalogSecretNameDetector` match by comparing against a field of
    `ScanSources` instead. `scan_lines` depends on this protocol, not on any
    one of the three concrete records, so adding a new kind of detector never
    requires changing `scan_lines`.

    Every member below is declared as a read-only `@property` rather than a
    plain attribute. All three concrete records are frozen dataclasses, so
    their fields are read-only too; a plain-attribute Protocol member is
    structurally a settable variable and mypy would reject a frozen
    dataclass as satisfying it, even though nothing here ever needs to
    write to one.
    """

    @property
    def identifier(self) -> str: ...

    @property
    def description(self) -> str: ...

    @property
    def printable(self) -> bool: ...

    @property
    def safe_prefix_len(self) -> int: ...

    def find(self, line: str, sources: ScanSources) -> str | None:
        """The matched text in `line`, or `None` if this detector does not match."""
        ...


@dataclass(frozen=True)
class PatternDetector:
    """A detector that matches by searching `line` with a compiled expression.

    When `pattern` declares exactly one capturing group, `find` returns that
    group instead of the whole match, so a detector such as the AWS secret
    access key assignment can report the value alone rather than the
    variable name and operator that precede it.
    """

    identifier: str
    description: str
    printable: bool
    safe_prefix_len: int
    pattern: Pattern[str]

    def find(self, line: str, sources: ScanSources) -> str | None:
        match = self.pattern.search(line)
        if match is None:
            return None
        return match.group(1) if self.pattern.groups else match.group(0)


@dataclass(frozen=True)
class ShellEnvLineDetector:
    """A detector that matches when `line` is exactly one already in `shell.env`.

    Comparison is a single membership test against
    `sources.shell_env_lines`, not a per-character search, because the thing
    being detected is line identity, not a substring shape. `shell_env_lines`
    (E2-F1-S1-T4) has already filtered that set before it ever reaches this
    detector, so a line that also appears, verbatim, in both the index and
    `HEAD` version of the tracked `shell.env.example` is never a member here;
    this detector matches only a value a developer actually typed in,
    including one a developer stages into `shell.env.example` itself
    (E2-F1-S1-T4 security review round 4): that value has no counterpart in
    `HEAD` yet, so it is never excluded, whatever shape it takes.
    """

    identifier: str
    description: str
    printable: bool
    safe_prefix_len: int

    def find(self, line: str, sources: ScanSources) -> str | None:
        return line if line in sources.shell_env_lines else None


@dataclass(frozen=True)
class CatalogSecretNameDetector:
    """A detector that matches when `line` contains any catalog secret's name.

    Returns the specific name found, not the whole line, so a redacted
    render never needs more of the line than the name itself.
    """

    identifier: str
    description: str
    printable: bool
    safe_prefix_len: int

    def find(self, line: str, sources: ScanSources) -> str | None:
        for name in sources.catalog_secret_names:
            if name in line:
                return name
        return None


@dataclass(frozen=True)
class ScanSources:
    """The two sources `scan_lines` compares content against, injected rather than discovered.

    Both fields are required with no default, so a caller cannot omit one
    without the omission being visible in the call itself (AC-FUNC-004).
    """

    shell_env_lines: tuple[str, ...]
    catalog_secret_names: tuple[str, ...]

    def __post_init__(self) -> None:
        for index, line in enumerate(self.shell_env_lines):
            if not line.strip():
                raise SecretScanError(
                    "ERROR: ScanSources.shell_env_lines contains a blank entry\n"
                    f"Index {index} is blank or whitespace-only.\n"
                    "A blank entry would compare equal to every scanned line, "
                    "turning the shell-env-line detector into a permanent "
                    "denial. Remove the blank entry from the shell.env lines "
                    "passed to ScanSources."
                )


@dataclass(frozen=True)
class Finding:
    """One detector's match on one line, with the rendering decision attached.

    Carrying `printable` here, copied from the detector that produced the
    finding, lets a caller group or filter findings by rendering behavior
    without re-deriving it from the detector registry.
    """

    detector_id: str
    line_number: int
    matched_text: str
    printable: bool


# Literal prefixes each of these three detectors recognizes. The compiled
# patterns below build their alternation from these same tuples, and the
# Scanner suite composes its positive fixtures from them too (via
# `SAMPLE_PREFIXES`), so the prefix set is declared exactly once. A test
# fixture that instead stored an assembled credential-shaped literal would be
# detected by this very scanner the next time it ran over this repository.
_AWS_ACCESS_KEY_PREFIXES: tuple[str, ...] = ("AKIA", "ASIA")
_GITHUB_TOKEN_PREFIXES: tuple[str, ...] = ("ghp_", "gho_", "github_pat_")
_SLACK_TOKEN_PREFIXES: tuple[str, ...] = ("xoxb-", "xoxp-")

SAMPLE_PREFIXES: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "aws-access-key-id": _AWS_ACCESS_KEY_PREFIXES,
        "github-token": _GITHUB_TOKEN_PREFIXES,
        "slack-token": _SLACK_TOKEN_PREFIXES,
    }
)


def _alternation(prefixes: tuple[str, ...]) -> str:
    """A regex alternation group over literal `prefixes`, each escaped."""
    return "(?:" + "|".join(re.escape(prefix) for prefix in prefixes) + ")"


_AWS_ACCESS_KEY_PATTERN = re.compile(
    r"\b" + _alternation(_AWS_ACCESS_KEY_PREFIXES) + r"[0-9A-Z]{16}\b"
)
_AWS_SECRET_KEY_PATTERN = re.compile(
    r"(?i)aws[_-]?secret[_-]?access[_-]?key\s*[:=]\s*[\"']?([A-Za-z0-9/+=]{40})[\"']?"
)
_PRIVATE_KEY_BLOCK_PATTERN = re.compile(r"-----BEGIN(?: [A-Z0-9]+)? PRIVATE KEY-----")
_GITHUB_TOKEN_PATTERN = re.compile(
    r"\b" + _alternation(_GITHUB_TOKEN_PREFIXES) + r"[A-Za-z0-9_]{20,}\b"
)
_SLACK_TOKEN_PATTERN = re.compile(
    r"\b" + _alternation(_SLACK_TOKEN_PREFIXES) + r"[A-Za-z0-9-]{10,}\b"
)
_BEARER_TOKEN_PATTERN = re.compile(r"(?i)\bBearer\s+([A-Za-z0-9\-._~+/]{20,}={0,2})\b")
_SSO_PORTAL_URL_PATTERN = re.compile(r"https://[A-Za-z0-9.-]+\.awsapps\.com/start\b")
_ACCOUNT_ID_PATTERN = re.compile(r"\b\d{12}\b")
_EC2_INSTANCE_ID_PATTERN = re.compile(r"\bi-(?:[0-9a-f]{8}|[0-9a-f]{17})\b")


def build_registry(detectors: Iterable[Detector]) -> tuple[Detector, ...]:
    """`detectors`, unchanged, after asserting every identifier is unique.

    Called once at import time to build `PATTERNS`, and directly by the
    Scanner suite to prove the duplicate-identifier error path, without
    needing a second, parallel construction function that could drift from
    this one.

    Raises:
        SecretScanError: if two detectors share an identifier. A duplicate
            would make a finding ambiguous to the report and to any caller
            that filters by identifier.
    """
    ordered = tuple(detectors)
    seen: dict[str, Detector] = {}
    for detector in ordered:
        first = seen.get(detector.identifier)
        if first is not None:
            raise SecretScanError(
                f"ERROR: duplicated detector identifier '{detector.identifier}'\n"
                f"First declared for: {first.description}\n"
                f"Duplicated by: {detector.description}\n"
                "Every detector in the registry must have a unique "
                "identifier; rename one of the two detectors above."
            )
        seen[detector.identifier] = detector
    return ordered


PATTERNS: tuple[Detector, ...] = build_registry(
    (
        PatternDetector(
            identifier="aws-access-key-id",
            description="AWS access key identifier",
            printable=False,
            safe_prefix_len=_REDACTED_SAFE_PREFIX_LEN,
            pattern=_AWS_ACCESS_KEY_PATTERN,
        ),
        PatternDetector(
            identifier="aws-secret-access-key",
            description="AWS secret access key assignment",
            printable=False,
            safe_prefix_len=_REDACTED_SAFE_PREFIX_LEN,
            pattern=_AWS_SECRET_KEY_PATTERN,
        ),
        PatternDetector(
            identifier="private-key-block",
            description="Private key block",
            printable=False,
            safe_prefix_len=_REDACTED_SAFE_PREFIX_LEN,
            pattern=_PRIVATE_KEY_BLOCK_PATTERN,
        ),
        PatternDetector(
            identifier="github-token",
            description="GitHub token",
            printable=False,
            safe_prefix_len=_REDACTED_SAFE_PREFIX_LEN,
            pattern=_GITHUB_TOKEN_PATTERN,
        ),
        PatternDetector(
            identifier="slack-token",
            description="Slack token",
            printable=False,
            safe_prefix_len=_REDACTED_SAFE_PREFIX_LEN,
            pattern=_SLACK_TOKEN_PATTERN,
        ),
        PatternDetector(
            identifier="bearer-token",
            description="Bearer token",
            printable=False,
            safe_prefix_len=_REDACTED_SAFE_PREFIX_LEN,
            pattern=_BEARER_TOKEN_PATTERN,
        ),
        PatternDetector(
            identifier="sso-portal-url",
            description="SSO portal URL",
            printable=True,
            safe_prefix_len=_PRINTABLE_SAFE_PREFIX_LEN,
            pattern=_SSO_PORTAL_URL_PATTERN,
        ),
        PatternDetector(
            identifier="account-id",
            description="Twelve-digit account identifier",
            printable=True,
            safe_prefix_len=_PRINTABLE_SAFE_PREFIX_LEN,
            pattern=_ACCOUNT_ID_PATTERN,
        ),
        PatternDetector(
            identifier="ec2-instance-id",
            description="EC2 instance identifier",
            printable=True,
            safe_prefix_len=_PRINTABLE_SAFE_PREFIX_LEN,
            pattern=_EC2_INSTANCE_ID_PATTERN,
        ),
        CatalogSecretNameDetector(
            identifier="catalog-secret-name",
            description="Catalog secret name",
            printable=False,
            safe_prefix_len=_REDACTED_SAFE_PREFIX_LEN,
        ),
        ShellEnvLineDetector(
            identifier="shell-env-line",
            description="shell.env line",
            printable=False,
            safe_prefix_len=_REDACTED_SAFE_PREFIX_LEN,
        ),
    )
)

DETECTORS_BY_ID: Mapping[str, Detector] = MappingProxyType(
    {detector.identifier: detector for detector in PATTERNS}
)


def _resolve_detectors(detector_ids: Sequence[str] | None) -> tuple[Detector, ...]:
    """The detectors `scan_lines` should run: all of `PATTERNS`, or a named subset."""
    if detector_ids is None:
        return PATTERNS
    unknown = sorted(
        {identifier for identifier in detector_ids if identifier not in DETECTORS_BY_ID}
    )
    if unknown:
        raise SecretScanError(
            "ERROR: unknown detector identifier(s): "
            f"{', '.join(unknown)}\n"
            f"Known identifiers: {', '.join(sorted(DETECTORS_BY_ID))}\n"
            "Pass only identifiers declared in PATTERNS, or omit detector_ids "
            "to scan with every detector."
        )
    return tuple(detector for detector in PATTERNS if detector.identifier in detector_ids)


def scan_lines(
    lines: Iterable[tuple[int, str]],
    sources: ScanSources,
    *,
    detector_ids: Sequence[str] | None = None,
) -> tuple[Finding, ...]:
    """Every finding across `lines`, from every detector in `detector_ids` (default: all).

    Pure: opens no file, spawns no subprocess, reaches no network. `lines`
    and `sources` are the only inputs, which is what lets the Scanner suite
    (spec Section 10.2) run with no AWS, no docker and no network at all
    (AC-10.14).

    Args:
        lines: numbered line content to scan, in `(line_number, text)` pairs.
            Numbered rather than a plain sequence so a caller scanning a diff
            hunk or a partial file can report the real line number, not a
            position relative to only what was handed in.
        sources: the developer's `shell.env` lines and the catalog secret
            names, injected because the catalog client does not exist until
            E3 (spec Section 4.5) and because this function must stay pure.
        detector_ids: restrict scanning to these detector identifiers. Every
            detector runs when omitted.

    Raises:
        SecretScanError: if `detector_ids` names an identifier not in
            `PATTERNS`, naming the unknown identifiers and the known ones,
            rather than silently scanning with an empty detector set and
            reporting a clean result.
    """
    active_detectors = _resolve_detectors(detector_ids)
    findings: list[Finding] = []
    for line_number, text in lines:
        for detector in active_detectors:
            matched_text = detector.find(text, sources)
            if matched_text is not None:
                findings.append(
                    Finding(
                        detector_id=detector.identifier,
                        line_number=line_number,
                        matched_text=matched_text,
                        printable=detector.printable,
                    )
                )
    return tuple(findings)


def render_finding(finding: Finding, detector: Detector) -> str:
    """`finding.matched_text`, in full if printable, otherwise redacted.

    A redacted render is the detector-declared safe prefix of the matched
    text plus the count of characters withheld, never the value itself
    (AC-5.3): the scanner holds matched text in memory only long enough to
    render this string.

    Raises:
        SecretScanError: if `detector.identifier` does not match
            `finding.detector_id`, so a caller cannot silently apply one
            detector's redaction policy to another detector's finding.
    """
    if detector.identifier != finding.detector_id:
        raise SecretScanError(
            "ERROR: render_finding detector mismatch\n"
            f"finding.detector_id={finding.detector_id!r} but "
            f"detector.identifier={detector.identifier!r}\n"
            "Pass the Detector that produced this Finding, typically "
            "DETECTORS_BY_ID[finding.detector_id]."
        )
    if finding.printable:
        return finding.matched_text
    prefix = finding.matched_text[: detector.safe_prefix_len]
    withheld = len(finding.matched_text) - len(prefix)
    return f"{prefix}... ({withheld} chars withheld)"


# Printed verbatim in the lint-secrets header next to the catalog secret name
# count (spec Section 4.5): the catalog client landed in E3-F1-S1-T1, but its
# call site is not wired into this staged scan yet, so `run_staged_scan`
# always sources an explicit empty tuple for `catalog_secret_names` rather
# than inferring the note from the count being zero. When a follow-up unit
# wires the call, the call site inside `run_staged_scan` changes to ask the
# catalog client instead of this constant disappearing.
_CATALOG_SECRET_NAMES_NOT_WIRED_NOTE = "catalog client not yet wired into this scan"


@overload
def run_git(
    args: Sequence[str],
    *,
    root: Path,
    not_installed_message: Callable[[], str],
    failure_message: Callable[[str], str],
    input_bytes: bytes | None = None,
    config: Mapping[str, str] = MappingProxyType({}),
    error_type: type[Exception] = SecretScanError,
    tolerated_exit_code: None = None,
) -> bytes: ...


@overload
def run_git(
    args: Sequence[str],
    *,
    root: Path,
    not_installed_message: Callable[[], str],
    failure_message: Callable[[str], str],
    input_bytes: bytes | None = None,
    config: Mapping[str, str] = MappingProxyType({}),
    error_type: type[Exception] = SecretScanError,
    tolerated_exit_code: int,
) -> bytes | None: ...


def run_git(
    args: Sequence[str],
    *,
    root: Path,
    not_installed_message: Callable[[], str],
    failure_message: Callable[[str], str],
    input_bytes: bytes | None = None,
    config: Mapping[str, str] = MappingProxyType({}),
    error_type: type[Exception] = SecretScanError,
    tolerated_exit_code: int | None = None,
) -> bytes | None:
    """Run `git <args>` under `root`; the raw stdout bytes on success.

    Public (E2-F2-S1-T3) so a caller outside this module -- the git-hooks
    installer, for one -- can reuse "run a git subprocess and turn a
    failure into an exception" instead of copy-pasting this subprocess
    shape. `staged_paths`, `staged_blob` and `_committed_text` all need
    exactly that, differing only in the message each raises, so that shape
    lives here once instead of being duplicated at each call site (or,
    worse, omitted at one of them). The caller supplies the two message
    builders because only the caller knows which git invocation failed and
    what the operator should do about it. `input_bytes` is optional and only
    `empty_tree_object_id` (range mode) supplies it, to feed
    `git hash-object --stdin` an empty tree. `config` is optional and only
    `added_lines` supplies it, to run `diff-tree` with
    `core.quotepath=false`, so a non-ASCII path in the diff it reads is not
    quoted and octal-escaped in the first place. `error_type` is
    keyword-only and defaults to `SecretScanError`; a caller outside this
    module supplies its own exception type so a git failure surfaces as
    that caller's own error rather than as this module's. `tolerated_exit_code`
    is keyword-only and, when given, names the single exit code that is an
    expected outcome rather than a failure: `_committed_text` passes
    `git rev-parse --verify --quiet`'s own documented exit code 1 (which
    that subcommand reserves for "this object does not resolve", printing no
    stderr) so it can tell "not tracked here" apart from every other git
    failure, which still raises `error_type` even with `tolerated_exit_code`
    set.

    Raises:
        error_type: built from `not_installed_message` if the `git` binary
            is not on PATH, or from `failure_message` (given git's decoded
            stderr) if git exits non-zero and its exit code is not
            `tolerated_exit_code`. `SecretScanError` unless the caller
            supplies a different `error_type`.
    """
    config_args = [flag for key, value in config.items() for flag in ("-c", f"{key}={value}")]
    try:
        completed = subprocess.run(
            ["git", *config_args, "-C", str(root), *args],
            input=input_bytes,
            capture_output=True,
            check=True,
        )
    except FileNotFoundError as exc:
        raise error_type(not_installed_message()) from exc
    except subprocess.CalledProcessError as exc:
        if tolerated_exit_code is not None and exc.returncode == tolerated_exit_code:
            return None
        stderr = exc.stderr.decode("utf-8", errors="replace").strip() if exc.stderr else ""
        raise error_type(failure_message(stderr)) from exc
    return completed.stdout


def staged_paths(root: Path) -> tuple[str, ...]:
    """Repo-relative paths staged for the next commit under `root`.

    Reads `git diff --cached --name-only -z --diff-filter=ACMR`: the
    `-z` terminator survives any path, including one containing a newline,
    and the filter is restricted to added, copied, modified and renamed
    entries because a staged deletion leaves no index blob for
    `staged_blob` to read.

    Raises:
        SecretScanError: if the `git` binary is not on PATH, or if `root` is
            not inside a git work tree.
    """
    stdout = run_git(
        ["diff", "--cached", "--name-only", "-z", "--diff-filter=ACMR"],
        root=root,
        not_installed_message=lambda: (
            "ERROR: git is not installed\n"
            f"staged_paths needs the git binary to list staged content under "
            f"{root} and none was found on PATH.\n"
            "Install git and ensure it is on PATH, then retry."
        ),
        failure_message=lambda stderr: (
            f"ERROR: {root} is not a git work tree\n"
            f"staged_paths ran 'git diff --cached' in {root} and git "
            f"reported: {stderr}\n"
            "Run this from inside a git checkout, or pass the checkout root "
            "explicitly instead of relying on discovery."
        ),
    )
    raw = stdout.decode("utf-8")
    return tuple(path for path in raw.split("\0") if path)


def staged_blob(root: Path, path: str) -> tuple[tuple[int, str], ...]:
    """Numbered lines of the index (staged) content of `path` under `root`.

    Reads `git show :<path>`, the index blob, never the working-tree copy at
    `root / path`: a developer who stages a secret and then edits the file
    would otherwise commit the staged version unchecked while this scanner
    reported a clean file (AC-TEST-002).

    Raises:
        SecretScanError: if the `git` binary is not on PATH; if `git show`
            exits non-zero, which happens for a staged gitlink (a nested
            repository or submodule reference) because the index holds no
            blob for it to read, only a commit pointer; or if the staged
            blob it did read does not decode as UTF-8. Skipping an
            undecodable or unreadable blob would let a credential through
            inside content the scanner declined to read, so every one of
            these refuses the whole scan instead of narrowing it silently.
    """
    stdout = run_git(
        ["show", f":{path}"],
        root=root,
        not_installed_message=lambda: (
            "ERROR: git is not installed\n"
            f"staged_blob needs the git binary to read the staged content of "
            f"{path} under {root} and none was found on PATH.\n"
            "Install git and ensure it is on PATH, then retry."
        ),
        failure_message=lambda stderr: (
            f"ERROR: cannot read staged content of {path}\n"
            f"staged_blob ran 'git show :{path}' in {root} and git "
            f"reported: {stderr}\n"
            "If this is a staged gitlink (a nested repository or "
            f"submodule reference), unstage it with 'git rm -r --cached "
            f"{path}' before committing; otherwise unstage it with "
            f"'git restore --staged {path}' and investigate why git could "
            "not read its staged content."
        ),
    )
    try:
        text = stdout.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SecretScanError(
            f"ERROR: staged content of {path} is not valid UTF-8\n"
            f"staged_blob could not decode the staged bytes of {path}: {exc}\n"
            f"Unstage it (git restore --staged {path}) before scanning, or "
            "restage it after converting its content to UTF-8."
        ) from exc
    return tuple(enumerate(text.splitlines(), start=1))


def _non_blank_lines(text: str) -> tuple[str, ...]:
    """Every line of `text` with content once whitespace is stripped, in order.

    The one filtering rule `shell_env_lines` and `shell_env_example_lines` both
    apply to whatever they load, kept here once instead of copy-pasted at each
    call site (E2-F1-S1-T4 REFACTOR).
    """
    return tuple(line for line in text.splitlines() if line.strip())


def _committed_text(root: Path, path: str, object_name: str) -> str | None:
    """UTF-8 text of `path` as recorded in the single git object `object_name`
    under `root` (`:<path>` for the index, `HEAD:<path>` for the last commit), or
    `None` if `object_name` does not resolve.

    Reads exactly the one object it is given rather than falling back from one to
    the other: `shell_env_example_lines`, the single caller, calls this once for
    the index object and once for the `HEAD` object so it can tell "committed but
    not currently staged this way" apart from "staged this way" and intersect the
    two (E2-F1-S1-T4 review round 4) -- a function that silently substituted one
    object's content for the other's absence could never make that distinction.
    Never reads `root / path` directly either way: the working tree is not a
    trust boundary this function accepts. A developer who edits a tracked file's
    on-disk copy without staging or committing that edit has produced content
    with no footprint in the commit this scan is about to allow; resolving
    `path`'s trusted content through the working tree instead of the index or
    `HEAD` would let that same uncommitted edit count as "trusted" (E2-F1-S1-T4
    review round 1).

    `object_name` is probed with `run_git` twice: `git rev-parse --verify
    --quiet <object_name>` first, tolerating only that subcommand's own exit code
    1 ("this object does not resolve", no stderr printed), and `git show
    <object_name>` second, only once the probe confirms the object exists. This
    tells "`path` does not resolve at `object_name`" -- a legitimate, empty-result
    outcome for an unborn branch or a template that was never added there --
    apart from every other git failure (an unreadable repository, a corrupt
    object, `root` not being a work tree at all), which still raises
    `SecretScanError` rather than being folded into the same empty result
    (E2-F1-S1-T4 review round 2). Both probes go through the module's shared
    `run_git` wrapper rather than a second raw `subprocess.run` call site,
    keeping the not-installed/nonzero-exit handling in the one place `run_git`'s
    own docstring describes.

    Raises:
        SecretScanError: if the `git` binary is not on PATH; if `root` is not a
            git work tree or another git failure occurs that is not "this
            object does not resolve"; or if a blob this function does find
            does not decode as UTF-8.
    """

    def _not_installed_message() -> str:
        return (
            "ERROR: git is not installed\n"
            f"_committed_text needs the git binary to read the tracked "
            f"content of {path} under {root} and none was found on PATH.\n"
            "Install git and ensure it is on PATH, then retry."
        )

    def _probe_failure_message(stderr: str) -> str:
        return (
            f"ERROR: cannot determine whether {path} is tracked\n"
            f"_committed_text ran 'git rev-parse --verify --quiet "
            f"{object_name}' in {root} and git reported: {stderr}\n"
            "Run this from inside a git checkout, or pass the checkout "
            "root explicitly instead of relying on discovery."
        )

    def _read_failure_message(stderr: str) -> str:
        return (
            f"ERROR: cannot read tracked content of {path}\n"
            f"_committed_text ran 'git show {object_name}' in {root} and "
            f"git reported: {stderr}\n"
            "Investigate why git could not read an object it just "
            "confirmed exists, then retry."
        )

    exists = run_git(
        ["rev-parse", "--verify", "--quiet", object_name],
        root=root,
        not_installed_message=_not_installed_message,
        failure_message=_probe_failure_message,
        tolerated_exit_code=1,
    )
    if exists is None:
        return None
    stdout = run_git(
        ["show", object_name],
        root=root,
        not_installed_message=_not_installed_message,
        failure_message=_read_failure_message,
    )
    try:
        return stdout.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SecretScanError(
            f"ERROR: tracked content of {path} is not valid UTF-8\n"
            f"_committed_text could not decode git object {object_name}: {exc}\n"
            "Fix the tracked file's encoding, then retry."
        ) from exc


def shell_env_example_lines(root: Path) -> tuple[str, ...]:
    """Lines of `root`'s tracked `shell.env.example` present, verbatim, in *both*
    the git index and `HEAD` -- the lines this commit does not introduce or
    change -- or empty when the index does not resolve the path at all (it is
    tracked nowhere, or a staged deletion removed it from the index).

    Intersecting the index and `HEAD` versions, rather than matching on line
    shape (E2-F1-S1-T4 security review round 4), is what makes this exclusion
    set safe: `shell.env` (spec Section 1.7) is documented as seeded by
    `cp shell.env.example shell.env`, so every line the template ships with
    also lands, verbatim, in a fresh `shell.env`, and an untouched template
    line is scaffolding, not a secret a developer typed in -- `shell_env_lines`
    uses this loader's result to tell the two apart. A line a developer stages
    into `shell.env.example` itself, in this same commit, has no counterpart in
    `HEAD` yet, so the intersection never contains it, whatever it looks like:
    an assignment, a comment, or anything else. Comparing shape instead of
    provenance let a real, filled-in value staged into the template -- as a
    plain assignment or hidden behind a `#` -- blind the detector for that
    same value staged anywhere else in the same commit; comparing provenance
    closes that regardless of shape, because no line this commit introduces
    can ever have already been in `HEAD`.

    Reads both objects through `_committed_text`: `:shell.env.example` for the
    index and `HEAD:shell.env.example` for the last commit, neither ever
    falling back to the other or to the working tree. Resolved with
    `repo.example_for(repo.SHELL_ENV)` rather than a hard-coded name, so the
    private-file/example naming convention stays declared in exactly one
    place.

    Raises:
        SecretScanError: if `git` is not on PATH; if `root` is not a git work
            tree or another git failure occurs (`_committed_text` never folds
            that into the same empty result "does not resolve" returns); or if
            a tracked blob this finds does not decode as UTF-8. Treating an
            unreadable source as an empty one would silently narrow the
            exclusion set without telling the operator why.
    """
    path = repo.example_for(repo.SHELL_ENV)
    index_text = _committed_text(root, path, f":{path}")
    if index_text is None:
        return ()
    head_text = _committed_text(root, path, f"HEAD:{path}")
    head_lines = frozenset(_non_blank_lines(head_text)) if head_text is not None else frozenset()
    return tuple(line for line in _non_blank_lines(index_text) if line in head_lines)


def _shell_env_comparison_lines(root: Path) -> tuple[tuple[str, ...], int]:
    """`root`'s `shell.env` non-blank lines with every line the tracked
    `shell.env.example` shares between the index and `HEAD` removed, paired with
    how many lines that removal excluded.

    Shared by `shell_env_lines` (the tuple `run_staged_scan` / `scan_range` inject
    into `ScanSources`) and `run_staged_scan` (the excluded-line count
    `LintReport` / `render_lint_report` surfaces to the operator, E2-F1-S1-T4
    security review round 4 LOW), so both read `shell.env` once and agree on what
    "excluded" means instead of computing the split twice.

    Absence of `shell.env` is the normal state of a fresh clone (spec Section 1.7)
    and contributes zero lines and zero exclusions; an unreadable file that does
    exist is a different condition, so it is not folded into the same empty
    result.

    Raises:
        SecretScanError: if `shell.env` exists but cannot be opened. Treating an
            unreadable source as an empty one would silently narrow the scan
            without telling the operator why.
    """
    path = repo.private_paths(root)[repo.SHELL_ENV]
    if not path.is_file():
        return (), 0
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SecretScanError(
            f"ERROR: cannot read {path}\n"
            f"{path} exists but could not be opened: {exc}\n"
            "Fix the file's permissions, then retry."
        ) from exc
    template_lines = frozenset(shell_env_example_lines(root))
    raw_lines = _non_blank_lines(text)
    compared = tuple(line for line in raw_lines if line not in template_lines)
    return compared, len(raw_lines) - len(compared)


def shell_env_lines(root: Path) -> tuple[str, ...]:
    """Non-blank lines of `root`'s `shell.env` that are not also a line
    `shell_env_example_lines` returns for the tracked `shell.env.example`, or
    empty when `shell.env` is absent.

    Resolved through `repo.private_paths` rather than a new root walk (AC-3.1).
    `shell.env` is gitignored (spec Section 1.7) and never tracked, so this reads
    it straight from the working tree -- the working tree is the only copy that
    exists, unlike `shell_env_example_lines`, which never trusts it.

    A line also returned by `shell_env_example_lines` is excluded from the
    comparison set built here (AC-CODE-001, E2-F1-S1-T4): `shell_env_example_lines`
    only ever returns a line present, verbatim, in both the index and `HEAD`
    version of the tracked template, so a line is excluded because it demonstrably
    predates this commit, never merely because the template happens to repeat that
    exact value some other way. Every one of `run_staged_scan` / `scan_range`
    shares this one loader (via `_shell_env_comparison_lines`), so the exclusion
    cannot be forgotten at one call site while present at another.

    Raises:
        SecretScanError: see `_shell_env_comparison_lines`.
    """
    compared, _excluded_count = _shell_env_comparison_lines(root)
    return compared


@dataclass(frozen=True)
class StagedFinding:
    """One `Finding` together with the repo-relative staged path it came from."""

    path: str
    finding: Finding


@dataclass(frozen=True)
class LintReport:
    """Every source's entry count and every finding from one `run_staged_scan` call.

    `shell_env_line_count` is `shell_env_lines`' result, which already excludes
    any line also present, verbatim, in both the index and `HEAD` version of the
    tracked `shell.env.example` (E2-F1-S1-T4): it is the count of lines actually
    compared, not the raw line count of `shell.env` on disk.
    `shell_env_excluded_line_count` is how many lines that exclusion removed
    (E2-F1-S1-T4 security review round 4 LOW):
    `render_lint_report` prints both counts so an operator reading
    `shell.env lines: 0` can tell a genuinely empty `shell.env` apart from one
    whose entire comparison set the template exclusion suppressed, rather than
    the label alone standing in for a number the report never showed.
    """

    staged_path_count: int
    shell_env_line_count: int
    shell_env_excluded_line_count: int
    catalog_secret_name_count: int
    findings: tuple[StagedFinding, ...]


def _grouped_findings(
    lines_by_path: Mapping[str, Sequence[tuple[int, str]]], sources: ScanSources
) -> tuple[tuple[str, Finding], ...]:
    """`(path, finding)` for every finding across `lines_by_path`, one `scan_lines` call per path.

    Both `run_staged_scan` and `scan_range` reduce to the same thing --
    "for each path, scan its numbered lines and pair every finding with
    that path" -- and differ only in how they collect `lines_by_path`
    (`staged_blob` per staged path, versus `added_lines` grouped per commit).
    This is that shared reduction, written once so the two modes cannot
    each grow their own slightly different version of it.
    """
    results: list[tuple[str, Finding]] = []
    for path, lines in lines_by_path.items():
        results.extend((path, finding) for finding in scan_lines(lines, sources))
    return tuple(results)


def run_staged_scan(root: Path) -> LintReport:
    """Scan every path staged for the next commit under `root`; the full report.

    Reads the git index, never the working tree (AC-FUNC-001): `staged_paths`
    lists what is staged and `staged_blob` reads each one from the index, so
    a file edited after being staged is still scanned as it will actually be
    committed (AC-TEST-002). Catalog secret names are an explicit empty
    tuple (spec Section 4.5): the catalog client landed in E3-F1-S1-T1, but
    its call site is not wired into this staged scan yet, so nothing here
    infers or guesses a value for it until a follow-up unit adds that call.
    """
    paths = staged_paths(root)
    shell_env, shell_env_excluded_count = _shell_env_comparison_lines(root)
    catalog_secret_names: tuple[str, ...] = ()
    sources = ScanSources(shell_env_lines=shell_env, catalog_secret_names=catalog_secret_names)

    lines_by_path = {path: staged_blob(root, path) for path in paths}
    findings = tuple(
        StagedFinding(path=path, finding=finding)
        for path, finding in _grouped_findings(lines_by_path, sources)
    )

    return LintReport(
        staged_path_count=len(paths),
        shell_env_line_count=len(shell_env),
        shell_env_excluded_line_count=shell_env_excluded_count,
        catalog_secret_name_count=len(catalog_secret_names),
        findings=findings,
    )


def render_lint_report(report: LintReport) -> str:
    """The `[LINT]` report text for `report`: a header, then one line per finding.

    The header names every source and its entry count (AC-FUNC-003), even
    when nothing was staged, so "zero paths were scanned" is a fact the
    header states rather than an empty screen the operator has to interpret.
    The `shell.env` line prints both the compared count and how many lines
    the template exclusion removed (E2-F1-S1-T4 security review round 4 LOW)
    so an operator reading `shell.env lines: 0` can tell a genuinely empty
    `shell.env` apart from one whose entire comparison set the tracked
    `shell.env.example` intersection excluded -- the label alone, with no
    count, could not make that distinction. Each finding line names
    the staged path, the line number and the detector description
    (AC-FUNC-004); the value itself comes from `render_finding`, which
    redacts every non-printable detector's match.
    """
    lines = [
        "[LINT] secrets in staged content",
        f"  staged paths scanned: {report.staged_path_count}",
        f"  shell.env lines: {report.shell_env_line_count} "
        f"({report.shell_env_excluded_line_count} template lines excluded)",
        f"  catalog secret names: {report.catalog_secret_name_count} "
        f"({_CATALOG_SECRET_NAMES_NOT_WIRED_NOTE})",
    ]
    for staged in report.findings:
        detector = DETECTORS_BY_ID[staged.finding.detector_id]
        rendered = render_finding(staged.finding, detector)
        lines.append(
            f"{staged.path}:{staged.finding.line_number}: {detector.description}: {rendered}"
        )
    return "\n".join(lines)


# --- Range mode (spec Section 4.6, E2-F1-S2-T1) ---
#
# Scanning only the tip would miss a secret introduced earlier in a pushed
# range and removed later, which still reaches the remote in history. Range
# mode enumerates every commit in `<a>..<b>` oldest first and scans the
# lines each commit adds, so that credential is still reported and
# attributed to the commit that introduced it. It reuses `scan_lines` and
# `PATTERNS` unchanged (AC-FUNC-005): only the content collection differs
# from staged mode.

_RANGE_SEPARATOR = ".."

_HUNK_HEADER_PATTERN = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")

# `git diff-tree` emits the unquoted form below for a path with no byte an
# unquoted path cannot represent literally. It falls back to the C-quoted
# form when the path holds a double quote or a backslash -- run under
# `-c core.quotepath=false` (see `added_lines`), that is the only remaining
# reason a path would be quoted, since that setting already stops a
# non-ASCII byte alone from triggering it.
_DIFF_NEW_FILE_HEADER_PATTERN = re.compile(r"^\+\+\+ b/(.+)$")
_DIFF_NEW_FILE_HEADER_QUOTED_PATTERN = re.compile(r'^\+\+\+ "((?:[^"\\]|\\.)*)"$')

# `git diff-tree` emits this exact line, rather than `+++ b/<path>`, as the
# post-image header of a file section that has no post-image: the delete
# side of a deletion, and (since `added_lines` runs `-r` without rename
# detection) the delete half of a rename as well. Neither header pattern
# above matches it, and it must not be treated as an uninterpretable
# header, because a section with no post-image contributes no `+` lines --
# there is nothing for an unresolved `current_path` to hide.
_DIFF_DELETED_FILE_HEADER = "+++ /dev/null"

# `git diff-tree -p` emits no `+++ `/hunk section at all for a file whose
# first 8000 bytes hold a NUL byte -- git's own binary heuristic -- only
# this one line in place of everything else a file section would
# otherwise carry. `.+` on the pre-image side is deliberately permissive
# (mirroring the two file-header patterns above): only the post-image
# side, captured as `dst`, is ever read, either the literal `/dev/null` of
# a deletion (no post-image, same as `_DIFF_DELETED_FILE_HEADER`) or a
# `b/<path>` / C-quoted `"b/<path>"` token identical in shape to what a
# `+++ ` header would carry, resolved through the very same
# `_diff_new_file_path` a text file's header goes through.
_DIFF_BINARY_FILE_HEADER_PATTERN = re.compile(
    r'^Binary files .+ and (?P<dst>/dev/null|b/.+|"(?:[^"\\]|\\.)*") differ$'
)

# Byte-for-byte the escape tokens git's quote_c_style() emits: a doubled
# backslash or double quote, one of the seven single-letter C escapes, or a
# three-digit octal byte value (matched separately in
# `_unescape_git_quoted_path`, not listed here since it is not one fixed
# token).
_GIT_QUOTE_SIMPLE_ESCAPES: Mapping[bytes, bytes] = MappingProxyType(
    {
        b"\\": b"\\",
        b'"': b'"',
        b"a": b"\a",
        b"b": b"\b",
        b"f": b"\f",
        b"n": b"\n",
        b"r": b"\r",
        b"t": b"\t",
        b"v": b"\v",
    }
)
_GIT_QUOTE_ESCAPE_PATTERN = re.compile(rb"\\([0-7]{3}|.)", re.DOTALL)


def _parse_range(revision_range: str) -> tuple[str, str]:
    """`(base, tip)` parsed from `revision_range`, which must be exactly `<a>..<b>`.

    Rejects the three-dot symmetric-difference form (`<a>...<b>`) and any
    string that does not split into exactly two non-empty endpoints on the
    two-dot separator, because `commits_in_range` and the range report's
    header both need one unambiguous base and one unambiguous tip, not a
    general git range expression this module would have to special-case.

    Raises:
        SecretScanError: if `revision_range` does not have the `<a>..<b>`
            shape, naming what was received and what is expected instead.
    """
    malformed = SecretScanError(
        f"ERROR: malformed range argument: {revision_range!r}\n"
        "scan_range expects exactly '<a>..<b>', two non-empty git revisions "
        "separated by '..', for example 'main..HEAD'.\n"
        "Pass a range in that shape, not a single revision or a three-dot "
        "('...') range."
    )
    if "..." in revision_range:
        raise malformed
    parts = revision_range.split(_RANGE_SEPARATOR)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise malformed
    return parts[0], parts[1]


def commits_in_range(root: Path, revision_range: str) -> tuple[str, ...]:
    """Commit SHAs in `revision_range`, oldest first (`git rev-list --reverse`).

    Oldest first because a credential added early and removed later must be
    attributed to the commit that added it; scanning in that order is what
    lets `scan_range` group findings under the correct commit without a
    second pass to reorder them.

    Raises:
        SecretScanError: if `revision_range` is not `<a>..<b>` (see
            `_parse_range`); if the `git` binary is not on PATH; or if git
            cannot resolve one or both endpoints, naming the range and
            git's own message.
    """
    _parse_range(revision_range)
    stdout = run_git(
        ["rev-list", "--reverse", revision_range],
        root=root,
        not_installed_message=lambda: (
            "ERROR: git is not installed\n"
            f"commits_in_range needs the git binary to enumerate {revision_range} "
            f"under {root} and none was found on PATH.\n"
            "Install git and ensure it is on PATH, then retry."
        ),
        failure_message=lambda stderr: (
            f"ERROR: cannot resolve range {revision_range}\n"
            f"commits_in_range ran 'git rev-list --reverse {revision_range}' "
            f"in {root} and git reported: {stderr}\n"
            "Confirm both endpoints exist in this repository, for example "
            "with 'git rev-parse <endpoint>', then retry."
        ),
    )
    text = stdout.decode("utf-8")
    return tuple(line for line in text.splitlines() if line)


def empty_tree_object_id(root: Path, *, error_type: type[Exception] = SecretScanError) -> str:
    """Git's empty-tree object id for `root`'s hash algorithm, computed rather than assumed.

    Public (E2-F2-S1-T3) so a caller outside this module -- the git-hooks
    installer, for one -- can derive the same constant without duplicating
    this `git hash-object` invocation. A root commit's `added_lines` needs
    something to diff against that represents "nothing" (spec Section 4.6,
    AC-FUNC-002). The empty-tree id is a mathematical constant of the
    object format a repository uses (SHA-1 or SHA-256), not a value this
    codebase chooses, so it is derived with `git hash-object` on every call
    rather than hard-coded as a literal SHA-1 hex string that would be
    silently wrong for a SHA-256 repository. `error_type` is keyword-only
    and defaults to `SecretScanError`; it is forwarded to `run_git`
    unchanged, so a caller outside this module gets its own exception type
    back rather than `SecretScanError`.

    Raises:
        error_type: if the `git` binary is not on PATH, or if git cannot
            compute the object id under `root`. `SecretScanError` unless
            the caller supplies a different `error_type`.
    """
    stdout = run_git(
        ["hash-object", "-t", "tree", "--stdin"],
        root=root,
        input_bytes=b"",
        error_type=error_type,
        not_installed_message=lambda: (
            "ERROR: git is not installed\n"
            f"empty_tree_object_id needs the git binary to derive the empty "
            f"tree id under {root} and none was found on PATH.\n"
            "Install git and ensure it is on PATH, then retry."
        ),
        failure_message=lambda stderr: (
            f"ERROR: cannot derive the empty tree object id under {root}\n"
            f"'git hash-object -t tree --stdin' reported: {stderr}\n"
            "Run this from inside a git checkout, or pass the checkout root "
            "explicitly instead of relying on discovery."
        ),
    )
    return stdout.decode("utf-8").strip()


def _first_parent(root: Path, commit: str) -> str | None:
    """`commit`'s first parent SHA, or `None` if `commit` is a root commit.

    Reads `git rev-list --parents -n1 <commit>`, whose output is the commit
    itself followed by zero or more parent SHAs; a root commit has none, so
    the split has exactly one token.

    Raises:
        SecretScanError: if the `git` binary is not on PATH, or if `commit`
            cannot be inspected.
    """
    stdout = run_git(
        ["rev-list", "--parents", "-n1", commit],
        root=root,
        not_installed_message=lambda: (
            "ERROR: git is not installed\n"
            f"_first_parent needs the git binary to inspect {commit} under "
            f"{root} and none was found on PATH.\n"
            "Install git and ensure it is on PATH, then retry."
        ),
        failure_message=lambda stderr: (
            f"ERROR: cannot inspect commit {commit}\n"
            f"_first_parent ran 'git rev-list --parents -n1 {commit}' in "
            f"{root} and git reported: {stderr}\n"
            f"Confirm {commit} exists in this repository."
        ),
    )
    tokens = stdout.decode("utf-8").split()
    return tokens[1] if len(tokens) > 1 else None


def _unescape_git_quoted_path(quoted: str, *, commit: str, header: str) -> str:
    """Reverse git's C-style quoting of one diff header path.

    Git quotes a path in a diff header whenever it holds a byte an unquoted
    path cannot represent literally: a double quote or a backslash, escaped
    here as `\\"` or `\\\\`, or (absent `core.quotepath=false`, which
    `added_lines` already sets) a non-ASCII byte, escaped as a three-digit
    octal value. `quoted` is the text between the outer double quotes, not
    including them.

    Raises:
        SecretScanError: naming `commit` and `header`, if an escape token in
            `quoted` is not one git's own `quote_c_style` ever emits, or the
            unescaped bytes do not decode as UTF-8.
    """

    def _replace(match: re.Match[bytes]) -> bytes:
        token = match.group(1)
        if len(token) == 3 and all(0x30 <= byte <= 0x37 for byte in token):
            return bytes([int(token, 8)])
        simple = _GIT_QUOTE_SIMPLE_ESCAPES.get(token)
        if simple is not None:
            return simple
        raise SecretScanError(
            f"ERROR: cannot parse the diff header for commit {commit}\n"
            "added_lines found an escape sequence 'git diff-tree' does not "
            f"emit in this quoted path: {header!r}\n"
            f"Review the commit by hand with 'git show {commit}' before "
            "trusting this range scan."
        )

    try:
        unescaped = _GIT_QUOTE_ESCAPE_PATTERN.sub(_replace, quoted.encode("utf-8"))
        return unescaped.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SecretScanError(
            f"ERROR: cannot parse the diff header for commit {commit}\n"
            f"added_lines decoded a quoted path from {header!r} that is not "
            f"valid UTF-8: {exc}\n"
            f"Review the commit by hand with 'git show {commit}' before "
            "trusting this range scan."
        ) from exc


def _diff_new_file_path(line: str, *, commit: str) -> str:
    """The repo-relative path named by one `+++ ...` diff file header line.

    Handles both the plain `+++ b/<path>` form and the C-quoted
    `+++ "b/<escaped>"` form git falls back to whenever a path holds a
    literal double quote or backslash -- the non-ASCII case is already
    avoided by running the diff under `core.quotepath=false` (see
    `added_lines`), but a real quote or backslash in a path still triggers
    quoting regardless of that setting.

    Raises:
        SecretScanError: naming `commit` and `line`, if neither form
            matches. A caller must never narrow its scan to whatever it
            could parse when a header cannot be interpreted.
    """
    plain_match = _DIFF_NEW_FILE_HEADER_PATTERN.match(line)
    if plain_match:
        return plain_match.group(1)
    quoted_match = _DIFF_NEW_FILE_HEADER_QUOTED_PATTERN.match(line)
    if quoted_match:
        unescaped = _unescape_git_quoted_path(quoted_match.group(1), commit=commit, header=line)
        if unescaped.startswith("b/"):
            return unescaped[len("b/") :]
    raise SecretScanError(
        f"ERROR: cannot parse the diff header for commit {commit}\n"
        f"added_lines could not interpret this 'git diff-tree' file header: {line!r}\n"
        "This scanner refuses to narrow its scan to whatever it could parse; "
        f"review the commit by hand with 'git show {commit}' before trusting "
        "this range scan."
    )


def _binary_post_image_lines(root: Path, commit: str, path: str) -> tuple[str, ...]:
    """`path`'s full post-image content at `commit`, split into lines.

    `git diff-tree -p` never prints the content of a file section it
    classifies as binary -- only a `Binary files ... differ` line, no
    hunks -- so `_parse_added_lines` calls this to read that content the
    same way `staged_blob` already reads the index: `git show
    <commit>:<path>`, the whole post-image blob, not a diff against it.
    That keeps a binary-classified addition scanned exactly as its staged
    counterpart already is (AC-FUNC-005: staged mode and range mode must
    not diverge).

    Raises:
        SecretScanError: naming `commit` and `path`, if `git show` cannot
            read the blob, or if the blob it read does not decode as
            UTF-8. Skipping an undecodable blob would let a credential
            through inside content the scanner declined to read, so this
            refuses the whole scan instead of narrowing it silently.
    """
    stdout = run_git(
        ["show", f"{commit}:{path}"],
        root=root,
        not_installed_message=lambda: (
            "ERROR: git is not installed\n"
            f"added_lines needs the git binary to read the blob {path} at "
            f"commit {commit} under {root} and none was found on PATH.\n"
            "Install git and ensure it is on PATH, then retry."
        ),
        failure_message=lambda stderr: (
            f"ERROR: cannot read blob {path} at commit {commit}\n"
            f"added_lines ran 'git show {commit}:{path}' in {root} and git "
            f"reported: {stderr}\n"
            f"Confirm {path} exists at commit {commit} in this repository."
        ),
    )
    try:
        return tuple(stdout.decode("utf-8").splitlines())
    except UnicodeDecodeError as exc:
        raise SecretScanError(
            f"ERROR: commit {commit} contains a blob that is not valid UTF-8\n"
            f"added_lines could not decode the binary-classified blob {path} "
            f"at {commit}: {exc}\n"
            "This scanner reads text content only; a binary file whose "
            "content is not valid UTF-8 cannot be scanned and must be "
            "reviewed by hand before this range is pushed."
        ) from exc


def _parse_added_lines(
    diff_text: str, *, commit: str, root: Path
) -> tuple[tuple[str, int, str], ...]:
    """`(path, post_image_line_number, text)` for every added line in `diff_text`.

    `diff_text` is `git diff-tree -p -U0`'s output: zero context means every
    non-header line is either an addition or a removal, never unrelated
    context that could be mistaken for one. The post-image line number
    comes from each hunk's `@@ -a,b +c,d @@` header and advances by one for
    every added line that follows it, so a finding names the line as it
    exists in the commit rather than an offset into the patch.

    A `+++ ...` line is read as a file header only while still inside a
    file's header block -- between its `diff --git` line and the first
    `@@` hunk header that follows. An added line whose own content starts
    with `++ ` reads back, once git prefixes it with the single `+` that
    marks every addition, as `+++ <that content>`; outside the header
    block that is content, never a header, and is scanned like any other
    added line rather than being mistaken for one and silently discarding
    the rest of the file.

    A file section whose post-image header is `+++ /dev/null` -- the
    delete side of a deletion, or the delete half of a rename, since this
    parser is fed diffs taken without rename detection -- leaves
    `current_path` unresolved rather than raising: that section has no
    post-image, so it contributes no `+` lines and there is nothing for an
    unresolved path to hide.

    A file section git classifies as binary (any NUL byte in its first
    8000 bytes) carries no `+++ `/hunk structure at all, only a `Binary
    files ... differ` line -- also read only while still inside a file's
    header block, the same guard the `+++ ` branch above uses, so an added
    line whose own content happens to start with that exact text is never
    mistaken for one. Its post-image side, resolved through
    `_diff_new_file_path` exactly as a `+++ ` header's path is, names
    `/dev/null` when the section has no post-image (a binary deletion, or
    the delete half of a rename), which contributes no lines for the same
    reason the `+++ /dev/null` case above does not; otherwise
    `_binary_post_image_lines` reads the whole post-image blob directly
    with `git show`, and every one of its lines is recorded as added,
    numbered from 1, rather than the section falling through unread.

    Raises:
        SecretScanError: naming `commit`, if a `+++ ` or binary file
            header names a path this parser cannot interpret (see
            `_diff_new_file_path`), if a binary section's post-image blob
            cannot be read or decoded (see `_binary_post_image_lines`), or
            if an added line is reached with no file path or hunk position
            resolved -- a diff shape this parser does not understand,
            never silently narrowed to whatever it could parse.
    """
    added: list[tuple[str, int, str]] = []
    current_path: str | None = None
    next_line_number: int | None = None
    in_file_header = False
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            current_path = None
            next_line_number = None
            in_file_header = True
            continue
        if in_file_header and line.startswith("+++ "):
            if line == _DIFF_DELETED_FILE_HEADER:
                current_path = None
                next_line_number = None
                in_file_header = False
                continue
            current_path = _diff_new_file_path(line, commit=commit)
            next_line_number = None
            continue
        if in_file_header and line.startswith("Binary files "):
            binary_match = _DIFF_BINARY_FILE_HEADER_PATTERN.match(line)
            if binary_match is None:
                raise SecretScanError(
                    f"ERROR: cannot parse the diff header for commit {commit}\n"
                    "added_lines could not interpret this 'git diff-tree' "
                    f"binary-file marker: {line!r}\n"
                    "This scanner refuses to narrow its scan to whatever it "
                    f"could parse; review the commit by hand with 'git show "
                    f"{commit}' before trusting this range scan."
                )
            in_file_header = False
            destination = binary_match.group("dst")
            if destination == "/dev/null":
                current_path = None
                next_line_number = None
                continue
            binary_path = _diff_new_file_path(f"+++ {destination}", commit=commit)
            for line_number, text in enumerate(
                _binary_post_image_lines(root, commit, binary_path), start=1
            ):
                added.append((binary_path, line_number, text))
            current_path = None
            next_line_number = None
            continue
        if line.startswith("--- "):
            continue
        hunk_match = _HUNK_HEADER_PATTERN.match(line)
        if hunk_match:
            next_line_number = int(hunk_match.group(1))
            in_file_header = False
            continue
        if line.startswith("+"):
            if current_path is None or next_line_number is None:
                raise SecretScanError(
                    f"ERROR: cannot parse the diff for commit {commit}\n"
                    "added_lines reached an added line before resolving both "
                    f"a file path and a hunk position: {line!r}\n"
                    "This is either a 'git diff-tree' output shape this "
                    "parser does not understand or a parser state bug; "
                    f"review the commit by hand with 'git show {commit}' "
                    "before trusting this range scan."
                )
            added.append((current_path, next_line_number, line[1:]))
            next_line_number += 1
        elif line.startswith("-") or line.startswith("\\"):
            continue
    return tuple(added)


def added_lines(root: Path, commit: str) -> tuple[tuple[str, int, str], ...]:
    """`(path, post_image_line_number, text)` for every line `commit` adds.

    Diffed against `commit`'s first parent with zero context
    (`git diff-tree -p -U0 -r`), or against the empty tree when `commit` is
    a root commit, so a repository's first commit is scanned rather than
    skipped for want of a parent (spec Section 4.6, AC-FUNC-002). For an
    ordinary commit that diff is the one line of work it adds on top of its
    parent. For a merge commit it is `git diff-tree -p <first-parent>
    <merge>`'s actual output: everything the merge's tree holds that its
    first parent's tree does not, which is the whole side branch the first
    time a merge with unresolved (fast-forwardable) content is diffed this
    way, not only a conflict resolution. That over-reports rather than
    under-reports -- a credential the side branch already introduced is
    reported twice, once against the commit that added it and once against
    the merge -- which is the safe direction for a scanner whose job is
    never missing a credential that reaches history.

    Run under `-c core.quotepath=false`, so a diff header naming a path
    with a non-ASCII byte prints that byte literally instead of quoting and
    octal-escaping the whole path (`_diff_new_file_path` still handles the
    quoted form, for a path holding a literal quote or backslash, which
    triggers quoting regardless of this setting).

    Raises:
        SecretScanError: if the `git` binary is not on PATH; if `commit`
            cannot be diffed; if the diff `git diff-tree` produced does not
            decode as UTF-8 -- which happens only for a file section git
            classified as text but whose content is not valid UTF-8, since
            a section it classified as binary never prints its bytes into
            this diff at all, only a `Binary files ... differ` marker
            (see `_parse_added_lines`) -- naming the commit rather than
            silently narrowing the scan to whatever did decode; or if a
            file header or an added line inside that diff cannot be
            parsed (see `_parse_added_lines`), for the same reason.
    """
    parent = _first_parent(root, commit)
    base = parent if parent is not None else empty_tree_object_id(root)
    stdout = run_git(
        ["diff-tree", "-p", "--no-color", "-U0", "-r", base, commit],
        root=root,
        config={"core.quotepath": "false"},
        not_installed_message=lambda: (
            "ERROR: git is not installed\n"
            f"added_lines needs the git binary to diff {commit} under {root} "
            "and none was found on PATH.\n"
            "Install git and ensure it is on PATH, then retry."
        ),
        failure_message=lambda stderr: (
            f"ERROR: cannot diff commit {commit}\n"
            f"added_lines ran 'git diff-tree -p --no-color -U0 -r {base} "
            f"{commit}' in {root} and git reported: {stderr}\n"
            f"Confirm {commit} exists in this repository."
        ),
    )
    try:
        diff_text = stdout.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SecretScanError(
            f"ERROR: commit {commit} contains a blob that is not valid UTF-8\n"
            f"added_lines could not decode the diff for {commit}: {exc}\n"
            "This scanner reads text content only; a file in this commit "
            "that git diffed as text but whose content is not valid UTF-8 "
            "cannot be scanned and must be reviewed by hand before this "
            "range is pushed."
        ) from exc
    return _parse_added_lines(diff_text, commit=commit, root=root)


@dataclass(frozen=True)
class RangeFinding:
    """One `Finding` together with the commit and repo-relative path it came from."""

    commit: str
    path: str
    finding: Finding


@dataclass(frozen=True)
class RangeReport:
    """The range scanned, how many commits it held, and every finding from `scan_range`."""

    revision_range: str
    range_base: str
    commit_count: int
    findings: tuple[RangeFinding, ...]


def scan_range(root: Path, revision_range: str) -> RangeReport:
    """Scan every commit in `revision_range`, oldest first; the full range report.

    Each commit's added lines (`added_lines`) are grouped by path and
    scanned with the same `scan_lines` and `PATTERNS` registry staged mode
    uses (AC-FUNC-005): no second pattern list exists for history. A
    credential added in one commit and removed in a later commit is still
    reported, attributed to the commit that added it (AC-FUNC-001), because
    each commit is diffed and scanned independently rather than the range
    being collapsed into one net diff.
    """
    base, _tip = _parse_range(revision_range)
    commits = commits_in_range(root, revision_range)
    shell_env = shell_env_lines(root)
    catalog_secret_names: tuple[str, ...] = ()
    sources = ScanSources(shell_env_lines=shell_env, catalog_secret_names=catalog_secret_names)

    findings: list[RangeFinding] = []
    for commit in commits:
        lines_by_path: dict[str, list[tuple[int, str]]] = {}
        for path, line_number, text in added_lines(root, commit):
            lines_by_path.setdefault(path, []).append((line_number, text))
        findings.extend(
            RangeFinding(commit=commit, path=path, finding=finding)
            for path, finding in _grouped_findings(lines_by_path, sources)
        )

    return RangeReport(
        revision_range=revision_range,
        range_base=base,
        commit_count=len(commits),
        findings=tuple(findings),
    )


def render_range_report(report: RangeReport) -> str:
    """The `[LINT]` range report text for `report` (spec Section 4.6's worked example shape).

    The header names the scanned range and how many commits it held, even
    when the range is empty, so "zero commits were scanned" is a fact the
    header states rather than an empty screen the operator has to
    interpret. Each finding names the commit, the path, the line and the
    detector description; the value itself comes from `render_finding`, so
    redaction cannot diverge between staged mode and range mode. When any
    finding exists, the report closes with the sentence Section 4.6
    requires -- the value is in history, so removing it now is not enough
    -- followed by both remedies: an interactive rebase to edit the
    offending commit out, or rotating the exposed value and pushing
    deliberately with a recorded approval.
    """
    lines = [
        f"[LINT] secrets in pushed range {report.revision_range}",
        f"  commits scanned: {report.commit_count}",
    ]
    for item in report.findings:
        detector = DETECTORS_BY_ID[item.finding.detector_id]
        rendered = render_finding(item.finding, detector)
        lines.append(
            f"  ERROR commit {item.commit}  {item.path}:{item.finding.line_number}  "
            f"{detector.description}"
        )
        lines.append(f"        {rendered}")
    if report.findings:
        lines.append("  The value is in history, so removing it now is not enough.")
        lines.append(
            f"  Rewrite:  git rebase -i {report.range_base}    "
            "(edit the offending commit, remove, continue)"
        )
        lines.append(
            "  Or:       rotate the exposed value and push deliberately with a recorded approval."
        )
    return "\n".join(lines)
