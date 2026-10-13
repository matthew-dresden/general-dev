# Engine and provider version floors for the compute module.
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
# submodules declare, for the same reason: every resource here (the Linux
# VM, its network interface, the managed data disk and its attachment) and
# every argument referenced is available across the whole 4.x series. The
# tls floor is the 4.x series of hashicorp/tls, the current major at the
# time this module was written: the module uses exactly one tls resource
# (the inert bootstrap key), whose ED25519 support and
# public_key_openssh attribute are stable across that series.
terraform {
  required_version = ">= 1.10"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.0"
    }
    tls = {
      source  = "hashicorp/tls"
      version = "~> 4.0"
    }
  }
}
