"""Phase 1 — runtime capability probe.

The judge depends on a few interpreter features that are *not* guaranteed across
Python versions or platforms. Rather than assume them and fail mysteriously deep
inside a submission's measurement run, we probe for them once, up front, and pick
an implementation strategy.

Four questions this module answers:

1. **Can we count executed opcodes?** Method B of the complexity profiler infers
   complexity by counting bytecode instructions instead of timing them, which is
   deterministic and therefore needs only one run per input size. There are two
   mechanisms, and they are not equally available:

   * ``sys.settrace`` + ``frame.f_trace_opcodes = True`` — works from 3.7, but it
     is the old, slow tracing path.
   * ``sys.monitoring`` (PEP 669) ``INSTRUCTION`` events — 3.12+, and much cheaper.

   We probe both, verify each actually produces counts that *scale with the input*
   (a mechanism that silently counts nothing would otherwise look like O(1) for
   every submission), and prefer the faster one.

2. **Can we measure auxiliary memory?** ``tracemalloc.reset_peak()`` is 3.9+, and
   without it we cannot subtract the input's own footprint from the peak — which
   is exactly what makes "auxiliary space" measurable and O(1) space checkable.

3. **Can we impose a hard memory limit?** ``resource.setrlimit(RLIMIT_AS)`` is
   POSIX-only. On Windows it does not exist, which is the single biggest weakness
   of the ``subprocess`` backend, and this probe is where that gets stated out
   loud instead of discovered later.

4. **Is Docker usable?** Optional. If it is, ``--backend docker`` gives real cgroup
   limits and a kernel-level network block. If it is not, the judge falls back to
   ``subprocess`` and must *say so* in the verdict rather than claim otherwise.

Run it directly for a readable report::

    python -m cdap.capabilities
"""

from __future__ import annotations

import platform
import shutil
import subprocess
import sys
import time
import tracemalloc
from dataclasses import dataclass, field

# PEP 669 reserves tool ids 0-5 and names a few of them (DEBUGGER_ID = 0,
# COVERAGE_ID = 1, PROFILER_ID = 2, OPTIMIZER_ID = 5). Id 3 is unassigned, so we
# claim that one and avoid fighting a real debugger or profiler for a slot.
_MONITORING_TOOL_ID = 3
_MONITORING_TOOL_NAME = "cdap-judge"

# The probe workload. Deliberately pure Python with no builtin calls: every unit
# of work it does is a bytecode instruction that a working counter must see. That
# matters because Method B is blind to work inside C-implemented builtins, so a
# workload built out of sum() or sorted() would under-report and we would not be
# able to tell a broken counter from the known blind spot.
_PROBE_N_SMALL = 200
_PROBE_N_LARGE = 400
# count(2n) / count(n) should land near 2.0 for this linear workload. The window
# is loose because a constant per-call overhead shifts the ratio slightly at these
# small sizes; we are testing "does it scale at all", not measuring precisely.
_PROBE_RATIO_MIN = 1.6
_PROBE_RATIO_MAX = 2.4


def _linear_workload(n: int) -> int:
    """Pure-Python O(n) loop used to check that an opcode counter really counts."""
    total = 0
    for i in range(n):
        total += i
    return total


# --------------------------------------------------------------------------
# Mechanism 1: sys.settrace with per-opcode tracing
# --------------------------------------------------------------------------

def count_opcodes_settrace(fn, *args):
    """Run ``fn(*args)`` and return (result, opcodes_executed) using sys.settrace.

    The trace function receives a ``"call"`` event for each frame that is entered.
    Setting ``frame.f_trace_opcodes = True`` on that frame upgrades it to also emit
    an ``"opcode"`` event before every single bytecode instruction. Returning the
    tracer from the call event is what makes it apply to nested calls too, so a
    submission that splits its work across helper functions is still counted whole.
    """
    counter = 0

    def tracer(frame, event, arg):
        nonlocal counter
        if event == "call":
            # Opt this new frame in to per-opcode events.
            frame.f_trace_opcodes = True
            return tracer  # keep tracing inside it
        if event == "opcode":
            counter += 1
        return tracer

    sys.settrace(tracer)
    try:
        result = fn(*args)
    finally:
        # Always clear the trace hook, even if the submission raised. Leaving a
        # tracer installed would poison every later measurement in this process.
        sys.settrace(None)
    return result, counter


