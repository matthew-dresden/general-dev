"""Tests for devcontainer_config.secrets: the Scanner suite (spec Section 10.2).

The `devcontainer_config` import is deferred into function bodies (via
`_import_secrets`) instead of done once at module scope, and the case table
at `tests/data/secret_scanner_cases.json` is loaded with plain `json.loads`
that never imports `devcontainer_config`. Both choices exist for the same
reason documented in `tests/test_repo.py`: the TDD RED gate stashes this
unit's production-source files and re-runs a single named test node, and a
module-level `from devcontainer_config.secrets import ...` would fail
collection for the whole file (pytest exit 2, no test outcome recorded)
instead of failing the one test for the real reason.

Positive samples are never stored pre-assembled in the case table. Each case
holds a `positive_prefix` (a literal string, or `null` meaning "pull every
candidate from `devcontainer_config.secrets.SAMPLE_PREFIXES`") and a
`positive_suffix`; only `test_positive_sample_yields_one_finding` (and the
tests that reuse `_POSITIVE_CASES`) concatenate them, at run time, in memory.
Storing the concatenated result as a single JSON literal would itself be a
credential-shaped string this very scanner would flag the next time it ran
over this repository, which is exactly the failure mode Approach step 1 of
this work unit calls out.
"""

from __future__ import annotations

import ast
import importlib
import json
import re
import socket
import subprocess
import uuid
from pathlib import Path
from types import ModuleType

import pytest
from devcontainer_config.repo import find_root
from gitfixtures import commit_text, generated_root, init_repo, stage_bytes, stage_text


def _import_secrets() -> ModuleType:
    """Import devcontainer_config.secrets from inside a function body.

    See the module docstring for why this is not a module-level import.
    """
    return importlib.import_module("devcontainer_config.secrets")


def _real_shell_env_example_text() -> str:
    """This repository's own `shell.env.example`, read fresh for every call.

    AC-TEST-001's fixture uses this instead of a synthetic all-comment/
    all-empty-assignment template (E2-F1-S1-T4 review round 4, test_review
    FAIL): a synthetic template built from only placeholder-shaped lines
    cannot observe a false positive on a concrete-default line such as
    `export DEFAULT_GIT_BRANCH='main'` or `unset GIT_EDITOR`, which the real
    template ships and which the reported defect (AC-CODE-001, "87
    false-positive findings") was measured against. `find_root` is imported
    at module scope, and this function reads the file fresh rather than
    caching it, for the same reason `tests/test_shellrc.py` documents for its
    own `_shell_env_example_text` helper: `repo.py` is not in this task's
    Changes Manifest, so the TDD RED gate's stash of `secrets.py` never
    touches this import path.
    """
    return (find_root(Path(__file__).resolve().parent) / "shell.env.example").read_text(
        encoding="utf-8"
    )


class _CallerDefinedRunGitError(Exception):
    """A caller-defined exception type deliberately not a `SecretScanError` subclass.

    `run_git` and `empty_tree_object_id` (E2-F2-S1-T3) accept a
    keyword-only `error_type` so a caller outside `devcontainer_config.secrets`
    -- the git-hooks installer, for one -- gets its own exception type back
    from a git failure rather than `SecretScanError`. This type stands in
    for that caller here, and is deliberately unrelated to `SecretScanError`
    so a test that asserts the raised type is this one would fail if the
    production code silently still raised `SecretScanError` instead.
    """


def _monkeypatch_subprocess_run_raises(
    monkeypatch: pytest.MonkeyPatch, raised: BaseException
) -> None:
    def _raise(*_args: object, **_kwargs: object) -> None:
        raise raised

    monkeypatch.setattr(subprocess, "run", _raise)


def _secrets_module_path() -> Path:
    return (
        Path(__file__).resolve().parents[1]
        / ".claude"
        / "plugins"
        / "devcontainer"
        / "scripts"
        / "devcontainer_config"
        / "secrets.py"
    )


