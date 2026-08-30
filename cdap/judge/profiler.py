"""The model fitter and the decision policy — where measurements become a verdict.

``runner.py`` measures and refuses to judge; this module judges and refuses to measure.
The split is deliberate and it pays off twice: the fitter can be exercised on recorded
numbers with no subprocess in sight, and the runner can be trusted not to have an opinion
about the answer it is producing.

The question this module answers is *how does the cost grow*, not *how long did it take*.
Absolute milliseconds are a property of the machine; the growth rate is a property of the
algorithm, which is what a contract can fairly be written against.

Fitting, by hand
----------------
Given measurements ``y`` at sizes ``n``, each candidate model ``f`` (1, log n, n, n log n,
n^2, n^3, 2^n) is fitted by least squares **through the origin** — one free parameter, the
constant factor:

    c = sum(y * f) / sum(f * f)

One parameter, not two, because the model is a growth *shape* and the intercept is not
meaningful: an algorithm does not have a fixed startup cost we care about, and giving the
fit an intercept lets a straight line impersonate every curve.

The models are then ranked by **relative** RMSE:

    rel_rmse = sqrt(mean(((y - c*f) / y) ** 2))

Relative, not absolute, because the residual at n=32000 is numerically enormous compared
to the one at n=1000 in absolute terms, and an absolute error would let the largest point
decide the fit by itself. Dividing by ``y`` makes every point speak at the same volume,
and it makes the number comparable *across* models, which is what ranking needs.

The arithmetic is written out in plain loops. No numpy — partly because the project is
stdlib-only, mostly because this is the part of the code that gets explained on video and
``sum(...)/sum(...)`` in a loop is explainable in a way a vectorized one-liner is not.

Deciding, and who benefits from the doubt
-----------------------------------------
Ranking produces a winner and a runner-up, and the gap between them is the confidence:

    margin = rel_rmse[runner_up] / rel_rmse[winner]

A margin of 3.0 means the winner fits three times better and the answer is not in doubt.
A margin of 1.02 means the two models are indistinguishable in this data, and reporting
the winner as though it were settled would be a lie of precision. So:

* ``margin >= 1.15``  -> report the winner, ``confidence: high``
* ``margin <  1.15``  -> report **the cheaper of the two**, ``confidence: low``
* best ``rel_rmse > 0.35`` -> fit nothing, ``611 INDETERMINATE_COMPLEXITY``

Reporting the cheaper class under ambiguity is CLAUDE.md invariant 3, and the reason is
that the two errors are not equally bad. A false ``606`` tells a player their correct,
well-written algorithm is worse than it is — it accuses them. A false accept lets one
borderline solution through. Asymmetric costs, asymmetric policy.

This is also the honest answer to the O(n) vs O(n log n) problem. At the sizes we can
afford, log-log slopes of 1.00 and ~1.10 sit inside measurement noise, and the ``margin``
between those two models is routinely under 1.15. The profiler says ``confidence: low``
rather than inventing a threshold that would sound decisive and be wrong half the time.

Method A vs Method B
--------------------
Method A (wall-clock) is **authoritative**, because the contract is about real time and
real time is what a player experiences. Method B (opcode counts) is deterministic and
machine-independent, which makes it a useful second opinion, but it is blind to work done
inside C builtins — ``list.sort()`` is one ``CALL`` no matter how much sorting it does. So
Method B never overrides a verdict; when the two disagree the record says
``methods_disagree: true`` and carries both. That disagreement rate is a reported result
in the write-up, not a bug to be tuned away.
"""

from __future__ import annotations

import math
import sys
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from ..problems import COMPLEXITY_CLASSES, complexity_rank, within_contract
from ..status import Verdict, verdict_phrase_for

# --------------------------------------------------------------------------
# Policy constants — every threshold in one place, each with its reason
# --------------------------------------------------------------------------

#: How much better the winner must fit than the runner-up before the answer is called
#: settled. 1.15 (a 15% edge) is deliberately modest: the neighbouring classes in this
#: ladder are genuinely close at measurable sizes, and demanding a large margin would
#: push almost everything into "low confidence" and make the field useless.
CONFIDENT_MARGIN = 1.15

