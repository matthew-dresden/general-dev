# Compute module: the instance itself, its encrypted gp3 root and data
# volumes, and the templated user data that turns a bare Ubuntu host into
# the rootless docker engine this program talks to. See Section 5.6 of
# repos/spec/devcontainer-platform.md.
#
# Deliberately absent, by design rather than by omission: no CIDR input and
# no data source looking up an AMI, a subnet or a VPC. Addressing belongs
# to the network module (E5-F1-S2); this module consumes identifiers rather
# than deriving them, which is what allows the network module to be absent
# entirely when a caller supplies its own subnet and security groups.
#
# IMDSv2 is required with a hop limit of 2 (Section 1.5): hop limit 1 would
# stop a containerized process from reaching instance credentials through
# the host's network namespace at all, which the rootless daemon's
# containers need one hop to do. Termination and stop protection guard
# against the developer's engine being removed by a stray console action.

resource "aws_instance" "this" {
  ami                    = var.ami
  instance_type          = var.instance_type
  subnet_id              = var.subnet_id
  vpc_security_group_ids = var.security_group_ids
  iam_instance_profile   = var.iam_instance_profile_name

  disable_api_termination = var.disable_api_termination
  disable_api_stop        = var.disable_api_stop

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 2
  }

  root_block_device {
    volume_type           = "gp3"
    volume_size           = var.root_volume_size_gb
    encrypted             = true
    delete_on_termination = true

    tags = merge(var.tags, {
      Name = "${var.name_prefix}-root"
    })
  }

  user_data = templatefile("${path.module}/user-data.yaml", {
    daemon_user          = var.docker_daemon_user
    data_root            = var.docker_data_root
    tls_listen_address   = var.docker_tls_listen_address
    tls_listen_port      = var.docker_tls_listen_port
    create_data_volume   = var.create_data_volume
    docker_repo_base_url = var.docker_repo_base_url
    docker_repo_channel  = var.docker_repo_channel
  })

  tags = merge(var.tags, {
    Name = var.name_prefix
  })
}

# The data volume, its attachment and its device name exist only when the
# caller opts in; a deployment that wants root-volume-only storage gets
# exactly that, with no aws_ebs_volume or aws_volume_attachment declared at
# all rather than one that is merely unused.
resource "aws_ebs_volume" "data" {
  count = var.create_data_volume ? 1 : 0

  availability_zone = aws_instance.this.availability_zone
  size              = var.data_volume_size_gb
  type              = "gp3"
  encrypted         = true

  tags = merge(var.tags, {
    Name = "${var.name_prefix}-data"
  })
}

resource "aws_volume_attachment" "data" {
  count = var.create_data_volume ? 1 : 0

  device_name = var.data_volume_device_name
  volume_id   = aws_ebs_volume.data[0].id
  instance_id = aws_instance.this.id
}
