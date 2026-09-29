"""Skill lint: frontmatter, roster, and bare-name reference correspondence.

Section 10.2 of `spec/devcontainer-platform.md` names the "Skill lint" suite:
"Frontmatter valid; plugin and marketplace JSON well formed; every `answers`
field covered by a skill and no skill inventing one." Section 5.1 states the
schema side of the same requirement: `devcontainer_config.answers` is the
single source of truth for the interview, a skill asks the questions
declared there and no others, and a test asserts the correspondence in both
directions. Section 4.2.1 and 4.2.2 describe the per-check and
failure-semantics table shape that AC-4.1 (Section 4.2) requires every check
to have a distinct failure message; rule 7 below makes that requirement
machine-checkable at the document level. Section 11's integrations matrix
records the plugin as an inbound integration whose failure mode is "A
malformed manifest fails skill lint" -- this module is that check.

The canonical skills home is `.agents/skills/<name>/SKILL.md` (every name
`gd-`-prefixed); the plugin's `skills/` entry is a relative symlink to it,
so this module lints the canonical directory and the plugin manifests side
by side. The roster table lives in `docs/skills.md`.

`check_plugin` is the checker. It takes the plugin root, the canonical
skills root, the two documentation paths and the `answers` module, and
returns a list of findings rather than raising, the same "report everything
at once" contract `answers.validate` already uses (Section 4.2.2's "report
every failing field at once"). It runs eight rules, each named below with
the spec section it enforces:

1. `plugin.json` parses and its `name` equals the plugin directory name
   (Section 11).
2. `marketplace.json` parses, has exactly one plugin entry, and that entry's
   `source` resolves to the plugin root (Section 11).
3. `.claude/settings.json` parses and registers the plugin root as a
   `directory` marketplace with the plugin enabled (Section 11).
4. Every `SKILL.md` has a delimited frontmatter block with a `name` that is
   non-empty, matches its directory, and matches the roster pattern
   `^gd-[a-z0-9]+(-[a-z0-9]+)*$`, plus a non-empty `description` no longer
   than 1024 characters (opencode's cap) and no longer than 500 characters
   (house style; both limits produce their own finding) (Section 10.2's
   "Frontmatter valid").
5. The skill roster table in `docs/skills.md` and the directories under the
   canonical skills root are the same set (the roster cannot drift from
   what is installed).
6. A skill declaring `Interview backend: <backend>` has a `## Questions`
   table whose `Field` column, as a set, equals
   `answers.required_fields({"backend": backend, "aws_config_enabled":
   True, "host_proxy": True})` (Section 5.1's bidirectional correspondence),
   and `<backend>` is one of `answers.BACKENDS`.
7. A `## Checks` table has unique `Check` names, unique `Failure message`
   values, and a non-empty `Remedy` cell in every row (Section 4.2.1,
   4.2.2, AC-4.1's distinctness requirement).
8. Every bare `gd-<name>` cross-reference, in any `SKILL.md`, in
   `docs/skills.md`, or in `docs/devcontainer.md`, names a skill present in
   the roster (the reference rule, repointed from the plugin-only
   `/devcontainer:<name>` form to the agent-agnostic bare name; a dangling
   reference is the same defect either way).

Frontmatter is parsed by `_read_frontmatter`, restricted to scalar
`key: value` lines inside the delimited block, and fails loudly (one finding
naming the file and the offending line) on anything else. Section 3.4
requires standard library only unless a dependency is justified in
Section 6, and no dependency is justified for parsing this small a format,
so no YAML package is used.

The negative cases below build synthesized repo trees under `tmp_path`,
using `_default_spec` and `_default_skill` as the single valid baseline and
a `mutate` callable that breaks exactly one rule, and assert the exact
finding text produced. The positive case (`test_real_...`) runs
`check_plugin` over the real repository and asserts an empty list.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from conftest import _generated_dir
from devcontainer_config import answers, repo

# ---------------------------------------------------------------------------
# The checker
# ---------------------------------------------------------------------------

_PLUGIN_MANIFEST_RELATIVE = Path(".claude-plugin") / "plugin.json"
_MARKETPLACE_MANIFEST_RELATIVE = Path(".claude-plugin") / "marketplace.json"
_SETTINGS_RELATIVE = Path(".claude") / "settings.json"

_FRONTMATTER_DELIMITER = "---"
_FRONTMATTER_LINE_PATTERN = re.compile(r"^([A-Za-z0-9_]+):\s*(.*)$")

_SKILL_NAME_PATTERN = re.compile(r"^gd-[a-z0-9]+(-[a-z0-9]+)*$")
# opencode caps skill descriptions at 1024 characters; house style keeps them
# well under a tighter 500-character bound. Both limits are enforced, each
# producing its own finding.
_DESCRIPTION_LIMIT_OPENCODE = 1024
_DESCRIPTION_LIMIT_HOUSE = 500

_ROSTER_HEADER = "| Skill | Invocation |"
_QUESTIONS_HEADER = "| Field | Prompt |"
_CHECKS_HEADER = "| Check | Prevents | Failure message | Remedy |"

_INTERVIEW_BACKEND_PATTERN = re.compile(r"^Interview backend:\s*(\S+)\s*$", re.MULTILINE)
# A bare cross-reference: `gd-` plus at least one name segment, not preceded
# or followed by a character that would make it a different word or a longer
# name. `gd-<name>` placeholders and a bare `gd-` prefix never match.
_REFERENCE_PATTERN = re.compile(r"(?<![\w-])gd-[a-z0-9]+(?:-[a-z0-9]+)*(?![\w-])")


def _parse_markdown_table(
    source_path: Path, text: str, header_line: str
) -> tuple[list[dict[str, str]], list[str]]:
    """`(rows, findings)` for the `| a | b |`-style table starting with `header_line`.

    Each returned row dict is keyed by the header row's column names, values
    stripped of surrounding whitespace. Returns `([], [])` when `header_line`
    is not found in `text` at all, since several callers use that to mean
    "this document has no such table" rather than a bug in the document
    under lint.

    A data row whose cell count disagrees with the header's (a missing
    column, or an unescaped `|` inside a cell splitting it into extra ones)
    produces one finding naming `source_path`, the 1-based line number, the
    offending row text and the column-count mismatch, and is excluded from
    the returned rows -- it is never allowed to raise, matching every other
    sub-check in this module (AC-FUNC-001).
    """
    lines = text.splitlines()
    start: int | None = None
    for index, line in enumerate(lines):
        if line.strip() == header_line.strip():
            start = index
            break
    if start is None:
        return [], []
    header_cells = [cell.strip() for cell in lines[start].strip().strip("|").split("|")]
    rows: list[dict[str, str]] = []
    findings: list[str] = []
    for offset, line in enumerate(lines[start + 2 :]):
        if not line.strip().startswith("|"):
            break
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) != len(header_cells):
            line_number = start + 3 + offset
            findings.append(
                f"{source_path}: line {line_number}: table row {line.strip()!r} has "
                f"{len(cells)} cell(s), header {header_line.strip()!r} declares "
                f"{len(header_cells)} column(s)"
            )
            continue
        rows.append(dict(zip(header_cells, cells, strict=True)))
    return rows, findings


def _read_text(path: Path) -> tuple[str | None, list[str]]:
    """`(text, findings)`: `text` is `None` exactly when `findings` is non-empty.

    Every read of a repository-authored file (a manifest or a `SKILL.md`)
    goes through this one function rather than calling `Path.read_text`
    directly, so a file containing bytes that are not valid UTF-8, or one
    that becomes unreadable between the `is_file()` check and the read
    (permissions, a race with a concurrent delete), produces a finding
    naming the path and, for a decode failure, the byte offset -- instead of
    raising and aborting the whole `check_plugin` run (AC-FUNC-001). This is
    the same defect class `_parse_markdown_table`'s `zip(strict=True)` fix
    addressed for malformed table rows, generalized to every text read in
    this module.
    """
    try:
        return path.read_text(encoding="utf-8"), []
    except UnicodeDecodeError as exc:
        return None, [f"{path}: is not valid UTF-8 at byte offset {exc.start}: {exc.reason}"]
    except OSError as exc:
        return None, [f"{path}: could not be read: {exc}"]


def _load_json(path: Path) -> tuple[dict[str, Any] | None, list[str]]:
    """`(parsed, findings)`: `parsed` is `None` exactly when `findings` is non-empty."""
    if not path.is_file():
        return None, [f"{path}: no such file"]
    text, findings = _read_text(path)
    if text is None:
        return None, findings
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, [f"{path}: does not parse as JSON: {exc}"]
    if not isinstance(parsed, dict):
        return None, [
            f"{path}: top-level JSON value must be an object, got {type(parsed).__name__}"
        ]
    return parsed, []


def _repo_root_above(plugin_root: Path) -> Path | None:
    """The ancestor of `plugin_root`'s `.claude` directory, or `None` if there is none.

    Plugin roots always live at `<repo_root>/.claude/plugins/<name>`
    (Section 11: "Marketplace at `.claude/plugins/devcontainer`"); this walks
    upward looking for the `.claude` path segment instead of hardcoding a
    fixed parent-call depth, so a synthesized tree under `tmp_path` resolves
    the same way the real checkout does.
    """
    for ancestor in plugin_root.parents:
        if ancestor.name == ".claude":
            return ancestor.parent
    return None


def _check_plugin_manifest(plugin_root: Path) -> list[str]:
    """Rule 1: `plugin.json` parses and its `name` equals the plugin directory name."""
    manifest_path = plugin_root / _PLUGIN_MANIFEST_RELATIVE
    manifest, findings = _load_json(manifest_path)
    if manifest is None:
        return findings
    name = manifest.get("name")
    if name != plugin_root.name:
        return [
            f"{manifest_path}: 'name' is {name!r}, does not match the plugin "
            f"directory name {plugin_root.name!r}"
        ]
    return []


def _check_marketplace_manifest(plugin_root: Path) -> list[str]:
    """Rule 2: `marketplace.json` parses, has one entry, whose `source` resolves to
    `plugin_root`.
    """
    manifest_path = plugin_root / _MARKETPLACE_MANIFEST_RELATIVE
    manifest, findings = _load_json(manifest_path)
    if manifest is None:
        return findings

    if "plugins" not in manifest:
        return [f"{manifest_path}: has no 'plugins' key"]

    plugins = manifest["plugins"]
    if not isinstance(plugins, list):
        return [f"{manifest_path}: 'plugins' must be a list, got {type(plugins).__name__}"]
    if len(plugins) != 1:
        return [f"{manifest_path}: 'plugins' must have exactly one entry, has {len(plugins)}"]

    entry = plugins[0]
    source = entry.get("source") if isinstance(entry, dict) else None
    if not isinstance(source, str):
        return [f"{manifest_path}: the plugin entry has no string 'source'"]

    resolved = (plugin_root / source).resolve()
    expected = plugin_root.resolve()
    if resolved != expected:
        return [
            f"{manifest_path}: plugin entry 'source' {source!r} resolves to "
            f"{resolved}, not the plugin directory {expected}"
        ]
    return []


def _check_settings_registration(plugin_root: Path) -> list[str]:
    """Rule 3: `.claude/settings.json` registers `plugin_root` as an enabled
    `directory` marketplace.
    """
    repo_root = _repo_root_above(plugin_root)
    if repo_root is None:
        return [f"{plugin_root}: has no '.claude' ancestor directory to locate settings.json under"]

    settings_path = repo_root / _SETTINGS_RELATIVE
    settings, findings = _load_json(settings_path)
    if settings is None:
        return findings

    plugin_manifest, _ = _load_json(plugin_root / _PLUGIN_MANIFEST_RELATIVE)
    plugin_name = plugin_manifest.get("name") if plugin_manifest is not None else None

    relative_plugin_root = plugin_root.resolve().relative_to(repo_root.resolve())
    marketplaces = settings.get("extraKnownMarketplaces")
    marketplace_key: str | None = None
    if isinstance(marketplaces, dict):
        for key, entry in marketplaces.items():
            source = entry.get("source") if isinstance(entry, dict) else None
            if (
                isinstance(source, dict)
                and source.get("source") == "directory"
                and source.get("path") == str(relative_plugin_root)
            ):
                marketplace_key = key
                break

    if marketplace_key is None:
        return [
            f"{settings_path}: no 'directory' marketplace is registered for {relative_plugin_root}"
        ]

    if plugin_name is None:
        return []

    enabled = settings.get("enabledPlugins")
    expected_key = f"{plugin_name}@{marketplace_key}"
    if not isinstance(enabled, dict) or enabled.get(expected_key) is not True:
        return [f"{settings_path}: plugin {expected_key!r} is not enabled in 'enabledPlugins'"]
    return []


def _read_frontmatter(skill_md_path: Path, text: str) -> tuple[dict[str, str] | None, list[str]]:
    """The scalar `key: value` frontmatter block of `text` (already-read `skill_md_path`
    content), or findings explaining why not.

    Restricted to scalar lines inside a `---`-delimited block at the top of
    the file: an absent, unterminated, or non-scalar block each produce
    their own finding naming the file (and, for a non-scalar line, the line
    number and its text) rather than being silently treated as empty
    frontmatter. `text` is passed in rather than read here so `check_plugin`
    reads each `SKILL.md` exactly once and reports a decode failure exactly
    once, instead of every rule that inspects the file re-reading it and
    each producing its own copy of the same finding.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != _FRONTMATTER_DELIMITER:
        return None, [f"{skill_md_path}: no frontmatter block found (expected '---' delimited)"]

    end: int | None = None
    for index in range(1, len(lines)):
        if lines[index].strip() == _FRONTMATTER_DELIMITER:
            end = index
            break
    if end is None:
        return None, [f"{skill_md_path}: frontmatter block is not terminated with '---'"]

    fields: dict[str, str] = {}
    findings: list[str] = []
    for index in range(1, end):
        line = lines[index]
        if not line.strip():
            continue
        match = _FRONTMATTER_LINE_PATTERN.match(line)
        if match is None:
            findings.append(
                f"{skill_md_path}: frontmatter line {index + 1} is not a scalar "
                f"'key: value' pair: {line!r}"
            )
            continue
        fields[match.group(1)] = match.group(2).strip()
    return fields, findings


