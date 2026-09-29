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
7 on, the remote route is a different sequence -- certificates, Parameter
Store publication, the SSM port forward -- and it is documented once, in
the README's "Quick start, remote" section, with the certificate lifecycle
in [devcontainer.md](devcontainer.md)'s "Certificate lifecycle" section
and `make cert-status` reporting expiry. The credential push needs no
remote variant at all: it rides the active docker context, so after
`make remote` the very same `make push-creds` and `make build` steps
deliver the same fragments to the EC2 engine's container.

## Troubleshooting

**Ruff or Python logs `spawn .venv/bin/python ENOENT`.** Symptom: the
container's Ruff log shows the error and falls back to its bundled binary.
Cause: a host-OS virtual environment created inside the workspace -- the
directory is bind-mounted into the Linux container as-is, and the
extensions resolve `.venv/bin/python` whose symlinks point at the macOS
Python. The repository's make targets already redirect uv's project
environment outside the workspace (`UV_PROJECT_ENVIRONMENT` in the
Makefile), so this only happens after a bare `uv run` in the repository
root. Fix: delete the `.venv` directory and use the make targets.

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
