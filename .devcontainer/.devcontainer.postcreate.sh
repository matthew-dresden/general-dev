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
OPENCODE_CONFIG_FILE="${WORK_DIR}/.devcontainer/opencode.json"
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

# The hostcreds credential-startup block, rendered by devcontainer_config
# rather than hand-written here (the same render-once-in-Python rule the
# hooks and every other generated text in this script follow). The block is
# shell-agnostic, so one text serves bash and zsh, and it is rendered once
# and appended to both startup files configure_shell_env wires below.
render_hostcreds_shell_block() {
  PYTHONPATH="${DEVCONTAINER_SCRIPTS_DIR}" python3 -m devcontainer_config.cli shell-block
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

  # A container whose shells silently lack their pushed credentials is worse
  # than a container that failed to create, so a non-zero render is fatal
  # through exit_with_error rather than warned about or skipped.
  local startup_block
  startup_block="$(render_hostcreds_shell_block)" || exit_with_error "$(printf '%s\n' \
    "devcontainer_config.cli failed to render the hostcreds startup block." \
    "Rerun 'PYTHONPATH=${DEVCONTAINER_SCRIPTS_DIR} python3 -m devcontainer_config.cli shell-block'" \
    "from ${WORK_DIR} to see the underlying error.")"

  # Idempotent: a rebuild or a manual rerun of this function must not
  # duplicate the block. The rendered block's own first line is its marker
  # (devcontainer_config.hostcreds.MARKER); grepping each target startup
  # file for that line before appending, the same 'grep -q ... || <action>'
  # guard style used elsewhere in this file, is what makes a second run a
  # no-op instead of a second copy. One marker guards both files because
  # the same block text is appended to each.
  local startup_marker
  startup_marker="$(printf '%s\n' "${startup_block}" | head -n 1)"

  grep -qF -- "${startup_marker}" "${BASH_RC}" || printf '%s\n' "${startup_block}" >> "${BASH_RC}"
  grep -qF -- "${startup_marker}" "${ZSH_ENV}" || printf '%s\n' "${startup_block}" >> "${ZSH_ENV}"

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

# opencode, the one CLI here no devcontainer feature ships. Installed as the
# container user into the prefix configure_npm_global_ownership just handed
# over, so the package and its bin symlink are user-owned from the start and
# self-update never needs the handover to be re-run. The committed config
# (.devcontainer/opencode.json) carries no credential: each provider block
# injects its key through a {env:...} interpolation, which the hostcreds
# startup block exports from the fragments 'make push-creds' wrote for the
# manifest's keychain entries.
#
# The installed copy is resolved from the shipped one at provisioning time,
# in three steps jq applies in one pass:
#   - the ${WORK_DIR} placeholder in the MCP wrapper commands becomes the
#     real workspace root. The placeholder is the only shape a committed
#     JSON can carry: the committed wrappers live at
#     <workspace>/.devcontainer/opencode/bin/ and the workspace root is
#     known only at container runtime, so the shipped config names them
#     through the literal token and this function is the single place the
#     token is expanded (on the installed copy, never on the committed file,
#     which stays environment-agnostic).
#   - enabled_providers is written from the ZAI_ENABLED / OPENROUTER_ENABLED
#     toggles (0/1 only; anything else aborts, naming the toggle and where
#     it is set).
#   - model/small_model follow the matrix: both toggles on -> the zai-max
#     flagship + flash pair; openrouter only -> the openrouter pair; zai
#     only -> the shipped zai-max defaults unchanged; both off -> the model
#     keys removed entirely.
configure_opencode() {
  if ! container_user_has npm; then
    log_section_skipped "opencode" \
      "npm is not installed, add the node feature to devcontainer.json"
    return 0
  fi
  [ -s "${OPENCODE_CONFIG_FILE}" ] \
    || exit_with_error "opencode config not found at ${OPENCODE_CONFIG_FILE}"
  jq empty "${OPENCODE_CONFIG_FILE}" > /dev/null 2>&1 \
    || exit_with_error "${OPENCODE_CONFIG_FILE} is not valid JSON"

  local toggle value
  for toggle in ZAI_ENABLED OPENROUTER_ENABLED; do
    value="${!toggle:-}"
    case "${value}" in
      0|1) ;;
      *) exit_with_error "$(printf '%s\n' \
        "${toggle} is '${value:-unset}', but only 0 (disabled) or 1 (enabled) is accepted." \
        "It is set by the containerEnv block in .devcontainer/devcontainer.json, which" \
        "reads the host environment of the same name; set ${toggle}=0 or ${toggle}=1" \
        "on the host (or change the devcontainer.json default), then rebuild.")" ;;
    esac
  done

  local user_path="${CONTAINER_USER_PATH:-${PATH}}"
  local installed="${USER_HOME}/.config/opencode"
  local target="${installed}/opencode.json"
  log_section "opencode" "npm install, config -> ${target}"

  as_container_user "PATH='${user_path}' npm install --global opencode-ai" \
    || exit_with_error "npm install --global opencode-ai failed, so opencode is not installed"
  as_container_user "PATH='${user_path}' opencode --version" > /dev/null \
    || exit_with_error "opencode is installed but does not run"

  # g-whiz, the gcp MCP server's binary: the shipped config runs it straight
  # from PATH, so it is installed globally beside opencode rather than
  # wrapped.
  as_container_user "PATH='${user_path}' npm install --global g-whiz" \
    || exit_with_error "npm install --global g-whiz failed, so the gcp MCP server has no binary"

  local resolved_dir
  resolved_dir="$(mktemp -d)" \
    || exit_with_error "mktemp failed while preparing the resolved opencode config"
  jq --arg workdir "${WORK_DIR}" \
     --arg zai "${ZAI_ENABLED}" \
     --arg openrouter "${OPENROUTER_ENABLED}" '
    walk(if type == "string" then gsub("\\$\\{WORK_DIR\\}"; $workdir) else . end)
    | if $zai == "1" and $openrouter == "1" then
        .model = "zai-max/glm-5.3"
        | .small_model = "zai-max/glm-5.3-flash"
        | .enabled_providers = ["zai-max", "openrouter"]
      elif $zai == "1" then
        .enabled_providers = ["zai-max"]
      elif $openrouter == "1" then
        .model = "openrouter/glm-5.3"
        | .small_model = "openrouter/glm-5.3-flash"
        | .enabled_providers = ["openrouter"]
      else
        del(.model) | del(.small_model) | del(.enabled_providers)
      end
  ' "${OPENCODE_CONFIG_FILE}" > "${resolved_dir}/opencode.json" \
    || exit_with_error "jq failed to resolve ${OPENCODE_CONFIG_FILE} into the installed copy"

  # No credential beyond the {env:...} interpolations may reach the installed
  # config: every apiKey it carries must be an interpolation (the count of
  # apiKey keys equals the count of interpolated ones), and the two shipped
  # provider blocks must each carry exactly the documented one. A literal key
  # committed to the shipped config, or one surviving a bad rewrite, aborts
  # here instead of installing.
  # grep -c exits 1 (not 2) when nothing matched, which would end this script
  # under set -e before the count check below could name the problem; the
  # assignment fallbacks route a zero count into the explicit check instead.
  local api_key_keys api_key_interpolations
  api_key_keys="$(grep -c '"apiKey":' "${resolved_dir}/opencode.json")" || api_key_keys=0
  api_key_interpolations="$(grep -c '"apiKey": *"{env:[A-Z][A-Z0-9_]*}"' "${resolved_dir}/opencode.json")" \
    || api_key_interpolations=0
  [ "${api_key_keys}" -gt 0 ] && [ "${api_key_keys}" = "${api_key_interpolations}" ] \
    || exit_with_error "$(printf '%s\n' \
      "the resolved opencode config carries an apiKey that is not a {env:...} interpolation." \
      "The installed config may hold no credential but an interpolation; check" \
      "${OPENCODE_CONFIG_FILE}'s provider options.")"
  jq -e '.provider["zai-max"].options.apiKey == "{env:ZAI_API_KEY}" and .provider["openrouter"].options.apiKey == "{env:OPENROUTER_API_KEY}"' \
      "${resolved_dir}/opencode.json" > /dev/null \
    || exit_with_error "$(printf '%s\n' \
      "the resolved opencode config does not interpolate ZAI_API_KEY and OPENROUTER_API_KEY." \
      "The key reaches opencode only through those interpolations; a config without" \
      "them would carry no credential path at all.")"

  # Matrix-integrity: every model id the jq matrix wrote (or the shipped
  # zai-only branch kept) must resolve inside the config it landed in. When
  # model or small_model is present it must parse as <provider>/<model> with
  # that provider in enabled_providers and that model defined under
  # provider.<provider>.models; the both-off branch (no model keys) passes.
  # A dangling reference aborts naming the key and the id, instead of
  # installing a config whose default model no enabled provider can serve.
  local matrix_assert='def resolves($cfg; $ref):
  ($ref | test("^[^/]+/.+$"))
  and (($ref | capture("^(?<p>[^/]+)/(?<m>.+)$")) as $parts
      | (($cfg.enabled_providers // []) | index($parts.p)) != null
      and $cfg.provider[$parts.p].models[$parts.m] != null);
[ "model", "small_model" ] as $keys
| [ $keys[] as $key
    | select(.[$key] != null)
    | select((resolves(.; .[$key]) | not))
    | $key + "=" + .[$key] ] as $dangling
| if ($dangling | length) == 0
  then true
  else ($dangling | join("; ")), false
  end'
  local matrix_result
  matrix_result="$(jq -e "${matrix_assert}" "${resolved_dir}/opencode.json")" \
    || exit_with_error "$(printf '%s\n' \
      "the resolved opencode config carries a dangling model reference: ${matrix_result%%$'\n'*}." \
      "When model or small_model is present it must parse as <provider>/<model> with that" \
      "provider in enabled_providers and that model defined under provider.<provider>.models;" \
      "check the jq matrix above and the shipped defaults in ${OPENCODE_CONFIG_FILE}.")"

  install -d -m 755 -o "${CONTAINER_USER}" -g "${CONTAINER_USER}" "$(dirname "${target}")"
  install -m 644 -o "${CONTAINER_USER}" -g "${CONTAINER_USER}" \
    "${resolved_dir}/opencode.json" "${target}"

  # Install-integrity: the installed bytes are compared against the resolved
  # bytes, so truncation or drift between resolution and install aborts
  # instead of leaving a half-written config that jq would parse but opencode
  # would silently misread. (The old spot check grepped one marker string; a
  # config that lost everything else around it passed.)
  cmp -s "${target}" "${resolved_dir}/opencode.json" \
    || exit_with_error "the installed ${target} does not match the post-jq expected bytes"
  rm -rf "${resolved_dir}"

  # The agent generator ships in the workspace: its lib/, its inputs, the
  # model-profiles plugin it feeds, and its self-test suites are copied into
  # the installed config, where the generator's own ROOT resolution places
  # agents in ~/.config/opencode/agent and the manifest in
  # ~/.config/opencode/lib/generated-agents.json. A generator failure leaves
  # opencode without its agents, so it is fatal, not a warning.
  local opencode_src="${WORK_DIR}/.devcontainer/opencode"
  [ -f "${opencode_src}/lib/generate-agents.mjs" ] \
    || exit_with_error "the committed agent generator is missing at ${opencode_src}/lib/generate-agents.mjs"
  install -d -m 755 -o "${CONTAINER_USER}" -g "${CONTAINER_USER}" \
    "${installed}/lib" "${installed}/plugins" "${installed}/tests" "${installed}/agent"
  cp -R "${opencode_src}/lib/." "${installed}/lib/"
  cp -R "${opencode_src}/tests/." "${installed}/tests/"
  install -m 644 "${opencode_src}/plugins/model-profiles.js" "${installed}/plugins/model-profiles.js"
  chown -R "${CONTAINER_USER}:${CONTAINER_USER}" "${installed}"

  as_container_user "PATH='${user_path}' node '${installed}/lib/generate-agents.mjs'" \
    || exit_with_error "$(printf '%s\n' \
      "the agent generator failed, so opencode has no generated agents." \
      "Rerun 'node ${installed}/lib/generate-agents.mjs' as ${CONTAINER_USER} to see the error.")"

  notify_opencode_mcp_credentials

  log_section_done "opencode"
}

# MCP credential notices: opencode starts an MCP server lazily, so a wrapper
# whose credential source is absent fails only when a session first reaches
# for it. The sources the shipped config depends on are probed here -- the
# SSM reads fetch a parameter's NAME only, never its decrypted value -- and
# every miss becomes a warning naming the documented enable step, so the gap
# is visible at provisioning time instead of mid-session.
notify_opencode_mcp_credentials() {
  # Provider keys: a toggle at 1 whose key variable is absent from the
  # environment means the hostcreds fragment named after the variable did
  # not export it, so the provider would load with an empty key and fail at
  # first request. One warning per gap, naming the toggle, the expected
  # fragment name and the remedy; never fatal, since provisioning may
  # legitimately run before the operator pushed the credentials.
  local -a provider_toggles=("ZAI_ENABLED" "OPENROUTER_ENABLED")
  local -a provider_keys=("ZAI_API_KEY" "OPENROUTER_API_KEY")
  local i
  for i in "${!provider_toggles[@]}"; do
    if [ "${!provider_toggles[$i]:-}" = "1" ] && [ -z "${!provider_keys[$i]:-}" ]; then
      WARNINGS+=("the ${provider_toggles[$i]} toggle is 1 but ${provider_keys[$i]} is absent from the environment: the hostcreds manifest fragment named ${provider_keys[$i]} did not export it. Remedy: add the ${provider_keys[$i]} entry to the gitignored hostcreds manifest, run 'make creds-init', then 'make push-creds'.")
    fi
  done

  # One SSM probe per wrapper-backed MCP: the read asks for the parameter's
  # NAME only (no --with-decryption), so no credential value is ever read,
  # and a failure means the parameter or the SSO session backing it is
  # absent -- the same failure the wrapper itself would hit at first use.
  local mcps=() params=()
  if container_user_has aws && [ -n "${AWS_REGION:-}" ]; then
    if [ -n "${SSM_ORG_PREFIX:-}" ]; then
      mcps=("github" "azure-devops")
      params=("${SSM_ORG_PREFIX}/github/github-app/app-id" "${SSM_ORG_PREFIX}/azure-devops/opencode-pat")
    else
      WARNINGS+=("the github and azure-devops MCPs' credential sources are absent: SSM_ORG_PREFIX is not set in shell.env (the gitignored host config), so the wrappers cannot read their parameters. Enable step: set SSM_ORG_PREFIX in shell.env and rebuild.")
    fi
    if [ -n "${SSM_CONTEXT7_PREFIX:-}" ]; then
      mcps+=("context7")
      params+=("${SSM_CONTEXT7_PREFIX}/api-key")
    else
      WARNINGS+=("the context7 MCP's credential source is absent: SSM_CONTEXT7_PREFIX is not set in shell.env (the gitignored host config). Enable step: set SSM_CONTEXT7_PREFIX in shell.env and rebuild.")
    fi
    local i
    for i in "${!mcps[@]}"; do
      as_container_user "PATH='${DEVCONTAINER_EXTRA_PATH}:${USER_BIN}:${PATH}' \
        aws ssm get-parameter --region '${AWS_REGION}' --name '${params[$i]}' \
        --query Parameter.Name --output text" > /dev/null 2>&1 \
        || WARNINGS+=("the ${mcps[$i]} MCP's credential source is absent: the parameter ${params[$i]} is not readable in ${AWS_REGION} (the read is SSO-gated). Enable step: run 'aws sso login' on the host, then 'make push-creds', and rebuild.")
    done
  else
    WARNINGS+=("the github, azure-devops and context7 MCPs' credential sources could not be probed: the aws CLI or AWS_REGION is unavailable in this build. Enable step: set AWS_REGION in shell.env and rebuild.")
  fi

  if [ ! -d "${USER_HOME}/.config/gws" ]; then
    WARNINGS+=("the gws MCP's credential source is absent: no gws OAuth state at ${USER_HOME}/.config/gws. Enable step: run the gws CLI's auth command ('gws --help' lists it) in the container and follow the browser flow; the refresh token is stored encrypted there.")
  fi

  if [ ! -d "${USER_HOME}/.config/gcloud" ]; then
    WARNINGS+=("the gcp MCP's credential source is absent: no gcloud login state at ${USER_HOME}/.config/gcloud. Enable step: run 'gcloud auth login' in the container (gcloud ships in the image) and set the project with 'gcloud config set project <project>'.")
  fi

  if [ ! -d "${USER_HOME}/.azure" ]; then
    WARNINGS+=("the azure MCP's credential source is absent: no az login state at ${USER_HOME}/.azure. Enable step: run 'az login' in the container.")
  fi
}

# The opencode aliases, in the same interactive-rc shape as ccd/ccdr:
# ocd opens a session, ocdr resumes one.
configure_opencode_aliases() {
  if ! container_user_has opencode; then
    log_section_skipped "opencode aliases" \
      "opencode is not installed, add the node feature to devcontainer.json"
    return 0
  fi
  log_section "opencode aliases" "ocd, ocdr"
  local rc
  while read -r rc; do
    {
      echo "alias ocd='opencode'"
      echo "alias ocdr='opencode --resume'"
    } >> "${rc}"
  done < <(interactive_rcs)
  log_section_done "opencode aliases"
}

# The github MCP's server binary. The wrapper resolves it from PATH and then
# from ${USER_BIN}; postcreate downloads the release into ${USER_BIN} so the
# MCP works out of the box. The version comes from the environment
# (GITHUB_MCP_SERVER_VERSION in shell.env, the gitignored host config) rather
# than a hardcoded value, so an update is an edit there plus a rebuild, and
# the download is skipped when the binary already exists (a manual rerun of
# provisioning, since a rebuild starts from an empty home).
configure_github_mcp_server() {
  if [ -x "${USER_BIN}/github-mcp-server" ]; then
    log_section_skipped "github-mcp-server" "already present at ${USER_BIN}/github-mcp-server"
    return 0
  fi
  [ -n "${GITHUB_MCP_SERVER_VERSION:-}" ] || exit_with_error "$(printf '%s\n' \
    "GITHUB_MCP_SERVER_VERSION is not set, so the github MCP's server binary cannot" \
    "be downloaded. Add the version (a github/github-mcp-server release tag, without" \
    "the leading v) to shell.env (the gitignored host config), then rebuild.")"
  local arch
  arch="$(dpkg --print-architecture)" \
    || exit_with_error "dpkg did not report the container architecture, so the github-mcp-server release asset cannot be selected"
  case "${arch}" in
    amd64|arm64) ;;
    *) exit_with_error "github-mcp-server has no release asset for architecture '${arch}'" ;;
  esac

  log_section "github-mcp-server" "v${GITHUB_MCP_SERVER_VERSION} -> ${USER_BIN}"
  local tarball_url="https://github.com/github/github-mcp-server/releases/download/v${GITHUB_MCP_SERVER_VERSION}/github-mcp-server_${GITHUB_MCP_SERVER_VERSION}_linux_${arch}.tar.gz"
  local staging
  staging="$(mktemp -d)" \
    || exit_with_error "mktemp failed while preparing the github-mcp-server download"
  curl --fail --show-error --silent --tlsv1.2 \
    --output "${staging}/github-mcp-server.tar.gz" "${tarball_url}" \
    || exit_with_error "downloading ${tarball_url} failed; check that GITHUB_MCP_SERVER_VERSION ('${GITHUB_MCP_SERVER_VERSION}') names a github/github-mcp-server release with a linux_${arch} asset"
  tar -xzf "${staging}/github-mcp-server.tar.gz" -C "${staging}" github-mcp-server \
    || exit_with_error "the github-mcp-server release tarball does not carry the github-mcp-server binary"
  install -d -m 755 "${USER_BIN}"
  install -m 755 "${staging}/github-mcp-server" "${USER_BIN}/github-mcp-server"
  rm -rf "${staging}"
  "${USER_BIN}/github-mcp-server" --version > /dev/null 2>&1 \
    || exit_with_error "the downloaded github-mcp-server binary does not run; check GITHUB_MCP_SERVER_VERSION ('${GITHUB_MCP_SERVER_VERSION}') against the release's asset list"
  chown "${CONTAINER_USER}:${CONTAINER_USER}" "${USER_BIN}/github-mcp-server"
  log_section_done "github-mcp-server"
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

  # The per-user seam: .devcontainer/project-setup.local.sh (gitignored,
  # authored per developer from its committed .example) is sourced in this
  # same environment context AFTER the project script, so its overrides --
  # model/default-agent, MCP toggles, provider keys it re-reads from the
  # hostcreds exports -- win over anything the project script did.
  local local_setup="${WORK_DIR}/.devcontainer/project-setup.local.sh"

  local command="export WORK_DIR='${WORK_DIR}'
export PATH='${DEVCONTAINER_EXTRA_PATH}:${USER_BIN}:'\"\$PATH\"
source '${SHELL_ENV}'
cd '${WORK_DIR}'
BASH_ENV='${FUNCTIONS_FILE}' bash '${PROJECT_SETUP}'
if [ -f '${local_setup}' ]; then
  . '${local_setup}'
fi"

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
  configure_opencode
  configure_opencode_aliases
  configure_github_mcp_server
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
