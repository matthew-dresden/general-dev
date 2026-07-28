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

# Run a command in the container as the account the devcontainer runs as.
rdc_exec() {
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
  docker info > /dev/null 2>&1 || rd_die \
    "docker context '$(docker context show)' is not reachable. For the remote engine run 'make connect'; for a local engine start Docker."
}

# The devcontainer CLI stamps every container with the in-container path of the
# config it was built from. That label identifies the project, but it is the
# same on every instance of it, so several clones of one repo all match.
rdc_container_ids() {
  docker ps -aq --filter "label=devcontainer.project=${PROJECT_NAME}"
}

# Resolve the one container to act on. CONTAINER=<name|id> selects explicitly;
# otherwise exactly one match is required. Never guesses between instances.
rdc_require_container() {
  local ids count
  if [ -n "${CONTAINER:-}" ]; then
    docker inspect -f '{{.Id}}' "$CONTAINER" 2> /dev/null \
      || rd_die "no container named '${CONTAINER}' on context '${REMOTE_DOCKER_CONTEXT}'"
    return 0
  fi
  ids="$(rdc_container_ids)"
  [ -n "$ids" ] || rd_die \
    "no container for project '${PROJECT_NAME}' on context '${REMOTE_DOCKER_CONTEXT}'. Create one in VS Code: Dev Containers: Clone Repository in Container Volume..."
  count="$(printf '%s\n' "$ids" | wc -l | tr -d ' ')"
  if [ "$count" -gt 1 ]; then
    printf '\033[0;31m[ERROR]\033[0m %s instances of '\''%s'\'' exist:\n\n' "$count" "$PROJECT_NAME" >&2
    docker ps -a --filter "label=devcontainer.project=${PROJECT_NAME}" \
      --format '  {{.Names}}  [{{.State}}]  {{.CreatedAt}}' >&2
    printf '\nSelect one explicitly, e.g.:  make %s CONTAINER=<name>\n\n' "${RDC_COMMAND:-<target>}" >&2
    exit 1
  fi
  printf '%s\n' "$ids"
}

rdc_container_state() {
  docker inspect "$1" --format '{{.State.Status}}'
}

# Volume names attached to the container, minus the engine-wide shared ones.
rdc_project_volumes() {
  local id="$1" vol
  docker inspect "$id" --format '{{range .Mounts}}{{if eq .Type "volume"}}{{.Name}}{{"\n"}}{{end}}{{end}}' \
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
  if ! docker info > /dev/null 2>&1; then
    printf '\033[1mEngine\033[0m         \033[0;31mnot reachable\033[0m\n'
    if [ "$context" = "$REMOTE_DOCKER_CONTEXT" ]; then
      if aws sts get-caller-identity --profile "$REMOTE_AWS_PROFILE" > /dev/null 2>&1; then
        printf '\033[1mLikely cause\033[0m   the SSM tunnel has dropped. Fix: make connect\n\n'
      else
        printf '\033[1mLikely cause\033[0m   AWS SSO has expired, which breaks the tunnel.\n'
        printf '               Fix: aws sso login --profile %s, then make connect\n\n' "$REMOTE_AWS_PROFILE"
      fi
    else
      printf '\033[1mLikely cause\033[0m   the local Docker engine is not running. Start it, or switch with: make remote\n\n'
    fi
    return 1
  fi

  ids="$(rdc_container_ids)"
  if [ -z "$ids" ]; then
    printf '\033[1mContainer\033[0m      none, create with Dev Containers: Clone Repository in Container Volume...\n\n'
    return 0
  fi
  for id in $ids; do
    printf '\n\033[1mContainer\033[0m      %s  [%s]\n' \
      "$(docker inspect "$id" --format '{{.Name}}' | sed 's|^/||')" "$(rdc_container_state "$id")"
    printf '\033[1mImage\033[0m          %s\n' "$(docker inspect "$id" --format '{{.Config.Image}}')"
    printf '\033[1mVolumes\033[0m        %s\n' "$(rdc_project_volumes "$id" | tr '\n' ' ')"
  done
  printf '\n'
}

