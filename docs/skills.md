# Skills

The canonical skills home is `.agents/skills/<name>/SKILL.md`. Every skill
name is prefixed `gd-`, and cross-references between skills use the bare
`gd-<name>` form, so the roster is agent-agnostic: any agent that can read
this repository can resolve every name. Claude Code consumes the same
roster through the plugin's `skills/` entry,
`.claude/plugins/devcontainer/skills`, which is a relative symlink to
`../../../.agents/skills` -- a pointer, never a copy -- so each skill is
also reachable as the Claude Code invocation `/devcontainer:gd-<name>`.

The table below is the skill roster: one row per skill directory under
`.agents/skills/`. `tests/test_skill_lint.py`'s `check_plugin` asserts the
row set and the directory set are equal, so the table cannot drift from
what is actually installed, and enforces the rest of the structural
contract in the same run:

- `plugin.json`'s `name` equals the plugin directory name, and
  `marketplace.json` has exactly one `plugins` entry whose `source` resolves
  to that same directory.
- `.claude/settings.json` registers the plugin directory as a `directory`
  marketplace and enables it in `enabledPlugins`.
- Every `SKILL.md` opens with a `---`-delimited frontmatter block holding a
  `name` that matches its directory and the `^gd-[a-z0-9]+(-[a-z0-9]+)*$`
  pattern, and a non-empty `description` no longer than 500 characters
  (opencode additionally caps descriptions at 1024; both limits are
  enforced).
- Every skill that declares `Interview backend: <backend>` restricts
  `<backend>` to one of `answers.BACKENDS`, and gives a `## Questions`
  table with header `| Field | Prompt |` whose `Field` column, as a set,
  equals `answers.required_fields({"backend": <backend>,
  "aws_config_enabled": True, "host_proxy": True})`.
- Every skill's `## Checks` table, when its header line matches exactly
  `| Check | Prevents | Failure message | Remedy |`, must have a unique
  `Check` name, a unique `Failure message`, and a non-empty `Remedy` in
  every row.
- Every bare `gd-<name>` reference, in any `SKILL.md`, in this document, or
  in `docs/devcontainer.md`, names a skill present in the roster below.

The families in the table's second column are the index `gd-help` maps in
prose.

