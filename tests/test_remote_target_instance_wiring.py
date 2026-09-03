"""Every remote make target accepts `INSTANCE`, and resolves it exactly once.

Before this wiring, `container.sh` and `push-secrets.sh` each derived the
docker context and the Parameter Store prefix from `PROJECT_NAME`
independently. Two callers could therefore address different instances while
each believed it was addressing "the" one, and the failure mode is silent:
secrets published under one prefix and read from another look like a missing
value, not a mis-addressed one.

`rd_resolve_instance` is now the single shell entry point. It calls
`devcontainer_config.cli resolve-instance` once, exports the address block it
prints, and translates failure into the repository's own `rd_fail` shape. It
adds no resolution policy of its own -- the four-step order lives in one place,
in Python, where it is tested.

Three properties are pinned here.

Resolution never happens at Make parse time. `INSTANCE` is a plain variable
passed into recipes, because resolving it at parse time would shell out on
every `make` invocation, including `make help`, in a repository that may have
no instances configured at all.

Every recipe that reaches the remote engine passes `INSTANCE` through. A target
that forgets is exactly the drift this replaced.

And the resolver runs once per invocation, guarded by `RD_INSTANCE_RESOLVED`,
so a script that sources `lib.sh` and dispatches several helpers does not pay
for, or risk disagreeing with, repeated resolutions.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_MAKEFILE = _REPO_ROOT / "Makefile"
_RD_DIR = _REPO_ROOT / ".devcontainer" / "remote-docker"
_LIB_SH = _RD_DIR / "lib.sh"
_CONTAINER_SH = _RD_DIR / "container.sh"
_PUSH_SECRETS_SH = _RD_DIR / "push-secrets.sh"
_CERTS_SH = _RD_DIR / "certs.sh"

_TIMEOUT_ENV_VAR = "MAKE_INSTANCE_WIRING_TEST_TIMEOUT_SECONDS"
_DEFAULT_TIMEOUT_SECONDS = 60.0


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


def _makefile_text() -> str:
    return _MAKEFILE.read_text(encoding="utf-8")


def _remote_recipe_lines() -> list[str]:
    """Every recipe line invoking one of the instance-aware entry scripts."""
    return [
        line
        for line in _makefile_text().splitlines()
        if line.startswith("\t") and re.search(r"\$\((CONTAINER_SH|SECRETS_SH|CERTS_SH)\)", line)
    ]


def test_remote_recipes_exist_so_this_module_cannot_pass_vacuously() -> None:
    lines = _remote_recipe_lines()
    assert len(lines) >= 10, (
        f"expected many remote recipes, parsed {len(lines)}; the parser may have drifted "
        "and every assertion below would pass against an empty set"
    )


def test_every_remote_recipe_passes_instance_through() -> None:
    missing = [line.strip() for line in _remote_recipe_lines() if 'INSTANCE="$(INSTANCE)"' not in line]
    assert not missing, (
        "these remote recipes do not pass INSTANCE, so `INSTANCE=<name> make <target>` would "
        f"silently act on whatever the resolver defaults to: {missing}"
    )


def test_instance_is_declared_once_and_defaults_empty() -> None:
    """Empty, so the resolver applies its own order rather than the Makefile guessing."""
    declarations = re.findall(r"^INSTANCE \?=.*$", _makefile_text(), re.MULTILINE)
    assert len(declarations) == 1, f"INSTANCE must be declared exactly once, found {declarations}"
    assert declarations[0].strip() == "INSTANCE ?=", (
        f"INSTANCE must default to empty so the resolver decides; got {declarations[0]!r}"
    )


def test_the_makefile_never_resolves_at_parse_time() -> None:
    """No `$(shell ... resolve-instance ...)`: that would run on every make call."""
    text = _makefile_text()
    assert not re.search(r"\$\(shell[^)]*resolve-instance", text), (
        "resolve-instance must not run at Make parse time; it would shell out on every "
        "invocation including `make help`, in a repo that may have no instances at all"
    )


def test_make_help_does_not_resolve_an_instance() -> None:
    """Observable proof of the parse-time rule: help works with nothing configured."""
    result = subprocess.run(
        ["make", "help"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=_timeout_seconds(),
    )
    assert result.returncode == 0, (
        f"make help must work with no instance configured; stderr={result.stderr[:400]!r}"
    )
    for token in ("resolve", "No instances configured"):
        assert token not in result.stderr, (
            f"make help triggered instance resolution: {result.stderr[:300]!r}"
        )


def test_resolution_is_guarded_against_running_twice() -> None:
    lib = _LIB_SH.read_text(encoding="utf-8")
    assert "RD_INSTANCE_RESOLVED" in lib, (
        "rd_resolve_instance must guard against repeated resolution; a script that dispatches "
        "several helpers would otherwise resolve repeatedly and could disagree with itself"
    )


def test_the_resolver_is_the_only_policy_holder() -> None:
    """No shell script may re-derive an address the resolver already computes."""
    for path in (_CONTAINER_SH, _PUSH_SECRETS_SH, _CERTS_SH):
        text = path.read_text(encoding="utf-8")
        assert not re.search(r'"/devcontainer/\$\{PROJECT_NAME\}"', text), (
            f"{path.name} still derives a Parameter Store prefix from PROJECT_NAME instead of "
            "consuming the resolved PARAMETER_PREFIX; that is the split this replaced"
        )


def test_push_secrets_resolves_before_any_aws_call() -> None:
    """Publishing to the wrong prefix writes secrets where another instance reads them."""
    text = _PUSH_SECRETS_SH.read_text(encoding="utf-8")
    resolve_at = text.index("rd_resolve_instance")
    first_aws = min(
        (text.index(token) for token in ("rd_aws ", "aws ssm") if token in text),
        default=len(text),
    )
    assert resolve_at < first_aws, (
        "push-secrets.sh must resolve the instance before its first AWS call, or it can "
        "publish under a prefix belonging to a different instance"
    )


def test_certs_sh_resolves_before_any_aws_call() -> None:
    """Publishing under the wrong prefix hands an instance a certificate it will reject."""
    text = _CERTS_SH.read_text(encoding="utf-8")
    resolve_at = text.index("rd_resolve_instance")
    first_aws = min(
        (text.index(token) for token in ("rd_check_aws_auth", "aws ssm") if token in text),
        default=len(text),
    )
    assert resolve_at < first_aws, (
        "certs.sh must resolve the instance before its first AWS call, or it can publish "
        "TLS material under a prefix belonging to a different instance"
    )


def test_certs_sh_writes_material_under_the_resolved_certificate_directory() -> None:
    """The resolver owns spec Section 9's certificate directory; certs.sh must not re-derive it."""
    text = _CERTS_SH.read_text(encoding="utf-8")
    assert 'dirname "$CERTS_DIR"' in text, (
        "certs.sh must take the certificate root from the resolved CERTS_DIR, or an operator "
        "who sets DOCKER_CONFIG writes material under one directory and reads it from another"
    )


