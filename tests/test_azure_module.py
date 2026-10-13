"""The azure terraform layer: pins on `provider/azure` and the azure Terragrunt roots.

`provider/azure` is the azure twin of `provider/aws`, and
`remote-instances/root-azure.hcl` is the azure twin of the s3 `root.hcl`;
this suite pins the composition the same way the aws-side suites pin theirs
(`tests/test_user_data_aws_cli.py`'s text pins, `tests/test_state_bucket_name.py`'s
committed-file consistency). Like `tests/test_relay_listener_script.py`,
nothing here initializes terraform: the assertions are `terraform fmt
-check` (a local parse, no providers downloaded) and text/AST pins on the
committed files, so the suite stays hermetic -- the terraform init and
validate that exercise provider schemas run in CI, not in the unit suite.

The pins exist because the composition's security and contract properties
are invisible to a formatter: an empty `security_rule` list is the
zero-inbound doctrine; the absence of any `azurerm_public_ip` resource is
the no-public-address doctrine; the eight root outputs are the names the
deploy flow's azure branch reads (pinned here against the Makefile's own
required list, so the two sides cannot drift); and the cloud-config's
enable-not-start discipline for both daemons is what keeps provisioning
from depending on material it does not deliver (the TLS material) or from
accruing listener-hours (the relay listener, started on demand by the
connect flow).

The root-azure.hcl consistency test is the azure counterpart of
`tests/test_state_bucket_name.py`: the storage account name is composed
from the committed file through `devcontainer_config.state_store`'s own
parser -- the functions production composes the name with -- and asserted
deterministic and charset-legal, so a template or suffix edit that breaks
azure's naming law fails here instead of at bootstrap time on azure.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest
from conftest import _repo_root
from devcontainer_config import state_store

_AZURE = _repo_root() / "provider" / "azure"
_AZURE_REMOTE_INANCES_ROOT = _repo_root() / "remote-instances" / "root-azure.hcl"
_AZURE_ENVCOMMON = _repo_root() / "remote-instances" / "_envcommon" / "remote-azure.hcl"
_CLOUD_INIT = _AZURE / "modules" / "compute" / "cloud-init.yaml"
_SECURITY_MAIN = _AZURE / "modules" / "security" / "main.tf"

# The eight outputs the deploy flow's azure branch requires, read from the
# Makefile's own required list by `_deploy_required_outputs` below -- the
# list is never restated as a python literal here, so a deploy-side edit
# that adds or renames a required output fails this suite until the module's
# outputs follow.
_DEPLOY_REQUIRED_PIN = [
    "vm_name",
    "vm_resource_id",
    "resource_group",
    "relay_namespace",
    "hybrid_connection",
    "vault_name",
    "vault_uri",
    "location",
]


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _read_code(path: Path) -> str:
    """`_read`, with full-line `#` comments stripped.

    The doctrine pins scan for code shapes, and the files' own comments
    quote those shapes when they document them (an absent resource named in
    a doctrine comment is prose, not a violation). Block comments in
    terraform HCL and cloud-config here are all full-line, so dropping
    lines whose first non-space character is `#` is exact for these files.
    """
    return "\n".join(
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )


# ---------------------------------------------------------------------------
# Formatting: the local parse the suite is allowed to run.
# ---------------------------------------------------------------------------


def test_terraform_fmt_is_clean() -> None:
    """`terraform fmt -check -recursive` passes over provider/azure.

    The binary is a hard requirement, not a skip: the composition cannot be
    authored without it (the same toolchain CI installs), so a missing
    binary fails this test with the install hint rather than passing
    vacuously.
    """
    if shutil.which("terraform") is None:
        pytest.fail(
            "terraform is not on PATH; the azure module's formatting contract "
            "cannot be checked without it. Install it: "
            "brew install terraform (macOS) or see hashicorp's install guide."
        )
    result = subprocess.run(
        ["terraform", "fmt", "-check", "-recursive", str(_AZURE)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"terraform fmt -check failed for provider/azure:\n"
        f"{result.stdout}{result.stderr}\n"
        "Run: terraform fmt -recursive provider/azure"
    )


# ---------------------------------------------------------------------------
# The security doctrine: zero inbound, no public address.
# ---------------------------------------------------------------------------


def test_the_nsg_declares_zero_security_rules() -> None:
    """The empty `security_rule` list IS the doctrine: nothing may dial in."""
    text = _read(_SECURITY_MAIN)
    match = re.search(
        r'resource "azurerm_network_security_group" "this" \{(.*?)\n\}', text, re.DOTALL
    )
    assert match is not None, "no azurerm_network_security_group resource found"
    assert re.search(r"^\s*security_rule = \[\]\s*$", match.group(1), re.MULTILINE), (
        "the network security group must declare an explicitly empty security_rule "
        "list: zero inbound rules is the composition's security property"
    )


def test_no_security_rule_resource_or_inline_rule_exists_anywhere() -> None:
    """No azurerm_security_rule resource and no second security_rule anywhere."""
    for tf_file in sorted(_AZURE.rglob("*.tf")):
        text = _read_code(tf_file)
        assert 'resource "azurerm_security_rule"' not in text, (
            f"{tf_file} declares a standalone security rule resource"
        )
        for match in re.finditer(r"^\s*security_rule\s*=", text, re.MULTILINE):
            assert text[match.start() :].lstrip().startswith("security_rule = []"), (
                f"{tf_file} carries a security_rule that is not the empty doctrine list"
            )


def test_no_public_ip_resource_exists_anywhere() -> None:
    """The VM is reachable through the relay and nothing else: no public address."""
    for tf_file in sorted(_AZURE.rglob("*.tf")):
        text = _read_code(tf_file)
        assert "azurerm_public_ip" not in text, (
            f"{tf_file} names azurerm_public_ip; the instance gets no public address"
        )
        assert "public_ip_address_id" not in text, (
            f"{tf_file} attaches a public IP to a network interface"
        )


# ---------------------------------------------------------------------------
# The deploy contract: the eight outputs, pinned against the Makefile.
# ---------------------------------------------------------------------------


def _makefile_text() -> str:
    return _read(_repo_root() / "Makefile")


def _deploy_required_outputs() -> list[str]:
    """The deploy flow's azure required-output list, read from the Makefile itself."""
    match = re.search(r"required=\[([^\]]+)\]", _makefile_text())
    assert match is not None, (
        "the Makefile's instance-deploy azure branch carries no required=[...] list"
    )
    names = re.findall(r"'([^']+)'", match.group(1))
    assert names, "the Makefile's required-output list parsed to nothing"
    return names


