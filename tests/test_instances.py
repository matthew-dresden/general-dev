"""Tests for devcontainer_config.instances: discovery, naming, and the

Section 9 addressing table (E8-F1-S1-T1).

The `devcontainer_config.instances` import is deferred into function bodies
(via `_import_instances`), the same convention `tests/test_repo.py` and
`tests/test_certs.py` document: the TDD RED gate stashes this unit's own
production-source files -- `instances.py` is new, added by this task -- and
re-runs a single named test node. A module-level `from devcontainer_config
import instances` would fail COLLECTION for the whole file (pytest exit
code 2, no test outcome recorded) instead of failing the one named test for
the real reason: the module is missing.

`FakeRunner` is imported from `tests/conftest.py` rather than redefined
here, the same shared seam `tests/test_transport.py` and
`tests/test_hostprobe.py` already reuse for `devcontainer_config.hostprobe
.CommandRunner`: no test in this file shells out to a real `docker` binary
(AC-TEST-004), every `forwarded_port` call is driven by a fixed
command-to-result map.

Every fixture repository here is a real, disposable git repository built
under `tmp_path` by shelling out to the actual `git` binary, via
`tests/gitfixtures.py`'s `generated_root`/`init_repo` plus a local
`_init_repo_with_origin` (an `origin` remote is this module's own
`repo.repo_slug` dependency, not a primitive any other consumer of
`gitfixtures.py` needs, so it stays local rather than joining that shared
module -- the same reasoning `tests/test_cli.py`'s own docstring documents
for its local, single-purpose helpers).
"""

from __future__ import annotations

import importlib
import json
import re
import subprocess
import uuid
from pathlib import Path
from types import ModuleType

import pytest
from conftest import FakeRunner
from gitfixtures import generated_root, import_cli, init_repo, run_cli


def _import_instances() -> ModuleType:
    """Import devcontainer_config.instances from inside a function body.

    See the module docstring for why this is not a module-level import.
    """
    return importlib.import_module("devcontainer_config.instances")


def _import_hostprobe() -> ModuleType:
    """Import devcontainer_config.hostprobe from inside a function body.

    `CommandResult` moved to being referenced from here rather than
    re-exported off `instances` once `instances.py` stopped importing it
    for its own use (`_recorded_local_port` now delegates to
    `hostprobe.docker_context_forwarded_port`, E8-F1-S1-T1 round 2); kept
    deferred for the same reason `_import_instances` is.
    """
    return importlib.import_module("devcontainer_config.hostprobe")


# The three name-shaped-not-length rejection cases: path traversal, a path
# separator and the empty string. The over-length case and the exact-boundary
# accept case are each their own dedicated test below (round 2, code_review
# WARN) instead of living in this collection-time list: a parametrize
# decorator's arguments run at module-import time, before `_import_instances`
# has a chance to run, so building the over-length name here would need its
# own copy of `MAX_INSTANCE_NAME_LENGTH` -- a length literal duplicating the
# real module's constant, exactly the drift a bumped bound could silently
# stop exercising.
_BAD_INSTANCE_NAMES: tuple[str, ...] = (
    "../escape",
    "a/b",
    "",
)


def _init_repo_with_origin(root: Path, slug: str) -> None:
    """A disposable git repository at `root` with an `origin` remote basename-ing to `slug`."""
    init_repo(root)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "remote",
            "add",
            "origin",
            f"https://example.invalid/org/{slug}.git",
        ],
        check=True,
        capture_output=True,
    )


def _seed_instance_dir(root: Path, name: str) -> Path:
    """A minimal per-instance directory under `remote-instances/<name>/`, ready for `discover`."""
    instance_dir = root / "remote-instances" / name
    instance_dir.mkdir(parents=True)
    (instance_dir / "terragrunt.hcl").write_text(
        "# seeded by test_instances.py\n", encoding="utf-8"
    )
    return instance_dir


def _context_inspect_command(context_name: str) -> tuple[str, ...]:
    return ("docker", "context", "inspect", context_name, "--format", "{{json .Endpoints}}")


def _tcp_endpoint_result(port: int) -> object:
    """A `CommandResult`-shaped success reporting a `tcp://127.0.0.1:<port>` endpoint."""
    hostprobe = _import_hostprobe()
    payload = json.dumps({"docker": {"Host": f"tcp://127.0.0.1:{port}"}})
    return hostprobe.CommandResult(exit_code=0, stdout=payload)


def _context_not_found_result(context_name: str) -> object:
    hostprobe = _import_hostprobe()
    return hostprobe.CommandResult(
        exit_code=1, stderr=f'context "{context_name}": context not found'
    )


# ---------------------------------------------------------------------------
# validate_name (AC-FUNC-008, AC-TEST-003)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_name", _BAD_INSTANCE_NAMES)
def test_validate_name_rejects_unsafe_names(bad_name: str) -> None:
    instances = _import_instances()
    with pytest.raises(instances.InvalidInstanceNameError, match=re.escape(repr(bad_name))):
        instances.validate_name(bad_name)


def test_validate_name_rejects_an_over_length_name() -> None:
    """The over-length case, built from the real `MAX_INSTANCE_NAME_LENGTH` inside the test
    body rather than a parametrize-decorator literal, so a bumped bound is still exercised
    against its own new value (AC-TEST-003).
    """
    instances = _import_instances()
    over_length_name = "x" * (instances.MAX_INSTANCE_NAME_LENGTH + 1)

    with pytest.raises(instances.InvalidInstanceNameError, match=re.escape(repr(over_length_name))):
        instances.validate_name(over_length_name)


