---
name: teardown
description: Destroys container state (`make clean` or `make rebuild`) only after an inventory-grounded confirmation with no default answer, explaining the unpushed-work and uncommitted-config guards rather than only enforcing them, and reports what was destroyed and what survived from a fresh post-operation read; never destroys an instance.
---

# teardown

Section 4.2's own roster row gives `teardown` a column no other skill in it
carries: it "Asks" not "Nothing" but "Confirmation, always." It "Does"
`clean` or `rebuild`, "explaining the work-loss guards rather than only
enforcing them," and it "Ends by" stating "What was destroyed and what
survived." Both operations this skill runs destroy container state, and the
case where a confirmation feels unnecessary is exactly the case where the
operator's own model of what exists is most likely wrong -- so this skill
takes an inventory first (`## Inventory`) and confirms against that
inventory (`## Confirmation`), never against the bare word `clean` or
`rebuild`.

`make clean` already refuses when the volume holds unpushed work, and
`make rebuild` already refuses when `.devcontainer` holds an uncommitted
edit (`## Guards`); this skill re-implements neither refusal, since doing so
would be exactly the duplicated primitive Section 3 forbids. What the two
targets cannot do on their own is say why each guard exists, what `FORCE=1`
gives up, and what the alternative costs -- one push from inside the
container, or one commit and push of `.devcontainer`, neither of which loses
anything. This skill states the commits or the diff it found, the exact
command that resolves it, and exactly what `FORCE=1` would discard, and it
never sets `FORCE` itself: an override is a decision only the operator
makes, in their own shell, after being told what it costs.

Scope is container-level only (`## Scope`): the two targets this skill runs
destroy or replace a container, its private volumes and its image, never an
AWS resource. Destroying an *instance* is a `terragrunt destroy` behind a
Section 4.4 phase gate that a request to "tear it all down" could also mean;
this skill states that boundary explicitly rather than silently acting at
the smaller, container-only scope it can actually reach.

Verification closes every run (`## Verification and report`): after the
operation, this skill re-reads state with the same primitives `## Inventory`
already used and reports success only from that fresh reading, never from
`rdc_clean`'s or `rdc_build`'s own exit code. Section 4.2.2 forbids reporting
success from an action that did not verify, and a `clean` that left a volume
behind is exactly the case where the exit code and the truth disagree.

## Inventory

Before asking anything, this skill reads state through the Section 3.3
primitives, reusing `make status` (`rdc_status`, `container.sh:184-219`) as
it stands rather than re-deriving the container id, its image or its
volumes a second way:

