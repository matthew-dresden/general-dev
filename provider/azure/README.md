# provider/azure

Terraform module that provisions one remote devcontainer engine on azure: a
resource group, virtual network and subnet (optional), the platform unit --
zero-inbound network security group, user-assigned identity, Key Vault,
relay namespace and hybrid connection -- (optional), and the Linux virtual
machine itself with its root disk and an optional second data disk. Every
option this module supports is an input; nothing is selected by editing a
`.tf` file. The deployments that consume this module live under
`remote-instances/`; the module itself never contains a deployment (spec
Section 13, decision D6).

This is the azure twin of `provider/aws`, and it mirrors that module's
composition, its toggle discipline and its comment structure deliberately.
This document is the reference for the developer or agent writing the
Terragrunt `inputs` block that drives a deployment of this module. The
state backend the deployments use is configured by
`remote-instances/root-azure.hcl` (the azure root), whose side-car handoff
with `devcontainer_config.state_store` that file's own header documents.

## Composition

The root module (`remote-azure-vm.tf`) owns no resource of its own. It is
exactly three `module` blocks:

- `module.network` (`modules/network`), instantiated only when
  `var.create_network` is `true`.
- `module.security` (`modules/security`), instantiated only when
  `var.create_platform` is `true`.
- `module.compute` (`modules/compute`), always instantiated.

Every identifier that crosses a submodule boundary is resolved once, in the
root module's `locals` block, from the submodule that created it when its
toggle is `true` or from the matching replacement input when it is `false`.
`module.compute` is written once and does not know which mode produced the
values it receives.

The compute submodule never takes a CIDR of any kind, and the network
submodule never takes an image URN: addressing and instance provisioning
are strictly separated, which is what makes the network substitutable
behind `create_network = false`.

## Toggles

There are three `create_*` inputs, where the aws composition has four by
one structural decision: the azure security submodule is ONE platform unit
(the network security group and its subnet association, the user-assigned
identity, the Key Vault, the relay namespace and hybrid connection, and
their role assignments), so there is exactly one toggle for it. The unit's
resources reference one another -- the association needs the group, the
hybrid connection needs the namespace, the role assignments need the vault
and the namespace -- so partial creation would be a different module, not
this one with fewer resources, and there is no internal toggle to expose.

### create_network

