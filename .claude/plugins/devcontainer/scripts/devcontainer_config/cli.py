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

`instance-init`, `instance-list`, `instance-status`, `instance-stop`,
`instance-start`, `instance-link` and `instance-cleanup` are thin handlers
over `devcontainer_config.instance_ops` (spec Section 4.5), the engine the
`make instance-*` targets and `make list-instances` also drive. Each handler
resolves the repository root, reads `instance_ops.subprocess_runner` from the
module at call time (so a hermetic test substitutes a fake there, the same
seam the creds commands use), and prints the engine's messages verbatim;
`instance-list` and `instance-status` carry `--json`, which prints exactly
one single-line JSON object per instance (or for the one instance) and no
summary line, for machine consumption. `instance-link` is the one handler
with a check of its own: it refuses an id outside the `i-`-plus-lowercase-hex
shape as a usage error before the store is written, because a typo'd id would
misdirect every power and status operation at once.

`skills-install`, `skills-remove` and `skills-list` are the skills surface
(U3): thin handlers over `devcontainer_config.skills_install`, the engine
the `make skills-install`, `make skills-remove` and `make skills-list`
targets also drive. Each takes `--agent` and `--scope` (defaults `both`
and `global`, the same values the Makefile declares; `argparse`'s
`choices=` is the validation, so an invalid value is a usage error listing
the accepted ones). `skills-install` at the `runtime` scope prints the
agent's one-shot incantation -- `OPENCODE_CONFIG` for opencode, a
`--settings` JSON for Claude Code -- and never executes it; the engine
owns every refusal and every filesystem rule (delete only our own
repository-pointing symlink), this layer only prints its messages and
maps `SkillsInstallError` to exit 1.

