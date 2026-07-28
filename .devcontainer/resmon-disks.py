#!/usr/bin/env python3
"""Point the Resource Monitor extension at this container's real disks.

resmon filters by the *device* behind a filesystem, never by mount point, and
the device differs per host: the workspace is an EBS volume on the remote
engine and a bind mount from the laptop on a local one. Naming a device in
devcontainer.json would therefore be correct on exactly one machine and would
silently show nothing everywhere else, so the mounts to report are named here
and their devices are resolved at run time.

Run from postAttachCommand, not postCreate: VS Code writes the settings file
itself when the server starts, which is after postCreate, and would overwrite
anything put there earlier.

This never creates that file. VS Code seeds it with everything under
customizations.vscode.settings, and only when it does not already exist, so an
earlier version of this script creating it left a container with one key in it
and no terminal profiles, no editor settings, nothing. If the file is not there
yet this exits without touching anything and the next attach sets the drives.

Inputs (environment):
  RESMON_DISK_MOUNTS   space-separated mount points  (default: /workspaces /tmp)
  RESMON_SETTINGS      settings file to update
                       (default: ~/.vscode-server/data/Machine/settings.json)
"""

import json
import os
import subprocess
import sys

SETTING = "resmon.disk.drives"


def fail(message):
    sys.stderr.write("resmon-disks: %s\n" % message)
    raise SystemExit(1)


def device_for(mount):
    """The device backing a mount point, as df reports it and resmon matches it."""
    result = subprocess.run(
        ["df", "--output=source", mount],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        fail(
            "df could not read %s (exit %d): %s\n"
            "Set RESMON_DISK_MOUNTS to mount points that exist in this container."
            % (mount, result.returncode, result.stderr.strip() or "no output")
        )
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if len(lines) < 2:
        fail("df reported no device for %s" % mount)
    return lines[1]


def load(path):
    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    if not text.strip():
        return {}
    try:
        return json.loads(text)
    except ValueError as error:
        fail(
            "%s is not valid JSON (%s). It is written by VS Code; fix or remove "
            "it and reattach." % (path, error)
        )


def main():
    mounts = os.environ.get("RESMON_DISK_MOUNTS", "/workspaces /tmp").split()
    if not mounts:
        fail("RESMON_DISK_MOUNTS is set but empty, so there is nothing to report")

    default_settings = os.path.expanduser(
        "~/.vscode-server/data/Machine/settings.json"
    )
    path = os.environ.get("RESMON_SETTINGS", default_settings)

    # Creating this file is what must never happen: VS Code seeds it from
    # customizations.vscode.settings only when it is absent, so getting there
    # first leaves a container with no terminal profiles and no editor settings.
    if not os.path.exists(path):
        print(
            "resmon-disks: %s does not exist yet, so VS Code has not written its "
            "settings. Leaving it alone; the next attach sets the drives." % path
        )
        return

    devices = []
    for mount in mounts:
        device = device_for(mount)
        if device not in devices:
            devices.append(device)

    settings = load(path)
    if settings.get(SETTING) == devices:
        print("resmon-disks: %s already %s" % (SETTING, devices))
        return

    settings[SETTING] = devices
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(settings, handle, indent=4)
        handle.write("\n")
    print(
        "resmon-disks: %s = %s (from %s)"
        % (SETTING, devices, " ".join(mounts))
    )


if __name__ == "__main__":
    main()
