"""Compare wall-clock and opcode-count complexity inference on known solutions.

Run with ``python -m experiments.confusion_matrix``. Results are printed and written to
``experiments/out/confusion_matrix.json``. The output directory is ignored because timing
is machine-specific; the report records the environment and representative run explicitly.
"""

from __future__ import annotations

import json
import platform
import sys
from dataclasses import dataclass
from pathlib import Path

from cdap import capabilities
from cdap.judge.backends import SubprocessBackend
from cdap.judge.profiler import judge_record
from cdap.judge.runner import run_budget_ms
from cdap.problems import get_problem

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "experiments" / "out" / "confusion_matrix.json"


@dataclass(frozen=True)
class Case:
    file: str
    problem: str
    expected: str
    note: str = ""


CASES = (
    Case("max_subarray_on.py", "max-subarray", "O(n)"),
    Case("max_subarray_on2.py", "max-subarray", "O(n^2)"),
    Case("two_sum_on.py", "two-sum-sorted", "O(n)"),
    Case("two_sum_on2.py", "two-sum-sorted", "O(n^2)"),
    Case("has_duplicate_on.py", "has-duplicate", "O(n)"),
    Case("has_duplicate_onlogn.py", "has-duplicate", "O(n log n)",
         "C-builtin blind spot"),
    Case("fib_on.py", "fib", "O(n)"),
    Case("fib_naive.py", "fib", "O(2^n)"),
)


def run_case(case: Case, backend: SubprocessBackend, counter: str) -> dict:
    problem = get_problem(case.problem)
    source = (ROOT / "samples" / case.file).read_text(encoding="utf-8")
    job = {
        "problem": case.problem,
        "source": source,
        "guard": True,
        "profile": True,
        "opcode_counter": counter,
    }
    run = backend.run(job, run_budget_ms(problem.contract.time_limit_ms))
    record = run.result or {"outcome": run.outcome_hint(), "detail": run.error}
    verdict = judge_record(record, problem.contract.to_json(),
                           outcome_hint=run.outcome_hint())
    return {
        "file": case.file,
        "problem": case.problem,
        "expected": case.expected,
        "method_a": verdict.get("inferred_time"),
        "method_a_confidence": verdict.get("confidence"),
        "method_b": verdict.get("method_b_inferred"),
        "method_b_confidence": verdict.get("method_b_confidence"),
        "methods_disagree": verdict.get("methods_disagree", False),
        "verdict": verdict["verdict"],
        "phrase": verdict["phrase"],
        "wall_ms": round(run.wall_ms, 1),
        "note": case.note,
    }


def accuracy(rows, key: str, include=None) -> dict:
    selected = [row for row in rows if include is None or include(row)]
    correct = sum(row.get(key) == row["expected"] for row in selected)
    return {
        "correct": correct,
        "total": len(selected),
        "percent": round(100.0 * correct / len(selected), 1) if selected else 0.0,
    }


def main() -> int:
    capabilities.enable_utf8_output()
    caps = capabilities.probe()
    backend = SubprocessBackend()
    rows = []
    print("CDAP complexity confusion matrix")
    print(f"Python {platform.python_version()} on {platform.platform()}")
    print(f"Method B counter: {caps.opcode_counter_name}\n")
    print(f"{'solution':30} {'true':12} {'Method A':12} {'conf':7} {'Method B':12} {'verdict'}")
    print("-" * 94)
    for case in CASES:
        row = run_case(case, backend, caps.opcode_counter_name)
        rows.append(row)
        print(f"{row['file']:30} {row['expected']:12} "
              f"{str(row['method_a']):12} {str(row['method_a_confidence']):7} "
              f"{str(row['method_b']):12} {row['verdict']} {row['phrase']}")

    polynomial = lambda row: row["expected"] != "O(2^n)"
    pure_python = lambda row: row["file"] != "has_duplicate_onlogn.py"
    summary = {
        "method_a_polynomial": accuracy(rows, "method_a", polynomial),
        "method_b_pure_python": accuracy(rows, "method_b", pure_python),
        "method_b_all": accuracy(rows, "method_b"),
        "disagreements": sum(bool(row["methods_disagree"]) for row in rows),
    }
    payload = {
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "opcode_counter": caps.opcode_counter_name,
        },
        "rows": rows,
        "summary": summary,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    print("\nSummary")
    for name, value in summary.items():
        print(f"  {name}: {value}")
    print(f"  JSON: {OUT}")

    sort_row = next(row for row in rows if row["file"] == "has_duplicate_onlogn.py")
    assert sort_row["method_b"] != sort_row["expected"], (
        "Method B unexpectedly saw through list.sort(); this experiment is meant to "
        "preserve and demonstrate its C-builtin blind spot"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
