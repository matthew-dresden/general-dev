---
name: gd-env-setup-gcp
description: Configures this laptop to reach a remote gcp docker engine over an IAP TCP tunnel the gcloud CLI opens, with no external IP, no SSH and one ingress rule admitting Google's IAP range, then verifies the result. The gcp twin of gd-env-setup-remote.
---

# gd-env-setup-gcp

`gd-env-setup-gcp INSTANCE=<name>` provisions and connects one gcp engine:
the gcp twin of `gd-env-setup-remote`, with an IAP TCP tunnel in place of
the SSM port forward and `gcloud` in place of the aws CLI. The engine it
creates is a rootless Docker daemon on a Google Cloud virtual machine whose
only route from outside is an IAP tunnel dialing the VM's internal address
after the operator's gcloud identity passes IAM: no external IP, no SSH,
and the network's one ingress rule admits Google's IAP range
(`35.235.240.0/20`) to the daemon's TLS port, by design. `<name>` is the
human-chosen instance name, the same naming contract every instance
follows: `remote-instances/<name>/`, docker context `<repo-slug>-<name>`
(`general-dev-<name>` in this repository), certificates under
`<certs-root>/<name>/` (`$DOCKER_CONFIG/certs/<name>/`, or
`~/.docker/certs/<name>/` when `DOCKER_CONFIG` is unset; `<certs-root>`
stands for that root throughout this document), and the engine address
record `<certs-root>/<name>/engine-address.json` the deploy writes.

This skill decides nothing about what a valid deployment looks like or what
"complete" means: the Terragrunt layer under `remote-instances/` (the gcp
root `root-gcp.hcl` and `_envcommon/remote-gcp.hcl`) and the module in
`provider/gcp/` own the deployment, `make instance-init` and
`make instance-deploy` own the scaffold-then-converge sequence,
`devcontainer_config.state_store_gcp` owns the state bucket its ensure
bootstraps, and `hostprobe` owns every fact about this machine. Certificate
generation and publication are owned by the `certs` module
(`certs.create_ca`, `certs.issue_server`, `certs.issue_client`,
`certs.gcp_publication_set`, `certs.SecretManagerClient`,
`certs.publish_to_secret_manager`), and the IAP transport by
`devcontainer_config.transport_gcp` (the supervised gcloud child, the port
poll, the boot-applied material waits it reuses from `transport`). The gcp
operations-facing checklist an operator team would follow by hand is
`docs/gcp-requirements.md`; this skill drives the make targets that produce
the same result.

Two things distinguish this path from its siblings, stated here because
they change the procedure's shape:

- The material install is boot-applied, not pushed: the VM's startup script
  fetches the TLS secrets and their checksum at every boot, applies them
  only on drift, and records the result in the `general-dev/tls-checksum`,
  `general-dev/provisioned` and `general-dev/error` guest attributes.
  Publication ends with a boot that applies; `make cert-install INSTANCE=<name>`
  is the bounded wait on those attributes, not a push.
- The scaffold needs no operator-owned edit. The azure twin stops for the
  operator's Entra object ids; the gcp scaffold's inputs are complete as
  written, so this skill's edit step is only the offer to adjust sizing
  before the plan, and it never edits the file itself either way.

The questions this skill asks that no `answers` schema field carries, the
gcp project id, the zone, and an optional VM size, are not interview schema
fields and render into no private file: they name the deployment, not the
developer. For that reason this skill declares no `Interview backend` and
keeps no `## Questions` table, the same precedent `gd-env-setup-azure`
states for a question that belongs to something other than the `answers`
schema; the local development-machine interview those schemas govern is
`gd-env-setup-local`'s and `gd-env-setup-remote`'s, and this skill requires
the three private files to verify clean before it provisions anything
rather than rendering them itself.

## Invocation map

