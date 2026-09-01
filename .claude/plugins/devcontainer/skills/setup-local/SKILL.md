---
name: setup-local
description: Configures this machine for the local docker engine by writing shell.env, devcontainer-environment-variables.json and aws-profile-map.json from an interview, then verifying them.
---

# setup-local

`/devcontainer:setup-local` is the worked example in spec Section 2, G1: a
fresh clone reaches a working local container without reading documentation.
This skill decides nothing about what a valid answer looks like, what a
rendered file contains, or what "complete" means -- `answers`, `render` and
`verify` (`.claude/plugins/devcontainer/scripts/devcontainer_config/`) own
those questions, and `hostprobe` owns every fact about this machine. This
skill's only job is sequencing calls to those four modules and reporting
their results.

Where this skill cannot act itself -- installing an absent prerequisite,
starting a stopped engine -- it states the exact command, waits for the
operator to run it, then re-probes with `hostprobe` itself before
continuing. It never assumes the action succeeded, and it never asks the
operator to re-invoke `/devcontainer:setup-local` to force a re-check.

Interview backend: local

## Questions

| Field | Prompt |
|---|---|
| backend | Not asked. Always set to `local` for this skill. |
| developer_name | What name should appear in prompts? |
| git_user | What is your git username? |
| git_user_email | What is your git email address? |
| git_provider_url | What is your git provider's host? |
| default_git_branch | What is your default git branch? |
| template_name | What should this devcontainer template be named? |
| aws_config_enabled | Do you want AWS profile configuration written into this checkout? |
| local_docker_context | Which docker context should `make build` use? |
| host_proxy | Are you behind a host-side network proxy? |
| aws_profiles | Asked only when the answered `aws_config_enabled` is `true` (`answers.Requiredness.WHEN_AWS`); not asked otherwise. For each AWS profile, the sub-fields `answers.AWS_PROFILE_SUB_FIELDS` declares. |
| host_proxy_url | Asked only when the answered `host_proxy` is `true` (`answers.Requiredness.WHEN_PROXY`); not asked otherwise. What is your proxy URL? |

## Checks

Every row below whose remedy names an operator action follows the Section
4.2 interaction contract: this skill states the exact command, waits for the
operator to run it, then re-probes with `hostprobe` itself (`probe_tools` for
a tool, `probe_docker` for a context or the engine) before continuing,
rather than asking the operator to re-invoke `/devcontainer:setup-local`.

This list is the local subset of Section 4.2.1's check list that this skill
itself owns. Disk headroom and `HOST_PROXY` agreeing with a reachable proxy
are also named by Section 4.2.1's local list; this skill collects
`host_proxy` and `host_proxy_url` in its own interview but does not probe
either one, because the `engine` skill (E4-F2-S2-T1) owns the full Section
4.2.1 validation contract and runs both checks. An unreachable proxy written
here surfaces there, or at `make build` time, rather than during this
interview.

