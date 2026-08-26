# CDAP Threat Model / แบบจำลองภัยคุกคาม

## 1. Scope

เอกสารนี้อธิบาย security boundary ของ Arena, Judge Worker, submission sandbox, TCP control
channel และ UDP live feed สำหรับ coursework deployment บนเครื่องเดียวหรือ LAN ที่เชื่อถือได้
ไม่อ้างว่า CDAP/1.0 พร้อมใช้งานบน public Internet

## 2. Assets

- ความถูกต้องของ match state, clock, submission ownership และ verdict
- Source code ของผู้เล่นก่อน match จบ
- Availability ของ Arena และ Judge Workers
- Host filesystem, process table, network access และ memory
- Worker pre-shared token และ player session/feed token
- ความจริงของ evidence เช่น `backend`, measured complexity และ status log

## 3. Adversaries

1. Malicious contestant ส่ง source code เพื่ออ่านไฟล์, เปิด network, spawn process หรือใช้ resource
2. Malformed client ส่ง frame ใหญ่, hash ผิด, method/state ผิด หรือ header injection
3. Fake Worker พยายาม pull source code หรือส่ง verdict ปลอม
4. UDP injector ส่ง display data ปลอม/reordered; ผลกระทบต้องจำกัดอยู่ที่หน้าจอ
5. Failed Worker หายกลาง job หรือส่ง result ช้าหลัง lease หมด

## 4. Trust Boundaries

```text
Untrusted player code
    | AST guard (cheap filter, bypassable)
    v
Subprocess process OR Docker container
    | result sentinel + JSON validation
    v
Judge Worker
    | authenticated WORKER_* over TCP (no encryption)
    v
Arena authoritative state
    | display-only snapshots
    v
Unauthenticated UDP feed after attach token
```

Container kernel boundary เป็น security boundary ที่แข็งแรงที่สุด AST guard ไม่ใช่ boundary
และ subprocess บน Windows ให้ isolation แบบ best-effort เท่านั้น

## 5. Controls

### 5.1 Protocol and Framing

- TCP frames จำกัดสูงสุด 1 MB; submission จำกัด 256 KB
- `Content-Length` กำหนด boundary และ reader อ่าน body เท่าจำนวน byte
- `Body-SHA256` ตรวจ source/job/result body ก่อนใช้
- Header values จาก user ถูกจำกัด character และ collapse whitespace เพื่อกัน CRLF injection
- Responses echo `Seq`; Events ไม่มี `Seq`; connection มี reader เพียง thread เดียว
- Event outbox จำกัด 256 entries เพื่อกัน slow receiver ใช้ memory ไม่จำกัด

### 5.2 Authentication and Authorization

- Unknown user และ wrong password ตอบ `401 AUTH_FAILED` เหมือนกัน
- Submission lookup ตรวจ owner; ของผู้อื่นได้ `403 FORBIDDEN`
- Worker ใช้ pre-shared token และ worker id ผูกกับ TCP connection
- Duplicate live worker id ถูกปฏิเสธ
- UDP attach token ใช้ระบุ display subscription เท่านั้น

### 5.3 Job Reliability

- Worker เป็น client และ long-poll จึงไม่ต้องเปิด inbound port
- Job มี heartbeat lease; สาม missed intervals ทำให้ eject/requeue
- Disconnect requeue job ทันที
- Dispatch เป็น at-least-once แต่ `record_verdict` รับ result แรกเพียงครั้งเดียว
- ไม่มี healthy local/remote judge ทำให้ `SUBMIT` ตอบ `503` ก่อนสร้าง record/cooldown

### 5.4 AST Guard

Static scan ปฏิเสธ import ที่ไม่อยู่ whitelist, `open`, `eval`, `exec`, `compile`, `input`,
`globals`, `locals`, `vars`, dynamic import และ dunder attribute access

ประโยชน์คือ reject operation ราคาถูกก่อน execute แต่ Python introspection มีหลายทาง bypass
จึงเรียก defence-in-depth เท่านั้น `--no-ast-guard` ต้องคงไว้เพื่อพิสูจน์ layer จริง

### 5.5 Subprocess Backend

- Fresh child, temporary cwd, closed stdin หลัง runner อ่าน job
- Parent wall-clock timeout และ process-tree kill
- stdout/stderr cap 64 KB
- Genuine sentinel result ต้องเป็น sentinel สุดท้าย
- POSIX ใช้ address-space rlimit เมื่อมี
- Windows ใช้ tracemalloc threshold และ optional psutil polling

