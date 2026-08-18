#!/usr/bin/env bash

set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

rd_load_config
rd_require_remote_config
rd_check_prereqs
rd_check_aws_auth
rd_install_ssh_config

if [ "$#" -gt 0 ]; then
  exec ssh -t "$REMOTE_SSH_ALIAS" "$@"
fi
exec ssh -t "$REMOTE_SSH_ALIAS"
