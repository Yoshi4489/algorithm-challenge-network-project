"""The in-child harness — the only place in CDAP that executes a player's code.

This module runs **inside the sandboxed child process**, started by a backend. It is
handed a job on stdin, imports the submitted source, and produces four things:

1. **correctness** — run every test case, compare against the expected answer
2. **time** — Method A, wall-clock timing across the problem's size ladder
3. **ops** — Method B, opcode counts across a shorter ladder (``sys.monitoring``)
4. **space** — peak auxiliary memory via ``tracemalloc``

It does *not* decide a verdict. It measures and reports; ``judge/profiler.py`` fits the
models and ``judge/worker.py`` maps the outcome to a 6xx code. Keeping measurement and
judgement apart matters because the profiler is then testable on recorded numbers, with
no subprocess in sight.

The result channel, and why it has a sentinel
---------------------------------------------
The child's stdout is **not** trustworthy: the submitted code can ``print()`` anything it
likes, including a convincing JSON verdict. So the harness emits

    __CDAP_RESULT__{"ok": true, ...}

as the final line, and the parent takes **the last line bearing that sentinel**, ignoring
everything before it. A solution that prints a forged result merely produces a line the
parent skips, because the harness always appends the real one afterwards. Solution output
is separately captured and truncated, so a print-loop cannot fill the disk either.

Measurement discipline
----------------------
Four rules, each guarding against a specific way of measuring the wrong thing:

* **Time the function, never the process.** Interpreter startup is tens of milliseconds
  and would swamp a small ``n``, making everything look constant-time.
* **Take the minimum of repeated runs, not the mean.** Timing noise is one-sided — a GC
  pause or a scheduler preemption can only make a run *slower*. The minimum is the run
  that suffered least, so it is the cleanest estimate of the true cost.
* **Discard a warm-up run.** The first call pays for lazily-created caches and
  first-touch page faults that later calls do not.
* **Rebuild the input for every run, outside the timer.** Some solutions mutate their
  argument (an in-place sort is the obvious one), and a mutated input is a different
  workload — the second run would be timing sorted data.

Growth, not absolute time
-------------------------
Nothing here compares milliseconds against a threshold. The judge asks how the cost
*grows* as ``n`` grows, which is why absolute machine speed mostly cancels out: a slow
machine makes every point slower by roughly the same factor, and the fitted class is
unchanged. That is the property that lets this run on a laptop and a grader's desktop and
reach the same verdict.
"""

from __future__ import annotations

import json
import sys
import time
import tracemalloc
from typing import Any, Callable, Dict, List, Optional, Tuple

#: Marks the real result line. Chosen to be something no plausible solution prints.
RESULT_SENTINEL = "__CDAP_RESULT__"

#: Method A: how many times each size is run before taking the minimum.
TIME_REPEATS = 5

#: A measurement below this is indistinguishable from timer noise, so the ladder is
#: widened rather than fitted. 5 ms is comfortably above the ~100 ns resolution of
#: perf_counter and above typical scheduler jitter.
MIN_MEASURABLE_S = 0.005

#: How many above-the-floor points the profiler needs before it will fit a growth rate.
#: Four is the smallest number that can tell a trend from a fluke: three points can be
#: fitted convincingly by almost any model.
MIN_FIT_POINTS = 4

#: Ceiling on one measured call. Distinct from the problem's own time_limit_ms, which
#: applies to a correctness run; this one stops a ladder from walking off a cliff — an
#: exponential solution at the next size up could run for hours.
MAX_LADDER_CALL_S = 8.0


class Timeout(Exception):
    """A single measured call exceeded its allowance."""


# --------------------------------------------------------------------------
# Loading the submission
# --------------------------------------------------------------------------

