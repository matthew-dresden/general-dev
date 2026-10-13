"""The azure engine kind's secrets-and-certificates shell surface.

The Python half of slice 3 (`certs.py`'s Key Vault client, the kind-selected
publication, `transport_azure.install-material`) carries its own coverage in
`tests/test_certs.py` and `tests/test_transport_azure.py`. This file pins the
SHELL half, the same static-text approach `tests/test_engine_container_path.py`
and `tests/test_container_verify.py` take for the other kinds' branches, plus
one behavioral run of `push-secrets.sh`'s azure arm end to end.

The invariants under test, per entry point:

- `certs.sh` refuses ssh engines once, after resolution, with the same
  message it has always carried; azure keeps all four subcommands: publish
  runs with no region, no profile and no AWS session (the az CLI is
  ambiently logged in), and install reaches the azure transport with the
  daemon user alone; the ec2 arms keep their byte-identical lines.
- `push-secrets.sh`'s azure branch sits between the ssh branch (which still
  exits first) and the EC2 branch (whose `SSM_PREFIX` dereference must never
  see an azure run, because the resolver's azure block carries no
  `PARAMETER_PREFIX` line), publishes the shared transform's output as the
  `shell-env` and `profile-map` secrets through `--file`, never argv, and
  names `az login` on failure.
- `container.sh` clears `DEVCONTAINER_SSM_PREFIX` for azure where ssh clears
  it, exports the azure pair the build and the container read, and the build
  override's containerEnv patch branches on the resolved kind without
  touching the ec2 patch's byte-identical text; the staleness probe reads
  Key Vault's `attributes.updated` into the same comparison the ec2 arm has
  always run.
- `postcreate-wrapper.sh` selects the Key Vault bootstrap mode on
  `DEVCONTAINER_ENGINE_KIND=azure` before the aws arm, fetches through the
  managed-identity endpoint with the same umask and remedy discipline, and
  the aws arm keeps its own IMDS variables untouched.

The behavioral run drives a stand-in resolver (answering both
`resolve-instance` and `secrets-vault`) and an `az` stub that records every
argv and captures each `--file` body: the transform that lands in the vault
is compared against `write_config_payload`'s own render, so the published
bytes are pinned to the shared transform rather than to a copy of it.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import _function_body

_REPO_ROOT = Path(__file__).resolve().parent.parent
_RD_DIR = _REPO_ROOT / ".devcontainer" / "remote-docker"
_CERTS_SH = _RD_DIR / "certs.sh"
_PUSH_SECRETS_SH = _RD_DIR / "push-secrets.sh"
_CONTAINER_SH = _RD_DIR / "container.sh"
_POSTCREATE_SH = _REPO_ROOT / ".devcontainer" / "postcreate-wrapper.sh"
_DEVCONTAINER_JSON = _REPO_ROOT / ".devcontainer" / "devcontainer.json"

_TIMEOUT_ENV_VAR = "AZURE_SECRETS_PATH_TEST_TIMEOUT_SECONDS"
_DEFAULT_TIMEOUT_SECONDS = 30.0

_BASH_BIN = shutil.which("bash")
_RESOLVER_CONTEXT = "resolver-context-must-not-win"
_RESOLVER_VAULT = "gd-test-vault"
_DELIVERY_PROJECT = "general-dev"
_DELIVERY_INSTANCE = "fake-inst"


def _timeout_seconds() -> float:
    raw = os.environ.get(_TIMEOUT_ENV_VAR)
    if raw is None:
        return _DEFAULT_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        pytest.fail(f"{_TIMEOUT_ENV_VAR}={raw!r} is not a number")
    if value <= 0:
        pytest.fail(f"{_TIMEOUT_ENV_VAR}={raw!r} must be positive")
    return value


def _certs_text() -> str:
    return _CERTS_SH.read_text(encoding="utf-8")


def _push_secrets_text() -> str:
    return _PUSH_SECRETS_SH.read_text(encoding="utf-8")


def _container_text() -> str:
    return _CONTAINER_SH.read_text(encoding="utf-8")


def _postcreate_text() -> str:
    return _POSTCREATE_SH.read_text(encoding="utf-8")


def _sh_function_body(name: str, text: str) -> str:
    return _function_body(name, text=text)


# ---------------------------------------------------------------------------
# certs.sh: the kind-aware dispatcher.
# ---------------------------------------------------------------------------


def test_certs_sh_refuses_ssh_once_after_resolution_and_allows_azure() -> None:
    text = _certs_text()
    resolve_at = text.index("rd_resolve_instance")
    guard_at = text.index('if [ "$(rd_engine_kind)" = "ssh" ]; then')
    assert resolve_at < guard_at, (
        "the ssh refusal must sit after resolution, so the diagnosis names a "
        "resolved instance rather than a guess"
    )
    assert "no TLS material exists for ssh engines" in text, "the refusal message is load-bearing"
    assert 'rd_require_engine_kind ec2 "no TLS material' not in text, (
        "the guard must refuse ssh alone; an azure engine keeps all four subcommands"
    )
    assert 'rd_engine_kind)" = "azure"' in text, "the azure kinds must branch somewhere"


def test_certs_sh_azure_publish_runs_without_any_aws_session() -> None:
    text = _certs_text()
    publish_arm = text[text.index("  publish)") : text.index("  *)")]
    assert publish_arm.count('rd_engine_kind)" = "azure"') == 1
    azure_arm, ec2_arm = publish_arm.split("else", 1)
    assert "rd_require_cmd az" in azure_arm
    assert "run_certs publish" in azure_arm
    assert "--region" not in azure_arm, (
        "the az CLI is ambiently logged in; no region rides the call"
    )
    assert "AWS_PROFILE" not in azure_arm, "no profile rides an azure publish"
    assert "rd_require_aws_config" not in azure_arm
    assert "rd_check_aws_auth" not in azure_arm
    # The ec2 arm keeps its exact shape.
    assert (
        'AWS_PROFILE="$REMOTE_AWS_PROFILE" run_certs publish --region "$REMOTE_AWS_REGION"'
        in ec2_arm
    )
    assert "rd_check_aws_auth" in ec2_arm


def test_certs_sh_azure_install_reaches_the_azure_transport() -> None:
    text = _certs_text()
    install_arm = text[text.index("  install)") : text.index("  publish)")]
    assert install_arm.count('rd_engine_kind)" = "azure"') == 1
    azure_arm, ec2_arm = install_arm.split("else", 1)
    assert "rd_require_cmd az" in azure_arm
    assert "devcontainer_config.transport_azure install-material" in azure_arm
    assert '--instance "$INSTANCE"' in azure_arm
    assert '--daemon-user "$REMOTE_DAEMON_USER"' in azure_arm
    assert "--instance-id" not in azure_arm, "an azure engine has no EC2 id to name"
    assert "--profile" not in azure_arm and "--region" not in azure_arm
    # The ec2 arm keeps its exact shape.
    assert "devcontainer_config.transport install-material" in ec2_arm
    assert '--instance-id "$REMOTE_INSTANCE_ID"' in ec2_arm
    assert '--profile "$REMOTE_AWS_PROFILE"' in ec2_arm
    assert '--region "$REMOTE_AWS_REGION"' in ec2_arm


# ---------------------------------------------------------------------------
# push-secrets.sh: the azure branch and its store.
# ---------------------------------------------------------------------------


def test_push_secrets_azure_branch_sits_between_ssh_and_the_ec2_publish() -> None:
    text = _push_secrets_text()
    ssh_branch_at = text.index('if [ "${KIND:-}" = "ssh" ]')
    ssh_exit_at = text.index("exit 0", ssh_branch_at)
    azure_branch_at = text.index('if [ "${KIND:-}" = "azure" ]')
    azure_exit_at = text.index("exit 0", azure_branch_at)
    ssm_prefix_at = text.index('SSM_PREFIX="${PARAMETER_PREFIX%/}"')
    assert ssh_branch_at < ssh_exit_at < azure_branch_at, "the ssh branch must exit first"
    assert azure_branch_at < azure_exit_at < ssm_prefix_at, (
        "the azure branch must exit before the EC2-only SSM_PREFIX dereference, "
        "which would end an azure run under set -u"
    )


def test_push_secrets_azure_publish_is_file_only_and_names_the_remedies() -> None:
    text = _push_secrets_text()
    azure_fn = _sh_function_body("rd_publish_config_payload_to_key_vault", text)
    assert "write_config_payload" in azure_fn, (
        "the published payload must be the shared transform's output, not a second copy"
    )
    assert 'az keyvault secret set --vault-name "$vault" --name shell-env' in azure_fn
    assert 'az keyvault secret set --vault-name "$vault" --name profile-map' in azure_fn
    joined = re.sub(r"\\\n\s*", " ", azure_fn)
    set_lines = [line.strip() for line in joined.splitlines() if "az keyvault secret set" in line]
    assert len(set_lines) == 2, f"one secret set per payload file expected; got {set_lines}"
    for line in set_lines:
        assert "--file" in line, f"each value travels in a --file, never an argument: {line}"
    assert "--value" not in azure_fn
    assert "az login" in azure_fn, "an authentication failure must name the sign-in remedy"
    assert "secrets-vault" in azure_fn, "the vault comes from the record's one reader"
    assert "make instance-deploy" in azure_fn, "a missing record names the deploy that writes it"
    assert "umask 077" in azure_fn and "trap 'rm -rf \"$RD_PUT_DIR\"' EXIT" in azure_fn, (
        "the payload directory is 0700 and trap-cleaned, the EC2 publish's own discipline"
    )


# ---------------------------------------------------------------------------
# push-secrets.sh behavioral: the azure publish run for real.
# ---------------------------------------------------------------------------


def _fake_resolver_bin(tmp_path: Path) -> str:
    """A PATH directory whose python3 answers resolve-instance and secrets-vault.

    The resolve answer is `_print_address_block`'s azure block, the one
    lib.sh's export loop consumes; secrets-vault answers the vault name the
    engine address record would carry.
    """
    real_python = shutil.which("python3")
    assert real_python is not None, "python3 must be installed to run this suite"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    fake = bin_dir / "python3"
    fake.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "-m" ] && [ "$2" = "devcontainer_config.cli" ]'
        ' && [ "$3" = "resolve-instance" ]; then\n'
        "  cat <<'RD_TEST_BLOCK'\n"
        f"INSTANCE={_DELIVERY_INSTANCE}\n"
        "KIND=azure\n"
        f"DOCKER_CONTEXT={_RESOLVER_CONTEXT}\n"
        f"CERTS_DIR={bin_dir.parent}\n"
        "RD_TEST_BLOCK\n"
        "  exit 0\n"
        "fi\n"
        'if [ "$1" = "-m" ] && [ "$2" = "devcontainer_config.cli" ]'
        ' && [ "$3" = "secrets-vault" ]; then\n'
        f"  printf '%s\\n' {_RESOLVER_VAULT}\n"
        "  exit 0\n"
        "fi\n"
        f'exec "{real_python}" "$@"\n',
        encoding="utf-8",
    )
    fake.chmod(0o755)
    return str(bin_dir)


def _fake_az_bin(tmp_path: Path, log: Path, capture: Path) -> str:
    """A PATH directory whose az records argv and captures each --file body."""
    bin_dir = tmp_path / "az-bin"
    bin_dir.mkdir(exist_ok=True)
    fake = bin_dir / "az"
    fake.write_text(
        "#!/bin/sh\n"
        'printf \'%s\\n\' "$*" >> "$FAKE_AZ_LOG"\n'
        'name=""\n'
        'file=""\n'
        "while [ $# -gt 0 ]; do\n"
        '  [ "$1" = "--file" ] && file="$2"\n'
        '  [ "$1" = "--name" ] && name="$2"\n'
        "  shift\n"
        "done\n"
        '[ -n "$name" ] && [ -n "$file" ] && cat "$file" > "$FAKE_AZ_CAPTURE/$name"\n'
        "exit 0\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    return str(bin_dir)


_FIXTURE_ENV_TEXT = (
    "# a leading comment\n"
    "\n"
    "export HOST_PROXY='true'\n"
    "export HOST_PROXY_URL='http://proxy.example.com:8080'\n"
    "export HTTP_PROXY='http://proxy.example.com:8080'\n"
    "export BASH_ENV='/local/path/shell.env'\n"
    "export LOCAL_DOCKER_CONTEXT='orbstack'\n"
    "export REMOTE_AWS_REGION='us-east-1'\n"
)
_FIXTURE_PROFILE_MAP = '{"profiles": [{"name": "dev", "region": "us-east-1"}]}\n'


def _run_write_config_payload(tmp_path: Path) -> Path:
    """The shared transform's own render of the fixture sources, for comparison."""
    dest = tmp_path / "payload-reference"
    script = (
        "set -euo pipefail\n"
        f'source "{_RD_DIR}/lib.sh"\n'
        f'source "{_RD_DIR}/config-payload.sh"\n'
        f"SHELL_ENV_SOURCE={tmp_path / 'shell.env'}\n"
        f"PROFILE_MAP_SOURCE={tmp_path / 'aws-profile-map.json'}\n"
        f"PROJECT_NAME={_DELIVERY_PROJECT}\n"
        f"write_config_payload {dest}\n"
    )
    assert _BASH_BIN is not None
    result = subprocess.run(
        [_BASH_BIN, "-c", script],
        capture_output=True,
        text=True,
        timeout=_timeout_seconds(),
    )
    assert result.returncode == 0, (
        f"the reference render must succeed; stderr={result.stderr[:400]!r}"
    )
    return dest


