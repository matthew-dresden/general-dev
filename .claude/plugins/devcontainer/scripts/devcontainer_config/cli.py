"""The `devcontainer_config` command-line entry points (spec Section 4.5).

`cli` is the only module in this package that calls `sys.exit`: every other
module raises a `*Error` and lets its caller decide what to do about it.
That split is what lets `secrets.py` and every module like it stay callable
directly from a test, while `main` is the single place a process exit code
is actually produced (AC-FUNC-006).

`lint-secrets` (spec Section 4.6), by default or with `--staged`, scans
whatever is currently staged for the next commit, using
`devcontainer_config.secrets.run_staged_scan`. With `--range <a>..<b>` it
instead scans every commit in that range, oldest first, using
`devcontainer_config.secrets.scan_range` (E2-F1-S2-T1): scanning only the
tip would miss a secret introduced earlier in the range and removed later,
which still reaches the remote in history. Either mode prints its report and
calls `sys.exit(1)` if it found anything, `sys.exit(0)` otherwise. There is
no flag, environment variable or marker comment on this command that
suppresses a finding: a finding is either real, and fixed, or a false
positive needing human review, per `CLAUDE.md` -- there is no ignore list.

`hooks-install` and `hooks-check` (spec Section 4.5) wrap
`devcontainer_config.githooks.install_hooks` and `.hooks_status`:
`hooks-install` writes the pre-commit and pre-push hooks and `hooks-check`
reports whether the installed hooks still match what `hooks-install` would
write, without rewriting them. `hooks-pre-push` (spec Section 4.6) is what
the pre-push hook itself execs (via `make hooks-run-push`): it reads git's
own pre-push stdin contract, derives the pushed range for every ref with
`devcontainer_config.githooks.ranges_from_push_refs`, and scans each range
with `scan_range`, exiting 1 if any of them found something.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from devcontainer_config import repo
from devcontainer_config.githooks import (
    HOOK_NAMES,
    GitHooksError,
    hooks_status,
    install_hooks,
    ranges_from_push_refs,
)
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

_HOOKS_INSTALL_DESCRIPTION = (
    "Writes the pre-commit and pre-push hooks under .git/hooks, executable. "
    "Idempotent: a second run leaves byte-identical content. Refuses to "
    "overwrite a hook it did not author, in case it is a developer's own hook."
)

_HOOKS_CHECK_DESCRIPTION = (
    "Reports, for each hook, whether the installed content still matches "
    "what 'hooks-install' would write, without rewriting it. Exits 1 if any "
    "hook has drifted or is not installed, 0 if every hook matches."
)

_HOOKS_PRE_PUSH_DESCRIPTION = (
    "Reads git's pre-push hook stdin contract -- one '<local ref> <local "
    "sha> <remote ref> <remote sha>' line per ref being pushed -- derives "
    "the pushed range for each ref, and scans every commit in each range for "
    "secrets. This is what 'make hooks-run-push' execs; it is not meant to "
    "be run by hand outside a pre-push hook."
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

    hooks_install_parser = subparsers.add_parser(
        "hooks-install",
        help="Install the pre-commit and pre-push hooks (spec Section 4.5).",
        description=_HOOKS_INSTALL_DESCRIPTION,
    )
    hooks_install_parser.set_defaults(handler=_run_hooks_install)

    hooks_check_parser = subparsers.add_parser(
        "hooks-check",
        help="Report whether the installed hooks match what hooks-install would write.",
        description=_HOOKS_CHECK_DESCRIPTION,
    )
    hooks_check_parser.set_defaults(handler=_run_hooks_check)

    hooks_pre_push_parser = subparsers.add_parser(
        "hooks-pre-push",
        help="Scan every commit in the pushed range, read from stdin (spec Section 4.6).",
        description=_HOOKS_PRE_PUSH_DESCRIPTION,
    )
    hooks_pre_push_parser.set_defaults(handler=_run_hooks_pre_push)

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


def _run_hooks_install(args: argparse.Namespace) -> int:
    """Install both hooks under the current repository root; always exits 0.

    `install_hooks` itself raises `GitHooksError` on any real failure
    (an unwritable `.git/hooks`, or a hook it did not author), which `main`
    converts into a non-zero exit code -- there is no failure this handler
    reports as anything but that exception.
    """
    root = repo.find_root(Path.cwd())
    for path in install_hooks(root):
        print(f"[DONE] installed {path.relative_to(root)}")
    return 0


def _run_hooks_check(args: argparse.Namespace) -> int:
    """Report each hook's drift status under the current repository root.

    Exits 1 if any hook is missing or does not match what `install_hooks`
    would write, 0 if every hook matches (AC-FUNC-003).
    """
    root = repo.find_root(Path.cwd())
    status = hooks_status(root)
    for hook_name in HOOK_NAMES:
        state = "match" if status[hook_name] else "drift"
        print(f"[HOOKS] {hook_name}: {state}")
    return 0 if all(status.values()) else 1


def _run_hooks_pre_push(args: argparse.Namespace) -> int:
    """Scan every range derived from stdin; exits 1 if any range found something.

    `sys.stdin.read()` is git's own pre-push hook contract (see the module
    docstring): one push can name several refs, and `ranges_from_push_refs`
    already orders and filters them, so this only has to scan whatever
    ranges it returns.
    """
    root = repo.find_root(Path.cwd())
    ranges = ranges_from_push_refs(sys.stdin.read(), root)
    found_anything = False
    for revision_range in ranges:
        range_report = scan_range(root, revision_range)
        print(render_range_report(range_report))
        found_anything = found_anything or bool(range_report.findings)
    return 1 if found_anything else 0


def main(argv: Sequence[str] | None = None) -> None:
    """Parse `argv`, run the selected command, and exit the process.

    The only `sys.exit` call in this package (AC-FUNC-006): every command
    handler raises `SecretScanError`, `repo.RepoError` or `GitHooksError` on
    a real failure instead of exiting itself, and this is where that
    exception becomes an exit code -- printed with an `ERROR:` prefix to
    stderr, never a stack trace.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        exit_code = args.handler(args)
    except (SecretScanError, repo.RepoError, GitHooksError) as exc:
        print(str(exc), file=sys.stderr)
        exit_code = 1
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
