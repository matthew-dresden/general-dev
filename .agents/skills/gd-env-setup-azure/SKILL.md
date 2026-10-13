---
name: gd-env-setup-azure
description: Configures this laptop to reach a remote azure docker engine over an Azure Relay hybrid connection the VM dials out to, with no inbound port, no public IP and no SSH anywhere, then verifies the result. The azure twin of gd-env-setup-remote.
---

# gd-env-setup-azure

`gd-env-setup-azure INSTANCE=<name>` provisions and connects one azure
engine: the azure twin of `gd-env-setup-remote`, with an Azure Relay hybrid
connection in place of the SSM port forward and `az` in place of the aws
CLI. The engine it creates is a rootless Docker daemon on an Azure virtual
machine whose only route from outside is a hybrid connection the VM dials
out to — no inbound security rule, no public IP, and no SSH anywhere, by
design. `<name>` is the human-chosen instance name, the same naming
contract every instance follows: `remote-instances/<name>/`, docker context
`<repo-slug>-<name>` (`general-dev-<name>` in this repository),
certificates under `<certs-root>/<name>/` (`$DOCKER_CONFIG/certs/<name>/`,
or `~/.docker/certs/<name>/` when `DOCKER_CONFIG` is unset; `<certs-root>`
stands for that root throughout this document), and, on azure, the relay
address record `<certs-root>/<name>/engine-address.json` the deploy writes.

This skill decides nothing about what a valid deployment looks like or what
"complete" means: the Terragrunt layer under `remote-instances/` (the azure
root `root-azure.hcl` and `_envcommon/remote-azure.hcl`) and the module in
`provider/azure/` own the deployment, `make instance-init` and
`make instance-deploy` own the scaffold-then-converge sequence, and
`hostprobe` owns every fact about this machine. Certificate generation and
publication are owned by the `certs` module
(`certs.create_ca`, `certs.issue_server`, `certs.issue_client`,
`certs.azure_publication_set`, `certs.KeyVaultClient`,
`certs.publish_to_key_vault`), and the relay transport by
`devcontainer_config.transport_azure` (`start_listener`, the sender dial,
the context and handshake it reuses from `transport`). The azure
operations-facing checklist an operator team would follow by hand is
`docs/azure-requirements.md`; this skill drives the make targets that
produce the same result.

The questions this skill asks that no `answers` schema field carries — the
azure location, the VM size, and the operator's own Entra object ids — are
not interview schema fields and render into no private file: they name the
deployment, not the developer. For that reason this skill declares no
`Interview backend` and keeps no `## Questions` table, the same
precedent `gd-instance-list` states for a question that belongs to
something other than the `answers` schema; the local development-machine
interview those schemas govern is `gd-env-setup-local`'s and
`gd-env-setup-remote`'s, and this skill requires the three private files to
verify clean before it provisions anything rather than rendering them
itself.

## Invocation map

| Step | What this skill runs | Notes |
|---|---|---|
| Probe and verify local configuration | `hostprobe`, `verify.verify_all` | The machine probe plus the azure CLI presence check; the three private files must verify clean before anything is provisioned. |
| Verify the azure login | `az account show` through `hostprobe`'s runner | `az login` is the operator's action, never an assumed one. |
| Scaffold | `make instance-init INSTANCE=<name> CLOUD=azure LOCATION=<location>` (`VM_SIZE=` optional) | Writes `remote-instances/<name>/terragrunt.hcl`; never deploys. `REGION=` and `AMI=` are EC2-only and refused on azure. |
| Edit the scaffold | None: this skill states the edit and waits | Adds the operator's Entra object id to `relay_sender_object_ids` and `keyvault_officer_object_ids` in the scaffolded file; this skill never edits a scaffolded file itself. |
| Terragrunt changes | PRECHECK-APPLY before `make instance-deploy` | Only when the deployment must be created or changed; never self-approved. |
| Converge | `make instance-deploy INSTANCE=<name>` | Provisions the platform unit and VM, carries the two object-id grants in as role assignments, writes the relay address record, pushes secrets. Refuses replacement without `CONFIRM=replace`. |
| Certificates | `certs.create_ca`, `certs.issue_server`, `certs.issue_client`, published through `certs.azure_publication_set(<name>)` into `certs.KeyVaultClient` | First issuance only (`## Procedure` steps 7-8); renewal is `gd-cert-lifecycle`'s. |
| Forward and context | `make connect-start INSTANCE=<name>`, which starts the VM-side listener and opens the relay forward | `make remote` is the standing form after setup: it opens the forward and points docker and VS Code at the engine. |
| Handshake and handoff | The docker version handshake, then name `make build INSTANCE=<name>` | This skill never runs the build. |

