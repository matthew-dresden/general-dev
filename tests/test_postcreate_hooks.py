"""Tests for the container half of git hook installation (spec Section 4.6, AC-4.7).

`.devcontainer/.devcontainer.postcreate.sh` is asserted on as text here, never
executed: the script's very first non-comment lines require a real
`CONTAINER_USER` resolvable through `getent passwd`, which the hermetic suite
cannot guarantee, and Section 10.2 says outright that this suite cannot start
docker. What AC-4.7 actually cares about -- that the container installs hooks
through the identical `Makefile` entry point the host uses, so the two
cannot render different content -- is a property of the script's text, not
of running it, so every assertion below is a text-level consistency check.
A live container build, the only way to observe the hooks actually land in
`.git/hooks`, is covered separately by the live suites; this suite does not
attempt one (see the work unit's Approach, "Verification is a consistency
test rather than a live container build").

Nothing here needs the module-body-deferred-import discipline
`tests/test_githooks.py`'s docstring documents: this task's Changes Manifest
touches no Python source file, so the TDD RED gate's stash of this unit's
own production files never touches `devcontainer_config`, the same reasoning
`tests/test_makefile_contract.py` already relies on for its own top-level
`from devcontainer_config.repo import find_root`.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import (
    _function_body,
    _postcreate_text,
    _provisioning_flow_required_steps_prose,
    _provisioning_flow_table,
)
from devcontainer_config.githooks import HOOK_NAMES, hook_body
from devcontainer_config.repo import find_root

_FORGIVING_SUFFIXES: tuple[str, ...] = ("|| true", "|| :", "; true", "2>/dev/null", "> /dev/null")


def _repo_root() -> Path:
    """The repository root, resolved from this test file's own location."""
    return find_root(Path(__file__).resolve().parent)


def _makefile_text() -> str:
    """The repository root `Makefile`, read fresh for every call."""
    return (_repo_root() / "Makefile").read_text(encoding="utf-8")


def _hooks_install_target() -> str:
    """The `Makefile` target name the host uses to install hooks.

    Parsed rather than hard-coded (AC-TEST-001): a rename of the target in
    the `Makefile` would silently desync a literal repeated here instead of
    failing this test.
    """
    match = re.search(r"^(hooks-install):", _makefile_text(), re.MULTILINE)
    assert match is not None, "no hooks-install target found in Makefile"
    return match.group(1)


def _git_hooks_step() -> str:
    """The `install_git_hooks` function body from `.devcontainer.postcreate.sh`.

    Isolated by name so the fatal-on-failure and workspace-naming assertions
    below examine only the step this task adds, not some unrelated step that
    happens to contain similar text elsewhere in the script. Delegates to
    the shared `conftest._function_body` brace-depth scanner (AC-REPL-001)
    rather than a local regex that stopped at the first line beginning with
    a literal `}`.
    """
    return _function_body("install_git_hooks")


def _main_body() -> str:
    """The `main()` function body from `.devcontainer.postcreate.sh`.

    Isolated so step-order assertions read the actual call sequence rather
    than any incidental ordering of the step functions' own definitions.
    Delegates to the shared `conftest._function_body` brace-depth scanner
    (AC-REPL-001) rather than a local regex that stopped at the first line
    beginning with a literal `}`.
    """
    return _function_body("main")


def test_function_body_includes_text_after_a_nested_brace_group() -> None:
    """AC-CONF-003: proves the brace-depth scanner is correct where the
    superseded first-closing-brace regex was wrong.

    The synthetic function below contains a nested `{ ... }` compound
    command whose own closing brace sits at the start of its own line -- exactly
    the shape the superseded `r"^name\\(\\)\\s*\\{(.*?)^\\}"` pattern (run
    with `re.MULTILINE | re.DOTALL`) matched as if it were the function's
    own closing brace, because that pattern is anchored on '^\\}' (the
    first line that *begins* with '}'), not on the first '{' that opened
    the function actually returning to brace depth zero. A brace-depth
    scan is not fooled: it counts every '{' the nested group opens against
    every '}' it closes, and only stops once the running depth returns to
    zero at the function's actual closing brace, so the text following the
    nested group survives extraction.
    """
    synthetic_script = "outer() {\n  {\necho nested\n}\n  echo after_nested\n}\n"
    body = _function_body("outer", text=synthetic_script)
    assert "echo after_nested" in body, (
        "the scanner stopped at the nested group's own closing brace instead of "
        "scanning to the function's actual closing brace"
    )


def test_postcreate_keeps_set_euo_pipefail() -> None:
    """The step relies on the script's own failure semantics (Section 7.1), not its own."""
    assert "set -euo pipefail" in _postcreate_text()


def test_postcreate_defines_a_git_hooks_step() -> None:
    """AC-FUNC-001: the postCreate script defines a hook-installation step."""
    assert "install_git_hooks" in _postcreate_text()


def test_git_hooks_step_is_called_from_main() -> None:
    """AC-FUNC-001 / AC-FUNC-002: main() actually invokes the step, not just defines it."""
    assert re.search(r"^\s*install_git_hooks\s*$", _main_body(), re.MULTILINE)


def test_git_hooks_step_invokes_the_host_installation_target() -> None:
    """AC-FUNC-001 / AC-TEST-001: the step runs the same target the host uses."""
    target = _hooks_install_target()
    assert re.search(rf"\bmake\s+{re.escape(target)}\b", _git_hooks_step())


def test_git_hooks_step_uses_the_shared_printers() -> None:
    """AC-FUNC-002 / AC-3.1: reports through log_section / log_section_done, no new printer."""
    step = _git_hooks_step()
    assert re.search(r"\blog_section\b", step)
    assert re.search(r"\blog_section_done\b", step)


def test_git_hooks_step_does_not_swallow_a_non_zero_status() -> None:
    """AC-TEST-002: nothing discards the installation command's exit status."""
    step = _git_hooks_step()
    for suffix in _FORGIVING_SUFFIXES:
        assert suffix not in step, f"{suffix!r} would swallow a non-zero status"