def _load_cases() -> tuple[dict[str, object], ...]:
    path = Path(__file__).resolve().parent / "data" / "secret_scanner_cases.json"
    return tuple(json.loads(path.read_text(encoding="utf-8")))


_CASES: tuple[dict[str, object], ...] = _load_cases()


def _sample_prefixes_for(detector_id: str) -> tuple[str, ...]:
    """Registry-declared prefixes for `detector_id`, or a placeholder tuple.

    Called at collection time while building `_POSITIVE_CASES`. Falls back
    to a single placeholder when `devcontainer_config` is not yet importable
    (the TDD RED gate), so collection succeeds either way; each parametrized
    case then fails inside the test body for the real reason instead of
    failing collection for the whole file.
    """
    try:
        secrets = _import_secrets()
    except ModuleNotFoundError as exc:
        if exc.name not in ("devcontainer_config", "devcontainer_config.secrets"):
            raise
        return ("<devcontainer_config unavailable>",)
    return secrets.SAMPLE_PREFIXES.get(detector_id, (None,))


def _positive_cases() -> tuple[tuple[dict[str, object], str], ...]:
    pairs: list[tuple[dict[str, object], str]] = []
    for case in _CASES:
        literal_prefix = case["positive_prefix"]
        prefixes = (
            _sample_prefixes_for(str(case["detector_id"]))
            if literal_prefix is None
            else (str(literal_prefix),)
        )
        pairs.extend((case, prefix) for prefix in prefixes)
    return tuple(pairs)


_POSITIVE_CASES: tuple[tuple[dict[str, object], str], ...] = _positive_cases()
_POSITIVE_CASE_IDS: list[str] = [
    f"{case['detector_id']}:{prefix}" for case, prefix in _POSITIVE_CASES
]
_NEGATIVE_CASE_IDS: list[str] = [str(case["detector_id"]) for case in _CASES]


def _composed_sample(case: dict[str, object], prefix: str) -> str:
    return f"{prefix}{case['positive_suffix']}"


def _sample_sso_portal_url() -> str:
    """A sso-portal-url-shaped sample, composed at run time (never stored assembled)."""
    return f"https://{uuid.uuid4().hex}.awsapps.com/start"


def _sample_account_id() -> str:
    """A twelve-digit account-id-shaped sample, composed at run time (never stored assembled)."""
    return str(uuid.uuid4().int)[:12]


@pytest.mark.parametrize("case,prefix", _POSITIVE_CASES, ids=_POSITIVE_CASE_IDS)
def test_positive_sample_yields_one_finding(case: dict[str, object], prefix: str) -> None:
    """AC-TEST-001: every positive sample yields exactly one correctly identified finding."""
    secrets = _import_secrets()
    sample = _composed_sample(case, prefix)
    sources = secrets.ScanSources(shell_env_lines=(), catalog_secret_names=())

    findings = secrets.scan_lines([(1, sample)], sources)

    assert len(findings) == 1
    assert findings[0].detector_id == case["detector_id"]
    assert findings[0].line_number == 1
    assert findings[0].printable == case["printable"]


@pytest.mark.parametrize("case", _CASES, ids=_NEGATIVE_CASE_IDS)
def test_negative_lookalike_yields_no_findings(case: dict[str, object]) -> None:
    """AC-TEST-002: a look-alike that must not match yields zero findings."""
    secrets = _import_secrets()
    sources = secrets.ScanSources(shell_env_lines=(), catalog_secret_names=())

    findings = secrets.scan_lines([(1, str(case["negative_sample"]))], sources)

    assert findings == ()


@pytest.mark.parametrize("case", _CASES, ids=_NEGATIVE_CASE_IDS)
def test_detector_description_matches_case_table(case: dict[str, object]) -> None:
    """AC-DOC-002: a detector's description is the exact text the case table expects."""
    secrets = _import_secrets()
    detector = secrets.DETECTORS_BY_ID[str(case["detector_id"])]

    assert detector.description == case["description"]
    assert detector.printable == case["printable"]


