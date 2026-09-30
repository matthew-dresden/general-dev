"""AC-4.8: the set of instance-scoped targets and the set accepting `INSTANCE` are equal.

"Instance-scoped" is read off the Makefile as "dispatches through one of the
instance-aware entry scripts" -- the scripts that call `rd_resolve_instance` and
so act on one named instance. Two of them reach the remote engine
(`container.sh`, `push-secrets.sh`) and one addresses an instance's material
without touching an engine at all (`certs.sh`, whose `ca` and `client`
subcommands write only to the local certificate directory). The property that
matters is the same for all three: a target scoped to one instance must accept
`INSTANCE`, or `INSTANCE=<name> make <target>` silently acts on the default
instead.

`tests/test_remote_target_instance_wiring.py` checks that each remote recipe
passes `INSTANCE` through. That is a forward check, and a forward check alone
cannot notice a target that reaches the remote engine by some route the parser
does not recognise, nor one that passes `INSTANCE` while doing nothing remote.

This module asserts the two sets are equal, in both directions, from the
Makefile as parsed rather than from a list written here. A hand-maintained list
would drift the moment a target was added, which is the drift the equality
exists to catch.

The vacuous-pass guards matter as much as the assertions. Two empty sets are
equal, so a parser that silently stops matching would turn this file green
while checking nothing. Each side is therefore required to be non-empty, and
the parse is required to find the specific targets that are known to exist.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_MAKEFILE = _REPO_ROOT / "Makefile"

# Targets dispatched through an instance-aware script, and so required to accept
# an instance. Known members, used only as a guard that the parse still works --
# never as the expected set itself, which is derived below.
_KNOWN_REMOTE_TARGETS = frozenset(
    {"status", "build", "up", "clean", "push-secrets", "cert-publish"}
)


def _makefile_text() -> str:
    return _MAKEFILE.read_text(encoding="utf-8")


def _target_recipes() -> dict[str, list[str]]:
    """Every target mapped to its recipe lines."""
    recipes: dict[str, list[str]] = {}
    current: str | None = None
    for line in _makefile_text().splitlines():
        if line.startswith("\t"):
            if current is not None:
                recipes.setdefault(current, []).append(line)
            continue
        match = re.match(r"^([A-Za-z0-9_.-]+):(?!=)", line)
        current = match.group(1) if match else None
    return recipes


def _targets_reaching_the_remote_engine() -> set[str]:
    return {
        target
        for target, lines in _target_recipes().items()
        if any(re.search(r"\$\((CONTAINER_SH|SECRETS_SH|CERTS_SH)\)", line) for line in lines)
    }


def _targets_accepting_instance() -> set[str]:
    return {
        target
        for target, lines in _target_recipes().items()
        if any('INSTANCE="$(INSTANCE)"' in line for line in lines)
    }


def test_the_parse_finds_targets_at_all() -> None:
    """Guard: a broken parser would make every assertion below vacuous."""
    recipes = _target_recipes()
    assert len(recipes) >= 15, f"parsed only {len(recipes)} targets; the parser has drifted"


def test_the_parse_finds_the_known_remote_targets() -> None:
    """Guard: the remote-target parse specifically, not just any targets."""
    found = _targets_reaching_the_remote_engine()
    missing = sorted(_KNOWN_REMOTE_TARGETS - found)
    assert not missing, (
        f"these targets are known to reach the remote engine but were not parsed as such: "
        f"{missing}. The parser, not the Makefile, is likely wrong."
    )


def test_both_sets_are_non_empty() -> None:
    """Guard: two empty sets compare equal, which would pass while checking nothing."""
    assert _targets_reaching_the_remote_engine(), "no remote targets parsed"
    assert _targets_accepting_instance(), "no INSTANCE-accepting targets parsed"


def test_every_remote_target_accepts_instance() -> None:
    """Forward: nothing reaches the remote engine without an instance."""
    missing = sorted(_targets_reaching_the_remote_engine() - _targets_accepting_instance())
    assert not missing, (
        f"these targets reach the remote engine but do not accept INSTANCE, so "
        f"`INSTANCE=<name> make <target>` would silently act on something else: {missing}"
    )


def test_every_instance_accepting_target_is_remote() -> None:
    """Reverse: nothing passes INSTANCE while doing nothing remote.

    A target that accepts an instance it cannot act on is a false promise: the
    caller believes it selected something, and nothing downstream reads it.
    """
    extra = sorted(_targets_accepting_instance() - _targets_reaching_the_remote_engine())
    assert not extra, (
        f"these targets accept INSTANCE but never reach the remote engine, so the value is "
        f"silently ignored: {extra}"
    )


def test_the_two_sets_are_identical() -> None:
    """The equality AC-4.8 actually states, asserted directly."""
    assert _targets_reaching_the_remote_engine() == _targets_accepting_instance()