def _check_frontmatter(skill_md_path: Path, text: str) -> list[str]:
    """Rule 4: a valid frontmatter block -- a `name` that is non-empty, matches the
    directory and the roster pattern, and a `description` that is non-empty
    and within both length limits.
    """
    fields, findings = _read_frontmatter(skill_md_path, text)
    if fields is None:
        return findings

    name = fields.get("name", "")
    if not name:
        findings.append(f"{skill_md_path}: frontmatter 'name' must not be empty")
    else:
        if name != skill_md_path.parent.name:
            findings.append(
                f"{skill_md_path}: frontmatter 'name' is {name!r}, does not match "
                f"the skill directory name {skill_md_path.parent.name!r}"
            )
        if not _SKILL_NAME_PATTERN.match(name):
            findings.append(
                f"{skill_md_path}: frontmatter 'name' is {name!r}, does not match "
                f"the roster pattern {_SKILL_NAME_PATTERN.pattern!r}"
            )

    description = fields.get("description", "")
    if not description:
        findings.append(f"{skill_md_path}: frontmatter 'description' must not be empty")
    else:
        if len(description) > _DESCRIPTION_LIMIT_OPENCODE:
            findings.append(
                f"{skill_md_path}: frontmatter 'description' is {len(description)} "
                f"characters, over the {_DESCRIPTION_LIMIT_OPENCODE}-character limit"
            )
        if len(description) > _DESCRIPTION_LIMIT_HOUSE:
            findings.append(
                f"{skill_md_path}: frontmatter 'description' is {len(description)} "
                f"characters, over the {_DESCRIPTION_LIMIT_HOUSE}-character house limit"
            )

    return findings


