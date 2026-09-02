"""What the host a skill is running on actually looks like (spec Section 4.5).

`engine` (spec Section 4.2.1) defines the validation contract `setup-local`,
`setup-remote`, `launch` and `doctor` all reuse. Its local check list is made
of host facts: docker CLI present and a context selected; whether a named
context exists; the engine answering; disk headroom; the three files
complete and consistent; `HOST_PROXY` agreeing with a reachable proxy; and,
for the remote path, the AWS identity behind a profile. This module answers
the host-fact subset of that list that this task owns: operating system,
tool presence and version, docker contexts, and AWS identity. The
three-files-consistency check is `verify`'s. Disk headroom and `HOST_PROXY`
reachability are host facts this module does not probe, and neither is
answered by `verify` either: no E1 unit owns them yet. None of the checks
this module does answer are judgments -- they are questions with one
correct, mechanically-derived answer -- so this module answers them in
tested Python instead of a skill's prose, returning a `ProbeResult` per
check that a caller renders into the `engine` verdict table, the `doctor`
findings list, or the `setup-local` prerequisite report, all from the same
data rather than each deriving its own.

Every external command a probe needs is issued through a `CommandRunner`
passed in as a parameter, never through a direct `subprocess` call in this
module. Two things require that: the unit suite runs hermetically, with no
docker, no AWS and no network (spec Section 10.2), so a test drives a probe
with recorded command output instead of shelling out to whatever the test
machine happens to have; and a probe's own logic -- which command to issue,
how to read its output, what a failure means -- is then testable independent
of any real binary ever being on PATH. `CommandResult.binary_missing` and
`CommandResult.timed_out` are carried on the result rather than inferred
from a bare exit code, because "the tool was never on PATH", "the daemon
never answered in time" and "the tool ran and failed on its own terms" are
three different findings with three different remedies, and folding them
into one non-zero exit code would make it impossible for a probe to tell
them apart.
"""

from __future__ import annotations

import json
import math
import os
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

# spec Section 7.3: "DOCKER_HANDSHAKE_TIMEOUT | 30 | Seconds to await the API".
# The only wait in this module -- whether the docker daemon behind the active
# context answers a version call (spec Section 7.2's own example of a
# readiness condition) -- is bounded by this variable, read fresh on every
# probe rather than cached at import time, so a caller (or a test) that sets
# it before probing observes its own value. Public (not underscore-prefixed)
# because `devcontainer_config.transport.handshake` (E6-F2-S1-T2) reads the
# identical variable for its own, retried "does a context's daemon answer"
# check and must read the same name and default this module does, rather
# than declaring a second, independently drifting copy of either
# (AC-FUNC-007: read in exactly one place).
DOCKER_HANDSHAKE_TIMEOUT_ENV_VAR = "DOCKER_HANDSHAKE_TIMEOUT"
DOCKER_HANDSHAKE_TIMEOUT_DEFAULT_SECONDS = 30.0

# The `is_wsl` detection this probe mirrors
# (`.devcontainer/devcontainer-functions.sh`): `uname -r | grep -i
# microsoft`. Checked case-insensitively against `uname -r`'s output before
# the plain Darwin/Linux distinction is even consulted, so a WSL kernel is
# never misreported as plain Linux.
_WSL_KERNEL_MARKER = "microsoft"

_OS_FAMILIES: dict[str, str] = {"Darwin": "macos", "Linux": "linux"}

# This repository does not document a laptop-side docker install step
# anywhere (README.md's remote prerequisites list names "docker CLI" but
# gives no command), so this points at docker's own official install page
# instead of a repo citation. Matches the one this repository's own
# require-command messages already print for the identical condition
# (`.devcontainer/remote-docker/lib.sh`, `container.sh`, `docker-tunnel.sh`),
# and is the single source every docker-absent remedy in this module reads
# from, so the four sites that report a missing docker CLI never drift apart.
_DOCKER_INSTALL_URL = "https://docs.docker.com/engine/install/"

# What every docker-context check in `probe_docker` prevents when it fails,
# worded once here instead of separately at each site that reports the
# context list itself (a passing report and the CLI-absent report both need
# it), so the two copies can never drift apart on wording.
_DOCKER_CONTEXT_LIST_PREVENTS = (
    "every check in this probe, and every skill that needs to know which "
    "docker engine it is talking to"
)

