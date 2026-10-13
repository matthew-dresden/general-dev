# Engine and provider version floors for the network module.
#
# The >= 1.10 engine floor is the platform's engine contract, declared for
# the same reason provider/aws/modules/network/versions.tf records: it is
# the floor Section 6 of repos/spec/devcontainer-platform.md requires, and
# per-backend files keep it rather than restating a different one. D14
# (spec Section 13) records the chosen engine as Terraform; this module is
# written in engine-neutral HCL, so an OpenTofu binary at the same floor is
# expected to plan and apply it identically, though that expectation has
# not been exercised here -- only Terraform has been run against this
# module.
#
# The azurerm floor is the 4.x series the root composition and its sibling
# submodules declare: the resources here (resource group, virtual network,
# subnet) and every argument referenced are available across the whole 4.x
# series, and the ~> 4.0 pessimistic constraint keeps a future 5.0 series
# with breaking resource-schema changes out until this module is reviewed
# against it.
terraform {
  required_version = ">= 1.10"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.0"
    }
  }
}