def test_push_secrets_azure_delivery_publishes_the_shared_transform(
    tmp_path: Path,
) -> None:
    """The azure run exits 0 and the vault receives the shared transform's bytes.

    With the stand-in resolver exporting KIND=azure and answering
    secrets-vault, and the az stub recording every argv, the run must reach
    its own success line, set both secrets on the resolved vault, carry each
    value in a `--file` (the argv log proves no value rides an argument),
    and store exactly the payload `write_config_payload` renders from the
    same sources -- the config-payload identity, at the azure arm.
    """
    (tmp_path / "shell.env").write_text(_FIXTURE_ENV_TEXT, encoding="utf-8")
    (tmp_path / "aws-profile-map.json").write_text(_FIXTURE_PROFILE_MAP, encoding="utf-8")
    az_log = tmp_path / "az-calls.log"
    az_capture = tmp_path / "az-captured"
    az_capture.mkdir()
    bin_dir = Path(_fake_resolver_bin(tmp_path))
    az_bin = Path(_fake_az_bin(tmp_path, az_log, az_capture))

    env = dict(os.environ)
    env.update(
        PATH=f"{bin_dir}:{az_bin}:{env.get('PATH', '')}",
        PROJECT_NAME=_DELIVERY_PROJECT,
        SHELL_ENV_SOURCE=str(tmp_path / "shell.env"),
        PROFILE_MAP_SOURCE=str(tmp_path / "aws-profile-map.json"),
        REPO_SHELL_ENV=str(tmp_path / "shell.env"),
        FAKE_AZ_LOG=str(az_log),
        FAKE_AZ_CAPTURE=str(az_capture),
        CONTAINER_WORKSPACES_ROOT="/workspaces",
    )
    for var in (
        "INSTANCE",
        "KIND",
        "ENGINE",
        "REMOTE_INSTANCE_ID",
        "PARAMETER_PREFIX",
        "REMOTE_AWS_PROFILE",
        "REMOTE_AWS_REGION",
    ):
        env.pop(var, None)
    assert _BASH_BIN is not None
    result = subprocess.run(
        [_BASH_BIN, str(_PUSH_SECRETS_SH)],
        capture_output=True,
        text=True,
        timeout=_timeout_seconds(),
        env=env,
    )

    assert result.returncode == 0, (
        f"the azure publish must succeed end to end; stderr={result.stderr[:400]!r}"
    )
    assert "Secrets published to the Key Vault" in result.stdout, (
        f"the run must reach its own success line; stdout={result.stdout[-400:]!r}"
    )

    calls = az_log.read_text(encoding="utf-8").splitlines()
    assert len(calls) == 2, f"one keyvault secret set per payload file expected; got {calls}"
    for call in calls:
        assert call.startswith("keyvault secret set"), f"unexpected az call: {call}"
        assert f"--vault-name {_RESOLVER_VAULT}" in call
    assert "--name shell-env" in calls[0]
    assert "--name profile-map" in calls[1]
    for call in calls:
        assert "BEGIN" not in call and "export " not in call, f"no value may ride the argv: {call}"

    reference = _run_write_config_payload(tmp_path)
    stored = {path.read_text(encoding="utf-8") for path in sorted(az_capture.iterdir())}
    expected = {
        (reference / "shell.env").read_text(encoding="utf-8"),
        (reference / ".devcontainer" / "aws-profile-map.json").read_text(encoding="utf-8"),
    }
    assert stored == expected, (
        "the vault must receive exactly the shared transform's output, "
        "not a private copy of the rules"
    )
    stored_env = next(text for text in stored if "export " in text)
    assert "export HOST_PROXY='false'" in stored_env
    assert f"export BASH_ENV='/workspaces/{_DELIVERY_PROJECT}/shell.env'" in stored_env
    assert "HTTP_PROXY" not in stored_env and "#" not in stored_env


