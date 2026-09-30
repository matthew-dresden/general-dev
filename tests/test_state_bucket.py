"""Tests for `devcontainer_config.state_bucket`, the backend existence probe.

`tg_init` in the Makefile delegates one decision to this module before any
Terragrunt command runs: does the instance's backend bucket exist yet? The
answer picks the init form -- plain `terragrunt init` for a bucket that
exists, `terragrunt init --backend-bootstrap` for one that is confirmed
missing -- and every probe outcome that is not a confirmed-missing bucket
is raised with the command and its raw output, so no failure is ever
caught and retried under a different flag.

These tests pin that contract: the bucket name is composed from
`remote-instances/root.hcl`'s own template and suffix (parsed here through
the same functions production uses, so a parser drift from root.hcl's
grammar is caught), the account is resolved from the ambient chain with no
profile, the head-bucket probe maps exit 0 to the plain form and
Terragrunt's own 404 wording to the bootstrap form, and every other answer
-- a 403 on a bucket this identity cannot see, an unreachable endpoint, a
failed account lookup -- raises naming the command and carrying its raw
output. `_FakeRunner` is the queued Runner double in the `instance_ops`
shape this module consumes; every generated identifier is built at test
time and no test touches AWS, the network or a real file outside
`tmp_path`.
"""

from __future__ import annotations

import json
import subprocess
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from devcontainer_config import state_bucket
from devcontainer_config.state_bucket import StateBucketError

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

# The template is composed from concatenated pieces so no source line
# exceeds the lint limit; the pieces join into the same single line
# `remote-instances/root.hcl` declares.
_BUCKET_TEMPLATE = (
    "tg-state-${local.instance_name}-${local.aws_region}"
    "-${local.account_id}-${local.state_bucket_suffix}"
)

ROOT_HCL = f"""\
locals {{
  aws_region = get_env("REMOTE_AWS_REGION")
  account_id = get_aws_account_id()
  state_bucket_suffix = "9d81aa"
  state_bucket_name = "{_BUCKET_TEMPLATE}"
}}
"""


def _ok(stdout: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


def _err(stderr: str, *, returncode: int = 1) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout="", stderr=stderr)


class _FakeRunner:
    """A queued Runner double: records every argv, answers from a queue.

    The queue is strict: an invocation with no queued response fails the
    test, so an unexpected aws call can never pass silently.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self._queue: list[subprocess.CompletedProcess[str]] = []

    def queue(self, *results: subprocess.CompletedProcess[str]) -> None:
        self._queue.extend(results)

    def __call__(
        self, argv: Sequence[str], stdin: str | None, *, env: Mapping[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(tuple(argv))
        assert self._queue, f"_FakeRunner was invoked with no queued response: {tuple(argv)!r}"
        return self._queue.pop(0)


def _instance_name() -> str:
    """A valid instance name unique per call."""
    return f"inst-{uuid.uuid4().hex[:8]}"


def _account_id() -> str:
    """A twelve-digit account-id-shaped value, generated per call."""
    return "".join(str(uuid.uuid4().int % 10) for _ in range(12))


def _root_with_root_hcl(tmp_path: Path) -> Path:
    """A scratch repository root carrying this repository's root.hcl shape."""
    instances_dir = tmp_path / "remote-instances"
    instances_dir.mkdir()
    (instances_dir / "root.hcl").write_text(ROOT_HCL, encoding="utf-8")
    return tmp_path


# ---------------------------------------------------------------------------
# Composition: the name comes from root.hcl's own template and suffix.
# ---------------------------------------------------------------------------


def test_compose_substitutes_every_component_in_the_template_order() -> None:
    instance = _instance_name()
    region = "us-west-2"
    account = _account_id()
    name = state_bucket.compose_from_root_hcl(ROOT_HCL, instance, region, account)
    assert name == f"tg-state-{instance}-{region}-{account}-9d81aa"


def test_compose_raises_naming_the_template_when_it_is_missing() -> None:
    with pytest.raises(StateBucketError, match="state_bucket_name"):
        state_bucket.compose_from_root_hcl("locals {\n}\n", _instance_name(), "us-east-1", "1" * 12)


def test_compose_raises_naming_the_suffix_when_it_is_missing() -> None:
    without_suffix = ROOT_HCL.replace('  state_bucket_suffix = "9d81aa"\n', "")
    with pytest.raises(StateBucketError, match="state_bucket_suffix"):
        state_bucket.compose_from_root_hcl(without_suffix, _instance_name(), "us-east-1", "1" * 12)


