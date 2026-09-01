"""Certificate authority, server and client issuance (spec Section 4.5: "certs").

Section 4.5 splits `certs` into generation, inspection and expiry arithmetic.
This module implements generation only; inspection and the expiry arithmetic
behind `make cert-status` land in E6-F1-S1-T2. Section 13 decisions D3 and D4
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
"""

from __future__ import annotations

import argparse
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Sequence
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
    row for "Issue server certificate" and "Issue client certificate" names
    exactly this requirement; both functions below check it before shelling
    out to `openssl` at all. Beyond the existence check, each component is
    also checked for being a parseable PEM file (`_require_parseable_ca_component`),
    so a corrupt authority is rejected by name rather than reaching `openssl`
    as an opaque failure.
    """
    if not paths.ca_key.is_file() or not paths.ca_cert.is_file():
        raise CertsError(
            f"ERROR: no certificate authority exists for instance {paths.instance!r}\n"
            f"Expected {paths.ca_key} and {paths.ca_cert} to already exist.\n"
            "Run create_ca for this instance first."
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
    are moved into place with `os.replace` -- atomic on the same filesystem.
    A mid-issuance failure therefore leaves whatever was already at
    `key_out`/`cert_out` (nothing, for a first issuance; the previously
    valid material, for a rotation) completely untouched, rather than
    truncating or unlinking it first and only then attempting the
    `openssl` calls that can fail. The pre-round-3 implementation prepared
    `key_out` for writing (truncating any existing key) before the first
    `openssl` call and unlinked both `key_out` and `cert_out` on failure, so
    a failing `openssl` during `issue_client`'s rotation destroyed a
    previously valid, still-trusted client certificate and key with nothing
    to replace them -- stranding the developer without any client
    credential (code_review, this unit, round 1: WARN 5; round 3: WARN C).
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
        os.replace(issued_key, key_out)
        os.replace(issued_cert, cert_out)


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
    """Issue (or rotate) the client certificate and key at `paths.client_cert`/`paths.client_key`.

    Unlike `create_ca`, an existing client certificate is overwritten rather
    than rejected: `.claude/plugins/devcontainer/skills/certs/SKILL.md`'s own
    "Rotate client certificate" operation is this same call repeated, and the
    instance is left running throughout, which only works if reissuing never
    refuses on an existing file.

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
        "issue-client", help="Issue or rotate the client certificate and key."
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
