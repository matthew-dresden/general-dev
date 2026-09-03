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
: "${DEVCONTAINER_NPM_MANAGED_CLIS:=claude}"
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
CLAUDE_SETTINGS_FILE="${WORK_DIR}/.devcontainer/claude-settings.json"
REPOS_PATH="${WORK_DIR}/${DEVCONTAINER_REPOS_DIR}"
RESMON_DISKS="${WORK_DIR}/.devcontainer/resmon-disks.py"
VSCODE_SETTINGS_SYNC="${WORK_DIR}/.devcontainer/vscode-settings-sync.py"

# Where devcontainer_config lives (Makefile:15 holds the same value for
# 'make hooks-install' and friends). Named once here so configure_shell_env
# does not hardcode this path inline.
DEVCONTAINER_SCRIPTS_DIR="${WORK_DIR}/.claude/plugins/devcontainer/scripts"

USER_BIN="${USER_HOME}/.local/bin"

VSCODE_SERVER_DIR="${USER_HOME}/${DEVCONTAINER_VSCODE_SERVER_DIRNAME}"

WARNINGS=()
is_cicd() { [ "${CICD,,}" = "true" ]; }

export PATH="${DEVCONTAINER_EXTRA_PATH}:${USER_BIN}:${PATH}"

# The devsecret export-list startup block for one shell (spec Section 11),
# rendered by devcontainer_config.shellrc rather than hand-written here
# (spec Section 3.5: all new logic is Python, no new shell script). Reused
# for both shells configure_shell_env wires below so the render invocation
# exists in exactly one place.
render_devsecret_shell_block() {
  local shell="$1"
  PYTHONPATH="${DEVCONTAINER_SCRIPTS_DIR}" python3 -m devcontainer_config.shellrc "${shell}"
}

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

  # A container whose shells silently lack their exported secrets is worse
  # than a container that failed to create, so a non-zero render is fatal
  # through exit_with_error rather than warned about or skipped.
  local bash_block zsh_block
  bash_block="$(render_devsecret_shell_block bash)" || exit_with_error "$(printf '%s\n' \
    "devcontainer_config.shellrc failed to render the bash devsecret export-list block." \
    "Rerun 'PYTHONPATH=${DEVCONTAINER_SCRIPTS_DIR} python3 -m devcontainer_config.shellrc bash'" \
    "from ${WORK_DIR} to see the underlying error.")"
  zsh_block="$(render_devsecret_shell_block zsh)" || exit_with_error "$(printf '%s\n' \
    "devcontainer_config.shellrc failed to render the zsh devsecret export-list block." \
    "Rerun 'PYTHONPATH=${DEVCONTAINER_SCRIPTS_DIR} python3 -m devcontainer_config.shellrc zsh'" \
    "from ${WORK_DIR} to see the underlying error.")"

  # Idempotent: a rebuild or a manual rerun of this function must not
  # duplicate the block (AC-FUNC-006). Each rendered block's own first line
  # is its marker (devcontainer_config.shellrc.MARKER); grepping the target
  # startup file for that line before appending, the same
  # 'grep -q ... || <action>' guard style already used elsewhere in this
  # file, is what makes a second run a no-op instead of a second copy.
  local bash_marker zsh_marker
  bash_marker="$(printf '%s\n' "${bash_block}" | head -n 1)"
  zsh_marker="$(printf '%s\n' "${zsh_block}" | head -n 1)"

  grep -qF -- "${bash_marker}" "${BASH_RC}" || printf '%s\n' "${bash_block}" >> "${BASH_RC}"
  grep -qF -- "${zsh_marker}" "${ZSH_ENV}" || printf '%s\n' "${zsh_block}" >> "${ZSH_ENV}"

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

# The node_modules a globally installed CLI actually lives under, walked up
# from the executable itself. 'npm prefix -g' reports the prefix of the active
# node version only, and nvm keeps one prefix per version, so a CLI a feature
# installed can sit under a version that is not active when postCreate runs.
npm_install_root_of() {
  local cli="$1" bin target root
  bin="$(container_user_path_to "${cli}")" || return 1
  [ -n "${bin}" ] || return 1
  target="$(readlink -f "${bin}")" || return 1
  root="${target}"
  while [ "${root}" != "/" ]; do
    root="$(dirname "${root}")"
    if [ "$(basename "${root}")" = "node_modules" ]; then
      printf '%s\n' "${root}"
      return 0
    fi
  done
  return 1
}

