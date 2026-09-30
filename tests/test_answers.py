"""Tests for devcontainer_config.answers.

The `devcontainer_config` import is deferred into function bodies (see
`_import_answers`) rather than done once at module scope, for the same reason
`tests/test_repo.py` defers it: the TDD RED gate stashes this unit's
production-source files and re-runs a single named test node, and a
module-level import would fail COLLECTION for the whole file (pytest exit
code 2, no test outcome recorded) rather than failing the one test that
actually exercises the missing module (pytest exit 1, a real FAILED result).

`_valid_local_payload`, `_valid_remote_payload` and `_valid_aws_profile`
(imported from `tests/conftest.py`) each build a complete,
independently-valid answer set from values shaped like the real committed
examples (`shell.env.example`, `.devcontainer/aws-profile-map.json.example`)
rather than inventing arbitrary strings, so a test that asserts "no
problems" is actually proving the validator accepts realistic input. The
account-id, remote-instance-id and SSO-portal-URL fields are the deliberate
exception, but not in the same way. The account-id and remote-instance-id
fields are generated at runtime by `tests/conftest.py._synthetic_account_id`
and `tests/conftest.py._synthetic_instance_id`, the same pattern
`tests/test_secrets.py._sample_account_id` already uses for the identical
detector -- no AWS-shaped digit or hex run is ever a literal in this file's
or `tests/conftest.py`'s source text, and each test run exercises a freshly
generated value rather than a fixed one. The SSO-portal-URL field is
different: its *literal value* intentionally departs from
`.devcontainer/aws-profile-map.json.example`'s
`https://<your-sso-portal>.awsapps.com/start` shape, using a non-AWS
placeholder domain instead. See `tests/conftest.py._valid_aws_profile` for
why that departure is required rather than merely permitted.
"""

from __future__ import annotations

import importlib
from types import ModuleType
from typing import Any

import pytest
from conftest import (
    _synthetic_account_id,
    _synthetic_instance_id,
    _valid_aws_profile,
    _valid_local_payload,
    _valid_remote_payload,
)


def _import_answers() -> ModuleType:
    """Import devcontainer_config.answers from inside a function body.

    See the module docstring for why this is deferred rather than done at
    import time.
    """
    return importlib.import_module("devcontainer_config.answers")


# ---------------------------------------------------------------------------
# AC-FUNC-001 / AC-TEST-001: the fifteen-row spec Section 5.1 field table.
# ---------------------------------------------------------------------------

_FIELD_TABLE: tuple[tuple[str, str, Any], ...] = (
    ("backend", "always", None),
    ("developer_name", "always", None),
    ("git_user", "always", None),
    ("git_user_email", "always", None),
    ("git_provider_url", "always", "github.com"),
    ("default_git_branch", "always", "main"),
    ("template_name", "always", None),
    ("aws_config_enabled", "always", None),
    ("local_docker_context", "always", None),
    ("host_proxy", "always", None),
    ("aws_profiles", "aws", None),
    ("host_proxy_url", "proxy", None),
    ("remote_instance_id", "remote", None),
    ("remote_aws_region", "remote", "us-east-1"),
    ("remote_aws_profile", "remote", "default"),
)


@pytest.mark.parametrize(("field_name", "requiredness_label", "default"), _FIELD_TABLE)
def test_fields_table_matches_spec_section_5_1(
    field_name: str, requiredness_label: str, default: Any
) -> None:
    answers = _import_answers()
    requiredness_by_label = {
        "always": answers.Requiredness.ALWAYS,
        "remote": answers.Requiredness.WHEN_REMOTE,
        "aws": answers.Requiredness.WHEN_AWS,
        "proxy": answers.Requiredness.WHEN_PROXY,
    }

    field = answers.FIELDS_BY_NAME[field_name]

    assert field.required is requiredness_by_label[requiredness_label]
    assert field.default == default


