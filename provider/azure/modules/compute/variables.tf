# Every option this module supports is declared here as an input. No value a
# caller might reasonably want to change is a literal inside main.tf or
# cloud-init.yaml; see Section 5.6 of repos/spec/devcontainer-platform.md.
#
# Deliberately absent: no CIDR of any kind, and no variable a data source
# could resolve on the caller's behalf. This module never looks up an image
# -- var.os_image_urn names one explicitly -- and never looks up a subnet,
# a resource group or an identity; addressing and platform belong entirely
# to the network and security modules.

variable "location" {
  description = <<-EOT
    Azure region the instance, its network interface and its data disk are
    created in (for example "westeurope"). Required, no default: the module
    never picks a region on the caller's behalf.
  EOT
  type        = string
}

variable "resource_group_name" {
  description = <<-EOT
    Name of the resource group the instance, its network interface and its
    data disk are deployed into. Required, no default: the module never
    creates or looks up a resource group on the caller's behalf. Typically
    the resource_group_name output of the network module.
  EOT
  type        = string
}

variable "subnet_id" {
  description = <<-EOT
    Identifier of the subnet the instance's network interface attaches to
    (for example a subnet resource id). Required, no default: the module
    never looks up a subnet on the caller's behalf, so this is an
    identifier input, not a lookup key. Typically the subnet_id output of
    the network module, but the module accepts any subnet identifier,
    including one this repository did not create.
  EOT
  type        = string
}

variable "identity_id" {
  description = <<-EOT
    Identifier of the user-assigned managed identity attached to the
    instance. Required, no default: the module never creates or looks up
    an identity on the caller's behalf, so an apply that omits this input
    fails at plan time naming it rather than launching an instance with no
    identity to reach its vault and relay namespace with. Typically the
    identity_id output of the security module.
  EOT
  type        = string
}

variable "name_prefix" {
  description = <<-EOT
    Prefix applied to the Name tag of the instance, its network interface
    and its data disk, and used to name the instance itself (for example
    "devcontainer-remote-prod"). Required, no default: no resource name is
    a literal inside this module.
  EOT
  type        = string
}

variable "vm_size" {
  description = <<-EOT
    Azure VM size the instance is launched as (for example
    "Standard_D2pls_v5"). Required, no default: no instance size is a
    literal inside this module. The size's architecture must match the
    image var.os_image_urn names -- the documented arm64 default URN pairs
    with an arm64-capable size, and an x64 size needs the x64 server URN.
  EOT
  type        = string
}

variable "os_image_urn" {
  description = <<-EOT
    Azure image URN the instance boots from, in the platform's
    Publisher:Offer:Sku:Version notation (for example
    "Canonical:ubuntu-24_04-lts:server-arm64:latest"). Required, no
    default, and never resolved by a data source: the module does not look
    up "the latest Ubuntu image" on the caller's behalf, so a caller always
    knows exactly which image an apply will launch. The documented default
    in the scaffold pairs the arm64 server sku with an arm64-capable VM
    size; an x64 size needs the x64 server sku.
  EOT
  type        = string

  validation {
    # A URN is exactly four colon-separated parts; anything else is a
    # caller typo that azure would otherwise report as an opaque image
    # error at apply time.
    condition     = length(split(":", var.os_image_urn)) == 4
    error_message = "var.os_image_urn (${var.os_image_urn}) must be a four-part azure image URN in Publisher:Offer:Sku:Version notation."
  }
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
    When false, no azurerm_managed_disk and no attachment are created, and
    the rendered cloud-init skips the disk-format-and-mount step, so the
    daemon's data-root lives on the root disk instead.
  EOT
  type        = bool
  default     = true
}

variable "data_volume_size_gb" {
  description = <<-EOT
    Size, in GiB, of the data disk (for example 100). Read only when
    var.create_data_volume is true; ignored otherwise. Defaults to 100 so a
    caller enabling var.create_data_volume with its default true need not
    also size the disk explicitly.
  EOT
  type        = number
  default     = 100
}

variable "docker_daemon_user" {
  description = <<-EOT
    Name of the dedicated, unprivileged Linux user the rootless docker
    daemon runs as (for example "dockerd"). This user is never added to a
    docker group on a rootful daemon: docker group membership on a rootful
    daemon is equivalent to host root (Section 1.5), which is exactly what
    D1 (Section 13) rejects. Defaults to "dockerd".
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
    Loopback IPv4 address the rootless daemon's mTLS listener binds to (for
    example "127.0.0.1"). Must be the loopback address: the relay couple
    reaches the daemon only through the listener's loopback bridge on the
    VM, and the generated server certificate's SAN is IP:127.0.0.1
    (Section 5.5), so any other address both breaks certificate validation
    and removes the loopback boundary the relay bridge depends on.
    Defaults to "127.0.0.1".
  EOT
  type        = string
  default     = "127.0.0.1"

  validation {
    condition     = var.docker_tls_listen_address == "127.0.0.1"
    error_message = "var.docker_tls_listen_address (${var.docker_tls_listen_address}) must be the loopback address 127.0.0.1: a daemon listener reachable off loopback removes the boundary the relay bridge depends on, and would not match the server certificate's IP:127.0.0.1 SAN (spec Section 5.5)."
  }
}

variable "docker_tls_listen_port" {
  description = <<-EOT
    TCP port the rootless daemon's mTLS listener binds to on loopback (for
    example 2376), which the relay listener's bridge targets and nothing
    else reaches. Defaults to 2376, the conventional Docker TLS port.
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

variable "relay_namespace" {
  description = <<-EOT
    Fully qualified host name of the relay namespace the listener unit's
    environment file carries (for example
    "gd-example-relay.servicebus.windows.net"). Required, no default: the
    listener cannot dial its control channel without it. Typically the
    relay_namespace output of the security module via the root composition.
  EOT
  type        = string
}

variable "hybrid_connection" {
  description = <<-EOT
    Name of the hybrid connection the listener unit's environment file
    carries (for example "gd-devcontainer-remote-prod"), the rendezvous
    point the relay couple meets on. Required, no default. Typically the
    hybrid_connection output of the security module via the root
    composition.
  EOT
  type        = string
}

variable "tags" {
  description = <<-EOT
    Common resource tags merged onto the instance, its network interface
    and its data disk, in addition to each resource's own Name tag.
    Optional; defaults to an empty map when the caller has no additional
    tags to apply.
  EOT
  type        = map(string)
  default     = {}
}