| Check | Prevents | Failure message | Remedy |
|---|---|---|---|
| docker CLI present | every later docker check, and `make build` itself | docker is not on PATH | Install docker with the command this result's `ProbeResult.remedy` names (`hostprobe.probe_tools`, sourced from `hostprobe._TOOL_SPECS`), then this skill waits and re-probes with `hostprobe.probe_tools`. |
| git present | the git identity `render` writes never being exercised by a real clone | git is not on PATH | Install git with the command this result's `ProbeResult.remedy` names (`hostprobe.probe_tools`, sourced from `hostprobe._TOOL_SPECS`), then this skill waits and re-probes with `hostprobe.probe_tools`. |
| jq present | `make build`'s prerequisite check | jq is not on PATH | Install jq with the command this result's `ProbeResult.remedy` names (`hostprobe.probe_tools`, sourced from `hostprobe._TOOL_SPECS`), then this skill waits and re-probes with `hostprobe.probe_tools`. |
| devcontainer CLI present | `make build`, which shells out to it | devcontainer CLI is not on PATH | Install the devcontainer CLI with the command this result's `ProbeResult.remedy` names (`hostprobe.probe_tools`, sourced from `hostprobe._TOOL_SPECS`), then this skill waits and re-probes with `hostprobe.probe_tools`. |
| uv present | the container's Python tooling bootstrap | uv is not on PATH | Install uv with the command this result's `ProbeResult.remedy` names (`hostprobe.probe_tools`, sourced from `hostprobe._TOOL_SPECS`), then this skill waits and re-probes with `hostprobe.probe_tools`. |
| docker context list obtained | every later docker check, since none can run against an unknown context set | docker context ls failed, or docker is not on PATH | If docker is absent, install it with the command the docker CLI row above names. If `docker context ls --format {{.Name}}` ran and failed, run that exact command manually, investigate why it failed, then this skill waits and re-probes with `hostprobe.probe_docker`. |
| local_docker_context exists | `make build` selecting a context that was never created | the answered local_docker_context is not among the configured docker contexts | Create it (`docker context create`), or re-answer `local_docker_context` with one of the contexts `hostprobe` lists. This skill never creates a context itself; it re-probes with `hostprobe.probe_docker(runner, requested_context=<answer>)` and re-asks the field rather than accepting an unconfirmed context. |
| docker engine answers | `make build`, and every command that needs the daemon reachable | the docker daemon behind the active context did not answer `docker version` in time | Start the engine behind the active context: open OrbStack or Docker Desktop, or start `dockerd`, whichever is installed. Then this skill waits and re-probes with `hostprobe.probe_docker`. |
| three private files complete | the container's postCreate step, which reads all three | one of shell.env, devcontainer-environment-variables.json or aws-profile-map.json could not be read or parsed | Move the three private files aside -- `render.write_all` is always called with `overwrite=False`, so it never replaces a file already on disk -- then re-run `/devcontainer:setup-local` against the clean tree; see the existing-file row in `## Failure semantics`. |
| no placeholders remain | a value the operator never supplied reaching the running container as literal text | an active configuration line still contains an unreplaced `<placeholder>` | Move the three private files aside -- `render.write_all` is always called with `overwrite=False`, so it never replaces a file already on disk -- then re-run `/devcontainer:setup-local` against the clean tree; see the existing-file row in `## Failure semantics`. |
| BASH_ENV matches the workspace path | every non-interactive shell in the container sourcing nothing | shell.env's BASH_ENV does not match the in-container workspace path | Move the three private files aside -- `render.write_all` is always called with `overwrite=False`, so it never replaces a file already on disk -- then re-run `/devcontainer:setup-local` against the clean tree; see the existing-file row in `## Failure semantics`. |
| identity variables agree across files | the container's actual environment disagreeing with the file every shell sources | an identity variable disagrees between shell.env and devcontainer-environment-variables.json | Move the three private files aside -- `render.write_all` is always called with `overwrite=False`, so it never replaces a file already on disk -- then re-run `/devcontainer:setup-local` against the clean tree; see the existing-file row in `## Failure semantics`. |
| aws-profile-map.json agrees with AWS_CONFIG_ENABLED | the container's AWS profile setup rendering nothing, or silently ignoring a populated map | aws-profile-map.json's populated-or-empty state disagrees with AWS_CONFIG_ENABLED | Move the three private files aside -- `render.write_all` is always called with `overwrite=False`, so it never replaces a file already on disk -- then re-run `/devcontainer:setup-local` against the clean tree; see the existing-file row in `## Failure semantics`. |

## Procedure

1. Probe this machine with `hostprobe`: operating system
   (`probe_operating_system`), the five prerequisite tools (`probe_tools`:
   docker, git, jq, devcontainer CLI, uv), and the configured docker
   contexts plus which one is currently selected (`probe_docker`, called
   with no `requested_context` yet, since no answer exists). If any
   prerequisite check fails, apply that row's `## Checks` remedy: name the
   exact command, wait for the operator, then re-probe before continuing.
2. Report the docker context list, the selected context, and whether its
   engine answers `docker version` within `DOCKER_HANDSHAKE_TIMEOUT`.
3. Run the interview from the `## Questions` table above, setting `backend`
   to `local` without asking it. Ask `aws_profiles` only when the answered
   `aws_config_enabled` is `true`, and `host_proxy_url` only when the
   answered `host_proxy` is `true`; ask every other row unconditionally.
