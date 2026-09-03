# Documents deleted at cutover

`tests/test_deleted_documents.py` reads this file and fails if any path below
is tracked again, exists on disk, or is referenced anywhere in the repository.
Each line under the list is one repo-relative path.

These were agent prompts: a human pasted them into an agent, which then set up
the machine. The `/devcontainer:setup-local` and `/devcontainer:setup-remote`
skills do that work now, and two sources of truth for the same setup drift --
which is the condition this sweep exists to prevent, not merely to record.

- docs/mac-setup-prompt.md
- docs/ec2-mdresden-setup-prompt.md