def load_solution(source: str, entry: str, guard: bool = True):
    """Compile ``source``, run the guard, execute it, and return its ``entry`` function.

    The three failure modes are distinguished deliberately, because they are three
    different verdicts and telling a player "your code doesn't compile" when it actually
    imported ``os`` would be useless:

    * won't parse            -> ``SyntaxError``          -> ``604 COMPILE_ERROR``
    * parses, but hostile    -> ``SandboxViolation``     -> ``609 SANDBOX_VIOLATION``
    * no such function       -> ``NameError``            -> ``604 COMPILE_ERROR``

    ``guard=False`` is the ``--no-ast-guard`` path. It exists for one purpose: the
    backend experiment runs the ``evil_*.py`` samples with the guard off to show that the
    *container*, not the guard, is what stops them. It is never used to judge a real
    submission.
    """
    from .sandbox import check_source

    if guard:
        check_source(source)          # SyntaxError and SandboxViolation both propagate
    else:
        compile(source, "<submission>", "exec")   # still must parse

    # A fresh namespace per submission. __name__ is set so that a solution guarded by
    # `if __name__ == "__main__":` does not run its demo block during import.
    namespace: Dict[str, Any] = {"__name__": "__cdap_submission__"}
    exec(compile(source, "<submission>", "exec"), namespace)

    function = namespace.get(entry)
    if function is None:
        raise NameError(f"solution does not define a function named {entry!r}")
    if not callable(function):
        raise NameError(f"{entry!r} is defined but is not callable")
    return function


# --------------------------------------------------------------------------
# 1. Correctness
# --------------------------------------------------------------------------

def run_tests(function, tests) -> dict:
    """Run every test case and report pass/fail counts plus the first failure.

    Every case runs even after one fails — "3/10 passed" is far more informative than
    "failed", both for the player and for the report's tables.

    Types are compared, not just values, and that is not pedantry: ``True == 1`` in
    Python, so a ``has-duplicate`` solution returning ``1`` would silently pass a value-only
    comparison. That mismatch is its own verdict, ``608 OUTPUT_FORMAT_ERROR`` — the right
    answer in the wrong shape.
    """
    passed = 0
    failures: List[dict] = []

    for case in tests:
        # Deep-copy the arguments so a mutating solution cannot corrupt a later case's
        # input. copy.deepcopy handles the nested lists these problems use.
        import copy

        args = copy.deepcopy(case.args)
        try:
            got = function(*args)
        except Exception as exc:                     # noqa: BLE001 - any error is a result
            failures.append({
                "test": case.name,
                "kind": "exception",
                "detail": f"{type(exc).__name__}: {exc}",
            })
            continue

        if _same_shape(got, case.expected) and got == case.expected:
            passed += 1
        elif got == case.expected:
            failures.append({
                "test": case.name,
                "kind": "format",
                "detail": (
                    f"expected {type(case.expected).__name__} {case.expected!r}, "
                    f"got {type(got).__name__} {got!r}"
                ),
            })
        else:
            failures.append({
                "test": case.name,
                "kind": "wrong",
                "detail": f"expected {case.expected!r}, got {got!r}",
            })

    return {
        "passed": passed,
        "total": len(tests),
        "summary": f"{passed}/{len(tests)}",
        "failures": failures[:5],           # enough to diagnose, not enough to flood
        "all_passed": passed == len(tests),
    }


def _same_shape(got, expected) -> bool:
    """True when ``got`` has the type the problem asked for.

    bool is checked before int because ``bool`` *is* a subclass of ``int``, so an
    ``isinstance`` test alone would let ``True`` stand in for ``1``.
    """
    if isinstance(expected, bool):
        return isinstance(got, bool)
    if isinstance(expected, int):
        return isinstance(got, int) and not isinstance(got, bool)
    return type(got) is type(expected)


# --------------------------------------------------------------------------
# 2. Method A — wall-clock timing
# --------------------------------------------------------------------------

