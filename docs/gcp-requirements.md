# Google Cloud requirements

## 1. What this is for

This document tells an operations team, with no access to and no prior knowledge of the software repository this instance supports, exactly what to provision on Google Cloud and how the resulting host is configured, so that a developer can later reach it as a working remote container-development machine.

- Use it whenever the credentials or the change-approval process for the target project belong to your team rather than to the requester, so the requester's own automation cannot create the instance directly and hands this checklist to you instead.
- Every value below is exact, every host step names the payload that performs it, and every item has its own verification command in Section 5, so nothing here depends on reading anything but this page.

The transport this instance is reached through shapes everything below: the docker API travels over an Identity-Aware Proxy TCP tunnel the gcloud CLI opens.

- The instance carries no external IP, its network admits exactly one inbound rule, and SSH plays no part anywhere.
- The tunnel right is the transport's whole authorization: Google's proxy dials the VM's internal address only after the calling identity passes IAM, so the operator's gcloud login, not a network path, is what reaches this instance.

## 2. What to provision

Every value below is supplied to you by the requester unless marked "fixed"; a fixed value is the same for every instance and is not a per-request choice.

**Once per project, before the first instance:** the five APIs the deployment uses must be enabled. A fresh project ships with none of them enabled, and the failures surface as misleading errors elsewhere:

```sh
for api in compute.googleapis.com secretmanager.googleapis.com \
           iam.googleapis.com iamcredentials.googleapis.com \
           storage.googleapis.com; do
  gcloud services enable "$api" --project <project-id>
done
```

**The machine type is quota-bound.** A fresh project carries a zero or near-zero quota for most machine families in most regions until a limit is granted. Check the region's CPU quota and the machine type's existence before fixing the size:

```sh
gcloud compute regions describe <region> --project <project-id> \
  --flatten="quotas" --format="table(quotas.metric,quotas.limit,quotas.usage)"
gcloud compute machine-types list --project <project-id> --zones=<zone> | grep <vm-size>
```

- Pick a size whose family shows `Usage` comfortably below `Limit`, and set `var.vm_size` in the scaffolded `terragrunt.hcl` accordingly.

**The instance's Terraform state lives in one Cloud Storage bucket** the deploy flow ensures in the same project before any Terragrunt command runs: one bucket per instance, named `gdstate-<sanitized-instance>-<digest>`, with the digest composed from the instance name, the zone, the project and the committed suffix (`var.state_store_suffix`, declared in `remote-instances/root-gcp.hcl`). Provisioning creates nothing for it: the ensure is the requester's automation's own first step, and the bucket outlives the deployment.

### 2.1 Network

Choose one:

**A. A new, dedicated network for this instance (the common case).**

| Item | Exact requirement | Requester supplies |
|---|---|---|
| Project | One project, whose id is carried explicitly on every resource below | `var.project_id`, for example `devcontainer-remote-prod` |
| Region | The region every regional resource is created in, derived from the zone (`var.zone`, for example `us-east1-b`: the zone with its final dash-letter removed), never chosen separately | `var.region`, for example `us-east1` |
| Virtual network | One custom-mode virtual network, IPv4 address space as given | `var.vpc_cidr`, for example `10.100.0.0/16` |
| Subnetwork | One subnetwork inside that virtual network, IPv4 CIDR block as given and fully contained inside its address space, in the region above | `var.subnet_cidr`, for example `10.100.1.0/24` |
| Firewall rule | Exactly one ingress rule for the whole network: tcp on the daemon's TLS port (`var.docker_tls_listen_port`, default `2376`), source `35.235.240.0/20` (Google's documented IAP range), targeted at the instance's network tag. A custom-mode network carries no implied allow rules, so the posture fails closed until and unless this one rule exists | fixed |
| Egress | One Cloud Router and one Cloud NAT in the region, giving the internal-only instance its outbound path (package installs, image pulls, Secret Manager fetches) | fixed |

- The subnetwork carries no secondary range, no external IPv6 and no load balancer of any kind.
- Section 6 states why nothing else needs to reach this instance from outside.

