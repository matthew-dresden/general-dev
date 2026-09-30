# remote-instances

Terragrunt deployment layer for the remote devcontainer instances. This
directory has no deployment of its own: it is the shared configuration every
deployment includes, plus one directory per instance. The `make help`
INSTANCES group drives an instance's whole lifecycle against these
directories -- `make instance-init` scaffolds the one file a new instance
needs, `make instance-plan` and `make instance-deploy` run the underlying
Terragrunt against it, `make instance-stop`/`instance-start` power the EC2
instance, and `make instance-destroy` tears it down and cleans up. The
Terraform module this layer deploys, and the full input surface it
accepts, is documented in
[`provider/aws/README.md`](../provider/aws/README.md). The operations-facing
checklist for a team that provisions the instance on the requester's behalf,
generated from the same variable surface, is
[`docs/ec2-requirements.md`](../docs/ec2-requirements.md). This document
covers neither: it is the reference for the directory layout under
`remote-instances/` itself, for the one file a new instance requires, and
for the sizing and state-model decisions the defaults encode.

## What a per-instance directory contains

Decision D6 (spec `devcontainer-platform.md` Section 13) separates the
module from its deployments: an instance is configured by writing an
`inputs` block, never by editing the module. Adding an instance is
`make instance-init INSTANCE=<name>`, which creates a directory under
`remote-instances/` holding exactly one scaffolded file, `terragrunt.hcl`,
and nothing else (Section 9); nothing outside the new directory changes.
The scaffold never deploys and never runs Terragrunt; it only pins a
default AMI (Canonical's current Ubuntu 24.04 arm64, resolved from SSM
unless `AMI=` names one), allocates a free CIDR block, and writes the
file below for you to edit. Re-running the target on an existing directory
is refused, so scaffolding can never silently replace edits already made.

A per-instance directory holds exactly one file, `terragrunt.hcl`, with three
parts -- this file is the contract `make instance-init` scaffolds and what a
user edits to change the deployment:

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
| Recorded EC2 id | `<certs-root>/<name>/instance-id`, the same directory the certificates live in | `E6` (`make instance-link` writes it; `make instance-deploy` records the applied id automatically) |
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
an instance name into any one of these seven strings or paths -- is a
separate concern spec Section 4.5 assigns to exactly one Python module:
`.claude/plugins/devcontainer/scripts/devcontainer_config/instances.py`.
A script or skill that needs one of these values calls into that module
(or shells out to its `resolve-instance` entry point) instead of
recomputing the pattern above independently; recomputing it a second time
is exactly the drift this table exists to prevent.

## Choosing which instance a command acts on

Every make target that reaches the remote engine accepts `INSTANCE`:

```sh
INSTANCE=personal make build
INSTANCE=personal make status
INSTANCE=personal make push-secrets
```

The name is resolved once per invocation, by
`devcontainer_config.cli resolve-instance`, which owns the resolution order:
an explicit `INSTANCE` wins; otherwise `DEFAULT_REMOTE_INSTANCE`; otherwise a
sole configured instance; and with several configured and no selector, the
command stops and names them rather than picking one.

Resolution deliberately does not happen when `make` parses this file. It shells
out, and doing it at parse time would run on every invocation, including
`make help` in a checkout with no instances configured at all.

If resolution fails, the error names both remedies:

```sh
INSTANCE=<name> make <target>          # name one for this command
export DEFAULT_REMOTE_INSTANCE=<name>  # or set a default for the shell
```

`make list-instances` shows what is available.

## Seeing what is configured

`make list-instances` lists every instance under `remote-instances/` with
its live state, one row per instance:

```text
INSTANCE  STATE    ID               PARAMS  CERTS  FORWARD  CONTEXT
acme      running  i-0abc123def456  yes     yes    49231    general-dev-acme
sandbox   stopped  -                no      no     -        absent
```