#: Above this relative error the best model is not describing the data at all, so no class
#: is reported. 0.35 means "the average point is 35% off the fitted curve", which is far
#: outside what timing noise produces on a ladder that cleared the noise floor.
MAX_ACCEPTABLE_RMSE = 0.35

#: Fitting needs enough points to tell a trend from a coincidence. Three points can be
#: fitted convincingly by nearly any model; four is the smallest honest number.
MIN_FIT_POINTS = 4

#: A backstop for the case where no named model fits within MAX_ACCEPTABLE_RMSE. Normally
#: that is 611 (indeterminate) — the data is too ambiguous to name a class. But if the raw
#: log-log slope already clears the contract by this many polynomial degrees, the growth is
#: a violation beyond doubt and 606 is issued instead. A naive-recursion Fibonacci grows
#: like phi^n; nothing on the ladder (the steepest model is O(2^n)) fits it within tolerance,
#: yet its slope (~12 against an O(n) contract) leaves no doubt it is super-polynomial. One
#: full degree is a deliberately wide moat: the backstop only ever *rejects*, and only when
#: the slope is decisively steep, so it can never turn a borderline-fast solution into a
#: false 606. That asymmetry is the same favour-the-contestant policy decide() follows.
SLOPE_VIOLATION_MARGIN = 1.0

#: The log-log slope a clean measurement of each class would show: for a*n^k the slope is the
#: exponent k, so the polynomial classes map to their degree. O(log n) flattens toward 0;
#: O(n log n) sits just above 1 (the log factor is a slow nudge, not a degree); O(2^n) is
#: unbounded on this axis, so nothing "decisively exceeds" an exponential contract.
_NOMINAL_LOGLOG_SLOPE = {
    "O(1)": 0.0,
    "O(log n)": 0.0,
    "O(n)": 1.0,
    "O(n log n)": 1.0,
    "O(n^2)": 2.0,
    "O(n^3)": 3.0,
    "O(2^n)": math.inf,
}

#: Space measurements below this are indistinguishable from allocator bookkeeping, and a
#: relative error against a value this small is meaningless. Kadane's two scalars land
#: here, which is exactly the O(1) the contract is asking about.
SPACE_NOISE_FLOOR_KB = 1.0


# --------------------------------------------------------------------------
# The candidate models
# --------------------------------------------------------------------------
#
# Each entry is (class name, f(n)). The names come from problems.COMPLEXITY_CLASSES so the
# fitter and the contracts cannot drift apart — a class this module could infer but a
# contract could not express would be a silent hole in the check.

def _f_constant(n: float) -> float:
    return 1.0


def _f_log(n: float) -> float:
    # Guarded at n <= 1 where log is zero or undefined. Real ladders start far above 1;
    # this only keeps a degenerate size from raising instead of simply fitting badly.
    return math.log(n) if n > 1 else 0.0


def _f_linear(n: float) -> float:
    return float(n)


def _f_nlogn(n: float) -> float:
    return float(n) * math.log(n) if n > 1 else 0.0


def _f_quadratic(n: float) -> float:
    return float(n) * float(n)


def _f_cubic(n: float) -> float:
    return float(n) * float(n) * float(n)


def _f_exponential(n: float) -> float:
    # Overflows for any n a list-shaped problem uses, which is fine and intended: the
    # caller drops a model that cannot be evaluated. Only `fib`, whose n is a *value* in
    # the tens, ever gets a usable exponential column.
    return math.pow(2.0, float(n))


MODELS: Tuple[Tuple[str, Callable[[float], float]], ...] = (
    ("O(1)", _f_constant),
    ("O(log n)", _f_log),
    ("O(n)", _f_linear),
    ("O(n log n)", _f_nlogn),
    ("O(n^2)", _f_quadratic),
    ("O(n^3)", _f_cubic),
    ("O(2^n)", _f_exponential),
)

# A model this file can infer but a contract cannot name would be unjudgeable, so the two
# vocabularies are checked against each other at import time rather than at 3am.
assert tuple(name for name, _ in MODELS) == COMPLEXITY_CLASSES, (
    "MODELS and problems.COMPLEXITY_CLASSES have drifted apart"
)


