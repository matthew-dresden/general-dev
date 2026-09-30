# Environment setup runbook

The ordered sequence a fresh machine follows from nothing to a verified
devcontainer, with a verification step after each stage. It is written so
that neither a new developer nor an AI agent needs tribal knowledge: every
command is stated, every check says what success looks like, and every
failure names its own remedy. Nothing here asks you to improvise.

This runbook expands the README's local quick start into full detail. The
reference material it points at rather than repeats:
[environment-files.md](environment-files.md) (what each private file and
every value in it is), [devcontainer.md](devcontainer.md) (how the
container is defined, provisioned and supplied with credentials), and the
[remote-docker README](../.devcontainer/remote-docker/README.md) (the EC2
engine itself).

One principle runs through the whole sequence: everything below runs on
the host, the Mac. The container never resolves a credential, never reads
the keychain, never runs an AWS login of its own. Each value moves once,
from the place only this machine holds it into the container, and the
container's shell startup does the rest.

## Prerequisites

The tooling is macOS-first: the keychain credential source, the OrbStack
context name and `make keybindings` all assume it. Install and verify each
tool before moving on; a missing one fails later with the same install
command, but earlier is cheaper.

| Tool | Install | Verify |
|---|---|---|
| macOS | (the machine you are setting up) | `uname -s` prints `Darwin` |
| Docker engine + CLI (OrbStack) | `brew install orbstack` | `docker info` reports a running engine |
| VS Code + `code` command | `brew install --cask visual-studio-code`, then Command Palette → "Shell Command: Install 'code' command in PATH" | `code --version` prints three lines |
| git | `xcode-select --install` | `git --version` |
| uv | `brew install uv` | `uv --version` |
| zsh | ships with macOS | `zsh --version` |
| aws CLI v2 | `brew install awscli` | `aws --version` prints `aws-cli/2…` |
| gh | `brew install gh`, then `gh auth login` | `gh auth status` reports a login |
| devcontainer CLI | `npm install -g @devcontainers/cli` | `devcontainer --version` |
| jq | `brew install jq` | `jq --version` |

Two of those need one more step beyond installation:

- The aws CLI needs a profile you can log into, because the `aws-export`
  credential source resolves through the host's own session:
  `aws sso login --profile <profile>` must succeed for every profile an
  `aws-export` manifest entry will name.
- gh needs its login because the `git` credential source resolves through
  git's credential helper on this machine, and `gh auth login` is what
  feeds that helper for github.com.

Verify: every row's command above answers without an error.

## 1. Clone the repository

```sh
git clone <this repository's URL>
cd general-dev
```

Verify: `git status` names the branch and reports a clean tree.

## 2. Create the private files

`make init` copies four gitignored files from their committed examples,
never overwriting a file that already exists:

- `shell.env`
- `devcontainer-environment-variables.json`
- `.devcontainer/aws-profile-map.json`
- `.devcontainer/hostcreds.map.json`

The first three hold identity and configuration; the fourth is the
hostcreds manifest, the one list of credentials this machine pushes. All
four are private on purpose -- they name you and your accounts -- which is
why `make init` copies them from examples instead of the repository
shipping them directly.

Verify: `make init` prints `created` for each of the four, then lists the
placeholders still to replace. A second run prints `already exists, left
untouched` for every file, which is the idempotence check.

## 3. Fill the three configuration files

Replace every `<PLACEHOLDER>` in `shell.env` (git identity, default
branch, proxy settings), `devcontainer-environment-variables.json` (the
template input), and `.devcontainer/aws-profile-map.json` (your SSO
profiles). What each value does, ready-made prompts for delegating the
filling to Claude, and the macOS / Linux / WSL differences are all in
[environment-files.md](environment-files.md).

Verify: run `make init` again. It scans all four files and ends with
`[DONE] no placeholders left` -- or lists exactly which placeholders
remain, per file, until then.

## 4. Author the hostcreds manifest

