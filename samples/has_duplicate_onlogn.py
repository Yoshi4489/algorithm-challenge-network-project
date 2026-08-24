# Sort-based duplicate detection — O(n log n) time. THE Method B blind-spot exhibit.
#
# DO NOT "FIX" THIS FILE. It is not a mistake; it is the most interesting result in the
# report, and CLAUDE.md and the confusion-matrix experiment both depend on it staying
# exactly as it is.
#
# What it does: sort the values, then scan for two equal neighbours. Sorting is O(n log n),
# so this solution is genuinely O(n log n) time — one class slower than the set-based O(n)
# solution, and against a required_time of O(n) it should be rejected.
#
# The two measurement methods disagree about it, and that disagreement is the finding:
#
#   * Method A (wall-clock regression) times the whole call. Sorting's O(n log n) shows up
#     in the clock, so Method A infers O(n log n) — or, honestly, "O(n) or O(n log n), low
#     confidence", because the two are inseparable at these input sizes (README, Method A
#     limitations). Either way it does NOT mistake this for the accepted O(n) solution.
#
#   * Method B (opcode counting) counts only Python-level bytecode. list.sort() is
#     implemented in C, so the entire sort — the dominant cost — registers as a SINGLE
#     CALL opcode. What Method B can see is the O(n) neighbour-scan loop, so it infers
#     ~O(n) and would wrongly call this ACCEPT.
#
# That is the lesson: a purely opcode-based profiler is blind to work done inside builtins,
# and would accept a solution that violates its contract. Method A is the authoritative one
# because the contract is about real time. experiments/confusion_matrix.py ASSERTS Method B
# gets this wrong rather than hiding it.
#
# The generator feeds shuffled, duplicate-free input, so Timsort cannot finish early on a
# pre-sorted run and the O(n log n) cost is actually paid. See cdap/problems.py.
#
# Expected verdict: 606 TIME_COMPLEXITY_VIOLATION by Method A (the authority);
# Method B reports ~O(n) and is recorded as the disagreement.


def solve(nums):
    ordered = sorted(nums)          # O(n log n), and entirely inside a C builtin
    for i in range(len(ordered) - 1):
        if ordered[i] == ordered[i + 1]:
            return True
    return False
