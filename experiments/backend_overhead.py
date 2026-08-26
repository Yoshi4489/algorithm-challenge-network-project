"""Measure backend latency and demonstrate which sandbox layer is the boundary.

Run with ``python -m experiments.backend_overhead``. Use ``--quick`` for one latency run
per backend. Security probes always run once and always disable the AST guard deliberately.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import socket
import statistics
import sys
import threading
from pathlib import Path

from cdap import capabilities
from cdap.judge.backends import DockerBackend, SubprocessBackend
from cdap.judge.runner import run_budget_ms
from cdap.problems import get_problem

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "experiments" / "out" / "backend_overhead.json"


def job_for(path: Path, *, guard: bool, security_probe: bool = False) -> dict:
    return {
        "problem": "max-subarray",
        "source": path.read_text(encoding="utf-8"),
        "guard": guard,
        "profile": False,
        "opcode_counter": "none",
        "report_security_probe": security_probe,
    }


def measure_backend(backend, repeats: int) -> dict:
    problem = get_problem("max-subarray")
    job = job_for(ROOT / "samples" / "max_subarray_on.py", guard=True)
    times = []
    outcomes = []
    for _ in range(repeats):
        run = backend.run(job, run_budget_ms(problem.contract.time_limit_ms))
        times.append(run.wall_ms)
        outcomes.append((run.result or {}).get("outcome", run.outcome_hint()))
    mean_ms = statistics.mean(times)
    return {
        "backend": backend.name,
        "runs": repeats,
        "samples_ms": [round(value, 1) for value in times],
        "mean_ms": round(mean_ms, 1),
        "median_ms": round(statistics.median(times), 1),
        "throughput_per_s": round(1000.0 / mean_ms, 3) if mean_ms else None,
        "outcomes": outcomes,
    }


def local_listener():
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(4)
    listener.settimeout(5.0)
    accepted = []

    def receive():
        try:
            connection, _ = listener.accept()
            accepted.append(True)
            connection.close()
        except OSError:
            pass

    thread = threading.Thread(target=receive, daemon=True)
    thread.start()
    return listener, thread, accepted


def security_run(backend, sample: str) -> dict:
    problem = get_problem("max-subarray")
    listener = thread = accepted = None
    old_host = os.environ.get("CDAP_PROBE_HOST")
    old_port = os.environ.get("CDAP_PROBE_PORT")
    old_path = os.environ.get("CDAP_HOST_PROBE_PATH")
    try:
        if sample == "evil_socket.py":
            listener, thread, accepted = local_listener()
            os.environ["CDAP_PROBE_HOST"] = "127.0.0.1"
            os.environ["CDAP_PROBE_PORT"] = str(listener.getsockname()[1])
        if sample == "evil_open.py":
            os.environ["CDAP_HOST_PROBE_PATH"] = str(Path(sys.executable).resolve().parent)
        run = backend.run(
            job_for(ROOT / "samples" / sample, guard=False, security_probe=True),
            run_budget_ms(problem.contract.time_limit_ms),
        )
    finally:
        if listener is not None:
            listener.close()
        if thread is not None:
            thread.join(timeout=1.0)
        _restore_env("CDAP_PROBE_HOST", old_host)
        _restore_env("CDAP_PROBE_PORT", old_port)
        _restore_env("CDAP_HOST_PROBE_PATH", old_path)
    probe = (run.result or {}).get("security_probe", {})
    return {
        "backend": backend.name,
        "sample": sample,
        "escaped": bool(probe.get("escaped")),
        "spawned": int(probe.get("spawned", 0)),
        "listener_reached": bool(accepted),
        "outcome": (run.result or {}).get("outcome", run.outcome_hint()),
        "wall_ms": round(run.wall_ms, 1),
        "error": run.error,
    }


def _restore_env(name: str, value) -> None:
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quick", action="store_true", help="one latency run per backend")
    parser.add_argument("--repeats", type=int, default=3, help="latency runs per backend")
    args = parser.parse_args(argv)
    repeats = 1 if args.quick else max(1, args.repeats)
    capabilities.enable_utf8_output()

    backends = [SubprocessBackend()]
    docker = DockerBackend()
    docker_ok, docker_reason = docker.available()
    if docker_ok:
        backends.append(docker)

    timing = [measure_backend(backend, repeats) for backend in backends]
    security = []
    for backend in backends:
        for sample in ("evil_socket.py", "evil_open.py", "evil_fork.py"):
            security.append(security_run(backend, sample))

    payload = {
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "docker_available": docker_ok,
            "docker_note": docker_reason,
        },
        "timing": timing,
        "security": security,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    print("CDAP backend overhead")
    for row in timing:
        print(f"  {row['backend']:10} mean={row['mean_ms']:8.1f} ms  "
              f"throughput={row['throughput_per_s']} submissions/s")
    print("\nAST guard OFF - security probes")
    for row in security:
        result = "ESCAPED" if row["escaped"] else "BLOCKED/CONFINED"
        print(f"  {row['backend']:10} {row['sample']:16} {result:18} "
              f"spawned={row['spawned']}")
    print(f"\nJSON: {OUT}")

    if docker_ok:
        subprocess_rows = [row for row in security if row["backend"] == "subprocess"]
        docker_rows = [row for row in security if row["backend"] == "docker"]
        assert all(row["escaped"] for row in subprocess_rows), subprocess_rows
        assert not any(row["escaped"] for row in docker_rows), docker_rows
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
