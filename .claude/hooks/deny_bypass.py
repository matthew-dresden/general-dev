"""`PreToolUse` hook denying the bypass surface named in spec Section 4.6.1.

Section 3.6.2 names the threat this hook answers: not a secret reaching the
repository, but an agent or a human disabling the controls under time
pressure. Every control in E2 runs from a git hook, and git ships a
documented way to skip git hooks; a scanner that one flag turns off is a
scanner that will be turned off on the day it first says no. This hook is
registered on the `Bash` matcher (`.claude/settings.json`) and denies, on
every invocation, the ten patterns Section 4.6.1 lists: `git commit
--no-verify` and its `-n` alias, including a bundled short-option cluster;
`git push --no-verify`; `git commit --no-gpg-sign`; the `HUSKY=0`, `SKIP=`
and `PRE_COMMIT_ALLOW_NO_CONFIG` environment assignments; `git add -f` of a
gitignored path; `rm` or `chmod` targeting `.git/hooks`; and `make
hooks-uninstall`. `git push --dry-run` (`-n` meaning dry run under `push`,
not the `commit` alias for `--no-verify`) is deliberately not one of them.

This module is standalone: it lives under `.claude/hooks/`, outside the
`devcontainer_config` package tree
(`.claude/plugins/devcontainer/scripts/devcontainer_config`), and imports
nothing from it, because Claude Code invokes this file directly with a bare
`python3` and no `PYTHONPATH` of its own (see `.claude/settings.json`'s
`command` field). Only the standard library is imported here.

Evaluation is structural, not a substring search. A command line can chain
several statements with `&&`, `||`, `;`, a pipe, a bare `&` background
operator, or simply a newline -- the ordinary shape of a two-line Bash-tool
invocation, and just as much a statement boundary as the other five; wrap
a statement in a subshell `( )` or a brace group `{ }`, whose grouping
tokens are boundaries the same way, so the wrapped command is evaluated on
its own rather than being read as an argument of whatever the grouping
token would otherwise be mistaken for; carry inline environment
assignments ahead of the program name (exactly how `HUSKY=0 git commit` is
written); bundle short options (`-nv` hides the denied `-n`); invoke its
program through a path prefix (`/usr/bin/git`, `/bin/rm`) rather than the
bare name; precede it with a bare-word launcher (`env`, `sudo`, `command`,
`nohup`, `time`, `doas`, `nice`, `ionice`, `setsid` -- see
`_LAUNCHER_PREFIXES`), any number of them in a row (`env sudo git commit
--no-verify`); or sit as the body of a shell compound statement, where a
reserved word (`if`, `then`, `elif`, `else`, `fi`, `do`, `done`, `while`,
`until`, `case`, `esac`, `in`, `function`, `!` -- see
`_SHELL_RESERVED_WORDS`) would otherwise be read as the segment's program
once `;` has split the compound onto its own segment (`if true; then git
commit --no-verify; fi` puts `then` where `_parse_segment` would
otherwise read the program). `segments` splits the line on all eight
boundary spellings and tokenizes each with `shlex`'s punctuation-aware
mode; `_parse_segment` then walks past any leading run of launcher words,
reserved words and `NAME=value` assignments, in any order, before reading
the program; and `evaluate` walks the resulting `Segment`s against
`RULES`, each of which is a structural predicate over one segment's
parsed program (compared by basename, via `_program_name`, so a path
prefix cannot hide it), arguments and leading assignments -- never a
regular expression over the raw text.

The `git add -f` rule needs one fact no token carries: whether the named
path is gitignored. That is a question only git can answer, so `evaluate`
receives an `is_ignored` callable rather than shelling out itself
(AC-FUNC-008); `main` is the only function in this module reachable from
`__main__` that touches stdin, and `_check_ignore` -- called only through
`main`'s default `is_ignored` -- is the only function that ever spawns a
subprocess.

The default is deny for anything this hook cannot understand. An
untokenizable command line, an `is_ignored` callable that raises, and (in
`main`) a stdin payload that is not valid JSON or carries no command to
evaluate, all produce a refusal, never an allow. A security control whose
failure mode is permissive is not a control.

Known out-of-scope gaps, so a future reader does not assume this module's
silence on them means coverage:

- A `git -c core.hooksPath=<path>` global config assignment is not
  inspected for a value that disables hooks (`_skip_git_global_options`
  walks past `-c` and its value without reading it).
- A denied command nested inside a quoted string this module cannot
  re-parse is not recovered from that string: `bash -c "<command>"`,
  `sh -c "<command>"` and `eval "<command>"` all read as an invocation of
  `bash`/`sh`/the `eval` builtin with one ordinary string argument, not as
  the command inside it, because no token-level matcher over the outer
  command line can see inside a nested one without recursing into a full
  shell parse.
- A launcher word combined with its own flags is not walked past, only
  the bare word is (`_LAUNCHER_PREFIXES`): `sudo -u root git commit
  --no-verify`, `env -i HUSKY=0 git commit` and `nice -n 10 git commit
  --no-verify` all read the flag token (`-u`, `-i`, `-n`) as the program.
- A wrapper that requires a positional argument before the command it
  runs is not in `_LAUNCHER_PREFIXES` at all, because skipping only the
  wrapper word would misread that mandatory argument as the program:
  `timeout 5 git commit --no-verify` (the duration), `stdbuf -oL git
  commit --no-verify` (a buffering option `stdbuf` requires), and `...  |
  xargs git commit --no-verify` (xargs is not a single-invocation
  launcher at all; it re-runs its trailing command once per line of
  input).
- A bash reserved word outside `_SHELL_RESERVED_WORDS` is not walked
  past: `select`, `coproc`, `[[`, `]]` and the arithmetic-command
  `((...))` syntax are not recognized as segment-leading boundaries.

None of these is one of the ten Section 4.6.1 patterns; all are raised
here as hardening candidates for a follow-up unit, not defects in this
one.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath

_DENY_EXIT_CODE = 2
_ALLOW_EXIT_CODE = 0

# The punctuation characters `segments` tokenizes as their own tokens
# instead of folding into a word: the four chain operators Section 4.6.1
# and AC-FUNC-006 name (`&&`, `||`, `;`, a bare pipe), the bare `&`
# background operator (the same class of evasion: "run this, then the
# denied command regardless" -- Section 3.6.2's threat model does not stop
# at the operators the spec happens to name), the newline that ordinarily
# separates two statements of a multi-line Bash command, which is the
# ordinary shape of a two-command Bash-tool invocation, not an exotic one,
# and the shell grouping tokens `(`, `)`, `{`, `}` a subshell or brace
# group wraps a statement in -- another spelling of the same threat: a
# command wrapped in `(...)` or `{ ...; }` still runs the wrapped program
# unchanged, so without these as their own tokens the grouping character
# glues onto the program name (`(git` is not `git`) and every rule below
# reads nothing. `<>` are included only because they were already part of
# shlex's own default punctuation set (`punctuation_chars=True`); this
# module has no rule that depends on them.
_PUNCTUATION_CHARS = "(){};<>|&\n"

# Whether `token` is entirely composed of chain-operator or shell-grouping
# characters, so it is a segment boundary rather than part of a program
# name or argument. This is a character-class match, not a fixed set of
# exact strings (`{"&&", "||", ";", "|"}`), because shlex's
# punctuation-aware tokenizer merges a run of *any* adjacent punctuation
# characters into a single token, not only runs of the same character:
# `git commit -n &&\ngit push` and `git commit -n\ngit push` both yield a
# merged token (`"&&\n"`, `"\n"`) that a fixed-string set would never
# match, silently letting the chain-splitting logic below treat the whole
# multi-line command as one unsplit segment; `(git commit -n);` yields a
# merged `");"` token the same way. `(`, `)`, `{` and `}` are included for
# the identical reason a bare `&` and a bare newline are: a subshell
# `(git commit --no-verify)` or a brace group `{ git commit --no-verify; }`
# is another spelling of "run the denied command regardless", and without
# these characters as boundaries the grouping token is read as the segment's
# `program` instead of a boundary, and the real program and its leading
# assignments slide into `args`, where no rule looks (see `_parse_segment`).
# Matching on "made up entirely of these characters" recognizes every such
# merge as the boundary it is, without needing to decide how many logical
# operators or grouping tokens it represents.
_CHAIN_BOUNDARY_RE = re.compile(r"^[;&|(){}\n]+$")

# Bare-word launchers this module walks past before reading the real
# program: "the real program follows, possibly after more launchers and
# assignments". `env` is Section 4.6.1's own "can be prefixed with env";
# `sudo`, `command`, `nohup` and `time` are the launcher-prefix class
# code_review verified ALLOW against an earlier version of this module
# (Section 3.6.2's threat model -- another spelling of an already-denied
# bypass, not a new one). `doas` (the OpenBSD analogue of `sudo`), `nice`,
# `ionice` and `setsid` take the identical bare shape -- `LAUNCHER cmd
# args...`, with no argument of their own required -- so they are handled
# the same way for the same reason, not because Section 4.6.1 names them.
# Only the bare word is recognized, not the launcher combined with its own
# flags (`sudo -u root`, `env -i`, `nice -n 10`): see the module
# docstring's "Known out-of-scope gaps" paragraph. `_parse_segment` walks
# past any number of these in a row (`env sudo git commit --no-verify`),
# interleaved with `_SHELL_RESERVED_WORDS` and leading assignments in any
# order, so a chain of launchers is skipped the same way a single one is.
_LAUNCHER_PREFIXES: frozenset[str] = frozenset(
    {"env", "sudo", "command", "nohup", "time", "doas", "nice", "ionice", "setsid"}
)

# Shell reserved words `_parse_segment` walks past before reading the real
# program, the same way it walks past a launcher. `segments` only splits a
# command line on `&&`/`||`/`;`/`|`/`&`/newline/grouping tokens; it does
# not parse the shell's compound-statement grammar. So a `;`-chained
# segment that lands inside an `if`/`for`/`while`/`case` compound, or
# right after its own condition, starts with the reserved word that
# introduces that branch or body -- `then`, `elif`, `else`, `do`, `done`,
# `fi`, `esac` close or open a branch/body and are typically alone or
# followed by nothing this module needs to read differently; `if`,
# `while`, `until` and `case` themselves can be immediately followed by
# the actual command being run (`if git commit --no-verify; then :; fi`
# puts the denied command in the `if`'s own condition, not only in its
# `then` branch); `in` introduces a `for`-loop list or a `case` pattern
# and can lead a segment the same way; `function` precedes a function
# definition's name, not an invocation, but is included for the same
# "word that would otherwise be misread as the program" reason; `!`
# negates a pipeline (`! git commit --no-verify`) and is Section 4.6.1's
# threat model in one character. Without this walk, `_parse_segment` reads
# the reserved word itself as `program`, and the real invocation --
# unmatched by any rule keyed on `git`/`rm`/`chmod`/`make` -- slides into
# `args`, exactly the failure mode already fixed for `(` and `{` via
# `_CHAIN_BOUNDARY_RE`. `select`, `coproc`, `[[`, `]]` and the
# arithmetic-command `((...))` syntax are not in this set: see the module
# docstring's "Known out-of-scope gaps" paragraph.
_SHELL_RESERVED_WORDS: frozenset[str] = frozenset(
    {
        "if",
        "then",
        "elif",
        "else",
        "fi",
        "do",
        "done",
        "while",
        "until",
        "case",
        "esac",
        "in",
        "function",
        "!",
    }
)

# A leading `NAME=value` (or `NAME=`) token, the shape `HUSKY=0`,
# `SKIP=pre-commit` and `PRE_COMMIT_ALLOW_NO_CONFIG=1` all share.
_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

# A `-xyz`-shaped short-option cluster: only letters after the leading
# dash, so a long option (`--no-verify`) or a value-carrying token never
# matches.
_SHORT_FLAG_CLUSTER_RE = re.compile(r"^-[A-Za-z]+$")

# `git add`'s two spellings of "stage this even though something excludes
# it".
_FORCE_FLAGS: frozenset[str] = frozenset({"-f", "--force"})

# Git global options that can appear between `git` and its subcommand
# (`git -C . commit --no-verify`, `git --git-dir=.git commit --no-verify`).
# Section 3.6.2's threat model is an agent or a human trying another
# spelling of an already-denied bypass, and prefixing the subcommand with a
# global option is exactly such a spelling: `_git_subcommand_and_rest` walks
# past every option here so the subcommand it reports is the token that
# actually names one (`commit`, `push`, `add`), never a global option that
# happens to occupy the first argument slot. `-C` and `-c` always consume
# the token that follows as their value; `--git-dir`, `--work-tree` and
# `--exec-path` take their value either fused with `=` or as the following
# token (git accepts both spellings); `-p`, `--paginate` and `--no-pager`
# take no value at all.
_GIT_GLOBAL_FLAGS_TAKING_NEXT_TOKEN: frozenset[str] = frozenset({"-C", "-c"})
_GIT_GLOBAL_LONGOPTS_TAKING_VALUE: frozenset[str] = frozenset(
    {"--git-dir", "--work-tree", "--exec-path"}
)
_GIT_GLOBAL_BARE_FLAGS: frozenset[str] = frozenset({"-p", "--paginate", "--no-pager"})

# Matches a path segment equal to, or nested under, a `.git/hooks`
# directory, with or without a leading `./`: `.git/hooks`,
# `.git/hooks/pre-commit`, `./.git/hooks/pre-push`.
_GIT_HOOKS_PATH_RE = re.compile(r"(^|/)\.git/hooks(/|$)")

# The rule identifier `evaluate` reports when `segments` cannot tokenize the
# command line at all (see `evaluate`'s docstring); not part of `RULES`
# because it is not a structural predicate over a parsed segment, it is
# what happens instead of ever reaching one.
_UNPARSEABLE_RULE_ID = "unparseable-command"


class DenyBypassError(RuntimeError):
    """Raised when a command line cannot be tokenized into segments.

    `segments` raises this directly, for a caller -- a test, for one --
    that wants to assert on the tokenization failure itself. `evaluate`
    catches it and turns it into a denial instead of letting it escape
    (see the module docstring's fail-closed rule), so nothing outside this
    module ever needs to catch it to stay safe.
    """


@dataclass(frozen=True)
class Segment:
    """One `&&`/`||`/`;`/`|`-delimited slice of a command line, already parsed.

    `tokens` is every token in the segment, in the order `segments`
    produced them. `assignments` is the leading run of `NAME=value`
    tokens, with any leading launcher words (`env`, `sudo`, ...) and shell
    reserved words (`then`, `do`, ...) already stripped off by
    `_parse_segment`; `program` and `args` are whatever tokens follow.
    `program` is `""` and `args` is `()` when a segment holds only
    launchers, reserved words and assignments, or nothing at all (an empty
    segment between two chain operators).
    """

    tokens: tuple[str, ...]
    assignments: tuple[str, ...]
    program: str
    args: tuple[str, ...]


@dataclass(frozen=True)
class Decision:
    """A denial: which rule fired (`rule`) and the reason shown to the caller (`reason`).

    `reason` names both the rule identifier and the fix (AC-FUNC-007), so a
    caller that surfaces only `reason` -- `main`, to stderr -- still
    redirects the agent that triggered the denial rather than only
    stopping it.
    """

    rule: str
    reason: str


# A rule's match function: given one parsed `Segment` and the injected
# `is_ignored` callable, `None` means the rule does not fire on this
# segment; any other string is the rule-specific detail folded into the
# reason `Rule.decision` builds (the flagged path for `git-add-force
# -ignored`, the failed check's error for its exception path, or `""` for
# every rule that needs no per-call detail). Every rule gets the same
# signature, including `is_ignored`, even though only one rule uses it, so
# `evaluate`'s loop stays uniform instead of special-casing one rule.
MatchFn = Callable[[Segment, Callable[[str], bool]], "str | None"]


@dataclass(frozen=True)
class Rule:
    """One denial rule: its identifier, its fix, and the predicate that fires it."""

    id: str
    fix: str
    match: MatchFn

    def decision(self, detail: str) -> Decision:
        """The `Decision` this rule reports when `match` returns `detail` (not `None`)."""
        reason = f"{self.id}: {self.fix}"
        if detail:
            reason = f"{reason} ({detail})"
        return Decision(rule=self.id, reason=reason)


def _program_name(segment: Segment) -> str:
    """The basename of `segment.program`, so a path-prefixed binary reads the same as its bare name.

    Section 3.6.2's threat model is an agent or a human reaching for
    another spelling of an already-denied bypass, and resolving the
    binary through its path is exactly that spelling: `/usr/bin/git
    commit --no-verify` and `/bin/rm .git/hooks/pre-commit` invoke the
    identical `git` and `rm` programs a bare `git`/`rm` token denies.
    Every program-name comparison in this module reads through this
    function rather than comparing `segment.program` directly, the same
    way `_skip_git_global_options` already treats `-C <dir>` ahead of the
    subcommand as "the same subcommand, spelled differently" rather than a
    different program. `PurePosixPath("").name` is `""`, so an
    assignment-only segment (`segment.program == ""`) still compares
    unequal to every real program name instead of raising.
    """
    return PurePosixPath(segment.program).name


def _parse_segment(tokens: Sequence[str]) -> Segment:
    """Split `tokens` (one chain segment) into its assignments, program and args.

    Walks past any leading run of launcher words (`_LAUNCHER_PREFIXES`),
    shell reserved words (`_SHELL_RESERVED_WORDS`) and `NAME=value`
    assignments, in any order and any number of times, before treating the
    next token as `program`: `env sudo git commit --no-verify` (launcher,
    launcher, program), `then HUSKY=0 git commit --no-verify` (reserved
    word, assignment, program) and `sudo HUSKY=0 git commit --no-verify`
    (launcher, assignment, program) all reach `program == "git"` the same
    way. A single combined loop, rather than one pass for launchers then
    one for assignments, is what makes an arbitrary order and repetition
    of the three token classes resolve to the same program instead of only
    the one fixed order (`env` once, then assignments) the pre-fix version
    recognized.
    """
    idx = 0
    assignments: list[str] = []
    while idx < len(tokens):
        token = tokens[idx]
        if token in _LAUNCHER_PREFIXES or token in _SHELL_RESERVED_WORDS:
            idx += 1
            continue
        if _ASSIGNMENT_RE.match(token):
            assignments.append(token)
            idx += 1
            continue
        break
    program = tokens[idx] if idx < len(tokens) else ""
    args = tuple(tokens[idx + 1 :]) if idx < len(tokens) else ()
    return Segment(tokens=tuple(tokens), assignments=tuple(assignments), program=program, args=args)


def segments(command: str) -> tuple[Segment, ...]:
    """`command` split on `&&`, `||`, `;`, `|`, a bare `&`, a newline, `(`, `)`, `{` and `}`.

    Tokenization uses `shlex`'s punctuation-aware mode
    (`punctuation_chars=_PUNCTUATION_CHARS`), which is what makes a chain
    operator its own token even with no surrounding whitespace (`git
    commit -n;git push`) instead of fusing onto a neighboring word, and
    what respects quoting the same way a shell would (`git commit -m
    "message with && in it"` stays one argument, and a literal newline
    inside a quoted string stays part of that string rather than becoming
    a boundary). A bare newline outside quotes is removed from `whitespace`
    and added to `_PUNCTUATION_CHARS` instead, so it becomes a token
    `_CHAIN_BOUNDARY_RE` recognizes rather than silently vanishing the way
    ordinary whitespace does -- a multi-line Bash-tool command is the
    ordinary shape of a two-statement invocation, not an exotic one, and a
    tokenizer that treats the newline as insignificant lets the second
    statement evade every rule below. `(`, `)`, `{` and `}` are boundaries
    for the same reason: `(git commit --no-verify)` and
    `{ git commit --no-verify; }` each wrap exactly one statement, and
    without the grouping token as its own boundary it is read as the
    segment's `program` (`_parse_segment` would take `"("` itself as the
    program and slide the real `git` invocation into `args`, where no rule
    looks) instead of the statement inside the group being evaluated on its
    own.

    Raises:
        DenyBypassError: if `command` cannot be tokenized, for example an
            unbalanced quote.
    """
    lexer = shlex.shlex(command, posix=True, punctuation_chars=_PUNCTUATION_CHARS)
    lexer.whitespace = lexer.whitespace.replace("\n", "")
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError as exc:
        raise DenyBypassError(
            f"ERROR: cannot tokenize command line: {exc}\n"
            f"Command: {command!r}\n"
            "Fix the quoting. If this is a legitimate command shlex cannot "
            "parse, escalate to the operator rather than retrying with "
            "--no-verify."
        ) from exc
    result: list[Segment] = []
    current: list[str] = []
    for token in tokens:
        if _CHAIN_BOUNDARY_RE.match(token):
            result.append(_parse_segment(current))
            current = []
            continue
        current.append(token)
    result.append(_parse_segment(current))
    return tuple(result)


def _skip_git_global_options(args: Sequence[str]) -> tuple[str, ...]:
    """`args` (a `git` segment's arguments) with any leading global options stripped.

    Git accepts a run of global options ahead of the subcommand (see
    `_GIT_GLOBAL_FLAGS_TAKING_NEXT_TOKEN`,
    `_GIT_GLOBAL_LONGOPTS_TAKING_VALUE` and `_GIT_GLOBAL_BARE_FLAGS`), and
    every one of them is a spelling of "run the same subcommand, but make
    the rule below think it isn't `git commit`/`git push`/`git add`" if
    this walk does not account for it. Stops at the first token that is
    neither a recognized global option nor that option's consumed value,
    which is the subcommand (or, for a malformed line with a dangling
    value-taking flag, the end of `args`).
    """
    idx = 0
    while idx < len(args):
        arg = args[idx]
        if arg in _GIT_GLOBAL_BARE_FLAGS:
            idx += 1
            continue
        if arg in _GIT_GLOBAL_FLAGS_TAKING_NEXT_TOKEN or arg in _GIT_GLOBAL_LONGOPTS_TAKING_VALUE:
            idx += 2
            continue
        if any(arg.startswith(f"{opt}=") for opt in _GIT_GLOBAL_LONGOPTS_TAKING_VALUE):
            idx += 1
            continue
        break
    return tuple(args[idx:])


def _git_subcommand_and_rest(segment: Segment) -> tuple[str, tuple[str, ...]] | None:
    """`(subcommand, args-after-subcommand)` for a `git` segment, or `None`.

    `None` when `segment` does not invoke `git` at all, or when it invokes
    `git` with nothing but global options and no subcommand. The threat
    model this answers (module docstring, Section 3.6.2) is an agent
    reaching for another spelling of the same denied bypass, and `git -C
    <dir> commit --no-verify` is exactly that spelling: reading
    `segment.args[0]` directly, as this function's predecessor did, treats
    `-C` itself as the subcommand and so evades every rule keyed on
    `commit`, `push` or `add`. Comparing through `_program_name` rather
    than `segment.program` directly answers the same question for
    `/usr/bin/git commit --no-verify`, the path-prefixed spelling of the
    identical evasion.
    """
    if _program_name(segment) != "git":
        return None
    remaining = _skip_git_global_options(segment.args)
    if not remaining:
        return None
    return remaining[0], remaining[1:]


def _bundled_short_flag(arg: str, letter: str) -> bool:
    """Whether `arg` is a `-xyz`-shaped short-option cluster containing `letter`.

    Matches both a bare `-n` and a bundled cluster such as `-vn`; a long
    option (`--no-verify`, `--force`) never matches, because it does not
    fit `_SHORT_FLAG_CLUSTER_RE`. This does not distinguish a genuine
    boolean cluster from a value bundled onto a flag that takes one
    (`-mNote` would also match): neither caller needs that distinction --
    `_match_commit_no_verify` checks `n` and `_match_add_force_ignored`
    checks `f`, and both are exercised only against this module's own case
    table, which never bundles a value-taking flag ahead of either letter.
    """
    return bool(_SHORT_FLAG_CLUSTER_RE.match(arg)) and letter in arg[1:]


def _match_commit_no_verify(segment: Segment, _is_ignored: Callable[[str], bool]) -> str | None:
    """`git commit --no-verify`, or its `-n` alias, bare or bundled."""
    subcommand_and_rest = _git_subcommand_and_rest(segment)
    if subcommand_and_rest is None or subcommand_and_rest[0] != "commit":
        return None
    for arg in subcommand_and_rest[1]:
        if arg == "--no-verify" or _bundled_short_flag(arg, "n"):
            return ""
    return None


def _match_commit_no_gpg_sign(segment: Segment, _is_ignored: Callable[[str], bool]) -> str | None:
    """`git commit --no-gpg-sign`."""
    subcommand_and_rest = _git_subcommand_and_rest(segment)
    if subcommand_and_rest is None or subcommand_and_rest[0] != "commit":
        return None
    return "" if "--no-gpg-sign" in subcommand_and_rest[1] else None


def _match_push_no_verify(segment: Segment, _is_ignored: Callable[[str], bool]) -> str | None:
    """`git push --no-verify`. `push` has no `-n` alias for this; `-n` there is dry run."""
    subcommand_and_rest = _git_subcommand_and_rest(segment)
    if subcommand_and_rest is None or subcommand_and_rest[0] != "push":
        return None
    return "" if "--no-verify" in subcommand_and_rest[1] else None


def _make_assignment_match(key: str) -> MatchFn:
    """A `MatchFn` that fires when `segment` carries a leading `key=...` assignment.

    Fires regardless of what program the segment invokes, per Section
    4.6.1: the assignment is denied wherever in the chain it appears,
    inline or behind `env` (`segments`/`_parse_segment` already normalize
    both spellings into `segment.assignments`), not only when it precedes
    `git` itself.
    """

    def _match(segment: Segment, _is_ignored: Callable[[str], bool]) -> str | None:
        for token in segment.assignments:
            name, _, _ = token.partition("=")
            if name == key:
                return ""
        return None

    return _match


def _match_add_force_ignored(segment: Segment, is_ignored: Callable[[str], bool]) -> str | None:
    """`git add -f`/`--force` of a path `is_ignored` reports as gitignored.

    The `-f` detection also fires on a bundled short-option cluster
    containing `f` (`-fA`, `-Af`, `-nf`), the same evasion
    `_match_commit_no_verify` already accounts for with `-n`: `git`
    accepts every one of those spellings as a forced add, so a check for
    the exact token `-f` alone lets `-fA` and friends stage a gitignored
    path unflagged. `paths` already excludes every token starting with
    `-`, so the cluster's other letters are never mistaken for a path.

    Fails closed (Error Handling Contract): if `is_ignored` raises for any
    reason -- git absent, not a work tree, or anything else -- that is
    treated the same as a confirmed gitignored path, because this rule
    cannot prove the path is *not* one.
    """
    subcommand_and_rest = _git_subcommand_and_rest(segment)
    if subcommand_and_rest is None or subcommand_and_rest[0] != "add":
        return None
    rest = subcommand_and_rest[1]
    if not any(arg in _FORCE_FLAGS or _bundled_short_flag(arg, "f") for arg in rest):
        return None
    paths = [arg for arg in rest if arg not in _FORCE_FLAGS and not arg.startswith("-")]
    for path in paths:
        try:
            ignored = is_ignored(path)
        except Exception as exc:  # the injected check can fail for any reason; fail closed
            return f"the ignore check for {path!r} raised {exc.__class__.__name__}: {exc}"
        if ignored:
            return f"{path!r} is gitignored"
    return None


def _targets_git_hooks(arg: str) -> bool:
    return bool(_GIT_HOOKS_PATH_RE.search(arg))


def _make_targets_git_hooks_match(program: str) -> MatchFn:
    """A `MatchFn` that fires when `program` (`rm` or `chmod`) targets `.git/hooks`.

    Compares through `_program_name` so `/bin/rm .git/hooks/pre-commit`
    denies the same as the bare `rm` spelling.
    """

    def _match(segment: Segment, _is_ignored: Callable[[str], bool]) -> str | None:
        if _program_name(segment) != program:
            return None
        for arg in segment.args:
            if not arg.startswith("-") and _targets_git_hooks(arg):
                return f"{arg!r} is under .git/hooks"
        return None

    return _match


def _match_make_hooks_uninstall(segment: Segment, _is_ignored: Callable[[str], bool]) -> str | None:
    """`make hooks-uninstall`, including the `make -C <dir> hooks-uninstall` spelling."""
    if _program_name(segment) != "make":
        return None
    return "" if "hooks-uninstall" in segment.args else None


# The ten denied patterns of Section 4.6.1, in the order the section lists
# them. `evaluate` walks a command's segments in order and, within each
# segment, these rules in order, returning the first match; order among
# rules that cannot both match the same segment (every pair here) has no
# observable effect, so this is simply the section's own order.
RULES: tuple[Rule, ...] = (
    Rule(
        id="git-commit-no-verify",
        fix=(
            "hook-skipping is forbidden; fix the failing pre-commit check or "
            "escalate to the operator instead of skipping it."
        ),
        match=_match_commit_no_verify,
    ),
    Rule(
        id="git-push-no-verify",
        fix=(
            "hook-skipping is forbidden on push too; fix the failing check or "
            "escalate to the operator instead of skipping it."
        ),
        match=_match_push_no_verify,
    ),
    Rule(
        id="git-commit-no-gpg-sign",
        fix=(
            "commit signing is required; configure a signing key instead of disabling verification."
        ),
        match=_match_commit_no_gpg_sign,
    ),
    Rule(
        id="env-husky-disable",
        fix=(
            "HUSKY=0 disables every husky-managed hook; fix the failing hook or "
            "escalate to the operator instead of disabling it."
        ),
        match=_make_assignment_match("HUSKY"),
    ),
    Rule(
        id="env-skip-hooks",
        fix=(
            "SKIP silently drops the named pre-commit stage; fix the failing "
            "stage or escalate to the operator instead of skipping it."
        ),
        match=_make_assignment_match("SKIP"),
    ),
    Rule(
        id="env-pre-commit-allow-no-config",
        fix=(
            "PRE_COMMIT_ALLOW_NO_CONFIG lets pre-commit run with no hooks "
            "configured at all; restore the pre-commit configuration instead "
            "of bypassing it."
        ),
        match=_make_assignment_match("PRE_COMMIT_ALLOW_NO_CONFIG"),
    ),
    Rule(
        id="git-add-force-ignored",
        fix=(
            "-f/--force stages a file .gitignore excludes; remove it from "
            ".gitignore first if it truly belongs in the repository, instead "
            "of forcing it past that exclusion."
        ),
        match=_match_add_force_ignored,
    ),
    Rule(
        id="rm-git-hooks",
        fix=(
            "deleting a file under .git/hooks uninstalls a guard hook; restore "
            "it with 'make hooks-install' if this was a mistake, or escalate "
            "to the operator if removing it is genuinely intended."
        ),
        match=_make_targets_git_hooks_match("rm"),
    ),
    Rule(
        id="chmod-git-hooks",
        fix=(
            "changing permissions under .git/hooks can silently disable an "
            "installed hook without deleting it; leave the mode alone, or "
            "escalate to the operator if a permission fix is genuinely needed."
        ),
        match=_make_targets_git_hooks_match("chmod"),
    ),
    Rule(
        id="make-hooks-uninstall",
        fix=(
            "'make hooks-uninstall' removes both guard hooks in one step; this "
            "is an operator decision -- escalate instead of running it."
        ),
        match=_match_make_hooks_uninstall,
    ),
)


def evaluate(command: str, is_ignored: Callable[[str], bool]) -> Decision | None:
    """Whether `command` matches a denied pattern; the `Decision` if so, else `None`.

    Pure (AC-FUNC-008): no input is read, no subprocess is spawned and no
    network is touched here; the only impure operation `evaluate` ever
    triggers is a call to the injected `is_ignored`, and only when a
    segment is a `git add -f`/`--force` invocation.

    An unparseable `command` denies rather than raising (module docstring's
    fail-closed rule): the `DenyBypassError` `segments` raises for that
    case is caught here and reported as the `_UNPARSEABLE_RULE_ID` rule
    instead of escaping to the caller.
    """
    try:
        parsed_segments = segments(command)
    except DenyBypassError as exc:
        return Decision(rule=_UNPARSEABLE_RULE_ID, reason=str(exc))
    for segment in parsed_segments:
        for rule in RULES:
            detail = rule.match(segment, is_ignored)
            if detail is not None:
                return rule.decision(detail)
    return None


def _load_event(stdin_text: str) -> Mapping[str, object]:
    """`stdin_text` parsed as a JSON object; the `PreToolUse` event Claude Code sent.

    Raises:
        DenyBypassError: if `stdin_text` is not valid JSON, or decodes to
            something other than a JSON object.
    """
    try:
        event = json.loads(stdin_text)
    except json.JSONDecodeError as exc:
        raise DenyBypassError(
            f"ERROR: event on stdin is not valid JSON: {exc}\n"
            "deny_bypass.py could not parse the PreToolUse event Claude Code "
            "sent on stdin.\n"
            "This is a Claude Code invocation defect, not something to work "
            "around; report it rather than retrying with --no-verify."
        ) from exc
    if not isinstance(event, dict):
        raise DenyBypassError(
            "ERROR: event on stdin is not a JSON object\n"
            f"deny_bypass.py expected a PreToolUse event object and got: {event!r}\n"
            "This is a Claude Code invocation defect; report it."
        )
    return event


def _command_from_event(event: Mapping[str, object]) -> str:
    """The Bash command `event` carries, from `event["tool_input"]["command"]`.

    Raises:
        DenyBypassError: if `event` carries no `tool_input.command`, or
            that value is not a string.
    """
    tool_input = event.get("tool_input")
    if not isinstance(tool_input, Mapping) or "command" not in tool_input:
        raise DenyBypassError(
            "ERROR: event carries no Bash command to evaluate\n"
            f"deny_bypass.py expected event['tool_input']['command'] and the "
            f"event was: {event!r}\n"
            "This is a Claude Code invocation defect; report it."
        )
    command = tool_input["command"]
    if not isinstance(command, str):
        raise DenyBypassError(
            f"ERROR: event['tool_input']['command'] is not a string: {command!r}\n"
            "deny_bypass.py cannot evaluate a non-string command.\n"
            "This is a Claude Code invocation defect; report it."
        )
    return command


def _check_ignore(path: str) -> bool:
    """`git check-ignore -q -- path`, run in the current working directory; True if ignored.

    The one impure boundary `evaluate` is deliberately kept out of
    (AC-FUNC-008 / the module docstring): only `main` (through this
    function) ever spawns a subprocess. Relies on the working directory
    Claude Code invokes this hook from already being inside the checkout,
    which is how `PreToolUse` hooks run.

    Raises:
        DenyBypassError: if the `git` binary is not on PATH, or if git
            exits with a code other than 0 (ignored) or 1 (not ignored) --
            for example because the current directory is not a git work
            tree.
    """
    try:
        completed = subprocess.run(
            ["git", "check-ignore", "-q", "--", path],
            capture_output=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise DenyBypassError(
            "ERROR: git is not installed\n"
            f"deny_bypass.py needs the git binary to check whether {path!r} is "
            "gitignored and none was found on PATH.\n"
            "Install git and ensure it is on PATH, then retry."
        ) from exc
    if completed.returncode not in (0, 1):
        stderr = completed.stderr.decode("utf-8", errors="replace").strip()
        raise DenyBypassError(
            f"ERROR: cannot determine whether {path!r} is gitignored\n"
            f"'git check-ignore' exited {completed.returncode}: {stderr}\n"
            "Confirm this runs inside a git work tree, then retry."
        )
    return completed.returncode == 0


def main(
    *,
    stdin_text: str | None = None,
    is_ignored: Callable[[str], bool] | None = None,
) -> int:
    """Read a `PreToolUse` event, deny per `evaluate`, or allow; the process exit code.

    `stdin_text` defaults to `sys.stdin.read()` and `is_ignored` defaults
    to `_check_ignore` (a real `git check-ignore` subprocess); both are
    parameters, not hard-wired reads, so a test can supply an in-memory
    event and a canned ignore check without a real stdin stream or a real
    git repository. Denial and error messages go to stderr either way;
    this always returns an int rather than calling `sys.exit` itself, so
    the `__main__` block below is the only place that turns the return
    value into a process exit code.

    Fails closed (module docstring): a malformed event, a non-string
    command, or a missing command field ends this call with a denial on
    stderr and exit code 2, the same code `evaluate`'s own denial path
    uses, never with an allow.
    """
    text = sys.stdin.read() if stdin_text is None else stdin_text
    ignored = _check_ignore if is_ignored is None else is_ignored
    try:
        event = _load_event(text)
        command = _command_from_event(event)
    except DenyBypassError as exc:
        print(str(exc), file=sys.stderr)
        return _DENY_EXIT_CODE

    decision = evaluate(command, ignored)
    if decision is None:
        return _ALLOW_EXIT_CODE
    print(
        f"deny_bypass: BLOCKED -- {decision.rule}\nReason: {decision.reason}\nCommand: {command}",
        file=sys.stderr,
    )
    return _DENY_EXIT_CODE


if __name__ == "__main__":
    sys.exit(main())
