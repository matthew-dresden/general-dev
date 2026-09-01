---
name: engine
description: Validates the Section 4.2.1 contract end to end -- thirteen checks, six local and seven remote -- and reports a per-check verdict table; the validation contract every other devcontainer skill reuses.
---

# engine

`/devcontainer:engine [INSTANCE=<name>]` is the skill Section 4.2's own roster
line names first for a reason its own words state: "`engine` defines the
validation contract the others reuse" (spec Section 4.2.1). It decides nothing
about what a valid answer looks like, what a rendered file contains, or what
"complete" means -- `verify` (`.claude/plugins/devcontainer/scripts/devcontainer_config/`)
owns those questions, and `hostprobe` owns every fact about this machine this
skill can currently obtain in tested Python. This skill's only job is walking
the thirteen checks in dependency order, stopping at the first failure, and
reporting a verdict; it asks nothing of its own beyond the one question
Section 4.1.1 already defines.

Two of the thirteen checks -- disk headroom, and `HOST_PROXY` agreeing with a
reachable proxy -- are host facts no module in this repository probes yet.
`hostprobe`'s own module docstring says so directly: "Disk headroom and
`HOST_PROXY` reachability are host facts this module does not probe, and
neither is answered by `verify` either: no E1 unit owns them yet." This is the
same situation `/devcontainer:setup-remote` was authored under for the
`certs` and `instances` modules (spec Section 4.5). `certs` (E6-F1-S1-T1)
now exists for certificate generation, but neither `instances` nor the
inspection/expiry-status half of `certs` (`make cert-status`'s own
"present and unexpired" check, E6-F1-S1-T2) exists yet, so every
`## Checks` row and `## Procedure` step below that depends on one of those
two still-missing pieces names the module that will own it -- `hostprobe`
for the two host facts above, `instances` for remote instance resolution,
`certs` for certificate expiry inspection, and the SSM port forward manager
(E6-F2-S1-T1) for the tunnel -- rather than a function this repository does
not yet contain. Naming the owner is this row's job; implementing the probe
is a later work unit's.

Where this skill cannot act itself -- installing an absent tool, starting a
stopped engine, a browser SSO login, issuing a certificate, resolving an
instance -- it states the exact command, waits for the operator to act, then
re-verifies itself before continuing (spec Section 4.2's interaction
contract). It never asks the operator to re-invoke `/devcontainer:engine` to
force a re-check, and it never assumes an action succeeded. The only two
actions this skill performs on its own, because both are reversible and need
no operator credential, are selecting an existing docker context and
re-establishing a port forward; see `## What this skill fixes itself`.

Every failure this skill states follows one of the two shapes Section 3.1's
primitives already define rather than a new error printer: `rd_die`'s
one-line fatal shape (`lib.sh:16-19`) for a single missing prerequisite, and
`rd_fail`'s framed, multi-line shape naming cause and remedy (`lib.sh:30-41`)
for everything else. `rd_require_cmd` (`lib.sh`) is the prerequisite-check
shape the tool-presence check below follows, and `rd_engine_diagnosis`
(`lib.sh`) is the diagnosis the engine-unreachable check below carries,
per the Section 4.1.4 error handling contract for every target ("Engine
unreachable | 1 | The context name and the diagnosis from
`rd_engine_diagnosis`"), rather than a generic connection error.

## Checks

The table below is Section 4.2.1's own two lists, verbatim and in order: the
six local checks, then the seven checks the remote path adds. Every row names
what its failure prevents and its prerequisite check (its `Depends on:`
prefix in the `Prevents` cell) in addition to the columns
`tests/test_skill_lint.py`'s `## Checks` rule inspects directly (`Check`,
`Failure message`, `Remedy`), so ordering is a property of this table, not
merely of the `## Procedure` section that walks it.

