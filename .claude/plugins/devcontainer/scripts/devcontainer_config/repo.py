"""Where private-file and container-workspace paths live, relative to the repo root.

Every other module in this package takes a root as an argument rather than
discovering one itself, so a test can point the whole package at a temporary
directory instead of the real checkout. Something has to know how to find
that root and how to derive paths beneath it in the first place; that is
this module, which is why it depends on nothing else in the package and is
the first to land.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

SHELL_ENV = "shell.env"
DEVCONTAINER_ENV_JSON = "devcontainer-environment-variables.json"
AWS_PROFILE_MAP = ".devcontainer/aws-profile-map.json"

# The three gitignored files a fresh clone does not have (spec Section 1.7),
# ordered as a developer meets them: the shell environment, the devcontainer
# feature environment, and the AWS profile map. Makefile:31 holds an
# independent copy of this same list in its PRIVATE_FILES variable (spec
# Section 3.3); that copy must be edited alongside this tuple whenever the
# private-file set changes. E1-F4-S1-T2 lands the assertion that keeps the
# two copies from drifting apart.
PRIVATE_FILES: tuple[str, ...] = (SHELL_ENV, DEVCONTAINER_ENV_JSON, AWS_PROFILE_MAP)

EXAMPLE_SUFFIX = ".example"


class RepoError(RuntimeError):
    """Raised when the repository state a caller needs is not there.

    A bare exception message would leave the operator guessing which path
    was probed or which prerequisite was missing; every raise site in this
    module names one or the other explicitly instead of a generic message.
    """


def example_for(relative_path: str) -> str:
    """The committed example a private file is rendered from (spec Section 5.2).

    `render` and `missing_examples` both need this mapping. Keeping the
    suffix here, and only here, is what makes it safe to change the naming
    convention in one place instead of wherever it happened to be repeated.
    """
    return f"{relative_path}{EXAMPLE_SUFFIX}"


def find_root(start: Path) -> Path:
    """The git repository root containing `start`.

    Asks git rather than walking upward for a `.git` directory, because a
    worktree has a `.git` file, not a directory, and a manual walk would
    resolve to the wrong place there.

    Raises:
        RepoError: if the `git` binary is not on PATH, or if `start` is not
            inside any git repository.
    """
    try:
        completed = subprocess.run(
            ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError as exc:
        raise RepoError(
            "ERROR: git is not installed\n"
            "find_root needs the git binary to locate the repository root "
            "and none was found on PATH.\n"
            "Install git and ensure it is on PATH, then retry."
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise RepoError(
            f"ERROR: {start} is not inside a git repository\n"
            f"find_root probed {start} with 'git rev-parse --show-toplevel' "
            "and git reported no repository there.\n"
            "Run this from inside a git checkout, or pass the checkout root "
            "explicitly instead of relying on discovery."
        ) from exc
    return Path(completed.stdout.strip())


def workspace_name(root: Path) -> str:
    """The basename devcontainer.json expects the checkout to be mounted under.

    devcontainer.json sets workspaceFolder to
    /workspaces/${localWorkspaceFolderBasename} (spec Section 1.10); BASH_ENV
    inside shell.env has to agree with whatever that resolves to, or every
    non-interactive shell in the container sources nothing.
    """
    return root.name


def container_workspace(root: Path, workspaces_root: str = "/workspaces") -> str:
    """The in-container path of the checkout.

    `workspaces_root` is a parameter with this documented default -- matching
    devcontainer.json's workspaceFolder root (spec Section 1.10) -- rather
    than the literal being embedded at each call site that needs it, so
    `render` and `verify` share one definition of where it comes from.
    """
    return f"{workspaces_root}/{workspace_name(root)}"


def private_paths(root: Path) -> dict[str, Path]:
    """Absolute path of every private file under root, keyed by its relative name.

    `render` writes to these paths and `verify` reads them; both need the
    same mapping from the repository-relative name to an absolute location,
    so it is derived here once from PRIVATE_FILES instead of recomputed by
    each caller.
    """
    return {relative: root / relative for relative in PRIVATE_FILES}


def missing_examples(root: Path) -> list[str]:
    """Example paths, under root, that are absent for a private file.

    `render` must refuse before writing any of the three private files if
    even one committed example is missing, rather than leaving a partially
    rendered configuration behind; this is what lets it check that up front.
    """
    return [
        example_for(relative)
        for relative in PRIVATE_FILES
        if not (root / example_for(relative)).is_file()
    ]
