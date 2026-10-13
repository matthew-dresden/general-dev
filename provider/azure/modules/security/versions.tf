# Engine and provider version floors for the security module.
#
# The >= 1.10 engine floor is the platform's engine contract, declared for
# the same reason provider/aws/modules/network/versions.tf records. D14
# (spec Section 13) records the chosen engine as Terraform; this module is
# written in engine-neutral HCL, so an OpenTofu binary at the same floor is
# expected to plan and apply it identically, though that expectation has
# not been exercised here -- only Terraform has been run against this
# module.
#
# The azurerm floor is the 4.x series the root composition and its sibling
# submodules declare, for the same reason: every resource here (network
# security group, subnet association, user-assigned identity, Key Vault,
# relay namespace, relay hybrid connection, role assignment) and every
# argument referenced is available across the whole 4.x series, and the
# pessimistic constraint keeps a future breaking 5.0 series out until this
# module is reviewed against it.
terraform {
  required_version = ">= 1.10"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.0"
    }
  }
}
