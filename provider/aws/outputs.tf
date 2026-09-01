# The surface a second deployment consumes to reuse this deployment's
# network (AC-10.11), plus the daemon endpoint the transport in E6 targets.
# No security group or IAM detail is re-exported here: AC-FUNC-005 names
# only the instance identifier, the network identifiers and the daemon
# endpoint, and the security submodule's own outputs remain internal to
# this composition.
#
# data.aws_region resolves the region a plan or apply is running against so
# a second deployment's operator does not have to already know it; the same
# 6.0-floor `region` attribute the security submodule reads (see
# versions.tf) rather than the 5.x-only `name` attribute.

data "aws_region" "current" {}

output "instance_id" {
  description = "Identifier of the instance this deployment creates."
  value       = module.compute.instance_id
}

output "region" {
  description = "AWS region this deployment runs in."
  value       = data.aws_region.current.region
}

output "availability_zone" {
  description = "Availability zone the instance this deployment creates runs in."
  value       = module.compute.availability_zone
}

output "docker_daemon_endpoint" {
  description = "The rootless daemon's mTLS endpoint, reachable only through the SSM port forward in E6."
  value       = module.compute.docker_daemon_endpoint
}

output "vpc_id" {
  description = <<-EOT
    Identifier of the VPC the instance's subnet belongs to: the network
    submodule's own output when var.create_network is true, or var.vpc_id
    unchanged when it is false. A second deployment passes this to its own
    var.vpc_id with var.create_network set to false to reuse this network
    (AC-10.11).
  EOT
  value       = local.vpc_id
}

output "subnet_id" {
  description = <<-EOT
    Identifier of the subnet the instance is launched into: the network
    submodule's own output when var.create_network is true, or var.subnet_id
    unchanged when it is false. A second deployment passes this to its own
    var.subnet_id with var.create_network set to false to reuse this network
    (AC-10.11).
  EOT
  value       = local.subnet_id
}
