"""ENGINE=local|<name> addresses one engine explicitly, without switching contexts.

Before this wiring, pointing work at a specific engine meant
`docker context use` -- machine-wide state every terminal and window shares,
so driving the local engine and a remote instance concurrently meant each
command switched the context out from under the others. `ENGINE` now names
the engine one invocation talks to. Unset, it changes nothing: every target
follows the active docker context exactly as before. `local` names this
machine's engine; an instance name resolves that instance's
`<repo-slug>-<name>` context.

Five properties are pinned here.

lib.sh resolves and validates ENGINE once per process (`rd_engine_context`):
'local' maps to LOCAL_DOCKER_CONTEXT, anything else must name an instance
directory under remote-instances/, the context is derived through
`instances.docker_context` (never a second shell copy of the rule), and a
bad ENGINE fails fast listing what exists.

rd_docker carries the resolved context explicitly (`--context`) on every call,
and only under the ENGINE guard, so unset leaves the plain docker call it
always was.

The resolver consumes ENGINE: when it names an instance, that instance's
address block -- REMOTE_DOCKER_CONTEXT, the Parameter Store prefix, the
recorded id -- is what the run resolves, not the four-step default order.

container.sh classifies the backend from ENGINE when set (local maps to
local, an instance to remote) rather than from the machine-wide active
context, and pins its whole process to the engine by exporting
DOCKER_CONTEXT once, in the dispatch prelude, before anything resolves or
classifies -- the variable docker itself reads ahead of the current context,
which is what carries the plain docker calls and the devcontainer CLI's own
invocations along.

The resolver cannot undo that pin: its export loop leaves DOCKER_CONTEXT
alone under ENGINE (its block was resolved from whatever the four-step order
defaulted to, which under ENGINE=local is not the pinned engine), and
container.sh re-asserts the pin after the resolver returns, so no export
between prelude and dispatch can split the run across two engines. Pinned
behaviorally: with a stand-in resolver whose block names a different
context, a pinned run survives resolution still pointing at its pin.

INSTANCE and ENGINE name engines too. Naming two different ones would
resolve one engine's addresses while aiming docker at another, so lib.sh
refuses it loudly (rd_fail naming both values and telling the caller to set
one); the same value is the redundant-but-consistent spelling and passes.

`make local`, `make remote` and `make disconnect` switch the machine-wide
docker context -- the exact state ENGINE exists to leave untouched -- so
under ENGINE they refuse with the reason and exit 2.

And `make connect` (the forwarding remedy `make remote INSTANCE=<name>`
stands on) resolves the instance it was asked for -- INSTANCE, or ENGINE
when it names one -- through the same lib.sh resolver every remote entry
point uses, instead of ignoring the variable and forwarding for the
resolver's default.

And the Makefile exports ENGINE to recipes when set (a plain make variable
does not cross into a recipe's environment) and documents the option under
OPTIONS.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import _function_body, _makefile_text, _synthetic_instance_id

_REPO_ROOT = Path(__file__).resolve().parent.parent
_RD_DIR = _REPO_ROOT / ".devcontainer" / "remote-docker"
_LIB_SH = _RD_DIR / "lib.sh"
_CONTAINER_SH = _RD_DIR / "container.sh"

_TIMEOUT_ENV_VAR = "ENGINE_TARGETING_TEST_TIMEOUT_SECONDS"
_DEFAULT_TIMEOUT_SECONDS = 30.0


def _timeout_seconds() -> float:
    raw = os.environ.get(_TIMEOUT_ENV_VAR)
    if raw is None:
        return _DEFAULT_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        pytest.fail(f"{_TIMEOUT_ENV_VAR}={raw!r} is not a number")
    if value <= 0:
        pytest.fail(f"{_TIMEOUT_ENV_VAR}={raw!r} must be positive")
    return value


def _lib_text() -> str:
    return _LIB_SH.read_text(encoding="utf-8")


def _container_text() -> str:
    return _CONTAINER_SH.read_text(encoding="utf-8")


def _lib_function_body(name: str) -> str:
    return _function_body(name, text=_lib_text())


def _container_function_body(name: str) -> str:
    return _function_body(name, text=_container_text())


def test_lib_sh_defines_the_engine_context_resolver() -> None:
    assert "rd_engine_context() {" in _lib_text(), (
        "lib.sh must hold the single ENGINE -> docker context resolution every other consumer calls"
    )


def test_engine_context_maps_local_to_the_local_context() -> None:
    body = _lib_function_body("rd_engine_context")
    assert '"$ENGINE" = "local"' in body
    assert 'resolved="$LOCAL_DOCKER_CONTEXT"' in body, (
        "ENGINE=local must name this machine's engine, LOCAL_DOCKER_CONTEXT"
    )


def test_engine_context_derives_an_instance_context_through_the_module() -> None:
    body = _lib_function_body("rd_engine_context")
    assert "instances.discover" in body, (
        "the instance check must go through the discovery the rest of the repo "
        "uses, not a second directory listing with its own filtering rule"
    )
    assert "instances.docker_context" in body, (
        "the context name must come from instances.docker_context -- the same "
        "<repo-slug>-<name> derivation every other consumer gets -- never a "
        "shell re-implementation of the prefix rule"
    )


def test_engine_context_fails_fast_listing_what_exists() -> None:
    body = _lib_function_body("rd_engine_context")
    assert "rd_fail" in body, "an unknown ENGINE must fail through rd_fail, not a bare exit"
    assert "remote-instances/" in body, "the failure must say where instances live"
    assert "make instance-init" in body, "the failure must name the command that creates one"
    assert "Unset ENGINE" in body, "the failure must offer the way back to today's behavior"


def test_engine_context_is_resolved_once_per_process() -> None:
    body = _lib_function_body("rd_engine_context")
    assert "RD_ENGINE_CONTEXT_RESOLVED" in body, (
        "resolution must be guarded against running twice; rd_docker consults "
        "it on every docker call and the derivation shells out to python"
    )


def test_rd_docker_prefixes_the_resolved_context_under_engine() -> None:
    body = _lib_function_body("rd_docker")
    guard_at = body.index('[ -n "${ENGINE:-}" ]')
    prefix_at = body.index('--context "$(rd_engine_context)"')
    assert guard_at < prefix_at, (
        "rd_docker must prefix --context <resolved>, and only after checking ENGINE is set"
    )


def test_rd_docker_is_a_plain_docker_call_when_engine_is_unset() -> None:
    body = _lib_function_body("rd_docker")
    assert '"$@"' in body, "the passthrough must remain"
    assert body.count("rd_engine_context") == 1, (
        "the resolved context must be consulted only inside the ENGINE guard; "
        "an unconditional resolution would change behavior with ENGINE unset"
    )


def test_the_resolver_consumes_engine_when_it_names_an_instance() -> None:
    lib = _lib_text()
    quiet = lib[lib.index("rd_resolve_instance_quiet() {") : lib.index("rd_engine_context() {")]
    assert '[ -n "${ENGINE:-}" ] && [ "$ENGINE" != "local" ]' in quiet, (
        "the resolver must be pointed at ENGINE's instance only when ENGINE "
        "names one; ENGINE=local and unset must leave the four-step order alone"
    )
    assert 'resolver_env+=(INSTANCE="$ENGINE")' in quiet, (
        "the resolver must resolve the instance ENGINE names, so the address "
        "block belongs to the engine rd_docker aims at"
    )


def test_diagnosis_names_the_instance_qualified_forward_remedy() -> None:
    body = _lib_function_body("rd_engine_diagnosis")
    assert "make remote INSTANCE=" in body, (
        "under ENGINE, the forward remedy must name the instance this run "
        "addresses: a bare 'make connect' refreshes the resolver's default, "
        "which is not necessarily this one"
    )
    assert '"make connect"' in body, (
        "with ENGINE unset the remedy must stay the plain 'make connect' it always was"
    )


def test_container_backend_classifies_from_engine_when_set() -> None:
    body = _container_function_body("rdc_backend")
    guard_at = body.index('[ -n "${ENGINE:-}" ]')
    engine_at = body.index("rd_engine_context")
    local_at = body.index('"$LOCAL_DOCKER_CONTEXT")')
    assert guard_at < engine_at < local_at, (
        "under ENGINE the backend must be classified from the resolved context "
        "(local maps to local, an instance to remote), never from the "
        "machine-wide active context"
    )


def test_container_backend_still_compares_the_active_context_when_engine_unset() -> None:
    body = _container_function_body("rdc_backend")
    assert "docker context show" in body, (
        "with ENGINE unset, classification must keep following the active context exactly as before"
    )


def test_container_pins_the_process_to_the_engine_before_anything_resolves() -> None:
    text = _container_text()
    # Scoped to the top-level dispatch region: rdc_build's own backend check
    # sits inside a function body, earlier in the file, and must not satisfy
    # the ordering this pins.
    dispatch = text[text.index('RDC_COMMAND="${1:-}"') :]
    export_at = dispatch.index('DOCKER_CONTEXT="$(rd_engine_context)"')
    assert "export DOCKER_CONTEXT" in dispatch[export_at:], (
        "the resolved context must be exported as DOCKER_CONTEXT -- the "
        "variable docker reads ahead of the machine-wide current context -- so "
        "the plain docker calls and the devcontainer CLI's own invocations "
        "address the named engine too"
    )
    resolve_at = dispatch.index("rd_resolve_instance_quiet")
    classify_at = dispatch.index('[ "$(rdc_backend)" = "remote" ]')
    assert export_at < resolve_at < classify_at, (
        "the process must be pinned to the engine before the resolver runs and "
        "before the backend is classified, and the export must be a direct "
        "call: inside a command substitution it would only reach a subshell"
    )


def test_the_makefile_exports_engine_when_set() -> None:
    assert re.search(r"^ifdef ENGINE\nexport ENGINE\nendif$", _makefile_text(), re.MULTILINE), (
        "a plain make variable does not cross into a recipe's environment; "
        "ENGINE must be exported when set so the container-level scripts see it"
    )


def test_help_advertises_the_engine_option() -> None:
    """The live help output carries the OPTIONS row, including the both-orders usage."""
    result = subprocess.run(
        ["make", "help"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=_timeout_seconds(),
    )
    assert result.returncode == 0, f"make help must succeed; stderr={result.stderr[:400]!r}"
    assert "ENGINE=local|<name>" in result.stdout, (
        "the OPTIONS section must advertise ENGINE=local|<name>"
    )
    assert "ENGINE=x make <target>" in result.stdout, (
        "the row must document that the variable works in either order: "
        "ENGINE=x make <target> and make <target> ENGINE=x"
    )


# ---------------------------------------------------------------------------
# The pin, the guard and the delegation. The behavioral tests stand in a
# fake resolver for `devcontainer_config.cli resolve-instance` -- a python3
# earlier on PATH whose block deliberately names a context the pin must
# outlive -- so the property is observed in the running shell code, not just
# grepped out of it. Nothing here reaches docker, AWS or the network: the
# fake answers from a heredoc, and the make-level refusal tests stop at the
# guards before any engine is touched.
# ---------------------------------------------------------------------------

_PINNED_CONTEXT = "pinned-by-engine"
_RESOLVER_CONTEXT = "resolver-context-must-not-win"


def _fake_resolver_block(tmp_path: Path) -> str:
    """The address block a stand-in resolver prints, naming the wrong context.

    The block's shape is `_print_address_block`'s, and the certificate
    directory is real so the id-store read runs for real; the context is the
    one value the pin must not let through.
    """
    certs_dir = tmp_path / "certs"
    certs_dir.mkdir()
    (certs_dir / "instance-id").write_text(_synthetic_instance_id(17) + "\n", encoding="utf-8")
    return (
        "INSTANCE=fake-inst\n"
        f"TERRAGRUNT_DIR={tmp_path / 'tg'}\n"
        "STATE_KEY=fake/inst\n"
        f"DOCKER_CONTEXT={_RESOLVER_CONTEXT}\n"
        "PARAMETER_PREFIX=/devcontainer/fake\n"
        f"CERTS_DIR={certs_dir}\n"
    )


def _fake_resolver_bin(tmp_path: Path, block: str) -> str:
    """A PATH directory whose python3 answers resolve-instance with `block`.

    Any other invocation is forwarded to the real interpreter, so nothing
    else on the machine's tooling is affected. The real interpreter is
    resolved before the directory is prepended anywhere.
    """
    real_python = shutil.which("python3")
    assert real_python is not None, "python3 must be installed to run this suite"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "python3"
    fake.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "-m" ] && [ "$2" = "devcontainer_config.cli" ]'
        ' && [ "$3" = "resolve-instance" ]; then\n'
        "  cat <<'RD_TEST_BLOCK'\n"
        f"{block}"
        "RD_TEST_BLOCK\n"
        "  exit 0\n"
        "fi\n"
        f'exec "{real_python}" "$@"\n',
        encoding="utf-8",
    )
    fake.chmod(0o755)
    return str(bin_dir)


def _run_lib_snippet(path_prepend: str, snippet: str) -> subprocess.CompletedProcess:
    """`snippet` as a bash script that has sourced lib.sh, with `path_prepend` first on PATH.

    The script starts from a clean variable slate (ENGINE, INSTANCE,
    DOCKER_CONTEXT and the resolver guards are unset) so the outer pytest
    environment can never leak into what the assertions observe.
    """
    script = (
        "set -euo pipefail\n"
        "unset ENGINE INSTANCE DOCKER_CONTEXT REMOTE_INSTANCE_ID RD_INSTANCE_RESOLVED\n"
        f'source "{_LIB_SH}"\n'
        f"{snippet}"
    )
    env = dict(os.environ)
    env["PATH"] = f"{path_prepend}:{env.get('PATH', '')}"
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=_timeout_seconds(),
        env=env,
    )


def test_the_resolver_export_loop_leaves_docker_context_alone_under_engine() -> None:
    """The skip is inside the consumption loop and conditioned on ENGINE.

    Without it, exporting the resolver's DOCKER_CONTEXT would re-aim every
    plain docker call and child process at the four-step default's engine
    while rd_docker's `--context` still carried ENGINE's -- the split the
    prelude's pin exists to prevent.
    """
    lib = _lib_text()
    quiet = lib[lib.index("rd_resolve_instance_quiet() {") : lib.index("rd_resolve_instance() {")]
    loop_at = quiet.index("while IFS= read -r line")
    loop = quiet[loop_at : quiet.index("EOF_BLOCK", loop_at)]
    engine_at = loop.index('[ -n "${ENGINE:-}" ]')
    skip_at = loop.index('[ "${line%%=*}" = "DOCKER_CONTEXT" ]')
    continue_at = loop.index("continue")
    assert engine_at < skip_at < continue_at, (
        "the export loop must skip the DOCKER_CONTEXT line -- and only under "
        "ENGINE -- so the resolver can never clobber the prelude's pin"
    )


def test_the_engine_pin_survives_the_resolver(tmp_path: Path) -> None:
    """Behavioral: a pinned run still points at its pin after resolution.

    The stand-in resolver's block names a different context; if the export
    loop ever exported it under ENGINE, the printed pin would be the
    resolver's value instead.
    """
    bin_dir = _fake_resolver_bin(tmp_path, _fake_resolver_block(tmp_path))
    result = _run_lib_snippet(
        bin_dir,
        "DOCKER_CONTEXT=" + _PINNED_CONTEXT + "\n"
        "export DOCKER_CONTEXT\n"
        "ENGINE=fake-engine\n"
        "rd_resolve_instance_quiet\n"
        'printf \'pin=%s instance=%s id=%s\\n\' "$DOCKER_CONTEXT" "$INSTANCE"'
        ' "$REMOTE_INSTANCE_ID"\n',
    )
    assert result.returncode == 0, f"resolution must succeed; stderr={result.stderr[:400]!r}"
    assert f"pin={_PINNED_CONTEXT}" in result.stdout, (
        f"the resolver clobbered the ENGINE pin: stdout={result.stdout!r}"
    )
    assert "instance=fake-inst" in result.stdout, (
        "the rest of the resolved block must still be consumed"
    )
    assert "id=i-" in result.stdout, "the id-store read must still run"


def test_the_resolver_still_exports_docker_context_when_engine_is_unset(tmp_path: Path) -> None:
    """Behavioral regression guard for the skip: unset ENGINE changes nothing.

    The prelude only pins under ENGINE, so the resolver's DOCKER_CONTEXT
    remains the value the ENGINE-unset world has always consumed.
    """
    bin_dir = _fake_resolver_bin(tmp_path, _fake_resolver_block(tmp_path))
    result = _run_lib_snippet(
        bin_dir,
        "ENGINE=\nrd_resolve_instance_quiet\nprintf 'ctx=%s\\n' \"$DOCKER_CONTEXT\"\n",
    )
    assert result.returncode == 0, f"resolution must succeed; stderr={result.stderr[:400]!r}"
    assert f"ctx={_RESOLVER_CONTEXT}" in result.stdout, (
        f"with ENGINE unset the resolver's context must still be exported; got {result.stdout!r}"
    )


def test_container_reasserts_the_engine_pin_after_the_resolver() -> None:
    """The dispatch block closes the clobber window behind the resolver too."""
    text = _container_text()
    dispatch = text[text.index('RDC_COMMAND="${1:-}"') :]
    first_pin_at = dispatch.index('DOCKER_CONTEXT="$(rd_engine_context)"')
    resolve_at = dispatch.index("rd_resolve_instance_quiet")
    reassert_pin_at = dispatch.index('DOCKER_CONTEXT="$(rd_engine_context)"', resolve_at)
    reassert_guard_at = dispatch.rindex('if [ -n "${ENGINE:-}" ]', resolve_at, reassert_pin_at)
    assert first_pin_at < resolve_at < reassert_guard_at < reassert_pin_at, (
        "the prelude must pin before resolving, and the re-assertion -- guarded "
        "by ENGINE -- must come after the resolver, so no export in between can "
        "leave the process split across two engines"
    )


def test_the_agreement_guard_is_checked_at_both_resolution_entries() -> None:
    """rd_engine_context and rd_resolve_instance_quiet both consult the guard.

    Either entry alone would leave a path that resolves one engine's
    addresses while pinning docker to the other.
    """
    lib = _lib_text()
    for function in ("rd_resolve_instance_quiet() {", "rd_engine_context() {"):
        body = _function_body(function[: function.index("(")], text=lib)
        assert "rd_require_engine_instance_agreement" in body, (
            f"{function} must run the INSTANCE/ENGINE agreement guard"
        )


def test_instance_and_engine_naming_different_engines_is_refused(tmp_path: Path) -> None:
    """Behavioral: the refusal names both values and says to set one."""
    bin_dir = _fake_resolver_bin(tmp_path, _fake_resolver_block(tmp_path))
    result = _run_lib_snippet(
        bin_dir,
        "ENGINE=engine-a\nINSTANCE=instance-b\nrd_resolve_instance_quiet\n",
    )
    assert result.returncode != 0, "two different engines in one run must not resolve"
    for fragment in (
        "name different engines",
        "INSTANCE='instance-b'",
        "ENGINE='engine-a'",
        "Set one of the two, not both",
    ):
        assert fragment in result.stderr, f"the refusal must name it: missing {fragment!r}"


def test_instance_and_engine_naming_the_same_engine_is_accepted(tmp_path: Path) -> None:
    """Behavioral: the redundant-but-consistent spelling resolves normally."""
    bin_dir = _fake_resolver_bin(tmp_path, _fake_resolver_block(tmp_path))
    result = _run_lib_snippet(
        bin_dir,
        "ENGINE=fake-inst\n"
        "INSTANCE=fake-inst\n"
        "rd_resolve_instance_quiet\n"
        "printf 'id=%s\\n' \"$REMOTE_INSTANCE_ID\"\n",
    )
    assert result.returncode == 0, (
        f"the same engine named twice must resolve; stderr={result.stderr[:400]!r}"
    )
    assert "id=i-" in result.stdout, f"the block must be consumed; got {result.stdout!r}"


def test_reopen_embeds_the_engine_resolved_context_in_its_authority() -> None:
    """reopen's attached-container authority must name the run's own engine.

    The authority is what VS Code re-attaches to every time the window
    reopens; baking the machine-wide active context into it would point the
    window at whichever engine was active when it was opened, not the one
    this run built the container on. Unset ENGINE must keep the active
    context, exactly as before.
    """
    body = _container_function_body("rdc_reopen")
    engine_at = body.index("rd_engine_context")
    fallback_at = body.index("docker context show")
    assert engine_at < fallback_at, (
        "the authority must take its context from rd_engine_context under "
        "ENGINE, falling back to the active context only when ENGINE is unset"
    )
    assert 'authority_context="$(rd_engine_context)"' in body
    assert '[ -n "$authority_context" ] || authority_context="$(docker context show)"' in body, (
        "an empty rd_engine_context (ENGINE unset) must fall back to the "
        "active context; an empty context in the authority would attach nowhere"
    )


def _make_target_recipe(target: str) -> str:
    """The recipe lines of `target`, read the same way test_makefile_contract reads them."""
    match = re.search(rf"^{target}:(?!=).*\n((?:\t.*\n?)*)", _makefile_text(), re.MULTILINE)
    assert match is not None, f"no {target} target found in Makefile"
    return match.group(1)


def _run_make(target: str, extra_env: dict[str, str]) -> subprocess.CompletedProcess:
    """`make <target>` with the given additions over a cleaned environment.

    MAKEFLAGS/MAKELEVEL are dropped so the child is a top-level make, and
    ENGINE/INSTANCE are dropped so only the values a test passes explicitly
    can drive what is asserted.
    """
    env = dict(os.environ)
    for inherited in ("MAKEFLAGS", "MAKELEVEL", "MFLAGS", "ENGINE", "INSTANCE"):
        env.pop(inherited, None)
    env.update(extra_env)
    return subprocess.run(
        ["make", target],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=_timeout_seconds(),
        env=env,
    )


@pytest.mark.parametrize("target", ["local", "remote", "disconnect"])
def test_context_switching_targets_refuse_under_engine(target: str) -> None:
    """Behavioral: the three context switchers exit 2 under ENGINE, saying why.

    The guard is the first thing each target (or, for local, its disconnect
    prerequisite) runs, so no context is touched and no forward is opened on
    the way to the refusal.
    """
    result = _run_make(target, {"ENGINE": "some-other-engine"})
    assert result.returncode == 2, (
        f"make {target} under ENGINE must exit 2; stderr={result.stderr[:400]!r}"
    )
    assert "switches the machine-wide docker context" in result.stderr
    assert "run it without ENGINE" in result.stderr
    assert "some-other-engine" in result.stderr


def test_every_context_switching_target_carries_the_refusal_in_its_recipe() -> None:
    """Each of the three recipes expands the shared refusal guard, and it exits 2.

    The exit code lives once, in the define's body -- the single source all
    three recipes expand -- not restated per recipe.
    """
    makefile = _makefile_text()
    define_at = makefile.index("define ENGINE_CONTEXT_SWITCH_REFUSAL")
    define_body = makefile[define_at : makefile.index("endef", define_at)]
    assert "exit 2" in define_body, "the refusal must exit 2, matching the usage guards"
    for target in ("local", "remote", "disconnect"):
        recipe = _make_target_recipe(target)
        assert f"ENGINE_CONTEXT_SWITCH_REFUSAL,{target})" in recipe, (
            f"make {target} must carry the ENGINE refusal"
        )


def test_connect_resolves_the_instance_it_is_asked_for() -> None:
    """connect honors INSTANCE (else ENGINE): resolution, then that id/context.

    The remedy `make remote INSTANCE=<name>` stands on this target, so the
    variable must reach a real resolution through lib.sh -- not be accepted
    and ignored while the parse-time config defaults are forwarded.
    """
    recipe = _make_target_recipe("connect")
    instance_at = recipe.index('if [ -n "$(INSTANCE)" ]')
    engine_at = recipe.index('[ "$${ENGINE}" != "local" ]')
    source_at = recipe.index(". $(RD_DIR)/lib.sh")
    resolve_at = recipe.index("rd_resolve_instance")
    default_at = recipe.index('ctx="$(REMOTE_CONTEXT)"')
    resolved_at = recipe.index('--instance-id "$$REMOTE_INSTANCE_ID" --context "$$ctx"')
    assert instance_at < engine_at, (
        "INSTANCE wins the target selection; ENGINE is honored when it names "
        "an instance and INSTANCE is empty"
    )
    assert source_at < resolve_at < default_at < resolved_at, (
        "a named target must resolve through lib.sh -- which reads the "
        "per-instance id store -- and the dispatch must forward that "
        "instance's id and context; the parse-time REMOTE_CONTEXT stays the "
        "unnamed-target default, resolved into ctx before the dispatch"
    )


def test_connect_refuses_an_unknown_instance_before_opening_anything() -> None:
    """Behavioral: `make connect INSTANCE=<unknown>` fails loudly, naming it."""
    result = _run_make("connect", {"INSTANCE": "no-such-instance"})
    assert result.returncode != 0, "an unresolvable INSTANCE must fail the target"
    assert "no-such-instance" in result.stderr, (
        f"the failure must name the instance it could not resolve; stderr={result.stderr[:400]!r}"
    )


def test_remote_passes_the_instance_through_to_connect() -> None:
    """The remedy is real: `make remote INSTANCE=<name>` reaches the resolver.

    Before the delegation this failed with the resolver's default instead of
    the named instance, which is what made the remedy hollow. The sub-make
    needs no explicit hand-off: a command-line INSTANCE definition travels
    inside MAKEFLAGS, and the environment spelling travels in the recipe's
    own environment.
    """
    recipe = _make_target_recipe("remote")
    make_at = recipe.index("$(MAKE) --no-print-directory connect")
    guard_at = recipe.index("ENGINE_CONTEXT_SWITCH_REFUSAL,remote)")
    assert guard_at < make_at, (
        "remote must refuse under ENGINE before delegating, since connect "
        "itself must keep honoring ENGINE"
    )
    result = _run_make("remote", {"INSTANCE": "no-such-instance"})
    assert result.returncode != 0, "an unresolvable INSTANCE must fail the target"
    assert "no-such-instance" in result.stderr, (
        f"the sub-make must carry INSTANCE; stderr={result.stderr[:400]!r}"
    )
    from_env = subprocess.run(
        ["make", "remote"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=_timeout_seconds(),
        env={
            **{
                k: v
                for k, v in os.environ.items()
                if k not in ("MAKEFLAGS", "MAKELEVEL", "MFLAGS", "ENGINE")
            },
            "INSTANCE": "no-such-instance",
        },
    )
    assert from_env.returncode != 0, "the environment spelling must fail the target too"
    assert "no-such-instance" in from_env.stderr, "the environment spelling must reach connect"


def test_connect_refuses_instance_and_engine_naming_different_engines() -> None:
    """Behavioral: the resolver's agreement guard fires through the make target."""
    result = _run_make("connect", {"INSTANCE": "one", "ENGINE": "two"})
    assert result.returncode != 0, "two different engines in one run must not forward"
    assert "name different engines" in result.stderr
    assert "INSTANCE='one'" in result.stderr
    assert "ENGINE='two'" in result.stderr