`.devcontainer/hostcreds.map.json` (created in step 2 from its example) is
a JSON object mapping a credential name to its source. No value is ever
written into the manifest; it names where each value lives, and the push
resolves it. The committed example shows one entry per source:

```json
{
  "EXAMPLE_API_TOKEN": {
    "source": "keychain"
  },
  "GIT_GITHUB": {
    "source": "git",
    "host": "github.com"
  },
  "AWS_DEFAULT": {
    "source": "aws-export",
    "profile": "default"
  }
}
```

What each source does:

- `keychain` -- a value stored in the macOS keychain. By default the item
  is addressed as service `devcontainer/<project>/<NAME>`, where
  `<project>` is the checkout directory's name, so one project's items
  cannot collide with another's; optional `service` and `account` labels
  override that for a credential that already lives somewhere else. In the
  container the credential is exported under its manifest name. Store the
  item with `make creds-init` in the next step.
- `git` -- the password git's own credential helper holds for the bare
  hostname in its required `host` label (`gh auth login` feeds that helper
  for github.com). Inside the container it seeds `~/.git-credentials` and
  exports no variable: git reads it through its own helper, and copying it
  into the environment would only widen its exposure.
- `aws-export` -- the session credentials `aws configure
  export-credentials` prints on this machine, for the optional `profile`
  label's profile (`default` when unset). In the container it becomes the
  three standard AWS variables, so every SDK and the aws CLI pick the
  session up. It expires with the session; a fragment past expiry exports
  nothing and says so (see Troubleshooting).

A credential name must match `[A-Z][A-Z0-9_]*` -- an uppercase letter,
then uppercase letters, digits or underscores -- because the name becomes
both a shell variable and a `<NAME>.env` fragment filename. Reserved names
are refused outright: `PATH`, `LD_PRELOAD`, `DYLD_INSERT_LIBRARIES`,
`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` and `AWS_SESSION_TOKEN`, each
of which would silently shadow a real variable at shell startup.

The one entry this repository's committed configuration expects is
`ZAI_API_KEY` from the keychain: the opencode config interpolates
`{env:ZAI_API_KEY}`, and without that entry opencode in the container
authenticates with nothing. The full schema, defaults and the scanner
integration are in [environment-files.md](environment-files.md)'s "Host
credentials (hostcreds)" section.

Verify (validates the whole manifest fail-fast, no container needed):

```sh
PYTHONPATH=.claude/plugins/devcontainer/scripts \
  python3 -m devcontainer_config.cli creds-fragments --print-git-hosts
```

Success prints the git-source hostnames, one per line, and nothing else;
a malformed manifest exits non-zero listing every problem in the file at
once, each naming its entry.

## 5. Store the keychain items

```sh
make creds-init
```

Prompts once per keychain credential the manifest names whose item does
not exist yet (the prompt shows the keychain service it will store under;
typed input is not echoed), then stores it. Items that already exist are
reported as `already present` and not re-asked. The value rides stdin into
`security -i`, never a command-line argument, because an argument reaches
the process table where any other user of this machine can read it.

The automation path feeds exactly one named credential from stdin, with
the value never appearing in the command line:

```sh
printf '%s' "$ZAI_API_KEY" | make creds-init CREDS_INIT_ARGS='--stdin ZAI_API_KEY'
```

One trailing newline is stripped; an empty value, or one containing a line
break, is an error rather than a stored credential.

Verify: the command prints `stored: <names>` and/or
`already present: <names>` and exits 0.

## 6. Bind Shift+Enter (once per machine)

```sh
make keybindings
```

Makes Shift+Enter insert a newline in every VS Code terminal, which Claude
Code and opencode prompts need. It must run on the host because the VS
Code window resolves keybindings where it runs, not inside the container;
reload the window afterwards. Running `/terminal-setup` from a container
terminal writes the binding where no VS Code process reads it, which is
why it appears to do nothing.

