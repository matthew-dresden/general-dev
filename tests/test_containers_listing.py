"""`make list-containers` / `cli container-list` shows the project's containers on every engine.

An operator with containers on the local engine and on remote instances has
no way to see them in one place: `make status` reports the active backend's
only. Every probe that answers the fleet question lives in
`devcontainer_config.containers.list_containers`; this suite pins the CLI
surface on top of it: the aligned table with its summary line, the
per-engine degradation when one engine cannot answer, the project-label
filter that keeps the listing scoped to this checkout's containers, and the
`--json` mode scripts consume.

The handler reads `containers.subprocess_runner` from the module at call
time, so these tests substitute a queued fake there -- the same seam
`tests/test_instances_listing.py` uses for the instance fleet -- and no
test in this file touches docker, AWS, or the network (AC-TEST-004). Probe
order is the engine's own: the local engine first, then every configured
instance in discovery order.

The honesty of the failure mode is what these tests mostly pin. One
unreachable engine must name itself on stderr and turn the exit non-zero
without suppressing the rows that did answer: a listing that aborted at the
first bad engine would hide every healthy container behind the broken one.
"""

from __future__ import annotations

import importlib
import json
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import ModuleType

import pytest
from gitfixtures import generated_root, init_repo, run_cli


def _import_containers() -> ModuleType:
    return importlib.import_module("devcontainer_config.containers")


def _import_instances() -> ModuleType:
    return importlib.import_module("devcontainer_config.instances")


def _ok(stdout: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


def _err(stderr: str, *, returncode: int = 1) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout="", stderr=stderr)


class _FakeRunner:
    """A queued Runner double: records every argv, answers from a queue.

    The queue is strict, the same discipline `tests/test_instances_listing.py`'s
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


def _repo_root(tmp_path: Path) -> Path:
    """A disposable git repository with an `origin` remote.

    `docker_context` derives its prefix from `repo.repo_slug`, which reads
    `remote.origin.url`, so a bare directory is not enough: the context
    derivation would fail rather than anything under test.
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


_LOCAL_CONTEXT = "orbstack-test"


def _ps_line(name: str, state: str, image: str, created: str) -> str:
    return f"{name}\t{state}\t{image}\t{created}"


def _run_container_list(
    monkeypatch: pytest.MonkeyPatch, root: Path, runner: _FakeRunner, args: list[str] | None = None
) -> int:
    """Run `cli container-list` with `runner` substituted; the exit code.

    Output is read through pytest's capsys in each test, the same way
    `tests/test_instances_listing.py` reads the instance listing's.
    """
    containers = _import_containers()
    monkeypatch.setattr(containers, "subprocess_runner", runner)
    return run_cli(
        monkeypatch, root, ["container-list", "--local-context", _LOCAL_CONTEXT, *(args or [])]
    )


def test_local_only_repo_lists_one_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A repository with no configured instances sweeps the local engine alone."""
    root = _repo_root(tmp_path)
    runner = _FakeRunner()
    runner.queue(
        _ok(
            _ps_line("widgets-main", "running", "vsc-widgets-abc123", "2 hours ago")
            + "\n"
            + _ps_line("widgets-review", "exited", "vsc-widgets-abc123", "3 days ago")
        )
    )
    code = _run_container_list(monkeypatch, root, runner)
    out = capsys.readouterr().out
    assert code == 0, "a sweep where every engine answered exits zero"
    assert "widgets-main" in out and "widgets-review" in out
    assert "local" in out
    assert "2 container(s) listed across 1 engine(s)" in out
    assert len(runner.calls) == 1, "no instances configured means exactly one probe"


def test_rows_follow_engine_order_local_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The local engine leads, then every instance in discovery order."""
    root = _repo_root(tmp_path)
    alpha, beta = "alpha", "beta"
    _make_instance(root, alpha)
    _make_instance(root, beta)
    runner = _FakeRunner()
    runner.queue(_ok(_ps_line("widgets-local", "running", "img", "1 hour ago")))
    runner.queue(_ok(_ps_line("widgets-alpha", "exited", "img", "1 day ago")))
    runner.queue(_err("cannot connect to the Docker daemon at tcp://127.0.0.1:1"))
    code = _run_container_list(monkeypatch, root, runner)
    out = capsys.readouterr().out
    assert code == 1, "beta's queued failure makes the sweep's exit non-zero"
    assert out.index("widgets-local") < out.index("widgets-alpha")
    assert len(runner.calls) == 3, "one probe per engine: local, alpha, beta"


