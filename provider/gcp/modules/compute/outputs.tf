# The entire surface the root composition and the transport see. No
# cloud-init content, no disk attachment detail, no metadata detail and no
# service-account detail is exposed here; those are this module's own and
# its sibling modules' internal implementation. The instance's identity
# inputs are already the deploy contract's names -- the engine address
# record is written from the root composition's outputs, most of which are
# passthroughs of this module's inputs.

output "instance_self_link" {
  description = "Self-link of the virtual machine this module creates, the fully qualified identifier the gcloud surfaces aim at."
  value       = google_compute_instance.this.id
}

output "instance_tag" {
  description = "The network tag the instance carries and the network module's IAP firewall targets."
  value       = local.instance_tag
}

output "data_disk_id" {
  description = <<-EOT
    Identifier of the data disk this module creates, or null when
    var.create_data_volume is false and no data disk exists.
  EOT
  value       = var.create_data_volume ? google_compute_disk.data[0].id : null
}
