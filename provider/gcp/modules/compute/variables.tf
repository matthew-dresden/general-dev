# Every option this module supports is declared here as an input. No value a
# caller might reasonably want to change is a literal inside main.tf,
# boot-user-data.yaml or boot-startup-script.sh; see Section 5.6 of
# repos/spec/devcontainer-platform.md.
#
# Deliberately absent: no CIDR of any kind, and no variable a data source
# could resolve on the caller's behalf. This module never looks up an image
# -- var.os_image names one explicitly -- and never looks up a subnetwork
# or a service account; addressing and platform belong entirely to the
# network and platform modules.

variable "instance_name" {
  description = <<-EOT
    Name of the virtual machine this module creates, already in its
    sanitized RFC1035 form (for example "devcontainer-remote-prod").
    Required, no default: the module never derives or mutates an identity
    the caller did not state. The root composition validates the RFC1035
    shape; this module re-uses the value verbatim rather than
    re-validating.
  EOT
  type        = string
}

variable "name_prefix" {
  description = <<-EOT
    Prefix that names the data disk this module creates (as
    "<prefix>-data") and that carries the network tag the IAP firewall
    targets, and the secrets prefix the startup script fetches its TLS
    material under -- the same sanitized value the publication flows mint
    their secret ids from. Required, no default: no resource name and no
    fetch path is a literal inside this module.
  EOT
  type        = string
}

variable "project_id" {
  description = <<-EOT
    GCP project id the instance and its disks are created in, and the
    project the startup script reads its secrets from. Required, no
    default: the module never picks a project on the caller's behalf, and
    the generated provider block is empty -- every resource names its
    project explicitly.
  EOT
  type        = string
}

variable "zone" {
  description = <<-EOT
    GCP zone the instance and its disks are created in (for example
    "us-east1-b"). Required, no default: the module never picks a zone on
    the caller's behalf.
  EOT
  type        = string
}

variable "subnet_id" {
  description = <<-EOT
    Identifier of the subnetwork the instance's network interface attaches
    to, in id or self-link form (for example the subnet_id output of the
    network module). Required, no default: the module never looks up a
    subnetwork on the caller's behalf, so this is an identifier input, not
    a lookup key. The module accepts any subnetwork identifier, including
    one this repository did not create.
  EOT
  type        = string
}

variable "sa_email" {
  description = <<-EOT
    Email address of the service account the instance assumes (for example
    "gd-EXAMPLE@EXAMPLE-project.iam.gserviceaccount.com"). Required, no
    default: an apply that omits this input fails at plan time naming it
    rather than launching an instance with no identity to read its secrets
    with. Typically the sa_email output of the platform module, but the
    module accepts any service account email, including one this repository
    did not create -- the caller owns whatever secretAccessor grants that
    reused account carries.
  EOT
  type        = string
}

variable "vm_size" {
  description = <<-EOT
    GCP machine type the instance is launched as (for example
    "e2-standard-2"). Required, no default: no instance size is a literal
    inside this module.
  EOT
  type        = string
}

variable "os_image" {
  description = <<-EOT
    GCP boot image the instance boots from, in project/family notation
    (for example "ubuntu-os-cloud/ubuntu-2404-lts-amd64"). Required, no
    default, and never resolved by a data source: the module does not look
    up "the latest Ubuntu image" on the caller's behalf, so a caller always
    knows exactly which image an apply will launch.
  EOT
  type        = string
}

variable "root_volume_size_gb" {
  description = <<-EOT
    Size, in GiB, of the root disk (for example 30). Required, no default:
    no disk size is a literal inside this module.
  EOT
  type        = number
}

variable "create_data_volume" {
  description = <<-EOT
    Whether to create, attach and mount a second data disk at
    var.docker_data_root, which is where the workspace clone-in-volume and
    the rootless daemon's images and containers live. Defaults to true.
    When false, no google_compute_disk and no attachment are created, and
    the rendered cloud-init skips the disk-format-and-mount step, so the
    daemon's data-root lives on the root disk instead.
  EOT
  type        = bool
  default     = true
}

