"""CDAP/1.0 wire format: messages, framing, the TCP connection, the UDP codec, and
the wire logger.

This is the module the video walks through, so it is written to be read aloud.

Three things worth understanding before the code:

**1. Why a text protocol.** The assignment requires the programs to print every
message they send and receive. A binary encoding would force the log to be a hex
dump or a decoded paraphrase; a text format means *the log is the wire*. What you
read on screen is byte-for-byte what crossed the socket.

**2. Why framing is needed at all — the central TCP lesson.** TCP is a byte
*stream*, not a message stream. ``send()`` twice and the peer may ``recv()`` once
and get both, or get half of the first. There is no "message" in TCP, so the
application layer has to draw the boundaries. CDAP draws them the way HTTP does:
a start line, headers, a blank line, then exactly ``Content-Length`` bytes of body.
The reader always knows where the frame ends because the frame said so.

Contrast this with UDP, further down: one datagram is one message, boundaries come
free from the transport, and no ``Content-Length`` is needed at all. The two codecs
sitting in one file is the clearest illustration of what each transport does and
does not give you.

**3. Why three kinds of message.** CDAP is not pure request/response — the server
pushes events nobody asked for (a match starting, a verdict arriving, the opponent
submitting). So a receiver has to tell three things apart from the first line:

    CDAP/1.0 SUBMIT              -> a request
    CDAP/1.0 202 ACCEPTED        -> a response
    CDAP/1.0 EVENT VERDICT       -> a server-pushed event

The rule is decided on the second token: all digits means a response; the literal
``EVENT`` means an event; anything else is a request method. ``EVENT`` is therefore
reserved and can never be a method name.

Correlation follows from that: requests carry a monotonic ``Seq``, responses echo
it back, and events deliberately carry **no** ``Seq`` at all. This is what lets a
client run one reader thread that routes every frame with no ambiguity — a frame
with a ``Seq`` belongs to whoever is blocked waiting on that number, and an event
goes to the event handler. Giving events a ``Seq`` would break that, which is why
``Message.event()`` refuses to build one.
"""

from __future__ import annotations

import hashlib
import socket
import sys
import threading
from collections.abc import MutableMapping
from dataclasses import dataclass, field
from enum import Enum
from urllib.parse import quote, unquote

from . import capabilities
from .status import assert_protocol_status, format_status, phrase_for

PROTOCOL_NAME = "CDAP"
PROTOCOL_VERSION = "CDAP/1.0"

#: The token that marks a start line as a server-pushed event. Reserved: no method
#: may be called EVENT, or the disambiguation rule above would be ambiguous.
EVENT_TOKEN = "EVENT"

CRLF = b"\r\n"

# Resource limits. These are not tuning knobs, they are a security boundary: a peer
# can otherwise announce Content-Length: 999999999999 and make us try to allocate
# it before we have authenticated anything at all.
MAX_HEADER_BYTES = 16 * 1024        # whole header block, generously above real use
MAX_HEADER_COUNT = 64
MAX_BODY_BYTES = 1024 * 1024        # 1 MB; a larger SUBMIT earns 413 PAYLOAD_TOO_LARGE
MAX_DATAGRAM_BYTES = 1400           # stays under a typical 1500-byte MTU, so no IP
                                    # fragmentation for the feed

#: How much of a body the wire log shows inline. The full length is always printed
#: even when the content is cut, because the length is part of the framing story.
LOG_BODY_PREVIEW = 96


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------

class ProtocolError(Exception):
    """A frame could not be parsed, so the byte stream is no longer trustworthy.

    Framing errors are not recoverable in the way a bad *request* is. If we cannot
    find the end of a frame, we no longer know where the next one starts, so the
    only correct response is to close the connection. A well-formed request that
    happens to be wrong gets a 4xx and the connection continues.
    """


class FrameTooLarge(ProtocolError):
    """A peer announced or sent more than the limits above allow."""


