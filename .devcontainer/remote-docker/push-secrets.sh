#!/usr/bin/env bash

set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
# The one remote transform of the configuration payload, shared with
# container.sh's SSH-engine volume seed: sourcing it here is what keeps a
# second copy of the rules from growing beside the first.
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/config-payload.sh"

rd_load_config
# Resolved, not derived, and resolved before the remote requirements below,
# so the EC2 id they demand is the one exported from the per-instance id
# store by the resolver, never a stale environment value. This script
# publishes secrets, so addressing the wrong instance writes them where
# another instance will read them; the resolver runs before any AWS call for
# that reason.
rd_resolve_instance
rd_require_remote_config

REPO_ROOT="$(cd "${RD_DIR}/../.." && pwd)"
PROJECT_NAME="${PROJECT_NAME:-$(basename "$REPO_ROOT")}"
SHELL_ENV_SOURCE="${SHELL_ENV_SOURCE:-${REPO_ROOT}/shell.env}"
PROFILE_MAP_SOURCE="${PROFILE_MAP_SOURCE:-${REPO_ROOT}/.devcontainer/aws-profile-map.json}"
# The user every docker exec in the ssh delivery runs as, the same default
# container.sh gives its own execs. The delivery branch dereferences it under
# set -u, so it must exist here and not only in container.sh.
: "${CONTAINER_USER:=vscode}"

# The SSH engine's channel is the running container, not Parameter Store:
# an engine declared by engine.json has no Parameter Store behind it, so the
# same transformed payload the EC2 path publishes is written straight into
# the checkout the container already mounts, and the container can be
# refreshed without a rebuild. The resolver has already run (above), so the
# project's workspace path is addressed on the engine this run resolved.
# Every value rides stdin (`cat >`, the same discipline rdc_push_creds
# applies to every credential fragment), never a docker exec's argv, where
# it would sit in the process table for the life of the call. The workspace
# path is the same CONTAINER_WORKSPACES_ROOT + PROJECT_NAME derivation
# container.sh uses; config-payload.sh sources the same default, so the
# BASH_ENV rewrite inside the payload and this destination agree by
# construction. CONTAINER=<name> selects the receiving container by name or
# id, with the same inspect-or-refuse discipline rdc_require_container
# applies: the multi-container refusal below names that variable, so a run
# that answers to it has to be honored here.
rd_deliver_config_payload_to_container() {
  local ids id count context workspace
  rd_require_cmd docker "Install the docker CLI: https://docs.docker.com/engine/install/"
  if [ -n "${CONTAINER:-}" ]; then
    context="$(docker context show 2> /dev/null || printf 'unknown')"
    id="$(docker inspect -f '{{.Id}}' "$CONTAINER" 2> /dev/null)" \
      || rd_fail "There is no container named '${CONTAINER}' on context '${context}'" \
        "CONTAINER selects an instance by name or id, and nothing here answers to that one." \
        "" \
        "What this engine has:  ${RD_BOLD}make status${RD_RESET}"
  else
    ids="$(docker ps -aq --filter "label=devcontainer.project=${PROJECT_NAME}" --filter "status=running")"
    [ -n "$ids" ] || rd_fail "No running container for project '${PROJECT_NAME}' can receive the configuration" \
      "An SSH engine's config channel is the container itself, so one has to be running." \
      "" \
      "Start or build one:" \
      "  ${RD_BOLD}make up${RD_RESET}"
    count="$(printf '%s\n' "$ids" | wc -l | tr -d ' ')"
    [ "$count" -eq 1 ] || rd_fail "${count} running containers for project '${PROJECT_NAME}' exist, so this cannot pick one" \
      "Writing the configuration into the wrong one would leave the others stale." \
      "" \
      "Name the one you mean with ${RD_BOLD}CONTAINER=<name>${RD_RESET}, or tear the extras down first:" \
      "  ${RD_BOLD}docker ps --filter label=devcontainer.project=${PROJECT_NAME}${RD_RESET}"
    id="$(printf '%s\n' "$ids" | sed -n 1p)"
  fi
  workspace="${CONTAINER_WORKSPACES_ROOT}/${PROJECT_NAME}"

  # The payload directory is removed by an EXIT trap, not a RETURN: rd_fail
  # exits, and a RETURN trap never runs on that path. The directory lives in
  # a global, not a local, for the same reason: the trap is evaluated when
  # the whole script exits, after this function's scope is gone, and under
  # set -u an unbound name there turns a successful delivery into a failed
  # run, the same shape the EC2 publish's RD_PUT_DIR below already uses.
  RD_PAYLOAD_DIR="$(umask 077 && mktemp -d "${TMPDIR:-/tmp}/rd-push.XXXXXX")"
  trap 'rm -rf "$RD_PAYLOAD_DIR"' EXIT
  write_config_payload "$RD_PAYLOAD_DIR"

  rd_log "Writing shell.env into the running container (${workspace}/shell.env)..."
  docker exec -i -u "$CONTAINER_USER" "$id" \
    sh -c "umask 077; mkdir -p '${workspace}/.devcontainer'; cat > '${workspace}/shell.env'" \
    < "${RD_PAYLOAD_DIR}/shell.env" \
    || rd_fail "shell.env could not be written into the container" \
      "Nothing else inside it was changed." \
      "" \
      "The container has to be running for this:  ${RD_BOLD}make start${RD_RESET}"
  rd_log "Writing aws-profile-map.json into the running container..."
  docker exec -i -u "$CONTAINER_USER" "$id" \
    sh -c "umask 077; cat > '${workspace}/.devcontainer/aws-profile-map.json'" \
    < "${RD_PAYLOAD_DIR}/.devcontainer/aws-profile-map.json" \
    || rd_fail "aws-profile-map.json could not be written into the container" \
      "Nothing else inside it was changed." \
      "" \
      "The container has to be running for this:  ${RD_BOLD}make start${RD_RESET}"

  # Trust nothing: prove both files landed non-empty, rather than trusting
  # the exec's exit status.
  docker exec -u "$CONTAINER_USER" "$id" \
    sh -c "[ -s '${workspace}/shell.env' ] && [ -s '${workspace}/.devcontainer/aws-profile-map.json' ]" \
    || rd_fail "The configuration files are missing or empty in the container" \
      "They were written moments ago, so the container's filesystem is misbehaving." \
      "" \
      "Re-run:  ${RD_BOLD}make push-secrets${RD_RESET}"
}