def _roster_names(docs_skills_path: Path, text: str) -> tuple[set[str], list[str]]:
    """`(names, findings)` for `docs_skills_path`'s `| Skill | Invocation |` roster table.

    `text` is `docs_skills_path`'s already-read content: `check_plugin` reads
    the roster document once (guarded by `_read_text`) and passes the result
    here and to `_check_references`, rather than each caller re-reading the
    file and risking its own copy of a decode-failure finding.
    """
    rows, findings = _parse_markdown_table(docs_skills_path, text, _ROSTER_HEADER)
    return {row["Skill"] for row in rows if row.get("Skill")}, findings


def _check_roster(skills_root: Path, docs_skills_path: Path, roster: set[str]) -> list[str]:
    """Rule 5: the roster table's skill names and the canonical skills root's
    directory names are the same set.
    """
    if not docs_skills_path.is_file():
        return [f"{docs_skills_path}: no such file"]

    directories: set[str] = set()
    if skills_root.is_dir():
        directories = {entry.name for entry in skills_root.iterdir() if entry.is_dir()}

    findings: list[str] = []
    for name in sorted(roster - directories):
        findings.append(
            f"{docs_skills_path}: roster names skill '{name}' with no directory at "
            f"{skills_root / name}"
        )
    for name in sorted(directories - roster):
        findings.append(
            f"{skills_root / name}: skill directory has no roster row in {docs_skills_path}"
        )
    return findings


def _check_questions(skill_md_path: Path, text: str, answers_module: Any) -> list[str]:
    """Rule 6: `Interview backend` is valid, and `## Questions`' `Field` column
    matches `required_fields`.
    """
    match = _INTERVIEW_BACKEND_PATTERN.search(text)
    if match is None:
        return []
    backend = match.group(1)

    if backend not in answers_module.BACKENDS:
        accepted = ", ".join(sorted(answers_module.BACKENDS))
        return [
            f"{skill_md_path}: 'Interview backend: {backend}' is not one of "
            f"the accepted backends: {accepted}"
        ]

    rows, findings = _parse_markdown_table(skill_md_path, text, _QUESTIONS_HEADER)
    declared = {row["Field"] for row in rows if row.get("Field")}
    expected = set(
        answers_module.required_fields(
            {"backend": backend, "aws_config_enabled": True, "host_proxy": True}
        )
    )

    for field_name in sorted(expected - declared):
        findings.append(
            f"{skill_md_path}: '## Questions' table is missing required field "
            f"'{field_name}' for backend '{backend}'"
        )
    for field_name in sorted(declared - expected):
        if field_name not in answers_module.FIELDS_BY_NAME:
            findings.append(
                f"{skill_md_path}: '## Questions' table declares field "
                f"'{field_name}', which is not a field 'answers' declares"
            )
        else:
            findings.append(
                f"{skill_md_path}: '## Questions' table declares field "
                f"'{field_name}', which is not required for backend '{backend}'"
            )
    return findings


def _check_checks_table(skill_md_path: Path, text: str) -> list[str]:
    """Rule 7: `## Checks` has unique `Check` names, unique failure messages, and
    no empty `Remedy`.
    """
    rows, findings = _parse_markdown_table(skill_md_path, text, _CHECKS_HEADER)

    seen_names: set[str] = set()
    message_owner: dict[str, str] = {}
    for row in rows:
        name = row.get("Check", "")
        message = row.get("Failure message", "")
        remedy = row.get("Remedy", "")

        if name in seen_names:
            findings.append(f"{skill_md_path}: '## Checks' table has duplicate check name '{name}'")
        else:
            seen_names.add(name)

        if message in message_owner:
            findings.append(
                f"{skill_md_path}: '## Checks' table has duplicate failure message "
                f"'{message}' (rows '{message_owner[message]}' and '{name}')"
            )
        else:
            message_owner[message] = name

        if not remedy:
            findings.append(f"{skill_md_path}: '## Checks' row '{name}' has an empty remedy")

    return findings


def _strip_frontmatter(text: str) -> str:
    """`text` with its `---`-delimited frontmatter block removed, for reference scanning.

    Rule 8 polices cross-references, and a `SKILL.md`'s own `name:` in its
    frontmatter is not one: a skill whose name is not yet in the roster (the
    orphan-directory case) must fail the roster rule alone, not also be
    reported as a dangling reference to itself. A file with no frontmatter
    block is returned unchanged -- the frontmatter rule owns that finding.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != _FRONTMATTER_DELIMITER:
        return text
    for index in range(1, len(lines)):
        if lines[index].strip() == _FRONTMATTER_DELIMITER:
            return "\n".join(lines[index + 1 :])
    return text


def _check_references(source_path: Path, text: str, roster: set[str]) -> list[str]:
    """Rule 8: every bare `gd-<name>` reference in `source_path`'s `text` names a
    skill in `roster`.
    """
    findings: list[str] = []
    for name in sorted(set(_REFERENCE_PATTERN.findall(text))):
        if name not in roster:
            findings.append(f"{source_path}: '{name}' references a skill not in the roster")
    return findings


def check_plugin(
    plugin_root: Path,
    skills_root: Path,
    docs_skills_path: Path,
    docs_devcontainer_path: Path,
    answers_module: Any,
) -> list[str]:
    """Every skill-lint finding for the roster at `skills_root` (spec Section 10.2).

    Never raises on malformed input: every sub-check returns findings rather
    than propagating a parse error, so one call reports every problem found
    across all eight rules instead of stopping at the first (AC-FUNC-001).
    No path or field name used to build this roster's identity (the plugin's
    own name, the skill names, the documentation content) is hardcoded in
    this function's body; every one of those is read from the five paths or
    `answers_module` (AC-FUNC-002).
    """
    findings: list[str] = []
    findings.extend(_check_plugin_manifest(plugin_root))
    findings.extend(_check_marketplace_manifest(plugin_root))
    findings.extend(_check_settings_registration(plugin_root))

    # Each `SKILL.md` is read exactly once here (guarded by `_read_text`) and
    # the resulting text threaded into every rule that inspects it, so a
    # decode failure produces exactly one finding naming the file instead of
    # one per rule that would otherwise re-read it.
    skill_md_paths = sorted(skills_root.glob("*/SKILL.md")) if skills_root.is_dir() else []
    skill_texts: dict[Path, str] = {}
    for skill_md_path in skill_md_paths:
        text, read_findings = _read_text(skill_md_path)
        if text is None:
            findings.extend(read_findings)
            continue
        skill_texts[skill_md_path] = text
        findings.extend(_check_frontmatter(skill_md_path, text))
        findings.extend(_check_questions(skill_md_path, text, answers_module))
        findings.extend(_check_checks_table(skill_md_path, text))

    docs_text: str | None = None
    if docs_skills_path.is_file():
        docs_text, docs_read_findings = _read_text(docs_skills_path)
        findings.extend(docs_read_findings)

    roster: set[str] = set()
    if docs_text is not None:
        roster, roster_findings = _roster_names(docs_skills_path, docs_text)
        findings.extend(roster_findings)
    findings.extend(_check_roster(skills_root, docs_skills_path, roster))

    reference_sources = [(path, _strip_frontmatter(text)) for path, text in skill_texts.items()]
    if docs_text is not None:
        reference_sources.append((docs_skills_path, docs_text))
    if docs_devcontainer_path.is_file():
        devcontainer_text, devcontainer_read_findings = _read_text(docs_devcontainer_path)
        findings.extend(devcontainer_read_findings)
        if devcontainer_text is not None:
            reference_sources.append((docs_devcontainer_path, devcontainer_text))
    for source_path, text in reference_sources:
        findings.extend(_check_references(source_path, text, roster))

    return findings


# ---------------------------------------------------------------------------
# The one parameterized synthesized-tree builder every negative case uses
# ---------------------------------------------------------------------------


def _default_skill(name: str, backend: str | None) -> dict[str, Any]:
    """A skill dict that passes every rule on its own, for `name` and interview `backend`.

    `backend=None` omits the `Interview backend:` line and the `##
    Questions` table entirely, matching a skill (like `gd-env-doctor` or
    `gd-container-lifecycle`) that asks nothing from `answers.FIELDS` (spec
    Section 4.2's "Asks: Nothing").
    """
    # The description embeds no `gd-` name: the reference rule (rule 8)
    # treats every bare `gd-<name>` in the file as a cross-reference, and a
    # default skill whose description echoed its own name would make the
    # name-mutation cases below fail for a reference reason instead of the
    # frontmatter reason each isolates.
    skill: dict[str, Any] = {
        "frontmatter": {"name": name, "description": "A skill that passes every rule."},
        "checks": [
            ("check-one", "prevents one bad thing", "failure message one", "remedy one"),
            ("check-two", "prevents another bad thing", "failure message two", "remedy two"),
        ],
        "extra_body": "",
    }
    if backend is None:
        skill["interview_backend"] = None
        skill["questions"] = []
    else:
        fields = answers.required_fields(
            {"backend": backend, "aws_config_enabled": True, "host_proxy": True}
        )
        skill["interview_backend"] = backend
        skill["questions"] = [(field_name, f"Prompt for {field_name}") for field_name in fields]
    return skill


def _default_spec(plugin_dir_name: str) -> dict[str, Any]:
    """The full valid file-content spec for one synthesized `<tmp_path>/checkout` tree.

    Every negative case in this file starts from this same spec, mutates
    exactly one thing, writes it with `_write_tree`, and asserts the one
    finding that mutation produces -- rather than each rule growing its own
    fixture-tree builder (Approach step 12).
    """
    return {
        "plugin_json": {
            "name": plugin_dir_name,
            "description": "A synthesized plugin.",
            "version": "0.1.0",
        },
        "marketplace_json": {
            "name": plugin_dir_name,
            "owner": {"name": "Example"},
            "plugins": [
                {"name": plugin_dir_name, "source": "./", "description": "A synthesized plugin."}
            ],
        },
        "settings_json": {
            "extraKnownMarketplaces": {
                plugin_dir_name: {
                    "source": {
                        "source": "directory",
                        "path": f".claude/plugins/{plugin_dir_name}",
                    }
                }
            },
            "enabledPlugins": {f"{plugin_dir_name}@{plugin_dir_name}": True},
        },
        "skills": {},
        "roster": [],
        "docs_extra": "",
        "docs_raw": None,
        "devcontainer_docs_extra": "",
    }


def _write_json_or_raw(path: Path, value: Any) -> None:
    """Write `value` to `path`: verbatim if it is `str` or `bytes`, else JSON-serialized.

    `value` may be any JSON-serializable shape, not only a `dict` -- a
    top-level list is how `test_manifest_top_level_json_array_is_a_finding`
    reproduces `_load_json`'s "top-level JSON value must be an object"
    finding, a shape `_render_*` (which always builds an object) could never
    produce. A `bytes` value is written verbatim via `write_bytes`, mirroring
    the per-skill `raw_bytes` hatch `_write_tree` already provides for
    `SKILL.md`, which is how `test_plugin_manifest_with_non_utf8_bytes_is_a_finding`
    reproduces `_load_json`'s `text is None` arm (a manifest whose bytes are
    not valid UTF-8) for a manifest file, a shape neither a `str` nor a
    JSON-serializable value could produce.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(value, bytes):
        path.write_bytes(value)
    elif isinstance(value, str):
        path.write_text(value, encoding="utf-8")
    else:
        path.write_text(json.dumps(value, indent=2), encoding="utf-8")


