"""The `devcontainer_config` command-line entry points (spec Section 4.5).

`cli` is the only module in this package that calls `sys.exit`: every other
module raises a `*Error` and lets its caller decide what to do about it.
That split is what lets `secrets.py` and every module like it stay callable
directly from a test, while this module's public console entry points --
`main` and, as of this task, `main_devsecret` -- are the only places a
process exit code is actually produced (AC-FUNC-006). No private helper and
no library function calls `sys.exit`; each public entry point calls it
exactly once, as the terminal statement of that function's body.

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

`devsecret` (spec Section 4.3, decision D13; E3-F2-S1-T1) is this module's
second console entry point, `main_devsecret`, installed by its own console
script (spec Section 4.3: "on PATH in the container and on the host") rather
than as a subcommand of `devcontainer_config`. It exposes four record
commands so far -- `get`, `list`, `set` and `rm` -- with `run` and
`export-list` left to E3-F2-S1-T2, which extends `_build_devsecret_parser`
rather than restructuring it. Its exit codes (spec Section 4.3, 14.2) are
declared once as named constants (`EXIT_SUCCESS`, `EXIT_USAGE_ERROR`,
`EXIT_BACKEND_ERROR`, `EXIT_NOT_FOUND`, `EXIT_VALUE_EXPOSURE_REFUSED`) and
mapped from the `devcontainer_config.catalog.CatalogError` hierarchy by
`_devsecret_exit_code_for`, the single place that mapping is made, so no
handler chooses a number for itself (AC-FUNC-011). Two rules keep a secret
value from ever reaching a place it should not: `list` calls
`catalog.list_resolved`, which is built on `describe-parameters` and never
requests decryption, so a value is never held in memory on the listing path
at all (AC-4.3); `set` reads the value from stdin only -- a value supplied
as a positional argument is refused (exit 5) with no part of it echoed,
because arguments reach the process table where any other user on the
machine can read them. No instance-detection mechanism exists yet (Section
9's addressing is later, separate work), so every command here resolves or
narrows against `catalog.scope_set(None)` -- the shared scope alone, the
correct answer for an engine with no instance (decision D11), not a partial
implementation of instance-first resolution.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from devcontainer_config import catalog, repo
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

    One of this module's two public console entry points (AC-FUNC-006), and
    the only `sys.exit` site on the `devcontainer_config` command path: every
    command handler raises `SecretScanError`, `repo.RepoError` or
    `GitHooksError` on a real failure instead of exiting itself, and this is
    where that exception becomes an exit code -- printed with an `ERROR:`
    prefix to stderr, never a stack trace.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        exit_code = args.handler(args)
    except (SecretScanError, repo.RepoError, GitHooksError) as exc:
        print(str(exc), file=sys.stderr)
        exit_code = 1
    sys.exit(exit_code)


# ---------------------------------------------------------------------------
# devsecret: get, list, set, rm (spec Section 4.3, 14.2; E3-F2-S1-T1). `run`
# and `export-list` are E3-F2-S1-T2's addition to this same section.
# ---------------------------------------------------------------------------

# The five exit codes spec Section 4.3 and 14.2 fix, declared once so no
# handler below chooses a number for itself (AC-FUNC-011).
EXIT_SUCCESS = 0
EXIT_USAGE_ERROR = 2
EXIT_BACKEND_ERROR = 3
EXIT_NOT_FOUND = 4
EXIT_VALUE_EXPOSURE_REFUSED = 5

_DEVSECRET_PROG = "devsecret"

_DEVSECRET_GET_HELP = "get <NAME>: print one value on stdout. Nothing else."
_DEVSECRET_LIST_HELP = (
    "list [--scope <scope>]: names, scopes, last-changed, exported flag. Never prints a value."
)
_DEVSECRET_SET_HELP = (
    "set <NAME> [--scope <scope>] [--exported]: read the value from stdin. Never from an argument."
)
_DEVSECRET_RM_HELP = "rm <NAME> --scope <scope>: delete after confirmation."

# Rendered verbatim in `devsecret --help` (AC-TEST-005), matching the scopes
# and exit-codes blocks of spec Section 14.2 exactly; the full snapshot test
# pinning the entire reference text belongs to E4-F4-S1-T1.
_DEVSECRET_EPILOG = (
    "scopes:\n"
    "  shared                        Every engine and instance.\n"
    "  <instance>                    One environment. Resolved before shared.\n"
    "\n"
    "exit codes:\n"
    "  0 success   2 usage   3 backend unreachable or unauthorized\n"
    "  4 not found 5 refused because a value would have been exposed\n"
)

# Checked in this fixed, most-specific-first order (see
# `_devsecret_exit_code_for`): every named subclass of `catalog.CatalogError`
# this module distinguishes gets its own row, and the base `CatalogError` row
# is the only one that can ever match a condition none of the named
# subclasses covers (for example a malformed-response `CatalogError` raised
# directly), treated as a backend problem (exit 3) rather than inventing a
# sixth exit code Section 4.3 does not define.
_DEVSECRET_EXIT_CODES: tuple[tuple[type[catalog.CatalogError], int], ...] = (
    (catalog.SecretNotFoundError, EXIT_NOT_FOUND),
    (catalog.UnknownScopeError, EXIT_USAGE_ERROR),
    (catalog.InvalidScopeError, EXIT_USAGE_ERROR),
    (catalog.InvalidSecretNameError, EXIT_USAGE_ERROR),
    (catalog.CatalogUnauthorizedError, EXIT_BACKEND_ERROR),
    (catalog.CatalogUnavailableError, EXIT_BACKEND_ERROR),
    (catalog.CatalogUnclassifiedError, EXIT_BACKEND_ERROR),
    (catalog.CatalogError, EXIT_BACKEND_ERROR),
)


def _devsecret_exit_code_for(exc: catalog.CatalogError) -> int:
    """The exit code spec Section 4.3 assigns to `exc`'s most specific matching class.

    Checks every named row but the trailing one in order, then falls back to
    that trailing `(catalog.CatalogError, EXIT_BACKEND_ERROR)` row without
    testing it: `exc` is typed `catalog.CatalogError`, so that row always
    matches, and there is no unmapped case for it to guard against -- a
    trailing `raise` for "no row matched" would be dead code by
    construction, per `_DEVSECRET_EXIT_CODES`'s docstring.
    """
    for error_type, exit_code in _DEVSECRET_EXIT_CODES[:-1]:
        if isinstance(exc, error_type):
            return exit_code
    return _DEVSECRET_EXIT_CODES[-1][1]


def _devsecret_value_as_argument_message() -> str:
    return (
        "ERROR: a secret value may not be supplied as a command-line argument\n"
        "Arguments reach the process table, where any other user on this "
        "machine can read them.\n"
        "Pipe the value on stdin instead: printf '%s' \"$VALUE\" | devsecret set <NAME>"
    )


def _devsecret_tty_without_stdin_flag_message() -> str:
    return (
        "ERROR: stdin is a terminal\n"
        "An interactive paste must be deliberate; pass --stdin to confirm the "
        "value is being typed or pasted now.\n"
        "Otherwise pipe the value: printf '%s' \"$VALUE\" | devsecret set <NAME>"
    )


def _devsecret_missing_scope_message(scopes_in_effect: Sequence[str]) -> str:
    effective = ", ".join(scopes_in_effect)
    return (
        "ERROR: --scope is required\n"
        f"The scopes in effect are: {effective}.\n"
        "Deleting from the wrong tier is silent until something downstream "
        "breaks; name the scope explicitly, for example --scope shared."
    )


def _devsecret_unknown_scope_message(requested_scope: str, scopes_in_effect: Sequence[str]) -> str:
    effective = ", ".join(scopes_in_effect)
    return (
        f"ERROR: unknown scope {requested_scope!r}\n"
        f"The scopes in effect are: {effective}.\n"
        "Pass one of these scopes, or omit --scope to reach the scopes in effect."
    )


def _require_known_scope(scope: str) -> None:
    """Raise `UnknownScopeError` if `scope` is outside `catalog.scope_set(None)`.

    `list` enforces scope membership through `catalog.list_resolved`
    (AC-FUNC-004: an unrecognized scope exits 2). `set` and `rm` instead
    write directly through `catalog.parameter_path`, which validates only a
    scope's character shape, not its membership in the resolution set --
    without this check, a mistyped `--scope` on `set` would silently write
    a secret into a tier `get` and `list` can never reach, exactly the
    silent-wrong-tier failure this task's own rationale for requiring
    `--scope` on `rm` describes. Calling this from both scope-accepting
    write paths (`set`, `rm`) keeps one scope rule in effect across every
    command that touches a scope.
    """
    scopes = catalog.scope_set(None)
    if scope not in scopes:
        raise catalog.UnknownScopeError(_devsecret_unknown_scope_message(scope, scopes))


def _build_devsecret_parser() -> argparse.ArgumentParser:
    """The `devsecret` top-level parser: get, list, set and rm (spec Section 4.3).

    A separate parser from `_build_parser`'s (this module's other console
    entry point, `devcontainer_config`'s own lint-secrets/hooks-* commands):
    `devsecret` is installed as its own command (spec Section 4.3, decision
    D13) with its own `prog` and its own `--help` reference (spec Section
    14.2), not a subcommand of `devcontainer_config`. `run` and
    `export-list` (E3-F2-S1-T2) are two more subparsers added here, not a
    restructuring of this function.
    """
    parser = argparse.ArgumentParser(
        prog=_DEVSECRET_PROG,
        description="Read and write the secret catalog (spec Section 4.3).",
        epilog=_DEVSECRET_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    get_parser = subparsers.add_parser("get", help=_DEVSECRET_GET_HELP)
    get_parser.add_argument("name", metavar="NAME", help="The secret name to resolve.")
    get_parser.set_defaults(handler=_run_devsecret_get)

    list_parser = subparsers.add_parser("list", help=_DEVSECRET_LIST_HELP)
    list_parser.add_argument(
        "--scope",
        metavar="<scope>",
        default=None,
        help="Narrow the listing to this scope instead of every scope in effect.",
    )
    list_parser.set_defaults(handler=_run_devsecret_list)

    set_parser = subparsers.add_parser("set", help=_DEVSECRET_SET_HELP)
    set_parser.add_argument("name", metavar="NAME", help="The secret name to write.")
    # A trap, not a real interface: this positional exists only so a value
    # mistakenly passed as an argument can be recognized and refused with
    # exit 5 (AC-FUNC-005) instead of argparse rejecting it as an unknown
    # argument.
    set_parser.add_argument(
        "value",
        nargs="?",
        default=None,
        metavar="VALUE",
        help="Never supply the value here; pipe it on stdin instead (refused with exit 5).",
    )
    set_parser.add_argument(
        "--scope",
        metavar="<scope>",
        default=catalog.SHARED_SCOPE,
        help=f"Defaults to {catalog.SHARED_SCOPE!r}.",
    )
    set_parser.add_argument(
        "--exported", action="store_true", help="Mark the secret exported for shell startup."
    )
    set_parser.add_argument(
        "--stdin",
        action="store_true",
        help="Confirm that an interactive paste on a TTY is deliberate.",
    )
    set_parser.set_defaults(handler=_run_devsecret_set)

    rm_parser = subparsers.add_parser("rm", help=_DEVSECRET_RM_HELP)
    rm_parser.add_argument("name", metavar="NAME", help="The secret name to delete.")
    rm_parser.add_argument(
        "--scope",
        metavar="<scope>",
        default=None,
        help="Required: the scope to delete from.",
    )
    rm_parser.set_defaults(handler=_run_devsecret_rm)

    return parser


_LISTING_COLUMNS = ("NAME", "SCOPE", "LAST-CHANGED", "EXPORTED")


def _render_secret_listing(records: Sequence[catalog.SecretRecord]) -> str:
    """Render `records` as the four-column table spec Section 4.3 defines.

    Never given anything but `SecretRecord`s, which never carry a value
    field (AC-FUNC-003): this function structurally cannot render one.
    """
    header = (
        f"{_LISTING_COLUMNS[0]:<32} {_LISTING_COLUMNS[1]:<12} "
        f"{_LISTING_COLUMNS[2]:<28} {_LISTING_COLUMNS[3]}"
    )
    lines = [header]
    for record in records:
        exported = "yes" if record.exported else "no"
        lines.append(f"{record.name:<32} {record.scope:<12} {record.last_modified:<28} {exported}")
    return "\n".join(lines)


def _confirm_delete(name: str, scope: str) -> bool:
    """Prompt on stdout, read one line from stdin, and answer whether it was affirmative.

    Reads `sys.stdin` directly rather than calling `input()`: `_run_devsecret_set`
    already reads `sys.stdin` directly for the value itself, and using the
    same mechanism here keeps confirmation testable with a plain
    `io.StringIO` stand-in for stdin, with no dependency on `input()`'s own
    TTY detection.
    """
    print(f"Delete {name!r} in scope {scope!r}? [y/N] ", end="", flush=True)
    answer = sys.stdin.readline()
    return answer.strip().lower() in {"y", "yes"}


def _run_devsecret_get(args: argparse.Namespace, client: catalog.CatalogClient) -> int:
    """AC-FUNC-001/002: resolve `args.name` and print only the value.

    `instance` is always `None` (see the module docstring): the resolution
    set `catalog.scope_set(None)` computes is the shared scope alone, the
    correct answer for an engine with no instance-detection mechanism yet
    (decision D11), not a partial implementation of instance-first
    resolution.
    """
    resolved = catalog.resolve(client, None, args.name)
    sys.stdout.write(resolved.value)
    return EXIT_SUCCESS


def _run_devsecret_list(args: argparse.Namespace, client: catalog.CatalogClient) -> int:
    """AC-FUNC-003/004: render the four-column, value-free listing."""
    records = catalog.list_resolved(client, None, scope=args.scope)
    print(_render_secret_listing(records))
    return EXIT_SUCCESS


def _run_devsecret_set(args: argparse.Namespace, client: catalog.CatalogClient) -> int:
    """AC-FUNC-005/006/007: stdin-only write, TTY refusal, and the version-naming success line.

    Order matters: the positional-argument refusal is checked before
    anything else touches the catalog or stdin (AC-FUNC-005); the name and
    scope are validated next (`catalog.parameter_path` raises before this
    call returns, and `_require_known_scope` raises if `--scope` is not one
    of `catalog.scope_set(None)`), so a malformed name or an unrecognized
    scope never reaches the TTY prompt (AC-FUNC-009) and never silently
    writes into a tier `get` and `list` can never reach; stdin is read only
    after every check passes, so a request that was always going to be
    refused never consumes it.
    """
    if args.value is not None:
        print(_devsecret_value_as_argument_message(), file=sys.stderr)
        return EXIT_VALUE_EXPOSURE_REFUSED
    path = catalog.parameter_path(args.scope, args.name)
    _require_known_scope(args.scope)
    if sys.stdin.isatty() and not args.stdin:
        print(_devsecret_tty_without_stdin_flag_message(), file=sys.stderr)
        return EXIT_USAGE_ERROR
    value = sys.stdin.read()
    version = client.write(args.scope, args.name, value, exported=args.exported)
    print(f"Wrote {path} (SecureString, version {version}).")
    if args.exported:
        print(f"Exported. Shell startup exports this as {args.name}.")
    else:
        print(f"Not exported. Agents reach it with: devsecret get {args.name}")
    return EXIT_SUCCESS


def _run_devsecret_rm(args: argparse.Namespace, client: catalog.CatalogClient) -> int:
    """AC-FUNC-008: a required, named, known scope; deletes only after confirmation."""
    if args.scope is None:
        print(_devsecret_missing_scope_message(catalog.scope_set(None)), file=sys.stderr)
        return EXIT_USAGE_ERROR
    catalog.parameter_path(args.scope, args.name)
    _require_known_scope(args.scope)
    if not _confirm_delete(args.name, args.scope):
        print(f"Not deleted: {args.name!r} in scope {args.scope!r}.")
        return EXIT_SUCCESS
    client.delete(args.scope, args.name)
    print(f"Deleted {args.name!r} from scope {args.scope!r}.")
    return EXIT_SUCCESS


def _build_production_catalog_client() -> catalog.CatalogClient:
    """The default CatalogClient `devsecret` constructs outside a test.

    No region is passed (spec Section 5.4, decision D11): the `aws` CLI
    resolves it the same way any other invocation on this host does, from
    `AWS_DEFAULT_REGION` or the active profile, so this module hardcodes
    neither a default region nor an environment variable name of its own.
    """
    return catalog.CatalogClient(catalog.subprocess_runner)


def main_devsecret(
    argv: Sequence[str] | None = None, *, client: catalog.CatalogClient | None = None
) -> None:
    """Parse `argv`, run the selected devsecret command, and exit the process.

    `client` is the one seam this entry point exposes for a test: a caller
    that supplies one (an injected fake runner's `CatalogClient`, per
    E3-F1-S1-T1) reaches the catalog with no network, no AWS and no docker;
    the production console script never supplies it, so it always reaches
    `_build_production_catalog_client`'s real subprocess runner instead. The
    exit-code contract (spec Section 4.3, AC-FUNC-011) is applied in exactly
    one place, `_devsecret_exit_code_for`: no handler above chooses a number
    for itself.
    """
    parser = _build_devsecret_parser()
    args = parser.parse_args(argv)
    devsecret_client = client if client is not None else _build_production_catalog_client()
    try:
        exit_code = args.handler(args, devsecret_client)
    except catalog.CatalogError as exc:
        print(str(exc), file=sys.stderr)
        exit_code = _devsecret_exit_code_for(exc)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