| Check | Prevents | Failure message | Remedy |
|---|---|---|---|
| docker CLI present and a context selected | Depends on: none -- the first check in dependency order. Prevents: "every step of setup-local, setup-remote and doctor that needs docker" (`hostprobe._probe_one_tool`'s prevents text, verbatim, for `spec.name='docker'`), and "rdc_backend from determining local against remote (spec Section 1.1)" (`hostprobe._probe_selected_context`'s prevents text, verbatim). | Either `docker is not on PATH` (`hostprobe.probe_tools`'s docker entry, or `hostprobe._docker_absent_result`) or `docker context show reported no context` (`hostprobe._probe_selected_context`) -- both are this check's failure, and both are reported by name so the operator or this skill knows which condition it is. | If docker is absent: install it with the command `hostprobe.probe_tools`'s docker entry names (sourced from `hostprobe._TOOL_SPECS`), state that command, wait for the operator, then re-probe with `hostprobe.probe_tools` before continuing -- an operator action, not a self-fix. If docker is present but no context is selected: this skill selects the context the backend it is validating already names (`LOCAL_DOCKER_CONTEXT` for a local run, the resolved instance's `general-dev-<name>` context for a remote run) with `docker context use <name>` itself, then re-probes with `hostprobe.probe_docker` before continuing -- one of the two actions `## What this skill fixes itself` names. |
| `LOCAL_DOCKER_CONTEXT` exists | Depends on: `docker CLI present and a context selected`. Prevents: "setup-local/setup-remote from selecting the engine LOCAL_DOCKER_CONTEXT or REMOTE_DOCKER_CONTEXT names" (`hostprobe._probe_named_context`'s prevents text, verbatim). | The configured `LOCAL_DOCKER_CONTEXT` is not among the configured docker contexts (`hostprobe._probe_named_context`'s `found` shape: `f"{requested_context} is not among the configured contexts: {listed!r}"`). | Create it (`docker context create`) or correct `LOCAL_DOCKER_CONTEXT` in `.devcontainer/remote-docker/config.env` to one of the contexts `hostprobe._probe_named_context`'s remedy lists. This skill never creates a context itself: state the exact command, wait for the operator, then re-probe with `hostprobe.probe_docker(runner, requested_context=<LOCAL_DOCKER_CONTEXT>)` before continuing. Once this check passes and the confirmed context is not yet the active one, this skill selects it itself with `docker context use <LOCAL_DOCKER_CONTEXT>` -- the same self-fix action the row above names -- before moving on to the next check. |
| the engine answers | Depends on: `docker CLI present and a context selected` and `LOCAL_DOCKER_CONTEXT exists`. Prevents: every command that needs the docker daemon to be reachable: build, exec, status (`hostprobe._probe_docker_handshake`'s prevents text, verbatim). | `no response within {timeout:g}s (DOCKER_HANDSHAKE_TIMEOUT)` (`hostprobe._probe_docker_handshake`'s timeout shape), reported against the active local context; an unreachable engine additionally carries the diagnosis `rd_engine_diagnosis` (`lib.sh`, spec Section 3.1) produces for that context, per Section 4.1.4's error handling contract, rather than a generic connection error. | Confirm the engine behind the active docker context is running (start OrbStack, Docker Desktop, or `dockerd`, whichever is installed) -- an operator action, not a self-fix. State that exact action, wait, then re-probe with `hostprobe.probe_docker` before continuing. `DOCKER_HANDSHAKE_TIMEOUT` governs how long this waits, read fresh by `hostprobe._docker_handshake_timeout_seconds` rather than cached, so it is never a literal at this call site. |
| disk headroom | Depends on: `the engine answers` -- headroom on the filesystem behind an engine that cannot be reached is not a fact this check can obtain. Prevents: `make build` or any later `docker build`/`docker run` failing mid-operation with "no space left on device" (the same condition `rd_docker_failed`'s `*'no space left on device'*` case in `lib.sh` already diagnoses after the fact, spec Section 3.1) instead of being caught before it starts. | Free space on the filesystem backing the active docker engine's data root is below this check's minimum-headroom threshold. `hostprobe`'s own module docstring already disclaims probing this today ("Disk headroom ... are host facts this module does not probe ... no E1 unit owns them yet"); no function of this name exists in this repository yet, so this row states the check's exact contract and names `hostprobe` as the module Section 4.5 will assign it to, per this document's introduction. The minimum-headroom threshold itself has no Section 7.3 variable yet; this document does not invent one ahead of the work unit that implements the probe. | Reclaim space (`docker system prune`, the same remedy `rd_docker_failed` already gives for "no space left on device", spec Section 3.1) or free host disk space, then this skill waits and re-probes disk headroom itself before continuing -- an operator action. Once the probe exists, the minimum-headroom threshold is read fresh from the environment via its own Section 7.3 variable, the same way `DOCKER_HANDSHAKE_TIMEOUT` is read fresh above, so no headroom value is ever a literal at a call site. |
| the three files complete and consistent | Depends on: none -- independently testable of every docker check above; reads only the three files `repo.PRIVATE_FILES` names. Prevents: the container's postCreate step, which reads all three, from ever running against complete, placeholder-free, mutually consistent configuration (`verify`'s own module docstring, spec Section 4.5). | The first `verify.Finding` `verify.verify_all` returns, shaped `<check>: <found>` -- for example `completeness: shell.env exists and is readable: shell.env could not be read: <OSError>` (`verify._completeness_finding`), `no placeholders: shell.env: shell.env still contains <token>` (`verify._placeholder_findings`), or `consistency: BASH_ENV matches repo.container_workspace: shell.env sets BASH_ENV to '...', expected '...'` (`verify._bash_env_finding`) -- `verify.verify_all` reports every finding, not only the first (spec Section 4.2.2). | Report every `Finding.remedy` `verify.verify_all` returns, not only the first; each already names either fixing the value directly or re-rendering. A remedy naming a re-render is never bare: `render.write_all` is always called with `overwrite=False`, so it refuses and names every existing path rather than replacing one already on disk (`/devcontainer:setup-local`'s own `## Checks` table states the same caveat for its identical findings) -- the operator moves the three private files aside first, then re-runs `/devcontainer:setup-local` (or `/devcontainer:setup-remote`) against the clean tree. State the remedies in that order, wait for the operator to act, then re-run `verify.verify_all` before continuing -- an operator action, not a self-fix: this skill never writes to the three files itself. |
| `HOST_PROXY` agrees with a reachable proxy | Depends on: none -- independent of every docker, disk and file check above; reads `shell.env`'s `HOST_PROXY`/`HOST_PROXY_URL` pair and reachability, not a docker or AWS fact. Prevents: `make build`, and every command run inside the container, reaching for a proxy `shell.env` claims exists but that is not actually there -- `docs/environment-files.md`'s own `HOST_PROXY` row: "`true` requires a reachable `HOST_PROXY_URL` or the build fails." | `shell.env` sets `HOST_PROXY` to `'true'` but `HOST_PROXY_URL` did not answer within the probe's timeout. `hostprobe`'s own module docstring already disclaims probing this today (the same sentence disk headroom's row quotes); no function of this name exists in this repository yet, so this row states the check's exact contract and names `hostprobe` as the module Section 4.5 will assign it to. | Start the host-side proxy (`docs/environment-files.md`'s proxy section names the exact command per host family) or correct `HOST_PROXY_URL` in `shell.env`, then this skill waits and re-probes before continuing -- an operator action. |
| SSO session valid | Depends on: none among the local checks above -- the first remote-only check, run only when step 1 of `## Procedure` resolves the backend to remote. Prevents everything remote depends on it: the SSM tunnel, Parameter Store, and the build that publishes to it (`hostprobe.probe_aws_identity`'s prevents text, verbatim). | The SSO session for the resolved `remote_aws_profile` has expired (`hostprobe.probe_aws_identity`'s `found` shape: `f"the SSO session for profile '{profile}' has expired"`); `aws is not on PATH` and `profile '<profile>' has no credentials at all` are the other two shapes the same function returns for this same check. | State `aws sso login --profile <profile>` with the resolved `remote_aws_profile` substituted (`hostprobe.probe_aws_identity`'s own `login_command`), wait for the operator to complete the browser login, then re-probe with `hostprobe.probe_aws_identity(runner, profile=<profile>)` before continuing -- an operator action. Never proceeds on the assumption the login succeeded, and never falls back to any other credential source. |
| instance exists, is running, SSM agent online | Depends on: `SSO session valid`. Prevents the port forward and the docker version handshake, since neither can reach an instance whose SSM agent is not reporting online -- owned by the `instances` module (spec Section 4.5) once it exists; no function of this name exists in this repository yet, per this document's introduction. | The resolved `remote_instance_id` does not resolve to a running instance, or the instance is running but its SSM agent is not online. | Stop, name the instance id and the agent state, and state that the port forward cannot be established until the agent reports online -- an operator action; this skill does not attempt to start an instance or its SSM agent itself. Do not attempt the forward: its failure would name the wrong cause. |
| port forward established and the local port answering | Depends on: `instance exists, is running, SSM agent online`. Prevents `make build INSTANCE=<name>`, and every command that needs the daemon reachable through the tunnel, from ever reaching the instance -- owned by the SSM port forward manager (E6-F2-S1-T1) once it exists; no function of this name exists in this repository yet. | The SSM port forward to the instance's `DOCKER_TLS_PORT` did not answer within `SSM_FORWARD_TIMEOUT` (the same deadline `/devcontainer:setup-remote` step 12 already waits on, spec Section 7.3). | This skill re-establishes the forward itself -- the second of the two actions `## What this skill fixes itself` names, since it is reversible and needs no operator credential beyond the already-verified SSO session. If the forward still does not answer after being re-established, stop, name the instance and the allocated local port (spec Section 9: "never a fixed number"), and state that the checks depending on this one (the tunnel handshake, remote disk headroom, unpushed work) were not run. |
| client, server and CA certificates present and unexpired | Depends on: none among the remote checks above -- reads only the local certificate files under `~/.docker/certs/<name>/` once `<name>` is resolved (Section 4.1.1); needs neither the SSO session, the instance, nor the forward. Prevents `make build INSTANCE=<name>` presenting an expired or absent client certificate to a server that refuses it -- `certs.py` (E6-F1-S1-T1) exists for generation, but this check is the inspection/expiry-status half, and no function of this name exists in this repository yet (the still-missing half, E6-F1-S1-T2). | No certificate exists at the expected path under `~/.docker/certs/<name>/`, or one of the client, server or CA certificates has already expired. A certificate that has not yet expired but expires within `CERT_WARN_DAYS` is a `WARN`, not this failure; see `## Verdict`. | Issue or renew the missing or expired certificate with `certs.create_ca`, `certs.issue_server` or `certs.issue_client` (E6-F1-S1-T1). State the exact reissue action, wait for the operator to run it, then re-check the certificate's expiry before continuing -- an operator action. Never proceeds on the assumption that reissuing succeeded. |
| docker version handshake over the tunnel | Depends on: `port forward established and the local port answering`. Prevents `make build INSTANCE=<name>`, and every command that needs the daemon reachable through the tunnel (`hostprobe.probe_docker`'s handshake portion, run with the remote context selected). | The identical `no response within {timeout:g}s (DOCKER_HANDSHAKE_TIMEOUT)` string the local `the engine answers` row quotes (`hostprobe._probe_docker_handshake`), this time returned for the tunnel context `general-dev-<name>` rather than the local one. | Confirm the port forward is still established (`SSM_FORWARD_TIMEOUT` governs how long this skill waits for it) and that the daemon behind it is running, then re-probe with `hostprobe.probe_docker(runner, requested_context=<the created context>)` before continuing -- an operator action once the forward itself is confirmed established; re-establishing the forward is the self-fix the row above already performs. |
| disk headroom on the data volume | Depends on: `docker version handshake over the tunnel` -- headroom on a volume behind an engine that cannot be reached is not a fact this check can obtain. Prevents the instance running out of space mid-build or mid-clone, checked before the build rather than discovered during it. | Free space on the remote instance's data volume, checked over the docker context rather than the local filesystem, is below the same minimum-headroom threshold the local `disk headroom` row describes, applied to a different mount. Not yet implemented in this repository, the same disclaimed gap the local row quotes from `hostprobe`'s own module docstring. | Reclaim space inside the container (`docker system prune`, run over the resolved remote context) or grow the data volume, then this skill waits and re-probes before continuing -- an operator action. |
| no unpushed work in the volume | Depends on: `docker version handshake over the tunnel` -- listing commits inside the container needs the engine reachable to exec into it. Prevents `GATE-DESTROY` (spec Section 4.4) and a future teardown skill from destroying a volume that holds work nowhere else: "the volume holds no unpushed work" is one of `GATE-DESTROY`'s own requirements. | `N` commit(s) are not on `origin/<branch>` (the same shape `rdc_build_prereqs`'s `rd_fail` call already reports on the host side, `container.sh`, spec Section 3.1), this time listed from `git log --oneline origin/<branch>..HEAD` run inside the container over the resolved docker context rather than on the host. | List the commits and state the push command to run inside the container: `git push origin <branch>` (spec Section 4.1.4's own error handling contract for the "Unpushed work in a volume" row: exit 1, message contains the commits and the push command to run inside the container). This check fails rather than warns -- it never becomes a `WARN` row -- because a later teardown skill reads this verdict to decide whether destroying the volume is safe. Not a self-fix: state the exact commands, wait for the operator to push from inside the container, then re-run this check before continuing. |

## Procedure

1. Resolve which engine this run validates. Probe the active docker context
   with `hostprobe.probe_docker(runner)` (no `requested_context` yet) and
   apply `rdc_backend`'s own rule (spec Section 1.1: remote when the active
   context equals `REMOTE_DOCKER_CONTEXT`, local otherwise) to it. When the
   resolved backend is remote, resolve the instance per Section 4.1.1: the
   `INSTANCE` argument, then `DEFAULT_REMOTE_INSTANCE` from `shell.env`, then
   the sole directory under `remote-instances/` if exactly one exists;
   otherwise ask which instance -- the only question this skill ever asks.
   When the resolved backend is local, no instance resolution happens and
   only the six local checks below run.
2. Run `docker CLI present and a context selected`. Apply this row's
   `## Checks` remedy on failure: an operator action if docker is absent, or
   this skill's own context-selection self-fix if a context merely was not
   selected. Do not continue to step 3 until this check passes.
3. Run `LOCAL_DOCKER_CONTEXT exists` (local) or, for a remote run, the
   equivalent check that the resolved instance's `general-dev-<name>` context
   is configured (`hostprobe.probe_docker(runner, requested_context=<name>)`).
   Apply this row's remedy on failure. When it passes and the confirmed
   context is not yet active, select it (`docker context use <name>`) as this
   row's remedy also states, then re-probe before step 4.
4. Run `the engine answers` against the now-active, now-confirmed context.
   Apply this row's remedy (including `rd_engine_diagnosis`'s diagnosis) on
   failure.
5. Run `disk headroom` against the same engine. Apply this row's remedy on
   failure; state plainly when the underlying probe does not yet exist in
   this repository, per this document's introduction.
6. Run `the three files complete and consistent`: `verify.verify_all(root)`,
   reporting every `Finding` it returns, not only the first. Apply this row's
   remedy on any finding.
7. Run `HOST_PROXY agrees with a reachable proxy`. Apply this row's remedy on
   failure; state plainly when the underlying probe does not yet exist in
   this repository, per this document's introduction.
8. When the resolved backend is local, stop here and report the verdict
   (`## Verdict`): all six local checks ran and every dependent check that
   was never reached (none, on a local run) is named as not run and why.
9. When the resolved backend is remote, run `SSO session valid`:
   `hostprobe.probe_aws_identity(runner, profile=<remote_aws_profile>)`.
   Apply this row's remedy on failure, and do not run steps 10, 11, 13, 14
   and 15 until it passes: name each of those checks and state that they
   depend on this one. Step 12, the certificate check, is unaffected: its
   `## Checks` row depends on none of the remote checks above, so it still
   runs even when this check has not yet passed.
10. Run `instance exists, is running, SSM agent online` (owned by the
    `instances` module, spec Section 4.5, once it exists). Apply this row's
    remedy on failure, and do not run steps 11, 13, 14 and 15 until it
    passes. Step 12, the certificate check, remains independent for the
    same reason step 9 states, so it still runs.
11. Run `port forward established and the local port answering` (owned by
    the SSM port forward manager, E6-F2-S1-T1, once it exists). On failure,
    apply this row's self-fix remedy: re-establish the forward itself, then
    retry this check once. If it still fails, stop, and name steps 13, 14
    and 15 as not run because each depends on the forward. Step 12, the
    certificate check, is unaffected: its `## Checks` row depends on none of
    the remote checks above, so it still runs and is reported on its own
    merits rather than being folded into the not-run list.
12. Run `client, server and CA certificates present and unexpired` against
    the local certificate files under `~/.docker/certs/<name>/` (the
    inspection/expiry-status half of the `certs` module, spec Section 4.5,
    which E6-F1-S1-T2 owns and which does not exist yet; generation,
    `certs.create_ca`/`issue_server`/`issue_client`, E6-F1-S1-T1, already
    does). A certificate that has already expired, or is missing, is this
    check's failure; a certificate that expires within `CERT_WARN_DAYS` but
    has not yet expired is a `WARN` that does not stop the run
    (`## Verdict`).
13. Run `docker version handshake over the tunnel` against the remote
    context the forward addresses. Apply this row's remedy on failure, and do
    not run steps 14 and 15 until it passes.
14. Run `disk headroom on the data volume` over that same handshake. Apply
    this row's remedy on failure; state plainly when the underlying probe
    does not yet exist in this repository.
15. Run `no unpushed work in the volume` by listing
    `git log --oneline origin/<branch>..HEAD` executed inside the container
    over the resolved context. This check fails, never warns, on any
    unpushed commit; apply this row's remedy.
16. Report the verdict (`## Verdict`): one row per check that actually ran,
    with a result of `ok`, `WARN`, the failure that stopped the run, or
    `NOT RUN` for a check whose own `## Checks` row states its probe does
    not exist in this repository yet. When the run stopped at a failure,
    append a single trailing line naming every later check that depends on
    the one that failed and was therefore never reached, rather than a row
    per skipped check or silence.

## Verdict

The verdict reproduces Section 4.2.1's own transcript shape: one line per
check that ran, its result, and a value where one applies, followed by a
trailing summary line. A check's result is `ok`, `WARN`, the failure that
stopped the run, or `NOT RUN`. `NOT RUN` is reserved exclusively for a check
whose own `## Checks` row states that its probe does not exist in this
repository yet, so that gap is always reported plainly and is never
misreported as `ok`. A check merely skipped because an earlier check in its
dependency chain failed receives no row at all -- it is named instead in the
single trailing line the stopped-run example below shows -- so the two
reasons a check might not appear as an `ok` row are never conflated. A
`WARN` never stops the run; only a failure does, and the certificate check
is the only one of the thirteen able to produce a `WARN` (an unexpired
certificate inside `CERT_WARN_DAYS`).

The successful-run transcript below depicts this skill's contract once
every owning module named in `## Checks` exists (`hostprobe`'s disk-headroom
and `HOST_PROXY` probes, the `instances` module, the inspection/expiry-status
half of `certs`, and the SSM port forward manager, E6-F2-S1-T1). Until each
of those exists, the row for the check it owns reports `NOT RUN` in place of
the value shown here, per this document's introduction. The transcript's own
trailing summary line states only the warning count and the remedy, per
Section 4.2.1: the certificate inspection function E6-F1-S1-T2 owns (the
"present and unexpired" half of `## Checks`' certificates row) is not yet in
this repository, so this skill states the reissue action directly --
`certs.create_ca` and `certs.issue_client` (E6-F1-S1-T1) already exist --
rather than naming a slash command whose own precondition, detecting which
certificate is missing or expired, cannot yet be evaluated, but that
explanation belongs here in the prose, not in the operator-facing summary
line itself.

```console
$ /devcontainer:engine INSTANCE=sandbox
CHECK                                     RESULT
docker CLI present, context selected      ok      general-dev-sandbox
general-dev-sandbox context configured    ok      configured
the engine answers                        ok      Server 28.6.0
disk headroom                             ok      42G free
the three files complete and consistent   ok      verify.verify_all: no findings
HOST_PROXY agrees with a reachable proxy  ok      reachable
sso session valid                         ok      expires 18:42Z
instance i-0abc123                        ok      running, agent online
port forward                              ok      127.0.0.1:23760
certificates                              WARN    client expires in 11 days
docker handshake over tunnel              ok      Server 28.6.0, rootless
data volume headroom                      ok      612 GiB free
unpushed work in volume                   ok      none
1 warning. Renew the certificate for sandbox.
```

Note the second row: Procedure step 3 replaces `LOCAL_DOCKER_CONTEXT exists`
on a remote run with the check that the resolved instance's
`general-dev-<name>` context is configured, so the transcript names and
reports that check rather than the local-only one it replaced.

A stopped run never prints a row for a check it did not reach: it names the
first failing check, its `## Checks` failure message and remedy, and then a
single trailing line naming every later check that depends on it and was
therefore not run, for example: "Not run (depend on 'port forward
established and the local port answering', which failed): 'docker version
handshake over the tunnel', 'disk headroom on the data volume', 'no unpushed
work in the volume'." A verdict is never reported as `ok` overall for a run
that stopped partway.

## What this skill fixes itself

Exactly two actions, both reversible and needing no operator credential:

- Selecting an existing docker context (`docker context use <name>`), when
  docker is present, a context is not yet active or not yet the confirmed
  one, and the target context already exists among the configured contexts
  (`docker CLI present and a context selected`, `LOCAL_DOCKER_CONTEXT
  exists`). This skill never creates a context.
- Re-establishing an SSM port forward that has stopped answering (`port
  forward established and the local port answering`), once the instance and
  its SSM agent are already confirmed online.

Every other remedy in `## Checks` is stated to the operator, waited on, and
verified again by this skill before continuing -- installing a tool,
starting the engine, an SSO login, reclaiming disk space, correcting
`HOST_PROXY_URL`, issuing or renewing a certificate, resolving an instance,
and pushing commits from inside the container are none of them performed by
this skill on its own.

## Failure semantics

| Condition | Behavior |
|---|---|
| A precondition check fails | Stop at that check. Name what failed, per its `## Checks` row: what it prevents, its failure message, and its remedy. Do not run later checks that depend on it (`## Procedure`'s numbered dependency order); each of those skipped checks receives no row of its own in the verdict and is instead named in the single trailing line, per `## Verdict`'s skipped-check rendering rule, rather than being omitted from the report in silence. |
| An action the skill took did not verify | This applies to this skill's own two self-fix actions. Report the action taken (`docker context use <name>`, or re-establishing the port forward), the verification that failed (the re-probe with `hostprobe.probe_docker`, or the retried forward check), and the resulting machine state: the context switch happened but the engine still does not answer, or the forward was re-established but still does not answer. Never retries silently a second time, and never reports the check that follows as passing. |
| A step needs the operator (SSO, `sudo`, a key, a certificate reissue, an instance state change) | State the exact command or action, wait for the operator to act, then re-verify with the same `hostprobe` or `verify` call this row's `## Checks` remedy names, before continuing. Never assumes the action succeeded. |
| A gate is reached (Section 4.4) | Present the evidence and stop; never self-approve. This skill reaches no Section 4.4 gate: it validates and, in the two narrow cases above, self-fixes a reversible local state; it never runs `terragrunt apply` or `terragrunt destroy` and never performs the phase 4 cutover. `GATE-APPLY`, `GATE-CUTOVER` and `GATE-DESTROY` are `/devcontainer:setup-remote`'s and a future teardown skill's concern, not this one's. |
| An answer fails validation | This skill never calls `answers.validate`: the one thing it ever asks -- which instance, under Section 4.1.1 -- is a choice among the instances `instances` (spec Section 4.5) already enumerates, not a free-form interview field. An instance name naming no directory under `remote-instances/` is reported immediately, the same "report and re-ask" shape `answers.validate`'s failure semantics use, even though it does not run through `answers.validate` itself. |
| The operator aborts | Leaves no partial configuration. This skill writes nothing to the three private files (that is `render`'s and `verify`'s concern) and creates no docker context, certificate or AWS resource. Its only two self-fix actions -- selecting an already-existing context, re-establishing an already-authorized forward -- are themselves reversible and idempotent, so an abort mid-run leaves the host exactly where the last completed check left it: at worst a different existing context is now active, or a forward that was already permitted to exist has been re-established, neither of which is new state that an abort needs to unwind. |

## Related specifications

- Section 1.1, `repos/spec/devcontainer-platform.md`: `rdc_backend`'s rule for
  local against remote, which step 1 of `## Procedure` applies to the active
  docker context.
- Section 3.1: `rd_die`, `rd_fail`, `rd_require_cmd` and `rd_engine_diagnosis`,
  the shapes this skill's own failures and the engine-unreachable diagnosis
  reuse rather than reinventing.
- Section 4.1.1: instance resolution, the one question this skill ever asks,
  and only when it is ambiguous.
- Section 4.1.4: the error handling contract per condition, including the
  engine-unreachable and unpushed-work rows this skill's own `## Checks`
  table instantiates.
- Section 4.2 and 4.2.1: what `engine` asks, does and ends by, the
  interaction contract every skill obeys, and the two check lists in full
  this document's `## Checks` table reproduces verbatim and in order.
- Section 4.2.2: the failure-semantics table this document's own `##
  Failure semantics` table instantiates.
- Section 4.4: `GATE-APPLY`, `GATE-CUTOVER` and `GATE-DESTROY`, none of which
  this skill reaches.
- Section 4.5: `verify` and `hostprobe` are the only places a fact about a
  rendered file or this machine is decided today; `certs` (E6-F1-S1-T1) now
  owns certificate generation, but `instances` will own instance facts and
  the inspection/expiry-status half of `certs` (E6-F1-S1-T2) will own
  certificate-expiry facts once they exist, and the SSM port forward manager
  (E6-F2-S1-T1) will own the tunnel. This skill decides none of them itself.
- Section 7.3: `CERT_WARN_DAYS`, `SSM_FORWARD_TIMEOUT` and
  `DOCKER_HANDSHAKE_TIMEOUT`, the timeouts this document names rather than
  values. The two disk-headroom rows in `## Checks` describe a
  minimum-headroom threshold no Section 7.3 row yet lists; this document
  deliberately does not invent a variable name for it, leaving that naming
  decision to the work unit that implements the probe.
- Section 9: the addressing table naming `general-dev-<name>` as the docker
  context this skill selects or validates for a remote run, and that the
  forwarded local port is "allocated per instance, recorded, never a fixed
  number."
- AC-4.1 (Section 4.2): every check in the two lists is implemented,
  independently testable, and produces a distinct failure message -- this
  document's `## Checks` table is what `tests/test_skill_lint.py` checks
  that assertion against.
