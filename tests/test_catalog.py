"""Tests for devcontainer_config.catalog: the Parameter Store client (spec Section 5.3, 5.4).

The `devcontainer_config` import is deferred into function bodies (via
`_import_catalog`) instead of done once at module scope, for the same reason
documented in `tests/test_repo.py`: the TDD RED gate stashes this unit's
production-source files and re-runs a single named test node, and a
module-level `from devcontainer_config.catalog import ...` would fail
COLLECTION for the whole file (pytest exit 2, no test outcome recorded)
instead of failing the one test for the real reason.

`_FakeRunner` stands in for the injected subprocess runner: it never spawns a
process, records every `argv`/`stdin` pair it is handed, and answers from a
queue the test fills in beforehand. `_RaisingRunner` stands in for the one
case a real runner can never return from normally: the `aws` binary is not on
PATH, which `subprocess.run` reports by raising `FileNotFoundError`, not by
returning a non-zero exit code.

No seeded value in this file is a real credential; every one is a
deterministically generated placeholder built from `uuid.uuid4()` at test
time, so nothing here is itself a secret this repository's own scanner
(`devcontainer_config.secrets`) would need to flag.
"""

from __future__ import annotations

import ast
import importlib
import json
import subprocess
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path
from types import ModuleType

import pytest


def _import_catalog() -> ModuleType:
    """Import devcontainer_config.catalog from inside a function body.

    See the module docstring for why this is not a module-level import.
    """
    return importlib.import_module("devcontainer_config.catalog")


def _catalog_module_path() -> Path:
    return (
        Path(__file__).resolve().parents[1]
        / ".claude"
        / "plugins"
        / "devcontainer"
        / "scripts"
        / "devcontainer_config"
        / "catalog.py"
    )


def _seeded_value() -> str:
    """A generated placeholder value, unique per call, never a real credential."""
    return f"seeded-value-{uuid.uuid4().hex}"


def _ok(payload: object) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=json.dumps(payload), stderr="")


def _err(stderr: str, returncode: int = 254) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout="", stderr=stderr)


# One name per broken environment-variable-identifier rule (spec Section
# 4.3), shared by every test that asserts the rule is rejected, so the four
# cases are declared once instead of copy-pasted per test.
INVALID_SECRET_NAMES = ["notion-token", "1TOKEN", "", "FOO/BAR"]
INVALID_SECRET_NAME_IDS = ["hyphen", "leading-digit", "empty", "path-separator"]


class _FakeRunner:
    """A Runner double: records every call, answers from a queue, spawns nothing."""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], str | None]] = []
        self._queue: list[subprocess.CompletedProcess[str]] = []

    def queue(self, result: subprocess.CompletedProcess[str]) -> None:
        self._queue.append(result)

    def __call__(self, argv: Sequence[str], stdin: str | None) -> subprocess.CompletedProcess[str]:
        self.calls.append((tuple(argv), stdin))
        if not self._queue:
            raise AssertionError("_FakeRunner invoked with no queued response")
        return self._queue.pop(0)