# ---------------------------------------------------------------------------
# container.sh: the prelude, the build override, the staleness probe.
# ---------------------------------------------------------------------------


def _container_dispatch_text() -> str:
    text = _container_text()
    return text[text.index('RDC_COMMAND="${1:-}"') :]


def test_container_prelude_clears_the_prefix_and_sets_the_azure_pair() -> None:
    dispatch = _container_dispatch_text()
    ssh_arm_at = dispatch.index('if [ "$(rd_engine_kind)" = "ssh" ]; then')
    azure_arm_at = dispatch.index('elif [ "$(rd_engine_kind)" = "azure" ]; then')
    ec2_arm_at = dispatch.index('DEVCONTAINER_SSM_PREFIX="${PARAMETER_PREFIX%/}"')
    assert ssh_arm_at < azure_arm_at < ec2_arm_at, (
        "the azure arm sits between the ssh arm and the ec2 arm, reading the same "
        "resolved kind the other dispatchers read"
    )
    azure_arm = dispatch[azure_arm_at:ec2_arm_at]
    assert "unset DEVCONTAINER_SSM_PREFIX" in azure_arm, (
        "no EC2 value may survive for an azure engine, the same rule the ssh arm applies"
    )
    assert "DEVCONTAINER_ENGINE_KIND=azure" in azure_arm
    assert "export DEVCONTAINER_ENGINE_KIND" in azure_arm
    assert "secrets-vault" in azure_arm, "the vault comes from the record's one reader"
    assert "DEVCONTAINER_VAULT_NAME" in azure_arm
    assert "export DEVCONTAINER_VAULT_NAME" in azure_arm
    assert "make instance-deploy" in azure_arm, "a missing record names the deploy that writes it"
    # The ec2 arm keeps its exact shape.
    assert 'DEVCONTAINER_SSM_PREFIX="${PARAMETER_PREFIX%/}"' in dispatch[ec2_arm_at:]
    assert "export DEVCONTAINER_SSM_PREFIX" in dispatch[ec2_arm_at:]


