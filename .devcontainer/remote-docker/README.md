# remote-docker. EC2 remote Docker engine for devcontainers

Laptop-side tooling that connects VS Code Dev Containers to a Docker engine
running on an EC2 instance, over SSH carried inside an AWS SSM session.
Devcontainers run **on the instance**, with source code cloned into named
Docker volumes there, so containers keep running and lose nothing when the
laptop disconnects, sleeps, or shuts down.

## Architecture

```text
laptop                                        EC2 (us-east-1, no inbound ports)
──────                                        ─────────────────────────────────
docker CLI / VS Code ── ssh ── SSM session ── sshd ── dockerd (data on 1TB volume)
                              (IAM auth)              └─ devcontainer per project
                                                         └─ source in named volume
aws ssm put-parameter ───────────────────────► SSM Parameter Store
      (push-secrets.sh)                          └─ read at container create via
                                                    instance role (IMDSv2)
```

- **No inbound access:** the security group has zero inbound rules. All
  connectivity is outbound SSM. SSH host key + key pair still authenticate the
  SSH layer inside the tunnel.
- **Survives disconnects:** `shutdownAction: "none"` in devcontainer.json keeps
  containers running when VS Code detaches; `live-restore` keeps them running
  across dockerd restarts; source lives in Docker volumes on the instance.
- **Secrets:** never in git. `push-secrets.sh` publishes the local gitignored
  `shell.env` / `aws-profile-map.json` to SSM Parameter Store per project; the
  postcreate wrapper fetches them on the instance via the instance role.

## Prerequisites (laptop)

- aws CLI v2, authenticated for the sandbox account (`aws sso login`)
- [session-manager-plugin](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html)
- docker CLI (a local engine is not required)
- git
- The private key for the EC2 key pair (`REMOTE_SSH_KEY_PATH` in `config.env`)

`make build` / `make rebuild` additionally need:

```sh
npm install -g @devcontainers/cli
brew install jq            # and python3, used to compare timestamps
```

Every target checks its own prerequisites and fails with the install command
for whatever is missing.

## Scripts

The repo-root `Makefile` is the front door, run `make help` for detailed
documentation of every operation. It contains no logic of its own; each target
delegates to one of these scripts, so there is a single implementation per
operation and both entry points stay in step.

| Script | Purpose |
|---|---|
| `docker-tunnel.sh` | Install the managed SSH config block, create/refresh the `general-dev-remote` docker context, switch to it, verify end-to-end. |
| `shell.sh` | Interactive zsh on the instance (same tunnel). |
| `push-secrets.sh` | Transform local `shell.env` for remote use and publish both secret files to SSM Parameter Store (`/devcontainer/<project>/…`). |
| `container.sh` | Container lifecycle for the current project: status, start/stop/restart, unpushed-work check, teardown, rebuild report. |
| `lib.sh` | Shared functions (sourced by the others), including the failure translation described below. |
| `../resmon-disks.py` | Runs from `postAttachCommand`, by the absolute path postCreate links it to (`~/.local/bin/`), because an attached container gives the hook no workspace to run in. Points the Resource Monitor extension at the devices behind `/workspaces` and `/tmp`, resolved per host. |
| `config.env` | Defaults (instance ID, region, profile, context names). Every value is overridable via environment variables. |
| `ec2-user-data.yaml` | cloud-init config the instance was provisioned with (kept for reproducibility). |

## Launching a project on the remote engine

1. Push the project repo to GitHub (must be reachable from VS Code).
2. `./push-secrets.sh`, once per project (re-run after token rotation or
   template changes). Use `PROJECT_NAME=<repo-basename> ./push-secrets.sh` for
   other projects.
3. `./docker-tunnel.sh`, activates the remote docker context.
4. VS Code → **Dev Containers: Clone Repository in Container Volume…** →
   pick the GitHub repo. The postcreate wrapper bootstraps `shell.env` and
   `aws-profile-map.json` from Parameter Store automatically.
5. Reconnect later (any laptop state): run `./docker-tunnel.sh`, then VS Code →
   **Dev Containers: Attach to Running Container…** (or reopen the recent
   workspace entry). The container was running the whole time.

Multiple projects run concurrently: each clone-in-volume creates its own
volume + container on the shared engine.

### Naming instances

New containers are created as `<repo>-<devcontainerId>` (`runArgs` in
`devcontainer.json`) instead of taking Docker's random names. The id keeps
every instance distinct, so several clones of the *same* repo coexist, but it
is ~50 characters, which is no help in the **Attach to Running Container**
list. Rename each instance to what it is actually for:

```bash
make rename NAME=general-dev-review
```

Because every instance of a repo carries the same `devcontainer.config_file`
label, targets that act on a single container refuse to guess between them.
`make status` lists them all; `CONTAINER=<name>` selects one:

```bash
make stop CONTAINER=general-dev-review
make clean CONTAINER=general-dev-review
```

## Daily workflow, connect & disconnect

One-time Mac setup (tools, SSO profile, key verification, `~/.zshrc` aliases)
is automated by the agent prompt at `docs/mac-setup-prompt.md`.
It installs these aliases:

| Alias | Makefile equivalent | What it does |
|---|---|---|
| `gdev-connect` | `make connect` | Runs `docker-tunnel.sh`: refreshes the SSM tunnel config and switches the docker context to `general-dev-remote`. |
| `gdev-disconnect` | `make disconnect` | Switches the docker context back to the local engine (OrbStack). |
| `gdev-shell` | `make shell` | zsh on the EC2 host. |

The aliases work from any directory; the `make` targets work from the repo root
and cover the container lifecycle as well. `make help` documents all of them.

**Connect (or reconnect after sleep/reboot/SSO expiry):**

1. `gdev-connect`, if it fails with an auth error, run
   `aws sso login --profile default` first.
2. VS Code → new window → **Dev Containers: Attach to Running Container…**
   → pick the `vsc-<project>…` container. Your container was running the
   whole time; terminals and processes inside it are exactly where you left
   them (a Claude session that lost its terminal resumes with `ccdr`).

