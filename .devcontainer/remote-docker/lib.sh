#!/usr/bin/env bash
# Shared functions for the remote-docker scripts. Sourced, never executed.
# Compatible with macOS /bin/bash 3.2 (no bash-4-only constructs).

set -euo pipefail

RD_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

RD_RED=$'\033[0;31m'
RD_GREEN=$'\033[0;32m'
RD_CYAN=$'\033[0;36m'
RD_BOLD=$'\033[1m'
RD_RESET=$'\033[0m'

rd_log() { printf '%s[INFO]%s %s\n' "$RD_CYAN" "$RD_RESET" "$1"; }
rd_ok() { printf '%s[DONE]%s %s\n' "$RD_GREEN" "$RD_RESET" "$1"; }

# One-line failure. Anything that needs more than one line, because it has to
# name a cause and offer a way forward, uses rd_fail instead.
rd_die() {
  printf '%s[ERROR]%s %s\n' "$RD_RED" "$RD_RESET" "$1" >&2
  exit 1
}

rd_rule() {
  local width="${RD_RULE_WIDTH:-74}"
  printf '%*s' "$width" '' | tr ' ' '='
}

# Nth line of a multi-line string, for payloads that are structured by line.
rd_line() { printf '%s\n' "$2" | sed -n "$1p"; }

# Indent a block so quoted tool output is visibly not our own prose. Leading
# blank lines are dropped: the aws CLI starts its errors with one.
rd_quote() { printf '%s\n' "$1" | sed -e '/./,$!d' -e 's/^\(..*\)/  \1/'; }

