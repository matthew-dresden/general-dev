"""Text-level tests for container.sh's hostcreds push and verify wiring.

container.sh drives docker, so this hermetic suite cannot run it: like
`tests/test_postcreate_hooks.py` (postCreate) and
`tests/test_make_exec.py` (the exec/shell dispatch), every assertion here
reads the script's text and the dispatch structure instead of executing
anything. A live container build is the only way to observe the pushes
land; what these tests pin are the properties that make the pushes safe
when they run:

- the generalization is complete: `rdc_push_creds` replaces
  `rdc_push_git_creds` at every call site, and the old name is gone
  entirely (no caller outside this script needed an alias, so none exists);
- a resolved value never rides a docker exec's argv: each fragment is
  piped from the host-side file over stdin, and the git credential still
  travels inside the heredoc the original fix shipped;
- the `--replace-all` credential.helper fix survives the generalization
  (it is load-bearing: a plain `set` breaks on the second attach);
- every name the CLI prints is re-validated in shell before it becomes
  part of a path, defense in depth against a corrupted list;
- `make verify-container`'s checks exist and are gated exactly as
  designed: git only when the manifest names a git-source host, aws only
  when an aws-export fragment is present.
"""

from __future__ import annotations

import re
from pathlib import Path

from conftest import _function_body

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CONTAINER_SH = _REPO_ROOT / ".devcontainer" / "remote-docker" / "container.sh"
_MAKEFILE = _REPO_ROOT / "Makefile"

_STARTUP_BLOCK_MARKER = "# hostcreds-credential-startup-block"


def _container_sh_text() -> str:
    return _CONTAINER_SH.read_text(encoding="utf-8")


def _makefile_text() -> str:
    return _MAKEFILE.read_text(encoding="utf-8")


def _sh_function_body(name: str) -> str:
    """A container.sh function body, via the shared brace-depth scanner.

    `_function_body` defaults to the postCreate script; passing
    container.sh's own text points the same scanner at this script.
    """
    return _function_body(name, text=_container_sh_text())


def test_container_sh_exists_so_this_module_cannot_pass_vacuously() -> None:
    assert _CONTAINER_SH.is_file(), f"no container.sh at {_CONTAINER_SH}"


def test_dispatch_has_a_verify_case_routing_to_rdc_verify_container() -> None:
    text = _container_sh_text()
    assert re.search(
        r"^\s*verify\)\s+rdc_require_docker && rdc_verify_container ;;", text, re.MULTILINE
    ), "the verify command must dispatch to rdc_verify_container"


def test_dispatch_and_usage_replace_push_git_creds_with_push_creds() -> None:
    text = _container_sh_text()
    assert re.search(
        r"^\s*push-creds\)\s+rdc_require_docker && rdc_push_creds ;;", text, re.MULTILINE
    ), "the push-creds command must dispatch to rdc_push_creds"
    usage = re.search(r"rd_die \"usage:.*\"", text)
    assert usage is not None, "no usage string found in container.sh"
    assert "push-creds" in usage.group(0)
    assert "verify" in usage.group(0)
    assert "push-git-creds" not in usage.group(0)


def test_old_push_git_creds_name_is_gone_entirely() -> None:
    """No external caller needed the alias (verified by grep before the
    rename), so the old name must not survive anywhere in the script."""
    text = _container_sh_text()
    assert "rdc_push_git_creds" not in text
    assert "push-git-creds" not in text


def test_build_and_up_call_rdc_push_creds() -> None:
    for caller in ("rdc_build", "rdc_up"):
        body = _sh_function_body(caller)
        assert re.search(r"^\s*rdc_push_creds\s*$", body, re.MULTILINE), (
            f"{caller} must call rdc_push_creds"
        )


def test_rdc_push_creds_resolves_fragments_on_the_host_through_the_cli() -> None:
    body = _sh_function_body("rdc_push_creds")
    assert "mktemp -d" in body, "the fragment directory must be a fresh temp dir"
    assert "python3 -m devcontainer_config.cli creds-fragments --output-dir" in body
    assert "PYTHONPATH=" in body


def test_fragment_directory_is_removed_by_a_trap_inside_a_subshell() -> None:
    """The trap must not replace the script-level EXIT trap rdc_build_remote
    arms for its override config, so the whole push runs in one subshell."""
    body = _sh_function_body("rdc_push_creds")
    assert "trap 'rm -rf \"$frag_dir\"' EXIT" in body
    assert re.search(r"^\s+\($", body, re.MULTILINE), "the push body must open a subshell"
    assert re.search(r"^\s+\)$", body, re.MULTILINE), "the push body must close its subshell"
    assert "subshell" in body, "the reason the subshell exists must stay documented"


def test_fragments_are_piped_into_the_container_over_stdin_not_argv() -> None:
    body = _sh_function_body("rdc_push_creds")
    # The exec sets a private umask before writing (docker's default 0022
    # would land the fragment at 0644), redirects the host-side fragment
    # file into docker's stdin and lets `cat` write the destination: the
    # value never becomes an argument.
    assert (
        'sh -c "umask 077; cat > \\"\\$HOME/.hostcreds/${name}.env\\"" < "${frag_dir}/${name}.env"'
    ) in body
    # No fragment write may interpolate a file's CONTENT into an argv string.
    for forbidden in ("printf 'https://%s:%s@%s",):
        assert forbidden not in body, (
            f"{forbidden!r} belongs to the git heredoc only, not the fragment path"
        )


