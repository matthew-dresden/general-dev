# The root composition: exactly three module blocks, wiring the network,
# platform and compute submodules together, and no resource block of any
# kind. The gcp twin of provider/azure/remote-azure-vm.tf, mirroring its
# structure and its comment discipline. See Section 5.6 of
# repos/spec/devcontainer-platform.md and Section 13 decision D6, module in
# provider/gcp, deployments in remote-instances.
#
# Every identifier crossing a submodule boundary is resolved in `locals`
# below, from the submodule that created it when its toggle is true or from
# the matching replacement input when it is false. The compute module block
# is written once, unaware of which mode produced the values it receives.
#
# module.platform is instantiated whenever var.create_platform is true,
# because the platform submodule creates the service account the instance
# assumes and the six per-instance secrets with their secretAccessor
# bindings as a single unit with no internal toggle of its own. A
# deployment that needs none of the platform unit sets
# var.create_platform false and supplies the one replacement input the
# compute module still requires (var.sa_email); a deployment that wants
# only some of the unit cannot have that, by the same reasoning
# provider/azure records for its own security submodule -- the unit's
# resources reference one another (every binding needs both the secret and
# the account), so partial creation would be a different module, not this
# one with fewer resources.

locals {
  # The subnetwork identifier the compute submodule consumes, resolved from
  # the network submodule's own output when it was created or from the
  # matching replacement input when it was not.
  subnet_id = var.create_network ? module.network[0].subnet_id : var.subnet_id

  # The service account email the compute submodule attaches to the
  # instance, resolved from the platform submodule's own output when the
  # platform unit was created or from the matching replacement input when
  # it was not.
  sa_email = var.create_platform ? module.platform[0].sa_email : var.sa_email

  # The network tag the instance carries and the IAP firewall targets: one
  # value, composed once here, so the firewall's target and the instance's
  # tag cannot drift apart. The name_prefix is a validated RFC1035 label,
  # which is exactly a legal network tag.
  instance_tag = var.name_prefix

  # The secrets prefix and the state bucket name the deploy flow writes
  # into the engine address record. The prefix is the sanitized name the
  # secrets are created under (see provider/gcp/outputs.tf); the bucket
  # name is the composition state_store_gcp makes -- see the outputs file
  # for the one-source-of-truth note the digest carries.
  secrets_prefix = var.name_prefix
  state_bucket   = "gdstate-${var.instance_name}-${substr(sha256("${var.instance_name}|${var.zone}|${var.project_id}|${var.state_store_suffix}"), 0, 8)}"
}

module "network" {
  count  = var.create_network ? 1 : 0
  source = "./modules/network"

  name_prefix            = var.name_prefix
  project_id             = var.project_id
  region                 = var.region
  vpc_cidr               = var.vpc_cidr
  subnet_cidr            = var.subnet_cidr
  instance_tag           = local.instance_tag
  docker_tls_listen_port = var.docker_tls_listen_port

  # No tags input here, by the network module's own absence: gcp network
  # resources expose no labels, and the module refuses an input it could
  # only silently drop (see its variables file). var.tags reaches the
  # platform and compute submodules, whose resources do carry labels.
}

module "platform" {
  count  = var.create_platform ? 1 : 0
  source = "./modules/platform"

  name_prefix = var.name_prefix
  project_id  = var.project_id
  tags        = var.tags
}

module "compute" {
  source = "./modules/compute"

  instance_name             = var.instance_name
  name_prefix               = var.name_prefix
  project_id                = var.project_id
  zone                      = var.zone
  subnet_id                 = local.subnet_id
  sa_email                  = local.sa_email
  vm_size                   = var.vm_size
  os_image                  = var.os_image
  root_volume_size_gb       = var.root_volume_size_gb
  create_data_volume        = var.create_data_volume
  data_volume_size_gb       = var.data_volume_size_gb
  docker_daemon_user        = var.docker_daemon_user
  docker_data_root          = var.docker_data_root
  docker_tls_listen_address = var.docker_tls_listen_address
  docker_tls_listen_port    = var.docker_tls_listen_port
  docker_repo_base_url      = var.docker_repo_base_url
  docker_repo_channel       = var.docker_repo_channel
  boot_marker_timeout       = var.boot_marker_timeout
  tags                      = var.tags

  # The instance must not boot before the platform unit's secretAccessor
  # bindings exist: the first boot's startup script fetches the instance's
  # TLS material through exactly those bindings, and a fetch refused for
  # want of them would record a failure attribute instead of a readiness
  # one. The compute module's inputs already reference the service account
  # output, but the bindings feed no output compute consumes, so without an
  # explicit edge a first apply could boot the VM into the window where the
  # account it runs as cannot yet read its own secrets. The dependency
  # names the whole platform module rather than the one resource: the
  # bindings have no output of their own, and the whole-module edge is what
  # keeps this block true whatever the platform unit's internals grow. The
  # IAP firewall needs no matching edge here: a custom-mode VPC admits no
  # inbound traffic at all until a firewall rule allows it, so the window
  # before the rule lands fails closed -- the safe direction, unlike the
  # azure twin's implicit-default window its edge exists to close.
  depends_on = [module.platform]
}