# --------------------------------------------------------------------------
# Headers
# --------------------------------------------------------------------------

# Header names are case-insensitive on the wire, but the log is a deliverable and
# `Content-Length` reads better than `content-length`. So we store names folded to
# lowercase for comparison and render them through this table on the way out.
# Note why `str.title()` will not do: it would produce `Body-Sha256`.
_CANONICAL_NAMES = {
    "seq": "Seq",
    "session": "Session",
    "match": "Match",
    "room": "Room",
    "submission": "Submission",
    "content-length": "Content-Length",
    "content-type": "Content-Type",
    "body-sha256": "Body-SHA256",
    "detail": "Detail",
    "event-id": "Event-Id",
    "verdict": "Verdict",
    "lang": "Lang",
    "server": "Server",
    "user": "User",
    "queue-pos": "Queue-Pos",
    "est-wait-ms": "Est-Wait-Ms",
    "worker": "Worker",
    "udp-port": "Udp-Port",
    "stage": "Stage",
}


def canonical_header(name: str) -> str:
    """Preferred display spelling for a header name."""
    return _CANONICAL_NAMES.get(name.lower(), name)


class Headers(MutableMapping):
    """Case-insensitive header map that renders with canonical spelling.

    Implemented over ``MutableMapping`` so the five methods below give it full dict
    behaviour — ``in``, ``.get()``, iteration, ``len()`` — without re-implementing
    any of it.
    """

    def __init__(self, initial=None):
        self._values = {}
        if initial:
            for name, value in dict(initial).items():
                self[name] = value

    def __getitem__(self, name):
        return self._values[name.lower()]

    def __setitem__(self, name, value):
        # Everything goes on the wire as text, so normalise here rather than making
        # every call site remember to str() its ints.
        self._values[name.lower()] = value if isinstance(value, str) else str(value)

    def __delitem__(self, name):
        del self._values[name.lower()]

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)

    def get_int(self, name, default=None):
        """Read a header as an int, returning ``default`` if absent or not a number.

        Lenient on purpose: a peer sending ``Seq: banana`` is a bad request to be
        answered with 400, not a crash in the parser.
        """
        raw = self._values.get(name.lower())
        if raw is None:
            return default
        try:
            return int(raw)
        except ValueError:
            return default

    def render(self) -> list:
        """Header lines in canonical spelling, sorted for a stable, greppable log."""
        return [f"{canonical_header(name)}: {value}" for name, value in sorted(self._values.items())]

    def __repr__(self):
        return f"Headers({self._values!r})"


# --------------------------------------------------------------------------
# Messages
# --------------------------------------------------------------------------

class Kind(Enum):
    """Which of the three start-line forms a message uses."""

    REQUEST = "REQUEST"
    RESPONSE = "RESPONSE"
    EVENT = "EVENT"


