# CDAP Presentation and Demo Script (14:30 maximum)

โครงนี้ออกแบบสำหรับวิดีโอไม่เกิน 15 นาที มีเวลาเผื่อ 30 วินาที ผู้พูดควรอยู่บนกล้องในช่วงเปิด
และสรุป แล้วสลับ screen recording สำหรับ architecture, code และ live demo

## Slide 1 - Problem and Idea (0:00-0:45)

**On screen:** CDAP title, one sentence, `606 TIME_COMPLEXITY_VIOLATION`

**พูด:**

> โปรเจกต์นี้คือ Code Duel Arena Protocol หรือ CDAP เป็น network application สำหรับแข่งขัน
> เขียน algorithm แบบ real-time จุดต่างคือระบบไม่ได้ตรวจแค่ว่าคำตอบถูก แต่ตรวจว่า time และ
> space complexity ตรงตาม contract ด้วย คำตอบ O(n squared) ที่ถูกอาจได้ verdict 606

## Slide 2 - Assignment Deliverables (0:45-1:15)

- Named application-layer protocol: `CDAP/1.0`
- Client/server source code
- Every sent/received message prints status code and phrase
- PDF design and live tests

## Slide 3 - Why TCP and UDP (1:15-2:15)

แสดงตารางสองคอลัมน์:

- TCP: auth, source, verdict, match state - reliable, ordered, complete
- UDP: clock/progress/board - latest-value-wins, no retransmit

**Key sentence:** ถ้า UDP ตายทั้งหมด match ยังจบถูกต้องผ่าน TCP; `--no-udp` พิสูจน์ invariant นี้

## Slide 4 - Architecture (2:15-3:00)

```text
Players --TCP--> Arena/Queue <--TCP-- Workers
Feed panes <--UDP-- Arena
```

อธิบาย Worker เป็น connection initiator และใช้ long-poll จึงอยู่หลัง NAT ได้

## Slide 5 - CDAP Framing and Correlation (3:00-4:00)

แสดง request, response และ event อย่างละหนึ่ง start line:

```text
CDAP/1.0 SUBMIT        Seq: 7
CDAP/1.0 202 ACCEPTED Seq: 7
CDAP/1.0 EVENT VERDICT Event-Id: 17
```

อธิบาย TCP เป็น byte stream จึงใช้ `Content-Length`; Response echo `Seq`; Event ไม่มี `Seq`
และ `Connection` มี send lock กัน frame interleave

## Slide 6 - Status Design (4:00-4:45)

- Protocol status `1xx-5xx`: message/conversation สำเร็จหรือไม่
- Judge verdict `6xx`: code ของผู้เล่นเป็นอย่างไร
- `606` อยู่ใน event หลัง protocol success ไม่ใช่ response error

## Slide 7 - State Machine and Async Flow (4:45-5:30)

แสดง `INIT -> GREETED -> IDLE -> QUEUED -> IN_MATCH -> IDLE`

ลำดับ demo: `QUEUE -> 202`, `MATCH_START event`, `SUBMIT -> 202`, progress events,
`VERDICT event` การ defer dispatch หลัง response ป้องกัน verdict แซง submission id

## Slide 8 - Complexity Profiler (5:30-6:45)

- Method A: six sizes, five repeats, minimum, relative-RMSE model fitting
- Method B: deterministic opcode count with `sys.monitoring`
- Margin below 1.15 favors cheaper class
- Method A authoritative; Method B exposes disagreement

กล่าวข้อค้นพบ: `sys.settrace` บน CPython 3.14.3 นับ zero แบบ silent และ Method B มองไม่เห็น
work ใน C `list.sort()`

## Slide 9 - Sandbox and Workers (6:45-7:45)

- AST guard = defence-in-depth, not boundary
- Subprocess = portable but weak on Windows
- Docker = no network, read-only, memory/pid cap, unprivileged
- Worker heartbeat lease: three misses -> eject + requeue; first verdict wins

## Live Demo Setup (ก่อนอัด)

เปิด terminals และ Docker Desktop ล่วงหน้า ใช้ port default 5050/5051:

