#!/usr/bin/env bash
# Lifecycle operations for this project's devcontainer on the remote EC2
# Docker engine. Invoked by the root Makefile; usable directly.
#
# Usage: ./container.sh <status|start|stop|restart|rename|check|build|clean|rebuild>
# Configuration: see config.env. PROJECT_NAME defaults to the repo directory
# name; every value is overridable via the environment.

set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

rd_load_config

REPO_ROOT="$(cd "${RD_DIR}/../.." && pwd)"
: "${PROJECT_NAME:=$(basename "$REPO_ROOT")}"
# Volumes shared by every project on the engine. Never removed by 'clean':
# they hold no project state and only cost re-provisioning time.
: "${SHARED_VOLUMES:=vscode minikube-config}"
# User inside the devcontainer. git refuses to operate as root in the volume
# ("detected dubious ownership"), so exec must target this account.
: "${CONTAINER_USER:=vscode}"
# uid:gid the devcontainer runs as. The clone happens as root, so the tree has
# to be handed to this account or nothing in the container can write to it.
: "${CONTAINER_UID_GID:=1000:1000}"
# Image used for the throwaway clone step. The devcontainer base ships git and
# is pulled for the build anyway, so this adds no extra download.
: "${CLONE_IMAGE:=mcr.microsoft.com/devcontainers/base:noble}"
# The devcontainer CLI drives the build. Not the VS Code extension's bundled
# copy, which is unsupported for direct use.
: "${DEVCONTAINER_CLI:=devcontainer}"
# SSM path that postCreate bootstraps shell.env from.
: "${DEVCONTAINER_SSM_PREFIX:=/devcontainer/${PROJECT_NAME}}"
# Where the workspace volume is mounted, and the checkout inside it. Must match
# workspaceFolder in devcontainer.json.
: "${CONTAINER_WORKSPACES_ROOT:=/workspaces}"
CONTAINER_WORKSPACE="${CONTAINER_WORKSPACES_ROOT}/${PROJECT_NAME}"
# Generated per remote build and removed on exit. Declared here so the EXIT
# trap that removes it can still name it once the build function has returned.
RDC_OVERRIDE_CONFIG=""

