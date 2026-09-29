"""`make list-instances` / `cli instance-list` shows every instance's live state.

An operator with more than one instance has no way to see which exist, which
are running, or which carry their trust chain. Every probe that answers that
question lives in `devcontainer_config.instance_ops.list_state` (spec
Section 4.5); this suite pins the CLI surface on top of it: the aligned
table with its summary line, the per-row degradation when one instance's
probes fail, and the `--json` mode scripts consume.

The handler reads `instance_ops.subprocess_runner` from the module at call
time, so these tests substitute a queued fake there -- the same seam
`tests/test_instance_ops.py` uses for the engine itself -- and no test in
this file touches AWS, docker, or the network (AC-TEST-004). Probe order
per instance is the engine's own: the EC2 describe only when an id is
recorded, then describe-parameters, then the docker context inspect.

The honesty of the failure mode is what these tests mostly pin. One
unreachable surface must name itself on stderr and turn the exit non-zero
without suppressing the rows that did answer: a listing that aborted at the
first bad row would hide every healthy instance behind the broken one.
"""

from __future__ import annotations

import importlib
import json
import subprocess
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import ModuleType

import pytest
from gitfixtures import generated_root, init_repo, run_cli


def _import_instance_ops() -> ModuleType:
    return importlib.import_module("devcontainer_config.instance_ops")


def _ok(stdout: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


def _err(stderr: str, *, returncode: int = 1) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout="", stderr=stderr)


class _FakeRunner:
    """A queued Runner double: records every argv, answers from a queue.

    The queue is strict, the same discipline `tests/test_instance_ops.py`'s
    double applies: an invocation with no queued response is a test-authoring
    bug and fails the test rather than being answered with a default.
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


@pytest.fixture(autouse=True)
def _docker_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point DOCKER_CONFIG at a per-test directory.

    The certificate-material directory `list_state` reads lives under
    `instances.certs_dir(name)`, which honors DOCKER_CONFIG; setting it here
    keeps every test's state on `tmp_path` and away from the operator's real
    `~/.docker/certs`. Autouse, the same pattern `tests/test_instance_ops.py`
    applies, so no test can forget it.
    """
    docker_config_dir = tmp_path / "docker-config"
    docker_config_dir.mkdir()
    monkeypatch.setenv("DOCKER_CONFIG", str(docker_config_dir))
    return docker_config_dir


