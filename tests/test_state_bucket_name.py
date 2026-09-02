"""State bucket name reproducibility (spec Section 5.7, AC-5.1).

AC-5.1 requires that, given a fixed account, region and suffix, the computed
bucket name is byte-identical across runs. The name template itself --
`tg-state-<account-id>-<region>-<repo-slug>-<suffix>` -- and the committed
suffix both live in `remote-instances/root.hcl`; this module reads both from
that file rather than restating either as a Python literal, so a test that
hard-coded the expected name would keep passing even if the file started
composing something else, which is exactly the drift AC-5.1 exists to catch.

The account id, region and repository slug are the three components
`remote-instances/root.hcl` resolves at Terragrunt runtime
(`get_aws_account_id()`, `get_env("REMOTE_AWS_REGION")`, and a value derived
from `git config --get remote.origin.url`); none of the three is declared
anywhere in this repository as a value to read, and re-deriving the account
id or region here would need an AWS STS call or a required environment
variable, making this module non-hermetic and environment-dependent and
breaking AC-TEST-008's "no network" contract on every machine that has not
exported `REMOTE_AWS_REGION`. So this module builds all three the way
AC-FUNC-001 requires instead of typing any of them as a literal:

- `_FIXED_ACCOUNT_ID` is generated at runtime by
  `tests/conftest.py::_synthetic_account_id`, the same twelve-digit-shaped
  generator `tests/test_answers.py` already uses so no AWS-account-shaped
  digit run ever appears as source text (`lint-secrets` keys on exactly that
  shape).
- `_FIXED_REPO_SLUG` is derived from this checkout's own git remote via
  `_repo_slug_from_git_remote`, applying the identical `basename(...)` /
  `trimsuffix(..., ".git")` transform root.hcl's `repo_slug` local applies,
  so it is read from the repository rather than typed.
- `_FIXED_REGION` is generated at runtime by `_synthetic_region`, from the
  same partition/direction vocabulary real AWS region names use, for the
  identical "generated, not typed" reason `_synthetic_account_id` exists.

AC-5.1 itself asks for the property "given a fixed account, region and
suffix", which is exactly what a module-level constant, computed once, gives
this suite: all three stay fixed for the whole test run, so the *composition*
and the *committed suffix* -- the two values genuinely declared in the
repository -- are what gets exercised, never the account/region/slug values
themselves.

Two computations of the name are asserted equal from two independent reads
of `remote-instances/root.hcl`, not from one parse reused twice, so a
suffix or template read that were non-deterministic (e.g. accidentally
generated instead of read) would be caught by this test instead of hidden
behind Python's own referential equality.

`ROOT_HCL_RELATIVE`, `_repo_root`, `_read_repo_file`, the ast-based
skip/xfail/guarded-import detector and the self-check test that uses it are
shared with `tests/test_tool_version_floors.py` via `tests/conftest.py`
rather than declared twice; see that module's docstring for why.
"""

from __future__ import annotations

import random
import re
import subprocess
from pathlib import Path

import pytest
from conftest import (
    ROOT_HCL_RELATIVE,
    _assert_no_skip_guard,
    _read_repo_file,
    _repo_root,
    _synthetic_account_id,
)
from devcontainer_config.hostprobe import read_positive_seconds

_INTERPOLATION_TOKEN = re.compile(r"\$\{local\.([A-Za-z0-9_]+)\}")

# AWS region names are `<partition>-<direction>-<digit>`
# (`devcontainer_config.answers._REGION_PATTERN`:
# `^[a-z]{2}(?:-gov)?-[a-z]+-[0-9]$`). Building a region-shaped value from
# this small, generic vocabulary rather than committing a real region string
# such as "us-east-1" keeps `_synthetic_region` from ever restating a
# specific, identifiable region as a literal, mirroring
# `tests/conftest.py::_synthetic_account_id`'s "generated, not typed"
# approach for the same AC-FUNC-001 reason.
_REGION_PARTITIONS = ("us", "eu", "ap", "ca", "sa", "af", "me")
_REGION_DIRECTIONS = ("east", "west", "north", "south", "central")

# Bounds how long `_repo_slug_from_git_remote` waits on `git config --get
# remote.origin.url`, a local, network-free read of this checkout's own
# `.git/config` (spec Section 10.1's hermetic-suite contract). Read from the
# environment with a documented default via
# `devcontainer_config.hostprobe.read_positive_seconds`, the same fail-fast
# reader `tests/test_tool_version_floors.py` uses for its own subprocess
# timeout, rather than a bare literal at the `subprocess.run` call site
# (CLAUDE.md: "timeout values must be configurable via environment
# variables").
_GIT_REMOTE_TIMEOUT_ENV_VAR = "REPO_SLUG_GIT_TIMEOUT_SECONDS"
_GIT_REMOTE_TIMEOUT_DEFAULT_SECONDS = 10.0


