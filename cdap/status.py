"""CDAP status codes — two deliberately separate namespaces.

This module is small but it encodes one of the protocol's central design decisions,
so it is worth stating plainly.

**Protocol status (1xx-5xx)** answers: *did the message get here and was it
well-formed enough to act on?* It is about the conversation.

**Judge verdict (6xx)** answers: *what did the judge decide about the code?* It is
about the submission.

These are different questions and CDAP refuses to conflate them. A submission that
comes back ``606 TIME_COMPLEXITY_VIOLATION`` was a protocol **success**: the frame
arrived intact, the hash matched, the judge ran, and a decision was reached. The
player's algorithm was too slow — that is not a transport problem, and encoding it
as ``400 BAD_REQUEST`` would make "your frame was malformed" indistinguishable from
"your algorithm is too slow". Real HTTP APIs make this mistake constantly, usually
by returning 400 for business-rule failures.

The split also has a practical consequence: a client can handle every 4xx/5xx with
one generic error path, while 6xx always means *show this to the player as a result*.

Numeric code vs phrase
----------------------
The **code** is the machine-readable class; the **phrase** names the specific
condition. So ``409 ALREADY_QUEUED``, ``409 ROOM_FULL``, and ``409 USER_EXISTS``
share a code but carry distinct phrases. A client that only understands "409 means
a state conflict" still behaves correctly; one that reads the phrase can say
something more useful. This is exactly what HTTP reason phrases are for.

Because the assignment requires printing *"the status code and status phrase"*, the
phrase is never optional and never invented at the call site — it comes from here.
"""

from __future__ import annotations

from enum import IntEnum

# --------------------------------------------------------------------------
# Protocol status — 1xx-5xx. About the message, never about the code submitted.
# --------------------------------------------------------------------------

class Status(IntEnum):
    """Protocol-level status codes, grouped like HTTP's so the classes are familiar."""

    # 2xx — the request was understood and acted on
    OK = 200
    CREATED = 201
    ACCEPTED = 202
    NO_CONTENT = 204

    # 4xx — the client's request cannot be acted on as sent
    BAD_REQUEST = 400
    AUTH_FAILED = 401
    FORBIDDEN = 403
    NOT_FOUND = 404
    METHOD_NOT_ALLOWED = 405
    REQUEST_TIMEOUT = 408
    CONFLICT = 409
    MATCH_ENDED = 410
    PAYLOAD_TOO_LARGE = 413
    UNSUPPORTED_LANGUAGE = 415
    BODY_HASH_MISMATCH = 422
    VERSION_UNSUPPORTED = 426
    RATE_LIMITED = 429

    # 5xx — the server failed, or cannot serve right now
    INTERNAL_ERROR = 500
    JUDGE_UNAVAILABLE = 503


#: The phrase sent when a caller does not name a more specific one.
#: Several codes have alternates — see ``PHRASE_ALTERNATES`` — but every code has
#: exactly one default so a response can always be built without deciding.
DEFAULT_PHRASE = {
    Status.OK: "OK",
    Status.CREATED: "CREATED",
    Status.ACCEPTED: "ACCEPTED",
    Status.NO_CONTENT: "NO_CONTENT",
    Status.BAD_REQUEST: "BAD_REQUEST",
    Status.AUTH_FAILED: "AUTH_FAILED",
    Status.FORBIDDEN: "FORBIDDEN",
    Status.NOT_FOUND: "NOT_FOUND",
    Status.METHOD_NOT_ALLOWED: "METHOD_NOT_ALLOWED",
    Status.REQUEST_TIMEOUT: "REQUEST_TIMEOUT",
    Status.CONFLICT: "CONFLICT",
    Status.MATCH_ENDED: "MATCH_ENDED",
    Status.PAYLOAD_TOO_LARGE: "PAYLOAD_TOO_LARGE",
    Status.UNSUPPORTED_LANGUAGE: "UNSUPPORTED_LANGUAGE",
    Status.BODY_HASH_MISMATCH: "BODY_HASH_MISMATCH",
    Status.VERSION_UNSUPPORTED: "VERSION_UNSUPPORTED",
    Status.RATE_LIMITED: "RATE_LIMITED",
    Status.INTERNAL_ERROR: "INTERNAL_ERROR",
    Status.JUDGE_UNAVAILABLE: "JUDGE_UNAVAILABLE",
}

