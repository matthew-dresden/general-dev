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

    The checkout carries this repository's committed `.gitignore` -- read
    from the real checkout's HEAD, not its working tree, so a scratch repo
    never inherits uncommitted entries -- the same seeding
    `tests/test_gitignore_allowlist.py` uses for its scratch repositories,
    so a scaffold's per-instance ignore append exercises the real anchor
    rule rather than a test-owned stand-in.
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
    real_repo_root = _import_repo().find_root(Path(__file__).resolve().parent)
    committed_gitignore = subprocess.run(
        ["git", "-C", str(real_repo_root), "show", "HEAD:.gitignore"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    (root / ".gitignore").write_text(committed_gitignore, encoding="utf-8")
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
# scaffold, azure cloud
# ---------------------------------------------------------------------------


# The azure location fixture every azure scaffold test passes: a generated,
# location-shaped value, so no real region name is stored in this file.
def _azure_location() -> str:
    return f"testlocation-{uuid.uuid4().hex[:6]}"


def test_scaffold_azure_writes_the_azure_contract(tmp_path: Path) -> None:
    """The azure template carries the README's three parts with the azure
    inputs, issues no command, and validates nothing it only includes."""
    ops = _import_instance_ops()
    instances = _import_instances()
    root = _git_root(tmp_path)
    name = _instance_name("acme")
    location = _azure_location()
    runner = _FakeRunner()

    result = ops.scaffold(
        root, name, cloud=instances.ENGINE_KIND_AZURE, location=location, runner=runner
    )

    assert result.path == root / "remote-instances" / name / "terragrunt.hcl"
    assert result.ami is None, "the azure kind pins no image at scaffold time"
    assert runner.calls == [], "the azure scaffold issues no command at all"
    text = result.path.read_text(encoding="utf-8")
    # The README's conventions: the directory name is the identity, every
    # input is the operator's to edit, and the deploy command follows.
    assert f"# remote-instances/{name}/terragrunt.hcl" in text
    assert "The directory name IS this instance's identity" in text
    assert f"#   make instance-deploy INSTANCE={name}" in text
    assert 'include "root" {' in text
    assert 'path = find_in_parent_folders("root-azure.hcl")' in text
    assert (
        'path = "${dirname(find_in_parent_folders("root-azure.hcl"))}'
        '/_envcommon/remote-azure.hcl"' in text
    )
    assert f'instance_name = "{name}"' in text
    assert f'name_prefix   = "{name}"' in text
    assert f'location = "{location}"' in text
    assert f'vm_size  = "{ops.DEFAULT_AZURE_VM_SIZE}"' in text
    assert f"root_volume_size_gb = {ops.DEFAULT_VOLUME_SIZE_GB}" in text
    assert f"data_volume_size_gb = {ops.DEFAULT_VOLUME_SIZE_GB}" in text
    assert 'vnet_cidr   = "10.100.0.0/16"' in text
    assert 'subnet_cidr = "10.100.1.0/24"' in text
    assert f'Environment = "{name}"' in text
    # Forward-looking: the scaffold writes the declaration without checking
    # the layer or provider it names.
    assert not (root / "remote-instances" / "_envcommon" / "remote-azure.hcl").exists()
    assert not (root / "provider" / "azure").exists()
    assert any(f"created {result.path}" in message for message in result.messages)
    assert any(f"make instance-deploy INSTANCE={name}" in message for message in result.messages)


def test_scaffold_azure_vm_size_default_is_the_constant(tmp_path: Path) -> None:
    """The unpassed --vm-size default lands in the template from the one
    constant, which this test pins to the specified value."""
    ops = _import_instance_ops()
    instances = _import_instances()
    root = _git_root(tmp_path)
    runner = _FakeRunner()

    result = ops.scaffold(
        root,
        _instance_name(),
        cloud=instances.ENGINE_KIND_AZURE,
        location=_azure_location(),
        runner=runner,
    )

    assert ops.DEFAULT_AZURE_VM_SIZE == "Standard_D2pls_v5"
    assert f'vm_size  = "{ops.DEFAULT_AZURE_VM_SIZE}"' in result.path.read_text(encoding="utf-8")


def test_scaffold_azure_vm_size_override_lands_in_the_template(tmp_path: Path) -> None:
    ops = _import_instance_ops()
    instances = _import_instances()
    root = _git_root(tmp_path)
    runner = _FakeRunner()

    result = ops.scaffold(
        root,
        _instance_name(),
        cloud=instances.ENGINE_KIND_AZURE,
        location=_azure_location(),
        vm_size="Standard_D4pls_v5",
        runner=runner,
    )

    assert 'vm_size  = "Standard_D4pls_v5"' in result.path.read_text(encoding="utf-8")


def _git_root_gitignore_text() -> str:
    """The committed `.gitignore` text `_git_root` seeds a scratch checkout with.

    The azure refusal tests assert the gitignore is untouched; reading the
    same source `_git_root` copies from keeps the expected value one
    derivation from the fixture, not a second hand-typed copy.
    """
    repo = _import_repo()
    real_repo_root = repo.find_root(Path(__file__).resolve().parent)
    return subprocess.run(
        ["git", "-C", str(real_repo_root), "show", "HEAD:.gitignore"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def test_scaffold_azure_requires_a_location(tmp_path: Path) -> None:
    """No default location exists: the refusal names the input and writes nothing."""
    ops = _import_instance_ops()
    instances = _import_instances()
    root = _git_root(tmp_path)
    name = _instance_name()
    runner = _FakeRunner()

    with pytest.raises(ops.ScaffoldError) as exc_info:
        ops.scaffold(root, name, cloud=instances.ENGINE_KIND_AZURE, runner=runner)

    message = str(exc_info.value)
    assert "--location" in message and "LOCATION=<location>" in message
    assert runner.calls == []
    assert not (root / "remote-instances" / name).exists()
    assert (root / ".gitignore").read_text(encoding="utf-8") == _git_root_gitignore_text()


def test_scaffold_azure_refuses_a_region_naming_the_location_equivalent(
    tmp_path: Path,
) -> None:
    """The EC2 region input is refused on azure: the message names the azure
    equivalent (--location / LOCATION=) and nothing is written."""
    ops = _import_instance_ops()
    instances = _import_instances()
    root = _git_root(tmp_path)
    name = _instance_name()
    runner = _FakeRunner()

    with pytest.raises(ops.ScaffoldError) as exc_info:
        ops.scaffold(
            root,
            name,
            cloud=instances.ENGINE_KIND_AZURE,
            region=REGION,
            location=_azure_location(),
            runner=runner,
        )

    message = str(exc_info.value)
    assert "region" in message.lower()
    assert "--location" in message
    assert runner.calls == []
    assert not (root / "remote-instances" / name).exists()


def test_scaffold_azure_refuses_an_ami(tmp_path: Path) -> None:
    """There is no AMI input on azure: the refusal says so and writes nothing."""
    ops = _import_instance_ops()
    instances = _import_instances()
    root = _git_root(tmp_path)
    name = _instance_name()
    runner = _FakeRunner()

    with pytest.raises(ops.ScaffoldError) as exc_info:
        ops.scaffold(
            root,
            name,
            cloud=instances.ENGINE_KIND_AZURE,
            ami=_ami_id(),
            location=_azure_location(),
            runner=runner,
        )

    message = str(exc_info.value)
    assert "AMI" in message
    assert runner.calls == []
    assert not (root / "remote-instances" / name).exists()


def test_scaffold_azure_refuses_an_unknown_cloud(tmp_path: Path) -> None:
    """The gcp kind is a real choice now; only clouds none of the arms serve
    are refused, naming every cloud the scaffold writes."""
    ops = _import_instance_ops()
    root = _git_root(tmp_path)
    name = _instance_name()
    runner = _FakeRunner()

    with pytest.raises(ops.ScaffoldError) as exc_info:
        ops.scaffold(root, name, cloud="alicloud", runner=runner)

    message = str(exc_info.value)
    assert "alicloud" in message
    assert "ec2" in message and "azure" in message and "gcp" in message
    assert runner.calls == []
    assert not (root / "remote-instances" / name).exists()


def test_scaffold_azure_skips_claimed_cidrs_like_the_ec2_scaffold(tmp_path: Path) -> None:
    """The same CIDR-allocation helper answers both clouds: a sibling claiming
    10.100 pushes the azure deployment to the next free block."""
    ops = _import_instance_ops()
    instances = _import_instances()
    root = _git_root(tmp_path)
    _sibling_with_cidr(root, "claimed", "10.100.0.0/16")
    runner = _FakeRunner()

    result = ops.scaffold(
        root,
        _instance_name(),
        cloud=instances.ENGINE_KIND_AZURE,
        location=_azure_location(),
        runner=runner,
    )

    assert result.vpc_cidr == "10.101.0.0/16"
    assert 'vnet_cidr   = "10.101.0.0/16"' in result.path.read_text(encoding="utf-8")
    assert 'subnet_cidr = "10.101.1.0/24"' in result.path.read_text(encoding="utf-8")


def test_scaffold_azure_appends_the_same_gitignore_entry(tmp_path: Path) -> None:
    ops = _import_instance_ops()
    instances = _import_instances()
    root = _git_root(tmp_path)
    name = _instance_name()

    ops.scaffold(
        root,
        name,
        cloud=instances.ENGINE_KIND_AZURE,
        location=_azure_location(),
        runner=_FakeRunner(),
    )

    lines = (root / ".gitignore").read_text(encoding="utf-8").splitlines()
    entry = f"remote-instances/{name}/"
    assert lines.count(entry) == 1
    assert lines.index(entry) == lines.index(ops.GITIGNORE_SCAFFOLD_ANCHOR) + 1


def test_scaffold_azure_ec2_region_default_applies_only_when_unset(tmp_path: Path) -> None:
    """A region-less EC2 scaffold applies DEFAULT_REGION itself; the CLI no
    longer fills one in ahead of the azure refusal."""
    ops = _import_instance_ops()
    root = _git_root(tmp_path)
    ami = _ami_id()
    runner = _FakeRunner()
    runner.queue(_ok(ami))

    result = ops.scaffold(root, _instance_name(), ami=ami, runner=runner)

    assert ops.DEFAULT_REGION == "us-east-1"
    text = result.path.read_text(encoding="utf-8")
    assert f'availability_zone  = "{ops.DEFAULT_REGION}a"' in text


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


# ---------------------------------------------------------------------------
# state / list_state: the ssh engine kind
# ---------------------------------------------------------------------------


def _write_engine_manifest(
    root: Path,
    name: str,
    *,
    host: str,
    user: str,
    port: int,
) -> Path:
    """A valid engine.json under `remote-instances/<name>/`, from generated values."""
    instances = _import_instances()
    manifest = instances.engine_manifest_path(root, name)
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        json.dumps(
            {
                "kind": instances.ENGINE_KIND_SSH,
                "host": host,
                "user": user,
                "port": port,
                "docker_addr": "/var/run/docker.sock",
            }
        ),
        encoding="utf-8",
    )
    return manifest


def _engine_host() -> str:
    """A host-shaped value unique per call, under the reserved example.invalid domain."""
    return f"engine-{uuid.uuid4().hex[:8]}.example.invalid"


def _engine_user() -> str:
    """A user name unique per call."""
    return f"dev-{uuid.uuid4().hex[:8]}"


def _engine_port() -> int:
    """A valid non-default port number unique per call."""
    return 20000 + uuid.uuid4().int % 20000


def test_state_for_an_ssh_engine_probes_reachability_and_skips_the_ec2_probes(
    tmp_path: Path,
) -> None:
    ops = _import_instance_ops()
    root = _git_root(tmp_path)
    name = _instance_name("engine")
    host, user, port = _engine_host(), _engine_user(), _engine_port()
    _write_engine_manifest(root, name, host=host, user=user, port=port)
    runner = _FakeRunner()
    runner.queue(_ok("27.0.3\n"))  # the reach probe: docker answers through ssh
    runner.queue(_err("no such context: anything"))  # the context inspect

    snapshot = ops.state(root, name, runner=runner)

    assert snapshot.engine_reachable is True
    assert snapshot.ec2_state is None
    assert snapshot.params_present is None
    assert snapshot.recorded_id is None
    assert snapshot.certs_present is False
    assert snapshot.context_exists is False
    assert snapshot.lookup_error is None
    # The reach probe is the ssh one-shot the module documents; no aws call
    # of any kind runs for an ssh engine.
    assert runner.calls[0] == (
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=5",
        "-p",
        str(port),
        f"{user}@{host}",
        "docker",
        "version",
        "--format",
        "{{.Server.Version}}",
    )
    assert all(call[0] != "aws" for call in runner.calls)


def test_state_reports_an_unreachable_engine_as_an_answer_not_an_error(
    tmp_path: Path,
) -> None:
    """`engine_reachable` is False and no lookup_error: unreachable is the
    probe's answer, and a listing reporting it must still exit zero."""
    ops = _import_instance_ops()
    root = _git_root(tmp_path)
    name = _instance_name("engine")
    _write_engine_manifest(
        root, name, host=_engine_host(), user=_engine_user(), port=_engine_port()
    )
    runner = _FakeRunner()
    runner.queue(_err("Connection refused"))  # the reach probe failed
    runner.queue(_err("no such context: anything"))

    snapshot = ops.state(root, name, runner=runner)

    assert snapshot.engine_reachable is False
    assert snapshot.lookup_error is None


def test_state_ssh_probe_honors_a_configured_connect_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ops = _import_instance_ops()
    root = _git_root(tmp_path)
    name = _instance_name("engine")
    _write_engine_manifest(
        root, name, host=_engine_host(), user=_engine_user(), port=_engine_port()
    )
    monkeypatch.setenv(ops.ENGINE_PROBE_CONNECT_TIMEOUT_ENV_VAR, "2.5")
    runner = _FakeRunner()
    runner.queue(_ok("27.0.3\n"))
    runner.queue(_err("no such context: anything"))

    snapshot = ops.state(root, name, runner=runner)

    assert snapshot.engine_reachable is True
    assert runner.calls[0][4] == "ConnectTimeout=2.5"


def test_state_invalid_probe_timeout_is_a_lookup_error_and_skips_the_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A timeout value the configuration reader refuses is a configuration
    failure, not an unreachable engine: the probe never runs, the answer is
    None, and the reason names the variable."""
    ops = _import_instance_ops()
    root = _git_root(tmp_path)
    name = _instance_name("engine")
    _write_engine_manifest(
        root, name, host=_engine_host(), user=_engine_user(), port=_engine_port()
    )
    monkeypatch.setenv(ops.ENGINE_PROBE_CONNECT_TIMEOUT_ENV_VAR, "not-a-number")
    runner = _FakeRunner()
    runner.queue(_err("no such context: anything"))  # the context inspect only

    snapshot = ops.state(root, name, runner=runner)

    assert snapshot.engine_reachable is None
    assert snapshot.lookup_error is not None
    assert ops.ENGINE_PROBE_CONNECT_TIMEOUT_ENV_VAR in snapshot.lookup_error
    assert all(call[0] != "ssh" for call in runner.calls)


def test_state_for_a_corrupt_manifest_keeps_the_ssh_kind_and_records_the_reason(
    tmp_path: Path,
) -> None:
    """A manifest that parses as JSON but breaks a field rule is a probe
    failure, never a kind flip: no EC2 probe may run against a directory
    that declares (however brokenly) an engine."""
    ops = _import_instance_ops()
    instances = _import_instances()
    root = _git_root(tmp_path)
    name = _instance_name("engine")
    manifest = root / "remote-instances" / name / instances.ENGINE_MANIFEST_FILENAME
    manifest.parent.mkdir(parents=True)
    manifest.write_text('{"kind": "ssh", "host": ""}', encoding="utf-8")
    runner = _FakeRunner()
    runner.queue(_err("no such context: anything"))  # the context inspect only

    snapshot = ops.state(root, name, runner=runner)

    assert snapshot.engine_reachable is None
    assert snapshot.ec2_state is None
    assert snapshot.params_present is None
    assert snapshot.lookup_error is not None
    assert "host" in snapshot.lookup_error
    assert all(call[0] != "aws" and call[0] != "ssh" for call in runner.calls)


# ---------------------------------------------------------------------------
# engine_remove (the ssh engine teardown)
# ---------------------------------------------------------------------------


def _seed_engine(root: Path, name: str, *, host: str, user: str, port: int) -> None:
    """The registration an engine-init writes: the directory and its manifest."""
    manifest = _write_engine_manifest(root, name, host=host, user=user, port=port)
    assert manifest.is_file()


def _engine_ps_argv(root: Path, context: str) -> tuple[str, ...]:
    """The container probe engine_remove issues, through the shared label."""
    containers_module = importlib.import_module("devcontainer_config.containers")
    return (
        "docker",
        "--context",
        context,
        "ps",
        "-a",
        "--filter",
        containers_module.project_label(root),
        "--format",
        "{{.Names}}",
    )


class _RecordingStopper:
    """The injected forward stopper: records the name, answers success."""

    def __init__(self, timeline: list[object] | None = None) -> None:
        self.calls: list[str] = []
        self._timeline = timeline
        self.error: Exception | None = None

    def __call__(self, name: str) -> str:
        self.calls.append(name)
        if self._timeline is not None:
            self._timeline.append(("stop-forward", name))
        if self.error is not None:
            raise self.error
        return f"stopped forward for {name!r} (pid 424242)"


class _TimelineRunner(_FakeRunner):
    """A `_FakeRunner` that mirrors every argv into a shared timeline, so a
    test can assert the interleaving of runner-issued commands with the
    injected forward stopper."""

    def __init__(self, timeline: list[object]) -> None:
        super().__init__()
        self._timeline = timeline

    def __call__(
        self, argv: Sequence[str], stdin: str | None, *, env: Mapping[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        self._timeline.append(tuple(argv))
        return super().__call__(argv, stdin, env=env)


def test_engine_remove_refuses_an_ec2_instance_naming_instance_destroy(
    tmp_path: Path,
) -> None:
    ops = _import_instance_ops()
    root = _git_root(tmp_path)
    name = _instance_name("ec2")
    _seed_instance_dir(root, name)
    runner = _FakeRunner()
    stopper = _RecordingStopper()

    with pytest.raises(ops.EngineRemoveError) as exc_info:
        ops.engine_remove(root, name, runner=runner, stop_forward=stopper)

    message = str(exc_info.value)
    assert "make instance-destroy" in message
    assert f"INSTANCE={name}" in message
    assert runner.calls == [], "a refusal runs no command"
    assert stopper.calls == []
    assert (root / "remote-instances" / name).is_dir()


def test_engine_remove_refuses_while_a_project_container_exists(tmp_path: Path) -> None:
    ops = _import_instance_ops()
    instances = _import_instances()
    root = _git_root(tmp_path)
    name = _instance_name("engine")
    _seed_engine(root, name, host=_engine_host(), user=_engine_user(), port=_engine_port())
    runner = _FakeRunner()
    runner.queue(_ok("the-project-devcontainer\n"))  # the container probe
    stopper = _RecordingStopper()

    with pytest.raises(ops.EngineRemoveError) as exc_info:
        ops.engine_remove(root, name, runner=runner, stop_forward=stopper)

    message = str(exc_info.value)
    assert "the-project-devcontainer" in message
    assert f"make clean ENGINE={name}" in message
    assert "CONFIRM=remove" in message
    assert len(runner.calls) == 1, "a refusal stops at the container probe"
    assert stopper.calls == []
    assert instances.engine_manifest_path(root, name).is_file()


def test_engine_remove_refuses_when_the_container_probe_cannot_answer(
    tmp_path: Path,
) -> None:
    """A probe that cannot answer is a refusal, never a pass: removing an
    engine whose containers could not be counted could strand them."""
    ops = _import_instance_ops()
    root = _git_root(tmp_path)
    name = _instance_name("engine")
    _seed_engine(root, name, host=_engine_host(), user=_engine_user(), port=_engine_port())
    runner = _FakeRunner()
    runner.queue(_err("Cannot connect to the Docker daemon"))  # the container probe
    stopper = _RecordingStopper()

    with pytest.raises(ops.EngineRemoveError) as exc_info:
        ops.engine_remove(root, name, runner=runner, stop_forward=stopper)

    message = str(exc_info.value)
    assert "could not check the containers" in message
    assert f"make connect-start INSTANCE={name}" in message
    assert stopper.calls == []


def test_engine_remove_stops_the_forward_then_removes_everything_in_order(
    tmp_path: Path,
) -> None:
    ops = _import_instance_ops()
    instances = _import_instances()
    root = _git_root(tmp_path)
    name = _instance_name("engine")
    _seed_engine(root, name, host=_engine_host(), user=_engine_user(), port=_engine_port())
    context = instances.docker_context(root, name)
    cert_directory = instances.certs_dir(name)
    cert_directory.mkdir(parents=True)
    (cert_directory / ops.INSTANCE_ID_FILENAME).write_text("i-stale\n", encoding="utf-8")
    (cert_directory / "forward.json").write_text("{}\n", encoding="utf-8")
    (cert_directory / "forward.log").write_text("log\n", encoding="utf-8")
    timeline: list[object] = []
    runner = _TimelineRunner(timeline)
    runner.queue(_ok(""))  # the container probe: no containers
    runner.queue(_ok(""))  # docker context inspect: present
    runner.queue(_ok(""))  # docker context rm -f
    stopper = _RecordingStopper(timeline)

    messages = ops.engine_remove(root, name, runner=runner, stop_forward=stopper)

    assert stopper.calls == [name]
    # Order: the container probe, then the forward stop, then the context
    # removal -- nothing is removed before the forward is down. The
    # timeline carries raw argv tuples from the runner and ("stop-forward",
    # name) markers from the stopper, so the interleaving is direct.
    assert timeline[:2] == [
        _engine_ps_argv(root, context),
        ("stop-forward", name),
    ]
    assert runner.calls[1:] == [
        _context_inspect_argv(context),
        ("docker", "context", "rm", "-f", context),
    ]
    assert not cert_directory.exists(), "the forward record and id die with the directory"
    assert not (root / "remote-instances" / name).exists(), "the registration is removed"
    joined = "\n".join(messages)
    assert "removed docker context" in joined
    assert "removed certificate directory" in joined
    assert "removed engine registration" in joined


def test_engine_remove_tolerates_an_absent_docker_context(tmp_path: Path) -> None:
    ops = _import_instance_ops()
    instances = _import_instances()
    root = _git_root(tmp_path)
    name = _instance_name("engine")
    _seed_engine(root, name, host=_engine_host(), user=_engine_user(), port=_engine_port())
    context = instances.docker_context(root, name)
    runner = _FakeRunner()
    runner.queue(_ok(""))  # the container probe
    runner.queue(_err(f"no such context: {context}"))  # the context inspect
    stopper = _RecordingStopper()

    messages = ops.engine_remove(root, name, runner=runner, stop_forward=stopper)

    joined = "\n".join(messages)
    assert f"docker context {context!r} is absent" in joined
    assert all(call[1:3] != ("context", "rm") for call in runner.calls)
    assert not (root / "remote-instances" / name).exists()


def test_engine_remove_reports_an_absent_registration_and_certs_as_nothing_to_remove(
    tmp_path: Path,
) -> None:
    """An engine whose forward never ran and whose context was never created
    still removes cleanly: absence is the desired state, reported as such."""
    ops = _import_instance_ops()
    root = _git_root(tmp_path)
    name = _instance_name("engine")
    _seed_engine(root, name, host=_engine_host(), user=_engine_user(), port=_engine_port())
    instances = _import_instances()
    context = instances.docker_context(root, name)
    runner = _FakeRunner()
    runner.queue(_ok(""))  # the container probe
    runner.queue(_err(f"no such context: {context}"))
    stopper = _RecordingStopper()

    messages = ops.engine_remove(root, name, runner=runner, stop_forward=stopper)

    joined = "\n".join(messages)
    assert "no certificate directory" in joined
    assert "nothing to remove" in joined


def test_engine_remove_aggregates_failures_and_still_attempts_every_removal(
    tmp_path: Path,
) -> None:
    ops = _import_instance_ops()
    instances = _import_instances()
    forwards = importlib.import_module("devcontainer_config.forwards")
    root = _git_root(tmp_path)
    name = _instance_name("engine")
    _seed_engine(root, name, host=_engine_host(), user=_engine_user(), port=_engine_port())
    context = instances.docker_context(root, name)
    cert_directory = instances.certs_dir(name)
    cert_directory.mkdir(parents=True)
    runner = _FakeRunner()
    runner.queue(_ok(""))  # the container probe
    runner.queue(_err("Cannot connect to the Docker daemon"))  # context inspect
    stopper = _RecordingStopper()
    stopper.error = forwards.ForwardError("the process group refused to stop")

    with pytest.raises(ops.EngineRemoveError) as exc_info:
        ops.engine_remove(root, name, runner=runner, stop_forward=stopper)

    message = str(exc_info.value)
    assert f"stop forward for {name!r}" in message
    assert f"inspect docker context {context}" in message
    assert "did not complete" in message
    # A failed context removal never hides the filesystem removals: both
    # were attempted and both succeeded.
    assert not cert_directory.exists()
    assert not (root / "remote-instances" / name).exists()


def test_engine_remove_rejects_an_invalid_name_before_anything_runs(
    tmp_path: Path,
) -> None:
    ops = _import_instance_ops()
    root = _git_root(tmp_path)
    runner = _FakeRunner()
    stopper = _RecordingStopper()

    for bad_name in ("../escape", "a/b", ""):
        with pytest.raises(ops.EngineRemoveError):
            ops.engine_remove(root, bad_name, runner=runner, stop_forward=stopper)

    assert runner.calls == []
    assert stopper.calls == []


# ---------------------------------------------------------------------------
# state / list_state: the azure engine kind
# ---------------------------------------------------------------------------


def _write_azure_scaffold(root: Path, name: str, location: str = "westeurope") -> Path:
    """The per-instance terragrunt.hcl the azure scaffold writes, in miniature.

    The include text is what `read_kind` keys the azure kind on.
    """
    instances = _import_instances()
    directory = root / "remote-instances" / name
    directory.mkdir(parents=True, exist_ok=True)
    hcl = directory / "terragrunt.hcl"
    hcl.write_text(
        'include "envcommon" {\n'
        '  path = "${dirname(find_in_parent_folders("root-azure.hcl"))}'
        '/_envcommon/remote-azure.hcl"\n'
        "}\n"
        "\n"
        "inputs = {\n"
        f'  instance_name = "{name}"\n'
        f'  location = "{location}"\n'
        "}\n",
        encoding="utf-8",
    )
    del instances
    return hcl


def _write_engine_address(
    root: Path, name: str, *, resource_group: str, vm_name: str, vault_name: str
) -> None:
    """The relay address record the deploy writes, from generated values."""
    instances = _import_instances()
    instances.write_engine_address(
        root,
        name,
        {
            "vm_name": vm_name,
            "vm_resource_id": (
                f"/subscriptions/{uuid.uuid4().hex}/resourceGroups/{resource_group}"
                f"/providers/Microsoft.Compute/virtualMachines/{vm_name}"
            ),
            "resource_group": resource_group,
            "relay_namespace": f"gd-{uuid.uuid4().hex[:8]}.servicebus.windows.net",
            "hybrid_connection": f"gd-{uuid.uuid4().hex[:8]}",
            "vault_name": vault_name,
            "vault_uri": f"https://{vault_name}.vault.azure.net",
            "location": "westeurope",
        },
    )


def _azure_vm_names() -> tuple[str, str]:
    """A (resource_group, vm_name) pair, generated per call."""
    suffix = uuid.uuid4().hex[:8]
    return f"gd-rg-{suffix}", f"gd-vm-{suffix}"


def _vm_show_result(power: str) -> subprocess.CompletedProcess[str]:
    """The `az vm show -d --query {power:...}` answer for `power`."""
    return _ok(json.dumps({"power": power, "provisioning": "Succeeded"}))


def _vm_show_argv(resource_group: str, vm_name: str) -> tuple[str, ...]:
    return (
        "az",
        "vm",
        "show",
        "-g",
        resource_group,
        "-n",
        vm_name,
        "-d",
        "--query",
        "{power:powerState, provisioning:provisioningState}",
        "--output",
        "json",
    )


def _secret_show_argv(vault_name: str, secret_name: str) -> tuple[str, ...]:
    return (
        "az",
        "keyvault",
        "secret",
        "show",
        "--vault-name",
        vault_name,
        "--name",
        secret_name,
        "--query",
        "id",
        "--output",
        "tsv",
    )


def test_state_for_an_azure_engine_probes_the_vm_and_the_vault(
    tmp_path: Path,
) -> None:
    ops = _import_instance_ops()
    certs = _import_certs()
    root = _git_root(tmp_path)
    name = _instance_name("azinst")
    resource_group, vm_name = _azure_vm_names()
    vault_name = f"gd-vault-{uuid.uuid4().hex[:8]}"
    _write_azure_scaffold(root, name)
    _write_engine_address(
        root, name, resource_group=resource_group, vm_name=vm_name, vault_name=vault_name
    )
    runner = _FakeRunner()
    runner.queue(_vm_show_result("VM running"))
    for _ in certs.AZURE_PUBLISHABLE.values():
        runner.queue(_ok(f"https://{vault_name}.vault.azure.net/secrets/x\n"))
    runner.queue(_err("no such context: anything"))

    snapshot = ops.state(root, name, runner=runner)

    assert snapshot.azure_state == "VM running"
    assert snapshot.ec2_state is None
    assert snapshot.recorded_id == vm_name, "the record's VM name is the azure row's display id"
    assert snapshot.params_present is True
    assert snapshot.certs_present is False
    assert snapshot.context_exists is False
    assert snapshot.lookup_error is None
    assert runner.calls[0] == _vm_show_argv(resource_group, vm_name)
    assert runner.calls[1:4] == [
        _secret_show_argv(vault_name, secret_name)
        for secret_name in certs.AZURE_PUBLISHABLE.values()
    ]
    assert all(call[0] != "aws" for call in runner.calls), (
        "an azure engine runs no aws probe at all"
    )


def test_state_azure_a_missing_secret_answers_params_false(
    tmp_path: Path,
) -> None:
    ops = _import_instance_ops()
    certs = _import_certs()
    root = _git_root(tmp_path)
    name = _instance_name("azinst")
    resource_group, vm_name = _azure_vm_names()
    vault_name = f"gd-vault-{uuid.uuid4().hex[:8]}"
    _write_azure_scaffold(root, name)
    _write_engine_address(
        root, name, resource_group=resource_group, vm_name=vm_name, vault_name=vault_name
    )
    runner = _FakeRunner()
    runner.queue(_vm_show_result("VM running"))
    secrets = iter(certs.AZURE_PUBLISHABLE.values())
    runner.queue(_ok(f"https://{vault_name}.vault.azure.net/secrets/x\n"))  # first present
    next(secrets)
    runner.queue(_err("(SecretNotFound) not found", returncode=1))  # second missing
    runner.queue(_err("no such context: anything"))  # the context inspect

    snapshot = ops.state(root, name, runner=runner)

    assert snapshot.params_present is False
    assert snapshot.lookup_error is None


def test_state_azure_a_vault_probe_failure_is_a_lookup_error(
    tmp_path: Path,
) -> None:
    ops = _import_instance_ops()
    root = _git_root(tmp_path)
    name = _instance_name("azinst")
    resource_group, vm_name = _azure_vm_names()
    vault_name = f"gd-vault-{uuid.uuid4().hex[:8]}"
    _write_azure_scaffold(root, name)
    _write_engine_address(
        root, name, resource_group=resource_group, vm_name=vm_name, vault_name=vault_name
    )
    runner = _FakeRunner()
    runner.queue(_vm_show_result("VM running"))
    runner.queue(_err("(AuthorizationFailed) denied"))
    runner.queue(_err("no such context: anything"))  # the context inspect

    snapshot = ops.state(root, name, runner=runner)

    assert snapshot.params_present is None
    assert snapshot.lookup_error is not None
    assert "AuthorizationFailed" in snapshot.lookup_error


def test_state_azure_a_missing_record_degrades_with_the_deploy_remedy(
    tmp_path: Path,
) -> None:
    ops = _import_instance_ops()
    root = _git_root(tmp_path)
    name = _instance_name("azinst")
    _write_azure_scaffold(root, name)
    runner = _FakeRunner()
    runner.queue(_err("no such context: anything"))

    snapshot = ops.state(root, name, runner=runner)

    assert snapshot.azure_state is None
    assert snapshot.params_present is None
    assert snapshot.recorded_id is None
    assert snapshot.lookup_error is not None
    assert "engine-address.json" in snapshot.lookup_error
    assert f"make instance-deploy INSTANCE={name}" in snapshot.lookup_error
    assert all(call[0] != "az" for call in runner.calls), (
        "a row with no record runs no az probe at all"
    )


def test_state_azure_a_record_missing_its_targets_degrades(
    tmp_path: Path,
) -> None:
    ops = _import_instance_ops()
    root = _git_root(tmp_path)
    name = _instance_name("azinst")
    _write_azure_scaffold(root, name)
    _write_engine_address(root, name, resource_group="gd-rg", vm_name="", vault_name="gd-vault")
    runner = _FakeRunner()
    runner.queue(_err("no such context: anything"))

    snapshot = ops.state(root, name, runner=runner)

    assert snapshot.azure_state is None
    assert snapshot.lookup_error is not None
    assert "vm_name" in snapshot.lookup_error


# ---------------------------------------------------------------------------
# Power operations, azure arm
# ---------------------------------------------------------------------------


def _power_view_argv(resource_group: str, vm_name: str) -> tuple[str, ...]:
    """The instance-view power read the azure poll issues, through the shared builder.

    The builder names the sub-arguments; the module's `_run_az` prefixes the
    binary, so the recorded argv carries it.
    """
    transport_azure = importlib.import_module("devcontainer_config.transport_azure")
    return ("az", *transport_azure.build_vm_power_argv(resource_group, vm_name))


def test_stop_azure_deallocates_and_polls_to_deallocated(tmp_path: Path) -> None:
    ops = _import_instance_ops()
    root = _git_root(tmp_path)
    name = _instance_name("azinst")
    resource_group, vm_name = _azure_vm_names()
    _write_azure_scaffold(root, name)
    _write_engine_address(
        root, name, resource_group=resource_group, vm_name=vm_name, vault_name="gd-vault"
    )
    sleeps: list[float] = []
    runner = _FakeRunner()
    runner.queue(_ok("{}"))  # the deallocate call
    runner.queue(_ok("PowerState/stopping\n"))  # the poll: not yet
    runner.queue(_ok("PowerState/deallocated\n"))  # the poll: done

    message = ops.stop(root, name, region=REGION, runner=runner, sleep=sleeps.append)

    assert vm_name in message
    assert "deallocated" in message
    assert runner.calls[0] == ("az", "vm", "deallocate", "-g", resource_group, "-n", vm_name)
    assert runner.calls[1] == _power_view_argv(resource_group, vm_name)
    assert sleeps == [ops.POWER_POLL_SECONDS], "one not-yet answer paced one poll interval"


def test_stop_azure_is_idempotent_when_already_deallocated(tmp_path: Path) -> None:
    ops = _import_instance_ops()
    root = _git_root(tmp_path)
    name = _instance_name("azinst")
    resource_group, vm_name = _azure_vm_names()
    _write_azure_scaffold(root, name)
    _write_engine_address(
        root, name, resource_group=resource_group, vm_name=vm_name, vault_name="gd-vault"
    )
    sleeps: list[float] = []
    runner = _FakeRunner()
    runner.queue(_ok("{}"))
    runner.queue(_ok("PowerState/deallocated\n"))

    message = ops.stop(root, name, region=REGION, runner=runner, sleep=sleeps.append)

    assert "deallocated" in message
    assert sleeps == [], "the first read answers the target, so no waiting happens"


def test_stop_azure_timeout_names_the_last_observed_state(tmp_path: Path) -> None:
    ops = _import_instance_ops()
    root = _git_root(tmp_path)
    name = _instance_name("azinst")
    resource_group, vm_name = _azure_vm_names()
    _write_azure_scaffold(root, name)
    _write_engine_address(
        root, name, resource_group=resource_group, vm_name=vm_name, vault_name="gd-vault"
    )
    runner = _FakeRunner()
    runner.queue(_ok("{}"))
    for _ in range(ops.POWER_POLL_LIMIT):
        runner.queue(_ok("PowerState/stopping\n"))

    with pytest.raises(ops.PowerError) as exc_info:
        ops.stop(root, name, region=REGION, runner=runner, sleep=lambda _s: None)

    message = str(exc_info.value)
    assert "PowerState/stopping" in message
    assert str(ops.POWER_POLL_LIMIT) in message
    assert vm_name in message


def test_azure_power_without_a_record_names_the_deploy(tmp_path: Path) -> None:
    ops = _import_instance_ops()
    root = _git_root(tmp_path)
    name = _instance_name("azinst")
    _write_azure_scaffold(root, name)
    runner = _FakeRunner()

    with pytest.raises(ops.PowerError) as exc_info:
        ops.stop(root, name, region=REGION, runner=runner, sleep=lambda _s: None)

    message = str(exc_info.value)
    assert "engine-address.json" in message
    assert f"make instance-deploy INSTANCE={name}" in message
    assert runner.calls == []


def test_start_azure_waits_for_running_then_the_agent(tmp_path: Path) -> None:
    ops = _import_instance_ops()
    root = _git_root(tmp_path)
    name = _instance_name("azinst")
    resource_group, vm_name = _azure_vm_names()
    _write_azure_scaffold(root, name)
    _write_engine_address(
        root, name, resource_group=resource_group, vm_name=vm_name, vault_name="gd-vault"
    )
    sleeps: list[float] = []
    runner = _FakeRunner()
    runner.queue(_ok("{}"))  # the start call
    runner.queue(_ok("PowerState/running\n"))  # the power read
    runner.queue(_ok("Starting\n"))  # the agent read: not yet
    runner.queue(_ok("PowerState/running\n"))  # the second loop's power read
    runner.queue(_ok("Ready\n"))  # the agent read: ready

    message = ops.start(root, name, region=REGION, runner=runner, sleep=sleeps.append)

    assert vm_name in message
    assert "running" in message and "Ready" in message
    assert runner.calls[0] == ("az", "vm", "start", "-g", resource_group, "-n", vm_name)
    assert sleeps == [5.0], "the not-yet agent answer paced one poll interval"


def test_start_azure_surfaces_a_failed_readiness_read_as_a_power_error(
    tmp_path: Path,
) -> None:
    """The readiness wait is transport_azure's (the one home of the poll),
    and its failures cross into this module only as `PowerError`."""
    ops = _import_instance_ops()
    root = _git_root(tmp_path)
    name = _instance_name("azinst")
    resource_group, vm_name = _azure_vm_names()
    _write_azure_scaffold(root, name)
    _write_engine_address(
        root, name, resource_group=resource_group, vm_name=vm_name, vault_name="gd-vault"
    )
    runner = _FakeRunner()
    runner.queue(_ok("{}"))  # the start call
    runner.queue(_err("az: login required"))  # the first readiness read fails

    with pytest.raises(ops.PowerError) as exc_info:
        ops.start(root, name, region=REGION, runner=runner, sleep=lambda _s: None)

    assert "login required" in str(exc_info.value)


# ---------------------------------------------------------------------------
# cleanup: the azure arm
# ---------------------------------------------------------------------------


def test_cleanup_azure_skips_the_ssm_surface_and_removes_context_and_certs(
    tmp_path: Path,
) -> None:
    ops = _import_instance_ops()
    root = _git_root(tmp_path)
    name = _instance_name("azinst")
    _write_azure_scaffold(root, name)
    _write_engine_address(
        root, name, resource_group="gd-rg", vm_name="gd-vm", vault_name="gd-vault"
    )
    runner = _FakeRunner()
    runner.queue(_err('context "nope": context not found'))  # the context inspect: absent

    messages = ops.cleanup(root, name, region=REGION, runner=runner)

    assert any("absent; nothing to remove" in message for message in messages)
    assert all(call[0] != "aws" for call in runner.calls), (
        "an azure engine has no SSM surface for cleanup to touch"
    )


def test_cleanup_azure_still_aggregates_failures(tmp_path: Path) -> None:
    ops = _import_instance_ops()
    root = _git_root(tmp_path)
    name = _instance_name("azinst")
    _write_azure_scaffold(root, name)
    runner = _FakeRunner()
    runner.queue(_err("docker daemon exploded"))  # the context inspect fails for real

    with pytest.raises(ops.CleanupError) as exc_info:
        ops.cleanup(root, name, region=REGION, runner=runner)

    message = str(exc_info.value)
    assert "docker daemon exploded" in message
    assert all(call[0] != "aws" for call in runner.calls)


# ---------------------------------------------------------------------------
# The gcp scaffold arm: the template, the sanitized-name inputs, and the
# per-cloud refusals.
# ---------------------------------------------------------------------------


def _gcp_project_id() -> str:
    """A project id fixture value, generated so no real project is named."""
    return f"proj-{uuid.uuid4().hex[:10]}"


def _gcp_zone() -> str:
    return "us-east1-b"


def test_scaffold_gcp_writes_the_gcp_contract(tmp_path: Path) -> None:
    """The gcp template carries the README's three parts with the gcp inputs.

    The instance_name and name_prefix inputs carry the SANITIZED name (the
    provenance comment says where it came from), the region is derived from
    the zone with its own comment, and the scaffold issues no command and
    pins no image -- the same issues-nothing contract the azure arm keeps.
    """
    ops = _import_instance_ops()
    instances = _import_instances()
    root = _git_root(tmp_path)
    name = _instance_name("acme")
    project = _gcp_project_id()
    zone = _gcp_zone()
    runner = _FakeRunner()

    result = ops.scaffold(
        root,
        name,
        cloud=instances.ENGINE_KIND_GCP,
        project=project,
        zone=zone,
        runner=runner,
    )

    assert result.path == root / "remote-instances" / name / "terragrunt.hcl"
    assert result.ami is None, "the gcp kind pins no image at scaffold time"
    assert runner.calls == [], "the gcp scaffold issues no command at all"
    text = result.path.read_text(encoding="utf-8")
    assert 'path = find_in_parent_folders("root-gcp.hcl")' in text
    assert (
        'path = "${dirname(find_in_parent_folders("root-gcp.hcl"))}'
        '/_envcommon/remote-gcp.hcl"' in text
    )
    # The sanitized name carries the provenance comment, and the two inputs
    # it feeds agree with the sanitizer's own answer.
    sanitized = instances.sanitize_gcp_name(name)
    assert f'instance_name = "{sanitized}"' in text
    assert f'name_prefix   = "{sanitized}"' in text
    assert "instances.sanitize_gcp_name" in text
    assert f'project_id = "{project}"' in text
    assert f'zone       = "{zone}"' in text
    # The region is derived, commented, and never an input of its own.
    assert 'region = "us-east1"' in text
    assert "Derived from the zone" in text
    assert f'vm_size = "{ops.DEFAULT_GCP_VM_SIZE}"' in text
    assert f"root_volume_size_gb = {ops.DEFAULT_VOLUME_SIZE_GB}" in text
    assert f"data_volume_size_gb = {ops.DEFAULT_VOLUME_SIZE_GB}" in text
    assert 'vpc_cidr   = "10.100.0.0/16"' in text
    assert f'environment = "{name}"' in text, (
        'gcp labels are lowercase-only; the template must not write an'
        ' azure-style capitalized key'
    )
    # Forward-looking: the scaffold writes the declaration without checking
    # the layer or provider it names.
    assert not (root / "remote-instances" / "_envcommon" / "remote-gcp.hcl").exists()
    assert any(f"make instance-deploy INSTANCE={name}" in message for message in result.messages)


def test_scaffold_gcp_sanitizes_an_underscored_name(tmp_path: Path) -> None:
    """A name needing sanitization lands sanitized in the gcp inputs alone.

    The directory, the docker context and the tags carry the instance name
    as it is; only the platform-naming inputs carry the sanitized form.
    """
    ops = _import_instance_ops()
    instances = _import_instances()
    root = _git_root(tmp_path)
    name = "my" + "_" + "engine"
    runner = _FakeRunner()

    result = ops.scaffold(
        root,
        name,
        cloud=instances.ENGINE_KIND_GCP,
        project=_gcp_project_id(),
        zone=_gcp_zone(),
        runner=runner,
    )

    text = result.path.read_text(encoding="utf-8")
    assert f'instance_name = "{instances.sanitize_gcp_name(name)}"' in text
    assert f'environment = "{name}"' in text, (
        'gcp labels are lowercase-only; the template must not write an'
        ' azure-style capitalized key'
    )


def test_scaffold_gcp_refuses_a_name_with_no_legal_form(tmp_path: Path) -> None:
    """A leading digit has no RFC1035 fix; the refusal writes nothing."""
    ops = _import_instance_ops()
    instances = _import_instances()
    root = _git_root(tmp_path)
    runner = _FakeRunner()

    with pytest.raises(ops.ScaffoldError) as exc_info:
        ops.scaffold(
            root,
            "9lives",
            cloud=instances.ENGINE_KIND_GCP,
            project=_gcp_project_id(),
            zone=_gcp_zone(),
            runner=runner,
        )

    assert "invalid gcp resource name" in str(exc_info.value)
    assert not (root / "remote-instances" / "9lives").exists()


def test_scaffold_gcp_vm_size_default_is_the_constant(tmp_path: Path) -> None:
    """The unpassed --vm-size default is the gcp constant, never azure's."""
    ops = _import_instance_ops()
    instances = _import_instances()
    root = _git_root(tmp_path)
    runner = _FakeRunner()

    result = ops.scaffold(
        root,
        _instance_name(),
        cloud=instances.ENGINE_KIND_GCP,
        project=_gcp_project_id(),
        zone=_gcp_zone(),
        runner=runner,
    )

    assert ops.DEFAULT_GCP_VM_SIZE == "e2-standard-2"
    assert f'vm_size = "{ops.DEFAULT_GCP_VM_SIZE}"' in result.path.read_text(encoding="utf-8")


def test_scaffold_gcp_vm_size_override_lands_in_the_template(tmp_path: Path) -> None:
    ops = _import_instance_ops()
    instances = _import_instances()
    root = _git_root(tmp_path)
    runner = _FakeRunner()

    result = ops.scaffold(
        root,
        _instance_name(),
        cloud=instances.ENGINE_KIND_GCP,
        project=_gcp_project_id(),
        zone=_gcp_zone(),
        vm_size="e2-standard-8",
        runner=runner,
    )

    assert 'vm_size = "e2-standard-8"' in result.path.read_text(encoding="utf-8")


def test_scaffold_gcp_requires_project_and_zone(tmp_path: Path) -> None:
    """No default project or zone exists; each refusal names its input."""
    ops = _import_instance_ops()
    instances = _import_instances()
    root = _git_root(tmp_path)
    runner = _FakeRunner()

    with pytest.raises(ops.ScaffoldError) as project_exc:
        ops.scaffold(
            root,
            _instance_name(),
            cloud=instances.ENGINE_KIND_GCP,
            zone=_gcp_zone(),
            runner=runner,
        )
    assert "--project" in str(project_exc.value)

    with pytest.raises(ops.ScaffoldError) as zone_exc:
        ops.scaffold(
            root,
            _instance_name(),
            cloud=instances.ENGINE_KIND_GCP,
            project=_gcp_project_id(),
            runner=runner,
        )
    assert "--zone" in str(zone_exc.value)

    assert runner.calls == []
    assert not (root / "remote-instances").exists() or not any(
        (root / "remote-instances").iterdir()
    ), "a refusal must write nothing"


def test_scaffold_gcp_refuses_the_other_clouds_inputs(tmp_path: Path) -> None:
    """Region, location and AMI are refused under gcp, each naming its way out."""
    ops = _import_instance_ops()
    instances = _import_instances()
    root = _git_root(tmp_path)
    runner = _FakeRunner()

    cases = (
        ({"region": "us-east-1"}, "--zone"),
        ({"location": "westeurope"}, "--zone"),
        ({"ami": "ami-" + uuid.uuid4().hex}, "AMI"),
    )
    for extra, expected in cases:
        with pytest.raises(ops.ScaffoldError) as exc_info:
            ops.scaffold(
                root,
                _instance_name(),
                cloud=instances.ENGINE_KIND_GCP,
                project=_gcp_project_id(),
                zone=_gcp_zone(),
                runner=runner,
                **extra,
            )
        assert expected in str(exc_info.value), f"{extra}: {exc_info.value}"

    assert runner.calls == []


# ---------------------------------------------------------------------------
# state / power / cleanup: the gcp engine kind
# ---------------------------------------------------------------------------


def _write_gcp_scaffold(root: Path, name: str) -> Path:
    """The per-instance terragrunt.hcl the gcp scaffold writes, in miniature.

    The include text is what `read_kind` keys the gcp kind on.
    """
    directory = root / "remote-instances" / name
    directory.mkdir(parents=True, exist_ok=True)
    hcl = directory / "terragrunt.hcl"
    hcl.write_text(
        'include "envcommon" {\n'
        '  path = "${dirname(find_in_parent_folders("root-gcp.hcl"))}'
        '/_envcommon/remote-gcp.hcl"\n'
        "}\n"
        "\n"
        "inputs = {\n"
        f'  project_id = "{_gcp_project_id()}"\n'
        f'  zone       = "us-east1-b"\n'
        "}\n",
        encoding="utf-8",
    )
    return hcl


_GCP_ZONE = "us-east1-b"
_GCP_REGION = "us-east1"


def _write_gcp_engine_address(root: Path, name: str) -> str:
    """The engine address record the deploy writes, from generated values.

    Returns the project id the record carries.
    """
    instances = _import_instances()
    project = _gcp_project_id()
    instances.write_engine_address(
        root,
        name,
        {
            "instance_name": name,
            "zone": _GCP_ZONE,
            "project_id": project,
            "region": _GCP_REGION,
            "secrets_prefix": name,
            "state_bucket": f"gdstate-{name}-deadbeef",
        },
    )
    return project


def _gcp_describe_argv(name: str, project: str) -> tuple[str, ...]:
    transport_gcp = importlib.import_module("devcontainer_config.transport_gcp")
    return (
        "gcloud",
        *transport_gcp.build_instance_status_argv(
            instance_name=name, zone=_GCP_ZONE, project_id=project
        ),
    )


def _gcp_secret_describe_argv(secret_id: str, project: str) -> tuple[str, ...]:
    return (
        "gcloud",
        "secrets",
        "versions",
        "describe",
        "latest",
        "--secret",
        secret_id,
        "--project",
        project,
    )


def test_state_for_a_gcp_engine_probes_the_vm_and_the_six_secrets(tmp_path: Path) -> None:
    ops = _import_instance_ops()
    certs = _import_certs()
    root = _git_root(tmp_path)
    name = _instance_name("gcpinst")
    _write_gcp_scaffold(root, name)
    project = _write_gcp_engine_address(root, name)
    runner = _FakeRunner()
    runner.queue(_ok("RUNNING\n"))
    for secret_id in certs.gcp_instance_secret_ids(name):
        runner.queue(_ok(f"projects/p/secrets/{secret_id}\n"))
    runner.queue(_err("no such context: anything"))

    snapshot = ops.state(root, name, runner=runner)

    assert snapshot.gcp_state == "RUNNING"
    assert snapshot.ec2_state is None and snapshot.azure_state is None
    assert snapshot.recorded_id == name, "the record's VM name is the gcp row's display id"
    assert snapshot.params_present is True
    assert snapshot.certs_present is False
    assert snapshot.context_exists is False
    assert snapshot.lookup_error is None
    assert runner.calls[0][1:4] == ("compute", "instances", "describe")
    assert runner.calls[1:7] == [
        _gcp_secret_describe_argv(secret_id, project)
        for secret_id in certs.gcp_instance_secret_ids(name)
    ]
    assert all(call[0] != "aws" and call[0] != "az" for call in runner.calls), (
        "a gcp engine runs no aws or azure probe at all"
    )


def test_state_gcp_a_missing_secret_answers_params_false(tmp_path: Path) -> None:
    ops = _import_instance_ops()
    root = _git_root(tmp_path)
    name = _instance_name("gcpinst")
    _write_gcp_scaffold(root, name)
    _write_gcp_engine_address(root, name)
    runner = _FakeRunner()
    runner.queue(_ok("RUNNING\n"))
    runner.queue(_ok("present\n"))  # first secret present
    runner.queue(_err("ERROR: ... not found: 404."))  # second missing: enough to answer
    runner.queue(_err("no such context: anything"))

    snapshot = ops.state(root, name, runner=runner)

    assert snapshot.params_present is False
    assert snapshot.lookup_error is None


def test_state_gcp_a_secret_probe_failure_is_a_lookup_error(tmp_path: Path) -> None:
    ops = _import_instance_ops()
    root = _git_root(tmp_path)
    name = _instance_name("gcpinst")
    _write_gcp_scaffold(root, name)
    _write_gcp_engine_address(root, name)
    runner = _FakeRunner()
    runner.queue(_ok("RUNNING\n"))
    runner.queue(_err("ERROR: PERMISSION_DENIED"))
    runner.queue(_err("no such context: anything"))

    snapshot = ops.state(root, name, runner=runner)

    assert snapshot.params_present is None
    assert snapshot.lookup_error is not None
    assert "PERMISSION_DENIED" in snapshot.lookup_error


def test_state_gcp_a_missing_record_degrades_with_the_deploy_remedy(tmp_path: Path) -> None:
    ops = _import_instance_ops()
    root = _git_root(tmp_path)
    name = _instance_name("gcpinst")
    _write_gcp_scaffold(root, name)
    runner = _FakeRunner()
    runner.queue(_err("no such context: anything"))

    snapshot = ops.state(root, name, runner=runner)

    assert snapshot.gcp_state is None
    assert snapshot.params_present is None
    assert snapshot.recorded_id is None
    assert snapshot.lookup_error is not None
    assert "engine-address.json" in snapshot.lookup_error
    assert f"make instance-deploy INSTANCE={name}" in snapshot.lookup_error
    assert all(call[0] != "gcloud" for call in runner.calls), (
        "a row with no record runs no gcloud probe at all"
    )


def test_stop_gcp_stops_and_polls_to_terminated(tmp_path: Path) -> None:
    ops = _import_instance_ops()
    root = _git_root(tmp_path)
    name = _instance_name("gcpinst")
    _write_gcp_scaffold(root, name)
    _write_gcp_engine_address(root, name)
    sleeps: list[float] = []
    runner = _FakeRunner()
    runner.queue(_ok(""))  # the stop call
    runner.queue(_ok("STOPPING\n"))  # the poll: not yet
    runner.queue(_ok("TERMINATED\n"))  # the poll: done

    message = ops.stop(root, name, region=REGION, runner=runner, sleep=sleeps.append)

    assert name in message
    assert "stopped" in message
    assert runner.calls[0][:5] == ("gcloud", "compute", "instances", "stop", name)
    assert "--zone" in runner.calls[0] and "--project" in runner.calls[0]
    assert runner.calls[1][1:4] == ("compute", "instances", "describe")
    assert sleeps == [ops.POWER_POLL_SECONDS], "one not-yet answer paced one poll interval"


def test_stop_gcp_is_idempotent_when_already_terminated(tmp_path: Path) -> None:
    ops = _import_instance_ops()
    root = _git_root(tmp_path)
    name = _instance_name("gcpinst")
    _write_gcp_scaffold(root, name)
    _write_gcp_engine_address(root, name)
    sleeps: list[float] = []
    runner = _FakeRunner()
    runner.queue(_ok(""))  # the stop call
    runner.queue(_ok("TERMINATED\n"))  # the poll: done on the first read

    message = ops.stop(root, name, region=REGION, runner=runner, sleep=sleeps.append)

    assert "stopped" in message
    assert sleeps == [], "the first read answers the target, so no waiting happens"


def test_stop_gcp_timeout_names_the_last_observed_status(tmp_path: Path) -> None:
    ops = _import_instance_ops()
    root = _git_root(tmp_path)
    name = _instance_name("gcpinst")
    _write_gcp_scaffold(root, name)
    _write_gcp_engine_address(root, name)
    runner = _FakeRunner()
    runner.queue(_ok(""))
    for _ in range(ops.POWER_POLL_LIMIT):
        runner.queue(_ok("STOPPING\n"))

    with pytest.raises(ops.PowerError) as exc_info:
        ops.stop(root, name, region=REGION, runner=runner, sleep=lambda _s: None)

    message = str(exc_info.value)
    assert "STOPPING" in message
    assert str(ops.POWER_POLL_LIMIT) in message
    assert name in message


def test_gcp_power_without_a_record_names_the_deploy(tmp_path: Path) -> None:
    ops = _import_instance_ops()
    root = _git_root(tmp_path)
    name = _instance_name("gcpinst")
    _write_gcp_scaffold(root, name)
    runner = _FakeRunner()

    with pytest.raises(ops.PowerError) as exc_info:
        ops.stop(root, name, region=REGION, runner=runner, sleep=lambda _s: None)

    message = str(exc_info.value)
    assert "engine-address.json" in message
    assert f"make instance-deploy INSTANCE={name}" in message
    assert runner.calls == []


def test_start_gcp_waits_for_the_provisioned_marker(tmp_path: Path) -> None:
    ops = _import_instance_ops()
    root = _git_root(tmp_path)
    name = _instance_name("gcpinst")
    _write_gcp_scaffold(root, name)
    _write_gcp_engine_address(root, name)
    sleeps: list[float] = []
    runner = _FakeRunner()
    runner.queue(_ok(""))  # the start call
    runner.queue(_err("not found"))  # the marker read: not yet
    runner.queue(_ok("TRUE\n"))  # the marker read: ready

    message = ops.start(root, name, region=REGION, runner=runner, sleep=sleeps.append)

    assert name in message
    assert "provisioning complete" in message
    assert runner.calls[0][:5] == ("gcloud", "compute", "instances", "start", name)
    assert sleeps == [5.0], "the not-yet marker answer paced one poll interval"
    assert runner.calls[1][5] == "--query-path=general-dev/provisioned", (
        "the start wait gates on the provisioning marker, never on describe status alone"
    )


def test_start_gcp_surfaces_a_failed_readiness_read_as_a_power_error(
    tmp_path: Path,
) -> None:
    """The readiness wait is transport_gcp's (the one home of the poll), and
    an unfixable failure crosses into this module only as `PowerError`."""
    ops = _import_instance_ops()
    root = _git_root(tmp_path)
    name = _instance_name("gcpinst")
    _write_gcp_scaffold(root, name)
    _write_gcp_engine_address(root, name)
    runner = _FakeRunner()
    runner.queue(_ok(""))  # the start call
    runner.queue(_err("Your credentials have expired. Run gcloud auth login"))

    with pytest.raises(ops.PowerError) as exc_info:
        ops.start(root, name, region=REGION, runner=runner, sleep=lambda _s: None)

    assert "gcloud auth login" in str(exc_info.value)


def test_cleanup_gcp_deletes_the_six_secrets_and_tolerates_absent(
    tmp_path: Path,
) -> None:
    ops = _import_instance_ops()
    certs = _import_certs()
    root = _git_root(tmp_path)
    name = _instance_name("gcpinst")
    _write_gcp_scaffold(root, name)
    project = _write_gcp_engine_address(root, name)
    runner = _FakeRunner()
    for index, _secret_id in enumerate(certs.gcp_instance_secret_ids(name)):
        if index % 2 == 0:
            runner.queue(_ok(""))  # deleted
        else:
            runner.queue(_err("ERROR: ... not found: 404."))  # already absent
    runner.queue(_err('context "nope": context not found'))  # the context inspect: absent

    messages = ops.cleanup(root, name, region=REGION, runner=runner)

    assert any("absent; nothing to delete" in message for message in messages)
    assert sum("deleted secret" in message for message in messages) == 3
    assert runner.calls[:6] == [
        ("gcloud", "secrets", "delete", secret_id, "--project", project)
        for secret_id in certs.gcp_instance_secret_ids(name)
    ]
    assert all(call[0] != "aws" for call in runner.calls), (
        "a gcp engine has no SSM surface for cleanup to touch"
    )


def test_cleanup_gcp_still_aggregates_failures(tmp_path: Path) -> None:
    ops = _import_instance_ops()
    certs = _import_certs()
    root = _git_root(tmp_path)
    name = _instance_name("gcpinst")
    _write_gcp_scaffold(root, name)
    _write_gcp_engine_address(root, name)
    runner = _FakeRunner()
    for _ in certs.gcp_instance_secret_ids(name):
        runner.queue(_err("ERROR: PERMISSION_DENIED"))
    runner.queue(_err("docker daemon exploded"))  # the context inspect fails for real

    with pytest.raises(ops.CleanupError) as exc_info:
        ops.cleanup(root, name, region=REGION, runner=runner)

    message = str(exc_info.value)
    assert "PERMISSION_DENIED" in message
    assert "docker daemon exploded" in message


def test_cleanup_gcp_a_missing_record_fails_but_still_removes_the_rest(
    tmp_path: Path,
) -> None:
    ops = _import_instance_ops()
    root = _git_root(tmp_path)
    name = _instance_name("gcpinst")
    _write_gcp_scaffold(root, name)
    runner = _FakeRunner()
    runner.queue(_err('context "nope": context not found'))  # the context inspect: absent

    with pytest.raises(ops.CleanupError) as exc_info:
        ops.cleanup(root, name, region=REGION, runner=runner)

    message = str(exc_info.value)
    assert "engine address record" in message or "secrets_prefix" in message
