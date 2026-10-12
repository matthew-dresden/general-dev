"""Every container this project owns, across every engine it can run on.

`instance_ops.list_state` answers "which instances exist and are they
reachable" for the EC2 fleet; this module answers the same question for the
containers a checkout's devcontainer creates. The engines are exactly the
two kinds this workspace runs on: this machine's local engine (the
`LOCAL_DOCKER_CONTEXT` the Makefile reads from config.env) and every
configured remote instance's context, derived through
`instances.docker_context` the same way the resolver derives it -- never a
second copy of that rule.

Discovery is the container label devcontainer.json writes (`runArgs`:
`devcontainer.project=<workspace name>`), the same label container.sh's
`rdc_container_ids` filters on, so a container belongs to this project on
any engine regardless of what renamed it afterward.

Every docker call is issued through a `CommandRunner`, the same seam the
probe modules use, so the hermetic suite substitutes a fake and nothing
here touches docker, AWS or the network under test. A failing probe
degrades per engine -- the row reports the failure and the listing keeps
going -- because one engine being down (a remote forward that is not open)
must not hide the containers the other engines answered for.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from . import instances, repo

# The project label devcontainer.json's runArgs stamps every container with;
# container.sh's rdc_container_ids filters on the identical pair, and the two
# must agree or this listing and the acting targets disagree about what
# exists.
_PROJECT_LABEL = "devcontainer.project"

# docker ps --format template: one tab-separated line per container, the four
# fields the table columns carry. Tab is the separator because a space inside
# a container name (legal, discouraged) would otherwise split a field.
_PS_FORMAT = "{{.Names}}\t{{.State}}\t{{.Image}}\t{{.RunningFor}}"


@dataclass
class ContainerRow:
    """One container an engine reported for this project's label."""

    name: str
    state: str
    image: str
    created: str


@dataclass
class EngineContainers:
    """The containers one engine holds, or why they could not be read.

    `containers` is None exactly when `lookup_error` is set: the probe
    failed (docker absent, context unknown, engine unreachable) and the row
    reports the failure instead of an empty listing, which would read as
    "no containers" -- a different, wrong answer.
    """

    engine: str
    context: str
    containers: list[ContainerRow] | None = None
    lookup_error: str | None = None


Runner = Callable[[Sequence[str], str | None], subprocess.CompletedProcess[str]]


def _engines(root: Path, local_context: str) -> list[tuple[str, str]]:
    """`(engine, context)` pairs: the local engine first, then every instance's.

    The local engine leads because it is the one every checkout starts on;
    instance order is discovery order (sorted), the same order
    `instance-list` reports.
    """
    pairs = [("local", local_context)]
    for name in instances.discover(root):
        pairs.append((name, instances.docker_context(root, name)))
    return pairs


def list_containers(
    root: Path, local_context: str, runner: Runner | None = None
) -> list[EngineContainers]:
    """Probe every engine and collect this project's containers on each.

    `local_context` is the LOCAL_DOCKER_CONTEXT value the caller read from
    config.env -- the one place it is parsed -- and an empty value is
    refused here rather than probed, because `docker --context ""` targets
    the active context and would silently answer for the wrong engine.
    """
    if not local_context:
        raise ValueError(
            "local_context is empty: read LOCAL_DOCKER_CONTEXT from "
            ".devcontainer/remote-docker/config.env and pass it explicitly"
        )
    if runner is None:
        runner = subprocess_runner
    label = f"label={_PROJECT_LABEL}={repo.workspace_name(root)}"
    rows: list[EngineContainers] = []
    for engine, context in _engines(root, local_context):
        result = runner(
            (
                "docker",
                "--context",
                context,
                "ps",
                "-a",
                "--filter",
                label,
                "--format",
                _PS_FORMAT,
            ),
            None,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip().splitlines()
            rows.append(
                EngineContainers(
                    engine=engine,
                    context=context,
                    lookup_error=detail[0] if detail else f"docker exited {result.returncode}",
                )
            )
            continue
        rows.append(
            EngineContainers(
                engine=engine,
                context=context,
                containers=_parse_ps_output(result.stdout),
            )
        )
    return rows


def _parse_ps_output(stdout: str) -> list[ContainerRow]:
    """Docker's tab-separated lines into rows; a malformed line is an error.

    The format template fixes four fields, so any line without exactly three
    tabs is not docker output this module asked for; reporting it as a
    container would invent state, and skipping it would hide a container.
    """
    containers: list[ContainerRow] = []
    for line in stdout.splitlines():
        if not line:
            continue
        fields = line.split("\t")
        if len(fields) != 4:
            raise ValueError(
                f"docker ps reported a line the format template does not shape: {line!r}"
            )
        containers.append(
            ContainerRow(name=fields[0], state=fields[1], image=fields[2], created=fields[3])
        )
    return containers


def subprocess_runner(
    argv: Sequence[str], stdin: str | None, *, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """The production Runner: a real subprocess, fed `stdin` on its stdin.

    This is what a caller outside the test suite passes to every function in
    this module. Decoding is pinned to UTF-8 with strict errors, so command
    output decodes identically on every host regardless of the ambient
    locale (the same convention `devcontainer_config.hostcreds`
    .subprocess_runner establishes).
    """
    return subprocess.run(
        list(argv),
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        env=env,
    )
