#!/usr/bin/env bash
# Open an interactive shell (zsh) on the remote EC2 Docker host over the
# SSM-carried SSH tunnel.
#
# Usage: ./shell.sh [command...]   (no args = interactive login shell)

set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

rd_load_config
rd_check_prereqs
rd_check_aws_auth
rd_install_ssh_config

if [ "$#" -gt 0 ]; then
  exec ssh -t "$REMOTE_SSH_ALIAS" "$@"
fi
exec ssh -t "$REMOTE_SSH_ALIAS"
