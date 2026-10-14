# Included by every gcp instance directory under remote-instances/,
# alongside root-gcp.hcl, and fixes the module inputs every gcp instance
# holds in common. See spec Section 5.6 (the module's variable surface),
# Section 9 (adding an instance is a new directory and nothing else) and
# Section 13 decision D6 (module and deployments separated).
#
# What lives here is limited to values with no per-instance identity and no
# dependency on a create_* toggle a specific deployment sets: the rootless
# Docker daemon's account, its TLS listener, and the apt repository it
# installs from. Every one of those is a fixed operational convention this
# repository uses for every instance it provisions, not a choice a
# deployment makes.
#
# Deliberately absent from this file, and left to the including instance
# directory instead: the instance's own identity (var.instance_name,
# var.name_prefix, both carried in their sanitized RFC1035 form), its gcp
# project, zone and region (read from the scaffold; the region is derived
# from the zone, never chosen separately), its VM size and volume sizes,
# the CIDRs of the network a create_network = true deployment makes
# (var.vpc_cidr, var.subnet_cidr), the subnetwork identifier a
# create_network = false deployment reuses (var.subnet_id), the service
# account email a create_platform = false deployment reuses
# (var.sa_email), and var.tags: which of those is required, and to what
# value, depends on which network and which platform posture that one
# deployment chooses, which the including instance directory alone knows.
# Putting any of them here would either collide two instances on the same
# value or silently discard the choice a per-instance file makes.
terraform {
  source = "${get_repo_root()}//provider/gcp"
}

inputs = {
  # Always required by the module (no default); the same rootless-daemon
  # data directory convention applies to every instance this repository
  # provisions.
  docker_data_root = "/mnt/docker-data"

  # The remaining inputs below already match the module's own defaults
  # (provider/gcp/variables.tf). Pinning them here states the operational
  # convention explicitly, so a future change to the module's default does
  # not silently change what every existing instance renders, without
  # requiring any instance directory to know the value itself.
  docker_daemon_user = "dockerd"
  # 0.0.0.0, NOT the azure twin's loopback: the gcp doctrine is that the
  # IAP tunnel terminates on the VM's nic0, so the daemon must bind the
  # interface address for the tunnel to reach it -- loopback would bind a
  # address no tunnel can dial. The boundary is not the bind address here
  # but the VPC's sole ingress rule (tcp/2376 from the IAP range, the
  # network module's firewall), which is the guard the bind relies on.
  docker_tls_listen_address = "0.0.0.0"
  docker_tls_listen_port    = 2376
  docker_repo_base_url      = "https://download.docker.com/linux/ubuntu"
  docker_repo_channel       = "stable"
}
