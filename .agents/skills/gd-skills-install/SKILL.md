---
name: gd-skills-install
description: Installs this roster's canonical skills into an agent's skill surface -- verifies the agent reads .agents/skills directly or wires the documented relative symlink, never copies files -- and verifies the install by resolving every name to its SKILL.md through the agent's own surface; drives the make skills-install target.
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

This is a native-Mac operation: every scope below runs on this machine and
touches no devcontainer, docker or AWS state. The two agents this
repository ships wiring for are the standing example of the
`flow: opencode/claude agent setup pointers` flow of `docs/skills.md`'s
Flows section: opencode reads `.agents/skills` directly (the canonical
home needs no pointer), and Claude Code consumes the roster through the
plugin's `skills/` relative symlink described above. Wiring any other
agent follows the same two rules this skill verifies -- point at the
canonical home, never copy it -- and resolves every `gd-` name afterward.

## Invocation map

| Step | What this skill runs | Notes |
|---|---|---|
| Discover the agent surface | `make skills-list SCOPE=global AGENT=<agent>` (read) | Report the current state per agent: installed (with the resolved target), native (project), or not installed. |
| Wire | `make skills-install AGENT=<agent> SCOPE=<scope>` | `SCOPE=global` (the default) creates one `general-dev-skills` symlink in the agent's user-level skill directory, pointing absolutely at this checkout's `.agents/skills`; `SCOPE=project` verifies -- and, for Claude Code, wires -- the in-repo adapters; `SCOPE=runtime` prints the agent's one-shot incantation and changes nothing. |
| Verify | None (read) | Resolve every `gd-*` name in the roster (`docs/skills.md`) to a `SKILL.md` through the agent's own surface; report any name that does not resolve. |

### Scope semantics

| Scope | What `make skills-install` does |
|---|---|
| `global` | Symlink named `general-dev-skills` in `~/.config/opencode/skills` (opencode) or `~/.claude/skills` (Claude Code), absolute target, recorded in the output. Refuses any existing entry of that name that is not already our symlink. |
| `project` | The repository itself: opencode is native (`.agents/skills`, nothing to wire); Claude Code's tracked `.claude/plugins/devcontainer/skills` relative symlink is verified and, if missing, wired with the documented relative target. |
| `runtime` | No filesystem change. Prints a one-shot incantation instead -- see below. |

### The runtime incantation

`make skills-install SCOPE=runtime` prints, never executes, the one-shot
incantation for the selected agent:

- opencode: an `OPENCODE_CONFIG` incantation whose JSON carries a
  `skills.paths` override. Because an `OPENCODE_CONFIG` file REPLACES the
  project config rather than merging with it, the printed JSON embeds the
  provider/model block read from `.devcontainer/opencode.json` at
  generation time -- a bare skills-only override would launch opencode
  with no provider at all.
- Claude Code: a `--settings` one-shot whose JSON enables this checkout's
  plugin marketplace path. Printed only when the installed build
  advertises the flag (decided from `claude --help` at run time); a build
  without it gets an explicit unsupported message with the manual fallback
  (the global symlink, or the marketplace/plugin entries added to
  `~/.claude/settings.json` by hand).

## Checks

| Check | Prevents | Failure message | Remedy |
|---|---|---|---|
| the agent's surface resolves to the canonical home | a second, diverging copy of the roster | the agent's skill path is a real directory with its own SKILL.md files | Replace the copy with the documented relative symlink (`../../../.agents/skills` from the plugin's skills path, or the agent's equivalent), then re-verify; never merge the two. |
| every roster name resolves through the agent | an agent silently missing skills the rest of the platform assumes | a roster name does not resolve in the agent's surface | Re-run the wiring step and re-verify; report the missing names rather than a partial success. |

## Failure semantics

| Condition | Behavior |
|---|---|
| The agent cannot follow a symlink | State that fact and stop; ask the operator to choose between the agent's own indirection mechanism and not installing. Never fall back to copying. |
| An existing global entry named `general-dev-skills` is not our symlink | The target refuses and names the path: move or delete the entry by hand if it is genuinely yours, then re-run. Never replace an entry the target did not create. |
| An action the skill took did not verify | Re-run the resolution check and report the disagreement; never report an install complete from the wiring step's exit code alone. |
| The operator aborts | No canonical file is ever modified, so an abort leaves the roster untouched; the agent surface is left exactly as found. |

## Related skills

- `gd-skills-remove` and `gd-skills-scope`: the rest of this family.
- `gd-help`: the index an installed agent uses to discover the roster.
