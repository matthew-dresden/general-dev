"""Contract tests for the `make test` target and its wiring into `make validate`.

Three regressions here are silent failures if left unasserted: `test` falling
out of `.PHONY`, which turns it into a no-op the moment a path named exactly
`test` (a file or a directory) exists in the repository root; `validate`
losing `lint`, `test`, or both, which narrows what `make validate` verifies
without any caller noticing; and the `test` recipe reaching for something off
this machine (docker, aws, ssh, curl, an HTTP endpoint), which turns a
hermetic suite into an environment-dependent one (AC-10.14).

E3-F2-S2-T5 adds a second group of assertions: `zsh` became a real host
prerequisite of `make test` once E3-F2-S2-T1's shell-startup tests started
executing a real zsh interpreter, but the `help` recipe's PREREQUISITES block
still promised `uv` alone. `test_prerequisites_test_row_names_the_tool` and
`test_test_recipe_guards_every_prerequisite_tool_before_pytest` pin the
documentation row and the fail-fast guard together so neither can drift from
the other; `test_test_recipe_guard_fails_fast_when_a_tool_is_absent` proves
the guard by actually removing a tool from `PATH` and running `make test`.
The Makefile itself single-sources each tool's install command as a
`TEST_INSTALL_HINT_<tool>` variable, read by both the PREREQUISITES row and
the `test:` recipe's guard; `_resolve_make_refs` resolves a `$(NAME)` token
captured out of the Makefile text back to that variable's own `NAME := value`
line, so this suite still asserts on the real install-command text rather
than the literal token, and a row/guard that came to disagree on a tool's
install command would still be caught. A fourth guard -- cross-checking the
PREREQUISITES row's Linux install command against the package
`.github/workflows/ci.yml` installs -- is out of scope here: E3-F2-S2-T1 owns
that CI step and, per AC-TEST-003 of this unit's own spec, that criterion is
MOVED TO E3-F2-S2-T1 AC-TEST-006, which runs after this unit and can read
both halves of the cross-check.

The Makefile is read through `devcontainer_config.repo.find_root`, resolved
from this test file's own location, so the assertions hold from any working
directory a test runner is invoked from rather than assuming the repository
root is the current directory.

`_makefile_text` and `_resolve_make_refs` are imported from
`tests/conftest.py` rather than defined here (`_make_variable`, the helper
`_resolve_make_refs` calls internally, lives in `tests/conftest.py` too but
is not imported directly by any test in this file): `tests/test_ci_workflow.py`
needs the identical Makefile-variable-resolution logic for AC-TEST-006 (the
cross-check that the CI `Install zsh` step and this file's PREREQUISITES row
name the same package), and a private copy in each file risked one drifting
from the other while its sibling suite stayed green.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import _makefile_text, _removed_identifiers, _resolve_make_refs
from devcontainer_config import helpline, instances
from devcontainer_config.repo import find_root

# Spec Section 4.1.2 requires the `make test` row to stay "no docker, no AWS,
# no network"; this task's Approach step 2 derives the concrete tokens that
# would signal a breach of that "no network" clause: a container engine, a
# cloud CLI, a remote shell, or an HTTP call reaching off this machine.
OFF_MACHINE_TOKENS: tuple[str, ...] = ("docker", "aws", "ssh", "curl", "http")

# E3-F2-S2-T5 AC-DOC-001 / AC-TEST-001: the host prerequisites of `make test`,
# per the `test` PREREQUISITES row. Defined once so the two parametrized
# suites that exercise each tool (row membership, guard fail-fast behavior)
# cannot list a different tool set from one another.
TEST_PREREQUISITE_TOOLS: tuple[str, ...] = ("uv", "zsh")

# The `test:` recipe's opening banner line (`@printf ... running pytest
# suite`) has no shell operators, so this host's `make` runs it by directly
# exec-ing `printf` rather than handing it to `$(SHELL)` first (an
# optimization some `make` implementations apply to operator-free recipe
# lines). `_minimal_path_missing_tool` must therefore keep `printf` resolvable
# in its doctored `PATH` even though `printf` is never one of the tools under
# test, or every doctored run fails before the guard loop it exists to
# exercise ever runs.
_UNGUARDED_RECIPE_UTILITIES: tuple[str, ...] = ("printf",)


def _phony_targets(makefile_text: str) -> set[str]:
    """Every target named in the (possibly backslash-continued) `.PHONY` list.

    `.PHONY:` in this Makefile spans several lines joined with a trailing
    `\\`; a line-by-line split would only see the first line's targets. The
    whole block, up to the next line that starts a new statement, is
    collapsed into one string first so `.split()` sees every target once.
    """
    match = re.search(r"^\.PHONY:(.*?)(?=^\S|\Z)", makefile_text, re.MULTILINE | re.DOTALL)
    assert match is not None, "no .PHONY declaration found in Makefile"
    return set(match.group(1).replace("\\\n", " ").split())


def _validate_prerequisites(makefile_text: str) -> set[str]:
    """The prerequisite set on the `validate:` target line."""
    match = re.search(r"^validate:(.*)$", makefile_text, re.MULTILINE)
    assert match is not None, "no validate target found in Makefile"
    return set(match.group(1).split())


def _test_recipe_body(makefile_text: str) -> str:
    """The recipe lines (tab-indented) that follow the `test:` target header."""
    match = re.search(r"^test:.*\n((?:\t.*\n?)*)", makefile_text, re.MULTILINE)
    assert match is not None, "no test target found in Makefile"
    return match.group(1)


def _lint_secrets_recipe_body(makefile_text: str) -> str:
    """The recipe lines (tab-indented) that follow the `lint-secrets:` target header."""
    match = re.search(r"^lint-secrets:.*\n((?:\t.*\n?)*)", makefile_text, re.MULTILINE)
    assert match is not None, "no lint-secrets target found in Makefile"
    return match.group(1)


def _help_recipe_body(makefile_text: str) -> str:
    """The recipe lines (tab-indented) that follow the `help:` target header."""
    match = re.search(r"^help:.*\n((?:\t.*\n?)*)", makefile_text, re.MULTILINE)
    assert match is not None, "no help target found in Makefile"
    return match.group(1)


def _cert_status_recipe_body(makefile_text: str) -> str:
    """The recipe lines (tab-indented) that follow the `cert-status:` target header.

    E6-F1-S1-T2's own addition to this Makefile.
    """
    match = re.search(r"^cert-status:.*\n((?:\t.*\n?)*)", makefile_text, re.MULTILINE)
    assert match is not None, "no cert-status target found in Makefile"
    return match.group(1)


def _connect_recipe_body(makefile_text: str) -> str:
    """The recipe lines (tab-indented) that follow the `connect:` target header.

    E6-F2-S1-T4's own addition: the `connect` recipe now dispatches on
    `DEVCONTAINER_TRANSPORT` instead of running `$(TUNNEL_SH)` unconditionally.
    """
    match = re.search(r"^connect:.*\n((?:\t.*\n?)*)", makefile_text, re.MULTILINE)
    assert match is not None, "no connect target found in Makefile"
    return match.group(1)


def _shell_env_example_text() -> str:
    """The repository root `shell.env.example`, read fresh for every call.

    Not cached at module scope, for the same reason `_makefile_text`
    (tests/conftest.py) is not: a cached value would let one test's assertion
    about the file's content leak into another's failure message instead of
    each test reading the file it is actually asserting about.
    """
    root = find_root(Path(__file__).resolve().parent)
    return (root / "shell.env.example").read_text(encoding="utf-8")


def _remote_docker_engine_block(shell_env_example_text: str) -> str:
    """The `Remote docker engine` commented block of `shell.env.example`.

    Bounded by its own banner heading and the next `#####...` banner (or end
    of file, since this is currently the file's last section), so a line
    added inside the block is picked up without the match widening into an
    unrelated section.
    """
    match = re.search(
        r"^#{10,}\n# Remote docker engine.*?\n#{10,}\n(.*?)(?=^#{10,}\n|\Z)",
        shell_env_example_text,
        re.MULTILINE | re.DOTALL,
    )
    assert match is not None, "no 'Remote docker engine' block found in shell.env.example"
    return match.group(1)


def _prerequisites_row(makefile_text: str, label: str) -> str:
    """The second column of the PREREQUISITES row whose first column is `label`.

    Matched against the literal `@printf '  %-23s %s\\n' "<label>" "<row>"`
    line the `help` recipe renders that row from, not against `make help`'s
    rendered output, so this holds regardless of terminal width or color
    support on the machine running the test. The returned text may still
    carry `$(NAME)` Make-variable references; pass it through
    `_resolve_make_refs` before asserting on its rendered content.
    """
    help_body = _help_recipe_body(makefile_text)
    pattern = re.compile(
        r"^\t@printf '  %-23s %s\\n' \"" + re.escape(label) + r"\"\s+\"([^\"]*)\"$",
        re.MULTILINE,
    )
    match = pattern.search(help_body)
    assert match is not None, f"no PREREQUISITES row found for {label!r} in the help recipe"
    return match.group(1)


def _row_tool_names(row: str) -> list[str]:
    """The leading comma-separated tool list a PREREQUISITES row's second column opens with.

    E.g. `"uv, zsh   uv: brew install uv   zsh: ..."` -> `["uv", "zsh"]`. This
    is the one place a PREREQUISITES row's tool list is parsed, so a test that
    derives its expected tools from a row (rather than repeating them as a
    separate literal) cannot silently drift from what the row actually names.
    """
    match = re.match(r"^([A-Za-z0-9_]+(?:,\s*[A-Za-z0-9_]+)*)\s{2,}", row)
    assert match is not None, f"PREREQUISITES row {row!r} has no leading comma-separated tool list"
    return [name.strip() for name in match.group(1).split(",")]


def _install_hint(makefile_text: str, tool: str) -> str:
    """The real `Install it: ...` text the `test:` recipe's guard prints for `tool`.

    Read out of the recipe body's `case` dispatch (not repeated as a literal
    here), then resolved through `_resolve_make_refs` against the same
    `TEST_INSTALL_HINT_<tool>` variable the PREREQUISITES row reads, so a test
    asserting on this string can never fall out of sync with what the guard
    actually prints.
    """
    recipe = _test_recipe_body(makefile_text)
    pattern = re.compile(re.escape(tool) + r"\)\s+hint=\"(.+?)\"\s*;;")
    match = pattern.search(recipe)
    assert match is not None, f"no install hint found for {tool!r} in the test recipe's guard"
    return _resolve_make_refs(makefile_text, match.group(1))


def _guard_loop_tools(makefile_text: str, recipe: str) -> list[str]:
    """The tool list the `test:` recipe's `for tool in ...; do` guard loop iterates.

    Parsed from the recipe body itself and resolved through
    `_resolve_make_refs` against `TEST_PREREQUISITE_TOOLS`, so a test
    asserting on it can never drift from what the guard loop actually
    iterates.
    """
    match = re.search(r"for tool in (\S+); do", recipe)
    assert match is not None, "no `for tool in ...; do` guard loop found in the test recipe"
    return _resolve_make_refs(makefile_text, match.group(1)).split()


def _minimal_path_missing_tool(tool: str, tmp_path: Path) -> str:
    """A `PATH` naming exactly the tools the `test:` recipe needs, minus `tool`.

    Built as one temp directory of symlinks (to every `TEST_PREREQUISITE_TOOLS`
    entry other than `tool`, plus `_UNGUARDED_RECIPE_UTILITIES`) rather than
    by removing a directory from the real `PATH`. Subtracting a directory is
    host-layout dependent: on a host where `zsh` and `make` share `/usr/bin`,
    removing that one directory also removes `make`; on a host with `/usr/bin`
    and `/bin` both listing the same binaries (usr-merge), the tool stays
    resolvable through the surviving duplicate entry. A from-scratch minimal
    directory has neither failure mode, because only the tools this helper
    explicitly symlinks are ever resolvable in it.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for other in (*TEST_PREREQUISITE_TOOLS, *_UNGUARDED_RECIPE_UTILITIES):
        if other == tool:
            continue
        other_path = shutil.which(other)
        assert other_path is not None, (
            f"{other} must be installed on the machine running this test suite"
        )
        (bin_dir / other).symlink_to(other_path)
    doctored_path = str(bin_dir)
    assert shutil.which(tool, path=doctored_path) is None, (
        f"{tool!r} is unexpectedly resolvable inside a minimal PATH built without it"
    )
    return doctored_path


