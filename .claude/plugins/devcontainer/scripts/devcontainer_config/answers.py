"""The interview schema and its validation (spec Section 5.1).

This file is the single source of truth for the devcontainer setup
interview: a skill asks the questions declared in `FIELDS` here and no
others, and `render` (a later module) consumes the same field names.
`tests/test_skill_lint.py`, added in E4, asserts that correspondence in both
directions -- every question a skill asks exists in `FIELDS`, and every
always-or-conditionally-required field in `FIELDS` is asked by some skill.
Adding a question to the interview means adding a `Field` below, never
editing a skill's prose and this module's validation separately: the two
would drift, and the correspondence test would then be the only thing
holding them together.

`FIELDS` declares sixteen fields: the fifteen rows of the spec Section 5.1
table (AC-FUNC-001). `remote_ssh_key_path` was removed with SSH itself and
omits but the section's own prose keeps -- spec Section 11.5 Phase 4 removes
it together with SSH, but Phase 1 (this phase) keeps the remote path working
unchanged, so it stays declared until Phase 4 lands (AC-FUNC-011).

Two rules apply across every field and are implemented once, not repeated
per field:

* `validate` accumulates every failing field into one list rather than
  raising on the first (spec Section 4.2.2); only `ensure_valid` raises, and
  it raises with every problem in the message.
* An answer containing an unreplaced `<placeholder>` fails regardless of
  whether it would otherwise satisfy its field's rule, because the committed
  `.example` files ship bracketed values (`shell.env.example` ships
  `DEVELOPER_NAME='<Your Name>'`) and a value copied without editing must
  never reach a rendered configuration file.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any, Final, TypeAlias


class AnswerError(ValueError):
    """Raised by `ensure_valid` when one or more answers fail validation.

    The message carries every problem `validate` found, not just the first
    (spec Section 4.2.2), so a caller that surfaces this exception's message
    to an operator lets them correct the whole interview in one pass.
    """


class Requiredness(Enum):
    """Which branch of the interview makes a field required.

    Each member's value doubles as the human-readable reason `validate`
    reports when a required field is absent, so the reason is declared once,
    on the member, rather than restated in a separate lookup table.
    """

    ALWAYS = "always"
    WHEN_REMOTE = "the backend is 'remote'"
    WHEN_AWS = "aws_config_enabled is true"
    WHEN_PROXY = "host_proxy is true"


FieldValidator: TypeAlias = Callable[[str, Any], list[str]]
"""A field's own validation rule: (field_or_path_name, value) -> problems.

