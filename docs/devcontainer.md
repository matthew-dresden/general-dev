# Devcontainer internals

How this workspace's container is defined, provisioned, and supplied with
secrets, in both local and remote mode. Remote-engine operations (tunnel,
EC2 reference, troubleshooting) live in
[`.devcontainer/remote-docker/README.md`](../.devcontainer/remote-docker/README.md).

## Definition

`.devcontainer/devcontainer.json`:

- Prebuilt multi-arch image `caylent-solutions/devcontainer-base:noble`
  (no Dockerfile), user `vscode`, workspace at
  `/workspaces/${localWorkspaceFolderBasename}`.
- Features: aws-cli, Python 3.14, Node 25, kubectl + helm (minikube disabled
  via `"minikube": "none"`), common-utils, docker-in-docker.
- `"shutdownAction": "none"`, the container is NOT stopped when the VS Code
  window closes or the connection drops. Applies in both modes; on the remote
  engine this is what makes sessions survive laptop shutdowns.
- `runArgs` names the container `<repo>-${devcontainerId}` and stamps it with
  `devcontainer.project=<repo>`. The label is what the Makefile targets match
  on, so containers created by `make build` and by VS Code's Clone Repository
  in Container Volume are discovered identically. `${devcontainerId}` is unique
  per instance, so several clones of one repo coexist; the generated name is
  long, and `make rename` replaces it with something readable.
- Terminals default to a `tmux` profile that attaches to a shared session
  (`terminal.integrated.profiles.linux`). VS Code terminates terminal processes
  when the window closes, so this is what keeps a Claude session or long build
  alive across a disconnect, terminal persistence alone only restores the tab
  and its scrollback. The `tm-session-*` and `tm-window-*` commands that drive
  it are defined in `.devcontainer/tmux-commands.sh` and sourced by postCreate;
  `tm-help` lists them, and `tm-` plus Tab discovers them. `tmux: pick session`
  is a second profile that runs `tm-session-pick`, which lists the sessions
  that exist and opens the one you name: a VS Code profile is fixed
  configuration written when the container is built and cannot enumerate
  sessions created later. A plain non-persistent shell is the `zsh` profile.
- `.devcontainer/tmux.conf`, installed to `~/.tmux.conf` by postCreate, turns
  the mouse on so the wheel scrolls and clicks select, raises tmux's own
  scrollback from its 2000-line default, lists every session on the status bar,
  and sets `default-shell` so windows open zsh rather than the account's login
  shell. postCreate rewrites that line with the zsh it resolved, because the
  committed file cannot know where zsh was installed.
- `postAttachCommand` runs `.devcontainer/resmon-disks.py`, which points the
  Resource Monitor extension at the devices behind `/workspaces` and `/tmp`.
  resmon filters by device rather than mount point and the device differs per
  host, so it is resolved at run time. It runs on attach rather than in
  postCreate because VS Code writes its settings file after postCreate, and it
  never creates that file: doing so stops VS Code seeding it and leaves the
  container with no profiles or editor settings at all.
- Git repo detection: `git.autoRepositoryDetection: "subFolders"` +
  `git.repositoryScanMaxDepth: -1` + `git.openRepositoryInParentFolders:
  "never"`, every nested repo under the workspace root is detected at any
  depth, and nothing outside the workspace is ever adopted. Gitignored clones
  are found too: the extension's traversal filters on folder name, excluding
  `.git` and `git.repositoryScanIgnoredFolders`, and never reads `.gitignore`.
  Nothing needs listing in `git.scanRepositories`, which is why that setting is
  no longer used. Clone into `repos/`, or anywhere else under the workspace.
  Workspace settings override the devcontainer defaults, so both files carry
  the same values.

## Provisioning flow (postCreate)

`postCreateCommand` → `postcreate-wrapper.sh` → `.devcontainer.postcreate.sh`:

1. **Wrapper: secrets.** If `shell.env` is missing, bootstrap from SSM
   Parameter Store (see below); otherwise source the local file. Configures
   apt proxy when `HTTP_PROXY` is set. Fails fast on any missing input.