if [ "${KIND:-}" = "ssh" ]; then
  rd_deliver_config_payload_to_container
  rd_ok "Configuration delivered to the running '${PROJECT_NAME}' container (SSH engine: no Parameter Store involved)."
  exit 0
fi

# The azure engine's config channel is its own Key Vault, named in the
# engine address record the deploy wrote: the same transformed payload the
# EC2 path publishes to Parameter Store is stored there as two secrets
# (shell-env and profile-map), and the container's postCreate fetches them
# through the VM's managed identity. Every value rides a --file pointing at
# the 0700 trap-cleaned directory the payload was rendered into, never an
# argument -- the same invariant devcontainer_config.certs' KeyVaultClient
# enforces for every secret set it issues, applied at this script's level.
rd_publish_config_payload_to_key_vault() {
  local vault scripts_dir
  scripts_dir="${REPO_ROOT}/.claude/plugins/devcontainer/scripts"
  rd_require_cmd az "Install: brew install azure-cli"
  vault="$(PYTHONPATH="$scripts_dir" python3 -m devcontainer_config.cli secrets-vault "$INSTANCE")" \
    || rd_fail "The Key Vault for instance '${INSTANCE}' could not be read" \
      "The engine address record names it, and the deploy writes that record; the reader's own message is above." \
      "" \
      "Deploy first:  ${RD_BOLD}make instance-deploy INSTANCE=${INSTANCE}${RD_RESET}"

  # The value travels in a --file, never in argv: shell.env carries every
  # credential this repository has, and an argument is visible in the
  # process table to any other process on this machine for as long as the
  # call runs. One directory, created at mode 0700 and removed by an EXIT
  # trap, the same discipline the EC2 publish below applies to its
  # --cli-input-json document.
  RD_PUT_DIR="$(umask 077 && mktemp -d "${TMPDIR:-/tmp}/rd-put.XXXXXX")"
  trap 'rm -rf "$RD_PUT_DIR"' EXIT
  write_config_payload "$RD_PUT_DIR"

  rd_log "Publishing shell-env to Key Vault '${vault}'..."
  az keyvault secret set --vault-name "$vault" --name shell-env \
    --file "${RD_PUT_DIR}/shell.env" > /dev/null \
    || rd_fail "Publishing shell-env to Key Vault '${vault}' failed" \
      "If az reported an authentication failure, sign in and retry:" \
      "  ${RD_BOLD}az login${RD_RESET}, then ${RD_BOLD}make push-secrets${RD_RESET}"

  rd_log "Publishing profile-map to Key Vault '${vault}'..."
  az keyvault secret set --vault-name "$vault" --name profile-map \
    --file "${RD_PUT_DIR}/.devcontainer/aws-profile-map.json" > /dev/null \
    || rd_fail "Publishing profile-map to Key Vault '${vault}' failed" \
      "If az reported an authentication failure, sign in and retry:" \
      "  ${RD_BOLD}az login${RD_RESET}, then ${RD_BOLD}make push-secrets${RD_RESET}"
}