@pytest.mark.parametrize("good_name", ["sandbox", "personal-2", "a"])
def test_validate_name_accepts_safe_names(good_name: str) -> None:
    instances = _import_instances()
    instances.validate_name(good_name)


def test_validate_name_accepts_a_name_at_exactly_the_length_boundary() -> None:
    """The exact `MAX_INSTANCE_NAME_LENGTH`-character boundary, built from the real module's
    own constant inside the test body for the same reason the over-length case is.
    """
    instances = _import_instances()
    boundary_name = "x" * instances.MAX_INSTANCE_NAME_LENGTH

    instances.validate_name(boundary_name)


@pytest.mark.parametrize(
    "bad_name",
    ["../escape", "a/b", ""],
)
def test_every_derivation_rejects_an_invalid_name_before_composing_anything(
    tmp_path: Path, bad_name: str
) -> None:
    """AC-FUNC-008: a rejected name never reaches the filesystem, a docker context or AWS.

    `root` here is deliberately not a git repository at all: if
    `docker_context` reached `repo.repo_slug` before validating `name`, it
    would raise `RepoError` instead of `InvalidInstanceNameError`, so this
    also pins the validate-before-derive ordering, not merely that
    validation happens somewhere.
    """
    instances = _import_instances()
    root = generated_root(tmp_path)

    with pytest.raises(instances.InvalidInstanceNameError):
        instances.terragrunt_dir(root, bad_name)
    with pytest.raises(instances.InvalidInstanceNameError):
        instances.state_key(bad_name)
    with pytest.raises(instances.InvalidInstanceNameError):
        instances.docker_context(root, bad_name)
    with pytest.raises(instances.InvalidInstanceNameError):
        instances.parameter_prefix(bad_name)
    with pytest.raises(instances.InvalidInstanceNameError):
        instances.certs_dir(bad_name)


# ---------------------------------------------------------------------------
# discover (AC-FUNC-004)
# ---------------------------------------------------------------------------


def test_discover_returns_empty_tuple_when_directory_is_absent(tmp_path: Path) -> None:
    instances = _import_instances()
    root = generated_root(tmp_path)

    assert instances.discover(root) == ()


def test_discover_excludes_root_hcl_and_envcommon_and_sorts(tmp_path: Path) -> None:
    instances = _import_instances()
    root = generated_root(tmp_path)
    instances_dir = root / instances.INSTANCES_DIR_NAME
    instances_dir.mkdir()
    (instances_dir / "root.hcl").write_text("", encoding="utf-8")
    (instances_dir / "_envcommon").mkdir()
    _seed_instance_dir(root, "sandbox")
    _seed_instance_dir(root, "personal")

    assert instances.discover(root) == ("personal", "sandbox")


# ---------------------------------------------------------------------------
# The six Section 9 derivations, and AC-9.1's no-collision test
# ---------------------------------------------------------------------------


def test_no_collision_across_two_instance_configurations(tmp_path: Path) -> None:
    """AC-9.1 / AC-TEST-001: every one of the six addressing artifacts differs per instance."""
    instances = _import_instances()
    root = generated_root(tmp_path)
    _init_repo_with_origin(root, "acme-devcontainer")
    _seed_instance_dir(root, "sandbox")
    _seed_instance_dir(root, "personal")

    sandbox_context = instances.docker_context(root, "sandbox")
    personal_context = instances.docker_context(root, "personal")
    runner = FakeRunner(
        {
            _context_inspect_command(sandbox_context): _tcp_endpoint_result(51000),
            _context_inspect_command(personal_context): _tcp_endpoint_result(51001),
        }
    )

    sandbox = {
        "name": "sandbox",
        "terragrunt_dir": instances.terragrunt_dir(root, "sandbox"),
        "state_key": instances.state_key("sandbox"),
        "docker_context": sandbox_context,
        "parameter_prefix": instances.parameter_prefix("sandbox"),
        "certs_dir": instances.certs_dir("sandbox"),
        "forwarded_port": instances.forwarded_port(root, "sandbox", runner),
    }
    personal = {
        "name": "personal",
        "terragrunt_dir": instances.terragrunt_dir(root, "personal"),
        "state_key": instances.state_key("personal"),
        "docker_context": personal_context,
        "parameter_prefix": instances.parameter_prefix("personal"),
        "certs_dir": instances.certs_dir("personal"),
        "forwarded_port": instances.forwarded_port(root, "personal", runner),
    }

    for artifact in sandbox:
        assert sandbox[artifact] != personal[artifact], f"{artifact!r} collided between instances"


def test_terragrunt_dir_is_named_after_the_instance(tmp_path: Path) -> None:
    instances = _import_instances()
    root = generated_root(tmp_path)

    assert instances.terragrunt_dir(root, "sandbox") == root / "remote-instances" / "sandbox"


def test_state_key_matches_spec_section_9(tmp_path: Path) -> None:
    instances = _import_instances()

    assert instances.state_key("sandbox") == "sandbox/terraform.tfstate"


def test_docker_context_is_repo_slug_and_name(tmp_path: Path) -> None:
    instances = _import_instances()
    root = generated_root(tmp_path)
    _init_repo_with_origin(root, "acme-devcontainer")

    assert instances.docker_context(root, "sandbox") == "acme-devcontainer-sandbox"


def test_docker_context_is_general_dev_prefixed_in_this_repository(tmp_path: Path) -> None:
    """Description: 'which is general-dev-<name> in this repository'."""
    instances = _import_instances()
    root = generated_root(tmp_path)
    _init_repo_with_origin(root, "general-dev")

    assert instances.docker_context(root, "sandbox") == "general-dev-sandbox"