# Docker's generated names, and the ${devcontainerId} suffix the config applies,
# are both unreadable. Rename an instance to something you will recognise.
rdc_rename() {
  local id old
  [ -n "${NAME:-}" ] || rd_die "NAME is required, e.g.: make rename NAME=general-dev-review"
  id="$(rdc_require_container)"
  old="$(docker inspect "$id" --format '{{.Name}}' | sed 's|^/||')"
  [ "$old" != "$NAME" ] || rd_die "container is already named '${NAME}'"
  docker rename "$id" "$NAME"
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
  docker start "$id" > /dev/null
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
  docker stop "$id" > /dev/null
  rd_ok "stopped, the workspace volume is untouched; 'make start' resumes it"
}

rdc_restart() {
  local id
  id="$(rdc_require_container)"
  rd_log "restarting container..."
  docker restart "$id" > /dev/null
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
    docker start "$id" > /dev/null \
      || rd_die "could not start the container to inspect its checkout"
  fi

  dirty="$(rdc_exec "$id" git -C "${CONTAINER_WORKSPACE}" status --porcelain)"
  ahead="$(rdc_exec "$id" git -C "${CONTAINER_WORKSPACE}" log --oneline '@{upstream}..HEAD' 2> /dev/null || true)"

  if [ -z "$dirty" ] && [ -z "$ahead" ]; then
    rd_ok "checkout in the volume is clean and pushed, safe to destroy"
    return 0
  fi
  printf '\033[0;31m[ERROR]\033[0m the volume holds work that exists nowhere else:\n' >&2
  [ -z "$ahead" ] || printf '\n  unpushed commits:\n%s\n' "$ahead" >&2
  [ -z "$dirty" ] || printf '\n  uncommitted changes:\n%s\n' "$dirty" >&2
  printf '\nPush it from inside the container, then retry:\n' >&2
  printf "  docker exec -u %s %s git -C /workspaces/%s push\n\n" \
    "$CONTAINER_USER" "$(docker inspect "$id" --format '{{.Name}}' | sed 's|^/||')" "$PROJECT_NAME" >&2
  exit 1
}

rdc_branch() {
  git -C "$REPO_ROOT" rev-parse --abbrev-ref HEAD
}

# Volume holding the checkout. Deterministic, unlike the hash VS Code appends,
# so the same branch always maps to the same volume.
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

  local branch
  branch="$(rdc_branch)"
  git -C "$REPO_ROOT" rev-parse --verify --quiet "origin/${branch}" > /dev/null \
    || rd_die "origin/${branch} does not exist. Push the branch first, the build clones from origin, not from this machine."

  # The clone comes from origin, so unpushed local commits would silently not
  # be in the container.
  local unpushed
  unpushed="$(git -C "$REPO_ROOT" log --oneline "origin/${branch}..HEAD")"
  if [ -n "$unpushed" ] && [ "${FORCE:-0}" != "1" ]; then
    printf '\033[0;31m[ERROR]\033[0m %s commit(s) are not on origin/%s and would be missing from the container:\n\n' \
      "$(printf '%s\n' "$unpushed" | wc -l | tr -d ' ')" "$branch" >&2
    printf '%s\n' "$unpushed" | sed 's/^/  /' >&2
    printf '\nPush them, or rebuild deliberately without them with FORCE=1.\n\n' >&2
    exit 1
  fi

  # devcontainer.json is read from this machine, while the checkout comes from
  # origin. Uncommitted config would therefore build a container whose own
  # .devcontainer differs from the one that built it, silently.
  local dirty_config
  dirty_config="$(git -C "$REPO_ROOT" status --porcelain -- .devcontainer)"
  if [ -n "$dirty_config" ] && [ "${FORCE:-0}" != "1" ]; then
    printf '\033[0;31m[ERROR]\033[0m .devcontainer has uncommitted changes:\n\n' >&2
    printf '%s\n' "$dirty_config" | sed 's/^/  /' >&2
    printf '\nThe build reads this config from here but clones the checkout from\n' >&2
    printf 'origin, so the container would not contain the config that built it.\n' >&2
    printf 'Commit and push, or build deliberately anyway with FORCE=1.\n\n' >&2
    exit 1
  fi
}

