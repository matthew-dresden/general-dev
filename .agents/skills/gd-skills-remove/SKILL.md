---
name: gd-skills-remove
description: Removes a skill from an agent's surface by unwiring it -- deleting the symlink entry or the agent-specific registration, never editing the canonical .agents/skills copy -- and verifies the removal by confirming the name no longer resolves while the canonical SKILL.md still exists; the make target it will drive lands in U3.
---

# gd-skills-remove

Removal in this roster means unwiring, never deleting canonical content: the
`SKILL.md` under `.agents/skills/<name>/` is the one copy, and an agent that
should no longer see a skill loses its route to it, not the document. This
skill states which of the two it is doing at every step, because the failure
mode it exists to prevent -- a removal request silently taking out the
canonical file every other agent still uses -- is unrecoverable from git
history alone in any surface that caches.

## Invocation map

| Step | What this skill runs | Notes |
|---|---|---|
| Confirm scope | None | Name the skill, the agent surface, and what will remain (the canonical file) before acting. |
| Unwire | The `skills-remove` make target the U3 work unit adds (forthcoming) | Until it lands, this skill states the exact unwiring change for the operator to apply, then verifies. |
| Verify | None (read) | The name no longer resolves through the agent's surface, and `.agents/skills/<name>/SKILL.md` still exists. |

## Checks

| Check | Prevents | Failure message | Remedy |
|---|---|---|---|
| the canonical copy survives the removal | an agent-scoped request deleting the one copy every agent shares | `.agents/skills/<name>/SKILL.md` is missing after the run | Restore it with `git restore .agents/skills/<name>` and stop; the removal was mis-scoped and is reported as such. |
| the name no longer resolves in the target surface | reporting a removal that left the agent still invoking the skill | the agent's surface still resolves the name | Re-apply the unwiring step and re-verify; never report success from the unwiring command's exit code alone. |

## Failure semantics

| Condition | Behavior |
|---|---|
| The skill is not wired into the target surface | Report that there is nothing to remove; do not create then remove an entry to make the operation non-empty. |
| An action the skill took did not verify | Re-run both `## Checks` rows and report the disagreement; never a silent retry. |
| A gate is reached (Section 4.4) | This skill reaches no Section 4.4 gate: it touches no AWS resource and no docker state. |
| The operator aborts | The canonical roster is untouched by construction; the surface is left exactly as found unless the unwiring already applied, which is then reported. |

## Related skills

- `gd-skills-install` and `gd-skills-scope`: the rest of this family.
- `gd-help`: verifies at a glance which skills an agent still resolves.
