# The entire surface the root composition and the transport in E6 see. No
# security group detail, no IAM detail and no user-data content is exposed
# here; those are this module's own and its sibling modules' internal
# implementation.

output "instance_id" {
  description = "Identifier of the instance this module creates."
  value       = aws_instance.this.id
}

output "availability_zone" {
  description = "Availability zone the instance this module creates runs in."
  value       = aws_instance.this.availability_zone
}

output "data_volume_id" {
  description = <<-EOT
    Identifier of the encrypted gp3 data volume this module creates, or
    null when var.create_data_volume is false and no data volume exists.
  EOT
  value       = var.create_data_volume ? aws_ebs_volume.data[0].id : null
}

output "docker_daemon_endpoint" {
  description = <<-EOT
    The rootless daemon's mTLS endpoint, composed from
    var.docker_tls_listen_address and var.docker_tls_listen_port (for
    example "tcp://127.0.0.1:2376"). Reachable only through the SSM port
    forward in E6, which targets this endpoint and nothing else.
  EOT
  value       = "tcp://${var.docker_tls_listen_address}:${var.docker_tls_listen_port}"
}