This module exposes no console script and installs none: its CLI entry is
`python3 -m devcontainer_config.cli` (the form `make creds-init`,
`make lint-secrets` and the postCreate startup-block render all invoke),
so `pyproject.toml` declares no `[project.scripts]` and no build backend.
"""

from __future__ import annotations

import argparse
import getpass
import json
import re
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path

from devcontainer_config import hostcreds, instance_ops, instances, repo, skills_install
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

# The skills surface (U3): three thin handlers over
# devcontainer_config.skills_install, the engine the make skills-* targets
# also drive. AGENT and SCOPE carry the make variables' values and carry
# the same defaults the Makefile declares (AGENT ?= both, SCOPE ?= global),
# so a direct cli call behaves like the target with no variables set.
# argparse's choices= is the validation: an invalid value is a usage error
# (exit 2) whose message lists the valid ones, and nothing reaches the
# engine unvalidated.
_SKILLS_AGENT_HELP = (
    f"Which agent surface to act on: {', '.join(skills_install.AGENTS)}, "
    f"or {skills_install.AGENT_BOTH} (the default)."
)

_SKILLS_SCOPE_HELP = (
    f"Which scope to act on: {skills_install.SCOPE_GLOBAL} (the user-level "
    f"skill directories), {skills_install.SCOPE_PROJECT} (the in-repo "
    f"adapters), or {skills_install.SCOPE_RUNTIME} (print the one-shot "
    "incantation, changing nothing)."
)

_SKILLS_INSTALL_DESCRIPTION = (
    "Wire an agent's skill surface to this checkout's canonical .agents/"
    "skills home. SCOPE=global creates the general-dev-skills symlink in "
    "the agent's user-level skill directory (absolute target, recorded); "
    "SCOPE=project verifies -- and, for Claude Code, wires -- the in-repo "
    "adapters; SCOPE=runtime prints the agent's one-shot incantation "
    "(OPENCODE_CONFIG for opencode, --settings for Claude Code) and never "
    "executes it. Never copies a skill body: a copy would be a second "
    "source of truth."
)

_SKILLS_REMOVE_DESCRIPTION = (
    "Unwire an agent's skill surface. Only SCOPE=global deletes anything, "
    "and only a general-dev-skills symlink whose resolved target is inside "
    "this repository: a non-symlink is refused, a symlink resolving "
    "elsewhere is refused (your own skills are never touched), and sibling "
    "entries are never examined. SCOPE=project and SCOPE=runtime report why "
    "they leave the filesystem as found."
)

_SKILLS_LIST_DESCRIPTION = (
    "Report each selected agent's state at SCOPE: installed (with the "
    "resolved target) or not installed for the global scope, native or "
    "wired for the project scope; the runtime scope holds no persistent "
    "state and names where its one-shot incantation is printed."
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

    instance_init_parser = subparsers.add_parser(
        "instance-init",
        help="Scaffold remote-instances/<name>/terragrunt.hcl for a new instance; never deploys.",
        description=_INSTANCE_INIT_DESCRIPTION,
    )
    instance_init_parser.add_argument("name", metavar="NAME", help=_INSTANCE_NAME_HELP)
    instance_init_parser.add_argument(
        "--region", default=_DEFAULT_REGION, help=_INSTANCE_INIT_REGION_HELP
    )
    instance_init_parser.add_argument("--ami", metavar="AMI_ID", help=_AMI_HELP)
    instance_init_parser.set_defaults(handler=_run_instance_init)

    instance_list_parser = subparsers.add_parser(
        "instance-list",
        help="List every configured instance with its live state (spec Section 4.5).",
        description=_INSTANCE_LIST_DESCRIPTION,
    )
    instance_list_parser.add_argument("--json", action="store_true", help=_INSTANCE_JSON_HELP)
    instance_list_parser.set_defaults(handler=_run_instance_list)

    instance_status_parser = subparsers.add_parser(
        "instance-status",
        help="Report one instance's live state (spec Section 4.5).",
        description=_INSTANCE_STATUS_DESCRIPTION,
    )
    instance_status_parser.add_argument("name", metavar="NAME", help=_INSTANCE_NAME_HELP)
    instance_status_parser.add_argument("--json", action="store_true", help=_INSTANCE_JSON_HELP)
    instance_status_parser.set_defaults(handler=_run_instance_status)

    instance_stop_parser = subparsers.add_parser(
        "instance-stop",
        help="Stop one instance's EC2 instance and wait until it reports stopped.",
        description=_INSTANCE_STOP_DESCRIPTION,
    )
    instance_stop_parser.add_argument("name", metavar="NAME", help=_INSTANCE_NAME_HELP)
    instance_stop_parser.add_argument("--region", default=_DEFAULT_REGION, help=_REGION_HELP)
    instance_stop_parser.set_defaults(handler=_run_instance_stop)

    instance_start_parser = subparsers.add_parser(
        "instance-start",
        help="Start one instance's EC2 instance and wait for its SSM agent to report ready.",
        description=_INSTANCE_START_DESCRIPTION,
    )
    instance_start_parser.add_argument("name", metavar="NAME", help=_INSTANCE_NAME_HELP)
    instance_start_parser.add_argument("--region", default=_DEFAULT_REGION, help=_REGION_HELP)
    instance_start_parser.set_defaults(handler=_run_instance_start)

    instance_link_parser = subparsers.add_parser(
        "instance-link",
        help="Record one instance's EC2 id in the per-instance id store.",
        description=_INSTANCE_LINK_DESCRIPTION,
    )
    instance_link_parser.add_argument("name", metavar="NAME", help=_INSTANCE_NAME_HELP)
    instance_link_parser.add_argument(
        "--instance-id", metavar="INSTANCE_ID", required=True, help=_INSTANCE_ID_HELP
    )
    instance_link_parser.set_defaults(handler=_run_instance_link)

    instance_cleanup_parser = subparsers.add_parser(
        "instance-cleanup",
        help="Tear down everything one instance scattered outside its Terragrunt directory.",
        description=_INSTANCE_CLEANUP_DESCRIPTION,
    )
    instance_cleanup_parser.add_argument("name", metavar="NAME", help=_INSTANCE_NAME_HELP)
    instance_cleanup_parser.add_argument("--region", default=_DEFAULT_REGION, help=_REGION_HELP)
    instance_cleanup_parser.set_defaults(handler=_run_instance_cleanup)

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

    skills_install_parser = subparsers.add_parser(
        "skills-install",
        help="Wire an agent's skill surface to this checkout's .agents/skills home.",
        description=_SKILLS_INSTALL_DESCRIPTION,
    )
    skills_install_parser.add_argument(
        "--agent",
        default=skills_install.AGENT_BOTH,
        choices=skills_install.AGENT_CHOICES,
        help=_SKILLS_AGENT_HELP,
    )
    skills_install_parser.add_argument(
        "--scope",
        default=skills_install.SCOPE_GLOBAL,
        choices=skills_install.SCOPES,
        help=_SKILLS_SCOPE_HELP,
    )
    skills_install_parser.set_defaults(handler=_run_skills_install)

    skills_remove_parser = subparsers.add_parser(
        "skills-remove",
        help="Unwire an agent's skill surface, deleting only links that point inside this repo.",
        description=_SKILLS_REMOVE_DESCRIPTION,
    )
    skills_remove_parser.add_argument(
        "--agent",
        default=skills_install.AGENT_BOTH,
        choices=skills_install.AGENT_CHOICES,
        help=_SKILLS_AGENT_HELP,
    )
    skills_remove_parser.add_argument(
        "--scope",
        default=skills_install.SCOPE_GLOBAL,
        choices=skills_install.SCOPES,
        help=_SKILLS_SCOPE_HELP,
    )
    skills_remove_parser.set_defaults(handler=_run_skills_remove)

    skills_list_parser = subparsers.add_parser(
        "skills-list",
        help="Report each agent scope's state: installed, native, or not installed.",
        description=_SKILLS_LIST_DESCRIPTION,
    )
    skills_list_parser.add_argument(
        "--agent",
        default=skills_install.AGENT_BOTH,
        choices=skills_install.AGENT_CHOICES,
        help=_SKILLS_AGENT_HELP,
    )
    skills_list_parser.add_argument(
        "--scope",
        default=skills_install.SCOPE_GLOBAL,
        choices=skills_install.SCOPES,
        help=_SKILLS_SCOPE_HELP,
    )
    skills_list_parser.set_defaults(handler=_run_skills_list)

    return parser


_INSTANCE_INIT_DESCRIPTION = """Scaffold remote-instances/<name>/terragrunt.hcl for a new instance.

