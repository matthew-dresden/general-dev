# EC2 requirements

## 1. What this is for

This document tells an operations team, with no access to and no prior
knowledge of the software repository this instance supports, exactly what to
provision on Amazon EC2 and how to configure the resulting host, so that a
developer can later reach it as a working remote container-development
machine. Use it whenever the credentials or the change-approval process for
the target AWS account belong to your team rather than to the requester, so
the requester's own automation cannot create the instance directly and hands
this checklist to you instead. Every value below is exact, every host step
states the command to run, and every item has its own verification command
in Section 5, so nothing here depends on reading anything but this page.

## 2. What to provision

Every value below is supplied to you by the requester unless marked
"fixed"; a fixed value is the same for every instance and is not a per-request
choice.

### 2.1 Network

Choose one:

**A. A new, dedicated network for this instance (the common case).**

| Item | Exact requirement | Requester supplies |
|---|---|---|
| VPC | One VPC, DNS support and DNS hostnames both enabled, IPv4 CIDR block as given | `var.vpc_cidr`, for example `10.0.0.0/16` |
| Subnet | One public subnet inside that VPC, `Auto-assign public IPv4` enabled, IPv4 CIDR block as given, fully contained inside the VPC's CIDR | `var.subnet_cidr`, for example `10.0.1.0/24` |
| Availability zone | The subnet above is created in this zone | `var.availability_zone`, for example `us-east-1a` |
| Internet gateway | One, attached to the VPC | fixed |
| Route table | Attached to the subnet, with one route: destination `0.0.0.0/0`, target the internet gateway above | fixed |
| NAT | None: no NAT gateway, no NAT instance | fixed |

The subnet's public IPv4 address exists only to give the instance a return
path for connections it opens itself. It is not there to be connected to;
Section 6 states why nothing needs to reach this instance from outside.

**B. An existing network you already operate.** Skip the table above and use
your own VPC and subnet unchanged, provided the subnet still has
`Auto-assign public IPv4` enabled and a route to an internet gateway (the
instance still needs a return path for its own outbound connections, even
though nothing initiates a connection to it). Record the VPC's and the
subnet's identifiers: hand both back per Section 4. On the requester's side,
option B is what sets their `var.create_network` to false.

### 2.2 Security and IAM

Choose the same option, A or B, for both the security group and the IAM
role: creating one and reusing the other is not a supported combination for
this instance.

**A. A new security group and a new IAM role (the common case).**

| Item | Exact requirement | Requester supplies |
|---|---|---|
| Security group | Attached to the network above. Zero inbound rules of any kind. One outbound rule: all protocols, all ports, to the CIDR block(s) given | `var.egress_cidr_blocks`, for example `["0.0.0.0/0"]` |
| IAM role | Trust policy allows exactly one principal to assume it: the EC2 service (`ec2.amazonaws.com`). No other principal, no other action than `sts:AssumeRole` | fixed |
| Managed policy | Attach the AWS managed policy `AmazonSSMManagedInstanceCore` to the role | fixed |
| Inline policy | One inline policy on the role granting `ssm:GetParameter`, `ssm:GetParameters` and `ssm:GetParametersByPath`, and nothing else, scoped to exactly two Parameter Store path prefixes and no other resource: `/devcontainer/<instance-name>/*` and `/devcontainer/shared/*` | `var.instance_name` (the `<instance-name>` above), for example `EXAMPLE-devcontainer-remote` |
| Instance profile | One instance profile carrying the role above, attached to the instance | fixed |

The inline policy's two-prefix scoping is a control, not a convenience: an
instance compromised while it is running can read only its own and the
shared secret prefix, and can write no parameter at all. Do not widen either
`Resource` entry, and do not add a third.

**B. An existing security group and IAM role you already operate**, meeting
every requirement in the table above. Record the security group's and the
instance profile's identifiers: hand both back per Section 4. On the
requester's side, option B is what sets their `var.create_security_group`
and `var.create_iam_role` -- always together, never one without the other --
to false.

### 2.3 The instance and its volumes

