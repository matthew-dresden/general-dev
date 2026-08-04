#!/usr/bin/env bash

set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

rd_load_config
rd_check_prereqs
rd_require_cmd docker "Install the docker CLI (engine not required locally): https://docs.docker.com/engine/install/"
rd_check_aws_auth

PING=$(rd_aws ssm describe-instance-information \
  --profile "$REMOTE_AWS_PROFILE" --region "$REMOTE_AWS_REGION" \
  --filters "Key=InstanceIds,Values=${REMOTE_INSTANCE_ID}" \
  --query 'InstanceInformationList[0].PingStatus' --output text)
[ "$PING" = "Online" ] || rd_fail "Instance ${REMOTE_INSTANCE_ID} is not reachable through SSM" \
  "SSM reports its ping status as '${PING}'. The tunnel is carried inside an SSM" \
  "session, so nothing can connect until that says Online." \
  "" \
  "'None' means SSM has no record of the instance: it is stopped, terminated, or" \
  "REMOTE_INSTANCE_ID in shell.env names the wrong one." \
  "" \
  "Check what it is doing:" \
  "  ${RD_BOLD}aws ec2 describe-instances --instance-ids ${REMOTE_INSTANCE_ID} --region ${REMOTE_AWS_REGION} --profile ${REMOTE_AWS_PROFILE} --query 'Reservations[0].Instances[0].State.Name'${RD_RESET}" \
  "" \
  "If it is stopped, start it and wait for the agent to register, then: ${RD_BOLD}make connect${RD_RESET}"

rd_install_ssh_config

rd_log "Verifying SSH over SSM to ${REMOTE_SSH_ALIAS}..."
ssh -o BatchMode=yes "$REMOTE_SSH_ALIAS" true \
  || rd_fail "SSH into ${REMOTE_INSTANCE_ID} over the SSM session failed" \
    "The instance answers SSM, so the session opened and the SSH layer inside it is" \
    "what refused. ssh's own message is above." \
    "" \
    "  'Permission denied' means the key is wrong: REMOTE_SSH_KEY_PATH is" \
    "      ${REMOTE_SSH_KEY_PATH}, and it must match the instance's key pair." \
    "" \
    "  A timeout or a TargetNotConnected error means the SSM session could not be" \
    "      established: ${RD_BOLD}aws sso login --profile ${REMOTE_AWS_PROFILE}${RD_RESET}, then retry." \
    "" \
    "  'session-manager-plugin not found' means the plugin is missing from PATH."
rd_ok "SSH tunnel works"

if docker context inspect "$REMOTE_DOCKER_CONTEXT" > /dev/null 2>&1; then
  rd_log "Docker context '${REMOTE_DOCKER_CONTEXT}' already exists - updating endpoint"
  rd_docker context update "$REMOTE_DOCKER_CONTEXT" \
    --docker "host=ssh://${REMOTE_SSH_ALIAS}" > /dev/null
else
  rd_docker context create "$REMOTE_DOCKER_CONTEXT" \
    --description "general-dev remote EC2 docker engine (SSH over SSM)" \
    --docker "host=ssh://${REMOTE_SSH_ALIAS}" > /dev/null
fi

rd_docker context use "$REMOTE_DOCKER_CONTEXT" > /dev/null
rd_log "Verifying remote docker engine..."
SERVER_VERSION=$(rd_docker version --format '{{.Server.Version}}')
rd_ok "Connected: remote docker server ${SERVER_VERSION} via context '${REMOTE_DOCKER_CONTEXT}'"
rd_log "VS Code Dev Containers now targets the remote engine (it follows the active docker context)."
rd_log "Switch back to the local engine anytime with: docker context use default"
