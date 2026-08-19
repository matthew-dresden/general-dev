"""Tests for devcontainer_config.secrets: the range-scanning half (spec Section 4.6).

Section 4.6 is explicit about why the tip alone is not enough: a credential
introduced early in a pushed range and removed later still reaches the
remote, in history, even though the tip is clean. These tests build real,
disposable git repositories under `tmp_path` and drive `scan_range` and
`render_range_report` against them, rather than mocking git, because the
whole point of range mode -- walking a real commit graph and diffing each
commit against its actual parent (or the empty tree for a root commit) -- is
a distinction only a real git repository can prove.

The `devcontainer_config` import is deferred into function bodies (via
`import_secrets`), for the same reason documented in `tests/test_secrets.py`
and `tests/test_lint_secrets_cli.py`: the TDD RED gate stashes this unit's
production-source files and re-runs a single named test node, and a
module-level `from devcontainer_config.secrets import ...` would fail
COLLECTION for the whole file instead of failing the one test for the real
reason.

No credential-shaped literal is ever stored pre-assembled in this file. A
positive sample is built at run time from
`devcontainer_config.secrets.SAMPLE_PREFIXES` plus a `uuid.uuid4()` suffix,
the same discipline `tests/test_secrets.py` documents for the scanner's own
case table, so this file itself never becomes something a future
`make lint-secrets RANGE=<a>..<b>` run would flag.

Every one of the git-fixture primitives below (`generated_root`,
`init_repo`, `stage_text`, `commit_text`, `commit_bytes`, `rev_parse`,
`credential_line`) lives in `tests/gitfixtures.py` (shared with
`tests/test_cli.py` and `tests/test_lint_secrets_cli.py`) rather than being
redefined here; see that module's docstring for why.
"""

from __future__ import annotations

import subprocess
import uuid
from pathlib import Path

import pytest
from gitfixtures import (
    commit_bytes,
    commit_text,
    credential_line,
    generated_root,
    import_secrets,
    init_repo,
    rev_parse,
    stage_text,
)


def test_credential_added_then_removed_is_reported_against_the_adding_commit(
    tmp_path: Path,
) -> None:
    """AC-TEST-001 / AC-FUNC-001 / AC-FUNC-003: added-then-removed is reported at commit one."""
    secrets = import_secrets()
    root = generated_root(tmp_path)
    init_repo(root)
    line = credential_line()

    commit_text(root, "README.md", "base\n", "base commit")
    base_commit = rev_parse(root, "HEAD")
    commit_text(root, "src/config.py", line, "add credential")
    adding_commit = rev_parse(root, "HEAD")
    commit_text(root, "src/config.py", "CREDENTIAL=removed\n", "remove credential")
    tip_commit = rev_parse(root, "HEAD")

    report = secrets.scan_range(root, f"{base_commit}..{tip_commit}")

    assert report.commit_count == 2
    assert len(report.findings) == 1
    finding_record = report.findings[0]
    assert finding_record.commit == adding_commit
    assert finding_record.path == "src/config.py"
    assert finding_record.finding.line_number == 1
    assert finding_record.finding.detector_id == "aws-access-key-id"


def test_root_commit_is_scanned_against_the_empty_tree(tmp_path: Path) -> None:
    """AC-TEST-002 / AC-FUNC-002: a root commit is scanned rather than skipped."""
    secrets = import_secrets()
    root = generated_root(tmp_path)
    init_repo(root)
    line = credential_line()

    commit_text(root, "src/config.py", line, "root commit with credential")
    main_root_commit = rev_parse(root, "HEAD")

    subprocess.run(
        ["git", "-C", str(root), "checkout", "--orphan", "unrelated"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(root), "commit", "--allow-empty", "-m", "unrelated base"],
        check=True,
        capture_output=True,
    )
    unrelated_commit = rev_parse(root, "HEAD")

    report = secrets.scan_range(root, f"{unrelated_commit}..{main_root_commit}")

    assert report.commit_count == 1
    assert len(report.findings) == 1
    finding_record = report.findings[0]
    assert finding_record.commit == main_root_commit
    assert finding_record.path == "src/config.py"
    assert finding_record.finding.line_number == 1


