# Environment files

Three files configure this devcontainer. All three are gitignored because they
carry per-developer identity and configuration that must not reach git -- SSO
profile selection, git identity, proxy settings and feature toggles -- and
each has a committed `.example` alongside it.

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

> Ask Claude to write these files, never to *invent configuration values*. An
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
The wrapper normalizes `.devcontainer` automatically when it detects WSL. Avoid
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

`shell.env` carries identity and configuration -- git identity, AWS profile
selection, proxy settings -- and carries no credential. It is still
published to Parameter Store by `make push-secrets` so a remote container
can bootstrap itself; that mechanism is unchanged. What changed is what the
file is allowed to hold: every credential now lives in the secret catalog,
at its own parameter path, never in `shell.env`.

### The secret catalog

One backend, AWS Parameter Store: there is no second provider, no offline
store and no fallback to a local copy. A secret lives at
`/devcontainer/shared/secrets/<NAME>` or
`/devcontainer/<instance>/secrets/<NAME>`, both `SecureString` parameters;
the scope is a path prefix, so IAM enforces the boundary, not convention.
Resolution is instance-first then shared: a name is looked up in the
instance scope first, and in the shared scope only if the instance scope
does not hold it.

The `devsecret` CLI is the only way this repository reaches a catalog
secret:

- `devsecret get <NAME>` -- print one value on stdout, nothing else.
- `devsecret list [--scope <scope>]` -- names, scopes, last-changed and the
  exported flag; never a value.
- `devsecret set <NAME> [--scope <scope>] [--exported]` -- write a value
  read from stdin.
- `devsecret rm <NAME> --scope <scope>` -- delete after confirmation.
- `devsecret run --secrets A,B -- <cmd>` -- run `<cmd>` with only the named
  secrets in its environment.
- `devsecret export-list` -- names marked exported.

Run `devsecret --help` for the full reference, including every exit code.

A value is never accepted as a command-line argument. `devsecret set`
refuses one with exit 5, since an argument reaches the process table, where
any other user on this machine can read it. Pipe it on stdin instead:

```bash
printf '%s' "$VALUE" | devsecret set <NAME> --exported
```

`devsecret set` also refuses an interactive TTY paste with exit 2 unless
`--stdin` confirms it is deliberate.

The value still never reaches the process table. It travels to the `aws` CLI
inside a `--cli-input-json` document rather than as an argument, and that
document is a file the client creates with `O_EXCL` at mode `0600`, inside a
directory at mode `0700` that it removes before returning. A file is used
because the `aws` CLI v2 cannot read `file:///dev/stdin`: it reports "Invalid
JSON received" whether stdin is a pipe or a redirected regular file, and
blocks indefinitely on a FIFO. That bounded, private on-disk window is the
same one certificate issuance already accepts for a server private key, and a
far smaller exposure than an argument every process on the host can read for
the life of the call.

Neither engine needs a stored credential: the remote engine reaches the
store through the instance role over IMDSv2, and the local engine reaches
it through the developer's already-valid AWS SSO session.

### Configuration variables

| Variable | Default | Governs |
|---|---|---|
| `AWS_PROFILE` | `default` | The AWS CLI profile the local engine resolves credentials from when reaching the catalog. Read only; never set by this repository. |
| `SECRET_CACHE_DIR` | A writable, RAM-backed (`tmpfs`/`ramfs`) mount point chosen from the mount table, outside the repository and the container workspace. Falls back to the platform temporary directory in two cases: when no mount table is available at all (used directly, for example macOS); or when a mount table is available but names no such writable, out-of-boundary RAM-backed row, in which case that same mount table then refuses the fallback with exit 5 (`SecretCacheExposureError`) rather than using it. | The RAM-backed, per-invocation, owner-only directory `devsecret run` creates outside the repository and the container workspace and hands to the child, so a tool that can read a value only from a file, not an environment variable, has somewhere to write one. `devsecret` itself never writes a value there. The directory, and everything under it, is removed when the child process exits. |

### Certificate lifetimes

`.claude/plugins/devcontainer/scripts/devcontainer_config/certs.py` is the
single definition site for these three variables (E6-F1-S1-T1): every
`create_ca`, `issue_server` and `issue_client` call resolves its own
lifetime from the matching variable below, never a literal at the call
site.

| Variable | Default | Defined in |
|---|---|---|
| `CERT_CA_DAYS` | `3650` | `certs.py`, read by `create_ca` |
| `CERT_SERVER_DAYS` | `365` | `certs.py`, read by `issue_server` |
| `CERT_CLIENT_DAYS` | `90` | `certs.py`, read by `issue_client` |