@dataclass
class Message:
    """One CDAP frame: a start line, headers, and a body.

    Build these with the three classmethods rather than the constructor — they are
    what enforce the rules about which fields each kind may carry.
    """

    kind: Kind
    version: str = PROTOCOL_VERSION

    method: str = ""        # REQUEST only, e.g. "SUBMIT"
    status: int = 0         # RESPONSE only, e.g. 202
    phrase: str = ""        # RESPONSE only, e.g. "ACCEPTED"
    event: str = ""         # EVENT only, e.g. "VERDICT"

    headers: Headers = field(default_factory=Headers)
    body: bytes = b""

    # -- constructors ------------------------------------------------------

    @classmethod
    def request(cls, method, headers=None, body=b"", seq=None):
        """A client -> server request. Carries a ``Seq`` so the reply can be matched."""
        if method.upper() == EVENT_TOKEN:
            raise ValueError(
                "EVENT is reserved as a start-line marker and cannot be a method name"
            )
        message = cls(kind=Kind.REQUEST, method=method.upper(),
                      headers=Headers(headers), body=_as_bytes(body))
        if seq is not None:
            message.headers["Seq"] = seq
        return message

    @classmethod
    def response(cls, status, phrase=None, headers=None, body=b"", seq=None):
        """A server -> client reply.

        ``assert_protocol_status`` is what stops a judge verdict from being used as
        a response status: passing 606 here raises rather than emitting a frame the
        peer would misread. That is design invariant 2, enforced instead of trusted.
        """
        code = assert_protocol_status(status)
        message = cls(kind=Kind.RESPONSE, status=int(code),
                      phrase=phrase_for(code, phrase),
                      headers=Headers(headers), body=_as_bytes(body))
        if seq is not None:
            message.headers["Seq"] = seq
        return message

    @classmethod
    def make_event(cls, name, headers=None, body=b""):
        """A server -> client push.

        Refuses a ``Seq``. Events are not replies to anything, and a client's reader
        thread uses "has a Seq" to decide whether a frame belongs to a waiting
        caller. An event carrying a Seq would be delivered to whoever happened to be
        waiting on that number — design invariant 4.
        """
        built = Headers(headers)
        if "Seq" in built:
            raise ValueError(
                "events must not carry Seq — responses echo a request's Seq, events "
                "correlate by Event-Id. See CLAUDE.md design invariant 4."
            )
        return cls(kind=Kind.EVENT, event=name.upper(), headers=built, body=_as_bytes(body))

    # -- accessors ---------------------------------------------------------

    @property
    def seq(self):
        """The request/response correlation number, or None for events."""
        return self.headers.get_int("Seq")

    @property
    def start_line(self) -> str:
        """The first line of the frame, exactly as it goes on the wire."""
        if self.kind is Kind.REQUEST:
            return f"{self.version} {self.method}"
        if self.kind is Kind.RESPONSE:
            return f"{self.version} {self.status} {self.phrase}"
        return f"{self.version} {EVENT_TOKEN} {self.event}"

    @property
    def name(self) -> str:
        """Short label for logs and dispatch tables."""
        if self.kind is Kind.REQUEST:
            return self.method
        if self.kind is Kind.RESPONSE:
            return format_status(self.status, self.phrase)
        return f"{EVENT_TOKEN} {self.event}"

    def body_sha256(self) -> str:
        """Hex SHA-256 of the body as it currently stands."""
        return hashlib.sha256(self.body).hexdigest()

    def attach_body_hash(self):
        """Add ``Body-SHA256`` for integrity checking. Returns self for chaining."""
        self.headers["Body-SHA256"] = self.body_sha256()
        return self

    def body_hash_ok(self):
        """Compare the body against ``Body-SHA256``.

        Returns True/False, or None when no hash was sent (nothing was claimed, so
        there is nothing to contradict). The server turns False into
        ``422 BODY_HASH_MISMATCH``; the client's ``--tamper`` flag exists to make
        that happen on camera.
        """
        claimed = self.headers.get("Body-SHA256")
        if claimed is None:
            return None
        # Compare case-insensitively: hex is hex, and rejecting a peer for sending
        # uppercase digits would be a pointless interop failure.
        return claimed.lower() == self.body_sha256()

    def text(self) -> str:
        """Body decoded as UTF-8, replacing anything undecodable rather than raising."""
        return self.body.decode("utf-8", errors="replace")

    # -- serialisation -----------------------------------------------------

    def encode(self) -> bytes:
        """Render the whole frame to bytes.

        ``Content-Length`` is computed here and overwrites anything already set, so
        it can never disagree with the body actually sent — a mismatch would
        desynchronise the peer's reader for every following frame. It is emitted
        even when zero: a reader that always finds the header has one code path
        instead of two.
        """
        self.headers["Content-Length"] = len(self.body)

        lines = [self.start_line]
        lines.extend(self.headers.render())
        head = CRLF.join(line.encode("utf-8") for line in lines) + CRLF + CRLF
        return head + self.body

    def __str__(self):
        return self.start_line