def test_git_hooks_step_propagates_the_install_command_failure() -> None:
    """AC-TEST-002: the install command's own failure reaches an explicit handler."""
    step = _git_hooks_step()
    target = _hooks_install_target()
    install_idx = step.index(f"make {target}")
    tail = step[install_idx : install_idx + 200]
    assert "||" in tail, "the install command's failure is not wired to a handler"


def test_git_hooks_step_aborts_through_exit_with_error_naming_the_workspace() -> None:
    """AC-FUNC-003: a failing install names the workspace and the rerun command."""
    step = _git_hooks_step()
    target = _hooks_install_target()
    install_idx = step.index(f"make {target}")
    error_idx = step.index("exit_with_error", install_idx)
    handler = step[error_idx:]
    assert "${WORK_DIR}" in handler
    assert f"make {target}" in handler


def test_git_hooks_step_checks_for_the_git_directory_before_installing() -> None:
    """AC-FUNC-004: no .git directory aborts, naming the resolved workspace path."""
    step = _git_hooks_step()
    target = _hooks_install_target()
    git_dir_check = '"${WORK_DIR}/.git"'
    assert git_dir_check in step
    guard_idx = step.index(git_dir_check)
    install_idx = step.index(f"make {target}")
    assert guard_idx < install_idx, "the .git check must run before the install command"
    error_idx = step.index("exit_with_error", guard_idx)
    assert error_idx < install_idx, "the .git guard must reach exit_with_error before installing"
    handler = step[error_idx:install_idx]
    assert "${WORK_DIR}" in handler


def test_git_hooks_step_restores_container_user_ownership_after_install() -> None:
    """Ownership invariant: postCreate runs as root (sudo, postcreate-wrapper.sh), so
    `make hooks-install` would otherwise leave `.git/hooks/pre-commit` and
    `.git/hooks/pre-push` root-owned inside a workspace that belongs to
    `CONTAINER_USER`. Every other root-side workspace writer in this script
    restores ownership explicitly (`declare_unclaimed_path` chowns
    `.gitmodules`, `configure_repo_detection` chowns `REPOS_PATH`); this step
    must follow the same pattern for the hooks it installs.
    """
    step = _git_hooks_step()
    target = _hooks_install_target()
    install_idx = step.index(f"make {target}")
    chown_idx = step.index("chown", install_idx)
    assert chown_idx > install_idx, "the chown must run after the install command"
    tail = step[chown_idx:]
    assert '"${CONTAINER_USER}:${CONTAINER_USER}"' in tail
    assert ".git/hooks" in tail


def test_postcreate_contains_no_hook_body_text() -> None:
    """AC-FUNC-006 / AC-TEST-005: the container renders content from githooks, not a copy."""
    text = _postcreate_text()
    for hook_name in HOOK_NAMES:
        assert hook_body(hook_name) not in text


def test_host_and_container_share_one_hook_body_renderer() -> None:
    """AC-TEST-004: the host's target and the container's target are the same one.

    Both the `Makefile`'s `hooks-install` recipe and the container's step
    invoke the identical target name; since that target is the only place
    either installs hooks from -- and it delegates to
    `devcontainer_config.cli`, which renders content through
    `devcontainer_config.githooks.hook_body` -- there is exactly one
    renderer either install path can reach.
    """
    target = _hooks_install_target()
    makefile_recipe_match = re.search(
        rf"^{re.escape(target)}:.*\n((?:\t.*\n?)*)", _makefile_text(), re.MULTILINE
    )
    assert makefile_recipe_match is not None
    assert "devcontainer_config.cli hooks-install" in makefile_recipe_match.group(1)
    assert re.search(rf"\bmake\s+{re.escape(target)}\b", _git_hooks_step())


def test_provisioning_flow_table_stays_in_sync_with_mains_step_order() -> None:
    """AC-DOC-001 regression: doc_review once caught this exact drift.

    `main()` calls `configure_git`, then `install_git_hooks`, then
    `configure_repo_detection`; `docs/devcontainer.md`'s provisioning-flow
    table was written to document a hooks-install row between the
    git-identity row and the `.gitmodules` row to match. Pinning both
    orderings here means a future step reordered or added in `main()` with
    no matching table update fails this test instead of surfacing only at
    doc_review time.
    """
    main_body = _main_body()
    git_call_idx = main_body.index("configure_git")
    hooks_call_idx = main_body.index("install_git_hooks")
    repo_detect_call_idx = main_body.index("configure_repo_detection")
    assert git_call_idx < hooks_call_idx < repo_detect_call_idx, (
        "main() no longer calls install_git_hooks between configure_git and "
        "configure_repo_detection"
    )

    table = _provisioning_flow_table()
    git_row_idx = table.index("git identity and credential helper")
    hooks_row_idx = table.index("make hooks-install")
    gitmodules_row_idx = table.index("`.gitmodules` per repository")
    assert git_row_idx < hooks_row_idx < gitmodules_row_idx, (
        "docs/devcontainer.md's provisioning-flow table no longer documents the "
        "hooks-install step between the git-identity and .gitmodules rows"
    )


def test_provisioning_flow_intro_enumerates_every_required_step() -> None:
    """AC-DOC-001 regression: doc_review twice caught this paragraph's claim about
    which steps abort the build drifting from the table's `required` rows --
    once naming the resmon link the lone exception when two steps aborted,
    once claiming "Two steps are the exception" when four did. The fix stopped
    stating a count and instead enumerates the steps the table itself marks
    `required`; this pins that enumeration's length to the table's actual
    `required` row count, so a step gaining or losing `required` without a
    matching prose update fails here instead of at doc_review time again.
    """
    table = _provisioning_flow_table()
    required_row_count = table.count(", required |")
    assert required_row_count > 0, "no `required` rows found in the provisioning-flow table"

    intro = _provisioning_flow_required_steps_prose()
    enumerated_items = [item for item in intro.split("; ") if item.strip()]
    assert len(enumerated_items) == required_row_count, (
        f"provisioning-flow intro enumerates {len(enumerated_items)} step(s) but "
        f"the table marks {required_row_count} row(s) required"
    )