1. **Does a container exist at all?** `make status` reports "Container
   none, create with Dev Containers: Clone Repository in Container
   Volume..." (`container.sh:204-206`) exactly when `rdc_container_ids`
   (`container.sh:119-121`) finds none. When it does, this skill states
   there is nothing to destroy and stops -- the same outcome a real
   `make clean` invocation reaches on its own no-container branch
   (`rdc_clean`, `container.sh:813-822`), which also removes an orphaned
   workspace volume named by `rdc_workspace_volume`
   (`container.sh:318-320`) if one survived a previous run, so this skill
   names that same possibility rather than only the container's own
   absence. When more than one container carries the project's label,
   `make status` prints one block per id it finds (`container.sh:209-217`),
   and this skill cannot name a single container, image and volume set for
   `## Confirmation` until `CONTAINER=<name>` disambiguates which one --
   the same requirement `rdc_require_container` (`container.sh:123-158`)
   already enforces for `rdc_clean` itself, reused here rather than a
   second resolution scheme (Section 3.3's own `CONTAINER=` support).
2. **What will be destroyed.** Once a container exists, `make status`'s own
   `Container`, `Image` and `Volumes` lines are this half of the inventory
   as-is: the `Volumes` line is already the *private* volumes alone, since
   `rdc_project_volumes` (`container.sh:168-182`), the function `rdc_status`
   calls to build it, already excludes every name in `SHARED_VOLUMES`
   (`container.sh:13`, default `minikube-config vscode`, overridable, never
   hardcoded here) and every mount under `VSCODE_SERVER_DIRNAME`
   (`container.sh:14`, default `.vscode-server`). This skill states the
   three exactly as `make status` reports them: the container (name,
   state), its image (the project-specific tag `docker inspect
   --format '{{.Config.Image}}'` reports, never the cached base layer), and
   its private volumes.
3. **What will survive.** The shared volumes `SHARED_VOLUMES` names and the
   cached base image `CLONE_IMAGE` (`container.sh:18`, default
   `mcr.microsoft.com/devcontainers/base:noble`) are not re-inspected,
   since nothing in `## Guards` or `rdc_clean` ever destroys either one --
   this is exactly what `make clean`'s own `make help` line already states
   verbatim: "Destroy the container, its private volumes and its image.
   Shared volumes and the base image are kept." (`Makefile:72`).
4. **Guard pre-flight, before confirmation is ever asked.** With a
   container confirmed present, this skill also runs `make check`
   (`rdc_check`, `container.sh:265-297`) as part of this same inventory
   read, the identical reuse `/devcontainer:doctor`'s own "Container state"
   finding already makes of this primitive, rather than waiting for
   `rdc_clean`'s own internal call to discover the same guard later. On a
   local backend this always reports "local backend, the container shares
   this working tree, nothing to check" (`container.sh:267-269`) and the
   inventory proceeds straight to confirmation; on a remote backend a
   non-empty result here is `## Guards`' first row failing, reported and
   stopped on before confirmation is ever asked, since there is nothing
   left to responsibly confirm destroying while that guard is unresolved.
   When the requested operation is `rebuild`, this skill also pre-flights
   `## Guards`' second row here, before confirmation, using the technique
   `## Guards` describes; when the requested operation is `clean`, that
   second row is never pre-flighted, because `rdc_clean` never reaches it
   either (`## Guards`).

## Confirmation

Confirmation is required for both `clean` and `rebuild`, unconditionally --
Section 4.2's own roster row: "Asks: Confirmation, always." It is taken
against the inventory `## Inventory` just built: the container, its private
volumes and its image named individually as what will be destroyed, and the
shared volumes and base image named as what will survive, not the bare word
`clean` or `rebuild` on its own. A confirmation given against a list the
operator can check against what they actually expect to be running is
informed in a way a confirmation given against a verb never is.

No default answer exists. An empty response, a bare press of return, or an
answer that is not an unambiguous affirmative is treated exactly as a
decline, never as consent -- this skill never proceeds on silence or on
anything it cannot read as a clear "yes." On decline or on an unclear
answer, this skill runs neither `make clean` nor `make rebuild`, reports
plainly that nothing was destroyed, and the inventory already read in
`## Inventory` remains the current, unchanged state of the machine.

Confirming does not itself pass `FORCE=1`. If, once confirmed, the target
this skill runs still refuses on one of `## Guards`' two rows (a guard
`## Inventory` step 4 could not pre-flight, or one whose state changed
between the inventory read and the run), this skill reports that refusal
exactly as `## Guards` states it and stops; it never retries the same run
with `FORCE=1` substituted in on the operator's own behalf.

## Guards

Both operations this skill runs are gated by the same two guards
`make help`'s own `FORCE=1` line names together: "Proceed past the
unpushed-work and uncommitted-config guards." (`Makefile`'s `OPTIONS`
section, spec Section 14.1). This skill sets `FORCE` on neither guard,
ever; every remedy below is stated to the operator, who runs it themselves.
Every "local backend"/"remote backend" split below reads the identical
`rdc_backend` result (`container.sh:99-105`, spec Section 3.3) `rdc_check`'s
own `[ "$(rdc_backend)" != "remote" ]` (`container.sh:267`) and
`rdc_build_prereqs`'s own `[ "$(rdc_backend)" = "remote" ]`
(`container.sh:328`) already branch on; this skill resolves it no
differently than the target it is about to run.

