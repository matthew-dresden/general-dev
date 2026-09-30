---
name: gd-container-remote
description: Drives the remote backend's container -- refreshes the SSM port forward with make connect, points docker and VS Code at the instance with make remote, then builds, starts or reopens through the same state diagnosis as the local skill, against the cloned checkout in the instance's volume; destroys nothing.
---

# gd-container-remote

This skill is the remote-backend counterpart of `gd-container-local`: same
four actions (`make build`, `make start`, `make restart`, `make reopen`),
same never-destroys-anything boundary, one added stage -- the container
lives on a remote EC2 engine reached through an SSM port forward under
mutual TLS, so the forward and the context switch come first and every
later step runs against the instance's docker context. The container is a
fresh clone from origin in a volume, not a bind mount of this working tree,
which is why the unpushed-work guard (`make check`) matters here and is
carried forward rather than re-implemented.

This skill takes no destructive action and never sets `FORCE`: teardown of
the remote container belongs to `gd-container-lifecycle`, and teardown of
the instance itself to `gd-instance-destroy`.

## Invocation map

Like its local twin, every target below accepts `ENGINE=` in either
argument position, so a run can address one instance's engine explicitly
without switching the machine-wide docker context (the
`flow: ENGINE multi-engine addressing` flow of `docs/skills.md`'s Flows
section).

| Step | What this skill runs | Notes |
|---|---|---|
| Validate the instance | Invoke `gd-instance-list INSTANCE=<name>` first | Its thirteen-check verdict is this skill's precondition; a failure there is reported unchanged and stops this skill. |
| Refresh the forward | `make connect INSTANCE=<name>` | Re-run after a reboot, after sleep, or when SSO expires; idempotent. |
| Select the engine | `make remote INSTANCE=<name>` | Points docker and VS Code at the instance's context, refreshing the forward first. |
| Build or resume | `make build` / `make start` / `make restart` / `make reopen` | The same state diagnosis `gd-container-local` performs, on the now-active remote context. |

## Checks

| Check | Prevents | Failure message | Remedy |
|---|---|---|---|
| the forward answers before any docker call | a build failing mid-flight on a dead tunnel instead of at the gate | `docker version` over the forwarded port does not answer within `DOCKER_HANDSHAKE_TIMEOUT` | Re-run `make connect INSTANCE=<name>`, then re-probe before continuing; a forward that still does not answer is reported, never retried silently. |
| no unpushed work would be stranded by a rebuild | a rebuild re-cloning from origin and discarding container-only commits | `make check` reports uncommitted or unpushed work in the volume | State `make check`'s own remedy (push from inside the container), wait for the operator, then re-run `make check`; rebuilding is refused until it is clean. |

## Failure semantics

| Condition | Behavior |
|---|---|
| `gd-instance-list`'s verdict is not a clean pass | Report its failing check and remedy unchanged; no target in `## Invocation map` runs. |
| An action the skill took did not verify | Re-read container state after every target, exactly as `gd-container-local`'s `## Verification` section states; report any disagreement, never retry silently. |
| A step needs the operator (SSO login, agent online) | State the exact command, wait, then re-verify before continuing. |
| A gate is reached (Section 4.4) | This skill reaches no Section 4.4 gate: it creates and destroys no AWS resource and runs no Terragrunt command. |
| The operator aborts | The forward and the active context are the only state this skill changes; both are idempotent and safe to leave. |

## Related skills

- `gd-container-local`: the local-backend twin; the state diagnosis and
  verification contract are its statement, carried forward here.
- `gd-container-lifecycle`: the destructive actions this skill never takes.
- `gd-container-verify`: the read-only state checks this skill reuses.
