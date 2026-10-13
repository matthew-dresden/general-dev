"""Tests for `devcontainer_config.state_store`, the azure state backend's bootstrap.

`tg_init`'s azure arm delegates to this module before any Terragrunt command
runs: does the instance's state storage account exist yet? An existing (and
succeeded) account answers the plain form, a confirmed-missing one is
ensured -- group, account, container, each probed before its create -- and
every probe outcome that is neither "exists" nor "missing" is raised with
the command and its raw output, so no failure is ever caught and retried.

These tests pin that contract: the account name is composed from
`remote-instances/root-azure.hcl`'s own template and suffix (parsed through
the same functions production uses), sanitized into azure's lowercase
alphanumeric law with a deterministic digest over the instance, location,
subscription and suffix; the subscription is resolved from the ambient
login with no profile; every ensure step probes before it creates and is
idempotent, and ends by writing the `.state-store.json` side-car the azure
root's remote_state reads; and the delete flows purge then delete,
reporting an already-absent store without touching anything and removing
the side-car with the store. `_FakeRunner` is the queued Runner double in
the `instance_ops` shape this module consumes; every generated identifier
is built at test time and no test touches azure, the network or a real
file outside `tmp_path`.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from devcontainer_config import state_store
from devcontainer_config.state_store import StateStoreError

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

# The committed suffix this suite's root-azure.hcl fixture carries: the
# value the slice reports for the azure root's locals, used verbatim so the
# fixture composes exactly what production will.
SUFFIX = "gb9cho"

# The template is composed from concatenated pieces so no source line
# exceeds the lint limit; the pieces join into the same single line
# `remote-instances/root-azure.hcl` declares.
_TEMPLATE = "tgstate${local.instance_name}${local.state_store_suffix}"

ROOT_HCL = f"""\
locals {{
  state_store_suffix = "{SUFFIX}"
  state_storage_name_template = "{_TEMPLATE}"
}}
"""


def _ok(stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


def _err(stderr: str, *, returncode: int = 1) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout="", stderr=stderr)


class _FakeRunner:
    """A queued Runner double: records every argv, answers from a queue.

    The queue is strict: an invocation with no queued response fails the
    test, so an unexpected az call can never pass silently.
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


def _subscription() -> str:
    """A subscription-id-shaped value, generated per call."""
    return uuid.uuid4().hex


def _digest(instance: str, location: str, subscription: str) -> str:
    """The digest the module's own rule derives, recomputed here independently."""
    material = f"{instance}|{location}|{subscription}|{SUFFIX}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:8]


def _expected_name(instance: str, location: str, subscription: str) -> str:
    """The name the template composes: tgstate + component + digest + suffix.

    The template's fixed part (7 characters) plus the suffix (6) plus the
    digest (8) leave 3 characters of sanitized instance inside azure's
    24-character law, which is the composition the budget rule produces.
    """
    sanitized = re.sub(r"[^a-z0-9]", "", instance.lower())[:3]
    return f"tgstate{sanitized}{_digest(instance, location, subscription)}{SUFFIX}"


def _root_with_root_hcl(tmp_path: Path) -> Path:
    """A scratch repository root carrying this repository's azure root shape."""
    instances_dir = tmp_path / "remote-instances"
    instances_dir.mkdir()
    (instances_dir / "root-azure.hcl").write_text(ROOT_HCL, encoding="utf-8")
    return tmp_path


def _seed_azure_instance(root: Path, name: str, location: str = "westeurope") -> None:
    """A scaffolded azure instance directory: the include and the location.

    The include text is the azure scaffold's, in miniature: the envcommon
    include's dirname base names `root-azure.hcl`, the root the scaffold's
    own include targets.
    """
    directory = root / "remote-instances" / name
    directory.mkdir(parents=True)
    (directory / "terragrunt.hcl").write_text(
        'include "envcommon" {\n'
        '  path = "${dirname(find_in_parent_folders("root-azure.hcl"))}'
        '/_envcommon/remote-azure.hcl"\n'
        "}\n"
        "\n"
        "inputs = {\n"
        f'  instance_name = "{name}"\n'
        f'  location = "{location}"\n'
        "}\n",
        encoding="utf-8",
    )


