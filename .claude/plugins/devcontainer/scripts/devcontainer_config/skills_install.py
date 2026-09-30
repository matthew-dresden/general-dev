"""Make-driven skill-surface wiring for the canonical `.agents/skills` home.

The canonical skills home is `.agents/skills/<name>/SKILL.md` (every name
`gd-`-prefixed); opencode reads it natively and Claude Code consumes it
through the plugin's `skills/` relative symlink. This module is the engine
behind the `make skills-install`, `make skills-remove` and `make skills-list`
targets: it wires, unwires and reports an agent's route to that one home,
on the native Mac, with no devcontainer involvement.

Everything here is a pointer operation, never a copy: installing creates a
symlink into an agent's skill directory, removing deletes only a symlink
whose resolved target is inside this repository, and listing reads the
filesystem. A copied skill would be a second source of truth, which is the
failure this roster layout exists to prevent, so no code path writes a
`SKILL.md` anywhere.

Three scopes, one meaning each:

- `global` -- the user-level skill directory (`~/.config/opencode/skills`
  for opencode, `~/.claude/skills` for Claude Code). `install` creates one
  symlink named `general-dev-skills` in each agent's directory, pointing at
  THIS checkout's `.agents/skills` with an absolute target -- fine for a
  Mac-personal install, and recorded in the install message and by
  `list`. `install` refuses any existing entry of that name which is not
  already our symlink. `remove` deletes only a link whose resolved target
  is this repository's skills home: a non-symlink is refused, and a symlink
  resolving elsewhere is refused, so the user's own skills (aws-secrets,
  google-workspace, onepassword-secrets and anything else) can never be
  deleted by a target aimed at this repository. Sibling entries are never
  touched: every operation addresses exactly the one named path.
- `project` -- the repository itself. The in-repo adapters already exist
  for both agents (opencode's `.agents/skills` is native; Claude Code's
  `.claude/plugins/devcontainer/skills` relative symlink is tracked
  content), so this scope verifies and, when the Claude-side link is
  missing, wires it with the documented relative target
  (`../../../.agents/skills`, the form `tests/test_skills_symlink.py`
  pins). It is never removed by `remove`: the adapters are versioned
  repository content, so unwiring them is a deliberate git change, not an
  uninstall.
- `runtime` -- no filesystem change at all. `install` prints the agent's
  one-shot incantation and nothing else. For opencode that is an
  `OPENCODE_CONFIG` incantation whose JSON carries a `skills.paths`
  override; opencode's override config REPLACES the project config rather
  than merging with it, so the printed JSON embeds the provider/model block
  read from `.devcontainer/opencode.json` at generation time -- a bare
  `{"skills": ...}` file would launch opencode with no provider at all.
  For Claude Code that is a `--settings` one-shot (a settings JSON enabling
  this checkout's plugin marketplace path), printed only when this Claude
  Code build advertises the flag; `claude --help` is read at call time to
  decide, and a build without it gets an explicit unsupported message with
  the manual fallback. Neither incantation is ever executed here: this
  module prints, the operator decides.

Like every module in this package except `cli`, nothing here exits a
process: every refusal raises `SkillsInstallError` and the caller decides.
The unit suite drives this module hermetically through its explicit `root`
and `home` parameters -- there is no `Path.home()` or repository discovery
inside the engine -- so no test patches globals or touches a real home
directory.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from devcontainer_config.hostprobe import HostProbeError, read_positive_seconds

# The symlink name this module owns inside every global agent skill
# directory. One name, declared once: `install` refuses to reuse it for a
# different target, and `remove` refuses to delete it for a different
# target, so the name doubles as the ownership mark.
SKILL_LINK_NAME: str = "general-dev-skills"

AGENT_OPENCODE: str = "opencode"
AGENT_CLAUDE: str = "claude"
AGENT_BOTH: str = "both"

# The agents this repository ships wiring for, in the order report output
# renders them; `both` is the make-layer default, not a third agent.
AGENTS: tuple[str, ...] = (AGENT_OPENCODE, AGENT_CLAUDE)
AGENT_CHOICES: tuple[str, ...] = (*AGENTS, AGENT_BOTH)

SCOPE_GLOBAL: str = "global"
SCOPE_PROJECT: str = "project"
SCOPE_RUNTIME: str = "runtime"
SCOPES: tuple[str, ...] = (SCOPE_GLOBAL, SCOPE_PROJECT, SCOPE_RUNTIME)

# The opencode project config whose provider/model block the runtime
# incantation embeds (see the module docstring's replace-semantics note).
OPENCODE_PROJECT_CONFIG_RELATIVE = ".devcontainer/opencode.json"

# The in-repo Claude Code plugin directory: the marketplace path the
# runtime settings JSON enables, and the parent of the tracked `skills/`
# symlink the project scope verifies.
CLAUDE_MARKETPLACE_RELATIVE = ".claude/plugins/devcontainer"

# The documented relative target of the plugin's `skills/` symlink, spelled
# exactly as `tests/test_skills_symlink.py` pins it, so project-scope wiring
# can never create an absolute (checkout-path-dependent) link.
PLUGIN_SKILLS_RELATIVE_TARGET = "../../../.agents/skills"

# `--settings` is the one-shot settings flag the Claude Code runtime
# incantation relies on. Support is decided per invocation by reading
# `claude --help`, never assumed: a build without the flag gets the
# explicit unsupported message instead of an incantation that would fail.
CLAUDE_SETTINGS_FLAG = "--settings"
CLAUDE_EXECUTABLE = "claude"

# Bounds the `claude --help` read (a local binary printing and exiting,
# never a network call). Read fresh on every call through
# `hostprobe.read_positive_seconds`, the single shared reader this
# variable's name and default resolve against, per CLAUDE.md's
# no-hardcoded-timeouts rule.
CLAUDE_HELP_TIMEOUT_ENV_VAR = "SKILLS_CLAUDE_HELP_TIMEOUT_SECONDS"
CLAUDE_HELP_TIMEOUT_DEFAULT_SECONDS = 10.0


class SkillsInstallError(RuntimeError):
    """Raised when a skill-surface operation refuses or cannot proceed.

    Every message names the path or value at fault and the remedy, in the
    `ERROR:` house style, so a make-target run fails with an actionable
    line instead of a bare exception.
    """


def _create_symlink(link: Path, target: str) -> None:
    """Create `link` -> `target`, refusing with the OS reason on failure.

    The one creation site for both install scopes, so an unwritable
    directory or a regular file occupying a parent path surfaces as this
    module's own actionable error instead of a raw `OSError` traceback.
    """
    try:
        link.symlink_to(target)
    except OSError as exc:
        raise SkillsInstallError(
            f"ERROR: creating {link} -> {target} failed ({exc})\n"
            "A parent path may be a regular file, or the location may not be writable.\n"
            "Inspect the path by hand, then re-run."
        ) from exc


def canonical_skills_root(root: Path) -> Path:
    """The checkout's canonical, agent-agnostic skills home."""
    return root / ".agents" / "skills"


