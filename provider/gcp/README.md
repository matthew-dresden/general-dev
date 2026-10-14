# provider/gcp

Terraform module that provisions one remote devcontainer engine on gcp: a custom-mode virtual network, subnetwork, Cloud Router and Cloud NAT, and the IAP ingress firewall (optional), the platform unit (service account and the six per-instance secrets with their secretAccessor bindings, optional), and the Linux virtual machine itself with its root disk and an optional second data disk.

- Every option this module supports is an input; nothing is selected by editing a `.tf` file.
- The deployments that consume this module live under `remote-instances/`; the module itself never contains a deployment (spec Section 13, decision D6).

This is the gcp twin of `provider/azure`, and it mirrors that module's composition, its toggle discipline and its comment structure deliberately.

- This document is the reference for the developer or agent writing the Terragrunt `inputs` block that drives a deployment of this module.
- The state backend the deployments use is configured by `remote-instances/root-gcp.hcl` (the gcp root), whose side-car handoff with `devcontainer_config.state_store_gcp` that file's own header documents.

## The transport and the ingress doctrine

The engine is reached through google's Identity-Aware Proxy: `gcloud compute start-iap-tunnel` dials the VM's nic0 from google's front end, and google authenticates the operator before any packet reaches the network. That wire contract (the `transport_gcp` module's) dictates three composition-wide facts:

- The instance has **no external address**: its network interface declares no `access_config`, so none is assigned.
- The daemon binds **0.0.0.0**, not loopback -- the tunnel terminates on nic0, an address a loopback bind would never answer. `docker_tls_listen_address` is validated to be exactly `0.0.0.0` (the azure twin validates the opposite, `127.0.0.1`, for its relay doctrine; each module validates its own transport's).
- The network's inbound story is **one firewall rule**: tcp on the daemon's TLS port from `35.235.240.0/20` (google's documented IAP range), targeted at the instance's network tag. A custom-mode VPC carries no implied allow rules, so the posture fails closed until and unless that rule exists.

Outbound reachability is the Cloud Router and Cloud NAT the network module declares: an internal-only instance has no egress path on gcp until a NAT gateway exists (the azure twin's `default_outbound_access_enabled` pin exists for the same reason).

## Composition

The root module (`remote-gcp-vm.tf`) owns no resource of its own. It is exactly three `module` blocks:

- `module.network` (`modules/network`), instantiated only when `var.create_network` is `true`.
- `module.platform` (`modules/platform`), instantiated only when `var.create_platform` is `true`.
- `module.compute` (`modules/compute`), always instantiated.

- Every identifier that crosses a submodule boundary is resolved once, in the root module's `locals` block, from the submodule that created it when its toggle is `true` or from the matching replacement input when it is `false`.
- `module.compute` is written once and does not know which mode produced the values it receives.

The compute submodule never takes a CIDR of any kind, and the network submodule never takes an image: addressing and instance provisioning are strictly separated, which is what makes the network substitutable behind `create_network = false`.

## Toggles

There are three `create_*` inputs, the same count as the azure composition, by the same structural decision: the gcp platform submodule is ONE platform unit (the service account the instance assumes and the six per-instance secrets with their secretAccessor bindings), so there is exactly one toggle for it.

- The unit's resources reference one another: every binding needs both the secret and the account.
- Partial creation would be a different module, not this one with fewer resources, and there is no internal toggle to expose.

### create_network

Default `true`. Stops creating: the virtual network, the subnetwork, the Cloud Router, the Cloud NAT and the IAP firewall (`module.network`'s `count` becomes `0`).

- When `false`, `var.subnet_id` is required, and `var.vpc_cidr` and `var.subnet_cidr` are ignored.
- When `true` (the default), `var.subnet_id` must be left unset, and `var.vpc_cidr` and `var.subnet_cidr` are required instead.

- No second network reuse input exists: a gcp interface attaches to a subnetwork whose identifier carries its own network, so `var.subnet_id` (id or self-link form) is the whole reuse surface.
- A `create_network = false` deployment owns its own ingress and egress posture: the IAP firewall and the Cloud NAT are this module's creations, not gcp defaults, and a reused network brings neither with it.
- The `vpc_id` output is `null` on the reuse path: this deployment was given only a subnetwork, and the network it lives in was never named to it.

Plan-time validation messages:

- `var.vpc_cidr is required when var.create_network is true: supply the IPv4 CIDR block for the virtual network the network submodule creates.`
- `var.subnet_cidr is required when var.create_network is true: supply the IPv4 CIDR block for the subnetwork the network submodule creates.`
- `var.subnet_id is required when var.create_network is false: supply the identifier of the existing subnetwork this deployment reuses.`
- `var.subnet_id must be left unset when var.create_network is true: this deployment creates its own subnetwork, and also supplying var.subnet_id would leave it ambiguous which subnetwork is in use. Set var.create_network to false to reuse var.subnet_id instead.`

### create_platform

Default `true`. Stops creating: the service account and the six per-instance secrets with their secretAccessor bindings (`module.platform`'s `count` becomes `0`).

- When `false`, `var.sa_email` is required and is passed straight to the compute submodule as the account the instance assumes.
- When `true` (the default), `var.sa_email` must be left unset, and the unit creates the account and the secrets itself.

- The secret vehicles are created here, never their contents: the values are written exclusively by the laptop-side publication flows (`make cert-publish` for the TLS material and its checksum, push-secrets for the two configuration payloads). An empty secret is the pre-publish state the VM's startup script tolerates.
- The account is scoped by the per-secret bindings alone -- it carries no project-wide IAM role -- so a reused account must already carry `roles/secretmanager.secretAccessor` on whatever secrets it should read; this composition grants nothing outside the unit it creates.
- The instance boots only after the bindings exist (`module.compute` depends on `module.platform`): the first boot's startup script fetches the TLS material through exactly those bindings.

Plan-time validation messages:

- `var.sa_email is required when var.create_platform is false: supply the email of the existing service account this deployment reuses.`
- `var.sa_email must be left unset when var.create_platform is true: this deployment creates its own service account, and also supplying var.sa_email would leave it ambiguous which account is in use. Set var.create_platform to false to reuse var.sa_email instead.`

### create_data_volume

Default `true`. Stops creating: the second data disk and its attachment in the compute submodule (`output "data_disk_id"` becomes `null`). The instance is left with its root disk only.

- No replacement identifier is required when this toggle is `false`: there is no identifier to substitute, only a disk the instance does without.
- Consequently this toggle carries no plan-time validation of its own; setting it to `false` never fails a plan for a missing input.

## The two boot payloads

The instance's metadata carries two payloads, and the split is the design:

- `user-data` -- the rendered `boot-user-data.yaml` cloud-init -- runs **once** at first boot: rootless docker installed, the rootful daemon disabled with a FATAL assert, sshd disabled, the data disk formatted and mounted from its deterministic `/dev/disk/by-id/google-<name>` handle, `daemon.json` written (the listener on `0.0.0.0:<port>`, `tlsverify` on, paths under the daemon user's `tls/` directory), the daemon enabled but never started, and the `/etc/general-dev-provisioned` marker written last. It fetches nothing: a first boot may precede the first publish.
- `startup-script` -- plain text, far under the metadata value cap -- runs at **every** boot: it waits for the marker, fetches the three TLS secrets and the checksum secret from Secret Manager through the metadata-server token (passed to the API call on stdin, never in argv), compares the fetched checksum against the `general-dev/tls-checksum` guest attribute, applies the material and restarts the daemon only on drift, records `general-dev/provisioned=TRUE` on every success path, and records `general-dev/error` and exits 1 on any failure. Those attributes are what the laptop-side waits (`transport_gcp.wait_provisioned`, `wait_applied_checksum`) gate on.

The TLS material itself is never created by terraform: the platform unit creates empty secret vehicles, and the publication flows fill them.

## Inputs

Four inputs default to `null` (unset) rather than declaring no default at all: `vpc_cidr`, `subnet_cidr`, `subnet_id` and `sa_email`. Each of those becomes required only under the condition named in its row below.

Eight inputs declare no default and are always required regardless of any toggle: `region`, `instance_name`, `vm_size`, `root_volume_size_gb`, `docker_data_root`, `project_id`, `zone` and `name_prefix`.

| Input | Type | Required | Default | Effect |
|---|---|---|---|---|
| `create_network` | `bool` | optional | `true` | Whether the network submodule creates the virtual network, subnetwork, Cloud Router, Cloud NAT and IAP firewall. |
| `create_platform` | `bool` | optional | `true` | Whether the platform submodule creates the service account and the six per-instance secrets with their secretAccessor bindings. |
| `create_data_volume` | `bool` | optional | `true` | Whether the compute submodule creates, attaches and mounts the second data disk. |
| `region` | `string` | always required | none | GCP region the regional resources are created in; the scaffold derives it from the zone. |
| `vpc_cidr` | `string` | required when `create_network = true`; ignored otherwise | `null` | IPv4 CIDR block for the virtual network the network submodule creates. |
| `subnet_cidr` | `string` | required when `create_network = true`; ignored otherwise | `null` | IPv4 CIDR block for the subnetwork the network submodule creates; must be contained inside `vpc_cidr`. |
| `subnet_id` | `string` | required when `create_network = false`; must be unset when `true` | `null` | Identifier of an existing subnetwork this deployment reuses, passed to the compute submodule. |
| `sa_email` | `string` | required when `create_platform = false`; must be unset when `true` | `null` | Email of an existing service account this deployment reuses, passed to the compute submodule. |
| `instance_name` | `string` | always required | none | Name of the virtual machine, in sanitized RFC1035 form. Validated: lowercase letter first, then lowercase alphanumerics and dashes, at most 32 characters, ending alphanumerically. |
| `vm_size` | `string` | always required | none | GCP machine type the instance is launched as. |
| `os_image` | `string` | optional | `"ubuntu-os-cloud/ubuntu-2404-lts-amd64"` | Boot image in project/family notation. Never resolved by a data source. |
| `root_volume_size_gb` | `number` | always required | none | Size, in GiB, of the root disk. |
| `data_volume_size_gb` | `number` | optional | `30` | Size, in GiB, of the data disk. Read only when `create_data_volume = true`. |
| `docker_daemon_user` | `string` | optional | `"dockerd"` | Name of the dedicated, unprivileged Linux user the rootless docker daemon runs as. |
| `docker_data_root` | `string` | always required | none | Absolute path on the instance the rootless daemon uses as its data-root. |
| `docker_tls_listen_address` | `string` | optional | `"0.0.0.0"` | Address the mTLS listener binds to. Must be `0.0.0.0`: the IAP tunnel terminates on nic0, and the boundary is the VPC's one ingress rule. |
| `docker_tls_listen_port` | `number` | optional | `2376` | TCP port the mTLS listener binds to; the IAP tunnel dials it and the firewall's one rule admits it. |
| `docker_repo_base_url` | `string` | optional | `"https://download.docker.com/linux/ubuntu"` | Base URL of the Docker apt repository the compute submodule's cloud-init adds. |
| `docker_repo_channel` | `string` | optional | `"stable"` | Release channel of the Docker apt repository the compute submodule's cloud-init adds. |
| `boot_marker_timeout` | `number` | optional | `600` | Seconds the every-boot startup script waits for the cloud-init provisioning marker before failing its apply step; a first boot's provisioning runs the full budget on the VM. The laptop-side `GCP_PROVISIONED_TIMEOUT` (300) is shorter and only ever applies to already-provisioned VMs. |
| `project_id` | `string` | always required | none | GCP project id every resource is created in, carried explicitly on every resource. Validated: 6-30 lowercase letters, digits and dashes. |
| `zone` | `string` | always required | none | GCP zone the instance and its disks are created in. Validated: `<region>-<letter>` (us-east1-b). |
| `state_store_suffix` | `string` | optional | `"q4m7zk"` | Mirror of the committed suffix `remote-instances/root-gcp.hcl` declares, read only by the `state_bucket` output's digest. The bucket itself is not terraform-managed. |
| `name_prefix` | `string` | always required | none | Prefix used to name every derived resource this composition creates, in sanitized RFC1035 form. Validated: 3-27 lowercase alphanumeric or dash characters, beginning with a letter and ending alphanumerically, so the derived `gd-<prefix>` service account id stays inside gcp's 6-30 service-account-id law. |
| `tags` | `map(string)` | optional | `{}` | Common labels merged onto the resources that expose labels: the instance, its data disk and the six per-instance secrets. The network submodule takes no labels input -- gcp network resources expose none. Validated against gcp's label law (lowercase letters, digits, underscores and dashes). |

Every input any of the three submodules (`modules/network`, `modules/platform`, `modules/compute`) declares is redeclared here as a root input of the same name and passed straight through, or resolved into a `locals` value from a toggle and its matching replacement input. No submodule input exists that is not represented in this table.

## Outputs

The first six are the deploy flow's contract: the gcp branch of `make instance-deploy` reads exactly these, by these names, from `terragrunt output -json` and writes the engine address record from them.

| Output | Consumed by |
|---|---|
| `instance_name` | The engine address record; every gcloud surface and the IAP tunnel dial the VM by it. |
| `zone` | The engine address record; the IAP tunnel is dialed through it. |
| `project_id` | The engine address record; the state bucket and the six secrets live in it. |
| `region` | The engine address record; the state bucket takes its location from it. |
| `secrets_prefix` | The engine address record; the publication flows and the startup script compose their secret ids from it. |
| `state_bucket` | The engine address record; the deploy-side mirror of `state_store_gcp`'s own composition (exact whenever the instance directory's name is already in sanitized form). |
| `vpc_id` | A second deployment's operator. `null` when `create_network = false`. |
| `subnet_id` | A second deployment's `var.subnet_id`, set with `create_network = false`, to reuse this deployment's network. |
| `sa_email` | A second deployment's `var.sa_email`, set with `create_platform = false`, to reuse this deployment's service account. |

The platform submodule's `sa_id` and `secret_ids`, and the network submodule's `network_name`, `subnet_name` and `firewall_name`, are internal to this composition except as re-exported above; the compute submodule's `instance_self_link`, `instance_tag` and `data_disk_id` are likewise not re-exported.

## Worked examples

Both examples use obviously-fake placeholder identifiers, never values from a real project.

### New network and platform (`create_network = true`, `create_platform = true`, the defaults; what the scaffold writes)

```hcl
# terragrunt.hcl
inputs = {
  create_network  = true
  create_platform = true
  create_data_volume = true

  project_id = "EXAMPLE-devcontainer-remote"
  zone       = "us-east1-b"
  region     = "us-east1"

  vpc_cidr   = "10.100.0.0/16"
  subnet_cidr = "10.100.1.0/24"

  instance_name = "EXAMPLE-remote"
  name_prefix   = "EXAMPLE-remote"

  vm_size            = "e2-standard-2"
  root_volume_size_gb = 30
  data_volume_size_gb = 30
  docker_data_root   = "/mnt/docker-data"

  tags = {
    Environment = "EXAMPLE"
  }
}
```

### Reused network (`create_network = false`), a second deployment

The `subnet_id` value below is the `subnet_id` output of a prior deployment of this module.

- The platform toggle stays `true`: this deployment gets its own service account and secrets, the same way the azure reuse example keeps its own platform toggle true.

```hcl
# terragrunt.hcl
inputs = {
  create_network  = false
  create_platform = true
  create_data_volume = true

  subnet_id = "projects/EXAMPLE-devcontainer-remote/regions/us-east1/subnetworks/EXAMPLE-remote-subnet"

  project_id = "EXAMPLE-devcontainer-remote"
  zone       = "us-east1-b"
  region     = "us-east1"

  instance_name = "EXAMPLE-remote-2"
  name_prefix   = "EXAMPLE-remote-2"

  vm_size            = "e2-standard-2"
  root_volume_size_gb = 30
  docker_data_root   = "/mnt/docker-data"

  tags = {
    Environment = "EXAMPLE-2"
  }
}
```