# The resolved devcontainer configuration, as the single JSON line the CLI
# prints. Both the workspace path and the remote build's override config are
# derived from it, so it is read in one place.
#
# Three distinct things can go wrong, and they send you to three different
# places: no config file at all, a config the CLI rejects, and a config that
# parses but yields nothing. One message for all three is a message for none.
rdc_read_configuration() {
  rd_require_cmd "$DEVCONTAINER_CLI" "Install it: npm install -g @devcontainers/cli"
  rd_require_cmd jq "Install jq: 'brew install jq' or 'apt-get install jq'"

  local config="${REPO_ROOT}/.devcontainer/devcontainer.json"
  [ -f "$config" ] || rd_fail "There is no devcontainer configuration at ${config}" \
    "Every operation resolves the workspace path from it, so there is nothing to act on." \
    "" \
    "This ran against ${REPO_ROOT}. Run it from the repository that owns the" \
    ".devcontainer directory, or point PROJECT_NAME at the right one."

  # The CLI prints its banner on stderr and one JSON line on stdout, so the two
  # are captured separately: the banner would otherwise be fed to jq.
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

# The workspace path inside the container, taken from devcontainer.json rather
# than rebuilt here. That file owns workspaceFolder, and it can contain
# variables such as ${localWorkspaceFolderBasename}, so the resolved value is
# read from the CLI. Changing workspaceFolder therefore needs no change here.
rdc_workspace_folder() {
  if [ -n "${RDC_WORKSPACE_FOLDER:-}" ]; then
    printf '%s\n' "$RDC_WORKSPACE_FOLDER"
    return 0
  fi

  local config="${REPO_ROOT}/.devcontainer/devcontainer.json"

  # The status is propagated by hand: a failure inside the substitution would
  # otherwise arrive here as an empty value and be reported as a missing
  # workspaceFolder, on top of the real message.
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

# Run a command in the container as the account the devcontainer runs as.
# A failure here is an error, so it is explained: a stopped container and a
# missing command inside it look identical otherwise.
rdc_exec() {
  local id="$1"; shift
  rd_docker exec -u "$CONTAINER_USER" "$id" "$@"
}

# The same, for the callers that ask a question rather than issue an order and
# act on the exit status themselves. Wrapping these would turn "no upstream
# branch" into a fatal error.
rdc_exec_probe() {
  local id="$1"; shift
  docker exec -u "$CONTAINER_USER" "$id" "$@"
}

# Split the two-line credential pair produced by rdc_git_credentials.
rdc_cred_user() { printf '%s' "$1" | sed -n 1p; }
rdc_cred_secret() { printf '%s' "$1" | sed -n 2p; }

# Which engine the active docker context points at. Every operation adapts to
# this rather than demanding one particular endpoint: the same commands work on
# a laptop engine (OrbStack, Docker Desktop, docker-ce) and on the remote EC2
# engine, because the difference is confined to how the workspace is mounted.
#
#   remote, no laptop path exists on the engine, so the checkout is cloned
#            into a volume and workspaceMount is redirected at it
#   local, the engine shares this filesystem, so devcontainer.json's default
#            bind mount is correct and nothing has to be cloned
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

# The devcontainer CLI stamps every container with the in-container path of the
# config it was built from. That label identifies the project, but it is the
# same on every instance of it, so several clones of one repo all match.
rdc_container_ids() {
  rd_docker ps -aq --filter "label=devcontainer.project=${PROJECT_NAME}"
}

# Resolve the one container to act on. CONTAINER=<name|id> selects explicitly;
# otherwise exactly one match is required. Never guesses between instances.
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
  # Propagated rather than left to errexit: this function is called from inside
  # a command substitution, where a failure would otherwise read as "no
  # containers" and produce a second, wrong message.
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

# Docker records the name with a leading slash; nothing that reads it wants one.
rdc_container_name() {
  rd_docker inspect "$1" --format '{{.Name}}' | sed 's|^/||'
}

# Volume names attached to the container, minus the engine-wide shared ones.
rdc_project_volumes() {
  local id="$1" vol
  rd_docker inspect "$id" --format '{{range .Mounts}}{{if eq .Type "volume"}}{{.Name}}{{"\n"}}{{end}}{{end}}' \
    | while IFS= read -r vol; do
        [ -n "$vol" ] || continue
        case " ${SHARED_VOLUMES} " in
          *" ${vol} "*) continue ;;
        esac
        printf '%s\n' "$vol"
      done
}

# Lists every instance, so multiple clones of one repo are all visible.
rdc_status() {
  local ids id context
  context="$(docker context show 2>/dev/null || echo unknown)"
  printf '\n\033[1mProject\033[0m        %s\n' "$PROJECT_NAME"
  if [ "$context" = "$REMOTE_DOCKER_CONTEXT" ]; then
    printf '\033[1mBackend\033[0m        remote, context %s (%s)\n' "$context" "$REMOTE_INSTANCE_ID"
  else
    printf '\033[1mBackend\033[0m        local, context %s\n' "$context"
  fi

  # Report where you are pointed even when the engine cannot be reached: that
  # is exactly when you need to know, and the cause is usually diagnosable.
  # The diagnosis is the shared one, so this and a failing command agree.
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
  # Assigned before they are printed, so a docker failure stops here instead of
  # printing a row of blanks under the error it just produced.
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

# Docker's generated names, and the ${devcontainerId} suffix the config applies,
# are both unreadable. Rename an instance to something you will recognise.
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

# Report uncommitted work in the volume. Exit non-zero when the checkout is
# dirty so 'clean' can refuse to destroy unpushed work.
rdc_check() {
  local id state dirty ahead
  # A local container bind-mounts this working tree, so its checkout is the one
  # you can already see; there is no second copy that could hold lost work.
  if [ "$(rdc_backend)" != "remote" ]; then
    rd_ok "local backend, the container shares this working tree, nothing to check"
    return 0
  fi
  id="$(rdc_require_container)"
  state="$(rdc_container_state "$id")"
  # Inspecting the checkout needs a running container. Start it rather than
  # sending the caller away to run one command and come back.
  if [ "$state" != "running" ]; then
    rd_log "container is '${state}', starting it to inspect the checkout"
    rd_docker start "$id" > /dev/null
  fi

  dirty="$(rdc_exec "$id" git -C "${CONTAINER_WORKSPACE}" status --porcelain)"
  # A checkout with no upstream is a legitimate answer here, not a failure, so
  # this one asks rather than orders.
  ahead="$(rdc_exec_probe "$id" git -C "${CONTAINER_WORKSPACE}" log --oneline '@{upstream}..HEAD' 2> /dev/null || true)"

  if [ -z "$dirty" ] && [ -z "$ahead" ]; then
    rd_ok "checkout in the volume is clean and pushed, safe to destroy"
    return 0
  fi
  # Only the kinds of work that are actually present get a heading.
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

# The remote build clones from origin, so a branch that exists only on this
# machine cannot be built. Two ways out, both spelled out.
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

# Everything the build needs before it starts doing work.
rdc_build_prereqs() {
  rd_require_cmd "$DEVCONTAINER_CLI" "Install it: npm install -g @devcontainers/cli"
  rd_require_cmd git "Install git."
  rd_require_cmd jq "Install jq: 'brew install jq' or 'apt-get install jq'"
  rd_require_cmd python3 "Install python3 (used to compare timestamps)."

  # Everything below concerns the remote backend only: it clones from origin
  # and reads config from here, so the two can disagree. A local build uses
  # this working tree directly, where no such gap exists.
  [ "$(rdc_backend)" = "remote" ] || return 0

  # The container is built from the branch you are on. No guessing and no
  # substitution: if that branch is not on origin, say so and stop.
  local branch default_branch
  branch="$(rdc_branch)"
  git -C "$REPO_ROOT" fetch --quiet origin "$branch" 2> /dev/null || true
  if ! git -C "$REPO_ROOT" rev-parse --verify --quiet "origin/${branch}" > /dev/null; then
    # Under set -e with pipefail a failing pipeline here would abort the
    # script before the message below could be printed, which is exactly the
    # silent "Error 1" this guard exists to replace.
    default_branch="$(git -C "$REPO_ROOT" symbolic-ref --quiet --short refs/remotes/origin/HEAD 2> /dev/null | sed 's|^origin/||' || true)"
    rdc_branch_missing_on_origin "$branch" "${default_branch:-main}"
  fi

  # The clone comes from origin, so unpushed local commits would silently not
  # be in the container.
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

  # devcontainer.json is read from this machine, while the checkout comes from
  # origin. Uncommitted config would therefore build a container whose own
  # .devcontainer differs from the one that built it, silently.
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

# postCreate bootstraps shell.env from Parameter Store, so a container built
# against a stale copy comes up misconfigured, silently. Publish whenever the
# local file is newer, or has never been published, instead of stopping to ask.
rdc_ensure_secrets_current() {
  [ "${SKIP_SECRETS_CHECK:-0}" != "1" ] || { rd_log "SKIP_SECRETS_CHECK=1, leaving Parameter Store untouched"; return 0; }
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

  # Not 2>/dev/null: a discarded error here left the whole build exiting on a
  # bare status, with expired credentials looking exactly like an empty result.
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

# Host portion of a git remote URL, for a credential lookup.
rdc_remote_host() {
  printf '%s' "$1" | sed -e 's|^[a-z]*://||' -e 's|^[^@]*@||' -e 's|[:/].*$||'
}

# Ask git for the credentials it would use for this host, from whatever helper
# the laptop already has configured (osxkeychain, gh, store, ...). This is the
# same source that makes `git push` work here, so no separate token has to be
# maintained. Prints "<username>\n<password>"; empty if the helper has nothing.
rdc_git_credentials() {
  printf 'protocol=https\nhost=%s\n\n' "$1" \
    | GIT_TERMINAL_PROMPT=0 git credential fill 2> /dev/null \
    | awk -F= '$1=="username"{u=$2} $1=="password"{p=$2} END{if (u && p) printf "%s\n%s\n", u, p}'
}

# Clone the repo from origin into a named volume on the engine. The devcontainer
# CLI would otherwise bind-mount a laptop path, which does not exist there.
#
# The repo may be private, so the clone needs credentials. They are written to a
# throwaway credentials file inside the clone container and passed on stdin
# rather than as arguments or environment: the secret never reaches the
# container's argv or its inspect output, and the clone keeps a clean remote URL.
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
  # Output streams rather than being captured: a large clone with no progress
  # is indistinguishable from a hang. git's own message is therefore already on
  # screen above, and what follows says what to do about it.
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

# Copy this machine's git credentials into the container so it can reach the
# remote on its own.
#
# While VS Code is attached it forwards the host's credentials, which hides the
# fact that the container has none; detached, an agent running in tmux, push
# fails. postCreate writes whatever GIT_TOKEN shell.env carries, which is a
# separate credential that has to be kept alive by hand. This instead reuses the
# helper that already works on this machine, and overwrites postCreate's copy.
#
# The credential is written to a file inside the container, on the shared
# engine. Anyone with docker access to that engine can read it.
rdc_push_git_creds() {
  local id host creds git_user git_secret
  id="$(rdc_require_container)"
  host="$(rdc_remote_host "$(git -C "$REPO_ROOT" remote get-url origin)")"
  creds="$(rdc_git_credentials "$host")"
  [ -n "$creds" ] || rd_die \
    "no git credentials for ${host} on this machine. Authenticate first (e.g. 'gh auth login'), then retry."
  git_user="$(rdc_cred_user "$creds")"
  git_secret="$(rdc_cred_secret "$creds")"

  # Passed on stdin so the secret is in neither argv nor the exec's inspect output.
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

  # Prove it works with nothing attached, rather than assuming. A rejection is
  # an answer, so this asks rather than orders.
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

# Open the container in VS Code. Locally the folder is opened in its container
# directly; remotely there is no local folder to open, so the supported route is
# attaching to the running container.
: "${VSCODE_CLI:=code}"
rdc_reopen() {
  local id name workspace authority
  id="$(rdc_require_container)"
  name="$(rdc_container_name "$id")"

  if [ "$(rdc_backend)" != "remote" ]; then
    rd_require_cmd "$VSCODE_CLI" "Install the VS Code 'code' command: Command Palette > Shell Command: Install 'code' command in PATH"
    # Resolved before the URI is built: a failure inside the argument would
    # otherwise open VS Code on a truncated path.
    workspace="$(rdc_workspace_folder)"
    rd_log "opening ${REPO_ROOT} in its container"
    "$VSCODE_CLI" --folder-uri "vscode-remote://dev-container+$(printf '%s' "$REPO_ROOT" | od -A n -t x1 | tr -d ' \n')${workspace}"
    rd_ok "VS Code opening, the window attaches to '${name}'"
    return 0
  fi

  # A volume-backed workspace has no local path, but VS Code can open a folder
  # inside an already-attached container by URI. The authority is
  # "attached-container+" followed by hex-encoded JSON naming the container and
  # the docker context it lives on, which is exactly what VS Code writes to its
  # own recently-opened list when you attach by hand.
  rd_require_cmd "$VSCODE_CLI" "Install the VS Code 'code' command: Command Palette > Shell Command: Install 'code' command in PATH"
  authority="$(printf '{"containerName":"/%s","settings":{"context":"%s"}}' \
    "$name" "$(docker context show)" | od -A n -t x1 | tr -d ' \n')"
  workspace="$(rdc_workspace_folder)"
  rd_log "opening ${workspace} in '${name}'"
  "$VSCODE_CLI" --folder-uri "vscode-remote://attached-container+${authority}${workspace}"
  rd_ok "VS Code opening the workspace directly, no Attach step needed"
}

# One command that gets you working from whatever state things are in.
#
# Decides rather than asks: an unreachable remote engine means the tunnel needs
# refreshing, no container means build one, a stopped container means start it,
# and a running one means there is nothing to do but open it. Everything it
# calls is a target you can also run on its own, so nothing here is a second
# implementation of anything.
rdc_up() {
  rd_require_cmd docker "Install the docker CLI: https://docs.docker.com/engine/install/"

  local backend
  backend="$(rdc_backend)"

  # An unreachable engine is recoverable on the remote backend: the SSM tunnel
  # drops on sleep and on SSO expiry, and refreshing it is what connect does.
  if ! docker info > /dev/null 2>&1; then
    if [ "$backend" = "remote" ]; then
      rd_log "remote engine is not answering, refreshing the tunnel"
      # The tunnel script reports its own failures in full; this only has to say
      # which step of 'up' it was.
      "${RD_DIR}/docker-tunnel.sh" > /dev/null \
        || rd_fail "The tunnel could not be refreshed, so the remote engine stays unreachable" \
          "The reason is in the output above." \
          "" \
          "If it is an authentication failure:" \
          "  ${RD_BOLD}aws sso login --profile ${REMOTE_AWS_PROFILE}${RD_RESET}, then ${RD_BOLD}make up${RD_RESET}" \
          "" \
          "If the instance is stopped, start it, then ${RD_BOLD}make connect${RD_RESET}."
      rd_ok "tunnel refreshed"
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

  # rdc_require_container refuses to guess when several instances exist, and
  # names them, so CONTAINER=<name> is the answer rather than a wrong choice.
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

  # Cheap, and it verifies rather than assumes: a credential that expired since
  # the container was built would otherwise only surface on the next push.
  rdc_push_git_creds
  rdc_status
  rdc_reopen
}

# Build and start the container, blocking until postCreate finishes. Exits with
# the CLI's status, so a failed build fails the make target.
# Flags shared by both backends. NO_CACHE=1 rebuilds the image from scratch,
# which is the fix when a feature or base image changed underneath a cached layer.
rdc_build_flags() {
  [ "${NO_CACHE:-0}" != "1" ] || printf '%s\n' --build-no-cache
}

# Build against a laptop engine. devcontainer.json's own bind mount is correct
# here, so there is nothing to clone and no config to override.
rdc_build_local() {
  rd_log "backend: local (context '$(docker context show)'), workspace bind-mounted from ${REPO_ROOT}"
  rd_log "building, this runs the image build and postCreate, and will take a while"

  local flags=()
  while IFS= read -r flag; do [ -z "$flag" ] || flags+=("$flag"); done < <(rdc_build_flags)

  rd_devcontainer_up "$DEVCONTAINER_CLI" \
    --workspace-folder "$REPO_ROOT" \
    --id-label "devcontainer.project=${PROJECT_NAME}" \
    ${flags[@]+"${flags[@]}"}
}

# Build against the remote engine: no laptop path exists there, so the checkout
# is cloned into a volume and workspaceMount is redirected at it. The committed
# devcontainer.json is never modified, the override is generated per build.
rdc_build_remote() {
  rdc_ensure_secrets_current

  local branch url volume
  branch="$(rdc_branch)"
  url="$(git -C "$REPO_ROOT" remote get-url origin)"
  volume="$(rdc_workspace_volume)"
  rd_log "backend: remote (context '${REMOTE_DOCKER_CONTEXT}'), workspace cloned into volume '${volume}'"

  rdc_seed_volume "$volume" "$branch" "$url"

  # Explicit template: "mktemp -t NAME" means different things on BSD and GNU.
  # Held in a variable the trap can still see, and removed however this ends: a
  # failed build used to leave it behind, because under 'set -e' the cleanup
  # after the CLI call was never reached.
  RDC_OVERRIDE_CONFIG="$(mktemp "${TMPDIR:-/tmp}/devcontainer-override.XXXXXX")"
  trap 'rm -f "$RDC_OVERRIDE_CONFIG"' EXIT
  rdc_read_configuration \
    | jq --arg mount "source=${volume},target=${CONTAINER_WORKSPACES_ROOT},type=volume" \
      '.configuration | del(.configFilePath) | .workspaceMount = $mount' > "$RDC_OVERRIDE_CONFIG" \
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
  # Assigned before it is tested, not tested inside a substitution: a failure
  # there would read as "no containers" and start a second build.
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

# Destroy the container, its non-shared volumes, and its image. A rebuild
# re-clones from origin, so anything unpushed is gone for good.
rdc_clean() {
  local id name image volumes orphan existing
  existing="$(rdc_container_ids)"
  if [ -z "${CONTAINER:-}" ] && [ -z "$existing" ]; then
    rd_log "no container for '${PROJECT_NAME}'"
    # A build that failed after seeding leaves the workspace volume behind.
    # Its name is derived, not discovered, so removing it here is unambiguous.
    orphan="$(rdc_workspace_volume)"
    if docker volume inspect "$orphan" > /dev/null 2>&1; then
      rd_log "removing orphaned workspace volume ${orphan}"
      rd_docker volume rm "$orphan" > /dev/null
    fi
    rd_ok "nothing left to remove"
    return 0
  fi
  # Resolves to exactly one instance, or fails listing them all.
  id="$(rdc_require_container)"

  if [ "${FORCE:-0}" != "1" ]; then
    rdc_check
  else
    rd_log "FORCE=1, skipping the unpushed-work check"
  fi

  name="$(rdc_container_name "$id")"
  image="$(rd_docker inspect "$id" --format '{{.Config.Image}}')"
  # Capture mounts before removal: once the container is gone the association
  # between project and volumes cannot be recovered.
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

  rd_ok "torn down, shared volumes (${SHARED_VOLUMES}) and the cached base image were kept"
}

# Tear down and build again, blocking through both. Prerequisites are checked
# before anything is destroyed, so a missing tool cannot leave you with no
# container and no way to make one.
rdc_rebuild() {
  rdc_build_prereqs
  rdc_ensure_secrets_current
  rdc_clean
  rdc_build
}

# Surfaced in the ambiguity error so it can name the target the user just ran.
RDC_COMMAND="${1:-}"

case "$RDC_COMMAND" in
  status) rdc_status ;;
  start) rdc_require_docker && rdc_start ;;
  stop) rdc_require_docker && rdc_stop ;;
  restart) rdc_require_docker && rdc_restart ;;
  rename) rdc_require_docker && rdc_rename ;;
  check) rdc_require_docker && rdc_check ;;
  build) rdc_require_docker && rdc_build ;;
  reopen) rdc_require_docker && rdc_reopen ;;
  up) rdc_up ;;
  push-git-creds) rdc_require_docker && rdc_push_git_creds ;;
  clean) rdc_require_docker && rdc_clean ;;
  rebuild) rdc_require_docker && rdc_rebuild ;;
  *) rd_die "usage: $(basename "$0") <up|status|start|stop|restart|rename|reopen|check|build|push-git-creds|clean|rebuild>" ;;
esac
