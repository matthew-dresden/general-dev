"""Pinning test: `docs/ec2-requirements.md` names every variable `variables.tf` declares.

AC-8.2 (spec Section 8.1) requires the operations-facing requirements document to
name every Terraform variable an operations team must satisfy, and requires that
requirement to be verified by a test comparing the document against
`provider/aws/variables.tf` rather than against a literal list this test module
carries itself -- a list here would drift the moment either file changed without
this module changing too, which is exactly the drift AC-8.2 exists to catch.

Both sides are read from their real, committed source: `_module_variable_names`
parses every `variable "<name>" {` block in `provider/aws/variables.tf`, and
`_document_variable_references` parses every `var.<name>` reference in
`docs/ec2-requirements.md`. The document is required to use that reference form
(the same form `provider/aws/README.md` and `variables.tf`'s own validation
messages already use) precisely because it gives this test an unambiguous marker
to key on: a bare mention of an input's plain-English name would not tell this
parser it is looking at a Terraform variable, and a hand-maintained needle list
would reintroduce the drift risk AC-8.2 rules out.

`_assert_document_names_every_module_variable` is exercised three ways:

- Against the two real files, proving the shipped document and the shipped
  module currently agree in both directions (AC-TEST-001).
- Against a synthetic set with one real variable name removed, proving a
  missing variable fails naming that variable (AC-TEST-002).
- Against a synthetic set with one generated, never-declared name added,
  proving an extra variable fails naming that variable (AC-TEST-003).

`_read_text` and `_extract_declared_variable_names` are exercised separately
against an unreadable and an unparseable `tmp_path` file, proving both failure
modes raise `VariablesFileError` naming the file rather than returning an
empty set that would make the correspondence check pass vacuously
(AC-TEST-004).
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path

import pytest
from gitignore_check import repo_root

_VARIABLES_TF_RELATIVE_PATH = "provider/aws/variables.tf"
_DOC_RELATIVE_PATH = "docs/ec2-requirements.md"

# Every root-level Terraform variable declaration: `variable "name" {` at the
# start of a line, the form every declaration in `provider/aws/variables.tf`
# uses. Anchored to line-start so a `var.name` reference inside a
# description's prose (every variable's own `description` block quotes
# several) can never be mistaken for a second declaration of that name.
_VARIABLE_BLOCK_PATTERN = re.compile(r'^variable\s+"([A-Za-z_][A-Za-z0-9_]*)"\s*\{', re.MULTILINE)

# Every `var.<name>` reference in the document, the same reference form
# `provider/aws/README.md` and `variables.tf`'s own validation error messages
# already use for the identical purpose (naming a Terraform variable in prose
# meant for a human reader).
_DOC_VARIABLE_REFERENCE_PATTERN = re.compile(r"\bvar\.([A-Za-z_][A-Za-z0-9_]*)\b")


class VariablesFileError(RuntimeError):
    """Raised when a Terraform variables file cannot be read or parsed.

    Never returned as an empty variable set: an unreadable or unparseable file
    would otherwise make `_assert_document_names_every_module_variable` pass
    vacuously (there is nothing in an empty set for the document to omit),
    which is precisely the false-pass AC-TEST-004 exists to rule out.
    """


def _read_text(path: Path) -> str:
    """`path`'s content, decoded as UTF-8, or `VariablesFileError` naming `path`."""
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise VariablesFileError(
            f"ERROR: cannot read {path}: {exc}. Confirm the file exists and is "
            "readable, then re-run this check."
        ) from exc


def _extract_declared_variable_names(text: str, *, source_label: str) -> frozenset[str]:
    """Every `variable "<name>" {` declaration in `text`.

    Raises `VariablesFileError` naming `source_label` when no declaration is
    found, rather than returning an empty `frozenset`: an empty result here
    would let a corrupted or truncated variables file compare as trivially
    equal to an empty document instead of failing loudly.
    """
    names = frozenset(match.group(1) for match in _VARIABLE_BLOCK_PATTERN.finditer(text))
    if not names:
        raise VariablesFileError(
            f'ERROR: no `variable "<name>" {{` declarations found in {source_label}. '
            "The file is empty, malformed, or not a Terraform variables file; fix "
            "the source before re-running this check."
        )
    return names