def test_compose_raises_naming_the_suffix_when_it_is_empty() -> None:
    empty_suffix = ROOT_HCL.replace("9d81aa", "")
    with pytest.raises(StateBucketError, match="state_bucket_suffix"):
        state_bucket.compose_from_root_hcl(empty_suffix, _instance_name(), "us-east-1", "1" * 12)


def test_compose_raises_naming_an_unsupplied_component() -> None:
    with pytest.raises(StateBucketError, match="unknown_component"):
        state_bucket.compose_from_root_hcl(
            ROOT_HCL.replace("${local.instance_name}", "${local.unknown_component}"),
            _instance_name(),
            "us-east-1",
            "1" * 12,
        )


# ---------------------------------------------------------------------------
# bucket_name: root.hcl is read from the repo root, the account from the
# ambient chain.
# ---------------------------------------------------------------------------


def test_bucket_name_reads_root_hcl_and_the_ambient_account(
    tmp_path: Path,
) -> None:
    root = _root_with_root_hcl(tmp_path)
    instance = _instance_name()
    account = _account_id()
    runner = _FakeRunner()
    runner.queue(_ok(f"{account}\n"))

    name = state_bucket.bucket_name(root, instance, "us-east-1", runner)

    assert name == f"tg-state-{instance}-us-east-1-{account}-9d81aa"
    assert runner.calls == [
        ("aws", "sts", "get-caller-identity", "--query", "Account", "--output", "text")
    ]


def test_bucket_name_refuses_an_invalid_instance_name_before_any_aws_call(
    tmp_path: Path,
) -> None:
    root = _root_with_root_hcl(tmp_path)
    runner = _FakeRunner()

    with pytest.raises(StateBucketError, match="invalid instance name"):
        state_bucket.bucket_name(root, "../escape", "us-east-1", runner)

    assert runner.calls == []


def test_bucket_name_raises_when_root_hcl_is_missing(tmp_path: Path) -> None:
    runner = _FakeRunner()
    runner.queue(_ok(f"{_account_id()}\n"))

    with pytest.raises(StateBucketError, match="root.hcl"):
        state_bucket.bucket_name(tmp_path, _instance_name(), "us-east-1", runner)


def test_account_lookup_failure_raises_with_the_command(tmp_path: Path) -> None:
    root = _root_with_root_hcl(tmp_path)
    runner = _FakeRunner()
    runner.queue(_err("AccessDenied"))

    with pytest.raises(StateBucketError, match="get-caller-identity") as exc_info:
        state_bucket.bucket_name(root, _instance_name(), "us-east-1", runner)

    assert "AccessDenied" in str(exc_info.value)


# ---------------------------------------------------------------------------
# init_form: one head-bucket probe picks the form; everything else raises.
# ---------------------------------------------------------------------------


def test_existing_bucket_answers_the_plain_form(tmp_path: Path) -> None:
    root = _root_with_root_hcl(tmp_path)
    instance = _instance_name()
    runner = _FakeRunner()
    runner.queue(_ok(f"{_account_id()}\n"))  # account lookup
    runner.queue(_ok(""))  # head-bucket: exists

    assert state_bucket.init_form(root, instance, "us-east-1", runner) == "plain"


def test_confirmed_missing_bucket_answers_the_bootstrap_form(tmp_path: Path) -> None:
    root = _root_with_root_hcl(tmp_path)
    instance = _instance_name()
    account = _account_id()
    runner = _FakeRunner()
    runner.queue(_ok(f"{account}\n"))
    runner.queue(
        _err(
            "An error occurred (404) when calling the HeadBucket operation: Not Found",
            returncode=1,
        )
    )

    assert state_bucket.init_form(root, instance, "us-east-1", runner) == "bootstrap"
    assert runner.calls[1] == (
        "aws",
        "s3api",
        "head-bucket",
        "--bucket",
        f"tg-state-{instance}-us-east-1-{account}-9d81aa",
        "--region",
        "us-east-1",
    )


def test_nosuch_bucket_error_also_answers_the_bootstrap_form(tmp_path: Path) -> None:
    root = _root_with_root_hcl(tmp_path)
    runner = _FakeRunner()
    runner.queue(_ok(f"{_account_id()}\n"))
    runner.queue(_err("NoSuchBucket: The specified bucket does not exist"))

    assert state_bucket.init_form(root, _instance_name(), "us-east-1", runner) == "bootstrap"


