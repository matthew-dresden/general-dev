"""The `devcontainer_config` command-line entry points (spec Section 4.5).

`cli` is the only module in this package that calls `sys.exit`: every other
module raises a `*Error` and lets its caller decide what to do about it.
That split is what lets `secrets.py` and every module like it stay callable
directly from a test, while `main` is the single place a process exit code
is actually produced (AC-FUNC-006).

Today this exposes one command, `lint-secrets` (spec Section 4.6). By
default, or with `--staged`, it scans whatever is currently staged for the
next commit, using `devcontainer_config.secrets.run_staged_scan`. With
`--range <a>..<b>` it instead scans every commit in that range, oldest
first, using `devcontainer_config.secrets.scan_range` (E2-F1-S2-T1):
scanning only the tip would miss a secret introduced earlier in the range
and removed later, which still reaches the remote in history. Either mode
prints its report and calls `sys.exit(1)` if it found anything,
`sys.exit(0)` otherwise. There is no flag, environment variable or marker
comment on this command that suppresses a finding: a finding is either
real, and fixed, or a false positive needing human review, per
`CLAUDE.md` -- there is no ignore list.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from devcontainer_config import repo
from devcontainer_config.secrets import (
    SecretScanError,
    render_lint_report,
    render_range_report,
    run_staged_scan,
    scan_range,
)

_PROG = "devcontainer_config"

_LINT_SECRETS_DESCRIPTION = (
    "Scans the content staged for the next commit -- the git index, never "
    "the working-tree copy -- by default or with --staged. With --range "
    "<a>..<b> it instead scans every commit in that range, oldest first, so "
    "a secret introduced earlier in the range and removed later is still "
    "reported. Either mode exits 1 if any detector finds something, 0 "
    "otherwise. There is no ignore list: no flag, environment variable or "
    "marker comment suppresses a finding. A finding is either real, and "
    "fixed, or a false positive needing human review."
)

_LINT_SECRETS_RANGE_HELP = (
    "Scan every commit in <a>..<b>, oldest first, instead of only the tip. "
    "Scanning only the tip would miss a secret introduced earlier in the "
    "range and removed later, which still reaches the remote in history."
)

_LINT_SECRETS_STAGED_HELP = (
    "Scan the content staged for the next commit (the git index). This is "
    "the default when neither --staged nor --range is given."
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
        help="Scan staged content, or a commit range, for secrets (spec Section 4.6).",
        description=_LINT_SECRETS_DESCRIPTION,
    )
    mode_group = lint_secrets_parser.add_mutually_exclusive_group()
    mode_group.add_argument("--staged", action="store_true", help=_LINT_SECRETS_STAGED_HELP)
    mode_group.add_argument("--range", metavar="<a>..<b>", help=_LINT_SECRETS_RANGE_HELP)
    lint_secrets_parser.set_defaults(handler=_run_lint_secrets)
    return parser


def _run_lint_secrets(args: argparse.Namespace) -> int:
    """Scan staged content or a commit range under the current repository root.

    `args.range` is `None` unless `--range <a>..<b>` was given (mutually
    exclusive with `--staged` at the parser level -- AC-FUNC-007), in which
    case range mode runs instead of staged mode. Either mode's exit code
    is the same rule: 1 if anything was found, 0 otherwise.
    """
    root = repo.find_root(Path.cwd())
    if args.range is not None:
        range_report = scan_range(root, args.range)
        print(render_range_report(range_report))
        return 1 if range_report.findings else 0
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
