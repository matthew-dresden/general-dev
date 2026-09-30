"""Tests for devcontainer_config.verify (spec Section 4.5).

The `devcontainer_config.verify` import is deferred into function bodies (via
`_import_verify`) instead of done once at module scope, for the same reason
`tests/test_render.py` defers `devcontainer_config.render`: the TDD RED gate
stashes this unit's production-source files and re-runs a single named test
node, and a module-level `from devcontainer_config import verify` would fail
COLLECTION for the whole file (pytest exit code 2, no test outcome recorded)
instead of failing the one test that actually exercises the missing module
(pytest exit 1, a real FAILED result). `devcontainer_config.repo` and
`devcontainer_config.render` are imported at module scope because neither is
part of this work unit's Changes Manifest, so the RED gate never stashes
either one; `render.render_all` and `render.write_all` are how every fixture
in this file builds its configuration (AC-TEST-001), never by writing
configuration text by hand.

`_valid_local_payload`, `_valid_remote_payload` and `_valid_aws_profile` are
imported from `tests/conftest.py`, shared with `tests/test_render.py` and
`tests/test_answers.py`, rather than redefined here: every file that needs a
complete, independently-valid answer set shares the one builder, so a
required field added to `answers.FIELDS` cannot update some copies and miss
others.

`_generated_dir` and `_example_root` are imported from `tests/conftest.py`,
shared with `tests/test_render.py`, rather than defined here: `_example_root`
iterates `repo.PRIVATE_FILES` and calls `repo.example_for`, so a second,
independent copy could apply a change to one and miss the other
(test_review DRY finding, E1-F2-S2-T2). `_rendered_root` stays local to this
file: it builds on top of `_example_root` by additionally calling
`render.render_all` and `render.write_all`, a `verify`-specific concern no
other test file needs.
"""

from __future__ import annotations

import ast
import importlib
import json
import re
import sys
import uuid
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from conftest import (
    _example_root,
    _generated_dir,
    _valid_aws_profile,
    _valid_local_payload,
    _valid_remote_payload,
)
from devcontainer_config import render as render_module
from devcontainer_config import repo

# A placeholder-shaped token distinct from any value the real `.example`
# files ship, so a test that greps for it cannot accidentally match
# something rendering itself produced.
_PLACEHOLDER_TOKEN = f"<injected-{uuid.uuid4().hex[:8]}>"


def _import_verify() -> ModuleType:
    """Import devcontainer_config.verify from inside a function body.

    See the module docstring for why this is not a module-level import.
    """
    return importlib.import_module("devcontainer_config.verify")


def _rendered_root(tmp_path: Path, payload: dict[str, object]) -> Path:
    """A tmp_path checkout root holding a complete, valid rendered configuration.

    Built by calling `render.render_all` and `render.write_all` (AC-TEST-001),
    never by writing configuration text by hand.
    """
    root = _example_root(tmp_path)
    home = _generated_dir(tmp_path, "home")
    rendered = render_module.render_all(payload, root, home)
    render_module.write_all(rendered, root, overwrite=False)
    return root


def _with_placeholder_appended(text: str, variable: str) -> str:
    """`text` with `_PLACEHOLDER_TOKEN` appended inside `variable`'s quoted value.

    Embeds the token inside an otherwise well-formed value (AC-FUNC-004's
    "token in a value that is otherwise well formed" case) rather than
    replacing the value outright.
    """
    pattern = re.compile(rf"^export {re.escape(variable)}=(?P<value>'.*')$", re.MULTILINE)
    match = pattern.search(text)
    assert match is not None, f"no active 'export {variable}=' line found to corrupt"
    quoted = match.group("value")
    corrupted_value = quoted[:-1] + _PLACEHOLDER_TOKEN + "'"
    return text[: match.start("value")] + corrupted_value + text[match.end("value") :]


def _remove_export(root: Path, variable: str) -> None:
    """Delete `variable`'s active export line entirely from shell.env.

    Asserts the file actually changed, so a `variable` that names no active
    export line fails loudly instead of silently leaving shell.env untouched
    (test_review DRY finding, E1-F2-S2-T2).
    """
    path = root / repo.SHELL_ENV
    text = path.read_text(encoding="utf-8")
    without_export = re.sub(
        rf"^export {re.escape(variable)}=.*\n?",
        "",
        text,
        count=1,
        flags=re.MULTILINE,
    )
    assert without_export != text, f"no active 'export {variable}=' line found to remove"
    path.write_text(without_export, encoding="utf-8")


