"""NAME= on build/up names a container at creation, not after the fact.

`make rename NAME=x` has always renamed an existing container; the operator
flow that left was naming one on the way in, so `make build NAME=x` and
`make up NAME=x` carry the name through creation. This suite pins the
container.sh wiring: the fail-fast validator (docker's name shape, plus the
collision refusal), the validation point in build and up (before any image
builds, not after), the application point (between the backend build and
the credential push, so the status a build ends on shows the final name),
and the two orderings the semantics depend on -- apply treats
already-named as success without running the collision check that would
fire on the container's own name, and rename checks same-name before the
validator for the same reason.

Static pins over the script text, the same approach
`tests/test_container_verify.py` takes for the behavior that lives in
container.sh, plus a stub-docker behavioral pair for the collision probe --
the PATH-earlier fake docker recording argv, the style
`tests/test_engine_container_path.py` established -- because that probe's
quiet form is exactly what a static pin cannot prove: the translating
wrapper it replaces read correctly in prose while ending the whole run on
an unused name, the normal, expected answer. No real docker in this file
(AC-TEST-004).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CONTAINER_SH = _REPO_ROOT / ".devcontainer" / "remote-docker" / "container.sh"


@pytest.fixture(scope="module")
def container_text() -> str:
    return _CONTAINER_SH.read_text(encoding="utf-8")


def _function_body(name: str, text: str) -> str:
    start = text.index(f"{name}() {{")
    end = text.index("\n}\n", start)
    return text[start:end]


def test_the_validator_enforces_dockers_name_shape(container_text: str) -> None:
    """Outside [a-zA-Z0-9][a-zA-Z0-9_.-]* is refused, stating the rule."""
    body = _function_body("rdc_validate_requested_name", container_text)
    assert "tr -cd 'a-zA-Z0-9_.-'" in body, (
        "the shape check must strip every allowed character and compare, so any "
        "leftover character is a refusal"
    )
    assert "not a name docker accepts" in body, "the refusal names what docker accepts"


_QUIET_PROBE = "docker inspect -f '{{.Id}}' \"$NAME\" 2> /dev/null"


def test_the_validator_refuses_a_stolen_name(container_text: str) -> None:
    """A name another container answers to is refused with the listing remedy."""
    body = _function_body("rdc_validate_requested_name", container_text)
    assert _QUIET_PROBE in body, (
        "the collision check must probe with plain docker and stderr dropped, "
        "the established quiet form of rdc_require_container's named branch"
    )
    assert "already exists on this engine" in body
    assert "make rename NAME=<other>" in body


def test_the_collision_probe_is_never_the_translating_wrapper(
    container_text: str,
) -> None:
    """rd_docker must not appear in the validator.

    An unused name -- the normal, expected answer -- is docker exiting
    non-zero, and rd_docker's translator reports every non-zero as a failure
    and ends the run, which is how `make build NAME=<unused>` died at the
    collision check instead of building.
    """
    body = _function_body("rdc_validate_requested_name", container_text)
    # The negative pin reads code, not the comment that explains the rule --
    # the same non-comment discipline test_engine_container_path.py applies.
    code = "\n".join(line for line in body.splitlines() if not line.strip().startswith("#"))
    assert "rd_docker" not in code, (
        "the collision check is an existence probe whose non-zero exit is the "
        "expected answer; the translating wrapper turns that answer into a run-"
        "ending failure before the refusal branch can even be evaluated"
    )


def test_build_validates_before_anything_expensive(container_text: str) -> None:
    """The NAME check is instant; it runs before the build it would invalidate."""
    build = _function_body("rdc_build", container_text)
    prereqs_at = build.index("rdc_build_prereqs")
    validate_at = build.index("rdc_validate_requested_name")
    guard_at = build.index("already exists on this engine")
    assert prereqs_at < validate_at < guard_at, (
        "validate NAME after the prereqs but before the existing-container guard: "
        "an invalid NAME must fail before a guard message or an image build"
    )


def test_build_applies_the_name_before_pushing_credentials(container_text: str) -> None:
    """The name lands between the backend build and the creds push."""
    build = _function_body("rdc_build", container_text)
    local_at = build.index("rdc_build_local")
    apply_at = build.index("rdc_apply_requested_name")
    creds_at = build.index("rdc_push_creds")
    assert local_at < apply_at < creds_at, (
        "the name must be applied after the container exists and before creds, so "
        "the status a build ends on shows the final name"
    )


def test_up_validates_and_names_on_both_branches(container_text: str) -> None:
    """up refuses a bad NAME early and names on the start branch; build names its own."""
    up = _function_body("rdc_up", container_text)
    require_at = up.index("rd_require_cmd docker")
    validate_at = up.index("rdc_validate_requested_name")
    apply_at = up.rindex("rdc_apply_requested_name")
    creds_at = up.rindex("rdc_push_creds")
    assert require_at < validate_at, "the NAME check belongs with the other early checks"
    assert apply_at < creds_at, "the name lands before the creds push on the start branch"


def test_apply_treats_already_named_as_success_without_a_collision_check(
    container_text: str,
) -> None:
    """Repeated 'make up NAME=x' is idempotent; the collision check must not fire on it."""
    apply_body = _function_body("rdc_apply_requested_name", container_text)
    named_at = apply_body.index("container is named")
    validate_at = apply_body.index("rdc_validate_requested_name")
    assert named_at < validate_at, (
        "the already-named early return must precede the validator, whose "
        "collision check would otherwise fire on the container's own name"
    )


def test_rename_checks_same_name_before_the_validator(container_text: str) -> None:
    """'make rename NAME=<current>' reports already-named, not a name collision."""
    rename = _function_body("rdc_rename", container_text)
    same_at = rename.index("already named")
    validate_at = rename.index("rdc_validate_requested_name")
    assert same_at < validate_at


def test_the_help_advertises_the_creation_time_name() -> None:
    """The OPTIONS row documents NAME= next to CONTAINER=."""
    text = (_REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    assert 'row "NAME=<x>"' in text, "the help must document NAME= on build/up"
    snapshot = (_REPO_ROOT / "tests" / "data" / "make-help.txt").read_text(encoding="utf-8")
    assert "Name the container build/up creates at creation" in snapshot


# ---------------------------------------------------------------------------
# The collision probe, behaviorally.
#
# `make up` against a PATH-earlier fake docker that records argv (the stub
# style of tests/test_engine_container_path.py), with the environment stripped
# of everything the run must default or classify on its own. up is the
# lightest dispatch that reaches the validator first: the probe is rdc_up's
# first docker call, so the stub's log isolates it, and a stub `info` failure
# gives a run that gets past validation a deterministic, distinct stop at up's
# engine-reachability check -- the pass-through proof.
# ---------------------------------------------------------------------------

_PROBE_TIMEOUT_ENV_VAR = "NAME_BUILD_TEST_TIMEOUT_SECONDS"
_PROBE_DEFAULT_TIMEOUT_SECONDS = 30.0

_PROBE_PROJECT = "name-probe-proj"

# Every variable the validation run must default, resolve or classify on its
# own, so the assertions can never read a value this machine's environment
# leaked; REPO_SHELL_ENV is pointed outside the tree for the same reason.
_PROBE_RUN_UNSET = (
    "CONTAINER",
    "CONTAINER_USER",
    "NAME",
    "PROJECT_NAME",
    "ENGINE",
    "INSTANCE",
    "DEFAULT_REMOTE_INSTANCE",
    "DOCKER_CONTEXT",
    "KIND",
    "LOCAL_DOCKER_CONTEXT",
    "REMOTE_DOCKER_CONTEXT",
    "REMOTE_INSTANCE_ID",
    "RD_INSTANCE_RESOLVED",
    "RD_INSTANCE_CALLER",
    "RD_ENGINE_CONTEXT",
    "RD_ENGINE_CONTEXT_RESOLVED",
    "ENGINE_HOST",
    "ENGINE_PORT",
    "ENGINE_USER",
    "ENGINE_DOCKER_ADDR",
    "ENGINE_ENDPOINT",
)

_BASH_BIN = shutil.which("bash")


def _probe_timeout_seconds() -> float:
    raw = os.environ.get(_PROBE_TIMEOUT_ENV_VAR)
    if raw is None:
        return _PROBE_DEFAULT_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        pytest.fail(f"{_PROBE_TIMEOUT_ENV_VAR}={raw!r} is not a number")
    if value <= 0:
        pytest.fail(f"{_PROBE_TIMEOUT_ENV_VAR}={raw!r} must be positive")
    return value


def _fake_name_probe_bin(bin_dir: Path) -> None:
    """The docker stub the validation runs answer to.

    Appends one line of joined argv per call to $FAKE_DOCKER_LOG, answers
    `context show` with a context nothing in config.env could name (so the
    run classifies local and no remote requirements are demanded), and
    answers the validator's `inspect` probe from FAKE_DOCKER_NAME_EXISTS:
    0 -- an unused name, docker's own expected answer -- exits 1 quietly;
    1 prints an id and exits 0. `info` fails so a run that gets past
    validation stops deterministically at up's reachability branch.
    """
    fake = bin_dir / "docker"
    fake.write_text(
        "#!/bin/sh\n"
        "# Test stub: record argv; decide the name probe from the environment.\n"
        "set -u\n"
        'printf \'%s\\n\' "$*" >> "$FAKE_DOCKER_LOG"\n'
        'if [ "$1" = context ]; then\n'
        "  printf '%s\\n' \"${FAKE_DOCKER_CONTEXT:-stub-context}\"\n"
        "  exit 0\n"
        "fi\n"
        'if [ "$1" = inspect ]; then\n'
        '  if [ "${FAKE_DOCKER_NAME_EXISTS:-0}" = 1 ]; then\n'
        "    printf 'sha256:stubbed-container-id\\n'\n"
        "    exit 0\n"
        "  fi\n"
        "  exit 1\n"
        "fi\n"
        'if [ "$1" = info ]; then\n'
        "  exit 1\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)


def _run_name_validation(
    tmp_path: Path, name: str, *, name_exists: bool
) -> tuple[subprocess.CompletedProcess, list[str]]:
    """`container.sh up` with NAME set, against the stub docker.

    Returns the completed run and the stub's argv log split into lines.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    _fake_name_probe_bin(bin_dir)
    log = tmp_path / "docker-calls.log"
    script = (
        "set -euo pipefail\n"
        f'export FAKE_DOCKER_LOG="{log}"\n'
        f'export NAME="{name}"\n'
        f'export REPO_SHELL_ENV="{tmp_path / "no-shell.env"}"\n'
        f'"{_CONTAINER_SH}" up\n'
    )
    assert _BASH_BIN is not None, "bash must be installed to run this suite"
    env = dict(os.environ)
    for var in _PROBE_RUN_UNSET:
        env.pop(var, None)
    env.update(
        PATH=f"{bin_dir}:{env.get('PATH', '')}",
        PROJECT_NAME=_PROBE_PROJECT,
        FAKE_DOCKER_NAME_EXISTS="1" if name_exists else "0",
    )
    result = subprocess.run(
        [_BASH_BIN, "-c", script],
        capture_output=True,
        text=True,
        timeout=_probe_timeout_seconds(),
        env=env,
    )
    return result, log.read_text(encoding="utf-8").splitlines()


