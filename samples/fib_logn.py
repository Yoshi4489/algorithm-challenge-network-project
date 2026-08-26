# Fast doubling — O(log n) time, O(1) space. Better than the contract, and unclassifiable.
#
# This sample exists to demonstrate 611 INDETERMINATE_COMPLEXITY, and it does so honestly:
# it is not broken, not a stub, and not cheating. It passes every test and it is *better*
# than the O(n) the contract asks for. The profiler still refuses to classify it, and
# refusing is the correct answer.
#
# Why the profiler gives up. Method A needs the measured time to rise above its noise floor
# (5 ms) at enough sizes to fit a slope through. This solution consumes one bit of n per
# iteration, so n = 1,048,576 — the problem's size_cap — costs 20 iterations of small-int
# arithmetic: a few microseconds, indistinguishable from timer jitter. The measurement loop
# doubles the ladder past the declared n=18..28 and keeps doubling, and every point is still
# noise when it reaches the cap. With fewer than MIN_FIT_POINTS usable points it reports
# `measurable: false`, and the judge answers 611 rather than fitting six values of jitter and
# announcing O(1) with a straight face.
#
# So 611 is not a rejection. It says: this solution is too fast for the instrument. The
# player is told exactly that, and the report names it as a real limitation of empirical
# complexity inference — the method has a floor, and a good enough algorithm falls through it.
#
# How the algorithm works, since it is less familiar than the rest of the samples. Two
# identities let you jump from F(k) to F(2k) in one step:
#
#     F(2k)   = F(k) * (2*F(k+1) - F(k))
#     F(2k+1) = F(k)^2 + F(k+1)^2
#
# Read the bits of n from the top down. Start with k = 0 and the pair (F(0), F(1)). For each
# bit, double k using the identities above; if the bit is 1, advance one more place so k
# becomes 2k+1. After the last bit, k is n. That is one iteration per bit — O(log n) — and
# the only state is two integers, so O(1) space.
#
# Expected verdict: 611 INDETERMINATE_COMPLEXITY (tests 8/8, too fast to classify at any
# size below the problem's cap).

MOD = 10007


def solve(n):
    # (a, b) holds (F(k), F(k+1)) for the k built from the bits consumed so far.
    a, b = 0, 1

    for bit in bin(n)[2:]:          # bin(13) is '0b1101', so [2:] is '1101'
        # Doubling step: (F(k), F(k+1)) -> (F(2k), F(2k+1)).
        doubled = (a * (2 * b - a)) % MOD
        doubled_next = (a * a + b * b) % MOD

        if bit == "1":
            # k becomes 2k+1, so slide one place further: (F(2k+1), F(2k+2)).
            a, b = doubled_next, (doubled + doubled_next) % MOD
        else:
            a, b = doubled, doubled_next

    return a