# --------------------------------------------------------------------------
# Mechanism 2: sys.monitoring INSTRUCTION events (PEP 669, Python 3.12+)
# --------------------------------------------------------------------------

def count_opcodes_monitoring(fn, *args):
    """Run ``fn(*args)`` and return (result, opcodes_executed) using sys.monitoring.

    PEP 669 lets the interpreter dispatch straight to our callback instead of going
    through the general tracing machinery, so this is substantially cheaper than
    settrace while counting the same thing.

    Note the callback is *not* re-entrantly monitored while it runs, so our own
    counting code does not inflate the count. Monitoring is enabled immediately
    before the call and disabled immediately after, so the only foreign
    instructions included are the handful in this function itself — a constant,
    which the model fitter absorbs into its coefficient and which therefore cannot
    change the inferred complexity class.
    """
    monitoring = sys.monitoring  # type: ignore[attr-defined]
    counter = 0

    def on_instruction(code, instruction_offset):
        nonlocal counter
        counter += 1
        # Returning monitoring.DISABLE here would permanently switch off events at
        # this code location, which is the usual optimisation. We must not: we want
        # every instruction, every time.
        return None

    monitoring.use_tool_id(_MONITORING_TOOL_ID, _MONITORING_TOOL_NAME)
    try:
        monitoring.register_callback(
            _MONITORING_TOOL_ID, monitoring.events.INSTRUCTION, on_instruction
        )
        monitoring.set_events(_MONITORING_TOOL_ID, monitoring.events.INSTRUCTION)
        try:
            result = fn(*args)
        finally:
            monitoring.set_events(_MONITORING_TOOL_ID, monitoring.events.NO_EVENTS)
            monitoring.register_callback(
                _MONITORING_TOOL_ID, monitoring.events.INSTRUCTION, None
            )
    finally:
        monitoring.free_tool_id(_MONITORING_TOOL_ID)
    return result, counter


# --------------------------------------------------------------------------
# Probing
# --------------------------------------------------------------------------

@dataclass
class MechanismReport:
    """What we learned about one opcode-counting mechanism."""

    name: str
    available: bool = False          # the API exists on this interpreter
    counts: bool = False             # it produced a non-zero count
    deterministic: bool = False      # two identical runs agreed exactly
    scales: bool = False             # count(2n) / count(n) landed near 2
    count_small: int = 0
    count_large: int = 0
    ratio: float = 0.0
    slowdown: float = 0.0            # how many times slower than an untraced run
    note: str = ""

    @property
    def usable(self) -> bool:
        """Usable only if it counts, agrees with itself, and tracks input size."""
        return self.available and self.counts and self.deterministic and self.scales


def _measure_untraced_baseline(repeats: int = 5) -> float:
    """Seconds for one untraced run of the large workload (minimum of `repeats`).

    Minimum rather than mean, for the same reason Method A uses the minimum:
    timing noise is one-sided. Something can always steal the CPU and make a run
    slower, but nothing can make it faster than the work actually takes.
    """
    best = float("inf")
    for _ in range(repeats):
        start = time.perf_counter()
        _linear_workload(_PROBE_N_LARGE)
        elapsed = time.perf_counter() - start
        best = min(best, elapsed)
    return best


