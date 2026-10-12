# provider/aws

Terraform module that provisions one remote devcontainer instance: a VPC and
public subnet (optional), a zero-ingress security group and IAM role
(optional), and the EC2 instance itself with its root volume and an optional
second data volume. Every option this module supports is an input; nothing is
selected by editing a `.tf` file (spec `devcontainer-platform.md` Section
5.6). The deployments that consume this module live under
`remote-instances/`; the module itself never contains a deployment (spec
Section 13, decision D6).

This document is the reference for the developer or agent writing the
Terragrunt `inputs` block that drives a deployment of this module. The
operations-facing counterpart, generated from the same variable surface for
an audience with no repository context, is `docs/ec2-requirements.md`.

## Composition

The root module (`remote-ec2-instance.tf`) owns no resource of its own. It is
exactly three `module` blocks:

- `module.network` (`modules/network`), instantiated only when
  `var.create_network` is `true`.
- `module.security` (`modules/security`), instantiated when either
  `var.create_security_group` or `var.create_iam_role` is `true`.
- `module.compute` (`modules/compute`), always instantiated.

Every identifier that crosses a submodule boundary is resolved once, in the
root module's `locals` block, from the submodule that created it when its
toggle is `true` or from the matching replacement input when it is `false`.
`module.compute` is written once and does not know which mode produced the
values it receives.

The compute submodule never takes a CIDR of any kind, and the network
submodule never takes an AMI: addressing and instance provisioning are
strictly separated, which is what makes the network substitutable behind
`create_network = false` (AC-10.11).

## Toggles

There are four `create_*` inputs. Each stops a specific piece of
infrastructure from being created and, when it does, names the replacement
input(s) that then become required.

`var.create_security_group` and `var.create_iam_role` must be set to the same
value. This narrows the two-column presentation in spec Section 5.6, which
lists them as independently settable; the shipped module rejects any mixed
setting at plan time (deviation recorded in this Task's Comments for the spec
owner). The reason is structural, not a stricter policy choice: the security
submodule creates the security group, the IAM role, its inline Parameter
Store policy, the AWS-managed SSM policy attachment and the instance profile
as a single unit with no internal toggle of its own (`E5-F1-S3`). Gating
`module.security`'s instantiation on either toggle alone would let a caller
disable one resource while the module still silently created it.

### create_network

Default `true`. Stops creating: the VPC, the public subnet, the internet
gateway and the public route table and its association (`module.network`'s
`count` becomes `0`).

When `false`, `var.vpc_id` and `var.subnet_id` are both required, and
`var.vpc_cidr`, `var.subnet_cidr` and `var.availability_zone` are ignored.
When `true` (the default), `var.vpc_id` and `var.subnet_id` must be left
unset, and `var.vpc_cidr`, `var.subnet_cidr` and `var.availability_zone` are
all required instead.

`var.vpc_id` is required whenever `create_network` is `false`, but it is only
*consumed* by the security submodule, and only when that submodule is
actually instantiated (`create_security_group` or `create_iam_role` is
`true`). When both security toggles are `false`, `module.security` has
`count = 0` and `var.vpc_id`'s value is not passed to anything; it is only
re-exported through `output "vpc_id"` so a second deployment can still reuse
it.

Plan-time validation messages:

- `var.vpc_cidr is required when var.create_network is true: supply the IPv4 CIDR block for the VPC the network submodule creates.`
- `var.subnet_cidr is required when var.create_network is true: supply the IPv4 CIDR block for the public subnet the network submodule creates.`
- `var.availability_zone is required when var.create_network is true: supply the AWS availability zone the network submodule's public subnet is created in.`
- `var.vpc_id is required when var.create_network is false: supply the identifier of the existing VPC this deployment reuses.`
- `var.vpc_id must be left unset when var.create_network is true: this deployment creates its own VPC, and also supplying var.vpc_id would leave it ambiguous which network is in use. Set var.create_network to false to reuse var.vpc_id instead.`
- `var.subnet_id is required when var.create_network is false: supply the identifier of the existing subnet this deployment reuses.`
- `var.subnet_id must be left unset when var.create_network is true: this deployment creates its own subnet, and also supplying var.subnet_id would leave it ambiguous which network is in use. Set var.create_network to false to reuse var.subnet_id instead.`

### create_security_group

Default `true`. Stops creating: the zero-ingress security group. Because the
security submodule creates the security group, the IAM role and the instance
profile as one unit, `create_security_group` and `create_iam_role` must be
set to the same value; see the note above the per-toggle subsections.

When `false`, `var.security_group_ids` is required and must not be empty, and
it is passed straight to the compute submodule. When `true` (the default),
`var.security_group_ids` must be left unset.

Whenever either `create_security_group` or `create_iam_role` is `true`,
`var.egress_cidr_blocks` and `var.instance_name` are also required, since
they configure the security group and the inline IAM policy the security
submodule creates.

Plan-time validation messages:

