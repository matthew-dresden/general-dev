---
name: gd-project-onboard
description: Takes a fresh clone of this repository to ready-for-setup -- creates the four gitignored configuration files from their committed examples with make init, binds Shift+Enter with make keybindings, and ends by naming the env-setup skill for the chosen backend; configures nothing else itself.
---

# gd-project-onboard

This skill is the first run on a new checkout: it creates the private
configuration files every later skill reads, and nothing more. It decides
nothing about what those files contain -- `render` and `verify`
(`.claude/plugins/devcontainer/scripts/devcontainer_config/`) own the file
contracts, and `make init` performs the copy from the committed `.example`
files, refusing to overwrite anything already on disk. When a file already
exists, this skill reports that fact and moves on; it never merges, edits or
re-renders an existing file -- re-rendering is `gd-env-setup-local`'s and
`gd-env-setup-remote`'s concern, with the existing files moved aside first.

## Invocation map

| Step | What this skill runs | Notes |
|---|---|---|
| Create the private files | `make init` | Creates `shell.env`, `devcontainer-environment-variables.json`, `.devcontainer/aws-profile-map.json` and the hostcreds manifest `.devcontainer/hostcreds.map.json` from their examples. Never overwrites an existing file. |
| Bind Shift+Enter | `make keybindings` | Host-only; must run on this machine, not in a container. This step is the `flow: keybindings setup` flow of `docs/skills.md`'s Flows section. |
| Name the next skill | None | Ends by naming `gd-env-setup-local` (this machine is the engine) or `gd-env-setup-remote` (a remote instance is the engine), per which backend the operator means to use. This skill runs neither. |

## Checks

| Check | Prevents | Failure message | Remedy |
|---|---|---|---|
| the four private files exist after `make init` | every later skill that reads them failing on an absent file | one of the four paths `make init` creates is missing after the run | Re-run `make init` and report its output verbatim; if it exits non-zero, stop and name the error rather than creating any file by hand. |
| no private file is tracked by git | identity or credential material entering version control | `git ls-files` lists one of the four paths | Untrack it (`git rm --cached <path>`) exactly as `make lint-private`'s own failure output names; never add it to a gitignore exception. |

## Failure semantics

| Condition | Behavior |
|---|---|
| `make init` or `make keybindings` exits non-zero | Stop, print the target's own output, and name the remedy it states. Never create or patch a file by hand to get past the failure. |
| An action the skill took did not verify | Re-run the check that failed (`## Checks`) and report the result; never report onboarding complete from the target's exit code alone. |
| The operator aborts | Leaves no partial state beyond the files `make init` already wrote; a re-run of this skill is idempotent, since `make init` never overwrites. |

## Related skills

- `gd-env-setup-local` and `gd-env-setup-remote`: the interviews this skill
  hands off to; they own what the files contain.
- `gd-creds-setup`: the manifest `make init` creates is the manifest that
  skill operates on.
