# CDAP — Code Duel Arena Protocol

A real-time competitive programming arena where the server judges submissions not just for
**correctness**, but for whether they honor a declared **complexity contract**.

Every problem ships with a contract like `required_time: O(n)`, `required_space: O(1)`. The
server runs your submission at six input sizes, measures how the runtime scales, infers the
actual complexity class, and rejects a correct-but-too-slow solution with its own verdict:
`606 TIME_COMPLEXITY_VIOLATION`. On LeetCode, an O(n²) solution that passes the tests is
accepted. Here it is not.

---

## ภาพรวมโครงการ (Thai overview)

โปรเจกต์นี้เป็น **network application** สำหรับวิชา Computer Networks (หัวข้อ Socket
Programming) ประกอบด้วย 3 ส่วนที่ต้องส่ง:

1. **PDF** — ออกแบบ application-layer protocol พร้อมตั้งชื่อ อธิบาย request/response
   messages และเหตุผลในการเลือก service model ของ Transport Layer (TCP หรือ UDP)
2. **Source code** — client และ server ที่พิมพ์ messages และ status code/status phrase
   ทุกตัวที่ส่งและรับ
3. **VDO ≤ 15 นาที** — นำเสนอการออกแบบ อธิบาย code แล้ว demo การรันโปรแกรมหลายรูปแบบ

**ตัว application** คือสนามแข่งเขียนโปรแกรมแบบ real-time: ผู้เล่นถูกจับคู่ (matchmaking)
ได้รับโจทย์อัลกอริทึมข้อเดียวกัน แล้วส่ง code เข้ามา server จะ compile รัน test cases
แล้ว **วัด time complexity และ memory usage จริง** เพื่อตรวจว่าตรงตาม contract ของโจทย์หรือไม่
ถ้าคำตอบถูกแต่ช้าเกินกำหนด จะได้ verdict `606 TIME_COMPLEXITY_VIOLATION`

**ชื่อ protocol: CDAP (Code Duel Arena Protocol) เวอร์ชัน `CDAP/1.0`**

### ทำไมต้องใช้ทั้ง TCP และ UDP

CDAP แบ่ง traffic เป็น 2 ประเภทที่มีความต้องการต่างกันโดยสิ้นเชิง จึงเลือก service model
แยกกันตามประเภทของข้อมูล:

| Traffic | Transport | เหตุผล |
|---|---|---|
| Auth, submit code, verdict, match events | **TCP** | source code ต้องมาถึงครบทุก byte — ถ้าขาดหายจะกลายเป็น compile error ที่โทษผู้เล่นผิด ๆ, verdict หายไม่ได้, และ **ลำดับสำคัญ** (`MATCH_END` มาก่อน `VERDICT` คือ bug) |
| Progress ticks, countdown clock, leaderboard | **UDP** | ส่ง 2–5 ครั้ง/วินาที เป็นข้อมูลแบบ **latest-value-wins** ถ้าหายไปหนึ่งอันจะมีอันใหม่มาแทนใน ~200 ms การ retransmit ข้อมูลเก่าที่ตกยุคแล้ว **แย่กว่า** การทิ้งมันไป |

**หลักการสำคัญ (design invariant):** UDP feed เป็นเพียง *optimization* เท่านั้น ถ้า UDP
ตายทั้งหมด การแข่งยังทำงานถูกต้องครบถ้วนผ่าน TCP เพียงแต่ progress bar จะไม่ update สด ๆ
ข้อนี้คือสิ่งที่ทำให้การแยก transport มีเหตุผลจริง ไม่ใช่แค่ใส่ UDP มาให้ครบ

---

## Quick start

```bash
# Terminal 1 — arena server
python -m cdap.server --tcp-port 5050 --udp-port 5051 -v

# Terminals 2-3 — judge workers (the pool that executes untrusted code)
python -m cdap.judge.worker --arena 127.0.0.1:5050 --id w1
python -m cdap.judge.worker --arena 127.0.0.1:5050 --id w2

# Terminal 4 — player, TCP wire log
python -m cdap.client --host 127.0.0.1 --user alice

# Terminal 5 — same player, UDP live-feed pane only
python -m cdap.client --host 127.0.0.1 --user alice --feed-only

# Terminal 6 — opponent
python -m cdap.client --host 127.0.0.1 --user bob
```

