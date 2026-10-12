---
name: gd-creds-setup
description: Manages host credentials through the hostcreds manifest and the make targets that serve it -- add, list, rotate, delete, and deliver into the container -- never a value on a command line, never a value rendered into the conversation, and never a second path to the keychain; ends every store or delete with a confirmation naming every credential affected.
---

# gd-creds-setup

Credentials do not live in `shell.env` and are not read from Parameter
Store by the container. Each one is named in the hostcreds manifest
(`.devcontainer/hostcreds.map.json`, created by `make init` from its
committed example, gitignored) together with the host source that holds
it -- the macOS keychain, git's own credential helper, or `aws configure
export-credentials` -- and `make push-creds` resolves every entry on the
host and pushes it into the container as one `<NAME>.env` fragment under
`~/.hostcreds/`, which shell startup sources. The container never
resolves anything itself (`devcontainer_config.hostcreds` owns the
manifest contract, the three resolvers and the renderers; `cli.py`'s
`creds-init`, `creds-fragments` and `shell-block` subcommands are their
CLI half), and this skill never builds a second path around them.

What the make targets cannot decide on their own is the part this skill
supplies: which source a new credential belongs to, whether a name is
even a candidate before anything is stored, and that a store is followed
by an independent verification -- a re-read of the keychain item --
never a store trusted on the command's own exit code. That last point is
this skill's one addition to Section 4.2.2's own interaction contract --
"Nothing is assumed to have worked" -- applied to `make creds-init`:
`stored: <NAME>` on stdout is the command's own claim about what
happened, and every write-shaped row in `## Invocation map` below re-probes
the keychain afterward rather than repeating that claim as this skill's
own.

The one constraint every operation obeys without exception is `## Value
handling`: a value is never placed in a command's arguments, never
printed by this skill into the conversation, and a request to see one is
answered with the exact command the operator runs in their own terminal
instead.

## Invocation map

| Operation | What this skill runs | Confirmation | Verification |
|---|---|---|---|
| Add | Add the entry to the manifest (name = the environment variable the container should export, source one of `keychain`/`git`/`aws-export`, plus its labels), then `make creds-init`, answering the prompt for the new name, then `make push-creds`. | `creds-init`'s own `stored: <NAME>` line, then push-creds' push output naming the credential. | `make verify-container` (or `security find-generic-password -w -s <service>` by hand) confirms the item exists and the container exports it; a store is never trusted from `stored:` alone. |
| List | Read the manifest itself: `jq 'keys' .devcontainer/hostcreds.map.json` (or `python3 -m json.tool` and read the keys). | None: nothing was written. The manifest is the entire list -- names and sources only; it holds no values. | None: a read has nothing to verify against a copy of itself. |
| Rotate | `security delete-generic-password -s <service>` for the keychain item (`creds-init` only prompts for items that do not exist yet), then `make creds-init` to store the new value, then `make push-creds`. For a `git`-source credential the value rotates in the host's own credential helper; for `aws-export` the SSO session refreshes it. | The `deleted:` line `security` prints, then `creds-init`'s `stored: <NAME>` line. | The post-store re-probe `make creds-init` performs itself (it re-probes and fails the run if the item does not read back), plus `make verify-container` after the push. |
| Delete | Remove the entry from the manifest, then `security delete-generic-password -s <service>` for the keychain item it named. | The `deleted:` line `security` prints. | `security find-generic-password -s <service>` exits non-zero for the absent item; the manifest no longer names it. |
| Deliver / re-push | `make push-creds`, then `make verify-container`. | push-creds' own output naming every credential resolved and pushed. | `make verify-container` re-checks fragment modes, the startup block, and git/aws reachability inside the container. |

The keychain `service` label defaults to `devcontainer/<repo-dir>/<NAME>`
(`hostcreds.KEYCHAIN_SERVICE_PREFIX`), so one project's items cannot
collide with another's; the manifest's optional `service`/`account`
labels override it, and both `creds-init` and the push resolve through
the same builder (`hostcreds.keychain_find_argv`), so a probe can never
address a different item than the store wrote.

## Value handling

1. **A value is never placed in a command's arguments.** `make creds-init`
   prompts via getpass, and `make creds-init CREDS_INIT_ARGS='--stdin
   <NAME>'` reads one value from stdin; both paths store it through
   `security -i`, with the value riding the stdin document, never argv.
   This skill never composes a command with the value as a word.
2. **This skill never renders a secret value into the conversation.** A
   request to display a value is answered with the exact
   `security find-generic-password -w -s <service>` command for the
   operator to run in their own terminal, naming the credential and never
   the value -- in a refusal, a log line or a summary alike.
3. **The manifest never holds a value.** Adding a credential means adding
   a name, a source and labels -- nothing else -- and this skill never
   writes a value into any file.

## Names

A manifest entry's name must be a valid environment-variable identifier,
because the credential arrives in the container as an environment
variable under that exact name (`hostcreds` rejects an invalid name at
`load_manifest` with a `ManifestError` naming the entry). This skill
never offers to normalize an invalid name into a valid one: a silently
renamed credential is not the credential the operator asked for, and the
remedy is always "rename it and retry."

## Failure semantics

| Condition | Behavior |
|---|---|
| The manifest is missing or malformed | `make init` creates a missing manifest from the committed example; a malformed one fails every hostcreds command with a `ManifestError` naming the path and every problem found at once. Fix the manifest, then re-run the operation. |
| A keychain item is missing | `make creds-init` prompts once per missing item, stores it, and re-probes; a store that does not read back fails the run naming the credential. |
| A credential cannot be resolved at push time | `make push-creds` fails the whole push naming the credential and its remedy: a container is never shipped a subset of the manifest (no fallback, no skip-and-continue). |
| A step needs the operator (SSO, Touch ID) | An `aws-export` entry needs the developer's already-valid AWS SSO session (`aws sso login` refreshes it); the keychain may prompt for access. This skill waits for the operator to resolve it, then re-runs the identical command that failed -- never retrying silently. |
| An action the skill took did not verify | Every write-shaped row above re-reads the keychain or re-runs `make verify-container` rather than trusting a command's exit code. When that verification disagrees, this skill reports the action, the verification that failed and the state it left, and does not report the operation as complete. |
| A gate is reached (Section 4.4) | This skill reaches no Section 4.4 gate: it writes no AWS resource and runs no `terragrunt apply` or `terragrunt destroy`. |
| The operator aborts | Nothing is stored until a prompt is answered and `creds-init` has completed; an abort leaves the manifest, the keychain and the container exactly as they were. |

## Related specifications

- Section 2, G4: agents reach credentials without any of them touching
  the container's disk as configuration -- the fragment files under
  `~/.hostcreds/` are the container's only copy, written by push-creds.
- Section 4.2: `gd-creds-setup`'s own roster row in `docs/skills.md`, and
  the interaction contract every skill obeys.
- `docs/environment-files.md`'s "Host credentials (hostcreds)" section:
  the operator-facing reference for the manifest, `make creds-init`,
  `make push-creds` and `make verify-container`.