class BucketNameError(AssertionError):
    """The name template, the committed suffix, or a substitution could not be resolved.

    Every raise site below names the file and the value it could not make
    sense of, matching this work unit's Error Handling Contract: "the
    Terragrunt root configuration declares no suffix... the test fails
    naming the file and the missing declaration".
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


def _repo_slug_from_git_remote() -> str:
    """The `repo_slug` root.hcl's own `locals` block derives, read the same way root.hcl reads it.

    Mirrors `remote-instances/root.hcl`'s `repo_slug` local exactly: `git
    config --get remote.origin.url`, then split on the last `/` (which lands
    after the org/user segment for both the HTTPS form
    `https://host/org/repo.git` and the SSH form `git@host:org/repo.git`,
    per that local's own comment), then strip a trailing `.git`. Reads this
    checkout's own git remote rather than a typed literal, so `_FIXED_REPO_SLUG`
    is read from the repository instead of restated as an AC-FUNC-001
    literal.
    """
    timeout = read_positive_seconds(
        _GIT_REMOTE_TIMEOUT_ENV_VAR, _GIT_REMOTE_TIMEOUT_DEFAULT_SECONDS
    )
    result = subprocess.run(
        ["git", "config", "--get", "remote.origin.url"],
        cwd=_repo_root(),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    remote_url = result.stdout.strip()
    if result.returncode != 0 or not remote_url:
        raise BucketNameError(
            "git config --get remote.origin.url produced no remote URL in this checkout; "
            "root.hcl's repo_slug local cannot be derived without one"
        )
    last_segment = remote_url.rsplit("/", 1)[-1]
    if not last_segment:
        raise BucketNameError(
            f"git config --get remote.origin.url returned {remote_url!r}, which has no "
            "final path segment to derive root.hcl's repo_slug local from"
        )
    return last_segment.removesuffix(".git")


# Arbitrary, fixed stand-ins for the three components root.hcl resolves at
# Terragrunt runtime -- see the module docstring for why these are
# generated/derived rather than literals. Computed once, at import, so both
# computations in `test_bucket_name_is_byte_identical_across_two_computations`
# use the identical "fixed account, region [and slug]" AC-5.1 describes.
_FIXED_ACCOUNT_ID = _synthetic_account_id()
_FIXED_REGION = _synthetic_region()
_FIXED_REPO_SLUG = _repo_slug_from_git_remote()


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

    AC-TEST-005: raises naming the file and the missing declaration when no
    suffix is committed, since inventing a replacement here would silently
    point the composed name at a different bucket than the one Terragrunt's
    own bootstrap would find.
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
    the returned string -- the "name template is missing a component"
    malformed-input case this unit's Approach requires.
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
        "account_id": _FIXED_ACCOUNT_ID,
        "aws_region": _FIXED_REGION,
        "repo_slug": _FIXED_REPO_SLUG,
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
        r"\$\{local\.repo_slug\}", "${local.unknown_component}", hcl_text, count=1
    )
    if count != 1:
        raise BucketNameError(
            f"could not perturb the state_bucket_name template's local.repo_slug reference "
            f"in a copy of {ROOT_HCL_RELATIVE} to build the missing-component fixture"
        )
    return perturbed


def test_bucket_name_is_byte_identical_across_two_computations() -> None:
    """AC-5.1 / AC-TEST-004: same fixed account, region, slug and suffix -> same name, twice."""
    first = _bucket_name(_root_hcl_text())
    second = _bucket_name(_root_hcl_text())
    assert first == second
    assert first != ""


def test_bucket_name_embeds_every_component_in_the_template_order() -> None:
    """Proves substitution ran, rather than the template happening to already equal itself."""
    hcl_text = _root_hcl_text()
    name = _bucket_name(hcl_text)
    suffix = _declared_suffix(hcl_text)
    ordered_components = (_FIXED_ACCOUNT_ID, _FIXED_REGION, _FIXED_REPO_SLUG, suffix)
    positions = [name.index(component) for component in ordered_components]
    assert positions == sorted(positions), (
        f"components are not embedded in the order {ROOT_HCL_RELATIVE}'s template declares: "
        f"{name!r}"
    )


def test_missing_committed_suffix_raises_naming_the_missing_declaration() -> None:
    """AC-TEST-005: no committed suffix -> a specific error naming it, and no name produced."""
    perturbed_hcl_text = _without_suffix_declaration(_root_hcl_text())
    with pytest.raises(BucketNameError, match="state_bucket_suffix"):
        _bucket_name(perturbed_hcl_text)


def test_template_referencing_an_unsupplied_component_raises_naming_it() -> None:
    """Malformed-input case (Approach step 5): the name template names an unsupplied component."""
    perturbed_hcl_text = _with_unknown_template_component(_root_hcl_text())
    with pytest.raises(BucketNameError, match="unknown_component"):
        _bucket_name(perturbed_hcl_text)


def test_no_skip_xfail_or_conditional_import_guards_this_module() -> None:
    """AC-TEST-007: this module hides no failure behind a skip, xfail or guarded import."""
    _assert_no_skip_guard(Path(__file__))
