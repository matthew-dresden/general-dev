#!/usr/bin/env bash
#
# The operator entry point for one instance's mTLS material (spec Section 4.5).
#
# `devcontainer_config.certs` owns every certificate operation; this script
# adds no policy of its own. It resolves which instance is being acted on --
# once, through the shared resolver, so the material is written under the same
# directory `make connect` and `make instances` address -- and dispatches.
#
# The three subcommands are deliberately separate rather than one idempotent
# "ensure" target. Creating a CA, issuing a client certificate and publishing
# server material each refuse to overwrite what is already there, so an
# operator who runs the wrong one gets a message naming the existing path
# instead of a silently replaced certificate that every other instance of the
# pair no longer trusts.

set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

usage() {
  rd_fail "certs.sh needs one subcommand" \
    "Got: ${1:-<none>}" \
    "" \
    "  ${RD_BOLD}make cert-ca${RD_RESET}       Create this instance's certificate authority." \
    "  ${RD_BOLD}make cert-client${RD_RESET}   Issue the client certificate 'make connect' presents." \
    "  ${RD_BOLD}make cert-publish${RD_RESET}  Issue server material and publish it to Parameter Store." \
    "  ${RD_BOLD}make cert-install${RD_RESET}  Have the instance fetch it and start its daemon." \
    "" \
    "Expiry of what already exists: ${RD_BOLD}make cert-status${RD_RESET}"
}

command="${1:-}"
[ -n "$command" ] || usage ""

rd_load_config
# Resolved, not derived: this script writes certificate material, and
# addressing the wrong instance issues a certificate the daemon will reject.
rd_resolve_instance

REPO_ROOT="$(cd "${RD_DIR}/../.." && pwd)"
SCRIPTS_DIR="${REPO_ROOT}/.claude/plugins/devcontainer/scripts"
# The resolver owns the certificate directory (spec Section 9); its parent is
# the root the module takes, so an operator who sets DOCKER_CONFIG has both
# halves agree rather than writing under one directory and reading another.
CERTS_ROOT="$(dirname "$CERTS_DIR")"

run_certs() {
  PYTHONPATH="$SCRIPTS_DIR" python3 -m devcontainer_config.certs "$@" \
    --instance "$INSTANCE" --root "$CERTS_ROOT"
}

case "$command" in
  ca)
    run_certs create-ca
    rd_ok "Certificate authority created for '${INSTANCE}'. Next: make cert-client."
    ;;
  client)
    run_certs issue-client
    rd_ok "Client certificate issued for '${INSTANCE}'. 'make connect' can now present it."
    ;;
  install)
    rd_require_remote_config
    rd_require_cmd aws "Install: https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html"
    rd_check_aws_auth
    PYTHONPATH="$SCRIPTS_DIR" python3 -m devcontainer_config.transport install-material \
      --instance "$INSTANCE" \
      --instance-id "$REMOTE_INSTANCE_ID" \
      --profile "$REMOTE_AWS_PROFILE" \
      --region "$REMOTE_AWS_REGION" \
      --daemon-user "$REMOTE_DAEMON_USER"
    rd_ok "'${INSTANCE}' installed its TLS material and started its daemon."
    ;;
  publish)
    # Region and profile only: this publishes to a path the resolver addressed
    # by instance name, and never names an instance id over the wire.
    rd_require_aws_config
    rd_require_cmd aws "Install: https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html"
    rd_check_aws_auth
    # AWS_PROFILE rather than a --profile flag: the catalog client reaches the
    # store through the aws CLI's own credential resolution and takes no
    # profile argument, so the profile is passed the way the CLI reads it.
    AWS_PROFILE="$REMOTE_AWS_PROFILE" run_certs publish --region "$REMOTE_AWS_REGION"
    rd_ok "TLS material published for '${INSTANCE}'. The daemon can now open its listener."
    ;;
  *)
    usage "$command"
    ;;
esac
