"""Snapshot and correspondence tests for `make help` and `devsecret --help` (E4-F4-S1-T1).

Spec Section 14 specifies both help surfaces verbatim (14.1 for `make help`,
14.2 for `devsecret --help`) and AC-14.1 requires a snapshot test that fails
when either drifts. A snapshot alone only holds text steady; it says nothing
about whether the text is true. This file adds two more assertions on top of
the two snapshots, so `make help` cannot describe a `Makefile` that has moved
out from under it:

- `test_every_advertised_target_is_defined` (AC-FUNC-002): every target named
  in a help row is a real `Makefile` target.
- `test_every_phony_target_is_advertised_prerequisite_or_declared`
  (AC-FUNC-003): every `.PHONY` target is advertised, is a (direct)
  prerequisite of an advertised target, or is named in
  `tests/data/help-unadvertised.txt` with a non-empty reason. The declaration
  file is what lets `hooks-uninstall` -- part of the bypass surface Section
  4.6.1 denies -- and `help` itself -- the target that renders this document,
  which cannot sensibly advertise itself -- stay unadvertised without going
  undetected by this correspondence check.

Both snapshots are compared against the real, live output, never against a
copy embedded in this file: `_run_make_help` shells out to the real `make
help`, and `_devsecret_help_text` calls the real
`devcontainer_config.cli.main_devsecret(["--help"])`, the same console entry
point `pyproject.toml` installs as `devsecret`. `make help`'s output carries
two machine-dependent interpolations -- the checkout's own directory name
(the "Project: ..." banner) and the two docker context names `config.env`
supplies -- so `_normalize_make_help_output` replaces exactly those three
values (read the same way the `Makefile` itself reads them:
`$(notdir $(CURDIR))`, and `config.env` sourced in a subshell the same way
`$(LOCAL_CONTEXT)`/`$(REMOTE_CONTEXT)` are) with fixed placeholders before
comparison, so `tests/data/make-help.txt` is identical on every machine and
in CI regardless of the checkout's folder name or which docker context is
active.

AC-TEST-003, AC-TEST-004 and AC-TEST-005 each require a demonstrated failure
path. Every one of those is exercised here as a real, permanent test against
an in-memory mutation (a drifted string built from the real fixture's own
text, a copy of the real declarations file with one reason blanked out, or a
synthetic help row naming a target that does not exist) -- never by editing
`Makefile`, `tests/data/make-help.txt`, `tests/data/devsecret-help.txt` or
`tests/data/help-unadvertised.txt` on disk. `test_module_never_writes_to_a_fixture_path`
(AC-TEST-006) pins that this module holds no write call against any of the
three fixture variables it reads, by name, so a future edit that starts
"self-healing" a fixture on drift is caught here rather than discovered by a
snapshot that silently stopped meaning anything.

`_makefile_text` is imported from `tests/conftest.py` and `_phony_targets`
from `tests/test_makefile_contract.py` rather than redefined here, the same
reuse `tests/test_makefile_contract.py`'s own docstring documents for
`_resolve_make_refs`: both already parse the one `Makefile` this file also
parses, and a second, private copy of either risked drifting from its
sibling the moment one of the two Makefile sections it reads changed shape.
"""

from __future__ import annotations

import contextlib
import difflib
import io
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import _makefile_text
from devcontainer_config import cli as devcontainer_cli
from devcontainer_config.repo import find_root
from test_makefile_contract import _phony_targets

FIXTURES_DIR = Path(__file__).resolve().parent / "data"
MAKE_HELP_FIXTURE = FIXTURES_DIR / "make-help.txt"
DEVSECRET_HELP_FIXTURE = FIXTURES_DIR / "devsecret-help.txt"
UNADVERTISED_FILE = FIXTURES_DIR / "help-unadvertised.txt"

