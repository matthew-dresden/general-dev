"""The secret scanner: every detector named in spec Section 4.6, in one place.

Detects, in scanned line content: AWS access key identifiers (`AKIA` and
`ASIA` prefixes), AWS secret access key assignments, private key blocks,
GitHub tokens (`ghp_`, `gho_`, `github_pat_`), Slack tokens (`xoxb-`,
`xoxp-`), bearer tokens, SSO portal URLs, twelve-digit account identifiers,
EC2 instance identifiers, every catalog secret name, and any line also
present in the developer's own `shell.env`.

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

`staged_paths`, `staged_blob` and `shell_env_lines` (E2-F1-S1-T2) are the
impure shell around `scan_lines`: they read the git index and the
developer's `shell.env` from disk, and `run_staged_scan` composes them into
one `LintReport` that `render_lint_report` turns into the text
`devcontainer_config.cli` prints. None of the three raise on a caller's
behalf silently: an unreadable or undecodable source is a `SecretScanError`,
never an empty result standing in for one.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from re import Pattern
from types import MappingProxyType
from typing import Protocol

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
    being detected is line identity, not a substring shape.
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


def _run_git(
    args: Sequence[str],
    *,
    root: Path,
    not_installed_message: Callable[[], str],
    failure_message: Callable[[str], str],
) -> bytes:
    """Run `git <args>` under `root`; the raw stdout bytes on success.

    `staged_paths` and `staged_blob` both need "run a git subprocess and
    turn a failure into a `SecretScanError`", differing only in the message
    each raises, so that shape lives here once instead of being duplicated
    at each call site (or, worse, omitted at one of them). The caller
    supplies the two message builders because only the caller knows which
    git invocation failed and what the operator should do about it.

    Raises:
        SecretScanError: built from `not_installed_message` if the `git`
            binary is not on PATH, or from `failure_message` (given git's
            decoded stderr) if git exits non-zero.
    """
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            check=True,
        )
    except FileNotFoundError as exc:
        raise SecretScanError(not_installed_message()) from exc
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode("utf-8", errors="replace").strip() if exc.stderr else ""
        raise SecretScanError(failure_message(stderr)) from exc
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
    stdout = _run_git(
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
    stdout = _run_git(
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


def shell_env_lines(root: Path) -> tuple[str, ...]:
    """Non-blank lines of `root`'s `shell.env`, or an empty tuple when absent.

    Resolved through `repo.private_paths` rather than a new root walk
    (AC-3.1). Absence is the normal state of a fresh clone (spec Section
    1.7) and contributes zero lines; an unreadable file that does exist is a
    different condition, so it is not folded into the same empty result.

    Raises:
        SecretScanError: if `shell.env` exists but cannot be opened. Treating
            an unreadable source as an empty one would silently narrow the
            scan without telling the operator why.
    """
    path = repo.private_paths(root)[repo.SHELL_ENV]
    if not path.is_file():
        return ()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SecretScanError(
            f"ERROR: cannot read {path}\n"
            f"shell_env_lines found {path} but could not open it: {exc}\n"
            "Fix the file's permissions, then retry."
        ) from exc
    return tuple(line for line in text.splitlines() if line.strip())


@dataclass(frozen=True)
class StagedFinding:
    """One `Finding` together with the repo-relative staged path it came from."""

    path: str
    finding: Finding


@dataclass(frozen=True)
class LintReport:
    """Every source's entry count and every finding from one `run_staged_scan` call."""

    staged_path_count: int
    shell_env_line_count: int
    catalog_secret_name_count: int
    findings: tuple[StagedFinding, ...]


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
    shell_env = shell_env_lines(root)
    catalog_secret_names: tuple[str, ...] = ()
    sources = ScanSources(shell_env_lines=shell_env, catalog_secret_names=catalog_secret_names)

    findings: list[StagedFinding] = []
    for path in paths:
        lines = staged_blob(root, path)
        findings.extend(
            StagedFinding(path=path, finding=finding) for finding in scan_lines(lines, sources)
        )

    return LintReport(
        staged_path_count=len(paths),
        shell_env_line_count=len(shell_env),
        catalog_secret_name_count=len(catalog_secret_names),
        findings=tuple(findings),
    )


def render_lint_report(report: LintReport) -> str:
    """The `[LINT]` report text for `report`: a header, then one line per finding.

    The header names every source and its entry count (AC-FUNC-003), even
    when nothing was staged, so "zero paths were scanned" is a fact the
    header states rather than an empty screen the operator has to interpret.
    Each finding line names the staged path, the line number and the
    detector description (AC-FUNC-004); the value itself comes from
    `render_finding`, which redacts every non-printable detector's match.
    """
    lines = [
        "[LINT] secrets in staged content",
        f"  staged paths scanned: {report.staged_path_count}",
        f"  shell.env lines: {report.shell_env_line_count}",
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