@pytest.mark.parametrize("case,prefix", _POSITIVE_CASES, ids=_POSITIVE_CASE_IDS)
def test_render_finding_never_contains_the_full_composed_sample(
    case: dict[str, object], prefix: str
) -> None:
    """AC-TEST-006: a redacted render never contains the full composed sample."""
    secrets = _import_secrets()
    sample = _composed_sample(case, prefix)
    sources = secrets.ScanSources(shell_env_lines=(), catalog_secret_names=())
    finding = secrets.scan_lines([(1, sample)], sources)[0]
    detector = secrets.DETECTORS_BY_ID[finding.detector_id]

    rendered = secrets.render_finding(finding, detector)

    if case["printable"]:
        assert rendered == finding.matched_text
    else:
        assert sample not in rendered
        assert finding.matched_text not in rendered


def test_shell_env_line_detector_flags_matching_line() -> None:
    """AC-TEST-004: a line present in ScanSources.shell_env_lines is reported."""
    secrets = _import_secrets()
    shell_env_line = f"SOME_VAR=example-value-{uuid.uuid4().hex}"
    sources = secrets.ScanSources(shell_env_lines=(shell_env_line,), catalog_secret_names=())

    findings = secrets.scan_lines([(3, shell_env_line)], sources)

    assert len(findings) == 1
    assert findings[0].detector_id == "shell-env-line"
    assert findings[0].line_number == 3
    assert findings[0].matched_text == shell_env_line


def test_shell_env_line_detector_ignores_unrelated_line() -> None:
    """AC-TEST-004: an unrelated line yields no shell-env-line finding."""
    secrets = _import_secrets()
    sources = secrets.ScanSources(
        shell_env_lines=(f"SOME_VAR=example-value-{uuid.uuid4().hex}",),
        catalog_secret_names=(),
    )

    findings = secrets.scan_lines([(1, "an unrelated line of code")], sources)

    assert findings == ()


def test_catalog_secret_name_detector_flags_line_containing_name() -> None:
    """AC-TEST-004: a line containing a name in ScanSources.catalog_secret_names is reported."""
    secrets = _import_secrets()
    name = f"CATALOG_SECRET_{uuid.uuid4().hex}"
    sources = secrets.ScanSources(shell_env_lines=(), catalog_secret_names=(name,))
    line = f"token = lookup({name})"

    findings = secrets.scan_lines([(7, line)], sources)

    assert len(findings) == 1
    assert findings[0].detector_id == "catalog-secret-name"
    assert findings[0].matched_text == name
    assert findings[0].line_number == 7


def test_catalog_secret_name_detector_ignores_line_without_any_name() -> None:
    """AC-TEST-004: a line naming no catalog secret yields no finding."""
    secrets = _import_secrets()
    sources = secrets.ScanSources(
        shell_env_lines=(), catalog_secret_names=(f"CATALOG_SECRET_{uuid.uuid4().hex}",)
    )

    findings = secrets.scan_lines([(1, "an unrelated line of code")], sources)

    assert findings == ()


def test_build_registry_rejects_duplicated_identifier() -> None:
    """Error Handling Contract: a duplicated identifier names both descriptions."""
    secrets = _import_secrets()
    first = secrets.PatternDetector(
        identifier="dup-id",
        description="First description",
        printable=False,
        safe_prefix_len=4,
        pattern=re.compile("first-marker"),
    )
    second = secrets.PatternDetector(
        identifier="dup-id",
        description="Second description",
        printable=False,
        safe_prefix_len=4,
        pattern=re.compile("second-marker"),
    )

    with pytest.raises(secrets.SecretScanError, match="dup-id") as exc_info:
        secrets.build_registry((first, second))

    assert "First description" in str(exc_info.value)
    assert "Second description" in str(exc_info.value)


def test_scan_sources_rejects_blank_shell_env_line() -> None:
    """Error Handling Contract: a blank shell_env_lines entry names the field and index."""
    secrets = _import_secrets()

    with pytest.raises(secrets.SecretScanError, match="shell_env_lines") as exc_info:
        secrets.ScanSources(shell_env_lines=("VALID=1", "   "), catalog_secret_names=())

    assert "index 1" in str(exc_info.value).lower()


