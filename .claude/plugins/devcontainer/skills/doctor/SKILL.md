---
name: doctor
description: Reports every configuration, secrets, container-state and drift finding /devcontainer:engine's thirteen checks do not cover, each with an exact remedy; performs no repair of its own.
---

# doctor

Section 4.2's own roster row gives `/devcontainer:doctor` a definition by
subtraction, in its own three columns, quoted here exactly: it asks
"Nothing"; it does "Configuration, secrets, container state, drift.
Everything `engine` does not cover"; and it ends by "A findings list, each
with a remedy". `engine` (E4-F2-S2-T1) owns the thirteen checks in Section
4.2.1 and `## Checks` table; this skill does not restate one of them,
since two copies of that contract would drift apart (this document's own
Section 3 obligation) -- it invokes `/devcontainer:engine` first and
carries its verdict forward as a single summary line (`## Report shape`
states the exact shape), never reproducing engine's own per-check `##
Verdict` table a second time, then reports the state engine has no reason
to look at.

`doctor` decides nothing about what a valid file, secret or container looks
like: `verify`, `catalog`, `secrets` and `container.sh`'s `rdc_*` primitives
(`.claude/plugins/devcontainer/scripts/devcontainer_config/`,
`.devcontainer/remote-docker/container.sh`) already own those questions, per
Section 4.5's module split and Section 3.3's "DO NOT reinvent" primitives.
This skill's only job is calling each of them once per run and reporting
every result as one line in `## Findings`, grouped exactly as configuration,
secrets, container state and drift.

Unlike `/devcontainer:engine` and `/devcontainer:setup-remote`, this skill
takes no `INSTANCE=<name>` argument and asks nothing of its own: every check
below reads whichever backend is currently active (`rdc_backend`, `docker
context show`) and whichever scope `devsecret` resolves today
(`catalog.scope_set(None)`, the shared scope alone, since instance-scoped
resolution is not yet wired at the CLI layer -- `cli.py`'s own module
docstring: "No instance-detection mechanism exists yet ... every command
here resolves or narrows against `catalog.scope_set(None)`"). Delegating to
`/devcontainer:engine` can still surface engine's own one question (which
instance, only when Section 4.1.1 resolution is ambiguous, and only on a
remote backend); that question belongs to `engine`, not to this skill, so it
does not appear in a `## Questions` table here and this skill's own
Description column in Section 4.2 correctly reads `Nothing`.

`doctor` reports; it does not act. Section 4.2's interaction contract --
"Where a skill cannot act itself, it states the exact command, waits, then
verifies the result before continuing. Nothing is assumed to have worked."
-- governs every other skill's own repair actions, but this skill never
performs one (see `## What this skill fixes itself`), so it never itself
waits or re-probes mid-run. Instead every remedy below names the exact
command or skill invocation that fixes the condition, the operator (or the
named skill) runs it, and re-invokes `/devcontainer:doctor` for a fresh,
independently recomputed report -- the "wait, then verify" half of the
contract happens across two separate invocations of this skill rather than
inside one. A `doctor` run is one-shot: it never repeats an action, and it
never reports a finding as fixed without that fresh re-run confirming it.

## Findings

Every row below follows this skill's own three-column shape: the condition
this skill checked (`Finding`), the module, command or file it read that
answer from (`Source`), and the exact remedy. This is a distinct shape from
`verify.Finding` (spec Section 4.2.2), whose dataclass and docstring name
four fields, `check`, `found`, `prevents` and `remedy` (`verify.py`), not
three: `Source` names where this skill read the answer, which is not the
same thing `found` names (the value or condition `verify.Finding` itself
encountered), and this table has no column standing in for `prevents` at
all, since a finding's own text already states the condition it guards
against. A row whose owning module does not implement its check yet
reports `NOT RUN` instead of a value, never `ok` -- the same convention
`/devcontainer:engine`'s own `## Verdict` section states and this document
does not restate a second time; see `## Report shape` below for what that
looks like.

### Configuration

