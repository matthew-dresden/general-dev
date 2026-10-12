"""Tests for the permitted half of AC-4.6 (spec Section 4.6.1, Section 3.6.2).

`tests/test_deny_bypass_hook.py` (E2-F2-S2-T1) is the denial suite: every
Section 4.6.1 pattern, denied. This module is its complement, delivered as
its own work unit (E2-F2-S2-T2) precisely so the positive evidence -- what
must stay permitted -- is not buried inside a file whose subject is refusal.
Section 4.6.1's own example is the reason the distinction matters: "`git push
--dry-run` is allowed: `-n` means dry run on push, so the argument is read in
context rather than matched blindly." The same two characters deny under
`commit` and permit under `push`; a rule that degraded to a bare `-n` search
would forbid the rehearsal and, per Section 3.6.2, teach the people it
inconvenienced to route around the hook entirely.

`.claude/hooks/deny_bypass.py` (E2-F2-S2-T1) does not live on a package path
pytest resolves by name -- it is invoked directly by a bare `python3`, with
no `PYTHONPATH` of its own -- so `_import_deny_bypass` loads it from its file
path with `importlib.util.spec_from_file_location` rather than a dotted
import, exactly as `tests/test_deny_bypass_hook.py` does. That load is
deferred into a function body, called by every test that needs it, not run
once at module scope, for the same reason that module's own docstring
documents: a TDD RED re-verification pass that stashes a manifest file must
fail only the one test node reading it, not collection for the whole file.

`tests/data/bypass_permitted_cases.py` and `tests/data/bypass_denial_cases.json`
are the exception already established in this suite: both are static data
files that import nothing from `deny_bypass.py`, so reading them once at
module scope, with an ordinary `from data.bypass_permitted_cases import ...`
and a plain `json.loads` respectively, never depends on the module under
test -- the same reasoning `tests/test_deny_bypass_hook.py`'s own
`_load_cases` already relies on for the JSON table. `tests/` carries no
`__init__.py` at any level, so pytest's default import mode puts `tests/` on
`sys.path` and `tests/data/` resolves as an implicit namespace package
(no `__init__.py` of its own needed) the same way `tests/gitfixtures.py`
resolves as a sibling module.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest
from data.bypass_permitted_cases import PERMITTED_CASES, PermittedCase, load_cases


def _hook_module_path() -> Path:
    return Path(__file__).resolve().parent.parent / ".claude" / "hooks" / "deny_bypass.py"


def _denial_cases_path() -> Path:
    return Path(__file__).resolve().parent / "data" / "bypass_denial_cases.json"


def _import_deny_bypass() -> ModuleType:
    """Load `.claude/hooks/deny_bypass.py` from its file path.

    Duplicated from `tests/test_deny_bypass_hook.py` rather than shared,
    because this work unit's Changes Manifest owns only this file and
    `tests/data/bypass_permitted_cases.py`; extracting a shared loader into
    `tests/gitfixtures.py` would touch a file outside that Manifest. See
    that module's own docstring for why the load is deferred into a
    function body instead of module scope, and why the module is registered
    in `sys.modules` before `exec_module` runs.
    """
    path = _hook_module_path()
    spec = importlib.util.spec_from_file_location("deny_bypass", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot build an import spec for {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _denial_cases() -> tuple[dict[str, object], ...]:
    return tuple(json.loads(_denial_cases_path().read_text(encoding="utf-8")))


def _never_ignored(_path: str) -> bool:
    """An `is_ignored` stand-in for cases that never reach a `git add -f` rule.

    None of `PERMITTED_CASES` invokes `git add -f`/`--force`
    (`ordinary-git-add-no-force` deliberately carries neither), so this
    should never actually be called; it exists only to satisfy `evaluate`'s
    signature.
    """
    return False


_PERMITTED_IDS: list[str] = [case.id for case in PERMITTED_CASES]

_PAIRED_CASES: tuple[PermittedCase, ...] = tuple(
    case for case in PERMITTED_CASES if case.paired_denied_command is not None
)
_PAIRED_IDS: list[str] = [case.id for case in _PAIRED_CASES]


@pytest.mark.parametrize("case", PERMITTED_CASES, ids=_PERMITTED_IDS)
def test_permitted_case_is_allowed_with_no_decision(case: PermittedCase) -> None:
    """AC-TEST-001, AC-TEST-002, AC-TEST-003, AC-TEST-004, AC-TEST-005, AC-TEST-006.

    Every entry's primary `command` is asserted permitted here, driven
    entirely from `tests/data/bypass_permitted_cases.py`'s table -- no
    command literal appears in this function body. A permitted case
    `evaluate` denies fails naming the command, the rule that fired and its
    reason; a permitted case `evaluate` raises for fails naming the
    exception and the command, rather than letting either read as a silent
    pass or an opaque traceback.
    """
    deny_bypass = _import_deny_bypass()

    try:
        decision = deny_bypass.evaluate(case.command, _never_ignored)
    except Exception as exc:  # the task-specific error path: name it, don't let it escape opaque
        pytest.fail(
            f"evaluate raised {exc.__class__.__name__} for permitted command "
            f"{case.command!r} ({case.reason}): {exc}"
        )

    if decision is not None:
        pytest.fail(
            f"expected {case.command!r} to be permitted ({case.reason}), but rule "
            f"{decision.rule!r} denied it: {decision.reason}"
        )


@pytest.mark.parametrize("case", _PAIRED_CASES, ids=_PAIRED_IDS)
def test_paired_denial_case_is_still_denied(case: PermittedCase) -> None:
    """AC-TEST-002, AC-TEST-003, AC-TEST-004: the paired counterpart stays denied.

    Proves the discrimination is real, not accidental: for every permitted
    dry-run case, the same shape carrying the actual denied flag (`-n` under
    `commit`, or `--no-verify` under `push`) is still caught by the rule
    named in the table.
    """
    deny_bypass = _import_deny_bypass()
    assert case.paired_denied_command is not None
    assert case.paired_denied_rule is not None

    decision = deny_bypass.evaluate(case.paired_denied_command, _never_ignored)

    assert decision is not None, (
        f"expected paired command {case.paired_denied_command!r} (the denied "
        f"counterpart of permitted {case.command!r}) to be denied, but evaluate "
        "returned no decision"
    )
    assert decision.rule == case.paired_denied_rule, (
        f"expected {case.paired_denied_command!r} to be denied by rule "
        f"{case.paired_denied_rule!r}, got {decision.rule!r}: {decision.reason}"
    )


def test_every_permitted_case_has_a_nonempty_reason() -> None:
    """AC-DOC-001: every entry carries a reason stating why it is permitted."""
    for case in PERMITTED_CASES:
        assert case.reason, f"case {case.id!r} has no reason recorded"


def test_permitted_case_ids_are_unique() -> None:
    """Hygiene: a duplicate `id` would silently collapse two `-k`-selectable test nodes into one."""
    ids = [case.id for case in PERMITTED_CASES]
    assert len(ids) == len(set(ids)), f"duplicate case ids: {sorted(ids)}"


def test_permitted_commands_do_not_overlap_denial_table() -> None:
    """AC-TEST-007: the two tables cannot both claim the same command.

    Compares each table's own asserted-outcome command set: `PERMITTED_CASES`'
    `command` field (the commands this suite asserts are permitted) against
    `tests/data/bypass_denial_cases.json`'s `command` field (the commands
    E2-F2-S2-T1's suite asserts are denied). A `paired_denied_command` is
    deliberately excluded from this comparison: it is not a claim this table
    makes about being permitted, it is the denied counterpart the pairing
    test above already asserts is denied, so it appearing in the denial
    table too would agree with both suites rather than contradict either.
    """
    permitted_commands = {case.command for case in PERMITTED_CASES}
    denial_commands = {str(entry["command"]) for entry in _denial_cases()}

    overlap = permitted_commands & denial_commands

    assert not overlap, (
        "commands claimed permitted in tests/data/bypass_permitted_cases.py also "
        f"appear as denied in tests/data/bypass_denial_cases.json: {sorted(overlap)}"
    )


def test_malformed_entry_missing_required_key_raises_naming_file_and_index() -> None:
    """AC-TEST-008: a table entry missing a required key raises at load, not silently skipped."""
    malformed_cases: tuple[dict[str, object], ...] = (
        {"id": "well-formed", "command": "n/a", "reason": "n/a"},
        {"id": "missing-reason", "command": "n/a"},
    )

    with pytest.raises(ValueError) as excinfo:
        load_cases(malformed_cases)

    message = str(excinfo.value)
    assert "bypass_permitted_cases.py" in message
    assert "index 1" in message
    assert "reason" in message


def test_malformed_entry_with_only_one_paired_key_raises() -> None:
    """`load_cases`: the two paired-denial keys must both be present, or neither one."""
    malformed_cases: tuple[dict[str, object], ...] = (
        {
            "id": "half-paired",
            "command": "n/a",
            "reason": "n/a",
            "paired_denied_command": "n/a",
        },
    )

    with pytest.raises(ValueError) as excinfo:
        load_cases(malformed_cases)

    message = str(excinfo.value)
    assert "bypass_permitted_cases.py" in message
    assert "index 0" in message
