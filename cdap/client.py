"""CDAP player client — the other half of the protocol, and the demo driver.

This is what a player runs. It connects to the arena over TCP, performs the handshake,
and then does two things at once: answer whatever the person at the keyboard types, and
react to events the server pushes without being asked.

The one design problem a client of this protocol has to solve
-------------------------------------------------------------
The server sends two kinds of frame down one socket: **responses**, which answer a request
this client made, and **events**, which arrive whenever the server has news. A single
socket, two purposes, and no way to know which is next.

CDAP makes that decidable with one rule, and the rule is why the reader below is short:

* a **response** carries the ``Seq`` of the request it answers;
* an **event** carries no ``Seq`` at all, only an ``Event-Id``.

So exactly one reader thread pulls frames off the socket and looks at ``Seq``. If there is
one, some caller is blocked waiting for that number — hand it over and wake them. If there
is none, it is an event: render it, and let the event machinery decide whether anything
should follow. No guessing, no ambiguity, and nothing that breaks when a ``VERDICT``
happens to arrive in the middle of a ``GET_PROBLEM`` round trip. That is design invariant 4
seen from the receiving end.

Three threads, one job each
---------------------------
``main``
    Reads commands the player types and sends requests. Blocks on ``input()``.
``reader``
    The only thread that touches ``recv``. Routes each frame by ``Seq``, as above.
``agent``
    Runs the follow-up work an event asks for.

That third thread is not optional, and the reason is the most interesting bug in this file.
``--submit`` means "submit this file when the match starts", so a ``MATCH_START`` event has
to trigger a ``SUBMIT`` request. If the reader thread sent that request itself it would then
have to wait for the ``202`` — and the only thread that can read the ``202`` off the socket
is the reader thread, which is now blocked waiting for itself. A deadlock, arrived at by
writing the obvious thing. So the reader never sends anything that needs an answer: it
queues the work and goes straight back to reading.

Printing and compact play
-------------------------
The server always prints every frame. The client defaults to a compact player view, because
printing three UDP snapshots every quarter second hides the information a player needs.
``--wire`` restores the complete client transcript (with code and phrase); ``-v`` adds full
headers and bodies to that transcript.
"""

from __future__ import annotations

import argparse
import json
import queue
import random
import socket
import sys
import threading
import time
from typing import Callable, Dict, List, Optional

from . import capabilities
from .protocol import (
    PROTOCOL_VERSION,
    Connection,
    FrameTooLarge,
    Kind,
    Message,
    ProtocolError,
    WireLog,
    LatestWins,
    decode_datagram,
    encode_datagram,
)
from .status import Status, describe_status, format_status, is_success

#: How long a caller waits for a response before giving up. Generous, because a response
#: only ever confirms receipt — judging happens afterwards and arrives as an event — so
#: anything approaching this timeout means the connection is in trouble, not that the
#: server is thinking.
RESPONSE_TIMEOUT_S = 30.0

#: A version string the arena is guaranteed not to speak, used by ``--bad-version``.
#: Hard-coded rather than computed: the flag exists to produce ``426 VERSION_UNSUPPORTED``
#: on camera, and it should do that whatever the real version becomes.
WRONG_VERSION = "CDAP/9.9"

#: Events that mean "the match is over" — the client stops waiting for a verdict on them.
TERMINAL_EVENTS = ("MATCH_END", "SERVER_SHUTDOWN")


