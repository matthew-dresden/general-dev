# The surface the deploy flow and a second deployment consume: the eight
# values the engine address record is written from (the azure branch of
# make instance-deploy reads exactly these, by these names, from
# `terragrunt output -json`) plus the three a second deployment consumes to
# reuse this deployment's network and identity. The eight required names
# are the deploy flow's contract and are pinned by
# tests/test_azure_module.py against the Makefile's own required list, so
# the two sides cannot drift.
#
# data.azurerm_client_config resolves nothing here on purpose: the location
# is an input, the resource group a local, and the VM values come from the
# compute submodule -- no lookup exists to resolve a region or a tenant the
# caller did not already name.

output "vm_name" {
  description = "Name of the virtual machine this deployment creates, which every run-command surface addresses it by."
  value       = module.compute.vm_name
}

output "vm_resource_id" {
  description = "Resource id of the virtual machine this deployment creates, the fully qualified identifier the run-command surfaces aim at."
  value       = module.compute.vm_id
}

output "resource_group" {
  description = "Name of the resource group this deployment deployed into: the network submodule's own when var.create_network is true, or var.resource_group_name unchanged when it is false."
  value       = local.resource_group_name
}

output "relay_namespace" {
  description = "Fully qualified host name of the relay namespace the docker API travels over: the security submodule's own output when var.create_platform is true, or var.relay_namespace unchanged when it is false. The engine address record carries this verbatim."
  value       = local.relay_namespace
}

output "hybrid_connection" {
  description = "Name of the hybrid connection the relay couple meets on: the security submodule's own output when var.create_platform is true, or var.hybrid_connection unchanged when it is false. The engine address record carries this verbatim."
  value       = local.hybrid_connection
}

output "vault_name" {
  description = "Name of the Key Vault the instance's published TLS material and secrets live in. The engine address record carries this; a deployment that reuses its own platform (var.create_platform false) has no vault of this composition's and the record carries the name its operator manages."
  value       = var.create_platform ? module.security[0].vault_name : null
}

output "vault_uri" {
  description = "URI of the Key Vault the instance's published TLS material and secrets live in, the base URL the VM's material pulls are aimed at. Null on the reused-platform path, for the same reason vault_name is."
  value       = var.create_platform ? module.security[0].vault_uri : null
}

output "location" {
  description = "Azure region this deployment ran against, exactly the var.location the caller supplied."
  value       = var.location
}

output "vnet_id" {
  description = <<-EOT
    Identifier of the virtual network the instance's subnet belongs to,
    when this deployment created its own network. Null when
    var.create_network is false: this deployment was given only a subnet,
    and the network it lives in was never named to it.
  EOT
  value       = var.create_network ? module.network[0].vnet_id : null
}

output "subnet_id" {
  description = <<-EOT
    Identifier of the subnet the instance is launched into: the network
    submodule's own output when var.create_network is true, or
    var.subnet_id unchanged when it is false. A second deployment passes
    this to its own var.subnet_id with var.create_network set to false to
    reuse this network.
  EOT
  value       = local.subnet_id
}

output "identity_id" {
  description = <<-EOT
    Identifier of the user-assigned managed identity the instance assumes:
    the security submodule's own output when var.create_platform is true,
    or var.identity_id unchanged when it is false. A second deployment
    passes this to its own var.identity_id with var.create_platform set to
    false to reuse this identity.
  EOT
  value       = local.identity_id
}