Each of the three is optional: the default above applies whenever it is
unset. When set, the value must be a positive whole number of days;
`certs.py` rejects a non-integer value (exit naming the variable, "is not
an integer") or a value that is zero or negative ("must be a positive
integer") rather than silently falling back to the default or to an
unbounded lifetime.

### Certificate expiry warning

`CERT_WARN_DAYS` (E6-F1-S1-T2) governs `make cert-status`'s warning window
(spec Section 4.1.2, Section 7.3), read in exactly one place,
`certs._resolve_cert_warn_days`, and threaded from there into every
`certs.classify` call the report makes -- never a second literal `14`
anywhere else in `certs.py`.

| Variable | Default | Defined in |
|---|---|---|
| `CERT_WARN_DAYS` | `14` | `certs.py`, read by `_resolve_cert_warn_days` for `make cert-status` |

`CERT_WARN_DAYS` is optional; the default of `14` applies whenever it is
unset. When set, it must be a non-negative whole number of days: `certs.py`
rejects a non-integer value or a negative value, naming the
variable, its value and the expected form, before any row of the report is
rendered, the identical fail-fast rule the three lifetime variables above
follow.

`make cert-status`'s exit code is deliberately three-valued: `0` when every
certificate is outside the warning window, `0` with a `RENEW` row naming the
`/devcontainer:certs INSTANCE=<name>` invocation that reissues it when a
certificate is inside the window but not yet expired, and `1` when any
certificate has already expired. A `RENEW` that failed the build would train
the operator to ignore it, and a certificate with days left on it still
works; only an already-expired certificate is a build-breaking failure. An
unreadable or unparsable certificate file is reported by naming the path
and the parse failure, never silently classified as `expired` -- that would
send the operator to reissue material that may be perfectly valid. `make
cert-status` reports the `ca` and `client` roles it finds locally under
`certs.DEFAULT_CERTS_ROOT`/`instances.certs_root()`'s certificate-directory
root (`$DOCKER_CONFIG/certs/<instance>/`, or `~/.docker/certs/<instance>/`
when `DOCKER_CONFIG` is unset -- see the "Instances" section's `DOCKER_CONFIG`
row below); the server certificate is never reported because it is never
persisted to that path in the first place --
`certs.issue_server` generates it into a temporary directory it removes
before returning, handing the material back as PEM text instead (see the
certificate-storage paragraph below for the full rule).

`certs.py` shells out to the `openssl` binary on `PATH` for every operation
above and requires OpenSSL 3.0 or later: signing a certificate passes
`x509 -req -copy_extensions copy`, a flag OpenSSL added in 3.0 and that
LibreSSL, including the `/usr/bin/openssl` macOS ships by default, does not
implement (`unknown option -copy_extensions`). On macOS, `brew install
openssl` installs the `openssl@3` keg; add `$(brew --prefix openssl@3)/bin`
to the FRONT of `PATH` in the shell profile rather than relying on the
Homebrew prefix's own `bin` directory already being ahead of `/usr/bin` --
Homebrew does not guarantee `openssl@3` is symlinked into that shared
prefix on every install -- then confirm `openssl version` reports `OpenSSL
3.x`, not `LibreSSL`; on Linux/WSL, most current distributions'
`apt-get install openssl` already satisfies the floor.
The `/devcontainer:setup-local` skill checks this alongside the other host
tools, and `certs/SKILL.md`'s `## Failure semantics` documents the failure
this produces when the requirement is unmet.

The certificate authority and client material these variables govern is
never stored in this repository: it lives outside the checkout entirely,
under `certs.DEFAULT_CERTS_ROOT`/`instances.certs_root()`'s `<instance>/`
directory (`$DOCKER_CONFIG/certs/<instance>/`, or
`~/.docker/certs/<instance>/` when `DOCKER_CONFIG` is unset; spec Section
5.5), the same "outside the repository, not merely ignored" rule this
document's other private files (`shell.env`,
`.devcontainer/aws-profile-map.json`,
`devcontainer-environment-variables.json`) follow for a different reason
higher up in this file. The server key and certificate are never persisted
under that path: `certs.issue_server` generates both inside a
`tempfile.TemporaryDirectory` at modes `0600` and `0644` respectively and
removes that directory before returning, handing them back as PEM text for
the caller to publish directly (`certs/SKILL.md`'s `## Material` states the
same rule).