def test_fields_declares_no_fields_outside_the_schema() -> None:
    """AC-FUNC-001 plus AC-FUNC-011: the fifteen table rows, plus the one
    the schema table alone; remote_ssh_key_path was removed with SSH
    -- and nothing else.
    """
    answers = _import_answers()
    expected = {name for name, _, _ in _FIELD_TABLE}

    assert set(answers.FIELDS_BY_NAME) == expected
    assert len(answers.FIELDS) == len(expected)


def test_remote_ssh_key_path_is_no_longer_declared() -> None:
    """The field was removed with the SSH transport it existed to configure.

    It survived phase 1 only because the SSH scripts still read the value it
    rendered. Those scripts are gone, so a schema that still declared it would
    ask an operator for a path nothing reads.
    """
    answers = _import_answers()
    assert "remote_ssh_key_path" not in answers.FIELDS_BY_NAME
    assert all(field.name != "remote_ssh_key_path" for field in answers.FIELDS)


def test_a_payload_still_carrying_the_removed_field_is_rejected() -> None:
    """An old answers file is refused by name rather than silently ignored.

    Silently dropping it would leave an operator believing a key path is still
    configured and still used. The rejection names the field so the fix is
    obvious.
    """
    answers = _import_answers()
    payload = _valid_remote_payload()
    payload["remote_ssh_key_path"] = "/home/someone/.ssh/old-key.pem"
    problems = answers.validate(payload)
    assert any("remote_ssh_key_path" in str(problem) for problem in problems), (
        "a payload carrying the removed field must be rejected naming that field, "
        f"got: {problems}"
    )


def test_backends_is_exactly_local_and_remote() -> None:
    """`BACKENDS` is public so a consumer outside this module can validate a
    `backend` answer without re-declaring the literal set (spec Section 1.1).
    """
    answers = _import_answers()

    assert answers.BACKENDS == {"local", "remote"}


# ---------------------------------------------------------------------------
# AC-FUNC-002 / AC-TEST-002: branching requiredness.
# ---------------------------------------------------------------------------

_ALWAYS_FIELD_NAMES = frozenset(name for name, label, _ in _FIELD_TABLE if label == "always")
_REMOTE_FIELD_NAMES = frozenset(
    {"remote_instance_id", "remote_aws_region", "remote_aws_profile"}
)


@pytest.mark.parametrize(
    ("backend", "aws_config_enabled", "host_proxy"),
    [
        ("local", False, False),
        ("remote", False, False),
        ("local", True, False),
        ("local", False, True),
        ("remote", True, False),
        ("remote", False, True),
        ("local", True, True),
        ("remote", True, True),
    ],
)
def test_required_fields_follows_branching(
    backend: str, aws_config_enabled: bool, host_proxy: bool
) -> None:
    answers = _import_answers()
    context = {
        "backend": backend,
        "aws_config_enabled": aws_config_enabled,
        "host_proxy": host_proxy,
    }

    required = set(answers.required_fields(context))

    assert _ALWAYS_FIELD_NAMES <= required
    if backend == "remote":
        assert _REMOTE_FIELD_NAMES <= required
    else:
        assert not (_REMOTE_FIELD_NAMES & required)
    assert ("aws_profiles" in required) is aws_config_enabled
    assert ("host_proxy_url" in required) is host_proxy


# ---------------------------------------------------------------------------
# AC-FUNC-007/008 and AC-TEST-003: accept-and-reject tables per pattern
# validator. Every rejection asserts the message names the field.
# ---------------------------------------------------------------------------

# Generated via tests/conftest.py._synthetic_instance_id rather than a bare
# literal: an 8-or-17-hex-char string prefixed with 'i-' is, by construction,
# indistinguishable in shape from a real EC2 instance identifier -- the exact
# shape devcontainer_config.secrets' ec2-instance-id detector keys on. Each
# value still needs that exact valid shape to exercise the True (accepted)
# rows of this table, so it is generated at runtime rather than written as a
# source literal at all.
_VALID_8HEX_INSTANCE_ID = _synthetic_instance_id(8)
_VALID_17HEX_INSTANCE_ID = _synthetic_instance_id(17)

