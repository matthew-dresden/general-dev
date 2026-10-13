# Azure requirements

## 1. What this is for

This document tells an operations team, with no access to and no prior
knowledge of the software repository this instance supports, exactly what to
provision on Microsoft Azure and how to configure the resulting host, so that
a developer can later reach it as a working remote container-development
machine. Use it whenever the credentials or the change-approval process for
the target Azure subscription belong to your team rather than to the
requester, so the requester's own automation cannot create the instance
directly and hands this checklist to you instead. Every value below is exact,
every host step states the command to run, and every item has its own
verification command in Section 5, so nothing here depends on reading
anything but this page.

The transport this instance is reached through shapes everything below: the
docker API travels over an Azure Relay hybrid connection the virtual machine
dials out to, so the instance carries no public IP, its network security
group admits no inbound traffic, and SSH plays no part anywhere. The relay
itself is the jump point.

## 2. What to provision

Every value below is supplied to you by the requester unless marked
"fixed"; a fixed value is the same for every instance and is not a per-request
choice.

**Once per subscription, before the first instance:** the resource providers
the deployment uses must be registered. A fresh subscription ships with none
of them registered, and the failures surface as misleading errors elsewhere
(a storage create answering `SubscriptionNotFound` is this, not a billing
problem):

```sh
for ns in Microsoft.Storage Microsoft.Compute Microsoft.Network \
          Microsoft.KeyVault Microsoft.ManagedIdentity Microsoft.Relay; do
  az provider register --namespace "$ns"
done
```

**The VM size is quota- and capacity-bound.** A fresh subscription carries a
zero-core quota for most VM families until a limit is granted, and a family
with quota can still answer `SkuNotAvailable` in a chosen region (a capacity
restriction). Check both before fixing the size:

```sh
az vm list-usage --location <location> -o table | grep -i 'Family vCPU'
az vm list-skus --location <location> --size <vm-size> -o table   # restrictions: None
```

Pick a size whose family shows a nonzero quota and no restriction, and set
`vm_size` (and, for an x64 size, `os_image_urn`) in the scaffolded
`terragrunt.hcl` accordingly.

### 2.1 Network

Choose one:

**A. A new, dedicated network for this instance (the common case).**

| Item | Exact requirement | Requester supplies |
|---|---|---|
| Resource group | One resource group in the region given, holding every resource below | created inside it, named from `var.name_prefix` |
| Virtual network | One virtual network in that resource group, IPv4 address space as given | `var.vnet_cidr`, for example `10.100.0.0/16` |
| Subnet | One subnet inside that virtual network, IPv4 CIDR block as given, fully contained inside the virtual network's address space | `var.subnet_cidr`, for example `10.100.1.0/24` |
| Region | The Azure region every resource above is created in | `var.location`, for example `westeurope` |

The subnet carries no service endpoint, NAT gateway or load balancer of any
kind. The instance's only outbound need is its own egress path (package
installs, image pulls, the relay's control channel), which Azure's default
outbound route provides; Section 6 states why nothing needs to reach this
instance from outside.

