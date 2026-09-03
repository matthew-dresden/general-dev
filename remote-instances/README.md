# remote-instances

Terragrunt deployment layer for the remote devcontainer instance. This
directory has no deployment of its own: it is the shared configuration every
deployment includes, plus a document for a developer or agent to write a new
one. The Terraform module this layer deploys, and the full input surface it
accepts, is documented in
[`provider/aws/README.md`](../provider/aws/README.md). The operations-facing
checklist for a team that provisions the instance on the requester's behalf,
generated from the same variable surface, is
[`docs/ec2-requirements.md`](../docs/ec2-requirements.md). This document
covers neither: it is the reference for the directory layout under
`remote-instances/` itself, and for the one file a new instance requires.

## What a per-instance directory contains

Decision D6 (spec `devcontainer-platform.md` Section 13) separates the
module from its deployments: an instance is configured by writing an
`inputs` block, never by editing the module. Adding an instance is adding a
directory under `remote-instances/` and nothing else (Section 9); nothing
outside the new directory changes.

A per-instance directory holds exactly one file, `terragrunt.hcl`, with three
parts:

- **`include "root"`**, resolving [`root.hcl`](./root.hcl). It supplies the
  remote state backend, the derived, reproducible state bucket name, the
  generated AWS provider block and the Terragrunt and engine version floors.
  No per-instance file sets any of those directly.
- **`include "envcommon"`**, resolving
  [`_envcommon/remote-ec2.hcl`](./_envcommon/remote-ec2.hcl). It points
  `terraform.source` at the module in `provider/aws` and fixes the module
  inputs every instance holds in common: the rootless Docker daemon's data
  root and account, its TLS listener, the apt repository it installs from,
  and the data volume's device name. See that file's own header comment for
  exactly what is, and is not, fixed there and why.
- **An `inputs` block**, supplying only what genuinely differs for this one
  deployment: the instance's identity (`instance_name`, `name_prefix`), its
  AMI, instance type and volume sizes, and whichever `create_*` toggle this
  deployment sets, together with that toggle's companion inputs (the
  creation-side CIDRs and availability zone when a toggle defaults to
  `true`, or the replacement identifiers of an existing resource when it is
  set to `false`).

The directory's own name is the instance name (Section 9), but only one
artifact derives from it automatically: `root.hcl` sets
`remote_state.config.key` from `path_relative_to_include()`, the including
directory's own path, so the state key alone is never typed. Every other
namespaced artifact in the table below is keyed by `var.instance_name`, an
ordinary module input this layer requires the `inputs` block to set by hand
(`provider/aws/modules/security/main.tf` scopes the inline IAM policy's
Parameter Store prefix from `var.instance_name`; `E6`'s Docker context and
certificate directory are documented to derive from the same value). The
`inputs` block below MUST set both `instance_name` and `name_prefix` to the
same string as the directory name: nothing in this layer checks that they
agree, so a directory renamed without also updating `instance_name` silently
splits the state key, which follows the directory, from the Parameter Store
prefix and every other instance-keyed artifact, which follow whatever
`instance_name` was typed, defeating the AC-9.1 namespacing this document
exists to guarantee.

The instance's AWS region and, where a named credential profile is used for
that instance's SSO login, its profile are not module inputs at all, and no
per-instance file sets them. `root.hcl` reads the region from
`REMOTE_AWS_REGION`, a hard requirement with no default: an unset value
aborts the render naming the variable, matching the same fail-fast treatment
`.devcontainer/remote-docker/lib.sh` already gives this variable
(`: "${REMOTE_AWS_REGION:?REMOTE_AWS_REGION must be set}"`). Export
`REMOTE_AWS_REGION` (and, for `aws sso login`, `REMOTE_AWS_PROFILE`) matching
the instance a command targets before running Terragrunt against that
instance's directory; both variables are the same ones
`.devcontainer/remote-docker` reads for the same instance. No `profile`
attribute appears anywhere in this layer's own configuration: the backend,
the generated provider and the account lookup all resolve credentials from
the same ambient AWS SDK chain, so the account a bucket name embeds and the
account that creates and writes it can never diverge (`root.hcl`'s own
header comment covers this in full).

## An example per-instance file

```hcl
# remote-instances/EXAMPLE-devcontainer-remote/terragrunt.hcl
include "root" {
  path = find_in_parent_folders("root.hcl")
}

include "envcommon" {
  path = "${dirname(find_in_parent_folders("root.hcl"))}/_envcommon/remote-ec2.hcl"
}

inputs = {
  instance_name = "EXAMPLE-devcontainer-remote"
  name_prefix   = "EXAMPLE-devcontainer-remote"

  ami           = "ami-EXAMPLE00000000"
  instance_type = "t3.large"

  root_volume_size_gb = 50
  data_volume_size_gb = 100

  vpc_cidr           = "10.0.0.0/16"
  subnet_cidr        = "10.0.1.0/24"
  availability_zone  = "us-east-1a"
  egress_cidr_blocks = ["0.0.0.0/0"]

  tags = {
    Environment = "EXAMPLE"
  }
}
```