def test_same_detector_fires_in_staged_mode_and_range_mode_for_identical_content(
    tmp_path: Path,
) -> None:
    """AC-TEST-003 / AC-FUNC-005: staged mode and range mode share one detector registry."""
    secrets = import_secrets()
    root = generated_root(tmp_path)
    init_repo(root)
    line = credential_line()

    commit_text(root, "README.md", "base\n", "base commit")
    base_commit = rev_parse(root, "HEAD")
    commit_text(root, "src/committed.py", line, "commit credential for range mode")
    tip_commit = rev_parse(root, "HEAD")
    stage_text(root, "src/staged.py", line)

    range_report = secrets.scan_range(root, f"{base_commit}..{tip_commit}")
    staged_report = secrets.run_staged_scan(root)

    range_detector_ids = {item.finding.detector_id for item in range_report.findings}
    staged_detector_ids = {item.finding.detector_id for item in staged_report.findings}

    assert range_detector_ids == staged_detector_ids == {"aws-access-key-id"}


def test_render_range_report_contains_header_commit_history_sentence_and_both_remedies(
    tmp_path: Path,
) -> None:
    """AC-TEST-004 / AC-FUNC-004: header, commit attribution, in-history sentence, remedies."""
    secrets = import_secrets()
    root = generated_root(tmp_path)
    init_repo(root)
    line = credential_line()

    commit_text(root, "README.md", "base\n", "base commit")
    base_commit = rev_parse(root, "HEAD")
    commit_text(root, "src/config.py", line, "add credential")
    credential_commit = rev_parse(root, "HEAD")
    commit_text(root, "src/config.py", "CREDENTIAL=removed\n", "remove credential")
    tip_commit = rev_parse(root, "HEAD")

    revision_range = f"{base_commit}..{tip_commit}"
    report = secrets.scan_range(root, revision_range)

    rendered = secrets.render_range_report(report)

    assert rendered.splitlines()[0] == f"[LINT] secrets in pushed range {revision_range}"
    assert credential_commit in rendered
    assert "src/config.py:1" in rendered
    assert "The value is in history, so removing it now is not enough." in rendered
    assert f"git rebase -i {base_commit}" in rendered
    assert "rotate the exposed value and push deliberately with a recorded approval." in rendered
    assert line.strip() not in rendered


def test_empty_range_reports_zero_commits_and_no_findings(tmp_path: Path) -> None:
    """AC-TEST-006 / AC-FUNC-006: a range with no commits reports zero, not nothing."""
    secrets = import_secrets()
    root = generated_root(tmp_path)
    init_repo(root)
    commit_text(root, "README.md", "base\n", "base commit")
    head = rev_parse(root, "HEAD")

    report = secrets.scan_range(root, f"{head}..{head}")

    assert report.commit_count == 0
    assert report.findings == ()
    rendered = secrets.render_range_report(report)
    assert "commits scanned: 0" in rendered


@pytest.mark.parametrize("bad_range", ["not-a-range", "a...b", "..b", "a..", ""])
def test_scan_range_rejects_malformed_range_argument(tmp_path: Path, bad_range: str) -> None:
    """AC-TEST-005: a malformed range argument fails naming the expected shape."""
    secrets = import_secrets()
    root = generated_root(tmp_path)
    init_repo(root)

    with pytest.raises(secrets.SecretScanError, match="malformed") as exc_info:
        secrets.scan_range(root, bad_range)

    assert "ERROR:" in str(exc_info.value)


