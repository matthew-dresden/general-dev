"""Shared answers-payload test builders for `tests/test_answers.py`.

`tests/test_answers.py` exercises `devcontainer_config.answers.FIELDS`
directly. Before this extraction it carried its own copy of
`_valid_aws_profile`, `_valid_local_payload` and `_valid_remote_payload`;
those builders now live here so any future consumer under `tests/` can
share them instead of re-declaring the same payload shape.
"""

from __future__ import annotations

import uuid
from typing import Any


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
