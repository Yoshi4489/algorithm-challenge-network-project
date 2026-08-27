# CDAP Player Manual

This guide takes you from an empty terminal to a completed Code Duel Arena Protocol (CDAP)
match. You need Python 3.9 or newer; the project was tested with Python 3.14. No third-party
packages are required for the default subprocess judge.

## 1. Open the project

Open PowerShell and move to the project directory:

```powershell
cd C:\Users\win\Downloads\Projects\network-mid-term-project
```

Optional: confirm the interpreter and environment capabilities:

```powershell
py -3.14 -m cdap.capabilities
```

## 2. Start the arena

For a first test, run a solo arena. This creates a match as soon as one player queues:

```powershell
py -3.14 -m cdap.server --min-players 1
```

Leave this terminal running. It is the arena server and prints every TCP frame, UDP datagram,
status code, and status phrase.

For a normal two-player game, use the default server command instead:

```powershell
py -3.14 -m cdap.server
```

The default server needs two queued players before it starts a match.

## 3. Start a player client

Open a second PowerShell window in the same project directory, then run:

```powershell
py -3.14 -m cdap.client --host 127.0.0.1
```

The client prints its available commands. Type commands at the prompt.

## 4. Create an account and log in

Use `raw` for the two account-management requests:

```text
raw REGISTER {"user":"alice","pass":"1234"}
raw LOGIN {"user":"alice","pass":"1234"}
```

Expected results:

- `201 REGISTERED` means the account was created.
- `200 OK` means the login succeeded.
- `409 USER_EXISTS` means the username is already registered; choose another name.
- `401 AUTH_FAILED` means the username/password combination was not accepted.

The client stores the login session and the UDP display token automatically.

## 5. Find or create a match

For matchmaking, type:

```text
queue
```

In solo mode, wait for `MATCH_FOUND` followed by `MATCH_START`. In two-player mode, both
players must register, log in, and run `queue`.

You can leave the queue before a match starts:

```text
dequeue
```

### Private room option

Instead of public matchmaking, create a room:

```text
room
```

The server returns a short room code. The second player joins it:

```text
join ABCD
ready
```

The room creator also types:

```text
ready
```

Replace `ABCD` with the room code returned by the server. Once everybody is ready, the match
starts after its countdown.

## 6. Read the problem

After `MATCH_START`, type:

```text
problem
```

The response includes the problem statement, function signature, examples, accepted language,
time requirement, and space requirement. The default language is Python.

## 7. Write a solution

Create a Python file such as `solution.py` in the project directory. It must define the function
named in the problem response. For example, if the problem says:

```text
solve(nums: list[int]) -> int
```

your file must contain a compatible `solve` function. Do not add interactive `input()` or
terminal output code; the judge imports and calls the function directly.

## 8. Submit the solution

From the player client, submit the file:

```text
submit solution.py
```

The immediate response is normally `202 ACCEPTED`, meaning the arena queued the source for
judging. A later `VERDICT` event contains the actual outcome. You can also re-check the newest
submission at any time:

```text
status
```

To inspect a particular submission ID shown by the server:

```text
status s-0001
```

## 9. Understand verdicts

| Verdict | Meaning |
|---|---|
| `600 ACCEPTED` | Correct and within the declared time and space contract. |
| `601 WRONG_ANSWER` | The solution returned an incorrect result. |
| `602 TIME_LIMIT_EXCEEDED` | One execution exceeded its wall-clock limit. |
| `603 MEMORY_LIMIT_EXCEEDED` | Peak memory exceeded the problem limit. |
| `604 COMPILE_ERROR` | The source could not be parsed or compiled. |
| `605 RUNTIME_ERROR` | The submitted function raised an exception. |
| `606 TIME_COMPLEXITY_VIOLATION` | Correct result, but runtime grew faster than the contract permits. |
| `607 SPACE_COMPLEXITY_VIOLATION` | Correct result, but auxiliary memory grew too quickly. |
| `608 OUTPUT_FORMAT_ERROR` | Right value in an incompatible type or shape. |
| `609 SANDBOX_VIOLATION` | The code attempted an operation forbidden by the judge. |
| `611 INDETERMINATE_COMPLEXITY` | Measurements could not confidently identify a complexity class. |
| `612 JUDGE_ERROR` | The judging system failed; this is not a player verdict. |

The first player to receive `600 ACCEPTED` wins. A match can also end when the clock expires,
or when a player forfeits/disconnects.

## 10. Optional live UDP feed

Open another terminal and start a feed-only client for the same account:

```powershell
py -3.14 -m cdap.client --host 127.0.0.1 --user alice --feed-only
```

This window shows progress ticks, countdown clock updates, and the board. It is display-only:
the TCP client remains authoritative for login, submissions, verdicts, and match results.

To demonstrate UDP loss while keeping the game playable:

```powershell
py -3.14 -m cdap.client --host 127.0.0.1 --user alice --feed-only --udp-loss 0.4
```

To run the server without UDP:

```powershell
py -3.14 -m cdap.server --udp-port 0 --min-players 1
```

## 11. Leave safely

In the player client, type:

```text
quit
```

This sends `LOGOUT` and closes the connection. If you are in an active match, leaving counts as
a forfeit. Stop the server with `Ctrl+C` in its terminal.

## Troubleshooting

| Problem | What to do |
|---|---|
| `503 JUDGE_UNAVAILABLE` on submit | Start the server without `--judges 0`, or connect a remote worker. |
| `503 SERVER_BUSY` | The server reached a configured capacity limit; wait for a session to close or restart with a higher limit. |
| `403 NOT_IN_MATCH` | Run `queue` and wait for `MATCH_START` before submitting. |
| `403 WRONG_STATE` | The match countdown has not finished yet. |
| `410 MATCH_ENDED` | The match timer expired or another player won; queue for a new match. |
| `429 SUBMIT_COOLDOWN` | Wait a few seconds before submitting again. |
| Client cannot connect | Ensure the server is running and the host/port match. The default TCP port is 5050. |

For the full protocol design, status-code reference, judge details, and demo commands, see
`README.md` and `docs/CDAP-protocol-spec.md`.
