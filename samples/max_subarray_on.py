# Kadane's algorithm — O(n) time, O(1) space. The reference ACCEPT for max-subarray.
#
# One pass. `best_ending_here` is the largest sum of a subarray that ends at the current
# element: either we extend the previous such subarray, or we start fresh at this element,
# whichever is larger. `best` tracks the largest we have seen anywhere. Two scalars, so the
# auxiliary space is genuinely O(1) — this is the solution the O(1) space contract is for.
#
# Expected verdict: 600 ACCEPTED (time O(n), space O(1), both inside the contract).


def solve(nums):
    best = nums[0]
    best_ending_here = nums[0]
    for value in nums[1:]:
        best_ending_here = max(value, best_ending_here + value)
        best = max(best, best_ending_here)
    return best