def test_docker_context_prefix_is_the_shared_root_docker_context_composes_from(
    tmp_path: Path,
) -> None:
    """`devcontainer_config.transport.instance_from_context_name` delegates to this (AC-FUNC-001,
    AC-FUNC-002): it must be exactly the prefix `docker_context` itself composes with, or the two
    directions of the same mapping could drift apart.
    """
    instances = _import_instances()
    root = generated_root(tmp_path)
    _init_repo_with_origin(root, "acme-devcontainer")

    prefix = instances.docker_context_prefix(root)

    assert prefix == "acme-devcontainer-"
    assert instances.docker_context(root, "sandbox") == f"{prefix}sandbox"


def test_parameter_prefix_matches_spec_section_5_3(tmp_path: Path) -> None:
    instances = _import_instances()

    assert instances.parameter_prefix("sandbox") == "/devcontainer/sandbox/"


def test_certs_dir_defaults_to_home_docker_certs(monkeypatch: pytest.MonkeyPatch) -> None:
    instances = _import_instances()
    monkeypatch.delenv(instances.DOCKER_CONFIG_ENV_VAR, raising=False)

    assert instances.certs_dir("sandbox") == Path.home() / ".docker" / "certs" / "sandbox"


def test_certs_dir_honors_docker_config_when_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instances = _import_instances()
    moved_docker_home = tmp_path / "moved-docker-home"
    monkeypatch.setenv(instances.DOCKER_CONFIG_ENV_VAR, str(moved_docker_home))

    assert instances.certs_dir("sandbox") == moved_docker_home / "certs" / "sandbox"