def test_one_unreachable_engine_degrades_per_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An engine that cannot answer names itself on stderr, exits 1, hides nothing."""
    root = _repo_root(tmp_path)
    _make_instance(root, "alpha")
    runner = _FakeRunner()
    runner.queue(_ok(_ps_line("widgets-local", "running", "img", "1 hour ago")))
    runner.queue(_err("error during connect: the forward is not open"))
    code = _run_container_list(monkeypatch, root, runner)
    captured = capsys.readouterr()
    out, err = captured.out, captured.err
    assert code == 1, "a sweep where any engine failed exits non-zero"
    assert "widgets-local" in out, "the engine that answered still prints"
    assert "unreachable" in out, "the failed engine renders as a row, not an abort"
    assert "1 with probe errors" in out
    assert "ERROR: alpha" in err, "the failure names the engine on stderr"
    assert "error during connect" in err


def test_an_engine_with_no_containers_reports_a_none_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An engine that answered with nothing shows a none row, distinct from unreachable."""
    root = _repo_root(tmp_path)
    runner = _FakeRunner()
    runner.queue(_ok(""))
    code = _run_container_list(monkeypatch, root, runner)
    out = capsys.readouterr().out
    assert code == 0
    assert "none" in out
    assert "0 container(s) listed across 1 engine(s)" in out


def test_the_label_filter_names_this_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The filter is this checkout's name, so any fork lists its own containers."""
    root = _repo_root(tmp_path)
    runner = _FakeRunner()
    runner.queue(_ok(""))
    _run_container_list(monkeypatch, root, runner)
    argv = " ".join(runner.calls[0])
    assert f"label=devcontainer.project={root.name}" in argv, (
        f"the probe must filter on this repo's label; got {argv!r}"
    )


def test_json_mode_prints_one_object_per_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """--json keeps stdout to one single-line JSON object per engine row."""
    root = _repo_root(tmp_path)
    _make_instance(root, "alpha")
    runner = _FakeRunner()
    runner.queue(_ok(_ps_line("widgets-local", "running", "img", "1 hour ago")))
    runner.queue(_err("down"))
    code = _run_container_list(monkeypatch, root, runner, args=["--json"])
    out = capsys.readouterr().out
    assert code == 1
    lines = [line for line in out.splitlines() if line.strip()]
    assert len(lines) == 2, f"one object per engine row; got {lines!r}"
    local = json.loads(lines[0])
    assert local["engine"] == "local" and local["context"] == _LOCAL_CONTEXT
    assert [c["name"] for c in local["containers"]] == ["widgets-local"]
    unreachable = json.loads(lines[1])
    assert unreachable["engine"] == "alpha"
    assert unreachable["containers"] is None
    assert unreachable["lookup_error"] == "down"


def test_a_malformed_docker_line_is_an_error_not_a_guess() -> None:
    """A line the format template does not shape raises, never invents a container."""
    containers = _import_containers()
    with pytest.raises(ValueError, match="does not shape"):
        containers._parse_ps_output("only-one-field")


def test_an_empty_local_context_is_refused_with_the_config_remedy(
    tmp_path: Path,
) -> None:
    """An empty local context never reaches docker, which would target the active one."""
    containers = _import_containers()
    with pytest.raises(ValueError, match="LOCAL_DOCKER_CONTEXT"):
        containers.list_containers(tmp_path, "", runner=_FakeRunner())


def test_instance_contexts_come_from_the_single_derivation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each remote probe targets instances.docker_context's name for that instance."""
    root = _repo_root(tmp_path)
    instances = _import_instances()
    _make_instance(root, "alpha")
    runner = _FakeRunner()
    runner.queue(_ok(""))
    runner.queue(_ok(""))
    _run_container_list(monkeypatch, root, runner)
    expected = instances.docker_context(root, "alpha")
    assert any(expected in " ".join(call) for call in runner.calls[1:]), (
        f"the alpha probe must target the derived context {expected!r}; got {runner.calls!r}"
    )
