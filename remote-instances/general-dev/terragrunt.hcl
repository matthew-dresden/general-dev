# remote-instances/general-dev/terragrunt.hcl
#
# The directory name IS this instance's identity, and instance names are
# project names (e.g. brimbooks), never geographies or stages.
#
# Everything in the inputs block below is yours to edit freely: instance
# type, volume sizes, availability zone, tags and AMI. Apply any edit with:
#   make instance-deploy INSTANCE=general-dev

include "root" {
  path = find_in_parent_folders("root.hcl")
}

include "envcommon" {
  path = "${dirname(find_in_parent_folders("root.hcl"))}/_envcommon/remote-ec2.hcl"
}

inputs = {
  instance_name = "general-dev"
  name_prefix   = "general-dev"

  # Pinned at scaffold time from SSM parameter /aws/service/canonical/ubuntu/server/24.04/stable/current/arm64/hvm/ebs-gp3/ami-id
  # (region us-east-1, 2026-09-29).
  ami           = "ami-0bec8cef5313300ad"

  # c8g.xlarge: 4 vCPU / 8 GiB Graviton4, the cheapest 4 vCPU /
  # 8 GiB ARM instance in us-east-1 (~$0.12/hr) -- the default for a real
  # engine. t4g.medium is the proven cheapest size for a throwaway test
  # engine.
  instance_type = "t4g.medium"

  root_volume_size_gb = 30
  data_volume_size_gb = 30

  vpc_cidr           = "10.100.0.0/16"
  subnet_cidr        = "10.100.1.0/24"
  availability_zone  = "us-east-1a"
  egress_cidr_blocks = ["0.0.0.0/0"]

  # Both protection flags are false so a test engine can be torn down
  # without ceremony. Set them to true on an engine whose accidental stop
  # or termination would hurt (a real, long-lived project engine).
  disable_api_termination = false
  disable_api_stop        = false

  tags = {
    Environment = "general-dev"
  }
}