- `var.create_security_group and var.create_iam_role must be set to the same value: the security submodule creates the security group, IAM role and instance profile as a single unit with no internal toggle of its own, so setting only one of these two to false would still create the infrastructure the false toggle was meant to disable. Set both to true, or set both to false and supply var.security_group_ids and var.iam_instance_profile_name.`
- `var.security_group_ids is required and must not be empty when var.create_security_group is false: supply the identifiers of the existing security groups this deployment reuses.`
- `var.security_group_ids must be left unset when var.create_security_group is true: this deployment creates its own security group, and also supplying existing ones would leave it ambiguous which group is in use. Set var.create_security_group to false to reuse var.security_group_ids instead.`
- `var.egress_cidr_blocks is required and must not be empty when var.create_security_group or var.create_iam_role is true: supply the IPv4 CIDR blocks the security submodule's egress rule permits traffic to.`
- `var.instance_name is required when var.create_security_group or var.create_iam_role is true: supply the name of the instance this deployment provisions, used to scope the security submodule's inline IAM policy to this instance's Parameter Store prefix.`

### create_iam_role

Default `true`. Stops creating: the IAM role and the instance profile. Must
be set to the same value as `create_security_group`; see the note above the
per-toggle subsections.

When `false`, `var.iam_instance_profile_name` is required and it is passed
straight to the compute submodule. When `true` (the default),
`var.iam_instance_profile_name` must be left unset. `var.egress_cidr_blocks`
and `var.instance_name` become required under the same condition described
under `create_security_group` above, since both toggles gate the same
submodule instantiation.

Plan-time validation messages:

- `var.iam_instance_profile_name is required when var.create_iam_role is false: supply the name of the existing instance profile this deployment reuses.`
- `var.iam_instance_profile_name must be left unset when var.create_iam_role is true: this deployment creates its own role and profile, and also supplying an existing one would leave it ambiguous which profile is in use. Set var.create_iam_role to false to reuse var.iam_instance_profile_name instead.`

### create_data_volume

Default `true`. Stops creating: the second encrypted gp3 data volume and its
attachment in the compute submodule (`output "data_volume_id"` becomes
`null`). The instance is left with its root volume only.

No replacement identifier is required when this toggle is `false`: there is
no identifier to substitute, only a volume the instance does without.
Consequently this toggle carries no plan-time validation of its own; setting
it to `false` never fails a plan for a missing input.

## Inputs

Nine inputs default to `null` (unset) rather than declaring no default at
all: `vpc_cidr`, `subnet_cidr`, `availability_zone`, `vpc_id`, `subnet_id`,
`egress_cidr_blocks`, `instance_name`, `security_group_ids` and
`iam_instance_profile_name`. Each of those becomes required only under the
condition named in its row below. Five inputs declare no default and are
always required regardless of any toggle: `ami`, `instance_type`,
`root_volume_size_gb`, `docker_data_root` and `name_prefix`.

| Input | Type | Required | Default | Effect |
|---|---|---|---|---|
| `create_network` | `bool` | optional | `true` | Whether the network submodule creates the VPC, subnet, internet gateway and routes. |
| `create_security_group` | `bool` | optional | `true` | Whether the security submodule creates the zero-ingress security group. Must equal `create_iam_role`. |
| `create_iam_role` | `bool` | optional | `true` | Whether the security submodule creates the IAM role and instance profile. Must equal `create_security_group`. |
| `create_data_volume` | `bool` | optional | `true` | Whether the compute submodule creates, attaches and mounts the second gp3 data volume. |
| `vpc_cidr` | `string` | required when `create_network = true`; ignored otherwise | `null` | IPv4 CIDR block for the VPC the network submodule creates. |
| `subnet_cidr` | `string` | required when `create_network = true`; ignored otherwise | `null` | IPv4 CIDR block for the public subnet the network submodule creates; must be contained inside `vpc_cidr`. |
| `availability_zone` | `string` | required when `create_network = true`; ignored otherwise | `null` | AWS availability zone the network submodule's public subnet is created in. |
| `vpc_id` | `string` | required when `create_network = false`; must be unset when `true` | `null` | Identifier of an existing VPC this deployment reuses. Consumed by the security submodule only when it is instantiated; always re-exported via `output "vpc_id"`. |
| `subnet_id` | `string` | required when `create_network = false`; must be unset when `true` | `null` | Identifier of an existing subnet this deployment reuses, passed to the compute submodule. |
| `egress_cidr_blocks` | `list(string)` | required (non-empty) when `create_security_group` or `create_iam_role` is `true`; ignored when both are `false` | `null` | IPv4 CIDR blocks the security submodule's single egress rule permits traffic to. |
| `instance_name` | `string` | required when `create_security_group` or `create_iam_role` is `true`; ignored when both are `false` | `null` | Names the instance for the security submodule's inline IAM policy, which scopes read access to this instance's Parameter Store prefix. |
| `security_group_ids` | `list(string)` | required (non-empty) when `create_security_group = false`; must be unset when `true` | `null` | Identifiers of existing security groups this deployment reuses, passed to the compute submodule. |
| `iam_instance_profile_name` | `string` | required when `create_iam_role = false`; must be unset when `true` | `null` | Name of an existing IAM instance profile this deployment reuses, passed to the compute submodule. |
| `ami` | `string` | always required | none | AMI identifier the instance boots from. Never resolved by a data source. |
| `instance_type` | `string` | always required | none | EC2 instance type the instance is launched as. |
| `root_volume_size_gb` | `number` | always required | none | Size, in GiB, of the encrypted gp3 root volume. |
| `data_volume_size_gb` | `number` | optional | `100` | Size, in GiB, of the encrypted gp3 data volume. Read only when `create_data_volume = true`. |
| `data_volume_device_name` | `string` | optional | `"/dev/sdf"` | Device name the data volume is attached under. Read only when `create_data_volume = true`. |
| `docker_daemon_user` | `string` | optional | `"dockerd"` | Name of the dedicated, unprivileged Linux user the rootless docker daemon runs as. |
| `docker_data_root` | `string` | always required | none | Absolute path on the instance the rootless daemon uses as its data-root. |
| `docker_tls_listen_address` | `string` | optional | `"127.0.0.1"` | Loopback address the rootless daemon's mTLS listener binds to. Must be `127.0.0.1`. |
| `docker_tls_listen_port` | `number` | optional | `2376` | TCP port the rootless daemon's mTLS listener binds to on loopback. |
| `docker_repo_base_url` | `string` | optional | `"https://download.docker.com/linux/ubuntu"` | Base URL of the Docker apt repository the compute submodule's user data adds. |
| `docker_repo_channel` | `string` | optional | `"stable"` | Release channel of the Docker apt repository the compute submodule's user data adds. |
| `name_prefix` | `string` | always required | none | Prefix applied to the `Name` tag of every resource this composition creates, across all three submodules. |
| `tags` | `map(string)` | optional | `{}` | Common resource tags merged onto every resource this composition creates, across all three submodules. |