| Skill | Invocation |
|---|---|
| gd-env-setup-local | `gd-env-setup-local` (env-) -- asks the local backend's required answers (Section 5.1), writes and verifies the three private files, checks prerequisites, and ends by naming `make build` |
| gd-env-setup-remote | `gd-env-setup-remote` (env-) -- asks the local backend's required answers plus instance name, id, region and profile (Section 5.1), verifies the SSO session and instance state, gates any Terragrunt apply behind PRECHECK-APPLY, issues certificates, creates the docker context and port forward, and ends by naming `make build INSTANCE=<name>` |
| gd-env-doctor | `gd-env-doctor` (env-) -- asks nothing, delegates the thirteen `gd-instance-list` checks by reference rather than restating them, and reports every configuration, secrets, container-state and drift finding it does not cover, each with an exact remedy; it only reports, it never repairs anything itself |
| gd-project-onboard | `gd-project-onboard` (project-) -- takes a fresh clone to ready-for-setup with `make init` and `make keybindings`, and ends by naming the env- setup skill for the chosen backend |
| gd-instance-create | `gd-instance-create` (instance-) -- provisions one instance through `make instance-init`, `make instance-plan` and `make instance-deploy` behind PRECHECK-APPLY, linking the id with `make instance-link` |
| gd-instance-fleet | `gd-instance-fleet` (instance-) -- runs status, stop, start and (behind its own confirmation) destroy across every configured instance through the `ALL=1` targets, reporting a per-instance result table |
| gd-instance-list | `gd-instance-list` (instance-) -- defines the validation contract the other skills reuse: the thirteen checks of Section 4.2.1 (six local, seven remote), asking which instance only when Section 4.1.1 resolution is ambiguous, fixing only what is reversible and needs no operator credential (selecting an existing context, re-establishing a port forward), and ending in a per-check verdict table |
| gd-instance-destroy | `gd-instance-destroy` (instance-) -- destroys one instance behind GATE-DESTROY, recording the two-part precheck (operator confirmation, and the volume verified free of unpushed work before the confirmation is requested), then verifying every derived artifact is gone |
| gd-container-local | `gd-container-local` (container-) -- asks nothing, delegates engine reachability (and with it the `rdc_backend` local-against-remote selection) to `gd-instance-list`, then resolves the container itself through `rdc_container_ids` and `rdc_require_container` and picks `make build`, `make start`, `make restart` or `make reopen`, verifying by re-reading state after every action; it never destroys anything and ends with a running container |
| gd-container-remote | `gd-container-remote` (container-) -- the remote twin of `gd-container-local`: refreshes the SSM port forward with `make connect`, selects the engine with `make remote`, then builds or resumes through the same state diagnosis against the cloned checkout in the instance's volume |
| gd-container-verify | `gd-container-verify` (container-) -- read-only state verification through `make status` and `make check`, flagging uncommitted or unpushed work in a remote volume and naming the remedy without repairing |
| gd-container-lifecycle | `gd-container-lifecycle` (container-) -- asks for confirmation, always, taken against an inventory of what `make clean` or `make rebuild` will destroy (the container, its private volumes, its image) and what will survive (shared volumes, the base image); explains the unpushed-work and uncommitted-config guards rather than only enforcing them, never sets `FORCE` itself, destroys container state only (never an instance, which stays behind `GATE-DESTROY`), and ends by reporting what was destroyed and what survived from a fresh post-operation read |
| gd-creds-setup | `gd-creds-setup` (creds-) -- asks which credential, manages the hostcreds manifest and the keychain items it names entirely through `make creds-init` and `make push-creds` (never a value on a command line, never a value rendered into the conversation), verifies every store by re-reading the keychain rather than trusting the command's own exit code, and ends by naming every credential affected and what now holds it |
| gd-creds-rotate | `gd-creds-rotate` (creds-) -- replaces one stored credential: deletes the old keychain item, stores the replacement through `make creds-init`, re-pushes, and verifies by re-reading the keychain and the container |
| gd-creds-doctor | `gd-creds-doctor` (creds-) -- verifies the credential path end to end by running `gd-env-doctor`'s secrets checks on demand and driving each finding's repair through the creds- make targets |
| gd-cert-lifecycle | `gd-cert-lifecycle` (cert-) -- asks which instance, creates the CA and issues the server and client certificates on first use, rotates the client certificate with the instance left running, reports expiry (the `make cert-status` view), states that certificate revocation does not exist and that removing the principal's `ssm:StartSession` grant is the mechanism, and ends every material-changing operation by rewriting the docker context and completing a handshake before reporting success |
| gd-skills-install | `gd-skills-install` (skills-) -- wires an agent's skill surface to the canonical `.agents/skills` home by symlink, never a copy, and verifies every `gd-*` name resolves through the agent; drives the U3 `skills-install` make target (forthcoming) |
| gd-skills-remove | `gd-skills-remove` (skills-) -- removes a skill from one agent's surface by unwiring, with the canonical copy verified to survive; drives the U3 `skills-remove` make target (forthcoming) |
| gd-skills-scope | `gd-skills-scope` (skills-) -- records which skill families an agent surface receives, in that surface's own configuration, never by deleting from the canonical home; drives the U3 `skills-scope` make target (forthcoming) |
| gd-quality | `gd-quality` (cross-cutting) -- asks nothing, reads the sub-target set `make validate` invokes from the Makefile itself rather than a copy embedded in the skill, interprets each failing sub-target's root cause and fixes it, never suppresses a finding (no bypass annotation, no linter-ignore entry, no raised threshold, no narrowed `LINT_EXCLUDES` or `SPELL_FILES`), stops and asks for human approval on a suspected false positive, hands anything else it cannot fix to `gd-env-doctor` or the operator, and ends by reporting the exit code of a fresh `make validate` run |
| gd-help | `gd-help` (cross-cutting) -- the roster index: maps the seven families, names the one skill to invoke for a stated goal, and points at the make targets each family drives |

## Flows

Some recurring developer actions are not a single make target but a named
flow -- a judgment, an answer, or a multi-step procedure spanning several
targets. Each flow below is listed once here and referenced by name in
every covering skill's body as the backticked `` `flow: <name>` `` marker,
so the correspondence is exact in both directions:
`tests/test_skills_coverage.py` fails when a flow listed here is missing
from a covering skill's body, and when any skill references a flow this
section does not list.

| Flow | Covering skill(s) |
|---|---|
| `keybindings setup` | gd-project-onboard |
| `ENGINE multi-engine addressing` | gd-container-local gd-container-remote |
| `INSTANCE_ID link recovery` | gd-instance-create |
| `credential expiry troubleshooting` | gd-creds-doctor |
| `scanner-blocked-commit remedy` | gd-quality |
| `opencode/claude agent setup pointers` | gd-skills-install |

## Coverage guarantee

Coverage over the make-target surface is test-enforced.
`tests/test_skills_coverage.py` parses the live `make help` output and the
declared names in `tests/data/help-unadvertised.txt` -- the same two
sources `tests/test_help_snapshot.py` uses to account for every `.PHONY`
target -- and fails, naming each uncovered target, if any target in that
union is referenced by no `SKILL.md` under `.agents/skills`. The same
suite pins the roster's structure: every `SKILL.md` carries exactly one
`## Invocation map` heading, the one section where the make targets a
skill drives are declared. A make target added without a covering skill, a
skill body losing its invocation map, and a flow drifting on either side
of the table above each fail `make test`.
