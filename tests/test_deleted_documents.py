"""AC-8.1: the documents deleted at cutover stay deleted, and unreferenced.

Deleting a document is not finished when the file is gone. A path that is still
named somewhere sends a reader to a document that no longer exists, and the
reader cannot tell whether the reference is stale or the file was lost. The
sweep therefore checks three separate things -- untracked, absent from disk, and
unreferenced -- because a path can fail any one of them independently: a
restored file is tracked and present, a stale link is neither.

The paths are read from `tests/data/deleted-documents.md` rather than spelled
here, so the data and the sweep cannot drift apart, and so adding a newly
deleted document to the sweep is a one-line data change.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DATA = _REPO_ROOT / "tests" / "data" / "deleted-documents.md"
_SELF = "tests/test_deleted_documents.py"
_DATA_RELATIVE = "tests/data/deleted-documents.md"


def _deleted_paths() -> tuple[str, ...]:
    """Every repo-relative path listed in the data file."""
    assert _DATA.is_file(), f"{_DATA} is missing"
    paths = tuple(
        match.group(1)
        for match in re.finditer(r"^- (\S+)$", _DATA.read_text(encoding="utf-8"), re.MULTILINE)
    )
    assert paths, f"{_DATA} lists no paths; every assertion below would be vacuous"
    return paths


def _tracked_files() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files"], cwd=_REPO_ROOT, capture_output=True, text=True, check=True
    )
    return [line for line in result.stdout.splitlines() if line]


def test_the_data_file_lists_the_paths_it_declares() -> None:
    """A parser that silently stopped matching would make this module pass while checking nothing."""
    paths = _deleted_paths()
    assert all(path.endswith(".md") for path in paths), paths


@pytest.mark.parametrize("relative", _deleted_paths())
def test_the_document_is_not_tracked(relative: str) -> None:
    assert relative not in _tracked_files(), (
        f"{relative} was deleted at cutover but is tracked again"
    )


@pytest.mark.parametrize("relative", _deleted_paths())
def test_the_document_is_absent_from_disk(relative: str) -> None:
    assert not (_REPO_ROOT / relative).exists(), (
        f"{relative} was deleted at cutover but exists on disk"
    )


@pytest.mark.parametrize("relative", _deleted_paths())
def test_no_tracked_file_references_the_document(relative: str) -> None:
    """A surviving reference points a reader at a document that is not there."""
    name = Path(relative).name
    offenders = []
    for tracked in _tracked_files():
        if tracked in {_SELF, _DATA_RELATIVE}:
            continue
        path = _REPO_ROOT / tracked
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if name in text:
            offenders.append(tracked)
    assert not offenders, (
        f"{relative} was deleted at cutover but is still referenced by: {sorted(offenders)}\n"
        "Point the reference at what replaced it, or remove it."
    )
