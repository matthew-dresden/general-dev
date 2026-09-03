---
name: certs
description: Owns the certificate lifecycle for one remote instance -- create the CA, issue the server and client certificates, rotate the client certificate with the instance left running, and report expiry -- states that revocation does not exist rather than gesturing at it, and ends every material-changing operation by rewriting the docker context and completing a handshake before reporting success.
---

# certs

Section 4.2's own roster row gives `certs` one question and four jobs, quoted
here exactly: it "Asks" "Which instance"; it "Does" "Create CA, issue server
and client certificates, rotate, report expiry"; and it "Ends by" "Updated
docker context." Without this skill the certificate lifecycle exists only
inside `/devcontainer:setup-remote` (its own Procedure steps 9 and 10), so a
client certificate whose `CERT_CLIENT_DAYS` lifetime expires could be renewed
only by rerunning setup; this skill is the standing place that lifecycle lives
once an instance already exists, and `/devcontainer:setup-remote`'s own issuance steps stay the single
place the material is first created, per Section 3's "DO NOT reinvent" rule.

This skill obeys the one interaction contract every skill in Section 4.2
obeys, stated here in words rather than left implicit: where it cannot act
itself, it states the exact command, waits for the operator, then verifies
the result before continuing, and nothing is assumed to have worked. This
governs the one step below that needs the operator (`## Failure semantics`).