def plugin_skills_link(root: Path) -> Path:
    """The tracked plugin-side route Claude Code reads the roster through."""
    return root / CLAUDE_MARKETPLACE_RELATIVE / "skills"


def global_skills_dir(home: Path, agent: str) -> Path:
    """The user-level skill directory of `agent` under an explicit `home`."""
    if agent == AGENT_OPENCODE:
        return home / ".config" / "opencode" / "skills"
    if agent == AGENT_CLAUDE:
        return home / ".claude" / "skills"
    raise SkillsInstallError(_unknown_agent_message(agent))


def _unknown_agent_message(agent: str) -> str:
    """The refusal for an agent value outside `AGENT_CHOICES`, valid set included."""
    return (
        f"ERROR: unknown agent {agent!r}\n"
        f"Valid values: {', '.join(AGENT_CHOICES)}.\n"
        "Pass AGENT=<value> to the make target or --agent <value> to the cli, then retry."
    )


def _unknown_scope_message(scope: str) -> str:
    """The refusal for a scope value outside `SCOPES`, valid set included."""
    return (
        f"ERROR: unknown scope {scope!r}\n"
        f"Valid values: {', '.join(SCOPES)}.\n"
        "Pass SCOPE=<value> to the make target or --scope <value> to the cli, then retry."
    )


def _selected_agents(agent: str) -> tuple[str, ...]:
    """`AGENTS` for `both`, the one agent otherwise; anything else is a refusal."""
    if agent == AGENT_BOTH:
        return AGENTS
    if agent in AGENTS:
        return (agent,)
    raise SkillsInstallError(_unknown_agent_message(agent))


