# The entire surface the root composition and the deploy flow see. No
# binding detail and no replication detail is exposed; those are this
# module's internal implementation. The service account identifiers are
# exactly what the root composition resolves into the compute module's
# inputs (and re-exports for a second deployment's reuse) when this module
# is instantiated, and the secret id list is the record of what the
# publication flows write into.

output "sa_id" {
  description = "Identifier of the service account this module creates, which the compute module's instance resource consumes."
  value       = google_service_account.this.id
}

output "sa_email" {
  description = "Email address of the service account this module creates, the identity the instance assumes and a second deployment passes to its own var.sa_email with create_platform = false to reuse this account."
  value       = google_service_account.this.email
}

output "secret_ids" {
  description = "The six Secret Manager secret ids this module creates, in publication order: the ids the cert-publish and push-secrets flows write into and the VM's startup script fetches by."
  value       = [for leaf in local.secret_leaves : google_secret_manager_secret.this[leaf].id]
}