Every input any of the three submodules (`modules/network`,
`modules/security`, `modules/compute`) declares is redeclared here as a root
input of the same name and passed straight through, or resolved into a
`locals` value from a toggle and its matching replacement input. No submodule
input exists that is not represented in this table.

## Outputs

| Output | Consumed by |
|---|---|
| `instance_id` | The transport in `E6` (SSM session target) and the operator, to identify the instance an apply created. |
| `region` | The operator or a second deployment, to know which AWS region an apply ran against without having to already know it. |
| `availability_zone` | The operator, to confirm instance placement. |
| `docker_daemon_endpoint` | The SSM port forward in `E6`, which targets this mTLS endpoint and nothing else. |
| `vpc_id` | A second deployment's `var.vpc_id`, set with `create_network = false`, to reuse this deployment's network (AC-10.11). |
| `subnet_id` | A second deployment's `var.subnet_id`, set with `create_network = false`, to reuse this deployment's network (AC-10.11). |

The security submodule's own outputs (`security_group_id`, `iam_role_name`,
`iam_role_arn`, `iam_instance_profile_name`), the network submodule's
`route_table_id`, and the compute submodule's `data_volume_id` are not
re-exported by the root module. They remain internal to this composition; a
caller who needs to reuse a security group or instance profile passes the
replacement inputs above instead.

## Worked examples

Both examples use obviously-fake placeholder identifiers, never values from a
real account.

### New network (`create_network = true`, the default)

```hcl
# terragrunt.hcl
inputs = {
  create_network         = true
  create_security_group  = true
  create_iam_role        = true
  create_data_volume     = true

  vpc_cidr          = "10.0.0.0/16"
  subnet_cidr       = "10.0.1.0/24"
  availability_zone = "us-east-1a"

  egress_cidr_blocks = ["0.0.0.0/0"]
  instance_name      = "EXAMPLE-devcontainer-remote"

  ami                  = "ami-EXAMPLE00000000"
  instance_type        = "t3.large"
  root_volume_size_gb  = 50
  docker_data_root     = "/mnt/docker-data"
  name_prefix          = "EXAMPLE-devcontainer-remote"

  tags = {
    Environment = "EXAMPLE"
  }
}
```

### Reused network (`create_network = false`), a second deployment

The `vpc_id` and `subnet_id` values below are the `vpc_id` and `subnet_id`
outputs of a prior deployment of this module (AC-10.11):

```hcl
# terragrunt.hcl
inputs = {
  create_network         = false
  create_security_group  = true
  create_iam_role        = true
  create_data_volume     = true

  vpc_id    = "vpc-EXAMPLE00000000"
  subnet_id = "subnet-EXAMPLE0000000"

  egress_cidr_blocks = ["0.0.0.0/0"]
  instance_name      = "EXAMPLE-devcontainer-remote-2"

  ami                  = "ami-EXAMPLE00000000"
  instance_type        = "t3.large"
  root_volume_size_gb  = 50
  docker_data_root     = "/mnt/docker-data"
  name_prefix          = "EXAMPLE-devcontainer-remote-2"

  tags = {
    Environment = "EXAMPLE"
  }
}
```