Writes the one file a new instance requires, from the embedded template the
contract remote-instances/README.md fixes. Never runs Terragrunt: applying
the file is `make instance-deploy`'s job. The default AMI is Canonical's
current Ubuntu 24.04 arm64 image, resolved through SSM in the --region
value -- which is used ONLY for that AMI lookup and the scaffolded
availability zone, never as the deployment region; the deployment region is
REMOTE_AWS_REGION, which has no default. Pass --ami to pin one by hand
instead.
"""

_INSTANCE_LIST_DESCRIPTION = """List every configured instance with its live state.

Prints one aligned row per instance -- EC2 power state, recorded id,
Parameter Store material, client certificate, forwarded port and docker
context -- then a summary line. A probe that could not answer prints a dash
in its column; the first failure's reason is reported on stderr and the
command exits 1, so one unreachable surface degrades its own row instead of
suppressing the listing. With --json, prints exactly one single-line JSON
object per instance (the same fields, lookup_error included) and no summary
line, so scripts consume one object per line.
"""

_INSTANCE_STATUS_DESCRIPTION = """One instance's live state, in the columns instance-list renders.

With --json, prints exactly one single-line JSON object (the same fields
instance-list --json prints per row, lookup_error included). Exits 1 when
any probe failed; the object still prints.
"""
_INSTANCE_STOP_DESCRIPTION = (
    "Stop one instance's EC2 instance and poll until EC2 reports it stopped. "
    "Idempotent: an already-stopped instance returns immediately. Requires the "
    "id `make instance-link` (or instance-deploy) recorded."
)

_INSTANCE_START_DESCRIPTION = (
    "Start one instance's EC2 instance, then poll until its SSM agent reports "
    "ready. Requires the id `make instance-link` (or instance-deploy) recorded."
)

_INSTANCE_LINK_DESCRIPTION = (
    "Record an EC2 instance id in the instance's per-instance id store, the "
    "place the power and status operations read it from. instance-deploy does "
    "this automatically; run it by hand only after re-provisioning outside make."
)

_INSTANCE_CLEANUP_DESCRIPTION = """Tear down one instance's out-of-band state.