def test_forbidden_bucket_raises_instead_of_bootstrapping(tmp_path: Path) -> None:
    """A 403 means the name is not confirmed missing: bootstrap must not run.

    A bucket this identity cannot see could be someone else's; provisioning
    over an unconfirmed answer would answer the wrong question. The probe's
    raw output must surface in the error.
    """
    root = _root_with_root_hcl(tmp_path)
    runner = _FakeRunner()
    runner.queue(_ok(f"{_account_id()}\n"))
    raw = "An error occurred (403) when calling the HeadBucket operation: Forbidden"
    runner.queue(_err(raw, returncode=1))

    with pytest.raises(StateBucketError) as exc_info:
        state_bucket.init_form(root, _instance_name(), "us-east-1", runner)

    message = str(exc_info.value)
    assert "head-bucket" in message
    assert raw in message
    assert "no bootstrap is attempted" in message


def test_unexpected_probe_failure_raises_with_the_raw_output(tmp_path: Path) -> None:
    root = _root_with_root_hcl(tmp_path)
    runner = _FakeRunner()
    runner.queue(_ok(f"{_account_id()}\n"))
    raw = "EndpointConnectionError: could not connect to the endpoint URL"
    runner.queue(_err(raw, returncode=255))

    with pytest.raises(StateBucketError) as exc_info:
        state_bucket.init_form(root, _instance_name(), "us-east-1", runner)

    assert raw in str(exc_info.value)


def test_missing_head_bucket_binary_raises(tmp_path: Path) -> None:
    """A host without the aws CLI fails loudly, not as a missing bucket.

    The runner answers the account lookup normally and fails only when the
    head-bucket probe runs, so the error names the probe, not the lookup.
    """

    class _HeadBucketRaisingRunner:
        def __init__(self, inner: _FakeRunner) -> None:
            self._inner = inner

        def __call__(
            self, argv: Sequence[str], stdin: str | None, *, env: Mapping[str, str] | None = None
        ) -> subprocess.CompletedProcess[str]:
            if "head-bucket" in argv:
                raise FileNotFoundError(2, "No such file or directory", argv[0])
            return self._inner(argv, stdin, env=env)

    root = _root_with_root_hcl(tmp_path)
    runner = _FakeRunner()
    runner.queue(_ok(f"{_account_id()}\n"))

    with pytest.raises(StateBucketError, match="head-bucket"):
        state_bucket.init_form(
            root, _instance_name(), "us-east-1", _HeadBucketRaisingRunner(runner)
        )


# ---------------------------------------------------------------------------
# The module entry point.
# ---------------------------------------------------------------------------


def test_main_init_form_prints_the_form_and_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _root_with_root_hcl(tmp_path)
    instance = _instance_name()
    account = _account_id()
    runner = _FakeRunner()
    runner.queue(_ok(f"{account}\n"))
    runner.queue(_ok(""))
    monkeypatch.setattr(state_bucket, "subprocess_runner", runner)
    monkeypatch.setenv("REMOTE_AWS_REGION", "us-east-1")

    exit_code = state_bucket.main(["init-form", str(root), instance])

    assert exit_code == 0
    assert capsys.readouterr().out.strip() == "plain"


def test_main_init_form_without_the_region_names_the_remedy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _root_with_root_hcl(tmp_path)
    monkeypatch.delenv("REMOTE_AWS_REGION", raising=False)

    exit_code = state_bucket.main(["init-form", str(root), _instance_name()])

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "export REMOTE_AWS_REGION=" in captured.err
    assert "us-east-1" in captured.err


