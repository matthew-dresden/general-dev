"""Pinning tests for the exact-output and exact-identifier claims of
`docs/environment-setup.md`, the fresh-machine runbook.

The runbook's contract with its reader is that its quoted output is what the
tools actually print: `make init`'s per-file `created` / `already exists, left
untouched` / `[DONE] no placeholders left` lines, `creds-init`'s
`stored: <names>` / `already present: <names>` summary, the build's
`pushed <N> credential fragment(s) into ~/.hostcreds` and `container is up`,
and the fragment renderer's `notice: <NAME> expired; refresh with: make
push-creds`. Each of those quotes is produced by a printf, an f-string, or a
recipe line in this repository -- in the root `Makefile`,
`devcontainer_config.cli`, `devcontainer_config.hostcreds`, or
`.devcontainer/remote-docker/container.sh` -- and none of those producers
knows the runbook exists. A quiet edit on either side (rewording a printf,
re-quoting a line from memory) would leave the runbook teaching a command
output no tool prints, and nothing in the suite would notice. Every test here
reads both sides -- the rendered runbook text and the producing source -- so a
drift in either direction fails the assertion that names the claim.

The same two-sided discipline pins the runbook's exact identifiers: every
`make <target>` it names must exist as a target in the root Makefile; the four
gitignored files its step 2 lists must be exactly the
`PRIVATE_FILES_AND_MANIFEST` union the `init` recipe iterates; the flag
documentation (`--stdin`, `--print-git-hosts`, `CREDS_INIT_ARGS`) must exist in
both the runbook and the `cli.py`/Makefile declarations; the keychain service
convention `devcontainer/<project>/<NAME>` must be what `hostcreds` actually
applies as the default (proved behaviorally, through `load_manifest` against a
temporary checkout -- a pure file-read path, no subprocess); the reserved
credential names and the `[A-Z][A-Z0-9_]*` name shape it states must match
`hostcreds`'s own constants; every relative link and named sibling section it
references must resolve to a real file and heading; and the expiry notice its
troubleshooting section quotes must be a verbatim substring of the fragment
`render_env_fragment` renders for a synthetic aws-export credential.

Two observations from writing these pins, recorded here rather than papered
over: the runbook never quotes `--output-dir` (it uses `creds-fragments` only
in its `--print-git-hosts` mode, which per `cli.py`'s own help text requires no
output directory), so that flag is pinned in `cli.py` alone; and the runbook
spells the automation flag with a concrete name (`--stdin ZAI_API_KEY`) rather
than the metavar form `--stdin NAME`, which appears in the Makefile's
`creds-init` help line and in `docs/environment-files.md` -- so the metavar
form is pinned where it actually lives, against `cli.py`'s
`add_argument("--stdin", metavar="NAME", ...)` declaration. Neither runbook
statement is false; the runbook simply does not carry those two spellings.

The module is hermetic in the same sense the sibling doc-pinning suites are:
plain file reads, regex parsing of the rendered text, and in-process imports
only. No make, no docker, no `security`, no aws CLI, no subprocess and no
network is ever invoked; `load_manifest` and `render_env_fragment` are pure
functions of their inputs, pointed at a `tmp_path` checkout and a synthetic,
non-secret-shaped credential document.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from conftest import _makefile_text, _normalize_whitespace
from devcontainer_config import hostcreds
from gitignore_check import repo_root

_RUNBOOK_RELATIVE_PATH = "docs/environment-setup.md"
_CLI_RELATIVE_PATH = ".claude/plugins/devcontainer/scripts/devcontainer_config/cli.py"
_HOSTCREDS_RELATIVE_PATH = ".claude/plugins/devcontainer/scripts/devcontainer_config/hostcreds.py"
_CONTAINER_SH_RELATIVE_PATH = ".devcontainer/remote-docker/container.sh"

# The runbook sections whose own text carries the claims pinned below.
# Scoping a needle to its section (the technique
# `tests/test_docs_environment_files.py` documents) means the same words
# appearing elsewhere in the runbook cannot satisfy an assertion meant for
# that section's quote.
_PRIVATE_FILES_SECTION_HEADING = "## 2. Create the private files"
_FILL_SECTION_HEADING = "## 3. Fill the three configuration files"
_MANIFEST_SECTION_HEADING = "## 4. Author the hostcreds manifest"
_STORE_SECTION_HEADING = "## 5. Store the keychain items"
_BUILD_SECTION_HEADING = "## 8. Build the container"
_TROUBLESHOOTING_SECTION_HEADING = "## Troubleshooting"

# Every `make <target>` the runbook names must resolve to a target in the
# root Makefile. This floor is the parser's own self-check: if a reformat of
# the runbook ever starves the `make <target>` extraction below, the missing
# floor names the extraction as stale instead of letting an empty set pass
# vacuously.
_REQUIRED_RUNBOOK_TARGETS: tuple[str, ...] = (
    "init",
    "keybindings",
    "local",
    "build",
    "reopen",
    "exec",
    "verify-container",
    "creds-init",
    "push-creds",
    "status",
    "stop",
    "start",
)

# One case per exact-output line the runbook quotes from `make init`. Each
# case carries the section whose prose quotes it and the needle that must
# appear in the ANSI-stripped `init:` recipe: the per-file success and
# idempotence printfs are pinned in their printf-format shape (`%-44s ...`),
# because a bare `created` needle would also be satisfied by the missing-
# example error line's "cannot be created" and so could never notice that
# line's wording drifting from the success line it must stay distinct from.
_INIT_OUTPUT_CLAIMS: tuple[tuple[str, str, str, str], ...] = (
    (
        "created",
        _PRIVATE_FILES_SECTION_HEADING,
        "created",
        "%-44s created\\n",
    ),
    (
        "already_exists_left_untouched",
        _PRIVATE_FILES_SECTION_HEADING,
        "already exists, left untouched",
        "%-44s already exists, left untouched\\n",
    ),
    (
        "done_no_placeholders_left",
        _FILL_SECTION_HEADING,
        "[DONE] no placeholders left",
        "[DONE] no placeholders left",
    ),
)

# One case per summary line the runbook quotes from `creds-init`. The cli
# needle is the f-string body itself, so the pin covers the printed prefix
# and the ", "-joined name list shape together.
_CREDS_INIT_OUTPUT_CLAIMS: tuple[tuple[str, str, str], ...] = (
    ("stored", "stored: <names>", "stored: {', '.join(stored)}"),
    ("already_present", "already present: <names>", "already present: {', '.join(present)}"),
)

# One case per flag the runbook documents, naming every source whose text
# must carry the flag's spelling. `--output-dir` is deliberately absent from
# the runbook column (see the module docstring): the runbook drives
# `creds-fragments` only through `--print-git-hosts`, so the output-dir flag
# has its own cli-only test below.
_RUNBOOK_KEY = "runbook"
_CLI_KEY = "cli"
_MAKEFILE_KEY = "makefile"

_FLAG_LOCATIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("--stdin", (_RUNBOOK_KEY, _CLI_KEY, _MAKEFILE_KEY)),
    ("--print-git-hosts", (_RUNBOOK_KEY, _CLI_KEY)),
    ("CREDS_INIT_ARGS", (_RUNBOOK_KEY, _MAKEFILE_KEY)),
)

# The build-output lines the runbook's step 8 tells the reader to expect,
# each with the substring `container.sh` must carry. The pushed-fragments
# line interpolates the fragment count at runtime (`pushed $(...) credential
# fragment(s) ...`), so the pin carries the literal text around the count
# rather than a count the runbook itself renders as `<N>`.
_BUILD_OUTPUT_CLAIMS: tuple[tuple[str, str, str], ...] = (
    (
        "pushed_fragments",
        "pushed <N> credential fragment(s) into ~/.hostcreds",
        "credential fragment(s) into ~/.hostcreds",
    ),
    ("container_is_up", "container is up", "container is up"),
)

# The runbook's statement of the default keychain service, with the same
# placeholders its own prose defines (`<project>` is the checkout directory's
# name, `<NAME>` the credential name).
_KEYCHAIN_SERVICE_TEMPLATE = "devcontainer/<project>/<NAME>"

# The expiry notice the troubleshooting section quotes, `<NAME>` being the
# credential's manifest name.
_EXPIRY_NOTICE_TEMPLATE = "notice: <NAME> expired; refresh with: make push-creds"

# The keychain-resolution abort the troubleshooting section quotes.
_KEYCHAIN_ABORT_TEMPLATE = "cannot resolve <NAME> from the keychain source"

# The printf escape spellings the Makefile source carries (`\033[0;32m` as
# literal text); stripping them recovers the output a terminal actually
# renders, which is what the runbook quotes.
_ANSI_SPELLING_PATTERN = re.compile(r"\\033\[[0-9;]*m")


def _read_repo_text(relative_path: str) -> str:
    path = repo_root() / relative_path
    assert path.is_file(), f"{relative_path} does not exist under {repo_root()}"
    return path.read_text(encoding="utf-8")


def _runbook_path() -> Path:
    return repo_root() / _RUNBOOK_RELATIVE_PATH


def _runbook_text() -> str:
    return _runbook_path().read_text(encoding="utf-8")


def _runbook_text_normalized() -> str:
    return _normalize_whitespace(_runbook_text())


def _cli_text() -> str:
    return _read_repo_text(_CLI_RELATIVE_PATH)


def _hostcreds_source_text() -> str:
    return _read_repo_text(_HOSTCREDS_RELATIVE_PATH)


def _container_sh_text() -> str:
    return _read_repo_text(_CONTAINER_SH_RELATIVE_PATH)


def _section_text(heading: str) -> str:
    """The runbook section starting at `heading`, up to the next `##` or EOF."""
    match = re.search(
        rf"^{re.escape(heading)}\n.*?(?=^## |\Z)", _runbook_text(), re.MULTILINE | re.DOTALL
    )
    assert match is not None, f"{_RUNBOOK_RELATIVE_PATH} has no {heading!r} section."
    return match.group(0)


def _section_text_normalized(heading: str) -> str:
    return _normalize_whitespace(_section_text(heading))


def _make_question_variable(makefile_text: str, name: str) -> str:
    """The literal value of a `name ?= value` line in the root Makefile.

    The Makefile assigns `PRIVATE_FILES` and `PRIVATE_FILES_AND_MANIFEST`
    with the `?=` form, which `conftest._make_variable`'s `:=` reader does
    not cover; this is the same one-line extraction for that form.
    """
    match = re.search(rf"^{re.escape(name)}\s*\?=\s*(.+)$", makefile_text, re.MULTILINE)
    assert match is not None, f"no {name!r} ?= assignment found in the Makefile"
    return match.group(1).strip()


def _makefile_targets() -> set[str]:
    """Every target name defined at column 0 in the root Makefile.

    The first character must be a letter, which excludes the `.PHONY:` and
    `.DEFAULT_GOAL :=` lines, and the colon must sit immediately after the
    name, which excludes every variable assignment (`NAME :=`, `NAME ?=`,
    `NAME =` all carry a space before their operator).
    """
    targets = set(re.findall(r"^([a-zA-Z][a-zA-Z0-9_.-]*):(?!=)", _makefile_text(), re.MULTILINE))
    assert targets, "no make targets found in the Makefile; the extraction may be stale."
    return targets


def _runbook_named_targets() -> set[str]:
    """Every `make <target>` the runbook names, extracted from its own text."""
    targets = set(re.findall(r"\bmake ([a-z][a-z0-9-]*)", _runbook_text_normalized()))
    missing_floor = set(_REQUIRED_RUNBOOK_TARGETS) - targets
    assert not missing_floor, (
        f"{_RUNBOOK_RELATIVE_PATH} no longer names make target(s) {sorted(missing_floor)!r}; "
        "the `make <target>` extraction has gone stale against the runbook's own text."
    )
    return targets


def _init_recipe_text() -> str:
    """The tab-indented recipe lines that follow the Makefile's `init:` header."""
    match = re.search(r"^init:\n((?:\t.*\n?)*)", _makefile_text(), re.MULTILINE)
    assert match is not None, "no init target found in the Makefile"
    return match.group(1)


