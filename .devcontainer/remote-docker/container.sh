#!/usr/bin/env bash

set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

rd_load_config

REPO_ROOT="$(cd "${RD_DIR}/../.." && pwd)"
: "${PROJECT_NAME:=$(basename "$REPO_ROOT")}"
# 'vscode' is the Dev Containers extension's engine-wide server cache, mounted
# at /vscode into containers the extension creates itself; it belongs to every
# project on the engine, not this one.
: "${SHARED_VOLUMES:=minikube-config vscode}"
: "${VSCODE_SERVER_DIRNAME:=.vscode-server}"
: "${VSCODE_SERVER_CACHE_SUBDIR:=bin}"
: "${CONTAINER_USER:=vscode}"
: "${CONTAINER_UID_GID:=1000:1000}"
: "${CLONE_IMAGE:=mcr.microsoft.com/devcontainers/base:noble}"
: "${DEVCONTAINER_CLI:=devcontainer}"
: "${DEVCONTAINER_SSM_PREFIX:=/devcontainer/${PROJECT_NAME}}"
: "${CONTAINER_WORKSPACES_ROOT:=/workspaces}"
CONTAINER_WORKSPACE="${CONTAINER_WORKSPACES_ROOT}/${PROJECT_NAME}"
RDC_OVERRIDE_CONFIG=""

rdc_read_configuration() {
  rd_require_cmd "$DEVCONTAINER_CLI" "Install it: npm install -g @devcontainers/cli"
  rd_require_cmd jq "Install jq: 'brew install jq' or 'apt-get install jq'"

  local config="${REPO_ROOT}/.devcontainer/devcontainer.json"
  [ -f "$config" ] || rd_fail "There is no devcontainer configuration at ${config}" \
    "Every operation resolves the workspace path from it, so there is nothing to act on." \
    "" \
    "This ran against ${REPO_ROOT}. Run it from the repository that owns the" \
    ".devcontainer directory, or point PROJECT_NAME at the right one."

  local resolved errors reported status=0
  errors="$(mktemp "${TMPDIR:-/tmp}/rdc-read-config.XXXXXX")"
  resolved="$("$DEVCONTAINER_CLI" read-configuration --workspace-folder "$REPO_ROOT" 2> "$errors")" || status=$?
  reported="$(cat "$errors")"
  rm -f "$errors"

  [ "$status" -eq 0 ] || rd_fail "The devcontainer CLI could not read ${config}" \
    "The file is there and it still exited ${status}, so it is one of: a syntax error," \
    "a feature or template reference it cannot resolve, or a file it is not allowed" \
    "to read. The CLI is terse about which." \
    "" \
    "Check that it parses:      ${RD_BOLD}make lint-json${RD_RESET}" \
    "Check that it is readable: ${RD_BOLD}ls -l ${config}${RD_RESET}" \
    "" \
    "devcontainer reported:" \
    "$(rd_quote "${reported:-nothing on stderr}")"

  [ -n "$resolved" ] || rd_fail "The devcontainer CLI read ${config} but returned nothing" \
    "It exited 0 with no configuration on stdout, which leaves nothing to build from." \
    "" \
    "devcontainer reported:" \
    "$(rd_quote "${reported:-nothing on stderr}")"

  printf '%s\n' "$resolved"
}

rdc_workspace_folder() {
  if [ -n "${RDC_WORKSPACE_FOLDER:-}" ]; then
    printf '%s\n' "$RDC_WORKSPACE_FOLDER"
    return 0
  fi

  local config="${REPO_ROOT}/.devcontainer/devcontainer.json"

  local resolved status=0
  resolved="$(rdc_read_configuration)" || status=$?
  [ "$status" -eq 0 ] || exit "$status"

  RDC_WORKSPACE_FOLDER="$(printf '%s' "$resolved" | jq -r '.configuration.workspaceFolder // empty')"
  [ -n "$RDC_WORKSPACE_FOLDER" ] || rd_fail "${config} does not set workspaceFolder" \
    "It parses, but without that key there is no path inside the container to open," \
    "clone into, or run commands in, and guessing one would silently target the wrong" \
    "directory." \
    "" \
    "Add it to the config:" \
    "  ${RD_BOLD}\"workspaceFolder\": \"${CONTAINER_WORKSPACES_ROOT}/\${localWorkspaceFolderBasename}\"${RD_RESET}"

  printf '%s\n' "$RDC_WORKSPACE_FOLDER"
}

rdc_exec() {
  local id="$1"; shift
  rd_docker exec -u "$CONTAINER_USER" "$id" "$@"
}

rdc_exec_probe() {
  local id="$1"; shift
  docker exec -u "$CONTAINER_USER" "$id" "$@"
}

rdc_cred_user() { printf '%s' "$1" | sed -n 1p; }
rdc_cred_secret() { printf '%s' "$1" | sed -n 2p; }

rdc_backend() {
  if [ "$(docker context show)" = "$REMOTE_DOCKER_CONTEXT" ]; then
    printf 'remote\n'
  else
    printf 'local\n'
  fi
}

rdc_require_docker() {
  rd_require_cmd docker "Install the docker CLI: https://docs.docker.com/engine/install/"
  docker info > /dev/null 2>&1 && return 0

  local context diagnosis
  context="$(docker context show 2> /dev/null || printf 'unknown')"
  diagnosis="$(rd_engine_diagnosis "$context")"
  rd_fail "The docker engine behind context '${context}' is not reachable" \
    "Cause   $(rd_line 1 "$diagnosis")" \
    "Fix     ${RD_BOLD}$(rd_line 2 "$diagnosis")${RD_RESET}"
}

