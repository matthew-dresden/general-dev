# The entire surface the root composition and the compute module see. No
# security-rule detail, no role-assignment id and no vault key detail is
# exposed; those are this module's internal implementation. The identity,
# vault, relay namespace and hybrid connection names and identifiers are
# exactly what the root composition resolves into the compute module's
# inputs (and the engine address record the deploy writes) when this module
# is instantiated.

output "identity_id" {
  description = "Identifier of the user-assigned managed identity this module creates, which the compute module attaches to the instance."
  value       = azurerm_user_assigned_identity.this.id
}

output "vault_name" {
  description = "Name of the Key Vault this module creates, which the secrets surfaces address and the engine address record carries."
  value       = azurerm_key_vault.this.name
}

output "vault_uri" {
  description = "URI of the Key Vault this module creates, the base URL the VM's material pulls and the secret surfaces are aimed at."
  value       = azurerm_key_vault.this.vault_uri
}

output "relay_namespace_name" {
  description = <<-EOT
    Fully qualified host name of the relay namespace this module creates
    (the bare name plus azure's fixed public-cloud
    ".servicebus.windows.net" suffix), the host both halves of the relay
    couple dial. Composed here rather than in the root composition, because
    the suffix is a property of the namespace resource this module creates.
  EOT
  value       = "${azurerm_relay_namespace.this.name}.servicebus.windows.net"
}

output "hybrid_connection_name" {
  description = "Name of the hybrid connection this module creates, the rendezvous point the relay couple meets on."
  value       = azurerm_relay_hybrid_connection.this.name
}

output "nsg_id" {
  description = "Identifier of the zero-inbound network security group this module creates and associates with the subnet."
  value       = azurerm_network_security_group.this.id
}
