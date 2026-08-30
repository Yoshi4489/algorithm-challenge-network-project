# คู่มือไฟล์ส่งงานและคำอธิบายส่วนสำคัญ

## ไฟล์ที่ควรส่ง

ถ้าอาจารย์ต้องการ **source code ของระบบทั้งหมด** ให้ส่งไฟล์ข้อความต่อไปนี้:

- `cdap/**/*.py` — protocol, server, client, problems และ judge worker/backend
- `solution.py` — solution O(n) / O(1) สำหรับ Maximum Subarray
- `README.md`, `CLAUDE.md` — วิธีรันและ design invariants
- `docs/*.md` และ `docs/build_pdf.py` — source ของรายงานและสคริปต์สร้าง PDF
- `samples/*.py`, `experiments/*.py` — test/demo cases และงานทดลอง (ควรส่งเมื่อ rubric ต้องการหลักฐาน)

ไฟล์ที่ **ไม่จำเป็นใน source-code archive**:

- `.git/`, `.claude/`, `__pycache__/`, `.pytest_cache/`, virtual environment
- `experiments/out/`, log, temporary files และไฟล์ที่ generate ใหม่ได้
- วิดีโอ `.mp4` และ PDF final ไม่ใช่ source code; ส่งแยกในช่อง deliverable ของมัน
- password/token จริง, `.env`, private key หรือไฟล์ตั้งค่าเครื่องส่วนตัว

ถ้าระบบรับได้เพียงหนึ่ง ZIP ให้รวม source + PDF + video ตาม rubric แต่ยังไม่ควรรวม `.git`, cache,
log หรือ secret การใช้ `git archive develop` เป็นวิธีสร้าง ZIP จากไฟล์ที่ commit แล้วโดยไม่ดึง
ไฟล์ untracked/ไฟล์ภายใน `.git` เข้าไป

## โค้ดส่วนที่จำเป็น

| ส่วน | หน้าที่ |
|---|---|
| `cdap/protocol.py` | framing ของ TCP, `Message`, header, `Seq`, `Event-Id`, body hash, UDP codec และ wire log |
| `cdap/status.py` | แยก protocol status 1xx–5xx ออกจาก judge verdict 6xx และบังคับ phrase ที่ถูกต้อง |
| `cdap/server.py` | state machine, auth, lobby/room/match, deterministic winner, submission queue, worker lease, UDP feed, `GET_STATE` และ idempotency |
| `cdap/client.py` | request/response correlation, event reader, event-gap recovery, CLI, UDP display และ verdict renderer |
| `cdap/problems.py` | statement, contract, visible tests, hidden generator, trusted oracle และ performance sizes |
| `cdap/judge/runner.py` | โหลด solution, correctness, hidden performance/memory measurement และ optional Big-O measurement |
| `cdap/judge/profiler.py` | แปลง measurement record เป็น verdict พร้อมหลักฐาน |
| `cdap/judge/backends.py` | subprocess/Docker execution และ timeout boundary |
| `cdap/judge/worker.py` | remote worker long-poll, heartbeat, lease และส่งผลกลับ Arena |

เส้นทางหลักคือ `SUBMIT` ตอบ `202 ACCEPTED` ก่อน จากนั้น server ส่ง job ไป worker และส่ง
`VERDICT` event ภายหลัง TCP ใช้กับข้อมูลที่หายไม่ได้ ส่วน UDP ใช้กับ progress ล่าสุดเท่านั้น

## ภาษาและ password

- Wire format มี header `Lang` จึงออกแบบให้ขยายภาษาได้ แต่ implementation ปัจจุบันรองรับ
  **Python เท่านั้น** (`SUPPORTED_LANGUAGES = ("python",)`). การเพิ่มภาษาใหม่ต้องเพิ่ม runner,
  compiler/runtime image, limits และ tests ของภาษานั้น ไม่ใช่เพียงเปลี่ยนชื่อ header
- ค่าเริ่มต้นของ client คือ user `alice`, password `1234`
- ครั้งแรก `--pass VALUE` จะใช้ `VALUE` สร้าง account แล้ว login ทันที
- ครั้งถัดไปต้องใช้ password เดิม; `--pass` ไม่ได้เปลี่ยนหรือ reset password ของ account ที่มีอยู่
- account อยู่ใน memory ของ server process เมื่อ restart server จึงเริ่ม register ใหม่ได้

ตัวอย่าง:

```powershell
py -3 -m cdap.client --user alice --pass 1234
py -3 -m cdap.client --user Yoshi --pass my-first-password
```

## Status code สำคัญ

Protocol status บอกว่า request สำเร็จหรือไม่ ส่วน verdict บอกผลของ source code ห้ามนำ 6xx
ไปใส่ใน response start line