rdc_container_ids() {
  rd_docker ps -aq --filter "label=devcontainer.project=${PROJECT_NAME}"
}

rdc_require_container() {
  local ids count context
  context="$(docker context show 2> /dev/null || printf 'unknown')"
  if [ -n "${CONTAINER:-}" ]; then
    docker inspect -f '{{.Id}}' "$CONTAINER" 2> /dev/null \
      || rd_fail "There is no container named '${CONTAINER}' on context '${context}'" \
        "CONTAINER selects an instance by name or id, and nothing here answers to that one." \
        "" \
        "What this engine has:  ${RD_BOLD}make status${RD_RESET}" \
        "" \
        "Containers built on the other engine are invisible from here; switch with" \
        "${RD_BOLD}make local${RD_RESET} or ${RD_BOLD}make remote${RD_RESET} if you are pointed at the wrong one."
    return 0
  fi
  ids="$(rdc_container_ids)" || exit $?
  [ -n "$ids" ] || rd_fail "No container for project '${PROJECT_NAME}' exists on context '${context}'" \
    "Build one:" \
    "  ${RD_BOLD}make up${RD_RESET}      builds it, starts it and opens it" \
    "  ${RD_BOLD}make build${RD_RESET}   builds it and stops there" \
    "" \
    "If you expected one to be here already, it may be on the other engine:" \
    "${RD_BOLD}make local${RD_RESET} or ${RD_BOLD}make remote${RD_RESET}, then ${RD_BOLD}make status${RD_RESET}."
  count="$(printf '%s\n' "$ids" | wc -l | tr -d ' ')"
  if [ "$count" -gt 1 ]; then
    rd_fail "${count} instances of '${PROJECT_NAME}' exist, so this cannot act on one of them" \
      "Every instance of a repository carries the same project label, and picking for you" \
      "could stop or destroy the wrong checkout." \
      "" \
      "$(docker ps -a --filter "label=devcontainer.project=${PROJECT_NAME}" \
        --format '{{.Names}}  [{{.State}}]  {{.CreatedAt}}')" \
      "" \
      "Name the one you mean:" \
      "  ${RD_BOLD}make ${RDC_COMMAND:-<target>} CONTAINER=<name>${RD_RESET}"
  fi
  printf '%s\n' "$ids"
}

rdc_container_state() {
  rd_docker inspect "$1" --format '{{.State.Status}}'
}

rdc_container_name() {
  rd_docker inspect "$1" --format '{{.Name}}' | sed 's|^/||'
}

