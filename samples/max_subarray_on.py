# Kadane's algorithm — O(n) time, O(1) space. The reference ACCEPT for max-subarray.
#
# One pass. `best_ending_here` is the largest sum of a subarray that ends at the current
# element: either we extend the previous such subarray, or we start fresh at this element,
# whichever is larger. `best` tracks the largest we have seen anywhere. Two scalars, so the
# auxiliary space is genuinely O(1) — this is the solution the O(1) space contract is for.
#
# Note the loop iterates by index rather than the more idiomatic `for value in nums[1:]`.
# That slice looks free and is not: it copies n-1 elements into a fresh list, which is O(n)
# *auxiliary* space. The runner measures auxiliary space by snapshotting after the input is
# built and resetting the peak, so the input itself is discounted but a slice of it is not —
# it would regress as O(n), and this file would earn a false 607 SPACE_COMPLEXITY_VIOLATION
# while claiming in its own header to be O(1). Indexing keeps the claim true.
#
# Expected verdict: 600 ACCEPTED (time O(n), space O(1), both inside the contract).


def solve(nums):
    best = nums[0]
    best_ending_here = nums[0]
    for i in range(1, len(nums)):
        value = nums[i]
        best_ending_here = max(value, best_ending_here + value)
        best = max(best, best_ending_here)
    return best