Verify: in a VS Code terminal, Shift+Enter inserts a newline instead of
submitting the current prompt.

## 7. Point at the local engine

```sh
make local
```

Switches the active docker context to the local engine
(`LOCAL_DOCKER_CONTEXT` from `.devcontainer/remote-docker/config.env`,
`orbstack` by default). Nothing remote is stopped.

Verify: `docker context show` prints the local context name.

## 8. Build the container

```sh
make build
```

Builds the image, runs postCreate, and -- as its last step -- pushes every
credential the manifest names. The push resolves each entry on this
machine, writes one `<NAME>.env` fragment per credential into
`~/.hostcreds/` inside the container (directory mode 700, fragments mode
600), seeds `~/.git-credentials` for git-source entries, and proves the
git credential with `git ls-remote origin` before reporting success. The
build refuses to start if a container for this project already exists on
the engine (`make rebuild` replaces one deliberately) and blocks, exit
code and all, until the container is actually up.

A rebuild wipes the credentials by design: `~/.hostcreds` lives in the
container's own filesystem, not a volume, so a rebuilt container starts
with none -- and the build that created it has already pushed a fresh set.
There is nothing to restore by hand, ever.

Verify: the command exits 0 after printing `pushed <N> credential
fragment(s) into ~/.hostcreds` and `container is up`. `make status` shows
one container, running, for this project.

## 9. Open VS Code

```sh
make reopen
```

Seeds the VS Code server the window will need (so the first attach does
not transfer ~200 MiB over the connection) and opens the workspace
attached to the container.

Verify: the window opens on `/workspaces/<project>` and a terminal lands
in the shared tmux session; `echo $DEVCONTAINER` prints `true`.

## 10. Verify the container

```sh
make verify-container
```

Re-checks the pushed credentials inside the container, printing one PASS
line per check: the store directory at mode 700, every fragment at mode
600, the hostcreds startup block present in both `~/.bashrc` and
`~/.zshenv`, a shell startup that prints no hostcreds error, `git
ls-remote` when the manifest names a git-source entry, and
`aws sts get-caller-identity` when it names an aws-export one. Any FAIL
names its check and the exit code is non-zero; there is no check whose
failure is silent.

Verify: every line prints PASS and the command exits 0.

At this point the environment is complete. Day to day:

| Goal | Do this |
|---|---|
| A shell inside the container | `make exec` |
| What state is anything in | `make status` (read-only, start here when something looks wrong) |
| Pause / resume the container | `make stop`, `make start` (the checkout survives either) |
| Refresh pushed credentials after a rotation or an SSO re-login | `make push-creds` (`make up` does it too, plus its own engine checks) |
| Rebuild from scratch | `make rebuild` (credentials re-push automatically, see step 8) |

## Pointing at the remote engine instead

Steps 1 through 6 are unchanged: the manifest, the keychain items and the
keybinding are properties of this machine, not of either engine. From step
7 on, the remote route has a lifecycle of its own, described below; the
certificate material's reference half is
[devcontainer.md](devcontainer.md)'s "Certificate lifecycle" section, and
the per-instance deployment file's contract is
[remote-instances/README.md](../remote-instances/README.md). The credential
push needs no remote variant at all: it rides whichever docker context the
run addresses, so the very same `make push-creds` and `make build` steps
deliver the same fragments to the EC2 engine's container.

### The lifecycle at a glance

One remote engine per project under `remote-instances/`; the instance name
is the project name (e.g. `acme`). The `make help` INSTANCES group drives
every stage:

| Stage | Command | What it does |
|---|---|---|
| Scaffold (no EC2 exists yet) | `make instance-init INSTANCE=<project>` | Writes `remote-instances/<project>/terragrunt.hcl`; never deploys |
| Edit the deployment | edit the file `instance-init` wrote | Instance type, volume sizes, availability zone, tags, AMI |
| Provision and converge | `make instance-deploy INSTANCE=<project>` | Terragrunt apply, id link, trust chain where missing, secret push |
| Open the forward | `make remote INSTANCE=<project>` | Refreshes the SSM port forward, points docker at the engine; blocks until interrupted |
| Build and open | `make build INSTANCE=<project>`, then `make reopen INSTANCE=<project>` | Clone into a volume on the engine, build, run postCreate, attach VS Code |
| Day to day | `make status`, `stop`, `start`, `exec` with `INSTANCE=<project>` | Container lifecycle; the checkout survives all of it |
| Pause to save cost | `make instance-stop INSTANCE=<project>`, later `make instance-start INSTANCE=<project>` | Stops and starts the EC2 instance; containers, volumes and checkouts survive |
| Retire | `make instance-destroy INSTANCE=<project>` | Terragrunt destroy plus cleanup of parameters, certificates, context and id |

`make list-instances` reports every configured instance's live state at any
point: EC2 state, recorded id, Parameter Store and certificate material,
forwarded port, docker context. `make instance-status INSTANCE=<name>`
narrows it to one instance; the targets that accept it also take `ALL=1`
for every configured instance at once.

### What scaffold and deploy do

`make instance-init INSTANCE=<name>` writes the one file a new instance
requires, `remote-instances/<name>/terragrunt.hcl`, from a template
carrying the default sizing and a freshly allocated CIDR block, then prints
the commonly edited inputs. It never runs Terragrunt. Its `REGION=` option
selects only where the default AMI and availability zone are looked up; the
deployment region is `REMOTE_AWS_REGION`, which every Terragrunt-running
target requires with no default.

`make instance-deploy INSTANCE=<name>` converges the instance: terragrunt
init (bootstrapping the fleet's shared state bucket on the very first
run), validate, a plan guarded against accidental replacement, apply of
exactly the guarded plan, then follow-ups that run only when a status probe
reports the corresponding piece missing -- recording the applied EC2 id,
issuing and publishing the certificate material, and pushing secrets. When
the material is already present, the steps are skipped entirely, so a
deploy never restarts a live daemon; renewals are manual, via
`make cert-status`. The replacement guard is the safety net worth knowing:
a plan that would replace or destroy resources is refused unless
`CONFIRM=replace` is set, naming the offending plan lines (see
Troubleshooting).

### Why mutual TLS, and what the certificates are for

Two independent factors stack up between this machine and the docker
daemon, each answering a different question. IAM authorizes the SSM
session: the `ssm:StartSession` grant decides who may open the port forward
at all, and removing it is the only revocation mechanism the platform has.
The certificates then authenticate the docker API itself, in both
directions, on top of that forward: the daemon presents a server
certificate the client verifies, and the client presents a certificate the
daemon verifies against the CA it holds. Neither factor substitutes for the
other -- a valid SSO session alone commands nothing, and a copied client
certificate is inert without a tunnel IAM authorizes.

Certificate material lives per instance under `~/.docker/certs/<name>/`
(or `$DOCKER_CONFIG/certs/<name>/`), and the same directory holds the
instance's recorded EC2 id: an `instance-id` file written by
`make instance-link` and, automatically, by `make instance-deploy`, read by
every remote target's resolver -- so no id variable is ever set by hand,
and an id dies with its instance. `make cert-status` reports client and CA
expiry per instance; a renewal is a deliberate, manual step and touches
nothing on the running instance, because the daemon accepts any client
certificate that chains to its CA.

### Stop, start and destroy

`make instance-stop INSTANCE=<name>` stops the EC2 instance and waits until
it reports stopped; `make instance-start INSTANCE=<name>` starts it again
and waits for its SSM agent to report ready. Everything on the instance
survives the cycle: containers, images, volumes, the cloned checkouts.
The port forward does not -- open it again after a start with
`make remote INSTANCE=<name>`; if connecting then still fails, reinstall
the daemon's TLS material with `make cert-install INSTANCE=<name>`.

