# Tries to spawn processes without bound — the 609 demo for a fork bomb, and the exhibit
# that justifies --pids-limit.
#
# The third and most dangerous of the security samples. evil_socket.py reaches the network
# and evil_open.py reaches the disk; this one attacks the machine itself by multiplying
# processes, and it is the reason the process-group kill and the container's pid cap both
# exist.
#
# With the guard ON: neither `subprocess` nor `sys` is on the import whitelist, so
# check_source refuses the source — 609 SANDBOX_VIOLATION, reported with both offending lines,
# before a single process is spawned. Catching a fork bomb *statically*, before it runs, is
# the best possible outcome; once it is running it is already a problem.
#
# With the guard OFF (--no-ast-guard), the difference between the backends is stark:
#   * under `subprocess` the spawns SUCCEED. The only backstop is SubprocessBackend's
#     wall-clock kill, which then has to tear down the entire process *group* — this is
#     exactly why _kill_tree uses killpg (POSIX) / taskkill /T (Windows) instead of a plain
#     proc.kill() that would orphan the children. The demo shows the kill working, but also
#     shows that the damage window was real: processes did get created.
#   * under `docker` with `--pids-limit 8` the spawns hit a hard cgroup ceiling almost
#     immediately and fail. The kernel refuses the complete burst. No cooperation
#     from the judge required — the limit is enforced below the code.
#
# That contrast is the strongest single moment in the security experiment: the subprocess
# backend *survives* the fork bomb (the group kill cleans it up) but does not *prevent* it,
# while Docker prevents it outright. Survive-versus-prevent is the whole thesis about which
# layer is the boundary.
#
# This sample is deliberately BOUNDED — a fixed, small number of short-lived children, not an
# unbounded `while True: fork()`. It has to make the point on camera without risking the
# development machine, and a capped burst demonstrates "it can spawn at all" (which is the
# security claim) without an actual denial-of-service. Keeping it bounded is not softening the
# demo; the guard-on verdict and the docker pid-cap failure are identical either way.
#
# Expected verdict: 609 SANDBOX_VIOLATION (guard on). With --no-ast-guard: a bounded burst of
# spawns succeeds under subprocess (then the group kill reaps them), and is blocked by
# --pids-limit under docker.

import subprocess
import sys

#: Small and finite on purpose — enough to prove spawning works, not enough to wedge the
#: dev box. A real fork bomb would loop without this bound; the security claim does not need
#: it to.
BURST = 16

spawned = 0
try:
    for _ in range(BURST):
        # A child that stays alive for two seconds, long enough for the burst to overlap and
        # hit Docker's pid ceiling. It does no work and is not waited on individually.
        subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(2)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        spawned += 1
    ESCAPED = spawned == BURST  # Docker's pids cap prevents the complete burst
except OSError:
    # Under docker --pids-limit this is where the kernel says no.
    ESCAPED = False


def solve(nums):
    # Throwaway body so the file loads as a submission with the guard off.
    return max(nums)
