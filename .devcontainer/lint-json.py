#!/usr/bin/env python3
"""Validate JSON files, tolerating the // comments devcontainer.json allows.

devcontainer.json is JSONC: the spec permits comments, and the devcontainer CLI
parses them. A strict json.load would reject a valid config, so full-line
comments are stripped before parsing. Comments after a value on the same line
are left alone deliberately, stripping those would need a real JSONC parser,
and would risk mangling "https://..." inside a string.
"""

from __future__ import annotations

import json
import re
import sys

FULL_LINE_COMMENT = re.compile(r"^\s*//.*$", re.MULTILINE)


def parse(path: str) -> str | None:
    """Return an error message, or None when the file parses."""
    try:
        with open(path, encoding="utf-8") as handle:
            source = handle.read()
    except OSError as exc:
        return f"could not read: {exc}"

    try:
        json.loads(FULL_LINE_COMMENT.sub("", source))
    except json.JSONDecodeError as exc:
        return f"line {exc.lineno}, column {exc.colno}: {exc.msg}"
    return None


def main(paths: list[str]) -> int:
    failures = 0
    for path in paths:
        error = parse(path)
        if error:
            print(f"{path}: {error}", file=sys.stderr)
            failures += 1

    if failures:
        print(f"  {failures} of {len(paths)} file(s) failed to parse", file=sys.stderr)
        return 1
    print(f"  {len(paths)} file(s) parse")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
