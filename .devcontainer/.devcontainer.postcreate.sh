#!/usr/bin/env bash

set -euo pipefail

CONTAINER_USER="${1:?usage: $(basename "$0") <container-user>}"
WORK_DIR="$(pwd)"

# shellcheck source=devcontainer-functions.sh
source "${WORK_DIR}/.devcontainer/devcontainer-functions.sh"

: "${CICD:=false}"
: "${AWS_CONFIG_ENABLED:=true}"
: "${AWS_DEFAULT_OUTPUT:=json}"
: "${HOST_PROXY:=false}"
: "${HOST_PROXY_TIMEOUT:=10}"

: "${DEVCONTAINER_ZSH_THEME:=obraun}"
: "${DEVCONTAINER_ZSH_CORRECTION:=false}"
: "${DEVCONTAINER_ZSH_HIST_STAMPS:=%m/%d/%Y - %H:%M:%S}"
: "${DEVCONTAINER_CLAUDE_FLAGS:=--dangerously-skip-permissions}"
: "${DEVCONTAINER_EXTRA_PATH:=/usr/local/py-utils/bin:/usr/local/python/current/bin}"
: "${DEVCONTAINER_NPM_GLOBAL_DIRS:=lib/node_modules bin}"
: "${DEVCONTAINER_VSCODE_SERVER_DIRNAME:=.vscode-server}"
: "${DEVCONTAINER_VSCODE_SERVER_VOLUMES:=bin extensionsCache}"
: "${DEVCONTAINER_REPOS_DIR:=repos}"
: "${DEVCONTAINER_REPO_SCAN_IGNORE:=node_modules .venv venv __pycache__ .mypy_cache .pytest_cache .ruff_cache dist build target .next}"

USER_HOME="$(getent passwd "${CONTAINER_USER}" | cut -d: -f6)"
[ -n "${USER_HOME}" ] || exit_with_error "user '${CONTAINER_USER}' has no home directory in /etc/passwd"

BASH_RC="${USER_HOME}/.bashrc"
ZSH_RC="${USER_HOME}/.zshrc"
ZSH_ENV="${USER_HOME}/.zshenv"
SHELL_ENV="${WORK_DIR}/shell.env"
FUNCTIONS_FILE="${WORK_DIR}/.devcontainer/devcontainer-functions.sh"
TMUX_COMMANDS="${WORK_DIR}/.devcontainer/tmux-commands.sh"
TMUX_CONF="${WORK_DIR}/.devcontainer/tmux.conf"
PROJECT_SETUP="${WORK_DIR}/.devcontainer/project-setup.sh"
AWS_PROFILE_MAP_FILE="${WORK_DIR}/.devcontainer/aws-profile-map.json"
REPOS_PATH="${WORK_DIR}/${DEVCONTAINER_REPOS_DIR}"
RESMON_DISKS="${WORK_DIR}/.devcontainer/resmon-disks.py"
VSCODE_SETTINGS_SYNC="${WORK_DIR}/.devcontainer/vscode-settings-sync.py"

USER_BIN="${USER_HOME}/.local/bin"

VSCODE_SERVER_DIR="${USER_HOME}/${DEVCONTAINER_VSCODE_SERVER_DIRNAME}"

WARNINGS=()
is_cicd() { [ "${CICD,,}" = "true" ]; }

export PATH="${DEVCONTAINER_EXTRA_PATH}:${USER_BIN}:${PATH}"

configure_shell_env() {
  [ -f "${SHELL_ENV}" ] || exit_with_error "shell.env not found at ${SHELL_ENV}"
  log_section "Shell environment" "sourcing shell.env from bash and zsh"

  local path_prepend='case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) export PATH="$HOME/.local/bin:$PATH" ;; esac'

  {
    echo "source \"${SHELL_ENV}\""
    echo "export BASH_ENV=\"${SHELL_ENV}\""
    echo "${path_prepend}"
  } >> "${BASH_RC}"

  {
    echo "source \"${SHELL_ENV}\""
    echo "${path_prepend}"
  } > "${ZSH_ENV}"

  log_section_done "Shell environment"
}

