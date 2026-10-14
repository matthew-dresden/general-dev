# Every option this module supports is declared here as an input. No value a
# caller might reasonably want to change is a literal inside main.tf; see
# Section 5.6 of repos/spec/devcontainer-platform.md.

variable "name_prefix" {
  description = <<-EOT
    Prefix used to name the virtual network, subnetwork, router, NAT
    gateway and firewall this module creates (for example
    "devcontainer-remote-prod"). Required, no default: no resource name is
    a literal inside this module. Must satisfy the RFC1035 law the root
    composition validates on var.name_prefix; this module re-uses the value
    verbatim rather than re-validating.
  EOT
  type        = string
}

variable "project_id" {
  description = <<-EOT
    GCP project id the network, subnetwork, router, NAT gateway and
    firewall are created in. Required, no default: the module never picks
    a project on the caller's behalf, and the generated provider block is
    empty -- every resource names its project explicitly.
  EOT
  type        = string
}

variable "region" {
  description = <<-EOT
    GCP region the subnetwork, router and NAT gateway are created in (for
    example "us-east1"). Required, no default: the module never picks a
    region on the caller's behalf.
  EOT
  type        = string
}

variable "vpc_cidr" {
  description = <<-EOT
    IPv4 CIDR block for the virtual network, in address/prefix-length
    notation (for example "10.100.0.0/16"). Required, no default: the
    module never picks an address range on the caller's behalf.
  EOT
  type        = string
}

variable "subnet_cidr" {
  description = <<-EOT
    IPv4 CIDR block for the subnetwork, in address/prefix-length notation
    (for example "10.100.1.0/24"). Required, no default. Must be fully
    contained inside var.vpc_cidr; a value that is not fails at plan time
    naming both CIDRs.
  EOT
  type        = string

  validation {
    # Containment, not overlap: the subnetwork's prefix must be at least as
    # specific as the network's (same size or smaller), and the
    # subnetwork's network address, re-masked at the network's prefix
    # length, must land on the network's own network address.
    # cidrhost(prefix, 0) always returns the network base address for the
    # given prefix length, discarding any host bits in the address
    # supplied, which is what makes the re-mask step exact. The same
    # containment proof provider/azure/modules/network applies to its own
    # subnet_cidr.
    condition = (
      tonumber(split("/", var.subnet_cidr)[1]) >= tonumber(split("/", var.vpc_cidr)[1]) &&
      cidrhost(
        "${cidrhost(var.subnet_cidr, 0)}/${split("/", var.vpc_cidr)[1]}",
        0,
      ) == cidrhost(var.vpc_cidr, 0)
    )
    error_message = "var.subnet_cidr (${var.subnet_cidr}) must be fully contained inside var.vpc_cidr (${var.vpc_cidr})."
  }
}

variable "instance_tag" {
  description = <<-EOT
    Network tag the IAP firewall rule targets (for example
    "devcontainer-remote-prod"), the same tag the compute module puts on
    the instance's network interface. Required, no default: the rule must
    target exactly the instances this composition creates, and the pairing
    is the root composition's one composition of the value.
  EOT
  type        = string
}

variable "docker_tls_listen_port" {
  description = <<-EOT
    TCP port the firewall's one rule admits from the IAP range (for
    example 2376), the same port the daemon's mTLS listener binds. Required,
    no default: the rule and the listener cannot be allowed to disagree,
    and neither side owns the value.
  EOT
  type        = number

  validation {
    condition     = var.docker_tls_listen_port > 0 && var.docker_tls_listen_port <= 65535
    error_message = "var.docker_tls_listen_port (${var.docker_tls_listen_port}) must be a valid TCP port between 1 and 65535."
  }
}

# Deliberately absent: no tags/labels input --
# this module's resources carry no labels: gcp network resources expose
# none -- google_compute_network, google_compute_subnetwork,
# google_compute_router, google_compute_router_nat and
# google_compute_firewall all have no labels attribute. A labels variable
# here would be accepted and silently dropped at plan time, so the module
# refuses to declare one; the composition's labels ride on the instance,
# its data disk and the platform unit's secrets, which do expose them.
