# opencode in the container

What opencode is here, which providers and MCP servers the committed config ships, how the agent harness is generated, how credentials stay alive, and how a new operator reaches a working session without a key ever being committed.

- This page documents the shipped surface only: every value it names lives in [.devcontainer/opencode.json](../.devcontainer/opencode.json) or [.devcontainer/opencode/](../.devcontainer/opencode/), and the wrapper environment variables are documented in [shell.env.example](../shell.env.example)'s "Model provider MCP wrapper values" section.

## What this configures

`configure_opencode` in [.devcontainer/.devcontainer.postcreate.sh](../.devcontainer/.devcontainer.postcreate.sh) installs opencode generically and resolves one neutral, committed config into the per-user one:

- `npm install --global opencode-ai` runs as the container user, with `g-whiz` installed beside it for the gcp MCP server.
- `~/.config/opencode/opencode.json` is written from the committed config in one `jq` pass: the `${WORK_DIR}` placeholder in the MCP wrapper commands becomes the real workspace root, and `enabled_providers` plus the `model` / `small_model` pair follow the toggle matrix below.
- The installed bytes are compared against the resolved bytes with `cmp`, so truncation or drift between resolution and install aborts the build instead of leaving a config opencode would silently misread.
- After the rewrite, a `jq -e` matrix assertion aborts the build when a resolved `model` or `small_model` id names a provider that is not in `enabled_providers`, or a model its provider block does not define.
- The opencode-level settings the committed config carries: `shell` zsh, `share` disabled, `subagent_depth` 3, `permission` allow, the three plugins, the `tool_output` caps (2000 lines, 51200 bytes) and the compaction block (`auto` true, `tail_turns` 40, `buffer` 100000).
- The aliases `ocd` (`opencode`) and `ocdr` (`opencode --resume`) land in the container's interactive shell rc.
- No vendor default ships: both providers are optional, the toggles choose which are available, and neither key is ever in the config.

## The providers

Two providers ship, each reading its key at request time through the `{env:...}` interpolation its block carries:

| Provider | id | Models | Key variable |
|---|---|---|---|
| z.ai max coding plan | `zai-max` | `glm-5.3`, `glm-5.3-flash`, `glm-5.2`, `glm-5-turbo`, `glm-4.7` | `ZAI_API_KEY` |
| openrouter.ai | `openrouter` | `glm-5.3`, `glm-5.3-flash`, `gemini-3.8-flash`, `claude-opus-5` | `OPENROUTER_API_KEY` |

Toggles:

- `ZAI_ENABLED` and `OPENROUTER_ENABLED` are read from the host shell at build time through `devcontainer.json`'s `containerEnv` block, which uses the colon form for reference (`${localEnv:ZAI_ENABLED:1}`): the value after the colon is the default, so both providers are on unless you export `0` in your shell before `make build`.
- Only `0` and `1` are accepted; anything else aborts the build naming the toggle and where it is set.
- Each toggle resolves `enabled_providers` and the default models per this matrix:
  - Both on: both providers, `model` `zai-max/glm-5.3`, `small_model` `zai-max/glm-5.3-flash`.
  - `OPENROUTER_ENABLED=1` only: openrouter, `model` `openrouter/glm-5.3`, `small_model` `openrouter/glm-5.3-flash`.
  - `ZAI_ENABLED=1` only: zai-max with the shipped zai-max defaults.
  - Both off: the `model`, `small_model` and `enabled_providers` keys are removed entirely, which means no restriction: both shipped providers still load (each `{env:...}` key resolves to an empty string when its variable is unset) and opencode picks a default model outside the shipped blocks.

Keys:

- Both keys ride the gitignored hostcreds manifest like every credential: add an entry named after the variable to `.devcontainer/hostcreds.map.json`, run `make creds-init` to store it in the keychain, and `make push-creds` delivers the fragment whose startup block exports it, never committed and never in `shell.env`. The postcreate warns, never fatally, when a toggle is 1 and its key variable (`ZAI_API_KEY` or `OPENROUTER_API_KEY`) is absent from the environment, naming the toggle, the manifest fragment named after that variable, and the `make push-creds` remedy.

Limits and modalities:

- Every model block carries `limit.context` and `limit.output` in tokens, and opencode's context-percentage display computes against those numbers, so a wrong limit misstates the usage gauge and misfires compaction.
- The exact per-model values live in [.devcontainer/opencode.json](../.devcontainer/opencode.json), the single source the config, the generator and the tests all read.
- `modalities` names which inputs a model accepts: the two `glm-5.3-flash` blocks, `gemini-3.8-flash` and `claude-opus-5` carry image input with text output only, which is what the image-delegation rules in the agent harness point at.

## The MCP servers

