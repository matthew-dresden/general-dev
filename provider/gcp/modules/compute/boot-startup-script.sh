#!/bin/sh
# The gcp kind's boot-applied install: the startup-script metadata the
# google guest agent runs as root at EVERY boot (the cloud-init twin in
# boot-user-data.yaml runs once, from user-data). The contract this script
# implements is stated in devcontainer_config.transport_gcp's boot-applied
# comments and devcontainer_config.certs' publish flow, and this file is
# deliberately literal about it:
#
# - It waits for the cloud-init provisioning marker (bounded loop, never an
#   unbounded wait), because on the FIRST boot the guest agent can run this
#   script before cloud-init has finished building the daemon this script
#   serves.
# - It fetches the three TLS material secrets and the checksum secret from
#   Secret Manager's REST API, authenticating with the VM service account's
#   OAuth token from the metadata server. The token is passed to the API
#   curl through stdin (`-H @-`), never as an argv element: an argv token
#   would be readable from the process table for the length of the call.
# - It compares the fetched checksum against the root-owned state file
#   /etc/general-dev/applied-checksum -- 0600, written only by this script
#   after a successful apply and restart, so nothing short of host root can
#   forge "already applied" and defeat rotation. The general-dev/tls-checksum
#   guest attribute is written beside it as a pure REPORTING channel (the
#   laptop-side waits read it) but is never trusted for the drift decision:
#   the attribute is VM-writable through the metadata API by any process on
#   the VM, and a value a process could plant there would pin the daemon on
#   stale material forever. On drift or first apply: apply the material
#   through apply_material (whose every failure is recorded, never a bare
#   set -e exit that would leave mixed material unrecorded), write the
#   state file and the attribute, and only then echo the sentinel. On no
#   drift: skip the restart.
# - On a pre-publish boot (no checksum secret yet) it exits 0 quietly with
#   an explanatory line: provisioning already handled its own marker, and
#   a first boot that precedes the first publish is a success.
# - On any failure it records general-dev/error with the failing step and
#   exits 1; on every SUCCESS path it clears general-dev/error, so a stale
#   diagnostic from an earlier boot can never decorate a wait that is
#   actually about to succeed. The laptop-side waits read exactly these
#   attributes (wait_applied_checksum, wait_provisioned); a poisoned error
#   attribute can at most delay them, never make them skip a step.
#
# Rendered by templatefile() with exactly three values -- secrets_prefix,
# project_id and daemon_user -- so every other literal below is fixed by
# this contract. The shell is written POSIX-clean (no `[[`, no `local`, no
# brace expansions) because the guest agent's interpreter and its
# sh-compatible shells differ; shell expansions use the dollar-variable
# form without braces, and the template's own interpolations are the three
# placeholder assignments below this paragraph and no others.

set -eu

# The three rendered values: the secrets prefix (the sanitized instance
# name the publication flows mint their ids from), the project the secrets
# live in, and the daemon account the material is owned by and the unit
# restarted under. They are read out of a heredoc rather than assigned as
# bare expansions because the placeholders are substituted by templatefile
# at render time, never shell variables -- the heredoc keeps the render
# seam data instead of code a shell reader would misread as unassigned
# references. The four values are validated upstream as space-free tokens
# (an RFC1035 prefix, a project id, a daemon account name and a number),
# so one read line is exact. The delimiter is quoted: the placeholders are
# substituted before the shell ever runs this file, so the body is
# literal data at execution time and no shell expansion belongs in it.
read -r SECRETS_PREFIX PROJECT_ID DAEMON_USER MARKER_TIMEOUT_DEFAULT <<'RENDERED_VALUES'
${secrets_prefix} ${project_id} ${daemon_user} ${marker_timeout_default}
RENDERED_VALUES

# Fixed contract values: the guest-attribute namespace and keys
# (transport_gcp's constants), the metadata server (the fixed name every
# gcp VM resolves to the link-local metadata address), the Secret Manager
# API root, the four leaf names the TLS publication uses, the readiness
# marker cloud-init writes last, and the root-owned state file the drift
# decision trusts (this script is its only writer; see the header).
NAMESPACE="general-dev"
CHECKSUM_KEY="tls-checksum"
PROVISIONED_KEY="provisioned"
ERROR_KEY="error"
PROVISIONED_VALUE="TRUE"
METADATA="http://metadata.google.internal/computeMetadata/v1"
SECRET_API="https://secretmanager.googleapis.com/v1"
MARKER="/etc/general-dev-provisioned"
APPLIED_STATE="/etc/general-dev/applied-checksum"
SENTINEL="general-dev: tls material applied"

