---
name: gd-skills-scope
description: Decides and records which scope an agent surface receives -- the global symlink, the in-repo project adapters, or the printed one-shot runtime incantation -- by writing agent-surface configuration, never by moving or deleting canonical files, and verifies the scoped set resolves; drives the make skills-list target.
---

# gd-skills-scope

The roster is organized in families (see `gd-help`): a laptop-repair agent
needs the env- and creds- families but not instance-; a fleet-operations
agent needs instance- and container- but not skills-. Scoping is a property
of the consumer, so it is recorded where the consumer reads it -- the
agent's skill directory, the repository's in-repo adapters, or a one-shot
runtime override -- never by deleting from `.agents/skills`.

This family's scoping surface is the choice of AGENT and SCOPE on the
native Mac (no devcontainer, docker or AWS involvement):

- `SCOPE=global` scopes an agent to the whole roster through one
  `general-dev-skills` symlink in its user-level skill directory.
- `SCOPE=project` scopes the agents that work inside this checkout:
  opencode natively, Claude Code through the tracked plugin symlink.
- `SCOPE=runtime` scopes a single launch with a printed (never executed)
  one-shot incantation: `OPENCODE_CONFIG` for opencode -- whose JSON
  replaces the project config, so it embeds the provider/model block from
  `.devcontainer/opencode.json` read at generation time -- and a
  `--settings` JSON for Claude Code (printed only when the installed build
  advertises the flag; otherwise an explicit unsupported message with the
  manual fallback).

## Invocation map

| Step | What this skill runs | Notes |
|---|---|---|
| Collect the scope | None | Ask which agent (`AGENT=opencode|claude|both`) and which scope (`SCOPE=global|project|runtime`) the surface should receive; an unclear answer is re-asked, never defaulted. |
| Apply | `make skills-install AGENT=<agent> SCOPE=<scope>` | The wiring step for the chosen scope; `SCOPE=runtime` prints the incantation for the operator to run, and nothing else happens. |
| Verify | `make skills-list AGENT=<agent> SCOPE=<scope>` | Every scoped-in route reports installed/native/wired; anything else names the drift. The canonical home still holds every roster name. |

## Checks

| Check | Prevents | Failure message | Remedy |
|---|---|---|---|
| the scoped set matches the stated scope exactly | an agent silently gaining skills it was never meant to see | the surface resolves a route outside the stated scope | Re-apply the scoping with `make skills-install` for the stated AGENT/SCOPE and re-verify with `make skills-list`; report the extra routes rather than proceeding. |
| the canonical home is untouched | scoping one agent by deleting from the shared roster | `.agents/skills` no longer holds every roster name | `git restore .agents/skills` and stop; scoping never removes canonical files. |

## Failure semantics

| Condition | Behavior |
|---|---|
| The scope statement is ambiguous | Re-ask with the AGENT/SCOPE values spelled out; never interpret "everything" into a scope the operator did not name. |
| An existing entry is not ours | The install target refuses and names the path; move or delete it by hand only if it is genuinely yours. |
| An action the skill took did not verify | Re-run `make skills-list` over the full roster and report each disagreement; never a silent retry. |
| A gate is reached (Section 4.4) | This skill reaches no Section 4.4 gate: it writes agent-surface configuration only. |
| The operator aborts | The canonical home is untouched by construction; any applied surface configuration is reported as left in place. |

## Related skills

- `gd-skills-install` and `gd-skills-remove`: wiring and unwiring, which
  scoping composes.
- `gd-help`: the family map this skill's scope statements use.
