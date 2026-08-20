"""The permitted half of AC-4.6 (spec Section 4.6.1): what `deny_bypass.py` must still allow.

`tests/data/bypass_denial_cases.json` (E2-F2-S2-T1) is the record of every
command `.claude/hooks/deny_bypass.py` must deny. This module is its
complement: the record of commands that share a surface feature with a
denied pattern -- most pointedly, `-n` -- and must still pass through
unblocked, together with the reason each one does. Section 4.6.1 names the
sharpest case: "`git push --dry-run` is allowed: `-n` means dry run on push,
so the argument is read in context rather than matched blindly." A rule
written as a search for the token `-n` cannot make that distinction; only a
rule that first reads the git subcommand can. This table exists so that
distinction has a permanent, reviewable record, not just a comment in the
hook's own source.

`PERMITTED_CASE` (a `PermittedCase`) is the schema. `command` is the line
`tests/test_deny_bypass_permits.py` asserts `evaluate` returns no decision
for. `reason` (AC-DOC-001) is why: the rationale record a future reader
consults before "simplifying" the rule that keeps this command permitted.
`paired_denied_command` and `paired_denied_rule` are optional: where a
permitted command has a same-shape denied counterpart (the `push`/`commit`
split on `-n` is the paradigm case, but a dry run carrying `--no-verify`
instead of a harmless option is another), the pair is recorded together so a
future edit that collapses the two rules into one is caught by the same
table entry that documents why they were separate.

`load_cases` is the validating constructor `PERMITTED_CASES` (the real
table) is built through, and the same function
`tests/test_deny_bypass_permits.py` calls directly with a synthetic,
deliberately malformed sequence to prove a missing required key raises
rather than being silently skipped (AC-TEST-008): a table entry that never
loads is a case that looks covered and is not, exactly the failure mode
Section 3.6.2 calls out for an over-broad *or* silently incomplete guard.
"""

from __future__ import annotations

from dataclasses import dataclass

# The keys every entry must carry, regardless of whether it pairs with a
# denied counterpart.
_REQUIRED_KEYS: frozenset[str] = frozenset({"id", "command", "reason"})

# The two keys that only ever appear together: a permitted command paired
# with a denied counterpart needs both the command and the rule expected to
# fire on it, or neither.
_PAIRED_KEYS: frozenset[str] = frozenset({"paired_denied_command", "paired_denied_rule"})


@dataclass(frozen=True)
class PermittedCase:
    """One permitted command, why it is permitted, and its optional denied pair.

    `paired_denied_command` and `paired_denied_rule` are both `None` when
    the case has no paired counterpart (the ordinary, non-bypass-adjacent
    commands Approach step 5 adds); `load_cases` never leaves exactly one of
    the two set.
    """

    id: str
    command: str
    reason: str
    paired_denied_command: str | None = None
    paired_denied_rule: str | None = None


def load_cases(raw_cases: tuple[dict[str, object], ...]) -> tuple[PermittedCase, ...]:
    """Validate `raw_cases` and convert each entry into a `PermittedCase`.

    Raises:
        ValueError: naming this file and the offending entry's index
            (AC-TEST-008), if an entry is missing one of `_REQUIRED_KEYS`,
            or supplies only one of the two `_PAIRED_KEYS` instead of both
            or neither. A skipped entry is a case that appears covered and
            is not; failing loudly here is what keeps that from happening
            silently.
    """
    cases: list[PermittedCase] = []
    for index, entry in enumerate(raw_cases):
        missing = _REQUIRED_KEYS - entry.keys()
        if missing:
            raise ValueError(
                f"ERROR: malformed entry in {__file__} at index {index}: "
                f"missing required key(s) {sorted(missing)}. "
                "Every entry needs 'id', 'command' and 'reason'."
            )
        present_paired_keys = _PAIRED_KEYS & entry.keys()
        if present_paired_keys and present_paired_keys != _PAIRED_KEYS:
            raise ValueError(
                f"ERROR: malformed entry in {__file__} at index {index}: "
                f"'paired_denied_command' and 'paired_denied_rule' must both be "
                f"present or both be absent, got only {sorted(present_paired_keys)}."
            )
        cases.append(
            PermittedCase(
                id=str(entry["id"]),
                command=str(entry["command"]),
                reason=str(entry["reason"]),
                paired_denied_command=(
                    str(entry["paired_denied_command"])
                    if "paired_denied_command" in entry
                    else None
                ),
                paired_denied_rule=(
                    str(entry["paired_denied_rule"]) if "paired_denied_rule" in entry else None
                ),
            )
        )
    return tuple(cases)