def _probe_mechanism(name: str, counter_fn, baseline_s: float) -> MechanismReport:
    """Run one counting mechanism through the full battery of checks."""
    report = MechanismReport(name=name)

    try:
        _, small_a = counter_fn(_linear_workload, _PROBE_N_SMALL)
    except Exception as exc:  # the API exists but does not work here
        report.note = f"raised {type(exc).__name__}: {exc}"
        return report

    report.available = True
    report.count_small = small_a
    report.counts = small_a > 0
    if not report.counts:
        report.note = "counted zero opcodes — mechanism is silently inert"
        return report

    # Determinism: the whole appeal of Method B is that one run per size is enough.
    # If the same input gives a different count twice, that assumption is invalid.
    _, small_b = counter_fn(_linear_workload, _PROBE_N_SMALL)
    report.deterministic = small_a == small_b
    if not report.deterministic:
        report.note = f"non-deterministic: {small_a} then {small_b} for the same input"
        return report

    # Scaling: a counter stuck at a constant would make every submission look O(1),
    # which would silently accept every too-slow solution. Check it actually moves.
    started = time.perf_counter()
    _, large = counter_fn(_linear_workload, _PROBE_N_LARGE)
    traced_s = time.perf_counter() - started

    report.count_large = large
    report.ratio = large / small_a if small_a else 0.0
    report.scales = _PROBE_RATIO_MIN <= report.ratio <= _PROBE_RATIO_MAX
    if not report.scales:
        report.note = (
            f"count did not scale with input: ratio {report.ratio:.2f} "
            f"outside [{_PROBE_RATIO_MIN}, {_PROBE_RATIO_MAX}]"
        )
        return report

    # How expensive is tracing? This sets Method B's input sizes: the slower it is,
    # the smaller the largest n we can afford.
    if baseline_s > 0:
        report.slowdown = traced_s / baseline_s

    report.note = "ok"
    return report


def _probe_settrace(baseline_s: float) -> MechanismReport:
    return _probe_mechanism("sys.settrace + f_trace_opcodes", count_opcodes_settrace, baseline_s)


def _probe_monitoring(baseline_s: float) -> MechanismReport:
    if not hasattr(sys, "monitoring"):
        return MechanismReport(
            name="sys.monitoring INSTRUCTION",
            note="sys.monitoring absent (PEP 669 needs Python 3.12+)",
        )
    if not hasattr(sys.monitoring.events, "INSTRUCTION"):  # type: ignore[attr-defined]
        return MechanismReport(
            name="sys.monitoring INSTRUCTION",
            note="sys.monitoring present but INSTRUCTION event missing",
        )
    return _probe_mechanism("sys.monitoring INSTRUCTION", count_opcodes_monitoring, baseline_s)


def enable_utf8_output() -> bool:
    """Try to switch stdout/stderr to UTF-8 so the wire log's markers survive.

    Every CDAP entry point calls this first. ``TextIOWrapper.reconfigure`` exists
    precisely to change the encoding of an already-open stream, so this works even
    though the streams were opened before we got here.

    It can legitimately fail — a redirected or wrapped stream may not be
    reconfigurable — which is why ``_probe_console_unicode`` checks the *result*
    rather than trusting that this worked. Returns True if both streams took it.
    """
    changed = True
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            changed = False
            continue
        try:
            reconfigure(encoding="utf-8")
        except (ValueError, OSError, AttributeError):
            changed = False
    return changed


def _probe_console_unicode() -> tuple[bool, str]:
    """Can stdout actually render the arrows the wire log uses?

    This is not cosmetic. The assignment grades the printed message log, and that
    log is specified with direction markers ``→ ← ✗``. On a Windows console still
    running a legacy code page (cp874 for Thai, cp1252 for Western), those encode
    to ``?`` or raise UnicodeEncodeError mid-log — turning a graded deliverable
    into mojibake on camera. Detect it here so the logger can pick ASCII markers
    instead of guessing from the platform name.
    """
    encoding = getattr(sys.stdout, "encoding", None) or "ascii"
    probe_text = "→←✗"  # the three markers the wire log needs
    try:
        probe_text.encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return False, f"stdout encoding {encoding} cannot represent the arrow markers"
    return True, f"stdout encoding {encoding} renders the arrow markers"


def console_unicode_ok() -> bool:
    """True if stdout can render the wire log's ``→ ← ✗`` markers.

    The wire logger calls this once to choose between Unicode and ASCII markers.
    Public because it is a normal runtime question, not just a diagnostic one.
    """
    ok, _ = _probe_console_unicode()
    return ok


