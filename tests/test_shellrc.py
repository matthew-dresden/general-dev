"""Tests for devcontainer_config.shellrc: the devsecret export-list shell-startup

block (spec Section 11; E3-F2-S2-T1), the postCreate wiring that appends it to
both startup files, and the shell.env.example rewrite that retires the
"treat every value here as a secret" instruction (behavior change B5).

The `devcontainer_config` import is deferred into function bodies (via
`_import_shellrc` below) instead of done once at module scope, for the same
reason `tests/test_repo.py` documents: the TDD RED gate stashes this unit's
own production-source files -- `shellrc.py` is new, added by this task -- and
re-runs a single named test node. A module-level
`from devcontainer_config.shellrc import ...` would fail COLLECTION for the
whole file in that state (pytest exit 2, no test outcome recorded) instead of
failing the one named test for the real reason: the module is missing.

`devcontainer_config.repo.find_root` is imported at module scope instead:
`repo.py` is not in this task's Changes Manifest, so the RED gate's stash
never touches it, the same reasoning `tests/test_makefile_contract.py` and
`tests/test_postcreate_hooks.py` already document for their own top-level
imports of that module.

The `.devcontainer/.devcontainer.postcreate.sh` and `shell.env.example`
assertions below are text-level, the same discipline
`tests/test_postcreate_hooks.py` documents: postCreate's very first
non-comment lines require a real `CONTAINER_USER` resolvable through
`getent passwd`, which this hermetic suite cannot guarantee, so this task
never executes that script -- it only asserts on its text.

The AC-CYCLE-001 end-to-end test is the one place this file executes real
shell processes: it renders the block for each supported shell, writes it
into a throwaway startup script under `tmp_path`, and runs that script with
a stub `devsecret` (also written under `tmp_path`) standing in for the real
catalog CLI. The stub logs every invocation's arguments -- names, never a
value -- to a log file under `tmp_path`, which is what lets the test assert
"the stub was never handed a value in its argv" by reading that log back
and confirming neither generated value string appears in it, rather than by
trying to intercept the subprocess call directly. Every value is generated
per test run via `uuid.uuid4()`, never a literal, the same discipline every
other file in this suite documents.
"""

from __future__ import annotations

import importlib
import os
import re
import shlex
import shutil
import stat
import subprocess
import uuid
from pathlib import Path
from types import ModuleType

import pytest
from devcontainer_config.repo import find_root

SUPPORTED_SHELLS = ("bash", "zsh")


def _import_shellrc() -> ModuleType:
    """Import devcontainer_config.shellrc from inside a function body.

    See the module docstring for why this is not a module-level import.
    """
    return importlib.import_module("devcontainer_config.shellrc")


def _repo_root() -> Path:
    return find_root(Path(__file__).resolve().parent)


def _postcreate_text() -> str:
    """`.devcontainer/.devcontainer.postcreate.sh`, read fresh for every call."""
    return (_repo_root() / ".devcontainer" / ".devcontainer.postcreate.sh").read_text(
        encoding="utf-8"
    )


def _shell_env_example_text() -> str:
    """`shell.env.example`, read fresh for every call."""
    return (_repo_root() / "shell.env.example").read_text(encoding="utf-8")


def _configure_shell_env_body() -> str:
    """The `configure_shell_env` function body from `.devcontainer.postcreate.sh`.

    Isolated by name, the same pattern `tests/test_postcreate_hooks.py`
    uses for `install_git_hooks`, so an assertion meant for this step
    cannot be satisfied by unrelated text elsewhere in the 500-line script.
    """
    match = re.search(
        r"^configure_shell_env\(\)\s*\{(.*?)^\}", _postcreate_text(), re.MULTILINE | re.DOTALL
    )
    assert match is not None, (
        "no configure_shell_env function found in .devcontainer/.devcontainer.postcreate.sh"
    )
    return match.group(1)


# ---------------------------------------------------------------------------
# The renderer (AC-FUNC-001, AC-FUNC-002, AC-TEST-001)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("shell", SUPPORTED_SHELLS)
def test_render_contains_the_marker(shell: str) -> None:
    """AC-FUNC-006: the marker is present so a second application is detectable."""
    shellrc = _import_shellrc()
    block = shellrc.render(shell)
    assert shellrc.MARKER in block


