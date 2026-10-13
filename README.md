# general-dev

A reusable devcontainer platform: one repository that creates and manages
one to many separate development containers, whether on this laptop's
Docker engine, on remote EC2 engines, on SSH engines, on Azure engines, or
on any mix of them at once. The projects you work on are cloned inside the
workspace as plain nested repositories.

What that buys you, and why you would use it:

- **Many containers, four kinds of engine.**
  A container per clone, per branch under review, or per project.
  - Name one at creation: `make build NAME=x`.
  - List them across every engine at once: `make list-containers`.
  - Address each engine directly (`ENGINE=local` or `ENGINE=<instance>`), so
    parallel terminals never switch the docker context underneath each other.
- **Work survives the laptop.**
  Local containers bind-mount this checkout. Remote containers live on an
  engine reached over the network, and keep running through sleeps,
  restarts and offline stretches.
  - **EC2 engine.** Reached through an SSM port forward carrying the docker
    API under mutual TLS. No key pair and no host access, by design.
  - **SSH engine.** Reached through a managed ssh local forward.
  - **Azure engine.** Reached through an Azure Relay hybrid connection the
    VM dials out to; no inbound ports, no public IP, and no SSH anywhere,
    by design.
  - On every path, `make exec` opens a shell in a container: the only shell
    this workspace offers.
- **Every operation is automated.**
  The whole lifecycle for engines and containers alike runs through make
  targets, one command each.
  - Engines: provisioning, certificates, secrets, power, teardown.
  - Containers: building, naming, starting, stopping, checking, fleet
    operations, teardown.
  - `make help` lists them all. The `gd-*` skills drive the same targets
    when an agent runs them for you.

## Quick start, local

The local engine is the laptop's own Docker (OrbStack). The workspace folder is
bind-mounted, so an edit is visible on both sides at once.

**The make route, in the order they are run:**

```sh
make init             # create the four gitignored config files from examples
make creds-init       # store each keychain credential the hostcreds manifest names
make local            # point docker and VS Code at the local engine
make build            # build the container, run postCreate, push every credential
make exec             # a shell inside the container
```

Before `make build`, replace every `<PLACEHOLDER>` in the first three files
`make init` created; what each value does is in
[docs/environment-files.md](docs/environment-files.md). Then, once per
machine rather than once per container:

```sh
make keybindings      # Shift+Enter = newline in VS Code terminals
```

Keybindings are resolved by the VS Code window, which runs on your machine
even when the workspace is a container, so this one step cannot come from
`devcontainer.json`; reload the window afterwards.

**The skill route:** `gd-env-setup-local` prepares this machine, checking
each host tool and stating any command it cannot run itself, and
`gd-container-local` builds and opens the container. Both routes reach the
same container the make targets produce.

Deeper on each step:

- Every configuration value, the macOS/Linux/WSL differences, and how to
  have an agent fill them out: [docs/environment-files.md](docs/environment-files.md)
- The full ordered runbook, fresh machine to verified container with one
  verification per step: [docs/environment-setup.md](docs/environment-setup.md)
- Behind a corporate proxy, the host-side proxy helpers:
  [nix-family-os](.devcontainer/nix-family-os/README.md)
  (Windows/WSL: [wsl-family-os](.devcontainer/wsl-family-os/README.md))

## Quick start, remote

Three kinds of remote engine, one workflow shape each: any machine reachable
over SSH that runs Docker (general SSH), an EC2 instance this repository
provisions (AWS EC2), or an Azure virtual machine this repository provisions
(Azure). All three clone the workspace into a volume on the engine, and all
three keep the container running after you disconnect.

### General SSH

Works against any endpoint where `ssh <user>@<host> docker version` can
answer: a Linux daemon through its socket, or Windows through Docker
Desktop's exposed TCP endpoint.

**The make route, in the order they are run:**

```sh
make engine-init INSTANCE=<name> HOST=<host> USER=<user>
                                         # declares the engine (engine.json);
                                         # PORT= and DOCKER_ADDR= optional
                                         # (defaults: 22 and the Linux docker
                                         # socket; a Windows endpoint needs
                                         # DOCKER_ADDR=tcp://localhost:2375)
make engine-verify INSTANCE=<name>       # one-shot proof: ssh forward opens,
                                         # docker handshake answers, forward closes
make remote INSTANCE=<name>              # open the forward as a background daemon,
                                         # point docker at the engine, return
make build INSTANCE=<name>               # clone into a volume on the engine, build, run postCreate
make exec INSTANCE=<name>                # a shell inside the container
```

No AWS involvement: configuration seeds into the workspace volume at build
time, and credentials arrive by the hostcreds push over the same forward.
Then VS Code → **Dev Containers: Attach to Running Container…**.

