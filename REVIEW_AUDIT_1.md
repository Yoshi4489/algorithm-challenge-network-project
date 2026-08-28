# CDAP Full Code Audit 1

Date: 2026-08-28

Scope: static review of all production Python modules in `cdap/` and `cdap/judge/`, plus the
client UI, protocol framing, server/worker path, backends, profiler, and supplied CLI defaults.
Sample programs and experiments were inspected only where they affect a production security
claim. No code was modified during this audit.

## Remediation update (2026-08-28)

The following changes were implemented after this audit and are covered by
`py -3.14 -m cdap.selftest_audit` in addition to the existing protocol/client/problem tests.

| Finding | Remediation status | Implemented control |
|---|---|---|
| A-01 | Fixed | Empty worker token now returns `503 WORKERS_DISABLED`; remote workers require a configured token. |
| A-02, A-04, A-05 | Fixed | Wire logs redact credentials and escape C0/C1 controls; credentials are bounded, salted-scrypt verifiers; registration/login attempts are per-peer throttled; remote binds require explicit insecure Docker opt-in. |
| A-03 | Fixed for remote operation | Non-loopback operation requires Docker; the loopback subprocess mode remains explicitly a trusted local-demo mode. |
| A-06 | Fixed | Event-id allocation and outbox enqueue/drop happen under one session lock. |
| A-07, A-08, A-10, A-14 | Fixed | Match-specific auto-actions are cancelled after match end, queue state is shared, verdict blocks are atomic, UDP warnings are rate-limited, and finished-match view state is cleared. |
| A-09 | Fixed | Time/space blocks now report completeness; an incomplete required measurement receives `611` or `612`, never `600`. |
| A-11, A-12 | Fixed | Match score aggregates make feed snapshots O(players); submissions reserve a bounded pending-job slot before source is retained and overload returns `503 JUDGE_QUEUE_FULL`. |
| A-13, A-15, A-16 | Deferred | Per-submission randomized hidden generators, minimal digest-pinned Docker bundles, and connection-core extraction are follow-up hardening/refactoring work; they do not weaken the repaired authentication, local-only, backpressure, or verdict gates above. |

## Executive summary

The framing, request correlation, bounded TCP bodies, job leases, and Docker hardening are
thoughtfully designed. The project is not safe to expose as an Internet-facing service in its
current default configuration, however. The most serious flaw is that remote worker
authentication accepts an empty default token. A remote peer can then register as a worker,
obtain source code, and submit arbitrary verdicts. Normal wire logging also prints passwords,
UDP session tokens, and unescaped attacker-controlled text.

There are also correctness issues in event ordering, automatic submission, UI state, and space
profiling. Built-in compilation and self-tests pass, but they do not cover these concurrency,
authentication, or adversarial-input paths.

## Validation performed

- Compiled every production module with `py -3.14 -m py_compile`.
- Ran `py -3.14 -m cdap.selftest_protocol`.
- Ran `py -3.14 -m cdap.selftest_client`.
- Ran `py -3.14 -m cdap.problems`.

All commands passed. They establish baseline behavior only; they do not invalidate the findings
below.

## Findings