class _RaisingRunner:
    """A Runner double standing in for a host with no `aws` binary on PATH."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc
        self.calls: list[tuple[tuple[str, ...], str | None]] = []

    def __call__(self, argv: Sequence[str], stdin: str | None) -> subprocess.CompletedProcess[str]:
        self.calls.append((tuple(argv), stdin))
        raise self._exc


# ---------------------------------------------------------------------------
# RED, paths and names (Approach step 1)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("scope", "name", "expected"),
    [
        ("shared", "NOTION_TOKEN", "/devcontainer/shared/secrets/NOTION_TOKEN"),
        ("sandbox", "JENKINS_API_TOKEN", "/devcontainer/sandbox/secrets/JENKINS_API_TOKEN"),
    ],
)
def test_parameter_path_renders_scope_and_name(scope: str, name: str, expected: str) -> None:
    catalog = _import_catalog()
    assert catalog.parameter_path(scope, name) == expected


@pytest.mark.parametrize("name", INVALID_SECRET_NAMES, ids=INVALID_SECRET_NAME_IDS)
def test_parameter_path_rejects_invalid_names(name: str) -> None:
    catalog = _import_catalog()
    with pytest.raises(catalog.InvalidSecretNameError) as excinfo:
        catalog.parameter_path("shared", name)
    assert repr(name) in str(excinfo.value)


# ---------------------------------------------------------------------------
# RED, the four operations (Approach step 2)
# ---------------------------------------------------------------------------


def test_read_issues_get_parameter_with_decryption_and_returns_exact_value() -> None:
    catalog = _import_catalog()
    runner = _FakeRunner()
    value = f"{_seeded_value()}\n"
    runner.queue(
        _ok({"Parameter": {"Name": "/devcontainer/shared/secrets/NOTION_TOKEN", "Value": value}})
    )
    client = catalog.CatalogClient(runner)

    result = client.read("shared", "NOTION_TOKEN")

    assert result == value
    (argv, stdin) = runner.calls[0]
    assert argv[0] == catalog.AWS_EXECUTABLE
    assert "get-parameter" in argv
    assert "--with-decryption" in argv
    assert "--name" in argv
    assert argv[argv.index("--name") + 1] == "/devcontainer/shared/secrets/NOTION_TOKEN"
    assert stdin is None


def test_write_issues_put_parameter_with_value_only_on_stdin() -> None:
    catalog = _import_catalog()
    runner = _FakeRunner()
    runner.queue(_ok({"Version": 1, "Tier": "Standard"}))
    client = catalog.CatalogClient(runner)
    value = _seeded_value()

    client.write("shared", "NOTION_TOKEN", value)

    (argv, stdin) = runner.calls[0]
    assert "put-parameter" in argv
    assert value not in argv
    assert stdin is not None
    assert value in stdin
    document = json.loads(stdin)
    assert document["Type"] == "SecureString"
    assert document["Name"] == "/devcontainer/shared/secrets/NOTION_TOKEN"
    assert document["Value"] == value


def test_delete_issues_delete_parameter_for_exact_path() -> None:
    catalog = _import_catalog()
    runner = _FakeRunner()
    runner.queue(_ok({}))
    client = catalog.CatalogClient(runner)

    client.delete("sandbox", "JENKINS_API_TOKEN")

    (argv, stdin) = runner.calls[0]
    assert "delete-parameter" in argv
    assert argv[argv.index("--name") + 1] == "/devcontainer/sandbox/secrets/JENKINS_API_TOKEN"
    assert stdin is None


def test_region_is_appended_to_every_argv_when_configured() -> None:
    catalog = _import_catalog()
    runner = _FakeRunner()
    runner.queue(_ok({}))
    client = catalog.CatalogClient(runner, region="us-east-1")

    client.delete("shared", "NOTION_TOKEN")

    (argv, _stdin) = runner.calls[0]
    assert argv[-2:] == ("--region", "us-east-1")


def test_subprocess_runner_invokes_a_real_process_and_captures_output() -> None:
    catalog = _import_catalog()

    result = catalog.subprocess_runner(["echo", "hello"], None)

    assert result.returncode == 0
    assert result.stdout.strip() == "hello"


def test_list_secrets_issues_describe_parameters_with_recursive_path_filter() -> None:
    catalog = _import_catalog()
    runner = _FakeRunner()
    runner.queue(
        _ok(
            {
                "Parameters": [
                    {
                        "Name": "/devcontainer/shared/secrets/NOTION_TOKEN",
                        "LastModifiedDate": 1700000000.0,
                        "Description": json.dumps({"exported": True}),
                    },
                    {
                        "Name": "/devcontainer/shared/secrets/JENKINS_API_TOKEN",
                        "LastModifiedDate": 1700000100.0,
                        "Description": json.dumps({"exported": False}),
                    },
                ]
            }
        )
    )
    client = catalog.CatalogClient(runner)

    records = client.list_secrets("shared")

    (argv, stdin) = runner.calls[0]
    assert "describe-parameters" in argv
    assert "--parameter-filters" in argv
    filter_arg = argv[argv.index("--parameter-filters") + 1]
    assert "Path" in filter_arg
    assert "Recursive" in filter_arg
    assert "/devcontainer/shared/secrets" in filter_arg
    assert stdin is None

    assert [r.name for r in records] == ["NOTION_TOKEN", "JENKINS_API_TOKEN"]
    assert all(r.scope == "shared" for r in records)
    assert records[0].exported is True
    assert records[1].exported is False
    assert records[0].last_modified == "1700000000.0"


def test_list_secrets_response_carries_no_value_field() -> None:
    """AC-FUNC-005: even a response entry carrying a Value never reaches a SecretRecord.

    `describe-parameters` never actually returns a `Value` field, but this
    queues one anyway to prove the guarantee structurally: `SecretRecord`
    has no `value` attribute to put it in, and `list_secrets` only ever
    reads `Name`, `LastModifiedDate` and `Description` off an entry, so a
    foreign `Value` key is never consulted.
    """
    catalog = _import_catalog()
    seeded_value = _seeded_value()
    payload = {
        "Parameters": [
            {
                "Name": "/devcontainer/shared/secrets/NOTION_TOKEN",
                "LastModifiedDate": 1700000000.0,
                "Description": json.dumps({"exported": False}),
                "Value": seeded_value,
            }
        ]
    }
    runner = _FakeRunner()
    runner.queue(_ok(payload))
    client = catalog.CatalogClient(runner)

    records = client.list_secrets("shared")

    assert records[0].name == "NOTION_TOKEN"
    assert not hasattr(records[0], "value")
    assert seeded_value not in repr(records[0])
    (argv, _stdin) = runner.calls[0]
    assert "describe-parameters" in argv
    assert "get-parameters-by-path" not in argv


def test_list_secrets_follows_next_token_across_pages() -> None:
    """doc_review: 'every secret under scope' must hold for a scope with more than one page."""
    catalog = _import_catalog()
    runner = _FakeRunner()
    runner.queue(
        _ok(
            {
                "Parameters": [
                    {
                        "Name": "/devcontainer/shared/secrets/NOTION_TOKEN",
                        "LastModifiedDate": 1700000000.0,
                        "Description": json.dumps({"exported": True}),
                    }
                ],
                "NextToken": "page-2-token",
            }
        )
    )
    runner.queue(
        _ok(
            {
                "Parameters": [
                    {
                        "Name": "/devcontainer/shared/secrets/JENKINS_API_TOKEN",
                        "LastModifiedDate": 1700000100.0,
                        "Description": json.dumps({"exported": False}),
                    }
                ]
            }
        )
    )
    client = catalog.CatalogClient(runner)

    records = client.list_secrets("shared")

    assert [r.name for r in records] == ["NOTION_TOKEN", "JENKINS_API_TOKEN"]
    assert len(runner.calls) == 2
    (first_argv, _first_stdin) = runner.calls[0]
    assert "--next-token" not in first_argv
    (second_argv, _second_stdin) = runner.calls[1]
    assert "--next-token" in second_argv
    assert second_argv[second_argv.index("--next-token") + 1] == "page-2-token"


def test_list_secrets_raises_catalog_error_for_entry_with_no_description() -> None:
    """A foreign parameter (console-created, operator-created) has no Description at all."""
    catalog = _import_catalog()
    runner = _FakeRunner()
    runner.queue(
        _ok(
            {
                "Parameters": [
                    {
                        "Name": "/devcontainer/shared/secrets/FOREIGN_TOKEN",
                        "LastModifiedDate": 1700000000.0,
                    }
                ]
            }
        )
    )
    client = catalog.CatalogClient(runner)

    with pytest.raises(catalog.CatalogError) as excinfo:
        client.list_secrets("shared")

    assert "/devcontainer/shared/secrets/FOREIGN_TOKEN" in str(excinfo.value)


def test_list_secrets_raises_catalog_error_for_entry_with_non_json_description() -> None:
    """A Description this client did not write is not guaranteed to be its JSON document."""
    catalog = _import_catalog()
    runner = _FakeRunner()
    runner.queue(
        _ok(
            {
                "Parameters": [
                    {
                        "Name": "/devcontainer/shared/secrets/FOREIGN_TOKEN",
                        "LastModifiedDate": 1700000000.0,
                        "Description": "written by an operator, not this client",
                    }
                ]
            }
        )
    )
    client = catalog.CatalogClient(runner)

    with pytest.raises(catalog.CatalogError) as excinfo:
        client.list_secrets("shared")

    assert "/devcontainer/shared/secrets/FOREIGN_TOKEN" in str(excinfo.value)


def test_list_secrets_raises_catalog_error_for_entry_with_no_name() -> None:
    """A malformed store response entry never escapes as a bare KeyError."""
    catalog = _import_catalog()
    runner = _FakeRunner()
    runner.queue(_ok({"Parameters": [{"LastModifiedDate": 1700000000.0}]}))
    client = catalog.CatalogClient(runner)

    with pytest.raises(catalog.CatalogError) as excinfo:
        client.list_secrets("shared")

    assert "/devcontainer/shared/secrets" in str(excinfo.value)


def test_list_secrets_raises_catalog_error_for_entry_with_no_last_modified_date() -> None:
    catalog = _import_catalog()
    runner = _FakeRunner()
    runner.queue(
        _ok(
            {
                "Parameters": [
                    {
                        "Name": "/devcontainer/shared/secrets/NOTION_TOKEN",
                        "Description": json.dumps({"exported": True}),
                    }
                ]
            }
        )
    )
    client = catalog.CatalogClient(runner)

    with pytest.raises(catalog.CatalogError) as excinfo:
        client.list_secrets("shared")

    assert "/devcontainer/shared/secrets/NOTION_TOKEN" in str(excinfo.value)


def test_list_secrets_raises_catalog_error_for_description_missing_exported_key() -> None:
    catalog = _import_catalog()
    runner = _FakeRunner()
    runner.queue(
        _ok(
            {
                "Parameters": [
                    {
                        "Name": "/devcontainer/shared/secrets/NOTION_TOKEN",
                        "LastModifiedDate": 1700000000.0,
                        "Description": json.dumps({"created_by": "console"}),
                    }
                ]
            }
        )
    )
    client = catalog.CatalogClient(runner)

    with pytest.raises(catalog.CatalogError) as excinfo:
        client.list_secrets("shared")

    assert "/devcontainer/shared/secrets/NOTION_TOKEN" in str(excinfo.value)


# ---------------------------------------------------------------------------
# RED, error paths (Approach step 3)
# ---------------------------------------------------------------------------


def test_read_raises_unavailable_when_aws_is_not_on_path() -> None:
    catalog = _import_catalog()
    runner = _RaisingRunner(FileNotFoundError("aws"))
    client = catalog.CatalogClient(runner)

    with pytest.raises(catalog.CatalogUnavailableError) as excinfo:
        client.read("shared", "NOTION_TOKEN")

    assert "/devcontainer/shared/secrets/NOTION_TOKEN" in str(excinfo.value)


def test_read_raises_unavailable_when_sso_session_is_expired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = _import_catalog()
    monkeypatch.setenv("AWS_PROFILE", "sandbox-profile")
    runner = _FakeRunner()
    runner.queue(
        _err(
            "Error loading SSO Token: Token for sandbox-profile does not exist, "
            "the SSO session associated with this profile has expired"
        )
    )
    client = catalog.CatalogClient(runner)

    with pytest.raises(catalog.CatalogUnavailableError) as excinfo:
        client.read("shared", "NOTION_TOKEN")

    message = str(excinfo.value)
    assert "sandbox-profile" in message
    assert "aws sso login" in message


def test_read_raises_unauthorized_on_access_denied() -> None:
    catalog = _import_catalog()
    runner = _FakeRunner()
    runner.queue(
        _err(
            "An error occurred (AccessDeniedException) when calling the "
            "GetParameter operation: User is not authorized"
        )
    )
    client = catalog.CatalogClient(runner)

    with pytest.raises(catalog.CatalogUnauthorizedError) as excinfo:
        client.read("shared", "NOTION_TOKEN")

    assert "/devcontainer/shared/secrets/NOTION_TOKEN" in str(excinfo.value)


def test_read_raises_not_found_on_parameter_not_found() -> None:
    catalog = _import_catalog()
    runner = _FakeRunner()
    runner.queue(
        _err("An error occurred (ParameterNotFound) when calling the GetParameter operation: ")
    )
    client = catalog.CatalogClient(runner)

    with pytest.raises(catalog.SecretNotFoundError) as excinfo:
        client.read("shared", "NOTION_TOKEN")

    assert "/devcontainer/shared/secrets/NOTION_TOKEN" in str(excinfo.value)


def test_read_raises_unavailable_when_no_credentials_resolve() -> None:
    """The no-reachable-instance-role / no-SSO-session case, distinct from a token that expired."""
    catalog = _import_catalog()
    runner = _FakeRunner()
    runner.queue(
        _err(
            "Unable to locate credentials. You can configure credentials by "
            'running "aws configure".'
        )
    )
    client = catalog.CatalogClient(runner)

    with pytest.raises(catalog.CatalogUnavailableError) as excinfo:
        client.read("shared", "NOTION_TOKEN")

    assert "aws sso login" in str(excinfo.value)


def test_read_raises_unclassified_error_for_throttling_not_a_credential_failure() -> None:
    """code_review: an unrelated aws failure must never be misreported as an expired session."""
    catalog = _import_catalog()
    runner = _FakeRunner()
    runner.queue(
        _err(
            "An error occurred (ThrottlingException) when calling the GetParameter "
            "operation: Rate exceeded",
            returncode=254,
        )
    )
    client = catalog.CatalogClient(runner)

    with pytest.raises(catalog.CatalogUnclassifiedError) as excinfo:
        client.read("shared", "NOTION_TOKEN")

    message = str(excinfo.value)
    assert not isinstance(excinfo.value, catalog.CatalogUnavailableError)
    assert "aws sso login" not in message
    assert "ThrottlingException" not in message
    assert "/devcontainer/shared/secrets/NOTION_TOKEN" in message
    assert catalog.GET_PARAMETER_OP in message
    assert "254" in message


def _throttled_runner() -> _FakeRunner:
    runner = _FakeRunner()
    runner.queue(
        _err(
            "An error occurred (ThrottlingException) when calling the operation: Rate exceeded",
            returncode=254,
        )
    )
    return runner


def _read_args(value: str) -> tuple[str, ...]:
    del value
    return ("shared", "NOTION_TOKEN")


def _delete_args(value: str) -> tuple[str, ...]:
    del value
    return ("shared", "NOTION_TOKEN")


def _list_secrets_args(value: str) -> tuple[str, ...]:
    del value
    return ("shared",)


def _write_args(value: str) -> tuple[str, ...]:
    return ("shared", "NOTION_TOKEN", value)


@pytest.mark.parametrize(
    ("method_name", "args_factory", "must_contain", "must_not_contain"),
    [
        (
            "read",
            _read_args,
            [
                "aws ssm get-parameter --name "
                "/devcontainer/shared/secrets/NOTION_TOKEN --output json"
            ],
            ["--parameter-filters", "aws ssm put-parameter", "aws ssm describe-parameters"],
        ),
        (
            "delete",
            _delete_args,
            [
                "aws ssm delete-parameter --name "
                "/devcontainer/shared/secrets/NOTION_TOKEN --output json"
            ],
            [
                "--parameter-filters",
                "aws ssm put-parameter",
                "aws ssm describe-parameters",
                "aws ssm get-parameter",
            ],
        ),
        (
            "list_secrets",
            _list_secrets_args,
            [
                "aws ssm describe-parameters --parameter-filters "
                "Key=Path,Option=Recursive,Values=/devcontainer/shared/secrets --output json"
            ],
            ["--name", "aws ssm put-parameter", "aws ssm get-parameter"],
        ),
        (
            "write",
            _write_args,
            [
                "aws ssm get-parameter --name "
                "/devcontainer/shared/secrets/NOTION_TOKEN --output json"
            ],
            ["aws ssm put-parameter --name", "--value"],
        ),
    ],
    ids=["get-parameter", "delete-parameter", "describe-parameters", "put-parameter"],
)
def test_unclassified_failure_remediation_command_is_runnable_for_each_operation(
    method_name: str,
    args_factory: Callable[[str], tuple[str, ...]],
    must_contain: list[str],
    must_not_contain: list[str],
) -> None:
    """doc_review: the printed re-run command must actually exist for its aws subcommand.

    `describe-parameters` has no `--name` option and always operates on a
    scope prefix in this client, and re-submitting `put-parameter` would
    require typing the secret value on a command line, so each operation's
    rendered remediation is asserted here to contain only a form that
    subcommand actually accepts, pinning this against regression.
    """
    catalog = _import_catalog()
    value = _seeded_value()
    client = catalog.CatalogClient(_throttled_runner())

    with pytest.raises(catalog.CatalogUnclassifiedError) as excinfo:
        getattr(client, method_name)(*args_factory(value))

    message = str(excinfo.value)
    for token in must_contain:
        assert token in message
    for token in must_not_contain:
        assert token not in message


@pytest.mark.parametrize("name", INVALID_SECRET_NAMES, ids=INVALID_SECRET_NAME_IDS)
def test_write_raises_invalid_name_before_any_subprocess(name: str) -> None:
    catalog = _import_catalog()
    runner = _FakeRunner()
    client = catalog.CatalogClient(runner)

    with pytest.raises(catalog.InvalidSecretNameError):
        client.write("shared", name, _seeded_value())

    assert runner.calls == []


def _raising_runner_for_aws_absent(_value: str) -> _RaisingRunner:
    return _RaisingRunner(FileNotFoundError("aws"))


def _fake_runner_for_sso_expired(value: str) -> _FakeRunner:
    runner = _FakeRunner()
    # A real AWS CLI can echo request context in its stderr; seeding it with
    # the value proves the rendered message does not copy stderr verbatim.
    runner.queue(_err(f"Error loading SSO Token: session expired (request body: {value})"))
    return runner


def _fake_runner_for_access_denied(value: str) -> _FakeRunner:
    runner = _FakeRunner()
    runner.queue(
        _err(
            "An error occurred (AccessDeniedException) when calling PutParameter "
            f"operation: User is not authorized. Request: {value}"
        )
    )
    return runner


def _fake_runner_for_not_found(value: str) -> _FakeRunner:
    runner = _FakeRunner()
    runner.queue(_err(f"An error occurred (ParameterNotFound) when calling PutParameter: {value}"))
    return runner


def _fake_runner_for_unclassified_failure(value: str) -> _FakeRunner:
    runner = _FakeRunner()
    runner.queue(
        _err(
            "An error occurred (ThrottlingException) when calling PutParameter "
            f"operation: Rate exceeded. Submitted document: {value}",
            returncode=254,
        )
    )
    return runner


@pytest.mark.parametrize(
    "runner_factory",
    [
        _raising_runner_for_aws_absent,
        _fake_runner_for_sso_expired,
        _fake_runner_for_access_denied,
        _fake_runner_for_not_found,
        _fake_runner_for_unclassified_failure,
    ],
    ids=["aws-absent", "sso-expired", "access-denied", "not-found", "unclassified"],
)
def test_error_messages_never_contain_the_seeded_value(
    runner_factory: Callable[[str], _FakeRunner | _RaisingRunner],
) -> None:
    """AC-TEST-003: the seeded value is handed to the failing runner's stderr via `write`.

    Proven falsifiable by mutation: if `_raise_for_failure` were changed to
    append `stderr` to any rendered message, this test fails for every
    parametrization that seeds the value into stderr, because the value
    would then appear in `str(excinfo.value)`.
    """
    catalog = _import_catalog()
    value = _seeded_value()
    client = catalog.CatalogClient(runner_factory(value))

    with pytest.raises(catalog.CatalogError) as excinfo:
        client.write("shared", "NOTION_TOKEN", value)

    assert value not in str(excinfo.value)


# ---------------------------------------------------------------------------
# RED, the invariants (Approach step 4)
# ---------------------------------------------------------------------------


def test_full_cycle_leaves_the_working_directory_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog = _import_catalog()
    monkeypatch.chdir(tmp_path)
    before = sorted(p.name for p in tmp_path.iterdir())

    runner = _FakeRunner()
    runner.queue(_ok({"Version": 1}))  # write
    runner.queue(
        _ok(
            {
                "Parameters": [
                    {
                        "Name": "/devcontainer/shared/secrets/NOTION_TOKEN",
                        "LastModifiedDate": 1700000000.0,
                        "Description": json.dumps({"exported": False}),
                    }
                ]
            }
        )
    )  # list
    value = f"{_seeded_value()}\n"
    runner.queue(_ok({"Parameter": {"Value": value}}))  # read
    runner.queue(_ok({}))  # delete
    client = catalog.CatalogClient(runner)

    client.write("shared", "NOTION_TOKEN", value)
    client.list_secrets("shared")
    client.read("shared", "NOTION_TOKEN")
    client.delete("shared", "NOTION_TOKEN")

    after = sorted(p.name for p in tmp_path.iterdir())
    assert before == after == []


def test_module_emits_exactly_one_external_command_name() -> None:
    """AC-FUNC-007: the only external command literal is the AWS_EXECUTABLE constant."""
    source = _catalog_module_path().read_text(encoding="utf-8")
    tree = ast.parse(source)
    aws_literals = [
        node for node in ast.walk(tree) if isinstance(node, ast.Constant) and node.value == "aws"
    ]
    assert len(aws_literals) == 1


def test_module_has_no_backend_selection_construct() -> None:
    """AC-FUNC-007: no adapter registry and no branch that selects a backend.

    Inspects the parsed module for the two constructs an adapter-selection
    layer would need: a class whose bases include `Protocol` or `ABC` (a
    provider interface for more than one implementation to satisfy), and a
    module-level `if`/`match` statement (a branch that could choose between
    implementations). `CatalogClient` itself has neither.
    """
    source = _catalog_module_path().read_text(encoding="utf-8")
    tree = ast.parse(source)
    interface_base_names = {"Protocol", "ABC"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            base_names = {base.id for base in node.bases if isinstance(base, ast.Name)}
            assert not (base_names & interface_base_names), node.name
    module_level_branches = [node for node in tree.body if isinstance(node, (ast.If, ast.Match))]
    assert module_level_branches == []


# ---------------------------------------------------------------------------
# AC-CYCLE-001: end to end with the injected runner
# ---------------------------------------------------------------------------


def test_end_to_end_write_list_read_delete_then_not_found() -> None:
    catalog = _import_catalog()
    runner = _FakeRunner()
    client = catalog.CatalogClient(runner)
    value = f"{_seeded_value()}\n"

    runner.queue(_ok({"Version": 1}))
    client.write("shared", "NOTION_TOKEN", value)

    runner.queue(
        _ok(
            {
                "Parameters": [
                    {
                        "Name": "/devcontainer/shared/secrets/NOTION_TOKEN",
                        "LastModifiedDate": 1700000000.0,
                        "Description": json.dumps({"exported": False}),
                        # describe-parameters never actually returns Value;
                        # queued here anyway to prove list_secrets does not
                        # surface it even when a response entry carries one.
                        "Value": value,
                    }
                ]
            }
        )
    )
    records = client.list_secrets("shared")
    assert records[0].name == "NOTION_TOKEN"
    assert records[0].scope == "shared"
    assert records[0].last_modified == "1700000000.0"
    assert records[0].exported is False
    for record in records:
        assert not hasattr(record, "value")
        assert value not in repr(record)

    runner.queue(_ok({"Parameter": {"Value": value}}))
    read_back = client.read("shared", "NOTION_TOKEN")
    assert read_back == value

    runner.queue(_ok({}))
    client.delete("shared", "NOTION_TOKEN")

    runner.queue(
        _err("An error occurred (ParameterNotFound) when calling the GetParameter operation:")
    )
    with pytest.raises(catalog.SecretNotFoundError):
        client.read("shared", "NOTION_TOKEN")


# ---------------------------------------------------------------------------
# RED, malformed top-level responses (carry-forward from E3-F1-S1-T1 review)
# ---------------------------------------------------------------------------


def test_read_raises_catalog_error_for_response_missing_parameter_value() -> None:
    """code_review, E3-F1-S1-T1: a malformed response must not escape as a bare KeyError."""
    catalog = _import_catalog()
    runner = _FakeRunner()
    runner.queue(_ok({"Parameter": {"Name": "/devcontainer/shared/secrets/NOTION_TOKEN"}}))
    client = catalog.CatalogClient(runner)

    with pytest.raises(catalog.CatalogError) as excinfo:
        client.read("shared", "NOTION_TOKEN")

    assert not isinstance(excinfo.value, KeyError)
    assert "/devcontainer/shared/secrets/NOTION_TOKEN" in str(excinfo.value)


def test_read_raises_catalog_error_for_non_json_response() -> None:
    """code_review, E3-F1-S1-T1: a non-JSON response must not escape as a bare JSONDecodeError."""
    catalog = _import_catalog()
    runner = _FakeRunner()
    runner.queue(subprocess.CompletedProcess(args=[], returncode=0, stdout="not json", stderr=""))
    client = catalog.CatalogClient(runner)

    with pytest.raises(catalog.CatalogError) as excinfo:
        client.read("shared", "NOTION_TOKEN")

    assert not isinstance(excinfo.value, json.JSONDecodeError)


def test_read_raises_catalog_error_for_response_that_is_valid_json_but_not_an_object() -> None:
    """A response that parses (a JSON array, here) but is not an object is still malformed."""
    catalog = _import_catalog()
    runner = _FakeRunner()
    runner.queue(_ok([]))
    client = catalog.CatalogClient(runner)

    with pytest.raises(catalog.CatalogError):
        client.read("shared", "NOTION_TOKEN")


def test_list_secrets_raises_catalog_error_for_response_missing_parameters_field() -> None:
    """code_review, E3-F1-S1-T1: same guard on the listing response envelope."""
    catalog = _import_catalog()
    runner = _FakeRunner()
    runner.queue(_ok({"NotParameters": []}))
    client = catalog.CatalogClient(runner)

    with pytest.raises(catalog.CatalogError) as excinfo:
        client.list_secrets("shared")

    assert not isinstance(excinfo.value, KeyError)
    assert "/devcontainer/shared/secrets" in str(excinfo.value)


def test_list_secrets_raises_catalog_error_for_non_json_response() -> None:
    catalog = _import_catalog()
    runner = _FakeRunner()
    runner.queue(subprocess.CompletedProcess(args=[], returncode=0, stdout="not json", stderr=""))
    client = catalog.CatalogClient(runner)

    with pytest.raises(catalog.CatalogError) as excinfo:
        client.list_secrets("shared")

    assert not isinstance(excinfo.value, json.JSONDecodeError)


def test_list_secrets_raises_catalog_error_for_non_string_next_token() -> None:
    catalog = _import_catalog()
    runner = _FakeRunner()
    runner.queue(
        _ok(
            {
                "Parameters": [
                    {
                        "Name": "/devcontainer/shared/secrets/NOTION_TOKEN",
                        "LastModifiedDate": 1700000000.0,
                        "Description": json.dumps({"exported": True}),
                    }
                ],
                "NextToken": 12345,
            }
        )
    )
    client = catalog.CatalogClient(runner)

    with pytest.raises(catalog.CatalogError) as excinfo:
        client.list_secrets("shared")

    assert "NextToken" in str(excinfo.value)


# ---------------------------------------------------------------------------
# RED, the scope set (Approach step 1)
# ---------------------------------------------------------------------------


def _seeded_instance() -> str:
    """A generated instance name, unique per call, valid against `_SCOPE_PATTERN`."""
    return f"instance-{uuid.uuid4().hex}"


@pytest.mark.parametrize(
    ("instance", "expected"),
    [
        ("sandbox", ("sandbox", "shared")),
        (None, ("shared",)),
        ("shared", ("shared",)),
    ],
    ids=["with-instance", "no-instance", "instance-named-shared"],
)
def test_scope_set_is_instance_first_then_shared(
    instance: str | None, expected: tuple[str, ...]
) -> None:
    catalog = _import_catalog()
    assert catalog.scope_set(instance) == expected
    assert catalog.SHARED_SCOPE == "shared"


@pytest.mark.parametrize(
    "instance", ["", "with/slash", "with space"], ids=["empty", "path-separator", "space"]
)
def test_scope_set_rejects_invalid_instance(instance: str) -> None:
    catalog = _import_catalog()
    with pytest.raises(catalog.InvalidScopeError):
        catalog.scope_set(instance)


@pytest.mark.parametrize("scope", ["", "a/b"], ids=["empty", "path-separator"])
def test_parameter_path_rejects_invalid_scope(scope: str) -> None:
    catalog = _import_catalog()
    with pytest.raises(catalog.InvalidScopeError) as excinfo:
        catalog.parameter_path(scope, "NOTION_TOKEN")
    assert repr(scope) in str(excinfo.value)


# ---------------------------------------------------------------------------
# RED, resolve (Approach step 2)
# ---------------------------------------------------------------------------


def test_resolve_returns_value_and_answering_scope_from_shared_only() -> None:
    """AC-FUNC-003: a name present only in the shared scope resolves with scope 'shared'."""
    catalog = _import_catalog()
    runner = _FakeRunner()
    value = f"{_seeded_value()}\n"
    runner.queue(
        _err("An error occurred (ParameterNotFound) when calling the GetParameter operation:")
    )
    runner.queue(_ok({"Parameter": {"Value": value}}))
    client = catalog.CatalogClient(runner)

    resolved = catalog.resolve(client, "sandbox", "NOTION_TOKEN")

    assert resolved.value == value
    assert resolved.scope == "shared"
    assert len(runner.calls) == 2


def test_resolve_stops_at_instance_scope_without_querying_shared() -> None:
    """AC-FUNC-004 / AC-TEST-002: an instance hit never requests the shared parameter path."""
    catalog = _import_catalog()
    runner = _FakeRunner()
    value = f"{_seeded_value()}\n"
    runner.queue(_ok({"Parameter": {"Value": value}}))
    client = catalog.CatalogClient(runner)

    resolved = catalog.resolve(client, "sandbox", "JENKINS_API_TOKEN")

    assert resolved.value == value
    assert resolved.scope == "sandbox"
    assert len(runner.calls) == 1
    (argv, _stdin) = runner.calls[0]
    assert argv[argv.index("--name") + 1] == "/devcontainer/sandbox/secrets/JENKINS_API_TOKEN"
    paths_requested = [call_argv[call_argv.index("--name") + 1] for call_argv, _ in runner.calls]
    assert "/devcontainer/shared/secrets/JENKINS_API_TOKEN" not in paths_requested


def test_resolve_raises_not_found_naming_both_scopes_in_order() -> None:
    """AC-FUNC-005 / AC-TEST-003: the message names both scopes searched, in search order."""
    catalog = _import_catalog()
    instance = _seeded_instance()
    runner = _FakeRunner()
    runner.queue(
        _err("An error occurred (ParameterNotFound) when calling the GetParameter operation:")
    )
    runner.queue(
        _err("An error occurred (ParameterNotFound) when calling the GetParameter operation:")
    )
    client = catalog.CatalogClient(runner)

    with pytest.raises(catalog.SecretNotFoundError) as excinfo:
        catalog.resolve(client, instance, "NOTION_TOKEN")

    message = str(excinfo.value)
    assert instance in message
    assert catalog.SHARED_SCOPE in message
    assert message.index(instance) < message.index(catalog.SHARED_SCOPE)
    assert "devsecret list" in message


def test_resolve_with_no_instance_searches_shared_scope_only() -> None:
    catalog = _import_catalog()
    runner = _FakeRunner()
    runner.queue(
        _err("An error occurred (ParameterNotFound) when calling the GetParameter operation:")
    )
    client = catalog.CatalogClient(runner)

    with pytest.raises(catalog.SecretNotFoundError) as excinfo:
        catalog.resolve(client, None, "NOTION_TOKEN")

    assert len(runner.calls) == 1
    assert catalog.SHARED_SCOPE in str(excinfo.value)


# ---------------------------------------------------------------------------
# RED, authorization on resolve (Approach step 4)
# ---------------------------------------------------------------------------


def test_resolve_authorization_denied_on_instance_stops_before_shared_queried() -> None:
    """AC-FUNC-008 / AC-TEST-005: a denial on the instance prefix surfaces, not a partial result.

    A `SecretNotFoundError` from one scope means "keep searching"; an
    `AccessDeniedException` does not. Proven by mutation: widening
    `resolve`'s `except SecretNotFoundError:` to `except CatalogError:`
    would swallow this denial and fall through to the shared answer queued
    behind it, so this test must fail against that mutation.
    """
    catalog = _import_catalog()
    runner = _FakeRunner()
    value = f"{_seeded_value()}\n"
    runner.queue(
        _err(
            "An error occurred (AccessDeniedException) when calling the "
            "GetParameter operation: User is not authorized"
        )
    )
    runner.queue(_ok({"Parameter": {"Value": value}}))
    client = catalog.CatalogClient(runner)

    with pytest.raises(catalog.CatalogUnauthorizedError) as excinfo:
        catalog.resolve(client, "sandbox", "NOTION_TOKEN")

    assert "/devcontainer/sandbox/secrets/NOTION_TOKEN" in str(excinfo.value)
    assert len(runner.calls) == 1


def test_resolve_authorization_denied_on_shared_after_instance_not_found() -> None:
    """AC-FUNC-008 / AC-TEST-005, the mirror direction: shared denied after instance not-found.

    The instance scope answering `ParameterNotFound` means "keep
    searching", so the shared prefix is queried next; that prefix's
    `AccessDeniedException` must surface as `CatalogUnauthorizedError`, not
    be reported as `SecretNotFoundError`, so a denial is never mistaken for
    "the name does not exist anywhere".
    """
    catalog = _import_catalog()
    runner = _FakeRunner()
    runner.queue(
        _err("An error occurred (ParameterNotFound) when calling the GetParameter operation:")
    )
    runner.queue(
        _err(
            "An error occurred (AccessDeniedException) when calling the "
            "GetParameter operation: User is not authorized"
        )
    )
    client = catalog.CatalogClient(runner)

    with pytest.raises(catalog.CatalogUnauthorizedError) as excinfo:
        catalog.resolve(client, "sandbox", "NOTION_TOKEN")

    assert "/devcontainer/shared/secrets/NOTION_TOKEN" in str(excinfo.value)
    assert len(runner.calls) == 2


# ---------------------------------------------------------------------------
# RED, listing (Approach step 3)
# ---------------------------------------------------------------------------


def _describe_response(
    entries: list[tuple[str, str, float, bool]],
) -> subprocess.CompletedProcess[str]:
    """Build a `describe-parameters` payload from `(name, scope, last_modified, exported)` rows."""
    return _ok(
        {
            "Parameters": [
                {
                    "Name": f"/devcontainer/{scope}/secrets/{name}",
                    "LastModifiedDate": last_modified,
                    "Description": json.dumps({"exported": exported}),
                }
                for (name, scope, last_modified, exported) in entries
            ]
        }
    )


def test_list_resolved_returns_one_record_per_stored_parameter_with_shadow_marking() -> None:
    """AC-FUNC-006: both tiers' records are returned; the shared one is marked shadowed."""
    catalog = _import_catalog()
    runner = _FakeRunner()
    runner.queue(
        _describe_response(
            [
                ("JENKINS_API_TOKEN", "sandbox", 1700000000.0, False),
                ("NOTION_TOKEN", "sandbox", 1700000100.0, True),
            ]
        )
    )
    runner.queue(
        _describe_response(
            [
                ("ANTHROPIC_API_KEY", "shared", 1700000200.0, True),
                ("NOTION_TOKEN", "shared", 1700000300.0, True),
            ]
        )
    )
    client = catalog.CatalogClient(runner)

    records = catalog.list_resolved(client, "sandbox")

    assert len(records) == 4
    by_scope_and_name = {(r.scope, r.name): r for r in records}
    assert by_scope_and_name[("sandbox", "JENKINS_API_TOKEN")].in_effect is True
    assert by_scope_and_name[("sandbox", "NOTION_TOKEN")].in_effect is True
    assert by_scope_and_name[("shared", "ANTHROPIC_API_KEY")].in_effect is True
    assert by_scope_and_name[("shared", "NOTION_TOKEN")].in_effect is False


