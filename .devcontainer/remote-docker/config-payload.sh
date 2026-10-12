#!/usr/bin/env bash
#
# The remote flavor of this repository's configuration payload, rendered by
# one function wherever configuration reaches a remote container.
#
# Two channels carry that payload, and both must apply byte-identical
# transform rules: `push-secrets.sh` publishes it to Parameter Store (the
# EC2 engine's config channel, fetched by postCreate through the instance
# role), and `container.sh` seeds it straight into the workspace volume (the
# SSH engine's, which has no Parameter Store behind it at all). The rules
# live here exactly once; both scripts source this file, so a changed rule
# moves everywhere at once instead of drifting into two flavors that place
# different environments on the same engine.

# Sourced AFTER lib.sh, whose rd_die carries every failure below, and by a
# caller that has set SHELL_ENV_SOURCE, PROFILE_MAP_SOURCE and PROJECT_NAME
# (both consumers do, right after rd_load_config).

# The in-container workspace root the BASH_ENV rewrite points at. The same
# variable, with the same default, that container.sh derives its
# CONTAINER_WORKSPACE from, so the BASH_ENV path and the seeded file's real
# path agree by construction; a checkout that moves the workspace root moves
# both at once.
: "${CONTAINER_WORKSPACES_ROOT:=/workspaces}"

# Render the transformed payload under `dest_dir`:
#
#   dest_dir/shell.env                             the transformed shell.env
#   dest_dir/.devcontainer/aws-profile-map.json    copied through byte-for-byte
#
# The transform is the remote flavor push-secrets.sh has always published:
# HOST_PROXY forced off and the proxy exports dropped (no laptop tinyproxy
# reaches a remote engine), BASH_ENV repointed at the remote workspace copy,
# host-specific PATH prepends (.asdf, .localscripts) dropped, and comment
# and blank lines stripped. Everything else passes through untouched.
write_config_payload() {
  local dest_dir="$1"
  [ -n "${PROJECT_NAME:-}" ] || rd_die "PROJECT_NAME must be set before the config payload is rendered"
  [ -f "${SHELL_ENV_SOURCE:-}" ] || rd_die "shell.env not found at ${SHELL_ENV_SOURCE:-<unset>} (run 'make init' first)"
  [ -f "${PROFILE_MAP_SOURCE:-}" ] || rd_die "aws-profile-map.json not found at ${PROFILE_MAP_SOURCE:-<unset>}"
  mkdir -p "${dest_dir}/.devcontainer"
  sed \
    -e "s|^export HOST_PROXY=.*|export HOST_PROXY='false'|" \
    -e "s|^export BASH_ENV=.*|export BASH_ENV='${CONTAINER_WORKSPACES_ROOT}/${PROJECT_NAME}/shell.env'|" \
    -e '/^export HOST_PROXY_URL=/d' \
    -e '/^export HTTP_PROXY=/d' \
    -e '/^export HTTPS_PROXY=/d' \
    -e '/^export http_proxy=/d' \
    -e '/^export https_proxy=/d' \
    -e '/^export NO_PROXY=/d' \
    -e '/^export no_proxy=/d' \
    -e '/^export PATH=.*\.asdf/d' \
    -e '/^export PATH=.*\.localscripts/d' \
    -e '/^[[:space:]]*#/d' \
    -e '/^[[:space:]]*$/d' \
    "$SHELL_ENV_SOURCE" > "${dest_dir}/shell.env"
  [ -s "${dest_dir}/shell.env" ] || rd_die "transformed shell.env is empty"
  # Copied, not re-rendered: the profile map is consumed verbatim, and a
  # copy is the only transform guaranteed to be no transform.
  cp "$PROFILE_MAP_SOURCE" "${dest_dir}/.devcontainer/aws-profile-map.json"
}
