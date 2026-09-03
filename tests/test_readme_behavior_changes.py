"""`README.md` states the behavior changes, and names only things that exist.

A README that still describes the removed transport is worse than one that says
nothing: a reader follows it, the command fails, and they cannot tell whether
they are holding it wrong or reading a stale document. These assertions cover
the three ways that happens -- a behavior change that went unrecorded, a `make`
target or skill named here that does not exist, and a removed identifier
described as though it were still available.

The removed identifiers are deliberately allowed inside the "What changed"
table and nowhere else. That table's entire job is to say what those things
used to do and that they are gone, which it cannot do without naming them; a
blanket scan would forbid the one place the name belongs.

Hermetic: reads files, runs nothing, and needs no docker, AWS or network.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from conftest import _makefile_text

_REPO_ROOT = Path(__file__).resolve().parent.parent
_README = _REPO_ROOT / "README.md"
_SKILLS_DIR = _REPO_ROOT / ".claude" / "plugins" / "devcontainer" / "skills"

_BEHAVIOR_IDS = tuple(f"B{n}" for n in range(1, 11))
_WHAT_CHANGED_HEADING = "## What changed"


def _readme() -> str:
    return _README.read_text(encoding="utf-8")


def _what_changed_table() -> str:
    """The rows of the What changed table, and nothing else."""
    text = _readme()
    start = text.index(_WHAT_CHANGED_HEADING)
    end = text.index("\n## ", start + len(_WHAT_CHANGED_HEADING))
    return text[start:end]


def _readme_outside_what_changed() -> str:
    text = _readme()
    table = _what_changed_table()
    return text.replace(table, "")


def test_the_readme_has_a_what_changed_section() -> None:
    assert _WHAT_CHANGED_HEADING in _readme(), (
        "AC-DOC-001: the README must carry a What changed section"
    )


@pytest.mark.parametrize("identifier", _BEHAVIOR_IDS)
def test_each_behavior_change_has_a_row_with_both_halves(identifier: str) -> None:
    """AC-DOC-001: one row per identifier, and neither half may be empty.

    An identifier with an empty cell reads as "this changed, somehow", which
    tells a reader less than omitting the row would.
    """
    rows = [
        line
        for line in _what_changed_table().splitlines()
        if re.match(rf"^\|\s*{identifier}\s*\|", line)
    ]
    assert len(rows) == 1, f"expected exactly one row for {identifier}, found {len(rows)}"
    cells = [cell.strip() for cell in rows[0].strip().strip("|").split("|")]
    assert len(cells) == 3, f"{identifier} row must have identifier, was, and is-now cells: {cells}"
    assert cells[1], f"{identifier} has an empty 'was' cell"
    assert cells[2], f"{identifier} has an empty 'is now' cell"


def test_every_make_target_named_in_the_readme_exists() -> None:
    """AC-TEST-002: a documented target that the Makefile does not define is a dead end."""
    makefile = _makefile_text()
    defined = set(re.findall(r"^([A-Za-z0-9_.-]+):(?!=)", makefile, re.MULTILINE))
    assert defined, "no targets parsed from the Makefile; this assertion would be vacuous"
    missing = []
    for line_number, line in enumerate(_readme_outside_what_changed().splitlines(), start=1):
        for target in re.findall(r"`make ([a-z][a-z0-9-]*)", line):
            if target not in defined:
                missing.append(f"line {line_number}: make {target}")
    assert not missing, f"README names make targets the Makefile does not define: {missing}"


def test_every_skill_named_in_the_readme_exists() -> None:
    """AC-TEST-003: a documented skill must resolve to a SKILL.md (ref)."""
    named = sorted(set(re.findall(r"/devcontainer:([a-z][a-z0-9-]*)", _readme())))
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
    assert "/devcontainer:" in section, f"the {mode} section names no skill"


def test_the_remote_section_describes_the_transport_and_host_access() -> None:
    """AC-DOC-004: an SSM port forward under mutual TLS, and no host shell."""
    text = _readme()
    start = text.index("## Quick start, remote")
    section = text[start : text.index("\n## ", start + 10)]
    lowered = section.lower()
    assert "port forward" in lowered, "the remote section must name the SSM port forward"
    assert "mutual tls" in lowered or "mtls" in lowered, "the remote section must name mutual TLS"
    assert "make exec" in section, "the remote section must name make exec as the way into a container"


@pytest.mark.parametrize("token", ["make shell", "ssh", "tunnel"])
def test_removed_things_are_named_only_where_they_are_declared_removed(token: str) -> None:
    """AC-DOC-005: outside the What changed table, the README names none of them."""
    haystack = _readme_outside_what_changed().lower()
    assert token not in haystack, (
        f"README describes {token!r} outside the What changed table. It was removed at cutover; "
        "the only place the name belongs is the row that says so."
    )