def test_the_root_outputs_carry_the_deploy_flows_required_names() -> None:
    """The module emits every name the deploy flow requires, and the two agree."""
    makefile_required = _deploy_required_outputs()
    assert makefile_required == _DEPLOY_REQUIRED_PIN, (
        "the deploy flow's required list changed; update this suite's pin and the "
        "module's outputs together"
    )
    outputs_text = _read(_AZURE / "outputs.tf")
    declared = set(re.findall(r'^output "([^"]+)"', outputs_text, re.MULTILINE))
    missing = [name for name in makefile_required if name not in declared]
    assert not missing, (
        f"provider/azure/outputs.tf is missing the deploy-required outputs: {missing}"
    )


# ---------------------------------------------------------------------------
# The cloud-config: the daemon discipline and the embedded relay unit.
# ---------------------------------------------------------------------------


def test_cloud_init_embeds_the_relay_listener_and_unit_as_base64() -> None:
    """The listener script and unit ride the cloud-config as b64 write_files entries."""
    text = _read(_CLOUD_INIT)
    listener = (
        r"- path: /opt/gd-relay/gd-relay-listener\.py\n"
        r'\s+permissions: "\d{4}"\n'
        r"\s+encoding: b64\n"
        r"\s+content: \$\{relay_listener_b64\}"
    )
    assert re.search(listener, text), (
        "the listener script is not embedded as a b64 write_files entry from ${relay_listener_b64}"
    )
    unit = (
        r"- path: /opt/gd-relay/gd-relay\.service\n"
        r'\s+permissions: "\d{4}"\n'
        r"\s+encoding: b64\n"
        r"\s+content: \$\{relay_unit_b64\}"
    )
    assert re.search(unit, text), (
        "the systemd unit is not embedded as a b64 write_files entry from ${relay_unit_b64}"
    )


