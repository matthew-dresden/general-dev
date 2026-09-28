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
which still reaches the remote in history. Both modes also compare every
scanned line against the hostcreds credentials resolved live from the
host's gitignored manifest (see
`devcontainer_config.secrets.hostcreds_values`): a credential whose source
command fails is reported by name as unavailable rather than failing the
scan, and a resolved value never reaches stdout, stderr or the report.
Either mode prints its report and
calls `sys.exit(1)` if it found anything, `sys.exit(0)` otherwise. There is
no flag, environment variable or marker comment on this command that
suppresses a finding: a finding is either real, and fixed, or a false
positive needing human review, per `CLAUDE.md` -- there is no ignore list.

`creds-init`, `creds-fragments` and `shell-block` are the hostcreds wiring
(the CLI half of what 'make push-creds' and postCreate run; see
`devcontainer_config.hostcreds` for the mechanism's contract). `creds-init`
prompts once, via getpass, for every keychain-source credential the manifest
names whose keychain item does not exist yet, and stores each by piping an
`add-generic-password` command to `security -i` on STDIN -- the value rides
stdin, never argv, mirroring the argv discipline hostcreds.py documents, and
the account is always present (the manifest's, or the credential's own name
as the stable default, because `add-generic-password` without `-a` fails on
current macOS). A value carrying a newline or carriage return is refused
before the document is composed, on the prompt and --stdin paths alike:
`security -i` is line-oriented, so a newline would split the document and
execute the remainder of the value as a second security command.
`--stdin NAME` takes exactly one named credential's value from raw stdin
with no prompt, for automation. `creds-fragments` resolves every manifest
entry on the host and writes one `<NAME>.env` fragment per credential into
`--output-dir` at mode 0600 (created O_EXCL; the caller uses a fresh temp
dir), printing exactly the written names, one per line, so container.sh can
pipe each fragment file into the container over stdin; any entry that
cannot be resolved aborts the run with exit 1, because a container
silently shipping a subset of the manifest is the one failure this command
must make impossible; its `--print-git-hosts` mode prints only the
git-source hostnames (labels, safe to print) so the caller knows whether to
seed ~/.git-credentials. No resolved value ever reaches stdout, stderr,
argv or a log line on any of these paths: the fragment files are the only
place a value is written down.

`hooks-install` and `hooks-check` (spec Section 4.5) wrap
`devcontainer_config.githooks.install_hooks` and `.hooks_status`:
`hooks-install` writes the pre-commit and pre-push hooks and `hooks-check`
reports whether the installed hooks still match what `hooks-install` would
write, without rewriting them. `hooks-pre-push` (spec Section 4.6) is what
the pre-push hook itself execs (via `make hooks-run-push`): it reads git's
own pre-push stdin contract, derives the pushed range for every ref with
`devcontainer_config.githooks.ranges_from_push_refs`, and scans each range
with `scan_range`, exiting 1 if any of them found something.

`devsecret` (spec Section 4.3, decision D13; E3-F2-S1-T1, E3-F2-S1-T2) is
this module's second console entry point, `main_devsecret`, installed by its
own console script (spec Section 4.3: "on PATH in the container and on the
host") rather than as a subcommand of `devcontainer_config`. It exposes all
six commands Section 4.3 names: the four record commands from E3-F2-S1-T1
-- `get`, `list`, `set` and `rm` -- plus `run` and `export-list` from
E3-F2-S1-T2. Its exit codes (spec Section 4.3, 14.2) are declared once as
named constants (`EXIT_SUCCESS`, `EXIT_USAGE_ERROR`, `EXIT_BACKEND_ERROR`,
`EXIT_NOT_FOUND`, `EXIT_VALUE_EXPOSURE_REFUSED`) and mapped from the
`devcontainer_config.catalog.CatalogError` hierarchy by
`_devsecret_exit_code_for`, the single place that mapping is made, so no
handler chooses a number for itself (AC-FUNC-011). Rules that keep a secret
value from ever reaching a place it should not: `list` and `export-list`
call `catalog.list_resolved`, which is built on `describe-parameters` and
never requests decryption, so a value is never held in memory on the
listing path at all (AC-4.3); `set` reads the value from stdin only -- a
value supplied as a positional argument is refused (exit 5) with no part of
it echoed, because arguments reach the process table where any other user
on the machine can read them; `run` resolves every named secret and hands
each one to the child through its environment only, never through argv, and
does so inside `catalog.secret_cache_dir`, which refuses (also exit 5) to
materialize its transient directory anywhere a value could leak into the
workspace or a persistent layer (spec Section 5.4, 7.3). `devsecret`'s own
commands do not resolve an instance: every command here resolves or narrows
against `catalog.scope_set(None)` -- the shared scope alone, the correct
answer for an engine with no instance (decision D11) -- independent of
`devcontainer_config.instances`, this module's separate instance-resolution
entry point below.
"""

from __future__ import annotations

import argparse
import getpass
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from devcontainer_config import catalog, hostcreds, instances, repo
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

_RESOLVE_INSTANCE_DESCRIPTION = (
    "Resolves which instance INSTANCE/DEFAULT_REMOTE_INSTANCE/the sole "
    "configured directory selects (spec Section 4.1.1) and prints the "
    "resolved name plus the statically derivable half of its Section 9 "
    "addressing block -- Terragrunt directory, state key, docker context, "
    "parameter prefix, certificate directory -- as one KEY=value line per "
    "artifact on stdout, so the shell layer reads values instead of "
    "re-deriving them. Prints nothing to stdout and exits 1 on any "
    "resolution failure, with the operator-facing text on stderr."
)

_RESOLVE_INSTANCE_LOCAL_BACKEND_HELP = (
    "Pass this when the local backend is active (spec Section 1.1): "
    "INSTANCE, if set, is unused on that backend and is reported as a "
    "warning on stderr rather than an error, and nothing is resolved."
)

_CREDS_INIT_DESCRIPTION = (
    "For every keychain-source credential the hostcreds manifest names "
    "whose keychain item does not exist yet, prompt once (getpass) and "
    "store it by piping an add-generic-password command to 'security -i' "
    "on stdin. The value rides stdin, never argv. Re-probes after storing; "
    "exits 0 only when every keychain credential named by the manifest "
    "exists, reporting stored and already-present names."
)

_CREDS_INIT_STDIN_HELP = (
    "Read the value for exactly this one named credential from raw stdin "
    "(one trailing newline stripped; empty input or a value containing a "
    "newline is an error) instead of prompting for it. The automation path; "
    "other missing keychain items still prompt."
)

_CREDS_FRAGMENTS_DESCRIPTION = (
    "Resolve every credential the hostcreds manifest names on this host "
    "and write one <NAME>.env fragment per credential into --output-dir "
    "(mode 0600, created O_EXCL). Prints exactly the written names, one "
    "per line, so the caller can pipe each fragment into the container "
    "over stdin. Fails fast: any credential that cannot be resolved "
    "aborts the run with exit 1, named on stderr -- a container must "
    "never ship a subset of the manifest. With --print-git-hosts, prints "
    "only the git-source hostnames and writes nothing. No value is ever "
    "printed."
)

_CREDS_FRAGMENTS_OUTPUT_DIR_HELP = (
    "The directory to write one <NAME>.env fragment into per resolved "
    "credential. Required unless --print-git-hosts is given. Must not "
    "already hold a fragment of the same name: fragments are created with "
    "O_EXCL so a stale one fails loudly instead of being overwritten."
)

_CREDS_FRAGMENTS_PRINT_GIT_HOSTS_HELP = (
    "Print the 'host' label of every git-source manifest entry, one per "
    "line, and write no fragments. A hostname is a label, never a value, "
    "so it is safe to print; the caller uses it to decide whether to seed "
    "~/.git-credentials."
)

_SHELL_BLOCK_DESCRIPTION = (
    "Print the hostcreds credential-startup block: the shell-agnostic text "
    "postCreate appends to ~/.bashrc and ~/.zshenv, which sources every "
    "fragment under ~/.hostcreds/ at shell startup. Takes no arguments; "
    "the first line of the output is the idempotence marker."
)

# The remedy appended to a hostcreds ManifestError on the creds commands.
# load_manifest's own message already names the path and the committed
# example; this adds the command that creates the file from that example,
# which is the creds commands' own entry point into that fix.
_MANIFEST_MAKE_INIT_HINT = (
    "Run 'make init' to create the manifest from its committed example, then retry."
)


# Rendered by `_unresolved_credentials_message` when any manifest entry
# fails to resolve: an operator-facing summary stating that the push
# aborted, never a value.
def _unresolved_credentials_message(names: Sequence[str]) -> str:
    """The summary for a run where any manifest entry failed to resolve.

    Fail-fast, not skip-and-continue: a container that received a subset
    of the manifest's credentials would start half-configured with exit 0
    as the only record, so any unresolved entry fails the run. The
    resolver's own messages above already name each credential and its
    remedy; this names them together and states the consequence.
    """
    return (
        "ERROR: the push aborted because a credential was unavailable: "
        f"{', '.join(names)}\n"
        "Every failure above names its credential and its remedy; a "
        "keychain item that is missing is created by 'make creds-init'.\n"
        "Fix every credential listed, then run 'make push-creds' again."
    )


def _newline_value_message(name: str) -> str:
    """The refusal for a value carrying a newline or carriage return.

    `security -i` reads its command document line by line, so a newline
    inside the quoted value would end the command there and run the
    remainder of the value as a second security command. The refusal fires
    before the document is composed, names the credential and never the
    value.
    """
    return (
        f"ERROR: the value given for {name} contains a newline or carriage "
        "return; nothing was stored\n"
        "'security -i' reads one command per line, so a newline inside the "
        "value would split the document and run the remainder as another "
        "security command.\n"
        "Store a value without line breaks, then retry."
    )


_CREDS_FRAGMENTS_OUTPUT_DIR_REQUIRED_MESSAGE = (
    "ERROR: creds-fragments requires --output-dir\n"
    "Without it there is nowhere to write the <NAME>.env fragments. Pass "
    "--output-dir DIR, or --print-git-hosts to print the git-source hosts "
    "instead of writing anything."
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

    resolve_instance_parser = subparsers.add_parser(
        "resolve-instance",
        help="Resolve INSTANCE/DEFAULT_REMOTE_INSTANCE and print its addressing block "
        "(spec Section 4.1.1, 9).",
        description=_RESOLVE_INSTANCE_DESCRIPTION,
    )
    resolve_instance_parser.add_argument(
        "--local-backend-active",
        action="store_true",
        help=_RESOLVE_INSTANCE_LOCAL_BACKEND_HELP,
    )
    resolve_instance_parser.set_defaults(handler=_run_resolve_instance)

    instances_parser = subparsers.add_parser(
        "instances",
        help="List every configured instance and mark the active one (spec Section 9).",
        description=_INSTANCES_DESCRIPTION,
    )
    instances_parser.set_defaults(handler=_run_instances)

    creds_init_parser = subparsers.add_parser(
        "creds-init",
        help="Prompt once for each missing keychain credential the hostcreds manifest names.",
        description=_CREDS_INIT_DESCRIPTION,
    )
    creds_init_parser.add_argument("--stdin", metavar="NAME", help=_CREDS_INIT_STDIN_HELP)
    creds_init_parser.set_defaults(handler=_run_creds_init)

    creds_fragments_parser = subparsers.add_parser(
        "creds-fragments",
        help="Resolve every hostcreds manifest entry and write one fragment per credential.",
        description=_CREDS_FRAGMENTS_DESCRIPTION,
    )
    creds_fragments_parser.add_argument(
        "--output-dir", metavar="DIR", help=_CREDS_FRAGMENTS_OUTPUT_DIR_HELP
    )
    creds_fragments_parser.add_argument(
        "--print-git-hosts", action="store_true", help=_CREDS_FRAGMENTS_PRINT_GIT_HOSTS_HELP
    )
    creds_fragments_parser.set_defaults(handler=_run_creds_fragments)

    shell_block_parser = subparsers.add_parser(
        "shell-block",
        help="Print the hostcreds shell-startup block postCreate appends to both rc files.",
        description=_SHELL_BLOCK_DESCRIPTION,
    )
    shell_block_parser.set_defaults(handler=_run_shell_block)

    return parser


_INSTANCES_DESCRIPTION = """List every instance configured under remote-instances/.

Prints one row per instance with its region and docker context, marking the
one the active docker context points at. Reports what it finds rather than
inferring: an instance whose deployment records no region prints a dash, and
when the active context cannot be determined no row is marked, since guessing
which instance is current is worse than saying nothing.
"""


def _run_instances(args: argparse.Namespace) -> int:
    """Render the instance listing as a table.

    The active-context probe is `docker context show`, run here rather than
    inside `instances.listing` so the listing itself stays a pure function and
    a test can drive it without docker present. A probe failure is not fatal:
    the listing still prints, with nothing marked active.
    """
    root = repo.find_root(Path.cwd())
    try:
        completed = subprocess.run(
            ["docker", "context", "show"],
            capture_output=True,
            text=True,
            check=False,
        )
        active = completed.stdout.strip() if completed.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        active = None

    rows = instances.listing(root, active_context=active)
    if not rows:
        print(
            "No instances configured. Run /devcontainer:setup-remote to add one.",
            file=sys.stderr,
        )
        return 0

    name_width = max(len("INSTANCE"), max(len(r.name) for r in rows))
    region_width = max(len("REGION"), max(len(r.region or "-") for r in rows))
    print(f"{'INSTANCE':<{name_width}}  {'REGION':<{region_width}}  ACTIVE  DOCKER CONTEXT")
    for row in rows:
        marker = "  *   " if row.active else "      "
        print(
            f"{row.name:<{name_width}}  {(row.region or '-'):<{region_width}}  "
            f"{marker}  {row.docker_context}"
        )
    return 0


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


def _print_address_block(root: Path, name: str) -> None:
    """The statically derivable half of `name`'s Section 9 addressing block, one line each.

    `forwarded_port` is deliberately excluded: it needs a real docker
    context (AC-FUNC-009 names only "the statically derivable half"), and
    this entry point's own suite runs with no docker, no AWS and no network
    (AC-TEST-004).
    """
    print(f"INSTANCE={name}")
    print(f"TERRAGRUNT_DIR={instances.terragrunt_dir(root, name)}")
    print(f"STATE_KEY={instances.state_key(name)}")
    print(f"DOCKER_CONTEXT={instances.docker_context(root, name)}")
    print(f"PARAMETER_PREFIX={instances.parameter_prefix(name)}")
    print(f"CERTS_DIR={instances.certs_dir(name)}")


def _run_resolve_instance(args: argparse.Namespace) -> int:
    """Resolve an instance under the current repository root and print its addressing block.

    `resolution.warning`, when set (the local-backend edge case, spec
    Section 4.1.1), is printed to stderr regardless of outcome; nothing is
    printed to stdout when `resolution.instance` is `None`, since there is
    no address block to derive without a resolved name.
    """
    root = repo.find_root(Path.cwd())
    resolution = instances.resolve(root, local_backend_active=args.local_backend_active)
    if resolution.warning is not None:
        print(resolution.warning, file=sys.stderr)
    if resolution.instance is None:
        return 0
    _print_address_block(root, resolution.instance)
    return 0


# ---------------------------------------------------------------------------
# hostcreds: creds-init, creds-fragments, shell-block -- the CLI half of
# 'make push-creds' and the postCreate startup-block render, around
# devcontainer_config.hostcreds (which owns the manifest contract, the
# resolvers and the renderers used here).
# ---------------------------------------------------------------------------


def _load_creds_manifest(root: Path) -> tuple[hostcreds.CredentialSpec, ...] | None:
    """The validated manifest specs, or None after printing a `make init` remedy.

    The one loader both creds commands call, so the ManifestError path --
    message from `hostcreds.load_manifest` (which names the path and the
    committed example) plus this module's `make init` hint -- exists once.
    Returns None only after printing; each caller turns that into exit 1.
    """
    try:
        return hostcreds.load_manifest(root)
    except hostcreds.ManifestError as exc:
        print(str(exc), file=sys.stderr)
        print(_MANIFEST_MAKE_INIT_HINT, file=sys.stderr)
        return None


def _minus_one_trailing_newline(text: str) -> str:
    """`text` with exactly one trailing newline removed, mirroring the resolvers.

    Local because `hostcreds._stdout_value` is private; the rule (strip one,
    never all) keeps a value that genuinely ends in newline bytes intact.
    """
    return text[:-1] if text.endswith("\n") else text


def _keychain_item_exists(spec: hostcreds.CredentialSpec, runner: hostcreds.Runner) -> bool:
    """Whether `spec`'s keychain item exists and holds a non-empty password.

    The probe argv is `hostcreds.keychain_find_argv`'s -- the same builder
    `resolve_keychain` uses -- so the probe can never address a different
    item than the push-time resolution would read. A non-zero exit or
    empty output both mean missing: `security` exits non-zero for an
    absent item, and an item that exists with an empty password is no more
    resolvable than an absent one (the resolver refuses empty values), so
    creds-init offers to store over either state.
    """
    result = runner(hostcreds.keychain_find_argv(spec), None)
    return result.returncode == 0 and bool(_minus_one_trailing_newline(result.stdout))


def _security_command_quoted(token: str) -> str:
    """`token` quoted for `security -i`'s command reader.

    The interactive reader splits the command line itself; a double-quoted
    word with backslash-escaped backslashes and quotes carries spaces and
    quotes without a space splitting it into two arguments. It cannot
    carry a newline: the reader is line-oriented, so a newline ends the
    command no matter how it is quoted -- which is why values are rejected
    before this helper composes the document (`_newline_value_message`).
    Labels were already validated to carry no newline or quote by
    `load_manifest`, but they are quoted with the same helper so a space
    inside a service label cannot split the command.
    """
    escaped = token.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _store_keychain_item(
    spec: hostcreds.CredentialSpec, value: str, runner: hostcreds.Runner
) -> subprocess.CompletedProcess[str]:
    """Store `value` as `spec`'s keychain item, the whole command on stdin.

    The value rides the `security -i` stdin document, never argv: `argv` is
    exactly `['security', '-i']`, so the process table never carries the
    value -- the argv discipline hostcreds.py documents, applied to the
    write side. The account is always present -- the manifest's when it
    set one, the credential's own NAME as the stable default otherwise --
    because `add-generic-password` without `-a` fails on current macOS,
    and because the probe (`hostcreds.keychain_find_argv`) must address
    the item by the same service the store wrote. The completed process is
    returned so the caller decides what a non-zero exit means rather than
    this helper exiting for it.
    """
    words = [
        "add-generic-password",
        "-s",
        _security_command_quoted(spec.labels[hostcreds.KEYCHAIN_SERVICE_LABEL]),
        "-a",
        _security_command_quoted(spec.labels.get(hostcreds.KEYCHAIN_ACCOUNT_LABEL, spec.name)),
        "-w",
        _security_command_quoted(value),
    ]
    return runner([hostcreds.SECURITY_EXECUTABLE, "-i"], " ".join(words) + "\n")


def _prompt_for_credential(spec: hostcreds.CredentialSpec) -> str:
    """One getpass prompt for `spec`; the prompt names the credential and its
    service label, never a value.

    Called through the module's `getpass` attribute (resolved at call time,
    not import time) so a test substitutes a stub without patching the
    stdlib globally.
    """
    service = spec.labels[hostcreds.KEYCHAIN_SERVICE_LABEL]
    return getpass.getpass(f"Value for {spec.name} (keychain item '{service}'): ")


def _run_creds_init(args: argparse.Namespace) -> int:
    """Probe, prompt for and store every missing keychain credential the manifest names.

    The runner is `hostcreds.subprocess_runner`, read from the module at
    call time so a hermetic test substitutes a fake there. Exit 0 only when
    every keychain spec's item exists at the end: each store is followed by
    a re-probe, and a probe that still reports missing fails the run. Only
    names are ever reported -- stored and already-present -- never a value.
    A value carrying a newline or carriage return is refused before the
    `security -i` document is composed, on the --stdin path and the prompt
    path alike: the reader is line-oriented, so a newline would split the
    document and execute the remainder of the value as a second command.
    """
    root = repo.find_root(Path.cwd())
    specs = _load_creds_manifest(root)
    if specs is None:
        return 1

    stdin_name: str | None = None
    stdin_value: str | None = None
    if args.stdin is not None:
        named = next((spec for spec in specs if spec.name == args.stdin), None)
        if named is None or named.source != hostcreds.SOURCE_KEYCHAIN:
            print(
                f"ERROR: --stdin names {args.stdin!r}, which the manifest does not name "
                "as a keychain credential\n"
                "Only a keychain-source entry can be stored this way; git and "
                "aws-export entries resolve at push time.\n"
                "Check the name against the manifest, then retry.",
                file=sys.stderr,
            )
            return EXIT_USAGE_ERROR
        stdin_name = args.stdin
        stdin_value = _minus_one_trailing_newline(sys.stdin.read())
        if not stdin_value:
            print(
                f"ERROR: the value read on stdin for {args.stdin} is empty\n"
                "An empty value is never a credential. Supply a non-empty "
                "value on stdin, then retry.",
                file=sys.stderr,
            )
            return EXIT_USAGE_ERROR
        if "\n" in stdin_value or "\r" in stdin_value:
            print(_newline_value_message(args.stdin), file=sys.stderr)
            return EXIT_USAGE_ERROR

    runner = hostcreds.subprocess_runner
    stored: list[str] = []
    present: list[str] = []
    for spec in specs:
        if spec.source != hostcreds.SOURCE_KEYCHAIN:
            continue
        if _keychain_item_exists(spec, runner):
            present.append(spec.name)
            continue
        if spec.name == stdin_name:
            value = stdin_value
            assert value is not None  # guarded above: a non-empty value was read
        else:
            value = _prompt_for_credential(spec)
        if not value:
            print(
                f"ERROR: an empty value was given for {spec.name}; nothing was stored\n"
                "Re-run 'make creds-init' and enter a non-empty value, then retry.",
                file=sys.stderr,
            )
            return 1
        if "\n" in value or "\r" in value:
            print(_newline_value_message(spec.name), file=sys.stderr)
            return 1
        result = _store_keychain_item(spec, value, runner)
        if result.returncode != 0:
            print(
                f"ERROR: storing {spec.name} in the keychain failed\n"
                f"'security -i' exited with status {result.returncode}; its "
                "output is not repeated here because it can echo command "
                "tokens, value fragments included.\n"
                "Run 'security -i' by hand to reproduce the failure, then "
                "re-run 'make creds-init' and re-enter the value.",
                file=sys.stderr,
            )
            return 1
        if not _keychain_item_exists(spec, runner):
            service = spec.labels[hostcreds.KEYCHAIN_SERVICE_LABEL]
            print(
                f"ERROR: {spec.name} was stored, but probing it afterwards found no "
                "value\n"
                f"Run 'security find-generic-password -w -s {service}' "
                "by hand to see why the item is not readable back, then retry.",
                file=sys.stderr,
            )
            return 1
        stored.append(spec.name)

    if stored:
        print(f"stored: {', '.join(stored)}")
    if present:
        print(f"already present: {', '.join(present)}")
    if not stored and not present:
        print("no keychain credentials named by the manifest")
    return 0


def _run_creds_fragments(args: argparse.Namespace) -> int:
    """Resolve every manifest credential into `--output-dir`, or print git hosts.

    The runner is `hostcreds.subprocess_runner`, read from the module at
    call time so a hermetic test substitutes a fake there. stdout carries
    exactly the written names (or, with `--print-git-hosts`, exactly the
    git-source hosts), one per line: container.sh consumes that list, so
    nothing else may share the stream. Every resolution failure prints the
    resolver's own message (which names the credential and its remedy and
    never the value) to stderr as it happens, and the failed names are
    summarized on stderr at the end.

    Exit 0 only when every manifest entry resolved -- including a manifest
    with no entries at all, which is the explicit statement that this
    checkout pushes no host credentials; exit 1 when the manifest is
    missing or malformed, or when ANY entry failed to resolve. A partial
    result is never a success: a container that started with a subset of
    the manifest's credentials would ship silently half-configured.
    """
    root = repo.find_root(Path.cwd())
    specs = _load_creds_manifest(root)
    if specs is None:
        return 1

    if args.print_git_hosts:
        for spec in specs:
            if spec.source == hostcreds.SOURCE_GIT:
                print(spec.labels[hostcreds.GIT_HOST_LABEL])
        return 0

    if args.output_dir is None:
        print(_CREDS_FRAGMENTS_OUTPUT_DIR_REQUIRED_MESSAGE, file=sys.stderr)
        return EXIT_USAGE_ERROR

    runner = hostcreds.subprocess_runner
    written: list[str] = []
    unresolved: list[str] = []
    for spec in specs:
        try:
            credential = hostcreds.resolve(spec, runner)
            fragment = hostcreds.render_env_fragment(credential)
        except hostcreds.HostCredsError as exc:
            print(str(exc), file=sys.stderr)
            unresolved.append(spec.name)
            continue
        # hostcreds.write_fragment persists the fragment at 0600, O_EXCL,
        # with the value never leaving the file it lands in.
        hostcreds.write_fragment(Path(args.output_dir), spec.name, fragment)
        written.append(spec.name)

    for name in written:
        print(name)
    if unresolved:
        print(_unresolved_credentials_message(unresolved), file=sys.stderr)
        return 1
    return 0


def _run_shell_block(args: argparse.Namespace) -> int:
    """Print the deterministic hostcreds startup block; no arguments.

    What `.devcontainer/.devcontainer.postcreate.sh` renders once and
    appends, marker-guarded, to both ~/.bashrc and ~/.zshenv: the block is
    shell-agnostic, so one render serves both files. Written with
    `sys.stdout.write` rather than `print` because the rendered block
    already ends in exactly one newline.
    """
    sys.stdout.write(hostcreds.render_startup_block())
    return 0


def main(argv: Sequence[str] | None = None) -> None:
    """Parse `argv`, run the selected command, and exit the process.

    One of this module's two public console entry points (AC-FUNC-006), and
    the only `sys.exit` site on the `devcontainer_config` command path: every
    command handler raises `SecretScanError`, `repo.RepoError`,
    `GitHooksError`, `instances.InstancesError` or `hostcreds.HostCredsError`
    on a real failure instead of exiting itself, and this is where that
    exception becomes an exit code -- printed with an `ERROR:` prefix to
    stderr, never a stack trace.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        exit_code = args.handler(args)
    except (
        SecretScanError,
        repo.RepoError,
        GitHooksError,
        instances.InstancesError,
        hostcreds.HostCredsError,
    ) as exc:
        print(str(exc), file=sys.stderr)
        exit_code = 1
    sys.exit(exit_code)


