# Agent prompt. Mac host setup for the general-dev remote Docker engine

Use this with a Claude Code session running **on the macOS host** (a normal
Mac terminal, NOT inside a devcontainer), started in this repo's root folder:

```sh
cd /Users/mdresden/Workspace/caylent-solutions/general-dev
claude "Read docs/mac-setup-prompt.md and do everything it says."
```

---

## Mission

Prepare this Mac to develop against the remote EC2 Docker engine, make the
configuration persistent in `~/.zshrc`, and validate the tunnel end-to-end.
Run this when provisioning a new Mac against an already-running instance.

## Context

- Remote engine: EC2 `<ec2-instance-id>` (c8g.4xlarge, Ubuntu 24.04 arm64),
  us-east-1, AWS sandbox account `<aws-account-id>`, reachable ONLY via SSM (no
  inbound ports). Tunnel scripts live in `.devcontainer/remote-docker/`
  (`docker-tunnel.sh`, `shell.sh`, `push-secrets.sh`, `config.env`, `lib.sh`).
- The EC2 key pair is `<your-key-pair-name>` (ed25519). Its public-key
  fingerprint as registered in AWS: `SHA256:QmT+d5VyqB/ufJprRgMY2GVBHtc/7wOa/NnyrRXM7p0=`.
- OrbStack provides the local docker engine and CLI.

## Rules

- Fail fast; verify every step actually succeeded before moving on.
- Idempotent: safe to re-run this whole prompt. All `~/.zshrc` changes go in
  ONE marker-delimited block (replace the block on re-run, never duplicate):
  `# >>> general-dev remote-docker >>>` … `# <<< general-dev remote-docker <<<`
- Do not modify files inside this repo; do not commit anything; never write
  secrets anywhere.
- If something requires my input (key location, browser SSO login), ask me.

## Steps

1. **Preflight.** Confirm `uname` is `Darwin`, that the repo lives at
   `/Users/mdresden/Workspace/caylent-solutions/general-dev`, and that
   `.devcontainer/remote-docker/docker-tunnel.sh` exists there (fail with a
   clear message otherwise). Confirm Homebrew is installed.

2. **Install tools** (skip anything already present; verify each with a
   version command):
   - AWS CLI v2: `brew install awscli`
   - `brew install --cask session-manager-plugin`
   - docker CLI: should already exist via OrbStack (`docker context ls` must
     list a local context, typically `orbstack`). If missing, stop and ask.
   - `brew install jq`, used by `make build` to generate the override config
   - devcontainer CLI: `npm install -g @devcontainers/cli`, required by
     `make build` / `make rebuild`. Needs node/npm; if absent, stop and ask.
   - python3 and git are expected from the Xcode command line tools; verify
     both respond to a version command.
   - OpenSSL 3.0 or later, required by the `certs` module
     (`.claude/plugins/devcontainer/scripts/devcontainer_config/certs.py`,
     E6-F1-S1-T1) for `openssl x509 -req -copy_extensions`: `brew install
     openssl` installs the `openssl@3` keg. Add `$(brew --prefix
     openssl@3)/bin` to the FRONT of `PATH` in the shell profile rather than
     relying on the Homebrew prefix's own `bin` directory already being
     ahead of `/usr/bin` on `PATH` -- Homebrew does not guarantee `openssl@3`
     is symlinked into that shared prefix on every install, and the system
     `/usr/bin/openssl` on macOS is LibreSSL, which rejects
     `-copy_extensions`. After updating `PATH`, confirm `openssl version`
     reports `OpenSSL 3.x`, not `LibreSSL`; if it still reports `LibreSSL`,
     stop and ask.

