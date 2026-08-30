"""End-to-end regressions for the authoritative performance judge policy.

Unlike the fast unit-style audit tests, this module executes the large hidden cases. Run it
before a release, not on every edit::

    py -3 -m cdap.selftest_performance
"""

from __future__ import annotations

from pathlib import Path

from .judge.profiler import judge_record
from .judge.runner import PERFORMANCE_POLICY, run_job
from .problems import get_problem
from .status import Verdict, format_status


ROOT = Path(__file__).resolve().parent.parent
CASES = (
    (ROOT / "solution.py", "max-subarray"),
    (ROOT / "samples" / "has_duplicate_onlogn.py", "has-duplicate"),
)


def assess(path: Path, problem_id: str) -> dict:
    problem = get_problem(problem_id)
    record = run_job({
        "problem": problem_id,
        "source": path.read_text(encoding="utf-8"),
        "guard": True,
        "profile": True,
        "opcode_counter": "none",
        "policy": PERFORMANCE_POLICY,
    })
    return judge_record(record, problem.contract.to_json())


def main() -> int:
    for path, problem_id in CASES:
        verdict = assess(path, problem_id)
        code = int(verdict["verdict"])
        print(
            f"{path.name}: {format_status(code)} "
            f"cpu={verdict.get('cpu_ms', '?')}ms "
            f"wall={verdict.get('wall_ms', '?')}ms "
            f"peak_aux={verdict.get('peak_aux_kb', '?')}KB"
        )
        if code != int(Verdict.ACCEPTED):
            raise AssertionError(
                f"{path.name} should pass performance policy: {verdict.get('detail', verdict)}"
            )
    print("authoritative performance regressions passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