| Guard | Protects | Satisfied properly by | What `FORCE=1` discards |
|---|---|---|---|
| Unpushed commits (checked on the host and inside the container's own checkout) and uncommitted changes (checked inside the container's own checkout) | Commits or changes made from inside the container (an editor session, or a terminal opened with `make exec`) that exist nowhere else, checked by `rdc_check` (`container.sh:265-297`) whenever `rdc_clean` runs it (`container.sh:826-830`) -- for both `make clean` on its own and for `make rebuild`'s own leading call to `rdc_clean` (`container.sh:851-858`). Meaningful on the remote backend only: on local the container bind-mounts the host's working tree directly rather than holding an independent clone, so `rdc_check` reports "local backend, the container shares this working tree, nothing to check" and returns before this guard has anything of its own to check (`container.sh:267-269`). A second, host-side instance of the same underlying concern -- commits on this machine's own checkout that are not on `origin/<branch>` -- is checked separately by `rdc_build_prereqs` (`container.sh:338-350`) before `make rebuild`'s `build` half runs; `make clean` alone never reaches it, since `rdc_clean` never calls `rdc_build_prereqs`. | Push from inside the container: `docker exec -u ${CONTAINER_USER} <container-name> git -C ${CONTAINER_WORKSPACE} push` -- the exact command `rdc_check`'s own remedy states (`container.sh:290-293`). For the host-side instance, push this machine's own branch: `git push origin <branch>` (`container.sh:346-347`). | `rdc_clean` never even calls `rdc_check` under `FORCE=1` (`container.sh:826-829`), so whichever `rdc_check` would have listed is discarded unseen: the unpushed commits (`ahead`, `container.sh:279`), the uncommitted changes (`dirty`, `container.sh:278`), or both, exactly as `rdc_check`'s own combined `rd_fail` names them (`container.sh:284-296`). The host-side instance is likewise skipped entirely under `FORCE=1` (`container.sh:340`), discarding the same guarantee that every commit on this machine reached origin before the container is rebuilt from origin. |
| Uncommitted `.devcontainer` configuration | The guarantee that the container's own configuration matches what git records, checked by `rdc_build_prereqs` (`container.sh:352-362`), which `rdc_build` calls (`container.sh:789`) and which `rdc_rebuild` also calls directly, before `rdc_clean` ever runs (`container.sh:851-852`) -- the same order `make rebuild`'s own `make help` line already promises: "Prerequisites are checked before anything is destroyed" (`Makefile:69`). Remote backend only (`container.sh:328`); never reached by `make clean` alone, since `rdc_clean` never calls `rdc_build_prereqs`. The container is cloned fresh from origin at build time, but its `Dockerfile`/`devcontainer.json`/feature configuration is read from the local `.devcontainer` directory on the operator's own machine at that same moment (`container.sh:352-353`); an edit there that is not yet committed is baked into the rebuilt container while nothing on origin ever reproduces it. | Commit and push the `.devcontainer` change before rebuilding -- `rdc_build_prereqs`'s own stated remedy (`container.sh:358-361`). | The check is skipped entirely under `FORCE=1` (`container.sh:354`), so the rebuilt container is built from `.devcontainer` content that exists only on this operator's own machine, with nothing in git ever able to reproduce it. |

For `make rebuild`, this skill pre-flights the second row in `## Inventory`
step 4 by invoking `make build` against the already-existing container
before ever asking for confirmation: `rdc_build` runs `rdc_build_prereqs`
first and its own already-exists refusal second (`container.sh:788-798`),
so on an existing container this call either surfaces the same guard
`make rebuild` would have hit, or refuses harmlessly with `rdc_build`'s own
"already exists on this engine" message (`container.sh:792-798`) once the
guard passes -- never building a second container and never destroying
anything either way. The same `make build` call can also surface a missing
prerequisite unrelated to either guard (`rd_require_cmd`,
`container.sh:323-326`: the devcontainer CLI, git, jq or python3 absent);
this skill reports that condition the way Section 4.1.4's own table already
does for "Missing prerequisite," never mistaking it for one of the two
guards above.

## Scope