# ---------------------------------------------------------------------------
# devsecret: get, list, set, rm, run, export-list (spec Section 4.3, 14.2;
# E3-F2-S1-T1, E3-F2-S1-T2).
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
_DEVSECRET_RUN_HELP = (
    "run --secrets A,B -- <cmd>: run <cmd> with only those secrets in its environment."
)
_DEVSECRET_EXPORT_LIST_HELP = "export-list: names marked exported, for shell startup."

_DEVSECRET_RUN_SECRETS_HELP = (
    "Comma-separated secret names to add to the child's environment. Empty (the "
    "default) runs the command with no secrets, never with all of them."
)
_DEVSECRET_RUN_COMMAND_HELP = "The command to execute, after a '--' separator."

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
    (catalog.SecretCacheExposureError, EXIT_VALUE_EXPOSURE_REFUSED),
    (catalog.SecretCacheUnavailableError, EXIT_VALUE_EXPOSURE_REFUSED),
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
    """The `devsecret` top-level parser: get, list, set, rm, run, export-list (spec Section 4.3).

    A separate parser from `_build_parser`'s (this module's other console
    entry point, `devcontainer_config`'s own lint-secrets/hooks-* commands):
    `devsecret` is installed as its own command (spec Section 4.3, decision
    D13) with its own `prog` and its own `--help` reference (spec Section
    14.2), not a subcommand of `devcontainer_config`.
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

    run_parser = subparsers.add_parser("run", help=_DEVSECRET_RUN_HELP)
    run_parser.add_argument(
        "--secrets", metavar="<A,B>", default="", help=_DEVSECRET_RUN_SECRETS_HELP
    )
    # REMAINDER, not a fixed positional count: everything from the first
    # unrecognized token onward is the command to execute, including its own
    # flags (for example a child's own "-la"), which must never be parsed as
    # devsecret's flags. argparse's REMAINDER keeps a leading '--' token
    # rather than stripping it; `_devsecret_run_command` strips it.
    run_parser.add_argument(
        "command", nargs=argparse.REMAINDER, metavar="-- <cmd>", help=_DEVSECRET_RUN_COMMAND_HELP
    )
    run_parser.set_defaults(handler=_run_devsecret_run)

    export_list_parser = subparsers.add_parser("export-list", help=_DEVSECRET_EXPORT_LIST_HELP)
    export_list_parser.set_defaults(handler=_run_devsecret_export_list)

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
    correct answer for an engine with no instance resolved (decision D11),
    independent of `devcontainer_config.instances`, which this module's
    `devsecret` commands do not call.
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


def _parse_secrets_list(raw: str) -> tuple[str, ...]:
    """The ordered secret names `--secrets` names; empty for an empty string (AC-FUNC-002).

    An empty string is not "no names given, so an empty split produces one
    name that happens to be empty" -- it is the empty list itself, so `run`
    resolves nothing and fetches nothing, rather than raising
    `InvalidSecretNameError` on a single blank name.
    """
    if raw == "":
        return ()
    return tuple(raw.split(","))


def _devsecret_run_command(raw_command: Sequence[str]) -> list[str]:
    """`args.command` with the leading '--' argparse's REMAINDER preserves, stripped off."""
    if raw_command and raw_command[0] == "--":
        return list(raw_command[1:])
    return list(raw_command)


