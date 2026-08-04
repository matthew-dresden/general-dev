#!/usr/bin/env bash

set -euo pipefail

case "$(uname -s)" in
    Darwin) HOST_FAMILY="macOS";  TINYPROXY_INSTALL_HINT="brew install tinyproxy" ;;
    *)      if grep -qi microsoft /proc/version 2>/dev/null; then
                HOST_FAMILY="WSL"
            else
                HOST_FAMILY="Linux"
            fi
            TINYPROXY_INSTALL_HINT="sudo apt-get install tinyproxy" ;;
esac

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE_FILE="${TINYPROXY_TEMPLATE_FILE:-${SCRIPT_DIR}/tinyproxy.conf.template}"
LOG_DIR="${TINYPROXY_STATE_DIR:-${HOME}/.devcontainer-proxy}"
RUNTIME_CONFIG="${LOG_DIR}/tinyproxy.conf"
PID_FILE="${LOG_DIR}/tinyproxy.pid"
LOG_FILE="${LOG_DIR}/tinyproxy.log"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

log_info() {
    echo -e "${BLUE}ℹ${NC}  $1"
}

log_success() {
    echo -e "${GREEN}✓${NC}  $1"
}

log_warn() {
    echo -e "${YELLOW}⚠${NC}  $1"
}

log_error() {
    echo -e "${RED}✗${NC}  $1" >&2
}

exit_with_error() {
    log_error "$1"
    exit 1
}

detect_shell_rc() {
    local current_shell="${SHELL:-}"
    case "${current_shell}" in
        */zsh)  echo "${HOME}/.zshrc" ;;
        */bash) echo "${HOME}/.bashrc" ;;
        *)      echo "" ;;
    esac
}

print_env_guidance() {
    local var_name="$1"
    local example_value="$2"
    local rc_file
    rc_file="$(detect_shell_rc)"

    log_error "${var_name} is not set."
    echo "" >&2
    echo "  Export it before running this script:" >&2
    echo "    export ${var_name}=${example_value}" >&2
    echo "" >&2
    if [[ -n "${rc_file}" ]]; then
        echo "  For persistence, add it to ${rc_file}:" >&2
        echo "    echo 'export ${var_name}=${example_value}' >> ${rc_file}" >&2
    else
        echo "  For persistence, add it to your shell rc file (~/.bashrc or ~/.zshrc):" >&2
        echo "    echo 'export ${var_name}=${example_value}' >> ~/.bashrc  # or ~/.zshrc" >&2
    fi
}

validate_required_env() {
    local missing=0

    if [[ -z "${TINYPROXY_UPSTREAM_HOST:-}" ]]; then
        print_env_guidance "TINYPROXY_UPSTREAM_HOST" "edge.surepath.ai"
        missing=1
    fi

    if [[ -z "${TINYPROXY_UPSTREAM_PORT:-}" ]]; then
        print_env_guidance "TINYPROXY_UPSTREAM_PORT" "8080"
        missing=1
    fi

    if [[ -z "${TINYPROXY_PORT:-}" ]]; then
        print_env_guidance "TINYPROXY_PORT" "3128"
        missing=1
    fi

    if [[ -z "${TINYPROXY_READINESS_TIMEOUT:-}" ]]; then
        print_env_guidance "TINYPROXY_READINESS_TIMEOUT" "10"
        missing=1
    fi

    if [[ -z "${TINYPROXY_STOP_TIMEOUT:-}" ]]; then
        print_env_guidance "TINYPROXY_STOP_TIMEOUT" "10"
        missing=1
    fi

    if [[ ${missing} -ne 0 ]]; then
        exit 1
    fi
}

warn_if_in_container() {
    local in_container=false

    if [[ -f "/.dockerenv" ]]; then
        in_container=true
    elif grep -qE '(/docker/|/lxc/)' /proc/1/cgroup 2>/dev/null; then
        in_container=true
    fi

    if [[ "${in_container}" == "true" ]]; then
        log_warn "It looks like you are running inside a Docker container."
        log_warn "This script is intended to run on the HOST operating system (detected: ${HOST_FAMILY}),"
        log_warn "not inside the devcontainer. The devcontainer connects to this proxy via host.docker.internal."
        echo ""
        read -rp "Do you still want to continue? [y/N] " response
        case "${response}" in
            [yY][eE][sS]|[yY])
                log_info "Continuing inside container as requested..."
                ;;
            *)
                exit_with_error "Aborted. Run this script on your ${HOST_FAMILY} host instead."
                ;;
        esac
    fi
}

mkdir -p "${LOG_DIR}"

check_tinyproxy() {
    if ! command -v tinyproxy &> /dev/null; then
        exit_with_error "tinyproxy is not installed. Install it with: ${TINYPROXY_INSTALL_HINT}"
    fi
}