def test_scan_range_reports_when_revision_cannot_be_resolved(tmp_path: Path) -> None:
    """AC-TEST-005: a range naming a revision git cannot resolve fails naming it."""
    secrets = import_secrets()
    root = generated_root(tmp_path)
    init_repo(root)
    commit_text(root, "README.md", "base\n", "base commit")
    head = rev_parse(root, "HEAD")
    missing_ref = f"does-not-exist-{uuid.uuid4().hex}"

    with pytest.raises(secrets.SecretScanError, match="cannot resolve range") as exc_info:
        secrets.scan_range(root, f"{missing_ref}..{head}")

    assert "ERROR:" in str(exc_info.value)
    assert missing_ref in str(exc_info.value)


def test_scan_range_reports_when_commit_contains_non_utf8_blob(tmp_path: Path) -> None:
    """AC-TEST-005: a NUL-free, non-UTF-8 blob git diffs as text fails naming the commit.

    `\\xff\\xfe` holds no NUL byte, so git's own binary heuristic classifies
    this blob as text and `git diff-tree -p` prints its bytes literally
    inside the patch rather than collapsing the section to a `Binary
    files ... differ` line. That is what makes `added_lines`'s own
    `stdout.decode("utf-8")` the call that fails here -- see
    `test_scan_range_reports_a_credential_in_a_binary_classified_file` for
    the different, NUL-bearing case git classifies as binary instead,
    where no bytes ever reach that decode at all.
    """
    secrets = import_secrets()
    root = generated_root(tmp_path)
    init_repo(root)
    commit_text(root, "README.md", "base\n", "base commit")
    base_commit = rev_parse(root, "HEAD")
    commit_bytes(root, "notes.txt", b"CREDENTIAL=\xff\xfe not valid utf8\n", "non-utf8 commit")
    tip_commit = rev_parse(root, "HEAD")

    with pytest.raises(secrets.SecretScanError, match="not valid UTF-8") as exc_info:
        secrets.scan_range(root, f"{base_commit}..{tip_commit}")

    assert "ERROR:" in str(exc_info.value)
    assert tip_commit in str(exc_info.value)


def test_scan_range_reports_a_credential_in_a_binary_classified_file(tmp_path: Path) -> None:
    """AC-FUNC-001 / AC-FUNC-005: a NUL-containing file git calls binary is still scanned.

    Regression for code_review's finding on `_parse_added_lines`: a commit
    whose first 8000 bytes hold a NUL byte makes `git diff-tree -p
    --no-color -U0 -r` emit no hunks at all for that file, only a `Binary
    files /dev/null and b/<path> differ` line. That line started with
    neither `diff --git `, `+++ `, `--- `, a hunk header, `+`, `-` nor `\\`,
    so before the fix it matched none of the parser's branches and fell
    through the loop silently -- the range scan reported zero findings for
    a commit that added a credential, while `staged_blob` reads the
    identical bytes through `git show :<path>` with no such filtering and
    reports the same credential (AC-FUNC-005: staged mode and range mode
    must not diverge).
    """
    secrets = import_secrets()
    root = generated_root(tmp_path)
    init_repo(root)
    line = credential_line()

    commit_text(root, "README.md", "base\n", "base commit")
    base_commit = rev_parse(root, "HEAD")
    adding_commit = commit_bytes(
        root, "blob.bin", line.encode("utf-8") + b"\x00BINARY\n", "add binary credential"
    )

    report = secrets.scan_range(root, f"{base_commit}..{adding_commit}")

    assert len(report.findings) == 1
    finding_record = report.findings[0]
    assert finding_record.commit == adding_commit
    assert finding_record.path == "blob.bin"
    assert finding_record.finding.line_number == 1
    assert finding_record.finding.detector_id == "aws-access-key-id"