# The Section 4.6.1 discrimination cases (Approach steps 1, 3, 6): a dry-run
# push, bare, option-carrying, or chained, each paired with the commit- or
# push-side spelling that must still be denied, so the two subcommands
# cannot be collapsed into one rule unnoticed.
_DRY_RUN_CASES: tuple[dict[str, object], ...] = (
    {
        "id": "push-dry-run-long-form",
        "command": "git push --dry-run origin main",
        "reason": (
            "--dry-run rehearses the push without changing anything; Section "
            "4.6.1 denies --no-verify under push, not --dry-run."
        ),
        "paired_denied_command": "git push --no-verify origin main",
        "paired_denied_rule": "git-push-no-verify",
    },
    {
        "id": "push-dry-run-short-flag",
        "command": "git push -n origin main",
        "reason": (
            "-n means dry run under push, the opposite of what -n means under "
            "commit; this is the exact discrimination Section 4.6.1 requires."
        ),
        "paired_denied_command": "git commit -n",
        "paired_denied_rule": "git-commit-no-verify",
    },
    {
        "id": "push-dry-run-with-harmless-options",
        "command": "git push --dry-run --tags origin main",
        "reason": (
            "a dry run carrying ordinary push options (--tags) stays permitted; "
            "only --no-verify on the same subcommand changes that."
        ),
        "paired_denied_command": "git push --no-verify --tags origin main",
        "paired_denied_rule": "git-push-no-verify",
    },
    {
        "id": "push-dry-run-chained-segment",
        "command": "make lint && git push --dry-run origin main",
        "reason": (
            "a dry-run push as one segment of a chained command line is still "
            "read in its own segment and stays permitted."
        ),
        "paired_denied_command": "make lint && git push --no-verify origin main",
        "paired_denied_rule": "git-push-no-verify",
    },
)

# The ordinary, non-bypass-adjacent commands Approach step 5 requires: the
# wider permitted surface a hook this narrow must not encroach on. None of
# these pairs with a denied counterpart; there is no denied spelling of
# "check the repository status".
_ORDINARY_CASES: tuple[dict[str, object], ...] = (
    {
        "id": "ordinary-git-status",
        "command": "git status",
        "reason": "touches none of the ten Section 4.6.1 patterns.",
    },
    {
        "id": "ordinary-git-commit-with-message",
        "command": 'git commit -m "add feature"',
        "reason": "an ordinary commit with no hook-skipping flag runs the hooks normally.",
    },
    {
        "id": "ordinary-git-add-no-force",
        "command": "git add docs/readme.md",
        "reason": "git add with no -f/--force never reaches the force-ignored rule.",
    },
    {
        "id": "ordinary-rm-outside-git-hooks",
        "command": "rm build/output.tmp",
        "reason": "rm of a path outside .git/hooks removes an ordinary build artifact.",
    },
    {
        "id": "ordinary-chmod-outside-git-hooks",
        "command": "chmod +x scripts/build.sh",
        "reason": "chmod of a path outside .git/hooks changes an ordinary script's mode.",
    },
    {
        "id": "ordinary-make-lint",
        "command": "make lint",
        "reason": "make lint is not make hooks-uninstall; the lint target never removes a hook.",
    },
)

_RAW_CASES: tuple[dict[str, object], ...] = _DRY_RUN_CASES + _ORDINARY_CASES

PERMITTED_CASES: tuple[PermittedCase, ...] = load_cases(_RAW_CASES)