def test_cloud_init_writes_the_relay_environment_file_in_plain_text() -> None:
    """The env file carries the three resource names directly, never base64."""
    text = _read(_CLOUD_INIT)
    pattern = (
        r"- path: /etc/default/gd-relay\n"
        r'\s+permissions: "0644"\n'
        r"\s+content: \|(.*?)"
        r"(?=\n\s*- path:|\nruncmd:)"
    )
    match = re.search(pattern, text, re.DOTALL)
    assert match is not None, "no plain-text /etc/default/gd-relay write_files entry"
    content = match.group(1)
    assert "GD_RELAY_NAMESPACE=${relay_namespace}" in content
    assert "GD_RELAY_HYBRID_CONNECTION=${hybrid_connection}" in content
    assert "GD_RELAY_TARGET=127.0.0.1:${tls_listen_port}" in content
    assert "encoding: b64" not in match.group(0), (
        "the env file is plain text: three resource names, never secrets"
    )


def test_cloud_init_enables_never_starts_docker() -> None:
    """The rootful daemon is disabled with a FATAL assert; the rootless one enabled only."""
    text = _read(_CLOUD_INIT)
    assert "systemctl disable --now docker.service docker.socket" in text
    assert "FATAL: docker.service/docker.socket (rootful) still active or enabled" in text
    assert "systemctl --user enable docker.service" in text
    # The setup tool starts the user service it generates; provisioning
    # stops it again. Provisioning itself never starts the user service.
    assert "systemctl --user start docker.service" not in text
    assert "systemctl --user stop docker.service" in text


def test_cloud_init_enables_never_starts_the_relay_listener() -> None:
    """The listener unit is enabled, not started: the connect flow starts it on demand."""
    text = _read_code(_CLOUD_INIT)
    assert "systemctl enable gd-relay.service" in text
    assert not re.search(r"systemctl start gd-relay", text), (
        "provisioning must not start gd-relay: the unit is started on demand by the "
        "connect flow's run command, because listener-hours bill while it is connected"
    )
    assert "systemctl enable --now gd-relay" not in text


def test_cloud_init_disables_ssh_without_purging_it() -> None:
    """sshd is disabled, and the package stays for platform compatibility."""
    text = _read_code(_CLOUD_INIT)
    assert "systemctl disable --now ssh sshd 2>/dev/null || true" in text
    assert not re.search(r"apt-get purge\s+\S*openssh", text), (
        "openssh-server is not purged: the disable is the doctrine, the package "
        "stays for the azure VM agent's compatibility"
    )
    assert not re.search(r"systemctl (enable|start)\b[^\n]*\bsshd", text)


def test_cloud_init_has_no_aws_surface() -> None:
    """No SSM agent, no AWS CLI, no unzip: none of the aws twin's client surface."""
    text = _read_code(_CLOUD_INIT)
    for absent in ("amazon-ssm-agent", "awscli", "unzip", "snap install"):
        assert absent not in text, (
            f"the azure cloud-config carries the aws twin's {absent!r}; the azure "
            "surface pulls its material through the identity endpoint instead"
        )