def _render_skill_md(skill: dict[str, Any]) -> str:
    if "raw" in skill:
        return str(skill["raw"])

    lines = [_FRONTMATTER_DELIMITER]
    for key, value in skill["frontmatter"].items():
        lines.append(f"{key}: {value}")
    lines.append(_FRONTMATTER_DELIMITER)
    lines.append("")

    if skill.get("interview_backend"):
        lines.append(f"Interview backend: {skill['interview_backend']}")
        lines.append("")
        lines.append("## Questions")
        lines.append("")
        lines.append(_QUESTIONS_HEADER)
        lines.append("|---|---|")
        for field_name, prompt in skill["questions"]:
            lines.append(f"| {field_name} | {prompt} |")
        lines.append("")

    lines.append("## Checks")
    lines.append("")
    lines.append(_CHECKS_HEADER)
    lines.append("|---|---|---|---|")
    for check_name, prevents, message, remedy in skill["checks"]:
        lines.append(f"| {check_name} | {prevents} | {message} | {remedy} |")
    lines.append("")

    extra_body = skill.get("extra_body", "")
    if extra_body:
        lines.append(str(extra_body))

    return "\n".join(lines)


def _render_docs(roster: list[str], extra: str) -> str:
    lines = [
        "# Synthesized skills",
        "",
        "## Roster",
        "",
        _ROSTER_HEADER,
        "|---|---|",
    ]
    for name in roster:
        lines.append(f"| {name} | {name} |")
    lines.append("")
    if extra:
        lines.append(extra)
    return "\n".join(lines)


def _write_tree(
    tmp_path: Path, plugin_dir_name: str, spec: dict[str, Any]
) -> tuple[Path, Path, Path, Path]:
    """Serialize `spec` (built from `_default_spec`, then mutated) under a fresh `tmp_path` root.

    Returns `(plugin_root, skills_root, docs_skills_path,
    docs_devcontainer_path)`.

    `spec["docs_raw"]`, when not `None`, is written verbatim as
    `docs/skills.md` instead of being built from `spec["roster"]` and
    `spec["docs_extra"]` via `_render_docs`, mirroring the per-skill `"raw"`
    escape hatch `_render_skill_md` already provides. This is what lets a
    negative case write a roster table row whose cell count cannot be
    produced by `_render_docs`'s one-name-per-row loop.

    `spec["plugin_json"]`, `spec["marketplace_json"]` and
    `spec["settings_json"]` are each skipped (the file is left absent)
    when the corresponding value is `None`, which is how the
    "no such file" cases reproduce `_load_json`'s missing-manifest finding
    without hand-rolling a second tree builder.
    """
    root = _generated_dir(tmp_path, "checkout")
    plugin_root = root / ".claude" / "plugins" / plugin_dir_name
    skills_root = root / ".agents" / "skills"

    if spec["plugin_json"] is not None:
        _write_json_or_raw(plugin_root / _PLUGIN_MANIFEST_RELATIVE, spec["plugin_json"])
    if spec["marketplace_json"] is not None:
        _write_json_or_raw(plugin_root / _MARKETPLACE_MANIFEST_RELATIVE, spec["marketplace_json"])
    if spec["settings_json"] is not None:
        _write_json_or_raw(root / _SETTINGS_RELATIVE, spec["settings_json"])

    for skill_name, skill in spec["skills"].items():
        skill_dir = skills_root / skill_name
        skill_dir.mkdir(parents=True, exist_ok=True)
        skill_md_path = skill_dir / "SKILL.md"
        if "raw_bytes" in skill:
            skill_md_path.write_bytes(bytes(skill["raw_bytes"]))
        else:
            skill_md_path.write_text(_render_skill_md(skill), encoding="utf-8")

    docs_skills_path = root / "docs" / "skills.md"
    docs_skills_path.parent.mkdir(parents=True, exist_ok=True)
    docs_text = (
        spec["docs_raw"]
        if spec.get("docs_raw") is not None
        else _render_docs(spec["roster"], spec["docs_extra"])
    )
    docs_skills_path.write_text(docs_text, encoding="utf-8")

    docs_devcontainer_path = root / "docs" / "devcontainer.md"
    devcontainer_extra = spec["devcontainer_docs_extra"]
    docs_devcontainer_path.write_text(
        "# Synthesized devcontainer internals\n"
        + (f"\n{devcontainer_extra}" if devcontainer_extra else ""),
        encoding="utf-8",
    )

    return plugin_root, skills_root, docs_skills_path, docs_devcontainer_path