def test_scan_lines_rejects_unknown_detector_filter() -> None:
    """Error Handling Contract: an unknown detector_ids entry lists the known identifiers."""
    secrets = _import_secrets()
    sources = secrets.ScanSources(shell_env_lines=(), catalog_secret_names=())

    with pytest.raises(secrets.SecretScanError, match="unknown-detector-id") as exc_info:
        secrets.scan_lines([(1, "irrelevant")], sources, detector_ids=("unknown-detector-id",))

    for identifier in secrets.DETECTORS_BY_ID:
        assert identifier in str(exc_info.value)


def test_render_finding_rejects_detector_mismatch() -> None:
    """render_finding refuses to apply one detector's redaction to another's finding."""
    secrets = _import_secrets()
    finding = secrets.Finding(
        detector_id="aws-access-key-id", line_number=1, matched_text="X" * 20, printable=False
    )
    wrong_detector = secrets.DETECTORS_BY_ID["github-token"]

    with pytest.raises(secrets.SecretScanError, match="mismatch"):
        secrets.render_finding(finding, wrong_detector)


def test_scan_sources_requires_both_fields_explicitly() -> None:
    """AC-FUNC-004: omitting catalog_secret_names is a TypeError, not a silent default."""
    secrets = _import_secrets()
    kwargs: dict[str, tuple[str, ...]] = {"shell_env_lines": ()}

    with pytest.raises(TypeError, match="catalog_secret_names"):
        secrets.ScanSources(**kwargs)


def test_scan_lines_detector_ids_filter_restricts_detectors() -> None:
    """A detector_ids filter limits scanning to the named detectors only."""
    secrets = _import_secrets()
    sources = secrets.ScanSources(shell_env_lines=(), catalog_secret_names=())
    lines = [(1, _sample_sso_portal_url()), (2, _sample_account_id())]

    findings = secrets.scan_lines(lines, sources, detector_ids=("sso-portal-url",))

    assert len(findings) == 1
    assert findings[0].detector_id == "sso-portal-url"


def test_scan_lines_touches_no_file_subprocess_or_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-FUNC-003: scan_lines opens no file, spawns no subprocess, reaches no network."""
    secrets = _import_secrets()

    def _forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("scan_lines must not perform this operation")

    monkeypatch.setattr("builtins.open", _forbidden)
    monkeypatch.setattr(subprocess, "run", _forbidden)
    monkeypatch.setattr(socket, "socket", _forbidden)

    sources = secrets.ScanSources(shell_env_lines=(), catalog_secret_names=())
    findings = secrets.scan_lines([(1, "AKIA" + "B" * 16)], sources)

    assert len(findings) == 1


def _own_source_lines() -> list[tuple[int, str]]:
    """This test file's own source text, numbered like `scan_lines` expects."""
    source = Path(__file__).resolve().read_text(encoding="utf-8")
    return list(enumerate(source.splitlines(), start=1))


def test_own_source_yields_no_findings_from_the_scanner_it_tests() -> None:
    """This file's own committed bytes must not trip the very scanner it tests.

    Every positive sample this file needs is composed at run time (see
    `_composed_sample`, `_sample_sso_portal_url`, `_sample_account_id`) so no
    physical line here ever matches a detector pattern.
    """
    secrets = _import_secrets()
    sources = secrets.ScanSources(shell_env_lines=(), catalog_secret_names=())

    findings = secrets.scan_lines(_own_source_lines(), sources)

    assert findings == ()


def _imported_top_level_modules(tree: ast.Module) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module.split(".")[0])
    return modules


def _defined_or_referenced_identifiers(tree: ast.Module) -> set[str]:
    """Every name a bypass mechanism would need: a parameter, an attribute, or a constant.

    Walking the AST instead of grepping raw text means this only inspects
    actual code identifiers, never docstring prose that legitimately
    describes the absence of these mechanisms in English.
    """
    identifiers: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            identifiers.add(node.id)
        elif isinstance(node, ast.arg):
            identifiers.add(node.arg)
        elif isinstance(node, ast.Attribute):
            identifiers.add(node.attr)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            identifiers.add(node.name)
    return identifiers


