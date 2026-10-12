"""Tests for devcontainer_config.githooks: hook content, installation and

push-range derivation (spec Section 4.5).

Every fixture repository here is a real, disposable git repository created
under `tmp_path` by shelling out to the actual `git` binary, using the
shared primitives in `tests/gitfixtures.py` (`generated_root`, `init_repo`,
`commit_text`), because `install_hooks` and
`ranges_from_push_refs` both need a real `.git` directory to write into or
ask git a real question of, not a mocked filesystem. `_set_remote_tracking_ref`
below is a local, minimal primitive that stays local rather than joining
`tests/gitfixtures.py`, because that file is outside this task's Changes
Manifest, the same discipline `tests/test_cli.py` documents for its own
local `_run_cli_with_stdin`.

The `devcontainer_config` import is deferred into `import_githooks`, called
from inside each test body, for the same reason every other test file in
this suite documents (see `tests/test_repo.py`'s module docstring in full):
the TDD RED gate stashes this unit's production-source files and re-runs a
single named test node, and a module-level
`from devcontainer_config.githooks import ...` would fail COLLECTION for the
whole file instead of failing the one test for the real reason. This helper
is defined locally, not added to `tests/gitfixtures.py`, because that file
is outside this task's Changes Manifest.

No sha literal here is ever a real, resolvable commit unless a test's own
docstring says otherwise: `_fake_sha` builds a 40-hex-character string from
`uuid.uuid4()` at run time, because `ranges_from_push_refs` only ever
formats these into a range string for the ordinary and multi-ref cases, it
never asks git to resolve them, so a synthetic value is a fair and dynamic
stand-in for one git would have supplied instead. The not-on-any-remote
cases are different: deriving the correct base needs a real merge-base
computed by git, so those tests build a real commit graph instead.
"""

from __future__ import annotations

import importlib
import re
import stat
import subprocess
import uuid
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest
from devcontainer_config.repo import find_root
from gitfixtures import commit_text, generated_root, init_repo

_ZERO_SHA = "0" * 40


def import_githooks() -> ModuleType:
    """Import devcontainer_config.githooks from inside a function body.

    See the module docstring for why this is not a module-level import.
    """
    return importlib.import_module("devcontainer_config.githooks")


def _fake_sha() -> str:
    """A 40-hex-character string shaped like a git object id, never a real one."""
    return (uuid.uuid4().hex + uuid.uuid4().hex)[:40]


def _push_ref_line(local_ref: str, local_sha: str, remote_ref: str, remote_sha: str) -> str:
    """One line in git's pre-push hook stdin shape."""
    return f"{local_ref} {local_sha} {remote_ref} {remote_sha}"


def _empty_tree_object_id_for_test(root: Path) -> str:
    """The empty-tree object id under `root`, derived independently of production code.

    Runs `git hash-object -t tree --stdin` directly rather than calling
    `devcontainer_config.secrets.empty_tree_object_id` or any githooks
    helper, so a wrong value returned by the function under test could not
    also make its own expectation wrong.
    """
    completed = subprocess.run(
        ["git", "-C", str(root), "hash-object", "-t", "tree", "--stdin"],
        input=b"",
        capture_output=True,
        check=True,
    )
    return completed.stdout.decode("utf-8").strip()


def _set_remote_tracking_ref(root: Path, ref: str, commit: str) -> None:
    """Point `refs/remotes/<ref>` at `commit`, as `git fetch` would after a prior push.

    Building a real remote (a bare repository plus an actual `git push`)
    would prove nothing `git update-ref` does not already prove here: what
    `ranges_from_push_refs` reads is the local remote-tracking ref, not the
    remote itself, so writing that ref directly is the minimal real
    fixture, not a mock.
    """
    subprocess.run(
        ["git", "-C", str(root), "update-ref", f"refs/remotes/{ref}", commit],
        check=True,
        capture_output=True,
    )


# ---------------------------------------------------------------------------
# hook_body (AC-FUNC-001 / AC-TEST-001)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "hook_name,exec_target",
    [
        pytest.param("pre-commit", "hooks-run", id="pre-commit"),
        pytest.param("pre-push", "hooks-run-push", id="pre-push"),
    ],
)
def test_hook_body_execs_its_target_and_names_the_module(hook_name: str, exec_target: str) -> None:
    """AC-FUNC-001 / AC-TEST-001: each hook execs its own target, carries the authorship marker."""
    githooks = import_githooks()

    body = githooks.hook_body(hook_name)

    assert body.startswith("#!/usr/bin/env sh\n")
    assert f"exec make {exec_target}\n" in body
    assert "devcontainer_config.githooks" in body