def _set_export(root: Path, variable: str, raw_value: str) -> None:
    """Rewrite `variable`'s active export line in shell.env to `raw_value` verbatim.

    `raw_value` is substituted exactly as given, unquoted, single-quoted or
    otherwise, so callers control whether the resulting line is well-formed
    (test_review DRY finding, E1-F2-S2-T2).
    """
    path = root / repo.SHELL_ENV
    text = path.read_text(encoding="utf-8")
    replaced = re.sub(
        rf"^export {re.escape(variable)}=.*$",
        f"export {variable}={raw_value}",
        text,
        count=1,
        flags=re.MULTILINE,
    )
    assert replaced != text, f"no active 'export {variable}=' line found to rewrite"
    path.write_text(replaced, encoding="utf-8")


def _inject_shell_env_placeholder(root: Path) -> None:
    """Append `_PLACEHOLDER_TOKEN` into shell.env's DEVCONTAINER value.

    `DEVCONTAINER` shares no key with `containerEnv`, so this corruption
    cannot also trip the identity-consistency check.
    """
    path = root / repo.SHELL_ENV
    text = path.read_text(encoding="utf-8")
    corrupted = _with_placeholder_appended(text, "DEVCONTAINER")
    assert corrupted != text
    path.write_text(corrupted, encoding="utf-8")


def _inject_devcontainer_json_placeholder(root: Path) -> None:
    """Append `_PLACEHOLDER_TOKEN` into devcontainer-environment-variables.json's `cli_version`.

    `cli_version` is outside `containerEnv`, so this corruption cannot also
    trip the identity-consistency check.
    """
    path = root / repo.DEVCONTAINER_ENV_JSON
    document = json.loads(path.read_text(encoding="utf-8"))
    document["cli_version"] = str(document["cli_version"]) + _PLACEHOLDER_TOKEN
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")


def _inject_aws_profile_map_placeholder(root: Path) -> None:
    """Append `_PLACEHOLDER_TOKEN` into the first profile's `role_name`."""
    path = root / repo.AWS_PROFILE_MAP
    document = json.loads(path.read_text(encoding="utf-8"))
    first_profile_name = next(iter(document))
    document[first_profile_name]["role_name"] = (
        str(document[first_profile_name]["role_name"]) + _PLACEHOLDER_TOKEN
    )
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# AC-FUNC-001: a rendered configuration verifies clean.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "payload_factory"),
    [
        ("local", lambda: _valid_local_payload()),
        ("remote", lambda: _valid_remote_payload()),
        ("aws-enabled", lambda: _valid_local_payload(aws_config_enabled=True)),
        ("proxy-enabled", lambda: _valid_local_payload(host_proxy=True)),
    ],
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_verify_all_returns_no_findings_for_a_rendered_configuration(
    tmp_path: Path, label: str, payload_factory: Callable[[], dict[str, Any]]
) -> None:
    verify = _import_verify()
    payload = payload_factory()
    root = _rendered_root(tmp_path, payload)

    assert verify.verify_all(root) == [], f"unexpected findings for the {label!r} payload"


# ---------------------------------------------------------------------------
# AC-FUNC-002 / AC-TEST-002: missing or unreadable private files.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("relative", list(repo.PRIVATE_FILES))
def test_verify_all_finds_missing_private_file(tmp_path: Path, relative: str) -> None:
    verify = _import_verify()
    root = _rendered_root(tmp_path, _valid_local_payload())
    (root / relative).unlink()

    findings = verify.verify_all(root)

    matching = [finding for finding in findings if relative in finding.found]
    assert matching, f"no finding named the missing {relative}: {findings}"
    assert matching[0].check.startswith("completeness")
    assert "setup-local" in matching[0].remedy or "make init" in matching[0].remedy


@pytest.mark.parametrize("relative", list(repo.PRIVATE_FILES))
def test_verify_all_finds_a_private_file_that_is_not_utf8(tmp_path: Path, relative: str) -> None:
    """code_review Finding 2: a private file containing a byte sequence that
    is not valid UTF-8 must produce the same shaped completeness finding as
    a missing file, rather than let `UnicodeDecodeError` propagate out of
    `verify_all` -- the rendered header on every private file explicitly
    invites the operator to hand-edit it, and a stray non-UTF-8 byte from
    that edit is exactly as unreadable as a missing file, not a crash.
    """
    verify = _import_verify()
    root = _rendered_root(tmp_path, _valid_local_payload())
    path = root / relative
    corrupted = path.read_bytes() + b"\xff"
    path.write_bytes(corrupted)

    findings = verify.verify_all(root)

    matching = [finding for finding in findings if relative in finding.found]
    assert matching, f"no finding named the undecodable {relative}: {findings}"
    assert matching[0].check.startswith("completeness")