def measure_time(function, generate, sizes, size_cap) -> dict:
    """Time ``function`` across ``sizes``, widening the ladder if the numbers are noise.

    Two outputs matter to the profiler. ``samples_ms`` is every measurement taken, kept
    complete because the report quotes it. ``usable_sizes`` is the subset that carries
    information — the points at or above the noise floor.

    The distinction earns its keep when the ladder widens. Suppose an O(n) ``fib`` solution
    is measured at the declared sizes 18..28: every point is a few microseconds, which is
    timer jitter, not signal. The ladder widens to 56, 112, ... until the largest point is
    genuinely measurable. If the original sub-floor points were then fitted alongside the
    real ones, the fit would be dominated by six values that are pure noise and would
    happily report O(1). So they are recorded and excluded.

    When too few usable points remain to fit anything, ``measurable`` is False and the
    honest answer is ``611 INDETERMINATE_COMPLEXITY`` — a solution can be genuinely too
    fast to classify at any size we can afford, and saying so beats guessing.
    """
    samples: Dict[int, float] = {}
    ladder = list(sizes)
    notes: List[str] = []

    index = 0
    while index < len(ladder):
        n = ladder[index]
        try:
            samples[n] = _time_one_size(function, generate, n)
        except Timeout:
            # The solution is slow enough that the next size would be worse. Stop here:
            # the points already collected are what reveal the growth, and an exponential
            # solution reaching this branch is exactly the case we want to catch.
            notes.append(f"stopped at n={n}: a single call exceeded {MAX_LADDER_CALL_S}s")
            break

        index += 1
        if index < len(ladder):
            continue

        # The declared ladder is exhausted. Widen while there are still too few points
        # above the noise floor to fit — not merely until the *first* one clears it, which
        # would leave a single usable measurement and nothing to fit a slope through.
        # Doubling keeps the spacing geometric, which is what gives a growth-rate fit its
        # dynamic range.
        usable_now = sum(1 for seconds in samples.values() if seconds >= MIN_MEASURABLE_S)
        if usable_now >= MIN_FIT_POINTS:
            break

        wider = ladder[-1] * 2
        if wider > size_cap:
            notes.append(
                f"cannot widen past the problem's size cap ({size_cap}) with only "
                f"{usable_now} measurable point(s); this solution may be too fast to classify"
            )
            break

        ladder.append(wider)
        notes.append(
            f"{usable_now} of {MIN_FIT_POINTS} needed points clear the "
            f"{MIN_MEASURABLE_S * 1000:.0f} ms floor — widening to n={wider}"
        )

    usable = sorted(n for n, seconds in samples.items() if seconds >= MIN_MEASURABLE_S)
    if samples and len(usable) < len(samples):
        notes.append(
            f"{len(samples) - len(usable)} of {len(samples)} points fell below the noise "
            "floor and are excluded from fitting (reported for transparency)"
        )

    return {
        "samples_ms": {str(n): round(seconds * 1000, 4) for n, seconds in sorted(samples.items())},
        "sizes": sorted(samples),
        "usable_sizes": usable,
        "measurable": len(usable) >= MIN_FIT_POINTS,
        "repeats": TIME_REPEATS,
        "noise_floor_ms": MIN_MEASURABLE_S * 1000,
        "notes": notes,
    }


def _time_one_size(function, generate, n: int) -> float:
    """Minimum of ``TIME_REPEATS`` timed calls at size ``n``, after one warm-up.

    The input is regenerated before every call, outside the timed region, so a solution
    that mutates its argument cannot make its own later runs cheaper.
    """
    args = generate(n)
    function(*args)                       # warm-up, discarded

    best = None
    for _ in range(TIME_REPEATS):
        args = generate(n)                # rebuilt so every run sees identical input
        start = time.perf_counter()
        function(*args)
        elapsed = time.perf_counter() - start

        if elapsed > MAX_LADDER_CALL_S:
            raise Timeout(f"call at n={n} took {elapsed:.1f}s")
        if best is None or elapsed < best:
            best = elapsed

    return best


# --------------------------------------------------------------------------
# 3. Method B — opcode counting
# --------------------------------------------------------------------------