if [ "${KIND:-}" = "azure" ]; then
  rd_publish_config_payload_to_key_vault
  rd_ok "Secrets published to the Key Vault of instance '${INSTANCE}'. Remote workspaces of this project can now bootstrap."
  exit 0
fi

# The gcp engine's config channel is Secret Manager in the project the
# engine address record names: the same transformed payload the EC2 path
# publishes to Parameter Store is stored there as two secrets named under
# the instance's sanitized prefix, and the VM's startup fetch (the
# container's postCreate for the container's own workspace) reads them back
# with the VM's metadata-server identity. Every value rides a --data-file
# pointing into the 0700 trap-cleaned directory the payload was rendered
# into, never an argument -- the same invariant devcontainer_config.certs'
# SecretManagerClient enforces for every secret write it issues, applied at
# this script's level. The create-versus-version-add decision is an
# explicit probe (`gcloud secrets describe`), never a caught error, the
# same probe-before-mutate discipline the state stores apply.
rd_publish_config_payload_to_secret_manager() {
  local prefix project scripts_dir
  scripts_dir="${REPO_ROOT}/.claude/plugins/devcontainer/scripts"
  rd_require_cmd gcloud "Install: brew install google-cloud-sdk"
  prefix="$(PYTHONPATH="$scripts_dir" python3 -m devcontainer_config.cli gcp-secrets-prefix "$INSTANCE")" \
    || rd_fail "The Secret Manager prefix for instance '${INSTANCE}' could not be read" \
      "The engine address record names it, and the deploy writes that record; the reader's own message is above." \
      "" \
      "Deploy first:  ${RD_BOLD}make instance-deploy INSTANCE=${INSTANCE}${RD_RESET}"
  project="$(PYTHONPATH="$scripts_dir" python3 -m devcontainer_config.cli gcp-project-id "$INSTANCE")" \
    || rd_fail "The gcp project for instance '${INSTANCE}' could not be read" \
      "The engine address record names it, and the deploy writes that record; the reader's own message is above." \
      "" \
      "Deploy first:  ${RD_BOLD}make instance-deploy INSTANCE=${INSTANCE}${RD_RESET}"

  # The values travel in --data-file documents, never in argv: shell.env
  # carries every credential this repository has, and an argument is
  # visible in the process table to any other process on this machine for
  # as long as the call runs. One directory, created at mode 0700 and
  # removed by an EXIT trap, the same discipline the EC2 publish below
  # applies to its --cli-input-json document.
  RD_PUT_DIR="$(umask 077 && mktemp -d "${TMPDIR:-/tmp}/rd-put.XXXXXX")"
  trap 'rm -rf "$RD_PUT_DIR"' EXIT
  write_config_payload "$RD_PUT_DIR"

  if gcloud secrets describe "${prefix}-shell-env" --project "$project" > /dev/null 2>&1; then
    rd_log "Publishing ${prefix}-shell-env to Secret Manager (new version)..."
    gcloud secrets versions add "${prefix}-shell-env" --project "$project" \
      --data-file="${RD_PUT_DIR}/shell.env" > /dev/null \
      || rd_fail "Publishing ${prefix}-shell-env to Secret Manager failed" \
        "If gcloud reported an authentication failure, sign in and retry:" \
        "  ${RD_BOLD}gcloud auth login${RD_RESET}, then ${RD_BOLD}make push-secrets${RD_RESET}"
    rd_log "Publishing ${prefix}-profile-map to Secret Manager (new version)..."
    gcloud secrets versions add "${prefix}-profile-map" --project "$project" \
      --data-file="${RD_PUT_DIR}/.devcontainer/aws-profile-map.json" > /dev/null \
      || rd_fail "Publishing ${prefix}-profile-map to Secret Manager failed" \
        "If gcloud reported an authentication failure, sign in and retry:" \
        "  ${RD_BOLD}gcloud auth login${RD_RESET}, then ${RD_BOLD}make push-secrets${RD_RESET}"
  else
    rd_log "Creating ${prefix}-shell-env in Secret Manager..."
    gcloud secrets create "${prefix}-shell-env" --project "$project" \
      --data-file="${RD_PUT_DIR}/shell.env" > /dev/null \
      || rd_fail "Creating ${prefix}-shell-env in Secret Manager failed" \
        "If gcloud reported an authentication failure, sign in and retry:" \
        "  ${RD_BOLD}gcloud auth login${RD_RESET}, then ${RD_BOLD}make push-secrets${RD_RESET}"
    rd_log "Creating ${prefix}-profile-map in Secret Manager..."
    gcloud secrets create "${prefix}-profile-map" --project "$project" \
      --data-file="${RD_PUT_DIR}/.devcontainer/aws-profile-map.json" > /dev/null \
      || rd_fail "Creating ${prefix}-profile-map in Secret Manager failed" \
        "If gcloud reported an authentication failure, sign in and retry:" \
        "  ${RD_BOLD}gcloud auth login${RD_RESET}, then ${RD_BOLD}make push-secrets${RD_RESET}"
  fi
}

