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

# Shell-escape a value for interpolation inside a single-quoted string: each '
# becomes '\'' (close the quoting, a literal quote, reopen). The seed heredocs
# below are expanded before the container's sh parses them, so every
# credential-bearing value must pass through this first or a quote inside it
# terminates the quoting and the rest of the value runs as shell.
rdc_sh_escape() {
  printf '%s' "$1" | sed "s/'/'\\\\''/g"
}

rdc_backend() {
  if [ -n "${ENGINE:-}" ]; then
    # ENGINE names the engine this whole run addresses, so classification is
    # decided by it directly -- 'local' is the local engine, anything else a
    # remote instance's -- and never by the machine-wide active context,
    # which another terminal may have switched while this one runs.
    # rd_engine_context has already failed fast when ENGINE names nothing.
    case "$(rd_engine_context)" in
      "$LOCAL_DOCKER_CONTEXT") printf 'local\n' ;;
      *) printf 'remote\n' ;;
    esac
    return 0
  fi
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

# This machine's stored credential for a git host, as two lines (username,
# then password), or no output when nothing is stored. Each answer line is
# split on its FIRST '=' only -- field splitting on '=' would truncate a
# username or password that itself carries '='.
rdc_git_credentials() {
  printf 'protocol=https\nhost=%s\n\n' "$1" \
    | GIT_TERMINAL_PROMPT=0 git credential fill 2> /dev/null \
    | awk '{ key = $0; if (!sub(/=.*/, "", key)) next; value = $0; sub(/^[^=]*=/, "", value); if (key == "username") u = value; else if (key == "password") p = value } END { if (u && p) printf "%s\n%s\n", u, p }'
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
  # Values are escaped for the heredoc below: it is expanded before the seed container's sh parses it, so a raw single quote in any of them would terminate the quoting around it and run as shell inside the seed container.
  local esc_user esc_secret esc_host esc_branch esc_url
  esc_user="$(rdc_sh_escape "$git_user")"
  esc_secret="$(rdc_sh_escape "$git_secret")"
  esc_host="$(rdc_sh_escape "$host")"
  esc_branch="$(rdc_sh_escape "$branch")"
  esc_url="$(rdc_sh_escape "$url")"
  docker run --rm -i -v "${volume}:/workspaces" "$CLONE_IMAGE" sh -s <<SEED || {
set -e
umask 077
printf 'https://%s:%s@%s\n' '${esc_user}' '${esc_secret}' '${esc_host}' > /root/.git-credentials
git -c credential.helper=store clone --branch '${esc_branch}' '${esc_url}' '${CONTAINER_WORKSPACE}'
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

# The git half of a creds push: seed ~/.git-credentials with this machine's
# credential for origin's host and make git's own store helper the one
# helper, then prove the container can reach that host with it. Split out of
# rdc_push_creds so the manifest decides whether it runs: it runs only when
# the hostcreds manifest names a git-source credential.
rdc_seed_git_credentials() {
  local id="$1" host creds git_user git_secret written
  host="$(rdc_remote_host "$(git -C "$REPO_ROOT" remote get-url origin)")"
  creds="$(rdc_git_credentials "$host")"
  [ -n "$creds" ] || rd_die \
    "no git credentials for ${host} on this machine. Authenticate first (e.g. 'gh auth login'), then retry."
  git_user="$(rdc_cred_user "$creds")"
  git_secret="$(rdc_cred_secret "$creds")"

  written=0
  # Values are escaped for the heredoc below: it is expanded before the container's sh parses it, so a raw single quote in any of them would terminate the quoting around it and run as shell inside the container.
  local esc_user esc_secret esc_host
  esc_user="$(rdc_sh_escape "$git_user")"
  esc_secret="$(rdc_sh_escape "$git_secret")"
  esc_host="$(rdc_sh_escape "$host")"
  docker exec -i -u "$CONTAINER_USER" "$id" sh -s <<CREDS || written=$?
set -e
umask 077
printf 'https://%s:%s@%s\n' '${esc_user}' '${esc_secret}' '${esc_host}' > "\$HOME/.git-credentials"
chmod 600 "\$HOME/.git-credentials"
# --replace-all, not a plain set: the Dev Containers extension copies the
# host's ~/.gitconfig into the container when a window attaches, and adds its
# own forwarding helper next to whatever postCreate left, so credential.helper
# can hold several values by the time this runs. A plain set then fails with
# "cannot overwrite multiple values" and the build errors out after the
# container is otherwise complete. Exactly one helper, store, is the intended
# state: the credential this script writes is the one the container uses, with
# or without a window attached, and the ls-remote probe below proves it works.
git config --global --replace-all credential.helper store
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
      "  ${RD_BOLD}make push-creds${RD_RESET}"
  rd_ok "container authenticates to ${host} on its own (verified with git ls-remote)"
}

# The hostcreds push, the generalization of the old git-only credential
# push: resolve EVERY credential the manifest names on this machine (the container has no
# keychain, no git credential helper and no aws session of its own, and the
# point of the mechanism is that it never gains any), write one <NAME>.env
# fragment per credential into the container's ~/.hostcreds/, and seed
# ~/.git-credentials when the manifest names a git-source credential. A
# resolved value rides only file descriptors: each fragment is piped from
# the host-side file over docker's stdin, never placed on a docker exec's
# argv.
rdc_push_creds() {
  # One subshell owns the fragment directory's EXIT trap: rdc_build_remote
  # arms a script-level EXIT trap to remove its override config, and a trap
  # armed here at function scope would silently replace that one and leak
  # its file. rd_fail's message and exit status cross the subshell boundary
  # unchanged.
  (
    local id frag_dir names name git_hosts
    id="$(rdc_require_container)"

    frag_dir="$(mktemp -d)"
    trap 'rm -rf "$frag_dir"' EXIT

    rd_log "resolving hostcreds fragments on this machine"
    if ! names="$(PYTHONPATH="${RD_DIR}/../../.claude/plugins/devcontainer/scripts" \
        python3 -m devcontainer_config.cli creds-fragments --output-dir "$frag_dir")"; then
      rd_fail "hostcreds could not be resolved on this machine" \
        "The message above already names the credential that failed and its remedy." \
        "" \
        "A keychain item that is missing is created by:  ${RD_BOLD}make creds-init${RD_RESET}" \
        "Then push again:  ${RD_BOLD}make push-creds${RD_RESET}"
    fi

    # Idempotent: an existing store directory from an earlier push is
    # reused rather than recreated, so a re-run cannot disturb it. Its 700
    # mode is enforced on every push, not only at creation: the fragments
    # are only as private as the directory holding them, and one widened
    # since the last push would slip past a creation-time-only chmod.
    docker exec -u "$CONTAINER_USER" "$id" sh -c 'umask 077; mkdir -p "$HOME/.hostcreds"; chmod 700 "$HOME/.hostcreds"'

    while IFS= read -r name; do
      [ -n "$name" ] || continue
      # The CLI validates every printed name against the manifest's own
      # [A-Z][A-Z0-9_]* rule; re-checking the same shape here keeps a
      # corrupted name from turning the exec below into a path traversal
      # (defense in depth). Anchored at both ends: a glob's trailing *
      # matches anything, so 'TO; rm -rf x' would have passed the case
      # form, while single-character names were rejected by it.
      printf '%s' "$name" | grep -qE '^[A-Z][A-Z0-9_]*$' \
        || rd_fail "creds-fragments printed ${name}, which is not a credential name" \
          "A credential name matches [A-Z][A-Z0-9_]*; anything else cannot safely index ~/.hostcreds."
      # The fragment rides stdin (< file), never argv: a value on a docker
      # exec's argv would sit in the host's process table for the life of
      # the call, where any other local user can read it. The umask rides
      # with the exec: docker's default 0022 would land the file at 0644,
      # failing the mode check below with the value world-readable in the
      # meantime.
      docker exec -i -u "$CONTAINER_USER" "$id" \
        sh -c "umask 077; cat > \"\$HOME/.hostcreds/${name}.env\"" < "${frag_dir}/${name}.env" \
        || rd_fail "The fragment for ${name} could not be written into the container" \
          "Nothing else inside it was changed." \
          "" \
          "The container has to be running for this:  ${RD_BOLD}make start${RD_RESET}"
      # Trust nothing: prove the file landed with the private mode the
      # umask above gives it, rather than trusting the exec's exit status.
      rdc_exec_probe "$id" sh -c "[ \"\$(stat -c %a \"\$HOME/.hostcreds/${name}.env\")\" = 600 ]" \
        || rd_fail "the fragment ${name}.env in the container's hostcreds store is missing or not mode 600" \
          "It was written moments ago, so the container's filesystem is misbehaving." \
          "" \
          "Re-run:  ${RD_BOLD}make push-creds${RD_RESET}"
    done <<< "$names"

    # The git-source hosts from the same manifest, printed by the same CLI:
    # hostnames are labels, never values, so printing them is safe, and a
    # non-empty answer is what gates the ~/.git-credentials seeding above.
    git_hosts="$(PYTHONPATH="${RD_DIR}/../../.claude/plugins/devcontainer/scripts" \
      python3 -m devcontainer_config.cli creds-fragments --print-git-hosts)" \
      || rd_fail "The hostcreds manifest could not be read for its git hosts" \
        "The message above names the problem; the same manifest resolved a moment ago."
    if [ -n "$git_hosts" ]; then
      rdc_seed_git_credentials "$id"
    else
      rd_log "no git-source credential in the manifest, leaving ~/.git-credentials alone"
    fi

    rd_ok "pushed $(printf '%s\n' "$names" | sed '/^$/d' | wc -l | tr -d ' ') credential fragment(s) into ~/.hostcreds"
  )
}

# One verification check: run the command through docker exec, print one
# PASS or FAIL line, and answer 0 or 1. The caller counts failures and
# decides the exit, so one FAIL never hides the checks after it (a plain
# rd_fail here would exit at the first finding instead).
rdc_verify_one() {
  local id="$1" label="$2"
  shift 2
  if rdc_exec_probe "$id" "$@" > /dev/null 2>&1; then
    rd_ok "PASS ${label}"
    return 0
  fi
  printf '%s[FAIL]%s %s\n' "$RD_RED" "$RD_RESET" "${label}" >&2
  return 1
}

# 'make verify-container': structural plus functional checks of the pushed
# credentials, all through docker exec against the ACTIVE context, so local
# and remote containers verify identically -- no engine-specific paths
# anywhere. Prints one PASS/FAIL line per check; exits non-zero if any check
# failed.
rdc_verify_container() {
  local id failures=0 startup_stderr git_hosts
  id="$(rdc_require_container)"
  rd_log "verifying pushed credentials inside the container"

  rdc_verify_one "$id" "the hostcreds store directory exists with mode 700" \
    sh -c '[ "$(stat -c %a "$HOME/.hostcreds")" = 700 ]' \
    || failures=$(( failures + 1 ))

  rdc_verify_one "$id" "every hostcreds fragment in the store is mode 600" \
    sh -c 'for f in "$HOME"/.hostcreds/*.env; do [ -e "$f" ] || continue; [ "$(stat -c %a "$f")" = 600 ] || exit 1; done' \
    || failures=$(( failures + 1 ))

  rdc_verify_one "$id" "startup block present in ~/.bashrc and ~/.zshenv" \
    sh -c 'grep -qF "# hostcreds-credential-startup-block" "$HOME/.bashrc" && grep -qF "# hostcreds-credential-startup-block" "$HOME/.zshenv"' \
    || failures=$(( failures + 1 ))

  # Only hostcreds-shaped stderr counts as a failure here: zsh may grumble
  # about the terminal docker exec does not give it, and a fragment's own
  # expiry notice reports a credential state, not a startup break. The
  # command's exit status is deliberately not the criterion (hence the
  # captured-then-inspected stderr); the check's contract is "startup
  # prints no hostcreds error".
  startup_stderr="$(rdc_exec_probe "$id" zsh -ic 'exit 0' 2>&1)" || true
  if printf '%s' "$startup_stderr" | grep -q hostcreds; then
    printf '%s[FAIL]%s %s\n' "$RD_RED" "$RD_RESET" "shell startup prints a hostcreds error" >&2
    failures=$(( failures + 1 ))
  else
    rd_ok "PASS shell startup prints no hostcreds error"
  fi

  # git: only when the manifest names a git-source credential, decided by
  # the same --print-git-hosts mode the push used.
  git_hosts="$(PYTHONPATH="${RD_DIR}/../../.claude/plugins/devcontainer/scripts" \
    python3 -m devcontainer_config.cli creds-fragments --print-git-hosts)" \
    || rd_fail "The hostcreds manifest could not be read for its git hosts" \
      "The message above names the problem."
  if [ -n "$git_hosts" ]; then
    rdc_verify_one "$id" "git authenticates to origin on its own (ls-remote)" \
      sh -c "cd '${CONTAINER_WORKSPACE}' && GIT_TERMINAL_PROMPT=0 git ls-remote origin > /dev/null 2>&1" \
      || failures=$(( failures + 1 ))
  else
    rd_log "no git-source credential in the manifest, skipping the git check"
  fi

  # aws: only when an aws-export fragment is present. The probe greps for
  # the variable NAME inside the store (a presence check); no value is ever
  # printed. zsh -c sources ~/.zshenv, which is where the startup block
  # exports the session the fragment carries.
  if rdc_exec_probe "$id" sh -c 'grep -l AWS_ACCESS_KEY_ID "$HOME"/.hostcreds/*.env > /dev/null 2>&1'; then
    rdc_verify_one "$id" "aws sts get-caller-identity answers with the pushed session" \
      zsh -c 'aws sts get-caller-identity --output text --no-cli-pager > /dev/null 2>&1' \
      || failures=$(( failures + 1 ))
  else
    rd_log "no aws-export fragment in the store, skipping the aws check"
  fi

  if [ "$failures" -gt 0 ]; then
    rd_fail "${failures} pushed-credential check(s) failed in the container" \
      "Each FAIL line above names its check." \
      "" \
      "Re-push every credential, then check again:" \
      "  ${RD_BOLD}make push-creds${RD_RESET}" \
      "  ${RD_BOLD}make verify-container${RD_RESET}"
  fi
  rd_ok "every pushed-credential check passed in the container"
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
  # The authority must name the engine this run addresses, not whichever
  # context is machine-wide active: under ENGINE those differ, and a window
  # opened against the active context attaches to the wrong engine's
  # container. Unset, rd_engine_context prints nothing and the active context
  # is exactly what it always was.
  local authority_context
  authority_context="$(rd_engine_context)"
  [ -n "$authority_context" ] || authority_context="$(docker context show)"
  authority="$(printf '{"containerName":"/%s","settings":{"context":"%s"}}' \
    "$name" "$authority_context" | od -A n -t x1 | tr -d ' \n')"
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

  rdc_push_creds
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

  rdc_push_creds
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
  # ENGINE, when set, names one engine explicitly. Resolving it here -- and
  # exporting its context as DOCKER_CONTEXT, the variable docker reads ahead
  # of the machine-wide current context -- is what aims every docker call in
  # this process at that one engine: rd_docker's and the plain ones alike,
  # and every child process (the devcontainer CLI included) that inherits the
  # environment. No `docker context use`, so state other terminals share is
  # never touched. rd_engine_context has already failed fast when ENGINE
  # names nothing; a direct call, not a command substitution, so the export
  # lands in this shell.
  if [ -n "${ENGINE:-}" ]; then
    DOCKER_CONTEXT="$(rd_engine_context)"
    export DOCKER_CONTEXT
  fi

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

  # Re-assert the ENGINE pin. The resolver's export loop now skips
  # DOCKER_CONTEXT under ENGINE, but that skip is lib.sh's promise, not this
  # script's assumption: anything that ever exports DOCKER_CONTEXT between the
  # pin above and here would silently re-aim the plain docker calls and child
  # processes at a different engine than rd_docker's --context. One cheap,
  # idempotent re-derivation closes that window for good.
  if [ -n "${ENGINE:-}" ]; then
    DOCKER_CONTEXT="$(rd_engine_context)"
    export DOCKER_CONTEXT
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
  push-creds) rdc_require_docker && rdc_push_creds ;;
  verify) rdc_require_docker && rdc_verify_container ;;
  clean) rdc_require_docker && rdc_clean ;;
  rebuild) rdc_require_docker && rdc_rebuild ;;
  *) rd_die "usage: $(basename "$0") <up|exec|status|start|stop|restart|rename|reopen|vscode-server|check|build|push-creds|verify|clean|rebuild>" ;;
esac