A dash means the probe could not answer -- no recorded id yet, no
certificate material yet, or an AWS or docker surface that did not reply --
rather than a confirmed no; a failed probe is also reported on stderr, one
line per row, and the command exits non-zero when any row's probes failed.
`PARAMS` and `CERTS` are the same status probes `make instance-deploy`
consults before it decides which trust-chain steps still need to run.
`FORWARD` is the local port the instance's docker context records, so one
listing shows at a glance which engines have forwards open.
`make instance-status INSTANCE=<name>` renders the same row for one
instance; `ALL=1` covers every instance; the underlying cli subcommands
(`python3 -m devcontainer_config.cli instance-list` / `instance-status`)
accept `--json` for one single-line JSON object per instance.

## Sizing: the default instance type

`make instance-init` scaffolds `instance_type = "c8g.xlarge"`: 4 vCPU and
8 GiB on Graviton4, the cheapest 4 vCPU / 8 GiB ARM instance in us-east-1,
priced at roughly $0.12 per hour. That is the default for a real engine --
the size a project's day-to-day container, image builds and test runs fit
in without tuning. For a throwaway engine that only proves a pipeline
works, the scaffold's comment names `t4g.medium` as the proven cheapest
size; sizes below it (2 GiB, `t4g.small`) OOM during devcontainer image
builds, which is why the floor sits where it does. The input is an
ordinary `inputs` entry: edit it in the instance's own `terragrunt.hcl`
and apply the edit with `make instance-deploy INSTANCE=<name>`.

## Remote state: one bucket per fleet

All instances of a repository, in one account and region, share one
remote-state bucket. `root.hcl` derives its name
(`tg-state-<account-id>-<region>-<repo-slug>-<suffix>`) from the account,
the region in `REMOTE_AWS_REGION`, the repository's git remote slug and a
committed suffix, so every instance computes the same name without any
per-instance file naming it. What is per instance is the state *key*:
`<name>/terraform.tfstate`, derived from the instance directory's own
path, so two instances never share state even though they share a bucket.

Terragrunt bootstraps the bucket itself -- versioning, encryption and
TLS enforcement included -- the first time any instance runs a command
with backend bootstrapping, which every Terragrunt-running make target
does on your behalf (`make instance-plan`'s help line names this). Because
the bucket is fleet-wide, it stands outside the instance lifecycle:
`make instance-destroy` removes the instance's Parameter Store parameters,
docker context, certificates and recorded id; its state *key* survives the
destroy (the record of what existed), and the bucket is never touched.
After the last instance of a fleet is destroyed, deleting the bucket is a
separate, manual step -- and one worth pausing over, since it takes every
instance's state history with it.

## The make targets, and the Terragrunt underneath them

Every instance-* target acts on one instance (`INSTANCE=`) or, with
`ALL=1`, every configured instance, running the underlying tooling in that
instance's own directory. `make instance-plan` runs `terragrunt plan` and
never applies; `make instance-deploy` runs validate, a guarded plan, apply
and the follow-up converge steps; `make instance-destroy` runs
`terragrunt destroy` and then cleans up everything Terragrunt does not
know about. The underlying mechanism, should you need it directly, is
ordinary Terragrunt in the instance's directory: `cd
remote-instances/<name>`, export `REMOTE_AWS_REGION` (the same
hard-required variable every make target demands), then `terragrunt init`,
`plan`, `apply` or `destroy`. The make targets exist so the sequence, its
guards (the replacement refusal in deploy, the `ALL=1` destroy
confirmation) and the cleanup that must follow a destroy are not re-typed
from memory.

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
fails too, with a non-zero exit. The file above is not copied from this
document, either: `make instance-init INSTANCE=<name>` scaffolds it,
honoring the contract described here (the two includes, and an `inputs`
block carrying only what genuinely differs for the one deployment), and
`gd-env-setup-remote` performs the same provisioning and certificate
steps while verifying each one. A developer edits the scaffolded file
afterwards; nobody types the
whole file from the example.