Deletes every SSM parameter under the instance's prefix, removes its docker
context, and deletes its certificate-material directory (the recorded
instance-id file included). Every operation is attempted even after an
earlier one failed; all failures are raised together at the end. The
remote-state bucket is deliberately out of scope: the fleet shares one
bucket, so its lifecycle is a Terragrunt/backend concern.
"""

# The region default every aws-touching instance subcommand applies, the same
# default the make instance-* targets apply (spec Section 4.5's worked
# example). Declared once so the four parsers that use it cannot drift.
_DEFAULT_REGION = "us-east-1"

_INSTANCE_NAME_HELP = "The instance name (a project name, e.g. brimbooks)."

_REGION_HELP = (
    "AWS region the operation targets (default: us-east-1, the same default "
    "the make instance-* targets apply)."
)

# instance-init's --region deliberately carries its own help text, distinct
# from _REGION_HELP: the scaffold uses the value only to resolve the default
# AMI from SSM and to derive the availability_zone it writes, while the
# deployment itself runs in whatever REMOTE_AWS_REGION names at make
# instance-deploy time. Sharing the generic wording would let a scaffold-time
# flag read as the deployment region, which it is not.
_INSTANCE_INIT_REGION_HELP = (
    "Region for the scaffold-time AMI lookup and the written availability "
    "zone ONLY (default: us-east-1). Not the deployment region: the "
    "deployment runs in REMOTE_AWS_REGION, which has no default."
)

_AMI_HELP = (
    "Pin this AMI id instead of resolving Canonical's current Ubuntu 24.04 arm64 image from SSM."
)

_INSTANCE_JSON_HELP = (
    "Print machine-readable output instead of the table: exactly one "
    "single-line JSON object per instance (instance-list) or for the one "
    "instance (instance-status), lookup_error included, and no summary line."
)

_INSTANCE_ID_HELP = (
    "The EC2 instance id to record, shaped i- followed by lowercase hex "
    "(what Terragrunt's instance_id output carries)."
)

# The EC2 instance-id shape `instance-link` accepts: `i-` followed by one or
# more lowercase hex characters. The store itself (`instance_ops.link_id`)
# only refuses an empty value, so this is the one place the shape is checked,
# before a typo'd id becomes the thing every power operation addresses.
_INSTANCE_ID_PATTERN = re.compile(r"i-[0-9a-f]+")

_INSTANCE_ID_MALFORMED_MESSAGE = (
    "ERROR: --instance-id must look like i-0123456789abcdefg (i- followed by "
    "lowercase hex)\n"
    "The recorded id is what every power and status operation addresses, so a "
    "typo here misdirects them all at once.\n"
    "Copy the id from Terragrunt's instance_id output, then retry."
)

# The columns the instance-list table (and the single-row instance-status
# table) renders, in order. A probe that could not answer renders as a dash
# via the `_tri_state`/dash conventions in `_instance_row_values`.
_INSTANCE_TABLE_HEADER = ("INSTANCE", "STATE", "ID", "PARAMS", "CERTS", "FORWARD", "CONTEXT")

# The note printed when `remote-instances/` configures no instance at all:
# a listing of zero instances is an ordinary state, not an error.
_NO_INSTANCES_MESSAGE = "No instances configured under remote-instances/."


def _tri_state(value: bool | None) -> str:
    """`yes`/`no` for a probe that answered, `-` for one that could not."""
    return {True: "yes", False: "no"}.get(value, "-")


def _instance_row_values(row: instance_ops.InstanceState, root: Path) -> tuple[str, ...]:
    """One table row for `row`, every unanswerable probe rendered as a dash."""
    if row.context_exists is True:
        context = instances.docker_context(root, row.name)
    elif row.context_exists is False:
        context = "absent"
    else:
        context = "-"
    return (
        row.name,
        row.ec2_state or "-",
        row.recorded_id or "-",
        _tri_state(row.params_present),
        _tri_state(row.certs_present),
        str(row.forward_port) if row.forward_port is not None else "-",
        context,
    )


def _print_instance_table(rows: Sequence[instance_ops.InstanceState], root: Path) -> None:
    """The aligned table: one header row, then one row per `InstanceState`.

    Every column is padded to the widest value it carries, so the table
    reads as columns regardless of name or context length.
    """
    values = [_instance_row_values(row, root) for row in rows]
    widths = [
        max(len(header), *(len(row[column]) for row in values))
        for column, header in enumerate(_INSTANCE_TABLE_HEADER)
    ]
    header = "  ".join(
        column.ljust(width) for column, width in zip(_INSTANCE_TABLE_HEADER, widths, strict=True)
    ).rstrip()
    print(header)
    for row in values:
        print(
            "  ".join(value.ljust(width) for value, width in zip(row, widths, strict=True)).rstrip()
        )


def _report_probe_failures(rows: Sequence[instance_ops.InstanceState]) -> int:
    """Print one stderr line per row whose probes failed; exit 1 if any did.

    The listing itself always prints first; this is the per-row degradation
    the module docstring promises, so one unreachable surface names itself
    on stderr without suppressing what the other instances answered.
    """
    failed = [row for row in rows if row.lookup_error is not None]
    for row in failed:
        assert row.lookup_error is not None  # filtered above
        print(f"ERROR: {row.name}: {row.lookup_error}", file=sys.stderr)
    return 1 if failed else 0


def _run_instance_init(args: argparse.Namespace) -> int:
    """Scaffold the one file a new instance requires; print the guidance verbatim.

    `instance_ops.scaffold` raises `ScaffoldError` on any refusal, which
    `main` converts into exit 1 -- there is no failure this handler reports
    as anything but that exception.
    """
    root = repo.find_root(Path.cwd())
    result = instance_ops.scaffold(
        root, args.name, region=args.region, ami=args.ami, runner=instance_ops.subprocess_runner
    )
    for message in result.messages:
        print(message)
    return 0


def _run_instance_list(args: argparse.Namespace) -> int:
    """List every configured instance's live state, or one JSON object per row.

    The runner is `instance_ops.subprocess_runner`, read from the module at
    call time so a hermetic test substitutes a fake there. --json keeps
    stdout to exactly one single-line JSON object per row (no summary), so a
    consumer can stream it; the summary line and the aligned table are the
    human modes' output only.
    """
    root = repo.find_root(Path.cwd())
    rows = instance_ops.list_state(root, runner=instance_ops.subprocess_runner)
    if not rows:
        print(_NO_INSTANCES_MESSAGE, file=sys.stderr)
        return 0
    if args.json:
        for row in rows:
            print(json.dumps(asdict(row)))
    else:
        _print_instance_table(rows, root)
        failed_count = sum(1 for row in rows if row.lookup_error is not None)
        summary = f"{len(rows)} instance(s) listed"
        if failed_count:
            summary += f", {failed_count} with probe errors"
        print(summary)
    return _report_probe_failures(rows)


def _run_instance_status(args: argparse.Namespace) -> int:
    """Print one instance's live state as a one-row table, or one JSON object."""
    root = repo.find_root(Path.cwd())
    row = instance_ops.state(root, args.name, runner=instance_ops.subprocess_runner)
    if args.json:
        print(json.dumps(asdict(row)))
    else:
        _print_instance_table([row], root)
    return _report_probe_failures([row])