if command -v ss &> /dev/null; then
    PORT_INSPECTOR="ss"
elif command -v lsof &> /dev/null; then
    PORT_INSPECTOR="lsof"
else
    PORT_INSPECTOR="none"
fi
case "${PORT_INSPECTOR}" in
    ss)   PORT_INSPECT_HINT="ss -tlnp | grep :<port>" ;;
    lsof) PORT_INSPECT_HINT="lsof -i :<port>" ;;
    *)    PORT_INSPECT_HINT="(install ss or lsof)" ;;
esac

require_port_inspector() {
    [[ "${PORT_INSPECTOR}" != "none" ]] \
        || exit_with_error "neither 'ss' nor 'lsof' is available to inspect port usage, install one of them"
}

port_listeners() {
    local port="$1"
    require_port_inspector
    case "${PORT_INSPECTOR}" in
        ss)   ss -tlnp 2>/dev/null | grep ":${port} " || true ;;
        lsof) lsof -iTCP:"${port}" -sTCP:LISTEN -P 2>/dev/null | tail -n +2 || true ;;
    esac
}

check_port_listening() {
    [[ -n "$(port_listeners "$1")" ]]
}

port_listener_pids() {
    local listeners
    listeners="$(port_listeners "$1")"
    [[ -n "${listeners}" ]] || return 0
    case "${PORT_INSPECTOR}" in
        ss)   printf '%s\n' "${listeners}" | grep -oE 'pid=[0-9]+' | cut -d= -f2 | sort -u ;;
        lsof) printf '%s\n' "${listeners}" | awk '{print $2}' | sort -u ;;
    esac
}

resolve_group() {
    id -gn 2>/dev/null || exit_with_error "Failed to resolve primary group for user '${USER}'"
}

generate_config() {
    if [[ ! -f "${TEMPLATE_FILE}" ]]; then
        exit_with_error "Template file not found: ${TEMPLATE_FILE}"
    fi

    local group
    group="$(resolve_group)"

    sed \
        -e "s|__USER__|${USER}|g" \
        -e "s|__GROUP__|${group}|g" \
        -e "s|__LOG_DIR__|${LOG_DIR}|g" \
        -e "s|__UPSTREAM_HOST__|${TINYPROXY_UPSTREAM_HOST}|g" \
        -e "s|__UPSTREAM_PORT__|${TINYPROXY_UPSTREAM_PORT}|g" \
        -e "s|__LISTEN_PORT__|${TINYPROXY_PORT}|g" \
        "${TEMPLATE_FILE}" > "${RUNTIME_CONFIG}"

    log_info "Generated runtime config: ${RUNTIME_CONFIG}"
}

get_pid() {
    if [[ -f "${PID_FILE}" ]]; then
        local pid
        pid=$(cat "${PID_FILE}")
        if ps -p "${pid}" > /dev/null 2>&1; then
            echo "${pid}"
            return 0
        else
            rm -f "${PID_FILE}"
        fi
    fi
    return 1
}

wait_for_ready() {
    local port="$1"
    local elapsed=0

    while [[ ${elapsed} -lt ${TINYPROXY_READINESS_TIMEOUT} ]]; do
        if get_pid > /dev/null 2>&1 && check_port_listening "${port}"; then
            return 0
        fi
        sleep 0.5
        elapsed=$((elapsed + 1))
    done
    return 1
}

wait_for_stop() {
    local pid="$1"
    local elapsed=0

    while ps -p "${pid}" > /dev/null 2>&1 && [[ ${elapsed} -lt ${TINYPROXY_STOP_TIMEOUT} ]]; do
        sleep 0.5
        elapsed=$((elapsed + 1))
    done

    ps -p "${pid}" > /dev/null 2>&1
}

check_port_available() {
    local port="$1"
    local listeners
    listeners="$(port_listeners "${port}")"

    if [[ -z "${listeners}" ]]; then
        return 0
    fi

    log_error "Port ${port} is already in use by another process:"
    echo "" >&2
    printf '    %s\n' "${listeners}" >&2
    echo "" >&2

    local pids
    pids="$(port_listener_pids "${port}")"
    if [[ -n "${pids}" ]]; then
        log_warn "Processes using port ${port}:"
        while read -r pid; do
            log_warn "  PID ${pid} ($(ps -p "${pid}" -o comm= 2>/dev/null || echo unknown))"
        done <<< "${pids}"
        echo "" >&2
    fi

    log_info "To inspect port ${port}:      ${PORT_INSPECT_HINT}"
    log_info "To kill a specific process:  kill <PID>"
    log_info "To kill every tinyproxy:     pkill -9 tinyproxy"

    exit 1
}

