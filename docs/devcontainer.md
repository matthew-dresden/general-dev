# Devcontainer internals

How this workspace's container is defined, provisioned, and supplied with
secrets, in both local and remote mode. Remote-engine operations (tunnel,
EC2 reference, troubleshooting) live in
[`.devcontainer/remote-docker/README.md`](../.devcontainer/remote-docker/README.md).

## Definition

`.devcontainer/devcontainer.json`:

- Built from `.devcontainer/Dockerfile`, user `vscode`, workspace at
  `/workspaces/${localWorkspaceFolderBasename}`. The Dockerfile is
  `FROM mcr.microsoft.com/devcontainers/base:noble` plus one layer: it creates
  `~/.vscode-server` and the directories the server volumes mount over, owned by
  `vscode`. Docker creates a missing mount point, and a named volume that is
  empty, root-owned; VS Code installs its server as `vscode` before any
  lifecycle hook has run, so nothing inside the container can hand those
  directories over in time. In local mode that failure was fatal: the extension
  runs `devcontainer up --skip-post-create`, then installs the server itself and
  stops at `ln: failed to create symbolic link
  '/home/vscode/.vscode-server/bin/<build>': Permission denied`. Ownership set in
  the image is what a fresh volume is initialized with, so the mount points are
  writable from the moment the container starts. Remote mode never hit it,
  because `make build` runs the whole of `devcontainer up`, postCreate included,
  before it seeds the server.
- Features: aws-cli, Python 3.14, Node 25, kubectl + helm (minikube disabled
  via `"minikube": "none"`), common-utils, docker-in-docker, uv.
- Features contribute VS Code extensions of their own, merged with the
  `customizations.vscode.extensions` list: the container image's
  `devcontainer.metadata` label records which feature added which. The aws-cli
  feature contributes `AmazonWebServices.aws-toolkit-vscode`, which nothing here
  uses: nobody signs into it, so its explorer sat on a Sign in button, and it
  shows a telemetry notice on first activation regardless of the `aws.telemetry`
  setting, on every rebuild anew because the acknowledgment lives in `data/`,
  which is deliberately not persisted. The `-`-prefixed entry in the extensions
  list is the spec's opt-out and keeps the feature (the CLI is used) while
  dropping its extension. `aws.telemetry: false` stays in the settings so a
  manual install of the toolkit still collects nothing.
- `devcontainer-lock.json` is not tracked. The CLI writes it when it resolves a
  feature it has no entry for, so a tracked copy left `.devcontainer` dirty after
  the first build with a new feature, and the build guard then refused the next
  build: it clones from origin, where a locally written lock would not be. The
  file is an output, not an input; builds resolve features from the tags in
  `devcontainer.json` without it. Feature versions are pinned there instead.
- uv is a feature because `make lint` runs every linter through `uvx`, so without
  it the target fails at the tool rather than on findings, and nothing else
  installed it: not a feature, not `project-setup.sh`. The feature installs the
  release tarball from `astral-sh/uv` directly, and both `uv` and `uvx` land in
  `/usr/local/bin` for the container user.
- `"shutdownAction": "none"`, the container is NOT stopped when the VS Code
  window closes or the connection drops. Applies in both modes; on the remote
  engine this is what makes sessions survive laptop shutdowns.