# Every directory an update writes into: the global node_modules, each scope
# directory inside it, and bin, which holds the executable symlink npm
# replaces. A package is updated by replacing its directory, so what has to be
# writable is the directory holding it, not the package directory itself.
npm_package_parents() {
  local prefix
  for prefix in "$@"; do
    if [ -d "${prefix}/lib/node_modules" ]; then
      printf '%s\n' "${prefix}/lib/node_modules"
      find "${prefix}/lib/node_modules" -maxdepth 1 -mindepth 1 -type d -name '@*'
    fi
    if [ -d "${prefix}/bin" ]; then
      printf '%s\n' "${prefix}/bin"
    fi
  done
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

  # A devcontainer feature installs its CLI as root at image build time, so the
  # package directory and the scope directory above it are left root-owned. The
  # handover therefore has to reach the prefix that CLI is really in, which is
  # not always the one npm just reported.
  local -a prefixes=("${prefix}") clis=()
  local cli root cli_prefix
  read -ra clis <<< "${DEVCONTAINER_NPM_MANAGED_CLIS}"
  for cli in ${clis[@]+"${clis[@]}"}; do
    container_user_has "${cli}" || continue
    root="$(npm_install_root_of "${cli}")" \
      || exit_with_error "${cli} is on PATH but is not inside a node_modules, so the prefix owning it cannot be resolved"
    cli_prefix="$(dirname "$(dirname "${root}")")"
    case " ${prefixes[*]} " in
      *" ${cli_prefix} "*) ;;
      *) prefixes+=("${cli_prefix}") ;;
    esac
  done

  log_section "Global npm ownership" "${prefixes[*]} -> ${CONTAINER_USER}"

  local -a names targets=()
  local name
  read -ra names <<< "${DEVCONTAINER_NPM_GLOBAL_DIRS}"
  for prefix in "${prefixes[@]}"; do
    for name in "${names[@]}"; do
      [ -d "${prefix}/${name}" ] \
        || exit_with_error "${prefix}/${name} is missing, so the npm prefix is not laid out as expected"
      targets+=("${prefix}/${name}")
    done
  done
  chown -R "${CONTAINER_USER}" "${targets[@]}"

  # Probed by creating and removing an entry rather than with test -w, which
  # passes on a group-writable node_modules while the scope directories inside
  # it stay root-owned. That is the state a feature leaves behind, and it fails
  # an update with "no write permission to npm prefix" long after the build
  # reported success.
  local dir
  while read -r dir; do
    as_container_user "probe=\"${dir}/.postcreate-write-probe.\$\$\" \
      && mkdir \"\${probe}\" && rmdir \"\${probe}\"" > /dev/null 2>&1 \
      || exit_with_error "$(printf '%s\n' \
        "${CONTAINER_USER} cannot create entries in ${dir}, so npm cannot replace a package there" \
        "and a globally installed CLI will fail to update itself." \
        "Directories handed over: ${DEVCONTAINER_NPM_GLOBAL_DIRS} under ${prefixes[*]}." \
        "A directory still root-owned here is outside that set: add its prefix by naming the CLI" \
        "in DEVCONTAINER_NPM_MANAGED_CLIS, or the directory in DEVCONTAINER_NPM_GLOBAL_DIRS.")"
  done < <(npm_package_parents "${prefixes[@]}")

  log_section_done "Global npm ownership" \
    "$(npm_package_parents "${prefixes[@]}" | wc -l | tr -d ' ') writable directories"
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