# A bounded safety net for the one real subprocess this file shells out to
# (`make help`), not a readiness wait: `make help` prints and returns, it
# never blocks on anything. Configurable per CLAUDE.md's no-hardcoded-timeout
# rule, following the pattern `tests/test_makefile_contract.py` already uses.
_MAKE_HELP_TIMEOUT_SECONDS = float(os.environ.get("HELP_SNAPSHOT_TEST_TIMEOUT_SECONDS", "30"))

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

_REPO_DIR_PLACEHOLDER = "<REPO_DIR>"
_LOCAL_CONTEXT_PLACEHOLDER = "<LOCAL_DOCKER_CONTEXT>"
_REMOTE_CONTEXT_PLACEHOLDER = "<REMOTE_DOCKER_CONTEXT>"

_DECLARATION_LINE_RE = re.compile(r"^([A-Za-z0-9_.-]+):\s*(.*)$")

# A target definition line, e.g. "validate: lint test" or "help:" -- the
# name, then a bare colon not immediately followed by "=" (which would make
# it a "NAME:= value" or "NAME := value" variable assignment instead of a
# target). Recipe lines are tab-indented, so they never match at `^`.
_TARGET_LINE_RE = re.compile(r"^([A-Za-z0-9_.-]+):(?!=)(.*)$", re.MULTILINE)


def _strip_ansi(text: str) -> str:
    """`text` with every SGR escape sequence (`\\x1b[...m`) removed."""
    return _ANSI_RE.sub("", text)


def _defined_targets(makefile_text: str) -> set[str]:
    """Every real target the `Makefile` defines, `.PHONY` itself excluded.

    `.PHONY` is a directive, not a target `make` can build, so it is
    filtered out rather than treated as one more name a help row could
    legitimately advertise.
    """
    names = {match.group(1) for match in _TARGET_LINE_RE.finditer(makefile_text)}
    names.discard(".PHONY")
    return names


def _target_prerequisites_map(makefile_text: str) -> dict[str, set[str]]:
    """`{target: {its own direct prerequisites}}`, from each target's own definition line."""
    prerequisites: dict[str, set[str]] = {}
    for match in _TARGET_LINE_RE.finditer(makefile_text):
        name, rest = match.group(1), match.group(2)
        if name == ".PHONY":
            continue
        prerequisites.setdefault(name, set()).update(rest.split())
    return prerequisites


def _target_names_from_column(column: str) -> list[str]:
    """Every target name a help row's first column names.

    `"make start / stop"` -> `["start", "stop"]`; `"make rename NAME=x"` ->
    `["rename"]`; a column that does not open with `"make "` (an `OPTIONS` or
    `PREREQUISITES` row) -> `[]`.
    """
    if not column.startswith("make "):
        return []
    remainder = column[len("make ") :]
    names = []
    for part in remainder.split("/"):
        stripped_part = part.strip()
        if stripped_part:
            names.append(stripped_part.split()[0])
    return names


def _advertised_target_rows(help_output: str) -> list[tuple[str, list[str]]]:
    """`(row text, target names)` for every `help` row that names at least one `make` target.

    A row is any ANSI-stripped line indented by (at least) two spaces whose
    first `\\s{2,}`-delimited column opens with `"make "`; the fixed-width
    `printf` format this repo's `help` recipe uses always pads that first
    column with two or more trailing spaces before the next column starts,
    so splitting on runs of two-or-more whitespace characters reliably
    separates the target-name column from the "both/host/remote/local"
    column and the free-text description that follows it.
    """
    rows: list[tuple[str, list[str]]] = []
    for line in help_output.splitlines():
        clean = _strip_ansi(line).rstrip()
        stripped = clean.strip()
        if not clean.startswith("  ") or not stripped:
            continue
        columns = re.split(r"\s{2,}", stripped)
        names = _target_names_from_column(columns[0])
        if names:
            rows.append((clean, names))
    return rows


def _advertised_targets(help_output: str) -> set[str]:
    """Every target name any row of `help_output` advertises."""
    names: set[str] = set()
    for _, row_names in _advertised_target_rows(help_output):
        names.update(row_names)
    return names