- `runArgs` names the container `<repo>-${devcontainerId}` and stamps it with
  `devcontainer.project=<repo>`. The label is what the Makefile targets match
  on, so containers created by `make build` and by VS Code's Clone Repository
  in Container Volume are discovered identically. `${devcontainerId}` is unique
  per instance, so several clones of one repo coexist; the generated name is
  long, and `make rename` replaces it with something readable. Local `make
  build` also passes `devcontainer.local_folder` and `devcontainer.config_file`
  as id-labels: any `--id-label` replaces the CLI's defaults, and those two are
  how VS Code recognizes a folder's container, so without them `make reopen`
  had VS Code build a second, identically-configured container rather than
  attach to the one just built.
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
- Keybindings are the one part of the terminal experience `devcontainer.json`
  cannot carry. `customizations.vscode` takes `settings` and `extensions`, both
  of which the server applies inside the container, but a keybinding is
  resolved by the window on the developer's machine, and VS Code exposes no
  setting that changes what the terminal sends for a chord. Shift+Enter
  therefore reaches the shell as a bare `\r`, indistinguishable from Enter, and
  Claude Code submits instead of inserting a newline. `/terminal-setup` fixes
  exactly this by writing a `workbench.action.terminal.sendSequence` binding
  that sends `ESC CR`, but run from a container terminal it writes
  `~/.config/Code/User/keybindings.json` *in the container*, which nothing
  reads, and reports success. `.devcontainer/vscode-keybindings.json` holds the
  binding and `make keybindings` merges it into the editor's real
  `keybindings.json` on the host, refusing to run if it detects
  `REMOTE_CONTAINERS`, `DEVCONTAINER` or `CODESPACES`. It appends only, keeps
  the pre-existing file as `keybindings.json.pre-devcontainer.bak`, and fails
  rather than overwrite a `shift+enter` binding that already exists with other
  arguments. `VSCODE_USER_DIR` overrides the location for Insiders, Cursor or a
  portable install; `VSCODE_APP_DIRNAME` overrides just the application folder.
  tmux is not involved in the failure: it forwards `ESC CR` to the pane intact
  at every `escape-time`, because the two bytes arrive in one write and the
  timer that would split them never starts.
- `terminal.integrated.persistentSessionReviveProcess` is `never`. VS Code
  restores a persisted terminal by relaunching its executable without the args it
  was started with, so a tab launched as `tmux new-session -A -s main zsh` came
  back as bare `tmux`, which does not attach to anything: it created a fresh
  auto-numbered session. They accumulated one or more per reattach, and the tmux
  server outlives the connection, so they never went away. The evidence was in
  `~/.vscode-server/data/logs/*/ptyhost.log`, where every restore logged
  `args undefined` next to the profile launches that logged full args, and each
  one matched a numbered session to the second. `never` switches off recreating a
  dead process; reconnecting to a live one is unaffected. Nothing is lost, since
  scrollback lives in tmux via `history-limit`, not in VS Code's replay buffer.
  Because it is a setting, it reaches a container only when VS Code seeds
  `data/Machine/settings.json`, which is on a rebuild.
- `.devcontainer/tmux.conf`, installed to `~/.tmux.conf` by postCreate, turns
  the mouse on so the wheel scrolls and clicks select, raises tmux's own
  scrollback from its 2000-line default, lists every session on the status bar,
  and sets `default-shell` so windows open zsh rather than the account's login
  shell. postCreate rewrites that line with the zsh it resolved, because the
  committed file cannot know where zsh was installed.
- `postAttachCommand` runs `resmon-disks.py`, which points the Resource Monitor
  extension at the device behind the workspace. resmon filters by device rather
  than mount point and the device differs per host, so both the mount and its
  device are resolved at run time. Which mount holds the workspace differs per
  engine: remote mounts the checkout volume at `/workspaces`, local bind-mounts
  only the repository below it, leaving `/workspaces` itself on the container's
  overlay. Unset, `RESMON_DISK_MOUNTS` therefore resolves to the shallowest mount
  at or under `/workspaces` — `/workspaces` on one engine, `/workspaces/<repo>`
  on the other — and `RESMON_WORKSPACES` moves where that is searched. Reportable
  means the source names one filesystem, not that it names a device file: the
  extension keeps the filesystems whose source string appears in
  `resmon.disk.drives`, a local bind mount reports a driver name (`mac` for
  virtiofs on macOS) that matches exactly one of them, while `/tmp` is a tmpfs
  reporting `none` or `tmpfs` for several unrelated filesystems at once and would
  show up unlabelled or matching all of them. A source is checked by whether
  every mount sharing it is the same filesystem (`st_dev`), which accepts a
  device Docker bind-mounts repeatedly, as it does for `/etc/hosts`, and fails on
  an ambiguous one rather than writing that entry. It runs on attach rather than
  in postCreate because VS Code writes its settings file after postCreate, and it
  never creates that file: doing so stops VS Code seeding it and leaves the
  container with no profiles or editor settings at all.