def test_hook_body_shares_one_template_between_both_hooks() -> None:
    """AC-FUNC-001: both bodies share the same shebang and authorship marker line."""
    githooks = import_githooks()

    pre_commit = githooks.hook_body("pre-commit")
    pre_push = githooks.hook_body("pre-push")

    pre_commit_lines = pre_commit.splitlines()
    pre_push_lines = pre_push.splitlines()
    assert len(pre_commit_lines) == len(pre_push_lines)
    assert pre_commit_lines[0] == pre_push_lines[0]  # shebang
    assert pre_commit_lines[1] == pre_push_lines[1]  # authorship marker
    assert pre_commit_lines[-1] != pre_push_lines[-1]  # differing exec target


def test_hook_body_rejects_an_unknown_hook_name() -> None:
    """Error Handling Contract: an unrecognized hook name is refused, not silently rendered."""
    githooks = import_githooks()

    with pytest.raises(githooks.GitHooksError, match="unknown hook name"):
        githooks.hook_body("post-checkout")


# ---------------------------------------------------------------------------
# install_hooks / hooks_status (AC-FUNC-002 / AC-FUNC-003 / AC-TEST-002)
# ---------------------------------------------------------------------------


def test_install_hooks_writes_both_hooks_executable(tmp_path: Path) -> None:
    """AC-FUNC-002 / AC-TEST-002: both hooks are written, executable, matching hook_body."""
    githooks = import_githooks()
    root = generated_root(tmp_path)
    init_repo(root)

    written = githooks.install_hooks(root)

    assert len(written) == len(githooks.HOOK_NAMES)
    for hook_name in githooks.HOOK_NAMES:
        path = root / ".git" / "hooks" / hook_name
        assert path.is_file()
        assert path.stat().st_mode & stat.S_IXUSR
        assert path.read_text(encoding="utf-8") == githooks.hook_body(hook_name)


def test_install_hooks_twice_leaves_byte_identical_content(tmp_path: Path) -> None:
    """AC-FUNC-002 / AC-TEST-002: a second install is idempotent."""
    githooks = import_githooks()
    root = generated_root(tmp_path)
    init_repo(root)

    githooks.install_hooks(root)
    first = {
        hook_name: (root / ".git" / "hooks" / hook_name).read_bytes()
        for hook_name in githooks.HOOK_NAMES
    }
    githooks.install_hooks(root)
    second = {
        hook_name: (root / ".git" / "hooks" / hook_name).read_bytes()
        for hook_name in githooks.HOOK_NAMES
    }

    assert first == second


def test_hooks_status_reports_match_after_install(tmp_path: Path) -> None:
    """AC-FUNC-003 / AC-TEST-002: hooks_status reports every hook matching right after install."""
    githooks = import_githooks()
    root = generated_root(tmp_path)
    init_repo(root)

    githooks.install_hooks(root)
    status = githooks.hooks_status(root)

    assert status == {hook_name: True for hook_name in githooks.HOOK_NAMES}


def test_hooks_status_reports_drift_after_the_file_is_edited(tmp_path: Path) -> None:
    """AC-FUNC-003 / AC-TEST-002: an edited hook is reported as drifted, and is not rewritten."""
    githooks = import_githooks()
    root = generated_root(tmp_path)
    init_repo(root)
    githooks.install_hooks(root)
    edited_path = root / ".git" / "hooks" / "pre-commit"
    edited_content = edited_path.read_text(encoding="utf-8") + "# a developer's own line\n"
    edited_path.write_text(edited_content, encoding="utf-8")

    status = githooks.hooks_status(root)

    assert status["pre-commit"] is False
    assert status["pre-push"] is True
    assert edited_path.read_text(encoding="utf-8") == edited_content


def test_hooks_status_reports_missing_hooks_as_not_matching(tmp_path: Path) -> None:
    """AC-FUNC-003: a hook that was never installed counts as not matching, not an error."""
    githooks = import_githooks()
    root = generated_root(tmp_path)
    init_repo(root)

    status = githooks.hooks_status(root)

    assert status == {hook_name: False for hook_name in githooks.HOOK_NAMES}


