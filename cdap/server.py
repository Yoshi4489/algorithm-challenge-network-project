"""CDAP arena server — the TCP half of the protocol, and the state machine behind it.

This is the process a player connects to. It owns every piece of shared state in the
arena: accounts, sessions, the matchmaking lobby, private rooms, live matches, and the
queue of submissions waiting to be judged.

Why TCP for this side of the protocol
-------------------------------------
Everything here is *state-changing and must arrive*. A ``SUBMIT`` that vanishes costs a
player their match; a ``VERDICT`` that vanishes leaves them staring at a spinner; a
``LOGIN`` that arrives twice creates a mess. So this side of CDAP wants exactly what TCP
provides and UDP does not: reliable, ordered, at-most-once-per-send delivery with
retransmission handled below the application.

The UDP feed added in Phase 7 is the opposite case — a 5-per-second progress display,
where the newest datagram makes every older one irrelevant. Losing one costs nothing.
That contrast is the point of the dual-transport design, and design invariant 1 keeps it
honest: **if UDP is entirely dead the match still completes correctly over TCP.** Nothing
in this file depends on the feed.

The threading model, which is most of what makes a server a server
-----------------------------------------------------------------
Threads, not ``select``. The arena has at most a handful of players and the code is going
to be explained aloud, so a blocking read per connection is worth far more than an event
loop's efficiency.

Per accepted connection there are **two** threads:

* a **reader** thread (``_ClientHandler.serve``) that blocks in ``recv()``, dispatches the
  request, and writes the response itself;
* a **writer** thread that does nothing but drain that session's event outbox.

The second one is not ceremony. Server-pushed events originate on *other* threads — a
judge thread finishing a submission, the tick thread ending a match on its deadline. If
those threads called ``send()`` directly, one player whose TCP receive window had filled
up (a paused terminal is enough) would block the judge thread mid-``sendall``, and every
other player's verdict would stall behind it. Head-of-line blocking, one connection
poisoning the whole arena. Queuing the event and letting a thread that owns *only* that
socket do the blocking send confines the damage to the session that caused it.

Both threads write to the same socket, which is exactly the situation
``protocol.Connection``'s send lock exists for: a response and an event must not interleave
their bytes.

One more lock, ``Arena._lock``, guards all shared arena state. Deliberately one coarse
lock rather than several fine ones: at this scale it costs nothing measurable, and it
means the invariants can be stated in one sentence — *nobody reads or writes arena state
without holding it* — instead of requiring a lock-ordering argument.

Where the judging happens
-------------------------
Small demos may use ``LocalJudgePool`` threads inside the arena. Separate judge processes
use the four ``WORKER_*`` methods to pull from that exact same ``JobQueue`` over TCP. The
queue is the seam: local and remote consumers can coexist without either knowing about the
other, while ``--judges 0`` makes the isolation boundary visible by requiring remote workers.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import queue
import secrets
import socket
import sys
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from collections import defaultdict, deque
from typing import Callable, Dict, List, Optional, Tuple

from . import capabilities
from .problems import Problem, get_problem, problem_ids
from .protocol import (
    PROTOCOL_VERSION,
    Connection,
    FrameTooLarge,
    Kind,
    Message,
    ProtocolError,
    WireLog,
    decode_datagram,
    encode_datagram,
)
from .status import Status, Verdict, format_status
from .judge.backends import DockerBackend, make_backend
from .judge.profiler import judge_record
from .judge.runner import run_budget_ms

#: Largest source file a player may submit. Well under ``protocol.MAX_BODY_BYTES`` (1 MB)
#: on purpose, and the difference in *handling* is the interesting part:
#:
#: * Over this limit but under the framing limit — the frame was fully read, so we know its
#:   ``Seq`` and the stream is still synchronised. Answer ``413`` and carry on talking.
#: * Over the framing limit — ``read_message`` refuses before allocating, which means the
#:   body was never consumed. The unread bytes are still in the stream with no way to know
#:   where the next frame begins, so the only correct move is ``413`` and close.
#:
#: Same status code, two different recoveries, decided by whether the stream is still
#: trustworthy. That distinction is worth a sentence on video.
MAX_SUBMISSION_BYTES = 256 * 1024

#: The stage vocabulary a ``JUDGE_PROGRESS`` event may name.
#:
#: All five are protocol vocabulary; under the ``subprocess`` backend only ``QUEUED``,
#: ``COMPILING`` and ``DONE`` are ever *emitted*. The reason is honest rather than
#: technical: ``TESTING`` and ``PROFILING`` happen inside the child process, and the parent
#: cannot observe the transition without the child reporting it. Emitting them anyway —
#: on a timer, or after the fact from the finished record — would be inventing progress the
#: server never saw. So the server names only the stages it actually witnessed.
JUDGE_STAGES = ("QUEUED", "COMPILING", "TESTING", "PROFILING", "DONE")

#: How often the tick thread wakes to form matches and expire deadlines. Fast enough that
#: a countdown feels responsive on camera, slow enough to be invisible in a profile.
TICK_INTERVAL_S = 0.25

#: Room codes are drawn from this alphabet — no ``0``/``O`` or ``1``/``I``, because a
#: player reads the code aloud to their opponent during the demo.
ROOM_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

#: Longest username accepted, and the characters allowed in one. Both matter for more than
#: tidiness: a username is echoed into a ``Detail`` header and into UDP datagram fields, so
#: constraining it at the point of entry is the cheapest place to stop header injection.
MAX_USERNAME = 24
MAX_PASSWORD = 128
USERNAME_EXTRA_CHARS = "_-."

#: Bound on a session's pending events. A client that stops reading gets its oldest events
#: dropped rather than growing the server's memory without limit. Nothing is lost
#: permanently: ``GET_SUBMISSION`` re-reads a verdict on demand, which is exactly why that
#: method exists alongside the ``VERDICT`` event.
MAX_OUTBOX_EVENTS = 256

#: What the arena calls itself in the ``Server`` header of a ``HELLO`` reply. Version it
#: separately from the protocol version: an arena can be revised without the wire format
#: changing, and a peer needs to be able to tell those two apart.
SERVER_NAME = "cdap-arena/1.0"

#: Languages the judge can actually run. One, and the tuple is not padding for a future
#: that will not arrive — it is what lets ``SUBMIT`` answer ``415 UNSUPPORTED_LANGUAGE``
#: with the list of what *is* accepted instead of a bare refusal. The client's
#: ``--lang rust`` flag exists to trigger it on camera.
SUPPORTED_LANGUAGES = ("python",)

# Remote-worker liveness. A lease is renewed by every heartbeat and expires after three
# missed intervals, matching the protocol brief. Kept configurable from the server CLI so
# the ejection path can be demonstrated in seconds without changing production defaults.
DEFAULT_WORKER_HEARTBEAT_MS = 5_000
MISSED_HEARTBEATS_BEFORE_EJECTION = 3
MAX_WORKER_POLL_MS = 25_000
DEFAULT_MAX_SESSIONS = 64
DEFAULT_MAX_USERS = 1024
DEFAULT_MAX_SUBMISSIONS = 2048
DEFAULT_MAX_ENDED_MATCHES = 1024
DEFAULT_HISTORY_TTL_S = 3600.0
DEFAULT_MAX_FEED_ENDPOINTS = 2
DEFAULT_MAX_PENDING_JOBS = 128
LOGIN_WINDOW_S = 60.0
REGISTRATION_WINDOW_S = 600.0
MAX_FAILED_LOGINS_PER_IP = 5
MAX_REGISTRATIONS_PER_IP = 10


@dataclass(frozen=True)
class ArenaLimits:
    """Process-local bounds for state retained by this demonstration server."""

    max_users: int = DEFAULT_MAX_USERS
    max_submissions: int = DEFAULT_MAX_SUBMISSIONS
    max_ended_matches: int = DEFAULT_MAX_ENDED_MATCHES
    history_ttl_s: float = DEFAULT_HISTORY_TTL_S
    max_pending_jobs: int = DEFAULT_MAX_PENDING_JOBS


@dataclass(frozen=True)
class PasswordRecord:
    """Salted password verifier; the arena never retains recoverable passwords."""

    salt: bytes
    digest: bytes


# --------------------------------------------------------------------------
# Session state machine
# --------------------------------------------------------------------------

class State(Enum):
    """Where one connection sits in the conversation.

    ::

        INIT --HELLO--> GREETED --LOGIN--> IDLE <-> QUEUED --MATCH_START--> IN_MATCH
                                           ^                                   |
                                           +---------- MATCH_END <-------------+

                                          IDLE <-> IN_ROOM  (private rooms)

    The states are not decoration: every method declares which of them it is legal in, and
    a request arriving in the wrong one is answered ``403`` with a ``Detail`` header naming
    the state it arrived in. That makes the diagram above *testable* from a client.
    """

    INIT = "INIT"
    GREETED = "GREETED"
    IDLE = "IDLE"
    QUEUED = "QUEUED"
    IN_ROOM = "IN_ROOM"
    IN_MATCH = "IN_MATCH"
    CLOSED = "CLOSED"


#: The states a logged-in session can be in. Used by methods that need authentication but
#: do not care where in the lobby the player is.
LOGGED_IN_STATES = (State.IDLE, State.QUEUED, State.IN_ROOM, State.IN_MATCH)


@dataclass
class _MethodSpec:
    """One row of the method table: the handler plus the preconditions to reach it."""

    name: str
    handler: Callable
    requires_auth: bool
    states: Optional[Tuple[State, ...]]     # None means "any state"
    failure_phrase: Optional[str]           # the 403 phrase when the state is wrong


#: Method name -> spec. Filled by the ``@method`` decorator below, so each method's
#: preconditions are written directly above the code that assumes them rather than in a
#: table somewhere else that has to be kept in sync by hand.
METHODS: Dict[str, _MethodSpec] = {}


def method(name: str, *, requires_auth: bool = True,
           states: Optional[Tuple[State, ...]] = None,
           failure_phrase: Optional[str] = None):
    """Register a request handler along with the state it is legal in.

    ``failure_phrase`` is the phrase used when the state check fails. It defaults to
    ``WRONG_STATE``, but ``SUBMIT`` says ``NOT_IN_MATCH`` and ``READY`` says
    ``NOT_IN_ROOM`` — same 403, more useful phrase. That is what the two status namespaces
    document as the job of a phrase: name the specific condition under a general code.
    """

    def register(handler):
        METHODS[name] = _MethodSpec(
            name=name,
            handler=handler,
            requires_auth=requires_auth,
            states=states,
            failure_phrase=failure_phrase or "WRONG_STATE",
        )
        return handler

    return register


class BadRequest(Exception):
    """A well-formed frame whose *contents* cannot be acted on — answered ``400``.

    Distinct from ``ProtocolError``, and the difference decides whether the connection
    survives. A ``ProtocolError`` means the framing is broken and we no longer know where
    the next frame starts, so the connection must close. This means the frame was read
    perfectly and simply said something wrong, so the stream is fine and only the request
    fails.
    """


class SubmissionClosed(Exception):
    """The match changed state while a SUBMIT request was being validated."""


class ArenaCapacityExceeded(Exception):
    """A bounded arena collection cannot accept another record."""


# --------------------------------------------------------------------------
# Sessions
# --------------------------------------------------------------------------

class Session:
    """One connected client: its socket, its state, and its outbound event queue."""

    def __init__(self, session_id: str, conn: Connection, log: WireLog):
        self.id = session_id
        self.conn = conn
        self.log = log

        self.state = State.INIT
        self.user: Optional[str] = None
        self.token: Optional[str] = None
        # A connection becomes either a player session (after LOGIN) or a worker session
        # (after WORKER_REGISTER), never both. Worker identity lives on the connection so a
        # later request cannot impersonate a different registered worker with one header.
        self.worker_id: Optional[str] = None

        self.match_id: Optional[str] = None
        # Kept after the match ends, and that is the whole point of having it. Without a
        # memory of the last match, a SUBMIT arriving one second after the clock ran out
        # would be answered 403 NOT_IN_MATCH — technically true and actively misleading,
        # since the player was in a match and it finished. With it the server can say
        # 410 MATCH_ENDED, which is the difference between "you were never here" and
        # "you are too late".
        self.last_match_id: Optional[str] = None
        self.room_code: Optional[str] = None

        self.submissions: List[str] = []        # ids, oldest first
        self.last_submit_at = 0.0
        self.last_room_create_at = 0.0

        # Events wait here until this session's own writer thread picks them up. See the
        # module docstring for why the sending thread is never the thread that produced
        # the event.
        self.outbox: "queue.Queue[Optional[Message]]" = queue.Queue(maxsize=MAX_OUTBOX_EVENTS)
        self._event_id = 0
        self._lock = threading.Lock()
        self.closed = False

    # -- identity ----------------------------------------------------------

    @property
    def authenticated(self) -> bool:
        return self.user is not None

    @property
    def label(self) -> str:
        """Short name for log lines: the username once known, the session id before that."""
        return self.user or self.worker_id or self.id

    # -- events ------------------------------------------------------------

    def push_event(self, name: str, headers: Optional[dict] = None, body=b"") -> None:
        """Queue a server-pushed event for this session.

        Never blocks and never raises. An event is a courtesy — the client can always ask
        for the same information with a request — so a full outbox drops the oldest event
        and says so in the log rather than stalling the thread that produced it.
        """
        with self._lock:
            if self.closed:
                return
            self._event_id += 1
            event_id = self._event_id
            fields = dict(headers or {})
            fields["Event-Id"] = event_id
            message = Message.make_event(name, headers=fields, body=body)
            try:
                self.outbox.put_nowait(message)
                return
            except queue.Full:
                # This whole drop-and-put sequence is under the same lock as the id
                # allocation.  Otherwise producer B could enqueue Event-Id 2 before
                # producer A enqueued Event-Id 1.
                pass
            try:
                self.outbox.get_nowait()
            except queue.Empty:
                pass
            self.log.note(
                f"outbox full for {self.label}: dropped the oldest event to make room for "
                f"{name} (Event-Id={event_id}); the client can re-read state with "
                f"GET_SUBMISSION"
            )
            try:
                self.outbox.put_nowait(message)
            except queue.Full:
                pass

    def stop_writer(self) -> None:
        """Wake the writer thread so it can exit. ``None`` is the shutdown sentinel."""
        with self._lock:
            self.closed = True
        try:
            self.outbox.put_nowait(None)
        except queue.Full:
            # Full of events nobody will read now anyway. Empty one slot for the sentinel.
            try:
                self.outbox.get_nowait()
                self.outbox.put_nowait(None)
            except (queue.Empty, queue.Full):
                pass


# --------------------------------------------------------------------------
# Matches and submissions
# --------------------------------------------------------------------------

class MatchState(Enum):
    PENDING = "PENDING"     # players notified, clock not yet running
    RUNNING = "RUNNING"
    ENDED = "ENDED"


@dataclass
class Submission:
    """One judged (or pending) attempt."""

    id: str
    session_id: str
    user: str
    match_id: str
    problem_id: str
    lang: str
    source: str
    created_at: float
    stage: str = "QUEUED"
    verdict: Optional[dict] = None          # the profiler's payload, once judged
    finished_at: float = 0.0

    @property
    def done(self) -> bool:
        return self.verdict is not None


@dataclass
class Match:
    """One duel: the same problem, the same clock, two (or one) players."""

    id: str
    problem_id: str
    session_ids: List[str]
    duration_s: float
    created_at: float
    starts_at: float                        # when PENDING becomes RUNNING
    state: MatchState = MatchState.PENDING
    deadline: float = 0.0                   # set when the clock starts
    winner: Optional[str] = None
    end_reason: str = ""
    ended_at: float = 0.0
    submissions: List[str] = field(default_factory=list)
    score: Dict[str, Tuple[int, int]] = field(default_factory=dict)  # session -> (best passed, attempts)

    def remaining_ms(self, now: float) -> int:
        if self.state is not MatchState.RUNNING:
            return int(self.duration_s * 1000)
        return max(0, int((self.deadline - now) * 1000))


# --------------------------------------------------------------------------
# The job queue and the in-process judge pool
# --------------------------------------------------------------------------

@dataclass
class Job:
    """A submission packaged for a judge: everything needed to run it, nothing more."""

    submission_id: str
    problem_id: str
    payload: dict                           # exactly what cdap.judge.runner reads on stdin


@dataclass
class WorkerRecord:
    """One registered remote judge and, while busy, the lease it owns."""

    worker_id: str
    session_id: str
    backend: str
    registered_at: float
    last_seen: float
    active_job: Optional[Job] = None
    lease_deadline: float = 0.0


class JobQueue:
    """Pending submissions, oldest first.

    A thin wrapper over ``queue.Queue`` that also answers "how many are waiting?", because
    ``SUBMIT`` reports a ``Queue-Pos`` header and the player deserves to know they are
    third in line.

    This is the seam both consumers use: ``WORKER_PULL`` long-polls ``get`` from a remote
    worker connection, exactly as ``LocalJudgePool`` does locally. Neither consumer knows
    the other exists.
    """

    def __init__(self, max_pending: int = DEFAULT_MAX_PENDING_JOBS):
        self._queue: "queue.Queue[Optional[Job]]" = queue.Queue()
        self._max_pending = max(1, max_pending)
        self._pending = 0
        self._lock = threading.Lock()

    def reserve(self) -> Optional[int]:
        """Reserve a queue slot before accepting source into retained arena state."""
        with self._lock:
            if self._pending >= self._max_pending:
                return None
            self._pending += 1
            return self._pending

    def put_reserved(self, job: Job) -> None:
        """Publish a previously reserved job after its 202 response has been written."""
        self._queue.put(job)

    def cancel_reservation(self) -> None:
        with self._lock:
            self._pending = max(0, self._pending - 1)

    def put(self, job: Job) -> Optional[int]:
        """Enqueue a job and return its 1-based position in the queue."""
        position = self.reserve()
        if position is None:
            return None
        self.put_reserved(job)
        return position

    def requeue(self, job: Job) -> int:
        """Return a leased job without losing it to concurrent new reservations.

        A pulled job already has retained source and must never be discarded merely
        because a new SUBMIT won the last ordinary admission slot while its worker was
        being ejected.  This restores the slot consumed by ``get`` atomically.
        """
        with self._lock:
            self._pending += 1
            position = self._pending
        self._queue.put(job)
        return position

    def get(self, timeout: Optional[float] = None) -> Optional[Job]:
        """Take the next job, or None on timeout / shutdown."""
        try:
            job = self._queue.get(timeout=timeout)
        except queue.Empty:
            return None
        if job is not None:
            with self._lock:
                self._pending = max(0, self._pending - 1)
        return job

    def depth(self) -> int:
        with self._lock:
            return self._pending

    @property
    def capacity(self) -> int:
        return self._max_pending

    def wake_all(self, count: int) -> None:
        """Push ``count`` shutdown sentinels so every blocked consumer wakes and exits."""
        for _ in range(count):
            self._queue.put(None)


class LocalJudgePool:
    """N threads, each running submissions through a backend in this process.

    Every thread does the same three things: take a job, hand it to a backend, hand the
    resulting measurement record to ``profiler.judge_record`` for a verdict. The backend is
    where the isolation lives (a fresh child process, and in Phase 8 a container); this
    class only decides *how many at once*.

    ``size=0`` is a supported configuration, not a degenerate one: it is how the demo makes
    ``503 JUDGE_UNAVAILABLE`` happen on camera. A server with no judges accepts players,
    runs matches, and refuses submissions — which is precisely the backpressure the design
    claims to have.
    """

    def __init__(self, arena: "Arena", size: int, backend_name: str,
                 guard: bool = True, profile: bool = True):
        self.arena = arena
        self.size = size
        self.backend_name = backend_name
        self.guard = guard
        self.profile = profile
        self._threads: List[threading.Thread] = []
        self._running = False

    @property
    def healthy(self) -> bool:
        """Whether a submission can be judged at all. Drives ``503 JUDGE_UNAVAILABLE``."""
        return self.size > 0 and self._running

    def start(self) -> None:
        self._running = True
        for index in range(self.size):
            thread = threading.Thread(
                target=self._judge_loop,
                name=f"judge-{index + 1}",
                args=(f"local-{index + 1}",),
                daemon=True,
            )
            thread.start()
            self._threads.append(thread)

    def stop(self) -> None:
        self._running = False
        self.arena.jobs.wake_all(len(self._threads))

    def _judge_loop(self, worker_id: str) -> None:
        # Each thread builds its own backend. SubprocessBackend holds no mutable state, so
        # sharing one would work, but per-thread keeps it obviously true that two
        # concurrent judgements cannot interfere.
        backend = make_backend(self.backend_name)
        while self._running:
            job = self.arena.jobs.get(timeout=0.5)
            if job is None:
                continue
            try:
                self._judge_one(backend, worker_id, job)
            except Exception as exc:                      # noqa: BLE001 - see below
                # A judge thread must never die. If it did, the pool would quietly shrink
                # and submissions would hang forever with no verdict and no error — the
                # worst possible failure mode. So every unexpected exception becomes a
                # 612 JUDGE_ERROR for that one submission and the thread carries on.
                self.arena.log.note(f"judge {worker_id} failed on {job.submission_id}: {exc!r}")
                self.arena.record_verdict(
                    job.submission_id,
                    {
                        "verdict": int(Verdict.JUDGE_ERROR),
                        "phrase": "JUDGE_ERROR",
                        "detail": f"the judge itself failed: {exc!r}",
                        "tests_passed": "0/0",
                        "failures": [],
                    },
                    backend_name=backend.name,
                    worker_id=worker_id,
                )

    def _judge_one(self, backend, worker_id: str, job: Job) -> None:
        problem = get_problem(job.problem_id)
        contract = problem.contract

        # COMPILING is the last stage the parent can honestly claim to have seen: the
        # child is about to parse the source, and everything after that happens where we
        # cannot watch. See JUDGE_STAGES.
        self.arena.set_stage(job.submission_id, "COMPILING", worker_id=worker_id)

        run = backend.run(job.payload, time_limit_ms=run_budget_ms(contract.time_limit_ms))
        verdict = judge_record(
            run.result or {},
            contract.to_json(),
            outcome_hint=run.outcome_hint(),
        )
        self.arena.record_verdict(
            job.submission_id, verdict, backend_name=run.backend,
            worker_id=worker_id, wall_ms=run.wall_ms,
        )


# --------------------------------------------------------------------------
# Rooms
# --------------------------------------------------------------------------

@dataclass
class Room:
    """A private lobby: invite a specific opponent instead of taking whoever is waiting."""

    code: str
    host_session_id: str
    capacity: int
    problem_id: Optional[str]
    session_ids: List[str] = field(default_factory=list)
    ready: set = field(default_factory=set)


# --------------------------------------------------------------------------
# The arena
# --------------------------------------------------------------------------

class Arena:
    """All shared server state, and every operation that mutates it.

    One lock covers the whole object. The rule is short enough to say out loud: **no field
    below is touched without holding ``self._lock``.** Event *delivery* deliberately
    happens outside the lock — ``Session.push_event`` only appends to a queue, so a slow
    client can never be holding up the arena.
    """

    def __init__(self, log: WireLog, *, min_players: int, match_seconds: float,
                 countdown: float, submit_cooldown: float, room_cooldown: float,
                 problem_id: Optional[str], room_capacity: int, allow_panic: bool,
                 worker_token: str = "",
                 worker_heartbeat_ms: int = DEFAULT_WORKER_HEARTBEAT_MS,
                 limits: Optional[ArenaLimits] = None):
        self.log = log
        self.min_players = min_players
        self.match_seconds = match_seconds
        self.countdown = countdown
        self.submit_cooldown = submit_cooldown
        self.room_cooldown = room_cooldown
        self.pinned_problem = problem_id
        self.room_capacity = room_capacity
        self.allow_panic = allow_panic
        self.worker_token = worker_token
        self.worker_heartbeat_ms = max(250, int(worker_heartbeat_ms))
        self.limits = limits or ArenaLimits()

        self._lock = threading.RLock()

        self.users: Dict[str, PasswordRecord] = {}       # username -> salted verifier
        self.sessions: Dict[str, Session] = {}
        self.lobby: List[str] = []                       # session ids, oldest first
        self.rooms: Dict[str, Room] = {}
        self.matches: Dict[str, Match] = {}
        self.submissions: Dict[str, Submission] = {}
        self.workers: Dict[str, WorkerRecord] = {}
        self._feed_tokens: Dict[str, str] = {}

        self.jobs = JobQueue(self.limits.max_pending_jobs)
        self.pool: Optional[LocalJudgePool] = None
        self._failed_logins: Dict[str, deque[float]] = defaultdict(deque)
        self._registrations: Dict[str, deque[float]] = defaultdict(deque)

        self._session_counter = 0
        self._match_counter = 0
        self._submission_counter = 0
        self._problem_cursor = 0

        # Capability probe once, at startup, not once per submission: it runs real
        # micro-benchmarks and its answer cannot change while the process lives.
        self.capabilities = capabilities.probe()

    # -- ids ---------------------------------------------------------------

    def _next_session_id(self) -> str:
        with self._lock:
            self._session_counter += 1
            return f"c-{self._session_counter:04d}"

    def _next_match_id(self) -> str:
        with self._lock:
            self._match_counter += 1
            return f"m-{self._match_counter:04d}"

    def _next_submission_id(self) -> str:
        with self._lock:
            self._submission_counter += 1
            return f"s-{self._submission_counter:04d}"

    def _next_room_code(self) -> str:
        """A short code a player can read aloud, unique among live rooms."""
        while True:
            code = "".join(secrets.choice(ROOM_ALPHABET) for _ in range(4))
            if code not in self.rooms:
                return code

    def _next_problem_id(self) -> str:
        """Which problem the next match gets.

        Round-robin rather than random so a demo is reproducible: run the server, and the
        first match is always the same problem. ``--problem`` pins it outright.
        """
        if self.pinned_problem:
            return self.pinned_problem
        ids = problem_ids()
        with self._lock:
            chosen = ids[self._problem_cursor % len(ids)]
            self._problem_cursor += 1
        return chosen

    # -- sessions ----------------------------------------------------------

    def register_session(self, conn: Connection) -> Session:
        session = Session(self._next_session_id(), conn, self.log)
        with self._lock:
            self.sessions[session.id] = session
        return session

    def drop_session(self, session: Session) -> None:
        """Remove a disconnected session from everything that references it.

        A player who pulls the plug mid-match forfeits. Any other treatment would make
        disconnecting the optimal move whenever you are losing.
        """
        if session.worker_id is not None:
            self.unregister_worker(session, reason="connection closed")
            with self._lock:
                if session.token:
                    self._feed_tokens.pop(session.token, None)
                self.sessions.pop(session.id, None)
                session.state = State.CLOSED
            return

        with self._lock:
            if session.token:
                self._feed_tokens.pop(session.token, None)
            self.sessions.pop(session.id, None)
            if session.id in self.lobby:
                self.lobby.remove(session.id)

            room = self.rooms.get(session.room_code or "")
            if room is not None:
                self._leave_room_locked(session, room)

            match = self.matches.get(session.match_id or "")
            session.state = State.CLOSED

        if match is not None and match.state is not MatchState.ENDED:
            self.log.note(f"{session.label} disconnected mid-match — counted as a forfeit")
            self.forfeit(session, reason="DISCONNECT")

    # -- accounts ----------------------------------------------------------

    @staticmethod
    def _password_record(password: str) -> PasswordRecord:
        salt = secrets.token_bytes(16)
        return PasswordRecord(
            salt=salt,
            digest=hashlib.scrypt(password.encode("utf-8"), salt=salt,
                                 n=2**14, r=8, p=1, dklen=32),
        )

    @staticmethod
    def _peer_ip(session: Session) -> str:
        return session.conn.peer.rsplit(":", 1)[0].strip("[]")

    @staticmethod
    def _allow(bucket: Dict[str, deque[float]], key: str, limit: int, window_s: float) -> bool:
        now = time.monotonic()
        entries = bucket[key]
        while entries and now - entries[0] >= window_s:
            entries.popleft()
        if len(entries) >= limit:
            return False
        entries.append(now)
        return True

    def allow_registration(self, session: Session) -> bool:
        with self._lock:
            return self._allow(self._registrations, self._peer_ip(session),
                               MAX_REGISTRATIONS_PER_IP, REGISTRATION_WINDOW_S)

    def allow_login_attempt(self, session: Session, success: bool) -> bool:
        with self._lock:
            key = self._peer_ip(session)
            if success:
                self._failed_logins.pop(key, None)
                return True
            return self._allow(self._failed_logins, key, MAX_FAILED_LOGINS_PER_IP, LOGIN_WINDOW_S)

    def create_user(self, name: str, password: str) -> bool:
        """True if the account was created, False if the name is taken."""
        with self._lock:
            if name in self.users:
                return False
            if len(self.users) >= self.limits.max_users:
                raise ArenaCapacityExceeded("user capacity reached")
            self.users[name] = self._password_record(password)
            return True

    def issue_feed_token(self, session: Session) -> str:
        """Issue a token and index it for O(1) UDP ATTACH lookup."""
        with self._lock:
            if session.token:
                self._feed_tokens.pop(session.token, None)
            token = secrets.token_hex(16)
            session.token = token
            self._feed_tokens[token] = session.id
            return token

    def check_password(self, name: str, password: str) -> bool:
        """Constant-time-ish credential check.

        ``compare_digest`` rather than ``==`` so the comparison does not leak the length of
        the matching prefix through timing. The passwords themselves are held in plain text
        in memory, which the threat model names as an accepted limitation for a coursework
        arena — but the comparison costs nothing to get right, so it is got right.
        """
        with self._lock:
            stored = self.users.get(name)
        if stored is None:
            return False
        candidate = hashlib.scrypt(password.encode("utf-8"), salt=stored.salt,
                                   n=2**14, r=8, p=1, dklen=32)
        return secrets.compare_digest(stored.digest, candidate)

    # -- lobby and matchmaking ---------------------------------------------

    def enqueue_player(self, session: Session) -> Tuple[int, int]:
        """Add a session to the lobby. Returns ``(queue_pos, est_wait_ms)``."""
        with self._lock:
            self.lobby.append(session.id)
            session.state = State.QUEUED
            position = len(self.lobby)
            missing = max(0, self.min_players - len(self.lobby))
        # A crude estimate, and labelled as such: 5 s per player still needed. The header
        # exists to show that the protocol *can* carry a hint, not to be accurate.
        return position, missing * 5000

    def dequeue_player(self, session: Session) -> bool:
        with self._lock:
            if session.id not in self.lobby:
                return False
            self.lobby.remove(session.id)
            session.state = State.IDLE
            return True

    def try_matchmake(self) -> Optional[Match]:
        """Form a match if enough players are waiting. Called by the tick thread.

        Note that the arena matches **sessions**, not accounts. One person with two windows
        can duel themselves, which is a genuinely useful way to test the whole path alone —
        and it is also what makes the documented "two panes for one player" demo possible.
        """
        with self._lock:
            if len(self.lobby) < self.min_players:
                return None
            taken = [self.lobby.pop(0) for _ in range(self.min_players)]
            players = [self.sessions[sid] for sid in taken if sid in self.sessions]
            if len(players) < self.min_players:
                # Someone vanished between joining the lobby and being matched. Put the
                # survivors back and try again on the next tick.
                for player in players:
                    self.lobby.insert(0, player.id)
                return None
            match = self._create_match_locked(players)
        self._announce_match_found(match)
        return match

    def _create_match_locked(self, players: List[Session]) -> Match:
        now = time.monotonic()
        match = Match(
            id=self._next_match_id(),
            problem_id=self._next_problem_id(),
            session_ids=[player.id for player in players],
            duration_s=self.match_seconds,
            created_at=now,
            starts_at=now + self.countdown,
        )
        self.matches[match.id] = match
        for player in players:
            player.match_id = match.id
            player.last_match_id = match.id
            player.room_code = None
        return match

    def _announce_match_found(self, match: Match) -> None:
        """MATCH_FOUND: you have an opponent. The problem is not revealed yet.

        Two events rather than one, because they answer different questions. MATCH_FOUND
        says *who* — useful while the countdown runs. MATCH_START says *what* and starts
        the clock. Collapsing them would mean a player sees the problem before the timer
        they are being judged against has begun.
        """
        for session in self.match_sessions(match):
            opponents = [other.label for other in self.match_sessions(match)
                         if other.id != session.id]
            session.push_event("MATCH_FOUND", headers={
                "Match": match.id,
                "Opponents": _header_safe(", ".join(opponents) or "none (solo match)"),
                "Start-In-Ms": max(0, int((match.starts_at - time.monotonic()) * 1000)),
                "Detail": _header_safe(
                    f"opponent(s): {', '.join(opponents) or 'none (solo match)'}"
                ),
            })
        self.log.note(
            f"match {match.id} formed on {match.problem_id}: "
            f"{', '.join(s.label for s in self.match_sessions(match))}"
        )

    def match_sessions(self, match: Match) -> List[Session]:
        with self._lock:
            return [self.sessions[sid] for sid in match.session_ids if sid in self.sessions]

    # -- rooms -------------------------------------------------------------

    def create_room(self, session: Session, problem_id: Optional[str]) -> Room:
        with self._lock:
            room = Room(
                code=self._next_room_code(),
                host_session_id=session.id,
                capacity=self.room_capacity,
                problem_id=problem_id,
                session_ids=[session.id],
            )
            self.rooms[room.code] = room
            session.room_code = room.code
            session.state = State.IN_ROOM
            session.last_room_create_at = time.monotonic()
            return room

    def join_room(self, session: Session, code: str) -> Room:
        """Join by code. Raises ``KeyError`` if unknown, ``ValueError`` if full."""
        with self._lock:
            room = self.rooms[code]                      # KeyError -> 404 ROOM_NOT_FOUND
            if len(room.session_ids) >= room.capacity:
                raise ValueError("room is full")         # -> 409 ROOM_FULL
            room.session_ids.append(session.id)
            session.room_code = room.code
            session.state = State.IN_ROOM
            return room

    def mark_ready(self, session: Session) -> Tuple[Room, bool]:
        """Flag a player ready. Returns ``(room, everyone_is_ready)``."""
        with self._lock:
            room = self.rooms[session.room_code or ""]
            room.ready.add(session.id)
            enough = len(room.session_ids) >= max(1, self.min_players)
            return room, enough and room.ready >= set(room.session_ids)

    def start_room_match(self, room: Room) -> Optional[Match]:
        with self._lock:
            # READY handlers run concurrently. Only the first deferred action may consume
            # the room; repeated READY frames must not create duplicate matches.
            if self.rooms.get(room.code) is not room:
                return None
            self.rooms.pop(room.code, None)
            players = [self.sessions[sid] for sid in room.session_ids if sid in self.sessions]
            if not players:
                return None
            match = self._create_match_locked(players)
            if room.problem_id:
                match.problem_id = room.problem_id
        self._announce_match_found(match)
        return match

    def leave_room(self, session: Session) -> bool:
        with self._lock:
            room = self.rooms.get(session.room_code or "")
            if room is None:
                return False
            self._leave_room_locked(session, room)
            return True

    def find_room(self, code: Optional[str]) -> Optional[Room]:
        """Look a room up by code, or None. Exists so callers outside this class never
        touch ``self.rooms`` directly — the one-lock rule has no exceptions."""
        with self._lock:
            return self.rooms.get(code or "")

    def _leave_room_locked(self, session: Session, room: Room) -> None:
        if session.id in room.session_ids:
            room.session_ids.remove(session.id)
        room.ready.discard(session.id)
        session.room_code = None
        if session.state is State.IN_ROOM:
            session.state = State.IDLE
        if not room.session_ids:
            self.rooms.pop(room.code, None)

    def notify_room(self, room: Room, detail: str) -> None:
        """Tell everyone still in a room what just changed.

        Rooms are the one flow where a player's screen changes because of somebody *else's*
        request: the host has no other way to learn that a second player joined, or that
        they are ready. Display-only — nothing in a ROOM_UPDATE changes state, and a client
        that ignores the event entirely still plays a correct match, so it stays on the same
        side of the line as the UDP feed.
        """
        with self._lock:
            members = [self.sessions[sid] for sid in room.session_ids if sid in self.sessions]
            ready = len(room.ready)
            total = len(room.session_ids)
        for member in members:
            member.push_event("ROOM_UPDATE", headers={
                "Room": room.code,
                "Detail": _header_safe(
                    f"{detail} — {total}/{room.capacity} in room, {ready} ready"
                ),
            })

    # -- submissions -------------------------------------------------------

    def create_submission(self, session: Session, match: Match, lang: str,
                          source: str) -> Tuple[Submission, int]:
        """Record a submission and reserve its place in line, **without** queueing it yet.

        Split from :meth:`dispatch_submission` for one reason: ordering. The handler needs
        the submission id and the queue position to build the ``202 ACCEPTED``, but the
        judge must not start before that response has been written — a compile error comes
        back in milliseconds, and a client that received ``VERDICT`` before the ``202``
        naming the submission would have nothing to attach it to. So the handler calls this
        one synchronously, replies, and only then runs the dispatch as a deferred action.

        The returned position is ``depth + 1``: the place this job *will* occupy. Two
        players submitting in the same instant can both be told "1", and then one of them
        is really second. Queue-Pos is a hint, like Est-Wait-Ms — the honest number lands
        in the JUDGE_PROGRESS event, which carries the position ``JobQueue.put`` actually
        assigned.
        """
        with self._lock:
            self._prune_history_locked(time.monotonic())
            if len(self.submissions) >= self.limits.max_submissions:
                raise ArenaCapacityExceeded("submission capacity reached")
            position = self.jobs.reserve()
            if position is None:
                raise ArenaCapacityExceeded("pending judge queue is full")
            if (self.sessions.get(session.id) is not session
                    or session.match_id != match.id
                    or match.state is not MatchState.RUNNING
                    or time.monotonic() >= match.deadline):
                # The reservation has no associated source yet, so returning it here
                # is safe and keeps a deadline race from consuming queue capacity.
                self.jobs.cancel_reservation()
                raise SubmissionClosed("match is no longer accepting submissions")
            submission = Submission(
                id=self._next_submission_id(),
                session_id=session.id,
                user=session.label,
                match_id=match.id,
                problem_id=match.problem_id,
                lang=lang,
                source=source,
                created_at=time.monotonic(),
            )
            self.submissions[submission.id] = submission
            match.submissions.append(submission.id)
            passed, attempts = match.score.get(session.id, (0, 0))
            match.score[session.id] = (passed, attempts + 1)
            session.submissions.append(submission.id)
            session.last_submit_at = submission.created_at

        return submission, position

    def dispatch_submission(self, submission: Submission) -> int:
        """Queue an already-recorded submission for judging and announce it.

        Runs *after* the ``202 ACCEPTED`` has gone out. Returns the true queue position.
        """
        job = Job(
            submission_id=submission.id,
            problem_id=submission.problem_id,
            payload={
                "problem": submission.problem_id,
                "source": submission.source,
                "guard": True,
                "profile": True,
                "opcode_counter": self.capabilities.opcode_counter_name,
            },
        )
        self.jobs.put_reserved(job)
        position = self.jobs.depth()

        with self._lock:
            session = self.sessions.get(submission.session_id)
            match = self.matches.get(submission.match_id)

        # The submitter learns their place in line; the opponent learns only that a
        # submission happened. Never the source — that would hand over the solution.
        if session is not None:
            session.push_event("JUDGE_PROGRESS", headers={
                "Submission": submission.id,
                "Match": submission.match_id,
                "Stage": "QUEUED",
                "Queue-Pos": position,
            })
        if match is not None:
            for other in self.match_sessions(match):
                if other.id == submission.session_id:
                    continue
                other.push_event("OPPONENT_SUBMITTED", headers={
                    "Match": match.id,
                    "User": _header_safe(submission.user),
                    "Detail": "opponent submitted a solution",
                })
        return position

    def get_submission(self, submission_id: str) -> Optional[Submission]:
        with self._lock:
            return self.submissions.get(submission_id)

    def current_match(self, session: Session) -> Optional[Match]:
        """The match this session is playing right now, or None."""
        with self._lock:
            return self.matches.get(session.match_id or "")

    def last_match(self, session: Session) -> Optional[Match]:
        """The most recent match this session was in, finished or not.

        The pair of lookups is what makes ``410 MATCH_ENDED`` reachable: current_match goes
        None the moment a match ends, and without a memory of the last one every late
        SUBMIT would collapse into ``403 NOT_IN_MATCH``.
        """
        with self._lock:
            return self.matches.get(session.last_match_id or "")

    def set_stage(self, submission_id: str, stage: str, worker_id: str = "") -> None:
        """Advance a submission's judging stage and tell the submitter."""
        if stage not in JUDGE_STAGES:
            raise ValueError(f"unknown judge stage {stage!r}; expected one of {JUDGE_STAGES}")
        with self._lock:
            submission = self.submissions.get(submission_id)
            if submission is None:
                return
            submission.stage = stage
            session = self.sessions.get(submission.session_id)
        if session is None:
            return
        headers = {"Submission": submission_id, "Match": submission.match_id, "Stage": stage}
        if worker_id:
            headers["Worker"] = worker_id
        session.push_event("JUDGE_PROGRESS", headers=headers)

    def record_verdict(self, submission_id: str, verdict: dict, backend_name: str,
                       worker_id: str = "", wall_ms: float = 0.0) -> bool:
        """Attach the first verdict and publish it. False means a result already won.

        Remote dispatch is at-least-once when a lease expires, so two workers can finish the
        same submission. This check is the at-most-once half: the first result becomes
        authoritative and every later result is discarded instead of rewriting history.
        """
        # The backend that *actually ran* is stamped here, from the RunResult, never from
        # what the operator asked for. Design invariant 6: a run that fell back to
        # subprocess must not claim docker, because the security experiment's conclusions
        # rest on this field being true.
        verdict = dict(verdict)
        verdict["backend"] = backend_name
        if worker_id:
            verdict["worker"] = worker_id
        if wall_ms:
            verdict["judge_wall_ms"] = round(wall_ms, 1)

        with self._lock:
            submission = self.submissions.get(submission_id)
            if submission is None:
                return False
            if submission.verdict is not None:
                return False
            submission.verdict = verdict
            submission.stage = "DONE"
            submission.finished_at = time.monotonic()
            session = self.sessions.get(submission.session_id)
            match = self.matches.get(submission.match_id)
            if match is not None:
                current, attempts = match.score.get(submission.session_id, (0, 0))
                summary = str(verdict.get("tests_passed", "0/0"))
                try:
                    passed = int(summary.partition("/")[0])
                except ValueError:
                    passed = 0
                match.score[submission.session_id] = (max(current, passed), attempts)

        code = int(verdict.get("verdict", int(Verdict.JUDGE_ERROR)))
        body = json.dumps(verdict, indent=2).encode("utf-8")

        if session is not None:
            session.push_event("JUDGE_PROGRESS", headers={
                "Submission": submission_id,
                "Match": submission.match_id,
                "Stage": "DONE",
            })
            session.push_event("VERDICT", headers={
                "Submission": submission_id,
                "Match": submission.match_id,
                # Code *and* phrase in one header, which is the graded requirement applied
                # to the 6xx namespace as well as to response start lines. format_status
                # looks the phrase up from the verdict table, so the header cannot drift
                # from the body's own "phrase" field.
                "Verdict": format_status(code),
                "Content-Type": "application/json",
            }, body=body)

        self.log.status(code, detail=f"{submission_id} by {submission.user} "
                                     f"({submission.problem_id}, backend={backend_name})")

        # 600 ACCEPTED is the only verdict that wins a match. Everything else — including a
        # 606 for a correct-but-too-slow solution — leaves the clock running.
        if match is not None and code == int(Verdict.ACCEPTED):
            self.end_match(match, reason="SOLVED", winner=submission.user)
        return True

    # -- remote judge workers --------------------------------------------

    def judge_healthy(self) -> bool:
        """Whether either a local judge or a registered remote worker can drain jobs."""
        local = bool(self.pool and self.pool.healthy)
        with self._lock:
            remote = bool(self.workers)
        return local or remote

    def remote_worker_count(self) -> int:
        with self._lock:
            return len(self.workers)

    def register_worker(self, session: Session, worker_id: str, backend: str) -> Tuple[bool, str]:
        """Bind a worker id to this TCP session; duplicate live ids are rejected."""
        now = time.monotonic()
        with self._lock:
            if session.authenticated:
                return False, "a player session cannot become a judge worker"
            existing = self.workers.get(worker_id)
            if existing is not None and existing.session_id != session.id:
                return False, f"worker id {worker_id!r} is already connected"
            session.worker_id = worker_id
            self.workers[worker_id] = WorkerRecord(
                worker_id=worker_id,
                session_id=session.id,
                backend=backend,
                registered_at=now,
                last_seen=now,
            )
        self.log.note(f"worker {worker_id} registered (backend={backend})")
        return True, ""

    def unregister_worker(self, session: Session, reason: str) -> None:
        """Eject a worker and requeue its leased job, if that job still needs a verdict."""
        job = None
        worker_id = session.worker_id
        if worker_id is None:
            return
        with self._lock:
            record = self.workers.get(worker_id)
            if record is None or record.session_id != session.id:
                return
            self.workers.pop(worker_id, None)
            if record.active_job is not None:
                submission = self.submissions.get(record.active_job.submission_id)
                if submission is not None and not submission.done:
                    job = record.active_job
        if job is not None:
            position = self.jobs.requeue(job)
            self.set_stage(job.submission_id, "QUEUED")
            self.log.note(f"worker {worker_id} {reason}; requeued {job.submission_id} "
                          f"at position {position}")
        else:
            self.log.note(f"worker {worker_id} {reason}")

    def pull_worker_job(self, session: Session, wait_ms: int) -> Optional[Job]:
        """Long-poll the shared queue and lease one job to a registered worker."""
        worker_id = session.worker_id or ""
        with self._lock:
            record = self.workers.get(worker_id)
            if record is None or record.session_id != session.id:
                raise BadRequest("this connection is not a registered worker")
            if record.active_job is not None:
                raise BadRequest(f"worker {worker_id!r} already holds a job")

        deadline = time.monotonic() + max(0, wait_ms) / 1000.0
        while True:
            remaining = max(0.0, deadline - time.monotonic())
            job = self.jobs.get(timeout=remaining)
            if job is None:
                with self._lock:
                    current = self.workers.get(worker_id)
                    if current is not None:
                        current.last_seen = time.monotonic()
                return None

            with self._lock:
                current = self.workers.get(worker_id)
                submission = self.submissions.get(job.submission_id)
                if current is None or current.session_id != session.id:
                    should_requeue = submission is not None and not submission.done
                elif submission is None or submission.done:
                    should_requeue = False
                else:
                    now = time.monotonic()
                    current.active_job = job
                    current.last_seen = now
                    current.lease_deadline = now + self.worker_lease_ms / 1000.0
                    return job
            if should_requeue:
                self.jobs.requeue(job)
            if time.monotonic() >= deadline:
                return None

    @property
    def worker_lease_ms(self) -> int:
        return self.worker_heartbeat_ms * MISSED_HEARTBEATS_BEFORE_EJECTION

    def renew_worker_lease(self, session: Session, submission_id: str,
                           stage: str = "") -> Tuple[bool, str]:
        """Renew the named worker's current lease and optionally publish a real stage."""
        worker_id = session.worker_id or ""
        now = time.monotonic()
        with self._lock:
            record = self.workers.get(worker_id)
            if record is None or record.session_id != session.id:
                return False, "worker is no longer registered"
            job = record.active_job
            if job is None or job.submission_id != submission_id:
                return False, f"worker does not hold {submission_id}"
            if record.lease_deadline < now:
                return False, f"lease for {submission_id} has expired"
            record.last_seen = now
            record.lease_deadline = now + self.worker_lease_ms / 1000.0
        if stage:
            self.set_stage(submission_id, stage, worker_id=worker_id)
        return True, ""

    def accept_worker_result(self, session: Session, submission_id: str, verdict: dict,
                             backend: str, wall_ms: float) -> Tuple[bool, str]:
        """Accept a result only from the live lease owner; the first verdict wins."""
        worker_id = session.worker_id or ""
        with self._lock:
            record = self.workers.get(worker_id)
            if record is None or record.session_id != session.id:
                return False, "worker is no longer registered"
            job = record.active_job
            if job is None or job.submission_id != submission_id:
                return False, f"worker does not hold {submission_id}"
            if record.lease_deadline < time.monotonic():
                return False, f"lease for {submission_id} has expired"
            record.active_job = None
            record.lease_deadline = 0.0
            record.last_seen = time.monotonic()

        accepted = self.record_verdict(
            submission_id, verdict, backend_name=backend,
            worker_id=worker_id, wall_ms=wall_ms,
        )
        if not accepted:
            return False, f"{submission_id} already has a verdict"
        return True, ""

    def expire_worker_leases(self, now: float) -> None:
        """Eject workers that missed three heartbeats and reclaim their active jobs."""
        expired_sessions = []
        with self._lock:
            for record in list(self.workers.values()):
                if record.active_job is not None and record.lease_deadline <= now:
                    session = self.sessions.get(record.session_id)
                    if session is not None:
                        expired_sessions.append(session)
        for session in expired_sessions:
            self.unregister_worker(session, reason="missed three heartbeats and was ejected")

    # -- match lifecycle ---------------------------------------------------

    def start_pending_matches(self, now: float) -> None:
        """PENDING -> RUNNING once the countdown elapses. Called from the tick thread."""
        starting: List[Match] = []
        with self._lock:
            for match in self.matches.values():
                if match.state is MatchState.PENDING and now >= match.starts_at:
                    match.state = MatchState.RUNNING
                    match.deadline = now + match.duration_s
                    for session in self.match_sessions(match):
                        session.state = State.IN_MATCH
                    starting.append(match)

        for match in starting:
            problem = get_problem(match.problem_id)
            for session in self.match_sessions(match):
                session.push_event("MATCH_START", headers={
                    "Match": match.id,
                    "Problem": problem.id,
                    "Duration-Ms": int(match.duration_s * 1000),
                    "Required-Time": problem.contract.required_time,
                    "Required-Space": problem.contract.required_space,
                    "Detail": _header_safe(
                        f"{problem.title} — required_time={problem.contract.required_time} "
                        f"required_space={problem.contract.required_space} "
                        f"duration={int(match.duration_s)}s"
                    ),
                })
            self.log.note(f"match {match.id} started: {problem.title} "
                          f"({int(match.duration_s)}s on the clock)")

    def expire_matches(self, now: float) -> None:
        """End any match whose clock has run out."""
        expired: List[Match] = []
        with self._lock:
            for match in self.matches.values():
                if match.state is MatchState.RUNNING and now >= match.deadline:
                    expired.append(match)
        for match in expired:
            self.end_match(match, reason="TIMEOUT", winner=None)

    def forfeit(self, session: Session, reason: str = "FORFEIT") -> Optional[Match]:
        """One player gives up. The last one standing wins; a solo match just ends."""
        with self._lock:
            match = self.matches.get(session.match_id or "")
            if match is None or match.state is MatchState.ENDED:
                return None
            if session.id in match.session_ids:
                match.session_ids.remove(session.id)
            session.match_id = None
            if session.state is State.IN_MATCH:
                session.state = State.IDLE
            survivors = [self.sessions[sid] for sid in match.session_ids
                         if sid in self.sessions]

        if len(survivors) == 1:
            self.end_match(match, reason=reason, winner=survivors[0].label)
        elif not survivors:
            self.end_match(match, reason=reason, winner=None)
        return match

    def end_match(self, match: Match, reason: str, winner: Optional[str]) -> None:
        """Close a match once, whatever ended it, and return its players to IDLE."""
        with self._lock:
            if match.state is MatchState.ENDED:
                return                                   # already ended; ignore the race
            match.state = MatchState.ENDED
            match.end_reason = reason
            match.winner = winner
            match.ended_at = time.monotonic()
            sessions = self.match_sessions(match)
            for session in sessions:
                session.match_id = None
                if session.state is State.IN_MATCH:
                    session.state = State.IDLE

        for session in sessions:
            session.push_event("MATCH_END", headers={
                "Match": match.id,
                "Detail": _header_safe(
                    f"reason={reason} winner={winner or 'none'}"
                ),
            })
        self.log.note(f"match {match.id} ended: reason={reason} winner={winner or 'none'}")

    # -- shutdown ----------------------------------------------------------

    def all_sessions(self) -> List[Session]:
        """Every live session, as a snapshot. A copy, not a view — the caller iterates it
        without the lock, and sessions come and go while they do."""
        with self._lock:
            return list(self.sessions.values())

    def session_for_feed_token(self, token: str) -> Optional[Session]:
        """Resolve a display-scoped UDP attach token to its logged-in TCP session."""
        with self._lock:
            session_id = self._feed_tokens.get(token)
            session = self.sessions.get(session_id or "")
            if session is not None and session.authenticated and session.token == token:
                return session
        return None

    def prune_history(self, now: Optional[float] = None) -> None:
        with self._lock:
            self._prune_history_locked(time.monotonic() if now is None else now)

    def _prune_history_locked(self, now: float) -> None:
        """Remove only completed history; jobs and active matches are never pruned."""
        ttl = self.limits.history_ttl_s
        removable = [s for s in self.submissions.values() if s.done and s.finished_at
                     and now - s.finished_at >= ttl]
        completed = sorted((s for s in self.submissions.values() if s.done),
                           key=lambda s: s.finished_at or s.created_at)
        overflow = max(0, len(self.submissions) - self.limits.max_submissions)
        removable.extend(completed[:overflow])
        for submission in {s.id: s for s in removable}.values():
            self.submissions.pop(submission.id, None)
            session = self.sessions.get(submission.session_id)
            if session and submission.id in session.submissions:
                session.submissions.remove(submission.id)
            match = self.matches.get(submission.match_id)
            if match and submission.id in match.submissions:
                match.submissions.remove(submission.id)
        eligible = [m for m in self.matches.values() if m.state is MatchState.ENDED
                    and m.ended_at and now - m.ended_at >= ttl
                    and all(sid not in self.submissions for sid in m.submissions)]
        ended = sorted((m for m in self.matches.values() if m.state is MatchState.ENDED
                        and all(sid not in self.submissions for sid in m.submissions)),
                       key=lambda m: m.ended_at or m.created_at)
        overflow = max(0, len(ended) - self.limits.max_ended_matches)
        eligible.extend(ended[:overflow])
        for match in {m.id: m for m in eligible}.values():
            self.matches.pop(match.id, None)
            for session in self.sessions.values():
                if session.last_match_id == match.id:
                    session.last_match_id = None

    def feed_session_alive(self, session_id: str, user: str) -> bool:
        with self._lock:
            session = self.sessions.get(session_id)
            return bool(session and session.authenticated and session.user == user)

    def feed_session_is_alive(self, session_id: str) -> bool:
        with self._lock:
            session = self.sessions.get(session_id)
            return bool(session and session.authenticated)

    def feed_snapshots(self, now: float) -> List[dict]:
        """Copy the display-only state used to build UDP TICK/CLOCK/BOARD datagrams."""
        snapshots = []
        with self._lock:
            for match in self.matches.values():
                if match.state is not MatchState.RUNNING:
                    continue
                problem = get_problem(match.problem_id)
                total = len(problem.tests)
                players = []
                for session_id in match.session_ids:
                    session = self.sessions.get(session_id)
                    if session is None or session.user is None:
                        continue
                    passed, attempts = match.score.get(session_id, (0, 0))
                    players.append({
                        "user": session.user,
                        "passed": passed,
                        "total": total,
                        "subs": attempts,
                    })
                snapshots.append({
                    "match": match.id,
                    "remain": match.remaining_ms(now),
                    "players": players,
                    "session_ids": [sid for sid in match.session_ids if sid in self.sessions],
                })
        return snapshots

    def broadcast_shutdown(self) -> None:
        for session in self.all_sessions():
            session.push_event("SERVER_SHUTDOWN", headers={
                "Detail": "the arena is shutting down; reconnect later",
            })