# A failure explained in full: a heading naming the cause, then the ways
# forward, one per argument. Multi-line arguments keep their line breaks, so
# a tool's own output can be quoted verbatim. Exits 1.
#
# Callers must not leave a second conclusion reachable after one of these. On
# the bash 3.2 that macOS ships, errexit does not apply inside a command
# substitution, so a function that fails there ends only that subshell: its
# caller carries on with an empty value and reports something else, and the
# real cause scrolls away above the wrong one. Anything that captures the
# output of a function that can fail therefore propagates the status itself,
# with  x="$(f)" || exit $?  rather than relying on set -e.
rd_fail() {
  local heading="$1" line
  shift
  printf '\n%s%s%s\n' "$RD_RED" "$(rd_rule)" "$RD_RESET" >&2
  printf '%s  %s%s\n' "$RD_RED" "$heading" "$RD_RESET" >&2
  printf '%s%s%s\n\n' "$RD_RED" "$(rd_rule)" "$RD_RESET" >&2
  for line in "$@"; do
    printf '%s\n' "$line" | sed 's/^\(..*\)/  \1/' >&2
  done
  printf '\n' >&2
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

# ---------------------------------------------------------------------------
# Failure translation
#
# docker, aws and the devcontainer CLI each report failures in their own
# vocabulary: "conflict: unable to delete", "error during connect", "The config
# profile could not be found". Every failure that actually happens here has one
# cause and one fix, so call sites run through these wrappers and the message
# names both instead of leaving the tool's wording to be interpreted.
#
# Only calls whose failure is an error go through them. Where a non-zero status
# is the answer to a question, such as "does this volume exist", the command is
# still called directly and the caller acts on the status.
# ---------------------------------------------------------------------------

# Run a command, capturing stderr so a failure can be explained. stdout is left
# alone, so callers can still capture it. On failure the translator is called
# with the exit status, the captured stderr and the command; it does not return.
rd_run() {
  local translator="$1" err status=0 detail
  shift
  err="$(mktemp "${TMPDIR:-/tmp}/rd-run.XXXXXX")"
  "$@" 2> "$err" || status=$?
  detail="$(cat "$err")"
  rm -f "$err"
  [ "$status" -eq 0 ] || "$translator" "$status" "$detail" "$@"
  return "$status"
}

rd_docker() { rd_run rd_docker_failed docker "$@"; }
rd_aws() { rd_run rd_aws_failed aws "$@"; }

# Why the docker engine cannot be reached, and the command that fixes it, as
# two lines: cause, then fix. 'status' and the failure translator both use it,
# so they cannot end up telling you different things.
rd_engine_diagnosis() {
  local context="$1"
  if ! command -v docker > /dev/null 2>&1; then
    printf 'the docker CLI is not installed on this machine.\n'
    printf 'install it: https://docs.docker.com/engine/install/\n'
    return 0
  fi
  if [ "$context" != "$REMOTE_DOCKER_CONTEXT" ]; then
    printf 'the local docker engine behind context '\''%s'\'' is not running.\n' "$context"
    printf 'start Docker (OrbStack, Docker Desktop, dockerd), or target the remote engine: make remote\n'
    return 0
  fi
  if command -v aws > /dev/null 2>&1 \
    && ! aws sts get-caller-identity --profile "$REMOTE_AWS_PROFILE" --region "$REMOTE_AWS_REGION" > /dev/null 2>&1; then
    printf 'the AWS session for profile '\''%s'\'' has expired, which breaks the tunnel.\n' "$REMOTE_AWS_PROFILE"
    printf 'aws sso login --profile %s, then make connect\n' "$REMOTE_AWS_PROFILE"
    return 0
  fi
  printf 'the SSH-over-SSM tunnel to %s has dropped.\n' "$REMOTE_INSTANCE_ID"
  printf 'make connect\n'
}

# Translate a failed docker command. Patterns are the strings docker actually
# emits; each was reproduced against this setup before being matched on.
rd_docker_failed() {
  local status="$1" detail="$2"
  shift 2
  # The command still carries its own binary name, which the untranslated
  # message needs; the subcommand is what the translations key on.
  local invocation="$*"
  shift
  local context diagnosis subject holder

  case "$detail" in
    *'error during connect'* | *'Cannot connect to the Docker daemon'* | *'failed to connect to the docker API'*)
      context="$(docker context show 2> /dev/null || printf 'unknown')"
      diagnosis="$(rd_engine_diagnosis "$context")"
      rd_fail "The docker engine behind context '${context}' is not reachable" \
        "Cause   $(rd_line 1 "$diagnosis")" \
        "Fix     ${RD_BOLD}$(rd_line 2 "$diagnosis")${RD_RESET}" \
        "" \
        "docker reported:" \
        "$(rd_quote "$detail")"
      ;;
    *'context not found'* | *'unable to resolve docker endpoint'*)
      # Listed with DOCKER_CONTEXT cleared: docker echoes that value back as a
      # row of its own, so the missing context would appear in its own list.
      rd_fail "The docker context this run targets does not exist on this machine" \
        "Contexts that do exist:" \
        "$(rd_quote "$(env -u DOCKER_CONTEXT docker context ls --format '{{.Name}}' 2> /dev/null || printf 'none, docker could not list them')")" \
        "" \
        "Set REMOTE_DOCKER_CONTEXT or LOCAL_DOCKER_CONTEXT in shell.env to one of those," \
        "or create the remote one: ${RD_BOLD}make connect${RD_RESET}"
      ;;
    *'is already in use by container'*)
      subject="$(printf '%s' "$detail" | sed -n 's|.*container name "/\{0,1\}\([^"]*\)".*|\1|p')"
      rd_fail "The name '${subject}' already belongs to another container on this engine" \
        "The engine is shared by every project, so names are taken across all of them," \
        "not just this one." \
        "" \
        "Pick a different name, or free this one:" \
        "  ${RD_BOLD}docker rm -f ${subject}${RD_RESET}" \
        "" \
        "What is on the engine now:  ${RD_BOLD}docker ps -a${RD_RESET}"
      ;;
    *'volume is in use'*)
      subject="$(printf '%s' "$detail" | sed -n 's/.*remove \([^:]*\): volume is in use.*/\1/p')"
      holder="$(printf '%s' "$detail" | sed -n 's/.*volume is in use - \[\([^]]*\)\].*/\1/p')"
      rd_fail "Volume '${subject}' is still attached to a container" \
        "Docker will not remove a volume something is using. The container holding it:" \
        "  ${holder}" \
        "" \
        "Remove that container first, then retry:" \
        "  ${RD_BOLD}docker rm -f ${holder}${RD_RESET}" \
        "" \
        "If it belongs to this project, ${RD_BOLD}make clean${RD_RESET} removes both in the right order."
      ;;
    *'is using its referenced image'* | *'image is being used by running container'*)
      holder="$(printf '%s' "$detail" | sed -n 's/.*container \([0-9a-f]\{6,\}\) is using its referenced image.*/\1/p')"
      rd_fail "The image is still in use by another container" \
        "Another instance of this project, or another project, was built from the same" \
        "image, so removing it would break that container." \
        "" \
        "The container still using it:" \
        "  ${holder:-see the message below}" \
        "" \
        "Nothing here is broken: the container and volumes for this project are already" \
        "gone. Leave the image, or remove the other container first and then:" \
        "  ${RD_BOLD}docker rmi <image>${RD_RESET}" \
        "" \
        "docker reported:" \
        "$(rd_quote "$detail")"
      ;;
    *'is not running'*)
      rd_fail "The container is not running, so nothing can be executed in it" \
        "Start it and retry:" \
        "  ${RD_BOLD}make start${RD_RESET}" \
        "" \
        "If it will not start, ${RD_BOLD}make rebuild${RD_RESET} recreates it. On the remote engine" \
        "that re-clones from origin, so run ${RD_BOLD}make check${RD_RESET} first."
      ;;
    *'No such container'* | *'no such object'* | *'No such image'*)
      rd_fail "Docker does not have the object this command names" \
        "docker reported:" \
        "$(rd_quote "$detail")" \
        "" \
        "What exists on this engine:  ${RD_BOLD}make status${RD_RESET}" \
        "" \
        "A container built on the other engine is not visible from here. Check which one" \
        "you are pointed at, then switch with ${RD_BOLD}make local${RD_RESET} or ${RD_BOLD}make remote${RD_RESET}."
      ;;
    *'no space left on device'*)
      rd_fail "The docker engine has run out of disk" \
        "Nothing will build until space is freed. Reclaim what is not in use:" \
        "  ${RD_BOLD}docker system prune${RD_RESET}" \
        "" \
        "On the remote engine, inspect it directly first:" \
        "  ${RD_BOLD}make shell${RD_RESET}, then  df -h /var/lib/docker  and  docker system df"
      ;;
    *'permission denied while trying to connect'*)
      rd_fail "This account is not allowed to talk to the docker socket" \
        "Add it to the docker group, then start a new login session:" \
        "  ${RD_BOLD}sudo usermod -aG docker \"\$USER\"${RD_RESET}" \
        "" \
        "docker reported:" \
        "$(rd_quote "$detail")"
      ;;
    *'OCI runtime exec failed'*)
      rd_fail "The command does not exist inside the container" \
        "The container is running; what was asked for is not installed in it, or is not" \
        "on the PATH that a non-login exec sees." \
        "" \
        "docker reported:" \
        "$(rd_quote "$detail")"
      ;;
    *)
      # 'docker exec' writes its own OCI failure to stdout, not stderr, so a
      # failed exec arrives here with nothing to match on. The exit status is
      # what is left: 126 and 127 are the two the runtime uses when it could
      # not start the command at all.
      if [ "${1:-}" = "exec" ] && [ -z "$detail" ] && { [ "$status" -eq 126 ] || [ "$status" -eq 127 ]; }; then
        rd_fail "The command could not be started inside the container (exit ${status})" \
          "Either it is not installed in the container, or it is not on the PATH that a" \
          "non-login exec sees. docker prints the detail on stdout, so it is in the" \
          "output above rather than here." \
          "" \
          "Check what the container actually has:" \
          "  ${RD_BOLD}docker exec <container> sh -lc 'command -v <command>'${RD_RESET}"
      fi
      rd_fail "${invocation} failed (exit ${status})" \
        "This failure has no translation yet. docker reported:" \
        "$(rd_quote "${detail:-nothing on stderr}")"
      ;;
  esac
}