def _as_bytes(body) -> bytes:
    """Accept str or bytes for a body; everything on the wire is bytes."""
    if isinstance(body, bytes):
        return body
    if isinstance(body, str):
        return body.encode("utf-8")
    raise TypeError(f"body must be str or bytes, not {type(body).__name__}")


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

def parse_start_line(line: str) -> dict:
    """Split a start line into its parts, applying the disambiguation rule.

    Note what is *not* done here: the version is recorded, not validated. A peer
    speaking ``CDAP/0.9`` must still get a parsed frame so the server can answer
    ``426 VERSION_UNSUPPORTED`` — refusing to parse would leave us unable to
    explain why. That is what the client's ``--bad-version`` flag demonstrates.
    """
    tokens = line.split()
    if len(tokens) < 2:
        raise ProtocolError(f"start line has too few tokens: {line!r}")

    version, second = tokens[0], tokens[1]
    if not version.startswith(PROTOCOL_NAME + "/"):
        raise ProtocolError(f"not a {PROTOCOL_NAME} frame: {line!r}")

    # The three-way rule, in the order the docstring at the top of the file gives it.
    if second.isdigit():
        return {
            "kind": Kind.RESPONSE,
            "version": version,
            "status": int(second),
            # Multi-word phrases are not expected, but joining is more forgiving
            # than indexing and costs nothing.
            "phrase": " ".join(tokens[2:]),
        }
    if second == EVENT_TOKEN:
        if len(tokens) < 3:
            raise ProtocolError(f"EVENT frame with no event name: {line!r}")
        return {"kind": Kind.EVENT, "version": version, "event": tokens[2]}
    return {"kind": Kind.REQUEST, "version": version, "method": second}


def _read_line(reader) -> str:
    """Read one CRLF-terminated line, enforcing the header size limit."""
    raw = reader.readline(MAX_HEADER_BYTES + 1)
    if len(raw) > MAX_HEADER_BYTES:
        raise FrameTooLarge("header line exceeded MAX_HEADER_BYTES")
    if not raw:
        return ""  # EOF
    return raw.decode("utf-8", errors="replace").rstrip("\r\n")


def read_message(reader):
    """Read exactly one frame from a buffered binary reader.

    Returns None on a clean end-of-stream, which is how a peer closing normally is
    distinguished from a peer truncating a frame mid-way (that raises).

    This function is the answer to "TCP has no message boundaries": read one line
    for the start, lines until a blank one for the headers, then exactly as many
    body bytes as Content-Length promised. Never more — the bytes after the body
    belong to the next frame.
    """
    start = _read_line(reader)
    if start == "":
        return None  # peer closed cleanly between frames

    parts = parse_start_line(start)
    headers = Headers()
    total_header_bytes = len(start)

    while True:
        line = _read_line(reader)
        if line == "":
            # A blank line ends the header block. A truly closed socket would have
            # ended at the start line, so reaching EOF here means a truncated frame.
            break
        total_header_bytes += len(line)
        if total_header_bytes > MAX_HEADER_BYTES:
            raise FrameTooLarge("header block exceeded MAX_HEADER_BYTES")
        if len(headers) >= MAX_HEADER_COUNT:
            raise FrameTooLarge(f"more than {MAX_HEADER_COUNT} headers")
        name, separator, value = line.partition(":")
        if not separator:
            raise ProtocolError(f"header line has no colon: {line!r}")
        headers[name.strip()] = value.strip()

    length = headers.get_int("Content-Length", 0)
    if length is None or length < 0:
        raise ProtocolError(f"invalid Content-Length: {headers.get('Content-Length')!r}")
    if length > MAX_BODY_BYTES:
        # Refuse before allocating. The server still owes the peer a 413, but that
        # is a decision for the caller; here we only refuse to be the victim.
        raise FrameTooLarge(f"Content-Length {length} exceeds MAX_BODY_BYTES {MAX_BODY_BYTES}")

    body = reader.read(length) if length else b""
    if len(body) != length:
        raise ProtocolError(
            f"truncated body: Content-Length promised {length} bytes, got {len(body)}"
        )

    return Message(headers=headers, body=body, **parts)


