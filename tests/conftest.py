"""Shared answers-payload test builders for `tests/test_answers.py`.

`tests/test_answers.py` exercises `devcontainer_config.answers.FIELDS`
directly. Before this extraction it carried its own copy of
`_valid_aws_profile`, `_valid_local_payload` and `_valid_remote_payload`;
those builders now live here so any future consumer under `tests/` can
share them instead of re-declaring the same payload shape.

`_generated_dir` and `_example_root` are shared fixture-tree builders for
`tests/test_render.py` and `tests/test_verify.py`. Both files need an
identical, byte-for-byte-copied checkout of the three real `.example` files
this repository ships; before this extraction each file carried its own
copy of both functions, which risked silently diverging on the next change
to `repo.PRIVATE_FILES` or `repo.example_for`.

`_makefile_text`, `_make_variable` and `_resolve_make_refs` are shared
Makefile-parsing helpers for `tests/test_makefile_contract.py` and
`tests/test_ci_workflow.py`. Both suites need the same `NAME := value`
variable-resolution logic against the repository root `Makefile` --
`tests/test_makefile_contract.py`'s PREREQUISITES-row and guard-loop
assertions, and `tests/test_ci_workflow.py`'s AC-TEST-006 cross-check that
the CI `Install zsh` step and the Makefile's documented Linux package name
cannot drift apart. Before this extraction each file carried its own copy;
`_make_variable` and `_resolve_make_refs` were byte-identical and
`_makefile_text` was functionally identical, so a future change to the
Makefile's assignment syntax would have had to be fixed in two places, and
the AC-TEST-006 drift check itself could have silently mis-resolved while
`tests/test_makefile_contract.py` stayed green.

`_postcreate_text`, `_devcontainer_docs_text`, `_normalize_whitespace`,
`_function_body`, `_provisioning_flow_table` and
`_provisioning_flow_required_steps_prose` are shared
`.devcontainer/.devcontainer.postcreate.sh` / `docs/devcontainer.md` text
extractors for `tests/test_postcreate_hooks.py`, `tests/test_shellrc.py`
and `tests/test_docs_environment_files.py`, plus a planned consumer,
`tests/test_devcontainer_docs.py` (owned by E3-F2-S2-T3, not yet landed).
These suites need a function body pulled out of the same postCreate
script and the same provisioning-flow table and required-step enumeration
pulled out of the same doc section; before this extraction
`tests/test_postcreate_hooks.py` carried its own copy of all five
reader/extractor shapes, one of which (`_function_body`, then named
`_git_hooks_step`'s and `_main_body`'s inline regex) stopped at the first
line beginning with a literal `}` rather than scanning brace depth, so a
function body containing any nested `{ ... }` compound command before its
own closing brace would have been truncated. `_function_body` scans brace
depth instead, so no consumer can silently receive a truncated body again.

`FakeRunner` fakes `devcontainer_config.hostprobe.CommandRunner`, the
runner seam `hostprobe.py` defines and `devcontainer_config.transport`
imports and reuses rather than declaring a second, independent runner
type. Both `tests/test_transport.py` and `tests/test_hostprobe.py` import
this single definition (E6-F2-S1-T1); neither file declares its own copy.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from devcontainer_config import repo


def _synthetic_account_id() -> str:
    """A twelve-digit account-id-shaped value, generated at runtime.

    `devcontainer_config.answers`'s `account_id` field requires exactly this
    shape (twelve digits), which is, by construction, indistinguishable from
    a real AWS account id -- the repo's own secrets scanner
    (`devcontainer_config.secrets._ACCOUNT_ID_PATTERN`) keys on that
    identical shape. Rather than hardcoding a literal that looks like a real
    identifier, this generates a fresh one at runtime, the same way
    `tests/test_secrets.py._sample_account_id` already does for the same
    detector, so no AWS-shaped digit run ever appears in this file's source
    text.
    """
    return str(uuid.uuid4().int)[:12]


def _synthetic_instance_id(hex_length: int) -> str:
    """An `i-`-prefixed instance-id-shaped value with `hex_length` hex chars.

    `devcontainer_config.answers`'s `remote_instance_id` field accepts `i-`
    followed by exactly 8 or 17 lowercase hex characters, which is the exact
    shape `devcontainer_config.secrets._EC2_INSTANCE_ID_PATTERN` keys on.
    Generated at runtime for the same reason `_synthetic_account_id` is: no
    EC2-instance-id-shaped literal appears in source.
    """
    return "i-" + uuid.uuid4().hex[:hex_length]


class FakeRunner:
    """Records every command issued to it and answers from a fixed fixture map.

    `responses` maps an exact command tuple to the `CommandResult` a real
    invocation would have produced; `calls` accumulates `(command, timeout)`
    pairs in issue order, so a test can assert the sequence a probe
    function issued without any of those commands ever reaching a real
    subprocess. A command not present in the fixture map is a
    test-authoring bug, not a hermetic-suite escape hatch, so this raises
    `AssertionError` naming the unexpected command rather than falling back
    to a default result.
    """

    def __init__(self, responses: dict[tuple[str, ...], object]) -> None:
        self._responses = responses
        self.calls: list[tuple[tuple[str, ...], float | None]] = []

    def __call__(self, command: Sequence[str], timeout_seconds: float | None) -> object:
        key = tuple(command)
        self.calls.append((key, timeout_seconds))
        if key not in self._responses:
            raise AssertionError(f"FakeRunner received an unrecorded command: {key!r}")
        return self._responses[key]


_DEFAULT_ACCOUNT_ID: str = _synthetic_account_id()


def _valid_aws_profile(name: str = "dev", account_id: str = _DEFAULT_ACCOUNT_ID) -> dict[str, str]:
    """One well-formed aws_profiles entry, all seven sub-fields present.

    `sso_start_url` uses a non-AWS placeholder domain rather than
    `.devcontainer/aws-profile-map.json.example`'s
    `https://<your-sso-portal>.awsapps.com/start` shape: the secrets
    scanner's `sso-portal-url` detector
    (`devcontainer_config.secrets._SSO_PORTAL_URL_PATTERN`) matches any
    `https://<host>.awsapps.com/start` URL, so that shape cannot appear as a
    source literal here without tripping `lint-secrets` and, transitively,
    `make validate`. `devcontainer_config.answers`'s `sso_start_url` field
    only requires the generic `https://...` shape, not a specific host,
    which is what makes the placeholder domain an *acceptable substitute* --
    not the reason the substitution is required.
    """
    return {
        "name": name,
        "region": "us-east-1",
        "sso_start_url": "https://example-sso.identitycenter.example.com/start",
        "sso_region": "us-east-1",
        "account_name": "dev-account",
        "account_id": account_id,
        "role_name": "AdministratorAccess",
    }


def _valid_local_payload(
    *, aws_config_enabled: bool = False, host_proxy: bool = False
) -> dict[str, Any]:
    """A complete, valid local-backend answer set.

    The ten always-required answers (`devcontainer_config.answers.FIELDS`
    entries with `Requiredness.ALWAYS`), two of which -- `aws_config_enabled`
    and `host_proxy` -- are themselves always-required boolean toggles that
    additionally gate the branch-conditional `aws_profiles` (WHEN_AWS) and
    `host_proxy_url` (WHEN_PROXY) entries, mirroring the branching rule under
    test (AC-FUNC-002).
    """
    payload: dict[str, Any] = {
        "backend": "local",
        "developer_name": "Ada Lovelace",
        "git_user": "ada",
        "git_user_email": "ada@example.com",
        "git_provider_url": "github.com",
        "default_git_branch": "main",
        "template_name": "python-service",
        "aws_config_enabled": aws_config_enabled,
        "local_docker_context": "desktop-linux",
        "host_proxy": host_proxy,
    }
    if aws_config_enabled:
        payload["aws_profiles"] = [_valid_aws_profile()]
    if host_proxy:
        payload["host_proxy_url"] = "http://proxy.example.com:8080"
    return payload


def _valid_remote_payload() -> dict[str, Any]:
    """A complete, valid remote-backend answer set, including remote fields."""
    payload = _valid_local_payload()
    payload["backend"] = "remote"
    payload["remote_instance_id"] = _synthetic_instance_id(17)
    payload["remote_aws_region"] = "us-east-1"
    payload["remote_aws_profile"] = "default"
    payload["remote_ssh_key_path"] = "/home/dev/.ssh/example-key.pem"
    return payload


def _generated_dir(parent: Path, prefix: str) -> Path:
    """A `parent` subdirectory whose name is generated, never hard-coded."""
    generated = parent / f"{prefix}-{uuid.uuid4().hex}"
    generated.mkdir(parents=True)
    return generated


def _example_root(tmp_path: Path) -> Path:
    """A tmp_path checkout root holding copies of the three real `.example` files.

    Copied byte-for-byte from the real checkout (resolved via
    `repo.find_root`) rather than reconstructed as literal strings, so every
    assertion in this file runs against the examples this repository
    actually ships (AC-TEST-001).
    """
    root = _generated_dir(tmp_path, "checkout")
    real_root = repo.find_root(Path(__file__).resolve().parent)
    for relative in repo.PRIVATE_FILES:
        example_relative = repo.example_for(relative)
        source = real_root / example_relative
        destination = root / example_relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source.read_bytes())
    return root


def _makefile_text() -> str:
    """The repository root `Makefile`, read fresh for every call.

    Not cached at module scope: caching would let one test's assertion
    about the file's content leak into another's failure message instead of
    each test reading the file it is actually asserting about.
    """
    root = repo.find_root(Path(__file__).resolve().parent)
    return (root / "Makefile").read_text(encoding="utf-8")


def _make_variable(makefile_text: str, name: str) -> str:
    """The literal value assigned by a `name := value` line in the Makefile.

    Only supports the simple immediate-assignment form the Makefile uses for
    `TEST_PREREQUISITE_TOOLS` and each `TEST_INSTALL_HINT_<tool>` -- the only
    form `_resolve_make_refs` needs to look up.
    """
    match = re.search(r"^" + re.escape(name) + r"\s*:=\s*(.+)$", makefile_text, re.MULTILINE)
    assert match is not None, f"no {name!r} variable assignment found in Makefile"
    return match.group(1).strip()


def _resolve_make_refs(makefile_text: str, text: str) -> str:
    """`text` with every `$(NAME)` token replaced by `NAME`'s Makefile value.

    The Makefile's PREREQUISITES row and its `test:` recipe guard, and CI's
    `Install zsh` step documentation cross-check, all render their
    install-command wording from shared `TEST_INSTALL_HINT_<tool>` Make
    variables (single-sourced in the Makefile, not typed out twice), so text
    captured out of the Makefile's source carries the literal `$(...)` token
    rather than the value. This is a one-level, test-only substitution
    against that source -- not a general Make evaluator -- so a caller still
    asserts on real install-command text, and a reference that came to name
    a different variable would still be caught.
    """

    def _replace(match: re.Match[str]) -> str:
        return _make_variable(makefile_text, match.group(1))

    return re.sub(r"\$\(([A-Za-z0-9_]+)\)", _replace, text)


def _postcreate_text() -> str:
    """`.devcontainer/.devcontainer.postcreate.sh`, read fresh for every call.

    Consumers: `tests/test_postcreate_hooks.py` (every assertion that reads
    the script's text directly, plus `_git_hooks_step` and `_main_body` via
    `_function_body`'s default source) and `tests/test_shellrc.py` (the
    postCreate-wiring assertion that reads the script's text directly, plus
    `_configure_shell_env_body` via `_function_body`'s default source). Not
    cached at module scope, for the same reason `_makefile_text` above is
    not: a cached value would let one test's assertion about the file leak
    into another test's failure message instead of each test reading the
    file it is actually asserting about.
    """
    root = repo.find_root(Path(__file__).resolve().parent)
    return (root / ".devcontainer" / ".devcontainer.postcreate.sh").read_text(encoding="utf-8")


def _devcontainer_docs_text() -> str:
    """`docs/devcontainer.md`, read fresh for every call.

    Consumers: `_provisioning_flow_table` and
    `_provisioning_flow_required_steps_prose` below, which
    `tests/test_postcreate_hooks.py`'s provisioning-flow regression tests
    read through, rather than opening its own copy of the file, plus a
    planned consumer, `tests/test_devcontainer_docs.py` (owned by
    E3-F2-S2-T3, not yet landed).
    """
    root = repo.find_root(Path(__file__).resolve().parent)
    return (root / "docs" / "devcontainer.md").read_text(encoding="utf-8")


def _normalize_whitespace(text: str) -> str:
    """`text` with every run of whitespace, including line breaks, collapsed to one space.

    Consumers: `tests/test_docs_environment_files.py` (`.pymarkdown.json`
    disables MD013, so `docs/environment-files.md` wraps prose at whatever
    width reads well, not at a fixed column) and
    `_provisioning_flow_required_steps_prose` below, which needs the same
    collapse so its enumeration can be split on `; ` without an embedded
    line wrap breaking one item in two.
    """
    return re.sub(r"\s+", " ", text)


def _function_body(name: str, text: str | None = None) -> str:
    """The body of shell function `name() { ... }`, scanned by brace depth.

    Consumers: `tests/test_postcreate_hooks.py`'s `_git_hooks_step` and
    `_main_body`, and `tests/test_shellrc.py`'s `_configure_shell_env_body`
    -- all three read a function body out of
    `.devcontainer/.devcontainer.postcreate.sh` (the default source, read
    fresh via `_postcreate_text`). Scans brace depth from the opening `{`
    of `name() {` to the `}` that returns depth to zero, so a body
    containing any nested `{ ... }` compound command is captured in full.
    The extraction this replaces,
    `r"^name\\(\\)\\s*\\{(.*?)^\\}"` matched with `re.MULTILINE |
    re.DOTALL`, stopped at the first line beginning with a literal `}`,
    which is only ever the function's own closing brace when no nested
    compound command's closing brace happens to sit at column 0. Raises an
    `AssertionError` naming `name` when no such function is defined in the
    scanned text, and again if the braces never balance.

    `text` overrides the source scanned, so a caller (this module's own
    nested-brace correctness test in `tests/test_postcreate_hooks.py`) can
    point the scanner at a synthetic fragment instead of the real script.
    """
    source = _postcreate_text() if text is None else text
    header_match = re.search(rf"^{re.escape(name)}\(\)\s*\{{", source, re.MULTILINE)
    assert header_match is not None, f"no {name}() function found in the scanned script text"
    depth = 1
    index = header_match.end()
    while depth > 0:
        assert index < len(source), f"unbalanced braces scanning the {name}() function body"
        character = source[index]
        if character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
        index += 1
    return source[header_match.end() : index - 1]


def _provisioning_flow_table() -> str:
    """The `| Step | Depends on |` table inside `docs/devcontainer.md`.

    Consumers: `tests/test_postcreate_hooks.py`'s step-order regression
    test, plus a planned consumer, `tests/test_devcontainer_docs.py`'s
    provisioning-flow row assertions (owned by E3-F2-S2-T3, not yet
    landed). Bounded to the table's own rows (from its header row to
    the next blank line) so a `make hooks-install` mention in the
    section's surrounding prose -- the introductory paragraph and the
    `## Git hooks` section both describe the same command -- cannot
    satisfy an assertion meant for the step table itself.
    """
    doc_text = _devcontainer_docs_text()
    match = re.search(
        r"^   \| Step \| Depends on \|\n(.*?)(?=^\n)", doc_text, re.MULTILINE | re.DOTALL
    )
    assert match is not None, "no '| Step | Depends on |' table found in docs/devcontainer.md"
    return match.group(1)


def _provisioning_flow_required_steps_prose() -> str:
    """The sentence enumerating which provisioning steps abort the build.

    Consumers: `tests/test_postcreate_hooks.py`'s required-step-count
    regression test, plus a planned consumer,
    `tests/test_devcontainer_docs.py`'s exit_with_error-enumeration
    assertion (owned by E3-F2-S2-T3, not yet landed). Bounded from "below
    marks `required`:" to "Each of those aborts the build", so a
    `required` mention anywhere else in the file (the table rows
    themselves, or unrelated prose) cannot satisfy an assertion meant for
    this one enumeration. Line wraps are collapsed to single spaces via
    `_normalize_whitespace` so the semicolon-delimited items can be split
    without an embedded newline breaking one in two.
    """
    doc_text = _devcontainer_docs_text()
    match = re.search(
        r"below marks `required`:(.*?)\. Each of those aborts the build",
        doc_text,
        re.DOTALL,
    )
    assert match is not None, "no provisioning-flow exception-step enumeration found"
    return _normalize_whitespace(match.group(1))