def test_container_build_override_branches_the_container_env_patch() -> None:
    """The ec2 patch text survives byte-identically; azure injects its own pair."""
    text = _container_text()
    build_arm = text[text.index("rdc_build_remote() {") :]
    assert "((.containerEnv // {}) + {DEVCONTAINER_SSM_PREFIX: $ssmprefix})" in build_arm, (
        "the ec2 injection must keep its exact form; the merge, not a replace, is pinned"
    )
    assert (
        ".containerEnv = ((.containerEnv // {}) + {DEVCONTAINER_ENGINE_KIND: $enginetype, "
        "DEVCONTAINER_VAULT_NAME: $vaultname})" in build_arm
    ), "the azure arm injects the kind and the vault, which the postCreate reads"
    assert "'\"${env_patch}\"'".replace("\\", "") in build_arm, (
        "one patch expression is selected by the resolved kind and interpolated into "
        "the one jq program, never a second program per kind"
    )
    for line in build_arm.splitlines():
        if line.strip().startswith("env_patch="):
            assert "| .containerEnv = " in line, (
                "each patch carries its own pipeline segment; the interpolation site "
                f"contributes no pipe of its own: {line.strip()}"
            )
    assert '--arg ssmprefix "${DEVCONTAINER_SSM_PREFIX:-}"' in build_arm, (
        "the resolved prefix still reaches the generator, guarded as before"
    )
    assert '--arg enginetype "$(rd_engine_kind)"' in build_arm
    assert '--arg vaultname "${DEVCONTAINER_VAULT_NAME:-}"' in build_arm
    assert "make instance-deploy" in build_arm, (
        "an unresolved vault must fail the build with the deploy that writes the record"
    )


