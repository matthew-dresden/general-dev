---
name: secrets
description: Manages the secret catalog through the `devsecret` CLI only -- add, list, update, rotate, delete, mark exported, move scope -- never a second path to Parameter Store, never a value on a command line, and never a value rendered into the conversation; ends every write or delete with a confirmation naming the parameter path.
---

# secrets

Section 4.2's own roster row gives `secrets` three columns, quoted here
exactly: it "Asks" "Which secret, which scope"; it "Does" "Add, list,
update, rotate, delete, mark exported, move scope"; and it "Ends by"
"Confirmation naming the parameter path." Every operation below runs
through the `devsecret` CLI (spec Section 4.3), never a second path to
Parameter Store: `devsecret` and the `catalog` module it wraps are already
the one place that decides how a secret is stored, read or deleted
(`cli.py`'s `main_devsecret`, `devcontainer_config.catalog.CatalogClient`),
and a second implementation of that primitive is exactly what Section 3's
"DO NOT reinvent" rule and this document's own Section 3 obligation forbid.

What a CLI cannot decide on its own is the part this skill supplies: which
scope a value belongs in, whether a name is even a candidate before ever
reaching `devsecret set`, and that a rotation or a scope move is a write
followed by an independent verification, never a write trusted on its own
say-so. That last point is this skill's one addition to Section 4.2.2's own
interaction contract -- "Nothing is assumed to have worked" -- applied to a
CLI call that already returned exit `0`: a version number `devsecret set`
prints, or a `Deleted ...` line `devsecret rm` prints, is the store's own
claim about what happened, and every write- or delete-shaped row in
`## Operations` below re-reads the catalog afterward rather than repeating
that claim as this skill's own.

The one constraint every operation obeys without exception is `## Value
handling`: a value is never placed in a command's arguments, never printed
by this skill into the conversation, and a request to see one is answered
with the exact command the operator runs in their own terminal instead.
This is the operator-facing half of AC-4.3 (`devsecret list` never prints a
value, proved by seeding a sentinel and grepping the full output for it);
`## Value handling` is the one place that rule is stated, and every other
section below points back to it rather than restating it differently.

## Operations

Section 4.2's roster names seven things this skill does; the table below
gives each one row: the exact `devsecret` invocation it runs, the
confirmation that invocation itself prints (never a summary this skill
composes instead), and the independent read this skill performs afterward
to confirm the store actually agrees.

Add, update and rotate are the same `devsecret set` invocation
(`cli.py`'s `_run_devsecret_set`): they differ only in what already existed
under that name before the write and in why the operator asked for it,
never in the command issued or in what this skill verifies afterward. `set`
always resolves `--scope` against `catalog.scope_set(None)` before writing
(`cli._require_known_scope`); see `## Scope` for what that set contains
today.

| Operation | `devsecret` invocation | Confirmation | Verification |
|---|---|---|---|
| Add | A value never seen under `<NAME>` in the resolved scope: `printf '%s' "$VALUE" \| devsecret set <NAME> --scope <scope> [--exported]`, stdin only, never `devsecret set <NAME> <VALUE>` (refused at exit 5, `## Value handling`). | `set`'s own two printed lines: `Wrote /devcontainer/<scope>/secrets/<NAME> (SecureString, version <N>).`, then either `Exported. Shell startup exports this as <NAME>.` or `Not exported. Agents reach it with: devsecret get <NAME>` (`cli._run_devsecret_set`). | `devsecret list --scope <scope>` run once before the write and once after: the row for `<NAME>` is absent in the first listing and present in the second, so the write is confirmed by an independent read, not by the version number `set` returned. |
| List | Reading the catalog's metadata for one scope or every scope in effect: `devsecret list [--scope <scope>]`. | None: nothing was written, so there is no parameter path to confirm. The rendered `NAME SCOPE LAST-CHANGED EXPORTED` table (`cli._render_secret_listing`) is the operation's entire output. | None: a read has nothing to verify against a copy of itself. A repeated `list` immediately after reproduces the identical table when nothing else changed -- this is exactly the property every other row in this table uses `list` to check against a change it just made. |
| Update | Replacing the value already stored under an existing `<NAME>`, for a reason other than scheduled rotation (correcting a wrong value, for example): the identical invocation Add uses. | The identical two lines `set` prints for Add. | `devsecret list --scope <scope>` before and after the write: the `LAST-CHANGED` value for `<NAME>` differs between the two listings, confirming a new version actually reached the store rather than only the CLI reporting one. |
| Rotate | Replacing the value already stored under an existing `<NAME>` specifically so the old value stops being what agents fetch: the identical invocation Add and Update use. | The identical two lines `set` prints, naming the new version so the operator can distinguish it from the version being retired. | The identical before/after `LAST-CHANGED` comparison Update uses. |
| Delete | `devsecret rm <NAME> --scope <scope>` (`cli._run_devsecret_rm`). `rm`'s own confirmation prompt, `Delete '<NAME>' in scope '<scope>'? [y/N]` (`cli._confirm_delete`), is answered by the operator directly; this skill never answers it on the operator's behalf, the same no-silent-consent rule `/devcontainer:teardown`'s own confirmation follows. | `Deleted '<NAME>' from scope '<scope>'.` on confirmation; `Not deleted: '<NAME>' in scope '<scope>'.` on decline, reported as nothing having happened, never as an error. | `devsecret list --scope <scope>` run once more afterward: the row for `<NAME>` is absent, confirmed the same value-free way AC-4.3 already proves for `list` itself. |
| Mark exported | `set` always rewrites the whole document -- `Value`, `Type` and `Description` together (`catalog.CatalogClient.write`) -- so there is no call that changes only the exported flag. This skill reads the existing value and rewrites it unchanged with `--exported` set, in one pipeline that never lets the value rest anywhere this skill's own output could show it: `devsecret get <NAME> \| devsecret set <NAME> --scope <scope> --exported`, run under `pipefail` (`## Value handling` rule 4) so a failing `get` aborts before `set` ever consumes stdin. `get` takes no `--scope` of its own (`cli._run_devsecret_get` resolves through `catalog.resolve(client, None, args.name)`); see `## Scope` for what that resolves to today, which is the same set `set`'s own `--scope` must already agree with for this pipeline to write back to the scope the value was just read from. | `set`'s own `Wrote ... (SecureString, version <N>).` and `Exported. Shell startup exports this as <NAME>.` lines. | `devsecret list --scope <scope>` after: the row for `<NAME>` now shows `EXPORTED yes`. |
| Move scope | Three steps, strictly in this order, never reordered: (1) `devsecret get <NAME> \| devsecret set <NAME> --scope <new-scope> [--exported]` run under `pipefail` (`## Value handling` rule 4) so a failing `get` aborts before `set` ever consumes stdin, the same never-see-the-value pipeline Mark exported uses; (2) `devsecret list --scope <new-scope>`, whose output this skill checks for a row naming `<NAME>` to confirm the new path is actually present -- scope-targeted so it can never be answered by the old, still-live path the way an unscoped `get` would be, since `catalog.list_resolved`'s `scope` argument narrows the query to exactly the named scope (`cli._run_devsecret_list`), and structurally value-free (`describe-parameters` never requests decryption, `## Value handling` rule 3) so no `/dev/null` redirection is needed; (3) only once step 2's output includes `<NAME>`, `devsecret rm <NAME> --scope <old-scope>`, its own `y/N` prompt answered by the operator as in Delete. Refused before step 1 ever runs when `<old-scope>` equals `<new-scope>`: see `## Scope` for why, and for why that is the only case the current CLI can produce. | Both paths, from the two commands that actually ran: the new path with its version (step 1's `set` output) and the old path now deleted (step 3's `rm` output) -- for example, `Moved <NAME>: wrote /devcontainer/<new-scope>/secrets/<NAME> (SecureString, version <N>); confirmed present at the new path; deleted /devcontainer/<old-scope>/secrets/<NAME>.` A confirmation that said only "done" could not tell the operator an instance-scoped write from a shared one, which is exactly the gap this row's report closes. | Step 2 above is this operation's verification, and it is what gates step 3: the old path is never deleted until `devsecret list --scope <new-scope>` has independently shown `<NAME>` present at the new path. This is deliberately not an unscoped `devsecret get`: `catalog.resolve` walks the resolution set instance-first (`## Scope`) and, for the entire window between step 1 and step 3 in which `<NAME>` exists at both paths, an unscoped `get` for an instance-to-shared move would still be answered by the old instance-scoped path and never prove the new one readable at all. Between step 1 and step 3, `<NAME>` exists at both paths at once; a `devsecret list` run in that window shows both, and this is the intended, visible intermediate state, not an error. The state where `<NAME>` exists at neither path is never reachable, because step 3 only ever runs after step 2 has already confirmed the new path is present. |

## Value handling

Four rules. The first two are `devsecret`'s own (spec Section 4.3); the
last two are what this skill adds on top of them:

1. **A value is never placed in a command's arguments.** `set`'s own
   positional `VALUE` argument exists only to be refused: `_run_devsecret_set`
   checks `args.value is not None` before anything else runs, prints
   `_devsecret_value_as_argument_message` (`ERROR: a secret value may not be
   supplied as a command-line argument ...`), and exits `5`
   (`EXIT_VALUE_EXPOSURE_REFUSED`) without ever touching stdin or the
   catalog. This skill never composes a `devsecret set` invocation with the
   value as a second word; every write in `## Operations` reads the value
   from stdin.
2. **`set` reads stdin and refuses a TTY unless `--stdin` is explicit.**
   `sys.stdin.isatty() and not args.stdin` prints
   `_devsecret_tty_without_stdin_flag_message` (`ERROR: stdin is a
   terminal ...`) and exits `2`, before stdin is ever read
   (`cli._run_devsecret_set`). An interactive paste is deliberate, never
   the default.
3. **This skill never renders a secret value into the conversation.** A
   request to display a value is answered with the exact `devsecret get
   <NAME>` command for the operator to run in their own terminal, naming
   the secret and never the value -- in a refusal, a log line or a summary
   alike. Everywhere this skill itself needs to know only whether a value
   is present at a given scope, not what it is (Add's, Update's, Rotate's
   and Mark exported's post-write reads, and Move scope's step 2, all in
   `## Operations`), it uses `devsecret list --scope <scope>`, never
   `devsecret get`: `list` is built on `describe-parameters`, which never
   requests decryption, so it structurally cannot carry a value regardless
   of how its output is handled (AC-4.3), and no redirection is ever needed
   to keep it from doing so.
4. **A value-carrying pipeline aborts if its read half fails.** Mark
   exported and Move scope's step 1 (`## Operations`) both pipe `devsecret
   get <NAME>` directly into `devsecret set <NAME> --scope <scope> ...`.
   This skill runs both under `pipefail` (or an explicit check of `get`'s
   own exit status before `set` ever starts): a POSIX pipeline reports only
   the right-hand exit status, so without this a failing `get` -- for
   example exit `3` for an expired SSO session, `## Failure semantics` --
   would still let `set` run against empty stdin, surfacing the real
   failure as a confusing downstream write error rather than at the point
   `get` actually failed.

## Scope

Section 5.4 fixes scope resolution as instance-first then shared
(decision D12): `catalog.scope_set(instance)` returns the instance scope
followed by the shared scope when an instance is given, and the shared
scope alone otherwise (`catalog.py`). This skill states which scope
answered a `get`, a `list` row, or a write -- never leaves that
implicit -- since a value found in the shared scope when the operator
expected an instance-scoped one is a real difference in blast radius, not
a detail.

No instance is accepted by `devsecret`'s own argument parser (`cli.py`'s
module docstring: "`devsecret`'s own commands do not resolve an instance:
every command here resolves or narrows against `catalog.scope_set(None)`
... independent of `devcontainer_config.instances`, this module's separate
instance-resolution entry point below"): `cli.py` now exposes that entry
point as the `resolve-instance` subcommand (E8-F1-S1-T1), but `devsecret`
does not call it, the identical gap `/devcontainer:doctor`'s own
introduction already discloses for its own secrets findings. `catalog.scope_set(None)`
returns the shared scope alone, so today every `--scope` this skill's own
invocations name, and the scope `get` resolves against, is the shared
scope, and `set` and `rm` both refuse (`UnknownScopeError`, exit `2`) any
`--scope` value outside that one-member set before the store is ever
reached (`cli._require_known_scope`). Section 9's per-instance parameter
prefix (`/devcontainer/<name>/`) is real at the `catalog` layer --
`catalog.parameter_path` composes it for any syntactically valid scope --
but reaching it through `devsecret` itself is separate, future work, the
same instance-detection wiring `## Operations`' Mark exported row's own
`get` call is waiting on.

This is why Move scope refuses outright when `<old-scope>` equals
`<new-scope>`, and why that is the only case today's CLI can ever produce:
deleting `<old-scope>` after writing an identical `<new-scope>` path would
delete the very parameter step 1 of that operation just wrote -- the store
holds one entry per path, not one per write -- which is the opposite of
"the state where it exists in neither is not reachable," `## Operations`'
own invariant for that row. A move to or from any other scope is refused
for the same reason `## Naming`'s and `## Failure semantics`' unrecognized-
scope refusal already is: there is no second scope for `devsecret` to
reach yet. Once instance detection reaches `devsecret`'s own parser, this
skill's Move scope row runs unchanged against two genuinely distinct
scopes: its step 2 reads `devsecret list --scope <new-scope>`, and
`catalog.list_resolved`'s `scope` argument narrows that read to exactly
the named scope, so a still-live entry in the old scope can never answer
in the new scope's place -- unlike an unscoped `devsecret get`, which
resolves instance-first (`catalog.resolve`) and, during the window a move
leaves both scopes populated, could still be answered by the old path
instead of proving the new one present. Nothing above is written to be
discarded when that lands.

## Naming

A secret name must be a valid environment-variable identifier: it starts
with a letter or underscore and contains only letters, digits and
underscores (`catalog._NAME_PATTERN`), because an exported secret becomes a
shell variable and an invalid identifier could never be exported correctly
in the first place. `catalog.parameter_path` runs this check
(`catalog._validate_name`) before it ever returns a path, and `set`, `get`
(through `catalog.resolve`) and `rm` each reach the store only through a
path `parameter_path` returned, so a malformed name is refused at exit `2`
(`InvalidSecretNameError` -> `EXIT_USAGE_ERROR`) before any of them ever
touches the store, and, for `set`, before its own TTY check in `## Value
handling` rule 2 even runs. This skill never offers to
normalize an invalid name into a valid one: a silently renamed secret is
not the secret the operator asked for, and every remedy this skill states
for the condition is "rename it and retry," never a substitution this
skill performs on its own.

## Failure semantics

| Condition | Behavior |
|---|---|
| A precondition check fails | Covers every usage-error and not-found condition `## Value handling` and `## Naming` already name: an invalid name (exit `2`, `## Naming`'s rule, no auto-normalize), a value supplied as an argument (exit `5`, `## Value handling` rule 1), stdin a TTY without `--stdin` (exit `2`, rule 2), an unrecognized `--scope` (exit `2`, `catalog.UnknownScopeError`, naming the scopes in effect, today `shared` alone per `## Scope`), a Move scope request whose old and new scope agree (`## Scope`), and a secret absent from every searched scope (`get`, exit `4`, `catalog.SecretNotFoundError` naming every scope searched in order and pointing at `devsecret list`). None of these run any later step of the operation they interrupted; a failing check in step 1 of Move scope, for example, never reaches step 2 or 3. A request to display a value is refused before any `devsecret` invocation is composed at all, per `## Value handling` rule 3 -- there is no exit code for it, since this skill never runs a command to produce one. |
| An action the skill took did not verify | Every write- or delete-shaped row in `## Operations` re-reads the catalog afterward rather than trusting the command's own exit code or printed version number. When that re-read disagrees -- Add's or Update's post-write `list` still missing the row, Mark exported's post-write `list` still showing `EXPORTED no`, Move scope's step 2 `list --scope <new-scope>` still missing `<NAME>` -- this skill reports the write it attempted, the verification that failed, and the state that read left the catalog in, and does not report the operation as complete. It never retries the same write silently a second time; a second attempt is a fresh invocation the operator asks for, exactly as `/devcontainer:teardown`'s own "never retry silently" rule states for its own actions. |
| A step needs the operator (SSO, `sudo`, a key) | Exit `3` is `devsecret`'s own for every backend condition (`cli._DEVSECRET_EXIT_CODES` maps `CatalogUnauthorizedError`, `CatalogUnavailableError`, `CatalogUnclassifiedError` and the base `CatalogError` all to `EXIT_BACKEND_ERROR`), and this skill states a different remedy for each rather than one remedy for all of them, since `catalog.CatalogUnavailableError`'s own docstring keeps that class narrow "so a caller is never told to refresh a credential that was not the actual problem": (1) the `aws` binary itself is missing from `PATH` -- `catalog.CatalogClient._invoke` catches the runner's `FileNotFoundError` before any command ever reaches the store and raises `CatalogUnavailableError` with `catalog._unavailable_no_binary_message`, and this skill states that message's own remedy, "Install the AWS CLI v2"; (2) no AWS credential resolved -- `catalog._raise_for_failure` matches `SSO_SESSION_MARKER` or `NO_CREDENTIALS_MARKER` in the `aws` CLI's stderr and raises the same `CatalogUnavailableError` class, but through `catalog._unavailable_no_credential_message`, and only for this condition does this skill state `aws sso login --profile <profile>` (`<profile>` from `$AWS_PROFILE`, defaulting to `'default'`); (3) the store answered `AccessDeniedException` for the parameter prefix -- `catalog.CatalogUnauthorizedError` via `catalog._unauthorized_message`, and this skill states the missing `ssm:*` grant on that prefix, never an SSO remedy, since refreshing a credential does not change an IAM policy; (4) every other non-zero `aws` exit -- throttling, an invalid region, a validation failure, anything `catalog._raise_for_failure` does not otherwise classify -- raises `catalog.CatalogUnclassifiedError` via `catalog._unclassified_failure_message`, and this skill states the operation, the parameter path and the exact re-run command that message names (`catalog._unclassified_failure_next_step`) rather than guessing a credential or permission cause; (5) the store answered successfully but with a response this client cannot parse or a parameter it did not write -- the base `catalog.CatalogError`, raised directly by `catalog._parse_response_json` and `catalog._record_from_entry` (`catalog._malformed_response_message` / `catalog._malformed_listing_message` / `catalog._foreign_parameter_message`), and this skill states retrying the operation and, if that persists, checking the `aws` CLI version, since nothing about a malformed response is a credential or authorization problem. None of the five ever falls back to any local store (Section 5.4: one backend, no offline copy). Whichever condition applies, this skill waits for the operator to resolve it, then re-runs the identical invocation the failure interrupted, never retrying automatically. `rm`'s own `y/N` prompt, in Delete and in Move scope's step 3, is likewise answered by the operator directly and never assumed to be "yes" on their behalf. |
| A gate is reached (Section 4.4) | This skill reaches no Section 4.4 gate: every operation in `## Operations` writes, reads or deletes a Parameter Store entry, never a `terragrunt apply` or `terragrunt destroy`, and creates or destroys no AWS resource of the kind `GATE-APPLY`, `GATE-CUTOVER` or `GATE-DESTROY` governs. |
| An answer fails validation | The which-secret and which-scope answers this skill's own roster row asks for are covered by the two rules above (an invalid name, an unrecognized scope) whenever the answer is well-formed but wrong; an answer that is empty, ambiguous, or names neither a secret nor a scope this skill can resolve at all is reported plainly and asked again rather than guessed, since a guessed name or scope is exactly the wrong-tier risk `## Scope` describes. |
| The operator aborts | Nothing is written in Add, Update, Rotate or Mark exported until stdin's value has actually been read and `client.write` has returned a version (`## Operations`); an abort before that point leaves the catalog exactly as it was, confirmed by a fresh `list`. An abort mid-Move scope, after step 1's write but before step 3's delete, leaves `<NAME>` at both paths -- `## Operations`' own stated intended state for that window, not a corrupted one -- and re-invoking Move scope resumes at step 2 rather than repeating step 1's write. |

## Related specifications

- Section 2, G4, `spec/devcontainer-platform.md`: agents reach many
  credentials without any of them touching disk -- the goal `## Value
  handling` and `## Scope` exist to keep true for every write, read and
  delete this skill performs.
- Section 4.2: `secrets`'s own roster row, quoted in this document's
  introduction, and the interaction contract every skill obeys.
- Section 4.2.2: the failure-semantics table this document's own
  `## Failure semantics` table instantiates.
- Section 4.3: the `devsecret` CLI's six invocations, the no-argument and
  stdin/TTY rules `## Value handling` inherits, and the five exit codes
  `## Failure semantics` maps. AC-4.3: `devsecret list` never prints a
  value.
- Section 5.3: the Parameter Store layout every path named in
  `## Operations` matches exactly -- `/devcontainer/<scope>/secrets/<NAME>`,
  always `SecureString` (`catalog.SECURE_STRING_TYPE`), for both an
  instance scope and the shared scope.
- Section 5.4: one backend, no offline store, instance-first then shared
  resolution -- why `## Failure semantics`' operator-needed row never names
  a fallback, and why `## Scope` states the resolution rule once rather
  than restating it per operation.
- Section 9: the per-instance parameter prefix `## Scope` names as real at
  the `catalog` layer today and not yet reachable through `devsecret`'s own
  parser.
- Section 13, decisions D10-D13: one secret backend, no value on disk,
  two-tier scoping, and `devsecret` as a real CLI -- the four decisions
  this document's own design rests on without restating their rationale.
- AC-4.3 (Section 4.3) and AC-5.3 (Section 5.4): `devsecret list` never
  prints a value, and no code path writes a secret value to a persistent
  filesystem -- neither of which this skill's own operations add a new way
  to violate, since every write and read above goes through `devsecret`
  alone.