def _run_make(
    target: str, *, env: dict[str, str], timeout_env_var: str
) -> subprocess.CompletedProcess[str]:
    """Shells out to `make <target>` from the repository root with `env`,
    bounded by a configurable timeout read from `timeout_env_var`
    (CLAUDE.md: no hardcoded timeouts).

    Shared by every test in this file that proves a recipe's fail-fast
    behavior by actually running it end to end, rather than only
    inspecting its source text: `test_test_recipe_guard_fails_fast_when_a_tool_is_absent`
    and `test_connect_recipe_rejects_an_unrecognized_transport` each
    carried their own copy of this `find_root` + `shutil.which("make")` +
    timeout-resolution + `subprocess.run` scaffold before this extraction
    (test_review DRY finding, E6-F2-S1-T4 round 2), following the same
    "single source of truth" discipline `_makefile_text` and
    `_resolve_make_refs` already apply to Makefile-variable resolution.
    """
    root = find_root(Path(__file__).resolve().parent)
    make_path = shutil.which("make")
    assert make_path is not None, "make must be installed on the machine running this test suite"
    timeout_seconds = float(os.environ.get(timeout_env_var, "30"))
    return subprocess.run(
        [make_path, target],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout_seconds,
    )


def test_test_target_is_phony() -> None:
    """AC-FUNC-003 / AC-TEST-001: `test` is declared `.PHONY`."""
    assert "test" in _phony_targets(_makefile_text())


