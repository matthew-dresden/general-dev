# Included by every instance directory under remote-instances/, alongside
# root.hcl, and fixes the module inputs every instance holds in common. See
# spec Section 5.6 (the module's variable surface), Section 9 (adding an
# instance is a new directory and nothing else) and Section 13 decision D6
# (module and deployments separated).
#
# What lives here is limited to values with no per-instance identity and no
# dependency on a create_* toggle a specific deployment sets: the rootless
# Docker daemon's account, its TLS listener, the apt repository it installs
# from, and the device name a second data volume is attached under. Every
# one of those is a fixed operational convention this repository uses for
# every instance it provisions, not a choice a deployment makes.
#
# Deliberately absent from this file, and left to the including instance
# directory instead: the instance's own identity (var.instance_name,
# var.name_prefix), its region and profile (resolved by root.hcl and the
# ambient AWS SDK chain, never a module input here), its AMI, instance type
# and volume sizes, and every input that is a companion of a create_*
# toggle a specific deployment sets (var.vpc_cidr, var.subnet_cidr,
# var.availability_zone, var.vpc_id, var.subnet_id, var.egress_cidr_blocks,
# var.security_group_ids, var.iam_instance_profile_name and var.tags):
# which of those is required, and to what value, depends on which network
# and which security posture that one deployment chooses, which the
# including instance directory alone knows. Putting any of them here would
# either collide two instances on the same value or silently discard the
# choice a per-instance file makes.
terraform {
  source = "${get_repo_root()}//provider/aws"
}

inputs = {
  # Always required by the module (no default); the same rootless-daemon
  # data directory convention applies to every instance this repository
  # provisions.
  docker_data_root = "/mnt/docker-data"

  # The remaining inputs below already match the module's own defaults
  # (provider/aws/variables.tf). Pinning them here states the operational
  # convention explicitly, so a future change to the module's default does
  # not silently change what every existing instance renders, without
  # requiring any instance directory to know the value itself.
  docker_daemon_user        = "dockerd"
  docker_tls_listen_address = "127.0.0.1"
  docker_tls_listen_port    = 2376
  docker_repo_base_url      = "https://download.docker.com/linux/ubuntu"
  docker_repo_channel       = "stable"
  data_volume_device_name   = "/dev/sdf"
}