def _init_recipe_text_rendered() -> str:
    """The `init:` recipe with the Makefile's ANSI escape spellings removed.

    The runbook quotes what `make init` prints on a terminal; the recipe
    source carries those same lines with literal `\\033[...m` color escapes
    around them, which the reader never sees. Stripping the escapes before
    matching is what lets the pin run against the rendered output's text.
    """
    return _ANSI_SPELLING_PATTERN.sub("", _init_recipe_text())


def _runbook_init_file_list() -> set[str]:
    """The gitignored files the runbook's step 2 bullet list names."""
    names = re.findall(r"^- `([^`]+)`", _section_text(_PRIVATE_FILES_SECTION_HEADING), re.MULTILINE)
    assert names, (
        f"{_RUNBOOK_RELATIVE_PATH}'s {_PRIVATE_FILES_SECTION_HEADING!r} section lists no "
        "backticked bullet items; the file-list extraction may be stale."
    )
    return set(names)


def _makefile_private_files_union() -> set[str]:
    """The `PRIVATE_FILES_AND_MANIFEST` union, with `$(PRIVATE_FILES)` resolved.

    One level of substitution, the same shape `conftest._resolve_make_refs`
    performs, is enough because the Makefile itself defines the union as
    exactly `$(PRIVATE_FILES) .devcontainer/hostcreds.map.json` -- and the
    `init` recipe iterates this union, so this set is what `make init`
    creates from examples.
    """
    makefile = _makefile_text()
    private_files = _make_question_variable(makefile, "PRIVATE_FILES")
    union = _make_question_variable(makefile, "PRIVATE_FILES_AND_MANIFEST")
    resolved = union.replace("$(PRIVATE_FILES)", private_files)
    names = set(resolved.split())
    assert names, (
        "PRIVATE_FILES_AND_MANIFEST resolved to no file names; the `?=` extraction or the "
        "$(PRIVATE_FILES) substitution may be stale."
    )
    return names


