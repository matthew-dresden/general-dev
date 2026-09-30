---
name: gd-help
description: The roster index -- maps the skill hierarchy and its seven families, names the one skill to invoke for a stated goal, and points at the make targets each family drives; reads only, changes nothing, and is itself the entry point every other skill names when the right skill is unclear.
---

# gd-help

This skill is the roster's front door: given a goal in the operator's words,
it names the one skill to invoke and the make targets that skill drives. It
performs nothing itself -- every row below is a pointer, and the canonical
table it indexes is the roster in `docs/skills.md`, which
`tests/test_skill_lint.py` pins to the directories actually present, so
this index cannot name a skill that does not exist.

The hierarchy is one flat namespace, `gd-<name>`, grouped into seven
families by the middle segment. The table below is this skill's
invocation map: each family's make targets, one column over from its
members.

## Invocation map

| Family | Members | Drives |
|---|---|---|
| env- | `gd-env-setup-local`, `gd-env-setup-remote`, `gd-env-doctor` | `make init`, `make local` (and its `make disconnect`), `make remote`, `make connect`; the Section 4.2.1 check contract |
| project- | `gd-project-onboard` | `make init`, `make keybindings` |
| instance- | `gd-instance-create`, `gd-instance-fleet`, `gd-instance-list`, `gd-instance-destroy` | `make instance-init`, `make instance-plan`, `make instance-deploy`, `make instance-link`, `make list-instances`, `make instance-status`, `make instance-stop`, `make instance-start`, `make instance-destroy` |
| container- | `gd-container-local`, `gd-container-remote`, `gd-container-verify`, `gd-container-lifecycle` | `make build`, `make start`, `make restart`, `make reopen`, `make status`, `make check`, `make clean`, `make rebuild`, `make rename` |
| creds- | `gd-creds-setup`, `gd-creds-rotate`, `gd-creds-doctor` | `make creds-init`, `make push-creds`, `make verify-container` |
| cert- | `gd-cert-lifecycle` | `make cert-ca`, `make cert-client`, `make cert-publish`, `make cert-install`, `make cert-status` |
| skills- | `gd-skills-install`, `gd-skills-remove`, `gd-skills-scope` | the U3 skills-management make targets (forthcoming) |
| (cross-cutting) | `gd-quality`, `gd-help` | `make validate`, `make lint`, `make test` |

## Resolution rules

1. Goal mentions a fresh clone or first run: `gd-project-onboard`.
2. Goal is about where builds run (this machine, an instance, fleet state,
   or removing an instance): the instance- family, in that order.
3. Goal is about a container's build, run, state or destruction: the
   container- family; verification before action.
4. Goal mentions credentials, the keychain or the manifest: the creds-
   family; `gd-creds-doctor` when the question is "is it working".
5. Goal mentions certificates or expiry: `gd-cert-lifecycle`.
6. Goal is a red `make validate`: `gd-quality`.
7. Anything else, or an ambiguous goal: state the two closest rows above and
   ask, never guess into a destructive skill.

## Failure semantics

| Condition | Behavior |
|---|---|
| The goal matches no row | Say so plainly, name the closest families, and stop; never invent a skill or route a goal to `gd-instance-destroy` or `gd-container-lifecycle` by elimination. |
| The named skill does not resolve | Report the gap (the roster table in `docs/skills.md` and the directories cannot disagree silently); stop rather than substituting a different skill. |
| The operator aborts | Nothing to unwind: this skill is a read. |

## Related specifications

- `docs/skills.md`: the canonical roster table this index summarizes.
- `make help`: the target-level view of the same surface; every entry in
  this document's Drives column is verified against it.
