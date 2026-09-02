"""Pinning tests for the Secrets and Transport sections of
`docs/environment-files.md` (E3-F2-S2-T2, E6-F2-S1-T5).

Spec Section 8 makes this document's Secrets section part of "done" for E3:
the catalog (spec Sections 5.3, 5.4) replaces the pre-E3 caveat that anything
placed in `shell.env` reaches AWS and is acceptable there for a secret (spec
Section 0 row B5). `shell.env` itself is unchanged as a bootstrap mechanism
(spec Section 0 row B10); only what it is allowed to hold narrows.

Every test here reads the rendered document, not the source that produced
it, so a future edit that reintroduces the retired caveat, drops a catalog
fact, or names a configuration variable no module reads fails one of these
tests by name rather than surviving as an unnoticed drift (spec Section 8).

`_configuration_variable_names` and `_catalog_declared_env_var_names` both
parse their source directly (the rendered table, and the `*_ENV_VAR`
constants in `catalog.py`) instead of duplicating either list as a literal
in this module, so the comparison in
`test_documented_variables_match_catalog_declared_env_vars` fails on a real
divergence between the two, not on this test module falling out of sync
with either one.

The `### Transport` section tests below (E6-F2-S1-T5) follow the same
"parse the rendered fact, don't restate it" discipline for
`transport.py`'s environment variables. One of those four variables,
`DEVCONTAINER_TRANSPORT`, is documented ahead of the code that will read
it: `resolve_transport` and the `connect` entry point are E6-F2-S1-T3's
and E6-F2-S1-T4's changes, neither landed in this tree yet, so
`_transport_module_env_var_names` (which derives its set from
`devcontainer_config.transport`'s own namespace, the same technique
`_catalog_declared_env_var_names` uses for `catalog.py`) cannot see it.
`test_transport_table_documents_devcontainer_transport` pins that row's
literal content directly instead, the same way `_CATALOG_FACTS` pins
Secrets-section facts the code does not make derivable.

`_markdown_table_first_column_names` is the row-walking and
backtick-stripping logic `_configuration_variable_names` and
`_transport_table_variable_names` both need once a header regex has
isolated a table's row text; before this extraction each function carried
its own copy.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from conftest import _normalize_whitespace
from devcontainer_config import transport
from gitignore_check import repo_root

_DOC_RELATIVE_PATH = "docs/environment-files.md"
_CATALOG_RELATIVE_PATH = ".claude/plugins/devcontainer/scripts/devcontainer_config/catalog.py"

# The sentence this task retires (pre-E3 text, still present verbatim until
# the GREEN rewrite lands): anything in `shell.env` reaches AWS and that is
# acceptable for a value that belongs in the catalog instead.
_RETIRED_CAVEAT_NEEDLE = "acceptable for secrets you would store in Parameter Store anyway"

_CONFIGURATION_VARIABLES_HEADING = "### Configuration variables"

# `AWS_PROFILE_ENV_VAR = "AWS_PROFILE"`, `SECRET_CACHE_DIR_ENV_VAR =
# "SECRET_CACHE_DIR"`: every environment variable `catalog.py` reads is
# declared exactly once as a module-level `*_ENV_VAR` constant (see that
# module's own docstring), so this pattern is the single source of truth
# this test module reads from rather than re-declaring the variable names
# itself.
_ENV_VAR_CONST_PATTERN = re.compile(
    r'^([A-Z][A-Z0-9_]*_ENV_VAR) = "([A-Za-z0-9_]+)"$', re.MULTILINE
)


def _doc_path() -> Path:
    return repo_root() / _DOC_RELATIVE_PATH


def _doc_text() -> str:
    return _doc_path().read_text(encoding="utf-8")


def _secrets_section_text() -> str:
    """The `## Secrets` section only, from its heading up to the next top-level heading.

    Scoping a fact-presence assertion to this slice (rather than
    `_doc_text()`, which matches anywhere in the document) means a needle
    this section's rewrite is responsible for stating cannot be satisfied
    by the same words appearing somewhere else in the document for an
    unrelated reason. `## Secrets`'s subsections (`### The secret catalog`,
    `### Configuration variables`, `### What did not change`) use `###`,
    not `##`, so they stay inside this slice; only the next `##`-level
    heading (or end of file) ends it.
    """
    text = _doc_text()
    match = re.search(r"^## Secrets\n.*?(?=^## |\Z)", text, re.MULTILINE | re.DOTALL)
    assert match, f"{_DOC_RELATIVE_PATH} has no '## Secrets' section."
    return match.group(0)


def _secrets_section_text_normalized() -> str:
    return _normalize_whitespace(_secrets_section_text())


def _catalog_text() -> str:
    return (repo_root() / _CATALOG_RELATIVE_PATH).read_text(encoding="utf-8")


def _markdown_table_first_column_names(table_body: str) -> tuple[str, ...]:
    """The first-column cell of every data row in a rendered Markdown table body.

    `table_body` is the row text a table-header regex's own capture group
    already isolated (everything after the `|---|---|...|` separator line);
    this function only walks those rows. Shared by
    `_configuration_variable_names` (the `### Configuration variables`
    table) and `_transport_table_variable_names` (the `### Transport`
    table) so the row-walking and backtick-stripping logic exists exactly
    once, rather than as two copies that could silently diverge on the
    next Markdown-table quirk either section's table grows.
    """
    names = []
    for line in table_body.splitlines():
        if not line.strip().startswith("|"):
            continue
        first_cell = line.split("|")[1].strip()
        names.append(first_cell.strip("`"))
    return tuple(names)


def _configuration_variable_names() -> tuple[str, ...]:
    """The first-column variable names of the rendered configuration-variable table.

    Parses the actual Markdown table under `_CONFIGURATION_VARIABLES_HEADING`
    rather than any list this module declares, so a table row added,
    removed, or renamed in `docs/environment-files.md` changes what this
    function returns on the very next test run.
    """
    text = _doc_text()
    try:
        heading_index = text.index(_CONFIGURATION_VARIABLES_HEADING)
    except ValueError:
        return ()
    remainder = text[heading_index:]
    table_match = re.search(
        r"\| *Variable *\| *Default *\| *Governs *\|\n\|[-| ]+\|\n((?:\|.*\|\n?)+)",
        remainder,
    )
    if table_match is None:
        return ()
    return _markdown_table_first_column_names(table_match.group(1))


def _catalog_declared_env_var_names() -> frozenset[str]:
    return frozenset(match.group(2) for match in _ENV_VAR_CONST_PATTERN.finditer(_catalog_text()))


def test_retired_shell_env_caveat_is_absent() -> None:
    text = _doc_text()
    offending = [line for line in text.splitlines() if _RETIRED_CAVEAT_NEEDLE in line]
    assert not offending, (
        f"{_DOC_RELATIVE_PATH} still carries the retired shell.env-reaches-AWS-and-that-is-"
        f"acceptable-for-a-secret caveat (spec Section 0 row B5): {offending!r}"
    )


# The pre-fix intro said all three gitignored files "carry per-developer
# identity and secrets" -- an intra-document contradiction with the Secrets
# section's own claim that `shell.env` carries no credential, and wrong
# about the other two files: `aws-profile-map.json.example` is SSO
# profile/account configuration and `devcontainer-environment-variables.
# json.example` is git identity plus feature toggles, neither a credential
# (round-2 doc_review BLOCKING finding).
_INTRO_RETIRED_SECRETS_CLAIM_NEEDLE = "carry per-developer identity and secrets"
_INTRO_CORRECTED_CLAIM_NEEDLE = "identity and configuration that must not reach git"


def test_intro_does_not_claim_all_three_files_carry_secrets() -> None:
    text = _doc_text()
    assert _INTRO_RETIRED_SECRETS_CLAIM_NEEDLE not in text, (
        f"{_DOC_RELATIVE_PATH} intro still claims all three gitignored files carry "
        f"'secrets', contradicting the Secrets section's own no-credential claim about "
        f"shell.env and overstating what the AWS profile map and devcontainer template "
        f"input hold: {_INTRO_RETIRED_SECRETS_CLAIM_NEEDLE!r}"
    )
    assert _INTRO_CORRECTED_CLAIM_NEEDLE in text, (
        f"{_DOC_RELATIVE_PATH} intro no longer states the corrected claim that the three "
        f"gitignored files carry per-developer identity and configuration that must not "
        f"reach git: expected {_INTRO_CORRECTED_CLAIM_NEEDLE!r}."
    )


# Same pre-E3 presumption recurred in the "Filling them out with Claude"
# callout: "never to fill in secrets it invents" (round-2 doc_review
# BLOCKING finding).
_CALLOUT_RETIRED_SECRETS_CLAIM_NEEDLE = "fill in secrets it invents"


def test_claude_callout_does_not_claim_secrets_it_invents() -> None:
    text = _doc_text()
    assert _CALLOUT_RETIRED_SECRETS_CLAIM_NEEDLE not in text, (
        f"{_DOC_RELATIVE_PATH} 'Filling them out with Claude' callout still claims Claude "
        f"might invent 'secrets', the same pre-E3 presumption the intro claim retired: "
        f"{_CALLOUT_RETIRED_SECRETS_CLAIM_NEEDLE!r}"
    )
    assert "invent configuration values" in text, (
        f"{_DOC_RELATIVE_PATH} callout no longer states the corrected warning against "
        "inventing configuration values."
    )


# One case per catalog fact the Secrets section must state (spec Sections
# 5.3, 5.4, 4.3, 14.2). Each case fails independently and names the missing
# fact, rather than one assertion reporting a generic mismatch when several
# facts are missing at once.
_CATALOG_FACTS: tuple[tuple[str, str], ...] = (
    ("shell_env_carries_no_credential", "carries no credential"),
    ("single_backend", "no second provider"),
    ("no_offline_store", "no offline store"),
    ("no_fallback_to_local_copy", "no fallback to a local copy"),
    ("shared_scope_path_prefix", "/devcontainer/shared/secrets/<NAME>"),
    ("instance_scope_path_prefix", "/devcontainer/<instance>/secrets/<NAME>"),
    ("both_prefixes_securestring", "SecureString"),
    ("iam_enforces_boundary", "IAM enforces the boundary"),
    ("resolution_instance_first_then_shared", "instance-first then shared"),
    ("command_get", "devsecret get <NAME>"),
    ("command_list", "devsecret list [--scope <scope>]"),
    ("command_set", "devsecret set <NAME> [--scope <scope>] [--exported]"),
    ("command_rm", "devsecret rm <NAME> --scope <scope>"),
    ("command_run", "devsecret run --secrets A,B -- <cmd>"),
    ("command_export_list", "devsecret export-list"),
    ("help_reference_pointer", "devsecret --help"),
    ("value_never_a_command_line_argument", "never accepted as a command-line argument"),
    ("no_value_written_to_any_filesystem", "No value is ever written to any filesystem"),
    ("remote_engine_uses_instance_role", "instance role"),
    ("local_engine_uses_sso_session", "developer's already-valid AWS SSO session"),
)


@pytest.mark.parametrize(
    "fact_id,needle",
    _CATALOG_FACTS,
    ids=[fact_id for fact_id, _ in _CATALOG_FACTS],
)
def test_secrets_section_states_catalog_fact(fact_id: str, needle: str) -> None:
    text = _secrets_section_text_normalized()
    assert needle in text, (
        f"{_DOC_RELATIVE_PATH} is missing the catalog fact {fact_id!r}: "
        f"expected to find {needle!r} in the '## Secrets' section."
    )


def test_configuration_variable_table_is_present() -> None:
    names = _configuration_variable_names()
    assert names, (
        f"{_DOC_RELATIVE_PATH} has no configuration-variable table under "
        f"{_CONFIGURATION_VARIABLES_HEADING!r}."
    )


def test_documented_variables_match_catalog_declared_env_vars() -> None:
    documented = set(_configuration_variable_names())
    declared = _catalog_declared_env_var_names()
    assert declared, (
        f"no *_ENV_VAR constant found in {_CATALOG_RELATIVE_PATH}; the extraction pattern "
        "may be stale."
    )
    orphaned_in_doc = documented - declared
    assert not orphaned_in_doc, (
        f"{_DOC_RELATIVE_PATH} names configuration variable(s) that no module reads: "
        f"{sorted(orphaned_in_doc)!r}. Checked against the *_ENV_VAR constants declared in "
        f"{_CATALOG_RELATIVE_PATH}."
    )
    undocumented = declared - documented
    assert not undocumented, (
        f"{_CATALOG_RELATIVE_PATH} declares configuration variable(s) that "
        f"{_DOC_RELATIVE_PATH} does not document: {sorted(undocumented)!r}."
    )


def test_shell_env_still_published_for_remote_bootstrap() -> None:
    text = _secrets_section_text()
    assert "make push-secrets" in text, (
        f"{_DOC_RELATIVE_PATH} no longer states the AC-DOC-009 fact that `make push-secrets` "
        "publishes `shell.env` to Parameter Store, in the '## Secrets' section."
    )
    assert "bootstrap" in text, (
        f"{_DOC_RELATIVE_PATH} no longer states the AC-DOC-009 fact that publishing `shell.env` "
        "lets a remote container bootstrap itself, in the '## Secrets' section."
    )


def test_shell_env_republished_automatically_by_build_and_rebuild() -> None:
    text = _secrets_section_text_normalized()
    assert "build" in text and "rebuild" in text and "newer than" in text, (
        f"{_DOC_RELATIVE_PATH} no longer states the AC-DOC-009 fact that `build` and `rebuild` "
        "publish `shell.env` themselves when it is newer than the stored copy "
        "(.devcontainer/remote-docker/container.sh republishes automatically on this condition)."
    )


def test_git_credential_still_documented_as_not_from_shell_env() -> None:
    text = _secrets_section_text()
    assert "git credential" in text, (
        f"{_DOC_RELATIVE_PATH} no longer states the AC-DOC-009 fact about the container's git "
        "credential, in the '## Secrets' section."
    )
    assert "does not come from" in text, (
        f"{_DOC_RELATIVE_PATH} no longer states the AC-DOC-009 fact that the git credential does "
        "not come from `shell.env`, in the '## Secrets' section."
    )
    assert "make push-git-creds" in text, (
        f"{_DOC_RELATIVE_PATH} no longer states the AC-DOC-009 fact that the git credential is "
        "copied by `make push-git-creds`, in the '## Secrets' section."
    )


_TRANSPORT_HEADING = "### Transport"

_TRANSPORT_TABLE_PATTERN = re.compile(
    r"\| *Variable *\| *Default *\| *Defined in *\|\n\|[-| ]+\|\n((?:\|.*\|\n?)+)"
)

# "All three variables are optional" today, "All four variables ... are
# optional" once this row lands: the word varies, the extraction must not
# be a hard-coded "three" that a fifth variable would silently outgrow.
_TRANSPORT_ENUMERATION_PATTERN = re.compile(r"All (\w+) variables")

_ENUMERATION_WORD_TO_COUNT = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
}

# AC-DOC-003's negative clause: no claim that DEVCONTAINER_TRANSPORT changes
# what docker-tunnel.sh itself does, since resolve_transport never calls or
# modifies that script. Matches "changes" (not "change"), so it does not
# also match this section's own correct "will not change what
# `docker-tunnel.sh` itself does" sentence.
_TRANSPORT_FALSE_TUNNEL_CLAIM_NEEDLE = "changes what `docker-tunnel.sh`"

# code_review/doc_review REVIEW_FAIL round 1: a prior draft of the section
# described the SSH transport as running "for every other value, including
# unset", i.e. a silent fallback on any unrecognized value. E6-F2-S1-T4
# AC-FUNC-002 requires a non-zero exit for any value other than unset, `ssh`
# or `ssm`, and this section's own closing sentence already says so; the two
# cannot disagree. The pattern matches "ssh transport" followed by "for any
# other value" or "for every other value" with no intervening mention of the
# SSM transport, which is exactly the shape of the fallback claim: the
# correct phrasing always names the SSM transport (and the non-zero exit)
# between the SSH clause and any "other value" wording.
_TRANSPORT_SSH_UNQUALIFIED_FALLBACK_PATTERN = re.compile(
    r"ssh transport(?:(?!ssm transport).)*for (?:any|every) other value",
    re.IGNORECASE | re.DOTALL,
)


def _transport_section_text() -> str:
    """The `### Transport` section only, from its heading up to the next heading.

    Scoping to this slice, the same technique `_secrets_section_text` uses
    for `## Secrets`, means a fact this section is responsible for stating
    cannot be satisfied by the same words appearing somewhere else in the
    document for an unrelated reason. The next `##`- or `###`-level
    heading, or end of file, ends the slice.
    """
    text = _doc_text()
    match = re.search(r"^### Transport\n.*?(?=^## |^### |\Z)", text, re.MULTILINE | re.DOTALL)
    assert match, f"{_DOC_RELATIVE_PATH} has no '{_TRANSPORT_HEADING}' section."
    return match.group(0)


def _transport_section_text_normalized() -> str:
    return _normalize_whitespace(_transport_section_text())


def _transport_table_variable_names() -> tuple[str, ...]:
    """The first-column variable names of the rendered `### Transport` table.

    Parses the actual Markdown table rather than any list this module
    declares, the same technique `_configuration_variable_names` uses for
    the `### Configuration variables` table, so a table row added,
    removed, or renamed in `docs/environment-files.md` changes what this
    function returns on the very next test run.
    """
    section = _transport_section_text()
    table_match = _TRANSPORT_TABLE_PATTERN.search(section)
    if table_match is None:
        return ()
    return _markdown_table_first_column_names(table_match.group(1))


def _transport_module_env_var_names() -> frozenset[str]:
    """Every environment variable name `devcontainer_config.transport`'s own
    namespace declares as a `*_ENV_VAR` module-level constant.

    Imports the module and reads `vars(transport)` rather than parsing its
    source text, so an imported constant such as
    `DOCKER_HANDSHAKE_TIMEOUT_ENV_VAR` (declared in `hostprobe.py`, and
    reachable from `transport`'s own namespace only because `transport.py`
    imports it by name in a `from ... import (...)` statement rather than
    through a dotted `hostprobe.DOCKER_HANDSHAKE_TIMEOUT_ENV_VAR`
    reference) counts the same as a constant `transport.py` assigns
    directly. `test_transport_section_documents_every_transport_environment_variable`
    below derives its expectation from this function's return value rather
    than a literal list this test module would otherwise have to keep in
    sync with `transport.py` by hand.
    """
    return frozenset(
        value
        for name, value in vars(transport).items()
        if name.endswith("_ENV_VAR") and isinstance(value, str)
    )


def test_transport_section_documents_every_transport_environment_variable() -> None:
    """AC-TEST-001: every environment variable `transport.py`'s own namespace names
    must appear in the `### Transport` table.

    `devcontainer_config.transport` does not yet declare
    `DEVCONTAINER_TRANSPORT_ENV_VAR` in this tree: E6-F2-S1-T3, which adds
    it, has not landed. This check therefore cannot derive that fourth
    variable from the module the way it derives the other three; the
    table's `DEVCONTAINER_TRANSPORT` row is pinned directly instead by
    `test_transport_table_documents_devcontainer_transport` below. What
    this test guards is that no variable the module reads today, or starts
    reading once E6-F2-S1-T3 lands, can silently drop out of the table
    without failing here by name.
    """
    declared = _transport_module_env_var_names()
    assert declared, (
        "no *_ENV_VAR constant found on devcontainer_config.transport's own namespace; "
        "the extraction may be stale."
    )
    documented = set(_transport_table_variable_names())
    undocumented = declared - documented
    assert not undocumented, (
        f"devcontainer_config.transport reads environment variable(s) that "
        f"{_DOC_RELATIVE_PATH}'s '{_TRANSPORT_HEADING}' table does not document: "
        f"{sorted(undocumented)!r}."
    )


def test_transport_section_enumeration_matches_the_table() -> None:
    """AC-TEST-002: the closing sentence's stated count matches the table's row count.

    Extracts the stated word from the sentence and the actual row count
    from the table independently, so a future row added to the table
    without updating the sentence (or vice versa) fails here rather than
    surviving as an unnoticed drift.
    """
    section = _transport_section_text_normalized()
    match = _TRANSPORT_ENUMERATION_PATTERN.search(section)
    assert match, (
        f"{_DOC_RELATIVE_PATH}'s '{_TRANSPORT_HEADING}' section has no 'All <count> "
        "variables' enumeration sentence."
    )
    word = match.group(1).lower()
    stated_count = _ENUMERATION_WORD_TO_COUNT.get(word)
    assert stated_count is not None, (
        f"unrecognized enumeration word {word!r} in {_DOC_RELATIVE_PATH}'s "
        f"'{_TRANSPORT_HEADING}' section; extend _ENUMERATION_WORD_TO_COUNT."
    )
    names = _transport_table_variable_names()
    assert stated_count == len(names), (
        f"{_DOC_RELATIVE_PATH}'s '{_TRANSPORT_HEADING}' section states {word!r} variables "
        f"are optional but the table has {len(names)} row(s): {names!r}."
    )


def test_transport_table_documents_devcontainer_transport() -> None:
    """AC-DOC-001: the `DEVCONTAINER_TRANSPORT` row states its default, accepted
    values and single reader.

    `resolve_transport` (E6-F2-S1-T3) has not landed in this tree, so this
    fact cannot be derived from the module's own namespace the way
    `test_transport_section_documents_every_transport_environment_variable`
    derives the other three rows; it is pinned directly here instead, the
    same technique the '## Secrets' section's `_CATALOG_FACTS` parametrized
    cases use for facts the code does not (yet) make derivable.
    """
    names = _transport_table_variable_names()
    assert "DEVCONTAINER_TRANSPORT" in names, (
        f"{_DOC_RELATIVE_PATH}'s '{_TRANSPORT_HEADING}' table has no `DEVCONTAINER_TRANSPORT` "
        f"row: {names!r}."
    )
    section = _transport_section_text_normalized()
    assert "`ssh`" in section, (
        f"{_DOC_RELATIVE_PATH}'s '{_TRANSPORT_HEADING}' section does not state `ssh` as "
        "DEVCONTAINER_TRANSPORT's default."
    )
    assert "`ssm`" in section, (
        f"{_DOC_RELATIVE_PATH}'s '{_TRANSPORT_HEADING}' section does not name `ssm` as an "
        "accepted DEVCONTAINER_TRANSPORT value."
    )
    assert "resolve_transport" in section, (
        f"{_DOC_RELATIVE_PATH}'s '{_TRANSPORT_HEADING}' section does not name "
        "`resolve_transport` as DEVCONTAINER_TRANSPORT's reader."
    )


def _assert_transport_landed_caveat_state(section: str) -> None:
    """AC-DOC-003 caveat lifecycle guard, state-derived rather than pinned.

    A prior version of this check required the "not yet landed" /
    "no effect at all" caveat unconditionally, which is accurate only
    while `devcontainer_config.transport` declares no `resolve_transport`
    attribute. `E6-F2-S1-T3` (status `blocked`, on `E6-F2-S1-T4` and this
    task) and `E6-F2-S1-T4` (status `in-queue`) do not list
    `docs/environment-files.md` in their own Changes Manifests, so once
    they land, an unconditional pin would keep demanding wording that has
    gone false with nothing left to catch it, and correcting the prose
    afterward would then fail that same pin.

    Branching on `hasattr(transport, "resolve_transport")` instead makes
    the requirement track the module's own namespace: the caveat is
    required while the reader is absent (today's true state) and
    forbidden once it exists, forcing `docs/environment-files.md` to be
    corrected in the same change that lands the reader rather than
    silently going stale.
    """
    caveat_present = "has not landed" in section or "not yet landed" in section
    no_effect_present = "no effect at all" in section
    if hasattr(transport, "resolve_transport"):
        assert not caveat_present, (
            f"{_DOC_RELATIVE_PATH}'s '{_TRANSPORT_HEADING}' section still states the "
            "connect entry point has not landed, but devcontainer_config.transport now "
            "declares resolve_transport; remove the stale caveat."
        )
        assert not no_effect_present, (
            f"{_DOC_RELATIVE_PATH}'s '{_TRANSPORT_HEADING}' section still states that "
            "setting DEVCONTAINER_TRANSPORT has no effect, but "
            "devcontainer_config.transport now declares resolve_transport; remove the "
            "stale caveat."
        )
    else:
        assert caveat_present, (
            f"{_DOC_RELATIVE_PATH}'s '{_TRANSPORT_HEADING}' section does not state that the "
            "connect entry point and its selector have not landed in this repository yet."
        )
        assert no_effect_present, (
            f"{_DOC_RELATIVE_PATH}'s '{_TRANSPORT_HEADING}' section does not state that "
            "setting DEVCONTAINER_TRANSPORT today has no effect."
        )


def test_transport_section_describes_connect_entry_point() -> None:
    """AC-DOC-003: the section describes `make connect` and states precisely what
    the selector does and does not control, without claiming the interface
    is reachable before E6-F2-S1-T3 and E6-F2-S1-T4 land.

    The "not yet landed" / "no effect at all" caveat's presence is checked
    by `_assert_transport_landed_caveat_state`, which derives the
    requirement from `devcontainer_config.transport`'s own namespace
    instead of pinning the wording unconditionally; see that function's
    docstring for why an unconditional pin would go silently false.
    """
    section = _transport_section_text_normalized()
    assert "make connect" in section, (
        f"{_DOC_RELATIVE_PATH}'s '{_TRANSPORT_HEADING}' section does not describe the "
        "`make connect` entry point."
    )
    assert "resolve_transport" in section, (
        f"{_DOC_RELATIVE_PATH}'s '{_TRANSPORT_HEADING}' section does not name "
        "`resolve_transport` as DEVCONTAINER_TRANSPORT's single reader."
    )
    _assert_transport_landed_caveat_state(section)


@pytest.mark.parametrize(
    ("resolve_transport_present", "section_text", "should_raise"),
    [
        pytest.param(
            False,
            "make connect ... resolve_transport ... has not landed ... no effect at all",
            False,
            id="absent-caveat-present-passes",
        ),
        pytest.param(
            False,
            "make connect ... resolve_transport only",
            True,
            id="absent-caveat-missing-fails",
        ),
        pytest.param(
            True,
            "make connect ... resolve_transport only",
            False,
            id="present-caveat-absent-passes",
        ),
        pytest.param(
            True,
            "make connect ... resolve_transport ... has not landed ... no effect at all",
            True,
            id="present-caveat-present-fails",
        ),
    ],
)
def test_transport_landed_caveat_state_branches_on_module_state(
    monkeypatch: pytest.MonkeyPatch,
    resolve_transport_present: bool,
    section_text: str,
    should_raise: bool,
) -> None:
    """Exercises both branches of `_assert_transport_landed_caveat_state`
    against synthetic section text, independently of the current, real
    state of `docs/environment-files.md` and `devcontainer_config.transport`.

    Fixed a doc_review REVIEW_FAIL: the previous version of
    `test_transport_section_describes_connect_entry_point` pinned the
    "not yet landed" / "no effect at all" wording as an unconditionally
    required literal substring. That is accurate only while
    `devcontainer_config.transport` declares no `resolve_transport`
    attribute; once E6-F2-S1-T3 lands that attribute, the pinned wording
    becomes false with nothing left to catch it, and correcting the prose
    afterward would then fail the old, unconditional assertion. Branching
    on `hasattr(transport, "resolve_transport")` turns the trap into an
    alarm: the caveat is required while the reader is absent and forbidden
    once it exists, in both directions.
    """
    if resolve_transport_present:
        monkeypatch.setattr(transport, "resolve_transport", lambda: None, raising=False)
    else:
        monkeypatch.delattr(transport, "resolve_transport", raising=False)
    if should_raise:
        with pytest.raises(AssertionError):
            _assert_transport_landed_caveat_state(section_text)
    else:
        _assert_transport_landed_caveat_state(section_text)


def test_transport_section_does_not_claim_it_changes_docker_tunnel_sh() -> None:
    """AC-DOC-003's negative clause: no claim that DEVCONTAINER_TRANSPORT changes
    what `docker-tunnel.sh` itself does, since `resolve_transport` never calls
    or modifies that script.
    """
    section = _transport_section_text_normalized()
    assert _TRANSPORT_FALSE_TUNNEL_CLAIM_NEEDLE not in section, (
        f"{_DOC_RELATIVE_PATH}'s '{_TRANSPORT_HEADING}' section claims DEVCONTAINER_TRANSPORT "
        f"changes what docker-tunnel.sh does, which `resolve_transport` never does: "
        f"{_TRANSPORT_FALSE_TUNNEL_CLAIM_NEEDLE!r}."
    )
    assert "will not change what `docker-tunnel.sh` itself does" in section, (
        f"{_DOC_RELATIVE_PATH}'s '{_TRANSPORT_HEADING}' section no longer states that "
        "DEVCONTAINER_TRANSPORT does not change docker-tunnel.sh's own behavior."
    )


def test_transport_section_does_not_describe_ssh_as_the_fallback_for_unrecognized_values() -> None:
    """code_review/doc_review REVIEW_FAIL (round 1): the section must not describe
    the SSH transport as running for "every other value" (or "any other value")
    of `DEVCONTAINER_TRANSPORT`, since that documents a silent fallback on an
    unrecognized value. E6-F2-S1-T4 AC-FUNC-002 requires `make connect` to exit
    non-zero, before either transport starts, for any value other than unset,
    `ssh` or `ssm`, printing an `ERROR:` line naming the variable, the offending
    value and the accepted values; this section's own closing sentence already
    states the same fail-fast rule. A paragraph that instead routes "every other
    value" to the SSH transport contradicts both, and this test pins the
    contradiction so it cannot be reintroduced silently.
    """
    section = _transport_section_text_normalized()
    match = _TRANSPORT_SSH_UNQUALIFIED_FALLBACK_PATTERN.search(section)
    assert match is None, (
        f"{_DOC_RELATIVE_PATH}'s '{_TRANSPORT_HEADING}' section describes the SSH "
        f"transport as the fallback for an unrecognized DEVCONTAINER_TRANSPORT value "
        f"({match.group(0)!r}), contradicting the section's own fail-fast closing "
        "sentence and E6-F2-S1-T4 AC-FUNC-002, which requires a non-zero exit naming "
        "the variable, the offending value and the accepted values for any value "
        "other than unset, `ssh` or `ssm`."
    )
    assert "non-zero exit" in section, (
        f"{_DOC_RELATIVE_PATH}'s '{_TRANSPORT_HEADING}' section does not state that an "
        "unrecognized DEVCONTAINER_TRANSPORT value makes `make connect` exit non-zero."
    )
    assert "unset or the literal `ssh`" in section, (
        f"{_DOC_RELATIVE_PATH}'s '{_TRANSPORT_HEADING}' section does not scope the SSH "
        "transport branch to only unset or the literal `ssh`."
    )
