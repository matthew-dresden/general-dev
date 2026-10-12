---
name: gd-instance-fleet
description: Runs one operation across every configured instance at once through the ALL=1 make targets -- status, stop, start and (behind its own confirmation) destroy -- reading make list-instances first and reporting a per-instance result table; never widens a single-instance request into a fleet run on its own.
---

# gd-instance-fleet

This skill exists for the moment an operator says "all of them": stopping
every instance before a weekend, starting the fleet on Monday morning, or
reporting live state across the board. The make targets below already
implement the per-instance loop; this skill decides nothing about how to
talk to EC2 -- it sequences the targets, reads their per-instance results,
and reports one line per instance. `devcontainer_config.instances` owns
discovery, so the set "every configured instance" is always what that module
enumerates, never a list this skill maintains itself.

Fleet runs multiply blast radius, so the asymmetry between read, start and
stop against destroy is stated rather than implied: the first three report
and continue per instance, while `make instance-destroy ALL=1` requires the
target's own `CONFIRM=destroy` and is treated as a Section 4.4 gate -- the
operator confirms the fleet, in their own words, before this skill runs it.

## Invocation map

| Operation | What this skill runs | Confirmation | Verification |
|---|---|---|---|
| Report fleet state | `make list-instances`, or `make instance-status ALL=1` | None: reads only. | The per-instance table the target prints is the report; this skill restates no row it did not read. |
| Stop the fleet | `make instance-stop ALL=1` | Name every instance the run will stop, then wait for the operator's go-ahead. | Each instance reports `stopped` in the target's own output; an instance that fails to stop is reported, never retried silently. |
| Start the fleet | `make instance-start ALL=1` | Same naming-and-wait as stop. | Each instance's SSM agent reports ready in the target's own output. |
| Destroy the fleet | `make instance-destroy ALL=1` | `CONFIRM=destroy` is the target's own requirement; this skill additionally presents the full instance list and stops for an explicit operator confirmation, never assumed from silence. | The post-run `make list-instances` shows the instances gone, with their parameters, certificates, contexts and ids cleaned up. |

## Checks

| Check | Prevents | Failure message | Remedy |
|---|---|---|---|
| at least one instance is configured | an ALL=1 run against an empty fleet reading as a success | no directory under `remote-instances/` is configured | Report that the fleet is empty and stop; `make instance-init` (via `gd-instance-create`) is the remedy for the first instance. |
| SSO session valid for every profile the fleet spans | a half-executed fleet run failing midway on one profile | credentials do not resolve for one of the instances' profiles | State `aws sso login --profile <profile>` for the failing profile, wait for the operator, then re-probe with `hostprobe.probe_aws_identity` before running the operation. |

## Failure semantics

| Condition | Behavior |
|---|---|
| One instance's operation fails mid-fleet | Report that instance's failure with its own remedy and the result for every other instance; continue the remaining instances only for read-shaped operations, and stop the run for write-shaped ones. Never retry silently. |
| A step needs the operator (SSO, confirmation) | State the exact command or the exact instance list, wait, then re-verify before continuing. Never assume silence is consent. |
| A gate is reached (Section 4.4) | `make instance-destroy ALL=1` is `GATE-DESTROY` at fleet scale: present the evidence (every instance, its work-loss state), stop, and never self-approve. |
| The operator aborts | Instances already operated on stay in their new state, reported as such; nothing further runs. |

## Related skills

- `gd-instance-create` and `gd-instance-destroy`: the per-instance
  provisioning and destruction this skill scales across the fleet.
- `gd-instance-list`: the per-instance validation verdict this skill's
  status rows summarize.
