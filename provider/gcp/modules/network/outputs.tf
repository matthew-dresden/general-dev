# The entire surface the root composition and its consumers see. No NAT
# configuration, no firewall rule detail and no router detail is exposed;
# those are this module's internal implementation. The network and
# subnetwork identifiers and names are exactly what the root composition
# resolves into the compute module's inputs (and re-exports for a second
# deployment's reuse) when this module is instantiated.

output "network_id" {
  description = "Identifier of the virtual network this module creates."
  value       = google_compute_network.this.id
}

output "subnet_id" {
  description = "Identifier of the subnetwork this module creates, the value the compute module attaches the instance's interface to and a second deployment passes to its own var.subnet_id with create_network = false to reuse this network."
  value       = google_compute_subnetwork.this.id
}

output "network_name" {
  description = "Name of the virtual network this module creates."
  value       = google_compute_network.this.name
}

output "subnet_name" {
  description = "Name of the subnetwork this module creates."
  value       = google_compute_subnetwork.this.name
}

output "firewall_name" {
  description = "Name of the IAP ingress firewall this module creates, the composition's one inbound rule."
  value       = google_compute_firewall.iap_ingress.name
}