# --------------------------------------------------------------------------
# The fitter
# --------------------------------------------------------------------------

def fit_one_model(sizes: Sequence[float], values: Sequence[float],
                  f: Callable[[float], float]) -> Optional[Dict[str, float]]:
    """Fit ``values ~ c * f(sizes)`` and report how badly it misses.

    Returns None when the model cannot be evaluated on this ladder (the exponential
    overflows at any realistic list size) or when it is identically zero (``log n`` on a
    ladder that starts at n=1). Both are "this model does not apply here", not errors.
    """
    # Evaluate the model at every size first, so an overflow disqualifies the model before
    # any arithmetic has been done with partial data.
    predictors: List[float] = []
    for n in sizes:
        try:
            predictors.append(f(n))
        except (OverflowError, ValueError):
            return None
    for value in predictors:
        if math.isinf(value) or math.isnan(value):
            return None

    # c = sum(y*f) / sum(f*f) — least squares through the origin, one free parameter.
    numerator = 0.0
    denominator = 0.0
    for index in range(len(sizes)):
        numerator += values[index] * predictors[index]
        denominator += predictors[index] * predictors[index]

    if denominator == 0.0:
        return None                      # the model is flat zero on this ladder

    c = numerator / denominator

    # rel_rmse = sqrt(mean(((y - c*f) / y)^2)) — relative so every point counts equally
    # and so the number is comparable between models.
    squared_total = 0.0
    for index in range(len(sizes)):
        predicted = c * predictors[index]
        actual = values[index]
        relative_error = (actual - predicted) / actual
        squared_total += relative_error * relative_error

    rel_rmse = math.sqrt(squared_total / len(sizes))
    return {"c": c, "rel_rmse": rel_rmse}


def fit_models(sizes: Sequence[float], values: Sequence[float]) -> List[dict]:
    """Fit every candidate model and return them ranked best-first by ``rel_rmse``.

    Callers must pass strictly positive ``values`` — the relative error divides by each
    one. ``analyse`` is what enforces that; this function is the arithmetic alone.
    """
    fits: List[dict] = []
    for name, f in MODELS:
        fitted = fit_one_model(sizes, values, f)
        if fitted is None:
            continue
        fits.append({
            "model": name,
            "c": fitted["c"],
            "rel_rmse": fitted["rel_rmse"],
        })

    fits.sort(key=lambda entry: entry["rel_rmse"])
    return fits


def loglog_slope(sizes: Sequence[float], values: Sequence[float]) -> Optional[float]:
    """Least-squares slope of (log n, log y) — a sanity check on the fitted class.

    Reported alongside the verdict because it is the one number a reader can check by eye:
    a slope near 1 is linear, near 2 is quadratic. When it disagrees with the ranked fit,
    something is wrong with the measurement, and the write-up would rather show that than
    hide it.
    """
    if len(sizes) < 2:
        return None

    xs: List[float] = []
    ys: List[float] = []
    for index in range(len(sizes)):
        if sizes[index] <= 0 or values[index] <= 0:
            return None                  # log undefined; no honest slope to report
        xs.append(math.log(sizes[index]))
        ys.append(math.log(values[index]))

    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)

    covariance = 0.0
    variance = 0.0
    for index in range(len(xs)):
        dx = xs[index] - mean_x
        covariance += dx * (ys[index] - mean_y)
        variance += dx * dx

    if variance == 0.0:
        return None                      # every size identical; slope undefined
    return covariance / variance


def slope_exceeds_contract(slope: Optional[float], required: str) -> bool:
    """True when a raw log-log slope is steep enough to prove a contract violation by itself.

    A backstop for ``judge_record``, used only when no named model fit well enough to
    classify. It answers a narrower question than ``decide``: not "which class is this?" but
    "is the growth already, beyond doubt, steeper than the contract allows?" It says yes only
    when the slope clears the contract's nominal slope by a full polynomial degree
    (``SLOPE_VIOLATION_MARGIN``), so it rejects a decisively-too-slow solution — a phi^n
    recursion against an O(n) contract — without ever risking a false accusation against a
    merely borderline one. Against an O(2^n) contract it never fires: nothing is decisively
    steeper than an exponential at these sizes.
    """
    if slope is None:
        return False
    nominal = _NOMINAL_LOGLOG_SLOPE.get(required)
    if nominal is None or nominal == math.inf:
        return False
    return slope > nominal + SLOPE_VIOLATION_MARGIN