def test_scan_range_reports_when_a_binary_classified_blob_is_not_valid_utf8(
    tmp_path: Path,
) -> None:
    """AC-TEST-005: a binary-classified blob that is not valid UTF-8 fails naming the commit.

    `\\xff\\xfe` together with the NUL byte that makes git classify the
    file as binary is content `_binary_post_image_lines` reads straight
    off the blob with `git show`, unlike the NUL-free case
    `test_scan_range_reports_when_commit_contains_non_utf8_blob` exercises
    against `added_lines`'s own diff-text decode -- this is the decode
    inside `_binary_post_image_lines` failing instead.
    """
    secrets = import_secrets()
    root = generated_root(tmp_path)
    init_repo(root)
    commit_text(root, "README.md", "base\n", "base commit")
    base_commit = rev_parse(root, "HEAD")
    commit_bytes(root, "blob.bin", b"CREDENTIAL=\xff\xfe\x00BINARY\n", "non-utf8 binary commit")
    tip_commit = rev_parse(root, "HEAD")

    with pytest.raises(secrets.SecretScanError, match="not valid UTF-8") as exc_info:
        secrets.scan_range(root, f"{base_commit}..{tip_commit}")

    assert "ERROR:" in str(exc_info.value)
    assert tip_commit in str(exc_info.value)
    assert "blob.bin" in str(exc_info.value)


def test_added_line_starting_with_plus_plus_space_is_not_mistaken_for_a_file_header(
    tmp_path: Path,
) -> None:
    """AC-FUNC-001 / AC-FUNC-003: an added line whose text starts with '++ ' is still scanned.

    Regression for code_review's finding on `_parse_added_lines`: that exact
    content, once prefixed by git's own added-line '+', reads back as
    '+++ patch fragment' -- indistinguishable by a naive prefix check from a
    real '+++ b/<path>' diff file header. Before the fix this silently
    dropped every remaining added line of the file (`current_path` fell back
    to `None`), so the credential on the very next line went unreported with
    exit 0.
    """
    secrets = import_secrets()
    root = generated_root(tmp_path)
    init_repo(root)
    line = credential_line()

    commit_text(root, "README.md", "base\n", "base commit")
    base_commit = rev_parse(root, "HEAD")
    commit_text(
        root, "patch.txt", f"++ patch fragment\n{line}", "add patch fragment then credential"
    )
    adding_commit = rev_parse(root, "HEAD")

    report = secrets.scan_range(root, f"{base_commit}..{adding_commit}")

    assert len(report.findings) == 1
    finding_record = report.findings[0]
    assert finding_record.commit == adding_commit
    assert finding_record.path == "patch.txt"
    assert finding_record.finding.line_number == 2
    assert finding_record.finding.detector_id == "aws-access-key-id"


def test_added_line_in_an_unquoted_non_ascii_path_is_still_scanned(tmp_path: Path) -> None:
    """AC-FUNC-001 / AC-FUNC-003: a credential in a non-ASCII-named file is still scanned.

    Regression for code_review's finding on `_DIFF_NEW_FILE_HEADER_PATTERN`:
    without `core.quotepath=false`, git quotes and octal-escapes a diff
    header path that contains a non-ASCII byte
    (`+++ "b/caf\\303\\251_config.py"`), which the plain
    `^\\+\\+\\+ b/(.+)$` pattern never matched, so `current_path` fell back
    to `None` and the credential in that file went unreported with exit 0.
    `added_lines` now runs `git diff-tree` under `core.quotepath=false`
    (see that function's docstring), so a non-ASCII byte alone no longer
    triggers quoting and this header is emitted in the plain form; this
    test proves the plain form still resolves a non-ASCII path correctly.
    The quoted form -- still reachable when a path holds a literal double
    quote or backslash -- has its own coverage in
    `test_added_line_in_a_double_quote_named_path_is_still_scanned` and
    `test_added_line_in_a_backslash_named_path_is_still_scanned`.
    """
    secrets = import_secrets()
    root = generated_root(tmp_path)
    init_repo(root)
    line = credential_line()

    commit_text(root, "README.md", "base\n", "base commit")
    base_commit = rev_parse(root, "HEAD")
    commit_text(root, "src/café_config.py", line, "add credential in non-ascii path")
    adding_commit = rev_parse(root, "HEAD")

    report = secrets.scan_range(root, f"{base_commit}..{adding_commit}")

    assert len(report.findings) == 1
    finding_record = report.findings[0]
    assert finding_record.commit == adding_commit
    assert finding_record.path == "src/café_config.py"
    assert finding_record.finding.line_number == 1
    assert finding_record.finding.detector_id == "aws-access-key-id"


