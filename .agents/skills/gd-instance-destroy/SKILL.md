---
name: gd-instance-destroy
description: Destroys one remote instance behind GATE-DESTROY -- records the two-part precheck (operator confirmation, and the volume verified to hold no unpushed work before the confirmation is even requested), then runs make instance-destroy and verifies every derived artifact (parameters, certificates, context, id) is gone; never destroys on an ambiguous request.
---

# gd-instance-destroy

This skill is the destructive counterpart of `gd-instance-create`, and the
only skill in this roster that reaches `GATE-DESTROY`. The gate's two-part
requirement (spec Section 4.4) is ordered: the work-loss check runs and is
found clean *first*, and only then is the operator's confirmation requested
and recorded -- verbatim, by the human operator, never on this skill's own
behalf. A confirmation taken before the work-loss evidence exists is not a
confirmation this skill may act on.

Container-level teardown is never this skill's scope: `make clean` and
`make rebuild` belong to `gd-container-lifecycle`, and an ambiguous "tear it
down" is resolved by stating the boundary and asking which was meant, never
by acting at the smaller scope by default.

## Invocation map

| Step | What this skill runs | Notes |
|---|---|---|
| Work-loss precheck | Invoke `gd-instance-list INSTANCE=<name>` and read its `no unpushed work in the volume` row | Must report clean before anything else; a dirty volume stops this skill before confirmation is requested. |
| Confirm | Present the instance, its id, and the clean precheck; wait for the operator's explicit confirmation | Recorded verbatim. Silence, an empty answer, or ambiguity is a decline. |
| Destroy | `make instance-destroy INSTANCE=<name>` | Destroys the instance and cleans up its parameters, certificates, docker context and linked id. `ALL=1` (fleet) additionally requires `CONFIRM=destroy` and belongs to `gd-instance-fleet`. |
| Verify | `make list-instances` afterward | The instance is gone from the report, with no orphaned parameter, certificate, context or id entry. |
| Bucket roster | `make bucket-list` | The instance's state bucket survives the destroy by design; this shows it, marked orphaned, along with every other bucket the fleet's template matches. |
| State bucket removal | `make bucket-delete INSTANCE=<name>` | Deletes the instance's bucket after purging every version. `ALL=1 CONFIRM=delete` clears every bucket in `REMOTE_AWS_REGION`; an operator decision, never this skill's. |

## Checks

| Check | Prevents | Failure message | Remedy |
|---|---|---|---|
| volume verified clean before confirmation | destroying the only copy of container-only work | `gd-instance-list`'s `no unpushed work in the volume` row is failing or absent | Stop. State the commits and `git push origin <branch>` run from inside the container as the remedy; destruction is not offered until the row is clean. |
| confirmation recorded after the precheck | an autonomous run destroying AWS resources without a recorded, informed operator confirmation (AC-4.4, GATE-DESTROY) | destroy requested with no recorded clean precheck and confirmation | Present both, stop, and wait; proceeding without them is a work-unit failure this skill states rather than routes around. |
| derived artifacts gone after the run | a half-destroyed instance reserving a name, prefix or context the next `gd-instance-create` cannot reuse cleanly | `make list-instances` still lists a parameter, certificate, context or id for the destroyed instance | Re-run `make instance-destroy INSTANCE=<name>` once and re-verify; a residue that survives is reported with the exact artifact named, never cleaned by hand. |

## Failure semantics

| Condition | Behavior |
|---|---|
| The precheck is dirty or cannot be obtained | Stop before confirmation is requested; name the failing row and its remedy. Never request a confirmation this skill could not act on. |
| The operator declines or is ambiguous | Report that nothing was destroyed; the precheck record is kept for the next attempt. Never proceed on silence. |
| `make instance-destroy` exits non-zero | Print the target's own output and remedy; run the verification step anyway and report exactly what state the instance and its artifacts are in. Never retry silently. |
| A gate is reached (Section 4.4) | This skill exists at `GATE-DESTROY`: present the evidence, stop, never self-approve. |
| The operator aborts | Nothing is destroyed; only reads have run. |

## Related skills

- `gd-instance-create`: provisions what this skill destroys; the addressing
  both skills derive is `devcontainer_config.instances`' alone.
- `gd-instance-fleet`: the ALL=1 fleet form of destruction, with its own
  `CONFIRM=destroy` requirement.
- `gd-container-lifecycle`: container-level teardown, which a request to
  this skill must never silently degrade into.
