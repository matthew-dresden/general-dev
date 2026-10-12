"""Consistency check: PRIVATE_FILES agrees between the Makefile and repo.py.

Two independent copies of the same fact exist today: the Makefile primitive
that `lint-private` and `make init` key off (spec Section 3.3), and the
`devcontainer_config.repo.PRIVATE_FILES` tuple that `render` and `verify` key
off (spec Section 1.7). Nothing enforces they agree. A file added to one
copy and not the other is a silent failure either way: `lint-private` ends
up guarding a file the renderer never writes, or the renderer writes a file
`lint-private` will happily let someone commit identity or secrets under.
This module turns that drift into a test failure (spec Section 3.5, the
Consistency suite of Section 10.2) instead of a runtime surprise.

The Makefile side is obtained by asking `make` to resolve `PRIVATE_FILES`
and print it, not by matching the text of the `?=` assignment, so an
override supplied via the environment (which `?=` respects) is reflected
here exactly as `lint-private` and `make init` would see it. The extraction
below injects a throwaway target that `include`s the real Makefile and
echoes the resolved variable, rather than reading the database with
`make -p`, because `-p` prints every variable make knows about -- including
the whole process environment. This suite ran, at least once, in a sandbox
whose environment held a live secret; a helper that shells out to `-p` and
folds its output into an assertion message would have been one flaky
comparison away from putting that secret in a pytest failure log.
"""

from __future__ import annotations

import re
import os
import subprocess
import tempfile
import uuid
from pathlib import Path

import pytest
from devcontainer_config import repo

_PRINT_TARGET = "print-private-files-under-test"

_MAKE_FRAGMENT_TEMPLATE = """\
include {makefile}
ifndef PRIVATE_FILES
$(error PRIVATE_FILES is not defined in {makefile})
endif
{target}:
\t@echo $(PRIVATE_FILES)
"""



def _make_environment() -> dict[str, str]:
    """The environment for a `make` this suite invokes itself.

    `make test` runs pytest, so any `make` a test spawns is a sub-make: it
    inherits MAKEFLAGS and MAKELEVEL and, from GNU make 4.x onward, announces
    itself with "make[1]: Entering directory ..." on stdout. Those lines are
    not part of what the command prints for an operator running it from a
    shell, which is what these assertions are about, and they land in the
    middle of the output the extraction parses.

    Dropping the two variables makes the child a top-level make again, so the
    suite observes the same output an operator does whether or not it was
    started through `make`. This is why the failure appeared only in CI: the
    macOS make in the developer path is 3.81, which does not print the banner.
    """
    environment = dict(os.environ)
    for inherited in ("MAKEFLAGS", "MAKELEVEL", "MFLAGS"):
        environment.pop(inherited, None)
    return environment


class MakeInvocationError(RuntimeError):
    """Raised when asking make for the resolved PRIVATE_FILES value fails.

    AC-FUNC-006: a failed invocation must not be allowed to degenerate into
    an empty tuple. An empty Makefile-side tuple would make the equality
    assertion below either trivially false (against a non-empty
    `repo.PRIVATE_FILES`) or, on a fixture with an equally-empty Python
    side, trivially and misleadingly true. Naming the command and its exit
    code here means the failure is diagnosed at the extraction, not
    mistaken for a real drift between the two sources.
    """


def _repo_root() -> Path:
    """The repository root, resolved the same way test_makefile_contract.py does."""
    return repo.find_root(Path(__file__).resolve().parent)