def _assert_undefined_targets(help_output: str, defined_targets: set[str]) -> None:
    """AC-FUNC-002 / AC-TEST-005: fail naming the row and the target for any undefined target."""
    problems = [
        f"row {row!r} advertises undefined target {name!r}"
        for row, names in _advertised_target_rows(help_output)
        for name in names
        if name not in defined_targets
    ]
    assert not problems, "\n".join(problems)


def _load_unadvertised_declarations(path: Path) -> dict[str, str]:
    """`{target: reason}` from `path`'s `<target>: <reason>` lines. Blank and `#` lines skip.

    AC-TEST-004: an entry whose reason is empty (including a completely bare
    `target:` line) fails here, naming the file, the line number and the
    target -- this is the one place a declaration ever gets parsed, so a
    caller cannot silently accept an empty reason by reading the file some
    other way.
    """
    declarations: dict[str, str] = {}
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = _DECLARATION_LINE_RE.match(line)
        assert match is not None, (
            f"{path}:{line_number}: expected '<target>: <reason>', got {raw_line!r}"
        )
        target, reason = match.group(1), match.group(2).strip()
        assert reason, f"{path}:{line_number}: entry {target!r} has an empty reason"
        declarations[target] = reason
    return declarations


def _assert_phony_targets_accounted_for(
    phony_targets: set[str],
    advertised_targets: set[str],
    prerequisites: dict[str, set[str]],
    declarations: dict[str, str],
) -> None:
    """AC-FUNC-003: every `.PHONY` target is advertised, a prerequisite of one, or declared."""
    reachable = set(advertised_targets)
    for owner, owned_prerequisites in prerequisites.items():
        if owner in advertised_targets:
            reachable.update(owned_prerequisites)
    unaccounted = sorted(
        target for target in phony_targets if target not in reachable and target not in declarations
    )
    assert not unaccounted, (
        "these .PHONY targets are neither advertised, a prerequisite of an advertised "
        f"target, nor declared with a reason in tests/data/help-unadvertised.txt: {unaccounted}"
    )


def _docker_context_values(config_path: Path) -> tuple[str, str]:
    """`(LOCAL_DOCKER_CONTEXT, REMOTE_DOCKER_CONTEXT)`, read the way the `Makefile` reads them.

    Mirrors `LOCAL_CONTEXT`/`REMOTE_CONTEXT` in the `Makefile`
    (`$(shell source $(CONFIG) && echo $$...)`) exactly: sourcing the
    committed `config.env` in a subshell, never docker, AWS or the network,
    so computing these values here stays inside `make help`'s own
    no-docker/no-AWS/no-network contract (AC-10.14, Definition of Ready).
    """
    bash_path = shutil.which("bash")
    assert bash_path is not None, "bash must be installed on the machine running this test suite"
    script = (
        f'source "{config_path}" && printf "%s\\t%s" '
        '"$LOCAL_DOCKER_CONTEXT" "$REMOTE_DOCKER_CONTEXT"'
    )
    result = subprocess.run(
        [bash_path, "-c", script],
        capture_output=True,
        text=True,
        check=True,
        timeout=_MAKE_HELP_TIMEOUT_SECONDS,
    )
    local_context, _, remote_context = result.stdout.partition("\t")
    assert local_context and remote_context, (
        f"{config_path} did not yield both LOCAL_DOCKER_CONTEXT and REMOTE_DOCKER_CONTEXT"
    )
    return local_context, remote_context