| Item | Exact requirement | Requester supplies |
|---|---|---|
| AMI | Ubuntu 24.04 LTS, in the target region | `var.ami`, for example `ami-EXAMPLE00000000` |
| Instance type | As given | `var.instance_type`, for example `t3.large` |
| Network placement | The subnet, security group and instance profile from 2.1 and 2.2 | (from 2.1, 2.2) |
| Root volume | One `gp3` volume, encrypted, size as given, deleted automatically on instance termination | `var.root_volume_size_gb`, for example `50` |
| Data volume | Choose with the requester: either a second `gp3` volume, encrypted, size as given, attached at the device name given, or no second volume at all (root volume only) | `var.create_data_volume` (default: create one), `var.data_volume_size_gb` (default `100`), `var.data_volume_device_name` (default `/dev/sdf`) |
| Termination protection | Enabled (`DisableApiTermination` = true) | fixed |
| Stop protection | Enabled (`DisableApiStop` = true) | fixed |
| Instance metadata service | IMDSv2 required (`HttpTokens` = required), metadata endpoint enabled, hop limit exactly 2 | fixed |
| Name tag | `var.name_prefix` is a prefix, not a literal Name value: the instance, the VPC and the internet gateway carry it unchanged, and every other resource above carries it with a fixed suffix appended -- root volume `-root`, data volume `-data`, subnet and route table `-public`, security group `-instance-sg`, IAM role `-instance-role`, instance profile `-instance-profile` | `var.name_prefix`, for example `EXAMPLE-devcontainer-remote` |
| Additional tags | As given, if any | `var.tags`, for example `{"Environment": "EXAMPLE"}` |

The metadata hop limit is exactly 2, not the default of 1: a container
running on this host reaches the instance's own credentials one network hop
further out than a process on the host itself, and hop limit 1 would block
that container from ever completing the reach.

## 3. What to configure on the host

Boot the instance from an unmodified Ubuntu 24.04 AMI and run every step
below on it, in order. The three silent failures come first: each leaves the
host looking correctly configured, so finding one after the steps that follow
have already run costs more time than doing them first would have.

### 3.1 Three silent failures, fixed first

1. **Enable lingering for the daemon user**, `loginctl enable-linger
   <daemon-user>` (`var.docker_daemon_user`, default `dockerd`), run once the
   user account exists. Without lingering, systemd tears down that user's
   own service manager, and the rootless docker daemon running under it, the
   moment nobody is logged in as that user -- which for a dedicated daemon
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
4. Install and enable the `amazon-ssm-agent` snap
   (`snap install amazon-ssm-agent --classic`, then
   `systemctl enable --now snap.amazon-ssm-agent.amazon-ssm-agent.service`).
   Do this before any daemon-user-specific step below that can fail, so an
   operator can always reach the host through SSM to diagnose a later
   failure.
5. Create the dedicated, unprivileged daemon user given
   (`var.docker_daemon_user`, default `dockerd`), with no interactive login
   shell, then apply silent failure 1 above (`loginctl enable-linger
   <daemon-user>`) and start that user's service manager:
   `systemctl start user@<uid>.service`, where `<uid>` is that user's numeric
   ID from `id -u <daemon-user>`. Every later step that talks to this user's
   service manager (steps 11 and 13, and the "Daemon enabled, not started"
   row in Section 5) reuses this same `<uid>`.
6. Load the AppArmor profile written in silent failure 2 above:
    `apparmor_parser -r /etc/apparmor.d/rootlesskit`.
7. Apply the cgroup delegation drop-in written in silent failure 3 above for
    the daemon user's `user@<uid>.service`, then reload systemd and restart
    that unit so the delegation takes effect: `systemctl daemon-reload` and
    `systemctl restart user@<uid>.service`.
8. Create the daemon user's TLS directory, the destination the requester's
    certificate delivery (Section 4) targets:
    `install -d -m 0700 -o <daemon-user> -g <daemon-user>
    /home/<daemon-user>/tls`.
9. Create the daemon's data directory at the path given
    (`var.docker_data_root`, always required, for example
    `/mnt/docker-data`), owned by the daemon user.
10. If a second data volume was provisioned (2.3): format it once
    (`mkfs.ext4 -i 8192 -L DOCKERDATA <device>`), add a `LABEL=DOCKERDATA
    <data-root> ext4 defaults,noatime,nofail 0 2` line to `/etc/fstab`,
    mount it (`mount -a`), confirm the mount succeeded:
    `mountpoint -q <data-root>`, then restore the daemon user's ownership of
    the mount point, which mounting a filesystem over the directory from
    step 9 replaces with the new filesystem's own root ownership:
    `chown <daemon-user>:<daemon-user> <data-root>`. Skipping this chown
    leaves the data-root owned by root, and the unprivileged rootless daemon
    cannot write to it.
11. Install rootless docker for the daemon user, then stop the daemon it
    starts automatically; it is reconfigured and re-enabled next. The daemon
    user has no interactive login shell (step 5), so run both commands
    through `runuser`, supplying the daemon user's own runtime directory and
    session bus explicitly because `runuser -u` does not propagate the
    caller's environment:

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
    separately, into the directory from step 8, once the instance
    identifier is handed back (Section 4).
