"""Shared git-repository test primitives (spec Section 4.6 test suite).

Consumed by `tests/test_lint_secrets_cli.py`, `tests/test_repo.py`,
`tests/test_cli.py` and `tests/test_secrets_range.py`, all of which build
real, disposable git repositories under `tmp_path` by shelling out to the
actual `git` binary rather than mocking one, so the primitives that do that
work belong in exactly one place instead of being copied into each file.
That place is this module, `tests/gitfixtures.py`, and deliberately not
`tests/conftest.py`, because that path is already claimed by another work
unit's unrelated answers-payload builders. pytest's default prepend import
mode inserts `tests/` on `sys.path`, which makes any sibling
module under `tests/` importable by name, so no shared-fixture module needs
to contest that path.

The `devcontainer_config` import is deferred into the function bodies below
(`import_cli` / `import_secrets`) instead of done once at module scope, for
the reason every consumer's own module docstring documents: the TDD RED gate
stashes a unit's production-source files and re-runs a single named test
node, and a module-level `from devcontainer_config... import ...` here would
fail COLLECTION for every consumer that imports this module, instead of
failing only the one test for the real reason.

No credential-shaped literal is ever stored pre-assembled in this file.
`credential_line` builds a positive sample at run time from
`devcontainer_config.secrets.SAMPLE_PREFIXES` plus a `uuid.uuid4()` suffix,
the same discipline `tests/test_secrets.py` documents for the scanner's own
case table, so this module itself never becomes something a future
`make lint-secrets` run would flag.
"""

from __future__ import annotations

import importlib
import subprocess
import uuid
from pathlib import Path
from types import ModuleType

import pytest


def import_cli() -> ModuleType:
    """Import devcontainer_config.cli from inside a function body.

    See the module docstring for why this is not a module-level import.
    """
    return importlib.import_module("devcontainer_config.cli")


def import_secrets() -> ModuleType:
    """Import devcontainer_config.secrets from inside a function body.

    See the module docstring for why this is not a module-level import.
    """
    return importlib.import_module("devcontainer_config.secrets")


def generated_root(tmp_path: Path) -> Path:
    """A tmp_path subdirectory whose name is generated, never hard-coded."""
    root = tmp_path / f"checkout-{uuid.uuid4().hex}"
    root.mkdir()
    return root


def init_repo(root: Path) -> None:
    """A minimal, disposable git repository at root, ready to stage content."""
    subprocess.run(["git", "init", str(root)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(root), "config", "user.email", "devbench-test@example.com"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(root), "config", "user.name", "devbench-test"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(root), "config", "commit.gpgsign", "false"],
        check=True,
        capture_output=True,
    )


def stage_text(root: Path, relative: str, content: str) -> Path:
    """Write `content` to `relative` under `root` and `git add` it; the absolute path."""
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", relative], check=True, capture_output=True)
    return path


def stage_bytes(root: Path, relative: str, content: bytes) -> Path:
    """Write raw `content` to `relative` under `root` and `git add` it; the absolute path."""
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    subprocess.run(["git", "-C", str(root), "add", relative], check=True, capture_output=True)
    return path


def commit_text(root: Path, relative: str, content: str, message: str) -> str:
    """Write `content` to `relative`, stage it and commit it; the new commit's SHA."""
    stage_text(root, relative, content)
    subprocess.run(
        ["git", "-C", str(root), "commit", "-m", message], check=True, capture_output=True
    )
    return rev_parse(root, "HEAD")


def commit_bytes(root: Path, relative: str, content: bytes, message: str) -> str:
    """Write raw `content` to `relative`, stage it and commit it; the new commit's SHA."""
    stage_bytes(root, relative, content)
    subprocess.run(
        ["git", "-C", str(root), "commit", "-m", message], check=True, capture_output=True
    )
    return rev_parse(root, "HEAD")


def rev_parse(root: Path, ref: str) -> str:
    """Resolve `ref` to its full commit SHA inside the repository at `root`."""
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", ref],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def credential_line() -> str:
    """A single line that the AWS-access-key-id detector matches, built at run time.

    Composed from `SAMPLE_PREFIXES`, never stored pre-assembled: see the
    module docstring for why.
    """
    secrets = import_secrets()
    prefix = secrets.SAMPLE_PREFIXES["aws-access-key-id"][0]
    suffix = uuid.uuid4().hex.upper()[:16]
    return f"CREDENTIAL={prefix}{suffix}\n"


def run_cli(monkeypatch: pytest.MonkeyPatch, root: Path, args: list[str]) -> int:
    """Run `devcontainer_config.cli.main(args)` with cwd set to `root`; the exit code."""
    cli = import_cli()
    monkeypatch.chdir(root)
    with pytest.raises(SystemExit) as exc_info:
        cli.main(args)
    code = exc_info.value.code
    if not isinstance(code, int):
        raise AssertionError(f"cli.main exited with a non-integer code: {code!r}")
    return code
