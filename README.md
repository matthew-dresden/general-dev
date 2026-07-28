# general-dev

Personal general-purpose development workspace built on the
[Caylent devcontainer](https://github.com/caylent-solutions/devcontainer)
(`cdevcontainer` CLI). The same repo runs in two modes:

- **Local**. VS Code Dev Containers on the laptop's Docker engine (OrbStack).
- **Remote**. VS Code Dev Containers against a Docker engine on EC2, reached
  through SSH-over-SSM. Containers and source live on the instance, so work
  survives the laptop sleeping, restarting, or losing connectivity.

Project repos being worked on (currently `kanon` and `devbench`) are **plain
nested clones** inside the workspace, not submodules. They are gitignored by
this repo and listed in `.vscode/settings.json` `git.scanRepositories` so each
appears as its own repository in Source Control.

## Layout

| Path | Purpose |
|---|---|
| `.devcontainer/` | Devcontainer definition (image + features), postcreate setup, shared shell functions |
| `.devcontainer/remote-docker/` | Remote EC2 engine: tunnel/shell/secrets scripts, instance config, see its [README](.devcontainer/remote-docker/README.md) |
| `.devcontainer/nix-family-os/`, `wsl-family-os/` | Host-side proxy (tinyproxy) helpers for local mode |
| `.vscode/settings.json` | Workspace git-repo detection (nested clones) |
| `docs/devcontainer.md` | Deep dive: setup flow, secrets, cdevcontainer contract |
| `CLAUDE.md` | Engineering standards for AI-assisted work in this repo |

## Quick start, local

1. `cdevcontainer setup-devcontainer` (generates the gitignored `shell.env`,
   `devcontainer-environment-variables.json`, `.devcontainer/aws-profile-map.json`).
2. Start the host proxy if `HOST_PROXY=true` (see `nix-family-os/README.md`).
3. VS Code → **Reopen in Container**.

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

## Quick start, remote

Everything runs through `make` from this directory. `make help` documents each
target in detail; details and instance reference live in
[.devcontainer/remote-docker/README.md](.devcontainer/remote-docker/README.md).

**Prerequisites (laptop):** aws CLI v2, session-manager-plugin, docker CLI, git.
For `build`/`rebuild` additionally:

```sh
npm install -g @devcontainers/cli
brew install jq
```

Missing tools fail fast with the install command.

```sh
make push-secrets     # once per project (publishes shell.env to Parameter Store)
make connect          # SSH-over-SSM tunnel + docker context switch
make build            # clone into a volume on the engine, build, run postCreate
```

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
| `make shell` | zsh on the EC2 host itself |
| `make disconnect` | point docker back at the local engine |

Terminals inside the container open in a shared tmux session, so a Claude
session or long build survives closing VS Code. `tm-help` in the container
lists the commands and key bindings.

## Working on projects

```sh
# inside the (local or remote) devcontainer
cd /workspaces/general-dev
git clone https://github.com/caylent-solutions/kanon
git clone https://github.com/caylent-solutions/devbench
```

Each clone shows up as its own repo in Source Control. For a **new** project
clone, add it to `.gitignore` and to `git.scanRepositories` in
`.vscode/settings.json` (auto-detection skips gitignored folders).

To run a *different* project as its own remote devcontainer instead: push it to
GitHub, then from that repo's root run `make push-secrets` and `make build`
(both derive the project name from the directory). Multiple projects, and
multiple clones of one project, run side by side on the shared engine.
Every project gets its own container + volume on the shared engine.

## Conveniences

- `ccd`, `claude --dangerously-skip-permissions`
- `ccdr`, `claude --dangerously-skip-permissions --resume`
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
