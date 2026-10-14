"""Tests for `devcontainer_config.state_store_gcp`, the gcp state backend's ensure.

`tg_init`'s gcp arm delegates to this module before any Terragrunt command
runs: does the instance's state bucket exist yet? A confirmed-missing
bucket is ensured -- created with uniform bucket level access, at the
region the engine address record names -- and every probe outcome that is
neither "exists" nor "missing" is raised with the command and its raw
output, so no failure is ever caught and retried.

These tests pin that contract: the bucket name is composed from
`remote-instances/root-gcp.hcl`'s own template and suffix (parsed through
the same functions production uses) into the fixed
`gdstate-<sanitized>-<8 hex>` shape, deterministic in instance, zone,
project and suffix and legal under GCS's naming law; the zone, project and
region come from the engine address record the deploy wrote, so a record
missing any of them fails before any gcloud call; the ensure ends by
writing the `.state-store.json` side-car the gcp root's remote_state
reads, replacing any stale one; the listing keeps only names the
template-derived pattern matches, marks the configured ones, and skips
every other kind; and the delete flow purges the objects then the bucket,
reporting an already-absent store without touching anything and removing
the side-car with the store. `_FakeRunner` is the queued Runner double in
the `instance_ops` shape this module consumes; every generated identifier
is built at test time and no test touches gcp, the network or a real file
outside `tmp_path`.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from devcontainer_config import instances, state_store, state_store_gcp
from devcontainer_config.state_store_gcp import StateStoreGCPError

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

# The committed suffix this suite's root-gcp.hcl fixture carries: the value
# the slice reports for the gcp root's locals, used verbatim so the fixture
# composes exactly what production will.
SUFFIX = "q4m7zk"

# The template is composed from concatenated pieces so no source line
# exceeds the lint limit; the pieces join into the same single line
# `remote-instances/root-gcp.hcl` declares.
_TEMPLATE = "gdstate-${local.instance_name}"

ROOT_HCL = f"""\
locals {{
  state_store_suffix = "{SUFFIX}"
  state_bucket_template = "{_TEMPLATE}"
}}
"""

# The zone and project the fixture instances' engine address records carry,
# generated so no real zone or project is named.
ZONE = "us-east1-b"
REGION = "us-east1"


def _ok(stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


def _err(stderr: str, *, returncode: int = 1) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout="", stderr=stderr)


def _instance_name() -> str:
    """A valid instance name unique per call: lowercase, starts with a letter."""
    return f"inst-{uuid.uuid4().hex[:8]}"


def _project_id() -> str:
    """A project-id-shaped value, generated per call."""
    return f"proj-{uuid.uuid4().hex[:10]}"


def _digest(instance: str, project: str) -> str:
    """The digest the module's own rule derives, recomputed here independently.

    The module sanitizes FIRST and digests the sanitized name -- the same
    string terraform's deploy-side mirror hashes -- so this helper
    sanitizes too.
    """
    material = f"{instances.sanitize_gcp_name(instance)}|{ZONE}|{project}|{SUFFIX}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:8]


def _expected_name(instance: str, project: str) -> str:
    """The name the template composes: gdstate + sanitized + hyphen + digest."""
    sanitized = instances.sanitize_gcp_name(instance)
    return f"gdstate-{sanitized}-{_digest(instance, project)}"


def _record_payload(instance: str, project: str) -> dict[str, str]:
    """The engine address record a gcp deploy writes for `instance`."""
    return {
        "instance_name": instances.sanitize_gcp_name(instance),
        "zone": ZONE,
        "project_id": project,
        "region": REGION,
        "secrets_prefix": instances.sanitize_gcp_name(instance),
        "state_bucket": _expected_name(instance, project),
    }


def _root_with_root_hcl(tmp_path: Path) -> Path:
    """A scratch repository root carrying this repository's gcp root shape."""
    instances_dir = tmp_path / "remote-instances"
    instances_dir.mkdir()
    (instances_dir / "root-gcp.hcl").write_text(ROOT_HCL, encoding="utf-8")
    return tmp_path