def test_git_identity_variables_fail_with_a_named_error_not_an_unbound_variable() -> None:
    """`set -u` turns a missing shell.env value into a line number, not a remedy.

    `configure_git` reads GIT_USER, GIT_USER_EMAIL and GIT_PROVIDER_URL straight
    out of the environment. With any of them unset the script died with
    "GIT_USER: unbound variable" and a line number, naming neither where the
    value comes from nor how to supply it -- observed during a real container
    bring-up. The fail-fast standard requires the error to be actionable, which
    the sibling DEFAULT_GIT_BRANCH check already demonstrates.
    """
    text = (_repo_root() / ".devcontainer" / ".devcontainer.postcreate.sh").read_text(
        encoding="utf-8"
    )
    section = text[text.index("configure_git() {") : text.index("install_git_hooks() {")]
    for name in ("GIT_USER", "GIT_USER_EMAIL", "GIT_PROVIDER_URL"):
        assert name in section, f"{name} is no longer read here; update this test"
    assert "is not set in the environment." in section, (
        "configure_git must name the missing variable and where it comes from, rather than "
        "letting set -u report an unbound variable"
    )
    assert "shell.env" in section, "the error must name where the value comes from"


# ---------------------------------------------------------------------------
# The hostcreds startup block: the shell.env step also renders and appends
# the credential-startup block postCreate wires into both rc files. The
# block is rendered by devcontainer_config (never hand-written into this
# 500-line script), one shell-agnostic render serves both startup files,
# a render failure is fatal through exit_with_error, and the append is
# idempotent under the block's own marker. These tests pin that wiring at
# full strength: the render helper exists and delegates, the append is
# marker-guarded in both files, and nothing swallows a non-zero render.
# ---------------------------------------------------------------------------


def _configure_shell_env_body() -> str:
    """The `configure_shell_env` function body from `.devcontainer.postcreate.sh`.

    Isolated by name, the same pattern this file's `_git_hooks_step` uses,
    so an assertion meant for this step cannot be satisfied by unrelated
    text elsewhere in the script. Delegates to the shared
    `conftest._function_body` brace-depth scanner rather than a local
    regex.
    """
    return _function_body("configure_shell_env")


def test_postcreate_installs_no_uv_tools() -> None:
    """The provisioning flow never installs a tool into the host environment.

    The one `uv tool install` this script ever carried existed to put a
    console script on PATH for a superseded mechanism; postCreate now
    provisions only through devcontainer features and files inside the
    container, so a reintroduced tool install fails here rather than
    resuming silently.
    """
    assert "uv tool install" not in _postcreate_text(), (
        "postCreate must not install tools into the environment; everything "
        "installable belongs to a devcontainer feature"
    )


def test_postcreate_defines_a_render_helper_for_the_hostcreds_block() -> None:
    """The block is rendered by devcontainer_config, never hand-written here."""
    assert "devcontainer_config.cli shell-block" in _postcreate_text()


def test_configure_shell_env_renders_the_startup_block_once() -> None:
    """One shell-agnostic block serves both files, so there is exactly one render."""
    body = _configure_shell_env_body()
    assert len(re.findall(r"render_hostcreds_shell_block\b", body)) == 1


def test_render_helper_takes_no_shell_argument() -> None:
    """The block is the same text for bash and zsh; a shell argument would be dead."""
    body = _function_body("render_hostcreds_shell_block")
    assert "local shell" not in body
    assert '"${DEVCONTAINER_SCRIPTS_DIR}"' in body


def test_configure_shell_env_appends_the_block_to_the_bash_startup_file() -> None:
    """The rendered block is written into BASH_RC, beside the shell.env lines."""
    body = _configure_shell_env_body()
    assert re.search(r'startup_block.*>>\s*"\$\{BASH_RC\}"', body, re.DOTALL)


def test_configure_shell_env_appends_the_block_to_the_zsh_environment_file() -> None:
    """The same rendered block is written into ZSH_ENV, beside the shell.env lines."""
    body = _configure_shell_env_body()
    assert re.search(r'startup_block.*>>\s*"\$\{ZSH_ENV\}"', body, re.DOTALL)


def test_configure_shell_env_does_not_swallow_a_render_failure() -> None:
    """Nothing discards the render command's status."""
    body = _configure_shell_env_body()
    for line in body.splitlines():
        if "render_hostcreds_shell_block" not in line:
            continue
        for suffix in _FORGIVING_SUFFIXES:
            assert suffix not in line, f"{suffix!r} would swallow a non-zero render status"


def test_configure_shell_env_aborts_through_exit_with_error_on_a_render_failure() -> None:
    """A non-zero render is fatal through exit_with_error, not warned about."""
    body = _configure_shell_env_body()
    render_idx = body.index("render_hostcreds_shell_block")
    error_idx = body.index("exit_with_error", render_idx)
    tail = body[render_idx:error_idx]
    assert "||" in tail, "the render's failure is not wired to a handler"
    assert "log_section_skipped" not in body[render_idx : error_idx + 400]


def test_configure_shell_env_reuses_the_shared_printers() -> None:
    """No new error printer is introduced; the existing primitives are reused."""
    body = _configure_shell_env_body()
    assert re.search(r"\bexit_with_error\b", body)
    assert re.search(r"\blog_section_done\b", body)


def test_configure_shell_env_derives_the_marker_from_the_rendered_block() -> None:
    """The guard's marker text is read back out of the rendered block itself
    (its own first line), rather than a second hand-copied literal of
    hostcreds.MARKER that could drift out of sync with the renderer.
    """
    body = _configure_shell_env_body()
    assert re.search(
        r'startup_marker="\$\(printf[^\n]*startup_block[^\n]*\|\s*head\s+-n\s*1\)"', body
    )


