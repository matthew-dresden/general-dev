"""The ALL=1 fleet forms of the container targets, and the guards they stand on.

The instance family has had ALL=1 from the start; the container family now
matches it: ALL=1 runs status, start, stop, restart, check or clean once per
engine -- the local engine plus every configured instance -- with RDC_ALL
exported so container.sh takes every project container on that engine
instead of refusing ambiguity. clean's fleet form is gated behind
CONFIRM=destroy at the Makefile, mirroring instance-destroy and
bucket-delete, and CONTAINER alongside ALL is refused everywhere, the same
one-or-the-other rule INSTANCE and ALL enforce.

Also pinned here: the teardown image guard. Several containers of this
project share one image, so an unconditional rmi in a teardown would either
fail mid-run (docker refuses an image a sibling still runs) or strand the
sibling without its image once it succeeded; the ancestor probe now runs
after the container's removal, so it cannot count the container being torn
down.

Static pins over the script and Makefile text, the approach
`tests/test_named_build.py` and `tests/test_container_verify.py` take:
no docker in this file (AC-TEST-004).
"""

from __future__ import annotations

from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CONTAINER_SH = _REPO_ROOT / ".devcontainer" / "remote-docker" / "container.sh"
_MAKEFILE = _REPO_ROOT / "Makefile"

_FLEET_TARGETS = ("status", "start", "stop", "restart", "check", "clean")


@pytest.fixture(scope="module")
def container_text() -> str:
    return _CONTAINER_SH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def makefile_text() -> str:
    return _MAKEFILE.read_text(encoding="utf-8")


def _function_body(name: str, text: str) -> str:
    start = text.index(f"{name}() {{")
    end = text.index("\n}\n", start)
    return text[start:end]


def test_fleet_selection_takes_every_project_container(container_text: str) -> None:
    """RDC_ALL widens the single-container contract; CONTAINER alongside it is refused."""
    body = _function_body("rdc_selected_ids", container_text)
    assert "RDC_ALL:-0" in body
    assert "rdc_container_ids" in body, "the fleet path reads the same label the single path does"
    assert "pass CONTAINER=<name> or ALL=1, not both" in body


def test_every_acting_target_selects_through_the_fleet_aware_helper(
    container_text: str,
) -> None:
    """start, stop, restart, check and clean all loop ids from rdc_selected_ids."""
    for name in ("rdc_start", "rdc_stop", "rdc_restart", "rdc_check", "rdc_clean"):
        body = _function_body(name, container_text)
        assert "rdc_selected_ids" in body, f"{name} must select through rdc_selected_ids"


def test_teardown_keeps_an_image_a_sibling_still_uses(container_text: str) -> None:
    """The ancestor probe runs after the container's removal and before any rmi."""
    body = _function_body("rdc_clean_one", container_text)
    rm_at = body.index('rd_docker rm -f "$id"')
    probe_at = body.index('--filter "ancestor=${image}"')
    rmi_at = body.index('rd_docker rmi "$image"')
    assert rm_at < probe_at < rmi_at, (
        "the image probe must follow the container's removal (so it cannot count "
        "the container being torn down) and precede the rmi it gates"
    )
    assert "another container still uses it" in body, "the kept image says why"


def test_the_work_loss_guard_still_runs_per_container(container_text: str) -> None:
    """Fleet teardown checks every volume, not just the first container's."""
    body = _function_body("rdc_clean_one", container_text)
    guard_at = body.index("rdc_check_one")
    rm_at = body.index('rd_docker rm -f "$id"')
    assert guard_at < rm_at, "the unpushed-work check precedes the removal it protects"


def test_every_fleet_target_dispatches_through_the_shared_define(
    makefile_text: str,
) -> None:
    """One dispatcher owns the loop; six thin recipes call it by subcommand name."""
    assert "define CONTAINER_FLEET_DISPATCH" in makefile_text
    for target in _FLEET_TARGETS:
        assert f"$(call CONTAINER_FLEET_DISPATCH,{target})" in makefile_text, (
            f"the {target} recipe must dispatch through the shared fleet define"
        )


def test_the_fleet_loop_covers_local_plus_every_configured_engine(
    makefile_text: str,
) -> None:
    """The engine list is 'local' plus the run-time discovery, never a parse-time shell."""
    start = makefile_text.index("define CONTAINER_FLEET_DISPATCH")
    end = makefile_text.index("endef", start)
    define = makefile_text[start:end]
    assert 'engines="local $$($(DISCOVER_INSTANCE_NAMES))"' in define, (
        "the loop must start at the local engine and continue with the same "
        "discovery the instance targets use, computed at run time"
    )
    assert 'ENGINE="$$engine"' in define and "RDC_ALL=1" in define, (
        "each iteration pins one engine through ENGINE and widens the script through RDC_ALL"
    )


def test_the_fleet_refuses_container_alongside_all(makefile_text: str) -> None:
    start = makefile_text.index("define CONTAINER_FLEET_DISPATCH")
    end = makefile_text.index("endef", start)
    define = makefile_text[start:end]
    assert "pass CONTAINER=<name> or ALL=1, not both" in define


def test_fleet_teardown_is_gated_behind_confirm_destroy(makefile_text: str) -> None:
    """clean ALL=1 without CONFIRM=destroy refuses, stating the exact command."""
    start = makefile_text.index("define CONTAINER_FLEET_DISPATCH")
    end = makefile_text.index("endef", start)
    define = makefile_text[start:end]
    assert '"$(1)" = "clean"' in define, "the CONFIRM gate is scoped to the clean subcommand"
    assert "make clean ALL=1 CONFIRM=destroy" in define, (
        "the refusal states the exact confirming command, like bucket-delete"
    )
    assert "FORCE=1 bypasses those too" in define, (
        "the refusal also states the separate role FORCE keeps"
    )


def test_no_fleet_target_still_names_the_removed_host_shell() -> None:
    """The cutover removed the EC2 host shell; no remedy may point at its tombstone."""
    for path in (
        _CONTAINER_SH,
        _REPO_ROOT / ".devcontainer" / "remote-docker" / "lib.sh",
    ):
        text = path.read_text(encoding="utf-8")
        assert "make shell," not in text and "make shell${RD_RESET}" not in text, (
            f"{path.name} still points a remedy at the removed host-shell target; "
            "the in-container shell is 'make exec', engine diagnostics are context-scoped docker calls"
        )
