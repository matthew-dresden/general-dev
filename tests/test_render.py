"""Tests for devcontainer_config.render (spec Section 5.2).

The `devcontainer_config.render` import is deferred into function bodies (via
`_import_render`) instead of done once at module scope, for the same reason
`tests/test_repo.py` defers it: the TDD RED gate stashes this unit's
production-source files and re-runs a single named test node, and a
module-level `from devcontainer_config import render` would fail COLLECTION
for the whole file (pytest exit code 2, no test outcome recorded) instead of
failing the one test that actually exercises the missing module (pytest exit
1, a real FAILED result). `devcontainer_config.repo` and
`devcontainer_config.answers` are imported at module scope because neither is
part of this work unit's Changes Manifest, so the RED gate never stashes
either one.

`_example_root` builds every fixture tree by copying the three real
`.example` files this repository actually ships -- `shell.env.example`,
`devcontainer-environment-variables.json.example` and
`.devcontainer/aws-profile-map.json.example` -- from the checkout resolved by
`repo.find_root`, into a generated subdirectory of `tmp_path`. No test in
this file reconstructs example content as a literal string: a renderer that
only works against an invented shape would not be proven to work against the
files this repository ships (AC-TEST-001).

`_valid_local_payload` and `_valid_remote_payload` are imported from
`tests/conftest.py`, shared with `tests/test_answers.py`, rather than
defined here: both files need a complete, independently-valid answer set
shaped like the real committed examples, and a required field added to
`answers.FIELDS` must not be able to update one file's copy and miss the
other. Neither builder takes render-specific parameters (an apostrophe in
`developer_name`, an `aws_profiles` list with two named entries, an
`remote_ssh_key_path` under a generated home directory): a test that needs
one of those mutates the dict the builder returns, the same pattern
`tests/test_answers.py` already uses for its own per-test variations,
rather than growing the shared builders' parameter surface for one caller.
"""

from __future__ import annotations

import ast
import importlib
import json
import os
import re
import stat
import subprocess
from pathlib import Path
from types import ModuleType

import conftest
import pytest
from conftest import (
    _example_root,
    _generated_dir,
    _synthetic_account_id,
    _valid_aws_profile,
    _valid_local_payload,
    _valid_remote_payload,
)
from devcontainer_config import answers as answers_module
from devcontainer_config import repo

_PLACEHOLDER_PATTERN = re.compile(r"<[^<>]*>")
# Deliberately excludes commented-out lines: the example's free-form
# "Project-specific (optional)" section ships a commented sample
# (DEVBENCH_NOTIFICATIONS_SLACK_WEBHOOK_URL) that no answers.Field governs
# and render never touches, so its bracketed placeholder is expected to
# survive rendering untouched. AC-CYCLE-001's "no placeholder remains"
# property is about the active configuration the container actually uses.
_ACTIVE_EXPORT_LINE_PATTERN = re.compile(r"^export[ \t]+\w+=.*$", re.MULTILINE)


def _import_render() -> ModuleType:
    """Import devcontainer_config.render from inside a function body.

    See the module docstring for why this is not a module-level import.
    """
    return importlib.import_module("devcontainer_config.render")


def _export_value(text: str, variable: str) -> str:
    """The literal right-hand side of an uncommented `export VARIABLE=...` line."""
    match = re.search(rf"^export {re.escape(variable)}=(.*)$", text, re.MULTILINE)
    assert match is not None, f"no active 'export {variable}=' line in rendered text"
    return match.group(1)


def _commented_export_lines(text: str, variable: str) -> list[str]:
    return re.findall(rf"^[ \t]*#[ \t]*export[ \t]+{re.escape(variable)}=.*$", text, re.MULTILINE)


def _active_export_lines(text: str, variable: str) -> list[str]:
    return re.findall(rf"^export[ \t]+{re.escape(variable)}=.*$", text, re.MULTILINE)


