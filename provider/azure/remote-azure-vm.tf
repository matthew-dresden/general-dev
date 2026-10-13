# The root composition: exactly three module blocks, wiring the network,
# security and compute submodules together, and no resource block of any
# kind. The azure twin of provider/aws/remote-ec2-instance.tf, mirroring
# its structure and its comment discipline. See Section 5.6 of
# repos/spec/devcontainer-platform.md and Section 13 decision D6, module in
# provider/azure, deployments in remote-instances.
#
# Every identifier crossing a submodule boundary is resolved in `locals`
# below, from the submodule that created it when its toggle is true or from
# the matching replacement input when it is false. The compute module block
# is written once, unaware of which mode produced the values it receives.
#
# module.security is instantiated whenever var.create_platform is true,
# because the security submodule creates the zero-inbound network security
# group and its subnet association, the user-assigned identity, the Key
# Vault, the relay namespace and hybrid connection, and their role
# assignments as a single unit with no internal toggle of its own. A
# deployment that needs none of the platform unit sets
# var.create_platform false and supplies the three replacement inputs the
# compute module still requires (var.identity_id, var.relay_namespace,
# var.hybrid_connection); a deployment that wants only some of the unit
# cannot have that, by the same reasoning provider/aws records for its own
# security submodule -- the unit's resources reference one another (the
# association needs the group, the hybrid connection needs the namespace,
# the assignments need the vault and the namespace), so partial creation
# would be a different module, not this one with fewer resources.

locals {
  # The resource group and subnet identifiers the security and compute
  # submodules consume, resolved from the network submodule's own output
  # when it was created or from the matching replacement input when it was
  # not.
  resource_group_name = var.create_network ? module.network[0].resource_group_name : var.resource_group_name
  subnet_id           = var.create_network ? module.network[0].subnet_id : var.subnet_id

  # The identity, relay namespace and hybrid connection identifiers and
  # names the compute submodule consumes, resolved from the security
  # submodule's own outputs when the platform unit was created or from the
  # matching replacement inputs when it was not.
  identity_id       = var.create_platform ? module.security[0].identity_id : var.identity_id
  relay_namespace   = var.create_platform ? module.security[0].relay_namespace_name : var.relay_namespace
  hybrid_connection = var.create_platform ? module.security[0].hybrid_connection_name : var.hybrid_connection
}

module "network" {
  count  = var.create_network ? 1 : 0
  source = "./modules/network"

  location    = var.location
  vnet_cidr   = var.vnet_cidr
  subnet_cidr = var.subnet_cidr
  name_prefix = var.name_prefix
  tags        = var.tags
}

module "security" {
  count  = var.create_platform ? 1 : 0
  source = "./modules/security"

  location                    = var.location
  resource_group_name         = local.resource_group_name
  subnet_id                   = local.subnet_id
  instance_name               = var.instance_name
  name_prefix                 = var.name_prefix
  relay_sender_object_ids     = var.relay_sender_object_ids
  keyvault_officer_object_ids = var.keyvault_officer_object_ids
  tags                        = var.tags
}

module "compute" {
  source = "./modules/compute"

  location                  = var.location
  resource_group_name       = local.resource_group_name
  subnet_id                 = local.subnet_id
  identity_id               = local.identity_id
  name_prefix               = var.name_prefix
  vm_size                   = var.vm_size
  os_image_urn              = var.os_image_urn
  root_volume_size_gb       = var.root_volume_size_gb
  create_data_volume        = var.create_data_volume
  data_volume_size_gb       = var.data_volume_size_gb
  docker_daemon_user        = var.docker_daemon_user
  docker_data_root          = var.docker_data_root
  docker_tls_listen_address = var.docker_tls_listen_address
  docker_tls_listen_port    = var.docker_tls_listen_port
  docker_repo_base_url      = var.docker_repo_base_url
  docker_repo_channel       = var.docker_repo_channel
  relay_namespace           = local.relay_namespace
  hybrid_connection         = local.hybrid_connection
  tags                      = var.tags

  # The instance must not boot before the platform unit's network security
  # group is associated with its subnet. The compute module's inputs already
  # reference the identity, namespace and hybrid connection outputs, but the
  # subnet association feeds no output compute consumes, so without an
  # explicit edge a first apply could boot the VM into the window where no
  # group is attached -- and while it is absent, azure's implicit default
  # admits inbound traffic the zero-inbound group exists to deny. The
  # dependency names the whole security module rather than the one resource:
  # the association has no output of its own, and the whole-module edge is
  # what keeps this block true whatever the platform unit's internals grow.
  depends_on = [module.security]
}
