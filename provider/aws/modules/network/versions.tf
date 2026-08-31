# Engine and provider version floors for the network module.
#
# The >= 1.10 engine floor is required by Section 6 of
# repos/spec/devcontainer-platform.md: native S3 state locking through
# `use_lockfile` arrives at that level. D14 (spec Section 13) records the
# chosen engine as Terraform; this module is written in engine-neutral HCL,
# so an OpenTofu binary at the same floor is expected to plan and apply it
# identically, though that expectation has not been exercised here -- only
# Terraform has been run against this module (see TDD Cycle Log).
terraform {
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0, < 7.0"
    }
  }
}