# ---------------------------------------------------------------------------
# AC-FUNC-003: unparsable JSON, distinct from a completeness finding.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("relative", [repo.DEVCONTAINER_ENV_JSON, repo.AWS_PROFILE_MAP])
def test_verify_all_finds_unparsable_json(tmp_path: Path, relative: str) -> None:
    verify = _import_verify()
    root = _rendered_root(tmp_path, _valid_local_payload())
    (root / relative).write_text("{not valid json", encoding="utf-8")

    findings = verify.verify_all(root)

    matching = [finding for finding in findings if relative in finding.found]
    assert matching, f"no finding named the unparsable {relative}: {findings}"
    assert "parses as JSON" in matching[0].check
    assert not matching[0].check.startswith("consistency")


@pytest.mark.parametrize("relative", [repo.DEVCONTAINER_ENV_JSON, repo.AWS_PROFILE_MAP])
def test_verify_all_finds_json_that_parses_but_is_not_an_object(
    tmp_path: Path, relative: str
) -> None:
    """code_review Finding 2: a private JSON file that parses cleanly but is
    not a JSON object (a list, here) must produce a shaped completeness
    finding, distinct from `_json_parse_finding`'s syntax-error case, rather
    than crash `verify_all` with an `AttributeError` from a later check
    calling `.get`/`.items()` on a non-mapping.
    """
    verify = _import_verify()
    root = _rendered_root(tmp_path, _valid_local_payload(aws_config_enabled=True))
    (root / relative).write_text(json.dumps(["not", "an", "object"]) + "\n", encoding="utf-8")

    findings = verify.verify_all(root)

    matching = [finding for finding in findings if relative in finding.found]
    assert matching, f"no finding for the non-object {relative}: {findings}"
    assert matching[0].check.startswith("completeness")
    assert "list" in matching[0].found
    assert not matching[0].check.startswith("consistency")


# ---------------------------------------------------------------------------
# AC-FUNC-004 / AC-TEST-002: placeholders, one per file.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("relative", "inject"),
    [
        (repo.SHELL_ENV, _inject_shell_env_placeholder),
        (repo.DEVCONTAINER_ENV_JSON, _inject_devcontainer_json_placeholder),
        (repo.AWS_PROFILE_MAP, _inject_aws_profile_map_placeholder),
    ],
)
def test_verify_all_finds_placeholder_reinserted_into_each_file(
    tmp_path: Path, relative: str, inject: Callable[[Path], None]
) -> None:
    verify = _import_verify()
    root = _rendered_root(tmp_path, _valid_local_payload(aws_config_enabled=True))
    inject(root)

    findings = verify.verify_all(root)

    matching = [
        finding
        for finding in findings
        if relative in finding.found and _PLACEHOLDER_TOKEN in finding.found
    ]
    assert matching, f"no placeholder finding for {relative}: {findings}"
    assert matching[0].check.startswith("no placeholders")


# ---------------------------------------------------------------------------
# AC-FUNC-005: the placeholder shape is `make init`'s, not a second one.
# ---------------------------------------------------------------------------


def test_placeholder_pattern_matches_makefile_scan() -> None:
    verify = _import_verify()
    real_root = repo.find_root(Path(__file__).resolve().parent)
    makefile_text = (real_root / "Makefile").read_text(encoding="utf-8")

    assert verify._PLACEHOLDER_PATTERN.pattern in makefile_text


# ---------------------------------------------------------------------------
# AC-FUNC-006: BASH_ENV against repo.container_workspace.
# ---------------------------------------------------------------------------


def test_verify_all_finds_bash_env_mismatch(tmp_path: Path) -> None:
    verify = _import_verify()
    root = _rendered_root(tmp_path, _valid_local_payload())
    wrong_workspace = f"/workspaces/{uuid.uuid4().hex}"
    _set_export(root, "BASH_ENV", f"'{wrong_workspace}/{repo.SHELL_ENV}'")

    findings = verify.verify_all(root)

    matching = [
        finding for finding in findings if finding.check.startswith("consistency: BASH_ENV")
    ]
    assert len(matching) == 1
    expected = f"{repo.container_workspace(root)}/{repo.SHELL_ENV}"
    assert expected in matching[0].found
    assert f"{wrong_workspace}/{repo.SHELL_ENV}" in matching[0].found