13. Enable, but do not start, the daemon user's `docker.service`, again
    through `runuser` with the same environment as step 11:

    ```text
    runuser -u <daemon-user> -- env HOME=/home/<daemon-user> XDG_RUNTIME_DIR=/run/user/<uid> DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/<uid>/bus systemctl --user enable docker.service
    ```

    The daemon's first start reads the TLS material at the paths step 12's
    `daemon.json` names; starting it before that material exists would fail
    and leave the enablement itself undone, so this step stops short of
    starting it.
14. Once every step above has succeeded, write a UTC timestamp to
    `/etc/general-dev-provisioned`. This file's presence, and only its
    presence, is the signal the instance is ready to hand back; nothing
    earlier in this list writes it.

## 4. What to hand back

Once every item above verifies (Section 5), send the requester:

- **The instance identifier** (for example `i-EXAMPLE00000000`), the value
  the requester's own tooling uses to reach this host through SSM.
- **The AWS region** the instance runs in (for example `us-east-1`).
- **If you followed option B in Section 2.1** (an existing network): the
  VPC identifier and the subnet identifier. These become the requester's
  `var.vpc_id` and `var.subnet_id` inputs respectively -- the identifiers
  their own automation needs whenever it does not create the network itself.
- **If you followed option B in Section 2.2** (an existing security group
  and IAM role): the security group identifier and the instance profile
  name. These become the requester's `var.security_group_ids` and
  `var.iam_instance_profile_name` inputs respectively.

Send nothing else: no certificate, no key, no credential of any kind. The
requester delivers the TLS material this instance needs separately, once
they have the instance identifier above.

## 5. How to verify each item

Run every command below against the instance before handing it back. Each
states what a correct instance returns; anything else means the
corresponding provisioning or configuration item above did not take effect.

