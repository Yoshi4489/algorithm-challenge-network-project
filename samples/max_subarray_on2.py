# Brute force — O(n^2) time, O(1) space. Correct, and rejected anyway.
#
# THIS IS THE HEADLINE DEMO. Every test passes: for each start index it extends a running
# sum rightward and keeps the best total, which is a genuinely correct way to solve the
# problem. On LeetCode this is an accepted submission.
#
# Here it is not. The problem's contract requires O(n) time, the judge measures runtime
# across six input sizes and fits a growth model, and the fit comes back O(n^2). The
# verdict is 606 TIME_COMPLEXITY_VIOLATION — a protocol *success* carrying a rejection of
# the algorithm, not of the message.
#
# Note the inner loop keeps a running sum rather than calling sum(nums[i:j]): that would be
# O(n^3) and would also hide the work inside a C builtin, which is a different lesson.
#
# Expected verdict: 606 TIME_COMPLEXITY_VIOLATION (tests 10/10, inferred O(n^2)).


def solve(nums):
    best = nums[0]
    for i in range(len(nums)):
        running = 0
        for j in range(i, len(nums)):
            running += nums[j]
            if running > best:
                best = running
    return best