def test_verify_all_finds_bash_env_missing_from_shell_env(tmp_path: Path) -> None:
    """Round-4 code_review Finding 2: `shell.env` with no active `BASH_ENV`
    export line at all must produce a completeness finding naming the
    variable, not a consistency finding whose `found` text asserts BASH_ENV
    was set to `''`, a value the file never contained (AC-FUNC-010's "what
    was found" contract).
    """
    verify = _import_verify()
    root = _rendered_root(tmp_path, _valid_local_payload())
    _remove_export(root, "BASH_ENV")

    findings = verify.verify_all(root)

    matching = [finding for finding in findings if "BASH_ENV" in finding.found]
    assert matching, f"no finding for the missing BASH_ENV export: {findings}"
    assert len(matching) == 1
    assert matching[0].check.startswith("completeness")
    assert "no active export line" in matching[0].found
    assert "sets BASH_ENV to ''" not in matching[0].found
    assert matching[0].remedy.strip()


# ---------------------------------------------------------------------------
# AC-FUNC-006 / AC-FUNC-010, code_review Finding 3, doc_review Finding 1:
# `_unshell_quote` must reverse `render.shell_quote` and `render.home_relative`
# alike, and must leave a bare unquoted value untouched.
# ---------------------------------------------------------------------------


def test_unshell_quote_reverses_shell_quote() -> None:
    verify = _import_verify()
    value_with_embedded_quote = f"O'Brien-{uuid.uuid4().hex[:8]}"

    single_quoted = render_module.shell_quote(value_with_embedded_quote)

    assert verify._unshell_quote(single_quoted) == value_with_embedded_quote


def test_unshell_quote_reverses_home_relative(tmp_path: Path) -> None:
    """doc_review Finding 1: `render.home_relative` wraps an under-`${HOME}`
    path in DOUBLE quotes, never passing through `shell_quote`, and
    `render_shell_env` writes exactly that shape into the active
    `REMOTE_SSH_KEY_PATH` export line on a remote backend. `_unshell_quote`
    must reverse this shape too, not just the single-quoted one.
    """
    verify = _import_verify()
    home = _generated_dir(tmp_path, "home")
    key_path = home / ".ssh" / f"{uuid.uuid4().hex[:8]}.pem"

    double_quoted = render_module.home_relative(str(key_path), home)

    assert double_quoted.startswith('"') and double_quoted.endswith('"')
    assert verify._unshell_quote(double_quoted) == f"${{HOME}}/{key_path.relative_to(home)}"


@pytest.mark.parametrize(
    "bare_value",
    ["/workspaces/checkout/shell.env", "SomeoneElse"],
)
def test_unshell_quote_leaves_a_bare_unquoted_value_unchanged(bare_value: str) -> None:
    """code_review Finding 3: an active export line whose value was never
    quoted at all (an operator hand-edit, which the rendered header
    explicitly permits) must not be mangled by stripping a leading and
    trailing character that was never a quote pair.
    """
    verify = _import_verify()

    assert verify._unshell_quote(bare_value) == bare_value


def test_verify_all_accepts_unquoted_but_correct_export_values(tmp_path: Path) -> None:
    """code_review Finding 3: hand-editing an export line to a correct but
    unquoted value must not produce a false-positive finding. Verified
    empirically before the fix: an unquoted, correct BASH_ENV line reported a
    mismatch against a value missing its first and last character, and the
    same happened to an unquoted, correct DEVELOPER_NAME line compared
    against containerEnv.
    """
    verify = _import_verify()
    root = _rendered_root(tmp_path, _valid_local_payload())

    expected_bash_env = f"{repo.container_workspace(root)}/{repo.SHELL_ENV}"
    _set_export(root, "BASH_ENV", expected_bash_env)
    unquoted_name = f"SomeoneElse{uuid.uuid4().hex[:8]}"
    _set_export(root, "DEVELOPER_NAME", unquoted_name)

    devcontainer_env_path = root / repo.DEVCONTAINER_ENV_JSON
    document = json.loads(devcontainer_env_path.read_text(encoding="utf-8"))
    document["containerEnv"]["DEVELOPER_NAME"] = unquoted_name
    devcontainer_env_path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")

    assert verify.verify_all(root) == []