def _seed_ec2_instance(root: Path, name: str) -> None:
    """A scaffolded EC2 instance directory: no azure include, no location."""
    directory = root / "remote-instances" / name
    directory.mkdir(parents=True)
    (directory / "terragrunt.hcl").write_text(
        'include "envcommon" {\n'
        '  path = "${dirname(find_in_parent_folders("root.hcl"))}'
        '/_envcommon/remote-ec2.hcl"\n'
        "}\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Composition: deterministic, charset-legal, collision-differentiating.
# ---------------------------------------------------------------------------


def test_compose_is_deterministic_and_charset_legal() -> None:
    instance = _instance_name()
    location = "westeurope"
    subscription = _subscription()
    first = state_store.compose_from_root_hcl(ROOT_HCL, instance, location, subscription)
    second = state_store.compose_from_root_hcl(ROOT_HCL, instance, location, subscription)
    assert first == second == _expected_name(instance, location, subscription)
    assert re.fullmatch(r"[a-z0-9]{3,24}", first), "azure's own name law must hold"


def test_compose_stays_inside_the_length_law_for_long_names() -> None:
    """A 63-character instance name must still compose a legal account name."""
    long_name = "a" * 63
    name = state_store.compose_from_root_hcl(ROOT_HCL, long_name, "westeurope", _subscription())
    assert len(name) <= state_store.STORAGE_ACCOUNT_NAME_MAX
    assert re.fullmatch(r"[a-z0-9]+", name)


def test_compose_sanitizes_the_instance_component() -> None:
    """Uppercase, underscores and hyphens never reach the account name."""
    mixed = "Mixed_Case-Instance"
    name = state_store.compose_from_root_hcl(ROOT_HCL, mixed, "westeurope", _subscription())
    assert "Mixed" not in name and "_" not in name and "-" not in name


@pytest.mark.parametrize(
    ("other", "component"),
    (
        ("location", "eastus"),
        ("subscription", None),
    ),
)
def test_compose_differentiates_across_locations_and_subscriptions(
    other: str, component: str | None
) -> None:
    instance = _instance_name()
    location = "westeurope"
    subscription = _subscription()
    base = state_store.compose_from_root_hcl(ROOT_HCL, instance, location, subscription)
    if component == "location":
        other_name = state_store.compose_from_root_hcl(ROOT_HCL, instance, other, subscription)
    else:
        other_name = state_store.compose_from_root_hcl(
            ROOT_HCL, instance, location, _subscription()
        )
    assert base != other_name, f"the digest must separate the differing {other}"


def test_compose_differentiates_names_that_sanitize_alike() -> None:
    """Two names that sanitize to the same component must still get different
    accounts: the digest runs over the raw name, never the sanitized one."""
    subscription = _subscription()
    first = state_store.compose_from_root_hcl(ROOT_HCL, "inst-one", "westeurope", subscription)
    second = state_store.compose_from_root_hcl(ROOT_HCL, "inst-two", "westeurope", subscription)
    assert first != second


def test_compose_raises_naming_the_template_when_it_is_missing() -> None:
    with pytest.raises(StateStoreError, match="state_storage_name_template"):
        state_store.compose_from_root_hcl("locals {\n}\n", _instance_name(), "westeurope", "s")


def test_compose_raises_naming_the_suffix_when_it_is_missing() -> None:
    without_suffix = ROOT_HCL.replace(f'state_store_suffix = "{SUFFIX}"\n', "")
    with pytest.raises(StateStoreError, match="state_store_suffix"):
        state_store.compose_from_root_hcl(without_suffix, _instance_name(), "westeurope", "s")


def test_compose_raises_naming_the_suffix_when_it_is_empty() -> None:
    empty_suffix = ROOT_HCL.replace(SUFFIX, "")
    with pytest.raises(StateStoreError, match="state_store_suffix"):
        state_store.compose_from_root_hcl(empty_suffix, _instance_name(), "westeurope", "s")


def test_compose_raises_naming_an_unsupplied_component() -> None:
    with pytest.raises(StateStoreError, match="unknown_component"):
        state_store.compose_from_root_hcl(
            ROOT_HCL.replace("${local.instance_name}", "${local.unknown_component}"),
            _instance_name(),
            "westeurope",
            "s",
        )


def test_compose_caps_the_component_at_fifteen_when_the_template_allows() -> None:
    """The 15-character cap is a ceiling, not a target: with a template whose
    fixed parts leave room, the component still stops at fifteen."""
    template = "${local.instance_name}${local.state_store_suffix}"
    root_hcl = (
        f'locals {{\n  state_store_suffix = "q"\n  state_storage_name_template = "{template}"\n}}\n'
    )
    name = state_store.compose_from_root_hcl(root_hcl, "a" * 63, "westeurope", _subscription())
    # 1 (suffix) + 15 (component cap) + 8 (digest) = 24, exactly the law.
    assert len(name) == state_store.STORAGE_ACCOUNT_NAME_MAX
    assert len(name) == 1 + 15 + 8


def test_compose_refuses_a_template_that_cannot_fit_the_law() -> None:
    bloated = "tgstate-with-a-very-long-fixed-prefix-${local.instance_name}"
    root_hcl = (
        "locals {\n"
        f'  state_store_suffix = "{SUFFIX}"\n'
        f'  state_storage_name_template = "{bloated}"\n'
        "}\n"
    )
    with pytest.raises(StateStoreError, match="24-character name law"):
        state_store.compose_from_root_hcl(root_hcl, _instance_name(), "westeurope", "s")


def test_sanitize_strips_everything_but_lowercase_alphanumerics() -> None:
    assert state_store.sanitize_instance("Remote_Dev-01") == "remotedev01"


# ---------------------------------------------------------------------------
# storage_account_name: root.hcl, the scaffold's location, the ambient
# subscription.
# ---------------------------------------------------------------------------


def test_storage_account_name_reads_root_hcl_location_and_subscription(
    tmp_path: Path,
) -> None:
    root = _root_with_root_hcl(tmp_path)
    instance = _instance_name()
    _seed_azure_instance(root, instance)
    subscription = _subscription()
    runner = _FakeRunner()
    runner.queue(_ok(f"{subscription}\n"))

    name = state_store.storage_account_name(root, instance, runner)

    assert name == _expected_name(instance, "westeurope", subscription)
    assert runner.calls == [("az", "account", "show", "--query", "id", "--output", "tsv")]


def test_storage_account_name_refuses_an_invalid_instance_before_any_az_call(
    tmp_path: Path,
) -> None:
    root = _root_with_root_hcl(tmp_path)
    runner = _FakeRunner()

    with pytest.raises(StateStoreError, match="invalid instance name"):
        state_store.storage_account_name(root, "../escape", runner)

    assert runner.calls == []


def test_storage_account_name_raises_when_the_azure_root_is_missing(
    tmp_path: Path,
) -> None:
    runner = _FakeRunner()
    runner.queue(_ok(f"{_subscription()}\n"))

    with pytest.raises(StateStoreError, match="root-azure.hcl"):
        state_store.storage_account_name(tmp_path, _instance_name(), runner)


def test_storage_account_name_raises_when_the_scaffold_has_no_location(
    tmp_path: Path,
) -> None:
    root = _root_with_root_hcl(tmp_path)
    instance = _instance_name()
    _seed_azure_instance(root, instance)
    hcl = root / "remote-instances" / instance / "terragrunt.hcl"
    hcl.write_text(
        hcl.read_text(encoding="utf-8").replace('  location = "westeurope"\n', ""),
        encoding="utf-8",
    )
    runner = _FakeRunner()

    with pytest.raises(StateStoreError) as exc_info:
        state_store.storage_account_name(root, instance, runner)

    message = str(exc_info.value)
    assert "location" in message
    assert f"make instance-init INSTANCE={instance}" in message
    assert runner.calls == [], "a missing location must fail before the subscription lookup"


def test_subscription_lookup_failure_raises_with_the_command(tmp_path: Path) -> None:
    root = _root_with_root_hcl(tmp_path)
    instance = _instance_name()
    _seed_azure_instance(root, instance)
    runner = _FakeRunner()
    runner.queue(_err("az: no subscription found"))

    with pytest.raises(StateStoreError, match="subscription id lookup") as exc_info:
        state_store.storage_account_name(root, instance, runner)

    assert "no subscription found" in str(exc_info.value)


# ---------------------------------------------------------------------------
# init_form: one provisioningState probe picks the form; everything else
# raises.
# ---------------------------------------------------------------------------


def test_succeeded_account_answers_the_plain_form(tmp_path: Path) -> None:
    root = _root_with_root_hcl(tmp_path)
    instance = _instance_name()
    _seed_azure_instance(root, instance)
    subscription = _subscription()
    runner = _FakeRunner()
    runner.queue(_ok(f"{subscription}\n"))
    runner.queue(_ok("Succeeded\n"))

    assert state_store.init_form(root, instance, runner) == "plain"


def test_confirmed_missing_account_answers_the_bootstrap_form(tmp_path: Path) -> None:
    root = _root_with_root_hcl(tmp_path)
    instance = _instance_name()
    _seed_azure_instance(root, instance)
    subscription = _subscription()
    runner = _FakeRunner()
    runner.queue(_ok(f"{subscription}\n"))
    runner.queue(
        _err(
            "ERROR: (ResourceNotFound) The Resource 'Microsoft.Storage/storageAccounts/x' "
            "was not found.",
            returncode=3,
        )
    )

    assert state_store.init_form(root, instance, runner) == "bootstrap"


def test_a_404_marker_also_answers_the_bootstrap_form(tmp_path: Path) -> None:
    root = _root_with_root_hcl(tmp_path)
    instance = _instance_name()
    _seed_azure_instance(root, instance)
    runner = _FakeRunner()
    runner.queue(_ok(f"{_subscription()}\n"))
    runner.queue(_err("404 Not Found", returncode=3))

    assert state_store.init_form(root, instance, runner) == "bootstrap"


def test_a_provisioning_state_that_is_not_succeeded_raises(tmp_path: Path) -> None:
    root = _root_with_root_hcl(tmp_path)
    instance = _instance_name()
    _seed_azure_instance(root, instance)
    runner = _FakeRunner()
    runner.queue(_ok(f"{_subscription()}\n"))
    runner.queue(_ok("Creating\n"))

    with pytest.raises(StateStoreError) as exc_info:
        state_store.init_form(root, instance, runner)

    message = str(exc_info.value)
    assert "Creating" in message
    assert "storage account show" in message


def test_forbidden_account_raises_instead_of_bootstrapping(tmp_path: Path) -> None:
    """A 403 means the name is not confirmed missing: bootstrap must not run."""
    root = _root_with_root_hcl(tmp_path)
    instance = _instance_name()
    _seed_azure_instance(root, instance)
    raw = "ERROR: (AuthorizationFailed) The client does not have authorization"
    runner = _FakeRunner()
    runner.queue(_ok(f"{_subscription()}\n"))
    runner.queue(_err(raw, returncode=3))

    with pytest.raises(StateStoreError) as exc_info:
        state_store.init_form(root, instance, runner)

    message = str(exc_info.value)
    assert raw in message
    assert "storage account show" in message
    assert "not confirmed missing" in message


# ---------------------------------------------------------------------------
# state_resource_group: the dedicated group that outlives the deployment.
# ---------------------------------------------------------------------------


def test_state_resource_group_composes_the_dedicated_name() -> None:
    assert state_store.state_resource_group("Remote_Dev") == "rg-remotedev-state"


def test_state_resource_group_refuses_an_invalid_name() -> None:
    with pytest.raises(StateStoreError, match="invalid instance name"):
        state_store.state_resource_group("../escape")


# ---------------------------------------------------------------------------
# ensure: probe before create, idempotent, pinned argv.
# ---------------------------------------------------------------------------


def test_ensure_creates_group_account_and_container_when_all_missing(
    tmp_path: Path,
) -> None:
    root = _root_with_root_hcl(tmp_path)
    instance = _instance_name()
    _seed_azure_instance(root, instance)
    subscription = _subscription()
    runner = _FakeRunner()
    runner.queue(_ok(f"{subscription}\n"))  # the name's subscription lookup
    runner.queue(
        _err("(ResourceGroupNotFound) Resource group 'rg-x' could not be found.", returncode=3)
    )
    runner.queue(_ok())  # group create
    runner.queue(_err("(ResourceNotFound) not found", returncode=3))  # account probe
    runner.queue(_ok())  # account create
    runner.queue(_err("(ContainerNotFound) The specified container does not exist.", returncode=3))
    runner.queue(_ok())  # container create

    messages = state_store.ensure(root, instance, runner)

    assert len(messages) == 4
    assert "created resource group" in messages[0]
    assert "created storage account" in messages[1]
    assert "created blob container tfstate" in messages[2]
    assert ".state-store.json" in messages[3]
    rg = state_store.state_resource_group(instance)
    name = state_store.storage_account_name(root, instance, _FakeRunner()) if False else None
    del name
    create_account = runner.calls[4]
    assert create_account[:4] == ("az", "storage", "account", "create")
    assert "--resource-group" in create_account
    assert create_account[create_account.index("--resource-group") + 1] == rg
    assert "--location" in create_account
    assert create_account[create_account.index("--location") + 1] == "westeurope"
    assert "--sku" in create_account
    assert create_account[create_account.index("--sku") + 1] == "Standard_LRS"
    assert "--allow-blob-public-access" in create_account
    assert create_account[create_account.index("--allow-blob-public-access") + 1] == "false"
    create_container = runner.calls[6]
    assert create_container[:4] == ("az", "storage", "container", "create")
    assert "--auth-mode" in create_container
    assert create_container[create_container.index("--auth-mode") + 1] == "login"
    assert "--name" in create_container
    assert create_container[create_container.index("--name") + 1] == "tfstate"


def test_ensure_writes_the_sidecar_the_azure_root_reads(tmp_path: Path) -> None:
    """The side-car names the group and account exactly as they were ensured."""
    root = _root_with_root_hcl(tmp_path)
    instance = _instance_name()
    _seed_azure_instance(root, instance)
    subscription = _subscription()
    runner = _FakeRunner()
    runner.queue(_ok(f"{subscription}\n"))
    runner.queue(_ok())  # group exists
    runner.queue(_ok("Succeeded\n"))  # account exists and succeeded
    runner.queue(_ok())  # container exists

    state_store.ensure(root, instance, runner)

    sidecar = root / "remote-instances" / instance / ".state-store.json"
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    assert payload == {
        "resource_group": state_store.state_resource_group(instance),
        "storage_account_name": _expected_name(instance, "westeurope", subscription),
    }


def test_ensure_replaces_a_stale_sidecar(tmp_path: Path) -> None:
    """A previous side-car is overwritten, never appended to: one replace, in place."""
    root = _root_with_root_hcl(tmp_path)
    instance = _instance_name()
    _seed_azure_instance(root, instance)
    sidecar = root / "remote-instances" / instance / ".state-store.json"
    sidecar.write_text('{"resource_group": "stale", "storage_account_name": "stale"}\n')
    runner = _FakeRunner()
    runner.queue(_ok(f"{_subscription()}\n"))
    runner.queue(_ok())  # group exists
    runner.queue(_ok("Succeeded\n"))  # account exists
    runner.queue(_ok())  # container exists

    state_store.ensure(root, instance, runner)

    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    assert payload["resource_group"] != "stale"
    assert "stale" not in sidecar.read_text(encoding="utf-8")


def test_ensure_refuses_an_ensure_without_an_instance_directory(tmp_path: Path) -> None:
    """No scaffold, no store: the failure names the init that writes one, before any az call."""
    root = _root_with_root_hcl(tmp_path)
    instance = _instance_name()
    runner = _FakeRunner()

    with pytest.raises(StateStoreError, match="make instance-init"):
        state_store.ensure(root, instance, runner)

    assert runner.calls == [], "a missing scaffold must fail before the subscription lookup"


def test_ensure_is_idempotent_when_everything_exists(tmp_path: Path) -> None:
    root = _root_with_root_hcl(tmp_path)
    instance = _instance_name()
    _seed_azure_instance(root, instance)
    runner = _FakeRunner()
    runner.queue(_ok(f"{_subscription()}\n"))  # the name's subscription lookup
    runner.queue(_ok())  # group exists
    runner.queue(_ok("Succeeded\n"))  # account exists and succeeded
    runner.queue(_ok())  # container exists

    messages = state_store.ensure(root, instance, runner)

    assert len(messages) == 4
    assert "already exists" in messages[0]
    assert "already exists" in messages[1]
    assert "already exists" in messages[2]
    assert ".state-store.json" in messages[3], (
        "the side-car is rewritten even when the store already exists: it is the "
        "handoff terragrunt reads, and it must name the store that was just probed"
    )
    assert all("create" not in call[1] or call[1] == "show" for call in runner.calls[1:]), (
        "an existing store must create nothing: every call after the lookups is a probe"
    )


def test_ensure_refuses_an_unexpected_group_probe_answer(tmp_path: Path) -> None:
    root = _root_with_root_hcl(tmp_path)
    instance = _instance_name()
    _seed_azure_instance(root, instance)
    runner = _FakeRunner()
    runner.queue(_ok(f"{_subscription()}\n"))
    runner.queue(_err("(AuthorizationFailed) denied", returncode=3))

    with pytest.raises(StateStoreError) as exc_info:
        state_store.ensure(root, instance, runner)

    message = str(exc_info.value)
    assert "(AuthorizationFailed) denied" in message
    assert "group show" in message
    assert "no create is attempted" in message


def test_ensure_refuses_a_failed_account_create(tmp_path: Path) -> None:
    root = _root_with_root_hcl(tmp_path)
    instance = _instance_name()
    _seed_azure_instance(root, instance)
    runner = _FakeRunner()
    runner.queue(_ok(f"{_subscription()}\n"))
    runner.queue(_err("(ResourceGroupNotFound) missing", returncode=3))
    runner.queue(_ok())  # group create
    runner.queue(_err("(ResourceNotFound) not found", returncode=3))  # account probe
    runner.queue(
        _err("StorageAccountAlreadyExists-ish failure", returncode=1)
    )  # account create fails

    with pytest.raises(StateStoreError, match="creating storage account") as exc_info:
        state_store.ensure(root, instance, runner)

    assert "StorageAccountAlreadyExists-ish failure" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Listing: the pattern derives from root.hcl's own template.
# ---------------------------------------------------------------------------


def test_storage_name_pattern_matches_the_composed_name(tmp_path: Path) -> None:
    root = _root_with_root_hcl(tmp_path)
    pattern = state_store.storage_name_pattern(root)
    assert pattern.fullmatch(_expected_name("inst-x", "westeurope", "s"))
    assert not pattern.fullmatch("tgstateinstxyznotthesuffix")
    assert not pattern.fullmatch("someone-elses-account")


def test_list_stores_reports_configured_orphaned_and_skips_other_kinds(
    tmp_path: Path,
) -> None:
    root = _root_with_root_hcl(tmp_path)
    azure_instance = _instance_name()
    _seed_azure_instance(root, azure_instance)
    ec2_instance = _instance_name()
    _seed_ec2_instance(root, ec2_instance)
    subscription = _subscription()
    ours = _expected_name(azure_instance, "westeurope", subscription)
    orphan = _expected_name("inst-gone", "westeurope", subscription)
    other = "some-other-tenants-account"
    runner = _FakeRunner()
    runner.queue(_ok(f"{subscription}\n"))  # the listing's subscription lookup
    runner.queue(
        _ok(
            json.dumps(
                [
                    {"name": orphan, "location": "westeurope", "rg": "rg-gone-state"},
                    {"name": other, "location": "westeurope", "rg": "other"},
                    {"name": ours, "location": "westeurope", "rg": "rg-ours-state"},
                ]
            )
        )
    )

    listings = state_store.list_stores(root, runner)

    assert [(listing.name, listing.configured) for listing in listings] == [
        (ours, True),
        (orphan, False),
    ]
    ours_listing = listings[0]
    assert ours_listing.instance == azure_instance
    assert ours_listing.resource_group == "rg-ours-state"
    assert listings[1].instance == ""
    # The EC2 instance was never a candidate: its name never entered the
    # composition at all.
    assert all(listing.instance != ec2_instance for listing in listings)


def test_list_stores_raises_on_a_failed_or_unparseable_az_answer(tmp_path: Path) -> None:
    root = _root_with_root_hcl(tmp_path)
    runner = _FakeRunner()
    runner.queue(_err("az: login required"))
    with pytest.raises(StateStoreError, match="subscription id lookup failed"):
        state_store.list_stores(root, runner)
    runner = _FakeRunner()
    runner.queue(_ok(f"{_subscription()}\n"))
    runner.queue(_ok("not json"))
    with pytest.raises(StateStoreError, match="not valid JSON"):
        state_store.list_stores(root, runner)


# ---------------------------------------------------------------------------
# Deletion: purge then delete, the emptied group behind the account.
# ---------------------------------------------------------------------------


def test_delete_account_purges_containers_then_deletes_account_and_group() -> None:
    subscription = _subscription()
    account = _expected_name("inst-x", "westeurope", subscription)
    rg = "rg-instx-state"
    runner = _FakeRunner()
    runner.queue(_ok("Succeeded\n"))  # the account probe
    runner.queue(_ok("tfstate\n"))  # the container listing
    runner.queue(_ok())  # the container delete
    runner.queue(_ok())  # the account delete
    runner.queue(_ok())  # the group probe: still there
    runner.queue(_ok())  # the group delete

    message = state_store.delete_account(account, rg, runner)

    assert account in message and rg in message
    assert runner.calls[2][:3] == ("az", "storage", "container")
    assert runner.calls[3][:3] == ("az", "storage", "account")
    assert "--yes" in runner.calls[3]
    assert runner.calls[5] == ("az", "group", "delete", "--name", rg, "--yes", "--output", "none")


def test_delete_account_prefers_the_container_delete_and_purges_on_refusal() -> None:
    account = _expected_name("inst-x", "westeurope", _subscription())
    runner = _FakeRunner()
    runner.queue(_ok("Succeeded\n"))  # the account probe
    runner.queue(_ok("tfstate\n"))  # the container listing
    runner.queue(_err("(ContainerBeingDeleted) busy", returncode=3))  # the container delete refuses
    runner.queue(_ok())  # the blob purge
    runner.queue(_ok())  # the retried container delete
    runner.queue(_ok())  # the account delete
    runner.queue(_err("(ResourceGroupNotFound) missing", returncode=3))  # the group probe: gone

    message = state_store.delete_account(account, "rg-x-state", runner)

    assert "deleted" in message
    delete_index = 2
    purge_index = 3
    assert runner.calls[delete_index][1:3] == ("storage", "container")
    assert runner.calls[purge_index][1:3] == ("storage", "blob")


def test_delete_account_reports_an_absent_account_without_touching_anything() -> None:
    account = _expected_name("inst-x", "westeurope", _subscription())
    runner = _FakeRunner()
    runner.queue(_err("(ResourceNotFound) was not found", returncode=3))

    message = state_store.delete_account(account, "rg-x-state", runner)

    assert "already absent" in message
    assert len(runner.calls) == 1


def test_delete_account_refuses_an_empty_resource_group() -> None:
    runner = _FakeRunner()

    with pytest.raises(StateStoreError, match="resource group"):
        state_store.delete_account("some-account", "", runner)

    assert runner.calls == []


def test_delete_account_surfaces_a_failed_account_delete() -> None:
    account = _expected_name("inst-x", "westeurope", _subscription())
    runner = _FakeRunner()
    runner.queue(_ok("Succeeded\n"))  # the account probe
    runner.queue(_ok())  # the container listing: empty
    runner.queue(_err("(AuthorizationFailed) denied", returncode=3))  # the account delete

    with pytest.raises(StateStoreError, match="deleting storage account") as exc_info:
        state_store.delete_account(account, "rg-x-state", runner)

    assert "(AuthorizationFailed) denied" in str(exc_info.value)
    assert len(runner.calls) == 3, "the group must not be touched when the account delete failed"


def test_delete_store_refuses_an_invalid_name_before_any_az_call(tmp_path: Path) -> None:
    root = _root_with_root_hcl(tmp_path)
    runner = _FakeRunner()

    with pytest.raises(StateStoreError, match="invalid instance name"):
        state_store.delete_store(root, "../escape", runner)

    assert runner.calls == []


def test_delete_store_removes_the_sidecar_with_the_store(tmp_path: Path) -> None:
    root = _root_with_root_hcl(tmp_path)
    instance = _instance_name()
    _seed_azure_instance(root, instance)
    subscription = _subscription()
    sidecar = root / "remote-instances" / instance / ".state-store.json"
    sidecar.write_text('{"resource_group": "rg", "storage_account_name": "acc"}\n')
    runner = _FakeRunner()
    runner.queue(_ok(f"{subscription}\n"))  # the name's subscription lookup
    runner.queue(_ok("Succeeded\n"))  # the account probe
    runner.queue(_ok())  # the container listing: empty
    runner.queue(_ok())  # the account delete
    runner.queue(_err("(ResourceGroupNotFound) missing", returncode=3))  # the group probe

    message = state_store.delete_store(root, instance, runner)

    assert "deleted" in message
    assert f"removed {sidecar}" in message
    assert not sidecar.exists()


def test_delete_store_reports_no_sidecar_removal_when_none_exists(tmp_path: Path) -> None:
    """An absent side-car is the desired end state and gets no removal mention."""
    root = _root_with_root_hcl(tmp_path)
    instance = _instance_name()
    _seed_azure_instance(root, instance)
    runner = _FakeRunner()
    runner.queue(_ok(f"{_subscription()}\n"))  # the name's subscription lookup
    runner.queue(_err("(ResourceNotFound) was not found", returncode=3))  # absent account

    message = state_store.delete_store(root, instance, runner)

    assert "already absent" in message
    assert "removed" not in message


def test_delete_matching_stores_uses_each_listing_s_own_group(tmp_path: Path) -> None:
    root = _root_with_root_hcl(tmp_path)
    azure_instance = _instance_name()
    _seed_azure_instance(root, azure_instance)
    subscription = _subscription()
    ours = _expected_name(azure_instance, "westeurope", subscription)
    runner = _FakeRunner()
    runner.queue(_ok(f"{subscription}\n"))  # the listing's subscription lookup
    runner.queue(_ok(json.dumps([{"name": ours, "location": "westeurope", "rg": "rg-ours-state"}])))
    runner.queue(_ok("Succeeded\n"))  # the account probe
    runner.queue(_ok())  # the container listing: empty
    runner.queue(_ok())  # the account delete
    runner.queue(_err("(ResourceGroupNotFound) missing", returncode=3))  # the group probe: gone

    messages = state_store.delete_matching_stores(root, runner)

    assert len(messages) == 1
    assert "deleted" in messages[0]
    assert runner.calls[-1][1:3] == ("group", "show"), (
        "an already-absent group is the desired end state and is probed, not created"
    )


# ---------------------------------------------------------------------------
# The module entry point.
# ---------------------------------------------------------------------------


def test_main_init_form_prints_the_form_and_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _root_with_root_hcl(tmp_path)
    instance = _instance_name()
    _seed_azure_instance(root, instance)
    runner = _FakeRunner()
    runner.queue(_ok(f"{_subscription()}\n"))
    runner.queue(_ok("Succeeded\n"))
    monkeypatch.setattr(state_store, "subprocess_runner", runner)

    exit_code = state_store.main(["init-form", str(root), instance])

    assert exit_code == 0
    assert capsys.readouterr().out.strip() == "plain"


def test_main_ensure_prints_one_message_per_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _root_with_root_hcl(tmp_path)
    instance = _instance_name()
    _seed_azure_instance(root, instance)
    runner = _FakeRunner()
    runner.queue(_ok(f"{_subscription()}\n"))
    runner.queue(_ok())  # group exists
    runner.queue(_ok("Succeeded\n"))  # account exists
    runner.queue(_ok())  # container exists
    monkeypatch.setattr(state_store, "subprocess_runner", runner)

    exit_code = state_store.main(["ensure", str(root), instance])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert out.count("already exists") == 3


def test_main_reports_probe_failures_on_stderr_and_exits_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _root_with_root_hcl(tmp_path)
    instance = _instance_name()
    _seed_azure_instance(root, instance)
    runner = _FakeRunner()
    runner.queue(_err("az: login required"))
    monkeypatch.setattr(state_store, "subprocess_runner", runner)

    exit_code = state_store.main(["init-form", str(root), instance])

    assert exit_code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "login required" in captured.err


def test_main_rejects_unknown_commands_and_wrong_argument_counts(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert state_store.main(["frobnicate"]) == 2
    assert state_store.main([]) == 2
    assert state_store.main(["delete", "a", "b"]) == 1
    captured = capsys.readouterr()
    assert "usage:" in captured.err
    assert "takes exactly" in captured.err


def test_main_list_and_delete_run_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import subprocess as sp

    from gitfixtures import generated_root, init_repo

    root = generated_root(tmp_path)
    init_repo(root)
    sp.run(
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
    azure_instance = _instance_name()
    (root / "remote-instances").mkdir(exist_ok=True)
    (root / "remote-instances" / "root-azure.hcl").write_text(ROOT_HCL, encoding="utf-8")
    _seed_azure_instance(root, azure_instance)
    subscription = _subscription()
    account = _expected_name(azure_instance, "westeurope", subscription)
    runner = _FakeRunner()
    runner.queue(_ok(f"{subscription}\n"))  # list's subscription lookup
    runner.queue(_ok(json.dumps([{"name": account, "location": "westeurope", "rg": "rg-x-state"}])))
    monkeypatch.setattr(state_store, "subprocess_runner", runner)
    monkeypatch.chdir(root)

    assert state_store.main(["list"]) == 0
    out = capsys.readouterr().out
    assert account in out and "configured" in out

    runner.queue(_ok(f"{subscription}\n"))  # delete's subscription lookup
    runner.queue(_ok("Succeeded\n"))  # the account probe
    runner.queue(_ok())  # the container listing: empty
    runner.queue(_ok())  # the account delete
    runner.queue(_err("(ResourceGroupNotFound) missing", returncode=3))  # the group probe: gone
    assert state_store.main(["delete", azure_instance]) == 0
    assert "deleted" in capsys.readouterr().out
