"""Tests for devcontainer_config.cli: the `lint-secrets` command (spec Section 4.6).

The `devcontainer_config` import is deferred into function bodies (via
`gitfixtures.import_cli` / `gitfixtures.import_secrets`) instead of done once
at module scope, for the same reason documented in `tests/test_repo.py`: the
TDD RED gate stashes this unit's production-source files and re-runs a
single named test node, and a module-level
`from devcontainer_config.cli import ...` would fail COLLECTION for the
whole file (pytest exit 2, no test outcome recorded) instead of failing the
one test for the real reason.

Every fixture repository here is a real, disposable git repository created
under `tmp_path` by shelling out to the actual `git` binary, never a mocked
filesystem, because the whole point of this module -- reading the index, not
the working tree -- is a distinction only a real git repository can prove.
The primitives that build those repositories live in `tests/gitfixtures.py`,
shared with `tests/test_repo.py`, `tests/test_cli.py` and
`tests/test_secrets_range.py`, rather than being redefined here.

No credential-shaped literal is ever stored pre-assembled in this file. A
positive sample is built at run time from `devcontainer_config.secrets
.SAMPLE_PREFIXES` plus a `uuid.uuid4()` suffix, the same discipline
`tests/test_secrets.py` documents for the scanner's own case table, so this
file itself never becomes something a future `make lint-secrets` run would
flag.
"""

from __future__ import annotations

import ast
import importlib
import re
import subprocess
import uuid
from pathlib import Path
from types import ModuleType
from typing import cast

import pytest
from gitfixtures import (
    credential_line,
    generated_root,
    import_cli,
    import_secrets,
    init_repo,
    run_cli,
    stage_bytes,
    stage_text,
)


def _import_repo() -> ModuleType:
    """Import devcontainer_config.repo from inside a function body."""
    return importlib.import_module("devcontainer_config.repo")


def _plugin_scripts_module_path(*parts: str) -> Path:
    return (
        Path(__file__).resolve().parents[1]
        / ".claude"
        / "plugins"
        / "devcontainer"
        / "scripts"
        / "devcontainer_config"
        / Path(*parts)
    )


def _repo_root() -> Path:
    """This repository's own root, found from this test file's location."""
    return cast(Path, _import_repo().find_root(Path(__file__).resolve().parent))


def test_staged_credential_reported_and_exit_code_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC-TEST-001 / AC-FUNC-001 / AC-FUNC-004: a staged credential is reported, exit 1."""
    root = generated_root(tmp_path)
    init_repo(root)
    stage_text(root, "src/config.py", credential_line())

    exit_code = run_cli(monkeypatch, root, ["lint-secrets"])

    out = capsys.readouterr().out
    assert exit_code == 1
    assert "src/config.py" in out
    assert ":1:" in out
    assert "AWS access key identifier" in out


def test_index_scanned_not_working_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC-TEST-002: the staged index is scanned, not a working-tree edit made afterward."""
    root = generated_root(tmp_path)
    init_repo(root)
    path = stage_text(root, "src/config.py", credential_line())
    path.write_text("CREDENTIAL=removed\n", encoding="utf-8")

    exit_code = run_cli(monkeypatch, root, ["lint-secrets"])

    out = capsys.readouterr().out
    assert exit_code == 1
    assert "src/config.py" in out
    assert "AWS access key identifier" in out