def test_container_staleness_probe_reads_the_vault_timestamp_into_the_shared_comparison() -> None:
    body = _sh_function_body("rdc_ensure_secrets_current", _container_text())
    assert body.count('rd_engine_kind)" = "azure"') == 1
    azure_at = body.index('rd_engine_kind)" = "azure"')
    aws_at = body.index("rd_require_cmd aws")
    assert azure_at < aws_at, "the azure arm must run before the ec2 arm's aws requirement"
    assert "az keyvault secret show --vault-name" in body
    assert "--query attributes.updated" in body
    assert "--name shell-env" in body, (
        "the same secret the azure publish writes is the one compared"
    )
    assert "secrets-vault" in body
    assert "az login" in body, "an authentication failure must name the sign-in remedy"
    assert "make instance-deploy" in body
    # The comparison and the re-publish are shared, extended rather than duplicated.
    assert body.count("fromisoformat") == 1, (
        "the one python3 heredoc serves both kinds; a second copy would drift"
    )
    assert "published >= local_mtime" in body
    assert 'PROJECT_NAME="$PROJECT_NAME" "${RD_DIR}/push-secrets.sh"' in body
    assert "store_label" in body, "the store's name in the verdict line follows the kind"


# ---------------------------------------------------------------------------
# postcreate-wrapper.sh: the azure bootstrap mode.
# ---------------------------------------------------------------------------