### Transport

`.claude/plugins/devcontainer/scripts/devcontainer_config/transport.py`
(E6-F2-S1-T1) is the SSM port forward manager: it builds the
`aws ssm start-session --document-name AWS-StartPortForwardingSession`
argument vector that carries the docker API (no SSH element anywhere in
it), allocates the local end of that forward, and detects when it is ready
by waiting for the session process to report its port opened and then
confirming it with a connection to the local port, rather than waiting a
fixed amount of time. E6-F2-S1-T2 adds this module's other half: creating
or updating the per-instance docker context that carries the TLS material
(`transport.ensure_context`) and completing the `docker version` handshake
that proves the whole path works (`transport.handshake`), translating a
handshake failure into the SAN requirement or a connection diagnosis
(`transport.diagnose_handshake_failure`) rather than a bare, opaque TLS
error -- see `docs/devcontainer.md`'s "Transport" section.
`.claude/plugins/devcontainer/scripts/devcontainer_config/hostprobe.py`
(E1-F3-S1-T1) is the docker-handshake probe `engine`, `setup-local` and
`setup-remote` share for their own, simpler "does the engine answer" check.

`DEVCONTAINER_TRANSPORT` selects the transport for the `make connect` entry
point. Since the cutover there is one transport, so the selector has one
accepted value, `ssm`, which is also the default. It is kept rather than
removed because it is the seam a future transport is added at, and because a
caller that still exports the old value gets told so rather than being
silently redirected.

It has two independent readers: the Makefile's `connect` recipe, which reads
the variable from the environment and dispatches on it, and `transport.py`'s
`resolve_transport`, which the module's own `connect` subcommand calls. Both
dispatch the same way. Unset selects `ssm`. The literal `ssm` selects it
explicitly. Any other non-empty value, including the `ssh` that used to be the
default, exits non-zero before any transport starts, printing an `ERROR:` line
to stderr naming the variable, the offending value and the accepted value. No
branch silently defaults.

The one value the two readers do not agree on is the empty string: the
Makefile's `${DEVCONTAINER_TRANSPORT:-ssm}` substitution treats a set-but-empty
value the same as unset and selects `ssm`, while `resolve_transport`'s
`os.environ.get` returns the empty string itself rather than the default, which
is not in the accepted set, so it raises `TransportError` naming the empty
value. Setting the variable to an explicit empty string is not a supported way
to request the default; leave it unset instead.

| Variable | Default | Defined in |
|---|---|---|
| `DOCKER_TLS_PORT` | `2376` | `transport.py`, read by `build_start_session_argv` |
| `SSM_FORWARD_TIMEOUT` | `30` | `transport.py`, read by `wait_ready` and `stop_forward` |
| `MATERIAL_INSTALL_TIMEOUT` | `300` | `transport.py`, read by `install_material` for the instance-side fetch and daemon start |
| `DOCKER_HANDSHAKE_TIMEOUT` | `30` | `hostprobe.py`'s `read_positive_seconds`, the one place its name, default and validation are declared; `transport.py`'s `handshake` (E6-F2-S1-T2) calls the same function rather than declaring its own copy |
| `DEVCONTAINER_TRANSPORT` | `ssm` | selector for `make connect`'s transport dispatch; both the Makefile's `connect` recipe and `transport.py`'s `resolve_transport` (used by the module's own `connect` subcommand) read and dispatch on it; `ssm` is the only accepted value, and `ssh` is rejected by name as removed at cutover |

