# Devcontainer internals

How this workspace's container is defined, provisioned, and supplied with
secrets, in both local and remote mode. Remote-engine operations (tunnel,
EC2 reference, troubleshooting) live in
[`.devcontainer/remote-docker/README.md`](../.devcontainer/remote-docker/README.md).

## Definition

`.devcontainer/devcontainer.json`:

- Built from `.devcontainer/Dockerfile`, user `vscode`, workspace at
  `/workspaces/${localWorkspaceFolderBasename}`. The Dockerfile declares a
  `# syntax=docker/dockerfile:1` directive (required for the heredoc `RUN`
  form used below) and is `FROM mcr.microsoft.com/devcontainers/base:noble`
  plus two layers. The first creates `~/.vscode-server` and the directories
  the server volumes mount over, owned by `vscode`. Docker creates a missing
  mount point, and a named volume that is empty, root-owned; VS Code installs
  its server as `vscode` before any lifecycle hook has run, so nothing inside
  the container can hand those directories over in time. In local mode that
  failure was fatal: the extension runs `devcontainer up --skip-post-create`,
  then installs the server itself and stops at `ln: failed to create symbolic
  link '/home/vscode/.vscode-server/bin/<build>': Permission denied`.
  Ownership set in the image is what a fresh volume is initialized with, so
  the mount points are writable from the moment the container starts. Remote
  mode never hit it, because `make build` runs the whole of `devcontainer up`,
  postCreate included, before it seeds the server. The second layer installs
  the AWS session-manager-plugin (see the dedicated bullet below).
- Features: aws-cli, Python 3.14, Node 25, kubectl + helm (minikube disabled
  via `"minikube": "none"`), common-utils, docker-in-docker, uv,
  `ghcr.io/devcontainers/features/terraform:1` (Terraform, Terragrunt and
  tflint, each pinned `latest`); `jq` joins the existing apt-packages list.
- The AWS `session-manager-plugin` is installed by the Dockerfile's second
  `RUN` layer rather than by a devcontainer Feature: no registry Feature for
  it exists. The spec originally named
  `ghcr.io/devcontainers-extra/features/aws-ssm-session-manager-plugin:1` as
  the mechanism, but that Feature has never existed in any registry, and the
  only two personal repositories that come close are either unresolvable on
  `ghcr` or unmaintained since 2024-06-27, so `CLAUDE.md`'s dependency-trust
  rule rules them out. The step fetches the package over TLS from AWS's own
  S3-hosted download path and
  verifies its detached signature with `gpg --batch --verify` before
  `dpkg --install` ever runs, failing the build on any verification error.
  The trust anchor is AWS's publisher signing key, embedded in the Dockerfile
  and pinned by fingerprint `7959637124CE093AD501D47A2C4D4AFF6F6757EE`
  (`devcontainer_config.plugin_install` carries the same pinned values so a
  test can assert the Dockerfile has not drifted from them). No package
  checksum is pinned: the download always targets AWS's moving `latest` path,
  so a checksum pinned against today's build would break every future rebuild
  the moment AWS publishes a new plugin version at that same URL; the
  signing key's fingerprint is stable across releases, so verifying the
  signature against it is what gives the install provenance without freezing
  the plugin version. The plugin runs at build time, not `postCreateCommand`,
  so it ships inside the immutable image; `devcontainer_config/transport.py`
  shells out to it by relying on it being on `PATH` for the non-root
  `vscode` user (work unit `E5-F3-S1-T2`, operator ruling signed at commit
  `9a38c703adda210e27c2c34607cb1a09a4d63378`).
