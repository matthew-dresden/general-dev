#!/usr/bin/env bash
# Wrapper kept so the path documented in this directory's README keeps working.
# The implementation is shared by every host family, see ../tinyproxy-daemon.sh.
exec "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/tinyproxy-daemon.sh" "$@"
