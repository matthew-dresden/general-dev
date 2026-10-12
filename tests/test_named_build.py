"""NAME= on build/up names a container at creation, not after the fact.

`make rename NAME=x` has always renamed an existing container; the operator
flow that left was naming one on the way in, so `make build NAME=x` and
`make up NAME=x` carry the name through creation. This suite pins the
container.sh wiring: the fail-fast validator (docker's name shape, plus the
collision refusal), the validation point in build and up (before any image
builds, not after), the application point (between the backend build and
the credential push, so the status a build ends on shows the final name),
and the two orderings the semantics depend on -- apply treats
already-named as success without running the collision check that would
fire on the container's own name, and rename checks same-name before the
validator for the same reason.

Static pins over the script text, the same approach
`tests/test_container_verify.py` takes for the behavior that lives in
container.sh: no docker in this file (AC-TEST-004).
"""

from __future__ import annotations

from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CONTAINER_SH = _REPO_ROOT / ".devcontainer" / "remote-docker" / "container.sh"


@pytest.fixture(scope="module")
def container_text() -> str:
    return _CONTAINER_SH.read_text(encoding="utf-8")


def _function_body(name: str, text: str) -> str:
    start = text.index(f"{name}() {{")
    end = text.index("\n}\n", start)
    return text[start:end]


def test_the_validator_enforces_dockers_name_shape(container_text: str) -> None:
    """Outside [a-zA-Z0-9][a-zA-Z0-9_.-]* is refused, stating the rule."""
    body = _function_body("rdc_validate_requested_name", container_text)
    assert "tr -cd 'a-zA-Z0-9_.-'" in body, (
        "the shape check must strip every allowed character and compare, so any "
        "leftover character is a refusal"
    )
    assert "not a name docker accepts" in body, "the refusal names what docker accepts"


def test_the_validator_refuses_a_stolen_name(container_text: str) -> None:
    """A name another container answers to is refused with the listing remedy."""
    body = _function_body("rdc_validate_requested_name", container_text)
    assert 'rd_docker inspect "$NAME"' in body
    assert "already exists on this engine" in body
    assert "make rename NAME=<other>" in body


def test_build_validates_before_anything_expensive(container_text: str) -> None:
    """The NAME check is instant; it runs before the build it would invalidate."""
    build = _function_body("rdc_build", container_text)
    prereqs_at = build.index("rdc_build_prereqs")
    validate_at = build.index("rdc_validate_requested_name")
    guard_at = build.index("already exists on this engine")
    assert prereqs_at < validate_at < guard_at, (
        "validate NAME after the prereqs but before the existing-container guard: "
        "an invalid NAME must fail before a guard message or an image build"
    )


def test_build_applies_the_name_before_pushing_credentials(container_text: str) -> None:
    """The name lands between the backend build and the creds push."""
    build = _function_body("rdc_build", container_text)
    local_at = build.index("rdc_build_local")
    apply_at = build.index("rdc_apply_requested_name")
    creds_at = build.index("rdc_push_creds")
    assert local_at < apply_at < creds_at, (
        "the name must be applied after the container exists and before creds, so "
        "the status a build ends on shows the final name"
    )


def test_up_validates_and_names_on_both_branches(container_text: str) -> None:
    """up refuses a bad NAME early and names on the start branch; build names its own."""
    up = _function_body("rdc_up", container_text)
    require_at = up.index("rd_require_cmd docker")
    validate_at = up.index("rdc_validate_requested_name")
    apply_at = up.rindex("rdc_apply_requested_name")
    creds_at = up.rindex("rdc_push_creds")
    assert require_at < validate_at, "the NAME check belongs with the other early checks"
    assert apply_at < creds_at, "the name lands before the creds push on the start branch"


def test_apply_treats_already_named_as_success_without_a_collision_check(
    container_text: str,
) -> None:
    """Repeated 'make up NAME=x' is idempotent; the collision check must not fire on it."""
    apply_body = _function_body("rdc_apply_requested_name", container_text)
    named_at = apply_body.index("container is named")
    validate_at = apply_body.index("rdc_validate_requested_name")
    assert named_at < validate_at, (
        "the already-named early return must precede the validator, whose "
        "collision check would otherwise fire on the container's own name"
    )


def test_rename_checks_same_name_before_the_validator(container_text: str) -> None:
    """'make rename NAME=<current>' reports already-named, not a name collision."""
    rename = _function_body("rdc_rename", container_text)
    same_at = rename.index("already named")
    validate_at = rename.index("rdc_validate_requested_name")
    assert same_at < validate_at


def test_the_help_advertises_the_creation_time_name() -> None:
    """The OPTIONS row documents NAME= next to CONTAINER=."""
    text = (_REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    assert 'row "NAME=<x>"' in text, "the help must document NAME= on build/up"
    snapshot = (_REPO_ROOT / "tests" / "data" / "make-help.txt").read_text(encoding="utf-8")
    assert "Name the container build/up creates at creation" in snapshot