def test_install_hooks_errors_on_a_non_utf8_existing_hook(tmp_path: Path) -> None:
    """Error Handling Contract: a non-UTF-8 existing hook is refused, not an unhandled traceback."""
    githooks = import_githooks()
    root = generated_root(tmp_path)
    init_repo(root)
    hooks_dir = root / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    (hooks_dir / "pre-commit").write_bytes(b"#!/usr/bin/env sh\n# \xff\xfe not utf-8\n")

    with pytest.raises(githooks.GitHooksError) as exc_info:
        githooks.install_hooks(root)

    message = str(exc_info.value)
    assert message.startswith("ERROR:")
    assert "not valid UTF-8" in message
    assert "hooks-install" in message


def test_hooks_status_errors_on_a_non_utf8_existing_hook(tmp_path: Path) -> None:
    """Error Handling Contract: hooks_status refuses a non-UTF-8 hook rather than crashing."""
    githooks = import_githooks()
    root = generated_root(tmp_path)
    init_repo(root)
    hooks_dir = root / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    (hooks_dir / "pre-push").write_bytes(b"#!/usr/bin/env sh\n# \xff\xfe not utf-8\n")

    with pytest.raises(githooks.GitHooksError) as exc_info:
        githooks.hooks_status(root)

    message = str(exc_info.value)
    assert message.startswith("ERROR:")
    assert "not valid UTF-8" in message
    assert "hooks-check" in message


# ---------------------------------------------------------------------------
# ranges_from_push_refs (AC-FUNC-005 / AC-FUNC-006 / AC-TEST-003)
# ---------------------------------------------------------------------------

# Each case is a callable rather than a plain data tuple: the expected
# ranges for the new-branch cases depend on values only known at test run
# time (a fresh `_fake_sha()` per case, and this repository's own
# empty-tree object id), so the case builds both the stdin text and the
# expected ranges together, given the two things every case might need.
_RangeCaseBuilder = Callable[[Callable[[], str], str], tuple[str, tuple[str, ...]]]


def _ordinary_update_case(
    fake_sha: Callable[[], str], empty_tree: str
) -> tuple[str, tuple[str, ...]]:
    """An ordinary ref update yields '<remote sha>..<local sha>'."""
    local_sha = fake_sha()
    remote_sha = fake_sha()
    stdin_text = _push_ref_line("refs/heads/feature", local_sha, "refs/heads/feature", remote_sha)
    return stdin_text, (f"{remote_sha}..{local_sha}",)


def _new_branch_no_remote_known_case(
    fake_sha: Callable[[], str], empty_tree: str
) -> tuple[str, tuple[str, ...]]:
    """A new branch, all-zero remote id, no known remote ref, yields the empty-tree form."""
    local_sha = fake_sha()
    stdin_text = _push_ref_line(
        "refs/heads/new-branch", local_sha, "refs/heads/new-branch", _ZERO_SHA
    )
    return stdin_text, (f"{empty_tree}..{local_sha}",)


def _delete_case(fake_sha: Callable[[], str], empty_tree: str) -> tuple[str, tuple[str, ...]]:
    """A deleted ref (all-zero local id) contributes no range."""
    stdin_text = _push_ref_line("refs/heads/deleted", _ZERO_SHA, "refs/heads/deleted", fake_sha())
    return stdin_text, ()


def _multi_ref_case(fake_sha: Callable[[], str], empty_tree: str) -> tuple[str, tuple[str, ...]]:
    """Several refs in one push yield one range each, in git's order, deletes contributing none."""
    ordinary_local = fake_sha()
    ordinary_remote = fake_sha()
    new_branch_local = fake_sha()
    stdin_text = "\n".join(
        [
            _push_ref_line("refs/heads/a", ordinary_local, "refs/heads/a", ordinary_remote),
            _push_ref_line("refs/heads/b", new_branch_local, "refs/heads/b", _ZERO_SHA),
            _push_ref_line("refs/heads/c", _ZERO_SHA, "refs/heads/c", fake_sha()),
        ]
    )
    return stdin_text, (
        f"{ordinary_remote}..{ordinary_local}",
        f"{empty_tree}..{new_branch_local}",
    )