| Step | What this skill runs | Notes |
|---|---|---|
| Probe and verify local configuration | `hostprobe`, `verify.verify_all` | The machine probe plus the gcloud CLI presence check; the three private files must verify clean before anything is provisioned. |
| Verify the gcloud login | `gcloud auth list` through `hostprobe`'s runner | `gcloud auth login` is the operator's action, never an assumed one; the tunnel authenticates with this session later, so it is verified here and again at connect. |
| Scaffold | `make instance-init INSTANCE=<name> CLOUD=gcp PROJECT=<project-id> ZONE=<zone>` (`VM_SIZE=` optional, default `e2-standard-2`) | Writes `remote-instances/<name>/terragrunt.hcl`; never deploys. The region derives from the zone; `REGION=` and `AMI=` are EC2-only and refused on gcp. |
| Offer the sizing edit | None: this skill states the commonly edited inputs and waits | Machine type, volume sizes, CIDR blocks and tags are the operator's to change in the scaffolded file; this skill never edits a scaffolded file itself, and re-reads it before planning when the operator chose to. |
| Terragrunt changes | PRECHECK-APPLY before `make instance-deploy` | Only when the deployment must be created or changed; never self-approved. |
| Converge | `make instance-deploy INSTANCE=<name>` | Ensures the state bucket, provisions the network with its one ingress rule, the Cloud Router and NAT, the platform unit and the VM, writes the engine address record from the Terragrunt outputs, and pushes the secrets. Refuses replacement without `CONFIRM=replace`. |
| Certificates | `certs.create_ca`, `certs.issue_server`, `certs.issue_client`, published through `certs.publish_to_secret_manager` with a `certs.SecretManagerClient` bound to the record's project | First issuance only (`## Procedure` steps 8 and 9); renewal is `gd-cert-lifecycle`'s. |
| Forward and context | `make connect-start INSTANCE=<name>`, which opens the IAP tunnel as a background daemon | The gcp kind has no instance-side unit to start first: the VM-side contract is the startup script and its guest attributes. `make remote` is the standing form after setup. |
| Handshake and handoff | The docker version handshake, then name `make build INSTANCE=<name>` | This skill never runs the build. |

## Checks

| Check | Prevents | Failure message | Remedy |
|---|---|---|---|
| gcloud CLI present and logged in | every gcloud call this skill and the make targets make: provisioning, the state bucket ensure, the secret probes, and the tunnel child itself | `gcloud` is not on PATH, or `gcloud auth list` answers with no active account | State `gcloud auth login`, wait for the operator to complete the browser login, then re-probe before continuing. Never proceed on the assumption the login succeeded; the tunnel authenticates with this same session. |
| the three private files complete and consistent | a build whose rendered configuration disagrees with the machine that pushed it | `verify.verify_all` reports a finding naming the file and the rule | Report every finding with its own remedy (`gd-env-setup-local` and `gd-env-setup-remote` own the re-render), and stop before provisioning: a gcp deploy built on inconsistent files converges the wrong configuration. |
| PRECHECK-APPLY recorded before `make instance-deploy` | an autonomous run creating Google Cloud resources without a recorded operator confirmation | deploy reached with no recorded plan and confirmation | Present the plan output and stop until the operator confirms; record both before running the target. This skill never proceeds past PRECHECK-APPLY on its own. |
| engine address record written | a connect with nothing to dial: no instance name, zone, project, region, secrets prefix or state bucket | no `engine-address.json` under `<certs-root>/<name>/` after the deploy | Re-run `make instance-deploy INSTANCE=<name>`, which writes the record from the Terragrunt outputs; never compose one by hand. |
| provisioning marker reported before the first install | an install wait polling a VM whose first boot is still inside cloud-init | `GCP_PROVISIONED_TIMEOUT` elapsed with the `general-dev/provisioned` attribute still absent | Read the serial console (`gcloud compute instances get-serial-port-output`) for the cloud-init progress and any `FATAL:` line, resolve what it names, then re-run `make cert-install INSTANCE=<name>`; never shorten the wait or skip the marker. |
| docker answers over the IAP tunnel | `make build INSTANCE=<name>`, and every command that needs the daemon reachable through the tunnel | `docker version` did not answer within `DOCKER_HANDSHAKE_TIMEOUT` over the forwarded port | Confirm the forward is open (`make connect-status INSTANCE=<name>`) and re-probe with `hostprobe.probe_docker` before continuing. An authorization failure first means the gcloud session expired: state `gcloud auth login`, wait, then `make connect-refresh INSTANCE=<name>`. |