def _lint(
    tmp_path: Path,
    mutate: Callable[[dict[str, Any]], None],
    plugin_dir_name: str = "sample",
) -> list[str]:
    """Build the default spec, apply `mutate` in place, write it, and return the findings."""
    spec = _default_spec(plugin_dir_name)
    mutate(spec)
    plugin_root, skills_root, docs_skills_path, docs_devcontainer_path = _write_tree(
        tmp_path, plugin_dir_name, spec
    )
    return check_plugin(plugin_root, skills_root, docs_skills_path, docs_devcontainer_path, answers)


# ---------------------------------------------------------------------------
# Rule 1: plugin.json
# ---------------------------------------------------------------------------


def test_plugin_manifest_that_does_not_parse_is_a_finding(tmp_path: Path) -> None:
    def mutate(spec: dict[str, Any]) -> None:
        spec["plugin_json"] = "{ not valid json"

    findings = _lint(tmp_path, mutate)
    assert len(findings) == 1
    assert "plugin.json" in findings[0]
    assert "does not parse as JSON" in findings[0]


def test_plugin_manifest_name_disagreeing_with_directory_is_a_finding(tmp_path: Path) -> None:
    def mutate(spec: dict[str, Any]) -> None:
        # Keep rule 3 (settings registration) satisfied under the renamed
        # plugin.json 'name' so this mutation isolates rule 1 alone.
        spec["plugin_json"]["name"] = "not-the-directory-name"
        spec["settings_json"]["enabledPlugins"] = {"not-the-directory-name@sample": True}

    findings = _lint(tmp_path, mutate)
    assert len(findings) == 1
    assert "plugin.json" in findings[0]
    assert "'name' is 'not-the-directory-name'" in findings[0]
    assert "'sample'" in findings[0]


# ---------------------------------------------------------------------------
# Rule 2: marketplace.json
# ---------------------------------------------------------------------------


def test_marketplace_manifest_that_does_not_parse_is_a_finding(tmp_path: Path) -> None:
    def mutate(spec: dict[str, Any]) -> None:
        spec["marketplace_json"] = "not json at all"

    findings = _lint(tmp_path, mutate)
    assert len(findings) == 1
    assert "marketplace.json" in findings[0]
    assert "does not parse as JSON" in findings[0]


def test_marketplace_manifest_with_two_entries_is_a_finding(tmp_path: Path) -> None:
    def mutate(spec: dict[str, Any]) -> None:
        spec["marketplace_json"]["plugins"].append(dict(spec["marketplace_json"]["plugins"][0]))

    findings = _lint(tmp_path, mutate)
    assert len(findings) == 1
    assert "marketplace.json" in findings[0]
    assert "'plugins' must have exactly one entry, has 2" in findings[0]


def test_marketplace_source_not_resolving_to_plugin_root_is_a_finding(tmp_path: Path) -> None:
    def mutate(spec: dict[str, Any]) -> None:
        spec["marketplace_json"]["plugins"][0]["source"] = "../elsewhere"

    findings = _lint(tmp_path, mutate)
    assert len(findings) == 1
    assert "marketplace.json" in findings[0]
    assert "'source' '../elsewhere' resolves to" in findings[0]
    assert "not the plugin directory" in findings[0]


def test_marketplace_entry_without_string_source_is_a_finding(tmp_path: Path) -> None:
    def mutate(spec: dict[str, Any]) -> None:
        spec["marketplace_json"]["plugins"][0]["source"] = {"not": "a string"}

    findings = _lint(tmp_path, mutate)
    assert len(findings) == 1
    assert "marketplace.json" in findings[0]
    assert "the plugin entry has no string 'source'" in findings[0]


@pytest.mark.parametrize(
    ("marketplace_json", "expected_substring"),
    [
        pytest.param(
            {"name": "sample", "owner": {"name": "Example"}},
            "has no 'plugins' key",
            id="plugins-key-absent",
        ),
        pytest.param(
            {"name": "sample", "owner": {"name": "Example"}, "plugins": "not-a-list"},
            "'plugins' must be a list, got str",
            id="plugins-not-a-list",
        ),
        pytest.param(
            {"name": "sample", "owner": {"name": "Example"}, "plugins": {"a": 1}},
            "'plugins' must be a list, got dict",
            id="plugins-is-a-dict",
        ),
    ],
)
def test_marketplace_manifest_wrong_shaped_plugins_is_a_distinct_finding(
    tmp_path: Path, marketplace_json: dict[str, Any], expected_substring: str
) -> None:
    """The three ways 'plugins' can be the wrong shape each name that shape, rather
    than all three collapsing into the same fabricated 'has 0' count.
    """

    def mutate(spec: dict[str, Any]) -> None:
        spec["marketplace_json"] = marketplace_json

    findings = _lint(tmp_path, mutate)
    assert len(findings) == 1
    assert "marketplace.json" in findings[0]
    assert expected_substring in findings[0]


# ---------------------------------------------------------------------------
# Rule 3: .claude/settings.json
# ---------------------------------------------------------------------------


def test_settings_that_does_not_parse_is_a_finding(tmp_path: Path) -> None:
    def mutate(spec: dict[str, Any]) -> None:
        spec["settings_json"] = "{ broken"

    findings = _lint(tmp_path, mutate)
    assert len(findings) == 1
    assert "settings.json" in findings[0]
    assert "does not parse as JSON" in findings[0]


def test_settings_with_no_directory_marketplace_entry_is_a_finding(tmp_path: Path) -> None:
    def mutate(spec: dict[str, Any]) -> None:
        spec["settings_json"]["extraKnownMarketplaces"] = {}

    findings = _lint(tmp_path, mutate)
    assert len(findings) == 1
    assert "settings.json" in findings[0]
    assert "no 'directory' marketplace is registered for" in findings[0]
    assert ".claude/plugins/sample" in findings[0]


def test_settings_with_plugin_not_enabled_is_a_finding(tmp_path: Path) -> None:
    def mutate(spec: dict[str, Any]) -> None:
        spec["settings_json"]["enabledPlugins"] = {}

    findings = _lint(tmp_path, mutate)
    assert len(findings) == 1
    assert "settings.json" in findings[0]
    assert "'sample@sample' is not enabled" in findings[0]


def test_settings_registration_check_with_no_claude_ancestor_is_a_finding(tmp_path: Path) -> None:
    """A plugin root with no `.claude` ancestor cannot have `settings.json` located
    above it, so `_repo_root_above` returning `None` is its own finding rather than
    `_check_settings_registration` guessing a path or raising on `None / path`.

    Every other case in this module builds its tree with `_write_tree`, which
    always nests `plugin_root` under `<checkout>/.claude/plugins/<name>` --
    this is the one case that must not, so it writes the manifests directly
    with `_write_json_or_raw` instead.
    """
    root = _generated_dir(tmp_path, "standalone")
    plugin_root = root / "sample"
    _write_json_or_raw(
        plugin_root / _PLUGIN_MANIFEST_RELATIVE,
        {"name": "sample", "description": "A synthesized plugin.", "version": "0.1.0"},
    )
    _write_json_or_raw(
        plugin_root / _MARKETPLACE_MANIFEST_RELATIVE,
        {
            "name": "sample",
            "owner": {"name": "Example"},
            "plugins": [{"name": "sample", "source": "./", "description": "A synthesized plugin."}],
        },
    )
    docs_skills_path = root / "docs" / "skills.md"
    docs_skills_path.parent.mkdir(parents=True, exist_ok=True)
    docs_skills_path.write_text(_render_docs([], ""), encoding="utf-8")
    docs_devcontainer_path = root / "docs" / "devcontainer.md"
    docs_devcontainer_path.write_text("# Synthesized devcontainer internals\n", encoding="utf-8")

    findings = check_plugin(
        plugin_root, root / ".agents" / "skills", docs_skills_path, docs_devcontainer_path, answers
    )

    assert len(findings) == 1
    assert str(plugin_root) in findings[0]
    assert "has no '.claude' ancestor directory to locate settings.json under" in findings[0]