def test_the_compute_module_passes_the_base64_payloads_into_the_template() -> None:
    """filebase64 runs in terraform; the template receives the results as variables."""
    main_text = _read_code(_AZURE / "modules" / "compute" / "main.tf")
    assert re.search(
        r'relay_listener_b64\s*=\s*filebase64\("\$\{path\.module\}/gd-relay-listener\.py"\)',
        main_text,
    )
    assert re.search(
        r'relay_unit_b64\s*=\s*filebase64\("\$\{path\.module\}/gd-relay\.service"\)', main_text
    )
    assert "relay_listener_b64   = local.relay_listener_b64" in main_text
    assert "relay_unit_b64       = local.relay_unit_b64" in main_text
    cloud_init_text = _read(_CLOUD_INIT)
    assert "${relay_listener_b64}" in cloud_init_text
    assert "${relay_unit_b64}" in cloud_init_text


# ---------------------------------------------------------------------------
# The variable surface: the validations the toggles depend on.
# ---------------------------------------------------------------------------


def test_root_variables_carry_the_composition_validations() -> None:
    """The paired toggle validations, the naming law, and the listener bounds."""
    text = _read(_AZURE / "variables.tf")
    for fragment in (
        "var.vnet_cidr is required when var.create_network is true",
        "var.subnet_cidr is required when var.create_network is true",
        "var.resource_group_name is required when var.create_network is false",
        "var.resource_group_name must be left unset when var.create_network is true",
        "var.subnet_id is required when var.create_network is false",
        "var.subnet_id must be left unset when var.create_network is true",
        "var.instance_name is required when var.create_platform is true",
        "var.identity_id is required when var.create_platform is false",
        "var.identity_id must be left unset when var.create_platform is true",
        "var.relay_namespace is required when var.create_platform is false",
        "var.hybrid_connection is required when var.create_platform is false",
    ):
        assert fragment in text, f"missing validation message: {fragment!r}"
    # The azure naming law: lowercase alphanumerics and dashes, letter
    # first, bounded so the derived -vault and -relay names stay legal.
    assert 'can(regex("^[a-z][a-z0-9-]{0,17}$", var.name_prefix))' in text


def test_compute_variables_carry_the_daemon_validations() -> None:
    """The listener binds loopback only, on a legal port, from a four-part URN."""
    text = _read(_AZURE / "modules" / "compute" / "variables.tf")
    assert 'condition     = var.docker_tls_listen_address == "127.0.0.1"' in text
    assert "var.docker_tls_listen_port > 0 && var.docker_tls_listen_port <= 65535" in text
    assert 'length(split(":", var.os_image_urn)) == 4' in text


# ---------------------------------------------------------------------------
# root-azure.hcl: the committed template, composed through the production
# parser.
# ---------------------------------------------------------------------------


def _committed_azure_root() -> str:
    return _AZURE_REMOTE_INANCES_ROOT.read_text(encoding="utf-8")


def test_azure_root_composes_a_deterministic_legal_account_name() -> None:
    """The committed template and suffix compose the same legal name, twice.

    The same shape tests/test_state_bucket_name.py pins for the s3 root:
    composed through state_store's own parser -- the functions production
    composes the name with -- so a template or suffix edit that breaks
    azure's naming law fails here, not at bootstrap.
    """
    instance = f"inst-{uuid.uuid4().hex[:8]}"
    location = f"testlocation-{uuid.uuid4().hex[:6]}"
    subscription = uuid.uuid4().hex
    text = _committed_azure_root()
    first = state_store.compose_from_root_hcl(text, instance, location, subscription)
    second = state_store.compose_from_root_hcl(text, instance, location, subscription)
    assert first == second, "the committed template must compose deterministically"
    assert re.fullmatch(r"[a-z0-9]{3,24}", first), (
        f"the committed template composed {first!r}, outside azure's naming law"
    )


