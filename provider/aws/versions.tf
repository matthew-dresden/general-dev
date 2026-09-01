# Engine and provider version floors for the root composition.
#
# The >= 1.10 engine floor is required by Section 6 of
# repos/spec/devcontainer-platform.md: native S3 state locking through
# `use_lockfile` arrives at that level. The cross-variable validation blocks
# in variables.tf (a validation condition referencing a sibling variable,
# for example var.create_network from inside var.vpc_id's validation block)
# only need engine 1.9 or later, so the 1.10 floor already satisfies that
# requirement without being the reason for it. D14 (spec Section 13) records
# the chosen engine as Terraform; every file in this module is
# engine-neutral HCL with no Terraform-only syntax, so an OpenTofu binary at
# the same floor is expected to plan and apply it identically, though that
# expectation has not been exercised here -- only Terraform has been run
# against this module (see TDD Cycle Log).
#
# The aws provider floor is 6.0, matching the security submodule rather than
# the 5.0 floor the network and compute submodules declare: this module's
# own data.aws_region.current.region reference (outputs.tf) is only exposed
# by provider 6.0 onward (5.x exposes name/endpoint/description, not
# region), and a module composing a 6.0-only submodule cannot itself declare
# a looser floor than its own submodule requires.
terraform {
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 6.0, < 7.0"
    }
  }
}