def test_runbook_make_targets_exist_in_the_makefile() -> None:
    """Every `make <target>` the runbook names must be a real Makefile target.

    The runbook's steps are only followable if each command they state names
    a target this repository actually defines; a renamed or removed target
    whose mention survives in the runbook strands a fresh machine mid-sequence
    with no remedy in the document.
    """
    named = _runbook_named_targets()
    defined = _makefile_targets()
    missing = sorted(named - defined)
    assert not missing, (
        f"{_RUNBOOK_RELATIVE_PATH} names make target(s) {missing!r} that the root Makefile "
        f"does not define. Defined targets read: {sorted(defined)!r}."
    )


@pytest.mark.parametrize(
    "claim_id,section_heading,runbook_needle,recipe_needle",
    _INIT_OUTPUT_CLAIMS,
    ids=[claim_id for claim_id, _, _, _ in _INIT_OUTPUT_CLAIMS],
)
def test_init_output_claims_quoted_in_the_runbook_appear_in_the_init_recipe(
    claim_id: str, section_heading: str, runbook_needle: str, recipe_needle: str
) -> None:
    """The output lines the runbook quotes from `make init` are what it prints.

    Checked on both sides: the runbook's own section must still carry the
    quote, and the `init:` recipe -- with its ANSI escapes stripped -- must
    print it, so the test fails on a runbook requote from memory and on a
    printf reword alike.
    """
    section = _section_text_normalized(section_heading)
    assert runbook_needle in section, (
        f"{_RUNBOOK_RELATIVE_PATH}'s {section_heading!r} section no longer quotes the "
        f"`make init` output {runbook_needle!r} (claim {claim_id!r})."
    )
    recipe = _init_recipe_text_rendered()
    assert recipe_needle in recipe, (
        f"the Makefile's `init:` recipe no longer prints {recipe_needle!r}, which "
        f"{_RUNBOOK_RELATIVE_PATH} tells the reader to expect from `make init` "
        f"(claim {claim_id!r}); rendered recipe text read: {recipe!r}."
    )