as_container_user() {
  if is_wsl; then
    bash -c "$1"
  else
    sudo -u "${CONTAINER_USER}" bash -c "$1"
  fi
}

interactive_rcs() {
  printf '%s\n' "${BASH_RC}"
  [ -f "${ZSH_RC}" ] && printf '%s\n' "${ZSH_RC}"
  return 0
}

configure_npm_global_ownership() {
  if ! container_user_has npm; then
    log_section_skipped "Global npm ownership" \
      "npm is not installed, add the node feature to devcontainer.json"
    return 0
  fi
  [ -n "${DEVCONTAINER_NPM_GLOBAL_DIRS}" ] \
    || exit_with_error "DEVCONTAINER_NPM_GLOBAL_DIRS must name at least one directory"

  local user_path="${CONTAINER_USER_PATH:-${PATH}}"
  local prefix
  prefix="$(as_container_user "PATH='${user_path}' npm prefix -g")"
  [ -n "${prefix}" ] || exit_with_error "npm did not report a global prefix"
  [ -d "${prefix}" ] \
    || exit_with_error "npm reports a global prefix that does not exist: ${prefix}"

  log_section "Global npm ownership" "${prefix} -> ${CONTAINER_USER}"

  local -a names targets=()
  local name
  read -ra names <<< "${DEVCONTAINER_NPM_GLOBAL_DIRS}"
  for name in "${names[@]}"; do
    [ -d "${prefix}/${name}" ] \
      || exit_with_error "${prefix}/${name} is missing, so the npm prefix is not laid out as expected"
    targets+=("${prefix}/${name}")
  done
  chown -R "${CONTAINER_USER}" "${targets[@]}"

  as_container_user "test -w '${prefix}/lib/node_modules'" \
    || exit_with_error "${CONTAINER_USER} still cannot write ${prefix}/lib/node_modules"

  log_section_done "Global npm ownership"
}

configure_claude_aliases() {
  if ! container_user_has claude; then
    log_section_skipped "Claude Code aliases" \
      "claude is not installed, add the claude-code feature to devcontainer.json"
    return 0
  fi
  log_section "Claude Code aliases" "ccd, ccdr"
  local rc
  while read -r rc; do
    {
      echo "alias ccd='claude ${DEVCONTAINER_CLAUDE_FLAGS}'"
      echo "alias ccdr='claude ${DEVCONTAINER_CLAUDE_FLAGS} --resume'"
    } >> "${rc}"
  done < <(interactive_rcs)
  log_section_done "Claude Code aliases"
}

configure_tmux_commands() {
  if ! container_user_has tmux; then
    log_section_skipped "tmux commands" \
      "tmux is not installed, add it to the apt-packages feature in devcontainer.json"
    return 0
  fi
  [ -f "${TMUX_COMMANDS}" ] || {
    log_section_skipped "tmux commands" "${TMUX_COMMANDS} is missing"
    return 0
  }
  [ -f "${TMUX_CONF}" ] || exit_with_error "tmux config not found at ${TMUX_CONF}"
  log_section "tmux commands" "tm* helpers, mouse and scrollback"
  local rc
  while read -r rc; do
    echo "source \"${TMUX_COMMANDS}\"" >> "${rc}"
  done < <(interactive_rcs)
  install -m 644 "${TMUX_CONF}" "${USER_HOME}/.tmux.conf"

  local zsh_path
  zsh_path="$(container_user_path_to zsh)"
  [ -n "${zsh_path}" ] || exit_with_error \
    "zsh not found, so tmux would open bash. Enable installZsh on the common-utils feature."
  sed -i "s|^set -g default-shell .*|set -g default-shell ${zsh_path}|" "${USER_HOME}/.tmux.conf"
  grep -q "^set -g default-shell ${zsh_path}$" "${USER_HOME}/.tmux.conf" \
    || exit_with_error "could not set default-shell in ${USER_HOME}/.tmux.conf"

  log_section_done "tmux commands"
}

is_mount_point() {
  awk -v target="$1" '$2 == target { found = 1 } END { exit found ? 0 : 1 }' /proc/self/mounts
}

