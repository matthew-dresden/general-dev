#!/usr/bin/env bash

set -euo pipefail
script="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/container.sh"
missing=0

while read -r command function; do
  if ! grep -q "^${function}()" "$script"; then
    printf '\033[0;31m[ERROR]\033[0m container.sh dispatches "%s" to %s(), which is not defined\n' \
      "$command" "$function" >&2
    missing=1
  fi
done < <(sed -nE 's/^[[:space:]]*([a-z-]+)\)[[:space:]]+(rdc_require_docker[[:space:]]*&&[[:space:]]*)?(rdc_[a-z_]+)[[:space:]]*;;.*/\1 \3/p' "$script")

[ "$missing" -eq 0 ] || exit 1
printf '  every dispatched command resolves\n'
