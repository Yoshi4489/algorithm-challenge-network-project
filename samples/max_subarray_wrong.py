# Sums the positive values — the classic wrong answer to Maximum Subarray. Verdict 601.
#
# It looks right, and on the sample input it *is* right: [-2,1,-3,4,-1,2,1,-5,4] has best
# subarray [4,-1,2,1] summing to 6, and the positives 1+4+2+1+4 also come to... 12. Not even
# the sample survives. Which is the point of having a wrong sample at all: 601 WRONG_ANSWER
# has to be reachable on camera, and it should fail for a reason a viewer immediately
# recognizes rather than an obscure edge case.
#
# Two independent bugs, both worth naming out loud:
#
#   1. It ignores contiguity. A subarray is a *run* of adjacent elements; this picks
#      positives wherever they sit, skipping the negatives between them.
#   2. It returns 0 when every value is negative. The statement says the answer is then the
#      largest single element — the "all-negative" test case exists precisely to catch this,
#      because it is the single most common mistake people make on this problem.
#
# Note what the judge does with it: correctness runs first, it fails, and the profiler never
# runs at all. Measuring how a wrong answer scales answers a question nobody asked, and
# skipping it keeps the wrong-answer path fast.
#
# Expected verdict: 601 WRONG_ANSWER (fails on 'sample', 'all-negative', 'alternating',
# and others; not profiled).


def solve(nums):
    total = 0
    for value in nums:
        if value > 0:
            total += value
    return total
