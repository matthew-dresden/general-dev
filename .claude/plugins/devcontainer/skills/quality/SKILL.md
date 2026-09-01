---
name: quality
description: Drives `make validate` to green by reading its own sub-target set from the Makefile rather than a copy embedded here, interpreting each failing sub-target's root cause and fixing it -- never a suppression, an ignore-list entry, a raised threshold, or a narrowed `LINT_EXCLUDES`/`SPELL_FILES`; stops and asks for human approval on a suspected false positive, hands anything else it cannot fix to `/devcontainer:doctor` or the operator, and ends by reporting the exit code of a fresh `make validate` run.
---

# quality

Section 4.2's own roster row gives `quality` three words for what it does,
quoted here exactly: it "Asks" "Nothing"; it "Does" "Runs `validate`,
interprets findings, fixes root causes"; and it "Ends by" "Green, or a
findings list." AC-3.5.1 (Section 3.5, Standards audit) is the rule this
skill exists to give a surface: `make validate` passes on every commit this
spec produces, and a work unit leaving it red is incomplete regardless of
its own tests. Without a standing place that reads a red `validate`,
interprets what it means, and fixes the cause, that rule has no agent
acting on it.

Interpretation is the value this skill adds over the target itself: `make
validate`'s own exit code says only pass or fail, never which of its
sub-targets failed or why. `## Findings` states, for each sub-target this
skill has interpretation for today, what a failure means, its likely root
cause, and the fix -- the skill states which of the three applies before
touching anything, per this document's own Error Handling Contract.

The prohibition in `## Never` is the reason this skill is specified rather
than left implicit: the shortest path from a red `validate` to a green one
is almost always a suppression, and `CLAUDE.md` and Section 4.6 forbid
every one of them. Section 4.6 admits exactly one alternative to fixing a
finding -- a false positive, which needs human approval -- and `##
False positives` is this skill's own instance of Section 4.2's interaction
contract applied to that one case: state the exact request, wait, and
never assume silence is a yes.

## Procedure

1. Read the current sub-target set `make validate` actually runs from the
   Makefile itself -- its `validate:` line names `lint` and `test`, and
   `lint:`'s own line names `lint`'s own sub-targets -- rather than testing
   against a list carried in this document. A sub-target added to or
   removed from either line changes what this step reads without an edit
   here (AC-FUNC-002); nothing below is this skill's own second copy of
   that list, only its interpretation of the names that list can produce.
2. Run `make validate`. An exit of `0` needs no interpretation: report per
   `## Completion` and stop.
3. On a non-zero exit, identify which sub-target(s) from step 1's set
   produced the failing output, and capture each one's own output rather
   than only `make validate`'s aggregate exit code.
