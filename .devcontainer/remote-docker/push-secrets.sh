#!/usr/bin/env bash

set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

rd_load_config
rd_require_remote_config
rd_require_cmd aws "Install: https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html"
rd_check_aws_auth

REPO_ROOT="$(cd "${RD_DIR}/../.." && pwd)"
PROJECT_NAME="${PROJECT_NAME:-$(basename "$REPO_ROOT")}"
SHELL_ENV_SOURCE="${SHELL_ENV_SOURCE:-${REPO_ROOT}/shell.env}"
PROFILE_MAP_SOURCE="${PROFILE_MAP_SOURCE:-${REPO_ROOT}/.devcontainer/aws-profile-map.json}"
# Resolved, not derived. This script publishes secrets, so addressing the
# wrong instance writes them where another instance will read them. The
# resolver runs before any AWS call for that reason.
rd_resolve_instance
SSM_PREFIX="${PARAMETER_PREFIX%/}"

[ -f "$SHELL_ENV_SOURCE" ] || rd_die "shell.env not found at ${SHELL_ENV_SOURCE} (run 'cdevcontainer setup-devcontainer' first)"
[ -f "$PROFILE_MAP_SOURCE" ] || rd_die "aws-profile-map.json not found at ${PROFILE_MAP_SOURCE}"

REMOTE_SHELL_ENV=$(sed \
  -e "s|^export HOST_PROXY=.*|export HOST_PROXY='false'|" \
  -e "s|^export BASH_ENV=.*|export BASH_ENV='/workspaces/${PROJECT_NAME}/shell.env'|" \
  -e '/^export HOST_PROXY_URL=/d' \
  -e '/^export HTTP_PROXY=/d' \
  -e '/^export HTTPS_PROXY=/d' \
  -e '/^export http_proxy=/d' \
  -e '/^export https_proxy=/d' \
  -e '/^export NO_PROXY=/d' \
  -e '/^export no_proxy=/d' \
  -e '/^export PATH=.*\.asdf/d' \
  -e '/^export PATH=.*\.localscripts/d' \
  "$SHELL_ENV_SOURCE")
[ -n "$REMOTE_SHELL_ENV" ] || rd_die "transformed shell.env is empty"

rd_log "Publishing ${SSM_PREFIX}/shell.env (SecureString)..."
rd_aws ssm put-parameter \
  --profile "$REMOTE_AWS_PROFILE" --region "$REMOTE_AWS_REGION" \
  --name "${SSM_PREFIX}/shell.env" \
  --type SecureString \
  --value "$REMOTE_SHELL_ENV" \
  --overwrite > /dev/null

rd_log "Publishing ${SSM_PREFIX}/aws-profile-map.json (String)..."
rd_aws ssm put-parameter \
  --profile "$REMOTE_AWS_PROFILE" --region "$REMOTE_AWS_REGION" \
  --name "${SSM_PREFIX}/aws-profile-map.json" \
  --type String \
  --value "$(cat "$PROFILE_MAP_SOURCE")" \
  --overwrite > /dev/null

rd_ok "Secrets published for project '${PROJECT_NAME}'. Remote workspaces of this project can now bootstrap."
