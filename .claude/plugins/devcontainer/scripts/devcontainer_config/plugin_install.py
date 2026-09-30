"""The pinned session-manager-plugin install metadata (spec Section 6.1, E5-F3-S1-T2).

Section 6.1 originally named a registry `features:` entry
(`ghcr.io/devcontainers-extra/features/aws-ssm-session-manager-plugin:1`) as the
install mechanism for the AWS session-manager-plugin. That Feature has never
existed in any registry (verified independently three times: by an executor,
by the blocker-resolver, and by the orchestrator's own `docker manifest
inspect` and `collection-index.yml` search). The operator ruled on
2026-09-01 (signed commit `9a38c703adda210e27c2c34607cb1a09a4d63378`) that the
fallback mechanism is a `.devcontainer/Dockerfile` build step: fetch the
AWS-published package over TLS, verify its detached signature against a
pinned key fingerprint, and fail the build on a verification error. That
decision accepted a single shell `RUN` step as a deliberate, one-time
exception to the no-shell-in-build-logic rule.

This module is the pinned side of that mechanism, not the mechanism itself:
the actual download, signature verification and installation are shell
inside the Dockerfile (the operator's sanctioned exception), but the values
that make that shell trustworthy -- the signing key's identity and the
package coordinates -- are declared here, once, so the Dockerfile cannot
silently drift from them and a test can assert the Dockerfile still declares
what this module says it should.

Nothing here pins a package checksum. AWS publishes the plugin at a
"latest" S3 path with no version segment; a checksum pinned against today's
build would break every future rebuild the moment AWS ships a new plugin
version at that same URL, which is the download surface this project relies
on for pointing every developer at the same S3 path forever. The signing
key's fingerprint does not change between plugin releases, so pinning the
key, and verifying the plugin's detached signature against it on every
build, is what actually gives every developer's build immutable-artifact
provenance (AC-FUNC-002) without freezing the plugin version.

The signing key material below (`SIGNING_KEY_ID`, `SIGNING_KEY_FINGERPRINT`)
is copied from AWS's own published instructions for verifying the
session-manager-plugin package signature, reproduced verbatim rather than
generated, so this module is a citation of AWS's key identity rather than an
independent claim about it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

# The GPG key AWS signs every session-manager-plugin release with, for every
# supported architecture and package format. It does not change between
# plugin releases, which is what makes pinning it (rather than a per-release
# checksum) the correct control for a "latest" download URL.
SIGNING_KEY_ID: str = "2C4D4AFF6F6757EE"

# The full fingerprint the short key ID above is derived from. Comparing the
# fingerprint after import is a second, independent check beyond `gpg
# --verify` succeeding: it also catches an embedded public-key block that
# was corrupted or replaced by an edit to the Dockerfile, before that edit
# ever gets a chance to authenticate a package. Kept in the same
# space-free, upper-case form `gpg --with-colons` reports it in, so a
# comparison against the Dockerfile's own extracted value needs no
# reformatting on either side.
SIGNING_KEY_FINGERPRINT: str = "7959637124CE093AD501D47A2C4D4AFF6F6757EE"

# AWS's own S3 path for the plugin, always resolving to whatever AWS
# currently calls "latest" (no version segment). This project never pins a
# plugin version; the signature check against SIGNING_KEY_FINGERPRINT is the
# control, not a frozen URL.
DOWNLOAD_BASE_URL: str = "https://s3.amazonaws.com/session-manager-downloads/plugin/latest"

# The installer package and its detached-signature sibling, named exactly as
# AWS publishes them alongside each architecture directory below.
PACKAGE_FILENAME: str = "session-manager-plugin.deb"
SIGNATURE_SUFFIX: str = ".sig"

# Every `dpkg --print-architecture` value the Dockerfile's install step
# switches on, mapped to the S3 directory AWS publishes that architecture's
# Debian package under. Both keys must be handled explicitly in the
# Dockerfile: a build that only covered one architecture would install
# nothing (and give no diagnostic) on a developer's machine of the other
# kind.
ARCHITECTURE_PACKAGE_DIRECTORIES: Mapping[str, str] = {
    "amd64": "ubuntu_64bit",
    "arm64": "ubuntu_arm64",
}

# The heredoc terminator the Dockerfile's install RUN step uses. Naming it
# here, once, is what lets `_install_block` locate exactly that step's body
# in the Dockerfile text instead of assuming the plugin install is the only
# RUN instruction present.
DOCKERFILE_INSTALL_BLOCK_MARKER: str = "INSTALL_SESSION_MANAGER_PLUGIN"

# The public-key block AWS publishes for verifying this plugin. Its presence
# in the Dockerfile is what makes the fingerprint check meaningful: the
# fingerprint above only authenticates whatever key material the build
# actually imported, so that key material must be embedded in the reviewed
# Dockerfile itself, never fetched from an unauthenticated source at build
# time (an attacker able to intercept an unauthenticated key fetch could
# substitute their own key alongside their own package).
PUBLIC_KEY_BLOCK_MARKER: str = "BEGIN PGP PUBLIC KEY BLOCK"

# Shell verbs the Dockerfile's install step must show, in this order, for
# the signature check to be load-bearing rather than decorative:
# verification before installation, and no silent continuation past a
# non-zero exit anywhere in the block.
_GPG_VERIFY_MARKER: str = "gpg --batch --verify"
_DPKG_INSTALL_MARKER: str = "dpkg --install"
_FAIL_FAST_SHELL_MARKER: str = "set -euo pipefail"
_CURL_FAIL_FLAG: str = "--fail"
_SILENT_CONTINUATION_MARKERS: tuple[str, ...] = ("|| true", "; true")


@dataclass(frozen=True)
class Finding:
    """One way the Dockerfile's install step could fail to match this module's pins.

    Shaped like `devcontainer_config.verify.Finding` (check/found/prevents/
    remedy), a discipline this module keeps as an independent declaration
    rather than an import: `verify.Finding` is scoped to the private-file
    verification domain (spec Section 4.5's "verify" concern) and importing
    it here would couple two unrelated domains for the sake of four
    identical field names.
    """

    check: str
    found: str
    prevents: str
    remedy: str


def _install_block(dockerfile_text: str) -> str | None:
    """The install RUN step's body, or None if the marker is missing entirely.

    Every other check in this module operates on this narrowed text rather
    than the whole Dockerfile, so a coincidental match elsewhere in the file
    (for example, in a comment above an unrelated RUN step) can never stand
    in for the real install block.
    """
    start = dockerfile_text.find(DOCKERFILE_INSTALL_BLOCK_MARKER)
    if start == -1:
        return None
    # The block's *body* is everything between the two occurrences of the
    # marker (the heredoc's opening `<<'MARKER'` and its closing line); the
    # first occurrence itself is the opening delimiter, not part of the body.
    body_start = start + len(DOCKERFILE_INSTALL_BLOCK_MARKER)
    end = dockerfile_text.find(DOCKERFILE_INSTALL_BLOCK_MARKER, body_start)
    if end == -1:
        return dockerfile_text[body_start:]
    return dockerfile_text[body_start:end]


def _missing_block_finding() -> Finding:
    return Finding(
        check=f"install step present: {DOCKERFILE_INSTALL_BLOCK_MARKER}",
        found=f"no '{DOCKERFILE_INSTALL_BLOCK_MARKER}' heredoc block in the Dockerfile",
        prevents="the session-manager-plugin from ever being installed into the image",
        remedy=(
            f"Add a RUN <<'{DOCKERFILE_INSTALL_BLOCK_MARKER}' ... "
            f"{DOCKERFILE_INSTALL_BLOCK_MARKER} step to .devcontainer/Dockerfile."
        ),
    )


def _substring_finding(
    block: str, needle: str, check: str, prevents: str, remedy: str
) -> Finding | None:
    """A single missing-substring check, the shape every metadata check below shares."""
    if needle in block:
        return None
    return Finding(
        check=check,
        found=f"'{needle}' not found in the install step",
        prevents=prevents,
        remedy=remedy,
    )


def _key_material_findings(block: str) -> list[Finding]:
    """The signing key's identity must be declared, and its material embedded."""
    findings: list[Finding] = []
    key_id_finding = _substring_finding(
        block,
        SIGNING_KEY_ID,
        check="signing key ID declared",
        prevents="a build-time key import from being tied to the key AWS actually signs with",
        remedy=f"Reference gpg key ID {SIGNING_KEY_ID} in the install step.",
    )
    if key_id_finding:
        findings.append(key_id_finding)
    fingerprint_finding = _substring_finding(
        block,
        SIGNING_KEY_FINGERPRINT,
        check="signing key fingerprint pinned",
        prevents="AC-FUNC-002's pinned-fingerprint verification from ever running",
        remedy=(
            f"Compare the imported key's fingerprint against {SIGNING_KEY_FINGERPRINT} "
            "and fail the build on mismatch."
        ),
    )
    if fingerprint_finding:
        findings.append(fingerprint_finding)
    public_key_finding = _substring_finding(
        block,
        PUBLIC_KEY_BLOCK_MARKER,
        check="public key material embedded",
        prevents=(
            "the fingerprint pin from meaning anything: without the key material embedded "
            "in the reviewed Dockerfile, a build-time key fetch could be substituted by an attacker"
        ),
        remedy="Embed AWS's published PGP public key block directly in the install step.",
    )
    if public_key_finding:
        findings.append(public_key_finding)
    return findings


def _download_findings(block: str) -> list[Finding]:
    """The package must be fetched from the pinned host, for every known architecture, fail-fast."""
    findings: list[Finding] = []
    base_url_finding = _substring_finding(
        block,
        DOWNLOAD_BASE_URL,
        check="download host pinned",
        prevents="the package from being fetched from a host nobody reviewed",
        remedy=f"Fetch the package from {DOWNLOAD_BASE_URL}.",
    )
    if base_url_finding:
        findings.append(base_url_finding)
    filename_finding = _substring_finding(
        block,
        PACKAGE_FILENAME,
        check="package filename declared",
        prevents="the install step from naming the artifact it downloads",
        remedy=f"Reference the package filename {PACKAGE_FILENAME}.",
    )
    if filename_finding:
        findings.append(filename_finding)
    signature_finding = _substring_finding(
        block,
        SIGNATURE_SUFFIX,
        check="detached signature file referenced",
        prevents="AC-FUNC-002's signature verification from having anything to verify against",
        remedy=f"Fetch the sibling {SIGNATURE_SUFFIX} file alongside the package.",
    )
    if signature_finding:
        findings.append(signature_finding)
    for dpkg_arch, package_dir in ARCHITECTURE_PACKAGE_DIRECTORIES.items():
        arch_finding = _substring_finding(
            block,
            package_dir,
            check=f"architecture directory declared: {dpkg_arch}",
            prevents=f"a build on a dpkg {dpkg_arch!r} host from finding any package to install",
            remedy=(
                f"Select the {package_dir!r} S3 directory when dpkg reports "
                f"architecture {dpkg_arch!r}."
            ),
        )
        if arch_finding:
            findings.append(arch_finding)
    fail_flag_count = block.count(_CURL_FAIL_FLAG)
    if fail_flag_count < 2:
        findings.append(
            Finding(
                check="fetches fail fast on a non-2xx response",
                found=(
                    f"'{_CURL_FAIL_FLAG}' appears {fail_flag_count} time(s) in the install "
                    "step, expected at least 2 (package fetch and signature fetch)"
                ),
                prevents=(
                    "a 404 or S3 error page from being silently written to the package "
                    "or signature file and treated as legitimate content"
                ),
                remedy=(
                    f"Pass {_CURL_FAIL_FLAG} to every curl invocation that downloads "
                    "the package or its signature."
                ),
            )
        )
    return findings


def _ordering_findings(block: str) -> list[Finding]:
    """Verification must happen, and must happen before installation."""
    verify_index = block.find(_GPG_VERIFY_MARKER)
    install_index = block.find(_DPKG_INSTALL_MARKER)
    findings: list[Finding] = []
    if verify_index == -1:
        findings.append(
            Finding(
                check="signature verification invoked",
                found=f"'{_GPG_VERIFY_MARKER}' not found in the install step",
                prevents="AC-FUNC-002's signature check from ever running at all",
                remedy=f"Invoke {_GPG_VERIFY_MARKER} against the downloaded signature and package.",
            )
        )
    if install_index == -1:
        findings.append(
            Finding(
                check="package installation invoked",
                found=f"'{_DPKG_INSTALL_MARKER}' not found in the install step",
                prevents=(
                    "the plugin from ever being installed even after a successful verification"
                ),
                remedy=f"Invoke {_DPKG_INSTALL_MARKER} against the downloaded package.",
            )
        )
    if verify_index != -1 and install_index != -1 and verify_index > install_index:
        findings.append(
            Finding(
                check="verification precedes installation",
                found="'dpkg --install' appears before 'gpg --batch --verify' in the install step",
                prevents=(
                    "an unverified binary from being rejected before dpkg installs it (AC-FUNC-002)"
                ),
                remedy="Move the gpg --batch --verify call before the dpkg --install call.",
            )
        )
    return findings


def _fail_fast_shell_findings(block: str) -> list[Finding]:
    """A verification failure must abort the whole step, not be swallowed."""
    findings: list[Finding] = []
    if _FAIL_FAST_SHELL_MARKER not in block:
        findings.append(
            Finding(
                check="shell aborts on the first failing command",
                found=f"'{_FAIL_FAST_SHELL_MARKER}' not found in the install step",
                prevents=(
                    "a failed download, import, fingerprint mismatch, or signature "
                    "verification from aborting the build (it would instead be silently ignored)"
                ),
                remedy=f"Start the install step's script body with {_FAIL_FAST_SHELL_MARKER}.",
            )
        )
    for marker in _SILENT_CONTINUATION_MARKERS:
        if marker in block:
            findings.append(
                Finding(
                    check="no silent continuation past a failed command",
                    found=f"'{marker}' found in the install step",
                    prevents="a failed verification or download from being treated as success",
                    remedy=f"Remove '{marker}' from the install step.",
                )
            )
    return findings


def dockerfile_findings(dockerfile_text: str) -> list[Finding]:
    """Every way `dockerfile_text` fails to declare a load-bearing, pinned plugin install.

    Returns every finding rather than the first, mirroring
    `devcontainer_config.verify.verify_all`'s discipline: one pass over the
    result names every gap the Dockerfile's install step has, rather than
    only the first one a caller happens to fix.
    """
    block = _install_block(dockerfile_text)
    if block is None:
        return [_missing_block_finding()]
    findings: list[Finding] = []
    findings.extend(_key_material_findings(block))
    findings.extend(_download_findings(block))
    findings.extend(_ordering_findings(block))
    findings.extend(_fail_fast_shell_findings(block))
    return findings


def postcreate_findings(postcreate_script_text: str) -> list[Finding]:
    """The plugin must never be installed by a runtime lifecycle script.

    AC-FUNC-003 requires the plugin to be present in the immutable image, not
    added by `postCreateCommand` after the container starts. This check is
    the negative half of that requirement: `dockerfile_findings` proves the
    Dockerfile installs it, this proves nothing outside the Dockerfile
    installs it a second time (or instead).
    """
    if "session-manager-plugin" not in postcreate_script_text:
        return []
    return [
        Finding(
            check="plugin not installed by a lifecycle script",
            found="'session-manager-plugin' appears in the postCreate script",
            prevents=(
                "the plugin from being present in the immutable image; a postCreateCommand install "
                "would instead re-run, and could silently fail, on every container start"
            ),
            remedy=(
                "Remove the session-manager-plugin reference from the postCreate script; "
                "install it in the Dockerfile only."
            ),
        )
    ]