@pytest.mark.parametrize(
    "claim_id,runbook_needle,cli_needle",
    _CREDS_INIT_OUTPUT_CLAIMS,
    ids=[claim_id for claim_id, _, _ in _CREDS_INIT_OUTPUT_CLAIMS],
)
def test_creds_init_output_claims_quoted_in_the_runbook_appear_in_cli_source(
    claim_id: str, runbook_needle: str, cli_needle: str
) -> None:
    """The summary lines the runbook quotes from `creds-init` are what cli prints.

    `_run_creds_init` prints `stored: ...` / `already present: ...` as the
    f-strings pinned here; the runbook's step 5 verify line quotes the same
    prefixes. Either side reworded alone fails this by name.
    """
    section = _section_text_normalized(_STORE_SECTION_HEADING)
    assert runbook_needle in section, (
        f"{_RUNBOOK_RELATIVE_PATH}'s {_STORE_SECTION_HEADING!r} section no longer quotes "
        f"the `creds-init` summary {runbook_needle!r} (claim {claim_id!r})."
    )
    source = _cli_text()
    assert cli_needle in source, (
        f"devcontainer_config.cli no longer prints {cli_needle!r}, which "
        f"{_RUNBOOK_RELATIVE_PATH} tells the reader to expect from `make creds-init` "
        f"(claim {claim_id!r})."
    )


