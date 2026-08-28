# Security Guide

## Security posture

CDAP is a coursework arena, not an Internet-facing service. Its safe default is a
loopback-only server (`127.0.0.1` / `localhost`) for a trusted local demonstration.
The TCP protocol does **not** use TLS. Do not expose it to the public Internet, use it
over an untrusted Wi-Fi/LAN, or treat the UDP feed as an authenticated channel.

A non-loopback bind is rejected unless the operator deliberately supplies both:

```powershell
py -3.14 -m cdap.server --host <LAN-IP> --backend docker --allow-insecure-remote
```

This is only an explicit controlled-demo override. Docker protects submitted code; it
does not encrypt passwords, source code, worker tokens, or TCP traffic.

## Enforced controls

### Protocol and resource bounds

- TCP framing limits headers to 16 KiB, 64 fields, and bodies to 1 MiB before allocation.
- Python submissions are limited to 256 KiB.
- `Content-Length` frames each message exactly; malformed or oversized frames are rejected.
- `Body-SHA256` protects body integrity after framing.
- User-derived header values are normalized before they are placed in response headers.
- Connection writes are serialized, and each session's event outbox is bounded to 256 events.
- Pending judge work is bounded (default 128 jobs). Overload returns
  `503 JUDGE_QUEUE_FULL` before a new source record is retained.

### Accounts, credentials, and abuse resistance

- Usernames allow only letters, numbers, `_`, `-`, and `.` and are capped at 24 characters.
- Passwords must be strings and are capped at 128 characters.
- Passwords are stored only as unique-salt `scrypt` verifiers (`N=16384`, `r=8`, `p=1`),
  compared with `secrets.compare_digest`; plaintext passwords are not retained.
- Login intentionally returns the same `401 AUTH_FAILED` result for an unknown account and
  a wrong password.
- Registrations are capped at 10 per peer address per 10 minutes.
- Failed logins are capped at 5 per peer address per minute; the server returns `429` when
  the limit is reached.
- Feed/session tokens are random 128-bit values and are replaced when a new login token is issued.

### Worker authorization and job integrity

- Remote worker methods are disabled by default. With an empty `--worker-token`, every
  `WORKER_*` request returns `503 WORKERS_DISABLED`.
- A configured non-empty worker token is checked with constant-time comparison.
- A worker identity is bound to its TCP connection; a player connection cannot become a worker.
- Duplicate live worker IDs are rejected.
- Worker jobs use heartbeat leases. A disconnected or expired worker has its job requeued.
- The first valid verdict wins; delayed or duplicate results cannot overwrite it.
- Submission ownership is checked before a player can retrieve a verdict.

Use a high-entropy token and avoid putting it in shell history when possible:

```powershell
py -3.14 -m cdap.server --worker-token '<long-random-secret>'
py -3.14 -m cdap.judge.worker --id judge-1 --token '<same-secret>'
```

### Logging and terminal safety

- Wire logs redact `pass`, `password`, `Token`, `Worker-Token`, and `Authorization`-like
  fields in both headers and JSON bodies, including verbose logs.
- C0/C1 terminal controls, including escape/OSC sequences, are printed as visible `\xNN`
  escapes. Untrusted source or headers cannot control the terminal through log output.
- The protocol still logs message type, status code, phrase, framing metadata, and safe body
  previews required for the coursework demonstration.

### Judge isolation

The AST guard rejects common dangerous operations (`import` outside the allow-list,
`open`, `eval`, `exec`, dynamic import, dunder attribute access, and similar constructs).
It is a fast filter only—not a sandbox boundary.

The subprocess backend is acceptable only for the local trusted demo. It uses a fresh child,
temporary working directory, wall-clock timeout/process-tree termination, output caps, and
best-effort memory controls. On Windows it has no hard `setrlimit`/cgroup boundary.

Docker is required for an opted-in remote demo. It runs the harness with:

- no network;
- read-only root filesystem;
- a small `noexec,nosuid` `/tmp` tmpfs;
- memory, CPU, and PID limits;
- all Linux capabilities dropped and `no-new-privileges`;
- unprivileged UID/GID `65534`;
- automatic container removal and timeout cleanup.

### Match and display integrity

- TCP remains authoritative for state-changing actions, verdicts, and match outcomes.
- UDP carries display-only clock/score snapshots. It cannot submit source, alter a match, or
  issue a verdict.
- Clients reject malformed, unexpected, stale, and excessive-jump UDP datagrams; warning output
  is rate-limited.
- Event IDs are allocated and enqueued under one lock so event delivery preserves event order.
- Automatic problem fetch/submission is tied to the exact active match and is cancelled after
  match end.
- A `600 ACCEPTED` verdict requires complete required time and space measurement; incomplete
  measurement results in `611 INDETERMINATE_COMPLEXITY` or `612 JUDGE_ERROR` instead.

## Operator checklist

1. Prefer the default loopback host and local judge for demonstrations.
2. Use Docker for any untrusted submission or non-loopback demonstration.
3. Set a long, unique `--worker-token` before enabling remote workers.
4. Do not share logs containing values outside CDAP's redacted fields without reviewing them.
5. Keep Docker Desktop/current container runtime patched when using DockerBackend.
6. Do not use `--allow-panic` outside a demonstration of the protocol's `500` path.
7. Stop the arena after the demonstration; accounts, match state, and tokens are in-memory only.

## Known limitations and deferred hardening

- CDAP has no TLS or mTLS. The remote override is plaintext and vulnerable to network
  interception or modification.
- Worker tokens passed on a command line can be visible to local process-inspection tools.
- UDP attach tokens and UDP snapshots are replayable/spoofable; this affects display only.
- Docker currently uses a mutable base-image tag and a read-only project mount. Pinning an image
  digest and building a minimal runtime bundle remain follow-up work.
- Hidden test inputs/profiling ladders are deterministic at present; per-submission randomized
  hidden generation is planned to reduce special-casing and cross-call cache bias.
- Client and worker connection routing code is not yet consolidated into a shared component.
- Complexity inference and opcode counting are measurement tools, not cryptographic proofs;
  C-implemented work and timing noise have documented blind spots.

See [docs/threat-model.md](docs/threat-model.md) for the original bilingual threat-model
discussion and [REVIEW_AUDIT_1.md](REVIEW_AUDIT_1.md) for the audit findings and remediation map.

## Reporting a security issue

For this coursework repository, do not publish credentials, bearer tokens, exploit payloads,
or other sensitive material in a public issue. Report the affected file/function, impact,
reproduction conditions, and a safe redacted proof to the project maintainer/instructor first.