@pytest.mark.parametrize("shell", SUPPORTED_SHELLS)
def test_render_invokes_export_list_exactly_once(shell: str) -> None:
    """AC-FUNC-002: `devsecret export-list` is invoked exactly once in the block.

    Counts the command-substitution invocation form specifically
    (`$(devsecret export-list)`), not every textual mention: the remedy
    line printed on the failure path also names `devsecret export-list` in
    prose, which is not a second invocation.
    """
    shellrc = _import_shellrc()
    block = shellrc.render(shell)
    assert block.count("$(devsecret export-list)") == 1


@pytest.mark.parametrize("shell", SUPPORTED_SHELLS)
def test_render_invokes_get_once_per_returned_name(shell: str) -> None:
    """AC-FUNC-002: `devsecret get` appears exactly once, inside the per-name loop.

    The loop is rendered once and iterates once per name `export-list`
    returns at shell startup; a single `devsecret get` call site in the
    block's text is what "once per name" means for a template rendered
    once and executed N times. Counts the command-substitution invocation
    form specifically, not the error and remedy lines that also mention
    `devsecret get` in prose.
    """
    shellrc = _import_shellrc()
    block = shellrc.render(shell)
    assert block.count('$(devsecret get "') == 1


@pytest.mark.parametrize("shell", SUPPORTED_SHELLS)
def test_render_exports_through_the_shell_builtin(shell: str) -> None:
    """AC-FUNC-002: the fetched value is assigned via `export`, never via a new process."""
    shellrc = _import_shellrc()
    block = shellrc.render(shell)
    assert re.search(r'\bexport\s+"\$\{[a-zA-Z_]+\}=\$\{[a-zA-Z_]+\}"', block)
    for forbidden in ("env ", "eval ", "sh -c", "exec "):
        assert forbidden not in block, f"{forbidden!r} would spawn a process carrying a value"


@pytest.mark.parametrize("shell", SUPPORTED_SHELLS)
def test_render_is_deterministic(shell: str) -> None:
    """AC-FUNC-006: rendering twice for the same shell yields byte-identical text."""
    shellrc = _import_shellrc()
    assert shellrc.render(shell) == shellrc.render(shell)


def test_render_differs_between_shells_by_a_single_line() -> None:
    """AC-FUNC-009: the per-shell difference is one parameter, not two copies of the text."""
    shellrc = _import_shellrc()
    bash_lines = shellrc.render("bash").splitlines()
    zsh_lines = shellrc.render("zsh").splitlines()
    assert len(bash_lines) == len(zsh_lines)
    differing = [(a, b) for a, b in zip(bash_lines, zsh_lines, strict=True) if a != b]
    assert len(differing) == 1, f"expected exactly one differing line, found {differing}"


@pytest.mark.parametrize("shell", ["fish", "csh", "powershell", "", "BASH", "sh"])
def test_render_rejects_an_unsupported_shell(shell: str) -> None:
    """AC-FUNC-001, AC-TEST-001, AC-TEST-005: the error names what was asked and what is
    supported.
    """
    shellrc = _import_shellrc()
    with pytest.raises(shellrc.UnsupportedShellError) as exc_info:
        shellrc.render(shell)
    message = str(exc_info.value)
    assert repr(shell) in message
    for supported in shellrc.SUPPORTED_SHELLS:
        assert supported in message


# ---------------------------------------------------------------------------
# The CLI entry point (AC-FUNC-008: what postCreate actually invokes)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("shell", SUPPORTED_SHELLS)
def test_main_prints_the_rendered_block_and_exits_zero(
    capsys: pytest.CaptureFixture[str], shell: str
) -> None:
    """`main([shell])` is exactly what `python3 -m devcontainer_config.shellrc <shell>` runs."""
    shellrc = _import_shellrc()
    exit_code = shellrc.main([shell])
    assert exit_code == shellrc.EXIT_SUCCESS
    out = capsys.readouterr().out
    assert out.strip() == shellrc.render(shell).strip()


