# The surface the deploy flow and a second deployment consume: the six
# values the engine address record is written from (the gcp branch of
# make instance-deploy reads exactly these, by these names, from
# `terragrunt output -json`) plus the two a second deployment consumes to
# reuse this deployment's network and service account. The six required
# names are the deploy flow's contract and are pinned by
# tests/test_gcp_module.py against the Makefile's own required list, so the
# two sides cannot drift.
#
# Two of the six are computed here rather than read from a resource, and
# both computations are deliberate:
#
# - `secrets_prefix` is var.name_prefix unchanged, because the sanitized
#   name IS the prefix: the platform unit creates its secrets as
#   "<name_prefix>-<leaf>" and the publication flows (certs.gcp_secret_id)
#   compose the same ids from the same value, so there is nothing to look
#   up.
# - `state_bucket` is the composition state_store_gcp makes -- the same
#   template and suffix remote-instances/root-gcp.hcl declares, digested
#   with sha256 over "instance|zone|project|suffix" and truncated to the
#   first 8 hex characters -- mirrored here so the record carries the name
#   in one deploy run. ONE source of truth governs: root-gcp.hcl's
#   declaration is authoritative (state_store_gcp parses it and composes
#   the bucket terragrunt actually writes state to, through the side-car),
#   and this local is the deploy-side mirror of the same formula. They
#   agree exactly when the instance directory's name is already in its
#   sanitized form (lowercase letters, digits and dashes) -- the platform's
#   naming convention -- because this module receives only the sanitized
#   name while state_store_gcp digests the raw directory name. var.
#   state_store_suffix mirrors the committed suffix for the same reason,
#   and tests/test_gcp_module.py pins both sides.

output "instance_name" {
  description = "Name of the virtual machine this deployment creates, the sanitized RFC1035 label every gcloud surface addresses it by. The engine address record carries this verbatim."
  value       = var.instance_name
}

output "zone" {
  description = "GCP zone the instance was created in, exactly the var.zone the caller supplied. The engine address record carries this verbatim; the IAP tunnel is dialed through it."
  value       = var.zone
}

output "project_id" {
  description = "GCP project id this deployment ran against, exactly the var.project_id the caller supplied. The state bucket and the six secrets live in this project; the engine address record carries this verbatim."
  value       = var.project_id
}

output "region" {
  description = "GCP region this deployment ran against, exactly the var.region the caller supplied (the zone's own region). The engine address record carries this verbatim; the state bucket takes its location from it."
  value       = var.region
}

output "secrets_prefix" {
  description = "The Secret Manager prefix the instance's six secrets live under: var.name_prefix unchanged, the sanitized name the publication flows compose their ids from. The engine address record carries this verbatim."
  value       = local.secrets_prefix
}

output "state_bucket" {
  description = <<-EOT
    Name of the instance's state bucket, composed with the same formula
    devcontainer_config.state_store_gcp applies to remote-instances/
    root-gcp.hcl's declaration (gdstate-<sanitized>-<8 hex of
    sha256(instance|zone|project|suffix)>). The bucket itself is NOT
    terraform-managed -- state_store_gcp owns it -- and this output is the
    deploy-side mirror of that composition, exact whenever the instance
    directory's name is already in sanitized form (see this file's header).
    The engine address record carries this verbatim.
  EOT
  value       = local.state_bucket
}

output "vpc_id" {
  description = <<-EOT
    Identifier of the virtual network the instance's subnetwork belongs to,
    when this deployment created its own network. Null when
    var.create_network is false: this deployment was given only a
    subnetwork, and the network it lives in was never named to it.
  EOT
  value       = var.create_network ? module.network[0].network_id : null
}

output "subnet_id" {
  description = <<-EOT
    Identifier of the subnetwork the instance is launched into: the network
    submodule's own output when var.create_network is true, or
    var.subnet_id unchanged when it is false. A second deployment passes
    this to its own var.subnet_id with var.create_network set to false to
    reuse this network.
  EOT
  value       = local.subnet_id
}

output "sa_email" {
  description = <<-EOT
    Email address of the service account the instance assumes: the platform
    submodule's own output when var.create_platform is true, or var.sa_email
    unchanged when it is false. A second deployment passes this to its own
    var.sa_email with var.create_platform set to false to reuse this
    account -- which carries its secretAccessor grants only where the
    creating deployment's platform unit scoped them.
  EOT
  value       = local.sa_email
}
