---
name: gd-creds-rotate
description: Replaces one stored credential -- deletes the old keychain item, stores the replacement through make creds-init, re-pushes with make push-creds, and verifies by re-reading the keychain and re-running make verify-container; never accepts, renders or transmits a value on a command line.
---

# gd-creds-rotate

This skill is the rotate operation of the creds family, split out from
`gd-creds-setup` so the standing setup surface stays add-and-deliver while
rotation -- the operation with a destructive half -- has its own contract.
The value discipline is the one `gd-creds-setup`'s `## Value handling`
states, unchanged: a value is never placed in a command's arguments, never
rendered into the conversation, and a request to see one is answered with
the exact command the operator runs in their own terminal.

A `git`-source credential rotates in the host's own credential helper and an
`aws-export` credential in the SSO session; only a `keychain`-source
credential has a delete-and-restore path here, and this skill states which
case applies before touching anything.

## Invocation map

| Step | What this skill runs | Notes |
|---|---|---|
| Remove the old item | `security delete-generic-password -s <service>` | `creds-init` only prompts for items that do not exist yet, so the old item must go first. The `deleted:` line is the confirmation. |
| Store the replacement | `make creds-init` | Prompts via getpass; `CREDS_INIT_ARGS='--stdin <NAME>'` feeds one value from stdin. The value rides stdin, never argv. |
| Re-push | `make push-creds` | Fails the whole push naming any credential that cannot resolve; a container is never shipped a subset of the manifest. |
| Verify in the container | `make verify-container` | Fragment modes, the startup block, git and aws reachability. |

## Checks

| Check | Prevents | Failure message | Remedy |
|---|---|---|---|
| the old item is gone | a stale value silently surviving next to its replacement | `security find-generic-password -s <service>` still succeeds after the delete | Re-run the delete and report its output; never store the replacement while the old item reads back. |
| the new item reads back | reporting a store that did not take | the post-store re-probe `make creds-init` performs fails naming the credential | Re-run `make creds-init`; a store is never trusted from `stored:` output alone. |
| the container exports the rotated value | the rotation reaching the manifest and keychain but not the container | `make verify-container` reports the credential missing or unreachable | Fix per that target's own output, re-push, then re-verify; report the rotation incomplete until it passes. |

## Failure semantics

| Condition | Behavior |
|---|---|
| The old item cannot be deleted (never existed, access denied) | Report `security`'s own output; for a never-existed item, proceed straight to the store step and say so, rather than pretending a deletion happened. |
| A step needs the operator (Touch ID, SSO, the value itself) | State exactly what is needed and wait; the value is always entered by the operator, never requested into the conversation. |
| An action the skill took did not verify | Report the step, the verification that failed, and the state left (old item deleted, new item not confirmed); never report the rotation complete. |
| A gate is reached (Section 4.4) | This skill reaches no Section 4.4 gate: it writes no AWS resource and runs no Terragrunt command. |
| The operator aborts | Between the delete and the store the credential is temporarily absent; the abort is reported as exactly that state, with `make creds-init` as the resuming step. |

## Related skills

- `gd-creds-setup`: the manifest and delivery contract this skill operates
  within; its `## Value handling` applies here verbatim.
- `gd-creds-doctor`: the read-only verification this skill's final check
  reuses.
