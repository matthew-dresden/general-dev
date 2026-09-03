# general-dev

Personal general-purpose development workspace built on the
[Caylent devcontainer](https://github.com/caylent-solutions/devcontainer)
(`cdevcontainer` CLI). The same repo runs in two modes:

- **Local**. VS Code Dev Containers on the laptop's Docker engine (OrbStack).
- **Remote**. VS Code Dev Containers against a Docker engine on EC2, reached
  through an SSM port forward carrying the docker API under mutual TLS.
  Containers and source live on the instance, so work survives the laptop
  sleeping, restarting, or losing connectivity. There is no key pair, and no
  interactive access to the host: `make exec` opens a shell in a container,
  which is the only shell this workspace offers.

Project repos being worked on are **plain nested clones** inside the
workspace, not submodules, and `repos/` is where they go. They are gitignored
by this repo and each appears as its own repository in Source Control with
nothing to configure: VS Code's scan walks directories and never consults
`.gitignore`, so an ignored clone is found like any other.

## What changed

Each row is a behavior an earlier version of this workspace had, and what it
does now. The identifiers match Section 0 of the platform specification.

| | Was | Is now |
|---|---|---|
| B1 | Remote engine reached over SSH inside SSM, requiring `REMOTE_SSH_KEY_PATH` and a key pair. | Remote engine reached over an SSM port forward with mutual TLS. No key pair exists. |
| B2 | `make shell` opened an interactive shell on the EC2 host. | That target is removed. Host access is not available to the developer at all; `make exec` opens a shell inside a container. |
| B3 | A developer with engine access could obtain root on the EC2 host, through the docker group on a rootful daemon. | Host root is unreachable. The daemon is rootless; containers still run as uid 0 inside a user namespace. |
| B4 | `make remote`, `make build` and `make status` operated on one implicit instance. | The same targets accept `INSTANCE=<name>`, defaulting to `DEFAULT_REMOTE_INSTANCE`. |
| B5 | API tokens were placed in `shell.env`, which was published wholesale to Parameter Store. | Tokens live in a per-secret catalog reached through `devsecret`. `shell.env` carries no credentials. |
| B6 | `git commit --no-verify` succeeded. | Denied by a `PreToolUse` hook and by the pre-commit hook. |
| B7 | `.claude/` was untracked in its entirety. | `.claude/` is tracked except `settings.local.json`, so the plugin and hooks arrive with a clone. |
| B8 | The devcontainer image had no Terraform, Terragrunt or session-manager-plugin. | All three are installed by devcontainer features. An image rebuild is required. |
| B9 | The instance's cloud-init user data was applied by hand, to an instance created in the console. | The instance is declared in Terraform and created by Terragrunt, and its user data is rendered from the module. |
| B10 | `shell.env` was the only place project configuration lived. | Unchanged for configuration. Only credentials moved. |

## Layout

| Path | Purpose |
|---|---|
| `.devcontainer/` | Devcontainer definition (image + features), postcreate setup, shared shell functions |
| `.devcontainer/remote-docker/` | Remote EC2 engine: transport, certificate and secret entry points, instance config, see its [README](.devcontainer/remote-docker/README.md) |
| `.devcontainer/nix-family-os/`, `wsl-family-os/` | Host-side proxy (tinyproxy) helpers for local mode |
| `repos/` | Where project repositories are cloned. Only its `.gitkeep` is tracked |
| `.vscode/settings.json` | Workspace git-repo detection (nested clones) |
| `docs/devcontainer.md` | Deep dive: setup flow, secrets, cdevcontainer contract |
| `CLAUDE.md` | Engineering standards for AI-assisted work in this repo |

## Quick start, local

The local engine is the laptop's own Docker (OrbStack). The workspace folder is
bind-mounted, so an edit is visible on both sides at once.

**The make route, in the order they are run:**

```sh
make init             # create the three gitignored config files from examples
make local            # point docker and VS Code at the local engine
make build            # build the container and run postCreate
make exec             # a shell inside the container
```

**The skill route:** `/devcontainer:setup-local` prepares this machine, checking
each host tool and stating any command it cannot run itself, and
`/devcontainer:launch` builds and opens the container. Both reach the same
container the make targets produce.

`cdevcontainer setup-devcontainer` generates the three gitignored files if you
would rather not use `make init`. Start the host proxy if `HOST_PROXY=true`
(see `nix-family-os/README.md`), then VS Code → **Reopen in Container**.

## First-time setup

Three gitignored files configure the container; each has a committed example:

```sh
cp shell.env.example shell.env
cp .devcontainer/aws-profile-map.json.example .devcontainer/aws-profile-map.json
cp devcontainer-environment-variables.json.example devcontainer-environment-variables.json
```

Replace every `<PLACEHOLDER>`. What each value does, how to have Claude fill
them out, and the differences between macOS, Linux and WSL are in
[docs/environment-files.md](docs/environment-files.md).

Then, once per machine rather than once per container:

```sh
make keybindings      # Shift+Enter = newline in VS Code terminals
```

Everything else the container needs it configures itself, but keybindings are
resolved by the VS Code window, which runs on your machine even when the
workspace is a container, so this one step cannot come from `devcontainer.json`.
Reload the window afterwards. Running Claude Code's `/terminal-setup` from a
container terminal writes the same binding into the *container's* home
directory, where no VS Code process reads it, which is why it appears to do
nothing.

## Quick start, remote

The remote engine is a rootless Docker daemon on an EC2 instance. Nothing
listens for inbound connections: the daemon binds its TLS port to loopback on
the instance, and the only route to it is an SSM port forward, authenticated by
IAM, carrying the docker API under mutual TLS. The certificate authenticates
the client; IAM authorizes the session. Host access does not exist, by design.

Two routes reach the same result. The `make` targets are the mechanism, and the
`/devcontainer:` skills drive those same targets while asking for what they
need and verifying each step; use whichever suits the moment.

**Prerequisites (laptop):** aws CLI v2, session-manager-plugin, docker CLI, git.
For `build`/`rebuild` additionally:

```sh
npm install -g @devcontainers/cli
brew install jq
```

Missing tools fail fast with the install command.

**The make route, in the order they are run:**

```sh
make cert-ca          # once per instance: create its certificate authority
make cert-client      # the client certificate make connect presents
make cert-publish     # issue server material and publish it to Parameter Store
make cert-install     # the instance fetches it and starts its daemon
make push-secrets     # publish this project's shell.env and profile map
make connect          # open the SSM port forward, point docker at the instance
make build            # clone into a volume on the engine, build, run postCreate
make exec             # a shell inside the container
```

**The skill route:** `/devcontainer:setup-remote` performs the same
provisioning and certificate steps and verifies each one before continuing;
`/devcontainer:certs` owns the certificate lifecycle afterward, including
renewal; `/devcontainer:engine` switches which engine is active; and
`/devcontainer:launch` builds and opens the container.

Add `INSTANCE=<name>` to any of the targets above to act on a specific
instance, or set `DEFAULT_REMOTE_INSTANCE`. `make instances` lists what is
configured and marks the active one.

`make build` blocks until the container is actually up and exits non-zero if
the build or postCreate fails. It clones from **origin**, not from this
machine, and refuses to start if the branch has unpushed commits, if
`.devcontainer` has uncommitted changes, or if `shell.env` is newer than the
copy in Parameter Store.

Then VS Code → **Dev Containers: Attach to Running Container…**. Reconnect the
same way after any disconnect, the container never stopped
(`shutdownAction: "none"`). The container bootstraps its secrets from Parameter
Store via the instance role, so there is no manual seeding.

| | |
|---|---|
| `make status` | context, container, image, volumes |
| `make stop` / `make start` / `make restart` | lifecycle; the checkout is untouched |
| `make rename NAME=…` | readable container name |
| `make check` | report uncommitted/unpushed work inside the volume |
| `make clean` / `make rebuild` | destroy / destroy and build again |
| `make exec` | a shell inside the container, the only shell available |
| `make cert-status` | client and CA expiry per instance |
| `make disconnect` | point docker back at the local engine |

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

Each clone shows up as its own repo in Source Control, with nothing to add
anywhere, and it appears the moment you clone it rather than at the next window
open. `repos/` is ignored except for its `.gitkeep`, and being ignored does not
hide a clone from the scan.

That immediacy is the one thing `repos/` buys you. VS Code scans the workspace
when the window opens, and separately watches for new `.git` directories, but
it drops any whose path is already inside an open repository. This workspace
root is itself a repository, so it claims every clone made under it and the
watcher never fires. postCreate writes a `.gitmodules` naming `repos/` as a
submodule path, the one case that lookup skips: the clone is left unclaimed and
VS Code opens it as its own repository.

A clone is itself a repository, so it claims anything beneath it in the same
way. postCreate therefore walks every repository in the workspace and declares
each one unclaimed in whichever repository encloses it, which is what makes a
checkout nested inside a clone show up rather than disappear into its parent.
No gitlink is created anywhere, so `git submodule status`, `update` and `sync`
stay no-ops and `git clone --recurse-submodules` is unaffected. The files are
generated, not committed, so every container rebuild recreates them.

That walk is a snapshot of what exists when the container is built. `repos/` is
declared as a whole directory, so anything cloned there later is still picked up
immediately; a repository cloned later *inside another clone* waits for the next
window open.

To run a *different* project as its own remote devcontainer instead: push it to
GitHub, then from that repo's root run `make push-secrets` and `make build`
(both derive the project name from the directory). Multiple projects, and
multiple clones of one project, run side by side on the shared engine.
Every project gets its own container + volume on the shared engine.

## Conveniences

- `ccd`, `claude --dangerously-skip-permissions`
- `ccdr`, `claude --dangerously-skip-permissions --resume`
- Claude Code starts on the classic renderer and never offers the flicker-free
  fullscreen one, from `.devcontainer/claude-settings.json`. `/tui fullscreen`
  still opts in for the current container.
- Shift+Enter inserts a newline in every VS Code terminal, tmux or not, once
  `make keybindings` has run on the machine.
- kubectl + helm installed (minikube removed); Python 3.14, Node 25, AWS CLI,
  docker-in-docker via devcontainer features.

## Caveats

- This repo's `.devcontainer` diverges from the upstream cdevcontainer catalog:
  asdf support is removed, minikube is disabled, and the postcreate wrapper
  gained the SSM secret bootstrap. Choosing "replace" during a future
  `cdevcontainer setup-devcontainer` would clobber these changes, review the
  git diff and merge back. (Candidate for upstreaming to the catalog.)
- `cdevcontainer` regenerates `shell.env` with an asdf `PATH` line; it is dead
  but harmless. Re-run `push-secrets.sh` after regenerating or rotating tokens.
- Secrets live only in the gitignored local files and SSM Parameter Store
  (`/devcontainer/<project>/…`), never in git.