configure_vscode_server_dir() {
  [ -n "${DEVCONTAINER_VSCODE_SERVER_VOLUMES}" ] \
    || exit_with_error "DEVCONTAINER_VSCODE_SERVER_VOLUMES must name at least one directory"

  local -a names paths=()
  local name path
  read -ra names <<< "${DEVCONTAINER_VSCODE_SERVER_VOLUMES}"
  for name in "${names[@]}"; do
    path="${VSCODE_SERVER_DIR}/${name}"
    if ! is_mount_point "${path}"; then
      exit_with_error "$(printf '%s\n' \
        "${path} is not a mount point, so VS Code would refill it on every rebuild." \
        "devcontainer.json must mount a volume there. Its targets are literal paths and this one is" \
        "derived from ${CONTAINER_USER}'s home in /etc/passwd, so they disagree: check the mounts entries," \
        "remoteUser, DEVCONTAINER_VSCODE_SERVER_DIRNAME ('${DEVCONTAINER_VSCODE_SERVER_DIRNAME}') and" \
        "DEVCONTAINER_VSCODE_SERVER_VOLUMES ('${DEVCONTAINER_VSCODE_SERVER_VOLUMES}').")"
    fi
    paths+=("${path}")
  done

  log_section "VS Code server volumes" "${DEVCONTAINER_VSCODE_SERVER_VOLUMES}"
  chown "${CONTAINER_USER}:${CONTAINER_USER}" "${VSCODE_SERVER_DIR}" "${paths[@]}"

  for path in "${VSCODE_SERVER_DIR}" "${paths[@]}"; do
    as_container_user "test -w '${path}'" \
      || exit_with_error "${CONTAINER_USER} cannot write ${path}, so VS Code could not install into it"
  done

  log_section_done "VS Code server volumes" \
    "$(du -sh "${paths[@]}" | awk '{ printf "%s %s  ", $1, $2 }')carried over"
}

container_user_python() {
  local python
  python="$(container_user_path_to python3)"
  [ -n "${python}" ] || exit_with_error \
    "python3 not found, so postAttachCommand cannot run its hooks. Add the python feature to devcontainer.json."
  printf '%s\n' "${python}"
}

install_attach_hook() {
  local source="$1" hook
  hook="${USER_BIN}/$(basename "$1")"
  [ -f "${source}" ] || exit_with_error \
    "hook script not found at ${source}, which postAttachCommand runs on every attach"
  install -d -m 755 "${USER_BIN}"
  ln -sfn "${source}" "${hook}"
  printf '%s\n' "${hook}"
}

run_attach_hook() {
  local python="$1" hook="$2"
  as_container_user "HOME='${USER_HOME}' '${python}' '${hook}'" \
    || exit_with_error "${hook} does not run, so postAttachCommand would fail on every attach"
}

configure_resmon_disks() {
  local python hook
  python="$(container_user_python)"
  hook="$(install_attach_hook "${RESMON_DISKS}")"
  log_section "Resource Monitor disks" "${hook} -> ${RESMON_DISKS}"
  run_attach_hook "${python}" "${hook}"
  log_section_done "Resource Monitor disks"
}

configure_vscode_settings_sync() {
  local python hook
  python="$(container_user_python)"
  hook="$(install_attach_hook "${VSCODE_SETTINGS_SYNC}")"
  log_section "VS Code settings sync" "${hook} -> ${VSCODE_SETTINGS_SYNC}"
  run_attach_hook "${python}" "${hook}"
  log_section_done "VS Code settings sync"
}

configure_oh_my_zsh() {
  if [ ! -d "${USER_HOME}/.oh-my-zsh" ] || [ ! -f "${ZSH_RC}" ]; then
    log_section_skipped "Oh My Zsh configuration" \
      "no ~/.oh-my-zsh or ~/.zshrc, enable installOhMyZsh on the common-utils feature"
    return 0
  fi
  log_section "Oh My Zsh configuration" "theme ${DEVCONTAINER_ZSH_THEME}"

  sed -i \
    -e "s|^ZSH_THEME=.*|ZSH_THEME=\"${DEVCONTAINER_ZSH_THEME}\"|" \
    -e "s|^# *ENABLE_CORRECTION=.*|ENABLE_CORRECTION=\"${DEVCONTAINER_ZSH_CORRECTION}\"|" \
    -e "s|^# *HIST_STAMPS=.*|HIST_STAMPS=\"${DEVCONTAINER_ZSH_HIST_STAMPS}\"|" \
    "${ZSH_RC}"

  grep -q "^ZSH_THEME=\"${DEVCONTAINER_ZSH_THEME}\"" "${ZSH_RC}" \
    || exit_with_error "could not set ZSH_THEME in ${ZSH_RC}, the base image's .zshrc layout changed"
  log_section_done "Oh My Zsh configuration"
}

