#!/usr/bin/env bash
# postCreateCommand entry point. Runs as the container user, before the
# provisioning script, and exists to do the one thing that must happen first:
# make shell.env available.
#
# Locally, cdevcontainer generates shell.env and aws-profile-map.json (both
# gitignored). A remote clone-in-volume workspace starts without them, so they
# are fetched from Parameter Store using the instance role. IMDSv2, on an
# instance launched with hop-limit 2 so containers can reach it.
#
# Every value is overridable from the environment.

set -euo pipefail

: "${DEVCONTAINER_USER:=vscode}"
: "${DEVCONTAINER_SSM_PREFIX:=/devcontainer/$(basename "$(pwd)")}"
: "${DEVCONTAINER_IMDS_ENDPOINT:=http://169.254.169.254}"
: "${DEVCONTAINER_IMDS_TIMEOUT_SECONDS:=2}"
: "${DEVCONTAINER_IMDS_TOKEN_TTL_SECONDS:=60}"
: "${DEVCONTAINER_SECRET_UMASK:=077}"

WORK_DIR="$(pwd)"
SHELL_ENV="${WORK_DIR}/shell.env"
PROFILE_MAP="${WORK_DIR}/.devcontainer/aws-profile-map.json"
POSTCREATE="${WORK_DIR}/.devcontainer/.devcontainer.postcreate.sh"

# shellcheck source=devcontainer-functions.sh
source "${WORK_DIR}/.devcontainer/devcontainer-functions.sh"

fail_bootstrap() {
  log_error "shell.env not found in ${WORK_DIR} and Parameter Store bootstrap failed: $1"
  log_error "  Local dev:  run 'cdevcontainer setup-devcontainer' to generate shell.env"
  log_error "  Remote dev: run 'make push-secrets' from your machine, then rebuild"
  exit 1
}

imds_token() {
  curl -sf -X PUT "${DEVCONTAINER_IMDS_ENDPOINT}/latest/api/token" \
    -H "X-aws-ec2-metadata-token-ttl-seconds: ${DEVCONTAINER_IMDS_TOKEN_TTL_SECONDS}" \
    --connect-timeout "${DEVCONTAINER_IMDS_TIMEOUT_SECONDS}" 2> /dev/null || true
}

imds_get() {
  curl -sf -H "X-aws-ec2-metadata-token: $1" \
    --connect-timeout "${DEVCONTAINER_IMDS_TIMEOUT_SECONDS}" \
    "${DEVCONTAINER_IMDS_ENDPOINT}/latest/meta-data/$2"
}

# Write one Parameter Store value to a file, failing if it arrives empty.
fetch_parameter() {
  local region="$1" name="$2" destination="$3" decrypt="${4:-}"
  local args=(--region "${region}" --name "${DEVCONTAINER_SSM_PREFIX}/${name}"
    --query 'Parameter.Value' --output text)
  [ -z "${decrypt}" ] || args+=(--with-decryption)

  aws ssm get-parameter "${args[@]}" > "${destination}" \
    || fail_bootstrap "could not fetch ${DEVCONTAINER_SSM_PREFIX}/${name} (was 'make push-secrets' run for this project?)"
  [ -s "${destination}" ] || fail_bootstrap "fetched ${name} is empty"
}

bootstrap_secrets() {
  if [ -f "${SHELL_ENV}" ]; then
    log_info "Using existing shell.env"
    return 0
  fi
  log_info "shell.env not found, bootstrapping from Parameter Store (${DEVCONTAINER_SSM_PREFIX})"

  command -v aws > /dev/null 2>&1 || fail_bootstrap "aws CLI is not installed"

  local token region
  token="$(imds_token)"
  [ -n "${token}" ] || fail_bootstrap "instance metadata (IMDSv2) unreachable, not on an instance-role host"
  region="$(imds_get "${token}" "placement/region")" \
    || fail_bootstrap "could not read region from instance metadata"

  local previous_umask
  previous_umask="$(umask)"
  umask "${DEVCONTAINER_SECRET_UMASK}"
  fetch_parameter "${region}" "shell.env" "${SHELL_ENV}" decrypt
  [ -f "${PROFILE_MAP}" ] || fetch_parameter "${region}" "aws-profile-map.json" "${PROFILE_MAP}"
  umask "${previous_umask}"

  log_success "Bootstrapped shell.env and aws-profile-map.json from Parameter Store"
}

# Git checkouts on Windows hosts can carry CRLF, which breaks these scripts.
normalize_line_endings() {
  is_wsl || return 0
  log_info "WSL detected, normalising line endings under .devcontainer"
  find .devcontainer -type f -exec sed -i "s/\r$//" {} +
  python3 .devcontainer/fix-line-endings.py
}

main() {
  bootstrap_secrets

  # shellcheck source=/dev/null
  source "${SHELL_ENV}"
  log_info "HOST_PROXY=${HOST_PROXY:-false} HTTP_PROXY=${HTTP_PROXY:-unset}"
  if [ "${HOST_PROXY:-false}" = "true" ] && [ -z "${HTTP_PROXY:-}" ]; then
    exit_with_error "HOST_PROXY=true but HTTP_PROXY is not set after sourcing shell.env"
  fi

  normalize_line_endings

  # sudo replaces PATH with secure_path, discarding the image ENV that puts
  # feature-installed tools on the path. Pass it through so the provisioning
  # script can tell what the container user actually has available.
  sudo -E env "CONTAINER_USER_PATH=${PATH}" bash "${POSTCREATE}" "${DEVCONTAINER_USER}"

  log_success "Setup complete. View logs: cat /tmp/devcontainer-setup.log"
}

main "$@"
