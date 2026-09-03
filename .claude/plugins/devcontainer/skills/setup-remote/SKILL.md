---
name: setup-remote
description: Configures this laptop to reach a remote EC2 docker engine over an SSM port forward secured with mTLS -- no SSH anywhere -- then verifies the result.
---

# setup-remote

`/devcontainer:setup-remote INSTANCE=<name>` is the worked example in spec
Section 2, G2: G1 for a remote instance, with no SSH anywhere. `<name>` is
the human-chosen instance name (spec Section 9's addressing pattern:
`remote-instances/<name>/`, docker context `<repo-slug>-<name>`
(`general-dev-<name>` in this repository), Parameter prefix
`/devcontainer/<name>/`, certificates under `<certs-root>/<name>/` --
`$DOCKER_CONFIG/certs/<name>/`, or `~/.docker/certs/<name>/` when
`DOCKER_CONFIG` is unset; `<certs-root>` stands for that root throughout
this document); `remote_instance_id` is the separate schema
field recording the actual EC2 instance id once resolved. This skill is
`/devcontainer:setup-local` plus the remote fields and the remote work: it
asks everything `setup-local` asks, plus instance name, id, region and
profile, and it decides nothing about what a valid answer looks like, what
a rendered file contains, or what "complete" means -- `answers`, `render`
and `verify` (`.claude/plugins/devcontainer/scripts/devcontainer_config/`)
own those questions, and `hostprobe` owns every fact about this machine.
Certificate generation and instance resolution are owned by the `certs` and
`instances` modules of that same section (spec Section 4.5). `certs`
(E6-F1-S1-T1, E6-F1-S1-T2) now exists for both generation -- `certs.create_ca`,
`certs.issue_server`, `certs.issue_client` and `certs.publication_set`, which
Procedure step 9 below calls directly -- and inspection/expiry status
(`certs.status_rows`, `certs.classify`), the same functions `make cert-status`
calls. `transport` (E6-F2-S1-T1) owns the SSM port forward itself: SSM agent
status, per-instance local port allocation, the `aws ssm start-session`
argument vector and its readiness detection, which Procedure steps 7 and 11
through 12 below call directly. `instances` (E8-F1-S1-T1) now owns instance
discovery, the resolution order and the Section 9 addressing derivations named
in the paragraph above -- `instances.discover`, `instances.resolve`,
`instances.docker_context_prefix`, `instances.docker_context`,
`instances.parameter_prefix`, `instances.certs_root` and `instances.certs_dir`
-- deriving every `<repo-slug>-<name>`, `<certs-root>/<name>/` and
`/devcontainer/<name>/` path this document uses. Procedure step 7 below never
calls `instances.resolve`: `instances.resolve`'s `INSTANCE` /
`DEFAULT_REMOTE_INSTANCE` / sole-directory order selects among
already-configured `remote-instances/` directories, and this skill is what
creates `remote-instances/<name>` for the first time (Procedure step 8), so
this skill's own `<name>` comes from the operator's `/devcontainer:setup-remote
INSTANCE=<name>` invocation directly, never from a call to `instances.resolve`
here.