# --------------------------------------------------------------------------
# The decision policy
# --------------------------------------------------------------------------

def decide(fits: List[dict]) -> dict:
    """Turn a ranked list of fits into an inferred class, a margin, and a confidence.

    This is where CLAUDE.md invariant 3 lives: under ambiguity, report the cheaper class.
    """
    if not fits:
        return {
            "inferred": None,
            "confidence": "none",
            "margin": None,
            "rel_rmse": None,
            "reason": "no candidate model could be evaluated on this ladder",
        }

    best = fits[0]

    if best["rel_rmse"] > MAX_ACCEPTABLE_RMSE:
        return {
            "inferred": None,
            "confidence": "none",
            "margin": None,
            "rel_rmse": best["rel_rmse"],
            "reason": (
                f"best fit {best['model']} misses by {best['rel_rmse']:.1%}, above the "
                f"{MAX_ACCEPTABLE_RMSE:.0%} ceiling — the data fits no model well enough to judge"
            ),
        }

    if len(fits) == 1:
        return {
            "inferred": best["model"],
            "confidence": "high",
            "margin": None,
            "rel_rmse": best["rel_rmse"],
            "reason": "only one model was applicable on this ladder",
        }

    runner_up = fits[1]

    # A perfect fit (rel_rmse 0.0) would divide by zero. It means the winner is exact, so
    # the margin is unbounded and the answer is as settled as it can be.
    if best["rel_rmse"] == 0.0:
        margin = math.inf
    else:
        margin = runner_up["rel_rmse"] / best["rel_rmse"]

    if margin >= CONFIDENT_MARGIN:
        return {
            "inferred": best["model"],
            "confidence": "high",
            "margin": margin,
            "rel_rmse": best["rel_rmse"],
            "reason": (
                f"{best['model']} fits {margin:.2f}x better than {runner_up['model']}"
            ),
        }

    # Ambiguous. The two models are within noise of each other, so name the cheaper one
    # and say the confidence is low rather than pretending the ranking settled it.
    if complexity_rank(best["model"]) <= complexity_rank(runner_up["model"]):
        cheaper = best["model"]
    else:
        cheaper = runner_up["model"]

    return {
        "inferred": cheaper,
        "confidence": "low",
        "margin": margin,
        "rel_rmse": best["rel_rmse"],
        "reason": (
            f"{best['model']} and {runner_up['model']} are within {margin:.2f}x — "
            f"indistinguishable at these sizes, so the cheaper class ({cheaper}) is "
            f"reported in the contestant's favour"
        ),
    }


def analyse(samples: Dict[str, float], usable_sizes: Optional[Sequence[int]] = None,
            floor: float = 0.0) -> dict:
    """Fit and decide over one measurement series. The common path for time, ops, and space.

    ``samples`` is the runner's ``{"1000": 12.4, ...}`` mapping — string keys, because it
    arrived as JSON. ``usable_sizes`` restricts fitting to the points the runner marked as
    above its noise floor; sub-floor points are still reported, never fitted.
    """
    points: List[Tuple[float, float]] = []
    for key, value in samples.items():
        n = float(key)
        if usable_sizes is not None and int(n) not in set(usable_sizes):
            continue
        if value <= floor:
            continue                     # relative error needs a positive denominator
        points.append((n, float(value)))

    points.sort()
    sizes = [n for n, _ in points]
    values = [value for _, value in points]

    if len(points) < MIN_FIT_POINTS:
        return {
            "inferred": None,
            "confidence": "none",
            "margin": None,
            "rel_rmse": None,
            "fitted_points": len(points),
            "loglog_slope": None,
            "ranked": [],
            "reason": (
                f"only {len(points)} usable measurement(s); {MIN_FIT_POINTS} are needed "
                "to fit a growth rate"
            ),
        }

    fits = fit_models(sizes, values)
    decision = decide(fits)
    decision["fitted_points"] = len(points)
    decision["loglog_slope"] = loglog_slope(sizes, values)
    # The full ranking is carried so the report can show *why* a class won, not just that
    # it did. Trimmed to three: past that the models are all hopeless and add only noise.
    decision["ranked"] = [
        {"model": entry["model"], "rel_rmse": round(entry["rel_rmse"], 6)}
        for entry in fits[:3]
    ]
    return decision