4. Validate the collected answers with `answers.validate`. If any field
   fails, report every failing field and its rule in one message and
   re-ask only those fields; do not proceed until every answer validates.
5. Re-probe the answered `local_docker_context` with
   `hostprobe.probe_docker(runner, requested_context=<answer>)`. If it is
   not among the configured contexts, stop, name the answered value, list
   the contexts `hostprobe` reports, and re-ask only `local_docker_context`.
   Never create a context on the operator's behalf.
6. Render `shell.env`, `devcontainer-environment-variables.json` and
   `aws-profile-map.json` in memory with `render.render_all`. Nothing is
   written to disk yet.
7. Commit all three files together with
   `render.write_all(rendered, root, overwrite=False)`. Because `overwrite`
   is `False`, `write_all` refuses and names every existing path when any
   of the three already exists on disk; stop there rather than merging into
   an existing file or passing `overwrite=True`.
8. Run `verify.verify_all` against the written files and report every
   finding: completeness, no placeholders, and cross-file consistency
   (including `BASH_ENV` against the in-container workspace path).
9. Report the prerequisite table from step 1's tool probes: each tool name
   and whether it is present.
10. Select the answered `local_docker_context` as the active docker
    context: `docker context use <local_docker_context>`. Then re-probe with
    `hostprobe.probe_docker(runner, requested_context=<local_docker_context>)`
    and report whether the engine behind the now-active context answers
    `docker version` within `DOCKER_HANDSHAKE_TIMEOUT`. Step 2's handshake
    ran against whichever context was active before this switch, so this is
    the first confirmation for the context `make build` will actually use.
    If the engine does not answer, apply the `docker engine answers` remedy
    from `## Checks`: name the exact action, wait, then re-probe again
    before continuing.
11. End by naming `make build`. This skill does not run it.

## Failure semantics

| Condition | Behavior |
|---|---|
| A precondition check fails (a tool absent, the docker context list unobtainable, the answered `local_docker_context` not among the configured contexts, the engine not answering) | Stop at that check. Name what failed, what it prevents, and the exact remedy. Do not run later checks that depend on it. |
| A step needs the operator (installing an absent tool, starting a stopped engine) | State the exact command from the `## Checks` table's remedy column, wait for the operator to run it, then re-probe with `hostprobe` before continuing. Never assume the action succeeded. |
| A gate is reached (Section 4.4) | Present the evidence and stop; never self-approve. This skill reaches no Section 4.4 gate: it creates no AWS resource and performs no Terragrunt apply or destroy, so `GATE-APPLY`, `GATE-CUTOVER` and `GATE-DESTROY` never apply to it. |
| One or more answers fail `answers.validate` | Report every failing field and its rule in one message, not the first. Re-ask only those fields. Never accept a partially valid answer set and never call `render.write_all`. |
| The operator aborts the interview | Leave no partial configuration: nothing is written until every answer validates and `render.write_all` is called, so an abort after the first answer leaves the working tree, and every docker context, exactly as they were. |
| `render.write_all` finds an existing private file (this skill always calls it with `overwrite=False`) | Stop, name every existing path, and state that this skill does not overwrite it. Direct the operator to move it aside or run this skill against a clean clone. Never merge into it and never pass `overwrite=True`. |
| An action the skill took did not verify (`verify.verify_all` reports a finding, or the Procedure step 10 re-probe finds the engine still not answering after the context switch) | Report the action taken, the verification that failed, and the resulting machine state: the three files are on disk but not confirmed consistent, or `local_docker_context` is now the active context but its engine is not confirmed reachable. Never retry silently and never report success having failed. |

## Related specifications

- Section 2, G1, `repos/spec/devcontainer-platform.md`: the worked transcript
  this skill reproduces.
- Section 4.2 and 4.2.1: what this skill asks, does, ends by, and the checks
  it runs before acting.
- Section 4.2.2: the failure-semantics table this skill's own table above
  instantiates.
- Section 4.5: `answers`, `render`, `verify` and `hostprobe` are the only
  places a fact about the interview, a rendered file, or this machine is
  decided; this skill decides none of them itself.
- Section 5.1 and 5.2: the interview schema and the three configuration
  files this skill writes.