`certs` (spec Section 4.5) is implemented at
`.claude/plugins/devcontainer/scripts/devcontainer_config/certs.py`
(E6-F1-S1-T1): `certs.create_ca`, `certs.issue_server`, `certs.issue_client`
and `certs.rotate_client` (E6-F1-S2-T1) generate the material `## Operations`
names below, and `certs.publication_set(instance)` declares exactly the
Parameter Store entries Section 5.3 allows this instance's material to
publish. Every
operation below calls these functions directly rather than shelling out to
`openssl` by hand -- `certs.py` is the one place that ever does that
(Section 3.4's dependency rule) -- and every publish step iterates whatever
`certs.publication_set(instance)` returns rather than a hand-written list of
paths, so a Parameter Store path this skill publishes to can never drift
from the set that module enforces. `ca-key.pem` is never among them:
`certs.publication_set` raises rather than returning an entry for it under
any spelling, the CA private key's own second, structural enforcement of the
"never leaves the laptop" rule `## Material` states in prose (spec Section
5.5). Publication is `certs.publish`'s own
operation, behind `make cert-publish` (Section 4.1.2): it issues the server
material, writes each entry `publication_set` returned through
`catalog.CatalogClient.write_parameter`, then reads every one back through
`catalog.CatalogClient.read_parameter` and compares it against what it wrote
before reporting anything published. It reaches the store through that client
rather than shelling out to `aws` here for two reasons. The value travels to
the child process inside a `--cli-input-json` document on stdin and never in
argv, so a TLS private key is never visible in the process table nor echoed
back by this repository's own failure translator -- the identical invariant
`catalog`'s own module docstring states for a stored secret, which a
hand-written `put-parameter --value` call would break silently. And the error
classification an operator needs -- an expired SSO session, a denied prefix --
already exists there with its remedy, rather than being reinvented at this
call site. Publishing to `/devcontainer/<instance>/tls/*` (`## Operations`,
Section 5.3) is `certs`'s own path, never `catalog`'s or `devsecret`'s:
Section 4.3's `devsecret` CLI and the `catalog` module
`/devcontainer:secrets` wraps reach only
`/devcontainer/<scope>/secrets/<NAME>` (`/devcontainer:secrets`'s own "never
a second path to Parameter Store" concern), a disjoint prefix neither module
has any operation for, so this skill's own publish calls are a distinct
primitive, not a second implementation of one that already exists.

Generation and inspection are both landed now: `certs.not_after`,
`certs.days_remaining`, `certs.classify` and `certs.status_rows`
(E6-F1-S1-T2) are the inspection/expiry-arithmetic half Section 4.5 also
assigns `certs`, behind `make cert-status` today, not a contract documented
ahead of its landing. `## Operations`'s "Report expiry" row and `## Expiry`
below describe that command as it runs now.

Instance selection reuses Section 4.1.1's own resolution order (`INSTANCE` on
the command line, then `DEFAULT_REMOTE_INSTANCE`, then the sole directory
under `remote-instances/` if exactly one exists, otherwise fail listing what
is configured) rather than this skill inventing a second way to disambiguate
which instance "which instance" refers to.

Each operation has an operator-invocable entry point, so the lifecycle is
reachable without this skill as well as through it: `make cert-ca`,
`make cert-client`, `make cert-publish` and `make cert-status`. All four
resolve the instance through that same order -- the first three by way of
`.devcontainer/remote-docker/certs.sh`, which calls the shared resolver and
adds no addressing policy of its own. They are deliberately separate targets
rather than one idempotent "ensure": each underlying operation refuses to
overwrite existing material, so running the wrong one names the path that
already exists instead of silently replacing a certificate the other half of
the pair still trusts.

## Material

Section 5.5's own block, reproduced here as a table, one row per file. Every
path below is rooted at `certs.DEFAULT_CERTS_ROOT`/`instances.certs_root()`
(`$DOCKER_CONFIG/certs/<instance>/`, or `~/.docker/certs/<instance>/` when
`DOCKER_CONFIG` is unset); `<certs-root>` stands for that root in the table:

| File | Path | Mode | Leaves the laptop | Lifetime |
|---|---|---|---|---|
| CA private key | `<certs-root>/<instance>/ca/ca-key.pem` | `0600` | Never | `CERT_CA_DAYS` |
| CA public certificate | `<certs-root>/<instance>/ca/ca.pem` | `0644` | Published as `String` to `/devcontainer/<instance>/tls/ca.pem` (Section 5.3) | `CERT_CA_DAYS` |
| Client certificate | `<certs-root>/<instance>/cert.pem` | `0644` | Never -- Section 5.3 names no parameter path for it, so it is presented only over the TLS handshake itself, never persisted to the store | `CERT_CLIENT_DAYS` |
| Client private key | `<certs-root>/<instance>/key.pem` | `0600` | Never | `CERT_CLIENT_DAYS` |

All four paths are outside the repository entirely, which removes the class of
mistake rather than relying on an ignore rule (Section 5.5). The CA private
key and the client certificate and key never leave the laptop
(`/devcontainer:setup-remote`'s own Procedure step 10 states the identical
rule for the same three items).

The server key and server certificate are deliberately not a fifth and sixth
row here: Section 5.5's own path list names only the four files above, and
G2's own worked transcript (Section 2) shows the server certificate only as
"Issued ... " and "Published ...", never a "Wrote ~/.docker/certs/..." line
the way the CA key is shown. The server key and certificate are generated and
published in the same step, with no persistent path on the laptop at all;
`## Operations`' "Issue server certificate" row is the one place that
generation and its publication are described, not restated here.

## Operations

Every row below states its inputs, the function it calls in `certs.py`, the
files it leaves on the laptop, the Parameter Store paths it publishes (each
one an entry `certs.publication_set(instance)` returned, never a
hand-written path), and the verification performed before the operation is
reported complete. Every row that changes material this instance's docker
context depends on ends with `## Docker context`'s rewrite and handshake,
stated once there and cross-referenced here rather than repeated per row.
Every row's SAN or key-usage requirement is stated once in `## Requirements`
and cross-referenced here rather than restated per row.

| Operation | Inputs | Resulting files | Parameter Store paths | Verification |
|---|---|---|---|---|
| Create CA | The instance name, passed to `certs.create_ca`. Idempotent: if `<certs-root>/<instance>/ca/ca-key.pem` already exists, `certs.create_ca` raises naming the existing path rather than silently overwriting a private key that every issued certificate still depends on; this skill reports the existing CA's expiry (`## Expiry`) for that case instead of treating the raise as a failure. | `ca-key.pem` (`0600`) and `ca.pem` (`0644`), lifetime `CERT_CA_DAYS` (`## Material`). | The `ca.pem` entry `certs.publication_set(instance)` returns: `/devcontainer/<instance>/tls/ca.pem` as `String` (Section 5.3; not secret, so not `SecureString`). Never `ca-key.pem`: `publication_set` has no entry for it and raises if one is requested (this document's own introduction). | Parse the generated `ca.pem` to confirm its validity period matches a `CERT_CA_DAYS`-length lifetime, then read the published parameter back and compare its value against the local file's bytes -- an independent re-read after the publish, never trusted from the publish call's own exit code (Section 4.2.2's "nothing is assumed to have worked," the same pattern `/devcontainer:secrets`'s own `## Operations` table applies to a `devsecret set`). Ends with `## Docker context`. |
| Issue server certificate | An existing CA (`## Failure semantics`'s precondition row states what happens when one is missing), passed to `certs.issue_server`. No operator-chosen SANs: the SANs are fixed (`## Requirements`). | None persisted (`## Material`'s own note): `certs.issue_server` generates the key and certificate inside a `tempfile.TemporaryDirectory` at mode `0600`/`0644` and removes that directory before returning, handing both back as text. Never persisted under `<certs-root>/<instance>/`; the private key does exist on disk, briefly, inside that removed temporary directory. | The `server-key.pem` and `server-cert.pem` entries `certs.publication_set(instance)` returns: `/devcontainer/<instance>/tls/server-key.pem` and `/devcontainer/<instance>/tls/server-cert.pem`, both `SecureString` (Section 5.3), lifetime `CERT_SERVER_DAYS`. | Parse the generated certificate before it is ever published, confirming it carries exactly the two required SANs and no other (`## Requirements`, AC-5.2), then read both published parameters back and compare against what was generated, the same independent-re-read rule Create CA's row states. Ends with `## Docker context`, whose completed handshake is this operation's own strongest confirmation that the instance-side daemon actually accepted the published material. |
| Issue client certificate | An existing CA (`## Failure semantics`), passed to `certs.issue_client`. | `cert.pem` (`0644`) and `key.pem` (`0600`) (`## Material`), lifetime `CERT_CLIENT_DAYS`, carrying `clientAuth` (`## Requirements`). | None: `certs.publication_set` has no entry for the client certificate, matching Section 5.3's layout, which names no client-certificate parameter path (`## Material`). | Parse the generated certificate to confirm it carries `clientAuth` (`## Requirements`, AC-5.2). Ends with `## Docker context`. |
| Rotate client certificate | An existing CA and an existing client certificate to replace, passed to `certs.rotate_client` (E6-F1-S2-T1), which calls `certs.issue_client` and nothing else. **This operation touches nothing on the instance**: the daemon holds only `ca.pem` and accepts any client certificate that chains to it, so the replacement is accepted the moment it is presented -- no parameter is written, the daemon is never restarted, no `terragrunt apply` runs, and no docker context update is structurally required (the rewrite below runs anyway, as a verification, never as a repair). `certs.rotate_client` refuses, naming the instance and the `/devcontainer:certs INSTANCE=<name>` invocation that creates one, if no CA exists yet for this instance, rather than creating one implicitly: a fresh CA would silently invalidate every certificate the previous one already signed, including the server certificate this same daemon is already serving (AC-10.9). | `cert.pem` and `key.pem`, overwritten in place as a single atomic unit (`certs._commit_pair`): a rotation that fails during the commit (a read-only material root or a cross-device destination, both raising `OSError`) leaves the previous, still-valid pair, never a half-written key or a mismatched pair; a killed process between the backup and the install is outside that guarantee and leaves an outage for the operator to detect and repair by hand. The CA key, the CA certificate and the server material are byte-identical/unchanged throughout. | None, for the identical reason Issue client certificate names. | The identical parse-for-`clientAuth` check Issue client certificate performs, plus a verification that the new certificate still chains to the unchanged CA (`openssl verify -CAfile`). Ends with `## Docker context`. |
| Report expiry | None: `certs status` takes no instance selector and reports every instance under the certificate material root (`--root`, defaulting to `DEFAULT_CERTS_ROOT`). | None: this operation reads, never writes. | None: this operation reads, never writes. | `## Expiry` is this operation's own output shape and exit-code rule; there is nothing further to verify about a read. |