**The skill route:** `gd-engine-ssh` drives the whole ssh-engine lifecycle,
from `engine-init` through build, verifying each step.

Deeper on each step:

- The full path with platform guidance (Linux socket vs the Windows TCP
  requirement): [environment-setup.md, "The ssh engine
  path"](docs/environment-setup.md)
- Engine architecture and the daily workflow:
  [remote-docker README](.devcontainer/remote-docker/README.md)

### AWS EC2

The EC2 engine is a rootless Docker daemon on an EC2 instance. Nothing
listens for inbound connections: the daemon binds its TLS port to loopback on
the instance, and the only route to it is an SSM port forward, authenticated by
IAM, carrying the docker API under mutual TLS. The certificate authenticates
the client; IAM authorizes the session. Host access does not exist, by design.

**Prerequisites (laptop):** aws CLI v2, session-manager-plugin, docker CLI,
git; for `build`/`rebuild` additionally `npm install -g @devcontainers/cli`
and `jq`. Missing tools fail fast with the install command.

**The make route, in the order they are run:**

```sh
make instance-init INSTANCE=<project>    # scaffold remote-instances/<project>/terragrunt.hcl
                                          # then edit its inputs: instance type, volume
                                          # sizes, availability zone, tags
make instance-deploy INSTANCE=<project>  # converge: provision, record the EC2 id, trust
                                          # chain where missing, push secrets
make remote INSTANCE=<project>           # open the SSM forward as a background daemon,
                                          # point docker at the instance, return
make build INSTANCE=<project>            # clone into a volume on the engine, build, run postCreate
make exec INSTANCE=<project>             # a shell inside the container
```

`make instance-init` writes the one file a new instance needs and never
deploys; `make instance-deploy` applies it and is safe to re-run, and prints
the follow-on chain above when it finishes. Then VS Code → **Dev Containers:
Attach to Running Container…**. The same step is the way back after any
disconnect; the container never stopped (`shutdownAction: "none"`).

**The skill route:** `gd-env-setup-remote` performs the same provisioning and
certificate steps and verifies each one before continuing, `gd-cert-lifecycle`
owns certificate renewal afterward, and `gd-instance-list` validates which
engine is active. The full roster: [docs/skills.md](docs/skills.md).

Deeper on each step:

- Which instance a command acts on when `INSTANCE` is omitted, and
  addressing engines without switching context (`ENGINE=local|<name>`):
  [environment-setup.md, "Working with several engines at
  once"](docs/environment-setup.md)
- Instance architecture, prerequisites and the daily connect/disconnect
  workflow: [remote-docker README](.devcontainer/remote-docker/README.md)
- Provisioning an instance by hand (IAM, network, user data):
  [docs/ec2-requirements.md](docs/ec2-requirements.md)

### Azure

The azure engine is a rootless Docker daemon on an Azure virtual machine.
Nothing listens for inbound connections: the daemon binds its TLS port to
loopback on the VM, and the only route to it is an Azure Relay hybrid
connection the VM dials out to. Entra ID authorizes the laptop's sender half
of that connection; mutual TLS then authenticates the docker client at the
daemon. No public IP, no inbound rule, and no SSH anywhere, by design. The
VM's relay listener stops itself after fifteen idle minutes, so the relay
accrues listener time only while you are working.

**Prerequisites (laptop):** azure CLI (`brew install azure-cli`, logged in
with `az login`), docker CLI, git; for `build`/`rebuild` additionally
`npm install -g @devcontainers/cli` and `jq`, the same build prerequisites
the EC2 path has. Missing tools fail fast with the install command.

**The make route, in the order they are run:**

```sh
make instance-init INSTANCE=<project> CLOUD=azure LOCATION=<location>
                                          # scaffold remote-instances/<project>/terragrunt.hcl
                                          # ([VM_SIZE=] optional); then edit its inputs and
                                          # add your Entra object id to relay_sender_object_ids
                                          # and keyvault_officer_object_ids, see below
make instance-deploy INSTANCE=<project>  # converge: provisions the resource group, vnet and
                                          # subnet, the VM, its Key Vault, the relay namespace
                                          # and hybrid connection, and the state storage;
                                          # records the relay address; prints the follow-on chain
make remote INSTANCE=<project>           # open the relay forward as a background daemon,
                                          # point docker at the engine, return
make build INSTANCE=<project>            # clone into a volume on the engine, build, run postCreate
make exec INSTANCE=<project>             # a shell inside the container
```

`make instance-init` writes the one file a new azure instance needs and
never deploys; the two object-id lists are the one edit the scaffold cannot
do for you, because they name *you*: `relay_sender_object_ids` grants your
Entra identity the Relay Sender role on the namespace (the right to open the
sender half of the hybrid connection the forward rides), and
`keyvault_officer_object_ids` grants the Key Vault Secrets Officer role on
the vault (the right to publish the instance's TLS material and secrets into
it). The deploy carries both in as role assignments on the first run, so
without them it provisions fine and the connect and the credential push
afterwards have no grant to stand on. `make instance-deploy` applies the
file, is safe to re-run, and prints the follow-on chain above when it
finishes. Then VS Code → **Dev Containers: Attach to Running Container…**.
The same step is the way back after any disconnect; the container never
stopped (`shutdownAction: "none"`).

**The skill route:** `gd-env-setup-azure` performs the same provisioning and
certificate steps and verifies each one before continuing, `gd-cert-lifecycle`
owns certificate renewal afterward, and `gd-instance-list` validates which
engine is active. The full roster: [docs/skills.md](docs/skills.md).

Deeper on each step:

- Provisioning an instance by hand (network, identity, relay, host
  configuration): [docs/azure-requirements.md](docs/azure-requirements.md)
- The Terraform module and its full input surface:
  [provider/azure/README.md](provider/azure/README.md)
- Instance architecture and the state model:
  [remote-instances/README.md](remote-instances/README.md)
- The full ordered runbook: [environment-setup.md, "The Azure engine
  path"](docs/environment-setup.md)

## Daily use

| | |
|---|---|
| `make up` | from any state: refresh the forward (remote), build or start as needed, open VS Code |
| `make status` | the active engine's backend, container, image and volumes |
| `make list-containers` | every engine at once, one row per container this project owns |
| `make stop` / `make start` / `make restart` | lifecycle; the checkout is untouched |
| `make rename NAME=…` (or `NAME=…` on `build`/`up`) | readable container names |
| `make check` | report uncommitted/unpushed work inside a remote volume |
| `make clean` / `make rebuild` | destroy / destroy and build again |
| `make exec` | a shell inside the container, the only shell available |
| `make cert-status` | client and CA expiry per instance |
| `make disconnect` | point docker back at the local engine |

The lifecycle targets also take `ALL=1` for the fleet form, one engine after
the other; `make clean ALL=1` requires `CONFIRM=destroy`. `ENGINE=local` or
`ENGINE=<instance-name>` addresses one engine without switching the context
other terminals share: [environment-setup.md](docs/environment-setup.md)'s
"Working with several engines at once".

Terminals inside the container open in a shared tmux session, so a Claude
session or long build survives closing VS Code. `tm-help` in the container
lists the commands and key bindings.

## Working on projects

```sh
# inside the (local or remote) devcontainer.
# $PROJECT_NAME is this repository's own directory name, so the path is
# correct whatever the project is called; nothing here names another project.
cd "/workspaces/${PROJECT_NAME}/repos"
git clone https://github.com/<org>/<your-project>
git clone https://github.com/<org>/<another-project>
```

Each clone shows up as its own repo in Source Control the moment you clone
it, with nothing to add anywhere, even though `repos/` is gitignored. To
run a *different* project as its own remote devcontainer instead: push it to
GitHub, then from that repo's root run `make push-secrets` and `make build`
(both derive the project name from the directory); every project gets its
own container and volume on the shared engine.

How VS Code discovers nested repositories, and what postCreate writes to
make clones under clones visible: the Definition section of
[devcontainer.md](docs/devcontainer.md).

## Conveniences

- `ccd`, `claude --dangerously-skip-permissions`
- `ccdr`, `claude --dangerously-skip-permissions --resume`
- opencode, installed by postCreate (no devcontainer feature ships it),
  configured for the z.ai coding plan with GLM 5.3 flagship and GLM 5.3
  Flash; its config still injects the key through `{env:ZAI_API_KEY}`, and the
  variable itself is supplied by the hostcreds startup block from a
  `ZAI_API_KEY` manifest entry, never committed and never in `shell.env`.
- The skills suite: `make skills-install`, `make skills-remove` and
  `make skills-list` wire, unwire and report this repo's `.agents/skills`
  roster in an agent on this Mac, selected by `AGENT=opencode|claude|both`
  and `SCOPE=global|project|runtime`. For example, `make skills-install
  AGENT=claude SCOPE=global` symlinks the roster into `~/.claude/skills`.
  Symlinks, never copies; see [docs/skills.md](docs/skills.md).
- `make verify-container` re-checks the pushed credentials inside the
  container: fragment modes, the startup block, silent shell startup, and
  git and aws reachability for whichever sources the manifest names.
- One remote engine per project under `remote-instances/`: the `make help`
  INSTANCES group scaffolds, converges, powers and retires the EC2 kind and
  the azure kind (`CLOUD=azure` at scaffold time selects it; `make
  list-instances` shows every instance's live state). The third engine
  kind, SSH, is any machine reachable over SSH that runs Docker: a Linux
  daemon through its socket or a TCP endpoint, or Windows through Docker
  Desktop's exposed TCP endpoint. An SSH engine, declared by a
  `remote-instances/<name>/engine.json` manifest with no AWS behind it, is
  scaffolded, verified and removed with `make engine-init`,
  `make engine-verify` and `make engine-remove`. The fourth engine kind,
  azure, is an Azure virtual machine reached through an Azure Relay hybrid
  connection the VM dials out to — no public IP, no inbound rule, no SSH —
  scaffolded, converged, powered and retired with the same `instance-*`
  targets, `make instance-init … CLOUD=azure LOCATION=<location>`,
  `make instance-deploy`, `make instance-stop`/`instance-start` and
  `make instance-destroy`.
- Claude Code starts on the classic renderer and never offers the flicker-free
  fullscreen one, from `.devcontainer/claude-settings.json`. `/tui fullscreen`
  still opts in for the current container.
- Claude Code talks to the z.ai GLM Coding Plan: `/model opus` (or
  `--model opus`) runs the GLM 5.3 flagship, `/model sonnet` runs
  GLM 5.3 Flash, and `/status` shows the active one. The key comes from
  `ZAI_API_KEY` at request time, never from a file.
- Shift+Enter inserts a newline in every VS Code terminal, tmux or not, once
  `make keybindings` has run on the machine.
- kubectl + helm installed (minikube removed); Python 3.14, Node 25, AWS CLI,
  docker-in-docker via devcontainer features.

## Caveats

- This repo's `.devcontainer` is self-managed: asdf is not installed,
  minikube is disabled, and the postcreate wrapper carries the SSM secret
  bootstrap. Nothing regenerates the directory, so changes here stay.
  (Provenance and license attribution: `NOTICE`.)
- Rotating a credential is `make creds-init` plus `make push-creds`, never a
  `shell.env` edit; after any `shell.env` change, re-run `make push-secrets`
  so the remote copies match.
- Credentials live only in the macOS keychain, git's own credential helper
  and the AWS SSO session, all named in the gitignored hostcreds manifest,
  plus the fragments `make push-creds` writes inside the container. Parameter
  Store (`/devcontainer/<project>/…`) holds the credential-free `shell.env`
  and profile map for an EC2 engine; an azure engine's copies publish to the
  instance's own Key Vault instead, one vault per instance. None of it is
  ever in git.

## Layout

| Path | Purpose |
|---|---|
| `.devcontainer/` | Devcontainer definition (image + features), postcreate setup, shared shell functions |
| `.devcontainer/hostcreds.map.json` (+ committed `.example`) | Hostcreds manifest, gitignored: every credential this machine pushes into the container, named with its source |
| `.devcontainer/remote-docker/` | Remote engines, EC2 and azure: the transports (SSM forward, relay forward), certificate and secret entry points, instance config, see its [README](.devcontainer/remote-docker/README.md) |
| `remote-instances/` | One remote engine per project: a Terragrunt deployment (`terragrunt.hcl`) per EC2 or azure engine — the azure one including `root-azure.hcl` and the `_envcommon/remote-azure.hcl` layer — scaffolded by `make instance-init`, or an SSH engine declared by an `engine.json` manifest, scaffolded by `make engine-init`; see its [README](remote-instances/README.md) |
| `provider/azure/` | The azure Terraform module those deployments consume: network, platform unit (security group, identity, Key Vault, relay) and VM, see its [README](provider/azure/README.md) |
| `.devcontainer/nix-family-os/`, `wsl-family-os/` | Host-side proxy (tinyproxy) helpers for local mode |
| `repos/` | Where project repositories are cloned. Only its `.gitkeep` is tracked |
| `.agents/skills/` | Canonical agent-agnostic skills home (roster prefixed `gd-`); wired into an agent on this Mac by the `skills-*` make targets |
| `.vscode/settings.json` | Workspace git-repo detection (nested clones) |
| `docs/devcontainer.md` | Deep dive: setup flow, secrets, nested repositories, the devcontainer file contract |
| `docs/environment-setup.md` | Ordered runbook: fresh machine to verified container, one verification per step |
| `docs/environment-files.md` | Every private configuration value and what it does |
| `docs/ec2-requirements.md` | Provisioning an EC2 instance by hand: network, IAM, host configuration |
| `docs/azure-requirements.md` | Provisioning an azure instance by hand: network, identity and relay, host configuration |
| `docs/skills.md` | The skill roster: families, flows, coverage guarantee, and installing it for an agent |
| `CLAUDE.md` | Engineering standards for AI-assisted work in this repo |
