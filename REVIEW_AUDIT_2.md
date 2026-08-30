# CDAP Principal Engineering Review and Upgrade 2

Date: 2026-08-30

Branch: `develop`
Scope: competitive-judge correctness, complexity policy, protocol recovery, match lifecycle,
resource retention, source-data exposure, and regression coverage.

## Outcome

The competition path now defaults to deterministic hidden performance limits instead of using
empirical Big-O classification as a hard gate. A correct O(n log n) submission is accepted when
it satisfies the advertised CPU/wall/memory budgets. Big-O fitting remains intact behind
`--judge-policy complexity-demo` for coursework experiments and can still produce `606`.

The optimized `solution.py` uses Kadane's algorithm with Θ(n) time and Θ(1) auxiliary space.

## Root cause of the repeat-dependent 606

The old decision tried to infer O(n) versus O(n log n) from six wall-clock points. Across the
available range, their slopes are approximately 1.00 and 1.10, smaller than ordinary process,
cache, garbage-collection, and scheduler variation. Taking minima and fitting relative RMSE
reduces noise but cannot create information absent from the samples. Opcode counting also cannot
see work performed inside C functions such as `list.sort()`, so the second method may classify a
sort-based solution as near-linear.

Observed repeated classification of the same sort-based solution included seven `606` results
and one `611`; a stored earlier run had `611`. Therefore the hard result depended on measurement
conditions, not solely on source and input. Re-submitting could change the result and worker
completion order could additionally change the winner.

The fix changes the authoritative question from “which asymptotic model best fits this noisy
curve?” to “does this exact hidden workload return the oracle result inside explicit limits?”:

1. Each problem declares large deterministic stress sizes and a trusted oracle.
2. Every hidden case loads fresh module state; input/oracle construction is outside the timer.
3. CPU and wall time are both recorded; a ±10% grey band triggers three trials and a median.
4. Auxiliary memory is measured separately at the largest size.
5. Incomplete evidence is `612`, never an optimistic `600`.
6. Verdicts are cached by problem/language/source digest/policy version for process-lifetime
   repeat consistency (bounded to 4,096 records).

## Correctness failures found and repaired

| Area | Failure | Repair |
|---|---|---|
| Judge policy | Close Big-O classes caused false/unstable hard `606` | Default `performance-v1`; fitting is optional demo mode |
| Hidden cases | Fixed small tests did not establish performance-case correctness | Trusted oracle checks value and shape at every hidden size |
| Module state | Reusing one function let global caches/state affect later sizes | Fresh load for each hidden performance case |
| Match winner | Fastest worker response won, even for a later submission | Resolve by `(created_at, submission_id)` and drain earlier pending jobs |
| Deadline race | Match could time out while an accepted job was still judging | `DRAINING` state closes new submits and waits for accepted jobs |
| Lost response | A failed send skipped deferred dispatch and could strand a reserved job | Deferred commit runs even when response delivery fails |
| Retry duplication | Retrying after a lost response could create duplicate state/submissions | Optional per-user `Request-Id` with bounded replay ledger |
| Event loss | Full event outbox discarded lifecycle events like progress | Progress is dropped first; Event-Id gap triggers `GET_STATE` recovery |
| Reconnect ownership | `GET_SUBMISSION` required the original connection ID | Authorization uses authenticated username |
| UTF-8 | Lossy replacement could silently mutate submitted source | Strict decode; `400 INVALID_SOURCE_ENCODING` |
| Forfeit | Removing a player from the participant list prevented complete cleanup/notice | Immutable participants + active set + terminal-notification set |
| UDP attach | Endpoint value was a timestamp but cleanup treated it as session ID | Store authenticated TCP session ID and prune stale feed sequence state |
| Worker liveness | Idle remote worker could remain healthy indefinitely | Eject worker that stops polling for 60 seconds |
| Backend evidence | HELLO could report requested Docker after fallback | Report backend actually instantiated |
| Retained state | Verdict/client history and rate-limit keys could grow | Bounded caches/deque and periodic stale-key pruning |
| Source exposure | Verbose wire logs printed submitted source | Always redact source body and log only length + digest prefix |
| Password default | Client still used `secret` despite the required demo value | Default is `1234`; explicit first-run `--pass` registers that value |

## Edge-case matrix

