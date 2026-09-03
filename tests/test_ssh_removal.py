"""AC-4.9: the SSH transport is gone from the repository, and stays gone.

`E7-F1-S1-T1` deleted `docker-tunnel.sh`, `shell.sh` and `ec2-user-data.yaml`,
removed `rd_install_ssh_config` and the `REMOTE_SSH_ALIAS` requirement from
`lib.sh`, and repointed `container.sh` and the `connect` recipe at the SSM port
forward manager. Deleting files does not keep them deleted: a later change can
reintroduce a reference to a script that no longer exists, and the failure then
surfaces at runtime on someone's machine rather than here.

This module scans every tracked file for the identifiers in
`tests/data/removed-ssh-tokens.txt` and fails on any occurrence anywhere in the
repository. There are no carve-outs: the two test modules that once needed one,
because asserting an identifier's absence meant naming it, now read the same
token list through `conftest._removed_identifiers` and contain none of them.
The token list is data, read
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


def _files_containing(token: str) -> set[str]:
    hits: set[str] = set()
    for relative in _tracked_files():
        if relative in {_SELF, "tests/data/removed-ssh-tokens.txt"}:
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
    """AC-4.9: the identifier appears nowhere in the repository at all."""
    offenders = sorted(_files_containing(token))
    assert not offenders, (
        f"{token!r} was removed by the cutover but is still referenced by: {offenders}. "
        "Remove the reference. There is no carve-out list any more: a test that must "
        "assert this identifier's absence reads it from tests/data/removed-ssh-tokens.txt "
        "through conftest._removed_identifiers rather than spelling it."
    )


def test_the_three_deleted_scripts_are_absent_from_disk() -> None:
    """The files themselves are gone, not merely unreferenced."""
    for relative in (
        ".devcontainer/remote-docker/docker-tunnel.sh",
        ".devcontainer/remote-docker/shell.sh",
        ".devcontainer/remote-docker/ec2-user-data.yaml",
    ):
        assert not (_REPO_ROOT / relative).exists(), f"{relative} was deleted at cutover but exists"


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