@pytest.mark.parametrize(
    "flag,expected_sources",
    _FLAG_LOCATIONS,
    ids=[flag for flag, _ in _FLAG_LOCATIONS],
)
def test_flag_documentation_appears_in_runbook_and_producing_source(
    flag: str, expected_sources: tuple[str, ...]
) -> None:
    """Each documented flag must exist in the runbook and its declaring source.

    A flag the CLI renames or drops strands the runbook's verify command
    (step 4 names `--print-git-hosts`; step 5 names `--stdin` through
    `CREDS_INIT_ARGS`) with no working spelling, and a runbook that stops
    naming a flag the Makefile still passes through leaves the automation
    path undocumented.
    """
    for source in expected_sources:
        if source == _RUNBOOK_KEY:
            text = _runbook_text_normalized()
            origin = _RUNBOOK_RELATIVE_PATH
        elif source == _CLI_KEY:
            text = _cli_text()
            origin = _CLI_RELATIVE_PATH
        else:
            text = _makefile_text()
            origin = "the root Makefile"
        assert flag in text, (
            f"flag {flag!r} is missing from {origin}; the runbook's flag documentation and "
            "the producing source must carry the same spelling."
        )


def test_stdin_metavar_form_is_declared_in_cli_and_quoted_in_the_makefile_help() -> None:
    """The `--stdin NAME` metavar form matches cli's declaration and the Makefile help.

    The runbook itself spells the flag with a concrete name
    (`CREDS_INIT_ARGS='--stdin ZAI_API_KEY'`); the metavar form lives in the
    Makefile's `creds-init` help line and in `docs/environment-files.md`.
    This pins the metavar form to the two sources that actually carry it, so
    a metavar rename in `cli.py` cannot desynchronize the help lines that
    teach it.
    """
    source = _cli_text()
    assert '"--stdin", metavar="NAME"' in source, (
        f"{_CLI_RELATIVE_PATH} no longer declares --stdin with the NAME metavar; the "
        "'--stdin NAME' spelling quoted by the Makefile's creds-init help line would "
        "then describe a flag shape the parser does not accept."
    )
    makefile = _makefile_text()
    assert "CREDS_INIT_ARGS='--stdin NAME'" in makefile, (
        "the Makefile's creds-init help line no longer quotes CREDS_INIT_ARGS='--stdin NAME'; "
        "the automation spelling it teaches must keep matching cli.py's declaration."
    )
    assert "$(CREDS_INIT_ARGS)" in makefile, (
        "the Makefile's creds-init recipe no longer passes $(CREDS_INIT_ARGS) through to "
        "devcontainer_config.cli; the documented automation path would be dead."
    )


def test_creds_fragments_output_dir_flag_is_declared_in_cli_source() -> None:
    """`--output-dir` exists where the fragment-writing mode is implemented.

    The runbook does not quote this flag: it drives `creds-fragments` only
    through `--print-git-hosts`, which per cli's own help text requires no
    output directory. The flag is still pinned in its declaring source so its
    removal (which `container.sh`'s fragment push depends on) cannot land
    silently behind a doc suite that never mentions it.
    """
    source = _cli_text()
    assert '"--output-dir"' in source, (
        f"{_CLI_RELATIVE_PATH} no longer declares --output-dir for creds-fragments; the "
        "fragment-writing half of the push the runbook describes in step 8 would then "
        "have no writable destination flag."
    )


