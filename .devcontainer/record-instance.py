#!/usr/bin/env python3
"""Record a provisioned instance's EC2 id into the gitignored shell.env.

`make record-instance INSTANCE=<name>` feeds this script the instance_id
Terragrunt output on argv. The id is a real identifier, so shell.env (the
one file every remote target sources on the host) is the only place it may
live, exactly like REMOTE_AWS_PROFILE and the other values the laptop-side
scripts read there.

Idempotent: a second run replaces the previous export line in place rather
than appending a second one, so re-provisioning an instance never leaves two
ids for lib.sh's resolver to trip over. Fails loudly when shell.env is
missing (make init creates it) rather than silently writing a file the rest
of the tooling does not source.
"""

from __future__ import annotations

import sys
from pathlib import Path

MARKER_COMMENT = "# Recorded by make record-instance; the EC2 id of this machine's remote engine."
EXPORT_PREFIX = "export REMOTE_INSTANCE_ID="


def record(shell_env: Path, instance_id: str) -> None:
    lines = shell_env.read_text(encoding="utf-8").splitlines()
    export_line = f"{EXPORT_PREFIX}'{instance_id}'"
    for index, line in enumerate(lines):
        if line.startswith(EXPORT_PREFIX):
            lines[index] = export_line
            break
    else:
        lines += ["", MARKER_COMMENT, export_line]
    shell_env.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(
            "usage: record-instance.py <instance-id>",
            file=sys.stderr,
        )
        return 2
    shell_env = Path(__file__).resolve().parent.parent / "shell.env"
    if not shell_env.is_file():
        print(
            f"ERROR: {shell_env} does not exist; run 'make init' first.",
            file=sys.stderr,
        )
        return 1
    record(shell_env, argv[0])
    print(f"recorded REMOTE_INSTANCE_ID={argv[0]} in {shell_env.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
