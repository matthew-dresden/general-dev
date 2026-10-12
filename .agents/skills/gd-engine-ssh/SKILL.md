---
name: gd-engine-ssh
description: Owns the SSH engine kind's lifecycle end to end, scaffolding the engine.json manifest with make engine-init, proving reachability with the one-shot make engine-verify, running the container through make remote and make build, and tearing the engine whole down with make engine-remove behind its CONFIRM=remove gate; also drives the forward lifecycle (connect-start, connect-status, connect-stop, connect-refresh, connect-list) for ssh engines.
---

# gd-engine-ssh

This skill exists for the engine kind that is not an EC2 instance: a
generic SSH endpoint with Docker, declared wholly by
`remote-instances/<name>/engine.json` (see `devcontainer_config.instances`).
An ssh engine has no AWS session, no Terragrunt deployment, no Parameter
Store prefix and no certificates behind it (the manifest is the whole
declaration), so every EC2-shaped question ("is the instance running",
"where is its state bucket") is replaced by exactly one question: can this
machine reach the engine's docker through an ssh forward? That question is
what `make engine-verify` answers, one shot at a time.

The asymmetry this skill never blurs: EC2 instances are provisioned,
powered and destroyed through the instance- targets (`gd-instance-create`,
`gd-instance-fleet`, `gd-instance-destroy`); an ssh engine is scaffolded,
verified and removed through this skill's three targets. The teardown
refuses to run while a project container still exists on the engine, the
same way `GATE-DESTROY` refuses on unpushed work: destroy the containers
first, deliberately.

## Invocation map

| Operation | What this skill runs | Confirmation | Verification |
|---|---|---|---|
| Scaffold an ssh engine | `make engine-init INSTANCE=<name> HOST=<host> USER=<user>` (PORT= and DOCKER_ADDR= optional; a Windows engine needs Docker Desktop exposed on tcp://localhost:2375, passed as DOCKER_ADDR=tcp://localhost:2375) | None: writes the manifest and nothing else. | `make list-instances` shows the engine with its reachability column; the manifest reads back through `instances.engine_manifest`. |
| Prove the engine is reachable | `make engine-verify INSTANCE=<name>` | None: opens the forward, handshakes docker, closes the forward; no daemon is left running and no context switch happens. | The `Verified:` line names server and api versions; on failure the ssh diagnosis names the endpoint, the port and, for a socket-path destination on a Windows engine, the platform rule. |
| Open the forward for real work | `make connect-start INSTANCE=<name> DEVCONTAINER_TRANSPORT=ssh` (or `make remote INSTANCE=<name>`, which also switches docker) | None: the daemon holds the tunnel, not a terminal. | `make connect-status INSTANCE=<name>` reports pid, port and listening; `make build` answers through the context. |
| Manage the forward daemon | `make connect-status` / `make connect-stop` / `make connect-refresh` / `make connect-list` (INSTANCE= or ALL=1 for the first three) | None: refresh re-runs the recorded command and can never aim at a different engine. | Each target verifies its own effect: stop verifies the port actually closed before reporting. |
| Build and inspect | `make build`, `make list-containers` | `make build` refuses if a container already exists; nothing here destroys one. | `make list-containers` reports per-engine reachability; the ssh engine appears with its own row. |
| Remove the engine | `make engine-remove INSTANCE=<name> CONFIRM=remove` | The target's own `CONFIRM=remove`, plus this skill's rule: report every container `make list-containers` shows on the engine and stop for an explicit operator go-ahead before removing. | A fresh `make list-instances` no longer lists the engine; the docker context, the forward record, the certificate directory and `remote-instances/<name>` are gone. |

## Checks

| Check | Prevents | Failure message | Remedy |
|---|---|---|---|
| the instance is the ssh kind | running an engine-* target against an EC2 instance, whose lifecycle belongs to other targets | `instance '<name>' is an ec2 engine; this target only reaches ssh instances` | Name the EC2 surface the refusal prints: `make connect-start INSTANCE=<name>` for reachability, `make instance-destroy INSTANCE=<name>` for teardown. Never force one kind's target onto the other. |
| the manifest is valid | a typo'd host, user, port or docker_addr becoming a half-working engine discovered only at connect time | the `engine_manifest` field refusal, naming the field and the rule | Fix the named field in `remote-instances/<name>/engine.json`, then re-run `make engine-verify INSTANCE=<name>` before anything depends on the engine. |
| no project container exists on the engine | `make engine-remove` stranding this project's containers and their volumes on an engine that then disappears | `instance '<name>' still holds this project's container(s): <names>` | Run `make clean ENGINE=<name>` (FORCE=1 additionally bypasses its unpushed-work guard, only on an explicit operator instruction), then re-run `make engine-remove INSTANCE=<name> CONFIRM=remove`. |
| the operator confirmed the removal | a typo'd or aborted intent deleting a registration, a context and local state in one step | `engine-remove deletes the engine registration, its docker context, its forward and its local state; confirm it: make engine-remove INSTANCE=<name> CONFIRM=remove` | State the exact confirming command, wait for the operator to run it deliberately. Never add CONFIRM=remove on the operator's behalf. |

## Failure semantics

| Condition | Behavior |
|---|---|
| `make engine-verify` fails | Report the ssh diagnosis verbatim: the endpoint, the local port, and whether the forward never opened (confirm plain ssh reachability and the docker destination) or the handshake never answered (the daemon behind docker_addr is down). Nothing is left running; the context may exist and is reused by the next connect. |
| The engine's removal partially fails | Every removal is attempted even after an earlier one failed; report each named failure and re-run `make engine-remove INSTANCE=<name> CONFIRM=remove` after fixing them. Never hand-finish the removal piecemeal while the target exists. |
| A fleet mixes kinds | The EC2-only fleet surfaces (bucket and cert reporting, the instance- ALL=1 targets) refuse naming the first ssh engine. Do not widen an ssh-engine request into a fleet run or the reverse; state the refusal and act on the one engine asked about. |
| The forward drops mid-session | `make connect-refresh INSTANCE=<name>` re-runs the recorded command; the ssh diagnosis lives in `rd_engine_diagnosis` and never names the AWS path an ssh engine does not have. |

## Related skills

- `gd-instance-create`, `gd-instance-fleet`, `gd-instance-destroy`: the
  EC2 kind's provisioning, fleet and teardown (the surfaces the engine-*
  targets deliberately do not touch).
- `gd-container-remote`: driving a container on a remote engine once the
  forward is open; this skill hands over after `make remote`.
- `gd-instance-list`: the per-instance validation verdicts, whose
  reachability check an ssh engine answers through this skill's probe.
- `gd-help`: the roster index, when the right surface for a goal is
  unclear.
