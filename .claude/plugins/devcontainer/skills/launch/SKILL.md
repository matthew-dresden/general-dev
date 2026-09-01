---
name: launch
description: Diagnoses container state (delegating engine reachability to /devcontainer:engine, whose local-against-remote rdc_backend selection stage 2 simply inherits, then reading rdc_container_ids and rdc_require_container itself), then runs make build, start, restart or reopen as the state calls for, verifying every action by re-reading state afterward rather than trusting a target's exit code; asks nothing and destroys nothing.
---

# launch

Section 4.2's own roster row gives `launch` the shortest definition of the
nine skills: it asks "Nothing"; it "Diagnoses state, then builds, starts or
reopens as appropriate"; and it ends by "A running container". A developer
who has to know whether a container exists, whether it is stopped, and
whether the image behind it is stale before choosing between `make build`,
`make start`, `make restart` and `make reopen` is carrying state the machine
already holds; this skill reads that state and picks, the skill-layer
equivalent of the `make up` target that already exists for exactly this
purpose.

Diagnosis happens in two stages, in this order, because the order is what
keeps a failure's message pointed at its real cause: building against an
engine that does not answer produces a failure naming the build rather than
the engine, sending the developer to the wrong remedy. Stage 1 delegates the
engine's own reachability, disk and configuration validation to
`/devcontainer:engine`; stage 2, reached only once that delegation reports
clean, reads the container itself through the Section 3.3 primitives named
below. This skill never re-implements a check `/devcontainer:engine` already
owns (Section 3's "DO NOT reinvent" prohibition) and never restates one of
its thirteen `## Checks` rows or its own `## Verdict` table here; a delegated
failure is carried forward as engine's own message and remedy, unchanged,
never paraphrased.

This skill takes exactly four actions -- `make build`, `make start`, `make
restart`, `make reopen` -- and never a fifth. It never runs `make clean`,
`make rebuild`, or either `--no-cache` variant: those destroy or replace
existing state, and destruction stays behind the confirmation the teardown
skill that Section 4.2's roster defines, and that E4-F3-S1-T2 authors, owns
rather than something this skill decides on the developer's behalf. That
skill has no `/devcontainer:<name>` invocation or roster row yet, so this
document names it by role rather than by slash command until it lands.
Section 4.2's own interaction contract governs every step below that this
skill cannot perform itself: "Where a
skill cannot act itself, it states the exact command, waits, then verifies
the result before continuing. Nothing is assumed to have worked." Section
4.2.2's failure semantics, common to every skill in this plugin, are
instantiated for this skill's own five conditions in `## Failure semantics`
below.

## Diagnosis

**Stage 1, the engine.** Invoke `/devcontainer:engine`. When its verdict
reports anything other than a clean pass -- a failing check, a warning that
still let it complete, or no verdict at all because the invocation itself
did not produce one -- stop immediately. Print the failing check's message
and remedy exactly as `/devcontainer:engine` phrased them, and state plainly
that no target in `## Decision` was attempted, naming all four so the
developer knows none of `make build`, `make start`, `make restart` or `make
reopen` ran. Stage 2 below never runs until stage 1 reports a clean pass,
because every stage 2 read (`docker ps`, `docker inspect`, an exec into the
container) needs the same reachable engine stage 1 already confirmed; a
second reachability probe here would be exactly the duplicated check Section
3 forbids.

**Stage 2, the container.** Which backend stage 2 reads against is not
re-resolved here: `rdc_backend` (`container.sh:99-105`) is the same local
against remote rule `/devcontainer:engine`'s own procedure already applied
to select the active docker context in stage 1, so stage 2 simply acts on
whichever context is active when stage 1 finished. Read the container itself
with the remaining Section 3.3 primitives
(`.devcontainer/remote-docker/container.sh`) in this order, so an absent
container is read as a normal, actionable state rather than as a failure:

1. `rdc_container_ids` (`container.sh:119-121`) first. On a zero exit it
   lists the docker ids carrying the `label=devcontainer.project=${PROJECT_NAME}`
   filter (`container.sh:120`; `PROJECT_NAME` defaults to the repository
   directory basename, `container.sh:9`) on the active context, and returns
   an empty result rather than an error when none exist -- that
   empty-on-zero-exit result is `## Decision`'s "no container" row.
   `rdc_container_ids` is not itself infallible: it calls `rd_docker`
   (`lib.sh:146`), which translates a non-zero `docker` exit through
   `rd_run`'s own translation step (`lib.sh:135-144`) into
   `rd_docker_failed`/`rd_fail` (`lib.sh:171+`) rather than returning it
   silently, and `rdc_require_container` guards the identical call with
   `ids="$(rdc_container_ids)" || exit $?` (`container.sh:136-137`)
   precisely because it can fail. This skill treats the two outcomes
   differently: an empty result on a zero exit is the normal "no container"
   state above; a non-zero exit is reported as a failure, exactly as
   `container.sh:136-137` treats it, never read as "no container" and never
   routed to `make build`. `rdc_require_container` is deliberately not
   called at this point even though it already carries this same guard,
   because its own contract is also to fail fast when nothing exists
   (`container.sh:138-144`, its own remedy naming `make build` or `make
   up`) -- calling it here would turn the normal, addressable "no container
   yet" state into a fatal early exit instead of a decision.
2. Once `rdc_container_ids` reports at least one id, `rdc_require_container`
   (`container.sh:123-158`) resolves to exactly one, preserving `CONTAINER=`
   selection exactly as it does for every other target today: a name that
   matches nothing, or more than one id with `CONTAINER=` unset, is
   `rdc_require_container`'s own failure, reported unchanged (`## Failure
   semantics`, "a step needs the operator"). On a remote backend, whichever
   instance stage 1's `/devcontainer:engine INSTANCE=<name>` invocation
   resolved is the same instance every target in `## Decision` is run
   against; this skill resolves an instance once, at stage 1, never twice.
3. With one id resolved, its docker-reported status decides running against
   stopped: `rdc_start`'s own comparison (`container.sh:231-242`) is the
   precedent this skill reuses rather than inventing a second state
   vocabulary -- `state = "running"` is running, anything else (`created`,
   `exited`, `paused`, `restarting`, `dead`) is stopped, because that is the
   exact two-way split `docker start` itself already treats as idempotent
   against and non-idempotent against.
4. When docker reports the container running, this skill probes it with
   `rdc_exec_probe <id> true` (`container.sh:91-94`). This skill reuses
   `rdc_exec_probe` rather than `rdc_exec` deliberately: `rdc_exec` calls
   `rd_docker` (`container.sh:86-89`), whose fail-fast translation
   (`lib.sh:146`) turns a non-zero exit into a diagnosed engine failure
   rather than a value this skill could inspect, while `rdc_exec_probe`
   calls `docker exec` directly with no such translation
   (`container.sh:91-94`), so a non-zero exit is returnable -- which is the
   entire reason a probe wants it. This is the same choice `rdc_check`'s own
   ahead probe already makes (`container.sh:279`, in contrast to the same
   function's dirty probe at `container.sh:278`, which uses `rdc_exec`
   because a failing `git status` there should stop the check, not be read
   as a probe result) and the same choice the git-credential probe makes
   (`container.sh:479`). The probe returning is "running and answering"; the
   probe failing (a non-zero exit from the exec itself, not from `true`,
   which never fails on its own) is "running but not answering" -- a wedged
   container, the exact condition `make restart`'s own `make help` line
   names: "Restart in place. Fixes a wedged container without rebuilding
   anything."

Whenever a container already exists (stage 2 step 2 resolved one), this
skill also reports whether the image behind it is stale -- whether the
Dockerfile or feature list `.devcontainer/devcontainer.json` names has
changed since that image was built. No primitive in this repository computes
that comparison today; this skill reports it as `NOT RUN`, naming that no
`rdc_*` function of this name exists yet, the same convention
`/devcontainer:engine` and `/devcontainer:doctor` already use for a probe
that has no owner. A stale image is never acted on by this skill: rebuilding
replaces the container and its volumes, which is the destructive path
`## Decision` never takes, and which the confirmation of the teardown skill
that Section 4.2's roster defines, and that E4-F3-S1-T2 authors, governs
instead.

## Decision

One row per state stage 2 can observe, mutually exclusive and covering
every combination of container present or absent and running or stopped:

| State | Target | Reason |
|---|---|---|
| No container exists for the active backend | `make build` | Nothing to reuse or resume; this is the one state in which `rdc_build`'s own already-exists guard (`container.sh:791-798`) cannot fire. |
| A container exists and is not running | `make start` | Resumes the same container against the same image with no re-clone and no rebuild, exactly as `make help`'s own line states: "Start or stop the container. The checkout survives either way." (`Makefile:77`) |
| A container exists, is running, and answers the stage 2 probe | `make reopen` | The container is already usable; only the editor needs to attach to it. |
| A container exists, is running, but does not answer the stage 2 probe | `make restart` | The docker-reported state and the container's own responsiveness disagree. Restarting in place resolves a wedged container without rebuilding anything, never destroying the volume a rebuild would. |

No fifth state exists to fall through unhandled: stage 2 step 1 splits
absent from present, step 3 splits present into running and not running, and
step 4 splits running into answering and not answering, so every path
through stage 2 lands on exactly one row above.

## Verification

Section 4.2.2's rule for every skill in this plugin -- "An action the skill
took did not verify: report the action, the verification that failed, and
what state the machine is now in. Never retry silently" -- is this skill's
central obligation, because its whole job is choosing and running one of
four targets. After running the target `## Decision` chose, this skill
re-reads container state with the same stage 2 primitives (`rdc_container_ids`
and, once resolved, `rdc_container_state`) and reports success only from
that fresh reading, never from the target's own exit code. A zero exit code
from `make build`, `make start`, `make restart` or `make reopen` is not
evidence the container is running afterward: `rdc_start` and `rdc_restart`
report success as soon as the underlying `docker start`/`docker restart`
call returns, with no re-inspection of their own after that point
(`container.sh:231-242` for `rdc_start`, `container.sh:257-263` for
`rdc_restart`), and `rdc_reopen`'s own exit code does not by itself confirm
the container is running afterward: by default it execs into the container
to read `$HOME` while seeding the VS Code server and dies if that exec
cannot read it (`container.sh:552-553`), so on that default path a zero exit
does carry some evidence the container answered, but under
`SKIP_VSCODE_SERVER_SEED=1` that exec is skipped entirely and the container
is probed not at all (`container.sh:545-546`) -- so this skill's own
post-action read is not redundant with any of the three, even though
`rdc_build`'s own trailing `rdc_status` call (`container.sh:806-808`) happens
to already re-read state after a successful build. This skill performs its
own fresh read uniformly after all four targets rather than relying on that
one target's internal behavior. When the fresh read disagrees with what the
chosen target should
have produced -- built, started or restarted but not running afterward, or
reopened but no longer running at all -- this skill reports the target that
ran, the state the fresh read found, and that the two disagree, and it never
reruns the target on its own to try again.

## Failure semantics

| Condition | Behavior |
|---|---|
| A precondition check fails | Stage 1's delegated `/devcontainer:engine` verdict is this skill's only precondition. A failure there stops this skill before stage 2 ever reads the container: print the failing check's message and remedy unchanged, and state that no target in `## Decision` was attempted, naming all four (`## Diagnosis`, stage 1). |
| An action the skill took did not verify | Report the target that ran, the state the post-action read found, and that the two disagree; never retried silently (`## Verification`). This is also this skill's answer to two task-specific conditions: a target that exits zero while the container is not running afterward is reported this way, never as success from the exit code alone; and `make build` refusing because a container already exists (a race between stage 2's read and the target's own guard, `container.sh:791-798`) is treated as a diagnosis that was already wrong the instant the guard fired -- this skill re-reads container state (`## Diagnosis`, stage 2) and switches to `make start` or `make reopen` based on what that fresh read shows, never passing a force flag to get past the guard, because the refusal is the guard working. |
| A step needs the operator (SSO, `sudo`, a key) | Two distinct moments reach this row. First, any operator-facing remedy `/devcontainer:engine`'s own stage 1 verdict names (an SSO login, starting the local engine, freeing disk space) is stated, waited on, and re-verified by re-invoking `/devcontainer:engine` before this skill's own diagnosis can proceed -- never assumed to have succeeded, per Section 4.2's interaction contract quoted in this document's introduction. Second, `rdc_require_container` itself needs the operator when more than one container matches and `CONTAINER=` is not set, or when a supplied `CONTAINER=` name matches nothing (`## Diagnosis`, stage 2 step 2): its own failure already states every candidate and the exact `CONTAINER=<name>` invocation to disambiguate, printed unchanged, and this skill waits for the operator to supply it and re-invoke rather than guessing. |
| A gate is reached (Section 4.4) | This skill reaches no Section 4.4 gate: it creates no AWS resource, runs no `terragrunt apply` or `terragrunt destroy`, and performs no phase 4 cutover. `GATE-APPLY`, `GATE-CUTOVER` and `GATE-DESTROY` are `/devcontainer:setup-remote`'s and a future teardown skill's concern, not this one's. |
| An answer fails validation | This skill asks nothing of its own (Section 4.2's own roster row: "Asks: Nothing") and never calls `answers.validate`. The one question this plugin ever asks under ambiguity -- which remote instance, Section 4.1.1 -- belongs to `/devcontainer:engine`'s own stage 1 invocation, not to this skill; this skill never re-asks it. |
| The operator aborts | An abort during stage 1 or stage 2 of `## Diagnosis` leaves no state changed, since both stages only read: `/devcontainer:engine`'s own delegated verdict and this skill's own container reads perform no action of their own. An abort after this skill has started running the target `## Decision` chose may leave that target's own effect complete or partial, exactly as if the operator had run `make build`, `make start`, `make restart` or `make reopen` directly and interrupted it themselves; this skill retries nothing on its own, and its next invocation reads the machine's actual state fresh (`## Diagnosis`, stage 2) rather than assuming the interrupted run finished. |

## Related specifications

- Section 4.2, `repos/spec/devcontainer-platform.md`: `launch` asks nothing,
  diagnoses, then builds, starts or reopens, and ends with a running
  container; the interaction contract this document's introduction quotes.
- Section 4.2.1: the thirteen checks `/devcontainer:engine` owns, delegated
  by reference in `## Diagnosis` stage 1 rather than restated.
- Section 4.2.2: the failure-semantics table this document's own `##
  Failure semantics` table instantiates for this skill's five conditions.
- Section 3.3: `rdc_backend`, `rdc_container_ids`, `rdc_require_container`,
  and `CONTAINER=` support, the primitives `## Diagnosis` stage 2 reads
  through rather than resolving a container a second way.
- Section 3.1: `rd_devcontainer_up`, the wrapper `rdc_build_local` and
  `rdc_build_remote` use to translate a devcontainer CLI failure into a
  diagnosed remedy, which this skill relies on rather than reimplementing.
- Section 2: G1 and G2's happy paths end at "Ready. Run: `make build`" before
  this skill exists, and at a repeatable diagnosis once it does.
- AC-3.1 (Section 3): no primitive reinvented; `## Diagnosis` stage 2 cites
  the exact `container.sh` line ranges it reuses rather than a new
  resolution scheme.