def test_validate_requires_lint_and_test() -> None:
    """AC-FUNC-004 / AC-TEST-002: `validate`'s prerequisites are exactly these two.

    Asserted as set equality, not membership, so both a removal and an
    unexpected addition fail this test.
    """
    assert _validate_prerequisites(_makefile_text()) == {"lint", "test"}


@pytest.mark.parametrize("token", OFF_MACHINE_TOKENS)
def test_test_recipe_has_no_off_machine_token(token: str) -> None:
    """AC-FUNC-005 / AC-TEST-003: the `test` recipe reaches nothing off this machine."""
    recipe = _test_recipe_body(_makefile_text())
    assert token not in recipe.lower()


def test_lint_secrets_recipe_forwards_range_variable_to_the_cli() -> None:
    """AC-FUNC-006: `make lint-secrets RANGE=<a>..<b>` forwards RANGE to `--range`."""
    recipe = _lint_secrets_recipe_body(_makefile_text())
    assert "RANGE" in recipe
    assert "--range" in recipe


def test_help_documents_the_range_form_of_lint_secrets() -> None:
    """AC-DOC-001: `make help` describes the range form of `make lint-secrets`."""
    help_recipe = _help_recipe_body(_makefile_text())
    lint_secrets_rows = [line for line in help_recipe.splitlines() if '"make lint-secrets"' in line]
    assert len(lint_secrets_rows) == 1
    assert "RANGE" in lint_secrets_rows[0]


def test_help_secrets_section_uses_the_certificates_heading() -> None:
    """E4-F4-S1-T1 AC-FUNC-001: the secrets section heading matches spec Section 14.1.

    Renamed from "SECRETS AND CREDENTIALS" to "SECRETS AND CERTIFICATES" so
    the heading itself already reads the way spec Section 14.1 has it,
    ahead of `make cert-status` (E6-F1-S1-T2) landing under the same
    heading.
    """
    help_recipe = _help_recipe_body(_makefile_text())
    assert "SECRETS AND CERTIFICATES" in help_recipe
    assert "SECRETS AND CREDENTIALS" not in help_recipe


def test_help_quality_section_carries_test_and_lint_secrets_rows() -> None:
    """E4-F4-S1-T1 AC-FUNC-001: QUALITY carries `make test` and `make lint-secrets` rows."""
    help_recipe = _help_recipe_body(_makefile_text())
    assert '"make test"' in help_recipe
    assert '"make lint-secrets"' in help_recipe


@pytest.mark.parametrize("tool", TEST_PREREQUISITE_TOOLS)
def test_prerequisites_test_row_names_the_tool(tool: str) -> None:
    """E3-F2-S2-T5 AC-DOC-001 / AC-TEST-001: the `test` PREREQUISITES row names `uv` and `zsh`."""
    row = _prerequisites_row(_makefile_text(), "test")
    assert tool in _row_tool_names(row)


def test_prerequisites_test_row_gives_macos_and_linux_zsh_install_commands() -> None:
    """E3-F2-S2-T5 AC-DOC-001: the `test` row gives a Homebrew and an apt-get command for zsh."""
    makefile_text = _makefile_text()
    row = _resolve_make_refs(makefile_text, _prerequisites_row(makefile_text, "test"))
    assert "brew install zsh" in row
    assert "apt-get install -y zsh" in row


def test_test_recipe_guards_every_prerequisite_tool_before_pytest() -> None:
    """E3-F2-S2-T5 AC-FUNC-001/AC-TEST-002: one guard loop covers every tool, before `$(PYTEST)`."""
    text = _makefile_text()
    tools = _row_tool_names(_prerequisites_row(text, "test"))
    assert tools, "the test PREREQUISITES row named no tools to guard"
    recipe = _test_recipe_body(text)
    pytest_index = recipe.index("$(PYTEST)")
    guard_index = recipe.find('command -v "$$tool"')
    assert guard_index != -1, 'no single `command -v "$$tool"` guard loop found in the test recipe'
    assert guard_index < pytest_index, "the guard loop must precede the $(PYTEST) invocation"
    loop_tools = _guard_loop_tools(text, recipe)
    assert loop_tools == tools, (
        f"the guard loop must iterate exactly the tools named in the PREREQUISITES row, "
        f"in the same order: loop={loop_tools!r} row={tools!r}"
    )


@pytest.mark.parametrize("tool", TEST_PREREQUISITE_TOOLS)
def test_test_recipe_guard_fails_fast_when_a_tool_is_absent(tool: str, tmp_path: Path) -> None:
    """E3-F2-S2-T5 AC-FUNC-001/002 / AC-TEST-004: the guard names the missing tool and its fix."""
    makefile_text = _makefile_text()
    hint = _install_hint(makefile_text, tool)
    env = dict(os.environ)
    env["PATH"] = _minimal_path_missing_tool(tool, tmp_path)
    # A bounded safety net, not a readiness wait: this recipe is asserted to fail
    # inside its guard loop, before `$(PYTEST)` ever runs, so it normally returns
    # in well under a second. If the guard regressed and let `$(PYTEST) tests` run
    # for real, that invocation collects this very test file again and recurses
    # without limit; the timeout turns that into a fast, clear failure instead of
    # a runaway process tree. Configurable so a slower CI runner is not penalized
    # by a bound tuned for a developer's machine (CLAUDE.md: no hardcoded
    # timeouts), following the pattern in tests/test_hostprobe.py.
    result = _run_make("test", env=env, timeout_env_var="MAKEFILE_GUARD_TEST_TIMEOUT_SECONDS")
    combined = result.stdout + result.stderr
    assert result.returncode != 0, f"make test must fail fast when {tool!r} is absent from PATH"
    assert tool in combined, f"the failure output must name the missing tool {tool!r}"
    assert hint in combined, f"the failure output must carry the install command for {tool!r}"
    assert "passed" not in combined and "failed" not in combined, (
        "pytest must never run (partially or fully) when a prerequisite tool is missing"
    )


# ---------------------------------------------------------------------------
# E6-F1-S1-T2: the `cert-status` target this task adds, its `.PHONY` entry,
# and its `make help` row. The help text reports the two roles `make
# cert-status` actually renders -- `client` and `ca` -- rather than spec
# Section 14.1's three-role wording verbatim: the server certificate is
# never persisted under `~/.docker/certs/<instance>/` (certs.py's own module
# docstring, `docs/environment-files.md`'s "Certificate expiry warning"
# section), so a help row promising server-expiry monitoring the command
# cannot perform would mislead the operator (code_review, E6-F1-S1-T2,
# BLOCKING 1/2). AC-TEST-004 is satisfied against the reconciled text so
# the target and the help surface cannot drift from each other.
# ---------------------------------------------------------------------------