def test_container_sh_consumes_the_resolved_block() -> None:
    text = _CONTAINER_SH.read_text(encoding="utf-8")
    assert "rd_resolve_instance" in text
    assert 'REMOTE_DOCKER_CONTEXT="$DOCKER_CONTEXT"' in text, (
        "container.sh must take its docker context from the resolved block"
    )


def test_resolver_failure_is_reported_through_rd_fail_with_remedies() -> None:
    """A resolution failure must name both remedies, not print a traceback."""
    lib = _LIB_SH.read_text(encoding="utf-8")
    body = lib[lib.index("rd_resolve_instance() {"):]
    body = body[: body.index("\n}\n")]
    assert "rd_fail" in body, "failure must go through rd_fail, not a bare exit"
    assert "INSTANCE=<name>" in body, "the remedy must show naming an instance explicitly"
    assert "DEFAULT_REMOTE_INSTANCE" in body, "the remedy must show setting a default"
    assert "make instances" in body, "the remedy should point at the listing"


def test_the_backend_is_classified_only_after_the_instance_is_resolved() -> None:
    """`rdc_backend` compares against a value only the resolver knows.

    Classifying first compared the active docker context against whatever
    `config.env` defaulted `REMOTE_DOCKER_CONTEXT` to, so every instance whose
    context did not match that one default was classified `local`. `make build`
    then took the bind-mount path and asked the remote engine to mount a path
    that exists only on the laptop -- observed against a real instance, where
    the image built and container creation failed with "bind source path does
    not exist".

    The ordering is the fix, so the ordering is what is pinned.
    """
    # Scoped to the top-level dispatch block. Earlier `rdc_backend` calls sit
    # inside function bodies, which run when those functions are called, long
    # after this block has already assigned the value they read.
    text = _CONTAINER_SH.read_text(encoding="utf-8")
    dispatch = text[text.index('RDC_COMMAND="${1:-}"') :]
    resolve_at = dispatch.index("rd_resolve_instance_quiet")
    assign_at = dispatch.index('REMOTE_DOCKER_CONTEXT="$DOCKER_CONTEXT"')
    classify_at = dispatch.index('[ "$(rdc_backend)" = "remote" ]')
    assert resolve_at < assign_at < classify_at, (
        "the backend must be classified only after REMOTE_DOCKER_CONTEXT has been assigned "
        "from the resolved block, or the comparison reads a config default"
    )