# --------------------------------------------------------------------------
# From a measurement record to a 6xx verdict
# --------------------------------------------------------------------------
#
# Outcomes the runner reports without ever profiling. Kept as a table because the mapping
# is pure data, and a table can be read off the page during the video.

_OUTCOME_VERDICT = {
    "compile_error": Verdict.COMPILE_ERROR,
    "sandbox_violation": Verdict.SANDBOX_VIOLATION,
    "runtime_error": Verdict.RUNTIME_ERROR,
    "wrong_answer": Verdict.WRONG_ANSWER,
    "output_format_error": Verdict.OUTPUT_FORMAT_ERROR,
    "time_limit_exceeded": Verdict.TIME_LIMIT_EXCEEDED,
    "memory_limit_exceeded": Verdict.MEMORY_LIMIT_EXCEEDED,
    "judge_error": Verdict.JUDGE_ERROR,
}


def judge_record(record: dict, contract: dict, outcome_hint: Optional[str] = None) -> dict:
    """Decide the verdict for one measurement record.

    ``outcome_hint`` comes from ``RunResult.outcome_hint()`` and outranks the record's own
    outcome, because a child killed at the deadline may have emitted nothing or something
    stale — the parent's stopwatch is the one that saw the whole run.

    The order of the checks is the order a player should read them in, and it is not
    arbitrary. Hard resource ceilings come first (602, 603): those are facts about a single
    run, and they are true regardless of how the solution scales. Growth violations follow
    (606, 607), because they are claims about a *trend*, and a trend measured through a run
    that blew a ceiling is not worth much.
    """
    outcome = outcome_hint or record.get("outcome", "judge_error")
    policy = record.get("judge_policy", "complexity-demo")

    # -- outcomes decided before any profiling happened ---------------------
    if outcome in _OUTCOME_VERDICT:
        verdict = _OUTCOME_VERDICT[outcome]
        return _verdict(verdict, record, contract, detail=record.get("detail", ""))

    if outcome != "tests_passed":
        return _verdict(Verdict.JUDGE_ERROR, record, contract,
                        detail=f"unrecognised outcome {outcome!r} from the harness")

    # Production judging is based on hidden correctness plus absolute CPU/wall/memory
    # budgets. Big-O fitting remains available as an advisory/demo tool, but timing noise
    # can no longer turn a resubmission into a different match result.
    if policy == "performance":
        performance = record.get("performance", {})
        if not performance.get("complete", False):
            return _verdict(
                Verdict.JUDGE_ERROR, record, contract,
                detail="performance record is incomplete; refusing to guess",
            )
        return _verdict(
            Verdict.ACCEPTED, record, contract,
            detail=(f"hidden suite passed in {performance.get('cpu_ms', '?')}ms CPU, "
                    f"{performance.get('wall_ms', '?')}ms wall; peak auxiliary "
                    f"{performance.get('peak_aux_kb', '?')} KB"),
        )

    # Correct, but measurement was switched off (--no-profile). Accept it and say plainly
    # that the contract was not checked, rather than implying it was.
    if not record.get("profiled"):
        return _verdict(Verdict.ACCEPTED, record, contract,
                        detail="tests passed; complexity not measured (profiling disabled)")

    time_block = record.get("time", {})
    ops_block = record.get("ops", {})
    space_block = record.get("space", {})

    # A partial profile is evidence of neither compliant nor non-compliant growth.  In
    # particular, accepting after the space pass raised at one large input lets a tailored
    # program evade the space contract.  A harness failure is 612; a budget/noise limited
    # but otherwise valid measurement is 611.  Neither path may reach 600.
    for label, block in (("time", time_block), ("space", space_block)):
        if not block.get("complete", False):
            notes = "; ".join(block.get("notes", [])) or "no complete measurement"
            code = (Verdict.JUDGE_ERROR if "failed" in notes or "unavailable" in notes
                    else Verdict.INDETERMINATE_COMPLEXITY)
            return _verdict(code, record, contract,
                            detail=f"{label} profile is incomplete: {notes}")

    # -- 603: the absolute memory ceiling ----------------------------------
    # About a single run's peak, not about growth. A solution allocating a fixed 96 MB has
    # perfectly O(1) space growth and is still over a 64 MB limit.
    space_samples = space_block.get("samples_kb", {})
    peak_aux_kb = max((float(v) for v in space_samples.values()), default=0.0)
    mem_limit_kb = contract.get("mem_limit_kb", 65536)
    if peak_aux_kb > mem_limit_kb:
        return _verdict(
            Verdict.MEMORY_LIMIT_EXCEEDED, record, contract,
            detail=(f"peak auxiliary memory {peak_aux_kb / 1024:.1f} MB exceeds the "
                    f"{mem_limit_kb / 1024:.0f} MB limit"),
        )

    # -- Method A: the authoritative time analysis -------------------------
    if not time_block.get("measurable", False):
        notes = "; ".join(time_block.get("notes", [])) or "no usable timing points"
        return _verdict(Verdict.INDETERMINATE_COMPLEXITY, record, contract,
                        detail=f"time is not measurable at these sizes: {notes}")

    time_analysis = analyse(
        time_block.get("samples_ms", {}),
        usable_sizes=time_block.get("usable_sizes"),
    )
    if time_analysis["inferred"] is None:
        # No named model fit within tolerance. Normally that is 611 — the data is too
        # ambiguous to classify. But if the raw log-log slope already clears the contract by
        # a full polynomial degree, the growth is a violation beyond doubt: a phi^n recursion
        # is not "indeterminate", it is simply too slow. Reject it rather than shrug. This
        # only ever rejects (never accepts on slope alone), so it stays inside invariant 3.
        if slope_exceeds_contract(time_analysis.get("loglog_slope"),
                                  contract["required_time"]):
            return _verdict(
                Verdict.TIME_COMPLEXITY_VIOLATION, record, contract,
                detail=(f"growth is super-{contract['required_time']}: log-log slope "
                        f"{time_analysis['loglog_slope']:.1f} clears the required "
                        f"{contract['required_time']} by more than a polynomial degree — no "
                        f"single model fit within the {MAX_ACCEPTABLE_RMSE:.0%} ceiling, but "
                        f"the trend is unambiguously too steep"),
                time_analysis=time_analysis,
            )
        return _verdict(Verdict.INDETERMINATE_COMPLEXITY, record, contract,
                        detail=time_analysis["reason"], time_analysis=time_analysis)

    # -- Method B: the deterministic second opinion, never the decider -----
    ops_analysis = None
    if ops_block.get("available"):
        ops_analysis = analyse(ops_block.get("samples", {}))

    # -- 606: time growth against the contract -----------------------------
    required_time = contract["required_time"]
    if not within_contract(time_analysis["inferred"], required_time):
        return _verdict(
            Verdict.TIME_COMPLEXITY_VIOLATION, record, contract,
            detail=(f"measured {time_analysis['inferred']} against a required "
                    f"{required_time}"),
            time_analysis=time_analysis, ops_analysis=ops_analysis,
        )

    # -- 607: space growth against the contract ----------------------------
    # The floor matters here. A true O(1) solution allocates a few hundred bytes that do
    # not grow, and fitting relative error against numbers that small measures allocator
    # bookkeeping, not the algorithm. Below the floor the answer is O(1) by observation.
    space_analysis = analyse(space_samples, floor=SPACE_NOISE_FLOOR_KB)
    if space_analysis["inferred"] is None and peak_aux_kb <= SPACE_NOISE_FLOOR_KB:
        space_analysis = {
            "inferred": "O(1)",
            "confidence": "high",
            "margin": None,
            "rel_rmse": None,
            "fitted_points": 0,
            "loglog_slope": None,
            "ranked": [],
            "reason": (
                f"peak auxiliary memory stayed under {SPACE_NOISE_FLOOR_KB} KB at every "
                "size — constant by observation, too small to fit"
            ),
        }

    required_space = contract["required_space"]
    if space_analysis["inferred"] is not None:
        if not within_contract(space_analysis["inferred"], required_space):
            return _verdict(
                Verdict.SPACE_COMPLEXITY_VIOLATION, record, contract,
                detail=(f"measured {space_analysis['inferred']} auxiliary space against a "
                        f"required {required_space}"),
                time_analysis=time_analysis, ops_analysis=ops_analysis,
                space_analysis=space_analysis,
            )

    # -- 600: correct, and inside the contract on both axes ----------------
    return _verdict(
        Verdict.ACCEPTED, record, contract,
        detail=(f"time {time_analysis['inferred']} <= {required_time}, "
                f"space {space_analysis.get('inferred') or 'unmeasured'} <= {required_space}"),
        time_analysis=time_analysis, ops_analysis=ops_analysis,
        space_analysis=space_analysis,
    )


