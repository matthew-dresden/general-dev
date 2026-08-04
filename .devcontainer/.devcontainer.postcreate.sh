#!/usr/bin/env bash
# Provisions the devcontainer with everything that depends on values only known
# at create time, secrets fetched from Parameter Store, the developer's git
# identity, the AWS profile map. Anything installable is a devcontainer.json
# feature instead, so this script configures rather than installs.
#
# Usage: .devcontainer.postcreate.sh <container-user>
#
# Every value below is overridable from the environment; nothing is fixed in
# code. Sections whose dependency is absent are skipped with a banner rather
# than aborting, so a container missing one feature still provisions.

set -euo pipefail

CONTAINER_USER="${1:?usage: $(basename "$0") <container-user>}"
WORK_DIR="$(pwd)"

# shellcheck source=devcontainer-functions.sh
source "${WORK_DIR}/.devcontainer/devcontainer-functions.sh"

############
# Settings #
############
: "${CICD:=false}"
: "${AWS_CONFIG_ENABLED:=true}"
: "${AWS_DEFAULT_OUTPUT:=json}"
: "${HOST_PROXY:=false}"
: "${HOST_PROXY_TIMEOUT:=10}"

: "${DEVCONTAINER_ZSH_THEME:=obraun}"
: "${DEVCONTAINER_ZSH_CORRECTION:=false}"
: "${DEVCONTAINER_ZSH_HIST_STAMPS:=%m/%d/%Y - %H:%M:%S}"
# Flags the ccd/ccdr aliases pass to the Claude CLI.
: "${DEVCONTAINER_CLAUDE_FLAGS:=--dangerously-skip-permissions}"
# Prepended to PATH for the container user and for project-setup.sh.
: "${DEVCONTAINER_EXTRA_PATH:=/usr/local/py-utils/bin:/usr/local/python/current/bin}"
: "${DEVCONTAINER_REPOS_DIR:=repos}"
: "${DEVCONTAINER_REPO_SCAN_IGNORE:=node_modules .venv venv __pycache__ .mypy_cache .pytest_cache .ruff_cache dist build target .next}"

###########
# Derived #
###########
# Read the home directory rather than assuming /home/<user>: the account is
# supplied by whatever base image devcontainer.json names, and its home is that
# image's decision.
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

# Where postAttachCommand looks for the script. devcontainer.json names this
# path literally, because a hook string cannot resolve it, so the two files have
# to agree: changing it here needs the same change there. remoteEnv in that file
# already puts this directory on PATH; nothing created it until now.
USER_BIN="${USER_HOME}/.local/bin"
RESMON_DISKS_HOOK="${USER_BIN}/$(basename "${RESMON_DISKS}")"

WARNINGS=()
is_cicd() { [ "${CICD,,}" = "true" ]; }

export PATH="${DEVCONTAINER_EXTRA_PATH}:${USER_BIN}:${PATH}"

############
# Sections #
############

# shell.env carries the developer's environment and is fetched from Parameter
# Store by the wrapper. bash reads .bashrc when interactive and BASH_ENV when
# not; zsh reads .zshenv for both.
configure_shell_env() {
  [ -f "${SHELL_ENV}" ] || exit_with_error "shell.env not found at ${SHELL_ENV}"
  log_section "Shell environment" "sourcing shell.env from bash and zsh"

  {
    echo "# Source project shell.env"
    echo "source \"${SHELL_ENV}\""
    echo "export BASH_ENV=\"${SHELL_ENV}\""
    echo "export PATH=\"\$HOME/.local/bin:\$PATH\""
  } >> "${BASH_RC}"

  {
    echo "source \"${SHELL_ENV}\""
    echo "export PATH=\"\$HOME/.local/bin:\$PATH\""
  } > "${ZSH_ENV}"

  log_section_done "Shell environment"
}

# Run a command as the container user, so anything it creates is correctly owned
# and anything it tests is tested with that account's permissions rather than
# root's. WSL cannot use sudo -u, so only the invocation differs between the two.
as_container_user() {
  if is_wsl; then
    bash -c "$1"
  else
    sudo -u "${CONTAINER_USER}" bash -c "$1"
  fi
}

# Interactive rc files that should carry aliases and functions. Written once
# and applied to each, so bash and zsh cannot drift apart.
interactive_rcs() {
  printf '%s\n' "${BASH_RC}"
  [ -f "${ZSH_RC}" ] && printf '%s\n' "${ZSH_RC}"
  return 0
}

# Depends on: claude-code feature.
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

# Depends on: tmux, from the apt-packages feature. The tm* helpers take
# arguments and --help, so they are functions in a sourced file, not aliases.
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
  # tmux reads this once, when the server starts. Copied rather than sourced
  # from the workspace so a session still comes up correctly if the checkout is
  # not mounted where it was built.
  install -m 644 "${TMUX_CONF}" "${USER_HOME}/.tmux.conf"

  # default-shell has to be an absolute path and the committed config cannot
  # know where zsh landed, so it is resolved here. Without this every window
  # opens the account's login shell, which is bash.
  local zsh_path
  zsh_path="$(container_user_path_to zsh)"
  [ -n "${zsh_path}" ] || exit_with_error \
    "zsh not found, so tmux would open bash. Enable installZsh on the common-utils feature."
  sed -i "s|^set -g default-shell .*|set -g default-shell ${zsh_path}|" "${USER_HOME}/.tmux.conf"
  grep -q "^set -g default-shell ${zsh_path}$" "${USER_HOME}/.tmux.conf" \
    || exit_with_error "could not set default-shell in ${USER_HOME}/.tmux.conf"

  log_section_done "tmux commands"
}

