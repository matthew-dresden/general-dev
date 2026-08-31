"""Shared answers-payload test builders for `tests/test_answers.py`.

`tests/test_answers.py` exercises `devcontainer_config.answers.FIELDS`
directly. Before this extraction it carried its own copy of
`_valid_aws_profile`, `_valid_local_payload` and `_valid_remote_payload`;
those builders now live here so any future consumer under `tests/` can
share them instead of re-declaring the same payload shape.

`_generated_dir` and `_example_root` are shared fixture-tree builders for
`tests/test_render.py` and `tests/test_verify.py`. Both files need an
identical, byte-for-byte-copied checkout of the three real `.example` files
this repository ships; before this extraction each file carried its own
copy of both functions, which risked silently diverging on the next change
to `repo.PRIVATE_FILES` or `repo.example_for`.

`_makefile_text`, `_make_variable` and `_resolve_make_refs` are shared
Makefile-parsing helpers for `tests/test_makefile_contract.py` and
`tests/test_ci_workflow.py`. Both suites need the same `NAME := value`
variable-resolution logic against the repository root `Makefile` --
`tests/test_makefile_contract.py`'s PREREQUISITES-row and guard-loop
assertions, and `tests/test_ci_workflow.py`'s AC-TEST-006 cross-check that
the CI `Install zsh` step and the Makefile's documented Linux package name
cannot drift apart. Before this extraction each file carried its own copy;
`_make_variable` and `_resolve_make_refs` were byte-identical and
`_makefile_text` was functionally identical, so a future change to the
Makefile's assignment syntax would have had to be fixed in two places, and
the AC-TEST-006 drift check itself could have silently mis-resolved while
`tests/test_makefile_contract.py` stayed green.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Any

from devcontainer_config import repo


def _synthetic_account_id() -> str:
    """A twelve-digit account-id-shaped value, generated at runtime.

    `devcontainer_config.answers`'s `account_id` field requires exactly this
    shape (twelve digits), which is, by construction, indistinguishable from
    a real AWS account id -- the repo's own secrets scanner
    (`devcontainer_config.secrets._ACCOUNT_ID_PATTERN`) keys on that
    identical shape. Rather than hardcoding a literal that looks like a real
    identifier, this generates a fresh one at runtime, the same way
    `tests/test_secrets.py._sample_account_id` already does for the same
    detector, so no AWS-shaped digit run ever appears in this file's source
    text.
    """
    return str(uuid.uuid4().int)[:12]


def _synthetic_instance_id(hex_length: int) -> str:
    """An `i-`-prefixed instance-id-shaped value with `hex_length` hex chars.

    `devcontainer_config.answers`'s `remote_instance_id` field accepts `i-`
    followed by exactly 8 or 17 lowercase hex characters, which is the exact
    shape `devcontainer_config.secrets._EC2_INSTANCE_ID_PATTERN` keys on.
    Generated at runtime for the same reason `_synthetic_account_id` is: no
    EC2-instance-id-shaped literal appears in source.
    """
    return "i-" + uuid.uuid4().hex[:hex_length]


_DEFAULT_ACCOUNT_ID: str = _synthetic_account_id()


def _valid_aws_profile(name: str = "dev", account_id: str = _DEFAULT_ACCOUNT_ID) -> dict[str, str]:
    """One well-formed aws_profiles entry, all seven sub-fields present.

    `sso_start_url` uses a non-AWS placeholder domain rather than
    `.devcontainer/aws-profile-map.json.example`'s
    `https://<your-sso-portal>.awsapps.com/start` shape: the secrets
    scanner's `sso-portal-url` detector
    (`devcontainer_config.secrets._SSO_PORTAL_URL_PATTERN`) matches any
    `https://<host>.awsapps.com/start` URL, so that shape cannot appear as a
    source literal here without tripping `lint-secrets` and, transitively,
    `make validate`. `devcontainer_config.answers`'s `sso_start_url` field
    only requires the generic `https://...` shape, not a specific host,
    which is what makes the placeholder domain an *acceptable substitute* --
    not the reason the substitution is required.
    """
    return {
        "name": name,
        "region": "us-east-1",
        "sso_start_url": "https://example-sso.identitycenter.example.com/start",
        "sso_region": "us-east-1",
        "account_name": "dev-account",
        "account_id": account_id,
        "role_name": "AdministratorAccess",
    }


def _valid_local_payload(
    *, aws_config_enabled: bool = False, host_proxy: bool = False
) -> dict[str, Any]:
    """A complete, valid local-backend answer set.

    The ten always-required answers (`devcontainer_config.answers.FIELDS`
    entries with `Requiredness.ALWAYS`), two of which -- `aws_config_enabled`
    and `host_proxy` -- are themselves always-required boolean toggles that
    additionally gate the branch-conditional `aws_profiles` (WHEN_AWS) and
    `host_proxy_url` (WHEN_PROXY) entries, mirroring the branching rule under
    test (AC-FUNC-002).
    """
    payload: dict[str, Any] = {
        "backend": "local",
        "developer_name": "Ada Lovelace",
        "git_user": "ada",
        "git_user_email": "ada@example.com",
        "git_provider_url": "github.com",
        "default_git_branch": "main",
        "template_name": "python-service",
        "aws_config_enabled": aws_config_enabled,
        "local_docker_context": "desktop-linux",
        "host_proxy": host_proxy,
    }
    if aws_config_enabled:
        payload["aws_profiles"] = [_valid_aws_profile()]
    if host_proxy:
        payload["host_proxy_url"] = "http://proxy.example.com:8080"
    return payload


def _valid_remote_payload() -> dict[str, Any]:
    """A complete, valid remote-backend answer set, including remote fields."""
    payload = _valid_local_payload()
    payload["backend"] = "remote"
    payload["remote_instance_id"] = _synthetic_instance_id(17)
    payload["remote_aws_region"] = "us-east-1"
    payload["remote_aws_profile"] = "default"
    payload["remote_ssh_key_path"] = "/home/dev/.ssh/example-key.pem"
    return payload


def _generated_dir(parent: Path, prefix: str) -> Path:
    """A `parent` subdirectory whose name is generated, never hard-coded."""
    generated = parent / f"{prefix}-{uuid.uuid4().hex}"
    generated.mkdir(parents=True)
    return generated


def _example_root(tmp_path: Path) -> Path:
    """A tmp_path checkout root holding copies of the three real `.example` files.

    Copied byte-for-byte from the real checkout (resolved via
    `repo.find_root`) rather than reconstructed as literal strings, so every
    assertion in this file runs against the examples this repository
    actually ships (AC-TEST-001).
    """
    root = _generated_dir(tmp_path, "checkout")
    real_root = repo.find_root(Path(__file__).resolve().parent)
    for relative in repo.PRIVATE_FILES:
        example_relative = repo.example_for(relative)
        source = real_root / example_relative
        destination = root / example_relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source.read_bytes())
    return root


def _makefile_text() -> str:
    """The repository root `Makefile`, read fresh for every call.

    Not cached at module scope: caching would let one test's assertion
    about the file's content leak into another's failure message instead of
    each test reading the file it is actually asserting about.
    """
    root = repo.find_root(Path(__file__).resolve().parent)
    return (root / "Makefile").read_text(encoding="utf-8")


def _make_variable(makefile_text: str, name: str) -> str:
    """The literal value assigned by a `name := value` line in the Makefile.

    Only supports the simple immediate-assignment form the Makefile uses for
    `TEST_PREREQUISITE_TOOLS` and each `TEST_INSTALL_HINT_<tool>` -- the only
    form `_resolve_make_refs` needs to look up.
    """
    match = re.search(r"^" + re.escape(name) + r"\s*:=\s*(.+)$", makefile_text, re.MULTILINE)
    assert match is not None, f"no {name!r} variable assignment found in Makefile"
    return match.group(1).strip()


def _resolve_make_refs(makefile_text: str, text: str) -> str:
    """`text` with every `$(NAME)` token replaced by `NAME`'s Makefile value.

    The Makefile's PREREQUISITES row and its `test:` recipe guard, and CI's
    `Install zsh` step documentation cross-check, all render their
    install-command wording from shared `TEST_INSTALL_HINT_<tool>` Make
    variables (single-sourced in the Makefile, not typed out twice), so text
    captured out of the Makefile's source carries the literal `$(...)` token
    rather than the value. This is a one-level, test-only substitution
    against that source -- not a general Make evaluator -- so a caller still
    asserts on real install-command text, and a reference that came to name
    a different variable would still be caught.
    """

    def _replace(match: re.Match[str]) -> str:
        return _make_variable(makefile_text, match.group(1))

    return re.sub(r"\$\(([A-Za-z0-9_]+)\)", _replace, text)
