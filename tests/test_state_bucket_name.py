"""State bucket name reproducibility.

The bucket name must be byte-identical across runs, given a fixed instance
name, region and suffix. The name template itself --
`tg-state-<instance-name>-<region>-<account-id>-<suffix>` -- and the
committed suffix both live in `remote-instances/root.hcl`; this module reads
both from that file rather than restating either as a Python literal, so a
test that hard-coded the expected name would keep passing even if the file
started composing something else.

The instance name, region and account id are the three components
`remote-instances/root.hcl` resolves at Terragrunt runtime
(`basename(path_relative_to_include())`, `get_env("REMOTE_AWS_REGION")`, and
`get_aws_account_id()`); none of the three is declared anywhere in this
repository as a value to read, and re-deriving the account id or region here
would need an AWS STS call or a required environment variable, making this
module non-hermetic and environment-dependent. So this module builds all
three instead of typing any of them as a literal:

- `_FIXED_ACCOUNT_ID` is generated at runtime by
  `tests/conftest.py::_synthetic_account_id`, the same twelve-digit-shaped
  generator `tests/test_answers.py` already uses so no AWS-account-shaped
  digit run ever appears as source text (`lint-secrets` keys on exactly that
  shape).
- `_FIXED_INSTANCE_NAME` is generated at runtime, in the instance-name shape
  `devcontainer_config.instances.validate_name` accepts, for the identical
  "generated, not typed" reason.
- `_FIXED_REGION` is generated at runtime by `_synthetic_region`, from the
  same partition/direction vocabulary real AWS region names use, for the
  same reason.

Two computations of the name are asserted equal from two independent reads
of `remote-instances/root.hcl`, not from one parse reused twice, so a
suffix or template read that were non-deterministic (e.g. accidentally
generated instead of read) would be caught by this test instead of hidden
behind Python's own referential equality.

`ROOT_HCL_RELATIVE`, `_repo_root`, `_read_repo_file` and the
skip/xfail/guarded-import detector are shared with
`tests/test_tool_version_floors.py` via `tests/conftest.py` rather than
declared twice; see that module's docstring for why.
"""

from __future__ import annotations

import random
import re
import uuid
from pathlib import Path

import pytest
from conftest import (
    ROOT_HCL_RELATIVE,
    _assert_no_skip_guard,
    _read_repo_file,
    _synthetic_account_id,
)

_INTERPOLATION_TOKEN = re.compile(r"\$\{local\.([A-Za-z0-9_]+)\}")

# AWS region names are `<partition>-<direction>-<digit>`
# (`devcontainer_config.answers._REGION_PATTERN`:
# `^[a-z]{2}(?:-gov)?-[a-z]+-[0-9]$`). Building a region-shaped value from
# this small, generic vocabulary rather than committing a real region string
# such as "us-east-1" keeps `_synthetic_region` from ever restating a
# specific, identifiable region as a literal, mirroring
# `tests/conftest.py::_synthetic_account_id`'s "generated, not typed"
# approach for the same reason.
_REGION_PARTITIONS = ("us", "eu", "ap", "ca", "sa", "af", "me")
_REGION_DIRECTIONS = ("east", "west", "north", "south", "central")


class BucketNameError(AssertionError):
    """The name template, the committed suffix, or a substitution could not be resolved.

    Every raise site below names the file and the value it could not make
    sense of: the Terragrunt root configuration declares no suffix, or the
    template references an unsupplied component, and the test fails naming
    the file and the missing declaration.
    """


def _synthetic_region() -> str:
    """An AWS-region-shaped value (`<partition>-<direction>-<digit>`), generated at runtime.

    See the module docstring for why this, `_FIXED_REGION`'s source, is
    generated rather than a literal such as `"us-east-1"`.
    """
    partition = random.choice(_REGION_PARTITIONS)
    direction = random.choice(_REGION_DIRECTIONS)
    digit = random.randint(1, 9)
    return f"{partition}-{direction}-{digit}"


def _synthetic_instance_name() -> str:
    """An instance-name-shaped value, generated at runtime.

    `devcontainer_config.instances.validate_name` accepts letters, digits,
    hyphens and underscores; this generator composes from that vocabulary at
    runtime so no instance-name-shaped literal is stored in this file.
    """
    return f"inst-{uuid.uuid4().hex[:8]}"


# Arbitrary, fixed stand-ins for the three components root.hcl resolves at
# Terragrunt runtime -- see the module docstring for why these are generated
# rather than literals. Computed once, at import, so both computations in
# `test_bucket_name_is_byte_identical_across_two_computations` use the
# identical "fixed instance name, region and account" this property needs.
_FIXED_ACCOUNT_ID = _synthetic_account_id()
_FIXED_REGION = _synthetic_region()
_FIXED_INSTANCE_NAME = _synthetic_instance_name()


def _root_hcl_text() -> str:
    return _read_repo_file(ROOT_HCL_RELATIVE, error_cls=BucketNameError)