# postCreate bootstraps shell.env from Parameter Store, so a container built
# against a stale copy comes up misconfigured, silently. Publish whenever the
# local file is newer, or has never been published, instead of stopping to ask.
rdc_ensure_secrets_current() {
  [ "${SKIP_SECRETS_CHECK:-0}" != "1" ] || { rd_log "SKIP_SECRETS_CHECK=1, leaving Parameter Store untouched"; return 0; }
  local local_env published
  local_env="${REPO_ROOT}/shell.env"
  [ -f "$local_env" ] || rd_die "shell.env not found at ${local_env}"

  published="$(aws ssm describe-parameters \
    --profile "$REMOTE_AWS_PROFILE" --region "$REMOTE_AWS_REGION" \
    --parameter-filters "Key=Name,Values=${DEVCONTAINER_SSM_PREFIX}/shell.env" \
    --query 'Parameters[0].LastModifiedDate' --output text 2> /dev/null)"

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
  docker volume create "$volume" > /dev/null
  rd_log "cloning ${url} (${branch}) into ${volume}"
  docker run --rm -i -v "${volume}:/workspaces" "$CLONE_IMAGE" sh -s <<SEED || {
set -e
umask 077
printf 'https://%s:%s@%s\n' '${git_user}' '${git_secret}' '${host}' > /root/.git-credentials
git -c credential.helper=store clone --branch '${branch}' '${url}' '${CONTAINER_WORKSPACE}'
rm -f /root/.git-credentials
chown -R ${CONTAINER_UID_GID} /workspaces
SEED
    docker volume rm "$volume" > /dev/null 2>&1
    rd_die "clone failed, the partially created volume was removed"
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
  docker exec -i -u "$CONTAINER_USER" "$id" sh -s <<CREDS || rd_die "could not write credentials into the container"
set -e
umask 077
printf 'https://%s:%s@%s\n' '${git_user}' '${git_secret}' '${host}' > "\$HOME/.git-credentials"
chmod 600 "\$HOME/.git-credentials"
git config --global credential.helper store
CREDS

  # Prove it works with nothing attached, rather than assuming.
  rdc_exec "$id" sh -c "cd '${CONTAINER_WORKSPACE}' && GIT_TERMINAL_PROMPT=0 git ls-remote origin > /dev/null 2>&1" \
    || rd_die "credentials were written but ${host} still rejects them, check that the token on this machine is valid"
  rd_ok "container authenticates to ${host} on its own (verified with git ls-remote)"
}

# Open the container in VS Code. Locally the folder is opened in its container
# directly; remotely there is no local folder to open, so the supported route is
# attaching to the running container.
: "${VSCODE_CLI:=code}"
rdc_reopen() {
  local id
  id="$(rdc_require_container)"
  local name
  name="$(docker inspect "$id" --format '{{.Name}}' | sed 's|^/||')"

  if [ "$(rdc_backend)" != "remote" ]; then
    rd_require_cmd "$VSCODE_CLI" "Install the VS Code 'code' command: Command Palette > Shell Command: Install 'code' command in PATH"
    rd_log "opening ${REPO_ROOT} in its container"
    "$VSCODE_CLI" --folder-uri "vscode-remote://dev-container+$(printf '%s' "$REPO_ROOT" | od -A n -t x1 | tr -d ' \n')${CONTAINER_WORKSPACE}"
    rd_ok "VS Code opening, the window attaches to '${name}'"
    return 0
  fi

  # The remote workspace lives in a volume, so there is no local path to hand
  # VS Code. Attach is the route that works; the clone-in-volume reattach path
  # is documented as unreliable on a high-latency context.
  rd_log "remote backend, attach is the supported route"
  printf '\n  VS Code -> Cmd+Shift+P -> \033[1mDev Containers: Attach to Running Container...\033[0m\n'
  printf '  Pick: \033[1m%s\033[0m\n\n' "$name"
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
      "${RD_DIR}/docker-tunnel.sh" > /dev/null \
        || rd_die "could not reach the remote engine. If this is an auth failure run: aws sso login --profile ${REMOTE_AWS_PROFILE}"
      rd_ok "tunnel refreshed"
    else
      rd_die "docker context '$(docker context show)' is not answering. Start your local Docker engine, or switch endpoint with 'make remote'."
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
      docker start "$id" > /dev/null \
        || rd_die "could not start the container. 'make rebuild' recreates it if it is broken."
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

  "$DEVCONTAINER_CLI" up \
    --workspace-folder "$REPO_ROOT" \
    --id-label "devcontainer.project=${PROJECT_NAME}" \
    ${flags[@]+"${flags[@]}"} \
    || exit $?
}

# Build against the remote engine: no laptop path exists there, so the checkout
# is cloned into a volume and workspaceMount is redirected at it. The committed
# devcontainer.json is never modified, the override is generated per build.
rdc_build_remote() {
  rdc_ensure_secrets_current

  local branch url volume config
  branch="$(rdc_branch)"
  url="$(git -C "$REPO_ROOT" remote get-url origin)"
  volume="$(rdc_workspace_volume)"
  rd_log "backend: remote (context '${REMOTE_DOCKER_CONTEXT}'), workspace cloned into volume '${volume}'"

  rdc_seed_volume "$volume" "$branch" "$url"

  # Explicit template: "mktemp -t NAME" means different things on BSD and GNU.
  config="$(mktemp "${TMPDIR:-/tmp}/devcontainer-override.XXXXXX")"
  "$DEVCONTAINER_CLI" read-configuration --workspace-folder "$REPO_ROOT" 2> /dev/null \
    | jq --arg mount "source=${volume},target=${CONTAINER_WORKSPACES_ROOT},type=volume" \
      '.configuration | del(.configFilePath) | .workspaceMount = $mount' > "$config" \
    || rd_die "could not generate the override configuration"
  [ -s "$config" ] || rd_die "generated override configuration is empty"

  rd_log "building, this runs the image build and postCreate, and will take a while"

  local flags=()
  while IFS= read -r flag; do [ -z "$flag" ] || flags+=("$flag"); done < <(rdc_build_flags)

  "$DEVCONTAINER_CLI" up \
    --workspace-folder "$REPO_ROOT" \
    --override-config "$config" \
    --id-label "devcontainer.project=${PROJECT_NAME}" \
    ${flags[@]+"${flags[@]}"}
  local status=$?
  rm -f "$config"
  [ "$status" -eq 0 ] || exit "$status"
}

rdc_build() {
  rdc_build_prereqs
  [ -z "$(rdc_container_ids)" ] \
    || rd_die "a container for '${PROJECT_NAME}' already exists. Use 'make rebuild' to replace it."

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
  local id name image volumes orphan
  if [ -z "${CONTAINER:-}" ] && [ -z "$(rdc_container_ids)" ]; then
    rd_log "no container for '${PROJECT_NAME}'"
    # A build that failed after seeding leaves the workspace volume behind.
    # Its name is derived, not discovered, so removing it here is unambiguous.
    orphan="$(rdc_workspace_volume)"
    if docker volume inspect "$orphan" > /dev/null 2>&1; then
      rd_log "removing orphaned workspace volume ${orphan}"
      docker volume rm "$orphan" > /dev/null
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

  name="$(docker inspect "$id" --format '{{.Name}}' | sed 's|^/||')"
  image="$(docker inspect "$id" --format '{{.Config.Image}}')"
  # Capture mounts before removal: once the container is gone the association
  # between project and volumes cannot be recovered.
  volumes="$(rdc_project_volumes "$id")"

  rd_log "removing container ${name}"
  docker rm -f "$id" > /dev/null

  local vol
  for vol in $volumes; do
    rd_log "removing volume ${vol}"
    docker volume rm "$vol" > /dev/null
  done

  rd_log "removing image ${image}"
  docker rmi "$image" > /dev/null

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