def _verdict(code, record: dict, contract: dict, detail: str = "",
             time_analysis: Optional[dict] = None,
             ops_analysis: Optional[dict] = None,
             space_analysis: Optional[dict] = None) -> dict:
    """Assemble the verdict payload — the body of a ``VERDICT`` event.

    The evidence travels with the decision on purpose. "Your solution is O(n^2)" invites
    an argument; the same claim with the measured points, the fitted alternatives, and the
    margin behind it is checkable, which is the difference between a judge and an oracle.
    """
    tests = record.get("tests", {})
    payload = {
        # The code and the phrase are kept as separate fields, never pre-joined. The
        # assignment requires printing both, and the caller that renders them (the CLI
        # below, and the VERDICT event in Phase 5) is the right place to decide the
        # formatting — pre-joining them here caused every log line to read "606 606 ...".
        "verdict": int(code),
        "phrase": verdict_phrase_for(code),
        "detail": detail,
        "tests_passed": tests.get("summary", "0/0"),
        "failures": tests.get("failures", []),
        "required_time": contract.get("required_time"),
        "required_space": contract.get("required_space"),
        "judge_policy": record.get("judge_policy", "complexity-demo"),
    }

    performance = record.get("performance")
    if isinstance(performance, dict) and performance:
        payload["decision_basis"] = "performance_limits"
        payload["policy_version"] = performance.get("policy_version")
        payload["cpu_ms"] = performance.get("cpu_ms")
        payload["wall_ms"] = performance.get("wall_ms")
        payload["peak_aux_kb"] = performance.get("peak_aux_kb")
        payload["performance"] = performance

    if time_analysis is not None:
        payload["inferred_time"] = time_analysis["inferred"]
        payload["confidence"] = time_analysis["confidence"]
        payload["rel_rmse"] = _round(time_analysis["rel_rmse"], 4)
        payload["margin"] = _round(time_analysis["margin"], 3)
        payload["loglog_slope"] = _round(time_analysis["loglog_slope"], 3)
        payload["ranked_time"] = time_analysis["ranked"]
        payload["fit_reason"] = time_analysis["reason"]
        payload["samples_ms"] = record.get("time", {}).get("samples_ms", {})

    if ops_analysis is not None:
        payload["method_b_inferred"] = ops_analysis["inferred"]
        payload["method_b_confidence"] = ops_analysis["confidence"]
        payload["method_b_mechanism"] = record.get("ops", {}).get("mechanism")
        # Disagreement is recorded, never resolved. Method A stays authoritative; this
        # flag is the raw material for the write-up's Method A vs B table.
        if time_analysis is not None and time_analysis["inferred"] is not None:
            payload["methods_disagree"] = (
                ops_analysis["inferred"] != time_analysis["inferred"]
            )

    if space_analysis is not None:
        payload["inferred_space"] = space_analysis["inferred"]
        payload["space_confidence"] = space_analysis["confidence"]
        samples_kb = record.get("space", {}).get("samples_kb", {})
        payload["peak_aux_kb"] = _round(
            max((float(v) for v in samples_kb.values()), default=0.0), 1
        )

    return payload