def _require_scope(scope: str) -> None:
    """Fail fast on a scope value outside `SCOPES` before any path is touched."""
    if scope not in SCOPES:
        raise SkillsInstallError(_unknown_scope_message(scope))


def _lexists(path: Path) -> bool:
    """Whether `path` names anything at all, a broken symlink included.

    `Path.exists()` follows the link and answers False for a symlink whose
    target is gone; an install must treat a stale link exactly like a live
    one -- an occupied name to inspect and refuse or accept, never a free
    slot to create over.
    """
    return path.is_symlink() or path.exists()


def _shell_single_quoted(text: str) -> str:
    """`text` quoted for a shell single-quoted word, embedded quotes included.

    The runtime incantations are printed for the operator to paste into a
    shell, so a value containing a single quote (an apostrophe in a
    checkout path, say) must not terminate the word early. The
    `'\''` idiom closes the word, escapes the quote, and reopens it.
    """
    return "'" + text.replace("'", "'\\''") + "'"


def _install_global(root: Path, home: Path, agent: str) -> str:
    """The one global-scope install for `agent`, idempotent and refusing.

    The target is recorded absolute (the module docstring explains why that
    is acceptable for a Mac-personal install); an existing name is accepted
    only when it is already our symlink, so a directory or a foreign link
    of the same name is named in the refusal instead of being replaced.
    """
    target = canonical_skills_root(root)
    if not target.is_dir():
        raise SkillsInstallError(
            f"ERROR: the canonical skills home {target} does not exist\n"
            f"{SKILL_LINK_NAME} would point at nothing. Run this from inside the "
            "repository checkout that carries .agents/skills, then retry."
        )
    link_dir = global_skills_dir(home, agent)
    try:
        link_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise SkillsInstallError(
            f"ERROR: cannot create the skills directory {link_dir}\n"
            f"A file may occupy the path. Reason: {exc}"
        ) from exc
    link = link_dir / SKILL_LINK_NAME
    if _lexists(link):
        if link.is_symlink():
            resolved = link.resolve()
            if resolved == target.resolve():
                return f"{agent} global: already installed ({link} -> {resolved}); nothing changed"
            raise SkillsInstallError(
                f"ERROR: {link} already exists and points at {resolved}, "
                "not this repository's skills home\n"
                f"install never replaces an entry named {SKILL_LINK_NAME} that it did not "
                "create. If that entry is genuinely yours, move or delete it by hand, "
                "then re-run."
            )
        raise SkillsInstallError(
            f"ERROR: {link} exists and is not a symlink\n"
            f"install never overwrites a real file or directory named {SKILL_LINK_NAME}. "
            "If it is genuinely yours, move or delete it by hand, then re-run."
        )
    _create_symlink(link, str(target))
    return f"{agent} global: installed {link} -> {target} (absolute target recorded)"


def _install_project(root: Path, agent: str) -> str:
    """The one project-scope install for `agent`: verify, wiring where absent."""
    canonical = canonical_skills_root(root).resolve()
    if agent == AGENT_OPENCODE:
        return (
            f"{AGENT_OPENCODE} project: native -- opencode reads {canonical} "
            "directly; nothing to wire"
        )
    link = plugin_skills_link(root)
    if _lexists(link):
        if link.is_symlink() and link.resolve() == canonical:
            return f"{AGENT_CLAUDE} project: already wired ({link} resolves to {canonical})"
        raise SkillsInstallError(
            f"ERROR: {link} exists but is not the documented relative symlink to "
            f"{canonical}\n"
            "A real directory there would be a second, drifting copy of the roster, and "
            "a mispointed symlink would feed Claude Code someone else's skills.\n"
            "Replace it with a symlink whose target string is "
            f"{PLUGIN_SKILLS_RELATIVE_TARGET!r} (git tracks that form), then re-run."
        )
    _create_symlink(link, PLUGIN_SKILLS_RELATIVE_TARGET)
    if link.resolve() != canonical:
        raise SkillsInstallError(
            f"ERROR: wiring {link} did not resolve to {canonical}\n"
            "The relative symlink was created but does not resolve from this checkout; "
            "the filesystem layout under .claude/plugins may have moved.\n"
            "Inspect the path by hand, then re-run."
        )
    return f"{AGENT_CLAUDE} project: wired {link} -> resolves to {canonical}"