#: Additional phrases a code may legitimately carry, beyond its default.
#: Declared explicitly so a typo at a call site is caught by ``phrase_for`` instead
#: of silently going out on the wire and confusing the other end.
PHRASE_ALTERNATES = {
    Status.CREATED: ("REGISTERED",),
    Status.ACCEPTED: ("QUEUED",),
    Status.FORBIDDEN: ("NOT_IN_MATCH", "NOT_IN_ROOM", "WRONG_STATE"),
    Status.NOT_FOUND: ("ROOM_NOT_FOUND", "SUBMISSION_NOT_FOUND"),
    Status.CONFLICT: ("ALREADY_QUEUED", "NOT_QUEUED", "ROOM_FULL", "USER_EXISTS"),
    Status.RATE_LIMITED: ("SUBMIT_COOLDOWN",),
    Status.JUDGE_UNAVAILABLE: ("SERVER_BUSY",),
}


def phrase_for(code, phrase=None) -> str:
    """Return the phrase to put on the wire for ``code``.

    With no ``phrase``, the default is used. With one, it is checked against the
    declared alternates so that a mistyped phrase fails here — loudly, at the call
    site — rather than being sent to a peer that cannot interpret it.
    """
    status = Status(code)
    if phrase is None:
        return DEFAULT_PHRASE[status]

    allowed = (DEFAULT_PHRASE[status],) + PHRASE_ALTERNATES.get(status, ())
    if phrase not in allowed:
        raise ValueError(
            f"phrase {phrase!r} is not declared for status {int(status)}; "
            f"allowed: {', '.join(allowed)}. "
            f"Add it to PHRASE_ALTERNATES if it is intentional."
        )
    return phrase


def is_success(code) -> bool:
    """True for 2xx. Anything else means the request did not fully succeed."""
    return 200 <= int(code) < 300


# --------------------------------------------------------------------------
# Judge verdicts — 6xx. About the submitted code, never about the message.
# --------------------------------------------------------------------------

class Verdict(IntEnum):
    """Judge verdicts. Reached only *after* a protocol-level success."""

    ACCEPTED = 600                     # correct AND within the complexity contract
    WRONG_ANSWER = 601
    TIME_LIMIT_EXCEEDED = 602          # a single run blew the wall-clock limit
    MEMORY_LIMIT_EXCEEDED = 603
    COMPILE_ERROR = 604
    RUNTIME_ERROR = 605
    TIME_COMPLEXITY_VIOLATION = 606    # correct, but scales worse than the contract
    SPACE_COMPLEXITY_VIOLATION = 607
    OUTPUT_FORMAT_ERROR = 608
    SANDBOX_VIOLATION = 609

    # 610 is unassigned, and the gap is kept rather than closed. 600-609 are verdicts about
    # the *submission*: it was wrong, too slow, over a limit, hostile. 611 and 612 are
    # verdicts about the *judgment* — the judge could not reach a confident answer, or the
    # judge itself broke. Those two are the only verdicts that are not a statement about the
    # player's code, so they start on a fresh number. Renumbering them down to close the gap
    # would also be fine; leaving it documented costs nothing and the boundary is real.
    INDETERMINATE_COMPLEXITY = 611     # measurements fit no model well enough to judge
    JUDGE_ERROR = 612                  # the judge itself failed; never the player's fault


VERDICT_PHRASE = {
    Verdict.ACCEPTED: "ACCEPTED",
    Verdict.WRONG_ANSWER: "WRONG_ANSWER",
    Verdict.TIME_LIMIT_EXCEEDED: "TIME_LIMIT_EXCEEDED",
    Verdict.MEMORY_LIMIT_EXCEEDED: "MEMORY_LIMIT_EXCEEDED",
    Verdict.COMPILE_ERROR: "COMPILE_ERROR",
    Verdict.RUNTIME_ERROR: "RUNTIME_ERROR",
    Verdict.TIME_COMPLEXITY_VIOLATION: "TIME_COMPLEXITY_VIOLATION",
    Verdict.SPACE_COMPLEXITY_VIOLATION: "SPACE_COMPLEXITY_VIOLATION",
    Verdict.OUTPUT_FORMAT_ERROR: "OUTPUT_FORMAT_ERROR",
    Verdict.SANDBOX_VIOLATION: "SANDBOX_VIOLATION",
    Verdict.INDETERMINATE_COMPLEXITY: "INDETERMINATE_COMPLEXITY",
    Verdict.JUDGE_ERROR: "JUDGE_ERROR",
}