_AWS_EXPIRED_SESSION_MARKERS: tuple[str, ...] = (
    "Error loading SSO Token",
    "session associated with this profile has expired",
    "ExpiredToken",
    "Token has expired",
    "security token included in the request is expired",
)
_AWS_NO_CREDENTIALS_MARKERS: tuple[str, ...] = ("Unable to locate credentials",)


class HostProbeError(RuntimeError):
    """Raised when hostprobe's own configuration is wrong, not the host it inspects.

    Every operational finding about the host -- a missing tool, an expired
    AWS session, an unreachable engine -- is reported as a `ProbeResult`
    instead of raised, because it is exactly the kind of thing a caller
    needs to render into a findings list and continue past. This exception
    is reserved for the one thing that is not an operational finding: a
    value this module was itself configured with (currently, only
    `DOCKER_HANDSHAKE_TIMEOUT`) that cannot be interpreted at all.
    """


@dataclass(frozen=True)
class CommandResult:
    """The outcome of one command issued through a `CommandRunner`.

    `binary_missing` and `timed_out` are distinct from a merely non-zero
    `exit_code`: a tool that ran and failed on its own terms (a bad flag, a
    corrupted config) is a different finding, with a different remedy, from
    a tool that was never on PATH to run at all, or a daemon call that never
    answered within its deadline. Folding either into a bare non-zero exit
    code would make AC-FUNC-004's and AC-FUNC-006's "distinct result"
    guarantees impossible to keep at the data level, and would push the
    distinction down into fragile stderr string-matching at every call site
    that needs it.
    """

    exit_code: int
    stdout: str = ""
    stderr: str = ""
    binary_missing: bool = False
    timed_out: bool = False


# A `CommandRunner` issues `command` and returns what happened, honoring
# `timeout_seconds` when it is not `None`. Every probe in this module takes
# one as a parameter rather than shelling out itself (see the module
# docstring); the real, subprocess-backed implementation is a caller's
# concern, not this module's.
CommandRunner = Callable[[Sequence[str], "float | None"], CommandResult]


@dataclass(frozen=True)
class ProbeResult:
    """One host-fact check, shaped for the `engine` verdict table (spec Section 4.2.1).

    `check` names what was inspected, `found` states the value or condition
    encountered, `ok` is whether the check passed, and `prevents` names the
    concrete failure this check guards against -- populated whether or not
    the check passed, so a caller building a verdict table never needs to
    fabricate the "what does this check protect against" column only when
    something failed (AC-FUNC-008). `remedy` is the exact next step, and is
    empty when `ok` is `True`: there is nothing to remedy.
    """

    check: str
    found: str
    ok: bool
    prevents: str
    remedy: str = ""


def _command_failed_result(
    check: str, prevents: str, command: Sequence[str], result: CommandResult
) -> ProbeResult:
    """The 'the command ran, on PATH, and exited non-zero' finding, built once for every site.

    `probe_operating_system` (both `uname -r` and `uname -s`),
    `_probe_one_tool`, `_probe_context_list` and `_probe_selected_context`
    each detect the same condition and would otherwise hand-build an
    identical `found`/`remedy` pair from `command` and `result`;
    centralizing it here means the wording can never drift apart between
    the five sites, the same reason `_docker_absent_result` exists for the
    "binary not on PATH" case.
    """
    joined = " ".join(command)
    return ProbeResult(
        check=check,
        found=f"{joined} exited {result.exit_code}: {result.stderr.strip()}",
        ok=False,
        prevents=prevents,
        remedy=f"Run {joined} manually and investigate why it failed.",
    )