## Procedure

1. Probe this machine with `hostprobe`: operating system, the prerequisite
   tools, and the configured docker contexts — plus the gcloud CLI, this
   path's one extra tool (`brew install google-cloud-sdk` installs it).
   Follow the `## Checks` remedy for any failure before continuing.
2. Verify the three private files with `verify.verify_all`. They must be
   complete, placeholder-free and mutually consistent before anything is
   provisioned; where a finding's remedy is a re-render, name the setup
   skill that owns it and stop.
3. Verify the gcloud login: `gcloud auth list` must answer with the active
   account, and the project the deployment targets must be named in the
   questions below, never read from the CLI's ambient default. On failure,
   state `gcloud auth login`, wait for the operator, and re-probe before
   continuing.
4. Ask the three gcp questions no schema field carries: the project id (a
   real project in the operator's possession), the zone (for example
   `us-east4-a`; the region derives from it), and the VM size (default
   `e2-standard-2`; check the region's CPU quota before naming a larger
   one, per `docs/gcp-requirements.md`'s Section 2 commands).
5. Run `make instance-init INSTANCE=<name> CLOUD=gcp PROJECT=<project-id>
   ZONE=<zone>` (pass `VM_SIZE=` when the operator named one). The target
   writes `remote-instances/<name>/terragrunt.hcl` and never deploys; a
   re-run against an existing directory is refused rather than replacing
   edits.
6. Offer the sizing edit: state the commonly edited inputs (machine type,
   volume sizes, CIDR blocks, tags) and wait. There is no operator-owned
   grant to add here, the azure twin's one stopping step has no gcp
   analog; if the operator edits the file, re-read it and continue, and if
   not, continue as scaffolded. This skill never edits the file itself.
7. Reach PRECHECK-APPLY: run `make instance-plan INSTANCE=<name>`, present
   the plan output, wait for the operator's explicit confirmation, and
   record both. This skill never runs a converge on its own confirmation;
   doing so is a work-unit failure.
8. Run `make instance-deploy INSTANCE=<name>`. It ensures the instance's
   state bucket, applies the guarded plan (network with its one ingress
   rule, Cloud Router and NAT, service account, the six secret vehicles,
   the VM and its disks), records the engine address record
   `<certs-root>/<name>/engine-address.json` from the Terragrunt outputs,
   and pushes the secrets. Verify the record exists and parses before
   continuing.
9. Create the CA if one does not exist, then issue the server and client
   certificates, by calling `certs.create_ca`, `certs.issue_server` and
   `certs.issue_client` — the same functions `gd-env-setup-remote` calls
   for the EC2 kind, with the same modes, SANs and lifetimes: CA private
   key at `<certs-root>/<name>/ca/ca-key.pem` mode `0600`, server SANs
   `IP:127.0.0.1` and `DNS:localhost`, client material at
   `<certs-root>/<name>/cert.pem` and `key.pem`.
10. Publish the server key, the server certificate, the CA certificate and
    their checksum: call `certs.publish_to_secret_manager` with a
    `certs.SecretManagerClient` bound to the project the record names. The
    four secret ids are the ones `certs.gcp_publication_set(<name>)`
    returns (`<prefix>-tls-server-key`, `-tls-server-cert`, `-tls-ca`,
    `-tls-checksum`), never a hand-written id, and the prefix is the
    record's `secrets_prefix`, never a prompt answer; every secret is read
    back and compared before anything is reported published. The CA
    private key and the client certificate and key never leave the laptop:
    the publication set has no entry for any of the three.
11. Apply the material: run `make cert-install INSTANCE=<name>`, which
    waits for the VM's boot-applied install — the startup script fetches
    the four secrets, compares the checksum against the
    `general-dev/tls-checksum` attribute, applies on drift, and records
    `general-dev/provisioned=TRUE` — bounded by `GCP_MATERIAL_APPLY_TIMEOUT`.
    A timeout surfaces the VM's own `general-dev/error` diagnostic; apply
    the `## Checks` remedy rather than retrying blindly.
12. Run `make connect-start INSTANCE=<name>`. It opens the IAP tunnel as a
    background daemon — one supervised `gcloud compute start-iap-tunnel`
    child authenticated with the ambient gcloud session, its local port
    allocated and recorded in the instance's docker context — and returns
    once docker answers through it. The gcp kind has no listener unit to
    start first; the wait is the tunnel's own, bounded by
    `GCP_TUNNEL_CONNECT_TIMEOUT`.
13. Complete the docker version handshake against the context the tunnel
    addresses, bounded by `DOCKER_HANDSHAKE_TIMEOUT`, reporting the server
    version reached. On an authorization failure first, apply the
    `## Checks` remedy: `gcloud auth login`, wait, then
    `make connect-refresh INSTANCE=<name>`, and re-probe.
14. End by naming `make build INSTANCE=<name>`, then VS Code → **Dev
    Containers: Attach to Running Container…**. This skill runs neither.

## Failure semantics

| Condition | Behavior |
|---|---|
| A precondition check fails (a tool absent, the login invalid, the private files inconsistent, the plan refused, the record missing, the marker absent, the handshake unanswered) | Stop at that check. Name what failed, what it prevents, and the exact remedy. Do not run later steps that depend on it. |
| A step needs the operator (a browser `gcloud auth login`, the sizing edit, the PRECHECK-APPLY confirmation, a Google Cloud resource only an operator may create or destroy) | State the exact command or the exact plan output, wait for the operator to act, then re-verify (the re-probe, the re-read file, or the recorded confirmation) before continuing. Never assume the action succeeded. |
| A gate is reached | Present the evidence and stop; never self-approve. This skill reaches PRECHECK-APPLY every time it converges, and it never runs `make instance-destroy`: teardown is `gd-instance-destroy`'s, behind GATE-DESTROY. |
| `make instance-deploy` refuses for replacement | Report the refusal verbatim: replacement is an operator decision made with `CONFIRM=replace` in their own invocation, never this skill's. |
| An attribute wait times out | Report the deadline variable (`GCP_PROVISIONED_TIMEOUT` or `GCP_MATERIAL_APPLY_TIMEOUT`), the last observed guest attribute, and the VM's own `general-dev/error` diagnostic verbatim. Resolve the cause the diagnostic names in the project, then re-run the same command; never shorten a timeout to force a pass. |
| The operator aborts | Nothing is provisioned until `make instance-deploy` actually runs; an abort before that leaves only the `remote-instances/<name>/` scaffold, which a later run reuses or the operator deletes. Certificate and secret writes happen only after the record exists, so an abort mid-certificate-step leaves the deployment intact and the material issuance resumable. |
| An action the skill took did not verify (a published secret's read-back disagrees, or the handshake still fails after the re-probe) | Report the action taken, the verification that failed, and the resulting state: the material generated (and, for a publish, written but unconfirmed), the deployment provisioned, the tunnel open but the daemon unconfirmed. Never retry silently and never report success having failed. |

## Related skills

- `gd-env-setup-remote` and `gd-env-setup-azure`: the EC2 and azure twins;
  the SSO-session, instance-agent and forward checks there have their gcp
  counterparts in this document's `## Checks` table, and the three skills
  share `hostprobe`, `verify` and the `certs` generation half unchanged.
- `gd-cert-lifecycle`: owns renewal and expiry afterwards; this skill only
  performs the first issuance, and on this kind a renewal publishes new
  material that the next boot's drift comparison applies.
- `gd-instance-list`: validates the provisioned engine end to end.
- `gd-instance-destroy`: the destructive counterpart, behind GATE-DESTROY.