_PATTERN_CASES: tuple[tuple[str, Any, bool], ...] = (
    # git_provider_url -- bare host, no scheme, no path (AC-FUNC-007)
    ("git_provider_url", "github.com", True),
    ("git_provider_url", "git.example.co.uk", True),
    ("git_provider_url", "https://github.com", False),
    ("git_provider_url", "github.com/path", False),
    ("git_provider_url", "github.com:443", False),
    ("git_provider_url", "<github-host>", False),  # placeholder branch
    ("git_provider_url", "", False),  # empty-string branch
    # remote_instance_id -- 'i-' plus exactly 8 or 17 hex chars (AC-FUNC-008)
    ("remote_instance_id", _VALID_8HEX_INSTANCE_ID, True),
    ("remote_instance_id", _VALID_17HEX_INSTANCE_ID, True),
    ("remote_instance_id", "i-0123abc", False),
    ("remote_instance_id", "i-01234abg", False),
    ("remote_instance_id", "i-01234ABC", False),
    # remote_aws_region -- AWS region form
    ("remote_aws_region", "us-east-1", True),
    ("remote_aws_region", "us-gov-west-1", True),
    ("remote_aws_region", "US-EAST-1", False),
    ("remote_aws_region", "us-east", False),
    ("remote_aws_region", "us_east_1", False),
    # host_proxy_url -- scheme://host:port, port mandatory
    ("host_proxy_url", "http://proxy.example.com:8080", True),
    ("host_proxy_url", "https://10.0.0.1:3128", True),
    ("host_proxy_url", "http://proxy.example.com", False),
    ("host_proxy_url", "proxy.example.com:8080", False),
    ("host_proxy_url", "http://proxy.example.com:abc", False),
    # default_git_branch -- valid git branch name
    ("default_git_branch", "main", True),
    ("default_git_branch", "feature/new-thing", True),
    ("default_git_branch", "-main", False),
    ("default_git_branch", "main.lock", False),
    ("default_git_branch", "fea..ture", False),
    ("default_git_branch", "br anch", False),
    ("default_git_branch", "<branch>", False),  # placeholder branch
    ("default_git_branch", "", False),  # empty-string branch
    # remote_aws_profile -- profile name
    ("remote_aws_profile", "default", True),
    ("remote_aws_profile", "dev-admin", True),
    ("remote_aws_profile", "has space", False),
    ("remote_aws_profile", "weird!name", False),
    # git_user_email -- email address
    ("git_user_email", "dev@example.com", True),
    ("git_user_email", "first.last+tag@example.co.uk", True),
    ("git_user_email", "not-an-email", False),
    ("git_user_email", "missing-at.example.com", False),
    ("git_user_email", "double@@example.com", False),
)


def _assert_validator_accepts_or_rejects(field_name: str, value: Any, expect_valid: bool) -> None:
    """Shared body for both the pattern-case and scalar-case tables below.

    A rejection must name the field in every returned problem, so a failing
    interview tells the operator which answer to fix (both AC-TEST-003 and
    the scalar validators share this same contract).
    """
    answers = _import_answers()
    field = answers.FIELDS_BY_NAME[field_name]

    problems = field.validator(field_name, value)

    if expect_valid:
        assert problems == []
    else:
        assert problems
        assert all(field_name in problem for problem in problems)


@pytest.mark.parametrize(("field_name", "value", "expect_valid"), _PATTERN_CASES)
def test_pattern_validators_accept_and_reject(
    field_name: str, value: Any, expect_valid: bool
) -> None:
    _assert_validator_accepts_or_rejects(field_name, value, expect_valid)


