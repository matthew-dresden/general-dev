"""Tests for devcontainer_config.instance_ops: the engine behind the make
instance-* targets (scaffold, the per-instance id store, state, power, cleanup).

The `devcontainer_config.instance_ops` import is deferred into function bodies
(via `_import_instance_ops`) instead of done once at module scope, the same
convention `tests/test_instances.py` and `tests/test_certs.py` document: the
TDD RED gate stashes this unit's own production-source files and re-runs a
single named test node, and a module-level import would fail COLLECTION for
the whole file (pytest exit 2, no test outcome recorded) instead of failing
the one test for the real reason.

`_FakeRunner` is a queued Runner double in the `hostcreds` shape this module
consumes: it never spawns a process, records every argv it is handed, and
answers from a queue the test fills beforehand. `_RaisingRunner` stands in
for a host missing a binary, which `subprocess.run` reports by raising
FileNotFoundError. No test here touches AWS, docker, a keychain or the
network; the single deliberate local-process exception is the certificate
expiry test, which issues a real client certificate through
`devcontainer_config.certs` (the same real-openssl discipline
`tests/test_certs.py` runs under) so the days-remaining arithmetic is
checked against a genuine certificate's notAfter with a fixed reference
time, not a hand-typed date.

Every generated identifier -- instance ids, AMI ids, port numbers, instance
names -- is built from `uuid.uuid4()` at test time, so no EC2- or AMI-shaped
literal is stored in this file. `sleep` is always injected as
`sleeps.append`, so the recorded call list proves no real waiting ever
happened.
"""

from __future__ import annotations

import importlib
import json
import os
import re
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING

import pytest
from gitfixtures import generated_root, init_repo

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

# The region every aws call in this suite targets. A test fixture value, not
# configuration: the functions under test take their region as a parameter.
REGION = "us-east-1"

_EC2_STATE_QUERY = "Reservations[0].Instances[0].State.Name"


def _import_instance_ops() -> ModuleType:
    """Import devcontainer_config.instance_ops from inside a function body."""
    return importlib.import_module("devcontainer_config.instance_ops")


def _import_certs() -> ModuleType:
    """Import devcontainer_config.certs from inside a function body."""
    return importlib.import_module("devcontainer_config.certs")


def _import_instances() -> ModuleType:
    """Import devcontainer_config.instances from inside a function body."""
    return importlib.import_module("devcontainer_config.instances")


def _import_repo() -> ModuleType:
    """Import devcontainer_config.repo from inside a function body."""
    return importlib.import_module("devcontainer_config.repo")


def _import_gitignore_check() -> ModuleType:
    """Import tests/gitignore_check from inside a function body."""
    return importlib.import_module("gitignore_check")


def _ok(stdout: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


def _err(stderr: str, *, returncode: int = 1) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout="", stderr=stderr)


class _FakeRunner:
    """A queued Runner double: records every argv, answers from a queue.

    The queue is strict: an invocation with no queued response is a
    test-authoring bug and fails the test (assertion), rather than being
    answered with a default that would let an unexpected aws/docker call
    pass silently.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self._queue: list[subprocess.CompletedProcess[str]] = []

    def queue(self, *results: subprocess.CompletedProcess[str]) -> None:
        self._queue.extend(results)

    def __call__(
        self, argv: Sequence[str], stdin: str | None, *, env: Mapping[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(tuple(argv))
        assert self._queue, f"_FakeRunner was invoked with no queued response: {tuple(argv)!r}"
        return self._queue.pop(0)


class _RaisingRunner:
    """A Runner double standing in for a host with no binary on PATH."""

    def __call__(
        self, argv: Sequence[str], stdin: str | None, *, env: Mapping[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError(2, "No such file or directory", argv[0])


@pytest.fixture(autouse=True)
def _docker_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point DOCKER_CONFIG at a per-test directory.

    The id store and the certificate material both live under
    `instances.certs_dir(name)`, which honors DOCKER_CONFIG; setting it here
    keeps every test's state on `tmp_path` and away from the operator's real
    `~/.docker/certs`.

    `certs` is imported BEFORE the env var is set: its module-level
    `DEFAULT_CERTS_ROOT` freezes `instances.certs_root()` at first import, and
    when this file runs before `tests/test_certs.py` in the same session that
    first import would otherwise happen here, pinning the constant to this
    tmp path and breaking
    `test_default_certs_root_is_sourced_from_instances_certs_root`. Imports
    are cached, and the tests here pass an explicit root wherever the value
    would matter, so nothing depends on which environment the frozen
    constant came from.
    """
    importlib.import_module("devcontainer_config.certs")
    docker_config = tmp_path / "docker-config"
    docker_config.mkdir()
    monkeypatch.setenv("DOCKER_CONFIG", str(docker_config))
    return docker_config


def _instance_id() -> str:
    """An i-prefixed, 17-hex instance-id-shaped value, generated per call."""
    return "i-" + uuid.uuid4().hex[:17]


def _ami_id() -> str:
    """An ami-id-shaped value, generated per call."""
    return "ami-" + uuid.uuid4().hex