> Always reconnect via **Attach to Running Container**. Do NOT reopen the
> "… in volume" Open Recent entry: the extension's clone-in-volume reattach
> path removes its own bootstrap helper mid-session (exit 137, "No such
> container" in the Dev Containers log), which kills the new window every
> time on a high-latency remote context. The clone-in-volume command is only
> for the FIRST create of a project. Attach uses no helper and is immune.

**Disconnect, there is nothing to shut down:**

- Just close the VS Code window (or sleep/shut the Mac). Containers on EC2
  keep running (`shutdownAction: "none"`), including any long-lived agent
  sessions inside them.
- `gdev-disconnect` is only needed when you want NEW VS Code / docker
  operations to target the local OrbStack engine again. It stops nothing
  remotely, and already-open remote windows are unaffected.

### Git credentials when nothing is attached

While VS Code is attached, the Dev Containers extension forwards the laptop's
git credentials into the container, so git "just works". Detach, which is the
point of the tmux session above, and that forwarding is gone. An unattended
agent can commit but cannot push unless the container holds credentials of its
own.

So the container gets a credential of its own:

```bash
make push-git-creds
```

`make build` runs this as its final step, so a freshly built container already
has one. Run it again whenever the credential on your machine changes or is
revoked.

It copies whatever credential already works **on this machine**, asked for via
`git credential fill`, so it uses your configured helper (osxkeychain on macOS,
libsecret / gh / store on Linux) with no separate token to maintain. The secret
travels on stdin, so it appears in neither the exec's arguments nor its inspect
output, and lands in `~/.git-credentials` mode 600. The command then proves it
works with `git ls-remote` and fails if the remote rejects it.

> The credential is stored in a file on the shared engine. Anyone with docker
> access to that instance can read it.

postCreate no longer writes any credential. It only sets `credential.helper
store` and the SSH→HTTPS URL rewrite; `GIT_AUTH_METHOD`, `GIT_TOKEN` and the
ssh-key path in `shell.env` are no longer consulted. That replaced a setup
which had to be rotated by hand and failed silently, and which wrote `.netrc`
while configuring the `store` helper, so it never authenticated anything even
when the token was valid.

To check a container's credentials directly, with no VS Code window attached:

```bash
docker exec -u vscode <container> bash -lc \
  'cd /workspaces/<project> && GIT_TERMINAL_PROMPT=0 git ls-remote origin'
```

`could not read Username` means it has none, run `make push-git-creds`. Local
commands like `git status` and `make check` keep working regardless, so this
failure stays hidden until something tries to push.

### Keeping work running across a disconnect

VS Code **terminates terminal processes when the window closes**. Terminal
persistence (`persistentSessionReviveProcess`) restores the tab and its
scrollback and starts a fresh shell, a command that was mid-run does not
resume. No VS Code setting changes that.

So every terminal opens inside a shared tmux session named `main`
(`terminal.integrated.profiles.linux` in `devcontainer.json`). The tmux server
is a daemon VS Code does not own, so anything running in it, a Claude session,
a long build, survives the window closing. Reattach to the container, open a
terminal, and you are back in the same session with the work still going.
Nothing to remember on the way out; `-A` attaches to the existing session or
creates it.

Inside the container, `tm-help` lists every command and key binding. Type
`tm` and press Tab to discover them; each also takes `--help` for detail.
They are defined in `.devcontainer/tmux-commands.sh`, sourced into bash and
zsh by postcreate.

| Command | Does |
|---|---|
| `tm-session-open [name]` | Go to a session, creating it if missing (default: `main`) |
| `tm-session-pick` | List sessions and open the one you name |
| `tm-session-new <name>` | Create a session and go to it; refuses an existing name |
| `tm-session-list` | List sessions |
| `tm-session-current` | Name of the session you are in |
| `tm-session-rename <name>` | Rename the session you are in |
| `tm-window-new [name]` | New window in this session |
| `tm-window-list` | List windows in the current session |
| `tm-window-rename <name>` | Rename the current window |
| `tm-session-detach` | Leave the session; everything keeps running |
| `tm-session-kill <name>` | Kill one named session |
| `tm-session-kill-all` | Kill every session |
| `tm-help` | All commands and key bindings |

Every window and pane runs zsh. tmux opens `default-shell`, which is the
account's login shell and is bash here, so the terminal profile's `zsh`
argument only applied when it *created* the session: attaching to an existing
one, or opening a window with `Ctrl+b c`, landed in bash. `tmux.conf` sets
`default-shell`, and postcreate rewrites that line with the zsh it resolved,
since a committed config cannot know where zsh was installed.

A session holds windows, a window holds panes. A window is a whole screen,
like a tab, and only one is visible at a time; panes are splits inside the
window you are looking at, and are all visible together.

| Key | Does |
|---|---|
| `Ctrl+b w` | Pick a window from a list |
| `Ctrl+b c` / `Ctrl+b ,` | New window / rename it |
| `Ctrl+b n` / `Ctrl+b p` / `Ctrl+b <number>` | Switch windows |
| `Ctrl+b %` / `Ctrl+b "` | Split the window into panes, left\|right / top-bottom |
| `Ctrl+b o` / `Ctrl+b x` | Cycle between panes / close one |
| `Ctrl+b [` | Scroll back (`q` exits) |
| Plain non-persistent shell | pick the `zsh` profile from the terminal dropdown |

The terminal dropdown offers three profiles. `tmux` is the default and always
opens the shared `main` session. `tmux: pick session` runs `tm-session-pick`, which
lists the sessions that exist and opens the one you name, creating it if the
name is new. `zsh` is a plain shell outside tmux. A VS Code profile is fixed
configuration and cannot list sessions that did not exist when the container
was built, which is why the choice is made by `tm-session-pick` rather than by more
profiles.

The `b` is lowercase and takes no Shift: hold Ctrl, tap `b`, release both, then
press the second key on its own. Shift appears only when the character itself
needs it, as in `%` and `"`.

`.devcontainer/tmux.conf`, installed to `~/.tmux.conf` by postcreate, turns the
mouse on: the wheel scrolls the pane's own history, clicking focuses a pane,
and dragging a border resizes one. Without it tmux ignores the wheel and the
terminal falls back to sending arrow keys, which walks shell history and prints
`^[[A`. It also raises tmux's scrollback from its 2000-line default to the
100000 that `devcontainer.json` asks VS Code for, since tmux keeps its own and
would otherwise discard everything past 2000. Mouse selection becomes tmux's
rather than the terminal's, so hold Shift while dragging to select for the
system clipboard.

Because every VS Code terminal attaches to the *same* session, opening a second
terminal tab mirrors the first. Use tmux windows rather than VS Code tabs to
run several things side by side.

What this does and does not survive:

| Event | Survives |
|---|---|
| Closing the VS Code window, quitting VS Code, laptop sleep or shutdown | yes |
| Losing the SSM tunnel or SSO session | yes |
| `make stop` / `make restart` / container recreate | **no**, the tmux server dies with the container |

tmux is installed via `EXTRA_APT_PACKAGES` in `shell.env`, so it is published
to Parameter Store by `make push-secrets` and installed by postCreate. A
container built before that variable was set will not have it.

## Stopping and rebuilding a project container

Pick the smallest operation that does what you need, a full teardown throws
away the checkout in the volume.

| Goal | Do this | Checkout in the volume |
|---|---|---|
| Pause work, free the CPU | `make stop` / `make start` | preserved |
| Container wedged, image is fine | `make restart` | preserved |
| Rebuild the image, keep the checkout | VS Code → **Dev Containers: Rebuild Container** | preserved |
| Start completely fresh | `make rebuild` | **destroyed** |

> The teardown + clone-in-volume path below is the one verified against this
> remote setup. The extension's in-place **Rebuild Container** relies on the
> same helper machinery that misbehaves on the clone-in-volume reattach path
> (see the warning above), so if it fails, fall back to a full teardown rather
> than retrying it.

### First: the volume is the only copy of your work

The clone-in-volume checkout is a working tree independent of your Mac. What
you change there is on neither the Mac nor GitHub until you push it *from
inside the container*, and a rebuild re-clones from `origin`, so anything
unpushed does not come back.

```bash
make check          # exits non-zero if the volume holds uncommitted or unpushed work
```

`make clean` and `make rebuild` run this first and refuse to destroy a dirty
checkout; `make clean FORCE=1` overrides that deliberately. The underlying
query, if you want to run it by hand:

```bash
docker exec -u vscode <container> git -C /workspaces/<project> status --short
```

`-u vscode` is required; as root, git aborts with "detected dubious ownership".

If that prints anything you want to keep, push it before tearing down:

```bash
docker exec -u vscode <container> git -C /workspaces/<project> add -A
docker exec -u vscode <container> git -C /workspaces/<project> commit -m "wip"
docker exec -u vscode <container> git -C /workspaces/<project> push
```

### Identify what the project owns

Each clone-in-volume project owns one container, one image, and three volumes:

| Resource | How it is named |
|---|---|
| container | `<project>-<devcontainerId>` from `runArgs` in `devcontainer.json`, or whatever `make rename` set; carries label `devcontainer.config_file=/workspaces/<project>/.devcontainer/devcontainer.json`, which is shared by every instance of the project |
| image | `vsc-<project>-<hash>-features` |
| workspace volume | `<project>-<branch>-<hash>`, also on label `vsch.local.repository.volume` |
| Claude state volume | `<project>-claude`, from the `mounts` entry in `devcontainer.json` |
| docker-in-docker volume | `dind-var-lib-docker-<devcontainerId>` |

```bash
docker ps -a --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}'
docker inspect <container> --format '{{index .Config.Labels "vsch.local.repository.volume"}}'
docker inspect <container> --format '{{json .Mounts}}'
```

### Full teardown

```bash
make clean
```

It reads the container's own mounts before removing it, so the volumes it
deletes are the ones actually attached, not guessed from a naming convention, then removes the container, those volumes, and the image. `vscode` and
`minikube-config` are excluded as shared (`SHARED_VOLUMES` overrides the list),
and `devcontainer-base:noble` and `vsc-volume-bootstrap` stay cached: they hold
no project state and make the rebuild faster.

The equivalent by hand:

```bash
docker rm -f <container>
docker volume rm <project>-<branch>-<hash> <project>-claude dind-var-lib-docker-<devcontainerId>
docker rmi <image>
docker ps -a && docker volume ls && docker images    # confirm the engine is clean
```

### Build and rebuild

```bash
make build      # create the container
make rebuild    # clean, then build
```

Both block until the container is actually up, through the image build *and*
postCreate, and exit non-zero if either fails, so they are safe to script.
`rebuild` checks every prerequisite before destroying anything, so a missing
tool cannot leave you with neither a container nor a way to create one.

What `build` does:

1. Clones the repo from **origin** into a volume named `<repo>-<branch>` on the
   engine, using the cached base image, then hands the tree to the container's
   uid.
2. Generates an override config, the resolved `devcontainer.json` with
   `workspaceMount` pointed at that volume. The committed file is untouched, so
   local builds still bind-mount normally.
3. Runs `devcontainer up` against it.

It refuses to start, before touching anything, when:

| Condition | Why | Override |
|---|---|---|
| Branch missing from `origin`, or has unpushed commits | the checkout is cloned from origin | `FORCE=1` |
| `.devcontainer` has uncommitted changes | config is read from your machine while the checkout comes from origin, so the container would not contain the config that built it | `FORCE=1` |
| `shell.env` newer than the copy in Parameter Store | postCreate bootstraps it from there, so the container would come up with stale configuration | `SKIP_SECRETS_CHECK=1` |

VS Code → **Dev Containers: Clone Repository in Container Volume…** still works
and produces an equivalent container, `make build` exists because it blocks,
reports an exit code, and can be scripted.

Reconnect on later sessions with **Attach to Running Container…** per the
warning above.

## Instance reference (sandbox `<aws-account-id>`, us-east-1)

| Item | Value |
|---|---|
| Instance | `<ec2-instance-id>` (c8g.4xlarge, Ubuntu 24.04 arm64) |
| VPC / subnet | `<vpc-id>` / `<subnet-id>` (public, dedicated) |
| Security group | `<security-group-id>` (no inbound) |
| IAM role | `general-dev-remote-docker` (SSM core + read `/devcontainer/*` params) |
| Storage | 100GB gp3 root + 1TB gp3 (6000 IOPS / 500 MB/s) ext4 `-i 8192` at `/var/lib/docker` |
| Protections | API termination + stop protection enabled; IMDSv2 required, hop limit 2 |

To stop the instance (cost saving), first disable stop protection:
`aws ec2 modify-instance-attribute --instance-id <ec2-instance-id> --no-disable-api-stop --region us-east-1`

## When something fails

Failures from `docker`, `aws` and the `devcontainer` CLI are translated before
they reach you. Each one names the cause and the command that fixes it, so a
message like `conflict: unable to delete ... (must be forced)` arrives as an
explanation of which other container still holds the image and what to do about
it. The wrappers are `rd_docker`, `rd_aws` and `rd_devcontainer_up` in
`lib.sh`; anything with no translation yet still prints the tool's own message,
labelled as untranslated, rather than a bare exit code.

Two rules keep it honest, and both matter when editing these scripts:

- Only calls where a non-zero status is an *error* go through the wrappers.
  Where it is an *answer*, such as "does this volume exist" or "is this branch
  ahead of its upstream", the command is called directly and the caller reads
  the status. `rdc_exec` and `rdc_exec_probe` in `container.sh` are that
  distinction made explicit.
- On the bash 3.2 that macOS ships, `set -e` does not apply inside a command
  substitution. A helper that fails there ends only its own subshell, and its
  caller carries on with an empty value and reports something else. Anything
  capturing the output of a function that can fail therefore writes
  `x="$(f)" || exit $?` rather than relying on `set -e`.

`devcontainer up` is the exception to capturing anything at all. It renders a
live log to the terminal for minutes at a time, and redirecting either of its
streams changes how it writes: capturing stdout to parse its JSON summary made
the log arrive without carriage returns, so every line started where the
previous one ended. Nothing is lost by leaving both alone, because the CLI
prints that summary to stdout *and* stderr, so it is already the last thing on
screen. Do not redirect either stream here.

## Troubleshooting

- **`bind source path does not exist: /Users/<you>/…` when starting a
  devcontainer:** you used **Reopen in Container** on a local folder while the
  remote context was active. Bind mounts are resolved by the *daemon*, so the
  EC2 engine looked for your Mac path and did not find it. There is nothing to
  fix in `.devcontainer/`, pick the workflow that matches the context:
  - Remote engine → **Attach to Running Container…** (or clone-in-volume for
    the first create of a project). Never Reopen in Container.
  - Local engine → `gdev-disconnect` first, then Reopen in Container.

  The two working trees are independent: the local folder and the
  `<project>-<branch>-<hash>` volume are separate checkouts that can hold
  different uncommitted changes. Check both before assuming work is lost, `docker exec -u vscode <container> git -C /workspaces/<project> status`.
- **`docker version` hangs / SSH fails:** AWS SSO session expired, run
  `aws sso login --profile default`. Then re-run `docker-tunnel.sh`.
- **Instance not Online in SSM:** check instance state and
  `aws ssm describe-instance-information` in us-east-1.
- **postCreate fails with "shell.env not found … SSM bootstrap failed":**
  `push-secrets.sh` wasn't run for this project, or the parameter prefix
  differs, the wrapper uses `/devcontainer/<workspace-basename>` by default
  (override with `DEVCONTAINER_SSM_PREFIX`).
- **Back to local development:** `make disconnect`, switches to the
  `LOCAL_DOCKER_CONTEXT` from `config.env` (`orbstack`). The `default` context
  points at Docker Desktop's socket, which is not what runs on this laptop.
  Nothing about the remote engine depends on the local one; the local context
  only matters when you want to build or run containers on the Mac itself.