def _devsecret_run_missing_command_message() -> str:
    return (
        "ERROR: no command given to run\n"
        "devsecret run --secrets A,B -- <cmd> requires a command after the "
        "'--' separator.\n"
        "Pass the command to execute after '--'."
    )


def _devsecret_run_command_not_found_message(command_name: str, os_reason: str) -> str:
    return (
        f"ERROR: cannot execute {command_name!r}\n"
        f"The command was not found on PATH, is not executable, or is not a "
        f"valid executable for this platform ({os_reason}).\n"
        "Check the command name, and that it is installed and executable, then retry."
    )


def _devsecret_child_exit_code(returncode: int) -> int:
    """AC-FUNC-004: `Popen.returncode`'s negative-signal convention, translated to 128+signal.

    A negative `returncode` is Python's own convention for "this process was
    terminated by signal `-returncode`" on POSIX; translating it to 128 plus
    the signal number matches the exit status a shell reports for a killed
    foreground job. A non-negative `returncode` is the child's own exit
    status, propagated unchanged.
    """
    if returncode < 0:
        return 128 - returncode
    return returncode


def _run_devsecret_run(args: argparse.Namespace, client: catalog.CatalogClient) -> int:
    """AC-FUNC-001 through 008: resolve named secrets into a copy of the environment, then exec.

    Order matters. `catalog.secret_cache_dir` -- with all of its exposure
    refusals -- runs before any secret is resolved (AC-FUNC-007: "before any
    secret is fetched"), and every named secret is resolved before the child
    process is created (AC-FUNC-003: every failure this command owns happens
    before the child exists), so no failure ever reaches a partially
    populated child, and the transient directory is always removed on the
    way out, whatever the child's outcome (AC-FUNC-006).

    The directory named by `catalog.SECRET_CACHE_DIR_ENV_VAR` this command
    hands the child (spec Section 7.3) is never the workspace and never the
    container's persistent layer, and it does not survive this process
    (spec Section 5.4); `catalog.secret_cache_dir` is where that contract is
    enforced, not here.
    """
    command = _devsecret_run_command(args.command)
    if not command:
        print(_devsecret_run_missing_command_message(), file=sys.stderr)
        return EXIT_USAGE_ERROR
    names = _parse_secrets_list(args.secrets)
    repository_root = repo.find_root(Path.cwd())
    container_workspace_root = Path(repo.container_workspace(repository_root))
    with catalog.secret_cache_dir(
        repository_root=repository_root,
        container_workspace_root=container_workspace_root,
    ) as cache_dir:
        child_env = catalog.process_environment()
        child_env[catalog.SECRET_CACHE_DIR_ENV_VAR] = str(cache_dir)
        for name in names:
            resolved = catalog.resolve(client, None, name)
            child_env[name] = resolved.value
        try:
            process = subprocess.Popen(command, env=child_env)
        except OSError as exc:
            # `FileNotFoundError` (not on PATH) and `PermissionError` (not
            # executable) are both `OSError` subclasses; so is the case
            # `Popen` raises for a file that exists and has the exec bit set
            # but is not a valid executable format (`OSError: [Errno 8] Exec
            # format error`). All three are the same contract: the command
            # cannot be executed, exit 2, naming it, after cleanup has run.
            # `exc.strerror` (the OS's own reason, for example "Exec format
            # error") is included so an unrelated `OSError` (`ENOMEM`,
            # `EMFILE`) is not misdiagnosed as "not found on PATH" with the
            # real reason discarded.
            os_reason = exc.strerror if exc.strerror else exc.__class__.__name__
            print(
                _devsecret_run_command_not_found_message(command[0], os_reason),
                file=sys.stderr,
            )
            return EXIT_USAGE_ERROR
        process.wait()
    return _devsecret_child_exit_code(process.returncode)