```powershell
python -m cdap.server --judges 0 --worker-token demo --countdown 1 -v
python -m cdap.judge.worker --arena 127.0.0.1:5050 --id w1 --token demo
python -m cdap.client --host 127.0.0.1 --user alice --feed-only
```

เตรียม command history สำหรับ player terminals และตรวจ UTF-8 arrows ก่อนเริ่มอัด

## Demo 1 - Full Duel and Wire Log (7:45-9:30)

Terminal Alice:

```powershell
python -m cdap.client --user alice --queue --submit samples/max_subarray_on.py --once
```

Terminal Bob:

```powershell
python -m cdap.client --user bob --queue --submit samples/max_subarray_on2.py --once
```

ชี้ให้เห็นทุก frame มี direction, transport, code และ phrase Alice ควรได้ `600 ACCEPTED`
ส่วน brute force ถูกแต่ช้าได้ `606 TIME_COMPLEXITY_VIOLATION` พร้อม measured vs required

## Demo 2 - Protocol Error Matrix (9:30-10:45)

ใช้ solo server (`--min-players 1`) หรือเปิด commands ทีละตัว:

```powershell
python -m cdap.client --user version-demo --bad-version
python -m cdap.client --user hash-demo --tamper
python -m cdap.client --user lang-demo --lang rust --queue --submit samples/max_subarray_on.py --once
```

Expected: `426 VERSION_UNSUPPORTED`, `422 BODY_HASH_MISMATCH`, `415 UNSUPPORTED_LANGUAGE`

## Demo 3 - UDP Loss vs TCP Correctness (10:45-11:45)

```powershell
python -m cdap.client --user alice --feed-only --udp-loss 0.4
python -m cdap.client --user alice --queue --submit samples/max_subarray_on.py --once --no-udp
```

ชี้ `[UDP X] simulated loss`, sequence กระโดดไปค่าล่าสุด และ TCP pane ยังได้ verdict

## Demo 4 - Worker Backpressure and Recovery (11:45-12:30)

1. Start Arena ด้วย `--judges 0` และยังไม่ start Worker
2. Submit แล้วชี้ `503 JUDGE_UNAVAILABLE`
3. Start Worker และ retry; ได้ `202 ACCEPTED`
4. ถ้ามีเวลา kill Worker หลัง pull แล้ว start replacement; log แสดง requeue

## Slide 10 - Experimental Results (12:30-13:30)

แสดง confusion matrix 8 rows และ backend table:

- Method A polynomial 6/7 = 85.7%
- Method B pure-Python 5/7 = 71.4%, all 5/8 = 62.5%
- Sort case: A `O(n log n)`, B `O(n)`
- Repeated overhead (`n=3`): subprocess mean 92.2 ms vs Docker mean 2429.8 ms
- Docker median 1027.3 ms; first warm run explains the higher mean
- Guard off: three subprocess probes escape; Docker blocks/confines all three

อย่าพูดว่า accuracy สมบูรณ์ ให้ชี้ indeterminate set solution เป็น limitation จริง

## Slide 11 - Limitations (13:30-14:05)

- O(n) vs O(n log n) ใกล้กันและ timing noisy
- Opcode counting blind to C builtins
- Windows subprocess memory/CPU limits weak
- Worker token/TCP ไม่มี encryption; UDP unauthenticated beyond attach token
- Worst-case generator เป็น responsibility ของ problem author

## Slide 12 - Conclusion (14:05-14:30)

กลับมา on camera:

> CDAP แสดงทั้ง TCP framing, asynchronous server push, UDP latest-wins feed และ distributed
> worker protocol ใน application เดียว จุดสำคัญคือแยก reliability ตามชนิดข้อมูล และรายงาน
> limitation จากการทดลองจริงแทนการซ่อน ขอบคุณครับ/ค่ะ

## Recording Checklist

- [ ] Video duration <= 15:00
- [ ] Student on camera during opening and conclusion
- [ ] Both client and server logs visible
- [ ] Every demonstrated response shows code + phrase
- [ ] Show at least one success, protocol error, judge rejection, UDP loss and TCP-only run
- [ ] Hide real passwords/tokens; use demo values only
- [ ] Stop all worker/server processes after recording
