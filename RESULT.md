# CDAP Manual Test Result

**Result: PASS** — I ran a complete solo game on this computer using the bundled correct
Fibonacci solution. The arena accepted the account, started a match, judged the file, and
returned `600 ACCEPTED`.

## What was started

### Terminal 1: server

```powershell
py -3.14 -m cdap.server --host 127.0.0.1 --tcp-port 5060 --udp-port 5061 --min-players 1 --problem fib --countdown 0.1 --match-seconds 30
```

Why these options were used:

- `--min-players 1` creates a solo match, so a second player is not needed.
- `--problem fib` selects the Fibonacci problem.
- Ports `5060` and `5061` avoid interfering with a normal server using the defaults.

The server reported that two local judges were healthy and that it was listening on TCP port
5060 and UDP port 5061.

### Terminal 2: player

```powershell
py -3.14 -m cdap.client --host 127.0.0.1 --port 5060 --udp-port 5061 --user manual-alice --pass manual-pass --queue --submit samples\fib_on.py --once --no-udp
```

This one command performs the manual steps automatically:

| Option | Same manual action |
|---|---|
| `--user` and `--pass` | Choose account credentials. |
| `--queue` | Type `queue` after login. |
| `--submit samples\fib_on.py` | Submit this file after `MATCH_START`. |
| `--once` | Exit after the first verdict. |
| `--no-udp` | Demonstrate that the match works using TCP only. |

## What happened during the test

| Step | Protocol result | Meaning |
|---|---|---|
| 1. Handshake | `200 OK` | Client and server agreed on CDAP/1.0. |
| 2. Account creation | `201 REGISTERED` | Account `manual-alice` was created. |
| 3. Login | `200 OK` | The client received a session ID and UDP token. |
| 4. Queue | `202 QUEUED` | The player entered matchmaking. |
| 5. Match event | `MATCH_FOUND` | A solo match was created. |
| 6. Match event | `MATCH_START` | The Fibonacci problem clock started. |
| 7. Problem request | `200 OK` | Client received the problem and its `O(n)` / `O(1)` contract. |
| 8. Submission | `202 ACCEPTED` | `samples\fib_on.py` was added to the judge queue as `s-0001`. |
| 9. Judge result | `600 ACCEPTED` | The solution passed every test and met both complexity requirements. |
| 10. Match event | `MATCH_END` | The match ended with `manual-alice` as winner. |

## Actual final verdict

```text
VERDICT 600 ACCEPTED
submission : s-0001
tests      : 8/8
time       : measured O(n) vs required O(n), confidence=high
space      : measured O(1) vs required O(1), confidence=high
backend    : subprocess
MATCH_END  : reason=SOLVED winner=manual-alice
```

## The easiest way for you to repeat it

Open two PowerShell windows in the project folder.

In the first window, paste:

```powershell
py -3.14 -m cdap.server --min-players 1 --problem fib
```

In the second window, paste:

```powershell
py -3.14 -m cdap.client --user your-name --pass your-password --queue --submit samples\fib_on.py --once
```

Replace `your-name` and `your-password` with values you choose. You should see `600 ACCEPTED`.

## If you want to type commands yourself

Start the server as above, then start an ordinary client:

```powershell
py -3.14 -m cdap.client --user your-name --pass your-password
```

The client automatically registers and logs in when the account does not already exist. Then
type these two commands at its prompt:

```text
queue
```

Wait for `MATCH_START`, then type:

```text
submit samples\fib_on.py
```

That is the complete gameplay loop: **log in → queue → wait for match → submit solution → read
the verdict**.

## Important note

The successful test used `--no-udp`, proving that UDP is optional. TCP carries the important
actions: login, matchmaking, problem data, source-code submission, verdict, and match result.
UDP only adds live display updates.

## Compact player view

The normal client now keeps gameplay readable: it shows persistent `MATCH FOUND`, `MATCH START`,
`PROBLEM`, `VERDICT`, and `MATCH_END` blocks; a visible pre-match countdown; automatic problem
display; and only UDP timer milestones or score changes. Add `--wire` for the complete client
protocol transcript used in demonstrations; the server transcript remains complete by default.
