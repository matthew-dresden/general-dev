---
name: gd-skills-install
description: Installs this roster's canonical skills into an agent's skill surface -- verifies the agent reads .agents/skills directly or wires the documented relative symlink, never copies files -- and verifies the install by resolving every gd-* name to its SKILL.md through the agent's own surface; the make target it will drive lands in U3.
---

# gd-skills-install

The canonical skills home is `.agents/skills/<name>/SKILL.md`; every name is
prefixed `gd-`. Claude Code consumes the roster through the plugin's
`skills/` entry, which is a relative symlink to `../../../.agents/skills`
rather than a copy, so there is exactly one copy of every skill body and no
surface can drift from the canonical set. This skill exists to reproduce
that wiring for any agent: point the agent's skill directory at the
canonical home, by symlink where the agent supports one, and verify by
resolving each roster name through the agent's own surface afterward.

This skill never copies or renders a skill body into another location: a
copied skill is a second source of truth, which is the failure this roster
layout exists to prevent.

The two agents this repository ships wiring for are the standing example of
the `flow: opencode/claude agent setup pointers` flow of `docs/skills.md`'s
Flows section: opencode reads `.agents/skills` directly (the canonical
home needs no pointer), and Claude Code consumes the roster through the
plugin's `skills/` relative symlink described above. Wiring any other
agent follows the same two rules this skill verifies -- point at the
canonical home, never copy it -- and resolves every `gd-` name afterward.

## Invocation map

| Step | What this skill runs | Notes |
|---|---|---|
| Discover the agent surface | None (read) | Identify where the target agent looks for skill directories. |
| Wire | The `skills-install` make target the U3 work unit adds (forthcoming) | This is the target this skill will drive once it lands; until then this skill performs the wiring by stating the exact symlink or configuration for the operator to apply, then verifying it. |
| Verify | None (read) | Resolve every `gd-*` name in the roster (`docs/skills.md`) to a `SKILL.md` through the agent's own surface; report any name that does not resolve. |

## Checks

| Check | Prevents | Failure message | Remedy |
|---|---|---|---|
| the agent's surface resolves to the canonical home | a second, diverging copy of the roster | the agent's skill path is a real directory with its own SKILL.md files | Replace the copy with the documented relative symlink (`../../../.agents/skills` from the plugin's skills path, or the agent's equivalent), then re-verify; never merge the two. |
| every roster name resolves through the agent | an agent silently missing skills the rest of the platform assumes | a roster name does not resolve in the agent's surface | Re-run the wiring step and re-verify; report the missing names rather than a partial success. |

## Failure semantics

| Condition | Behavior |
|---|---|
| The agent cannot follow a symlink | State that fact and stop; ask the operator to choose between the agent's own indirection mechanism and not installing. Never fall back to copying. |
| An action the skill took did not verify | Re-run the resolution check and report the disagreement; never report an install complete from the wiring step's exit code alone. |
| The operator aborts | No canonical file is ever modified, so an abort leaves the roster untouched; the agent surface is left exactly as found. |

## Related skills

- `gd-skills-remove` and `gd-skills-scope`: the rest of this family.
- `gd-help`: the index an installed agent uses to discover the roster.
