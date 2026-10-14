# Included by every gcp instance directory under remote-instances/.
# Configures the remote state backend, generates the google provider block,
# and declares the tool version floors Section 6 requires.
#
# This is the gcp twin of root-azure.hcl, and it exists as a separate root
# rather than an extension of either earlier root for one structural reason:
# the gcs backend shares no shape with the s3 or azurerm ones -- no bucket
# name template of theirs, no region, no account id, no storage account --
# so a merged root would carry three backends' config shapes through one
# file, and every gcp instance would parse (and every reader would reason
# about) two backends it never runs. One root per cloud keeps each cloud's
# backend byte-honest: root.hcl configures exactly the s3 backend and stays
# byte-identical, root-azure.hcl configures exactly the azurerm backend, and
# this file configures exactly the gcs backend. A scaffolded gcp instance
# includes THIS root (its include targets `root-gcp.hcl`); no instance
# includes more than one.
#
# The state bucket's name is composed by
# `devcontainer_config.state_store_gcp`, never here: GCS's own naming law
# (3-63 lowercase letters, digits, hyphens, underscores and dots) and the
# collision-proofing digest both need a composition HCL has no crypto
# functions to re-derive, so the composition lives once, in python, keyed on
# the template and the committed suffix declared below -- the same
# declaration shape the other two roots carry for their own stores.
# state_store_gcp writes the one backend name it composed (the bucket) into
# `remote-instances/<instance>/.state-store.json` before any Terragrunt
# command runs (`make`'s tg_init ensures the store first -- Terragrunt's gcs
# backend has no `--backend-bootstrap` form, so the ensure IS the
# bootstrap), and the remote_state block below reads that side-car rather
# than recomposing the name, so the bucket terragrunt writes state to can
# never drift from the bucket the bootstrap provisioned: one naming
# authority, one handoff file. The side-car is a per-instance scaffold
# artifact, already covered by the `remote-instances/<name>/` rule
# `make instance-init` appends to .gitignore.
#
# D14 (spec Section 13) records the operator's chosen engine as Terraform,
# and for the same reason root.hcl and root-azure.hcl record -- Terragrunt's
# own default binary is "tofu" on PATH and neither the devcontainer image
# nor that default guarantees Terraform is what actually runs --
# terraform_binary below states the engine choice explicitly: the pin is
# the guarantee, not a restatement of one the image already provides.
terraform_binary = "terraform"

locals {
  # The instance's own name, derived from the including directory rather
  # than typed, exactly the way the state key below is derived: this root is
  # included only by per-instance directories under remote-instances/, and
  # path_relative_to_include() names that directory.
  instance_name = basename(path_relative_to_include())

  # The bucket name template and its committed suffix are the two
  # declarations `devcontainer_config.state_store_gcp` parses (its parser
  # reads THIS file), stated here so the naming contract lives beside the
  # backend the name is composed for. The template substitutes one
  # `${local.NAME}` token per component; the suffix is generated once and
  # committed, never regenerated -- a fresh clone with no local state still
  # composes this same name and finds the bucket an earlier ensure
  # provisioned instead of creating an orphaned second one. If the suffix
  # is ever absent, nothing invents a replacement: state_store_gcp refuses,
  # and a new suffix would strand whatever state the old one pointed at.
  state_store_suffix     = "q4m7zk"
  state_bucket_template  = "gdstate-${local.instance_name}"
}

# Section 6 floors, the same floors root.hcl and root-azure.hcl declare,
# enforced by Terragrunt itself at parse time. Terragrunt below 1.1.3 and an
# engine below 1.10 carry the same defects root.hcl's comment records (the
# use_lockfile/backend-bootstrap mis-combination and the missing native
# locking); the gcs backend does not use use_lockfile, but the floors are
# the platform's engine contract, not a per-backend detail, and the tests
# that assert them (tests/test_tool_version_floors.py) read them from the
# roots rather than restating either number.
terraform_version_constraint  = ">= 1.10"
terragrunt_version_constraint = ">= 1.1.3"

remote_state {
  backend = "gcs"

  # Terragrunt bootstraps nothing here (the gcs backend has no
  # --backend-bootstrap form): the bucket is ensured by
  # devcontainer_config.state_store_gcp before any Terragrunt command runs,
  # probed before it is created and created with uniform bucket-level
  # access. No google_storage_bucket resource exists anywhere in this
  # repository; declaring one here would race state_store_gcp's own ensure
  # over the same bucket.
  generate = {
    path      = "backend.tf"
    if_exists = "overwrite_terragrunt"
  }

  config = {
    # The one name in the side-car (see the locals above): the per-instance
    # bucket `gdstate-<sanitized-instance>-<8 hex>` that state_store_gcp
    # composed for THIS instance, zone, project and suffix. A missing
    # side-car fails here with terragrunt's own file-not-found, which is
    # the not-yet-bootstrapped state -- the remedy is the same deploy or
    # init flow that runs state_store_gcp's ensure first.
    bucket = jsondecode(file("${get_terragrunt_dir()}/.state-store.json"))["bucket"]

    # Derived from the including instance directory, never typed, so it
    # cannot disagree with the directory it names (spec Section 9).
    prefix = "${path_relative_to_include()}/terraform.tfstate"
  }
}

# No gcp instance directory under remote-instances/ declares a provider
# block of its own; it is generated here, once. The block stays EMPTY on
# purpose: no project, no region, no credentials file is pinned into it.
# Every resource in the composition carries `project = var.project_id` (the
# instance input the scaffold writes) explicitly, so the project that
# composes the state bucket's digest (state_store_gcp's record read) and the
# project that creates the resources and writes state can never diverge --
# pinning a project here would let the provider's ambient default disagree
# with the resource-level one, and credentials resolve from the ambient
# ADC/application-default chain exactly the way the other two roots resolve
# theirs from their ambient chains.
generate "google_provider" {
  path      = "provider.tf"
  if_exists = "overwrite_terragrunt"

  contents = <<-EOF
    provider "google" {}
  EOF
}

# No state resource is declared anywhere in this repository: no GCS bucket.
# The bootstrap is state_store_gcp's ensure, run by every
# Terragrunt-driving make target before Terragrunt itself; the destroy path
# deliberately does not touch the store (`make bucket-delete` owns that), so
# an instance's state survives its deployment and a re-deploy finds its
# history intact.