Running the client twice for one player is deliberate: window 4 shows **only**
request/response traffic over TCP, window 5 shows **only** UDP datagrams. Putting the two
transports in separate panes makes the protocol split visible rather than merely claimed.

Requires **Python 3.9+** (3.14 tested). No third-party packages needed. `psutil` and Docker
are optional — see [Sandbox](#sandbox).

Check what your interpreter and machine can actually do before anything else:

```bash
python -m cdap.capabilities
```

That reports which opcode-counting mechanism is usable, whether auxiliary-space measurement and
hard memory limits are available, whether a Docker daemon is reachable, and whether the console
can render the wire log's markers. See [Method B](#method-b--opcode-counting-deterministic-exact)
for why this check is not optional.

Then check the wire layer itself, which starts no servers and needs no ports:

```bash
python -m cdap.selftest_protocol
```

It builds a request, a response, and an event; prints them through the real wire logger;
re-parses each one; verifies a `Body-SHA256` and then breaks it; proves a `606` cannot be used as
a response status; round-trips a UDP datagram through the percent-encoding codec and drops a
stale `seq`; and finally writes **two frames in a single `sendall()`** over a loopback socket to
show the reader finding the boundary in a byte stream that has none of its own.

---

## Architecture

```
 player ──TCP──► ┌────────────────────────┐ ◄──TCP── judge worker 1
 client ◄──TCP── │  Arena Server          │ ◄──TCP── judge worker 2
        ◄──UDP── │  sessions · lobby ·    │ ◄──TCP── judge worker N
                 │  matches · job queue   │
                 └────────────────────────┘
```

Judge workers speak the **same CDAP framing** over their own TCP connection using `WORKER_*`
methods — one protocol serving two audiences.

Workers **long-poll** (`WORKER_PULL` blocks up to 25 s) rather than being pushed to. This
avoids busy polling while keeping the worker as the connection *initiator*, so a worker needs
no inbound ports and can sit behind NAT. The server ejects a worker after 3 missed heartbeats;
with no healthy workers, `SUBMIT` returns `503 JUDGE_UNAVAILABLE` — real backpressure.

---

## Protocol reference — CDAP/1.0

### Wire format (TCP)

Text-based and human-readable on purpose. The assignment requires printing every message, and
readable frames make a far stronger demo than an opaque binary encoding.

```
CDAP/1.0 <START>\r\n
Header-Name: value\r\n
...\r\n
\r\n
<body — exactly Content-Length bytes>
```

### Three message kinds, and how a receiver tells them apart

CDAP is **not** pure request/response — the server pushes unsolicited events. Naive line
protocols break here, so the start line is designed to be unambiguous on the first token:

| Kind | Start line | Detection rule |
|---|---|---|
| Request | `CDAP/1.0 SUBMIT` | else-branch |
| Response | `CDAP/1.0 202 ACCEPTED` | `tokens[1].isdigit()` |
| Event (server push) | `CDAP/1.0 EVENT VERDICT` | `tokens[1] == "EVENT"` |

**Correlation.** Requests carry a monotonic `Seq:`; responses echo it back. Events carry
`Event-Id:` and deliberately **no** `Seq`. So a client's reader thread routes purely on the
frame itself: `Seq` present → hand to the blocked caller; `EVENT` → dispatch to the event
handler. No guessing, no ambiguity.

Common headers: `Seq`, `Session`, `Match`, `Submission`, `Content-Length`, `Content-Type`,
`Body-SHA256`, `Detail`.

### Method catalogue

**Session**

| Method | Body | Success | Errors |
|---|---|---|---|
| `HELLO` | – | `200 OK` + `Server`, `Session` | `426 VERSION_UNSUPPORTED` |
| `REGISTER` | `{user,pass}` | `201 REGISTERED` | `409 USER_EXISTS`, `400 BAD_REQUEST` |
| `LOGIN` | `{user,pass}` | `200 OK` + `Session`, `Token` | `401 AUTH_FAILED` |
| `LOGOUT` | – | `204 NO_CONTENT` | `401 AUTH_FAILED` |

`HELLO`'s body advertises what the arena can do — protocol version, problem list, match
clock, and a `judge` block naming the backend and the opcode counter actually in use — so a
client discovers the arena instead of hard-coding it.

`LOGIN` answers **the same `401 AUTH_FAILED`** for an unknown user and for a wrong password.
Distinguishing them would turn `LOGIN` into a username oracle, and the arena has no reason to
confirm who has an account.

**Lobby / matchmaking**

| Method | Success | Errors |
|---|---|---|
| `QUEUE {mode,difficulty}` | `202 QUEUED` + `Queue-Pos`, `Est-Wait-Ms` | `409 ALREADY_QUEUED` |
| `DEQUEUE` | `200 OK` | `409 NOT_QUEUED` |
| `CREATE_ROOM {problem}` | `201 CREATED` + `Room`, `Capacity` | `404 NOT_FOUND`, `429 RATE_LIMITED` |
| `JOIN_ROOM {room}` | `200 OK` + `Room` | `404 ROOM_NOT_FOUND`, `409 ROOM_FULL` |
| `READY` | `200 OK` | `403 NOT_IN_ROOM` |
| `LEAVE` | `204 NO_CONTENT` | `403 NOT_IN_ROOM` |
| `FORFEIT` | `200 OK` | `403 NOT_IN_MATCH` |

The state table earns its keep here. `QUEUE` and `DEQUEUE` are both *accepted* in the
`QUEUED` state rather than rejected by the state check, because otherwise queueing twice
would collapse into a generic `403 WRONG_STATE` and the specific `409 ALREADY_QUEUED` /
`409 NOT_QUEUED` codes would be unreachable. A status code no request can produce is a status
code that does not exist.

**Problem** — `GET_PROBLEM` → `200 OK` + problem JSON. Errors: `403 NOT_IN_MATCH` (never been
in one), `403 WRONG_STATE` (the countdown is still running — the statement is revealed when the
clock starts, so neither player reads it early). After a match ends the problem is still
served, on purpose: a player reviewing what they just lost to is not cheating.
The body carries the complexity contract:

```json
{
  "id": "max-subarray",
  "title": "Maximum Subarray Sum",
  "entry": "solve",
  "signature": "solve(nums: list[int]) -> int",
  "samples": [{"in": "[-2,1,-3,4,-1,2,1,-5,4]", "out": "6"}],
  "contract": {
    "required_time": "O(n)",
    "required_space": "O(1)",
    "time_limit_ms": 2000,
    "mem_limit_kb": 65536
  },
  "languages": ["python"]
}
```

**Submission** — `SUBMIT` with headers `Match`, `Lang`, `Content-Length`, `Body-SHA256` and the
raw source as the body → `202 ACCEPTED` + `Submission`, `Queue-Pos`. Errors: `403 NOT_IN_MATCH`,
`403 WRONG_STATE`, `410 MATCH_ENDED`, `413 PAYLOAD_TOO_LARGE`, `415 UNSUPPORTED_LANGUAGE`,
`422 BODY_HASH_MISMATCH`, `429 SUBMIT_COOLDOWN`, `503 JUDGE_UNAVAILABLE`.
Also `GET_SUBMISSION {submission}` → `200 OK` + verdict JSON when the judge is done,
`202 ACCEPTED` + `Stage` while it is still running, `404 SUBMISSION_NOT_FOUND`, or
`403 FORBIDDEN` for someone else's submission — **403, not 404**, because pretending another
player's submission does not exist would be a lie the client could detect by ID collision.

Two details in `SUBMIT` that the checks' *order* decides:

- `415 UNSUPPORTED_LANGUAGE` is answered **before** the match checks, so `--lang rust` reaches
  it without needing an opponent — the arena can refuse a language it cannot run whatever the
  match state is.
- On `503 JUDGE_UNAVAILABLE` the submission is **not recorded**, so the submit cooldown never
  starts. Being rate-limited for a request the arena itself could not process would punish the
  player for the arena's problem.

**Debug** — `DEBUG_PANIC` → `500 INTERNAL_ERROR`, and `405 METHOD_NOT_ALLOWED` unless the
server was started with `--allow-panic`. It exists so that `500` is a code the demo can
actually *show* rather than merely list: the handler raises, and the dispatcher's catch-all
turns the exception into a response instead of dropping the connection.

**Worker methods** (arena ↔ judge pool): `WORKER_REGISTER`, `WORKER_PULL` (long-poll → `200 OK`
+ job, or `204 NO_CONTENT`), `WORKER_RESULT`, `WORKER_HEARTBEAT`.

**Server → client events (TCP push):** `MATCH_FOUND`, `MATCH_START`, `ROOM_UPDATE`,
`OPPONENT_SUBMITTED`, `JUDGE_PROGRESS` (stages `QUEUED → COMPILING → TESTING → PROFILING →
DONE`), `VERDICT`, `MATCH_END`, `SERVER_SHUTDOWN`.

### Two status namespaces — a deliberate design decision

CDAP keeps **protocol status** and **judge verdict** in separate numeric namespaces.

A submission returning `606 TIME_COMPLEXITY_VIOLATION` was a protocol *success*: it transferred
intact, the judge ran, and it reached a decision. Folding the two together would make "your
frame was malformed" indistinguishable from "your algorithm is too slow" — a conflation real
HTTP APIs make constantly.

**Protocol status (1xx–5xx)**

| Code | Phrase(s) |
|---|---|
| `200` | `OK` |
| `201` | `CREATED`, `REGISTERED` |
| `202` | `ACCEPTED`, `QUEUED` |
| `204` | `NO_CONTENT` |
| `400` | `BAD_REQUEST` |
| `401` | `AUTH_FAILED` |
| `403` | `FORBIDDEN`, `NOT_IN_MATCH`, `NOT_IN_ROOM` |
| `404` | `NOT_FOUND`, `ROOM_NOT_FOUND` |
| `405` | `METHOD_NOT_ALLOWED` |
| `408` | `REQUEST_TIMEOUT` |
| `409` | `CONFLICT`, `ALREADY_QUEUED`, `NOT_QUEUED`, `ROOM_FULL`, `USER_EXISTS` |
| `410` | `MATCH_ENDED` |
| `413` | `PAYLOAD_TOO_LARGE` |
| `415` | `UNSUPPORTED_LANGUAGE` |
| `422` | `BODY_HASH_MISMATCH` |
| `426` | `VERSION_UNSUPPORTED` |
| `429` | `RATE_LIMITED`, `SUBMIT_COOLDOWN` |
| `500` | `INTERNAL_ERROR` |
| `503` | `JUDGE_UNAVAILABLE` |

The numeric code is the machine-readable class; the phrase names the specific condition. So
`409 ALREADY_QUEUED` and `409 ROOM_FULL` share a code with distinct phrases — the same thing
HTTP reason phrases do.

**Judge verdicts (6xx)**

| Code | Phrase | Meaning |
|---|---|---|
| `600` | `ACCEPTED` | Correct *and* within the complexity contract |
| `601` | `WRONG_ANSWER` | Output mismatch |
| `602` | `TIME_LIMIT_EXCEEDED` | Wall-clock kill |
| `603` | `MEMORY_LIMIT_EXCEEDED` | Peak memory over cap |
| `604` | `COMPILE_ERROR` | Failed to parse/compile |
| `605` | `RUNTIME_ERROR` | Raised an exception |
| `606` | `TIME_COMPLEXITY_VIOLATION` | Correct, but scales worse than the contract |
| `607` | `SPACE_COMPLEXITY_VIOLATION` | Correct, but uses more auxiliary space than allowed |
| `608` | `OUTPUT_FORMAT_ERROR` | Right value, wrong shape |
| `609` | `SANDBOX_VIOLATION` | Attempted a forbidden import or syscall |
| `611` | `INDETERMINATE_COMPLEXITY` | Measurements would not fit any model confidently |
| `612` | `JUDGE_ERROR` | The judge itself failed |

A `VERDICT` event carries the **evidence**, which is what makes the result auditable rather
than an oracle:

```
CDAP/1.0 EVENT VERDICT
Event-Id: 17
Submission: s-8831
Verdict: 606 TIME_COMPLEXITY_VIOLATION
Content-Type: application/json
Content-Length: 412

{ "tests_passed": "10/10",
  "required_time": "O(n)", "inferred_time": "O(n^2)",
  "loglog_slope": 1.98, "rel_rmse": 0.021, "margin": 3.4, "confidence": "high",
  "samples_ms": {"1000":0.9,"2000":3.5,"4000":14.1,"8000":56.7,"16000":225.3},
  "method_b_inferred": "O(n^2)", "methods_disagree": false,
  "peak_aux_kb": 2048, "inferred_space": "O(1)", "backend": "subprocess" }
```

### UDP datagram format

One datagram = one message. **No framing is needed — the datagram boundary *is* the frame**,
a clean contrast to TCP's `Content-Length` requirement.

```
CDAP/1.0 ATTACH session=<token>                                    (client → server)
CDAP/1.0 TICK  match=m-0001 seq=87 t=1724500000123 player=alice passed=7 total=10 subs=2
CDAP/1.0 CLOCK match=m-0001 seq=88 remain=42150
CDAP/1.0 BOARD match=m-0001 seq=89 e=alice:7:2,bob:10:1
```

Values are percent-encoded so they stay space-free. Semantics:

- The server learns a client's UDP address from the **source address of its `ATTACH` datagram**
  — no configuration, and it works through NAT.
- Every datagram carries `seq`. The receiver **discards any datagram with `seq` ≤ the last one
  seen** (stale-drop, latest-wins). Reordering is handled by discarding, not buffering.
- No ACKs and no retransmission, by design.
- The channel is unauthenticated beyond the attach token, so it carries **nothing sensitive and
  nothing state-changing** — display data only. That is a stated security boundary, not an
  oversight.

### State machine (per player connection)

```
INIT ──HELLO──► GREETED ──LOGIN──► IDLE ⇄ QUEUED ──MATCH_START──► IN_MATCH
                                    ▲                                │
                                    └──────── MATCH_END ◄────────────┘

IN_MATCH:  SUBMIT (rate-limited), GET_PROBLEM, UDP feed active
Any state: LOGOUT → CLOSED ; protocol error → response, stay ; fatal → CLOSED
```

A request outside its legal state gets `403 FORBIDDEN` with a `Detail:` header naming the
current state — which makes the state machine itself demonstrable.

---

## The complexity profiler

Two independent methods, deliberately, because comparing them *is* the project's research
contribution.

### Method A — wall-clock regression (statistical, general)

Runs at `n = [1000, 2000, 4000, 8000, 16000, 32000]` — six doublings. At each `n`, 5 repeats
and **take the minimum**, not the mean: timing noise is one-sided, so the minimum is the
cleanest estimator. One warm-up call is discarded, and only the solution function is timed,
never process startup.

### Method B — opcode counting (deterministic, exact)

Counts the bytecode instructions a solution actually executes, instead of timing it. Because
the count is **deterministic**, one run per input size suffices — no repeats, no noise.

Two mechanisms can do this, and `cdap/capabilities.py` probes both at startup rather than
assuming either works:

- **`sys.monitoring` `INSTRUCTION` events** (PEP 669, Python 3.12+) — the one actually used.
- **`sys.settrace` + `frame.f_trace_opcodes`** — the historical approach, kept as a fallback.

**Measured finding on CPython 3.14.3 (Windows 11):** `sys.settrace` opcode tracing is
**silently inert** — it counts *zero* opcodes rather than raising. That failure mode is
dangerous in exactly this application: zero counts at every input size fit `O(1)` perfectly,
so Method B would have confidently reported every submission as constant-time and accepted
every too-slow solution. The probe catches it by requiring a counter to produce counts that
are non-zero, reproducible, **and scale with the input** before it will be used.
`sys.monitoring` passed all three (ratio 1.98 on a linear workload, ~31× overhead), so it is
selected. This is why the capability probe is Phase 1 and not an afterthought.

Tracing overhead means Method B's input sizes shrink to `[500, 1000, 2000, 4000]`.

### Shared model fitter

```
models = [O(1), O(log n), O(n), O(n log n), O(n²), O(n³), O(2ⁿ)]

for each candidate f:
    c        = Σ(y·f) / Σ(f²)                    # least squares through the origin
    rel_rmse = sqrt(mean(((y − c·f) / y)²))      # scale-free, comparable across models

rank ascending by rel_rmse
margin       = rmse[1] / rmse[0]
loglog_slope = least-squares slope of (log n, log y)     # reported as a sanity check
```

Written by hand in pure stdlib — no numpy — so the math stays visible in the source and can be
explained aloud.

**Decision policy.** `margin ≥ 1.15` → confident. `margin < 1.15` → ambiguous, so report the
**cheaper** of the two tied classes and mark `confidence: low`. `best rel_rmse > 0.35` →
`611 INDETERMINATE_COMPLEXITY`.

When ambiguous, the profiler **favours the contestant**. A false `606` accuses someone of
having written a worse algorithm than they actually did; a false accept merely lets a borderline
solution through. Asymmetric costs justify an asymmetric policy.

**Combining the two.** Method A is authoritative, because the contract is about real time. When
the two disagree the verdict reports both and sets `methods_disagree: true`.

**Space.** `tracemalloc`: snapshot after building the input, `reset_peak()`, run, then
`aux = peak − before`. Subtracting the input's own footprint is what makes it *auxiliary* space
— without that, `O(1)` space would be unmeasurable, since the input alone is `O(n)`.

**Contract check.** Classes rank `O(1) < O(log n) < O(n) < O(n log n) < O(n²) < O(n³) < O(2ⁿ)`;
a submission passes iff `rank(inferred) ≤ rank(required)`.

### Known limitations — stated, not hidden

These are documented deliberately. Two of them are findings, not defects:

1. **O(n) vs O(n log n) cannot be reliably separated** at these input sizes — the log-log
   slopes are 1.00 vs ~1.10, well inside measurement noise. The profiler says so via
   `confidence: low` rather than pretending to a precision it does not have.
2. **Method B is blind to C-implemented builtins.** Opcode counting only sees Python-level
   bytecode, so work inside `list.sort()`, `sum()`, or `str.join()` registers as a single
   `CALL`. A Timsort-based O(n log n) solution therefore looks near-linear under Method B.
   `samples/has_duplicate_onlogn.py` exists specifically to exhibit this, and the confusion
   matrix experiment measures it.
3. **Worst-case inputs are the problem author's responsibility.** A quicksort measures
   O(n log n) on random data and O(n²) on sorted data, so every problem ships an *adversarial*
   generator. Without one, the profiler measures the wrong thing.

---

## Sandbox

The judge executes untrusted submitted code, so this is a real security control. Two
interchangeable backends sit behind one interface, selected with `--backend`:

**Shared layers.** AST static guard (import whitelist; rejects `__import__`, `eval`, `exec`,
`compile`, `open`, `input`, `globals`, `locals`, `vars`, dunder attribute access) → wall-clock
hard kill → 64 KB output cap → result-channel integrity (the child emits a `__CDAP_RESULT__`
sentinel before its JSON, so a solution's stray `print()` cannot forge a verdict).

**`subprocess`** (default) — fresh temp cwd, stdin closed, no inherited handles.
`resource.setrlimit(RLIMIT_AS)` in-child on POSIX; on Windows, in-child `tracemalloc` threshold
plus optional `psutil` polling. No setup, no latency, runs anywhere.

**`docker`** (optional) — `--network none` for a **kernel-level** network block, `--memory` for
a real cgroup cap, `--pids-limit` as a genuine fork-bomb defence, `--read-only` rootfs.
Costs ~0.5–1.5 s container startup per submission.

**Honest limitations:** the AST guard is **defence-in-depth, not a security boundary** — it is
bypassable in principle. **Windows has no `setrlimit` and no cgroups**, so under the
`subprocess` backend memory limits are best-effort. Under `subprocess`, CPU exhaustion is
bounded only by the wall-clock kill. See `docs/threat-model.md`.

Rather than merely asserting the guard is weak, `experiments/backend_overhead.py` **proves it**
— see below.

---

## Experiments

Two scripts produce the report's quantitative results.

**`experiments/confusion_matrix.py`** — runs every known-complexity reference solution in
`samples/` through both profiler methods and prints true-vs-inferred. Method A is expected
≥ 90% on the polynomial cases. Method B should be exact on pure-Python cases **and
demonstrably wrong on the sort case** — the experiment asserts that contrast rather than
hiding it, because it is the most interesting thing the comparison reveals.

**`experiments/backend_overhead.py`** — reports per-submission latency and throughput for both
backends, then re-runs `samples/evil_*.py` under each **with the AST guard deliberately
disabled** (`--no-ast-guard`). Under `subprocess`, `evil_socket.py` opens a connection and
`evil_fork.py` spawns freely — the guard was the only thing stopping them. Under `docker`, both
still fail. That is direct empirical proof of **which layer is the actual security boundary**,
which is a much stronger claim than prose.

---

## Project layout

```
cdap/status.py                  the two code tables + phrases
cdap/protocol.py                Message, framing, Connection, UDP codec, wire logger
cdap/problems.py                Problem dataclass + 4 problems w/ adversarial generators
cdap/server.py                  arena: TCP, UDP feed, matchmaking, dispatcher
cdap/client.py                  player CLI; --feed-only renders the UDP pane
cdap/judge/worker.py            worker process, long-poll loop
cdap/judge/backends.py          Backend interface + SubprocessBackend + DockerBackend
cdap/judge/sandbox.py           AST guard (shared by both backends)
cdap/judge/runner.py            in-child harness: correctness / time / ops / space
cdap/judge/profiler.py          model fitter + decision policy
samples/                        known-complexity reference solutions + evil_*.py
experiments/                    the two result-producing scripts
docs/CDAP-protocol-spec.md      → export to PDF (deliverable 1)
docs/threat-model.md            sandbox threat model + stated limitations
docs/slides-outline.md          timings for the ≤15 min video (deliverable 3)
```

Four problems, each with an adversarial generator: `max-subarray` (Kadane, O(n)/O(1)),
`two-sum-sorted` (two-pointer), `has-duplicate` (set vs sort — the Method B blind spot),
`fib` (DP vs naive recursion, for a visible O(2ⁿ) detection).

---

## Project status

Built in phases, each ending in a demoable result and its own commit.

| # | Phase | Status |
|---|---|---|
| 0 | Repo bootstrap, README, CLAUDE.md | ✅ done |
| 1 | Python 3.14 capability probe (`f_trace_opcodes`, `tracemalloc.reset_peak`) | ✅ done |
| 2 | `status.py`, `protocol.py` — framing + wire logging | ✅ done |
| 3 | `problems.py`, `runner.py`, `sandbox.py`, subprocess backend | ✅ done |
| 4 | `profiler.py` — the model fitter | ⏳ next |
| 5 | `server.py` TCP path, `client.py` — a full duel | ⬜ |
| 6 | `worker.py` + dispatcher — the judge pool | ⬜ |
| 7 | UDP feed, stale-drop, `--feed-only`, `--no-udp` | ⬜ |
| 8 | `DockerBackend` | ⬜ |
| 9 | Both experiments, three bilingual docs | ⬜ |

**What runs today:** the capability probe (`python -m cdap.capabilities`), the wire layer
self-test (`python -m cdap.selftest_protocol`), which frames all three message kinds, verifies
`Body-SHA256`, refuses to mix the two status namespaces, round-trips the UDP codec, and carries
two back-to-back frames over a real loopback socket — and, as of Phase 3, the judge itself:

```bash
python -m cdap.problems                                     # problem catalogue self-check
python -m cdap.judge.sandbox samples/evil_socket.py          # AST guard verdict for one file
python -m cdap.judge.backends samples/max_subarray_on2.py    # run + measure one submission
python -m cdap.judge.backends samples/fib_naive.py fib       # ...against a chosen problem
```

`cdap.judge.backends` is the end-to-end judge minus the decision: it runs a submission in a
child process behind the guard, the wall-clock kill, the output cap and the sentinel result
channel, then prints the raw measurement record — tests passed, per-size timings, opcode
counts, peak auxiliary memory. Turning that record into a `6xx` verdict is Phase 4's job, so
the classes named in `samples/` are measured but not yet judged.

`samples/` covers the verdict matrix, each file's docstring naming its expected verdict:
`600` `max_subarray_on.py` · `601` `max_subarray_wrong.py` · `602` `max_subarray_busy_loop.py` ·
`603` `max_subarray_memory.py` · `604` `max_subarray_syntax_error.py` ·
`605` `max_subarray_runtime_error.py` · `606` `max_subarray_on2.py`, `fib_naive.py` ·
`607` `max_subarray_on_space.py` · `608` `has_duplicate_int_return.py` ·
`609` `evil_socket.py`, `evil_open.py`, `evil_fork.py` · `611` `fib_logn.py`. Plus
`forge_result.py`, which prints a fake `__CDAP_RESULT__` line and is ignored because the real
one is always last, and `has_duplicate_onlogn.py`, which exists to *fail* Method B.

Everything past Phase 3 is still design only — the sections above describe what the following
phases implement, and this table is the honest source of truth for what works.

---

## Assignment deliverables

| Deliverable | Artifact |
|---|---|
| PDF — protocol design + answers | `docs/CDAP-protocol-spec.md` + `docs/threat-model.md` |
| Source code | this repository |
| VDO ≤ 15 min | `docs/slides-outline.md` (script + timings + demo matrices) |

Course: Computer Networks — Project 1, Socket Programming.
