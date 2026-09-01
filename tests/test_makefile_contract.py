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
from conftest import _makefile_text, _resolve_make_refs
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
    root = find_root(Path(__file__).resolve().parent)
    makefile_text = _makefile_text()
    hint = _install_hint(makefile_text, tool)
    make_path = shutil.which("make")
    assert make_path is not None, "make must be installed on the machine running this test suite"
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
    timeout_seconds = float(os.environ.get("MAKEFILE_GUARD_TEST_TIMEOUT_SECONDS", "30"))
    result = subprocess.run(
        [make_path, "test"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout_seconds,
    )
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