**B. An existing subnetwork you already operate.**

- Skip the table above and reuse your own subnetwork unchanged, provided it has a route to internet egress (the instance still needs its own outbound path for package installs, image pulls and Secret Manager fetches) and your own ingress posture admits `35.235.240.0/20` to the daemon port on the instance's network tag.
- Record the subnetwork's identifier: hand it back per Section 4.
- On the requester's side, option B is what sets their `var.create_network` to false, with `var.subnet_id` naming what you created; the subnetwork's identifier carries its own network, so no second reuse input exists.
- Neither the IAP firewall nor the Cloud NAT is created for you in this mode: a reused subnetwork brings neither with it, so both are yours to provide.

### 2.2 Security and IAM

The security posture and the instance identity are one unit on Google Cloud, not two independently reusable pieces: the service account, the six per-instance secrets and their bindings reference one another, so they are provisioned together.

**A. A new platform unit (the common case).**

| Item | Exact requirement | Requester supplies |
|---|---|---|
| Service account | One service account the VM assumes, named by prefixing `gd-` onto the prefix given, carrying no project-wide IAM role at all | `var.name_prefix`, 3 to 27 lowercase alphanumeric or dash characters beginning with a letter and ending alphanumerically, so the derived `gd-<prefix>` account id stays inside the 6 to 30 character service-account-id law |
| Secret Manager secrets | Six secret vehicles under the prefix given: `tls-server-key`, `tls-server-cert`, `tls-ca`, `tls-checksum`, `shell-env`, `profile-map`. The unit creates the vehicles, never their contents; the requester's publication flows write those | `var.name_prefix` (the same prefix; the sanitized form the instance is named by) |
| Secret bindings | The service account above carries exactly `roles/secretmanager.secretAccessor` on those six secrets, and nothing else anywhere | fixed |
| Operator grants | Each identity that will open the tunnel is granted the right at the project level: Owner, or the narrow `roles/iap.tunnelResourceAccessor` role | (whoever connects) |

- The instance's identity reads exactly its six secrets: the per-secret bindings are the whole grant, so a reused account must carry the same bindings or the reads fail.
- The operator grant is what lets a laptop talk to this instance at all: IAP forwards a connection only after IAM accepts the calling identity, so removing the grant is what cuts a laptop off, and it is the only revocation mechanism this kind has.
- The `tls-checksum` secret carries the digest of the other three TLS secrets, and the VM's startup script compares its fetch against the guest attribute of the same name: it is a drift detector, not a credential.

**B. An existing platform unit you already operate.**

- Skip the unit rows above and name the service account the deployment must reuse instead: on the requester's side, option B is what sets their `var.create_platform` to false, with `var.sa_email` naming what you created.
- The account must already carry `roles/secretmanager.secretAccessor` on the six secret ids this deployment's prefix composes; the six secret vehicles must exist, and empty vehicles are a legal pre-publish state the startup script tolerates.
- The operator grants above are then your own IAM bindings to make directly, since no platform unit is being created to carry them.

### 2.3 The instance and its disks

