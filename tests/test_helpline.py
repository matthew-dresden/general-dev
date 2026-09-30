"""Unit tests for `devcontainer_config.helpline`, `make help`'s wrapped-row renderer.

The renderer owns the 180-visible-character rule for help rows too long for
their description to stay inline: line 1 carries the target and scope
columns only, and the description starts on the line below, indented to the
instruction column (34) and wrapped at word boundaries so no line passes the
limit. These tests pin the geometry itself -- the exact bytes of the inline
form (which must match the help recipe's own printf), the exact bytes of
wrapped first lines for a scoped and an unscoped row, the budget boundary on
both sides, multi-line wrapping, and the fail-fast refusal of a word too
long to wrap.

The inline form is asserted against an independent restatement of the
recipe's printf semantics (`'  \\033[1;36m%-23s\\033[0m %-7s %s\\n'`:
two-space indent, target left-justified to 23, ANSI reset, one space, scope
left-justified to 7, one space, description) rather than against a literal
copied from the module, so a renderer that drifted from the recipe's format
would fail here even if it stayed self-consistent. The Makefile-side half of
that agreement -- the recipe's `row()` helper and its literal constants --
is pinned by `tests/test_makefile_contract.py`; the rendered output of the
real rows is pinned end to end by `tests/test_help_snapshot.py`'s fixture.
"""

from __future__ import annotations

import re

import pytest
from devcontainer_config import helpline
from devcontainer_config.helpline import HelpLineError, render_row

# The printf semantics the help recipe renders short rows with, restated
# independently of the module under test (see module docstring).
_ANSI_PREFIX = "\033[1;36m"
_ANSI_SUFFIX = "\033[0m"


def _printf_equivalent(target: str, scope: str, description: str) -> str:
    """What the recipe's `'  %-23s %-7s %s\\n'`-shaped printf produces for these columns."""
    return f"  {_ANSI_PREFIX}{target:<23}{_ANSI_SUFFIX} {scope:<7} {description}\n"


def test_short_row_matches_the_recipe_printf_byte_for_byte() -> None:
    """A row under the cap renders exactly what the recipe's printf produces."""
    rendered = render_row("make up", "both", "Get working from any state.")
    assert rendered == _printf_equivalent("make up", "both", "Get working from any state.")
    assert len(rendered.splitlines()[0].replace(_ANSI_PREFIX, "").replace(_ANSI_SUFFIX, "")) <= (
        helpline.INSTRUCTION_COLUMN + helpline.DESCRIPTION_MAX
    )


def test_description_cap_boundary_inline_at_120_wraps_at_121() -> None:
    """A 120-character description stays inline; one more character wraps.

    The 121-character side is two words (`"a" * 120 + " b"`), because a
    single 121-character word is unsplittable and refused outright -- the
    next test pins that refusal.
    """
    inline = render_row("make x", "both", "a" * 120)
    assert inline == _printf_equivalent("make x", "both", "a" * 120)
    assert len(inline.splitlines()) == 1

    wrapped = render_row("make x", "both", "a" * 120 + " b")
    lines = wrapped.splitlines()
    assert len(lines) == 3
    assert lines[1] == " " * helpline.INSTRUCTION_COLUMN + "a" * 120
    assert lines[2] == " " * helpline.INSTRUCTION_COLUMN + "b"
    assert all(
        len(line) <= helpline.INSTRUCTION_COLUMN + helpline.DESCRIPTION_MAX for line in lines
    )


def test_unsplittable_word_longer_than_the_budget_raises_naming_it() -> None:
    """A single word longer than the description budget fails fast, naming it."""
    long_word = "w" * 121
    with pytest.raises(HelpLineError) as exc_info:
        render_row("make x", "both", f"starts fine {long_word} then more")
    message = str(exc_info.value)
    assert long_word in message
    assert str(helpline.DESCRIPTION_MAX) in message