def _declared_template(hcl_text: str) -> str:
    """The raw `state_bucket_name` interpolation string committed in root.hcl."""
    match = re.search(r'^\s*state_bucket_name\s*=\s*"([^"]*)"', hcl_text, re.MULTILINE)
    if match is None:
        raise BucketNameError(f"no state_bucket_name declaration found in {ROOT_HCL_RELATIVE}")
    return match.group(1)


def _declared_suffix(hcl_text: str) -> str:
    """The committed `state_bucket_suffix` value.

    Raises naming the file and the missing declaration when no suffix is
    committed, since inventing a replacement here would silently point the
    composed name at a different bucket than the one Terragrunt's own
    bootstrap would find.
    """
    match = re.search(r'^\s*state_bucket_suffix\s*=\s*"([^"]*)"', hcl_text, re.MULTILINE)
    if match is None or not match.group(1):
        raise BucketNameError(
            f"no committed state_bucket_suffix found in {ROOT_HCL_RELATIVE}; a missing suffix "
            "means a fresh bootstrap would mint a new bucket instead of finding the existing one"
        )
    return match.group(1)


def _compose(template: str, values: dict[str, str]) -> str:
    """`template`'s `${local.NAME}` tokens substituted from `values`.

    Raises naming the unresolved `local.NAME` reference and the file it
    came from when the template names a component this caller did not
    supply, rather than leaving the literal `${local...}` token embedded in
    the returned string.
    """

    def _substitute(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in values:
            raise BucketNameError(
                f"{ROOT_HCL_RELATIVE}'s state_bucket_name template references local.{name}, "
                f"which is not one of the composed components {sorted(values)}"
            )
        return values[name]

    return _INTERPOLATION_TOKEN.sub(_substitute, template)


def _bucket_name(hcl_text: str) -> str:
    template = _declared_template(hcl_text)
    suffix = _declared_suffix(hcl_text)
    values = {
        "instance_name": _FIXED_INSTANCE_NAME,
        "aws_region": _FIXED_REGION,
        "account_id": _FIXED_ACCOUNT_ID,
        "state_bucket_suffix": suffix,
    }
    return _compose(template, values)


def _without_suffix_declaration(hcl_text: str) -> str:
    """A copy of `hcl_text` with the `state_bucket_suffix` line removed entirely."""
    perturbed, count = re.subn(
        r'^\s*state_bucket_suffix\s*=\s*"[^"]*"\n', "", hcl_text, count=1, flags=re.MULTILINE
    )
    if count != 1:
        raise BucketNameError(
            f"could not remove the state_bucket_suffix declaration from a copy of "
            f"{ROOT_HCL_RELATIVE} to build the missing-suffix fixture"
        )
    return perturbed


def _with_unknown_template_component(hcl_text: str) -> str:
    """A copy of `hcl_text` whose `state_bucket_name` template names an unsupplied component."""
    perturbed, count = re.subn(
        r"\$\{local\.instance_name\}", "${local.unknown_component}", hcl_text, count=1
    )
    if count != 1:
        raise BucketNameError(
            f"could not perturb the state_bucket_name template's local.instance_name reference "
            f"in a copy of {ROOT_HCL_RELATIVE} to build the missing-component fixture"
        )
    return perturbed


def test_bucket_name_is_byte_identical_across_two_computations() -> None:
    """Same fixed instance name, region, account and suffix -> same name, twice."""
    first = _bucket_name(_root_hcl_text())
    second = _bucket_name(_root_hcl_text())
    assert first == second
    assert first != ""


def test_bucket_name_embeds_every_component_in_the_template_order() -> None:
    """Proves substitution ran, rather than the template happening to already equal itself."""
    hcl_text = _root_hcl_text()
    name = _bucket_name(hcl_text)
    suffix = _declared_suffix(hcl_text)
    ordered_components = (_FIXED_INSTANCE_NAME, _FIXED_REGION, _FIXED_ACCOUNT_ID, suffix)
    positions = [name.index(component) for component in ordered_components]
    assert positions == sorted(positions), (
        f"components are not embedded in the order {ROOT_HCL_RELATIVE}'s template declares: "
        f"{name!r}"
    )


def test_missing_committed_suffix_raises_naming_the_missing_declaration() -> None:
    """No committed suffix -> a specific error naming it, and no name produced."""
    perturbed_hcl_text = _without_suffix_declaration(_root_hcl_text())
    with pytest.raises(BucketNameError, match="state_bucket_suffix"):
        _bucket_name(perturbed_hcl_text)


def test_template_referencing_an_unsupplied_component_raises_naming_it() -> None:
    """Malformed-input case: the name template names an unsupplied component."""
    perturbed_hcl_text = _with_unknown_template_component(_root_hcl_text())
    with pytest.raises(BucketNameError, match="unknown_component"):
        _bucket_name(perturbed_hcl_text)


def test_no_skip_xfail_or_conditional_import_guards_this_module() -> None:
    """This module hides no failure behind a skip, xfail or guarded import."""
    _assert_no_skip_guard(Path(__file__))
