#!/usr/bin/env python3
"""Point the Resource Monitor extension at this container's real disks.

resmon filters by the *device* behind a filesystem, never by mount point, and
the device differs per host: the workspace is an EBS volume on the remote
engine and a bind mount from the laptop on a local one. Naming a device in
devcontainer.json would therefore be correct on exactly one machine and would
silently show nothing everywhere else, so the mounts to report are named here
and their devices are resolved at run time.

Which mount holds the workspace differs by engine too. On the remote engine the
checkout is a volume mounted at /workspaces, so that path is the filesystem. On
a local engine only the repository below it is bind-mounted, and /workspaces
itself is the container's own overlay, so reporting it would name the image
layer rather than the disk the code is on. Unset, the mount is therefore the
shallowest one at or under /workspaces, which is /workspaces on one engine and
/workspaces/<repo> on the other.

A source is reportable when it names one filesystem. That is not the same as
naming a device file: a bind mount from a laptop reports a driver name (macOS
virtiofs reports 'mac'), which resmon matches perfectly well, while a tmpfs
reports 'none' or 'tmpfs' for several unrelated filesystems at once and would
show up unlabelled or matching all of them. Sources are therefore checked by
whether every mount point sharing the name is the same filesystem (st_dev),
which accepts a device bind-mounted repeatedly, as Docker does with /etc/hosts.

Run from postAttachCommand, not postCreate: VS Code writes the settings file
itself when the server starts, which is after postCreate, and would overwrite
anything put there earlier.

This never creates that file. VS Code seeds it with everything under
customizations.vscode.settings, and only when it does not already exist, so an
earlier version of this script creating it left a container with one key in it
and no terminal profiles, no editor settings, nothing. If the file is not there
yet this exits without touching anything and the next attach sets the drives.

Inputs (environment):
  RESMON_DISK_MOUNTS   space-separated mount points
                       (default: the mount holding the workspace)
  RESMON_WORKSPACES    where workspaces are mounted, searched for that default
                       (default: /workspaces)
  RESMON_SETTINGS      settings file to update
                       (default: ~/.vscode-server/data/Machine/settings.json)
"""

import json
import os
import subprocess
import sys

SETTING = "resmon.disk.drives"
MOUNTS = "/proc/self/mounts"

def fail(message):
    sys.stderr.write("resmon-disks: %s\n" % message)
    raise SystemExit(1)

def unescape(field):
    """A mount field as the kernel writes it, with its octal escapes resolved."""
    for escape, character in (
        ("\\040", " "),
        ("\\011", "\t"),
        ("\\012", "\n"),
        ("\\134", "\\"),
    ):
        field = field.replace(escape, character)
    return field

def mount_table():
    """(source, mount point) for every mounted filesystem, in kernel order."""
    try:
        with open(MOUNTS, encoding="utf-8") as handle:
            lines = handle.readlines()
    except OSError as error:
        fail("%s could not be read (%s), so no mount can be resolved" % (MOUNTS, error))
    table = []
    for line in lines:
        fields = line.split()
        if len(fields) >= 2:
            table.append((unescape(fields[0]), unescape(fields[1])))
    return table

def workspace_mount(table, root):
    """The mount holding the workspace: the shallowest one at or under root."""
    under = [point for _, point in table if point == root or point.startswith(root + "/")]
    if not under:
        fail(
            "nothing is mounted at or under %s, so the filesystem holding the "
            "workspace cannot be resolved. Name the mount points to report in "
            "RESMON_DISK_MOUNTS, or point RESMON_WORKSPACES at where this "
            "container mounts them." % root
        )
    depth = min(point.count("/") for point in under)
    shallowest = sorted({point for point in under if point.count("/") == depth})
    if len(shallowest) > 1:
        fail(
            "%s are all mounted directly under %s, so which one holds the "
            "workspace is ambiguous. Name it in RESMON_DISK_MOUNTS."
            % (", ".join(shallowest), root)
        )
    return shallowest[0]

def filesystem_of(point):
    try:
        return os.stat(point).st_dev
    except OSError:
        return None

def check_reportable(mount, device, table):
    """Reject a source resmon cannot match to exactly one filesystem."""
    sharing = sorted({point for source, point in table if source == device})
    filesystems = {filesystem_of(point) for point in sharing}
    filesystems.discard(None)
    if len(filesystems) > 1:
        fail(
            "%s is backed by %r, which %d unrelated filesystems report as their "
            "source (%s): resmon matches entries by that string, so this one "
            "would show up matching all of them at once.\n"
            "Name mount points whose source identifies one filesystem in "
            "RESMON_DISK_MOUNTS."
            % (mount, device, len(filesystems), ", ".join(sharing))
        )

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
    table = mount_table()
    named = os.environ.get("RESMON_DISK_MOUNTS")
    if named is None:
        mounts = [workspace_mount(table, os.environ.get("RESMON_WORKSPACES", "/workspaces"))]
    else:
        mounts = named.split()
    if not mounts:
        fail("RESMON_DISK_MOUNTS is set but empty, so there is nothing to report")

    default_settings = os.path.expanduser(
        "~/.vscode-server/data/Machine/settings.json"
    )
    path = os.environ.get("RESMON_SETTINGS", default_settings)

    if not os.path.exists(path):
        print(
            "resmon-disks: %s does not exist yet, so VS Code has not written its "
            "settings. Leaving it alone; the next attach sets the drives." % path
        )
        return

    devices = []
    for mount in mounts:
        device = device_for(mount)
        check_reportable(mount, device, table)
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
