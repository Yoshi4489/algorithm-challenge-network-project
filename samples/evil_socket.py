# Tries to open an outbound network connection — the 609 SANDBOX_VIOLATION demo, and one of
# the three exhibits for the security experiment.
#
# This file wears two hats:
#
#   1. Normal judging (guard ON). `import socket` is not on the AST guard's import
#      whitelist, so check_source refuses the source before a line of it runs. Verdict:
#      609 SANDBOX_VIOLATION, reported as "line N: import of 'socket' is not allowed". The
#      escape never gets the chance to happen — which is the one thing a *static* check can
#      offer that a runtime one cannot.
#
#   2. The security experiment (guard OFF, --no-ast-guard). experiments/backend_overhead.py
#      runs this exact file with the guard disabled under BOTH backends to show which layer
#      is the real boundary:
#        * under `subprocess` the connect SUCCEEDS — nothing but the guard was stopping it,
#          and the guard is off. The escape works.
#        * under `docker` with --network none the connect FAILS at the kernel — there is no
#          network namespace to reach, guard or no guard. The escape is blocked.
#      That side-by-side is the report's proof that the AST guard is defence-in-depth and the
#      *container* is the boundary. It is a far stronger claim than asserting it in prose.
#
# Do not delete this file or the --no-ast-guard flag: CLAUDE.md's "do not fix" list and the
# experiment both depend on this staying runnable with the guard off.
#
# The hostile act is at module top level, so it runs during exec() — i.e. the moment the
# solution is loaded, before any test is called. A trivial solve() is defined too so that
# with the guard off the file still looks like an ordinary submission.
#
# Expected verdict: 609 SANDBOX_VIOLATION (guard on). With --no-ast-guard: connects under
# subprocess, fails under docker.

import socket

# Attempt to phone home. 8.8.8.8:53 (Google public DNS, TCP) is a stand-in for "any host on
# the internet"; the short timeout keeps the demo snappy whether it succeeds or is refused.
# The point is not the destination — it is that the code can create a socket and reach the
# network at all.
try:
    connection = socket.create_connection(("8.8.8.8", 53), timeout=2.0)
    connection.close()
    ESCAPED = True          # reached only when the network namespace was accessible
except OSError:
    ESCAPED = False         # blocked — the outcome under docker --network none


def solve(nums):
    # A throwaway body so that, with the guard off, the runner finds an entry point. The
    # verdict of interest was already decided above, at import time.
    return max(nums)