def probe_operating_system(runner: CommandRunner) -> ProbeResult:
    """Which host family this machine is.

    The host family decides the proxy address a developer is told to use --
    `shell.env.example` and `docs/environment-files.md` document the
    host-specific `HOST_PROXY_URL` to use on macOS, Linux and WSL -- and
    every other host-conditional decision a skill makes. A failure here
    prevents every one of those decisions from having anywhere to start:
    nothing downstream can safely guess a family it was never told.
    """
    check = "operating system: host family"
    prevents = (
        "every host-family-dependent decision, including which proxy address a "
        "developer is told to use"
    )
    uname_r_command = ("uname", "-r")
    kernel = runner(uname_r_command, None)
    if kernel.binary_missing:
        return ProbeResult(
            check=check,
            found="uname is not on PATH",
            ok=False,
            prevents=prevents,
            remedy="hostprobe requires 'uname' on PATH; this host is not supported without it.",
        )
    if kernel.exit_code != 0:
        return _command_failed_result(check, prevents, uname_r_command, kernel)
    if _WSL_KERNEL_MARKER in kernel.stdout.lower():
        return ProbeResult(check=check, found="wsl", ok=True, prevents=prevents)

    uname_s_command = ("uname", "-s")
    system = runner(uname_s_command, None)
    if system.exit_code != 0:
        return _command_failed_result(check, prevents, uname_s_command, system)
    reported = system.stdout.strip()
    family = _OS_FAMILIES.get(reported)
    if family is None:
        return ProbeResult(
            check=check,
            found=f"uname -s reported {reported!r}, which is not a recognized host family",
            ok=False,
            prevents=prevents,
            remedy=(
                "hostprobe recognizes macOS (Darwin), Linux and WSL only; this host "
                "family is not supported."
            ),
        )
    return ProbeResult(check=check, found=family, ok=True, prevents=prevents)


@dataclass(frozen=True)
class _ToolSpec:
    """One prerequisite tool: how to invoke it, how to parse its version, how to install it."""

    name: str
    command: tuple[str, ...]
    version_pattern: re.Pattern[str]
    install_command: str


# The G1 worked example's prerequisite line, in the order it reports them:
# "docker OK, git OK, jq OK, devcontainer CLI OK, uv OK". Where this
# repository documents a tool's install step, the command matches that
# documentation exactly: jq and the devcontainer CLI in README.md's Quick
# start sections, the devcontainer CLI and uv (macOS) in the Makefile's
# `make help` PREREQUISITES block. jq and uv also carry a Linux/WSL
# command, because `probe_operating_system` distinguishes macos, linux and
# wsl and a Homebrew-only remedy fails outright on the latter two; jq's
# second command matches this repository's own precedent for the identical
# tool (`.devcontainer/remote-docker/container.sh`'s jq requirement already
# lists "'brew install jq' or 'apt-get install jq'"). uv's second command,
# `curl -LsSf https://astral.sh/uv/install.sh | sh`, is Astral's own
# standalone installer script for uv -- NOT a repo citation: this repository
# documents only the macOS `brew install uv` step (Makefile PREREQUISITES
# block); the devcontainer itself installs uv from the `astral-sh/uv`
# release tarball (`docs/devcontainer.md`), and CI installs it via the
# `astral-sh/setup-uv` action (`.github/workflows/ci.yml`), neither of which
# is a command a laptop operator would type by hand.
# docker has no install command documented in this repository at all
# (README.md:71 names it a laptop prerequisite but gives no command), so its
# value points at docker's own official install page instead of a repo
# citation; that URL is shared with, and never drifts from,
# `_DOCKER_INSTALL_URL`, which every docker-absent remedy in this module
# also reads from. git is the same story for a command -- README.md:71 names
# it a laptop prerequisite with no command either -- but this repository
# does document where it usually comes from:
# `docs/environment-files.md:157` lists git under "Xcode command line
# tools | usually preinstalled", not a command an operator runs by hand, so
# git's value also points at its own official install page rather than
# that non-command citation.
_TOOL_SPECS: tuple[_ToolSpec, ...] = (
    _ToolSpec(
        name="docker",
        command=("docker", "--version"),
        version_pattern=re.compile(r"Docker version (\d+\.\d+\.\d+)"),
        install_command=_DOCKER_INSTALL_URL,
    ),
    _ToolSpec(
        name="git",
        command=("git", "--version"),
        version_pattern=re.compile(r"git version (\d+\.\d+(?:\.\d+)?)"),
        install_command="https://git-scm.com/downloads",
    ),
    _ToolSpec(
        name="jq",
        command=("jq", "--version"),
        version_pattern=re.compile(r"jq-(\d+\.\d+(?:\.\d+)?)"),
        install_command="'brew install jq' (macOS) or 'apt-get install jq' (Linux/WSL)",
    ),
    _ToolSpec(
        name="devcontainer CLI",
        command=("devcontainer", "--version"),
        version_pattern=re.compile(r"^(\d+\.\d+\.\d+)"),
        install_command="npm install -g @devcontainers/cli",
    ),
    _ToolSpec(
        name="uv",
        command=("uv", "--version"),
        version_pattern=re.compile(r"uv (\d+\.\d+\.\d+)"),
        install_command=(
            "'brew install uv' (macOS) or "
            "'curl -LsSf https://astral.sh/uv/install.sh | sh' (Linux/WSL)"
        ),
    ),
)