- The IaC engine is Terraform, per D14, spec `devcontainer-platform.md`
  Section 13, licensed BUSL-1.1; the installed Terraform and Terragrunt
  versions satisfy the spec Section 6 floors, `>= 1.10` and `>= 1.1.3`
  respectively.
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
   is absent rather than aborting the build, except for every step the table
   below marks `required`: the VS Code server volumes handed to the container
   user, because a path that is not a mount point refills from empty on every
   rebuild and a volume an earlier build left root-owned cannot be written by
   the container user; the resmon-disks postAttach link, because
   `postAttachCommand` runs it unconditionally and a container that cannot
   provide it fails on every attach; the vscode-settings-sync postAttach
   link, for the same postAttach reason; the git hooks step, because a
   container whose hooks did not install would run `git commit` with no
   secret scan; and the devsecret export-list block render, because a
   container whose shells silently lack their exported secrets is worse
   than a container that failed to create. Each of those aborts the build
   through `exit_with_error` instead of shipping a container missing the
   guarantee:

   | Step | Depends on |
   |---|---|
   | apt proxy config (root-only, for later manual `apt` use) | `HTTP_PROXY` set |
   | every npm prefix holding a global CLI handed to the container user | the node feature |
   | `~/.vscode-server` handed to the container user | the `mounts` volume, required |
   | `shell.env` sourcing into `.bashrc` / `.zshenv`, plus the devsecret export-list startup block appended to both | `python3` + `devcontainer_config` on `PYTHONPATH`, required |
   | `ccd` / `ccdr` aliases | `claude-code` feature |
   | `claude-settings.json` merged into `~/.claude/settings.json` | `claude-code` feature + `jq` |
   | `tm-*` commands sourced into both shells | `tmux` |
   | `resmon-disks.py` linked into `~/.local/bin` for postAttach | `python3`, required |
   | `vscode-settings-sync.py` linked into `~/.local/bin` for postAttach | `python3`, required |
   | Oh My Zsh theme and options | `common-utils` `installOhMyZsh` |
   | `~/.aws/config` from `aws-profile-map.json` | `jq` + a non-empty map |
   | host proxy reachability | `HOST_PROXY=true` |
   | git identity and credential helper | `git` |
   | pre-commit and pre-push hooks, via `make hooks-install` | the workspace `.git` directory, required |
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

`make hooks-uninstall` removes both hooks; running that command (or any of
the other patterns the Bypass denial section below lists) through Claude
Code's `Bash` tool is itself a denied bypass.

On the remote engine the workspace is cloned into a volume, so the container
has its own `.git` directory, one `make hooks-install` on the host never
touches. Without a second install, a commit made from a container terminal
-- where most work in this repository happens -- would run no secret scan at
all, the unguarded path becoming the common one instead of the exception.
postCreate closes that gap: after the workspace and its `.git` directory are
in place, it runs the identical `make hooks-install` the host uses, so the
container renders the same hook content from the same
`devcontainer_config.githooks` module rather than a second copy. A failed
install aborts postCreate rather than continuing with no hooks in place: a
container that reached the prompt with no guard would run `git commit` with
no secret scan, at the moment a developer is least likely to be reading the
setup log.

## Bypass denial