- The hook names that script as `$HOME/.local/bin/resmon-disks.py`, not by a
  workspace-relative path, and `configure_resmon_disks` in postCreate links it
  there. Only postCreate is run by the devcontainer CLI, which knows
  `workspaceFolder` and runs it there; `postAttachCommand` is run by VS Code, and
  on the remote engine the workspace is a volume with no local path, so the
  window attaches to the container by name. An attached container carries no
  `workspaceFolder` in its metadata for the extension to run hooks in, so the
  hook runs in the home directory: a relative path resolved to
  `~/.devcontainer/resmon-disks.py`, and every attach ended in
  `postAttachCommand … failed with exit code 2` and no disk figures. postCreate
  verifies the link by running it as the container user, so a broken one fails
  the build instead of every attach.
- `mounts` puts a volume, `vscode-server-<repo>`, over
  `/home/vscode/.vscode-server/bin`. VS Code installs its headless server there by
  piping ~200 MiB from the laptop into the container, which crosses the SSM
  tunnel on the remote engine: 252s at 836 kB/s in a measured build. The volume
  outlives the container, so a rebuild finds `bin/<vscode-build>` already
  present, and the extension's check (`test -d`) skips the transfer entirely.
  Per project rather than engine-wide, because two containers sharing one server
  directory would collide on `data/Machine/.devport-<build>` and the connection
  token. `configure_vscode_server_dir` in postCreate aborts the build if that
  path is not a mount point, so a config that stopped mounting it cannot go
  unnoticed, and then proves the container user can write it. It no longer
  chowns anything: the Dockerfile owns that, and a chown running after VS Code
  has already tried to install the server is too late to be the fix. A volume
  that an earlier build left root-owned with content in it keeps that ownership,
  since Docker only initializes an empty one, and the write check is what says
  so. The
  target is a literal path there because no devcontainer variable resolves to
  the remote user's home and `${containerEnv:HOME}` is empty in this image;
  postCreate composes the same path from `/etc/passwd` instead, which is what
  makes the mismatch detectable. `make clean` keeps it, identified by its mount
  point rather than its name, so a teardown does not throw away a cache that
  costs a fetch to rebuild.
- `postAttachCommand` also runs `vscode-settings-sync.py`, ahead of the resmon
  hook and sequentially so the two never write the settings file at once. VS Code
  writes `data/Machine/settings.json` from `customizations.vscode.settings` only
  when that file is absent, so before this an edit to `devcontainer.json` reached
  a container only by creating one. The hook merges the settings that file
  declares into the existing copy on every attach: keys VS Code or a feature put
  there are left alone, and a key removed from `devcontainer.json` is not
  removed from the container, which still needs a rebuild. It never creates the
  file, for the same reason the resmon hook does not. It locates the config by
  the one `\*/.devcontainer/devcontainer.json` under `/workspaces`, since an
  attached container gives a hook no workspace path, and fails when that is not
  exactly one file; `DEVCONTAINER_CONFIG` names it directly.
- `extensionsCache` is on a second volume, `vscode-extensions-cache-<repo>`.
  Without it VS Code copies the extension packages it has cached on the laptop
  into each new container over the docker connection, 23.4s in and 11.2s tarring
  them back in a measured build, both across the tunnel on the remote engine.
  Kept between containers, the packages are already there.
- `data/` and `extensions/` are deliberately not on a volume. VS Code seeds
  `data/Machine/settings.json` from `customizations.vscode.settings` only when
  that file does not exist, so persisting `data/` would freeze the in-container
  settings; `extensions/` is what VS Code installs from the cache, 6s for 19 of
  them in a measured build, so persisting it would only risk a set that no longer
  matches the server build. `DEVCONTAINER_VSCODE_SERVER_VOLUMES` names the
  directories postCreate expects to find mounted.
