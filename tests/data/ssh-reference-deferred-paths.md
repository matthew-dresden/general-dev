# Paths allowed to name a removed SSH identifier

`tests/test_ssh_removal.py` fails when any tracked file mentions one of the
identifiers in `removed-ssh-tokens.txt`, except the paths listed here. Each
line is one repo-relative path.

Every deferred entry has now been cleared. What remains is permanent and small:
two test modules whose job is to assert that a removed identifier is absent.
A test cannot check that a string is gone without naming the string, so these
will always contain it, and excluding them is not a carve-out for unfinished
work but a consequence of what they test.

The stale check still applies to them: if either stops asserting the absence,
its entry fails and must be removed, so this list cannot quietly outlive its
reason.

## Assert the absence, and must name it to do so

- tests/test_docs_environment_files.py
- tests/test_makefile_contract.py