def _probe_one_tool(runner: CommandRunner, spec: _ToolSpec) -> ProbeResult:
    """Presence and version of one prerequisite tool named by `spec`.

    A failure here -- absent, present but failing, or present but
    unparsable -- prevents every step of `setup-local`, `setup-remote` and
    `doctor` that needs `spec.name`. An absent tool is reported with its
    install command (see `_TOOL_SPECS` for each tool's provenance); a tool
    that ran and exited non-zero, or produced version output this probe
    cannot parse, is reported with the command to run manually and
    investigate instead, since there is nothing to install.
    """
    check = f"tool: {spec.name}"
    prevents = f"every step of setup-local, setup-remote and doctor that needs {spec.name}"
    result = runner(spec.command, None)
    if result.binary_missing:
        return ProbeResult(
            check=check,
            found=f"{spec.name} is not on PATH",
            ok=False,
            prevents=prevents,
            remedy=f"Install {spec.name}: {spec.install_command}",
        )
    if result.exit_code != 0:
        # Distinct from both "absent" (never ran) and "unparsable" (ran,
        # exited zero, produced output this probe cannot read): the tool
        # ran and failed on its own terms, per the module docstring's own
        # three-findings contract, so it gets its own result carrying the
        # stderr that explains why, instead of falling into the unparsable
        # bucket with an empty-string version match and no explanation.
        return _command_failed_result(check, prevents, spec.command, result)
    match = spec.version_pattern.search(result.stdout)
    if match is None:
        return ProbeResult(
            check=check,
            found=f"{spec.name} produced unparsable version output: {result.stdout.strip()!r}",
            ok=False,
            prevents=prevents,
            remedy=(
                f"Run {' '.join(spec.command)} manually and investigate why its output "
                "does not match the expected version format."
            ),
        )
    return ProbeResult(
        check=check, found=f"{spec.name} {match.group(1)}", ok=True, prevents=prevents
    )


def probe_tools(runner: CommandRunner) -> list[ProbeResult]:
    """Presence and version of every prerequisite tool (G1's worked example).

    A tool this probe finds absent prevents every step of `setup-local`,
    `setup-remote` and `doctor` that needs it, so each result names an
    install command: this repository's own documented command where one
    exists, plus a Linux/WSL equivalent for the two tools (jq, uv) whose
    only repo-documented command is Homebrew-only, and each tool's own
    official install page where this repository documents no command at all
    (docker, git) -- see `_TOOL_SPECS` for the provenance of each value --
    rather than leaving the operator to search for one, or handing a
    Linux/WSL operator a macOS-only command that does not exist on their
    host. A tool that is present but whose version output does not parse is
    reported as its own distinct result (AC-FUNC-004), not folded into
    "absent": one sends the operator to install the tool, the other to
    investigate why a tool that is clearly there produced output this probe
    does not recognize.
    """
    return [_probe_one_tool(runner, spec) for spec in _TOOL_SPECS]


def _docker_absent_result(check: str, prevents: str) -> ProbeResult:
    """The 'docker CLI is not on PATH' finding, built once for every call site that reports it.

    `_probe_context_list`, `_probe_selected_context` and
    `_probe_docker_handshake` each detect the same condition
    (`CommandResult.binary_missing`) and would otherwise hand-build an
    identical `found`/`remedy` pair; centralizing it here means the wording
    and the install remedy can never drift apart between the three sites.
    """
    return ProbeResult(
        check=check,
        found="docker is not on PATH",
        ok=False,
        prevents=prevents,
        remedy=f"Install docker: {_DOCKER_INSTALL_URL}",
    )


