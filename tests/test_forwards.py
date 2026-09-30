"""Tests for `devcontainer_config.forwards`, the detached forward lifecycle.

`make connect` opens the SSM forward as a daemon and returns once the
transport announces readiness; `status`, `stop`, `refresh` and `list` manage
the daemon from its per-instance record. These tests pin that contract
hermetically: a fake spawner launches a stand-in child that writes whatever
the test queues to the log, aliveness and signal delivery are injected so
no real process is ever probed or signalled, and the port probe is a
callable the test owns. The transport's readiness marker -- the line it
prints only after the docker context answered a handshake through the
tunnel -- is the one confirmation the open path accepts, and the tests pin
both its acceptance and every failure around it: a child that dies first,
a poll limit exhausted (with the child stopped, never orphaned), a stop
whose port refuses to close, and a record that cannot be parsed.
"""

from __future__ import annotations

import json
import os
import signal
from pathlib import Path

import pytest
from devcontainer_config import forwards, instances
from devcontainer_config.forwards import ForwardError, ForwardRecord
from devcontainer_config.hostprobe import CommandResult

READINESS_LINE = f"{forwards.READINESS_MARKER} docker context 'ctx' -> i-abc over SSM.\n"


@pytest.fixture(autouse=True)
def _docker_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point DOCKER_CONFIG at a per-test directory.

    The forward record and log live under `instances.certs_dir`, which
    honors DOCKER_CONFIG; setting it here keeps every test's state on
    `tmp_path` and away from the operator's real `~/.docker/certs` (the
    same guard `tests/test_instance_ops.py` installs).
    """
    docker_config = tmp_path / "docker-config"
    docker_config.mkdir()
    monkeypatch.setenv("DOCKER_CONFIG", str(docker_config))
    return docker_config


class _FakePopen:
    """A child handle stand-in: pid, poll(), terminate(), kill(), wait()."""

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0

    def kill(self) -> None:
        self.killed = True
        self.returncode = 0

    def wait(self, timeout: float | None = None) -> int:
        return 0


class _Spawner:
    """A spawner double standing in for a healthy child unless told otherwise.

    By default it writes the transport's readiness announcement to the log
    it was handed -- what a real, successful child does -- so the tests that
    exercise the happy path need no log plumbing. Tests that need a silent
    or dying child pass `announce=False` and write (or skip) the log
    themselves.
    """

    def __init__(self, pid: int = 4242, *, announce: bool = True) -> None:
        self.pid = pid
        self.announce = announce
        self.argv: tuple[str, ...] | None = None
        self.log_path: Path | None = None
        self.process = _FakePopen(pid)

    def __call__(self, argv: object, log_path: Path) -> _FakePopen:
        assert isinstance(argv, (list, tuple))
        self.argv = tuple(argv)
        self.log_path = log_path
        if self.announce:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text(READINESS_LINE, encoding="utf-8")
        return self.process


def _child_argv() -> tuple[str, ...]:
    return forwards.child_argv("i-abc", "general-dev-x", "profile", "us-east-1")


def _git_root(tmp_path: Path) -> Path:
    """A scratch git checkout whose origin slug names the general-dev contexts."""
    import subprocess

    from gitfixtures import generated_root, init_repo

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
    return root


def _record(tmp_path: Path, instance: str, *, pid: int = 4242, port: int = 50000) -> None:
    path = forwards.record_path(instance)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = ForwardRecord(
        pid=pid,
        instance=instance,
        instance_id="i-abc",
        context="general-dev-x",
        port=port,
        argv=_child_argv(),
        log=str(forwards.log_path(instance)),
        started_at="2026-09-30T12:00:00+00:00",
    )
    path.write_text(record.to_json(), encoding="utf-8")


# ---------------------------------------------------------------------------
# open: spawn, confirm readiness, record; idempotent when already open.
# ---------------------------------------------------------------------------


def test_open_spawns_the_transport_confirms_and_records(tmp_path: Path) -> None:
    tmp_path = _git_root(tmp_path)
    spawner = _Spawner(pid=777)
    port_probe_calls: list[str] = []

    def runner(command: object, timeout_seconds: float | None) -> CommandResult:
        port_probe_calls.append(" ".join(str(part) for part in command))  # type: ignore[arg-type]
        return CommandResult(
            exit_code=0, stdout=json.dumps({"docker": {"Host": "tcp://127.0.0.1:51368"}})
        )

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(forwards, "_context_port_runner", runner)
        message = forwards.open_forward(
            "i-abc",
            "general-dev-x",
            "profile",
            "us-east-1",
            tmp_path,
            spawner=spawner,
            poll_clock=lambda _seconds: None,
        )

    assert "pid 777" in message and "51368" in message
    assert spawner.argv is not None and spawner.argv[1:4] == (
        "-m",
        "devcontainer_config.transport",
        "connect",
    )
    assert "--instance-id" in spawner.argv and "i-abc" in spawner.argv
    assert "--profile" in spawner.argv and "--region" in spawner.argv
    assert any("docker" in call and "context" in call for call in port_probe_calls)
    record = forwards.read_record("x")
    assert record is not None and record.pid == 777 and record.port == 51368
    assert record.argv == spawner.argv


def test_open_polls_until_the_readiness_marker_arrives(tmp_path: Path) -> None:
    tmp_path = _git_root(tmp_path)
    spawner = _Spawner(announce=False)
    polls: list[float] = []

    def runner(_command: object, _timeout: float | None) -> CommandResult:
        return CommandResult(
            exit_code=0, stdout=json.dumps({"docker": {"Host": "tcp://127.0.0.1:51368"}})
        )

    def write_marker_later(_seconds: float) -> None:
        polls.append(_seconds)
        log = spawner.log_path
        assert log is not None
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(READINESS_LINE, encoding="utf-8")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(forwards, "_context_port_runner", runner)
        message = forwards.open_forward(
            "i-abc",
            "general-dev-x",
            "profile",
            "us-east-1",
            tmp_path,
            spawner=spawner,
            poll_clock=write_marker_later,
        )

    assert "is open" in message
    assert polls, "the open path must poll, never return before the marker"


def test_open_is_idempotent_when_the_daemon_is_alive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tmp_path = _git_root(tmp_path)
    _record(tmp_path, "x", pid=os.getpid(), port=51368)
    spawner = _Spawner()

    message = forwards.open_forward(
        "i-abc",
        "general-dev-x",
        "profile",
        "us-east-1",
        tmp_path,
        spawner=spawner,
        poll_clock=lambda _seconds: None,
    )

    assert "already open" in message
    assert spawner.argv is None, "a live forward must never be spawned over"


def test_open_replaces_a_stale_record_and_says_so(tmp_path: Path) -> None:
    tmp_path = _git_root(tmp_path)
    _record(tmp_path, "x", pid=999999999, port=51368)
    spawner = _Spawner()

    def runner(_command: object, _timeout: float | None) -> CommandResult:
        return CommandResult(
            exit_code=0, stdout=json.dumps({"docker": {"Host": "tcp://127.0.0.1:51368"}})
        )

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(forwards, "_context_port_runner", runner)
        message = forwards.open_forward(
            "i-abc",
            "general-dev-x",
            "profile",
            "us-east-1",
            tmp_path,
            spawner=spawner,
            poll_clock=lambda _seconds: None,
        )

    assert "stale record" in message
    assert "pid 4242" in message


def test_open_raises_with_the_log_tail_when_the_child_dies_first(tmp_path: Path) -> None:
    tmp_path = _git_root(tmp_path)
    log = tmp_path / "log-will-be-created-by-spawner"

    def fail_fast(_argv: object, log_path: Path) -> _FakePopen:
        nonlocal log
        log = log_path
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("ERROR: expired SSO session\n", encoding="utf-8")
        child = _Spawner().process
        child.returncode = 1
        return child

    with pytest.raises(ForwardError) as exc_info:
        forwards.open_forward(
            "i-abc",
            "general-dev-x",
            "profile",
            "us-east-1",
            tmp_path,
            spawner=fail_fast,
            poll_clock=lambda _seconds: None,
        )

    assert "exited before announcing readiness" in str(exc_info.value)
    assert "expired SSO session" in str(exc_info.value)


def test_open_stops_the_child_and_raises_when_the_limit_is_exhausted(tmp_path: Path) -> None:
    tmp_path = _git_root(tmp_path)
    spawner = _Spawner(announce=False)

    with pytest.raises(ForwardError, match="announced no readiness"):
        forwards.open_forward(
            "i-abc",
            "general-dev-x",
            "profile",
            "us-east-1",
            tmp_path,
            spawner=spawner,
            poll_clock=lambda _seconds: None,
            poll_limit=2,
        )

    assert spawner.process.terminated, "a hung spawn must be stopped, never orphaned"


def test_open_raises_when_the_forwarded_port_cannot_be_read(tmp_path: Path) -> None:
    tmp_path = _git_root(tmp_path)
    spawner = _Spawner()

    def broken_port_reader(*_args: object) -> int:
        raise instances.InstancesError("docker context 'general-dev-x' not found")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(forwards.instances, "forwarded_port", broken_port_reader)
        with pytest.raises(ForwardError, match="forwarded port"):
            forwards.open_forward(
                "i-abc",
                "general-dev-x",
                "profile",
                "us-east-1",
                tmp_path,
                spawner=spawner,
                poll_clock=lambda _seconds: None,
            )


# ---------------------------------------------------------------------------
# status and list.
# ---------------------------------------------------------------------------


def test_status_reports_no_forward_without_a_record(tmp_path: Path) -> None:
    status = forwards.status_forward("x")
    assert status.record is None and not status.listening


def test_status_reports_listening_only_when_process_and_port_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _record(tmp_path, "x", pid=os.getpid(), port=51368)
    monkeypatch.setattr(forwards, "port_listening", lambda _port: True)
    assert forwards.status_forward("x").listening

    monkeypatch.setattr(forwards, "port_listening", lambda _port: False)
    status = forwards.status_forward("x")
    assert status.process_alive and not status.listening


def test_status_reports_a_stale_record_when_the_process_is_gone(tmp_path: Path) -> None:
    _record(tmp_path, "x", pid=999999999, port=51368)
    status = forwards.status_forward("x")
    assert not status.process_alive and not status.listening
    assert status.port_listening is None, "the port is not probed for a dead daemon"


def test_list_covers_every_discovered_instance_in_discover_order(tmp_path: Path) -> None:
    (tmp_path / "remote-instances" / "beta").mkdir(parents=True)
    (tmp_path / "remote-instances" / "alpha").mkdir(parents=True)
    statuses = forwards.list_forwards(tmp_path)
    assert [status.instance for status in statuses] == sorted(["beta", "alpha"])


# ---------------------------------------------------------------------------
# stop: SIGINT first, escalation, port-closed verification, record removal.
# ---------------------------------------------------------------------------


def test_stop_sends_sigint_first_then_verifies_the_port_closed(tmp_path: Path) -> None:
    _record(tmp_path, "x", pid=4242, port=51368)
    sent: list[tuple[int, int]] = []
    alive = {"value": True}

    def send(pid: int, sig: int) -> None:
        sent.append((pid, sig))
        alive["value"] = False

    message = forwards.stop_forward(
        "x",
        send_signal=send,
        alive_probe=lambda _pid: alive["value"],
        listening_probe=lambda _port: False,
        poll_clock=lambda _seconds: None,
    )

    assert sent == [(4242, signal.SIGINT)]
    assert "stopped forward" in message
    assert forwards.read_record("x") is None


def test_stop_escalates_sigint_sigterm_sigkill_until_the_process_dies(tmp_path: Path) -> None:
    _record(tmp_path, "x", pid=4242, port=51368)
    sent: list[tuple[int, int]] = []
    alive = {"value": True}

    def send(pid: int, sig: int) -> None:
        sent.append((pid, sig))
        if sig == signal.SIGKILL:
            alive["value"] = False

    message = forwards.stop_forward(
        "x",
        send_signal=send,
        alive_probe=lambda _pid: alive["value"],
        listening_probe=lambda _port: False,
        poll_clock=lambda _seconds: None,
    )

    assert "stopped forward" in message
    assert [sig for _pid, sig in sent] == [signal.SIGINT, signal.SIGTERM, signal.SIGKILL]


def test_stop_reports_nothing_to_stop_without_a_record(tmp_path: Path) -> None:
    message = forwards.stop_forward("x", poll_clock=lambda _seconds: None)
    assert "nothing to stop" in message


def test_stop_cleans_a_stale_record_without_signalling(tmp_path: Path) -> None:
    _record(tmp_path, "x", pid=999999999, port=51368)
    sent: list[tuple[int, int]] = []

    message = forwards.stop_forward(
        "x",
        send_signal=lambda pid, sig: sent.append((pid, sig)),
        alive_probe=lambda _pid: False,
        listening_probe=lambda _port: False,
        poll_clock=lambda _seconds: None,
    )

    assert "was not running" in message
    assert sent == []
    assert forwards.read_record("x") is None


def test_stop_raises_when_the_port_never_closes(tmp_path: Path) -> None:
    _record(tmp_path, "x", pid=4242, port=51368)

    with pytest.raises(ForwardError, match="still listening") as exc_info:
        forwards.stop_forward(
            "x",
            send_signal=lambda _pid, _sig: None,
            alive_probe=lambda _pid: False,
            listening_probe=lambda _port: True,
            poll_clock=lambda _seconds: None,
            poll_limit=2,
        )

    assert "lsof" in str(exc_info.value)
    assert forwards.read_record("x") is not None, (
        "a stop that could not verify the tunnel closed must leave the record in place: "
        "the failure is the report, and the record is what a retry works from"
    )


def test_stop_raises_when_the_port_probe_cannot_answer(tmp_path: Path) -> None:
    _record(tmp_path, "x", pid=4242, port=51368)

    with pytest.raises(ForwardError, match="could not be probed"):
        forwards.stop_forward(
            "x",
            send_signal=lambda _pid, _sig: None,
            alive_probe=lambda _pid: False,
            listening_probe=lambda _port: None,
            poll_clock=lambda _seconds: None,
        )


# ---------------------------------------------------------------------------
# refresh: stop, re-spawn the recorded command, re-record.
# ---------------------------------------------------------------------------


def test_refresh_respawns_the_recorded_command_and_re_records(tmp_path: Path) -> None:
    tmp_path = _git_root(tmp_path)
    _record(tmp_path, "x", pid=999999999, port=51368)
    spawner = _Spawner(pid=8888)

    def runner(_command: object, _timeout: float | None) -> CommandResult:
        return CommandResult(
            exit_code=0, stdout=json.dumps({"docker": {"Host": "tcp://127.0.0.1:51368"}})
        )

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(forwards, "_context_port_runner", runner)
        message = forwards.refresh_forward(
            "x",
            tmp_path,
            spawner=spawner,
            poll_clock=lambda _seconds: None,
        )

    assert "refreshed forward" in message and "pid 8888" in message
    assert spawner.argv == _child_argv()
    record = forwards.read_record("x")
    assert record is not None and record.pid == 8888 and record.port == 51368


def test_refresh_raises_naming_the_remedy_without_a_record(tmp_path: Path) -> None:
    with pytest.raises(ForwardError, match="make connect"):
        forwards.refresh_forward("x", tmp_path, poll_clock=lambda _seconds: None)


# ---------------------------------------------------------------------------
# The record survives a round trip; malformed records fail loudly.
# ---------------------------------------------------------------------------


def test_record_round_trips_through_json(tmp_path: Path) -> None:
    _record(tmp_path, "x", pid=4242, port=51368)
    record = forwards.read_record("x")
    assert record is not None
    again = ForwardRecord.from_json(record.to_json())
    assert again == record


def test_malformed_record_raises_instead_of_reading_as_absent(tmp_path: Path) -> None:
    path = forwards.record_path("x")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ForwardError, match="not valid JSON"):
        forwards.read_record("x")


# ---------------------------------------------------------------------------
# The module entry point.
# ---------------------------------------------------------------------------


def test_main_open_prints_the_message_and_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _git_root(tmp_path)
    spawner = _Spawner(pid=777)

    def runner(_command: object, _timeout: float | None) -> CommandResult:
        return CommandResult(
            exit_code=0, stdout=json.dumps({"docker": {"Host": "tcp://127.0.0.1:51368"}})
        )

    monkeypatch.setattr(forwards, "_context_port_runner", runner)
    monkeypatch.setattr(forwards, "_default_spawner", spawner)
    monkeypatch.chdir(root)
    exit_code = forwards.main(
        [
            "open",
            "--instance-id",
            "i-abc",
            "--context",
            "general-dev-x",
            "--profile",
            "profile",
            "--region",
            "us-east-1",
        ]
    )

    assert exit_code == 0
    assert "is open" in capsys.readouterr().out


def test_main_status_exits_one_when_the_forward_is_down(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = forwards.main(["status", "x"])
    assert exit_code == 1
    assert "no forward" in capsys.readouterr().out


def test_main_stop_reports_and_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = forwards.main(["stop", "x"])
    assert exit_code == 0
    assert "nothing to stop" in capsys.readouterr().out


def test_main_rejects_unknown_commands_and_wrong_argument_counts(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert forwards.main(["frobnicate"]) == 2
    assert forwards.main([]) == 2
    assert forwards.main(["status"]) == 1
    captured = capsys.readouterr()
    assert "usage:" in captured.err
    assert "takes exactly 1 argument" in captured.err


def test_main_list_exits_zero_for_a_known_down_forward_and_one_for_a_failed_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import subprocess as sp

    from gitfixtures import generated_root, init_repo

    root = generated_root(tmp_path)
    init_repo(root)
    sp.run(
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
    (root / "remote-instances" / "x").mkdir(parents=True)
    monkeypatch.chdir(root)
    assert forwards.main(["list"]) == 0, "a known-down forward is a valid answer"
    _record(root, "x", pid=999999999, port=51368)
    monkeypatch.setattr(forwards, "port_listening", lambda _port: None)
    assert forwards.main(["list"]) == 1, "a probe that cannot answer is the error case"
    assert "port probe failed" in capsys.readouterr().out


def test_module_entry_point_invokes_main_under_the_dunder_guard() -> None:
    """`python3 -m devcontainer_config.forwards` must actually run `main`.

    A module entry point without its `__main__` guard imports and exits 0
    silently -- every make target it backs would then succeed while doing
    nothing, which is the quietest failure a CLI can have. This pins the
    guard's presence and that it calls this module's own main.
    """
    import ast

    source = Path(forwards.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    guards = [
        node
        for node in tree.body
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Compare)
        and ast.unparse(node.test).replace("'", '"') == '__name__ == "__main__"'
    ]
    assert len(guards) == 1, "exactly one __main__ guard must exist on the module"
    calls = [
        ast.unparse(statement)
        for statement in guards[0].body
        if isinstance(statement, (ast.Expr, ast.Raise))
    ]
    assert any("sys.exit(main())" in call for call in calls), (
        "the __main__ guard must exit through this module's own main()"
    )