`make instance-destroy INSTANCE=<name>` runs terragrunt destroy for that
instance, then cleans up everything Terragrunt does not know about: its
Parameter Store parameters, its docker context, its certificate directory
(the recorded id file included). `ALL=1` destroys every configured
instance, and only that form requires `CONFIRM=destroy`, because a typo'd
ALL should never be all it takes to end the fleet.

The remote-state bucket is shared by the whole fleet and stands outside
every instance's lifecycle: one bucket per AWS account, region and
repository (`tg-state-<account-id>-<region>-<repo-slug>-<suffix>`, derived
in `remote-instances/root.hcl`), holding one state key per instance
(`<name>/terraform.tfstate`). No destroy target deletes it; after the last
instance of a fleet is gone, deleting the bucket by hand is a separate,
deliberate step.

### Working with several engines at once

Every instance's port forward is its own: each instance's docker context
records an allocated local port, so forwards for several instances coexist
alongside the laptop's own engine without colliding. That is what makes the
`ENGINE` variable work. It addresses one engine explicitly instead of
following the machine-wide active docker context, so parallel terminals can
drive different engines concurrently without any of them switching the
context the others share.

`ENGINE=local` names this machine's engine; any other value names an
instance under `remote-instances/`. Both spellings below are the same
command:

```sh
make status ENGINE=acme
ENGINE=acme make status
```

Unset, every target behaves as before and follows the active context. Set,
every docker call the target makes is aimed at the engine it names: one
terminal can run `make build ENGINE=local` while another runs
`make build ENGINE=acme`, and a third drives a second instance. Each
remote engine needs its forward open first: `make remote INSTANCE=<name>`
opens it (in its own terminal -- it blocks), and later
`make connect ENGINE=<name>` re-opens a single forward without touching
what other terminals see. `make list-instances` shows the forward port each
instance's context records.

VS Code is per engine by attachment, not by switching: a window attaches to
the container it was opened against, wherever that container lives. Run one
window per engine and attach each to the container on that engine; `ENGINE`
changes what `make` addresses, never what an open window talks to.

Three targets refuse under `ENGINE` with exit code 2: `make local`,
`make disconnect` and `make remote` switch the machine-wide docker context,
which is exactly what `ENGINE` exists to avoid -- under it, every call is
already aimed at the named engine and nothing needs switching. For the same
reason, a run that names two different engines (`INSTANCE=a ENGINE=b`) is
refused before any work happens.

## Agent skills

The steps above reach a human through make; the same surface reaches an
AI agent through the `gd-` skill roster shipped in this checkout. Every
skill lives in one canonical place, `.agents/skills/<name>/SKILL.md`,
prefixed `gd-` so the names are agent-agnostic, and drives the same
targets this runbook states -- asking for what it needs, verifying each
step, and refusing to guess. The roster, its families and its coverage
guarantee are [skills.md](skills.md).

Inside this checkout both supported agents already see the roster:
opencode reads `.agents/skills` natively, and Claude Code reads it
through the plugin's tracked `skills/` symlink -- nothing to install. To
reach the roster from an agent launched outside this checkout, wire it
once on this Mac:

```sh
make skills-install                            # both agents, global scope
make skills-install AGENT=opencode             # opencode only
make skills-install AGENT=claude SCOPE=global  # Claude Code, global scope
```

`SCOPE=global` (the default) creates one symlink named
`general-dev-skills` in the agent's user-level skill directory, pointing
at this checkout. `make skills-remove` deletes only a link resolving
inside this repository, so your personal skills are never touched, and
`make skills-list` reports the state per agent and scope. The project
and runtime scopes are described with the full AGENT x SCOPE matrix in
[skills.md](skills.md)'s "Installing for agents" section.

## Troubleshooting