4. For each failing sub-target, classify it against `## Findings`: a row
   below names its meaning, root cause and fix; a sub-target with no row
   below is reported by its own name and raw output rather than given
   invented guidance (`## Findings`'s own closing rule).
5. State which case applies -- a real defect with a known fix, a real
   defect with no row yet, or a suspected false positive (`##
   False positives`) -- before changing anything.
6. Apply the minimal fix the classification in step 5 names, refusing
   every response `## Never` lists; a candidate fix that narrows what is
   checked is refused with the reason stated, not silently substituted for
   a real fix (this document's own Error Handling Contract).
7. Repeat steps 2 through 6 until a fresh `make validate` run started
   after every fix has landed exits `0`, or until every remaining failure
   has been named to `/devcontainer:doctor` or the operator per
   `## Handoff`.
8. Report per `## Completion`.

## Findings

One row per sub-target this skill has interpretation for today. A
sub-target `## Procedure` step 1 finds failing that has no row here (for
example, one `lint` gains after this table was written) is reported by its
own name and its own raw output; this skill states plainly that it has no
interpretation on file for it rather than inventing a root cause the
sub-target's own author never gave it.

| Sub-target | Failure means | Root cause | Fix |
|---|---|---|---|
| `lint-private` | A file this repository requires to stay untracked reached the git index. | `git add` picked up `shell.env`, `devcontainer-environment-variables.json` or `.devcontainer/aws-profile-map.json` (the `PRIVATE_FILES` set) despite `.gitignore`, typically after `git add -f` or a rename. | Untrack the named file -- `lint-private`'s own failure line already states the exact `git rm --cached <path>` command -- and confirm a fresh run no longer finds it tracked. Never add it to an ignore exception; these files hold identity or secrets. |
| `lint-nested` | A path in the index is a gitlink (mode `160000`) rather than ordinary tracked content. | A clone of another repository was `git add`ed from inside this checkout instead of being kept outside it or wired in as a real submodule. | Untrack the pointer -- `lint-nested`'s own failure line already states the exact `git rm -r --cached <path>` command -- and keep the other repository's clone outside this one's tree. |
| `lint-json` | A `*.json` file in scope does not parse. | A syntax error in that file's own content (for example `plugin.json`, `marketplace.json`, `.claude/settings.json`). | Correct the JSON at the path `.devcontainer/lint-json.py` names. Never widen `LINT_EXCLUDES` to skip the file (`## Never`). |
| `lint-sh` | Shellcheck reported a warning-level or higher finding in a `*.sh` file. | A real shell-scripting defect, named by shellcheck's own rule id and line. | Correct the script per the cited diagnostic. A `# shellcheck disable=` directive is this sub-target's own instance of `## Never`'s suppression prohibition, not an exception to it. |
| `lint-md` | `pymarkdownlnt` reported a structural or formatting rule violation in a `*.md` file. | The document's own Markdown does not conform to the cited rule. | Correct the Markdown at the cited line. `make format` applies the auto-fixable subset; its diff is reviewed before it is trusted, the same discipline `lint-spell`'s row below states for `make spell-fix`. |
| `lint-spell` | A British form or a misspelling reached a document `SPELL_FILES` covers. | The word itself, not the dictionary. | `make spell-fix` rewrites what its dictionary can map; the diff is reviewed, not trusted, since a rewrite can be wrong on a proper noun. A word the dictionary does not know that is a legitimate term is a `## False positives` case, never a silent addition to a personal dictionary or an exclusion from `SPELL_FILES` (`## Never`). |
| `lint-secrets` | A credential-shaped value reached staged content or a commit in the pushed range. | Stated in full in `## Secret findings` rather than restated here: Section 4.6's own consequence and its two remedies apply, not a same-shape fix as the rows above. | See `## Secret findings`. |
| `test` | The hermetic pytest suite (`make test`; no docker, no AWS, no network, Section 4.1.2) reported a failure, or its own leading prerequisite loop found `uv` or `zsh` missing. | A missing host tool needs no further diagnosis: the prerequisite loop's own printed line already names the install command. A test failure is either the production code under test behaving incorrectly, or the test's own expectation being wrong. | Install the missing tool with the command the prerequisite loop already printed. For a test failure, state which of the two causes applies, then fix it: the production code for a genuine defect, or the test's own expectation for a wrong one -- never by skipping it, marking it `xfail`, or asserting around it instead (`## Never`, AC-FINAL-013). |

## Never

None of the responses below is applied regardless of how small the
remaining diff would be, and none is applied on this skill's own judgment
even once an approval is being sought elsewhere (`## False positives`):

- Inline suppression annotations: `# noqa`, `# nosec`, `// nosec`,
  `# type: ignore`, `@SuppressWarnings`, `// nolint`, `// eslint-disable`
  or `/* eslint-disable */`, `# pragma: no cover`, `# skipcq`, a
  `# shellcheck disable=` directive, or any other per-language annotation
  that suppresses a linter, type checker or security-scanner finding
  instead of fixing it -- `CLAUDE.md`'s own list is stated as illustrative,
  not exhaustive, and this skill treats every member of that class the
  same way regardless of language.
- Command-line bypasses: `--no-verify`, `--no-gpg-sign`, `--skip-checks`,
  `--force`, or an equivalent flag used to get past a quality gate.
  Section 4.6.1's own `PreToolUse` hook already denies the git-level
  instances of this (`git commit --no-verify`/`-n`, `git push --no-verify`,
  `git commit --no-gpg-sign`, `HUSKY=0`, `SKIP=`,
  `PRE_COMMIT_ALLOW_NO_CONFIG`, `git add -f` of a gitignored private file,
  removing or `chmod`-ing `.git/hooks`, `make hooks-uninstall`); this skill
  never attempts one of those either, and never asks the operator to.
- Configuration changes that hide a finding rather than fix it: adding a
  path or pattern to a linter's own ignore list or config `exclude` rule,
  or raising a threshold so a finding it already caught stops being
  reported.
- Narrowing `LINT_EXCLUDES` or `SPELL_FILES` to remove the file a finding
  named from what gets checked. Neither name looks like a suppression --
  `LINT_EXCLUDES` exists to keep `repos/`'s own clones and `node_modules`
  or `devbench` out of scope, and `SPELL_FILES` exists to let a caller
  spell-check a set this repository does not own -- but narrowing either
  one specifically to drop the file a finding named is the same
  suppression as any entry above, spelled as a Make variable instead of a
  code comment, and this skill treats it identically.

Every one of the above is refused with the reason stated, per this
document's own Error Handling Contract; the fix that follows always
changes the flagged content itself, never what checks it.

## False positives

A finding this skill judges to be a false positive is never resolved by
applying one of `## Never`'s prohibited responses on its own judgment:
Section 4.6 states plainly that there is no ignore list, that a finding is
either real and fixed, or a false positive that needs human approval to
suppress, per `CLAUDE.md`. This skill's own procedure for that case: stop
before changing anything; state the finding exactly as the sub-target
reported it, the file and line it names, and the specific reason this
skill believes the finding does not describe a real defect; then ask for
explicit human approval and wait. An unanswered request, a later unrelated
commit, or silence is never read as approval, and this skill never
proceeds as though approval had been granted. Writing the suppression
itself, with the documented rationale `CLAUDE.md`'s own security-scan
section requires, is the operator's own action once approval is given;
this skill's role ends at asking, exactly as `## Handoff` states for
anything needing approval.

## Secret findings

`lint-secrets` failing is not the same shape of finding as `## Findings`'
other rows, and this section states Section 4.6's own consequence directly
rather than leaving it implied: "The value is in history, so removing it
now is not enough." A value already staged or already pushed is reachable
from every clone that fetched it, and deleting the line in a later commit
leaves it recoverable from every commit before that one. Section 4.6 names
exactly two remedies, and this skill states both rather than choosing one
on the operator's behalf:

- **Rewrite history**: `git rebase -i <before>` (edit the offending
  commit, remove the value, continue), for a value that has not left the
  operator's own unpublished branch, or that the operator chooses to
  force-push despite having shared it.
- **Rotate and push deliberately with a recorded approval**: replace the
  exposed value at its real source -- the credential's own issuer, or the
  secret's own entry in the catalog through `/devcontainer:secrets` -- so
  the leaked value stops being valid, then push with the rotation and the
  approval recorded.

This skill never rewrites history and never rotates a value itself: both
remedies change something outside the file that failed the check, and
Section 4.2's own interaction contract governs this case exactly -- state
the exact command, wait for the operator, then verify, never assumed to
have worked. When the same value that failed `lint-secrets` also appears
in the developer's own `shell.env` -- one of the conditions `lint-secrets`
itself detects (Section 4.6) -- the finding is also a `/devcontainer:doctor`
configuration finding (its own "`shell.env`'s active configuration holds
no credential-shaped value" row); this skill names that overlap and hands
it to `/devcontainer:doctor` per `## Handoff` rather than re-deriving
doctor's own remedy a second way.

## Completion

`make validate`'s own exit code from a fresh run is this skill's only
source of truth for "green." After every fix from `## Findings` has
landed, and every finding resolved through `## False positives` or named
through `## Handoff`, this skill re-runs `make validate` once more and
reports exactly that run's own exit code:

- **Exit `0`**: report green, stating that this is the fresh run's own
  result, not a projection from the fix that was just made.
- **Non-zero**: report the sub-target that is still red, its output, and
  that the baseline remains red -- a findings list, per Section 4.2's own
  roster row for this skill ("Green, or a findings list"). Never reported
  as green from the run that preceded the fix, and this skill never
  commits anything while `make validate` is red.

A run of `make validate` made before every fix has landed proves nothing
about the state after the last one; this skill's own report always names
which run -- the fresh one -- its exit code came from.

## Handoff

Two destinations, never left implied:

- **`/devcontainer:doctor`**, for a finding that is also a state finding
  doctor's own table already covers -- concretely, a `lint-secrets`
  finding that also names a value present in `shell.env` (`##
  Secret findings`), which is simultaneously doctor's own configuration
  finding. This skill names the overlap and points at
  `/devcontainer:doctor` rather than re-implementing that finding's own
  remedy a second way. Nothing else in `## Findings` maps to a
  container-state or drift finding, since `make validate` runs host-only
  with no docker or AWS call (Section 4.1.2) and never itself surfaces
  one.
- **The operator**, for anything needing a decision, a credential, or an
  approval this skill cannot supply on its own: a suspected false positive
  (`## False positives`), and a secret finding's own rewrite-or-rotate
  remedy (`## Secret findings`), since both change something outside the
  file that failed and neither is this skill's to decide alone.

Nothing `make validate` surfaces is left without one of these two named,
or a fix already applied under `## Findings`.

## Failure semantics

| Condition | Behavior |
|---|---|
| A precondition check fails | This skill has no precondition of its own ahead of the first `make validate` run: `make test`'s own leading prerequisite loop (a missing `uv` or `zsh`, `## Findings`'s `test` row) is the one precondition `make validate` itself can fail on before any sub-target actually runs, and this skill reports it exactly as that row states, then stops rather than attempting `## Procedure`'s later steps against a suite that never ran. |
| An action the skill took did not verify | Every fix this skill applies is verified only by `## Completion`'s own fresh `make validate` run, never by the fix's own edit alone. A fix that run still reports failing is reported exactly as `## Completion` states -- the sub-target, its output, and that the baseline is still red -- and never retried silently a second time without a new, distinct fix. |
| A step needs the operator (SSO, `sudo`, a key) | `## Secret findings`'s two remedies and `## False positives`'s approval request are this skill's own instances of this row: state the exact command or the exact request, wait, and verify -- the git state a rewrite or a rotation leaves, or the recorded answer an approval leaves -- before continuing, never assumed to have happened. |
| A gate is reached (Section 4.4) | This skill reaches no Section 4.4 gate: every fix it applies edits a file already in the working tree, and it creates or destroys no AWS resource and runs no `terragrunt apply` or `terragrunt destroy`. |
| An answer fails validation | This skill asks nothing of its own (Section 4.2's own roster row: "Asks: Nothing") and calls no interview validator, so this condition never arises for it. The only answer it ever waits on is the human approval `## False positives` requests, and an ambiguous or unclear one is treated as not yet granted, never as a yes. |
| The operator aborts | Nothing is reported green, and nothing is left implied as fixed, until `## Completion`'s own fresh `make validate` run has actually reported it. An aborted run leaves whatever fixes had already landed in the working tree exactly as they are, still unverified; re-invoking this skill resumes at `## Procedure` step 2 (run `make validate` again) rather than repeating a fix already applied. |

## Related specifications

- Section 3.5, "Section 3.5 -- Standards audit", `spec/devcontainer-platform.md`:
  AC-3.5.1, quoted in this document's introduction, and the clause table
  `## Never` and `## False positives` together satisfy for "No bypassing
  checks."
- Section 4.1.2: `make validate`'s own row ("Already exists. Gains `test`
  when the suite lands.") and `make test`'s host-only, no-docker-no-AWS
  contract, which `## Handoff` relies on to state that nothing here ever
  surfaces a container-state or drift finding.
- Section 4.2: `quality`'s own roster row, quoted in this document's
  introduction, and the interaction contract every skill obeys, applied
  here in `## False positives` and `## Secret findings`.
- Section 4.2.2: the failure-semantics table this document's own
  `## Failure semantics` table instantiates.
- Section 4.6: "There is no ignore list", the false-positive/human-approval
  rule `## False positives` states, and the two remedies `##
  Secret findings` reproduces.
- Section 4.6.1: the `PreToolUse` bypass-denial hook `## Never`'s
  command-line-bypass bullet cross-references rather than restates.
- Section 14.1: the `make help` `QUALITY` section naming `make validate`,
  `make test`, `make test-live`, `make lint-secrets` and
  `make hooks-install` as the same targets this document interprets.
- AC-3.5.1 (Section 3.5): `make validate` passes on every commit produced
  by this spec; a work unit leaving it red is incomplete regardless of its
  own tests.
- AC-4.6 (Section 4.6): every bypass pattern `## Never` names is denied,
  either by the `PreToolUse` hook (the git-level patterns) or by this
  skill's own refusal (the annotation and configuration patterns).
