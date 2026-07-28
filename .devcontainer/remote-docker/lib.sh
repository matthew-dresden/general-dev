#!/usr/bin/env bash
# Shared functions for the remote-docker scripts. Sourced, never executed.
# Compatible with macOS /bin/bash 3.2 (no bash-4-only constructs).

set -euo pipefail

RD_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

rd_log() { printf '\033[0;36m[INFO]\033[0m %s\n' "$1"; }
rd_ok() { printf '\033[0;32m[DONE]\033[0m %s\n' "$1"; }
rd_die() {
  printf '\033[0;31m[ERROR]\033[0m %s\n' "$1" >&2
  exit 1
}

# Load configuration. Environment variables win over config.env defaults
# because config.env only assigns unset variables (: "${VAR:=...}").
rd_load_config() {
  local config_file="${REMOTE_DOCKER_CONFIG:-${RD_DIR}/config.env}"
  [ -f "$config_file" ] || rd_die "config file not found: $config_file"

  # Real values live in shell.env, which is gitignored, so config.env can be
  # committed with placeholders and the repo published without identifiers.
  #
  # Only the settings these scripts own are taken, not the whole file: it also
  # carries container-side values such as http_proxy pointing at
  # host.docker.internal, which would break the AWS CLI if applied here. Read
  # before config.env, because config.env only assigns what is still unset.
  # Precedence is environment, then shell.env, then the config.env defaults, so
  # each assignment is rewritten to only-if-unset form. A plain eval would make
  # shell.env beat an exported override, which is backwards.
  local shell_env="${REPO_SHELL_ENV:-${RD_DIR}/../../shell.env}"
  if [ -f "$shell_env" ]; then
    local assignments
    # Strip surrounding quotes: shell.env writes export VAR='value', and the
    # only-if-unset rewrite would otherwise carry the quotes into the value.
    assignments="$(sed -nE -e "s/^[[:space:]]*(export[[:space:]]+)?((REMOTE_|LOCAL_DOCKER_|TINYPROXY_)[A-Z_]+)='(.*)'[[:space:]]*$/: \"\\\${\2:=\4}\"/p" \
      -e 's/^[[:space:]]*(export[[:space:]]+)?((REMOTE_|LOCAL_DOCKER_|TINYPROXY_)[A-Z_]+)="(.*)"[[:space:]]*$/: "${\2:=\4}"/p' \
      -e 's/^[[:space:]]*(export[[:space:]]+)?((REMOTE_|LOCAL_DOCKER_|TINYPROXY_)[A-Z_]+)=([^"'"'"'].*)$/: "${\2:=\4}"/p' "$shell_env" || true)"
    [ -z "$assignments" ] || eval "$assignments"
  fi

  # shellcheck source=config.env
  source "$config_file"

  # config.env ships placeholders so the repo carries no real identifiers.
  case "${REMOTE_INSTANCE_ID:-}" in
    "<"*">") rd_die "REMOTE_INSTANCE_ID is still the placeholder ${REMOTE_INSTANCE_ID}. Set it in shell.env (see shell.env.example) or export it." ;;
  esac

  : "${REMOTE_INSTANCE_ID:?REMOTE_INSTANCE_ID must be set}"
  : "${REMOTE_AWS_REGION:?REMOTE_AWS_REGION must be set}"
  : "${REMOTE_AWS_PROFILE:?REMOTE_AWS_PROFILE must be set}"
  : "${REMOTE_SSH_ALIAS:?REMOTE_SSH_ALIAS must be set}"
  : "${REMOTE_USER:?REMOTE_USER must be set}"
  : "${REMOTE_DOCKER_CONTEXT:?REMOTE_DOCKER_CONTEXT must be set}"
}

rd_require_cmd() {
  local cmd="$1" hint="$2"
  command -v "$cmd" > /dev/null 2>&1 || rd_die "'$cmd' is required but not installed. $hint"
}

rd_check_prereqs() {
  rd_require_cmd aws "Install: https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html"
  rd_require_cmd session-manager-plugin "Install: https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html"
  rd_require_cmd ssh "Install OpenSSH client."
}

rd_check_aws_auth() {
  aws sts get-caller-identity --profile "$REMOTE_AWS_PROFILE" --region "$REMOTE_AWS_REGION" > /dev/null 2>&1 \
    || rd_die "AWS credentials for profile '$REMOTE_AWS_PROFILE' are not valid. Run: aws sso login --profile $REMOTE_AWS_PROFILE"
}

# Install/refresh the managed SSH config block for the SSM tunnel.
# Idempotent: replaces any previous block between the markers.
rd_install_ssh_config() {
  [ -n "${REMOTE_SSH_KEY_PATH:-}" ] || rd_die "REMOTE_SSH_KEY_PATH must be set (private key for the EC2 key pair)"
  [ -f "$REMOTE_SSH_KEY_PATH" ] || rd_die "SSH private key not found at $REMOTE_SSH_KEY_PATH (set REMOTE_SSH_KEY_PATH to your key for the '<your-key-pair-name>' key pair)"

  local ssh_dir="${HOME}/.ssh"
  local ssh_config="${ssh_dir}/config"
  local marker="general-dev remote-docker ${REMOTE_SSH_ALIAS}"
  mkdir -p "$ssh_dir"
  chmod 700 "$ssh_dir"
  touch "$ssh_config"
  chmod 600 "$ssh_config"

  local tmp_config
  tmp_config="$(mktemp)"
  awk -v marker="$marker" '
    index($0, ">>> " marker " >>>") { skip = 1; next }
    index($0, "<<< " marker " <<<") { skip = 0; next }
    !skip { print }
  ' "$ssh_config" > "$tmp_config"

  {
    echo "# >>> ${marker} >>>"
    echo "Host ${REMOTE_SSH_ALIAS}"
    echo "  HostName ${REMOTE_INSTANCE_ID}"
    echo "  User ${REMOTE_USER}"
    echo "  IdentityFile ${REMOTE_SSH_KEY_PATH}"
    echo "  IdentitiesOnly yes"
    echo "  ProxyCommand sh -c \"aws ssm start-session --target %h --document-name AWS-StartSSHSession --parameters 'portNumber=%p' --region ${REMOTE_AWS_REGION} --profile ${REMOTE_AWS_PROFILE}\""
    echo "  ConnectTimeout ${REMOTE_SSH_CONNECT_TIMEOUT:-30}"
    echo "  ServerAliveInterval 15"
    echo "  ServerAliveCountMax 3"
    echo "  StrictHostKeyChecking accept-new"
    echo "  ControlMaster auto"
    echo "  ControlPath ~/.ssh/cm-%C"
    echo "  ControlPersist ${REMOTE_SSH_CONTROL_PERSIST:-10m}"
    echo "# <<< ${marker} <<<"
  } >> "$tmp_config"

  mv "$tmp_config" "$ssh_config"
  chmod 600 "$ssh_config"
  rd_ok "SSH config block installed for Host '${REMOTE_SSH_ALIAS}' -> ${REMOTE_INSTANCE_ID}"
}
