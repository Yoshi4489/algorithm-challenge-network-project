# Never returns — the 602 TIME_LIMIT_EXCEEDED demo, and the one that proves who holds the
# stopwatch.
#
# solve() enters a loop with no exit and no I/O. It cannot be interrupted from inside, it
# will not raise, and it will never produce a result. That is deliberate: 602 is the verdict
# for a submission that stops cooperating entirely, and the only way to reach it is for
# something *outside* the child process to end the run.
#
# What actually happens, step by step:
#
#   1. The parent (SubprocessBackend) starts the child with a deadline of
#      time_limit_ms + KILL_GRACE_S — 2 s + 10 s = 12 s for max-subarray.
#   2. The child wedges on the first test case and stops reading or writing anything.
#   3. communicate() raises TimeoutExpired, and the parent kills the whole process *group*,
#      not just the direct child — a submission that spawned helpers does not get to orphan
#      them past the deadline.
#   4. No __CDAP_RESULT__ line ever arrives, and timed_out=True outranks anything the child
#      might have managed to say, so outcome_hint() returns time_limit_exceeded -> 602.
#
# The design point worth saying aloud: the timeout is enforced by the parent, never by the
# child. A child in a busy loop cannot be trusted to time itself out — it is not running any
# code of ours to check a clock. This is also why the demo genuinely takes ~12 seconds; the
# wait is the mechanism working, not the program hanging.
#
# Note it is a counting loop rather than `while True: pass` — the accumulator keeps CPython
# from optimising anything away and makes the CPU cost visible in a task manager, which is a
# nice touch on camera. Under `subprocess` this burns one core for the full 12 s, which is
# exactly the limitation the threat model names: CPU exhaustion is bounded only by the
# wall-clock kill.
#
# Expected verdict: 602 TIME_LIMIT_EXCEEDED (killed by the parent after ~12 s; no result
# line, no profiling).


def solve(nums):
    spins = 0
    while True:
        spins += 1
    return spins        # unreachable, and that is the whole point
