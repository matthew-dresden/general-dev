"""Hermetic unit suite for `devcontainer_config.skills_install` (U3).

The engine behind `make skills-install`, `make skills-remove` and
`make skills-list` takes its repository root and home directory as explicit
parameters -- there is no `Path.home()` and no repository discovery inside
the module -- so this suite drives every scope, agent, success and refusal
against throwaway directories under `tmp_path`, touching neither the real
home nor a real checkout.

The fixtures reconstruct, in miniature, the two inputs the engine reads at
generation time: `.devcontainer/opencode.json` (whose provider/model block
the opencode runtime incantation embeds, because an `OPENCODE_CONFIG`
override replaces the project config) and the plugin tree's
`.claude-plugin/marketplace.json` (whose name fields the Claude Code
runtime settings JSON takes verbatim). Both are written as synthetic JSON
with placeholder values, never copied from the real files, so the tests
pin the mechanism rather than any particular provider's coordinates.

The `claude --help` seam is injectable (`claude_runtime_incantation`'s
`claude_help_text` parameter), which is what keeps the supported and
unsupported branches testable without the real binary; the one test that
exercises the live read puts a fake `claude` executable on a doctored
`PATH`, the same pattern `tests/test_makefile_contract.py` uses for its
guard-loop tests. The timeout test drives a busy-looping fake -- no
`sleep` anywhere -- against an env-configurable deadline.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import uuid
from collections.abc import Callable
from pathlib import Path

import pytest
from devcontainer_config import skills_install as si

# A placeholder provider/model pair the synthetic .devcontainer/opencode.json
# carries; the opencode incantation test asserts this model value survives
# into the printed JSON, proving the project config was read at generation
# time rather than the override being skills-only.
_PROJECT_MODEL = "example-org/example-model"

# The user-skill siblings the remove-refusal property test plants beside the
# link this module owns: a remove run must leave every one of them exactly
# as found, because they are the operator's own entries.
_USER_SKILL_SIBLINGS: tuple[str, ...] = ("aws-secrets", "google-workspace", "onepassword-secrets")


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A synthetic checkout carrying every file the engine reads."""
    root = tmp_path / "checkout"
    skills = root / ".agents" / "skills"
    skills.mkdir(parents=True)
    (skills / "gd-sample").mkdir()
    (skills / "gd-sample" / "SKILL.md").write_text("# sample\n", encoding="utf-8")
    _write_json(
        root / ".devcontainer" / "opencode.json",
        {"$schema": "https://opencode.ai/config.json", "model": _PROJECT_MODEL},
    )
    _write_json(
        root / ".claude" / "plugins" / "devcontainer" / ".claude-plugin" / "marketplace.json",
        {"name": "example-marketplace", "plugins": [{"name": "example-plugin"}]},
    )
    return root


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """A synthetic home directory the engine's global scopes act inside."""
    home = tmp_path / "home"
    home.mkdir()
    return home


def _link(home: Path, agent: str) -> Path:
    return si.global_skills_dir(home, agent) / si.SKILL_LINK_NAME


def _foreign_target(tmp_path: Path) -> Path:
    """A target outside any repository, for foreign-symlink refusals."""
    foreign = tmp_path / f"foreign-skills-{uuid.uuid4().hex}"
    foreign.mkdir()
    return foreign


def _user_skill_siblings(home: Path, agent: str) -> list[Path]:
    """The agent dir pre-populated with the operator's own skill entries."""
    agent_dir = si.global_skills_dir(home, agent)
    agent_dir.mkdir(parents=True)
    planted = []
    for sibling in _USER_SKILL_SIBLINGS:
        entry = agent_dir / sibling
        entry.mkdir()
        (entry / "SKILL.md").write_text("# theirs\n", encoding="utf-8")
        planted.append(entry)
    return planted


# ---------------------------------------------------------------------------
# install: global scope
# ---------------------------------------------------------------------------