def test_cert_status_target_is_phony() -> None:
    assert "cert-status" in _phony_targets(_makefile_text())


def test_cert_status_recipe_invokes_the_certs_status_module() -> None:
    """The target shells out to `devcontainer_config.certs status` -- this task's own
    Changes Manifest addition -- never a second, ad hoc report implementation
    duplicated into the Makefile."""
    recipe = _cert_status_recipe_body(_makefile_text())
    assert "devcontainer_config.certs status" in recipe
    assert "PYTHONPATH=$(DEVCONTAINER_SCRIPTS_DIR)" in recipe


def test_cert_status_help_row_matches_the_two_roles_the_command_reports() -> None:
    help_recipe = _help_recipe_body(_makefile_text())
    cert_status_rows = [line for line in help_recipe.splitlines() if '"make cert-status"' in line]
    assert len(cert_status_rows) == 1
    assert '"host"' in cert_status_rows[0]
    assert "Client and CA expiry per instance." in cert_status_rows[0]


# ---------------------------------------------------------------------------
# E6-F2-S1-T4: the `connect` recipe dispatches on `DEVCONTAINER_TRANSPORT`
# instead of running `$(TUNNEL_SH)` unconditionally, so the SSM port-forward
# manager E6-F2-S1-T1 delivered (and the docker context E6-F2-S1-T2 delivered)
# become reachable from the build path at all. `transport.py`'s own `connect`
# subparser is E6-F2-S1-T3's, adapted after this recipe's interface lands
# (Approach note), so these assertions are against the Makefile's dispatch
# wiring only -- the source-side arguments and fail-fast behavior it is
# already responsible for -- never against invoking the ssm branch for real.
# ---------------------------------------------------------------------------

# `make connect` shells out with a bogus DEVCONTAINER_TRANSPORT value in a
# real subprocess; bounded so a regression that makes the recipe hang (rather
# than exit fast in its `*` branch) fails this test instead of the run.
# Configurable per CLAUDE.md's no-hardcoded-timeouts rule; see
# MAKEFILE_GUARD_TEST_TIMEOUT_SECONDS above for the identical rationale.
_CONNECT_REJECT_TEST_TIMEOUT_SECONDS_ENV_VAR = "MAKEFILE_CONNECT_TEST_TIMEOUT_SECONDS"


def test_connect_recipe_dispatches_on_the_transport_selector() -> None:
    """AC-FUNC-001 / AC-TEST-001: the recipe reads DEVCONTAINER_TRANSPORT and
    names both branches through their existing Make variables, never a
    duplicated literal path or module name.
    """
    text = _makefile_text()
    recipe = _connect_recipe_body(text)
    assert "DEVCONTAINER_TRANSPORT" in recipe
    assert "$(TUNNEL_SH)" not in recipe, (
        "the ssh branch was removed at cutover (E7-F1-S1-T1); $(TUNNEL_SH) no longer exists"
    )
    for identifier in _removed_identifiers():
        assert identifier not in text, (
            f"the Makefile must not reference {identifier!r}, deleted at cutover"
        )
    assert "devcontainer_config.transport connect" in recipe
    assert "general-dev" not in recipe, (
        "the recipe must never carry a literal 'general-dev' substring "
        "(AC-FUNC-003: no prefix arithmetic on REMOTE_DOCKER_CONTEXT)"
    )


def _remote_docker_context_prefix_owner() -> str:
    """Dotted name of the function that owns REMOTE_DOCKER_CONTEXT
    prefix-stripping logic, derived from the live object (not restated as a
    literal) so a future rename of `docker_context_prefix` breaks this
    helper's callers instead of leaving them pointed at a stale name.
    """
    return f"{instances.__name__}.{instances.docker_context_prefix.__qualname__}"


def _remote_docker_context_prefix_diagnostic() -> str:
    """The exact message `test_connect_recipe_passes_the_resolved_context_variable`
    asserts on REMOTE_DOCKER_CONTEXT# regression, naming the current owner of
    the prefix-stripping logic. Single-sourced here so the assertion that uses
    it and the guard that checks its wording can never drift apart.
    """
    return (
        "the recipe must not strip a prefix off REMOTE_DOCKER_CONTEXT to "
        f"derive the context name; {_remote_docker_context_prefix_owner()} already owns that logic"
    )


def test_connect_recipe_passes_the_resolved_context_variable() -> None:
    """AC-FUNC-003: the ssm branch passes `--context "$(REMOTE_CONTEXT)"`,
    reusing the existing Makefile variable, rather than reconstructing the
    instance name by stripping a literal prefix off REMOTE_DOCKER_CONTEXT
    (the standards violation the rejected E6-F2-S1-T3 amendment carried).
    """
    recipe = _connect_recipe_body(_makefile_text())
    assert '--context "$(REMOTE_CONTEXT)"' in recipe
    assert "REMOTE_DOCKER_CONTEXT#" not in recipe, _remote_docker_context_prefix_diagnostic()


def test_context_prefix_assertion_message_names_the_current_prefix_owner() -> None:
    """AC-FUNC-001/002: the constant `transport.py` used to derive the docker
    context prefix from was deleted in favor of
    `devcontainer_config.instances.docker_context_prefix`. Two independent
    checks: (1) this file's own source text must no longer reference the
    deleted constant anywhere (a genuine whole-file search, since the deleted
    symbol has no legitimate reason to appear here at all); and (2) the
    diagnostic message `test_connect_recipe_passes_the_resolved_context_variable`
    actually raises on a REMOTE_DOCKER_CONTEXT# regression -- obtained by
    calling `_remote_docker_context_prefix_diagnostic()`, the single function
    that builds both that assertion's message and this guard's expectation --
    must name the current owner. Scoping the second check to that message
    (rather than this whole file's source, which also contains this
    docstring's own prose) means a diagnostic reworded to drop the owner name
    fails this guard instead of being satisfied by unrelated text elsewhere in
    the file. The deleted constant's name is assembled from two halves below
    so this guard's own source does not trip its own search term.
    """
    source = Path(__file__).read_text(encoding="utf-8")
    deleted_symbol = "CONTEXT_NAME" + "_PREFIX"
    assert deleted_symbol not in source, (
        f"this file must not reference the deleted transport.{deleted_symbol} symbol"
    )
    owner = _remote_docker_context_prefix_owner()
    diagnostic = _remote_docker_context_prefix_diagnostic()
    assert owner in diagnostic, (
        f"the REMOTE_DOCKER_CONTEXT# diagnostic message must name {owner!r} "
        "as the current owner of the prefix-stripping logic"
    )