This is the `create_network = true` (the default) case: `vpc_cidr`,
`subnet_cidr` and `availability_zone` are the creation-side companions of
that toggle. Reusing an existing network instead
(`create_network = false`) replaces all three with `vpc_id` and `subnet_id`,
the `vpc_id` and `subnet_id` outputs of a prior deployment of this module
(AC-10.11); the same substitution applies to `create_security_group` and
`create_iam_role` and their own replacement inputs. `provider/aws/README.md`
carries the full input reference, including every validation message a
missing or misplaced value produces, and both worked examples (new network,
reused network) this file's two variants are drawn from.

## Artifacts namespaced by instance name

An instance name keys every artifact below (spec Section 9). Adding a second
instance under a different directory whose `inputs` block also sets
`instance_name` and `name_prefix` to that new directory's name produces none
of these values in common with the first, with no edit to either existing
directory or to the shared configuration. The state key follows the
directory automatically; every other row follows `var.instance_name` because
the new directory's `inputs` block was written to match, not because this
layer enforces that agreement:

| Artifact | Pattern | Owned by |
|---|---|---|
| Terragrunt directory | `remote-instances/<name>/` | This layer |
| State key | `<name>/terraform.tfstate` | This layer (`root.hcl`, derived via `path_relative_to_include()`) |
| Docker context | `<repo-slug>-<name>` (`general-dev-<name>` in this repository) | `E6` |
| Parameter prefix | `/devcontainer/<name>/` | The security submodule's inline IAM policy (`provider/aws`), scoped from `var.instance_name` |
| Certificates | `$DOCKER_CONFIG/certs/<name>/`, or `~/.docker/certs/<name>/` when `DOCKER_CONFIG` is unset | `E6` |
| Local forwarded port | Allocated per instance, recorded, never a fixed number | `E6` |

The docker context row's Pattern is `<repo-slug>-<name>`, `repo.repo_slug`
(the same value `root.hcl`'s own `local.repo_slug` derives) prepended to the
instance name -- never a literal, so a fork of this repository under a
different name gets its own, non-colliding prefix without editing any of
this table's owners. `general-dev-<name>` above is this repository's own
worked example, not a fixed pattern to copy into a fork. The certificates
row's on-disk material root and this table's addressing value are the same
derivation, `devcontainer_config.instances.certs_root` --
`devcontainer_config.certs.DEFAULT_CERTS_ROOT` is sourced from it -- so an
operator who has set `DOCKER_CONFIG` never has certificates written under
one directory while this table points at another.

"Owned by" above names what creates or manages each artifact at runtime
(Terragrunt, the security submodule's inline IAM policy, `E6`'s transport
and certificate modules). Deriving the value programmatically -- turning
an instance name into any one of these six strings or paths -- is a
separate concern spec Section 4.5 assigns to exactly one Python module:
`.claude/plugins/devcontainer/scripts/devcontainer_config/instances.py`.
A script or skill that needs one of these values calls into that module
(or shells out to its `resolve-instance` entry point) instead of
recomputing the pattern above independently; recomputing it a second time
is exactly the drift this table exists to prevent.

## Reusing an existing network: `create_network = false`

An operations team that already owns a VPC does not have to let this module
build another one. Setting `create_network = false` switches the root module
from creating a network to attaching to one, and the compute and security
submodules are unchanged either way: identifiers are passed in, never looked up.

Turning the toggle off makes two inputs required that are otherwise unused:

| Input | Required when | Why |
|-------|---------------|-----|
| `vpc_id` | `create_network = false` | The security group is created in this VPC |
| `subnet_id` | `create_network = false` | The instance is launched into this subnet |

**The refusal happens at plan time, and it names the input that is missing.**
This is deliberate: a missing identifier surfaces before anything is created,
and the message says which one rather than failing generically at apply. With
neither supplied, the plan reports both:

```text
Error: Invalid value for variable
  var.vpc_id is null
  var.vpc_id is required when var.create_network is false: supply the ...
  var.subnet_id is null
  var.subnet_id is required when var.create_network is false: supply the ...
```

Supply `vpc_id` alone and the plan still refuses, naming only `subnet_id`.

A deployment in this mode creates no networking resource at all. Against the
same module, a network-creating deployment plans 13 resources and a
network-reusing one plans 8: the VPC, internet gateway, subnet, route table and
route-table association are simply absent, and what remains is the instance, its
data volume and attachment, the security group, and the IAM role, inline policy,
managed-policy attachment and instance profile.

Attaching does not adopt. A second deployment placed into a first deployment's
network leaves that first deployment's state alone, and re-planning it after the
second one applies reports `No changes.` The two remain separate deployments
that happen to share a network, each with its own state key, docker context,
parameter prefix, certificate directory and forwarded port, as described under
"Artifacts namespaced by instance name" above.

## No instance directory is committed

No instance directory exists in this repository, and none is added by this
document. Under the resolution order in Section 4.1.1, a single directory
under `remote-instances/` becomes the implicit default for every remote make
target once `INSTANCE` and `DEFAULT_REMOTE_INSTANCE` are both unset;
committing one here would hand every fresh clone a default instance
belonging to somebody else, with a name they did not choose and a region
they may not use. An empty `remote-instances/` directory on a remote backend
fails too, with a non-zero exit: Section 4.1.1 directs the operator to
`/devcontainer:setup-remote`, which is also where the file above comes
from. The skill collects an instance's identity, its network and
security choices and its sizing through an interview and writes the
per-instance file from the answers, rather than a developer copying and
hand-editing the example above.
