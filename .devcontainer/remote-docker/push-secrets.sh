#!/usr/bin/env bash

set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

rd_load_config
rd_require_remote_config
rd_require_cmd aws "Install: https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html"
rd_check_aws_auth

REPO_ROOT="$(cd "${RD_DIR}/../.." && pwd)"
PROJECT_NAME="${PROJECT_NAME:-$(basename "$REPO_ROOT")}"
SHELL_ENV_SOURCE="${SHELL_ENV_SOURCE:-${REPO_ROOT}/shell.env}"
PROFILE_MAP_SOURCE="${PROFILE_MAP_SOURCE:-${REPO_ROOT}/.devcontainer/aws-profile-map.json}"
# Resolved, not derived. This script publishes secrets, so addressing the
# wrong instance writes them where another instance will read them. The
# resolver runs before any AWS call for that reason.
rd_resolve_instance
SSM_PREFIX="${PARAMETER_PREFIX%/}"

[ -f "$SHELL_ENV_SOURCE" ] || rd_die "shell.env not found at ${SHELL_ENV_SOURCE} (run 'cdevcontainer setup-devcontainer' first)"
[ -f "$PROFILE_MAP_SOURCE" ] || rd_die "aws-profile-map.json not found at ${PROFILE_MAP_SOURCE}"

REMOTE_SHELL_ENV=$(sed \
  -e "s|^export HOST_PROXY=.*|export HOST_PROXY='false'|" \
  -e "s|^export BASH_ENV=.*|export BASH_ENV='/workspaces/${PROJECT_NAME}/shell.env'|" \
  -e '/^export HOST_PROXY_URL=/d' \
  -e '/^export HTTP_PROXY=/d' \
  -e '/^export HTTPS_PROXY=/d' \
  -e '/^export http_proxy=/d' \
  -e '/^export https_proxy=/d' \
  -e '/^export NO_PROXY=/d' \
  -e '/^export no_proxy=/d' \
  -e '/^export PATH=.*\.asdf/d' \
  -e '/^export PATH=.*\.localscripts/d' \
  "$SHELL_ENV_SOURCE")
[ -n "$REMOTE_SHELL_ENV" ] || rd_die "transformed shell.env is empty"

# The value travels inside a --cli-input-json document, never in argv:
# shell.env carries every credential this repository has, and an argument is
# visible in the process table to any other process on this machine for as
# long as the call runs -- and is echoed back verbatim by rd_run's own error
# translator. This is the same invariant devcontainer_config.catalog states
# for a stored secret and devcontainer_config.certs.publish applies to a TLS
# private key.
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
rd_put_parameter "${SSM_PREFIX}/shell.env" SecureString "$REMOTE_SHELL_ENV"

rd_log "Publishing ${SSM_PREFIX}/aws-profile-map.json (String)..."
rd_put_parameter "${SSM_PREFIX}/aws-profile-map.json" String "$(cat "$PROFILE_MAP_SOURCE")"

rd_ok "Secrets published for project '${PROJECT_NAME}'. Remote workspaces of this project can now bootstrap."
