"""Regression checks for the fixes recorded in REVIEW_AUDIT_1.md.

Run with ``py -3.14 -m cdap.selftest_audit``.  These tests stay local and do not
start listeners or containers; they protect the security and correctness boundaries
that are easy to regress during a protocol demonstration.
"""

from __future__ import annotations

import io
import json
import threading
import unittest
from types import SimpleNamespace

from .judge.profiler import judge_record
from .judge.runner import load_solution
from .client import CdapClient
from .protocol import Message, WireLog
from .server import (
    Arena,
    ArenaServer,
    CachedResponse,
    Job,
    JobQueue,
    Match,
    MatchState,
    Session,
    State,
    Submission,
    _ClientHandler,
    _is_loopback_host,
)
from .status import Status, Verdict


class AuditRegressionTests(unittest.TestCase):
    @staticmethod
    def _bare_arena_with_players():
        arena = Arena.__new__(Arena)
        arena._lock = threading.RLock()
        arena.log = WireLog(stream=io.StringIO(), use_unicode=False)
        arena.submissions = {}
        arena.matches = {}
        arena._idempotency_cache = {}
        players = []
        for index, user in enumerate(("alice", "bob"), start=1):
            session = Session(f"c-{index}", SimpleNamespace(), arena.log)
            session.user = user
            session.state = State.IN_MATCH
            session.match_id = "m-1"
            players.append(session)
        arena.sessions = {session.id: session for session in players}
        match = Match(
            id="m-1", problem_id="max-subarray",
            session_ids=[session.id for session in players],
            active_session_ids={session.id for session in players},
            duration_s=30, created_at=0, starts_at=0,
            state=MatchState.RUNNING, deadline=30,
        )
        arena.matches[match.id] = match
        return arena, match, players

    def test_earliest_valid_submission_wins_not_first_worker(self) -> None:
        arena, match, players = self._bare_arena_with_players()
        earlier = Submission("s-1", players[0].id, "alice", match.id,
                             "max-subarray", "python", "", 1.0)
        later = Submission("s-2", players[1].id, "bob", match.id,
                           "max-subarray", "python", "", 2.0,
                           verdict={"verdict": int(Verdict.ACCEPTED)})
        arena.submissions = {earlier.id: earlier, later.id: later}
        match.submissions = [earlier.id, later.id]

        arena.resolve_match_after_verdict(match)
        self.assertEqual(MatchState.DRAINING, match.state)
        earlier.verdict = {"verdict": int(Verdict.WRONG_ANSWER)}
        arena.resolve_match_after_verdict(match)
        self.assertEqual(MatchState.ENDED, match.state)
        self.assertEqual("bob", match.winner)

    def test_udp_targets_are_bound_to_live_session_ids(self) -> None:
        server = ArenaServer.__new__(ArenaServer)
        server._udp_lock = threading.Lock()
        server._feed_endpoints = {"alice": {("127.0.0.1", 5051): "c-1"}}
        server.arena = SimpleNamespace(feed_session_is_alive=lambda session_id: session_id == "c-1")
        self.assertEqual({("127.0.0.1", 5051)}, server._feed_targets(["alice"]))

    def test_forfeiter_and_winner_both_leave_match_state(self) -> None:
        arena, match, players = self._bare_arena_with_players()
        arena.forfeit(players[0])
        self.assertEqual(MatchState.ENDED, match.state)
        self.assertEqual("bob", match.winner)
        self.assertEqual(State.IDLE, players[0].state)
        self.assertEqual(State.IDLE, players[1].state)
        self.assertEqual("MATCH_END", players[0].outbox.get_nowait().event)
        self.assertEqual("MATCH_END", players[1].outbox.get_nowait().event)

    def test_deadline_drains_pending_submission(self) -> None:
        arena, match, players = self._bare_arena_with_players()
        pending = Submission("s-1", players[0].id, "alice", match.id,
                             "max-subarray", "python", "", 1.0)
        arena.submissions[pending.id] = pending
        match.submissions = [pending.id]
        arena.expire_matches(31.0)
        self.assertEqual(MatchState.DRAINING, match.state)
        pending.verdict = {"verdict": int(Verdict.ACCEPTED)}
        arena.resolve_match_after_verdict(match)
        self.assertEqual(MatchState.ENDED, match.state)
        self.assertEqual("alice", match.winner)

    def test_invalid_utf8_submission_is_rejected_without_lossy_decode(self) -> None:
        arena, match, players = self._bare_arena_with_players()
        handler = _ClientHandler.__new__(_ClientHandler)
        handler.session = players[0]
        handler.arena = SimpleNamespace(
            current_match=lambda _session: match,
            last_match=lambda _session: None,
            submit_cooldown=0,
        )
        response = handler.handle_submit(Message.request(
            "SUBMIT", headers={"Lang": "python"}, body=b"def solve():\n\xff", seq=1,
        ))
        self.assertEqual(int(Status.BAD_REQUEST), response.status)
        self.assertEqual("INVALID_SOURCE_ENCODING", response.phrase)

    def test_reconnected_user_can_read_own_submission(self) -> None:
        submission = Submission(
            "s-1", "old-session", "alice", "m-1", "max-subarray", "python", "", 1.0,
        )
        session = Session("new-session", SimpleNamespace(), WireLog(stream=io.StringIO()))
        session.user = "alice"
        session.state = State.IDLE
        handler = _ClientHandler.__new__(_ClientHandler)
        handler.session = session
        handler.arena = SimpleNamespace(get_submission=lambda _submission_id: submission)
        response = handler.handle_get_submission(Message.request(
            "GET_SUBMISSION", body=json.dumps({"submission": "s-1"}), seq=1,
        ))
        self.assertEqual(int(Status.ACCEPTED), response.status)

    def test_request_id_replays_before_changed_state_and_detects_conflict(self) -> None:
        arena, _match, players = self._bare_arena_with_players()
        session = players[0]
        session.state = State.IN_MATCH  # QUEUE would now fail its ordinary state check.
        handler = _ClientHandler.__new__(_ClientHandler)
        handler.arena = arena
        handler.session = session
        handler.log = arena.log
        handler._deferred = []
        request = Message.request("QUEUE", headers={"Request-Id": "retry-1"}, seq=7)
        fingerprint = handler._request_fingerprint(request)
        arena.remember_request("alice", "retry-1", CachedResponse(
            fingerprint, int(Status.ACCEPTED), "QUEUED",
            {"Queue-Pos": "1"}, b"", True,
        ))

        replay, keep_open = handler._dispatch(request)
        self.assertTrue(keep_open)
        self.assertEqual(int(Status.ACCEPTED), replay.status)
        self.assertEqual("true", replay.headers.get("Idempotent-Replay"))
        conflicting = Message.request(
            "QUEUE", headers={"Request-Id": "retry-1"}, body=b"different", seq=8,
        )
        conflict, _keep_open = handler._dispatch(conflicting)
        self.assertEqual(int(Status.CONFLICT), conflict.status)
        self.assertEqual("IDEMPOTENCY_CONFLICT", conflict.phrase)

    def test_full_outbox_preserves_verdict_over_progress(self) -> None:
        session = Session("test", SimpleNamespace(), WireLog(stream=io.StringIO()))
        for _index in range(256):
            session.push_event("JUDGE_PROGRESS")
        session.push_event("VERDICT")
        events = [session.outbox.get_nowait().event for _index in range(256)]
        self.assertIn("VERDICT", events)
        self.assertEqual(255, events.count("JUDGE_PROGRESS"))

    def test_client_detects_event_gap_and_schedules_one_resync(self) -> None:
        client = CdapClient("127.0.0.1", 1, WireLog(stream=io.StringIO()))
        client._handle_event(Message.make_event("NOTICE", headers={"Event-Id": 3}))
        self.assertTrue(client._resync_pending)
        self.assertEqual(3, client._last_event_id)
        self.assertIsNotNone(client._actions.get_nowait())

    def test_deferred_commit_runs_even_when_response_send_fails(self) -> None:
        class FailingConnection:
            def __init__(self):
                self.first = True

            def recv(self):
                if self.first:
                    self.first = False
                    return Message.request("QUEUE", seq=1)
                return None

            @staticmethod
            def send(_message):
                raise OSError("peer vanished")

        committed = []
        handler = _ClientHandler.__new__(_ClientHandler)
        handler.server = SimpleNamespace(stopping=False)
        handler.session = SimpleNamespace(label="alice")
        handler.conn = FailingConnection()
        handler.log = WireLog(stream=io.StringIO())
        handler._deferred = [lambda: committed.append(True)]
        handler._dispatch = lambda message: (Message.response(Status.OK, seq=message.seq), True)
        handler.serve()
        self.assertEqual([True], committed)

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

    def test_wire_log_never_prints_submission_source(self) -> None:
        stream = io.StringIO()
        log = WireLog(stream=stream, verbose=True, use_unicode=False)
        source = "def solve(nums):\n    return 987654321\n"
        log.received(Message.request("SUBMIT", body=source, seq=1))
        text = stream.getvalue()
        self.assertNotIn("987654321", text)
        self.assertIn("<source redacted sha256=", text)

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