def _two_new_branches_share_one_base_case(
    fake_sha: Callable[[], str], empty_tree: str
) -> tuple[str, tuple[str, ...]]:
    """Two all-zero-remote refs in one push both resolve to the same base."""
    first_local = fake_sha()
    second_local = fake_sha()
    stdin_text = "\n".join(
        [
            _push_ref_line("refs/heads/a", first_local, "refs/heads/a", _ZERO_SHA),
            _push_ref_line("refs/heads/b", second_local, "refs/heads/b", _ZERO_SHA),
        ]
    )
    return stdin_text, (
        f"{empty_tree}..{first_local}",
        f"{empty_tree}..{second_local}",
    )


def _blank_lines_ignored_case(
    fake_sha: Callable[[], str], empty_tree: str
) -> tuple[str, tuple[str, ...]]:
    """ranges_from_push_refs tolerates the trailing blank line git's own stdin carries."""
    local_sha = fake_sha()
    remote_sha = fake_sha()
    stdin_text = (
        _push_ref_line("refs/heads/feature", local_sha, "refs/heads/feature", remote_sha) + "\n\n"
    )
    return stdin_text, (f"{remote_sha}..{local_sha}",)


@pytest.mark.parametrize(
    "build_case",
    [
        pytest.param(_ordinary_update_case, id="ordinary_update"),
        pytest.param(_new_branch_no_remote_known_case, id="new_branch_no_remote_known"),
        pytest.param(_delete_case, id="delete"),
        pytest.param(_multi_ref_case, id="multi_ref"),
        pytest.param(_two_new_branches_share_one_base_case, id="two_new_branches_share_one_base"),
        pytest.param(_blank_lines_ignored_case, id="blank_lines_ignored"),
    ],
)
def test_ranges_from_push_refs_derives_expected_ranges(
    tmp_path: Path, build_case: _RangeCaseBuilder
) -> None:
    """AC-FUNC-005 / AC-FUNC-006 / AC-TEST-003: every push-ref case yields the expected ranges.

    The empty-tree base every new-branch case needs is derived
    independently by `_empty_tree_object_id_for_test`, a direct `git
    hash-object` call, never by calling `ranges_from_push_refs` or any
    production helper for it, so a wrong production value could not also
    make its own expectation wrong. None of these repositories has a
    remote-tracking ref, so every all-zero-remote case here falls back to
    the empty-tree form; `test_new_branch_forked_from_existing_history_...`
    below covers the case where a remote-tracking ref is known.
    """
    githooks = import_githooks()
    root = generated_root(tmp_path)
    init_repo(root)
    empty_tree = _empty_tree_object_id_for_test(root)

    stdin_text, expected_ranges = build_case(_fake_sha, empty_tree)
    ranges = githooks.ranges_from_push_refs(stdin_text, root)

    assert ranges == expected_ranges


def test_new_branch_forked_from_existing_history_excludes_already_pushed_commits(
    tmp_path: Path,
) -> None:
    """AC-FUNC-005: the not-on-any-remote form excludes history a remote-tracking ref already has.

    Reproduces the defect a prior version of this function had: it must
    not fall back to "every commit reachable from the local tip" for an
    all-zero remote object id when this repository already knows a
    remote-tracking ref that shares ancestry with the local tip. A base
    commit is committed and pointed at by a simulated `refs/remotes/origin/main`
    (as a prior push would have left it); the branch now being pushed for
    the first time is that same base plus one new commit. The derived
    range must scan only the new commit, not the shared base.
    """
    githooks = import_githooks()
    root = generated_root(tmp_path)
    init_repo(root)
    base_commit = commit_text(root, "README.md", "base\n", "already-pushed base commit")
    _set_remote_tracking_ref(root, "origin/main", base_commit)
    local_tip = commit_text(root, "src/new_feature.py", "print('new')\n", "new local-only commit")
    stdin_text = _push_ref_line("refs/heads/feature", local_tip, "refs/heads/feature", _ZERO_SHA)

    ranges = githooks.ranges_from_push_refs(stdin_text, root)

    assert ranges == (f"{base_commit}..{local_tip}",)