`DOCKER_TLS_PORT` is the port the rootless docker daemon on the instance
listens on, `127.0.0.1`-only, behind a security group with zero ingress
rules (spec Section 5.6); `transport.py` reads it in exactly one place and
never hardcodes `2376` at any call site. The local end of the forward is a
different, per-instance port: allocated and recorded in the instance's
docker context endpoint (never a fixed number, spec Section 9), reused
across reconnects when that context already exists. `SSM_FORWARD_TIMEOUT`
has two readers, and raising it affects both: `transport.wait_ready` bounds
by it how long it waits for the session process to announce the port
opened and then confirm that with a connection attempt, naming the
instance, the port and this variable before raising; `transport.stop_forward`
separately bounds by the same variable the grace period it gives the
session process to exit cleanly after asking it to terminate, before
escalating to a kill. `DOCKER_HANDSHAKE_TIMEOUT` is read by two callers that
each bound a different `docker version` call, but through one shared
reader: `hostprobe.read_positive_seconds` is the single place this
variable's name, the `30` default and the finite/positive validation are
declared (AC-FUNC-007), and both `hostprobe.py`'s `probe_docker` handshake
check (one single `docker version` call) and `transport.py`'s `handshake`
(E6-F2-S1-T2, which retries its own `docker version` call against a
context it may have just created, until it answers or this deadline
passes) call that same function rather than either declaring its own copy
of the variable or its default. `transport.py`'s own
`_positive_seconds_from_env` wraps `read_positive_seconds`'s
`HostProbeError` in its own `TransportError` naming the variable, its
offending value and the default, so a caller of `transport.py` never needs
to catch an exception type owned by `hostprobe.py`; both
`_forward_timeout_seconds` (`SSM_FORWARD_TIMEOUT`) and
`_handshake_timeout_seconds` (`DOCKER_HANDSHAKE_TIMEOUT`) call that one
wrap rather than each declaring its own. Naming the context, the local
port and this variable together is a different error,
`DockerHandshakeTimeoutError`, which `transport.handshake` raises instead,
only once the retried `docker version` call itself either never answers
before the deadline or answers with no budget left for the rootless probe
that follows it -- `handshake` already knows the context and the port at
that point, which the timeout reader itself never does. All five
variables, `DOCKER_TLS_PORT`, `SSM_FORWARD_TIMEOUT`,
`MATERIAL_INSTALL_TIMEOUT`, `DOCKER_HANDSHAKE_TIMEOUT` and
`DEVCONTAINER_TRANSPORT`, are optional. `DOCKER_TLS_PORT`,
`SSM_FORWARD_TIMEOUT`, `MATERIAL_INSTALL_TIMEOUT` and
`DOCKER_HANDSHAKE_TIMEOUT` are read by code in this repository today: each default above applies
whenever the variable is unset, and each reader rejects a non-numeric or
non-positive value naming the variable and its value rather than
silently falling back to the default. `DEVCONTAINER_TRANSPORT`'s two
readers apply the same fail-fast rule to every non-empty value other than
`ssh` or `ssm`: the Makefile's `connect` recipe rejects it by name, and
`transport.py`'s `resolve_transport` does the same; the one value they
disagree on, the empty string, is documented above.

### Repository slug derivation