#: One-line explanation shown to the player. The verdict phrase says *what*; this
#: says *why it means what it means*, which matters most for the two complexity
#: verdicts, since "your code is correct but rejected" is otherwise baffling.
VERDICT_HELP = {
    Verdict.ACCEPTED: "Correct, and within the problem's complexity contract.",
    Verdict.WRONG_ANSWER: "Ran fine, but produced the wrong answer on at least one test.",
    Verdict.TIME_LIMIT_EXCEEDED: "A single run exceeded the problem's wall-clock limit.",
    Verdict.MEMORY_LIMIT_EXCEEDED: "Peak memory exceeded the problem's limit.",
    Verdict.COMPILE_ERROR: "The source could not be parsed or compiled.",
    Verdict.RUNTIME_ERROR: "The solution raised an uncaught exception.",
    Verdict.TIME_COMPLEXITY_VIOLATION: (
        "Every test passed, but measured runtime grows faster than the contract "
        "allows. A correct algorithm of the wrong complexity class."
    ),
    Verdict.SPACE_COMPLEXITY_VIOLATION: (
        "Every test passed, but auxiliary memory grows faster than the contract "
        "allows."
    ),
    Verdict.OUTPUT_FORMAT_ERROR: "The right value in the wrong shape or type.",
    Verdict.SANDBOX_VIOLATION: "Attempted an operation the sandbox forbids.",
    Verdict.INDETERMINATE_COMPLEXITY: (
        "Measurements did not fit any complexity model well enough to judge. "
        "Usually means the solution is too fast to measure at these input sizes."
    ),
    Verdict.JUDGE_ERROR: "The judge failed. This is our bug, not yours.",
}


def verdict_phrase_for(code) -> str:
    """Return the phrase for a judge verdict."""
    return VERDICT_PHRASE[Verdict(code)]


def is_accepted(code) -> bool:
    """True only for a full pass — correct *and* inside the complexity contract."""
    return int(code) == int(Verdict.ACCEPTED)


# --------------------------------------------------------------------------
# The boundary between the namespaces
# --------------------------------------------------------------------------
#
# These two helpers exist so the invariant is enforced by code rather than by
# everyone remembering it. protocol.py calls them when building frames, so a bug
# that tries to send a verdict as a response status fails immediately and locally,
# instead of producing a frame that a peer will misinterpret.

def assert_protocol_status(code) -> Status:
    """Validate that ``code`` belongs in a response start line. Returns it."""
    value = int(code)
    if 600 <= value < 700:
        raise ValueError(
            f"{value} is a judge verdict and must not be used as a response status. "
            "Verdicts travel in a VERDICT event's Verdict: header, not in a start "
            "line. See CLAUDE.md design invariant 2."
        )
    return Status(value)


def assert_verdict(code) -> Verdict:
    """Validate that ``code`` is a judge verdict. Returns it."""
    value = int(code)
    if not 600 <= value < 700:
        raise ValueError(
            f"{value} is a protocol status and must not be used as a judge verdict. "
            "See CLAUDE.md design invariant 2."
        )
    return Verdict(value)


def format_status(code, phrase=None) -> str:
    """Render any code as ``"<code> <PHRASE>"`` — the form the wire log requires.

    Handles both namespaces, because every log line and every start line needs the
    code and the phrase together. The assignment asks for both; there is no code
    path in CDAP that prints a bare number.

    Strict: an unknown code or an undeclared phrase raises. That is right for
    everything *we* build, and wrong for anything we *received* — see
    ``describe_status``.
    """
    value = int(code)
    if 600 <= value < 700:
        return f"{value} {verdict_phrase_for(value)}"
    return f"{value} {phrase_for(value, phrase)}"


def describe_status(code, phrase=None) -> str:
    """Render a code and phrase that arrived from a peer, **never raising**.

    ``format_status`` is deliberately strict, and that strictness is a feature right up
    to the moment the value came from someone else. ``parse_start_line`` records a
    response's status and phrase exactly as sent, without validating either — it has to,
    because refusing to parse a frame we disagree with would leave us unable to answer
    ``426`` or ``400`` and explain why.

    So there is a gap: a peer can send ``CDAP/1.0 999 NONSENSE``, and the *log line* that
    would have told us so would itself raise while formatting it. One malformed frame
    would take down the session that was about to reject it, turning a peer's mistake into
    our own failure.

    This closes that gap. Strict when we build a frame, tolerant when we display one —
    and the code and phrase still always travel together, which is the requirement that
    matters.
    """
    try:
        value = int(code)
    except (TypeError, ValueError):
        return f"? {phrase or 'UNKNOWN'}"
    if phrase:
        # Shown exactly as received. A phrase we do not recognise is information about the
        # peer, and rewriting it to our own spelling would hide that.
        return f"{value} {phrase}"
    try:
        return format_status(value)
    except (ValueError, KeyError):
        return f"{value} UNKNOWN"