def read_positive_seconds(env_var: str, default_seconds: float) -> float:
    """Read `env_var` as a finite, positive number of seconds, or `default_seconds` if unset.

    The one place this repository parses and validates a "positive number
    of seconds" environment variable (AC-FUNC-007's "read in exactly one
    place" requirement, generalized to every caller that needs the same
    shape of deadline): `_docker_handshake_timeout_seconds` below and
    `devcontainer_config.transport._handshake_timeout_seconds`
    (E6-F2-S1-T2) both read the identical `DOCKER_HANDSHAKE_TIMEOUT`
    variable through this function rather than each declaring its own copy
    of the env-var name, the default and this validation. Rejects
    non-positive and non-finite values (`0`, a negative number, `nan`,
    `inf`) with the same clear error as an unparsable one: each would
    otherwise be handed to a caller as a deadline that can never be honored
    (an instantaneous or infinite wait is not a deadline at all), and
    `float()` alone accepts every one of them without complaint. Raises
    `HostProbeError` naming `env_var` and the offending value; a caller in
    another module that needs its own exception type catches this and
    re-raises it as one of its own, so this module's exception type is
    never leaked across a module boundary it does not own.
    """
    raw = os.environ.get(env_var)
    if raw is None:
        return default_seconds
    try:
        value = float(raw)
    except ValueError as exc:
        raise HostProbeError(f"{env_var}={raw!r} is not a number") from exc
    if not math.isfinite(value) or value <= 0:
        raise HostProbeError(f"{env_var}={raw!r} must be a finite positive number")
    return value


def _docker_handshake_timeout_seconds() -> float:
    """The deadline for `docker version` to answer (spec Section 7.3).

    Read fresh on every call, not cached at import time, so a caller that
    sets `DOCKER_HANDSHAKE_TIMEOUT` before probing observes its own value
    and no timeout literal is ever written at a probe call site
    (AC-FUNC-009). Delegates the actual parse and validation to
    `read_positive_seconds`, the single shared reader this variable's name
    and default are declared against (AC-FUNC-007).
    """
    return read_positive_seconds(
        DOCKER_HANDSHAKE_TIMEOUT_ENV_VAR, DOCKER_HANDSHAKE_TIMEOUT_DEFAULT_SECONDS
    )


def _probe_context_list(runner: CommandRunner) -> tuple[ProbeResult | None, list[str]]:
    """The docker CLI's own presence, whether it answered, and the context names it reports.

    Returns a `ProbeResult` in the first slot whenever the context list
    itself could not be established -- the CLI is absent, or `docker
    context ls` ran but exited non-zero (an unreadable context store, a
    corrupt config) -- in every other case the first slot is `None` and the
    second is the parsed context list, which the caller folds into its own
    `ProbeResult`. A non-zero exit is reported distinctly from a zero-context
    list rather than parsed as one: an empty `stdout` on a failed command is
    not "no contexts configured", and folding the two would both mislabel a
    failed command as a passing check and send `_probe_named_context`
    looking for a context in a list this probe never actually obtained.
    """
    context_ls_command = ("docker", "context", "ls", "--format", "{{.Name}}")
    listing = runner(context_ls_command, None)
    if listing.binary_missing:
        return _docker_absent_result(
            check="docker: CLI present", prevents=_DOCKER_CONTEXT_LIST_PREVENTS
        ), []
    if listing.exit_code != 0:
        return (
            _command_failed_result(
                "docker: context list", _DOCKER_CONTEXT_LIST_PREVENTS, context_ls_command, listing
            ),
            [],
        )
    contexts = [line.strip() for line in listing.stdout.splitlines() if line.strip()]
    return None, contexts


def _probe_selected_context(runner: CommandRunner) -> ProbeResult:
    """Which docker context `docker context show` reports as currently active.

    A failure here -- docker absent, the command failing, or no context
    selected at all -- prevents `rdc_backend` from determining local against
    remote (spec Section 1.1): with no reliably known active context, a
    caller cannot tell which engine it is about to talk to.
    """
    check = "docker: selected context"
    prevents = "rdc_backend from determining local against remote (spec Section 1.1)"
    context_show_command = ("docker", "context", "show")
    result = runner(context_show_command, None)
    if result.binary_missing:
        return _docker_absent_result(check=check, prevents=prevents)
    if result.exit_code != 0:
        return _command_failed_result(check, prevents, context_show_command, result)
    selected = result.stdout.strip()
    if not selected:
        return ProbeResult(
            check=check,
            found="docker context show reported no context",
            ok=False,
            prevents=prevents,
            remedy="Select a context: docker context use <name>",
        )
    return ProbeResult(check=check, found=selected, ok=True, prevents=prevents)


