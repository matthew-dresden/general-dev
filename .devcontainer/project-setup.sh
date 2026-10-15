#!/usr/bin/env bash

set -euo pipefail

log_info "Running project-specific setup..."

# This script is the per-project seam the postcreate runs after provisioning.
# Everything below is commented: log-only by default, and every example here
# has its durable home in .devcontainer/project-setup.local.sh.example, the
# gitignored per-user file the postcreate sources in this same environment
# context after this script -- put overrides there, never in this committed
# file.
#
# Per-user overrides this seam supports, with the shape each takes:
#
# - Model/default-agent: edit the installed opencode config
#   (~/.config/opencode/opencode.json) after the postcreate resolved the
#   provider toggles, e.g.:
#
#     jq '.default_agent = "build-zai-max-glm-5.3-flash"' \
#       ~/.config/opencode/opencode.json > /tmp/oc.json \
#       && mv /tmp/oc.json ~/.config/opencode/opencode.json
#
#   The value must name a generated agent: see
#   ~/.config/opencode/lib/generated-agents.json for the set.
#
# - MCP toggles: enable/disable one MCP server for this user only, e.g.:
#
#     jq '.mcp.playwright.enabled = false' \
#       ~/.config/opencode/opencode.json > /tmp/oc.json \
#       && mv /tmp/oc.json ~/.config/opencode/opencode.json
#
# - Provider API keys: do NOT set them here. Keys reach opencode only through
#   the {env:...} interpolations the shipped config carries, and the
#   hostcreds startup block exports the values; add the key to
#   .devcontainer/hostcreds.map.json instead and run 'make creds-init' /
#   'make push-creds'. See project-setup.local.sh.example.
#
# - gws state restore: the gws CLI keeps its OAuth refresh token in
#   ~/.config/gws; a fresh container starts without it. Restore it from a
#   backup you keep outside the repo, e.g.:
#
#     mkdir -p ~/.config/gws
#     cp /path/to/your/gws-backup/* ~/.config/gws/
#
log_info "Add project specific setup commands here!"

log_info "Project-specific setup complete"