def test_keychain_service_convention_matches_hostcreds_default(tmp_path: Path) -> None:
    """`devcontainer/<project>/<NAME>` is the service `load_manifest` defaults to.

    Behavioral, not textual: a temporary checkout's manifest with one
    keychain entry is loaded in-process (a pure file read -- no subprocess),
    and the default service label must equal the runbook's own template with
    its documented placeholders filled in. A change to either the convention
    string in the runbook or `hostcreds`'s default construction fails here.
    """
    runbook = _runbook_text_normalized()
    assert _KEYCHAIN_SERVICE_TEMPLATE in runbook, (
        f"{_RUNBOOK_RELATIVE_PATH} no longer states the keychain service convention "
        f"{_KEYCHAIN_SERVICE_TEMPLATE!r}."
    )
    name = "EXAMPLE_API_TOKEN"
    manifest_dir = tmp_path / ".devcontainer"
    manifest_dir.mkdir()
    (manifest_dir / "hostcreds.map.json").write_text(
        json.dumps({name: {"source": "keychain"}}), encoding="utf-8"
    )
    specs = hostcreds.load_manifest(tmp_path)
    assert len(specs) == 1, (
        f"load_manifest returned {len(specs)} spec(s) for a one-entry manifest; the "
        "temporary fixture may be stale."
    )
    service = specs[0].labels[hostcreds.KEYCHAIN_SERVICE_LABEL]
    expected = _KEYCHAIN_SERVICE_TEMPLATE.replace("<project>", tmp_path.name).replace(
        "<NAME>", name
    )
    assert service == expected, (
        f"hostcreds default keychain service is {service!r} but the runbook's convention "
        f"{_KEYCHAIN_SERVICE_TEMPLATE!r} resolves to {expected!r} for project "
        f"{tmp_path.name!r}, credential {name!r}."
    )


def test_runbook_relative_links_resolve() -> None:
    """Every relative link target the runbook references exists in the checkout.

    The runbook deliberately points at the reference docs instead of
    repeating them; a renamed or moved sibling would leave each pointer dead
    for exactly the reader the runbook exists for.
    """
    targets = re.findall(r"\]\(([^)]+)\)", _runbook_text())
    assert "environment-files.md" in targets and "devcontainer.md" in targets, (
        f"{_RUNBOOK_RELATIVE_PATH} no longer links its two reference docs; the link "
        f"extraction may be stale. Links read: {targets!r}."
    )
    docs_dir = _runbook_path().parent
    for target in targets:
        resolved = docs_dir / target
        assert resolved.is_file(), (
            f"{_RUNBOOK_RELATIVE_PATH} links {target!r}, which does not exist at "
            f"{resolved} (links resolve relative to the runbook's own directory)."
        )


# The sibling sections the runbook names in prose (not as markdown links):
# "the README's 'Quick start, remote' section", "environment-files.md's
# 'Host credentials (hostcreds)' section", "devcontainer.md's 'Certificate
# lifecycle' section". A heading that moves or retitles strands the pointer.
_NAMED_SECTION_REFERENCES: tuple[tuple[str, str], ...] = (
    ("README.md", "## Quick start, remote"),
    ("docs/environment-files.md", "### Host credentials (hostcreds)"),
    ("docs/devcontainer.md", "## Certificate lifecycle"),
)


@pytest.mark.parametrize(
    "relative_path,heading",
    _NAMED_SECTION_REFERENCES,
    ids=[relative_path for relative_path, _ in _NAMED_SECTION_REFERENCES],
)
def test_named_sibling_doc_sections_exist(relative_path: str, heading: str) -> None:
    """Each section the runbook names in prose still exists under that heading."""
    text = _read_repo_text(relative_path)
    assert heading in text, (
        f"{_RUNBOOK_RELATIVE_PATH} refers readers to {relative_path}'s {heading!r} section, "
        "which no longer exists under that heading."
    )


