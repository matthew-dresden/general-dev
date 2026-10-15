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


def test_the_seam_files_are_documented() -> None:
    """Both per-user seam files appear by path in the doc."""
    text = _doc_text()
    for seam in _SEAM_FILES:
        assert seam in text, (
            f"{_DOC_RELATIVE_PATH} does not name the seam file {seam!r}; the per-user "
            "override path would be undocumented"
        )
