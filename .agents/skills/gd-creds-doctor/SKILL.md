---
name: gd-creds-doctor
description: Verifies the credential path end to end -- the hostcreds manifest loads, every keychain item it names exists, every entry reached the container, and every stored value rotates on request -- reporting each finding with the exact make target that fixes it and performing no repair of its own.
---

# gd-creds-doctor

This skill is the creds family's standing read: the same four secrets checks
`gd-env-doctor`'s `## Findings` defines, run on demand rather than as part of
a full environment report, plus the one thing a doctor-shaped skill does not
do -- driving the repair to green by naming the exact remediation target for
each finding. It reports and repairs only through the make targets below; it
never reads a credential value (presence probes only, `>/dev/null`), never
edits the manifest, and never pushes a partial manifest.

The check definitions are deliberately not restated here: `gd-env-doctor`'s
secrets group owns their exact sources and remedies, and two copies would
drift. This skill invokes that group's checks as its own first section and
adds the repair loop.

## Invocation map

| Step | What this skill runs | Notes |
|---|---|---|
| Read | The four secrets checks of `gd-env-doctor`'s `## Findings`, run for this checkout | Manifest load, keychain presence per entry, container delivery via `make verify-container`, name validity. |
| Repair | `make creds-init` for missing items; `gd-creds-rotate` for stale values; `make push-creds` and `make verify-container` after any repair | One repair per finding, re-reading after each; the manifest is the source of truth for what must resolve. |

## Checks

| Check | Prevents | Failure message | Remedy |
|---|---|---|---|
| every finding from the read step is either green or repaired | reporting a credential path healthy while a finding stands | the closing re-read still reports a finding | Name the finding, the target that fixes it, and stop; never report a clean bill from the pre-repair read. |
| no repair widened the manifest | a "fix" that adds entries the operator never asked for | the manifest changed during the run | Restore the manifest (it is the operator's file; `git diff` shows no tracked change, so report the edit and let the operator decide) and re-run the read. |

## Failure semantics

| Condition | Behavior |
|---|---|
| The manifest is missing or malformed | `make init` creates a missing one; a malformed one fails with a `ManifestError` naming every problem at once. Fix the manifest, then re-run the read; never patch around it. |
| A step needs the operator (SSO, Touch ID, the value itself) | State the exact need and wait; values are entered by the operator, never into this conversation. |
| An action the skill took did not verify | Re-run `make verify-container` and the presence probes; report the step and the failing verification, never a silent retry. |
| A gate is reached (Section 4.4) | This skill reaches no Section 4.4 gate: it writes no AWS resource and runs no Terragrunt command. |
| The operator aborts | Repairs already applied stay applied and are reported as such; the closing re-read is the record of what state was left. |

## Related skills

- `gd-env-doctor`: owns the four check definitions this skill runs by
  reference.
- `gd-creds-setup` and `gd-creds-rotate`: the add and rotate operations the
  repair loop names.