def _repo_root(tmp_path: Path) -> Path:
    """A disposable git repository with an `origin` remote.

    `docker_context` derives its prefix from `repo.repo_slug`, which reads
    `remote.origin.url`, so a bare directory is not enough: the listing would
    fail on the derivation rather than on anything it is testing.
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
            "https://github.com/example/widgets.git",
        ],
        check=True,
        capture_output=True,
    )
    (root / "remote-instances").mkdir(exist_ok=True)
    return root


def _make_instance(root: Path, name: str) -> None:
    """A minimal per-instance deployment directory under `remote-instances/`."""
    directory = root / "remote-instances" / name
    directory.mkdir(parents=True)
    (directory / "terragrunt.hcl").write_text(
        f'inputs = {{\n  instance_name = "{name}"\n}}\n', encoding="utf-8"
    )


def _instance_name(prefix: str) -> str:
    """A valid instance name unique per call."""
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _queue_clean_probes(runner: _FakeRunner, root: Path, names: Sequence[str]) -> None:
    """Queue the no-incident probe answers `list_state` reads, in engine order.

    Per instance: describe-parameters (none published), then the docker
    context inspect (the context does not exist yet, so no forwarded-port
    probe follows it). No EC2 describe is queued because no id is recorded
    unless the test records one itself.
    """
    instance_ops = _import_instance_ops()
    for name in names:
        runner.queue(_ok(json.dumps({"Parameters": []})))
        runner.queue(_err(f"no such context: {instance_ops.instances.docker_context(root, name)}"))


def _run_instance_list(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
    runner: _FakeRunner,
    *args: str,
) -> int:
    """Run `cli instance-list` with `runner` substituted for the production runner."""
    instance_ops = _import_instance_ops()
    monkeypatch.setattr(instance_ops, "subprocess_runner", runner)
    return run_cli(monkeypatch, root, ["instance-list", *args])


def test_no_instances_configured_prints_a_note_and_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repo_root(tmp_path)
    exit_code = _run_instance_list(monkeypatch, root, _FakeRunner())

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out == ""
    assert "No instances configured" in captured.err


def test_rows_follow_discovery_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repo_root(tmp_path)
    alpha = _instance_name("alpha")
    beta = _instance_name("beta")
    _make_instance(root, alpha)
    _make_instance(root, beta)
    runner = _FakeRunner()
    _queue_clean_probes(runner, root, (alpha, beta))

    exit_code = _run_instance_list(monkeypatch, root, runner)

    out = capsys.readouterr().out
    assert exit_code == 0
    assert out.index(alpha) < out.index(beta), "rows must follow instances.discover order"
    assert "2 instance(s) listed" in out.splitlines()[-1]
    assert len(runner.calls) == 4  # params + context inspect, per instance


def test_table_columns_match_the_documented_surface(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """One header row, seven columns per row, unanswered probes as dashes."""
    root = _repo_root(tmp_path)
    alpha = _instance_name("alpha")
    _make_instance(root, alpha)
    runner = _FakeRunner()
    _queue_clean_probes(runner, root, (alpha,))

    exit_code = _run_instance_list(monkeypatch, root, runner)

    out_lines = capsys.readouterr().out.splitlines()
    assert exit_code == 0
    assert out_lines[0].split() == [
        "INSTANCE",
        "STATE",
        "ID",
        "PARAMS",
        "CERTS",
        "FORWARD",
        "CONTEXT",
    ]
    row_columns = out_lines[1].split()
    assert len(row_columns) == 7
    assert row_columns[0] == alpha
    # EC2 state and id do not apply (nothing recorded/linked yet); the params
    # and certs probes answered "nothing there"; no forward is recorded; the
    # context inspect answered "no such context".
    assert row_columns[1:] == ["-", "-", "no", "no", "-", "absent"]


def test_a_recorded_id_shows_the_ec2_state_in_its_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    instance_ops = _import_instance_ops()
    root = _repo_root(tmp_path)
    name = _instance_name("alpha")
    _make_instance(root, name)
    instance_id = "i-" + uuid.uuid4().hex[:17]
    instance_ops.link_id(root, name, instance_id)
    runner = _FakeRunner()
    runner.queue(_ok("running\n"))  # ec2 describe, first because an id is recorded
    _queue_clean_probes(runner, root, (name,))

    exit_code = _run_instance_list(monkeypatch, root, runner)

    out_lines = capsys.readouterr().out.splitlines()
    assert exit_code == 0
    row_columns = out_lines[1].split()
    assert row_columns[1] == "running"
    assert row_columns[2] == instance_id


def test_a_probe_failure_exits_nonzero_but_still_prints_every_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """One unreachable surface names itself; the other rows still print."""
    root = _repo_root(tmp_path)
    alpha = _instance_name("alpha")
    beta = _instance_name("beta")
    _make_instance(root, alpha)
    _make_instance(root, beta)
    runner = _FakeRunner()
    _queue_clean_probes(runner, root, (alpha,))
    runner.queue(_err("simulated SSM outage"))  # beta's describe-parameters
    runner.queue(_err("no such context: anything"))  # beta's context inspect

    exit_code = _run_instance_list(monkeypatch, root, runner)

    captured = capsys.readouterr()
    assert exit_code != 0
    assert alpha in captured.out and beta in captured.out, "no row may be suppressed"
    assert beta in captured.err
    assert "1 with probe errors" in captured.out


def test_json_mode_prints_one_object_per_row_and_no_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repo_root(tmp_path)
    alpha = _instance_name("alpha")
    beta = _instance_name("beta")
    _make_instance(root, alpha)
    _make_instance(root, beta)
    runner = _FakeRunner()
    _queue_clean_probes(runner, root, (alpha, beta))

    exit_code = _run_instance_list(monkeypatch, root, runner, "--json")

    out_lines = capsys.readouterr().out.splitlines()
    assert exit_code == 0
    assert len(out_lines) == 2, "json mode prints exactly one object per row, no summary"
    rows = [json.loads(line) for line in out_lines]
    assert [row["name"] for row in rows] == [alpha, beta]
    assert all(row["params_present"] is False for row in rows)
    assert all(row["certs_present"] is False for row in rows)
    assert all(row["lookup_error"] is None for row in rows)


def test_json_mode_still_exits_nonzero_on_a_probe_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The failure is a field on the object AND the exit code, never hidden."""
    root = _repo_root(tmp_path)
    alpha = _instance_name("alpha")
    _make_instance(root, alpha)
    runner = _FakeRunner()
    runner.queue(_err("simulated SSM outage"))  # alpha's describe-parameters
    runner.queue(_err("no such context: anything"))  # alpha's context inspect

    exit_code = _run_instance_list(monkeypatch, root, runner, "--json")

    captured = capsys.readouterr()
    assert exit_code != 0
    (line,) = captured.out.splitlines()
    row = json.loads(line)
    assert row["params_present"] is None
    assert "simulated SSM outage" in row["lookup_error"]
    assert alpha in captured.err