def test_azure_root_declares_what_state_store_parses() -> None:
    """The two declarations the parser needs are the committed file's only naming truth."""
    text = _committed_azure_root()
    assert state_store.declared_template(text) == (
        "tgstate${local.instance_name}${local.state_store_suffix}"
    )
    assert state_store.declared_suffix(text) == "gb9cho"
    # And the parser reads exactly this file, by name.
    assert state_store.ROOT_AZURE_HCL_FILENAME == "root-azure.hcl"


def test_azure_root_declares_the_version_floors_and_engine() -> None:
    """The floors root.hcl declares, declared again for the azure root."""
    text = _committed_azure_root()
    assert 'terraform_binary = "terraform"' in text
    assert 'terraform_version_constraint  = ">= 1.10"' in text
    assert 'terragrunt_version_constraint = ">= 1.1.3"' in text
    assert 'provider "azurerm" {' in text
    assert "features {}" in text


def test_azure_root_reads_the_sidecar_the_state_store_writes() -> None:
    """The remote_state names come from the side-car, one naming authority."""
    text = _committed_azure_root()
    assert 'jsondecode(file("${get_terragrunt_dir()}/.state-store.json"))' in text
    assert "resource_group_name  = local.state_resource_group" in text
    assert "storage_account_name = local.state_storage_account" in text
    assert 'container_name   = "tfstate"' in text
    assert "use_azuread_auth = true" in text
    assert 'key = "${path_relative_to_include()}/terraform.tfstate"' in text
    # The side-car is written beside terragrunt.hcl, the path the root reads.
    assert state_store.STATE_STORE_SIDECAR_FILENAME == ".state-store.json"
    assert 'backend = "azurerm"' in text


def test_the_s3_root_stays_untouched_by_the_azure_root() -> None:
    """root.hcl carries no azure declarations, and root-azure.hcl no s3 backend."""
    s3_text = _read(_repo_root() / "remote-instances" / "root.hcl")
    azure_text = _committed_azure_root()
    assert "state_storage_name_template" not in s3_text
    assert "state_store_suffix" not in s3_text
    assert "state_bucket_suffix" not in azure_text
    assert "state_bucket_name" not in azure_text
    assert 'backend = "s3"' not in azure_text
    assert 'backend = "azurerm"' not in s3_text


# ---------------------------------------------------------------------------
# _envcommon/remote-azure.hcl and the scaffold include.
# ---------------------------------------------------------------------------


def test_the_azure_envcommon_sources_the_azure_module_and_pins_the_conventions() -> None:
    text = _read(_AZURE_ENVCOMMON)
    assert 'source = "${get_repo_root()}//provider/azure"' in text
    for fragment in (
        'docker_data_root = "/mnt/docker-data"',
        'docker_daemon_user        = "dockerd"',
        'docker_tls_listen_address = "127.0.0.1"',
        "docker_tls_listen_port    = 2376",
        'docker_repo_base_url      = "https://download.docker.com/linux/ubuntu"',
        'docker_repo_channel       = "stable"',
    ):
        assert fragment in text, f"the azure envcommon is missing the convention: {fragment!r}"
    assert "find_in_parent_folders" not in text, (
        "the envcommon is included by path, never found by search"
    )


def test_the_azure_scaffold_includes_the_azure_root() -> None:
    """The scaffolded terragrunt.hcl targets root-azure.hcl and the azure envcommon.

    Rendered through the production template, not a restated string.
    """
    from devcontainer_config import instance_ops

    text = instance_ops._render_azure_terragrunt_hcl(
        "example", "examplelocation", "Standard_D2pls_v5", "10.100.0.0/16", "10.100.1.0/24"
    )
    assert 'path = find_in_parent_folders("root-azure.hcl")' in text
    assert (
        'path = "${dirname(find_in_parent_folders("root-azure.hcl"))}'
        '/_envcommon/remote-azure.hcl"' in text
    )
    assert 'find_in_parent_folders("root.hcl")' not in text, (
        "the azure scaffold must not target the s3 root"
    )


