# The entire surface the root composition and the transport see. No
# cloud-config content, no disk attachment detail and no identity detail is
# exposed here; those are this module's own and its sibling modules'
# internal implementation. The VM's resource id and name are exactly what
# the engine address record the deploy writes carries, so the run-command
# surfaces can aim at the instance without ever looking anything up.

output "vm_id" {
  description = "Resource id of the virtual machine this module creates, the vm_resource_id the engine address record carries."
  value       = azurerm_linux_virtual_machine.this.id
}

output "vm_name" {
  description = "Name of the virtual machine this module creates, which every run-command surface addresses it by."
  value       = azurerm_linux_virtual_machine.this.name
}

output "data_disk_id" {
  description = <<-EOT
    Identifier of the data disk this module creates, or null when
    var.create_data_volume is false and no data disk exists.
  EOT
  value       = var.create_data_volume ? azurerm_managed_disk.data[0].id : null
}