def test_configure_shell_env_guards_the_bash_append_against_a_second_application() -> None:
    """Idempotence: a second configure_shell_env run must not duplicate the
    block in BASH_RC. The append is guarded by a grep for the block's own
    marker line, the same `grep -q ... || <action>` guard style this file
    already uses.
    """
    body = _configure_shell_env_body()
    assert re.search(
        r'grep\s+-qF\s+--\s+"\$\{startup_marker\}"\s+"\$\{BASH_RC\}"\s*\|\|.*startup_block.*>>\s*"\$\{BASH_RC\}"',
        body,
    ), "the bash append is not guarded by a marker grep"


def test_configure_shell_env_guards_the_zsh_append_against_a_second_application() -> None:
    """Same idempotence guard as the bash case, for ZSH_ENV."""
    body = _configure_shell_env_body()
    assert re.search(
        r'grep\s+-qF\s+--\s+"\$\{startup_marker\}"\s+"\$\{ZSH_ENV\}"\s*\|\|.*startup_block.*>>\s*"\$\{ZSH_ENV\}"',
        body,
    ), "the zsh append is not guarded by a marker grep"


def test_configure_shell_env_renders_the_hostcreds_startup_block() -> None:
    """The startup block configure_shell_env renders is the hostcreds one."""
    body = _configure_shell_env_body()
    assert "render_hostcreds_shell_block" in body
    assert "shellrc" not in body, "the superseded renderer must not linger here"


# ---------------------------------------------------------------------------
# The generic opencode harness (pass 1): the shipped config, the parameterized
# MCP wrappers, the provider toggles, the agent generator and the per-user
# seam. Every assertion is a text- or JSON-level consistency check against the
# committed files, in the same spirit the hooks tests above use: the
# provisioning script is never executed, because it requires a real container
# user and a real npm, and the properties AC-pinned here are properties of the
# committed text.
# ---------------------------------------------------------------------------

_SHIPPED_CONFIG_RELATIVE_PATH = ".devcontainer/opencode.json"
_OPENCODE_DIR_RELATIVE_PATH = ".devcontainer/opencode"

# The eight MCP servers the shipped config defines, exactly. Stripe and
# image-gen are deliberately absent: they are personal to the live host
# config and are documented, never shipped.
_SHIPPED_MCP_NAMES = {
    "playwright",
    "context7",
    "gws",
    "gcp",
    "aws-mcp",
    "azure-devops",
    "azure",
    "github",
}

# The committed wrapper binaries under .devcontainer/opencode/bin/, referenced
# through the ${WORK_DIR} placeholder the postcreate resolves into the
# installed copy (the only shape a committed JSON can carry: the workspace
# root is known only at container runtime).
_WRAPPER_PLACEHOLDER_PREFIX = "${WORK_DIR}/.devcontainer/opencode/bin/"

# Organization-specific literals that must never appear in a committed file:
# the org's SSM prefix and the GitHub App's name. The history-scrub rule
# allows zero occurrences under .devcontainer/, tests/ and shell.env.example.
# Runtime-construction rule: the scrub forbids these tokens in committed
# bytes, so each forbidden token is built at runtime from fragments below
# (the same pattern conftest.py's `_synthetic_account_id` uses).
_ORGANIZATION_LITERALS = (
    "brim" + "capitalgroup",
    "matthew" + "dresden-" + "opencode-" + "mcp",
)

# The provider toggles, validated to 0/1 only by configure_opencode.
_PROVIDER_TOGGLES = ("ZAI_ENABLED", "OPENROUTER_ENABLED")


def _shipped_opencode_config() -> dict:
    """`.devcontainer/opencode.json`, parsed fresh for every call."""
    text = (_repo_root() / _SHIPPED_CONFIG_RELATIVE_PATH).read_text(encoding="utf-8")
    return json.loads(text)


def _devcontainer_json() -> dict:
    """`.devcontainer/devcontainer.json`, parsed fresh for every call."""
    text = (_repo_root() / ".devcontainer" / "devcontainer.json").read_text(encoding="utf-8")
    return json.loads(text)


def _configure_opencode_body() -> str:
    """The `configure_opencode` function body from `.devcontainer.postcreate.sh`."""
    return _function_body("configure_opencode")


def _run_project_setup_body() -> str:
    """The `run_project_setup` function body from `.devcontainer.postcreate.sh`."""
    return _function_body("run_project_setup")


def _frontmatter(agent_file: Path) -> str:
    """The YAML frontmatter of a generated agent markdown file.

    The generator renders `---\\n<yaml>\\n---\\n\\n<marker>\\n<body>\\n`;
    splitting on `---` recovers the YAML between the two fences.
    """
    parts = agent_file.read_text(encoding="utf-8").split("---")
    assert len(parts) >= 3, f"{agent_file} has no frontmatter fences"
    return parts[1]


# A synthetic installed-config the committed generator is run against. The
# generator reads `ROOT/opencode.json` where ROOT is the installed config's
# directory, so the fixture builds that layout in tmp_path: the committed
# lib/ tree copied in, and this config written as the config the postcreate
# jq-rewrite would produce (toggles on, model matrix applied).
_GENERATOR_SYNTHETIC_CONFIG = {
    "$schema": "https://opencode.ai/config.json",
    "shell": "zsh",
    "model": "zai-max/glm-5.3",
    "small_model": "zai-max/glm-5.3-flash",
    "subagent_depth": 3,
    "provider": {
        "zai-max": {
            "name": "z.ai max coding plan",
            "npm": "@ai-sdk/openai-compatible",
            "options": {"apiKey": "{env:ZAI_API_KEY}"},
            "models": {"glm-5.3": {}, "glm-5.3-flash": {}},
        },
        "openrouter": {
            "name": "openrouter.ai",
            "options": {"apiKey": "{env:OPENROUTER_API_KEY}"},
            "models": {"glm-5.3": {}, "glm-5.3-flash": {}},
        },
    },
}