# ---------------------------------------------------------------------------
# AC-FUNC-007: identity variables shared with containerEnv.
# ---------------------------------------------------------------------------


def test_verify_all_finds_identity_variable_mismatch(tmp_path: Path) -> None:
    verify = _import_verify()
    root = _rendered_root(tmp_path, _valid_local_payload())
    new_name = f"Someone Else {uuid.uuid4().hex[:8]}"
    _set_export(root, "DEVELOPER_NAME", render_module.shell_quote(new_name))

    findings = verify.verify_all(root)

    matching = [
        finding for finding in findings if finding.check.startswith("consistency: DEVELOPER_NAME")
    ]
    assert len(matching) == 1
    assert "shell.env" in matching[0].found
    assert repo.DEVCONTAINER_ENV_JSON in matching[0].check


def test_verify_all_finds_identity_variable_missing_from_shell_env(tmp_path: Path) -> None:
    """Round-4 code_review Finding 1: a `containerEnv` key with no active
    export line in `shell.env` at all must produce its own completeness
    finding naming the variable and both files, rather than defaulting the
    missing export to `''` and comparing that default against containerEnv's
    value. Reproduced empirically for `HOST_PROXY_URL`: on a
    `host_proxy=False` render, `containerEnv` carries `''` for it, and the
    now-deleted `shell.env` export line also carried `''`, so the two
    "agree" once the missing line is silently defaulted and `verify_all`
    returns no finding at all for a variable no shell in the container can
    see.
    """
    verify = _import_verify()
    root = _rendered_root(tmp_path, _valid_local_payload(host_proxy=False))
    _remove_export(root, "HOST_PROXY_URL")

    findings = verify.verify_all(root)

    matching = [
        finding
        for finding in findings
        if "HOST_PROXY_URL" in finding.found and repo.SHELL_ENV in finding.found
    ]
    assert matching, f"no finding for the missing HOST_PROXY_URL export: {findings}"
    assert matching[0].check.startswith("completeness")
    assert repo.DEVCONTAINER_ENV_JSON in matching[0].found
    assert matching[0].remedy.strip()


def test_verify_all_finds_identity_variable_missing_from_shell_env_reports_no_value(
    tmp_path: Path,
) -> None:
    """The same missing-export shape for a non-empty identity variable must
    not produce a `found` text asserting shell.env set the variable to
    `''`, a value the file never contained, breaking AC-FUNC-010's "what
    was found" contract the same way a missing `BASH_ENV` export line did.
    """
    verify = _import_verify()
    root = _rendered_root(tmp_path, _valid_local_payload())
    _remove_export(root, "DEVELOPER_NAME")

    findings = verify.verify_all(root)

    matching = [finding for finding in findings if "DEVELOPER_NAME" in finding.found]
    assert matching, f"no finding for the missing DEVELOPER_NAME export: {findings}"
    assert len(matching) == 1
    assert matching[0].check.startswith("completeness")
    assert "shell.env sets DEVELOPER_NAME to ''" not in matching[0].found


def test_verify_all_finds_missing_container_env_object(tmp_path: Path) -> None:
    """code_review Finding 1: `devcontainer-environment-variables.json` with
    no `containerEnv` object at all must not silently verify clean.
    `render.render_devcontainer_env_json` already refuses to produce that
    shape (`RenderError`), so `verify` must be at least as strict, rather
    than defaulting the missing key to an empty mapping and comparing
    against nothing.
    """
    verify = _import_verify()
    root = _rendered_root(tmp_path, _valid_local_payload())
    path = root / repo.DEVCONTAINER_ENV_JSON
    document = json.loads(path.read_text(encoding="utf-8"))
    del document["containerEnv"]
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")

    findings = verify.verify_all(root)

    matching = [
        finding
        for finding in findings
        if repo.DEVCONTAINER_ENV_JSON in finding.found and "containerEnv" in finding.found
    ]
    assert matching, f"no finding for the missing containerEnv object: {findings}"
    assert matching[0].check.startswith("completeness")