def _sourced_value(shell_env_path: Path, variable: str) -> str:
    """Sources shell_env_path in a real shell and returns the named variable's value."""
    completed = subprocess.run(
        ["bash", "-c", f'source "{shell_env_path}" && printf %s "${variable}"'],
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout


# ---------------------------------------------------------------------------
# AC-FUNC-001 / AC-FUNC-002 / AC-FUNC-003: render_all's contract.
# ---------------------------------------------------------------------------


def test_render_all_returns_content_for_exactly_the_three_private_files(tmp_path: Path) -> None:
    render = _import_render()
    root = _example_root(tmp_path)
    home = _generated_dir(tmp_path, "home")

    rendered = render.render_all(_valid_local_payload(), root, home)

    assert set(rendered) == set(repo.PRIVATE_FILES)


def test_render_all_validates_before_checking_examples(tmp_path: Path) -> None:
    """AC-FUNC-002: an invalid payload raises AnswerError, not a missing-example RenderError.

    `root` here has no examples at all copied into it. If `render_all`
    checked example presence before validating, this would raise
    `RenderError`; observing `AnswerError` instead proves validation runs
    first.
    """
    render = _import_render()
    root = _generated_dir(tmp_path, "checkout")
    home = _generated_dir(tmp_path, "home")

    with pytest.raises(answers_module.AnswerError):
        render.render_all({}, root, home)


def test_render_all_raises_naming_every_missing_example(tmp_path: Path) -> None:
    """AC-FUNC-003 / AC-TEST-003 (absent example)."""
    render = _import_render()
    root = _generated_dir(tmp_path, "checkout")
    home = _generated_dir(tmp_path, "home")

    with pytest.raises(render.RenderError) as excinfo:
        render.render_all(_valid_local_payload(), root, home)

    for relative in repo.PRIVATE_FILES:
        assert repo.example_for(relative) in str(excinfo.value)


def test_render_all_raises_naming_only_the_missing_examples(tmp_path: Path) -> None:
    """AC-FUNC-003: a partially-present example set names only what is absent."""
    render = _import_render()
    root = _example_root(tmp_path)
    home = _generated_dir(tmp_path, "home")
    present_example = repo.example_for(repo.SHELL_ENV)
    for relative in repo.PRIVATE_FILES:
        if repo.example_for(relative) != present_example:
            (root / repo.example_for(relative)).unlink()

    with pytest.raises(render.RenderError) as excinfo:
        render.render_all(_valid_local_payload(), root, home)

    message = str(excinfo.value)
    assert present_example not in message
    for relative in repo.PRIVATE_FILES:
        example = repo.example_for(relative)
        if example != present_example:
            assert example in message


# ---------------------------------------------------------------------------
# AC-4.2 / AC-TEST-004: atomicity.
# ---------------------------------------------------------------------------


def test_invalid_payload_leaves_no_file_written(tmp_path: Path) -> None:
    """AC-4.2 / AC-TEST-004: an aborted render leaves the root untouched."""
    render = _import_render()
    root = _example_root(tmp_path)
    home = _generated_dir(tmp_path, "home")

    with pytest.raises(answers_module.AnswerError):
        render.render_all({}, root, home)

    for relative in repo.PRIVATE_FILES:
        assert not (root / relative).exists()


# ---------------------------------------------------------------------------
# AC-FUNC-004: header replacement.
# ---------------------------------------------------------------------------


def test_render_shell_env_replaces_header_and_preserves_marker_onward(tmp_path: Path) -> None:
    render = _import_render()
    root = _example_root(tmp_path)
    home = _generated_dir(tmp_path, "home")
    example = (root / repo.example_for(repo.SHELL_ENV)).read_text(encoding="utf-8")
    marker_index = example.find(render.SHELL_ENV_HEADER_MARKER)
    assert marker_index != -1, "fixture example is missing the section marker this test proves"

    rendered = render.render_shell_env(
        answers_module.ensure_valid(_valid_local_payload()), example, root, home
    )

    assert "cp shell.env.example shell.env" not in rendered
    assert rendered.startswith(render.RENDERED_HEADER)
    assert render.SHELL_ENV_HEADER_MARKER in rendered
    assert "Identity, always set" in rendered
    # A comment render never touches survives byte-for-byte within the
    # preserved suffix, proving the replacement stops at the marker rather
    # than rewriting everything below it.
    untouched_comment = "# Marks the shell as running inside the devcontainer."
    assert untouched_comment in example[marker_index:]
    assert untouched_comment in rendered


def test_replace_header_raises_when_marker_missing() -> None:
    """AC-TEST-003 (missing marker)."""
    render = _import_render()

    with pytest.raises(render.RenderError, match=re.escape(repo.example_for(repo.SHELL_ENV))):
        render.replace_header("no marker anywhere in this text\n")


def test_rendered_header_does_not_claim_every_value_is_a_secret() -> None:
    """docs/environment-files.md's Secrets section: shell.env carries identity
    and configuration and no credential, so the generated header must not
    misdirect a developer into treating an ordinary value as a secret, or
    worse, into putting a real credential in a file this repository pushes
    to Parameter Store as a plain `String`, not a `SecureString`.
    """
    render = _import_render()
    # Header lines wrap at column ~80 and each carries a leading '# ' shell
    # comment marker, so a phrase spanning a wrap is broken by both a
    # newline and the next line's marker in the raw string. Stripping each
    # line's marker and joining with a single space lets this assertion
    # survive re-wrapping.
    collapsed_header = " ".join(
        line.lstrip("#").strip() for line in render.RENDERED_HEADER.splitlines()
    )

    assert "treat every value here as a secret" not in collapsed_header.lower()
    assert "never a credential" in collapsed_header
    assert "devsecret set" in collapsed_header


# ---------------------------------------------------------------------------
# AC-FUNC-005 / AC-TEST-002: single-quoted values, apostrophe round trip.
# ---------------------------------------------------------------------------


def test_render_shell_env_quotes_every_value(tmp_path: Path) -> None:
    render = _import_render()
    root = _example_root(tmp_path)
    home = _generated_dir(tmp_path, "home")
    example = (root / repo.example_for(repo.SHELL_ENV)).read_text(encoding="utf-8")
    payload = _valid_local_payload()

    rendered = render.render_shell_env(answers_module.ensure_valid(payload), example, root, home)

    assert _export_value(rendered, "DEVELOPER_NAME") == render.shell_quote(
        payload["developer_name"]
    )
    assert _export_value(rendered, "GIT_USER") == render.shell_quote(payload["git_user"])


def test_render_shell_env_apostrophe_round_trips_through_a_real_shell(tmp_path: Path) -> None:
    """AC-TEST-002."""
    render = _import_render()
    root = _example_root(tmp_path)
    home = _generated_dir(tmp_path, "home")
    example = (root / repo.example_for(repo.SHELL_ENV)).read_text(encoding="utf-8")
    apostrophe_name = "Pat O'Brien"
    payload = _valid_local_payload()
    payload["developer_name"] = apostrophe_name

    rendered = render.render_shell_env(
        answers_module.ensure_valid(payload),
        example,
        root,
        home,
    )
    shell_env_path = root / repo.SHELL_ENV
    shell_env_path.write_text(rendered, encoding="utf-8")

    assert _sourced_value(shell_env_path, "DEVELOPER_NAME") == apostrophe_name


# ---------------------------------------------------------------------------
# AC-FUNC-006: BASH_ENV derived from repo.container_workspace.
# ---------------------------------------------------------------------------


def test_render_shell_env_bash_env_matches_container_workspace(tmp_path: Path) -> None:
    render = _import_render()
    root = _example_root(tmp_path)
    home = _generated_dir(tmp_path, "home")
    example = (root / repo.example_for(repo.SHELL_ENV)).read_text(encoding="utf-8")

    rendered = render.render_shell_env(
        answers_module.ensure_valid(_valid_local_payload()), example, root, home
    )

    expected = f"'{repo.container_workspace(root)}/{repo.SHELL_ENV}'"
    assert _export_value(rendered, "BASH_ENV") == expected


# ---------------------------------------------------------------------------
# AC-FUNC-007: remote block populated on remote, commented on local.
# ---------------------------------------------------------------------------


def test_backend_remote_constant_stays_in_step_with_answers_backends() -> None:
    """`render._BACKEND_REMOTE`'s module comment claims this file's own test
    asserts membership directly; this is that test. Without it, renaming the
    literal `answers_module.BACKENDS` declares for the remote backend would
    make `render_shell_env` silently take the local (else) branch for a
    remote payload -- commenting out the entire remote block with no error.
    """
    render = _import_render()

    assert render._BACKEND_REMOTE in answers_module.BACKENDS


def test_render_shell_env_remote_block_populated_on_remote_payload(tmp_path: Path) -> None:
    render = _import_render()
    root = _example_root(tmp_path)
    home = _generated_dir(tmp_path, "home")
    example = (root / repo.example_for(repo.SHELL_ENV)).read_text(encoding="utf-8")
    payload = _valid_remote_payload()
    payload["remote_ssh_key_path"] = str(home / ".ssh" / "example-key.pem")

    rendered = render.render_shell_env(answers_module.ensure_valid(payload), example, root, home)

    assert _export_value(rendered, "REMOTE_INSTANCE_ID") == f"'{payload['remote_instance_id']}'"
    assert _export_value(rendered, "REMOTE_AWS_REGION") == f"'{payload['remote_aws_region']}'"
    assert _export_value(rendered, "REMOTE_AWS_PROFILE") == f"'{payload['remote_aws_profile']}'"
    ssh_line = _active_export_lines(rendered, "REMOTE_SSH_KEY_PATH")
    assert ssh_line, "REMOTE_SSH_KEY_PATH must be an active (uncommented) export"
    assert '"${HOME}/.ssh/example-key.pem"' in ssh_line[0]


def test_render_shell_env_remote_ssh_key_outside_home_falls_back_to_quoted_literal(
    tmp_path: Path,
) -> None:
    render = _import_render()
    root = _example_root(tmp_path)
    home = _generated_dir(tmp_path, "home")
    outside = _generated_dir(tmp_path, "outside-home")
    example = (root / repo.example_for(repo.SHELL_ENV)).read_text(encoding="utf-8")
    payload = _valid_remote_payload()
    payload["remote_ssh_key_path"] = str(outside / "keys" / "example-key.pem")

    rendered = render.render_shell_env(answers_module.ensure_valid(payload), example, root, home)

    expected_path = str(outside / "keys" / "example-key.pem")
    assert _export_value(rendered, "REMOTE_SSH_KEY_PATH") == f"'{expected_path}'"


def test_render_shell_env_remote_block_commented_on_local_payload(tmp_path: Path) -> None:
    render = _import_render()
    root = _example_root(tmp_path)
    home = _generated_dir(tmp_path, "home")
    example = (root / repo.example_for(repo.SHELL_ENV)).read_text(encoding="utf-8")

    rendered = render.render_shell_env(
        answers_module.ensure_valid(_valid_local_payload()), example, root, home
    )

    for variable in (
        "REMOTE_INSTANCE_ID",
        "REMOTE_SSH_KEY_PATH",
        "REMOTE_AWS_REGION",
        "REMOTE_AWS_PROFILE",
    ):
        assert _commented_export_lines(rendered, variable), f"{variable} must stay commented out"
        assert not _active_export_lines(rendered, variable), f"{variable} must not be active"


# ---------------------------------------------------------------------------
# AC-FUNC-008 / AC-TEST-003 (missing export line): set_export / comment_export.
# ---------------------------------------------------------------------------


def test_set_export_raises_naming_the_variable_when_no_matching_line() -> None:
    render = _import_render()

    with pytest.raises(render.RenderError, match="NO_SUCH_VARIABLE"):
        render.set_export("export OTHER_VAR='x'\n", "NO_SUCH_VARIABLE", "'y'")


def test_set_export_does_not_match_a_variable_with_a_shared_prefix() -> None:
    """A regex that matched on prefix alone could silently set the wrong variable."""
    render = _import_render()

    with pytest.raises(render.RenderError, match="no 'export FOO=' line"):
        render.set_export("export FOO_BAR=1\n", "FOO", "'bar'")


def test_comment_export_raises_naming_the_variable_when_no_matching_line() -> None:
    render = _import_render()

    with pytest.raises(render.RenderError, match="NO_SUCH_VARIABLE"):
        render.comment_export("export OTHER_VAR='x'\n", "NO_SUCH_VARIABLE")


def test_set_export_uncomments_a_commented_line() -> None:
    render = _import_render()

    result = render.set_export("# export FOO=''\n", "FOO", "'bar'")

    assert result == "export FOO='bar'\n"


def test_comment_export_is_idempotent_on_an_already_commented_line() -> None:
    render = _import_render()

    result = render.comment_export("  # export FOO='bar'\n", "FOO")

    assert result == "  # export FOO='bar'\n"


# ---------------------------------------------------------------------------
# AC-FUNC-009 / AC-TEST-003 (unparsable JSON, missing containerEnv):
# devcontainer-environment-variables.json.
# ---------------------------------------------------------------------------


def test_render_devcontainer_env_json_parses_carries_answers_no_template_path(
    tmp_path: Path,
) -> None:
    render = _import_render()
    root = _example_root(tmp_path)
    example = (root / repo.example_for(repo.DEVCONTAINER_ENV_JSON)).read_text(encoding="utf-8")
    payload = _valid_local_payload(host_proxy=True)
    validated = answers_module.ensure_valid(payload)

    rendered = render.render_devcontainer_env_json(validated, example)
    document = json.loads(rendered)

    assert "template_path" not in document
    assert document["template_name"] == payload["template_name"]
    assert document["containerEnv"]["DEVELOPER_NAME"] == payload["developer_name"]
    assert document["containerEnv"]["HOST_PROXY"] == "true"
    assert document["containerEnv"]["HOST_PROXY_URL"] == payload["host_proxy_url"]


def test_render_devcontainer_env_json_host_proxy_off_clears_url(tmp_path: Path) -> None:
    render = _import_render()
    root = _example_root(tmp_path)
    example = (root / repo.example_for(repo.DEVCONTAINER_ENV_JSON)).read_text(encoding="utf-8")
    validated = answers_module.ensure_valid(_valid_local_payload(host_proxy=False))

    document = json.loads(render.render_devcontainer_env_json(validated, example))

    assert document["containerEnv"]["HOST_PROXY"] == "false"
    assert document["containerEnv"]["HOST_PROXY_URL"] == ""


def test_render_devcontainer_env_json_raises_when_example_unparsable() -> None:
    """AC-TEST-003 (unparsable JSON example)."""
    render = _import_render()
    validated = answers_module.ensure_valid(_valid_local_payload())

    with pytest.raises(
        render.RenderError, match=re.escape(repo.example_for(repo.DEVCONTAINER_ENV_JSON))
    ):
        render.render_devcontainer_env_json(validated, "{not valid json")


def test_render_devcontainer_env_json_raises_when_container_env_missing() -> None:
    """AC-TEST-003 (missing containerEnv)."""
    render = _import_render()
    validated = answers_module.ensure_valid(_valid_local_payload())

    with pytest.raises(
        render.RenderError, match=re.escape(repo.example_for(repo.DEVCONTAINER_ENV_JSON))
    ):
        render.render_devcontainer_env_json(validated, json.dumps({"template_name": "x"}))


# ---------------------------------------------------------------------------
# AC-FUNC-010: aws-profile-map.json.
# ---------------------------------------------------------------------------


def test_render_aws_profile_map_is_empty_object_when_disabled() -> None:
    render = _import_render()
    validated = answers_module.ensure_valid(_valid_local_payload(aws_config_enabled=False))

    rendered = render.render_aws_profile_map(validated)

    assert json.loads(rendered) == {}


def test_render_aws_profile_map_one_object_per_profile_with_six_subfields() -> None:
    """AC-FUNC-010: `name` is consumed as the map key, the other six of the
    seven spec Section 5.1 sub-fields are written under each profile entry.
    """
    render = _import_render()
    dev_profile = _valid_aws_profile(name="dev", account_id=_synthetic_account_id())
    staging_profile = _valid_aws_profile(name="staging", account_id=_synthetic_account_id())
    payload = _valid_local_payload(aws_config_enabled=True)
    payload["aws_profiles"] = [dev_profile, staging_profile]
    validated = answers_module.ensure_valid(payload)

    document = json.loads(render.render_aws_profile_map(validated))

    assert set(document) == {dev_profile["name"], staging_profile["name"]}
    for profile in (dev_profile, staging_profile):
        entry = document[profile["name"]]
        assert set(entry) == set(answers_module.AWS_PROFILE_SUB_FIELDS) - {"name"}
        for sub_field, expected_value in profile.items():
            if sub_field == "name":
                continue
            assert entry[sub_field] == expected_value


# ---------------------------------------------------------------------------
# AC-FUNC-011: write_all mode and return value.
# ---------------------------------------------------------------------------


def test_write_all_writes_mode_0600_and_returns_sorted_paths(tmp_path: Path) -> None:
    render = _import_render()
    root = _example_root(tmp_path)
    home = _generated_dir(tmp_path, "home")
    rendered = render.render_all(_valid_local_payload(), root, home)

    written = render.write_all(rendered, root, overwrite=False)

    assert written == sorted(repo.PRIVATE_FILES)
    for relative in repo.PRIVATE_FILES:
        mode = stat.S_IMODE((root / relative).stat().st_mode)
        assert mode == 0o600, f"{relative} has mode {oct(mode)}, expected 0o600"


def test_write_all_creates_fresh_files_at_0600_under_a_permissive_umask(tmp_path: Path) -> None:
    """CWE-732/CWE-279 regression: a permissive process umask must not widen
    a freshly created file's mode. `Path.write_text` opens with O_CREAT at
    `0o666 & ~umask`, which under umask 0 would create the file at 0o666
    (world-writable) for the instant before a later chmod narrows it. This
    asserts the file is at 0o600 immediately, proving creation itself uses
    the restrictive mode rather than relying on chmod alone.
    """
    render = _import_render()
    root = _example_root(tmp_path)
    home = _generated_dir(tmp_path, "home")
    rendered = render.render_all(_valid_local_payload(), root, home)

    previous_umask = os.umask(0)
    try:
        written = render.write_all(rendered, root, overwrite=False)
    finally:
        os.umask(previous_umask)

    assert written == sorted(repo.PRIVATE_FILES)
    for relative in repo.PRIVATE_FILES:
        mode = stat.S_IMODE((root / relative).stat().st_mode)
        assert mode == 0o600, f"{relative} has mode {oct(mode)}, expected 0o600"


def test_write_all_never_creates_a_file_through_path_write_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`Path.write_text` cannot be used to create these files: it opens with
    O_CREAT at `0o666 & ~umask`, leaving a window where a freshly created
    file exists at a wider mode than 0o600 while carrying its full contents,
    before a subsequent `chmod(0o600)` narrows it (CWE-732/CWE-279). Forcing
    `Path.write_text` to raise proves `write_all` creates every file through
    a path (`os.open` with an explicit `0o600` mode) that never opens this
    window, rather than merely happening to end up at 0o600 by the time the
    test samples the terminal state.
    """
    render = _import_render()
    root = _example_root(tmp_path)
    home = _generated_dir(tmp_path, "home")
    rendered = render.render_all(_valid_local_payload(), root, home)

    def _forbidden_write_text(self: Path, *args: object, **kwargs: object) -> int:
        raise AssertionError(
            f"write_all must not create {self} via Path.write_text; "
            "os.open with an explicit 0o600 mode must be used instead"
        )

    monkeypatch.setattr(Path, "write_text", _forbidden_write_text)

    written = render.write_all(rendered, root, overwrite=False)

    assert written == sorted(repo.PRIVATE_FILES)
    for relative in repo.PRIVATE_FILES:
        mode = stat.S_IMODE((root / relative).stat().st_mode)
        assert mode == 0o600, f"{relative} has mode {oct(mode)}, expected 0o600"


def test_write_all_narrows_a_pre_existing_wide_mode_file_on_overwrite(tmp_path: Path) -> None:
    """The overwrite path must narrow, not merely preserve, a target's mode:
    a file that predates this render (for example hand-edited at the
    default 0o644) is not proof that 0o644 is an acceptable mode for it.
    """
    render = _import_render()
    root = _example_root(tmp_path)
    home = _generated_dir(tmp_path, "home")
    rendered = render.render_all(_valid_local_payload(), root, home)
    for relative in repo.PRIVATE_FILES:
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text("pre-existing hand-edited content", encoding="utf-8")
        destination.chmod(0o644)

    written = render.write_all(rendered, root, overwrite=True)

    assert written == sorted(repo.PRIVATE_FILES)
    for relative in repo.PRIVATE_FILES:
        mode = stat.S_IMODE((root / relative).stat().st_mode)
        assert mode == 0o600, f"{relative} has mode {oct(mode)}, expected 0o600 after overwrite"


# ---------------------------------------------------------------------------
# AC-FUNC-012: write_all refuses to clobber.
# ---------------------------------------------------------------------------


def test_write_all_refuses_without_overwrite_naming_existing_paths(tmp_path: Path) -> None:
    render = _import_render()
    root = _example_root(tmp_path)
    home = _generated_dir(tmp_path, "home")
    rendered = render.render_all(_valid_local_payload(), root, home)
    render.write_all(rendered, root, overwrite=False)
    original_bytes = {relative: (root / relative).read_bytes() for relative in repo.PRIVATE_FILES}

    with pytest.raises(render.RenderError) as excinfo:
        render.write_all(rendered, root, overwrite=False)

    for relative in repo.PRIVATE_FILES:
        assert relative in str(excinfo.value)
    for relative in repo.PRIVATE_FILES:
        assert (root / relative).read_bytes() == original_bytes[relative]


def test_write_all_overwrites_when_overwrite_true(tmp_path: Path) -> None:
    render = _import_render()
    root = _example_root(tmp_path)
    home = _generated_dir(tmp_path, "home")
    first_payload = _valid_local_payload()
    first_payload["developer_name"] = "First Name"
    first_rendered = render.render_all(first_payload, root, home)
    render.write_all(first_rendered, root, overwrite=False)
    second_payload = _valid_local_payload()
    second_payload["developer_name"] = "Second Name"
    second_rendered = render.render_all(second_payload, root, home)

    written = render.write_all(second_rendered, root, overwrite=True)

    assert written == sorted(repo.PRIVATE_FILES)
    shell_env_text = (root / repo.SHELL_ENV).read_text(encoding="utf-8")
    assert "Second Name" in shell_env_text
    assert "First Name" not in shell_env_text


# ---------------------------------------------------------------------------
# AC-CYCLE-001: end-to-end remote render, write, source, parse, no placeholders.
# ---------------------------------------------------------------------------


def test_end_to_end_remote_render_write_source_and_parse(tmp_path: Path) -> None:
    render = _import_render()
    root = _example_root(tmp_path)
    home = _generated_dir(tmp_path, "home")
    payload = _valid_remote_payload()
    payload["remote_ssh_key_path"] = str(home / ".ssh" / "example-key.pem")
    payload["aws_config_enabled"] = True
    payload["aws_profiles"] = [
        _valid_aws_profile(name="dev"),
        _valid_aws_profile(name="staging", account_id=_synthetic_account_id()),
    ]
    # host_proxy_url's value comes from the shared builder's own
    # host_proxy=True default rather than a literal restated here, so this
    # test cannot drift from tests/conftest.py's payload shape.
    payload["host_proxy"] = True
    payload["host_proxy_url"] = _valid_local_payload(host_proxy=True)["host_proxy_url"]

    rendered = render.render_all(payload, root, home)
    written = render.write_all(rendered, root, overwrite=False)

    assert written == sorted(repo.PRIVATE_FILES)

    shell_env_path = root / repo.SHELL_ENV
    source_result = subprocess.run(
        ["bash", "-c", f'source "{shell_env_path}"'],
        capture_output=True,
        text=True,
    )
    assert source_result.returncode == 0, source_result.stderr

    shell_env_text = shell_env_path.read_text(encoding="utf-8")
    active_export_lines = _ACTIVE_EXPORT_LINE_PATTERN.findall(shell_env_text)
    assert active_export_lines, "no active export lines found; render must have produced content"
    for export_line in active_export_lines:
        assert not _PLACEHOLDER_PATTERN.search(export_line), export_line

    devcontainer_env_document = json.loads(
        (root / repo.DEVCONTAINER_ENV_JSON).read_text(encoding="utf-8")
    )
    assert not _PLACEHOLDER_PATTERN.search(json.dumps(devcontainer_env_document))

    aws_profile_map_document = json.loads((root / repo.AWS_PROFILE_MAP).read_text(encoding="utf-8"))
    assert not _PLACEHOLDER_PATTERN.search(json.dumps(aws_profile_map_document))


# ---------------------------------------------------------------------------
# AC-DRY-001 / AC-DRY-002: _generated_dir and _example_root live in conftest.
# ---------------------------------------------------------------------------


def test_shared_fixture_helpers_are_defined_once() -> None:
    """tests/conftest.py owns _generated_dir and _example_root, this file does not.

    tests/test_verify.py needs the identical byte-for-byte fixture builders
    this file uses; carrying independent copies in each file risks silent
    divergence on the next change to repo.PRIVATE_FILES or repo.example_for.
    This pins that conftest exposes both helpers and that this file's own
    parsed source defines neither of them locally, so a re-introduced
    duplicate fails this test instead of drifting unnoticed.
    """
    assert hasattr(conftest, "_generated_dir"), "conftest must define _generated_dir"
    assert hasattr(conftest, "_example_root"), "conftest must define _example_root"

    source = Path(__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    local_helper_names = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name in {"_generated_dir", "_example_root"}
    }
    assert local_helper_names == set(), (
        f"tests/test_render.py must not locally define {local_helper_names}; "
        "import both from conftest instead"
    )