@pytest.fixture
def generated_agent_tree(tmp_path: Path) -> Path:
    """A synthetic installed-config tree with the committed generator run in it.

    The committed `.devcontainer/opencode/lib/generate-agents.mjs` is designed
    to run from the installed copy (`~/.config/opencode`), where its own ROOT
    resolution places agents in `agent/` and the manifest in
    `lib/generated-agents.json`. The fixture rebuilds that layout under
    `tmp_path` from the committed lib/ and runs the generator for real, so the
    frontmatter contracts below are pinned against the generator's actual
    output, not against a copy of it.
    """
    root = tmp_path / "opencode-home"
    root.mkdir()
    shutil.copytree(_repo_root() / _OPENCODE_DIR_RELATIVE_PATH / "lib", root / "lib")
    # The installed config carries agent/ from the start: the postcreate
    # creates it before running the generator (the generator does not mkdir).
    (root / "agent").mkdir()
    (root / "opencode.json").write_text(
        json.dumps(_GENERATOR_SYNTHETIC_CONFIG, indent=2), encoding="utf-8"
    )
    subprocess.run(
        ["node", str(root / "lib" / "generate-agents.mjs")],
        check=True,
        capture_output=True,
        text=True,
    )
    return root


# --------------------------------------------------------------- shipped config


def test_shipped_config_carries_both_provider_api_key_interpolations() -> None:
    """Both provider blocks ship, each authenticating only through an env
    interpolation; the config itself carries no credential."""
    config = _shipped_opencode_config()
    providers = config["provider"]
    assert set(providers) == {"zai-max", "openrouter"}
    for provider_id, env_name in (
        ("zai-max", "{env:ZAI_API_KEY}"),
        ("openrouter", "{env:OPENROUTER_API_KEY}"),
    ):
        api_key = providers[provider_id]["options"]["apiKey"]
        assert api_key == env_name, (
            f"provider {provider_id!r} ships apiKey {api_key!r}; the committed config "
            "may carry only a {env:...} interpolation, never a credential value"
        )


def test_shipped_config_carries_no_literal_api_key_anywhere() -> None:
    """Every apiKey in the shipped config is a {env:...} interpolation.

    The stronger general form of the per-provider pin above: no provider
    block, known or added later, may ship a literal credential.
    """
    config = _shipped_opencode_config()
    api_keys = [
        block["options"]["apiKey"]
        for block in config["provider"].values()
        if "apiKey" in block.get("options", {})
    ]
    assert api_keys, "no apiKey found in the shipped config; the scan is vacuous"
    for api_key in api_keys:
        assert api_key.startswith("{env:") and api_key.endswith("}"), (
            f"apiKey {api_key!r} is not a {{env:...}} interpolation"
        )


def test_shipped_config_has_no_per_user_or_generated_keys() -> None:
    """The per-user and generated-at-provisioning keys stay out of the shipped file.

    `username` and `default_agent` are per-developer (the seam file or the
    operator sets them); `enabled_providers` is written by the toggles at
    postcreate; the generated agents land as files in the installed agent/
    directory, never as a config key.
    """
    config = _shipped_opencode_config()
    for key in ("username", "default_agent", "enabled_providers", "agent"):
        assert key not in config, (
            f"{key!r} must not ship in .devcontainer/opencode.json: it is per-user "
            "or resolved at provisioning time, and a shipped value would override it"
        )


def test_shipped_config_model_keys_point_at_zai_max() -> None:
    """model/small_model ship under the zai-max provider, flagship + flash.

    The shipped defaults are the zai-max pair (flagship as the primary model,
    flash as the small model); the postcreate jq rewrite keeps them unchanged
    when zai is enabled and replaces them per the matrix otherwise.
    """
    config = _shipped_opencode_config()
    assert config["model"] == "zai-max/glm-5.3"
    assert config["small_model"] == "zai-max/glm-5.3-flash"


def test_shipped_config_defines_exactly_the_eight_mcp_servers() -> None:
    """The MCP set is exactly the eight shipped servers; nothing dead.

    Stripe and image-gen are excluded by design (they are personal to the
    live host config); a stray entry here would ship a server no container
    can authenticate.
    """
    mcp = _shipped_opencode_config()["mcp"]
    assert set(mcp) == _SHIPPED_MCP_NAMES, (
        f"shipped MCP set is {sorted(mcp)}, expected exactly {sorted(_SHIPPED_MCP_NAMES)}"
    )


@pytest.mark.parametrize(
    "mcp_name",
    ["context7", "github", "azure-devops", "gws"],
)
def test_wrapper_backed_mcp_commands_use_the_workspace_placeholder(mcp_name: str) -> None:
    """Every committed-wrapper MCP runs the wrapper under .devcontainer/opencode/bin/.

    The live host config references the host's own bin by absolute path; the
    shipped config cannot know the container's workspace root at commit time,
    so the wrapper-backed entries carry the ${WORK_DIR} placeholder the
    postcreate resolves into the installed copy.
    """
    command = _shipped_opencode_config()["mcp"][mcp_name]["command"]
    assert command[0].startswith(_WRAPPER_PLACEHOLDER_PREFIX), (
        f"{mcp_name} command {command!r} does not reference the committed wrapper "
        f"through the ${'{WORK_DIR}'} placeholder"
    )


def test_shipped_config_gcp_mcp_runs_gwhiz_from_path() -> None:
    """The gcp MCP runs `g-whiz` from PATH (postcreate installs it globally),
    and the aws/azure/playwright entries keep their npx/uvx commands."""
    mcp = _shipped_opencode_config()["mcp"]
    assert mcp["gcp"]["command"] == ["g-whiz"]
    assert mcp["playwright"]["command"][:3] == ["npx", "-y", "@playwright/mcp"]
    assert mcp["azure"]["command"][:3] == ["npx", "-y", "@azure/mcp@latest"]
    assert mcp["aws-mcp"]["command"][:2] == ["uvx", "mcp-proxy-for-aws@1.7.0"]


