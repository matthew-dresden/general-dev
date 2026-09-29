"""The `devcontainer_config` command-line entry point (spec Section 4.5).

`cli` is the only module in this package that calls `sys.exit`: every other
module raises a `*Error` and lets its caller decide what to do about it.
That split is what lets `secrets.py` and every module like it stay callable
directly from a test, while this module's public console entry point,
`main`, is the only place a process exit code is actually produced
(AC-FUNC-006). No private helper and no library function calls `sys.exit`;
`main` calls it exactly once, as the terminal statement of that function's
body.

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

This module exposes no console script and installs none: its CLI entry is
`python3 -m devcontainer_config.cli` (the form `make creds-init`,
`make lint-secrets` and the postCreate startup-block render all invoke),
so `pyproject.toml` declares no `[project.scripts]` and no build backend.
"""

from __future__ import annotations

import argparse
import getpass
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from devcontainer_config import hostcreds, instances, repo
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

# The exit code for a usage error on a command handler that reports one
# itself (rather than letting argparse exit for it): the same code 2
# argparse uses, declared once so the handlers that return it name the
# concept rather than the number.
EXIT_USAGE_ERROR = 2

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
    """The top-level parser, with every subcommand this module exposes.

    A subparser per verb, not a flat set of top-level flags, so adding the
    next command means adding another subparser here, not restructuring
    this one into something that can hold more than one verb.
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

    This module's one public console entry point (AC-FUNC-006), and
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


if __name__ == "__main__":
    main()
