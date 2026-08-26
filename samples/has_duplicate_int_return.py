# Right answer, wrong type — the 608 OUTPUT_FORMAT_ERROR demo.
#
# This solution is algorithmically perfect: a single pass with a set, O(n) time and O(n)
# space, exactly what has-duplicate's contract asks for. It returns 1 and 0 instead of True
# and False.
#
# In Python that would normally slip straight through, and that is precisely why the check
# exists. `1 == True` and `0 == False` are both True, because bool is a *subclass* of int.
# A judge comparing only values would see every test pass and hand out 600 ACCEPTED for a
# solution whose signature says `-> bool` and which returns ints. So run_tests compares the
# type as well as the value, and it checks bool before int — an isinstance(got, int) test
# alone would happily accept True as 1.
#
# The result is its own verdict rather than a wrong answer, and the distinction is worth
# stating on camera:
#
#   * 601 WRONG_ANSWER        — the value is wrong. The algorithm is broken.
#   * 608 OUTPUT_FORMAT_ERROR — the value is right, the shape is wrong. This file.
#
# Telling a player "wrong answer" when their logic is flawless and they merely returned the
# wrong type would send them rewriting a correct algorithm. 608 says "your answer is right,
# fix the return type", which is a completely different piece of advice.
#
# The failure detail spells it out per test: `expected bool True, got int 1`. All nine tests
# fail this way, so the failure kinds are uniformly "format" and the runner reports
# output_format_error rather than falling through to wrong_answer.
#
# Judge with:  python -m cdap.judge.backends samples/has_duplicate_int_return.py has-duplicate
#
# Expected verdict: 608 OUTPUT_FORMAT_ERROR (0/9 passed, every failure a type mismatch; not
# profiled).


def solve(nums):
    seen = set()
    for value in nums:
        if value in seen:
            return 1        # should be True
        seen.add(value)
    return 0                # should be False