def _header_safe(text: str, limit: int = 180) -> str:
    """Make a string safe to put in a header value, and short enough to read.

    Header values are terminated by CRLF, so a value *containing* CRLF would let a caller
    inject headers of their own — a real injection class, and usernames and judge details
    both reach headers from outside. Collapsing all whitespace removes the possibility
    rather than escaping it, and truncation keeps one long detail line from swamping the
    log that is itself a deliverable.
    """
    collapsed = " ".join(str(text).split())
    if len(collapsed) > limit:
        collapsed = collapsed[: limit - 3] + "..."
    return collapsed


# --------------------------------------------------------------------------
# One connection
# --------------------------------------------------------------------------

class _ClientHandler:
    """Reads requests from one player and writes their responses.

    This is the reader thread from the module docstring. It owns the request/response half
    of the socket; events reach the same socket through the session's writer thread.
    """

    def __init__(self, server: "ArenaServer", session: Session):
        self.server = server
        self.arena = server.arena
        self.session = session
        self.conn = session.conn
        self.log = server.log
        # Work that must happen *after* the response has been written. See _defer.
        self._deferred: List[Callable[[], None]] = []

    # -- the loop ----------------------------------------------------------

    def serve(self) -> None:
        while not self.server.stopping:
            try:
                message = self.conn.recv()
            except socket.timeout:
                # socket.timeout rather than TimeoutError: the two are the same class from
                # Python 3.10 on, but on 3.9 socket.timeout is only an OSError, so
                # catching TimeoutError there would miss it entirely and fall through to
                # the OSError branch — a silent close instead of a 408.
                #
                # The socket's own idle timeout fired. The buffered reader may be sitting
                # on a partial frame, so the stream is no longer trustworthy: say 408 and
                # close rather than pretending we can pick up where we left off.
                self.log.note(f"{self.session.label} idle past the timeout")
                self._send_unsolicited(Status.REQUEST_TIMEOUT,
                                       detail="no request within the idle timeout")
                return
            except FrameTooLarge as exc:
                # read_message refused *before* consuming the body, so those bytes are
                # still in the stream and there is no way to find the next frame boundary.
                # 413 then close — and no Seq to echo, because the frame was never
                # assembled far enough to have one.
                self.log.note(f"oversized frame from {self.session.label}: {exc}")
                self._send_unsolicited(Status.PAYLOAD_TOO_LARGE, detail=str(exc))
                return
            except ProtocolError as exc:
                self.log.note(f"unparseable frame from {self.session.label}: {exc}")
                self._send_unsolicited(Status.BAD_REQUEST, detail=str(exc))
                return
            except OSError:
                return                      # peer vanished; nothing left to answer to

            if message is None:
                return                      # clean close between frames

            if message.kind is not Kind.REQUEST:
                # A client has no business sending responses or events. Answering rather
                # than ignoring makes the rule visible in the log.
                self.log.note(f"{self.session.label} sent a {message.kind.value}, not a request")
                self._send_unsolicited(Status.BAD_REQUEST,
                                       detail="clients send requests; only the server pushes events")
                return

            response, keep_open = self._dispatch(message)
            try:
                self.conn.send(response)
            except OSError:
                return
            self._run_deferred()
            if not keep_open:
                return

    def _run_deferred(self) -> None:
        """Run whatever the handler deferred until after the response went out.

        Ordering, not tidiness. ``QUEUE`` may complete a match and ``SUBMIT`` hands work to
        a judge that could finish in milliseconds — if either ran before the response was
        written, a client could receive ``MATCH_FOUND`` before the ``202 QUEUED`` that
        caused it, or a ``VERDICT`` before the ``202 ACCEPTED`` naming the submission it
        refers to. Both are legal on the wire and both are baffling to read.
        """
        deferred, self._deferred = self._deferred, []
        for action in deferred:
            try:
                action()
            except Exception as exc:                     # noqa: BLE001
                self.log.note(f"deferred action failed for {self.session.label}: {exc!r}")

    def _defer(self, action: Callable[[], None]) -> None:
        self._deferred.append(action)

    # -- dispatch ----------------------------------------------------------

    def _dispatch(self, message: Message) -> Tuple[Message, bool]:
        """Turn one request into ``(response, keep_the_connection_open)``.

        The checks run from the most general to the most specific, and the order is a
        design decision rather than an accident:

        1. **Version** — a peer speaking another dialect may not even mean what we think by
           the rest of the frame, so nothing else is worth checking.
        2. **Seq** — without it there is nothing to correlate a reply to. This is the only
           response in the whole server that carries no ``Seq``, because there is none.
        3. **Body integrity** — if ``Body-SHA256`` disagrees with the body, the bytes we
           would act on are not the bytes that were sent.
        4. **Size** — refuse a body too large to be a plausible submission.
        5. **Method** — is this a verb we know?
        6. **Authentication** — 401, and never 403: "who are you" comes before "you may
           not do that here".
        7. **State** — 403 with a phrase naming the specific condition.

        Only then does a handler see the request, and a handler can therefore assume every
        one of those things is already true.
        """
        session = self.session

        if message.version != PROTOCOL_VERSION:
            # Answer, then close: there is no version negotiation in CDAP/1.0, so a peer
            # speaking something else has nothing to gain from staying connected. The
            # client's --bad-version flag exists to put this on camera.
            return self._error(message, Status.VERSION_UNSUPPORTED,
                               detail=f"this arena speaks {PROTOCOL_VERSION}, "
                                      f"you sent {message.version}"), False

        if message.seq is None:
            return self._error(message, Status.BAD_REQUEST,
                               detail="every request must carry a numeric Seq header"), True

        if message.body_hash_ok() is False:
            # The frame arrived intact as far as TCP is concerned; the *content* does not
            # match what the sender said it hashed to. That is exactly what --tamper
            # demonstrates, and it is why the check is here and not left to the transport.
            return self._error(message, Status.BODY_HASH_MISMATCH,
                               detail="Body-SHA256 does not match the body as received"), True

        if len(message.body) > MAX_SUBMISSION_BYTES:
            return self._error(message, Status.PAYLOAD_TOO_LARGE,
                               detail=f"body is {len(message.body)} bytes; the limit is "
                                      f"{MAX_SUBMISSION_BYTES}"), True

        spec = METHODS.get(message.method)
        if spec is None:
            return self._error(message, Status.METHOD_NOT_ALLOWED,
                               detail=f"unknown method {message.method!r}"), True

        if session.worker_id is not None and not message.method.startswith("WORKER_"):
            return self._error(message, Status.FORBIDDEN,
                               detail="a registered worker connection cannot act as a player"), True

        if spec.requires_auth and not session.authenticated:
            return self._error(message, Status.AUTH_FAILED,
                               detail=f"{message.method} requires a LOGIN first"), True

        if spec.states is not None and session.state not in spec.states:
            return self._error(message, Status.FORBIDDEN, phrase=spec.failure_phrase,
                               detail=f"{message.method} is not allowed in state "
                                      f"{session.state.value}"), True

        try:
            result = spec.handler(self, message)
        except BadRequest as exc:
            return self._error(message, Status.BAD_REQUEST, detail=str(exc)), True
        except Exception as exc:                          # noqa: BLE001
            # A bug in a handler is the server's fault, not the player's, and 500 says so.
            # Caught per request so one broken request cannot take the connection — or the
            # arena — down with it.
            self.log.note(f"handler for {message.method} raised: {exc!r}")
            return self._error(message, Status.INTERNAL_ERROR,
                               detail=f"unhandled {type(exc).__name__} in {message.method}"), True

        # Handlers return a bare Message in the ordinary case and a
        # ``(message, keep_open)`` pair only when the connection should not survive the
        # reply — which is LOGOUT and nothing else.
        if isinstance(result, tuple):
            return result
        return result, True

    # -- building replies --------------------------------------------------
    #
    # Every response the server sends is built by one of these. Centralising it buys two
    # guarantees that would otherwise rely on fifteen handlers each remembering: the
    # request's ``Seq`` is always echoed (design invariant 4), and any ``Detail`` is run
    # through ``_header_safe`` before it reaches a header value.

    def _reply(self, request: Message, status: Status, phrase: Optional[str] = None,
               headers: Optional[dict] = None, body=b"", detail: str = "") -> Message:
        """Build a response to ``request``, echoing its Seq."""
        fields = dict(headers or {})
        if detail:
            fields["Detail"] = _header_safe(detail)
        return Message.response(status, phrase=phrase, headers=fields, body=body,
                                seq=request.seq)

    def _ok(self, request: Message, status: Status = Status.OK,
            phrase: Optional[str] = None, headers: Optional[dict] = None,
            body=b"", detail: str = "") -> Message:
        """A success response. Named apart from ``_error`` only so handlers read clearly."""
        return self._reply(request, status, phrase=phrase, headers=headers,
                           body=body, detail=detail)

    def _error(self, request: Message, status: Status, phrase: Optional[str] = None,
               detail: str = "", headers: Optional[dict] = None) -> Message:
        """A failure response, which always explains itself in ``Detail``.

        The explanation is not decoration. Somebody watching ``403 NOT_IN_MATCH`` scroll
        past on video should be able to see *why* from the same line, and the wire log
        prints ``Detail``.
        """
        return self._reply(request, status, phrase=phrase, headers=headers, detail=detail)

    def _send_unsolicited(self, status: Status, detail: str = "") -> None:
        """Send a response that answers no request — so it carries no ``Seq``.

        Only the failures found *before* a request could be understood use this: an idle
        timeout with nothing pending, an oversized frame refused before its body was read,
        a frame whose framing was broken. There is no Seq to echo because there is no
        request, and inventing one would be worse than omitting it — the client would route
        the reply to whichever caller happened to be waiting on that number.

        Errors are swallowed. This is always the last thing written before a close, and a
        peer that has already gone is the normal case rather than an exception worth
        raising.
        """
        try:
            self.conn.send(Message.response(status, headers={"Detail": _header_safe(detail)}))
        except OSError:
            pass

    @staticmethod
    def _json_body(payload) -> bytes:
        """Serialise a JSON body. Indented, because a human reads it in the log."""
        return json.dumps(payload, indent=2).encode("utf-8")

    def _json_request(self, message: Message) -> dict:
        """Parse a request body as a JSON object, or raise ``BadRequest``.

        Raising rather than returning keeps the handlers short: they state what they need
        and the dispatcher turns a refusal into ``400``. The type check earns its place as
        much as the parse does — ``"hello"`` is valid JSON, and without the check it would
        fail later on ``.get`` with an ``AttributeError``, surfacing as ``500`` and blaming
        the server for a malformed request.
        """
        if not message.body:
            raise BadRequest(f"{message.method} needs a JSON body")
        try:
            payload = json.loads(message.text())
        except ValueError as exc:
            raise BadRequest(f"body is not valid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise BadRequest(f"body must be a JSON object, not {type(payload).__name__}")
        return payload

    def _worker_identity(self, message: Message) -> Tuple[Optional[str], Optional[Message]]:
        """Validate the worker id and pre-shared token carried by a worker request."""
        if not self.arena.worker_token:
            return None, self._error(message, Status.JUDGE_UNAVAILABLE,
                                     phrase="WORKERS_DISABLED",
                                     detail="remote workers are disabled until --worker-token is set")
        worker_id = str(message.headers.get("Worker", "")).strip()
        if not worker_id:
            return None, self._error(message, Status.BAD_REQUEST,
                                     detail="WORKER_* requests require a Worker header")
        for character in worker_id:
            if not (character.isalnum() or character in USERNAME_EXTRA_CHARS):
                return None, self._error(
                    message, Status.BAD_REQUEST,
                    detail=f"worker ids may contain letters, digits and "
                           f"{USERNAME_EXTRA_CHARS!r} only",
                )
        supplied = str(message.headers.get("Worker-Token", ""))
        if not secrets.compare_digest(supplied, self.arena.worker_token):
            return None, self._error(message, Status.AUTH_FAILED,
                                     detail="worker token was not accepted")
        if self.session.worker_id is not None and self.session.worker_id != worker_id:
            return None, self._error(
                message, Status.FORBIDDEN,
                detail=f"this connection is registered as {self.session.worker_id!r}, "
                       f"not {worker_id!r}",
            )
        return worker_id, None

    @staticmethod
    def _credentials(payload: dict) -> Tuple[str, str]:
        """Pull and validate ``user`` / ``pass`` out of a REGISTER or LOGIN body.

        The username rules are not fussiness. A username is echoed into ``Detail`` headers,
        into ``MATCH_FOUND`` events, and (Phase 7) into UDP datagram fields where a space
        would split one field into two. Validating once, here, at the only point a username
        enters the arena, is cheaper and far more reliable than escaping it everywhere it
        is later printed.
        """
        raw_user = payload.get("user", "")
        raw_password = payload.get("pass", "")
        if not isinstance(raw_user, str) or not isinstance(raw_password, str):
            raise BadRequest("'user' and 'pass' must both be strings")
        user = raw_user.strip()
        password = raw_password
        if not user:
            raise BadRequest("'user' is required")
        if not password:
            raise BadRequest("'pass' is required")
        if len(user) > MAX_USERNAME:
            raise BadRequest(f"username is longer than {MAX_USERNAME} characters")
        if len(password) > MAX_PASSWORD:
            raise BadRequest(f"password is longer than {MAX_PASSWORD} characters")
        for character in user:
            if not (character.isalnum() or character in USERNAME_EXTRA_CHARS):
                raise BadRequest(
                    f"a username may contain letters, digits and {USERNAME_EXTRA_CHARS!r} "
                    f"only; {character!r} is not allowed"
                )
        return user, password

    # ======================================================================
    # Handlers
    # ======================================================================
    #
    # One method per protocol verb, each registered with the state it is legal in. The
    # decorator above each handler *is* the precondition documentation, and the dispatcher
    # has already enforced it by the time the body runs — so a handler never re-checks
    # authentication or state, and can be read as "given that this is legal, do it".

    # -- handshake and accounts --------------------------------------------

    @method("WORKER_REGISTER", requires_auth=False, states=(State.INIT,))
    def handle_worker_register(self, message: Message) -> Message:
        """Authenticate a remote judge and add it to the pool."""
        worker_id, error = self._worker_identity(message)
        if error is not None:
            return error
        assert worker_id is not None
        payload = self._json_request(message)
        backend = _header_safe(str(payload.get("backend", "unknown")))
        try:
            requested_poll_ms = int(payload.get("poll_wait_ms", MAX_WORKER_POLL_MS))
        except (TypeError, ValueError) as exc:
            raise BadRequest(f"poll_wait_ms must be an integer: {exc}") from exc
        ok, detail = self.arena.register_worker(self.session, worker_id, backend)
        if not ok:
            return self._error(message, Status.CONFLICT, detail=detail)
        return self._ok(message, Status.CREATED, phrase="REGISTERED", headers={
            "Server": SERVER_NAME,
            "Heartbeat-Ms": self.arena.worker_heartbeat_ms,
            "Poll-Timeout-Ms": min(
                MAX_WORKER_POLL_MS,
                max(0, requested_poll_ms),
            ),
            "Lease-Ms": self.arena.worker_lease_ms,
        }, detail=f"worker {worker_id} joined the judge pool")

    @method("WORKER_PULL", requires_auth=False)
    def handle_worker_pull(self, message: Message) -> Message:
        """Long-poll for a job; an empty, completed poll is ``204 NO_CONTENT``."""
        worker_id, error = self._worker_identity(message)
        if error is not None:
            return error
        if self.session.worker_id != worker_id:
            return self._error(message, Status.AUTH_FAILED,
                               detail="WORKER_REGISTER must succeed before WORKER_PULL")
        wait_ms = message.headers.get_int("Wait-Ms")
        if wait_ms is None:
            wait_ms = MAX_WORKER_POLL_MS
        wait_ms = min(MAX_WORKER_POLL_MS, max(0, wait_ms))
        try:
            job = self.arena.pull_worker_job(self.session, wait_ms)
        except BadRequest as exc:
            return self._error(message, Status.CONFLICT, detail=str(exc))
        if job is None:
            return self._ok(message, Status.NO_CONTENT,
                            detail=f"no job became available within {wait_ms}ms")

        problem = get_problem(job.problem_id)
        body = self._json_body(job.payload)
        response = self._ok(message, Status.OK, headers={
            "Submission": job.submission_id,
            "Problem": job.problem_id,
            "Time-Limit-Ms": problem.contract.time_limit_ms,
            "Lease-Ms": self.arena.worker_lease_ms,
            "Content-Type": "application/json",
        }, body=body, detail=f"leased {job.submission_id} to {worker_id}")
        response.attach_body_hash()
        return response

    @method("WORKER_HEARTBEAT", requires_auth=False)
    def handle_worker_heartbeat(self, message: Message) -> Message:
        """Renew the active job lease and forward a stage the worker really observed."""
        worker_id, error = self._worker_identity(message)
        if error is not None:
            return error
        if self.session.worker_id != worker_id:
            return self._error(message, Status.AUTH_FAILED,
                               detail="WORKER_REGISTER must succeed before heartbeats")
        submission_id = str(message.headers.get("Submission", "")).strip()
        if not submission_id:
            return self._error(message, Status.BAD_REQUEST,
                               detail="WORKER_HEARTBEAT requires a Submission header")
        stage = str(message.headers.get("Stage", "")).strip().upper()
        if stage and stage not in JUDGE_STAGES:
            return self._error(message, Status.BAD_REQUEST,
                               detail=f"unknown judge stage {stage!r}")
        ok, detail = self.arena.renew_worker_lease(self.session, submission_id, stage)
        if not ok:
            return self._error(message, Status.CONFLICT, detail=detail)
        return self._ok(message, Status.OK, headers={
            "Submission": submission_id,
            "Lease-Ms": self.arena.worker_lease_ms,
        }, detail=f"lease renewed for {submission_id}")

    @method("WORKER_RESULT", requires_auth=False)
    def handle_worker_result(self, message: Message) -> Message:
        """Accept the first result from the worker that owns the live lease."""
        worker_id, error = self._worker_identity(message)
        if error is not None:
            return error
        if self.session.worker_id != worker_id:
            return self._error(message, Status.AUTH_FAILED,
                               detail="WORKER_REGISTER must succeed before WORKER_RESULT")
        submission_id = str(message.headers.get("Submission", "")).strip()
        if not submission_id:
            return self._error(message, Status.BAD_REQUEST,
                               detail="WORKER_RESULT requires a Submission header")
        verdict = self._json_request(message)
        try:
            code = int(verdict.get("verdict"))
            format_status(code)  # proves this is a declared 6xx verdict
            if not 600 <= code < 700:
                raise ValueError("not a judge verdict")
            wall_ms = float(message.headers.get("Wall-Ms", "0"))
        except (TypeError, ValueError, KeyError) as exc:
            return self._error(message, Status.BAD_REQUEST,
                               detail=f"invalid worker result: {exc}")
        backend = _header_safe(str(message.headers.get("Backend", "unknown")))
        ok, detail = self.arena.accept_worker_result(
            self.session, submission_id, verdict, backend, wall_ms,
        )
        if not ok:
            return self._error(message, Status.CONFLICT, detail=detail)
        return self._ok(message, Status.OK, headers={"Submission": submission_id},
                        detail=f"verdict accepted from {worker_id}")

    @method("HELLO", requires_auth=False, states=(State.INIT, State.GREETED))
    def handle_hello(self, message: Message) -> Message:
        """Version handshake. The only method a client may send before anything else.

        A ``HELLO`` that reached this handler has already passed the version check in
        ``_dispatch``, so reaching here *is* the agreement. The reply advertises what the
        arena can do, which is what makes the handshake worth having at all: a client can
        discover the problem list and the per-match clock without hard-coding them.
        """
        self.session.state = State.GREETED
        body = self._json_body({
            "protocol": PROTOCOL_VERSION,
            "server": SERVER_NAME,
            "problems": list(problem_ids()),
            "match_seconds": self.arena.match_seconds,
            "min_players": self.arena.min_players,
            "languages": ["python"],
            "judge": {
                "backend": self.arena.pool.backend_name if self.arena.pool else "remote",
                "healthy": self.arena.judge_healthy(),
                "remote_workers": self.arena.remote_worker_count(),
                "opcode_counter": self.arena.capabilities.opcode_counter_name,
            },
        })
        return self._ok(message, Status.OK, headers={
            "Server": SERVER_NAME,
            "Session": self.session.id,
            "Content-Type": "application/json",
        }, body=body, detail=f"welcome — this arena speaks {PROTOCOL_VERSION}")

    @method("REGISTER", requires_auth=False, states=(State.GREETED,))
    def handle_register(self, message: Message) -> Message:
        """Create an account. ``201 REGISTERED``, or ``409 USER_EXISTS``.

        Registering does not log you in. Keeping the two apart costs one extra round trip
        and buys a demo where ``201`` and ``200`` are visibly different events, which is
        worth more here than the round trip costs.
        """
        if not self.arena.allow_registration(self.session):
            return self._error(message, Status.RATE_LIMITED,
                               detail="too many registrations from this address; try again later")
        user, password = self._credentials(self._json_request(message))
        try:
            created = self.arena.create_user(user, password)
        except ArenaCapacityExceeded:
            return self._error(message, Status.JUDGE_UNAVAILABLE, phrase="SERVER_BUSY",
                               detail="account capacity reached; try again later")
        if not created:
            return self._error(message, Status.CONFLICT, phrase="USER_EXISTS",
                               detail=f"the name {user!r} is already registered")
        return self._ok(message, Status.CREATED, phrase="REGISTERED",
                        headers={"User": user},
                        detail=f"account {user!r} created — LOGIN next")

    @method("LOGIN", requires_auth=False, states=(State.GREETED,))
    def handle_login(self, message: Message) -> Message:
        """Authenticate and enter ``IDLE``.

        The session token is returned for one purpose: Phase 7's ``UDP_ATTACH``. UDP is
        connectionless, so a datagram carries no session — the token is how the server
        decides which player a datagram belongs to. It is display-scoped and nothing more,
        which the threat model states plainly (design invariant 5).
        """
        user, password = self._credentials(self._json_request(message))
        valid = self.arena.check_password(user, password)
        if not self.arena.allow_login_attempt(self.session, valid):
            return self._error(message, Status.RATE_LIMITED,
                               detail="too many failed logins from this address; try again shortly")
        if not valid:
            # One message for both "no such account" and "wrong password", deliberately:
            # distinguishing them would turn LOGIN into a way to enumerate usernames.
            return self._error(message, Status.AUTH_FAILED,
                               detail="unknown user or wrong password")

        self.session.user = user
        self.session.token = self.arena.issue_feed_token(self.session)
        self.session.state = State.IDLE
        return self._ok(message, Status.OK, headers={
            "User": user,
            "Session": self.session.id,
            "Token": self.session.token,
        }, detail=f"logged in as {user} — QUEUE to find a match")

    @method("LOGOUT", states=LOGGED_IN_STATES)
    def handle_logout(self, message: Message) -> Tuple[Message, bool]:
        """Log out and close the connection — the one handler that ends the conversation.

        The order matters and is the reason this returns a pair. The ``204`` is built first
        and the caller writes it *before* closing, so the client sees its answer rather than
        a dropped socket. The forfeit is deferred for the same reason: a player logging out
        mid-match loses it, and the ``MATCH_END`` event that says so must not overtake the
        response to the request that caused it.
        """
        session = self.session
        if session.state is State.IN_MATCH:
            self._defer(lambda: self.arena.forfeit(session, reason="LOGOUT"))
        elif session.state is State.QUEUED:
            self.arena.dequeue_player(session)
        elif session.state is State.IN_ROOM:
            self.arena.leave_room(session)

        user = session.user
        response = self._ok(message, Status.NO_CONTENT,
                            detail=f"goodbye {user} — closing the connection")
        return response, False

    # -- matchmaking -------------------------------------------------------

    @method("QUEUE", states=(State.IDLE, State.QUEUED))
    def handle_queue(self, message: Message) -> Message:
        """Join the matchmaking lobby.

        ``QUEUED`` is an accepted state here rather than a rejected one, and that is what
        makes ``409 ALREADY_QUEUED`` reachable. Had the table restricted this to ``IDLE``,
        a second ``QUEUE`` would come back ``403 WRONG_STATE`` — true, but far less useful
        than a code that names the actual conflict.

        The reply is ``202``, not ``200``: the request was accepted, and the thing it asked
        for has not happened yet. The match arrives later as a ``MATCH_FOUND`` event. That
        is the same distinction ``SUBMIT`` draws, and it is why the protocol has a 202 at
        all.
        """
        session = self.session
        if session.state is State.QUEUED:
            return self._error(message, Status.CONFLICT, phrase="ALREADY_QUEUED",
                               detail="you are already in the matchmaking queue")

        position, wait_ms = self.arena.enqueue_player(session)
        # Matchmaking runs after the reply so a solo-tester whose second window completes
        # the match cannot receive MATCH_FOUND before the 202 QUEUED that triggered it.
        self._defer(self.arena.try_matchmake)
        return self._ok(message, Status.ACCEPTED, phrase="QUEUED", headers={
            "Queue-Pos": position,
            "Est-Wait-Ms": wait_ms,
        }, detail=f"queued at position {position}; waiting for "
                  f"{self.arena.min_players} players")

    @method("DEQUEUE", states=(State.IDLE, State.QUEUED))
    def handle_dequeue(self, message: Message) -> Message:
        """Leave the lobby. ``409 NOT_QUEUED`` if you were not in it."""
        if not self.arena.dequeue_player(self.session):
            return self._error(message, Status.CONFLICT, phrase="NOT_QUEUED",
                               detail="you are not in the matchmaking queue")
        return self._ok(message, Status.OK, detail="left the queue")

    # -- private rooms -----------------------------------------------------

    @method("CREATE_ROOM", states=(State.IDLE,))
    def handle_create_room(self, message: Message) -> Message:
        """Open a private room and get a code to read out to an opponent.

        The optional ``problem`` field pins the room's problem, which is how the demo shows
        a chosen problem instead of the round-robin one. An unknown id is ``404``, not a
        silent fallback: guessing which problem the player meant would be worse than saying
        the name was wrong.
        """
        session = self.session
        elapsed = time.monotonic() - session.last_room_create_at
        if session.last_room_create_at and elapsed < self.arena.room_cooldown:
            return self._error(message, Status.RATE_LIMITED,
                               detail=f"wait {self.arena.room_cooldown - elapsed:.1f}s "
                                      f"before creating another room")

        problem_id = None
        if message.body:
            payload = self._json_request(message)
            if payload.get("problem"):
                problem_id = str(payload["problem"])
                if problem_id not in problem_ids():
                    return self._error(message, Status.NOT_FOUND,
                                       detail=f"unknown problem {problem_id!r}; known: "
                                              f"{', '.join(problem_ids())}")

        room = self.arena.create_room(session, problem_id)
        return self._ok(message, Status.CREATED, headers={
            "Room": room.code,
            "Capacity": room.capacity,
        }, detail=f"room {room.code} created — tell your opponent to JOIN_ROOM {room.code}")

    @method("JOIN_ROOM", states=(State.IDLE,))
    def handle_join_room(self, message: Message) -> Message:
        """Join a room by code. ``404 ROOM_NOT_FOUND`` or ``409 ROOM_FULL`` when it fails.

        Room codes are upper-cased on the way in, because a player types what they heard
        and case is not information the code carries.
        """
        payload = self._json_request(message)
        code = str(payload.get("room", "")).strip().upper()
        if not code:
            raise BadRequest("'room' is required — the 4-character room code")

        try:
            room = self.arena.join_room(self.session, code)
        except KeyError:
            return self._error(message, Status.NOT_FOUND, phrase="ROOM_NOT_FOUND",
                               detail=f"no room with code {code}")
        except ValueError:
            return self._error(message, Status.CONFLICT, phrase="ROOM_FULL",
                               detail=f"room {code} is full")

        self._defer(lambda: self.arena.notify_room(room, f"{self.session.label} joined"))
        return self._ok(message, Status.OK, headers={
            "Room": room.code,
            "Players": len(room.session_ids),
            "Capacity": room.capacity,
        }, detail=f"joined room {room.code} — send READY when you are")

    @method("READY", states=(State.IN_ROOM,), failure_phrase="NOT_IN_ROOM")
    def handle_ready(self, message: Message) -> Message:
        """Declare yourself ready. The match starts when everyone in the room has.

        Starting the match is deferred, and this is the clearest case for why the deferral
        mechanism exists at all: the last player to send ``READY`` completes the room, and
        without the deferral their ``MATCH_FOUND`` event would be written before the
        ``200 OK`` that answers the ``READY`` which caused it.
        """
        room, everyone = self.arena.mark_ready(self.session)
        if everyone:
            self._defer(lambda: self.arena.start_room_match(room))
        else:
            self._defer(lambda: self.arena.notify_room(room,
                                                       f"{self.session.label} is ready"))
        return self._ok(message, Status.OK, headers={
            "Room": room.code,
            "Ready": f"{len(room.ready)}/{len(room.session_ids)}",
        }, detail="everyone is ready — starting" if everyone else "waiting for the others")

    @method("LEAVE", states=(State.IN_ROOM,), failure_phrase="NOT_IN_ROOM")
    def handle_leave(self, message: Message) -> Message:
        """Leave a room. The room disappears once the last player is gone."""
        code = self.session.room_code
        self.arena.leave_room(self.session)
        # Read the room back *after* leaving: if that was the last player, leave_room
        # deleted it and there is nobody left to notify.
        room = self.arena.find_room(code)
        if room is not None:
            self._defer(lambda: self.arena.notify_room(room,
                                                       f"{self.session.label} left"))
        return self._ok(message, Status.NO_CONTENT, detail=f"left room {code}")

    # -- playing a match ---------------------------------------------------

    @method("FORFEIT", states=(State.IN_MATCH,), failure_phrase="NOT_IN_MATCH")
    def handle_forfeit(self, message: Message) -> Message:
        """Give up. The opponent wins; a solo match simply ends."""
        session = self.session
        match_id = session.match_id
        self._defer(lambda: self.arena.forfeit(session, reason="FORFEIT"))
        return self._ok(message, Status.OK, headers={"Match": match_id or "-"},
                        detail="forfeited — the match is over")

    @method("GET_PROBLEM", states=LOGGED_IN_STATES, failure_phrase="NOT_IN_MATCH")
    def handle_get_problem(self, message: Message) -> Message:
        """Fetch the problem statement, samples, and the complexity contract.

        Two refusals here are worth more than they look:

        * **Before the clock starts** the problem is *not* revealed. ``MATCH_FOUND`` says
          who you are playing; ``MATCH_START`` says what you are solving and starts the
          timer. Handing over the statement during the countdown would give a player free
          thinking time that the clock they are judged against never saw.
        * **After the match ends** it *is* still served, because re-reading the problem you
          just played costs nothing and helps anyone reviewing a verdict.

        The contract travels with the statement on purpose. A player being held to
        ``O(n)`` has to be told so before they are judged on it — that is the whole premise
        of the arena, and hiding it would make ``606`` unfair rather than interesting.
        """
        session = self.session
        match = self.arena.current_match(session) or self.arena.last_match(session)
        if match is None:
            return self._error(message, Status.FORBIDDEN, phrase="NOT_IN_MATCH",
                               detail="you are not in a match")
        if match.state is MatchState.PENDING:
            return self._error(message, Status.FORBIDDEN, phrase="WRONG_STATE",
                               detail="the problem is revealed when the clock starts "
                                      "(wait for the MATCH_START event)")

        problem = get_problem(match.problem_id)
        return self._ok(message, Status.OK, headers={
            "Match": match.id,
            "Problem": problem.id,
            "Time-Remaining-Ms": match.remaining_ms(time.monotonic()),
            "Content-Type": "application/json",
        }, body=self._json_body(problem.to_json()),
           detail=f"{problem.title} — required_time={problem.contract.required_time} "
                  f"required_space={problem.contract.required_space}")

    @method("SUBMIT", states=LOGGED_IN_STATES, failure_phrase="NOT_IN_MATCH")
    def handle_submit(self, message: Message) -> Message:
        """Submit source code for judging. The busiest handler in the protocol.

        The state table lets every logged-in state through, and the checks below then
        separate cases the table would have collapsed into one ``403``:

        ==========================  ====================================================
        ``415 UNSUPPORTED_LANGUAGE``  the arena runs Python; ``--lang rust`` proves it
        ``403 NOT_IN_MATCH``          never been in a match
        ``410 MATCH_ENDED``           was in one, and it finished — *not* the same thing
        ``403 WRONG_STATE``           the countdown is still running
        ``429 SUBMIT_COOLDOWN``       too soon after the last attempt
        ``503 JUDGE_UNAVAILABLE``     no judge can take it (``--judges 0``)
        ``202 ACCEPTED``              queued; the verdict arrives as an event
        ==========================  ====================================================

        ``202`` rather than ``200`` is the honest code: judging takes seconds, so the
        response can only confirm receipt. The verdict follows as a ``VERDICT`` event, and
        ``GET_SUBMISSION`` can re-read it if the event was missed.
        """
        session = self.session
        lang = str(message.headers.get("Lang", "python")).strip().lower()
        if lang not in SUPPORTED_LANGUAGES:
            # Checked before the match, so the demo flag behaves the same in every state:
            # a language this arena cannot run is a property of the request, not of the
            # session it arrived on.
            return self._error(message, Status.UNSUPPORTED_LANGUAGE,
                               detail=f"this arena runs {', '.join(SUPPORTED_LANGUAGES)}; "
                                      f"you sent Lang={lang}")

        match = self.arena.current_match(session)
        if match is None:
            previous = self.arena.last_match(session)
            if previous is not None and previous.state is MatchState.ENDED:
                return self._error(message, Status.MATCH_ENDED,
                                   headers={"Match": previous.id},
                                   detail=f"match {previous.id} ended "
                                          f"({previous.end_reason}); submissions closed")
            return self._error(message, Status.FORBIDDEN, phrase="NOT_IN_MATCH",
                               detail="you are not in a match — QUEUE first")

        if match.state is MatchState.ENDED:
            return self._error(message, Status.MATCH_ENDED, headers={"Match": match.id},
                               detail=f"match {match.id} ended ({match.end_reason})")
        if match.state is MatchState.PENDING:
            return self._error(message, Status.FORBIDDEN, phrase="WRONG_STATE",
                               detail="the clock has not started yet; wait for MATCH_START")

        problem = get_problem(match.problem_id)
        if lang not in problem.languages:
            return self._error(message, Status.UNSUPPORTED_LANGUAGE,
                               detail=f"{problem.id} accepts "
                                      f"{', '.join(problem.languages)}, not {lang}")

        elapsed = time.monotonic() - session.last_submit_at
        if session.last_submit_at and elapsed < self.arena.submit_cooldown:
            return self._error(message, Status.RATE_LIMITED, phrase="SUBMIT_COOLDOWN",
                               detail=f"wait {self.arena.submit_cooldown - elapsed:.1f}s "
                                      f"before submitting again")

        source = message.text()
        if not source.strip():
            raise BadRequest("the body must contain the source code to judge")

        if not self.arena.judge_healthy():
            # Backpressure, and a truthful one: the submission is *not* recorded, so the
            # cooldown does not start and the player can retry immediately once a judge is
            # available. Accepting it into a queue nothing drains would be worse than
            # refusing it.
            return self._error(message, Status.JUDGE_UNAVAILABLE,
                               detail="no judge is available to run this submission; "
                                      "try again shortly")

        try:
            submission, position = self.arena.create_submission(session, match, lang, source)
        except SubmissionClosed:
            # The tick thread may have ended the match after the checks above. Do not record
            # a source; return the state that won the race.
            current = self.arena.current_match(session)
            if current is None or current.state is MatchState.ENDED:
                return self._error(message, Status.MATCH_ENDED, headers={"Match": match.id},
                                   detail=f"match {match.id} ended; submissions closed")
            return self._error(message, Status.FORBIDDEN, phrase="WRONG_STATE",
                               detail="the clock has not started yet; wait for MATCH_START")
        except ArenaCapacityExceeded as exc:
            if "pending judge queue" in str(exc):
                return self._error(message, Status.JUDGE_UNAVAILABLE, phrase="JUDGE_QUEUE_FULL",
                                   detail="the judge queue is full; retry after a verdict arrives")
            return self._error(message, Status.JUDGE_UNAVAILABLE, phrase="SERVER_BUSY",
                               detail="submission history is at capacity; try again later")
        # Queue it only after the 202 is on the wire — see Arena.create_submission for why
        # a fast verdict beating its own submission id is a real problem and not a
        # theoretical one.
        self._defer(lambda: self.arena.dispatch_submission(submission))
        return self._ok(message, Status.ACCEPTED, headers={
            "Submission": submission.id,
            "Match": match.id,
            "Queue-Pos": position,
            "Problem": problem.id,
        }, detail=f"queued for judging at position {position}; "
                  f"the verdict arrives as a VERDICT event")

    @method("GET_SUBMISSION", states=LOGGED_IN_STATES)
    def handle_get_submission(self, message: Message) -> Message:
        """Re-read a submission's verdict, or ask whether it is still being judged.

        This is what makes the dropped-event path in ``Session.push_event`` acceptable. An
        event is a courtesy; a client that missed one is never stuck, because the
        authoritative answer is always retrievable with a request. Any design that pushes
        state needs a pull path beside it, and this is CDAP's.

        Reading somebody else's submission is ``403``, not ``404``: pretending it does not
        exist would be a small lie, and the submission id is not a secret anyway — the
        source code behind it is.
        """
        payload = self._json_request(message)
        submission_id = str(payload.get("submission", "")).strip()
        if not submission_id:
            raise BadRequest("'submission' is required")

        submission = self.arena.get_submission(submission_id)
        if submission is None:
            return self._error(message, Status.NOT_FOUND, phrase="SUBMISSION_NOT_FOUND",
                               detail=f"no submission {submission_id}")
        if submission.session_id != self.session.id:
            return self._error(message, Status.FORBIDDEN,
                               detail=f"{submission_id} belongs to another player")

        headers = {
            "Submission": submission.id,
            "Match": submission.match_id,
            "Stage": submission.stage,
            "Content-Type": "application/json",
        }
        if not submission.done:
            # Still 202: accepted, not finished. The same code the SUBMIT got, for the same
            # reason — nothing has been decided yet.
            return self._ok(message, Status.ACCEPTED, headers=headers,
                            detail=f"{submission.id} is still being judged "
                                   f"(stage {submission.stage})")

        verdict = submission.verdict or {}
        code = int(verdict.get("verdict", int(Verdict.JUDGE_ERROR)))
        headers["Verdict"] = format_status(code)
        return self._ok(message, Status.OK, headers=headers,
                        body=self._json_body(verdict),
                        detail=f"{submission.id}: {format_status(code)}")

    # -- debug -------------------------------------------------------------

    @method("DEBUG_PANIC", states=LOGGED_IN_STATES)
    def handle_debug_panic(self, message: Message) -> Message:
        """Raise on purpose, so ``500 INTERNAL_ERROR`` can be demonstrated.

        Every other status in the table is reachable by a client doing something wrong.
        ``500`` is not — it needs the *server* to be wrong, and a server with no bugs has
        no way to show it. So the arena keeps one method that fails deliberately, behind
        ``--allow-panic`` and answering ``405`` without it, and the dispatcher's catch-all
        turns the exception into the ``500`` it would produce for a real bug.

        The point being demonstrated is not the crash. It is that the crash is *contained*:
        one request fails, the connection survives, and every other player keeps playing.
        """
        if not self.arena.allow_panic:
            return self._error(message, Status.METHOD_NOT_ALLOWED,
                               detail="DEBUG_PANIC needs the server's --allow-panic flag")
        raise RuntimeError("DEBUG_PANIC: a deliberate failure, to show 500 is reachable")


# --------------------------------------------------------------------------
# The listening server
# --------------------------------------------------------------------------

class ArenaServer:
    """Owns the listening socket, the threads, and the shutdown path.

    Three kinds of thread run here, and the split is the answer to "what makes this a
    server rather than a script":

    * the **accept** loop (this thread) — hands each new socket to a session and returns
      to accepting immediately, so one slow client cannot delay another's connect;
    * two threads **per session** — a reader that blocks on ``recv`` and a writer that
      blocks on the outbox (see the module docstring for why the second one exists);
    * one **tick** thread — the arena's clock, forming matches and expiring deadlines.

    The tick thread is what makes the server's behaviour depend on time passing rather than
    on requests arriving. A match has to end when its clock runs out even if both players
    have gone quiet, and nothing else in the design would notice.
    """

    def __init__(self, arena: Arena, log: WireLog, *, host: str, tcp_port: int,
                 udp_port: Optional[int], idle_timeout: float, backlog: int = 16,
                 max_sessions: int = DEFAULT_MAX_SESSIONS,
                 max_feed_endpoints: int = DEFAULT_MAX_FEED_ENDPOINTS):
        self.arena = arena
        self.log = log
        self.host = host
        self.tcp_port = tcp_port
        self.udp_port = udp_port
        self.idle_timeout = idle_timeout
        self.backlog = backlog
        self.max_feed_endpoints = max_feed_endpoints
        self._session_slots = threading.BoundedSemaphore(max_sessions)

        self.stopping = False
        self._listener: Optional[socket.socket] = None
        self._udp_socket: Optional[socket.socket] = None
        self._threads: set[threading.Thread] = set()
        self._threads_lock = threading.Lock()
        self._udp_lock = threading.Lock()
        # user -> {UDP source address -> authenticated TCP session id}.  A separate
        # feed-only login for the same user should see that user's game, while the session
        # id still lets cleanup discard endpoints when their attach session closes.
        self._feed_endpoints: Dict[str, dict] = {}
        self._feed_seq: Dict[str, int] = {}

    # -- lifecycle ---------------------------------------------------------

    def serve_forever(self) -> None:
        """Bind, then accept until stopped. Returns once shutdown is complete."""
        self._listener = self._bind()
        if self.udp_port:
            self._udp_socket = self._bind_udp()
        self._start_thread(self._tick_loop, "tick")

        if self._udp_socket is not None:
            self._start_thread(self._udp_loop, "udp-feed")

        self.log.note(f"arena listening on {self.host}:{self.tcp_port} (TCP) — "
                      f"protocol {PROTOCOL_VERSION}")
        if self.udp_port:
            self.log.note(f"live feed listening on {self.host}:{self.udp_port} (UDP) — "
                          "display-only; matches remain correct if every datagram is lost")

        try:
            self._accept_loop()
        except KeyboardInterrupt:
            self.log.note("interrupted — shutting down")
        finally:
            self.stop()

    def _bind(self) -> socket.socket:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # SO_REUSEADDR so a restart during the demo does not hit "address already in use"
        # while the previous socket sits in TIME_WAIT. On Windows this flag is closer to
        # SO_REUSEPORT than it is on Linux, which is fine here — one arena per port.
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((self.host, self.tcp_port))
        listener.listen(self.backlog)
        # A short accept timeout is what makes Ctrl+C work. A blocking accept() on Windows
        # is not interrupted by the signal, so without this the arena would only notice the
        # interrupt after the next connection — which, on an idle server, is never.
        listener.settimeout(0.5)
        return listener

    def _bind_udp(self) -> socket.socket:
        feed = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        feed.bind((self.host, int(self.udp_port or 0)))
        feed.settimeout(0.5)
        return feed

    def _accept_loop(self) -> None:
        assert self._listener is not None
        while not self.stopping:
            try:
                sock, address = self._listener.accept()
            except socket.timeout:
                continue                    # the normal case: nobody connected this half-second
            except OSError:
                if self.stopping:
                    return
                raise
            if not self._session_slots.acquire(blocking=False):
                self._reject_busy(sock)
                continue
            self._start_session(sock, address)

    def _start_thread(self, target, name: str, args=()) -> threading.Thread:
        """Track threads only while alive, so completed sessions are reaped."""
        thread: Optional[threading.Thread] = None
        def run() -> None:
            try:
                target(*args)
            finally:
                with self._threads_lock:
                    if thread is not None:
                        self._threads.discard(thread)
        thread = threading.Thread(target=run, name=name, daemon=True)
        with self._threads_lock:
            self._threads.add(thread)
        thread.start()
        return thread

    def _reject_busy(self, sock: socket.socket) -> None:
        try:
            sock.sendall(Message.response(
                Status.JUDGE_UNAVAILABLE, phrase="SERVER_BUSY",
                headers={"Detail": "concurrent session limit reached"},
            ).encode())
        except OSError:
            pass
        finally:
            try:
                sock.close()
            except OSError:
                pass

    def _start_session(self, sock: socket.socket, address) -> None:
        """Wrap one accepted socket in a session and give it its two threads."""
        # An idle timeout on the *session* socket, not on the listener: a client that
        # connects and says nothing holds a thread and a file descriptor, and the protocol
        # has a status code for exactly that (408 REQUEST_TIMEOUT).
        sock.settimeout(self.idle_timeout)
        # Nagle off. Every CDAP frame is a complete message the peer is waiting on, so
        # delaying a small write to coalesce it with the next one adds latency and buys
        # nothing — there is no next one until the peer replies.
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass                            # not fatal; some stacks refuse it

        conn = Connection(sock, log=self.log)
        session = self.arena.register_session(conn)
        self.log.note(f"connection from {conn.peer} → session {session.id}")

        handler = _ClientHandler(self, session)
        self._start_thread(self._run_session, f"read-{session.id}", (handler, session))
        self._start_thread(self._writer_loop, f"write-{session.id}", (session,))

    def _run_session(self, handler: "_ClientHandler", session: Session) -> None:
        """Run one session's reader thread and clean up however it ends."""
        try:
            handler.serve()
        except Exception as exc:                          # noqa: BLE001
            # The dispatcher already turns a handler's exception into a 500, so reaching
            # here means the framing layer itself failed. Log and drop this one session;
            # the arena carries on.
            self.log.note(f"session {session.id} failed: {exc!r}")
        finally:
            session.stop_writer()
            self.arena.drop_session(session)
            session.conn.close()
            self._remove_feed_endpoints(session.id)
            self._session_slots.release()
            self.log.note(f"session {session.id} ({session.label}) closed")

    def _writer_loop(self, session: Session) -> None:
        """Drain one session's event outbox — the only thread that sends its events.

        Blocking on ``queue.get`` and then on ``sendall`` is the whole idea: both blocks
        happen on a thread that belongs to this session alone, so a client that has stopped
        reading its socket stalls itself and nobody else. The judge thread that produced the
        event never waits.
        """
        while True:
            message = session.outbox.get()
            if message is None:
                return                      # the shutdown sentinel from stop_writer
            try:
                session.conn.send(message)
            except OSError:
                return                      # peer gone; the reader thread will clean up

    def _tick_loop(self) -> None:
        """The arena's clock: form matches, start them, end them when time runs out."""
        while not self.stopping:
            time.sleep(TICK_INTERVAL_S)
            now = time.monotonic()
            try:
                self.arena.try_matchmake()
                self.arena.start_pending_matches(now)
                self.arena.expire_matches(now)
                self.arena.expire_worker_leases(now)
                self.arena.prune_history(now)
                self._broadcast_udp(now)
            except Exception as exc:                      # noqa: BLE001
                # This thread must not die. If it did, matches would never start and never
                # end, and the failure would look like a hang rather than an error.
                self.log.note(f"tick failed: {exc!r}")

    # -- UDP live feed ----------------------------------------------------

    def _udp_loop(self) -> None:
        """Learn client endpoints from ATTACH datagrams; never changes match state."""
        feed = self._udp_socket
        if feed is None:
            return
        while not self.stopping:
            try:
                data, address = feed.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                return
            peer = f"{address[0]}:{address[1]}"
            try:
                if not data.startswith((PROTOCOL_VERSION + " ").encode("ascii")):
                    raise ProtocolError(f"UDP feed requires {PROTOCOL_VERSION}")
                kind, fields = decode_datagram(data)
            except (ProtocolError, FrameTooLarge) as exc:
                self.log.udp_dropped(f"malformed datagram from {peer}: {exc}")
                continue
            self.log.udp_received(kind, fields, peer=peer)
            if kind != "ATTACH":
                self.log.udp_dropped(f"{kind} from {peer}: clients may send ATTACH only")
                continue
            token = str(fields.get("session", ""))
            session = self.arena.session_for_feed_token(token)
            if session is None or session.user is None:
                self.log.udp_dropped(f"ATTACH from {peer}: unknown or expired session token")
                continue
            with self._udp_lock:
                endpoints = self._feed_endpoints.setdefault(session.user, {})
                endpoints.pop(address, None)
                endpoints[address] = time.monotonic()
                while len(endpoints) > self.max_feed_endpoints:
                    endpoints.pop(next(iter(endpoints)))
            self.log.note(f"UDP feed attached for {session.user} from source address {peer}")

    def _broadcast_udp(self, now: float) -> None:
        """Send current display snapshots. No caller relies on delivery or ordering."""
        if self._udp_socket is None:
            return
        for snapshot in self.arena.feed_snapshots(now):
            players = snapshot["players"]
            targets = self._feed_targets([player["user"] for player in players])
            if not targets:
                continue
            match_id = snapshot["match"]
            self._send_feed("CLOCK", {
                "match": match_id,
                "seq": self._next_feed_seq(match_id),
                "remain": snapshot["remain"],
            }, targets)
            for player in players:
                self._send_feed("TICK", {
                    "match": match_id,
                    "seq": self._next_feed_seq(match_id),
                    "t": int(time.time() * 1000),
                    "player": player["user"],
                    "passed": player["passed"],
                    "total": player["total"],
                    "subs": player["subs"],
                }, targets)
            board = ",".join(
                f"{player['user']}:{player['passed']}:{player['subs']}"
                for player in players
            )
            self._send_feed("BOARD", {
                "match": match_id,
                "seq": self._next_feed_seq(match_id),
                "e": board,
            }, targets)

    def _feed_targets(self, users: List[str]) -> set:
        with self._udp_lock:
            targets = set()
            for user in users:
                endpoints = self._feed_endpoints.get(user, {})
                for address in list(endpoints):
                    attached_session_id = endpoints[address]
                    if self.arena.feed_session_is_alive(attached_session_id):
                        targets.add(address)
                    else:
                        endpoints.pop(address, None)
                if not endpoints:
                    self._feed_endpoints.pop(user, None)
            return targets

    def _remove_feed_endpoints(self, session_id: str) -> None:
        with self._udp_lock:
            for user, endpoints in list(self._feed_endpoints.items()):
                for address, attached_session_id in list(endpoints.items()):
                    if attached_session_id == session_id:
                        endpoints.pop(address, None)
                if not endpoints:
                    self._feed_endpoints.pop(user, None)

    def _next_feed_seq(self, match_id: str) -> int:
        with self._udp_lock:
            value = self._feed_seq.get(match_id, 0) + 1
            self._feed_seq[match_id] = value
            return value

    def _send_feed(self, kind: str, fields: dict, targets: set) -> None:
        feed = self._udp_socket
        if feed is None:
            return
        data = encode_datagram(kind, fields)
        for address in targets:
            try:
                feed.sendto(data, address)
                self.log.udp_sent(kind, fields, peer=f"{address[0]}:{address[1]}")
            except OSError as exc:
                self.log.udp_dropped(f"send to {address[0]}:{address[1]} failed: {exc}")

    def stop(self) -> None:
        """Shut down once: tell the players, stop the judges, close the socket."""
        if self.stopping:
            return
        self.stopping = True

        # The courtesy first, while the sockets are still open. A client that gets
        # SERVER_SHUTDOWN can say so instead of reporting a bare connection reset.
        self.arena.broadcast_shutdown()
        if self.arena.pool is not None:
            self.arena.pool.stop()

        sessions = self.arena.all_sessions()
        for session in sessions:
            session.stop_writer()

        # Give the writer threads a moment to flush the shutdown event they were just
        # handed. Bounded, and deliberately so: the grace period is a courtesy, not a
        # correctness requirement, and a client that has stopped reading must not be able
        # to hold the server open by refusing to drain its socket.
        time.sleep(0.2)

        if self._listener is not None:
            try:
                self._listener.close()
            except OSError:
                pass
        if self._udp_socket is not None:
            try:
                self._udp_socket.close()
            except OSError:
                pass
        for session in sessions:
            session.conn.close()
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            with self._threads_lock:
                threads = list(self._threads)
            if not threads:
                break
            for thread in threads:
                if thread is not threading.current_thread():
                    thread.join(timeout=max(0.0, deadline - time.monotonic()))
        self.log.note("arena stopped")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m cdap.server",
        description="CDAP arena server — matchmaking, matches, and the judge queue.",
    )
    parser.add_argument("--host", default="127.0.0.1",
                        help="interface to bind (default: 127.0.0.1, loopback only)")
    parser.add_argument("--tcp-port", type=int, default=5050,
                        help="TCP port for the request/response protocol (default: 5050)")
    parser.add_argument("--udp-port", type=int, default=5051,
                        help="UDP port for the live progress feed; 0 disables it (default: 5051)")
    parser.add_argument("--judges", type=int, default=2,
                        help="in-process judge threads; 0 makes SUBMIT answer "
                             "503 JUDGE_UNAVAILABLE (default: 2)")
    parser.add_argument("--backend", choices=("subprocess", "docker"), default="subprocess",
                        help="how a submission is isolated (default: subprocess)")
    parser.add_argument("--allow-insecure-remote", action="store_true",
                        help="allow a non-loopback bind for a controlled demo only; requires "
                             "the Docker backend. CDAP does not provide TLS")
    parser.add_argument("--worker-token", default="",
                        help="pre-shared token required by remote WORKER_* clients")
    parser.add_argument("--worker-heartbeat-ms", type=int,
                        default=DEFAULT_WORKER_HEARTBEAT_MS,
                        help="remote-worker heartbeat interval; three missed beats eject a "
                             f"worker (default: {DEFAULT_WORKER_HEARTBEAT_MS})")
    parser.add_argument("--min-players", type=int, default=2,
                        help="players needed to form a match; 1 allows solo testing "
                             "(default: 2)")
    parser.add_argument("--match-seconds", type=float, default=300.0,
                        help="seconds on a match clock (default: 300)")
    parser.add_argument("--countdown", type=float, default=3.0,
                        help="seconds between MATCH_FOUND and MATCH_START (default: 3)")
    parser.add_argument("--submit-cooldown", type=float, default=3.0,
                        help="minimum seconds between two SUBMITs; triggers "
                             "429 SUBMIT_COOLDOWN (default: 3)")
    parser.add_argument("--room-cooldown", type=float, default=5.0,
                        help="minimum seconds between two CREATE_ROOMs (default: 5)")
    parser.add_argument("--problem", choices=problem_ids(), default=None,
                        help="pin every match to one problem instead of round-robin")
    parser.add_argument("--room-capacity", type=int, default=2,
                        help="players per private room (default: 2)")
    parser.add_argument("--idle-timeout", type=float, default=600.0,
                        help="seconds a connection may sit silent before 408 "
                             "REQUEST_TIMEOUT (default: 600)")
    parser.add_argument("--max-sessions", type=int, default=DEFAULT_MAX_SESSIONS,
                        help=f"maximum concurrent TCP sessions (default: {DEFAULT_MAX_SESSIONS})")
    parser.add_argument("--max-users", type=int, default=DEFAULT_MAX_USERS,
                        help=f"maximum registered accounts per process (default: {DEFAULT_MAX_USERS})")
    parser.add_argument("--max-submissions", type=int, default=DEFAULT_MAX_SUBMISSIONS,
                        help=f"maximum retained submissions (default: {DEFAULT_MAX_SUBMISSIONS})")
    parser.add_argument("--max-pending-jobs", type=int, default=DEFAULT_MAX_PENDING_JOBS,
                        help=f"maximum queued judge jobs before 503 JUDGE_QUEUE_FULL "
                             f"(default: {DEFAULT_MAX_PENDING_JOBS})")
    parser.add_argument("--max-ended-matches", type=int, default=DEFAULT_MAX_ENDED_MATCHES,
                        help=f"maximum retained completed matches (default: {DEFAULT_MAX_ENDED_MATCHES})")
    parser.add_argument("--history-ttl", type=float, default=DEFAULT_HISTORY_TTL_S,
                        help=f"seconds to retain completed history (default: {DEFAULT_HISTORY_TTL_S:.0f})")
    parser.add_argument("--max-feed-endpoints", type=int, default=DEFAULT_MAX_FEED_ENDPOINTS,
                        help=f"UDP endpoints allowed per session (default: {DEFAULT_MAX_FEED_ENDPOINTS})")
    parser.add_argument("--allow-panic", action="store_true",
                        help="enable DEBUG_PANIC, which raises on purpose so "
                             "500 INTERNAL_ERROR can be demonstrated")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="add full headers and body previews to the wire log. The "
                             "baseline log is NOT gated behind this — printing every "
                             "message with its status code and phrase is a requirement, "
                             "so -v only adds detail")
    return parser


