"""Pinning tests for `docs/opencode.md`, the opencode documentation.

The doc's contract with its reader is that it states exactly what ships: the
MCP table names the same server set `.devcontainer/opencode.json`'s own `mcp`
block defines, the two absent servers are named as exclusions, the provider
toggles and both provider key variables appear by name, the SSO-or-static
statement is present where the SSM-backed wrappers are documented, and the
per-user seam names both of its files. Deriving the MCP set from the shipped
config means a server added or removed there without a doc edit fails here by
name, the same two-sided discipline `test_readme_references.py` applies to
make targets and skills, and `test_docs_environment_setup.py` applies to
quoted command output.

The doc names no credential value anywhere: these assertions pin names,
paths and one phrased statement, all of which are committed content by
design.

Hermetic: reads files, parses the shipped JSON, runs nothing, and needs no
docker, AWS or network.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from gitignore_check import repo_root

_DOC_RELATIVE_PATH = "docs/opencode.md"
_SHIPPED_CONFIG_RELATIVE_PATH = ".devcontainer/opencode.json"

_MCP_SECTION_HEADING = "## The MCP servers"
_EXCLUSIONS_HEADING = "### The exclusions"

# The two provider keys the shipped config interpolates; the doc must carry
# both, in interpolation form, the only form the committed config may carry.
_PROVIDER_KEYS = ("ZAI_API_KEY", "OPENROUTER_API_KEY")

# The two provider toggles `devcontainer.json`'s containerEnv block reads.
_PROVIDER_TOGGLES = ("ZAI_ENABLED", "OPENROUTER_ENABLED")

# The statement the MCP section must carry verbatim (normalized whitespace):
# SSO sessions and static parameter values are both valid behind the three
# SSM-backed wrappers, and only an expiring session needs the refresh chain.
_SSO_OR_STATIC_NEEDLE = "SSO and static keys are both supported"

# The two seam files the per-user section must name, with their committed /
# example paths as the doc carries them.
_SEAM_FILES = (".devcontainer/project-setup.sh", ".devcontainer/project-setup.local.sh")


def _doc_text() -> str:
    return (repo_root() / _DOC_RELATIVE_PATH).read_text(encoding="utf-8")


def _doc_text_normalized() -> str:
    return " ".join(_doc_text().split())


def _section_text(heading: str) -> str:
    """The section starting at `heading`, up to the next heading of any level."""
    text = _doc_text()
    match = re.search(rf"^{re.escape(heading)}\n.*?(?=^#+ |\Z)", text, re.MULTILINE | re.DOTALL)
    assert match, f"{_DOC_RELATIVE_PATH} has no {heading!r} section."
    return match.group(0)


def _shipped_mcp_names() -> set[str]:
    """The MCP server names `.devcontainer/opencode.json`'s `mcp` block defines."""
    config = json.loads((repo_root() / _SHIPPED_CONFIG_RELATIVE_PATH).read_text(encoding="utf-8"))
    names = set(config.get("mcp", {}))
    assert names, f"{_SHIPPED_CONFIG_RELATIVE_PATH} defines no mcp servers; the derivation is stale"
    return names


def _mcp_table_names() -> set[str]:
    """The backticked server names in the first column of the doc's MCP table."""
    table_names: set[str] = set()
    for line in _section_text(_MCP_SECTION_HEADING).splitlines():
        if not line.startswith("| `"):
            continue
        first_cell = line.split("|")[1].strip()
        name = first_cell.strip("`")
        if re.fullmatch(r"[a-z0-9-]+", name):
            table_names.add(name)
    return table_names


def test_the_mcp_table_names_exactly_the_shipped_servers() -> None:
    """The doc's MCP table and the shipped config's `mcp` block agree exactly.

    One side reworded or drifted alone fails by name: a server added to the
    config without a doc row fails the missing-half assertion, and a doc row
    for a server the config no longer defines fails the extra-half
    assertion.
    """
    shipped = _shipped_mcp_names()
    documented = _mcp_table_names()
    assert shipped, f"{_SHIPPED_CONFIG_RELATIVE_PATH} defines no mcp servers"
    assert documented, f"{_DOC_RELATIVE_PATH}'s MCP table parsed no server names"
    missing = sorted(shipped - documented)
    extra = sorted(documented - shipped)
    assert not missing, (
        f"{_DOC_RELATIVE_PATH}'s MCP table omits server(s) {_SHIPPED_CONFIG_RELATIVE_PATH} "
        f"ships: {missing!r}"
    )
    assert not extra, (
        f"{_DOC_RELATIVE_PATH}'s MCP table names server(s) {_SHIPPED_CONFIG_RELATIVE_PATH} "
        f"does not define: {extra!r}"
    )


def test_stripe_and_image_gen_are_named_as_exclusions() -> None:
    """The exclusions section names both absent servers, by name, as absent."""
    section = _section_text(_EXCLUSIONS_HEADING).lower()
    for server in ("stripe", "image-gen"):
        assert server in section, (
            f"{_DOC_RELATIVE_PATH}'s exclusions section does not name {server!r}, which the "
            "shipped config deliberately leaves out; the exclusion and its reason must "
            "be documented, not silent"
        )
    assert "absent" in section, (
        f"{_DOC_RELATIVE_PATH}'s exclusions section does not state that the excluded "
        "servers are absent from the shipped config"
    )