Returns an empty list when `value` is valid, otherwise one or more messages,
each naming `field_or_path_name` so a caller can identify which answer (or,
for an `aws_profiles` entry, which sub-field of which entry) failed.
"""


@dataclass(frozen=True)
class Field:
    """One row of the interview schema (spec Section 5.1).

    `validator` is the field's rule, expressed exactly once here rather than
    restated in a skill's prose, in `render`, or in a test helper -- every
    consumer that needs to know whether a value is valid calls this same
    callable.
    """

    name: str
    required: Requiredness
    validator: FieldValidator
    default: Any = None
    notes: str | None = None


_PLACEHOLDER_PATTERN: Final = re.compile(r"<[^<>]*>")


def _placeholder_problems(name: str, value: Any) -> list[str]:
    """The shared placeholder check every string-typed validator runs first.

    Written once here so no validator restates the rule that an unreplaced
    `<...>` token fails regardless of anything else about the value.
    """
    if isinstance(value, str) and _PLACEHOLDER_PATTERN.search(value):
        return [f"'{name}' still contains an unreplaced placeholder: {value!r}"]
    return []


def _string_problems(name: str, value: Any) -> list[str]:
    """The shared type/emptiness gate every string-typed validator runs."""
    if not isinstance(value, str):
        return [f"'{name}' must be a string, got {type(value).__name__}"]
    if not value.strip():
        return [f"'{name}' must not be empty"]
    return []


def _string_rule(check: Callable[[str, str], list[str]]) -> FieldValidator:
    """Wrap a plain-string rule with the shared placeholder-then-type prelude.

    Every string-typed validator must run the placeholder check, then the
    type/emptiness check, before applying its own rule, and in that order.
    Writing the sequence once here means `_text_validator`, `_pattern_validator`,
    `_enum_validator` and `_branch_name_validator` each state only their own
    rule, never the shared prelude (rule: DRY). `check` receives `value`
    already known to be a non-empty, non-placeholder string.
    """

    def _validate(name: str, value: Any) -> list[str]:
        placeholder = _placeholder_problems(name, value)
        if placeholder:
            return placeholder
        problems = _string_problems(name, value)
        if problems:
            return problems
        return check(name, value)

    return _validate


def _no_extra_rule(_name: str, _value: str) -> list[str]:
    """No rule beyond the shared placeholder-then-type prelude."""
    return []


_text_validator: Final[FieldValidator] = _string_rule(_no_extra_rule)
"""Free text with no pattern beyond 'present, non-empty, not a placeholder'."""


def _bool_validator(name: str, value: Any) -> list[str]:
    if not isinstance(value, bool):
        return [f"'{name}' must be true or false, got {value!r}"]
    return []


def _pattern_validator(pattern: re.Pattern[str], description: str) -> FieldValidator:
    """Build a validator for a field whose rule is 'matches this regex'.

    Shared by every pattern-shaped field (bare host, instance id, region,
    proxy URL, profile name, email, and the `aws_profiles` sub-fields that
    reuse these same rules).
    """

    def _check(name: str, value: str) -> list[str]:
        if not pattern.fullmatch(value):
            return [f"'{name}' must be {description}, got {value!r}"]
        return []

    return _string_rule(_check)


def _enum_validator(choices: frozenset[str]) -> FieldValidator:
    """Build a validator for a field whose rule is 'one of these literals'."""
    ordered = ", ".join(sorted(choices))

    def _check(name: str, value: str) -> list[str]:
        if value not in choices:
            return [f"'{name}' must be one of: {ordered}, got {value!r}"]
        return []

    return _string_rule(_check)


_BRANCH_ALLOWED_PATTERN: Final = re.compile(r"^[A-Za-z0-9._/-]+$")


def _branch_name_check(name: str, value: str) -> list[str]:
    """A simplified but real subset of git's ref-name rules.

    Not a single regex: git ref names forbid several independent things
    (a leading dash, a trailing dot, `..` anywhere, `@{`, a `.lock` suffix)
    that no one pattern expresses cleanly, so this checks the allowed
    character set and then each structural rule explicitly.
    """
    invalid = (
        not _BRANCH_ALLOWED_PATTERN.fullmatch(value)
        or value[0] in "-/."
        or value[-1] in "/."
        or ".." in value
        or "//" in value
        or "@{" in value
        or value.endswith(".lock")
    )
    if invalid:
        return [f"'{name}' must be a valid git branch name, got {value!r}"]
    return []


_branch_name_validator: Final[FieldValidator] = _string_rule(_branch_name_check)


_BARE_HOST_PATTERN: Final = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)*$"
)
_INSTANCE_ID_PATTERN: Final = re.compile(r"^i-[0-9a-f]{8}(?:[0-9a-f]{9})?$")
_REGION_PATTERN: Final = re.compile(r"^[a-z]{2}(?:-gov)?-[a-z]+-[0-9]$")
_PROXY_URL_PATTERN: Final = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://[^\s/:]+:[0-9]+$")
_PROFILE_NAME_PATTERN: Final = re.compile(r"^[A-Za-z0-9_-]+$")
_EMAIL_PATTERN: Final = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
_SSO_URL_PATTERN: Final = re.compile(r"^https://[A-Za-z0-9.-]+(?::[0-9]+)?(?:/[^\s]*)?$")
_ACCOUNT_ID_PATTERN: Final = re.compile(r"^[0-9]{12}$")
_ROLE_NAME_PATTERN: Final = re.compile(r"^[A-Za-z0-9+=,.@_-]{1,64}$")

_bare_host_validator: Final[FieldValidator] = _pattern_validator(
    _BARE_HOST_PATTERN, "a bare hostname with no scheme and no path"
)
_instance_id_validator: Final[FieldValidator] = _pattern_validator(
    _INSTANCE_ID_PATTERN, "'i-' followed by exactly 8 or 17 hexadecimal characters"
)
_region_validator: Final[FieldValidator] = _pattern_validator(
    _REGION_PATTERN, "an AWS region, e.g. 'us-east-1'"
)
_proxy_url_validator: Final[FieldValidator] = _pattern_validator(
    _PROXY_URL_PATTERN, "'scheme://host:port' with an explicit port"
)
_profile_name_validator: Final[FieldValidator] = _pattern_validator(
    _PROFILE_NAME_PATTERN, "a profile name of letters, digits, hyphens and underscores"
)
_email_validator: Final[FieldValidator] = _pattern_validator(
    _EMAIL_PATTERN, "a valid email address"
)
_sso_url_validator: Final[FieldValidator] = _pattern_validator(
    _SSO_URL_PATTERN, "an 'https://' SSO start URL"
)
_account_id_validator: Final[FieldValidator] = _pattern_validator(
    _ACCOUNT_ID_PATTERN, "a 12-digit AWS account ID"
)
_role_name_validator: Final[FieldValidator] = _pattern_validator(
    _ROLE_NAME_PATTERN, "a valid IAM role or permission set name"
)
BACKENDS: Final[frozenset[str]] = frozenset({"local", "remote"})
"""The only two valid `backend` values (spec Section 1.1); public so a
consumer outside this module (e.g. a skill or `render`) can validate a
`backend` answer without re-declaring the literal set."""

_BACKEND_VALIDATOR: Final[FieldValidator] = _enum_validator(BACKENDS)

# Order matters: this is the seven sub-fields spec Section 5.1 requires each
# aws_profiles entry to carry, mirroring
# .devcontainer/aws-profile-map.json.example. Derived from the dict below
# (not restated as its own tuple literal) so the two can never drift apart.
_AWS_PROFILE_SUB_FIELD_VALIDATORS: Final[dict[str, FieldValidator]] = {
    "name": _profile_name_validator,
    "region": _region_validator,
    "sso_start_url": _sso_url_validator,
    "sso_region": _region_validator,
    "account_name": _text_validator,
    "account_id": _account_id_validator,
    "role_name": _role_name_validator,
}
AWS_PROFILE_SUB_FIELDS: Final[tuple[str, ...]] = tuple(_AWS_PROFILE_SUB_FIELD_VALIDATORS)


def _aws_profiles_validator(name: str, value: Any) -> list[str]:
    """`aws_profiles`: a non-empty list of entries, each with seven sub-fields.

    Every problem is reported with the entry's index (and its profile name,
    where duplicated) so an operator can find the offending entry in a list
    without counting by hand.
    """
    if not isinstance(value, list):
        return [f"'{name}' must be a non-empty list, got {type(value).__name__}"]
    if not value:
        return [f"'{name}' must not be empty"]

    problems: list[str] = []
    seen_at: dict[str, int] = {}
    for index, entry in enumerate(value):
        entry_label = f"{name}[{index}]"
        if not isinstance(entry, Mapping):
            problems.append(
                f"'{entry_label}' must be an object with the seven required sub-fields "
                f"{AWS_PROFILE_SUB_FIELDS}, got {type(entry).__name__}"
            )
            continue
        for sub_field in AWS_PROFILE_SUB_FIELDS:
            if sub_field not in entry:
                problems.append(f"'{entry_label}' is missing required sub-field '{sub_field}'")
                continue
            sub_validator = _AWS_PROFILE_SUB_FIELD_VALIDATORS[sub_field]
            problems.extend(sub_validator(f"{entry_label}.{sub_field}", entry[sub_field]))

        profile_name = entry.get("name")
        if (
            isinstance(profile_name, str)
            and profile_name
            and not _PLACEHOLDER_PATTERN.search(profile_name)
        ):
            if profile_name in seen_at:
                problems.append(
                    f"'{entry_label}' (name={profile_name!r}) duplicates the profile name "
                    f"used by '{name}[{seen_at[profile_name]}]'"
                )
            else:
                seen_at[profile_name] = index

    return problems


FIELDS: Final[tuple[Field, ...]] = (
    Field(
        name="backend",
        required=Requiredness.ALWAYS,
        validator=_BACKEND_VALIDATOR,
        notes=(
            "Drives which conditional interview questions are asked and "
            "which values 'render' writes into the three config files "
            "(spec Section 5.1); the engine actually used at build time is "
            "determined solely by the active docker context, never by this "
            "answer (spec Section 1.1)."
        ),
    ),
    Field(name="developer_name", required=Requiredness.ALWAYS, validator=_text_validator),
    Field(name="git_user", required=Requiredness.ALWAYS, validator=_text_validator),
    Field(name="git_user_email", required=Requiredness.ALWAYS, validator=_email_validator),
    Field(
        name="git_provider_url",
        required=Requiredness.ALWAYS,
        validator=_bare_host_validator,
        default="github.com",
        notes=(
            "Bare host only, no scheme and no path: this value feeds the git "
            "credential helper directly, and the helper matches on host, not "
            "URL, so a leading 'https://' silently breaks credential lookup "
            "instead of raising an error."
        ),
    ),
    Field(
        name="default_git_branch",
        required=Requiredness.ALWAYS,
        validator=_branch_name_validator,
        default="main",
        notes=(
            "Written into shell.env and devcontainer-environment-variables.json "
            "for the developer's own reference; the remote clone always uses "
            "the currently checked-out branch instead (spec Section 1.3), "
            "never this value. The only enforced consumer is a presence "
            "check: post-create setup fails the build immediately if this "
            "is unset, which is why it is validated eagerly rather than "
            "left unchecked."
        ),
    ),
    Field(name="template_name", required=Requiredness.ALWAYS, validator=_text_validator),
    Field(name="aws_config_enabled", required=Requiredness.ALWAYS, validator=_bool_validator),
    Field(name="local_docker_context", required=Requiredness.ALWAYS, validator=_text_validator),
    Field(name="host_proxy", required=Requiredness.ALWAYS, validator=_bool_validator),
    Field(
        name="aws_profiles",
        required=Requiredness.WHEN_AWS,
        validator=_aws_profiles_validator,
        notes=(
            "A non-empty list of profile entries, each carrying the seven "
            "sub-fields mirroring '.devcontainer/aws-profile-map.json.example' "
            "(name, region, sso_start_url, sso_region, account_name, "
            "account_id, role_name); 'render' folds this list into that "
            "file's name-keyed object shape."
        ),
    ),
    Field(
        name="host_proxy_url",
        required=Requiredness.WHEN_PROXY,
        validator=_proxy_url_validator,
        notes="Requires an explicit port; 'scheme://host' with no port is rejected.",
    ),
    Field(
        name="remote_instance_id",
        required=Requiredness.WHEN_REMOTE,
        validator=_instance_id_validator,
        notes=(
            "AWS EC2 instance IDs are 'i-' plus exactly 8 (legacy) or 17 (current) hex characters."
        ),
    ),
    Field(
        name="remote_aws_region",
        required=Requiredness.WHEN_REMOTE,
        validator=_region_validator,
        default="us-east-1",
    ),
    Field(
        name="remote_aws_profile",
        required=Requiredness.WHEN_REMOTE,
        validator=_profile_name_validator,
        default="default",
    ),
)

FIELDS_BY_NAME: Final[dict[str, Field]] = {f.name: f for f in FIELDS}

_BRANCH_TOGGLES: Final[dict[Requiredness, Callable[[Mapping[str, Any]], bool]]] = {
    Requiredness.WHEN_REMOTE: lambda a: a.get("backend") == "remote",
    Requiredness.WHEN_AWS: lambda a: a.get("aws_config_enabled") is True,
    Requiredness.WHEN_PROXY: lambda a: a.get("host_proxy") is True,
}


def required_fields(answers: Mapping[str, Any]) -> tuple[str, ...]:
    """Field names required for this answer set, in `FIELDS` declaration order.

    The always-required fields are always included; the remote, AWS and
    proxy fields are included only when `answers` turns on the branch that
    needs them (spec Section 5.1).
    """
    names: list[str] = []
    for f in FIELDS:
        if f.required is Requiredness.ALWAYS or _BRANCH_TOGGLES[f.required](answers):
            names.append(f.name)
    return tuple(names)


def validate(answers: Mapping[str, Any]) -> list[str]:
    """Every problem in `answers`, never just the first (spec Section 4.2.2).

    Three things are checked, all accumulated into one list: an answer key
    that is not a declared field, a required field that is absent for the
    branch selected, and every present field's own validation rule --
    including a present-but-not-required field, so a stale value left over
    from an earlier run cannot reach a rendered file (AC-FUNC-006).
    """
    problems: list[str] = []

    for key in answers:
        if key not in FIELDS_BY_NAME:
            problems.append(f"'{key}' is not a declared field")

    required = set(required_fields(answers))
    for f in FIELDS:
        if f.name not in answers or answers[f.name] is None:
            if f.name in required:
                if f.required is Requiredness.ALWAYS:
                    problems.append(f"'{f.name}' is required but was not provided")
                else:
                    problems.append(
                        f"'{f.name}' is required because {f.required.value} but was not provided"
                    )
            continue
        problems.extend(f.validator(f.name, answers[f.name]))

    return problems


def apply_defaults(answers: Mapping[str, Any]) -> dict[str, Any]:
    """A copy of `answers` with every declared default filled in where absent."""
    filled = dict(answers)
    for f in FIELDS:
        if f.default is not None and f.name not in filled:
            filled[f.name] = f.default
    return filled


def ensure_valid(answers: Mapping[str, Any]) -> dict[str, Any]:
    """Defaults applied, then validated; raises `AnswerError` or returns the result.

    Defaults are applied before validation so a caller never has to supply
    `git_provider_url` or `default_git_branch` just to pass; validation then
    runs against the filled answers, and either every problem is raised
    together or the filled answers are returned.
    """
    filled = apply_defaults(answers)
    problems = validate(filled)
    if problems:
        raise AnswerError("invalid interview answers:\n" + "\n".join(f"- {p}" for p in problems))
    return filled
