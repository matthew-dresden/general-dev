"""The container path is engine-kind aware: an SSH engine runs with zero AWS.

P1 gave every instance an engine kind (`remote-instances/<name>/engine.json`;
the resolver prints `KIND=ssh` plus `ENGINE_*` values for an ssh engine and
no `KIND` line for EC2) and P2 made the forward kind-aware. P3 is the
container half, and the design it pins: for `KIND=ssh` engines, configuration
reaches the container through the SEED VOLUME, never Parameter Store --
postcreate-wrapper's existing file-present branch is the consumer, so the
whole container path needs no AWS credential anywhere.

lib.sh (behavioral, with a stand-in resolver, the pattern
`tests/test_engine_targeting.py` established): `rd_require_remote_config`
passes for a resolved ssh engine without any id or AWS value and still
demands `REMOTE_DOCKER_CONTEXT`, while the EC2 requirements keep their exact
shape; `rd_check_prereqs` drops the aws/session-manager-plugin pair for ssh;
`rd_engine_diagnosis` names the ssh forward and its refresh remedy, never an
aws sso login and never the SSM session.

container.sh (static, the approach of `tests/test_container_fleet.py` and
`tests/test_container_verify.py`: no docker in this file): the secrets
freshness probe is skipped for ssh; the volume seed writes both payload
files over a stdin heredoc with `rdc_sh_escape`d values and a chown, only
under the ssh guard, and the EC2 clone heredoc carries no payload write; the
up-path refresh drives the forwards CLI's ssh flag form with no aws sso
login text in that branch.

config-payload.sh (behavioral): `write_config_payload` applies the exact
transform push-secrets.sh has always published -- HOST_PROXY forced off,
BASH_ENV repointed, proxy/asdf/localscripts lines and comments and blanks
stripped, the profile map copied verbatim -- and push-secrets.sh consumes
that one transform instead of carrying its own copy.

push-secrets.sh (static): the ssh branch resolves first, delivers over
stdin (`cat >`), verifies the files landed, and never touches Parameter
Store; the workspace path comes from the shared derivation, not a re-derived
`/devcontainer/<project>`; the CONTAINER_USER default lives here, next to
the execs that dereference it, not only in container.sh.

push-secrets.sh (behavioral, the ssh delivery run for real): with a
stand-in resolver exporting KIND=ssh, a docker stub that records every argv
and captures `exec -i`'s stdin while refusing nothing, and real fixture
sources, the run exits 0 with both payload files delivered into the running
container -- the rendered transform and the verbatim profile map, as the
defaulted or caller-pinned CONTAINER_USER -- honoring CONTAINER=<name> and
refusing a name nothing answers to.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest
from conftest import _function_body, _synthetic_instance_id

_REPO_ROOT = Path(__file__).resolve().parent.parent
_RD_DIR = _REPO_ROOT / ".devcontainer" / "remote-docker"
_LIB_SH = _RD_DIR / "lib.sh"
_CONTAINER_SH = _RD_DIR / "container.sh"
_PUSH_SECRETS_SH = _RD_DIR / "push-secrets.sh"
_CONFIG_PAYLOAD_SH = _RD_DIR / "config-payload.sh"

_TIMEOUT_ENV_VAR = "ENGINE_CONTAINER_PATH_TEST_TIMEOUT_SECONDS"
_DEFAULT_TIMEOUT_SECONDS = 30.0

# The block's context is deliberately NOT the context a caller pinned, the
# same way `tests/test_engine_targeting.py`'s stand-in names one: assertions
# here must be able to tell the two apart.
_RESOLVER_CONTEXT = "resolver-context-must-not-win"

_BASH_BIN = shutil.which("bash")


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


def _lib_text() -> str:
    return _LIB_SH.read_text(encoding="utf-8")


def _container_text() -> str:
    return _CONTAINER_SH.read_text(encoding="utf-8")


def _push_secrets_text() -> str:
    return _PUSH_SECRETS_SH.read_text(encoding="utf-8")


def _sh_function_body(name: str, text: str) -> str:
    return _function_body(name, text=text)


# ---------------------------------------------------------------------------
# The stand-in resolver. Mirrors `tests/test_engine_targeting.py`'s fake
# python3 -- a PATH-earlier interpreter that answers resolve-instance from a
# heredoc and forwards everything else to the real one -- because the shared
# helpers there are module-local and shaped for the EC2 block. The ssh block
# here is `_print_address_block`'s ssh shape, the one lib.sh's export loop
# consumes; the certificate directory is real so the id-store read runs.
# Nothing reaches docker, AWS or the network.
# ---------------------------------------------------------------------------


def _resolver_block(tmp_path: Path, *, kind: str) -> str:
    """The resolved address block a stand-in resolver prints, per kind.

    The block's shape is `_print_address_block`'s, and the certificate
    directory carries a real id store so the resolver's read of it runs.
    """
    certs_dir = tmp_path / "certs"
    certs_dir.mkdir(exist_ok=True)
    (certs_dir / "instance-id").write_text(_synthetic_instance_id(17) + "\n", encoding="utf-8")
    if kind == "ssh":
        host = _engine_host()
        user = _engine_user()
        port = _engine_port()
        return (
            "INSTANCE=fake-inst\n"
            "KIND=ssh\n"
            f"DOCKER_CONTEXT={_RESOLVER_CONTEXT}\n"
            f"CERTS_DIR={certs_dir}\n"
            f"ENGINE_HOST={host}\n"
            f"ENGINE_PORT={port}\n"
            f"ENGINE_USER={user}\n"
            f"ENGINE_DOCKER_ADDR=/var/run/docker-{uuid.uuid4().hex[:8]}.sock\n"
            f"ENGINE_ENDPOINT=ssh://{user}@{host}:{port}\n"
        )
    return (
        "INSTANCE=fake-inst\n"
        f"TERRAGRUNT_DIR={tmp_path / 'tg'}\n"
        "STATE_KEY=fake/inst\n"
        f"DOCKER_CONTEXT={_RESOLVER_CONTEXT}\n"
        "PARAMETER_PREFIX=/devcontainer/fake\n"
        f"CERTS_DIR={certs_dir}\n"
    )


def _engine_host() -> str:
    return f"engine-{uuid.uuid4().hex[:8]}.example.invalid"


def _engine_user() -> str:
    return f"dev-{uuid.uuid4().hex[:8]}"


def _engine_port() -> int:
    return 20000 + int(uuid.uuid4().hex[:4], 16) % 10000


def _fake_resolver_bin(tmp_path: Path, block: str) -> str:
    """A PATH directory whose python3 answers resolve-instance with `block`."""
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
        f"{block}"
        "RD_TEST_BLOCK\n"
        "  exit 0\n"
        "fi\n"
        f'exec "{real_python}" "$@"\n',
        encoding="utf-8",
    )
    fake.chmod(0o755)
    return str(bin_dir)


def _lib_snippet_script(snippet: str) -> str:
    """A bash script that has sourced lib.sh from a clean variable slate."""
    return (
        "set -euo pipefail\n"
        "unset ENGINE INSTANCE DOCKER_CONTEXT REMOTE_DOCKER_CONTEXT"
        " REMOTE_INSTANCE_ID RD_INSTANCE_RESOLVED RD_INSTANCE_CALLER"
        " KIND ENGINE_HOST ENGINE_PORT ENGINE_USER ENGINE_DOCKER_ADDR"
        " ENGINE_ENDPOINT REMOTE_AWS_REGION REMOTE_AWS_PROFILE PARAMETER_PREFIX\n"
        f'source "{_LIB_SH}"\n'
        f"{snippet}"
    )


def _run_snippet(
    snippet: str, *, path_prepend: str | None = None, path_only: str | None = None
) -> subprocess.CompletedProcess:
    """The snippet under a controlled PATH.

    `path_prepend` puts a directory first while keeping the machine's tools
    reachable (the stand-in resolver needs the real python3); `path_only`
    makes its directory the WHOLE PATH, so a tool missing from it is
    genuinely absent -- the property the prereqs and diagnosis tests
    observe. The interpreter is resolved by absolute path up front, because
    `path_only` must also govern how the child's own `command -v` answers,
    not only which tools a script happens to find.
    """
    assert _BASH_BIN is not None, "bash must be installed to run this suite"
    env = dict(os.environ)
    if path_only is not None:
        env["PATH"] = path_only
    elif path_prepend is not None:
        env["PATH"] = f"{path_prepend}:{env.get('PATH', '')}"
    return subprocess.run(
        [_BASH_BIN, "-c", _lib_snippet_script(snippet)],
        capture_output=True,
        text=True,
        timeout=_timeout_seconds(),
        env=env,
    )


# ---------------------------------------------------------------------------
# lib.sh: the kind-aware requirements and diagnosis, behaviorally.
# ---------------------------------------------------------------------------


def test_ssh_resolution_satisfies_remote_config_without_any_aws_value(tmp_path: Path) -> None:
    """KIND=ssh needs only the docker context: no id store, no AWS variable."""
    bin_dir = _fake_resolver_bin(tmp_path, _resolver_block(tmp_path, kind="ssh"))
    result = _run_snippet(
        "rd_resolve_instance_quiet\n"
        "rd_require_remote_config\n"
        'printf \'kind=%s ctx=%s\\n\' "$KIND" "$REMOTE_DOCKER_CONTEXT"\n',
        path_prepend=bin_dir,
    )
    assert result.returncode == 0, (
        f"ssh resolution must satisfy remote config; stderr={result.stderr[:400]!r}"
    )
    assert "kind=ssh" in result.stdout
    assert f"ctx={_RESOLVER_CONTEXT}" in result.stdout


def test_ssh_remote_config_still_demands_the_docker_context(tmp_path: Path) -> None:
    """The one requirement ssh keeps is REMOTE_DOCKER_CONTEXT."""
    block = _resolver_block(tmp_path, kind="ssh").replace(
        f"DOCKER_CONTEXT={_RESOLVER_CONTEXT}\n", ""
    )
    bin_dir = _fake_resolver_bin(tmp_path, block)
    result = _run_snippet(
        "rd_resolve_instance_quiet\nunset REMOTE_DOCKER_CONTEXT\nrd_require_remote_config\n",
        path_prepend=bin_dir,
    )
    assert result.returncode != 0, "a missing REMOTE_DOCKER_CONTEXT must still be refused"
    assert "REMOTE_DOCKER_CONTEXT must be set" in result.stderr, (
        f"the refusal must name the one variable ssh demands; stderr={result.stderr[:400]!r}"
    )


def test_ec2_remote_config_is_unchanged(tmp_path: Path) -> None:
    """KIND unset (EC2): the id and the AWS variables are demanded as before."""
    bin_dir = _fake_resolver_bin(tmp_path, _resolver_block(tmp_path, kind="ec2"))
    missing = _run_snippet(
        "rd_resolve_instance_quiet\nrd_require_remote_config\n", path_prepend=bin_dir
    )
    assert missing.returncode != 0, "EC2 without AWS values must still be refused"
    assert "REMOTE_AWS_REGION must be set" in missing.stderr, (
        f"the EC2 refusal must be the AWS one, unchanged; stderr={missing.stderr[:400]!r}"
    )
    satisfied = _run_snippet(
        "REMOTE_AWS_REGION=us-east-1\n"
        "REMOTE_AWS_PROFILE=dev\n"
        "rd_resolve_instance_quiet\n"
        "rd_require_remote_config\n"
        "printf 'id=%s\\n' \"$REMOTE_INSTANCE_ID\"\n",
        path_prepend=bin_dir,
    )
    assert satisfied.returncode == 0, (
        f"EC2 with its values must pass; stderr={satisfied.stderr[:400]!r}"
    )
    assert "id=i-" in satisfied.stdout, "the id-store read must still feed the requirement"


def test_ssh_prereqs_skip_the_aws_tools(tmp_path: Path) -> None:
    """KIND=ssh: rd_check_prereqs passes with neither aws tool on PATH."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    result = _run_snippet("KIND=ssh\nrd_check_prereqs\nprintf 'ok\\n'\n", path_only=str(bin_dir))
    assert result.returncode == 0, (
        f"ssh prereqs must not demand the aws tools; stderr={result.stderr[:400]!r}"
    )
    assert "ok" in result.stdout