def test_list_resolved_never_requests_decryption_or_exposes_a_value() -> None:
    """AC-FUNC-007 / AC-TEST-004: no call on the listing path decrypts anything."""
    catalog = _import_catalog()
    runner = _FakeRunner()
    runner.queue(_describe_response([("JENKINS_API_TOKEN", "sandbox", 1700000000.0, False)]))
    runner.queue(_describe_response([("ANTHROPIC_API_KEY", "shared", 1700000100.0, True)]))
    client = catalog.CatalogClient(runner)

    records = catalog.list_resolved(client, "sandbox")

    assert len(records) == 2
    for record in records:
        assert not hasattr(record, "value")
    for argv, stdin in runner.calls:
        assert "--with-decryption" not in argv
        assert stdin is None


def test_list_resolved_authorization_denied_on_shared_after_instance_answered() -> None:
    """AC-FUNC-008 / AC-TEST-005: a denial on the second scope surfaces, not a partial result."""
    catalog = _import_catalog()
    runner = _FakeRunner()
    runner.queue(_describe_response([("JENKINS_API_TOKEN", "sandbox", 1700000000.0, False)]))
    runner.queue(
        _err(
            "An error occurred (AccessDeniedException) when calling the "
            "DescribeParameters operation: User is not authorized"
        )
    )
    client = catalog.CatalogClient(runner)

    with pytest.raises(catalog.CatalogUnauthorizedError) as excinfo:
        catalog.list_resolved(client, "sandbox")

    assert "/devcontainer/shared/secrets" in str(excinfo.value)


