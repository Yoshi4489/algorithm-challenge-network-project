"""Focused checks for the compact player display; no ports or server required."""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout

from .client import PlayerView
from .protocol import Message, WireLog


class CompactViewTests(unittest.TestCase):
    def test_unchanged_udp_snapshots_do_not_scroll(self) -> None:
        output = io.StringIO()
        view = PlayerView()
        with redirect_stdout(output):
            view.udp_update("CLOCK", {"match": "m-1", "remain": "125000"})
            view.udp_update("TICK", {"match": "m-1", "player": "alice", "passed": "0",
                                      "total": "10", "subs": "0"})
            for _ in range(1_000):
                view.udp_update("CLOCK", {"match": "m-1", "remain": "125000"})
                view.udp_update("TICK", {"match": "m-1", "player": "alice", "passed": "0",
                                          "total": "10", "subs": "0"})
            view.udp_update("CLOCK", {"match": "m-1", "remain": "60000"})
            view.udp_update("CLOCK", {"match": "m-1", "remain": "60000"})

        rendered = output.getvalue()
        self.assertEqual(rendered.count("Time remaining:"), 2)
        self.assertEqual(rendered.count("Score update:"), 1)

    def test_wire_log_can_be_quiet_without_losing_lifecycle_notes(self) -> None:
        output = io.StringIO()
        log = WireLog(stream=output, use_unicode=False, wire=False)
        log.sent(Message.request("HELLO", seq=1))
        log.udp_received("CLOCK", {"match": "m-1", "seq": 1, "remain": 30_000})
        log.note("compact mode is active")
        rendered = output.getvalue()
        self.assertNotIn("HELLO", rendered)
        self.assertNotIn("CLOCK", rendered)
        self.assertIn("compact mode is active", rendered)

    def test_countdown_stops_when_match_starts(self) -> None:
        output = io.StringIO()
        view = PlayerView()
        with redirect_stdout(output):
            view.start_countdown(2_000)
            view.match_start({"Match": "m-1", "Problem": "fib", "Duration-Ms": "30000",
                              "Required-Time": "O(n)", "Required-Space": "O(1)"})
        self.assertIn("Match begins in 2", output.getvalue())
        self.assertIn("MATCH START", output.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