def _is_loopback_host(host: str) -> bool:
    """Recognise only unambiguous local binds; unknown DNS names are remote by policy."""
    if host.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def main(argv: Optional[List[str]] = None) -> int:
    # The wire log is a graded deliverable, so this happens before anything is printed:
    # the markers → ← ✗ need a UTF-8 console, and on Windows the default code page is not
    # one. enable_utf8_output falls back to ASCII markers if it cannot reconfigure.
    #
    # "Before anything is printed" includes argparse: ``--help`` and a bad flag both print
    # and exit *inside* parse_args, so this call has to come first to cover them too.
    capabilities.enable_utf8_output()

    args = build_parser().parse_args(argv)
    for name in ("max_sessions", "max_users", "max_submissions", "max_pending_jobs", "max_ended_matches",
                 "history_ttl", "max_feed_endpoints"):
        if getattr(args, name) <= 0:
            build_parser().error(f"--{name.replace('_', '-')} must be positive")
    log = WireLog(stream=sys.stdout, verbose=args.verbose, prefix="")
    remote = not _is_loopback_host(args.host)
    if remote and not args.allow_insecure_remote:
        log.note("refusing non-loopback bind without --allow-insecure-remote; CDAP has no TLS")
        return 2
    if remote:
        if args.backend != "docker":
            log.note("remote demo requires --backend docker; subprocess is not a containment boundary")
            return 2
        docker_ok, docker_reason = DockerBackend().available()
        if not docker_ok:
            log.note(f"remote demo requires Docker: {docker_reason}")
            return 2
        log.note("WARNING: remote CDAP is plaintext and intended only for a controlled demo")

    arena = Arena(
        log,
        min_players=max(1, args.min_players),
        match_seconds=args.match_seconds,
        countdown=args.countdown,
        submit_cooldown=args.submit_cooldown,
        room_cooldown=args.room_cooldown,
        problem_id=args.problem,
        room_capacity=max(1, args.room_capacity),
        allow_panic=args.allow_panic,
        worker_token=args.worker_token,
        worker_heartbeat_ms=args.worker_heartbeat_ms,
        limits=ArenaLimits(
            max_users=args.max_users,
            max_submissions=args.max_submissions,
            max_pending_jobs=args.max_pending_jobs,
            max_ended_matches=args.max_ended_matches,
            history_ttl_s=args.history_ttl,
        ),
    )

    pool = LocalJudgePool(arena, size=max(0, args.judges), backend_name=args.backend)
    arena.pool = pool
    pool.start()

    # Report what the judge *is*, not what was asked for. If --backend docker was requested
    # and no daemon answered, make_backend hands back a SubprocessBackend, and saying
    # "docker" here would be the first step towards a result claiming an isolation it never
    # had (design invariant 6).
    actual = make_backend(args.backend).name
    if actual != args.backend:
        log.note(f"backend {args.backend!r} is unavailable — falling back to {actual!r}; "
                 f"every verdict will report backend={actual}")
    log.note(f"judges: {args.judges} thread(s), backend={actual}, "
             f"opcode counter={arena.capabilities.opcode_counter_name}")
    if args.judges == 0:
        log.note("no local judge threads — SUBMIT answers 503 JUDGE_UNAVAILABLE until a "
                 "remote worker registers")
    log.note(f"remote workers: token={'configured' if args.worker_token else 'empty'} "
             f"heartbeat={arena.worker_heartbeat_ms}ms "
             f"eject-after={arena.worker_lease_ms}ms")
    if args.allow_panic:
        log.note("DEBUG_PANIC is enabled — 500 INTERNAL_ERROR is reachable on request")

    server = ArenaServer(
        arena, log,
        host=args.host,
        tcp_port=args.tcp_port,
        udp_port=args.udp_port,
        idle_timeout=args.idle_timeout,
        max_sessions=args.max_sessions,
        max_feed_endpoints=args.max_feed_endpoints,
    )
    try:
        server.serve_forever()
    except OSError as exc:
        log.note(f"could not start the arena: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