def _instance_name(prefix: str = "inst") -> str:
    """A valid instance name unique per call."""
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _git_root(tmp_path: Path) -> Path:
    """A disposable git checkout whose origin slug is this repository's own.

    The checkout carries this repository's committed `.gitignore` -- the
    same seeding `tests/test_gitignore_allowlist.py` uses for its scratch
    repositories -- so a scaffold's per-instance ignore append exercises the
    real anchor rule rather than a test-owned stand-in.
    """
    root = generated_root(tmp_path)
    init_repo(root)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "remote",
            "add",
            "origin",
            "https://example.invalid/org/general-dev.git",
        ],
        check=True,
        capture_output=True,
    )
    real_gitignore = (
        _import_repo().find_root(Path(__file__).resolve().parent) / ".gitignore"
    ).read_text(encoding="utf-8")
    (root / ".gitignore").write_text(real_gitignore, encoding="utf-8")
    return root


def _seed_instance_dir(root: Path, name: str) -> Path:
    """A minimal per-instance directory under `remote-instances/<name>/`."""
    instance_dir = root / "remote-instances" / name
    instance_dir.mkdir(parents=True)
    (instance_dir / "terragrunt.hcl").write_text("# seeded\n", encoding="utf-8")
    return instance_dir


def _sibling_with_cidr(root: Path, name: str, vpc_cidr: str) -> None:
    """A sibling instance directory claiming `vpc_cidr`, for CIDR allocation."""
    directory = _seed_instance_dir(root, name)
    hcl = directory / "terragrunt.hcl"
    hcl.write_text(f'vpc_cidr = "{vpc_cidr}"\n', encoding="utf-8")


def _params_result(names: list[str]) -> subprocess.CompletedProcess[str]:
    """A describe-parameters success listing `names`."""
    return _ok(json.dumps({"Parameters": [{"Name": name} for name in names]}))


def _context_inspect_argv(context: str) -> tuple[str, ...]:
    return ("docker", "context", "inspect", context)


def _describe_argv(instance_id: str) -> tuple[str, ...]:
    return (
        "aws",
        "ec2",
        "describe-instances",
        "--instance-ids",
        instance_id,
        "--query",
        _EC2_STATE_QUERY,
        "--output",
        "text",
    )


def _power_describe_argv(instance_id: str) -> tuple[str, ...]:
    return (
        "aws",
        "ec2",
        "describe-instances",
        "--instance-ids",
        instance_id,
        "--region",
        REGION,
        "--query",
        _EC2_STATE_QUERY,
        "--output",
        "text",
    )


# ---------------------------------------------------------------------------
# scaffold
# ---------------------------------------------------------------------------


def test_scaffold_wires_name_and_readme_contract(tmp_path: Path) -> None:
    ops = _import_instance_ops()
    root = _git_root(tmp_path)
    name = _instance_name("acme")
    ami = _ami_id()
    runner = _FakeRunner()
    runner.queue(_ok(ami))

    result = ops.scaffold(root, name, region=REGION, ami=None, runner=runner)

    assert result.path == root / "remote-instances" / name / "terragrunt.hcl"
    assert result.path.is_file()
    text = result.path.read_text(encoding="utf-8")
    assert f'instance_name = "{name}"' in text
    assert f'name_prefix   = "{name}"' in text
    assert f'Environment = "{name}"' in text
    assert 'include "root" {' in text
    assert 'path = find_in_parent_folders("root.hcl")' in text
    assert "_envcommon/remote-ec2.hcl" in text
    assert f'availability_zone  = "{REGION}a"' in text
    assert 'egress_cidr_blocks = ["0.0.0.0/0"]' in text
    assert runner.calls[0] == (
        "aws",
        "ssm",
        "get-parameter",
        "--name",
        ops.AMI_SSM_PARAMETER_PATH,
        "--region",
        REGION,
        "--query",
        "Parameter.Value",
        "--output",
        "text",
    )
    assert result.ami == ami


def test_scaffold_ami_provenance_comment_carries_parameter_path_region_and_date(
    tmp_path: Path,
) -> None:
    ops = _import_instance_ops()
    root = _git_root(tmp_path)
    name = _instance_name()
    runner = _FakeRunner()
    runner.queue(_ok(_ami_id()))

    result = ops.scaffold(root, name, region=REGION, ami=None, runner=runner)

    text = result.path.read_text(encoding="utf-8")
    assert ops.AMI_SSM_PARAMETER_PATH in text
    assert REGION in text
    assert re.search(r"\d{4}-\d{2}-\d{2}", text) is not None


def test_scaffold_instance_type_default_carries_t4g_note(tmp_path: Path) -> None:
    ops = _import_instance_ops()
    root = _git_root(tmp_path)
    runner = _FakeRunner()
    runner.queue(_ok(_ami_id()))

    result = ops.scaffold(root, _instance_name(), region=REGION, ami=None, runner=runner)

    text = result.path.read_text(encoding="utf-8")
    assert f'instance_type = "{ops.DEFAULT_INSTANCE_TYPE}"' in text
    assert ops.DEFAULT_INSTANCE_TYPE == "c8g.xlarge"
    assert "t4g.medium" in text


