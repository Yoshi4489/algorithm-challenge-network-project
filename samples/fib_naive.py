# The two-line recursive definition of Fibonacci — correct, and exponential.
#
# This is the headline demo for the `fib` problem, and the reason the problem exists. Every
# test case passes: fib(0)=0, fib(1)=1, the modulus is applied, and the recursion is a
# faithful transcription of the mathematical definition. A judge that only checked
# correctness would accept it.
#
# It is O(phi^n) ~ O(1.618^n) time, which the profiler classifies as O(2^n) — the coarsest
# exponential class in the table. Against a required_time of O(n) that is a violation, and
# the growth is so steep the profiler has no trouble seeing it: the declared ladder runs
# n=22..32, and each +2 steps multiplies the work by phi^2 ~ 2.6, so the largest point costs
# about 120x the smallest. No ambiguity, no `confidence: low`.
#
# Why it is this slow: solve(n) recomputes solve(n-2) twice, solve(n-3) three times, and so
# on down. The call tree has about 2*fib(n+1) nodes — at n=28 that is roughly a million
# calls to produce a number the iterative version reaches in 28 additions.
#
# Space is O(n) too: the recursion is n frames deep at its deepest, so it violates the
# O(1) space half of the contract as well. Time is the headline because it is reported first
# and because the difference is enormous rather than marginal.
#
# Expected verdict: 606 TIME_COMPLEXITY_VIOLATION (tests 8/8, inferred O(2^n) against a
# required O(n)).

MOD = 10007


def solve(n):
    if n < 2:
        return n % MOD
    # Reducing at each step keeps the arithmetic in machine words. It does not make the
    # algorithm any less exponential — the cost here is the number of calls, not their width.
    return (solve(n - 1) + solve(n - 2)) % MOD
