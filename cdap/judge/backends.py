"""Execution backends — where a submission actually runs, behind one interface.

A backend answers one question: *given a job, run it in isolation and hand back the
measurement record.* Two implement that question with very different isolation, and the
report's security experiment is a direct comparison of them:

* ``SubprocessBackend`` (this file, default) — a fresh Python child per submission. No
  container, so no setup cost and it runs anywhere, but the isolation is only as strong as
  what one OS process can deny another. On Windows that is notably weak (no ``setrlimit``,
  no cgroups), which is stated openly rather than hidden.
* ``DockerBackend`` (Phase 8) — the same child inside ``python:3.14-slim`` with
  ``--network none``, a real memory cgroup, and a pid cap. Slower to start, genuinely
  isolated at the kernel.

Both run the *same* ``cdap.judge.runner`` child. Only the box around it differs, which is
what makes the comparison fair: identical measurement code, different walls.

The four protections this backend provides
------------------------------------------
1. **A fresh process per submission.** No submission can see another's globals, and a
   crash takes down only its own child.
2. **A wall-clock hard kill.** The parent, not the child, holds the stopwatch — a child
   wedged in a busy loop cannot be trusted to time itself out. Past the deadline the whole
   process tree is killed and the job is ``602 TIME_LIMIT_EXCEEDED``.
3. **An output cap.** stdout and stderr are truncated at 64 KB, so a ``print`` loop fills a
   buffer, not the disk.
4. **Result-channel integrity.** The verdict is read from the last ``__CDAP_RESULT__`` line
   only. A submission that prints a forged result produces a line the parser skips, because
   the harness always appends the genuine one after the submission has returned.

What this backend does **not** provide, and says so
---------------------------------------------------
Real memory and CPU limits. On POSIX the child sets an address-space ``rlimit`` on itself;
on Windows there is no equivalent, so a memory limit is best-effort (an in-child
``tracemalloc`` threshold, plus optional ``psutil`` polling from the parent). CPU is bounded
only by the wall-clock kill. These are not oversights — they are the reason the Docker
backend exists, and the reason the threat model names ``subprocess`` as the weaker box.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

from .runner import RESULT_SENTINEL

#: Truncate captured stdout/stderr at this many bytes. A correct solution prints nothing;
#: this only bounds a misbehaving one.
MAX_OUTPUT_BYTES = 64 * 1024

#: How long past the job's own wall-clock allowance the parent waits before killing the
#: child tree. The child does its own per-call timing; this is the backstop for a child
#: that has stopped cooperating entirely (a busy loop, a deadlock). The measurement ladder
#: can legitimately take a while, so the backstop is generous and additive.
KILL_GRACE_S = 10.0


@dataclass
class RunResult:
    """What a backend returns for one job — the record, plus how the run itself went.

    ``result`` is the harness's measurement dict (or None if none arrived). ``ok`` is about
    the *run*, not the verdict: a solution that is wrong but ran cleanly still has
    ``ok=True`` and a ``result`` describing the wrong answer. ``ok=False`` means the backend
    itself could not get an answer — a timeout kill, a crashed child, an unparseable channel.
    """

    ok: bool
    backend: str                       # the backend that actually ran — never a lie (invariant 6)
    result: Optional[dict] = None
    timed_out: bool = False
    killed: bool = False
    exit_code: Optional[int] = None
    wall_ms: float = 0.0
    stdout: str = ""
    stderr: str = ""
    error: str = ""                    # why the *backend* failed, when ok is False

    def outcome_hint(self) -> str:
        """The outcome the worker should map to a verdict, considering the run itself.

        A timeout outranks whatever the child managed to say, because a child killed
        mid-measurement may have emitted a partial or stale record. Everything else defers
        to the harness's own ``outcome`` field.
        """
        if self.timed_out:
            return "time_limit_exceeded"
        if not self.ok:
            return "judge_error"
        if self.result:
            return self.result.get("outcome", "judge_error")
        return "judge_error"


class Backend:
    """The interface every backend implements. One method, one job, one result."""

    name = "base"

    def run(self, job: dict, time_limit_ms: int) -> RunResult:
        raise NotImplementedError

    def available(self) -> tuple:
        """``(is_available, reason)``. The subprocess backend is always available; Docker
        is not, and the worker uses this to fall back truthfully."""
        return True, ""


class SubprocessBackend(Backend):
    """Run each job in a fresh ``python -m cdap.judge.runner`` child process."""

    name = "subprocess"

    def __init__(self, python: Optional[str] = None, repo_root: Optional[Path] = None):
        # sys.executable, not a bare "python", so the child is the *same* interpreter the
        # server is running — the one the capability probe actually measured. A different
        # python on PATH could have a different opcode-counting story entirely.
        self.python = python or sys.executable
        # The child runs with its cwd set to a throwaway temp dir (see run()), so it cannot
        # find the cdap package by cwd. We put the repo root on PYTHONPATH instead. Three
        # parents up from cdap/judge/backends.py is the repo root.
        self.repo_root = repo_root or Path(__file__).resolve().parents[2]

    def run(self, job: dict, time_limit_ms: int) -> RunResult:
        payload = json.dumps(job).encode("utf-8")
        deadline_s = max(1.0, time_limit_ms / 1000.0) + KILL_GRACE_S

        # A fresh, empty working directory per run: the child starts with nothing to read,
        # so a solution that tries to open a file in "." finds an empty directory rather
        # than the repo. Removed regardless of how the run ends.
        workdir = tempfile.mkdtemp(prefix=".cdap-run-")
        env = self._child_env()

        start = time.perf_counter()
        try:
            completed = self._spawn(payload, workdir, env, deadline_s)
        finally:
            _rmtree_quiet(workdir)
        wall_ms = (time.perf_counter() - start) * 1000.0

        if completed.timed_out:
            return RunResult(
                ok=False,
                backend=self.name,
                timed_out=True,
                killed=True,
                wall_ms=wall_ms,
                stdout=completed.stdout,
                stderr=completed.stderr,
                error=f"killed after {deadline_s:.1f}s wall-clock",
            )

        result = _extract_result(completed.stdout)
        if result is None:
            # No sentinel line. The child crashed, was killed by the OS (a POSIX rlimit
            # kill lands here), or died before it could report. That is a judge-side
            # failure, not a player verdict — the worker will send 612, or 603 if stderr
            # shows the tell-tale MemoryError.
            return RunResult(
                ok=False,
                backend=self.name,
                exit_code=completed.returncode,
                wall_ms=wall_ms,
                stdout=completed.stdout,
                stderr=completed.stderr,
                error=self._diagnose_missing_result(completed),
            )

        return RunResult(
            ok=True,
            backend=self.name,
            result=result,
            exit_code=completed.returncode,
            wall_ms=wall_ms,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )

    # -- internals ---------------------------------------------------------

    def _child_env(self) -> Dict[str, str]:
        env = dict(os.environ)
        # Prepend the repo root so `-m cdap.judge.runner` resolves from the temp cwd.
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = (
            str(self.repo_root) + (os.pathsep + existing if existing else "")
        )
        # Deterministic hashing keeps set/dict iteration order stable across child runs,
        # so a measurement is reproducible for the report. Unbuffered I/O means the
        # sentinel line is not stranded in a buffer if the child is killed late.
        env["PYTHONHASHSEED"] = "0"
        env["PYTHONUNBUFFERED"] = "1"
        return env

    def _spawn(self, payload: bytes, workdir: str, env: dict, deadline_s: float):
        """Start the child, feed it the job, and enforce the deadline with a hard kill."""
        proc = subprocess.Popen(
            [self.python, "-m", "cdap.judge.runner"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=workdir,
            env=env,
            # New process group so a kill reaches the whole tree — a child that spawned
            # its own children (which the guard forbids, but --no-ast-guard permits) does
            # not get to orphan them past the deadline.
            **_process_group_kwargs(),
        )

        timed_out = False
        try:
            out, err = proc.communicate(input=payload, timeout=deadline_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_tree(proc)
            # Drain whatever the child produced before the kill; ignore a second timeout.
            try:
                out, err = proc.communicate(timeout=5.0)
            except subprocess.TimeoutExpired:
                out, err = b"", b""

        return _Completed(
            stdout=_decode_cap(out),
            stderr=_decode_cap(err),
            returncode=proc.returncode,
            timed_out=timed_out,
        )

    def _diagnose_missing_result(self, completed) -> str:
        """Turn a resultless child into a one-line reason, guessing at the common causes."""
        tail = completed.stderr.strip().splitlines()[-1:] if completed.stderr.strip() else []
        hint = tail[0] if tail else "no output on stderr"
        if "MemoryError" in completed.stderr:
            return f"child ran out of memory: {hint}"
        return (
            f"child exited with code {completed.returncode} and produced no result line "
            f"(last stderr: {hint})"
        )


@dataclass
class _Completed:
    stdout: str
    stderr: str
    returncode: Optional[int]
    timed_out: bool


# --------------------------------------------------------------------------
# Free functions — kept module-level so they can be unit-tested without a backend
# --------------------------------------------------------------------------

def _extract_result(stdout: str) -> Optional[dict]:
    """Return the parsed dict from the **last** sentinel line, or None if there is none.

    Scanning from the end is the whole defence: the genuine result is appended after the
    submission has run, so it is always the last sentinel-bearing line. A submission that
    prints ``__CDAP_RESULT__{...}`` earlier only plants a line the scan passes over.
    """
    for line in reversed(stdout.splitlines()):
        marker = line.find(RESULT_SENTINEL)
        if marker != -1:
            payload = line[marker + len(RESULT_SENTINEL):]
            try:
                return json.loads(payload)
            except ValueError:
                # A corrupted sentinel line: keep scanning upward. A truncated final line
                # (output cap hit mid-JSON) can then fall back to an earlier intact one.
                continue
    return None


def _decode_cap(raw: bytes) -> str:
    """Decode captured bytes to text, truncating past the output cap with a marker."""
    if len(raw) > MAX_OUTPUT_BYTES:
        raw = raw[:MAX_OUTPUT_BYTES]
        suffix = b"\n...[truncated at 64 KB]..."
        raw = raw + suffix
    return raw.decode("utf-8", errors="replace")


def _rmtree_quiet(path: str) -> None:
    import shutil

    shutil.rmtree(path, ignore_errors=True)


def _process_group_kwargs() -> dict:
    """Popen kwargs that isolate the child into its own killable process group.

    POSIX and Windows spell this completely differently, so it is localized here rather
    than smeared through _spawn. On POSIX, start_new_session puts the child in a new
    session/group; on Windows, the CREATE_NEW_PROCESS_GROUP flag is the nearest analogue.
    """
    if os.name == "posix":
        return {"start_new_session": True}
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {}


def _kill_tree(proc) -> None:
    """Kill the child and any processes it started, on either platform.

    A plain ``proc.kill()`` reaches only the direct child. A submission run under
    ``--no-ast-guard`` can spawn (that is precisely what ``evil_fork.py`` does), so the
    whole group must go — otherwise a fork bomb outlives the timeout.
    """
    if proc.poll() is not None:
        return
    try:
        if os.name == "posix":
            import signal

            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        elif os.name == "nt":
            # taskkill /T reaches the child's descendants; /F forces it. This is the
            # reliable tree-kill on Windows, where there is no killpg.
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        else:
            proc.kill()
    except (ProcessLookupError, PermissionError, OSError):
        # Already gone, or we lack the rights: fall back to the direct kill and move on.
        try:
            proc.kill()
        except OSError:
            pass


# --------------------------------------------------------------------------
# Selection
# --------------------------------------------------------------------------

def make_backend(name: str) -> Backend:
    """Return a backend by name, falling back to subprocess **and saying so**.

    Invariant 6 in CLAUDE.md: the backend that ran must be reported truthfully in the
    verdict. So this never silently substitutes — a caller asking for ``docker`` when no
    daemon is reachable gets a ``SubprocessBackend`` whose ``.name`` is still
    ``"subprocess"``, and the difference is visible in the result. The experiment's
    conclusions depend on that field not lying.

    ``DockerBackend`` arrives in Phase 8. Until then asking for it lands on the same
    honest fallback path a missing daemon would, which is the behaviour the invariant
    demands anyway.
    """
    if name == "subprocess":
        return SubprocessBackend()

    if name == "docker":
        docker_backend = globals().get("DockerBackend")
        if docker_backend is None:
            return SubprocessBackend()
        backend = docker_backend()
        ok, _reason = backend.available()
        return backend if ok else SubprocessBackend()

    raise ValueError(f"unknown backend {name!r}; expected 'subprocess' or 'docker'")


def main() -> int:
    """``python -m cdap.judge.backends <file.py> [problem]`` — judge one file, print JSON.

    A thin harness for eyeballing a backend end to end without the server: it loads a
    source file, wraps it in a job for the given problem (``max-subarray`` by default),
    runs it through the subprocess backend, and prints the raw measurement record. The
    profiler CLI in Phase 4 builds the human-readable verdict on top of this.
    """
    from .. import capabilities
    from ..problems import get_problem, problem_ids

    capabilities.enable_utf8_output()
    if not 2 <= len(sys.argv) <= 3:
        print("usage: python -m cdap.judge.backends <file.py> [problem-id]")
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
    run = backend.run(job, time_limit_ms=problem.contract.time_limit_ms)

    print(f"[backend={run.backend}] ok={run.ok} wall={run.wall_ms:.0f}ms "
          f"timed_out={run.timed_out} exit={run.exit_code}")
    if run.stderr.strip():
        print("--- child stderr ---")
        print(run.stderr.rstrip())
        print("--- end stderr ---")
    print(json.dumps(run.result if run.ok else {"error": run.error}, indent=2))
    return 0 if run.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