Section 5.2 makes `shell.env`'s content a rule of the file, not a
suggestion: "Phase 1 changes only what it is allowed to contain: identity
and configuration, never a credential." `verify` already owns whether the
three private files are complete, placeholder-free and internally
consistent (spec Section 4.5) -- that is exactly `/devcontainer:engine`'s
own check "the three files complete and consistent" (its `## Checks` row,
Section 4.2.1), so this skill carries that result forward from the engine
verdict above rather than calling `verify.verify_all` a second time and
restating it under a different name (AC-FUNC-003). The one configuration
fact `verify` does not check, and that Section 5.2's rule requires, is
whether a value that reached `shell.env` is credential-shaped in the first
place:

| Finding | Source | Remedy |
|---|---|---|
| `shell.env`'s active configuration holds no credential-shaped value | `secrets.scan_lines` (`devcontainer_config.secrets`) run over `shell.env`'s active `export` lines -- the same scope `verify._active_configuration_text` already isolates for the placeholder check, reused here rather than re-deriving it -- restricted to the six credential-bearing detectors in `secrets.PATTERNS` (`aws-access-key-id`, `aws-secret-access-key`, `private-key-block`, `github-token`, `slack-token`, `bearer-token`). The three printable detectors (`sso-portal-url`, `account-id`, `ec2-instance-id`) are excluded: `shell.env` legitimately carries a resolved AWS account or instance identifier as configuration, not a credential, so flagging one would be a false positive by design. `shell-env-line` and `catalog-secret-name` are excluded too: the former needs a comparison source this scan is not a comparison against, and the latter needs the catalog secret names this skill has not yet fetched at this point in `## Procedure`. | Move the value into the catalog by piping it into `devsecret set <NAME> --scope <scope> [--exported]` (never as an argument), then delete the offending `export` line from `shell.env` by hand (this skill never edits it) and re-run `/devcontainer:doctor` to confirm. `/devcontainer:secrets` owns this step: its Add row (`## Operations`) is the same `devsecret set` invocation, reading the value from stdin and never as an argument. The finding names only the variable and, via `secrets.render_finding`, a redacted, safe-to-paste rendering of the match -- never the value itself. |

### Secrets

Section 4.3's `devsecret` CLI is the only way this skill reaches the
catalog, and Section 5.4 fixes one backend with no offline store: "The
store answers or the command fails; nothing degrades to a local copy."
`devsecret`'s own exit code 3 covers exactly "backend unreachable or
unauthorized" (spec Section 4.3, 14.2); this skill reads that exit code and
turns it into a finding, never a retry against anything else.

| Finding | Source | Remedy |
|---|---|---|
| The secret catalog answers | `devsecret list` (`catalog.list_resolved`, built on `describe-parameters`) | On `CatalogUnavailableError` (devsecret exit 3), name `aws sso login --profile <profile>` with `<profile>` read from `$AWS_PROFILE` (defaulting to `'default'`, `catalog._unavailable_no_credential_message`), wait for the operator to complete the login, then re-run `/devcontainer:doctor`. Never falls back to any local store (Section 5.4). |
| The exported list resolves | `devsecret export-list` (`catalog.list_resolved` plus `cli._export_list_names`) | The same catalog call `devsecret list` makes, so an unreachable or unauthorized catalog is the identical condition and the identical remedy above; on `CatalogUnauthorizedError` instead, name the missing `ssm:*` grant on the parameter prefix (`catalog._unauthorized_message`) and ask an operator to grant it, then re-run `/devcontainer:doctor`. |
| Every name `devsecret list` reports as exported is a valid environment-variable identifier | Every exported `catalog.SecretRecord.name` cross-checked against `catalog._NAME_PATTERN`, the identifier rule `catalog._validate_name` already enforces before any `devsecret set` ever reaches the store (spec Section 4.3: "A name that is not a valid environment-variable identifier is a usage error, since exported secrets become variables"). A record failing this pattern could only have reached the store through a write that bypassed `devsecret set` entirely, and even then only when the record also satisfies `catalog._record_from_entry`'s own listing contract: that function raises `CatalogError` for any parameter under the prefix whose `Description` is missing or is not the JSON document this client writes (`{"exported": ...}`), so `devsecret list` can only ever surface a bad-name row for a parameter planted with both an out-of-pattern `Name` and a client-shaped `Description` -- for example a raw `aws ssm put-parameter` call that copied this client's `Description` format. A parameter with an out-of-pattern name and no client-shaped `Description` instead fails the `devsecret list` call itself with a `CatalogError`, never reaching this row. | Name the offending secret and its scope (from the same `devsecret list` row). `devsecret get` and `devsecret rm` both refuse this name before ever reaching the store -- `catalog.resolve` calls `CatalogClient.read`, which calls `parameter_path`, which calls `_validate_name` (`catalog.py`); `devsecret rm`'s handler calls `parameter_path` directly -- so recover and delete the parameter against the store path directly, using the same shape `catalog.parameter_path` composes (`/devcontainer/<scope>/secrets/<NAME>`), piping the value straight into the replacement rather than writing it to disk: `aws ssm get-parameter --with-decryption --name /devcontainer/<scope>/secrets/<NAME> --query Parameter.Value --output text \| devsecret set <NEW_NAME> --scope <scope> --exported`, then `aws ssm delete-parameter --name /devcontainer/<scope>/secrets/<NAME>` to remove the old entry, then re-run `/devcontainer:doctor`. |