ข้อจำกัด: child ยังรันด้วย user account เดียวกับ host, อ่าน absolute host path และเปิด network
ได้เมื่อ guard ถูก bypass CPU exhaustion จำกัดด้วย wall-clock kill เท่านั้น

### 5.6 Docker Backend

- `--network none`
- Read-only root filesystem และ explicit read-only `/app` mount
- `/tmp` เป็น small `noexec,nosuid` tmpfs
- Memory/memory-swap cgroup รวม runtime allowance; auxiliary contract ยังตรวจใน runner
- `--pids-limit 8`, `--cpus 1.0`
- `--cap-drop ALL`, `no-new-privileges`, UID/GID 65534
- Unique container name, `--rm`, force-remove on parent timeout

Base image provisioning เกิดก่อน submission stopwatch เพื่อไม่ให้ download ถูกนับเป็น algorithm time

### 5.7 UDP Boundary

UDP ไม่มี ACK, encryption หรือ message authentication นอกเหนือจาก attach token ผู้โจมตีใน LAN
อาจ spoof progress ได้ ดังนั้น payload มีเฉพาะ username, match id, clock, passed count และ number
of submissions ไม่มี source, password, authoritative verdict หรือ state-changing command

Client ทิ้ง malformed datagram และ `seq <= last_seen` การ spoof ทำให้ display ผิดชั่วคราวแต่ไม่
เปลี่ยน match outcome ซึ่งยังมาจาก TCP เท่านั้น

## 6. Empirical Security Test

`python -m experiments.backend_overhead` ปิด AST guard โดยตั้งใจและรัน probe เดียวกันใต้
สอง backends บน CPython 3.14.3 / Windows 11:

| Probe | Subprocess | Docker |
|---|---|---|
| Connect to controlled host listener | Escaped | Blocked by network namespace |
| Read controlled host-only path | Escaped | Confined; host path unavailable |
| Spawn 16 overlapping children | Escaped, 16 spawned | Incomplete, 7 spawned before pid cap |

ผลนี้แสดงว่า AST guard เป็น first filter แต่ Docker/kernel policy เป็น boundary ที่บังคับจริง
Subprocess สามารถ survive probe ได้แต่ไม่ได้ prevent capability นั้น

## 7. Accepted Risks / Deliberate Limitations

1. TCP ไม่มี TLS: password, source และ Worker token อ่านได้โดยผู้ดัก traffic
2. Password เก็บ plain text ใน memory; ไม่มี persistent database หรือ password hashing
3. Worker token เป็น shared secret ใน command line มองเห็นได้ใน process listing และ replay ได้
4. UDP attach token อาจ spoof/replay และ feed data ปลอมได้
5. AST guard bypassable และห้ามอ้างเป็น sandbox boundary
6. Windows subprocess ไม่มี hard memory cgroup/rlimit
7. Docker bind-mount `/app` read-only ทำให้ container อ่าน project source ได้ แม้อ่าน host ส่วนอื่นไม่ได้
8. Timing inference มี indeterminate classification; run ล่าสุดไม่ class set-based O(n)
9. O(n) กับ O(n log n) แยกได้ไม่สม่ำเสมอที่ input sizes นี้
10. Opcode counting มองไม่เห็น work ใน C builtins เช่น `list.sort()`
11. Worst-case generator ถูกต้องหรือไม่เป็นหน้าที่ problem author
12. In-memory account/match state หายเมื่อ Arena restart

## 8. Recommended Production Improvements

หากพัฒนาต่อสำหรับ public deployment ควรเพิ่ม TLS/mTLS, password hashing, per-worker credential,
persistent database, signed UDP snapshots หรือเปลี่ยน display channel เป็น authenticated QUIC,
container image pin by digest, seccomp/AppArmor profile, audit log persistence และ multi-host
orchestrator โดยการปรับเหล่านี้อยู่นอก scope ของ Socket Programming coursework

## 9. Security Conclusion

CDAP ลดผลกระทบจาก untrusted code ด้วย layered controls และระบุข้อจำกัดอย่างตรงไปตรงมา
หลักสำคัญคือไม่ให้ weak display channel หรือ bypassable static guard กลายเป็น authority:
Arena state และ verdict เดินทางผ่าน TCP ส่วน kernel-enforced Docker policy เป็น execution boundary