def _run_instance_stop(args: argparse.Namespace) -> int:
    """Stop the instance and print `instance_ops.stop`'s completion message."""
    root = repo.find_root(Path.cwd())
    print(
        instance_ops.stop(
            root, args.name, region=args.region, runner=instance_ops.subprocess_runner
        )
    )
    return 0


def _run_instance_start(args: argparse.Namespace) -> int:
    """Start the instance and print `instance_ops.start`'s completion message."""
    root = repo.find_root(Path.cwd())
    print(
        instance_ops.start(
            root, args.name, region=args.region, runner=instance_ops.subprocess_runner
        )
    )
    return 0


def _run_instance_link(args: argparse.Namespace) -> int:
    """Record the instance id, refusing a malformed one before anything is written.

    The shape check is a usage error (exit code 2, this module's own
    `EXIT_USAGE_ERROR`): a malformed id names no instance AWS can address,
    so it is a malformed command, not a failed operation.
    """
    if _INSTANCE_ID_PATTERN.fullmatch(args.instance_id) is None:
        print(_INSTANCE_ID_MALFORMED_MESSAGE, file=sys.stderr)
        return EXIT_USAGE_ERROR
    root = repo.find_root(Path.cwd())
    print(instance_ops.link_id(root, args.name, args.instance_id))
    return 0


