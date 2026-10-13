#!/usr/bin/env bash

set -euo pipefail

: "${DEVCONTAINER_USER:=vscode}"
: "${DEVCONTAINER_SSM_PREFIX:=/devcontainer/$(basename "$(pwd)")}"
: "${DEVCONTAINER_ENGINE_KIND:=}"
: "${DEVCONTAINER_VAULT_NAME:=}"
: "${DEVCONTAINER_IMDS_ENDPOINT:=http://169.254.169.254}"
: "${DEVCONTAINER_IMDS_TIMEOUT_SECONDS:=2}"
: "${DEVCONTAINER_IMDS_TOKEN_TTL_SECONDS:=60}"
: "${DEVCONTAINER_SECRET_UMASK:=077}"

# The azure bootstrap's platform constants, each overridable the same way
# the aws IMDS ones above are. The identity endpoint is the fixed link-local
# address every azure VM answers on; the resource is the audience a Key
# Vault access token is minted for; the API versions pin the request shapes
# the curl calls below compose.
: "${DEVCONTAINER_AZURE_IMDS_ENDPOINT:=http://169.254.169.254}"
: "${DEVCONTAINER_AZURE_IMDS_API_VERSION:=2018-02-01}"
: "${DEVCONTAINER_KEYVAULT_RESOURCE:=https://vault.azure.net}"
: "${DEVCONTAINER_KEYVAULT_API_VERSION:=7.4}"

WORK_DIR="$(pwd)"
SHELL_ENV="${WORK_DIR}/shell.env"
PROFILE_MAP="${WORK_DIR}/.devcontainer/aws-profile-map.json"
POSTCREATE="${WORK_DIR}/.devcontainer/.devcontainer.postcreate.sh"

# shellcheck source=devcontainer-functions.sh
source "${WORK_DIR}/.devcontainer/devcontainer-functions.sh"

fail_bootstrap() {
  log_error "shell.env not found in ${WORK_DIR} and Parameter Store bootstrap failed: $1"
  log_error "  Local dev:  run 'make init' to generate shell.env"
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

fetch_parameter() {
  local region="$1" name="$2" destination="$3" decrypt="${4:-}"
  local args=(--region "${region}" --name "${DEVCONTAINER_SSM_PREFIX}/${name}"
    --query 'Parameter.Value' --output text)
  [ -z "${decrypt}" ] || args+=(--with-decryption)

  aws ssm get-parameter "${args[@]}" > "${destination}" \
    || fail_bootstrap "could not fetch ${DEVCONTAINER_SSM_PREFIX}/${name} (was 'make push-secrets' run for this project?)"
  [ -s "${destination}" ] || fail_bootstrap "fetched ${name} is empty"
}

# The azure twin of imds_token/imds_get: a managed-identity access token for
# the Key Vault resource, fetched from the identity endpoint the VM answers
# on. The endpoint answers a GET (not aws IMDSv2's PUT), the token rides the
# JSON body, and python3 -- already the container's own tool -- extracts it.
azure_imds_token() {
  curl -sf --connect-timeout "${DEVCONTAINER_IMDS_TIMEOUT_SECONDS}" \
    -H "Metadata:true" \
    "${DEVCONTAINER_AZURE_IMDS_ENDPOINT}/metadata/identity/oauth2/token?api-version=${DEVCONTAINER_AZURE_IMDS_API_VERSION}&resource=${DEVCONTAINER_KEYVAULT_RESOURCE}" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])'
}

fetch_key_vault_secret() {
  local token="$1" secret="$2" destination="$3"
  curl -sf --connect-timeout "${DEVCONTAINER_IMDS_TIMEOUT_SECONDS}" \
    -H "Authorization: Bearer ${token}" \
    "https://${DEVCONTAINER_VAULT_NAME}.vault.azure.net/secrets/${secret}?api-version=${DEVCONTAINER_KEYVAULT_API_VERSION}" \
    | python3 -c 'import json,sys; sys.stdout.write(json.load(sys.stdin)["value"])' > "${destination}" \
    || fail_bootstrap "could not fetch ${secret} from Key Vault '${DEVCONTAINER_VAULT_NAME}' (was 'make push-secrets' run for this project?)"
  [ -s "${destination}" ] || fail_bootstrap "fetched ${secret} is empty"
}

bootstrap_secrets_from_key_vault() {
  log_info "shell.env not found, bootstrapping from Key Vault (${DEVCONTAINER_VAULT_NAME})"

  [ -n "${DEVCONTAINER_VAULT_NAME}" ] \
    || fail_bootstrap "DEVCONTAINER_VAULT_NAME is not set; the build's containerEnv must carry the instance's vault"
  command -v curl > /dev/null 2>&1 || fail_bootstrap "curl is not installed"
  command -v python3 > /dev/null 2>&1 || fail_bootstrap "python3 is not installed"

  local token
  token="$(azure_imds_token)" \
    || fail_bootstrap "the instance identity endpoint did not yield a vault token, not on a managed-identity host"
  [ -n "${token}" ] || fail_bootstrap "the instance identity endpoint returned an empty vault token"

  local previous_umask
  previous_umask="$(umask)"
  umask "${DEVCONTAINER_SECRET_UMASK}"
  fetch_key_vault_secret "${token}" "shell-env" "${SHELL_ENV}"
  [ -f "${PROFILE_MAP}" ] || fetch_key_vault_secret "${token}" "profile-map" "${PROFILE_MAP}"
  umask "${previous_umask}"

  log_success "Bootstrapped shell.env and aws-profile-map.json from Key Vault"
}

bootstrap_secrets() {
  if [ -f "${SHELL_ENV}" ]; then
    log_info "Using existing shell.env"
    return 0
  fi
  # The azure engine's secrets live in the instance's own Key Vault, named
  # by the build's containerEnv; its VM answers the managed-identity
  # endpoint instead of aws IMDSv2, so the bootstrap mode is selected here,
  # before the aws arm -- whose IMDS variables stay untouched -- can run.
  if [ "${DEVCONTAINER_ENGINE_KIND}" = "azure" ]; then
    bootstrap_secrets_from_key_vault
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

  sudo -E env "CONTAINER_USER_PATH=${PATH}" bash "${POSTCREATE}" "${DEVCONTAINER_USER}"

  log_success "Setup complete. View logs: cat /tmp/devcontainer-setup.log"
}

main "$@"
