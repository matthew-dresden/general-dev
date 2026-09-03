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

    with pytest.raises(instances.NoInstancesConfiguredError, match=r"/devcontainer:setup-remote"):
        instances.resolve(root, local_backend_active=False)


def test_resolve_no_remote_instances_directory_at_all_names_setup_remote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instances = _import_instances()
    root = generated_root(tmp_path)
    _clear_resolution_env(monkeypatch)

    with pytest.raises(instances.NoInstancesConfiguredError, match=r"/devcontainer:setup-remote"):
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
    assert "/devcontainer:setup-remote" in captured.err


def test_module_help_lists_resolve_instance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Wiring smoke test: `resolve-instance` is a subcommand of `main`, not `main_devsecret`."""
    cli = import_cli()
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["--help"])

    assert exc_info.value.code == 0
    assert "resolve-instance" in capsys.readouterr().out