configure_aws_profiles() {
  if is_cicd; then
    log_section_skipped "AWS profile configuration" "CICD mode"
    return 0
  fi
  if [ "${AWS_CONFIG_ENABLED,,}" != "true" ]; then
    log_section_skipped "AWS profile configuration" "AWS_CONFIG_ENABLED=${AWS_CONFIG_ENABLED}"
    return 0
  fi
  if [ ! -s "${AWS_PROFILE_MAP_FILE}" ]; then
    log_section_skipped "AWS profile configuration" "no profile map at ${AWS_PROFILE_MAP_FILE}"
    return 0
  fi
  if ! container_user_has jq; then
    log_section_skipped "AWS profile configuration" \
      "jq is not installed, add it to the apt-packages feature in devcontainer.json"
    return 0
  fi

  local profile_map
  profile_map="$(<"${AWS_PROFILE_MAP_FILE}")"
  jq empty <<< "${profile_map}" > /dev/null 2>&1 \
    || exit_with_error "${AWS_PROFILE_MAP_FILE} is not valid JSON"

  log_section "AWS profile configuration" "generating ~/.aws/config"
  mkdir -p "${USER_HOME}/.aws/amazonq/cache"
  jq -r --arg output "${AWS_DEFAULT_OUTPUT}" 'to_entries[] |
    "[profile \(.key)]\n" +
    "sso_start_url = \(.value.sso_start_url)\n" +
    "sso_region = \(.value.sso_region)\n" +
    "sso_account_name = \(.value.account_name)\n" +
    "sso_account_id = \(.value.account_id)\n" +
    "sso_role_name = \(.value.role_name)\n" +
    "region = \(.value.region)\n" +
    "output = \($output)\n" +
    "sso_auto_populated = true\n"' <<< "${profile_map}" \
    > "${USER_HOME}/.aws/config"

  log_section_done "AWS profile configuration" \
    "$(grep -c '^\[profile' "${USER_HOME}/.aws/config") profile(s) written"
}

validate_proxy() {
  if [ "${HOST_PROXY,,}" != "true" ]; then
    log_info "Host proxy not enabled (HOST_PROXY=${HOST_PROXY})"
    return 0
  fi
  [ -n "${HOST_PROXY_URL:-}" ] \
    || exit_with_error "HOST_PROXY=true but HOST_PROXY_URL is not set (e.g. http://host.docker.internal:3128)"

  log_section "Host proxy validation" "${HOST_PROXY_URL}"
  parse_proxy_host_port "${HOST_PROXY_URL}"
  local guide="nix-family-os/README.md"
  is_wsl && guide="wsl-family-os/README.md"
  validate_host_proxy "${PROXY_PARSED_HOST}" "${PROXY_PARSED_PORT}" "${HOST_PROXY_TIMEOUT}" "${guide}"
  log_section_done "Host proxy validation"
}

configure_git() {
  if is_cicd; then
    log_section_skipped "Git configuration" "CICD mode"
    return 0
  fi
  if ! container_user_has git; then
    log_section_skipped "Git configuration" "git is not installed"
    return 0
  fi
  log_section "Git configuration" "identity and credential helper"
  configure_git_shared "${CONTAINER_USER}" "${GIT_USER}" "${GIT_USER_EMAIL}"
  configure_git_credential_helper "${CONTAINER_USER}" "${GIT_PROVIDER_URL}"
  log_section_done "Git configuration"
}

declare_unclaimed_path() {
  local file="$1/.gitmodules" relative_path="$2"
  git config -f "${file}" "submodule.${relative_path}.path" "${relative_path}"
  git config -f "${file}" "submodule.${relative_path}.url" "./${relative_path}"
  chown "${CONTAINER_USER}:${CONTAINER_USER}" "${file}"
}