This skill destroys container state only: the container, its private
volumes and its image, exactly what `## Inventory`'s destroyed list names,
never an AWS resource. Destroying an *instance* is a separate operation, a
`terragrunt destroy` run against `remote-instances/<name>/`, gated behind
GATE-DESTROY (Section 4.4): "Operator confirms, and the volume holds no
unpushed work." This repository's own real-hardware acceptance work
(`E6-F3-S3-T2`) names that same two-part requirement `PRECHECK-DESTROY` and
requires recording both conditions -- the confirmation, and the work-loss
check run and found clean *before* the confirmation is even requested --
verbatim in that task's own `## Comments`; that recording is a human
operator's and that task's own responsibility, never something this skill
decides or records on the operator's behalf. `terragrunt destroy` belongs to that
work, not to this skill: this skill runs no Terragrunt command of any kind
and reaches no Section 4.4 gate itself (`## Failure semantics`).

When a request is ambiguous between the two -- "tear it all down" could mean
either -- this skill states the boundary above and asks which is meant,
rather than silently acting at the smaller, container-only scope it can
actually reach. The same "no default answer" rule `## Confirmation` states
for destruction applies here too: an unclear answer to which scope was
meant is not read as consent to the container-only meaning by default.

## Verification and report

After `make clean` or `make rebuild` runs, this skill re-reads state with
the same primitives `## Inventory` used and reports success only from that
fresh reading, never from `rdc_clean`'s or `rdc_build`'s own exit code.
`rdc_clean`'s own trailing `rd_ok "torn down, shared volumes (...) ... were
kept"` (`container.sh:848`) is printed once every removal call above it
(`container.sh:836-847`: `rd_docker rm -f`, `rd_docker volume rm`,
`rd_docker rmi`) itself returns zero; what a zero exit there cannot rule out
is exactly Section 4.2.2's own condition -- a docker call that exits zero
while the resource it targeted is still reachable a moment later (a stale
reference, a race with something else recreating a volume of the same
name). This skill's own fresh `make status` after the run is what actually
closes that gap, the same reason `/devcontainer:launch`'s own
`## Verification` section already gives for never trusting a target's exit
code alone: a re-read after the fact is not redundant with what the target
itself claims.

- **After `make clean`:** the fresh `make status` must show no container
  for the project, and, when `CONTAINER` was unset, no orphaned workspace
  volume either (mirroring `rdc_clean`'s own orphan-volume branch,
  `container.sh:813-822`). The survived half is confirmed by that same
  fresh read still naming the `SHARED_VOLUMES` volumes; the base image
  needs no separate check, since nothing in `## Guards` or `rdc_clean` ever
  removes a cached layer, only the one image tag the destroyed container
  used.
- **After `make rebuild`:** the fresh `make status` must show a new
  container, built from the same image and configuration `## Inventory`
  read before this run started. `rdc_build`'s own trailing `rdc_status`
  call (`container.sh:806-808`) already prints this, but this skill
  performs its own read anyway rather than relying on that call, for the
  identical reason stated above.
- **When the fresh read disagrees** -- an artifact `## Inventory` named as
  destroyed is still present after the operation reported success -- this
  skill reports the artifact that survived and the exact command that
  removes it, and does not report the teardown as complete. This is
  Section 4.2.2's own rule ("report the action, the verification that
  failed, and what state the machine is now in. Never retry silently")
  applied to the one condition this document's own Error Handling Contract
  names for it.

## Failure semantics