# ---------------------------------------------------------------------------
# Scalar (non-pattern) validators: backend enum and the two boolean fields.
# ---------------------------------------------------------------------------

_SCALAR_CASES: tuple[tuple[str, Any, bool], ...] = (
    ("backend", "local", True),
    ("backend", "remote", True),
    ("backend", "hybrid", False),
    ("backend", 1, False),
    ("backend", "<local-or-remote>", False),  # placeholder branch
    ("aws_config_enabled", True, True),
    ("aws_config_enabled", False, True),
    ("aws_config_enabled", "true", False),
    ("host_proxy", True, True),
    ("host_proxy", "yes", False),
)


@pytest.mark.parametrize(("field_name", "value", "expect_valid"), _SCALAR_CASES)
def test_scalar_validators_accept_and_reject(
    field_name: str, value: Any, expect_valid: bool
) -> None:
    _assert_validator_accepts_or_rejects(field_name, value, expect_valid)


@pytest.mark.parametrize(
    "field_name",
    ["developer_name", "git_user", "template_name", "local_docker_context"],
)
def test_text_fields_reject_non_string_and_empty(field_name: str) -> None:
    answers = _import_answers()
    field = answers.FIELDS_BY_NAME[field_name]

    assert field.validator(field_name, 123) != []
    assert field.validator(field_name, "") != []
    assert field.validator(field_name, "   ") != []
    assert field.validator(field_name, "Ada Lovelace") == []


# ---------------------------------------------------------------------------
# AC-FUNC-004: an unreplaced placeholder always fails.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field_name",
    ["developer_name", "git_user", "template_name", "local_docker_context"],
)
def test_placeholder_value_always_fails(field_name: str) -> None:
    answers = _import_answers()
    field = answers.FIELDS_BY_NAME[field_name]

    problems = field.validator(field_name, "<placeholder>")

    assert problems
    assert all("placeholder" in problem.lower() for problem in problems)


def test_placeholder_fails_even_when_shape_is_otherwise_plausible() -> None:
    """developer_name has no rule beyond 'non-empty text', so '<Your Name>'
    (the literal value shell.env.example ships) would otherwise pass; the
    placeholder rule catches it regardless (AC-FUNC-004).
    """
    answers = _import_answers()
    field = answers.FIELDS_BY_NAME["developer_name"]

    problems = field.validator("developer_name", "<Your Name>")

    assert problems


# ---------------------------------------------------------------------------
# AC-FUNC-009: aws_profiles error paths.
# ---------------------------------------------------------------------------

# Generated via tests/conftest.py._synthetic_account_id, the same rationale
# as _VALID_8HEX_INSTANCE_ID / _VALID_17HEX_INSTANCE_ID above: this needs to
# be a second, distinct valid 12-digit account id, generated at runtime
# rather than written as a source literal at all.
_OTHER_VALID_ACCOUNT_ID = _synthetic_account_id()


def test_aws_profiles_rejects_non_list() -> None:
    answers = _import_answers()
    field = answers.FIELDS_BY_NAME["aws_profiles"]

    problems = field.validator("aws_profiles", "not-a-list")

    assert problems
    assert all("aws_profiles" in problem for problem in problems)


def test_aws_profiles_rejects_empty_list() -> None:
    answers = _import_answers()
    field = answers.FIELDS_BY_NAME["aws_profiles"]

    problems = field.validator("aws_profiles", [])

    assert problems
    assert all("aws_profiles" in problem for problem in problems)


def test_aws_profiles_rejects_entry_that_is_not_an_object() -> None:
    answers = _import_answers()
    field = answers.FIELDS_BY_NAME["aws_profiles"]

    problems = field.validator("aws_profiles", ["not-an-object"])

    assert any("aws_profiles[0]" in problem for problem in problems)