def test_install_global_creates_one_symlink_per_agent(repo: Path, home: Path) -> None:
    messages = si.install(repo, home, si.AGENT_BOTH, si.SCOPE_GLOBAL)
    canonical = si.canonical_skills_root(repo)
    assert len(messages) == 2
    for agent in si.AGENTS:
        link = _link(home, agent)
        assert link.is_symlink(), f"{link} was not created as a symlink"
        assert link.resolve() == canonical.resolve()
        # The absolute target is a recorded decision for a Mac-personal
        # install, not a silent one: the agent's own message must carry it.
        assert any(
            message.startswith(f"{agent} ") and str(canonical) in message for message in messages
        )


def test_install_global_message_records_the_absolute_target(repo: Path, home: Path) -> None:
    messages = si.install(repo, home, si.AGENT_OPENCODE, si.SCOPE_GLOBAL)
    assert len(messages) == 1
    assert str(si.canonical_skills_root(repo)) in messages[0]
    assert "absolute target" in messages[0]


def test_install_global_is_idempotent_over_our_own_link(repo: Path, home: Path) -> None:
    si.install(repo, home, si.AGENT_CLAUDE, si.SCOPE_GLOBAL)
    link_before = os.readlink(_link(home, si.AGENT_CLAUDE))
    messages = si.install(repo, home, si.AGENT_CLAUDE, si.SCOPE_GLOBAL)
    assert "already installed" in messages[0]
    assert os.readlink(_link(home, si.AGENT_CLAUDE)) == link_before


def test_install_global_refuses_a_foreign_symlink_at_the_name(
    repo: Path, home: Path, tmp_path: Path
) -> None:
    _user_skill_siblings(home, si.AGENT_OPENCODE)
    link = _link(home, si.AGENT_OPENCODE)
    link.symlink_to(_foreign_target(tmp_path))
    with pytest.raises(si.SkillsInstallError) as exc_info:
        si.install(repo, home, si.AGENT_OPENCODE, si.SCOPE_GLOBAL)
    assert str(link) in str(exc_info.value)
    assert link.is_symlink(), "the refused entry must be left exactly as found"


def test_install_global_refuses_a_broken_symlink_at_the_name(
    repo: Path, home: Path, tmp_path: Path
) -> None:
    link = _link(home, si.AGENT_CLAUDE)
    link.parent.mkdir(parents=True)
    link.symlink_to(tmp_path / "gone" / "nowhere")
    with pytest.raises(si.SkillsInstallError):
        si.install(repo, home, si.AGENT_CLAUDE, si.SCOPE_GLOBAL)
    assert link.is_symlink()


def test_install_global_refuses_a_real_directory_at_the_name(repo: Path, home: Path) -> None:
    _user_skill_siblings(home, si.AGENT_CLAUDE)
    link = _link(home, si.AGENT_CLAUDE)
    link.mkdir()
    (link / "SKILL.md").write_text("# theirs\n", encoding="utf-8")
    with pytest.raises(si.SkillsInstallError) as exc_info:
        si.install(repo, home, si.AGENT_CLAUDE, si.SCOPE_GLOBAL)
    assert "not a symlink" in str(exc_info.value)
    assert (link / "SKILL.md").is_file(), "the real directory must survive the refusal"


def test_install_global_single_agent_touches_only_that_agent(repo: Path, home: Path) -> None:
    si.install(repo, home, si.AGENT_OPENCODE, si.SCOPE_GLOBAL)
    assert _link(home, si.AGENT_OPENCODE).is_symlink()
    assert not si.global_skills_dir(home, si.AGENT_CLAUDE).exists(), (
        "AGENT=opencode must not create the Claude Code directory at all"
    )


def test_install_global_refuses_when_the_canonical_home_is_missing(
    tmp_path: Path, home: Path
) -> None:
    bare = tmp_path / "bare-checkout"
    bare.mkdir()
    with pytest.raises(si.SkillsInstallError) as exc_info:
        si.install(bare, home, si.AGENT_BOTH, si.SCOPE_GLOBAL)
    assert str(si.canonical_skills_root(bare)) in str(exc_info.value)