Default `true`. Stops creating: the resource group, the virtual network
and the subnet (`module.network`'s `count` becomes `0`).

When `false`, `var.resource_group_name` and `var.subnet_id` are both
required, and `var.vnet_cidr` and `var.subnet_cidr` are ignored. When
`true` (the default), `var.resource_group_name` and `var.subnet_id` must be
left unset, and `var.vnet_cidr` and `var.subnet_cidr` are required instead.

`var.resource_group_name` is required whenever `create_network` is `false`,
because every resource -- the platform unit's and the compute module's
alike -- lands in some resource group. The `vnet_id` output is `null` on
the reuse path: this deployment was given only a subnet, and the network it
lives in was never named to it.

Plan-time validation messages:

- `var.vnet_cidr is required when var.create_network is true: supply the IPv4 CIDR block for the virtual network the network submodule creates.`
- `var.subnet_cidr is required when var.create_network is true: supply the IPv4 CIDR block for the subnet the network submodule creates.`
- `var.resource_group_name is required when var.create_network is false: supply the name of the existing resource group this deployment deploys into.`
- `var.resource_group_name must be left unset when var.create_network is true: this deployment creates its own resource group, and also supplying var.resource_group_name would leave it ambiguous which group is in use. Set var.create_network to false to reuse var.resource_group_name instead.`
- `var.subnet_id is required when var.create_network is false: supply the identifier of the existing subnet this deployment reuses.`
- `var.subnet_id must be left unset when var.create_network is true: this deployment creates its own subnet, and also supplying var.subnet_id would leave it ambiguous which subnet is in use. Set var.create_network to false to reuse var.subnet_id instead.`

### create_platform

Default `true`. Stops creating: the platform unit -- the zero-inbound
network security group and its subnet association, the user-assigned
identity, the Key Vault, the relay namespace and hybrid connection, and
their role assignments.

When `false`, `var.identity_id`, `var.relay_namespace` and
`var.hybrid_connection` are all required, and are passed straight to the
compute submodule; `var.instance_name`,
`var.relay_sender_object_ids` and `var.keyvault_officer_object_ids` are
ignored. When `true` (the default), those three replacement inputs must be
left unset, and `var.instance_name` is required instead.

Note for the deploy flow: `make instance-deploy` writes the engine address
record from eight terraform outputs, including `vault_name` and `vault_uri`
(`provider/azure/outputs.tf`). A `create_platform = false` deployment has
no vault of this composition's, those two outputs are `null`, and the
deploy flow refuses to write a half-empty record -- so a reused-platform
deployment is wired by hand, not by the deploy flow. The documented reuse
path below reuses the network only and keeps the platform toggle `true`.

Plan-time validation messages:

- `var.instance_name is required when var.create_platform is true: supply the name of the instance this deployment provisions, used to name the relay hybrid connection the couple meets on.`
- `var.identity_id is required when var.create_platform is false: supply the identifier of the existing user-assigned identity this deployment reuses.`
- `var.identity_id must be left unset when var.create_platform is true: this deployment creates its own identity, and also supplying var.identity_id would leave it ambiguous which identity is in use. Set var.create_platform to false to reuse var.identity_id instead.`
- `var.relay_namespace is required when var.create_platform is false: supply the fully qualified host name of the existing relay namespace this deployment reuses.`
- `var.relay_namespace must be left unset when var.create_platform is true: this deployment creates its own relay namespace, and also supplying var.relay_namespace would leave it ambiguous which namespace is in use. Set var.create_platform to false to reuse var.relay_namespace instead.`
- `var.hybrid_connection is required when var.create_platform is false: supply the name of the existing hybrid connection this deployment reuses.`
- `var.hybrid_connection must be left unset when var.create_platform is true: this deployment creates its own hybrid connection, and also supplying var.hybrid_connection would leave it ambiguous which connection is in use. Set var.create_platform to false to reuse var.hybrid_connection instead.`

### create_data_volume

Default `true`. Stops creating: the second data disk and its attachment in
the compute submodule (`output "data_disk_id"` becomes `null`). The
instance is left with its root disk only.

No replacement identifier is required when this toggle is `false`: there is
no identifier to substitute, only a disk the instance does without.
Consequently this toggle carries no plan-time validation of its own;
setting it to `false` never fails a plan for a missing input.

## Inputs

Eight inputs default to `null` (unset) rather than declaring no default at
all: `vnet_cidr`, `subnet_cidr`, `resource_group_name`, `subnet_id`,
`instance_name`, `identity_id`, `relay_namespace` and
`hybrid_connection`. Each of those becomes required only under the
condition named in its row below. Four inputs declare no default and are
always required regardless of any toggle: `location`, `vm_size`,
`root_volume_size_gb`, `docker_data_root` and `name_prefix`.

| Input | Type | Required | Default | Effect |
|---|---|---|---|---|
| `create_network` | `bool` | optional | `true` | Whether the network submodule creates the resource group, virtual network and subnet. |
| `create_platform` | `bool` | optional | `true` | Whether the security submodule creates the platform unit: zero-inbound NSG and subnet association, identity, Key Vault, relay namespace and hybrid connection, role assignments. |
| `create_data_volume` | `bool` | optional | `true` | Whether the compute submodule creates, attaches and mounts the second data disk. |
| `location` | `string` | always required | none | Azure region every resource is created in. |
| `vnet_cidr` | `string` | required when `create_network = true`; ignored otherwise | `null` | IPv4 CIDR block for the virtual network the network submodule creates. |
| `subnet_cidr` | `string` | required when `create_network = true`; ignored otherwise | `null` | IPv4 CIDR block for the subnet the network submodule creates; must be contained inside `vnet_cidr`. |
| `resource_group_name` | `string` | required when `create_network = false`; must be unset when `true` | `null` | Name of an existing resource group this deployment deploys into. |
| `subnet_id` | `string` | required when `create_network = false`; must be unset when `true` | `null` | Identifier of an existing subnet this deployment reuses, passed to the security (association) and compute submodules. |
| `instance_name` | `string` | required when `create_platform = true`; ignored otherwise | `null` | Names the relay hybrid connection the couple meets on. |
| `identity_id` | `string` | required when `create_platform = false`; must be unset when `true` | `null` | Identifier of an existing user-assigned identity this deployment reuses, passed to the compute submodule. |
| `relay_namespace` | `string` | required when `create_platform = false`; must be unset when `true` | `null` | Fully qualified host name of an existing relay namespace this deployment reuses. |
| `hybrid_connection` | `string` | required when `create_platform = false`; must be unset when `true` | `null` | Name of an existing hybrid connection this deployment reuses. |
| `relay_sender_object_ids` | `list(string)` | optional | `[]` | Entra object ids granted the Azure Relay Sender role on the created namespace. Read only when `create_platform = true`. |
| `keyvault_officer_object_ids` | `list(string)` | optional | `[]` | Entra object ids granted the Key Vault Secrets Officer role on the created vault. Read only when `create_platform = true`. |
| `vm_size` | `string` | always required | none | Azure VM size the instance is launched as. |
| `os_image_urn` | `string` | optional | `"Canonical:ubuntu-24_04-lts:server-arm64:latest"` | Image URN in Publisher:Offer:Sku:Version notation. The arm64 default pairs with an arm64-capable size; an x64 size needs the x64 server URN. |
| `root_volume_size_gb` | `number` | always required | none | Size, in GiB, of the root disk. |
| `data_volume_size_gb` | `number` | optional | `100` | Size, in GiB, of the data disk. Read only when `create_data_volume = true`. |
| `docker_daemon_user` | `string` | optional | `"dockerd"` | Name of the dedicated, unprivileged Linux user the rootless docker daemon runs as. |
| `docker_data_root` | `string` | always required | none | Absolute path on the instance the rootless daemon uses as its data-root. |
| `docker_tls_listen_address` | `string` | optional | `"127.0.0.1"` | Loopback address the rootless daemon's mTLS listener binds to. Must be `127.0.0.1`. |
| `docker_tls_listen_port` | `number` | optional | `2376` | TCP port the rootless daemon's mTLS listener binds to on loopback. |
| `docker_repo_base_url` | `string` | optional | `"https://download.docker.com/linux/ubuntu"` | Base URL of the Docker apt repository the compute submodule's cloud-init adds. |
| `docker_repo_channel` | `string` | optional | `"stable"` | Release channel of the Docker apt repository the compute submodule's cloud-init adds. |
| `name_prefix` | `string` | always required | none | Prefix used to name every resource this composition creates. Validated: 1-18 lowercase alphanumeric or dash characters, beginning with a letter, not ending with a dash, so the derived `-vault` and `-relay` names stay inside azure's own naming laws. |
| `tags` | `map(string)` | optional | `{}` | Common resource tags merged onto every resource this composition creates, across all three submodules. |

Every input any of the three submodules (`modules/network`,
`modules/security`, `modules/compute`) declares is redeclared here as a
root input of the same name and passed straight through, or resolved into a
`locals` value from a toggle and its matching replacement input. No
submodule input exists that is not represented in this table.

## Outputs

The first eight are the deploy flow's contract: the azure branch of
`make instance-deploy` reads exactly these, by these names, from
`terragrunt output -json` and writes the engine address record from them.

| Output | Consumed by |
|---|---|
| `vm_name` | The engine address record, and every run-command surface that addresses the VM. |
| `vm_resource_id` | The engine address record. |
| `resource_group` | The engine address record. |
| `relay_namespace` | The engine address record; the relay transport dials it. |
| `hybrid_connection` | The engine address record; the relay couple meets on it. |
| `vault_name` | The engine address record; the secrets surfaces address the vault by it. `null` when `create_platform = false`. |
| `vault_uri` | The engine address record; the VM's material pulls are aimed at it. `null` when `create_platform = false`. |
| `location` | The operator, to know which region an apply ran against. |
| `vnet_id` | A second deployment's operator. `null` when `create_network = false`. |
| `subnet_id` | A second deployment's `var.subnet_id`, set with `create_network = false`, to reuse this deployment's network. |
| `identity_id` | A second deployment's `var.identity_id`, set with `create_platform = false`, to reuse this deployment's identity. |

The security submodule's own outputs (`nsg_id` alongside the re-exported
ones) and the network submodule's `resource_group_name` are internal to
this composition except as re-exported above; the compute submodule's
`data_disk_id` is likewise not re-exported.