def test_init_creates_exactly_the_four_gitignored_files_the_runbook_lists() -> None:
    """Step 2's file list is exactly the `PRIVATE_FILES_AND_MANIFEST` union.

    The runbook promises `make init` copies four named files and nothing
    else; the recipe iterates the Makefile's union variable. A file added to
    one side only makes the two sets differ here, and a fifth file silently
    joining either side fails the count assertion before the set comparison.
    """
    documented = _runbook_init_file_list()
    created = _makefile_private_files_union()
    assert len(created) == 4, (
        f"the Makefile's PRIVATE_FILES_AND_MANIFEST union has {len(created)} entries, not "
        f"the four the runbook promises: {sorted(created)!r}."
    )
    assert len(documented) == 4, (
        f"{_RUNBOOK_RELATIVE_PATH}'s {_PRIVATE_FILES_SECTION_HEADING!r} section lists "
        f"{len(documented)} files, not four: {sorted(documented)!r}."
    )
    assert documented == created, (
        f"{_RUNBOOK_RELATIVE_PATH} step 2 lists {sorted(documented)!r} but the init recipe "
        f"creates {sorted(created)!r}; the two must be the same four gitignored files."
    )
    section = _section_text_normalized(_PRIVATE_FILES_SECTION_HEADING)
    assert "four gitignored files" in section, (
        f"{_RUNBOOK_RELATIVE_PATH}'s {_PRIVATE_FILES_SECTION_HEADING!r} section no longer "
        "states the count of four its own list is pinned against."
    )


def test_expiry_notice_quoted_in_troubleshooting_matches_render_env_fragment() -> None:
    """The quoted expiry notice is verbatim what a rendered fragment prints.

    The troubleshooting section's symptom quote must be a substring of the
    fragment `render_env_fragment` actually renders for an expiring
    credential -- proved here against a synthetic aws-export credential whose
    document is plainly non-secret and whose expiry is generated at runtime.
    A rewording of `_expiry_guarded`'s notice line, or of the runbook's
    quote, fails the side that drifted.
    """
    section = _section_text_normalized(_TROUBLESHOOTING_SECTION_HEADING)
    assert _EXPIRY_NOTICE_TEMPLATE in section, (
        f"{_RUNBOOK_RELATIVE_PATH}'s {_TROUBLESHOOTING_SECTION_HEADING!r} section no longer "
        f"quotes the expiry notice {_EXPIRY_NOTICE_TEMPLATE!r}."
    )
    name = "EXAMPLE_SESSION"
    spec = hostcreds.CredentialSpec(
        name=name,
        source=hostcreds.SOURCE_AWS_EXPORT,
        labels={hostcreds.AWS_PROFILE_LABEL: hostcreds.DEFAULT_AWS_PROFILE},
    )
    expires_at = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    document = json.dumps(
        {
            hostcreds.AWS_ACCESS_KEY_FIELD: "example-access-key-id",
            hostcreds.AWS_SECRET_KEY_FIELD: "example-secret-access-key",
            hostcreds.AWS_SESSION_TOKEN_FIELD: "example-session-token",
            hostcreds.AWS_EXPIRATION_FIELD: expires_at,
        }
    )
    credential = hostcreds.ResolvedCredential(
        spec=spec, value=document, username=None, expires_at=expires_at
    )
    rendered = hostcreds.render_env_fragment(credential)
    expected_notice = _EXPIRY_NOTICE_TEMPLATE.replace("<NAME>", name)
    assert expected_notice in rendered, (
        f"render_env_fragment no longer emits {expected_notice!r}; the runbook's "
        "troubleshooting section quotes that notice verbatim as the symptom of an expired "
        f"AWS credential. Rendered fragment read: {rendered!r}."
    )


