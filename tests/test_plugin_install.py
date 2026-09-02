"""Tests for devcontainer_config.plugin_install (spec Section 6.1, E5-F3-S1-T2).

The `devcontainer_config.plugin_install` import is deferred into function
bodies (via `_import_plugin_install`), the same discipline
`tests/test_certs.py` and `tests/test_repo.py` document: `plugin_install.py`
is new, added by this task, so a module-level `from devcontainer_config
import plugin_install` would fail COLLECTION for this whole file (pytest
exit code 2, no test outcome recorded) instead of failing the one test that
needs it, for the real reason: the module is missing. `_plugin_install_module_path`
reads the file by path for the same reason.

`test_dockerfile_declares_a_load_bearing_session_manager_plugin_install` is
this file's pin against the real `.devcontainer/Dockerfile`, and it is the
one test this unit's RED/GREEN cycle is built around (AC-TEST-001,
AC-TEST-002). Before this unit lands, that file has no install step for
`session-manager-plugin` at all, which is the source-level fact behind the
observed `session-manager-plugin --version` exit 127 in the current
container: the RUN step that would install the binary does not exist, so
the binary does not exist. After this unit lands, every check
`plugin_install.dockerfile_findings` runs passes against the real file,
which is the source-level fact behind the observed successful `--version`
in the rebuilt image. This suite never builds or runs a container itself
(Makefile's `test:` target is hermetic: no docker, no AWS, no network,
AC-10.14); the actual `docker build` before-and-after evidence for
AC-TEST-001/AC-TEST-002, and the deliberately-corrupted-signature build
failure for AC-TEST-003, are isolated `docker build` runs recorded in this
unit's `## TDD Cycle Log`, not pytest tests.

Every other test below proves one of `dockerfile_findings`'s or
`postcreate_findings`'s checks is load-bearing rather than decorative
(AC-TEST-003's "load-bearing, not decorative" standard applied to the
static checker itself, not only to the Dockerfile's own `gpg --verify`):
each removes or mutates exactly one control from the real Dockerfile's
install step (derived from the live file via `.replace`, never a
hand-duplicated copy of the block, so these tests cannot silently drift
from what the Dockerfile actually contains) and asserts the specific
finding that control's absence should produce actually appears. A test that
only asserted `dockerfile_findings(real_text) == []` would stay green even
if every individual check inside `dockerfile_findings` were deleted down to
`return []`; these parametrized cases are what keeps that from happening
unnoticed.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from types import ModuleType

import pytest
from conftest import _postcreate_text
from devcontainer_config import repo


def _import_plugin_install() -> ModuleType:
    """Import devcontainer_config.plugin_install from inside a function body.

    See the module docstring for why this is not a module-level import.
    """
    return importlib.import_module("devcontainer_config.plugin_install")


def _plugin_install_module_path() -> Path:
    return (
        Path(__file__).resolve().parents[1]
        / ".claude"
        / "plugins"
        / "devcontainer"
        / "scripts"
        / "devcontainer_config"
        / "plugin_install.py"
    )


def _dockerfile_path() -> Path:
    root = repo.find_root(Path(__file__).resolve().parent)
    return root / ".devcontainer" / "Dockerfile"


def _dockerfile_text() -> str:
    """`.devcontainer/Dockerfile`, read fresh for every call.

    Not cached at module scope, the same discipline `conftest._postcreate_text`
    documents: a cached value would let one test's assertion about the file
    leak into another test's failure message instead of each test reading
    the file it is actually asserting about.
    """
    return _dockerfile_path().read_text(encoding="utf-8")


def _devcontainer_json_document() -> dict[str, object]:
    root = repo.find_root(Path(__file__).resolve().parent)
    text = (root / ".devcontainer" / "devcontainer.json").read_text(encoding="utf-8")
    return json.loads(text)


# ---------------------------------------------------------------------------
# AC-TEST-001 / AC-TEST-002: the RED/GREEN pin against the real Dockerfile.
# ---------------------------------------------------------------------------


def test_dockerfile_declares_a_load_bearing_session_manager_plugin_install() -> None:
    plugin_install = _import_plugin_install()
    findings = plugin_install.dockerfile_findings(_dockerfile_text())
    assert findings == [], [finding.check for finding in findings]


# ---------------------------------------------------------------------------
# Each check below is proven load-bearing by removing exactly the control it
# names from the real Dockerfile's install step and confirming the matching
# finding appears. The needle/expected-check pairs are literal strings, not
# `plugin_install` attribute references, so this parametrize table can be
# collected before the module under test exists (the same reason
# `tests/test_certs.py`'s parametrize tables never reference `certs.`
# attributes in a decorator).
# ---------------------------------------------------------------------------
_REMOVED_CONTROL_CASES: tuple[tuple[str, str], ...] = (
    ("2C4D4AFF6F6757EE", "signing key ID declared"),
    ("7959637124CE093AD501D47A2C4D4AFF6F6757EE", "signing key fingerprint pinned"),
    ("BEGIN PGP PUBLIC KEY BLOCK", "public key material embedded"),
    (
        "https://s3.amazonaws.com/session-manager-downloads/plugin/latest",
        "download host pinned",
    ),
    ("session-manager-plugin.deb", "package filename declared"),
    ("ubuntu_64bit", "architecture directory declared: amd64"),
    ("ubuntu_arm64", "architecture directory declared: arm64"),
    ("gpg --batch --verify", "signature verification invoked"),
    ("dpkg --install", "package installation invoked"),
    ("set -euo pipefail", "shell aborts on the first failing command"),
)


@pytest.mark.parametrize(("needle", "expected_check"), _REMOVED_CONTROL_CASES)
def test_dockerfile_findings_flags_a_removed_control(needle: str, expected_check: str) -> None:
    plugin_install = _import_plugin_install()
    mutated = _dockerfile_text().replace(needle, "")
    findings = plugin_install.dockerfile_findings(mutated)
    assert any(finding.check == expected_check for finding in findings), [
        finding.check for finding in findings
    ]


def test_dockerfile_findings_flags_missing_install_block() -> None:
    plugin_install = _import_plugin_install()
    mutated = _dockerfile_text().replace("INSTALL_SESSION_MANAGER_PLUGIN", "UNRELATED_MARKER")
    findings = plugin_install.dockerfile_findings(mutated)
    assert len(findings) == 1
    assert findings[0].check == (
        f"install step present: {plugin_install.DOCKERFILE_INSTALL_BLOCK_MARKER}"
    )


def test_dockerfile_findings_flags_verification_after_installation() -> None:
    """A Dockerfile that installs before it verifies must be rejected (AC-FUNC-002)."""
    plugin_install = _import_plugin_install()
    marker = plugin_install.DOCKERFILE_INSTALL_BLOCK_MARKER
    reordered = (
        f"RUN <<'{marker}'\n"
        "set -euo pipefail\n"
        'dpkg --install "$workdir/session-manager-plugin.deb"\n'
        'gpg --batch --verify "$workdir/session-manager-plugin.deb.sig" '
        '"$workdir/session-manager-plugin.deb"\n'
        f"{marker}\n"
    )
    findings = plugin_install.dockerfile_findings(reordered)
    assert any(finding.check == "verification precedes installation" for finding in findings), [
        finding.check for finding in findings
    ]


def test_dockerfile_findings_flags_silent_continuation_past_a_failure() -> None:
    plugin_install = _import_plugin_install()
    verify_call = (
        'gpg --batch --verify "${workdir}/session-manager-plugin.deb.sig" '
        '"${workdir}/session-manager-plugin.deb"'
    )
    mutated = _dockerfile_text().replace(verify_call, f"{verify_call} || true")
    findings = plugin_install.dockerfile_findings(mutated)
    assert any(
        finding.check == "no silent continuation past a failed command" for finding in findings
    ), [finding.check for finding in findings]


def test_dockerfile_findings_flags_a_fetch_missing_fail_flag() -> None:
    plugin_install = _import_plugin_install()
    mutated = _dockerfile_text().replace("--fail --show-error", "--show-error", 1)
    findings = plugin_install.dockerfile_findings(mutated)
    assert any(
        finding.check == "fetches fail fast on a non-2xx response" for finding in findings
    ), [finding.check for finding in findings]


def test_install_block_handles_an_unterminated_heredoc() -> None:
    """A truncated heredoc (opening marker with no closing line) still gets analyzed.

    `_install_block` falls back to "everything after the opening marker"
    rather than raising, so a malformed Dockerfile is reported through the
    normal findings list (missing controls) instead of crashing the check
    that is supposed to catch exactly this kind of mistake.
    """
    plugin_install = _import_plugin_install()
    marker = plugin_install.DOCKERFILE_INSTALL_BLOCK_MARKER
    truncated = f"RUN <<'{marker}'\nset -euo pipefail\n"
    findings = plugin_install.dockerfile_findings(truncated)
    assert any(finding.check == "signature verification invoked" for finding in findings), [
        finding.check for finding in findings
    ]


# ---------------------------------------------------------------------------
# AC-FUNC-003: build-time only, never a runtime lifecycle script.
# ---------------------------------------------------------------------------


def test_postcreate_script_does_not_install_the_plugin() -> None:
    plugin_install = _import_plugin_install()
    findings = plugin_install.postcreate_findings(_postcreate_text())
    assert findings == []


def test_postcreate_findings_flags_a_lifecycle_install() -> None:
    plugin_install = _import_plugin_install()
    findings = plugin_install.postcreate_findings("dpkg -i /tmp/session-manager-plugin.deb\n")
    assert len(findings) == 1
    assert findings[0].check == "plugin not installed by a lifecycle script"


# ---------------------------------------------------------------------------
# AC-FUNC-004: no unmaintained third-party Feature is used as the source.
# ---------------------------------------------------------------------------


def test_devcontainer_json_declares_no_session_manager_feature() -> None:
    document = _devcontainer_json_document()
    features = document.get("features", {})
    assert isinstance(features, dict)
    offending = [
        key for key in features if "ssm" in key.lower() or "session-manager" in key.lower()
    ]
    assert offending == []


# ---------------------------------------------------------------------------
# Metadata sanity: the pinned constants this module's checks compare against.
# ---------------------------------------------------------------------------


def test_signing_key_fingerprint_matches_aws_published_value() -> None:
    """The fingerprint AWS's own instructions publish for this signing key.

    Reproduced from
    https://docs.aws.amazon.com/systems-manager/latest/userguide/install-plugin-linux-verify-signature.html
    (fetched and independently confirmed against the real detached signature
    during this unit's implementation) so a future accidental edit to the
    pinned constant is caught here rather than only inside a Dockerfile
    build.
    """
    plugin_install = _import_plugin_install()
    assert plugin_install.SIGNING_KEY_FINGERPRINT == "7959637124CE093AD501D47A2C4D4AFF6F6757EE"
    assert plugin_install.SIGNING_KEY_FINGERPRINT.endswith(plugin_install.SIGNING_KEY_ID)


def test_architecture_package_directories_cover_amd64_and_arm64() -> None:
    plugin_install = _import_plugin_install()
    assert plugin_install.ARCHITECTURE_PACKAGE_DIRECTORIES == {
        "amd64": "ubuntu_64bit",
        "arm64": "ubuntu_arm64",
    }


def test_download_base_url_has_no_pinned_version_segment() -> None:
    """The URL always resolves to AWS's "latest"; no checksum or version is pinned.

    AC-FUNC-002 pins the signing key, not the artifact: a checksum pinned
    against today's build would break every future rebuild the moment AWS
    publishes a new plugin version at this same URL.
    """
    plugin_install = _import_plugin_install()
    assert plugin_install.DOWNLOAD_BASE_URL.endswith("/latest")
    assert plugin_install.DOWNLOAD_BASE_URL.startswith("https://")


def test_plugin_install_source_declares_no_pinned_checksum() -> None:
    """The module's own source never asserts a fixed sha256 for the package.

    A hex-digest constant here would tempt a future Dockerfile edit to
    compare against it, silently reintroducing the moving-target breakage
    `test_download_base_url_has_no_pinned_version_segment` documents.
    """
    source = _plugin_install_module_path().read_text(encoding="utf-8")
    assert "sha256" not in source.lower()