rdc_project_volumes() {
  local id="$1" vol target
  rd_docker inspect "$id" \
    --format '{{range .Mounts}}{{if eq .Type "volume"}}{{.Name}} {{.Destination}}{{"\n"}}{{end}}{{end}}' \
    | while read -r vol target; do
        [ -n "$vol" ] || continue
        case " ${SHARED_VOLUMES} " in
          *" ${vol} "*) continue ;;
        esac
        case "$target" in
          */"${VSCODE_SERVER_DIRNAME}"/*) continue ;;
        esac
        printf '%s\n' "$vol"
      done
}

rdc_status() {
  local ids id context
  context="$(docker context show 2>/dev/null || echo unknown)"
  printf '\n\033[1mProject\033[0m        %s\n' "$PROJECT_NAME"
  if [ "$context" = "$REMOTE_DOCKER_CONTEXT" ]; then
    printf '\033[1mBackend\033[0m        remote, context %s (%s)\n' "$context" "$REMOTE_INSTANCE_ID"
  else
    printf '\033[1mBackend\033[0m        local, context %s\n' "$context"
  fi

  if ! docker info > /dev/null 2>&1; then
    local diagnosis
    diagnosis="$(rd_engine_diagnosis "$context")"
    printf '%sEngine%s         %snot reachable%s\n' "$RD_BOLD" "$RD_RESET" "$RD_RED" "$RD_RESET"
    printf '%sLikely cause%s   %s\n' "$RD_BOLD" "$RD_RESET" "$(rd_line 1 "$diagnosis")"
    printf '%sFix%s            %s\n\n' "$RD_BOLD" "$RD_RESET" "$(rd_line 2 "$diagnosis")"
    return 1
  fi

  ids="$(rdc_container_ids)"
  if [ -z "$ids" ]; then
    printf '\033[1mContainer\033[0m      none, create with Dev Containers: Clone Repository in Container Volume...\n\n'
    return 0
  fi
  local name state image volumes
  for id in $ids; do
    name="$(rdc_container_name "$id")"
    state="$(rdc_container_state "$id")"
    image="$(rd_docker inspect "$id" --format '{{.Config.Image}}')"
    volumes="$(rdc_project_volumes "$id" | tr '\n' ' ')"
    printf '\n\033[1mContainer\033[0m      %s  [%s]\n' "$name" "$state"
    printf '\033[1mImage\033[0m          %s\n' "$image"
    printf '\033[1mVolumes\033[0m        %s\n' "$volumes"
  done
  printf '\n'
}

rdc_rename() {
  local id old
  [ -n "${NAME:-}" ] || rd_die "NAME is required, e.g.: make rename NAME=general-dev-review"
  id="$(rdc_require_container)"
  old="$(rdc_container_name "$id")"
  [ "$old" != "$NAME" ] || rd_die "container is already named '${NAME}'"
  rd_docker rename "$id" "$NAME"
  rd_ok "renamed ${old} -> ${NAME}"
}

rdc_start() {
  local id state
  id="$(rdc_require_container)"
  state="$(rdc_container_state "$id")"
  if [ "$state" = "running" ]; then
    rd_ok "container already running"
    return 0
  fi
  rd_log "starting container..."
  rd_docker start "$id" > /dev/null
  rd_ok "started, reconnect with Dev Containers: Attach to Running Container..."
}

rdc_stop() {
  local id state
  id="$(rdc_require_container)"
  state="$(rdc_container_state "$id")"
  if [ "$state" != "running" ]; then
    rd_ok "container already stopped (state: ${state})"
    return 0
  fi
  rd_log "stopping container..."
  rd_docker stop "$id" > /dev/null
  rd_ok "stopped, the workspace volume is untouched; 'make start' resumes it"
}

rdc_restart() {
  local id
  id="$(rdc_require_container)"
  rd_log "restarting container..."
  rd_docker restart "$id" > /dev/null
  rd_ok "restarted, reconnect with Dev Containers: Attach to Running Container..."
}

rdc_check() {
  local id state dirty ahead
  if [ "$(rdc_backend)" != "remote" ]; then
    rd_ok "local backend, the container shares this working tree, nothing to check"
    return 0
  fi
  id="$(rdc_require_container)"
  state="$(rdc_container_state "$id")"
  if [ "$state" != "running" ]; then
    rd_log "container is '${state}', starting it to inspect the checkout"
    rd_docker start "$id" > /dev/null
  fi

  dirty="$(rdc_exec "$id" git -C "${CONTAINER_WORKSPACE}" status --porcelain)"
  ahead="$(rdc_exec_probe "$id" git -C "${CONTAINER_WORKSPACE}" log --oneline '@{upstream}..HEAD' 2> /dev/null || true)"

  if [ -z "$dirty" ] && [ -z "$ahead" ]; then
    rd_ok "checkout in the volume is clean and pushed, safe to destroy"
    return 0
  fi
  local lines
  lines=()
  [ -z "$ahead" ] || lines+=("unpushed commits:" "$(rd_quote "$ahead")" "")
  [ -z "$dirty" ] || lines+=("uncommitted changes:" "$(rd_quote "$dirty")" "")
  lines+=(
    "A rebuild re-clones from origin, so none of this comes back. Push it from inside"
    "the container, then retry:"
    "  ${RD_BOLD}docker exec -u ${CONTAINER_USER} $(rdc_container_name "$id") git -C ${CONTAINER_WORKSPACE} push${RD_RESET}"
    ""
    "Or destroy it deliberately:  ${RD_BOLD}make clean FORCE=1${RD_RESET}"
  )
  rd_fail "The volume holds work that exists nowhere else" "${lines[@]}"
}

rdc_branch() {
  git -C "$REPO_ROOT" rev-parse --abbrev-ref HEAD
}

rdc_branch_missing_on_origin() {
  local branch="$1" default_branch="$2"
  rd_fail "Branch '${branch}' does not exist on origin" \
    "The container is built by cloning from origin, not from this machine, so the" \
    "branch has to be there first." \
    "" \
    "${RD_BOLD}Push this branch${RD_RESET} and build from it:" \
    "    git push -u origin ${branch}" \
    "    make up" \
    "" \
    "${RD_BOLD}Or switch to ${default_branch}${RD_RESET} and build from that instead:" \
    "    git checkout ${default_branch}" \
    "    make up"
}

rdc_workspace_volume() {
  printf '%s-%s\n' "$PROJECT_NAME" "$(rdc_branch | tr '/' '-')"
}

rdc_build_prereqs() {
  rd_require_cmd "$DEVCONTAINER_CLI" "Install it: npm install -g @devcontainers/cli"
  rd_require_cmd git "Install git."
  rd_require_cmd jq "Install jq: 'brew install jq' or 'apt-get install jq'"
  rd_require_cmd python3 "Install python3 (used to compare timestamps)."

  [ "$(rdc_backend)" = "remote" ] || return 0

  local branch default_branch
  branch="$(rdc_branch)"
  git -C "$REPO_ROOT" fetch --quiet origin "$branch" 2> /dev/null || true
  if ! git -C "$REPO_ROOT" rev-parse --verify --quiet "origin/${branch}" > /dev/null; then
    default_branch="$(git -C "$REPO_ROOT" symbolic-ref --quiet --short refs/remotes/origin/HEAD 2> /dev/null | sed 's|^origin/||' || true)"
    rdc_branch_missing_on_origin "$branch" "${default_branch:-main}"
  fi

  local unpushed
  unpushed="$(git -C "$REPO_ROOT" log --oneline "origin/${branch}..HEAD" 2> /dev/null || true)"
  if [ -n "$unpushed" ] && [ "${FORCE:-0}" != "1" ]; then
    rd_fail "$(printf '%s\n' "$unpushed" | wc -l | tr -d ' ') commit(s) are not on origin/${branch}" \
      "The container is cloned from origin, so these would simply not be in it:" \
      "" \
      "$(rd_quote "$unpushed")" \
      "" \
      "Push them:" \
      "  ${RD_BOLD}git push origin ${branch}${RD_RESET}" \
      "" \
      "Or build without them, deliberately:  ${RD_BOLD}make build FORCE=1${RD_RESET}"
  fi

  local dirty_config
  dirty_config="$(git -C "$REPO_ROOT" status --porcelain -- .devcontainer)"
  if [ -n "$dirty_config" ] && [ "${FORCE:-0}" != "1" ]; then
    rd_fail ".devcontainer has uncommitted changes" \
      "$(rd_quote "$dirty_config")" \
      "" \
      "The build reads this config from here but clones the checkout from origin, so" \
      "the container would not contain the config that built it." \
      "" \
      "Commit and push, or build deliberately anyway:  ${RD_BOLD}make build FORCE=1${RD_RESET}"
  fi
}

rdc_ensure_secrets_current() {
  [ "${SKIP_SECRETS_CHECK:-0}" != "1" ] || { rd_log "SKIP_SECRETS_CHECK=1, leaving Parameter Store untouched"; return 0; }
  rd_require_remote_config
  rd_require_cmd aws "Install: https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html"

  local local_env published
  local_env="${REPO_ROOT}/shell.env"
  [ -f "$local_env" ] || rd_fail "shell.env not found at ${local_env}" \
    "postCreate bootstraps the container from the copy of this file in Parameter Store," \
    "so there is nothing to compare against and nothing to publish." \
    "" \
    "Create it from the committed example:" \
    "  ${RD_BOLD}make init${RD_RESET}" \
    "" \
    "What each value does: docs/environment-files.md"

  published="$(rd_aws ssm describe-parameters \
    --profile "$REMOTE_AWS_PROFILE" --region "$REMOTE_AWS_REGION" \
    --parameter-filters "Key=Name,Values=${DEVCONTAINER_SSM_PREFIX}/shell.env" \
    --query 'Parameters[0].LastModifiedDate' --output text)"

  if [ -n "$published" ] && [ "$published" != "None" ] \
    && python3 - "$local_env" "$published" <<'PY'
import datetime, os, sys
local_mtime = os.path.getmtime(sys.argv[1])
published = datetime.datetime.fromisoformat(sys.argv[2]).timestamp()
sys.exit(0 if published >= local_mtime else 1)
PY
  then
    rd_ok "Parameter Store copy of shell.env is current"
    return 0
  fi

  rd_log "shell.env is newer than Parameter Store (or was never published), publishing"
  PROJECT_NAME="$PROJECT_NAME" "${RD_DIR}/push-secrets.sh" \
    || rd_die "failed to publish secrets to Parameter Store"
}

rdc_remote_host() {
  printf '%s' "$1" | sed -e 's|^[a-z]*://||' -e 's|^[^@]*@||' -e 's|[:/].*$||'
}

rdc_git_credentials() {
  printf 'protocol=https\nhost=%s\n\n' "$1" \
    | GIT_TERMINAL_PROMPT=0 git credential fill 2> /dev/null \
    | awk -F= '$1=="username"{u=$2} $1=="password"{p=$2} END{if (u && p) printf "%s\n%s\n", u, p}'
}

rdc_seed_volume() {
  local volume="$1" branch="$2" url="$3" host creds git_user git_secret
  if docker volume inspect "$volume" > /dev/null 2>&1; then
    rd_die "volume '${volume}' already exists. Run 'make clean' first, or 'make rebuild' to do both."
  fi

  host="$(rdc_remote_host "$url")"
  creds="$(rdc_git_credentials "$host")"
  [ -n "$creds" ] || rd_die \
    "no git credentials for ${host}. The clone runs on the engine and cannot prompt, authenticate on this machine first (e.g. 'gh auth login', or any push to ${host}), then retry."
  git_user="$(rdc_cred_user "$creds")"
  git_secret="$(rdc_cred_secret "$creds")"

  rd_log "creating volume ${volume}"
  rd_docker volume create "$volume" > /dev/null
  rd_log "cloning ${url} (${branch}) into ${volume}"
  docker run --rm -i -v "${volume}:/workspaces" "$CLONE_IMAGE" sh -s <<SEED || {
set -e
umask 077
printf 'https://%s:%s@%s\n' '${git_user}' '${git_secret}' '${host}' > /root/.git-credentials
git -c credential.helper=store clone --branch '${branch}' '${url}' '${CONTAINER_WORKSPACE}'
rm -f /root/.git-credentials
chown -R ${CONTAINER_UID_GID} /workspaces
SEED
    docker volume rm "$volume" > /dev/null 2>&1 || true
    rd_fail "Cloning ${url} into the volume failed" \
      "git's own message is in the output above. On this engine it is one of three" \
      "things:" \
      "" \
      "  the credential for ${host} is rejected or lacks access to this repository" \
      "      re-authenticate on this machine (${RD_BOLD}gh auth login${RD_RESET}), then retry" \
      "" \
      "  branch '${branch}' is not on origin after all" \
      "      ${RD_BOLD}git push -u origin ${branch}${RD_RESET}" \
      "" \
      "  the engine cannot pull ${CLONE_IMAGE} or reach ${host}" \
      "      check egress from the instance: ${RD_BOLD}make shell${RD_RESET}" \
      "" \
      "The partially created volume was removed, so a retry starts clean."
  }
  rd_ok "checkout seeded at ${CONTAINER_WORKSPACE}"
}

rdc_push_git_creds() {
  local id host creds git_user git_secret
  id="$(rdc_require_container)"
  host="$(rdc_remote_host "$(git -C "$REPO_ROOT" remote get-url origin)")"
  creds="$(rdc_git_credentials "$host")"
  [ -n "$creds" ] || rd_die \
    "no git credentials for ${host} on this machine. Authenticate first (e.g. 'gh auth login'), then retry."
  git_user="$(rdc_cred_user "$creds")"
  git_secret="$(rdc_cred_secret "$creds")"

  local written=0
  docker exec -i -u "$CONTAINER_USER" "$id" sh -s <<CREDS || written=$?
set -e
umask 077
printf 'https://%s:%s@%s\n' '${git_user}' '${git_secret}' '${host}' > "\$HOME/.git-credentials"
chmod 600 "\$HOME/.git-credentials"
git config --global credential.helper store
CREDS
  [ "$written" -eq 0 ] || rd_fail "The credential could not be written into the container" \
    "Nothing inside it was changed, so its git access is whatever it was before." \
    "" \
    "The container has to be running for this:  ${RD_BOLD}make start${RD_RESET}"

  rdc_exec_probe "$id" sh -c "cd '${CONTAINER_WORKSPACE}' && GIT_TERMINAL_PROMPT=0 git ls-remote origin > /dev/null 2>&1" \
    || rd_fail "The credential was written but ${host} rejects it" \
      "It is the same credential this machine uses, so it is expired, revoked, or has" \
      "no access to this repository." \
      "" \
      "Check it here first:" \
      "  ${RD_BOLD}git ls-remote origin${RD_RESET}" \
      "" \
      "Then re-authenticate and run this again:" \
      "  ${RD_BOLD}gh auth login${RD_RESET}  (or whatever helper this machine uses)" \
      "  ${RD_BOLD}make push-git-creds${RD_RESET}"
  rd_ok "container authenticates to ${host} on its own (verified with git ls-remote)"
}

: "${VSCODE_CLI:=code}"

rdc_vscode_commit() {
  rd_require_cmd "$VSCODE_CLI" "Install the VS Code 'code' command: Command Palette > Shell Command: Install 'code' command in PATH"

  local reported commit errors
  errors="$(mktemp "${TMPDIR:-/tmp}/rdc-vscode-version.XXXXXX")"
  reported="$("$VSCODE_CLI" --version 2> "$errors" || true)"
  commit="$(printf '%s\n' "$reported" | sed -n 2p | tr -d '[:space:]')"
  case "$commit" in
    *[!0-9a-f]* | "")
      rd_fail "Could not read the VS Code build from '${VSCODE_CLI} --version'" \
        "Line 2 of that output is the build the container's server is keyed on, and it read:" \
        "$(rd_quote "${commit:-nothing}")" \
        "" \
        "It reported:" \
        "$(rd_quote "${reported:-nothing on stdout}")" \
        "$(rd_quote "$(cat "$errors")")" \
        "" \
        "Check the CLI belongs to the VS Code you connect with:  ${RD_BOLD}${VSCODE_CLI} --version${RD_RESET}" \
        "" \
        "To open the window and let VS Code transfer the server itself:" \
        "  ${RD_BOLD}SKIP_VSCODE_SERVER_SEED=1 make reopen${RD_RESET}"
      ;;
  esac
  rm -f "$errors"
  printf '%s\n' "$commit"
}

rdc_vscode_server_platform() {
  local id="$1" os machine arch
  os="$(rdc_exec_probe "$id" uname -s | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')"
  machine="$(rdc_exec_probe "$id" uname -m | tr -d '[:space:]')"
  case "$machine" in
    aarch64 | arm64) arch=arm64 ;;
    x86_64 | amd64) arch=x64 ;;
    armv7l) arch=armhf ;;
    *)
      rd_fail "No VS Code server build is published for the container's architecture '${machine}'" \
        "The download service names builds per architecture, and this is not one it publishes." \
        "" \
        "To open the window and let VS Code transfer the server itself:" \
        "  ${RD_BOLD}SKIP_VSCODE_SERVER_SEED=1 make reopen${RD_RESET}"
      ;;
  esac
  printf 'server-%s-%s\n' "$os" "$arch"
}

rdc_seed_vscode_server() {
  local id="${1:-}"
  [ -n "$id" ] || id="$(rdc_require_container)"

  if [ "${SKIP_VSCODE_SERVER_SEED}" = "1" ]; then
    rd_log "SKIP_VSCODE_SERVER_SEED=1, leaving the server transfer to VS Code"
    return 0
  fi

  local commit home dir
  commit="$(rdc_vscode_commit)"
  home="$(rdc_exec_probe "$id" sh -c 'printf %s "$HOME"')"
  [ -n "$home" ] || rd_die "could not read ${CONTAINER_USER}'s home directory from the container"
  dir="${home}/${VSCODE_SERVER_DIRNAME}/${VSCODE_SERVER_CACHE_SUBDIR}"

  rdc_exec_probe "$id" awk -v target="$dir" \
    '$2 == target { found = 1 } END { exit found ? 0 : 1 }' /proc/self/mounts \
    || rd_fail "${dir} in the container is not a mount point, so a seeded server would not survive a rebuild" \
      "devcontainer.json is meant to mount a volume there. Without it, VS Code reinstalls the server" \
      "into the container's own filesystem every time one is built." \
      "" \
      "Check the mounts entry in ${RD_BOLD}.devcontainer/devcontainer.json${RD_RESET} targets ${dir}." \
      "" \
      "To open the window and let VS Code transfer the server itself:" \
      "  ${RD_BOLD}SKIP_VSCODE_SERVER_SEED=1 make reopen${RD_RESET}"

  if rdc_exec_probe "$id" test -x "${dir}/${commit}/bin/code-server"; then
    rd_ok "server for build ${commit} is already in the volume, nothing to fetch"
    rdc_prune_vscode_servers "$id" "$dir" "$commit"
    return 0
  fi

  # test -e follows symlinks, so a dangling one answers to neither check above
  # yet still makes mv refuse the seeded server. A VS Code window leaves exactly
  # that: attached to a container it created itself, it mounts the host's server
  # cache at /vscode and writes a symlink to it into the volume, which dangles
  # in every container built without that mount.
  if rdc_exec_probe "$id" sh -c "[ -e '${dir}/${commit}' ] || [ -L '${dir}/${commit}' ]"; then
    if rdc_exec_probe "$id" pgrep -f "${dir}/${commit}/" > /dev/null 2>&1; then
      rd_fail "${dir}/${commit} is not a usable server, but a process in the container is running from it" \
        "It cannot be replaced while something uses it." \
        "" \
        "To open the window and let VS Code transfer the server itself:" \
        "  ${RD_BOLD}SKIP_VSCODE_SERVER_SEED=1 make reopen${RD_RESET}"
    fi
    rd_log "removing ${dir}/${commit}, it is present but not a usable server"
    rdc_exec_probe "$id" rm -rf "${dir}/${commit}" \
      || rd_die "could not remove the unusable ${dir}/${commit} from the container"
  fi

  local platform url
  platform="$(rdc_vscode_server_platform "$id")"
  url="${VSCODE_UPDATE_URL}/commit:${commit}/${platform}/${VSCODE_UPDATE_CHANNEL}"
  rd_log "fetching ${platform} for build ${commit} inside the container"

  rdc_exec_probe "$id" bash -c "
set -euo pipefail
incoming=\"${dir}/${commit}.incoming.\$\$\"
trap 'rm -rf \"\$incoming\"' EXIT
mkdir -p \"\$incoming\"
curl -fsSL --max-time '${VSCODE_SERVER_FETCH_TIMEOUT}' '${url}' | tar -xz --strip-components=1 -C \"\$incoming\"
delivered=\"\$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))[\"commit\"])' \"\$incoming/product.json\")\"
[ \"\$delivered\" = '${commit}' ] || { echo \"downloaded server reports build \$delivered, not ${commit}\" >&2; exit 1; }
test -x \"\$incoming/bin/code-server\"
mv -n \"\$incoming\" '${dir}/${commit}'
" || rd_fail "The VS Code server could not be fetched inside the container" \
    "Nothing was installed, so VS Code will transfer it over the docker connection instead, which is" \
    "what this avoids. The reason is in the output above." \
    "" \
    "Check the container can reach the download service:" \
    "  ${RD_BOLD}make shell${RD_RESET}, then ${RD_BOLD}curl -sSI ${url}${RD_RESET}" \
    "" \
    "To open the window and accept the slow transfer:" \
    "  ${RD_BOLD}SKIP_VSCODE_SERVER_SEED=1 make reopen${RD_RESET}"

  rd_ok "server for build ${commit} seeded, VS Code will find it and transfer nothing"
  rdc_prune_vscode_servers "$id" "$dir" "$commit"
}

rdc_prune_vscode_servers() {
  local id="$1" dir="$2" keep="$3" build removed=0
  while IFS= read -r build; do
    [ -n "$build" ] || continue
    [ "$build" != "$keep" ] || continue
    if rdc_exec_probe "$id" pgrep -f "${dir}/${build}/" > /dev/null 2>&1; then
      rd_log "keeping build ${build}, a process in the container is running it"
      continue
    fi
    rdc_exec_probe "$id" rm -rf "${dir}/${build}" \
      || rd_die "could not remove ${dir}/${build} from the container"
    rd_log "removed build ${build}, nothing needs it"
    removed=$(( removed + 1 ))
  done < <(rdc_exec_probe "$id" sh -c "ls -1 '${dir}' 2> /dev/null || true")
  [ "$removed" -eq 0 ] || rd_ok "pruned ${removed} unused server build(s) from the volume"
}

rdc_reopen() {
  local id name workspace authority
  id="$(rdc_require_container)"

  rdc_seed_vscode_server "$id"
  name="$(rdc_container_name "$id")"

  if [ "$(rdc_backend)" != "remote" ]; then
    rd_require_cmd "$VSCODE_CLI" "Install the VS Code 'code' command: Command Palette > Shell Command: Install 'code' command in PATH"
    workspace="$(rdc_workspace_folder)"
    rd_log "opening ${REPO_ROOT} in its container"
    "$VSCODE_CLI" --folder-uri "vscode-remote://dev-container+$(printf '%s' "$REPO_ROOT" | od -A n -t x1 | tr -d ' \n')${workspace}"
    rd_ok "VS Code opening, the window attaches to '${name}'"
    return 0
  fi

  rd_require_cmd "$VSCODE_CLI" "Install the VS Code 'code' command: Command Palette > Shell Command: Install 'code' command in PATH"
  authority="$(printf '{"containerName":"/%s","settings":{"context":"%s"}}' \
    "$name" "$(docker context show)" | od -A n -t x1 | tr -d ' \n')"
  workspace="$(rdc_workspace_folder)"
  rd_log "opening ${workspace} in '${name}'"
  "$VSCODE_CLI" --folder-uri "vscode-remote://attached-container+${authority}${workspace}"
  rd_ok "VS Code opening the workspace directly, no Attach step needed"
}

rdc_exec_shell() {
  # An interactive shell inside the container, on whichever engine the active
  # context points at. This is the replacement for the host shell the cutover
  # removed: the container is where work happens, and the host deliberately has
  # no human access path left.
  #
  # Named rdc_exec_shell, not rdc_exec: that name is already taken above by the
  # non-interactive helper rdc_check and others use to run a single command in
  # a container and capture its output.
  local id
  id="$(rdc_require_container)" || exit $?
  docker exec -it "$id" "$CONTAINER_SHELL" \
    || rd_fail "The shell '${CONTAINER_SHELL}' could not be started in the container" \
      "The container is running; the shell itself failed to start." \
      "" \
      "If the image does not ship that shell, name one it does have:" \
      "  ${RD_BOLD}CONTAINER_SHELL=/bin/bash make exec${RD_RESET}"
}

rdc_up() {
  rd_require_cmd docker "Install the docker CLI: https://docs.docker.com/engine/install/"

  local backend
  backend="$(rdc_backend)"

  if ! docker info > /dev/null 2>&1; then
    if [ "$backend" = "remote" ]; then
      rd_log "remote engine is not answering, refreshing the port forward"
      PYTHONPATH="${RD_DIR}/../../.claude/plugins/devcontainer/scripts" python3 -m devcontainer_config.transport connect \
        --instance-id "$REMOTE_INSTANCE_ID" --context "$REMOTE_DOCKER_CONTEXT" \
        --profile "$REMOTE_AWS_PROFILE" --region "$REMOTE_AWS_REGION" > /dev/null \
        || rd_fail "The port forward could not be refreshed, so the remote engine stays unreachable" \
          "The reason is in the output above." \
          "" \
          "If it is an authentication failure:" \
          "  ${RD_BOLD}aws sso login --profile ${REMOTE_AWS_PROFILE}${RD_RESET}, then ${RD_BOLD}make up${RD_RESET}" \
          "" \
          "If the instance is stopped, start it, then ${RD_BOLD}make connect${RD_RESET}."
      rd_ok "port forward refreshed"
    else
      local diagnosis
      diagnosis="$(rd_engine_diagnosis "$(docker context show)")"
      rd_fail "The local docker engine is not answering" \
        "Cause   $(rd_line 1 "$diagnosis")" \
        "Fix     ${RD_BOLD}$(rd_line 2 "$diagnosis")${RD_RESET}"
    fi
  fi

  local ids
  ids="$(rdc_container_ids)"

  if [ -z "$ids" ]; then
    rd_log "no container for '${PROJECT_NAME}' on the ${backend} engine, building one"
    rdc_build
    rdc_reopen
    return 0
  fi

  local id state
  id="$(rdc_require_container)"
  state="$(rdc_container_state "$id")"

  case "$state" in
    running)
      rd_ok "container is already running"
      ;;
    *)
      rd_log "container is '${state}', starting it"
      rd_docker start "$id" > /dev/null
      rd_ok "started"
      ;;
  esac

  rdc_push_git_creds
  rdc_status
  rdc_reopen
}

rdc_build_flags() {
  [ "${NO_CACHE:-0}" != "1" ] || printf '%s\n' --build-no-cache
}

rdc_build_local() {
  rd_log "backend: local (context '$(docker context show)'), workspace bind-mounted from ${REPO_ROOT}"
  rd_log "building, this runs the image build and postCreate, and will take a while"

  local flags=()
  while IFS= read -r flag; do [ -z "$flag" ] || flags+=("$flag"); done < <(rdc_build_flags)

  # Passing any --id-label replaces the CLI's defaults, and VS Code identifies
  # a folder's container by devcontainer.local_folder/config_file. Without
  # them, opening the folder builds a second, identically-configured container
  # instead of attaching to this one.
  rd_devcontainer_up "$DEVCONTAINER_CLI" \
    --workspace-folder "$REPO_ROOT" \
    --id-label "devcontainer.project=${PROJECT_NAME}" \
    --id-label "devcontainer.local_folder=${REPO_ROOT}" \
    --id-label "devcontainer.config_file=${REPO_ROOT}/.devcontainer/devcontainer.json" \
    ${flags[@]+"${flags[@]}"}
}

rdc_build_remote() {
  rdc_ensure_secrets_current

  local branch url volume
  branch="$(rdc_branch)"
  url="$(git -C "$REPO_ROOT" remote get-url origin)"
  volume="$(rdc_workspace_volume)"
  rd_log "backend: remote (context '${REMOTE_DOCKER_CONTEXT}'), workspace cloned into volume '${volume}'"

  rdc_seed_volume "$volume" "$branch" "$url"

  # The override carries the resolved Parameter Store prefix into the
  # container, not just the volume mount. Without it the create-time bootstrap
  # falls back to /devcontainer/$(basename "$(pwd)") -- the workspace folder
  # name -- and an instance whose name differs from the project folder reads a
  # different environment's secrets entirely. Observed: instance 'sandbox'
  # bootstrapped from /devcontainer/general-dev/shell.env.
  RDC_OVERRIDE_CONFIG="$(mktemp "${TMPDIR:-/tmp}/devcontainer-override.XXXXXX")"
  trap 'rm -f "$RDC_OVERRIDE_CONFIG"' EXIT
  rdc_read_configuration \
    | jq --arg mount "source=${volume},target=${CONTAINER_WORKSPACES_ROOT},type=volume" \
      --arg configdir "${REPO_ROOT}/.devcontainer" \
      --arg ssmprefix "${DEVCONTAINER_SSM_PREFIX}" \
      '.configuration
       | del(.configFilePath)
       | .workspaceMount = $mount
       | .containerEnv = ((.containerEnv // {}) + {DEVCONTAINER_SSM_PREFIX: $ssmprefix})
       | if .build.dockerfile then
           .build.dockerfile = "\($configdir)/\(.build.dockerfile)"
           | .build.context = (if .build.context then "\($configdir)/\(.build.context)" else $configdir end)
         else . end' > "$RDC_OVERRIDE_CONFIG" \
    || rd_fail "The override configuration could not be generated" \
      "The resolved config was read, but rewriting workspaceMount to point at volume" \
      "'${volume}' failed, so there is nothing to build from." \
      "" \
      "This is a jq failure; check that jq runs:  ${RD_BOLD}jq --version${RD_RESET}"
  [ -s "$RDC_OVERRIDE_CONFIG" ] || rd_fail "The generated override configuration is empty" \
    "Building from it would produce a container with no configuration at all." \
    "" \
    "Check what the CLI resolves:" \
    "  ${RD_BOLD}devcontainer read-configuration --workspace-folder ${REPO_ROOT}${RD_RESET}"

  rd_log "building, this runs the image build and postCreate, and will take a while"

  local flags=()
  while IFS= read -r flag; do [ -z "$flag" ] || flags+=("$flag"); done < <(rdc_build_flags)

  rd_devcontainer_up "$DEVCONTAINER_CLI" \
    --workspace-folder "$REPO_ROOT" \
    --override-config "$RDC_OVERRIDE_CONFIG" \
    --id-label "devcontainer.project=${PROJECT_NAME}" \
    ${flags[@]+"${flags[@]}"}
}

rdc_build() {
  rdc_build_prereqs
  local existing
  existing="$(rdc_container_ids)"
  [ -z "$existing" ] || rd_fail "A container for '${PROJECT_NAME}' already exists on this engine" \
    "Building a second one from the same repository is almost never what is wanted," \
    "and both would answer to the same project label." \
    "" \
    "Replace it:            ${RD_BOLD}make rebuild${RD_RESET}" \
    "Use the existing one:  ${RD_BOLD}make up${RD_RESET}" \
    "See what is there:     ${RD_BOLD}make status${RD_RESET}"

  if [ "$(rdc_backend)" = "remote" ]; then
    rdc_build_remote
  else
    rdc_build_local
  fi

  rdc_push_git_creds
  rd_ok "container is up"
  rdc_status
}

rdc_clean() {
  local id name image volumes orphan existing
  existing="$(rdc_container_ids)"
  if [ -z "${CONTAINER:-}" ] && [ -z "$existing" ]; then
    rd_log "no container for '${PROJECT_NAME}'"
    orphan="$(rdc_workspace_volume)"
    if docker volume inspect "$orphan" > /dev/null 2>&1; then
      rd_log "removing orphaned workspace volume ${orphan}"
      rd_docker volume rm "$orphan" > /dev/null
    fi
    rd_ok "nothing left to remove"
    return 0
  fi
  id="$(rdc_require_container)"

  if [ "${FORCE:-0}" != "1" ]; then
    rdc_check
  else
    rd_log "FORCE=1, skipping the unpushed-work check"
  fi

  name="$(rdc_container_name "$id")"
  image="$(rd_docker inspect "$id" --format '{{.Config.Image}}')"
  volumes="$(rdc_project_volumes "$id")"

  rd_log "removing container ${name}"
  rd_docker rm -f "$id" > /dev/null

  local vol
  for vol in $volumes; do
    rd_log "removing volume ${vol}"
    rd_docker volume rm "$vol" > /dev/null
  done

  rd_log "removing image ${image}"
  rd_docker rmi "$image" > /dev/null

  rd_ok "torn down, shared volumes (${SHARED_VOLUMES}), the VS Code server cache and the cached base image were kept"
}

rdc_rebuild() {
  rdc_build_prereqs
  if [ "$(rdc_backend)" = "remote" ]; then
    rdc_ensure_secrets_current
  fi
  rdc_clean
  rdc_build
}

RDC_COMMAND="${1:-}"

# Resolve before classifying, not after. rdc_backend answers "is the active
# docker context this instance's context", and only the resolver knows what
# that context is. Asking first compared the active context against whatever
# config.env defaulted REMOTE_DOCKER_CONTEXT to, so every instance whose name
# did not happen to match that default was classified `local`: `make build`
# then took the bind-mount path and asked the remote engine to mount a path
# that exists only on this laptop.
#
# The quiet form is deliberate. A repository that configures no instances at
# all is a legitimately local one, not an error, and it must still be able to
# build against its local engine.
if command -v docker > /dev/null 2>&1; then
  # Resolve once, here, and let every downstream reader use the result. The
  # two names below were previously derived from PROJECT_NAME independently
  # by this script and by push-secrets.sh, which meant two callers could
  # address different instances while each believed it was addressing "the"
  # one. Assigning them from the single resolved block removes that split
  # without rewriting every use site.
  if rd_resolve_instance_quiet; then
    REMOTE_DOCKER_CONTEXT="$DOCKER_CONTEXT"
    DEVCONTAINER_SSM_PREFIX="${PARAMETER_PREFIX%/}"
    export REMOTE_DOCKER_CONTEXT DEVCONTAINER_SSM_PREFIX
  fi

  # Anything aimed at the remote engine needs the EC2 identity before docker
  # is even reachable; local commands must not, so this cannot live in
  # rd_load_config.
  if [ "$(rdc_backend)" = "remote" ]; then
    rd_require_remote_config
  fi
fi

case "$RDC_COMMAND" in
  status) rdc_status ;;
  start) rdc_require_docker && rdc_start ;;
  stop) rdc_require_docker && rdc_stop ;;
  restart) rdc_require_docker && rdc_restart ;;
  rename) rdc_require_docker && rdc_rename ;;
  check) rdc_require_docker && rdc_check ;;
  build) rdc_require_docker && rdc_build ;;
  reopen) rdc_require_docker && rdc_reopen ;;
  vscode-server) rdc_require_docker && rdc_seed_vscode_server ;;
  up) rdc_up ;;
  exec) rdc_require_docker && rdc_exec_shell ;;
  push-git-creds) rdc_require_docker && rdc_push_git_creds ;;
  clean) rdc_require_docker && rdc_clean ;;
  rebuild) rdc_require_docker && rdc_rebuild ;;
  *) rd_die "usage: $(basename "$0") <up|exec|status|start|stop|restart|rename|reopen|vscode-server|check|build|push-git-creds|clean|rebuild>" ;;
esac