def test_every_printed_name_is_revalidated_in_shell_before_becoming_a_path() -> None:
    body = _sh_function_body("rdc_push_creds")
    assert re.search(r"grep -qE '\^\[A-Z\]\[A-Z0-9_\]\*\$'", body), (
        "the name must be re-checked against the anchored [A-Z][A-Z0-9_]* rule"
    )
    assert 'case "$name" in' not in body, (
        "the unanchored glob check must be replaced, not kept beside the grep"
    )


def test_each_written_fragment_is_verified_mode_600_in_the_container() -> None:
    body = _sh_function_body("rdc_push_creds")
    assert "stat -c %a" in body
    assert ".hostcreds/${name}.env" in body
    assert "= 600 ]" in body


def test_store_directory_is_created_idempotently_under_umask_077() -> None:
    body = _sh_function_body("rdc_push_creds")
    assert 'umask 077; mkdir -p "$HOME/.hostcreds"' in body
    assert 'chmod 700 "$HOME/.hostcreds"' in body, (
        "the store directory's 700 mode must be enforced on every push, not only at creation"
    )


def test_git_seeding_is_gated_on_the_manifests_git_hosts() -> None:
    body = _sh_function_body("rdc_push_creds")
    assert "creds-fragments --print-git-hosts" in body
    assert re.search(r'if \[ -n "\$git_hosts" \]; then', body), (
        "the ~/.git-credentials seeding must run only when a git-source entry exists"
    )
    assert "rdc_seed_git_credentials" in body


def test_replace_all_credential_fix_and_its_probe_survive_the_generalization() -> None:
    """The load-bearing fix: --replace-all, not a plain set, plus the
    ls-remote probe that proves the credential actually works."""
    body = _sh_function_body("rdc_seed_git_credentials")
    assert "git config --global --replace-all credential.helper store" in body
    assert "--replace-all" in body
    assert "cannot overwrite multiple values" in body, (
        "the comment explaining why --replace-all is load-bearing must stay"
    )
    assert "git ls-remote origin" in body
    assert ".git-credentials" in body


def test_verify_checks_store_and_fragment_modes() -> None:
    body = _sh_function_body("rdc_verify_container")
    assert 'stat -c %a "$HOME/.hostcreds"' in body
    assert '"$HOME"/.hostcreds/*.env' in body
    assert "= 600 ]" in body
    assert "= 700 ]" in body


def test_verify_checks_the_startup_block_in_both_rc_files() -> None:
    body = _sh_function_body("rdc_verify_container")
    assert _STARTUP_BLOCK_MARKER in body
    assert ".bashrc" in body
    assert ".zshenv" in body


def test_verify_runs_an_interactive_zsh_and_fails_only_on_hostcreds_stderr() -> None:
    body = _sh_function_body("rdc_verify_container")
    assert "zsh -ic 'exit 0'" in body
    assert "grep -q hostcreds" in body


def test_verify_gates_git_on_print_git_hosts_and_aws_on_fragment_presence() -> None:
    body = _sh_function_body("rdc_verify_container")
    assert "creds-fragments --print-git-hosts" in body
    assert re.search(r'if \[ -n "\$git_hosts" \]; then', body)
    assert "git ls-remote origin" in body
    # aws: presence of an aws-export fragment detected by variable NAME only
    assert "grep -l AWS_ACCESS_KEY_ID" in body
    assert "aws sts get-caller-identity --output text --no-cli-pager" in body


def test_verify_counts_failures_and_exits_nonzero_on_any() -> None:
    body = _sh_function_body("rdc_verify_container")
    assert "rdc_verify_one" in body
    assert re.search(r'if \[ "\$failures" -gt 0 \]; then', body)
    assert "rd_fail" in body


def test_makefile_delegates_push_creds_and_verify_container_to_container_sh() -> None:
    text = _makefile_text()
    push = re.search(r"^push-creds:\n((?:\t.*\n)+)", text, re.MULTILINE)
    assert push is not None, "no push-creds target in the Makefile"
    assert "$(CONTAINER_SH) push-creds" in push.group(1)
    verify = re.search(r"^verify-container:\n((?:\t.*\n)+)", text, re.MULTILINE)
    assert verify is not None, "no verify-container target in the Makefile"
    assert 'INSTANCE="$(INSTANCE)" $(CONTAINER_SH) verify' in verify.group(1)
    assert "push-git-creds" not in text, "the old target must be replaced, not kept beside"


def test_makefile_creds_init_runs_the_cli_subcommand_on_the_host() -> None:
    text = _makefile_text()
    creds = re.search(r"^creds-init:\n((?:\t.*\n)+)", text, re.MULTILINE)
    assert creds is not None, "no creds-init target in the Makefile"
    recipe = creds.group(1)
    assert "PYTHONPATH=$(DEVCONTAINER_SCRIPTS_DIR)" in recipe
    assert "python3 -m devcontainer_config.cli creds-init" in recipe
