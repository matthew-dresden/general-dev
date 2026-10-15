"""`README.md` describes only what exists, and names no removed surface.

A README that still describes a removed transport is worse than one that says
nothing: a reader follows it, the command fails, and they cannot tell whether
they are holding it wrong or reading a stale document. These assertions cover
the ways that happens -- a `make` target or skill named here that does not
exist, and a removed identifier described as though it were still available.

The README carries no change history: it documents the current state only,
so the removed-identifier check is blanket, with no section exempted.

Hermetic: reads files, runs nothing, and needs no docker, AWS or network.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from conftest import _makefile_text

_REPO_ROOT = Path(__file__).resolve().parent.parent
_README = _REPO_ROOT / "README.md"
# The canonical, agent-agnostic skills home; every skill name is `gd-`-prefixed.
_SKILLS_DIR = _REPO_ROOT / ".agents" / "skills"

# A bare cross-reference: `gd-` plus at least one name segment, not part of a
# longer word. The README names skills in this bare form, the same form every
# SKILL.md cross-reference uses.
_SKILL_NAME_IN_TEXT = re.compile(r"(?<![\w-])gd-[a-z0-9]+(?:-[a-z0-9]+)*(?![\w-])")


def _readme() -> str:
    return _README.read_text(encoding="utf-8")


def test_every_make_target_named_in_the_readme_exists() -> None:
    """AC-TEST-002: a documented target that the Makefile does not define is a dead end."""
    makefile = _makefile_text()
    defined = set(re.findall(r"^([A-Za-z0-9_.-]+):(?!=)", makefile, re.MULTILINE))
    assert defined, "no targets parsed from the Makefile; this assertion would be vacuous"
    missing = []
    for line_number, line in enumerate(_readme().splitlines(), start=1):
        for target in re.findall(r"`make ([a-z][a-z0-9-]*)", line):
            if target not in defined:
                missing.append(f"line {line_number}: make {target}")
    assert not missing, f"README names make targets the Makefile does not define: {missing}"


def test_every_skill_named_in_the_readme_exists() -> None:
    """AC-TEST-003: a documented skill must resolve to a SKILL.md (ref)."""
    named = sorted(set(_SKILL_NAME_IN_TEXT.findall(_readme())))
    assert named, "the README names no skills; the skill route would be undocumented"
    missing = [
        f"{name} (expected {_SKILLS_DIR.relative_to(_REPO_ROOT)}/{name}/SKILL.md)"
        for name in named
        if not (_SKILLS_DIR / name / "SKILL.md").is_file()
    ]
    assert not missing, f"README names skills that do not exist: {missing}"


@pytest.mark.parametrize("mode", ["local", "remote"])
def test_each_mode_has_its_own_section_naming_both_routes(mode: str) -> None:
    """AC-DOC-002, AC-DOC-003, AC-TEST-004: two sections, each with both routes."""
    text = _readme()
    heading = f"## Quick start, {mode}"
    assert heading in text, f"the README has no distinct {mode} section"
    start = text.index(heading)
    end = text.index("\n## ", start + len(heading))
    section = text[start:end]
    assert re.search(r"`make [a-z]", section), f"the {mode} section names no make command"
    assert _SKILL_NAME_IN_TEXT.search(section), f"the {mode} section names no skill"


def test_the_remote_section_describes_the_transport_and_host_access() -> None:
    """AC-DOC-004: an SSM port forward under mutual TLS, and no host shell."""
    text = _readme()
    start = text.index("## Quick start, remote")
    section = text[start : text.index("\n## ", start + 10)]
    lowered = section.lower()
    assert "port forward" in lowered, "the remote section must name the SSM port forward"
    assert "mutual tls" in lowered or "mtls" in lowered, "the remote section must name mutual TLS"
    assert "make exec" in section, (
        "the remote section must name make exec as the way into a container"
    )


def _remote_lead_in() -> str:
    """The lead-in paragraph under `## Quick start, remote`, before the first bullet."""
    text = _readme()
    start = text.index("## Quick start, remote")
    rest = text[text.index("\n", start) + 1 :]
    match = re.search(r"\n\n|\n- ", rest)
    return rest[: match.start()] if match else rest


def test_the_remote_lead_in_states_only_one_kind_uses_ssh() -> None:
    """AC-DOC-006: the lead-in states four kinds, of which only general SSH uses SSH.

    The three provisioned kinds are reached with no SSH anywhere (EC2
    through an SSM port forward, Azure through the relay the VM dials out
    to, GCP through an IAP forward), so a lead-in that introduced all four
    as SSH-reachable, or named no SSH split at all, would misstate the
    platform's defining security property. Both halves of the statement are
    pinned: the no-SSH-anywhere clause for the three, and the only-one clause
    for the fourth.
    """
    lead = _remote_lead_in().lower()
    assert "no ssh anywhere" in lead, (
        "the remote lead-in must state that the three provisioned kinds are reached "
        "with no SSH anywhere (EC2 through an SSM port forward, Azure through the "
        "relay, GCP through an IAP forward)"
    )
    assert "the only kind that uses ssh" in lead, (
        "the remote lead-in must state that general SSH is the only kind of the four that uses SSH"
    )


@pytest.mark.parametrize("token", ["make shell", "tunnel"])
def test_removed_surfaces_are_named_nowhere(token: str) -> None:
    """AC-DOC-005: the README documents current state only, so none of these appear.

    There is no change-history table to exempt: a reader meeting one of these
    tokens has found a description of something the platform does not offer.
    `ssh` is deliberately absent from this list: it names a first-class
    engine kind now, reached through `engine.json` manifests and the ssh
    transport, so the README naming it is documentation, not a stale
    reference.
    """
    haystack = _readme().lower()
    assert token not in haystack, (
        f"README names {token!r}, a surface the platform does not offer. Remove the "
        "reference: the README documents the current state only."
    )
