# Included by every instance directory under remote-instances/. Configures
# the remote state backend, generates the AWS provider block, and declares
# the tool version floors Section 6 requires. See spec Section 5.7 (state
# bucket naming), Section 6 (version and interoperability semantics) and
# Section 13 decision D7 (deterministic name plus a committed suffix).
#
# D14 (spec Section 13) records the operator's chosen engine as Terraform.
# Spec Section 6.1 proposes adding the devcontainer feature
# ghcr.io/devcontainers/features/terraform:1 to install it, but that feature
# is not yet present in .devcontainer/devcontainer.json (verified by reading
# the file's `features` block and by grepping terraform, terragrunt, tflint
# and tofu across .devcontainer/Dockerfile, project-setup.sh and
# postcreate-wrapper.sh, all empty); Section 6.1 marks it a change to a
# critical file "applied only after review," which E5-F1-S1-T1 deferred when
# it resolved D14 as decision-only. Terragrunt's own default binary is
# "tofu" on PATH (verified against the installed Terragrunt 1.1.4: `terragrunt
# run --help` documents "Default is tofu (on PATH)", and with a stub tofu
# executable placed on PATH, `terragrunt info print` reports
# terraform_binary=tofu). Because neither the devcontainer image nor
# Terragrunt's own default guarantees Terraform is what actually runs,
# terraform_binary below states D14's engine choice explicitly: the pin is
# the guarantee, not a restatement of one the image already provides.
terraform_binary = "terraform"

locals {
  # Region for both the state backend and the generated provider. Read from
  # the environment, with no default, so this file drives every account and
  # region without being edited and fails loudly rather than silently naming
  # a bucket under the wrong region when unset. REMOTE_AWS_REGION is the same
  # variable .devcontainer/remote-docker reads for the same instance
  # (shell.env.example); get_env(NAME) with a single argument throws if the
  # variable is not set, matching the fail-fast treatment
  # .devcontainer/remote-docker/lib.sh:69 already gives this variable
  # (`: "${REMOTE_AWS_REGION:?REMOTE_AWS_REGION must be set}"`).
  aws_region = get_env("REMOTE_AWS_REGION")

  # No aws_profile local exists here on purpose. get_aws_account_id() below
  # always resolves the account through the ambient AWS SDK credential chain
  # (AWS_PROFILE, or access-key environment variables) and has no parameter
  # to pin it to a named profile; if the S3 backend and the generated
  # provider carried an explicit `profile` while the account lookup did not,
  # the account embedded in the bucket NAME could come from one identity
  # while the bucket is created and written under another, with nothing
  # failing. Rather than accept that divergence, backend, provider and
  # account lookup all resolve from the exact same ambient chain: no
  # `profile` attribute is set anywhere in this file. That also matches this
  # Task's Definition of Ready, which resolves credentials from the process
  # environment rather than a named SSO profile. REMOTE_AWS_PROFILE remains
  # in use elsewhere (.devcontainer/remote-docker, for `aws sso login
  # --profile`), a genuinely different, SSO-based credential flow this file
  # does not participate in.

  # Globally unique, resolved at runtime, so a collision with another
  # account's state bucket is impossible (spec Section 5.7, decision D7).
  account_id = get_aws_account_id()

  # Derived from the git remote rather than typed or read from the local
  # checkout directory name, which a developer is free to rename. basename()
  # splits on the last "/", which lands after the org/user segment for both
  # the HTTPS form (https://host/org/repo.git) and the SSH form
  # (git@host:org/repo.git); trimsuffix() removes the trailing ".git" either
  # form leaves.
  repo_slug = trimsuffix(
    basename(run_cmd("--terragrunt-quiet", "git", "config", "--get", "remote.origin.url")),
    ".git",
  )

  # Generated once on first bootstrap and committed here; never regenerated.
  # Committing it, rather than deriving it, is what makes the bucket name
  # reproducible (spec Section 5.7, decision D7): a fresh clone with no
  # local state still computes this same suffix and finds the bucket that
  # already exists instead of creating an orphaned second one. If this
  # value is ever absent, nothing here invents a replacement: a new suffix
  # means a new, empty bucket, which strands whatever state the missing
  # suffix used to point at.
  state_bucket_suffix = "8f2ac1"

  # tg-state-<account-id>-<region>-<repo-slug>-<suffix>, in the order
  # Section 5.7 states. Every component is derived except the suffix, which
  # is a literal by design (see above).
  state_bucket_name = "tg-state-${local.account_id}-${local.aws_region}-${local.repo_slug}-${local.state_bucket_suffix}"
}

# Section 6 floors, enforced by Terragrunt itself at parse time and asserted
# again by a test in E5-F2-S1-T2 so the floor is proven, not merely
# declared. Terragrunt below 1.1.3 does not reliably combine use_lockfile
# with backend bootstrap (fixed in gruntwork-io/terragrunt PR #5665); the
# engine below 1.10 has no use_lockfile at all, so native S3 locking would
# silently not happen rather than failing loudly.
terraform_version_constraint  = ">= 1.10"
terragrunt_version_constraint = ">= 1.1.3"

remote_state {
  backend = "s3"

  # Terragrunt bootstraps this bucket itself the first time any instance
  # directory that includes this file runs `terragrunt backend bootstrap`
  # (or any run command with --backend-bootstrap) -- versioning, server-side
  # encryption and TLS enforcement included. No aws_s3_bucket resource
  # exists anywhere in this repository; declaring one here would race
  # Terragrunt's own bootstrap over the same bucket.
  generate = {
    path      = "backend.tf"
    if_exists = "overwrite_terragrunt"
  }

  config = {
    bucket = local.state_bucket_name
    region = local.aws_region

    # No `profile` attribute: the backend resolves credentials from the same
    # ambient AWS SDK chain local.account_id does (see the locals block
    # above), so the account the bucket name embeds and the account that
    # creates and writes the bucket can never diverge.

    # Derived from the including instance directory, never typed, so it
    # cannot disagree with the directory it names (spec Section 9).
    key = "${path_relative_to_include()}/terraform.tfstate"

    encrypt = true

    # Native S3 state locking (spec Section 6): no DynamoDB table is
    # created, paid for or forgotten.
    use_lockfile = true

    s3_bucket_tags = {
      Name = local.state_bucket_name
    }
  }
}

# No instance directory under remote-instances/ declares a provider block of
# its own; it is generated here, once, from the same region the backend
# uses. No `profile` here either, for the same reason the backend config
# above has none: the provider resolves credentials from the same ambient
# AWS SDK chain as local.account_id and the backend, so name and state can
# never point at different accounts.
generate "provider" {
  path      = "provider.tf"
  if_exists = "overwrite_terragrunt"

  contents = <<-EOF
    provider "aws" {
      region = "${local.aws_region}"
    }
  EOF
}