| Code | ความหมายที่ควรจำ |
|---|---|
| `200 OK` | request สำเร็จและมีผลทันที |
| `201 REGISTERED/CREATED` | สร้าง account/room สำเร็จ |
| `202 ACCEPTED/QUEUED` | รับงานแล้ว แต่ผลสุดท้ายจะมาทีหลัง |
| `400 BAD_REQUEST` | body/header ไม่ถูกต้อง; `INVALID_SOURCE_ENCODING` คือ source ไม่ใช่ UTF-8 |
| `401 AUTH_FAILED` | ยังไม่ login หรือ user/password ไม่ถูกต้อง |
| `403 NOT_IN_MATCH/NOT_IN_ROOM/WRONG_STATE` | identity ถูกต้อง แต่ state ไม่อนุญาต |
| `404 ..._NOT_FOUND` | resource ที่ระบุไม่มีอยู่ |
| `409 CONFLICT` | state ซ้ำ/ชนกัน; รวม `IDEMPOTENCY_CONFLICT` |
| `410 MATCH_ENDED` | match ปิดรับ submission แล้ว |
| `413 PAYLOAD_TOO_LARGE` | body/source ใหญ่เกิน limit |
| `415 UNSUPPORTED_LANGUAGE` | server ไม่มี runner สำหรับภาษานั้น |
| `422 BODY_HASH_MISMATCH` | bytes ที่รับไม่ตรง SHA-256 ที่ client ประกาศ |
| `429 RATE_LIMITED` | request เร็วเกิน cooldown/rate limit |
| `500 INTERNAL_ERROR` | bug ฝั่ง server |
| `503 JUDGE_UNAVAILABLE` | ไม่มี worker/queue เต็ม/server busy |
| `600 ACCEPTED` | output ถูกและผ่าน authoritative limits |
| `601 WRONG_ANSWER` | ค่าหรือ shape ของ output ผิด |
| `602 TIME_LIMIT_EXCEEDED` | CPU/wall time เกิน limit |
| `603 MEMORY_LIMIT_EXCEEDED` | peak auxiliary memory เกิน limit |
| `604/605` | compile error / runtime error |
| `606/607` | Big-O time/space ผิด contract ใน `complexity-demo` |
| `611/612` | วัด Big-O ไม่ชัดเจน / judge ทำงานผิดพลาด |

## Measurement ที่ส่งใน verdict

โหมดเริ่มต้น `performance` ใช้ข้อมูลต่อไปนี้ตัดสิน:

- `sizes`: hidden stress sizes ของโจทย์
- `cases`: `n`, `cpu_ms`, `wall_ms` ต่อ case
- `cpu_ms`: เวลาประมวลผลของ contestant รวม
- `wall_ms`: เวลาจริงรวม ใช้จับ sleep/block/scheduling delay
- `time_limit_ms`, `wall_limit_ms`: เกณฑ์เปรียบเทียบ
- `trials`: 1 รอบเมื่อผลชัดเจน; 3 รอบและใช้ median เมื่ออยู่ในช่วง ±10% ของ limit
- `peak_aux_kb`, `mem_limit_kb`: peak memory ที่วัดแยกบน hidden case ใหญ่สุด
- `policy_version`, `decision_basis`, `complete`: บอก policy และยืนยันว่าหลักฐานครบ

`complexity-demo` เพิ่ม `samples_ms`, opcode counts, fitted class, relative RMSE, margin,
log-log slope, confidence, `inferred_time`, `inferred_space` และ `methods_disagree`

## Time และ space complexity ของ solution หลัก

`solution.py` ใช้ Kadane's algorithm:

- Best/Average/Worst time: **Θ(n)** เพราะอ่านสมาชิกแต่ละตัวหนึ่งครั้ง
- Auxiliary space: **Θ(1)** มีเพียง `current` และ `best`
- Input space: **Θ(n)** เป็นข้อมูลของ caller จึงไม่นับเป็น auxiliary memory
- empty input: raise `ValueError` ชัดเจน; single/all-negative input คืนสมาชิกที่ดีที่สุด

Complexity ของระบบรอบหนึ่ง (ไม่รวมการรัน source ของผู้ใช้): parsing frame เป็น O(header + body),
lookup session/submission โดย dict เฉลี่ย O(1), queue put/get O(1), และสร้าง scoreboard O(players)
หลังเปลี่ยนจากการ scan submission history ทุก tick มาใช้ score aggregate

## คำสั่งตรวจสุดท้าย

```powershell
py -3 -m cdap.selftest_protocol
py -3 -m cdap.selftest_client
py -3 -m cdap.selftest_audit
py -3 -m cdap.selftest_performance
py -3 -m cdap.problems
```

ก่อนส่งให้ยืนยันว่าอยู่ branch `develop`, tests ผ่าน และ archive ไม่มี secret/cache/media ที่ไม่ได้
ตั้งใจส่ง