def test_certs_root_is_the_name_less_root_certs_dir_appends_to(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`devcontainer_config.certs.DEFAULT_CERTS_ROOT` is sourced from this (round 2 BLOCKING 2):
    a single derivation must own the certificate root so an operator who sets `DOCKER_CONFIG`
    never has certificates written under one directory while the addressing table points at
    another.
    """
    instances = _import_instances()
    moved_docker_home = tmp_path / "moved-docker-home"
    monkeypatch.setenv(instances.DOCKER_CONFIG_ENV_VAR, str(moved_docker_home))

    assert instances.certs_root() == moved_docker_home / "certs"
    assert instances.certs_dir("sandbox") == instances.certs_root() / "sandbox"


# ---------------------------------------------------------------------------
# forwarded_port (AC-FUNC-003)
# ---------------------------------------------------------------------------


def test_forwarded_port_reads_the_recorded_allocation(tmp_path: Path) -> None:
    instances = _import_instances()
    root = generated_root(tmp_path)
    _init_repo_with_origin(root, "acme-devcontainer")
    context = instances.docker_context(root, "sandbox")
    runner = FakeRunner({_context_inspect_command(context): _tcp_endpoint_result(54321)})

    assert instances.forwarded_port(root, "sandbox", runner) == 54321


def test_forwarded_port_raises_when_no_context_is_recorded(tmp_path: Path) -> None:
    instances = _import_instances()
    root = generated_root(tmp_path)
    _init_repo_with_origin(root, "acme-devcontainer")
    context = instances.docker_context(root, "sandbox")
    runner = FakeRunner({_context_inspect_command(context): _context_not_found_result(context)})

    with pytest.raises(instances.ForwardedPortNotRecordedError, match=re.escape(context)):
        instances.forwarded_port(root, "sandbox", runner)


def test_forwarded_port_raises_when_docker_is_not_on_path(tmp_path: Path) -> None:
    instances = _import_instances()
    hostprobe = _import_hostprobe()
    root = generated_root(tmp_path)
    _init_repo_with_origin(root, "acme-devcontainer")
    context = instances.docker_context(root, "sandbox")
    runner = FakeRunner(
        {
            _context_inspect_command(context): hostprobe.CommandResult(
                exit_code=127, binary_missing=True
            )
        }
    )

    with pytest.raises(instances.InstancesError, match="docker is not on PATH"):
        instances.forwarded_port(root, "sandbox", runner)


def test_forwarded_port_raises_when_inspect_fails_for_another_reason(tmp_path: Path) -> None:
    instances = _import_instances()
    hostprobe = _import_hostprobe()
    root = generated_root(tmp_path)
    _init_repo_with_origin(root, "acme-devcontainer")
    context = instances.docker_context(root, "sandbox")
    runner = FakeRunner(
        {
            _context_inspect_command(context): hostprobe.CommandResult(
                exit_code=1, stderr="permission denied"
            )
        }
    )

    with pytest.raises(instances.InstancesError, match="permission denied"):
        instances.forwarded_port(root, "sandbox", runner)


def test_forwarded_port_raises_on_unparsable_endpoint_json(tmp_path: Path) -> None:
    instances = _import_instances()
    hostprobe = _import_hostprobe()
    root = generated_root(tmp_path)
    _init_repo_with_origin(root, "acme-devcontainer")
    context = instances.docker_context(root, "sandbox")
    runner = FakeRunner(
        {_context_inspect_command(context): hostprobe.CommandResult(exit_code=0, stdout="not-json")}
    )

    with pytest.raises(instances.InstancesError, match="unparsable"):
        instances.forwarded_port(root, "sandbox", runner)


def test_forwarded_port_raises_on_non_tcp_endpoint(tmp_path: Path) -> None:
    instances = _import_instances()
    hostprobe = _import_hostprobe()
    root = generated_root(tmp_path)
    _init_repo_with_origin(root, "acme-devcontainer")
    context = instances.docker_context(root, "sandbox")
    payload = json.dumps({"docker": {"Host": "unix:///var/run/docker.sock"}})
    runner = FakeRunner(
        {_context_inspect_command(context): hostprobe.CommandResult(exit_code=0, stdout=payload)}
    )

    with pytest.raises(instances.InstancesError, match="non-TCP"):
        instances.forwarded_port(root, "sandbox", runner)


# ---------------------------------------------------------------------------
# recorded_instance_id (the per-instance id store, read side)
# ---------------------------------------------------------------------------


def _import_instance_ops() -> ModuleType:
    """Import devcontainer_config.instance_ops from inside a function body.

    Same deferred-import convention as `_import_instances`; instance_ops is
    the id store's single writer and these tests exercise the read/write
    halves together.
    """
    return importlib.import_module("devcontainer_config.instance_ops")


def test_recorded_instance_id_reads_what_instance_ops_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`instances.recorded_instance_id` is the read half of the store
    `instance_ops.link_id` writes: the same value, from the same file."""
    instances = _import_instances()
    instance_ops = _import_instance_ops()
    monkeypatch.setenv(instances.DOCKER_CONFIG_ENV_VAR, str(tmp_path / "docker-config"))
    root = generated_root(tmp_path)
    instance_id = "i-" + "0123456789abcdef0"

    assert instances.recorded_instance_id(root, "sandbox") is None
    instance_ops.link_id(root, "sandbox", instance_id)

    assert instances.recorded_instance_id(root, "sandbox") == instance_id


def test_recorded_instance_id_returns_none_when_the_store_is_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing store file -- or directory around it -- is the ordinary
    not-yet-linked state: None, never an error."""
    instances = _import_instances()
    monkeypatch.setenv(instances.DOCKER_CONFIG_ENV_VAR, str(tmp_path / "docker-config"))
    root = generated_root(tmp_path)

    assert instances.recorded_instance_id(root, "sandbox") is None


def test_recorded_instance_id_rejects_an_invalid_name(tmp_path: Path) -> None:
    instances = _import_instances()
    root = generated_root(tmp_path)

    with pytest.raises(instances.InvalidInstanceNameError):
        instances.recorded_instance_id(root, "../escape")


def test_instance_id_filename_constant_matches_the_single_writer() -> None:
    """`instances.INSTANCE_ID_FILENAME` restates `instance_ops`'s constant
    because the two modules cannot import each other (instance_ops imports
    instances). This pin is what keeps that restatement honest: a rename on
    the write side fails here instead of silently splitting the store in
    two (see the constant's own comment in instances.py)."""
    instances = _import_instances()
    instance_ops = _import_instance_ops()

    assert instances.INSTANCE_ID_FILENAME == instance_ops.INSTANCE_ID_FILENAME


# ---------------------------------------------------------------------------
# engine_address (the azure relay address record)
# ---------------------------------------------------------------------------


def _address_record() -> dict[str, object]:
    """A relay address record payload, every value generated per call.

    The record's keys are owned by the slice that opens the relay forward;
    the accessor contract under test is "the parsed dict comes back
    verbatim", so the payload's shape is arbitrary here and generated, not
    stored.
    """
    return {
        "endpoint": f"tcp://127.0.0.1:{20000 + uuid.uuid4().int % 20000}",
        "relay_id": uuid.uuid4().hex,
    }


def test_engine_address_reads_what_write_engine_address_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`engine_address` is the read half of the record `write_engine_address`
    writes: the same dict, from the same file."""
    instances = _import_instances()
    monkeypatch.setenv(instances.DOCKER_CONFIG_ENV_VAR, str(tmp_path / "docker-config"))
    root = generated_root(tmp_path)
    record = _address_record()

    with pytest.raises(instances.EngineAddressError):
        instances.engine_address(root, "sandbox")
    instances.write_engine_address(root, "sandbox", record)

    assert instances.engine_address(root, "sandbox") == record


def test_engine_address_missing_names_the_deploy_remedy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing record is the not-yet-deployed state, and the refusal names
    the deploy that writes it."""
    instances = _import_instances()
    monkeypatch.setenv(instances.DOCKER_CONFIG_ENV_VAR, str(tmp_path / "docker-config"))
    root = generated_root(tmp_path)

    with pytest.raises(instances.EngineAddressError) as exc_info:
        instances.engine_address(root, "sandbox")

    message = str(exc_info.value)
    assert "make instance-deploy INSTANCE=sandbox" in message
    assert instances.ENGINE_ADDRESS_FILENAME in message


def test_engine_address_malformed_json_names_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A record that is not valid JSON fails fast naming the file and the
    parse error."""
    instances = _import_instances()
    docker_config = tmp_path / "docker-config"
    monkeypatch.setenv(instances.DOCKER_CONFIG_ENV_VAR, str(docker_config))
    root = generated_root(tmp_path)
    path = instances.engine_address_path("sandbox")
    path.parent.mkdir(parents=True)
    path.write_text("not-json{", encoding="utf-8")

    with pytest.raises(instances.EngineAddressError) as exc_info:
        instances.engine_address(root, "sandbox")

    message = str(exc_info.value)
    assert str(path) in message
    assert "Expecting value" in message


def test_engine_address_rejects_a_non_object_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A record that parses to a non-object is not an address record: the
    refusal names the file and the shape."""
    instances = _import_instances()
    monkeypatch.setenv(instances.DOCKER_CONFIG_ENV_VAR, str(tmp_path / "docker-config"))
    root = generated_root(tmp_path)
    path = instances.engine_address_path("sandbox")
    path.parent.mkdir(parents=True)
    path.write_text('["not", "an", "object"]', encoding="utf-8")

    with pytest.raises(instances.EngineAddressError) as exc_info:
        instances.engine_address(root, "sandbox")

    message = str(exc_info.value)
    assert str(path) in message
    assert "JSON object" in message


def test_engine_address_path_sits_in_the_certs_dir_the_id_store_uses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The record shares the per-instance certificate-material directory, so a
    relocated `DOCKER_CONFIG` moves it together with the id store."""
    instances = _import_instances()
    moved_docker_home = tmp_path / "moved-docker-home"
    monkeypatch.setenv(instances.DOCKER_CONFIG_ENV_VAR, str(moved_docker_home))

    assert instances.engine_address_path("sandbox") == (
        instances.certs_dir("sandbox") / instances.ENGINE_ADDRESS_FILENAME
    )
    assert instances.ENGINE_ADDRESS_FILENAME == "engine-address.json"


def test_write_engine_address_replaces_a_previous_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second write replaces, never accumulates: exactly one record file,
    holding the newest payload."""
    instances = _import_instances()
    monkeypatch.setenv(instances.DOCKER_CONFIG_ENV_VAR, str(tmp_path / "docker-config"))
    root = generated_root(tmp_path)
    first, second = _address_record(), _address_record()
    instances.write_engine_address(root, "sandbox", first)
    instances.write_engine_address(root, "sandbox", second)

    assert instances.engine_address(root, "sandbox") == second
    assert len(list(instances.engine_address_path("sandbox").parent.iterdir())) == 1


def test_write_engine_address_leaves_no_temp_file_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The temp file the atomic write stages is gone once the replace landed."""
    instances = _import_instances()
    monkeypatch.setenv(instances.DOCKER_CONFIG_ENV_VAR, str(tmp_path / "docker-config"))
    root = generated_root(tmp_path)
    instances.write_engine_address(root, "sandbox", _address_record())

    assert [entry.name for entry in instances.engine_address_path("sandbox").parent.iterdir()] == [
        instances.ENGINE_ADDRESS_FILENAME
    ]


def test_clear_engine_address_removes_once_then_reports_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Remove-if-present: 1 when a record was removed (and it is gone), 0 when
    none was recorded."""
    instances = _import_instances()
    monkeypatch.setenv(instances.DOCKER_CONFIG_ENV_VAR, str(tmp_path / "docker-config"))
    root = generated_root(tmp_path)

    assert instances.clear_engine_address(root, "sandbox") == 0
    instances.write_engine_address(root, "sandbox", _address_record())
    assert instances.clear_engine_address(root, "sandbox") == 1
    with pytest.raises(instances.EngineAddressError):
        instances.engine_address(root, "sandbox")
    assert instances.clear_engine_address(root, "sandbox") == 0


def test_engine_address_accessors_reject_an_invalid_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instances = _import_instances()
    monkeypatch.setenv(instances.DOCKER_CONFIG_ENV_VAR, str(tmp_path / "docker-config"))
    root = generated_root(tmp_path)

    for bad_name in ("../escape", "a/b", ""):
        with pytest.raises(instances.InvalidInstanceNameError):
            instances.engine_address(root, bad_name)
        with pytest.raises(instances.InvalidInstanceNameError):
            instances.write_engine_address(root, bad_name, {})
        with pytest.raises(instances.InvalidInstanceNameError):
            instances.clear_engine_address(root, bad_name)


# ---------------------------------------------------------------------------
# resolve (spec Section 4.1.1, AC-FUNC-005, AC-FUNC-006, AC-FUNC-007, AC-TEST-002)
# ---------------------------------------------------------------------------


def _clear_resolution_env(monkeypatch: pytest.MonkeyPatch) -> None:
    instances = _import_instances()
    monkeypatch.delenv(instances.INSTANCE_ENV_VAR, raising=False)
    monkeypatch.delenv(instances.DEFAULT_REMOTE_INSTANCE_ENV_VAR, raising=False)


def test_resolve_instance_arg_wins_over_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instances = _import_instances()
    root = generated_root(tmp_path)
    _seed_instance_dir(root, "sandbox")
    _seed_instance_dir(root, "personal")
    _clear_resolution_env(monkeypatch)
    monkeypatch.setenv(instances.INSTANCE_ENV_VAR, "sandbox")
    monkeypatch.setenv(instances.DEFAULT_REMOTE_INSTANCE_ENV_VAR, "personal")

    resolution = instances.resolve(root, local_backend_active=False)

    assert resolution == instances.Resolution(instance="sandbox", warning=None)


def test_resolve_default_wins_over_a_sole_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instances = _import_instances()
    root = generated_root(tmp_path)
    _seed_instance_dir(root, "sandbox")
    _seed_instance_dir(root, "personal")
    _clear_resolution_env(monkeypatch)
    monkeypatch.setenv(instances.DEFAULT_REMOTE_INSTANCE_ENV_VAR, "personal")

    resolution = instances.resolve(root, local_backend_active=False)

    assert resolution == instances.Resolution(instance="personal", warning=None)


def test_resolve_sole_directory_resolves(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    instances = _import_instances()
    root = generated_root(tmp_path)
    _seed_instance_dir(root, "sandbox")
    _clear_resolution_env(monkeypatch)

    resolution = instances.resolve(root, local_backend_active=False)

    assert resolution == instances.Resolution(instance="sandbox", warning=None)


def test_resolve_ambiguous_names_every_instance_and_both_remedies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instances = _import_instances()
    root = generated_root(tmp_path)
    _seed_instance_dir(root, "sandbox")
    _seed_instance_dir(root, "personal")
    _clear_resolution_env(monkeypatch)

    with pytest.raises(instances.AmbiguousInstanceError) as exc_info:
        instances.resolve(root, local_backend_active=False)

    message = str(exc_info.value)
    assert "sandbox" in message
    assert "personal" in message
    assert f"{instances.INSTANCE_ENV_VAR}=" in message
    assert instances.DEFAULT_REMOTE_INSTANCE_ENV_VAR in message


def test_resolve_unknown_instance_arg_lists_what_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instances = _import_instances()
    root = generated_root(tmp_path)
    _seed_instance_dir(root, "sandbox")
    _clear_resolution_env(monkeypatch)
    monkeypatch.setenv(instances.INSTANCE_ENV_VAR, "ghost")

    with pytest.raises(instances.UnknownInstanceError) as exc_info:
        instances.resolve(root, local_backend_active=False)

    message = str(exc_info.value)
    assert "ghost" in message
    assert "sandbox" in message


def test_resolve_unknown_default_instance_lists_what_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instances = _import_instances()
    root = generated_root(tmp_path)
    _seed_instance_dir(root, "sandbox")
    _clear_resolution_env(monkeypatch)
    monkeypatch.setenv(instances.DEFAULT_REMOTE_INSTANCE_ENV_VAR, "ghost")

    with pytest.raises(instances.UnknownInstanceError, match="ghost"):
        instances.resolve(root, local_backend_active=False)


def test_resolve_raises_invalid_name_error_for_an_unsafe_instance_arg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instances = _import_instances()
    root = generated_root(tmp_path)
    _clear_resolution_env(monkeypatch)
    monkeypatch.setenv(instances.INSTANCE_ENV_VAR, "../escape")

    with pytest.raises(instances.InvalidInstanceNameError):
        instances.resolve(root, local_backend_active=False)


def test_resolve_empty_remote_instances_on_remote_backend_names_setup_remote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instances = _import_instances()
    root = generated_root(tmp_path)
    (root / instances.INSTANCES_DIR_NAME).mkdir()
    _clear_resolution_env(monkeypatch)

    with pytest.raises(instances.NoInstancesConfiguredError, match=r"gd-env-setup-remote"):
        instances.resolve(root, local_backend_active=False)


def test_resolve_no_remote_instances_directory_at_all_names_setup_remote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instances = _import_instances()
    root = generated_root(tmp_path)
    _clear_resolution_env(monkeypatch)

    with pytest.raises(instances.NoInstancesConfiguredError, match=r"gd-env-setup-remote"):
        instances.resolve(root, local_backend_active=False)


def test_resolve_instance_on_local_backend_warns_without_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instances = _import_instances()
    root = generated_root(tmp_path)
    monkeypatch.setenv(instances.INSTANCE_ENV_VAR, "sandbox")

    resolution = instances.resolve(root, local_backend_active=True)

    assert resolution.instance is None
    assert resolution.warning is not None
    assert "sandbox" in resolution.warning


def test_resolve_local_backend_without_instance_has_no_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instances = _import_instances()
    root = generated_root(tmp_path)
    _clear_resolution_env(monkeypatch)

    resolution = instances.resolve(root, local_backend_active=True)

    assert resolution == instances.Resolution(instance=None, warning=None)


# ---------------------------------------------------------------------------
# resolve-instance entry point (AC-FUNC-009, AC-TEST-004)
# ---------------------------------------------------------------------------


def test_resolve_instance_cli_prints_the_address_block_on_stdout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = generated_root(tmp_path)
    _init_repo_with_origin(root, "acme-devcontainer")
    _seed_instance_dir(root, "sandbox")
    instances = _import_instances()
    _clear_resolution_env(monkeypatch)

    exit_code = run_cli(monkeypatch, root, ["resolve-instance"])

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "INSTANCE=sandbox" in out
    assert "TERRAGRUNT_DIR=" in out
    assert "STATE_KEY=sandbox/terraform.tfstate" in out
    assert "DOCKER_CONTEXT=acme-devcontainer-sandbox" in out
    assert "PARAMETER_PREFIX=/devcontainer/sandbox/" in out
    assert "CERTS_DIR=" in out
    assert str(instances.certs_dir("sandbox")) in out


def test_resolve_instance_cli_prints_nothing_on_stdout_and_exits_one_on_ambiguity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = generated_root(tmp_path)
    _init_repo_with_origin(root, "acme-devcontainer")
    _seed_instance_dir(root, "sandbox")
    _seed_instance_dir(root, "personal")
    _clear_resolution_env(monkeypatch)

    exit_code = run_cli(monkeypatch, root, ["resolve-instance"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert "sandbox" in captured.err
    assert "personal" in captured.err


def test_resolve_instance_cli_local_backend_flag_warns_and_prints_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = generated_root(tmp_path)
    _init_repo_with_origin(root, "acme-devcontainer")
    monkeypatch.setenv("INSTANCE", "sandbox")

    exit_code = run_cli(monkeypatch, root, ["resolve-instance", "--local-backend-active"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out == ""
    assert "sandbox" in captured.err


def test_resolve_instance_cli_prints_nothing_and_exits_one_on_no_instances_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = generated_root(tmp_path)
    _init_repo_with_origin(root, "acme-devcontainer")
    _clear_resolution_env(monkeypatch)

    exit_code = run_cli(monkeypatch, root, ["resolve-instance"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert "gd-env-setup-remote" in captured.err


def test_module_help_lists_resolve_instance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Wiring smoke test: `resolve-instance` is a subcommand of `main`."""
    cli = import_cli()
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["--help"])

    assert exc_info.value.code == 0
    assert "resolve-instance" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# engine_vault_name (the azure Key Vault the address record names)
# ---------------------------------------------------------------------------


def test_engine_vault_name_reads_the_record_instances_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`engine_vault_name` is the read accessor on the record
    `write_engine_address` writes: the same vault, from the same file."""
    instances = _import_instances()
    monkeypatch.setenv(instances.DOCKER_CONFIG_ENV_VAR, str(tmp_path / "docker-config"))
    root = generated_root(tmp_path)
    vault_name = f"gd-vault-{uuid.uuid4().hex[:6]}"

    with pytest.raises(instances.EngineAddressError):
        instances.engine_vault_name(root, "sandbox")
    instances.write_engine_address(root, "sandbox", {"vault_name": vault_name})

    assert instances.engine_vault_name(root, "sandbox") == vault_name


def test_engine_vault_name_missing_names_the_deploy_remedy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A record without a usable vault name fails fast naming the redeploy,
    the same remedy a missing record names."""
    instances = _import_instances()
    monkeypatch.setenv(instances.DOCKER_CONFIG_ENV_VAR, str(tmp_path / "docker-config"))
    root = generated_root(tmp_path)
    instances.write_engine_address(
        root, "sandbox", {"relay_namespace": "gd.servicebus.windows.net"}
    )

    with pytest.raises(instances.EngineAddressError) as exc_info:
        instances.engine_vault_name(root, "sandbox")

    message = str(exc_info.value)
    assert instances.VAULT_NAME_KEY in message
    assert "make instance-deploy INSTANCE=sandbox" in message


def test_engine_vault_name_rejects_a_non_string_vault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instances = _import_instances()
    monkeypatch.setenv(instances.DOCKER_CONFIG_ENV_VAR, str(tmp_path / "docker-config"))
    root = generated_root(tmp_path)
    instances.write_engine_address(root, "sandbox", {"vault_name": ["not", "a", "name"]})

    with pytest.raises(instances.EngineAddressError):
        instances.engine_vault_name(root, "sandbox")


# ---------------------------------------------------------------------------
# azure_instance_location (the scaffold's location input)
# ---------------------------------------------------------------------------


def _seed_azure_scaffold(root: Path, name: str, location: str) -> Path:
    """The per-instance terragrunt.hcl the azure scaffold writes, in miniature.

    The include text is what `read_kind` keys the azure kind on, and the
    `location = "..."` line is what the reader under test matches -- the
    scaffold owns the line's exact shape, so the fixture writes that shape.
    """
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
    return hcl


def test_azure_instance_location_reads_the_scaffolded_assignment(
    tmp_path: Path,
) -> None:
    instances = _import_instances()
    location = f"region-{uuid.uuid4().hex[:6]}"
    _seed_azure_scaffold(tmp_path, "sandbox", location)

    assert instances.azure_instance_location(tmp_path, "sandbox") == location


def test_azure_instance_location_missing_scaffold_names_the_init_remedy(
    tmp_path: Path,
) -> None:
    instances = _import_instances()

    with pytest.raises(instances.InstancesError) as exc_info:
        instances.azure_instance_location(tmp_path, "sandbox")

    message = str(exc_info.value)
    assert "no terragrunt.hcl" in message
    assert "make instance-init INSTANCE=sandbox CLOUD=azure LOCATION=" in message


def test_azure_instance_location_missing_assignment_names_the_init_remedy(
    tmp_path: Path,
) -> None:
    instances = _import_instances()
    hcl = _seed_azure_scaffold(tmp_path, "sandbox", "westeurope")
    hcl.write_text(
        hcl.read_text(encoding="utf-8").replace('  location = "westeurope"\n', ""),
        encoding="utf-8",
    )

    with pytest.raises(instances.InstancesError) as exc_info:
        instances.azure_instance_location(tmp_path, "sandbox")

    message = str(exc_info.value)
    assert "carries no location input" in message
    assert "make instance-init INSTANCE=sandbox CLOUD=azure LOCATION=" in message


def test_azure_instance_location_rejects_an_invalid_name(tmp_path: Path) -> None:
    instances = _import_instances()

    with pytest.raises(instances.InvalidInstanceNameError):
        instances.azure_instance_location(tmp_path, "../escape")


# ---------------------------------------------------------------------------
# The gcp engine kind: read_kind's gcp branch, the one name sanitizer, and
# the zone accessor the shell entry points consume.
# ---------------------------------------------------------------------------


def _seed_gcp_scaffold(root: Path, name: str) -> Path:
    """The per-instance terragrunt.hcl the gcp scaffold writes, in miniature.

    The include text is what `read_kind` keys the gcp kind on -- the
    scaffold owns the line's exact shape, so the fixture writes that shape.
    """
    directory = root / "remote-instances" / name
    directory.mkdir(parents=True, exist_ok=True)
    hcl = directory / "terragrunt.hcl"
    hcl.write_text(
        'include "envcommon" {\n'
        '  path = "${dirname(find_in_parent_folders("root-gcp.hcl"))}'
        '/_envcommon/remote-gcp.hcl"\n'
        "}\n",
        encoding="utf-8",
    )
    return hcl


def test_read_kind_gcp_include_declares_the_gcp_kind(tmp_path: Path) -> None:
    instances = _import_instances()
    _seed_gcp_scaffold(tmp_path, "sandbox")

    assert instances.read_kind(tmp_path, "sandbox") == instances.ENGINE_KIND_GCP


def test_read_kind_manifest_still_wins_over_a_gcp_include(tmp_path: Path) -> None:
    """The manifest's ssh declaration outranks everything, gcp included."""
    instances = _import_instances()
    _seed_gcp_scaffold(tmp_path, "sandbox")
    (tmp_path / "remote-instances" / "sandbox" / "engine.json").write_text(
        json.dumps(
            {
                "kind": "ssh",
                "host": "engine.example",
                "user": "operator",
                "port": 22,
                "docker_addr": "/var/run/docker.sock",
            }
        ),
        encoding="utf-8",
    )

    assert instances.read_kind(tmp_path, "sandbox") == instances.ENGINE_KIND_SSH


def test_read_kind_azure_include_wins_over_a_gcp_include(tmp_path: Path) -> None:
    """Both include targets in one file declare the azure kind, never gcp.

    The azure arm of `read_kind` is checked first, so a file an operator
    spliced both includes into cannot silently read as the gcp kind.
    """
    instances = _import_instances()
    directory = tmp_path / "remote-instances" / "sandbox"
    directory.mkdir(parents=True)
    (directory / "terragrunt.hcl").write_text(
        '  path = "${dirname(find_in_parent_folders("root-azure.hcl"))}'
        '/_envcommon/remote-azure.hcl"\n'
        '  path = "${dirname(find_in_parent_folders("root-gcp.hcl"))}'
        '/_envcommon/remote-gcp.hcl"\n',
        encoding="utf-8",
    )

    assert instances.read_kind(tmp_path, "sandbox") == instances.ENGINE_KIND_AZURE


def test_read_kind_a_bare_terragrunt_hcl_is_still_ec2(tmp_path: Path) -> None:
    instances = _import_instances()
    directory = tmp_path / "remote-instances" / "sandbox"
    directory.mkdir(parents=True)
    (directory / "terragrunt.hcl").write_text("inputs = {}\n", encoding="utf-8")

    assert instances.read_kind(tmp_path, "sandbox") == instances.ENGINE_KIND_EC2


@pytest.mark.parametrize(
    ("raw", "sanitized"),
    (
        ("acme", "acme"),
        ("Acme", "acme"),
        ("my_engine", "my-engine"),
        ("Engine.One", "engine-one"),
    ),
)
def test_sanitize_gcp_name_lowercases_and_replaces(raw: str, sanitized: str) -> None:
    instances = _import_instances()

    assert instances.sanitize_gcp_name(raw) == sanitized


@pytest.mark.parametrize(
    "raw",
    (
        "9lives",
        "engine-",
        "-" + "a",
    ),
)
def test_sanitize_gcp_name_refuses_shapes_it_cannot_fix(raw: str) -> None:
    instances = _import_instances()

    with pytest.raises(instances.InvalidGCPNameError) as exc_info:
        instances.sanitize_gcp_name(raw)

    message = str(exc_info.value)
    assert "invalid gcp resource name" in message
    assert "Choose" in message, "the refusal must name the operator's way out"


def test_sanitize_gcp_name_refuses_the_over_length_form_at_the_boundary() -> None:
    instances = _import_instances()
    bound = instances.GCP_NAME_MAX_LENGTH

    assert instances.sanitize_gcp_name("a" * bound) == "a" * bound
    with pytest.raises(instances.InvalidGCPNameError):
        instances.sanitize_gcp_name("a" * (bound + 1))


def test_gcp_zone_reads_what_write_engine_address_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instances = _import_instances()
    monkeypatch.setenv(instances.DOCKER_CONFIG_ENV_VAR, str(tmp_path / "docker-config"))
    instances.write_engine_address(
        tmp_path,
        "sandbox",
        {
            "instance_name": "sandbox",
            "zone": "us-east1-b",
            "project_id": "proj-123456",
            "region": "us-east1",
            "secrets_prefix": "sandbox",
            "state_bucket": "sandbox-state",
        },
    )

    assert instances.gcp_zone(tmp_path, "sandbox") == "us-east1-b"


def test_gcp_zone_missing_record_names_the_deploy_remedy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instances = _import_instances()
    monkeypatch.setenv(instances.DOCKER_CONFIG_ENV_VAR, str(tmp_path / "docker-config"))

    with pytest.raises(instances.EngineAddressError) as exc_info:
        instances.gcp_zone(tmp_path, "sandbox")

    message = str(exc_info.value)
    assert "no engine address record" in message
    assert "make instance-deploy INSTANCE=sandbox" in message


def test_gcp_zone_malformed_value_names_the_redeploy_remedy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instances = _import_instances()
    monkeypatch.setenv(instances.DOCKER_CONFIG_ENV_VAR, str(tmp_path / "docker-config"))
    instances.write_engine_address(tmp_path, "sandbox", {"zone": "US-EAST1-B"})

    with pytest.raises(instances.EngineAddressError) as exc_info:
        instances.gcp_zone(tmp_path, "sandbox")

    message = str(exc_info.value)
    assert "malformed 'zone'" in message
    assert "make instance-deploy INSTANCE=sandbox" in message


def test_resolve_instance_cli_prints_the_gcp_address_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The gcp kind prints exactly the five-key shape the azure kind prints.

    INSTANCE, KIND, TERRAGRUNT_DIR, DOCKER_CONTEXT and CERTS_DIR -- and
    nothing else: the state-key and Parameter Store rows name nothing a
    kind without SSM material addresses, and the export loop consumes every
    KEY=value line generically.
    """
    root = generated_root(tmp_path)
    _init_repo_with_origin(root, "acme-devcontainer")
    _seed_gcp_scaffold(root, "sandbox")
    instances = _import_instances()
    _clear_resolution_env(monkeypatch)
    monkeypatch.setenv(instances.DOCKER_CONFIG_ENV_VAR, str(tmp_path / "docker-config"))

    exit_code = run_cli(monkeypatch, root, ["resolve-instance"])

    out = capsys.readouterr().out
    assert exit_code == 0
    assert out.splitlines() == [
        "INSTANCE=sandbox",
        "KIND=gcp",
        f"TERRAGRUNT_DIR={root / 'remote-instances' / 'sandbox'}",
        "DOCKER_CONTEXT=acme-devcontainer-sandbox",
        f"CERTS_DIR={instances.certs_dir('sandbox')}",
    ]