def _extract_document_variable_references(text: str) -> frozenset[str]:
    """Every `var.<name>` reference in `text`."""
    return frozenset(match.group(1) for match in _DOC_VARIABLE_REFERENCE_PATTERN.finditer(text))


def _variables_tf_path() -> Path:
    return repo_root() / _VARIABLES_TF_RELATIVE_PATH


def _doc_path() -> Path:
    return repo_root() / _DOC_RELATIVE_PATH


def _module_variable_names() -> frozenset[str]:
    """Every variable `provider/aws/variables.tf` declares, read from that file."""
    path = _variables_tf_path()
    return _extract_declared_variable_names(_read_text(path), source_label=str(path))


def _document_variable_references() -> frozenset[str]:
    """Every `var.<name>` reference `docs/ec2-requirements.md` makes, read from that file."""
    return _extract_document_variable_references(_read_text(_doc_path()))


def _assert_document_names_every_module_variable(
    module_vars: frozenset[str], doc_vars: frozenset[str]
) -> None:
    """`doc_vars` names exactly `module_vars`, no more and no fewer, naming any mismatch."""
    missing = sorted(module_vars - doc_vars)
    assert not missing, (
        f"{_DOC_RELATIVE_PATH} omits Terraform variable(s) declared in "
        f"{_VARIABLES_TF_RELATIVE_PATH}: {missing}. An operations team following "
        "this document would not know these inputs exist."
    )
    extra = sorted(doc_vars - module_vars)
    assert not extra, (
        f"{_DOC_RELATIVE_PATH} names variable(s) that {_VARIABLES_TF_RELATIVE_PATH} "
        f"does not declare: {extra}. An operations team would supply a value that "
        "has no effect."
    )


def test_document_names_every_module_variable_in_both_directions() -> None:
    """AC-TEST-001: the shipped document and the shipped module agree exactly."""
    _assert_document_names_every_module_variable(
        _module_variable_names(), _document_variable_references()
    )


def test_missing_variable_fails_naming_the_variable() -> None:
    """AC-TEST-002: a document omitting one declared variable fails naming it."""
    module_vars = _module_variable_names()
    omitted = sorted(module_vars)[0]
    doc_vars = module_vars - {omitted}
    with pytest.raises(AssertionError, match=re.escape(omitted)):
        _assert_document_names_every_module_variable(module_vars, doc_vars)


def test_undeclared_variable_fails_naming_the_variable() -> None:
    """AC-TEST-003: a document naming an undeclared variable fails naming it."""
    module_vars = _module_variable_names()
    undeclared = f"undeclared_{uuid.uuid4().hex}"
    doc_vars = module_vars | {undeclared}
    with pytest.raises(AssertionError, match=re.escape(undeclared)):
        _assert_document_names_every_module_variable(module_vars, doc_vars)


def test_unreadable_variables_file_raises_naming_the_file(tmp_path: Path) -> None:
    """AC-TEST-004, read failure: a missing file raises naming its path."""
    missing_path = tmp_path / "variables.tf"
    with pytest.raises(VariablesFileError, match=re.escape(str(missing_path))):
        _read_text(missing_path)


def test_unparseable_variables_file_raises_naming_the_file(tmp_path: Path) -> None:
    """AC-TEST-004, parse failure: a file with no variable block raises naming its path.

    Proves the failure names the file rather than the parser silently returning
    an empty `frozenset`, which would otherwise pass the correspondence check
    vacuously against an empty document.
    """
    unparseable_path = tmp_path / "variables.tf"
    unparseable_path.write_text("# no variable blocks in this file\n", encoding="utf-8")
    with pytest.raises(VariablesFileError, match=re.escape(str(unparseable_path))):
        _extract_declared_variable_names(
            _read_text(unparseable_path), source_label=str(unparseable_path)
        )