def test_verify_all_finds_container_env_that_is_not_an_object(tmp_path: Path) -> None:
    """Round-2 code_review Finding 1: a `containerEnv` key that is present
    but is not a JSON object (a list, here) must produce a shaped
    completeness finding, not let `AttributeError` propagate out of
    `verify_all` from `.items()` being called on a non-mapping -- the
    type guard added for the missing-key case does not, by itself, cover
    a wrongly-shaped value under a present key.
    """
    verify = _import_verify()
    root = _rendered_root(tmp_path, _valid_local_payload())
    path = root / repo.DEVCONTAINER_ENV_JSON
    document = json.loads(path.read_text(encoding="utf-8"))
    document["containerEnv"] = ["a", "b"]
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")

    findings = verify.verify_all(root)

    matching = [
        finding
        for finding in findings
        if repo.DEVCONTAINER_ENV_JSON in finding.found and "containerEnv" in finding.found
    ]
    assert matching, f"no finding for the non-object containerEnv: {findings}"
    assert matching[0].check.startswith("completeness")
    assert "list" in matching[0].found


# ---------------------------------------------------------------------------
# AC-FUNC-008: aws-profile-map.json against AWS_CONFIG_ENABLED.
# ---------------------------------------------------------------------------


def test_verify_all_finds_aws_enabled_but_map_empty(tmp_path: Path) -> None:
    verify = _import_verify()
    root = _rendered_root(tmp_path, _valid_local_payload(aws_config_enabled=True))
    (root / repo.AWS_PROFILE_MAP).write_text("{}\n", encoding="utf-8")

    findings = verify.verify_all(root)

    matching = [
        finding
        for finding in findings
        if finding.check.startswith("consistency: aws-profile-map.json")
    ]
    assert len(matching) == 1
    assert "empty" in matching[0].found


def test_verify_all_finds_aws_disabled_but_map_populated(tmp_path: Path) -> None:
    verify = _import_verify()
    root = _rendered_root(tmp_path, _valid_local_payload(aws_config_enabled=False))
    profile = _valid_aws_profile()
    populated = json.dumps({profile["name"]: profile}, indent=2) + "\n"
    (root / repo.AWS_PROFILE_MAP).write_text(populated, encoding="utf-8")

    findings = verify.verify_all(root)

    matching = [
        finding
        for finding in findings
        if finding.check.startswith("consistency: aws-profile-map.json")
    ]
    assert len(matching) == 1
    assert "populated" in matching[0].found


def test_verify_all_finds_aws_config_enabled_missing_from_shell_env(tmp_path: Path) -> None:
    """Round-2 code_review Finding 3: `AWS_CONFIG_ENABLED` with no active
    export line in `shell.env` must produce its own finding rather than
    default to `'false'` -- an empty `aws-profile-map.json` then agrees
    with that default by coincidence, silently hiding the fact that the
    variable required by AC-FUNC-008 was never set at all. Also dropped
    from `containerEnv` here, or `_identity_findings`' own mismatch check
    would independently catch the absence and mask the defect under test.
    """
    verify = _import_verify()
    root = _rendered_root(tmp_path, _valid_local_payload(aws_config_enabled=False))
    _remove_export(root, "AWS_CONFIG_ENABLED")
    (root / repo.AWS_PROFILE_MAP).write_text("{}\n", encoding="utf-8")

    devcontainer_env_path = root / repo.DEVCONTAINER_ENV_JSON
    document = json.loads(devcontainer_env_path.read_text(encoding="utf-8"))
    del document["containerEnv"]["AWS_CONFIG_ENABLED"]
    devcontainer_env_path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")

    findings = verify.verify_all(root)

    matching = [finding for finding in findings if "AWS_CONFIG_ENABLED" in finding.found]
    assert matching, f"no finding for the missing AWS_CONFIG_ENABLED export: {findings}"
    assert matching[0].remedy.strip()


def _set_aws_config_enabled(root: Path, raw_value: str) -> None:
    """Rewrite `shell.env`'s active `AWS_CONFIG_ENABLED` export to `raw_value`.

    Also mirrored into `containerEnv` so `_identity_findings`' own
    mismatch check does not independently catch the divergence and mask
    the defect a caller is trying to isolate.
    """
    _set_export(root, "AWS_CONFIG_ENABLED", raw_value)

    devcontainer_env_path = root / repo.DEVCONTAINER_ENV_JSON
    document = json.loads(devcontainer_env_path.read_text(encoding="utf-8"))
    document["containerEnv"]["AWS_CONFIG_ENABLED"] = raw_value
    devcontainer_env_path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")