def _makefile_private_files(makefile_dir: Path) -> tuple[str, ...]:
    """The resolved PRIVATE_FILES value from the Makefile in makefile_dir.

    Builds a throwaway fragment that includes the target Makefile and adds
    one target that echoes $(PRIVATE_FILES) once make has finished
    resolving it (an override from the environment included, since `?=`
    respects one), then runs that target with
    `make -f <fragment> <target>`. Raises MakeInvocationError, naming the
    command and its exit code, if that invocation does not succeed --
    which also covers a Makefile that never defines PRIVATE_FILES at all,
    since the fragment's `ifndef` guard turns that into a make error rather
    than a silently empty echo.
    """
    makefile = makefile_dir / "Makefile"
    fragment_text = _MAKE_FRAGMENT_TEMPLATE.format(makefile=makefile, target=_PRINT_TARGET)
    with tempfile.NamedTemporaryFile(
        "w", suffix=".mk", delete=False, encoding="utf-8"
    ) as fragment_file:
        fragment_file.write(fragment_text)
        fragment_path = Path(fragment_file.name)
    command = ["make", "-f", str(fragment_path), _PRINT_TARGET]
    try:
        completed = subprocess.run(
            command,
            cwd=makefile_dir,
            capture_output=True,
            text=True,
            check=True,
            env=_make_environment(),
        )
    except FileNotFoundError as exc:
        raise MakeInvocationError(
            f"ERROR: '{' '.join(command)}' failed: make is not installed\n"
            "The extraction needs the make binary on PATH to resolve "
            "PRIVATE_FILES the same way lint-private and make init do.\n"
            "Install make and ensure it is on PATH, then retry."
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise MakeInvocationError(
            f"ERROR: '{' '.join(command)}' exited {exc.returncode}\n"
            f"Ran against {makefile}; make reported: {exc.stderr.strip()}\n"
            "Confirm PRIVATE_FILES is defined in that Makefile."
        ) from exc
    finally:
        fragment_path.unlink()
    return tuple(completed.stdout.split())


def _set_equality_message(makefile_files: tuple[str, ...], python_files: tuple[str, ...]) -> str:
    """Names the entries present on only one side and the file to edit for each.

    A bare "not equal" leaves the reader to work out which file to edit;
    this instead points at devcontainer_config.repo.PRIVATE_FILES for a
    Makefile-only entry, and at the Makefile's PRIVATE_FILES for a
    Python-only entry.
    """
    only_in_makefile = sorted(set(makefile_files) - set(python_files))
    only_in_python = sorted(set(python_files) - set(makefile_files))
    lines = [
        "PRIVATE_FILES disagrees between the Makefile and devcontainer_config.repo.PRIVATE_FILES."
    ]
    if only_in_makefile:
        lines.append(
            f"Only in the Makefile's PRIVATE_FILES: {only_in_makefile}. "
            "Add each to devcontainer_config.repo.PRIVATE_FILES, or remove "
            "it from the Makefile if it should not be private."
        )
    if only_in_python:
        lines.append(
            f"Only in devcontainer_config.repo.PRIVATE_FILES: {only_in_python}. "
            "Add each to the Makefile's PRIVATE_FILES, or remove it from "
            "devcontainer_config.repo.PRIVATE_FILES if it should not be private."
        )
    return "\n".join(lines)


def _assert_private_files_match_as_sets(
    makefile_files: tuple[str, ...], python_files: tuple[str, ...]
) -> None:
    """AC-FUNC-004: set equality in both directions, with a naming message."""
    assert set(makefile_files) == set(python_files), _set_equality_message(
        makefile_files, python_files
    )


def _assert_private_files_match_in_order(
    makefile_files: tuple[str, ...], python_files: tuple[str, ...]
) -> None:
    """AC-FUNC-005: order agreement, asserted and reported separately from set equality.

    repo.PRIVATE_FILES documents its order as the one a developer meets the
    files in; `make init` prints them in Makefile order. A divergence here
    is cosmetic, not the dangerous drift the set-equality assertion guards
    against, so it gets its own assertion and its own message rather than
    being folded into that one.
    """
    assert tuple(makefile_files) == tuple(python_files), (
        "PRIVATE_FILES agrees as sets but disagrees in order between the "
        f"Makefile ({list(makefile_files)}) and devcontainer_config.repo "
        f"({list(python_files)}). repo.PRIVATE_FILES documents its order as "
        "the one a developer meets the files in, and make init prints them "
        "in Makefile order; reorder one list to match the other."
    )


def _write_fixture_makefile(tmp_path: Path, entries: tuple[str, ...]) -> Path:
    """A tmp_path directory holding a Makefile declaring PRIVATE_FILES as entries."""
    fixture_dir = tmp_path / f"fixture-{uuid.uuid4().hex}"
    fixture_dir.mkdir()
    (fixture_dir / "Makefile").write_text(
        f"PRIVATE_FILES ?= {' '.join(entries)}\n", encoding="utf-8"
    )
    return fixture_dir


def test_makefile_extraction_returns_nonempty_relative_paths() -> None:
    """Approach step 1: the real Makefile yields relative paths, none absolute."""
    makefile_files = _makefile_private_files(_repo_root())

    assert makefile_files
    for entry in makefile_files:
        assert not entry.startswith("/"), entry


def test_real_makefile_and_repo_module_agree_as_sets() -> None:
    """AC-TEST-001."""
    _assert_private_files_match_as_sets(_makefile_private_files(_repo_root()), repo.PRIVATE_FILES)


def test_real_makefile_and_repo_module_agree_in_order() -> None:
    """AC-TEST-002."""
    _assert_private_files_match_in_order(_makefile_private_files(_repo_root()), repo.PRIVATE_FILES)


def test_extra_makefile_entry_fails_naming_that_entry(tmp_path: Path) -> None:
    """AC-TEST-003: a fourth, Makefile-only entry makes the comparison fail."""
    extra_entry = f"extra-{uuid.uuid4().hex}.env"
    fixture_dir = _write_fixture_makefile(tmp_path, (*repo.PRIVATE_FILES, extra_entry))

    with pytest.raises(AssertionError, match=re.escape(extra_entry)):
        _assert_private_files_match_as_sets(
            _makefile_private_files(fixture_dir), repo.PRIVATE_FILES
        )


def test_missing_makefile_entry_fails_naming_the_missing_one(tmp_path: Path) -> None:
    """AC-TEST-004: a Makefile declaring only two entries fails, naming the missing one."""
    kept_entries = repo.PRIVATE_FILES[:-1]
    missing_entry = repo.PRIVATE_FILES[-1]
    fixture_dir = _write_fixture_makefile(tmp_path, kept_entries)

    with pytest.raises(AssertionError, match=re.escape(missing_entry)):
        _assert_private_files_match_as_sets(
            _makefile_private_files(fixture_dir), repo.PRIVATE_FILES
        )


def test_reordered_makefile_entries_fail_order_but_not_set_equality(tmp_path: Path) -> None:
    """A reordered Makefile is a cosmetic divergence: set equality holds, order fails."""
    reordered_entries = tuple(reversed(repo.PRIVATE_FILES))
    fixture_dir = _write_fixture_makefile(tmp_path, reordered_entries)
    makefile_files = _makefile_private_files(fixture_dir)

    _assert_private_files_match_as_sets(makefile_files, repo.PRIVATE_FILES)
    with pytest.raises(AssertionError, match="disagrees in order"):
        _assert_private_files_match_in_order(makefile_files, repo.PRIVATE_FILES)


def test_extraction_raises_when_makefile_has_no_private_files(tmp_path: Path) -> None:
    """AC-TEST-005: no PRIVATE_FILES at all raises, naming the command that was run."""
    fixture_dir = tmp_path / f"fixture-{uuid.uuid4().hex}"
    fixture_dir.mkdir()
    (fixture_dir / "Makefile").write_text("SOME_UNRELATED_VARIABLE := value\n", encoding="utf-8")

    with pytest.raises(MakeInvocationError) as excinfo:
        _makefile_private_files(fixture_dir)

    assert "make -f" in str(excinfo.value)
    assert _PRINT_TARGET in str(excinfo.value)


def test_extraction_raises_when_make_binary_is_not_on_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The extraction fails loudly, rather than yielding an empty tuple, when make is absent."""
    fixture_dir = _write_fixture_makefile(tmp_path, repo.PRIVATE_FILES)
    monkeypatch.setenv("PATH", "")

    with pytest.raises(MakeInvocationError, match="make is not installed"):
        _makefile_private_files(fixture_dir)
