# The gcp root: the shared configuration every gcp instance directory under
# remote-instances/ includes (through `_envcommon/remote-gcp.hcl`, which
# targets this file).
#
# THIS FILE IS A MINIMAL STUB. The gcp provider slice that lands with the
# next change completes it -- the remote_state block reading the side-car,
# the provider generation, the version floors -- exactly the way
# root-azure.hcl is the azure root's completed form. What is here now, and
# all that exists now, are the two locals the state-store bootstrap
# (`devcontainer_config.state_store_gcp`) parses:
#
# - `state_store_suffix`: the committed suffix. Like the azure root's own
#   suffix, it is generated once and committed, never regenerated -- a
#   fresh clone with no local state still composes the same bucket name
#   and finds the bucket an earlier ensure provisioned instead of creating
#   an orphaned second one. If the suffix is ever absent, nothing invents
#   a replacement: state_store_gcp refuses, and a new suffix would strand
#   whatever state the old one pointed at.
# - `state_bucket_template`: the bucket name template the bootstrap
#   substitutes one `${local.NAME}` token per component into. The instance
#   component is the sanitized instance name (the one sanitizer the gcp
#   kind names its VM with) joined to a deterministic 8-hex digest of
#   instance, zone, project and suffix, so the composed name has the fixed
#   shape `gdstate-<sanitized>-<8 hex>`: collision-proof without a random
#   value, and legal under GCS's own naming law.

locals {
  state_store_suffix     = "q4m7zk"
  state_bucket_template  = "gdstate-${local.instance_name}"
}