def test_module_source_has_no_allowlist_or_bypass_mechanism() -> None:
    """AC-FUNC-007: no allowlist, inline marker or environment variable disables a detector."""
    source = _secrets_module_path().read_text(encoding="utf-8")
    tree = ast.parse(source)

    assert "os" not in _imported_top_level_modules(tree)

    forbidden_substrings = ("allowlist", "allow_list", "ignorelist", "ignore_list", "suppress")
    identifiers = _defined_or_referenced_identifiers(tree)
    offending = {
        identifier
        for identifier in identifiers
        if any(bad in identifier.lower() for bad in forbidden_substrings)
    }
    assert offending == set()


def test_scan_lines_end_to_end_multiple_detectors_and_lines() -> None:
    """AC-CYCLE-001 / AC-FINAL-010: several lines and detector kinds, scanned in one call."""
    secrets = _import_secrets()
    catalog_name = f"CATALOG_SECRET_{uuid.uuid4().hex}"
    shell_env_line = f"SOME_VAR=example-{uuid.uuid4().hex}"
    sources = secrets.ScanSources(
        shell_env_lines=(shell_env_line,), catalog_secret_names=(catalog_name,)
    )
    lines = [
        (1, _sample_sso_portal_url()),
        (2, "nothing interesting here"),
        (3, shell_env_line),
        (4, f"value = lookup({catalog_name})"),
    ]

    findings = secrets.scan_lines(lines, sources)

    by_line = {finding.line_number: finding.detector_id for finding in findings}
    assert by_line[1] == "sso-portal-url"
    assert 2 not in by_line
    assert by_line[3] == "shell-env-line"
    assert by_line[4] == "catalog-secret-name"
    assert len(findings) == 3


@pytest.mark.parametrize(
    "raised",
    [
        pytest.param(FileNotFoundError(), id="git-not-installed"),
        pytest.param(
            subprocess.CalledProcessError(1, ["git"], output=b"", stderr=b"synthetic stderr"),
            id="git-nonzero-exit",
        ),
    ],
)
def test_run_git_raises_caller_supplied_error_type(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, raised: BaseException
) -> None:
    """AC-SHARED-001 / AC-SHARED-004: a non-SecretScanError error_type is the type raised."""
    secrets = _import_secrets()
    _monkeypatch_subprocess_run_raises(monkeypatch, raised)
    not_installed_message = f"ERROR: synthetic not-installed message {uuid.uuid4().hex}"

    def _failure_message(stderr: str) -> str:
        return f"ERROR: synthetic failure message for stderr={stderr!r}"

    with pytest.raises(_CallerDefinedRunGitError) as exc_info:
        secrets.run_git(
            ["status"],
            root=tmp_path,
            not_installed_message=lambda: not_installed_message,
            failure_message=_failure_message,
            error_type=_CallerDefinedRunGitError,
        )

    expected = (
        not_installed_message
        if isinstance(raised, FileNotFoundError)
        else _failure_message("synthetic stderr")
    )
    assert str(exc_info.value) == expected


def test_run_git_default_error_type_is_secret_scan_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AC-SHARED-005: omitting error_type still raises SecretScanError."""
    secrets = _import_secrets()
    _monkeypatch_subprocess_run_raises(monkeypatch, FileNotFoundError())

    with pytest.raises(secrets.SecretScanError):
        secrets.run_git(
            ["status"],
            root=tmp_path,
            not_installed_message=lambda: "ERROR: git is not installed",
            failure_message=lambda stderr: f"ERROR: {stderr}",
        )


def test_empty_tree_object_id_forwards_caller_supplied_error_type(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AC-SHARED-002: empty_tree_object_id forwards error_type to run_git's failure path."""
    secrets = _import_secrets()
    _monkeypatch_subprocess_run_raises(monkeypatch, FileNotFoundError())

    with pytest.raises(_CallerDefinedRunGitError):
        secrets.empty_tree_object_id(tmp_path, error_type=_CallerDefinedRunGitError)