def _probe_docker() -> tuple[bool, str]:
    """Is the Docker CLI present *and* is a daemon actually answering?

    ``shutil.which`` alone is not enough: Docker Desktop can be installed but not
    running, and in that case ``docker run`` fails several seconds into a
    submission rather than at startup. We ask the daemon for its version with a
    short timeout so a stopped or starting daemon is detected here.
    """
    exe = shutil.which("docker")
    if exe is None:
        return False, "docker CLI not on PATH"
    try:
        completed = subprocess.run(
            [exe, "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        return False, "docker CLI present but daemon did not respond within 10s"
    except OSError as exc:
        return False, f"docker CLI present but could not be run: {exc}"

    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip().splitlines()
        first = detail[0] if detail else "unknown error"
        return False, f"docker CLI present but daemon unreachable: {first}"
    return True, f"docker daemon {completed.stdout.strip()}"


@dataclass
class Capabilities:
    """Everything the judge needs to know about the interpreter it is running on."""

    python_version: str
    platform_name: str

    settrace: MechanismReport
    monitoring: MechanismReport
    opcode_counter_name: str = "none"

    has_reset_peak: bool = False
    has_setrlimit: bool = False
    has_psutil: bool = False
    docker_ok: bool = False
    docker_note: str = ""
    console_unicode: bool = False
    console_note: str = ""

    perf_counter_resolution_ns: float = 0.0
    untraced_baseline_us: float = 0.0

    warnings: list = field(default_factory=list)

    @property
    def method_b_available(self) -> bool:
        """Can Method B (opcode counting) run at all on this interpreter?"""
        return self.opcode_counter_name != "none"

    def opcode_counter(self):
        """Return the chosen counting function, or None if Method B is unavailable."""
        return opcode_counter_by_name(self.opcode_counter_name)


def opcode_counter_by_name(name: str):
    """Map a mechanism name to its counting function, or None for ``"none"``.

    Exists so a judge child process can obtain the counter *without* re-running
    ``probe()``. Probing costs real time — it executes workloads under both mechanisms
    to check they count, are deterministic, and scale — and paying that per submission
    would dominate the measurement it is supposed to protect. The parent probes once at
    startup, decides, and passes the winning name down; the child just looks it up.
    """
    if name == "sys.monitoring":
        return count_opcodes_monitoring
    if name == "sys.settrace":
        return count_opcodes_settrace
    return None


def probe() -> Capabilities:
    """Run every check and return the result. Safe to call once at judge startup."""
    baseline_s = _measure_untraced_baseline()

    settrace_report = _probe_settrace(baseline_s)
    monitoring_report = _probe_monitoring(baseline_s)

    # Prefer sys.monitoring when it is usable: same counts, far less overhead, which
    # buys Method B larger input sizes and therefore a better-conditioned fit.
    if monitoring_report.usable:
        chosen = "sys.monitoring"
    elif settrace_report.usable:
        chosen = "sys.settrace"
    else:
        chosen = "none"

    caps = Capabilities(
        python_version=platform.python_version(),
        platform_name=f"{platform.system()} {platform.release()}",
        settrace=settrace_report,
        monitoring=monitoring_report,
        opcode_counter_name=chosen,
        has_reset_peak=hasattr(tracemalloc, "reset_peak"),
        perf_counter_resolution_ns=time.get_clock_info("perf_counter").resolution * 1e9,
        untraced_baseline_us=baseline_s * 1e6,
    )

    try:
        import resource  # noqa: F401  (POSIX only)

        caps.has_setrlimit = True
    except ImportError:
        caps.has_setrlimit = False

    try:
        import psutil  # noqa: F401

        caps.has_psutil = True
    except ImportError:
        caps.has_psutil = False

    caps.docker_ok, caps.docker_note = _probe_docker()
    caps.console_unicode, caps.console_note = _probe_console_unicode()

    # Turn missing capabilities into warnings the operator can actually act on.
    if not caps.method_b_available:
        caps.warnings.append(
            "No working opcode counter: Method B is unavailable, so verdicts will "
            "carry only the wall-clock inference and methods_disagree stays null."
        )
    if not caps.has_reset_peak:
        caps.warnings.append(
            "tracemalloc.reset_peak missing (needs Python 3.9+): auxiliary space "
            "cannot be separated from the input's own footprint, so O(1) space "
            "cannot be checked and 607 will not be reachable."
        )
    if not caps.has_setrlimit:
        caps.warnings.append(
            "No resource.setrlimit on this platform: under the subprocess backend "
            "memory limits are best-effort only (in-child tracemalloc threshold"
            + (", plus psutil polling)." if caps.has_psutil else ", psutil absent).")
            + " This is a documented limitation — see docs/threat-model.md."
        )
    if not caps.docker_ok:
        caps.warnings.append(
            f"Docker unusable ({caps.docker_note}): --backend docker will fall back "
            "to subprocess and report backend=subprocess in the verdict."
        )
    if not caps.console_unicode:
        caps.warnings.append(
            f"Console cannot render Unicode arrows ({caps.console_note}): the wire "
            "logger will use its ASCII markers (->, <-, x) instead. The log stays "
            "complete and greppable; only the glyphs change."
        )
    return caps


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------

def _yes_no(flag: bool) -> str:
    return "yes" if flag else "no"


def _format_mechanism(report: MechanismReport) -> list:
    lines = [f"  {report.name}"]
    if not report.available:
        lines.append(f"    unavailable — {report.note}")
        return lines
    lines.append(
        f"    counts={_yes_no(report.counts)}"
        f"  deterministic={_yes_no(report.deterministic)}"
        f"  scales={_yes_no(report.scales)}"
    )
    if report.counts:
        lines.append(
            f"    n={_PROBE_N_SMALL} -> {report.count_small} opcodes,"
            f"  n={_PROBE_N_LARGE} -> {report.count_large} opcodes,"
            f"  ratio={report.ratio:.2f} (want ~2.00)"
        )
    if report.slowdown:
        lines.append(f"    tracing overhead: {report.slowdown:.1f}x slower than untraced")
    if report.note and report.note != "ok":
        lines.append(f"    note: {report.note}")
    return lines


def format_report(caps: Capabilities) -> str:
    """Human-readable probe report — this is what gets shown on video."""
    lines = [
        "CDAP capability probe",
        "=" * 68,
        f"Python           : {caps.python_version}",
        f"Platform         : {caps.platform_name}",
        f"perf_counter res : {caps.perf_counter_resolution_ns:.1f} ns",
        f"untraced baseline: {caps.untraced_baseline_us:.1f} us for n={_PROBE_N_LARGE}",
        "",
        "Method B — opcode counting mechanisms",
        "-" * 68,
    ]
    lines += _format_mechanism(caps.monitoring)
    lines.append("")
    lines += _format_mechanism(caps.settrace)
    lines.append("")
    lines.append(f"  SELECTED: {caps.opcode_counter_name}")

    lines += [
        "",
        "Measurement and sandbox capabilities",
        "-" * 68,
        f"  tracemalloc.reset_peak (auxiliary space)  : {_yes_no(caps.has_reset_peak)}",
        f"  resource.setrlimit (hard memory cap)      : {_yes_no(caps.has_setrlimit)}",
        f"  psutil (parent-side memory polling)       : {_yes_no(caps.has_psutil)}",
        f"  docker (real cgroup limits, no network)   : {_yes_no(caps.docker_ok)}",
        f"      {caps.docker_note}",
        f"  console renders Unicode wire markers      : {_yes_no(caps.console_unicode)}",
        f"      {caps.console_note}",
    ]

    if caps.warnings:
        lines += ["", "Warnings", "-" * 68]
        for warning in caps.warnings:
            lines.append(f"  ! {warning}")
    else:
        lines += ["", "No warnings — every optional capability is present."]

    return "\n".join(lines)


def main() -> int:
    enable_utf8_output()
    caps = probe()
    print(format_report(caps))
    # Exit non-zero only when something the judge genuinely cannot work without is
    # missing. Absent psutil or Docker are degradations, not failures.
    fatal = not caps.method_b_available and not caps.has_reset_peak
    return 1 if fatal else 0


if __name__ == "__main__":
    raise SystemExit(main())