def measure_ops(function, generate, sizes, counter_name: str) -> dict:
    """Count executed opcodes at each size, using the mechanism the probe selected.

    Deterministic, so one run per size is enough — no repeats, no minimum. That is Method
    B's advantage: no timing noise at all, and no dependence on machine speed.

    Its disadvantage is documented at length in the README and is the report's most
    interesting finding: **opcode counting only sees Python-level bytecode.** Work inside
    a C-implemented builtin — ``list.sort()``, ``sum()``, ``str.join()`` — costs a single
    ``CALL`` no matter how much work it does, so a Timsort-based O(n log n) solution
    measures as near-linear. ``samples/has_duplicate_onlogn.py`` exists to exhibit exactly
    that, and it must not be "fixed".

    Which mechanism is in use is not a detail: Phase 1 found ``sys.settrace`` +
    ``f_trace_opcodes`` **silently inert** on CPython 3.14.3, counting zero opcodes without
    raising. Zero at every size fits O(1) perfectly, so an unguarded Method B would have
    confidently accepted every too-slow solution. The name is carried in the result so the
    report can say which mechanism produced the numbers.
    """
    from .. import capabilities

    counter = capabilities.opcode_counter_by_name(counter_name)
    if counter is None:
        return {
            "available": False,
            "mechanism": counter_name,
            "reason": "no usable opcode-counting mechanism on this interpreter",
            "samples": {},
            "sizes": [],
        }

    samples: Dict[int, int] = {}
    notes: List[str] = []
    for n in sizes:
        args = generate(n)
        try:
            samples[n] = counter(function, *args)
        except Exception as exc:                     # noqa: BLE001
            notes.append(f"counting failed at n={n}: {type(exc).__name__}: {exc}")
            break

    # The same guard the Phase 1 probe applies, repeated here because a mechanism that
    # works on a warm-up workload could still return zero for a solution that spends all
    # its time inside builtins. Zero counts must never be fitted.
    counted = bool(samples) and max(samples.values()) > 0
    if samples and not counted:
        notes.append(
            "every size counted zero opcodes — the mechanism is inert or the solution "
            "does all its work inside C builtins"
        )

    return {
        "available": counted,
        "mechanism": counter_name,
        "samples": {str(n): count for n, count in sorted(samples.items())},
        "sizes": sorted(samples),
        "notes": notes,
    }


# --------------------------------------------------------------------------
# 4. Space
# --------------------------------------------------------------------------

def measure_space(function, generate, sizes) -> dict:
    """Peak **auxiliary** memory at each size, via ``tracemalloc``.

    "Auxiliary" is the whole trick. A solution that receives a list of n ints is holding
    O(n) memory before it does anything, so measuring raw peak would rank every solution
    to every list problem as O(n) and make an ``O(1)`` space contract impossible to satisfy.
    So the sequence is:

        build the input  ->  snapshot  ->  reset_peak()  ->  run  ->  read peak

    and the reported figure is what the *solution* allocated on top of its input. That is
    the number the contract is about, and it is what distinguishes Kadane's two scalars
    from a prefix-sum array.

    Limitation, stated rather than hidden: ``tracemalloc`` sees Python-level allocations.
    Memory allocated inside a C extension outside the Python allocator is invisible to it —
    the same class of blind spot Method B has, for the same reason.
    """
    if not hasattr(tracemalloc, "reset_peak"):
        return {
            "available": False,
            "reason": "tracemalloc.reset_peak is unavailable (needs Python 3.9+)",
            "samples_kb": {},
            "sizes": [],
        }

    samples: Dict[int, float] = {}
    notes: List[str] = []

    for n in sizes:
        args = generate(n)                # built BEFORE tracing starts being measured
        tracemalloc.start()
        try:
            tracemalloc.reset_peak()      # discount the input's own footprint
            function(*args)
            _current, peak = tracemalloc.get_traced_memory()
        except Exception as exc:                     # noqa: BLE001
            notes.append(f"space measurement failed at n={n}: {type(exc).__name__}: {exc}")
            break
        finally:
            tracemalloc.stop()
        samples[n] = peak / 1024.0

    return {
        "available": bool(samples),
        "samples_kb": {str(n): round(kb, 3) for n, kb in sorted(samples.items())},
        "sizes": sorted(samples),
        "notes": notes,
    }


# --------------------------------------------------------------------------
# The job
# --------------------------------------------------------------------------