def test_an_unused_name_passes_validation_through_to_the_next_stage(
    tmp_path: Path,
) -> None:
    """The probe failing -- docker's answer to an unused name -- must not end
    the run: validation passes through and up reaches its next stage.

    This is the E2E defect: the translating wrapper turned the probe's
    expected non-zero into a run-ending failure, so the build never started.
    The stub's `info` failure gives the run past validation a distinct stop,
    which is what proves the probe's exit status was consumed by the `if`
    rather than translated.
    """
    name = "fresh-container-name"
    result, calls = _run_name_validation(tmp_path, name, name_exists=False)
    probe = f"inspect -f {{{{.Id}}}} {name}"
    assert result.returncode != 0, (
        f"the run stops at the stubbed engine check, not silently; stdout={result.stdout[-400:]!r}"
    )
    assert probe in calls, f"the quiet probe is what ran; calls={calls!r}"
    assert "already exists on this engine" not in result.stderr, (
        f"an unused name must not take the refusal branch; stderr={result.stderr[-400:]!r}"
    )
    assert "The local docker engine is not answering" in result.stderr, (
        "the run must get past validation to up's engine-reachability check; "
        f"stderr={result.stderr[-400:]!r}"
    )


def test_a_taken_name_fires_the_refusal_at_the_probe(tmp_path: Path) -> None:
    """The probe succeeding -- the name is taken -- must fire the refusal,
    naming the container, before anything past validation runs."""
    name = "taken-container-name"
    result, calls = _run_name_validation(tmp_path, name, name_exists=True)
    probe = f"inspect -f {{{{.Id}}}} {name}"
    assert result.returncode != 0, "a taken name must refuse"
    assert "already exists on this engine" in result.stderr, (
        f"the refusal must fire; stderr={result.stderr[-400:]!r}"
    )
    assert name in result.stderr, "the refusal must name the container"
    assert "make rename NAME=<other>" in result.stderr, "the refusal carries its remedy"
    assert calls[-1] == probe, f"the refusal must fire at the probe itself; calls={calls!r}"
    assert not [call for call in calls if call.startswith("info")], (
        f"nothing may run past validation once the name is refused; calls={calls!r}"
    )