## Checks

| Check | Prevents | Failure message | Remedy |
|---|---|---|---|
| azure CLI present and logged in | every az call this skill and the make targets make: provisioning, the state store's subscription lookup, and the relay sender's token | `az` is not on PATH, or `az account show` answers nothing | State `az login`, wait for the operator to complete the browser login, then re-probe before continuing. Never proceed on the assumption the login succeeded. |
| the three private files complete and consistent | a build whose rendered configuration disagrees with the machine that pushed it | `verify.verify_all` reports a finding naming the file and the rule | Report every finding with its own remedy (`gd-env-setup-local` and `gd-env-setup-remote` own the re-render), and stop before provisioning: an azure deploy built on inconsistent files converges the wrong configuration. |
| PRECHECK-APPLY recorded before `make instance-deploy` | an autonomous run creating Azure resources without a recorded operator confirmation | deploy reached with no recorded plan and confirmation | Present the plan output and stop until the operator confirms; record both before running the target. This skill never proceeds past PRECHECK-APPLY on its own. |
| object ids present in the scaffolded inputs | a deployment that provisions cleanly and cannot be reached or supplied: the connect has no sender grant, the push no officer grant | the scaffolded `relay_sender_object_ids` or `keyvault_officer_object_ids` is empty or absent | State the two assignments to add, with `az ad signed-in-user show --query id` as the way to find the id, wait for the operator to edit the scaffolded file, then re-read it before continuing. |
| relay address record written | a connect with nothing to dial | no `engine-address.json` under `<certs-root>/<name>/` after the deploy | Re-run `make instance-deploy INSTANCE=<name>`, which writes the record from the Terragrunt outputs; never compose one by hand. |
| relay listener running before the forward opens | the first docker call dying on a 404 no-listener answer | the run-command that `make connect-start` issues reports the VM-side listener unit inactive | Read why with the same channel the start used (`az vm run-command invoke` running `journalctl -u <listener-unit> -n 50 --no-pager`), name the cause, and stop for the operator; never restart the forward on top of a listener that will not run. |
| docker answers over the relay forward | `make build INSTANCE=<name>`, and every command that needs the daemon reachable through the relay | `docker version` did not answer within `DOCKER_HANDSHAKE_TIMEOUT` over the forwarded port | Confirm the forward is open (`make connect-status INSTANCE=<name>`) and the listener is running, then re-probe with `hostprobe.probe_docker` before continuing. A 401 first means the Entra token expired: state `az login`, wait, then `make connect-refresh`. |

## Procedure

1. Probe this machine with `hostprobe`: operating system, the prerequisite
   tools, and the configured docker contexts — plus the azure CLI, this
   path's one extra tool (`brew install azure-cli` installs it). Follow the
   `## Checks` remedy for any failure before continuing.
2. Verify the three private files with `verify.verify_all`. They must be
   complete, placeholder-free and mutually consistent before anything is
   provisioned; where a finding's remedy is a re-render, name the setup
   skill that owns it and stop.
3. Verify the azure login: `az account show` must answer with the
   subscription the deployment targets. On failure, state `az login`, wait
   for the operator, and re-probe before continuing.
4. Ask the three azure questions no schema field carries: the location
   (`az account list-locations` names the valid spellings), the VM size
   (default `Standard_D2pls_v5`; its architecture must match the image URN,
   and an x64 size needs the x64 server URN), and the operator's Entra
   object id, found with `az ad signed-in-user show --query id`.
5. Run `make instance-init INSTANCE=<name> CLOUD=azure
   LOCATION=<location>` (pass `VM_SIZE=` when the operator named one). The
   target writes `remote-instances/<name>/terragrunt.hcl` and never
   deploys; a re-run against an existing directory is refused rather than
   replacing edits.
