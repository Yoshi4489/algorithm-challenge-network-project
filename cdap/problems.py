"""The problem catalogue — four problems, each with an *adversarial* generator.

A problem in CDAP is more than a statement and some tests. It carries a **contract**:

    required_time: O(n)      required_space: O(1)

Passing the tests is necessary but not sufficient. The judge measures how the solution
*scales* and rejects a correct-but-too-slow one with ``606 TIME_COMPLEXITY_VIOLATION``.
That is the whole point of the arena, and it puts an unusual demand on this module.

Why the generators are adversarial
----------------------------------
To measure how an algorithm scales you must feed it its **worst case**, because most
algorithms are only slow on inputs that provoke them. The classic example: quicksort
measures a comfortable O(n log n) on random data and a disqualifying O(n^2) on data that
is already sorted. A generator that hands out random input is therefore not neutral — it
is *lenient*, and it makes the judge measure the wrong thing.

So each generator below is built to defeat the natural wrong answer for its problem, and
each says in a comment what it is defeating and why. Two are worth previewing:

* ``has-duplicate`` generates input with **no duplicates at all**, and shuffled. A
  duplicate would let both the set solution and the sort solution stop early, so we would
  be timing the early exit instead of the algorithm; and *sorted* input would let Timsort
  finish in O(n), hiding the very blind spot this problem exists to expose.
* ``two-sum-sorted`` sets the target to the sum of the last two elements. Because the list
  is strictly increasing, that is provably the **unique** answer *and* the maximum pair
  sum, so a left-to-right nested loop must scan the entire O(n^2) space to reach it.

Determinism
-----------
Every generator seeds its own ``random.Random`` from the problem id and ``n``, never the
global ``random``. Two consequences, both required: repeated timings at the same ``n`` see
the *same* input, so run-to-run differences are noise and not a different workload; and a
measurement can be reproduced tomorrow, on another machine, for the report.

Input sizes
-----------
Each problem carries three size ladders because the three measurements have different
costs. Wall-clock timing can afford large ``n``; opcode counting runs ~30x slower so its
ladder is smaller; ``tracemalloc`` sits in between. ``fib`` needs its own tiny ladder
because there ``n`` is a *value*, not a length — the naive recursion at n=64 would outlive
the assignment.

``size_cap`` exists for the opposite problem. A solution can be too *fast* to measure: an
O(n) ``fib`` at n=28 finishes in microseconds, which is indistinguishable from noise and
would fit O(1) perfectly. The measurement loop widens the ladder until the largest ``n``
takes measurable time, and ``size_cap`` is where it gives up and reports
``611 INDETERMINATE_COMPLEXITY`` instead of guessing.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Tuple

# --------------------------------------------------------------------------
# The vocabulary of a contract
# --------------------------------------------------------------------------
#
# Ordered cheapest to most expensive. The ordering *is* the contract check: a
# solution satisfies its contract when the class the judge inferred sits no later in
# this tuple than the class the problem demanded. Keeping the order in one place means
# the profiler and the problems cannot disagree about whether O(n log n) is worse than
# O(n) — which, at the input sizes we can afford, is a question the profiler often
# cannot answer at all (see README, Method A limitations).

COMPLEXITY_CLASSES: Tuple[str, ...] = (
    "O(1)",
    "O(log n)",
    "O(n)",
    "O(n log n)",
    "O(n^2)",
    "O(n^3)",
    "O(2^n)",
)


def complexity_rank(name: str) -> int:
    """Position of a complexity class in ``COMPLEXITY_CLASSES``.

    Raises rather than returning a sentinel, because a typo'd class name in a contract
    should fail when the catalogue is imported, not silently rank as "cheapest" and
    accept everything.
    """
    try:
        return COMPLEXITY_CLASSES.index(name)
    except ValueError:
        raise ValueError(
            f"{name!r} is not a known complexity class; "
            f"expected one of: {', '.join(COMPLEXITY_CLASSES)}"
        ) from None


def within_contract(inferred: str, required: str) -> bool:
    """True when ``inferred`` is no worse than ``required``.

    Note the direction: being *cheaper* than the contract is fine. A problem demanding
    O(n) happily accepts an O(log n) solution — the contract is a ceiling on growth, not
    a target to hit.
    """
    return complexity_rank(inferred) <= complexity_rank(required)


# --------------------------------------------------------------------------
# Problem structure
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Contract:
    """What a solution must satisfy beyond being correct.

    The two limits are per-run guards and belong to a different question than the two
    complexity classes. ``time_limit_ms`` catches a solution that hangs on one input
    (``602 TIME_LIMIT_EXCEEDED``); ``required_time`` catches one that works fine on small
    input and dies on large (``606 TIME_COMPLEXITY_VIOLATION``). A solution can pass
    either check and fail the other, which is exactly why both exist.
    """

    required_time: str
    required_space: str
    time_limit_ms: int = 2000
    mem_limit_kb: int = 65536

    def __post_init__(self):
        # Validate at construction so a bad class name is an import-time error.
        complexity_rank(self.required_time)
        complexity_rank(self.required_space)

    def to_json(self) -> dict:
        return {
            "required_time": self.required_time,
            "required_space": self.required_space,
            "time_limit_ms": self.time_limit_ms,
            "mem_limit_kb": self.mem_limit_kb,
        }


@dataclass(frozen=True)
class TestCase:
    """One correctness check: call ``entry(*args)`` and compare against ``expected``.

    Kept separate from the generators on purpose. These decide *whether the solution is
    right*; the generators decide *how fast it is*. Mixing them would be a mistake — the
    generator's input is chosen to be a worst case, and a worst case is usually a poor
    correctness test (``has-duplicate``'s generator only ever produces ``False``).
    """

    name: str
    args: tuple
    expected: Any


@dataclass(frozen=True)
class Problem:
    id: str
    title: str
    entry: str                       # the function name the solution must define
    signature: str                   # shown to the player; not enforced
    statement: str
    contract: Contract
    samples: Tuple[dict, ...]        # {"in": ..., "out": ...}, shown to the player
    tests: Tuple[TestCase, ...]      # never sent to the player
    generate: Callable[[int], tuple] # n -> args tuple, worst case for this problem
    oracle: Callable[..., Any]        # trusted answer for generated stress inputs
    performance_sizes: Tuple[int, ...]
    time_sizes: Tuple[int, ...]
    ops_sizes: Tuple[int, ...]
    space_sizes: Tuple[int, ...]
    size_cap: int
    size_note: str = ""
    languages: Tuple[str, ...] = ("python",)

    def to_json(self) -> dict:
        """The body of a ``200 OK`` response to ``GET_PROBLEM``.

        Deliberately excludes ``tests`` and ``generate``. The player gets the samples and
        the contract — enough to solve the problem and to know what they are being held
        to — but not the test cases, because a solution that can read the tests can pass
        them without solving anything.
        """
        return {
            "id": self.id,
            "title": self.title,
            "entry": self.entry,
            "signature": self.signature,
            "statement": self.statement,
            "samples": [dict(sample) for sample in self.samples],
            "contract": self.contract.to_json(),
            "languages": list(self.languages),
        }


# --------------------------------------------------------------------------
# Generators
# --------------------------------------------------------------------------
#
# Each returns the *argument tuple* for one call to the solution's entry point, so a
# problem taking two arguments needs no special case anywhere else in the judge.


def _gen_max_subarray(n: int) -> tuple:
    """Mixed-sign values, so the shortcuts fail.

    The brute-force O(n^2) here is O(n^2) on every input, so this generator is not
    fighting the timing — it is fighting the *cheats*. Values straddle zero so that
    "sum the positives" and "return 0 when everything is negative" both give wrong
    answers, and no prefix of the list is the answer often enough to be guessable.
    """
    rng = random.Random(f"max-subarray:{n}")
    values = []
    for _ in range(n):
        values.append(rng.randint(-50, 50))
    return (values,)


def _gen_two_sum(n: int) -> tuple:
    """Strictly increasing list; target is the sum of the last two elements.

    Why that target is the right adversarial choice, in two parts.

    *It is unique.* For any i < j other than (n-2, n-1), at least one index is <= n-3, and
    the values strictly increase, so nums[i] + nums[j] < nums[n-2] + nums[n-1]. The
    largest pair sum is achieved by exactly one pair, so there is one correct answer and
    the expected output is unambiguous — no "any valid pair" special case in the harness.

    *It is the worst case for the natural wrong answer.* The obvious brute force scans
    i left to right, j after it, and only reaches (n-2, n-1) last — a full O(n^2) sweep.
    The intended two-pointer solution is also driven the long way: it starts at
    (0, n-1), finds every sum too small, and advances lo n-2 times. Both algorithms do
    their maximum work, which is the only honest way to compare their growth.

    Honest limitation: a brute force that scanned *right to left* would find this answer
    immediately and measure as O(1). Making the minimum pair sum the target would defeat
    that one and be lenient to this one; a single input cannot be the worst case for both
    directions. The generator targets the loop people actually write.
    """
    rng = random.Random(f"two-sum-sorted:{n}")
    nums = []
    value = rng.randint(1, 10)
    for _ in range(n):
        nums.append(value)
        value += rng.randint(1, 9)   # strictly increasing, so no sort is needed here
    target = nums[-1] + nums[-2] if n >= 2 else 0
    return (nums, target)


def _gen_has_duplicate(n: int) -> tuple:
    """All values distinct, and shuffled. Both halves of that matter.

    **Distinct** means the answer is ``False``, which is the worst case: a solution can
    only return ``False`` after examining everything. Plant a duplicate and the set
    solution returns on the element that repeats while the sort solution returns on the
    first adjacent match — we would be timing how early the duplicate sits, not the
    algorithm.

    **Shuffled** matters because of what this problem is for. It is the exhibit for
    Method B's blindness to C-implemented builtins: the O(n log n) sort-based solution
    spends its time inside ``list.sort()``, which opcode counting sees as a single
    ``CALL``, so it measures as near-linear. Hand that solution *sorted* input and
    Timsort's run detection finishes in O(n) — genuinely linear, and the interesting
    wrong answer would vanish for the boring reason instead of the instructive one.
    """
    rng = random.Random(f"has-duplicate:{n}")
    values = list(range(n))          # distinct by construction, no rejection sampling
    rng.shuffle(values)
    return (values,)


def _gen_fib(n: int) -> tuple:
    """``n`` is the value whose Fibonacci number is wanted, not a length.

    This is the one problem where the size parameter is not a collection length, and it
    is why ``Problem`` carries per-problem ladders instead of one global list of sizes.
    The naive recursion costs about phi**n calls, so the declared ladder has to stay in the
    twenties; the ladders elsewhere run to tens of thousands. The measurement loop widens
    it for solutions fast enough to need larger n.
    """
    return (n,)


def _oracle_max_subarray(nums) -> int:
    iterator = iter(nums)
    current = best = next(iterator)
    for value in iterator:
        current = max(value, current + value)
        best = max(best, current)
    return best


def _oracle_two_sum(nums, target) -> list:
    lo, hi = 0, len(nums) - 1
    while lo < hi:
        total = nums[lo] + nums[hi]
        if total == target:
            return [lo, hi]
        if total < target:
            lo += 1
        else:
            hi -= 1
    return []


def _oracle_has_duplicate(nums) -> bool:
    return len(set(nums)) != len(nums)


def _oracle_fib(n: int) -> int:
    previous, current = 0, 1
    for _ in range(n):
        previous, current = current, (previous + current) % FIB_MODULUS
    return previous


#: Fibonacci answers are reduced modulo this, and the choice is a measurement decision
#: rather than a mathematical one. See FIB's ``size_note`` for the full reasoning; the
#: short version is that unbounded Fibonacci numbers grow to O(n) digits, which makes a
#: correct O(n) algorithm measure as O(n^2) and earn a false 606.
#:
#: 10007 is small deliberately. The modulus has to actually bite inside a test case the
#: *naive* solution can still finish, or nothing checks that a solution applies it:
#: fib(21) = 10946 exceeds 10007, and the naive recursion reaches n=21 in about 57k calls.
#: A conventional 10**9+7 would need n >= 45, which the naive solution cannot compute in
#: this lifetime — so the conventional choice would leave the modulus untested.
FIB_MODULUS = 10007


# --------------------------------------------------------------------------
# The catalogue
# --------------------------------------------------------------------------
#
# Ladder note: the three ladders differ because the three measurements cost differently.
# Timing is cheap, so it gets six doublings — enough span that O(n) and O(n^2) separate
# by a factor of 32 in slope. Opcode counting via sys.monitoring runs ~30x slower
# (Phase 1 measured it), so its ladder is shorter and starts lower; it can afford to,
# because it is deterministic and needs only one run per size instead of five.

MAX_SUBARRAY = Problem(
    id="max-subarray",
    title="Maximum Subarray Sum",
    entry="solve",
    signature="solve(nums: list[int]) -> int",
    statement=(
        "Return the largest sum obtainable from a contiguous, non-empty subarray of "
        "nums. The list may contain negative values, and if every value is negative the "
        "answer is the largest single element."
    ),
    contract=Contract(required_time="O(n)", required_space="O(1)"),
    samples=(
        {"in": "[-2, 1, -3, 4, -1, 2, 1, -5, 4]", "out": "6"},
        {"in": "[-3, -1, -7]", "out": "-1"},
    ),
    tests=(
        TestCase("sample", ([-2, 1, -3, 4, -1, 2, 1, -5, 4],), 6),
        TestCase("single", ([5],), 5),
        TestCase("single-negative", ([-5],), -5),
        TestCase("zero", ([0],), 0),
        # The test that catches "return 0 if the best sum is negative", which is the
        # single most common wrong answer to this problem.
        TestCase("all-negative", ([-3, -1, -7],), -1),
        TestCase("all-positive", ([1, 2, 3],), 6),
        TestCase("pair-negative", ([-1, -2],), -1),
        # Kadane must reset here; a running sum that never resets returns 4.
        TestCase("alternating", ([2, -1, 2, -1, 2],), 4),
        TestCase("prefix-is-best", ([9, -1, -1, -1],), 9),
        TestCase("suffix-is-best", ([-1, -1, -1, 9],), 9),
    ),
    generate=_gen_max_subarray,
    oracle=_oracle_max_subarray,
    performance_sizes=(250_000, 500_000, 1_000_000),
    time_sizes=(1000, 2000, 4000, 8000, 16000, 32000),
    ops_sizes=(500, 1000, 2000, 4000),
    space_sizes=(1000, 2000, 4000, 8000),
    size_cap=2097152,
)


TWO_SUM_SORTED = Problem(
    id="two-sum-sorted",
    title="Two Sum (Sorted Input)",
    entry="solve",
    signature="solve(nums: list[int], target: int) -> list[int]",
    statement=(
        "nums is sorted in strictly increasing order. Return the 0-based indices of the "
        "two distinct elements that sum to target, smaller index first, as a list of two "
        "ints. Return an empty list if no such pair exists. At most one pair will match."
    ),
    contract=Contract(required_time="O(n)", required_space="O(1)"),
    samples=(
        {"in": "nums=[1, 2, 4, 8], target=12", "out": "[2, 3]"},
        {"in": "nums=[1, 2, 4, 8], target=100", "out": "[]"},
    ),
    # Powers of two are used throughout: every subset sum is distinct, so no test can
    # accidentally admit a second correct answer.
    tests=(
        TestCase("at-end", ([1, 2, 4, 8], 12), [2, 3]),
        TestCase("at-start", ([1, 2, 4, 8], 3), [0, 1]),
        TestCase("spanning", ([1, 2, 4, 8], 9), [0, 3]),
        TestCase("middle", ([1, 2, 4, 8, 16], 6), [1, 2]),
        TestCase("no-answer", ([1, 2, 4, 8], 100), []),
        TestCase("no-answer-small", ([1, 2, 4, 8], 2), []),
        TestCase("two-elements", ([3, 5], 8), [0, 1]),
        TestCase("too-short", ([3], 3), []),
        TestCase("empty", ([], 0), []),
        TestCase("negatives", ([-8, -2, 1, 4], -1), [1, 2]),
        # An element must not be paired with itself: 4+4 = 8 is not an answer here.
        TestCase("no-self-pairing", ([1, 4, 9], 8), []),
    ),
    generate=_gen_two_sum,
    oracle=_oracle_two_sum,
    performance_sizes=(250_000, 500_000, 1_000_000),
    time_sizes=(1000, 2000, 4000, 8000, 16000, 32000),
    ops_sizes=(500, 1000, 2000, 4000),
    space_sizes=(1000, 2000, 4000, 8000),
    size_cap=2097152,
)


HAS_DUPLICATE = Problem(
    id="has-duplicate",
    title="Contains Duplicate",
    entry="solve",
    signature="solve(nums: list[int]) -> bool",
    statement=(
        "Return True if any value appears more than once in nums, otherwise False. "
        "Note the contract: O(n) time is required, so sorting the list first is not "
        "fast enough — even though it passes every test."
    ),
    # The contract that makes this problem interesting. O(n) time forbids the sort-based
    # solution, and O(n) space is what permits the set-based one. The statement warns
    # the player, because being rejected for an algorithm choice is unfamiliar and the
    # arena should be upfront about it rather than clever.
    contract=Contract(required_time="O(n)", required_space="O(n)"),
    samples=(
        {"in": "[1, 2, 3, 1]", "out": "True"},
        {"in": "[1, 2, 3]", "out": "False"},
    ),
    tests=(
        TestCase("dup-at-end", ([1, 2, 3, 1],), True),
        TestCase("no-dup", ([1, 2, 3],), False),
        TestCase("adjacent-dup", ([5, 5],), True),
        TestCase("empty", ([],), False),
        TestCase("single", ([7],), False),
        TestCase("dup-late", ([1, 2, 3, 4, 5, 3],), True),
        TestCase("negatives", ([-1, -2, -1],), True),
        TestCase("zero-and-empty-like", ([0, 0],), True),
        TestCase("descending-no-dup", ([9, 6, 3, 1],), False),
    ),
    generate=_gen_has_duplicate,
    oracle=_oracle_has_duplicate,
    performance_sizes=(100_000, 250_000, 500_000),
    time_sizes=(1000, 2000, 4000, 8000, 16000, 32000),
    ops_sizes=(500, 1000, 2000, 4000),
    space_sizes=(1000, 2000, 4000, 8000),
    size_cap=2097152,
    size_note=(
        "Generated input contains no duplicates and is shuffled: the worst case for "
        "both the set solution and the sort solution."
    ),
)


FIB = Problem(
    id="fib",
    title="Fibonacci Number (modular)",
    entry="solve",
    signature="solve(n: int) -> int",
    statement=(
        f"Return the nth Fibonacci number modulo {FIB_MODULUS}, where fib(0) = 0 and "
        "fib(1) = 1. The contract requires O(n) time, so the two-line recursive "
        "definition will be rejected: it is correct, and it is exponential."
    ),
    contract=Contract(required_time="O(n)", required_space="O(1)", time_limit_ms=5000),
    samples=(
        {"in": "10", "out": "55"},
        {"in": "21", "out": "939"},
    ),
    tests=(
        TestCase("zero", (0,), 0),
        TestCase("one", (1,), 1),
        TestCase("two", (2,), 1),
        TestCase("three", (3,), 2),
        TestCase("ten", (10,), 55),
        TestCase("sixteen", (16,), 987),
        # fib(21) = 10946 and fib(22) = 17711, both above the modulus, so these two are
        # what catch a solution that ignores it. Small enough that the naive recursion
        # still finishes them, which matters: the naive solution must PASS every
        # correctness test and then be rejected on complexity. That is the whole demo.
        TestCase("modulus-bites", (21,), 939),
        TestCase("modulus-bites-again", (22,), 7704),
    ),
    generate=_gen_fib,
    oracle=_oracle_fib,
    performance_sizes=(250_000, 500_000, 1_000_000),
    # n is a value, not a length, so the declared ladder is tiny. Each +2 steps multiplies
    # the naive recursion's work by phi**2 ~ 2.6, giving a ~120x span across the ladder —
    # plenty of dynamic range to separate exponential from polynomial. The ladder starts in
    # the low twenties on purpose: the naive solution's smallest sizes finish below the 5 ms
    # timing floor, and if fewer than four rungs cleared it the measurement loop would widen
    # the ladder upward — into a naive fib(56) that runs for hours and is only stopped by the
    # parent's wall-clock kill, turning the headline 606 into a spurious 602. Starting at 22
    # keeps enough measurable rungs (roughly n=24 and up) that the exponential is fitted at
    # its declared sizes and the ladder never has to widen.
    time_sizes=(22, 24, 26, 28, 30, 32),
    ops_sizes=(12, 14, 16, 18),
    space_sizes=(12, 14, 16, 18),
    # An O(n) solution finishes n=28 in microseconds, far below the timer's noise floor, so
    # the measurement loop widens the ladder by doubling until enough points are big enough
    # to fit. This cap is where widening stops. It is large because it has to be: an O(n)
    # modular fib needs n in the hundreds of thousands before a single call takes 5 ms.
    # The naive recursion never comes near it — it is already slow at n=28 — so the cap
    # costs nothing there.
    size_cap=1_048_576,
    size_note=(
        "n is the Fibonacci index, not a collection length. Answers are reduced modulo "
        f"{FIB_MODULUS} so that every intermediate value stays a machine-word int. "
        "Without the modulus, fib(n) has about 0.694*n bits, so each addition costs O(n) "
        "and a textbook-correct O(n) DP solution measures as O(n^2) in wall-clock — "
        "earning a false 606 for an algorithm that is right. The modulus removes the "
        "confound instead of letting the profiler take the blame for it."
    ),
)


PROBLEMS: Dict[str, Problem] = {
    problem.id: problem
    for problem in (MAX_SUBARRAY, TWO_SUM_SORTED, HAS_DUPLICATE, FIB)
}


def get_problem(problem_id: str) -> Problem:
    """Look up a problem, or raise ``KeyError``.

    The server turns that ``KeyError`` into ``404 NOT_FOUND``; letting it propagate here
    keeps the catalogue free of any opinion about protocol status codes.
    """
    return PROBLEMS[problem_id]


def problem_ids() -> Tuple[str, ...]:
    return tuple(PROBLEMS)


def _plain_fib(n: int) -> int:
    """Fibonacci with no modulus, used only by the catalogue's own self-check."""
    a, b = 0, 1
    for _ in range(n):
        a, b = b, a + b
    return a


def _self_check() -> None:
    """Sanity checks over the catalogue itself, run by ``python -m cdap.problems``.

    These check the *problems*, not any solution: that contracts name real complexity
    classes, that every expected answer is what a known-good reference computes, and
    that each generator is deterministic and produces the worst case it claims. A broken
    test case here would look like a broken submission later, which is a miserable thing
    to debug during a demo.
    """
    for problem in PROBLEMS.values():
        assert problem.id and problem.entry and problem.statement
        assert problem.languages == ("python",), problem.id
        complexity_rank(problem.contract.required_time)
        complexity_rank(problem.contract.required_space)
        # Ladders must ascend, and opcode counting must not ask for more than timing.
        for ladder in (problem.time_sizes, problem.ops_sizes, problem.space_sizes):
            assert len(ladder) >= 4, (problem.id, ladder)
            assert list(ladder) == sorted(ladder), (problem.id, ladder)
        assert max(problem.ops_sizes) <= max(problem.time_sizes), problem.id
        assert problem.size_cap >= max(problem.time_sizes), problem.id
        # Generators must be deterministic: same n, same input, every time.
        first = problem.generate(64 if problem.id != "fib" else 8)
        again = problem.generate(64 if problem.id != "fib" else 8)
        assert first == again, f"{problem.id} generator is not deterministic"

    # Every expected answer, checked against an independent reference implementation.
    # Writing the reference twice is the point: if the test data and the checker share a
    # bug, the bug is invisible.
    for case in MAX_SUBARRAY.tests:
        nums, = case.args
        best = max(
            sum(nums[i:j])
            for i in range(len(nums))
            for j in range(i + 1, len(nums) + 1)
        )
        assert best == case.expected, (case.name, best, case.expected)

    for case in TWO_SUM_SORTED.tests:
        nums, target = case.args
        found = [
            [i, j]
            for i in range(len(nums))
            for j in range(i + 1, len(nums))
            if nums[i] + nums[j] == target
        ]
        assert len(found) <= 1, f"{case.name} has {len(found)} answers, must have at most 1"
        expected = found[0] if found else []
        assert expected == case.expected, (case.name, expected, case.expected)

    for case in HAS_DUPLICATE.tests:
        nums, = case.args
        assert (len(set(nums)) != len(nums)) == case.expected, case.name

    for case in FIB.tests:
        n, = case.args
        a, b = 0, 1
        for _ in range(n):
            a, b = b, a + b
        assert a % FIB_MODULUS == case.expected, (case.name, a % FIB_MODULUS, case.expected)

    # The modulus must actually be exercised by at least one test, or a solution could
    # ignore it and still pass everything.
    assert any(
        case.expected != _plain_fib(case.args[0]) for case in FIB.tests
    ), "no fib test forces the modulus; a solution could ignore it and pass"

    # The two generator claims that the report leans on.
    values, = HAS_DUPLICATE.generate(2000)
    assert len(set(values)) == len(values), "has-duplicate input must have no duplicates"
    assert values != sorted(values), "has-duplicate input must be shuffled, not sorted"

    nums, target = TWO_SUM_SORTED.generate(2000)
    assert all(nums[i] < nums[i + 1] for i in range(len(nums) - 1)), "must strictly increase"
    assert target == nums[-1] + nums[-2], "target must be the maximum pair sum"
    hits = sum(
        1
        for i in range(len(nums))
        for j in range(i + 1, len(nums))
        if nums[i] + nums[j] == target
    )
    assert hits == 1, f"generated two-sum target has {hits} answers, must be unique"


def main() -> int:
    from . import capabilities

    capabilities.enable_utf8_output()
    _self_check()

    print("CDAP problem catalogue")
    print("=" * 74)
    for problem in PROBLEMS.values():
        contract = problem.contract
        print(f"\n{problem.id}  —  {problem.title}")
        print(f"  entry      {problem.signature}")
        print(f"  contract   time {contract.required_time}, space {contract.required_space}"
              f"  (limits: {contract.time_limit_ms} ms, {contract.mem_limit_kb} KB)")
        print(f"  tests      {len(problem.tests)} cases")
        print(f"  ladders    time={list(problem.time_sizes)}")
        print(f"             ops ={list(problem.ops_sizes)}  space={list(problem.space_sizes)}"
              f"  cap={problem.size_cap}")
        if problem.size_note:
            print(f"  note       {problem.size_note}")
    print("\n" + "=" * 74)
    print(f"{len(PROBLEMS)} problems, all self-checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