def test_postcreate_selects_azure_before_the_aws_bootstrap() -> None:
    body = _sh_function_body("bootstrap_secrets", _postcreate_text())
    file_present_at = body.index('if [ -f "${SHELL_ENV}" ]')
    azure_at = body.index('if [ "${DEVCONTAINER_ENGINE_KIND}" = "azure" ]')
    aws_at = body.index('log_info "shell.env not found, bootstrapping from Parameter Store')
    assert file_present_at < azure_at < aws_at, (
        "the file-present branch wins first, azure next, and the aws arm runs only "
        "when neither applied -- its IMDS variables stay untouched for azure"
    )
    azure_arm = body[azure_at:aws_at]
    assert "bootstrap_secrets_from_key_vault" in azure_arm


def test_postcreate_azure_bootstrap_pulls_through_the_managed_identity() -> None:
    token_body = _sh_function_body("azure_imds_token", _postcreate_text())
    assert "Metadata:true" in token_body
    assert "api-version=${DEVCONTAINER_AZURE_IMDS_API_VERSION}" in token_body
    assert "resource=${DEVCONTAINER_KEYVAULT_RESOURCE}" in token_body
    assert "access_token" in token_body, (
        "the identity endpoint answers JSON; the token is extracted"
    )

    fetch_body = _sh_function_body("fetch_key_vault_secret", _postcreate_text())
    assert "https://${DEVCONTAINER_VAULT_NAME}.vault.azure.net/secrets/${secret}" in fetch_body
    assert "api-version=${DEVCONTAINER_KEYVAULT_API_VERSION}" in fetch_body
    assert "printf 'Authorization: Bearer %s\\n' \"${token}\"" in fetch_body
    assert "-H @-" in fetch_body, (
        "the bearer header reaches curl through stdin (`-H @-`), never as an argv word"
    )
    command_lines = [line for line in fetch_body.splitlines() if not line.strip().startswith("#")]
    assert all('-H "Authorization: Bearer' not in line for line in command_lines), (
        "a shell-expanded Authorization header sits in the process table for the "
        "whole fetch, readable by anything that can list processes"
    )
    assert '["value"]' in fetch_body, "the Key Vault answer is JSON; the secret value is extracted"
    assert "was 'make push-secrets' run for this project?" in fetch_body, (
        "the remedy discipline the aws arm applies is the azure arm's too"
    )
    assert '[ -s "${destination}" ]' in fetch_body, "an empty fetch is a failure, never a success"

    bootstrap_body = _sh_function_body("bootstrap_secrets_from_key_vault", _postcreate_text())
    assert 'fetch_key_vault_secret "${token}" "shell-env" "${SHELL_ENV}"' in bootstrap_body
    assert 'fetch_key_vault_secret "${token}" "profile-map" "${PROFILE_MAP}"' in bootstrap_body
    assert 'umask "${DEVCONTAINER_SECRET_UMASK}"' in bootstrap_body
    assert "DEVCONTAINER_VAULT_NAME is not set" in bootstrap_body, (
        "a missing vault fails with the variable that must carry it"
    )