# --------------------------------------------------------------------------
# TCP connection
# --------------------------------------------------------------------------

class Connection:
    """A CDAP framing layer over one TCP socket, with logging built in.

    Sends are serialised with a lock because the server genuinely writes from more
    than one thread: a request handler can be composing a response while the match
    loop pushes an event down the same socket. Without the lock the two frames
    would interleave and the client's reader would see corruption — a real bug this
    protocol invites, and worth mentioning on video.

    Reads are *not* locked: exactly one reader thread per connection is the design.
    """

    def __init__(self, sock, log=None, peer=""):
        self._sock = sock
        # makefile gives us buffered readline()/read(n), which is what turns a byte
        # stream into something we can frame without hand-rolling a buffer.
        self._reader = sock.makefile("rb")
        self._send_lock = threading.Lock()
        self.log = log
        self.peer = peer or _peer_name(sock)
        self.closed = False

    def send(self, message):
        """Frame, log, and write one message."""
        data = message.encode()  # encode first: it sets Content-Length, which we log
        if self.log:
            self.log.sent(message, peer=self.peer)
        with self._send_lock:
            self._sock.sendall(data)

    def recv(self):
        """Read, log, and return one message. None means the peer closed cleanly."""
        message = read_message(self._reader)
        if message is None:
            self.closed = True
            return None
        if self.log:
            self.log.received(message, peer=self.peer)
        return message

    def close(self):
        """Close the connection, tolerating a peer that has already gone away."""
        self.closed = True
        try:
            self._reader.close()
        except OSError:
            pass
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass  # already closed, or never connected
        try:
            self._sock.close()
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()
        return False


def _peer_name(sock) -> str:
    """``host:port`` for logs, or a placeholder if the socket is already detached."""
    try:
        address = sock.getpeername()
    except OSError:
        return "?"
    if isinstance(address, tuple) and len(address) >= 2:
        return f"{address[0]}:{address[1]}"
    return str(address)


# --------------------------------------------------------------------------
# UDP codec
# --------------------------------------------------------------------------
#
# Compare this with the TCP framing above: there is no length header and no framing
# logic at all, because one datagram is one message. UDP preserves message
# boundaries; TCP does not. That single difference is most of what the transport
# chapter of the report is about.

def encode_datagram(kind: str, fields) -> bytes:
    """Build ``CDAP/1.0 TICK match=m-0001 seq=87 ...``.

    Values are percent-encoded so a space or ``=`` inside one cannot be mistaken
    for a field separator. Player names come from users, so this is not theoretical.
    """
    parts = [PROTOCOL_VERSION, kind.upper()]
    for name, value in fields.items():
        parts.append(f"{name}={quote(str(value), safe='')}")
    datagram = " ".join(parts).encode("utf-8")
    if len(datagram) > MAX_DATAGRAM_BYTES:
        raise FrameTooLarge(
            f"datagram is {len(datagram)} bytes, over the {MAX_DATAGRAM_BYTES} limit; "
            "the feed must stay inside one MTU so a tick is never fragmented"
        )
    return datagram


def decode_datagram(data: bytes):
    """Parse a datagram into ``(kind, fields)``.

    Anything unparseable raises ``ProtocolError``. Unlike the TCP path, a bad
    datagram is *not* fatal: the caller logs it, drops it, and waits for the next
    one 200 ms later. Nothing needs recovering because nothing was owed.
    """
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProtocolError(f"datagram is not valid UTF-8: {exc}") from exc

    tokens = text.split()
    if len(tokens) < 2:
        raise ProtocolError(f"datagram too short: {text!r}")
    if not tokens[0].startswith(PROTOCOL_NAME + "/"):
        raise ProtocolError(f"not a {PROTOCOL_NAME} datagram: {text!r}")

    kind = tokens[1].upper()
    fields = {}
    for token in tokens[2:]:
        name, separator, value = token.partition("=")
        if not separator:
            raise ProtocolError(f"datagram field has no '=': {token!r}")
        fields[name] = unquote(value)
    return kind, fields


