# Agent prompt, create `mdresden` on the EC2 box with auto-discovered workspace symlinks

Use with a Claude Code session running **on the macOS host**:

```sh
cd /Users/mdresden/Workspace/caylent-solutions/general-dev
claude "Read docs/ec2-mdresden-setup-prompt.md and do everything it says."
```

---

## Mission

On the remote EC2 Docker host (`<ec2-instance-id>`, reachable as
`ssh general-dev-remote`, user `ubuntu`, passwordless sudo):

1. Create a login user **`mdresden`** (zsh + oh-my-zsh) that I can SSH into
   from this Mac over the existing SSM tunnel.
2. Give `mdresden` a `~/workspaces` directory that is **automatically and
   continuously populated** with one symlink per devcontainer workspace
   discovered on the box, every workspace that exists now or appears later,
   not just general-dev. Each symlink is named after the **git project**
   cloned in that workspace (e.g. `general-dev`).
3. Ensure `mdresden` has **full read/write access** to those workspace trees,
   without breaking the container user's (uid 1000) access.
4. Wire up the Mac side: SSH host alias + a `gdev-mdresden` zsh alias.

## Context you need

- The Mac's `~/.ssh/config` already has a managed block `Host
  general-dev-remote` (User ubuntu) whose ProxyCommand runs an AWS SSM
  session; reuse the same HostName/ProxyCommand/IdentityFile pattern for the
  new alias. The private key path is in that block (`REMOTE_SSH_KEY_PATH`
  from `.devcontainer/remote-docker/config.env` if you need it).
- VS Code "Clone Repository in Container Volume" creates named docker
  volumes **labeled** `vsch.local.repository=<git clone URL>[/tree/<branch>]`.
  The volume's `Mountpoint` (host path under `/var/lib/docker/volumes/…/_data`)
  contains one folder per workspace, normally named after the repo. That
  label is the discovery mechanism, do NOT hardcode volume names.
- Docker's data root is a dedicated ext4 volume (ACL-capable) at
  `/var/lib/docker`. Never loosen its `0710` permissions with chmod; grant
  traversal with narrow ACL entries only (`u:mdresden:x`).
- Workspace files are owned by uid 1000 (`vscode` in containers, which is
  also `ubuntu` on the host). Access for `mdresden` must come from ACLs, and
  files `mdresden` creates must stay usable by uid 1000 → set **default
  ACLs for both users** on workspace directories.

## Rules

- Idempotent throughout: safe to re-run this entire prompt; managed
  SSH-config/zshrc edits stay inside marker comment blocks; installed files
  are simply overwritten with the same content.
- Fail fast; verify every step's effect before moving on. No `sleep`-based
  waits.
- No secrets in any file in this repo or on the box. Nothing here touches
  git repos' contents.
- Validate sudoers changes with `visudo -c` before and after installing.

## Steps

### A. On the box (via `ssh general-dev-remote`, using sudo)

1. **User**: create `mdresden` (home dir, shell `/usr/bin/zsh`) if absent.
   Add to groups `docker` and `sudo`. Install
   `/etc/sudoers.d/mdresden` containing
   `mdresden ALL=(ALL) NOPASSWD:ALL` (mode 0440, `visudo -c` must pass, the account has no password, so sudo must be NOPASSWD to work at all).
2. **SSH access**: derive the public key from the Mac private key
   (`ssh-keygen -y -f <key>`) and append it to
   `/home/mdresden/.ssh/authorized_keys` (dir 700, file 600, owned by
   mdresden) unless already present.
3. **oh-my-zsh** for mdresden, unattended install, as done for ubuntu:
   `runuser -l mdresden -c 'sh -c "$(curl -fsSL https://raw.githubusercontent.com/ohmyzsh/ohmyzsh/master/tools/install.sh)" "" --unattended'`
4. **Workspace sync script**, install as
   `/usr/local/sbin/sync-devcontainer-workspaces` (root:root, 0755), with
   this exact behavior (write clean bash; adjust syntax only if something
   doesn't run on the box):

   ```bash
   #!/usr/bin/env bash
   # Populates /home/mdresden/workspaces with a symlink per devcontainer
   # workspace volume and grants mdresden ACL access to the workspace trees.
   set -euo pipefail

   TARGET_USER="mdresden"
   TARGET_DIR="/home/${TARGET_USER}/workspaces"
   CONTAINER_UID="1000"

   install -d -o "$TARGET_USER" -g "$TARGET_USER" -m 755 "$TARGET_DIR"
   setfacl -m "u:${TARGET_USER}:x" /var/lib/docker /var/lib/docker/volumes

   declare -A mapped=()
   while IFS= read -r vol; do
     [ -n "$vol" ] || continue
     mnt=$(docker volume inspect -f '{{.Mountpoint}}' "$vol")
     repo=$(docker volume inspect -f '{{index .Labels "vsch.local.repository"}}' "$vol")
     project="${repo%/tree/*}"; project="${project%/}"; project="${project%.git}"; project="${project##*/}"
     [ -n "$project" ] || continue
     wsdir="${mnt}/${project}"
     if [ ! -d "$wsdir" ]; then
       wsdir=$(find "$mnt" -mindepth 1 -maxdepth 1 -type d ! -name '.*' | sort | head -1 || true)
       [ -n "$wsdir" ] && [ -d "$wsdir" ] || continue
     fi
     name="$project"; n=2
     while [ -n "${mapped[$name]:-}" ] && [ "${mapped[$name]}" != "$wsdir" ]; do
       name="${project}-${n}"; n=$((n+1))
     done
     mapped[$name]="$wsdir"
     setfacl -m "u:${TARGET_USER}:x" "$mnt"
     # one-time recursive grant; default ACLs make it stick for new files
     if ! getfacl -p "$wsdir" 2>/dev/null | grep -q "^user:${TARGET_USER}:rwx"; then
       setfacl -R  -m "u:${TARGET_USER}:rwX" -m "u:${CONTAINER_UID}:rwX" "$wsdir"
       setfacl -Rd -m "u:${TARGET_USER}:rwX" -m "u:${CONTAINER_UID}:rwX" "$wsdir"
     fi
     ln -sfn "$wsdir" "${TARGET_DIR}/${name}"
   done < <(docker volume ls -q --filter label=vsch.local.repository)

   # prune symlinks whose volume/workspace disappeared or is no longer mapped
   for link in "$TARGET_DIR"/*; do
     [ -L "$link" ] || continue
     base=$(basename "$link")
     if [ -z "${mapped[$base]:-}" ] || [ ! -e "$link" ]; then
       rm -f "$link"
     fi
   done
   ```

5. **systemd units** so discovery is continuous (new clones appear within a
   minute, removed ones get pruned):
   - `/etc/systemd/system/sync-devcontainer-workspaces.service`, `Type=oneshot`,
     `ExecStart=/usr/local/sbin/sync-devcontainer-workspaces`,
     `After=docker.service`, `Requires=docker.service`.
   - `/etc/systemd/system/sync-devcontainer-workspaces.timer`, `OnBootSec=30s`, `OnUnitActiveSec=60s`, `[Install] WantedBy=timers.target`.
   - `systemctl daemon-reload && systemctl enable --now` the timer, then run
     the service once immediately and check
     `systemctl status` + `journalctl -u sync-devcontainer-workspaces --no-pager -n 20`
     for a clean run.

### B. On the Mac

1. Add a second managed block to `~/.ssh/config` (markers:
   `# >>> general-dev remote-docker mdresden >>>` / `# <<< … <<<`):
   `Host general-dev-mdresden` with the same HostName, ProxyCommand,
   IdentityFile, ConnectTimeout/ServerAlive settings as the existing
   `general-dev-remote` block, but `User mdresden`.
2. In the existing managed zshrc block (`# >>> general-dev remote-docker >>>`),
   add: `alias gdev-mdresden='ssh general-dev-mdresden'` (keep the block's
   other lines; still exactly one block after your edit).

### C. Verify (all must pass)

- `ssh general-dev-mdresden 'whoami && echo $SHELL'` → `mdresden`, zsh.
- `ssh general-dev-mdresden 'ls -l ~/workspaces'` → contains `general-dev`
  symlink pointing into `/var/lib/docker/volumes/…/_data/general-dev`.
- Write access both directions:
  - `ssh general-dev-mdresden 'touch ~/workspaces/general-dev/.acl-test && ls -l ~/workspaces/general-dev/.acl-test'`
  - uid-1000 can still write it:
    `ssh general-dev-remote 'sudo -u ubuntu sh -c "echo ok >> $(readlink -f /home/mdresden/workspaces/general-dev)/.acl-test"'`
  - then delete the test file as mdresden.
- `ssh general-dev-mdresden 'git -C ~/workspaces/general-dev status'` works
  (safe.directory complaints are acceptable to fix via mdresden's global
  gitconfig `safe.directory = *`).
- Idempotency: run the whole of section A again; no errors, no duplicate
  ssh/zshrc blocks, timer still active.

## Done when

All section-C checks pass and `gdev-mdresden` from a fresh Mac shell lands
in mdresden's zsh with a populated `~/workspaces`.
