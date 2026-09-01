"""Pinning tests for the Secrets section of `docs/environment-files.md` (E3-F2-S2-T2).

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
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from conftest import _normalize_whitespace
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
    names = []
    for line in table_match.group(1).splitlines():
        if not line.strip().startswith("|"):
            continue
        first_cell = line.split("|")[1].strip()
        names.append(first_cell.strip("`"))
    return tuple(names)


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