def test_scaffold_allocates_cidrs_and_volume_defaults(tmp_path: Path) -> None:
    ops = _import_instance_ops()
    root = _git_root(tmp_path)
    runner = _FakeRunner()
    runner.queue(_ok(_ami_id()))

    result = ops.scaffold(root, _instance_name(), region=REGION, ami=None, runner=runner)

    text = result.path.read_text(encoding="utf-8")
    assert result.vpc_cidr == "10.100.0.0/16"
    assert 'vpc_cidr           = "10.100.0.0/16"' in text
    assert 'subnet_cidr        = "10.100.1.0/24"' in text
    assert f"root_volume_size_gb = {ops.DEFAULT_VOLUME_SIZE_GB}" in text
    assert f"data_volume_size_gb = {ops.DEFAULT_VOLUME_SIZE_GB}" in text
    assert ops.DEFAULT_VOLUME_SIZE_GB == 30


def test_scaffold_protection_flags_are_false_with_when_to_enable_comment(
    tmp_path: Path,
) -> None:
    ops = _import_instance_ops()
    root = _git_root(tmp_path)
    runner = _FakeRunner()
    runner.queue(_ok(_ami_id()))

    result = ops.scaffold(root, _instance_name(), region=REGION, ami=None, runner=runner)

    text = result.path.read_text(encoding="utf-8")
    assert "disable_api_termination = false" in text
    assert "disable_api_stop        = false" in text
    assert "Set them to true" in text


def test_scaffold_guidance_names_commonly_edited_fields_and_deploy_command(
    tmp_path: Path,
) -> None:
    ops = _import_instance_ops()
    root = _git_root(tmp_path)
    name = _instance_name()
    runner = _FakeRunner()
    runner.queue(_ok(_ami_id()))

    result = ops.scaffold(root, name, region=REGION, ami=None, runner=runner)

    assert any(str(result.path) in message for message in result.messages)
    assert any(
        "instance_type" in message and "availability_zone" in message for message in result.messages
    )
    assert any(f"make instance-deploy INSTANCE={name}" in message for message in result.messages)


def test_scaffold_cidr_skips_used_blocks(tmp_path: Path) -> None:
    ops = _import_instance_ops()
    root = _git_root(tmp_path)
    _sibling_with_cidr(root, _instance_name("first"), "10.100.0.0/16")
    _sibling_with_cidr(root, _instance_name("second"), "10.101.0.0/16")
    runner = _FakeRunner()
    runner.queue(_ok(_ami_id()))

    result = ops.scaffold(root, _instance_name(), region=REGION, ami=None, runner=runner)

    assert result.vpc_cidr == "10.102.0.0/16"
    text = result.path.read_text(encoding="utf-8")
    assert 'subnet_cidr        = "10.102.1.0/24"' in text


def test_scaffold_cidr_exhaustion_raises(tmp_path: Path) -> None:
    ops = _import_instance_ops()
    root = _git_root(tmp_path)
    for octet in range(ops.FIRST_FREE_CIDR_OCTET_MIN, ops.FIRST_FREE_CIDR_OCTET_MAX + 1):
        _sibling_with_cidr(root, _instance_name(f"used{octet}"), f"10.{octet}.0.0/16")

    with pytest.raises(ops.ScaffoldError) as exc_info:
        ops.scaffold(root, _instance_name(), region=REGION, ami=_ami_id(), runner=_FakeRunner())

    message = str(exc_info.value)
    assert "10.100.0.0/16" in message
    assert "10.254.0.0/16" in message


def test_scaffold_refuses_existing_directory(tmp_path: Path) -> None:
    ops = _import_instance_ops()
    root = _git_root(tmp_path)
    name = _instance_name()
    directory = _seed_instance_dir(root, name)
    marker = directory / "terragrunt.hcl"

    with pytest.raises(ops.ScaffoldError) as exc_info:
        ops.scaffold(root, name, region=REGION, ami=_ami_id(), runner=_FakeRunner())

    assert marker.read_text(encoding="utf-8") == "# seeded\n"
    assert str(directory) in str(exc_info.value)


def test_scaffold_ami_override_passthrough_skips_aws(tmp_path: Path) -> None:
    ops = _import_instance_ops()
    root = _git_root(tmp_path)
    name = _instance_name()
    ami = _ami_id()
    runner = _FakeRunner()

    result = ops.scaffold(root, name, region=REGION, ami=ami, runner=runner)

    assert result.ami == ami
    assert runner.calls == []
    text = result.path.read_text(encoding="utf-8")
    assert f'ami           = "{ami}"' in text


def test_scaffold_ami_failure_names_manual_override(tmp_path: Path) -> None:
    ops = _import_instance_ops()
    root = _git_root(tmp_path)
    runner = _FakeRunner()
    runner.queue(_err("boom"))

    with pytest.raises(ops.ScaffoldError) as exc_info:
        ops.scaffold(root, _instance_name(), region=REGION, ami=None, runner=runner)

    message = str(exc_info.value)
    assert "AMI=" in message
    assert ops.AMI_SSM_PARAMETER_PATH in message


def test_scaffold_name_length_headroom(tmp_path: Path) -> None:
    ops = _import_instance_ops()
    root = _git_root(tmp_path)
    longest_ok = "a" * 51  # "general-dev-" + 51 chars == 63, exactly at the limit
    too_long = "a" * 52  # 64 > 63

    accepted = ops.scaffold(root, longest_ok, region=REGION, ami=_ami_id(), runner=_FakeRunner())
    assert accepted.path.is_file()

    with pytest.raises(ops.ScaffoldError) as exc_info:
        ops.scaffold(root, too_long, region=REGION, ami=_ami_id(), runner=_FakeRunner())

    message = str(exc_info.value)
    assert "63" in message
    assert f"general-dev-{too_long}" in message


