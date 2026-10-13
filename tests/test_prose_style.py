"""Tests for the prose gate: `.devcontainer/lint-prose.py` and its wiring.

Two layers. The unit layer runs the checker as a subprocess over temp
files, asserting on real exit codes and real output -- never by importing
it. The checker is executed, never imported, anywhere in this file: its
path is resolved from the repository root and handed to `sys.executable`,
which is why this module needs no deferred-import helper -- a missing or
broken checker surfaces as a failure inside a test body, not as a
collection error, the property the deferred-import convention (see
`tests/test_repo.py`'s module docstring) exists to keep.

The repo layer replicates the Makefile's `PROSE_FILES` derivation in
Python (the same `find` expression: every `*.md` under the checkout, minus
`LINT_EXCLUDES`'s four trees and the prose gate's own three excludes) and
runs the checker over the live tree, asserting zero findings. That
assertion is RED today by design -- the repository's own docs carry em
dashes and hard-wrapped paragraph walls the gate refuses -- and turns
green when the documentation restyle lands; its failure message carries
the checker's own file:line report so the restyle is actionable line by
line. The Makefile wiring itself (composition, `.PHONY`, the
`PROSE_FILES` excludes, the checker path) is pinned in
`tests/test_makefile_contract.py`, the established home for
lint-composition pins (`test_validate_requires_lint_and_test`,
`test_lint_secrets_recipe_forwards_range_variable_to_the_cli`).

Every dash literal the unit fixtures need is spelled as an escape
(`\\u2014`, `\\u2013`) so the source states the code point it means; the
escape sequence and the character can never be told apart by a reader.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from devcontainer_config.repo import find_root

CHECKER_RELATIVE = Path(".devcontainer") / "lint-prose.py"

EM_DASH = "\u2014"
EN_DASH = "\u2013"

# A bounded safety net for the subprocesses this file runs, not a readiness
# wait: the checker reads files and prints, it never blocks on anything.
# Configurable per CLAUDE.md's no-hardcoded-timeouts rule, following the
# pattern `tests/test_help_snapshot.py` already uses.
CHECKER_TIMEOUT_SECONDS = float(os.environ.get("PROSE_CHECK_TEST_TIMEOUT_SECONDS", "30"))


def _checker_path() -> Path:
    """The prose checker, resolved from this repository's root."""
    return find_root(Path(__file__).resolve().parent) / CHECKER_RELATIVE


def _run_checker(*files: Path) -> subprocess.CompletedProcess[str]:
    """Run the checker over `files` exactly as `make lint-prose` would."""
    return subprocess.run(
        [sys.executable, str(_checker_path()), *(str(path) for path in files)],
        capture_output=True,
        text=True,
        check=False,
        timeout=CHECKER_TIMEOUT_SECONDS,
    )


def _write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Rule D: dashes
# ---------------------------------------------------------------------------


def test_em_dash_fails_naming_file_line_and_offending_line(tmp_path: Path) -> None:
    path = _write(tmp_path, "em.md", f"a clause{EM_DASH}split here\n")
    result = _run_checker(path)
    assert result.returncode == 1
    assert f"{path}: line 1:" in result.stderr
    assert "em dash (U+2014)" in result.stderr
    assert f"a clause{EM_DASH}split here" in result.stderr


def test_en_dash_fails_naming_file_line_and_offending_line(tmp_path: Path) -> None:
    path = _write(tmp_path, "en.md", f"years 1995{EN_DASH}1999\n")
    result = _run_checker(path)
    assert result.returncode == 1
    assert f"{path}: line 1:" in result.stderr
    assert "en dash (U+2013)" in result.stderr
    assert f"years 1995{EN_DASH}1999" in result.stderr


def test_dash_inside_a_fenced_block_passes(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "fenced.md",
        f"Before the fence:\n\n```sh\necho one{EM_DASH}two\n```\n",
    )
    result = _run_checker(path)
    assert result.returncode == 0, result.stderr
    assert "1 file(s) pass the prose rules" in result.stdout


def test_dash_inside_a_table_line_fails(tmp_path: Path) -> None:
    """Table lines are exempt from the paragraph rule, never from Rule D."""
    path = _write(
        tmp_path,
        "table.md",
        f"| Column A | Column B |\n| --- | --- |\n| one{EM_DASH}two | three |\n",
    )
    result = _run_checker(path)
    assert result.returncode == 1
    assert f"{path}: line 3:" in result.stderr
    assert "em dash (U+2014)" in result.stderr
    assert "paragraph run" not in result.stderr, (
        "a table line must never be charged to the paragraph rule"
    )


# ---------------------------------------------------------------------------
# Rule P: paragraph walls
# ---------------------------------------------------------------------------


def test_single_prose_line_passes_as_a_lead_in(tmp_path: Path) -> None:
    path = _write(tmp_path, "leadin.md", "A single lead-in line.\n")
    result = _run_checker(path)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "  1 file(s) pass the prose rules\n"