3. **AWS SSO profile.** Ensure `~/.aws/config` contains a `[profile default]`
   for the sandbox account. If a conflicting `default` profile exists for a
   DIFFERENT account, stop and ask me. Otherwise create/append:

   ```ini
   [profile default]
   sso_start_url = https://<your-sso-portal>.awsapps.com/start
   sso_region = us-east-1
   sso_account_id = <aws-account-id>
   sso_role_name = AdministratorAccess
   region = us-east-2
   output = json
   ```

   Run `aws sso login --profile default` (I'll complete the browser flow),
   then verify `aws sts get-caller-identity --profile default` returns
   account `<aws-account-id>`.

4. **Locate and verify the SSH private key.** Search `~/.ssh/` for the key
   pair's private key (common names: `<your-key-pair-name>.pem`,
   `id_ed25519`, downloads from the EC2 console). Verify the match with
   `ssh-keygen -lf <candidate>`, the fingerprint must equal
   `SHA256:QmT+d5VyqB/ufJprRgMY2GVBHtc/7wOa/NnyrRXM7p0=`.
   If no candidate matches, list what you checked and ask me where the key
   is. Ensure the file is `chmod 600`.

5. **Persist configuration in `~/.zshrc`** (single managed block, see Rules).
   The block must contain:
   - `export REMOTE_SSH_KEY_PATH="<verified key path>"`, only if it differs
     from the default `~/.ssh/<your-key-pair-name>.pem`.
   - Convenience aliases (substitute `<LOCAL_CONTEXT>` with the actual local
     docker context name discovered in step 2):

     ```sh
     alias gdev-connect='(cd /Users/mdresden/Workspace/caylent-solutions/general-dev/.devcontainer/remote-docker && ./docker-tunnel.sh)'
     alias gdev-disconnect='docker context use <LOCAL_CONTEXT>'
     alias gdev-shell='/Users/mdresden/Workspace/caylent-solutions/general-dev/.devcontainer/remote-docker/shell.sh'
     ```

6. **Trust workspaces automatically.** VS Code otherwise asks "Do you trust the
   authors of the files in this folder" whenever it opens one it has not seen,
   and every rebuilt container is a new URI, so it asks again after each
   rebuild. Add to `~/Library/Application Support/Code/User/settings.json`
   (create it if absent; it is JSONC, so comments are allowed):

   ```json
   "security.workspace.trust.enabled": false
   ```

   It has to be the *user* settings file. All five `security.workspace.trust.*`
   settings are application-scoped, which VS Code reads only from there: they
   are ignored in a workspace's `.vscode/settings.json` and in
   `devcontainer.json` customizations, so neither can be used to do this.

   Tell me before applying it, because it applies to every folder opened on
   this Mac. Workspace trust is what withholds automatic execution of tasks,
   debug configurations and some extensions from a repository that has not
   been reviewed, and this project clones other repositories into its
   workspace.

7. **Validate end-to-end.** In a shell that has sourced the new block:
   - `gdev-connect` → must end with
     `Connected: remote docker server … via context 'general-dev-remote'`.
   - `docker ps` → talks to the remote engine without error.
   - `gdev-disconnect` → `docker context show` prints the local context.
   - `gdev-connect` again and LEAVE IT CONNECTED (my next step is launching
     the remote devcontainer from VS Code).
   - Re-run your `~/.zshrc` block installation once more and verify the file
     contains exactly one block (idempotency proof).

8. **Report.** Summarize: what was installed vs already present, the key
   path used, the aliases created, and validation results. Remind me that
   the next action is VS Code → *Dev Containers: Clone Repository in
   Container Volume*, that `make help` in the repo root documents every
   remote-engine operation, and that the daily connect/disconnect workflow
   is documented in `.devcontainer/remote-docker/README.md`.

## Done when

- All tools verified, SSO login works for account `<aws-account-id>`.
- The verified key path is used by the scripts (via default or exported var).
- `~/.zshrc` contains exactly one managed block after two runs.
- `gdev-connect` / `gdev-disconnect` / `gdev-shell` all work, and the Mac is
  left CONNECTED to the remote context.
- VS Code opens a devcontainer workspace without asking whether its authors are
  trusted, or I declined that step knowing it will ask on every rebuild.
