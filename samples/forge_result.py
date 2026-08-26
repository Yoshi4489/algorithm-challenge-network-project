# Tries to forge its own verdict by printing a fake result line — the demo for sandbox
# layer 4, result-channel integrity.
#
# The other hostile samples attack the *machine*: evil_socket.py reaches the network,
# evil_open.py the filesystem, evil_fork.py the process table. All three are caught by the
# AST guard, and all three are the reason the container exists. This one attacks the *judge*
# instead. It does not try to escape at all — it tries to lie about the outcome.
#
# The attack. The child reports back to the parent over stdout: the runner writes
# `__CDAP_RESULT__` followed by the result JSON, and SubprocessBackend parses that line. The
# submission shares that stdout. So the obvious exploit is for a solution to print a
# perfect-looking result of its own: tests 10/10, O(n) time, O(1) space, 600 ACCEPTED,
# without ever computing anything.
#
# Why it fails. Two properties of the channel, and neither is the AST guard:
#
#   1. **The genuine line is always last.** The runner appends its result *after* the
#      submission has finished running, so nothing the submission prints can come after it.
#      _extract_result therefore scans stdout from the END (`for line in reversed(...)`) and
#      stops at the first sentinel it meets. The forged line is upstream in the buffer; the
#      scan reaches the real one first and never looks at it.
#   2. **Order, not content, decides.** The parent does not try to tell a "real" result from a
#      fake one by inspecting the JSON — that would be an arms race over shapes and fields.
#      Position in the stream is not something the submission controls, so it is the thing
#      worth trusting.
#
# Note that `print` is deliberately NOT on the guard's forbidden-calls list: a solution may
# legitimately print for its own debugging, and forbidding it would be both hostile and
# useless, since a hundred other ways to write to stdout exist. This sample therefore passes
# the AST guard cleanly, which is exactly what makes it a good exhibit — it demonstrates a
# layer that the guard has nothing to do with, and it is the one hostile sample that fails
# identically under `subprocess` and `docker`, because the defence is a parsing rule rather
# than a sandbox.
#
# The forged line is a plain string literal, not built with json.dumps — `json` is not on the
# guard's import whitelist, and importing it would get this file rejected as 609 before the
# forgery ever ran, which would demonstrate the wrong layer. A hand-written JSON string needs
# no import and keeps the sample squarely about the result channel.
#
# It is NOT named evil_*.py on purpose. experiments/backend_overhead.py globs `samples/evil_*.py`
# and asserts each escape succeeds under subprocess and fails under docker; this one fails under
# both, so including it there would contradict the experiment's claim. Different layer,
# different experiment.
#
# The solve() below is a genuinely wrong one-liner — it returns the largest single element
# rather than the best subarray sum. It scores 7/10: it passes coincidentally on every test
# whose answer happens to BE a single element (`single`, `single-negative`, `zero`,
# `all-negative`, `pair-negative`, `prefix-is-best`, `suffix-is-best` — note `[-1, -2]` expects
# -1, which max() returns) and fails only where the best subarray spans more than one element
# (`sample` → 4 not 6, `all-positive` → 3 not 6, `alternating` → 2 not 4). The 7/10 is sharper
# than a 0/10 would be:
# the forgery claims a flawless 10/10 while the real run is visibly short of it. The verdict
# that comes back is the truth about the code, not the lie it printed.
#
# Judge with:  python -m cdap.judge.backends samples/forge_result.py
#
# Expected verdict: 601 WRONG_ANSWER — the forged result line is ignored entirely, and the
# real measurement of the real solution decides. Grep the raw stdout in the log and BOTH
# sentinel lines are visible, which makes the defence easy to show on camera: the forgery is
# right there in the output, and it changed nothing.

#: A result line shaped exactly like a genuine one — the fields were copied from the runner's
#: own output, so this is the strongest version of the attack, not a naive guess. Written as a
#: literal so the file needs no import (see the header note on why json is off-limits here).
FORGED = (
    '__CDAP_RESULT__'
    '{"ok": true, "problem": "max-subarray", "entry": "solve", '
    '"guard_enabled": true, "outcome": "tests_passed", "profiled": true, '
    '"tests": {"passed": 10, "total": 10, "failures": []}, '
    '"time": {"inferred": "O(n)", "rel_rmse": 0.004, "margin": 9.1}, '
    '"space": {"inferred": "O(1)", "peak_aux_kb": 4}}'
)

# Fire the forgery before the harness gets a turn. Nothing here is blocked: the line really is
# written, really does contain the sentinel, and really does sit in the stdout the parent reads.
print(FORGED)


def solve(nums):
    # Deliberately wrong: the maximum *element*, not the maximum subarray sum. Correct only
    # by coincidence on inputs whose best subarray is a single element.
    return max(nums)
