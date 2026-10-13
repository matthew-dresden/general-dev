# Every option this module supports is declared here as an input. No value a
# caller might reasonably want to change is a literal inside main.tf; see
# Section 5.6 of repos/spec/devcontainer-platform.md.

variable "location" {
  description = <<-EOT
    Azure region the resource group, virtual network and subnet are created
    in (for example "westeurope"). Required, no default: the module never
    picks a region on the caller's behalf.
  EOT
  type        = string
}

variable "vnet_cidr" {
  description = <<-EOT
    IPv4 CIDR block for the virtual network, in address/prefix-length
    notation (for example "10.100.0.0/16"). Required, no default: the
    module never picks an address range on the caller's behalf.
  EOT
  type        = string
}

variable "subnet_cidr" {
  description = <<-EOT
    IPv4 CIDR block for the subnet, in address/prefix-length notation (for
    example "10.100.1.0/24"). Required, no default. Must be fully contained
    inside var.vnet_cidr; a value that is not fails at plan time naming
    both CIDRs.
  EOT
  type        = string

  validation {
    # Containment, not overlap: the subnet's prefix must be at least as
    # specific as the network's (same size or smaller), and the subnet's
    # network address, re-masked at the network's prefix length, must land
    # on the network's own network address. cidrhost(prefix, 0) always
    # returns the network base address for the given prefix length,
    # discarding any host bits in the address supplied, which is what makes
    # the re-mask step exact. The same containment proof
    # provider/aws/modules/network applies to its own subnet_cidr.
    condition = (
      tonumber(split("/", var.subnet_cidr)[1]) >= tonumber(split("/", var.vnet_cidr)[1]) &&
      cidrhost(
        "${cidrhost(var.subnet_cidr, 0)}/${split("/", var.vnet_cidr)[1]}",
        0,
      ) == cidrhost(var.vnet_cidr, 0)
    )
    error_message = "var.subnet_cidr (${var.subnet_cidr}) must be fully contained inside var.vnet_cidr (${var.vnet_cidr})."
  }
}

variable "name_prefix" {
  description = <<-EOT
    Prefix used to name the resource group, the virtual network and the
    subnet this module creates (for example "devcontainer-remote-prod").
    Required, no default: no resource name is a literal inside this module.
    Must satisfy the azure resource-naming law the root composition
    validates on var.name_prefix (lowercase alphanumeric characters and
    dashes); this module re-uses the value verbatim rather than re-validating.
  EOT
  type        = string
}

variable "tags" {
  description = <<-EOT
    Common resource tags merged onto the resource group, the virtual
    network and the subnet this module creates. Optional; defaults to an
    empty map when the caller has no additional tags to apply.
  EOT
  type        = map(string)
  default     = {}
}