# ---------------------------------------------------------------------------
# install: project scope
# ---------------------------------------------------------------------------


def test_install_project_opencode_is_native(repo: Path, home: Path) -> None:
    messages = si.install(repo, home, si.AGENT_OPENCODE, si.SCOPE_PROJECT)
    assert "native" in messages[0]
    assert "nothing to wire" in messages[0]
    assert not _link(home, si.AGENT_OPENCODE).exists(), (
        "the project scope must not create any global entry"
    )


def test_install_project_wires_the_claude_plugin_link_when_missing(repo: Path, home: Path) -> None:
    messages = si.install(repo, home, si.AGENT_CLAUDE, si.SCOPE_PROJECT)
    link = si.plugin_skills_link(repo)
    assert link.is_symlink()
    # The relative form is what tests/test_skills_symlink.py pins for the
    # tracked link; wiring must produce exactly that target string.
    assert os.readlink(link) == si.PLUGIN_SKILLS_RELATIVE_TARGET
    assert link.resolve() == si.canonical_skills_root(repo).resolve()
    assert "wired" in messages[0]


def test_install_project_claude_is_idempotent(repo: Path, home: Path) -> None:
    si.install(repo, home, si.AGENT_CLAUDE, si.SCOPE_PROJECT)
    messages = si.install(repo, home, si.AGENT_CLAUDE, si.SCOPE_PROJECT)
    assert "already wired" in messages[0]


def test_install_project_refuses_a_real_directory_at_the_plugin_link(
    repo: Path, home: Path
) -> None:
    link = si.plugin_skills_link(repo)
    link.mkdir(parents=True)
    (link / "gd-sample").mkdir()
    with pytest.raises(si.SkillsInstallError):
        si.install(repo, home, si.AGENT_CLAUDE, si.SCOPE_PROJECT)
    assert (link / "gd-sample").is_dir(), "a forked roster directory must survive the refusal"


def test_install_project_refuses_a_mispointed_plugin_link(
    repo: Path, home: Path, tmp_path: Path
) -> None:
    link = si.plugin_skills_link(repo)
    link.symlink_to(_foreign_target(tmp_path))
    with pytest.raises(si.SkillsInstallError):
        si.install(repo, home, si.AGENT_CLAUDE, si.SCOPE_PROJECT)
    assert link.is_symlink()


def test_install_project_refuses_when_a_parent_path_is_a_regular_file(
    repo: Path, home: Path
) -> None:
    """A regular file where the plugin directory belongs is refused, not a
    raw OSError traceback: the link cannot be created under it."""
    shutil.rmtree(repo / ".claude")
    (repo / ".claude" / "plugins").mkdir(parents=True)
    (repo / ".claude" / "plugins" / "devcontainer").write_text("not a dir\n", encoding="utf-8")
    with pytest.raises(si.SkillsInstallError) as exc_info:
        si.install(repo, home, si.AGENT_CLAUDE, si.SCOPE_PROJECT)
    assert "failed (" in str(exc_info.value)


def test_install_project_verification_catches_a_relocated_plugins_tree(
    repo: Path, home: Path, tmp_path: Path
) -> None:
    """A symlinked plugins parent makes the fresh link resolve outside the
    checkout; the write-then-verify step catches it and refuses."""
    relocated = tmp_path / "elsewhere"
    (relocated / "devcontainer" / ".claude-plugin").mkdir(parents=True)
    _write_json(
        relocated / "devcontainer" / ".claude-plugin" / "marketplace.json",
        {"name": "example-marketplace", "plugins": [{"name": "example-plugin"}]},
    )
    shutil.rmtree(repo / ".claude")
    (repo / ".claude").mkdir(parents=True)
    plugins = repo / ".claude" / "plugins"
    plugins.symlink_to(relocated)
    with pytest.raises(si.SkillsInstallError) as exc_info:
        si.install(repo, home, si.AGENT_CLAUDE, si.SCOPE_PROJECT)
    assert "did not resolve" in str(exc_info.value)


