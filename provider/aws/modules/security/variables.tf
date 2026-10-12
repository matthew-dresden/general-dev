# Every option this module supports is declared here as an input. No value a
# caller might reasonably want to change is a literal inside main.tf; see
# Section 5.6 of repos/spec/devcontainer-platform.md.

variable "vpc_id" {
  description = <<-EOT
    Identifier of the VPC the security group is attached to (for example
    "vpc-0123456789abcdef0"). Required, no default: the module never looks
    up a VPC on the caller's behalf, so this is an identifier input, not a
    lookup key. Typically the vpc_id output of the network module (E5-F1-S2),
    but the module accepts any VPC identifier, including one this repository
    did not create.
  EOT
  type        = string
}

variable "instance_name" {
  description = <<-EOT
    Name of the instance this security module is provisioned for (for
    example "devcontainer-remote-prod"). Keys the instance-scoped tier of
    the two-tier secret model in Section 5.3 of
    repos/spec/devcontainer-platform.md, "/devcontainer/<instance>/*",
    which the inline IAM policy below scopes read access to alongside the
    shared "/devcontainer/shared/*" tier. Required, no default: no instance
    name is a literal inside this module, and an instance's own secrets are
    reachable only through its own name.
  EOT
  type        = string
}

variable "egress_cidr_blocks" {
  description = <<-EOT
    IPv4 CIDR blocks the security group's single egress rule permits traffic
    to (for example ["0.0.0.0/0"]). Required, no default and no literal
    address range inside this module, so an account with restricted egress
    can narrow this list without editing the module; see Section 5.6.
    Narrowing this list below what the instance's outbound paths need
    (the SSM endpoints, package repositories and the container registry)
    costs those paths their connectivity, so narrow deliberately and test
    the result.
  EOT
  type        = list(string)

  validation {
    condition     = length(var.egress_cidr_blocks) > 0
    error_message = "var.egress_cidr_blocks must not be empty: with no egress destination declared, the instance could not reach SSM, install packages or pull container images."
  }
}

variable "name_prefix" {
  description = <<-EOT
    Prefix used to name the IAM role, its inline Parameter Store policy and
    the instance profile this module creates, and applied to the Name tag
    of the security group (for example "devcontainer-remote-prod"). IAM
    role, policy and instance-profile names are account-global, not scoped
    to a region or a VPC, so this value must be unique across every
    deployment of this module in the same AWS account or the later
    deployment's apply fails on a naming collision with the earlier one.
    Required, no default: no resource name is a literal inside this module.
  EOT
  type        = string
}

variable "tags" {
  description = <<-EOT
    Common resource tags merged onto the security group, the IAM role and
    the instance profile this module creates, in addition to each
    resource's own Name tag. Optional; defaults to an empty map when the
    caller has no additional tags to apply.
  EOT
  type        = map(string)
  default     = {}
}
