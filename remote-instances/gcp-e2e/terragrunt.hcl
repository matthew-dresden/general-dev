# remote-instances/gcp-e2e/terragrunt.hcl
#
# The directory name IS this instance's identity, and instance names are
# project names (e.g. acme).
#
# Everything in the inputs block below is yours to edit freely: project,
# zone, VM size, volume sizes and tags. Apply any edit with:
#   make instance-deploy INSTANCE=gcp-e2e

include "root" {
  path = find_in_parent_folders("root-gcp.hcl")
}

include "envcommon" {
  path = "${dirname(find_in_parent_folders("root-gcp.hcl"))}/_envcommon/remote-gcp.hcl"
}

inputs = {
  # instances.sanitize_gcp_name of the instance name: a gcp VM name must
  # be a legal RFC1035 label, and the secrets prefix a later slice mints
  # is derived from this same sanitized value.
  instance_name = "gcp-e2e"
  name_prefix   = "gcp-e2e"

  project_id = "brimcapitalgroup"
  zone       = "us-east4-a"

  # Derived from the zone: us-east1-b means us-east1. Change the zone and
  # the region follows; it is never chosen separately.
  region = "us-east4"

  # e2-standard-2: 2 vCPU / 4 GiB, the sizing default for a
  # real engine on this kind.
  vm_size = "e2-standard-2"

  root_volume_size_gb = 30
  data_volume_size_gb = 30

  vpc_cidr   = "10.101.0.0/16"
  subnet_cidr = "10.101.1.0/24"

  tags = {
    environment = "gcp-e2e"
  }
}