def _normalize_make_help_output(
    raw_output: str, *, repo_dir: str, local_context: str, remote_context: str
) -> str:
    """`raw_output` with ANSI stripped and every machine-dependent value replaced by a placeholder.

    `repo_dir` is `$(notdir $(CURDIR))`'s value. It opens the banner line,
    which the `help` recipe now derives rather than printing a hardcoded
    product name beside it.

    That hardcoded name is why this substitution was once anchored to a
    literal `"Project: "` label: with a product name baked into the recipe, a
    blanket replacement would also have rewritten that name whenever a
    checkout happened to live in a directory called the same thing, masking
    real drift. The recipe no longer carries such a name -- the banner and
    the project label were the same derived value printed twice -- so the
    anchor guards nothing and is dropped. Anchoring the replacement to the
    start of the line keeps it from matching the word elsewhere in the help
    text.

    `local_context` / `remote_context` are `$(LOCAL_CONTEXT)` /
    `$(REMOTE_CONTEXT)`, each of which only ever appears inside a single pair
    of parentheses in the `ENGINE` section.
    """
    text = _strip_ansi(raw_output)
    text = re.sub(
        rf"^{re.escape(repo_dir)}(?= devcontainer control\.)",
        _REPO_DIR_PLACEHOLDER,
        text,
        flags=re.MULTILINE,
    )
    text = text.replace(f"({local_context})", f"({_LOCAL_CONTEXT_PLACEHOLDER})")
    text = text.replace(f"({remote_context})", f"({_REMOTE_CONTEXT_PLACEHOLDER})")
    return text


def _run_make_help(repo_root: Path) -> str:
    """The real, raw (ANSI-carrying) stdout of `make help`, run from `repo_root`."""
    make_path = shutil.which("make")
    assert make_path is not None, "make must be installed on the machine running this test suite"
    result = subprocess.run(
        [make_path, "help"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
        timeout=_MAKE_HELP_TIMEOUT_SECONDS,
    )
    return result.stdout


def _devsecret_help_text() -> str:
    """The real stdout of `devcontainer_config.cli.main_devsecret(["--help"])`.

    `main_devsecret` parses `argv` before ever building a catalog client
    (spec Section 4.3): `--help` is handled inside `argparse.parse_args`,
    which prints and calls `parser.exit()` (raising `SystemExit(0)`) before
    that client is ever constructed, so this never touches a real backend,
    docker, AWS or the network.
    """
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer), pytest.raises(SystemExit) as exc_info:
        devcontainer_cli.main_devsecret(["--help"])
    assert exc_info.value.code == 0, f"devsecret --help must exit 0, got {exc_info.value.code!r}"
    return buffer.getvalue()


def _compare_snapshot(expected: str, actual: str, fixture_name: str) -> None:
    """Raise `AssertionError` with a unified diff naming the differing lines if they differ.

    Never writes `fixture_name`: on a mismatch this only builds a diff
    string for the failure message (AC-TEST-003), it does not repair the
    fixture, because a snapshot that rewrites itself on drift asserts
    nothing (AC-TEST-006).
    """
    if actual == expected:
        return
    diff = "".join(
        difflib.unified_diff(
            expected.splitlines(keepends=True),
            actual.splitlines(keepends=True),
            fromfile=f"{fixture_name} (expected)",
            tofile=f"{fixture_name} (actual)",
        )
    )
    raise AssertionError(f"{fixture_name} drifted from its fixture:\n{diff}")


@pytest.fixture(scope="module")
def repo_root() -> Path:
    return find_root(Path(__file__).resolve().parent)


@pytest.fixture(scope="module")
def raw_help_output(repo_root: Path) -> str:
    return _run_make_help(repo_root)


@pytest.fixture(scope="module")
def docker_contexts(repo_root: Path) -> tuple[str, str]:
    return _docker_context_values(repo_root / ".devcontainer" / "remote-docker" / "config.env")


@pytest.fixture(scope="module")
def normalized_help_output(
    repo_root: Path, raw_help_output: str, docker_contexts: tuple[str, str]
) -> str:
    local_context, remote_context = docker_contexts
    return _normalize_make_help_output(
        raw_help_output,
        repo_dir=repo_root.name,
        local_context=local_context,
        remote_context=remote_context,
    )