**Ruff or Python logs `spawn .venv/bin/python ENOENT`.** Symptom: the
container's Ruff log shows the error and falls back to its bundled binary.
Cause: a host-OS virtual environment created inside the workspace -- the
directory is bind-mounted into the Linux container as-is, and the
extensions resolve `.venv/bin/python` whose symlinks point at the macOS
Python. The repository's Makefile already redirects uv's project
environment outside the workspace (`UV_PROJECT_ENVIRONMENT` in the
Makefile), so this only happens after a bare `uv run` in the repository
root. Fix: delete the `.venv` directory and run the work through the
Makefile (for example `make test`) instead of a bare `uv run`.

**An AWS credential expired.** Symptom: opening a shell prints
`notice: <NAME> expired; refresh with: make push-creds` on stderr, and the
AWS variables are absent from that shell (the fragment's guard exports
nothing past expiry rather than exporting stale values). Fix: re-login the
host's session if it lapsed (`aws sso login --profile <profile>`), then
run `make push-creds` -- or `make up`, which pushes as part of its normal
work -- and open a new shell.

**A keychain item is missing.** Symptom: `make build` or
`make push-creds` aborts with `cannot resolve <NAME> from the keychain
source`, naming the keychain service and the command that failed; because
the push is the build's last step, the container is otherwise complete and
no rebuild is needed. Fix: `make creds-init` stores the item (the error
message says so), then re-run `make push-creds`. An item that exists with
an empty password counts as missing; creds-init stores over it.

**The scanner blocked a commit.** Symptom: pre-commit (or
`make lint-secrets`) fails with a finding naming a hostcreds credential
-- `make lint-secrets` compares every scanned line against the real values
resolved from this machine's manifest, so a pasted fragment or an
accidentally committed value is caught even when it matches no generic
pattern. The finding never prints the value. Fix: remove the value from
what is being committed; it already lives in the keychain and travels by
push, so nothing needs it in git. There is no ignore list and no
suppression annotation: a finding is either real, and fixed, or a
suspected false positive requiring human review.

**A deploy refuses with "this plan replaces or destroys resources".**
Symptom: `make instance-deploy INSTANCE=<name>` stops before applying and
prints the offending plan lines (`must be replaced`, or a `Plan:` line
counting resources to destroy). Cause: an edit to the per-instance file
forces Terraform to replace a resource that exists. What dies if you
proceed: the current EC2 instance and its volumes, and with them every
container on that engine and the cloned checkout in its volume -- unpushed
work goes with it. Deploy converges the fresh instance it creates, but it
cannot bring data back. Fix: push anything worth keeping from the container
first (`make exec`), then re-run deliberately with
`make instance-deploy INSTANCE=<name> CONFIRM=replace`. A plan that only
changes resources in place never reaches this guard.

**`make instance-destroy ALL=1` refuses.** Symptom: exit 1 with
"ALL=1 destroys every configured instance; confirm it". Cause: the
fleet-wide form destroys every instance under `remote-instances/` and
demands an explicit confirmation for exactly that reason. Fix: if a fleet
teardown is meant, `make instance-destroy ALL=1 CONFIRM=destroy`; to end
one instance, `make instance-destroy INSTANCE=<name>`, which needs no
confirmation.

**A remote target reports the engine unreachable, or no recorded id.**
Symptom: `make connect` or any docker call against the remote context fails
with a connection diagnosis, or names a missing recorded id. Cause: the SSM
port forward is not open -- it lives in the terminal that ran
`make remote INSTANCE=<name>` and dies when that terminal is interrupted,
when the laptop sleeps, or when the SSO session lapses. Fix: open or
refresh the forward for that instance (`make remote INSTANCE=<name>`, after
`aws sso login --profile <profile>` if the session expired), then retry.
If the failure instead names a missing recorded id, record it with
`make instance-link INSTANCE=<name> INSTANCE_ID=<id>` -- or re-run
`make instance-deploy INSTANCE=<name>`, which records the id
automatically. `make list-instances` shows the forward port each
instance's context records.
