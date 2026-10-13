# Engine and provider version floors for the root composition.
#
# The >= 1.10 engine floor is the platform's engine contract, declared for
# the same reason root.hcl and provider/aws/versions.tf record it: it is
# the floor Section 6 of repos/spec/devcontainer-platform.md requires, and
# D14 (spec Section 13) records the chosen engine as Terraform. Every file
# in this module is engine-neutral HCL with no Terraform-only syntax, so an
# OpenTofu binary at the same floor is expected to plan and apply it
# identically, though that expectation has not been exercised here -- only
# Terraform has been run against this module.
#
# The azurerm floor is the 4.x series, matched by all three submodules
# rather than pinned independently: this composition consumes only
# attributes each submodule's own 4.x floor already guarantees, and the
# pessimistic constraint keeps a future breaking 5.0 series out until the
# whole composition is reviewed against it. The tls provider is required
# here because the composition passes the compute submodule through, and a
# module composing a tls-using submodule must be able to resolve that
# requirement (the root generates no tls provider configuration of its own;
# the provider's defaults are what run).
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
