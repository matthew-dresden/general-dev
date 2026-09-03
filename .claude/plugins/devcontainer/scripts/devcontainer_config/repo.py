"""Where private-file and container-workspace paths live, relative to the repo root.

Every other module in this package takes a root as an argument rather than
discovering one itself, so a test can point the whole package at a temporary
directory instead of the real checkout. Something has to know how to find
that root and how to derive paths beneath it in the first place; that is
this module, which is why it was the first to land. `repo_slug` (below)
reads `devcontainer_config.hostprobe.read_positive_seconds` for its
env-configurable timeout, the same shared reader `transport.py` uses, which
is this module's only intra-package dependency.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from devcontainer_config.hostprobe import HostProbeError, read_positive_seconds

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

# Bounds how long `repo_slug` waits on `git config --get remote.origin.url`,
# a local, network-free read of the checkout's own `.git/config` file. Read
# fresh on every call through `hostprobe.read_positive_seconds`, the single
# shared reader this variable's name and default are resolved against.
# This module holds the single declaration of both names below;
# `tests/test_state_bucket_name.py` binds to them by importing
# `GIT_REMOTE_TIMEOUT_ENV_VAR` and `GIT_REMOTE_TIMEOUT_DEFAULT_SECONDS`
# from this module rather than restating either.
GIT_REMOTE_TIMEOUT_ENV_VAR = "REPO_SLUG_GIT_TIMEOUT_SECONDS"
GIT_REMOTE_TIMEOUT_DEFAULT_SECONDS = 10.0


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


def _git_remote_timeout_seconds() -> float:
    """The deadline `repo_slug` gives its git-config read (`GIT_REMOTE_TIMEOUT_ENV_VAR`).

    Read fresh on every call, not cached at import time, so a caller that
    sets `REPO_SLUG_GIT_TIMEOUT_SECONDS` before calling `repo_slug` observes
    its own value. Delegates the parse and validation to
    `hostprobe.read_positive_seconds`, the single shared reader this
    variable's name and default are declared against, re-raising its
    `HostProbeError` as `RepoError` so no exception type foreign to this
    module crosses the `repo_slug` boundary.
    """
    try:
        return read_positive_seconds(GIT_REMOTE_TIMEOUT_ENV_VAR, GIT_REMOTE_TIMEOUT_DEFAULT_SECONDS)
    except HostProbeError as exc:
        raise RepoError(
            f"ERROR: {exc}\n"
            f"Set {GIT_REMOTE_TIMEOUT_ENV_VAR} to a positive number of seconds, "
            f"or unset it to use the default of "
            f"{GIT_REMOTE_TIMEOUT_DEFAULT_SECONDS:g}."
        ) from exc


def repo_slug(root: Path) -> str:
    """The repository slug `remote-instances/root.hcl`'s `repo_slug` local also derives.

    Mirrors that local's own transform on every well-formed remote URL:
    `git config --get remote.origin.url`, then the final `/`-delimited path
    segment (which is what Terragrunt's `basename()` also gives for both the
    HTTPS form `https://host/org/repo.git` and the SSH form
    `git@host:org/repo.git`, per root.hcl's own comment), then a trailing
    `.git` removed (Terragrunt's `trimsuffix(..., ".git")`). This
    correspondence is a documented convention, not an enforced invariant:
    an HCL `locals` block and a Python function cannot share one
    declaration across languages, so keeping the two transforms in step
    depends on updating both here and in root.hcl whenever either changes,
    the same way `tests/test_repo.py::test_repo_slug_derives_from_git_remote`
    pins this function's own behavior against root.hcl's documented forms.

    Derives the slug from the git remote only; there is no fallback to the
    checkout directory name and no default value on any path.

    Raises:
        RepoError: if the `git` binary is not on PATH, if `GIT_REMOTE_TIMEOUT_ENV_VAR`
            is set to a value `hostprobe.read_positive_seconds` rejects, if
            the read does not answer within the resolved deadline, if no
            `remote.origin.url` is configured for `root`, if the configured
            URL has no final path segment, or if that final path segment is
            only `.git` and therefore trims down to an empty slug.
    """
    timeout = _git_remote_timeout_seconds()
    try:
        completed = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RepoError(
            "ERROR: git is not installed\n"
            "repo_slug needs the git binary to read remote.origin.url and "
            "none was found on PATH.\n"
            "Install git and ensure it is on PATH, then retry."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise RepoError(
            f"ERROR: git config --get remote.origin.url on {root} did not "
            f"answer within {timeout:g}s\n"
            f"repo_slug bounds this local, network-free config read via "
            f"{GIT_REMOTE_TIMEOUT_ENV_VAR}.\n"
            "Investigate why a local git config read is unexpectedly slow "
            "(for example a stale index.lock or disk contention), or raise "
            f"{GIT_REMOTE_TIMEOUT_ENV_VAR}."
        ) from exc

    remote_url = completed.stdout.strip()
    if completed.returncode != 0 or not remote_url:
        raise RepoError(
            f"ERROR: no remote.origin.url is configured for {root}\n"
            "repo_slug derives the repository slug from 'git config --get "
            "remote.origin.url' and git reported no value.\n"
            "Configure a remote named 'origin' on this checkout, or pass "
            "the repository root that has one."
        )

    last_segment = remote_url.rsplit("/", 1)[-1]
    if not last_segment:
        raise RepoError(
            f"ERROR: no final path segment in remote.origin.url configured "
            f"for {root}\n"
            "repo_slug could not derive a repository slug from the "
            "configured remote.origin.url.\n"
            "Fix the configured remote URL so it ends in a repository name."
        )

    slug = last_segment.removesuffix(".git")
    if not slug:
        raise RepoError(
            f"ERROR: remote.origin.url configured for {root} has no "
            "repository name once a trailing '.git' is removed\n"
            "repo_slug could not derive a non-empty repository slug from "
            "the configured remote.origin.url.\n"
            "Fix the configured remote URL so it ends in a repository name, "
            "not a bare '.git'."
        )
    return slug


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