def _remove_global(root: Path, home: Path, agent: str) -> str:
    """The one global-scope removal for `agent`, deleting only our own link.

    Three refusals protect the operator's own skills: an absent name is
    nothing-to-remove (never created-then-removed), a non-symlink is never
    deleted, and a symlink resolving outside this repository is left
    untouched -- it is someone else's entry, however it came to carry this
    module's link name.
    """
    link = global_skills_dir(home, agent) / SKILL_LINK_NAME
    if not _lexists(link):
        return f"{agent} global: not installed ({link} absent); nothing to remove"
    if not link.is_symlink():
        raise SkillsInstallError(
            f"ERROR: {link} is not a symlink; refusing to delete it\n"
            "remove unwires pointers, never deletes real files or directories."
        )
    resolved = link.resolve()
    if resolved != canonical_skills_root(root).resolve():
        raise SkillsInstallError(
            f"ERROR: {link} resolves to {resolved}, which is not this repository's "
            "skills home\n"
            "remove deletes only links pointing inside this repository; this entry "
            "belongs to something else (a personal skill such as aws-secrets, perhaps) "
            "and is left untouched."
        )
    link.unlink()
    return f"{agent} global: removed {link} (resolved target {resolved}); canonical home untouched"


def _remove_project(agent: str) -> str:
    """The project-scope removal refusal: the adapters are tracked content."""
    if agent == AGENT_OPENCODE:
        return f"{AGENT_OPENCODE} project: native -- there is no wiring to remove"
    return (
        f"{AGENT_CLAUDE} project: the plugin skills symlink is tracked repository "
        "content (.claude/plugins/devcontainer/skills), so unwiring it is a git "
        "change to review, not an uninstall; nothing was removed"
    )


def _remove_runtime(agent: str) -> str:
    """The runtime-scope removal note: an incantation leaves no state behind."""
    return (
        f"{agent} runtime: the one-shot incantation changes no filesystem state, "
        f"so there is nothing to remove; `make skills-install SCOPE={SCOPE_RUNTIME} "
        f"AGENT={agent}` prints it again"
    )


def _report_global(root: Path, home: Path, agent: str) -> str:
    """The global-scope state line for `agent`: installed (target) or not."""
    link = global_skills_dir(home, agent) / SKILL_LINK_NAME
    if not _lexists(link):
        return f"{agent} global: not installed"
    if not link.is_symlink():
        raise SkillsInstallError(
            f"ERROR: {link} exists and is not a symlink; refusing to report it as "
            "installed\n"
            "Move or delete it by hand if it is genuinely yours, then re-run."
        )
    resolved = link.resolve()
    if resolved != canonical_skills_root(root).resolve():
        raise SkillsInstallError(
            f"ERROR: {link} resolves to {resolved}, not this repository's skills home\n"
            "Something else occupies this module's link name; inspect it by hand "
            "before removing or replacing it."
        )
    return f"{agent} global: installed ({link} -> {resolved})"


def _report_project(root: Path, agent: str) -> str:
    """The project-scope state line for `agent`: native, wired, or not wired."""
    canonical = canonical_skills_root(root).resolve()
    if agent == AGENT_OPENCODE:
        return f"{AGENT_OPENCODE} project: native ({canonical})"
    link = plugin_skills_link(root)
    if not _lexists(link):
        return f"{AGENT_CLAUDE} project: not wired ({link} absent)"
    if not link.is_symlink() or link.resolve() != canonical:
        raise SkillsInstallError(
            f"ERROR: {link} is not the documented relative symlink to {canonical}\n"
            "A real directory there is a fork of the roster; a mispointed symlink "
            "feeds Claude Code the wrong skills.\n"
            "Replace it with a symlink whose target string is "
            f"{PLUGIN_SKILLS_RELATIVE_TARGET!r}, then re-run."
        )
    return f"{AGENT_CLAUDE} project: wired ({link} -> {canonical})"