def test_added_line_in_a_double_quote_named_path_is_still_scanned(tmp_path: Path) -> None:
    """AC-FUNC-001 / AC-FUNC-003: a credential in a path holding a literal `"` is still scanned.

    Regression for test_review's COVERAGE_REGRESSION finding: once
    `added_lines` ran `git diff-tree` under `core.quotepath=false`, a
    non-ASCII byte alone stopped triggering git's C-quoted diff header
    form, so the quoted branch of `_diff_new_file_path` and all of
    `_unescape_git_quoted_path` went unexercised by any test. A literal
    double quote in a path still forces quoting regardless of
    `core.quotepath`, so this proves that branch is both still reachable
    through a real diff and still correctly reversed.
    """
    secrets = import_secrets()
    root = generated_root(tmp_path)
    init_repo(root)
    line = credential_line()

    commit_text(root, "README.md", "base\n", "base commit")
    base_commit = rev_parse(root, "HEAD")
    quoted_relative = 'src/we"ird.py'
    commit_text(root, quoted_relative, line, "add credential in double-quote-named path")
    adding_commit = rev_parse(root, "HEAD")

    report = secrets.scan_range(root, f"{base_commit}..{adding_commit}")

    assert len(report.findings) == 1
    finding_record = report.findings[0]
    assert finding_record.commit == adding_commit
    assert finding_record.path == quoted_relative
    assert finding_record.finding.line_number == 1
    assert finding_record.finding.detector_id == "aws-access-key-id"


def test_added_line_in_a_backslash_named_path_is_still_scanned(tmp_path: Path) -> None:
    """AC-FUNC-001 / AC-FUNC-003: a credential in a path holding a literal `\\` is still scanned.

    Same regression as the double-quote case above, for the other
    character that forces git's C-quoted diff header form regardless of
    `core.quotepath`: a literal backslash.
    """
    secrets = import_secrets()
    root = generated_root(tmp_path)
    init_repo(root)
    line = credential_line()

    commit_text(root, "README.md", "base\n", "base commit")
    base_commit = rev_parse(root, "HEAD")
    quoted_relative = "src/back\\slash.py"
    commit_text(root, quoted_relative, line, "add credential in backslash-named path")
    adding_commit = rev_parse(root, "HEAD")

    report = secrets.scan_range(root, f"{base_commit}..{adding_commit}")

    assert len(report.findings) == 1
    finding_record = report.findings[0]
    assert finding_record.commit == adding_commit
    assert finding_record.path == quoted_relative
    assert finding_record.finding.line_number == 1
    assert finding_record.finding.detector_id == "aws-access-key-id"