def test_connect_recipe_defaults_to_the_ssm_transport_when_unset() -> None:
    """An unset DEVCONTAINER_TRANSPORT selects ssm, the only transport left.

    Before the cutover this asserted the opposite: an unset selector ran
    `$(TUNNEL_SH)` bare so `make remote` and `make up` saw no behavior change
    while both transports existed. The cutover deleted the SSH tunnel script, so the
    default has to move rather than merely lose a branch -- otherwise an
    operator who sets nothing gets a recipe that dispatches to a value its own
    case statement rejects. Asserted against the recipe source, never by
    invoking the ssm branch, which needs real AWS state.
    """
    recipe = _connect_recipe_body(_makefile_text())
    assert "DEVCONTAINER_TRANSPORT:-ssm" in recipe, (
        "an unset DEVCONTAINER_TRANSPORT must default to ssm now that ssh is gone"
    )
    assert "ssh)" not in recipe, "the ssh branch must be gone from the case statement"


def test_connect_recipe_rejects_an_unrecognized_transport() -> None:
    """AC-FUNC-002 / AC-TEST-003: an unrecognized DEVCONTAINER_TRANSPORT value
    makes `make connect` exit non-zero before any transport starts, printing
    an ERROR line naming the variable, the offending value and the accepted
    values -- proved by actually running the dispatch, not only by inspecting
    the recipe's source text.
    """
    env = dict(os.environ)
    env["DEVCONTAINER_TRANSPORT"] = "bogus-transport"
    result = _run_make(
        "connect",
        env=env,
        timeout_env_var=_CONNECT_REJECT_TEST_TIMEOUT_SECONDS_ENV_VAR,
    )
    assert result.returncode != 0, "make connect must fail fast on an unrecognized transport"
    assert "ERROR" in result.stderr
    assert "DEVCONTAINER_TRANSPORT" in result.stderr
    assert "bogus-transport" in result.stderr
    assert "ssh" in result.stderr
    assert "ssm" in result.stderr


def test_shell_env_example_documents_the_transport_selector() -> None:
    """AC-DOC-001 / AC-TEST-002: `shell.env.example`'s `Remote docker engine`
    block names DEVCONTAINER_TRANSPORT, states the ssh default and names both
    accepted values. Any `docs/*.md` path the new commented lines cite must
    itself exist, already contain the string DEVCONTAINER_TRANSPORT -- the
    guard the rejected E6-F2-S1-T3 amendment's dangling
    `docs/environment-files.md` reference did not have -- and must not
    claim the variable "has no effect" or "is not read", the round-2
    doc_review REVIEW_FAIL this file's original version of this guard let
    through: it only checked the variable's name appeared, so it passed
    against a cited section whose prose was stale the moment this task's
    own Makefile change landed a real reader for the variable.
    """
    root = find_root(Path(__file__).resolve().parent)
    shell_env_example_text = _shell_env_example_text()
    block = _remote_docker_engine_block(shell_env_example_text)
    assert "DEVCONTAINER_TRANSPORT" in block
    assert "ssh" in block.lower() and "default" in block.lower()
    assert "ssm" in block.lower()

    cited_doc_paths = set(re.findall(r"\bdocs/[\w./-]+\.md\b", block))
    for doc_path in cited_doc_paths:
        doc_file = root / doc_path
        assert doc_file.is_file(), f"{doc_path!r} is cited but does not exist on disk"
        cited_text = doc_file.read_text(encoding="utf-8")
        assert "DEVCONTAINER_TRANSPORT" in cited_text, (
            f"{doc_path!r} is cited as documenting DEVCONTAINER_TRANSPORT but "
            "does not itself mention the variable"
        )
        lowered_cited_text = cited_text.lower()
        assert "no effect at all" not in lowered_cited_text, (
            f"{doc_path!r} is cited as the DEVCONTAINER_TRANSPORT reference but claims "
            "the variable has 'no effect at all', which this Makefile's connect recipe "
            "contradicts by reading and dispatching on the variable today"
        )
        assert "not read by any code" not in lowered_cited_text, (
            f"{doc_path!r} is cited as the DEVCONTAINER_TRANSPORT reference but claims "
            "the variable is 'not read by any code', which this Makefile's connect "
            "recipe contradicts by reading it today"
        )


# ---------------------------------------------------------------------------
# The instance surface (U2): list-instances and the instance-* targets, the
# complete replacement of `instances` and `record-instance`. The engine is
# `devcontainer_config.instance_ops` behind `devcontainer_config.cli`; the
# contract pinned here is the Makefile half of that split -- every target is
# defined, .PHONY'd and advertised; every INSTANCE-consuming recipe guards an
# empty INSTANCE with usage and exit 2; the ALL=1 targets loop over the
# engine's own discovery instead of a second shell reimplementation of it;
# deploy refuses an accidental replacement unless CONFIRM=replace and
# converges the trust chain from the status JSON's params_present/
# certs_present fields; destroy demands CONFIRM=destroy only when ALL=1 and
# runs the cli's instance-cleanup after Terragrunt's.
# ---------------------------------------------------------------------------


# Every target the U2 surface adds. Defined once so the phony/defined/help-row
# suites cannot list a different set from one another.
INSTANCE_TARGETS: tuple[str, ...] = (
    "list-instances",
    "instance-init",
    "instance-plan",
    "instance-deploy",
    "instance-status",
    "instance-stop",
    "instance-start",
    "instance-destroy",
    "instance-link",
)

# The members of INSTANCE_TARGETS that accept ALL=1 in place of INSTANCE.
INSTANCE_ALL_TARGETS: tuple[str, ...] = (
    "instance-plan",
    "instance-deploy",
    "instance-status",
    "instance-stop",
    "instance-start",
    "instance-destroy",
)

# The cli subcommand each recipe delegates to, pinned together with the
# target so the Makefile stays the thin loop the module docstring of
# `devcontainer_config.instance_ops` describes -- the aws/docker logic lives
# in the engine, never re-implemented inline.
_INSTANCE_TARGET_SUBCOMMANDS: tuple[tuple[str, str], ...] = (
    ("list-instances", "instance-list"),
    ("instance-init", "instance-init"),
    ("instance-plan", None),  # Terragrunt-only: init + plan, no cli call
    ("instance-deploy", "instance-link"),
    ("instance-status", "instance-status"),
    ("instance-stop", "instance-stop"),
    ("instance-start", "instance-start"),
    ("instance-destroy", "instance-cleanup"),
    ("instance-link", "instance-link"),
)

# instance-init and instance-link act on exactly one instance by definition:
# "every configured instance" is meaningless for a scaffold or an id record,
# so they take the strict single-instance guard instead of the ALL variant.


def _target_recipe_body(makefile_text: str, target: str) -> str:
    """The recipe lines (tab-indented) that follow `target`'s definition line.

    One generic extractor for the instance surface, the same shape the
    per-target helpers above (`_test_recipe_body`, `_connect_recipe_body`,
    ...) establish; those keep their unit-specific docstrings, this one
    serves the parametrized suites below that read nine targets.
    """
    match = re.search(rf"^{re.escape(target)}:[^\n]*\n((?:\t.*\n?)*)", makefile_text, re.MULTILINE)
    assert match is not None, f"no {target} target found in Makefile"
    return match.group(1)


