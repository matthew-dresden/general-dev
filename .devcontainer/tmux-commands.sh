# shellcheck shell=bash
# tmux commands for this devcontainer. Sourced into interactive bash and zsh
# by postcreate, never executed, so it sets no shell options of its own.
#
# Terminals here open inside a shared tmux session so work survives VS Code
# closing. These wrap the tmux invocations worth remembering, under a common
# tm prefix: type "tm" and press Tab to list them. Every command accepts
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
# Both tmhelp and each command's --help read from here.
__tm_registry() {
  cat <<'EOF'
tmopen|[name]|Go to a session, creating it if missing (default: main).
tmlist||List sessions.
tmwindows||List windows in the current session.
tmrename|<name>|Rename the current window.
tmdetach||Leave the session; everything keeps running.
tmkill|<name>|Kill one named session.
tmkillall||Kill every session.
tmhelp||Show all commands and key bindings.
EOF
}

# Longer notes, shown only by "<command> --help".
__tm_detail() {
  case "$1" in
    tmopen)
      printf '%s\n' \
        'Terminals in this container already open inside the shared session, so' \
        'you rarely need this. Use it after tmdetach, or to start a second,' \
        'isolated session:' \
        '' \
        '    tmopen build' \
        '' \
        'It never destroys anything: an existing session is joined with' \
        'everything still running, a missing one is created.' \
        '' \
        'Works from inside a session too, where it moves this terminal to the' \
        'named session rather than nesting one inside the other. Ctrl+b d, or' \
        'tmopen main, brings you back.'
      ;;
    tmlist)
      printf '%s\n' \
        'Shows every session on this container, its window count, and which one' \
        'is currently attached. Use it when you have lost track of what is' \
        'running after a reconnect.'
      ;;
    tmwindows)
      printf '%s\n' \
        'Windows are the tabs inside a session. The active one is marked with' \
        '*. Name them with tmrename so this list stays readable.'
      ;;
    tmrename)
      printf '%s\n' \
        'Renames the window you are in, so tmlist, tmwindows and Ctrl+b w show' \
        'something meaningful instead of "zsh":' \
        '' \
        '    tmrename claude'
      ;;
    tmdetach)
      printf '%s\n' \
        'Leaves the session running and returns you to a plain shell. Nothing is' \
        'stopped. Equivalent to Ctrl+b d. Rejoin with tmopen.' \
        '' \
        'Closing the VS Code window has the same effect on the session, so you' \
        'do not need to detach before disconnecting.'
      ;;
    tmkill)
      printf '%s\n' \
        'Destroys a session and every process inside it. Requires an explicit' \
        'name so it can never take out the session you are sitting in by' \
        'accident:' \
        '' \
        '    tmkill build'
      ;;
    tmkillall)
      printf '%s\n' \
        'Stops the tmux server and every session on this container. Anything' \
        'running in them is killed. Rebuilding the container has the same' \
        'effect, since the tmux server does not outlive it.'
      ;;
    tmhelp)
      printf '%s\n' \
        'Lists every tm command with its summary, then the tmux key bindings.' \
        'For detail on one command, ask it directly: tmopen --help'
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

tmopen() {
  if [ "$1" = "--help" ]; then __tm_help_for tmopen; return 0; fi
  set -- "${1:-$TM_SESSION}"
  if [ -z "${TMUX:-}" ]; then
    tmux new-session -A -s "$1" "$TM_SHELL"
    return $?
  fi
  # Already inside a session. tmux refuses to attach one from within another
  # ("sessions should be nested with care"), and every terminal in this
  # container starts inside the shared session, so plain attach could never
  # work from where you actually type. Create it detached if it is missing,
  # then move this client to it.
  tmux has-session -t "$1" 2> /dev/null || tmux new-session -d -s "$1" "$TM_SHELL"
  tmux switch-client -t "$1"
}

tmlist() {
  if [ "$1" = "--help" ]; then __tm_help_for tmlist; return 0; fi
  tmux list-sessions
}

tmwindows() {
  if [ "$1" = "--help" ]; then __tm_help_for tmwindows; return 0; fi
  tmux list-windows
}

tmrename() {
  if [ "$1" = "--help" ]; then __tm_help_for tmrename; return 0; fi
  if [ -z "$1" ]; then
    printf 'tmrename: a name is required. See: tmrename --help\n' >&2
    return 1
  fi
  tmux rename-window "$1"
}

tmdetach() {
  if [ "$1" = "--help" ]; then __tm_help_for tmdetach; return 0; fi
  if [ -z "${TMUX:-}" ]; then
    printf 'tmdetach: not inside a tmux session, nothing to detach from.\n' >&2
    return 1
  fi
  tmux detach-client
}

tmkill() {
  if [ "$1" = "--help" ]; then __tm_help_for tmkill; return 0; fi
  if [ -z "$1" ]; then
    printf 'tmkill: a session name is required. See: tmkill --help\n' >&2
    return 1
  fi
  tmux kill-session -t "$1"
}

tmkillall() {
  if [ "$1" = "--help" ]; then __tm_help_for tmkillall; return 0; fi
  tmux kill-server
}

tmhelp() {
  if [ "$1" = "--help" ]; then __tm_help_for tmhelp; return 0; fi
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

KEYS                                  press Ctrl+b, release, then the key

  A session holds windows, a window holds panes. A window is a whole screen,
  like a tab: only one is visible at a time. Panes are splits inside the
  window you are looking at, all visible together.

  WINDOWS
  Ctrl+b  w      pick a window from a list          <- start here
  Ctrl+b  c      new window
  Ctrl+b  ,      rename current window
  Ctrl+b  n / p  next / previous window
  Ctrl+b  0-9    jump to window by number
  Ctrl+b  &      close window

  PANES                               splits inside the current window
  Ctrl+b  %      split left | right
  Ctrl+b  "      split top / bottom
  Ctrl+b  o      cycle between panes
  Ctrl+b  x      close pane

  SESSION
  Ctrl+b  d      detach, everything keeps running
  Ctrl+b  [      scroll back                        (q to exit)
  Ctrl+b  ?      full tmux key list

MOUSE
  wheel          scroll this pane's history         (q to exit)
  click          focus a pane, or a window on the status bar
  drag a border  resize a pane
  Shift + drag   the terminal's own selection, which copies to the clipboard

EOF
}
