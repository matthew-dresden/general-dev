"""The two EC2 protection flags are inputs, not literals, and default to true.

`provider/aws/modules/compute/main.tf` once set `disable_api_termination` and
`disable_api_stop` to a hardcoded `true`. Both settings are correct for a
long-lived developer box, and together they made the module's own instances
undestroyable by the tool that created them: `terragrunt destroy` gets
`OperationNotPermitted` from `TerminateInstances`, the internet gateway then
fails to detach because the surviving instance still holds a mapped public
address, and clearing it takes two out-of-band
`aws ec2 modify-instance-attribute` calls that no Terragrunt configuration can
express. That was observed twice against real infrastructure before the flags
were exposed.

This module pins three properties, each of which independently prevents the
regression:

- Both flags are declared as `bool` variables defaulting to `true`, in the
  compute submodule and in the root module, so an existing deployment that
  names neither keeps exactly the protection it has today.
- `compute/main.tf` assigns them from `var.*` rather than from a literal, so
  the value a deployment supplies actually reaches the resource.
- The root module forwards both to the submodule, so setting them on a
  deployment is not silently dropped one layer down.

Every assertion reads the real committed HCL. The parsers are additionally
exercised against synthetic `tmp_path` files carrying the old literal form and
a wrong default, proving each check fails when the property it pins is absent
rather than passing vacuously.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_COMPUTE_MAIN = Path("provider/aws/modules/compute/main.tf")
_COMPUTE_VARS = Path("provider/aws/modules/compute/variables.tf")
_ROOT_VARS = Path("provider/aws/variables.tf")
_ROOT_MODULE = Path("provider/aws/remote-ec2-instance.tf")

_FLAGS = ("disable_api_termination", "disable_api_stop")


def _read(relative: Path) -> str:
    absolute = _REPO_ROOT / relative
    assert absolute.is_file(), f"{relative} is missing from the repository"
    return absolute.read_text(encoding="utf-8")


def _declared_bool_default(text: str, name: str) -> str | None:
    """Return the `default` of variable *name* when it is declared `bool`.

    Returns None when the variable is absent, so a caller can tell "not
    declared" apart from "declared with the wrong default".
    """
    block = re.search(
        rf'variable\s+"{re.escape(name)}"\s*\{{(.*?)\n\}}',
        text,
        re.DOTALL,
    )
    if block is None:
        return None
    body = block.group(1)
    if not re.search(r"^\s*type\s*=\s*bool\s*$", body, re.MULTILINE):
        return None
    default = re.search(r"^\s*default\s*=\s*(\S+)\s*$", body, re.MULTILINE)
    return default.group(1) if default else None


def _resource_assignment(text: str, name: str) -> str | None:
    """Return the right-hand side `aws_instance.this` assigns to *name*."""
    resource = re.search(r'resource\s+"aws_instance"\s+"this"\s*\{(.*?)\n\}', text, re.DOTALL)
    if resource is None:
        return None
    match = re.search(rf"^\s*{re.escape(name)}\s*=\s*(\S+)\s*$", resource.group(1), re.MULTILINE)
    return match.group(1) if match else None


def _module_passthrough(text: str, name: str) -> str | None:
    """Return what the root module's `compute` block forwards for *name*."""
    block = re.search(r'module\s+"compute"\s*\{(.*?)\n\}', text, re.DOTALL)
    if block is None:
        return None
    match = re.search(rf"^\s*{re.escape(name)}\s*=\s*(\S+)\s*$", block.group(1), re.MULTILINE)
    return match.group(1) if match else None


@pytest.mark.parametrize("flag", _FLAGS)
def test_compute_submodule_declares_the_flag_as_a_bool_defaulting_true(flag: str) -> None:
    default = _declared_bool_default(_read(_COMPUTE_VARS), flag)
    assert default == "true", (
        f"{_COMPUTE_VARS} must declare var.{flag} as a bool defaulting to true so an "
        f"existing deployment that names it keeps today's protection; got {default!r}"
    )


@pytest.mark.parametrize("flag", _FLAGS)
def test_root_module_declares_the_flag_as_a_bool_defaulting_true(flag: str) -> None:
    default = _declared_bool_default(_read(_ROOT_VARS), flag)
    assert default == "true", (
        f"{_ROOT_VARS} must declare var.{flag} as a bool defaulting to true; got {default!r}"
    )


@pytest.mark.parametrize("flag", _FLAGS)
def test_instance_reads_the_flag_from_a_variable_rather_than_a_literal(flag: str) -> None:
    assigned = _resource_assignment(_read(_COMPUTE_MAIN), flag)
    assert assigned == f"var.{flag}", (
        f"aws_instance.this must set {flag} from var.{flag} so a deployment's value reaches "
        f"the resource; got {assigned!r}. A literal here is what made destroy impossible."
    )


@pytest.mark.parametrize("flag", _FLAGS)
def test_root_module_forwards_the_flag_to_the_compute_submodule(flag: str) -> None:
    forwarded = _module_passthrough(_read(_ROOT_MODULE), flag)
    assert forwarded == f"var.{flag}", (
        f"the compute module block must forward {flag} = var.{flag}, or a deployment setting it "
        f"is silently dropped one layer down; got {forwarded!r}"
    )


def test_literal_assignment_is_detected_as_such(tmp_path: Path) -> None:
    """The old, broken form fails the assignment check rather than passing."""
    hcl = tmp_path / "main.tf"
    hcl.write_text(
        'resource "aws_instance" "this" {\n'
        "  disable_api_termination = true\n"
        "  disable_api_stop        = true\n"
        "}\n",
        encoding="utf-8",
    )
    text = hcl.read_text(encoding="utf-8")
    for flag in _FLAGS:
        assert _resource_assignment(text, flag) == "true"
        assert _resource_assignment(text, flag) != f"var.{flag}"


def test_a_wrong_default_is_detected(tmp_path: Path) -> None:
    """A flag defaulting false fails the default check, so the safe default is pinned."""
    hcl = tmp_path / "variables.tf"
    hcl.write_text(
        'variable "disable_api_termination" {\n'
        "  type    = bool\n"
        "  default = false\n"
        "}\n",
        encoding="utf-8",
    )
    assert _declared_bool_default(hcl.read_text(encoding="utf-8"), "disable_api_termination") == "false"


def test_an_absent_variable_is_distinguishable_from_a_wrong_default(tmp_path: Path) -> None:
    hcl = tmp_path / "variables.tf"
    hcl.write_text('variable "unrelated" {\n  type = string\n}\n', encoding="utf-8")
    assert _declared_bool_default(hcl.read_text(encoding="utf-8"), "disable_api_stop") is None


def test_a_non_bool_declaration_does_not_satisfy_the_check(tmp_path: Path) -> None:
    """A string-typed flag is not acceptable: terraform would not coerce it safely."""
    hcl = tmp_path / "variables.tf"
    hcl.write_text(
        'variable "disable_api_stop" {\n  type    = string\n  default = "true"\n}\n',
        encoding="utf-8",
    )
    assert _declared_bool_default(hcl.read_text(encoding="utf-8"), "disable_api_stop") is None
