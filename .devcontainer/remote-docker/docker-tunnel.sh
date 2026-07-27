#!/usr/bin/env bash
# Connect the local docker CLI (and VS Code Dev Containers) to the remote
# EC2 Docker engine through an SSH tunnel carried inside an IAM-authenticated
# SSM session. No inbound ports are open on the instance; docker traffic is
# never exposed over TCP.
#
# Usage: ./docker-tunnel.sh
# Configuration: see config.env (every value overridable via environment).

set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

rd_load_config
rd_check_prereqs
rd_require_cmd docker "Install the docker CLI (engine not required locally): https://docs.docker.com/engine/install/"
rd_check_aws_auth

PING=$(aws ssm describe-instance-information \
  --profile "$REMOTE_AWS_PROFILE" --region "$REMOTE_AWS_REGION" \
  --filters "Key=InstanceIds,Values=${REMOTE_INSTANCE_ID}" \
  --query 'InstanceInformationList[0].PingStatus' --output text)
[ "$PING" = "Online" ] || rd_die "instance ${REMOTE_INSTANCE_ID} is not Online with SSM (status: ${PING}). Is it running?"

rd_install_ssh_config

rd_log "Verifying SSH over SSM to ${REMOTE_SSH_ALIAS}..."
ssh -o BatchMode=yes "$REMOTE_SSH_ALIAS" true \
  || rd_die "SSH over SSM failed. Check REMOTE_SSH_KEY_PATH and that your AWS session is valid."
rd_ok "SSH tunnel works"

if docker context inspect "$REMOTE_DOCKER_CONTEXT" > /dev/null 2>&1; then
  rd_log "Docker context '${REMOTE_DOCKER_CONTEXT}' already exists - updating endpoint"
  docker context update "$REMOTE_DOCKER_CONTEXT" \
    --docker "host=ssh://${REMOTE_SSH_ALIAS}" > /dev/null
else
  docker context create "$REMOTE_DOCKER_CONTEXT" \
    --description "general-dev remote EC2 docker engine (SSH over SSM)" \
    --docker "host=ssh://${REMOTE_SSH_ALIAS}" > /dev/null
fi

docker context use "$REMOTE_DOCKER_CONTEXT" > /dev/null
rd_log "Verifying remote docker engine..."
SERVER_VERSION=$(docker version --format '{{.Server.Version}}')
rd_ok "Connected: remote docker server ${SERVER_VERSION} via context '${REMOTE_DOCKER_CONTEXT}'"
rd_log "VS Code Dev Containers now targets the remote engine (it follows the active docker context)."
rd_log "Switch back to the local engine anytime with: docker context use default"