def test_scan_range_does_not_raise_on_a_commit_that_deletes_a_file(tmp_path: Path) -> None:
    """AC-FUNC-001 / AC-FUNC-006: a commit that deletes a file does not abort the range scan.

    Regression for code_review's finding on `_diff_new_file_path`: `git
    diff-tree -p --no-color -U0 -r` emits `+++ /dev/null` for the delete
    side of a deletion. Neither `_DIFF_NEW_FILE_HEADER_PATTERN` nor its
    quoted counterpart matches that, so before the fix `_diff_new_file_path`
    raised `SecretScanError` for every deletion commit -- aborting the
    whole range scan and hiding the finding on the commit that added the
    credential in the first place, even though a deletion emits no `+`
    lines and so cannot itself hide anything.
    """
    secrets = import_secrets()
    root = generated_root(tmp_path)
    init_repo(root)
    line = credential_line()

    commit_text(root, "README.md", "base\n", "base commit")
    base_commit = rev_parse(root, "HEAD")
    commit_text(root, "src/config.py", line, "add credential")
    adding_commit = rev_parse(root, "HEAD")
    subprocess.run(
        ["git", "-C", str(root), "rm", "-q", "src/config.py"], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(root), "commit", "-m", "delete credential file"],
        check=True,
        capture_output=True,
    )
    delete_commit = rev_parse(root, "HEAD")

    report = secrets.scan_range(root, f"{base_commit}..{delete_commit}")

    assert report.commit_count == 2
    assert len(report.findings) == 1
    finding_record = report.findings[0]
    assert finding_record.commit == adding_commit
    assert finding_record.path == "src/config.py"
    assert finding_record.finding.line_number == 1
    assert finding_record.finding.detector_id == "aws-access-key-id"


def test_scan_range_does_not_raise_on_a_commit_that_renames_a_file(tmp_path: Path) -> None:
    """AC-FUNC-001 / AC-FUNC-006: a commit that renames a file does not abort the range scan.

    Regression for code_review's finding on `_diff_new_file_path`: `git
    diff-tree -r` is run without rename detection, so a rename is emitted
    as two file sections, a full deletion of the old path (`+++ /dev/null`)
    and a full addition of the new path. Before the fix, the deletion
    half's unparseable header raised `SecretScanError` and aborted the
    scan before the addition half -- which re-adds the credential under
    its new path and must be reported on its own -- was ever reached.
    """
    secrets = import_secrets()
    root = generated_root(tmp_path)
    init_repo(root)
    line = credential_line()

    commit_text(root, "README.md", "base\n", "base commit")
    base_commit = rev_parse(root, "HEAD")
    commit_text(root, "src/old_config.py", line, "add credential at old path")
    adding_commit = rev_parse(root, "HEAD")
    subprocess.run(
        ["git", "-C", str(root), "mv", "src/old_config.py", "src/new_config.py"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(root), "commit", "-m", "rename credential file"],
        check=True,
        capture_output=True,
    )
    rename_commit = rev_parse(root, "HEAD")

    report = secrets.scan_range(root, f"{base_commit}..{rename_commit}")

    assert report.commit_count == 2
    findings_by_commit = {item.commit: item for item in report.findings}
    assert len(report.findings) == 2
    assert findings_by_commit[adding_commit].path == "src/old_config.py"
    assert findings_by_commit[adding_commit].finding.line_number == 1
    assert findings_by_commit[rename_commit].path == "src/new_config.py"
    assert findings_by_commit[rename_commit].finding.line_number == 1


def test_scan_range_does_not_raise_on_a_commit_that_deletes_a_binary_classified_file(
    tmp_path: Path,
) -> None:
    """AC-FUNC-001 / AC-FUNC-006: deleting a binary-classified file does not abort the range scan.

    `git diff-tree -p` emits `Binary files a/<path> and /dev/null differ`
    for the delete side of a binary file's deletion -- the same "no
    post-image" shape `+++ /dev/null` already handles for a text file, now
    handled by the binary marker branch of `_parse_added_lines`: the
    section is left unresolved rather than read, since a deletion
    contributes no lines for either scanner to hide behind.
    """
    secrets = import_secrets()
    root = generated_root(tmp_path)
    init_repo(root)
    line = credential_line()

    commit_text(root, "README.md", "base\n", "base commit")
    base_commit = rev_parse(root, "HEAD")
    adding_commit = commit_bytes(
        root, "blob.bin", line.encode("utf-8") + b"\x00BINARY\n", "add binary credential"
    )
    subprocess.run(
        ["git", "-C", str(root), "rm", "-q", "blob.bin"], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(root), "commit", "-m", "delete binary credential file"],
        check=True,
        capture_output=True,
    )
    delete_commit = rev_parse(root, "HEAD")

    report = secrets.scan_range(root, f"{base_commit}..{delete_commit}")

    assert report.commit_count == 2
    assert len(report.findings) == 1
    finding_record = report.findings[0]
    assert finding_record.commit == adding_commit
    assert finding_record.path == "blob.bin"