enclosing_repo() {
  local dir
  dir="$(dirname "$1")"
  while [ "${dir}" != "/" ]; do
    if [ -e "${dir}/.git" ]; then
      printf '%s\n' "${dir}"
      return 0
    fi
    [ "${dir}" != "${WORK_DIR}" ] || return 1
    dir="$(dirname "${dir}")"
  done
  return 1
}

configure_repo_detection() {
  if ! container_user_has git; then
    log_section_skipped "Repository detection" "git is not installed"
    return 0
  fi
  [ -n "${DEVCONTAINER_REPO_SCAN_IGNORE}" ] ||
    exit_with_error "DEVCONTAINER_REPO_SCAN_IGNORE must name at least one directory"
  log_section "Repository detection" "${DEVCONTAINER_REPOS_DIR}/ and every nested clone"
  mkdir -p "${REPOS_PATH}"
  chown "${CONTAINER_USER}:${CONTAINER_USER}" "${REPOS_PATH}"
  declare_unclaimed_path "${WORK_DIR}" "${DEVCONTAINER_REPOS_DIR}"

  local -a ignore_names ignore_expr=()
  local name
  read -ra ignore_names <<< "${DEVCONTAINER_REPO_SCAN_IGNORE}"
  for name in "${ignore_names[@]}"; do
    ignore_expr+=(-name "${name}" -o)
  done
  unset "ignore_expr[$(( ${#ignore_expr[@]} - 1 ))]"

  local marker repo parent relative count=0
  while IFS= read -r marker; do
    repo="$(dirname "${marker}")"
    [ "${repo}" != "${WORK_DIR}" ] || continue
    parent="$(enclosing_repo "${repo}")" || continue
    relative="${repo#"${parent}"/}"
    declare_unclaimed_path "${parent}" "${relative}"
    count=$(( count + 1 ))
  done < <(find "${WORK_DIR}" \( "${ignore_expr[@]}" \) -prune -o -name .git -prune -print)

  log_info "Declared ${count} nested repository path(s)"
  log_section_done "Repository detection"
}

fix_ownership() {
  log_info "Setting ownership of ${USER_HOME} to ${CONTAINER_USER} (excluding ${VSCODE_SERVER_DIR})"
  find "${USER_HOME}" -path "${VSCODE_SERVER_DIR}" -prune -o \
    -exec chown "${CONTAINER_USER}:${CONTAINER_USER}" {} +
}

run_project_setup() {
  if [ ! -f "${PROJECT_SETUP}" ]; then
    log_warn "No project setup script at ${PROJECT_SETUP}"
    return 0
  fi
  log_section "Project setup" "$(basename "${PROJECT_SETUP}")"

  local command="export WORK_DIR='${WORK_DIR}'
export PATH='${DEVCONTAINER_EXTRA_PATH}:${USER_BIN}:'\"\$PATH\"
source '${SHELL_ENV}'
cd '${WORK_DIR}'
BASH_ENV='${FUNCTIONS_FILE}' bash '${PROJECT_SETUP}'"

  as_container_user "${command}"
  log_section_done "Project setup"
}

report_warnings() {
  if [ ${#WARNINGS[@]} -eq 0 ]; then
    log_success "Dev container setup completed with no warnings"
    return 0
  fi
  echo ""
  log_warn_summary "${WARNINGS[@]}"
}

main() {
  log_info "Starting post-create setup as '${CONTAINER_USER}' (CICD=${CICD})"
  [ -n "${DEFAULT_GIT_BRANCH:-}" ] || exit_with_error "DEFAULT_GIT_BRANCH is not set in the environment"

  configure_apt_proxy

  configure_vscode_server_dir
  configure_shell_env
  configure_npm_global_ownership
  configure_claude_aliases
  configure_tmux_commands
  configure_resmon_disks
  configure_vscode_settings_sync
  configure_oh_my_zsh
  configure_aws_profiles
  validate_proxy
  configure_git
  configure_repo_detection
  fix_ownership
  report_warnings
  run_project_setup
}

main "$@"
