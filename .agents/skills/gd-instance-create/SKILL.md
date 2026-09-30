---
name: gd-instance-create
description: Provisions one remote EC2 instance end to end -- scaffolds remote-instances/<name> with make instance-init, plans with make instance-plan, converges with make instance-deploy behind the PRECHECK-APPLY gate, and links the id with make instance-link; never applies without a recorded operator confirmation.
---

# gd-instance-create

This skill owns first-time provisioning of one named instance. The instance
name is a project name (the convention `make help`'s INSTANCES section
states: instance names are project names, never geographies or stages), and
every address this skill derives -- `remote-instances/<name>/`, the docker
context, the Parameter prefix, the certificate paths -- follows from it
through `devcontainer_config.instances`, the single owner of the addressing
derivations. This skill decides nothing about what a valid deployment looks
like: the Terragrunt modules under `remote-instances/` and the make targets
below own that.

Where this skill cannot act itself -- the Terragrunt apply, an SSO login --
it states the exact command, waits for the operator, then re-verifies before
continuing. It never proceeds past PRECHECK-APPLY on its own confirmation;
doing so is a work-unit failure under AC-4.4.

## Invocation map

| Step | What this skill runs | Notes |
|---|---|---|
| Scaffold | `make instance-init INSTANCE=<name>` | Creates `remote-instances/<name>/`; never deploys. `AMI=` and `REGION=` name the AMI/AZ lookup only; the deployment region is `REMOTE_AWS_REGION`. |
| Plan | `make instance-plan INSTANCE=<name>` | Terragrunt plan; bootstraps the shared state bucket on first run. |
| Confirm | PRECHECK-APPLY | Present the plan output, wait for the operator's explicit confirmation, record both. Never self-approve. |
| Converge | `make instance-deploy INSTANCE=<name>` | Provisions, links the id, establishes the trust chain if missing, pushes secrets. Refuses instance replacement without `CONFIRM=replace` -- a refusal this skill reports, never bypasses. |
| Link | `make instance-link INSTANCE_ID=<id>` | Only needed after re-provisioning outside make; `instance-deploy` does it automatically. This row is also the recovery path for a lost or stale link (the `flow: INSTANCE_ID link recovery` flow of `docs/skills.md`'s Flows section): when a target reports that no EC2 id is recorded for `<name>`, re-running it with the id from the Terragrunt output re-records it. |

## Checks

| Check | Prevents | Failure message | Remedy |
|---|---|---|---|
| instance name is a project name | an instance named for a geography or stage confusing every later `INSTANCE=<name>` resolution | the proposed name is not a project name | Ask for the project name and re-run `make instance-init INSTANCE=<name>` with it; never rename a directory after deploy. |
| SSO session valid for the deployment profile | every AWS call this skill makes | `aws` cannot resolve credentials for the profile | State `aws sso login --profile <profile>`, wait for the operator, then re-probe with `hostprobe.probe_aws_identity` before continuing. |
| PRECHECK-APPLY recorded before `make instance-deploy` | an autonomous run creating AWS resources without a recorded operator confirmation (spec Section 4.4, AC-4.4) | deploy reached with no recorded plan and confirmation | Present the plan output and stop until the operator confirms; record both before running the target. |

## Failure semantics

| Condition | Behavior |
|---|---|
| A make target in `## Invocation map` exits non-zero | Stop at that step, print the target's own output and remedy, and run no later step. Never retry silently. |
| `make instance-deploy` refuses for instance replacement | Report the refusal verbatim: replacement is an operator decision made with `CONFIRM=replace` in their own invocation, never this skill's. |
| A gate is reached (Section 4.4) | Present the evidence and stop; never self-approve. This skill reaches `GATE-APPLY`, named PRECHECK-APPLY above, every time it converges. |
| The operator aborts | No AWS resource is created until `make instance-deploy` actually runs; an abort before that leaves only the `remote-instances/<name>/` scaffold, which a later run reuses or the operator deletes. |

## Related skills

- `gd-env-setup-remote`: configures this laptop to reach the instance this
  skill creates; certificates and the docker context are its steps.
- `gd-instance-list`: validates the provisioned instance end to end.
- `gd-instance-fleet` and `gd-instance-destroy`: fleet operations and the
  destructive counterpart of this skill.