# ---------------------------------------------------------------------------
# _load_json: an absent manifest, and a top-level JSON value that is not an
# object, are the most likely real occurrences of "a malformed manifest
# fails skill lint" (spec Section 11), so each gets its own case rather than
# only the unparseable-content path each rule's own tests already cover.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("manifest_key", "manifest_name"),
    [
        pytest.param("plugin_json", "plugin.json", id="plugin-json-absent"),
        pytest.param("marketplace_json", "marketplace.json", id="marketplace-json-absent"),
        pytest.param("settings_json", "settings.json", id="settings-json-absent"),
    ],
)
def test_absent_manifest_is_a_finding(
    tmp_path: Path, manifest_key: str, manifest_name: str
) -> None:
    def mutate(spec: dict[str, Any]) -> None:
        spec[manifest_key] = None

    findings = _lint(tmp_path, mutate)
    assert len(findings) == 1
    assert manifest_name in findings[0]
    assert "no such file" in findings[0]


def test_manifest_top_level_json_array_is_a_finding(tmp_path: Path) -> None:
    def mutate(spec: dict[str, Any]) -> None:
        spec["plugin_json"] = ["not", "an", "object"]

    findings = _lint(tmp_path, mutate)
    assert len(findings) == 1
    assert "plugin.json" in findings[0]
    assert "top-level JSON value must be an object, got list" in findings[0]


def test_plugin_manifest_with_non_utf8_bytes_is_a_finding(tmp_path: Path) -> None:
    """A `plugin.json` that is not valid UTF-8 produces one finding naming the file and
    the decode position, rather than `check_plugin` raising `UnicodeDecodeError`
    (AC-FUNC-001). `test_skill_md_with_non_utf8_bytes_is_a_finding` proves the same
    `_read_text` guard for a `SKILL.md`; this proves it for `_load_json`'s `text is
    None` arm, which no manifest case previously exercised.
    """

    def mutate(spec: dict[str, Any]) -> None:
        spec["plugin_json"] = b'{"name": "sample", "bad byte \xff here": true}'

    findings = _lint(tmp_path, mutate)
    assert len(findings) == 1
    assert "plugin.json" in findings[0]
    assert "is not valid UTF-8" in findings[0]


def test_plugin_manifest_unreadable_is_a_finding(tmp_path: Path) -> None:
    """A `plugin.json` that exists but cannot be read (a permissions failure, distinct
    from a decode failure) produces one finding naming the path, exercising
    `_read_text`'s `except OSError` arm rather than its `UnicodeDecodeError` arm.
    """
    plugin_root, skills_root, docs_skills_path, docs_devcontainer_path = _write_tree(
        tmp_path, "sample", _default_spec("sample")
    )
    manifest_path = plugin_root / _PLUGIN_MANIFEST_RELATIVE
    manifest_path.chmod(0o000)

    try:
        findings = check_plugin(
            plugin_root, skills_root, docs_skills_path, docs_devcontainer_path, answers
        )
    finally:
        manifest_path.chmod(0o644)

    assert len(findings) == 1
    assert str(manifest_path) in findings[0]
    assert "could not be read" in findings[0]


# ---------------------------------------------------------------------------
# Rule 4: SKILL.md frontmatter
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("frontmatter_key", "frontmatter_value", "skill_directory", "expected_substrings"),
    [
        pytest.param(
            "description",
            "",
            "gd-sample",
            ("'description' must not be empty",),
            id="empty-description",
        ),
        pytest.param(
            "name",
            "",
            "gd-sample",
            ("frontmatter 'name' must not be empty",),
            id="empty-name",
        ),
        pytest.param(
            "name",
            "gd-wrong-name",
            "gd-sample",
            ("'name' is 'gd-wrong-name'", "'gd-sample'"),
            id="name-not-matching-directory",
        ),
        pytest.param(
            "name",
            "sample",
            "sample",
            ("does not match the roster pattern",),
            id="name-without-gd-prefix",
        ),
    ],
)
def test_skill_md_frontmatter_field_is_a_finding(
    tmp_path: Path,
    frontmatter_key: str,
    frontmatter_value: str,
    skill_directory: str,
    expected_substrings: tuple[str, ...],
) -> None:
    """One default-otherwise-valid `SKILL.md` frontmatter field mutated in turn produces
    exactly one finding naming the file and the broken field (AC-TEST-004).
    """

    def mutate(spec: dict[str, Any]) -> None:
        skill = _default_skill("gd-sample", backend="local")
        skill["frontmatter"][frontmatter_key] = frontmatter_value
        spec["skills"][skill_directory] = skill
        spec["roster"] = [skill_directory]

    findings = _lint(tmp_path, mutate)
    assert len(findings) == 1
    assert "SKILL.md" in findings[0]
    for substring in expected_substrings:
        assert substring in findings[0]


@pytest.mark.parametrize(
    ("description_length", "expected_findings", "expected_substrings"),
    [
        pytest.param(
            _DESCRIPTION_LIMIT_HOUSE + 1,
            1,
            (
                f"over the {_DESCRIPTION_LIMIT_HOUSE}-character house limit",
                f"'description' is {_DESCRIPTION_LIMIT_HOUSE + 1} characters",
            ),
            id="over-house-limit-only",
        ),
        pytest.param(
            _DESCRIPTION_LIMIT_OPENCODE + 1,
            2,
            (
                f"over the {_DESCRIPTION_LIMIT_OPENCODE}-character limit",
                f"over the {_DESCRIPTION_LIMIT_HOUSE}-character house limit",
            ),
            id="over-opencode-limit",
        ),
    ],
)
def test_skill_md_description_over_a_limit_is_a_distinct_finding(
    tmp_path: Path,
    description_length: int,
    expected_findings: int,
    expected_substrings: tuple[str, ...],
) -> None:
    """Each description limit produces its own finding: a description over the house
    limit but under opencode's cap trips one rule, and one over opencode's cap
    trips both, never collapsing into a single fabricated count.
    """

    def mutate(spec: dict[str, Any]) -> None:
        skill = _default_skill("gd-sample", backend=None)
        skill["frontmatter"]["description"] = "d" * description_length
        spec["skills"]["gd-sample"] = skill
        spec["roster"] = ["gd-sample"]

    findings = _lint(tmp_path, mutate)
    assert len(findings) == expected_findings
    assert "SKILL.md" in findings[0]
    for substring in expected_substrings:
        assert any(substring in finding for finding in findings)
    if expected_findings == 2:
        assert findings[0] != findings[1]


def test_skill_md_description_at_each_limit_passes(tmp_path: Path) -> None:
    """A description of exactly the house limit produces no length finding: the
    limits are inclusive maxima, not exclusive thresholds.
    """

    def mutate(spec: dict[str, Any]) -> None:
        skill = _default_skill("gd-sample", backend=None)
        skill["frontmatter"]["description"] = "d" * _DESCRIPTION_LIMIT_HOUSE
        spec["skills"]["gd-sample"] = skill
        spec["roster"] = ["gd-sample"]

    findings = _lint(tmp_path, mutate)
    assert findings == []


@pytest.mark.parametrize(
    ("raw", "expected_substrings"),
    [
        pytest.param(
            "# No frontmatter here\n\nJust prose.\n",
            ("no frontmatter block found",),
            id="no-delimiter",
        ),
        pytest.param(
            "---\nname: gd-sample\ndescription: d\n",
            ("not terminated with '---'",),
            id="unterminated",
        ),
        pytest.param(
            "---\n"
            "name: gd-sample\n"
            "description: The gd-sample skill.\n"
            "not-a-scalar-line\n"
            "---\n\n"
            "## Checks\n\n" + _CHECKS_HEADER + "\n|---|---|---|---|\n| c | p | m | r |\n",
            ("is not a scalar 'key: value' pair", "not-a-scalar-line"),
            id="non-scalar-line",
        ),
    ],
)
def test_skill_md_raw_frontmatter_defect_is_a_finding(
    tmp_path: Path, raw: str, expected_substrings: tuple[str, ...]
) -> None:
    """A hand-authored `SKILL.md` whose frontmatter block is malformed at the raw-text
    level (absent, unterminated, or containing a non-scalar line) produces exactly one
    finding naming the file (AC-TEST-004).
    """

    def mutate(spec: dict[str, Any]) -> None:
        spec["skills"]["gd-sample"] = {"raw": raw}
        spec["roster"] = ["gd-sample"]

    findings = _lint(tmp_path, mutate)
    assert len(findings) == 1
    assert "SKILL.md" in findings[0]
    for substring in expected_substrings:
        assert substring in findings[0]