def test_clean_staged_tree_exits_zero_with_header(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC-TEST-003 / AC-FUNC-002 / AC-FUNC-003: a clean staged file exits 0 with a header."""
    root = generated_root(tmp_path)
    init_repo(root)
    stage_text(root, "README.md", "nothing sensitive here\n")

    exit_code = run_cli(monkeypatch, root, ["lint-secrets"])

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "[LINT]" in out
    assert "staged paths scanned: 1" in out
    assert "shell.env lines: 0" in out
    assert "catalog secret names: 0 (catalog client not yet wired into this scan)" in out


def test_empty_index_exits_zero_and_reports_zero_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC-TEST-003 / AC-FUNC-002: nothing staged exits 0 and says zero paths were scanned."""
    root = generated_root(tmp_path)
    init_repo(root)

    exit_code = run_cli(monkeypatch, root, ["lint-secrets"])

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "staged paths scanned: 0" in out


def test_shell_env_lines_counted_in_header(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC-FUNC-003 / AC-3.1: shell.env is read through the repo module and counted."""
    root = generated_root(tmp_path)
    init_repo(root)
    marker = uuid.uuid4().hex
    (root / "shell.env").write_text(f"VAR_ONE={marker}\nVAR_TWO=example\n", encoding="utf-8")
    stage_text(root, "README.md", "nothing sensitive here\n")

    exit_code = run_cli(monkeypatch, root, ["lint-secrets"])

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "shell.env lines: 2" in out


def test_outside_git_work_tree_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Error Handling Contract: invoked outside a git work tree, exit non-zero with ERROR."""
    root = generated_root(tmp_path)

    exit_code = run_cli(monkeypatch, root, ["lint-secrets"])

    err = capsys.readouterr().err
    assert exit_code != 0
    assert "ERROR:" in err
    assert str(root) in err


def test_staged_paths_raises_when_root_is_not_a_git_work_tree(tmp_path: Path) -> None:
    """Error Handling Contract: `secrets.staged_paths` itself refuses a non-repository root."""
    secrets = import_secrets()
    root = generated_root(tmp_path)

    with pytest.raises(secrets.SecretScanError, match="not a git work tree") as exc_info:
        secrets.staged_paths(root)

    assert str(root) in str(exc_info.value)


def test_staged_paths_raises_when_git_is_not_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Error Handling Contract: `secrets.staged_paths` names the missing `git` binary."""
    secrets = import_secrets()
    root = generated_root(tmp_path)
    monkeypatch.setenv("PATH", "")

    with pytest.raises(secrets.SecretScanError, match="git is not installed"):
        secrets.staged_paths(root)


def test_non_utf8_staged_blob_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Error Handling Contract: a staged blob that is not valid UTF-8, exit non-zero with ERROR."""
    root = generated_root(tmp_path)
    init_repo(root)
    stage_bytes(root, "binary.dat", b"\xff\xfe\x00not-utf8")

    exit_code = run_cli(monkeypatch, root, ["lint-secrets"])

    err = capsys.readouterr().err
    assert exit_code != 0
    assert "ERROR:" in err
    assert "binary.dat" in err
    assert "unstage" in err.lower() or "restore --staged" in err


def test_unreadable_shell_env_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Error Handling Contract: an unreadable shell.env, exit non-zero with ERROR."""
    root = generated_root(tmp_path)
    init_repo(root)
    stage_text(root, "README.md", "nothing sensitive here\n")
    shell_env = root / "shell.env"
    shell_env.write_text("VAR=example\n", encoding="utf-8")
    shell_env.chmod(0o000)

    try:
        exit_code = run_cli(monkeypatch, root, ["lint-secrets"])
        err = capsys.readouterr().err
        assert exit_code != 0
        assert "ERROR:" in err
        assert "shell.env" in err
    finally:
        shell_env.chmod(0o644)


def _stage_gitlink(root: Path, relative: str) -> None:
    """Stage `relative` as a gitlink (nested repository / submodule reference).

    `git show :<relative>` cannot read this: the index holds a commit
    pointer for a gitlink, not a blob, so `staged_blob` must convert git's
    non-zero exit into a `SecretScanError` rather than let it leak.
    """
    fake_commit_sha = (uuid.uuid4().hex + uuid.uuid4().hex)[:40]
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "update-index",
            "--add",
            "--cacheinfo",
            f"160000,{fake_commit_sha},{relative}",
        ],
        check=True,
        capture_output=True,
    )


def test_staged_gitlink_errors_without_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Error Handling Contract: `git show` cannot read a staged gitlink; ERROR, no traceback."""
    root = generated_root(tmp_path)
    init_repo(root)
    _stage_gitlink(root, "sub")

    exit_code = run_cli(monkeypatch, root, ["lint-secrets"])

    err = capsys.readouterr().err
    assert exit_code != 0
    assert err.startswith("ERROR:")
    assert "Traceback" not in err
    assert "sub" in err
    assert "git rm -r --cached sub" in err


def test_staged_blob_raises_when_git_show_fails_on_a_gitlink(tmp_path: Path) -> None:
    """Error Handling Contract: `secrets.staged_blob` itself converts the git failure."""
    secrets = import_secrets()
    root = generated_root(tmp_path)
    init_repo(root)
    _stage_gitlink(root, "sub")

    with pytest.raises(secrets.SecretScanError, match="cannot read staged content of sub"):
        secrets.staged_blob(root, "sub")


def test_render_redacts_credential_bearing_finding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Approach step 8: the worked report shape, with a credential-bearing match redacted."""
    root = generated_root(tmp_path)
    init_repo(root)
    line = credential_line()
    stage_text(root, "src/config.py", line)

    exit_code = run_cli(monkeypatch, root, ["lint-secrets"])

    out = capsys.readouterr().out
    assert exit_code == 1
    assert out.splitlines()[0] == "[LINT] secrets in staged content"
    assert "src/config.py:1: AWS access key identifier:" in out
    assert line.strip() not in out


def test_makefile_lint_secrets_is_a_lint_prerequisite() -> None:
    """AC-TEST-005 / AC-FUNC-005: `lint-secrets` is a prerequisite of the `lint` target."""
    makefile_text = (_repo_root() / "Makefile").read_text(encoding="utf-8")

    match = re.search(r"^lint:(.*)$", makefile_text, re.MULTILINE)
    assert match is not None, "no lint target found in Makefile"
    prerequisites = set(match.group(1).split())

    assert "lint-secrets" in prerequisites


def test_makefile_lint_secrets_target_uses_the_scripts_directory_variable() -> None:
    """The lint-secrets recipe names the CLI through a Makefile variable, not an inline path."""
    makefile_text = (_repo_root() / "Makefile").read_text(encoding="utf-8")

    match = re.search(r"^lint-secrets:.*\n((?:\t.*\n?)*)", makefile_text, re.MULTILINE)
    assert match is not None, "no lint-secrets target found in Makefile"
    recipe = match.group(1)

    assert "devcontainer_config.cli" in recipe
    assert ".claude/plugins/devcontainer/scripts" not in recipe


def test_makefile_help_documents_lint_secrets_as_a_host_target() -> None:
    """AC-DOC-001: `make help` gains a row for `make lint-secrets` marked host."""
    makefile_text = (_repo_root() / "Makefile").read_text(encoding="utf-8")

    help_match = re.search(r"^help:.*\n((?:\t.*\n?)*)", makefile_text, re.MULTILINE)
    assert help_match is not None, "no help target found in Makefile"
    help_recipe = help_match.group(1)

    lint_secrets_rows = [line for line in help_recipe.splitlines() if '"make lint-secrets"' in line]
    assert len(lint_secrets_rows) == 1
    assert '"host"' in lint_secrets_rows[0]


def _imported_top_level_modules(tree: ast.Module) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module.split(".")[0])
    return modules


def _is_sys_exit_call(node: ast.AST) -> bool:
    """Whether `node` is a call to `sys.exit`.

    The one definition of that shape, shared by `_sys_exit_call_sites` and every
    assertion that needs the matching `ast.Call` node itself rather than just the
    enclosing function.
    """
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "exit"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "sys"
    )


def _sys_exit_call_sites(tree: ast.Module) -> list[ast.FunctionDef]:
    """Every function definition in `tree` whose body directly calls `sys.exit`."""
    call_sites: list[ast.FunctionDef] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for inner in ast.walk(node):
            if _is_sys_exit_call(inner):
                call_sites.append(node)
                break
    return call_sites


def _exit_call_sites_match_public_entry_points(tree: ast.Module) -> tuple[set[str], set[str]]:
    """The functions that call `sys.exit` and the module's public entry points.

    AC-FUNC-006's invariant, stated in entry-point terms: 'only public
    console entry points exit the process, never a private helper and never
    library code'. Returns the pair (names of functions `_sys_exit_call_sites`
    finds calling `sys.exit`, names of module-level function definitions
    whose name does not begin with an underscore), so a caller can assert
    the two sets are equal instead of comparing either one to a hard-coded
    name literal.
    """
    exit_call_names = {node.name for node in _sys_exit_call_sites(tree)}
    public_entry_point_names = {
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and not node.name.startswith("_")
    }
    return exit_call_names, public_entry_point_names


def test_sys_exit_pin_rejects_a_private_helper_that_exits() -> None:
    """AC-TEST-001: a private helper calling `sys.exit` must not satisfy the pin."""
    source = (
        "import sys\n\ndef main() -> None:\n    pass\n\ndef _helper() -> None:\n    sys.exit(1)\n"
    )
    tree = ast.parse(source)

    exit_call_names, public_entry_point_names = _exit_call_sites_match_public_entry_points(tree)

    assert exit_call_names != public_entry_point_names


def test_sys_exit_pin_rejects_a_public_function_that_never_exits() -> None:
    """AC-TEST-002: a public function that never calls `sys.exit` must not satisfy the pin."""
    source = (
        "import sys\n"
        "\n"
        "def main() -> None:\n"
        "    sys.exit(0)\n"
        "\n"
        "def report() -> None:\n"
        "    print('report')\n"
    )
    tree = ast.parse(source)

    exit_call_names, public_entry_point_names = _exit_call_sites_match_public_entry_points(tree)

    assert exit_call_names != public_entry_point_names


def test_sys_exit_call_sites_are_exactly_the_public_entry_points() -> None:
    """AC-FUNC-006: only cli.py's public entry points call `sys.exit`.

    Each calls it exactly once, as the terminal statement of that entry point's
    function body, and there is at least one such entry point.
    """
    cli_source = _plugin_scripts_module_path("cli.py").read_text(encoding="utf-8")
    cli_tree = ast.parse(cli_source)

    exit_call_names, public_entry_point_names = _exit_call_sites_match_public_entry_points(cli_tree)

    assert exit_call_names == public_entry_point_names
    assert exit_call_names != set()

    entry_points_by_name = {
        node.name: node
        for node in cli_tree.body
        if isinstance(node, ast.FunctionDef) and node.name in exit_call_names
    }
    for name in exit_call_names:
        entry_point = entry_points_by_name[name]
        exit_calls_in_body = [inner for inner in ast.walk(entry_point) if _is_sys_exit_call(inner)]
        assert len(exit_calls_in_body) == 1, name
        last_statement = entry_point.body[-1]
        assert isinstance(last_statement, ast.Expr), name
        assert last_statement.value is exit_calls_in_body[0]


def test_secrets_module_never_calls_sys_exit() -> None:
    """AC-FUNC-006: secrets.py raises SecretScanError and never exits."""
    secrets_source = _plugin_scripts_module_path("secrets.py").read_text(encoding="utf-8")
    secrets_tree = ast.parse(secrets_source)

    assert "sys" not in _imported_top_level_modules(secrets_tree)
    assert _sys_exit_call_sites(secrets_tree) == []


def _defined_or_referenced_identifiers(tree: ast.Module) -> set[str]:
    """Every name a bypass mechanism would need: a parameter, an attribute, or a constant."""
    identifiers: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            identifiers.add(node.id)
        elif isinstance(node, ast.arg):
            identifiers.add(node.arg)
        elif isinstance(node, ast.Attribute):
            identifiers.add(node.attr)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            identifiers.add(node.name)
    return identifiers


def test_cli_module_has_no_allowlist_or_bypass_mechanism() -> None:
    """AC-FUNC-007: no argument, environment variable or marker comment suppresses a finding."""
    source = _plugin_scripts_module_path("cli.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    assert "os" not in _imported_top_level_modules(tree)

    forbidden_substrings = ("allowlist", "allow_list", "ignorelist", "ignore_list", "suppress")
    identifiers = _defined_or_referenced_identifiers(tree)
    offending = {
        identifier
        for identifier in identifiers
        if any(bad in identifier.lower() for bad in forbidden_substrings)
    }
    assert offending == set()


def test_lint_secrets_help_states_exit_code_contract(capsys: pytest.CaptureFixture[str]) -> None:
    """AC-DOC-002: the `lint-secrets --help` text states the exit-code contract."""
    cli = import_cli()

    with pytest.raises(SystemExit) as exc_info:
        cli.main(["lint-secrets", "--help"])

    out = capsys.readouterr().out
    assert exc_info.value.code == 0
    assert "exits 1" in out.lower()
    assert "no ignore list" in out.lower()


def test_cli_module_docstring_states_exit_code_and_no_ignore_list() -> None:
    """AC-DOC-002: the cli.py module docstring states the exit-code contract."""
    cli = import_cli()
    doc = (cli.__doc__ or "").lower()

    assert "exit 1" in doc or "sys.exit" in doc
    assert "ignore" in doc