if [ "${KIND:-}" = "gcp" ]; then
  rd_publish_config_payload_to_secret_manager
  rd_ok "Secrets published to Secret Manager for instance '${INSTANCE}'. Remote workspaces of this project can now bootstrap."
  exit 0
fi

# The Parameter Store prefix is the EC2 publish's address alone, and the
# resolver's ssh block carries no PARAMETER_PREFIX line: assigning it before
# the ssh branch dereferenced an unset variable under set -u and ended the
# run there. The Linux bash CI runs refuses `${unset%/}` on an unset
# variable, where macOS's stock bash 3.2 silently expands it to empty. The
# ssh branch has exited above, so this assignment runs only where the
# resolver printed the prefix.
SSM_PREFIX="${PARAMETER_PREFIX%/}"

rd_require_cmd aws "Install: https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html"
rd_check_aws_auth

# The value travels inside a --cli-input-json document, never in argv:
# shell.env carries every credential this repository has, and an argument is
# visible in the process table to any other process on this machine for as
# long as the call runs, and is echoed back verbatim by rd_run's own error
# translator. This is the same invariant devcontainer_config.certs'
# ParameterStoreClient enforces for every put-parameter it issues: the value
# travels in a 0600 --cli-input-json file:// document, never an argument.
#
# The document is a real file rather than stdin because the aws CLI v2 does
# not read file:///dev/stdin: it reports "Invalid JSON received" whether stdin
# is a pipe or a redirected regular file, and blocks indefinitely on a FIFO.
#
# One directory, created at mode 0700 and removed by an EXIT trap. The trap is
# on EXIT rather than RETURN because rd_aws reports a failure through rd_fail,
# which calls exit: a RETURN trap never runs on that path, and the document
# would outlive the script holding the secret it carried.
RD_PUT_DIR="$(umask 077 && mktemp -d "${TMPDIR:-/tmp}/rd-put.XXXXXX")"
trap 'rm -rf "$RD_PUT_DIR"' EXIT

# The EC2 publish sources are the shared transform's output, byte-identical
# to what this script's own inline sed produced before the SSH engine's
# volume seed needed the same rules (config-payload.sh is the one owner).
write_config_payload "$RD_PUT_DIR"

rd_put_parameter() {
  local name="$1" type="$2" value="$3" document="${RD_PUT_DIR}/put-parameter.json"
  # printf, not a here-string: a here-string appends a newline, which would
  # store bytes the local file does not have and make every later comparison
  # against it report a difference that is not there.
  ( umask 077
    printf '%s' "$value" \
      | python3 -c 'import json,sys; sys.stdout.write(json.dumps({"Name":sys.argv[1],"Value":sys.stdin.read(),"Type":sys.argv[2],"Overwrite":True}))' \
        "$name" "$type" > "$document" )
  rd_aws ssm put-parameter \
    --profile "$REMOTE_AWS_PROFILE" --region "$REMOTE_AWS_REGION" \
    --cli-input-json "file://${document}" > /dev/null
}

rd_log "Publishing ${SSM_PREFIX}/shell.env (SecureString)..."
rd_put_parameter "${SSM_PREFIX}/shell.env" SecureString "$(cat "${RD_PUT_DIR}/shell.env")"

rd_log "Publishing ${SSM_PREFIX}/aws-profile-map.json (String)..."
rd_put_parameter "${SSM_PREFIX}/aws-profile-map.json" String "$(cat "${RD_PUT_DIR}/.devcontainer/aws-profile-map.json")"

rd_ok "Secrets published for project '${PROJECT_NAME}'. Remote workspaces of this project can now bootstrap."