def test_shipped_config_carries_the_live_runtime_settings() -> None:
    """The runtime settings carried from the live host config stay pinned."""
    config = _shipped_opencode_config()
    assert config["shell"] == "zsh"
    assert config["subagent_depth"] == 3
    assert config["share"] == "disabled"
    assert config["autoupdate"] is True
    assert config["snapshot"] is True
    assert config["permission"] == {"*": "allow"}
    assert config["formatter"] is True
    assert config["lsp"] is False
    assert config["tool_output"] == {"max_lines": 2000, "max_bytes": 51200}
    assert config["compaction"] == {"auto": True, "tail_turns": 40, "buffer": 100000}
    assert config["plugin"] == [
        "opencode-glm-quota@latest",
        "opencode-cross-session-messaging@latest",
        "opencode-auto-continue",
    ]


def test_shipped_config_carries_model_limits_and_compaction_buffer() -> None:
    """The shipped config carries the per-model verification payload.

    Each shipped model entry mirrors the host config's verified per-model
    context/output limits (and modalities where the host entry carries them);
    the compaction block pins the token buffer. A regression that strips
    `limit` keys or the compaction `buffer` goes red here.
    """
    config = _shipped_opencode_config()
    entries_with_limit = [
        (provider_id, model_id)
        for provider_id, provider in config["provider"].items()
        for model_id, entry in provider.get("models", {}).items()
        if "limit" in entry
    ]
    assert entries_with_limit, (
        "no shipped model entry carries a limit: the verified per-model limits were stripped"
    )
    for provider_id, model_id in entries_with_limit:
        limit = config["provider"][provider_id]["models"][model_id]["limit"]
        assert set(limit) == {"context", "output"}, (
            f"{provider_id}/{model_id} limit {limit!r} must carry exactly context and output"
        )
        assert all(isinstance(value, int) and value > 0 for value in limit.values()), (
            f"{provider_id}/{model_id} limit {limit!r} must carry positive integer values"
        )
    assert config["compaction"].get("buffer") == 100000, (
        "the shipped compaction block must pin buffer=100000 alongside auto/tail_turns"
    )


# ---------------------------------------------------------------- devcontainer.json


def test_devcontainer_json_carries_the_provider_toggles_in_colon_form() -> None:
    """The toggles reach the container through containerEnv, in the colon form.

    devcontainer's variable substitution supports the `${VAR:default}` colon
    form; the bash-style `${VAR:-default}` form is not substituted (verified
    against the devcontainer CLI) and would ship the literal text. The exact
    spelling is pinned so a rewording back to the broken form fails here.
    """
    container_env = _devcontainer_json().get("containerEnv", {})
    assert container_env.get("ZAI_ENABLED") == "${localEnv:ZAI_ENABLED:1}", (
        "ZAI_ENABLED must be '${localEnv:ZAI_ENABLED:1}' exactly: the colon form "
        "substitutes, the bash ':-' form does not"
    )
    assert container_env.get("OPENROUTER_ENABLED") == "${localEnv:OPENROUTER_ENABLED:1}"


# ---------------------------------------------------------------- postcreate wiring


def test_configure_opencode_validates_the_provider_toggles() -> None:
    """Both toggles must be 0 or 1; anything else aborts, naming the variable
    and where it is set."""
    body = _configure_opencode_body()
    for toggle in _PROVIDER_TOGGLES:
        assert toggle in body, f"{toggle} is no longer read by configure_opencode"
    assert "0|1" in body, "the toggle values are not restricted to 0 and 1"
    assert "exit_with_error" in body
    assert "devcontainer.json" in body, (
        "the refusal must name where the toggle is set (devcontainer.json's "
        "containerEnv, from the host environment) so the fix is findable"
    )


def test_configure_opencode_writes_enabled_providers_and_the_model_matrix() -> None:
    """The jq rewrite resolves the toggles into enabled_providers and the
    model matrix: both on -> zai-max primary pair plus both providers;
    openrouter only -> the openrouter pair; zai only -> the shipped defaults
    unchanged; both off -> the model keys removed entirely."""
    body = _configure_opencode_body()
    assert "enabled_providers" in body
    assert '"zai-max/glm-5.3"' in body and '"zai-max/glm-5.3-flash"' in body
    assert '"openrouter/glm-5.3"' in body and '"openrouter/glm-5.3-flash"' in body
    assert "del(.model)" in body and "del(.small_model)" in body, (
        "the both-off branch must remove the model keys entirely, not leave a "
        "model no enabled provider can serve"
    )


def test_configure_opencode_asserts_no_literal_api_key_in_the_resolved_config() -> None:
    """The resolved config is asserted to carry only {env:...} apiKeys before
    it is installed, so a future literal in the shipped file aborts provisioning.

    The assert counts apiKey keys and interpolated ones (the grep pattern
    pins the exact interpolation shape, `"{env:NAME}"` with an uppercase
    name) and requires the counts to match and be non-zero.
    """
    body = _configure_opencode_body()
    assert '"apiKey":' in body
    assert '"{env:[A-Z][A-Z0-9_]*}"' in body, (
        "configure_opencode does not assert that every apiKey is a {env:...} interpolation"
    )


def test_configure_opencode_cmps_the_installed_file_against_expected_bytes() -> None:
    """Install-integrity: the installed file is compared byte-for-byte against
    the post-jq expected bytes (replacing the old single-grep spot check,
    which could not catch truncation or drift between resolution and install)."""
    body = _configure_opencode_body()
    assert re.search(r"\bcmp\s+", body), "configure_opencode does not cmp the installed file"