Section 3.6.2 ("Boundaries, and what each defends") of
`repos/spec/devcontainer-platform.md` names the threat this control
answers: not a secret reaching the repository, but an agent or a human
disabling the controls above under time pressure. Section 4.6.1 ("Bypass
denial") of the same document fixes the denied surface. A `PreToolUse`
hook on the `Bash` matcher, `.claude/hooks/deny_bypass.py`, registered in
`.claude/settings.json`, denies every structurally recognized attempt made
through Claude Code's `Bash` tool to run one of the following, naming the
rule and the fix instead of only refusing. This hook intercepts only
commands issued through that tool; a command a human (or an agent working
outside Claude Code) types directly into a host or container terminal is
not intercepted here at all, and stays covered, if at all, only by the git
hooks the previous section describes:

- `git commit --no-verify`, its `-n` alias (bare or bundled with another
  short option, for example `-vn`), and `git commit --no-gpg-sign`.
- `git push --no-verify`.
- Any `HUSKY=`, `SKIP=` or `PRE_COMMIT_ALLOW_NO_CONFIG=` assignment,
  whatever the value, whether written inline before the program or behind
  `env`, wherever in a chained command they appear.
- `git add -f` / `--force` of a path `.gitignore` excludes.
- `rm` or `chmod` targeting anything under `.git/hooks`.
- `make hooks-uninstall`, including the `make -C <dir> hooks-uninstall`
  spelling.

Evaluation is structural: a command line chained with `&&`, `||`, `;`, a
pipe, a bare `&` background operator, simply a newline (the ordinary shape
of a multi-line Bash-tool command), or a statement wrapped in a subshell
`( )` or a brace group `{ }`, is denied when any segment matches, not only
the first, and a denial is decided from the tokenized program, arguments
and leading assignments, never a substring search over the raw text. The
program name is compared by its basename, so `/usr/bin/git commit
--no-verify` and `/bin/rm .git/hooks/pre-commit` are denied the same as
the unprefixed spellings. A bare-word launcher ahead of the program
(`sudo`, `env`, `command`, `nohup`, `time`, `doas`, `nice`, `ionice`,
`setsid`, any number of them chained) and a shell reserved word that would
otherwise be misread as the program after a `;` inside a compound
statement (`then`, `else`, `elif`, `do`, `done`, `fi`, `while`, `until`,
`case`, `esac`, `in`, `function`, `if`, `!`) are both walked past the same
way, so `sudo git commit --no-verify` and `if true; then git commit
--no-verify; fi` deny exactly as the unprefixed, unwrapped spelling does.

Known limitations, so this section does not claim broader coverage than
the hook delivers: a denied command nested inside a quoted string this
hook cannot re-parse -- `bash -c "<command>"`, `sh -c "<command>"` and
`eval "<command>"` -- is not recovered from that string; a `git -c
core.hooksPath=<path>` global config assignment is walked past without
its value being inspected; a launcher combined with its own flags (`sudo
-u root`, `env -i`, `nice -n 10`) is not walked past, only the bare word
is; and a wrapper that requires a positional argument before the command
it runs (`timeout <n>`, `stdbuf <opts>`, `xargs`) is not treated as a
launcher at all. Each of these is documented here, in the module
docstring, as a hardening candidate for a follow-up unit, not asserted as
covered.

`git push --dry-run` is permitted: under `push`, `-n` means dry run, a
rehearsal that changes nothing, not the `commit` alias for `--no-verify`.
The same two characters are read in the context of the subcommand they
follow rather than matched blindly, so `git commit -n` is denied while
`git push -n` is not.

The default is deny for anything the hook cannot understand: an
untokenizable command line, an event on stdin that is not valid JSON or
carries no command, or a `git add -f` whose ignore check cannot run, all
deny rather than allow.

A denial that looks wrong is not worked around with a different spelling
of the same bypass. Fix the underlying problem the flag was reaching for
(a genuinely broken hook, a signing key that needs configuring), or
escalate to a human operator, who can evaluate whether the denied action
is legitimate; only a human, not this hook and not the agent it stopped,
approves running a command it lists above.

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

## Certificate lifecycle

A remote engine is reached over mTLS: the docker daemon authenticates the
laptop's client certificate, and the laptop authenticates the daemon's server
certificate, both signed by a certificate authority created once per
instance. Three lifetimes govern that material (`CERT_CA_DAYS`,
`CERT_SERVER_DAYS`, `CERT_CLIENT_DAYS`; defaults and validation are
documented in `docs/environment-files.md`'s "Certificate lifetimes"
section, the single place they are defined):

| Material | Default lifetime | Persisted at |
|---|---|---|
| CA (`ca-key.pem`, `ca.pem`) | 3650 days | `~/.docker/certs/<instance>/ca/` |
| Server (key, certificate) | 365 days | Nowhere: generated and published to Parameter Store in one step, never written under `~/.docker/certs/<instance>/` |
| Client (`key.pem`, `cert.pem`) | 90 days | `~/.docker/certs/<instance>/` |

The client certificate's ninety-day lifetime is the one an operator actually
lives with day to day, and it is sustainable only because rotating it is
cheap: `make cert-status` reports a `RENEW` row, naming the exact
`/devcontainer:certs INSTANCE=<name>` invocation that clears it, once a
client certificate is inside its warning window (`CERT_WARN_DAYS`, default
14 days); running that invocation's "Rotate client certificate" operation
(`.claude/plugins/devcontainer/skills/certs/SKILL.md`) issues a fresh
client key and certificate from the same CA and installs them atomically.

**Rotating a client certificate touches nothing on the instance.** The daemon
holds only `ca.pem` and accepts any client certificate that chains to it, so
a replacement signed by the same CA is accepted the moment it is presented:
no Parameter Store entry is written, the daemon is never restarted, no
`terragrunt apply` runs, and the docker context needs no update since it
already points at `cert.pem`/`key.pem` by path. The CA itself is never
reissued by a rotation -- reissuing it would invalidate every certificate it
already signed, including the server certificate the daemon is already
serving, which would take the instance off the air until a new server key is
published and the daemon restarted. This is also why a client rotation is
not a substitute for revoking access: Docker supports neither CRL nor OCSP,
so the superseded client certificate stays cryptographically valid until its
own `notAfter` passes; removing the principal's `ssm:StartSession` grant is
what actually renders a certificate inert, because a certificate
authenticates but IAM authorizes, and a certificate is useless without the
tunnel it authenticates inside.

## Transport

The security group behind a remote instance carries zero ingress rules
(spec Section 5.6): no packet from the network reaches the docker daemon,
only the loopback listener `127.0.0.1:DOCKER_TLS_PORT` on the instance
itself. Reaching that listener from the laptop is two independent factors
stacked on top of each other, each answering a different question (spec
Section 3.6.2), and neither substitutes for the other:

- **The SSM port forward**
  (`.claude/plugins/devcontainer/scripts/devcontainer_config/transport.py`,
  E6-F2-S1-T1) maps the loopback listener to a local port on the laptop with
  `aws ssm start-session --document-name AWS-StartPortForwardingSession` --
  no SSH element anywhere in the argument vector. `ssm:StartSession` scoped
  to that document decides *who may open the tunnel at all*, and, since
  Docker supports neither CRL nor OCSP, revoking it is the only revocation
  mechanism this platform has.
- **mTLS over that tunnel** (`transport.ensure_context`/`transport.handshake`,
  E6-F2-S1-T2) decides *who may command the daemon once the tunnel is
  open*. An SSM grant alone is coarse: without the client certificate,
  anyone whose policy permits `ssm:StartSession` would drive the engine.

Once the forward answers, `transport.ensure_context` creates or updates the
docker context `general-dev-<instance>` (spec Section 9) to address
`tcp://127.0.0.1:<the allocated port>` and carry the `ca`, `cert` and `key`
files spec Section 5.5 fixes under `~/.docker/certs/<instance>/`. An
existing context is updated in place rather than deleted and recreated; an
existing context whose endpoint is `ssh://` -- the legacy transport
`.devcontainer/remote-docker/docker-tunnel.sh` still uses through phase 3 --
is refused by name and left completely untouched, so that path keeps
working unchanged. VS Code Dev Containers needs no transport of its own: it
follows the active docker context, so pointing the context at the tunnel
points the editor at it too.

`transport.handshake` then completes a `docker version` handshake against
that context, retried until it answers and bounded by
`DOCKER_HANDSHAKE_TIMEOUT` (`docs/environment-files.md`), reporting the
daemon's API version -- which must be at least 1.44 for mTLS on a TCP
listener with a rootless daemon (spec Section 6) -- and whether it reports
running rootless.

**The SAN requirement, and its symptom.** TLS validates the name the
*client* used to dial the daemon, and the client dials `127.0.0.1` through
the loopback forward, so the server certificate must carry `IP:127.0.0.1`
and `DNS:localhost` -- never the instance's own hostname or private
address. A server certificate issued for either of those produces a
handshake failure that names neither the forward nor the SANs: a real
example, captured against a deliberately mis-issued certificate, reads
`tls: failed to verify certificate: x509: certificate is valid for
10.0.0.5, not 127.0.0.1` -- prose that reads like a tunnel, security-group
or daemon problem before it reads like a certificate one.
`transport.diagnose_handshake_failure` exists to close that gap: it
recognizes that shape (and the related `x509: cannot validate certificate
for 127.0.0.1 because it doesn't contain any IP SANs`) and states the SAN
requirement, both required values, and the `/devcontainer:certs
INSTANCE=<name>` invocation that reissues the server certificate, instead
of surfacing the bare TLS error. A connection failure -- the forward not
established, the daemon not listening -- is translated into a distinct
diagnosis naming the forward instead, so the two causes are never
conflated.

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

## Plugin and skills

Section 11 of `repos/spec/devcontainer-platform.md` records the Claude Code plugin as
an inbound integration: "Marketplace at `.claude/plugins/devcontainer`", failing
as "A malformed manifest fails skill lint". The plugin lives at
`.claude/plugins/devcontainer/`, a directory holding `.claude-plugin/plugin.json`
(the plugin manifest, `name: devcontainer`) and `.claude-plugin/marketplace.json`
(a marketplace whose single `plugins` entry sources that same directory). Neither
manifest enumerates skills: the skill set is discovered from the plugin's
`skills/` directory, so the manifests never drift from what is actually present
there.

`.claude/settings.json` registers the plugin directory as a `directory`-sourced
marketplace under `extraKnownMarketplaces.devcontainer` and enables the plugin
from it via `enabledPlugins["devcontainer@devcontainer"]`. This is additive to
the `PreToolUse` bypass-denial hook registration the previous section describes;
both keys coexist in the same file.

With the marketplace registered and the plugin enabled, Claude Code resolves
each skill directory under `.claude/plugins/devcontainer/skills/<name>/` as the
slash command `/devcontainer:<name>`. Section 4.2 of
`repos/spec/devcontainer-platform.md` names nine skills that will populate that
directory: `setup-local`, `setup-remote`, `engine`, `launch`, `doctor`,
`secrets`, `certs`, `teardown`, `quality`. Each lands in its own work unit as a
Markdown-only change, because this plugin container already exists;
`tests/test_skill_lint.py` is the skill lint suite that checks it.

The table below is the skill roster: one row per skill directory under
`.claude/plugins/devcontainer/skills/`, added by that skill's own work unit.
`tests/test_skill_lint.py`'s `check_plugin` asserts the row set and the
directory set are equal, so the table cannot drift from what is actually
installed, and enforces the rest of the plugin's structural contract in the
same run:

- `plugin.json`'s `name` equals the plugin directory name, and
  `marketplace.json` has exactly one `plugins` entry whose `source` resolves
  to that same directory.
- `.claude/settings.json` registers the plugin directory as a `directory`
  marketplace and enables it in `enabledPlugins`.
- Every `SKILL.md` opens with a `---`-delimited frontmatter block holding a
  non-empty `description` and a `name` matching its directory.
- Every skill that declares `Interview backend: <backend>` restricts
  `<backend>` to one of `answers.BACKENDS`, and gives a `## Questions` table
  with header `| Field | Prompt |` whose `Field` column, as a set, equals
  `answers.required_fields({"backend": <backend>, "aws_config_enabled": True,
  "host_proxy": True})` -- the comparison always runs with the AWS and
  host-proxy branches enabled, regardless of what the skill's own backend or
  interview flow would actually ask, so `aws_profiles` and `host_proxy_url`
  are always expected fields in every skill's table. A required field the
  table omits and a declared field that is not a real `answers` field each
  produce their own finding; a declared field that is a real `answers` field
  but is not required for `<backend>` produces a third finding, distinct
  from the invented-field case.
- Every skill's `## Checks` table, when its header line matches exactly
  `| Check | Prevents | Failure message | Remedy |`, must have a unique
  `Check` name, a unique `Failure message`, and a non-empty `Remedy` in
  every row. A `SKILL.md` with no `## Checks` table, or one whose header is
  misspelled or reordered, is treated as having no such table and produces
  no finding; the `Prevents` column's content is never validated.
- Every `/devcontainer:<name>` reference, in any `SKILL.md` or in this
  document, names a skill present in the roster below.

| Skill | Invocation |
|---|---|
| setup-local | `/devcontainer:setup-local` -- asks the local backend's required answers (Section 5.1), writes and verifies the three private files, checks prerequisites, and ends by naming `make build` |
| setup-remote | `/devcontainer:setup-remote` -- asks the local backend's required answers plus instance name, id, region and profile (Section 5.1), verifies the SSO session and instance state, gates any Terragrunt apply behind PRECHECK-APPLY, issues certificates, creates the docker context and port forward, and ends by naming `make build INSTANCE=<name>` |
| engine | `/devcontainer:engine` -- defines the validation contract the other skills reuse: the thirteen checks of Section 4.2.1 (six local, seven remote), asking which instance only when Section 4.1.1 resolution is ambiguous, fixing only what is reversible and needs no operator credential (selecting an existing context, re-establishing a port forward), and ending in a per-check verdict table |
| launch | `/devcontainer:launch` -- asks nothing, delegates engine reachability (and with it the `rdc_backend` local-against-remote selection) to `/devcontainer:engine`, then resolves the container itself through `rdc_container_ids` and `rdc_require_container` and picks `make build`, `make start`, `make restart` or `make reopen`, verifying by re-reading state after every action; it never destroys anything and ends with a running container |
| doctor | `/devcontainer:doctor` -- asks nothing, delegates the thirteen `/devcontainer:engine` checks by reference rather than restating them, and reports every configuration, secrets, container-state and drift finding engine does not cover, each with an exact remedy; it only reports, it never repairs anything itself |
| secrets | `/devcontainer:secrets` -- asks which secret and which scope, runs add, list, update, rotate, delete, mark exported and move scope entirely through the `devsecret` CLI (never a second path to Parameter Store), never places a value in a command's arguments, never renders a value into the conversation, verifies every write or delete with an independent re-read rather than trusting the CLI's own exit code, and ends by naming the parameter path, its type and the resulting version |
| certs | `/devcontainer:certs` -- asks which instance, creates the CA and issues the server and client certificates on first use, rotates the client certificate with the instance left running, reports expiry (the `make cert-status` view), states that certificate revocation does not exist and that removing the principal's `ssm:StartSession` grant is the mechanism, and ends every material-changing operation by rewriting the docker context and completing a handshake before reporting success |
| teardown | `/devcontainer:teardown` -- asks for confirmation, always, taken against an inventory of what `make clean` or `make rebuild` will destroy (the container, its private volumes, its image) and what will survive (shared volumes, the base image); explains the unpushed-work and uncommitted-config guards rather than only enforcing them, never sets `FORCE` itself, destroys container state only (never an instance, which stays behind `GATE-DESTROY`), and ends by reporting what was destroyed and what survived from a fresh post-operation read, never from the target's own exit code |
| quality | `/devcontainer:quality` -- asks nothing, reads the sub-target set `make validate` invokes from the Makefile itself rather than a copy embedded in the skill, interprets each failing sub-target's root cause and fixes it, never suppresses a finding (no bypass annotation, no linter-ignore entry, no raised threshold, no narrowed `LINT_EXCLUDES` or `SPELL_FILES`), stops and asks for human approval on a suspected false positive, hands anything else it cannot fix to `/devcontainer:doctor` or the operator, and ends by reporting the exit code of a fresh `make validate` run |