class LatestWins:
    """Stale-datagram filter: accept a sequence number only if it beats the last one.

    This is the whole reliability story for the UDP feed, and it is deliberately
    this small. The feed carries *current state* — the clock, the score, how many
    tests have passed — so an older datagram arriving late has nothing to add. Its
    information is already superseded.

    Retransmitting it would actively hurt: the display would jump backwards. So
    reordering is handled by discarding rather than buffering, and loss is handled
    by waiting for the next tick. No ACKs, no timers, no retry queue.
    """

    def __init__(self):
        self._highest = {}

    def accept(self, stream, seq) -> bool:
        """True if this datagram is newer than everything seen on ``stream``."""
        last = self._highest.get(stream)
        if last is not None and seq <= last:
            return False
        self._highest[stream] = seq
        return True

    def highest(self, stream):
        return self._highest.get(stream)


# --------------------------------------------------------------------------
# Wire logger
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Markers:
    """Direction markers for the log.

    Two sets, because Phase 1 found that a Windows console on a legacy code page
    cannot encode the arrows: it prints replacement characters and the graded log
    becomes unreadable. So we test whether the arrows survive and fall back to
    ASCII if not. The log's structure, fields, and grep-ability are identical
    either way — only the glyphs change.
    """

    sent: str
    received: str
    dropped: str


UNICODE_MARKERS = Markers(sent="\u2192", received="\u2190", dropped="\u2717")   # → ← ✗
ASCII_MARKERS = Markers(sent="->", received="<-", dropped="x")


