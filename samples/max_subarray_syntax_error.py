# Does not parse — the 604 COMPILE_ERROR demo.
#
# There is a syntax error in the loop body: a missing colon on the `if`, and an unmatched
# bracket on the line under it. Because the source never compiles, nothing runs: the sandbox
# guard's ast.parse raises SyntaxError, the runner lets it propagate, and the verdict is
# 604 — distinct from 605 (parsed, then crashed) and 609 (parsed, but hostile).
#
# This is the one sample whose body is *supposed* to be broken, so it is left obviously
# broken rather than subtly: a viewer should see at a glance why the parser refuses it. Do
# not "fix" the syntax — the whole reason the file is here is to reach the 604 branch on
# camera.
#
# Expected verdict: 604 COMPILE_ERROR (SyntaxError at parse time; never executed).


def solve(nums):
    best = nums[0]
    running = nums[0]
    for value in nums[1:]:
        running = max(value, running + value)
        # Two deliberate errors: the `if` below is missing its colon, and the max( bracket
        # on the next line is never closed. Either one alone makes ast.parse reject the whole
        # module, so nothing in this file ever runs — which is exactly the 604 path.
        if running > best
            best = max(running, best
    return best