variable "data_volume_size_gb" {
  description = <<-EOT
    Size, in GiB, of the data disk (for example 30). Read only when
    var.create_data_volume is true; ignored otherwise. Defaults to 30 so a
    caller enabling var.create_data_volume with its default true need not
    also size the disk explicitly.
  EOT
  type        = number
  default     = 30
}

variable "docker_daemon_user" {
  description = <<-EOT
    Name of the dedicated, unprivileged Linux user the rootless docker
    daemon runs as (for example "dockerd"). This user is never added to a
    docker group on a rootful daemon: docker group membership on a rootful
    daemon is equivalent to host root (Section 1.5), which is exactly what
    D1 (Section 13) rejects. Defaults to "dockerd". Also the account the
    startup script owns the TLS material as and restarts the daemon's unit
    under.
  EOT
  type        = string
  default     = "dockerd"
}

variable "docker_data_root" {
  description = <<-EOT
    Absolute path on the instance the rootless daemon uses as its
    data-root (for example "/mnt/docker-data"), where images, containers
    and volumes live. When var.create_data_volume is true this is also the
    mount point the rendered cloud-init formats and mounts the data disk
    at. Required, no default: the daemon's data-root is settled as an
    input, not a literal, before the cloud-init template is rendered.
  EOT
  type        = string
}

variable "docker_tls_listen_address" {
  description = <<-EOT
    IPv4 address the rootless daemon's mTLS listener binds to. Must be
    exactly "0.0.0.0": the gcp doctrine is that the IAP tunnel terminates
    on the VM's nic0, so the daemon binds the interface address -- a
    loopback bind (the azure twin's doctrine) would listen on an address
    no tunnel can dial -- and the boundary is the VPC's sole ingress rule,
    not the bind address. Defaults to "0.0.0.0".
  EOT
  type        = string
  default     = "0.0.0.0"

  validation {
    # The gcp module validates ITS doctrine, where the azure twin validates
    # loopback: the bind must be the interface wildcard, because the IAP
    # tunnel dials nic0. Anything narrower breaks the tunnel; anything
    # wider than the wildcard does not exist.
    condition     = var.docker_tls_listen_address == "0.0.0.0"
    error_message = "var.docker_tls_listen_address (${var.docker_tls_listen_address}) must be the interface address 0.0.0.0: the IAP tunnel terminates on the VM's nic0, so the daemon must bind the interface address -- the boundary is the VPC's sole ingress rule (tcp/2376 from the IAP range), not a loopback bind."
  }
}

variable "docker_tls_listen_port" {
  description = <<-EOT
    TCP port the rootless daemon's mTLS listener binds to (for example
    2376), which the IAP tunnel dials and the network module's one
    firewall rule admits. Defaults to 2376, the conventional Docker TLS
    port.
  EOT
  type        = number
  default     = 2376

  validation {
    condition     = var.docker_tls_listen_port > 0 && var.docker_tls_listen_port <= 65535
    error_message = "var.docker_tls_listen_port (${var.docker_tls_listen_port}) must be a valid TCP port between 1 and 65535."
  }
}

variable "docker_repo_base_url" {
  description = <<-EOT
    Base URL of the Docker apt repository the rendered cloud-init adds (for
    example "https://download.docker.com/linux/ubuntu"). Defaults to
    Docker's own public repository; a caller mirroring third-party
    packages internally overrides this rather than the template carrying a
    literal upstream URL.
  EOT
  type        = string
  default     = "https://download.docker.com/linux/ubuntu"
}

variable "docker_repo_channel" {
  description = <<-EOT
    Release channel of the Docker apt repository the rendered cloud-init
    adds (for example "stable"). Defaults to "stable".
  EOT
  type        = string
  default     = "stable"
}

variable "tags" {
  description = <<-EOT
    Common labels merged onto the instance and its data disk, in addition
    to the resource names themselves. Optional; defaults to an empty map
    when the caller has no additional labels to apply. gcp label law
    (lowercase letters, digits, underscores and dashes) is enforced by the
    root composition's validation on var.tags.
  EOT
  type        = map(string)
  default     = {}
}
