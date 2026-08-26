# Raises ZeroDivisionError while running — the 605 RUNTIME_ERROR demo.
#
# The solve() body is a plausible-looking averaging attempt at Kadane that divides by a
# counter which is zero on the very first iteration. It parses (so not 604), it imports
# nothing forbidden (so not 609), and it is not merely wrong-but-returning (which would be
# 601) — it *throws* while executing a test case. That is its own verdict, 605.
#
# Why the distinction matters, and why the judge keeps these apart:
#
#   * 604 COMPILE_ERROR  — the source will not parse. Nothing ran.
#   * 605 RUNTIME_ERROR  — it parsed and ran, then raised. This file.
#   * 601 WRONG_ANSWER   — it ran to completion and returned the wrong value.
#
# A player debugging "your code doesn't compile" when it actually crashed on input would be
# sent looking in entirely the wrong place, so run_tests() records the exception kind and
# the worker maps it to 605 rather than folding it into a generic failure.
#
# The crash is on the first element: `count` starts at 0 and the average is taken before it
# is ever incremented. Every test case that calls solve() with a non-empty list therefore
# raises on element one, which is all of them.
#
# Expected verdict: 605 RUNTIME_ERROR (ZeroDivisionError on the first test; not profiled).


def solve(nums):
    best = nums[0]
    running = 0
    count = 0
    for value in nums:
        # Bug: dividing by count, which is still 0 on the first pass. A ZeroDivisionError
        # is raised here before count is ever incremented below.
        running = (running + value) // count
        count += 1
        if running > best:
            best = running
    return best