def _export_list_names(records: Sequence[catalog.SecretRecord]) -> list[str]:
    """The exported names in `records`, in `records` order (AC-FUNC-009).

    `list_resolved` (spec Section 5.4) already decided which record is in
    effect and stamped exactly one `in_effect=True` record per name; this
    function trusts that decision rather than re-deriving it. Filtering on
    `in_effect` before `exported` means a name whose in-effect record is not
    exported is never printed here even when a shadowed record for the same
    name is exported, so a name marked exported in more than one scope is
    both printed once and printed on the authority of the record a shell
    export at E3-F2-S2-T1 actually uses.
    """
    return [record.name for record in records if record.in_effect and record.exported]


def _run_devsecret_export_list(args: argparse.Namespace, client: catalog.CatalogClient) -> int:
    """AC-FUNC-009/010: the exported names, one per line, never a value.

    Built on `catalog.list_resolved`, the same metadata-only listing `list`
    uses (AC-4.3): the store's `describe-parameters` response has no field
    that could carry a value, so this command structurally cannot print one.
    """
    records = catalog.list_resolved(client, None)
    for name in _export_list_names(records):
        print(name)
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

    `run` (`_run_devsecret_run`) is the one handler that also calls
    `repo.find_root`, and `devsecret` is on PATH in the container and on the
    host, so a cwd outside any git checkout is an ordinary invocation, not a
    crash: `repo.RepoError` is mapped to `EXIT_USAGE_ERROR` here, the same
    `ERROR: ...` shape and no traceback as every other error this entry
    point owns.
    """
    parser = _build_devsecret_parser()
    args = parser.parse_args(argv)
    devsecret_client = client if client is not None else _build_production_catalog_client()
    try:
        exit_code = args.handler(args, devsecret_client)
    except catalog.CatalogError as exc:
        print(str(exc), file=sys.stderr)
        exit_code = _devsecret_exit_code_for(exc)
    except repo.RepoError as exc:
        print(str(exc), file=sys.stderr)
        exit_code = EXIT_USAGE_ERROR
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
