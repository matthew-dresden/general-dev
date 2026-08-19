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
AWS, no docker and no network at all (AC-10.14). `make lint-secrets`
(E2-F1-S1-T2) and the pushed-range scan (E2-F1-S2-T1) are the impure shells
that read real content and call this module; this module makes none of those
decisions itself.

Every finding also carries a rendering decision. The SSO portal URL, the
account identifier and the EC2 instance identifier are printable, exactly as
the worked report in spec Section 4.6 prints an `awsapps.com/start` URL in
full; every other detector is credential-bearing and is redacted:
`render_finding` emits only the detector-declared safe prefix of the match
plus the length of what it withheld, so a report stays actionable without
ever returning a full credential value.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from re import Pattern
from types import MappingProxyType
from typing import Protocol

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