def test_aws_profiles_rejects_entry_missing_a_sub_field() -> None:
    answers = _import_answers()
    field = answers.FIELDS_BY_NAME["aws_profiles"]
    entry = _valid_aws_profile()
    del entry["role_name"]

    problems = field.validator("aws_profiles", [entry])

    assert any("role_name" in p and "aws_profiles[0]" in p for p in problems)


def test_aws_profiles_entry_missing_name_does_not_crash_duplicate_check() -> None:
    answers = _import_answers()
    field = answers.FIELDS_BY_NAME["aws_profiles"]
    entry = _valid_aws_profile()
    del entry["name"]

    problems = field.validator("aws_profiles", [entry])

    assert any("name" in p and "aws_profiles[0]" in p for p in problems)


def test_aws_profiles_rejects_entry_whose_sub_field_fails_its_own_rule() -> None:
    answers = _import_answers()
    field = answers.FIELDS_BY_NAME["aws_profiles"]
    entry = _valid_aws_profile()
    entry["account_id"] = "not-numeric"

    problems = field.validator("aws_profiles", [entry])

    assert any("account_id" in p for p in problems)


def test_aws_profiles_entry_sub_field_placeholder_fails() -> None:
    answers = _import_answers()
    field = answers.FIELDS_BY_NAME["aws_profiles"]
    entry = _valid_aws_profile()
    entry["account_name"] = "<account-alias>"

    problems = field.validator("aws_profiles", [entry])

    assert any("account_name" in p for p in problems)


def test_aws_profiles_rejects_duplicated_profile_name() -> None:
    answers = _import_answers()
    field = answers.FIELDS_BY_NAME["aws_profiles"]
    first = _valid_aws_profile(name="dev")
    second = _valid_aws_profile(name="dev", account_id=_OTHER_VALID_ACCOUNT_ID)

    problems = field.validator("aws_profiles", [first, second])

    assert any("duplicates" in p and "dev" in p for p in problems)


def test_aws_profiles_accepts_a_well_formed_list() -> None:
    answers = _import_answers()
    field = answers.FIELDS_BY_NAME["aws_profiles"]

    problems = field.validator(
        "aws_profiles",
        [
            _valid_aws_profile(name="dev"),
            _valid_aws_profile(name="prod", account_id=_OTHER_VALID_ACCOUNT_ID),
        ],
    )

    assert problems == []


# ---------------------------------------------------------------------------
# AC-FUNC-003/005/006: validate() accumulates, flags unknown keys, and still
# validates present-but-not-required answers.
# ---------------------------------------------------------------------------


def test_validate_returns_no_problems_for_a_complete_local_payload() -> None:
    answers = _import_answers()

    assert answers.validate(_valid_local_payload()) == []


def test_validate_returns_no_problems_for_a_complete_remote_payload_with_aws_and_proxy() -> None:
    answers = _import_answers()
    payload = _valid_remote_payload()
    payload["aws_config_enabled"] = True
    payload["aws_profiles"] = [_valid_aws_profile()]
    payload["host_proxy"] = True
    payload["host_proxy_url"] = "http://proxy.example.com:8080"

    assert answers.validate(payload) == []


def test_validate_reports_every_failing_field_not_just_first() -> None:
    answers = _import_answers()
    payload = _valid_remote_payload()
    payload["git_provider_url"] = "https://github.com"
    payload["remote_instance_id"] = "not-an-id"
    payload["remote_aws_region"] = "US-EAST-1"

    problems = answers.validate(payload)

    assert len(problems) == 3
    assert any("git_provider_url" in p for p in problems)
    assert any("remote_instance_id" in p for p in problems)
    assert any("remote_aws_region" in p for p in problems)


def test_validate_reports_unknown_answer_keys() -> None:
    answers = _import_answers()
    payload = _valid_local_payload()
    payload["totally_unknown_field"] = "value"

    problems = answers.validate(payload)

    assert any("totally_unknown_field" in p for p in problems)


