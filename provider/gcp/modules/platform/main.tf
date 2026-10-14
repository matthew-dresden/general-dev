# Platform module: the platform unit the remote engine sits behind -- the
# service account the instance assumes, and the six per-instance secrets
# (the three TLS material secrets, the checksum secret beside them, and the
# two configuration-payload secrets) with the secretAccessor bindings that
# let exactly this instance read them. The gcp twin of
# provider/azure/modules/security, mirroring its structure and its comment
# discipline. See Section 5.6 of repos/spec/devcontainer-platform.md.
#
# The two-tier secret model this module implements is the whole security
# story: the laptop-side operator holds write access through their own
# ambient gcloud identity (the cert-publish and push-secrets flows), and
# the instance reads exactly its own secrets through the secretAccessor
# bindings below -- one service account, scoped to the six secrets this
# module created, carrying no project-wide IAM role at all. A principal
# holding neither the operator's identity nor this account learns nothing
# from knowing the project id; revocation is performed on the IAM side
# (removing a binding), not from the resources below.
#
# The secret VEHICLES are created here; their CONTENTS never are. No
# google_secret_manager_secret_version resource exists in this module: the
# values are written exclusively by the laptop-side publication flows
# (cert material and the checksum by `make cert-publish`, the two
# configuration payloads by push-secrets), and a version created here
# -- even an empty one -- would race those flows and leave a
# "latest version" that is not the published one. An empty secret with no
# versions is exactly the pre-publish state the VM's startup script is
# written to tolerate.
#
# The six derived names below come from the shared name_prefix, junctioned
# to the same leaf names devcontainer_config.certs' publication flows
# compose (`<prefix>-<leaf>`, the one composition gcp_secret_id states on
# the python side): the ids the VM's startup script fetches by and the ids
# created here cannot drift, because both sides derive from one rule. The
# leaves are declared once in the locals list below, and every resource
# that needs them iterates that list.

locals {
  # The six secret leaves, in publication order: the three TLS material
  # secrets, the checksum secret beside them (the boot-applied install's
  # one comparison value), and the two configuration payloads. The full
  # secret id is "<name_prefix>-<leaf>" for each. One list, iterated by
  # both the secret resource and its binding, so a leaf added to one
  # without the other is impossible by construction.
  secret_leaves = [
    "tls-server-key",
    "tls-server-cert",
    "tls-ca",
    "tls-checksum",
    "shell-env",
    "profile-map",
  ]

  # The service account's id, junctioned onto the shared prefix with the
  # repository's "gd-" convention. A gcp service account id is 6 to 30
  # lowercase characters beginning with a letter: the root composition's
  # name_prefix validation (3 to 27 RFC1035 characters) is exactly what
  # keeps this junction inside that law, so no second validation lives
  # here.
  account_id = "gd-${var.name_prefix}"
}

resource "google_service_account" "this" {
  account_id   = local.account_id
  project      = var.project_id
  display_name = "general-dev engine ${var.name_prefix}"
}

resource "google_secret_manager_secret" "this" {
  for_each = toset(local.secret_leaves)

  project   = var.project_id
  secret_id = "${var.name_prefix}-${each.value}"

  # Automatic replication: google replicates the secret as needed and no
  # replica set is pinned. The VM reads through the Secret Manager REST
  # API wherever it runs, and a regional replica set would be a second
  # decision to keep in step with the instance's own region for no
  # property this platform needs.
  replication {
    auto {}
  }

  labels = var.tags
}

resource "google_secret_manager_secret_iam_member" "secret_accessor" {
  for_each = toset(local.secret_leaves)

  project = var.project_id
  # The binding is per secret, never project-wide: the two-tier model's
  # boundary is that this account reads exactly the six secrets of its own
  # instance and nothing else in the project.
  secret_id = google_secret_manager_secret.this[each.key].id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.this.email}"
}