Where this skill cannot act itself -- a browser SSO login, an AWS resource
that only an operator may create or destroy -- it states the exact
command, waits for the operator to run it, then verifies the result itself
before continuing (spec Section 4.2's interaction contract). For a browser
SSO login this means: state `aws sso login --profile <profile>` with the
answered profile substituted, wait for the operator to complete the login
in their browser, then re-probe with `hostprobe.probe_aws_identity` before
continuing -- it never hands the operator back the whole
`/devcontainer:setup-remote` invocation to force a re-check, and it never
assumes the login succeeded.

Interview backend: remote

## Questions

| Field | Prompt |
|---|---|
| backend | Not asked. Always set to `remote` for this skill. |
| developer_name | What name should appear in prompts? |
| git_user | What is your git username? |
| git_user_email | What is your git email address? |
| git_provider_url | What is your git provider's host? |
| default_git_branch | What is your default git branch? |
| template_name | What should this devcontainer template be named? |
| aws_config_enabled | Do you want AWS profile configuration written into this checkout? |
| local_docker_context | Which docker context should `make build` use? Collected and validated for schema symmetry with `setup-local`, but this skill does not select or probe it: `render` does not write it into any file, and this run selects the docker context it creates instead. A later switch back to the local backend is a different skill's concern. |
| host_proxy | Are you behind a host-side network proxy? |
| aws_profiles | Asked only when the answered `aws_config_enabled` is `true` (`answers.Requiredness.WHEN_AWS`); not asked otherwise. For each AWS profile, the sub-fields `answers.AWS_PROFILE_SUB_FIELDS` declares. |
| host_proxy_url | Asked only when the answered `host_proxy` is `true` (`answers.Requiredness.WHEN_PROXY`); not asked otherwise. What is your proxy URL? |
| remote_instance_id | What is the id of the EC2 instance this backend addresses (`i-` plus 8 or 17 hexadecimal characters)? |
| remote_aws_region | Which AWS region is that instance in? |
| remote_aws_profile | Which AWS SSO profile authenticates to that region? |

Every row whose Prompt names a condition is asked only under that
condition; every other row is asked unconditionally. `backend` is set
without being asked, per the frontmatter's `Interview backend: remote`.

## Checks

Every check `/devcontainer:setup-local`'s own `## Checks` table owns
applies here unchanged, on the same machine: the five tool-presence checks
(docker CLI, git, jq, devcontainer CLI, uv), the docker context list being
obtainable, and the three private files' completeness, placeholder-freedom,
`BASH_ENV` correctness, cross-file identity agreement and
`aws-profile-map.json` agreement with `AWS_CONFIG_ENABLED`. This skill does
not restate them; see `/devcontainer:setup-local`'s own table for each
one's failure message and remedy. The table below lists only the checks
this skill adds for the remote path, per Section 4.2.1's "Remote, in
addition" list. Every row below whose remedy names an operator action
follows the same interaction contract this document's introduction states:
name the exact command, wait for the operator, then re-verify before
continuing.

| Check | Prevents | Failure message | Remedy |
|---|---|---|---|
| SSO session valid for the answered profile | everything remote depends on: the SSM tunnel, Parameter Store, and the build that publishes to it | aws is not on PATH, no credentials resolve for the profile, or the SSO session for the profile has expired | State `aws sso login --profile <profile>` with the answered `remote_aws_profile` substituted, wait for the operator to complete the browser login, then re-probe with `hostprobe.probe_aws_identity(runner, profile=<profile>)` before continuing. Never proceed on the assumption that the login succeeded, and never fall back to any other credential source. |
| instance resolved and SSM agent online | the port forward and the docker version handshake, since neither can reach an instance whose SSM agent is not reporting online | the answered remote_instance_id does not resolve to a running instance, or the instance is running but its SSM agent is not online | Stop, name the instance id and the agent state, and state that the port forward cannot be established until the agent reports online. Do not attempt the forward: its failure would name the wrong cause. |
| PRECHECK-APPLY recorded before any terragrunt apply | an autonomous run creating or destroying AWS resources without a recorded operator confirmation (spec Section 4.4, AC-4.4) | PRECHECK-APPLY reached with no recorded plan and verification | Present the Terragrunt plan output, wait for the operator's explicit confirmation, and record both before proceeding. This skill never proceeds past PRECHECK-APPLY on its own; doing so is a work-unit failure under AC-4.4. |
| remote docker context answers over the port forward | `make build INSTANCE=<name>`, and every command that needs the daemon reachable through the tunnel | docker version did not answer within DOCKER_HANDSHAKE_TIMEOUT over the forwarded port | Confirm the port forward is still established (SSM_FORWARD_TIMEOUT governs how long this skill waits for it) and that the daemon behind it is running, then re-probe with `hostprobe.probe_docker(runner, requested_context=<the created context>)` before continuing. |

## Procedure

1. Probe this machine with `hostprobe`, exactly as `/devcontainer:setup-local`
   step 1 does: operating system, the five prerequisite tools, and the
   configured docker contexts. Follow the `## Checks` remedy for any failure
   before continuing.
2. Report the probe results, as `/devcontainer:setup-local` step 2 does.
3. Run the interview from the `## Questions` table above, setting `backend`
   to `remote` without asking it. Ask `aws_profiles` only when the answered
   `aws_config_enabled` is `true`, and `host_proxy_url` only when the
   answered `host_proxy` is `true`; ask every other row unconditionally.
4. Validate the collected answers with `answers.validate`, exactly as
   `/devcontainer:setup-local` step 4 does. If any field fails, report every
   failing field and its rule in one message and re-ask only those fields;
   do not proceed until every answer validates.
5. Render, commit and verify the three private files exactly as
   `/devcontainer:setup-local` steps 6 through 8 do:
   `render.render_all`, then `render.write_all(rendered, root,
   overwrite=False)` (refusing and naming every existing path rather than
   merging into or replacing one), then `verify.verify_all` reporting every
   finding. `render.render_shell_env` writes `REMOTE_INSTANCE_ID`,
   `REMOTE_AWS_REGION` and `REMOTE_AWS_PROFILE`
   because the answered `backend` is `remote`.
6. Verify the SSO session for the answered `remote_aws_profile` with
   `hostprobe.probe_aws_identity(runner, profile=<remote_aws_profile>)`. On
   failure, follow the `## Checks` remedy above: state the exact
   `aws sso login` command, wait, then re-probe before continuing.
7. Confirm the SSM agent for the answered `remote_instance_id` reports
   `Online` by calling `transport.ensure_agent_online(runner,
   instance_id=<remote_instance_id>, profile=<remote_aws_profile>,
   region=<remote_aws_region>)`
   (`.claude/plugins/devcontainer/scripts/devcontainer_config/transport.py`,
   E6-F2-S1-T1), which raises for any status other than `Online` -- naming
   either the reported ping status, or that SSM has no record of the
   instance at all, meaning it is stopped, terminated, or the configured id
   is wrong. This step never calls `instances.resolve`: that function
   selects an instance NAME from the `remote-instances/` directories
   `instances.discover` already lists (the `INSTANCE` /
   `DEFAULT_REMOTE_INSTANCE` / sole-directory order), whereas this skill's
   `<name>` comes directly from the operator's `/devcontainer:setup-remote
   INSTANCE=<name>` invocation and `remote_instance_id` is the separate,
   already-answered EC2 id -- neither is something to resolve here. On a
   first run, `remote-instances/<name>` does not exist until step 8 creates
   it, so calling `instances.resolve` at this point would raise
   `NoInstancesConfiguredError` telling the operator to run this very
   skill. On failure, follow the `## Checks` remedy above: stop naming the
   instance id and the agent state, and do not attempt the forward.
8. If provisioning or changing this instance's `remote-instances/<name>`
   Terragrunt state is needed to proceed -- creating it for the first time,
   per spec Section 2 G5, or changing a resource that module owns -- reach
   PRECHECK-APPLY: present the plan output, wait for the operator's explicit
   confirmation, and record both before continuing. This skill never runs
   `terragrunt apply` on its own confirmation and never proceeds past
   PRECHECK-APPLY automatically; doing so is a work-unit failure under
   AC-4.4. When no such change is needed, record that and continue.
9. Create the CA for `<name>` if one does not already exist, then issue the
   server and client certificates, per spec Section 5.5, by calling
   `certs.create_ca`, `certs.issue_server` and `certs.issue_client`
   (`.claude/plugins/devcontainer/scripts/devcontainer_config/certs.py`,
   spec Section 4.5, E6-F1-S1-T1) rather than invoking `openssl` by hand:
   CA private key at `<certs-root>/<name>/ca/ca-key.pem` mode `0600`, CA
   public certificate at `<certs-root>/<name>/ca/ca.pem` mode `0644`,
   lifetime `CERT_CA_DAYS`; server certificate with SANs `IP:127.0.0.1` and
   `DNS:localhost`, lifetime `CERT_SERVER_DAYS`; client certificate at
   `<certs-root>/<name>/cert.pem` mode `0644` and client key at
   `<certs-root>/<name>/key.pem` mode `0600`, lifetime `CERT_CLIENT_DAYS`.
   All four paths are outside the repository entirely, which removes the
   class of mistake rather than relying on an ignore rule.
10. Publish the server key, the server certificate and the CA certificate to
    the Parameter Store entries `certs.publication_set(<name>)` returns
    (spec Section 5.3), never a hand-written path:
    `/devcontainer/<name>/tls/server-key.pem` (SecureString),
    `/devcontainer/<name>/tls/server-cert.pem` (SecureString), and
    `/devcontainer/<name>/tls/ca.pem` (String). The CA private key and the
    client certificate and key never leave the laptop: `certs.publication_set`
    has no entry for any of the three and raises if one is requested.
11. Allocate the local port this instance's forward binds to, by calling
    `transport.allocate_local_port`
    (`.claude/plugins/devcontainer/scripts/devcontainer_config/transport.py`,
    spec Section 4.5, E6-F2-S1-T1) rather than picking a port by hand: it
    reuses the port already recorded in `<repo-slug>-<name>`'s docker
    context endpoint when that context already exists, or binds an
    OS-assigned free port when it does not (spec Section 9: "never a fixed
    number"). If the port already recorded for `<repo-slug>-<name>` is held
    by a foreign process, `transport.allocate_local_port` raises
    `PortOccupiedError` naming the port and the context rather than silently
    allocating a different one, since a silent move would strand every
    docker context and configuration that recorded the old value: an
    operator action (free the port, then retry), not a self-fix. Create the
    docker context `<repo-slug>-<name>` (`general-dev-<name>` in this
    repository; spec Section 9's addressing table) addressed at
    `tcp://127.0.0.1:<the allocated port>`, over the forward the next step
    establishes on that same port.
12. Establish the SSM port forward to the instance's `DOCKER_TLS_PORT` by
    calling `transport.start_forward` with the port allocated in the
    previous step, then confirm it answers by calling `transport.wait_ready`,
    bounded by `SSM_FORWARD_TIMEOUT`. A forward that does not answer within
    that timeout raises naming the instance, the local port and
    `SSM_FORWARD_TIMEOUT`; it is never retried silently, and this skill never
    reports a forward established because the session process is merely
    still alive -- only `wait_ready` observing the local port accept a
    connection does that.
13. Complete the docker version handshake against the new context, bounded
    by `DOCKER_HANDSHAKE_TIMEOUT` (the same deadline
    `hostprobe.probe_docker`'s handshake check uses), reporting the server
    version reached. On failure, follow the `## Checks` remedy above: confirm
    the forward and the daemon, then re-probe with `hostprobe.probe_docker`
    before continuing.
14. End by naming `make build INSTANCE=<name>`. This skill does not run it.

## Failure semantics

| Condition | Behavior |
|---|---|
| A precondition check fails (a tool absent, the docker context list unobtainable, the SSO session invalid, the instance not running or its SSM agent not online, the forward not answering, the engine not answering) | Stop at that check. Name what failed, what it prevents, and the exact remedy. Do not run later checks that depend on it. |
| A step needs the operator (a browser SSO login, an AWS resource only an operator may create or destroy) | State the exact command or the exact plan output, wait for the operator to act, then re-verify (`hostprobe` for SSO, the recorded confirmation for a gate) before continuing. Never assume the action succeeded. |
| A gate is reached (Section 4.4) | Present the evidence and stop; never self-approve. This skill reaches `GATE-APPLY`, named `PRECHECK-APPLY` in this document, whenever provisioning or changing `remote-instances/<name>`'s Terragrunt state is needed: it records the plan output and the verification result, then waits for the operator's explicit confirmation before any apply proceeds. Proceeding without that recorded confirmation is a work-unit failure under AC-4.4; this skill states so rather than offering a way past it. `GATE-CUTOVER` and `GATE-DESTROY` never apply to this skill: it performs no phase-4 cutover and no `terragrunt destroy`. |
| One or more answers fail `answers.validate` | Report every failing field and its rule in one message, not the first. Re-ask only those fields. Never accept a partially valid answer set and never call `render.write_all`. |
| The operator aborts the interview | Leave no partial configuration: nothing is written, no AWS resource is created or destroyed, no certificate is issued, and no docker context is created until every answer validates and the file-write step (Procedure step 5) runs, so an abort during the interview leaves the working tree, every docker context, and every AWS resource exactly as they were. |
| An action the skill took did not verify (`verify.verify_all` reports a finding, Procedure step 8's PRECHECK-APPLY has no recorded confirmation, or Procedure step 13's handshake still fails after the re-probe) | Report the action taken, the verification that failed, and the resulting machine state: the three files are on disk but not confirmed consistent, an apply is recorded as needed but not yet confirmed, or the docker context and the forward exist but the daemon behind them is not confirmed reachable. Never retry silently and never report success having failed. |

## Related specifications

- Section 2, G2, `repos/spec/devcontainer-platform.md`: the worked
  transcript this skill reproduces.
- Section 4.2 and 4.2.1: what this skill asks, does, ends by, and the
  interaction contract governing browser SSO login and every Terragrunt
  apply; the remote check list this skill's own `## Checks` table adds to
  `/devcontainer:setup-local`'s.
- Section 4.2.2: the failure-semantics table this skill's own table above
  instantiates.
- Section 4.4: `GATE-APPLY`, named `PRECHECK-APPLY` in this document, and
  that an agent reaching it records the plan and the confirmation rather
  than self-approving.
- Section 4.5: `answers`, `render`, `verify` and `hostprobe` are the only
  places a fact about the interview, a rendered file, or this machine is
  decided; `certs` (E6-F1-S1-T1, E6-F1-S1-T2) now owns both certificate
  generation and certificate-expiry facts (`certs.status_rows`,
  `certs.classify`), and `transport` (E6-F2-S1-T1) now owns the SSM port
  forward facts: SSM agent status, per-instance local port allocation, and
  forward readiness. `instances` (E8-F1-S1-T1) now owns instance facts:
  discovery, the resolution order and the Section 9 addressing
  derivations. This skill decides none of them itself.
- Section 5.1: the interview schema, including which fields are asked only
- Section 5.3 and 5.5: the Parameter Store paths and the certificate
  material, modes, SANs and lifetimes this skill's procedure follows
  exactly.
- Section 7.3: `CERT_CA_DAYS`, `CERT_SERVER_DAYS`, `CERT_CLIENT_DAYS`,
  `DOCKER_TLS_PORT`, `SSM_FORWARD_TIMEOUT` and `DOCKER_HANDSHAKE_TIMEOUT`.
- Section 9: the addressing table this skill's docker context, Parameter
  prefix and certificate paths follow.
- Section 11.5, phase 1: all nine skills are authored and no AWS call is
  made; the Terraform modules this skill's apply step drives arrive in E5,
  and the transport it establishes is proven on real hardware in E6-F3.
