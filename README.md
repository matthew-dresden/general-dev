# general-dev

A reusable devcontainer platform: one repository that creates and manages
one to many separate development containers -- on this laptop's Docker
engine, on remote EC2 engines, or both at once -- with the projects you
work on cloned inside the workspace as plain nested repositories.

What that buys you, and why you would use it:

- **Many containers, two kinds of engine.** A container per clone, per
  branch under review, or per project -- named at creation (`make build
  NAME=x`), listed across every engine at once (`make list-containers`),
  and addressed per engine (`ENGINE=local` or `ENGINE=<instance>`) so
  parallel terminals never switch the docker context underneath each other.
- **Work survives the laptop.** Local containers bind-mount this checkout;
  remote containers live on an EC2 engine reached through an SSM port
  forward carrying the docker API under mutual TLS, so they keep running
  through sleeps, restarts and offline stretches. There is no key pair and
  no host access by design: `make exec` opens a shell in a container, the
  only shell this workspace offers.
- **Every operation is automated.** Provisioning an engine, its
  certificates and secrets, building, naming, starting, stopping, checking,
  fleet operations and teardown -- the whole lifecycle for engines and
  containers alike is driven by make targets, one command each; `make help`
  lists them all, and the `gd-*` skills drive those same targets when an
  agent runs them for you.

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
`devcontainer.json` -- reload the window afterwards.

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

The remote engine is a rootless Docker daemon on an EC2 instance. Nothing
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
Attach to Running Container…** -- the same way back after any disconnect; the
container never stopped (`shutdownAction: "none"`).

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
it, with nothing to add anywhere -- even though `repos/` is gitignored. To
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
  `ZAI_API_KEY` manifest entry -- never committed, never in `shell.env`.
- The skills suite: `make skills-install`, `make skills-remove` and
  `make skills-list` wire, unwire and report this repo's `.agents/skills`
  roster in an agent on this Mac, selected by `AGENT=opencode|claude|both`
  and `SCOPE=global|project|runtime` -- `make skills-install AGENT=claude
  SCOPE=global`, for instance, symlinks the roster into `~/.claude/skills`.
  Symlinks, never copies; see [docs/skills.md](docs/skills.md).
- `make verify-container` re-checks the pushed credentials inside the
  container: fragment modes, the startup block, silent shell startup, and
  git and aws reachability for whichever sources the manifest names.
- One remote engine per project under `remote-instances/`: the `make help`
  INSTANCES group scaffolds, converges, powers and retires them
  (`make list-instances` shows every instance's live state).
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
  and the AWS SSO session -- named in the gitignored hostcreds manifest --
  plus the fragments `make push-creds` writes inside the container. Parameter
  Store (`/devcontainer/<project>/…`) holds the credential-free `shell.env`
  and profile map. None of it is ever in git.

## Layout

| Path | Purpose |
|---|---|
| `.devcontainer/` | Devcontainer definition (image + features), postcreate setup, shared shell functions |
| `.devcontainer/hostcreds.map.json` (+ committed `.example`) | Hostcreds manifest, gitignored: every credential this machine pushes into the container, named with its source |
| `.devcontainer/remote-docker/` | Remote EC2 engine: transport, certificate and secret entry points, instance config, see its [README](.devcontainer/remote-docker/README.md) |
| `remote-instances/` | One Terragrunt deployment per remote engine, scaffolded by `make instance-init`; see its [README](remote-instances/README.md) |
| `.devcontainer/nix-family-os/`, `wsl-family-os/` | Host-side proxy (tinyproxy) helpers for local mode |
| `repos/` | Where project repositories are cloned. Only its `.gitkeep` is tracked |
| `.agents/skills/` | Canonical agent-agnostic skills home (roster prefixed `gd-`); wired into an agent on this Mac by the `skills-*` make targets |
| `.vscode/settings.json` | Workspace git-repo detection (nested clones) |
| `docs/devcontainer.md` | Deep dive: setup flow, secrets, nested repositories, the devcontainer file contract |
| `docs/environment-setup.md` | Ordered runbook: fresh machine to verified container, one verification per step |
| `docs/environment-files.md` | Every private configuration value and what it does |
| `docs/ec2-requirements.md` | Provisioning an instance by hand: network, IAM, host configuration |
| `docs/skills.md` | The skill roster: families, flows, coverage guarantee, and installing it for an agent |
| `CLAUDE.md` | Engineering standards for AI-assisted work in this repo |
