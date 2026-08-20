"""Whether the private configuration on disk is usable (spec Section 4.5).

`/devcontainer:setup-local`'s G1 worked example (spec Section 2) ends its
happy path with one line this module produces:
`Verified: no placeholders, BASH_ENV matches /workspaces/general-dev/shell.env`.
Without it, `render` can write three files, report success, and leave the
operator with a container that fails during postCreate for a reason no check
named (spec Section 4.2.1's "the three files complete and consistent").

Three classes of finding, each with a distinct remedy:

* **Completeness** -- each of `repo.PRIVATE_FILES` exists, is readable, and
  parses in its own format: `shell.env` is read as text, the two JSON files
  additionally parse as JSON.
* **No placeholders** -- reuses the `<[^<>]*>` shape from the placeholder
  scan in the `make init` target in `_PLACEHOLDER_PATTERN`, rather than a
  second definition, so the two scans cannot disagree about the shape of a
  token. Their scope still differs: this check scans only `shell.env`'s
  active `export` lines, since a placeholder in a commented-out line is not
  reachable by any shell that sources the file, while `make init`'s scan
  greps the whole file, comments included, so `make init` can still report
  a placeholder that this check calls clean.
* **Consistency** -- facts asserted in more than one file must agree:
  `BASH_ENV` against `repo.container_workspace`, every identity variable
  `shell.env` shares with `containerEnv`, and `aws-profile-map.json`'s
  populated-or-empty state against `AWS_CONFIG_ENABLED`.

This module only reports: no function here writes, chmods or deletes
anything. `verify_all` returns every finding it discovers rather than
stopping at the first (spec Section 4.2.2), so one pass over the result
corrects every problem the configuration has, the same way answer
validation reports every failing field rather than the first.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import repo

# The placeholder scan in the `make init` target (`grep -o '<[^<>]*>'
# "$target"`) defines this shape; kept identical here rather than restated
# as a second definition, so the two checks cannot disagree about what
# counts as a placeholder left behind by an operator who copied an example
# and stopped.
_PLACEHOLDER_PATTERN = re.compile(r"<[^<>]*>")

# An uncommented `export VARIABLE=value` line: the configuration a shell
# sourcing this file actually observes. Anchored at column zero, so a `#
# export ...` comment (the remote-docker block on a local backend, or the
# project-specific example line render never touches) never matches.
_ACTIVE_EXPORT_LINE = re.compile(
    r"^export[ \t]+(?P<name>[A-Za-z_][A-Za-z0-9_]*)=(?P<value>.*)$", re.MULTILINE
)

# The two PRIVATE_FILES entries that must additionally parse as JSON.
_JSON_FILES: tuple[str, ...] = (repo.DEVCONTAINER_ENV_JSON, repo.AWS_PROFILE_MAP)


@dataclass(frozen=True)
class Finding:
    """One verification failure, shaped per spec Section 4.2.2.

    `check` names what was inspected, `found` states the value or condition
    encountered, `prevents` names the concrete failure this guards against,
    and `remedy` is the exact next step -- the same four-part shape every
    other skill's failing check uses, so a `verify` finding needs no
    separate reading.
    """

    check: str
    found: str
    prevents: str
    remedy: str


def _unshell_quote(token: str) -> str:
    """The literal value a shell would see for an `export VAR=<token>` line.

    `render.shell_quote` wraps a value in single quotes and escapes an
    embedded quote as `'\\''`; `render.home_relative` instead wraps an
    under-`${HOME}` path in double quotes with no escaping, since a
    filesystem path contains nothing that needs it (`render_shell_env`
    writes exactly that double-quoted form into the active
    `REMOTE_SSH_KEY_PATH` line on a remote backend). The rendered header
    also tells the operator the file is read as-is and may be hand-edited,
    so a value with no quoting at all is equally in scope. Only a token that
    actually starts and ends with the same quote character has a layer to
    strip; every other shape, including a bare unquoted value, is returned
    unchanged, so a correct-but-unquoted or double-quoted value is never
    mangled into a false-positive finding.
    """
    if len(token) >= 2 and token[0] == token[-1] and token[0] in ("'", '"'):
        inner = token[1:-1]
        if token[0] == "'":
            return inner.replace("'\\''", "'")
        return inner
    return token


def _active_exports(shell_env_text: str) -> dict[str, str]:
    """Every uncommented `export VAR=value` line in `shell.env`, unquoted.

    Deliberately excludes commented-out lines: a variable no shell sources
    cannot disagree with anything, so the remote-docker block on a local
    backend and the untouched project-specific example line never
    contribute a value here.
    """
    return {
        match.group("name"): _unshell_quote(match.group("value"))
        for match in _ACTIVE_EXPORT_LINE.finditer(shell_env_text)
    }


def _active_configuration_text(relative: str, text: str) -> str:
    """The subset of `text` a placeholder scan actually needs to inspect.

    `shell.env` ships commented-out optional lines (the remote-docker block,
    the project-specific example) that `render` leaves untouched by design;
    a placeholder inside one of those is not reachable by any shell that
    sources the file, so only the active export lines are scanned. Neither
    JSON file has a comment syntax, so their whole text is in scope.
    """
    if relative != repo.SHELL_ENV:
        return text
    return "\n".join(match.group(0) for match in _ACTIVE_EXPORT_LINE.finditer(text))


def _completeness_finding(relative: str, exc: OSError | UnicodeDecodeError) -> Finding:
    """A private file that cannot be read: nothing else about it can be checked.

    Every later check needs this file's content, so a missing, unreadable or
    undecodable file is reported once, here, rather than assumed to be an
    intentionally empty file with nothing further to say about it.
    `UnicodeDecodeError` is not an `OSError` subclass, but a stray non-UTF-8
    byte from a hand edit (the rendered header explicitly invites one) is
    exactly as unreadable as a missing file, not a reason to let a traceback
    reach the operator.
    """
    return Finding(
        check=f"completeness: {relative} exists and is readable",
        found=f"{relative} could not be read: {exc}",
        prevents=(
            "every check that reads this file's content, and the container "
            "startup step that needs it"
        ),
        remedy=(
            f"Run /devcontainer:setup-local (or /devcontainer:setup-remote) to "
            f"write {relative}, or make init to see what remains."
        ),
    )


def _json_parse_finding(relative: str, exc: json.JSONDecodeError) -> Finding:
    """A private file that exists but is not valid JSON.

    Distinct from `_json_type_finding`'s file-parses-but-wrong-shape case, so
    the operator knows whether to fix syntax or content. Otherwise the
    container startup step that loads this file, and every consistency
    check that reads it, never runs against valid content.
    """
    return Finding(
        check=f"completeness: {relative} parses as JSON",
        found=f"{relative} does not parse: {exc}",
        prevents=(
            "the container startup step that loads this file, and every "
            "consistency check that reads it"
        ),
        remedy=(
            f"Fix the JSON syntax in {relative} at {exc}, or re-render it "
            "via /devcontainer:setup-local."
        ),
    )


def _json_type_finding(relative: str, document: Any) -> Finding:
    """A private JSON file that parses but is not a JSON object.

    Distinct from `_json_parse_finding`'s syntax-error case: this file is
    syntactically valid JSON, but every later check that reads it
    (`.get`/`in`/`.items()`) expects a mapping, so a wrongly-shaped document
    is reported here rather than left to crash the first check that expects
    an object. Otherwise the container startup step that loads this file,
    and every consistency check that reads it, either raises or silently
    reads nothing from a document with no keys to look up.
    """
    return Finding(
        check=f"completeness: {relative} is a JSON object",
        found=f"{relative} parses as {type(document).__name__}, not an object",
        prevents=(
            "the container startup step that loads this file, and every "
            "consistency check that reads it"
        ),
        remedy=(
            f"Replace {relative}'s contents with a JSON object, or re-render "
            "it via /devcontainer:setup-local."
        ),
    )


def _placeholder_findings(relative: str, text: str) -> list[Finding]:
    """Every distinct `<placeholder>` token left in `relative`'s active configuration.

    One finding per unique token, mirroring `make init`'s own `sort -u`
    report, rather than one finding per raw occurrence. Otherwise a value
    the operator never supplied reaches the running container as literal
    text.
    """
    scan_text = _active_configuration_text(relative, text)
    tokens = sorted(set(_PLACEHOLDER_PATTERN.findall(scan_text)))
    return [
        Finding(
            check=f"no placeholders: {relative}",
            found=f"{relative} still contains {token}",
            prevents=(
                "a value the operator never supplied reaches the running container as literal text"
            ),
            remedy=f"Replace {token} in {relative}, then re-run make init to confirm none remain.",
        )
        for token in tokens
    ]


def _bash_env_finding(shell_env_text: str, root: Path) -> list[Finding]:
    """`BASH_ENV` must equal `repo.container_workspace(root) + "/shell.env"`.

    An absent export line gets its own completeness finding rather than
    being defaulted to `''` and compared against the expected value: the
    default-masks-a-failure shape this module already rejects for
    `containerEnv` and `AWS_CONFIG_ENABLED` applies here too, and a
    consistency finding built from the default would assert BASH_ENV was
    set to a value shell.env never contained (AC-FUNC-010's "what was
    found" contract). Either way, every non-interactive shell in the
    container sources nothing.
    """
    active_exports = _active_exports(shell_env_text)
    expected_value = f"{repo.container_workspace(root)}/{repo.SHELL_ENV}"
    if "BASH_ENV" not in active_exports:
        return [
            Finding(
                check="completeness: shell.env sets BASH_ENV",
                found="shell.env has no active export line for BASH_ENV",
                prevents="every non-interactive shell in the container sources nothing",
                remedy=(
                    f"Set BASH_ENV to {expected_value!r} in shell.env, or "
                    "re-render via /devcontainer:setup-local."
                ),
            )
        ]
    found_value = active_exports["BASH_ENV"]
    if found_value == expected_value:
        return []
    return [
        Finding(
            check="consistency: BASH_ENV matches repo.container_workspace",
            found=f"shell.env sets BASH_ENV to {found_value!r}, expected {expected_value!r}",
            prevents="every non-interactive shell in the container sources nothing",
            remedy=(
                f"Set BASH_ENV to {expected_value!r} in shell.env, or "
                "re-render via /devcontainer:setup-local."
            ),
        )
    ]


def _identity_findings(
    shell_env_text: str, container_env_document: dict[str, Any]
) -> list[Finding]:
    """Every identity variable `shell.env` shares with `containerEnv` must agree.

    A document with no `containerEnv` key at all, or one whose value is not
    a JSON object, is reported as its own completeness finding rather than
    treated as (or indexed into as) an empty mapping to compare against.
    `render.render_devcontainer_env_json` refuses the missing-key shape with
    `RenderError`, but does not guard the present-and-not-an-object shape at
    all: that shape reaches its own `document["containerEnv"].update(...)`
    call and fails there with `AttributeError` instead. This check must
    cover both shapes explicitly, or a `containerEnv` the renderer would
    have rejected outright verifies clean, or worse, raises `AttributeError`
    out of this function when a later `.items()` call meets a value that was
    never a mapping. Once
    `containerEnv` exists and is an object, a shared variable with no active
    export line in shell.env at all also gets its own completeness finding
    rather than being defaulted to `''` and compared against containerEnv's
    value: the default-masks-a-failure shape already rejected above would
    otherwise let a variable dropped from the file every shell sources
    "agree" by coincidence whenever containerEnv's own value for it happens
    to be empty (reproduced for `HOST_PROXY_URL` on a `host_proxy=False`
    render), and would misreport a non-empty containerEnv value as if
    shell.env had explicitly set the variable to `''`. Otherwise, every
    shared identity variable with an active export line must agree with
    containerEnv's value, or the container's actual environment disagrees
    with the file every shell inside it sources.
    """
    if "containerEnv" not in container_env_document:
        return [
            Finding(
                check=f"completeness: {repo.DEVCONTAINER_ENV_JSON} has a containerEnv object",
                found=f"{repo.DEVCONTAINER_ENV_JSON} has no 'containerEnv' object",
                prevents=(
                    "every identity variable in shell.env from ever being compared, and "
                    "the container's environment from being populated at all"
                ),
                remedy=(
                    f"Re-render {repo.DEVCONTAINER_ENV_JSON} via /devcontainer:setup-local, "
                    "which refuses to produce one with no 'containerEnv' object."
                ),
            )
        ]
    container_env = container_env_document["containerEnv"]
    if not isinstance(container_env, dict):
        return [
            Finding(
                check=f"completeness: {repo.DEVCONTAINER_ENV_JSON}'s containerEnv is a JSON object",
                found=(
                    f"{repo.DEVCONTAINER_ENV_JSON}'s containerEnv parses as "
                    f"{type(container_env).__name__}, not an object"
                ),
                prevents=(
                    "every identity variable in shell.env from ever being compared, and "
                    "the container's environment from being populated at all"
                ),
                remedy=(
                    f"Replace {repo.DEVCONTAINER_ENV_JSON}'s containerEnv with a JSON object, "
                    "or re-render it via /devcontainer:setup-local."
                ),
            )
        ]
    shell_exports = _active_exports(shell_env_text)
    findings: list[Finding] = []
    for variable, container_value in container_env.items():
        if variable not in shell_exports:
            findings.append(
                Finding(
                    check=f"completeness: shell.env sets {variable}",
                    found=(
                        f"shell.env has no active export line for {variable}, but "
                        f"{repo.DEVCONTAINER_ENV_JSON}'s containerEnv sets it to "
                        f"{container_value!r}"
                    ),
                    prevents=(
                        "the container's actual environment disagrees with the file every "
                        "shell sources, and this check from ever comparing the two values"
                    ),
                    remedy=(
                        f"Add an export {variable}=... line to shell.env reconciled with "
                        f"{repo.DEVCONTAINER_ENV_JSON}, or re-render via "
                        "/devcontainer:setup-local."
                    ),
                )
            )
            continue
        found_value = shell_exports[variable]
        if found_value == container_value:
            continue
        findings.append(
            Finding(
                check=(
                    f"consistency: {variable} agrees between shell.env and "
                    f"{repo.DEVCONTAINER_ENV_JSON}"
                ),
                found=(
                    f"shell.env sets {variable} to {found_value!r}, "
                    f"containerEnv sets it to {container_value!r}"
                ),
                prevents=(
                    "the container's actual environment disagrees with the file every shell sources"
                ),
                remedy=(
                    f"Reconcile {variable} between shell.env and {repo.DEVCONTAINER_ENV_JSON}, "
                    "or re-render via /devcontainer:setup-local."
                ),
            )
        )
    return findings


def _aws_profile_map_findings(
    shell_env_text: str, aws_profile_map_document: dict[str, Any]
) -> list[Finding]:
    """`aws-profile-map.json`'s populated-or-empty state must agree with `AWS_CONFIG_ENABLED`.

    Populated exactly when it is `'true'`, empty exactly when it is
    `'false'` (spec Section 5.2): otherwise the container's AWS profile
    setup either has nothing to render, or silently ignores a populated map.
    `AWS_CONFIG_ENABLED` itself must have an active export line in
    shell.env: defaulting a missing variable to `'false'` would let it
    coincidentally agree with an empty map and hide the missing variable
    from every finding, the same default-masks-a-failure shape already
    rejected for `containerEnv`. A value that is neither `'true'` nor
    `'false'` gets the same treatment rather than being coerced to `False`
    by an `== 'true'` comparison: coercion would let a misconfigured flag
    (for example a stray `'True'`) silently read as disabled, agreeing by
    coincidence with an empty map, or, against a populated map, produce a
    mismatch finding that asserts a `'false'` value never actually present
    in shell.env.
    """
    active_exports = _active_exports(shell_env_text)
    if "AWS_CONFIG_ENABLED" not in active_exports:
        return [
            Finding(
                check="completeness: shell.env sets AWS_CONFIG_ENABLED",
                found="shell.env has no active export line for AWS_CONFIG_ENABLED",
                prevents=(
                    "every check and container startup step that needs to know whether "
                    "AWS profile support is enabled"
                ),
                remedy=(
                    "Set AWS_CONFIG_ENABLED to 'true' or 'false' in shell.env, or "
                    "re-render it via /devcontainer:setup-local."
                ),
            )
        ]
    raw_value = active_exports["AWS_CONFIG_ENABLED"]
    if raw_value not in ("true", "false"):
        return [
            Finding(
                check="completeness: shell.env's AWS_CONFIG_ENABLED is 'true' or 'false'",
                found=f"shell.env sets AWS_CONFIG_ENABLED to {raw_value!r}, not 'true' or 'false'",
                prevents=(
                    "every check and container startup step that needs to know whether "
                    "AWS profile support is enabled from reading a value it can trust"
                ),
                remedy=(
                    "Set AWS_CONFIG_ENABLED to 'true' or 'false' in shell.env, or "
                    "re-render it via /devcontainer:setup-local."
                ),
            )
        ]
    aws_config_enabled = raw_value == "true"
    is_populated = bool(aws_profile_map_document)
    if aws_config_enabled == is_populated:
        return []
    if aws_config_enabled:
        found = f"AWS_CONFIG_ENABLED is {raw_value!r} but {repo.AWS_PROFILE_MAP} is empty"
        prevents = "make build has no AWS profile to render into ~/.aws/config"
    else:
        found = f"AWS_CONFIG_ENABLED is {raw_value!r} but {repo.AWS_PROFILE_MAP} is populated"
        prevents = "a profile the container will never read stays silently unused"
    return [
        Finding(
            check="consistency: aws-profile-map.json agrees with AWS_CONFIG_ENABLED",
            found=found,
            prevents=prevents,
            remedy=(
                f"Set AWS_CONFIG_ENABLED to match {repo.AWS_PROFILE_MAP}'s contents in "
                "shell.env, or re-render via /devcontainer:setup-local."
            ),
        )
    ]


def verify_all(root: Path) -> list[Finding]:
    """Every completeness, placeholder and consistency finding for `root`.

    Returns every finding rather than the first (AC-FUNC-009), so one pass
    over the result corrects the whole configuration, the same way answer
    validation reports every failing field at once. A file that cannot be
    read, a JSON file that does not parse, or a JSON file that parses but is
    not an object, is reported once by the completeness check and skipped by
    every later check that needs its content: there is nothing further
    those checks could say about it, and none of them may assume the
    document they were handed is a mapping.
    """
    findings: list[Finding] = []
    texts: dict[str, str] = {}
    paths = repo.private_paths(root)
    for relative in repo.PRIVATE_FILES:
        try:
            texts[relative] = paths[relative].read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            findings.append(_completeness_finding(relative, exc))

    documents: dict[str, dict[str, Any]] = {}
    for relative in _JSON_FILES:
        text = texts.get(relative)
        if text is None:
            continue
        try:
            document = json.loads(text)
        except json.JSONDecodeError as exc:
            findings.append(_json_parse_finding(relative, exc))
            continue
        if not isinstance(document, dict):
            findings.append(_json_type_finding(relative, document))
            continue
        documents[relative] = document

    for relative, text in texts.items():
        findings.extend(_placeholder_findings(relative, text))

    if repo.SHELL_ENV in texts:
        findings.extend(_bash_env_finding(texts[repo.SHELL_ENV], root))

    if repo.SHELL_ENV in texts and repo.DEVCONTAINER_ENV_JSON in documents:
        findings.extend(
            _identity_findings(texts[repo.SHELL_ENV], documents[repo.DEVCONTAINER_ENV_JSON])
        )

    if repo.SHELL_ENV in texts and repo.AWS_PROFILE_MAP in documents:
        findings.extend(
            _aws_profile_map_findings(texts[repo.SHELL_ENV], documents[repo.AWS_PROFILE_MAP])
        )

    return findings