def test_a_repository_with_no_configured_instance_stays_local() -> None:
    """Resolution must not be fatal at the point the backend is merely being classified."""
    lib = _LIB_SH.read_text(encoding="utf-8")
    assert "rd_resolve_instance_quiet()" in lib, (
        "a non-fatal resolver is required: a repository configuring no instances is a "
        "legitimately local one and must still build against its local engine"
    )
    container = _CONTAINER_SH.read_text(encoding="utf-8")
    assert "if rd_resolve_instance_quiet; then" in container, (
        "container.sh must tolerate an unresolvable instance while classifying"
    )


def test_the_fatal_resolver_still_names_both_remedies() -> None:
    """Splitting out the quiet form must not have dropped the diagnosis."""
    lib = _LIB_SH.read_text(encoding="utf-8")
    fatal = lib[lib.index("rd_resolve_instance() {") :]
    assert "INSTANCE=<name> make <target>" in fatal
    assert "DEFAULT_REMOTE_INSTANCE=<name>" in fatal


def test_the_resolved_parameter_prefix_reaches_the_container() -> None:
    """The container must be told the prefix, not left to derive one of its own.

    `.devcontainer/postcreate-wrapper.sh` defaults `DEVCONTAINER_SSM_PREFIX` to
    `/devcontainer/$(basename "$(pwd)")` -- the workspace folder name. That is a
    second, independent derivation of the address the resolver already owns, and
    when the two disagree the container bootstraps from a different environment's
    secrets. Observed against a real instance: with the instance named `sandbox`,
    the container read `/devcontainer/general-dev/shell.env`, an unrelated
    environment that happens to share the project's folder name.

    Forwarding it through the generated override is what makes the container's
    prefix the resolved one rather than a guess.
    """
    text = _CONTAINER_SH.read_text(encoding="utf-8")
    assert '--arg ssmprefix "${DEVCONTAINER_SSM_PREFIX}"' in text, (
        "the override generator must be given the resolved prefix"
    )
    assert "DEVCONTAINER_SSM_PREFIX: $ssmprefix" in text, (
        "the override must set containerEnv.DEVCONTAINER_SSM_PREFIX, or the container "
        "falls back to deriving a prefix from its workspace folder name"
    )


def test_the_override_preserves_any_container_env_the_configuration_declares() -> None:
    """Adding the prefix must not drop what devcontainer.json already set."""
    text = _CONTAINER_SH.read_text(encoding="utf-8")
    assert "((.containerEnv // {}) + {DEVCONTAINER_SSM_PREFIX: $ssmprefix})" in text, (
        "containerEnv must be merged, not replaced; a bare assignment would discard "
        "every variable the checked-in configuration declares"
    )
