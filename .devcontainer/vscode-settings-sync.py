#!/usr/bin/env python3

import glob
import json
import os
import re
import sys

FULL_LINE_COMMENT = re.compile(r"^\s*//.*$", re.MULTILINE)


def fail(message):
    sys.stderr.write("vscode-settings-sync: %s\n" % message)
    raise SystemExit(1)


def load(path, strip_comments=False):
    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    if not text.strip():
        return {}
    if strip_comments:
        text = FULL_LINE_COMMENT.sub("", text)
    try:
        return json.loads(text)
    except ValueError as error:
        fail("%s is not valid JSON (%s)" % (path, error))


def devcontainer_config(root):
    pattern = os.path.join(root, "*", ".devcontainer", "devcontainer.json")
    matches = sorted(glob.glob(pattern))
    if len(matches) != 1:
        fail(
            "expected exactly one configuration at %s, found %d: %s\n"
            "Set DEVCONTAINER_CONFIG to the file to read, or "
            "DEVCONTAINER_WORKSPACES_ROOT to the directory holding one workspace."
            % (pattern, len(matches), ", ".join(matches) or "none")
        )
    return matches[0]


def main():
    root = os.environ.get("DEVCONTAINER_WORKSPACES_ROOT", "/workspaces")
    config = os.environ.get("DEVCONTAINER_CONFIG") or devcontainer_config(root)
    default_settings = os.path.expanduser(
        "~/.vscode-server/data/Machine/settings.json"
    )
    path = os.environ.get("VSCODE_MACHINE_SETTINGS", default_settings)

    if not os.path.exists(path):
        print(
            "vscode-settings-sync: %s does not exist yet, so VS Code has not "
            "written its settings. Leaving it alone; it seeds them from %s "
            "itself." % (path, config)
        )
        return

    desired = (
        load(config, strip_comments=True)
        .get("customizations", {})
        .get("vscode", {})
        .get("settings", {})
    )
    if not desired:
        print("vscode-settings-sync: %s declares no settings to apply" % config)
        return

    current = load(path)
    changed = {
        key: value
        for key, value in desired.items()
        if key not in current or current[key] != value
    }
    if not changed:
        print(
            "vscode-settings-sync: all %d setting(s) from %s already applied"
            % (len(desired), config)
        )
        return

    current.update(changed)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(current, handle, indent=4)
        handle.write("\n")
    print(
        "vscode-settings-sync: applied %d changed setting(s) from %s: %s"
        % (len(changed), config, ", ".join(sorted(changed)))
    )


if __name__ == "__main__":
    main()