def test_configure_opencode_installs_and_runs_the_agent_generator() -> None:
    """The committed generator + inputs are copied into the installed config
    and run with node, fail-fast; opencode is left with a full agent set or
    provisioning aborts."""
    body = _configure_opencode_body()
    assert "generate-agents.mjs" in body
    assert re.search(r"\bnode\b", body), "the generator is not run with node"
    gen_idx = body.index("generate-agents.mjs")
    assert "exit_with_error" in body[gen_idx : gen_idx + 400], (
        "a generator failure does not abort provisioning"
    )


def test_configure_opencode_notifies_when_mcp_credential_sources_are_absent() -> None:
    """Every MCP whose credential source is missing gets a provisioning-time
    notice naming the documented enable step: github/azure-devops/context7
    (SSM reads), gws (OAuth state), gcp and azure (login state)."""
    assert re.search(
        r"notify_opencode_mcp_credentials\s*$", _configure_opencode_body(), re.MULTILINE
    )
    notify_body = _function_body("notify_opencode_mcp_credentials")
    assert ".config/gws" in notify_body, "the gws OAuth state check is missing"
    assert ".config/gcloud" in notify_body, "the gcp login-state check is missing"
    assert ".azure" in notify_body, "the azure login-state check is missing"
    assert "SSM_ORG_PREFIX" in notify_body, "the github/azure-devops SSM source check is missing"
    assert "SSM_CONTEXT7_PREFIX" in notify_body, "the context7 SSM source check is missing"


def test_configure_opencode_aliases_define_ocd_and_ocdr() -> None:
    """The opencode aliases land in the same interactive-rc shape as ccd/ccdr."""
    body = _function_body("configure_opencode_aliases")
    assert "alias ocd=" in body and "alias ocdr=" in body
    assert "interactive_rcs" in body, "the aliases must land in the same rc files ccd/ccdr use"
    assert re.search(r"^\s*configure_opencode_aliases\s*$", _main_body(), re.MULTILINE)


def test_run_project_setup_sources_the_per_user_seam_file() -> None:
    """After the project script, the gitignored per-user file is sourced in the
    same environment context when present."""
    body = _run_project_setup_body()
    assert "project-setup.local.sh" in body
    seam_idx = body.index(". '${local_setup}'")
    assert re.search(r"if\s+\[\s+-f\s+'\$\{local_setup\}'", body), (
        "the seam file is not sourced behind an existence check"
    )
    project_idx = body.index("bash '${PROJECT_SETUP}'")
    assert seam_idx > project_idx, "the seam must be sourced after the project script"


def test_configure_github_mcp_server_is_installed_by_postcreate() -> None:
    """The github MCP's server binary is downloaded into the user's bin, with
    the version read from the environment (never hardcoded)."""
    body = _function_body("configure_github_mcp_server")
    assert "github-mcp-server" in body
    assert "GITHUB_MCP_SERVER_VERSION" in body, (
        "the release version must come from the environment, not a hardcoded value"
    )
    assert "${USER_BIN}" in body or "USER_BIN" in body
    assert re.search(r"^\s*configure_github_mcp_server\s*$", _main_body(), re.MULTILINE)


# ---------------------------------------------------------------- scrub rule


def _scrub_scoped_files() -> list[Path]:
    """Every file the history-scrub rule covers: .devcontainer/, tests/ and
    shell.env.example.

    This module itself is excluded: it is the scanner, and it must carry the
    literals it scans for (the same reason AC-DOC-003's own grep scans
    exclude their own enforcement mechanism).
    """
    root = _repo_root()
    files = list((root / ".devcontainer").rglob("*")) + list((root / "tests").rglob("*"))
    files.append(root / "shell.env.example")
    return [
        path
        for path in files
        if path.is_file()
        and path.resolve() != Path(__file__).resolve()
        and "__pycache__" not in path.parts
        and ".git" not in path.parts
        and "node_modules" not in path.parts
    ]


@pytest.mark.parametrize(
    "literal",
    _ORGANIZATION_LITERALS,
    ids=["org-ssm-prefix", "github-app-name"],
)
def test_no_organization_literal_in_committed_files(literal: str) -> None:
    """Zero occurrences of the org's SSM prefix or the GitHub App name in any
    committed file under .devcontainer/, tests/ or shell.env.example: the
    committed tree carries no organization naming."""
    offenders = [
        str(path.relative_to(_repo_root()))
        for path in _scrub_scoped_files()
        if literal in path.read_text(encoding="utf-8", errors="replace")
    ]
    assert not offenders, (
        f"the organization literal {literal!r} appears in committed file(s) {offenders}; "
        "parameterize it (env vars read from shell.env) or strip it"
    )


# ---------------------------------------------------------------- MCP wrappers


@pytest.mark.parametrize(
    "wrapper,required_vars",
    (
        ("github-mcp", ("SSM_ORG_PREFIX", "AWS_REGION", "GITHUB_APP_TARGET")),
        ("azure-devops-mcp", ("SSM_ORG_PREFIX", "AWS_REGION", "ADO_ORG", "ADO_USER")),
        (
            "azure-devops-pat-check",
            ("SSM_ORG_PREFIX", "AWS_REGION", "ADO_ORG", "ADO_USER"),
        ),
        ("context7-mcp", ("SSM_CONTEXT7_PREFIX", "AWS_REGION")),
        ("gws-mcp", ()),
    ),
)
def test_mcp_wrappers_are_bash_and_parameterized(
    wrapper: str, required_vars: tuple[str, ...]
) -> None:
    """Each committed wrapper is POSIX bash (the container has no zsh at MCP
    spawn time) and reads every organization/infra value from an environment
    variable that fails fast naming shell.env when unset."""
    path = _repo_root() / _OPENCODE_DIR_RELATIVE_PATH / "bin" / wrapper
    assert path.is_file(), f"wrapper {wrapper} is missing"
    text = path.read_text(encoding="utf-8")
    assert text.startswith("#!/usr/bin/env bash"), f"{wrapper} must run under bash"
    for variable in required_vars:
        assert f'"{variable}' in text or f"${{{variable}:" in text, (
            f"{wrapper} does not read {variable} from the environment"
        )
        assert "shell.env" in text, (
            f"{wrapper}'s fail-fast message must name shell.env as where the value is set"
        )


