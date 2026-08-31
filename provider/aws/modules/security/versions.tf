# Engine and provider version floors for the security module.
#
# The >= 1.10 engine floor is required by Section 6 of
# repos/spec/devcontainer-platform.md: native S3 state locking through
# `use_lockfile` arrives at that level. D14 (spec Section 13) records the
# chosen engine as Terraform; this module is written in engine-neutral HCL,
# so an OpenTofu binary at the same floor is expected to plan and apply it
# identically, though that expectation has not been exercised here -- only
# Terraform has been run against this module (see TDD Cycle Log).
#
# The aws provider floor is 6.0, not 5.0 like the sibling network module:
# main.tf's inline Parameter Store policy reads
# data.aws_region.current.region, an attribute the aws_region data source
# only exposes from provider 6.0 onward (5.x exposes name/endpoint/
# description, not region). A 5.x provider fails validate/plan on that
# attribute reference, so the floor must match the attribute actually used.
terraform {
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 6.0, < 7.0"
    }
  }
}
