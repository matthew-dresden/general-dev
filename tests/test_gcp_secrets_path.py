"""The gcp engine kind's secrets-and-certificates shell surface.

The Python half of the gcp slices (`certs.py`'s Secret Manager client, the
kind-selected publication, `transport_gcp`'s applied-checksum compare and
wait) carries its own coverage in `tests/test_certs.py` and
`tests/test_transport_gcp.py`. This file pins the SHELL half, the same
static-text approach `tests/test_azure_secrets_path.py` takes for the
azure kind's branches, plus one behavioral run of `push-secrets.sh`'s gcp
arm end to end.

The invariants under test, per entry point:

- `certs.sh` keeps the ssh refusal once, after resolution; the gcp publish
  arm demands gcloud alone and runs the kind-dispatched module publish; and
  the gcp install arm refuses, naming the make target that owns the
  boot-applied flow, because a gcp VM has no run-command channel for this
  script to drive.
- `push-secrets.sh`'s gcp branch sits between the ssh and azure branches
  (both of which still exit first) and the EC2 branch (whose `SSM_PREFIX`
  dereference must never see a gcp run), publishes the shared transform's
  output as `<prefix>-shell-env` and `<prefix>-profile-map` through
  `--data-file`, never argv, decides create versus version-add from the
  explicit `gcloud secrets describe` probe, and names `gcloud auth login`
  on failure.
- `container.sh` clears `DEVCONTAINER_SSM_PREFIX` for gcp where ssh and
  azure clear it, exports the gcp trio the build and the container read,
  the build override's containerEnv patch branches on the resolved kind
  without touching the ec2 patch's byte-identical text, and the staleness
  probe reads Secret Manager's `createTime` into the same comparison the
  other kinds have always run.
- `postcreate-wrapper.sh` selects the Secret Manager bootstrap mode on
  `DEVCONTAINER_ENGINE_KIND=gcp` before the aws arm, fetches through the
  metadata-server token with the same umask and remedy discipline, and the
  aws arm keeps its own IMDS variables untouched.

The behavioral run drives a stand-in resolver (answering `resolve-instance`,
`gcp-project-id` and `gcp-secrets-prefix`) and a `gcloud` stub that records
every argv and captures each `--data-file` body: the transform that lands
in Secret Manager is compared against `write_config_payload`'s own render,
so the published bytes are pinned to the shared transform rather than to a
copy of it.
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

_TIMEOUT_ENV_VAR = "GCP_SECRETS_PATH_TEST_TIMEOUT_SECONDS"
_DEFAULT_TIMEOUT_SECONDS = 30.0

_BASH_BIN = shutil.which("bash")
_RESOLVER_CONTEXT = "resolver-context-must-not-win"
_RESOLVER_PROJECT = "gd-test-project"
_RESOLVER_PREFIX = "fake-inst"
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
# certs.sh: the gcp publish and install arms.
# ---------------------------------------------------------------------------


def test_certs_sh_gcp_publish_demands_gcloud_alone() -> None:
    text = _certs_text()
    publish_arm = text[text.index("  publish)") : text.index("  *)")]
    gcp_at = publish_arm.index('rd_engine_kind)" = "gcp"')
    gcp_arm = publish_arm[gcp_at : publish_arm.index("else", gcp_at)]
    assert "rd_require_cmd gcloud" in gcp_arm
    assert "run_certs publish" in gcp_arm, (
        "the module dispatches on the kind and resolves the record's project and prefix"
    )
    assert "--region" not in gcp_arm, "no aws region rides a gcp publish"
    assert "AWS_PROFILE" not in gcp_arm
    assert "rd_require_aws_config" not in gcp_arm
    assert "rd_check_aws_auth" not in gcp_arm
    # The azure and ec2 arms keep their exact shapes.
    assert 'rd_engine_kind)" = "azure"' in publish_arm
    assert (
        'AWS_PROFILE="$REMOTE_AWS_PROFILE" run_certs publish --region "$REMOTE_AWS_REGION"'
        in publish_arm
    )


def test_certs_sh_gcp_install_refuses_naming_the_make_target() -> None:
    """A gcp VM has no run-command channel: the install is boot-applied and
    the make target owns the drift compare, the reboot and the wait."""
    text = _certs_text()
    install_arm = text[text.index("  install)") : text.index("  publish)")]
    gcp_at = install_arm.index('rd_engine_kind)" = "gcp"')
    gcp_arm = install_arm[gcp_at : install_arm.index("else", gcp_at)]
    assert "rd_fail" in gcp_arm
    assert "make cert-install INSTANCE=" in gcp_arm, (
        "the refusal names the target that owns the boot-applied flow"
    )
    assert "transport install-material" not in gcp_arm, (
        "the ec2 transport never reaches a gcp VM through this script"
    )
    assert "transport_azure install-material" not in gcp_arm


# ---------------------------------------------------------------------------
# push-secrets.sh: the gcp branch and its store.
# ---------------------------------------------------------------------------


def test_push_secrets_gcp_branch_sits_between_azure_and_the_ec2_publish() -> None:
    text = _push_secrets_text()
    ssh_branch_at = text.index('if [ "${KIND:-}" = "ssh" ]')
    ssh_exit_at = text.index("exit 0", ssh_branch_at)
    azure_branch_at = text.index('if [ "${KIND:-}" = "azure" ]')
    azure_exit_at = text.index("exit 0", azure_branch_at)
    gcp_branch_at = text.index('if [ "${KIND:-}" = "gcp" ]')
    gcp_exit_at = text.index("exit 0", gcp_branch_at)
    ssm_prefix_at = text.index('SSM_PREFIX="${PARAMETER_PREFIX%/}"')
    assert ssh_branch_at < ssh_exit_at < azure_branch_at, "the ssh branch must exit first"
    assert azure_branch_at < azure_exit_at < gcp_branch_at, "the azure branch exits before gcp"
    assert gcp_branch_at < gcp_exit_at < ssm_prefix_at, (
        "the gcp branch must exit before the EC2-only SSM_PREFIX dereference, "
        "which would end a gcp run under set -u"
    )


def test_push_secrets_gcp_publish_is_file_only_probed_and_names_the_remedies() -> None:
    text = _push_secrets_text()
    gcp_fn = _sh_function_body("rd_publish_config_payload_to_secret_manager", text)
    assert "write_config_payload" in gcp_fn, (
        "the published payload must be the shared transform's output, not a second copy"
    )
    assert "gcp-secrets-prefix" in gcp_fn, "the prefix comes from the record's one reader"
    assert "gcp-project-id" in gcp_fn, "the project comes from the record's one reader"
    assert "make instance-deploy" in gcp_fn, "a missing record names the deploy that writes it"
    # The create-versus-version-add decision is an explicit probe.
    assert 'gcloud secrets describe "${prefix}-shell-env"' in gcp_fn
    assert "gcloud secrets create" in gcp_fn
    assert "gcloud secrets versions add" in gcp_fn
    # Every value rides a --data-file, never an argument.
    joined = re.sub(r"\\\n\s*", " ", gcp_fn)
    data_file_lines = [
        line.strip()
        for line in joined.splitlines()
        if "gcloud secrets" in line and "describe" not in line
    ]
    assert len(data_file_lines) == 4, (
        f"two creates plus two version adds expected: {data_file_lines}"
    )
    for line in data_file_lines:
        assert "--data-file=" in line, (
            f"each value travels in a --data-file, never an argument: {line}"
        )
    assert "gcloud auth login" in gcp_fn, "an authentication failure must name the sign-in remedy"
    assert "umask 077" in gcp_fn and "trap 'rm -rf \"$RD_PUT_DIR\"' EXIT" in gcp_fn, (
        "the payload directory is 0700 and trap-cleaned, the EC2 publish's own discipline"
    )


# ---------------------------------------------------------------------------
# push-secrets.sh behavioral: the gcp publish run for real.
# ---------------------------------------------------------------------------


def _fake_resolver_bin(tmp_path: Path) -> str:
    """A PATH directory whose python3 answers resolve-instance and the two
    gcp record readers.

    The resolve answer is the address block lib.sh's export loop consumes;
    the readers answer the project and prefix the engine address record
    would carry.
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
        "KIND=gcp\n"
        f"DOCKER_CONTEXT={_RESOLVER_CONTEXT}\n"
        f"CERTS_DIR={bin_dir.parent}\n"
        "RD_TEST_BLOCK\n"
        "  exit 0\n"
        "fi\n"
        'if [ "$1" = "-m" ] && [ "$2" = "devcontainer_config.cli" ]'
        ' && [ "$3" = "gcp-project-id" ]; then\n'
        f"  printf '%s\\n' {_RESOLVER_PROJECT}\n"
        "  exit 0\n"
        "fi\n"
        'if [ "$1" = "-m" ] && [ "$2" = "devcontainer_config.cli" ]'
        ' && [ "$3" = "gcp-secrets-prefix" ]; then\n'
        f"  printf '%s\\n' {_RESOLVER_PREFIX}\n"
        "  exit 0\n"
        "fi\n"
        f'exec "{real_python}" "$@"\n',
        encoding="utf-8",
    )
    fake.chmod(0o755)
    return str(bin_dir)