**B. An existing network you already operate.** Skip the table above and use
your own resource group and subnet unchanged, provided the subnet has a
route to internet egress (the instance still needs its own outbound path for
package installs, image pulls and the relay's control channel). Record the
resource group's name and the subnet's identifier: hand both back per
Section 4. On the requester's side, option B is what sets their
`var.create_network` to false, with `var.resource_group_name` and
`var.subnet_id` naming what you created.

### 2.2 Security and identity

The security posture and the identity are one unit on Azure, not two
independently reusable pieces: the network security group, the user-assigned
managed identity, the Key Vault and the relay namespace reference one
another, so they are provisioned together.

**A. A new platform unit (the common case).**

| Item | Exact requirement | Requester supplies |
|---|---|---|
| Network security group | Attached to the subnet from 2.1. Zero inbound security rules of any kind — the rule list is empty, and that emptiness is the control. Outbound: Azure's default allow-all outbound | fixed |
| User-assigned managed identity | One identity, in the resource group from 2.1 | fixed, named from `var.name_prefix` |
| Key Vault | One vault, name derived by appending `-vault` to the prefix given. Purge protection and RBAC authorization on. The identity above carries exactly one data role on it: Key Vault Secrets User | `var.name_prefix`, at most 18 lowercase alphanumeric or dash characters beginning with a letter and not ending with a dash, so the derived `-vault` name stays inside Azure's 3-24 vault-name law |
| Relay namespace | One Relay namespace, name derived by appending `-relay` to the prefix given, in the region from 2.1. The identity above carries exactly one role on it: Azure Relay Listener | `var.name_prefix` (the same prefix; the derived `-relay` name stays inside the namespace naming law) |
| Hybrid connection | One hybrid connection inside that namespace, named as given | `var.instance_name`, for example `devcontainer-remote-prod` |
| Identity role assignments | Key Vault Secrets User on the vault, and Azure Relay Listener on the namespace, both scoped to the managed identity above — and nothing else | fixed |
| Operator grants | The Entra object ids given are each granted exactly one role: Azure Relay Sender on the relay namespace (who may open the sender half of the hybrid connection), Key Vault Secrets Officer on the vault (who may publish the instance's TLS material and configuration secrets) | `var.relay_sender_object_ids` and `var.keyvault_officer_object_ids`, each a list of Entra object ids, for example `["<entra-object-id>"]` |

The two operator grants are what let the developer's own laptop talk to the
instance at all: the sender role authorizes opening the relay half the
docker API travels through, and the officer role authorizes writing the
TLS material and configuration the instance reads out of its vault. Each
list may be empty, which grants no human anything; a deployment whose
operator lists are empty provisions fine and simply cannot be reached or
supplied until the ids are named.

**B. An existing platform unit you already operate**, meeting every
requirement in the table above. Record the identity's resource id, the relay
namespace's fully qualified host name and the hybrid connection's name: hand
all three back per Section 4. On the requester's side, option B is what sets
their `var.create_platform` to false, with `var.identity_id`,
`var.relay_namespace` and `var.hybrid_connection` naming what you created.
The two human grants above are then your own role assignments to make
directly, since no platform unit is being created to carry them.

### 2.3 The instance and its disks

| Item | Exact requirement | Requester supplies |
|---|---|---|
| Image | Ubuntu 24.04 LTS, in Publisher:Offer:Sku:Version URN form, whose architecture matches the VM size below: the arm64 server image (`Canonical:ubuntu-24_04-lts:server-arm64:latest`) with an arm64-capable size, or the x64 server image with an x64 size | `var.os_image_urn` (default: the arm64 URN above), paired with `var.vm_size` |
| VM size | As given, its architecture matching the image URN above | `var.vm_size`, for example `Standard_D2pls_v5` |
| Admin account | One admin SSH public key, satisfying the Azure VM API's own admin-credential requirement and nothing else (see below) | fixed: generated for the deployment, never a shared key |
| Network placement | The subnet and network security group from 2.1 and 2.2, with **no public IP address attached to the network interface** | (from 2.1, 2.2) |
| Root disk | One `StandardSSD_LRS` disk, size as given | `var.root_volume_size_gb`, for example `50` |
| Data disk | Choose with the requester: either a second `StandardSSD_LRS` disk, size as given, attached to the VM, or no second disk at all (root disk only) | `var.create_data_volume` (default: create one), `var.data_volume_size_gb` (default `100`) |
| Identity attachment | The user-assigned managed identity from 2.2 attached to the VM | (from 2.2) |
| Name and tags | The VM, its network interface and every disk carry the prefix given as their Name tag, the disks with fixed suffixes appended (`-vm`, `-nic`, `-data`), plus every additional tag given | `var.name_prefix`, `var.tags`, for example `{"Environment": "EXAMPLE"}` |

The bootstrap admin key is inert by construction, and this is a control
rather than an oversight: it exists only because the Azure VM API refuses to
create a Linux virtual machine with no admin credential at all. The host
configuration in Section 3 disables sshd, the network security group admits
no inbound traffic, and no public IP exists, so there is no path on which
the key could ever be presented. There is no password admin, ever.

## 3. What to configure on the host

Boot the virtual machine from the unmodified Ubuntu 24.04 image above and
run every step below on it, in order. The three silent failures come first:
each leaves the host looking correctly configured, so finding one after the
steps that follow have already run costs more time than doing them first
would have.

### 3.1 Three silent failures, fixed first

1. **Enable lingering for the daemon user**, `loginctl enable-linger
   <daemon-user>` (`var.docker_daemon_user`, default `dockerd`), run once the
   user account exists. Without lingering, systemd tears down that user's
   own service manager, and the rootless docker daemon running under it, the
   moment nobody is logged in as that user, which for a dedicated daemon
   account with no interactive login is immediately after boot. The daemon
   appears to have installed correctly and then is simply gone, with nothing
   in its own log explaining why: the process that killed it was systemd
   tearing down the session, not the daemon failing.

2. **Install the AppArmor profile Ubuntu 24.04's unprivileged-user-namespace
   restriction requires.** Ubuntu 24.04 denies an unprivileged process from
   creating a user namespace unless an AppArmor profile explicitly permits
   it, and rootless docker's `rootlesskit` component needs exactly that
   permission to build each container's namespace. Write
   `/etc/apparmor.d/rootlesskit` with this exact content:

   ```text
   abi <abi/4.0>,
   include <tunables/global>

   /usr/bin/rootlesskit flags=(unconfined) {
     userns,
     include if exists <local/rootlesskit>
   }
   ```

   then load it: `apparmor_parser -r /etc/apparmor.d/rootlesskit`. Without
   the profile, the daemon itself starts and reports healthy; only
   container creation fails, with an AppArmor denial that does not name the
   daemon at all, so the symptom looks like a docker or a kernel problem
   rather than a missing permission grant.

3. **Delegate cgroup v2 controllers to the daemon user's service manager.**
   Ubuntu 24.04 delegates the `cpu`, `memory` and `pids` controllers to a
   user's own systemd instance by default, but not `cpuset` or `io`.
   Create `/etc/systemd/system/user@<uid>.service.d/delegate.conf`, where
   `<uid>` is the daemon user's numeric ID from `id -u <daemon-user>`, with
   this exact content:

   ```text
   [Service]
   Delegate=cpu cpuset io memory pids
   ```

   then reload systemd and restart the daemon user's service-manager unit
   so the delegation takes effect: `systemctl daemon-reload` and
   `systemctl restart user@<uid>.service`. Without an explicit delegation
   drop-in, rootless docker still starts and still creates containers, and
   every `--cpus`, `--memory` or `--pids-limit` flag a later container run
   supplies is silently unenforceable. Nothing fails and nothing logs a
   warning: the limit is simply not applied.

### 3.2 Remaining host configuration, in the order performed

1. Update the package index and install `ca-certificates`, `curl`, `gnupg`,
   `uidmap`, `dbus-user-session` and `iptables`. `iptables` is a hard
   prerequisite of the rootless install step below, not an optional
   convenience: rootless docker manages its own bridge NAT and
   published-port DNAT through it.
2. Add the Docker apt repository's signing key and source list, from the
   base URL and release channel given (`var.docker_repo_base_url`, default
   `https://download.docker.com/linux/ubuntu`; `var.docker_repo_channel`,
   default `stable`), then install `docker-ce`, `docker-ce-cli`,
   `docker-ce-rootless-extras` and `containerd.io`.
3. Disable and stop the rootful daemon the package install just enabled:
   `systemctl disable --now docker.service docker.socket`. Confirm neither
   unit is active or enabled afterward (Section 5). Docker group membership
   on a rootful daemon is equivalent to host root, so this host never runs
   the rootful daemon at all, and never adds any account to the `docker`
   group.
4. **Disable sshd**: `systemctl disable --now ssh sshd`. This host has no
   SSH surface anywhere: it has no public IP, its security group admits no
   inbound traffic, and every operational path — the docker API over the
   relay, certificate and secret delivery, provisioning diagnostics — runs
   through run commands against the VM agent and the identity. Disabling,
   not purging: the Azure VM agent tolerates an absent server, but
   disabling keeps the package intact for platform diagnostics while
   removing the listener.
5. Create the dedicated, unprivileged daemon user given
   (`var.docker_daemon_user`, default `dockerd`), with no interactive login
   shell, then apply silent failure 1 above (`loginctl enable-linger
   <daemon-user>`) and start that user's service manager:
   `systemctl start user@<uid>.service`, where `<uid>` is that user's
   numeric ID from `id -u <daemon-user>`. Every later step that talks to
   this user's service manager (steps 10, 12 and 14, and the "Daemon
   enabled, not started" row in Section 5) reuses this same `<uid>`.
6. Load the AppArmor profile written in silent failure 2 above:
   `apparmor_parser -r /etc/apparmor.d/rootlesskit`.
7. Apply the cgroup delegation drop-in written in silent failure 3 above
   for the daemon user's `user@<uid>.service`, then reload systemd and
   restart that unit so the delegation takes effect: `systemctl
   daemon-reload` and `systemctl restart user@<uid>.service`.
8. Create the daemon user's TLS directory, the destination the requester's
   certificate delivery (Section 4) targets:
   `install -d -m 0700 -o <daemon-user> -g <daemon-user>
   /home/<daemon-user>/tls`.
9. Create the daemon's data directory at the path given
   (`var.docker_data_root`, always required, for example
   `/mnt/docker-data`), owned by the daemon user.
10. If a second data disk was provisioned (2.3): format it once
    (`mkfs.ext4 -i 8192 -L DOCKERDATA <device>`), add a `LABEL=DOCKERDATA
    <data-root> ext4 defaults,noatime,nofail 0 2` line to `/etc/fstab`,
    mount it (`mount -a`), confirm the mount succeeded:
    `mountpoint -q <data-root>`, then restore the daemon user's ownership
    of the mount point, which mounting a filesystem over the directory from
    step 9 replaces with the new filesystem's own root ownership:
    `chown <daemon-user>:<daemon-user> <data-root>`. Skipping this chown
    leaves the data-root owned by root, and the unprivileged rootless
    daemon cannot write to it. Azure presents attached data disks at
    `/dev/sd*` names whose letters are allocation-order dependent, so
    locate the disk by scanning for exactly one empty disk rather than by
    trusting a device literal.
11. Install rootless docker for the daemon user, then stop the daemon it
    starts automatically; it is reconfigured and re-enabled next. The
    daemon user has no interactive login shell (step 5), so run both
    commands through `runuser`, supplying the daemon user's own runtime
    directory and session bus explicitly because `runuser -u` does not
    propagate the caller's environment:

    ```text
    runuser -u <daemon-user> -- env HOME=/home/<daemon-user> XDG_RUNTIME_DIR=/run/user/<uid> DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/<uid>/bus PATH=/usr/bin:/usr/sbin:/bin:/sbin dockerd-rootless-setuptool.sh install
    runuser -u <daemon-user> -- env HOME=/home/<daemon-user> XDG_RUNTIME_DIR=/run/user/<uid> DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/<uid>/bus systemctl --user stop docker.service
    ```

12. Write that user's `daemon.json` at
    `/home/<daemon-user>/.config/docker/daemon.json`, owned by the daemon
    user, with this exact content, substituting the data-root path from
    step 9, the TLS directory from step 8, and the loopback address and
    port given (`var.docker_tls_listen_address`, default `127.0.0.1`, must
    remain loopback; `var.docker_tls_listen_port`, default `2376`):

    ```json
    {
      "data-root": "<data-root>",
      "hosts": ["unix:///run/user/<uid>/docker.sock", "tcp://<tls-listen-address>:<tls-listen-port>"],
      "tls": true,
      "tlsverify": true,
      "tlscacert": "/home/<daemon-user>/tls/ca.pem",
      "tlscert": "/home/<daemon-user>/tls/server-cert.pem",
      "tlskey": "/home/<daemon-user>/tls/server-key.pem"
    }
    ```

    This step names the paths the daemon reads for its TLS material; it
    does not supply the certificate itself, which the requester delivers
    separately, into the directory from step 8, once the values in
    Section 4 are handed back.
13. Enable, but do not start, the daemon user's `docker.service`, again
    through `runuser` with the same environment as step 11:

    ```text
    runuser -u <daemon-user> -- env HOME=/home/<daemon-user> XDG_RUNTIME_DIR=/run/user/<uid> DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/<uid>/bus systemctl --user enable docker.service
    ```

    The daemon's first start reads the TLS material at the paths step 12's
    `daemon.json` names; starting it before that material exists would fail
    and leave the enablement itself undone, so this step stops short of
    starting it.
14. Install the relay listener. Write the listener's environment file
    `/etc/default/gd-relay`, mode `0644`, with these three lines,
    substituting the relay namespace's fully qualified host name, the
    hybrid connection's name, and the TLS listen port from step 12:

    ```text
    GD_RELAY_NAMESPACE=<relay-namespace-fqdn>
    GD_RELAY_HYBRID_CONNECTION=<hybrid-connection-name>
    GD_RELAY_TARGET=127.0.0.1:<tls-listen-port>
    ```

    The file carries resource names and no credential: the listener
    authenticates to the relay with a token it fetches from the instance
    metadata endpoint under the managed identity from 2.2, which is why
    that identity's Azure Relay Listener role is a provisioning item rather
    than a secret. Install the listener itself at
    `/opt/gd-relay/gd-relay-listener.py` and its systemd unit at
    `/opt/gd-relay/gd-relay.service` (the requester supplies both from the
    repository's `provider/azure/modules/compute/` directory), copy the
    unit into `/etc/systemd/system/`, and **enable but do not start** it:
    `systemctl enable gd-relay.service`. Azure bills listener-hours while a
    hybrid connection has a connected listener, so a listener started at
    boot with nothing to forward would accrue them from first boot; the
    developer's own connect flow starts the unit on demand, and the
    listener stops itself after an idle stretch.
15. Once every step above has succeeded, write a UTC timestamp to
    `/etc/general-dev-provisioned`. This file's presence, and only its
    presence, is the signal the instance is ready to hand back; nothing
    earlier in this list writes it.

## 4. What to hand back

Once every item above verifies (Section 5), send the requester:

- **The relay namespace's fully qualified host name** (for example
  `gd-example-relay.servicebus.windows.net`).
- **The hybrid connection's name** (for example
  `devcontainer-remote-prod`).
- **The Key Vault's name and URI** (for example
  `https://gd-example-vault.vault.azure.net/`).