| ID | Severity | Area | Evidence | Impact and actionable fix |
|---|---|---|---|---|
| A-01 | Critical | Authentication | `cdap/server.py:1780` compares the supplied worker token with `Arena.worker_token`; the CLI default is the empty string at `cdap/server.py:2785`. `WORKER_*` methods explicitly do not require player authentication. | Any peer that can reach the TCP port can send an empty worker token, register a worker, pull player source from the job queue, and return a forged `600` or `612` verdict. Reject all `WORKER_*` methods when no non-empty worker token is configured. Prefer a generated token or mutual TLS; keep an explicit `--allow-insecure-workers` opt-in only for a local demo. Add an integration test proving empty-token registration is rejected. |
| A-02 | High | Secret exposure / transport | Server logging is enabled by default (`cdap/server.py:2847`). `WireLog` prints all headers and body previews at `cdap/protocol.py:722-755`; REGISTER/LOGIN bodies contain `pass`, while LOGIN replies contain the `Token` header (`cdap/server.py:2014-2018`). The protocol is plain TCP. | Passwords and UDP bearer tokens appear in terminal history, screen recordings, redirected logs, and can be read/modified by a network attacker outside loopback. Redact `pass`, `Token`, `Worker-Token`, and authorization-like headers/body fields before logging. Use TLS for any non-loopback deployment, or reject non-loopback binds without an explicit insecure-development switch. Store passwords as salted password hashes rather than plaintext (`cdap/server.py:743-775`). |
| A-03 | High | Sandbox boundary | The default backend is `subprocess` (`cdap/server.py:2783-2784`), which executes submitted Python after a documented bypassable AST guard (`cdap/judge/sandbox.py:15-31`, `cdap/judge/runner.py:149-175`). | A determined submission can escape the static guard and execute with the account permissions of the arena process. On Windows, hard resource limits are also unavailable under this backend. Default to Docker when judging untrusted remote users; otherwise bind only to loopback and present a startup warning that the subprocess backend is not a containment boundary. |
| A-04 | High | Terminal injection | `WireLog._header_summary` and `_body_line` send header/body text directly to the terminal (`cdap/protocol.py:734-755`). Newlines are escaped, but ESC/OSC and other control characters are retained. Submission source is attacker-controlled and is logged by the server. | A source comment or string containing terminal control sequences can clear/alter the terminal, create deceptive log output, or exploit terminal features such as clipboard-setting sequences. Escape all C0/C1 controls, especially `ESC`, before writing logs; use a safe visible representation such as `\\x1b`. Redact secrets before this sanitization. |
| A-05 | High | Input/memory DoS | Password length is not bounded in `_credentials` (`cdap/server.py:1792-1815`) and stored verbatim (`cdap/server.py:743-751`). Accepted request bodies are up to 256 KiB and up to 1,024 users may be retained by default. | An attacker can register many accounts with near-maximum passwords and retain hundreds of MiB of password text, before other state and allocator overhead. Set conservative maximum username/password lengths, hash passwords, and apply per-IP registration/login throttles. |
| A-06 | Medium | Event ordering race | `Session.push_event` allocates `Event-Id` under `self._lock` but releases the lock before putting into `outbox` (`cdap/server.py:317-352`). Two producer threads can enqueue event 2 before event 1. | Progress, verdict, match-end, and room events can arrive in an order different from their event IDs and causal order. Queue the message while holding the same lock, or use an event-dispatcher thread/monotonic sequence gate. Add a concurrent producer test that asserts FIFO event IDs on the wire. |
| A-07 | Medium | Client correctness | `MATCH_START` schedules an asynchronous problem fetch (`cdap/client.py:643-664`). `_fetch_problem_then_submit` submits even when GET_PROBLEM failed (`cdap/client.py:666-673`), and does not verify the match is still active. GET_PROBLEM deliberately serves the last completed match (`cdap/server.py:2205-2223`). | If a match ends before the agent action runs, the client can show a stale problem after MATCH_END and attempt a rejected submission. In compact mode those failed responses are not shown, so `--once` may appear hung. Capture the match ID at scheduling time; skip the action unless that exact match remains active; print unsuccessful GET_PROBLEM/SUBMIT status and detail. |
| A-08 | Medium | Client UI state | The interactive `queue` command sets `client.queued` (`cdap/client.py:911-915`), but the `--queue` path only logs its response (`cdap/client.py:1178-1180`). | A client launched with `--queue` presents an `[idle]` prompt while it is actually queued, which makes the state model misleading. Share one queue-success handler and set `client.queued = True` in both paths. |
| A-09 | Medium | Judge correctness | `measure_space` records an exception as a note and returns partial samples (`cdap/judge/runner.py:618-636`). `judge_record` can still return `600 ACCEPTED` when space inference is absent (`cdap/judge/profiler.py:553-590`). | A tailored solution that passes fixed correctness tests but raises on a large space-profile input can avoid space-contract evaluation and still be accepted. Include explicit `complete`/`failed` status in each measurement block; return `612 JUDGE_ERROR` or `611 INDETERMINATE_COMPLEXITY` when required space measurement is incomplete. Add a regression sample that fails only at a large profiling size. |
| A-10 | Medium | UI concurrency | `PlayerView` has its own print lock (`cdap/client.py:103-117`), but verdict output uses direct `print()` calls (`cdap/client.py:697-737`) and `WireLog` uses a separate lock (`cdap/protocol.py:819-823`). Background countdown/UDP messages can also appear while `input()` is reading. | Lines can interleave and asynchronous messages can split the prompt or a command the player is typing. Route every client-visible line through one renderer and redraw the stateful prompt after background notices. A small terminal abstraction is sufficient; no full-screen UI dependency is required. |
| A-11 | Medium | Performance / memory | `feed_snapshots` loops over every player and scans every match submission to calculate each player's score (`cdap/server.py:1452-1486`). It runs every 250 ms from the tick loop. | With many matches/submissions, this is O(players × submissions-per-match) four times per second, before UDP send/log costs. Maintain per-match/per-player aggregate passed/submission counters when a submission is created or finalized, then build each snapshot in O(players). |
| A-12 | Medium | Queue/backpressure | `JobQueue` is unbounded (`cdap/server.py:461-472`). The global retained-submission cap bounds it eventually, but the default permits 2,048 source records, each up to 256 KiB (`cdap/server.py:1649-1652`, `cdap/server.py:2814-2815`). | A healthy but slow judge can accumulate hundreds of megabytes of source and measurement state. Add a maximum pending-job limit separate from retained history and answer a documented overload status before accepting additional submissions. Export queue depth and reject/slow producers at a threshold. |
| A-13 | Medium | Fairness / edge cases | Test cases and profiling generators are local deterministic code (`cdap/problems.py`), while a submitted function is reused for every correctness and profiling call (`cdap/judge/runner.py:681-729`). | A contestant can special-case known inputs/sizes, cache earlier calls, or fail selectively outside visible tests to pass an intended complexity contract. Randomize hidden test values and profiling ladders per submission from a server-side seed; execute fresh function state for independent measurement repetitions where reproducibility permits. |
| A-14 | Low | UDP/UI robustness | Each stale or malformed feed packet produces a user-visible warning (`cdap/client.py:334-374`); compact view state dictionaries are never cleared after a match (`cdap/client.py:108-110`, `cdap/client.py:648-651`). | Reordered/bad packet bursts can recreate terminal spam, and a client used for many matches retains old score/timer state indefinitely. Rate-limit warnings with a suppression counter and clear/bound view state at MATCH_END. |
| A-15 | Low | Docker supply chain / data minimization | Docker uses the mutable `python:3.14-slim` tag and mounts the entire repository read-only (`cdap/judge/backends.py:250-255`, `cdap/judge/backends.py:296-318`). | A later image tag can change behavior, and a sandbox escape can read every repository file, including accidental secrets or `.git` metadata. Pin the image by digest and copy only the required `cdap` runtime files into a minimal judge image; do not bind-mount the project checkout. |
| A-16 | Low | Maintainability | Client and worker duplicate independent request sequencing, pending-response routing, reader loops, and shutdown code (`cdap/client.py:402-753`, `cdap/judge/worker.py:174-420`). The server has several thousand lines of state, transport, handler, and CLI logic in one module. | Parallel fixes can drift, as compact-mode behavior already differs from worker wire behavior. Extract a tested reusable request-router/connection-session component and split server state, handlers, feed, and CLI into modules. Preserve the protocol-specific invariants in focused unit tests. |

