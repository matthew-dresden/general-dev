"""State bucket name reproducibility.

The bucket name must be byte-identical across runs, given a fixed instance
name, region and suffix. The name template itself --
`tg-state-<instance-name>-<region>-<account-id>-<suffix>` -- and the
committed suffix both live in `remote-instances/root.hcl`; this module reads
both from that file through `devcontainer_config.state_bucket`'s own parser
-- the same functions production composes the name with -- rather than
restating either as a Python literal, so a test that hard-coded the expected
name would keep passing even if the file started composing something else,
and a parser that drifted from root.hcl's grammar would fail here too.

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

`ROOT_HCL_RELATIVE` and the skip/xfail/guarded-import detector are shared
with `tests/test_tool_version_floors.py` via `tests/conftest.py` rather
than declared twice; see that module's docstring for why.
"""

from __future__ import annotations

import random
import re
import uuid
from pathlib import Path

import pytest
from conftest import ROOT_HCL_RELATIVE, _assert_no_skip_guard, _synthetic_account_id
from devcontainer_config import state_bucket
from devcontainer_config.repo import find_root
from devcontainer_config.state_bucket import StateBucketError

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
    """`remote-instances/root.hcl` as the production parser reads it."""
    root = find_root(Path(__file__).resolve().parent)
    try:
        return state_bucket.root_hcl_path(root).read_text(encoding="utf-8")
    except OSError as exc:
        raise StateBucketError(f"{ROOT_HCL_RELATIVE} could not be read: {exc}") from exc


def _bucket_name(hcl_text: str) -> str:
    """The name production's own composer produces for this module's fixed components."""
    return state_bucket.compose_from_root_hcl(
        hcl_text, _FIXED_INSTANCE_NAME, _FIXED_REGION, _FIXED_ACCOUNT_ID
    )


def _without_suffix_declaration(hcl_text: str) -> str:
    """A copy of `hcl_text` with the `state_bucket_suffix` line removed entirely."""
    perturbed, count = re.subn(
        r'^\s*state_bucket_suffix\s*=\s*"[^"]*"\n', "", hcl_text, count=1, flags=re.MULTILINE
    )
    if count != 1:
        raise StateBucketError(
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
        raise StateBucketError(
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
    suffix = state_bucket.declared_suffix(hcl_text)
    ordered_components = (_FIXED_INSTANCE_NAME, _FIXED_REGION, _FIXED_ACCOUNT_ID, suffix)
    positions = [name.index(component) for component in ordered_components]
    assert positions == sorted(positions), (
        f"components are not embedded in the order {ROOT_HCL_RELATIVE}'s template declares: "
        f"{name!r}"
    )


def test_missing_committed_suffix_raises_naming_the_missing_declaration() -> None:
    """No committed suffix -> a specific error naming it, and no name produced."""
    perturbed_hcl_text = _without_suffix_declaration(_root_hcl_text())
    with pytest.raises(StateBucketError, match="state_bucket_suffix"):
        _bucket_name(perturbed_hcl_text)


def test_template_referencing_an_unsupplied_component_raises_naming_it() -> None:
    """Malformed-input case: the name template names an unsupplied component."""
    perturbed_hcl_text = _with_unknown_template_component(_root_hcl_text())
    with pytest.raises(StateBucketError, match="unknown_component"):
        _bucket_name(perturbed_hcl_text)


def test_no_skip_xfail_or_conditional_import_guards_this_module() -> None:
    """This module hides no failure behind a skip, xfail or guarded import."""
    _assert_no_skip_guard(Path(__file__))
