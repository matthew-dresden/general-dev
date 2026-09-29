---
name: gd-skills-scope
description: Decides and records which skill families an agent surface receives -- env-, project-, instance-, container-, creds-, cert-, skills- or the gd-quality and gd-help index -- by writing agent-surface configuration, never by moving or deleting canonical files, and verifies the scoped set resolves; the make target it will drive lands in U3.
---

# gd-skills-scope

The roster is organized in families (see `gd-help`): a laptop-repair agent
needs the env- and creds- families but not instance-; a fleet-operations
agent needs instance- and container- but not skills-. This skill turns such
a statement into agent-surface configuration -- which entries the agent's
skill directory or manifest exposes -- while the canonical home stays
complete. Scoping is a property of the consumer, so it is recorded where
the consumer reads it, never by deleting from `.agents/skills`.

## Invocation map

| Step | What this skill runs | Notes |
|---|---|---|
| Collect the scope | None | Ask which families (and named exceptions) the surface should receive; an unclear answer is re-asked, never defaulted to "everything". |
| Apply | The `skills-scope` make target the U3 work unit adds (forthcoming) | Until it lands, this skill states the exact surface configuration for the operator to apply, then verifies. |
| Verify | None (read) | Every in-scope name resolves through the surface; every out-of-scope name does not; the canonical home still holds all of them. |

## Checks

| Check | Prevents | Failure message | Remedy |
|---|---|---|---|
| the scoped set matches the stated families exactly | an agent silently gaining destructive skills it was never meant to see | the surface resolves a name outside the stated scope | Re-apply the scoping configuration and re-verify; report the extra names rather than proceeding. |
| the canonical home is untouched | scoping one agent by deleting from the shared roster | `.agents/skills` no longer holds every roster name | `git restore .agents/skills` and stop; scoping never removes canonical files. |

## Failure semantics

| Condition | Behavior |
|---|---|
| The scope statement is ambiguous | Re-ask with the family list from `gd-help` spelled out; never interpret "everything" into a destructive family the operator did not name. |
| An action the skill took did not verify | Re-run the resolution check over the full roster and report each disagreement; never a silent retry. |
| A gate is reached (Section 4.4) | This skill reaches no Section 4.4 gate: it writes agent-surface configuration only. |
| The operator aborts | The canonical home is untouched by construction; any applied surface configuration is reported as left in place. |

## Related skills

- `gd-skills-install` and `gd-skills-remove`: wiring and unwiring, which
  scoping composes.
- `gd-help`: the family map this skill's scope statements use.
