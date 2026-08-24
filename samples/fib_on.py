# Iterative DP — O(n) time, O(1) space. The reference ACCEPT for fib.
#
# Two rolling variables, n additions. No table, no recursion, no memo dict — which is why
# the auxiliary space is O(1) rather than O(n): a memoised recursive solution is also O(n)
# time but keeps a dict of n entries and would violate the space half of the contract.
#
# The modulus is applied on every step, not once at the end. That is not just for speed:
# reducing as you go keeps every intermediate value inside a machine word, so each addition
# costs the same regardless of n. Skip it and the values grow to O(n) digits, each addition
# becomes O(n), and this O(n) algorithm measures as O(n^2) — see cdap/problems.py's note on
# FIB_MODULUS for why the problem is stated this way.
#
# Expected verdict: 600 ACCEPTED (time O(n), space O(1)).

MOD = 10007


def solve(n):
    a, b = 0, 1
    for _ in range(n):
        a, b = b, (a + b) % MOD
    return a % MOD