def run_job(job: dict) -> dict:
    """Execute one submission end to end and return the measurement record.

    The order is deliberate: **correctness first, and stop if it fails.** Profiling a
    wrong solution wastes seconds to answer a question nobody asked — "how does this
    incorrect answer scale?" — and the verdict is already decided.

    Every outcome is a *return*, never an exception escaping to the caller. A crash in
    here would leave the parent with no sentinel line at all and no way to tell a hostile
    submission from a judge bug, and those are two very different verdicts (``605`` vs
    ``612``).
    """
    from ..problems import get_problem

    source = job["source"]
    guard = job.get("guard", True)
    profile = job.get("profile", True)
    counter_name = job.get("opcode_counter", "none")

    try:
        problem = get_problem(job["problem"])
    except KeyError:
        return _failed("judge_error", f"unknown problem {job.get('problem')!r}")

    # -- load ------------------------------------------------------------
    try:
        function = load_solution(source, problem.entry, guard=guard)
    except SyntaxError as exc:
        return _failed("compile_error", f"line {exc.lineno}: {exc.msg}")
    except NameError as exc:
        return _failed("compile_error", str(exc))
    except Exception as exc:                         # noqa: BLE001
        # Covers SandboxViolation and anything raised at module level by the submission
        # itself (a solution whose top-level code divides by zero lands here).
        kind = "sandbox_violation" if type(exc).__name__ == "SandboxViolation" else "runtime_error"
        return _failed(kind, str(exc))

    # -- correctness ------------------------------------------------------
    tests = run_tests(function, problem.tests)
    result = {
        "ok": True,
        "problem": problem.id,
        "entry": problem.entry,
        "guard_enabled": guard,
        "tests": tests,
        "contract": problem.contract.to_json(),
    }

    if not tests["all_passed"]:
        # Report which kind of wrong, so the worker can pick 601 vs 605 vs 608 without
        # re-deriving it from the failure text.
        kinds = {failure["kind"] for failure in tests["failures"]}
        if "exception" in kinds:
            result["outcome"] = "runtime_error"
        elif kinds == {"format"}:
            result["outcome"] = "output_format_error"
        else:
            result["outcome"] = "wrong_answer"
        result["profiled"] = False
        return result

    result["outcome"] = "tests_passed"

    if not profile:
        result["profiled"] = False
        return result

    # -- measurement ------------------------------------------------------
    try:
        result["time"] = measure_time(
            function, problem.generate, problem.time_sizes, problem.size_cap
        )
        result["ops"] = measure_ops(
            function, problem.generate, problem.ops_sizes, counter_name
        )
        result["space"] = measure_space(
            function, problem.generate, problem.space_sizes
        )
    except Exception as exc:                         # noqa: BLE001
        # Correctness already passed, so this is a measurement failure and the player is
        # not at fault. 612 JUDGE_ERROR, never a complexity verdict.
        return _failed("judge_error", f"profiling failed: {type(exc).__name__}: {exc}")

    result["profiled"] = True
    return result


def _failed(outcome: str, detail: str) -> dict:
    return {"ok": False, "outcome": outcome, "detail": detail, "profiled": False}


def main() -> int:
    """Read a job as JSON on stdin, emit the sentinel-tagged result on stdout.

    stdin is consumed entirely before the submission is loaded, so a solution that reads
    stdin cannot swallow part of its own job description.
    """
    raw = sys.stdin.read()
    try:
        job = json.loads(raw)
    except ValueError as exc:
        print(RESULT_SENTINEL + json.dumps(_failed("judge_error", f"bad job JSON: {exc}")))
        return 1

    try:
        result = run_job(job)
    except Exception as exc:                         # noqa: BLE001 - last line of defence
        result = _failed("judge_error", f"{type(exc).__name__}: {exc}")

    # The sentinel goes last, on its own line, and is flushed explicitly — a killed child
    # whose buffer never flushed looks identical to one that produced no result.
    sys.stdout.write("\n" + RESULT_SENTINEL + json.dumps(result) + "\n")
    sys.stdout.flush()
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
