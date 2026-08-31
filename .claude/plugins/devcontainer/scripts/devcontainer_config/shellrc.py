"""Renders the `devsecret export-list` shell-startup block (spec Section 11).

`configure_shell_env` in `.devcontainer/.devcontainer.postcreate.sh` already
appends `source shell.env` to the container user's bash startup file and
writes the zsh environment file; that step wires identity and configuration
(spec Section 5.2, behavior change B5). This module renders the second block
that sits beside it: the one that reaches every secret `devsecret set
--exported` marked, without ever writing a value to a file (spec Section
5.4, goal G4).

Section 3.5 requires the block to be Python-rendered rather than a heredoc
hand-written into the 500-line provisioning script, so it can be unit
tested. `render` is the one function that produces the block text; the
per-shell difference (the comment naming which shell the block belongs to)
is a single parameter substituted into one shared template, not two copies
of the same text (AC-FUNC-009).

The block itself has three load-bearing safety properties, all enforced by
its literal text rather than by any runtime check this module could bypass:
it exports through the shell builtin so no new process ever carries a
secret value in its argv; it redirects only to `/dev/null` and stream
duplicates, never to a file, so no value ever reaches a filesystem; and on a
`devsecret export-list` failure, a `devsecret get` failure, or `devsecret`
itself being absent from `PATH`, it prints the error and a remedy on stderr,
exports nothing for that name, and does not abort the shell -- it never
substitutes an empty or a default value for a secret it could not fetch.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

# The five exit codes `devcontainer_config.cli` defines for `devsecret`
# (spec Section 4.3) are that module's own contract, not this one's; this
# renderer only ever needs "it worked" or "it did not", so it defines its
# own two-value subset rather than importing an unrelated module's larger
# vocabulary for a distinction it does not make.
EXIT_SUCCESS = 0
EXIT_USAGE_ERROR = 2

BASH_SHELL = "bash"
ZSH_SHELL = "zsh"

# Ordered so `_unsupported_shell_message` lists the supported shells in a
# stable, predictable order rather than whatever order a set would iterate.
SUPPORTED_SHELLS: tuple[str, ...] = (BASH_SHELL, ZSH_SHELL)

# Present once per rendered block, at the top. A caller that wants to avoid
# appending the block twice into the same startup file greps for this
# string before appending; `render` returning byte-identical text for the
# same shell on every call (AC-FUNC-006) is what makes that grep reliable.
MARKER = "# devsecret-export-list-startup-block (spec Section 11)"

_SHELL_TOKEN = "__DEVSECRET_SHELLRC_SHELL__"

# One shared template, per-shell text confined to a single substitution of
# `_SHELL_TOKEN` (AC-FUNC-009), applied with plain `str.replace` in `render`
# (the block is full of `${...}` shell parameter expansions, and formatting
# them with `str.format` would require escaping every brace instead of
# naming the one thing that actually varies). `MARKER` is interpolated once
# here, at module load, through an f-string; the double braces below are
# literal `${` / `}` in the rendered shell text, not further substitution.
_BLOCK_TEMPLATE = f"""\
{MARKER}
# Exports every devsecret --exported secret for __DEVSECRET_SHELLRC_SHELL__ (spec Section 11).
# Uses the export builtin, not a subprocess, so no value ever reaches this
# shell's process table. Redirects only to /dev/null and stream duplicates,
# never to a file: no secret value this block handles is ever written to
# disk. A fetch failure -- including devsecret itself being absent from
# PATH -- prints an error and a remedy on stderr, exports nothing for that
# name, never substitutes an empty or default value, and does not abort the
# shell: a missing credential fails only where it is used.
if command -v devsecret > /dev/null 2>&1; then
  if __devsecret_names="$(devsecret export-list)"; then
    while IFS= read -r __devsecret_name; do
      [ -n "${{__devsecret_name}}" ] || continue
      if __devsecret_value="$(devsecret get "${{__devsecret_name}}")"; then
        export "${{__devsecret_name}}=${{__devsecret_value}}"
      else
        echo "ERROR: devsecret get ${{__devsecret_name}} failed" >&2
        echo "remedy: run 'devsecret get ${{__devsecret_name}}' to see the error, then retry" >&2
      fi
      unset __devsecret_value
    done <<< "${{__devsecret_names}}"
  else
    echo "ERROR: devsecret export-list failed" >&2
    echo "remedy: run 'devsecret export-list' to see the underlying error" >&2
  fi
  unset __devsecret_names __devsecret_name
else
  echo "ERROR: devsecret is not on PATH" >&2
  echo "remedy: install the devsecret console script (spec Section 4.3) and open a new shell" >&2
fi
"""


class UnsupportedShellError(ValueError):
    """`render` was asked for a shell outside `SUPPORTED_SHELLS`.

    Named explicitly, rather than letting a bare `ValueError` or `KeyError`
    propagate, so the message can name both what was asked and what is
    supported: an operator editing `.devcontainer.postcreate.sh` for a shell
    this module does not yet render for gets an actionable answer, not a
    traceback with no next step.
    """


def _unsupported_shell_message(shell: str) -> str:
    supported = ", ".join(SUPPORTED_SHELLS)
    return (
        f"ERROR: unsupported shell {shell!r}\n"
        f"devcontainer_config.shellrc only renders a startup block for: {supported}.\n"
        "Pass one of these shell names."
    )


def render(shell: str) -> str:
    """The devsecret export-list startup block text for `shell` (AC-FUNC-001).

    Deterministic: the same `shell` always renders the same text, which is
    what lets a caller detect a prior application by searching for `MARKER`
    (AC-FUNC-006) instead of re-deriving idempotence from scratch.

    Raises:
        UnsupportedShellError: if `shell` is not in `SUPPORTED_SHELLS`.
    """
    if shell not in SUPPORTED_SHELLS:
        raise UnsupportedShellError(_unsupported_shell_message(shell))
    return _BLOCK_TEMPLATE.replace(_SHELL_TOKEN, shell)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="devcontainer_config.shellrc",
        description="Render the devsecret export-list shell-startup block (spec Section 11).",
    )
    parser.add_argument(
        "shell",
        metavar="SHELL",
        help=f"The shell to render the block for. One of: {', '.join(SUPPORTED_SHELLS)}.",
    )
    return parser


def main(argv: Sequence[str]) -> int:
    """Parse `argv`, print the rendered block for the requested shell, and return an exit code.

    `.devcontainer.postcreate.sh`'s `configure_shell_env` invokes this
    through `python3 -m devcontainer_config.shellrc <shell>` and treats any
    non-zero exit as fatal through `exit_with_error` (AC-FUNC-008): a
    container whose shells silently lack their exported secrets is worse
    than a container that failed to create.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        block = render(args.shell)
    except UnsupportedShellError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_USAGE_ERROR
    print(block)
    return EXIT_SUCCESS


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