def _run_instance_cleanup(args: argparse.Namespace) -> int:
    """Tear down the instance's out-of-band state, printing each message."""
    root = repo.find_root(Path.cwd())
    for message in instance_ops.cleanup(
        root, args.name, region=args.region, runner=instance_ops.subprocess_runner
    ):
        print(message)
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


def _run_skills_install(args: argparse.Namespace) -> int:
    """Print `skills_install.install`'s messages verbatim; runtime scope included.

    The engine validates AGENT/SCOPE semantics (the parser has already
    constrained the values), resolves the repository root and the real
    home itself being the only machine-specific read, and prints every
    returned line -- the runtime scope's incantations included -- without
    ever executing one.
    """
    root = repo.find_root(Path.cwd())
    for message in skills_install.install(root, Path.home(), args.agent, args.scope):
        print(message)
    return 0


def _run_skills_remove(args: argparse.Namespace) -> int:
    """Print `skills_install.remove`'s messages verbatim; refusals exit 1 via main."""
    root = repo.find_root(Path.cwd())
    for message in skills_install.remove(root, Path.home(), args.agent, args.scope):
        print(message)
    return 0


def _run_skills_list(args: argparse.Namespace) -> int:
    """Print `skills_install.report`'s state lines verbatim; read-only."""
    root = repo.find_root(Path.cwd())
    for message in skills_install.report(root, Path.home(), args.agent, args.scope):
        print(message)
    return 0


def main(argv: Sequence[str] | None = None) -> None:
    """Parse `argv`, run the selected command, and exit the process.

    This module's one public console entry point (AC-FUNC-006), and
    the only `sys.exit` site on the `devcontainer_config` command path: every
    command handler raises `SecretScanError`, `repo.RepoError`,
    `GitHooksError`, `instances.InstancesError`, `instance_ops.InstanceOpsError`,
    `hostcreds.HostCredsError` or `skills_install.SkillsInstallError` on a
    real failure instead of exiting itself, and this is where that exception
    becomes an exit code -- printed with an `ERROR:` prefix to stderr, never
    a stack trace.
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
        instance_ops.InstanceOpsError,
        hostcreds.HostCredsError,
        skills_install.SkillsInstallError,
    ) as exc:
        print(str(exc), file=sys.stderr)
        exit_code = 1
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
