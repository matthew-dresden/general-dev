# shellcheck shell=bash
# tmux commands for this devcontainer. Sourced into interactive bash and zsh
# by postcreate, never executed, so it sets no shell options of its own.
#
# Terminals here open inside a shared tmux session so work survives VS Code
# closing. These wrap the tmux invocations worth remembering, under a common
# tm- prefix: type "tm-" and press Tab to list them. Every command accepts
# --help.
#
# Portability: must parse under both bash and zsh. No associative arrays, no
# [[ ]], no process substitution.

# Shell started inside new sessions. The vscode account's login shell is bash,
# so this is set explicitly to match the VS Code terminal profile.
: "${TM_SHELL:=zsh}"
# Session used when no name is given.
: "${TM_SESSION:=main}"

# Single source of truth for command documentation: name|args|summary.
# Both tm-help and each command's --help read from here.
__tm_registry() {
  cat <<'EOF'
tm-open|[name]|Attach to a session, creating it if missing (default: main).
tm-list||List sessions.
tm-windows||List windows in the current session.
tm-rename|<name>|Rename the current window.
tm-detach||Leave the session; everything keeps running.
tm-kill|<name>|Kill one named session.
tm-kill-all||Kill every session.
tm-help||Show all commands and key bindings.
EOF
}

# Longer notes, shown only by "<command> --help".
__tm_detail() {
  case "$1" in
    tm-open)
      printf '%s\n' \
        'Terminals in this container already open inside the shared session, so' \
        'you rarely need this. Use it after tm-detach, or to start a second,' \
        'isolated session:' \
        '' \
        '    tm-open build' \
        '' \
        'Attaching never destroys anything: if the session exists you join it' \
        'with everything still running, otherwise it is created.'
      ;;
    tm-list)
      printf '%s\n' \
        'Shows every session on this container, its window count, and which one' \
        'is currently attached. Use it when you have lost track of what is' \
        'running after a reconnect.'
      ;;
    tm-windows)
      printf '%s\n' \
        'Windows are the tabs inside a session. The active one is marked with' \
        '*. Name them with tm-rename so this list stays readable.'
      ;;
    tm-rename)
      printf '%s\n' \
        'Renames the window you are in, so tm-list, tm-windows and Ctrl+B w show' \
        'something meaningful instead of "zsh":' \
        '' \
        '    tm-rename claude'
      ;;
    tm-detach)
      printf '%s\n' \
        'Leaves the session running and returns you to a plain shell. Nothing is' \
        'stopped. Equivalent to Ctrl+B d. Rejoin with tm-open.' \
        '' \
        'Closing the VS Code window has the same effect on the session, so you' \
        'do not need to detach before disconnecting.'
      ;;
    tm-kill)
      printf '%s\n' \
        'Destroys a session and every process inside it. Requires an explicit' \
        'name so it can never take out the session you are sitting in by' \
        'accident:' \
        '' \
        '    tm-kill build'
      ;;
    tm-kill-all)
      printf '%s\n' \
        'Stops the tmux server and every session on this container. Anything' \
        'running in them is killed. Rebuilding the container has the same' \
        'effect, since the tmux server does not outlive it.'
      ;;
    tm-help)
      printf '%s\n' \
        'Lists every tm- command with its summary, then the tmux key bindings.' \
        'For detail on one command, ask it directly: tm-open --help'
      ;;
  esac
}

# Usage block for a single command, assembled from the registry plus detail.
__tm_help_for() {
  __tm_registry | while IFS='|' read -r __tm_name __tm_args __tm_summary; do
    if [ "$__tm_name" = "$1" ]; then
      printf '\n\033[1m%s %s\033[0m\n\n  %s\n' "$__tm_name" "$__tm_args" "$__tm_summary"
    fi
  done
  __tm_d="$(__tm_detail "$1")"
  if [ -n "$__tm_d" ]; then
    printf '\n%s\n' "$__tm_d" | sed 's/^/  /'
  fi
  unset __tm_d
  printf '\n'
}

tm-open() {
  if [ "$1" = "--help" ]; then __tm_help_for tm-open; return 0; fi
  tmux new-session -A -s "${1:-$TM_SESSION}" "$TM_SHELL"
}

tm-list() {
  if [ "$1" = "--help" ]; then __tm_help_for tm-list; return 0; fi
  tmux list-sessions
}

tm-windows() {
  if [ "$1" = "--help" ]; then __tm_help_for tm-windows; return 0; fi
  tmux list-windows
}

tm-rename() {
  if [ "$1" = "--help" ]; then __tm_help_for tm-rename; return 0; fi
  if [ -z "$1" ]; then
    printf 'tm-rename: a name is required. See: tm-rename --help\n' >&2
    return 1
  fi
  tmux rename-window "$1"
}

tm-detach() {
  if [ "$1" = "--help" ]; then __tm_help_for tm-detach; return 0; fi
  if [ -z "${TMUX:-}" ]; then
    printf 'tm-detach: not inside a tmux session, nothing to detach from.\n' >&2
    return 1
  fi
  tmux detach-client
}

tm-kill() {
  if [ "$1" = "--help" ]; then __tm_help_for tm-kill; return 0; fi
  if [ -z "$1" ]; then
    printf 'tm-kill: a session name is required. See: tm-kill --help\n' >&2
    return 1
  fi
  tmux kill-session -t "$1"
}

tm-kill-all() {
  if [ "$1" = "--help" ]; then __tm_help_for tm-kill-all; return 0; fi
  tmux kill-server
}

tm-help() {
  if [ "$1" = "--help" ]; then __tm_help_for tm-help; return 0; fi
  cat <<'EOF'

tmux, persistent shells in this container

  Terminals open inside a shared session. Whatever runs there, such as a
  Claude session or a long build, survives closing VS Code, sleeping the
  laptop, and losing the tunnel. It does NOT survive the container being
  stopped or rebuilt.

COMMANDS                              every command also accepts --help
EOF
  __tm_registry | while IFS='|' read -r __tm_name __tm_args __tm_summary; do
    printf '  \033[1;36m%-20s\033[0m %s\n' "$__tm_name $__tm_args" "$__tm_summary"
  done
  cat <<'EOF'

KEYS                                  press Ctrl+B, release, then the key
  Ctrl+B  w      pick a window from a list          <- start here
  Ctrl+B  c      new window
  Ctrl+B  ,      rename current window
  Ctrl+B  n / p  next / previous window
  Ctrl+B  0-9    jump to window by number
  Ctrl+B  %      split left | right
  Ctrl+B  "      split top / bottom
  Ctrl+B  o      cycle between panes
  Ctrl+B  x      close pane
  Ctrl+B  &      close window
  Ctrl+B  d      detach, everything keeps running
  Ctrl+B  [      scroll back                        (q to exit)
  Ctrl+B  ?      full tmux key list

EOF
}
