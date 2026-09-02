"""Terragrunt and engine version floor assertions (spec Section 6, AC-6.1).

Section 6 states the minimum Terragrunt version is "asserted by a test, not
assumed": an older Terragrunt does not fail loudly, it silently mis-combines
`use_lockfile` with `--backend-bootstrap` (the defect gruntwork-io/terragrunt
PR #5665 fixed), so two overlapping runs can corrupt state with neither run
reporting an error. The engine floor exists for the identical reason:
`use_lockfile` -- native S3 state locking -- does not exist below OpenTofu or
Terraform 1.10, so an older engine again runs and says nothing while not
locking at all. Both are exactly the class of defect a human would never
notice until two runs overlapped in production, which is why Section 6
requires a test rather than a comment.

Both floors are declared once, in `remote-instances/root.hcl`
(`terragrunt_version_constraint`, `terraform_version_constraint`), and this
module reads them from there rather than restating either number: a floor
bumped in root.hcl and forgotten here would leave this module asserting a
stale, already-superseded requirement while claiming to guard the real one.
The installed versions are read the same way, by asking the tools
themselves (`terragrunt --version`, and whichever engine root.hcl's
`terraform_binary` names, `--version`), never assumed from an image tag or a
CI matrix entry.

AC-TEST-003's parameterized negative cases are built by degrading the
*parsed* floor by one release at the patch, minor and major position (with a
borrow into the next-more-significant position when the target position is
already zero -- exactly the way real release history has no "1.10.-1", and
the release before "1.10.0" is "1.9.x", not a negative patch) rather than by
writing three separate literal version strings, so the negative cases
automatically track whatever floor root.hcl declares instead of a frozen
copy of today's floor. The positive floor assertions and the AC-TEST-003
negative cases both route through the same `_meets_floor` comparator (see
below), and the "which tool" axis (terragrunt vs. its engine) is a
`pytest.mark.parametrize` axis rather than a pair of copy-pasted test
functions, so a change to the assertion shape cannot be applied to one tool
and forgotten on the other.

`ROOT_HCL_RELATIVE`, `_repo_root`, `_read_repo_file`, the ast-based
skip/xfail/guarded-import detector and the self-check test that uses it are
shared with `tests/test_state_bucket_name.py` via `tests/conftest.py`
rather than declared twice; see that module's docstring for why.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import ROOT_HCL_RELATIVE, _assert_no_skip_guard, _read_repo_file
from devcontainer_config.hostprobe import read_positive_seconds

DEVCONTAINER_JSON_RELATIVE = ".devcontainer/devcontainer.json"

# Bounds how long this module waits on `terragrunt --version` / `<engine>
# --version` before giving up; both are local subprocess calls with no
# network involved (spec Section 10.1's hermetic-suite contract), so this
# only guards against a genuinely hung process, never a slow remote call.
# Read from the environment, with a documented default, rather than a bare
# literal at the `subprocess.run` call site: CLAUDE.md requires "timeout
# values must be configurable via environment variables", and this
# repository's existing pattern for exactly that shape is
# `devcontainer_config.hostprobe.read_positive_seconds`
# (`DOCKER_HANDSHAKE_TIMEOUT` / `DOCKER_HANDSHAKE_TIMEOUT_ENV_VAR`) and
# `devcontainer_config.transport`'s `SSM_FORWARD_TIMEOUT`; this module reuses
# that same fail-fast reader rather than declaring a second, independent
# parser for the identical "positive number of seconds" shape.
_VERSION_SUBPROCESS_TIMEOUT_ENV_VAR = "TOOL_VERSION_PROBE_TIMEOUT_SECONDS"
_VERSION_SUBPROCESS_TIMEOUT_DEFAULT_SECONDS = 10.0

# Position of each release component within a (major, minor, patch) tuple,
# read by `_degrade` below. Declared once so the three parameterized degrees
# in AC-TEST-003's negative cases and `_degrade`'s own padding agree on what
# "patch", "minor" and "major" mean.
_DEGREE_POSITIONS: dict[str, int] = {"major": 0, "minor": 1, "patch": 2}

# AC-TEST-003's "which tool" axis: each entry names the `>= X.Y[.Z]`
# variable root.hcl declares for that tool. `_binary_name_for` below resolves
# the actual binary name for each label; kept separate from this tuple
# because the engine's binary name is itself read out of root.hcl
# (`terraform_binary`), not a second constraint variable.
_TOOL_CONSTRAINT_VARIABLES: tuple[tuple[str, str], ...] = (
    ("terragrunt", "terragrunt_version_constraint"),
    ("engine", "terraform_version_constraint"),
)


class FloorAssertionError(AssertionError):
    """A version floor, an installed version, or a required binary could not be resolved.

    Every raise site below names the file or command it could not make
    sense of and the raw value involved, rather than letting a bare
    `assert` collapse into a generic `AssertionError` with no context --
    the Error Handling Contract this work unit's Approach requires, applied
    to test code exactly as it applies to production code.
    """


def _version_subprocess_timeout_seconds() -> float:
    """The deadline for a `--version` subprocess to answer, read fresh on every call.

    Not cached at import time, so a caller that sets
    `TOOL_VERSION_PROBE_TIMEOUT_SECONDS` before this module's tests run
    observes its own value, matching
    `devcontainer_config.hostprobe._docker_handshake_timeout_seconds`'s
    identical "read fresh, never cached" contract for the same reason.
    """
    return read_positive_seconds(
        _VERSION_SUBPROCESS_TIMEOUT_ENV_VAR, _VERSION_SUBPROCESS_TIMEOUT_DEFAULT_SECONDS
    )


def _version_tuple(raw: str, *, source: str) -> tuple[int, ...]:
    """Parse a dotted version string such as "1.1.3" into `(1, 1, 3)`.

    Raises naming `source` and the exact text that failed to parse, never
    silently drops to a default: AC-TEST-006 requires an unparsable version
    to fail the test outright, not be treated as satisfying (or as failing)
    a floor by accident.
    """
    stripped = raw.strip()
    if re.fullmatch(r"\d+(?:\.\d+)*", stripped) is None:
        raise FloorAssertionError(
            f"could not parse a dotted version number out of {raw!r} from {source}"
        )
    return tuple(int(part) for part in stripped.split("."))


def _declared_floor(hcl_text: str, variable: str) -> tuple[int, ...]:
    """The `>= X.Y[.Z]` constraint `variable` declares in `remote-instances/root.hcl`."""
    pattern = re.compile(rf'^\s*{re.escape(variable)}\s*=\s*">=\s*([0-9][0-9.]*)"', re.MULTILINE)
    match = pattern.search(hcl_text)
    if match is None:
        raise FloorAssertionError(f"no {variable!r} declaration found in {ROOT_HCL_RELATIVE}")
    return _version_tuple(match.group(1), source=f"{ROOT_HCL_RELATIVE}:{variable}")


def _without_declared_floor(hcl_text: str, variable: str) -> str:
    """A copy of `hcl_text` with `variable`'s `>= X.Y[.Z]` declaration line removed entirely.

    Built the same way `tests/test_state_bucket_name.py::_without_suffix_declaration`
    builds its own missing-declaration fixture: by deleting the real line out
    of a real, freshly read `root.hcl` copy via `re.subn`, rather than typing
    a hand-written HCL fragment that would restate `terraform_version_constraint
    = ">= 1.10"` as a literal (the exact AC-FUNC-001 violation this replaces).
    """
    perturbed, count = re.subn(
        rf'^\s*{re.escape(variable)}\s*=\s*">=\s*[0-9][0-9.]*"\n',
        "",
        hcl_text,
        count=1,
        flags=re.MULTILINE,
    )
    if count != 1:
        raise FloorAssertionError(
            f"could not remove the {variable} declaration from a copy of "
            f"{ROOT_HCL_RELATIVE} to build the missing-declaration fixture"
        )
    return perturbed


def _declared_engine_binary(hcl_text: str) -> str:
    """The engine binary name `terraform_binary` names in root.hcl (spec Section 13, D14)."""
    match = re.search(r'^\s*terraform_binary\s*=\s*"([^"]+)"', hcl_text, re.MULTILINE)
    if match is None:
        raise FloorAssertionError(f"no terraform_binary declaration found in {ROOT_HCL_RELATIVE}")
    return match.group(1)


def _binary_name_for(tool: str, hcl_text: str) -> str:
    """The on-PATH binary name for `tool` ("terragrunt" or "engine").

    Terragrunt's own binary name is not a declared repository value (it is
    this suite's own name for the tool it is testing, not a version number
    or a bucket-name component AC-FUNC-001 governs); the engine's binary
    name is read out of root.hcl's `terraform_binary`, the same way
    `_declared_engine_binary` always has. Shared by the positive floor test
    and the missing-binary test so both "which tool" axes resolve a binary
    name identically.
    """
    if tool == "terragrunt":
        return "terragrunt"
    if tool == "engine":
        return _declared_engine_binary(hcl_text)
    raise FloorAssertionError(f"no binary-name resolver registered for tool {tool!r}")


def _terraform_feature_key(devcontainer_json_text: str) -> str:
    """The `features` key that installs Terragrunt, its engine and tflint together (spec 6.1).

    Found by locating the feature options object that sets a `terragrunt`
    option, rather than assumed by name, so this stays correct if the
    feature is ever re-pinned to a different registry path or version tag.
    """
    try:
        payload = json.loads(devcontainer_json_text)
    except json.JSONDecodeError as exc:
        raise FloorAssertionError(f"{DEVCONTAINER_JSON_RELATIVE} is not valid JSON: {exc}") from exc
    features = payload.get("features", {})
    if not isinstance(features, dict):
        raise FloorAssertionError(f"{DEVCONTAINER_JSON_RELATIVE} has no usable 'features' object")
    for name, options in features.items():
        if isinstance(options, dict) and "terragrunt" in options:
            return str(name)
    raise FloorAssertionError(
        f"no feature in {DEVCONTAINER_JSON_RELATIVE} declares a 'terragrunt' option"
    )


def _resolve_binary(binary_name: str, *, feature_key: str, path: str | None = None) -> str:
    """The absolute path to `binary_name`, or a fail-fast naming the feature that installs it.

    `path=None` searches the real `PATH`; a test builds an empty directory
    and passes it here to prove the missing-binary branch (AC-TEST-007)
    without needing to actually uninstall anything from this machine.
    """
    resolved = shutil.which(binary_name, path=path)
    if resolved is None:
        raise FloorAssertionError(
            f"{binary_name!r} is not on PATH; install it via the devcontainer feature "
            f"{feature_key!r} (declared in {DEVCONTAINER_JSON_RELATIVE})"
        )
    return resolved


def _installed_version(binary_path: str, *, source: str) -> tuple[int, ...]:
    """The version `binary_path --version` reports, parsed from its own first output line.

    Fails before any parsing is attempted if the probe itself did not exit
    0: a broken install, a PATH shim, or an engine error must never be
    treated as satisfying a floor merely because its stderr happens to
    contain an unrelated dotted number (a plugin protocol version, a Go
    toolchain version, and so on) -- the identical "check `returncode`
    before trusting output" contract `_repo_slug_from_git_remote` in
    `tests/test_state_bucket_name.py` applies to its own subprocess call,
    and `catalog.py`'s `_invoke` and `certs.py`'s `_run_openssl` apply to
    production subprocess calls.

    Only `stdout`'s first line is parsed on success: every tool this module
    probes (`terragrunt version <n>` / `Terraform v<n>`) reports its own
    version there, and a later line can carry an unrelated dotted number (a
    "new version available" banner, a provider protocol number) that must
    never be mistaken for the installed version.
    """
    result = subprocess.run(
        [binary_path, "--version"],
        capture_output=True,
        text=True,
        timeout=_version_subprocess_timeout_seconds(),
        check=False,
    )
    if result.returncode != 0:
        raise FloorAssertionError(
            f"{source} exited {result.returncode}, not 0; refusing to parse a version out of "
            f"its output: stdout={result.stdout!r} stderr={result.stderr!r}"
        )
    first_line = result.stdout.splitlines()[0] if result.stdout.splitlines() else ""
    match = re.search(r"v?(\d+(?:\.\d+)+)", first_line)
    if match is None:
        raise FloorAssertionError(
            f"could not find a version number in {source}'s first output line: {first_line!r}"
        )
    return _version_tuple(match.group(1), source=source)


def _degrade(floor: tuple[int, ...], position: int) -> tuple[int, ...]:
    """`floor` with the component at `position` (0=major, 1=minor, 2=patch) lowered by one release.

    Pads `floor` on the right with zeros to at least three components
    first, since a two-component floor like ">= 1.10" has no explicit patch
    digit to decrement. Borrows from the next more-significant component
    when the target position is already zero, so the result is always a
    valid, non-negative version guaranteed strictly less than `floor` under
    tuple comparison -- the same way the release immediately before 1.10.0
    is 1.9.x, never 1.10.-1.
    """
    components = list(floor) + [0] * max(0, 3 - len(floor))
    index = position
    while components[index] == 0:
        index -= 1
        if index < 0:
            raise FloorAssertionError(
                f"cannot construct a version below the all-zero floor {floor!r}"
            )
    components[index] -= 1
    return tuple(components)


def _meets_floor(installed: tuple[int, ...], floor: tuple[int, ...]) -> bool:
    """Whether `installed` satisfies `floor` under tuple comparison.

    Both positive floor assertions (`installed` really is on or above its
    floor) and all six AC-TEST-003 degraded cases (a version one release
    below the floor is NOT `_meets_floor`) route through this single
    comparator rather than one side inlining `installed >= floor` and the
    other reimplementing it as `degraded < floor`: if the comparison
    direction were ever inverted or widened, both directions would catch
    it, not just the one that happened to still spell it out.
    """
    return installed >= floor


@pytest.fixture(scope="module")
def hcl_text() -> str:
    return _read_repo_file(ROOT_HCL_RELATIVE, error_cls=FloorAssertionError)


@pytest.fixture(scope="module")
def devcontainer_json_text() -> str:
    return _read_repo_file(DEVCONTAINER_JSON_RELATIVE, error_cls=FloorAssertionError)


@pytest.mark.parametrize(("tool", "constraint_variable"), _TOOL_CONSTRAINT_VARIABLES)
def test_installed_tool_meets_the_declared_floor(
    tool: str, constraint_variable: str, hcl_text: str, devcontainer_json_text: str
) -> None:
    """AC-TEST-001 / AC-TEST-002 / AC-6.1: installed tool is at least root.hcl's declared floor."""
    floor = _declared_floor(hcl_text, constraint_variable)
    binary_name = _binary_name_for(tool, hcl_text)
    feature_key = _terraform_feature_key(devcontainer_json_text)
    binary = _resolve_binary(binary_name, feature_key=feature_key)
    installed = _installed_version(binary, source=f"{binary_name} --version")
    assert _meets_floor(installed, floor), (
        f"installed {binary_name} {installed} is below the floor {floor} declared at "
        f"{ROOT_HCL_RELATIVE}:{constraint_variable}"
    )