def test_ec2_prereqs_still_demand_the_aws_tools(tmp_path: Path) -> None:
    """KIND unset: the missing aws CLI is refused exactly as before."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    result = _run_snippet("rd_check_prereqs\n", path_only=str(bin_dir))
    assert result.returncode != 0, "EC2 prereqs must still demand aws"
    assert "aws" in result.stderr, (
        f"the refusal must name the missing tool; stderr={result.stderr[:400]!r}"
    )


def test_engine_diagnosis_for_ssh_names_the_forward_and_never_the_aws_path(tmp_path: Path) -> None:
    """Behavioral, with aws present and failing: ssh never reads that as an
    expired SSO session and never speaks of the SSM forward."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    docker = bin_dir / "docker"
    docker.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    docker.chmod(0o755)
    aws = bin_dir / "aws"
    aws.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    aws.chmod(0o755)
    result = _run_snippet(
        "KIND=ssh\n"
        "INSTANCE=fake-inst\n"
        "REMOTE_DOCKER_CONTEXT=general-dev-fake-inst\n"
        "rd_engine_diagnosis general-dev-fake-inst\n",
        path_only=str(bin_dir),
    )
    assert result.returncode == 0, f"diagnosis must answer; stderr={result.stderr[:400]!r}"
    out = result.stdout
    assert "ssh forward" in out, f"the ssh branch must name the ssh forward; got {out!r}"
    assert "make connect-refresh INSTANCE=fake-inst" in out, (
        f"the ssh remedy must name the refresh; got {out!r}"
    )
    assert "aws sso login" not in out, (
        f"ssh must never send the operator to aws sso login; got {out!r}"
    )
    assert "SSM port forward" not in out, f"ssh must never speak of the SSM session; got {out!r}"