def test_the_committed_azure_root_and_envcommon_exist_for_the_scaffold() -> None:
    """The files the scaffold's includes name are committed beside it."""
    assert _AZURE_REMOTE_INANCES_ROOT.is_file()
    assert _AZURE_ENVCOMMON.is_file()
    assert (_AZURE / "remote-azure-vm.tf").is_file()
    assert (_AZURE / "README.md").is_file()


# ---------------------------------------------------------------------------
# The composition: three module blocks, the aws-mirroring structure.
# ---------------------------------------------------------------------------


def test_the_root_composition_is_exactly_three_module_blocks() -> None:
    """No resource block of any kind in the root; the aws twin's composition shape."""
    text = _read(_AZURE / "remote-azure-vm.tf")
    blocks = re.findall(r"^(module|resource|data|variable|output|locals)\b", text, re.MULTILINE)
    assert blocks.count("module") == 3, f"the root declares {blocks.count('module')} modules, not 3"
    assert "resource" not in blocks, "the root owns no resource of its own"
    assert "data" not in blocks, "the root resolves nothing through a data source"
    for name in ("network", "security", "compute"):
        assert f'module "{name}"' in text


def test_the_security_unit_carries_the_platform_role_grants() -> None:
    """The identity gets read-only vault access and the listener role; humans get their own."""
    text = _read(_SECURITY_MAIN)
    for fragment in (
        '"Key Vault Secrets User"',
        '"Azure Relay Listener"',
        '"Azure Relay Sender"',
        '"Key Vault Secrets Officer"',
        "for_each = toset(var.relay_sender_object_ids)",
        "for_each = toset(var.keyvault_officer_object_ids)",
        "enable_rbac_authorization = true",
        "requires_client_authorization = true",
    ):
        assert fragment in text, f"the platform unit is missing: {fragment!r}"


def test_the_hybrid_connection_is_keyed_to_the_instance() -> None:
    text = _read(_SECURITY_MAIN)
    assert 'name                          = "gd-${var.instance_name}"' in text


def test_every_submodule_declares_the_version_floors() -> None:
    """terraform >= 1.10 and azurerm ~> 4.0, in the root and all three submodules."""
    for versions_file in (
        _AZURE / "versions.tf",
        _AZURE / "modules" / "network" / "versions.tf",
        _AZURE / "modules" / "security" / "versions.tf",
        _AZURE / "modules" / "compute" / "versions.tf",
    ):
        text = _read(versions_file)
        assert 'required_version = ">= 1.10"' in text, f"{versions_file} lacks the engine floor"
        assert '"~> 4.0"' in text, f"{versions_file} lacks the azurerm floor"
    assert 'source  = "hashicorp/tls"' in _read(_AZURE / "modules" / "compute" / "versions.tf"), (
        "the compute module's bootstrap key needs the tls provider declared"
    )


# ---------------------------------------------------------------------------
# The module README: present, and synchronized with the input surface.
# ---------------------------------------------------------------------------


def test_the_readme_documents_every_root_input() -> None:
    """Every root variable appears in the README's input table, and vice versa."""
    variables_text = _read(_AZURE / "variables.tf")
    readme = _read(_AZURE / "README.md")
    declared = set(re.findall(r'^variable "([^"]+)"', variables_text, re.MULTILINE))
    # Scoped to the Inputs section: the Outputs table's rows share the
    # table syntax but name outputs, not inputs.
    inputs_section = readme.split("## Inputs", 1)[1].split("## Outputs", 1)[0]
    documented = set(re.findall(r"^\| `([a-z_]+)` \|", inputs_section, re.MULTILINE))
    missing = sorted(declared - documented)
    assert not missing, f"the README's input table is missing: {missing}"
    stale = sorted(documented - declared)
    assert not stale, f"the README's input table documents nonexistent inputs: {stale}"