def test_scaffold_invalid_name_rejected(tmp_path: Path) -> None:
    ops = _import_instance_ops()
    runner = _FakeRunner()

    for bad_name in ("../escape", "a/b", ""):
        with pytest.raises(ops.ScaffoldError):
            ops.scaffold(tmp_path, bad_name, region=REGION, ami=_ami_id(), runner=runner)
    assert runner.calls == []


def _gitignore_lines(root: Path) -> list[str]:
    """The scratch checkout's `.gitignore`, one list entry per line."""
    return (root / ".gitignore").read_text(encoding="utf-8").splitlines()


def test_scaffold_appends_exactly_one_gitignore_entry_below_the_anchor(tmp_path: Path) -> None:
    """One scaffold writes its own single ignore entry, directly under the anchor.

    The anchor is the committed `.terraform.lock.hcl` blanket rule that marks
    the per-instance block; the scaffold's entry lands immediately below it,
    so the appended line -- not some earlier rule -- is what git reports for
    the scaffolded path.
    """
    ops = _import_instance_ops()
    root = _git_root(tmp_path)
    name = _instance_name()

    ops.scaffold(root, name, region=REGION, ami=_ami_id(), runner=_FakeRunner())

    lines = _gitignore_lines(root)
    entry = f"remote-instances/{name}/"
    assert lines.count(entry) == 1
    assert lines.index(entry) == lines.index(ops.GITIGNORE_SCAFFOLD_ANCHOR) + 1


def test_scaffold_second_instance_appends_its_own_entry(tmp_path: Path) -> None:
    """A second scaffold adds a second entry, and neither duplicates either's."""
    ops = _import_instance_ops()
    root = _git_root(tmp_path)
    first = _instance_name()
    second = _instance_name()

    ops.scaffold(root, first, region=REGION, ami=_ami_id(), runner=_FakeRunner())
    ops.scaffold(root, second, region=REGION, ami=_ami_id(), runner=_FakeRunner())

    lines = _gitignore_lines(root)
    anchor_index = lines.index(ops.GITIGNORE_SCAFFOLD_ANCHOR)
    assert lines[anchor_index + 1] == f"remote-instances/{first}/"
    assert lines[anchor_index + 2] == f"remote-instances/{second}/"
    for entry in (f"remote-instances/{first}/", f"remote-instances/{second}/"):
        assert lines.count(entry) == 1


def test_scaffold_gitignore_entry_is_idempotent_when_already_present(tmp_path: Path) -> None:
    """Re-scaffolding a name whose entry survives its directory never duplicates.

    A prior scaffold whose directory was later removed leaves its `.gitignore`
    entry behind; scaffolding the same name again must append nothing.
    """
    ops = _import_instance_ops()
    root = _git_root(tmp_path)
    name = _instance_name()
    entry = f"remote-instances/{name}/"
    lines = _gitignore_lines(root)
    lines.insert(lines.index(ops.GITIGNORE_SCAFFOLD_ANCHOR) + 1, entry)
    (root / ".gitignore").write_text("\n".join(lines) + "\n", encoding="utf-8")

    ops.scaffold(root, name, region=REGION, ami=_ami_id(), runner=_FakeRunner())

    assert _gitignore_lines(root).count(entry) == 1


def test_scaffold_refusal_leaves_gitignore_untouched(tmp_path: Path) -> None:
    """The existing-directory refusal fires before any `.gitignore` append."""
    ops = _import_instance_ops()
    root = _git_root(tmp_path)
    name = _instance_name()
    _seed_instance_dir(root, name)
    before = (root / ".gitignore").read_text(encoding="utf-8")

    with pytest.raises(ops.ScaffoldError):
        ops.scaffold(root, name, region=REGION, ami=_ami_id(), runner=_FakeRunner())

    assert (root / ".gitignore").read_text(encoding="utf-8") == before