| Item | Exact requirement | Requester supplies |
|---|---|---|
| Image | Ubuntu 24.04 LTS, amd64, in project/family notation | `var.os_image`, default `ubuntu-os-cloud/ubuntu-2404-lts-amd64` |
| Machine type | As given, sized per the quota note in Section 2 | `var.vm_size`, for example `e2-standard-2` (2 vCPU, 4 GiB, the sizing default the scaffold writes) |
| Name and labels | The VM, its disks and every derived resource carry names derived from the sanitized instance name given, a legal RFC1035 label: lowercase letters, digits and dashes, beginning with a letter and ending alphanumerically. Every resource carries the labels given | `var.instance_name` and `var.name_prefix` (both the sanitized form of the instance directory's name), `var.tags`, for example `{"Environment": "EXAMPLE"}` |
| Service account | The platform unit from 2.2 attached to the VM, with the `cloud-platform` access scope | (from 2.2) |
| External IP | **None.** The network interface declares no access config, so Google assigns no address | fixed |
| Root disk | One persistent disk, size as given | `var.root_volume_size_gb`, for example `50` |
| Data disk | Choose with the requester: either a second persistent disk, size as given, attached by the deployment and mounted by the boot payload, or no second disk at all (root disk only) | `var.create_data_volume` (default: create one), `var.data_volume_size_gb` (default `30`) |

The azure kind's story of an inert bootstrap admin key has no gcp twin, and none is needed.

- The Azure VM API refuses to create a Linux virtual machine with no admin credential, which is why that platform mints a key that is then deliberately unusable; Compute Engine has no such requirement, so this instance is created with no admin credential, no SSH key and no password anywhere.
- The provisioning channel is GCE metadata itself: the deployment renders the whole host configuration into the instance's `user-data` and `startup-script` metadata, and the guest agent applies it. Section 3 states what each payload does.
- The remaining daemon conventions are inputs, not manual steps: `var.docker_daemon_user` names the dedicated, unprivileged account the rootless daemon runs as (default `dockerd`); `var.docker_data_root` names its data-root (always required, for example `/mnt/docker-data`); `var.docker_tls_listen_address` is pinned to `0.0.0.0` because the IAP tunnel terminates on nic0, an address a loopback bind would never answer, and the boundary is the VPC's one ingress rule from 2.1 rather than a bind restriction; and the Docker apt repository the payload installs from is `var.docker_repo_base_url` (default `https://download.docker.com/linux/ubuntu`), channel `var.docker_repo_channel` (default `stable`).

## 3. What to configure on the host

Nothing is configured by hand: the deployment carries the whole host configuration in the instance's own metadata, applied by two boot payloads.

- An operations team provisions per Section 2 and hands back per Section 4; every step below is what the payloads do, stated so each verification in Section 5 has something concrete to check against.
- The instance boots from the unmodified Ubuntu 24.04 image in 2.3; there is no login, no console session and no manual step at any point.

### 3.1 Three silent failures, fixed by the payload

The azure kind fixes three silent failures by hand, first, because Azure offers this platform no boot-time channel; Google Cloud does, so the same three are fixed declaratively in the cloud-init payload and never by an operator:

- **Lingering for the daemon user.** Without `loginctl enable-linger`, systemd tears down the daemon user's own service manager, and the rootless daemon under it, the moment nobody is logged in as that user, which for a dedicated daemon account is immediately after boot. The daemon would appear to have installed correctly and then be simply gone.
- **The AppArmor profile Ubuntu 24.04's unprivileged-user-namespace restriction requires.** Without a profile permitting it, rootless docker's `rootlesskit` component cannot create container namespaces: the daemon reports healthy and only container creation fails, with a denial that names no daemon at all.
- **cgroup v2 controller delegation to the daemon user's service manager.** Ubuntu delegates `cpu`, `memory` and `pids` to a user's own systemd instance by default but not `cpuset` or `io`; without the delegation drop-in, every `--cpus`, `--memory` or `--pids-limit` flag a later container run supplies is silently unenforceable.

The payload writes all three (`/etc/apparmor.d/rootlesskit` and its load, `user@<uid>.service.d/delegate.conf` with `Delegate=cpu cpuset io memory pids`, and the linger flag with the service manager started), before the daemon is enabled.

### 3.2 The two boot channels, in the order their steps run

The instance's metadata carries two payloads, and the split is the design:

- **`user-data`, the cloud-init payload, runs once at first boot:** package index update, then `ca-certificates`, `curl`, `gnupg`, `uidmap`, `dbus-user-session` and `iptables`; the Docker apt repository (`var.docker_repo_base_url`, `var.docker_repo_channel`) and `docker-ce`, `docker-ce-cli`, `docker-ce-rootless-extras`, `containerd.io`; the rootful daemon disabled and verified gone (a rootful docker group is equivalent to host root, so this host never runs it and never joins the group); sshd disabled, keeping the package for platform diagnostics while removing the listener; the daemon user created with no login shell, lingering enabled and its service manager started; the three silent failures of 3.1; the data disk formatted and mounted at `var.docker_data_root` from its deterministic `/dev/disk/by-id/google-<name>` handle when `var.create_data_volume` is true; `daemon.json` written (listener on `var.docker_tls_listen_address`:`var.docker_tls_listen_port`, `tlsverify` on, material under the daemon user's `tls/` directory); the daemon enabled but never started; and the `/etc/general-dev-provisioned` marker written last. It fetches nothing: a first boot may precede the first publish.
- **`startup-script`, plain metadata, runs at every boot:** it waits for the provisioning marker (a first boot may still be inside cloud-init), fetches the three TLS secrets and the checksum secret through the metadata-server token, compares the fetched checksum against the `general-dev/tls-checksum` guest attribute, and only on drift (or first apply) writes the material, restarts the daemon and updates the attribute. On every success path it records `general-dev/provisioned=TRUE`; on a pre-publish boot it exits quietly; on any failure it records `general-dev/error` with the failing step and exits 1.
- The guest attributes are the laptop-side contract: `general-dev/provisioned` gates every wait that assumes provisioning ran, `general-dev/tls-checksum` says which material is applied, and `general-dev/error` carries the VM's own diagnostic when something fails.
- Rotation rides the same channel: publishing new material changes the stored checksum, and the next boot's drift comparison, not a human step, applies it. The instance is rebooted only when drift exists; an unchanged publish reboots nothing.

## 4. What to hand back

Once every item above verifies (Section 5), send the requester:

- **The instance name** (for example `devcontainer-remote-prod`), the sanitized RFC1035 form the VM is named by.
- **The zone** (for example `us-east1-b`).
- **The project id** (for example `devcontainer-remote-prod`).
- **The region** (for example `us-east1`).
- **If you followed option B in Section 2.1** (an existing subnetwork): the subnetwork's identifier. This becomes the requester's `var.subnet_id` input.
- **If you followed option B in Section 2.2** (an existing platform unit): the service account's email. This becomes the requester's `var.sa_email` input.

Send nothing else: no key, no secret, no service-account key file. The instance authenticates with its attached service account, a platform identity with no exported key, and the requester delivers the TLS material and configuration separately, into the Secret Manager secrets named in 2.2, once they hold these values.

## 5. How to verify each item

Run every command below before handing the instance back. Each states what a correct instance returns; anything else means the corresponding provisioning item above did not take effect.

| Verifies | Command | Expect |
|---|---|---|
| The five APIs enabled | `gcloud services list --enabled --project <project-id> --format="value(config.title)"` | `Compute Engine API`, `Secret Manager API`, `Cloud Storage API`, `Identity and Access Management (IAM) API` and `IAM Service Account Credentials API` among the answers |
| Machine type quota headroom | `gcloud compute regions describe <region> --project <project-id> --flatten="quotas" --format="table(quotas.metric,quotas.limit,quotas.usage)"` | The `CPUS` family metrics show `Usage` below `Limit` |
| Machine type exists in the zone | `gcloud compute machine-types list --project <project-id> --zones=<zone> | grep <vm-size>` | One line naming the chosen size |
| Network and subnetwork | `gcloud compute networks subnets describe <subnet> --region <region> --project <project-id> --format="value(ipCidrRange,network)"` | The CIDR from `var.subnet_cidr`, inside the network carrying the range from `var.vpc_cidr` |
| Exactly one ingress rule | `gcloud compute firewall-rules list --project <project-id> --filter="network:<network-name>" --format="table(name,direction,sourceRanges.list(),allowed)"` | One row: `INGRESS`, source `35.235.240.0/20`, tcp on the port from `var.docker_tls_listen_port` |
| Router and NAT exist | `gcloud compute routers nats describe <nat> --router <router> --region <region> --project <project-id>` | The NAT answers, sourcing the subnetwork from 2.1 |
| No external IP | `gcloud compute instances describe <vm> --zone <zone> --project <project-id> --format="value(networkInterfaces[0].accessConfigs)"` | Empty output |
| Service account attached and scoped | `gcloud compute instances describe <vm> --zone <zone> --project <project-id> --format="value(serviceAccounts[0].email,serviceAccounts[0].scopes)"` | `gd-<prefix>@<project>.iam.gserviceaccount.com` with `https://www.googleapis.com/auth/cloud-platform` among the scopes |
| Image and machine type | `gcloud compute instances describe <vm> --zone <zone> --project <project-id> --format="value(machineType.basename())"`, then `gcloud compute disks describe <vm> --zone <zone> --project <project-id> --format="value(sizeGb,sourceImage)"` | The size from `var.vm_size`; the disk size from 2.3; the image from `var.os_image` |
| Disks and their sizes | `gcloud compute disks list --project <project-id> --filter="zone:<zone>" --format="table(name,sizeGb,type.basename())"` | The root disk and, when `var.create_data_volume` is true, the data disk at `var.data_volume_size_gb` |
| The six secrets exist | `gcloud secrets list --project <project-id> --filter="name:<prefix>-" --format="value(name.basename())"` | `tls-server-key`, `tls-server-cert`, `tls-ca`, `tls-checksum`, `shell-env`, `profile-map` |
| The account's read grant | `gcloud secrets get-iam-policy <prefix>-tls-server-key --project <project-id> --format="value(bindings.role,bindings.members)"` | `roles/secretmanager.secretAccessor` bound to `serviceAccount:gd-<prefix>@<project>.iam.gserviceaccount.com` |
| Tunnel authorization granted | `gcloud projects get-iam-policy <project-id> --flatten="bindings[].members" --format="table(bindings.role,bindings.members)"` | One assignment per operator identity: Owner or `roles/iap.tunnelResourceAccessor` |
| Instance identity reads its secrets | `gcloud projects get-iam-policy <project-id> --flatten="bindings[].members" --format="table(bindings.role,bindings.members)"` | The service account appears under no project-wide role |
| Provisioning completed | `gcloud compute instances get-guest-attributes <vm> --zone <zone> --project <project-id> --query-path=general-dev/provisioned` | `TRUE` |
| Provisioning marker file written | `gcloud compute instances get-serial-port-output <vm> --zone <zone> --project <project-id>` | The cloud-init run ends writing `/etc/general-dev-provisioned`, with no `FATAL:` line before it |
| No recorded failure | `gcloud compute instances get-guest-attributes <vm> --zone <zone> --project <project-id> --query-path=general-dev/error` | HTTP 404: the attribute does not exist, which is the success state |
| Applied material matches the store | `gcloud compute instances get-guest-attributes <vm> --zone <zone> --project <project-id> --query-path=general-dev/tls-checksum`, compared against `gcloud secrets versions access latest --secret=<prefix>-tls-checksum --project <project-id>` | The two digests are equal |
| Material applied after the first publish | the serial port output above, read for the startup script's sentinel | `general-dev: tls material applied`, once the publication flows have run |
| State bucket in the right place | `gcloud storage buckets describe gs://<bucket> --project <project-id> --format="value(location)"` | The region from Section 2 |

## 6. What is explicitly not needed

None of the following is required, and adding any of them weakens the security model this instance is built around rather than strengthening it:

- **No external IP.** The instance never accepts a connection from outside its VPC, so there is no address for one to arrive at; its interface carries a private address inside the subnetwork from 2.1 and nothing else. The tunnel terminates on that private address from Google's own front end, which is what makes the absence free rather than a limitation.
- **No SSH, and no keys of any kind.** Access to the host is metadata and IAM, not an SSH session; do not add SSH keys to project or instance metadata, and do not enable OS Login. There is no admin-credential requirement to satisfy, so no bootstrap key exists to protect.
- **No bastion host, no VPN, no jump box.** IAP is the jump point: the developer's gcloud CLI couples to the VM's nic0 through Google's proxy, authorized by the operator grant from 2.2, with no second host to reach through and no private network to join first.
- **No run-command or agent channel for provisioning.** The azure kind drives host steps through `az vm run-command` because it has to; this kind's boot payloads make material apply at boot by design, so nothing ever needs to execute a command on the VM from outside.
