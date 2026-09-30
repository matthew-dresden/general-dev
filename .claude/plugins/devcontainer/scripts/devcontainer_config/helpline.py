"""Renders the two-column rows of `make help` under the description-column cap.

`make help` renders every command row through one geometry: a two-space
indent, the target padded to 23 columns, a space, the scope word
(both/host/remote/local) padded to 7, a space, then the description -- so
the description column starts at character 34. The description column has a
maximum width: no description renders longer than `DESCRIPTION_MAX` visible
characters (ANSI escape sequences excluded, exactly the normalization
`tests/test_help_snapshot.py` performs). A longer description does not stay
inline: line 1 carries the target and scope columns only, with trailing
whitespace trimmed, and the description starts on the line below, indented
to the instruction column and wrapped at the last word boundary that fits
within `DESCRIPTION_MAX` characters per line. Rows at or under the cap
render byte-identically to the plain `printf` the help recipe used before
this rule existed.

The Makefile's `row()` shell helper owns the threshold decision (its literal
constants are pinned to this module's by `tests/test_makefile_contract.py`)
and renders short rows itself; this module renders the wrapped rows. It is
invoked per wrapped row as
`python3 -m devcontainer_config.helpline TARGET SCOPE DESCRIPTION`, the same
`PYTHONPATH=$(DEVCONTAINER_SCRIPTS_DIR)` form every other module entry point
in the Makefile uses.

Descriptions are plain text -- no ANSI sequences, no make references --
which is what makes the description length directly measurable here.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence

# The description column's maximum width: no `make help` description renders
# longer than this many visible characters; longer descriptions wrap onto
# continuation lines indented to the instruction column. Public so the
# Makefile contract test can pin the `row()` helper's literal threshold to it.
DESCRIPTION_MAX = 120

# The column the description text starts at: a two-space indent plus the
# 23-column target field, a separating space, the 7-column scope field, and
# one more separating space. Public for the same reason as `DESCRIPTION_MAX`;
# the wrapped description lines indent to exactly this column.
INSTRUCTION_COLUMN = 34

# The help table's column widths, matching the `row()` helper's printf format
# (`'  \033[1;36m%-23s\033[0m %-7s %s\n'`). Public so the contract test can
# pin the recipe's printf widths to them and the two renderers cannot drift.
TARGET_WIDTH = 23
SCOPE_WIDTH = 7

# The ANSI sequence the help recipe wraps the target column in (bold cyan).
_TARGET_ANSI_PREFIX = "\033[1;36m"
_TARGET_ANSI_SUFFIX = "\033[0m"

# The most visible characters a description chunk may occupy: the column's
# own maximum.
_DESCRIPTION_BUDGET = DESCRIPTION_MAX


class HelpLineError(RuntimeError):
    """A description cannot be rendered within the description-column cap.

    Raised only for content a help row can never legally carry -- a word
    longer than the column's budget, which no wrapping can fit. The message
    names the word and the budget, so editing the description (not the
    renderer) is the remedy.
    """


def _description_chunks(description: str) -> list[str]:
    """The description packed greedily into chunks of at most the column budget.

    Words are split on runs of whitespace (help descriptions carry none of
    the spacing that collapsing would change) and packed left to right: each
    chunk takes as many whole words as fit within `DESCRIPTION_MAX`
    characters, so every description line stays inside the cap. A word longer
    than the budget cannot be wrapped from any position and raises rather
    than silently overflowing a line.
    """
    chunks: list[str] = []
    current = ""
    for word in description.split():
        if len(word) > _DESCRIPTION_BUDGET:
            raise HelpLineError(
                f"ERROR: description word {word!r} is {len(word)} characters, "
                f"longer than the {_DESCRIPTION_BUDGET}-character description budget\n"
                f"No `make help` description may exceed {DESCRIPTION_MAX} visible "
                "characters, and a word this long cannot wrap.\n"
                "Shorten the description in the Makefile's help recipe."
            )
        candidate = word if not current else f"{current} {word}"
        if len(candidate) <= _DESCRIPTION_BUDGET:
            current = candidate
        else:
            chunks.append(current)
            current = word
    if current:
        chunks.append(current)
    return chunks


def _wrapped_first_line(target: str, scope: str) -> str:
    """Line 1 of a wrapped row: the target and scope columns, nothing more.

    The visible text is the same two columns a short row renders -- a
    two-space indent, the target padded to `TARGET_WIDTH`, a separating
    space, the scope -- with all trailing whitespace trimmed, which trims the
    scope's padding away with it when the scope is empty. The ANSI sequence
    then colors exactly the target segment, so the line's visible bytes equal
    the trimmed text and carry no trailing whitespace of their own.
    """
    visible = f"  {target:<{TARGET_WIDTH}} {scope}".rstrip()
    if scope:
        target_width = len(visible) - len(scope) - 3
        return f"  {_TARGET_ANSI_PREFIX}{target:<{target_width}}{_TARGET_ANSI_SUFFIX} {scope}"
    return f"  {_TARGET_ANSI_PREFIX}{target}{_TARGET_ANSI_SUFFIX}"


def render_row(target: str, scope: str, description: str) -> str:
    """One help row's rendered text: inline when it fits, wrapped when it does not.

    At or under `DESCRIPTION_MAX` visible characters this reproduces the help
    recipe's own printf byte for byte, so a short row's rendering never
    depends on which renderer produced it. Past the cap, line 1 carries the
    target and scope columns only (`_wrapped_first_line`) and the description
    starts on line 2, indented to `INSTRUCTION_COLUMN` and wrapped at word
    boundaries to keep every description line within the cap.

    Returns:
        The rendered lines, each terminated by a newline.
    """
    if len(description) <= DESCRIPTION_MAX:
        return (
            f"  {_TARGET_ANSI_PREFIX}{target:<{TARGET_WIDTH}}{_TARGET_ANSI_SUFFIX}"
            f" {scope:<{SCOPE_WIDTH}} {description}\n"
        )
    lines = [_wrapped_first_line(target, scope)]
    lines.extend(f"{' ' * INSTRUCTION_COLUMN}{chunk}" for chunk in _description_chunks(description))
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    """Render one row from the three positional arguments; print it to stdout.

    A usage error -- not exactly three arguments -- prints the expected
    argument order to stderr and exits 2, the same usage-error convention
    the make targets' guards follow.
    """
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 3:
        print(
            "ERROR: expected exactly three arguments: TARGET SCOPE DESCRIPTION\n"
            "This is `make help`'s row renderer; the recipe's row() helper "
            "supplies the three columns.",
            file=sys.stderr,
        )
        return 2
    sys.stdout.write(render_row(*arguments))
    return 0


if __name__ == "__main__":
    sys.exit(main())
