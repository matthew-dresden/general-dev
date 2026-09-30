"""The instance can exercise the Parameter Store grant its role already carries.

`provider/aws/modules/security/main.tf` grants the instance role
`ssm:GetParameter` on `/devcontainer/<instance>/*`, which is where
`devcontainer_config.certs.publish` writes the daemon's TLS material and where
the create-time secret bootstrap reads from. A grant with no client on the host
is unusable, and Ubuntu 24.04 publishes no `awscli` apt candidate, so the
rendered user data installs the CLI from the archive Amazon publishes.

These assertions exist because the failure mode is silent and late: an instance
missing the CLI provisions cleanly, reports `/etc/general-dev-provisioned`, and
only fails when something first tries to read a parameter -- by which point the
symptom (`aws: not found`, inside a command run over SSM) names neither the
template nor the grant it was supposed to exercise. That is exactly how this
gap was found.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_COMPUTE = _REPO_ROOT / "provider" / "aws" / "modules" / "compute"
_USER_DATA = _COMPUTE / "user-data.yaml"
_VARIABLES = _COMPUTE / "variables.tf"
_MAIN = _COMPUTE / "main.tf"

_INSTALLER_VARIABLE = "aws_cli_installer_base_url"


def _user_data() -> str:
    return _USER_DATA.read_text(encoding="utf-8")


def test_user_data_is_readable_so_this_module_cannot_pass_vacuously() -> None:
    text = _user_data()
    assert text.startswith("#cloud-config"), "not the cloud-config template this module pins"
    assert "packages:" in text


def test_unzip_is_installed_for_the_installer_archive() -> None:
    """The archive cannot be unpacked without it, and Ubuntu 24.04 omits it."""
    packages = _user_data().split("packages:", 1)[1].split("write_files:", 1)[0]
    assert re.search(r"^\s*-\s*unzip\s*$", packages, re.MULTILINE), (
        "unzip is absent from the package list, so the AWS CLI archive cannot be unpacked"
    )


def test_user_data_installs_the_aws_cli() -> None:
    text = _user_data()
    assert f"${{{_INSTALLER_VARIABLE}}}" in text, (
        "the rendered user data never references the installer URL, so nothing downloads the CLI"
    )
    assert "aws --version" in text, (
        "the install step must confirm the binary runs; without it a partial install is "
        "reported as success and only fails later, when a parameter read finds no aws"
    )


def test_the_installer_url_is_a_variable_rather_than_a_literal() -> None:
    """A caller mirroring third-party downloads internally must be able to override it."""
    variables = _VARIABLES.read_text(encoding="utf-8")
    assert f'variable "{_INSTALLER_VARIABLE}"' in variables
    assert f"{_INSTALLER_VARIABLE} = var.{_INSTALLER_VARIABLE}" in _MAIN.read_text(
        encoding="utf-8"
    ), "the variable is declared but never passed into templatefile"
    assert "https://awscli.amazonaws.com" not in _user_data(), (
        "the template carries a literal upstream URL instead of taking the variable"
    )


def test_the_archive_name_is_derived_from_the_host_architecture() -> None:
    """var.instance_type selects the architecture; a pinned archive breaks the other one.

    An archive for the wrong architecture unpacks and installs cleanly, then
    fails with "Exec format error" the first time anything runs `aws` -- which
    is how this was found, on an arm64 instance type.
    """
    text = _user_data()
    assert "awscli-exe-linux-$(uname -m).zip" in text, (
        "the archive name must come from uname -m, which reports exactly the two names "
        "Amazon publishes (x86_64, aarch64)"
    )
    for pinned in ("awscli-exe-linux-x86_64.zip", "awscli-exe-linux-aarch64.zip"):
        assert f"/{pinned}" not in text, f"the template pins {pinned} instead of deriving it"


def test_the_install_is_repeatable() -> None:
    """cloud-init may re-run the step; a second run must update, not abort."""
    text = _user_data()
    assert "--update" in text, (
        "the AWS CLI installer aborts when an installation already exists unless --update "
        "is given, which would fail the whole runcmd under set -e on any re-run"
    )


def test_a_user_data_change_replaces_the_instance() -> None:
    """cloud-init runs once, at first boot, so an edit that does not replace is inert.

    Without `user_data_replace_on_change`, terraform reports "1 changed" for a
    template edit, the running host keeps whatever it was provisioned with, and
    the two disagree silently -- the operator believes a provisioning change
    took effect when nothing on the instance ran it. Replacement is also what
    the immutable-deployment rule requires of a provisioning change.
    """
    main = _MAIN.read_text(encoding="utf-8")
    assert "user_data_replace_on_change = true" in main, (
        "an edit to user-data.yaml would update the attribute without re-provisioning"
    )
