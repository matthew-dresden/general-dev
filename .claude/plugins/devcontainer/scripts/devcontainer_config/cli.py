"""The `devcontainer_config` command-line entry points (spec Section 4.5).

`cli` is the only module in this package that calls `sys.exit`: every other
module raises a `*Error` and lets its caller decide what to do about it.
That split is what lets `secrets.py` and every module like it stay callable
directly from a test, while `main` is the single place a process exit code
is actually produced (AC-FUNC-006).

Today this exposes one command, `lint-secrets` (spec Section 4.6): it scans
whatever is currently staged for the next commit, using
`devcontainer_config.secrets.run_staged_scan`, prints the report, and calls
`sys.exit(1)` if it found anything, `sys.exit(0)` otherwise. There is no
flag, environment variable or marker comment on this command that suppresses
a finding: a finding is either real, and fixed, or a false positive needing
human review, per `CLAUDE.md` -- there is no ignore list. The pushed-range
half of the same contract (`--range <a>..<b>`) is E2-F1-S2-T1's addition,
not this module's.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from devcontainer_config import repo
from devcontainer_config.secrets import SecretScanError, render_lint_report, run_staged_scan

_PROG = "devcontainer_config"

_LINT_SECRETS_DESCRIPTION = (
    "Scans the content staged for the next commit -- the git index, never "
    "the working-tree copy -- and exits 1 if any detector finds something, "
    "0 otherwise. There is no ignore list: no flag, environment variable or "
    "marker comment suppresses a finding. A finding is either real, and "
    "fixed, or a false positive needing human review."
)


def _build_parser() -> argparse.ArgumentParser:
    """The top-level parser, with `lint-secrets` as its first subcommand.

    A subparser, not a flat set of top-level flags, because spec Section 4.5
    names this module as the future home of every `devsecret` entry point
    too; adding the next command means adding another subparser here, not
    restructuring this one into something that can hold more than one verb.
    """
    parser = argparse.ArgumentParser(
        prog=_PROG,
        description="Entry points devcontainer_config exposes to make targets and skills.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    lint_secrets_parser = subparsers.add_parser(
        "lint-secrets",
        help="Scan staged content for secrets (spec Section 4.6). Exit 1 on any finding.",
        description=_LINT_SECRETS_DESCRIPTION,
    )
    lint_secrets_parser.set_defaults(handler=_run_lint_secrets)
    return parser


def _run_lint_secrets(_args: argparse.Namespace) -> int:
    """Scan staged content under the current repository root; the exit code to use."""
    root = repo.find_root(Path.cwd())
    report = run_staged_scan(root)
    print(render_lint_report(report))
    return 1 if report.findings else 0


def main(argv: Sequence[str] | None = None) -> None:
    """Parse `argv`, run the selected command, and exit the process.

    The only `sys.exit` call in this package (AC-FUNC-006): every command
    handler raises `SecretScanError` or `repo.RepoError` on a real failure
    instead of exiting itself, and this is where that exception becomes an
    exit code -- printed with an `ERROR:` prefix to stderr, never a stack
    trace.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        exit_code = args.handler(args)
    except (SecretScanError, repo.RepoError) as exc:
        print(str(exc), file=sys.stderr)
        exit_code = 1
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