def test_list_resolved_authorization_denied_on_instance_stops_before_shared_queried() -> None:
    """AC-FUNC-008 / AC-TEST-005, the mirror direction: instance denied, shared never queried."""
    catalog = _import_catalog()
    runner = _FakeRunner()
    runner.queue(
        _err(
            "An error occurred (AccessDeniedException) when calling the "
            "DescribeParameters operation: User is not authorized"
        )
    )
    client = catalog.CatalogClient(runner)

    with pytest.raises(catalog.CatalogUnauthorizedError) as excinfo:
        catalog.list_resolved(client, "sandbox")

    assert "/devcontainer/sandbox/secrets" in str(excinfo.value)
    assert len(runner.calls) == 1


def test_list_resolved_rejects_unknown_scope_before_any_subprocess() -> None:
    catalog = _import_catalog()
    runner = _FakeRunner()
    client = catalog.CatalogClient(runner)

    with pytest.raises(catalog.UnknownScopeError) as excinfo:
        catalog.list_resolved(client, "sandbox", scope="prod")

    assert "prod" in str(excinfo.value)
    assert "sandbox" in str(excinfo.value)
    assert "shared" in str(excinfo.value)
    assert runner.calls == []


def test_list_resolved_narrows_to_one_scope_when_given() -> None:
    catalog = _import_catalog()
    runner = _FakeRunner()
    runner.queue(_describe_response([("ANTHROPIC_API_KEY", "shared", 1700000000.0, True)]))
    client = catalog.CatalogClient(runner)

    records = catalog.list_resolved(client, "sandbox", scope="shared")

    assert [r.name for r in records] == ["ANTHROPIC_API_KEY"]
    assert len(runner.calls) == 1