def test_wrapped_row_scoped_columns_and_alignment() -> None:
    """A wrapped row keeps target+scope on line 1 and indents the description to 34.

    The first-line bytes are pinned literally (the target keeps its 23-column
    padding, the scope lands at column 26), and every description line starts
    at the instruction column.
    """
    description = (
        "Address one engine explicitly (ENGINE=x make <target>, or make <target> "
        "ENGINE=x): parallel terminals can drive local and remote engines "
        "concurrently, without switching contexts. Unset follows the active context."
    )
    rendered = render_row("ENGINE=local|<name>", "", description)
    lines = rendered.splitlines()
    assert lines[0] == f"  {_ANSI_PREFIX}ENGINE=local|<name>{_ANSI_SUFFIX}"
    wrapped_lines = lines[1:]
    assert wrapped_lines, "the description must start on the line below, not stay inline"
    for line in wrapped_lines:
        assert line.startswith(" " * helpline.INSTRUCTION_COLUMN)
        assert line[helpline.INSTRUCTION_COLUMN] != " "
        assert len(line) <= helpline.INSTRUCTION_COLUMN + helpline.DESCRIPTION_MAX
    assert " ".join(line.strip() for line in wrapped_lines) == description


def test_wrapped_row_with_scope_keeps_both_columns_on_line_one() -> None:
    """A scoped wrapped row's line 1 carries the padded target and the scope."""
    description = (
        "Wire an agent to the canonical skills home: global symlink (default), "
        "project verify/wire, or the runtime one-shot incantation (printed, never "
        "run). AGENT=opencode|claude|both SCOPE=global|project|runtime"
    )
    rendered = render_row("make skills-install", "host", description)
    lines = rendered.splitlines()
    assert lines[0] == f"  {_ANSI_PREFIX}make skills-install    {_ANSI_SUFFIX} host"
    assert " ".join(line.strip() for line in lines[1:]) == description
    assert all(
        len(line) <= helpline.INSTRUCTION_COLUMN + helpline.DESCRIPTION_MAX for line in lines
    )


def test_wrapping_packs_whole_words_greedily_across_continuation_lines() -> None:
    """Ten-character words pack 11 per line (11*11-1 == 120), then spill two."""
    description = " ".join("abcdefghij" for _ in range(13))
    rendered = render_row("make x", "host", description)
    lines = rendered.splitlines()
    assert lines[1] == " " * helpline.INSTRUCTION_COLUMN + " ".join(["abcdefghij"] * 11)
    assert lines[2] == " " * helpline.INSTRUCTION_COLUMN + "abcdefghij abcdefghij"
    assert len(lines) == 3


def test_rendered_lines_each_end_with_one_newline() -> None:
    """The renderer returns newline-terminated lines and nothing else."""
    for description in ("short", "a" * 120 + " " + "b" * 40):
        rendered = render_row("make x", "both", description)
        assert rendered.endswith("\n")
        assert not rendered.endswith("\n\n")
        assert "\n\n" not in rendered


def test_main_renders_three_arguments_to_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    """The module entry point renders the three columns it is handed."""
    exit_code = helpline.main(["make x", "both", "Renders a row."])
    assert exit_code == 0
    captured = capsys.readouterr()
    assert captured.out == _printf_equivalent("make x", "both", "Renders a row.")
    assert captured.err == ""


def test_main_rejects_any_argument_count_other_than_three(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A usage error prints the expected argument order and exits 2."""
    for arguments in ([], ["a"], ["a", "b"], ["a", "b", "c", "d"]):
        exit_code = helpline.main(arguments)
        assert exit_code == 2
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "TARGET SCOPE DESCRIPTION" in captured.err


def test_module_constants_carry_the_documented_geometry() -> None:
    """The geometry constants hold the values the help recipe's columns define."""
    assert helpline.DESCRIPTION_MAX == 120
    assert helpline.INSTRUCTION_COLUMN == 34
    assert helpline.TARGET_WIDTH == 23
    assert helpline.SCOPE_WIDTH == 7
    assert helpline._DESCRIPTION_BUDGET == helpline.DESCRIPTION_MAX
    # The indent arithmetic the wrapped lines rely on: 34 spaces of indent are
    # produced from the constant, never a literal.
    assert re.fullmatch(r" {34}", " " * helpline.INSTRUCTION_COLUMN)
