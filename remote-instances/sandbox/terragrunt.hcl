# The sandbox instance: the smallest deployment this repository provisions,
# created to exercise the remote engine end to end (build, verify, teardown)
# during platform work, and kept as the reference example of a real
# per-instance file. t4g.medium (4 GiB, ARM) is the smallest instance that
# reliably builds the full devcontainer image on the instance's own rootless
# daemon -- the feature installs compile CPython from source, which does not
# fit the 2 GiB of a t4g.small. It is not meant for development work.
include "root" {
  path = find_in_parent_folders("root.hcl")
}

include "envcommon" {
  path = "${dirname(find_in_parent_folders("root.hcl"))}/_envcommon/remote-ec2.hcl"
}

inputs = {
  instance_name = "sandbox"
  name_prefix   = "sandbox"

  # Ubuntu 24.04 LTS, arm64, gp3. Resolved at creation time from Canonical's
  # public SSM parameter (aws ssm get-parameter --name
  # /aws/service/canonical/ubuntu/server/24.04/stable/current/arm64/hvm/ebs-gp3/ami-id
  # --region us-east-1) and pinned here: the module takes an explicit AMI id
  # and resolves nothing itself, and a pinned id keeps this deployment
  # reproducible while the moving "current" pointer advances.
  ami           = "ami-0bec8cef5313300ad"
  instance_type = "t4g.medium"

  # Big enough for the base image, the built devcontainer image and the
  # workspace volume; both are gp3 and die with the instance.
  root_volume_size_gb = 30
  data_volume_size_gb = 30

  vpc_cidr           = "10.33.0.0/16"
  subnet_cidr        = "10.33.1.0/24"
  availability_zone  = "us-east-1a"
  egress_cidr_blocks = ["0.0.0.0/0"]

  tags = {
    Environment = "sandbox"
  }
}