def test_list_resolved_narrowed_scope_never_shadows_against_an_unqueried_tier() -> None:
    """Pins `SecretRecord.in_effect`'s documented narrowed-scope contract.

    A narrowed, single-scope `list_resolved` call never queries any other
    tier, so `in_effect` is computed only across the scope this call
    actually queried -- even when a name also exists in a scope earlier in
    the full resolution order that this call never queried. This is the
    documented behavior (see `SecretRecord.in_effect`'s docstring); a
    caller that needs shadowing across the whole resolution set must call
    `list_resolved` without `scope`, which
    `test_list_resolved_returns_one_record_per_stored_parameter_with_shadow_marking`
    pins separately.
    """
    catalog = _import_catalog()
    runner = _FakeRunner()
    runner.queue(_describe_response([("NOTION_TOKEN", "shared", 1700000000.0, True)]))
    client = catalog.CatalogClient(runner)

    records = catalog.list_resolved(client, "sandbox", scope="shared")

    assert len(runner.calls) == 1
    assert [(r.scope, r.name, r.in_effect) for r in records] == [("shared", "NOTION_TOKEN", True)]


# ---------------------------------------------------------------------------
# AC-CYCLE-001: end to end with the injected runner, instance-first then shared
# ---------------------------------------------------------------------------