# The marker wait's budget, in seconds, from the terraform variable the
# instance metadata renders in; the default covers a first boot where
# cloud-init is still installing packages, and its exhaustion is a
# failure, never a silent continue.
MARKER_TIMEOUT="$MARKER_TIMEOUT_DEFAULT"

TLS_DIR="/home/$DAEMON_USER/tls"

console() {
    # Everything the operator needs on the serial console as well as in
    # the guest agent's log.
    echo "$1" | tee /dev/console
}

record_error() {
    # The failing step's diagnostic, recorded so the laptop-side waits can
    # surface it. Best-effort by design -- a failed record must not mask
    # the original failure's exit code -- but a failed record is itself
    # announced on the console, never silently dropped.
    if ! curl -sf -X PUT -H 'Metadata-Flavor: Google' \
            --data "$1" \
            "$METADATA/instance/guest-attributes/$NAMESPACE/$ERROR_KEY" >/dev/null 2>&1; then
        console "general-dev: could not record the error attribute: $1"
    fi
}

fail() {
    console "general-dev: FAILED at '$1': $2"
    record_error "$1: $2"
    exit 1
}

put_attribute() {
    curl -sf -X PUT -H 'Metadata-Flavor: Google' \
        --data "$2" \
        "$METADATA/instance/guest-attributes/$NAMESPACE/$1" >/dev/null
}

clear_error() {
    # Every success path clears the error attribute: a diagnostic left by
    # an earlier boot's failure must never decorate a wait that is about
    # to succeed. Best-effort like record_error -- a failed clear is
    # announced, never fatal, because the material is already applied.
    if ! put_attribute "$ERROR_KEY" ""; then
        console "general-dev: could not clear the error attribute (the material itself is fine)"
    fi
}

fetch_secret() {
    # fetch_secret LEAF OUTFILE: the secret's latest version payload,
    # base64-decoded into OUTFILE. Returns 3 when the secret is confirmed
    # absent (HTTP 404 -- the pre-publish state), 4 on any other failure.
    # The OAuth token reaches curl through stdin as a header document
    # (`-H @-`), never through argv.
    _fs_leaf="$1"
    _fs_out="$2"
    _fs_code="$(curl -s -o "$_fs_out.raw" -w '%%{http_code}' -H @- \
        "$SECRET_API/projects/$PROJECT_ID/secrets/$SECRETS_PREFIX-$_fs_leaf/versions/latest:access" \
        <<AUTH
Authorization: Bearer $TOKEN
AUTH
    )" || return 4
    if [ "$_fs_code" = "404" ]; then
        return 3
    fi
    if [ "$_fs_code" != "200" ]; then
        return 4
    fi
    sed -n 's/.*"data": *"\([^"]*\)".*/\1/p' "$_fs_out.raw" > "$_fs_out.b64"
    if [ ! -s "$_fs_out.b64" ]; then
        return 4
    fi
    base64 -d "$_fs_out.b64" > "$_fs_out"
}

# Scratch space for the fetches and the staged material, removed on every
# exit path.
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

# --- Wait for provisioning ----------------------------------------------

waited=0
while [ ! -f "$MARKER" ]; do
    if [ "$waited" -ge "$MARKER_TIMEOUT" ]; then
        fail "wait for the provisioning marker" \
            "$MARKER never appeared within $MARKER_TIMEOUT seconds; cloud-init has not finished"
    fi
    sleep 5
    waited=$((waited + 5))
done

# --- Fetch the material's truth from Secret Manager ----------------------

if ! TOKEN_JSON="$(curl -sf -H 'Metadata-Flavor: Google' \
        "$METADATA/instance/service-accounts/default/token")"; then
    fail "fetch the metadata-server token" \
        "the VM service account's token endpoint did not answer"
