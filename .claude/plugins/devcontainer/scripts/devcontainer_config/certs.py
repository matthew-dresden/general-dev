"""Certificate authority, server and client issuance (spec Section 4.5: "certs").

Section 4.5 splits `certs` into generation, inspection and expiry arithmetic.
This module implements both halves: generation, and the inspection and expiry
arithmetic behind `make cert-status`. Section 13 decisions D3 and D4
fix the design: there is no CA server, no step-ca, no Vault and no AWS
Private CA -- the authority is nothing more than a key pair and the `openssl`
binary, driven by `.agents/skills/gd-cert-lifecycle/SKILL.md`. Every
`openssl` invocation below passes an argument list, never a shell string
(spec Section 3.4's dependency rule, AC-FUNC-006), and this module imports
nothing beyond the standard library and this project's own
`devcontainer_config.instances` and `devcontainer_config.repo` --
no third-party package is added.
`instances` supplies `DEFAULT_CERTS_ROOT` (`instances.certs_root`, spec
Section 9's own addressing-table derivation of this identical value,
E8-F1-S1-T1 round 2), `instances.PARAMETER_ROOT`, and the instance-name
rule `_validate_instance` delegates to, rather than this module declaring
second, independently drifting copies of any of them; `repo` supplies the
repository root `repo.find_root` resolves, the one value the kind-selected
publication path below needs `instances.read_kind` against.

`instance` reaches this module unvalidated from `--instance` on the CLI and,
per `.agents/skills/gd-cert-lifecycle/SKILL.md`'s own instance
resolution, from `INSTANCE`/`DEFAULT_REMOTE_INSTANCE`; every path this module
composes from it -- `CertPaths.instance_dir`, the Parameter Store path
`publication_set` builds and the Key Vault secret name
`azure_publication_set` builds -- validates it first, through
`_validate_instance`,
which delegates to `devcontainer_config.instances.validate_name`: the single
owner spec Section 4.5 assigns the naming rule, so a name containing a path
separator, the empty string, or a name longer than
`instances.MAX_INSTANCE_NAME_LENGTH` is rejected before it ever reaches a
filesystem path or a store path (code_review, this unit, round 1:
BLOCKING 1; round 2: WARN, a second, length-unbounded validator of the
identical concept). `PARAMETER_ROOT` below is likewise
`instances.PARAMETER_ROOT` itself, not a second literal declaration
(round 1, WARN 4).

Material layout and modes are fixed by spec Section 5.5 and sit outside the
repository entirely, under `<root>/<instance>/`, where `<root>` defaults to
`~/.docker/certs` and is a constructor parameter of `CertPaths` rather than a
literal, so a test can point it at `tmp_path`:

    <root>/<instance>/ca/ca-key.pem   0600   never leaves the laptop
    <root>/<instance>/ca/ca.pem       0644   public
    <root>/<instance>/cert.pem        0644   client certificate
    <root>/<instance>/key.pem         0600   never leaves the laptop

`ca-key.pem` never leaves the laptop and is never placed on the instance: the
daemon only ever needs `ca.pem` to verify a client, so shipping the CA
private key would let a compromised instance mint its own client
certificates and drive any daemon that trusts the same authority.
`publication_set` is this module's own enforcement of that boundary: it
returns exactly the three Parameter Store entries spec Section 5.3 names for
TLS material (`tls/server-key.pem` and `tls/server-cert.pem` as
`SecureString`, `tls/ca.pem` as `String`) and raises for anything else,
naming the CA private key explicitly when that is what was asked for. No
function in this module reads, copies, prints, or otherwise returns
`ca-key.pem`'s contents.

The server certificate's subject alternative names are the one detail that
fails opaquely if it is wrong: they must be exactly `IP:127.0.0.1` and
`DNS:localhost`, because the client reaches the daemon through a loopback
port forward and TLS validates the name the *client* used, never the
identity of the machine that answered. The instance hostname and its private
address both produce a handshake failure that names neither the SANs nor the
forward. The client certificate carries `extendedKeyUsage = clientAuth` and
the server `serverAuth`, because a daemon started with `--tlsverify` refuses
a peer certificate whose extended key usage does not permit the role it is
playing. Both SANs and both extended key usages are requested as CSR
extensions with `-addext` and copied into the signed certificate with
`-copy_extensions copy`, never through a temporary `openssl.cnf` (AC-FUNC-006
requires no configuration file survive any path, including a failure path,
and `-addext` needs none).

Every private key this module creates -- the CA key, the client key, and the
server key even though it is never persisted -- is created through `os.open`
with the target mode passed explicitly and re-applied with `os.chmod`
afterward, so the mode is never left to the process umask and no window
exists where the key is briefly world-readable.

Key material is ECDSA on the P-256 curve (`EC_CURVE` below), signed with
SHA-256: a modern floor that meets the "strong cryptography" bar this
project's standards require, and is well inside the `openssl` versions this
module's Definition of Ready already needs for CSR-extension support via
`-addext`.

Lifetimes come from spec Section 7.3 and are defined once, in this module:
`CERT_CA_DAYS` (default 3650), `CERT_SERVER_DAYS` (default 365) and
`CERT_CLIENT_DAYS` (default 90). No call site below hardcodes a lifetime,
each resolves its own environment variable through `_resolve_lifetime_days`.
Revocation is out of scope by design, not by omission: Docker supports
neither CRL nor OCSP, so removing `ssm:StartSession` is the revocation
mechanism (spec Section 3.6.3), and nothing here pretends otherwise.

E6-F1-S1-T2 adds this module's other half: inspection and expiry arithmetic
(spec Section 4.5), behind `make cert-status` (spec Section 4.1.2). `not_after`
parses a certificate's `notAfter` timestamp with `openssl x509 -enddate`
rather than a second, hand-rolled ASN.1 parser (spec Section 3.4/Section 6's
stdlib-only rule, extended the same way `_require_parseable_ca_component`
already leans on `openssl`'s own tooling instead of a parser this module
would have to maintain). `days_remaining` and `classify` both take the
reference time as an explicit parameter rather than calling
`datetime.now()` internally, so the warning-window boundary is exact and
testable without freezing the system clock (AC-FUNC-001) -- the identical
dependency-injection shape `_resolve_lifetime_days` already gives every
lifetime above, applied here to the clock instead of the environment.
`CERT_WARN_DAYS` (default 14, spec Section 7.3) is read from the
environment in exactly one place, `_resolve_cert_warn_days`, and threaded
from there into every `classify` call `status_rows` makes; no call site
anywhere in this module hardcodes `14` a second time (AC-FUNC-007).

`status_rows` reports the `ca` and `client` roles for every instance
directory it finds under a certificate material root: exactly the two
roles `CertPaths` exposes a persisted path for. The server certificate is
deliberately never one of them: `issue_server`'s own docstring above states
that neither the server key nor the server certificate is ever written
under `CertPaths.instance_dir`, and `certs/SKILL.md`'s `## Material` states
the identical rule in prose -- there is no local file for this function to
inspect. Spec Section 4.1.2's own worked example shows a `server` row
because it illustrates the fully-featured report a persisted server
artifact would produce; until a future task gives the server certificate a
locally inspectable copy, `make cert-status` reports only the two roles
this module actually persists, rather than inventing a placeholder value
for a role it cannot read. A missing role alongside a present one for the
same instance (a client certificate with no CA, or the reverse) is refused
by name instead of being rendered as a blank row (Error Handling Contract),
because that partial state means the instance's material is not what
`create_ca`/`issue_client` would have left behind on their own.

E6-F1-S2-T1 adds `rotate_client`, the standing name spec Section 4.5 and
`certs/SKILL.md`'s "Rotate client certificate" row give the operation of
replacing an already-issued client certificate while the instance keeps
running (AC-10.9). It calls `issue_client` and nothing else: `issue_client`
already overwrites an existing `cert.pem`/`key.pem` pair rather than
refusing one, already refuses when `paths.instance` has no authority yet
rather than creating one (`_require_ca`, never called with an implicit
`create_ca` fallback -- a fresh CA would silently invalidate every
certificate already signed by the old one, including the server certificate
the running daemon is serving), and, since round 3's WARN C fix, already
commits its replacement key and certificate through `_commit_pair` rather
than two independent `os.replace` calls. `_commit_pair` is this task's own
addition: the pre-existing implementation moved the new key into place and
then the new certificate into place as two separate, unguarded operations,
so a failure between the two committed a new key with no matching
certificate (or the reverse) -- a mismatched pair neither the old nor the
new material, which is worse than leaving the previous pair in place
untouched. `_commit_pair` backs up whichever of the destination files
already exist to a sibling path in the same directory before installing
either replacement, and restores those backups on any `OSError` from the
commit itself, so a rotation that raises `OSError` during the commit always
leaves either the complete previous pair or the complete new one, never a
mix of the two (AC-FUNC-004); a process terminated between the backup and
the install, which no `except` clause runs after, is outside that guarantee
and leaves an outage for the caller to detect and repair by hand.
Nothing about a rotation is ever published: `rotate_client` returns `None`
and calls no function in this module that touches Parameter Store, so no
caller of it can infer that a parameter write follows (AC-FUNC-003) --
`publication_set` has no entry for the client certificate at all (spec
Section 5.3), the same fact `certs/SKILL.md`'s own "Rotate client
certificate" row states in prose.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

from devcontainer_config import instances, repo

# The three external commands this module ever shells out to. Every openssl
# argv built here starts with `OPENSSL_EXECUTABLE`; every Parameter Store
# argv the client below builds starts with `AWS_EXECUTABLE`; every Key Vault
# argv the azure client builds starts with `AZ_EXECUTABLE`; every Secret
# Manager argv the gcp client builds starts with `GCLOUD_EXECUTABLE`;
# nothing else in this module names an executable (AC-FUNC-006).
OPENSSL_EXECUTABLE = "openssl"
AWS_EXECUTABLE = "aws"
AZ_EXECUTABLE = "az"
GCLOUD_EXECUTABLE = "gcloud"

# spec Section 5.5's material root. A parameter of `CertPaths`, not a literal
# baked into any function body, so the test suite can point an entire run at
# `tmp_path` instead of the developer's real `~/.docker/certs`. Sourced from
# `devcontainer_config.instances.certs_root`, the single spec Section 9
# addressing-table derivation of this same value (E8-F1-S1-T1 round 2): an
# operator who sets `DOCKER_CONFIG` must never have certificates written
# under one directory while `instances.certs_dir` addresses another.
DEFAULT_CERTS_ROOT = instances.certs_root()

CA_SUBDIR = "ca"
CA_KEY_FILENAME = "ca-key.pem"
CA_CERT_FILENAME = "ca.pem"
SERVER_KEY_FILENAME = "server-key.pem"
SERVER_CERT_FILENAME = "server-cert.pem"
CLIENT_CERT_FILENAME = "cert.pem"
CLIENT_KEY_FILENAME = "key.pem"

# `_commit_pair`'s own bookkeeping suffix for the sibling path it moves a
# pre-existing `key_out`/`cert_out` aside to before installing a
# replacement, so a failed commit can restore exactly what was there before
# it ran. Never left behind: `_commit_pair` unlinks both on every path,
# success or failure (AC-FUNC-004).
_PAIR_BACKUP_SUFFIX = ".rotate-previous"

# spec Section 5.5's modes, applied through `os.chmod` after every write so
# the process umask can never weaken them.
INSTANCE_DIR_MODE = 0o700
PRIVATE_KEY_MODE = 0o600
PUBLIC_CERT_MODE = 0o644

# spec Section 7.3: the three lifetime variables this module owns, and their
# documented defaults (also recorded in docs/environment-files.md).
CERT_CA_DAYS_ENV_VAR = "CERT_CA_DAYS"
CERT_SERVER_DAYS_ENV_VAR = "CERT_SERVER_DAYS"
CERT_CLIENT_DAYS_ENV_VAR = "CERT_CLIENT_DAYS"

CERT_CA_DAYS_DEFAULT = 3650
CERT_SERVER_DAYS_DEFAULT = 365
CERT_CLIENT_DAYS_DEFAULT = 90

# The key algorithm/digest floor (module docstring). Declared once so every
# `-newkey`/`-pkeyopt` call site names the same curve rather than a literal
# that could drift between `create_ca` and `_issue_certificate`.
EC_CURVE = "prime256v1"
DIGEST = "sha256"

# spec Section 5.5's SAN and extended-key-usage requirements, verified by
# parsing a generated certificate in tests/test_certs.py (AC-5.2).
SERVER_SAN = "IP:127.0.0.1,DNS:localhost"
SERVER_EXTENDED_KEY_USAGE = "serverAuth"
CLIENT_EXTENDED_KEY_USAGE = "clientAuth"

# spec Section 7.3: the expiry-warning window `make cert-status` (spec
# Section 4.1.2) applies, resolved in exactly one place, `_resolve_cert_warn_days`
# (AC-FUNC-007); the literal `14` below is the only place this module ever
# writes that number; every other reference to the window is this constant
# or a value `_resolve_cert_warn_days` returned.
CERT_WARN_DAYS_ENV_VAR = "CERT_WARN_DAYS"
CERT_WARN_DAYS_DEFAULT = 14

# The three `classify` outcomes spec Section 4.1.2 names, and the one
# invocation template a `RENEW` row carries (AC-FUNC-005). Roles are the two
# certificate kinds this module ever persists locally (module docstring),
# ordered the way spec Section 4.1.2's own worked example orders them:
# client before ca. `RENEW_INVOCATION_TEMPLATE` is public (not
# underscore-prefixed) because `devcontainer_config.transport` (E6-F2-S1-T2)
# names the identical reissue invocation in its own certificate-not-ready
# and SAN-mismatch remedies and must format the same template this module
# defines, rather than declaring a second, independently drifting copy of
# the same skill invocation string.
STATUS_OK = "ok"
STATUS_RENEW = "RENEW"
STATUS_EXPIRED = "expired"
RENEW_INVOCATION_TEMPLATE = "gd-cert-lifecycle INSTANCE={instance}"
ROLE_CLIENT = "client"
ROLE_CA = "ca"
_INSPECTABLE_ROLES: tuple[str, ...] = (ROLE_CLIENT, ROLE_CA)

# spec Section 4.1.2's own column header, reproduced verbatim, and the line
# `make cert-status` prints in place of any rows when no instance has any
# certificate material yet (AC-FUNC-006's empty-inventory edge case).
_REPORT_HEADER = "INSTANCE   ROLE     EXPIRES       DAYS  STATUS"
_NO_CERTIFICATES_LINE = (
    "No certificates found. Run gd-env-setup-remote to issue the first instance's material."
)

# spec Section 5.3's Parameter Store layout for TLS material.
# `PARAMETER_ROOT` is `instances.PARAMETER_ROOT` itself, not a second
# literal declaration of the `/devcontainer` root; `instances` owns the
# addressing table (spec Section 9), so the root has a single definition
# site across both modules. `SECURE_STRING_TYPE` and `STRING_TYPE` are
# declared here: publication is the only Parameter Store write this
# module's client performs, and the deleted secret-catalog client that
# previously owned the `SecureString` spelling no longer shares it.
PARAMETER_ROOT = instances.PARAMETER_ROOT
TLS_SEGMENT = "tls"
SECURE_STRING_TYPE = "SecureString"
STRING_TYPE = "String"

# The only three names `publication_set` ever hands back (spec Section 5.3).
# `ca-key.pem` is deliberately absent: it is rejected by name in
# `_is_ca_private_key_request` before this mapping is even consulted, so its
# absence here is a second, independent enforcement of the same rule.
_PUBLISHABLE: dict[str, str] = {
    SERVER_KEY_FILENAME: SECURE_STRING_TYPE,
    SERVER_CERT_FILENAME: SECURE_STRING_TYPE,
    CA_CERT_FILENAME: STRING_TYPE,
}

# The azure engine kind's publication set: the same three filenames, mapped
# to Key Vault secret names -- which the vault permits only as
# alphanumerics and dashes, hence the dash-joined forms. The CA private key
# is absent from this mapping for the same reason it is absent from
# `_PUBLISHABLE`, and `azure_publication_set` refuses it by name through the
# same `_is_ca_private_key_request` rule.
AZURE_SERVER_KEY_SECRET = "tls-server-key"
AZURE_SERVER_CERT_SECRET = "tls-server-cert"
AZURE_CA_SECRET = "tls-ca"

AZURE_PUBLISHABLE: dict[str, str] = {
    SERVER_KEY_FILENAME: AZURE_SERVER_KEY_SECRET,
    SERVER_CERT_FILENAME: AZURE_SERVER_CERT_SECRET,
    CA_CERT_FILENAME: AZURE_CA_SECRET,
}

# The marker `certs publication-set` prints after each Key Vault secret name
# when the instance's engine kind is azure, the same role the Parameter
# Store type column plays for the ec2 set.
AZURE_KEYVAULT_MARK = "azure-key-vault"

# The gcp engine kind's publication set: the same three filenames, mapped to
# Secret Manager secret-id leaves -- Secret Manager permits `[A-Za-z0-9_-]`,
# so the dash-joined forms the azure set uses carry over unchanged. The CA
# private key is absent from this mapping for the same reason it is absent
# from `_PUBLISHABLE`, and `gcp_publication_set` refuses it by name through
# the same `_is_ca_private_key_request` rule.
GCP_SERVER_KEY_SECRET = "tls-server-key"
GCP_SERVER_CERT_SECRET = "tls-server-cert"
GCP_CA_SECRET = "tls-ca"
GCP_CHECKSUM_SECRET = "tls-checksum"

GCP_PUBLISHABLE: dict[str, str] = {
    SERVER_KEY_FILENAME: GCP_SERVER_KEY_SECRET,
    SERVER_CERT_FILENAME: GCP_SERVER_CERT_SECRET,
    CA_CERT_FILENAME: GCP_CA_SECRET,
}

# The two configuration-payload secret-id leaves `push-secrets.sh` publishes
# beside the TLS material, declared here so the status probe, the cleanup
# arm and the shell script all compose the same ids from one set of leaves
# rather than each restating its own literals. They are not part of
# `GCP_PUBLISHABLE`: publishing them is the shell publish's job, and the
# certificate publication set never grows a non-certificate entry.
GCP_SHELL_ENV_SECRET = "shell-env"
GCP_PROFILE_MAP_SECRET = "profile-map"

# The marker `certs publication-set` prints after each Secret Manager secret
# id when the instance's engine kind is gcp, the same role
# AZURE_KEYVAULT_MARK plays for the azure set.
GCP_SECRET_MANAGER_MARK = "gcp-secret-manager"

# Every spelling of "the CA private key" `publication_set` must recognize and
# refuse by name rather than falling through to the generic "not
# publishable" message: the bare filename, and the filename qualified by its
# own subdirectory the way `CertPaths.ca_key` would render it relative to an
# instance root.
_CA_PRIVATE_KEY_NAMES: frozenset[str] = frozenset(
    {CA_KEY_FILENAME, f"{CA_SUBDIR}/{CA_KEY_FILENAME}"}
)


class CertsError(RuntimeError):
    """Raised when certificate generation or publication cannot proceed as requested.

    Every raise site in this module names the offending path, tool, or
    parameter name and states the remedy, per this work unit's Error
    Handling Contract; nothing here ever falls back to a default or retries
    silently.
    """


def _validate_instance(instance: str) -> None:
    """Reject a path-unsafe or over-length instance name before it reaches any path composition.

    Delegates to `instances.validate_name` (`devcontainer_config.instances`),
    the single owner spec Section 4.5 assigns the naming rule, rather than a
    second, independent implementation of the same rule (code_review, this
    unit, round 1: BLOCKING 1). Delegating there applies the identical
    character-class rule and `instances.MAX_INSTANCE_NAME_LENGTH`
    in one call, so a name accepted here is accepted by every other
    derivation that composes a path from it. `instance` reaches this
    module unvalidated from
    `--instance` on the CLI and, per
    `.agents/skills/gd-cert-lifecycle/SKILL.md`'s own instance
    resolution, from `INSTANCE`/`DEFAULT_REMOTE_INSTANCE`, so leaving it
    unvalidated would let a name carrying a path separator (or the empty
    string) compose a filesystem path outside `CertPaths.root`
    (`CertPaths.instance_dir`) or a Parameter Store path outside
    `PARAMETER_ROOT` (`publication_set`).

    Raises:
        CertsError: `instance` is empty, contains a path separator or
            another character `instances.validate_name` rejects, or exceeds
            `instances.MAX_INSTANCE_NAME_LENGTH` characters.
    """
    try:
        instances.validate_name(instance)
    except instances.InvalidInstanceNameError as exc:
        raise CertsError(
            f"ERROR: invalid instance name {instance!r}\n"
            "An instance name must be a non-empty path segment of at most "
            f"{instances.MAX_INSTANCE_NAME_LENGTH} characters: letters, "
            "digits, hyphens and underscores only; the identical rule "
            "devcontainer_config.instances enforces for the same "
            "/devcontainer/<instance>/ prefix.\n"
            "Use a valid instance name."
        ) from exc


@dataclass(frozen=True)
class CertPaths:
    """Every on-disk path spec Section 5.5 fixes for one instance's material.

    `root` defaults to `DEFAULT_CERTS_ROOT` but is a plain field, not a
    global read at call time, so a test can construct as many independent
    instances under as many `tmp_path` roots as it needs without any shared
    mutable state. `instance` is validated in `__post_init__`, once, so
    every property below and every function that receives a `CertPaths`
    composes a path from an already-validated instance name rather than
    each caller having to remember to validate it first (code_review, this
    unit, round 1: BLOCKING 1/2).
    """

    instance: str
    root: Path = DEFAULT_CERTS_ROOT

    def __post_init__(self) -> None:
        _validate_instance(self.instance)

    @property
    def instance_dir(self) -> Path:
        return self.root / self.instance

    @property
    def ca_dir(self) -> Path:
        return self.instance_dir / CA_SUBDIR

    @property
    def ca_key(self) -> Path:
        return self.ca_dir / CA_KEY_FILENAME

    @property
    def ca_cert(self) -> Path:
        return self.ca_dir / CA_CERT_FILENAME

    @property
    def client_cert(self) -> Path:
        return self.instance_dir / CLIENT_CERT_FILENAME

    @property
    def client_key(self) -> Path:
        return self.instance_dir / CLIENT_KEY_FILENAME


@dataclass(frozen=True)
class ServerCertificate:
    """The server key and certificate `issue_server` generates.

    Neither field is ever written under `CertPaths.instance_dir`: spec
    Section 5.5's own material list has no server-key/server-cert row.
    `issue_server` generates both inside a `tempfile.TemporaryDirectory` at
    mode `0600`/`0644` (matching `.agents/skills/gd-cert-lifecycle/
    SKILL.md`'s "Issue server certificate" row) and removes that directory
    before returning, so the private key exists on disk only briefly, inside
    that removed temporary directory -- it is never persisted under
    `CertPaths.instance_dir`. Returning it as PEM text is what lets a future
    publishing step (out of this task's scope) hand it to Parameter Store
    without an intermediate file.
    """

    key_pem: str
    cert_pem: str


@dataclass(frozen=True)
class PublicationEntry:
    """One Parameter Store destination `publication_set` allows publishing to."""

    parameter_path: str
    parameter_type: str


# ---------------------------------------------------------------------------
# The Parameter Store publication client. This section is the write path
# TLS material takes to the store: `publish` is its only consumer inside
# this module, and nothing else in the package writes parameters, so the
# client lives here rather than in a module of its own. Absorbed from the
# deleted secret-catalog client, which owned the identical argv invariant
# (a value travels in a `--cli-input-json` document, never an argument);
# the secret-shaped operations (resolve, list, the two-tier scope rule)
# went with that module; only the path-shaped write and read back
# survived, because publishing a certificate is not a secret operation.
# ---------------------------------------------------------------------------

# The two `aws ssm` subcommands this client ever issues. Declared once so
# each is named identically in the argv it builds and in the operation
# name an error message reports for it.
GET_PARAMETER_OP = "get-parameter"
PUT_PARAMETER_OP = "put-parameter"

# `--cli-input-json` accepts a `file://` URI; pointing it at the process's
# own stdin is what lets a write hand the value to `aws` without it ever
# appearing in argv. stdin is not an option: the aws CLI v2 does not read
# `file:///dev/stdin` (it reports "Invalid JSON received" whether stdin is
# a pipe or a redirected regular file, and blocks indefinitely on a FIFO),
# so the document must be a real, regular file for the duration of the
# call, created at mode 0600 inside a 0700 directory that is removed
# before returning.
PUT_DOCUMENT_FILENAME = "put-parameter.json"

AWS_PROFILE_ENV_VAR = "AWS_PROFILE"
DEFAULT_AWS_PROFILE = "default"

# Substrings the AWS CLI's stderr carries for the error conditions this
# client distinguishes from an unclassified failure. Matched literally,
# not parsed as JSON, because the CLI's own error rendering is plain text.
# Only these specific conditions are ever diagnosed as "no credential
# resolved"; every other non-zero exit is unclassified (see
# `_raise_for_failure`), because asserting a credential cause for a failure
# that is not one hands the operator a remediation that cannot work.
PARAMETER_NOT_FOUND_MARKER = "ParameterNotFound"
ACCESS_DENIED_MARKER = "AccessDeniedException"
SSO_SESSION_MARKER = "SSO Token"
NO_CREDENTIALS_MARKER = "Unable to locate credentials"


class ParameterStoreError(RuntimeError):
    """Base class for every failure the Parameter Store client raises.

    Every raise site names the offending parameter path and states the
    remedy, so an operator reading the message knows what to do next
    without consulting this module.
    """


class ParameterStoreUnavailableError(ParameterStoreError):
    """The `aws` binary is missing, or no AWS credential resolved for it."""


class ParameterStoreUnauthorizedError(ParameterStoreError):
    """The caller's identity is not authorized for the parameter prefix."""


class ParameterNotFoundError(ParameterStoreError):
    """No parameter exists at the path a read asked for."""


class ParameterStoreUnclassifiedError(ParameterStoreError):
    """The store rejected the operation for a reason this client does not classify."""


# The Runner a ParameterStoreClient is constructed with: given the full
# argv and an optional stdin document, return a completed process.
# Injected rather than called internally via `subprocess.run` directly, so
# every test substitutes a fake runner instead of patching this module.
Runner = Callable[[Sequence[str], "str | None"], subprocess.CompletedProcess[str]]


def subprocess_runner(argv: Sequence[str], stdin: str | None) -> subprocess.CompletedProcess[str]:
    """The production Runner: a real subprocess, fed `stdin` on its stdin.

    This is what a caller outside the test suite constructs a
    `ParameterStoreClient` with. `ParameterStoreClient` itself never calls
    `subprocess.run`, so nothing here needs patching to be tested
    hermetically.
    """
    return subprocess.run(list(argv), input=stdin, capture_output=True, text=True, check=False)


def _unavailable_no_binary_message(path: str) -> str:
    return (
        f"ERROR: cannot reach Parameter Store for {path}\n"
        "The aws CLI is not on PATH.\n"
        "Install the AWS CLI v2 so this command can run 'aws ssm', then retry."
    )


def _unavailable_no_credential_message(path: str) -> str:
    profile = os.environ.get(AWS_PROFILE_ENV_VAR, DEFAULT_AWS_PROFILE)
    return (
        f"ERROR: cannot reach Parameter Store for {path}\n"
        f"No AWS credential resolved for profile '{profile}'.\n"
        f"Run 'aws sso login --profile {profile}' to refresh the session, then retry."
    )


def _unauthorized_message(path: str) -> str:
    prefix = path.rsplit("/", 1)[0]
    return (
        f"ERROR: access denied for {path}\n"
        f"The caller's identity is not authorized for the parameter prefix {prefix}.\n"
        "Ask an operator to grant the missing ssm:* permission on this prefix, "
        "then retry."
    )


def _not_found_message(path: str) -> str:
    prefix = path.rsplit("/", 1)[0]
    return (
        f"ERROR: no parameter at {path}\n"
        "The store reported ParameterNotFound.\n"
        f"List what exists under this prefix: aws ssm describe-parameters "
        f"--parameter-filters Key=Path,Option=Recursive,Values={prefix}"
    )


def _malformed_response_message(path: str, operation: str, reason: str) -> str:
    """A top-level `aws ssm` response is missing a field this client needs to read.

    Would otherwise escape as a bare `KeyError` or `json.JSONDecodeError`
    instead of a named `ParameterStoreError`.
    """
    return (
        f"ERROR: cannot complete '{operation}' for {path}\n"
        f"The store's response is malformed: {reason}.\n"
        "Retry the operation; if this persists, the aws CLI version may be "
        "incompatible with this client."
    )


def _parse_response_json(stdout: str, path: str, operation: str) -> dict[str, object]:
    """Parse `stdout` as the JSON object `aws ssm <operation>` returns for `path`.

    The one place the client turns a response body into data, so a
    response that is not valid JSON, or that parses to something other
    than a JSON object, raises a named `ParameterStoreError` here instead
    of a bare `json.JSONDecodeError` escaping from each call site
    individually.

    Raises:
        ParameterStoreError: `stdout` is not valid JSON, or does not parse
            to a JSON object.
    """
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise ParameterStoreError(
            _malformed_response_message(path, operation, "the response is not valid JSON")
        ) from exc
    if not isinstance(payload, dict):
        raise ParameterStoreError(
            _malformed_response_message(path, operation, "the response is not a JSON object")
        )
    return payload


def _unclassified_failure_next_step(path: str, operation: str) -> str:
    """The remediation clause naming a command that actually runs for `operation`.

    `get-parameter` accepts `--name`, so re-running the same shape the
    client itself issued is a faithful diagnostic. `put-parameter`
    requires `--value`/`--type` to run at all, and a faithful re-submission
    would mean typing the private key on a command line, which is exactly
    what this client exists to avoid, so the hint reads back the
    parameter's current metadata instead of re-submitting the write.
    """
    if operation == PUT_PARAMETER_OP:
        rerun = f"aws ssm get-parameter --name {path} --output json"
        return (
            "The store's diagnostic is not repeated here because it may "
            "contain the submitted document; re-submitting the write would "
            "mean typing the parameter value on a command line, so read back "
            f"the parameter's current metadata instead: '{rerun}', then "
            "retry the write."
        )
    rerun = f"aws ssm {operation} --name {path} --output json"
    return (
        "The store's diagnostic is not repeated here because it may contain "
        f"response detail this client does not echo; re-run '{rerun}' (add "
        "'--debug' for detail) to see it directly, then retry."
    )


def _unclassified_failure_message(path: str, operation: str, returncode: int) -> str:
    return (
        f"ERROR: cannot complete '{operation}' for {path}\n"
        f"The aws CLI exited with status {returncode}; this is not a missing "
        "credential, an access-denied response, or a not-found response, so "
        "this client does not guess a cause.\n"
        f"{_unclassified_failure_next_step(path, operation)}"
    )


def _raise_for_failure(path: str, operation: str, returncode: int, stderr: str) -> NoReturn:
    if PARAMETER_NOT_FOUND_MARKER in stderr:
        raise ParameterNotFoundError(_not_found_message(path))
    if ACCESS_DENIED_MARKER in stderr:
        raise ParameterStoreUnauthorizedError(_unauthorized_message(path))
    if SSO_SESSION_MARKER in stderr or NO_CREDENTIALS_MARKER in stderr:
        raise ParameterStoreUnavailableError(_unavailable_no_credential_message(path))
    raise ParameterStoreUnclassifiedError(
        _unclassified_failure_message(path, operation, returncode)
    )


class ParameterStoreClient:
    """Writes and reads individual parameters in AWS Parameter Store.

    `runner` and `region` are the only inputs: no credential, endpoint or
    region is read from anywhere else in this class, which is what lets a
    test construct one with no network, no AWS and no docker. No region is
    passed by default: the `aws` CLI resolves it the same way any other
    invocation on this host does, from `AWS_DEFAULT_REGION` or the active
    profile, so nothing here hardcodes one.
    """

    def __init__(self, runner: Runner, *, region: str | None = None) -> None:
        self._runner = runner
        self._region = region

    def _argv(self, *operation_args: str) -> list[str]:
        """The shared argv shell every operation builds on.

        Executable, subcommand, output format and region live here, once,
        so the output format and the region flag cannot drift between the
        two operations below.
        """
        argv = [AWS_EXECUTABLE, "ssm", *operation_args, "--output", "json"]
        if self._region is not None:
            argv += ["--region", self._region]
        return argv

    def _invoke(
        self, argv: list[str], stdin: str | None, path: str, *, operation: str
    ) -> subprocess.CompletedProcess[str]:
        """Run `argv` through the injected runner and translate any failure.

        The one place every operation's error handling passes through, so
        the translation from an `aws` exit code or a missing binary to a
        `ParameterStoreError` subclass exists once, not once per
        operation. `operation` is the `aws ssm` subcommand this call
        issues, named in any resulting `ParameterStoreUnclassifiedError`
        message so the operator knows which command to re-run for the
        store's own diagnostic.
        """
        try:
            result = self._runner(argv, stdin)
        except FileNotFoundError as exc:
            raise ParameterStoreUnavailableError(_unavailable_no_binary_message(path)) from exc
        if result.returncode != 0:
            _raise_for_failure(path, operation, result.returncode, result.stderr)
        return result

    def read_parameter(self, path: str) -> str:
        """The value stored at the fully-qualified `path`, byte for byte.

        The read counterpart of `write_parameter`, and symmetric with it:
        `publish` uses it to confirm each published parameter
        independently, rather than trusting the write call's own exit
        code.

        Raises:
            ParameterStoreUnavailableError: the store could not be reached.
            ParameterStoreUnauthorizedError: the caller lacks access to this prefix.
            ParameterNotFoundError: no parameter exists at this path.
            ParameterStoreError: the response has no `Parameter.Value`
                field, or is not valid JSON.
            ParameterStoreUnclassifiedError: the store rejected the
                operation for a reason this client does not classify.
        """
        argv = self._argv(GET_PARAMETER_OP, "--name", path, "--with-decryption")
        result = self._invoke(argv, None, path, operation=GET_PARAMETER_OP)
        payload = _parse_response_json(result.stdout, path, GET_PARAMETER_OP)
        parameter = payload.get("Parameter")
        if not isinstance(parameter, dict) or "Value" not in parameter:
            raise ParameterStoreError(
                _malformed_response_message(
                    path, GET_PARAMETER_OP, "no 'Parameter.Value' field in the response"
                )
            )
        return str(parameter["Value"])

    def write_parameter(
        self,
        path: str,
        value: str,
        parameter_type: str,
        *,
        description: str | None = None,
    ) -> int:
        """Store `value` at the fully-qualified `path`, overwriting any existing version.

        The one place in this repository a parameter is written. The argv
        invariant is structural rather than a discipline a caller has to
        remember: `value` travels inside a `--cli-input-json` document
        written to a private file this method creates and removes, and
        never appears in argv, so no sensitive material -- a TLS private
        key included -- can reach the process table.

        Returns:
            The integer `Version` the store assigned to the parameter.

        Raises:
            ParameterStoreUnavailableError: the store could not be reached.
            ParameterStoreUnauthorizedError: the caller lacks access to this prefix.
            ParameterStoreUnclassifiedError: the store rejected the
                operation for a reason this client does not classify.
            ParameterStoreError: the response has no integer `Version`
                field, or is not valid JSON.
        """
        document: dict[str, object] = {
            "Name": path,
            "Value": value,
            "Type": parameter_type,
            "Overwrite": True,
        }
        if description is not None:
            document["Description"] = description
        with tempfile.TemporaryDirectory() as directory:
            document_path = Path(directory) / PUT_DOCUMENT_FILENAME
            descriptor = os.open(document_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(document))
            argv = self._argv(PUT_PARAMETER_OP, "--cli-input-json", f"file://{document_path}")
            result = self._invoke(argv, None, path, operation=PUT_PARAMETER_OP)
        payload = _parse_response_json(result.stdout, path, PUT_PARAMETER_OP)
        version = payload.get("Version")
        if not isinstance(version, int) or isinstance(version, bool):
            raise ParameterStoreError(
                _malformed_response_message(
                    path, PUT_PARAMETER_OP, "no integer 'Version' field in the response"
                )
            )
        return version


# ---------------------------------------------------------------------------
# The Key Vault publication client. This section is the azure engine kind's
# twin of the Parameter Store client above, and it mirrors that client's
# discipline exactly: the value travels in a 0600 `--file` inside a 0700
# directory removed before returning, never an argument; every write is read
# back and byte-compared before the caller may report it published; the
# same injectable Runner seam substitutes a fake in tests; and the failure
# classes name the remedy. The `az` CLI is ambiently logged in, so the
# client takes a vault name and no region and no profile.
# ---------------------------------------------------------------------------

# The two `az keyvault` subcommands this client ever issues. Declared once so
# each is named identically in the argv it builds and in the operation name
# an error message reports for it.
KEYVAULT_SET_OP = "keyvault secret set"
KEYVAULT_SHOW_OP = "keyvault secret show"

# The write's `--file` document, created at mode 0600 inside the temporary
# 0700 directory the write removes before returning -- the same discipline
# `PUT_DOCUMENT_FILENAME` documents for the aws path.
PUT_SECRET_FILENAME = "put-secret.txt"

# Substrings the az CLI's stderr carries for the error conditions this
# client distinguishes from an unclassified failure. Matched literally,
# not parsed as JSON, because the CLI's own error rendering is plain text.
SECRET_NOT_FOUND_MARKER = "SecretNotFound"
FORBIDDEN_MARKER = "AuthorizationFailed"
AZURE_LOGIN_MARKER = "az login"


class KeyVaultError(RuntimeError):
    """Base class for every failure the Key Vault client raises.

    Every raise site names the offending secret name and states the
    remedy, so an operator reading the message knows what to do next
    without consulting this module.
    """


class KeyVaultUnavailableError(KeyVaultError):
    """The `az` binary is missing, or no Entra credential resolved for it."""


class KeyVaultUnauthorizedError(KeyVaultError):
    """The caller's identity is not authorized for the vault."""


class KeyVaultSecretNotFoundError(KeyVaultError):
    """No secret exists under the name a read asked for."""


class KeyVaultUnclassifiedError(KeyVaultError):
    """The vault rejected the operation for a reason this client does not classify."""


def _keyvault_unavailable_no_binary_message(secret_name: str) -> str:
    return (
        f"ERROR: cannot reach Key Vault for {secret_name}\n"
        "The az CLI is not on PATH.\n"
        "Install the Azure CLI ('brew install azure-cli' on macOS), then retry."
    )


def _keyvault_unavailable_no_login_message(secret_name: str) -> str:
    return (
        f"ERROR: cannot reach Key Vault for {secret_name}\n"
        "No Entra credential resolved for the az CLI.\n"
        "Run 'az login' to refresh the credentials, then retry."
    )


def _keyvault_unauthorized_message(secret_name: str, vault_name: str) -> str:
    return (
        f"ERROR: access denied for {secret_name} in vault '{vault_name}'\n"
        "The caller's identity is not authorized for this vault.\n"
        "Ask an operator to grant the missing Key Vault secret permission on this "
        "vault, then retry."
    )


def _keyvault_not_found_message(secret_name: str, vault_name: str) -> str:
    return (
        f"ERROR: no secret named {secret_name} in vault '{vault_name}'\n"
        "The vault reported SecretNotFound.\n"
        f"Publish it first: run the publication that writes '{secret_name}' "
        "and retry."
    )


def _keyvault_unclassified_failure_message(
    secret_name: str, operation: str, returncode: int
) -> str:
    return (
        f"ERROR: cannot complete '{operation}' for {secret_name}\n"
        f"The az CLI exited with status {returncode}; this is not a missing "
        "credential, an access-denied response, or a not-found response, so "
        "this client does not guess a cause.\n"
        "The az CLI's diagnostic is not repeated here because it may contain "
        "response detail this client does not echo; re-run the command by hand "
        "(add '--debug' for detail) to see it directly, then retry."
    )


def _raise_keyvault_for_failure(
    secret_name: str, vault_name: str, operation: str, returncode: int, stderr: str
) -> NoReturn:
    if SECRET_NOT_FOUND_MARKER in stderr:
        raise KeyVaultSecretNotFoundError(_keyvault_not_found_message(secret_name, vault_name))
    if FORBIDDEN_MARKER in stderr:
        raise KeyVaultUnauthorizedError(_keyvault_unauthorized_message(secret_name, vault_name))
    if AZURE_LOGIN_MARKER in stderr:
        raise KeyVaultUnavailableError(_keyvault_unavailable_no_login_message(secret_name))
    raise KeyVaultUnclassifiedError(
        _keyvault_unclassified_failure_message(secret_name, operation, returncode)
    )


def _tsv_value(stdout: str) -> str:
    """`az --query value --output tsv`'s stdout as the stored value, byte for byte.

    tsv keeps its output line-oriented: a tab, newline or backslash inside a
    value is rendered as the escape text ``\\t``/``\\n``/``\\\\`` rather than
    raw, and the CLI appends one trailing newline to the line it prints. This
    reverses exactly that rendering -- a single left-to-right pass, so an
    escaped backslash is never mistaken for the start of a second escape --
    which is what lets `publish_to_key_vault`'s read-back comparison be a
    byte comparison against the write. For a value carrying none of the three
    characters (PEM material carries none) the pass is the identity after the
    trailing-newline trim.
    """
    text = stdout[:-1] if stdout.endswith("\n") else stdout
    out: list[str] = []
    index = 0
    while index < len(text):
        character = text[index]
        if character == "\\" and index + 1 < len(text):
            escaped = text[index + 1]
            if escaped == "n":
                out.append("\n")
                index += 2
                continue
            if escaped == "t":
                out.append("\t")
                index += 2
                continue
            if escaped == "\\":
                out.append("\\")
                index += 2
                continue
        out.append(character)
        index += 1
    return "".join(out)


class KeyVaultClient:
    """Writes and reads individual secrets in the Key Vault `vault_name` names.

    The azure twin of `ParameterStoreClient`, bound to one vault instead of
    addressing paths. `runner` and `vault_name` are the only inputs: no
    credential, endpoint, region or profile is read from anywhere else in
    this class, because the `az` CLI resolves its Entra session the way any
    other invocation on this host does, so nothing here hardcodes one and a
    test constructs the client with no network, no azure and no docker.
    """

    def __init__(self, runner: Runner, *, vault_name: str) -> None:
        self._runner = runner
        self._vault_name = vault_name

    def _set_argv(self, secret_name: str, file_path: Path) -> list[str]:
        """The write argv: the value rides `--file`, never an argument."""
        return [
            AZ_EXECUTABLE,
            "keyvault",
            "secret",
            "set",
            "--vault-name",
            self._vault_name,
            "--name",
            secret_name,
            "--file",
            str(file_path),
        ]

    def _show_argv(self, secret_name: str) -> list[str]:
        """The read argv: `--query value` renders the stored value alone."""
        return [
            AZ_EXECUTABLE,
            "keyvault",
            "secret",
            "show",
            "--vault-name",
            self._vault_name,
            "--name",
            secret_name,
            "--query",
            "value",
            "--output",
            "tsv",
        ]

    def _invoke(
        self, argv: list[str], secret_name: str, *, operation: str
    ) -> subprocess.CompletedProcess[str]:
        """Run `argv` through the injected runner and translate any failure.

        The one place every operation's error handling passes through, the
        same single-seam shape `_invoke` gives the Parameter Store client:
        the translation from an `az` exit code or a missing binary to a
        `KeyVaultError` subclass exists once, not once per operation.
        """
        try:
            result = self._runner(argv, None)
        except FileNotFoundError as exc:
            raise KeyVaultUnavailableError(
                _keyvault_unavailable_no_binary_message(secret_name)
            ) from exc
        if result.returncode != 0:
            _raise_keyvault_for_failure(
                secret_name, self._vault_name, operation, result.returncode, result.stderr
            )
        return result

    def read_secret(self, secret_name: str) -> str:
        """The value stored under `secret_name`, byte for byte.

        The read counterpart of `write_secret`, and symmetric with it:
        `publish_to_key_vault` uses it to confirm each published secret
        independently, rather than trusting the write call's own exit
        code.

        Raises:
            KeyVaultUnavailableError: the vault could not be reached.
            KeyVaultUnauthorizedError: the caller lacks access to this vault.
            KeyVaultSecretNotFoundError: no secret exists under this name.
            KeyVaultError: the response could not be interpreted as one
                stored value.
            KeyVaultUnclassifiedError: the vault rejected the operation
                for a reason this client does not classify.
        """
        result = self._invoke(self._show_argv(secret_name), secret_name, operation=KEYVAULT_SHOW_OP)
        if result.stdout.strip() == "":
            raise KeyVaultError(
                f"ERROR: the read of {secret_name} returned nothing\n"
                f"Vault '{self._vault_name}' answered successfully but printed no "
                "value, which is not a state a published secret can be in.\n"
                "Re-run the publication, and check whether another process is "
                "writing the same vault."
            )
        return _tsv_value(result.stdout)

    def write_secret(self, secret_name: str, value: str) -> None:
        """Store `value` under `secret_name`, overwriting any existing version.

        The one place in this repository a Key Vault secret is written. The
        argv invariant is the Parameter Store client's own, restated for
        azure: `value` travels inside a `--file` document written to a
        private file this method creates and removes, and never appears in
        argv, so no sensitive material -- a TLS private key included -- can
        reach the process table. Key Vault versions every `set`, so the
        overwrite a re-issuance needs is the store's own normal behavior.

        Raises:
            KeyVaultUnavailableError: the vault could not be reached.
            KeyVaultUnauthorizedError: the caller lacks access to this vault.
            KeyVaultUnclassifiedError: the vault rejected the operation
                for a reason this client does not classify.
        """
        with tempfile.TemporaryDirectory() as directory:
            document_path = Path(directory) / PUT_SECRET_FILENAME
            descriptor = os.open(document_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(value)
            argv = self._set_argv(secret_name, document_path)
            self._invoke(argv, secret_name, operation=KEYVAULT_SET_OP)


# ---------------------------------------------------------------------------
# The Secret Manager publication client. This section is the gcp engine
# kind's twin of the two clients above, and it mirrors their discipline
# exactly: the value travels in a `--data-file` pointing at a 0600 file
# inside a 0700 directory removed before returning, never an argument; the
# decision to create versus version-add is made from an explicit probe,
# never from a caught error; every write is read back through
# `versions access latest` and byte-compared before the caller may report
# it published; the same injectable Runner seam substitutes a fake in
# tests; and the failure classes name the remedy. `gcloud` authenticates
# ambiently, so the client takes a project id and no credential.
# ---------------------------------------------------------------------------

# The three `gcloud secrets` subcommands this client ever issues. Declared
# once so each is named identically in the argv it builds and in the
# operation name an error message reports for it.
SECRETS_CREATE_OP = "secrets create"
SECRETS_VERSIONS_ADD_OP = "secrets versions add"
SECRETS_ACCESS_OP = "secrets versions access latest"
SECRETS_DESCRIBE_OP = "secrets describe"

# The write's `--data-file` document, created at mode 0600 inside the
# temporary 0700 directory the write removes before returning -- the same
# discipline `PUT_DOCUMENT_FILENAME` and `PUT_SECRET_FILENAME` document for
# the aws and azure paths.
PUT_SECRET_DATA_FILENAME = "put-secret.bin"

# Substrings the gcloud CLI's stderr carries when the thing a probe names
# does not exist, and when the caller has no ambient identity. Matched
# literally, not parsed, because gcloud's own error rendering is plain
# text. Only the not-found set is ever diagnosed as "the secret is
# missing"; every other non-zero exit is classified by the shared rule
# below (`_raise_for_secret_failure`), because asserting a cause for a
# failure that is not one hands the operator a remediation that cannot
# work.
SECRET_NOT_FOUND_MARKERS: tuple[str, ...] = ("NOT_FOUND", "not found", "404")
GCLOUD_AUTH_MARKER = "gcloud auth login"
GCLOUD_PERMISSION_MARKERS: tuple[str, ...] = ("PERMISSION_DENIED", "403")
GCLOUD_NO_CREDENTIALS_MARKERS: tuple[str, ...] = (
    "Your credentials have expired",
    "could not obtain an access token",
    "(gcloud.auth)",
)


class SecretManagerError(RuntimeError):
    """Base class for every failure the Secret Manager client raises.

    Every raise site names the offending secret id and states the remedy,
    so an operator reading the message knows what to do next without
    consulting this module.
    """


class SecretManagerUnavailableError(SecretManagerError):
    """The `gcloud` binary is missing, or no ambient credential resolved."""


class SecretManagerUnauthorizedError(SecretManagerError):
    """The caller's identity is not authorized for the project's secrets."""


class SecretManagerSecretNotFoundError(SecretManagerError):
    """No secret exists under the id a read asked for."""


class SecretManagerUnclassifiedError(SecretManagerError):
    """The store rejected the operation for a reason this client does not classify."""


def _secret_unavailable_no_binary_message(secret_id: str) -> str:
    return (
        f"ERROR: cannot reach Secret Manager for {secret_id}\n"
        "The gcloud CLI is not on PATH.\n"
        "Install the Google Cloud CLI ('brew install google-cloud-sdk' on macOS), "
        "then retry."
    )


def _secret_unavailable_no_login_message(secret_id: str) -> str:
    return (
        f"ERROR: cannot reach Secret Manager for {secret_id}\n"
        "No ambient gcloud credential resolved.\n"
        "Run 'gcloud auth login' to refresh the credentials, then retry."
    )


def _secret_unauthorized_message(secret_id: str, project_id: str) -> str:
    return (
        f"ERROR: access denied for {secret_id} in project '{project_id}'\n"
        "The caller's identity is not authorized for this project's secrets.\n"
        "Ask an operator to grant the missing Secret Manager permission on this "
        "project, then retry."
    )


def _secret_not_found_message(secret_id: str, project_id: str) -> str:
    return (
        f"ERROR: no secret named {secret_id} in project '{project_id}'\n"
        "The store reported the secret as missing.\n"
        f"Publish it first: run the publication that writes '{secret_id}' and retry."
    )


def _secret_unclassified_failure_message(secret_id: str, operation: str, returncode: int) -> str:
    return (
        f"ERROR: cannot complete '{operation}' for {secret_id}\n"
        f"The gcloud CLI exited with status {returncode}; this is not a missing "
        "credential, an access-denied response, or a not-found response, so this "
        "client does not guess a cause.\n"
        "The gcloud CLI's diagnostic is not repeated here because it may contain "
        "response detail this client does not echo; re-run the command by hand "
        "(add '--log-http' for detail) to see it directly, then retry."
    )


def _raise_for_secret_failure(
    secret_id: str, project_id: str, operation: str, returncode: int, stderr: str
) -> NoReturn:
    if any(marker in stderr for marker in SECRET_NOT_FOUND_MARKERS):
        raise SecretManagerSecretNotFoundError(_secret_not_found_message(secret_id, project_id))
    if any(marker in stderr for marker in GCLOUD_PERMISSION_MARKERS):
        raise SecretManagerUnauthorizedError(_secret_unauthorized_message(secret_id, project_id))
    if GCLOUD_AUTH_MARKER in stderr or any(
        marker in stderr for marker in GCLOUD_NO_CREDENTIALS_MARKERS
    ):
        raise SecretManagerUnavailableError(_secret_unavailable_no_login_message(secret_id))
    raise SecretManagerUnclassifiedError(
        _secret_unclassified_failure_message(secret_id, operation, returncode)
    )


class SecretManagerClient:
    """Writes and reads individual secrets in the project `project_id` names.

    The gcp twin of `ParameterStoreClient` and `KeyVaultClient`, bound to
    one project instead of addressing paths or vaults. `runner` and
    `project_id` are the only inputs: no credential, endpoint or region is
    read from anywhere else in this class, because gcloud resolves its
    ambient session and its own configured project fallback the way any
    other invocation on this host does -- and the project id is passed
    explicitly on every call so the store can never drift with the CLI's
    ambient configuration.
    """

    def __init__(self, runner: Runner, *, project_id: str) -> None:
        self._runner = runner
        self._project_id = project_id

    def _describe_argv(self, secret_id: str) -> list[str]:
        """The probe argv: exit status alone decides create versus version-add."""
        return [
            GCLOUD_EXECUTABLE,
            "secrets",
            "describe",
            secret_id,
            "--project",
            self._project_id,
        ]

    def _create_argv(self, secret_id: str, file_path: Path) -> list[str]:
        """The first-publish argv: the value rides `--data-file`, never an argument."""
        return [
            GCLOUD_EXECUTABLE,
            "secrets",
            "create",
            secret_id,
            "--project",
            self._project_id,
            f"--data-file={file_path}",
        ]

    def _versions_add_argv(self, secret_id: str, file_path: Path) -> list[str]:
        """The re-publish argv: a new version of the existing secret."""
        return [
            GCLOUD_EXECUTABLE,
            "secrets",
            "versions",
            "add",
            secret_id,
            "--project",
            self._project_id,
            f"--data-file={file_path}",
        ]

    def _access_argv(self, secret_id: str) -> list[str]:
        """The read argv: `versions access latest` prints the stored bytes raw."""
        return [
            GCLOUD_EXECUTABLE,
            "secrets",
            "versions",
            "access",
            "latest",
            "--secret",
            secret_id,
            "--project",
            self._project_id,
        ]

    def _invoke(
        self, argv: list[str], secret_id: str, *, operation: str
    ) -> subprocess.CompletedProcess[str]:
        """Run `argv` through the injected runner and translate any failure.

        The one place every operation's error handling passes through, the
        same single-seam shape the two clients above give their own
        operations: the translation from a gcloud exit code or a missing
        binary to a `SecretManagerError` subclass exists once, not once per
        operation.
        """
        try:
            result = self._runner(argv, None)
        except FileNotFoundError as exc:
            raise SecretManagerUnavailableError(
                _secret_unavailable_no_binary_message(secret_id)
            ) from exc
        if result.returncode != 0:
            _raise_for_secret_failure(
                secret_id, self._project_id, operation, result.returncode, result.stderr
            )
        return result

    def secret_exists(self, secret_id: str) -> bool:
        """Whether `secret_id` exists, loudly on any answer but yes or no.

        The explicit probe the write path decides create-versus-version-add
        from, the same probe-before-mutate discipline the state stores
        apply: a confirmed-missing secret answers False, a present one
        True, and any other outcome -- a 403 on a project this identity
        cannot see, an unreachable endpoint -- raises with the remedy,
        never misread as "missing".

        Raises:
            SecretManagerUnavailableError: the store could not be reached.
            SecretManagerUnauthorizedError: the caller lacks access.
            SecretManagerUnclassifiedError: any other failure.
        """
        try:
            self._invoke(self._describe_argv(secret_id), secret_id, operation=SECRETS_DESCRIBE_OP)
        except SecretManagerSecretNotFoundError:
            return False
        return True

    def read_secret(self, secret_id: str) -> str:
        """The value stored under `secret_id`, byte for byte.

        The read counterpart of `write_secret`, and symmetric with it:
        `publish_to_secret_manager` uses it to confirm each published
        secret independently, rather than trusting the write call's own
        exit code. `versions access latest` prints the stored bytes raw
        with no trailing newline added (verified live, NUL bytes included),
        so the stdout is compared to what was written without trimming.

        Raises:
            SecretManagerUnavailableError: the store could not be reached.
            SecretManagerUnauthorizedError: the caller lacks access to
                this project.
            SecretManagerSecretNotFoundError: no secret exists under this
                id.
            SecretManagerError: the read answered nothing, which is not a
                state a published secret can be in.
            SecretManagerUnclassifiedError: the store rejected the
                operation for a reason this client does not classify.
        """
        result = self._invoke(self._access_argv(secret_id), secret_id, operation=SECRETS_ACCESS_OP)
        if result.stdout == "":
            raise SecretManagerError(
                f"ERROR: the read of {secret_id} returned nothing\n"
                f"Project '{self._project_id}' answered successfully but printed no "
                "value, which is not a state a published secret can be in.\n"
                "Re-run the publication, and check whether another process is "
                "writing the same project."
            )
        return result.stdout

    def write_secret(self, secret_id: str, value: str) -> None:
        """Store `value` under `secret_id`, adding a version when it already exists.

        The one place in this repository a Secret Manager secret is
        written. The argv invariant is the two clients' own, restated for
        gcp: `value` travels inside a `--data-file` document written to a
        private file this method creates and removes, and never appears in
        argv, so no sensitive material -- a TLS private key included -- can
        reach the process table. The create-versus-version-add decision is
        made from `secret_exists`' explicit probe, never from a caught
        error: `secrets create` answers the first publish (and creates the
        secret's first version from the same `--data-file`), and
        `versions add` answers every publish after it.

        Raises:
            SecretManagerUnavailableError: the store could not be reached.
            SecretManagerUnauthorizedError: the caller lacks access to
                this project.
            SecretManagerUnclassifiedError: the store rejected the
                operation for a reason this client does not classify.
        """
        with tempfile.TemporaryDirectory() as directory:
            document_path = Path(directory) / PUT_SECRET_DATA_FILENAME
            descriptor = os.open(document_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(value)
            if self.secret_exists(secret_id):
                argv = self._versions_add_argv(secret_id, document_path)
                operation = SECRETS_VERSIONS_ADD_OP
            else:
                argv = self._create_argv(secret_id, document_path)
                operation = SECRETS_CREATE_OP
            self._invoke(argv, secret_id, operation=operation)


def _publication_contents(paths: CertPaths) -> dict[str, str]:
    """Issue the server material and assemble everything a publish path writes.

    The one head both publication paths share: the CA existence check and
    its `create-ca` remedy, then `issue_server`'s output joined by the CA
    certificate read, keyed by filename. `publish` (Parameter Store) and
    `publish_to_key_vault` (Key Vault) both consume it, so the two stores
    can never be handed different bytes for the same instance.

    Raises:
        CertsError: the CA is missing, so there is nothing to sign with.
    """
    if not paths.ca_key.is_file() or not paths.ca_cert.is_file():
        raise CertsError(
            f"ERROR: no certificate authority for {paths.instance!r}\n"
            f"Expected {paths.ca_key} and {paths.ca_cert}.\n"
            f"Create it first: create-ca --instance {paths.instance}"
        )
    server = issue_server(paths)
    return {
        SERVER_KEY_FILENAME: server.key_pem,
        SERVER_CERT_FILENAME: server.cert_pem,
        CA_CERT_FILENAME: paths.ca_cert.read_text(encoding="utf-8"),
    }


def publish(paths: CertPaths, client: ParameterStoreClient) -> tuple[str, ...]:
    """Publish `paths.instance`'s TLS material to Parameter Store, returning the paths written.

    `issue_server` returns the server key and certificate as PEM text and
    persists neither, precisely so this function can hand them to Parameter
    Store without an intermediate file: the private key never touches disk
    outside the temporary directory `issue_server` already removed. It reaches
    the store through `ParameterStoreClient.write_parameter`, which passes
    every value on the child's stdin rather than in argv, so the key never
    reaches the process table either -- the same invariant that protects a
    stored secret (spec Section 5.4), applied to the one other kind of
    sensitive material this repository writes.

    Only the three destinations `publication_set` allows are written, with the
    types it declares: `SecureString` for the key and certificate, `String`
    for the CA certificate, which is public by construction. The CA *private*
    key is never a destination `publication_set` returns, so it cannot be
    published even by mistake.

    Each parameter overwrites rather than creates-if-absent: re-issuing
    material for an instance is a normal operation, and a publish that failed
    because a previous value existed would leave the instance pinned to a
    certificate the operator has already replaced.

    Every parameter is read back and compared against what was written before
    this function reports it published. A write's own exit code says the store
    accepted the call, not that the bytes an instance will later read are the
    bytes that were issued, and the failure that distinction produces surfaces
    much later as a handshake error naming neither the publish nor the
    certificate.

    Raises:
        CertsError: the CA is missing, so there is nothing to sign with.
        ParameterStoreError: the store rejected or could not serve a write.
            Deliberately not translated -- `ParameterStoreUnavailableError`
            and `ParameterStoreUnauthorizedError` already carry the remedy
            an operator needs, and re-wrapping them here would only bury
            it.
    """
    contents = _publication_contents(paths)

    written: list[str] = []
    for entry in publication_set(paths.instance):
        filename = entry.parameter_path.rsplit("/", 1)[1]
        value = contents[filename]
        client.write_parameter(entry.parameter_path, value, entry.parameter_type)
        stored = client.read_parameter(entry.parameter_path)
        if stored != value:
            raise CertsError(
                _read_back_mismatch_message(entry.parameter_path, paths, len(value), len(stored))
            )
        written.append(entry.parameter_path)
    return tuple(written)


def _read_back_mismatch_message(
    destination: str, paths: CertPaths, written_bytes: int, read_bytes: int
) -> str:
    """The one read-back refusal both publication paths raise."""
    return (
        f"ERROR: {destination} does not read back as it was written\n"
        f"Wrote {written_bytes} bytes, read back {read_bytes}.\n"
        "The daemon reads this material at container create; starting it against "
        "material that is not what was issued would fail the TLS handshake with an "
        "error naming neither this publish nor the certificate.\n"
        f"Re-run the publish for {paths.instance!r}, and check whether another "
        "process is writing the same store."
    )


def publish_to_key_vault(paths: CertPaths, client: KeyVaultClient) -> tuple[str, ...]:
    """Publish `paths.instance`'s TLS material to its Key Vault, returning the secrets written.

    The azure twin of `publish`, and it inherits that function's guarantees
    wholesale by consuming the same `_publication_contents` head and the
    same read-back discipline: `issue_server`'s output reaches the vault
    through `KeyVaultClient.write_secret`, which passes every value on a
    0600 `--file` inside a removed 0700 directory rather than in argv, so
    the key never touches the process table; only the three destinations
    `azure_publication_set` allows are written, the CA *private* key among
    the refused names; and every secret is read back through
    `KeyVaultClient.read_secret` and byte-compared against what was written
    before this function reports it published.

    Raises:
        CertsError: the CA is missing, so there is nothing to sign with.
        KeyVaultError: the vault rejected or could not serve a write.
            Deliberately not translated -- the `KeyVaultError` subclasses
            already carry the remedy an operator needs, and re-wrapping
            them here would only bury it.
    """
    contents = _publication_contents(paths)

    written: list[str] = []
    for secret_name in azure_publication_set(paths.instance):
        filename = next(
            candidate for candidate, secret in AZURE_PUBLISHABLE.items() if secret == secret_name
        )
        value = contents[filename]
        client.write_secret(secret_name, value)
        stored = client.read_secret(secret_name)
        if stored != value:
            raise CertsError(
                _read_back_mismatch_message(secret_name, paths, len(value), len(stored))
            )
        written.append(secret_name)
    return tuple(written)


def _require_openssl() -> None:
    """Raise before any file or directory is touched if `openssl` is not on PATH.

    Error Handling Contract, path 1: this check runs first in every function
    below that creates material, ahead of any `mkdir` or `os.open`, so a
    missing `openssl` never leaves a half-created instance directory behind.
    """
    if shutil.which(OPENSSL_EXECUTABLE) is None:
        raise CertsError(
            "ERROR: openssl is not installed\n"
            "certs.py shells out to the openssl binary for every certificate "
            "operation (spec Section 3.4, Section 6) and none was found on "
            "PATH.\n"
            "Install openssl: 'brew install openssl' (macOS) or 'apt-get "
            "install openssl' (Linux/WSL), then retry."
        )


def _run_openssl(args: Sequence[str]) -> None:
    """Run `openssl <args>` and raise, carrying its stderr, on a non-zero exit.

    The single subprocess-construction site both `create_ca` and
    `_issue_certificate` use (TDD REFACTOR step of this task's own Approach),
    so the argument-list-only invocation (`shell=True` never appears here)
    and the error-message shape are defined exactly once.
    """
    result = subprocess.run(
        [OPENSSL_EXECUTABLE, *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise CertsError(
            f"ERROR: openssl {args[0]} failed (exit {result.returncode})\n"
            f"{result.stderr.strip()}\n"
            "Check the arguments and certificate authority state above and retry."
        )


def _resolve_lifetime_days(env_var: str, default: int) -> int:
    """The lifetime in days for one certificate role (spec Section 7.3).

    Reads `env_var` fresh on every call rather than caching it at import
    time, so a caller that sets it before issuing observes its own value and
    no call site ever hardcodes a lifetime (AC-FUNC-004).
    """
    raw = os.environ.get(env_var)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise CertsError(
            f"ERROR: {env_var}={raw!r} is not an integer\n"
            f"{env_var} must be a whole number of days.\n"
            "Unset it to use the default, or set it to a valid positive integer."
        ) from exc
    if value <= 0:
        raise CertsError(
            f"ERROR: {env_var}={raw!r} must be a positive integer\n"
            f"A certificate lifetime of {value} days is not valid.\n"
            "Set a positive integer, or unset it to use the default."
        )
    return value


def _raise_from_oserror(action: str, path: Path, exc: OSError) -> NoReturn:
    """The one place an `OSError` from a directory or file operation below
    becomes `CertsError`, carrying `path` and a remedy, instead of escaping
    uncaught past `main`'s single `except CertsError` boundary as a stack
    trace (code_review, this unit, round 1: BLOCKING 3; Error Handling
    Contract; Section 3.1's rule against writing a second error printer,
    reused here the same way `_run_openssl` is the one subprocess-error
    printer)."""
    raise CertsError(
        f"ERROR: cannot {action} {path}\n"
        f"{exc.strerror or exc}\n"
        "Check filesystem permissions for the certificate material root and retry."
    ) from exc


def _prepare_output_file(path: Path, mode: int, *, allow_overwrite: bool) -> None:
    """Pre-create `path` at `mode` before `openssl` ever writes to it.

    `openssl`'s own `-out`/`-keyout` writers apply the process umask to a
    newly created file and never change the mode of a file that already
    exists, so this function is what guarantees the final mode regardless of
    which of those two cases applies or what the umask happens to be: it
    creates (or truncates) the file, then re-applies `mode` explicitly with
    `os.chmod` rather than trusting the mode passed to `os.open` to survive
    the umask unmodified.

    Raises:
        CertsError: `allow_overwrite` is `False` and `path` already exists --
            used only by `create_ca`, whose CA material must never be
            silently overwritten -- or `os.open`/`os.chmod` fails for any
            other reason (for example a read-only material root), which
            round 1's BLOCKING 3 requires surface as `CertsError` rather
            than an uncaught `OSError`.
    """
    flags = os.O_WRONLY | os.O_CREAT | (os.O_TRUNC if allow_overwrite else os.O_EXCL)
    try:
        file_descriptor = os.open(str(path), flags, mode)
    except FileExistsError as exc:
        raise CertsError(
            f"ERROR: {path} already exists\n"
            "Refusing to overwrite existing certificate material through this path.\n"
            "Remove it first if replacing it is truly intended."
        ) from exc
    except OSError as exc:
        _raise_from_oserror("create", path, exc)
    try:
        os.close(file_descriptor)
        os.chmod(path, mode)
    except OSError as exc:
        _raise_from_oserror("set permissions on", path, exc)


def _ensure_dir(path: Path, mode: int) -> None:
    """Create `path` (and parents) at `mode` if it does not exist yet.

    Shared by `_ensure_instance_dir` and `create_ca`'s own `ca_dir` creation,
    which previously repeated the identical `mkdir`/`os.chmod` pair
    independently (code_review, this unit, round 1: WARN 4, DRY), and the
    single place an `OSError` from either call -- a permission failure on
    the material root, for example -- is translated into `CertsError`
    (round 1: BLOCKING 3).
    """
    try:
        path.mkdir(parents=True, exist_ok=True)
        os.chmod(path, mode)
    except OSError as exc:
        _raise_from_oserror("create or set permissions on", path, exc)


def _ensure_instance_dir(paths: CertPaths) -> None:
    """Create `paths.instance_dir` at `INSTANCE_DIR_MODE` if it does not exist yet."""
    _ensure_dir(paths.instance_dir, INSTANCE_DIR_MODE)


# The marker every well-formed PEM block starts with. `_require_parseable_ca_component`
# checks for its presence rather than fully parsing the file (this module adds no
# third-party X.509 parser, spec Section 3.4/Section 6's stdlib-only rule), which is
# enough to distinguish real key/certificate material from the empty or truncated
# file a partially written authority can leave behind.
_PEM_MARKER = b"-----BEGIN"


def _require_parseable_ca_component(path: Path, instance: str) -> None:
    """Raise unless `path` contains a PEM block.

    `_require_ca`'s own `Path.is_file()` check is true for a zero-byte or
    otherwise corrupt file, which would let `issue_server`/`issue_client`
    hand it straight to `openssl x509 -req -CAkey`: that fails with a raw
    `openssl` stderr line naming neither this path nor a remedy, while a
    second `create_ca` call for the same instance reports "already exists"
    for an authority that, in this corrupt state, cannot actually sign
    anything -- two contradictory opaque failures for the same root cause
    (code_review, this unit, round 2: BLOCKING part B). Checked only after
    `_require_ca` has already confirmed both files exist, so a genuinely
    missing authority still raises the "no certificate authority exists"
    message below instead of this one.

    Raises:
        CertsError: `path` cannot be read, or its content contains no PEM
            block.
    """
    try:
        content = path.read_bytes()
    except OSError as exc:
        _raise_from_oserror("read", path, exc)
    if _PEM_MARKER not in content:
        raise CertsError(
            f"ERROR: {path} is empty or not a valid PEM file\n"
            f"The certificate authority for instance {instance!r} is corrupt: "
            f"{path} exists but does not contain usable key or certificate "
            "material.\n"
            "Remove the existing authority directory and run create_ca again."
        )


def _require_ca(paths: CertPaths) -> None:
    """Raise unless a usable authority already exists for `paths.instance`.

    `.agents/skills/gd-cert-lifecycle/SKILL.md`'s own precondition
    row for "Issue server certificate", "Issue client certificate" and
    "Rotate client certificate" names exactly this requirement; every
    function below that needs an authority checks it before shelling out to
    `openssl` at all, and none of them ever creates one implicitly on this
    failure -- a fresh CA would silently invalidate every certificate the
    previous one already signed, including the server certificate the
    running daemon is serving (this task's Error Handling Contract,
    AC-FUNC-005). The remedy names the exact skill invocation that creates
    an authority (`RENEW_INVOCATION_TEMPLATE`, the identical
    `gd-cert-lifecycle INSTANCE=<name>` spelling `## Expiry`'s own `RENEW`
    row already gives an operator, reused here rather than a second literal)
    rather than the Python function name, since the operator driving this
    module is the `certs` skill, never `certs.py` directly. Beyond the
    existence check, each component is also checked for being a parseable
    PEM file (`_require_parseable_ca_component`), so a corrupt authority is
    rejected by name rather than reaching `openssl` as an opaque failure.
    """
    if not paths.ca_key.is_file() or not paths.ca_cert.is_file():
        raise CertsError(
            f"ERROR: no certificate authority exists for instance {paths.instance!r}\n"
            f"Expected {paths.ca_key} and {paths.ca_cert} to already exist.\n"
            f"Run {RENEW_INVOCATION_TEMPLATE.format(instance=paths.instance)} to create one first."
        )
    for component in (paths.ca_key, paths.ca_cert):
        _require_parseable_ca_component(component, paths.instance)


def _random_serial() -> int:
    """A fresh positive serial number for one signing operation.

    Avoids `openssl x509 -req`'s own `-CAcreateserial`, which would leave a
    `.srl` bookkeeping file next to the CA certificate -- a fifth path spec
    Section 5.5 does not name. A 63-bit random value is unique enough for
    this project's developer-scale, single-CA-per-instance deployment (spec
    Section 13, decisions D3/D4) without any file tracking the last serial
    issued.
    """
    return secrets.randbits(63) | 1


def _ca_already_exists_message(paths: CertPaths) -> str:
    """Name whichever CA component(s) already exist for `paths.instance`.

    `create_ca`'s pre-flight check (round 3 fix, BLOCKING A) raises whenever
    either `paths.ca_key` or `paths.ca_cert` is present, alone or together,
    so this message names exactly the file(s) that exist rather than always
    naming `paths.ca_key` -- a lone pre-existing `ca.pem` with no matching
    `ca-key.pem` is exactly the case that fix exists to protect.
    """
    existing = [str(path) for path in (paths.ca_key, paths.ca_cert) if path.exists()]
    verb = "is" if len(existing) == 1 else "are"
    return (
        f"ERROR: a certificate authority already exists for instance {paths.instance!r}\n"
        f"{' and '.join(existing)} {verb} already present.\n"
        "Overwriting it would silently invalidate every certificate already issued "
        "from this CA and every daemon that already trusts it. Remove the existing "
        "authority deliberately first if reissuing is truly intended."
    )


def create_ca(paths: CertPaths) -> None:
    """Create the authority for `paths.instance`: `ca-key.pem` (0600) and `ca.pem` (0644).

    Creates `paths.instance_dir` at `INSTANCE_DIR_MODE` (0700) if it does not
    exist yet. The existence check below runs before any filesystem mutation
    and rejects EITHER component being present, alone or together, so a lone
    pre-existing `ca.pem` with no matching `ca-key.pem` (or the reverse) is
    refused up front rather than being discovered mid-write (code_review,
    this unit, round 3: BLOCKING A -- the round 2 version of this check only
    tested `paths.ca_key.exists()`, so that lone-`ca.pem` case reached the
    try block below and, when `_prepare_output_file(paths.ca_cert, ...)`
    then raised `FileExistsError`, the except handler's unconditional
    `paths.ca_cert.unlink(missing_ok=True)` deleted that pre-existing
    `ca.pem`, which this invocation never wrote). On any failure once
    writing has begun, only the component(s) THIS invocation actually
    created are removed -- tracked by `created_ca_key`/`created_ca_cert`
    below -- so a retry never sees a stale "authority already exists" error
    for an authority that was never actually created, and a pre-existing
    file this invocation did not touch is never at risk of deletion by this
    handler.

    Raises:
        CertsError: `openssl` is not on PATH (checked first, before anything
            is created), or an authority already exists for `paths.instance`.
    """
    _require_openssl()
    if paths.ca_key.exists() or paths.ca_cert.exists():
        raise CertsError(_ca_already_exists_message(paths))
    lifetime = _resolve_lifetime_days(CERT_CA_DAYS_ENV_VAR, CERT_CA_DAYS_DEFAULT)
    created_ca_key = False
    created_ca_cert = False
    try:
        _ensure_instance_dir(paths)
        _ensure_dir(paths.ca_dir, INSTANCE_DIR_MODE)
        _prepare_output_file(paths.ca_key, PRIVATE_KEY_MODE, allow_overwrite=False)
        created_ca_key = True
        _prepare_output_file(paths.ca_cert, PUBLIC_CERT_MODE, allow_overwrite=False)
        created_ca_cert = True
        _run_openssl(
            [
                "req",
                "-x509",
                "-new",
                "-newkey",
                "ec",
                "-pkeyopt",
                f"ec_paramgen_curve:{EC_CURVE}",
                "-keyout",
                str(paths.ca_key),
                "-out",
                str(paths.ca_cert),
                "-days",
                str(lifetime),
                "-nodes",
                "-subj",
                f"/CN={paths.instance}-ca",
                f"-{DIGEST}",
            ]
        )
    except CertsError:
        if created_ca_key:
            paths.ca_key.unlink(missing_ok=True)
        if created_ca_cert:
            paths.ca_cert.unlink(missing_ok=True)
        raise


def _commit_pair(key_out: Path, cert_out: Path, issued_key: Path, issued_cert: Path) -> None:
    """Install `issued_key`/`issued_cert` at `key_out`/`cert_out` as a single unit.

    Whichever of `key_out`/`cert_out` already exists is moved aside first,
    to a sibling path in the same directory (`_PAIR_BACKUP_SUFFIX`) --
    itself an `os.replace`, atomic on this filesystem -- before either
    replacement is installed. This task's own atomicity requirement is
    about the *pair*, not each file independently: installing the new key
    with one `os.replace` and the new certificate with a second, unguarded
    one left a failure between them free to commit a new key with no
    matching certificate (or the reverse), a mismatched pair that is worse
    than leaving the previous, matched pair untouched (this task's Error
    Handling Contract, second bullet; AC-FUNC-004). On any `OSError` from
    this function, whichever backup(s) were made are moved back to their
    original paths before `CertsError` is raised naming both destination
    paths, so a rotation that raises `OSError` at any point during the
    commit always leaves either the complete previous pair or the complete
    new one. This guarantee covers only the `OSError` path the `except`
    clause below handles: a process terminated between the backup and the
    install (a `SIGKILL`, for example, which no `except` clause can run
    after) leaves both destination paths absent and the two
    `_PAIR_BACKUP_SUFFIX` files as the only trace, an outage the caller must
    detect and repair by hand rather than something this function recovers
    from automatically.
    First issuance (neither `key_out` nor `cert_out` exists yet) never
    triggers a backup at all: a first-issuance failure at this point leaves
    nothing at either path, matching `issue_client`'s existing "nothing
    created" guarantee.

    Raises:
        CertsError: any `os.replace` call above raises `OSError` -- a
            read-only material root, or a cross-device destination.
    """
    key_backup = key_out.with_name(key_out.name + _PAIR_BACKUP_SUFFIX)
    cert_backup = cert_out.with_name(cert_out.name + _PAIR_BACKUP_SUFFIX)
    backed_up_key = False
    backed_up_cert = False
    try:
        if key_out.exists():
            os.replace(key_out, key_backup)
            backed_up_key = True
        if cert_out.exists():
            os.replace(cert_out, cert_backup)
            backed_up_cert = True
        os.replace(issued_key, key_out)
        os.replace(issued_cert, cert_out)
    except OSError as exc:
        if backed_up_key:
            os.replace(key_backup, key_out)
        if backed_up_cert:
            os.replace(cert_backup, cert_out)
        raise CertsError(
            f"ERROR: cannot install the replacement certificate at {key_out} and {cert_out}\n"
            f"{exc.strerror or exc}\n"
            "The previous key and certificate at these paths, if any, remain in place "
            "unchanged. Check filesystem permissions for the certificate material root "
            "and retry."
        ) from exc
    finally:
        key_backup.unlink(missing_ok=True)
        cert_backup.unlink(missing_ok=True)


def _issue_certificate(
    paths: CertPaths,
    *,
    role_cn_suffix: str,
    days: int,
    addext_lines: Sequence[str],
    key_out: Path,
    cert_out: Path,
) -> None:
    """Generate a fresh key, a CSR carrying `addext_lines`, and a CA-signed certificate.

    The key, CSR and certificate are all generated inside a
    `tempfile.TemporaryDirectory` nested under `key_out.parent` -- the same
    filesystem as the final destination, so the commit step below is a plain
    rename rather than a cross-device copy -- and removed before this
    function returns on every path, including a failure inside either
    `openssl` call, so no CSR and no temporary `openssl` configuration file
    is ever left behind (AC-FUNC-006). Extensions travel through `-addext`
    at CSR-creation time and `-copy_extensions copy` at signing time, never
    through a temporary `-extfile`.

    `key_out` and `cert_out` are never touched until both `openssl` calls
    have succeeded, at which point the freshly issued key and certificate
    are committed as a single unit by `_commit_pair` -- atomic on the same
    filesystem. A mid-issuance failure (inside either `openssl` call above)
    therefore leaves whatever was already at `key_out`/`cert_out` (nothing,
    for a first issuance; the previously valid material, for a rotation)
    completely untouched, rather than truncating or unlinking it first and
    only then attempting the `openssl` calls that can fail. The pre-round-3
    implementation prepared `key_out` for writing (truncating any existing
    key) before the first `openssl` call and unlinked both `key_out` and
    `cert_out` on failure, so a failing `openssl` during `issue_client`'s
    rotation destroyed a previously valid, still-trusted client certificate
    and key with nothing to replace them -- stranding the developer without
    any client credential (code_review, this unit, round 1: WARN 5; round 3:
    WARN C).
    """
    with tempfile.TemporaryDirectory(
        prefix="devcontainer-certs-", dir=str(key_out.parent)
    ) as tmp_dir:
        csr_path = Path(tmp_dir) / "request.csr"
        issued_key = Path(tmp_dir) / "issued-key.pem"
        issued_cert = Path(tmp_dir) / "issued-cert.pem"
        _prepare_output_file(issued_key, PRIVATE_KEY_MODE, allow_overwrite=True)
        request_args = [
            "req",
            "-new",
            "-newkey",
            "ec",
            "-pkeyopt",
            f"ec_paramgen_curve:{EC_CURVE}",
            "-keyout",
            str(issued_key),
            "-out",
            str(csr_path),
            "-nodes",
            "-subj",
            f"/CN={paths.instance}-{role_cn_suffix}",
            f"-{DIGEST}",
        ]
        for addext_line in addext_lines:
            request_args.extend(["-addext", addext_line])
        _run_openssl(request_args)
        _prepare_output_file(issued_cert, PUBLIC_CERT_MODE, allow_overwrite=True)
        _run_openssl(
            [
                "x509",
                "-req",
                "-in",
                str(csr_path),
                "-CA",
                str(paths.ca_cert),
                "-CAkey",
                str(paths.ca_key),
                "-set_serial",
                str(_random_serial()),
                "-days",
                str(days),
                "-copy_extensions",
                "copy",
                "-out",
                str(issued_cert),
                f"-{DIGEST}",
            ]
        )
        _commit_pair(key_out, cert_out, issued_key, issued_cert)


def issue_server(paths: CertPaths) -> ServerCertificate:
    """Issue the server certificate and key from `paths.instance`'s CA.

    Both are generated inside a temporary directory removed before this
    function returns; neither is ever written under `paths.instance_dir`
    (see `ServerCertificate`'s own docstring for why).

    Raises:
        CertsError: `openssl` is not on PATH, or no authority exists yet for
            `paths.instance`.
    """
    _require_openssl()
    _require_ca(paths)
    lifetime = _resolve_lifetime_days(CERT_SERVER_DAYS_ENV_VAR, CERT_SERVER_DAYS_DEFAULT)
    with tempfile.TemporaryDirectory(prefix="devcontainer-certs-server-") as tmp_dir:
        key_path = Path(tmp_dir) / SERVER_KEY_FILENAME
        cert_path = Path(tmp_dir) / SERVER_CERT_FILENAME
        _issue_certificate(
            paths,
            role_cn_suffix="server",
            days=lifetime,
            addext_lines=(
                f"subjectAltName={SERVER_SAN}",
                f"extendedKeyUsage={SERVER_EXTENDED_KEY_USAGE}",
            ),
            key_out=key_path,
            cert_out=cert_path,
        )
        return ServerCertificate(
            key_pem=key_path.read_text(encoding="ascii"),
            cert_pem=cert_path.read_text(encoding="ascii"),
        )


def issue_client(paths: CertPaths) -> None:
    """Issue the client certificate and key at `paths.client_cert`/`paths.client_key`.

    Unlike `create_ca`, an existing client certificate is overwritten rather
    than rejected: `rotate_client` below is this same call, under its own
    name, and the instance is left running throughout, which only works if
    reissuing never refuses on an existing file.

    Raises:
        CertsError: `openssl` is not on PATH, or no authority exists yet for
            `paths.instance`.
    """
    _require_openssl()
    _require_ca(paths)
    lifetime = _resolve_lifetime_days(CERT_CLIENT_DAYS_ENV_VAR, CERT_CLIENT_DAYS_DEFAULT)
    _ensure_instance_dir(paths)
    _issue_certificate(
        paths,
        role_cn_suffix="client",
        days=lifetime,
        addext_lines=(f"extendedKeyUsage={CLIENT_EXTENDED_KEY_USAGE}",),
        key_out=paths.client_key,
        cert_out=paths.client_cert,
    )


def rotate_client(paths: CertPaths) -> None:
    """Rotate the client certificate and key at `paths.client_cert`/`paths.client_key` (AC-10.9).

    Calls `issue_client` and does nothing else: it is the standing name spec
    Section 4.5 and `.agents/skills/gd-cert-lifecycle/SKILL.md`'s
    own "Rotate client certificate" row give the operation `issue_client`
    already performs, reused here rather than a second, independent
    issuance path (this task's own Approach). Every invariant this task
    proves belongs to `issue_client`/`_issue_certificate`/`_commit_pair`,
    which this function inherits by delegating to them:

    - Only `paths.client_cert` and `paths.client_key` ever change. The CA
      key, the CA certificate, and the server material the running daemon
      is already serving are never read, written, or reissued by any call
      this function makes (AC-FUNC-001).
    - The replacement carries a fresh serial (`_random_serial`) and its own
      independently computed `notAfter`, `clientAuth` and the file modes
      spec Section 5.5 fixes, and verifies against the unchanged CA
      (AC-FUNC-002).
    - Nothing is published: this function calls no function in this module
      that touches Parameter Store and returns `None`, so no caller of it
      can infer that a parameter write follows (AC-FUNC-003); the instance
      itself is untouched, so no docker context update, daemon restart or
      `terragrunt` run is implied by a call to this function either.
    - The commit is atomic per `_commit_pair`: a rotation that raises
      `OSError` during the commit leaves either the complete previous pair
      or the complete new one, never a half-written key or a mismatched
      pair (AC-FUNC-004); see `_commit_pair`'s own docstring for the
      narrower failure mode -- a killed process -- that guarantee does not
      cover.
    - No certificate authority is ever created here: `_require_ca` (via
      `issue_client`) raises rather than falling back to `create_ca`, naming
      the skill invocation that creates one, because a fresh CA would
      silently invalidate every certificate the previous one already
      signed, including the server certificate the running daemon is
      serving (AC-FUNC-005).

    Raises:
        CertsError: `openssl` is not on PATH, or no authority exists yet for
            `paths.instance` -- never created implicitly.
    """
    issue_client(paths)


def _is_ca_private_key_request(name: str) -> bool:
    return name in _CA_PRIVATE_KEY_NAMES or Path(name).name == CA_KEY_FILENAME


def _ca_key_publication_denied_message(name: str) -> str:
    return (
        f"ERROR: refusing to publish {name!r}\n"
        "The CA private key never leaves the laptop: the daemon only ever needs "
        "ca.pem to verify a client, and an instance holding the CA key could mint "
        "its own client certificates after a compromise and drive any daemon that "
        "trusts the same authority.\n"
        "Call publication_set() with no arguments for the entries that may be published."
    )


def _unpublishable_name_message(name: str, publishable: dict[str, str]) -> str:
    allowed = ", ".join(publishable)
    return (
        f"ERROR: {name!r} is not part of the publication set\n"
        f"Only {allowed} may be published for an instance (spec Section 5.3).\n"
        "Call publication_set() with no arguments for the default set."
    )


def _validated_publication_names(
    instance: str, filenames: Sequence[str] | None, publishable: dict[str, str]
) -> tuple[str, ...]:
    """The publication names asked for, after both refusal rules ran.

    The one body both `publication_set` and `azure_publication_set` share,
    so the CA-private-key refusal and the not-in-the-set refusal can never
    drift between the Parameter Store set and the Key Vault set: the rules
    live once, and only the mapping a validated name resolves through
    differs.
    """
    _validate_instance(instance)
    names = tuple(publishable) if filenames is None else tuple(filenames)
    validated = []
    for name in names:
        if _is_ca_private_key_request(name):
            raise CertsError(_ca_key_publication_denied_message(name))
        if name not in publishable:
            raise CertsError(_unpublishable_name_message(name, publishable))
        validated.append(name)
    return tuple(validated)


def publication_set(
    instance: str, filenames: Sequence[str] | None = None
) -> tuple[PublicationEntry, ...]:
    """The Parameter Store entries `instance`'s material is allowed to publish (spec Section 5.3).

    With `filenames` omitted, returns exactly the three entries spec Section
    5.3 names for TLS material: `tls/server-key.pem` and `tls/server-cert.pem`
    as `SecureString`, `tls/ca.pem` as `String`. Passing `filenames`
    restricts (or, for any name outside that set, rejects) the returned
    entries -- it never adds one.

    Raises:
        CertsError: `instance` is empty or contains a path separator
            (`_validate_instance`, checked before any path is composed), or
            `filenames` names the CA private key, under any spelling
            (`ca-key.pem`, `ca/ca-key.pem`, or any path whose final component
            is `ca-key.pem`), or any other name outside the three entries
            this function returns by default.
    """
    entries = []
    for name in _validated_publication_names(instance, filenames, _PUBLISHABLE):
        entries.append(
            PublicationEntry(
                parameter_path=f"{PARAMETER_ROOT}/{instance}/{TLS_SEGMENT}/{name}",
                parameter_type=_PUBLISHABLE[name],
            )
        )
    return tuple(entries)


def azure_publication_set(instance: str, filenames: Sequence[str] | None = None) -> tuple[str, ...]:
    """The Key Vault secret names `instance`'s material may publish (the azure kind).

    The azure twin of `publication_set`: the same three filenames, refused
    through the same rules -- the CA private key under any spelling, and
    anything outside the set by name -- mapped to the Key Vault secret names
    `AZURE_PUBLISHABLE` declares, whose dash-joined forms are the shapes the
    vault's own naming rule permits (alphanumerics and dashes only). Passing
    `filenames` restricts (or, for any name outside the set, rejects) the
    returned names -- it never adds one.

    Raises:
        CertsError: `instance` is empty or contains a path separator
            (`_validate_instance`, checked before anything is returned), or
            `filenames` names the CA private key under any spelling, or any
            other name outside the three names this function returns by
            default.
    """
    validated = _validated_publication_names(instance, filenames, AZURE_PUBLISHABLE)
    return tuple(AZURE_PUBLISHABLE[name] for name in validated)


def gcp_secret_id(prefix: str, leaf: str) -> str:
    """The full Secret Manager secret id for `leaf` under the sanitized `prefix`.

    The one composition of the `<prefix>-<leaf>` shape the gcp kind's six
    per-instance secrets carry -- the four TLS-and-checksum leaves this
    module publishes and the two configuration-payload leaves
    `push-secrets.sh` publishes -- so the write side and every later
    reader (the VM's startup script, the status probe, the cleanup arm)
    compose the same ids from one rule, never from two spellings of it.
    """
    return f"{prefix}-{leaf}"


def gcp_instance_secret_ids(prefix: str) -> tuple[str, ...]:
    """All six Secret Manager secret ids one gcp instance's state lives under.

    The three TLS secrets, the checksum secret beside them, and the two
    configuration-payload secrets -- in publication order. The status
    probe asks for exactly these six and the cleanup arm deletes exactly
    these six, both through this one tuple, so a leaf added to one without
    the other is impossible by construction.
    """
    return (
        gcp_secret_id(prefix, GCP_SERVER_KEY_SECRET),
        gcp_secret_id(prefix, GCP_SERVER_CERT_SECRET),
        gcp_secret_id(prefix, GCP_CA_SECRET),
        gcp_secret_id(prefix, GCP_CHECKSUM_SECRET),
        gcp_secret_id(prefix, GCP_SHELL_ENV_SECRET),
        gcp_secret_id(prefix, GCP_PROFILE_MAP_SECRET),
    )


def gcp_material_checksum(contents: dict[str, str]) -> str:
    """The sha256 of the three PEM bodies concatenated, in publication order.

    The one value the gcp kind's boot-applied install is decided by:
    `publish_to_secret_manager` writes this checksum beside the material it
    covers, the VM's startup script (whose contract this module and
    `transport_gcp` own) compares the checksum it fetched against the
    `general-dev/tls-checksum` guest attribute, and
    `transport_gcp.applied_checksum_matches` compares the store's checksum
    against the same attribute from the laptop. Deterministic by
    construction -- the same three bodies always hash to the same value --
    so the comparison needs no canonical form beyond this function's own
    fixed order.
    """
    material = "".join(contents[filename] for filename in GCP_PUBLISHABLE)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def gcp_publication_set(instance: str, filenames: Sequence[str] | None = None) -> tuple[str, ...]:
    """The Secret Manager secret ids `instance`'s material may publish (the gcp kind).

    The gcp twin of `publication_set` and `azure_publication_set`: the
    same three filenames, refused through the same rules -- the CA private
    key under any spelling, and anything outside the set by name -- mapped
    to the secret-id leaves `GCP_PUBLISHABLE` declares, prefixed by the
    sanitized instance name and completed by the checksum leaf, so the
    default answer is four ids: the three TLS secrets and
    `<prefix>-tls-checksum`, whose value `publish_to_secret_manager`
    computes. The prefix is derived here the same way the deploy mints the
    record's `secrets_prefix` (`instances.sanitize_gcp_name`), so the
    listing and the store can never disagree. Passing `filenames`
    restricts (or, for any name outside the TLS set, rejects) the returned
    names -- it never adds one; the checksum id rides every publication
    and is not itself a `filenames` entry.

    Raises:
        CertsError: `instance` is empty or contains a path separator
            (`_validate_instance`, checked before anything is returned),
            or `filenames` names the CA private key under any spelling, or
            any other name outside the three names this function maps.
        instances.InvalidGCPNameError: the instance name has no legal
            sanitized form to prefix the ids with.
    """
    validated = _validated_publication_names(instance, filenames, GCP_PUBLISHABLE)
    prefix = instances.sanitize_gcp_name(instance)
    ids = [gcp_secret_id(prefix, GCP_PUBLISHABLE[name]) for name in validated]
    # The checksum secret rides every publication: the boot-applied install
    # is decided by it, so a set without it would publish material no VM
    # could ever confirm.
    ids.append(gcp_secret_id(prefix, GCP_CHECKSUM_SECRET))
    return tuple(ids)


def publish_to_secret_manager(
    paths: CertPaths, client: SecretManagerClient, *, prefix: str
) -> tuple[str, ...]:
    """Publish `paths.instance`'s TLS material to Secret Manager, returning the ids written.

    The gcp twin of `publish` and `publish_to_key_vault`, and it inherits
    their guarantees wholesale by consuming the same `_publication_contents`
    head and the same read-back discipline: `issue_server`'s output reaches
    the store through `SecretManagerClient.write_secret`, which passes every
    value in a `--data-file` inside a removed 0700 directory rather than in
    argv, so the key never touches the process table; only the four ids
    `gcp_publication_set` allows are written -- the three TLS secrets plus
    `<prefix>-tls-checksum`, whose value `gcp_material_checksum` computes
    over the same contents; and every secret is read back through
    `SecretManagerClient.read_secret` and byte-compared against what was
    written before this function reports it published.

    The checksum secret is what makes the install boot-applied rather than
    pushed: the VM's startup script fetches the three secrets and this
    checksum on every boot, compares the checksum against the
    `general-dev/tls-checksum` guest attribute, and applies the material
    only on drift -- so `prefix` must be exactly the prefix the deploy
    recorded, because that is the prefix the VM fetches by.

    Raises:
        CertsError: the CA is missing, so there is nothing to sign with;
            or `prefix` is not the sanitized form of `paths.instance`,
            which would publish material the VM would never fetch.
        SecretManagerError: the store rejected or could not serve a
            write. Deliberately not translated -- the
            `SecretManagerError` subclasses already carry the remedy an
            operator needs, and re-wrapping them here would only bury it.
    """
    expected_prefix = instances.sanitize_gcp_name(paths.instance)
    if prefix != expected_prefix:
        raise CertsError(
            f"ERROR: the secrets prefix {prefix!r} does not match instance "
            f"{paths.instance!r}\n"
            f"The gcp kind publishes under the sanitized instance name "
            f"({expected_prefix!r}), and the VM's startup script fetches under the "
            "prefix the engine address record recorded.\n"
            f"Redeploy to rewrite the record: make instance-deploy "
            f"INSTANCE={paths.instance}"
        )
    contents = _publication_contents(paths)
    checksum = gcp_material_checksum(contents)
    publications: list[tuple[str, str]] = [
        (gcp_secret_id(prefix, GCP_PUBLISHABLE[filename]), contents[filename])
        for filename in GCP_PUBLISHABLE
    ]
    publications.append((gcp_secret_id(prefix, GCP_CHECKSUM_SECRET), checksum))
    written: list[str] = []
    for secret_id, value in publications:
        client.write_secret(secret_id, value)
        stored = client.read_secret(secret_id)
        if stored != value:
            raise CertsError(_read_back_mismatch_message(secret_id, paths, len(value), len(stored)))
        written.append(secret_id)
    return tuple(written)


def not_after(cert_path: Path) -> datetime.datetime:
    """The `notAfter` timestamp `cert_path` carries, as a timezone-aware UTC `datetime`.

    Parses `openssl x509 -noout -enddate`'s own output rather than a second,
    hand-rolled ASN.1 reader (module docstring). Never reports a read or
    parse failure as an expired certificate (Error Handling Contract): a
    file that cannot be opened, or one `openssl` cannot parse as a
    certificate at all, raises naming `cert_path` and the underlying
    failure instead, because reporting either as "expired" would send the
    operator to reissue material that may be perfectly valid.

    Raises:
        CertsError: `cert_path` cannot be read or is not a parseable
            certificate.
    """
    result = subprocess.run(
        [OPENSSL_EXECUTABLE, "x509", "-noout", "-enddate", "-in", str(cert_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise CertsError(
            f"ERROR: cannot read certificate {cert_path}\n"
            f"{result.stderr.strip()}\n"
            "Confirm the file exists, is readable, and is a valid PEM certificate."
        )
    _, _, raw_date = result.stdout.strip().partition("=")
    try:
        parsed = datetime.datetime.strptime(raw_date.strip(), "%b %d %H:%M:%S %Y %Z")
    except ValueError as exc:
        raise CertsError(
            f"ERROR: cannot parse the expiry date of {cert_path}\n"
            f"openssl returned {result.stdout.strip()!r}, which is not a recognized "
            "notAfter date.\n"
            "Confirm the file is a valid PEM certificate."
        ) from exc
    return parsed.replace(tzinfo=datetime.UTC)


def days_remaining(cert_not_after: datetime.datetime, reference_time: datetime.datetime) -> int:
    """The whole number of days from `reference_time` until `cert_not_after`.

    Negative once `cert_not_after` has already passed `reference_time`.
    Never reads the system clock (AC-FUNC-001): both arguments are supplied
    by the caller -- `status_rows`'s own `reference_time` parameter in
    production, an injected clock in a test -- so this arithmetic is exact
    and reproducible regardless of when it runs.
    """
    return (cert_not_after - reference_time).days


def classify(
    cert_not_after: datetime.datetime, reference_time: datetime.datetime, warn_days: int
) -> str:
    """`STATUS_EXPIRED`, `STATUS_RENEW` or `STATUS_OK` for one certificate (spec Section 4.1.2).

    `STATUS_EXPIRED` once `cert_not_after` has passed `reference_time`;
    `STATUS_RENEW` from that instant through `warn_days` days out inclusive
    on both ends (a certificate expiring in exactly `warn_days` days is
    `STATUS_RENEW`, one expiring in `warn_days + 1` days is `STATUS_OK`);
    `STATUS_OK` beyond that. Takes `reference_time` and `warn_days` as
    explicit parameters rather than reading the system clock or
    `CERT_WARN_DAYS` itself (AC-FUNC-001, AC-FUNC-007): `_resolve_cert_warn_days`
    is the one place the environment variable is read, and its result is
    threaded through every call this module makes to this function.
    """
    remaining = days_remaining(cert_not_after, reference_time)
    if remaining < 0:
        return STATUS_EXPIRED
    if remaining <= warn_days:
        return STATUS_RENEW
    return STATUS_OK


def _resolve_cert_warn_days() -> int:
    """`CERT_WARN_DAYS` (spec Section 7.3), read fresh, exactly once per report (AC-FUNC-007).

    Zero is a valid window (only an already-expired certificate warns).
    Raises before `status_rows` renders a single row, naming the variable,
    its value and the expected form, rather than silently reverting to the
    default of `CERT_WARN_DAYS_DEFAULT` (Error Handling Contract).

    Raises:
        CertsError: `CERT_WARN_DAYS` is set to a non-integer or a negative value.
    """
    raw = os.environ.get(CERT_WARN_DAYS_ENV_VAR)
    if raw is None:
        return CERT_WARN_DAYS_DEFAULT
    try:
        value = int(raw)
    except ValueError as exc:
        raise CertsError(
            f"ERROR: {CERT_WARN_DAYS_ENV_VAR}={raw!r} is not an integer\n"
            f"{CERT_WARN_DAYS_ENV_VAR} must be a whole number of days.\n"
            f"Unset it to use the default of {CERT_WARN_DAYS_DEFAULT}, or set it to a "
            "non-negative integer."
        ) from exc
    if value < 0:
        raise CertsError(
            f"ERROR: {CERT_WARN_DAYS_ENV_VAR}={raw!r} must not be negative\n"
            f"A warning window of {value} days is not valid.\n"
            f"Set a non-negative integer, or unset it to use the default of "
            f"{CERT_WARN_DAYS_DEFAULT}."
        )
    return value


@dataclass(frozen=True)
class CertStatusRow:
    """One `INSTANCE ROLE EXPIRES DAYS STATUS` row of `make cert-status`'s report.

    `days` and `status` are pre-computed at construction time (`status_rows`'s
    own job) rather than derived lazily here, so `render_report` stays a pure
    formatter with no arithmetic or clock of its own.
    """

    instance: str
    role: str
    not_after: datetime.datetime
    days: int
    status: str


def _discover_instances(root: Path) -> tuple[str, ...]:
    """Every instance name with a directory directly under `root`, sorted for a stable report.

    `root` not existing at all is the identical "nothing configured yet"
    case as an existing, empty `root` (AC-FUNC-006's empty-inventory edge
    case): both return no instances rather than one raising `OSError` where
    the other returns an empty tuple.
    """
    if not root.is_dir():
        return ()
    return tuple(sorted(entry.name for entry in root.iterdir() if entry.is_dir()))


def _missing_role_message(instance: str, missing_role: str) -> str:
    return (
        f"ERROR: instance {instance!r} is missing its {missing_role} certificate\n"
        f"Other certificate material exists for this instance, but its {missing_role} "
        "certificate does not; a partial material set is never reported as a row.\n"
        f"Run gd-cert-lifecycle INSTANCE={instance} to reissue it."
    )


# Every role `status_rows` can resolve a certificate path for, mapped
# explicitly rather than an `if/else` falling through to `ca_cert` for
# anything that is not `ROLE_CLIENT`: that fallthrough would let a role
# added to `_INSPECTABLE_ROLES` without a matching entry here silently
# render CA expiry data under the new role's label instead of failing
# (code_review, E6-F1-S1-T2, WARN 4: fail-fast/OCP on an unrecognized role).
_ROLE_CERT_PATH_RESOLVERS: dict[str, Callable[[CertPaths], Path]] = {
    ROLE_CLIENT: lambda paths: paths.client_cert,
    ROLE_CA: lambda paths: paths.ca_cert,
}


def _role_cert_path(paths: CertPaths, role: str) -> Path:
    """The on-disk certificate path `role` resolves to for `paths.instance`.

    Raises:
        CertsError: `role` has no entry in `_ROLE_CERT_PATH_RESOLVERS`. Every
            caller today only ever passes a member of `_INSPECTABLE_ROLES`,
            which is exactly `_ROLE_CERT_PATH_RESOLVERS`'s key set, so this
            path is unreachable through the public API today; it exists so a
            future role added to one without the other fails loudly instead
            of resolving to the wrong certificate.
    """
    try:
        resolver = _ROLE_CERT_PATH_RESOLVERS[role]
    except KeyError as exc:
        raise CertsError(
            f"ERROR: unrecognized certificate role {role!r}\n"
            f"status_rows only knows how to resolve a path for "
            f"{sorted(_ROLE_CERT_PATH_RESOLVERS)}.\n"
            "This is a bug in devcontainer_config.certs; report it rather than "
            "adding a role to _INSPECTABLE_ROLES without a matching resolver here."
        ) from exc
    return resolver(paths)


def _instance_rows(
    paths: CertPaths, reference_time: datetime.datetime, warn_days: int
) -> tuple[CertStatusRow, ...]:
    """Every `CertStatusRow` for `paths.instance`, or raise for a partial material set.

    An instance directory with neither role's certificate present yet
    contributes no rows (a `create_ca` in progress, or an instance directory
    left over with nothing in it); one role present without the other is a
    partial material set and raises naming the missing role, rather than
    rendering a table with a blank row (Error Handling Contract).
    """
    present = {role: _role_cert_path(paths, role).is_file() for role in _INSPECTABLE_ROLES}
    if not any(present.values()):
        return ()
    for role, is_present in present.items():
        if not is_present:
            raise CertsError(_missing_role_message(paths.instance, role))
    rows = []
    for role in _INSPECTABLE_ROLES:
        cert_path = _role_cert_path(paths, role)
        expiry = not_after(cert_path)
        rows.append(
            CertStatusRow(
                instance=paths.instance,
                role=role,
                not_after=expiry,
                days=days_remaining(expiry, reference_time),
                status=classify(expiry, reference_time, warn_days),
            )
        )
    return tuple(rows)


def status_rows(
    root: Path, reference_time: datetime.datetime, warn_days: int
) -> tuple[CertStatusRow, ...]:
    """Every `CertStatusRow` for every instance found under `root` (`make cert-status`).

    Raises:
        CertsError: an unreadable or unparseable certificate file (`not_after`),
            or a partial material set for one instance (`_instance_rows`).
    """
    rows: list[CertStatusRow] = []
    for instance in _discover_instances(root):
        paths = CertPaths(instance=instance, root=root)
        rows.extend(_instance_rows(paths, reference_time, warn_days))
    return tuple(rows)


def _format_row(row: CertStatusRow) -> str:
    line = (
        f"{row.instance:<11}{row.role:<9}{row.not_after.strftime('%Y-%m-%d'):<10}"
        f"{row.days:>8}  {row.status}"
    )
    if row.status == STATUS_RENEW:
        line += f"   {RENEW_INVOCATION_TEMPLATE.format(instance=row.instance)}"
    return line


def render_report(rows: Sequence[CertStatusRow]) -> str:
    """The full `make cert-status` report text for `rows` (spec Section 4.1.2), header included.

    An empty `rows` is not an error (AC-FUNC-006): the header still prints,
    followed by a line directing the operator to `gd-env-setup-remote`
    rather than a bare header with nothing underneath it.
    """
    lines = [_REPORT_HEADER]
    if not rows:
        lines.append(_NO_CERTIFICATES_LINE)
    else:
        lines.extend(_format_row(row) for row in rows)
    return "\n".join(lines)


def _current_time() -> datetime.datetime:
    """The real clock, read through this one seam so `main`'s `status` command is testable.

    Every other function above takes its reference time as a parameter
    (AC-FUNC-001); this is the one place `main` itself resolves "now" from,
    so a test can monkeypatch this single function to force any `status_rows`
    outcome deterministically instead of waiting real time for a certificate
    to age.
    """
    return datetime.datetime.now(datetime.UTC)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="devcontainer_config.certs",
        description=(
            "Certificate authority creation, server and client issuance, and the "
            "publication set an instance's material is allowed to publish "
            "(spec Section 4.5)."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_instance_arguments(subparser: argparse.ArgumentParser) -> None:
        subparser.add_argument("--instance", required=True, help="The remote instance name.")
        subparser.add_argument(
            "--root",
            type=Path,
            default=DEFAULT_CERTS_ROOT,
            help=f"Certificate material root (default: {DEFAULT_CERTS_ROOT}).",
        )

    create_ca_parser = subparsers.add_parser(
        "create-ca", help="Create the certificate authority for one instance."
    )
    add_instance_arguments(create_ca_parser)
    create_ca_parser.set_defaults(handler=_run_create_ca)

    issue_server_parser = subparsers.add_parser(
        "issue-server", help="Issue the server certificate (never persisted)."
    )
    add_instance_arguments(issue_server_parser)
    issue_server_parser.set_defaults(handler=_run_issue_server)

    issue_client_parser = subparsers.add_parser(
        "issue-client", help="Issue the client certificate and key."
    )
    add_instance_arguments(issue_client_parser)
    issue_client_parser.set_defaults(handler=_run_issue_client)

    publish_parser = subparsers.add_parser(
        "publish",
        help=(
            "Publish this instance's TLS material to its engine kind's store "
            "(Parameter Store for ec2, the instance's Key Vault for azure, "
            "Secret Manager for gcp)."
        ),
    )
    add_instance_arguments(publish_parser)
    publish_parser.add_argument(
        "--region",
        default=None,
        help="The AWS region (default: resolved from the environment by the aws CLI).",
    )
    publish_parser.set_defaults(handler=_run_publish)

    publication_set_parser = subparsers.add_parser(
        "publication-set",
        help=(
            "Print the store entries this instance's material may publish, "
            "selected by its engine kind."
        ),
    )
    publication_set_parser.add_argument(
        "--instance", required=True, help="The remote instance name."
    )
    publication_set_parser.set_defaults(handler=_run_publication_set)

    status_parser = subparsers.add_parser(
        "status",
        help="Report client and CA certificate expiry for every instance (make cert-status).",
    )
    status_parser.add_argument(
        "--root",
        type=Path,
        default=DEFAULT_CERTS_ROOT,
        help=f"Certificate material root (default: {DEFAULT_CERTS_ROOT}).",
    )
    status_parser.set_defaults(handler=_run_status)

    return parser


def _run_create_ca(args: argparse.Namespace) -> int:
    paths = CertPaths(instance=args.instance, root=args.root)
    create_ca(paths)
    print(f"Created CA for {args.instance!r} at {paths.ca_cert}.")
    return 0


def _run_issue_server(args: argparse.Namespace) -> int:
    paths = CertPaths(instance=args.instance, root=args.root)
    issue_server(paths)
    print(
        f"Issued server certificate for {args.instance!r} "
        f"(SANs: {SERVER_SAN}; extendedKeyUsage: {SERVER_EXTENDED_KEY_USAGE})."
    )
    return 0


def _run_issue_client(args: argparse.Namespace) -> int:
    paths = CertPaths(instance=args.instance, root=args.root)
    issue_client(paths)
    print(f"Issued client certificate for {args.instance!r} at {paths.client_cert}.")
    return 0


def _run_publish(args: argparse.Namespace) -> int:
    """Issue server material and publish it to the instance kind's store.

    The store is selected by the instance's engine kind
    (`instances.read_kind`): the ec2 kind publishes to Parameter Store as it
    always has, the azure kind publishes to the Key Vault named in its
    engine address record (`instances.engine_vault_name`, whose refusal
    names the deploy that writes the record), and the gcp kind publishes to
    Secret Manager in the project the record names, under the prefix the
    record recorded -- the same deploy remedy for a missing or half-written
    record. An ssh engine is refused by `certs.sh` before this handler runs
    and reaches no store from here.
    """
    paths = CertPaths(instance=args.instance, root=args.root)
    try:
        root = repo.find_root(Path.cwd())
        kind = instances.read_kind(root, args.instance)
    except (repo.RepoError, instances.InstancesError) as exc:
        raise CertsError(str(exc)) from exc
    if kind == instances.ENGINE_KIND_AZURE:
        try:
            vault_name = instances.engine_vault_name(root, args.instance)
        except instances.InstancesError as exc:
            raise CertsError(str(exc)) from exc
        for secret_name in publish_to_key_vault(
            paths, KeyVaultClient(subprocess_runner, vault_name=vault_name)
        ):
            print(f"published {secret_name}")
        return 0
    if kind == instances.ENGINE_KIND_GCP:
        try:
            project_id = instances.gcp_project_id(root, args.instance)
            prefix = instances.gcp_secrets_prefix(root, args.instance)
        except instances.InstancesError as exc:
            raise CertsError(str(exc)) from exc
        written = publish_to_secret_manager(
            paths, SecretManagerClient(subprocess_runner, project_id=project_id), prefix=prefix
        )
        for secret_id in written:
            print(f"published {secret_id}")
        return 0
    client = ParameterStoreClient(subprocess_runner, region=args.region)
    for path in publish(paths, client):
        print(f"published {path}")
    return 0


def _run_publication_set(args: argparse.Namespace) -> int:
    """Print the publication set the instance's kind publishes to.

    The ec2 set is the Parameter Store entries spec Section 5.3 names; the
    azure set is the Key Vault secret names, azure-marked so the operator
    can tell at a glance which store the name addresses; the gcp set is the
    Secret Manager secret ids -- the three TLS secrets plus the checksum
    secret -- gcp-marked the same way.
    """
    try:
        root = repo.find_root(Path.cwd())
        kind = instances.read_kind(root, args.instance)
    except (repo.RepoError, instances.InstancesError) as exc:
        raise CertsError(str(exc)) from exc
    if kind == instances.ENGINE_KIND_AZURE:
        for secret_name in azure_publication_set(args.instance):
            print(f"{secret_name} {AZURE_KEYVAULT_MARK}")
        return 0
    if kind == instances.ENGINE_KIND_GCP:
        for secret_id in gcp_publication_set(args.instance):
            print(f"{secret_id} {GCP_SECRET_MANAGER_MARK}")
        return 0
    for entry in publication_set(args.instance):
        print(f"{entry.parameter_path} {entry.parameter_type}")
    return 0


def _run_status(args: argparse.Namespace) -> int:
    """`make cert-status`'s own command: render the report and return its exit code.

    `CERT_WARN_DAYS` is resolved before `status_rows` computes a single row
    (Error Handling Contract: a bad value fails before any row renders), and
    `_current_time` is the one clock read in this whole command, so a test
    can force any outcome by monkeypatching that single function.
    """
    warn_days = _resolve_cert_warn_days()
    rows = status_rows(args.root, _current_time(), warn_days)
    print(render_report(rows))
    return 1 if any(row.status == STATUS_EXPIRED for row in rows) else 0


def main(argv: Sequence[str] | None = None) -> int:
    """Parse `argv`, run the selected command, and return an exit code.

    Every handler raises `CertsError` on a real failure instead of exiting
    itself; this is the one place that exception becomes a non-zero exit
    code, printed with an `ERROR:` prefix to stderr, never a stack trace.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except CertsError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