configure_claude_settings() {
  if ! container_user_has claude; then
    log_section_skipped "Claude Code settings" \
      "claude is not installed, add the claude-code feature to devcontainer.json"
    return 0
  fi
  if ! container_user_has jq; then
    log_section_skipped "Claude Code settings" \
      "jq is not installed, add it to the apt-packages feature in devcontainer.json"
    return 0
  fi
  [ -s "${CLAUDE_SETTINGS_FILE}" ] \
    || exit_with_error "Claude Code settings not found at ${CLAUDE_SETTINGS_FILE}"
  jq empty "${CLAUDE_SETTINGS_FILE}" > /dev/null 2>&1 \
    || exit_with_error "${CLAUDE_SETTINGS_FILE} is not valid JSON"

  local target="${USER_HOME}/.claude/settings.json"
  log_section "Claude Code settings" \
    "$(jq -r 'keys | join(", ")' "${CLAUDE_SETTINGS_FILE}") -> ${target}"

  mkdir -p "$(dirname "${target}")"
  [ -f "${target}" ] || echo '{}' > "${target}"
  jq empty "${target}" > /dev/null 2>&1 \
    || exit_with_error "${target} is not valid JSON, so it cannot be merged into"

  local merged
  merged="$(jq -s '.[0] * .[1]' "${target}" "${CLAUDE_SETTINGS_FILE}")"
  printf '%s\n' "${merged}" > "${target}"

  local key
  while read -r key; do
    [ "$(jq -c --arg k "${key}" '.[$k]' "${target}")" \
      = "$(jq -c --arg k "${key}" '.[$k]' "${CLAUDE_SETTINGS_FILE}")" ] \
      || exit_with_error "'${key}' is not set in ${target} after the merge"
  done < <(jq -r 'keys[]' "${CLAUDE_SETTINGS_FILE}")

  log_section_done "Claude Code settings"
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

  for path in "${VSCODE_SERVER_DIR}" "${paths[@]}"; do
    as_container_user "test -w '${path}'" \
      || exit_with_error "$(printf '%s\n' \
        "${CONTAINER_USER} cannot write ${path}, so VS Code could not install its server into it." \
        ".devcontainer/Dockerfile creates ${VSCODE_SERVER_DIR} and every mount point under it owned by" \
        "${CONTAINER_USER}, and Docker copies that ownership into a volume only while the volume is empty." \
        "A volume that an earlier build left root-owned with content in it keeps that ownership: remove" \
        "it from the engine and build again.")"
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
  # Named, not left to `set -u`. Each of these comes from shell.env, and an
  # incomplete one is an ordinary operator mistake; without these guards the
  # script dies with "GIT_USER: unbound variable" and a line number, which
  # names neither the variable's source nor the fix. Checked here rather than
  # in main() so a CICD run, or a container with no git, is not required to
  # set variables it never uses.
  local name
  for name in GIT_USER GIT_USER_EMAIL GIT_PROVIDER_URL; do
    [ -n "${!name:-}" ] || exit_with_error "$(printf '%s\n' \
      "${name} is not set in the environment." \
      "It comes from shell.env; add it there, or to this instance's shell.env in" \
      "Parameter Store, then rebuild.")"
  done
  configure_git_shared "${CONTAINER_USER}" "${GIT_USER}" "${GIT_USER_EMAIL}"
  configure_git_credential_helper "${CONTAINER_USER}" "${GIT_PROVIDER_URL}"
  log_section_done "Git configuration"
}

install_git_hooks() {
  [ -d "${WORK_DIR}/.git" ] || exit_with_error "$(printf '%s\n' \
    "no .git directory at ${WORK_DIR}, so hooks cannot be installed." \
    "confirm the workspace was cloned into place before postCreate runs, then retry.")"

  log_section "Git hooks" "pre-commit and pre-push, via 'make hooks-install'"
  (cd "${WORK_DIR}" && make hooks-install) || exit_with_error "$(printf '%s\n' \
    "'make hooks-install' failed in ${WORK_DIR}." \
    "rerun 'make hooks-install' from ${WORK_DIR} once the failure above is resolved.")"
  # This script runs as root (postcreate-wrapper.sh's 'sudo -E ... bash'), so the
  # hooks 'make hooks-install' just wrote land root-owned inside a workspace that
  # belongs to CONTAINER_USER. Every other root-side workspace writer in this file
  # restores ownership the same way (declare_unclaimed_path, configure_repo_detection).
  chown -R "${CONTAINER_USER}:${CONTAINER_USER}" "${WORK_DIR}/.git/hooks"
  log_section_done "Git hooks"
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
  configure_claude_settings
  configure_tmux_commands
  configure_resmon_disks
  configure_vscode_settings_sync
  configure_oh_my_zsh
  configure_aws_profiles
  validate_proxy
  configure_git
  install_git_hooks
  configure_repo_detection
  fix_ownership
  report_warnings
  run_project_setup
}

main "$@"