def test_new_branch_sharing_no_history_with_any_remote_yields_the_empty_tree_form(
    tmp_path: Path,
) -> None:
    """AC-FUNC-005: a remote-tracking ref with no shared ancestry falls back to the empty-tree form.

    `git merge-base` itself exits 1 with no stderr when the named commits
    share no common ancestor at all -- a valid result, not a git failure --
    and `ranges_from_push_refs` must treat that the same as "no
    remote-tracking ref known": nothing is provably already pushed, so the
    whole local history is scanned rather than silently narrowed. The
    unrelated history is a second root commit on an orphan branch of the
    same repository (`git checkout --orphan`), the minimal real fixture for
    "shares no ancestor with the current branch at all."
    """
    githooks = import_githooks()
    root = generated_root(tmp_path)
    init_repo(root)
    local_tip = commit_text(
        root, "src/feature.py", "print('a')\n", "root commit, no shared history"
    )
    subprocess.run(
        ["git", "-C", str(root), "checkout", "--orphan", "unrelated"],
        check=True,
        capture_output=True,
    )
    unrelated_commit = commit_text(root, "other.py", "print('b')\n", "unrelated orphan root commit")
    _set_remote_tracking_ref(root, "origin/main", unrelated_commit)
    empty_tree = _empty_tree_object_id_for_test(root)
    stdin_text = _push_ref_line("refs/heads/feature", local_tip, "refs/heads/feature", _ZERO_SHA)

    ranges = githooks.ranges_from_push_refs(stdin_text, root)

    assert ranges == (f"{empty_tree}..{local_tip}",)


def test_not_on_any_remote_base_raises_when_git_is_not_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Error Handling Contract: no git on PATH is refused while deriving the merge-base.

    `remote_ids` is fetched first, while git is still on PATH, so this
    exercises `_not_on_any_remote_base`'s own `FileNotFoundError` handling
    around the `git merge-base` call specifically, not the earlier
    `_remote_tracking_object_ids` lookup.
    """
    githooks = import_githooks()
    root = generated_root(tmp_path)
    init_repo(root)
    local_tip = commit_text(root, "README.md", "base\n", "root commit")
    _set_remote_tracking_ref(root, "origin/main", local_tip)
    remote_ids = githooks._remote_tracking_object_ids(root)
    monkeypatch.setenv("PATH", "")

    with pytest.raises(githooks.GitHooksError, match="git is not installed"):
        githooks._not_on_any_remote_base(root, local_tip, remote_ids)


def test_not_on_any_remote_base_raises_when_merge_base_reports_a_real_failure(
    tmp_path: Path,
) -> None:
    """Error Handling Contract: a genuine git merge-base failure is refused, naming it.

    A well-formed but unresolvable object id makes `git merge-base` exit
    non-zero with real stderr ("Not a valid commit name"), the case
    `_not_on_any_remote_base` must distinguish from the "no common
    ancestor" exit code 1 with no stderr, which is not an error.
    """
    githooks = import_githooks()
    root = generated_root(tmp_path)
    init_repo(root)
    local_tip = commit_text(root, "README.md", "base\n", "root commit")
    unresolvable_remote_id = _fake_sha()

    with pytest.raises(githooks.GitHooksError) as exc_info:
        githooks._not_on_any_remote_base(root, local_tip, (unresolvable_remote_id,))

    message = str(exc_info.value)
    assert message.startswith("ERROR:")
    assert "not-on-any-remote base" in message


# ---------------------------------------------------------------------------
# Error Handling Contract (AC-TEST-004)
# ---------------------------------------------------------------------------


def test_install_hooks_errors_when_the_hooks_directory_cannot_be_created(tmp_path: Path) -> None:
    """Error Handling Contract: .git/hooks absent and not creatable, ERROR with a remedy.

    `git init` pre-creates `.git/hooks` with its own sample scripts, so this
    removes that directory first and then makes `.git` itself unwritable,
    which is what actually forces `hooks_dir.mkdir` to fail: chmoding
    `.git/hooks` alone would not, because writing new files into an
    already-existing, normally-permissioned directory needs no permission
    on its parent.
    """
    githooks = import_githooks()
    root = generated_root(tmp_path)
    init_repo(root)
    git_dir = root / ".git"
    hooks_dir = git_dir / "hooks"
    for sample in hooks_dir.iterdir():
        sample.unlink()
    hooks_dir.rmdir()
    git_dir.chmod(0o500)

    try:
        with pytest.raises(githooks.GitHooksError) as exc_info:
            githooks.install_hooks(root)
        message = str(exc_info.value)
        assert message.startswith("ERROR:")
        assert "writable" in message.lower()
    finally:
        git_dir.chmod(0o700)


def test_install_hooks_errors_when_writing_a_stale_authored_hook_fails(tmp_path: Path) -> None:
    """Error Handling Contract: a write failure on a stale, self-authored hook is refused too.

    Unlike the foreign-hook case, a hook that already carries this module's
    authorship marker but has drifted is allowed to be rewritten; this
    proves that path also converts an OS failure into a `GitHooksError`
    rather than letting a bare `OSError` escape.
    """
    githooks = import_githooks()
    root = generated_root(tmp_path)
    init_repo(root)
    hooks_dir = root / ".git" / "hooks"
    stale_hook = hooks_dir / "pre-commit"
    stale_hook.write_text(githooks.hook_body("pre-commit") + "# stale\n", encoding="utf-8")
    hooks_dir.chmod(0o500)

    try:
        with pytest.raises(githooks.GitHooksError) as exc_info:
            githooks.install_hooks(root)
        message = str(exc_info.value)
        assert message.startswith("ERROR:")
        assert "writable" in message.lower()
    finally:
        hooks_dir.chmod(0o700)


def test_install_hooks_refuses_a_hook_it_did_not_write(tmp_path: Path) -> None:
    """Error Handling Contract: a foreign hook is refused, naming the path and a remedy."""
    githooks = import_githooks()
    root = generated_root(tmp_path)
    init_repo(root)
    hooks_dir = root / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    foreign_hook = hooks_dir / "pre-commit"
    foreign_hook.write_text("#!/usr/bin/env sh\necho a developer's own hook\n", encoding="utf-8")

    with pytest.raises(githooks.GitHooksError) as exc_info:
        githooks.install_hooks(root)

    message = str(exc_info.value)
    assert message.startswith("ERROR:")
    assert "pre-commit" in message
    assert "not written by devcontainer_config.githooks" in message
    assert "re-run" in message.lower()


def test_ranges_from_push_refs_errors_on_a_malformed_line(tmp_path: Path) -> None:
    """Error Handling Contract: a stdin line missing a field is refused, not silently parsed."""
    githooks = import_githooks()
    root = generated_root(tmp_path)
    init_repo(root)

    with pytest.raises(githooks.GitHooksError) as exc_info:
        githooks.ranges_from_push_refs("refs/heads/feature only-two-fields", root)

    message = str(exc_info.value)
    assert message.startswith("ERROR:")
    assert "malformed" in message.lower()
    assert "report this as a bug" in message.lower()


# ---------------------------------------------------------------------------
# Makefile wiring (AC-FUNC-004 / AC-FUNC-007 / AC-TEST-005)
# ---------------------------------------------------------------------------


def _makefile_text() -> str:
    """The repository root `Makefile`, read fresh for every call."""
    root = find_root(Path(__file__).resolve().parent)
    return (root / "Makefile").read_text(encoding="utf-8")


def _recipe_body(makefile_text: str, target: str) -> str:
    """The recipe lines (tab-indented) that follow `target:`'s header line."""
    match = re.search(rf"^{re.escape(target)}:.*\n((?:\t.*\n?)*)", makefile_text, re.MULTILINE)
    assert match is not None, f"no {target} target found in Makefile"
    return match.group(1)


