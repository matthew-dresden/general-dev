"""Pinning test: `docs/azure-requirements.md` names every variable the azure
module declares.

The azure twin of `tests/test_ec2_requirements_doc.py`, which owns the full
rationale (AC-8.2's both-directions correspondence, read from the real
committed sources rather than any literal list carried here). The parser
helpers and the both-directions assertion are imported from that module
rather than restated -- a second copy of the `variable "<name>"` block
parser is exactly the drift its docstring exists to prevent -- and only the
two paths differ: `provider/azure/variables.tf` against
`docs/azure-requirements.md`.

The five cases below mirror the EC2 module's case-for-case:

- Against the two real files, proving the shipped document and the shipped
  module currently agree in both directions.
- Against a synthetic set with one real variable name removed, proving a
  missing variable fails naming that variable.
- Against a synthetic set with one generated, never-declared name added,
  proving an extra variable fails naming that variable.
- Against an unreadable and an unparseable `tmp_path` file, proving both
  failure modes raise `VariablesFileError` naming the file rather than
  returning an empty set that would make the correspondence check pass
  vacuously.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path

import pytest
from gitignore_check import repo_root
from test_ec2_requirements_doc import (
    VariablesFileError,
    _assert_document_names_every_module_variable,
    _extract_declared_variable_names,
    _extract_document_variable_references,
    _read_text,
)

_VARIABLES_TF_RELATIVE_PATH = "provider/azure/variables.tf"
_DOC_RELATIVE_PATH = "docs/azure-requirements.md"


def _variables_tf_path() -> Path:
    return repo_root() / _VARIABLES_TF_RELATIVE_PATH


def _doc_path() -> Path:
    return repo_root() / _DOC_RELATIVE_PATH


def _module_variable_names() -> frozenset[str]:
    """Every variable `provider/azure/variables.tf` declares, read from that file."""
    path = _variables_tf_path()
    return _extract_declared_variable_names(_read_text(path), source_label=str(path))


def _document_variable_references() -> frozenset[str]:
    """Every `var.<name>` reference `docs/azure-requirements.md` makes, read from that file."""
    return _extract_document_variable_references(_read_text(_doc_path()))


def test_document_names_every_module_variable_in_both_directions() -> None:
    """The shipped azure document and the shipped azure module agree exactly."""
    _assert_document_names_every_module_variable(
        _module_variable_names(), _document_variable_references()
    )


def test_missing_variable_fails_naming_the_variable() -> None:
    """A document omitting one declared variable fails naming it."""
    module_vars = _module_variable_names()
    omitted = sorted(module_vars)[0]
    doc_vars = module_vars - {omitted}
    with pytest.raises(AssertionError, match=re.escape(omitted)):
        _assert_document_names_every_module_variable(module_vars, doc_vars)


def test_undeclared_variable_fails_naming_the_variable() -> None:
    """A document naming an undeclared variable fails naming it."""
    module_vars = _module_variable_names()
    undeclared = f"undeclared_{uuid.uuid4().hex}"
    doc_vars = module_vars | {undeclared}
    with pytest.raises(AssertionError, match=re.escape(undeclared)):
        _assert_document_names_every_module_variable(module_vars, doc_vars)


def test_unreadable_variables_file_raises_naming_the_file(tmp_path: Path) -> None:
    """A missing file raises naming its path, never an empty variable set."""
    missing_path = tmp_path / "variables.tf"
    with pytest.raises(VariablesFileError, match=re.escape(str(missing_path))):
        _read_text(missing_path)


def test_unparseable_variables_file_raises_naming_the_file(tmp_path: Path) -> None:
    """A file with no variable block raises naming its path, never an empty set."""
    unparseable_path = tmp_path / "variables.tf"
    unparseable_path.write_text("# no variable blocks in this file\n", encoding="utf-8")
    with pytest.raises(VariablesFileError, match=re.escape(str(unparseable_path))):
        _extract_declared_variable_names(
            _read_text(unparseable_path), source_label=str(unparseable_path)
        )
