# Two-pointer scan — O(n) time, O(1) space. The reference ACCEPT for two-sum-sorted.
#
# The list is sorted, which is the fact that makes one pass enough. Start with the widest
# pair. If their sum is too small, the only way to increase it is to move the left pointer
# right; if too large, move the right pointer left. Each step discards one element for good,
# so the loop runs at most n times, and the two indices are the only state — O(1) space.
#
# The generator's target is the sum of the last two elements, which drives this solution the
# long way round: every sum it sees is too small, so `lo` advances all n-2 steps. That is
# deliberate — a worst case is the only honest thing to measure.
#
# Expected verdict: 600 ACCEPTED (time O(n), space O(1)).


def solve(nums, target):
    lo = 0
    hi = len(nums) - 1
    while lo < hi:
        total = nums[lo] + nums[hi]
        if total == target:
            return [lo, hi]
        if total < target:
            lo += 1
        else:
            hi -= 1
    return []
