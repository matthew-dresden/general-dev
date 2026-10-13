# Every option this module supports is declared here as an input. No value a
# caller might reasonably want to change is a literal inside main.tf; see
# Section 5.6 of repos/spec/devcontainer-platform.md.

variable "location" {
  description = <<-EOT
    Azure region the network security group, the identity, the vault and
    the relay namespace are created in (for example "westeurope").
    Required, no default: the module never picks a region on the caller's
    behalf.
  EOT
  type        = string
}

variable "resource_group_name" {
  description = <<-EOT
    Name of the resource group every resource this module creates is
    deployed into. Required, no default: the module never creates or looks
    up a resource group on the caller's behalf, so this is an identifier
    input, not a lookup key. Typically the resource_group_name output of
    the network module, but the module accepts any resource group name,
    including one this repository did not create.
  EOT
  type        = string
}

variable "subnet_id" {
  description = <<-EOT
    Identifier of the subnet the zero-inbound network security group is
    associated with. Required, no default: the module never looks up a
    subnet on the caller's behalf, so this is an identifier input, not a
    lookup key. Typically the subnet_id output of the network module, but
    the module accepts any subnet identifier, including one this repository
    did not create.
  EOT
  type        = string
}

variable "instance_name" {
  description = <<-EOT
    Name of the instance this platform unit is provisioned for (for
    example "devcontainer-remote-prod"). Names the hybrid connection the
    relay couple meets on, keyed to this one instance so two deployments
    sharing a namespace never couple across instances. Required, no
    default: no instance name is a literal inside this module.
  EOT
  type        = string

  validation {
    # The hybrid connection this module creates is the junction
    # "gd-" + var.instance_name, and a hybrid connection name must end in
    # an alphanumeric character; a trailing dash on the instance name
    # would leave azure refusing the connection's own creation at apply.
    # Refusing it here fails the plan naming the junction instead.
    condition     = !can(regex("-$", var.instance_name))
    error_message = "var.instance_name (${var.instance_name}) must not end with a dash: the hybrid connection name is the junction \"gd-${var.instance_name}\" and a relay hybrid connection name must end in an alphanumeric character."
  }
}

variable "name_prefix" {
  description = <<-EOT
    Prefix used to name the network security group, the user-assigned
    identity, the Key Vault and the relay namespace this module creates
    (for example "devcontainer-remote-prod"). Vault and namespace names are
    globally unique in azure, not scoped to a resource group or a region,
    so this value must be unique across every deployment of this module in
    the same tenant or the later deployment's apply fails on a naming
    collision with the earlier one. Required, no default: no resource name
    is a literal inside this module. Must satisfy the azure resource-naming
    law the root composition validates on var.name_prefix.
  EOT
  type        = string
}

variable "relay_sender_object_ids" {
  description = <<-EOT
    Entra object ids of the principals granted the Azure Relay Sender role
    on the namespace this module creates -- the humans and pipelines
    allowed to open the sender half of the couple. Type list(string),
    defaults to an empty list: an empty list creates no grant at all, and a
    principal not listed cannot open a sender connection even with the
    namespace's name.
  EOT
  type        = list(string)
  default     = []
}

variable "keyvault_officer_object_ids" {
  description = <<-EOT
    Entra object ids of the principals granted the Key Vault Secrets
    Officer role on the vault this module creates -- the operators who
    publish the instance's TLS material and secrets into it. Type
    list(string), defaults to an empty list: an empty list creates no grant
    at all, and a principal not listed can neither read nor write the
    vault's secrets through this assignment (the VM identity's own grant is
    the read-only Secrets User role, declared above independently of this
    list).
  EOT
  type        = list(string)
  default     = []
}

variable "tags" {
  description = <<-EOT
    Common resource tags merged onto every resource this module creates,
    in addition to each resource's own Name tag. Optional; defaults to an
    empty map when the caller has no additional tags to apply.
  EOT
  type        = map(string)
  default     = {}
}