def test_the_toggle_variable_names_are_documented() -> None:
    """Both provider toggle names appear in the doc, the names the containerEnv
    block reads from the host shell."""
    text = _doc_text()
    for toggle in _PROVIDER_TOGGLES:
        assert toggle in text, (
            f"{_DOC_RELATIVE_PATH} does not document the toggle {toggle!r}; the reader "
            "could not disable or enable a provider by name"
        )
    assert "${localEnv:" in text, (
        f"{_DOC_RELATIVE_PATH} does not carry the colon-form localEnv note that explains "
        "how devcontainer.json defaults both toggles to on"
    )


def test_both_provider_key_variables_are_documented_in_interpolation_form() -> None:
    """Both provider key variables appear, and each in its `{env:...}` form.

    The interpolation form is the only form the committed config may carry
    (the postcreate asserts it), so the doc naming a key only as a bare
    variable would understate the mechanism the config actually uses.
    """
    text = _doc_text()
    for key in _PROVIDER_KEYS:
        assert key in text, (
            f"{_DOC_RELATIVE_PATH} does not document the provider key variable {key!r}"
        )
        interpolation = "{env:" + key + "}"
        assert interpolation in text, (
            f"{_DOC_RELATIVE_PATH} does not document {key!r} in its interpolation form "
            f"{interpolation!r}, the only form the committed config carries"
        )


def test_the_sso_or_static_statement_is_present() -> None:
    """The MCP section states that SSO and static keys are both supported."""
    section = " ".join(_section_text(_MCP_SECTION_HEADING).split())
    assert _SSO_OR_STATIC_NEEDLE in section, (
        f"{_DOC_RELATIVE_PATH}'s MCP section is missing the SSO-or-static statement "
        f"({_SSO_OR_STATIC_NEEDLE!r}): the SSM-backed wrappers accept whatever the host "
        "CLI resolves, and only an expiring session needs the refresh chain"
    )


# The committed wrapper scripts the shipped config runs MCP servers through,
# and the fail-fast form a wrapper's required-variable check takes (the
# `: "${VAR:?message}"` form, which aborts naming the variable when it is
# unset). The required set is always parsed from the wrappers, never copied.
_WRAPPER_BIN_RELATIVE_PATH = ".devcontainer/opencode/bin"
_WRAPPER_COMMAND_PREFIX = "${WORK_DIR}/.devcontainer/opencode/bin/"
_REQUIRED_VAR_CHECK = r':\s*"\$\{([A-Z][A-Z0-9_]*):\?'


def _wrapper_backed_mcp_names() -> dict[str, str]:
    """Map each MCP the shipped config runs through a committed wrapper to
    its wrapper script name, derived from the config's own command paths."""
    config = json.loads((repo_root() / _SHIPPED_CONFIG_RELATIVE_PATH).read_text(encoding="utf-8"))
    backed: dict[str, str] = {}
    for name, entry in config.get("mcp", {}).items():
        command = entry.get("command", [])
        if command and str(command[0]).startswith(_WRAPPER_COMMAND_PREFIX):
            backed[name] = Path(str(command[0])).name
    assert backed, (
        f"the shipped config defines no wrapper-backed MCP; the {_SHIPPED_CONFIG_RELATIVE_PATH} "
        "command-path derivation is stale"
    )
    return backed


def _mcp_table_row_text(name: str) -> str:
    """The doc's MCP table row for `name`, as one raw markdown line."""
    for line in _section_text(_MCP_SECTION_HEADING).splitlines():
        if line.startswith(f"| `{name}` |"):
            return line
    raise AssertionError(f"{_DOC_RELATIVE_PATH}'s MCP table has no row for {name!r}")


def _wrapper_required_vars(wrapper: str) -> list[str]:
    """The variables `wrapper` requires at spawn, parsed from its own
    required-variable checks rather than a copied list."""
    path = repo_root() / _WRAPPER_BIN_RELATIVE_PATH / wrapper
    assert path.is_file(), f"the wrapper {wrapper} is missing from {_WRAPPER_BIN_RELATIVE_PATH}"
    text = path.read_text(encoding="utf-8")
    return re.findall(_REQUIRED_VAR_CHECK, text)


def test_wrapper_mcp_rows_document_every_required_wrapper_variable() -> None:
    """Each wrapper-backed MCP's doc row names every variable its wrapper
    requires.

    The required set is parsed fresh from the wrapper's own fail-fast checks
    in `.devcontainer/opencode/bin/` (the same checks the
    `test_mcp_wrappers_are_bash_and_parameterized` pins in the wrapper
    suite), so a wrapper that renames or adds a required variable without
    the doc row moving fails here by name. The count guard keeps the check
    from going vacuous if the parse itself stops matching.
    """
    backed = _wrapper_backed_mcp_names()
    checked = 0
    for mcp_name, wrapper in sorted(backed.items()):
        row = _mcp_table_row_text(mcp_name)
        for variable in _wrapper_required_vars(wrapper):
            checked += 1
            assert variable in row, (
                f"the {mcp_name} MCP's doc row does not name {variable}, which the "
                f"{wrapper} wrapper requires through its own fail-fast check"
            )
    assert checked > 0, (
        "the required-variable parse matched no wrapper check at all, so the "
        "cross-check ran over nothing"
    )


def test_the_seam_files_are_documented() -> None:
    """Both per-user seam files appear by path in the doc."""
    text = _doc_text()
    for seam in _SEAM_FILES:
        assert seam in text, (
            f"{_DOC_RELATIVE_PATH} does not name the seam file {seam!r}; the per-user "
            "override path would be undocumented"
        )
