"""The gcp terraform layer: pins on `provider/gcp` and the gcp Terragrunt roots.

`provider/gcp` is the gcp twin of `provider/azure`, and
`remote-instances/root-gcp.hcl` is the gcp twin of the azure root; this
suite pins the composition the same way the azure-side suite pins theirs
(`tests/test_azure_module.py`'s text pins, its deploy-output pin against
the Makefile, its committed-root consistency test through the production
parser). Like that suite, nothing here initializes terraform: the
assertions are `terraform fmt -check` (a local parse, no providers
downloaded) and text pins on the committed files, so the suite stays
hermetic -- the terraform init and validate that exercise provider schemas
run in CI and in the slice's own verification, not in the unit suite.

The pins exist because the composition's security and contract properties
are invisible to a formatter: the absence of any `access_config` block is
the no-external-address doctrine; the single `35.235.240.0/20` firewall
rule is the IAP-only ingress doctrine; `enable-guest-attributes` is the
boot-applied install's whole channel; and the startup script's
attribute-written-last discipline is what keeps the laptop-side waits from
gating on state that was never applied. The deploy-output pin reads the
Makefile's own gcp required list, and the root-gcp.hcl consistency test
composes the state bucket's name through `devcontainer_config.state_store_gcp`'s
own parser -- the functions production composes the name with -- so a
template, suffix or digest-formula edit that breaks the two sides' agreement
fails here instead of at deploy time on gcp.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest
from conftest import _repo_root
from devcontainer_config import certs, state_store_gcp

_GCP = _repo_root() / "provider" / "gcp"
_GCP_REMOTE_ROOT = _repo_root() / "remote-instances" / "root-gcp.hcl"
_GCP_ENVCOMMON = _repo_root() / "remote-instances" / "_envcommon" / "remote-gcp.hcl"
_CLOUD_INIT = _GCP / "modules" / "compute" / "boot-user-data.yaml"
_STARTUP_SCRIPT = _GCP / "modules" / "compute" / "boot-startup-script.sh"
_NETWORK_MAIN = _GCP / "modules" / "network" / "main.tf"
_PLATFORM_MAIN = _GCP / "modules" / "platform" / "main.tf"
_COMPUTE_MAIN = _GCP / "modules" / "compute" / "main.tf"

# The six outputs the deploy flow's gcp branch requires, read from the
# Makefile's own required list by `_deploy_required_outputs` below -- the
# list is never restated as a python literal here, so a deploy-side edit
# that adds or renames a required output fails this suite until the
# module's outputs follow.
_DEPLOY_REQUIRED_PIN = [
    "instance_name",
    "zone",
    "project_id",
    "region",
    "secrets_prefix",
    "state_bucket",
]

# The four secret leaves the boot-applied install fetches, in fetch order:
# the checksum secret first (its absence is the pre-publish state), then
# the three TLS material bodies. These are never restated as literals in
# the assertions below -- they are read from `devcontainer_config.certs`'s
# constants, the same constants the publication flows write with.
_TLS_LEAVES = (
    certs.GCP_SERVER_KEY_SECRET,
    certs.GCP_SERVER_CERT_SECRET,
    certs.GCP_CA_SECRET,
    certs.GCP_CHECKSUM_SECRET,
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _read_code(path: Path) -> str:
    """`_read`, with full-line `#` comments stripped.

    The doctrine pins scan for code shapes, and the files' own comments
    quote those shapes when they document them (an absent block named in
    a doctrine comment is prose, not a violation). Block comments in
    terraform HCL, cloud-config and this POSIX shell here are all
    full-line, so dropping lines whose first non-space character is `#`
    is exact for these files.
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
    """`terraform fmt -check -recursive` passes over provider/gcp.

    The binary is a hard requirement, not a skip: the composition cannot be
    authored without it (the same toolchain CI installs), so a missing
    binary fails this test with the install hint rather than passing
    vacuously.
    """
    if shutil.which("terraform") is None:
        pytest.fail(
            "terraform is not on PATH; the gcp module's formatting contract "
            "cannot be checked without it. Install it: "
            "brew install terraform (macOS) or see hashicorp's install guide."
        )
    result = subprocess.run(
        ["terraform", "fmt", "-check", "-recursive", str(_GCP)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"terraform fmt -check failed for provider/gcp:\n"
        f"{result.stdout}{result.stderr}\n"
        "Run: terraform fmt -recursive provider/gcp"
    )


# ---------------------------------------------------------------------------
# The security doctrine: no external address, one IAP rule.
# ---------------------------------------------------------------------------


def test_no_access_config_exists_anywhere() -> None:
    """The interface declares no access_config: no external address, ever.

    gcp assigns an external address to an interface exactly when an
    access_config block is declared, so the absence IS the doctrine --
    there is no flag left at false to flip back.
    """
    for tf_file in sorted(_GCP.rglob("*.tf")):
        text = _read_code(tf_file)
        assert "access_config" not in text, (
            f"{tf_file} carries an access_config; the instance gets no external address"
        )
        assert "nat_ip" not in text or "nat_ip_allocate_option" in text, (
            f"{tf_file} names a static NAT address; addresses are allocated automatically"
        )


def test_the_iap_firewall_is_the_single_ingress_rule() -> None:
    """Exactly one google_compute_firewall, admitting tcp from the IAP range."""
    firewall_files = [
        path
        for path in sorted(_GCP.rglob("*.tf"))
        if re.search(r'^resource "google_compute_firewall"', _read_code(path), re.MULTILINE)
    ]
    assert len(firewall_files) == 1, (
        f"the composition declares {len(firewall_files)} firewall resources; "
        "the sole ingress rule is the doctrine"
    )
    text = _read_code(firewall_files[0])
    match = re.search(
        r'resource "google_compute_firewall" "iap_ingress" \{(.*?)\n\}', text, re.DOTALL
    )
    assert match is not None, "no google_compute_firewall iap_ingress resource found"
    body = match.group(1)
    assert 'direction     = "INGRESS"' in body
    assert re.search(r'source_ranges = \["35\.235\.240\.0/20"\]', body), (
        "the one rule must admit exactly google's documented IAP range"
    )
    assert re.search(r'protocol = "tcp"', body), "the rule must be a tcp rule"
    assert "ports    = [var.docker_tls_listen_port]" in body, (
        "the admitted port must be the daemon's configured TLS port, never a literal"
    )
    assert "target_tags = [var.instance_tag]" in body, (
        "the rule must target the instance's network tag, not the whole network"
    )
    # Flow logging is off by omission: no log_config block is declared
    # anywhere on the firewall, because a log_config block ENABLES logging
    # -- a block with EXCLUDE_ALL_METADATA is still flow logging on, only
    # with metadata excluded.
    assert "log_config" not in body, (
        "the one rule declares no log_config: the block's presence ENABLES "
        "flow logging, and logging-off is expressed by the block's absence"
    )


def test_the_network_module_declares_no_tags_input() -> None:
    """gcp network resources expose no labels, so the module refuses an input
    it could only silently drop -- and the root wires none into it."""
    variables_text = _read_code(_GCP / "modules" / "network" / "variables.tf")
    assert 'variable "tags"' not in variables_text, (
        "the network module declares no tags variable: google_compute_network, "
        "google_compute_subnetwork, google_compute_router, "
        "google_compute_router_nat and google_compute_firewall expose no "
        "labels attribute, so the input would be accepted and silently dropped"
    )
    assert "this module's resources carry no labels" in _read(
        _GCP / "modules" / "network" / "variables.tf"
    ), "the module's variables file states why no labels input exists"
    composition_text = _read_code(_GCP / "remote-gcp-vm.tf")
    network_block = re.search(r'module "network" \{(.*?)\n\}', composition_text, re.DOTALL)
    assert network_block is not None
    assert "tags" not in network_block.group(1), "the root wires no tags into the network module"
    # The README's labels row is scoped to the resources that do expose
    # labels, naming the network module's exemption.
    readme = _read(_GCP / "README.md")
    tags_row = next(line for line in readme.splitlines() if line.startswith("| `tags` |"))
    assert "network" in tags_row and "expose none" in tags_row, (
        "the README's tags row must scope the labels to instance/disk/secrets "
        "and state the network module's exemption"
    )


def test_the_custom_mode_network_carries_no_implied_allow() -> None:
    """auto_create_subnetworks is false: a custom-mode VPC has no implied rules."""
    text = _read_code(_NETWORK_MAIN)
    assert "auto_create_subnetworks = false" in text, (
        "the virtual network must be custom-mode: an auto-mode network carries "
        "implied default rules and a subnetwork per region, neither of which "
        "this composition inherits unexamined"
    )


def test_the_nat_gateway_is_declared_for_egress() -> None:
    """The internal-only instance's outbound path is a declared Cloud NAT."""
    text = _read_code(_NETWORK_MAIN)
    assert 'resource "google_compute_router_nat"' in text, (
        "an internal-only instance has no egress on gcp until a NAT gateway "
        "exists; the azure twin pins default_outbound_access_enabled for the "
        "same reason"
    )
    assert 'nat_ip_allocate_option = "AUTO_ONLY"' in text, (
        "NAT addresses are allocated automatically: the composition holds no "
        "static IP to reserve and track"
    )


# ---------------------------------------------------------------------------
# The instance: guest attributes, scopes, tags, and the update doctrine.
# ---------------------------------------------------------------------------


def test_the_instance_enables_guest_attributes_and_logging() -> None:
    text = _read_code(_COMPUTE_MAIN)
    assert 'enable-guest-attributes = "TRUE"' in text, (
        "the boot-applied install's checksum, provisioned and error attributes "
        "are the guest-attribute API's; without this flag the channel answers "
        "nothing"
    )
    assert 'google-logging-enabled  = "true"' in text


def test_the_instance_assumes_the_account_with_cloud_platform_scope() -> None:
    text = _read_code(_COMPUTE_MAIN)
    match = re.search(r"service_account \{(.*?)\n  \}", text, re.DOTALL)
    assert match is not None, "no service_account block on the instance"
    assert 'scopes = ["cloud-platform"]' in match.group(1), (
        "the scope list is immutable at create time; cloud-platform is what "
        "the Secret Manager REST calls require, and least privilege is "
        "enforced by the per-secret bindings, not by the scope"
    )


def test_the_instance_tags_carry_the_firewall_target() -> None:
    text = _read_code(_COMPUTE_MAIN)
    match = re.search(r"resource \"google_compute_instance\" \"this\" \{(.*?)\n\}", text, re.DOTALL)
    assert match is not None, "no google_compute_instance resource found"
    assert "tags = [local.instance_tag]" in match.group(1), (
        "the instance's network tag must be exactly the local the root "
        "composition hands the firewall, so the one rule and the one tagged "
        "interface cannot drift apart"
    )


def test_the_data_disk_carries_the_labels_the_variables_document() -> None:
    """var.tags' documented contract covers the instance AND its data disk."""
    text = _read_code(_COMPUTE_MAIN)
    match = re.search(r"resource \"google_compute_disk\" \"data\" \{(.*?)\n\}", text, re.DOTALL)
    assert match is not None, "no google_compute_disk data resource found"
    assert "labels = var.tags" in match.group(1), (
        "the data disk must carry the same labels the instance carries: "
        "the tags variable's own description names the disk, so a disk "
        "without them would silently opt out of the labeling the rest of "
        "the composition answers to"
    )


def test_the_instance_updates_metadata_in_place() -> None:
    text = _read_code(_COMPUTE_MAIN)
    assert "allow_stopping_for_update = true" in text, (
        "the boot payloads' rendering is instance metadata; gcp updates it in "
        "place under this flag, where azure replaces the VM -- and in place is "
        "safe because both boot scripts are idempotent and the cert-install "
        "flow drives any material-change reboot"
    )


# ---------------------------------------------------------------------------
# The two boot payloads.
# ---------------------------------------------------------------------------


def test_cloud_init_disables_and_asserts_the_rootful_daemon_and_enables_never_starts_the_rootless_one() -> (
    None
):
    text = _read(_CLOUD_INIT)
    assert "systemctl disable --now docker.service docker.socket" in text
    assert "FATAL: docker.service/docker.socket (rootful) still active or enabled" in text
    assert "systemctl --user enable docker.service" in text
    # The setup tool starts the user service it generates; provisioning
    # stops it again. Provisioning itself never starts the user service.
    assert "systemctl --user start docker.service" not in text
    assert "systemctl --user stop docker.service" in text


def test_cloud_init_writes_the_marker_last() -> None:
    """The provisioning marker is the LAST runcmd entry, and only there."""
    text = _read_code(_CLOUD_INIT)
    marker_writes = [match.start() for match in re.finditer(r"/etc/general-dev-provisioned", text)]
    assert marker_writes, "the cloud-init writes no provisioning marker"
    # Exactly one write, in the final runcmd entry: nothing may follow it.
    assert len(marker_writes) == 1, (
        "the marker path must appear exactly once in executable content -- the last runcmd entry"
    )
    marker_line_start = text.rfind("\n", 0, marker_writes[0]) + 1
    write_line = text[marker_line_start : marker_writes[0] + len("/etc/general-dev-provisioned")]
    assert "date -u" in write_line, (
        "the marker carries the provisioning timestamp, written in one statement"
    )
    assert text.rstrip().endswith("/etc/general-dev-provisioned"), (
        "the marker write must be the cloud-init's final statement: a "
        "consumer polling it must never see it before the actual work is done"
    )


def test_cloud_init_binds_the_interface_address_with_tls_verify() -> None:
    text = _read(_CLOUD_INIT)
    match = re.search(
        r"cat > /home/\$\{daemon_user\}/\.config/docker/daemon\.json <<DAEMON_JSON\n(.*?)\n\s*DAEMON_JSON",
        text,
        re.DOTALL,
    )
    assert match is not None, "no daemon.json heredoc in the cloud-init"
    daemon_json = match.group(1)
    assert (
        '"hosts": ["unix:///run/user/$(id -u ${daemon_user})/docker.sock", "tcp://${tls_listen_address}:${tls_listen_port}"]'
        in daemon_json
    )
    assert '"tls": true' in daemon_json
    assert '"tlsverify": true' in daemon_json, (
        "the listener is mTLS or it is nothing: tlsverify is the wire "
        "contract the docker context's client certificates answer"
    )
    for filename in ("ca.pem", "server-cert.pem", "server-key.pem"):
        assert f"/home/${{daemon_user}}/tls/{filename}" in daemon_json


def test_cloud_init_has_no_aws_surface_and_no_relay_unit() -> None:
    """None of the aws twin's client surface, none of the azure twin's listener."""
    text = _read_code(_CLOUD_INIT)
    for absent in ("amazon-ssm-agent", "awscli", "unzip", "snap install", "gd-relay"):
        assert absent not in text, (
            f"the gcp cloud-config carries {absent!r}; the gcp transport is "
            "google's IAP front end and pulls its material through Secret "
            "Manager, so neither twin's extras belong here"
        )


def test_cloud_init_disables_ssh_without_purging_it() -> None:
    text = _read_code(_CLOUD_INIT)
    assert "systemctl stop ssh.service 2>/dev/null || true" in text
    assert "systemctl stop ssh.socket 2>/dev/null || true" in text, (
        "the socket-activated unit is named alongside the service: Ubuntu "
        "24.04 can ship either, and a live ssh.socket alone would answer "
        "connections"
    )
    assert "systemctl disable ssh.service 2>/dev/null || true" in text
    assert "systemctl disable ssh.socket 2>/dev/null || true" in text
    assert "disable --now ssh.service ssh.socket sshd" not in text, (
        "one combined disable fails atomically when any name is absent "
        "(sshd is not a unit on noble), leaving the existing units running"
    )
    assert not re.search(r"apt-get purge\s+\S*openssh", text), (
        "openssh-server is not purged: the disable is the doctrine, the "
        "package stays for platform compatibility"
    )
    # The disable's post-condition is asserted, ssh.socket included: the
    # `|| true` covers images that ship none of the unit names, so the
    # assert is what turns a failed disable of an existing unit into a
    # FATAL instead of a silent listener.
    assert re.search(
        r"is-active --quiet ssh\.socket.*is-enabled --quiet ssh\.socket", text, re.DOTALL
    ), "the ssh post-condition assert must cover ssh.socket, not the service alone"
    assert "FATAL: ssh.service/ssh.socket still active or enabled" in text


def test_cloud_init_pins_the_rootlesskit_detach_netns_drop_in() -> None:
    """The 0.0.0.0 listener's reachability rests on rootlesskit detaching the
    container netns while dockerd keeps the host netns; that behavior is
    pinned by a systemd user drop-in, not left to the installed
    rootlesskit's default, which has flipped across releases.

    The drop-in mirrors the delegate.conf pattern: root-created under the
    daemon user's own systemd user unit directory, owned by the daemon
    user, declared before the unit is enabled.
    """
    text = _read(_CLOUD_INIT)
    assert "/home/${daemon_user}/.config/systemd/user/docker.service.d/rootlesskit.conf" in text, (
        "the drop-in lives beside the generated unit, in the daemon user's own config"
    )
    assert "DOCKERD_ROOTLESS_ROOTLESSKIT_DETACH_NETNS=true" in text, (
        "the environment the listener's nic0 reachability depends on is "
        "declared, never inherited from the installed rootlesskit's default"
    )
    assert "detaches the container netns" in text, (
        "the doctrine comment must state WHY the value is pinned: the "
        "daemon's 0.0.0.0 listener is reachable on nic0 only because "
        "rootlesskit detaches the container netns while dockerd keeps the "
        "host netns"
    )
    # Ordering: the drop-in is written before the unit is enabled, and it
    # is chowned to the daemon user (root-created, daemon-owned, the
    # delegate.conf pattern).
    drop_in_at = text.index("rootlesskit.conf")
    enable_at = text.index("systemctl --user enable docker.service")
    assert drop_in_at < enable_at, "the drop-in must exist before the unit is enabled"
    chown_at = text.index(
        "chown ${daemon_user}:${daemon_user} /home/${daemon_user}/.config/systemd/user/docker.service.d/rootlesskit.conf"
    )
    assert chown_at > drop_in_at


def test_cloud_init_locates_the_data_disk_by_the_deterministic_by_id_handle() -> None:
    """The cloud-init's handle and the disk resource's name compose from one local."""
    cloud_text = _read_code(_CLOUD_INIT)
    assert "/dev/disk/by-id/google-${data_disk_name}" in cloud_text, (
        "the data disk must be located through /dev/disk/by-id/google-<name>, "
        "the handle gce derives from the disk's own name"
    )
    main_text = _read_code(_COMPUTE_MAIN)
    match = re.search(r'data_disk_name = "\$\{var\.name_prefix\}-data"', main_text)
    assert match is not None, (
        "the disk name must be composed once, in the compute module's locals, "
        "so the resource and the by-id handle cannot disagree"
    )
    assert "name    = local.data_disk_name" in main_text or re.search(
        r"name\s+= local\.data_disk_name", main_text
    )
    # And no scan fallback: the by-id handle's absence is a FATAL, not a
    # shape to guess around.
    assert "FATAL" in cloud_text and "blkid -L DOCKERDATA" in cloud_text
    assert "expected exactly 1 empty data disk" not in cloud_text, (
        "the azure twin's lsblk scan fallback must not return: the by-id "
        "handle is a property of the disk this deployment created"
    )


def test_the_startup_script_implements_the_boot_applied_contract() -> None:
    """The every-boot script: marker wait, drift guard, attribute writes, sentinel."""
    text = _read_code(_STARTUP_SCRIPT)
    # The marker wait: bounded, and before any fetch.
    assert "/etc/general-dev-provisioned" in text
    assert "MARKER_TIMEOUT_DEFAULT" in text, (
        "the marker wait's budget arrives from the terraform variable the "
        "instance metadata renders in, never a bare literal"
    )
    assert "GD_STARTUP_MARKER_TIMEOUT" not in text, (
        "the script runs at boot on the VM, where a laptop-side env var "
        "cannot reach; the override is the terraform variable"
    )
    marker_at = text.index("/etc/general-dev-provisioned")
    fetch_at = text.index("versions/latest:access")
    assert marker_at < fetch_at, (
        "the script must wait for provisioning before fetching anything: on "
        "the first boot the guest agent can run it before cloud-init finishes"
    )
    # The token passes through stdin, never argv.
    assert "-H @-" in text, "the OAuth token reaches the Secret Manager call through stdin (-H @-)"
    assert '-H "Authorization' not in text and "-H 'Authorization" not in text, (
        "no Authorization header may be built as an argv element: it would be "
        "readable from the process table for the length of the call"
    )
    # The guest-attribute PUTs: checksum (reporting only, after
    # apply+restart), provisioned (every success path), error (any
    # failure, cleared on every success).
    assert re.search(r"put_attribute \"\$CHECKSUM_KEY\" \"\$CHECKSUM\"", text)
    assert re.search(r"put_attribute \"\$PROVISIONED_KEY\" \"\$PROVISIONED_VALUE\"", text)
    assert "$ERROR_KEY" in text
    # The drift guard trusts the root-owned state file, never the VM-
    # writable guest attribute: a planted attribute value must not be able
    # to read as "already applied" and defeat rotation. The state file is
    # 0600 and written only by the apply path, after the restart.
    assert 'APPLIED_STATE="/etc/general-dev/applied-checksum"' in text, (
        "the drift comparison reads the root-owned state file, not the "
        "tls-checksum attribute: the attribute is VM-writable, so a planted "
        "value there would silently defeat rotation"
    )
    assert 'chmod 0600 "$APPLIED_STATE"' in text, (
        "the applied-checksum state file is 0600: the daemon account and "
        "everything below it must not be able to rewrite the drift input"
    )
    dir_at = text.index("install -d -m 0755 /etc/general-dev")
    assert dir_at < text.index('printf \'%s\\n\' "$CHECKSUM" > "$APPLIED_STATE"'), (
        "the state file's parent directory is created before the state-file "
        "write: without it the write fails with 'cannot create ... Directory "
        "nonexistent' on the first apply"
    )
    state_at = text.index("$APPLIED_STATE")
    attribute_read_absent = "guest-attributes/$NAMESPACE/$CHECKSUM_KEY" not in text
    assert attribute_read_absent, (
        "the fetched checksum must never be compared against the "
        "tls-checksum attribute; the attribute is the reporting channel "
        "the laptop-side waits read, nothing more"
    )
    assert state_at < text.index('applied" = "$CHECKSUM"'), (
        "the drift comparison reads the state file"
    )
    # The apply phase is one recorded unit: every failure inside it routes
    # through fail (which records the error attribute), never a bare set -e
    # exit that would leave mixed material on disk with nothing recorded.
    apply_at = text.index("apply_material()")
    apply_call_at = text.index("if ! apply_material; then")
    assert apply_at < apply_call_at
    assert 'fail "apply the TLS material"' in text, (
        "an apply-phase failure must be recorded, never a bare set -e exit"
    )
    # The state file is written LAST inside the apply, so its value always
    # describes material that is live.
    state_write_at = text.index('printf \'%s\\n\' "$CHECKSUM" > "$APPLIED_STATE"')
    restart_at = text.index("systemctl --user restart docker.service")
    assert restart_at < state_write_at, (
        "the state file is written only after the restart succeeded, so a "
        "half-apply never reads as applied"
    )
    # The sentinel comes last, after the attribute writes and the clear.
    assert text.rstrip().endswith('console "$SENTINEL ($CHECKSUM)"'), (
        "the sentinel is echoed only after the apply, the restart and all "
        "attribute writes succeeded"
    )
    # Every success path clears the error attribute: a stale diagnostic
    # from an earlier boot must never decorate a wait that is about to
    # succeed. Three success paths exist: pre-publish, no-drift, post-apply.
    assert text.count("clear_error") >= 4, (
        "clear_error is defined once and called on all three success paths"
    )
    # The pre-publish boot: exit 0, quietly, after recording provisioned.
    assert "no TLS material published yet" in text
    assert re.search(r"exit 0", text)


def test_the_startup_script_fetches_exactly_the_published_leaves() -> None:
    """The four leaf literals are the publication flows' own constants."""
    text = _read(_STARTUP_SCRIPT)
    for leaf in _TLS_LEAVES:
        assert f"fetch_secret {leaf} " in text, (
            f"the startup script must fetch the {leaf!r} secret under its "
            "certs.py constant name, so the fetch and the publish cannot drift"
        )


def test_the_startup_script_is_posix_clean() -> None:
    """No bashisms: the guest agent's interpreter family is sh-compatible."""
    code = _read_code(_STARTUP_SCRIPT)
    for bashism in ("[[", "local ", "function ", "&>", "== "):
        assert bashism not in code, (
            f"the startup script uses the bashism {bashism!r}; it must run "
            "clean under dash and bash 3.2 alike"
        )
    assert _read(_STARTUP_SCRIPT).startswith("#!/bin/sh")


# ---------------------------------------------------------------------------
# The deploy contract: the six outputs, pinned against the Makefile.
# ---------------------------------------------------------------------------


def _makefile_text() -> str:
    return _read(_repo_root() / "Makefile")


def _deploy_required_outputs() -> list[str]:
    """The deploy flow's gcp required-output list, read from the Makefile itself.

    The Makefile carries one required list per cloud branch; the gcp one is
    the list that names `zone` -- the azure list names `vault_uri`, the EC2
    branch reads a single raw output -- and exactly one list does.
    """
    required_lists = re.findall(r"required=\[([^\]]+)\]", _makefile_text())
    assert required_lists, "the Makefile carries no required=[...] deploy lists"
    gcp_lists = [entry for entry in required_lists if "'zone'" in entry]
    assert len(gcp_lists) == 1, (
        f"expected exactly one gcp required-output list in the Makefile, found {len(gcp_lists)}"
    )
    names = re.findall(r"'([^']+)'", gcp_lists[0])
    assert names, "the Makefile's gcp required-output list parsed to nothing"
    return names


def test_the_root_outputs_carry_the_deploy_flows_required_names() -> None:
    """The module emits every name the deploy flow requires, and the two agree."""
    makefile_required = _deploy_required_outputs()
    assert makefile_required == _DEPLOY_REQUIRED_PIN, (
        "the deploy flow's required list changed; update this suite's pin and the "
        "module's outputs together"
    )
    outputs_text = _read(_GCP / "outputs.tf")
    declared = set(re.findall(r'^output "([^"]+)"', outputs_text, re.MULTILINE))
    missing = [name for name in makefile_required if name not in declared]
    assert not missing, f"provider/gcp/outputs.tf is missing the deploy-required outputs: {missing}"


def test_the_deploy_side_state_bucket_mirrors_the_state_store_composition() -> None:
    """The output's digest formula and the state store's digest agree.

    The terraform-side mirror must digest the same four components in the
    same order with the same separator, truncated to the same 8 hex
    characters, and the mirrored suffix must be the committed one.
    """
    locals_text = _read(_GCP / "remote-gcp-vm.tf")
    assert re.search(
        r'state_bucket\s+= "gdstate-\$\{var\.instance_name\}-\$'
        r'{substr\(sha256\("\$\{var\.instance_name\}\|\$\{var\.zone\}\|'
        r'\$\{var\.project_id\}\|\$\{var\.state_store_suffix\}"\), 0, 8\)}"',
        locals_text,
    ), (
        "the state_bucket local must mirror state_store_gcp's composition: "
        "gdstate-<instance>-<first 8 hex of sha256(instance|zone|project|suffix)>"
    )
    variables_text = _read(_GCP / "variables.tf")
    default_match = re.search(
        r'variable "state_store_suffix" .*?default\s+= "([a-z0-9]+)"',
        variables_text,
        re.DOTALL,
    )
    assert default_match is not None, "var.state_store_suffix declares no default"
    committed = state_store_gcp.declared_suffix(_committed_gcp_root())
    assert default_match.group(1) == committed, (
        f"the module's state_store_suffix default {default_match.group(1)!r} "
        f"drifted from the committed root's {committed!r}"
    )


# ---------------------------------------------------------------------------
# root-gcp.hcl: the committed template, composed through the production
# parser.
# ---------------------------------------------------------------------------


def _committed_gcp_root() -> str:
    return _GCP_REMOTE_ROOT.read_text(encoding="utf-8")


def test_gcp_root_composes_a_deterministic_legal_bucket_name() -> None:
    """The committed template and suffix compose the same legal name, twice.

    The same shape tests/test_azure_module.py pins for the azure root:
    composed through state_store_gcp's own parser -- the functions
    production composes the name with -- so a template or suffix edit that
    breaks gcs's naming law fails here, not at bootstrap.
    """
    instance = f"inst-{uuid.uuid4().hex[:8]}"
    zone = f"us-{uuid.uuid4().hex[:4]}1-b"
    project = f"proj-{uuid.uuid4().hex[:10]}"
    text = _committed_gcp_root()
    first = state_store_gcp.compose_from_root_hcl(text, instance, zone, project)
    second = state_store_gcp.compose_from_root_hcl(text, instance, zone, project)
    assert first == second, "the committed template must compose deterministically"
    sanitized = f"gdstate-{instance}-"
    digest = state_store_gcp.state_digest(
        instance, zone, project, state_store_gcp.declared_suffix(text)
    )
    assert first == f"{sanitized}{digest}", (
        f"the committed template composed {first!r}, not the documented "
        f"gdstate-<sanitized>-<8 hex> shape"
    )
    assert re.fullmatch(r"[a-z0-9][a-z0-9._-]{1,61}[a-z0-9]", first), (
        f"the committed template composed {first!r}, outside gcs's naming law"
    )


def test_gcp_root_declares_what_state_store_gcp_parses() -> None:
    """The two declarations the parser needs are the committed file's only naming truth."""
    text = _committed_gcp_root()
    assert state_store_gcp.declared_template(text) == "gdstate-${local.instance_name}"
    assert state_store_gcp.declared_suffix(text) == "q4m7zk"
    # And the parser reads exactly this file, by name.
    assert state_store_gcp.ROOT_GCP_HCL_FILENAME == "root-gcp.hcl"


def test_gcp_root_declares_the_version_floors_and_engine() -> None:
    """The floors the other roots declare, declared again for the gcp root."""
    text = _committed_gcp_root()
    assert 'terraform_binary = "terraform"' in text
    assert 'terraform_version_constraint  = ">= 1.10"' in text
    assert 'terragrunt_version_constraint = ">= 1.1.3"' in text
    assert 'provider "google" {}' in text


def test_gcp_root_reads_the_sidecar_the_state_store_writes() -> None:
    """The remote_state bucket name comes from the side-car, one naming authority."""
    text = _committed_gcp_root()
    assert 'backend = "gcs"' in text
    assert (
        'bucket = jsondecode(file("${get_terragrunt_dir()}/.state-store.json"))["bucket"]' in text
    )
    assert 'prefix = "${path_relative_to_include()}/terraform.tfstate"' in text
    # The side-car is written beside terragrunt.hcl, the path the root reads.
    from devcontainer_config import state_store

    assert state_store.STATE_STORE_SIDECAR_FILENAME == ".state-store.json"


def test_the_other_roots_stay_untouched_by_the_gcp_root() -> None:
    """root-gcp.hcl configures exactly the gcs backend, and only it."""
    gcp_text = _committed_gcp_root()
    s3_text = _read(_repo_root() / "remote-instances" / "root.hcl")
    azure_text = _read(_repo_root() / "remote-instances" / "root-azure.hcl")
    assert 'backend = "s3"' not in gcp_text
    assert 'backend = "azurerm"' not in gcp_text
    assert 'backend = "gcs"' not in s3_text
    assert 'backend = "gcs"' not in azure_text
    assert "state_bucket_template" not in s3_text
    assert "state_bucket_template" not in azure_text


def test_no_state_bucket_resource_exists_anywhere() -> None:
    """The state bucket is state_store_gcp's ensure, never a terraform resource."""
    for tf_file in sorted(_GCP.rglob("*.tf")):
        text = _read_code(tf_file)
        assert "google_storage_bucket" not in text, (
            f"{tf_file} declares a storage bucket; the state bucket is "
            "provisioned by state_store_gcp, and a resource here would race "
            "its ensure"
        )


# ---------------------------------------------------------------------------
# _envcommon/remote-gcp.hcl and the scaffold include.
# ---------------------------------------------------------------------------


def test_the_gcp_envcommon_sources_the_gcp_module_and_pins_the_conventions() -> None:
    text = _read(_GCP_ENVCOMMON)
    assert 'source = "${get_repo_root()}//provider/gcp"' in text
    for fragment in (
        'docker_data_root = "/mnt/docker-data"',
        'docker_daemon_user = "dockerd"',
        'docker_tls_listen_address = "0.0.0.0"',
        "docker_tls_listen_port    = 2376",
        'docker_repo_base_url      = "https://download.docker.com/linux/ubuntu"',
        'docker_repo_channel       = "stable"',
    ):
        assert fragment in text, f"the gcp envcommon is missing the convention: {fragment!r}"
    assert "find_in_parent_folders" not in text, (
        "the envcommon is included by path, never found by search"
    )


def test_the_gcp_scaffold_includes_the_gcp_root() -> None:
    """The scaffolded terragrunt.hcl targets root-gcp.hcl and the gcp envcommon.

    Rendered through the production template, not a restated string.
    """
    from devcontainer_config import instance_ops

    text = instance_ops._render_gcp_terragrunt_hcl(
        "example",
        "example",
        "example-project",
        "us-east1-b",
        "us-east1",
        "e2-standard-2",
        "10.100.0.0/16",
        "10.100.1.0/24",
    )
    assert 'path = find_in_parent_folders("root-gcp.hcl")' in text
    assert (
        'path = "${dirname(find_in_parent_folders("root-gcp.hcl"))}'
        '/_envcommon/remote-gcp.hcl"' in text
    )
    assert 'find_in_parent_folders("root.hcl")' not in text, (
        "the gcp scaffold must not target the s3 root"
    )


def test_the_committed_gcp_root_and_envcommon_exist_for_the_scaffold() -> None:
    """The files the scaffold's includes name are committed beside it."""
    assert _GCP_REMOTE_ROOT.is_file()
    assert _GCP_ENVCOMMON.is_file()
    assert (_GCP / "remote-gcp-vm.tf").is_file()
    assert (_GCP / "README.md").is_file()


# ---------------------------------------------------------------------------
# The composition: three module blocks, the azure-mirroring structure.
# ---------------------------------------------------------------------------


def test_the_root_composition_is_exactly_three_module_blocks() -> None:
    """No resource block of any kind in the root; the azure twin's composition shape."""
    text = _read(_GCP / "remote-gcp-vm.tf")
    blocks = re.findall(r"^(module|resource|data|variable|output|locals)\b", text, re.MULTILINE)
    assert blocks.count("module") == 3, f"the root declares {blocks.count('module')} modules, not 3"
    assert "resource" not in blocks, "the root owns no resource of its own"
    assert "data" not in blocks, "the root resolves nothing through a data source"
    for name in ("network", "platform", "compute"):
        assert f'module "{name}"' in text


def test_the_compute_module_depends_on_the_platform_unit() -> None:
    """The VM must not boot before the secretAccessor bindings exist."""
    text = _read(_GCP / "remote-gcp-vm.tf")
    match = re.search(r'module "compute" \{(.*?)\n\}', text, re.DOTALL)
    assert match is not None, "no compute module block found in the root composition"
    assert "depends_on = [module.platform]" in match.group(1), (
        "the compute module must depend on the platform unit: the bindings "
        "feed no output compute consumes, so without the explicit edge a "
        "first apply can boot the VM into the window where its account "
        "cannot yet read its own secrets"
    )


def test_the_platform_unit_carries_the_two_tier_secret_model() -> None:
    """Six secrets from one list, the accessor role, no project-wide grant."""
    text = _read_code(_PLATFORM_MAIN)
    for leaf in (
        certs.GCP_SERVER_KEY_SECRET,
        certs.GCP_SERVER_CERT_SECRET,
        certs.GCP_CA_SECRET,
        certs.GCP_CHECKSUM_SECRET,
        certs.GCP_SHELL_ENV_SECRET,
        certs.GCP_PROFILE_MAP_SECRET,
    ):
        assert f'"{leaf}",' in text, (
            f"the platform unit's secret list is missing the {leaf!r} leaf "
            "under its certs.py constant name"
        )
    assert "for_each = toset(local.secret_leaves)" in text
    assert '"roles/secretmanager.secretAccessor"' in text, (
        "the binding must be the accessor role and nothing wider"
    )
    assert "replication {" in text and "auto {}" in text
    assert "google_secret_manager_secret_version" not in text, (
        "the secret vehicles are created here, never their contents: the "
        "publication flows own every version"
    )


def test_every_submodule_declares_the_version_floors() -> None:
    """terraform >= 1.10 and google ~> 6.0, in the root and all three submodules."""
    for versions_file in (
        _GCP / "versions.tf",
        _GCP / "modules" / "network" / "versions.tf",
        _GCP / "modules" / "platform" / "versions.tf",
        _GCP / "modules" / "compute" / "versions.tf",
    ):
        text = _read(versions_file)
        assert 'required_version = ">= 1.10"' in text, f"{versions_file} lacks the engine floor"
        assert '"~> 6.0"' in text, f"{versions_file} lacks the google floor"
        assert 'source  = "hashicorp/google"' in text


def test_the_root_variables_carry_the_composition_validations() -> None:
    """The paired toggle validations, the naming laws, and the listener bounds."""
    text = _read(_GCP / "variables.tf")
    for fragment in (
        "var.vpc_cidr is required when var.create_network is true",
        "var.subnet_cidr is required when var.create_network is true",
        "var.subnet_id is required when var.create_network is false",
        "var.subnet_id must be left unset when var.create_network is true",
        "var.sa_email is required when var.create_platform is false",
        "var.sa_email must be left unset when var.create_platform is true",
    ):
        assert fragment in text, f"missing validation message: {fragment!r}"
    # The gcp naming laws: the instance name is an RFC1035 label of at most
    # 32 characters, the project id and zone carry the shapes the engine
    # address record's accessors enforce, and the prefix's bound is the
    # service account id law under the gd- junction.
    assert 'can(regex("^[a-z][a-z0-9-]{0,30}[a-z0-9]$", var.instance_name))' in text
    assert 'can(regex("^[a-z0-9][a-z0-9-]{5,29}$", var.project_id))' in text, (
        "the project shape must mirror instances.PROJECT_ID_PATTERN, anchored "
        "against a leading hyphen"
    )
    assert 'can(regex("^[a-z0-9][a-z0-9-]*-[a-z]$", var.zone))' in text, (
        "the zone shape must mirror instances.ZONE_PATTERN, anchored against a leading hyphen"
    )
    assert 'can(regex("^[a-z0-9-]+$", var.region))' in text
    assert 'can(regex("^[a-z][a-z0-9-]{2,26}[a-z0-9]$", var.name_prefix))' in text


def test_the_compute_module_validates_its_own_doctrine_address() -> None:
    """The gcp module validates 0.0.0.0, where the azure twin validates loopback."""
    text = _read(_GCP / "modules" / "compute" / "variables.tf")
    assert 'condition     = var.docker_tls_listen_address == "0.0.0.0"' in text, (
        "the gcp doctrine is the IAP tunnel terminating on nic0: the listener "
        "must bind the interface address, and the azure twin's loopback "
        "validation is the wrong shape to inherit"
    )
    assert "var.docker_tls_listen_port > 0 && var.docker_tls_listen_port <= 65535" in text


def test_the_root_declares_and_passes_the_marker_timeout() -> None:
    """boot_marker_timeout is a root input (terragrunt.hcl can tune it), passed
    through to the compute submodule whose startup script renders it.

    The laptop's GCP_PROVISIONED_TIMEOUT (300) is shorter than the VM's
    marker budget (600) and only ever applies to already-provisioned VMs;
    the root variable's own description carries that relationship, so a
    reader tuning one budget finds the other's terms beside it.
    """
    variables_text = _read(_GCP / "variables.tf")
    match = re.search(
        r'variable "boot_marker_timeout" .*?default\s+= 600', variables_text, re.DOTALL
    )
    assert match is not None, "the root declares boot_marker_timeout with the 600 default"
    assert "GCP_PROVISIONED_TIMEOUT" in variables_text, (
        "the root variable's description names the laptop-side budget it "
        "must not be confused with, and which of the two waits longer"
    )
    composition_text = _read_code(_GCP / "remote-gcp-vm.tf")
    assert re.search(r"boot_marker_timeout\s+= var\.boot_marker_timeout", composition_text), (
        "the root passes the marker timeout through to the compute submodule"
    )


# ---------------------------------------------------------------------------
# The module README: present, and synchronized with the input surface.
# ---------------------------------------------------------------------------


def test_the_readme_documents_every_root_input() -> None:
    """Every root variable appears in the README's input table, and vice versa."""
    variables_text = _read(_GCP / "variables.tf")
    readme = _read(_GCP / "README.md")
    declared = set(re.findall(r'^variable "([^"]+)"', variables_text, re.MULTILINE))
    # Scoped to the Inputs section: the Outputs table's rows share the
    # table syntax but name outputs, not inputs.
    inputs_section = readme.split("## Inputs", 1)[1].split("## Outputs", 1)[0]
    documented = set(re.findall(r"^\| `([a-z_]+)` \|", inputs_section, re.MULTILINE))
    missing = sorted(declared - documented)
    assert not missing, f"the README's input table is missing: {missing}"
    stale = sorted(documented - declared)
    assert not stale, f"the README's input table documents nonexistent inputs: {stale}"
