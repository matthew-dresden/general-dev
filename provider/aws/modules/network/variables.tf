# Every option this module supports is declared here as an input. No value a
# caller might reasonably want to change is a literal inside main.tf; see
# Section 5.6 of repos/spec/devcontainer-platform.md.

variable "vpc_cidr" {
  description = <<-EOT
    IPv4 CIDR block for the VPC, in address/prefix-length notation
    (for example "10.0.0.0/16"). Required, no default: the module never
    picks an address range on the caller's behalf.
  EOT
  type        = string
}

variable "subnet_cidr" {
  description = <<-EOT
    IPv4 CIDR block for the public subnet, in address/prefix-length notation
    (for example "10.0.1.0/24"). Required, no default. Must be fully
    contained inside var.vpc_cidr; a value that is not fails at plan time
    naming both CIDRs.
  EOT
  type        = string

  validation {
    # Containment, not overlap: the subnet's prefix must be at least as
    # specific as the VPC's (same size or smaller), and the subnet's network
    # address, re-masked at the VPC's prefix length, must land on the VPC's
    # own network address. cidrhost(prefix, 0) always returns the network
    # base address for the given prefix length, discarding any host bits in
    # the address supplied, which is what makes the re-mask step exact.
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

variable "availability_zone" {
  description = <<-EOT
    AWS availability zone the public subnet is created in (for example
    "us-east-1a"). Required, no default: the module never selects a zone
    on the caller's behalf.
  EOT
  type        = string
}

variable "name_prefix" {
  description = <<-EOT
    Prefix applied to the Name tag of the VPC, subnet, internet gateway and
    route table this module creates (for example "devcontainer-remote-prod").
    The route table association carries no tags and is not taggable, so it
    is not affected by this value. Required, no default: no resource name is
    a literal inside this module.
  EOT
  type        = string
}

variable "tags" {
  description = <<-EOT
    Common resource tags merged onto the VPC, subnet, internet gateway and
    route table this module creates, in addition to each resource's own Name
    tag. The route table association carries no tags block and cannot be
    tagged, so it is not affected by this value. Optional; defaults to an
    empty map when the caller has no additional tags to apply.
  EOT
  type        = map(string)
  default     = {}
}