## Requirements

The server certificate carries SANs `IP:127.0.0.1` and `DNS:localhost` and no
other SAN, because the client connects through a loopback forward and TLS
validates the name the client used against the certificate, never the
instance's own identity. The instance hostname and the instance's private IP
are both wrong here and fail opaquely: neither is the name the client
actually dialed, so a handshake against either produces a generic name
mismatch that names neither the reissue nor the docker context, which is
exactly the failure `## Docker context`'s own rewrite-then-handshake step
exists to catch before it reaches an operator as a confusing downstream
error. The client certificate carries `clientAuth` so the daemon's mTLS check
accepts it as a client rather than rejecting a certificate with no key usage
that permits the role it is presented in. Both properties are verified by
parsing the generated certificate (AC-5.2, "Verified by parsing the generated
certificates in a test"), never assumed from the generation command's own
exit code -- the identical "an action did not verify itself" concern Section
4.2.2 states for every skill, applied here to a certificate rather than a
file write.

## Docker context

Every operation in `## Operations` that changes material the active docker
context depends on -- creating the CA (whose public certificate the context's
`ca` reference names), issuing or rotating the client certificate (whose
`cert`/`key` the context references directly), and issuing the server
certificate (whose acceptance by the instance-side daemon only the next
handshake can confirm) -- ends by rewriting the docker context
`general-dev-<instance>` (Section 9's addressing table) to reference the
current material, then completing a docker version handshake over that
context before reporting success, bounded by `DOCKER_HANDSHAKE_TIMEOUT`
(Section 7.3), the identical deadline `hostprobe.probe_docker`'s own
handshake check uses and the identical primitive
`/devcontainer:setup-remote`'s own Procedure step 13 reuses rather than a
second implementation of the same wait. A reissued or rotated certificate the
context does not reference is inert, and the failure it produces later is a
handshake error that names neither the reissue nor the context (this
document's own introduction); ending here rather than at the certificate
files closes exactly that gap. On a handshake that still fails after this
rewrite, `## Failure semantics`'s "an action did not verify" row applies:
this skill reports the material as generated (and, for a publish, published)
but not confirmed reachable, and never reports the operation complete.

## Revocation

**Rotation is not revocation, and this skill states so explicitly rather than
letting an operator infer it.** Docker supports neither CRL nor OCSP, so
certificate revocation does not exist (Section 3.6.3); this skill states that
plainly rather than performing a gesture that revokes nothing. "Rotate client
certificate" (`## Operations`) replaces `cert.pem`/`key.pem` with a fresh pair
signed by the same CA -- the superseded certificate is never added to a
revocation list, because there is no such list to add it to, and it stays
cryptographically valid until its own `notAfter` passes (`## Material`'s own
`CERT_CLIENT_DAYS` lifetime), exactly the same as any certificate this
authority ever signed. Removing the principal's `ssm:StartSession` permission
is the revocation mechanism instead: it is immediate, and it renders any
certificate that principal holds inert, because a certificate is useless
without the tunnel it authenticates inside. The certificate authenticates;
IAM authorizes (Section 3.6.2's own boundary table states the same division
for who may command the daemon versus who may open the tunnel). A request to
revoke a certificate is answered with this explanation and the
`ssm:StartSession` remedy, never with a report that a certificate was
revoked, because nothing would have been revoked -- and never by rotating the
certificate, which answers a different question ("issue a fresh one") than
the one being asked ("make the old one stop working").

## Expiry

`make cert-status` (Section 4.1.2) is this skill's own "report expiry"
operation; its output shape is reproduced here rather than restated
elsewhere. It reports the `client` and `ca` roles: the two certificates
`## Material` shows a persisted path for. The server certificate has no
entry: `## Material`'s own note above states it is generated and published
in one step with no persistent path on the laptop at all, so there is
nothing under `instances.certs_root()`'s `<instance>/` directory
(`$DOCKER_CONFIG/certs/<instance>/`, or `~/.docker/certs/<instance>/` when
`DOCKER_CONFIG` is unset) for this operation to inspect for it --
`certs.status_rows`'s own docstring states the identical rule.

Rows are grouped by instance, instances in the alphabetical order
`certs._discover_instances` returns, and both inspectable roles (`client`,
then `ca`) are always present together for a fully-provisioned instance --
`certs._instance_rows` raises rather than printing a row for a client
certificate with no matching CA (`## Failure semantics`'s partial-material
row). The block below is the literal output of `make cert-status` run
against two fully-provisioned instances, `personal` (whose client
certificate is inside the warning window) and `sandbox` (whose material is
newly issued):

```console
$ make cert-status
INSTANCE   ROLE     EXPIRES       DAYS  STATUS
personal   client   2026-09-12      10  RENEW   /devcontainer:certs INSTANCE=personal
personal   ca       2036-08-29    3649  ok
sandbox    client   2026-11-30      89  ok
sandbox    ca       2036-08-29    3649  ok
```

Exit code `0` when every certificate is outside the warning window,
`1` when any has expired, and `0` with a `RENEW` row when one is inside the
window. The window is `CERT_WARN_DAYS` (Section 7.3). No
certificates at all is not an error: it prints a header and a line directing
the operator to `/devcontainer:setup-remote`, since there is nothing yet for
`certs` itself to act on. An unreadable certificate file fails naming the
path rather than being reported as expired -- a read failure and an expired
certificate are different conditions with different remedies, and collapsing
them would send the operator to reissue a certificate that may simply be
unreadable for a permissions reason nothing here should silently reinterpret.
The `RENEW` row's own remedy names the exact `/devcontainer:certs
INSTANCE=<name>` invocation that resolves it, per Section 4.1.2's own worked
example, so an operator reading `make cert-status` output never has to guess
which operation in `## Operations` clears a given row.

## Failure semantics

| Condition | Behavior |
|---|---|
| `openssl` is missing or too old | `certs.py`'s `_require_openssl` runs before any `mkdir` or `os.open` in every operation above, so a missing binary leaves no half-created instance directory or file behind; the reported error names the tool and its install command (`brew install openssl` on macOS, `apt-get install openssl` on Linux/WSL). `_require_openssl` checks only that a binary named `openssl` is on `PATH`, not that it supports the flags `certs.py` uses, so a *present but too old* `openssl` fails opaquely instead: `_issue_certificate`'s signing step passes `x509 -req -copy_extensions copy`, an OpenSSL 3.0+-only flag, and a pre-3.0 `openssl` (including the LibreSSL binary macOS ships at `/usr/bin/openssl`, still on `PATH` on an unconfigured host) rejects it with `unknown option -copy_extensions`, surfaced as an `openssl x509 failed` `CertsError` naming that raw stderr rather than the tool-version cause. `docs/mac-setup-prompt.md` step 2 and `docs/environment-files.md`'s certificate section document the OpenSSL 3.0+ requirement and the `brew`-before-`/usr/bin` `PATH` ordering it depends on; verifying the binary's actual capability rather than merely its presence on `PATH` is left to a later work unit rather than folded into this one. |
| A precondition check fails | A client or server certificate requested for an instance whose CA private key is missing: stop naming the path, and state that creating a new CA invalidates every certificate it signed and requires reissuing and republishing the server certificate. Never create a second CA silently, because the resulting handshake failure later would name neither cause. A certificate found present but expired during Issue or Rotate: fail naming its role, its path and its expiry date, and name the `## Operations` row that reissues it. Never proceed with an expired certificate, and never report one as a warning -- only `## Expiry`'s own `RENEW` row, for a certificate still inside the warning window, is a warning. An instance name that resolves to no directory under `remote-instances/` (Section 4.1.1's own edge case): fail listing every configured instance, the same message every remote make target already gives for the same condition, rather than this skill inventing a second one. |
| An action the skill took did not verify | Every row in `## Operations` parses what it generated before trusting it, and every row that publishes re-reads the parameter afterward rather than trusting the publish call's own exit code; every row that changes context-relevant material additionally requires `## Docker context`'s handshake to succeed. When a parse disagrees (a missing required SAN, a missing `clientAuth`), when a re-read disagrees with what was generated, or when the handshake still fails after `## Docker context`'s rewrite: this skill reports the material generated (and, for a publish, published), the verification that failed, and that the operation is not complete. It never retries the same generation or publish silently a second time; a second attempt is a fresh invocation the operator asks for, the same rule `/devcontainer:teardown`'s own "never retry silently" states for its own actions. |
| A step needs the operator (SSO, `sudo`, a key) | Publishing to `/devcontainer/<instance>/tls/*` (`## Operations`) needs a valid AWS credential for the answered profile. On an unresolved credential or an expired SSO session, this skill states `aws sso login --profile <profile>` with the profile substituted, waits for the operator to complete the browser login, then re-probes with `hostprobe.probe_aws_identity(runner, profile=<profile>)` before continuing -- the identical remedy and re-probe `/devcontainer:setup-remote`'s own `## Checks` table states for the same condition, reused rather than a second implementation. Never assumes the login succeeded, and never falls back to any other credential source (Section 5.4: one backend). |
| A gate is reached (Section 4.4) | This skill reaches no Section 4.4 gate: every operation in `## Operations` generates or reads certificate material and publishes a Parameter Store entry, never a `terragrunt apply` or `terragrunt destroy`, and creates or destroys no AWS resource of the kind `GATE-APPLY`, `GATE-CUTOVER` or `GATE-DESTROY` governs. |
| An answer fails validation | The one answer this skill's own roster row asks for, which instance, is covered by the precondition row above when it names no configured directory. A request this skill has no operation for -- naming `revoke` being the one Section 3.6.3 anticipates -- is answered by `## Revocation` stating why the request cannot be honored, rather than this skill guessing which of `## Operations`'s five real operations was meant. |
| The operator aborts | Nothing in `## Operations` writes a file or publishes a parameter until generation has completed and the value to write is already in hand; an abort before that point leaves every existing file and every existing parameter exactly as they were. An abort between a publish and `## Docker context`'s own handshake leaves the new material published but the context not yet confirmed against it -- reported exactly as the "did not verify" row above states, never as success, and resolved by re-invoking this skill rather than this skill retrying on its own. |

## Related specifications

- Section 2, G2, `spec/devcontainer-platform.md`: the worked transcript
  showing the CA, server and client issuance steps this skill's own
  `## Operations` table separates into standing, individually invocable
  operations.
- Section 3.6.2 and 3.6.3: the boundary mTLS defends, and the IAM-based
  revocation mechanism `## Revocation` states.
- Section 4.1.1: the `INSTANCE=<name>` resolution order this skill's own
  instance selection reuses rather than reimplementing.
- Section 4.1.2: `make cert-status`'s output shape, warning window and exit
  codes, reproduced verbatim in `## Expiry`.
- Section 4.2 and 4.2.2: `certs`'s own roster row, quoted in this document's
  introduction, and the failure-semantics table `## Failure semantics`
  instantiates.
- Section 4.5: `certs` (and `instances`, for the addressing this skill's own
  docker context and Parameter Store paths follow) are the only places a
  fact about certificate generation, inspection or expiry arithmetic is
  decided; this skill decides none of them itself.
- Section 5.3: the Parameter Store paths `## Operations` publishes to and
  reads back from, and their `String`/`SecureString` types.
- Section 5.5: the paths, modes and lifetimes `## Material` reproduces, and
  the SAN reasoning `## Requirements` states.
- Section 7.3: `CERT_CA_DAYS`, `CERT_SERVER_DAYS`, `CERT_CLIENT_DAYS`,
  `CERT_WARN_DAYS` and `DOCKER_HANDSHAKE_TIMEOUT`, never written as a literal
  day count outside the Section 4.1.2 transcript reproduced in `## Expiry`.
- Section 9: the docker context, Parameter prefix and certificate-path
  addressing `## Docker context` and `## Material` follow.
- AC-5.2 (Section 5.5): both required server SANs and `clientAuth` on the
  client certificate, verified by parsing -- `## Requirements`'s own rule.
- AC-10.9 (Section 10.3): a client certificate is rotated with the instance
  left running -- `## Operations`'s "Rotate client certificate" row.