fi
TOKEN="$(printf '%s' "$TOKEN_JSON" | sed -n 's/.*"access_token": *"\([^"]*\)".*/\1/p')"
if [ -z "$TOKEN" ]; then
    fail "extract the access token" "the token response carried no access_token field"
fi

# The checksum secret is the publish flow's LAST write, so its absence is
# exactly the not-yet-published state: a boot that precedes the first
# publish is a success (provisioning owns its own marker), recorded in the
# provisioned attribute and closed quietly.
if fetch_secret tls-checksum "$TMP_DIR/checksum"; then
    :
else
    fetch_rc=$?
    if [ "$fetch_rc" = "3" ]; then
        console "general-dev: no TLS material published yet (no checksum secret under $SECRETS_PREFIX-); nothing to apply on this boot"
        put_attribute "$PROVISIONED_KEY" "$PROVISIONED_VALUE"
        clear_error
        exit 0
    fi
    fail "fetch the checksum secret" \
        "the checksum secret under $SECRETS_PREFIX- could not be read (fetch returned $fetch_rc)"
fi
CHECKSUM="$(tr -d '[:space:]' < "$TMP_DIR/checksum")"
if [ -z "$CHECKSUM" ]; then
    fail "extract the checksum" "the checksum secret decoded to nothing"
fi

if fetch_secret tls-server-key "$TMP_DIR/server-key"; then
    :
else
    fail "fetch the server key" \
        "the checksum secret exists but its material could not be read; the store is inconsistent"
fi
if fetch_secret tls-server-cert "$TMP_DIR/server-cert"; then
    :
else
    fail "fetch the server certificate" \
        "the checksum secret exists but its material could not be read; the store is inconsistent"
fi
if fetch_secret tls-ca "$TMP_DIR/ca"; then
    :
else
    fail "fetch the CA certificate" \
        "the checksum secret exists but its material could not be read; the store is inconsistent"
fi

# --- Drift decision ------------------------------------------------------

# The comparison is against the root-owned state file, never the
# tls-checksum guest attribute: the attribute is writable by any process
# on the VM through the metadata API, and a planted value would read as
# "already applied" and silently defeat rotation. This file is written
# only by the apply path below, after the material and the restart have
# both succeeded, so its value is this script's own witness.
applied=""
if [ -f "$APPLIED_STATE" ]; then
    applied="$(tr -d '[:space:]' < "$APPLIED_STATE")"
fi
if [ "$applied" = "$CHECKSUM" ]; then
    # No drift: the applied material is exactly the published one. The
    # daemon keeps running untouched; only the readiness attribute is
    # refreshed, and any error a previous boot recorded is cleared.
    put_attribute "$PROVISIONED_KEY" "$PROVISIONED_VALUE"
    clear_error
    exit 0
fi

# --- Apply on drift or first boot -----------------------------------------

# apply_material: the installs and the daemon restart, in the one order
# the state file's meaning depends on -- stage every file first, then
# restart, then write the state file LAST, so a value in it always
# describes material that is live. Every failure is routed through fail
# (which records the error attribute): a bare set -e exit here would leave
# mixed material on disk with nothing recorded, and a state file that
# still names the old checksum is exactly the "drifted" state the next
# boot must see, never a half-apply reported as applied.
apply_material() {
    install -d -m 0700 -o "$DAEMON_USER" -g "$DAEMON_USER" "$TLS_DIR" || return 1
    install -m 0600 -o "$DAEMON_USER" -g "$DAEMON_USER" \
        "$TMP_DIR/server-key" "$TLS_DIR/server-key.pem" || return 1
    install -m 0644 -o "$DAEMON_USER" -g "$DAEMON_USER" \
        "$TMP_DIR/server-cert" "$TLS_DIR/server-cert.pem" || return 1
    install -m 0644 -o "$DAEMON_USER" -g "$DAEMON_USER" \
        "$TMP_DIR/ca" "$TLS_DIR/ca.pem" || return 1
    # Restart the rootless daemon under its own user manager: runuser execs
    # directly (the account's nologin shell is never consulted) and the
    # environment it needs is passed on the command line, the same pattern
    # provisioning itself uses. The restart is what makes the new material
    # live; skipping it on the no-drift path above is the point of the
    # drift guard.
    DAEMON_UID="$(id -u "$DAEMON_USER")"
    runuser -u "$DAEMON_USER" -- \
            env HOME="/home/$DAEMON_USER" \
            XDG_RUNTIME_DIR="/run/user/$DAEMON_UID" \
            DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$DAEMON_UID/bus" \
            systemctl --user restart docker.service || return 1
    # The state file records what is NOW live, only after the apply and
    # the restart both succeeded: 0600, root-owned, the drift decision's
    # single trusted input.
    printf '%s\n' "$CHECKSUM" > "$APPLIED_STATE" || return 1
    chmod 0600 "$APPLIED_STATE" || return 1
}

if ! apply_material; then
    fail "apply the TLS material" \
        "staging the material or restarting the rootless daemon failed; the state file still names what was live before this boot"
fi

# Record what was applied only after the apply and the restart succeeded,
# then the readiness marker, then the sentinel -- in that order, because
# the laptop-side waits gate on the attributes and must never see them
# ahead of the state they describe.
put_attribute "$CHECKSUM_KEY" "$CHECKSUM"
put_attribute "$PROVISIONED_KEY" "$PROVISIONED_VALUE"
clear_error
console "$SENTINEL ($CHECKSUM)"
