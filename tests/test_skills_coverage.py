"""The mechanical coverage guarantee for the skill roster (U2).

Every action a developer can take in this repository is either a make
target or a named flow, and every one of them must be reachable through a
skill: a developer (or an agent) holding only the roster should never face
an action no skill knows how to drive. This module proves that guarantee
mechanically, in three layers:

- **Target coverage.** The union of every target `make help` advertises
  and every target `tests/data/help-unadvertised.txt` declares with a
  reason is the complete surface of real make targets (the same two
  sources `tests/test_help_snapshot.py`'s AC-FUNC-003 correspondence
  check uses, so a target hidden from one is still caught by the other).
  Each target in that union must be referenced by at least one
  `SKILL.md` under `.agents/skills` -- either spelled `make <target>`
  anywhere in the body, or named bare inside the skill's `## Invocation
  map` section or a code span/block, where a bare name is a deliberate
  reference rather than an English word that happens to collide with a
  target (`test`, `check`, `status`, `shell`). Failures list every
  uncovered target by name.
- **Flow coverage.** `docs/skills.md`'s `## Flows` section lists the
  named no-target flows -- recurring developer actions that are answers,
  judgments or multi-step procedures rather than a single make target.
  The correspondence is pinned equally in both directions: every flow
  listed in the roster must be referenced by name in every skill its
  row names, and every flow reference in any `SKILL.md` must be listed
  in the roster. The reference spelling is the backticked
  `` `flow: <name>` `` marker, defined once in `docs/skills.md`'s Flows
  section, so both directions are exact string matches rather than
  prose-judgment calls.
- **Structure.** Every `SKILL.md` carries exactly one `## Invocation
  map` heading: the one section where the make targets a skill drives
  are declared. The roster's bodies use the heading for every skill
  that has a target-mapping table (the newer skills were authored with
  it, and the older ones' `## Decision`- and `## Operations`-shaped
  tables were normalized to it in the same change that added this
  suite), so one heading name is pinned across all twenty-one skills
  and a body drifting back to a private heading name fails here.

The helpers are imported rather than redeclared, the same reuse
`tests/test_help_snapshot.py` documents for `_makefile_text`:
`_run_make_help` (which applies `_make_environment` to the subprocess it
spawns), `_normalize_make_help_output`, `_advertised_targets` and
`_load_unadvertised_declarations` come from `tests/test_help_snapshot.py`,
and `_strip_frontmatter` and `_parse_markdown_table` come from
`tests/test_skill_lint.py` -- both already parse the exact formats this
module needs, and a second copy of either could drift from its sibling.

The negative cases run the checkers over synthesized skill-body and
roster text built in memory, asserting the exact finding each defect
produces; nothing under `.agents/skills`, `docs/` or `tests/data/` is
ever written. Like the rest of the suite the module is hermetic: the one
subprocess is `make help`, which prints and returns, bounded by the same
environment-configurable timeout `tests/test_help_snapshot.py` already
uses; no docker, no AWS, no network.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from devcontainer_config.repo import find_root
from test_help_snapshot import (
    UNADVERTISED_FILE,
    _advertised_targets,
    _load_unadvertised_declarations,
    _normalize_make_help_output,
    _run_make_help,
)
from test_skill_lint import _parse_markdown_table, _strip_frontmatter

SKILLS_RELATIVE = ".agents/skills"
DOCS_SKILLS_RELATIVE = "docs/skills.md"

# The one heading name pinned for the make-target section of every
# SKILL.md. The bodies were normalized to this spelling (the majority
# convention the roster's own invocation tables already used) in the same
# change that added this suite; a body using a private heading name for
# its target map fails `test_every_skill_declares_exactly_one_...` below.
_INVOCATION_HEADING = "## Invocation map"

# The roster side of the flow correspondence.
_FLOWS_HEADING = "## Flows"
_FLOWS_TABLE_HEADER = "| Flow | Covering skill(s) |"

# The named no-target flows, pinned exactly: a flow added to or removed
# from `docs/skills.md` must land here in the same change, or this module
# names the drift -- the same complete-pin discipline
# `tests/test_docs_environment_setup.py` applies to the runbook's claims.
EXPECTED_FLOWS: tuple[str, ...] = (
    "keybindings setup",
    "ENGINE multi-engine addressing",
    "INSTANCE_ID link recovery",
    "credential expiry troubleshooting",
    "scanner-blocked-commit remedy",
    "opencode/claude agent setup pointers",
)

# How a skill body references a flow by name. Backticks make the
# reference an exact string on both sides of the correspondence, which is
# what lets the reverse direction be checked mechanically at all.
_FLOW_MARKER_PATTERN = re.compile(r"`flow: ([^`\n]+)`")


# A make target spelled with its `make ` prefix, not preceded or followed
# by a character that would make it a different word or a longer name
# (`make instance-plan` must not satisfy `plan`; `make rebuild` must not
# satisfy `build`). The same boundary discipline
# `tests/test_skill_lint.py`'s `_REFERENCE_PATTERN` applies to skill names.
def _make_prefixed_pattern(target: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![\w-])make {re.escape(target)}(?![\w-])")


def _bare_pattern(target: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![\w-]){re.escape(target)}(?![\w-])")


# Floors for the two extraction sides, in the spirit of
# `tests/test_docs_environment_setup.py`'s `_REQUIRED_RUNBOOK_TARGETS`:
# if a help-output or declarations-file reformat starves an extraction,
# the missing floor names the extraction as stale instead of letting an
# empty or shrunken union pass vacuously.
_REQUIRED_ADVERTISED_TARGETS: frozenset[str] = frozenset(
    {
        "init",
        "build",
        "lint",
        "test",
        "validate",
        "instance-deploy",
        "push-creds",
        "verify-container",
        "cert-status",
        "hooks-install",
    }
)
_REQUIRED_DECLARED_TARGETS: frozenset[str] = frozenset({"help", "hooks-uninstall", "shell"})

# Stands in for the machine-dependent docker context names
# `_normalize_make_help_output` substitutes over. This module's target
# extraction never matches a context name, so a sentinel that cannot
# occur in the output replaces it rather than this suite sourcing
# `config.env` for values it would only throw away.
_NON_OCCURRING_SENTINEL = "\x00never-occurs\x00"


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------


def _invocation_map_section(body: str) -> str:
    """`body`'s `## Invocation map` section, up to the next `##` heading.

    Empty when the body has no such heading; the heading's own rule
    reports that, so this function stays a pure extractor.
    """
    start = re.search(rf"^{re.escape(_INVOCATION_HEADING)}\s*$", body, re.MULTILINE)
    if start is None:
        return ""
    rest = body[start.end() :]
    next_heading = re.search(r"^## ", rest, re.MULTILINE)
    return rest[: next_heading.start()] if next_heading else rest


def _code_span_text(body: str) -> str:
    """Every inline code span and fenced code block of `body`, joined.

    A bare target name counts as a reference only inside one of these
    (or inside the `## Invocation map` section), never in running prose:
    the words `test`, `check`, `status`, `shell` and `start` are ordinary
    English, and prose collisions must not fake coverage.
    """
    spans = list(re.findall(r"`([^`\n]+)`", body))
    spans.extend(block for block in re.findall(r"```[^\n]*\n(.*?)```", body, re.DOTALL))
    return "\n".join(spans)


def _flow_names_from_marker(body: str) -> set[str]:
    return set(_FLOW_MARKER_PATTERN.findall(body))


# ---------------------------------------------------------------------------
# The checkers
# ---------------------------------------------------------------------------


def check_target_coverage(skill_bodies: dict[str, str], targets: set[str]) -> list[str]:
    """One finding per target in `targets` that no skill body references.

    A target is referenced by a skill when its body spells it
    `make <target>` anywhere, or names it bare inside the skill's
    `## Invocation map` section or a code span/block. Never raises:
    every uncovered target becomes its own finding, so one run reports
    the whole uncovered set rather than stopping at the first.
    """
    owners: dict[str, set[str]] = {target: set() for target in targets}
    for name, body in skill_bodies.items():
        invocation_map = _invocation_map_section(body)
        code_text = _code_span_text(body)
        for target in targets:
            if (
                _make_prefixed_pattern(target).search(body)
                or _bare_pattern(target).search(invocation_map)
                or _bare_pattern(target).search(code_text)
            ):
                owners[target].add(name)
    return [
        f"make target '{target}' is referenced by no SKILL.md under "
        f"{SKILLS_RELATIVE} (spelled `make {target}`, or named bare in a "
        "'## Invocation map' section or code block)"
        for target in sorted(owners)
        if not owners[target]
    ]


def check_invocation_map_headings(skill_bodies: dict[str, str]) -> list[str]:
    """One finding per skill whose body lacks exactly one invocation-map heading.

    The heading is the roster-wide convention for the make-target section
    (see this module's docstring); zero occurrences means the skill has no
    pinned home for its targets, and more than one means a second,
    competing map has appeared.
    """
    findings: list[str] = []
    for name, body in sorted(skill_bodies.items()):
        count = len(re.findall(rf"^{re.escape(_INVOCATION_HEADING)}\s*$", body, re.MULTILINE))
        if count != 1:
            findings.append(
                f"{SKILLS_RELATIVE}/{name}/SKILL.md: has {count} "
                f"{_INVOCATION_HEADING!r} heading(s), expected exactly 1"
            )
    return findings


def _flows_rows(docs_skills_path: Path, docs_text: str) -> tuple[list[dict[str, str]], list[str]]:
    """`(rows, findings)` for `docs_text`'s Flows table, plus a section check.

    A missing `## Flows` section is its own finding (the roster side of
    the correspondence must exist for the skills side to be checked
    against); a present section with a malformed table yields the
    table parser's own findings.
    """
    if not re.search(rf"^{re.escape(_FLOWS_HEADING)}\s*$", docs_text, re.MULTILINE):
        return [], [f"{docs_skills_path}: has no {_FLOWS_HEADING!r} section"]
    rows, findings = _parse_markdown_table(docs_skills_path, docs_text, _FLOWS_TABLE_HEADER)
    return rows, findings


def check_flows(
    docs_skills_path: Path,
    docs_text: str,
    skill_bodies: dict[str, str],
    roster_names: set[str],
) -> list[str]:
    """Every Flows-correspondence finding, in both directions.

    Roster side: the section exists, its flow-name column is exactly
    `EXPECTED_FLOWS`, and every covering skill it names is on the roster.
    Forward: every flow is referenced by every skill its row names, via
    the `` `flow: <name>` `` marker. Reverse: every marker in any skill
    body names a flow the roster lists -- a skill inventing a flow the
    roster does not know fails here, so the two sides cannot drift apart
    in either direction.
    """
    findings: list[str] = []
    rows, row_findings = _flows_rows(docs_skills_path, docs_text)
    findings.extend(row_findings)

    roster_flows = {row["Flow"].strip("`") for row in rows if row.get("Flow")}
    for flow in sorted(set(EXPECTED_FLOWS) - roster_flows):
        findings.append(
            f"{docs_skills_path}: the {_FLOWS_HEADING!r} section is missing flow '{flow}'"
        )
    for flow in sorted(roster_flows - set(EXPECTED_FLOWS)):
        findings.append(
            f"{docs_skills_path}: the {_FLOWS_HEADING!r} section lists "
            f"'{flow}', which is not one of the pinned flows"
        )

    covering: dict[str, set[str]] = {}
    for row in rows:
        flow = row.get("Flow", "").strip("`")
        named = set(row.get("Covering skill(s)", "").split())
        unknown = named - roster_names
        for name in sorted(unknown):
            findings.append(
                f"{docs_skills_path}: flow '{flow}' names covering skill "
                f"'{name}', which is not on the roster"
            )
        covering[flow] = named

    for flow in sorted(covering):
        for name in sorted(covering[flow] & roster_names):
            body = skill_bodies.get(name)
            marker = f"`flow: {flow}`"
            if body is None:
                findings.append(
                    f"{docs_skills_path}: flow '{flow}' names covering skill "
                    f"'{name}', which has no SKILL.md"
                )
            elif marker not in body:
                findings.append(
                    f"{SKILLS_RELATIVE}/{name}/SKILL.md: flow '{flow}' lists "
                    f"this skill as covering, but the body never carries the "
                    f"reference {marker}"
                )

    for name, body in sorted(skill_bodies.items()):
        for flow in sorted(_flow_names_from_marker(body) - set(covering)):
            findings.append(
                f"{SKILLS_RELATIVE}/{name}/SKILL.md: references flow "
                f"'{flow}', which the {_FLOWS_HEADING!r} section of "
                f"{docs_skills_path} does not list"
            )
    return findings


# ---------------------------------------------------------------------------
# Fixtures over the real repository
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def repo_root() -> Path:
    return find_root(Path(__file__).resolve().parent)


@pytest.fixture(scope="module")
def advertised_targets(repo_root: Path) -> set[str]:
    """Every target the live `make help` output advertises, floors checked."""
    raw = _run_make_help(repo_root)
    normalized = _normalize_make_help_output(
        raw,
        repo_dir=repo_root.name,
        local_context=_NON_OCCURRING_SENTINEL,
        remote_context=_NON_OCCURRING_SENTINEL,
    )
    targets = _advertised_targets(normalized)
    missing = set(_REQUIRED_ADVERTISED_TARGETS) - targets
    assert not missing, (
        "`make help` no longer advertises target(s) "
        f"{sorted(missing)!r}; the advertised-target extraction has gone "
        "stale against the help recipe's own output."
    )
    return targets


@pytest.fixture(scope="module")
def declared_targets() -> set[str]:
    """Every target `tests/data/help-unadvertised.txt` declares, floors checked."""
    declarations = _load_unadvertised_declarations(UNADVERTISED_FILE)
    missing = set(_REQUIRED_DECLARED_TARGETS) - declarations.keys()
    assert not missing, (
        f"{UNADVERTISED_FILE} no longer declares target(s) "
        f"{sorted(missing)!r}; the declarations extraction has gone stale."
    )
    return set(declarations)


@pytest.fixture(scope="module")
def surface_targets(advertised_targets: set[str], declared_targets: set[str]) -> set[str]:
    """The union every skill must cover: advertised plus declared-unadvertised.

    These are the same two sources `tests/test_help_snapshot.py`'s
    AC-FUNC-003 check uses to account for every `.PHONY` target, so a
    target that reaches the Makefile but hides from both sides of this
    union is that test's failure first and this module's never; the union
    here is exactly the surface a developer can actually invoke.
    """
    return advertised_targets | declared_targets


@pytest.fixture(scope="module")
def skill_bodies(repo_root: Path) -> dict[str, str]:
    """`{skill name: body}` for every canonical SKILL.md, frontmatter stripped."""
    root = repo_root / SKILLS_RELATIVE
    bodies = {
        path.parent.name: _strip_frontmatter(path.read_text(encoding="utf-8"))
        for path in sorted(root.glob("*/SKILL.md"))
    }
    assert bodies, f"no SKILL.md files found under {root}"
    return bodies


@pytest.fixture(scope="module")
def roster_names(repo_root: Path) -> set[str]:
    """The skill names the canonical skills root actually holds."""
    root = repo_root / SKILLS_RELATIVE
    return {entry.name for entry in root.iterdir() if entry.is_dir()}


@pytest.fixture(scope="module")
def docs_skills_path(repo_root: Path) -> Path:
    return repo_root / DOCS_SKILLS_RELATIVE


@pytest.fixture(scope="module")
def docs_skills_text(docs_skills_path: Path) -> str:
    return docs_skills_path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Positive cases: the real repository is fully covered
# ---------------------------------------------------------------------------


def test_every_make_target_is_referenced_by_at_least_one_skill(
    skill_bodies: dict[str, str], surface_targets: set[str]
) -> None:
    """The mechanical guarantee: no real make target lacks a covering skill."""
    findings = check_target_coverage(skill_bodies, surface_targets)
    assert not findings, "\n".join(findings)


def test_every_skill_declares_exactly_one_invocation_map_heading(
    skill_bodies: dict[str, str],
) -> None:
    """The heading convention is pinned across the whole roster."""
    findings = check_invocation_map_headings(skill_bodies)
    assert not findings, "\n".join(findings)


def test_flows_section_lists_exactly_the_pinned_flows_with_roster_skills(
    docs_skills_path: Path,
    docs_skills_text: str,
    roster_names: set[str],
) -> None:
    """The roster side: the section exists, names the pinned flows, and every
    covering skill it names is a real roster entry.
    """
    rows, findings = _flows_rows(docs_skills_path, docs_skills_text)
    assert not findings, "\n".join(findings)
    flows = {row["Flow"].strip("`") for row in rows}
    assert flows == set(EXPECTED_FLOWS)
    for row in rows:
        named = set(row["Covering skill(s)"].split())
        assert named, f"flow row {row!r} names no covering skill"
        assert named <= roster_names, (
            f"flow row {row!r} names skill(s) {sorted(named - roster_names)!r} "
            "that are not on the roster"
        )


def test_every_roster_flow_is_referenced_by_every_covering_skill(
    docs_skills_path: Path,
    docs_skills_text: str,
    skill_bodies: dict[str, str],
    roster_names: set[str],
) -> None:
    """Forward direction: roster flows appear by name in the skills that cover them."""
    findings = check_flows(docs_skills_path, docs_skills_text, skill_bodies, roster_names)
    forward_only = [finding for finding in findings if "never carries the reference" in finding]
    assert not forward_only, "\n".join(forward_only)


def test_no_skill_names_a_flow_the_roster_does_not_list(
    docs_skills_path: Path,
    docs_skills_text: str,
    skill_bodies: dict[str, str],
    roster_names: set[str],
) -> None:
    """Reverse direction: a skill inventing a flow fails, so the sides match."""
    findings = check_flows(docs_skills_path, docs_skills_text, skill_bodies, roster_names)
    reverse_only = [finding for finding in findings if "does not list" in finding]
    assert not reverse_only, "\n".join(reverse_only)


# ---------------------------------------------------------------------------
# Negative cases: each checker's failure path, against synthesized text
# ---------------------------------------------------------------------------


def test_target_coverage_finding_names_each_uncovered_target() -> None:
    """A target no skill references produces one finding naming the target.

    Built from a body set that covers every target except one, so the
    finding isolates that target alone rather than a broken extraction.
    """
    targets = {"build", "proxy-stop"}
    bodies = {"gd-sample": "Run `make build` to create the container.\n"}
    findings = check_target_coverage(bodies, targets)
    assert len(findings) == 1
    assert "make target 'proxy-stop' is referenced by no SKILL.md" in findings[0]


def test_prose_collision_does_not_cover_a_target() -> None:
    """A bare target-shaped English word in prose is not a reference.

    `test` and `check` are ordinary words; a body whose prose contains
    them (outside the invocation map and any code span) covers neither
    target, which is why the bare-name rule is scoped the way it is.
    """
    body = (
        "We will check the result and test the outcome, then start over.\n"
        "## Invocation map\n\n"
        "Next heading follows.\n\n## Failure semantics\n"
    )
    findings = check_target_coverage({"gd-sample": body}, {"test", "check"})
    assert len(findings) == 2
    assert all("is referenced by no SKILL.md" in finding for finding in findings)


def test_bare_target_name_in_the_invocation_map_is_a_reference() -> None:
    """The scoping cuts both ways: a bare name inside the map is coverage."""
    body = (
        "Intro prose mentioning test and check freely.\n"
        "## Invocation map\n\n"
        "| Step | What this skill runs |\n"
        "|---|---|\n"
        "| Verify | `test` |\n"
        "| Guard | `check` |\n"
    )
    assert check_target_coverage({"gd-sample": body}, {"test", "check"}) == []


def test_missing_invocation_map_heading_is_a_finding() -> None:
    """A skill body without the pinned heading is named by the structure rule."""
    findings = check_invocation_map_headings({"gd-sample": "# gd-sample\n\nNo map here.\n"})
    assert len(findings) == 1
    assert "gd-sample" in findings[0]
    assert "expected exactly 1" in findings[0]


def test_duplicated_invocation_map_heading_is_a_finding() -> None:
    """A second, competing invocation map in one body is its own finding."""
    body = f"# gd-sample\n\n{_INVOCATION_HEADING}\n\ntable\n\n{_INVOCATION_HEADING}\n"
    findings = check_invocation_map_headings({"gd-sample": body})
    assert len(findings) == 1
    assert "has 2" in findings[0]


def _flows_docs_text(flows: list[str]) -> str:
    """A valid Flows section over `flows`, one synthetic covering skill per flow.

    The baseline every flows negative case starts from: the covering skill
    for each flow is named `gd-covering-<index>` and its body carries that
    flow's marker, so a mutation breaks exactly one rule and the finding
    list isolates it.
    """
    rows = "\n".join(f"| `{flow}` | gd-covering-{index} |" for index, flow in enumerate(flows))
    return f"{_FLOWS_HEADING}\n\n{_FLOWS_TABLE_HEADER}\n|---|---|\n{rows}\n"


def _flows_marker_bodies(flows: list[str]) -> dict[str, str]:
    """`{skill name: body}` pairing each `_flows_docs_text` row with its marker."""
    return {f"gd-covering-{index}": f"`flow: {flow}`\n" for index, flow in enumerate(flows)}


def test_missing_flows_section_is_a_finding(docs_skills_path: Path) -> None:
    """A roster without the Flows section fails before anything else can match.

    The section's own finding comes first, and each pinned flow the absent
    section cannot list adds its own -- the checker reports everything at
    once, the same contract `tests/test_skill_lint.py`'s `check_plugin`
    uses.
    """
    findings = check_flows(docs_skills_path, "# Skills\n\nroster table only\n", {}, {"gd-sample"})
    assert len(findings) == len(EXPECTED_FLOWS) + 1
    assert f"has no {_FLOWS_HEADING!r} section" in findings[0]


def test_flows_drift_is_a_finding_in_both_directions(docs_skills_path: Path) -> None:
    """One drifted roster row and one invented skill marker each get a finding.

    Built from the valid baseline with exactly two mutations: the roster
    drops one pinned flow and adds an unpinned one, and a second skill
    references a flow the roster does not list -- the three failure shapes
    the bidirectional rule exists to catch, each named by its own finding.
    """
    dropped, added = EXPECTED_FLOWS[0], "an unpinned flow"
    mutated = [flow for flow in EXPECTED_FLOWS if flow != dropped] + [added]
    # The dropped flow's covering skill is removed with it: the point of
    # this case is the roster-side drift and the invented marker, not the
    # reverse finding a still-referencing body would also (correctly) raise.
    skill_bodies = _flows_marker_bodies(mutated)
    skill_bodies["gd-inventor"] = "`flow: an invented flow`\n"
    findings = check_flows(
        docs_skills_path, _flows_docs_text(mutated), skill_bodies, set(skill_bodies)
    )
    assert len(findings) == 3
    assert any(f"missing flow '{dropped}'" in finding for finding in findings)
    assert any(
        f"lists '{added}', which is not one of the pinned flows" in finding for finding in findings
    )
    assert any(
        "references flow 'an invented flow'" in finding and "does not list" in finding
        for finding in findings
    )


def test_covering_skill_without_the_marker_is_a_finding(docs_skills_path: Path) -> None:
    """A flow row naming a skill whose body lacks the marker fails forward."""
    docs_text = _flows_docs_text(list(EXPECTED_FLOWS))
    skill_bodies = _flows_marker_bodies(list(EXPECTED_FLOWS))
    skill_bodies["gd-covering-0"] = skill_bodies["gd-covering-0"].replace(
        f"`flow: {EXPECTED_FLOWS[0]}`", "prose without the marker"
    )
    findings = check_flows(docs_skills_path, docs_text, skill_bodies, set(skill_bodies))
    assert len(findings) == 1
    assert "never carries the reference" in findings[0]
    assert f"`flow: {EXPECTED_FLOWS[0]}`" in findings[0]