def test_main_rejects_unknown_commands_and_wrong_argument_counts(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert state_bucket.main(["frobnicate"]) == 2
    assert state_bucket.main([]) == 2
    assert state_bucket.main(["delete", "a", "b"]) == 1
    captured = capsys.readouterr()
    assert "usage:" in captured.err
    assert "takes exactly" in captured.err


def test_main_reports_probe_failures_on_stderr_and_exits_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _root_with_root_hcl(tmp_path)
    runner = _FakeRunner()
    runner.queue(_err("AccessDenied"))
    monkeypatch.setattr(state_bucket, "subprocess_runner", runner)
    monkeypatch.setenv("REMOTE_AWS_REGION", "us-east-1")

    exit_code = state_bucket.main(["init-form", str(root), _instance_name()])

    assert exit_code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "AccessDenied" in captured.err


# ---------------------------------------------------------------------------
# Listing and deletion: the pattern derives from root.hcl's own template.
# ---------------------------------------------------------------------------


def _bucket_listing_name(instance: str, region: str, account: str) -> str:
    return f"tg-state-{instance}-{region}-{account}-9d81aa"


def test_bucket_name_pattern_matches_the_composed_name(tmp_path: Path) -> None:
    import re

    root = _root_with_root_hcl(tmp_path)
    pattern = state_bucket.bucket_name_pattern(root)
    assert pattern.fullmatch(_bucket_listing_name("inst-x", "us-east-1", "1" * 12))
    assert pattern.fullmatch(_bucket_listing_name("weird_name-2", "eu-west-9", "9" * 12))
    assert not pattern.fullmatch("tg-state-other-suffix-3734c3")
    assert not pattern.fullmatch("someone-elses-bucket")


def test_list_buckets_reports_configured_and_orphaned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _root_with_root_hcl(tmp_path)
    (root / "remote-instances" / "inst-x").mkdir()
    (root / "remote-instances" / "inst-x" / "terragrunt.hcl").write_text(
        "# configured\n", encoding="utf-8"
    )
    monkeypatch.setenv("REMOTE_AWS_REGION", "us-east-1")
    account = "1" * 12
    ours = _bucket_listing_name("inst-x", "us-east-1", account)
    orphan = _bucket_listing_name("inst-gone", "us-east-1", account)
    other = "some-other-repos-bucket"
    runner = _FakeRunner()
    runner.queue(_ok(f"{account}\n"))  # the configured instances' account lookup
    runner.queue(_ok(json.dumps({"Buckets": [{"Name": orphan}, {"Name": other}, {"Name": ours}]})))

    listings = state_bucket.list_buckets(root, runner)

    assert [(listing.name, listing.configured) for listing in listings] == [
        (ours, True),
        (orphan, False),
    ]
    ours_listing = listings[0]
    assert ours_listing.instance == "inst-x" and ours_listing.account_id == account
    assert listings[1].instance == "" and listings[1].region == ""


def test_list_buckets_raises_on_a_failed_or_unparseable_aws_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _root_with_root_hcl(tmp_path)
    monkeypatch.setenv("REMOTE_AWS_REGION", "us-east-1")
    runner = _FakeRunner()
    runner.queue(_err("AccessDenied"))
    with pytest.raises(StateBucketError, match="account id lookup failed"):
        state_bucket.list_buckets(root, runner)
    runner = _FakeRunner()
    runner.queue(_ok(f"{_account_id()}\n"))
    runner.queue(_ok("not json"))
    with pytest.raises(StateBucketError, match="not valid JSON"):
        state_bucket.list_buckets(root, runner)


def test_delete_bucket_purges_every_version_then_deletes(tmp_path: Path) -> None:
    account = "1" * 12
    bucket = _bucket_listing_name("inst-x", "us-east-1", account)
    versions = json.dumps(
        {
            "Versions": [{"Key": "inst-x/terraform.tfstate", "VersionId": "v1"}],
            "DeleteMarkers": [{"Key": "inst-x/terraform.tfstate.tflock", "VersionId": "v2"}],
        }
    )
    runner = _FakeRunner()
    runner.queue(_ok(""))  # head-bucket: exists
    runner.queue(_ok(versions))  # list-object-versions
    runner.queue(_ok(json.dumps({})))  # delete-objects batch
    runner.queue(_ok(""))  # delete-bucket

    message = state_bucket.delete_bucket(bucket, "us-east-1", runner)

    assert "deleted" in message and "2 version(s)" in message
    calls = runner.calls
    assert calls[0] == ("aws", "s3api", "head-bucket", "--bucket", bucket, "--region", "us-east-1")
    assert calls[2][0:3] == ("aws", "s3api", "delete-objects")
    assert calls[3] == (
        "aws",
        "s3api",
        "delete-bucket",
        "--bucket",
        bucket,
        "--region",
        "us-east-1",
    )


def test_delete_bucket_reports_an_absent_bucket_without_touching_anything(tmp_path: Path) -> None:
    account = "1" * 12
    bucket = _bucket_listing_name("inst-x", "us-east-1", account)
    runner = _FakeRunner()
    runner.queue(
        _err(
            "An error occurred (404) when calling the HeadBucket operation: Not Found",
            returncode=1,
        )
    )

    message = state_bucket.delete_bucket(bucket, "us-east-1", runner)

    assert "already absent" in message
    assert len(runner.calls) == 1


def test_delete_bucket_surfaces_a_failed_purge(tmp_path: Path) -> None:
    account = "1" * 12
    bucket = _bucket_listing_name("inst-x", "us-east-1", account)
    runner = _FakeRunner()
    runner.queue(_ok(""))  # head-bucket: exists
    runner.queue(_ok(json.dumps({"Versions": [{"Key": "k", "VersionId": "v"}]})))
    runner.queue(_err("AccessDenied"))

    with pytest.raises(StateBucketError, match="purging") as exc_info:
        state_bucket.delete_bucket(bucket, "us-east-1", runner)

    assert "AccessDenied" in str(exc_info.value)
    assert len(runner.calls) == 3, "the delete must not run when the purge failed"


def test_delete_instance_bucket_refuses_an_invalid_name_before_any_aws_call(
    tmp_path: Path,
) -> None:
    root = _root_with_root_hcl(tmp_path)
    runner = _FakeRunner()

    with pytest.raises(StateBucketError, match="invalid instance name"):
        state_bucket.delete_instance_bucket(root, "../escape", "us-east-1", runner)

    assert runner.calls == []


def test_delete_matching_buckets_scopes_to_the_region(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _root_with_root_hcl(tmp_path)
    monkeypatch.setenv("REMOTE_AWS_REGION", "us-east-1")
    account = "1" * 12
    east = _bucket_listing_name("inst-a", "us-east-1", account)
    west = _bucket_listing_name("inst-b", "us-west-2", account)
    runner = _FakeRunner()
    runner.queue(_ok(f"{account}\n"))  # the listing's account lookup
    runner.queue(_ok(json.dumps({"Buckets": [{"Name": east}, {"Name": west}]})))
    runner.queue(_ok(""))  # head: east exists
    runner.queue(_ok(json.dumps({})))  # purge: empty
    runner.queue(_ok(""))  # delete east
    runner.queue(
        _err(
            "An error occurred (301) when calling the HeadBucket operation: Moved Permanently",
            returncode=1,
        )
    )  # head: west lives in another region

    messages = state_bucket.delete_matching_buckets(root, "us-east-1", runner)

    assert len(messages) == 2
    assert east in messages[0] and "deleted" in messages[0]
    assert west in messages[1] and "skipped" in messages[1] and "different region" in messages[1]


def test_main_list_and_delete_all_run_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import subprocess

    from gitfixtures import generated_root, init_repo

    root = generated_root(tmp_path)
    init_repo(root)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "remote",
            "add",
            "origin",
            "https://example.invalid/org/general-dev.git",
        ],
        check=True,
        capture_output=True,
    )
    instances_dir = root / "remote-instances"
    instances_dir.mkdir()
    (instances_dir / "root.hcl").write_text(ROOT_HCL, encoding="utf-8")
    account = "1" * 12
    bucket = _bucket_listing_name("x", "us-east-1", account)
    runner = _FakeRunner()
    runner.queue(_ok(f"{account}\n"))  # list's account lookup
    runner.queue(_ok(json.dumps({"Buckets": [{"Name": bucket}]})))
    monkeypatch.setattr(state_bucket, "subprocess_runner", runner)
    monkeypatch.setenv("REMOTE_AWS_REGION", "us-east-1")
    monkeypatch.chdir(root)

    assert state_bucket.main(["list"]) == 0
    out = capsys.readouterr().out
    assert bucket in out and "orphaned" in out

    runner.queue(_ok(f"{account}\n"))  # delete-all's account lookup
    runner.queue(_ok(json.dumps({"Buckets": [{"Name": bucket}]})))  # delete-all's own listing
    runner.queue(_ok(""))  # head: exists
    runner.queue(_ok(json.dumps({})))  # purge: empty
    runner.queue(_ok(""))  # delete
    assert state_bucket.main(["delete-all"]) == 0
    out = capsys.readouterr().out
    assert "deleted" in out


def test_main_delete_without_the_region_names_the_remedy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import subprocess

    from gitfixtures import generated_root, init_repo

    root = generated_root(tmp_path)
    init_repo(root)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "remote",
            "add",
            "origin",
            "https://example.invalid/org/general-dev.git",
        ],
        check=True,
        capture_output=True,
    )
    monkeypatch.setattr(state_bucket, "subprocess_runner", _FakeRunner())
    monkeypatch.delenv("REMOTE_AWS_REGION", raising=False)
    monkeypatch.chdir(root)

    exit_code = state_bucket.main(["delete", "x"])

    assert exit_code == 1
    assert "export REMOTE_AWS_REGION=" in capsys.readouterr().err