def _make_define_body(makefile_text: str, name: str) -> str:
    """The body between `define <name>` and its `endef`, newlines included."""
    match = re.search(
        rf"^define {re.escape(name)}\n(.*?)^endef\s*$", makefile_text, re.MULTILINE | re.DOTALL
    )
    assert match is not None, f"no define {name} block found in Makefile"
    return match.group(1)


def _expand_guard_calls(makefile_text: str, recipe: str) -> str:
    """`recipe` with every `$(call GUARD,...)` replaced by the guard's body.

    The usage guards live in `define` blocks shared by every instance target
    (nine near-identical inline copies would be exactly the duplication
    CLAUDE.md forbids), so a test that wants to assert "this recipe checks an
    empty INSTANCE" must resolve the call back to the body the shell actually
    runs. Only `$(1)` is substituted: the guards reference no other argument.
    """

    def replace(match: re.Match[str]) -> str:
        body = _make_define_body(makefile_text, match.group(1))
        return body.replace("$(1)", match.group(2))

    return re.sub(r"\$\(call (\w+),([^)]*)\)", replace, recipe)


def _expanded_instance_recipe(makefile_text: str, target: str) -> str:
    """`_target_recipe_body` with guard calls expanded -- what the shell runs."""
    return _expand_guard_calls(makefile_text, _target_recipe_body(makefile_text, target))


@pytest.mark.parametrize("target", INSTANCE_TARGETS)
def test_instance_target_is_phony(target: str) -> None:
    assert target in _phony_targets(_makefile_text())


@pytest.mark.parametrize("target", INSTANCE_TARGETS)
def test_instance_target_is_defined(target: str) -> None:
    assert re.search(rf"^{re.escape(target)}:", _makefile_text(), re.MULTILINE) is not None, (
        f"{target} is .PHONY'd but defines no recipe"
    )


@pytest.mark.parametrize("target", INSTANCE_TARGETS)
def test_instance_target_has_a_help_row(target: str) -> None:
    help_recipe = _help_recipe_body(_makefile_text())
    rows = [line for line in help_recipe.splitlines() if f'"make {target}"' in line]
    assert len(rows) == 1, f"make help must advertise {target} exactly once"


@pytest.mark.parametrize(
    "target",
    [t for t, subcommand in _INSTANCE_TARGET_SUBCOMMANDS if subcommand is not None],
)
def test_instance_recipe_delegates_to_the_cli_subcommand(target: str) -> None:
    """Each recipe shells the cli subcommand of the same concern, never re-implements it."""
    makefile_text = _makefile_text()
    recipe = _target_recipe_body(makefile_text, target)
    subcommand = dict(_INSTANCE_TARGET_SUBCOMMANDS)[target]
    assert f"devcontainer_config.cli {subcommand}" in recipe, (
        f"{target} must delegate to `devcontainer_config.cli {subcommand}` "
        "(PYTHONPATH=$(DEVCONTAINER_SCRIPTS_DIR)), not re-implement it"
    )
    assert "PYTHONPATH=$(DEVCONTAINER_SCRIPTS_DIR)" in recipe


@pytest.mark.parametrize("target", [t for t in INSTANCE_TARGETS if t != "list-instances"])
def test_instance_recipe_guards_a_missing_instance_with_usage_and_exit_two(target: str) -> None:
    """Usage-on-missing: an INSTANCE-empty run prints usage lines and exits 2."""
    recipe = _expanded_instance_recipe(_makefile_text(), target)
    assert '[ -z "$(INSTANCE)" ]' in recipe, f"{target} never checks INSTANCE for emptiness"
    assert "exit 2" in recipe, f"{target} must exit 2 on a missing INSTANCE"
    assert "make list-instances" in recipe, "the usage lines must point at the listing"


@pytest.mark.parametrize("target", INSTANCE_ALL_TARGETS)
def test_instance_recipe_loops_over_discovery_when_all_is_set(target: str) -> None:
    """ALL=1 iterates the engine's own discovery, one name per loop iteration."""
    recipe = _target_recipe_body(_makefile_text(), target)
    assert "$(DISCOVER_INSTANCE_NAMES)" in recipe, (
        f"{target}'s ALL=1 branch must list instances via $(DISCOVER_INSTANCE_NAMES) "
        "(instances.discover), not a second shell reimplementation of the rule"
    )
    assert "while IFS= read -r name" in recipe, f"{target} must loop one name per iteration"


def test_removed_targets_are_gone_everywhere() -> None:
    """`instances` and `record-instance` have no target, phony entry or help row left."""
    makefile_text = _makefile_text()
    phony = _phony_targets(makefile_text)
    for target in ("instances", "record-instance"):
        assert target not in phony
        assert re.search(rf"^{target}:", makefile_text, re.MULTILINE) is None
        assert f'"make {target}"' not in makefile_text
    assert "record-instance.py" not in makefile_text, (
        "the recipe that fed .devcontainer/record-instance.py is deleted, so the "
        "Makefile must not reference the deleted script"
    )


def test_record_instance_script_is_deleted() -> None:
    """The superseded script is gone from disk: the id store lives per instance now."""
    root = find_root(Path(__file__).resolve().parent)
    assert not (root / ".devcontainer" / "record-instance.py").exists(), (
        ".devcontainer/record-instance.py is fully replaced by "
        "devcontainer_config.instance_ops's per-instance id store; delete it"
    )


def test_destroy_requires_confirm_only_in_the_all_branch() -> None:
    """CONFIRM=destroy guards every instance at once, never a named one.

    Asserted against the recipe's textual split at its only `else`: the
    single-instance path (after the else) must not demand a confirmation,
    and the ALL path (before it) must.
    """
    makefile_text = _makefile_text()
    recipe = _target_recipe_body(makefile_text, "instance-destroy")
    assert "CONFIRM" in recipe, "instance-destroy carries no CONFIRM guard at all"
    head, separator, tail = recipe.partition("else \\")
    assert separator, "instance-destroy's ALL/single split is missing"
    assert 'if [ "$(CONFIRM)" != "destroy" ]' in head, (
        "the ALL=1 branch must refuse to run without CONFIRM=destroy"
    )
    assert "CONFIRM" not in tail, (
        "destroying one named instance must not require CONFIRM; only ALL=1 does"
    )


def test_deploy_refuses_replacement_without_confirm_replace() -> None:
    """The plan is read before apply: replacement or destroys refuse without CONFIRM=replace."""
    recipe = _target_recipe_body(_makefile_text(), "instance-deploy")
    assert "must be replaced" in recipe, "the guard must match Terraform's replacement wording"
    assert "to destroy" in recipe, "the guard must match the plan summary's destroy count"
    assert 'CONFIRM)" != "replace"' in recipe, "the refusal must be lifted by CONFIRM=replace"
    plan_index = recipe.index("plan_log=")
    apply_index = recipe.index("terragrunt apply")
    assert plan_index < apply_index, "the plan must be captured and guarded before any apply"