def test_present_but_not_required_answer_is_still_validated() -> None:
    """AC-FUNC-006: host_proxy is False, so host_proxy_url is not required,
    but a stale value left over from an earlier run must still fail.
    """
    answers = _import_answers()
    payload = _valid_local_payload()
    payload["host_proxy_url"] = "not-a-valid-proxy-url"

    problems = answers.validate(payload)

    assert any("host_proxy_url" in p for p in problems)


def test_validate_reports_missing_always_required_field() -> None:
    answers = _import_answers()
    payload = _valid_local_payload()
    del payload["developer_name"]

    problems = answers.validate(payload)

    assert any("developer_name" in p for p in problems)


def test_validate_reports_missing_required_field_with_branch_reason() -> None:
    answers = _import_answers()
    payload = _valid_remote_payload()
    del payload["remote_instance_id"]

    problems = answers.validate(payload)

    assert any("remote_instance_id" in p and "remote" in p for p in problems)


def test_validate_reports_missing_aws_profiles_when_aws_enabled() -> None:
    answers = _import_answers()
    payload = _valid_local_payload(aws_config_enabled=True)
    del payload["aws_profiles"]

    problems = answers.validate(payload)

    assert any("aws_profiles" in p for p in problems)


def test_validate_reports_missing_host_proxy_url_when_proxy_enabled() -> None:
    answers = _import_answers()
    payload = _valid_local_payload(host_proxy=True)
    del payload["host_proxy_url"]

    problems = answers.validate(payload)

    assert any("host_proxy_url" in p for p in problems)


# ---------------------------------------------------------------------------
# AC-FUNC-010: ensure_valid applies defaults, then raises or returns.
# ---------------------------------------------------------------------------


def test_apply_defaults_only_fills_absent_fields() -> None:
    answers = _import_answers()
    payload = {"default_git_branch": "develop"}

    filled = answers.apply_defaults(payload)

    assert filled["default_git_branch"] == "develop"
    assert filled["git_provider_url"] == "github.com"
    assert filled["remote_aws_region"] == "us-east-1"
    assert filled["remote_aws_profile"] == "default"


def test_ensure_valid_applies_defaults_and_returns_filled_answers() -> None:
    answers = _import_answers()
    payload = _valid_local_payload()
    del payload["git_provider_url"]
    del payload["default_git_branch"]

    filled = answers.ensure_valid(payload)

    assert filled["git_provider_url"] == "github.com"
    assert filled["default_git_branch"] == "main"


def test_ensure_valid_raises_answer_error_naming_every_problem() -> None:
    answers = _import_answers()
    payload = _valid_local_payload()
    payload["git_user_email"] = "not-an-email"
    payload["template_name"] = ""

    with pytest.raises(answers.AnswerError) as excinfo:
        answers.ensure_valid(payload)

    message = str(excinfo.value)
    assert "git_user_email" in message
    assert "template_name" in message


# ---------------------------------------------------------------------------
# AC-CYCLE-001: end-to-end, a complete remote payload, then three corruptions.
# ---------------------------------------------------------------------------


def test_end_to_end_cycle_remote_payload_then_three_corruptions() -> None:
    answers = _import_answers()
    payload = _valid_remote_payload()
    del payload["remote_aws_region"]
    del payload["remote_aws_profile"]

    filled = answers.ensure_valid(payload)

    assert filled["backend"] == "remote"
    assert filled["remote_aws_region"] == "us-east-1"
    assert filled["remote_aws_profile"] == "default"

    corrupted = dict(filled)
    corrupted["git_provider_url"] = "https://github.com"
    corrupted["remote_instance_id"] = "not-an-id"
    corrupted["git_user_email"] = "not-an-email"

    problems = answers.validate(corrupted)
    assert len(problems) == 3

    with pytest.raises(answers.AnswerError) as excinfo:
        answers.ensure_valid(corrupted)

    message = str(excinfo.value)
    assert "git_provider_url" in message
    assert "remote_instance_id" in message
    assert "git_user_email" in message
