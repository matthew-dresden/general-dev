"""`make instances` lists what is configured and marks what is active.

An operator with more than one instance has no way to see which exist, which
region each is in, or which one the next `make build` would reach. Every
addressing artifact is derived per instance (spec Section 9), so the answer is
computable; it just was not shown anywhere.

`instances.listing` is a pure function of the repository plus one string, the
active docker context. The probe that discovers that string lives in the CLI,
not here, for two reasons: the listing stays testable without docker present,
and a probe failure degrades to "nothing marked active" rather than taking the
whole listing down with it.

The honesty of the two "unknown" cases is what these tests mostly pin. An
instance whose deployment records no region reports None rather than the
caller's ambient region, because two instances may live in different regions
and printing the current one against both would be confidently wrong. And when
the active context cannot be determined, no row is marked, rather than guessing
that the first row is current.
"""

from __future__ import annotations

import importlib
import subprocess
from pathlib import Path
from types import ModuleType

import pytest


def _import_instances() -> ModuleType:
    return importlib.import_module("devcontainer_config.instances")


def _make_instance(root: Path, name: str, *, region: str | None) -> None:
    """Write a minimal per-instance deployment under `root`."""
    directory = root / "remote-instances" / name
    directory.mkdir(parents=True, exist_ok=True)
    body = 'inputs = {\n  instance_name = "%s"\n' % name
    if region is not None:
        body += '  aws_region = "%s"\n' % region
    body += "}\n"
    (directory / "terragrunt.hcl").write_text(body, encoding="utf-8")


def _repo_root(tmp_path: Path) -> Path:
    """A disposable git repository with an `origin` remote.

    `docker_context` derives its prefix from `repo.repo_slug`, which reads
    `remote.origin.url`, so a bare directory is not enough: the listing would
    fail on the derivation rather than on anything it is testing.
    """
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "remote", "add", "origin",
         "https://github.com/example/widgets.git"],
        check=True,
    )
    (tmp_path / "remote-instances").mkdir(exist_ok=True)
    return tmp_path


def test_no_instances_configured_lists_nothing(tmp_path: Path) -> None:
    instances = _import_instances()
    root = _repo_root(tmp_path)
    assert instances.listing(root, active_context=None) == ()


def test_two_instances_are_listed_in_discovery_order(tmp_path: Path) -> None:
    instances = _import_instances()
    root = _repo_root(tmp_path)
    _make_instance(root, "alpha", region="us-east-1")
    _make_instance(root, "beta", region="eu-west-2")

    rows = instances.listing(root, active_context=None)

    assert [row.name for row in rows] == ["alpha", "beta"]
    assert [row.region for row in rows] == ["us-east-1", "eu-west-2"]


def test_each_row_carries_its_own_region_not_a_shared_one(tmp_path: Path) -> None:
    """Two instances in different regions must not collapse to one value."""
    instances = _import_instances()
    root = _repo_root(tmp_path)
    _make_instance(root, "alpha", region="us-east-1")
    _make_instance(root, "beta", region="ap-southeast-2")

    regions = {row.name: row.region for row in instances.listing(root, active_context=None)}

    assert regions == {"alpha": "us-east-1", "beta": "ap-southeast-2"}


def test_an_instance_recording_no_region_reports_none(tmp_path: Path) -> None:
    """None, not the caller's ambient region, which would be a confident guess."""
    instances = _import_instances()
    root = _repo_root(tmp_path)
    _make_instance(root, "alpha", region=None)

    (row,) = instances.listing(root, active_context=None)

    assert row.region is None


def test_the_active_context_marks_exactly_one_row(tmp_path: Path) -> None:
    instances = _import_instances()
    root = _repo_root(tmp_path)
    _make_instance(root, "alpha", region="us-east-1")
    _make_instance(root, "beta", region="us-east-1")
    active = instances.docker_context(root, "beta")

    rows = instances.listing(root, active_context=active)

    assert [row.active for row in rows] == [False, True]


def test_an_unknown_active_context_marks_no_row(tmp_path: Path) -> None:
    """A context belonging to something else must not mark an unrelated row."""
    instances = _import_instances()
    root = _repo_root(tmp_path)
    _make_instance(root, "alpha", region="us-east-1")

    rows = instances.listing(root, active_context="orbstack")

    assert not any(row.active for row in rows)


def test_a_failed_probe_marks_no_row_rather_than_guessing(tmp_path: Path) -> None:
    """None means "could not tell", and the listing still prints."""
    instances = _import_instances()
    root = _repo_root(tmp_path)
    _make_instance(root, "alpha", region="us-east-1")
    _make_instance(root, "beta", region="us-east-1")

    rows = instances.listing(root, active_context=None)

    assert len(rows) == 2, "a failed probe must not suppress the listing"
    assert not any(row.active for row in rows)


def test_region_of_survives_an_unreadable_deployment(tmp_path: Path) -> None:
    """A malformed or unreadable file yields None, never an exception.

    The listing is a diagnostic. Failing it because one deployment is being
    edited would remove the tool exactly when it is most wanted.
    """
    instances = _import_instances()
    root = _repo_root(tmp_path)
    directory = root / "remote-instances" / "alpha"
    directory.mkdir(parents=True)
    (directory / "terragrunt.hcl").write_bytes(b"\xff\xfe not utf-8 at all")

    assert instances.region_of(root, "alpha") is None


def test_region_of_rejects_a_name_that_escapes_the_directory(tmp_path: Path) -> None:
    """Name validation applies here too; a listing must not read outside the tree."""
    instances = _import_instances()
    root = _repo_root(tmp_path)
    with pytest.raises(instances.InvalidInstanceNameError):
        instances.region_of(root, "../escape")


def test_docker_context_in_each_row_matches_the_derivation(tmp_path: Path) -> None:
    """The row's context is the same derivation every other caller uses."""
    instances = _import_instances()
    root = _repo_root(tmp_path)
    _make_instance(root, "alpha", region="us-east-1")

    (row,) = instances.listing(root, active_context=None)

    assert row.docker_context == instances.docker_context(root, "alpha")