### Container state

Section 3.3 names `rdc_container_ids` and `rdc_require_container`
(`container.sh`) as the container-resolution primitives every skill reuses
rather than reinventing; this skill's container-state findings read `make
status` and `make check`, the two existing dispatch targets already built
on those primitives, rather than a new resolution scheme.

| Finding | Source | Remedy |
|---|---|---|
| A container, image and volumes exist for the active backend | `make status` (`container.sh`'s `rdc_status`, itself built on `rdc_container_ids`, `docker inspect` and `rdc_project_volumes`) | When none exists, `rdc_require_container`'s own condition applies verbatim: `"No container for project '<PROJECT_NAME>' exists on context '<context>'"`. Build one with `make up` (builds, starts and opens it) or `make build` (builds it and stops there), then re-run `/devcontainer:doctor`. |
| The container's workspace volume holds no uncommitted or unpushed work | `make check` (`container.sh`'s `rdc_check`). For a local backend this always reports `rdc_check`'s own local-branch result verbatim ("local backend, the container shares this working tree, nothing to check"), since the container mounts the host's working tree directly rather than holding an independent clone. This differs from `/devcontainer:engine`'s own remote-only check "no unpushed work in the volume" (Section 4.2.1): that check inspects only unpushed commits, over the resolved remote context, and only as the last of a chain of prior remote checks; `rdc_check` additionally flags uncommitted working-tree changes (`git status --porcelain`), is invoked on both backends but performs this inspection only on a remote one (the local early return this same cell states above), and reads the currently active docker context directly rather than depending on the SSO, instance and port-forward checks ahead of it. | `rdc_check`'s own remedy, verbatim in substance: push from inside the container (`docker exec -u <CONTAINER_USER> <container-name> git -C <CONTAINER_WORKSPACE> push`) since a rebuild re-clones from origin and none of this comes back, or destroy it deliberately with `make clean FORCE=1`. Then re-run `/devcontainer:doctor`. |

### Drift

Four conditions Section 4.2.1's thirteen checks never look at, because each
compares two things `engine`'s own precondition walk never puts side by
side:

| Finding | Source | Remedy |
|---|---|---|
| Every variable in a committed `.example` file is present in the rendered private file | `NOT RUN`. No function of this name exists in this repository yet, the same gap `/devcontainer:engine`'s own introduction discloses for disk headroom and `HOST_PROXY`: this row states the check's exact contract -- diff the export names in `repo.example_for(relative)` against the export names `verify._ACTIVE_EXPORT_LINE` finds in the rendered file -- and names `verify` as the module a future work unit extends to own it, since Section 4.5 already gives `verify` the "written configuration is complete" responsibility this comparison extends. | Once implemented: re-render with `/devcontainer:setup-local` or `/devcontainer:setup-remote` (moving the three existing private files aside first, since the setup skills always call `render.write_all` with `overwrite=False`, so it refuses and names every existing path rather than replacing one already on disk), then re-run `/devcontainer:doctor`. |
| The Parameter Store copy of `shell.env` agrees with the local one | `NOT RUN`. `catalog` (`devcontainer_config.catalog`) is the only module that reaches Parameter Store, but its functions read only `/devcontainer/<scope>/secrets/<NAME>` (spec Section 5.3); nothing today fetches `/devcontainer/<instance>/shell.env` itself for comparison, only the shell-side `fetch_parameter` bootstrap (Section 3.5) that writes it once at container creation. This row names `catalog` as the module a future work unit extends to own the comparison. | Once the comparison itself is implemented: `make push-secrets` (`push-secrets.sh`) already republishes the local `shell.env` and `aws-profile-map.json` to Parameter Store, so a finding here is resolved the same way, then re-run `/devcontainer:doctor`. |
| The active docker context's certificate paths match the material on disk | `NOT RUN`. Owned by the `certs` module (spec Section 4.5): `certs.py` (E6-F1-S1-T1) exists for generation, but this comparison is inspection, and no function of this name exists in this repository yet -- the identical gap `/devcontainer:engine`'s own check "client, server and CA certificates present and unexpired" discloses for the same still-missing inspection half (E6-F1-S1-T2). This is a distinct condition from that engine check: engine asks whether the certificate files themselves are present and unexpired, this row asks whether the docker context's configured TLS paths still point at where those files actually live under `~/.docker/certs/<instance>/`. | Once implemented: reissue or realign the certificate material with the `/devcontainer:certs` skill (Section 4.2's roster; already authored, E4-F3-S2-T2), then re-run `/devcontainer:doctor`. |
| Hooks installed on the host are also installed inside the container | `githooks.hooks_status(root)` (`devcontainer_config.githooks`) run on the host, compared against the identical call run inside the container. For a local backend the two are definitionally equal, since the container mounts the host's working tree directly (the same fact `rdc_check`'s local-branch row above states); for a remote backend, run inside the container over the resolved docker context with `rdc_exec` (`container.sh`, the same helper `rdc_check` already uses): `rdc_exec <container-id> sh -c "cd '<CONTAINER_WORKSPACE>' && env PYTHONPATH=<CONTAINER_WORKSPACE>/.claude/plugins/devcontainer/scripts python3 -m devcontainer_config.cli hooks-check"`, where `<CONTAINER_WORKSPACE>` is `repo.container_workspace(root)`. The explicit `cd` is required, not cosmetic: `cli._run_hooks_check` resolves the repository with `repo.find_root(Path.cwd())`, and `rdc_exec` forwards everything after the container id straight through as the command (`rd_docker exec -u "$CONTAINER_USER" "$id" "$@"`, `container.sh:86-89`), so there is no way to inject a bare `-w` after the container id; without the `cd`, the process starts at `/`, where `find_root` fails with "is not inside a git repository" instead of reporting hook drift -- the same `sh -c "cd '${CONTAINER_WORKSPACE}' && ..."` pattern `container.sh`'s own `rdc_exec_probe` call already uses (line 479). This is exactly the gap AC-4.7 (Section 4.6) exists to prevent: "Hooks are installed on the host by `make hooks-install` and inside the container by postCreate, so a commit made from a container terminal is guarded identically." | Re-run `make hooks-install` on whichever side (host or container, named by which `hooks-check` failed) reports drift, then re-run `/devcontainer:doctor`. |

## Procedure

1. Run `/devcontainer:engine`. Carry its result forward faithfully as the
   single summary line `## Report shape` states -- pass, warn, fail, or a
   partial run naming what it never reached -- without restating any of its
   thirteen `## Checks` rows, or its own `## Verdict` table's rows, as a row
   of this skill's own `## Findings` table (AC-FUNC-003). If the
   invocation produces no verdict at all, report that condition instead of
   a verdict, per `## Failure semantics`. This skill's own remaining steps
   run regardless of what that verdict says or whether one was obtained:
   `doctor` is a complete list, not a fail-fast gate, so a precondition
   engine's own run stopped at, or the absence of a verdict entirely, does
   not stop this skill from still reporting every configuration, secrets,
   container-state and drift finding it can.