| Case | Concrete input/action | Expected result |
|---|---|---|
| Empty sequence | `solve([])` | `ValueError`, not index error or fabricated zero |
| One negative | `solve([-7])` | `-7` |
| All negative | `solve([-8,-3,-6,-2,-5,-4])` | `-2` |
| All positive | `solve([1,2,3,4])` | `10` |
| Mixed canonical | `solve([-2,1,-3,4,-1,2,1,-5,4])` | `6` |
| Very large input | one million values | Completes Θ(n), constant auxiliary state |
| Invalid source bytes | SUBMIT body `b"def solve():\n\xff"` | `400 INVALID_SOURCE_ENCODING` |
| Empty source | whitespace-only SUBMIT | `400 BAD_REQUEST` |
| Oversized source | body > 256 KiB | `413 PAYLOAD_TOO_LARGE`, stream remains valid if frame was read |
| Wrong language | `Lang: rust` | `415 UNSUPPORTED_LANGUAGE` |
| Reused Request-Id/content | identical `QUEUE` retry | Original response + `Idempotent-Replay: true` |
| Reused Request-Id/different body | same ID, changed content | `409 IDEMPOTENCY_CONFLICT` |
| Lost SUBMIT response | socket send raises after reservation | Deferred judge dispatch still runs |
| Later accepted finishes first | s-2 accepted while s-1 pending | Match drains; s-1 result decides whether s-2 can win |
| Deadline with pending job | deadline passes during judge execution | New submissions close; accepted job completes before result |
| Full event queue | 256 progress events, then VERDICT | Progress is displaced; VERDICT retained; gap is recoverable |
| Reconnect after verdict | same user, new session, GET_SUBMISSION old ID | Allowed; another user remains `403` |
| Worker dies with lease | disconnect/heartbeat expiry | Job requeued; first authoritative result remains immutable |
| UDP reorder/loss | lower datagram seq after higher | Stale datagram dropped; TCP match correctness unchanged |

These paths are encoded in `cdap.selftest_audit` alongside framing and client self-tests.

## Complexity audit

### Contestant solution (`solution.py`)

| Case | Time | Auxiliary space |
|---|---:|---:|
| Best | Θ(n) | Θ(1) |
| Average | Θ(n) | Θ(1) |
| Worst | Θ(n) | Θ(1) |

The algorithm cannot stop early because any unseen suffix can change the maximum subarray.

### Protocol/server operations

| Operation | Expected complexity |
|---|---|
| Frame parse/encode | O(header bytes + body bytes) |
| Session/submission lookup | O(1) average dictionary lookup |
| Judge queue put/get | O(1) |
| Verdict-cache/idempotency lookup | O(1) average; both bounded |
| Winner resolution | O(s log s) for `s` submissions in one match |
| UDP scoreboard snapshot | O(p) for `p` active players using stored score aggregates |
| GET_STATE | O(S) over retained submissions, returning at most 20 |

Winner resolution runs on verdict completion, not every tick. `GET_STATE` can be indexed by user
if retained history is scaled far beyond the coursework defaults.

## Remaining limitations and next-level roadmap

1. Persist users, request IDs, matches, and verdict digests in SQLite/PostgreSQL so a server
   restart preserves recovery and multi-process replicas share one authority.
2. Replace the global arena lock with clearly owned match/queue repositories only after load
   tests demonstrate contention; the current coarse `RLock` is safer at coursework scale.
3. Use a priority/deque event abstraction instead of accessing `queue.Queue` internals, and add
   reconnect tokens with an explicit grace interval if mid-match network roaming is required.
4. Prebuild a minimal digest-pinned judge image containing only runtime files; reuse warm worker
   containers while resetting namespaces/filesystem state per submission.
5. Randomize hidden values from a server-side reproducible seed and store the seed digest with
   verdict evidence; keep oracle and stress generation versioned.
6. Add property-based frame/state-machine tests, mutation testing for oracles, deterministic fake
   clocks, forced disconnects, and a concurrent multi-worker integration harness.
7. Export metrics for queue depth, judge latency, retry/cache hits, event gaps, deadline drains,
   worker ejections, and verdict distribution; alert on incomplete performance evidence.
8. To support another language, add an isolated language adapter (compile, execute, marshal I/O,
   timeout/memory controls) and advertise it only after the full verdict matrix passes.

## Verification commands

```powershell
py -3 -m cdap.selftest_protocol
py -3 -m cdap.selftest_client
py -3 -m cdap.selftest_audit
py -3 -m cdap.selftest_performance
py -3 -m cdap.problems
git diff --check
```

The audit intentionally does not claim Internet-grade containment for the local subprocess
backend. Network deployment should continue to use the existing Docker/non-loopback policy.