Eight MCP servers ship in the committed config, one row each; the SSM-backed three (github, azure-devops and context7) read every organization-specific value from the environment at spawn time and fail fast naming the variable when one is missing:

| MCP | What it does | Credential source, where the value lives | Toggle or enable step |
|---|---|---|---|
| `playwright` | Browser automation for a session, isolated per launch | None | Ships enabled; disable per user with the jq override in [project-setup.local.sh.example](../.devcontainer/project-setup.local.sh.example) |
| `context7` | Library and framework documentation search | The SSM parameter `${SSM_CONTEXT7_PREFIX}/api-key`, read by the wrapper at spawn time (`SSM_CONTEXT7_PREFIX`, `AWS_REGION`) | Ships enabled; set the prefix and `AWS_REGION` in `shell.env` and rebuild |
| `aws-mcp` | The official AWS MCP: documentation search, API access, region checks | None of its own: it runs on the AWS identity the container exports (the SSO session or the static keys behind the `aws-export` fragment) | Ships enabled, pinned to the `mcp-proxy-for-aws` version and region in the command |
| `github` | GitHub through a GitHub App installation | The SSM parameters `${SSM_ORG_PREFIX}/github/github-app/app-id` and `.../private-key`; the wrapper mints a one-hour installation token (`SSM_ORG_PREFIX`, `AWS_REGION`, `GITHUB_APP_TARGET`) | Ships enabled; set the three variables in `shell.env` and rebuild |
| `azure-devops` | Azure DevOps work items, teams and iterations | The SSM parameter `${SSM_ORG_PREFIX}/azure-devops/opencode-pat`, used as BASIC auth for `ADO_USER` against `ADO_ORG` (`SSM_ORG_PREFIX`, `AWS_REGION`, `ADO_ORG`, `ADO_USER`) | Ships enabled; set the four variables in `shell.env` and rebuild |
| `azure` | Azure resource operations through the az CLI's MCP server | The az login state at `~/.azure`, created in the container | Ships enabled; run `az login` in the container |
| `gcp` | GCP resource operations through `g-whiz` (installed at postCreate) | The gcloud login state at `~/.config/gcloud`, created in the container | Ships enabled; run `gcloud auth login` in the container, then `gcloud config set project <project>` |
| `gws` | Google Workspace: Gmail, Drive, Sheets, Docs, Slides, Calendar, Tasks, Contacts | The gws CLI's encrypted OAuth state at `~/.config/gws` | Ships enabled; restore the state through [project-setup.local.sh](../.devcontainer/project-setup.local.sh.example), or run the gws CLI's browser auth in the container |

- SSO and static keys are both supported for the SSM-backed three: the wrappers read whatever value the host CLI resolves at the parameter path at spawn time, so the operator can keep a long-lived static value there or refresh it under the AWS SSO session, and only an expiring session needs the refresh chain.
- The postcreate probes each SSM parameter's name (never its decrypted value) and prints a provisioning-time warning naming the enable step on a miss, because opencode starts an MCP server lazily and a wrapper whose credential source is absent would otherwise fail mid-session instead.

### The exclusions

Two MCP servers are deliberately absent from the shipped config:

- `stripe`: it requires a Stripe account's secret key, an account-bound credential no committed value or parameter path here supplies.
- `image-gen`: it requires an image-capable provider key of its own, and no manifest entry or parameter path ships for it.
- Adding stripe back is one entry: add one `mcp` entry to the installed config through the per-user jq override (`.mcp.stripe = {...}` in project-setup.local.sh), and its key as a hostcreds manifest entry.

## The agent harness

The agent set is generated, never hand-written, from one source of truth:

- [.devcontainer/opencode/lib/generate-agents.mjs](../.devcontainer/opencode/lib/generate-agents.mjs) reads the installed config and [agents.inputs.json](../.devcontainer/opencode/lib/agents.inputs.json) (providers, roles, model tiers, optional concurrency caps) at postcreate, and writes the agent files into `~/.config/opencode/agent` plus the deterministic manifest `~/.config/opencode/lib/generated-agents.json`.
- It runs for every provider and model the inputs list (both shipped providers today); the `enabled_providers` resolution decides which providers opencode itself offers, not which agents exist.

The per-provider agent set, one entry per model:

- `<provider>-<model>`, the worker agent (mode subagent): executes delegated chunks of work, and may spawn same-provider sub-workers nested up to `subagent_depth` (3) levels.
- `build-<provider>-<model>`, the build driver (mode all): manages development and judgment work, and delegates per the tier.
  - A flagship driver does focused dev work directly, delegates parallelizable dev chunks to the same-provider flash worker, and delegates review, judgment and security checks to the full worker.
  - A flash driver delegates ALL work to its exact flash worker.
  - Delegation is fail-closed through the frontmatter task permissions: a flash driver is allowed only its worker, a flagship driver only same-provider workers.