# Depends on: python3, from the python feature, and the script itself. Both are
# required rather than optional, so this section aborts where the ones above skip
# with a banner: devcontainer.json runs `python3 $HOME/.local/bin/resmon-disks.py`
# from postAttachCommand unconditionally, so skipping would leave a container
# whose every attach fails. A build that stops here says so once, with the
# remedy; the alternative reports it on every attach, after provisioning claimed
# to have succeeded.
#
# resmon-disks.py runs from postAttachCommand, and that hook's working directory
# is not the workspace on every route into the container. On the remote engine
# the workspace is a volume with no local path, so VS Code attaches to the
# container by name, and an attached container carries no workspaceFolder for the
# extension to run hooks in: it uses the home directory. A workspace-relative
# command therefore resolved to ~/.devcontainer/resmon-disks.py, and the hook
# died with exit 2 on every attach, taking the Resource Monitor's disk figures
# with it. Linking the script somewhere the hook can always name fixes it for
# both routes at once.
#
# Linked rather than copied, unlike .tmux.conf: tmux reads its config once when
# its server starts, which can be before the workspace is there, while this only
# ever runs on attach, when the workspace is mounted by definition. So there is
# one copy of the script, and editing it takes effect on the next attach.
configure_resmon_disks() {
  [ -f "${RESMON_DISKS}" ] || exit_with_error \
    "resmon script not found at ${RESMON_DISKS}, which postAttachCommand runs on every attach"

  # Resolved rather than run as a bare name: PATH under sudo is not the container
  # user's, and the answer is what proves the interpreter the hook names exists.
  local python
  python="$(container_user_path_to python3)"
  [ -n "${python}" ] || exit_with_error \
    "python3 not found, so postAttachCommand cannot run ${RESMON_DISKS_HOOK}. Add the python feature to devcontainer.json."

  log_section "Resource Monitor disks" "${RESMON_DISKS_HOOK} -> ${RESMON_DISKS}"

  install -d -m 755 "${USER_BIN}"
  ln -sfn "${RESMON_DISKS}" "${RESMON_DISKS_HOOK}"

  # Run it as the account the hook runs as, rather than assuming the link works:
  # that proves python3 resolves, the link resolves, and the file is readable by
  # that user, which root reading it does not. VS Code has not written its
  # settings this early, so the script says so and exits 0 without touching
  # anything; the first attach after this is what sets the drives.
  #
  # HOME is named rather than inherited because the script derives the settings
  # path from it, and the WSL branch of as_container_user does not go through
  # sudo, so it would inherit root's and check a file nothing ever reads.
  as_container_user "HOME='${USER_HOME}' '${python}' '${RESMON_DISKS_HOOK}'" \
    || exit_with_error \
      "${RESMON_DISKS_HOOK} does not run, so postAttachCommand would fail on every attach"

  log_section_done "Resource Monitor disks"
}

# Depends on: common-utils feature (installOhMyZsh) and a .zshrc to edit.
configure_oh_my_zsh() {
  if [ ! -d "${USER_HOME}/.oh-my-zsh" ] || [ ! -f "${ZSH_RC}" ]; then
    log_section_skipped "Oh My Zsh configuration" \
      "no ~/.oh-my-zsh or ~/.zshrc, enable installOhMyZsh on the common-utils feature"
    return 0
  fi
  log_section "Oh My Zsh configuration" "theme ${DEVCONTAINER_ZSH_THEME}"

  # The shipped .zshrc already exports ZSH and sources the framework. Settings
  # oh-my-zsh reads at load time are edited in place: appending them would apply
  # after it had loaded, and re-sourcing here would load it twice.
  sed -i \
    -e "s|^ZSH_THEME=.*|ZSH_THEME=\"${DEVCONTAINER_ZSH_THEME}\"|" \
    -e "s|^# *ENABLE_CORRECTION=.*|ENABLE_CORRECTION=\"${DEVCONTAINER_ZSH_CORRECTION}\"|" \
    -e "s|^# *HIST_STAMPS=.*|HIST_STAMPS=\"${DEVCONTAINER_ZSH_HIST_STAMPS}\"|" \
    "${ZSH_RC}"

  grep -q "^ZSH_THEME=\"${DEVCONTAINER_ZSH_THEME}\"" "${ZSH_RC}" \
    || exit_with_error "could not set ZSH_THEME in ${ZSH_RC}, the base image's .zshrc layout changed"
  log_section_done "Oh My Zsh configuration"
}

# Depends on: jq, and a profile map to read. An absent or empty map means there
# are no profiles to generate; malformed content is an error, not an absence.
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

# Only reachable when a host proxy is declared; validates it answers.
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

# Depends on: git. Identity comes from shell.env; the credential itself is
# supplied separately by `make push-git-creds`.
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

# Hand the home directory back to the container user: this script runs as root,
# so anything it wrote is root-owned until now.
fix_ownership() {
  log_info "Setting ownership of ${USER_HOME} to ${CONTAINER_USER}"
  chown -R "${CONTAINER_USER}:${CONTAINER_USER}" "${USER_HOME}"
}

# Runs as the container user so anything it creates is correctly owned; see
# as_container_user for how that is arranged on each platform.
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

########
# Main #
########
main() {
  log_info "Starting post-create setup as '${CONTAINER_USER}' (CICD=${CICD})"
  [ -n "${DEFAULT_GIT_BRANCH:-}" ] || exit_with_error "DEFAULT_GIT_BRANCH is not set in the environment"

  # Runs here rather than in the wrapper because writing /etc/apt needs root.
  # Nothing at provisioning time installs packages any more, but a developer
  # running apt by hand behind a host proxy needs this.
  configure_apt_proxy

  configure_shell_env
  configure_claude_aliases
  configure_tmux_commands
  configure_resmon_disks
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