def test_scaffold_directory_is_ignored_by_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After a scaffold, git reports the directory and everything in it ignored.

    `git check-ignore` names the appended rule for the scaffolded directory
    and its `terragrunt.hcl`, and `git status --porcelain` lists nothing
    under `remote-instances/`. Deleting the appended entry -- the promotion
    path -- then leaves the deployment trackable while the committed blanket
    rule keeps a first init's `.terraform.lock.hcl` out of git regardless.
    """
    gitignore_check = _import_gitignore_check()
    ops = _import_instance_ops()
    root = _git_root(tmp_path)
    name = _instance_name()
    ops.scaffold(root, name, region=REGION, ami=_ami_id(), runner=_FakeRunner())
    monkeypatch.setattr(gitignore_check, "repo_root", lambda: root)

    directory = f"remote-instances/{name}"
    for path in (directory, f"{directory}/terragrunt.hcl"):
        result = gitignore_check.check_ignore(path)
        assert result.ignored, f"{path}: expected ignored; evidence={result.evidence!r}"
        assert result.evidence == f"{directory}/"
    lock_result = gitignore_check.check_ignore(f"{directory}/.terraform.lock.hcl")
    assert lock_result.ignored

    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    assert not any("remote-instances" in line for line in status.stdout.splitlines()), status.stdout

    lines = _gitignore_lines(root)
    lines.remove(f"{directory}/")
    (root / ".gitignore").write_text("\n".join(lines) + "\n", encoding="utf-8")
    promoted = subprocess.run(
        ["git", "check-ignore", "--no-index", "-v", f"{directory}/terragrunt.hcl"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert promoted.returncode == 1, (
        "the promoted deployment's terragrunt.hcl must be trackable, not ignored"
    )
    lock_after_promotion = gitignore_check.check_ignore(f"{directory}/.terraform.lock.hcl")
    assert lock_after_promotion.ignored, (
        "the lock file must stay ignored under the blanket rule after promotion"
    )
    assert lock_after_promotion.evidence == ops.GITIGNORE_SCAFFOLD_ANCHOR
    assert not any("remote-instances" in line for line in status.stdout.splitlines()), status.stdout


def test_scaffold_fails_fast_when_gitignore_anchor_missing(tmp_path: Path) -> None:
    """A `.gitignore` without the per-instance block's anchor refuses, writing nothing.

    Guessing an insert point would be a fallback; the scaffold names the
    missing rule and creates no directory.
    """
    ops = _import_instance_ops()
    root = _git_root(tmp_path)
    (root / ".gitignore").write_text(
        "# a checkout predating the scaffold block\n.venv/\n", encoding="utf-8"
    )
    name = _instance_name()

    with pytest.raises(ops.ScaffoldError) as exc_info:
        ops.scaffold(root, name, region=REGION, ami=_ami_id(), runner=_FakeRunner())

    assert ops.GITIGNORE_SCAFFOLD_ANCHOR in str(exc_info.value)
    assert not (root / "remote-instances" / name).exists()


def test_scaffold_fails_fast_when_gitignore_is_missing(tmp_path: Path) -> None:
    """No `.gitignore` at all refuses for the same reason, creating nothing."""
    ops = _import_instance_ops()
    root = _git_root(tmp_path)
    (root / ".gitignore").unlink()
    name = _instance_name()

    with pytest.raises(ops.ScaffoldError) as exc_info:
        ops.scaffold(root, name, region=REGION, ami=_ami_id(), runner=_FakeRunner())

    assert ".gitignore" in str(exc_info.value)
    assert not (root / "remote-instances" / name).exists()


# ---------------------------------------------------------------------------
# Per-instance id store
# ---------------------------------------------------------------------------


def test_link_id_is_idempotent_and_readable(tmp_path: Path) -> None:
    ops = _import_instance_ops()
    name = _instance_name()
    first = _instance_id()
    second = _instance_id()

    message = ops.link_id(tmp_path, name, first)
    assert first in message
    assert ops.recorded_id(tmp_path, name) == first

    ops.link_id(tmp_path, name, second)
    assert ops.recorded_id(tmp_path, name) == second


def test_unlink_id_returns_whether_a_file_was_removed(tmp_path: Path) -> None:
    ops = _import_instance_ops()
    name = _instance_name()

    assert ops.unlink_id(tmp_path, name) == 0

    ops.link_id(tmp_path, name, _instance_id())
    assert ops.unlink_id(tmp_path, name) == 1
    assert ops.recorded_id(tmp_path, name) is None


def test_recorded_id_tolerates_missing_directory(tmp_path: Path) -> None:
    ops = _import_instance_ops()

    assert ops.recorded_id(tmp_path, _instance_name()) is None

    certs_dir = Path(os.environ["DOCKER_CONFIG"]) / "certs"
    assert not certs_dir.exists(), "reading a missing id must not create the store directory"


# ---------------------------------------------------------------------------
# state / list_state
# ---------------------------------------------------------------------------


def test_state_probes_every_surface_with_pinned_argv(tmp_path: Path) -> None:
    ops = _import_instance_ops()
    root = _git_root(tmp_path)
    name = _instance_name()
    instance_id = _instance_id()
    ops.link_id(root, name, instance_id)
    _seed_instance_dir(root, name)
    port = 10000 + uuid.uuid4().int % 20000
    context = f"general-dev-{name}"
    runner = _FakeRunner()
    runner.queue(_ok("running\n"))
    runner.queue(_params_result([f"/devcontainer/{name}/tls/ca.pem"]))
    runner.queue(_ok(""))
    runner.queue(_ok(json.dumps({"docker": {"Host": f"tcp://127.0.0.1:{port}"}})))

    snapshot = ops.state(root, name, runner=runner)

    assert snapshot.name == name
    assert snapshot.directory is True
    assert snapshot.recorded_id == instance_id
    assert snapshot.ec2_state == "running"
    assert snapshot.params_present is True
    assert snapshot.certs_present is False
    assert snapshot.client_cert_days_left is None
    assert snapshot.context_exists is True
    assert snapshot.forward_port == port
    assert snapshot.lookup_error is None
    assert runner.calls[0] == _describe_argv(instance_id)
    assert runner.calls[1] == (
        "aws",
        "ssm",
        "describe-parameters",
        "--parameter-filters",
        f"Key=Name,Option=BeginsWith,Values=/devcontainer/{name}/",
        "--output",
        "json",
    )
    assert runner.calls[2] == _context_inspect_argv(context)
    assert runner.calls[3] == (
        "docker",
        "context",
        "inspect",
        context,
        "--format",
        "{{json .Endpoints}}",
    )


def test_state_reports_empty_parameter_prefix_and_absent_context(tmp_path: Path) -> None:
    ops = _import_instance_ops()
    root = _git_root(tmp_path)
    name = _instance_name()
    runner = _FakeRunner()
    runner.queue(_params_result([]))
    runner.queue(_err('context "nope": context not found'))

    snapshot = ops.state(root, name, runner=runner)

    assert snapshot.recorded_id is None
    assert snapshot.ec2_state is None
    assert snapshot.params_present is False
    assert snapshot.context_exists is False
    assert snapshot.forward_port is None
    assert snapshot.lookup_error is None
    assert all(call[0] != "aws" or "describe-instances" not in call for call in runner.calls)


def test_state_probe_failure_sets_sanitized_lookup_error_without_raising(
    tmp_path: Path,
) -> None:
    ops = _import_instance_ops()
    root = _git_root(tmp_path)
    name = _instance_name()
    ops.link_id(root, name, _instance_id())
    runner = _FakeRunner()
    runner.queue(_err("throttled"))
    runner.queue(_err("throttled"))
    runner.queue(_err("docker exploded"))

    snapshot = ops.state(root, name, runner=runner)

    assert snapshot.ec2_state is None
    assert snapshot.params_present is None
    assert snapshot.context_exists is False
    assert snapshot.lookup_error is not None
    assert "describe-instances" in snapshot.lookup_error
    assert "\n" not in snapshot.lookup_error


def test_state_missing_binary_becomes_lookup_error(tmp_path: Path) -> None:
    ops = _import_instance_ops()
    root = _git_root(tmp_path)

    snapshot = ops.state(root, _instance_name(), runner=_RaisingRunner())

    assert snapshot.params_present is None
    assert snapshot.context_exists is None
    assert snapshot.lookup_error is not None


def test_state_unreadable_certificate_becomes_lookup_error(tmp_path: Path) -> None:
    ops = _import_instance_ops()
    certs = _import_certs()
    instances = _import_instances()
    root = _git_root(tmp_path)
    name = _instance_name()
    cert_path = instances.certs_dir(name) / certs.CLIENT_CERT_FILENAME
    cert_path.parent.mkdir(parents=True)
    cert_path.write_text("not a certificate", encoding="utf-8")
    runner = _FakeRunner()
    runner.queue(_params_result([]))
    runner.queue(_err("no context"))

    snapshot = ops.state(root, name, runner=runner)

    assert snapshot.certs_present is True
    assert snapshot.client_cert_days_left is None
    assert snapshot.lookup_error is not None


def test_state_client_cert_days_left_with_fixed_now(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ops = _import_instance_ops()
    certs = _import_certs()
    root = _git_root(tmp_path)
    name = _instance_name()
    monkeypatch.setenv("CERT_CLIENT_DAYS", "30")
    docker_config = Path(os.environ["DOCKER_CONFIG"])
    paths = certs.CertPaths(instance=name, root=docker_config / "certs")
    # Whole seconds: openssl stamps notBefore/notAfter at second precision,
    # so a sub-second reference would make (notAfter - reference).days
    # non-deterministically 29 instead of 30.
    reference_time = datetime.now(UTC).replace(microsecond=0)
    certs.create_ca(paths)
    certs.issue_client(paths)
    monkeypatch.setattr(ops, "_utc_now", lambda: reference_time)
    runner = _FakeRunner()
    runner.queue(_params_result([]))
    runner.queue(_err("no context"))

    snapshot = ops.state(root, name, runner=runner)

    assert snapshot.certs_present is True
    assert snapshot.client_cert_days_left == 30


def test_list_state_follows_discover_order(tmp_path: Path) -> None:
    ops = _import_instance_ops()
    root = _git_root(tmp_path)
    alpha = _instance_name("alpha")
    beta = _instance_name("beta")
    _seed_instance_dir(root, beta)
    _seed_instance_dir(root, alpha)
    runner = _FakeRunner()
    for _ in (alpha, beta):
        runner.queue(_params_result([]))
        runner.queue(_err("no context"))

    snapshots = ops.list_state(root, runner=runner)

    assert [snapshot.name for snapshot in snapshots] == sorted((alpha, beta))
    assert all(snapshot.params_present is False for snapshot in snapshots)


def test_list_state_without_instances_is_empty(tmp_path: Path) -> None:
    ops = _import_instance_ops()

    assert ops.list_state(_git_root(tmp_path), runner=_FakeRunner()) == ()


# ---------------------------------------------------------------------------
# Power operations
# ---------------------------------------------------------------------------


def test_stop_polls_until_stopped_with_injected_sleep(tmp_path: Path) -> None:
    ops = _import_instance_ops()
    root = tmp_path / "checkout"
    root.mkdir()
    name = _instance_name()
    instance_id = _instance_id()
    ops.link_id(root, name, instance_id)
    runner = _FakeRunner()
    runner.queue(_ok(""))
    runner.queue(_ok("stopping\n"))
    runner.queue(_ok("stopped\n"))
    sleeps: list[float] = []

    message = ops.stop(root, name, region=REGION, runner=runner, sleep=sleeps.append)

    assert "stopped" in message
    assert runner.calls[0] == (
        "aws",
        "ec2",
        "stop-instances",
        "--instance-ids",
        instance_id,
        "--region",
        REGION,
    )
    assert runner.calls[1] == _power_describe_argv(instance_id)
    assert runner.calls[2] == runner.calls[1]
    assert sleeps == [ops.POWER_POLL_SECONDS]


def test_stop_is_idempotent_when_already_stopped(tmp_path: Path) -> None:
    ops = _import_instance_ops()
    root = tmp_path / "checkout"
    root.mkdir()
    name = _instance_name()
    instance_id = _instance_id()
    ops.link_id(root, name, instance_id)
    runner = _FakeRunner()
    runner.queue(_ok(""))
    runner.queue(_ok("stopped\n"))
    sleeps: list[float] = []

    message = ops.stop(root, name, region=REGION, runner=runner, sleep=sleeps.append)

    assert "stopped" in message
    assert sleeps == []
    assert len(runner.calls) == 2


def test_stop_times_out_as_power_error(tmp_path: Path) -> None:
    ops = _import_instance_ops()
    root = tmp_path / "checkout"
    root.mkdir()
    name = _instance_name()
    instance_id = _instance_id()
    ops.link_id(root, name, instance_id)
    runner = _FakeRunner()
    runner.queue(_ok(""))
    runner.queue(*[_ok("stopping\n") for _ in range(ops.POWER_POLL_LIMIT)])
    sleeps: list[float] = []

    with pytest.raises(ops.PowerError) as exc_info:
        ops.stop(root, name, region=REGION, runner=runner, sleep=sleeps.append)

    assert "stopped" in str(exc_info.value)
    assert sleeps == [ops.POWER_POLL_SECONDS] * ops.POWER_POLL_LIMIT


def test_power_without_recorded_id_names_instance_link(tmp_path: Path) -> None:
    ops = _import_instance_ops()
    root = tmp_path / "checkout"
    root.mkdir()
    name = _instance_name()
    runner = _FakeRunner()

    with pytest.raises(ops.PowerError) as stop_error:
        ops.stop(root, name, region=REGION, runner=runner, sleep=lambda _seconds: None)
    with pytest.raises(ops.PowerError) as start_error:
        ops.start(root, name, region=REGION, runner=runner, sleep=lambda _seconds: None)

    assert "make instance-link" in str(stop_error.value)
    assert "make instance-link" in str(start_error.value)
    assert runner.calls == []


def test_start_waits_for_running_then_ssm_online(tmp_path: Path) -> None:
    ops = _import_instance_ops()
    root = tmp_path / "checkout"
    root.mkdir()
    name = _instance_name()
    instance_id = _instance_id()
    ops.link_id(root, name, instance_id)
    runner = _FakeRunner()
    runner.queue(_ok(""))
    runner.queue(_ok("pending\n"))
    runner.queue(_ok("running\n"))
    runner.queue(_ok("None\n"))
    runner.queue(_ok("Online\n"))
    sleeps: list[float] = []

    message = ops.start(root, name, region=REGION, runner=runner, sleep=sleeps.append)

    assert runner.calls[0] == (
        "aws",
        "ec2",
        "start-instances",
        "--instance-ids",
        instance_id,
        "--region",
        REGION,
    )
    assert runner.calls[3] == (
        "aws",
        "ssm",
        "describe-instance-information",
        "--filters",
        f"Key=InstanceIds,Values={instance_id}",
        "--query",
        "InstanceInformationList[0].PingStatus",
        "--output",
        "text",
        "--region",
        REGION,
    )
    assert sleeps == [ops.POWER_POLL_SECONDS, ops.POWER_POLL_SECONDS]
    assert "Online" in message
    assert f"make cert-install INSTANCE={name}" in message


def test_start_times_out_as_power_error_before_running(tmp_path: Path) -> None:
    ops = _import_instance_ops()
    root = tmp_path / "checkout"
    root.mkdir()
    name = _instance_name()
    instance_id = _instance_id()
    ops.link_id(root, name, instance_id)
    runner = _FakeRunner()
    runner.queue(_ok(""))
    runner.queue(*[_ok("pending\n") for _ in range(ops.POWER_POLL_LIMIT)])
    sleeps: list[float] = []

    with pytest.raises(ops.PowerError) as exc_info:
        ops.start(root, name, region=REGION, runner=runner, sleep=sleeps.append)

    assert "running" in str(exc_info.value)
    assert len(sleeps) == ops.POWER_POLL_LIMIT
    assert all("PingStatus" not in " ".join(call) for call in runner.calls)


# ---------------------------------------------------------------------------
# cleanup
# ---------------------------------------------------------------------------


def test_cleanup_lists_and_deletes_every_parameter(tmp_path: Path) -> None:
    ops = _import_instance_ops()
    root = _git_root(tmp_path)
    name = _instance_name()
    context = f"general-dev-{name}"
    prefix = f"/devcontainer/{name}/"
    server_key = f"{prefix}tls/server-key.pem"
    ca_cert = f"{prefix}tls/ca.pem"
    runner = _FakeRunner()
    runner.queue(_params_result([server_key, ca_cert]))
    runner.queue(_ok(""))
    runner.queue(_ok(""))
    runner.queue(_ok(""))  # docker context inspect: present
    runner.queue(_ok(""))  # docker context rm -f

    messages = ops.cleanup(root, name, region=REGION, runner=runner)

    assert runner.calls[0] == (
        "aws",
        "ssm",
        "describe-parameters",
        "--parameter-filters",
        f"Key=Name,Option=BeginsWith,Values={prefix}",
        "--output",
        "json",
        "--region",
        REGION,
    )
    assert runner.calls[1] == (
        "aws",
        "ssm",
        "delete-parameter",
        "--name",
        server_key,
        "--region",
        REGION,
    )
    assert runner.calls[2] == (
        "aws",
        "ssm",
        "delete-parameter",
        "--name",
        ca_cert,
        "--region",
        REGION,
    )
    assert runner.calls[3] == _context_inspect_argv(context)
    assert runner.calls[4] == ("docker", "context", "rm", "-f", context)
    assert any(server_key in message for message in messages)
    assert any(ca_cert in message for message in messages)


def test_cleanup_tolerates_absent_docker_context(tmp_path: Path) -> None:
    ops = _import_instance_ops()
    root = _git_root(tmp_path)
    name = _instance_name()
    context = f"general-dev-{name}"
    runner = _FakeRunner()
    runner.queue(_params_result([]))
    runner.queue(_err("no such context"))

    messages = ops.cleanup(root, name, region=REGION, runner=runner)

    assert all("rm" not in " ".join(call) for call in runner.calls)
    assert any(context in message and "absent" in message for message in messages)


def test_cleanup_unreachable_daemon_on_inspect_is_a_failure_not_absence(tmp_path: Path) -> None:
    """A nonzero inspect whose stderr is not docker's not-found answer is a
    failure -- a stopped daemon must not pass for an absent context, or
    cleanup would report success while the context survives.

    test_review round 2, MEDIUM: the original treated every nonzero inspect
    as absence. The SSM deletions are queued before the inspect and asserted
    afterward, proving a docker failure never hides parameter deletion.
    """
    ops = _import_instance_ops()
    root = _git_root(tmp_path)
    name = _instance_name()
    context = f"general-dev-{name}"
    parameter_name = f"/devcontainer/{name}/tls/ca.pem"
    runner = _FakeRunner()
    runner.queue(_params_result([parameter_name]))
    runner.queue(_ok(""))  # delete-parameter succeeds
    runner.queue(_err("Cannot connect to the Docker daemon at unix:///var/run/docker.sock"))

    with pytest.raises(ops.CleanupError) as exc_info:
        ops.cleanup(root, name, region=REGION, runner=runner)

    message = str(exc_info.value)
    assert f"inspect docker context {context}" in message
    assert "Cannot connect to the Docker daemon" in message
    assert "absent" not in message
    # The context was never touched: no `context rm` may follow a failed
    # inspect, and nothing reported the context as removed.
    assert all("rm" not in " ".join(call) for call in runner.calls)
    # SSM deletion happened first and is not hidden by the docker failure.
    assert (
        "aws",
        "ssm",
        "delete-parameter",
        "--name",
        parameter_name,
        "--region",
        REGION,
    ) in runner.calls


def test_cleanup_removes_certs_directory_including_id_file(tmp_path: Path) -> None:
    ops = _import_instance_ops()
    instances = _import_instances()
    root = _git_root(tmp_path)
    name = _instance_name()
    cert_directory = instances.certs_dir(name)
    ops.link_id(root, name, _instance_id())
    assert (cert_directory / ops.INSTANCE_ID_FILENAME).is_file()
    runner = _FakeRunner()
    runner.queue(_params_result([]))
    runner.queue(_err("no such context"))

    messages = ops.cleanup(root, name, region=REGION, runner=runner)

    assert not cert_directory.exists()
    assert any(str(cert_directory) in message and "removed" in message for message in messages)


def test_cleanup_aggregates_every_failure_into_one_error(tmp_path: Path) -> None:
    ops = _import_instance_ops()
    root = _git_root(tmp_path)
    name = _instance_name()
    context = f"general-dev-{name}"
    runner = _FakeRunner()
    runner.queue(_err("AccessDenied"))
    runner.queue(_ok(""))  # docker context inspect: present
    runner.queue(_err("cannot remove context"))

    with pytest.raises(ops.CleanupError) as exc_info:
        ops.cleanup(root, name, region=REGION, runner=runner)

    message = str(exc_info.value)
    assert "list SSM parameters" in message
    assert f"remove docker context {context}" in message


def test_cleanup_docker_failure_does_not_abort_ssm_or_certs(tmp_path: Path) -> None:
    ops = _import_instance_ops()
    instances = _import_instances()
    root = _git_root(tmp_path)
    name = _instance_name()
    parameter_name = f"/devcontainer/{name}/tls/ca.pem"
    cert_directory = instances.certs_dir(name)
    ops.link_id(root, name, _instance_id())
    runner = _FakeRunner()
    runner.queue(_params_result([parameter_name]))
    runner.queue(_ok(""))  # delete-parameter succeeds
    runner.queue(_ok(""))  # docker context inspect: present
    runner.queue(_err("cannot remove context"))

    with pytest.raises(ops.CleanupError) as exc_info:
        ops.cleanup(root, name, region=REGION, runner=runner)

    assert (
        "aws",
        "ssm",
        "delete-parameter",
        "--name",
        parameter_name,
        "--region",
        REGION,
    ) in runner.calls
    assert not cert_directory.exists()
    assert "remove docker context" in str(exc_info.value)