# Translate a failed aws command. Credentials expiring mid-session is the
# common one and used to surface as an empty result or a bare exit code.
rd_aws_failed() {
  local status="$1" detail="$2"
  shift 2
  local invocation="$*"

  case "$detail" in
    *'could not be found'*)
      rd_fail "AWS profile '${REMOTE_AWS_PROFILE}' is not configured on this machine" \
        "Profiles that are:" \
        "$(rd_quote "$(aws configure list-profiles 2> /dev/null || printf 'none, aws could not list them')")" \
        "" \
        "Set REMOTE_AWS_PROFILE in shell.env to one of those, or create it:" \
        "  ${RD_BOLD}aws configure sso --profile ${REMOTE_AWS_PROFILE}${RD_RESET}"
      ;;
    *'Error loading SSO Token'* | *'session associated with this profile has expired'* \
      | *'ExpiredToken'* | *'Token has expired'* | *'security token included in the request is expired'*)
      rd_fail "The AWS session for profile '${REMOTE_AWS_PROFILE}' has expired" \
        "Everything remote depends on it: the SSM tunnel, Parameter Store, and the" \
        "build that publishes to it." \
        "" \
        "Sign in again, then retry:" \
        "  ${RD_BOLD}aws sso login --profile ${REMOTE_AWS_PROFILE}${RD_RESET}"
      ;;
    *'Unable to locate credentials'*)
      rd_fail "Profile '${REMOTE_AWS_PROFILE}' has no credentials at all" \
        "It resolves to no access key and no SSO session." \
        "" \
        "Sign in:" \
        "  ${RD_BOLD}aws sso login --profile ${REMOTE_AWS_PROFILE}${RD_RESET}" \
        "" \
        "If that profile is the wrong one, set REMOTE_AWS_PROFILE in shell.env."
      ;;
    *'AccessDenied'* | *'not authorized to perform'* | *'UnauthorizedOperation'*)
      rd_fail "Profile '${REMOTE_AWS_PROFILE}' is signed in but not permitted to do this" \
        "The session is valid, the permission is missing, so signing in again will not" \
        "change anything." \
        "" \
        "aws reported:" \
        "$(rd_quote "$detail")"
      ;;
    *'ParameterNotFound'*)
      rd_fail "This project has nothing published in Parameter Store yet" \
        "postCreate bootstraps the container from there, so it has to be published first:" \
        "  ${RD_BOLD}make push-secrets${RD_RESET}"
      ;;
    *'Could not connect to the endpoint URL'*)
      rd_fail "The AWS endpoint for region '${REMOTE_AWS_REGION}' could not be reached" \
        "Either the region is wrong or this machine has no route to AWS." \
        "" \
        "Check REMOTE_AWS_REGION in shell.env, then aws reported:" \
        "$(rd_quote "$detail")"
      ;;
    *)
      rd_fail "${invocation} failed (exit ${status})" \
        "This failure has no translation yet. aws reported:" \
        "$(rd_quote "${detail:-nothing on stderr}")"
      ;;
  esac
}

