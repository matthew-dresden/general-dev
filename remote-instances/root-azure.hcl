# Included by every azure instance directory under remote-instances/.
# Configures the remote state backend, generates the azure provider block,
# and declares the tool version floors Section 6 requires.
#
# This is the azure twin of root.hcl, and it exists as a separate root
# rather than an extension of it for one structural reason: the azurerm
# backend shares no shape with the s3 one -- no bucket, no region, no
# account id, no DynamoDB-era locking flag -- so a merged root would carry
# two backends' config shapes through one file, and every azure instance
# would parse (and every reader would reason about) the s3 backend it never
# runs. One root per cloud keeps each cloud's backend byte-honest: root.hcl
# configures exactly the s3 backend and stays byte-identical, and this file
# configures exactly the azurerm backend. A scaffolded azure instance
# includes THIS root (its include targets `root-azure.hcl`); no instance
# includes both.
#
# The state storage account's name is composed by
# `devcontainer_config.state_store`, never here: azure's own naming law for
# a storage account (3-24 lowercase alphanumerics, globally unique) needs a
# sanitize-and-digest composition HCL has no crypto functions to express,
# so the composition lives once, in python, keyed on the template and the
# committed suffix declared below -- the same declaration shape root.hcl
# carries for its bucket. state_store writes the two backend names it
# composed (the resource group and the storage account) into
# `remote-instances/<instance>/.state-store.json` before any Terragrunt
# command runs (`make`'s tg_init ensures the store first), and the
# remote_state block below reads that side-car rather than recomposing the
# names, so the names terragrunt writes state to can never drift from the
# names the bootstrap provisioned: one naming authority, one handoff file.
# The side-car is a per-instance scaffold artifact, already covered by the
# `remote-instances/<name>/` rule `make instance-init` appends to
# .gitignore.
#
# D14 (spec Section 13) records the operator's chosen engine as Terraform,
# and for the same reason root.hcl records -- Terragrunt's own default
# binary is "tofu" on PATH and neither the devcontainer image nor that
# default guarantees Terraform is what actually runs -- terraform_binary
# below states the engine choice explicitly: the pin is the guarantee, not
# a restatement of one the image already provides.
terraform_binary = "terraform"

locals {
  # The instance's own name, derived from the including directory rather
  # than typed, exactly the way the state key below is derived: this root is
  # included only by per-instance directories under remote-instances/, and
  # path_relative_to_include() names that directory.
  instance_name = basename(path_relative_to_include())

  # The storage account name template and its committed suffix are the two
  # declarations `devcontainer_config.state_store` parses (its parser reads
  # THIS file), stated here so the naming contract lives beside the backend
  # the names are composed for. The template substitutes one
  # `${local.NAME}` token per component; the suffix is generated once and
  # committed, never regenerated -- a fresh clone with no local state still
  # composes this same name and finds the account an earlier bootstrap
  # provisioned instead of creating an orphaned second one. If the suffix
  # is ever absent, nothing invents a replacement: state_store refuses, and
  # a new suffix would strand whatever state the old one pointed at.
  state_store_suffix = "gb9cho"

  state_storage_name_template = "tgstate${local.instance_name}${local.state_store_suffix}"

  # The two backend names, read from the side-car state_store wrote. The
  # file is the handoff, not a duplicate of the composition: it names the
  # resource group (`rg-<sanitized-instance>-state`, the one that stands
  # outside the deployment's lifecycle) and the storage account exactly as
  # state_store composed them for THIS instance, subscription and location.
  # A missing side-car fails here with terragrunt's own file-not-found,
  # which is the not-yet-bootstrapped state -- the remedy is the same
  # deploy or init flow that runs state_store's ensure first.
  state_store_file = jsondecode(file("${get_terragrunt_dir()}/.state-store.json"))

  state_resource_group  = local.state_store_file["resource_group"]
  state_storage_account = local.state_store_file["storage_account_name"]

  # No azure_subscription_id local exists here on purpose. The generated
  # provider block below omits subscription_id, so the provider resolves it
  # from the ambient azure login (`az login` / ARM_* environment
  # variables), the same no-profile discipline root.hcl applies to the AWS
  # SDK chain: the subscription that composes the state account's digest
  # (state_store's `az account show`) and the subscription that creates and
  # writes state can never diverge, because they are the same ambient
  # lookup. Pinning a subscription id here would let the two diverge the
  # moment the ambient login changed.
}

# Section 6 floors, the same floors root.hcl declares, enforced by
# Terragrunt itself at parse time. Terragrunt below 1.1.3 and an engine
# below 1.10 carry the same defects root.hcl's comment records (the
# use_lockfile/backend-bootstrap mis-combination and the missing native
# locking); the azurerm backend does not use use_lockfile, but the floors
# are the platform's engine contract, not a per-backend detail, and the
# tests that assert them (tests/test_tool_version_floors.py) read them from
# the roots rather than restating either number.
terraform_version_constraint  = ">= 1.10"
terragrunt_version_constraint = ">= 1.1.3"

remote_state {
  backend = "azurerm"

  # Terragrunt bootstraps nothing here (the azurerm backend has no
  # --backend-bootstrap form): the resource group, the storage account and
  # its `tfstate` container are ensured by devcontainer_config.state_store
  # before any Terragrunt command runs, each probed before it is created.
  # No azurerm_storage_* resource exists anywhere in this repository;
  # declaring one here would race state_store's own ensure over the same
  # account.
  generate = {
    path      = "backend.tf"
    if_exists = "overwrite_terragrunt"
  }

  config = {
    # Both names come from the side-car (see the locals above): the
    # dedicated `rg-<instance>-state` group that outlives the deployment's
    # own resource group, and the globally unique account name state_store
    # composed from the template and suffix declared in this file.
    resource_group_name  = local.state_resource_group
    storage_account_name = local.state_storage_account

    # The container state_store ensures, created with `--auth-mode login`,
    # so every later state read and write authorizes through the caller's
    # Entra identity and no account key is ever involved. use_azuread_auth
    # gives the backend itself the same rule.
    container_name   = "tfstate"
    use_azuread_auth = true

    # Derived from the including instance directory, never typed, so it
    # cannot disagree with the directory it names (spec Section 9).
    key = "${path_relative_to_include()}/terraform.tfstate"
  }
}

# No azure instance directory under remote-instances/ declares a provider
# block of its own; it is generated here, once. No subscription_id here,
# for the reason the locals block above records: the provider resolves the
# subscription from the same ambient azure login state_store's account
# lookup does, so the account the state names and the subscription that
# writes it can never diverge. `features {}` is azurerm's required (empty)
# features block; every default it would carry is a provider-internal
# behavior, not a deployment choice this repository makes.
generate "azure_provider" {
  path      = "provider.tf"
  if_exists = "overwrite_terragrunt"

  contents = <<-EOF
    provider "azurerm" {
      features {}
    }
  EOF
}

# No state resource is declared anywhere in this repository: no storage
# account, no container, no resource group for them. The bootstrap is
# state_store's ensure, run by every Terragrunt-driving make target before
# Terragrunt itself; the destroy path deliberately does not touch the store
# (`make store-delete` owns that), so an instance's state survives its
# deployment and a re-deploy finds its history intact.