def test_empty_tree_object_id_default_error_type_is_secret_scan_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AC-SHARED-005: omitting error_type on empty_tree_object_id still raises SecretScanError."""
    secrets = _import_secrets()
    _monkeypatch_subprocess_run_raises(monkeypatch, FileNotFoundError())

    with pytest.raises(secrets.SecretScanError):
        secrets.empty_tree_object_id(tmp_path)


def test_empty_tree_object_id_matches_independent_git_hash_object(tmp_path: Path) -> None:
    """AC-SHARED-006: the derived id equals an independently invoked git hash-object."""
    secrets = _import_secrets()
    root = generated_root(tmp_path)
    init_repo(root)

    derived = secrets.empty_tree_object_id(root)

    independent = (
        subprocess.run(
            ["git", "-C", str(root), "hash-object", "-t", "tree", "--stdin"],
            input=b"",
            capture_output=True,
            check=True,
        )
        .stdout.decode("utf-8")
        .strip()
    )
    assert derived == independent


def test_run_staged_scan_reports_clean_when_shell_env_example_edit_is_staged(
    tmp_path: Path,
) -> None:
    """AC-TEST-001: staging a shell.env.example edit, with shell.env seeded from the
    template's own content (the documented `cp shell.env.example shell.env`
    workflow), reports no findings for every line the edit does not touch.

    Uses this repository's own `shell.env.example` (`_real_shell_env_example_text`)
    instead of a synthetic all-comment/all-empty-assignment fixture (E2-F1-S1-T4
    review round 4, test_review FAIL): a synthetic fixture built only from
    placeholder-shaped lines cannot observe a false positive on a concrete-default
    line such as `export DEFAULT_GIT_BRANCH='main'` or `unset GIT_EDITOR`, which the
    real template ships and which the reported defect (AC-CODE-001, "87
    false-positive findings") was measured against.

    Also pins `shell_env_excluded_line_count` and the rendered
    "(N template lines excluded)" text (E2-F1-S1-T4 security review round 4 LOW,
    test_review FAIL): every one of the template's non-blank lines is untouched by
    the trivial staged edit below, so all of them, and none besides, must be
    excluded."""
    secrets = _import_secrets()
    root = generated_root(tmp_path)
    init_repo(root)
    template = _real_shell_env_example_text()
    template_line_count = len([line for line in template.splitlines() if line.strip()])
    commit_text(root, "shell.env.example", template, "add shell.env.example")
    (root / "shell.env").write_text(template, encoding="utf-8")
    stage_text(root, "shell.env.example", template + "# a trivial, unrelated template edit\n")

    report = secrets.run_staged_scan(root)

    assert report.findings == ()
    assert report.shell_env_excluded_line_count == template_line_count
    assert f"({template_line_count} template lines excluded)" in secrets.render_lint_report(report)


def test_run_staged_scan_still_denies_a_shell_env_value_absent_from_the_template(
    tmp_path: Path,
) -> None:
    """AC-TEST-001 negative case: a value a developer actually typed into shell.env,
    that does not trace back to a line shared between shell.env.example's index and
    HEAD versions, is still caught when that same value leaks into staged content
    elsewhere."""
    secrets = _import_secrets()
    root = generated_root(tmp_path)
    init_repo(root)
    commit_text(root, "shell.env.example", "AWS_PROFILE=\n", "add shell.env.example")
    filled_in = f"AWS_PROFILE=prod-{uuid.uuid4().hex}"
    (root / "shell.env").write_text(filled_in + "\n", encoding="utf-8")
    stage_text(root, "src/leaked.py", filled_in + "\n")

    report = secrets.run_staged_scan(root)

    assert len(report.findings) == 1
    assert report.findings[0].path == "src/leaked.py"
    assert report.findings[0].finding.detector_id == "shell-env-line"


def test_shell_env_example_lines_raises_when_git_is_not_installed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`shell_env_example_lines` reads tracked content through git; a missing git
    binary must surface as SecretScanError, never as an empty (zero-exclusion)
    result standing in for it."""
    secrets = _import_secrets()
    _monkeypatch_subprocess_run_raises(monkeypatch, FileNotFoundError())

    with pytest.raises(secrets.SecretScanError, match="git is not installed"):
        secrets.shell_env_example_lines(tmp_path)


def test_shell_env_example_lines_raises_on_non_utf8_staged_content(tmp_path: Path) -> None:
    """A staged `shell.env.example` blob that is not valid UTF-8 fails the scan instead
    of silently narrowing the exclusion set to zero lines."""
    secrets = _import_secrets()
    root = generated_root(tmp_path)
    init_repo(root)
    stage_bytes(root, "shell.env.example", b"\xff\xfe\x00not-utf8")

    with pytest.raises(secrets.SecretScanError, match="not valid UTF-8"):
        secrets.shell_env_example_lines(root)


def test_shell_env_example_lines_raises_on_a_genuine_git_failure_not_a_missing_path(
    tmp_path: Path,
) -> None:
    """Review round 2 regression (E2-F1-S1-T4): a genuine git failure -- `root` is not a
    git work tree at all -- must not be conflated with "shell.env.example is tracked
    nowhere". The latter is a legitimate empty exclusion set (a fresh repo, or one that
    never added shell.env.example); the former is a condition this scanner's own
    docstring says must never be silently swallowed as an empty result standing in for
    an error."""
    secrets = _import_secrets()
    root = generated_root(tmp_path)
    # Deliberately never init_repo(root): there is no `.git` here at all, so every git
    # invocation against `root` fails the same way `staged_paths` fails against a
    # non-work-tree root, not the way a legitimately untracked path fails.

    with pytest.raises(secrets.SecretScanError, match="cannot determine whether"):
        secrets.shell_env_example_lines(root)


def test_committed_text_raises_when_git_show_fails_after_rev_parse_confirms_existence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A TOCTOU race between the `rev-parse --verify` existence probe and the `git
    show` read -- the object existed when probed, but `git show` still fails -- must
    raise `SecretScanError`, never fall through as though the path were untracked."""
    secrets = _import_secrets()
    root = generated_root(tmp_path)
    init_repo(root)
    commit_text(root, "shell.env.example", "AWS_PROFILE=\n", "add shell.env.example")
    real_run = subprocess.run

    def _fake_run(
        cmd: list[str],
        *,
        input: bytes | None = None,
        capture_output: bool = True,
        check: bool = True,
    ) -> subprocess.CompletedProcess[bytes]:
        if "show" in cmd:
            raise subprocess.CalledProcessError(
                128, cmd, output=b"", stderr=b"synthetic show failure"
            )
        return real_run(cmd, input=input, capture_output=capture_output, check=check)

    monkeypatch.setattr(subprocess, "run", _fake_run)

    with pytest.raises(secrets.SecretScanError, match="cannot read tracked content"):
        secrets.shell_env_example_lines(root)


def test_run_staged_scan_ignores_a_worktree_only_shell_env_example_edit(
    tmp_path: Path,
) -> None:
    """Review-round-1 regression (E2-F1-S1-T4): the exclusion set built by
    shell_env_example_lines must trace back to tracked (index or HEAD) content
    only. A line pasted into the developer's uncommitted, unstaged working copy of
    shell.env.example has no footprint in the commit this scan is about to allow,
    so it must not widen the exclusion set and silently blind the shell-env-line
    detector for the same value leaking into staged content elsewhere."""
    secrets = _import_secrets()
    root = generated_root(tmp_path)
    init_repo(root)
    commit_text(root, "shell.env.example", "AWS_PROFILE=\n", "add shell.env.example")
    leaked = f"MY_API_KEY=super-real-value-{uuid.uuid4().hex}"
    (root / "shell.env").write_text(f"AWS_PROFILE=\n{leaked}\n", encoding="utf-8")
    # Worktree-only edit: written directly, never `git add`-ed or committed.
    (root / "shell.env.example").write_text(f"AWS_PROFILE=\n{leaked}\n", encoding="utf-8")
    stage_text(root, "src/leaked.py", leaked + "\n")

    report = secrets.run_staged_scan(root)

    assert len(report.findings) == 1
    assert report.findings[0].path == "src/leaked.py"
    assert report.findings[0].finding.detector_id == "shell-env-line"


def test_run_staged_scan_still_flags_a_leak_staged_into_shell_env_example_itself(
    tmp_path: Path,
) -> None:
    """HIGH security regression (E2-F1-S1-T4 security review round 4): the previous
    test proves a *worktree-only* template edit cannot widen the exclusion set, but
    that test passes even when the exclusion set is built from every tracked
    shell.env.example line, because the edit is never staged there. This test
    poisons the template the same way but `git add`-s it, so the poisoned line IS
    tracked (index-resolved) content -- the scenario the worktree-only test cannot
    reach. A developer copies their filled-in shell.env over shell.env.example and
    stages it, and the same real value also leaks into src/leaked.py; both staged
    occurrences of that value must still be reported, because the poisoned line has
    no counterpart in HEAD (it was never committed before this staged edit), so the
    HEAD-and-index intersection this unit's fix builds the exclusion set from can
    never contain it, regardless of what it looks like."""
    secrets = _import_secrets()
    root = generated_root(tmp_path)
    init_repo(root)
    commit_text(root, "shell.env.example", "AWS_PROFILE=\n", "add shell.env.example")
    leaked = f"DB_PASSWORD=really-secret-{uuid.uuid4().hex}"
    (root / "shell.env").write_text(f"AWS_PROFILE=\n{leaked}\n", encoding="utf-8")
    # Poisoned template, staged (not worktree-only): the developer's shell.env
    # content copied verbatim over shell.env.example and `git add`-ed.
    stage_text(root, "shell.env.example", f"AWS_PROFILE=\n{leaked}\n")
    stage_text(root, "src/leaked.py", leaked + "\n")

    report = secrets.run_staged_scan(root)

    assert {(finding.path, finding.finding.detector_id) for finding in report.findings} == {
        ("shell.env.example", "shell-env-line"),
        ("src/leaked.py", "shell-env-line"),
    }


def test_run_staged_scan_still_flags_a_leak_staged_as_a_comment_into_shell_env_example(
    tmp_path: Path,
) -> None:
    """Finding B (E2-F1-S1-T4 review round 4, code_review HIGH): the placeholder-shape
    design's comment branch treated ANY line starting with `#` as excludable, so a
    real, filled-in value staged into shell.env.example as a commented-out
    assignment -- a shape this repository's own template already ships (`#
    export REMOTE_INSTANCE_ID=...`) -- silently blinded the shell-env-line detector
    for that same value leaking anywhere else in the same commit. The
    HEAD-and-index intersection rule closes this regardless of shape: the poisoned
    line was never committed before this staged edit, so it can never appear in
    both HEAD and the index, comment or not."""
    secrets = _import_secrets()
    root = generated_root(tmp_path)
    init_repo(root)
    commit_text(root, "shell.env.example", "AWS_PROFILE=\n", "add shell.env.example")
    leaked_comment = f"# export DB_PASSWORD='really-secret-{uuid.uuid4().hex}'"
    (root / "shell.env").write_text(f"AWS_PROFILE=\n{leaked_comment}\n", encoding="utf-8")
    # Poisoned template, staged as a comment (not an assignment): the developer's
    # shell.env content copied verbatim, `#`-prefix included, and `git add`-ed.
    stage_text(root, "shell.env.example", f"AWS_PROFILE=\n{leaked_comment}\n")
    stage_text(root, "src/leaked.py", leaked_comment + "\n")

    report = secrets.run_staged_scan(root)

    assert {(finding.path, finding.finding.detector_id) for finding in report.findings} == {
        ("shell.env.example", "shell-env-line"),
        ("src/leaked.py", "shell-env-line"),
    }