def test_deploy_converges_the_trust_chain_from_the_status_json_fields() -> None:
    """certs_present/params_present decide what converge runs; present params skip publish."""
    recipe = _target_recipe_body(_makefile_text(), "instance-deploy")
    assert "--json" in recipe, "converge must read instance-status --json"
    for field in ("params_present", "certs_present"):
        assert field in recipe, f"converge must branch on the status JSON's {field}"
    publish_index = recipe.index("cert-publish")
    branch_index = recipe.index('if [ "$$params_present" != "true" ]')
    assert branch_index < publish_index, "publish/install must sit inside the params-absent branch"


def test_destroy_runs_the_cli_cleanup_after_terragrunt_destroy() -> None:
    """The params, context, certs and id die only after Terragrunt's own teardown."""
    recipe = _target_recipe_body(_makefile_text(), "instance-destroy")
    destroy_index = recipe.index("terragrunt destroy -auto-approve")
    cleanup_index = recipe.index("instance-cleanup")
    assert destroy_index < cleanup_index, (
        "instance-cleanup must run after terragrunt destroy, not before or instead of it"
    )


def test_help_instances_section_carries_the_whole_surface_with_the_naming_rule() -> None:
    """The INSTANCES section is one section: instance rows, the cert chain, connect, remote.

    The moved rows must appear exactly once each -- moved, not copied -- and
    the section header must state the naming rule, that instance names are
    project names.
    """
    makefile_text = _makefile_text()
    help_recipe = _help_recipe_body(makefile_text)
    assert "INSTANCES" in help_recipe
    assert "instance names are project names" in help_recipe
    moved_rows = (
        "make cert-ca",
        "make cert-client",
        "make cert-publish",
        "make cert-install",
        "make cert-status",
        "make push-secrets",
        "make connect",
        "make remote",
    )
    for row in moved_rows:
        assert help_recipe.count(f'"{row}"') == 1, f"{row} must appear exactly once in help"


def test_help_instances_section_sits_between_engine_and_build() -> None:
    """Section order: ENGINE, then INSTANCES, then BUILD."""
    help_recipe = _help_recipe_body(_makefile_text())
    engine_index = help_recipe.index("ENGINE")
    instances_index = help_recipe.index('"INSTANCES"')
    build_index = help_recipe.index('"BUILD"')
    assert engine_index < instances_index < build_index


# The instruction-column note rule: every bold legend label and section
# header that carries a note renders through the one `note()` helper, whose
# printf pads the bold name to the instruction column so the note aligns
# with the descriptions below it.


def test_header_notes_render_through_one_helper_at_the_instruction_column() -> None:
    """The note printf appears once, inside `note()`, padded to column 34.

    One printf occurrence proves no header note bypasses the helper; the
    width pin ties the helper's pad to helpline's instruction column, so the
    notes can never drift from the description column the rows use.
    """
    recipe = _help_recipe_body(_makefile_text())
    note_format = "printf '\\n\\033[1m%-34s\\033[0m%s\\n'"
    assert recipe.count(note_format) == 1, (
        "the header-note printf must exist only inside the note() helper"
    )
    assert 'note "Engines"' in recipe
    assert 'note "INSTANCES"' in recipe
    width = re.search(re.escape("\\033[1m%-") + r"(\d+)" + re.escape("s\\033[0m%s"), recipe)
    assert width is not None, "note() carries no name pad to pin"
    assert int(width.group(1)) == helpline.INSTRUCTION_COLUMN


# The description-column cap (the wrapped help row): every two-column row
# renders through the one `row()` helper defined in the recipe -- rows with
# a description at or under the cap via its printf, longer rows via
# devcontainer_config.helpline -- and the helper's literal constants are
# pinned to the module's geometry so the two renderers cannot drift. The
# legend entries render through `row` too, with an empty target, so their
# names sit in the scope column and their descriptions in the description
# column. The literal fragments below are read out of the recipe text, never
# duplicated as expectations about rendered output.


def test_help_rows_render_through_one_helper() -> None:
    """The two-column printf appears once, inside `row()`, and rows call `row()`.

    One printf occurrence proves no row bypasses the helper (the format lives
    only inside `row()`'s short-row branch); the ENGINE OPTIONS row's `row()`
    call pins that the wrapping-motivating OPTIONS row routes through it too,
    a legend entry's call pins that the legend uses the same columns as the
    table it describes, and the first row's call pins that the helper
    predates every row.
    """
    recipe = _help_recipe_body(_makefile_text())
    row_format = "printf '  \\033[1;36m%-23s\\033[0m %-7s %s\\n'"
    assert recipe.count(row_format) == 1, (
        "the two-column printf must exist only inside the row() helper; a row "
        "rendering through its own printf bypasses the description-column cap"
    )
    assert 'row "ENGINE=local|<name>"' in recipe
    assert 'row "" "both"' in recipe
    assert 'row "make up"' in recipe


def test_help_row_helper_constants_match_the_helpline_module() -> None:
    """`row()`'s literal threshold and column widths equal helpline's geometry.

    The shell helper decides inline-versus-wrapped from its own literals (the
    120-character description cap) and the module wraps from the same
    geometry; reading both sides here is what keeps a change to one from
    silently outdating the other.
    """
    recipe = _help_recipe_body(_makefile_text())
    threshold = re.search(re.escape('"$${#3}" -le ') + r"(\d+)", recipe)
    assert threshold is not None, "row() carries no description-length threshold to pin"
    assert int(threshold.group(1)) == helpline.DESCRIPTION_MAX
    widths = re.search(r"printf '  \\033\[1;36m%-(\d+)s\\033\[0m %-(\d+)s", recipe)
    assert widths is not None, "row() carries no two-column printf to pin"
    assert int(widths.group(1)) == helpline.TARGET_WIDTH
    assert int(widths.group(2)) == helpline.SCOPE_WIDTH


def test_help_column_titles_pin_the_value_columns() -> None:
    """The column titles are flush left, over the columns they name.

    TARGET starts at column 0 -- the section headers' column, per the help
    header's design -- while SCOPE and WHAT IT DOES sit exactly over the
    scope and description columns the rows render at, so the titles govern
    the table all the way down. The prerequisites table's TARGETS /
    REQUIREMENTS titles follow the same rule for its own geometry.
    """
    recipe = _help_recipe_body(_makefile_text())
    titles = re.search(
        r"printf '\\n\\033\[1m%-(\d+)s\\033\[0m %-(\d+)s %s\\n' \"TARGET\" \"SCOPE\"",
        recipe,
    )
    assert titles is not None, "no TARGET/SCOPE column-title row found to pin"
    # TARGET's field spans from column 0 to one separator short of where the
    # scope column begins: the rows' two-space indent plus the target field.
    assert int(titles.group(1)) == 2 + helpline.TARGET_WIDTH
    assert int(titles.group(2)) == helpline.SCOPE_WIDTH
    prerequisite_titles = re.search(
        r"printf '\\033\[1m%-(\d+)s\\033\[0m %s\\n' \"TARGETS\"",
        recipe,
    )
    assert prerequisite_titles is not None, "no TARGETS column-title row found to pin"
    assert int(prerequisite_titles.group(1)) == 2 + helpline.TARGET_WIDTH
    assert "devcontainer_config.helpline" in recipe, (
        "rows past the limit must wrap through devcontainer_config.helpline"
    )


