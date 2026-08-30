"""Regression checks for the fixes recorded in REVIEW_AUDIT_1.md.

Run with ``py -3.14 -m cdap.selftest_audit``.  These tests stay local and do not
start listeners or containers; they protect the security and correctness boundaries
that are easy to regress during a protocol demonstration.
"""

from __future__ import annotations

import io
import threading
import unittest
from types import SimpleNamespace

from .judge.profiler import judge_record
from .judge.runner import load_solution
from .protocol import Message, WireLog
from .server import Job, JobQueue, Session, _ClientHandler, _is_loopback_host
from .status import Status, Verdict


class AuditRegressionTests(unittest.TestCase):
    def test_performance_policy_accepts_complete_hidden_evidence(self) -> None:
        record = {
            "outcome": "tests_passed",
            "judge_policy": "performance",
            "profiled": False,
            "tests": {"summary": "3/3", "failures": []},
            "performance": {
                "complete": True,
                "policy_version": "performance-v1",
                "cpu_ms": 12.0,
                "wall_ms": 13.0,
                "peak_aux_kb": 1.0,
            },
        }
        result = judge_record(record, {
            "required_time": "O(n)", "required_space": "O(1)", "mem_limit_kb": 65536,
        })
        self.assertEqual(int(Verdict.ACCEPTED), result["verdict"])
        self.assertEqual("performance_limits", result["decision_basis"])

    def test_submission_gets_private_builtins(self) -> None:
        import builtins

        original_print = builtins.print
        function = load_solution(
            "__builtins__['print'] = lambda *args, **kwargs: None\n"
            "def solve(nums):\n"
            "    return len(nums)\n",
            "solve",
        )
        self.assertEqual(3, function([1, 2, 3]))
        self.assertIs(original_print, builtins.print)

    def test_wire_log_redacts_and_escapes(self) -> None:
        stream = io.StringIO()
        log = WireLog(stream=stream, verbose=True, use_unicode=False)
        message = Message.request(
            "LOGIN", headers={"Token": "bearer-secret", "Detail": "bad\x1b]52;c;evil"},
            body=b'{"user":"alice","pass":"hunter2"}', seq=1,
        )
        log.sent(message)
        text = stream.getvalue()
        self.assertNotIn("hunter2", text)
        self.assertNotIn("bearer-secret", text)
        self.assertIn("<redacted>", text)
        self.assertIn("\\x1b", text)

    def test_bounded_queue_never_over_reserves(self) -> None:
        jobs = JobQueue(max_pending=1)
        self.assertEqual(1, jobs.reserve())
        self.assertIsNone(jobs.reserve())
        jobs.put_reserved(Job("s-1", "fib", {}))
        self.assertEqual("s-1", jobs.get(timeout=0).submission_id)
        self.assertEqual(1, jobs.reserve())

    def test_profile_with_incomplete_space_cannot_be_accepted(self) -> None:
        record = {
            "outcome": "tests_passed", "profiled": True,
            "time": {"complete": True, "measurable": True, "samples_ms":
                     {"1": 1, "2": 2, "4": 4, "8": 8}, "usable_sizes": [1, 2, 4, 8]},
            "ops": {},
            "space": {"complete": False, "samples_kb": {"1": 1},
                      "notes": ["space measurement failed at n=8: RuntimeError"]},
            "tests": {"summary": "1/1", "failures": []},
        }
        result = judge_record(record, {"required_time": "O(n)", "required_space": "O(1)",
                                       "mem_limit_kb": 65536})
        self.assertEqual(int(Verdict.JUDGE_ERROR), result["verdict"])

    def test_remote_policy_only_treats_loopback_as_local(self) -> None:
        self.assertTrue(_is_loopback_host("127.0.0.1"))
        self.assertTrue(_is_loopback_host("localhost"))
        self.assertFalse(_is_loopback_host("0.0.0.0"))
        self.assertFalse(_is_loopback_host("192.168.1.9"))

    def test_workers_disabled_status_is_declared(self) -> None:
        from .status import phrase_for
        self.assertEqual("WORKERS_DISABLED", phrase_for(Status.JUDGE_UNAVAILABLE,
                                                          "WORKERS_DISABLED"))

    def test_empty_worker_token_is_rejected_before_worker_registration(self) -> None:
        handler = _ClientHandler.__new__(_ClientHandler)
        handler.arena = SimpleNamespace(worker_token="")
        _worker, response = handler._worker_identity(Message.request(
            "WORKER_REGISTER", headers={"Worker": "w1", "Worker-Token": ""}, seq=1,
        ))
        self.assertEqual(int(Status.JUDGE_UNAVAILABLE), response.status)
        self.assertEqual("WORKERS_DISABLED", response.phrase)

    def test_concurrent_event_producers_preserve_event_id_delivery_order(self) -> None:
        session = Session("test", SimpleNamespace(), WireLog(stream=io.StringIO(), use_unicode=False))
        threads = [threading.Thread(target=session.push_event, args=("NOTICE",)) for _ in range(32)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        ids = [session.outbox.get_nowait().headers.get_int("Event-Id") for _ in threads]
        self.assertEqual(list(range(1, 33)), ids)


if __name__ == "__main__":
    unittest.main(verbosity=2)