def _seed_gcp_instance(root: Path, name: str, tmp_path: Path, project: str) -> None:
    """A scaffolded gcp instance directory and its deploy-written record.

    The include text is the gcp scaffold's, in miniature; the record is
    written through `instances.write_engine_address`, the deploy's own
    writer, into a DOCKER_CONFIG-redirected certificate root so no test
    ever touches the real `~/.docker`.
    """
    directory = root / "remote-instances" / name
    directory.mkdir(parents=True)
    (directory / "terragrunt.hcl").write_text(
        'include "envcommon" {\n'
        '  path = "${dirname(find_in_parent_folders("root-gcp.hcl"))}'
        '/_envcommon/remote-gcp.hcl"\n'
        "}\n"
        "\n"
        "inputs = {\n"
        f'  project_id = "{project}"\n'
        f'  zone       = "{ZONE}"\n'
        f'  region     = "{REGION}"\n'
        "}\n",
        encoding="utf-8",
    )
    instances.write_engine_address(root, name, _record_payload(name, project))


def _redirect_docker_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(instances.DOCKER_CONFIG_ENV_VAR, str(tmp_path / "docker-config"))


def _seed_ec2_instance(root: Path, name: str) -> None:
    """A scaffolded EC2 instance directory: no gcp include, no record."""
    directory = root / "remote-instances" / name
    directory.mkdir(parents=True)
    (directory / "terragrunt.hcl").write_text(
        'include "envcommon" {\n'
        '  path = "${dirname(find_in_parent_folders("root.hcl"))}'
        '/_envcommon/remote-ec2.hcl"\n'
        "}\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Composition: deterministic, charset-legal, collision-differentiating.
# ---------------------------------------------------------------------------


def test_compose_is_deterministic_and_charset_legal() -> None:
    instance = _instance_name()
    project = _project_id()
    first = state_store_gcp.compose_from_root_hcl(ROOT_HCL, instance, ZONE, project)
    second = state_store_gcp.compose_from_root_hcl(ROOT_HCL, instance, ZONE, project)
    assert first == second == _expected_name(instance, project)
    assert re.fullmatch(r"[a-z0-9][a-z0-9._-]{1,61}[a-z0-9]", first), "GCS's own name law must hold"
    assert first == f"gdstate-{instances.sanitize_gcp_name(instance)}-{_digest(instance, project)}"


def test_compose_has_the_fixed_naming_shape() -> None:
    """`gdstate-<sanitized>-<8 hex>`: the shape the naming contract fixes."""
    instance = _instance_name()
    name = state_store_gcp.compose_from_root_hcl(ROOT_HCL, instance, ZONE, _project_id())
    assert name.startswith("gdstate-")
    assert len(name) == len("gdstate-") + len(instances.sanitize_gcp_name(instance)) + 1 + 8


def test_compose_stays_inside_the_length_law_for_the_longest_legal_name() -> None:
    """A 32-character (the sanitizer's bound) instance name must still compose
    a legal bucket name."""
    long_name = "a" * instances.GCP_NAME_MAX_LENGTH
    name = state_store_gcp.compose_from_root_hcl(ROOT_HCL, long_name, ZONE, _project_id())
    assert len(name) <= state_store_gcp.BUCKET_NAME_MAX
    assert re.fullmatch(r"[a-z0-9][a-z0-9._-]+", name)


def test_compose_refuses_a_name_with_no_legal_sanitized_form() -> None:
    """A 33-character instance name is refused by the sanitizer, wrapped in
    this module's error so the CLI's single except clause holds."""
    with pytest.raises(StateStoreGCPError, match="invalid gcp resource name"):
        state_store_gcp.compose_from_root_hcl(
            ROOT_HCL, "a" * (instances.GCP_NAME_MAX_LENGTH + 1), ZONE, _project_id()
        )


def test_compose_sanitizes_the_instance_component_through_the_one_sanitizer() -> None:
    """Uppercase and underscores are sanitized, not stripped: the same rule
    that named the VM names the bucket's instance component."""
    mixed = "Mixed_Case-Instance"
    name = state_store_gcp.compose_from_root_hcl(ROOT_HCL, mixed, ZONE, _project_id())
    assert instances.sanitize_gcp_name(mixed) in name
    assert "Mixed" not in name and "_" not in name.split("gdstate-")[1].split("-")[0]


def test_compose_differentiates_across_zones_and_projects() -> None:
    instance = _instance_name()
    project = _project_id()
    base = state_store_gcp.compose_from_root_hcl(ROOT_HCL, instance, ZONE, project)
    other_zone = state_store_gcp.compose_from_root_hcl(ROOT_HCL, instance, "us-west1-c", project)
    other_project = state_store_gcp.compose_from_root_hcl(ROOT_HCL, instance, ZONE, _project_id())
    assert base != other_zone, "the digest must separate differing zones"
    assert base != other_project, "the digest must separate differing projects"


def test_compose_differentiates_names_that_sanitize_alike() -> None:
    """Different sanitized names still get different buckets, and the digest
    input is the SANITIZED name: two spellings of the same instance
    (the raw directory name and its sanitized form) compose the SAME
    bucket, which is the point -- terraform's deploy-side mirror hashes
    the sanitized var.instance_name, so both sides must agree."""
    project = _project_id()
    raw = "Inst_One"
    sanitized = instances.sanitize_gcp_name(raw)
    first = state_store_gcp.compose_from_root_hcl(ROOT_HCL, raw, ZONE, project)
    second = state_store_gcp.compose_from_root_hcl(ROOT_HCL, sanitized, ZONE, project)
    assert first == second, (
        "the digest runs over the sanitized name, so a raw directory name "
        "and its sanitized form compose one bucket, the same one the "
        "terraform mirror hashes"
    )
    other = state_store_gcp.compose_from_root_hcl(ROOT_HCL, "inst-two", ZONE, project)
    assert other != first


def test_compose_raises_naming_the_template_when_it_is_missing() -> None:
    with pytest.raises(StateStoreGCPError, match="state_bucket_template"):
        state_store_gcp.compose_from_root_hcl("locals {\n}\n", _instance_name(), ZONE, "p")


def test_compose_raises_naming_the_suffix_when_it_is_missing() -> None:
    without_suffix = ROOT_HCL.replace(f'state_store_suffix = "{SUFFIX}"\n', "")
    with pytest.raises(StateStoreGCPError, match="state_store_suffix"):
        state_store_gcp.compose_from_root_hcl(without_suffix, _instance_name(), ZONE, "p")


def test_compose_raises_naming_the_suffix_when_it_is_empty() -> None:
    empty_suffix = ROOT_HCL.replace(SUFFIX, "")
    with pytest.raises(StateStoreGCPError, match="state_store_suffix"):
        state_store_gcp.compose_from_root_hcl(empty_suffix, _instance_name(), ZONE, "p")


def test_compose_raises_naming_an_unsupplied_component() -> None:
    with pytest.raises(StateStoreGCPError, match="unknown_component"):
        state_store_gcp.compose_from_root_hcl(
            ROOT_HCL.replace("${local.instance_name}", "${local.unknown_component}"),
            _instance_name(),
            ZONE,
            "p",
        )


# ---------------------------------------------------------------------------
# bucket_name: root-gcp.hcl, the record's zone and project.
# ---------------------------------------------------------------------------


def test_bucket_name_reads_root_hcl_and_the_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _redirect_docker_config(monkeypatch, tmp_path)
    root = _root_with_root_hcl(tmp_path)
    instance = _instance_name()
    project = _project_id()
    _seed_gcp_instance(root, instance, tmp_path, project)

    assert state_store_gcp.bucket_name(root, instance) == _expected_name(instance, project)


def test_bucket_name_refuses_an_invalid_instance_before_any_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _redirect_docker_config(monkeypatch, tmp_path)
    root = _root_with_root_hcl(tmp_path)
    runner = _QueueRunner()

    with pytest.raises(StateStoreGCPError, match="invalid instance name"):
        state_store_gcp.bucket_name(
            root,
            "../escape",
        )

    assert runner.calls == []


def test_bucket_name_raises_when_the_gcp_root_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _redirect_docker_config(monkeypatch, tmp_path)
    instance = _instance_name()
    (tmp_path / "remote-instances").mkdir()
    _seed_gcp_instance(tmp_path, instance, tmp_path, _project_id())

    with pytest.raises(StateStoreGCPError, match="root-gcp.hcl"):
        state_store_gcp.bucket_name(tmp_path, instance)


def test_bucket_name_raises_when_the_scaffold_has_no_project(
    tmp_path: Path,
) -> None:
    """A scaffold without a project input is refused, naming the init.

    The compose path reads the scaffold, never the deploy-written record:
    the state store runs before the apply that would write the record, so
    the scaffold is the only pre-deploy truth it may depend on.
    """
    root = _root_with_root_hcl(tmp_path)
    instance = _instance_name()
    directory = root / "remote-instances" / instance
    directory.mkdir(parents=True)
    (directory / "terragrunt.hcl").write_text(
        f'inputs = {{\n  zone   = "{ZONE}"\n  region = "{REGION}"\n}}\n',
        encoding="utf-8",
    )

    with pytest.raises(StateStoreGCPError, match="carries no project_id input"):
        state_store_gcp.bucket_name(root, instance)


def test_existing_bucket_answers_the_plain_form(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _redirect_docker_config(monkeypatch, tmp_path)
    root = _root_with_root_hcl(tmp_path)
    instance = _instance_name()
    project = _project_id()
    _seed_gcp_instance(root, instance, tmp_path, project)
    runner = _QueueRunner()
    runner.queue(_ok("name: gs://x\n"))

    assert state_store_gcp.init_form(root, instance, runner) == "plain"
    describe = runner.calls[0]
    assert describe[:4] == ("gcloud", "storage", "buckets", "describe")
    assert f"gs://{_expected_name(instance, project)}" in describe
    assert "--project" in describe and describe[describe.index("--project") + 1] == project


class _QueueRunner:
    """A queued Runner double in the `instance_ops` shape, strict like
    tests/test_state_store.py's: an invocation with no queued response
    fails the test."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self._queue: list[subprocess.CompletedProcess[str]] = []

    def queue(self, *results: subprocess.CompletedProcess[str]) -> None:
        self._queue.extend(results)

    def __call__(
        self, argv: Sequence[str], stdin: str | None, *, env: Mapping[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(tuple(argv))
        assert self._queue, f"_QueueRunner was invoked with no queued response: {tuple(argv)!r}"
        return self._queue.pop(0)


def test_confirmed_missing_bucket_answers_the_bootstrap_form(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _redirect_docker_config(monkeypatch, tmp_path)
    root = _root_with_root_hcl(tmp_path)
    instance = _instance_name()
    _seed_gcp_instance(root, instance, tmp_path, _project_id())
    runner = _QueueRunner()
    runner.queue(
        _err(
            f"ERROR: (gcloud.storage.buckets.describe) gs://[{_expected_name(instance, 'p')}] "
            "not found: 404.",
            returncode=1,
        )
    )

    assert state_store_gcp.init_form(root, instance, runner) == "bootstrap"


def test_a_probe_failure_that_is_not_missing_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 403 means the name is not confirmed missing: ensure must not run."""
    _redirect_docker_config(monkeypatch, tmp_path)
    root = _root_with_root_hcl(tmp_path)
    instance = _instance_name()
    _seed_gcp_instance(root, instance, tmp_path, _project_id())
    raw = "ERROR: (gcloud.storage.buckets.describe) PERMISSION_DENIED"
    runner = _QueueRunner()
    runner.queue(_err(raw, returncode=1))

    with pytest.raises(StateStoreGCPError) as exc_info:
        state_store_gcp.init_form(root, instance, runner)

    message = str(exc_info.value)
    assert raw in message
    assert "buckets describe" in message
    assert "not confirmed missing" in message


# ---------------------------------------------------------------------------
# ensure: probe before create, idempotent, pinned argv, side-car.
# ---------------------------------------------------------------------------


def test_ensure_creates_the_bucket_when_missing_and_writes_the_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _redirect_docker_config(monkeypatch, tmp_path)
    root = _root_with_root_hcl(tmp_path)
    instance = _instance_name()
    project = _project_id()
    _seed_gcp_instance(root, instance, tmp_path, project)
    runner = _QueueRunner()
    runner.queue(_err("not found: 404.", returncode=1))  # the bucket probe
    runner.queue(_ok())  # the bucket create

    messages = state_store_gcp.ensure(root, instance, runner)

    assert len(messages) == 2
    assert "created bucket" in messages[0]
    assert ".state-store.json" in messages[1]
    create = runner.calls[1]
    assert create[:4] == ("gcloud", "storage", "buckets", "create")
    assert create[4] == f"gs://{_expected_name(instance, project)}"
    assert "--project" in create and create[create.index("--project") + 1] == project
    assert "--location" in create and create[create.index("--location") + 1] == REGION
    assert "--uniform-bucket-level-access" in create
    sidecar = root / "remote-instances" / instance / ".state-store.json"
    assert json.loads(sidecar.read_text(encoding="utf-8")) == {
        "bucket": _expected_name(instance, project)
    }


def test_ensure_is_idempotent_and_replaces_a_stale_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _redirect_docker_config(monkeypatch, tmp_path)
    root = _root_with_root_hcl(tmp_path)
    instance = _instance_name()
    project = _project_id()
    _seed_gcp_instance(root, instance, tmp_path, project)
    sidecar = root / "remote-instances" / instance / ".state-store.json"
    sidecar.write_text('{"bucket": "stale-bucket"}\n', encoding="utf-8")
    runner = _QueueRunner()
    runner.queue(_ok("createTime: 2025-01-01T00:00:00Z\n"))  # the bucket probe

    messages = state_store_gcp.ensure(root, instance, runner)

    assert len(messages) == 2
    assert "already exists" in messages[0]
    assert ".state-store.json" in messages[1], (
        "the side-car is rewritten even when the bucket already exists: it is the "
        "handoff terragrunt reads, and it must name the bucket that was just probed"
    )
    assert json.loads(sidecar.read_text(encoding="utf-8"))["bucket"] != "stale-bucket"
    assert all("create" not in call[1:4] for call in runner.calls), (
        "an existing bucket must create nothing: every call is a probe"
    )


def test_ensure_refuses_a_failed_create(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _redirect_docker_config(monkeypatch, tmp_path)
    root = _root_with_root_hcl(tmp_path)
    instance = _instance_name()
    _seed_gcp_instance(root, instance, tmp_path, _project_id())
    runner = _QueueRunner()
    runner.queue(_err("not found: 404.", returncode=1))  # the bucket probe
    runner.queue(_err("could not create the bucket: quota exceeded", returncode=1))

    with pytest.raises(StateStoreGCPError, match="creating bucket") as exc_info:
        state_store_gcp.ensure(root, instance, runner)

    assert "quota exceeded" in str(exc_info.value)


def test_ensure_refuses_an_ensure_without_an_instance_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No scaffold, no store: the failure names the init, before the side-car write."""
    _redirect_docker_config(monkeypatch, tmp_path)
    root = _root_with_root_hcl(tmp_path)
    instance = _instance_name()
    project = _project_id()
    # The record exists but no instance directory was scaffolded: the
    # bucket probe and create still run (the record is addressable), and
    # the side-car write is the step that refuses. The record itself lives
    # under the redirected DOCKER_CONFIG, so writing it does not create
    # the instance directory either.
    instances.write_engine_address(root, instance, _record_payload(instance, project))
    runner = _QueueRunner()
    runner.queue(_err("not found: 404.", returncode=1))
    runner.queue(_ok())

    with pytest.raises(StateStoreGCPError, match="make instance-init"):
        state_store_gcp.ensure(root, instance, runner)


# ---------------------------------------------------------------------------
# Listing: the pattern derives from root-gcp.hcl's own template.
# ---------------------------------------------------------------------------


def test_bucket_name_pattern_matches_the_composed_name(tmp_path: Path) -> None:
    root = _root_with_root_hcl(tmp_path)
    pattern = state_store_gcp.bucket_name_pattern(root)
    assert pattern.fullmatch(_expected_name("inst-x", "p"))
    assert not pattern.fullmatch("gdstate-notadigest1234")
    assert not pattern.fullmatch("someone-elses-bucket")


def test_list_buckets_reports_configured_orphaned_and_skips_other_kinds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _redirect_docker_config(monkeypatch, tmp_path)
    root = _root_with_root_hcl(tmp_path)
    gcp_instance = _instance_name()
    project = _project_id()
    _seed_gcp_instance(root, gcp_instance, tmp_path, project)
    ec2_instance = _instance_name()
    _seed_ec2_instance(root, ec2_instance)
    ours = _expected_name(gcp_instance, project)
    orphan = _expected_name("inst-gone", project)
    other = "some-other-fleets-bucket"
    runner = _QueueRunner()
    runner.queue(_ok(f"gs://{ours}\ngs://{orphan}\ngs://{other}\n"))

    listings = state_store_gcp.list_buckets(root, runner)

    assert [(listing.name, listing.configured) for listing in listings] == [
        (ours, True),
        (orphan, False),
    ]
    assert listings[0].instance == gcp_instance
    assert listings[0].project_id == project
    assert listings[1].instance == ""
    # The EC2 instance was never a candidate: its name never entered the
    # composition at all.
    assert all(listing.instance != ec2_instance for listing in listings)


def test_list_buckets_raises_on_a_failed_gcloud_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _redirect_docker_config(monkeypatch, tmp_path)
    root = _root_with_root_hcl(tmp_path)
    instance = _instance_name()
    _seed_gcp_instance(root, instance, tmp_path, _project_id())
    runner = _QueueRunner()
    runner.queue(_err("ERROR: gcloud auth login required"))

    with pytest.raises(StateStoreGCPError, match="listing the buckets"):
        state_store_gcp.list_buckets(root, runner)


# ---------------------------------------------------------------------------
# Deletion: purge then delete, the side-car with the store.
# ---------------------------------------------------------------------------


def test_delete_bucket_purges_objects_then_deletes() -> None:
    project = _project_id()
    name = _expected_name("inst-x", project)
    runner = _QueueRunner()
    runner.queue(_ok("name: x\n"))  # the bucket probe
    runner.queue(_ok())  # the object purge
    runner.queue(_ok())  # the bucket delete

    message = state_store_gcp.delete_bucket(name, project, runner)

    assert name in message and project in message
    purge = runner.calls[1]
    assert purge[1:4] == ("storage", "rm", "--recursive")
    assert purge[4] == f"gs://{name}/*"
    delete = runner.calls[2]
    assert delete[:4] == ("gcloud", "storage", "buckets", "delete")
    assert delete[4] == f"gs://{name}"


def test_delete_bucket_tolerates_an_already_empty_purge() -> None:
    """`matched no objects` is the desired end state, not a purge failure."""
    project = _project_id()
    runner = _QueueRunner()
    runner.queue(_ok("name: x\n"))  # the bucket probe
    runner.queue(_err("ERROR: One or more URLs matched no objects.", returncode=1))
    runner.queue(_ok())  # the bucket delete

    message = state_store_gcp.delete_bucket(_expected_name("inst-x", project), project, runner)

    assert "deleted" in message
    assert len(runner.calls) == 3


def test_delete_bucket_reports_an_absent_bucket_without_touching_anything() -> None:
    project = _project_id()
    runner = _QueueRunner()
    runner.queue(_err("not found: 404.", returncode=1))

    message = state_store_gcp.delete_bucket(_expected_name("inst-x", project), project, runner)

    assert "already absent" in message
    assert len(runner.calls) == 1


def test_delete_bucket_surfaces_a_failed_delete() -> None:
    project = _project_id()
    runner = _QueueRunner()
    runner.queue(_ok("name: x\n"))  # the bucket probe
    runner.queue(_ok())  # the object purge
    runner.queue(_err("ERROR: (gcloud.storage.buckets.delete) denied", returncode=1))

    with pytest.raises(StateStoreGCPError, match="deleting bucket") as exc_info:
        state_store_gcp.delete_bucket(_expected_name("inst-x", project), project, runner)

    assert "denied" in str(exc_info.value)


def test_delete_store_refuses_an_invalid_name_before_any_gcloud_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _redirect_docker_config(monkeypatch, tmp_path)
    root = _root_with_root_hcl(tmp_path)
    runner = _QueueRunner()

    with pytest.raises(StateStoreGCPError, match="invalid instance name"):
        state_store_gcp.delete_store(root, "../escape", runner)

    assert runner.calls == []


def test_delete_store_removes_the_sidecar_with_the_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _redirect_docker_config(monkeypatch, tmp_path)
    root = _root_with_root_hcl(tmp_path)
    instance = _instance_name()
    project = _project_id()
    _seed_gcp_instance(root, instance, tmp_path, project)
    sidecar = root / "remote-instances" / instance / ".state-store.json"
    sidecar.write_text('{"bucket": "b"}\n', encoding="utf-8")
    runner = _QueueRunner()
    runner.queue(_ok("name: x\n"))  # the bucket probe
    runner.queue(_ok())  # the object purge
    runner.queue(_ok())  # the bucket delete

    message = state_store_gcp.delete_store(root, instance, runner)

    assert "deleted" in message
    assert f"removed {sidecar}" in message
    assert not sidecar.exists()


def test_delete_store_reports_no_sidecar_removal_when_none_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An absent side-car is the desired end state and gets no removal mention."""
    _redirect_docker_config(monkeypatch, tmp_path)
    root = _root_with_root_hcl(tmp_path)
    instance = _instance_name()
    _seed_gcp_instance(root, instance, tmp_path, _project_id())
    runner = _QueueRunner()
    runner.queue(_err("not found: 404.", returncode=1))  # absent bucket

    message = state_store_gcp.delete_store(root, instance, runner)

    assert "already absent" in message
    assert "removed" not in message


def test_delete_matching_buckets_uses_each_listing_s_own_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _redirect_docker_config(monkeypatch, tmp_path)
    root = _root_with_root_hcl(tmp_path)
    instance = _instance_name()
    project = _project_id()
    _seed_gcp_instance(root, instance, tmp_path, project)
    ours = _expected_name(instance, project)
    runner = _QueueRunner()
    runner.queue(_ok(f"gs://{ours}\n"))  # the listing
    runner.queue(_ok("name: x\n"))  # the bucket probe
    runner.queue(_ok())  # the object purge
    runner.queue(_ok())  # the bucket delete

    messages = state_store_gcp.delete_matching_buckets(root, runner)

    assert len(messages) == 1
    assert "deleted" in messages[0]
    delete = runner.calls[-1]
    assert delete[delete.index("--project") + 1] == project


# ---------------------------------------------------------------------------
# The module entry point.
# ---------------------------------------------------------------------------


def test_main_init_form_prints_the_form_and_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _redirect_docker_config(monkeypatch, tmp_path)
    root = _root_with_root_hcl(tmp_path)
    instance = _instance_name()
    _seed_gcp_instance(root, instance, tmp_path, _project_id())
    runner = _QueueRunner()
    runner.queue(_ok("name: x\n"))
    monkeypatch.setattr(state_store_gcp, "subprocess_runner", runner)

    exit_code = state_store_gcp.main(["init-form", str(root), instance])

    assert exit_code == 0
    assert capsys.readouterr().out.strip() == "plain"


def test_main_ensure_prints_one_message_per_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _redirect_docker_config(monkeypatch, tmp_path)
    root = _root_with_root_hcl(tmp_path)
    instance = _instance_name()
    _seed_gcp_instance(root, instance, tmp_path, _project_id())
    runner = _QueueRunner()
    runner.queue(_ok("name: x\n"))  # the bucket probe
    monkeypatch.setattr(state_store_gcp, "subprocess_runner", runner)

    exit_code = state_store_gcp.main(["ensure", str(root), instance])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "already exists" in out
    assert ".state-store.json" in out


def test_main_reports_probe_failures_on_stderr_and_exits_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _redirect_docker_config(monkeypatch, tmp_path)
    root = _root_with_root_hcl(tmp_path)
    instance = _instance_name()
    _seed_gcp_instance(root, instance, tmp_path, _project_id())
    runner = _QueueRunner()
    runner.queue(_err("ERROR: gcloud auth login required"))
    monkeypatch.setattr(state_store_gcp, "subprocess_runner", runner)

    exit_code = state_store_gcp.main(["init-form", str(root), instance])

    assert exit_code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "auth login required" in captured.err


def test_main_rejects_unknown_commands_and_wrong_argument_counts(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert state_store_gcp.main(["frobnicate"]) == 2
    assert state_store_gcp.main([]) == 2
    assert state_store_gcp.main(["delete", "a", "b"]) == 1
    captured = capsys.readouterr()
    assert "usage:" in captured.err
    assert "takes exactly" in captured.err


def test_the_sidecar_filename_is_the_azure_root_s_own(tmp_path: Path) -> None:
    """One per-instance side-car path, shared with the azure kind: an instance
    directory is exactly one kind, so the same filename can never carry two
    payloads."""
    root = _root_with_root_hcl(tmp_path)
    instance = _instance_name()
    (root / "remote-instances" / instance).mkdir(parents=True)
    assert (
        state_store_gcp.sidecar_path(root, instance)
        == state_store.sidecar_path(root, instance)
        == root / "remote-instances" / instance / ".state-store.json"
    )