# ---------------------------------------------------------------------------
# Review round: REMOTE_AWS_REGION carries no default anywhere in the
# Makefile, because root.hcl derives each instance's state bucket's name
# from it -- a silently substituted region would address another region's
# bucket instead of failing. The guard macro is the single statement of the
# requirement; every region-consuming target expands it. The deploy recipe
# applies the plan it guarded (saved with -out, removed by a trap), its
# status probes must answer exactly true or false, and the init-bootstrap
# retry triggers only on Terragrunt's own missing-bucket wording.
# ---------------------------------------------------------------------------


# The guard line the REMOTE_AWS_REGION_GUARD define must carry, exactly as
# the Makefile source spells it (the $$ is make's escape for the shell's $).
_REGION_GUARD_LINE = (
    ': "$${REMOTE_AWS_REGION:?REMOTE_AWS_REGION must be set '
    '(no default: root.hcl names the state bucket from it)}"'
)

# The targets that name a region and must therefore expand the guard.
_REGION_GUARD_TARGETS: tuple[str, ...] = (
    "instance-stop",
    "instance-start",
    "instance-destroy",
)


def test_the_makefile_never_defaults_the_region() -> None:
    """No `us-east-1` (and so no `:-us-east-1` fallback) survives anywhere.

    The literal must not appear in any recipe, help row or comment: a
    default that survives in a comment still reads as the sanctioned value,
    and instance-init's REGION= is the AMI/AZ lookup only.
    """
    assert "us-east-1" not in _makefile_text(), (
        "REMOTE_AWS_REGION must have no default in the Makefile: root.hcl "
        "names the state bucket from it, so a fallback region would silently "
        "address the wrong bucket"
    )


def test_the_region_guard_define_carries_the_fail_fast_requirement() -> None:
    body = _make_define_body(_makefile_text(), "REMOTE_AWS_REGION_GUARD")
    assert _REGION_GUARD_LINE in body, (
        "REMOTE_AWS_REGION_GUARD must require the variable with :? and name why there is no default"
    )


@pytest.mark.parametrize("target", _REGION_GUARD_TARGETS)
def test_region_consuming_targets_expand_the_guard(target: str) -> None:
    """Every region-naming target requires the variable before it runs."""
    recipe = _target_recipe_body(_makefile_text(), target)
    assert "$(REMOTE_AWS_REGION_GUARD)" in recipe, (
        f"{target} names REMOTE_AWS_REGION but never requires it; expand "
        "$(REMOTE_AWS_REGION_GUARD) so an unset region fails fast instead of "
        "falling back"
    )


def test_the_terragrunt_helper_expands_the_region_guard() -> None:
    """tg_init fronts every Terragrunt-running target (plan, deploy,
    destroy), so the guard inside it covers all of them."""
    body = _make_define_body(_makefile_text(), "TERRAGRUNT_INIT_HELPER")
    assert "$(REMOTE_AWS_REGION_GUARD)" in body


def test_instance_init_help_row_presents_region_as_the_ami_az_lookup_only() -> None:
    """REGION= must never read as the deployment region in the help surface.

    instance-init's REGION= resolves the default AMI and the written
    availability zone at scaffold time; the deployment region is
    REMOTE_AWS_REGION. The row must say so.
    """
    help_recipe = _help_recipe_body(_makefile_text())
    rows = [line for line in help_recipe.splitlines() if '"make instance-init"' in line]
    assert len(rows) == 1
    assert "REGION=" in rows[0], "the row must still advertise the AMI/AZ lookup variable"
    assert "AMI/AZ" in rows[0], "the row must scope REGION= to the AMI/AZ lookup"
    assert "REMOTE_AWS_REGION" in rows[0], (
        "the row must name the variable that actually selects the deployment region"
    )


def test_deploy_applies_the_saved_plan_it_guarded() -> None:
    """The guarded plan is the applied plan: saved with -out, consumed by
    apply, removed on every exit path by the per-instance trap."""
    recipe = _target_recipe_body(_makefile_text(), "instance-deploy")
    plan_out = recipe.index("plan -out=tfplan.deploy")
    apply_index = recipe.index("terragrunt apply -auto-approve tfplan.deploy")
    assert plan_out < apply_index, (
        "apply must consume the saved tfplan.deploy, so the plan that was "
        "guarded is the plan that runs"
    )
    assert "trap 'rm -f \"$$plan_file\"' EXIT" in recipe, (
        "the saved plan file must be removed by the per-instance EXIT trap, "
        "on failure paths too, not left behind in the instance directory"
    )


def test_deploy_status_probes_must_answer_exactly_true_or_false() -> None:
    """Empty, null or malformed probe answers fail the deploy: no `// false`
    fallback and no `|| true` swallowing on the status or jq probes."""
    recipe = _target_recipe_body(_makefile_text(), "instance-deploy")
    assert "// false" not in recipe, (
        "the jq probes must not fall back to false -- an unanswerable probe "
        "would masquerade as 'absent' and skip the trust-chain converge"
    )
    status_index = recipe.index("instance-status")
    line_end = recipe.index("\n", status_index)
    assert "|| true" not in recipe[status_index:line_end], (
        "a failed instance-status probe must fail the deploy, not be swallowed"
    )
    for field in ("certs_present", "params_present"):
        assert f'probe_bool "$$name" {field}' in recipe, (
            f"{field} must be read through the strict probe_bool check"
        )
    assert "true|false)" in recipe, (
        "probe_bool must accept exactly true or false and reject everything else"
    )


def test_init_bootstrap_retries_only_on_the_missing_bucket_message() -> None:
    """The bootstrap trigger must match Terragrunt's own wording -- both
    phrases, case-insensitively -- so versioning or AccessDenied failures on
    an existing bucket surface instead of triggering a bootstrap."""
    body = _make_define_body(_makefile_text(), "TERRAGRUNT_INIT_HELPER")
    assert "grep -qi 'remote state bucket'" in body
    assert "grep -qi 'does not exist'" in body
    assert "grep -qi 'bucket'" not in body, (
        "the trigger must not fire on any line mentioning a bucket: a "
        "versioning or AccessDenied error on an existing bucket must fail "
        "the init, not trigger a bootstrap"
    )