- `plan-<provider>-<model>`, the planning agent (mode primary, read-only): file edits are denied, and bash is gated by the generator's read-only allowlist (state-changing commands and interpreter calls need explicit approval, and secret-value reads are deliberately not allowlisted).
  - A flagship plan agent may spawn both same-provider workers: the full worker for judgment and the flash worker for fast research.
  - A flash plan agent is locked to its exact flash worker.
- Image reading flows per the generator's rules: flash agents read images directly, and every other agent delegates the image to the same-provider flash worker.

Self-tests:

- Four suites ship beside the generator in [.devcontainer/opencode/tests](../.devcontainer/opencode/tests): `bash-allowlist.test.mjs`, `generated-agents.test.mjs`, `model-profiles.test.mjs` and `zai-quota-wait.test.mjs`.
- Run them from the installed config root, where the generator's own ROOT resolution points: `node tests/<suite>.test.mjs` (exit 0 is pass).
- `node lib/generate-agents.mjs --check` reports drift between the generated files and a fresh regeneration, and `--dry-run` prints the plan without writing anything.

## The keep-alive model

Credentials age at different rates, and the design splits the work between host and container:

- The host trio keeps credentials alive: the launchd SSO token refresh (`aws-sso-refresh`, every 30 minutes), the daily exercise of the other logins (`auth-keepalive`) and the catch-up re-auth (`mcp-auth`); these are machine-level, outside this repository's targets, and a host without them runs the refresh commands by hand when a notice names one.
- The container side reads the pushed AWS fragment's expiry: each `aws-export` fragment embeds the session's `Expiration`, and the startup guard compares it against the wall clock on every shell start, past expiry exporting nothing and printing `notice: <NAME> expired; refresh with: make push-creds`, the host-side remedy.
- SSO sessions age and need the chain: a fresh `aws sso login` on the host, then `make push-creds` (plus a rebuild, or `make connect-refresh` for a remote engine whose forward the expired session broke) restores the fragment.
- Static keys do not age: a keychain-stored API key or a static SSM parameter value resolves the same way at every spawn, with no refresh chain.

## The per-user seam

Two files, one committed and one not:

- [.devcontainer/project-setup.sh](../.devcontainer/project-setup.sh) is committed and runs at postCreate, after provisioning resolved the opencode config; it is log-only by default, and its comments are examples, never live values.
- [.devcontainer/project-setup.local.sh](../.devcontainer/project-setup.local.sh.example) is gitignored (create it with `cp .devcontainer/project-setup.local.sh.example .devcontainer/project-setup.local.sh`), and the postcreate sources it in the same environment context after project-setup.sh and after the opencode config is resolved, so everything there overrides provisioning.
- Both live in the workspace checkout, so they survive the container's lifetime: the local file re-runs on every rebuild, and you can re-run its commands by hand at any time without one.

What the seam is for, with the examples both files carry:

- Pin the session driver: rewrite `model` / `small_model` to another provider's pair with one jq line.
- Set the default agent: `default_agent` must name a generated agent, the set listed in `~/.config/opencode/lib/generated-agents.json`.
- Toggle one MCP server for this user only: rewrite `.mcp.<name>.enabled` with one jq line.
- Restore the gws state: copy a backup you keep outside the repository into `~/.config/gws`.
- Provider keys never appear here: they ride the hostcreds manifest, never the seam.

## Credential safety

The safety properties, each enforced rather than promised:

- The committed config carries only `{env:...}` interpolations: `zai-max` reads `{env:ZAI_API_KEY}` and `openrouter` reads `{env:OPENROUTER_API_KEY}`, and no literal key exists anywhere in git.
- The postcreate asserts it: the count of `apiKey` keys must equal the count of `{env:...}` interpolations, both shipped provider blocks must carry exactly the documented variable, and a violation aborts the build instead of installing.
- The live hostcreds manifest is gitignored (`make init` creates it from its committed example, and the lint refuses to let it be tracked), so neither a key value nor the live name list is committed.
- Fragments ride `docker exec` stdin at mode 0600, never a command line, and `make verify-container` re-checks the fragment modes, the startup block and silent shell startup inside the container.

A new operator's path, six commands:

```sh
make init             # create the four gitignored config files from examples
                      # (replace the placeholders; add a ZAI_API_KEY and/or an
                      # OPENROUTER_API_KEY entry per enabled provider)
make creds-init       # store each keychain credential the manifest names
make local            # point docker and VS Code at the local engine
make build            # build, run postCreate, push every credential
make exec             # a shell inside the container
ocd                   # open opencode
```