## Worked examples

Both examples use obviously-fake placeholder identifiers, never values from
a real tenant.

### New network and platform (`create_network = true`, `create_platform = true`, the defaults -- what the scaffold writes)

```hcl
# terragrunt.hcl
inputs = {
  create_network  = true
  create_platform = true
  create_data_volume = true

  location    = "EXAMPLE-REGION"
  vnet_cidr   = "10.100.0.0/16"
  subnet_cidr = "10.100.1.0/24"

  instance_name      = "EXAMPLE-devcontainer-remote"
  relay_sender_object_ids    = []
  keyvault_officer_object_ids = []

  vm_size            = "Standard_D2pls_v5"
  root_volume_size_gb = 30
  docker_data_root   = "/mnt/docker-data"
  name_prefix        = "EXAMPLE-devcontainer-remote"

  tags = {
    Environment = "EXAMPLE"
  }
}
```

### Reused network (`create_network = false`), a second deployment

The `resource_group_name` and `subnet_id` values below are the
`resource_group` and `subnet_id` outputs of a prior deployment of this
module. The platform toggle stays `true`: this deployment gets its own
vault, namespace and identity, the same way the aws reuse example keeps its
own security toggles true.

```hcl
# terragrunt.hcl
inputs = {
  create_network  = false
  create_platform = true
  create_data_volume = true

  resource_group_name = "EXAMPLE-devcontainer-remote-rg"
  subnet_id           = "/subscriptions/EXAMPLE/subnets/EXAMPLE-subnet"

  instance_name      = "EXAMPLE-devcontainer-remote-2"

  vm_size            = "Standard_D2pls_v5"
  root_volume_size_gb = 30
  docker_data_root   = "/mnt/docker-data"
  name_prefix        = "EXAMPLE-devcontainer-remote-2"

  tags = {
    Environment = "EXAMPLE"
  }
}
```