def _probe_named_context(requested_context: str, contexts: list[str]) -> ProbeResult:
    """Whether `requested_context` is among the docker contexts this host actually has configured.

    A failure here prevents `setup-local`/`setup-remote` from selecting the
    engine `LOCAL_DOCKER_CONTEXT` or `REMOTE_DOCKER_CONTEXT` names to. Both
    variables live in `.devcontainer/remote-docker/config.env`
    (`docs/environment-files.md`'s "The local docker context" section, and
    the identical disconnect error in `Makefile`'s `disconnect` target), not
    `shell.env` -- `shell.env.example` carries no context variable at all --
    so the remedy names that file, never `shell.env`.
    """
    check = f"docker: context '{requested_context}' exists"
    prevents = (
        "setup-local/setup-remote from selecting the engine LOCAL_DOCKER_CONTEXT or "
        "REMOTE_DOCKER_CONTEXT names"
    )
    if requested_context in contexts:
        return ProbeResult(
            check=check, found=f"{requested_context} is configured", ok=True, prevents=prevents
        )
    listed = ", ".join(contexts)
    return ProbeResult(
        check=check,
        found=f"{requested_context} is not among the configured contexts: {listed!r}",
        ok=False,
        prevents=prevents,
        remedy=(
            "Create it, or correct the context name in "
            f".devcontainer/remote-docker/config.env. Configured: {listed!r}"
        ),
    )


def _probe_docker_handshake(runner: CommandRunner) -> ProbeResult:
    """Whether the daemon behind the active docker context actually answers `docker version`.

    A failure here -- docker absent, no response within
    `DOCKER_HANDSHAKE_TIMEOUT`, or the command itself failing -- prevents
    every command that needs the docker daemon to be reachable: build,
    exec, status.
    """
    check = "docker: engine answers"
    prevents = "every command that needs the docker daemon to be reachable: build, exec, status"
    timeout = _docker_handshake_timeout_seconds()
    result = runner(("docker", "version", "--format", "{{.Server.Version}}"), timeout)
    if result.binary_missing:
        return _docker_absent_result(check=check, prevents=prevents)
    if result.timed_out:
        return ProbeResult(
            check=check,
            found=f"no response within {timeout:g}s ({DOCKER_HANDSHAKE_TIMEOUT_ENV_VAR})",
            ok=False,
            prevents=prevents,
            remedy=(
                "Confirm the engine behind the active docker context is running, then "
                f"retry. {DOCKER_HANDSHAKE_TIMEOUT_ENV_VAR} controls how long this waits."
            ),
        )
    if result.exit_code != 0:
        return ProbeResult(
            check=check,
            found=f"docker version failed: {result.stderr.strip()}",
            ok=False,
            prevents=prevents,
            remedy="Confirm the engine behind the active docker context is running, then retry.",
        )
    return ProbeResult(
        check=check, found=f"server {result.stdout.strip()}", ok=True, prevents=prevents
    )


def probe_docker(runner: CommandRunner, requested_context: str | None = None) -> list[ProbeResult]:
    """Every docker-context fact a skill needs before choosing an engine.

    `rdc_backend` (spec Section 1.1) derives local against remote purely
    from the active docker context, so a skill that gets this wrong talks to
    the wrong engine without any error at all. A missing docker CLI, or a
    `docker context ls` that ran but failed, is reported as its own result
    and nothing else runs (AC-FUNC-006): an empty context list reads as "no
    contexts configured" and sends the operator to the wrong remedy, so a
    genuinely empty list is never conflated with either failure mode. When
    the context list is obtained, this reports it, which context is
    selected, whether `requested_context` (when given) is among them, and
    whether the daemon behind the active context actually answers within
    `DOCKER_HANDSHAKE_TIMEOUT` -- one call sequence, so a caller building a
    verdict table never reconciles facts gathered from separate passes over
    docker's own CLI.
    """
    list_failure, contexts = _probe_context_list(runner)
    if list_failure is not None:
        return [list_failure]

    results = [
        ProbeResult(
            check="docker: context list",
            found=f"{len(contexts)} context(s): {', '.join(contexts)}",
            ok=True,
            prevents=_DOCKER_CONTEXT_LIST_PREVENTS,
        )
    ]
    results.append(_probe_selected_context(runner))
    if requested_context is not None:
        results.append(_probe_named_context(requested_context, contexts))
    results.append(_probe_docker_handshake(runner))
    return results