# Run 'devcontainer up'. Neither of its streams is touched, and that is the
# whole design: this is the one call that renders a live log to the terminal
# for minutes at a time, and redirecting either stream changes how the CLI
# writes. Capturing stdout to parse the summary made the log arrive without
# carriage returns, so every line started where the previous one ended.
#
# Nothing is lost by leaving them alone. The CLI prints the same JSON summary
# to both streams, so it is already the last thing on screen; this only has to
# say what it means and what to do next.
rd_devcontainer_up() {
  local cli="$1" status=0
  shift
  "$cli" up "$@" || status=$?
  if [ "$status" -eq 0 ]; then
    return 0
  fi

  rd_fail "The devcontainer build failed (exit ${status})" \
    "The CLI's summary is the last line above, as {\"outcome\":\"error\",\"message\":...}." \
    "What its message means:" \
    "" \
    "  ${RD_BOLD}Command failed: docker pull ...${RD_RESET}" \
    "      the tag does not exist, or this engine cannot reach the registry." \
    "" \
    "  ${RD_BOLD}Command failed: docker build ...${RD_RESET}" \
    "      a step in the image build failed. It is the last error in the log above." \
    "      When a cached layer has gone stale under a changed feature or base image:" \
    "      make rebuild-no-cache" \
    "" \
    "  ${RD_BOLD}a postCreate, onCreate or postStart command${RD_RESET}" \
    "      the image is fine and a command in .devcontainer exited non-zero. Its own" \
    "      output is above, before the stack trace. Fix it, then: make rebuild" \
    "" \
    "  ${RD_BOLD}no space left on device${RD_RESET}" \
    "      the engine is full. Reclaim space with: docker system prune" \
    "      On the remote engine, look first: make shell, then docker system df"
}
