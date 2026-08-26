# CDAP code review

Scope: `cdap/**/*.py` (protocol, client, server, worker, sandbox, backends, runner,
profiler, capabilities, and problem catalogue). Review focused on runtime correctness,
security, resource handling, and performance. Existing documented limitations (plain-text
demo accounts, AST-only sandboxing, and Windows best-effort memory limits) are not repeated
as defects.

## Findings fixed in this pass

### High — contestant output could forge a judge result

**Location:** `cdap/judge/runner.py:674, 758` and `cdap/judge/backends.py:445-462`.

The harness parses a sentinel from child stdout. A submission can print a fake sentinel and
raise `SystemExit` (a `BaseException`, not an `Exception`), preventing the genuine result
from being emitted. The backend then accepts the forged record. This can turn a malicious
submission into an accepted verdict.

**Fix applied:** runner load and top-level dispatch now catch `BaseException`, convert it to a
real failure record, and always emit the genuine final sentinel. The backend still scans the
last sentinel line, so earlier contestant output cannot win.

**Further hardening:** move the record to a dedicated parent-controlled IPC/file-descriptor
channel when the deployment threat model includes untrusted `--no-ast-guard` jobs.

### High — unbounded child output could exhaust judge memory

**Location:** `cdap/judge/backends.py:203-235, 293-351` (previous `communicate()` paths).

`communicate()` accumulated all stdout/stderr before `_decode_cap()` truncated it. A tight
print loop could therefore grow the worker/arena process until memory pressure or OOM.

**Fix applied:** both subprocess and Docker backends now drain each pipe in reader threads,
retain at most `MAX_OUTPUT_BYTES`, and discard excess while the child continues. The decoded
capture retains the tail so the final result sentinel remains parseable.

### High — duplicate matches from repeated/concurrent READY

**Location:** `cdap/server.py:846-865`.

`mark_ready()` can report “everyone ready” more than once. Deferred handlers then raced in
`start_room_match()` and could create two matches for one room, leaving an orphan match.

**Fix applied:** match startup atomically verifies that the room object is still registered,
consumes it under the arena lock, and lets only the first deferred action proceed.

### High — SUBMIT could be recorded after match end

**Location:** `cdap/server.py:914-947, 2145-2199`.

The handler validated match state, released the lock, and later created the submission. The
tick thread could end the match in between, allowing a post-deadline submission to be stored
and queued.

**Fix applied:** `create_submission()` revalidates session ownership, match identity, RUNNING
state, and the monotonic deadline while holding the arena lock. A `SubmissionClosed` result is
translated to `410 MATCH_ENDED` or `403 WRONG_STATE` without recording source or starting the
cooldown.

### Medium — malformed framing could desynchronise a TCP session

**Location:** `cdap/protocol.py:396-450`.

EOF while reading headers was indistinguishable from a legal blank line, and invalid
`Content-Length` values were treated as zero. Remaining bytes could then be parsed as a new
frame or a bodyless request.

**Fix applied:** `_read_line()` distinguishes EOF from CRLF; truncated header blocks raise
`ProtocolError`; `Content-Length` is parsed strictly and negative/non-numeric values are
rejected before body processing.

## Remaining findings (not yet fixed)

### High — accepted-session/thread accumulation (resource DoS)

**Location:** `cdap/server.py:2324, 2417` and session accept loop around `2390-2420`.

Two daemon threads are created per accepted TCP session and completed threads remain in the
server’s `_threads` list indefinitely. There is no concurrent-session cap. Repeated connects
can exhaust thread/object memory; many idle sockets also consume two threads until timeout.

**Action:** enforce a semaphore/session limit, reject excess connections, remove completed
threads from a synchronized set, and join/reap them during shutdown.

### Medium — UDP feed endpoint and token lookup growth

**Location:** `cdap/server.py:617-622, 1343-1360, 2328, 2494-2548`.

Every valid token can register arbitrary source addresses, while cleanup is tied to active
snapshot users. Endpoints for disconnected/never-matched users can persist. Token validation
also linearly scans all sessions for every datagram, permitting unauthenticated CPU
amplification.

**Action:** maintain a token-to-session map, cap endpoints per user/token, and remove endpoint
state on session disconnect/expiry.

### Medium — UDP client accepts spoofed display updates

**Location:** `cdap/client.py:132-170`.

The client validates match/sequence fields but not the datagram source or authenticity. An
attacker able to send to the UDP port can inject a high sequence number and suppress real
updates under `LatestWins`, producing an incorrect display (gameplay state remains TCP-only).

**Action:** use a connected UDP socket/source allow-list and authenticate datagrams with a
per-session MAC/token; bound acceptable sequence jumps.

### Medium — historical arena state is unbounded

**Location:** `cdap/server.py:617-622, 954`.

Users, ended matches, and submissions (including up to 256-KB source strings) are retained for
the entire process lifetime. Authenticated clients can grow memory indefinitely.

**Action:** apply bounded/TTL retention for ended matches and submissions, cap registrations,
and persist only data required for `GET_SUBMISSION`.

## Validation

- `py -3.14 -m compileall -q cdap` — passed.
- `py -3.14 -m cdap.selftest_protocol` — all checks passed.
- `py -3.14 -m cdap.problems` — all four catalogue self-checks passed.

No unrelated working-tree changes were modified.
