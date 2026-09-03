#!/usr/bin/env bash

set -euo pipefail

RD_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

RD_RED=$'\033[0;31m'
RD_GREEN=$'\033[0;32m'
RD_CYAN=$'\033[0;36m'
RD_BOLD=$'\033[1m'
RD_RESET=$'\033[0m'

rd_log() { printf '%s[INFO]%s %s\n' "$RD_CYAN" "$RD_RESET" "$1"; }
rd_ok() { printf '%s[DONE]%s %s\n' "$RD_GREEN" "$RD_RESET" "$1"; }

rd_die() {
  printf '%s[ERROR]%s %s\n' "$RD_RED" "$RD_RESET" "$1" >&2
  exit 1
}

rd_rule() {
  local width="${RD_RULE_WIDTH:-74}"
  printf '%*s' "$width" '' | tr ' ' '='
}

rd_line() { printf '%s\n' "$2" | sed -n "$1p"; }

rd_quote() { printf '%s\n' "$1" | sed -e '/./,$!d' -e 's/^\(..*\)/  \1/'; }

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

rd_load_config() {
  local config_file="${REMOTE_DOCKER_CONFIG:-${RD_DIR}/config.env}"
  [ -f "$config_file" ] || rd_die "config file not found: $config_file"

  local shell_env="${REPO_SHELL_ENV:-${RD_DIR}/../../shell.env}"
  if [ -f "$shell_env" ]; then
    local assignments
    assignments="$(sed -nE -e "s/^[[:space:]]*(export[[:space:]]+)?((REMOTE_|LOCAL_DOCKER_|TINYPROXY_)[A-Z_]+)='(.*)'[[:space:]]*$/: \"\\\${\2:=\4}\"/p" \
      -e 's/^[[:space:]]*(export[[:space:]]+)?((REMOTE_|LOCAL_DOCKER_|TINYPROXY_)[A-Z_]+)="(.*)"[[:space:]]*$/: "${\2:=\4}"/p' \
      -e 's/^[[:space:]]*(export[[:space:]]+)?((REMOTE_|LOCAL_DOCKER_|TINYPROXY_)[A-Z_]+)=([^"'"'"'].*)$/: "${\2:=\4}"/p' "$shell_env" || true)"
    [ -z "$assignments" ] || eval "$assignments"
  fi

  # shellcheck source=config.env
  source "$config_file"
}

# Only remote operations need the EC2 identity, so this is separate from
# rd_load_config: local-engine commands must keep working on a machine where
# none of it is configured.
# The region and profile every AWS call needs, and nothing more. An operation
# that reaches AWS but never names the instance over the wire -- publishing
# this instance's TLS material, which the resolver addresses by name -- must
# require only these, or an unrelated placeholder blocks work that does not
# depend on it.
rd_require_aws_config() {
  : "${REMOTE_AWS_REGION:?REMOTE_AWS_REGION must be set}"
  : "${REMOTE_AWS_PROFILE:?REMOTE_AWS_PROFILE must be set}"
}

rd_require_remote_config() {
  case "${REMOTE_INSTANCE_ID:-}" in
    "<"*">") rd_die "REMOTE_INSTANCE_ID is still the placeholder ${REMOTE_INSTANCE_ID}. Set it in shell.env (see shell.env.example) or export it." ;;
  esac

  : "${REMOTE_INSTANCE_ID:?REMOTE_INSTANCE_ID must be set}"
  rd_require_aws_config
  : "${REMOTE_DOCKER_CONTEXT:?REMOTE_DOCKER_CONTEXT must be set}"
}

rd_require_cmd() {
  local cmd="$1" hint="$2"
  command -v "$cmd" > /dev/null 2>&1 || rd_die "'$cmd' is required but not installed. $hint"
}

rd_check_prereqs() {
  rd_require_cmd aws "Install: https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html"
  rd_require_cmd session-manager-plugin "Install: https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html"
}

# The single place any shell caller turns INSTANCE / DEFAULT_REMOTE_INSTANCE
# into a concrete instance and its addressing block. Every remote entry point
# calls this exactly once, at its own start, and then reads the exported
# values -- rather than each script deriving a context name or parameter
# prefix from PROJECT_NAME on its own, which is how two callers end up
# addressing different instances while believing they agree.
#
# The resolver is `devcontainer_config.cli resolve-instance`, which owns the
# four-step resolution order. This function adds no policy of its own; it
# runs that once, exports what it printed, and translates a failure into the
# repository's own rd_fail shape so the caller sees a remedy rather than a
# traceback.
rd_resolve_instance() {
  [ -n "${RD_INSTANCE_RESOLVED:-}" ] && return 0

  local repo_root scripts_dir block
  repo_root="$(cd "${RD_DIR}/../.." && pwd)"
  scripts_dir="${repo_root}/.claude/plugins/devcontainer/scripts"

  block="$(PYTHONPATH="$scripts_dir" python3 -m devcontainer_config.cli resolve-instance 2>&1)" \
    || rd_fail "Could not resolve which instance to act on" \
      "$(printf '%s' "$block" | sed -n '1,6p')" \
      "" \
      "Name one explicitly:      ${RD_BOLD}INSTANCE=<name> make <target>${RD_RESET}" \
      "Or set a default:         ${RD_BOLD}export DEFAULT_REMOTE_INSTANCE=<name>${RD_RESET}" \
      "" \
      "What is configured:       ${RD_BOLD}make instances${RD_RESET}"

  # Only KEY=VALUE lines are consumed; a warning the resolver printed to
  # stderr has already reached the caller and must not be eval'd.
  local line
  while IFS= read -r line; do
    case "$line" in
      [A-Z_]*=*) export "${line?}" ;;
    esac
  done <<EOF_BLOCK
$block
EOF_BLOCK

  [ -n "${INSTANCE:-}" ] || rd_fail "The resolver returned no instance" \
    "It exited successfully but printed no INSTANCE line, so there is nothing to act on." \
    "" \
    "What is configured:       ${RD_BOLD}make instances${RD_RESET}"

  RD_INSTANCE_RESOLVED=1
  export RD_INSTANCE_RESOLVED
}

rd_check_aws_auth() {
  aws sts get-caller-identity --profile "$REMOTE_AWS_PROFILE" --region "$REMOTE_AWS_REGION" > /dev/null 2>&1 \
    || rd_die "AWS credentials for profile '$REMOTE_AWS_PROFILE' are not valid. Run: aws sso login --profile $REMOTE_AWS_PROFILE"
}


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
    printf 'the AWS session for profile '\''%s'\'' has expired, which breaks the port forward.\n' "$REMOTE_AWS_PROFILE"
    printf 'aws sso login --profile %s, then make connect\n' "$REMOTE_AWS_PROFILE"
    return 0
  fi
  printf 'the SSM port forward to %s has dropped.\n' "$REMOTE_INSTANCE_ID"
  printf 'make connect\n'
}

rd_docker_failed() {
  local status="$1" detail="$2"
  shift 2
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