2. Configuration: run the credential-shape scan against `shell.env`
   described in `## Findings`.
3. Secrets: run `devsecret list` and `devsecret export-list`; cross-check
   every name either reports as exported against `catalog._NAME_PATTERN`.
4. Container state: run `make status` and `make check`.
5. Drift: run the host-against-container hooks comparison. Report the
   other three drift rows as `NOT RUN`, each naming the module a future
   work unit will extend to own it, per `## Findings`.
6. Print the findings list, grouped exactly as `## Findings` groups them,
   one line per row: the finding text, `ok`, `NOT RUN`, or the failure
   found, and, for anything other than `ok`, the exact remedy. State
   plainly, once, that this skill performed no repair for any row above
   (`## What this skill fixes itself`).

## Report shape

The transcript below depicts this skill's contract today, run against a
local backend: the three drift rows with no owning module yet report
`NOT RUN`, exactly as `## Findings` states, never a fabricated `ok`; and the
container-state row below prints `rdc_check`'s own local-branch result
verbatim, exactly as `## Findings`' container-state group states, never the
computed "0 commits ahead" only a remote run's `dirty`/`ahead` check ever
produces (`container.sh:265-270`). The `ENGINE` line is a single summary
line this skill derives from `/devcontainer:engine`'s own per-check `##
Verdict` table; it never reproduces that table's own rows here, since doing
so a second time is exactly the duplication Section 3 forbids. The line
states how many of the checks that ran reported `ok`, and names every one
that reported anything else (`NOT RUN`, a `WARN`, or the failure that
stopped the run) -- shown below as engine's own local-backend verdict
reports it today, per its own `## Verdict` and introduction: four of its six
local checks `ok`, and `disk headroom` and `HOST_PROXY agrees with a
reachable proxy` reporting `NOT RUN`, since neither probe exists in this
repository yet. This skill never turns an engine `ok` into anything but `ok`
here, and never turns a `NOT RUN` or a failure into a fabricated `ok`, so
the same `NOT RUN` discipline this document's own drift rows follow applies
here too.