- `make reopen` seeds that volume before it opens the window, and `make
  vscode-server` does it on demand. The build to fetch is only knowable on the
  laptop, since VS Code updates itself between container builds, so postCreate
  cannot have seeded what the next attach will look for: `code --version` line 2
  is the build, the platform comes from `uname` in the container, and the tarball
  is fetched from `VSCODE_UPDATE_URL` inside the container, off the tunnel. A
  measured cold seed took 31s for download and extraction of 635 MB, against
  252s for VS Code's own transfer. It verifies the `commit` in the delivered
  `product.json` before moving it into place, extracts beside the target so an
  interrupted fetch leaves no half-server where the extension probes, and does
  nothing when the build is already present. An entry that is present but not a
  usable server is removed first, unless a process is running from it: a VS Code
  window attached to a local container it created itself mounts the host's
  server cache at `/vscode` and writes a symlink to it into the volume, which
  dangles in every container built without that mount — invisible to `test -e`,
  which follows links, yet enough to make the final `mv` refuse and fail the
  seed on every attach. `SKIP_VSCODE_SERVER_SEED=1` opens
  the window without it. It then prunes builds nothing needs: VS Code never
  deletes the server it stops using, and each is around 635 MB, so one arrives
  per VS Code update and none leave. A build a process in the container is
  running is kept, since that is the server the current window is talking to, and
  every removal is reported.
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
- That scan only runs at window open. Between scans the extension watches for
  new `.git` directories, but discards any path already inside an open
  repository, and both the workspace root and every clone under it are open
  repositories. `configure_repo_detection` in postCreate defeats that by
  writing `.gitmodules` files: `repos/` in the workspace root, plus one entry
  per nested repository in whichever repository encloses it. A path declared as
  a submodule path is the one case the extension's lookup skips, so the clone
  is left unclaimed and opened on its own. No gitlink is written, so every
  `git submodule` command stays a no-op, and the files are generated rather
  than committed so a rebuild recreates them. `DEVCONTAINER_REPOS_DIR` and
  `DEVCONTAINER_REPO_SCAN_IGNORE` control the directory and the folders the
  walk prunes.

## Provisioning flow (postCreate)

`postCreateCommand` → `postcreate-wrapper.sh` → `.devcontainer.postcreate.sh`:

1. **Wrapper: secrets.** If `shell.env` is missing, bootstrap from SSM
   Parameter Store (see below); otherwise source the local file. Configures
   apt proxy when `HTTP_PROXY` is set. Fails fast on any missing input.
2. **Postcreate: configuration.** Installs nothing, everything installable is
   a feature. Each step is a function in `.devcontainer.postcreate.sh`, states
   the dependency it needs, and is skipped with a banner when that dependency
   is absent rather than aborting the build. The resmon link is the exception:
   `postAttachCommand` runs it unconditionally, so a container that cannot
   provide it fails on every attach, and the build stops instead of shipping
   one:

   | Step | Depends on |
   |---|---|
   | apt proxy config (root-only, for later manual `apt` use) | `HTTP_PROXY` set |
   | every npm prefix holding a global CLI handed to the container user | the node feature |
   | `~/.vscode-server` handed to the container user | the `mounts` volume, required |
   | `shell.env` sourcing into `.bashrc` / `.zshenv` |, |
   | `ccd` / `ccdr` aliases | `claude-code` feature |
   | `claude-settings.json` merged into `~/.claude/settings.json` | `claude-code` feature + `jq` |
   | `tm-*` commands sourced into both shells | `tmux` |
   | `resmon-disks.py` linked into `~/.local/bin` for postAttach | `python3`, required |
   | `vscode-settings-sync.py` linked into `~/.local/bin` for postAttach | `python3`, required |
   | Oh My Zsh theme and options | `common-utils` `installOhMyZsh` |
   | `~/.aws/config` from `aws-profile-map.json` | `jq` + a non-empty map |
   | host proxy reachability | `HOST_PROXY=true` |
   | git identity and credential helper | `git` |
   | `.gitmodules` per repository, for live repo detection | `git` |

   It then hands `$HOME` back to the container user and runs
   `project-setup.sh` as that user.

   The npm handover is what lets a globally installed CLI update itself.
   A feature installs one as root at image build time, so the package directory
   and the scope directory above it (`lib/node_modules/@anthropic-ai` for Claude
   Code) are left owned by root and not group-writable, while
   `lib/node_modules` itself is group-writable from the node feature. npm
   updates a package by replacing its directory, which needs write permission on
   the directory holding it, so the update fails with `no write permission to
   npm prefix` even though the prefix looks writable. Two things follow from
   that:
   - The prefixes handed over are not only the one `npm prefix -g` reports.
     nvm keeps a prefix per node version, and a CLI can sit under a version that
     is not the active one, so each CLI named in
     `DEVCONTAINER_NPM_MANAGED_CLIS` (default `claude`) is resolved from its own
     executable and its prefix added.
   - The check is a real `mkdir` and `rmdir` as the container user in every
     directory npm writes into: `lib/node_modules`, each scope directory inside
     it, and `bin`, where the executable symlink is replaced. `test -w` on
     `lib/node_modules` passes while the scope directories inside it are still
     root-owned, which reported a successful build and left the update broken
     until someone ran `claude update` by hand.

   `.devcontainer/claude-settings.json` is the desired state for Claude Code's
   own `~/.claude/settings.json`, merged in rather than written over so
   anything the CLI has already stored there survives. `$HOME` is not a volume,
   so a rebuild starts from an empty `~/.claude` and the merge is what makes
   the choice repeatable. It currently sets `tui` to `default`, the classic
   main-screen renderer. The value matters less than the key being present:
   Claude Code offers the flicker-free fullscreen renderer on startup only
   while `tui` is unset, so declaring it both picks the renderer and retires
   the prompt and the tip that advertises it. `/tui fullscreen` still switches
   the running container, and postCreate puts it back on the next rebuild.

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

