# The entire surface the root composition and its consumers see. No VM, VM
# size or identity is declared or emitted here; those belong to the compute
# module, and the security platform to the security module, mirroring the
# split provider/aws/modules/network documents.

output "resource_group_name" {
  description = "Name of the resource group this module creates, which every other resource in the composition is deployed into."
  value       = azurerm_resource_group.this.name
}

output "vnet_id" {
  description = "Identifier of the virtual network this module creates."
  value       = azurerm_virtual_network.this.id
}

output "subnet_id" {
  description = "Identifier of the subnet this module creates."
  value       = azurerm_subnet.this.id
}
