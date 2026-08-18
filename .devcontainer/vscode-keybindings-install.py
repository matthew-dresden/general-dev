#!/usr/bin/env python3

"""Apply this repository's VS Code keybindings to the editor on this machine.

Keybindings are resolved by the VS Code window, which runs on the developer's
machine even when the workspace is a dev container, so devcontainer.json cannot
carry them the way it carries settings and extensions. Running Claude Code's
/terminal-setup from a container terminal writes into the container's home
directory, where no VS Code process ever reads it. This runs on the host
instead, and is the only piece of the container's terminal behaviour that has
to.
"""

import json
import os
import platform
import re
import sys

FULL_LINE_COMMENT = re.compile(r"^\s*//.*$", re.MULTILINE)

CONTAINER_MARKERS = ("REMOTE_CONTAINERS", "DEVCONTAINER", "CODESPACES")

USER_DIR_TEMPLATES = {
    "Darwin": ("~", "Library", "Application Support", "{app}", "User"),
    "Linux": ("~", ".config", "{app}", "User"),
}

MATCH_ON = ("key", "command", "when")


def fail(message):
    sys.stderr.write("vscode-keybindings-install: %s\n" % message)
    raise SystemExit(1)


def refuse_inside_container():
    present = [name for name in CONTAINER_MARKERS if os.environ.get(name)]
    if not present:
        return
    fail(
        "%s is set, so this is running inside the dev container. VS Code "
        "resolves keybindings in the window, not in the container, so a file "
        "written here would never be read. Run 'make keybindings' on the "
        "machine running VS Code." % ", ".join(present)
    )


def user_directory():
    override = os.environ.get("VSCODE_USER_DIR")
    if override:
        return os.path.expanduser(override)

    app = os.environ.get("VSCODE_APP_DIRNAME", "Code")
    system = platform.system()

    if system == "Windows":
        appdata = os.environ.get("APPDATA")
        if not appdata:
            fail("APPDATA is not set, so the VS Code user directory is unknown")
        return os.path.join(appdata, app, "User")

    template = USER_DIR_TEMPLATES.get(system)
    if template is None:
        fail(
            "no VS Code user directory is known for platform '%s'. Set "
            "VSCODE_USER_DIR to the directory holding keybindings.json."
            % system
        )
    return os.path.expanduser(os.path.join(*template).format(app=app))


def load_desired(path):
    if not os.path.exists(path):
        fail("%s does not exist, so there are no keybindings to apply" % path)
    with open(path, encoding="utf-8") as handle:
        try:
            desired = json.load(handle)
        except ValueError as error:
            fail("%s is not valid JSON (%s)" % (path, error))
    if not isinstance(desired, list) or not desired:
        fail("%s must be a non-empty array of keybindings" % path)
    for binding in desired:
        missing = [field for field in MATCH_ON if field not in binding]
        if missing:
            fail(
                "%s has a binding missing %s, which is what identifies it"
                % (path, " and ".join(missing))
            )
    return desired


def load_current(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    if not text.strip():
        return []
    try:
        current = json.loads(FULL_LINE_COMMENT.sub("", text))
    except ValueError as error:
        fail(
            "%s is not valid JSON once whole-line // comments are removed "
            "(%s). Fix it in VS Code, then run this again." % (path, error)
        )
    if not isinstance(current, list):
        fail("%s must contain an array of keybindings" % path)
    return current


def identity(binding):
    return tuple(binding.get(field) for field in MATCH_ON)


def back_up(path):
    backup = "%s.pre-devcontainer.bak" % path
    if os.path.exists(backup):
        return backup
    with open(path, encoding="utf-8") as source:
        contents = source.read()
    with open(backup, "w", encoding="utf-8") as handle:
        handle.write(contents)
    return backup


def main():
    refuse_inside_container()

    source = os.environ.get(
        "VSCODE_KEYBINDINGS",
        os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "vscode-keybindings.json"),
    )
    desired = load_desired(source)

    directory = user_directory()
    if not os.path.isdir(directory):
        fail(
            "%s does not exist, so VS Code is not installed for this user "
            "here. Set VSCODE_USER_DIR if it keeps its configuration "
            "elsewhere (VS Code Insiders, Cursor, a portable install)."
            % directory
        )
    path = os.path.join(directory, "keybindings.json")

    current = load_current(path)
    existing = {identity(binding): binding for binding in current}

    added = []
    for binding in desired:
        held = existing.get(identity(binding))
        if held is None:
            added.append(binding)
            continue
        if held.get("args") != binding.get("args"):
            fail(
                "%s already binds %s to %s with different arguments (%s, "
                "wanted %s). Remove that binding, then run this again."
                % (path, binding["key"], binding["command"],
                   json.dumps(held.get("args")),
                   json.dumps(binding.get("args")))
            )

    if not added:
        print(
            "vscode-keybindings-install: all %d binding(s) from %s are "
            "already in %s" % (len(desired), source, path)
        )
        return

    note = ""
    if os.path.exists(path):
        note = ", previous file kept at %s" % back_up(path)

    with open(path, "w", encoding="utf-8") as handle:
        json.dump(current + added, handle, indent=4)
        handle.write("\n")

    print(
        "vscode-keybindings-install: added %d binding(s) to %s%s: %s"
        % (len(added), path, note,
           ", ".join(binding["key"] for binding in added))
    )
    print(
        "vscode-keybindings-install: comments are dropped when the file is "
        "rewritten. Reload the VS Code window to pick the bindings up."
    )


if __name__ == "__main__":
    main()
