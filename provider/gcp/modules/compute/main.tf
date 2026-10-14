# Compute module: the instance itself, its root and data disks, and the two
# boot payloads that turn a bare Ubuntu host into the rootless docker
# engine this program talks to. The gcp twin of
# provider/azure/modules/compute, mirroring its structure and its comment
# discipline. See Section 5.6 of repos/spec/devcontainer-platform.md.
#
# The two payloads ride SEPARATE metadata keys, and the split is the
# design: `user-data` (the rendered cloud-init, plain text: GCE's
# datasource does not decode base64) runs ONCE at first
# boot and builds the engine; `startup-script` (plain text -- the guest
# agent writes and executes it, and it sits far under the 256 KiB metadata
# value cap) runs at EVERY boot and applies the TLS material from the
# instance's secrets. Neither payload embeds the other, and the cloud-init
# embeds nothing base64-encoded: its one embedded artifact pattern is the
# azure twin's, and the gcp composition has no listener script or unit to
# embed -- the transport is google's IAP front end.
#
# Deliberately absent, by design rather than by omission: no CIDR input,
# no data source looking up an image or a subnetwork, and no access_config
# of any kind -- the network interface is never given an external address,
# which is the whole ingress doctrine: the instance is addressable only
# through the IAP tunnel (the transport_gcp module's wire contract),
# admitted by the network module's one firewall rule. The instance reaches
# the internet outbound (package installs, image pulls) through the Cloud
# NAT the network module declares, and nothing else.
#
# allow_stopping_for_update is set true, and that is a doctrine note, not
# a convenience: the metadata this module carries is the boot payloads'
# rendering, and a payload edit is a custom_data-class change on azure --
# a replacement, a new VM. gcp updates instance metadata in place under
# this flag, and in place is safe here because both boot scripts are
# idempotent (cloud-init re-runs only at first boot and its steps are
# guarded; the startup script's drift logic applies only what changed),
# while a material change that must actually reach the running daemon is
# driven by the cert-install flow's reboot -- the boot-applied install's
# contract, not aterraform one.

locals {
  # The network tag the instance carries: the same value the root
  # composition handed the network module's firewall as its target, so the
  # one rule and the one tagged interface cannot drift apart.
  instance_tag = var.name_prefix

  # The data disk's name, composed once: the disk resource below is created
  # under it, and the rendered cloud-init locates the attached device at
  # /dev/disk/by-id/google-<this name> -- gce derives that by-id handle
  # from the disk's own name, so the template and the resource cannot
  # disagree.
  data_disk_name = "${var.name_prefix}-data"
}

resource "google_compute_instance" "this" {
  name         = var.instance_name
  machine_type = var.vm_size
  zone         = var.zone
  project      = var.project_id

  # pd-balanced rather than pd-ssd: the azure composition picked
  # StandardSSD for its root disk as the cost-appropriate general-purpose
  # tier, and pd-balanced is that tier on gcp. The root disk carries the
  # OS and the rendered cloud-init; everything the daemon writes lives on
  # the data disk below.
  boot_disk {
    initialize_params {
      image = var.os_image
      size  = var.root_volume_size_gb
      type  = "pd-balanced"
    }
  }

  # The instance's network interface: the subnetwork identifier only, and
  # deliberately NO access_config block. gcp assigns an external address
  # to an interface exactly when an access_config is declared, so the
  # absence IS the no-external-address doctrine -- there is no flag left
  # at "false" to flip back. Inbound reachability is the IAP tunnel's,
  # through the network module's one rule; outbound reachability is Cloud
  # NAT's.
  network_interface {
    subnetwork = var.subnet_id
  }

  # The metadata surface: guest attributes enabled (the startup script's
  # checksum, provisioned and error attributes are the boot-applied
  # install's whole channel -- without this flag the guest-attribute API
  # answers nothing), the rendered cloud-init (plain text: GCE's
  # datasource hands it over undecoded, so base64 arrives as gibberish
  # cloud-init ignores), the plain-text startup script (see this file's
  # header for the split), and google's own logging switch.
  metadata = {
    enable-guest-attributes = "TRUE"
    google-logging-enabled  = "true"
    user-data = templatefile("${path.module}/boot-user-data.yaml", {
      daemon_user          = var.docker_daemon_user
      data_root            = var.docker_data_root
      tls_listen_address   = var.docker_tls_listen_address
      tls_listen_port      = var.docker_tls_listen_port
      create_data_volume   = var.create_data_volume
      docker_repo_base_url = var.docker_repo_base_url
      docker_repo_channel  = var.docker_repo_channel
      data_disk_name       = local.data_disk_name
    })
    startup-script = templatefile("${path.module}/boot-startup-script.sh", {
      secrets_prefix         = var.name_prefix
      project_id             = var.project_id
      daemon_user            = var.docker_daemon_user
      marker_timeout_default = var.boot_marker_timeout
    })
  }

  # The instance runs as the platform unit's service account. The scopes
  # list is immutable at create time -- it cannot be narrowed on a running
  # VM -- so it is stated at the widest step the account's grants actually
  # need, and least privilege is enforced where it is enforceable: by the
  # platform unit's per-secret secretAccessor bindings, which let this
  # account read exactly its own six secrets and nothing else in the
  # project. cloud-platform is the scope the Secret Manager REST calls
  # require; no narrower published scope covers them.
  service_account {
    email  = var.sa_email
    scopes = ["cloud-platform"]
  }

  # The network tag the firewall targets, and nothing else: tags shape
  # firewall reachability, and this instance's reachability is the one
  # rule's. Resource labels (the tags variable) ride separately below.
  tags = [local.instance_tag]

  labels = var.tags

  allow_stopping_for_update = true
}

# The data disk and its attachment exist only when the caller opts in; a
# deployment that wants root-disk-only storage gets exactly that, with no
# google_compute_disk or attachment declared at all rather than one that is
# merely unused. The disk's name is the one local the rendered cloud-init
# composes its by-id handle from (see the locals above).
resource "google_compute_disk" "data" {
  count = var.create_data_volume ? 1 : 0

  name    = local.data_disk_name
  project = var.project_id
  zone    = var.zone
  size    = var.data_volume_size_gb
  type    = "pd-balanced"
}

resource "google_compute_attached_disk" "data" {
  count = var.create_data_volume ? 1 : 0

  disk     = google_compute_disk.data[0].id
  instance = google_compute_instance.this.id
  project  = var.project_id
  zone     = var.zone

  # READ_WRITE: the disk is the daemon's exclusive data-root, mounted
  # read-write by the rendered cloud-init. No LUN is pinned -- gcp assigns
  # SCSI LUNs itself and the provider exposes no argument for it -- and
  # none is needed: the rendered cloud-init locates the disk through the
  # deterministic by-id handle the disk's own name derives, not through an
  # attachment-slot path. No provisioned_iops is declared: pd-balanced
  # provisions its own performance profile, and pinning IOPS is a pd-ssd
  # concern.
  mode = "READ_WRITE"
}
