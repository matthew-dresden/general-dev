"""Certificate authority, server and client issuance (spec Section 4.5: "certs").

Section 4.5 splits `certs` into generation, inspection and expiry arithmetic.
This module implements both halves: generation, and the inspection and expiry
arithmetic behind `make cert-status`. Section 13 decisions D3 and D4
fix the design: there is no CA server, no step-ca, no Vault and no AWS
Private CA -- the authority is nothing more than a key pair and the `openssl`
binary, driven by `.claude/plugins/devcontainer/skills/certs/SKILL.md`. Every
`openssl` invocation below passes an argument list, never a shell string
(spec Section 3.4's dependency rule, AC-FUNC-006), and this module imports
nothing beyond the standard library and this project's own
`devcontainer_config.catalog`, reused below for its existing instance/scope
validation and Parameter Store constants rather than a second, independent
copy of either -- no third-party package is added.

`instance` reaches this module unvalidated from `--instance` on the CLI and,
per `.claude/plugins/devcontainer/skills/certs/SKILL.md`'s own instance
resolution, from `INSTANCE`/`DEFAULT_REMOTE_INSTANCE`; every path this module
composes from it -- `CertPaths.instance_dir` and the Parameter Store path
`publication_set` builds -- validates it first, through `_validate_instance`,
which delegates to `devcontainer_config.catalog._validate_scope`: the control
`catalog.py` already carries for the identical `/devcontainer/<scope>/` path
interpolation, reused here rather than reimplemented, so a name containing a
path separator or the empty string is rejected before it ever reaches a
filesystem path or a Parameter Store path (code_review, this unit, round 1:
BLOCKING 1). `PARAMETER_ROOT` and `SECURE_STRING_TYPE` below are likewise
`catalog.PATH_ROOT` and `catalog.SECURE_STRING_TYPE` themselves, not a second
literal declaration of either (round 1, WARN 4).

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

from devcontainer_config import catalog

# The one external command every function below shells out to. Every argv
# built in this module starts with this constant; nothing else here names an
# executable (mirrors `catalog.AWS_EXECUTABLE`'s own single-command
# guarantee, AC-FUNC-006).
OPENSSL_EXECUTABLE = "openssl"

# spec Section 5.5's material root. A parameter of `CertPaths`, not a literal
# baked into any function body, so the test suite can point an entire run at
# `tmp_path` instead of the developer's real `~/.docker/certs`.
DEFAULT_CERTS_ROOT = Path.home() / ".docker" / "certs"

CA_SUBDIR = "ca"
CA_KEY_FILENAME = "ca-key.pem"
CA_CERT_FILENAME = "ca.pem"
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
# (AC-FUNC-007) -- the literal `14` below is the only place this module ever
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
RENEW_INVOCATION_TEMPLATE = "/devcontainer:certs INSTANCE={instance}"
ROLE_CLIENT = "client"
ROLE_CA = "ca"
_INSPECTABLE_ROLES: tuple[str, ...] = (ROLE_CLIENT, ROLE_CA)

# spec Section 4.1.2's own column header, reproduced verbatim, and the line
# `make cert-status` prints in place of any rows when no instance has any
# certificate material yet (AC-FUNC-006's empty-inventory edge case).
_REPORT_HEADER = "INSTANCE   ROLE     EXPIRES       DAYS  STATUS"
_NO_CERTIFICATES_LINE = (
    "No certificates found. Run /devcontainer:setup-remote to issue the first instance's material."
)

# spec Section 5.3's Parameter Store layout for TLS material. `PARAMETER_ROOT`
# and `SECURE_STRING_TYPE` are `catalog.PATH_ROOT` and
# `catalog.SECURE_STRING_TYPE` themselves, not a second literal declaration of
# either (code_review, this unit, round 1: WARN 4) -- `catalog.py`'s own
# comment records that `PATH_ROOT` exists so the `/devcontainer/...` path
# shape has a single definition site. `STRING_TYPE` has no equivalent in
# `catalog`, which only ever writes `SecureString` secrets, so it stays a
# local literal.
PARAMETER_ROOT = catalog.PATH_ROOT
TLS_SEGMENT = "tls"
SECURE_STRING_TYPE = catalog.SECURE_STRING_TYPE
STRING_TYPE = "String"

# The only three names `publication_set` ever hands back (spec Section 5.3).
# `ca-key.pem` is deliberately absent: it is rejected by name in
# `_is_ca_private_key_request` before this mapping is even consulted, so its
# absence here is a second, independent enforcement of the same rule.
_PUBLISHABLE: dict[str, str] = {
    "server-key.pem": SECURE_STRING_TYPE,
    "server-cert.pem": SECURE_STRING_TYPE,
    CA_CERT_FILENAME: STRING_TYPE,
}

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
    """Reject a path-unsafe instance name before it reaches any path composition.

    Delegates the pattern check to `catalog._validate_scope`
    (`devcontainer_config.catalog`), the control already added there for the
    identical `/devcontainer/<scope>/` path interpolation (its own
    `InvalidScopeError` docstring records why), rather than a second,
    independent implementation of the same rule (code_review, this unit,
    round 1: BLOCKING 1). `instance` reaches this module unvalidated from
    `--instance` on the CLI and, per
    `.claude/plugins/devcontainer/skills/certs/SKILL.md`'s own instance
    resolution, from `INSTANCE`/`DEFAULT_REMOTE_INSTANCE`, so leaving it
    unvalidated would let a name carrying a path separator (or the empty
    string) compose a filesystem path outside `CertPaths.root`
    (`CertPaths.instance_dir`) or a Parameter Store path outside
    `PARAMETER_ROOT` (`publication_set`).

    Raises:
        CertsError: `instance` is empty, or contains a path separator or
            another character `catalog._validate_scope` rejects.
    """
    try:
        catalog._validate_scope(instance)
    except catalog.InvalidScopeError as exc:
        raise CertsError(
            f"ERROR: invalid instance name {instance!r}\n"
            "An instance name must be a non-empty path segment: letters, "
            "digits, hyphens and underscores only -- the identical rule "
            "devcontainer_config.catalog enforces for the same "
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
    mode `0600`/`0644` (matching `.claude/plugins/devcontainer/skills/certs/
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

    `.claude/plugins/devcontainer/skills/certs/SKILL.md`'s own precondition
    row for "Issue server certificate", "Issue client certificate" and
    "Rotate client certificate" names exactly this requirement; every
    function below that needs an authority checks it before shelling out to
    `openssl` at all, and none of them ever creates one implicitly on this
    failure -- a fresh CA would silently invalidate every certificate the
    previous one already signed, including the server certificate the
    running daemon is serving (this task's Error Handling Contract,
    AC-FUNC-005). The remedy names the exact skill invocation that creates
    an authority (`RENEW_INVOCATION_TEMPLATE`, the identical
    `/devcontainer:certs INSTANCE=<name>` spelling `## Expiry`'s own `RENEW`
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
        key_path = Path(tmp_dir) / "server-key.pem"
        cert_path = Path(tmp_dir) / "server-cert.pem"
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
    Section 4.5 and `.claude/plugins/devcontainer/skills/certs/SKILL.md`'s
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


def _unpublishable_name_message(name: str) -> str:
    allowed = ", ".join(_PUBLISHABLE)
    return (
        f"ERROR: {name!r} is not part of the publication set\n"
        f"Only {allowed} may be published for an instance (spec Section 5.3).\n"
        "Call publication_set() with no arguments for the default set."
    )


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
    _validate_instance(instance)
    names = tuple(_PUBLISHABLE) if filenames is None else tuple(filenames)
    entries = []
    for name in names:
        if _is_ca_private_key_request(name):
            raise CertsError(_ca_key_publication_denied_message(name))
        if name not in _PUBLISHABLE:
            raise CertsError(_unpublishable_name_message(name))
        entries.append(
            PublicationEntry(
                parameter_path=f"{PARAMETER_ROOT}/{instance}/{TLS_SEGMENT}/{name}",
                parameter_type=_PUBLISHABLE[name],
            )
        )
    return tuple(entries)


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
        "certificate does not -- a partial material set is never reported as a row.\n"
        f"Run /devcontainer:certs INSTANCE={instance} to reissue it."
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
    followed by a line directing the operator to `/devcontainer:setup-remote`
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

    publication_set_parser = subparsers.add_parser(
        "publication-set",
        help="Print the Parameter Store entries this instance's material may publish.",
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


def _run_publication_set(args: argparse.Namespace) -> int:
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