def test_two_consecutive_prose_lines_fail_naming_the_first_line(tmp_path: Path) -> None:
    path = _write(tmp_path, "wall.md", "First wall line.\nSecond wall line.\n")
    result = _run_checker(path)
    assert result.returncode == 1
    assert f"{path}: line 1:" in result.stderr
    assert "'First wall line.'" in result.stderr


def test_paragraphs_separated_by_a_blank_line_pass(tmp_path: Path) -> None:
    path = _write(tmp_path, "blank.md", "One paragraph.\n\nAnother paragraph.\n")
    result = _run_checker(path)
    assert result.returncode == 0, result.stderr


def test_list_item_run_passes(tmp_path: Path) -> None:
    """Consecutive list items are structure, not a paragraph wall."""
    cases = ("- one\n- two\n- three\n", "1. one\n2. two\n3. three\n")
    files = [_write(tmp_path, f"list{index}.md", text) for index, text in enumerate(cases)]
    result = _run_checker(*files)
    assert result.returncode == 0, result.stderr
    assert "2 file(s) pass the prose rules" in result.stdout


def test_heading_lead_in_and_fence_pass(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "mixed.md",
        f"# Title\n\nLead-in sentence:\n\n```text\nwall{EM_DASH}of{EM_DASH}dashes\n```\n",
    )
    result = _run_checker(path)
    assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------------------
# Exit codes and output conventions (lint-json.py's register)
# ---------------------------------------------------------------------------


def test_exit_codes_and_success_summary_go_to_stdout(tmp_path: Path) -> None:
    clean = _write(tmp_path, "clean.md", "The only line.\n")
    dirty = _write(tmp_path, "dirty.md", f"broken{EM_DASH}line\n")
    clean_run = _run_checker(clean)
    assert clean_run.returncode == 0
    assert clean_run.stdout == "  1 file(s) pass the prose rules\n"
    dirty_run = _run_checker(dirty)
    assert dirty_run.returncode == 1
    assert dirty_run.stdout == ""


def test_unreadable_file_fails_with_lint_json_wording(tmp_path: Path) -> None:
    """A path that cannot be read is a finding naming it, exit 1 (not a traceback)."""
    missing = tmp_path / "missing.md"
    result = _run_checker(missing)
    assert result.returncode == 1
    assert f"{missing}: could not read:" in result.stderr


# ---------------------------------------------------------------------------
# Repo layer: the live tree, through the derivation make would use
# ---------------------------------------------------------------------------


def _repo_prose_files() -> list[Path]:
    """The live `PROSE_FILES` set, replicated from the Makefile's find.

    Same selection as
    `find . -name '*.md' $(LINT_EXCLUDES) -not -path './.agents/skills/*'
    -not -name 'CLAUDE.md' -not -path './tests/*'
    -not -path '*/.terragrunt-cache/*' -not -path './.pytest_cache/*'
    -not -path './.venv/*'`:
    every markdown file under the checkout, minus the four `LINT_EXCLUDES`
    trees (`.git`, `devbench`, `node_modules`, `repos`), the prose gate's
    own three excludes (the `.agents/skills` roster, any `CLAUDE.md`, and
    `tests/`), and the generated caches -- `.terragrunt-cache` copies of
    the repo's docs that terragrunt refreshes on its own schedule, and
    `.pytest_cache` whose README pytest itself writes at session start
    (the CI run creates it before this test executes). The make-side find
    excludes both for the same reason.
    Returned relative to the repository root, the shape make hands the
    checker.
    """
    root = find_root(Path(__file__).resolve().parent)
    excluded_top_dirs = {
        ".git",
        "devbench",
        "node_modules",
        "repos",
        "tests",
        ".pytest_cache",
        ".venv",
    }
    files: list[Path] = []
    for path in sorted(root.rglob("*.md")):
        relative = path.relative_to(root)
        parts = relative.parts
        if parts[0] in excluded_top_dirs:
            continue
        if ".terragrunt-cache" in parts:
            continue
        if parts[0] == ".agents" and len(parts) > 1 and parts[1] == "skills":
            continue
        if relative.name == "CLAUDE.md":
            continue
        files.append(relative)
    return files


def test_repo_prose_files_pass_the_gate() -> None:
    """The repository's own prose files pass the gate. RED today, by design.

    The tree carries em dashes and hard-wrapped paragraph walls the gate
    refuses; this assertion turns green when the documentation restyle
    lands. The failure message carries the checker's own report so every
    finding is actionable as file:line.
    """
    root = find_root(Path(__file__).resolve().parent)
    files = _repo_prose_files()
    assert files, "the PROSE_FILES derivation resolved to nothing; the walk is broken"
    result = _run_checker(*(root / relative for relative in files))
    assert result.returncode == 0, (
        "the repository's prose files fail the gate: rewrite every dash the "
        "report names, reflow every paragraph wall it names, then re-run:\n"
        f"{result.stderr}"
    )
