# Paths whose SSH references are owned by a later cutover task

`tests/test_ssh_removal.py` fails when any tracked file mentions one of the
identifiers in `removed-ssh-tokens.txt`, except the paths listed here. Each
line is one repo-relative path. A later task empties its own rows; the file is
deleted by the last of them, at which point the scanner covers the whole
repository with no exceptions.

Listing a path here is a deliberate, temporary carve-out, not a permanent
allowance: the scanner still fails if a path appears here that no longer
contains a reference, so a stale exception cannot outlive the reference it was
written for.

## Owned by E7-F1-S1-T2 (configuration and schema)

- shell.env.example

## Owned by the E7-F2 documentation tasks

- .devcontainer/remote-docker/README.md
- docs/devcontainer.md
- docs/environment-files.md
- docs/mac-setup-prompt.md

## Explanatory references in code and tests

These name the removed scripts to explain why the current design exists, or
assert their absence. They are reviewed with the tasks above and reworded or
kept deliberately.

- .claude/plugins/devcontainer/scripts/devcontainer_config/hostprobe.py
- .claude/plugins/devcontainer/scripts/devcontainer_config/transport.py
- tests/test_docs_environment_files.py
- tests/test_makefile_contract.py
- tests/test_transport.py