def test_end_to_end_resolve_and_list_across_instance_and_shared_scopes() -> None:
    catalog = _import_catalog()
    runner = _FakeRunner()
    client = catalog.CatalogClient(runner)
    anthropic_value = f"{_seeded_value()}\n"
    jenkins_value = f"{_seeded_value()}\n"
    notion_value = f"{_seeded_value()}\n"

    # Seed: shared holds ANTHROPIC_API_KEY; sandbox holds JENKINS_API_TOKEN;
    # both hold NOTION_TOKEN.
    runner.queue(
        _err("An error occurred (ParameterNotFound) when calling the GetParameter operation:")
    )
    runner.queue(_ok({"Parameter": {"Value": anthropic_value}}))
    resolved_anthropic = catalog.resolve(client, "sandbox", "ANTHROPIC_API_KEY")
    assert resolved_anthropic.value == anthropic_value
    assert resolved_anthropic.scope == "shared"

    runner.queue(_ok({"Parameter": {"Value": jenkins_value}}))
    resolved_jenkins = catalog.resolve(client, "sandbox", "JENKINS_API_TOKEN")
    assert resolved_jenkins.value == jenkins_value
    assert resolved_jenkins.scope == "sandbox"

    runner.queue(_ok({"Parameter": {"Value": notion_value}}))
    resolved_notion = catalog.resolve(client, "sandbox", "NOTION_TOKEN")
    assert resolved_notion.value == notion_value
    assert resolved_notion.scope == "sandbox"

    runner.queue(
        _describe_response(
            [
                ("JENKINS_API_TOKEN", "sandbox", 1700000000.0, False),
                ("NOTION_TOKEN", "sandbox", 1700000100.0, True),
            ]
        )
    )
    runner.queue(
        _describe_response(
            [
                ("ANTHROPIC_API_KEY", "shared", 1700000200.0, True),
                ("NOTION_TOKEN", "shared", 1700000300.0, True),
            ]
        )
    )
    records = catalog.list_resolved(client, "sandbox")

    assert len(records) == 4
    shadowed = [r for r in records if not r.in_effect]
    assert len(shadowed) == 1
    assert shadowed[0].name == "NOTION_TOKEN"
    assert shadowed[0].scope == "shared"