def test_skill_md_with_non_utf8_bytes_is_a_finding(tmp_path: Path) -> None:
    """A `SKILL.md` that is not valid UTF-8 produces one finding naming the file and
    the decode position, rather than `check_plugin` raising `UnicodeDecodeError`
    (AC-FUNC-001).
    """

    def mutate(spec: dict[str, Any]) -> None:
        spec["skills"]["gd-sample"] = {
            "raw_bytes": b"---\nname: gd-sample\ndescription: bad byte \xff here\n---\n"
        }
        spec["roster"] = ["gd-sample"]

    findings = _lint(tmp_path, mutate)
    assert len(findings) == 1
    assert "SKILL.md" in findings[0]
    assert "is not valid UTF-8" in findings[0]


# ---------------------------------------------------------------------------
# Rule 5: roster vs. canonical skills/ directories
# ---------------------------------------------------------------------------


def test_roster_row_with_no_directory_is_a_finding(tmp_path: Path) -> None:
    def mutate(spec: dict[str, Any]) -> None:
        spec["roster"] = ["gd-ghost-skill"]

    findings = _lint(tmp_path, mutate)
    assert len(findings) == 1
    assert "roster names skill 'gd-ghost-skill' with no directory" in findings[0]


def test_directory_with_no_roster_row_is_a_finding(tmp_path: Path) -> None:
    def mutate(spec: dict[str, Any]) -> None:
        spec["skills"]["gd-orphan-skill"] = _default_skill("gd-orphan-skill", backend=None)
        spec["roster"] = []

    findings = _lint(tmp_path, mutate)
    assert len(findings) == 1
    assert "gd-orphan-skill" in findings[0]
    assert "has no roster row" in findings[0]


def test_missing_docs_path_is_a_finding(tmp_path: Path) -> None:
    """A wrong `docs_skills_path` produces exactly `_check_roster`'s 'no such file'
    finding.

    `_roster_names` itself is silent on a missing path (`set(), []`) because
    `_check_roster` already owns that finding; this pins that the two
    branches agree rather than the reference check (rule 8) silently seeing
    an empty roster and reporting nothing.
    """
    plugin_root, skills_root, docs_skills_path, docs_devcontainer_path = _write_tree(
        tmp_path, "sample", _default_spec("sample")
    )
    missing_docs_path = docs_skills_path.parent / "does-not-exist.md"

    findings = check_plugin(
        plugin_root, skills_root, missing_docs_path, docs_devcontainer_path, answers
    )
    assert len(findings) == 1
    assert str(missing_docs_path) in findings[0]
    assert "no such file" in findings[0]


# ---------------------------------------------------------------------------
# Rule 6: Interview backend / ## Questions correspondence
# ---------------------------------------------------------------------------


def _questions_table_missing_required_field_mutate(spec: dict[str, Any]) -> None:
    skill = _default_skill("gd-sample", backend="local")
    skill["questions"] = [
        (name, prompt) for name, prompt in skill["questions"] if name != "developer_name"
    ]
    spec["skills"]["gd-sample"] = skill
    spec["roster"] = ["gd-sample"]


def _questions_table_invented_field_mutate(spec: dict[str, Any]) -> None:
    skill = _default_skill("gd-sample", backend="local")
    skill["questions"].append(("not_a_real_field", "Prompt for not_a_real_field"))
    spec["skills"]["gd-sample"] = skill
    spec["roster"] = ["gd-sample"]


def test_questions_table_missing_a_required_field_is_a_finding(tmp_path: Path) -> None:
    findings = _lint(tmp_path, _questions_table_missing_required_field_mutate)
    assert len(findings) == 1
    assert "missing required field 'developer_name'" in findings[0]
    assert "backend 'local'" in findings[0]


def test_questions_table_with_invented_field_is_a_finding(tmp_path: Path) -> None:
    findings = _lint(tmp_path, _questions_table_invented_field_mutate)
    assert len(findings) == 1
    assert "declares field 'not_a_real_field'" in findings[0]
    assert "not a field 'answers' declares" in findings[0]


def test_questions_table_with_field_not_required_for_backend_is_a_finding(tmp_path: Path) -> None:
    """A field `answers` declares, but that is not required for the declared backend,
    is a third distinct finding from both the missing-required-field and the
    invented-field cases (a `local`-backend skill has no business asking a
    `remote_*` field).
    """

    def mutate(spec: dict[str, Any]) -> None:
        skill = _default_skill("gd-sample", backend="local")
        skill["questions"].append(("remote_instance_id", "Prompt for remote_instance_id"))
        spec["skills"]["gd-sample"] = skill
        spec["roster"] = ["gd-sample"]

    findings = _lint(tmp_path, mutate)
    assert len(findings) == 1
    assert "declares field 'remote_instance_id'" in findings[0]
    assert "not required for backend 'local'" in findings[0]
    assert "not a field 'answers' declares" not in findings[0]


def test_missing_and_invented_field_findings_are_distinct(tmp_path: Path) -> None:
    missing_findings = _lint(tmp_path, _questions_table_missing_required_field_mutate)
    invented_findings = _lint(tmp_path, _questions_table_invented_field_mutate)
    assert len(missing_findings) == 1
    assert len(invented_findings) == 1
    assert missing_findings != invented_findings
    assert missing_findings[0] != invented_findings[0]


def test_interview_backend_outside_backends_is_a_finding(tmp_path: Path) -> None:
    def mutate(spec: dict[str, Any]) -> None:
        skill = _default_skill("gd-sample", backend="local")
        skill["interview_backend"] = "quantum"
        spec["skills"]["gd-sample"] = skill
        spec["roster"] = ["gd-sample"]

    findings = _lint(tmp_path, mutate)
    assert len(findings) == 1
    assert "'Interview backend: quantum' is not one of" in findings[0]
    assert "local" in findings[0]
    assert "remote" in findings[0]


# ---------------------------------------------------------------------------
# Rule 7: ## Checks table
# ---------------------------------------------------------------------------


def test_checks_table_duplicate_check_name_is_a_finding(tmp_path: Path) -> None:
    def mutate(spec: dict[str, Any]) -> None:
        skill = _default_skill("gd-doctor", backend=None)
        skill["checks"] = [
            ("dup-check", "prevents one", "message one", "remedy one"),
            ("dup-check", "prevents two", "message two", "remedy two"),
        ]
        spec["skills"]["gd-doctor"] = skill
        spec["roster"] = ["gd-doctor"]

    findings = _lint(tmp_path, mutate)
    assert any("duplicate check name 'dup-check'" in finding for finding in findings)
    assert not any("duplicate failure message" in finding for finding in findings)
    assert not any("empty remedy" in finding for finding in findings)


def test_checks_table_duplicate_failure_message_is_a_finding(tmp_path: Path) -> None:
    def mutate(spec: dict[str, Any]) -> None:
        skill = _default_skill("gd-doctor", backend=None)
        skill["checks"] = [
            ("check-a", "prevents one", "shared message", "remedy one"),
            ("check-b", "prevents two", "shared message", "remedy two"),
        ]
        spec["skills"]["gd-doctor"] = skill
        spec["roster"] = ["gd-doctor"]

    findings = _lint(tmp_path, mutate)
    assert any("duplicate failure message 'shared message'" in finding for finding in findings)
    assert not any("duplicate check name" in finding for finding in findings)
    assert not any("empty remedy" in finding for finding in findings)


