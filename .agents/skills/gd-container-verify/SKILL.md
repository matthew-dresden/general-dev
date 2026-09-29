---
name: gd-container-verify
description: Read-only container-state verification -- runs make status and make check, reports backend, container, image and volume state, and flags uncommitted or unpushed work in a remote volume; performs no repair of its own and names the exact remedy for every finding.
---

# gd-container-verify

This skill answers "what state is the container in, and is anything about to
be lost" without changing anything. It reads only: `make status` for the
backend, container, image and volumes, and `make check` for the
uncommitted-or-unpushed work a rebuild would strand in a remote volume. It
performs no repair: every finding names the exact command or skill that
fixes it, and the operator (or the named skill) runs it.

The primitives beneath both targets are `container.sh`'s `rdc_status`,
`rdc_check`, `rdc_container_ids` and `rdc_require_container` -- this skill
runs the targets rather than re-deriving container state a second way, the
same reuse rule `gd-env-doctor`'s container-state group and
`gd-container-lifecycle`'s inventory already follow.

## Invocation map

| Check | What this skill runs | Notes |
|---|---|---|
| State | `make status` | Read-only on both backends; the right first read whenever something looks wrong. |
| Work-loss guard | `make check` | Remote: reports uncommitted or unpushed work in the volume, non-zero when dirty. Local: a no-op -- the container shares this working tree. |

## Checks

| Check | Prevents | Failure message | Remedy |
|---|---|---|---|
| a container exists for the active backend | later commands failing against a container that is not there | no container for the project exists on the active context | Name `make up` (builds, starts and opens) or `make build` (builds and stops there); this skill runs neither. |
| the remote volume holds no uncommitted or unpushed work | a rebuild re-cloning from origin and stranding container-only work | `make check` lists the commits or the dirty paths | State `make check`'s own remedy verbatim: push from inside the container, or destroy deliberately with `make clean FORCE=1` via `gd-container-lifecycle`; this skill never pushes and never sets `FORCE`. |

## Failure semantics

| Condition | Behavior |
|---|---|
| A target exits non-zero | Report the target's own output and remedy verbatim; never interpret a non-zero exit as a clean state. |
| An action the skill took did not verify | Not applicable by construction: this skill runs nothing that changes state, so there is nothing to verify beyond the reads themselves. |
| A gate is reached (Section 4.4) | This skill reaches no Section 4.4 gate: it writes nothing and calls nothing against AWS. |
| The operator aborts | Nothing to unwind: the machine is exactly as it was. |

## Related skills

- `gd-container-local` and `gd-container-remote`: act on the state this
  skill reports.
- `gd-container-lifecycle`: the destructive path a dirty-volume finding
  points at, behind its own confirmation.
- `gd-env-doctor`: the fuller findings list this skill's two reads fold
  into.