| Verifies | Command | Expect |
|---|---|---|
| VPC CIDR | `aws ec2 describe-vpcs --vpc-ids <vpc-id> --query "Vpcs[].CidrBlock"` | Matches `var.vpc_cidr` from 2.1 |
| VPC DNS support | `aws ec2 describe-vpc-attribute --vpc-id <vpc-id> --attribute enableDnsSupport --query "EnableDnsSupport.Value"` | `true` |
| VPC DNS hostnames | `aws ec2 describe-vpc-attribute --vpc-id <vpc-id> --attribute enableDnsHostnames --query "EnableDnsHostnames.Value"` | `true` |
| Subnet CIDR, AZ, auto-assign public IPv4 | `aws ec2 describe-subnets --subnet-ids <subnet-id> --query "Subnets[].{Cidr:CidrBlock,Az:AvailabilityZone,AutoAssign:MapPublicIpOnLaunch}"` | `Cidr` matches `var.subnet_cidr`, `Az` matches `var.availability_zone`, `AutoAssign` is `true` |
| Internet gateway attached | `aws ec2 describe-internet-gateways --filters Name=attachment.vpc-id,Values=<vpc-id> --query "InternetGateways[].Attachments[].State"` | `["available"]` |
| Default route to the internet gateway | `aws ec2 describe-route-tables --filters Name=association.subnet-id,Values=<subnet-id> --query "RouteTables[].Routes[?DestinationCidrBlock=='0.0.0.0/0']"` | One route, `GatewayId` set to the internet gateway from the row above |
| No NAT device | `aws ec2 describe-nat-gateways --filter Name=vpc-id,Values=<vpc-id> --query "NatGateways[?State!='deleted']"` | `[]` (empty) |
| Name tag and additional tags | `aws ec2 describe-tags --filters Name=resource-id,Values=<instance-id>` | A `Name` tag matching 2.3's value for the instance, plus every key/value pair given in `var.tags` |
| AMI, instance type, root volume | `aws ec2 describe-instances --instance-ids <instance-id> --query "Reservations[].Instances[].{Ami:ImageId,Type:InstanceType,Root:RootDeviceName}"` | The AMI and instance type given in 2.3 |
| Instance subnet placement | `aws ec2 describe-instances --instance-ids <instance-id> --query "Reservations[].Instances[].SubnetId"` | Matches the subnet from 2.1 |
| Root and data volumes | `aws ec2 describe-volumes --filters Name=attachment.instance-id,Values=<instance-id> --query "Volumes[].{Size:Size,Type:VolumeType,Encrypted:Encrypted}"` | Every listed volume `gp3` and `Encrypted: true`, with the sizes given in 2.3 |
| Termination and stop protection | `aws ec2 describe-instance-attribute --instance-id <instance-id> --attribute disableApiTermination` and `--attribute disableApiStop` | Both `Value: true` |
| IMDSv2 hop limit | `aws ec2 describe-instances --instance-ids <instance-id> --query "Reservations[].Instances[].MetadataOptions"` | `HttpTokens: required`, `HttpPutResponseHopLimit: 2` |
| Zero-ingress security group | `aws ec2 describe-security-groups --group-ids <sg-id> --query "SecurityGroups[].IpPermissions"` | `[]` (empty) |
| Egress rule | `aws ec2 describe-security-groups --group-ids <sg-id> --query "SecurityGroups[].IpPermissionsEgress"` | One rule matching the CIDR block(s) given in 2.2 |
| IAM role trust policy | `aws iam get-role --role-name <role-name> --query "Role.AssumeRolePolicyDocument"` | Exactly one statement, principal `ec2.amazonaws.com`, action `sts:AssumeRole` |
| Managed policy attachment | `aws iam list-attached-role-policies --role-name <role-name>` | `AmazonSSMManagedInstanceCore` present |
| Inline policy scope | `aws iam get-role-policy --role-name <role-name> --policy-name <policy-name> --query "PolicyDocument.Statement[].Resource"` | Exactly the two `/devcontainer/.../*` ARNs from 2.2, no `*` |
| Instance profile role membership | `aws iam get-instance-profile --instance-profile-name <profile-name> --query "InstanceProfile.Roles[].RoleName"` | `["<role-name>"]`, the role from the row above |
| Instance profile attached to the instance | `aws ec2 describe-instances --instance-ids <instance-id> --query "Reservations[].Instances[].IamInstanceProfile.Arn"` | The instance profile's ARN, matching the profile above |
| SSM registration | `aws ssm describe-instance-information --filters Key=InstanceIds,Values=<instance-id>` | One entry, `PingStatus: Online` |
| Package prerequisites installed | On the host: `dpkg-query -W -f='${Status}\n' ca-certificates curl gnupg uidmap dbus-user-session iptables \| grep -c '^install ok installed$'` | `6` |
| Rootful daemon disabled | On the host: `systemctl is-active docker.service; systemctl is-enabled docker.service` | Both report `inactive` / `disabled` (non-zero exit is expected here) |
| Daemon user lingering | On the host: `loginctl show-user <daemon-user> --property=Linger` | `Linger=yes` |
| AppArmor profile loaded | On the host: `cat /sys/kernel/security/apparmor/profiles \| grep rootlesskit` | `/usr/bin/rootlesskit (unconfined)` -- the profile grants `userns` rather than confining the binary, so `unconfined` is the correct, loaded state |
| cgroup v2 delegation | On the host: `cat /sys/fs/cgroup/user.slice/user-<uid>.slice/user@<uid>.service/cgroup.controllers` | Includes `cpuset` and `io` alongside `cpu`, `memory` and `pids` |
| TLS directory permissions | On the host: `stat -c "%a %U %G" /home/<daemon-user>/tls` | `700 <daemon-user> <daemon-user>` |
| Data-root directory ownership | On the host: `stat -c "%U:%G" <data-root>` | `<daemon-user>:<daemon-user>`, whether or not a second data volume was provisioned |
| Data volume mounted | On the host: `mountpoint -q <data-root> && echo mounted` | `mounted` (only if a second data volume was provisioned per 2.3) |
| Daemon configuration | On the host: `cat /home/<daemon-user>/.config/docker/daemon.json` | `data-root` matches the path given, `tls: true`, `tlsverify: true`, TCP host at the address and port given |
| Daemon enabled, not started | On the host: `runuser -u <daemon-user> -- env HOME=/home/<daemon-user> XDG_RUNTIME_DIR=/run/user/<uid> DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/<uid>/bus systemctl --user is-enabled docker.service` | `enabled` (the daemon is not expected to be running yet; Section 4 covers what happens next) |
| Provisioning completed | On the host: `cat /etc/general-dev-provisioned` | A single UTC timestamp; its presence alone confirms every step in Section 3 succeeded |

## 6. What is explicitly not needed

None of the following is required, and adding any of them weakens the
security model this instance is built around rather than strengthening it:

- **No inbound security group rule of any kind.** The only path into this
  instance is an SSM session, which the SSM agent establishes outbound; an
  inbound rule would open a path nothing here needs and everything here is
  built to avoid.
- **No NAT gateway or NAT instance.** The instance's own public IPv4 address
  gives it a return path for the outbound connections it opens itself
  (package installs, image pulls, SSM); nothing needs a NAT device to
  translate an inbound connection that never arrives.
- **No SSH.** Access to the host is an SSM session, not an SSH session; do
  not open port 22, and do not install or configure an SSH server for this
  purpose.
- **No EC2 key pair.** A key pair exists to authenticate an SSH login; with
  no SSH access, there is no login for it to authenticate.
- **No bastion host.** SSM itself is the jump point: it reaches this
  instance without a second host in between to reach through.