6. Direct the operator's one edit: add their object id to
   `relay_sender_object_ids` and `keyvault_officer_object_ids` in the
   scaffolded `inputs` block. The first grants the Azure Relay Sender role
   on the namespace the deployment creates — the right to open the sender
   half of the hybrid connection the forward rides; the second grants Key
   Vault Secrets Officer on the vault — the right to publish the
   instance's TLS material and secrets into it. This skill never edits the
   file itself; it re-reads the file and applies the `## Checks` remedy
   until both lists carry the id.
7. Reach PRECHECK-APPLY: run `make instance-plan INSTANCE=<name>`, present
   the plan output, wait for the operator's explicit confirmation, and
   record both. This skill never runs a converge on its own confirmation;
   doing so is a work-unit failure.
8. Run `make instance-deploy INSTANCE=<name>`. It applies the guarded plan,
   provisions the platform unit and the VM, carries the two object-id
   grants in as role assignments, records the relay address record
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
10. Publish the server key, the server certificate and the CA certificate
    into the instance's Key Vault: iterate
    `certs.azure_publication_set(<name>)`, never a hand-written secret
    name, and write each through `certs.KeyVaultClient.write_secret`,
    reading every secret back and comparing it against what was written
    before reporting anything published. The vault name comes from the
    relay address record, never from a prompt. The CA private key and the
    client certificate and key never leave the laptop:
    `azure_publication_set` has no entry for any of the three.
11. Run `make connect-start INSTANCE=<name>`. It starts the VM-side relay
    listener first — one synchronous `az vm run-command invoke`, because
    the listener stops itself when idle so the relay accrues listener-hours
    only while work is happening — then opens the forward as a background
    daemon, coupling the laptop's sender to the hybrid connection under a
    fresh Entra token, and returns once docker answers through it. A cold
    start on a freshly started VM can take tens of seconds; that wait is
    the design, not a hang.
12. Complete the docker version handshake against the context the forward
    addresses, bounded by `DOCKER_HANDSHAKE_TIMEOUT`, reporting the server
    version reached. On a 401 first, apply the `## Checks` remedy: `az
    login`, wait, then `make connect-refresh INSTANCE=<name>`, and re-probe.
13. End by naming `make build INSTANCE=<name>`, then VS Code → **Dev
    Containers: Attach to Running Container…**. This skill runs neither.

## Failure semantics

| Condition | Behavior |
|---|---|
| A precondition check fails (a tool absent, the login invalid, the private files inconsistent, the plan refused, the record missing, the listener inactive, the handshake unanswered) | Stop at that check. Name what failed, what it prevents, and the exact remedy. Do not run later steps that depend on it. |
| A step needs the operator (a browser `az login`, the scaffold edit, the PRECHECK-APPLY confirmation, an Azure resource only an operator may create or destroy) | State the exact command or the exact plan output, wait for the operator to act, then re-verify (the re-probe, the re-read file, or the recorded confirmation) before continuing. Never assume the action succeeded. |
| A gate is reached | Present the evidence and stop; never self-approve. This skill reaches PRECHECK-APPLY every time it converges, and it never runs `make instance-destroy`: teardown is `gd-instance-destroy`'s, behind GATE-DESTROY. |
| `make instance-deploy` refuses for replacement | Report the refusal verbatim: replacement is an operator decision made with `CONFIRM=replace` in their own invocation, never this skill's. |
| The operator aborts | Nothing is provisioned until `make instance-deploy` actually runs; an abort before that leaves only the `remote-instances/<name>/` scaffold, which a later run reuses or the operator deletes. Certificate and secret writes happen only after the record exists, so an abort mid-certificate-step leaves the deployment intact and the material issuance resumable. |
| An action the skill took did not verify (a published secret's read-back disagrees, or the handshake still fails after the re-probe) | Report the action taken, the verification that failed, and the resulting state: the material generated (and, for a publish, written but unconfirmed), the deployment provisioned, the forward open but the daemon unconfirmed. Never retry silently and never report success having failed. |

## Related skills

- `gd-env-setup-remote`: the EC2 twin; the SSO-session, instance-agent and
  SSM-forward checks there have their azure counterparts in this document's
  `## Checks` table, and the two skills share `hostprobe`, `verify` and the
  `certs` generation half unchanged.
- `gd-cert-lifecycle`: owns renewal and expiry afterwards; this skill only
  performs the first issuance.
- `gd-instance-list`: validates the provisioned engine end to end.
- `gd-instance-destroy`: the destructive counterpart, behind GATE-DESTROY.
