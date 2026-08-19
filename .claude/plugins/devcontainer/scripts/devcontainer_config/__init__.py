"""Devcontainer configuration: one module per concern (spec Section 4.5).

`repo` is the module set's foundation: it discovers the repository root and
derives every path -- private files, their examples, the in-container
workspace -- from it. Later modules (`answers`, `render`, `verify`, and so on)
take that root as a parameter instead of discovering it themselves, which is
what lets each of them be pointed at a temporary directory in a test. This
file exists only to make the directory importable as a package; each
module's public surface is imported from the module itself
(`from devcontainer_config.repo import find_root`), not re-exported here, so
adding a module never requires editing this file.
"""

from __future__ import annotations
