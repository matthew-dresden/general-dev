# Engine and provider version floors for the root composition.
#
# The >= 1.10 engine floor is the platform's engine contract, declared for
# the same reason root.hcl and provider/azure/versions.tf record it: it is
# the floor Section 6 of repos/spec/devcontainer-platform.md requires, and
# D14 (spec Section 13) records the chosen engine as Terraform. Every file
# in this module is engine-neutral HCL with no Terraform-only syntax, so an
# OpenTofu binary at the same floor is expected to plan and apply it
# identically, though that expectation has not been exercised here -- only
# Terraform has been run against this module.
#
# The google floor is the 6.x series, matched by all three submodules rather
# than pinned independently: this composition consumes only attributes each
# submodule's own 6.x floor already guarantees, and the pessimistic
# constraint keeps a future breaking 7.0 series out until the whole
# composition is reviewed against it. No auxiliary provider is required
# here: the composition creates no certificate (the trust chain is the
# cert-publish flow's, minted on the laptop), no random value (the state
# bucket's digest is composed in state_store_gcp, and the deploy-side
# mirror of it is plain sha256) and no template beyond terraform's own
# templatefile.
terraform {
  required_version = ">= 1.10"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }
}