| Condition | Behavior |
|---|---|
| A precondition check fails | Both rows in `## Guards` are this skill's own precondition checks. The unpushed-work row is pre-flighted during `## Inventory` before confirmation is ever asked, for both operations; the uncommitted-config row is pre-flighted during `## Inventory` too, but only when the requested operation is `rebuild`, using the `make build` technique `## Guards` states. On either guard's failure at any point (pre-flight or the target's own later, identical check): stop, list the commits or the `.devcontainer` diff, name the exact remedy, state exactly what `FORCE=1` would discard, and never set `FORCE` on the operator's behalf. No confirmation is asked while a guard is unresolved, and `## Verification and report` never runs. |
| An action the skill took did not verify | After `make clean` or `make rebuild` runs, this skill re-reads state (`## Verification and report`) and reports success only from that fresh reading. When the fresh read finds an inventoried artifact still present, this skill reports the artifact that survived and the exact command that removes it, and does not report the teardown as complete -- never retried silently a second time. |
| A step needs the operator (SSO, `sudo`, a key) | Every dispatch case this skill uses (`check`, `build`, `clean`, `rebuild`) is itself gated on `rdc_require_docker` (`container.sh:107-117`, invoked from the dispatch `case` at `container.sh:874-881`) before running; an unreachable engine there reports the same diagnosis `rd_engine_diagnosis` produces (Section 3.1), and this skill states it unchanged -- an operator action (start the engine), waited on, then re-verified with a fresh `make status` before this skill's own inventory proceeds, never assumed to have succeeded. A remote `rebuild`'s own `rdc_ensure_secrets_current` step (`container.sh:365-401`) needs a valid AWS credential to publish `shell.env`; a failure there is reported and waited on the same way (`aws sso login`), before `rdc_clean` or `rdc_build` ever run. Within `## Guards` itself, the operator action this row names is pushing from inside the container, pushing this machine's own branch, or committing and pushing `.devcontainer` -- each stated exactly, waited on, and never assumed to have succeeded; the operator resolves it and re-invokes this skill for a fresh inventory and pre-flight rather than this skill polling or retrying on its own. |
| A gate is reached (Section 4.4) | This skill reaches no Section 4.4 gate: it creates and destroys no AWS resource and runs no `terragrunt apply` or `terragrunt destroy`. GATE-DESTROY governs instance destruction, which `## Scope` places entirely outside this skill; a request meaning that instead is told the boundary and asked which was meant, rather than this skill silently proceeding at the smaller scope it can actually reach. |
| An answer fails validation | The confirmation is the one thing this skill ever asks (Section 4.2's own roster row: "Asks: Confirmation, always"). Anything other than an explicit, unambiguous affirmative -- silence, an empty answer, an ambiguous one -- is treated as a decline, never as consent (`## Confirmation`). This is also this document's own Error Handling Contract row for a declined or ambiguous confirmation, and it is reported the same way the row below states. |
| The operator aborts | Whether by an explicit decline, an unclear answer, or aborting mid-interaction before a decision is reached, this skill runs neither `make clean` nor `make rebuild`: it reports that nothing was destroyed, and the inventory already read in `## Inventory` remains the current, unchanged state of the machine -- verified against a fresh `make status` read matching the one taken before confirmation was asked. |

## Related specifications

- Section 4.2, `repos/spec/devcontainer-platform.md`: `teardown`'s own
  roster row, quoted in this document's introduction, and the interaction
  contract every skill obeys.
- Section 4.2.2: the failure-semantics table this document's own
  `## Failure semantics` table instantiates.
- Section 4.1.4: the error handling contract for "Unpushed work in a
  volume" (exit 1, the commits and the push command to run inside the
  container), the exact shape `rdc_check`'s own remedy already follows
  (`## Guards`).
- Section 4.4: `GATE-DESTROY`, the two-part requirement (`## Scope`) this
  skill never reaches itself, and the phase-gate rule that an agent
  reaching a gate stops, presents the evidence, and never self-approves.
- Section 3.3: `rdc_backend`, `rdc_container_ids`, `rdc_require_container`,
  `rdc_workspace_volume`, the primitives `## Inventory` and
  `## Verification and report` reuse rather than resolving container state
  a second way.
- Section 14.1: `FORCE=1`'s own `make help` line, "Proceed past the
  unpushed-work and uncommitted-config guards," the two rows `## Guards`
  names.
- AC-2.1 (Section 2) and AC-3.1 (Section 3): no goal accepted on inspection
  alone, and no primitive in `## Inventory`, `## Guards` or
  `## Verification and report` reimplemented rather than reused.
- AC-4.4 (Section 4.4): no autonomous run performs a `terragrunt apply` or
  `terragrunt destroy` without a recorded gate confirmation -- this skill
  performs neither, ever, per `## Scope` and `## Failure semantics`.