# ---------------------------------------------------------------------------
# remove
# ---------------------------------------------------------------------------


def test_remove_global_deletes_only_our_link_and_never_the_siblings(repo: Path, home: Path) -> None:
    planted = _user_skill_siblings(home, si.AGENT_OPENCODE)
    si.install(repo, home, si.AGENT_OPENCODE, si.SCOPE_GLOBAL)
    messages = si.remove(repo, home, si.AGENT_OPENCODE, si.SCOPE_GLOBAL)
    assert "removed" in messages[0]
    assert not _link(home, si.AGENT_OPENCODE).exists()
    assert si.canonical_skills_root(repo).is_dir(), "the canonical home is never touched"
    for entry in planted:
        assert entry.is_dir() and (entry / "SKILL.md").is_file(), (
            f"{entry} is the operator's own skill and must be untouched"
        )


def test_remove_global_absent_reports_nothing_to_remove(repo: Path, home: Path) -> None:
    messages = si.remove(repo, home, si.AGENT_CLAUDE, si.SCOPE_GLOBAL)
    assert "not installed" in messages[0]
    assert "nothing to remove" in messages[0]


def test_remove_global_refuses_a_non_symlink(repo: Path, home: Path) -> None:
    link = _link(home, si.AGENT_OPENCODE)
    link.mkdir(parents=True)
    with pytest.raises(si.SkillsInstallError) as exc_info:
        si.remove(repo, home, si.AGENT_OPENCODE, si.SCOPE_GLOBAL)
    assert "not a symlink" in str(exc_info.value)
    assert link.is_dir()


def test_remove_global_refuses_a_symlink_resolving_outside_the_repo(
    repo: Path, home: Path, tmp_path: Path
) -> None:
    link = _link(home, si.AGENT_CLAUDE)
    link.parent.mkdir(parents=True)
    link.symlink_to(_foreign_target(tmp_path))
    with pytest.raises(si.SkillsInstallError) as exc_info:
        si.remove(repo, home, si.AGENT_CLAUDE, si.SCOPE_GLOBAL)
    assert "left untouched" in str(exc_info.value)
    assert link.is_symlink(), "a foreign-target symlink must survive the refusal"


def test_remove_project_changes_nothing(repo: Path, home: Path) -> None:
    si.install(repo, home, si.AGENT_CLAUDE, si.SCOPE_PROJECT)
    messages = si.remove(repo, home, si.AGENT_BOTH, si.SCOPE_PROJECT)
    assert len(messages) == 2
    assert "native" in messages[0]
    assert "nothing was removed" in messages[1]
    assert si.plugin_skills_link(repo).is_symlink(), "the tracked adapter must survive remove"


def test_remove_runtime_changes_nothing(repo: Path, home: Path) -> None:
    messages = si.remove(repo, home, si.AGENT_BOTH, si.SCOPE_RUNTIME)
    assert all("nothing to remove" in message for message in messages)
    assert not _link(home, si.AGENT_OPENCODE).exists()
    assert not _link(home, si.AGENT_CLAUDE).exists()


# ---------------------------------------------------------------------------
# list (report)
# ---------------------------------------------------------------------------


def test_report_global_not_installed(repo: Path, home: Path) -> None:
    messages = si.report(repo, home, si.AGENT_BOTH, si.SCOPE_GLOBAL)
    assert all("not installed" in message for message in messages)


def test_report_global_installed_names_the_target(repo: Path, home: Path) -> None:
    si.install(repo, home, si.AGENT_BOTH, si.SCOPE_GLOBAL)
    messages = si.report(repo, home, si.AGENT_BOTH, si.SCOPE_GLOBAL)
    assert len(messages) == 2
    for message in messages:
        assert "installed" in message
        assert str(si.canonical_skills_root(repo).resolve()) in message


