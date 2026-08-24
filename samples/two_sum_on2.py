# Nested-loop two-sum — O(n^2) time, O(1) space. Correct, and too slow.
#
# It ignores the fact that the input is sorted and checks every pair. Correct on every test,
# and the natural first thing to write.
#
# The generator makes this the worst case on purpose: the target is the sum of the last two
# elements, which is provably the unique answer and the maximum pair sum, so this
# left-to-right scan reaches it only on its final comparison — a full O(n^2) sweep with no
# early exit to rescue it.
#
# Expected verdict: 606 TIME_COMPLEXITY_VIOLATION (tests pass, inferred O(n^2) against a
# required O(n)).


def solve(nums, target):
    for i in range(len(nums)):
        for j in range(i + 1, len(nums)):
            if nums[i] + nums[j] == target:
                return [i, j]
    return []