def test_every_advertised_target_is_defined(raw_help_output: str) -> None:
    """AC-FUNC-002: `make help` advertises no target the `Makefile` does not define."""
    defined = _defined_targets(_makefile_text())
    assert defined, "no targets parsed out of the Makefile at all"
    _assert_undefined_targets(raw_help_output, defined)


def test_every_phony_target_is_advertised_prerequisite_or_declared(raw_help_output: str) -> None:
    """AC-FUNC-003: every `.PHONY` target is advertised, a prerequisite, or declared."""
    makefile_text = _makefile_text()
    phony = _phony_targets(makefile_text)
    advertised = _advertised_targets(raw_help_output)
    prerequisites = _target_prerequisites_map(makefile_text)
    declarations = _load_unadvertised_declarations(UNADVERTISED_FILE)
    _assert_phony_targets_accounted_for(phony, advertised, prerequisites, declarations)


def test_unadvertised_file_declares_hooks_uninstall_with_the_bypass_surface_reason() -> None:
    """AC-FUNC-004: `help-unadvertised.txt` names `hooks-uninstall` with a bypass-surface reason."""
    declarations = _load_unadvertised_declarations(UNADVERTISED_FILE)
    assert "hooks-uninstall" in declarations
    reason = declarations["hooks-uninstall"].lower()
    assert "bypass" in reason


def test_make_help_output_matches_its_snapshot(normalized_help_output: str) -> None:
    """AC-TEST-001: normalized `make help` output is byte-identical to its fixture."""
    expected = MAKE_HELP_FIXTURE.read_text(encoding="utf-8")
    _compare_snapshot(expected, normalized_help_output, "tests/data/make-help.txt")


def test_devsecret_help_output_matches_its_snapshot() -> None:
    """AC-TEST-002: `devsecret --help` output is byte-identical to its fixture."""
    expected = DEVSECRET_HELP_FIXTURE.read_text(encoding="utf-8")
    actual = _devsecret_help_text()
    _compare_snapshot(expected, actual, "tests/data/devsecret-help.txt")


def test_make_help_snapshot_reports_a_unified_diff_naming_the_changed_line() -> None:
    """AC-TEST-003: one changed help row fails the comparison, naming the changed line in a diff.

    Demonstrated against an in-memory mutation of the real fixture's own
    text, never against the fixture file on disk (AC-TEST-006): the fixture
    path is only ever opened for reading, here and everywhere else in this
    module.
    """
    expected = MAKE_HELP_FIXTURE.read_text(encoding="utf-8")
    marker = "Restart in place. Fixes a wedged container without rebuilding anything."
    assert marker in expected, "fixture text no longer carries the row this test mutates"
    drifted = expected.replace(marker, "Restart in place, but differently now.")
    assert drifted != expected
    with pytest.raises(AssertionError) as exc_info:
        _compare_snapshot(expected, drifted, "tests/data/make-help.txt")
    message = str(exc_info.value)
    assert "Restart in place, but differently now." in message


def test_unadvertised_declaration_with_an_empty_reason_fails_naming_the_entry(
    tmp_path: Path,
) -> None:
    """AC-TEST-004: an entry with an empty reason fails, naming that entry.

    Built from a `tmp_path` copy of the real declarations file with its
    first entry's reason blanked out, so this demonstrates the failure
    against the real declaration shape without ever writing to
    `tests/data/help-unadvertised.txt` itself.
    """
    real_lines = UNADVERTISED_FILE.read_text(encoding="utf-8").splitlines()
    blanked_target: str | None = None
    mutated_lines: list[str] = []
    for line in real_lines:
        stripped = line.strip()
        if blanked_target is None and stripped and not stripped.startswith("#"):
            blanked_target, _, _ = stripped.partition(":")
            blanked_target = blanked_target.strip()
            mutated_lines.append(f"{blanked_target}:")
            continue
        mutated_lines.append(line)
    assert blanked_target is not None, (
        "help-unadvertised.txt has no real entry for this test to blank"
    )
    mutated_file = tmp_path / "help-unadvertised.txt"
    mutated_file.write_text("\n".join(mutated_lines) + "\n", encoding="utf-8")

    with pytest.raises(AssertionError) as exc_info:
        _load_unadvertised_declarations(mutated_file)
    assert blanked_target in str(exc_info.value)