```console
$ /devcontainer:doctor
ENGINE      4 checks ok, 2 NOT RUN (disk headroom, HOST_PROXY agrees with a reachable proxy)

CONFIGURATION
  shell.env holds no credential-shaped value                         ok

SECRETS
  the secret catalog answers                                         ok
  the exported list resolves                                         ok
  every exported name is a valid identifier                          ok

CONTAINER STATE
  container, image and volumes exist                                 ok      general-dev  [running]
  no uncommitted or unpushed work in the volume                      ok      local backend, the container shares this working tree, nothing to check

DRIFT
  every .example variable is present in the rendered file             NOT RUN (verify: not implemented yet)
  Parameter Store copy of shell.env agrees with the local one         NOT RUN (catalog: not implemented yet)
  active docker context certificate paths match the material on disk NOT RUN (certs: comparison not implemented yet)
  hooks installed on the host are also installed in the container    ok      match

No repair performed. 0 findings need a remedy.
```

A run that finds something never says `ok` for the row that failed: it
prints the same failure text and remedy `## Findings` states for that row,
in place of `ok`, and continues to every remaining row regardless -- unlike
`/devcontainer:engine`'s stop-at-the-first-failure walk, nothing here ever
depends on an earlier row succeeding.

## What this skill fixes itself

None. `doctor` reports; it never writes a file, never creates or destroys a
docker context, never edits `shell.env`, and never touches the secret
catalog. Every remedy in `## Findings` names the exact operator command or
skill invocation that fixes the condition instead.

## Failure semantics