class WireLog:
    """Prints every message sent and received, with status codes and their phrases.

    This class *is* deliverable 2's "print the messages and the status (status code,
    status phrase) they send and receive" requirement, so two rules hold:

    * It is never gated behind a verbosity flag. ``verbose=True`` adds more detail
      — full bodies, every header — but the baseline log always prints.
    * A status never appears as a bare number. Everything goes through
      ``format_status`` so the phrase always travels with the code.

    Format::

        [TCP →] CDAP/1.0 SUBMIT  Seq=7 Match=m-0001 Lang=python Content-Length=412
        [TCP ←] CDAP/1.0 202 ACCEPTED  Seq=7 Submission=s-8831 Queue-Pos=1
        [EVENT] CDAP/1.0 EVENT VERDICT  Event-Id=17 Verdict=606 TIME_COMPLEXITY_VIOLATION
        [UDP ←] TICK match=m-0001 seq=87 player=alice passed=7/10
        [UDP ✗] stale seq=86 <= 87, dropped

    Events use ``[EVENT]`` with no direction marker: they only ever travel
    server -> client, so the direction carries no information.
    """

    def __init__(self, stream=None, verbose=False, prefix="", use_unicode=None):
        self._stream = stream if stream is not None else sys.stdout
        self.verbose = verbose
        self.prefix = prefix
        if use_unicode is None:
            capabilities.enable_utf8_output()
            use_unicode = capabilities.console_unicode_ok()
        self.markers = UNICODE_MARKERS if use_unicode else ASCII_MARKERS
        # One lock so concurrent threads cannot interleave half-lines into the log.
        # A garbled log is a damaged deliverable, not just untidy output.
        self._lock = threading.Lock()

    # -- TCP ---------------------------------------------------------------

    def sent(self, message, peer=""):
        self._log_message("TCP", self.markers.sent, message, peer)

    def received(self, message, peer=""):
        self._log_message("TCP", self.markers.received, message, peer)

    def _log_message(self, transport, marker, message, peer=""):
        if message.kind is Kind.EVENT:
            tag = "[EVENT]"
        else:
            tag = f"[{transport} {marker}]"

        line = f"{tag} {message.start_line}"
        summary = self._header_summary(message)
        if summary:
            line += "  " + summary
        self._emit(line)

        if message.body:
            self._emit(self._body_line(message))
        if self.verbose:
            for header_line in message.headers.render():
                self._emit(f"        {header_line}")

    def _header_summary(self, message) -> str:
        """Headers as ``Name=value``, in a fixed order so the log stays greppable.

        ``Content-Length`` is shown only when there is a body: printing
        ``Content-Length=0`` on every bodyless frame would add noise to every line
        of a log someone has to read on video.
        """
        rendered = []
        for name in sorted(message.headers):
            if name == "content-length" and not message.body:
                continue
            rendered.append(f"{canonical_header(name)}={message.headers[name]}")
        return " ".join(rendered)

    def _body_line(self, message) -> str:
        """One truncated preview line. The full length is always stated."""
        text = message.text().replace("\n", "\\n").replace("\r", "")
        shown = text[:LOG_BODY_PREVIEW]
        suffix = "..." if len(text) > LOG_BODY_PREVIEW else ""
        if self.verbose:
            shown, suffix = text, ""
        return f'        body[{len(message.body)}] "{shown}{suffix}"'

    # -- UDP ---------------------------------------------------------------

    def udp_sent(self, kind, fields, peer=""):
        self._emit(f"[UDP {self.markers.sent}] {self._udp_summary(kind, fields)}{self._peer(peer)}")

    def udp_received(self, kind, fields, peer=""):
        self._emit(f"[UDP {self.markers.received}] {self._udp_summary(kind, fields)}{self._peer(peer)}")

    def udp_dropped(self, reason):
        """A datagram we deliberately threw away — the visible half of LatestWins.

        Logged rather than silently discarded because "we dropped this on purpose,
        and here is why" is precisely the behaviour the UDP design claims, and the
        video needs to show it happening.
        """
        self._emit(f"[UDP {self.markers.dropped}] {reason}")

    def _peer(self, peer) -> str:
        """Append the datagram's source/destination, but only under ``-v``.

        The address matters for one specific claim: the server learns a client's UDP
        endpoint from the ATTACH datagram's *source address* rather than from any
        configuration, which is what makes the feed work through NAT. Worth showing
        on camera; not worth cluttering every one of 5-per-second ticks with.
        """
        return f"  from={peer}" if peer and self.verbose else ""

    @staticmethod
    def _udp_summary(kind, fields) -> str:
        """``KIND name=value name=value`` — the decoded view of a datagram.

        A value is quoted when it contains a space. On the wire it cannot contain one
        (values are percent-encoded), so quoting here is what keeps the *decoded* log
        unambiguous: ``player="alice smith"`` is visibly one field, not two.
        """
        parts = []
        for name, value in fields.items():
            text = str(value)
            if " " in text:
                text = f'"{text}"'
            parts.append(f"{name}={text}")
        rendered = " ".join(parts)
        return f"{kind} {rendered}" if rendered else kind

    # -- free-form ---------------------------------------------------------

    def note(self, text):
        """Server/client lifecycle lines: binding a port, a match starting, a kill."""
        self._emit(f"[     ] {text}")

    def status(self, code, phrase=None, detail=""):
        """Print a status with its phrase, for the paths that report one outside a frame."""
        line = f"[     ] {format_status(code, phrase)}"
        if detail:
            line += f" — {detail}" if self.markers is UNICODE_MARKERS else f" - {detail}"
        self._emit(line)

    def _emit(self, line):
        with self._lock:
            if self.prefix:
                line = f"{self.prefix} {line}"
            print(line, file=self._stream, flush=True)