def test_gws_wrapper_names_its_enable_step() -> None:
    """The gws wrapper is not organization-parameterized (it carries no SSM
    value): its failure message names the install it needs instead."""
    text = (_repo_root() / _OPENCODE_DIR_RELATIVE_PATH / "bin" / "gws-mcp").read_text(
        encoding="utf-8"
    )
    assert "brew install gws" in text


# ---------------------------------------------------------------- generator contract


def test_generator_config_path_points_at_the_installed_config() -> None:
    """The committed generator reads `opencode.json` (the installed,
    toggle-resolved config), not the host's opencode.jsonc."""
    text = (_repo_root() / _OPENCODE_DIR_RELATIVE_PATH / "lib" / "generate-agents.mjs").read_text(
        encoding="utf-8"
    )
    assert 'join(ROOT, "opencode.json")' in text
    assert 'join(ROOT, "opencode.jsonc")' not in text, (
        "the committed generator still reads the host's opencode.jsonc"
    )


def test_flagship_plan_agent_task_rule_allows_both_same_provider_workers(
    generated_agent_tree: Path,
) -> None:
    """A flagship plan agent's task rule allows both same-provider workers
    (`<provider>-glm-*`), not just its own model."""
    for provider in ("zai-max", "openrouter"):
        frontmatter = _frontmatter(generated_agent_tree / "agent" / f"plan-{provider}-glm-5.3.md")
        assert '"*": "deny"' in frontmatter
        assert f'"{provider}-glm-*": "allow"' in frontmatter, (
            f"plan-{provider}-glm-5.3 does not allow both same-provider workers"
        )


def test_flash_plan_agent_locks_to_its_exact_worker(generated_agent_tree: Path) -> None:
    """A flash plan agent's task rule locks to its exact flash worker."""
    for provider in ("zai-max", "openrouter"):
        frontmatter = _frontmatter(
            generated_agent_tree / "agent" / f"plan-{provider}-glm-5.3-flash.md"
        )
        assert '"*": "deny"' in frontmatter
        assert f'"{provider}-glm-5.3-flash": "allow"' in frontmatter
        assert f'"{provider}-glm-*"' not in frontmatter, (
            f"plan-{provider}-glm-5.3-flash leaks the wildcard task rule"
        )


def test_flash_build_driver_delegates_all_work_to_the_flash_worker(
    generated_agent_tree: Path,
) -> None:
    """A flash build driver manages the loop only: its task rule allows only
    its flash worker, and its body states the all-work delegation."""
    for provider in ("zai-max", "openrouter"):
        agent_file = generated_agent_tree / "agent" / f"build-{provider}-glm-5.3-flash.md"
        frontmatter = _frontmatter(agent_file)
        assert '"*": "deny"' in frontmatter
        assert f'"{provider}-glm-5.3-flash": "allow"' in frontmatter
        body = agent_file.read_text(encoding="utf-8")
        assert "ALWAYS and only delegated" in body, (
            f"build-{provider}-glm-5.3-flash does not carry the all-work delegation contract"
        )


def test_build_agents_carry_no_bash_rule(generated_agent_tree: Path) -> None:
    """Build agents inherit full permissions: their frontmatter carries no
    bash rule at all (only the task rule)."""
    for provider in ("zai-max", "openrouter"):
        for model in ("glm-5.3", "glm-5.3-flash"):
            frontmatter = _frontmatter(
                generated_agent_tree / "agent" / f"build-{provider}-{model}.md"
            )
            assert "bash" not in frontmatter, (
                f"build-{provider}-{model} carries a bash rule; build agents must "
                "inherit the full permission set"
            )


def test_plan_agents_gate_bash_and_allow_git_grep(generated_agent_tree: Path) -> None:
    """Plan agents gate bash behind ask with a read-only allowlist, and the
    allowlist carries the git grep family."""
    manifest = json.loads(
        (generated_agent_tree / "lib" / "generated-agents.json").read_text(encoding="utf-8")
    )
    assert "git grep*" in manifest["readOnlyBash"], (
        "the read-only bash allowlist lost the git grep family"
    )
    for provider in ("zai-max", "openrouter"):
        for model in ("glm-5.3", "glm-5.3-flash"):
            frontmatter = _frontmatter(
                generated_agent_tree / "agent" / f"plan-{provider}-{model}.md"
            )
            assert '"edit": "deny"' in frontmatter
            assert '"*": "ask"' in frontmatter, f"plan-{provider}-{model} does not gate bash"
            assert '"git grep*": "allow"' in frontmatter, (
                f"plan-{provider}-{model} does not allow git grep"
            )


def test_worker_agents_are_subagents(generated_agent_tree: Path) -> None:
    """Every provider-model pair gets its subagent-mode worker file."""
    for provider in ("zai-max", "openrouter"):
        for model in ("glm-5.3", "glm-5.3-flash"):
            frontmatter = _frontmatter(generated_agent_tree / "agent" / f"{provider}-{model}.md")
            assert '"mode": "subagent"' in frontmatter


# ---------------------------------------------------------------- self-test suites


def test_opencode_self_test_suites_are_shipped() -> None:
    """The four self-test suites ship under .devcontainer/opencode/tests/ and
    the bash-allowlist suite carries the git grep fixture."""
    tests_dir = _repo_root() / _OPENCODE_DIR_RELATIVE_PATH / "tests"
    for suite in (
        "bash-allowlist.test.mjs",
        "generated-agents.test.mjs",
        "model-profiles.test.mjs",
        "zai-quota-wait.test.mjs",
    ):
        assert (tests_dir / suite).is_file(), f"the {suite} suite is not shipped"
    allowlist = (tests_dir / "bash-allowlist.test.mjs").read_text(encoding="utf-8")
    assert '"git grep -n' in allowlist, (
        "the bash-allowlist suite has no observed-read-only git grep fixture"
    )
