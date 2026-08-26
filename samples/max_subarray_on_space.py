# Correct, O(n) time — and O(n) auxiliary space where the contract demands O(1). Verdict 607.
#
# This is the sample that separates the two halves of a contract. max-subarray requires
# `required_time: O(n)` AND `required_space: O(1)`. This solution nails the time half — it is
# a single left-to-right pass, genuinely linear — but it builds a full dp[] array of length n
# to do it, so its auxiliary space grows with the input. Time O(n), space O(n). The time
# contract is satisfied; the space contract is violated; the verdict is 607
# SPACE_COMPLEXITY_VIOLATION, not 606.
#
# Why it is worth a distinct sample. It would be easy to think "fast enough" is the whole
# game, the way LeetCode largely treats it. CDAP measures both axes, and this file is the
# proof that they are independent: a solution can be optimal in time and still fail on space.
# Kadane (samples/max_subarray_on.py) computes the exact same answer with two scalars — best
# and running — which is the O(1) the contract wants. The only difference between accept and
# 607 here is that this version *remembers* every prefix result it never needed to keep.
#
# How the profiler sees it. Method A/B measure time and land on O(n): accept on that axis.
# tracemalloc measures auxiliary space: it snapshots after the input list is built, resets
# the peak, runs solve(), and reads the peak — so the input's own O(n) footprint is
# discounted and what remains is the dp[] array this solution allocated. That regresses as
# O(n), rank(O(n)) > rank(O(1)), and the space contract fails.
#
# The dp recurrence is the textbook one: dp[i] is the best subarray sum *ending at i*, which
# is either nums[i] alone or nums[i] extending the best run ending at i-1. The answer is
# max(dp). Correct, clear — and needlessly O(n) in space, which is exactly the point.
#
# Expected verdict: 607 SPACE_COMPLEXITY_VIOLATION (tests 10/10, time O(n) accepted, space
# inferred O(n) against a required O(1)).


def solve(nums):
    dp = [0] * len(nums)            # the O(n) auxiliary array that violates the contract
    dp[0] = nums[0]
    best = dp[0]
    for i in range(1, len(nums)):
        # Extend the best run ending at i-1, or start fresh at nums[i] — whichever is larger.
        dp[i] = max(nums[i], dp[i - 1] + nums[i])
        if dp[i] > best:
            best = dp[i]
    return best
