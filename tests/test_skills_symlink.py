"""The plugin's `skills/` entry is a relative symlink to the canonical home.

The canonical, agent-agnostic skills home is `.agents/skills/<name>/SKILL.md`
(every name `gd-`-prefixed). Claude Code consumes the same roster through
`.claude/plugins/devcontainer/skills`, which must be a *relative* symlink to
`../../../.agents/skills` -- a pointer, never a copy. A real directory there
would be a second source of truth that drifts from the roster; an absolute
symlink would break every clone whose checkout does not live at the
absolute path the link was created from.

These assertions read the filesystem, not `.gitignore` or any manifest: the
symlink's own target string, what it resolves to, and the name set each
side of it exposes. `tests/test_skill_lint.py` pins the roster table to the
canonical directory's contents; this module pins the plugin-side route to
that same directory, so the two ends of the pointer cannot drift apart.

The repository root is resolved through `devcontainer_config.repo.find_root`
before any path assertion, so a checkout outside a repository surfaces as a
`RepoError` naming the cause rather than as a missing-path assertion that
only looks like a pass.
"""

from __future__ import annotations

import os
from pathlib import Path

from devcontainer_config import repo

_RELATIVE_TARGET = "../../../.agents/skills"


def _repo_root() -> Path:
    return repo.find_root(Path(__file__).resolve().parent)


def _plugin_skills_path() -> Path:
    return _repo_root() / ".claude" / "plugins" / "devcontainer" / "skills"


def _canonical_skills_root() -> Path:
    return _repo_root() / ".agents" / "skills"


def _roster_names(skills_dir: Path) -> set[str]:
    return {
        entry.name
        for entry in sorted(skills_dir.iterdir())
        if entry.is_dir() and (entry / "SKILL.md").is_file()
    }


def test_plugin_skills_entry_is_a_symlink_not_a_real_directory() -> None:
    """A real directory at the plugin's skills path would fork the roster: this
    pins the pointer being a pointer.
    """
    plugin_skills = _plugin_skills_path()
    assert plugin_skills.is_symlink(), (
        f"{plugin_skills} is not a symlink; the plugin must route to the canonical "
        ".agents/skills home, never hold its own copy of the roster"
    )


def test_symlink_target_is_relative_and_resolves_to_the_canonical_home() -> None:
    """The target string is the documented relative form, and it resolves to the
    canonical skills root from the checkout it actually lives in.

    The exact-string assertion pins the documented form (`../../../.agents/
    skills`); the resolution assertion proves it works from this checkout, so
    a correctly-spelled-but-misplaced link cannot pass on spelling alone.
    """
    plugin_skills = _plugin_skills_path()
    target = os.readlink(plugin_skills)
    assert not os.path.isabs(target), (
        f"the plugin skills symlink target {target!r} is absolute; it must be "
        f"relative ({_RELATIVE_TARGET!r}) so a clone at any path resolves it"
    )
    assert target == _RELATIVE_TARGET, (
        f"the plugin skills symlink target is {target!r}, expected {_RELATIVE_TARGET!r}"
    )
    resolved = plugin_skills.resolve()
    canonical = _canonical_skills_root().resolve()
    assert resolved == canonical, (
        f"the plugin skills symlink resolves to {resolved}, not the canonical home {canonical}"
    )


def test_symlink_exposes_the_same_roster_as_the_canonical_home() -> None:
    """Name-set parity: every skill reachable through the plugin's symlink is the
    canonical set, and nothing extra is reachable through it.
    """
    through_plugin = _roster_names(_plugin_skills_path())
    canonical = _roster_names(_canonical_skills_root())
    assert canonical, (
        "the canonical .agents/skills home holds no skills; the roster cannot be "
        "empty if the plugin route is to resolve anything"
    )
    missing = sorted(canonical - through_plugin)
    extra = sorted(through_plugin - canonical)
    assert not missing and not extra, (
        f"the plugin's skills route and the canonical home disagree: "
        f"missing through the plugin: {missing}; extra through the plugin: {extra}"
    )


def test_symlink_exposes_the_same_skill_md_count_as_the_canonical_home() -> None:
    """Count parity of `SKILL.md` files on both sides of the pointer -- the cheap
    arithmetic check that catches a partially-populated copy, should one ever
    appear, even before the name sets are compared.
    """
    plugin_count = len(list((_plugin_skills_path()).glob("*/SKILL.md")))
    canonical_count = len(list((_canonical_skills_root()).glob("*/SKILL.md")))
    assert plugin_count == canonical_count, (
        f"the plugin's skills route exposes {plugin_count} SKILL.md files while the "
        f"canonical home holds {canonical_count}"
    )