@pytest.mark.parametrize(
    "claim_id,runbook_needle,container_sh_needle",
    _BUILD_OUTPUT_CLAIMS,
    ids=[claim_id for claim_id, _, _ in _BUILD_OUTPUT_CLAIMS],
)
def test_build_output_claims_quoted_in_the_runbook_appear_in_container_sh(
    claim_id: str, runbook_needle: str, container_sh_needle: str
) -> None:
    """Step 8's verify quotes (`pushed <N> ...`, `container is up`) are what prints.

    The build's success output lives in `container.sh`, one refactor away
    from disagreeing with the runbook's step 8 verify line; both sides are
    read here so the drift fails whichever file moved.
    """
    section = _section_text_normalized(_BUILD_SECTION_HEADING)
    assert runbook_needle in section, (
        f"{_RUNBOOK_RELATIVE_PATH}'s {_BUILD_SECTION_HEADING!r} section no longer quotes "
        f"the build output {runbook_needle!r} (claim {claim_id!r})."
    )
    source = _container_sh_text()
    assert container_sh_needle in source, (
        f"{_CONTAINER_SH_RELATIVE_PATH} no longer prints {container_sh_needle!r}, which "
        f"the runbook's step 8 verify line tells the reader to expect (claim {claim_id!r})."
    )


def test_keychain_abort_message_quoted_in_troubleshooting_matches_hostcreds() -> None:
    """The quoted keychain abort is the resolution error `hostcreds` raises.

    The troubleshooting section quotes `cannot resolve <NAME> from the
    keychain source`; every resolver failure message in `hostcreds.py` is
    built from the same `cannot resolve {name} from the {source} source`
    template, with `keychain` as the source, so the pin holds against the
    template the three resolvers share.
    """
    section = _section_text_normalized(_TROUBLESHOOTING_SECTION_HEADING)
    assert _KEYCHAIN_ABORT_TEMPLATE in section, (
        f"{_RUNBOOK_RELATIVE_PATH}'s {_TROUBLESHOOTING_SECTION_HEADING!r} section no longer "
        f"quotes the keychain abort {_KEYCHAIN_ABORT_TEMPLATE!r}."
    )
    source = _hostcreds_source_text()
    assert "cannot resolve {name} from the {source} source" in source, (
        f"{_HOSTCREDS_RELATIVE_PATH} no longer builds its resolver failures from the "
        "'cannot resolve {name} from the {source} source' template, which the runbook's "
        "troubleshooting section quotes as `cannot resolve <NAME> from the keychain source`."
    )


def test_credential_name_and_reserved_name_claims_match_hostcreds() -> None:
    """The name shape and reserved-name list the runbook states are hostcreds' own.

    The runbook teaches manifest authors exactly which names are accepted
    (`[A-Z][A-Z0-9_]*`) and which are refused outright; `hostcreds` enforces
    both. A pattern loosened or a reserved name added on one side alone means
    the runbook green-lights a manifest the validator rejects, or vice versa.
    """
    section = _section_text_normalized(_MANIFEST_SECTION_HEADING)
    pattern_match = re.search(r"must match `([^`]+)`", section)
    assert pattern_match is not None, (
        f"{_RUNBOOK_RELATIVE_PATH}'s {_MANIFEST_SECTION_HEADING!r} section no longer states "
        "the credential name pattern with 'must match'."
    )
    documented_pattern = pattern_match.group(1)
    enforced_pattern = hostcreds._CREDENTIAL_NAME_PATTERN.pattern.strip("^$")
    assert documented_pattern == enforced_pattern, (
        f"the runbook states the credential name pattern as {documented_pattern!r} but "
        f"hostcreds enforces {enforced_pattern!r} (up to its ^...$ anchors)."
    )
    reserved_match = re.search(r"refused outright: (.*?), each of which", section)
    assert reserved_match is not None, (
        f"{_RUNBOOK_RELATIVE_PATH}'s {_MANIFEST_SECTION_HEADING!r} section no longer "
        "enumerates the reserved credential names with 'refused outright:'."
    )
    documented_reserved = set(re.findall(r"`([^`]+)`", reserved_match.group(1)))
    assert documented_reserved == set(hostcreds._RESERVED_CREDENTIAL_NAMES), (
        f"the runbook's reserved-name list {sorted(documented_reserved)!r} differs from "
        f"hostcreds's enforced set {sorted(hostcreds._RESERVED_CREDENTIAL_NAMES)!r}."
    )