def test_verify_all_finds_aws_config_enabled_invalid_value_with_empty_map(
    tmp_path: Path,
) -> None:
    """code_review round-3 Finding: a value that is neither `'true'` nor
    `'false'` must produce its own finding quoting the actual value, not
    collapse to `False` and verify clean against an empty map -- the same
    default-masks-a-failure shape already rejected for the missing-key
    case, just not yet extended to a present-but-invalid value.
    """
    verify = _import_verify()
    root = _rendered_root(tmp_path, _valid_local_payload(aws_config_enabled=False))
    _set_aws_config_enabled(root, "True")
    (root / repo.AWS_PROFILE_MAP).write_text("{}\n", encoding="utf-8")

    findings = verify.verify_all(root)

    matching = [
        finding
        for finding in findings
        if "AWS_CONFIG_ENABLED" in finding.found and "True" in finding.found
    ]
    assert matching, f"no finding for the invalid AWS_CONFIG_ENABLED value: {findings}"
    assert matching[0].remedy.strip()
    mismatch = [
        finding
        for finding in findings
        if finding.check.startswith("consistency: aws-profile-map.json")
    ]
    assert not mismatch, f"invalid value must not be silently treated as 'false': {findings}"


def test_verify_all_finds_aws_config_enabled_invalid_value_with_populated_map(
    tmp_path: Path,
) -> None:
    """code_review round-3 Finding: the same invalid value against a
    populated map must not emit a mismatch finding whose `found` text
    asserts `'false'`, a value never present in shell.env, breaking
    AC-FUNC-010's "what was found" contract.
    """
    verify = _import_verify()
    root = _rendered_root(tmp_path, _valid_local_payload(aws_config_enabled=False))
    _set_aws_config_enabled(root, "True")
    profile = _valid_aws_profile()
    populated = json.dumps({profile["name"]: profile}, indent=2) + "\n"
    (root / repo.AWS_PROFILE_MAP).write_text(populated, encoding="utf-8")

    findings = verify.verify_all(root)

    matching = [
        finding
        for finding in findings
        if "AWS_CONFIG_ENABLED" in finding.found and "True" in finding.found
    ]
    assert matching, f"no finding for the invalid AWS_CONFIG_ENABLED value: {findings}"
    for finding in findings:
        assert "AWS_CONFIG_ENABLED is 'false'" not in finding.found, (
            f"found text must not assert a value absent from shell.env: {finding.found}"
        )
    mismatch = [
        finding
        for finding in findings
        if finding.check.startswith("consistency: aws-profile-map.json")
    ]
    assert not mismatch, f"invalid value must not be silently treated as 'false': {findings}"


# ---------------------------------------------------------------------------
# AC-FUNC-009 / AC-FUNC-010 / AC-CYCLE-001 / AC-TEST-004: accumulation, shape,
# and the end-to-end cycle.
# ---------------------------------------------------------------------------


def test_every_finding_carries_all_four_fields(tmp_path: Path) -> None:
    """AC-FUNC-010."""
    verify = _import_verify()
    root = _rendered_root(tmp_path, _valid_local_payload())
    (root / repo.SHELL_ENV).unlink()

    findings = verify.verify_all(root)

    assert findings
    for finding in findings:
        assert finding.check.strip()
        assert finding.found.strip()
        assert finding.prevents.strip()
        assert finding.remedy.strip()


def test_end_to_end_zero_then_three_findings(tmp_path: Path) -> None:
    """AC-CYCLE-001 / AC-TEST-004: a valid configuration verifies clean;
    corrupting three independent things at once (a missing file, a BASH_ENV
    mismatch, a reinserted placeholder) yields exactly three findings, each
    carrying a remedy, proving `verify_all` accumulates rather than
    short-circuits at the first problem.
    """
    verify = _import_verify()
    root = _rendered_root(tmp_path, _valid_local_payload())

    assert verify.verify_all(root) == []

    (root / repo.AWS_PROFILE_MAP).unlink()

    wrong_workspace = f"/workspaces/{uuid.uuid4().hex}"
    _set_export(root, "BASH_ENV", f"'{wrong_workspace}/{repo.SHELL_ENV}'")

    shell_env_path = root / repo.SHELL_ENV
    text = shell_env_path.read_text(encoding="utf-8")
    text = _with_placeholder_appended(text, "DEVCONTAINER")
    shell_env_path.write_text(text, encoding="utf-8")

    findings = verify.verify_all(root)

    assert len(findings) == 3, findings
    for finding in findings:
        assert finding.remedy.strip()
    assert any(
        finding.check.startswith("completeness") and repo.AWS_PROFILE_MAP in finding.found
        for finding in findings
    )
    assert any(finding.check.startswith("no placeholders") for finding in findings)
    assert any(finding.check.startswith("consistency: BASH_ENV") for finding in findings)