def test_report_project_native_and_wired(repo: Path, home: Path) -> None:
    si.install(repo, home, si.AGENT_CLAUDE, si.SCOPE_PROJECT)
    messages = si.report(repo, home, si.AGENT_BOTH, si.SCOPE_PROJECT)
    assert "native" in messages[0]
    assert "wired" in messages[1]


def test_report_project_not_wired(repo: Path, home: Path) -> None:
    messages = si.report(repo, home, si.AGENT_CLAUDE, si.SCOPE_PROJECT)
    assert "not wired" in messages[0]


def test_report_runtime_has_no_persistent_state(repo: Path, home: Path) -> None:
    messages = si.report(repo, home, si.AGENT_OPENCODE, si.SCOPE_RUNTIME)
    assert "no persistent state" in messages[0]
    assert "make skills-install" in messages[0]


def test_report_global_refuses_a_foreign_entry(repo: Path, home: Path, tmp_path: Path) -> None:
    link = _link(home, si.AGENT_OPENCODE)
    link.parent.mkdir(parents=True)
    link.symlink_to(_foreign_target(tmp_path))
    with pytest.raises(si.SkillsInstallError):
        si.report(repo, home, si.AGENT_OPENCODE, si.SCOPE_GLOBAL)


def test_report_global_refuses_a_non_symlink(repo: Path, home: Path) -> None:
    link = _link(home, si.AGENT_CLAUDE)
    link.mkdir(parents=True)
    with pytest.raises(si.SkillsInstallError):
        si.report(repo, home, si.AGENT_CLAUDE, si.SCOPE_GLOBAL)


def test_report_project_refuses_a_forked_plugin_link(repo: Path, home: Path) -> None:
    link = si.plugin_skills_link(repo)
    link.mkdir(parents=True)
    with pytest.raises(si.SkillsInstallError) as exc_info:
        si.report(repo, home, si.AGENT_CLAUDE, si.SCOPE_PROJECT)
    assert "not the documented relative symlink" in str(exc_info.value)


# ---------------------------------------------------------------------------
# runtime incantations
# ---------------------------------------------------------------------------