| Condition | Behavior |
|---|---|
| A precondition check fails | This skill has no fail-fast precondition of its own to stop at: the only precondition-shaped input it consumes is `/devcontainer:engine`'s own verdict (`## Procedure` step 1), carried forward faithfully as the single `ENGINE` summary line `## Report shape` states. Unlike every other skill in this platform, a failure in that carried-forward verdict does not stop this skill's own remaining steps -- configuration, secrets, container state and drift all still run and are still reported, because `doctor`'s job is a complete list, not a gate. |
| The engine verdict cannot be obtained at all (`/devcontainer:engine`'s invocation produces no verdict, as distinct from a verdict that itself reports a failure or a partial run) | Print the `ENGINE` line as `NOT RUN`, naming that its thirteen checks did not run, and report no overall health for that line -- the same `NOT RUN` discipline `## Findings`' own drift rows already use for a check with no owning module yet, never a fabricated `ok`. This skill's own remaining steps still run per the row above, and the trailing summary line must never read as a clean bill in this case, since none of the thirteen checks that would justify one ran: reporting the rest as healthy would be a silent failure of the delegation. |
| An action the skill took did not verify | Not applicable: this skill performs no action of its own (`## What this skill fixes itself` is empty), so there is no resulting machine state to report and nothing to retry, silently or otherwise. The machine is always exactly as it was before this skill ran. |
| A step needs the operator (SSO, `sudo`, a key) | This skill never waits inline, because it has no in-progress action to resume: every remedy in `## Findings` names the exact command, the operator (or the named skill) runs it, and re-invokes `/devcontainer:doctor` for a fresh, independently recomputed report. Never assumes a remedy succeeded without that fresh run confirming it. |
| A gate is reached (Section 4.4) | This skill reaches no Section 4.4 gate: it creates no AWS resource, runs no `terragrunt apply` or `terragrunt destroy`, and performs no phase-4 cutover. `GATE-APPLY`, `GATE-CUTOVER` and `GATE-DESTROY` are `/devcontainer:setup-remote`'s and a future teardown skill's concern, not this one's. |
| An answer fails validation | This skill asks nothing of its own (Section 4.2's own roster row: "Asks: Nothing") and never calls `answers.validate`, so this condition never arises for it. |
| The operator aborts | Leaves no partial state: this skill writes nothing, creates nothing and deletes nothing at any point in its run, so an interrupted run (stopped before its findings list finishes printing) leaves the repository, every docker context and every AWS resource exactly as they were -- except for whatever `/devcontainer:engine`'s own two narrow self-fixes already did under its own verdict before this skill was interrupted, which is engine's own state to account for, not a new state this skill introduces. |

## Related specifications

- Section 3.3, `repos/spec/devcontainer-platform.md`: `rdc_backend`,
  `rdc_container_ids`, `rdc_require_container`, `PRIVATE_FILES`, the
  primitives `## Findings`' container-state group reuses rather than
  resolving a container a second way.
- Section 4.2: the roster row this document's introduction quotes
  verbatim, and the interaction contract every skill obeys.
- Section 4.2.1 and 4.2.2: the thirteen checks this skill delegates by
  reference rather than restating, and the failure-semantics table this
  document's own `## Failure semantics` table instantiates.
- Section 4.3: `devsecret`'s six commands, its five exit codes, and the
  "never a value" and "usage error" rules `## Findings`' secrets group
  checks against. AC-4.3.
- Section 4.5: `verify`, `catalog` and `certs` are the only places a fact
  about a rendered file, the secret catalog, or a certificate is decided;
  this skill decides none of them itself, and names each as the future
  owner of a drift check that does not exist in this repository yet.
  `githooks` is the one module in this same table this skill's drift group
  already calls today, for the one drift row that is implemented.
- Section 4.6 and AC-4.7: the host-against-container hooks parity this
  skill's drift group checks, and the exact sentence its remedy exists to
  satisfy.
- Section 5.2: `shell.env` holds identity and configuration, never a
  credential -- the rule this skill's one new configuration finding
  enforces that `verify` does not.
- Section 5.4: one backend, no offline store, nothing degrades to a local
  copy -- why every secrets finding above names `aws sso login` or an
  IAM grant and never a fallback.
- AC-4.3 (Section 4.3): `devsecret list` never prints a value; this
  skill's own secrets findings read only name, scope, last-changed and the
  exported flag, the same four columns `devsecret list` itself renders.