def test_unescape_git_quoted_path_rejects_an_escape_token_git_never_emits() -> None:
    """AC-TEST-005: an escape token `git quote_c_style` never emits fails naming the header.

    `git`'s own `quote_c_style` only ever emits a doubled backslash or
    double quote, one of the seven single-letter C escapes in
    `_GIT_QUOTE_SIMPLE_ESCAPES`, or a three-digit octal byte value -- so an
    escape token outside that set is unreachable through any real diff and
    is proven directly against `_unescape_git_quoted_path` here instead.
    """
    secrets = import_secrets()
    header = '+++ "b/odd\\qpath.py"'

    with pytest.raises(secrets.SecretScanError, match="escape sequence") as exc_info:
        secrets._unescape_git_quoted_path("b/odd\\qpath.py", commit="deadbeef", header=header)

    assert "ERROR:" in str(exc_info.value)
    assert "deadbeef" in str(exc_info.value)
    assert repr(header) in str(exc_info.value)


def test_unescape_git_quoted_path_rejects_bytes_that_are_not_valid_utf8() -> None:
    """AC-TEST-005: an escaped byte sequence that is not valid UTF-8 fails naming the header.

    A three-digit octal escape decodes to a raw byte before the final
    UTF-8 decode in `_unescape_git_quoted_path`; `git` only ever emits
    octal escapes for a path it read as valid UTF-8, so a lone byte that
    cannot decode is unreachable through any real diff and is proven
    directly here instead.
    """
    secrets = import_secrets()
    header = '+++ "b/bad\\377.py"'

    with pytest.raises(secrets.SecretScanError, match="not valid UTF-8") as exc_info:
        secrets._unescape_git_quoted_path("b/bad\\377.py", commit="deadbeef", header=header)

    assert "ERROR:" in str(exc_info.value)
    assert "deadbeef" in str(exc_info.value)


def test_diff_new_file_path_rejects_a_header_matching_neither_form() -> None:
    """AC-TEST-005: a `+++ ` header matching neither known form fails naming it.

    `git diff-tree` only ever emits the plain `b/<path>` form, the
    C-quoted form, or the `+++ /dev/null` deletion header
    `_parse_added_lines` already special-cases before calling this
    function, so `_diff_new_file_path`'s final raise is unreachable
    through any real diff and is proven directly here instead.
    """
    secrets = import_secrets()
    line = "+++ neither form matches this"

    with pytest.raises(secrets.SecretScanError, match="could not interpret") as exc_info:
        secrets._diff_new_file_path(line, commit="deadbeef")

    assert "ERROR:" in str(exc_info.value)
    assert "deadbeef" in str(exc_info.value)
    assert line in str(exc_info.value)


def test_parse_added_lines_rejects_a_binary_marker_matching_neither_form(tmp_path: Path) -> None:
    """AC-TEST-005: a `Binary files ... differ` line matching neither known form fails naming it.

    `git diff-tree` only ever names the post-image side of this marker
    `/dev/null`, the plain `b/<path>` form or the C-quoted `"b/<path>"`
    form, so a marker naming anything else on that side is unreachable
    through any real diff and is proven directly here instead.
    """
    secrets = import_secrets()
    diff_text = "diff --git a/blob.bin c/blob.bin\nBinary files a/blob.bin and c/blob.bin differ\n"

    with pytest.raises(secrets.SecretScanError, match="could not interpret") as exc_info:
        secrets._parse_added_lines(diff_text, commit="deadbeef", root=tmp_path)

    assert "ERROR:" in str(exc_info.value)
    assert "deadbeef" in str(exc_info.value)