def install(root: Path, home: Path, agent: str, scope: str) -> list[str]:
    """Wire `agent`'s route to the canonical skills home at `scope`.

    `global` creates the `general-dev-skills` symlink in each selected
    agent's user-level skill directory; `project` verifies (and, for
    Claude Code, wires) the in-repo adapters; `runtime` prints the one-shot
    incantations and touches nothing. Every returned line is a report for
    the caller to print; every refusal raises `SkillsInstallError`.
    """
    _require_scope(scope)
    return [
        message
        for one_agent in _selected_agents(agent)
        for message in _install_one(root, home, one_agent, scope)
    ]


def _install_one(root: Path, home: Path, agent: str, scope: str) -> list[str]:
    """The per-agent messages for one install call, dispatching on scope."""
    if scope == SCOPE_RUNTIME:
        return [_runtime_incantation(root, agent)]
    if scope == SCOPE_PROJECT:
        return [_install_project(root, agent)]
    return [_install_global(root, home, agent)]


def remove(root: Path, home: Path, agent: str, scope: str) -> list[str]:
    """Unwire `agent`'s route to the canonical skills home at `scope`.

    Only `global` deletes anything, and only a symlink resolving inside
    this repository; `project` and `runtime` report why they leave the
    filesystem exactly as found.
    """
    _require_scope(scope)
    return [
        message
        for one_agent in _selected_agents(agent)
        for message in _remove_one(root, home, one_agent, scope)
    ]


def _remove_one(root: Path, home: Path, agent: str, scope: str) -> list[str]:
    """The per-agent messages for one remove call, dispatching on scope."""
    if scope == SCOPE_RUNTIME:
        return [_remove_runtime(agent)]
    if scope == SCOPE_PROJECT:
        return [_remove_project(agent)]
    return [_remove_global(root, home, agent)]


def report(root: Path, home: Path, agent: str, scope: str) -> list[str]:
    """The state lines for `agent` at `scope`: installed, native, or not installed.

    Read-only, like `remove`'s no-op scopes: `runtime` has no persistent
    state to list and says where the incantation is printed instead.
    A name occupied by anything other than our symlink raises rather than
    being silently rendered as an installed state.
    """
    _require_scope(scope)
    return [
        message
        for one_agent in _selected_agents(agent)
        for message in _report_one(root, home, one_agent, scope)
    ]


def _report_one(root: Path, home: Path, agent: str, scope: str) -> list[str]:
    """The per-agent messages for one report call, dispatching on scope."""
    if scope == SCOPE_RUNTIME:
        return [
            f"{agent} runtime: no persistent state; `make skills-install "
            f"SCOPE={SCOPE_RUNTIME} AGENT={agent}` prints the one-shot incantation"
        ]
    if scope == SCOPE_PROJECT:
        return [_report_project(root, agent)]
    return [_report_global(root, home, agent)]