def test_main_rejects_an_unsupported_shell_without_raising(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC-FUNC-008: a bad shell name is a usage error on stderr, not an uncaught traceback."""
    shellrc = _import_shellrc()
    exit_code = shellrc.main(["fish"])
    assert exit_code == shellrc.EXIT_USAGE_ERROR
    err = capsys.readouterr().err
    assert "fish" in err
    for supported in shellrc.SUPPORTED_SHELLS:
        assert supported in err


def test_main_with_no_argument_is_a_usage_error() -> None:
    """A missing SHELL positional is argparse's own usage error (exit code 2)."""
    shellrc = _import_shellrc()
    with pytest.raises(SystemExit) as exc_info:
        shellrc.main([])
    assert exc_info.value.code == shellrc.EXIT_USAGE_ERROR


def test_module_invoked_as_a_script_renders_the_block_via_subprocess() -> None:
    """The exact invocation `.devcontainer.postcreate.sh` uses:

    `PYTHONPATH=<scripts dir> python3 -m devcontainer_config.shellrc bash`.
    """
    scripts_dir = _repo_root() / ".claude" / "plugins" / "devcontainer" / "scripts"
    env = dict(os.environ)
    env["PYTHONPATH"] = str(scripts_dir)
    result = subprocess.run(
        ["python3", "-m", "devcontainer_config.shellrc", "bash"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "devsecret export-list" in result.stdout


# ---------------------------------------------------------------------------
# Safety properties (AC-FUNC-003, AC-FUNC-004, AC-FUNC-005, AC-TEST-002)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("shell", SUPPORTED_SHELLS)
def test_render_has_no_disk_redirection(shell: str) -> None:
    """AC-FUNC-003: every '>' redirection targets /dev/null or a stream duplication, never

    a path that would persist a value to disk.
    """
    shellrc = _import_shellrc()
    block = shellrc.render(shell)
    allowed_targets = {"/dev/null", "&1", "&2"}
    for match in re.finditer(r">{1,2}\s*([^\s;]+)", block):
        target = match.group(1)
        assert target in allowed_targets, f"unexpected redirection target {target!r}"


@pytest.mark.parametrize("shell", SUPPORTED_SHELLS)
def test_render_has_no_tracing_directive(shell: str) -> None:
    """AC-FUNC-003 / AC-TEST-002: no `set -x` (or its long form) is present."""
    shellrc = _import_shellrc()
    block = shellrc.render(shell)
    assert "set -x" not in block
    assert "xtrace" not in block


@pytest.mark.parametrize("shell", SUPPORTED_SHELLS)
def test_render_has_no_shell_abort_statement(shell: str) -> None:
    """AC-TEST-002: nothing in the block would abort the shell it is sourced into."""
    shellrc = _import_shellrc()
    block = shellrc.render(shell)
    assert re.search(r"(?<!\w)exit\b", block) is None
    assert re.search(r"(?<!\w)return\b", block) is None
    assert "set -e" not in block
    assert "errexit" not in block


@pytest.mark.parametrize("shell", SUPPORTED_SHELLS)
def test_render_never_substitutes_an_empty_or_default_value(shell: str) -> None:
    """AC-FUNC-005: a fetch failure is never papered over with an empty or default export."""
    shellrc = _import_shellrc()
    block = shellrc.render(shell)
    assert ":-" not in block
    assert ':=""' not in block
    assert "=''" not in block
    assert '=""' not in block


@pytest.mark.parametrize("shell", SUPPORTED_SHELLS)
def test_render_prints_a_remediation_line_on_each_failure_path(shell: str) -> None:
    """AC-FUNC-004: the export-list failure, the get failure, and devsecret being absent

    from PATH each emit a remedy on stderr.
    """
    shellrc = _import_shellrc()
    block = shellrc.render(shell)
    assert block.count("remedy:") == 3
    assert block.count(">&2") >= 6


@pytest.mark.parametrize("shell", SUPPORTED_SHELLS)
def test_render_prints_error_and_remedy_when_devsecret_is_missing(shell: str) -> None:
    """AC-FUNC-004 / AC-TEST-005: the block's own contract ("prints the error and the

    remedy on stderr, exports nothing for that name, leaves the shell usable") also
    covers `devsecret` being absent from PATH, not only `export-list`/`get` failing once
    it was found. Before this, the `if command -v devsecret ...; then ... fi` wrapper had
    no `else`, so a missing `devsecret` produced no output at all -- exactly the silent
    failure the contract forbids.
    """
    shellrc = _import_shellrc()
    block = shellrc.render(shell)
    match = re.search(r"if command -v devsecret[^\n]*\n(.*)\nfi\n\Z", block, re.DOTALL)
    assert match is not None, "expected the block to end with the command -v devsecret guard"
    guarded_body = match.group(1)
    else_match = re.search(r"\nelse\n(.*)\Z", guarded_body, re.DOTALL)
    assert else_match is not None, "expected an else branch for devsecret missing from PATH"
    else_body = else_match.group(1)
    assert "ERROR:" in else_body
    assert "remedy:" in else_body
    assert "devsecret" in else_body.lower()
    assert ">&2" in else_body


@pytest.mark.parametrize("shell", SUPPORTED_SHELLS)
def test_render_twice_marker_makes_a_second_application_detectable(shell: str) -> None:
    """AC-FUNC-006: idempotence -- rendering twice yields byte-identical text with the

    marker appearing exactly once, which is the property a caller's guard (asserted
    separately below, in the postCreate wiring section) relies on to detect a prior
    application before appending a second one.
    """
    shellrc = _import_shellrc()
    first = shellrc.render(shell)
    second = shellrc.render(shell)
    assert first == second
    assert first.count(shellrc.MARKER) == 1


# ---------------------------------------------------------------------------
# The postCreate wiring (AC-FUNC-007, AC-FUNC-008, AC-TEST-003)
# ---------------------------------------------------------------------------


def test_postcreate_defines_a_render_helper_for_the_devsecret_block() -> None:
    """AC-FUNC-007: postCreate calls the renderer rather than hand-writing the block."""
    assert "devcontainer_config.shellrc" in _postcreate_text()


def test_configure_shell_env_renders_the_bash_block() -> None:
    """AC-FUNC-007: the bash startup file's block is rendered for the bash shell."""
    body = _configure_shell_env_body()
    assert re.search(r"render_devsecret_shell_block\s+bash", body)


def test_configure_shell_env_renders_the_zsh_block() -> None:
    """AC-FUNC-007: the zsh environment file's block is rendered for the zsh shell."""
    body = _configure_shell_env_body()
    assert re.search(r"render_devsecret_shell_block\s+zsh", body)


def test_configure_shell_env_appends_the_bash_block_to_the_bash_startup_file() -> None:
    """AC-FUNC-007: the rendered bash block is written into BASH_RC, beside the shell.env

    lines, not left unused.
    """
    body = _configure_shell_env_body()
    assert re.search(r'bash_block.*>>\s*"\$\{BASH_RC\}"', body, re.DOTALL)


def test_configure_shell_env_appends_the_zsh_block_to_the_zsh_environment_file() -> None:
    """AC-FUNC-007: the rendered zsh block is written into ZSH_ENV, beside the shell.env

    lines, not left unused.
    """
    body = _configure_shell_env_body()
    assert re.search(r'zsh_block.*>>\s*"\$\{ZSH_ENV\}"', body, re.DOTALL)


def test_configure_shell_env_does_not_swallow_a_render_failure() -> None:
    """AC-FUNC-008 / AC-TEST-002 discipline: nothing discards the render command's status."""
    body = _configure_shell_env_body()
    forgiving_suffixes = ("|| true", "|| :", "; true", "2>/dev/null", "> /dev/null")
    for line in body.splitlines():
        if "render_devsecret_shell_block" not in line:
            continue
        for suffix in forgiving_suffixes:
            assert suffix not in line, f"{suffix!r} would swallow a non-zero render status"


def test_configure_shell_env_aborts_through_exit_with_error_on_a_bash_render_failure() -> None:
    """AC-FUNC-008: a non-zero render is fatal through exit_with_error, not warned about."""
    body = _configure_shell_env_body()
    render_idx = body.index("render_devsecret_shell_block bash")
    error_idx = body.index("exit_with_error", render_idx)
    tail = body[render_idx:error_idx]
    assert "||" in tail, "the bash render's failure is not wired to a handler"
    assert "log_section_skipped" not in body[render_idx : error_idx + 400]


def test_configure_shell_env_aborts_through_exit_with_error_on_a_zsh_render_failure() -> None:
    """AC-FUNC-008: a non-zero render is fatal through exit_with_error, not warned about."""
    body = _configure_shell_env_body()
    render_idx = body.index("render_devsecret_shell_block zsh")
    error_idx = body.index("exit_with_error", render_idx)
    tail = body[render_idx:error_idx]
    assert "||" in tail, "the zsh render's failure is not wired to a handler"


def test_configure_shell_env_reuses_the_shared_printers() -> None:
    """AC-3.1: no new error printer is introduced; the existing primitives are reused."""
    body = _configure_shell_env_body()
    assert re.search(r"\bexit_with_error\b", body)
    assert re.search(r"\blog_section_done\b", body)


def test_configure_shell_env_derives_the_bash_marker_from_the_rendered_block() -> None:
    """AC-FUNC-006: the guard's marker text is read back out of the rendered block itself

    (its own first line), rather than a second hand-copied literal of `shellrc.MARKER`
    that could drift out of sync with the renderer.
    """
    body = _configure_shell_env_body()
    assert re.search(r'bash_marker="\$\(printf[^\n]*bash_block[^\n]*\|\s*head\s+-n\s*1\)"', body)


def test_configure_shell_env_derives_the_zsh_marker_from_the_rendered_block() -> None:
    """AC-FUNC-006: same as the bash case, for the zsh marker."""
    body = _configure_shell_env_body()
    assert re.search(r'zsh_marker="\$\(printf[^\n]*zsh_block[^\n]*\|\s*head\s+-n\s*1\)"', body)


def test_configure_shell_env_guards_the_bash_append_against_a_second_application() -> None:
    """AC-FUNC-006: idempotence -- a second `configure_shell_env` run must not duplicate

    the block in `BASH_RC`. The append is guarded by a grep for the block's own marker
    line, the same `grep -q ... || <action>` guard style this file already uses (see
    `zsh_path` in the tmux step and `ZSH_THEME` in the Oh My Zsh step).
    """
    body = _configure_shell_env_body()
    assert re.search(
        r'grep\s+-qF\s+--\s+"\$\{bash_marker\}"\s+"\$\{BASH_RC\}"\s*\|\|.*bash_block.*>>\s*"\$\{BASH_RC\}"',
        body,
    ), "the bash append is not guarded by a marker grep"


def test_configure_shell_env_guards_the_zsh_append_against_a_second_application() -> None:
    """AC-FUNC-006: same as the bash case, for `ZSH_ENV`."""
    body = _configure_shell_env_body()
    assert re.search(
        r'grep\s+-qF\s+--\s+"\$\{zsh_marker\}"\s+"\$\{ZSH_ENV\}"\s*\|\|.*zsh_block.*>>\s*"\$\{ZSH_ENV\}"',
        body,
    ), "the zsh append is not guarded by a marker grep"


# ---------------------------------------------------------------------------
# The shell.env.example rewrite (AC-FUNC-010, AC-DOC-001, AC-TEST-004)
# ---------------------------------------------------------------------------


def test_shell_env_example_no_longer_treats_every_value_as_a_secret() -> None:
    """AC-FUNC-010, behavior change B5: the retired instruction is gone."""
    text = _shell_env_example_text()
    assert "treat every value here as a secret" not in text.lower()
    assert "treat every value" not in text.lower()


def _project_specific_block() -> str:
    """The "Project-specific (optional)" section body of shell.env.example.

    Bounded from its own heading to the next `#`-ruled section heading, so
    an assertion meant for this block cannot be satisfied by an unrelated
    mention elsewhere in the file (the header, or another section).
    """
    match = re.search(
        r"# Project-specific \(optional\)\n#+\n(.*?)(?=\n#{10,}\n|\Z)",
        _shell_env_example_text(),
        re.DOTALL,
    )
    assert match is not None, "no Project-specific block found in shell.env.example"
    return match.group(1)


def test_shell_env_example_project_block_points_at_the_catalog() -> None:
    """AC-FUNC-010: the project block names the catalog and `devsecret set`."""
    assert "devsecret set" in _project_specific_block()


def test_shell_env_example_project_block_has_no_credential_shaped_placeholder() -> None:
    """AC-TEST-004: no example `export` line in the project block looks like a credential.

    Only the (possibly commented-out) `export NAME=value` lines themselves
    are checked, not the surrounding prose: the prose legitimately names
    "credential" and "secret" while explaining where a real one belongs, and
    that explanation is not itself a credential-shaped placeholder.
    """
    export_lines = [
        line for line in _project_specific_block().splitlines() if re.match(r"^#?\s*export\s", line)
    ]
    assert export_lines, "expected at least one example export line in the project block"
    credential_shaped_tokens = ("token", "key", "password", "secret", "webhook", "credential")
    for line in export_lines:
        lowered = line.lower()
        for token in credential_shaped_tokens:
            assert token not in lowered, f"credential-shaped placeholder found: {line!r}"


def test_shell_env_example_header_documents_identity_and_configuration_only() -> None:
    """AC-DOC-001: the header states the file carries identity and configuration only."""
    text = _shell_env_example_text()
    header = text.split("########################", 1)[0]
    assert "credential" in header.lower()
    assert "secret catalog" in header.lower() or "catalog" in header.lower()


def test_shell_env_example_header_documents_automatic_export_at_shell_startup() -> None:
    """AC-DOC-001: the header states exported credentials arrive automatically at

    shell startup.
    """
    text = _shell_env_example_text()
    header = text.split("########################", 1)[0]
    assert "exported" in header.lower()
    assert "shell startup" in header.lower() or "startup" in header.lower()


# ---------------------------------------------------------------------------
# End-to-end (AC-CYCLE-001)
# ---------------------------------------------------------------------------


def _require_interpreter(interpreter: str) -> None:
    """Fail fast, with a diagnostic, if `interpreter` is not on PATH.

    `.github/workflows/ci.yml`'s `ubuntu-latest` runner does not ship `zsh`
    in its base image, so `subprocess.run([interpreter, ...])` below would
    otherwise raise a raw `FileNotFoundError` there instead of failing with
    an actionable message (rule 27). Not a skip: a missing interpreter is a
    real precondition failure of the test environment, not an expected
    absence (AC-FINAL-013 forbids skip/xfail here), so this asserts rather
    than skipping.
    """
    assert shutil.which(interpreter) is not None, (
        f"{interpreter!r} is not on PATH; install it (e.g. via the OS package manager "
        f"or a devcontainer feature) to run the AC-CYCLE-001 end-to-end cases for {interpreter!r}."
    )


def _make_executable(path: Path) -> None:
    mode = path.stat().st_mode
    path.chmod(mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _stub_value_env_var(name: str) -> str:
    """The environment variable the stub resolves `name`'s value through.

    Never a literal value baked into the stub script's own source: the
    stub reads it back out of its own process environment at call time
    (bash indirect parameter expansion, `${!var}`), which the test supplies
    only through `subprocess.run(..., env=...)` -- in memory, never written
    to any file under `tmp_path` -- so the stub script on disk never
    contains a value literal.
    """
    return f"__STUB_DEVSECRET_VALUE_{name}"


def _write_stub_devsecret(
    bin_dir: Path,
    *,
    log_file: Path,
    names: tuple[str, ...],
    export_list_exit: int = 0,
    failing_get_name: str = "",
) -> Path:
    """A `devsecret` double for the AC-CYCLE-001 end-to-end test.

    Every invocation's arguments (names, never a value) are appended to
    `log_file`, which is how the test later confirms neither generated
    value ever reached the stub's argv, without needing to intercept the
    subprocess call from the test process directly. A value itself is
    never written into this script: `get` resolves it through
    `_stub_value_env_var`, at call time, from the stub's own environment.

    Always a bash script regardless of which shell the startup script under
    test sources it from: `devsecret` is a separate executable the block
    invokes by name (`command -v devsecret`), so its own shebang, not the
    calling shell, controls its interpreter.
    """
    names_output = "\n".join(names)
    script = f"""#!/usr/bin/env bash
printf '%s\\n' "$*" >> {shlex.quote(str(log_file))}
case "${{1:-}}" in
  export-list)
    if [ "{export_list_exit}" != "0" ]; then
      echo "stub: export-list failed" >&2
      exit {export_list_exit}
    fi
    printf '%s\\n' {shlex.quote(names_output)}
    ;;
  get)
    name="${{2:-}}"
    failing_name={shlex.quote(failing_get_name)}
    if [ -n "${{failing_name}}" ] && [ "${{name}}" = "${{failing_name}}" ]; then
      echo "stub: get ${{name}} failed" >&2
      exit 1
    fi
    var_name="__STUB_DEVSECRET_VALUE_${{name}}"
    if [ -z "${{!var_name+x}}" ]; then
      echo "stub: unknown name ${{name}}" >&2
      exit 1
    fi
    printf '%s' "${{!var_name}}"
    ;;
  *)
    echo "stub: unknown command $*" >&2
    exit 1
    ;;
esac
"""
    stub_path = bin_dir / "devsecret"
    stub_path.write_text(script, encoding="utf-8")
    _make_executable(stub_path)
    return stub_path


def _write_startup_script(
    path: Path, *, interpreter: str, block: str, names: tuple[str, ...]
) -> None:
    result_lines = "\n".join(f'echo "RESULT:{name}=${{{name}:-<unset>}}"' for name in names)
    script = f"""#!/usr/bin/env {interpreter}
{block}
{result_lines}
echo "REACHED_FINAL_COMMAND"
"""
    path.write_text(script, encoding="utf-8")
    _make_executable(path)


def _run_startup_script(
    interpreter: str,
    script_path: Path,
    *,
    bin_dir: Path,
    values: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run `script_path` with `interpreter`, `bin_dir` first on PATH.

    `values`, if given, is handed to the stub `devsecret` only through the
    child process's own environment (`_stub_value_env_var` names the
    variable), never written to any file: this is what lets the stub
    resolve a value without that value ever having been on the stub
    script's own disk copy.
    """
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    for name, value in (values or {}).items():
        env[_stub_value_env_var(name)] = value
    return subprocess.run(
        [interpreter, str(script_path)],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def _assert_no_file_under_contains(root: Path, *values: str) -> None:
    for candidate in root.rglob("*"):
        if not candidate.is_file():
            continue
        content = candidate.read_text(encoding="utf-8", errors="strict")
        for value in values:
            assert value not in content, f"{candidate} unexpectedly contains a secret value"


@pytest.mark.parametrize("shell", SUPPORTED_SHELLS)
def test_end_to_end_exports_two_secrets_with_no_value_on_disk_or_in_argv(
    tmp_path: Path, shell: str
) -> None:
    """AC-CYCLE-001: the rendered block, executed for real, exports both fetched values

    through the shell builtin, the stub never receives a value in its argv, and no file
    under tmp_path ever contains either value.
    """
    _require_interpreter(shell)
    shellrc = _import_shellrc()
    names = (f"FOO_{uuid.uuid4().hex[:8].upper()}", f"BAR_{uuid.uuid4().hex[:8].upper()}")
    values = {
        names[0]: f"foo-value-{uuid.uuid4().hex}",
        names[1]: f"bar-value-{uuid.uuid4().hex}",
    }
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log_file = tmp_path / "devsecret-calls.log"
    _write_stub_devsecret(bin_dir, log_file=log_file, names=names)

    block = shellrc.render(shell)
    script_path = tmp_path / f"startup.{shell}"
    _write_startup_script(script_path, interpreter=shell, block=block, names=names)

    result = _run_startup_script(shell, script_path, bin_dir=bin_dir, values=values)

    assert result.returncode == 0, result.stderr
    assert "REACHED_FINAL_COMMAND" in result.stdout
    for name in names:
        assert f"RESULT:{name}={values[name]}" in result.stdout

    log_text = log_file.read_text(encoding="utf-8")
    assert log_text.count("export-list") == 1
    for name in names:
        assert f"get {name}" in log_text
    for value in values.values():
        assert value not in log_text, "the stub was handed a value in its argv"

    _assert_no_file_under_contains(tmp_path, *values.values())


@pytest.mark.parametrize("shell", SUPPORTED_SHELLS)
def test_end_to_end_export_list_failure_prints_remedy_and_still_reaches_final_command(
    tmp_path: Path, shell: str
) -> None:
    """AC-CYCLE-001 / AC-FUNC-004: a non-zero `devsecret export-list` prints its remedy on

    stderr, exports nothing, and the shell still reaches its final command.
    """
    _require_interpreter(shell)
    shellrc = _import_shellrc()
    names = (f"FOO_{uuid.uuid4().hex[:8].upper()}",)
    values = {names[0]: f"foo-value-{uuid.uuid4().hex}"}
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log_file = tmp_path / "devsecret-calls.log"
    _write_stub_devsecret(bin_dir, log_file=log_file, names=names, export_list_exit=1)

    block = shellrc.render(shell)
    script_path = tmp_path / f"startup.{shell}"
    _write_startup_script(script_path, interpreter=shell, block=block, names=names)

    result = _run_startup_script(shell, script_path, bin_dir=bin_dir, values=values)

    assert result.returncode == 0, result.stderr
    assert "REACHED_FINAL_COMMAND" in result.stdout
    assert f"RESULT:{names[0]}=<unset>" in result.stdout
    assert "remedy:" in result.stderr

    _assert_no_file_under_contains(tmp_path, *values.values())


@pytest.mark.parametrize("shell", SUPPORTED_SHELLS)
def test_end_to_end_get_failure_prints_remedy_and_still_reaches_final_command(
    tmp_path: Path, shell: str
) -> None:
    """AC-CYCLE-001 / AC-FUNC-004: a non-zero `devsecret get` for one name prints its

    remedy on stderr, exports nothing for that name, exports the other name normally, and
    the shell still reaches its final command.
    """
    _require_interpreter(shell)
    shellrc = _import_shellrc()
    names = (f"FOO_{uuid.uuid4().hex[:8].upper()}", f"BAR_{uuid.uuid4().hex[:8].upper()}")
    values = {
        names[0]: f"foo-value-{uuid.uuid4().hex}",
        names[1]: f"bar-value-{uuid.uuid4().hex}",
    }
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log_file = tmp_path / "devsecret-calls.log"
    _write_stub_devsecret(
        bin_dir,
        log_file=log_file,
        names=names,
        failing_get_name=names[0],
    )

    block = shellrc.render(shell)
    script_path = tmp_path / f"startup.{shell}"
    _write_startup_script(script_path, interpreter=shell, block=block, names=names)

    result = _run_startup_script(shell, script_path, bin_dir=bin_dir, values=values)

    assert result.returncode == 0, result.stderr
    assert "REACHED_FINAL_COMMAND" in result.stdout
    assert f"RESULT:{names[0]}=<unset>" in result.stdout
    assert f"RESULT:{names[1]}={values[names[1]]}" in result.stdout
    assert "remedy:" in result.stderr

    _assert_no_file_under_contains(tmp_path, *values.values())


@pytest.mark.parametrize("shell", SUPPORTED_SHELLS)
def test_end_to_end_missing_devsecret_prints_remedy_and_still_reaches_final_command(
    tmp_path: Path, shell: str
) -> None:
    """AC-CYCLE-001 / AC-FUNC-004: `devsecret` absent from PATH entirely (not merely

    returning a non-zero exit) prints an error and a remedy on stderr, exports nothing,
    and the shell still reaches its final command. Uses an empty stub bin directory: no
    `devsecret` executable is written there at all, and this test asserts up front that
    the ambient test environment does not already have a real `devsecret` on PATH, so a
    later change that starts installing the console script into this repo's own test
    venv fails this test loudly instead of silently making the missing-PATH branch
    untestable.
    """
    _require_interpreter(shell)
    assert shutil.which("devsecret") is None, (
        "devsecret is already on this test environment's PATH; "
        "this test can no longer exercise the devsecret-missing branch as written"
    )
    shellrc = _import_shellrc()
    names = (f"FOO_{uuid.uuid4().hex[:8].upper()}",)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    block = shellrc.render(shell)
    script_path = tmp_path / f"startup.{shell}"
    _write_startup_script(script_path, interpreter=shell, block=block, names=names)

    result = _run_startup_script(shell, script_path, bin_dir=bin_dir, values=None)

    assert result.returncode == 0, result.stderr
    assert "REACHED_FINAL_COMMAND" in result.stdout
    assert f"RESULT:{names[0]}=<unset>" in result.stdout
    assert "ERROR:" in result.stderr
    assert "remedy:" in result.stderr
    assert "devsecret" in result.stderr.lower()

    # No secret value exists in this scenario (devsecret was never found on
    # PATH, so `get` was never invoked and no value was ever fetched), so
    # unlike the sibling end-to-end cases above there is nothing for
    # `_assert_no_file_under_contains` to check here.