`remote-instances/root.hcl`'s own `repo_slug` local derives a repo-slug
component of the Terraform state bucket name, consumed by that same
file's `state_bucket_name` local, from `git config --get
remote.origin.url`, applying a `basename()`-plus-`trimsuffix(".git")`
transform to that URL. `tests/test_state_bucket_name.py`'s private
`_repo_slug_from_git_remote` reads the identical git remote the same way,
bounded, for readers of the variable, by `REPO_SLUG_GIT_TIMEOUT_SECONDS`,
resolved through `hostprobe.py`'s `read_positive_seconds`, the same shared
reader `transport.py` uses for `DOCKER_HANDSHAKE_TIMEOUT`.
`read_positive_seconds` rejects a non-numeric or non-positive value by
name rather than silently falling back to the default, and
`_repo_slug_from_git_remote` reads a `10` second default for that
git-remote read.

| Variable | Default | Defined in |
|---|---|---|
| `REPO_SLUG_GIT_TIMEOUT_SECONDS` | `10` | Resolved through `hostprobe.py`'s `read_positive_seconds`; read by `tests/test_state_bucket_name.py`'s private `_repo_slug_from_git_remote` |

`read_positive_seconds` applies the same fail-fast rule
`DOCKER_HANDSHAKE_TIMEOUT`'s reader applies to every reader of
`REPO_SLUG_GIT_TIMEOUT_SECONDS`: it rejects a non-numeric or non-positive
value by name rather than silently falling back to the default.

### Instances

`.claude/plugins/devcontainer/scripts/devcontainer_config/instances.py` is
the single module that turns an instance name into every artifact spec
Section 9's addressing table names -- the Terragrunt directory, the state
key, the docker context, the Parameter Store prefix, the certificate
directory, and the recorded forwarded port -- and the single module that
decides which instance a command means. `resolve-instance` is its entry
point (`PYTHONPATH=.claude/plugins/devcontainer/scripts python3 -m
devcontainer_config.cli resolve-instance`): it prints the resolved name
and the statically derivable half of the addressing block -- everything
except the forwarded port, which needs a real docker context -- as one
`KEY=value` line per artifact on stdout, so the shell layer reads values
rather than re-deriving them, and prints nothing to stdout and exits 1
with the operator-facing text on stderr on any resolution failure.

The certificate-directory row honors `DOCKER_CONFIG`, the real docker
CLI's own variable for relocating its configuration home, so an operator
who has moved it also gets certificate material addressed under the same,
moved location:

| Variable | Default | Governs |
|---|---|---|
| `DOCKER_CONFIG` | Unset (`~/.docker`) | `instances.certs_dir`'s (and `certs.DEFAULT_CERTS_ROOT`'s) certificate-directory root: an absolute directory path. When set, certificates are addressed and written under `$DOCKER_CONFIG/certs/<name>/` instead of `~/.docker/certs/<name>/`. Read only; never set by this repository. |
| `INSTANCE` | Unset | `instances.resolve`'s first resolution step: the instance name to select this call, read directly from the process environment. Optional. When set, must pass `instances.validate_name` -- a non-empty path segment of letters, digits, hyphens and underscores only, at most `instances.MAX_INSTANCE_NAME_LENGTH` (63) characters -- or resolution fails naming the value and the rule it broke. |
| `DEFAULT_REMOTE_INSTANCE` | Unset | `instances.resolve`'s second resolution step, read directly from the process environment when `INSTANCE` is unset. Optional; has no entry in `shell.env.example`. Same format as `INSTANCE`: a non-empty path segment of at most `instances.MAX_INSTANCE_NAME_LENGTH` (63) characters. |

`DEFAULT_REMOTE_INSTANCE` is the second step of the instance-resolution
order spec Section 4.1.1 fixes, evaluated once per resolution by
`instances.resolve`:

1. `INSTANCE` in the process environment. No Makefile target accepts or
   forwards `INSTANCE` yet (`E8-F2-S1-T1` wires `make <target>
   INSTANCE=<name>` in); today, set it directly:
   `INSTANCE=<name> PYTHONPATH=.claude/plugins/devcontainer/scripts
   python3 -m devcontainer_config.cli resolve-instance`.
2. `DEFAULT_REMOTE_INSTANCE` from `shell.env`.
3. The sole directory under `remote-instances/`, when exactly one is
   configured.
4. Otherwise: failure.

Both `INSTANCE` and `DEFAULT_REMOTE_INSTANCE` are read directly from the
process environment by `instances.resolve`, the identical convention
`transport.resolve_transport` already establishes for
`DEVCONTAINER_TRANSPORT` above rather than an injected mapping. Three edge
cases each produce their own distinct outcome instead of folding into one
failure:

- `INSTANCE` naming no directory under `remote-instances/` fails, listing
  every instance actually configured.
- `INSTANCE` set while the local backend is active is a warning on
  stderr, not an error: the value names nothing on that backend and is
  simply unused, so nothing is resolved and nothing fails. This outcome is
  gated on `resolve-instance`'s own `--local-backend-active` flag (there is
  no other caller of `instances.resolve` yet): passing `INSTANCE=<name>
  ... resolve-instance` alone still resolves against `remote-instances/`
  and fails if `<name>` names no directory there; passing `INSTANCE=<name>
  ... resolve-instance --local-backend-active` is what produces the
  warning. A future caller that already knows the active backend (the
  Makefile, per spec Section 1.1) passes `local_backend_active=True` to
  `instances.resolve` directly instead of through this flag.
- An empty (or absent) `remote-instances/` directory on a remote backend
  fails, directing the operator to `/devcontainer:setup-remote`.

Ambiguity -- more than one instance configured and neither selector chose
one -- fails naming every configured instance and both remedies,
`make <target> INSTANCE=<name>` and `DEFAULT_REMOTE_INSTANCE='<name>'` in
`shell.env`, with exit code 1 and no partial work: resolution happens
before any docker or AWS call.

`DEFAULT_REMOTE_INSTANCE` has no default and no entry in
`shell.env.example`: unset, it is simply the second resolution step
producing nothing, and resolution proceeds to the third. A developer who
wants one instance to be their implicit default sets it in their own
`shell.env`.

### What did not change

- `shell.env` is still published to Parameter Store by `make push-secrets`
  so remote containers can bootstrap themselves.
- `build` and `rebuild` publish `shell.env` themselves when it is newer than
  the stored copy, so editing it is enough, no separate step to remember.
- The container's **git credential** does not come from `shell.env`. It is
  copied from the credential helper already working on your machine by
  `make push-git-creds`, which `make build` runs automatically.
