# Set-based duplicate detection — O(n) time, O(n) space. The reference ACCEPT.
#
# One pass, one set. Each value is checked for membership (average O(1)) and then added.
# The space contract for this problem is O(n) precisely because this is the intended
# solution: you cannot get O(n) time here without paying O(n) memory, and the contract
# says so instead of demanding something impossible.
#
# Written as an explicit loop rather than `len(set(nums)) != len(nums)` on purpose. The
# one-liner is shorter but does all its work inside C builtins, which makes it invisible to
# Method B — that is exactly the effect has_duplicate_onlogn.py exists to demonstrate, and
# the reference solution should not muddy it.
#
# Expected verdict: 600 ACCEPTED (time O(n), space O(n), both inside the contract).


def solve(nums):
    seen = set()
    for value in nums:
        if value in seen:
            return True
        seen.add(value)
    return False