- **The virtual machine's name and resource id** (for example
  `devcontainer-remote-prod-vm` and its full
  `/subscriptions/…/virtualMachines/…` id).
- **The Azure region** every resource runs in (for example `westeurope`).
- **If you followed option B in Section 2.1** (an existing network): the
  resource group's name and the subnet's identifier. These become the
  requester's `var.resource_group_name` and `var.subnet_id` inputs
  respectively.
- **If you followed option B in Section 2.2** (an existing platform unit):
  the identity's resource id, the relay namespace's fully qualified host
  name and the hybrid connection's name. These become the requester's
  `var.identity_id`, `var.relay_namespace` and `var.hybrid_connection`
  inputs respectively.

Send nothing else: no certificate, no key, no credential of any kind. The
requester delivers the TLS material this instance needs separately, through
the Key Vault named above, once they hold these values.

## 5. How to verify each item

Run every command below before handing the instance back. Each states what
a correct instance returns; anything else means the corresponding
provisioning or configuration item above did not take effect.

| Verifies | Command | Expect |
|---|---|---|
| Virtual network address space | `az network vnet show -g <resource-group> -n <vnet-name> --query "addressSpace.addressPrefixes"` | Matches `var.vnet_cidr` from 2.1 |
| Subnet CIDR | `az network vnet subnet show -g <resource-group> --vnet-name <vnet-name> -n <subnet-name> --query "addressPrefix"` | Matches `var.subnet_cidr` from 2.1 |
| Network security group has zero inbound rules | `az network nsg rule list -g <resource-group> --nsg-name <nsg-name> --query "[?direction=='Inbound']"` | `[]` (empty) |
| No public IP on the network interface | `az network nic show -g <resource-group> -n <nic-name> --query "ipConfigurations[].publicIPAddress"` | Every entry `null` |
| Managed identity exists and is attached to the VM | `az vm show -g <resource-group> -n <vm-name> --query "identity.userAssignedIdentities \| keys(@)"` | Exactly the identity from 2.2 |
| Identity's Key Vault data role | `az role assignment list --assignee <identity-principal-id> --scope <vault-resource-id>` | Exactly one assignment: Key Vault Secrets User |
| Identity's relay role | `az role assignment list --assignee <identity-principal-id> --scope <relay-namespace-resource-id>` | Exactly one assignment: Azure Relay Listener |
| Operator sender grants | `az role assignment list --scope <relay-namespace-resource-id> --role "Azure Relay Sender"` | One assignment per id given in `var.relay_sender_object_ids`, none otherwise |
| Operator officer grants | `az role assignment list --scope <vault-resource-id> --role "Key Vault Secrets Officer"` | One assignment per id given in `var.keyvault_officer_object_ids`, none otherwise |
| Hybrid connection exists | `az relay hyco show --namespace-name <namespace-name> -g <resource-group> -n <hybrid-connection-name>` | Returns the connection, name matching `var.instance_name` |
| Image and size | `az vm show -g <resource-group> -n <vm-name> --query "{Size:hardwareProfile.vmSize,Image:storageProfile.imageReference}"` | The URN and size given in 2.3, architectures matching |
| Root and data disks | `az resource show -g <resource-group> --resource-type Microsoft.Compute/disks --name <disk-name> --query "{Sku:sku.name,Size:diskSizeGb}"` | Every disk `StandardSSD_LRS`, with the sizes given in 2.3 |
| Vault RBAC and properties | `az keyvault show --name <vault-name> --query "{Rbac:enableRbacAuthorization,Name:name}"` | `Rbac` is `true`; the name is the prefix with `-vault` appended |
| Identity token reaches the vault from the VM | On the host: `curl -s -H Metadata:true "http://169.254.169.254/metadata/identity/oauth2/token?api-version=2018-02-01&resource=https%3A%2F%2Fvault.azure.net" \| grep -c access_token` | `1`: the identity answers, which is what the certificate fetch at container-create depends on |
| Identity token reaches the relay from the VM | On the host: `curl -s -H Metadata:true "http://169.254.169.254/metadata/identity/oauth2/token?api-version=2018-02-01&resource=https%3A%2F%2Frelay.azure.net" \| grep -c access_token` | `1`: the identity answers, which is what the listener's outbound connection depends on |
| Rootful daemon disabled | On the host: `systemctl is-active docker.service; systemctl is-enabled docker.service` | Both report `inactive` / `disabled` (non-zero exit is expected here) |
| sshd disabled | On the host: `systemctl is-active ssh; systemctl is-enabled ssh` | Both report `inactive` / `disabled` (non-zero exit is expected here) |
| Daemon user lingering | On the host: `loginctl show-user <daemon-user> --property=Linger` | `Linger=yes` |
| AppArmor profile loaded | On the host: `cat /sys/kernel/security/apparmor/profiles \| grep rootlesskit` | `/usr/bin/rootlesskit (unconfined)`: the profile grants `userns` rather than confining the binary, so `unconfined` is the correct, loaded state |
| cgroup v2 delegation | On the host: `cat /sys/fs/cgroup/user.slice/user-<uid>.slice/user@<uid>.service/cgroup.controllers` | Includes `cpuset` and `io` alongside `cpu`, `memory` and `pids` |
| TLS directory permissions | On the host: `stat -c "%a %U %G" /home/<daemon-user>/tls` | `700 <daemon-user> <daemon-user>` |
| Data-root directory ownership | On the host: `stat -c "%U:%G" <data-root>` | `<daemon-user>:<daemon-user>`, whether or not a second data disk was provisioned |
| Data disk mounted | On the host: `mountpoint -q <data-root> && echo mounted` | `mounted` (only if a second data disk was provisioned per 2.3) |
| Daemon configuration | On the host: `cat /home/<daemon-user>/.config/docker/daemon.json` | `data-root` matches the path given, `tls: true`, `tlsverify: true`, TCP host at the loopback address and port given |
| Daemon enabled, not started | On the host: `runuser -u <daemon-user> -- env HOME=/home/<daemon-user> XDG_RUNTIME_DIR=/run/user/<uid> DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/<uid>/bus systemctl --user is-enabled docker.service` | `enabled` (the daemon is not expected to be running yet; Section 4 covers what happens next) |
| Relay listener installed, enabled, not started | On the host: `cat /etc/default/gd-relay`, then `systemctl is-enabled gd-relay.service; systemctl is-active gd-relay.service` | The environment file carries the three names from step 14; the unit reports `enabled` and `inactive` — starting it is the requester's connect flow's job, never provisioning's |
| Provisioning completed | On the host: `cat /etc/general-dev-provisioned` | A single UTC timestamp; its presence alone confirms every step in Section 3 succeeded |

## 6. What is explicitly not needed

None of the following is required, and adding any of them weakens the
security model this instance is built around rather than strengthening it:

- **No inbound network security rule of any kind.** The only path into this
  instance is the relay hybrid connection, whose listener end is a process
  on the instance dialing *outward* to the relay service; an inbound rule
  would open a path nothing here needs and everything here is built to
  avoid.
- **No public IP address.** The instance never accepts a connection, so
  there is no address for one to arrive at; its network interface carries a
  private address inside the subnet from 2.1 and nothing else. A public IP
  would be a standing invitation with no door behind it.
- **No SSH, and no key.** Access to the host is a run command against the
  VM agent under the managed identity, not an SSH session; do not enable
  sshd, and do not distribute the bootstrap admin key — it exists to
  satisfy the VM API's own admin-credential requirement and is inert by
  construction.
- **No bastion host, no VPN, no jump box.** The relay itself is the jump
  point: the developer's laptop couples to the instance's listener through
  the hybrid connection, authorized by Entra, with no second host in
  between to reach through and no private network to join first.
