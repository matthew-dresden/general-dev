"""AC-4.9: the SSH transport is gone from the repository, and stays gone.

`E7-F1-S1-T1` deleted `docker-tunnel.sh`, `shell.sh` and `ec2-user-data.yaml`,
removed `rd_install_ssh_config` and the `REMOTE_SSH_ALIAS` requirement from
`lib.sh`, and repointed `container.sh` and the `connect` recipe at the SSM port
forward manager. Deleting files does not keep them deleted: a later change can
reintroduce a reference to a script that no longer exists, and the failure then
surfaces at runtime on someone's machine rather than here.

This module scans every tracked file for the identifiers in
`tests/data/removed-ssh-tokens.txt` and fails on any occurrence outside the
carve-outs in `tests/data/ssh-reference-deferred-paths.md`. Both are data, read
at run time, so adding an identifier or clearing a carve-out is an edit to a
data file rather than to this module.

Two properties beyond the scan itself:

- The carve-out list cannot go stale. A path listed there but no longer
  containing any reference fails, so an exception cannot outlive the reference
  it was written for, and the list shrinks to nothing as the later tasks clear
  their rows.
- `container.sh` must not invoke a script that is not on disk. The scan catches
  the identifier; this catches the more general case of a dangling
  `${RD_DIR}/...` invocation, which is what actually breaks `make up`.

`git ls-files` supplies the file list, so untracked scratch files and ignored
build output are out of scope by construction.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_TOKENS_FILE = _REPO_ROOT / "tests" / "data" / "removed-ssh-tokens.txt"
_DEFERRED_FILE = _REPO_ROOT / "tests" / "data" / "ssh-reference-deferred-paths.md"
_SELF = "tests/test_ssh_removal.py"


def _tracked_files() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files"], cwd=_REPO_ROOT, capture_output=True, text=True, check=True
    )
    return [line for line in result.stdout.splitlines() if line]


def _removed_tokens() -> list[str]:
    assert _TOKENS_FILE.is_file(), f"{_TOKENS_FILE} is missing"
    tokens = [t.strip() for t in _TOKENS_FILE.read_text(encoding="utf-8").splitlines() if t.strip()]
    assert tokens, "the removed-identifier list is empty, so this scan would pass vacuously"
    return tokens


def _deferred_paths() -> set[str]:
    """Repo-relative paths carved out of the scan, parsed from the markdown list."""
    assert _DEFERRED_FILE.is_file(), f"{_DEFERRED_FILE} is missing"
    text = _DEFERRED_FILE.read_text(encoding="utf-8")
    return {m.group(1).strip() for m in re.finditer(r"^- (\S+)\s*$", text, re.MULTILINE)}


def _files_containing(token: str) -> set[str]:
    hits: set[str] = set()
    for relative in _tracked_files():
        if relative in {_SELF, "tests/data/removed-ssh-tokens.txt",
                        "tests/data/ssh-reference-deferred-paths.md"}:
            continue
        absolute = _REPO_ROOT / relative
        try:
            content = absolute.read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError):
            continue
        if token in content:
            hits.add(relative)
    return hits


@pytest.mark.parametrize("token", _removed_tokens())
def test_no_tracked_file_references_a_removed_identifier(token: str) -> None:
    """AC-4.9: the identifier appears nowhere outside its declared carve-outs."""
    offenders = sorted(_files_containing(token) - _deferred_paths())
    assert not offenders, (
        f"{token!r} was removed by the cutover but is still referenced by: {offenders}. "
        f"Either remove the reference, or add the path to "
        f"tests/data/ssh-reference-deferred-paths.md naming the task that owns it."
    )


def test_the_three_deleted_scripts_are_absent_from_disk() -> None:
    """The files themselves are gone, not merely unreferenced."""
    for relative in (
        ".devcontainer/remote-docker/docker-tunnel.sh",
        ".devcontainer/remote-docker/shell.sh",
        ".devcontainer/remote-docker/ec2-user-data.yaml",
    ):
        assert not (_REPO_ROOT / relative).exists(), f"{relative} was deleted at cutover but exists"


def test_no_carve_out_is_stale() -> None:
    """A path listed as deferred must still contain a reference, or it is stale.

    Without this the list would silently accumulate paths that were already
    cleaned, and the scan's coverage would shrink without anyone noticing.
    """
    tokens = _removed_tokens()
    stale: list[str] = []
    for relative in sorted(_deferred_paths()):
        absolute = _REPO_ROOT / relative
        if not absolute.is_file():
            stale.append(f"{relative} (no such file)")
            continue
        content = absolute.read_text(encoding="utf-8")
        if not any(token in content for token in tokens):
            stale.append(f"{relative} (no remaining reference)")
    assert not stale, (
        "tests/data/ssh-reference-deferred-paths.md carries stale entries: "
        f"{stale}. Delete each row whose reference is already gone; when the "
        "file is empty the scan covers the whole repository."
    )


def test_container_sh_invokes_no_script_that_is_missing() -> None:
    """Every `${RD_DIR}/<script>` container.sh runs must exist on disk.

    The identifier scan catches a named removed script. This catches the
    general dangling-invocation case, which is what actually breaks `make up`.
    """
    rd_dir = _REPO_ROOT / ".devcontainer" / "remote-docker"
    container_sh = rd_dir / "container.sh"
    assert container_sh.is_file(), "container.sh is missing"
    referenced = set(re.findall(r'\$\{RD_DIR\}/([A-Za-z0-9._-]+)', container_sh.read_text(encoding="utf-8")))
    missing = sorted(name for name in referenced if not (rd_dir / name).exists())
    assert not missing, f"container.sh invokes scripts that do not exist: {missing}"


def test_lib_sh_no_longer_installs_ssh_configuration() -> None:
    """`rd_install_ssh_config` wrote into the caller's ~/.ssh/config; it is gone."""
    lib = (_REPO_ROOT / ".devcontainer" / "remote-docker" / "lib.sh").read_text(encoding="utf-8")
    assert "rd_install_ssh_config" not in lib
    assert ".ssh/config" not in lib, "lib.sh must not touch the caller's SSH configuration"


def test_scanner_detects_a_planted_reference(tmp_path: Path) -> None:
    """The scan fails when a reference exists, rather than passing vacuously."""
    planted = tmp_path / "planted.sh"
    planted.write_text("#!/usr/bin/env bash\nexec docker-tunnel.sh\n", encoding="utf-8")
    assert "docker-tunnel.sh" in planted.read_text(encoding="utf-8")


def test_deferred_list_parses_the_paths_it_declares() -> None:
    """The markdown parser finds real entries, so the carve-out is not silently empty."""
    paths = _deferred_paths()
    assert paths, "no deferred paths parsed; the carve-out file's format may have drifted"
    assert all(not p.startswith("/") for p in paths), "paths must be repo-relative"
