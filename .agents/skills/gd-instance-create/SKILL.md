---
name: gd-instance-create
description: Provisions one remote instance end to end, EC2, azure or gcp - scaffolding remote-instances/<name> with make instance-init, planning with make instance-plan, converging with make instance-deploy behind the PRECHECK-APPLY gate, and recording the applied address (the EC2 id, or the engine address record on azure and gcp); never applies without a recorded operator confirmation.
---

# gd-instance-create

This skill owns first-time provisioning of one named instance, of any of
the three provisioned kinds: the EC2 kind (the default), the azure kind
and the gcp kind, selected with `CLOUD=azure` or `CLOUD=gcp` at scaffold
time. The instance
name is a project name (the convention `make help`'s INSTANCES section
states: instance names are project names), and
every address this skill derives (`remote-instances/<name>/`, the docker
context, the certificate paths, the EC2 Parameter prefix or the azure
relay address record) follows from it
through `devcontainer_config.instances`, the single owner of the addressing
derivations. This skill decides nothing about what a valid deployment looks
like: the Terragrunt modules under `remote-instances/` and the make targets
below own that.

Where this skill cannot act itself (the Terragrunt apply, an SSO, `az` or
`gcloud` login), it states the exact command, waits for the operator, then
re-verifies before continuing. It never proceeds past PRECHECK-APPLY on its
own confirmation; doing so is a work-unit failure under AC-4.4.

## Invocation map

| Step | What this skill runs | Notes |
|---|---|---|
| Scaffold | `make instance-init INSTANCE=<name>` (EC2), `make instance-init INSTANCE=<name> CLOUD=azure LOCATION=<location>` with `VM_SIZE=` optional (azure), or `make instance-init INSTANCE=<name> CLOUD=gcp PROJECT=<project-id> ZONE=<zone>` with `VM_SIZE=` optional (gcp) | Creates `remote-instances/<name>/`; never deploys. EC2: `AMI=` and `REGION=` name the AMI/AZ lookup only, and the deployment region is `REMOTE_AWS_REGION`; azure: `REGION=` and `AMI=` are refused, and the azure scaffold cannot fill in the operator's Entra object ids — the one edit it states for the operator (`relay_sender_object_ids`, `keyvault_officer_object_ids`); gcp: `REGION=` and `AMI=` are refused, the region derives from the zone, and the scaffold needs no operator-owned edit at all. |
| Plan | `make instance-plan INSTANCE=<name>` | Terragrunt plan; bootstraps the instance's state bucket (EC2) or ensures the azure state store or the gcp state bucket on first run. |
| Confirm | PRECHECK-APPLY | Present the plan output, wait for the operator's explicit confirmation, record both. Never self-approve. |
| Converge | `make instance-deploy INSTANCE=<name>` | Provisions, records the applied address (EC2 links the id, azure and gcp write the engine address record from the Terragrunt outputs), establishes the trust chain if missing, pushes secrets. Refuses instance replacement without `CONFIRM=replace`; this skill reports that refusal, never bypasses it. |
| Link | `make instance-link INSTANCE_ID=<id>` (EC2 only) | Only needed after re-provisioning outside make; `instance-deploy` does it automatically. An azure or gcp engine has no EC2 id to link — its address record is the deploy's own write, and re-linking after out-of-band re-provisioning is re-running that deploy. This row is also the recovery path for a lost or stale link on EC2 (the `flow: INSTANCE_ID link recovery` flow of `docs/skills.md`'s Flows section): when a target reports that no EC2 id is recorded for `<name>`, re-running it with the id from the Terragrunt output re-records it. |

## Checks

| Check | Prevents | Failure message | Remedy |
|---|---|---|---|
| instance name is a project name | an instance named for a geography or stage confusing every later `INSTANCE=<name>` resolution | the proposed name is not a project name | Ask for the project name and re-run `make instance-init INSTANCE=<name>` with it; never rename a directory after deploy. |
| cloud credentials valid for the deployment | every call the converge makes: the aws CLI and the SSO session on EC2, `az` and its login on azure, `gcloud` and its login on gcp | EC2: `aws` cannot resolve credentials for the profile. Azure: `az account show` answers nothing. Gcp: `gcloud auth list` answers with no active account | EC2: state `aws sso login --profile <profile>`, wait for the operator, then re-probe with `hostprobe.probe_aws_identity` before continuing. Azure: state `az login`, wait for the operator to complete the browser login, then re-probe before continuing. Gcp: state `gcloud auth login`, wait for the operator to complete the browser login, then re-probe before continuing. |
| azure object ids present before an azure converge | a deployment that provisions cleanly and cannot be reached or supplied: no sender grant for the connect, no officer grant for the push | the scaffolded `relay_sender_object_ids` or `keyvault_officer_object_ids` is empty | State the two assignments to add and the `az ad signed-in-user show --query id` way to find the id, wait for the operator to edit the scaffolded file, then re-read it before running the converge. |
| PRECHECK-APPLY recorded before `make instance-deploy` | an autonomous run creating cloud resources without a recorded operator confirmation (spec Section 4.4, AC-4.4) | deploy reached with no recorded plan and confirmation | Present the plan output and stop until the operator confirms; record both before running the target. |

## Failure semantics

| Condition | Behavior |
|---|---|
| A make target in `## Invocation map` exits non-zero | Stop at that step, print the target's own output and remedy, and run no later step. Never retry silently. |
| `make instance-deploy` refuses for instance replacement | Report the refusal verbatim: replacement is an operator decision made with `CONFIRM=replace` in their own invocation, never this skill's. |
| A gate is reached (Section 4.4) | Present the evidence and stop; never self-approve. This skill reaches `GATE-APPLY`, named PRECHECK-APPLY above, every time it converges. |
| The operator aborts | No cloud resource is created until `make instance-deploy` actually runs; an abort before that leaves only the `remote-instances/<name>/` scaffold, which a later run reuses or the operator deletes. |

## Related skills

- `gd-env-setup-remote`, `gd-env-setup-azure` and `gd-env-setup-gcp`:
  configure this laptop to reach the instance this skill creates, per
  kind; certificates and the docker context are their steps.
- `gd-instance-list`: validates the provisioned instance end to end.
- `gd-instance-fleet` and `gd-instance-destroy`: fleet operations and the
  destructive counterpart of this skill.
