# CLAUDE.md — CDAP project brief

Read this before changing anything in this repository. Several things here look like bugs and
are not: they are documented findings that the coursework report depends on. The
["Do not fix"](#deliberate-limitations--do-not-fix-these) section is the important one.

---

## What this project is

**CDAP — Code Duel Arena Protocol** (`CDAP/1.0`), a real-time competitive-programming arena.
Players are matched, receive the same algorithm problem, and submit source code. The arena
server dispatches each submission to a judge worker, which runs it against test cases and then
**empirically measures its time and space complexity** to check it against the problem's
declared contract (`required_time: O(n)`, `required_space: O(1)`).

The distinguishing idea: a **correct but too-slow** solution is rejected with its own verdict,
`606 TIME_COMPLEXITY_VIOLATION`. On LeetCode an O(n²) solution that passes the tests is
accepted. Here it is not.

## Objective — this is coursework, and that shapes every tradeoff

Computer Networks, **Project 1: Socket Programming**. Three graded deliverables:

1. **PDF** — propose a network application: its purpose, characteristics, and which Transport
   Layer service model it needs (TCP or UDP) **and why**. Then design an application-layer
   protocol with request/response messages, and **give the protocol a name**.
2. **Source code** — client and server implementing that protocol. The assignment explicitly
   requires that the programs **print the messages and the status (status code, status phrase)
   they send and receive**.
3. **VDO clip ≤ 15 minutes** — present the design via slides, explain the code, then demo the
   program **being run and tested in various forms**, with the student on camera for part of it.

The consequence worth internalizing: **this code is going to be read aloud and explained on
video by a student.** Clever-but-opaque is a defect here even when it is faster or shorter.
Optimize for explainability.

---

## Hard constraints

- **Print every message and every status code + phrase**, on both sides, for both transports.
  This is a graded requirement, not a debugging aid. Do not gate the wire log behind a
  verbosity flag that defaults to off. `-v` may add *more* detail; it must not be required for
  the baseline log.
- **Standard library only.** `psutil` and Docker are optional extras and the program must run
  correctly without either. **No numpy** — in particular the regression math in
  `judge/profiler.py` stays hand-written so it is visible in the source and can be explained.
- **The code must be explainable aloud.** Favour a clear loop over a dense comprehension in the
  parts that get presented (framing, the model fitter, the state machine).
- **Documents are bilingual** — Thai prose with English technical terms inline, in `docs/` and
  in the README's overview. **Code, comments, and identifiers stay English.**
- **Python 3.9+**, developed on 3.14, on **Windows**. Windows is the primary dev platform, which
  is why `resource.setrlimit` cannot be relied on — see below.

---

## Design invariants — do not break these

1. **The UDP feed is strictly an optimization.** If UDP is entirely dead, the match must still
   complete correctly over TCP; only the live progress display degrades. This invariant is what
   makes the dual-transport design *defensible* rather than decorative, and `--no-udp` exists to
   demonstrate it. Never move state-changing or must-arrive data onto UDP.
2. **The two status namespaces stay separate.** Protocol status is 1xx–5xx; judge verdicts are
   6xx. A `606 TIME_COMPLEXITY_VIOLATION` is a protocol *success* — the frame arrived, the judge
   ran, a decision was reached. Never merge the tables, and never return a 6xx code as a
   response status or a 4xx code as a verdict.
3. **The profiler favours the contestant when ambiguous.** When the top two complexity models
   are within `margin < 1.15`, report the **cheaper** class and mark `confidence: low`. A false
   `606` accuses someone of writing a worse algorithm than they did; a false accept merely lets
   a borderline solution through. The costs are asymmetric, so the policy is too.
4. **Events carry no `Seq`; responses always echo the request's `Seq`.** This is what lets a
   client's reader thread route frames with no ambiguity. Adding a `Seq` to an event would break
   correlation.
5. **The UDP channel carries display data only**, and is unauthenticated beyond the attach
   token. That is a stated security boundary in the report. Do not put anything sensitive or
   state-changing there.
6. **`Body-SHA256` is verified**, and a mismatch is `422 BODY_HASH_MISMATCH`. The client's
   `--tamper` flag exists to trigger it on camera.

---

## Deliberate limitations — do NOT "fix" these

Each of these is documented in the README and `docs/threat-model.md`, and each is either a
report finding or a demo beat. Silently "improving" them damages the deliverable.

1. **The AST guard is defence-in-depth, NOT a security boundary.** It is bypassable in
   principle, and `experiments/backend_overhead.py` **proves** it by running `samples/evil_*.py`
   with `--no-ast-guard` under both backends: the escapes succeed under `subprocess` and fail
   under `docker`. Do not delete `--no-ast-guard`, and do not rewrite the guard to claim it is a
   real boundary. The container is the boundary; the guard is a cheap first filter.
2. **O(n) vs O(n log n) cannot be reliably separated** at these input sizes — log-log slopes of
   1.00 vs ~1.10 sit inside measurement noise. The profiler reports `confidence: low` instead of
   faking a precision it does not have. Do not add heuristics to force a confident answer here.
3. **Method B (opcode counting) is blind to C-implemented builtins.** Opcode counting only sees
   Python-level bytecode, so work inside `list.sort()`, `sum()`, or `str.join()` counts as a
   single `CALL`, and a Timsort-based O(n log n) solution looks near-linear.
   **`samples/has_duplicate_onlogn.py` exists specifically to exhibit this**, and
   `experiments/confusion_matrix.py` asserts the wrong answer rather than hiding it. This is the
   most interesting result in the report. Do not remove that sample, and do not patch Method B
   to special-case builtins.
4. **Windows has no `setrlimit` and no cgroups**, so under the `subprocess` backend memory
   limits are best-effort (in-child `tracemalloc` threshold, optionally `psutil` polling). This
   is named openly as the biggest implementation weakness rather than glossed over. The Docker
   backend is where real limits come from.
5. **Under `subprocess`, CPU exhaustion is bounded only by the wall-clock kill.** That is
   acknowledged, not solved.
6. **If Docker is unavailable the judge falls back to `subprocess` and says so in the verdict's
   `backend` field.** It must never fail silently, and it must never *claim* `docker` when it
   actually ran `subprocess` — the experiment's conclusions depend on that field being truthful.

---

## Layout

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
experiments/confusion_matrix.py Method A vs B  → the complexity-inference result
experiments/backend_overhead.py subprocess vs docker → the security + latency result
docs/CDAP-protocol-spec.md      → export to PDF (deliverable 1), bilingual
docs/threat-model.md            sandbox threat model + stated limitations, bilingual
docs/slides-outline.md          script + timings for the ≤15 min video (deliverable 3)
```

**Problems**, each with an adversarial generator (a quicksort measures O(n log n) on random data
and O(n²) on sorted data — without worst-case inputs the profiler measures the wrong thing):
`max-subarray` (Kadane), `two-sum-sorted` (two-pointer), `has-duplicate` (set vs sort — the
Method B blind spot), `fib` (DP vs naive recursion, for a visible O(2ⁿ)).

---

## Running it

```bash
python -m cdap.server --tcp-port 5050 --udp-port 5051 -v
python -m cdap.judge.worker --arena 127.0.0.1:5050 --id w1
python -m cdap.judge.worker --arena 127.0.0.1:5050 --id w2
python -m cdap.client --host 127.0.0.1 --user alice                 # TCP wire log
python -m cdap.client --host 127.0.0.1 --user alice --feed-only     # UDP feed pane
python -m cdap.client --host 127.0.0.1 --user bob
```

Two panes for one player is deliberate: TCP request/response in one window, UDP datagrams in
the other, so the transport split is *visible* on camera rather than merely claimed.

**Judge a single file:** `python -m cdap.judge.profiler samples/max_subarray_on2.py`

**Experiments:** `python -m experiments.confusion_matrix`,
`python -m experiments.backend_overhead`

**Demo flags that must keep working** — each one is a beat in the video:
`--bad-version` (→ `426`), `--tamper` (→ `422`), `--lang rust` (→ `415`), `--no-udp`
(TCP-only correctness), `--udp-loss 0.4` (convergence under loss), `--backend docker`,
`--no-ast-guard` (the security experiment).

---

## Logging conventions

The wire log is a deliverable, so keep the format stable and greppable:

```
[TCP →] CDAP/1.0 SUBMIT  Seq=7 Match=m-0001 Lang=python Content-Length=412
[TCP ←] CDAP/1.0 202 ACCEPTED  Seq=7 Submission=s-8831 Queue-Pos=1
[EVENT] CDAP/1.0 EVENT VERDICT  Event-Id=17 Verdict=606 TIME_COMPLEXITY_VIOLATION
[UDP ←] TICK match=m-0001 seq=87 player=alice passed=7/10
[UDP ✗] stale seq=86 ≤ 87, dropped
```

Direction marker, then transport, then the frame. Status codes appear **with their phrases**
everywhere — `202 ACCEPTED`, never a bare `202`. Bodies are truncated in the log but the length
is always shown.

---

## Build order

Phases, each ending in a working demo and its own commit, so the git history mirrors the
report. Current position is tracked in the README's **Project status** table — update it as
phases land.

0. Repo bootstrap, README, CLAUDE.md ✅
1. Python 3.14 capability probe (`cdap/capabilities.py`) ✅ — **finding:** `sys.settrace` +
   `f_trace_opcodes` is *silently inert* on CPython 3.14.3 (counts zero opcodes), so Method B
   uses `sys.monitoring` (PEP 669 `INSTRUCTION` events), which the probe verified counts,
   is deterministic, and scales (ratio 1.98 on a linear workload, ~31× overhead). The probe
   also found no `resource.setrlimit` and no Docker daemon on this box (both expected on
   Windows, both degrade gracefully), and that the console needs `sys.stdout.reconfigure`
   to UTF-8 for the wire log's `→ ← ✗` markers — `enable_utf8_output()` handles it, with an
   ASCII fallback if reconfigure fails.
2. `status.py`, `protocol.py` — framing + the wire logger ✅
3. `problems.py`, `judge/runner.py`, `judge/sandbox.py`, `judge/backends.py` (subprocess) ✅
4. `judge/profiler.py` — the model fitter ✅
5. `server.py` TCP path + `client.py` — a full duel, every protocol status reachable ✅
6. `judge/worker.py` + dispatcher — the pool, health ejection, `503` backpressure ✅
7. UDP feed, stale-drop, `--feed-only`, `--no-udp` ✅
8. `DockerBackend` ✅
9. Both experiments + the three bilingual docs ✅

Phase 8 is deliberately late: everything works without Docker, so if Docker Desktop misbehaves
it costs one experiment, not the project.

---

## Git

Remote: `git@github.com:Yoshi4489/algorithm-challenge-network-project.git` (SSH).

Each phase gets its own commit pushed to `origin/main`. **Never force-push** — check
`git ls-remote origin` before writing to a remote that may already have history, and rebase onto
it rather than overwriting.