## Positive controls observed

- TCP framing bounds header count/size and body size before allocation (`cdap/protocol.py:396-464`).
- Per-socket send serialization prevents byte-level response/event interleaving (`cdap/protocol.py:471-509`).
- Submission creation rechecks match state under the arena lock, closing the deadline race (`cdap/server.py:981-1005`, `cdap/server.py:2299-2309`).
- Worker leases and first-verdict-wins logic reduce duplicate remote execution damage (`cdap/server.py:1085-1143`, `cdap/server.py:1201-1287`).
- Docker runs use no network, dropped capabilities, a read-only root filesystem, PID/memory limits, and an unprivileged user (`cdap/judge/backends.py:299-318`).
- Compact UI tests demonstrate that unchanged UDP snapshots no longer scroll the player terminal.

## Recommended remediation order

1. Fix A-01 before enabling remote workers anywhere outside a fully trusted local network.
2. Fix A-02 and A-04 together: redact secrets and sanitize terminal output; add TLS/non-loopback policy.
3. Make Docker the only supported backend for untrusted networked play, or explicitly restrict subprocess mode to loopback (A-03).
4. Fix event ordering and automatic submission state handling (A-06 through A-10).
5. Add queue limits, snapshot aggregates, randomized hidden tests, and measurement-completeness handling (A-09, A-11, A-12, A-13).
6. Refactor duplicated connection code only after behavior is protected by new concurrency and integration tests (A-16).

## Required regression tests

- Empty worker token: `WORKER_REGISTER` must return an authentication/configuration error.
- Redaction: REGISTER, LOGIN, and worker request logs must not contain password, token, or control characters.
- Event ordering: two concurrent producers must result in strictly increasing Event-Id delivery.
- Match-end race: MATCH_END between MATCH_START and agent fetch must not show a stale problem or submit source.
- `--queue`: prompt must become `[queued]` after a successful response.
- Space-profile failure: a solution failing only on a large profiling size must not receive `600 ACCEPTED`.
- UDP flood: thousands of stale datagrams must produce a bounded warning summary.
- Backpressure: submissions above the pending-job threshold must be refused without growing retained source memory.
