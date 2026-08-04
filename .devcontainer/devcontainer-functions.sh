#!/usr/bin/env bash

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

log_info() {
  echo -e "${CYAN}[INFO]${NC} $1"
}

log_success() {
  echo -e "${GREEN}[DONE]${NC} $1"
}

log_warn() {
  echo -e "${YELLOW}[WARN]${NC} $1"
  WARNINGS+=("$1")
}

log_warn_summary() {
  echo -e "${YELLOW}Completed with $# warning(s):${NC}"
  printf "${YELLOW}  - %s${NC}\n" "$@"
}

log_error() {
  echo -e "${RED}[ERROR]${NC} $1"
}

exit_with_error() {
  log_error "$1"
  exit 1
}

: "${BANNER_WIDTH:=74}"

_banner_line() {
  local color="$1" char="$2" rule
  rule="$(printf "%${BANNER_WIDTH}s" "" | tr ' ' "$char")"
  printf '%b%s%b\n' "$color" "$rule" "$NC"
}

_banner() {
  local color="$1" tag="$2" title="$3" detail="${4:-}"
  echo ""
  _banner_line "$color" "="
  echo -e "${color}  ${tag}: ${title}${NC}"
  [ -z "$detail" ] || echo -e "${color}  ${detail}${NC}"
  _banner_line "$color" "="
}

log_section() {
  _banner "${CYAN}" "RUNNING " "$1" "${2:-}"
}

log_section_skipped() {
  _banner "${YELLOW}" "SKIPPED " "$1" "${2:-}"
  WARNINGS+=("Skipped: $1, ${2:-no reason given}")
}

log_section_done() {
  _banner "${GREEN}" "COMPLETE" "$1" "${2:-}"
}

container_user_has() {
  PATH="${CONTAINER_USER_PATH:-${PATH}}" command -v "$1" > /dev/null 2>&1
}

container_user_path_to() {
  PATH="${CONTAINER_USER_PATH:-${PATH}}" command -v "$1" 2> /dev/null
}

configure_apt_proxy() {
  local apt_proxy_conf="/etc/apt/apt.conf.d/99proxy"
  local proxy_url="${HTTP_PROXY:-${http_proxy:-}}"

  if [ -z "${proxy_url}" ]; then
    log_info "No proxy configured for apt (HTTP_PROXY not set)"
    return 0
  fi

  local https_proxy_url="${HTTPS_PROXY:-${https_proxy:-${proxy_url}}}"

  log_info "Configuring apt proxy: http=${proxy_url} https=${https_proxy_url}"

  cat > "${apt_proxy_conf}" <<APT_PROXY_EOF
Acquire::http::Proxy "${proxy_url}";
Acquire::https::Proxy "${https_proxy_url}";
APT_PROXY_EOF

  local no_proxy_val="${NO_PROXY:-${no_proxy:-}}"
  if [ -n "${no_proxy_val}" ]; then
    local _old_ifs="${IFS}"
    IFS=','
    for _entry in ${no_proxy_val}; do
      IFS="${_old_ifs}"
      _entry=$(echo "${_entry}" | tr -d ' ')
      case "${_entry}" in
        [a-zA-Z]*.*)
          echo "Acquire::http::Proxy::${_entry} \"DIRECT\";" >> "${apt_proxy_conf}"
          echo "Acquire::https::Proxy::${_entry} \"DIRECT\";" >> "${apt_proxy_conf}"
          ;;
      esac
    done
    IFS="${_old_ifs}"
  fi

  log_success "Wrote apt proxy configuration to ${apt_proxy_conf}"
}

is_wsl() {
  uname -r | grep -i microsoft > /dev/null
}

write_file_with_wsl_compat() {
  local file_path="$1"
  local content="$2"
  local permissions="${3:-}"

  if is_wsl; then
    echo "$content" | sudo tee "$file_path" > /dev/null
    if [ -n "$permissions" ]; then
      sudo chmod "$permissions" "$file_path"
    fi
  else
    echo "$content" > "$file_path"
    if [ -n "$permissions" ]; then
      chmod "$permissions" "$file_path"
    fi
  fi
}

append_to_file_with_wsl_compat() {
  local file_path="$1"
  local content="$2"

  if is_wsl; then
    echo "$content" | sudo tee -a "$file_path" > /dev/null
  else
    echo "$content" >> "$file_path"
  fi
}

parse_proxy_host_port() {
  local proxy_url="${1:?proxy URL must be provided}"

  local host_port="${proxy_url#*://}"
  host_port="${host_port%%/*}"

  if [[ "$host_port" != *:* ]]; then
    exit_with_error "❌ HOST_PROXY_URL '${proxy_url}' does not contain a port (expected format: http://host:port)"
  fi

  PROXY_PARSED_HOST="${host_port%:*}"
  PROXY_PARSED_PORT="${host_port##*:}"

  if [ -z "$PROXY_PARSED_HOST" ] || [ -z "$PROXY_PARSED_PORT" ]; then
    exit_with_error "❌ Failed to parse host/port from HOST_PROXY_URL '${proxy_url}'"
  fi
}

validate_host_proxy() {
  local proxy_host="$1"
  local proxy_port="$2"
  local timeout="${3:?timeout must be provided}"
  local readme_ref="$4"

  log_info "Validating host proxy at ${proxy_host}:${proxy_port} (timeout: ${timeout}s)..."

  local elapsed=0
  while [ "$elapsed" -lt "$timeout" ]; do
    if nc -z -w 1 "$proxy_host" "$proxy_port" 2>/dev/null; then
      log_success "Host proxy reachable at ${proxy_host}:${proxy_port}"
      return 0
    fi
    elapsed=$((elapsed + 1))
  done

  exit_with_error "❌ Host proxy not reachable at ${proxy_host}:${proxy_port} after ${timeout}s. See ${readme_ref} for troubleshooting."
}

configure_git_shared() {
  local container_user="$1"
  local git_user="${2:?GIT_USER must be set}"
  local git_user_email="${3:?GIT_USER_EMAIL must be set}"
  local gitconfig="/home/${container_user}/.gitconfig"

  cat <<EOF >> "${gitconfig}"
[user]
    name = ${git_user}
    email = ${git_user_email}
[core]
    editor = vim
[push]
    autoSetupRemote = true
[safe]
    directory = *
[pager]
    branch = false
    config = false
    diff = false
    log = false
    show = false
    status = false
    tag = false
EOF
}

configure_git_credential_helper() {
  local container_user="$1"
  local git_provider_url="${2:?GIT_PROVIDER_URL must be set}"
  local gitconfig="/home/${container_user}/.gitconfig"

  log_info "Configuring git credential helper..."

  cat <<EOF >> "${gitconfig}"
[credential]
    helper = store
EOF

  cat <<EOF >> "${gitconfig}"
[url "https://${git_provider_url}/"]
    insteadOf = git@${git_provider_url}:
EOF

  log_success "Git credential helper configured, run 'make push-git-creds' to supply credentials"
}