def test_install_runtime_prints_incantations_and_touches_nothing(
    repo: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`install(..., SCOPE_RUNTIME)` prints both incantations, creates nothing.

    The Claude Code help reader is stubbed here because the public
    `install` entry point has no help-text parameter to inject through and
    the alternative -- running the real `claude --help` -- would make this
    test depend on a binary CI does not install. The stub returns exactly
    the supported shape the live read produces.
    """
    monkeypatch.setattr(
        si, "_read_claude_help", lambda: f"  {si.CLAUDE_SETTINGS_FLAG} <file-or-json>"
    )
    messages = si.install(repo, home, si.AGENT_BOTH, si.SCOPE_RUNTIME)
    assert len(messages) == 2
    assert "OPENCODE_CONFIG" in messages[0]
    assert f"claude {si.CLAUDE_SETTINGS_FLAG}" in messages[1]
    assert not _link(home, si.AGENT_OPENCODE).exists()
    assert not _link(home, si.AGENT_CLAUDE).exists()


# ---------------------------------------------------------------------------
# runtime incantations
# ---------------------------------------------------------------------------


def test_opencode_incantation_embeds_the_project_config_and_the_skills_override(
    repo: Path,
) -> None:
    incantation = si.opencode_runtime_incantation(repo)
    assert "OPENCODE_CONFIG" in incantation
    assert "opencode" in incantation
    # The replace-semantics is stated where the operator reads it, and the
    # embedded JSON carries the project config read at generation time.
    assert "replaces the project config" in incantation
    assert _PROJECT_MODEL in incantation
    override = json.loads(incantation.split("<<'JSON'\n", 1)[1].split("\nJSON\n", 1)[0])
    assert override["model"] == _PROJECT_MODEL
    assert override["skills"] == {"paths": [str(si.canonical_skills_root(repo).resolve())]}


def test_opencode_incantation_refuses_a_missing_project_config(tmp_path: Path) -> None:
    bare = tmp_path / "bare"
    bare.mkdir()
    with pytest.raises(si.SkillsInstallError) as exc_info:
        si.opencode_runtime_incantation(bare)
    assert ".devcontainer/opencode.json" in str(exc_info.value)


def test_opencode_incantation_refuses_malformed_project_json(tmp_path: Path) -> None:
    root = tmp_path / "broken"
    (root / ".devcontainer").mkdir(parents=True)
    (root / ".devcontainer" / "opencode.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(si.SkillsInstallError):
        si.opencode_runtime_incantation(root)


def test_claude_incantation_carries_the_marketplace_json_when_supported(repo: Path) -> None:
    incantation = si.claude_runtime_incantation(
        repo, claude_help_text=f"  {si.CLAUDE_SETTINGS_FLAG} <file-or-json>  load settings"
    )
    assert f"claude {si.CLAUDE_SETTINGS_FLAG}" in incantation
    # The payload rides a shell single-quoted word on the final line (the
    # comment lines above it carry their own quotes), so isolate that line
    # first; the payload itself never contains a single quote.
    command_line = incantation.strip().splitlines()[-1]
    payload = json.loads(command_line.split("'")[1])
    marketplace_dir = str(repo / si.CLAUDE_MARKETPLACE_RELATIVE)
    assert payload["extraKnownMarketplaces"]["example-marketplace"]["source"] == {
        "source": "directory",
        "path": marketplace_dir,
    }
    assert payload["enabledPlugins"] == {"example-plugin@example-marketplace": True}


def test_claude_incantation_states_unsupported_and_the_fallback(repo: Path) -> None:
    incantation = si.claude_runtime_incantation(repo, claude_help_text="no flags here")
    assert "UNSUPPORTED" in incantation
    assert si.CLAUDE_SETTINGS_FLAG in incantation
    assert "make skills-install AGENT=claude SCOPE=global" in incantation
    assert "extraKnownMarketplaces" in incantation, "the manual fallback names the settings keys"


@pytest.mark.parametrize(
    ("manifest_payload", "expected_fragment"),
    [
        (None, "does not exist"),
        ("{not json", "not valid JSON"),
        ([], "does not hold a JSON object"),
        ({"plugins": [{"name": "example-plugin"}]}, "string 'name'"),
        ({"name": "example-marketplace"}, "string 'name'"),
        ({"name": "example-marketplace", "plugins": []}, "string 'name'"),
        ({"name": 1, "plugins": [{"name": "example-plugin"}]}, "string 'name'"),
    ],
)
def test_claude_incantation_refuses_a_drifted_marketplace_manifest(
    repo: Path, tmp_path: Path, manifest_payload: object, expected_fragment: str
) -> None:
    """Both names come from the plugin tree's manifest; every way it can
    drift (absent, unparsable, non-object, missing or mistyped names) is a
    refusal naming the manifest, never a hardcoded name falling back."""
    manifest_path = (
        repo / ".claude" / "plugins" / "devcontainer" / ".claude-plugin" / "marketplace.json"
    )
    if manifest_payload is None:
        manifest_path.unlink()
    else:
        if isinstance(manifest_payload, str):
            manifest_path.write_text(manifest_payload, encoding="utf-8")
        else:
            _write_json(manifest_path, manifest_payload)
    with pytest.raises(si.SkillsInstallError) as exc_info:
        si.claude_runtime_incantation(
            repo, claude_help_text=f"  {si.CLAUDE_SETTINGS_FLAG} <file-or-json>"
        )
    message = str(exc_info.value)
    assert expected_fragment in message
    assert "marketplace.json" in message


def test_claude_incantation_reads_the_live_help_when_not_injected(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The live read runs the real flag check against a fake `claude` on PATH."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_claude = bin_dir / "claude"
    # `echo` is a shell builtin, so the fake needs nothing from PATH: the
    # doctored PATH holds this directory alone.
    fake_claude.write_text(
        "#!/bin/sh\necho '  --settings <file-or-json>  Load additional settings'\n",
        encoding="utf-8",
    )
    fake_claude.chmod(fake_claude.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", str(bin_dir))
    incantation = si.claude_runtime_incantation(repo)
    assert f"claude {si.CLAUDE_SETTINGS_FLAG}" in incantation


def test_claude_help_read_refuses_a_missing_binary(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = tmp_path / "empty-bin"
    bin_dir.mkdir()
    monkeypatch.setenv("PATH", str(bin_dir))
    with pytest.raises(si.SkillsInstallError) as exc_info:
        si.claude_runtime_incantation(repo)
    assert "not on PATH" in str(exc_info.value)


def test_claude_help_read_refuses_an_invalid_deadline(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(si.CLAUDE_HELP_TIMEOUT_ENV_VAR, "not-a-number")
    with pytest.raises(si.SkillsInstallError) as exc_info:
        si.claude_runtime_incantation(repo)
    assert si.CLAUDE_HELP_TIMEOUT_ENV_VAR in str(exc_info.value)


def test_claude_help_read_times_out_against_a_stuck_binary(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A binary that never answers is refused, bounded by the env deadline.

    The fake busy-loops rather than sleeping: the test waits on the
    configured deadline only, and the deadline itself comes from the
    environment variable the module documents (no hard-coded bound).
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_claude = bin_dir / "claude"
    fake_claude.write_text("#!/bin/sh\nwhile true; do :; done\n", encoding="utf-8")
    fake_claude.chmod(fake_claude.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv(si.CLAUDE_HELP_TIMEOUT_ENV_VAR, "0.2")
    with pytest.raises(si.SkillsInstallError) as exc_info:
        si.claude_runtime_incantation(repo)
    assert "did not answer within" in str(exc_info.value)


def test_shell_single_quoting_survives_an_embedded_quote() -> None:
    quoted = si._shell_single_quoted("it's here")
    assert quoted == "'it'\\''s here'"


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------


def test_global_skills_dir_refuses_an_unknown_agent(home: Path) -> None:
    with pytest.raises(si.SkillsInstallError) as exc_info:
        si.global_skills_dir(home, "bogus-agent")
    assert "bogus-agent" in str(exc_info.value)


@pytest.mark.parametrize("scope", [si.SCOPE_GLOBAL, si.SCOPE_PROJECT, si.SCOPE_RUNTIME])
@pytest.mark.parametrize("verb", [si.install, si.remove, si.report])
def test_invalid_agent_is_refused_with_the_valid_set(
    repo: Path, home: Path, verb: Callable[[Path, Path, str, str], list[str]], scope: str
) -> None:
    with pytest.raises(si.SkillsInstallError) as exc_info:
        verb(repo, home, "bogus-agent", scope)
    message = str(exc_info.value)
    assert "bogus-agent" in message
    for valid in si.AGENT_CHOICES:
        assert valid in message


@pytest.mark.parametrize("agent", [si.AGENT_OPENCODE, si.AGENT_CLAUDE, si.AGENT_BOTH])
@pytest.mark.parametrize("verb", [si.install, si.remove, si.report])
def test_invalid_scope_is_refused_with_the_valid_set(
    repo: Path, home: Path, verb: Callable[[Path, Path, str, str], list[str]], agent: str
) -> None:
    with pytest.raises(si.SkillsInstallError) as exc_info:
        verb(repo, home, agent, "bogus-scope")
    message = str(exc_info.value)
    assert "bogus-scope" in message
    for valid in si.SCOPES:
        assert valid in message
