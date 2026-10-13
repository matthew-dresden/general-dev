#!/usr/bin/env python3
"""Prose gate over markdown: dashes and paragraph walls.

Two rules, both applied only to non-fenced content (a fenced code block,
from its opening ``` line through its closing fence, is stripped from
consideration for both):

Rule D (dashes): the em dash (U+2014) and the en dash (U+2013) never
appear. The prose standard separates clauses with ordinary punctuation or
rewrites the sentence; the checker cannot judge a rewrite, so it refuses
the character itself, naming file, line number and the offending line.
Table lines are not exempt: a dash inside a table cell is still a dash in
prose. Fenced code is.

Rule P (paragraphs): outside fences, a run of two or more consecutive
non-empty prose lines fails, naming the file, the run's first line number
and the run's first 60 characters. Table lines, headings (#), list items
(-, * or + after indent, or digits and a dot after indent) and blockquotes
(>) are structure, not paragraphs: they are exempt from the rule and they
end a run. A single prose line (a lead-in) is allowed, so an introductory
sentence ahead of a list or a code block stays legal.

Output follows .devcontainer/lint-json.py's conventions: one line per
finding on stderr, a two-space-indented summary line, exit 0 silent-success
(a one-line summary naming the file count) when everything passes, exit 1
on any finding. There is no ignore list.
"""

from __future__ import annotations

import re
import sys

EM_DASH = "\u2014"
EN_DASH = "\u2013"
DASH_RULES: tuple[tuple[str, str], ...] = ((EM_DASH, "em dash"), (EN_DASH, "en dash"))

FENCE_MARKER = "```"

# A numbered list item: digits and a dot after optional indent ("-", "*"
# and "+" are matched by prefix below). Requiring nothing after the marker
# keeps a bare "---" divider or front-matter fence a list item, so it ends
# a prose run rather than joining one.
_NUMBERED_ITEM = re.compile(r"^\d+\.")

_PREVIEW_WIDTH = 60


def _is_table(line: str) -> bool:
    return line.lstrip().startswith("|")


def _is_heading(line: str) -> bool:
    return line.lstrip().startswith("#")


def _is_blockquote(line: str) -> bool:
    return line.lstrip().startswith(">")


def _is_list_item(line: str) -> bool:
    stripped = line.lstrip()
    return stripped.startswith(("-", "*", "+")) or _NUMBERED_ITEM.match(stripped) is not None


def _is_structure(line: str) -> bool:
    """True for a line the paragraph rule does not count and does not join."""
    return (
        not line.strip()
        or _is_table(line)
        or _is_heading(line)
        or _is_list_item(line)
        or _is_blockquote(line)
    )


def check(path: str) -> list[str]:
    """Return the finding messages for one file, empty when it passes."""
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except OSError as exc:
        return [f"{path}: could not read: {exc}"]
    except UnicodeDecodeError as exc:
        return [f"{path}: not valid UTF-8: {exc}"]

    findings: list[str] = []
    in_fence = False
    # Line number of the first line of the current prose run, or None when
    # the previous line was blank, structure, or a fence boundary.
    run_start: int | None = None
    # First line numbers already reported, so one run yields one finding.
    reported: set[int] = set()

    for number, line in enumerate(lines, start=1):
        if in_fence:
            if line.lstrip().startswith(FENCE_MARKER):
                in_fence = False
            continue
        if line.lstrip().startswith(FENCE_MARKER):
            in_fence = True
            run_start = None
            continue

        # Rule D: dashes, everywhere outside fences, tables included.
        for character, name in DASH_RULES:
            if character in line:
                findings.append(
                    f"{path}: line {number}: {name} (U+{ord(character):04X}) found: {line.strip()}"
                )

        # Rule P: paragraph walls, in prose lines only. A structure line
        # (blank, table, heading, list item, quote) ends any open run.
        if _is_structure(line):
            run_start = None
            continue
        if run_start is None:
            run_start = number
        elif run_start not in reported:
            reported.add(run_start)
            preview = lines[run_start - 1][:_PREVIEW_WIDTH]
            findings.append(
                f"{path}: line {run_start}: paragraph run of 2 or more prose lines "
                f"starts here, first 60 characters: {preview!r}"
            )

    return findings


def main(paths: list[str]) -> int:
    failures = 0
    total = 0
    for path in paths:
        file_findings = check(path)
        for finding in file_findings:
            print(finding, file=sys.stderr)
        if file_findings:
            failures += 1
            total += len(file_findings)

    if failures:
        print(
            f"  {total} finding(s) in {failures} of {len(paths)} file(s) failed the prose rules",
            file=sys.stderr,
        )
        return 1
    print(f"  {len(paths)} file(s) pass the prose rules")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