@pytest.mark.parametrize(("tool", "constraint_variable"), _TOOL_CONSTRAINT_VARIABLES)
@pytest.mark.parametrize("degree", ["patch", "minor", "major"])
def test_a_degraded_tool_version_fails_the_floor_comparison(
    degree: str, tool: str, constraint_variable: str, hcl_text: str
) -> None:
    """AC-TEST-003: a version one patch/minor/major below either floor fails `_meets_floor`."""
    floor = _declared_floor(hcl_text, constraint_variable)
    degraded = _degrade(floor, _DEGREE_POSITIONS[degree])
    assert not _meets_floor(degraded, floor), (
        f"{tool}'s {degree}-degraded {degraded} still met floor {floor}"
    )


def test_unparsable_version_output_fails_naming_the_raw_text() -> None:
    """AC-TEST-006: a version string that does not parse fails, naming the raw output."""
    with pytest.raises(FloorAssertionError, match=re.escape("not-a-version-string")):
        _version_tuple("not-a-version-string", source="terragrunt --version (synthetic)")


def test_installed_version_fails_fast_on_a_non_zero_exit_without_parsing_a_decoy_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-TEST-006 / AC-6.1 / Error Handling Contract: a probe that exits non-zero must never
    be treated as satisfying a floor just because its stderr contains an unrelated dotted
    number (an engine protocol version, a Go toolchain version)."""

    class _FailedVersionProbe:
        returncode = 1
        stdout = ""
        stderr = "engine error: plugin protocol 5.0 (go1.22.3)\n"

    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: _FailedVersionProbe())

    with pytest.raises(FloorAssertionError) as excinfo:
        _installed_version("terragrunt (synthetic)", source="terragrunt --version (synthetic)")
    message = str(excinfo.value)
    assert "terragrunt --version (synthetic)" in message
    assert "exited 1" in message


def test_missing_terragrunt_declaration_fails_naming_the_file(hcl_text: str) -> None:
    """Malformed-input case (Approach step 5): no terragrunt_version_constraint declared at all."""
    perturbed_hcl_text = _without_declared_floor(hcl_text, "terragrunt_version_constraint")
    with pytest.raises(FloorAssertionError, match="terragrunt_version_constraint"):
        _declared_floor(perturbed_hcl_text, "terragrunt_version_constraint")


@pytest.mark.parametrize("tool", ["terragrunt", "engine"])
def test_missing_tool_binary_fails_naming_the_feature(
    tool: str, tmp_path: Path, hcl_text: str, devcontainer_json_text: str
) -> None:
    """AC-TEST-007: a missing terragrunt or engine binary fails, naming the installing feature."""
    binary_name = _binary_name_for(tool, hcl_text)
    feature_key = _terraform_feature_key(devcontainer_json_text)
    with pytest.raises(FloorAssertionError) as excinfo:
        _resolve_binary(binary_name, feature_key=feature_key, path=str(tmp_path))
    message = str(excinfo.value)
    assert binary_name in message
    assert feature_key in message


def test_no_skip_xfail_or_conditional_import_guards_this_module() -> None:
    """AC-TEST-007: this module hides no failure behind a skip, xfail or guarded import."""
    _assert_no_skip_guard(Path(__file__))
