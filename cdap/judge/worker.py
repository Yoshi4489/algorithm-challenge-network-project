"""CDAP judge worker — a separate process that pulls submissions from the arena and judges them.

Phase 5 already judges submissions, in threads inside the arena process (``LocalJudgePool``).
So the first question to answer is why this file exists at all.

Why the judge should not live in the arena
------------------------------------------
The arena owns irreplaceable state: accounts, live matches, the clock. The judge *executes
code written by a stranger*. Putting those two things in one process means every weakness of
the judge is a weakness of the arena — a submission that wedges the interpreter, exhausts
memory, or escapes the AST guard takes the match state with it. Moving the judge into its own
process, reachable only through a protocol, means the worst a bad submission can do is cost
one worker.

It also buys the ordinary thing a worker pool buys: two workers judge two submissions at once,
and the arena does not care how many there are.

Why the worker is a *client*
----------------------------
The worker dials the arena, not the other way round, and work flows by **pull** rather than
push. That is a deliberate choice worth a sentence on video: a worker can then sit behind NAT,
on a laptop, on a second machine on the same Wi-Fi, and the arena needs no route back to it.
The arena never learns a worker's address and never has to.

So this file is a CDAP client. It speaks the same framing, over the same TCP, with the same
``Seq`` correlation as ``cdap/client.py`` — one protocol, two audiences, which is the economy
the design gets to claim.

Three threads, and why a worker needs the same machinery a player does
---------------------------------------------------------------------
* **main** — the work loop: pull a job, judge it, report the result.
* **reader** — the only thread that calls ``recv``. It routes each reply to whichever thread
  is waiting on that ``Seq``.
* **heartbeat** — while a job is in flight, it tells the arena the worker is still alive.

The reader thread is not ceremony, and the reason is the most interesting thing in this file.
The arena has to distinguish **"this worker is forty seconds into a legitimate judging run"**
from **"this worker died holding my submission"**, and it cannot tell those apart by watching
the socket: both look like silence. Only the worker knows, so the worker has to say so — which
means sending a request while the main thread is blocked inside a subprocess.

That is two senders on one socket. ``protocol.Connection``'s send lock keeps their bytes from
interleaving, but it does nothing about the *replies*: two threads both calling ``recv`` would
each steal frames meant for the other. So exactly one thread reads, and it hands each reply to
the sender waiting on that ``Seq``.

Which is precisely the structure ``cdap/client.py`` needs, for a different reason (it receives
unsolicited events as well as replies). Worth saying out loud in the report: **``Seq``
correlation is a property of the protocol, not of the player client.** Any peer that sends
requests from more than one thread needs it, and CDAP's rule — responses echo the request's
``Seq``, events carry none — is what makes it a lookup rather than a guess.

Leases: how the arena reclaims a job from a worker that died
------------------------------------------------------------
``WORKER_PULL`` hands out a job together with a **lease**: the latest moment a verdict for it
can still be accepted. The arena can compute one honestly because the judging run's wall-clock
backstop is itself derived from the problem's contract (see ``runner.run_budget_ms``), so it
knows how long the run could legitimately take. Each ``WORKER_HEARTBEAT`` naming that
submission renews the lease. Miss three in a row and the arena ejects the worker and requeues
the job.

**Heartbeats are sent only while a job is in flight.** An idle worker is already sitting inside
a long-poll, and a long-poll is itself proof of life — a worker that died stops re-polling. So
heartbeats appear in the log exactly where they carry information, during a run, instead of
doubling the noise of an idle arena.

At-least-once dispatch, at-most-once verdict
--------------------------------------------
Requeuing means a submission can be dispatched twice: once to a worker that died and once to a
live one. That is the right way round (losing a submission is far worse than judging one
twice), but it puts the burden of deduplication on the *verdict*, and the arena carries it: the
first ``WORKER_RESULT`` for a submission wins, and a later one is answered ``409 CONFLICT``.
So a worker that was declared dead and then woke up cannot overwrite a verdict the player has
already read.

The stated weakness
-------------------
The worker channel is authenticated by a pre-shared token (``--token``, matched against the
arena's ``--worker-token``). That is weak, and saying so is better than implying otherwise: a
command-line token is visible in ``ps``, it is the same secret for every worker, and there is
no transport encryption, so anyone who can read the traffic can replay it. It gets the same
treatment ``docs/threat-model.md`` gives the AST guard — named, not glossed. But it is not
nothing, and the alternative is worse: an unauthenticated ``WORKER_PULL`` hands any stranger
who can reach the port the source code of every submission in the arena.

Backend truthfulness
--------------------
The result reports ``RunResult.backend`` — the backend that *actually ran* — and never the one
named on the command line. Asking for ``--backend docker`` on a machine with no daemon gets a
subprocess run that says ``subprocess``. That is CLAUDE.md design invariant 6, and the security
experiment's conclusions rest on the field being true.
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import threading
import time
from typing import Dict, List, Optional, Tuple

from .. import capabilities
from ..problems import get_problem
from ..protocol import (
    PROTOCOL_VERSION,
    Connection,
    FrameTooLarge,
    Kind,
    Message,
    ProtocolError,
    WireLog,
)
from ..status import Status, Verdict, describe_status, is_success
from .backends import make_backend
from .profiler import judge_record
from .runner import run_budget_ms

#: How long the arena is asked to hold a ``WORKER_PULL`` open before answering ``204``.
#:
#: This is the long-poll window, and the number is a compromise between two costs. Short waits
#: mean a request every few seconds forever, most of them answered "nothing yet" — noise in a
#: log that is a graded deliverable. Long waits mean a worker takes longer to notice the arena
#: has gone away. Twenty seconds keeps an idle log readable (three lines a minute) while
#: staying well inside the arena's idle timeout.
DEFAULT_POLL_WAIT_MS = 20_000

#: Added to the poll wait to get the reply deadline. The arena promises to answer a long-poll
#: within ``Wait-Ms``; this is the margin for it being busy. Blowing even this means the arena
#: is not answering, which is a dead connection rather than a slow one.
POLL_MARGIN_S = 10.0

#: How long to wait for a reply to anything that is *not* a long-poll. The arena answers these
#: immediately, so a delay this long means something is wrong.
REQUEST_TIMEOUT_S = 30.0

#: Default heartbeat interval while a job is in flight. The arena's advertised value wins.
#: Ejection is after three missed beats, so this also sets how fast a dead worker is noticed:
#: about fifteen seconds, which is short enough to watch happen on camera.
DEFAULT_HEARTBEAT_MS = 5_000

#: Reconnect backoff. A worker started before the arena — the normal order in a demo, and the
#: order the README's commands are written in — must wait and retry rather than exit. Doubling
#: with a ceiling, so a long outage does not become a busy loop.
RECONNECT_DELAY_S = 1.0
MAX_RECONNECT_DELAY_S = 15.0


class WorkerError(Exception):
    """The arena said something this worker cannot act on. Ends the connection, not the worker."""


class _Pending:
    """One in-flight request, waiting for the reader thread to hand back its reply.

    ``__slots__`` because there is one of these per request and the class is pure plumbing.
    ``failure`` carries the reason the connection died, so a blocked caller learns *why* it is
    not getting an answer instead of sitting out its whole timeout.
    """

    __slots__ = ("done", "response", "failure")

    def __init__(self):
        self.done = threading.Event()
        self.response: Optional[Message] = None
        self.failure = ""


# --------------------------------------------------------------------------
# The worker
# --------------------------------------------------------------------------

class JudgeWorker:
    """One judging process: register, then pull-judge-report until stopped."""

    def __init__(self, host: str, port: int, worker_id: str, log: WireLog, *,
                 backend_name: str = "subprocess", token: str = "",
                 poll_wait_ms: int = DEFAULT_POLL_WAIT_MS,
                 guard: bool = True, profile: bool = True):
        self.host = host
        self.port = port
        self.worker_id = worker_id
        self.log = log
        self.backend_name = backend_name
        self.token = token
        self.poll_wait_ms = poll_wait_ms

        #: Whether the AST guard runs in the child, and whether complexity is measured at all.
        #: Both are the *worker's* policy, not the job's: the queue says what to run, a worker
        #: says how it runs things. ``--no-ast-guard`` is the security experiment's flag and
        #: must keep working (CLAUDE.md "do not fix" #1).
        self.guard = guard
        self.profile = profile

        self.sock: Optional[socket.socket] = None
        self.conn: Optional[Connection] = None

        # Built once and reused. SubprocessBackend holds no mutable state between runs — every
        # run gets a fresh child process and a fresh temp directory — so reuse costs nothing.
        self.backend = make_backend(backend_name)

        # Probed once, here, and never again. ``capabilities.probe()`` executes real workloads
        # under both opcode-counting mechanisms to check they count, are deterministic, and
        # scale; Phase 1 measured counting at roughly 31x overhead. Paying that per submission
        # would dominate the measurement it exists to protect, so the worker probes at startup
        # and passes the winning mechanism's *name* down to each child.
        self.capabilities = capabilities.probe()

        self._seq = 0
        self._seq_lock = threading.Lock()
        self._pending: Dict[int, _Pending] = {}
        self._pending_lock = threading.Lock()

        #: The submission currently being judged, or None. Read by the heartbeat thread and
        #: written by the main thread, so it is guarded — a torn read here would renew the
        #: wrong lease.
        self._current: Optional[str] = None
        self._current_stage = ""
        self._current_lock = threading.Lock()

        self._stop = threading.Event()
        self._threads: List[threading.Thread] = []

        self.jobs_done = 0
        #: What the arena advertised when this worker registered.
        self.arena_name = ""
        self.heartbeat_ms = DEFAULT_HEARTBEAT_MS
        self.lease_ms = 0

    # -- connection --------------------------------------------------------

    def connect(self) -> None:
        """Open the connection and start its reader thread.

        The heartbeat thread starts only after ``register`` receives the arena's interval.
        Starting it here would let its first sleep use the local 5-second default even when
        the arena advertises a shorter interval, long enough for a short demo lease to expire.
        """
        self.sock = socket.create_connection((self.host, self.port), timeout=10.0)
        # The connect timeout guarded a dead arena. Now clear it: the reader thread blocks in
        # recv indefinitely by design, and per-request deadlines are enforced by waiting on a
        # _Pending event instead. A socket timeout firing mid-frame would leave the buffered
        # reader at an unknown offset, which is a far worse failure than a slow reply.
        self.sock.settimeout(None)
        try:
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            # Not supported everywhere, not fatal anywhere. Nagle would only add latency.
            pass

        self.conn = Connection(self.sock, log=self.log, peer=f"{self.host}:{self.port}")
        self._stop.clear()
        self._threads = []
        reader = threading.Thread(target=self._reader_loop, name=f"{self.worker_id}-reader",
                                  daemon=True)
        reader.start()
        self._threads.append(reader)

        self.log.note(f"connected to arena {self.host}:{self.port} as worker {self.worker_id}")

    def close(self) -> None:
        """Stop the threads and drop the connection. Safe to call more than once.

        The threads are **joined**, not merely signalled, and the reason is a bug this had
        before the join was added. ``run()`` reconnects by calling ``connect()`` again, and
        ``connect()`` clears the stop flag before starting the new pair of threads — so a
        previous heartbeat thread that had not yet noticed the flag would find it clear again
        and carry on, now sending heartbeats down the *new* connection. Two heartbeat threads
        after one reconnect, four after two. Joining first makes the generations disjoint.

        Closing the connection is what unblocks the reader: it is sitting in ``recv``, which
        no flag can interrupt, and shutting the socket down under it is the only way to end
        that wait.
        """
        self._stop.set()
        if self.conn is not None:
            self.conn.close()
        self.conn = None
        self.sock = None
        self._fail_pending("the connection was closed")
        with self._current_lock:
            self._current = None
            self._current_stage = ""

        for thread in self._threads:
            # A thread never joins itself. Nothing here calls close() from the reader or
            # heartbeat thread today, but a deadlock is a bad way to find out that changed.
            if thread is not threading.current_thread():
                thread.join(timeout=2.0)
        self._threads = []

    # -- requests ----------------------------------------------------------

    def _next_seq(self) -> int:
        with self._seq_lock:
            self._seq += 1
            return self._seq

    def request(self, method: str, headers: Optional[dict] = None, body: bytes = b"",
                timeout: float = REQUEST_TIMEOUT_S) -> Message:
        """Send one request and wait for the reply the reader thread routes back.

        The ``_Pending`` is registered **before** the send, not after. The arena can answer in
        well under a millisecond, so the reader thread can have the reply in hand before this
        thread returns from ``sendall`` — and a pending registered afterwards would be
        registered too late, leaving the reply unroutable and this caller waiting out its whole
        timeout for something that already arrived.

        ``conn`` is read into a local once and used from there. Two threads call this method
        and a third path — ``close()`` — sets ``self.conn`` to None, so testing the attribute
        and then using it would be a race: the heartbeat thread could pass the check and then
        find None, raising ``AttributeError`` out of a thread that has no handler for it. A
        local binding turns a shutdown into the clean ``WorkerError`` the callers already
        expect.
        """
        conn = self.conn
        if conn is None:
            raise WorkerError("not connected")

        fields = dict(headers or {})
        fields["Worker"] = self.worker_id
        if self.token:
            fields["Worker-Token"] = self.token

        seq = self._next_seq()
        message = Message.request(method, headers=fields, body=body, seq=seq)
        if body:
            # The same integrity check a player's SUBMIT uses, in the other direction. A
            # verdict is the one thing in this protocol a player cannot re-derive, so it is
            # worth knowing the bytes arrived as sent.
            message.attach_body_hash()

        pending = _Pending()
        with self._pending_lock:
            self._pending[seq] = pending

        try:
            conn.send(message)
        except OSError as exc:
            with self._pending_lock:
                self._pending.pop(seq, None)
            raise WorkerError(f"sending {method} failed: {exc}") from exc

        if not pending.done.wait(timeout):
            with self._pending_lock:
                self._pending.pop(seq, None)
            raise WorkerError(f"the arena did not answer {method} within {timeout:.0f}s")

        if pending.failure:
            raise WorkerError(pending.failure)
        if pending.response is None:
            raise WorkerError(f"{method} completed with no reply")
        return pending.response

    def _reader_loop(self) -> None:
        """The only thread that reads. Routes each reply to the sender waiting on its Seq."""
        conn = self.conn        # bound once, for the same reason request() binds it
        if conn is None:
            return
        try:
            while not self._stop.is_set():
                message = conn.recv()
                if message is None:
                    self._fail_pending("the arena closed the connection")
                    return
                self._route(message)
        except (OSError, ProtocolError, FrameTooLarge) as exc:
            self._fail_pending(f"the connection failed: {exc!r}")
        except Exception as exc:                          # noqa: BLE001
            # Nothing else may reach here, but a reader thread that dies silently would leave
            # every future request timing out with no explanation — so it reports and then
            # ends, which is what wakes the blocked senders.
            self._fail_pending(f"the reader thread failed: {exc!r}")

    def _route(self, message: Message) -> None:
        """Hand one received frame to whoever it belongs to.

        Three cases, decided entirely by the frame itself — which is the whole payoff of the
        rule that responses echo a ``Seq`` and events carry none.
        """
        if message.kind is Kind.EVENT:
            # The arena has no reason to push events to a worker session; a worker is not
            # playing a match. Reported rather than dropped, because a worker receiving
            # MATCH_START would mean the arena had confused it for a player.
            self.log.note(f"ignoring an unexpected {message.event} event — "
                          "a worker session receives no events")
            return

        if message.kind is Kind.REQUEST:
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

        # A response with no Seq, or one nobody is waiting for. The arena sends these when it
        # rejects something before it could read the request — an idle timeout, an oversized
        # frame — so it is information, not noise.
        self.log.note(
            f"unsolicited {describe_status(message.status, message.phrase)} "
            f"(Seq={seq if seq is not None else 'none'}) — "
            f"{message.headers.get('Detail', 'no detail given')}"
        )

    def _fail_pending(self, reason: str) -> None:
        """Wake every blocked caller with a reason, instead of letting them all time out."""
        with self._pending_lock:
            waiting = list(self._pending.values())
            self._pending.clear()
        for pending in waiting:
            pending.failure = reason
            pending.done.set()

    # -- the heartbeat -----------------------------------------------------

    def _heartbeat_loop(self) -> None:
        """Renew the lease on the job in flight, for as long as there is one.

        Nothing is sent while the worker is idle: a long-poll already proves it is alive. The
        interval is re-read each time round the loop because the arena hands it out at
        registration, which happens after this thread has already started.
        """
        while not self._stop.is_set():
            interval_s = max(0.5, self.heartbeat_ms / 1000.0)
            if self._stop.wait(interval_s):
                return

            with self._current_lock:
                submission_id = self._current
                stage = self._current_stage
            if submission_id is None:
                continue

            headers = {"Submission": submission_id}
            if stage:
                headers["Stage"] = stage
            try:
                reply = self.request("WORKER_HEARTBEAT", headers=headers, timeout=10.0)
            except (WorkerError, OSError) as exc:
                # Logged and dropped. The main thread will discover the same failure when it
                # reports the result, and it is the one that can do something about it. A
                # heartbeat thread that raised would take the worker down mid-judgement.
                #
                # Silent during shutdown: close() sets the flag and then drops the connection,
                # so a heartbeat already in flight fails as a matter of course. Reporting that
                # as a problem would put an alarming last line in the log of a clean exit.
                if not self._stop.is_set():
                    self.log.note(f"heartbeat for {submission_id} failed: {exc}")
                continue

            if reply.status == int(Status.CONFLICT):
                # The lease is gone: the arena decided this worker was dead and gave the job to
                # somebody else. Worth saying now, so the 409 that WORKER_RESULT is about to
                # get is already explained in the log above it.
                self.log.note(
                    f"{submission_id}: the arena has reclaimed this job "
                    f"({reply.headers.get('Detail', 'lease expired')}) — "
                    f"this run's result will be discarded"
                )
            elif not is_success(reply.status):
                self.log.note(
                    f"heartbeat for {submission_id} was refused: "
                    f"{describe_status(reply.status, reply.phrase)}"
                )

    def _set_current(self, submission_id: Optional[str], stage: str = "") -> None:
        with self._current_lock:
            self._current = submission_id
            self._current_stage = stage

    # -- the four worker methods -------------------------------------------

    def register(self) -> None:
        """``WORKER_REGISTER`` — join the pool and adopt the numbers the arena hands back.

        The body says what this worker can do; the reply says how the arena wants it to behave.
        Taking the poll window, the heartbeat interval and the lease *from the arena* rather
        than hard-coding them on both sides means the two cannot drift apart — and it is the
        arena that has the information to set them, because it knows the contract.
        """
        available, reason = self.backend.available()
        body = json.dumps({
            "worker": self.worker_id,
            "backend": self.backend.name,
            "backend_requested": self.backend_name,
            "backend_available": available,
            "backend_note": reason,
            "capacity": 1,
            "guard": self.guard,
            "profile": self.profile,
            "opcode_counter": self.capabilities.opcode_counter_name,
            "poll_wait_ms": self.poll_wait_ms,
        }, indent=2).encode("utf-8")

        reply = self.request("WORKER_REGISTER", body=body)
        if not is_success(reply.status):
            raise WorkerError(
                f"the arena refused this worker: "
                f"{describe_status(reply.status, reply.phrase)} — "
                f"{reply.headers.get('Detail', 'no detail given')}"
            )

        self.arena_name = reply.headers.get("Server", "unknown arena")
        # The arena's numbers win. This worker's --poll-wait-ms is a request, not a decision.
        advertised_wait = reply.headers.get_int("Poll-Timeout-Ms")
        if advertised_wait:
            self.poll_wait_ms = advertised_wait
        advertised_beat = reply.headers.get_int("Heartbeat-Ms")
        if advertised_beat:
            self.heartbeat_ms = advertised_beat
        self.lease_ms = reply.headers.get_int("Lease-Ms") or 0

        self.log.note(
            f"registered with {self.arena_name} — backend={self.backend.name} "
            f"poll={self.poll_wait_ms}ms heartbeat={self.heartbeat_ms}ms "
            f"lease={self.lease_ms or 'per-job'}"
        )
        if self.backend.name != self.backend_name:
            # Invariant 6, said at startup as well as in every result. An operator who asked
            # for docker and got subprocess should not have to read a verdict to find out.
            self.log.note(
                f"backend {self.backend_name!r} was requested but {self.backend.name!r} "
                f"will run: {reason or 'not available on this machine'}"
            )

        heartbeat = threading.Thread(
            target=self._heartbeat_loop,
            name=f"{self.worker_id}-heartbeat",
            daemon=True,
        )
        heartbeat.start()
        self._threads.append(heartbeat)

    def pull(self) -> Optional[dict]:
        """``WORKER_PULL`` — long-poll for one job. Returns the job, or None if there is none.

        ``204 NO_CONTENT`` is the empty answer, and the choice of code is worth defending. Not
        ``404``: the queue exists and the worker addressed it correctly, there is simply
        nothing in it. Not ``408``: nobody failed to respond in time — the arena waited exactly
        as long as it was asked to and then answered. **An empty long-poll is a success**, and
        the status code says so, which is why an idle worker's log reads as ``204 NO_CONTENT``
        over and over instead of as a stream of errors.
        """
        wait_ms = max(0, int(self.poll_wait_ms))
        reply = self.request(
            "WORKER_PULL",
            headers={"Wait-Ms": wait_ms},
            timeout=wait_ms / 1000.0 + POLL_MARGIN_S,
        )

        if reply.status == int(Status.NO_CONTENT):
            return None
        if not is_success(reply.status):
            raise WorkerError(
                f"WORKER_PULL was refused: "
                f"{describe_status(reply.status, reply.phrase)} — "
                f"{reply.headers.get('Detail', 'no detail given')}"
            )

        if reply.body_hash_ok() is False:
            # The source is the one field where a silently corrupted byte would produce a
            # confident, wrong verdict: a compile error for code the player wrote correctly.
            # Refuse the job rather than judge a corrupted copy of it.
            raise WorkerError("the job body does not match its Body-SHA256")

        try:
            job = json.loads(reply.text())
        except ValueError as exc:
            raise WorkerError(f"job body is not valid JSON: {exc}") from exc
        if not isinstance(job, dict):
            raise WorkerError(f"job body must be a JSON object, not {type(job).__name__}")

        submission_id = reply.headers.get("Submission", "")
        if not submission_id:
            raise WorkerError("the arena sent a job with no Submission header")

        # Everything the run needs, gathered so judge() takes a single argument.
        job["submission"] = submission_id
        job["time_limit_ms"] = reply.headers.get_int("Time-Limit-Ms") or 0
        job["lease_ms"] = reply.headers.get_int("Lease-Ms") or self.lease_ms
        return job

    def heartbeat_now(self, submission_id: str, stage: str) -> None:
        """Send one heartbeat immediately, to report a stage rather than to wait for the timer.

        Used once per job, for ``COMPILING``. That is the last stage this process can honestly
        claim to have witnessed: the child is about to parse the source, and everything after
        happens where the worker cannot watch. ``TESTING`` and ``PROFILING`` are inside the
        child, so the worker does not claim them — see ``server.JUDGE_STAGES``. Inventing
        progress the judge never saw would make the display a lie, which costs more than
        looking busy is worth.
        """
        try:
            reply = self.request("WORKER_HEARTBEAT",
                                 headers={"Submission": submission_id, "Stage": stage},
                                 timeout=10.0)
        except (WorkerError, OSError) as exc:
            self.log.note(f"stage update for {submission_id} failed: {exc}")
            return
        if not is_success(reply.status):
            self.log.note(f"stage update for {submission_id} was refused: "
                          f"{describe_status(reply.status, reply.phrase)}")

    def send_result(self, submission_id: str, verdict: dict, backend: str,
                    wall_ms: float) -> bool:
        """``WORKER_RESULT`` — hand a finished verdict back. True if the arena took it.

        ``409 CONFLICT`` here is expected rather than exceptional: the lease expired, the arena
        gave the job to another worker, and that worker got there first. The right response is
        to shrug and pull the next job — the submission has a verdict, which is all the player
        needs. Retrying would be the wrong instinct, because the arena is not failing; it is
        saying this work is no longer wanted.
        """
        body = json.dumps(verdict, indent=2).encode("utf-8")
        reply = self.request("WORKER_RESULT", headers={
            "Submission": submission_id,
            "Backend": backend,
            "Wall-Ms": round(wall_ms, 1),
            "Verdict": _verdict_header(verdict),
        }, body=body)

        if is_success(reply.status):
            return True

        detail = reply.headers.get("Detail", "no detail given")
        if reply.status == int(Status.CONFLICT):
            self.log.note(f"{submission_id}: the arena already has a verdict from another "
                          f"worker ({detail}) — this run is discarded")
            return False

        raise WorkerError(
            f"WORKER_RESULT for {submission_id} was refused: "
            f"{describe_status(reply.status, reply.phrase)} — {detail}"
        )

    # -- judging -----------------------------------------------------------

    def judge(self, job: dict) -> Tuple[dict, str, float]:
        """Run one job and return ``(verdict, backend_that_ran, wall_ms)``. **Never raises.**

        The no-raise rule is the one ``LocalJudgePool._judge_loop`` follows, for the same
        reason: a judge that dies leaves a submission with no verdict *and* no error, and a
        player watching a spinner forever is the worst failure this system has. So an
        unexpected exception becomes ``612 JUDGE_ERROR`` for that one submission — an honest
        answer, because a crash in the judge really is the judge's fault, and 612 says exactly
        that — and the worker carries on.
        """
        submission_id = job.get("submission", "?")
        problem_id = job.get("problem", "")

        try:
            problem = get_problem(problem_id)
        except (KeyError, ValueError) as exc:
            # A job naming a problem this worker does not have means the arena and the worker
            # are running different versions of the code. Named precisely, because the
            # alternative is a mystifying verdict.
            return _judge_error(
                f"this worker has no problem {problem_id!r} — the arena and the worker are "
                f"running different problem sets ({exc!r})"
            ), self.backend.name, 0.0

        contract = problem.contract

        # Two facts travel with a job and they belong to different owners. Worth separating
        # explicitly, because the instinct is to let one side win both.
        #
        # The *contract* is the arena's business: a protocol fact, the same for every worker,
        # and the player was told it. So the arena's time limit wins.
        limit_ms = job.get("time_limit_ms") or contract.time_limit_ms
        if job.get("time_limit_ms") and job["time_limit_ms"] != contract.time_limit_ms:
            self.log.note(
                f"{submission_id}: the arena says the time limit for {problem_id} is "
                f"{job['time_limit_ms']}ms, this worker's copy says {contract.time_limit_ms}ms "
                f"— using the arena's"
            )

        # The *opcode-counting mechanism* is this worker's business, because this machine runs
        # the child. The arena's probe measured the arena's interpreter; a job naming a
        # mechanism this Python cannot count with would produce a silently empty Method B.
        counter = self.capabilities.opcode_counter_name
        arena_counter = job.get("opcode_counter")
        if arena_counter and arena_counter != counter:
            self.log.note(
                f"{submission_id}: the arena counts opcodes with {arena_counter!r}, this "
                f"worker's interpreter uses {counter!r} — using the local mechanism, because "
                f"the child runs here"
            )

        payload = {
            "problem": problem_id,
            "source": job.get("source", ""),
            "guard": self.guard,
            "profile": self.profile,
            "opcode_counter": counter,
        }

        try:
            run = self.backend.run(payload, time_limit_ms=run_budget_ms(limit_ms))
        except Exception as exc:                          # noqa: BLE001 - see the docstring
            self.log.note(f"backend failed on {submission_id}: {exc!r}")
            return _judge_error(f"the judge backend failed: {exc!r}"), self.backend.name, 0.0

        try:
            verdict = judge_record(
                run.result or {},
                contract.to_json(),
                outcome_hint=run.outcome_hint(),
            )
        except Exception as exc:                          # noqa: BLE001 - see the docstring
            self.log.note(f"profiler failed on {submission_id}: {exc!r}")
            return _judge_error(f"the profiler failed: {exc!r}"), run.backend, run.wall_ms

        # run.backend, never self.backend_name. Invariant 6.
        return verdict, run.backend, run.wall_ms

    # -- the loop ----------------------------------------------------------

    def run_once(self) -> bool:
        """One pull-judge-report cycle. True if a job was judged, False if the poll was empty."""
        job = self.pull()
        if job is None:
            return False

        submission_id = job["submission"]
        lease_ms = job.get("lease_ms") or 0
        took = f"took {submission_id} (problem={job.get('problem', '?')}"
        if lease_ms:
            took += f", lease={lease_ms}ms"
        self.log.note(took + ")")

        # From here the heartbeat thread has a lease to renew. Set before any work starts, so
        # a run that is slow from its first second is still covered.
        self._set_current(submission_id, "COMPILING")
        self.heartbeat_now(submission_id, "COMPILING")

        started = time.monotonic()
        try:
            verdict, backend, wall_ms = self.judge(job)
        finally:
            # Whatever happened, stop claiming a lease on it. In a finally block because a
            # heartbeat thread still renewing a job nobody is judging would keep the arena
            # from ever requeuing it.
            self._set_current(None)
        elapsed_ms = (time.monotonic() - started) * 1000.0

        # ``_verdict_header`` rather than ``WireLog.status``: the code came out of the profiler
        # through a dict, and the strict formatter would turn a surprising verdict code into a
        # crash *here* — after the work was done and before it was reported, which is the one
        # place a crash costs a player their submission.
        self.log.note(f"{_verdict_header(verdict)} — {submission_id} judged in "
                      f"{elapsed_ms:.0f}ms on backend={backend}")

        if self.send_result(submission_id, verdict, backend, wall_ms):
            self.jobs_done += 1
        return True

    def run(self, *, once: bool = False, max_jobs: int = 0) -> int:
        """Connect, register, and judge until stopped. Returns a process exit code.

        The reconnect loop is not defensive padding — it is what makes the README's start order
        work. A worker is normally launched in its own terminal, often *before* the arena is up,
        and one that exited on a refused connection would have to be started again by hand at
        the right moment. Instead it waits, retrying with a doubling delay, and says so.
        """
        delay = RECONNECT_DELAY_S
        while True:
            try:
                self.connect()
            except OSError as exc:
                self.log.note(f"cannot reach the arena at {self.host}:{self.port} ({exc}); "
                              f"retrying in {delay:.0f}s")
                if not _sleep(delay):
                    return 0
                delay = min(MAX_RECONNECT_DELAY_S, delay * 2)
                continue

            try:
                self.register()
                # Only now does the backoff start over. Reaching a registered state proves both
                # the address and the token are right, so a later drop is worth retrying
                # quickly. Resetting on a bare TCP connect would turn a repeated registration
                # failure into a once-per-second loop.
                delay = RECONNECT_DELAY_S
                while True:
                    judged = self.run_once()
                    if judged and once:
                        self.log.note("--once: one job judged, stopping")
                        return 0
                    if judged and max_jobs and self.jobs_done >= max_jobs:
                        self.log.note(f"--max-jobs {max_jobs} reached, stopping")
                        return 0
            except KeyboardInterrupt:
                self.log.note("interrupted, disconnecting")
                return 0
            except WorkerError as exc:
                if self._fatal(exc):
                    # Reconnecting would produce the same refusal forever, so this one ends the
                    # process — with the arena's own words, which are what the operator needs.
                    self.log.note(f"giving up: {exc}")
                    return 1
                self.log.note(f"lost the arena ({exc}); reconnecting in {delay:.0f}s")
            except (OSError, ProtocolError, FrameTooLarge) as exc:
                self.log.note(f"connection failed ({exc!r}); reconnecting in {delay:.0f}s")
            finally:
                self.close()

            if not _sleep(delay):
                return 0
            delay = min(MAX_RECONNECT_DELAY_S, delay * 2)

    @staticmethod
    def _fatal(exc: WorkerError) -> bool:
        """Whether an arena refusal is worth retrying.

        A refusal naming an authentication, version or vocabulary problem will be refused
        identically on every reconnection, so retrying forever produces an endless log and no
        work. A connection that merely dropped is worth retrying. The test reads the message
        because that is where the arena put its reason.
        """
        text = str(exc)
        for hopeless in ("refused this worker", "AUTH_FAILED", "VERSION_UNSUPPORTED",
                         "METHOD_NOT_ALLOWED"):
            if hopeless in text:
                return True
        return False


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def _judge_error(detail: str) -> dict:
    """A ``612 JUDGE_ERROR`` verdict shaped the way ``server.record_verdict`` expects.

    612 exists so a judge-side failure is never charged to the player. Every field the verdict
    renderer reads is present, so a client displays this like any other verdict instead of
    failing on a missing key.
    """
    return {
        "verdict": int(Verdict.JUDGE_ERROR),
        "phrase": "JUDGE_ERROR",
        "detail": detail,
        "tests_passed": "0/0",
        "failures": [],
    }


def _verdict_header(verdict: dict) -> str:
    """``"606 TIME_COMPLEXITY_VIOLATION"`` for the result's ``Verdict`` header.

    The code and the phrase travel together — the graded requirement applied to the 6xx
    namespace as well as to response start lines. ``describe_status`` rather than
    ``format_status`` because this renders a value that reached us through a dict: strictness
    here would turn a surprising verdict code into a crash in the worker instead of a
    surprising line in the log.
    """
    code = verdict.get("verdict")
    if code is None:
        return "no verdict"
    return describe_status(code, verdict.get("phrase"))


def _sleep(seconds: float) -> bool:
    """Sleep, returning False if the user interrupted. Keeps Ctrl-C out of the caller."""
    try:
        time.sleep(seconds)
    except KeyboardInterrupt:
        return False
    return True


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m cdap.judge.worker",
        description="A CDAP judge worker: pulls submissions from an arena and judges them.",
    )
    parser.add_argument("--arena", default="127.0.0.1:5050",
                        help="arena address as HOST:PORT (default: 127.0.0.1:5050)")
    parser.add_argument("--id", default="w1",
                        help="this worker's id, shown in every verdict's 'worker' field and "
                             "as the log prefix (default: w1)")
    parser.add_argument("--token", default="",
                        help="pre-shared token matching the arena's --worker-token")
    parser.add_argument("--backend", choices=("subprocess", "docker"), default="subprocess",
                        help="isolation backend to request; the one that actually runs is "
                             "reported in the verdict and may differ (default: subprocess)")
    parser.add_argument("--poll-wait-ms", type=int, default=DEFAULT_POLL_WAIT_MS,
                        help=f"how long to ask the arena to hold a long-poll open (default: "
                             f"{DEFAULT_POLL_WAIT_MS}); the arena's advertised value wins")
    parser.add_argument("--no-ast-guard", action="store_true",
                        help="run submissions with the AST guard disabled. The guard is "
                             "defence-in-depth, not a security boundary, and this flag is how "
                             "experiments/backend_overhead.py proves it: the escapes in "
                             "samples/evil_*.py succeed under subprocess and fail under "
                             "docker. Never point this at untrusted input.")
    parser.add_argument("--no-profile", action="store_true",
                        help="judge correctness only, skipping the complexity measurement")
    parser.add_argument("--once", action="store_true",
                        help="exit after judging one submission (for scripted demos)")
    parser.add_argument("--max-jobs", type=int, default=0,
                        help="exit after judging this many submissions (0 means never)")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="add full headers and bodies to the wire log. The baseline log is "
                             "NOT gated behind this — every message and status is printed "
                             "either way, because that is a graded requirement.")
    return parser


def _split_address(address: str) -> Tuple[str, int]:
    """Split ``HOST:PORT``, with a message a human can act on when it is malformed."""
    host, separator, port = address.rpartition(":")
    if not separator or not port.isdigit():
        raise ValueError(f"--arena must look like HOST:PORT, not {address!r}")
    return (host or "127.0.0.1"), int(port)


def main(argv=None) -> int:
    # Before parse_args, not after. ``--help`` prints and exits *inside* parse_args, so a
    # reconfigure that came later would never run for the one output most likely to be read
    # first — and the em dashes in the help text would come out as mojibake on a cp1252
    # console. Phase 1 found this console needs the reconfigure for the wire log's arrows;
    # the help text needs it just as much.
    capabilities.enable_utf8_output()

    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        host, port = _split_address(args.arena)
    except ValueError as exc:
        parser.error(str(exc))       # exits; the return below is unreachable
        return 2

    # The prefix is what makes two workers readable, whether in two terminals or piped into one.
    log = WireLog(stream=sys.stdout, verbose=args.verbose, prefix=f"[{args.id}]")
    log.note(f"CDAP judge worker {args.id} speaking {PROTOCOL_VERSION}")

    if args.no_ast_guard:
        # Loud, every time, on purpose. The point of the flag is that it makes the sandbox
        # weaker, and a run whose log does not say so is a run whose results cannot be
        # interpreted afterwards.
        log.note("--no-ast-guard: submissions run WITHOUT the AST guard. The guard is "
                 "defence-in-depth, not a security boundary — the container is the boundary. "
                 "Point this at samples/evil_*.py only.")
    if args.no_profile:
        log.note("--no-profile: correctness only, no complexity measurement — so no 606/607 "
                 "verdict can be reached")

    worker = JudgeWorker(
        host, port, args.id, log,
        backend_name=args.backend,
        token=args.token,
        poll_wait_ms=args.poll_wait_ms,
        guard=not args.no_ast_guard,
        profile=not args.no_profile,
    )

    try:
        code = worker.run(once=args.once, max_jobs=args.max_jobs)
    except KeyboardInterrupt:
        log.note("interrupted")
        code = 0
    finally:
        worker.close()

    log.note(f"worker {args.id} stopped after judging {worker.jobs_done} submission(s)")
    return code


if __name__ == "__main__":
    sys.exit(main())
