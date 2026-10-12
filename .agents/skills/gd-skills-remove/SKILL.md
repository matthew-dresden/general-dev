---
name: gd-skills-remove
description: Removes a skill from an agent's surface by unwiring it -- deleting the symlink entry or the agent-specific registration, never editing the canonical copy -- and verifies the removal by confirming the name no longer resolves while the canonical SKILL.md still exists; drives the make skills-remove target.
---

# gd-skills-remove

Removal in this roster means unwiring, never deleting canonical content: the
`SKILL.md` under `.agents/skills/<name>/` is the one copy, and an agent that
should no longer see a skill loses its route to it, not the document. This
skill states which of the two it is doing at every step, because the failure
mode it exists to prevent -- a removal request silently taking out the
canonical file every other agent still uses -- is unrecoverable from git
history alone in any surface that caches.

The `make skills-remove` target is native-Mac: it deletes filesystem
pointers on this machine and touches no devcontainer, docker or AWS state.
It deletes ONLY a symlink named `general-dev-skills` whose resolved target
is inside this repository. A non-symlink at that name is refused, a symlink
resolving elsewhere (your own skills, such as aws-secrets) is refused and
left untouched, and sibling entries are never examined.

## Invocation map

| Step | What this skill runs | Notes |
|---|---|---|
| Confirm scope | None | Name the agent surface (`AGENT=opencode|claude|both`) and the scope (`SCOPE=global|project|runtime`), and state what will remain (the canonical file) before acting. `make skills-list` reports the current state first. |
| Unwire | `make skills-remove AGENT=<agent> SCOPE=<scope>` | `SCOPE=global` deletes the `general-dev-skills` symlink from the agent's user-level skill directory; `SCOPE=project` and `SCOPE=runtime` remove nothing and say why (tracked repository content; an incantation leaves no state). |
| Verify | None (read) | The route no longer resolves through the agent's surface, and `.agents/skills/<name>/SKILL.md` still exists. |

## Checks

| Check | Prevents | Failure message | Remedy |
|---|---|---|---|
| the canonical copy survives the removal | an agent-scoped request deleting the one copy every agent shares | `.agents/skills/<name>/SKILL.md` is missing after the run | Restore it with `git restore .agents/skills/<name>` and stop; the removal was mis-scoped and is reported as such. |
| the name no longer resolves in the target surface | reporting a removal that left the agent still invoking the skill | the agent's surface still resolves the name | Re-apply the unwiring step and re-verify; never report success from the unwiring command's exit code alone. |

## Failure semantics

| Condition | Behavior |
|---|---|
| The skill is not wired into the target surface | Report that there is nothing to remove; do not create then remove an entry to make the operation non-empty. The target says so: `not installed (<path> absent)`. |
| The entry at the link name is not ours | The target refuses and names the path: a non-symlink is never deleted, and a symlink resolving outside this repository is left untouched. Move or delete it by hand only if it is genuinely yours. |
| An action the skill took did not verify | Re-run both `## Checks` rows and report the disagreement; never a silent retry. |
| A gate is reached (Section 4.4) | This skill reaches no Section 4.4 gate: it touches no AWS resource and no docker state. |
| The operator aborts | The canonical roster is untouched by construction; the surface is left exactly as found unless the unwiring already applied, which is then reported. |

## Related skills

- `gd-skills-install` and `gd-skills-scope`: the rest of this family.
- `gd-help`: verifies at a glance which skills an agent still resolves.