def _round(value, digits: int):
    """Round for display, passing None and infinity through unharmed.

    ``json.dumps`` renders infinity as ``Infinity``, which is not valid JSON and would
    break a strict parser on the other end, so an unbounded margin becomes ``None``.
    """
    if value is None:
        return None
    if isinstance(value, float) and (math.isinf(value) or math.isnan(value)):
        return None
    return round(value, digits)


# --------------------------------------------------------------------------
# CLI — judge one file end to end
# --------------------------------------------------------------------------

def main() -> int:
    """``python -m cdap.judge.profiler <file.py> [problem-id]`` — the full judge, offline.

    Where ``cdap.judge.backends`` prints the raw measurement record, this prints the
    decision made from it, with the evidence underneath. It is the whole judge minus the
    network, which makes it the right thing to demonstrate before the server exists.
    """
    from .. import capabilities
    from ..problems import get_problem, problem_ids
    from .backends import SubprocessBackend
    from .runner import run_budget_ms

    capabilities.enable_utf8_output()
    if not 2 <= len(sys.argv) <= 3:
        print("usage: python -m cdap.judge.profiler <file.py> [problem-id]")
        print(f"problems: {', '.join(problem_ids())}")
        return 2

    path = sys.argv[1]
    problem_id = sys.argv[2] if len(sys.argv) == 3 else "max-subarray"
    with open(path, "r", encoding="utf-8") as handle:
        source = handle.read()

    problem = get_problem(problem_id)
    caps = capabilities.probe()
    job = {
        "problem": problem_id,
        "source": source,
        "guard": True,
        "profile": True,
        "opcode_counter": caps.opcode_counter_name,
    }

    backend = SubprocessBackend()
    # The run budget, not the contract's per-call time limit. Profiling deliberately calls
    # the solution at sizes the tests never use, so it needs its own allowance — see
    # runner.run_budget_ms. Passing time_limit_ms here would kill an O(n^2) solution
    # partway up the ladder and report 602 instead of the 606 it has earned.
    run = backend.run(job, time_limit_ms=run_budget_ms(problem.contract.time_limit_ms))
    record = run.result if run.result else {"outcome": run.outcome_hint(), "detail": run.error}
    verdict = judge_record(record, problem.contract.to_json(), outcome_hint=run.outcome_hint())

    print(f"submission : {path}")
    print(f"problem    : {problem.id} ({problem.title})")
    print(f"backend    : {run.backend}   wall={run.wall_ms:.0f}ms")
    print(f"contract   : time {problem.contract.required_time}, "
          f"space {problem.contract.required_space}, "
          f"mem_limit {problem.contract.mem_limit_kb} KB")
    print()
    # The assignment requires the code *and* the phrase together, everywhere.
    print(f"VERDICT    : {verdict['verdict']} {verdict['phrase']}")
    print(f"detail     : {verdict['detail']}")
    print(f"tests      : {verdict['tests_passed']}")

    if verdict.get("inferred_time"):
        print(f"time       : inferred {verdict['inferred_time']} "
              f"(confidence {verdict['confidence']}, margin {verdict['margin']}, "
              f"rel_rmse {verdict['rel_rmse']}, log-log slope {verdict['loglog_slope']})")
        print(f"             {verdict['fit_reason']}")
        print(f"             ranked: {verdict['ranked_time']}")
        print(f"             samples_ms: {verdict['samples_ms']}")
    if verdict.get("method_b_inferred"):
        flag = " (DISAGREES with Method A)" if verdict.get("methods_disagree") else ""
        print(f"method B   : inferred {verdict['method_b_inferred']} "
              f"via {verdict['method_b_mechanism']}{flag}")
    if verdict.get("inferred_space"):
        print(f"space      : inferred {verdict['inferred_space']} "
              f"(peak aux {verdict['peak_aux_kb']} KB)")
    if verdict.get("failures"):
        print("failures   :")
        for failure in verdict["failures"]:
            print(f"             {failure['test']}: [{failure['kind']}] {failure['detail']}")

    return 0 if verdict["verdict"] == int(Verdict.ACCEPTED) else 1


if __name__ == "__main__":
    raise SystemExit(main())