def test_hooks_install_recipe_has_no_hook_body_text_of_its_own() -> None:
    """AC-FUNC-007 / AC-TEST-005: hooks-install delegates, it does not write hook text itself."""
    recipe = _recipe_body(_makefile_text(), "hooks-install")

    assert "#!/usr/bin/env sh" not in recipe
    assert "exec make hooks-run" not in recipe
    assert "devcontainer_config.cli" in recipe
    assert "hooks-install" in recipe


def test_hooks_run_push_target_runs_lint_then_the_pre_push_command() -> None:
    """AC-FUNC-004 / AC-TEST-005: hooks-run-push depends on lint and execs hooks-pre-push."""
    makefile_text = _makefile_text()

    match = re.search(r"^hooks-run-push:(.*)$", makefile_text, re.MULTILINE)
    assert match is not None, "no hooks-run-push target found in Makefile"
    assert "lint" in match.group(1).split()

    recipe = _recipe_body(makefile_text, "hooks-run-push")
    assert "hooks-pre-push" in recipe


def test_hooks_run_push_target_is_phony() -> None:
    """hooks-run-push is declared .PHONY, the same as every other command target."""
    match = re.search(r"^\.PHONY:(.*?)(?=^\S|\Z)", _makefile_text(), re.MULTILINE | re.DOTALL)
    assert match is not None, "no .PHONY declaration found in Makefile"
    phony_targets = set(match.group(1).replace("\\\n", " ").split())
    assert "hooks-run-push" in phony_targets
