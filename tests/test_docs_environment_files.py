"""Pinning tests for the Secrets and Transport sections of
`docs/environment-files.md` (E3-F2-S2-T2, E6-F2-S1-T5, E6-F2-S1-T6).

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

E6-F2-S1-T6 narrows `_assert_transport_landed_caveat_state` from a single
`hasattr(transport, "resolve_transport")` branch to two independent
reader signals: that one, and `_connect_recipe_reads_devcontainer_transport`,
which parses the Makefile's `connect` recipe directly rather than assuming
DEVCONTAINER_TRANSPORT can only ever be wired up alongside
`resolve_transport`. Both readers are still absent from this repository
today: `transport.py` declares no `resolve_transport` function and the
Makefile's `connect` recipe does not reference DEVCONTAINER_TRANSPORT.
The Transport section's prose therefore describes the three-branch
dispatch as the contract both readers will implement once landed, and
caveats BOTH readers symmetrically as not yet landed, rather than
asserting either one's present-tense landed state (a round-2 defect,
code_review/doc_review/test_review REVIEW_FAIL: an earlier revision
caveated `resolve_transport` alone and, by that asymmetry, implied the
Makefile recipe already reads the variable today).
`_assert_transport_landed_caveat_state` enforces that symmetry
mechanically, but round 2's implementation only enforced it in one
direction: it required the doc to carry a "not yet landed, E6-F2-S1-T4"
caveat on the Makefile recipe today, and raised nothing the moment that
caveat went false (round 3 finding, code_review/test_review REVIEW_FAIL:
`test_review` proved the guard accepted `makefile_reads_variable=True`
against prose that still said the recipe "has not landed"). Round 3 closes
that hole with a third, mirrored check, so the guard now covers both
directions plus the retired-phrase forbid:

* While `_connect_recipe_reads_devcontainer_transport()` is False,
  `_connect_recipe_mentions_without_landed_caveat` flags any mention of
  the Makefile's `connect` recipe that has no "has not landed" / "not yet
  landed" caveat nearby, and `_assert_transport_landed_caveat_state` fails
  on any such mention. This is the check that catches the round-2 defect:
  a probe against the round-2 prose (test_review, round 2) confirmed the
  then-only check accepted synthetic text claiming a live Makefile reader
  while the real signal was False.
* Once `_connect_recipe_reads_devcontainer_transport()` is True, the
  mirror check, `_connect_recipe_mentions_with_landed_caveat`, flags any
  mention of the recipe that STILL carries a "has not landed" / "not yet
  landed" caveat nearby, and `_assert_transport_landed_caveat_state` fails
  on any such mention. This is the check that closes the round-3 defect:
  without it, the doc could keep asserting the recipe "has not landed"
  forever, silently, the moment E6-F2-S1-T4 actually lands its dispatch.
* Independently of either mention check, the retired "no effect at all" /
  "not read by any code" phrasing (E6-F2-S1-T4 round 2 doc_review
  REVIEW_FAIL, CONFIG_DOCS) is forbidden once EITHER reader signal is
  true, but not required while both are false, so an accurate
  no-reader-landed statement of the current, real state stays permitted
  rather than mechanically forbidden.

The `resolve_transport`-scoped "has not landed" caveat's LANDED-STATE
signal (`hasattr(transport, "resolve_transport")`) is unaffected by any of
the above: it tracks `transport.py`'s own namespace only and is unrelated
to whether the Makefile's `connect` recipe has landed. Its CAVEAT-TEXT
signal, however, was not independently scoped until round 4
(code_review REVIEW_FAIL): round 3's implementation derived it from
`bool(_LANDED_CAVEAT_PHRASE_PATTERN.search(section))`, a scan of the
WHOLE section for the same "has not landed" / "not yet landed" phrasing
the connect-recipe mention checks above require and forbid around their
own mentions. Proven by direct invocation to have two failure modes: (1)
once `resolve_transport` alone landed, an accurate, still-required
connect-recipe "not yet landed" caveat elsewhere in the section made the
`resolve_transport` forbid unsatisfiable, since the section-wide scan
could not distinguish whose caveat it was seeing; and (2) while neither
reader had landed, a connect-recipe caveat vacuously satisfied
`resolve_transport`'s requirement even when the section elsewhere
asserted, falsely, that `resolve_transport` reads the variable today.
Round 4 replaces the section-wide scan with
`_resolve_transport_mentions_with_landed_caveat`, generalized from the
connect-recipe mention-vicinity helper via the shared `_mention_vicinities`
function, so each reader's caveat text is now checked only against that
reader's own mentions, mirroring how the landed-state signals were already
independent.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from conftest import _makefile_text, _normalize_whitespace
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
    values and at least one of its two readers.

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


def _connect_recipe_body() -> str:
    """The recipe lines (tab-indented) that follow the Makefile's `connect:` target header.

    `_makefile_text` (imported from `tests/conftest.py`) already resolves
    the repository root through `devcontainer_config.repo.find_root` and
    reads the Makefile fresh for every call, so this function reuses it
    rather than re-deriving the root itself. The recipe-body extraction
    regex duplicates the pattern `tests/test_makefile_contract.py` already
    declares four times, once per Make target it inspects
    (`_test_recipe_body`, `_lint_secrets_recipe_body`, `_help_recipe_body`,
    `_cert_status_recipe_body`); this is a fifth, file-local copy for
    `connect:`. A shared `_recipe_body(makefile_text, target)` helper in
    `tests/conftest.py`, which this module already imports `_makefile_text`
    and `_normalize_whitespace` from, would let all five call sites share
    one implementation. `tests/test_makefile_contract.py` and
    `tests/conftest.py` are outside this unit's Changes Manifest
    (`docs/environment-files.md` and this file only), so that extraction is
    flagged here as a follow-up rather than performed in this change
    (code_review, E6-F2-S1-T6 round 2, DRY). No absolute path and no copy
    of the recipe text are hardcoded here: a future edit to the recipe's
    dispatch logic changes what this function returns on the very next
    call, and `_connect_recipe_reads_devcontainer_transport` below changes
    its answer with it.
    """
    match = re.search(r"^connect:.*\n((?:\t.*\n?)*)", _makefile_text(), re.MULTILINE)
    assert match is not None, "no connect target found in Makefile"
    return match.group(1)


def _connect_recipe_reads_devcontainer_transport() -> bool:
    """Whether the Makefile's `connect` recipe body references DEVCONTAINER_TRANSPORT.

    This is one of the two independent signals
    `_assert_transport_landed_caveat_state` accepts (the other,
    `hasattr(transport, "resolve_transport")`, it derives internally).
    Reading the Makefile directly, rather than trusting a single `hasattr`
    check on `transport.py`, is what lets the guard notice E6-F2-S1-T4's
    `connect` recipe landing its own, independent read of the variable: a
    prior version of this module collapsed both readers into one boolean
    and so could not tell the two apart (E6-F2-S1-T4 doc_review REVIEW_FAIL
    round 2, CONFIG_DOCS).
    """
    return "DEVCONTAINER_TRANSPORT" in _connect_recipe_body()


# Matches a mention of the Makefile's `connect` recipe. `resolve_transport`
# gets its own mirror pattern below (`_RESOLVE_TRANSPORT_MENTION_PATTERN`):
# round-4's guard collapsed both readers' caveat checks onto one section-wide
# scan of `_LANDED_CAVEAT_PHRASE_PATTERN`, which a connect-recipe caveat
# vacuously satisfied on `resolve_transport`'s behalf and which made no
# accurate prose satisfiable once `resolve_transport` alone landed
# (code_review REVIEW_FAIL, round 4); scoping each reader to its own
# mentions via the shared `_mention_vicinities` helper below closes both
# holes at once.
_CONNECT_RECIPE_MENTION_PATTERN = re.compile(r"connect`?\s*recipe")

# Matches a mention of `transport.py`'s `resolve_transport` reader.
_RESOLVE_TRANSPORT_MENTION_PATTERN = re.compile(r"resolve_transport")

# The "has not landed" / "not yet landed" caveat phrasing, reused both to
# scope a mention (this pattern) and, combined with "neither", to detect a
# blanket claim about both readers at once (`_BLANKET_NEITHER_CAVEAT_PATTERN`
# below).
_LANDED_CAVEAT_PHRASE_PATTERN = re.compile(r"has not landed|not yet landed")

# How many characters of context around a reader mention (the Makefile
# `connect` recipe or `resolve_transport`) count as "nearby" for
# caveat-scoping purposes. Wide enough to span a parenthetical aside naming
# the owning unit (e.g. "(not yet landed, E6-F2-S1-T4)"), including one that
# trails a full clause naming the reader (as the real section's
# `DEVCONTAINER_TRANSPORT` table row and closing paragraph both do), but
# tight enough that a caveat attached to a wholly different sentence about
# the OTHER reader does not count as covering this one -- verified directly
# against both the real `docs/environment-files.md` Transport section (every
# real mention of either reader has its own caveat within this window) and
# two adversarial probes (code_review, round 4): a connect-recipe caveat
# must not vacuously satisfy `resolve_transport`'s requirement when neither
# reader is mentioned together, and a `resolve_transport` mention that
# borrows a neighboring, unrelated connect-recipe caveat instead of stating
# its own must still be rejected.
_MENTION_CAVEAT_WINDOW = 40


def _mention_vicinities(section: str, pattern: re.Pattern[str]) -> list[str]:
    """The `_MENTION_CAVEAT_WINDOW`-character window either side of every match
    of `pattern` in `section`.

    Generalized (round 4, code_review REVIEW_FAIL) from a `connect`-recipe-only
    helper so `resolve_transport`'s caveat can be scoped to its OWN mentions
    the same way the Makefile `connect` recipe's caveat already is, instead of
    the two readers sharing one section-wide phrase scan. Shared by every
    mentions-with/-without-caveat pair below (`_connect_recipe_mentions_*` and
    `_resolve_transport_mentions_with_landed_caveat`) so the scanning loop is
    declared once rather than once per reader.
    """
    vicinities = []
    for match in pattern.finditer(section):
        start = max(0, match.start() - _MENTION_CAVEAT_WINDOW)
        end = min(len(section), match.end() + _MENTION_CAVEAT_WINDOW)
        vicinities.append(section[start:end])
    return vicinities


def _connect_recipe_mention_vicinities(section: str) -> list[str]:
    """The `_MENTION_CAVEAT_WINDOW`-character window either side of every
    mention of the Makefile's `connect` recipe in `section`.

    Thin wrapper over `_mention_vicinities`, kept as its own function so the
    two connect-recipe mirror checks below read the same as they did before
    the round-4 generalization.
    """
    return _mention_vicinities(section, _CONNECT_RECIPE_MENTION_PATTERN)


def _resolve_transport_mention_vicinities(section: str) -> list[str]:
    """The `_MENTION_CAVEAT_WINDOW`-character window either side of every
    mention of `resolve_transport` in `section`.

    The `resolve_transport`-scoped mirror of `_connect_recipe_mention_vicinities`
    (round 4, code_review REVIEW_FAIL): before this existed,
    `_assert_transport_landed_caveat_state` derived its `resolve_transport`
    signal from a section-wide `_LANDED_CAVEAT_PHRASE_PATTERN.search(section)`,
    which could not tell whether a matched caveat was actually attached to a
    `resolve_transport` mention or to a wholly unrelated connect-recipe
    mention elsewhere in the section.
    """
    return _mention_vicinities(section, _RESOLVE_TRANSPORT_MENTION_PATTERN)


def _connect_recipe_mentions_without_landed_caveat(section: str) -> list[str]:
    """Every mention of the Makefile's `connect` recipe with no landed-state
    caveat within `_MENTION_CAVEAT_WINDOW` characters either side.

    This is the mirror-image check to the retired-phrase forbid below: that
    one guards against a caveat phrased as a claim about whether the
    Makefile recipe currently reads the variable (going stale the moment it
    lands); this one guards against the opposite failure mode, a mention of
    the recipe with no caveat at all, which by omission implies the recipe
    already reads the variable today. A round-2 revision of this section
    (code_review/doc_review/test_review REVIEW_FAIL) caveated
    `resolve_transport` in every sentence but left one mention of the
    Makefile `connect` recipe uncaveated, and the then-single-signal guard
    did not notice; probing that guard directly (test_review, round 2)
    confirmed it also accepted synthetic text that claimed a live Makefile
    reader outright while `_connect_recipe_reads_devcontainer_transport()`
    was False.
    """
    return [
        vicinity
        for vicinity in _connect_recipe_mention_vicinities(section)
        if not _LANDED_CAVEAT_PHRASE_PATTERN.search(vicinity)
    ]


def _connect_recipe_mentions_with_landed_caveat(section: str) -> list[str]:
    """Every mention of the Makefile's `connect` recipe that STILL carries a
    landed-state caveat within `_MENTION_CAVEAT_WINDOW` characters either
    side, once the recipe has actually landed its own read of
    DEVCONTAINER_TRANSPORT.

    This is the round-3 mirror of `_connect_recipe_mentions_without_landed_caveat`
    above: that one catches a mention with no caveat while the recipe has
    NOT landed (implying, by omission, that it already reads the variable);
    this one catches a mention that still says "not yet landed" once the
    recipe genuinely HAS landed, which the round-2 guard never checked for
    at all (code_review/test_review REVIEW_FAIL, round 3). Without this
    check, `_assert_transport_landed_caveat_state` required the "not yet
    landed, E6-F2-S1-T4" caveat to be present today, but raised nothing the
    moment that caveat went stale, which `test_review` proved empirically
    by calling the guard with `makefile_reads_variable=True` against prose
    that still claimed the recipe had not landed.
    """
    return [
        vicinity
        for vicinity in _connect_recipe_mention_vicinities(section)
        if _LANDED_CAVEAT_PHRASE_PATTERN.search(vicinity)
    ]


def _resolve_transport_mentions_with_landed_caveat(section: str) -> list[str]:
    """Every mention of `resolve_transport` that carries a landed-state caveat
    within `_MENTION_CAVEAT_WINDOW` characters either side.

    Used both to REQUIRE a caveat (while `resolve_transport` has not landed,
    this list must be non-empty) and to FORBID one (once it has landed, this
    list must be empty), exactly as `_connect_recipe_mentions_with_landed_caveat`
    is reused for the Makefile reader's stale-caveat forbid. This is the
    round-4 fix (code_review REVIEW_FAIL): the prior implementation derived
    `resolve_transport`'s caveat state from
    `bool(_LANDED_CAVEAT_PHRASE_PATTERN.search(section))`, a section-wide scan
    of the SAME phrase the connect-recipe checks above require/forbid around
    their own mentions. That collapse had two proven failure modes: (1) once
    `resolve_transport` alone landed, ANY surviving connect-recipe "not yet
    landed" caveat elsewhere in the section made the forbid unsatisfiable
    (no accurate prose could pass, since the connect-recipe caveat is
    required in that state and the section-wide forbid treated it as
    `resolve_transport`'s own caveat too); and (2) while neither reader had
    landed, a connect-recipe caveat vacuously satisfied `resolve_transport`'s
    requirement even when the section elsewhere asserted, wrongly, that
    `resolve_transport` already reads the variable today. Scoping the scan to
    `resolve_transport`'s own mentions via `_resolve_transport_mention_vicinities`
    closes both holes.
    """
    return [
        vicinity
        for vicinity in _resolve_transport_mention_vicinities(section)
        if _LANDED_CAVEAT_PHRASE_PATTERN.search(vicinity)
    ]


# A blanket "neither ... has not landed" / "neither ... not yet landed"
# claim about both readers at once, anchored so a caveat sentence's "neither"
# word must be followed, within a short window that does not cross a
# sentence boundary, by the landed-state phrase itself. A bare substring
# scan for "neither" (the round-2 implementation) false-positives on this
# section's own benign independence clauses ("neither will call the other",
# "neither planned reader will call or modify that script"), neither of
# which makes any claim about landed state (test_review, round 2).
_BLANKET_NEITHER_CAVEAT_PATTERN = re.compile(
    r"neither\b[^.]{0,60}?\b(?:has not landed|not yet landed)\b"
)


def _resolve_transport_blanket_neither_caveat_present(section: str) -> bool:
    """Whether a `resolve_transport` mention's own vicinity carries a blanket
    "neither ... has/not yet landed" claim, rather than a section-wide scan.

    Round-4 fix (code_review REVIEW_FAIL): re-derives the blanket-neither
    check from the same `resolve_transport`-scoped vicinities
    `_resolve_transport_mentions_with_landed_caveat` uses, instead of
    searching `_BLANKET_NEITHER_CAVEAT_PATTERN` across the whole section.
    A section-wide search cannot tell whether a "neither" clause is actually
    attached to `resolve_transport`'s own mention or to some unrelated
    "neither" clause elsewhere in the Transport section; scoping to
    `resolve_transport`'s vicinities keeps this check honest the same way
    the caveat-presence check above already is.
    """
    return any(
        _BLANKET_NEITHER_CAVEAT_PATTERN.search(vicinity)
        for vicinity in _resolve_transport_mention_vicinities(section)
    )


def _assert_transport_landed_caveat_state(section: str, *, makefile_reads_variable: bool) -> None:
    """AC-DOC-003 caveat lifecycle guard, keyed to two independently SCOPED reader signals.

    `DEVCONTAINER_TRANSPORT` has two independent readers-to-be, the
    Makefile's `connect` recipe (E6-F2-S1-T4, `makefile_reads_variable`,
    parsed straight out of the Makefile by
    `_connect_recipe_reads_devcontainer_transport` rather than passed as a
    hardcoded literal) and `transport.resolve_transport` (E6-F2-S1-T3,
    derived internally via `hasattr(transport, "resolve_transport")`).
    Both signals are consulted independently, and -- round 4's fix,
    code_review REVIEW_FAIL -- each signal's caveat requirement/forbid is
    now scoped to THAT reader's OWN mentions (via `_mention_vicinities`),
    never to a section-wide phrase scan:

    * A mention of the Makefile's `connect` recipe with no landed-state
      caveat nearby is forbidden whenever `makefile_reads_variable` is
      False: omitting the caveat implies, by contrast with a caveated
      `resolve_transport` mention elsewhere in the same section, that the
      recipe already reads the variable today. This is checked first and
      unconditionally on `makefile_reads_variable` alone, independently of
      `resolve_transport`'s state.
    * The mirror of the above: a mention of the Makefile's `connect` recipe
      that STILL carries a landed-state caveat nearby is forbidden whenever
      `makefile_reads_variable` is True (round 3, code_review/test_review
      REVIEW_FAIL).
    * The retired "no effect at all" / "not read by any code" phrasing
      (E6-F2-S1-T4 round 2 doc_review REVIEW_FAIL, CONFIG_DOCS) is
      forbidden once EITHER reader signal is true, but not required while
      both are false: while neither reader exists, an accurate statement
      of that fact is not the defect this guard exists to catch, and
      forbidding it unconditionally would instead force the section to
      assert a live reader that does not exist (a round-2 deviation from
      AC-FIX-005, code_review/doc_review REVIEW_FAIL).
    * The `resolve_transport`-scoped "has not landed" / "not yet landed"
      caveat tracks `transport.resolve_transport` alone, using
      `_resolve_transport_mentions_with_landed_caveat` -- a caveat found
      only near a `resolve_transport` mention counts, never a caveat found
      near a connect-recipe mention instead (round 4, code_review
      REVIEW_FAIL). Before this fix, the check derived
      `resolve_transport`'s caveat state from a bare
      `_LANDED_CAVEAT_PHRASE_PATTERN.search(section)` over the WHOLE
      section, the same phrase the connect-recipe checks above
      require/forbid around their own mentions; that collapse was proven,
      by direct invocation, to make an accurate connect-recipe-only mention
      make the guard unsatisfiable once `resolve_transport` alone landed
      (the section-wide forbid fired on the connect recipe's own,
      unrelated, still-required caveat), and to let a connect-recipe
      caveat vacuously satisfy `resolve_transport`'s requirement even when
      the section elsewhere falsely claimed `resolve_transport` reads the
      variable today. It is required while `transport.resolve_transport` is
      absent and forbidden once it exists, independently of
      `makefile_reads_variable`: `transport.resolve_transport` genuinely
      still does not exist whether or not the Makefile recipe has landed
      its own, separate read of the variable. When the Makefile recipe HAS
      landed but `resolve_transport` has not, the caveat text must still
      name `resolve_transport` specifically rather than a blanket "neither
      reader has not landed" statement, which would be false the moment
      only one of the two readers exists; that blanket-phrasing check
      (`_resolve_transport_blanket_neither_caveat_present`) is itself
      scoped to `resolve_transport`'s own mentions for the same reason.

    A prior version of this function collapsed both signals into a single
    `hasattr(transport, "resolve_transport")` branch, which could not
    notice the Makefile landing its own, independent read -- exactly the
    doc_review REVIEW_FAIL this split exists to catch mechanically. A
    round-2 version of the split added the "no caveat while not landed"
    direction above but not its mirror; round 3 added the mirror; round 4
    replaced the still-collapsed section-wide `resolve_transport` scan with
    the mention-scoped version described above.
    """
    if makefile_reads_variable:
        stale_caveated = _connect_recipe_mentions_with_landed_caveat(section)
        assert not stale_caveated, (
            f"{_DOC_RELATIVE_PATH}'s '{_TRANSPORT_HEADING}' section mentions the Makefile's "
            "`connect` recipe still carrying a 'has not landed' / 'not yet landed' caveat "
            f"within {_MENTION_CAVEAT_WINDOW} characters, while "
            "_connect_recipe_reads_devcontainer_transport() is True against the real "
            f"Makefile; stale-caveated mention(s): {stale_caveated!r}."
        )
    else:
        uncaveated = _connect_recipe_mentions_without_landed_caveat(section)
        assert not uncaveated, (
            f"{_DOC_RELATIVE_PATH}'s '{_TRANSPORT_HEADING}' section mentions the Makefile's "
            "`connect` recipe with no 'has not landed' / 'not yet landed' caveat within "
            f"{_MENTION_CAVEAT_WINDOW} characters, while "
            "_connect_recipe_reads_devcontainer_transport() is False against the real "
            f"Makefile; uncaveated mention(s): {uncaveated!r}."
        )

    resolve_transport_landed = hasattr(transport, "resolve_transport")

    stale_phrase_present = "no effect at all" in section or "not read by any code" in section
    if makefile_reads_variable or resolve_transport_landed:
        assert not stale_phrase_present, (
            f"{_DOC_RELATIVE_PATH}'s '{_TRANSPORT_HEADING}' section still contains the retired "
            "'no effect at all' / 'not read by any code' phrasing (AC-FIX-001) even though a "
            "reader has landed; describe the dispatch contract and name both readers instead "
            "of claiming either one has, or has not, landed."
        )

    resolve_transport_caveated = _resolve_transport_mentions_with_landed_caveat(section)
    if resolve_transport_landed:
        assert not resolve_transport_caveated, (
            f"{_DOC_RELATIVE_PATH}'s '{_TRANSPORT_HEADING}' section still states that "
            "resolve_transport has not landed, but devcontainer_config.transport now "
            f"declares it; remove the stale caveat: {resolve_transport_caveated!r}."
        )
    else:
        assert resolve_transport_caveated, (
            f"{_DOC_RELATIVE_PATH}'s '{_TRANSPORT_HEADING}' section does not state that "
            "transport.resolve_transport has not landed in this repository yet, within "
            f"{_MENTION_CAVEAT_WINDOW} characters of a `resolve_transport` mention."
        )
        if makefile_reads_variable:
            assert not _resolve_transport_blanket_neither_caveat_present(section), (
                f"{_DOC_RELATIVE_PATH}'s '{_TRANSPORT_HEADING}' section's pending-reader "
                "caveat still uses blanket 'neither ... has/not yet landed' phrasing, but the "
                "Makefile's `connect` recipe now reads DEVCONTAINER_TRANSPORT; scope the "
                "remaining caveat to `transport.resolve_transport` alone."
            )


def test_connect_recipe_reads_devcontainer_transport_extracts_from_the_real_makefile() -> None:
    """AC-FIX-004: the helper parses the Makefile rather than returning a static value.

    Reads `_connect_recipe_body()` directly (not just the boolean helper
    built on top of it) so this test fails if the extraction ever stops
    finding a `connect:` target at all, independently of whatever the
    recipe body currently contains.
    """
    body = _connect_recipe_body()
    assert body.strip(), (
        "_connect_recipe_body() returned an empty recipe body; the `connect:` target "
        "extraction regex may be stale against the current Makefile."
    )
    assert _connect_recipe_reads_devcontainer_transport() == (
        "DEVCONTAINER_TRANSPORT" in body
    ), (
        "_connect_recipe_reads_devcontainer_transport() must equal "
        "'DEVCONTAINER_TRANSPORT' in _connect_recipe_body(), not a hardcoded literal: "
        f"body={body!r}."
    )


def test_transport_section_describes_connect_entry_point() -> None:
    """AC-DOC-003: the section describes `make connect` and states the DEVCONTAINER_TRANSPORT
    dispatch contract, naming both readers without claiming either one's landed state.

    Both caveats' presence is checked by `_assert_transport_landed_caveat_state`,
    fed the real, current state of both independent signals
    (`_connect_recipe_reads_devcontainer_transport()` for the Makefile
    reader, `hasattr(transport, "resolve_transport")` internally for the
    module reader) rather than pinning either caveat's wording
    unconditionally; see that function's docstring for why a collapsed,
    single-signal pin would go silently false.
    """
    section = _transport_section_text_normalized()
    assert "make connect" in section, (
        f"{_DOC_RELATIVE_PATH}'s '{_TRANSPORT_HEADING}' section does not describe the "
        "`make connect` entry point."
    )
    assert "resolve_transport" in section, (
        f"{_DOC_RELATIVE_PATH}'s '{_TRANSPORT_HEADING}' section does not name "
        "`resolve_transport` as one of DEVCONTAINER_TRANSPORT's readers."
    )
    _assert_transport_landed_caveat_state(
        section, makefile_reads_variable=_connect_recipe_reads_devcontainer_transport()
    )


# The four independent (makefile_reads_variable, resolve_transport_present)
# combinations `_assert_transport_landed_caveat_state` must branch on. Each
# `pytest.param` below supplies the exact section text that combination
# demands (`section_text`, `should_raise=False`) plus at least one mutated
# text proving the two signals are never collapsed into one boolean: text
# correct for a different combination fails here (`should_raise=True`) even
# though a single-signal guard would have accepted it.
_NEITHER_LANDED_TEXT = "make connect ... resolve_transport ... has not landed"
_MAKEFILE_ONLY_TEXT = (
    "make connect reads DEVCONTAINER_TRANSPORT ... resolve_transport has not landed"
)
_BLANKET_NEITHER_TEXT = "make connect ... resolve_transport ... neither reader has not landed"
_BOTH_OR_RESOLVE_ONLY_TEXT = "make connect ... resolve_transport only"
_STALE_NO_EFFECT_TEXT = "make connect ... resolve_transport ... no effect at all"
_STALE_NOT_READ_TEXT = "make connect ... resolve_transport ... not read by any code"

# AC-FIX-005 fix (code_review/doc_review REVIEW_FAIL, round 2): the retired
# stale-phrase forbid must be permitted, not mechanically prohibited, while
# neither reader has landed, so an accurate statement of the real current
# state stays writable.
_NEITHER_LANDED_TRUE_STATEMENT_TEXT = (
    "make connect ... resolve_transport has not landed ... setting it has no effect at all"
)

# Proves `makefile_reads_variable` alone (independent of resolve_transport)
# also triggers the stale-phrase forbid, closing the round-2 finding that
# the parameter was dead at its only real call site (code_review).
_MAKEFILE_ONLY_STALE_TEXT = (
    "make connect ... resolve_transport has not landed ... no effect at all"
)

# Proves the mirror-image check (`_connect_recipe_mentions_without_landed_caveat`)
# fires: a mention of the Makefile's `connect` recipe reading/dispatching the
# variable with no landed-state caveat nearby, exactly the shape of text a
# probe against the round-2 guard confirmed it accepted (test_review, round 2).
_MAKEFILE_RECIPE_UNCAVEATED_TEXT = (
    "make connect recipe reads DEVCONTAINER_TRANSPORT and dispatches it, resolve_transport only"
)

# Round 3 fix (code_review/test_review REVIEW_FAIL): proves the mirror
# branch of the mirror check, `_connect_recipe_mentions_with_landed_caveat`,
# fires once `makefile_reads_variable` is True. `test_review` proved the
# round-2 guard accepted this exact shape of text (a `connect` recipe
# mention that still carries a "not yet landed" caveat) with
# `makefile_reads_variable=True`, which is the one-directional hole this
# constant's two parametrized cases below close. The same string is reused
# for both the `resolve_transport`-absent and `resolve_transport`-present
# cases because the mirror check must fire independently of that signal;
# the assertion it triggers raises before either downstream check
# (stale-phrase forbid, `resolve_transport` caveat state) is reached.
_MAKEFILE_LANDED_RECIPE_STILL_CAVEATED_TEXT = (
    "the Makefile's connect recipe (not yet landed, E6-F2-S1-T4) dispatches "
    "DEVCONTAINER_TRANSPORT, resolve_transport has not landed"
)

# Round 4 fix (code_review REVIEW_FAIL, AC-FIX-006): the prior
# `(makefile_reads_variable=False, resolve_transport_present=True)` passing
# case fed `_BOTH_OR_RESOLVE_ONLY_TEXT`, which contains no `connect`?\s*recipe`
# match at all, so it never exercised the connect-recipe mention branch --
# concealing the AC-FIX-005 signal-collapse defect this round fixes. This
# text mentions the Makefile's `connect` recipe WITH its required "not yet
# landed" caveat (satisfying `makefile_reads_variable=False`'s requirement)
# AND states plainly that `resolve_transport` already reads the variable,
# with no caveat anywhere near that mention (satisfying
# `resolve_transport_present=True`'s forbid). It only passes once the
# `resolve_transport` caveat-forbid is scoped to `resolve_transport`'s own
# mentions: the pre-fix, section-wide scan would have found the
# connect-recipe caveat and (wrongly) treated it as a stale
# `resolve_transport` caveat, failing this case that is supposed to pass.
_RESOLVE_ONLY_CAVEATED_RECIPE_TEXT = (
    "the Makefile's connect recipe (not yet landed, E6-F2-S1-T4) will dispatch. "
    "resolve_transport reads DEVCONTAINER_TRANSPORT today."
)

# The should_raise=True mirror of the text above (code_review REVIEW_FAIL,
# AC-FIX-006): identical except the connect-recipe mention's required "not
# yet landed" caveat is omitted, so `_connect_recipe_mentions_without_landed_caveat`
# must still fire for this combination even though `resolve_transport` has
# landed, proving the two mention checks stay independent of each other.
_RESOLVE_ONLY_RECIPE_UNCAVEATED_TEXT = (
    "the Makefile's connect recipe will dispatch. "
    "resolve_transport reads DEVCONTAINER_TRANSPORT today."
)

# Distinctive fragments of each raising branch's assertion message, matched
# via `pytest.raises(..., match=...)` so a `should_raise=True` case can only
# go green by hitting the specific assertion its id names, not a different
# one in the same function (test_review, round 3, warn).
_MISSING_RESOLVE_TRANSPORT_CAVEAT_MATCH = (
    r"does not state that transport\.resolve_transport has not landed"
)
_UNCAVEATED_RECIPE_MENTION_MATCH = r"no 'has not landed' / 'not yet landed' caveat within"
_STALE_CAVEATED_RECIPE_MENTION_MATCH = (
    r"still carrying a 'has not landed' / 'not yet landed' caveat"
)
_STALE_PHRASE_MATCH = (
    r"still contains the retired 'no effect at all' / 'not read by any code' phrasing"
)
_BLANKET_NEITHER_MATCH = r"pending-reader caveat still uses blanket 'neither"
_STALE_RESOLVE_TRANSPORT_CAVEAT_MATCH = r"remove the stale caveat"


@pytest.mark.parametrize(
    (
        "makefile_reads_variable",
        "resolve_transport_present",
        "section_text",
        "should_raise",
        "match",
    ),
    [
        pytest.param(
            False,
            False,
            _NEITHER_LANDED_TEXT,
            False,
            None,
            id="neither-landed-resolve-caveat-present-passes",
        ),
        pytest.param(
            False,
            False,
            _BOTH_OR_RESOLVE_ONLY_TEXT,
            True,
            _MISSING_RESOLVE_TRANSPORT_CAVEAT_MATCH,
            id="neither-landed-resolve-caveat-missing-fails",
        ),
        pytest.param(
            False,
            False,
            _NEITHER_LANDED_TRUE_STATEMENT_TEXT,
            False,
            None,
            id="neither-landed-stale-phrase-permitted-when-true-passes",
        ),
        pytest.param(
            False,
            True,
            _MAKEFILE_RECIPE_UNCAVEATED_TEXT,
            True,
            _UNCAVEATED_RECIPE_MENTION_MATCH,
            id="makefile-not-landed-recipe-mention-uncaveated-fails",
        ),
        pytest.param(
            True,
            False,
            _MAKEFILE_ONLY_TEXT,
            False,
            None,
            id="makefile-only-scoped-resolve-caveat-passes",
        ),
        pytest.param(
            True,
            False,
            _MAKEFILE_ONLY_STALE_TEXT,
            True,
            _STALE_PHRASE_MATCH,
            id="makefile-only-stale-no-effect-phrase-fails",
        ),
        pytest.param(
            True,
            False,
            _BLANKET_NEITHER_TEXT,
            True,
            _BLANKET_NEITHER_MATCH,
            id="makefile-only-blanket-neither-caveat-fails",
        ),
        pytest.param(
            True,
            False,
            _BOTH_OR_RESOLVE_ONLY_TEXT,
            True,
            _MISSING_RESOLVE_TRANSPORT_CAVEAT_MATCH,
            id="makefile-only-missing-resolve-caveat-fails",
        ),
        pytest.param(
            True,
            False,
            _MAKEFILE_LANDED_RECIPE_STILL_CAVEATED_TEXT,
            True,
            _STALE_CAVEATED_RECIPE_MENTION_MATCH,
            id="makefile-landed-recipe-mention-still-caveated-resolve-absent-fails",
        ),
        pytest.param(
            True,
            True,
            _MAKEFILE_LANDED_RECIPE_STILL_CAVEATED_TEXT,
            True,
            _STALE_CAVEATED_RECIPE_MENTION_MATCH,
            id="makefile-landed-recipe-mention-still-caveated-resolve-present-fails",
        ),
        pytest.param(
            False,
            True,
            _RESOLVE_ONLY_CAVEATED_RECIPE_TEXT,
            False,
            None,
            id="resolve-only-caveat-forbidden-and-absent-passes",
        ),
        pytest.param(
            False,
            True,
            _RESOLVE_ONLY_RECIPE_UNCAVEATED_TEXT,
            True,
            _UNCAVEATED_RECIPE_MENTION_MATCH,
            id="resolve-only-recipe-mention-uncaveated-fails",
        ),
        pytest.param(
            False,
            True,
            _NEITHER_LANDED_TEXT,
            True,
            _STALE_RESOLVE_TRANSPORT_CAVEAT_MATCH,
            id="resolve-only-stale-resolve-caveat-fails",
        ),
        pytest.param(
            False,
            True,
            _STALE_NOT_READ_TEXT,
            True,
            _STALE_PHRASE_MATCH,
            id="resolve-only-stale-not-read-phrase-fails",
        ),
        pytest.param(
            True,
            True,
            _BOTH_OR_RESOLVE_ONLY_TEXT,
            False,
            None,
            id="both-landed-caveat-forbidden-and-absent-passes",
        ),
        pytest.param(
            True,
            True,
            _STALE_NO_EFFECT_TEXT,
            True,
            _STALE_PHRASE_MATCH,
            id="both-landed-stale-no-effect-phrase-fails",
        ),
        pytest.param(
            True,
            True,
            _MAKEFILE_ONLY_TEXT,
            True,
            _STALE_RESOLVE_TRANSPORT_CAVEAT_MATCH,
            id="both-landed-stale-resolve-caveat-fails",
        ),
    ],
)
def test_transport_landed_caveat_state_branches_on_reader_signals(
    monkeypatch: pytest.MonkeyPatch,
    makefile_reads_variable: bool,
    resolve_transport_present: bool,
    section_text: str,
    should_raise: bool,
    match: str | None,
) -> None:
    """Exercises all four combinations of the two independent reader signals
    against synthetic section text, independently of the current, real
    state of `docs/environment-files.md`, the Makefile and
    `devcontainer_config.transport`.

    Supersedes the single-signal version of this test (fixed a doc_review
    REVIEW_FAIL, E6-F2-S1-T4 round 2, CONFIG_DOCS): that version branched
    only on `hasattr(transport, "resolve_transport")`, so it could not
    notice a landing of the Makefile's own, independent read of
    DEVCONTAINER_TRANSPORT -- a prose correction that named the Makefile as
    a reader while `resolve_transport` stayed absent would have passed
    that guard by accident. Each combination here supplies text that is
    correct for a DIFFERENT combination, or that reintroduces the retired
    "no effect at all" / "not read by any code" phrasing, as a
    `should_raise=True` case, so a future regression in either direction
    fails this suite rather than surviving to review. Every `should_raise=True`
    case also supplies a `match` fragment anchored on the specific assertion
    message its id names, so a case can only go green by hitting the
    assertion it claims to (test_review, round 3, warn: a bare
    `pytest.raises(AssertionError)` cannot tell two different failing
    assertions apart).

    Round 2 (code_review/doc_review/test_review REVIEW_FAIL) added three
    more cases: `neither-landed-stale-phrase-permitted-when-true-passes`
    proves the retired phrasing is now permitted, not forbidden, while
    both signals are false (AC-FIX-005's "once EITHER reader has landed"
    was previously enforced unconditionally); `makefile-only-stale-no-
    effect-phrase-fails` proves `makefile_reads_variable` alone, not just
    `resolve_transport`, triggers that forbid; and `makefile-not-landed-
    recipe-mention-uncaveated-fails` proves the mirror-image check --
    text that mentions the Makefile's `connect` recipe reading or
    dispatching the variable with no landed-state caveat nearby fails
    while `makefile_reads_variable` is False, which a probe against the
    round-2 implementation (test_review) confirmed it previously accepted.

    Round 3 (code_review/test_review REVIEW_FAIL) added two more cases,
    `makefile-landed-recipe-mention-still-caveated-resolve-absent-fails`
    and `...-resolve-present-fails`: `test_review` proved the guard was
    one-directional, requiring a "not yet landed, E6-F2-S1-T4" caveat on
    the Makefile recipe today but never forbidding it once
    `makefile_reads_variable` flips True, so the doc would go silently
    stale the moment E6-F2-S1-T4 lands. Both cases supply the same text
    with `resolve_transport_present` on each side, proving the new mirror
    check fires independently of that other signal.

    Round 4 (code_review REVIEW_FAIL, AC-FIX-005/AC-FIX-006) replaced the
    `(False, True)` passing case's text and added its `should_raise=True`
    mirror. The prior passing case, `_BOTH_OR_RESOLVE_ONLY_TEXT`, contains
    no `connect`?\\s*recipe` match at all, so it never exercised the
    connect-recipe mention branch for this combination -- exactly the
    vacuity that concealed the `resolve_transport`-signal-collapse defect
    round 4 fixes. `resolve-only-caveat-forbidden-and-absent-passes` now
    supplies `_RESOLVE_ONLY_CAVEATED_RECIPE_TEXT`, which DOES mention the
    Makefile `connect` recipe with its required caveat AND states
    `resolve_transport` has landed with no caveat nearby; it can only pass
    once `_resolve_transport_mentions_with_landed_caveat` scopes the
    caveat-forbid to `resolve_transport`'s own mentions, since the prior
    section-wide scan would have (wrongly) treated the connect recipe's own,
    still-required caveat as a stale `resolve_transport` caveat and failed
    this case. `resolve-only-recipe-mention-uncaveated-fails` is its mirror,
    identical text with the connect-recipe caveat omitted, proving the
    connect-recipe mention check still fires for this combination
    independently of `resolve_transport`'s landed state.
    """
    if resolve_transport_present:
        monkeypatch.setattr(transport, "resolve_transport", lambda: None, raising=False)
    else:
        monkeypatch.delattr(transport, "resolve_transport", raising=False)
    if should_raise:
        with pytest.raises(AssertionError, match=match):
            _assert_transport_landed_caveat_state(
                section_text, makefile_reads_variable=makefile_reads_variable
            )
    else:
        _assert_transport_landed_caveat_state(
            section_text, makefile_reads_variable=makefile_reads_variable
        )


def test_blanket_neither_caveat_pattern_does_not_match_real_section() -> None:
    """Regression test for a false-positive bare `"neither" not in section` scan
    (test_review, round 2): the real Transport section's benign independence
    clauses ("neither will call the other", "neither planned reader will call
    or modify that script") state no claim about landed state and must not
    trip `_BLANKET_NEITHER_CAVEAT_PATTERN`.

    This is narrower than the round-2/round-3 version of this test, which
    invoked the WHOLE `_assert_transport_landed_caveat_state` guard with
    `makefile_reads_variable=True`. That was too broad a claim once the
    round-3 mirror check (`_connect_recipe_mentions_with_landed_caveat`)
    exists: today's real section correctly, accurately caveats the
    Makefile's `connect` recipe as "not yet landed" at every mention (the
    recipe genuinely has not landed, see the module docstring), so
    invoking the whole guard with `makefile_reads_variable=True` is SUPPOSED
    to fail now -- that is the round-3 fix working, not a regression
    (code_review/test_review REVIEW_FAIL, round 3). Asserting the whole
    guard passes today would re-cement the exact one-directional staleness
    hole this unit exists to close. Only the narrower claim this test
    actually needs, that the blanket-neither pattern itself does not
    false-positive on this section's benign independence clauses, stays
    true regardless of which reader lands next.
    """
    section = _transport_section_text_normalized()
    assert _BLANKET_NEITHER_CAVEAT_PATTERN.search(section) is None, (
        f"{_DOC_RELATIVE_PATH}'s '{_TRANSPORT_HEADING}' section false-positives against "
        "_BLANKET_NEITHER_CAVEAT_PATTERN on a benign independence clause that makes no "
        "claim about landed state."
    )


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
