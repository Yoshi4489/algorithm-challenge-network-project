# Tries to read a file off the host — the 609 SANDBOX_VIOLATION demo for filesystem access.
#
# The second of the three security exhibits. Where evil_socket.py reaches for the network,
# this one reaches for the filesystem, and it trips the guard two different ways at once —
# which is a nice thing to show, because the guard is built to catch a category, not a single
# spelling:
#
#   1. `open(...)` is on the guard's forbidden-calls list. A submission has no honest reason
#      to open a file — the input arrives as function arguments — so the bare `open` builtin
#      is refused.
#   2. `import os` is not on the import whitelist, so os.listdir / os.environ are refused too.
#
# With the guard ON either one alone is enough for 609; the scan reports both (the walker
# collects every violation in one pass), so the log shows the whole story rather than the
# first thing it tripped over.
#
# With the guard OFF (--no-ast-guard) the file actually tries to exfiltrate:
#   * under `subprocess` it CAN read the host filesystem — it runs as your user, with your
#     rights, in a throwaway cwd but with the whole disk reachable by absolute path. The read
#     succeeds. That is the sobering part of the demo: nothing but a bypassable text filter
#     stood between a submission and your files.
#   * under `docker` the container is `--read-only` with only the repo mounted `:ro` and a
#     small tmpfs, so a write fails and a read reaches only what was deliberately mounted.
#
# Reading a well-known path keeps the demo deterministic across machines. On this Windows box
# the Python executable's own directory always exists; listing it proves filesystem reach
# without depending on any particular file being present.
#
# Expected verdict: 609 SANDBOX_VIOLATION (guard on; two violations reported — the open()
# call and the os import). With --no-ast-guard: reads the host FS under subprocess, blocked
# or confined under docker.

import os

# Enumerate a directory that exists on any install, and try to read a file's bytes. The
# listing alone already demonstrates filesystem reach; the open() is the belt-and-suspenders
# escalation and the construct the guard names explicitly.
try:
    entries = os.listdir(os.path.dirname(os.__file__))   # the stdlib directory
    sample = entries[0] if entries else ""
    with open(os.path.join(os.path.dirname(os.__file__), sample), "rb") as handle:
        handle.read(16)
    ESCAPED = True          # reached only when the host filesystem was readable
except OSError:
    ESCAPED = False         # confined — the outcome under a locked-down container


def solve(nums):
    # Throwaway body, present so the file resembles a real submission with the guard off.
    return max(nums)
