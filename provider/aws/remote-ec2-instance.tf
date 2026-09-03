# The root composition: exactly three module blocks, wiring the network,
# security and compute submodules together, and no resource block of any
# kind. See Section 5.6 of repos/spec/devcontainer-platform.md and Section
# 13 decision D6, module in provider/aws, deployments in remote-instances.
#
# Every identifier crossing a submodule boundary is resolved in `locals`
# below, from the submodule that created it when its toggle is true or from
# the matching replacement input when it is false (AC-FUNC-003). The compute
# module block is written once, unaware of which mode produced the values it
# receives.
#
# module.security is instantiated whenever either var.create_security_group
# or var.create_iam_role is true, because the security submodule creates the
# zero-ingress security group, the IAM role and the instance profile as a
# single unit with no internal toggle of its own (E5-F1-S3). Because the
# submodule cannot create only one of the two, var.create_security_group's
# own validation block in variables.tf requires the two root-level toggles
# to be set to the same value, rejecting any mixed combination at plan time
# before this module block's count expression is ever evaluated. Both
# toggles are therefore always equal by the time this file runs, and
# `count` below reduces to "both true" or "both false".

locals {
  # The VPC and subnet identifiers the security and compute submodules
  # consume, resolved from the network submodule's own output when it was
  # created or from the matching replacement input when it was not.
  vpc_id    = var.create_network ? module.network[0].vpc_id : var.vpc_id
  subnet_id = var.create_network ? module.network[0].subnet_id : var.subnet_id

  # The security group and instance-profile identifiers the compute
  # submodule consumes. Each is written as its own per-toggle expression, but
  # the header above constrains var.create_security_group and
  # var.create_iam_role to always be equal, so only the both-true and
  # both-false combinations ever reach these expressions -- a mixed toggle
  # setting is rejected at plan time before this file runs.
  security_group_ids = var.create_security_group ? [module.security[0].security_group_id] : var.security_group_ids
  iam_instance_profile_name = (
    var.create_iam_role ? module.security[0].iam_instance_profile_name : var.iam_instance_profile_name
  )
}

module "network" {
  count  = var.create_network ? 1 : 0
  source = "./modules/network"

  vpc_cidr          = var.vpc_cidr
  subnet_cidr       = var.subnet_cidr
  availability_zone = var.availability_zone
  name_prefix       = var.name_prefix
  tags              = var.tags
}

module "security" {
  count  = var.create_security_group || var.create_iam_role ? 1 : 0
  source = "./modules/security"

  vpc_id             = local.vpc_id
  instance_name      = var.instance_name
  egress_cidr_blocks = var.egress_cidr_blocks
  name_prefix        = var.name_prefix
  tags               = var.tags
}

module "compute" {
  source = "./modules/compute"

  ami                        = var.ami
  instance_type              = var.instance_type
  subnet_id                  = local.subnet_id
  security_group_ids         = local.security_group_ids
  iam_instance_profile_name  = local.iam_instance_profile_name
  name_prefix                = var.name_prefix
  root_volume_size_gb        = var.root_volume_size_gb
  create_data_volume         = var.create_data_volume
  data_volume_size_gb        = var.data_volume_size_gb
  data_volume_device_name    = var.data_volume_device_name
  docker_daemon_user         = var.docker_daemon_user
  docker_data_root           = var.docker_data_root
  docker_tls_listen_address  = var.docker_tls_listen_address
  docker_tls_listen_port     = var.docker_tls_listen_port
  aws_cli_installer_base_url = var.aws_cli_installer_base_url
  docker_repo_base_url       = var.docker_repo_base_url
  docker_repo_channel        = var.docker_repo_channel
  disable_api_termination    = var.disable_api_termination
  disable_api_stop           = var.disable_api_stop
  tags                       = var.tags
}