def test_advertised_undefined_target_fails_naming_row_and_target() -> None:
    """AC-TEST-005: a help row naming an undefined target fails, naming the row and the target."""
    fake_target = "totally-not-a-real-makefile-target"
    fake_row = f"  make {fake_target}   host   Does not exist in the Makefile.\n"
    defined = _defined_targets(_makefile_text())
    assert fake_target not in defined

    with pytest.raises(AssertionError) as exc_info:
        _assert_undefined_targets(fake_row, defined)
    message = str(exc_info.value)
    assert fake_target in message
    assert fake_row.strip() in message


def test_normalize_strips_ansi_and_substitutes_dynamic_values() -> None:
    """`_normalize_make_help_output` removes ANSI and swaps every machine-dependent value."""
    repo_dir = "some-other-checkout"
    local_context = "a-local-ctx"
    remote_context = "a-remote-ctx"
    raw = (
        f"\033[1m{repo_dir}\033[0m devcontainer control.   Backend follows the context.\n"
        "  \033[1;36mmake local             \033[0m host    "
        f"Point docker and VS Code at the local engine ({local_context}). Nothing.\n"
        "  \033[1;36mmake remote            \033[0m host    "
        f"Point them at the EC2 engine ({remote_context}), refreshing.\n"
    )
    normalized = _normalize_make_help_output(
        raw, repo_dir=repo_dir, local_context=local_context, remote_context=remote_context
    )
    assert "\033[" not in normalized
    assert normalized.startswith(f"{_REPO_DIR_PLACEHOLDER} devcontainer control.")
    assert f"({_LOCAL_CONTEXT_PLACEHOLDER})" in normalized
    assert f"({_REMOTE_CONTEXT_PLACEHOLDER})" in normalized
    assert "some-other-checkout" not in normalized
    assert "a-local-ctx" not in normalized
    assert "a-remote-ctx" not in normalized


def test_normalize_only_substitutes_the_repo_dir_at_the_banner() -> None:
    """The replacement is anchored, so the same word elsewhere in help text survives.

    Dropping the old `"Project: "` anchor made a blanket replacement tempting.
    A blanket one would corrupt any help line that legitimately mentions the
    project's name, so the substitution is anchored to the start of the banner
    line and this pins that.
    """
    repo_dir = "widgets"
    raw = (
        f"{repo_dir} devcontainer control.   Backend follows the active docker context.\n"
        f"  make thing    both    Does a thing to the {repo_dir} workspace.\n"
    )
    normalized = _normalize_make_help_output(
        raw, repo_dir=repo_dir, local_context="l", remote_context="r"
    )
    assert normalized.startswith(f"{_REPO_DIR_PLACEHOLDER} devcontainer control.")
    assert f"the {repo_dir} workspace" in normalized, (
        "the anchored substitution must leave a later, legitimate mention intact"
    )


_FIXTURE_VARIABLE_NAMES = ("MAKE_HELP_FIXTURE", "DEVSECRET_HELP_FIXTURE", "UNADVERTISED_FILE")


def test_module_never_writes_to_a_fixture_path() -> None:
    """AC-TEST-006: this module never calls a write method on a fixture-path variable.

    `test_unadvertised_declaration_with_an_empty_reason_fails_naming_the_entry`
    does call `.write_text(` -- on a `tmp_path` file, never on
    `UNADVERTISED_FILE` itself -- so this checks each fixture *variable name*
    for a write call rather than searching for the string `"write_text("`
    anywhere in the module, which that other, legitimate call would trip.
    """
    source = Path(__file__).read_text(encoding="utf-8")
    for name in _FIXTURE_VARIABLE_NAMES:
        assert f"{name}.write" not in source, f"{name} must never be written to by this test module"
