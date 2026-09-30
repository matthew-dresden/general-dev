"""Contract pins for the `make skills-install/remove/list` targets (U3).

The skills surface follows the same Makefile discipline the instance
surface (`tests/test_makefile_contract.py`'s U2 section) pins: every target
is defined, `.PHONY`'d and advertised exactly once in `make help`; every
recipe is a thin delegation to the same-named `devcontainer_config.cli`
subcommand (the engine is `devcontainer_config.skills_install`, never a
re-implementation inline); and the AGENT/SCOPE selectors are defaulted at
the make layer so a no-variable invocation reaches the cli's argparse
defaults instead of an empty value.

The help rows are asserted against the help recipe's own `printf` lines
(not against rendered output, which the snapshot in
`tests/data/make-help.txt` already pins byte-for-byte), so these checks
hold regardless of terminal width or color support.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from conftest import _makefile_text
from test_makefile_contract import _help_recipe_body, _phony_targets, _target_recipe_body

# Every target the U3 skills surface adds, and the cli subcommand each
# recipe delegates to. Defined once so the phony/defined/help-row suites
# cannot list a different set from one another.
SKILLS_TARGETS: tuple[str, ...] = ("skills-install", "skills-remove", "skills-list")

# The selector variables the recipes pass through, with the defaults the
# Makefile must declare so a no-variable run behaves like the documented
# default (both agents, global scope).
SELECTOR_DEFAULTS: tuple[tuple[str, str], ...] = (("AGENT", "both"), ("SCOPE", "global"))

# The accepted values, as the help rows must spell them for both selectors.
_AGENT_VALUES = "opencode|claude|both"
_SCOPE_VALUES = "global|project|runtime"


def test_skills_selector_defaults_are_declared() -> None:
    """`AGENT ?= both` and `SCOPE ?= global` exist so an empty value never
    reaches the cli (an empty --agent would be an argparse usage error,
    not the documented default)."""
    makefile_text = _makefile_text()
    for name, default in SELECTOR_DEFAULTS:
        match = re.search(rf"^{name} \?= (\S+)$", makefile_text, re.MULTILINE)
        assert match is not None, f"no `{name} ?=` default declared in the Makefile"
        assert match.group(1) == default, f"`{name}` must default to {default!r}"


@pytest.mark.parametrize("target", SKILLS_TARGETS)
def test_skills_target_is_phony(target: str) -> None:
    assert target in _phony_targets(_makefile_text())


@pytest.mark.parametrize("target", SKILLS_TARGETS)
def test_skills_target_is_defined(target: str) -> None:
    assert re.search(rf"^{re.escape(target)}:", _makefile_text(), re.MULTILINE) is not None, (
        f"{target} is .PHONY'd but defines no recipe"
    )


@pytest.mark.parametrize("target", SKILLS_TARGETS)
def test_skills_target_has_exactly_one_help_row(target: str) -> None:
    help_recipe = _help_recipe_body(_makefile_text())
    rows = [line for line in help_recipe.splitlines() if f'"make {target}"' in line]
    assert len(rows) == 1, f"make help must advertise {target} exactly once"


@pytest.mark.parametrize("target", SKILLS_TARGETS)
def test_skills_help_row_names_both_selectors_with_their_valid_values(target: str) -> None:
    help_recipe = _help_recipe_body(_makefile_text())
    row = next(line for line in help_recipe.splitlines() if f'"make {target}"' in line)
    assert "AGENT=" in row
    assert _AGENT_VALUES in row
    assert "SCOPE=" in row
    assert _SCOPE_VALUES in row


@pytest.mark.parametrize("target", SKILLS_TARGETS)
def test_skills_recipe_delegates_to_the_cli_subcommand(target: str) -> None:
    """Each recipe shells the same-named cli subcommand; nothing inline."""
    recipe = _target_recipe_body(_makefile_text(), target)
    assert f"devcontainer_config.cli {target}" in recipe, (
        f"{target} must delegate to `devcontainer_config.cli {target}` "
        "(PYTHONPATH=$(DEVCONTAINER_SCRIPTS_DIR)), not re-implement it"
    )
    assert "PYTHONPATH=$(DEVCONTAINER_SCRIPTS_DIR)" in recipe


@pytest.mark.parametrize("target", SKILLS_TARGETS)
def test_skills_recipe_passes_both_selectors_from_the_make_variables(target: str) -> None:
    recipe = _target_recipe_body(_makefile_text(), target)
    assert '--agent "$(AGENT)"' in recipe
    assert '--scope "$(SCOPE)"' in recipe


def test_skills_help_section_heading_states_the_native_mac_scope() -> None:
    """The SKILLS section says what the targets act on and what they avoid."""
    help_recipe = _help_recipe_body(_makefile_text())
    assert "SKILLS" in help_recipe
    assert ".agents/skills" in help_recipe
    assert "no devcontainer involved" in help_recipe


def test_skills_help_rows_sit_in_a_dedicated_skills_section() -> None:
    """The three rows live between QUALITY and OPTIONS, one section."""
    help_recipe = _help_recipe_body(_makefile_text())
    quality_index = help_recipe.index("QUALITY")
    skills_index = help_recipe.index("SKILLS")
    options_index = help_recipe.index("OPTIONS")
    assert quality_index < skills_index < options_index


def test_help_snapshot_carries_the_skills_rows() -> None:
    """The regenerated snapshot advertises the whole surface (belt for the
    per-row pins above, which read the recipe; this reads the fixture)."""
    fixture = Path(__file__).resolve().parent / "data" / "make-help.txt"
    text = fixture.read_text(encoding="utf-8")
    for target in SKILLS_TARGETS:
        assert f"make {target}" in text