start_proxy() {
    warn_if_in_container
    log_info "Starting tinyproxy daemon..."

    check_tinyproxy
    validate_required_env

    if get_pid > /dev/null 2>&1; then
        local pid
        pid=$(get_pid)
        log_warn "tinyproxy is already running (PID: ${pid})"
        return 0
    fi

    check_port_available "${TINYPROXY_PORT}"

    generate_config

    log_info "Config file: ${RUNTIME_CONFIG}"
    log_info "Listening on: 0.0.0.0:${TINYPROXY_PORT}"
    log_info "Upstream proxy: ${TINYPROXY_UPSTREAM_HOST}:${TINYPROXY_UPSTREAM_PORT}"
    log_info "Log file: ${LOG_FILE}"

    tinyproxy -c "${RUNTIME_CONFIG}"

    if wait_for_ready "${TINYPROXY_PORT}"; then
        local pid
        pid=$(get_pid)
        log_success "tinyproxy started successfully (PID: ${pid})"
        log_success "Proxy is listening on port ${TINYPROXY_PORT}"

        echo ""
        log_info "To view logs in real-time:"
        log_info "  tail -f ${LOG_FILE}"
        echo ""
        log_info "To check status:"
        log_info "  ./tinyproxy-daemon.sh status"
    else
        exit_with_error "Failed to start tinyproxy within ${TINYPROXY_READINESS_TIMEOUT}s. Check logs: ${LOG_FILE}"
    fi
}

stop_proxy() {
    log_info "Stopping tinyproxy daemon..."

    validate_required_env

    if ! get_pid > /dev/null 2>&1; then
        log_warn "tinyproxy is not running"
        return 0
    fi

    local pid
    pid=$(get_pid)
    log_info "Sending TERM signal to PID ${pid}..."

    kill "${pid}" 2>/dev/null || true

    if wait_for_stop "${pid}"; then
        log_warn "Process didn't stop gracefully, sending KILL signal..."
        kill -9 "${pid}" 2>/dev/null || true

        local elapsed=0
        while ps -p "${pid}" > /dev/null 2>&1 && [[ ${elapsed} -lt ${TINYPROXY_STOP_TIMEOUT} ]]; do
            sleep 0.5
            elapsed=$((elapsed + 1))
        done
    fi

    rm -f "${PID_FILE}"
    log_success "tinyproxy stopped"
}

restart_proxy() {
    log_info "Restarting tinyproxy daemon..."
    stop_proxy
    start_proxy
}

show_status() {
    log_info "Checking tinyproxy daemon status..."
    echo ""

    validate_required_env

    if get_pid > /dev/null 2>&1; then
        local pid
        pid=$(get_pid)
        log_success "tinyproxy is RUNNING (PID: ${pid})"

        if lsof -iTCP:"${TINYPROXY_PORT}" -sTCP:LISTEN -P | grep -q "${pid}"; then
            log_success "Listening on port ${TINYPROXY_PORT}"
        else
            log_warn "Process running but not listening on port ${TINYPROXY_PORT}"
        fi

        if [[ -f "${LOG_FILE}" ]]; then
            echo ""
            log_info "Last log entries (last 20 lines):"
            echo "────────────────────────────────────────"
            tail -20 "${LOG_FILE}"
            echo "────────────────────────────────────────"
        fi
    else
        log_warn "tinyproxy is NOT RUNNING"

        if [[ -f "${LOG_FILE}" ]]; then
            echo ""
            log_info "Last run log entries (last 20 lines):"
            echo "────────────────────────────────────────"
            tail -20 "${LOG_FILE}"
            echo "────────────────────────────────────────"
        fi

        echo ""
        log_info "To start the proxy:"
        log_info "  ./tinyproxy-daemon.sh start"
    fi
}

case "${1:-}" in
    start)
        start_proxy
        ;;
    stop)
        stop_proxy
        ;;
    restart)
        restart_proxy
        ;;
    status)
        show_status
        ;;
    *)
        echo "Usage: $0 {start|stop|restart|status}"
        echo ""
        echo "Commands:"
        echo "  start    Start tinyproxy daemon"
        echo "  stop     Stop tinyproxy daemon"
        echo "  restart  Restart tinyproxy daemon"
        echo "  status   Show daemon status and recent logs"
        echo ""
        echo "Required environment variables:"
        echo "  TINYPROXY_UPSTREAM_HOST       Upstream proxy hostname (e.g., edge.surepath.ai)"
        echo "  TINYPROXY_UPSTREAM_PORT       Upstream proxy port (e.g., 8080)"
        echo "  TINYPROXY_PORT                Listen port (e.g., 3128)"
        echo "  TINYPROXY_READINESS_TIMEOUT   Startup readiness timeout in seconds (e.g., 10)"
        echo "  TINYPROXY_STOP_TIMEOUT        Graceful stop timeout in seconds (e.g., 10)"
        exit 1
        ;;
esac