# ---------------------------------------------------------------------------
# AC-FUNC-011: report-only, standard-library-only.
# ---------------------------------------------------------------------------


def test_verify_module_has_no_write_path() -> None:
    verify = _import_verify()
    source = Path(verify.__file__ or "").read_text(encoding="utf-8")

    forbidden = (
        "write_text(",
        "write_bytes(",
        ".chmod(",
        ".unlink(",
        "os.open(",
        "os.remove(",
        "shutil.",
    )
    for token in forbidden:
        assert token not in source, f"verify.py must not write, chmod or delete: found {token!r}"


def test_verify_module_imports_only_stdlib_and_local_package() -> None:
    verify = _import_verify()
    source = Path(verify.__file__ or "").read_text(encoding="utf-8")
    tree = ast.parse(source)

    external_imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            external_imports.extend(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            assert node.module is not None
            external_imports.append(node.module.split(".")[0])

    for module_name in external_imports:
        assert module_name in sys.stdlib_module_names, (
            f"verify.py imports {module_name!r}, which is outside the standard library"
        )


# ---------------------------------------------------------------------------
# AC-DOC-001 / AC-DOC-002: docstrings.
# ---------------------------------------------------------------------------


def test_check_functions_carry_docstrings() -> None:
    """AC-DOC-001: every check function names what its failure prevents,
    so the finding text and the docstring cannot drift apart.
    """
    verify = _import_verify()
    check_functions = (
        verify._completeness_finding,
        verify._json_parse_finding,
        verify._json_type_finding,
        verify._placeholder_findings,
        verify._bash_env_finding,
        verify._identity_findings,
        verify._aws_profile_map_findings,
    )
    for func in check_functions:
        assert func.__doc__, f"{func.__name__} has no docstring"


def test_check_function_docstrings_name_what_the_failure_prevents() -> None:
    """AC-DOC-001, doc_review Finding 2: `_placeholder_findings` and
    `_json_parse_finding` must name what their failure prevents, wording
    that matches the `Finding.prevents` text each one constructs, not just
    describe what the function returns.
    """
    verify = _import_verify()

    placeholder_doc = " ".join((verify._placeholder_findings.__doc__ or "").split())
    assert "reaches the running container as literal text" in placeholder_doc

    json_parse_doc = " ".join((verify._json_parse_finding.__doc__ or "").split())
    assert "container startup step" in json_parse_doc
    assert "consistency check" in json_parse_doc


def test_identity_findings_docstring_names_actual_render_behavior_per_shape() -> None:
    """doc_review Finding (blocking, round 3): `_identity_findings`' docstring
    must not credit `render.render_devcontainer_env_json` with refusing
    *both* the missing-`containerEnv`-key shape and the
    `containerEnv`-present-but-not-an-object shape with the same
    `RenderError` guard. `render` only guards the missing-key shape;
    a present non-object `containerEnv` reaches
    `document["containerEnv"].update(...)` and raises `AttributeError`
    instead. The docstring must name each shape's actual outcome, not
    "either shape (RenderError)".
    """
    verify = _import_verify()
    doc = " ".join((verify._identity_findings.__doc__ or "").split())

    assert "either shape (`RenderError`" not in doc
    assert "RenderError" in doc
    assert "AttributeError" in doc


def test_module_docstring_names_g1_line_and_make_init() -> None:
    """AC-DOC-002."""
    verify = _import_verify()
    docstring = verify.__doc__ or ""

    assert "Verified: no placeholders, BASH_ENV matches" in docstring
    assert "make init" in docstring


def test_module_docstring_states_the_real_placeholder_scope() -> None:
    """code_review Finding 4: the module docstring must not claim `verify`
    and `make init` "cannot disagree about what counts as one left behind"
    -- `_active_configuration_text` narrows the placeholder scan to
    `shell.env`'s active export lines, while `make init` greps the whole
    file, comments included, so the two scans' results can differ even
    though the token shape they use cannot.
    """
    verify = _import_verify()
    docstring = verify.__doc__ or ""

    assert "cannot disagree about what counts as one left behind" not in docstring
    assert "active" in docstring


def test_module_source_cites_the_make_init_target_not_a_line_number() -> None:
    """doc_review Finding 3 (advisory): a `Makefile:158`-shaped citation goes
    silently stale after any edit above that line; the make target's name
    does not.
    """
    verify = _import_verify()
    source = Path(verify.__file__ or "").read_text(encoding="utf-8")

    assert "Makefile:158" not in source
    assert "make init" in source