class PlayerView:
    """A quiet, persistent player-facing view over CDAP's noisy display feed.

    UDP is intentionally frequent so a lossy receiver quickly converges on current state.
    Printing every snapshot is useful for a protocol demonstration but unusable while a
    person is trying to play.  This class keeps the protocol unchanged and turns the feed
    into notices that matter to a player.
    """

    _MILESTONES_MS = (120_000, 60_000, 30_000, 10_000, 5_000, 4_000, 3_000, 2_000, 1_000)

    def __init__(self, *, compact: bool = True):
        self.compact = compact
        self._lock = threading.Lock()
        self._countdown_stop: Optional[threading.Event] = None
        self._countdown_thread: Optional[threading.Thread] = None
        self._last_remaining: Dict[str, int] = {}
        self._announced_milestones: Dict[str, set[int]] = {}
        self._scores: Dict[str, Dict[str, tuple[int, int, int]]] = {}

    def close(self) -> None:
        self.stop_countdown()

    def _print(self, text: str = "") -> None:
        with self._lock:
            print(text, flush=True)

    def banner(self, title: str, *lines: str) -> None:
        self._print()
        self._print(f"  ===== {title} =====")
        for line in lines:
            self._print(f"  {line}")
        self._print()

    def match_found(self, headers) -> None:
        match = headers.get("Match", "?")
        opponents = headers.get("Opponents") or headers.get("Detail", "opponent unknown")
        self.banner("MATCH FOUND", f"match      : {match}", f"opponents  : {opponents}")
        try:
            start_in_ms = max(0, int(headers.get("Start-In-Ms", "0")))
        except (TypeError, ValueError):
            start_in_ms = 0
        if start_in_ms:
            self.start_countdown(start_in_ms)

    def start_countdown(self, start_in_ms: int) -> None:
        self.stop_countdown()
        stop = threading.Event()
        self._countdown_stop = stop
        seconds = max(1, (start_in_ms + 999) // 1000)

        def run() -> None:
            for remaining in range(seconds, 0, -1):
                self._print(f"  Match begins in {remaining}..." )
                if stop.wait(1.0):
                    return

        self._countdown_thread = threading.Thread(target=run, name="match-countdown",
                                                  daemon=True)
        self._countdown_thread.start()

    def stop_countdown(self) -> None:
        if self._countdown_stop is not None:
            self._countdown_stop.set()
        thread = self._countdown_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=0.2)
        self._countdown_stop = None
        self._countdown_thread = None

    def match_start(self, headers) -> None:
        self.stop_countdown()
        duration = self._format_ms(headers.get("Duration-Ms"))
        self.banner(
            "MATCH START",
            f"match      : {headers.get('Match', '?')}",
            f"problem    : {headers.get('Problem', headers.get('Detail', '?'))}",
            f"clock      : {duration}",
            f"contract   : time {headers.get('Required-Time', '?')}, "
            f"space {headers.get('Required-Space', '?')}",
        )

    def terminal(self, name: str, headers) -> None:
        self.stop_countdown()
        match = headers.get("Match")
        if match:
            self.clear_match(match)
        self.banner(name, headers.get("Detail", ""))

    def warning(self, text: str) -> None:
        self._print(f"  ! UDP warning: {text}")

    def clear_match(self, match: str) -> None:
        with self._lock:
            self._last_remaining.pop(match, None)
            self._announced_milestones.pop(match, None)
            self._scores.pop(match, None)

    def verdict(self, lines: List[str]) -> None:
        """One atomic presentation block; background feed output cannot split it."""
        with self._lock:
            print("", flush=True)
            for line in lines:
                print(line, flush=True)
            print("", flush=True)

    @staticmethod
    def _format_ms(value) -> str:
        try:
            seconds = max(0, (int(value) + 999) // 1000)
        except (TypeError, ValueError):
            return "?"
        return f"{seconds // 60}:{seconds % 60:02d}"

    def udp_update(self, kind: str, fields: dict) -> None:
        """Render changed state and time milestones; ignore unchanged snapshots."""
        match = fields.get("match")
        if not match:
            return
        if kind == "CLOCK":
            try:
                remaining = max(0, int(fields["remain"]))
            except (KeyError, TypeError, ValueError):
                self._print("  ! UDP CLOCK ignored: invalid remaining time")
                return
            previous = self._last_remaining.get(match)
            self._last_remaining[match] = remaining
            if previous is None:
                self._print(f"  Time remaining: {self._format_ms(remaining)}")
                return
            seen = self._announced_milestones.setdefault(match, set())
            for milestone in self._MILESTONES_MS:
                if milestone not in seen and previous > milestone >= remaining:
                    seen.add(milestone)
                    self._print(f"  Time remaining: {self._format_ms(remaining)}")
                    break
            return

        if kind == "TICK":
            player = fields.get("player", "?")
            try:
                score = (int(fields.get("passed", 0)), int(fields.get("total", 0)),
                         int(fields.get("subs", 0)))
            except (TypeError, ValueError):
                self._print("  ! UDP TICK ignored: invalid score")
                return
            self._update_score(match, player, score)
            return

        if kind == "BOARD":
            for entry in str(fields.get("e", "")).split(","):
                player, separator, value = entry.partition(":")
                if not separator:
                    continue
                pieces = value.split(":")
                if len(pieces) != 2:
                    continue
                try:
                    self._update_score(match, player, (int(pieces[0]), 0, int(pieces[1])))
                except ValueError:
                    continue

    def _update_score(self, match: str, player: str, score: tuple[int, int, int]) -> None:
        scores = self._scores.setdefault(match, {})
        previous = scores.get(player)
        if previous == score:
            return
        # BOARD has no test-total field. Preserve it from the richer TICK snapshot.
        if score[1] == 0 and previous is not None:
            score = (score[0], previous[1], score[2])
        if previous == score:
            return
        scores[player] = score
        total = f"/{score[1]}" if score[1] else ""
        self._print(f"  Score update: {player} {score[0]}{total}, "
                    f"{score[2]} submission{'s' if score[2] != 1 else ''}")

    def show_problem(self, response: Message) -> None:
        try:
            problem = json.loads(response.text())
        except ValueError:
            return
        if not isinstance(problem, dict):
            return
        contract = problem.get("contract") or {}
        self.banner(
            "PROBLEM",
            f"{problem.get('title', '?')} [{problem.get('id', '?')}]",
            f"entry      : {problem.get('signature', problem.get('entry', '?'))}",
            f"contract   : time {contract.get('required_time', '?')}, "
            f"space {contract.get('required_space', '?')}",
            f"remaining  : {self._format_ms(response.headers.get('Time-Remaining-Ms'))}",
        )
        for paragraph in str(problem.get("statement", "")).strip().splitlines():
            self._print(f"    {paragraph}")
        for sample in problem.get("samples") or []:
            self._print(f"    in  {sample.get('in')!r}")
            self._print(f"    out {sample.get('out')!r}")
        self._print("\n  Next: submit <file.py>\n")


class ClientError(Exception):
    """The connection is gone, or the server said something the client cannot use."""


class UdpFeed:
    """Display-only UDP receiver: attach, drop stale/lost values, render the newest state."""

    def __init__(self, host: str, port: int, token: str, log: WireLog,
                 loss_probability: float = 0.0,
                 on_update: Optional[Callable[[str, dict], None]] = None,
                 on_warning: Optional[Callable[[str], None]] = None):
        self.host = host
        self.port = port
        self.token = token
        self.log = log
        self.loss_probability = loss_probability
        self.on_update = on_update
        self.on_warning = on_warning
        self.latest = LatestWins()
        self.closed = False
        self.sock: Optional[socket.socket] = None
        self.thread: Optional[threading.Thread] = None
        self._warning_count = 0
        self._warning_last = 0.0

    def start(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("0.0.0.0", 0))
        # A connected UDP socket accepts datagrams only from this arena endpoint on
        # supported platforms. The feed remains display-only and unauthenticated.
        sock.connect((self.host, self.port))
        sock.settimeout(0.5)
        self.sock = sock
        fields = {"session": self.token}
        data = encode_datagram("ATTACH", fields)
        sock.send(data)
        self.log.udp_sent("ATTACH", fields, peer=f"{self.host}:{self.port}")
        self.thread = threading.Thread(target=self._receive_loop, name="udp-feed", daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.closed = True
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
        if self.thread is not None and self.thread is not threading.current_thread():
            self.thread.join(timeout=1.0)

    def _receive_loop(self) -> None:
        assert self.sock is not None
        while not self.closed:
            try:
                data = self.sock.recv(2048)
            except socket.timeout:
                continue
            except OSError:
                return
            peer = f"{self.host}:{self.port}"
            try:
                if not data.startswith((PROTOCOL_VERSION + " ").encode("ascii")):
                    raise ProtocolError(f"UDP feed requires {PROTOCOL_VERSION}")
                kind, fields = decode_datagram(data)
            except (ProtocolError, FrameTooLarge) as exc:
                reason = f"malformed datagram from {peer}: {exc}"
                self.log.udp_dropped(reason)
                self._warn(reason)
                continue

            if kind not in {"TICK", "CLOCK", "BOARD"}:
                reason = f"unexpected {kind} from {peer}"
                self.log.udp_dropped(reason)
                self._warn(reason)
                continue

            if self.loss_probability and random.random() < self.loss_probability:
                self.log.udp_dropped(
                    f"simulated loss kind={kind} match={fields.get('match', '?')} "
                    f"seq={fields.get('seq', '?')}"
                )
                continue

            match_id = fields.get("match")
            try:
                seq = int(fields["seq"])
            except (KeyError, TypeError, ValueError):
                reason = f"{kind} from {peer}: missing numeric seq"
                self.log.udp_dropped(reason)
                self._warn(reason)
                continue
            if not match_id:
                reason = f"{kind} from {peer}: missing match"
                self.log.udp_dropped(reason)
                self._warn(reason)
                continue
            if not self.latest.accept(match_id, seq):
                highest = self.latest.highest(match_id)
                reason = f"stale or excessive seq={seq}, highest={highest}, dropped"
                self.log.udp_dropped(reason)
                self._warn(reason)
                continue
            self.log.udp_received(kind, fields, peer=peer)
            if self.on_update is not None:
                self.on_update(kind, fields)

    def _warn(self, reason: str) -> None:
        if self.on_warning is None:
            return
        now = time.monotonic()
        self._warning_count += 1
        # One immediate warning, then at most one summary every five seconds. A bad UDP
        # sender must not recreate the exact terminal flood compact mode prevents.
        if self._warning_count == 1 or now - self._warning_last >= 5.0:
            extra = self._warning_count - 1
            suffix = f" ({extra} similar packets suppressed)" if extra else ""
            self.on_warning(reason + suffix)
            self._warning_last = now
            self._warning_count = 0


class _Pending:
    """One in-flight request, waiting for the response that carries its ``Seq``.

    An ``Event`` rather than a ``Queue`` because there is exactly one response and exactly
    one waiter: the sending thread parks on ``wait()``, the reader thread fills ``response``
    and sets the flag. ``failure`` covers the case where the connection dies first, so a
    waiter is woken with a reason instead of timing out for no visible cause.
    """

    __slots__ = ("done", "response", "failure")

    def __init__(self):
        self.done = threading.Event()
        self.response: Optional[Message] = None
        self.failure: Optional[str] = None


class CdapClient:
    """One player's connection to the arena: send requests, receive events."""

    def __init__(self, host: str, port: int, log: WireLog, *,
                 bad_version: bool = False, tamper: bool = False,
                 lang: str = "python", auto_submit: Optional[str] = None,
                 view: Optional[PlayerView] = None):
        self.host = host
        self.port = port
        self.log = log
        self.bad_version = bad_version
        self.tamper = tamper
        self.lang = lang
        #: File to submit when MATCH_START arrives, or None. Consumed once per match.
        self.auto_submit = auto_submit
        self.view = view or PlayerView()

        self.conn: Optional[Connection] = None
        self.closed = False

        # Seq is per-connection and starts at 1. Monotonic, never reused: a reused number
        # would let a late response be delivered to the wrong caller, which is the exact
        # failure the Seq mechanism exists to prevent.
        self._seq = 0
        self._seq_lock = threading.Lock()
        self._pending: Dict[int, _Pending] = {}
        self._pending_lock = threading.Lock()

        # Work an event asked for, run by the agent thread. See the module docstring for
        # why the reader thread must not run it itself.
        self._actions: "queue.Queue[Optional[Callable[[], None]]]" = queue.Queue()

        # Session facts learned from responses, kept so commands can be short: `submit
        # file.py` needs no match id because the client already knows it.
        self.user: Optional[str] = None
        self.token: Optional[str] = None
        self.session_id: Optional[str] = None
        self.match_id: Optional[str] = None
        self.room_code: Optional[str] = None
        self.last_submission: Optional[str] = None

        self.in_match = False
        self.queued = False
        self.verdicts: List[dict] = []
        self._threads: List[threading.Thread] = []

    # -- connection --------------------------------------------------------

    def connect(self) -> None:
        """Open the TCP connection and start the reader and agent threads."""
        sock = socket.create_connection((self.host, self.port), timeout=10.0)
        # Blocking reads from here on. The connect timeout guards against an arena that is
        # not listening; once connected, the client waits as long as the player wants it to
        # — an idle connection is normal, and the *server* is the side that enforces a
        # timeout on one (408 REQUEST_TIMEOUT).
        sock.settimeout(None)
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass

        self.conn = Connection(sock, log=self.log)
        self.log.note(f"connected to {self.conn.peer} — speaking {PROTOCOL_VERSION}")

        reader = threading.Thread(target=self._reader_loop, name="reader", daemon=True)
        agent = threading.Thread(target=self._agent_loop, name="agent", daemon=True)
        reader.start()
        agent.start()
        self._threads = [reader, agent]

    def close(self) -> None:
        """Close the socket and wake anything still waiting."""
        self.closed = True
        self.view.close()
        self._actions.put(None)
        self._fail_pending("the connection was closed")
        if self.conn is not None:
            self.conn.close()
        # Daemon threads are still used so a genuinely wedged platform cannot hold the
        # process forever, but under normal shutdown both finish before stdout disappears.
        # Without this join, a reader printing its final line during interpreter teardown
        # can trigger CPython's ``_enter_buffered_busy`` fatal error.
        current = threading.current_thread()
        for thread in self._threads:
            if thread is not current and thread.is_alive():
                thread.join(timeout=1.0)

    # -- sending -----------------------------------------------------------

    def _next_seq(self) -> int:
        with self._seq_lock:
            self._seq += 1
            return self._seq

    def request(self, method: str, headers: Optional[dict] = None,
                body: bytes = b"", payload: Optional[dict] = None,
                timeout: float = RESPONSE_TIMEOUT_S) -> Message:
        """Send a request and block until its response arrives.

        The registration happens *before* the send, and the order is not incidental: a
        server that answers in under a millisecond can have its response read by the reader
        thread before this thread returns from ``sendall``. Registering afterwards would
        lose that race sometimes — which is the worst kind of bug to have in a demo, because
        it works on every attempt except the one being filmed.
        """
        if self.conn is None or self.closed:
            raise ClientError("not connected")

        if payload is not None:
            body = json.dumps(payload).encode("utf-8")

        seq = self._next_seq()
        message = Message.request(method, headers=headers, body=body, seq=seq)
        if body:
            message.attach_body_hash()
            if self.tamper:
                self._corrupt_body(message)
        if self.bad_version:
            message.version = WRONG_VERSION

        pending = _Pending()
        with self._pending_lock:
            self._pending[seq] = pending

        try:
            self.conn.send(message)
        except OSError as exc:
            with self._pending_lock:
                self._pending.pop(seq, None)
            raise ClientError(f"send failed: {exc}") from exc

        if not pending.done.wait(timeout):
            with self._pending_lock:
                self._pending.pop(seq, None)
            raise ClientError(f"no response to {method} (Seq={seq}) within {timeout:.0f}s")
        if pending.failure:
            raise ClientError(pending.failure)
        assert pending.response is not None
        return pending.response

    def _corrupt_body(self, message: Message) -> None:
        """Change the body *after* hashing it, so ``Body-SHA256`` no longer matches.

        This is what ``--tamper`` does, and it models the real threat honestly: the frame
        stays perfectly well-formed. ``Content-Length`` is recomputed on encode, so the
        framing is correct and TCP's checksum is satisfied — only the application-layer
        hash disagrees. A transport cannot catch this; that is the whole argument for
        checking integrity at the application layer, and why the arena answers
        ``422 BODY_HASH_MISMATCH`` rather than trusting the socket.
        """
        message.body = message.body + b"\n# tampered in flight\n"
        self.log.note("--tamper: body modified after hashing — expect "
                      f"{format_status(Status.BODY_HASH_MISMATCH)}")

    # -- receiving ---------------------------------------------------------

    def _reader_loop(self) -> None:
        """The only thread that reads. Routes every frame by whether it carries a ``Seq``."""
        try:
            while not self.closed:
                assert self.conn is not None
                message = self.conn.recv()
                if message is None:
                    self._fail_pending("the arena closed the connection")
                    self.log.note("the arena closed the connection")
                    return
                self._route(message)
        except (ProtocolError, FrameTooLarge) as exc:
            self._fail_pending(f"unreadable frame from the arena: {exc}")
            self.log.note(f"unreadable frame from the arena: {exc}")
        except OSError as exc:
            self._fail_pending(f"connection lost: {exc}")
        finally:
            self.closed = True
            self._actions.put(None)

    def _route(self, message: Message) -> None:
        """Deliver one frame to whoever it belongs to.

        Three cases, in the order the protocol makes them decidable:

        1. an **event** — no ``Seq``, so nobody is waiting for it;
        2. a **response with a matching Seq** — hand it to the waiter;
        3. a **response with no matching Seq** — which is not a bug and not ignorable.

        The third case is worth its lines. It happens legitimately: a frame refused before
        the server could read a ``Seq`` (an oversized body, a broken frame, an idle timeout)
        is answered without one, because there is no request to echo. It also happens when a
        caller has already given up and timed out. Either way the response is real
        information and the client prints it rather than dropping it silently.
        """
        if message.kind is Kind.EVENT:
            self._handle_event(message)
            return

        if message.kind is Kind.REQUEST:
            # Servers do not send requests in CDAP/1.0. Noted rather than acted on.
            self.log.note(f"ignoring a {message.method} request from the arena — "
                          "in CDAP/1.0 only clients send requests")
            return

        seq = message.seq
        if seq is not None:
            with self._pending_lock:
                pending = self._pending.pop(seq, None)
            if pending is not None:
                pending.response = message
                pending.done.set()
                return

        self.log.note(
            f"unsolicited {describe_status(message.status, message.phrase)} "
            f"(Seq={seq if seq is not None else 'none'}) — "
            f"{message.headers.get('Detail', 'no detail given')}"
        )

    def _fail_pending(self, reason: str) -> None:
        """Wake every blocked caller with a reason, instead of leaving them to time out."""
        with self._pending_lock:
            waiting, self._pending = self._pending, {}
        for pending in waiting.values():
            pending.failure = reason
            pending.done.set()

    # -- events ------------------------------------------------------------

    def _handle_event(self, message: Message) -> None:
        """React to a server push. Runs on the reader thread, so it never blocks.

        Everything here is bookkeeping and printing. Anything that needs a *request* is
        handed to the agent thread — see the module docstring for the deadlock that rule
        avoids.
        """
        name = message.event
        headers = message.headers

        if name == "MATCH_FOUND":
            self.match_id = headers.get("Match")
            self.room_code = None
            self.queued = False
            self.view.match_found(headers)
        elif name == "MATCH_START":
            self.match_id = headers.get("Match") or self.match_id
            self.in_match = True
            self.view.match_start(headers)
            self._on_match_start(headers)
        elif name == "MATCH_END":
            ended_match = headers.get("Match") or self.match_id
            self.in_match = False
            self.match_id = None
            self.queued = False
            if ended_match:
                self.view.clear_match(ended_match)
        elif name == "VERDICT":
            self._on_verdict(message)
        elif name == "ROOM_UPDATE":
            self.room_code = headers.get("Room") or self.room_code

        if name in TERMINAL_EVENTS:
            self._on_terminal(name, headers)

    def _on_match_start(self, headers) -> None:
        """Fetch the newly revealed problem, then optionally submit a scripted file."""
        path = self.auto_submit
        self.auto_submit = None         # once per match, not once per event
        match_id = headers.get("Match") or self.match_id
        self._actions.put(lambda: self._fetch_problem_then_submit(match_id, path))

    def _fetch_problem_then_submit(self, match_id: Optional[str], path: Optional[str]) -> None:
        if not match_id or not self.in_match or self.match_id != match_id:
            self.log.note("match ended before automatic problem fetch; action skipped")
            return
        problem = self.request("GET_PROBLEM")
        if is_success(problem.status):
            self.view.show_problem(problem)
        else:
            self.log.note("automatic GET_PROBLEM failed: "
                          f"{describe_status(problem.status, problem.phrase)} — "
                          f"{problem.headers.get('Detail', 'no detail')}")
            return
        if path is not None and self.in_match and self.match_id == match_id:
            self.log.note(f"auto-submitting {path} for "
                          f"{problem.headers.get('Problem', '?')}")
            response = self.submit_file(path)
            if not is_success(response.status):
                self.log.note("automatic SUBMIT failed: "
                              f"{describe_status(response.status, response.phrase)} — "
                              f"{response.headers.get('Detail', 'no detail')}")

    def _on_verdict(self, message: Message) -> None:
        """Render a verdict. The one place the 6xx namespace is displayed to a human.

        The ``Verdict`` header already carries the code and phrase, so this adds what a
        header cannot: the measured complexity next to the required one, and the profiler's
        own explanation of why they disagree. ``606`` is the verdict that most needs it —
        "every test passed and you were still rejected" is baffling without the numbers.
        """
        try:
            payload = json.loads(message.text()) if message.body else {}
        except ValueError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        self.verdicts.append(payload)

        code = payload.get("verdict")
        # The Verdict header already carries "606 TIME_COMPLEXITY_VIOLATION". Prefer it, and
        # fall back to the body's own code and phrase, so a verdict is never shown as a bare
        # number even if the header is missing.
        line = message.headers.get("Verdict") or (
            describe_status(code, payload.get("phrase")) if code else "unknown verdict")
        lines = [
            f"  ===== VERDICT  {line} =====",
            f"  submission : {message.headers.get('Submission', '?')}",
            f"  tests      : {payload.get('tests_passed', '?')}",
        ]
        if payload.get("detail"):
            lines.append(f"  detail     : {payload['detail']}")

        # Measured versus required, side by side. This is the line that makes a 606 land:
        # "every test passed and you were still rejected" only makes sense once the player
        # can see O(n^2) sitting next to the O(n) they agreed to.
        if payload.get("inferred_time") is not None:
            lines.append(f"  time       : measured {payload['inferred_time']} "
                         f"vs required {payload.get('required_time', '?')}"
                         f"   confidence={payload.get('confidence', '?')}")
            lines.append(f"  fit        : margin={payload.get('margin', '?')} "
                         f"rel_rmse={payload.get('rel_rmse', '?')} "
                         f"log-log slope={payload.get('loglog_slope', '?')}")
            if payload.get("fit_reason"):
                lines.append(f"  reason     : {payload['fit_reason']}")
        if payload.get("inferred_space") is not None:
            lines.append(f"  space      : measured {payload['inferred_space']} "
                         f"vs required {payload.get('required_space', '?')}"
                         f"   confidence={payload.get('space_confidence', '?')}"
                         f"   peak_aux={payload.get('peak_aux_kb', '?')} KB")
        # Method B is shown only when it *disagrees* with Method A. Agreement is the boring
        # case and would just be noise; disagreement is the report's most interesting
        # result (opcode counting cannot see work done inside C builtins), so it earns a
        # line, labelled as informational — Method A remains the one that decides.
        if payload.get("methods_disagree"):
            lines.append(f"  method B   : {payload.get('method_b_inferred')} "
                         f"(opcode counting, {payload.get('method_b_mechanism', '?')}) "
                         f"— disagrees with Method A; Method A decides")
        # The backend is printed for every verdict on purpose: a run that fell back from
        # docker to subprocess had weaker isolation, and the report's conclusions depend on
        # that being visible rather than assumed (design invariant 6).
        lines.append(f"  backend    : {payload.get('backend', '?')}"
                     f"   worker={payload.get('worker', '-')}"
                     f"   judge_wall={payload.get('judge_wall_ms', '?')} ms")
        for failure in (payload.get("failures") or [])[:3]:
            lines.append(f"  failed     : {failure}")
        self.view.verdict(lines)

    def _on_terminal(self, name: str, headers) -> None:
        self.view.terminal(name, headers)

    def mark_queued(self) -> None:
        """Keep both scripted and interactive queue flows on the same state path."""
        self.queued = True

    def _agent_loop(self) -> None:
        """Run the follow-up requests events asked for, one at a time."""
        while True:
            action = self._actions.get()
            if action is None:
                return
            try:
                action()
            except ClientError as exc:
                self.log.note(f"follow-up failed: {exc}")
            except Exception as exc:                      # noqa: BLE001
                self.log.note(f"follow-up raised: {exc!r}")

    # -- the flows a player actually uses ----------------------------------

    def handshake(self, user: str, password: str) -> None:
        """HELLO, then REGISTER, then LOGIN — the three every session begins with.

        ``409 USER_EXISTS`` is expected rather than exceptional: the demo runs the same
        client repeatedly with the same name, so the second run finds the account already
        there. That is a successful outcome of ``REGISTER`` from the player's point of view,
        and treating it as an error would make the ordinary path look broken.
        """
        hello = self.request("HELLO")
        if not is_success(hello.status):
            raise ClientError(f"HELLO refused: {self._why(hello)}")
        self.session_id = hello.headers.get("Session")
        self._print_hello(hello)

        registered = self.request("REGISTER", payload={"user": user, "pass": password})
        if registered.status == int(Status.CONFLICT):
            self.log.note(f"{user} already has an account — logging in instead")
        elif not is_success(registered.status):
            raise ClientError(f"REGISTER refused: {self._why(registered)}")

        login = self.request("LOGIN", payload={"user": user, "pass": password})
        if not is_success(login.status):
            raise ClientError(f"LOGIN refused: {self._why(login)}")
        self.user = login.headers.get("User") or user
        self.token = login.headers.get("Token")
        self.session_id = login.headers.get("Session") or self.session_id
        self.log.note(f"logged in as {self.user} (session {self.session_id})")

    def _print_hello(self, hello: Message) -> None:
        """Show what the arena said it can do. The handshake's whole purpose."""
        try:
            info = json.loads(hello.text()) if hello.body else {}
        except ValueError:
            return
        if not isinstance(info, dict):
            return
        judge = info.get("judge") or {}
        print(f"  arena      : {info.get('server', '?')} speaking {info.get('protocol', '?')}")
        print(f"  problems   : {', '.join(info.get('problems') or [])}")
        print(f"  match      : {info.get('match_seconds', '?')}s, "
              f"{info.get('min_players', '?')} player(s) per match")
        print(f"  judge      : backend={judge.get('backend', '?')} "
              f"healthy={judge.get('healthy', '?')} "
              f"opcodes={judge.get('opcode_counter', '?')}")

    def submit_file(self, path: str) -> Message:
        """Read a source file and SUBMIT it."""
        with open(path, "r", encoding="utf-8") as handle:
            source = handle.read()
        response = self.request(
            "SUBMIT",
            headers={"Lang": self.lang, "Match": self.match_id or "-"},
            body=source.encode("utf-8"),
        )
        if response.status == int(Status.ACCEPTED):
            self.last_submission = response.headers.get("Submission")
            self.log.note(f"submitted {path} as {self.last_submission} — "
                          f"the verdict will arrive as a VERDICT event")
        return response

    def _auto_submit(self, path: str) -> None:
        """The ``--submit`` flow: fetch the problem, then send the file.

        GET_PROBLEM first even though the file is already written. It is what a real client
        would do, it proves the request works, and it puts the contract the submission is
        about to be judged against into the log immediately above the submission itself.
        """
        self._fetch_problem_then_submit(self.match_id, path)

    @staticmethod
    def _why(response: Message) -> str:
        """``"<code> <PHRASE>: <detail>"`` — how a failed response is reported to a human."""
        detail = response.headers.get("Detail", "")
        line = describe_status(response.status, response.phrase)
        return f"{line}: {detail}" if detail else line


# --------------------------------------------------------------------------
# The interactive command loop
# --------------------------------------------------------------------------

HELP_TEXT = """
commands
  queue                  join matchmaking            → 202 QUEUED
  dequeue                leave matchmaking           → 200 / 409 NOT_QUEUED
  room [problem-id]      open a private room         → 201 CREATED
  join <CODE>            join a room by code         → 200 / 404 / 409 ROOM_FULL
  ready                  ready up in a room          → 200
  leave                  leave a room                → 204
  problem                fetch the statement         → 200 / 403 NOT_IN_MATCH
  submit <file.py>       submit a solution           → 202 ACCEPTED
  status [submission]    re-read a verdict           → 200 / 202 / 404
  forfeit                give up the match           → 200
  panic                  ask the server to fail      → 500 (needs --allow-panic)
  raw <METHOD> [json]    send any method by hand
  whoami                 what this client knows
  help                   this list
  quit                   LOGOUT and disconnect       → 204
"""


class CommandLoop:
    """Reads what the player types and turns it into requests.

    Runs on the main thread. Every command is a one-liner that sends a request and prints
    the outcome, because the wire log has already printed the frames themselves — this
    layer only adds what a human needs on top of them.
    """

    def __init__(self, client: CdapClient, log: WireLog):
        self.client = client
        self.log = log

    def run(self) -> None:
        print("\n  Next: type queue to find a match. Type help for all commands.\n")
        while not self.client.closed:
            try:
                line = input(f"{self.client.user or 'cdap'}[{self._state_label()}]> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                self._quit()
                return
            if not line:
                continue
            if not self._execute(line):
                return

    def _execute(self, line: str) -> bool:
        """Run one command. Returns False when the loop should end."""
        parts = line.split()
        command, args = parts[0].lower(), parts[1:]
        try:
            if command in ("quit", "exit"):
                self._quit()
                return False
            handler = getattr(self, f"_cmd_{command}", None)
            if handler is None:
                print(f"unknown command {command!r} — type 'help'")
                return True
            handler(args)
        except ClientError as exc:
            print(f"  ! {exc}")
            return not self.client.closed
        except FileNotFoundError as exc:
            print(f"  ! no such file: {exc.filename}")
        except ValueError as exc:
            print(f"  ! {exc}")
        return True

    # -- commands ----------------------------------------------------------

    def _cmd_help(self, args: List[str]) -> None:
        print(HELP_TEXT)

    def _cmd_queue(self, args: List[str]) -> None:
        response = self.client.request("QUEUE")
        if is_success(response.status):
            self.client.mark_queued()
        self._show(response)

    def _cmd_dequeue(self, args: List[str]) -> None:
        response = self.client.request("DEQUEUE")
        if is_success(response.status):
            self.client.queued = False
        self._show(response)

    def _cmd_room(self, args: List[str]) -> None:
        payload = {"problem": args[0]} if args else {}
        response = self.client.request("CREATE_ROOM", payload=payload)
        if is_success(response.status):
            self.client.room_code = response.headers.get("Room")
            print(f"  room code: {self.client.room_code} — "
                  f"tell your opponent to 'join {self.client.room_code}'")
        self._show(response)

    def _cmd_join(self, args: List[str]) -> None:
        if not args:
            raise ValueError("usage: join <CODE>")
        response = self.client.request("JOIN_ROOM", payload={"room": args[0]})
        if is_success(response.status):
            self.client.room_code = response.headers.get("Room")
        self._show(response)

    def _cmd_ready(self, args: List[str]) -> None:
        self._show(self.client.request("READY"))

    def _cmd_leave(self, args: List[str]) -> None:
        response = self.client.request("LEAVE")
        if is_success(response.status):
            self.client.room_code = None
        self._show(response)

    def _cmd_problem(self, args: List[str]) -> None:
        response = self.client.request("GET_PROBLEM")
        if is_success(response.status):
            self.client.view.show_problem(response)
        self._show(response)

    def _cmd_submit(self, args: List[str]) -> None:
        if not args:
            raise ValueError("usage: submit <file.py>")
        self._show(self.client.submit_file(args[0]))

    def _cmd_status(self, args: List[str]) -> None:
        submission = args[0] if args else self.client.last_submission
        if not submission:
            raise ValueError("nothing submitted yet — usage: status <submission-id>")
        self._show(self.client.request("GET_SUBMISSION",
                                       payload={"submission": submission}))

    def _cmd_forfeit(self, args: List[str]) -> None:
        self._show(self.client.request("FORFEIT"))

    def _cmd_panic(self, args: List[str]) -> None:
        """Ask the server to fail on purpose, to show 500 INTERNAL_ERROR is reachable."""
        self._show(self.client.request("DEBUG_PANIC"))

    def _cmd_raw(self, args: List[str]) -> None:
        """Send any method with any JSON body — the escape hatch for the demo.

        This is how a status code with no command of its own gets reached on camera:
        ``raw NOPE`` produces ``405 METHOD_NOT_ALLOWED``, and ``raw LOGIN {}`` produces the
        ``400`` for a body missing its required fields. Being able to send a deliberately
        wrong request is what makes the error half of the protocol demonstrable at all.
        """
        if not args:
            raise ValueError("usage: raw <METHOD> [json-body]")
        method = args[0].upper()
        body = " ".join(args[1:]).strip()
        if body:
            json.loads(body)                # fail here, not on the wire, if it is not JSON
            self._show(self.client.request(method, body=body.encode("utf-8")))
        else:
            self._show(self.client.request(method))

    def _cmd_whoami(self, args: List[str]) -> None:
        client = self.client
        print(f"  user={client.user} session={client.session_id} "
              f"match={client.match_id or '-'} room={client.room_code or '-'}")
        print(f"  in_match={client.in_match} last_submission="
              f"{client.last_submission or '-'} verdicts={len(client.verdicts)}")
        print(f"  token={client.token or '-'}  (Phase 7: this is what UDP_ATTACH will use)")

    # -- output ------------------------------------------------------------

    def _state_label(self) -> str:
        if self.client.in_match:
            return f"match {self.client.match_id or '?'}"
        if self.client.match_id:
            return "starting"
        if self.client.queued:
            return "queued"
        if self.client.room_code:
            return f"room {self.client.room_code}"
        return "idle"

    def _show(self, response: Message) -> None:
        """Print the code, the phrase, and the detail. Never a bare number."""
        line = describe_status(response.status, response.phrase)
        detail = response.headers.get("Detail", "")
        marker = "ok " if is_success(response.status) else "!  "
        print(f"  {marker} {line}" + (f" — {detail}" if detail else ""))

    def _print_problem(self, response: Message) -> None:
        try:
            problem = json.loads(response.text())
        except ValueError:
            return
        if not isinstance(problem, dict):
            return
        contract = problem.get("contract") or {}
        print()
        print(f"  {problem.get('title', '?')}  [{problem.get('id', '?')}]")
        print(f"  entry      : {problem.get('signature', problem.get('entry', '?'))}")
        print(f"  contract   : time {contract.get('required_time', '?')}, "
              f"space {contract.get('required_space', '?')}  "
              f"(limits: {contract.get('time_limit_ms', '?')} ms, "
              f"{contract.get('mem_limit_kb', '?')} KB)")
        print(f"  remaining  : {response.headers.get('Time-Remaining-Ms', '?')} ms")
        print()
        for paragraph in str(problem.get("statement", "")).strip().splitlines():
            print(f"    {paragraph}")
        print()
        for sample in problem.get("samples") or []:
            print(f"    in  {sample.get('in')!r}")
            print(f"    out {sample.get('out')!r}")
        print()

    def _quit(self) -> None:
        try:
            self.client.request("LOGOUT", timeout=5.0)
        except ClientError as exc:
            self.log.note(f"LOGOUT did not complete: {exc}")
        self.client.close()


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m cdap.client",
        description="CDAP player client — connect to an arena, duel, submit solutions.",
        epilog="The demo flags below each exist to make one status code happen on camera.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="arena host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=5050, help="arena TCP port (default: 5050)")
    parser.add_argument("--udp-port", type=int, default=5051,
                        help="arena UDP live-feed port (default: 5051)")
    parser.add_argument("--user", default="alice", help="username (default: alice)")
    parser.add_argument("--pass", dest="password", default="secret",
                        help="password (default: secret — this is a coursework arena)")

    parser.add_argument("--queue", action="store_true",
                        help="join matchmaking immediately after logging in")
    parser.add_argument("--submit", metavar="FILE", default=None,
                        help="submit FILE automatically when the match starts")
    parser.add_argument("--once", action="store_true",
                        help="exit after the first verdict instead of staying interactive; "
                             "for scripted demos and the experiments")

    demo = parser.add_argument_group("demo flags")
    demo.add_argument("--bad-version", action="store_true",
                      help=f"send {WRONG_VERSION} instead of {PROTOCOL_VERSION} "
                           f"→ 426 VERSION_UNSUPPORTED")
    demo.add_argument("--tamper", action="store_true",
                      help="modify each body after hashing it → 422 BODY_HASH_MISMATCH")
    demo.add_argument("--lang", default="python",
                      help="the Lang header to send; --lang rust → 415 "
                           "UNSUPPORTED_LANGUAGE")
    demo.add_argument("--feed-only", action="store_true",
                      help="keep an authenticated pane open for UDP progress datagrams")
    demo.add_argument("--no-udp", action="store_true",
                      help="do not attach to the UDP feed — proves the match completes "
                           "over TCP alone (design invariant 1)")
    demo.add_argument("--udp-loss", type=float, default=0.0, metavar="P",
                      help="drop this fraction of received datagrams, to show the feed "
                           "converging under loss")

    parser.add_argument("--wire", action="store_true",
                        help="show every TCP frame and UDP datagram for protocol demos "
                             "and debugging (normal play is compact)")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="add full headers and body previews to the wire log; this also "
                             "enables the full frame transcript")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    # Before parse_args, not after: ``--help`` prints and exits *inside* parse_args, so a
    # reconfigure that came later would never run for the one output most likely to be read
    # first, and the help text's em dashes would come out as mojibake on a cp1252 console.
    capabilities.enable_utf8_output()

    args = build_parser().parse_args(argv)
    wire_mode = args.wire or args.verbose or args.feed_only
    log = WireLog(stream=sys.stdout, verbose=args.verbose, prefix="", wire=wire_mode)

    if not 0.0 <= args.udp_loss <= 1.0:
        log.note("--udp-loss must be between 0.0 and 1.0")
        return 2
    if args.feed_only and args.no_udp:
        log.note("--feed-only and --no-udp contradict each other")
        return 2
    if args.udp_loss:
        log.note(f"--udp-loss {args.udp_loss}: received datagrams will be randomly dropped")
    if args.no_udp:
        log.note("--no-udp: TCP only. Nothing in a match depends on the feed "
                 "(design invariant 1), so this changes the display and nothing else.")

    view = PlayerView(compact=not wire_mode)
    client = CdapClient(args.host, args.port, log,
                        bad_version=args.bad_version,
                        tamper=args.tamper,
                        lang=args.lang,
                        auto_submit=args.submit,
                        view=view)
    feed: Optional[UdpFeed] = None

    try:
        client.connect()
    except OSError as exc:
        log.note(f"could not connect to {args.host}:{args.port} — {exc}")
        return 1

    try:
        client.handshake(args.user, args.password)
    except ClientError as exc:
        # For --bad-version and --tamper a refused handshake is the *point*: the arena
        # answered with the right code and hung up. Reporting that as a failure would be
        # backwards, so those two flags exit 0 and say what happened. Any other cause is a
        # real failure and still exits 1.
        log.note(f"handshake stopped: {exc}")
        client.close()
        if args.bad_version or args.tamper:
            log.note("that refusal is what the flag exists to produce — demo succeeded")
            return 0
        return 1

    if not args.no_udp:
        if not client.token:
            log.note("LOGIN returned no feed token; continuing over TCP only")
        else:
            try:
                feed = UdpFeed(args.host, args.udp_port, client.token, log,
                               loss_probability=args.udp_loss,
                               on_update=None if wire_mode else view.udp_update,
                               on_warning=None if wire_mode else view.warning)
                feed.start()
                log.note(f"UDP feed attached on port {args.udp_port}; it carries display "
                         "data only")
            except OSError as exc:
                log.note(f"UDP feed unavailable ({exc}); continuing over TCP only")
                feed = None

    try:
        if args.feed_only:
            log.note("feed-only pane ready; use the same --user in a playing TCP client")
            return _wait_feed_only(client)

        if args.queue:
            response = client.request("QUEUE")
            if is_success(response.status):
                client.mark_queued()
            log.note(f"QUEUE → {describe_status(response.status, response.phrase)}")

        if args.once:
            return _wait_for_verdict(client, log)

        CommandLoop(client, log).run()
    except ClientError as exc:
        log.note(str(exc))
        return 1
    finally:
        if feed is not None:
            feed.close()
        client.close()
    return 0


def _wait_for_verdict(client: CdapClient, log: WireLog, timeout: float = 180.0) -> int:
    """Block until a verdict arrives or the match ends — the ``--once`` path.

    Used by scripted demos and by the experiments, where there is nobody at a keyboard.
    Polling a flag rather than waiting on a condition variable, because the loop also has
    to notice a connection that died, and one sleep that checks both is easier to explain
    than two synchronisation primitives.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if client.verdicts:
            return 0
        if client.closed:
            log.note("the connection closed before a verdict arrived")
            return 1
        time.sleep(0.2)
    log.note(f"no verdict within {timeout:.0f}s")
    return 1


def _wait_feed_only(client: CdapClient) -> int:
    """Keep the authenticated attach token alive while the UDP-only pane is displayed."""
    try:
        while not client.closed:
            time.sleep(0.25)
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
