"""Hook content, installation and push-range derivation (spec Section 4.5).

Section 4.5 gives this module one job: hook content and installation.
`hook_body` renders both the pre-commit and the pre-push hook from one
template, each carrying an authorship marker naming this module, so
`install_hooks` can tell its own work apart from a developer's own hook
later. Pre-commit execs `make hooks-run`, which is `lint`, which includes
`lint-secrets` over staged content (E2-F1-S1-T2). Pre-push execs a
different target, `make hooks-run-push`, because it has to scan a range
that only pre-push knows: `ranges_from_push_refs` reads git's own pre-push
stdin contract, one `<local ref> <local sha> <remote ref> <remote sha>`
line per ref being pushed, and turns it into the `<a>..<b>` ranges
`devcontainer_config.secrets.scan_range` (E2-F1-S2-T1) understands.

Two of git's four fields carry a signal beyond "a ref moved": an all-zero
local object id means the ref is being deleted, so there is nothing to
scan; an all-zero remote object id means the remote has never seen this
branch, so there is no single remote object to subtract. The correct range
for that case is every commit reachable from the local tip that is not
already reachable from some remote-tracking ref this repository knows
about (AC-FUNC-005) -- not, as an earlier version of this module got
wrong, every commit reachable from the local tip outright, which would
re-scan a new branch's entire inherited history on its first push. That
base is the merge base of the local tip with every local remote-tracking
ref together: a commit derived that way is, by construction, an ancestor
of every one of those refs, so excluding its ancestors never excludes a
commit that is not, in fact, already on some remote. When this repository
knows no remote-tracking ref at all, or shares no ancestry with any of
them, nothing is provably already pushed, and the base falls back to the
empty tree object id -- the same "nothing to diff against" idiom
`devcontainer_config.secrets.empty_tree_object_id` uses for a root commit,
because two-dot range syntax needs a real revision on its left side and the
empty-tree object id is exactly that: the tree with nothing in it, always
reachable from nowhere. That fallback is deliberately the conservative
direction: it scans more than strictly necessary rather than risking a
skip, because the first push of a branch is precisely the one carrying the
most unreviewed history.

`install_hooks` renders the expected content for both hooks and writes
only what is not already there, which is what makes a second install
idempotent (AC-FUNC-002) instead of an unconditional rewrite. It refuses
to overwrite a hook that does not carry this module's authorship marker: a
developer's own pre-commit hook is their work, and clobbering it to
install a security control is the kind of surprise that gets the control
uninstalled. `hooks_status` answers the same "does this match" question
without ever writing, so a drift check cannot itself introduce drift
(AC-FUNC-003). A hook already on disk is not guaranteed to be valid UTF-8
-- it is a developer's own file until this module's marker says otherwise
-- so both functions read it through `_read_hook_text`, which turns an
undecodable file into a `GitHooksError` instead of letting a bare
`UnicodeDecodeError` escape.

The subprocess wrapper and the empty-tree derivation are not reimplemented
here: both are shared with `devcontainer_config.secrets`
(`secrets.run_git`, `secrets.empty_tree_object_id`), which already spawns
`git` and turns a failure into an exception the same shape this module
needs, parameterized by `error_type` so this module's failures still come
back as `GitHooksError` rather than `secrets.SecretScanError`.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from devcontainer_config.secrets import empty_tree_object_id, run_git

# The order `install_hooks` writes hooks in and `hooks_status` reports on;
# also the order the former Makefile inline loop used, preserved here so
# nothing downstream observes a change in ordering.
HOOK_NAMES: tuple[str, ...] = ("pre-commit", "pre-push")

# The make target each hook execs. Keyed by hook name rather than inferred
# from it, so adding a third hook later is one entry here, not a naming
# convention every caller has to guess at.
_HOOK_TARGETS: dict[str, str] = {
    "pre-commit": "hooks-run",
    "pre-push": "hooks-run-push",
}

# Named once and reused by both `hook_body` (to write it) and
# `install_hooks` (to recognize it on an existing file), so the two never
# drift into checking for different text.
_AUTHOR_MARKER = "# Written by devcontainer_config.githooks -- do not hand-edit."

_HOOK_TEMPLATE = (
    "#!/usr/bin/env sh\n"
    f"{_AUTHOR_MARKER}\n"
    '# Installed by "make hooks-install". Runs the same checks "make {target}" runs.\n'
    "exec make {target}\n"
)

# git pads an unset object id with zeros to the hash algorithm's length (40
# for SHA-1, 64 for SHA-256); matching on "one or more zero digits" instead
# of a hard-coded length is what makes this work under either.
_ZERO_OBJECT_ID = re.compile(r"^0+$")

_PUSH_REF_FIELD_COUNT = 4

# `git merge-base`'s own exit code when the named commits share no common
# ancestor at all -- a valid, non-error result (see `_not_on_any_remote_base`),
# distinguished from a genuine failure by git also emitting no stderr for it.
_MERGE_BASE_NO_COMMON_ANCESTOR_EXIT_CODE = 1


class GitHooksError(RuntimeError):
    """Raised when hook content cannot be rendered, installed, or reported on.

    Every raise site in this module names what it was doing and what the
    operator should do about it, the same `ERROR:` / remediation shape
    `devcontainer_config.secrets.SecretScanError` uses, so a caller (the
    CLI) can convert either into an exit code and a message the same way.
    """


def hook_body(hook_name: str) -> str:
    """The full text of the `hook_name` hook, rendered from one shared template.

    Both `pre-commit` and `pre-push` come from `_HOOK_TEMPLATE`, differing
    only in which make target they exec (AC-FUNC-001); nothing about the
    shebang, the authorship marker, or the installed-by comment can drift
    between the two, because there is only one place either is written.

    Raises:
        GitHooksError: if `hook_name` is not one of `HOOK_NAMES`.
    """
    target = _HOOK_TARGETS.get(hook_name)
    if target is None:
        raise GitHooksError(
            f"ERROR: unknown hook name: {hook_name!r}\n"
            f"hook_body only renders content for {', '.join(HOOK_NAMES)}.\n"
            "Pass one of those names, or add the new hook to HOOK_NAMES and "
            "_HOOK_TARGETS together before calling this again."
        )
    return _HOOK_TEMPLATE.format(target=target)


def _hooks_dir(root: Path) -> Path:
    """Where hooks live under `root`; the one place both callers derive this path."""
    return root / ".git" / "hooks"


def _read_hook_text(path: Path, *, make_target: str) -> str | None:
    """`path`'s decoded text if it exists, `None` if it does not.

    Both `install_hooks` and `hooks_status` need "read whatever hook is
    already on disk, if any, as text", differing only in which make target
    belongs in the remediation line if decoding fails; that shape lives
    here once so a hook that is not valid UTF-8 -- a developer's own hook
    is under no obligation to be one -- becomes a `GitHooksError` naming the
    file and a remedy in exactly one place, not two, and never an unhandled
    `UnicodeDecodeError`.

    Raises:
        GitHooksError: if `path` exists but its content cannot be decoded
            as UTF-8.
    """
    if not path.is_file():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise GitHooksError(
            f"ERROR: {path} is not valid UTF-8\n"
            f"{make_target} could not decode the existing content of {path}: {exc}\n"
            f"Move {path} aside or remove it yourself, then re-run 'make {make_target}'."
        ) from exc


def install_hooks(root: Path) -> tuple[Path, ...]:
    """Write both hooks under `root`'s `.git/hooks`, executable; the paths written.

    Idempotent (AC-FUNC-002): a hook already holding exactly the content
    `hook_body` would render is left untouched rather than rewritten, so a
    second install leaves byte-identical content and a byte-identical
    mtime.

    Refuses to overwrite a hook that does not carry this module's
    authorship marker (see the module docstring): a hook this module never
    wrote is a developer's own work, not this control's to replace.

    Raises:
        GitHooksError: if `.git/hooks` cannot be created or written to; if
            an existing hook's content cannot be decoded as UTF-8; or if an
            existing hook's content lacks the authorship marker.
    """
    hooks_dir = _hooks_dir(root)
    try:
        hooks_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise GitHooksError(
            f"ERROR: cannot create {hooks_dir}\n"
            f"install_hooks needs this directory to exist and be writable, and "
            f"the OS reported: {exc}\n"
            f"Confirm {root} is a git checkout with a writable .git directory, "
            "then retry."
        ) from exc

    written: list[Path] = []
    for hook_name in HOOK_NAMES:
        expected = hook_body(hook_name)
        path = hooks_dir / hook_name
        existing = _read_hook_text(path, make_target="hooks-install")
        if existing is not None:
            if existing == expected:
                written.append(path)
                continue
            if _AUTHOR_MARKER not in existing:
                raise GitHooksError(
                    f"ERROR: {path} was not written by devcontainer_config.githooks\n"
                    "install_hooks refuses to overwrite a hook it did not author, "
                    "in case it is a developer's own hook.\n"
                    f"Move {path} aside or remove it yourself, then re-run "
                    "'make hooks-install'."
                )
        try:
            path.write_text(expected, encoding="utf-8")
            path.chmod(0o755)
        except OSError as exc:
            raise GitHooksError(
                f"ERROR: cannot write {path}\n"
                f"install_hooks needs to write this file and make it executable, "
                f"and the OS reported: {exc}\n"
                f"Confirm {hooks_dir} is writable, then retry."
            ) from exc
        written.append(path)
    return tuple(written)


def hooks_status(root: Path) -> dict[str, bool]:
    """Whether each hook under `root` currently matches what `install_hooks` would write.

    Read-only (AC-FUNC-003): this never writes, so running it to look for
    drift cannot itself introduce drift. A missing hook counts as not
    matching, the same as one whose content has been edited.

    Raises:
        GitHooksError: if an existing hook's content cannot be decoded as
            UTF-8.
    """
    hooks_dir = _hooks_dir(root)
    status: dict[str, bool] = {}
    for hook_name in HOOK_NAMES:
        path = hooks_dir / hook_name
        existing = _read_hook_text(path, make_target="hooks-check")
        status[hook_name] = existing is not None and existing == hook_body(hook_name)
    return status


def _remote_tracking_object_ids(root: Path) -> tuple[str, ...]:
    """The object id of every local remote-tracking ref under `root`, in git's own order.

    Reads `git for-each-ref refs/remotes/`: exactly the refs a prior `git
    fetch` (or `git clone`) has made this repository aware of. A ref this
    repository has never fetched contributes nothing, which is deliberate
    (see `_not_on_any_remote_base`): this function answers "what does this
    checkout already know is on some remote", not "what actually is."

    Raises:
        GitHooksError: if the `git` binary is not on PATH, or if git cannot
            list refs under `root`.
    """
    stdout = run_git(
        ["for-each-ref", "--format=%(objectname)", "refs/remotes/"],
        root=root,
        error_type=GitHooksError,
        not_installed_message=lambda: (
            "ERROR: git is not installed\n"
            f"ranges_from_push_refs needs the git binary to list remote-tracking "
            f"refs under {root} and none was found on PATH.\n"
            "Install git and ensure it is on PATH, then retry."
        ),
        failure_message=lambda stderr: (
            f"ERROR: cannot list remote-tracking refs under {root}\n"
            f"'git for-each-ref refs/remotes/' reported: {stderr}\n"
            "Run this from inside a git checkout, or pass the checkout root "
            "explicitly instead of relying on discovery."
        ),
    )
    text = stdout.decode("utf-8")
    return tuple(line for line in text.splitlines() if line)


def _not_on_any_remote_base(root: Path, local_sha: str, remote_ids: tuple[str, ...]) -> str:
    """The base for a range covering only commits absent from every known remote ref.

    `remote_ids` is every local remote-tracking ref's object id
    (`_remote_tracking_object_ids`), passed in rather than looked up here so
    one push touching several new branches asks git for that list once, not
    once per ref.

    When `remote_ids` is empty, this repository knows of no remote-tracking
    ref at all, so nothing is provably already pushed; the base is the
    empty tree, so the range covers every commit reachable from `local_sha`
    (see the module docstring's "err toward scanning more" rule).
    Otherwise the base is `git merge-base local_sha <every remote id>`: a
    commit that is, by construction, an ancestor of `local_sha` and of
    every remote-tracking ref together, so excluding its ancestors can
    never exclude a commit that is not in fact already on some remote. If
    `local_sha` shares no ancestor with the remote refs at all -- `git
    merge-base` itself exits 1 with no stderr for that, not an error --
    nothing is provably shared either, and the same empty-tree fallback
    applies.

    Raises:
        GitHooksError: if the `git` binary is not on PATH, or if git fails
            to compute a merge base for a reason other than "no common
            ancestor".
    """
    if not remote_ids:
        return empty_tree_object_id(root, error_type=GitHooksError)
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "merge-base", local_sha, *remote_ids],
            capture_output=True,
            check=True,
        )
    except FileNotFoundError as exc:
        raise GitHooksError(
            "ERROR: git is not installed\n"
            f"ranges_from_push_refs needs the git binary to derive the "
            f"not-on-any-remote base for {local_sha} under {root} and none "
            "was found on PATH.\n"
            "Install git and ensure it is on PATH, then retry."
        ) from exc
    except subprocess.CalledProcessError as exc:
        no_common_ancestor = (
            exc.returncode == _MERGE_BASE_NO_COMMON_ANCESTOR_EXIT_CODE and not exc.stderr
        )
        if no_common_ancestor:
            return empty_tree_object_id(root, error_type=GitHooksError)
        stderr = exc.stderr.decode("utf-8", errors="replace").strip() if exc.stderr else ""
        raise GitHooksError(
            f"ERROR: cannot derive the not-on-any-remote base for {local_sha} under {root}\n"
            f"'git merge-base' reported: {stderr}\n"
            "Confirm the local and remote-tracking refs both exist in this "
            "repository, for example with 'git rev-parse <ref>', then retry."
        ) from exc
    return completed.stdout.decode("utf-8").strip()


def ranges_from_push_refs(stdin_text: str, root: Path) -> tuple[str, ...]:
    """One `<a>..<b>` scan-able range per ref in `stdin_text` that is not a delete.

    `stdin_text` is exactly what git's pre-push hook contract puts on
    stdin: one `<local ref> <local sha> <remote ref> <remote sha>` line per
    ref being pushed (AC-FUNC-005). For an ordinary update this yields
    `<remote sha>..<local sha>`; for a ref the remote has never seen (an
    all-zero remote object id) it yields the not-on-any-remote form instead
    (`_not_on_any_remote_base`, see the module docstring), because there is
    no single remote object to subtract; for a deleted ref (an all-zero
    local object id) it yields nothing, because there is no local tip to
    scan. Several refs in one push yield one range per ref, in the order
    git supplied them (AC-FUNC-006); the remote-tracking refs this
    repository knows about are looked up at most once per call, reused for
    every all-zero-remote line in the same push.

    Raises:
        GitHooksError: if any non-blank line does not split into exactly
            four whitespace-separated fields.
    """
    ranges: list[str] = []
    remote_ids: tuple[str, ...] | None = None
    for line_number, raw_line in enumerate(stdin_text.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        tokens = line.split()
        if len(tokens) != _PUSH_REF_FIELD_COUNT:
            raise GitHooksError(
                f"ERROR: malformed push-ref line {line_number}: {raw_line!r}\n"
                "ranges_from_push_refs expects git's pre-push hook contract: "
                "four whitespace-separated fields per line, '<local ref> "
                "<local sha> <remote ref> <remote sha>'.\n"
                "This text comes from git itself on stdin; report this as a "
                "bug rather than editing it by hand."
            )
        _local_ref, local_sha, _remote_ref, remote_sha = tokens
        if _ZERO_OBJECT_ID.match(local_sha):
            continue
        if _ZERO_OBJECT_ID.match(remote_sha):
            if remote_ids is None:
                remote_ids = _remote_tracking_object_ids(root)
            base = _not_on_any_remote_base(root, local_sha, remote_ids)
        else:
            base = remote_sha
        ranges.append(f"{base}..{local_sha}")
    return tuple(ranges)