def test_checks_table_empty_remedy_is_a_finding(tmp_path: Path) -> None:
    def mutate(spec: dict[str, Any]) -> None:
        skill = _default_skill("gd-doctor", backend=None)
        skill["checks"] = [
            ("check-a", "prevents one", "message one", ""),
            ("check-b", "prevents two", "message two", "remedy two"),
        ]
        spec["skills"]["gd-doctor"] = skill
        spec["roster"] = ["gd-doctor"]

    findings = _lint(tmp_path, mutate)
    assert len(findings) == 1
    assert "'## Checks' row 'check-a' has an empty remedy" in findings[0]


# ---------------------------------------------------------------------------
# Rule 8: bare gd-<name> references
# ---------------------------------------------------------------------------


def test_skill_md_reference_to_unknown_skill_is_a_finding(tmp_path: Path) -> None:
    def mutate(spec: dict[str, Any]) -> None:
        skill = _default_skill("gd-doctor", backend=None)
        skill["extra_body"] = "Run `gd-no-such-skill` first."
        spec["skills"]["gd-doctor"] = skill
        spec["roster"] = ["gd-doctor"]

    findings = _lint(tmp_path, mutate)
    assert len(findings) == 1
    assert "SKILL.md" in findings[0]
    assert "'gd-no-such-skill' references a skill not in the roster" in findings[0]


def test_docs_roster_reference_to_unknown_skill_is_a_finding(tmp_path: Path) -> None:
    def mutate(spec: dict[str, Any]) -> None:
        spec["docs_extra"] = "See also `gd-no-such-skill` for details."

    findings = _lint(tmp_path, mutate)
    assert len(findings) == 1
    assert "skills.md" in findings[0]
    assert "'gd-no-such-skill' references a skill not in the roster" in findings[0]


def test_devcontainer_docs_reference_to_unknown_skill_is_a_finding(tmp_path: Path) -> None:
    """The plugin-facing document is a rule-8 source alongside the roster: a
    dangling bare name there strands the same reader, so it gets the same
    finding rather than an exemption.
    """

    def mutate(spec: dict[str, Any]) -> None:
        spec["devcontainer_docs_extra"] = "See also `gd-no-such-skill` for details."

    findings = _lint(tmp_path, mutate)
    assert len(findings) == 1
    assert "devcontainer.md" in findings[0]
    assert "'gd-no-such-skill' references a skill not in the roster" in findings[0]


@pytest.mark.parametrize(
    "prose",
    [
        pytest.param("The gd- prefix marks every roster skill.", id="bare-prefix"),
        pytest.param("Invoke `gd-<name>` for the chosen backend.", id="placeholder"),
        pytest.param(
            "A skill id is gd- followed by lowercase segments.", id="bare-prefix-no-segment"
        ),
    ],
)
def test_reference_pattern_ignores_non_name_forms(tmp_path: Path, prose: str) -> None:
    """A bare `gd-` prefix, a `gd-<name>` placeholder, and a `gd-`-containing word
    that is not a name segment pattern each produce no finding: the rule
    polices names, not every appearance of the letters.
    """

    def mutate(spec: dict[str, Any]) -> None:
        skill = _default_skill("gd-doctor", backend=None)
        skill["extra_body"] = prose
        spec["skills"]["gd-doctor"] = skill
        spec["roster"] = ["gd-doctor"]

    findings = _lint(tmp_path, mutate)
    assert findings == []


# ---------------------------------------------------------------------------
# Cross-cutting: never raises, and the real repository is clean
# ---------------------------------------------------------------------------


def _garbage_top_level_json_mutate(spec: dict[str, Any]) -> None:
    spec.update(
        {
            "plugin_json": "{{{ garbage",
            "marketplace_json": "{{{ garbage",
            "settings_json": "{{{ garbage",
        }
    )


def _wrong_shaped_json_mutate(spec: dict[str, Any]) -> None:
    spec.update(
        {
            "plugin_json": {},
            "marketplace_json": {"plugins": "not-a-list"},
            "settings_json": {},
        }
    )


def _mis_columned_roster_row_mutate(spec: dict[str, Any]) -> None:
    """A roster table row with one cell under a two-column header.

    `_render_docs` always emits one `| name | name |` row per roster entry,
    so this uses `docs_raw` to write a row `_render_docs` could never
    produce -- the input class a hand-authored roster row is most likely to
    get wrong (Approach's rationale for this case).
    """
    spec["docs_raw"] = "\n".join(
        [
            "# Synthesized skills",
            "",
            "## Roster",
            "",
            _ROSTER_HEADER,
            "|---|---|",
            "| gd-doctor |",
            "",
        ]
    )


def _unescaped_pipe_checks_row_mutate(spec: dict[str, Any]) -> None:
    """A `## Checks` row with an unescaped `|`, producing two extra cells."""
    raw = "\n".join(
        [
            _FRONTMATTER_DELIMITER,
            "name: gd-doctor",
            "description: The gd-doctor skill.",
            _FRONTMATTER_DELIMITER,
            "",
            "## Checks",
            "",
            _CHECKS_HEADER,
            "|---|---|---|---|",
            "| check-a | prevents a | message | a | remedy with | an extra pipe |",
            "",
        ]
    )
    spec["skills"]["gd-doctor"] = {"raw": raw}
    spec["roster"] = ["gd-doctor"]


@pytest.mark.parametrize(
    ("mutate", "expected_substrings"),
    [
        pytest.param(_garbage_top_level_json_mutate, (), id="garbage-top-level-json"),
        pytest.param(
            _wrong_shaped_json_mutate,
            ("'plugins' must be a list, got str",),
            id="wrong-shaped-json",
        ),
        pytest.param(
            _mis_columned_roster_row_mutate,
            ("skills.md", "line 7", "'| gd-doctor |'", "has 1 cell(s)", "declares 2 column(s)"),
            id="mis-columned-roster-row",
        ),
        pytest.param(
            _unescaped_pipe_checks_row_mutate,
            (
                "SKILL.md",
                "line 10",
                "has 6 cell(s)",
                "declares 4 column(s)",
            ),
            id="unescaped-pipe-checks-row",
        ),
    ],
)
def test_checker_never_raises_on_malformed_input(
    tmp_path: Path,
    mutate: Callable[[dict[str, Any]], None],
    expected_substrings: tuple[str, ...],
) -> None:
    findings = _lint(tmp_path, mutate)
    assert isinstance(findings, list)
    assert all(isinstance(finding, str) for finding in findings)
    assert len(findings) > 0
    for substring in expected_substrings:
        assert any(substring in finding for finding in findings), (
            f"expected a finding containing {substring!r}, got {findings}"
        )


def test_checker_returns_no_findings_for_an_all_valid_tree(tmp_path: Path) -> None:
    def mutate(spec: dict[str, Any]) -> None:
        spec["skills"]["gd-sample"] = _default_skill("gd-sample", backend="local")
        spec["skills"]["gd-doctor"] = _default_skill("gd-doctor", backend=None)
        spec["roster"] = ["gd-sample", "gd-doctor"]

    findings = _lint(tmp_path, mutate)
    assert findings == []


def test_real_repository_has_no_findings() -> None:
    """Rules 1 through 8 hold over the real checkout: the canonical roster at
    `.agents/skills`, the roster table in `docs/skills.md`, the plugin
    manifests, and the two documents that carry bare-name references all
    agree. This is also the suite a later work unit observes fail the moment
    it adds a roster row before adding the directory (or vice versa), and
    observes pass once both land together.
    """
    root = repo.find_root(Path(__file__).resolve().parent)
    plugin_root = root / ".claude" / "plugins" / "devcontainer"
    skills_root = root / ".agents" / "skills"
    findings = check_plugin(
        plugin_root,
        skills_root,
        root / "docs" / "skills.md",
        root / "docs" / "devcontainer.md",
        answers,
    )
    assert findings == []
