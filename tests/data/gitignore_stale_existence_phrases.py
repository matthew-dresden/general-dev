"""The AC-DOC-003 phrase table: point-in-time existence claims that must
never reappear in `.gitignore`, `tests/gitignore_check.py` or
`tests/test_gitignore_allowlist.py`.

Kept as data rather than as a literal inside `tests/test_gitignore_allowlist.py`
itself, because that module is one of the three files AC-DOC-003's own grep
scans: a literal copy of every forbidden phrase living inside a scanned file
would make the criterion's grep permanently return non-zero matches against
its own enforcement mechanism, and CLAUDE.md's input-driven-configuration
rule forbids hard-coded test data regardless.
`tests/test_gitignore_allowlist.py` imports `STALE_EXISTENCE_PHRASES` from
here, the same `tests/data/` convention `tests/data/bypass_denial_cases.json`,
`tests/data/secret_scanner_cases.json` and `tests/data/bypass_permitted_cases.py`
already establish for this repository's test tables.

Each phrase names a claim about *whether* a directory-only re-include
currently exists in the checkout, superseded by this task's rewrite: why
`!/provider/` and `!/remote-instances/` are allowlisted, regardless of
whether either currently exists.
"""

from __future__ import annotations

STALE_EXISTENCE_PHRASES: tuple[str, ...] = (
    "does not exist",
    "do not yet exist",
    "not created yet",
    "PENDING_CONTENT",
    "on hold",
)