def _read_claude_help() -> str:
    """The live `claude --help` text, bounded by an env-configurable timeout.

    The one subprocess in this module, read only on the runtime-claude path
    when the caller supplies no help text: support for the `--settings`
    one-shot is decided against the installed build, never assumed from a
    version string. A missing binary or a timeout is a refusal naming the
    remedy, not a silent fallback to the global install.
    """
    timeout = _claude_help_timeout_seconds()
    try:
        completed = subprocess.run(
            [CLAUDE_EXECUTABLE, "--help"],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise SkillsInstallError(
            f"ERROR: {CLAUDE_EXECUTABLE} is not on PATH\n"
            "The runtime scope decides Claude Code one-shot support by reading "
            f"'{CLAUDE_EXECUTABLE} --help', which needs the binary.\n"
            "Install Claude Code and ensure it is on PATH, or install globally "
            "with `make skills-install AGENT=claude SCOPE=global`."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise SkillsInstallError(
            f"ERROR: '{CLAUDE_EXECUTABLE} --help' did not answer within {timeout:g}s\n"
            f"This module bounds the help read via {CLAUDE_HELP_TIMEOUT_ENV_VAR}.\n"
            "Investigate why the Claude Code binary is slow to start, raise "
            f"{CLAUDE_HELP_TIMEOUT_ENV_VAR}, or install globally with "
            "`make skills-install AGENT=claude SCOPE=global`."
        ) from exc
    return completed.stdout + completed.stderr


def _claude_help_timeout_seconds() -> float:
    """The deadline `_read_claude_help` gives its help read, read fresh per call.

    Delegates to `hostprobe.read_positive_seconds` and re-raises its
    `HostProbeError` as `SkillsInstallError`, so no foreign exception type
    crosses this module's boundary (the `repo.py` pattern).
    """
    try:
        return read_positive_seconds(
            CLAUDE_HELP_TIMEOUT_ENV_VAR, CLAUDE_HELP_TIMEOUT_DEFAULT_SECONDS
        )
    except HostProbeError as exc:
        raise SkillsInstallError(
            f"ERROR: {exc}\n"
            f"Set {CLAUDE_HELP_TIMEOUT_ENV_VAR} to a positive number of seconds, "
            f"or unset it to use the default of {CLAUDE_HELP_TIMEOUT_DEFAULT_SECONDS:g}."
        ) from exc


def opencode_runtime_incantation(root: Path) -> str:
    """The printable `OPENCODE_CONFIG` one-shot for opencode; never executed.

    The printed snippet writes the override JSON under `$TMPDIR` (a shell
    expansion at operator run time -- no path is decided here) and exports
    `OPENCODE_CONFIG` for one launch. The JSON is the project config's own
    content read at generation time PLUS the `skills.paths` override,
    because an `OPENCODE_CONFIG` file replaces the project config rather
    than merging with it: shipping only the skills override would launch
    opencode with no provider or model at all. `.agents/skills` is
    opencode's native home, so the runtime scope exists for an opencode
    that runs outside this checkout.
    """
    project_config_path = root / OPENCODE_PROJECT_CONFIG_RELATIVE
    if not project_config_path.is_file():
        raise SkillsInstallError(
            f"ERROR: {project_config_path} does not exist\n"
            "The runtime incantation embeds the provider/model block read from "
            "the project config, because an OPENCODE_CONFIG override replaces it "
            "rather than merging.\n"
            "Create it (make init renders the tree's examples), then re-run."
        )
    try:
        project_config: dict[str, object] = json.loads(
            project_config_path.read_text(encoding="utf-8")
        )
    except json.JSONDecodeError as exc:
        raise SkillsInstallError(
            f"ERROR: {project_config_path} is not valid JSON ({exc.msg})\n"
            "The runtime incantation embeds this file's content verbatim plus the "
            "skills.paths override, so it must parse.\n"
            "Fix the JSON, then re-run."
        ) from exc
    override = dict(project_config)
    override["skills"] = {"paths": [str(canonical_skills_root(root).resolve())]}
    payload = json.dumps(override, indent=2)
    override_name = "general-dev-opencode-skills.json"
    return (
        "# One-shot runtime surface for opencode; this prints, it never executes.\n"
        f"# OPENCODE_CONFIG replaces the project config, so the JSON below embeds the\n"
        f"# provider/model block read from {OPENCODE_PROJECT_CONFIG_RELATIVE} at\n"
        "# generation time plus the skills.paths override pointing at this checkout.\n"
        f"cat > \"$TMPDIR/{override_name}\" <<'JSON'\n"
        f"{payload}\n"
        "JSON\n"
        f'OPENCODE_CONFIG="$TMPDIR/{override_name}" opencode\n'
    )


def claude_runtime_incantation(root: Path, claude_help_text: str | None = None) -> str:
    """The printable `--settings` one-shot for Claude Code; never executed.

    The settings JSON mirrors the project-scope wiring with this checkout's
    absolute marketplace path, with both names read from the plugin tree's
    own `marketplace.json` rather than restated here. When the installed
    build does not advertise `--settings` (decided from `claude --help`,
    injectable as `claude_help_text` so tests stay hermetic), the message
    says so explicitly and gives the manual fallback instead of an
    incantation that would fail.
    """
    help_text = _read_claude_help() if claude_help_text is None else claude_help_text
    marketplace_dir = root / CLAUDE_MARKETPLACE_RELATIVE
    if CLAUDE_SETTINGS_FLAG not in help_text:
        return (
            f"claude runtime: UNSUPPORTED -- this Claude Code build does not "
            f"advertise {CLAUDE_SETTINGS_FLAG}\n"
            f"'{CLAUDE_EXECUTABLE} --help' names no {CLAUDE_SETTINGS_FLAG} flag, so "
            "there is no one-shot settings override to print.\n"
            "Manual fallback: either install globally with "
            "`make skills-install AGENT=claude SCOPE=global`, or add this "
            "checkout's plugin to ~/.claude/settings.json by hand --\n"
            f'  "extraKnownMarketplaces": {{"<marketplace>": {{"source": '
            f'{{"source": "directory", "path": "{marketplace_dir}"}}}}}}\n'
            '  "enabledPlugins": {"<plugin>@<marketplace>": true}\n'
            "using the names from the plugin tree's .claude-plugin/marketplace.json."
        )
    marketplace_name, plugin_name = _marketplace_names(marketplace_dir)
    settings = {
        "extraKnownMarketplaces": {
            marketplace_name: {"source": {"source": "directory", "path": str(marketplace_dir)}}
        },
        "enabledPlugins": {f"{plugin_name}@{marketplace_name}": True},
    }
    payload = json.dumps(settings)
    return (
        "# One-shot runtime surface for Claude Code; this prints, it never executes.\n"
        f"# Verified against '{CLAUDE_EXECUTABLE} --help': {CLAUDE_SETTINGS_FLAG} accepts "
        "a settings JSON string or file.\n"
        f"claude {CLAUDE_SETTINGS_FLAG} {_shell_single_quoted(payload)}\n"
    )


def _marketplace_names(marketplace_dir: Path) -> tuple[str, str]:
    """`(marketplace name, plugin name)` from the plugin tree's own manifest.

    Read, never restated: `.claude/settings.json` already carries both
    spellings for the project scope, and a third hardcoded copy here would
    drift the moment the plugin is renamed.
    """
    manifest_path = marketplace_dir / ".claude-plugin" / "marketplace.json"
    if not manifest_path.is_file():
        raise SkillsInstallError(
            f"ERROR: {manifest_path} does not exist\n"
            "The runtime settings JSON takes both the marketplace and plugin name "
            "from this manifest, the same source .claude/settings.json reads.\n"
            "Run this from the repository checkout that carries the plugin, then retry."
        )
    try:
        manifest: object = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SkillsInstallError(
            f"ERROR: {manifest_path} is not valid JSON ({exc.msg})\n"
            "The runtime settings JSON takes both the marketplace and plugin name "
            "from this manifest.\n"
            "Fix the manifest, then re-run."
        ) from exc
    if not isinstance(manifest, dict):
        raise SkillsInstallError(
            f"ERROR: {manifest_path} does not hold a JSON object\n"
            "The runtime settings JSON needs the manifest's 'name' and "
            "'plugins[0].name' fields.\n"
            "Fix the manifest, then re-run."
        )
    name = manifest.get("name")
    plugins = manifest.get("plugins")
    first_plugin = plugins[0] if isinstance(plugins, list) and plugins else None
    plugin_name = first_plugin.get("name") if isinstance(first_plugin, dict) else None
    if not isinstance(name, str) or not isinstance(plugin_name, str):
        raise SkillsInstallError(
            f"ERROR: {manifest_path} does not carry string 'name' and "
            "'plugins[0].name' fields\n"
            "The runtime settings JSON needs both names verbatim.\n"
            "Fix the manifest, then re-run."
        )
    return name, plugin_name


def _runtime_incantation(root: Path, agent: str) -> str:
    """The one runtime-scope message for `agent`, dispatching on the agent."""
    if agent == AGENT_OPENCODE:
        return opencode_runtime_incantation(root)
    return claude_runtime_incantation(root)


__all__ = [
    "AGENT_BOTH",
    "AGENT_CHOICES",
    "AGENT_CLAUDE",
    "AGENT_OPENCODE",
    "AGENTS",
    "CLAUDE_HELP_TIMEOUT_DEFAULT_SECONDS",
    "CLAUDE_HELP_TIMEOUT_ENV_VAR",
    "CLAUDE_MARKETPLACE_RELATIVE",
    "CLAUDE_SETTINGS_FLAG",
    "PLUGIN_SKILLS_RELATIVE_TARGET",
    "SCOPES",
    "SKILL_LINK_NAME",
    "SCOPE_GLOBAL",
    "SCOPE_PROJECT",
    "SCOPE_RUNTIME",
    "SkillsInstallError",
    "canonical_skills_root",
    "claude_runtime_incantation",
    "global_skills_dir",
    "install",
    "opencode_runtime_incantation",
    "plugin_skills_link",
    "remove",
    "report",
]