def test_postcreate_aws_bootstrap_keeps_its_own_imds_variables() -> None:
    text = _postcreate_text()
    assert 'curl -sf -X PUT "${DEVCONTAINER_IMDS_ENDPOINT}/latest/api/token"' in text, (
        "the aws IMDSv2 PUT is untouched"
    )
    assert "X-aws-ec2-metadata-token-ttl-seconds: ${DEVCONTAINER_IMDS_TOKEN_TTL_SECONDS}" in text
    assert '"${DEVCONTAINER_SSM_PREFIX}/${name}"' in text, (
        "the aws arm fetches the same Parameter Store prefix it always did"
    )
    for azure_var in (
        "DEVCONTAINER_AZURE_IMDS_ENDPOINT",
        "DEVCONTAINER_AZURE_IMDS_API_VERSION",
        "DEVCONTAINER_KEYVAULT_RESOURCE",
        "DEVCONTAINER_KEYVAULT_API_VERSION",
    ):
        assert azure_var in text, f"the azure bootstrap's own knob {azure_var} is declared"


# ---------------------------------------------------------------------------
# devcontainer.json: the azure CLI ships in the image beside the aws CLI.
# ---------------------------------------------------------------------------


def test_the_azure_cli_feature_sits_beside_the_aws_cli_feature() -> None:
    text = _DEVCONTAINER_JSON.read_text(encoding="utf-8")
    aws_at = text.index('"ghcr.io/devcontainers/features/aws-cli:1"')
    azure_at = text.index('"ghcr.io/devcontainers/features/azure-cli:1"')
    assert aws_at < azure_at, "the azure feature is declared beside the aws one"
    assert text.count('"ghcr.io/devcontainers/features/azure-cli:1"') == 1
