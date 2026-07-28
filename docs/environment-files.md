# Environment files

Three files configure this devcontainer. All three are gitignored because they
carry per-developer identity and secrets, and each has a committed `.example`
alongside it.

| File | Example | What it is |
|---|---|---|
| `shell.env` | `shell.env.example` | Environment sourced by every shell in the container |
| `.devcontainer/aws-profile-map.json` | `.devcontainer/aws-profile-map.json.example` | AWS SSO profiles, rendered into `~/.aws/config` |
| `devcontainer-environment-variables.json` | `devcontainer-environment-variables.json.example` | `cdevcontainer` template input that regenerates the other two |

```bash
cp shell.env.example shell.env
cp .devcontainer/aws-profile-map.json.example .devcontainer/aws-profile-map.json
cp devcontainer-environment-variables.json.example devcontainer-environment-variables.json
```

Then replace every `<PLACEHOLDER>`. Nothing else is required, placeholders are
the only thing standing between a fresh clone and a working container.

## Filling them out with Claude

These files are structured enough to delegate. From the repo root:

```text
Read docs/environment-files.md and shell.env.example, then create shell.env
for me. My git identity is <name> / <email>, my git provider is github.com,
my default branch is main, and I am on <macOS|Linux|WSL>. I am not behind a
corporate proxy.
```

For the AWS map, give the SSO portal URL and the accounts:

```text
Read .devcontainer/aws-profile-map.json.example and create
.devcontainer/aws-profile-map.json with profiles for these accounts:
  <alias> <account-id> <PermissionSetName> in <region>
My SSO portal is https://<portal>.awsapps.com/start in us-east-1.
```

If you already have working profiles on the host, they can be derived instead
of retyped:

```text
Read ~/.aws/config and .devcontainer/aws-profile-map.json.example, then build
.devcontainer/aws-profile-map.json from the SSO profiles you find there.
```

> Ask Claude to write these files, never to *fill in secrets it invents*. An
> account ID or SSO URL that looks plausible but is wrong fails at `aws sso
> login`, not at provisioning, which is a slower and more confusing failure.

## What each value does

`shell.env.example` documents every variable inline. The ones that stop
provisioning if wrong:

| Variable | Consequence |
|---|---|
| `DEFAULT_GIT_BRANCH` | unset fails the build immediately |
| `GIT_USER`, `GIT_USER_EMAIL` | become the container's global git identity |
| `GIT_PROVIDER_URL` | host for the credential helper and SSH→HTTPS rewrite |
| `AWS_CONFIG_ENABLED` | `false` skips AWS profile generation entirely |
| `HOST_PROXY` | `true` requires a reachable `HOST_PROXY_URL` or the build fails |

`BASH_ENV` points at the **in-container** path
(`/workspaces/<repo>/shell.env`), not a host path, the same file is at a
different location depending on which side reads it.

## Host differences

Most values are identical across hosts. These are not.

### Reaching a host proxy from the container

Only relevant when `HOST_PROXY=true`.

| Host | `HOST_PROXY_URL` | Why |
|---|---|---|
| macOS (Docker Desktop / OrbStack) | `http://host.docker.internal:3128` | resolves to the host automatically |
| Windows + WSL | `http://host.docker.internal:3128` | same, provided Docker Desktop's WSL integration is on |
| Linux | `http://172.17.0.1:3128` | `host.docker.internal` does not exist; use the docker0 bridge address, or add `--add-host=host.docker.internal:host-gateway` |

Confirm the bridge address on Linux:

```bash
ip -4 addr show docker0 | awk '/inet /{print $2}' | cut -d/ -f1
```

### Running the proxy itself

One script serves all hosts, `.devcontainer/tinyproxy-daemon.sh`, with
wrappers under `nix-family-os/` and `wsl-family-os/` for discoverability. It
detects the host family and picks `ss` or `lsof` automatically.

| Host | Install tinyproxy | Run from |
|---|---|---|
| macOS | `brew install tinyproxy` | a normal terminal |
| Linux | `sudo apt-get install tinyproxy` | a normal terminal |
| Windows | `sudo apt-get install tinyproxy` inside WSL | a **WSL** shell, not PowerShell |

Settings live in `.devcontainer/remote-docker/config.env` alongside the rest of
the host-side configuration, so nothing has to be exported by hand. Only
`TINYPROXY_UPSTREAM_HOST` has no default, set it there or in your environment:

```bash
make proxy-start
make proxy-status
make proxy-restart
make proxy-stop
```

| Setting | Default |
|---|---|
| `TINYPROXY_UPSTREAM_HOST` | *(none, required)* |
| `TINYPROXY_UPSTREAM_PORT` | `8080` |
| `TINYPROXY_PORT` | `3128`, must match the port in `HOST_PROXY_URL` |
| `TINYPROXY_READINESS_TIMEOUT` / `TINYPROXY_STOP_TIMEOUT` | `10` |
| `TINYPROXY_STATE_DIR` | `~/.devcontainer-proxy`, override to run more than one |

Starting the proxy does not make the container use it: set `HOST_PROXY=true`
and `HOST_PROXY_URL` in `shell.env` for that.

### Line endings on Windows

A checkout on a Windows filesystem can carry CRLF, which breaks shell scripts.
The wrapper normalises `.devcontainer` automatically when it detects WSL. Avoid
the problem at source by cloning inside the WSL filesystem (`~/...`, not
`/mnt/c/...`), which is also markedly faster.

### The local docker context

`make disconnect` returns docker to the local engine, whose context name
differs by host. Set `LOCAL_DOCKER_CONTEXT` in
`.devcontainer/remote-docker/config.env`:

| Host | Typical value |
|---|---|
| macOS with OrbStack | `orbstack` |
| macOS with Docker Desktop | `desktop-linux` |
| Linux | `default` |
| Windows + WSL | `default` |

`make disconnect` lists the contexts that actually exist if the configured one
does not.

### Prerequisites for `make build`

| Tool | macOS | Linux / WSL |
|---|---|---|
| aws CLI v2 | `brew install awscli` | distro package or AWS installer |
| session-manager-plugin | `brew install --cask session-manager-plugin` | AWS `.deb` |
| jq | `brew install jq` | `sudo apt-get install jq` |
| devcontainer CLI | `npm install -g @devcontainers/cli` | same |
| git, python3 | Xcode command line tools | usually preinstalled |

Every target checks its own prerequisites and fails with the install command
for whatever is missing.

## Secrets

`shell.env` is published to Parameter Store by `make push-secrets` so remote
containers can bootstrap themselves. Consequences worth knowing:

- Anything in `shell.env` reaches AWS. That is intended for configuration, and
  acceptable for secrets you would store in Parameter Store anyway.
- The container's **git credential** does not come from here. It is copied from
  the credential helper already working on your machine by
  `make push-git-creds`, which `make build` runs automatically.
- `build` and `rebuild` publish `shell.env` themselves when it is newer than
  the stored copy, so editing it is enough, no separate step to remember.