def probe_aws_identity(runner: CommandRunner, profile: str) -> ProbeResult:
    """Which AWS identity `profile` resolves to, and whether the session is live.

    An expired SSO session is the single most common remote failure, and an
    absent credential is functionally identical from the operator's seat:
    both prevent everything remote depends on -- the SSM tunnel, Parameter
    Store, and the build that publishes to it. Both name the exact recovery
    command, `aws sso login --profile <profile>` with `profile` substituted
    from the caller's argument rather than a fixed name, and this probe
    never falls back to any other credential source: per spec Section 5.4's
    single-backend rule, the store answers for this profile or the check
    fails. An exit-0 response whose `stdout` is not valid JSON, is JSON but
    not a JSON object (`null`, a bare number, an array, a bare string --
    `json.loads` accepts all of them), or is a JSON object with no `Arn`
    field, is its own distinct `ProbeResult` rather than an uncaught
    `json.JSONDecodeError`, `TypeError` or `KeyError` escaping this
    function: per the module docstring, every operational finding about the
    host is reported, never raised, the same treatment `_probe_one_tool`
    already gives an unparsable tool-version response (AC-FUNC-004).
    """
    check = f"aws: identity for profile '{profile}'"
    prevents = (
        "everything remote depends on it: the SSM tunnel, Parameter Store, and the "
        "build that publishes to it"
    )
    login_command = f"aws sso login --profile {profile}"
    identity_command = (
        "aws",
        "sts",
        "get-caller-identity",
        "--profile",
        profile,
        "--output",
        "json",
    )
    result = runner(identity_command, None)
    if result.binary_missing:
        return ProbeResult(
            check=check,
            found="aws is not on PATH",
            ok=False,
            prevents=prevents,
            remedy=f"Install the AWS CLI v2, then sign in: {login_command}",
        )
    if result.exit_code == 0:
        try:
            parsed: Any = json.loads(result.stdout)
        except json.JSONDecodeError:
            return ProbeResult(
                check=check,
                found=(
                    "aws sts get-caller-identity produced unparsable output: "
                    f"{result.stdout.strip()!r}"
                ),
                ok=False,
                prevents=prevents,
                remedy=(
                    f"Run {' '.join(identity_command)} manually and investigate why its "
                    "output is not valid JSON."
                ),
            )
        if not isinstance(parsed, dict):
            return ProbeResult(
                check=check,
                found=(
                    "aws sts get-caller-identity produced valid JSON that is not a JSON "
                    f"object: {result.stdout.strip()!r}"
                ),
                ok=False,
                prevents=prevents,
                remedy=(
                    f"Run {' '.join(identity_command)} manually and investigate why its "
                    "output is not a JSON object."
                ),
            )
        identity: dict[str, Any] = parsed
        if "Arn" not in identity:
            return ProbeResult(
                check=check,
                found=(
                    "aws sts get-caller-identity succeeded but its output has no 'Arn' "
                    f"field: {result.stdout.strip()!r}"
                ),
                ok=False,
                prevents=prevents,
                remedy=(
                    f"Run {' '.join(identity_command)} manually and investigate why its "
                    "output has no Arn field."
                ),
            )
        return ProbeResult(check=check, found=str(identity["Arn"]), ok=True, prevents=prevents)

    stderr = result.stderr
    if any(marker in stderr for marker in _AWS_EXPIRED_SESSION_MARKERS):
        return ProbeResult(
            check=check,
            found=f"the SSO session for profile '{profile}' has expired",
            ok=False,
            prevents=prevents,
            remedy=login_command,
        )
    if any(marker in stderr for marker in _AWS_NO_CREDENTIALS_MARKERS):
        return ProbeResult(
            check=check,
            found=f"profile '{profile}' has no credentials at all",
            ok=False,
            prevents=prevents,
            remedy=login_command,
        )
    return ProbeResult(
        check=check,
        found=f"aws sts get-caller-identity failed: {stderr.strip()}",
        ok=False,
        prevents=prevents,
        remedy=login_command,
    )