def _fake_gcloud_bin(tmp_path: Path, log: Path, capture: Path) -> str:
    """A PATH directory whose gcloud records argv and captures each --data-file body.

    `secrets describe` answers "missing" so the publish takes the create
    path; `secrets create` captures the data file's bytes per secret id.
    """
    bin_dir = tmp_path / "gcloud-bin"
    bin_dir.mkdir(exist_ok=True)
    fake = bin_dir / "gcloud"
    fake.write_text(
        "#!/bin/sh\n"
        'printf \'%s\\n\' "$*" >> "$FAKE_GCLOUD_LOG"\n'
        'for argument in "$@"; do\n'
        '  [ "$argument" = "describe" ] && exit 1\n'
        "done\n"
        'secret=""\n'
        'data_file=""\n'
        'previous=""\n'
        'for argument in "$@"; do\n'
        '  [ "$previous" = "create" ] && secret="$argument"\n'
        '  case "$argument" in --data-file=*) data_file="${argument#--data-file=}" ;; esac\n'
        '  previous="$argument"\n'
        "done\n"
        '[ -n "$secret" ] && [ -n "$data_file" ] && '
        'cat "$data_file" > "$FAKE_GCLOUD_CAPTURE/$secret"\n'
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


def test_push_secrets_gcp_delivery_publishes_the_shared_transform(tmp_path: Path) -> None:
    """The gcp run exits 0 and Secret Manager receives the shared transform's bytes.

    With the stand-in resolver exporting KIND=gcp and answering the two
    record readers, and the gcloud stub recording every argv, the run must
    reach its own success line, create both secrets under the resolved
    prefix, carry each value in a `--data-file` (the argv log proves no
    value rides an argument), and store exactly the payload
    `write_config_payload` renders from the same sources -- the
    config-payload identity, at the gcp arm.
    """
    (tmp_path / "shell.env").write_text(_FIXTURE_ENV_TEXT, encoding="utf-8")
    (tmp_path / "aws-profile-map.json").write_text(_FIXTURE_PROFILE_MAP, encoding="utf-8")
    gcloud_log = tmp_path / "gcloud-calls.log"
    gcloud_capture = tmp_path / "gcloud-captured"
    gcloud_capture.mkdir()
    bin_dir = Path(_fake_resolver_bin(tmp_path))
    gcloud_bin = Path(_fake_gcloud_bin(tmp_path, gcloud_log, gcloud_capture))

    env = dict(os.environ)
    env.update(
        PATH=f"{bin_dir}:{gcloud_bin}:{env.get('PATH', '')}",
        PROJECT_NAME=_DELIVERY_PROJECT,
        SHELL_ENV_SOURCE=str(tmp_path / "shell.env"),
        PROFILE_MAP_SOURCE=str(tmp_path / "aws-profile-map.json"),
        REPO_SHELL_ENV=str(tmp_path / "shell.env"),
        FAKE_GCLOUD_LOG=str(gcloud_log),
        FAKE_GCLOUD_CAPTURE=str(gcloud_capture),
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
        f"the gcp publish must succeed end to end; stderr={result.stderr[:400]!r}"
    )
    assert "Secrets published to Secret Manager" in result.stdout, (
        f"the run must reach its own success line; stdout={result.stdout[-400:]!r}"
    )

    calls = gcloud_log.read_text(encoding="utf-8").splitlines()
    creates = [call for call in calls if " secrets create " in f" {call} "]
    assert len(creates) == 2, f"one create per payload file expected; got {calls}"
    for call in creates:
        assert f"--project {_RESOLVER_PROJECT}" in call
        assert f"{_RESOLVER_PREFIX}-shell-env" in call or (
            f"{_RESOLVER_PREFIX}-profile-map" in call
        )
    for call in calls:
        assert "BEGIN" not in call and "export " not in call, f"no value may ride the argv: {call}"

    reference = _run_write_config_payload(tmp_path)
    stored = {path.read_text(encoding="utf-8") for path in sorted(gcloud_capture.iterdir())}
    expected = {
        (reference / "shell.env").read_text(encoding="utf-8"),
        (reference / ".devcontainer" / "aws-profile-map.json").read_text(encoding="utf-8"),
    }
    assert stored == expected, (
        "Secret Manager must receive exactly the shared transform's output, "
        "not a private copy of the rules"
    )
    stored_env = next(text for text in stored if "export " in text)
    assert "export HOST_PROXY='false'" in stored_env
    assert f"export BASH_ENV='/workspaces/{_DELIVERY_PROJECT}/shell.env'" in stored_env
    assert "HTTP_PROXY" not in stored_env and "#" not in stored_env


# ---------------------------------------------------------------------------
# container.sh: the prelude, the build override, the staleness probe.
# ---------------------------------------------------------------------------


def test_container_prelude_clears_the_prefix_and_sets_the_gcp_trio() -> None:
    dispatch = _container_text()
    dispatch = dispatch[dispatch.index('RDC_COMMAND="${1:-}"') :]
    ssh_arm_at = dispatch.index('if [ "$(rd_engine_kind)" = "ssh" ]; then')
    azure_arm_at = dispatch.index('elif [ "$(rd_engine_kind)" = "azure" ]; then')
    gcp_arm_at = dispatch.index('elif [ "$(rd_engine_kind)" = "gcp" ]; then')
    ec2_arm_at = dispatch.index('DEVCONTAINER_SSM_PREFIX="${PARAMETER_PREFIX%/}"')
    assert ssh_arm_at < azure_arm_at < gcp_arm_at < ec2_arm_at, (
        "the gcp arm sits between the azure arm and the ec2 arm, reading the same "
        "resolved kind the other dispatchers read"
    )
    gcp_arm = dispatch[gcp_arm_at:ec2_arm_at]
    assert "unset DEVCONTAINER_SSM_PREFIX" in gcp_arm, (
        "no EC2 value may survive for a gcp engine, the same rule the ssh arm applies"
    )
    assert "DEVCONTAINER_ENGINE_KIND=gcp" in gcp_arm
    assert "export DEVCONTAINER_ENGINE_KIND" in gcp_arm
    assert "gcp-project-id" in gcp_arm, "the project comes from the record's one reader"
    assert "gcp-secrets-prefix" in gcp_arm, "the prefix comes from the record's one reader"
    assert "DEVCONTAINER_GCP_PROJECT" in gcp_arm
    assert "export DEVCONTAINER_GCP_PROJECT" in gcp_arm
    assert "DEVCONTAINER_SECRETS_PREFIX" in gcp_arm
    assert "export DEVCONTAINER_SECRETS_PREFIX" in gcp_arm
    assert "make instance-deploy" in gcp_arm, "a missing record names the deploy that writes it"
    # The ec2 arm keeps its exact shape.
    assert 'DEVCONTAINER_SSM_PREFIX="${PARAMETER_PREFIX%/}"' in dispatch[ec2_arm_at:]
    assert "export DEVCONTAINER_SSM_PREFIX" in dispatch[ec2_arm_at:]


def test_container_build_override_branches_the_container_env_patch_for_gcp() -> None:
    """The ec2 patch text survives byte-identically; gcp injects its own trio."""
    text = _container_text()
    build_arm = text[text.index("rdc_build_remote() {") :]
    assert "((.containerEnv // {}) + {DEVCONTAINER_SSM_PREFIX: $ssmprefix})" in build_arm, (
        "the ec2 injection must keep its exact form; the merge, not a replace, is pinned"
    )
    assert (
        ".containerEnv = ((.containerEnv // {}) + {DEVCONTAINER_ENGINE_KIND: $enginetype, "
        "DEVCONTAINER_GCP_PROJECT: $gcpproject, DEVCONTAINER_SECRETS_PREFIX: $secretsprefix})"
        in build_arm
    ), "the gcp arm injects the kind, the project and the prefix, which the postCreate reads"
    assert "'\"${env_patch}\"'".replace("\\", "") in build_arm, (
        "one patch expression is selected by the resolved kind and interpolated into "
        "the one jq program, never a second program per kind"
    )
    assert '--arg gcpproject "${DEVCONTAINER_GCP_PROJECT:-}"' in build_arm
    assert '--arg secretsprefix "${DEVCONTAINER_SECRETS_PREFIX:-}"' in build_arm
    assert "make instance-deploy" in build_arm, (
        "an unresolved trio must fail the build with the deploy that writes the record"
    )


def test_container_staleness_probe_reads_the_create_time_into_the_shared_comparison() -> None:
    body = _sh_function_body("rdc_ensure_secrets_current", _container_text())
    gcp_at = body.index('rd_engine_kind)" = "gcp"')
    gcp_arm = body[gcp_at : body.index("else", gcp_at)]
    assert "rd_require_cmd gcloud" in gcp_arm
    assert "gcp-secrets-prefix" in gcp_arm and "gcp-project-id" in gcp_arm
    assert 'gcloud secrets describe "${prefix}-shell-env"' in gcp_arm
    assert '--format="value(createTime)"' in gcp_arm
    assert "gcloud auth login" in gcp_arm, "an authentication failure names the sign-in remedy"
    assert "make instance-deploy" in gcp_arm
    # The comparison and the re-publish are shared across all three kinds.
    assert body.count("fromisoformat") == 1, (
        "the one python3 heredoc serves every kind; a second copy would drift"
    )
    assert "published >= local_mtime" in body
    assert 'PROJECT_NAME="$PROJECT_NAME" "${RD_DIR}/push-secrets.sh"' in body
    assert "Secret Manager" in body, "the store's name in the verdict line follows the kind"


# ---------------------------------------------------------------------------
# postcreate-wrapper.sh: the gcp bootstrap mode.
# ---------------------------------------------------------------------------


def test_postcreate_selects_gcp_before_the_aws_bootstrap() -> None:
    body = _sh_function_body("bootstrap_secrets", _postcreate_text())
    file_present_at = body.index('if [ -f "${SHELL_ENV}" ]')
    azure_at = body.index('if [ "${DEVCONTAINER_ENGINE_KIND}" = "azure" ]')
    gcp_at = body.index('if [ "${DEVCONTAINER_ENGINE_KIND}" = "gcp" ]')
    aws_at = body.index('log_info "shell.env not found, bootstrapping from Parameter Store')
    assert file_present_at < azure_at < gcp_at < aws_at, (
        "the file-present branch wins first, azure next, gcp next, and the aws arm "
        "runs only when none applied -- its IMDS variables stay untouched for gcp"
    )
    gcp_arm = body[gcp_at:aws_at]
    assert "bootstrap_secrets_from_secret_manager" in gcp_arm


def test_postcreate_gcp_bootstrap_pulls_through_the_metadata_server() -> None:
    token_body = _sh_function_body("gcp_metadata_token", _postcreate_text())
    assert "Metadata-Flavor: ${DEVCONTAINER_GCP_METADATA_FLAVOR}" in token_body
    assert "${DEVCONTAINER_GCP_METADATA_ENDPOINT}${DEVCONTAINER_GCP_METADATA_TOKEN_PATH}" in (
        token_body
    )
    assert "access_token" in token_body, "the metadata server answers JSON; the token is extracted"

    fetch_body = _sh_function_body("fetch_secret_manager_secret", _postcreate_text())
    assert (
        "${DEVCONTAINER_SECRET_MANAGER_ENDPOINT}/projects/${DEVCONTAINER_GCP_PROJECT}"
        "/secrets/${secret}/versions/latest:access" in fetch_body
    )
    assert "printf 'Authorization: Bearer %s\\n' \"${token}\"" in fetch_body
    assert "-H @-" in fetch_body, (
        "the bearer header reaches curl through stdin (`-H @-`), never as an argv word"
    )
    command_lines = [line for line in fetch_body.splitlines() if not line.strip().startswith("#")]
    assert all('-H "Authorization: Bearer' not in line for line in command_lines), (
        "a shell-expanded Authorization header sits in the process table for the "
        "whole fetch, readable by anything that can list processes"
    )
    assert 'base64.b64decode(json.load(sys.stdin)["payload"]["data"])' in fetch_body, (
        "the Secret Manager answer is base64 inside JSON; the value is decoded"
    )
    assert "was 'make push-secrets' run for this project?" in fetch_body, (
        "the remedy discipline the aws arm applies is the gcp arm's too"
    )
    assert '[ -s "${destination}" ]' in fetch_body, "an empty fetch is a failure, never a success"

    bootstrap_body = _sh_function_body("bootstrap_secrets_from_secret_manager", _postcreate_text())
    shell_env_fetch = (
        'fetch_secret_manager_secret "${token}" '
        '"${DEVCONTAINER_SECRETS_PREFIX}-shell-env" "${SHELL_ENV}"'
    )
    assert shell_env_fetch in bootstrap_body
    profile_map_fetch = (
        'fetch_secret_manager_secret "${token}" '
        '"${DEVCONTAINER_SECRETS_PREFIX}-profile-map" "${PROFILE_MAP}"'
    )
    assert profile_map_fetch in bootstrap_body
    assert 'umask "${DEVCONTAINER_SECRET_UMASK}"' in bootstrap_body
    assert "DEVCONTAINER_GCP_PROJECT is not set" in bootstrap_body, (
        "a missing project fails with the variable that must carry it"
    )
    assert "DEVCONTAINER_SECRETS_PREFIX is not set" in bootstrap_body, (
        "a missing prefix fails with the variable that must carry it"
    )


def test_postcreate_gcp_constants_are_declared_beside_the_azure_ones() -> None:
    text = _postcreate_text()
    for gcp_var in (
        "DEVCONTAINER_GCP_METADATA_ENDPOINT",
        "DEVCONTAINER_GCP_METADATA_TOKEN_PATH",
        "DEVCONTAINER_SECRET_MANAGER_ENDPOINT",
        "DEVCONTAINER_GCP_METADATA_FLAVOR",
        "DEVCONTAINER_GCP_PROJECT",
        "DEVCONTAINER_SECRETS_PREFIX",
    ):
        assert gcp_var in text, f"the gcp bootstrap's own knob {gcp_var} is declared"
