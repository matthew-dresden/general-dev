# Every option this module supports is declared here as an input. No value a
# caller might reasonably want to change is a literal inside main.tf; see
# Section 5.6 of repos/spec/devcontainer-platform.md.

variable "name_prefix" {
  description = <<-EOT
    Prefix used to name the service account (junctioned as "gd-<prefix>")
    and every secret ("<prefix>-<leaf>") this module creates (for example
    "devcontainer-remote-prod"). Required, no default: no resource name is
    a literal inside this module. Must satisfy the RFC1035 law the root
    composition validates on var.name_prefix -- the same sanitized value
    the publication flows mint their secret ids from -- and its 27-character
    bound is what keeps the "gd-" junction inside gcp's 6-30
    service-account-id law.
  EOT
  type        = string
}

variable "project_id" {
  description = <<-EOT
    GCP project id the service account and the six secrets are created in.
    Required, no default: the module never picks a project on the caller's
    behalf, and the generated provider block is empty -- every resource
    names its project explicitly. The publication flows write into this
    same project, read from the engine address record the deploy writes.
  EOT
  type        = string
}

variable "tags" {
  description = <<-EOT
    Common labels merged onto the six secrets this module creates. The
    service account carries no labels: it is an identity, not labeled
    state. Optional; defaults to an empty map when the caller has no
    additional labels to apply.
  EOT
  type        = map(string)
  default     = {}
}