2. **Postcreate: configuration.** Installs nothing, everything installable is
   a feature. Each step is a function in `.devcontainer.postcreate.sh`, states
   the dependency it needs, and is skipped with a banner when that dependency
   is absent rather than aborting the build:

   | Step | Depends on |
   |---|---|
   | apt proxy config (root-only, for later manual `apt` use) | `HTTP_PROXY` set |
   | `shell.env` sourcing into `.bashrc` / `.zshenv` |, |
   | `ccd` / `ccdr` aliases | `claude-code` feature |
   | `tm-*` commands sourced into both shells | `tmux` |
   | Oh My Zsh theme and options | `common-utils` `installOhMyZsh` |
   | `~/.aws/config` from `aws-profile-map.json` | `jq` + a non-empty map |
   | host proxy reachability | `HOST_PROXY=true` |
   | git identity and credential helper | `git` |

   It then hands `$HOME` back to the container user and runs
   `project-setup.sh` as that user.

## Git credentials

The container holds no credential of its own until one is pushed to it.
postCreate only sets `credential.helper store` and the SSH→HTTPS URL rewrite;
`make push-git-creds`, which `make build` runs as its last step, copies in
the credential that already works on the developer's machine, obtained through
`git credential fill` so it works with any configured helper (osxkeychain,
libsecret, gh, store).

This replaced `GIT_AUTH_METHOD` / `GIT_TOKEN` / ssh-key handling driven by
`shell.env`, which had two problems: the token had to be rotated by hand, and
`configure_git_token` wrote `~/.netrc` while configuring the `store` helper,
which reads only `~/.git-credentials`, so it authenticated nothing even when
the token was current. Neither failure was visible while VS Code was attached,
because the extension forwards the host's credentials; it surfaced only when an
agent tried to push from a detached session.

## Creating the container (remote)

`make build` does what VS Code's Clone Repository in Container Volume does, but
blocking and scriptable:

1. Clone the repo from **origin** into a named volume (`<repo>-<branch>`) on
   the engine, using the cached base image, then chown it to the container uid.
2. Generate an override config: the resolved `devcontainer.json` with
   `workspaceMount` pointed at that volume. The committed file is untouched, so
   local builds still bind-mount normally.
3. `devcontainer up --override-config …`, which blocks through the image build
   and postCreate and propagates its exit code.

The config is read from the laptop while the checkout comes from origin, so the
build refuses to run when `.devcontainer` has uncommitted changes, otherwise
the container would not contain the config that built it. `FORCE=1` overrides.

asdf was removed entirely (it managed zero tools; Python/Node come from
features). If a future project needs asdf, that support must be reintroduced
deliberately, nothing references it anymore.

## Secrets model

```text
cdevcontainer setup-devcontainer          push-secrets.sh                postCreate (remote)
        │                                       │                              │
        ▼                                       ▼                              ▼
shell.env (local, gitignored)  ──transform──►  SSM Parameter Store  ──fetch──  shell.env in the
aws-profile-map.json                           /devcontainer/<project>/…       container volume
```

- Local mode: files exist on disk (generated by `cdevcontainer`); the SSM path
  is never attempted.
- Remote mode: a fresh clone-in-volume has no gitignored files. The wrapper
  detects EC2 instance credentials via IMDSv2 (instance launched with
  hop-limit 2 so containers can reach them) and fetches
  `${DEVCONTAINER_SSM_PREFIX:-/devcontainer/<workspace-basename>}/shell.env`
  (SecureString) and `…/aws-profile-map.json` (String), written with 0600 /
  default perms. Any failure is fatal with a message naming the remedy.
- The pushed `shell.env` is the local one transformed for remote: proxy
  disabled and removed (no laptop tinyproxy on EC2), `BASH_ENV` pointed at the
  remote workspace path, stale PATH prepends dropped.
- The instance role can only **read** `/devcontainer/*` parameters; writes
  happen from the laptop with SSO credentials.

## cdevcontainer contract

- `.devcontainer/` contents began as a snapshot copied from the tool's catalog
  (`caylent-solutions/devcontainer@2.3.0`) and have since diverged
  substantially, see the README caveats. The tool only overwrites them if you
  explicitly choose "replace", which would clobber those modifications.
- `shell.env` + `devcontainer-environment-variables.json` are regenerated on
  every `setup-devcontainer` run from your saved template
  (`~/.devcontainer-templates/`). After regenerating, re-run
  `push-secrets.sh` so the remote copies match.
- The tool appends the four secret-file entries to `.gitignore` if missing
  (already present here).