def test_engine_diagnosis_ec2_path_still_names_the_sso_remedy(tmp_path: Path) -> None:
    """The other side of the guard: KIND unset, aws failing, reads as an
    expired session exactly as before."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    docker = bin_dir / "docker"
    docker.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    docker.chmod(0o755)
    # aws is installed but its sts call fails, which is the exact condition
    # the EC2 branch reads as an expired session.
    aws = bin_dir / "aws"
    aws.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    aws.chmod(0o755)
    result = _run_snippet(
        "REMOTE_AWS_PROFILE=dev\n"
        "REMOTE_AWS_REGION=us-east-1\n"
        "REMOTE_DOCKER_CONTEXT=general-dev-fake-inst\n"
        "rd_engine_diagnosis general-dev-fake-inst\n",
        path_only=str(bin_dir),
    )
    assert result.returncode == 0, f"diagnosis must answer; stderr={result.stderr[:400]!r}"
    out = result.stdout
    assert "has expired" in out and "aws sso login --profile dev" in out, (
        f"the EC2 path keeps its sso remedy; got {out!r}"
    )
    assert "ssh forward" not in out


def test_engine_diagnosis_ssh_branch_text_is_forward_only() -> None:
    """Static: the ssh branch itself names ENGINE_ENDPOINT and no EC2 value."""
    body = _sh_function_body("rd_engine_diagnosis", _lib_text())
    branch_at = body.index('if [ "${KIND:-}" = "ssh" ]; then')
    branch = body[branch_at : body.index("return 0", branch_at)]
    assert "ENGINE_ENDPOINT" in branch, "the ssh diagnosis names the resolved endpoint"
    assert "ssh_forward_remedy" in branch or "make connect-refresh" in branch, (
        "the ssh diagnosis names a forward-opening remedy"
    )
    assert "REMOTE_INSTANCE_ID" not in branch, "the ssh diagnosis never names the EC2 id"
    assert "aws sso" not in branch, "the ssh diagnosis never names the sso login"
    assert "SSM" not in branch, "the ssh diagnosis never speaks of the SSM session"


def test_engine_diagnosis_ec2_branch_is_unchanged() -> None:
    """The SSM tail keeps its exact shape, after the ssh branch has returned."""
    body = _sh_function_body("rd_engine_diagnosis", _lib_text())
    tail_at = body.index("the SSM port forward to %s has dropped")
    tail = body[tail_at:]
    assert "$REMOTE_INSTANCE_ID" in tail
    assert "$forward_remedy" in tail


# ---------------------------------------------------------------------------
# container.sh: static pins over the script text.
# ---------------------------------------------------------------------------


def test_ensure_secrets_current_skips_the_aws_probe_for_ssh() -> None:
    """The freshness probe is an EC2 question; ssh returns before it."""
    body = _sh_function_body("rdc_ensure_secrets_current", _container_text())
    require_at = body.index("rd_require_remote_config")
    kind_at = body.index('if [ "${KIND:-}" = "ssh" ]')
    probe_at = body.index("ssm describe-parameters")
    assert require_at < kind_at < probe_at, (
        "the ssh branch must sit between the remote requirements and the aws probe"
    )
    branch = body[kind_at : body.index("return 0", kind_at)]
    assert "rd_aws" not in branch and "aws" not in branch, "the ssh branch makes no AWS call"
    assert "volume" in branch, (
        "the ssh branch states that config seeds into the volume at build time"
    )
    assert "rebuild" in branch, "the ssh branch states the payload is fresh on every rebuild"


def test_seed_volume_writes_the_payload_only_for_ssh() -> None:
    """The payload seed sits behind the ssh guard and writes both files via a
    stdin heredoc, escaped, chowned; the EC2 clone heredoc writes none of it."""
    body = _sh_function_body("rdc_seed_volume", _container_text())
    clone_at = body.index("<<SEED")
    guard_at = body.index('if [ "${KIND:-}" = "ssh" ]')
    call_at = body.index("rdc_seed_config_payload")
    ok_at = body.index('rd_ok "checkout seeded')
    assert clone_at < guard_at < call_at < ok_at, (
        "the payload seed must run after the clone succeeds and only under the ssh guard"
    )

    seed_end = body.index("\nSEED\n", clone_at)
    clone_heredoc = body[clone_at:seed_end]
    assert "shell.env" not in clone_heredoc and "aws-profile-map" not in clone_heredoc, (
        "the EC2 clone heredoc must carry no payload write"
    )

    payload = _sh_function_body("rdc_seed_config_payload", _container_text())
    assert "write_config_payload" in payload, "the payload is the shared transform's output"
    assert "mktemp" in payload and "umask 077" in payload, (
        "the payload is rendered under a private temp directory"
    )
    assert 'docker run --rm -i -v "${volume}:/workspaces"' in payload, (
        "the payload rides a second stdin heredoc into the same volume"
    )
    assert "sh -s <<PAYLOAD" in payload
    assert payload.count("rdc_sh_escape") >= 2, "the values are escaped for the heredoc they ride"
    assert "'${esc_env}'" in payload and "'${esc_map}'" in payload, (
        "both payload files' values ride the heredoc body, not an argv"
    )
    assert "${esc_workspace}/shell.env" in payload
    assert "${esc_workspace}/.devcontainer/aws-profile-map.json" in payload
    assert "chown ${CONTAINER_UID_GID}" in payload, (
        "the seeded files are chowned to the container user"
    )
    assert 'docker volume rm "$volume"' in payload and "rd_fail" in payload, (
        "a failed payload write removes the partial volume and diagnoses, like the clone failure"
    )


def test_up_refresh_uses_the_forwards_cli_for_ssh() -> None:
    """The ssh refresh drives `forwards open` with the ssh flag form; the ssm
    branch keeps its `transport connect` call and its sso remedy, unchanged."""
    body = _sh_function_body("rdc_up", _container_text())
    ssh_at = body.index('if [ "$backend" = "remote" ] && [ "${KIND:-}" = "ssh" ]')
    ssm_at = body.index('elif [ "$backend" = "remote" ]', ssh_at)
    ssh_branch = body[ssh_at:ssm_at]
    assert "devcontainer_config.forwards open" in ssh_branch
    for flag in ("--name", "--host", "--port", "--user", "--docker-addr", "--context"):
        assert flag in ssh_branch, f"the ssh refresh must carry the {flag} flag"
    assert "$ENGINE_HOST" in ssh_branch and "$ENGINE_USER" in ssh_branch, (
        "the flags come from the resolved block"
    )
    assert "transport connect" not in ssh_branch
    assert "aws sso login" not in ssh_branch, "no aws sso login remedy in the ssh branch"
    assert "make connect-refresh INSTANCE=" in ssh_branch, "the failure remedy names the refresh"

    ssm_branch = body[ssm_at : body.index("\n    else\n", ssm_at)]
    assert "devcontainer_config.transport connect" in ssm_branch, "the ssm branch is unchanged"
    assert "aws sso login" in ssm_branch, "the sso remedy belongs to the ssm branch alone"

    # The Windows guidance is keyed on a unix-path docker_addr inside rdc_up.
    assert 'case "${ENGINE_DOCKER_ADDR:-}" in' in body
    assert "tcp://localhost:2375" in body, (
        "the guidance names the tcp destination a Windows endpoint needs"
    )


# ---------------------------------------------------------------------------
# config-payload.sh: the transform, behaviorally.
# ---------------------------------------------------------------------------


def _run_write_config_payload(
    tmp_path: Path, source_env: Path, profile_map: Path, project_name: str
) -> subprocess.CompletedProcess:
    dest = tmp_path / "payload-dest"
    script = (
        "set -euo pipefail\n"
        f'source "{_LIB_SH}"\n'
        f'source "{_CONFIG_PAYLOAD_SH}"\n'
        f'SHELL_ENV_SOURCE="{source_env}"\n'
        f'PROFILE_MAP_SOURCE="{profile_map}"\n'
        f'PROJECT_NAME="{project_name}"\n'
        f'write_config_payload "{dest}"\n'
    )
    assert _BASH_BIN is not None
    env = dict(os.environ)
    env.pop("CONTAINER_WORKSPACES_ROOT", None)
    return subprocess.run(
        [_BASH_BIN, "-c", script],
        capture_output=True,
        text=True,
        timeout=_timeout_seconds(),
        env=env,
    )


def _write_fixture_sources(tmp_path: Path) -> tuple[Path, Path]:
    source_env = tmp_path / "shell.env"
    source_env.write_text(
        "# a leading comment\n"
        "\n"
        "export HOST_PROXY='true'\n"
        "export HOST_PROXY_URL='http://proxy.example.com:8080'\n"
        "export HTTP_PROXY='http://proxy.example.com:8080'\n"
        "export HTTPS_PROXY='http://proxy.example.com:8080'\n"
        "export http_proxy='http://proxy.example.com:8080'\n"
        "export https_proxy='http://proxy.example.com:8080'\n"
        "export NO_PROXY='localhost'\n"
        "export no_proxy='localhost'\n"
        'export PATH="$HOME/.asdf/shims:$PATH"\n'
        'export PATH="$HOME/.localscripts/bin:$PATH"\n'
        "export BASH_ENV='/local/path/shell.env'\n"
        "export LOCAL_DOCKER_CONTEXT='orbstack'\n"
        "export REMOTE_AWS_REGION='us-east-1'\n",
        encoding="utf-8",
    )
    profile_map = tmp_path / "aws-profile-map.json"
    profile_map.write_text(
        '{"profiles": [{"name": "dev", "region": "us-east-1"}]}\n', encoding="utf-8"
    )
    return source_env, profile_map


def test_config_payload_applies_the_remote_transform(tmp_path: Path) -> None:
    """HOST_PROXY forced off, BASH_ENV repointed, the noise stripped, the
    untouched lines and the profile map through."""
    source_env, profile_map = _write_fixture_sources(tmp_path)
    result = _run_write_config_payload(tmp_path, source_env, profile_map, "general-dev")
    assert result.returncode == 0, (
        f"write_config_payload must succeed; stderr={result.stderr[:400]!r}"
    )

    dest = tmp_path / "payload-dest"
    transformed = (dest / "shell.env").read_text(encoding="utf-8")
    assert "export HOST_PROXY='false'" in transformed
    assert "export BASH_ENV='/workspaces/general-dev/shell.env'" in transformed, (
        "BASH_ENV is repointed at the remote workspace copy"
    )
    for gone in (
        "HOST_PROXY_URL",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "http_proxy",
        "https_proxy",
        "NO_PROXY",
        "no_proxy",
        ".asdf",
        ".localscripts",
    ):
        assert gone not in transformed, f"{gone} must be stripped"
    assert "#" not in transformed, "comment lines are stripped"
    assert all(line.strip() for line in transformed.splitlines()), "blank lines are stripped"
    assert "export LOCAL_DOCKER_CONTEXT='orbstack'" in transformed, "unrelated lines pass through"
    assert "export REMOTE_AWS_REGION='us-east-1'" in transformed, "unrelated lines pass through"

    copied = dest / ".devcontainer" / "aws-profile-map.json"
    assert copied.read_bytes() == profile_map.read_bytes(), "the profile map is copied verbatim"


def test_config_payload_fails_fast_on_a_missing_source(tmp_path: Path) -> None:
    source_env, profile_map = _write_fixture_sources(tmp_path)
    result = _run_write_config_payload(
        tmp_path, tmp_path / "no-such-shell.env", profile_map, "general-dev"
    )
    assert result.returncode != 0, "a missing shell.env must fail the render"
    assert "shell.env not found" in result.stderr, f"stderr={result.stderr[:400]!r}"

    result = _run_write_config_payload(
        tmp_path, source_env, tmp_path / "no-such-map.json", "general-dev"
    )
    assert result.returncode != 0, "a missing profile map must fail the render"
    assert "aws-profile-map.json not found" in result.stderr, f"stderr={result.stderr[:400]!r}"


def test_push_secrets_consumes_the_one_shared_transform() -> None:
    """The sed rules live in config-payload.sh; push-secrets.sh sources them."""
    text = _push_secrets_text()
    assert "config-payload.sh" in text, "push-secrets.sh must source the shared transform"
    assert "write_config_payload" in text, "both of its publish sources come from it"
    assert "s|^export HOST_PROXY=" not in text, (
        "the inline transform is gone; a second copy would drift from the seed's"
    )


def test_the_payload_heredoc_round_trips_values_with_quotes(tmp_path: Path) -> None:
    """The seed's write discipline actually round-trips: an rdc_sh_escape'd
    multi-line value carrying a raw single quote survives the heredoc intact.

    Runs the exact two steps the seed runs -- escape on this side, write
    inside the heredoc -- against the local sh instead of a docker run,
    which is the only part of the seed this hermetic file cannot execute.
    """
    container = _container_text()
    # container.sh cannot be sourced (its prelude resolves and dispatches);
    # the one function this test needs is lifted out of it, verbatim.
    escape_fn = f"rdc_sh_escape() {{\n{_sh_function_body('rdc_sh_escape', container)}\n}}\n"
    raw = 'export QUOTED="it\'s escaped"\nexport LAST=1\n'
    raw_file = tmp_path / "raw.env"
    raw_file.write_text(raw, encoding="utf-8")
    out_file = tmp_path / "round-tripped.env"
    script = (
        "set -euo pipefail\n"
        f"{escape_fn}"
        f'esc_env="$(rdc_sh_escape "$(cat "{raw_file}")")"\n'
        "sh -s <<PAYLOAD\n"
        f"printf '%s\\n' '${{esc_env}}' > '{out_file}'\n"
        "PAYLOAD\n"
    )
    assert _BASH_BIN is not None
    result = subprocess.run(
        [_BASH_BIN, "-c", script],
        capture_output=True,
        text=True,
        timeout=_timeout_seconds(),
    )
    assert result.returncode == 0, (
        f"the heredoc round trip must succeed; stderr={result.stderr[:400]!r}"
    )
    assert out_file.read_text(encoding="utf-8") == raw, (
        "the value must survive escape, heredoc expansion and the inner sh byte-for-byte"
    )


# ---------------------------------------------------------------------------
# push-secrets.sh: the ssh branch, statically.
# ---------------------------------------------------------------------------


def test_push_secrets_ssh_branch_resolves_first_and_delivers_over_stdin() -> None:
    text = _push_secrets_text()
    resolve_at = text.index("rd_resolve_instance")
    branch_at = text.index('if [ "${KIND:-}" = "ssh" ]')
    assert resolve_at < branch_at, "the resolver runs before the ssh delivery"

    delivery = _sh_function_body("rd_deliver_config_payload_to_container", text)
    assert 'docker ps -aq --filter "label=devcontainer.project=${PROJECT_NAME}"' in delivery, (
        "the container is found the same way container.sh finds it"
    )
    assert "docker exec -i" in delivery and "cat >" in delivery, (
        "values ride stdin, never a docker exec's argv"
    )
    assert '< "${RD_PAYLOAD_DIR}/shell.env"' in delivery, (
        "shell.env is redirected from the rendered payload"
    )
    assert '< "${RD_PAYLOAD_DIR}/.devcontainer/aws-profile-map.json"' in delivery
    assert "[ -s " in delivery, "the delivery verifies the files landed non-empty"
    assert "${CONTAINER_WORKSPACES_ROOT}/${PROJECT_NAME}" in delivery, (
        "the workspace path is the shared derivation, not a re-derived one"
    )
    assert ': "${CONTAINER_USER:=vscode}"' in text, (
        "the delivery execs as CONTAINER_USER under set -u, so the default must "
        "be assigned in this script and not only in container.sh"
    )
    assert '"/devcontainer/${PROJECT_NAME}"' not in text, (
        "no Parameter Store prefix is derived from PROJECT_NAME (the standing pin)"
    )


def test_push_secrets_ssh_branch_never_touches_parameter_store() -> None:
    text = _push_secrets_text()
    branch_at = text.index('if [ "${KIND:-}" = "ssh" ]')
    branch_end = text.index("\nfi\n", branch_at)
    branch = text[branch_at:branch_end]
    delivery = _sh_function_body("rd_deliver_config_payload_to_container", text)
    for scoped in (branch, delivery):
        assert "put-parameter" not in scoped
        assert "rd_aws" not in scoped
        assert "rd_check_aws_auth" not in scoped
    # And the EC2 branch keeps its publish path: the aws requirement and the
    # put-parameter helper still follow the ssh branch.
    assert text.index("rd_require_cmd aws") > branch_end
    assert "rd_put_parameter" in text


def test_config_payload_is_sourced_by_both_consumers() -> None:
    container = _container_text()
    push = _push_secrets_text()
    for name, text in (("container.sh", container), ("push-secrets.sh", push)):
        assert "config-payload.sh" in text, f"{name} must source the shared transform"
        assert "write_config_payload" in text, f"{name} must render through it"


# ---------------------------------------------------------------------------
# push-secrets.sh: the ssh delivery, behaviorally.
#
# The static pins above describe the delivery without ever running it, which
# is exactly how a reference to a variable the script never assigned
# ($CONTAINER_USER, defaulted only in container.sh) survived: under set -u
# the first exec died, and the run delivered nothing. These tests run
# push-secrets.sh end to end against the stand-in resolver, a docker stub
# that records every argv and captures `exec -i`'s stdin, and real fixture
# sources -- then assert the payloads themselves reached the container paths,
# not merely that the exit status said so.
# ---------------------------------------------------------------------------


# The delivery run's project name, pinned so the workspace assertions do not
# depend on the checkout directory's own name, which is what PROJECT_NAME
# falls back to.
_DELIVERY_PROJECT = "delivery-proj"

_DELIVERY_WORKSPACE = f"/workspaces/{_DELIVERY_PROJECT}"


def _delivery_container_id() -> str:
    return uuid.uuid4().hex * 2


def _fake_docker_bin(bin_dir: Path) -> None:
    """The docker stub the delivery runs answer to.

    Appends one line of joined argv per call to $FAKE_DOCKER_LOG, captures
    the stdin of each `exec -i` call into
    $FAKE_DOCKER_CAPTURE_DIR/stdin-exec-<n> (n counts exec calls), answers
    `ps` and `inspect` with $FAKE_DOCKER_CONTAINER_ID, and succeeds at
    everything, refusing nothing. FAKE_DOCKER_INSPECT_FAILS=1 turns `inspect`
    into a failure instead, for the refused-name path.
    """
    fake = bin_dir / "docker"
    fake.write_text(
        "#!/bin/sh\n"
        "# Test stub: record argv, capture exec stdin, refuse nothing.\n"
        "set -u\n"
        'printf \'%s\\n\' "$*" >> "$FAKE_DOCKER_LOG"\n'
        'if [ "$1" = context ]; then\n'
        "  printf '%s\\n' \"${FAKE_DOCKER_CONTEXT:-stub-context}\"\n"
        "  exit 0\n"
        "fi\n"
        'if [ "$1" = inspect ] && [ "${FAKE_DOCKER_INSPECT_FAILS:-0}" = 1 ]; then\n'
        "  exit 1\n"
        "fi\n"
        'if [ "$1" = exec ]; then\n'
        '  n="$(grep -c \'^exec \' "$FAKE_DOCKER_LOG")"\n'
        '  for arg in "$@"; do\n'
        '    if [ "$arg" = -i ]; then\n'
        '      cat > "${FAKE_DOCKER_CAPTURE_DIR}/stdin-exec-${n}"\n'
        "      break\n"
        "    fi\n"
        "  done\n"
        "fi\n"
        'if [ "$1" = ps ] || [ "$1" = inspect ]; then\n'
        "  printf '%s\\n' \"$FAKE_DOCKER_CONTAINER_ID\"\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)


# Every variable the delivery run must default or resolve on its own: the
# assertions have to be able to tell the script's own defaults (and the
# stand-in resolver's block) from whatever this machine's environment leaks.
_DELIVERY_RUN_UNSET = (
    "CONTAINER_USER",
    "CONTAINER",
    "CONTAINER_WORKSPACES_ROOT",
    "PROJECT_NAME",
    "SHELL_ENV_SOURCE",
    "PROFILE_MAP_SOURCE",
    "REPO_SHELL_ENV",
    "INSTANCE",
    "ENGINE",
    "KIND",
    "REMOTE_DOCKER_CONTEXT",
    "REMOTE_INSTANCE_ID",
    "RD_INSTANCE_RESOLVED",
    "ENGINE_HOST",
    "ENGINE_PORT",
    "ENGINE_USER",
    "ENGINE_DOCKER_ADDR",
    "ENGINE_ENDPOINT",
)


def _run_push_secrets(
    tmp_path: Path, bin_dir: Path, extra_env: dict[str, str] | None = None
) -> tuple[subprocess.CompletedProcess, list[str], Path]:
    """push-secrets.sh end to end: stand-in resolver, docker stub, real fixtures.

    The resolver answers `_resolver_block`'s ssh block; the fixture sources
    `_write_fixture_sources` wrote are wired in through SHELL_ENV_SOURCE and,
    so `rd_load_config`'s optional read of the repository's own shell.env
    cannot leak machine values into the run, REPO_SHELL_ENV; the environment
    is stripped of every variable the run must default or resolve itself.
    Returns the completed run, the stub's argv log split into lines, and the
    stdin capture directory.
    """
    _fake_docker_bin(bin_dir)
    log = tmp_path / "docker-calls.log"
    capture = tmp_path / "captured"
    capture.mkdir(exist_ok=True)
    env = dict(os.environ)
    for var in _DELIVERY_RUN_UNSET:
        env.pop(var, None)
    env.update(
        PATH=f"{bin_dir}:{env.get('PATH', '')}",
        PROJECT_NAME=_DELIVERY_PROJECT,
        SHELL_ENV_SOURCE=str(tmp_path / "shell.env"),
        PROFILE_MAP_SOURCE=str(tmp_path / "aws-profile-map.json"),
        REPO_SHELL_ENV=str(tmp_path / "shell.env"),
        FAKE_DOCKER_LOG=str(log),
        FAKE_DOCKER_CAPTURE_DIR=str(capture),
        FAKE_DOCKER_CONTAINER_ID=_delivery_container_id(),
    )
    env.update(extra_env or {})
    assert _BASH_BIN is not None
    result = subprocess.run(
        [_BASH_BIN, str(_PUSH_SECRETS_SH)],
        capture_output=True,
        text=True,
        timeout=_timeout_seconds(),
        env=env,
    )
    return result, log.read_text(encoding="utf-8").splitlines(), capture


def _delivery_setup(tmp_path: Path) -> Path:
    """Fixture sources plus the stand-in resolver; returns the stub bin dir."""
    _write_fixture_sources(tmp_path)
    return Path(_fake_resolver_bin(tmp_path, _resolver_block(tmp_path, kind="ssh")))


def test_push_secrets_ssh_delivery_reaches_the_running_container(tmp_path: Path) -> None:
    """The run that would have caught the unbound CONTAINER_USER: the ssh
    delivery exits 0 and both payload files land in the running container.

    With the stand-in resolver exporting KIND=ssh and the docker stub
    refusing nothing, push-secrets.sh must exec as the default container
    user (vscode), write both files at the shared workspace derivation
    through `cat >`, and reach its own success line. The captured stdin
    proves the rendered transform -- not just an exit status -- reached the
    container: HOST_PROXY forced off, BASH_ENV repointed at the delivered
    copy, the noise stripped, pass-through lines intact, the profile map
    verbatim.
    """
    bin_dir = _delivery_setup(tmp_path)
    result, calls, capture = _run_push_secrets(tmp_path, bin_dir)

    assert result.returncode == 0, (
        f"the delivery must succeed end to end; stderr={result.stderr[:400]!r}"
    )
    assert "Configuration delivered to the running" in result.stdout, (
        f"the run must reach its own success line; stdout={result.stdout[-400:]!r}"
    )

    exec_calls = [call for call in calls if call.startswith("exec ")]
    assert len(exec_calls) == 3, f"two stdin writes and one verify expected; got {calls}"
    for call in exec_calls:
        assert "-u vscode" in call, (
            f"the default container user must be defined in this script and used; got {call!r}"
        )
    assert f"mkdir -p '{_DELIVERY_WORKSPACE}/.devcontainer'" in exec_calls[0]
    assert f"cat > '{_DELIVERY_WORKSPACE}/shell.env'" in exec_calls[0]
    assert f"cat > '{_DELIVERY_WORKSPACE}/.devcontainer/aws-profile-map.json'" in exec_calls[1]
    assert "[ -s " in exec_calls[2], "the delivery verifies both files landed non-empty"

    delivered_env = (capture / "stdin-exec-1").read_text(encoding="utf-8")
    assert "export HOST_PROXY='false'" in delivered_env, (
        "the transform must reach the container, not only the payload dir"
    )
    assert f"export BASH_ENV='{_DELIVERY_WORKSPACE}/shell.env'" in delivered_env, (
        "BASH_ENV must be repointed at the copy the container just received"
    )
    assert "HTTP_PROXY" not in delivered_env and "#" not in delivered_env, (
        "the container receives the stripped remote flavor"
    )
    assert "export LOCAL_DOCKER_CONTEXT='orbstack'" in delivered_env, "unrelated lines pass through"
    delivered_map = (capture / "stdin-exec-2").read_bytes()
    assert delivered_map == (tmp_path / "aws-profile-map.json").read_bytes(), (
        "the profile map must reach the container verbatim"
    )


def test_push_secrets_ssh_delivery_honors_a_named_container_and_a_pinned_user(
    tmp_path: Path,
) -> None:
    """CONTAINER=<name> selects the receiver by inspect, bypassing the label
    scan, and a caller-pinned CONTAINER_USER wins over the default.

    The multi-container refusal names CONTAINER=<name> as its remedy, so the
    delivery has to honor it: this run never reaches `docker ps` (the stub's
    log proves that), execs against the container the named one resolves to,
    and rides `-u deployer` from the caller's environment into every exec --
    the `:=` default may only fill an unset variable, never clobber a pinned
    one.
    """
    bin_dir = _delivery_setup(tmp_path)
    result, calls, capture = _run_push_secrets(
        tmp_path,
        bin_dir,
        extra_env={"CONTAINER": "the-named-one", "CONTAINER_USER": "deployer"},
    )

    assert result.returncode == 0, f"stderr={result.stderr[:400]!r}"
    assert calls[0].startswith("context show") and calls[1].startswith("inspect -f"), calls
    assert "the-named-one" in calls[1], "the name the caller chose is what gets inspected"
    assert not [call for call in calls if call.startswith("ps ")], (
        "a named container must bypass the label scan entirely"
    )
    exec_calls = [call for call in calls if call.startswith("exec ")]
    assert len(exec_calls) == 3, f"two stdin writes and one verify expected; got {calls}"
    for call in exec_calls:
        assert "-u deployer" in call, f"the pinned user must reach every exec; got {call!r}"
    assert f"cat > '{_DELIVERY_WORKSPACE}/shell.env'" in exec_calls[0]
    assert f"cat > '{_DELIVERY_WORKSPACE}/.devcontainer/aws-profile-map.json'" in exec_calls[1]
    assert (
        (capture / "stdin-exec-1")
        .read_text(encoding="utf-8")
        .startswith("export HOST_PROXY='false'")
    )
    assert (capture / "stdin-exec-2").read_bytes() == (
        tmp_path / "aws-profile-map.json"
    ).read_bytes()


def test_push_secrets_ssh_delivery_refuses_a_container_that_does_not_exist(
    tmp_path: Path,
) -> None:
    """A named container must answer `docker inspect`, or the run refuses
    with rdc_require_container's discipline before anything is delivered.

    The refusal must not fall through to the label scan -- that would
    deliver into a container the operator just ruled out -- and no exec call
    may follow it.
    """
    bin_dir = _delivery_setup(tmp_path)
    result, calls, _capture = _run_push_secrets(
        tmp_path,
        bin_dir,
        extra_env={"CONTAINER": "missing-container", "FAKE_DOCKER_INSPECT_FAILS": "1"},
    )

    assert result.returncode != 0, "a name nothing answers to must refuse"
    assert "There is no container named 'missing-container'" in result.stderr, (
        f"the refusal must name the container and its discipline; stderr={result.stderr[-400:]!r}"
    )
    assert not [call for call in calls if call.startswith("exec ")], (
        "nothing is delivered once the name is refused"
    )