## Git hooks

`make hooks-install` installs a pre-commit and a pre-push hook, both written
by `devcontainer_config.githooks`, so hook content has exactly one source
instead of being duplicated between the two `.git/hooks/*` files.

- **pre-commit** execs `make hooks-run`, which is `make lint`: private files
  untracked, no nested repos, JSON parses, shellcheck, markdown, US English
  spelling, and a secrets scan of whatever is currently staged.
- **pre-push** execs `make hooks-run-push`, which runs `lint` first and then
  scans every commit in the pushed range, not just the tip. Git hands the
  hook one `<local ref> <local sha> <remote ref> <remote sha>` line per ref
  being pushed on stdin; `devcontainer_config.githooks.ranges_from_push_refs`
  turns that into one `<a>..<b>` range per ref (an all-zero remote id, a
  branch the remote has never seen, becomes every commit reachable from the
  local tip that is not already reachable from a remote-tracking ref this
  checkout knows about, so a branch forked from `main` scans only its own new
  commits, not `main`'s history too; an all-zero local id, a delete,
  contributes nothing to scan), and each range is scanned with the same
  detectors staged mode uses. The whole range is scanned, not just the tip,
  because a credential introduced early and removed later still reaches the
  remote in history the moment the commit that added it is pushed; scanning
  only the tip would miss it.

Installing is idempotent: a second `make hooks-install` leaves the hooks
byte-identical, and it refuses to overwrite a hook it did not write, in case
a developer already has their own pre-commit or pre-push hook. To check for
drift, whether an installed hook still matches what `make hooks-install`
would write, without rewriting it, run:

```sh
PYTHONPATH=.claude/plugins/devcontainer/scripts python3 -m devcontainer_config.cli hooks-check
```

`make hooks-uninstall` removes both hooks.

## Creating the container (remote)

`make build` does what VS Code's Clone Repository in Container Volume does, but
blocking and scriptable:

1. Clone the repo from **origin** into a named volume (`<repo>-<branch>`) on
   the engine, using the cached base image, then chown it to the container uid.
2. Generate an override config: the resolved `devcontainer.json` with
   `workspaceMount` pointed at that volume. The committed file is untouched, so
   local builds still bind-mount normally. It is written to a temp file, and the
   CLI resolves a config's relative paths against the config it was handed, so
   `build.dockerfile` and `build.context` are rewritten absolute against
   `.devcontainer` on the way through. Without that the build would look for the
   Dockerfile next to the temp file.
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
