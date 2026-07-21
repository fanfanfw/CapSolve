# CapSolve Production Readiness Plan

## Status ringkas (repo vs ops)

Tracked on branch `chore/production-ready-core` (base `main` @ `299b74a`).  
Legend: **code** = proven by unit/contract tests or quality gate on this checkout; **ops** = requires operator, host install, sudo, or live secrets.

### Core — code (AI / local)

- [x] Quality gate static + unit/contract: `uv run python deployment/ops.py quality` → **PASS** (190 tests OK; 17 PostgreSQL integration skipped without disposable DB)
- [x] Production fail-closed settings (API key, allowlist, hosts, docs off, retention required, UDS)
- [x] Atomic queue admission contract + HTTP 429/503 + golden API success contract
- [x] Worker JIT claim / fencing / sanitization (unit); lost-claim vs success separated
- [x] Chrome unique profile + slot release paths + global slot settings (unit); Xvfb external path for production
- [x] IP allowlist, API key, Host gate, forged-header rejection (unit + real Uvicorn/nginx where tools present)
- [x] Health vs readiness split; worker freshness parser; structured non-PII events
- [x] Purge + retention validation; production preflight static/runtime gates (unit/mocked)
- [x] Deployment artifacts (systemd/nginx/cloudflared/env examples) validated by quality gate
- [x] SQL migration `002_job_attribution.sql` present (apply on target DB before traffic)
- [x] PostgreSQL integration suite via Docker disposable: `uv run python tools/with_disposable_postgres.py -- uv run python deployment/ops.py quality` → **PASS** (`postgres: ENABLED_LOCAL_DISPOSABLE`; capacity race, fencing, slots, purge, readiness)
- [x] **code fix (this branch):** `sql/001` includes attribution columns; test fixtures accept `get_connection(statement_timeout=...)`; capacity race truncates seed; `PGUSER`/`PGPASSWORD` honored; `tools/with_disposable_postgres.py` for auth-free local gate

### Core — ops (operator; AI cannot finish alone)

- [ ] Disposable Postgres for full quality gate (`TEST_DATABASE_URL=postgresql://127.0.0.1:5432/<name_with_test>`, `PGPASSWORD` for role `postgres`)
- [ ] Phase 0 baseline capture + restore-test + validate on deploy host
- [ ] Deploy `main`/tip: `uv sync --frozen`, install units/env, non-root users
- [ ] Run migrations including `002` on target DB
- [ ] Production secrets: random API key(s), env files mode `0600`, separate API/worker/purge env
- [ ] Network: explicit `API_IP_ALLOWLIST`, `ALLOWED_HOSTS`, UDS chain, docs off, PostgreSQL not public
- [ ] Approve `JOB_RETENTION_HOURS` (e.g. 24); enable purge + backup timers; align backup retention
- [ ] Static preflight → start Xvfb/API/worker/purge/backup → one scheduled backup + restore evidence → runtime preflight
- [ ] Smoke: one approved E2E job; 401/403/docs-off checks; canary low volume (`GLOBAL_CHROME_SLOTS=1`)

#### Operator: enable local disposable Postgres tests (dev machine)

**Recommended (no host postgres password):** Docker disposable with known ephemeral password:

```bash
cd /path/to/CapSolve
uv run python tools/with_disposable_postgres.py -- uv run python deployment/ops.py quality
# expect postgres: ENABLED_LOCAL_DISPOSABLE and status PASS
```

**Or host Postgres:** name must contain `test`/`temp`/`disposable`; `PGUSER` (default `postgres`) + matching `PGPASSWORD` for TCP to `127.0.0.1`. Wrong password → `password authentication failed` (not a CapSolve logic bug).

```bash
export TEST_DATABASE_URL='postgresql://127.0.0.1:5432/capsolve_disposable_test'
export PGUSER=postgres
export PGPASSWORD='PASSWORD_THAT_MATCHES_THAT_ROLE'
uv run python deployment/ops.py quality
```

Do not put user/password in the URL. Do not reuse a production DB.

### Not core for soft launch (defer)

- [ ] Three-round production benchmark + final concurrency numbers
- [ ] Formal failed-ratio / oldest-pending alert thresholds
- [ ] CI on GitHub; off-host backup copy; purge index `CONCURRENTLY`

**Do not use branch `feat/api-access-queue-reliability` for this work** — it is a strict ancestor of current `main`. Work only on `main` or this branch.

---

## 1. Tujuan

Mempersiapkan CapSolve agar aman dan stabil untuk menerima submit BUDI95 dalam jumlah lebih banyak tanpa:

- kehilangan atau menduplikasi status job;
- membuat antrean tumbuh tanpa batas;
- membocorkan API key, credential database, atau NRIC;
- mempercayai header IP dari client secara langsung;
- menjalankan Chrome melebihi kapasitas server;
- mengubah kontrak API sukses yang sedang dipakai client.

Plan ini mencakup perubahan repo dan deployment server: FastAPI, worker, PostgreSQL, Chrome/Xvfb, systemd, nginx/Cloudflare, secret, monitoring, rollout, rollback, dan pengujian.

## 2. Keputusan yang Sudah Disepakati

| Keputusan | Pilihan |
| --- | --- |
| Cakupan | Repo dan server |
| Kontrak API | Response sukses existing tetap kompatibel |
| Target throughput | Diukur melalui benchmark sebelum menentukan concurrency production |
| IP production | Public IP/CIDR server pemanggil, nilainya belum diketahui |
| Development allowlist | Wildcard diperbolehkan hanya pada environment development |
| Production allowlist | Wajib berisi IP/CIDR eksplisit dan fail-closed |
| Retensi NRIC/hasil | Belum ditentukan; wajib diputuskan sebelum go-live |
| Dependency baru | Dihindari jika stdlib, FastAPI, Uvicorn, dan PostgreSQL existing sudah cukup |

## 3. Definisi Penting

Tiga pengaturan berikut tidak boleh dianggap sama:

- **Queue capacity**: jumlah maksimum job outstanding yang boleh tersimpan di PostgreSQL.
- **Worker batch limit**: jumlah maksimum job berbeda yang diproses oleh satu invocation worker.
- **Worker concurrency**: jumlah Chrome solve yang berjalan bersamaan.

Definisi outstanding:

```text
status IN ('pending', 'processing')
```

Row `success` dan `failed` tidak mengonsumsi kapasitas antrean, tetapi tetap tunduk pada kebijakan retensi.

Kondisi baseline historis (sebelum hardening di `main` tip; jangan dipakai sebagai status deploy):

- `POST /api/budi95` dapat memasukkan job tanpa batas kapasitas.
- `JOB_BATCH_LIMIT` hanya membatasi jumlah job yang diklaim worker; ini bukan ukuran queue.
- Worker mengklaim satu batch di depan lalu memprosesnya secara serial.
- Stale worker dapat menimpa status job yang sudah diklaim ulang.
- API dan worker dapat memakai Chrome profile yang sama.

Kondisi implementasi repo saat ini (code di `main` tip / branch ini):

- Atomic queue capacity + advisory lock; full queue → HTTP 429; DB admission failure → 503.
- Worker claims just-in-time with attempt fencing; stale finalization cannot overwrite newer claims (unit-proven; Postgres race suite when disposable DB enabled).
- Unique per-solve Chrome profiles; host-wide `GLOBAL_CHROME_SLOTS` shared by API and worker.
- Production fail-closed API key, IP allowlist, hosts, docs, UDS; retention required; purge + preflight tooling present.

## 4. Target Arsitektur

```text
Client yang IP-nya diizinkan
  -> Cloudflare Tunnel
  -> nginx di loopback/private origin
  -> Uvicorn satu process
  -> API key + IP allowlist + Host validation
  -> atomic queue admission di PostgreSQL
  -> response submit existing

systemd timer
  -> worker satu invocation pada satu waktu
  -> claim satu job saat slot tersedia
  -> Chrome profile unik
  -> solve + upstream POST
  -> fenced finalization di PostgreSQL
  -> ulangi sampai batch limit tercapai atau queue kosong
```

Jaminan realistis:

```text
at-least-once processing, fenced final database state
```

Exactly-once tidak dijanjikan karena proses dapat berhenti setelah upstream menerima request tetapi sebelum status database berhasil diperbarui.

## 5. Prioritas

### P0 — Harus selesai sebelum go-live

1. Secret production dan permission file aman.
2. Queue capacity atomik dan response overload yang benar.
3. Worker claim just-in-time dan fencing terhadap stale claim.
4. API key production, IP allowlist, dan trusted proxy chain.
5. Chrome profile isolation, Xvfb deployment, dan global Chrome slot lintas API/worker.
6. Integration test untuk capacity race, stale-worker race, dan combined API/worker load.
7. Retensi NRIC/hasil diputuskan, diwajibkan oleh production preflight, dan purge diterapkan.
8. Backup dan restore PostgreSQL dibuktikan.
9. Failure persistence, response, dan log disanitasi dari secret serta PII yang tidak diperlukan.

### P1 — Harus selesai sebelum trafik production dinaikkan

1. Readiness probe database.
2. Bounded queue untuk endpoint synchronous.
3. Structured operational logs dan log rotation.
4. Monitoring queue depth, oldest pending age, success rate, dan worker freshness.
5. Load test serta penetapan throughput dan concurrency berdasarkan hasil ukur.
6. Resource limit systemd yang sesuai perilaku Chrome.

### P2 — Hardening setelah jalur utama stabil

1. Dedicated OS user khusus CapSolve.
2. CI quality gates dan dependency scanning.
3. Retry delay berbasis database jika satu menit antar-worker belum memadai.
4. Scale horizontal lebih dari satu host/process group setelah global slot dan observability terbukti.

---

# Phase 0 — Baseline dan Safety Net

## Tujuan

Menyimpan baseline yang dapat dibandingkan dan menyediakan rollback sebelum perubahan perilaku.

## Pekerjaan

- Catat commit Git yang sedang aktif di server.
- Simpan salinan unit systemd, crontab, nginx vhost, dan konfigurasi Cloudflare ingress.
- Simpan daftar env variable tanpa nilainya.
- Catat schema dan row count tabel `budi95_jobs`.
- Catat baseline 24 jam jika datanya tersedia:
  - submit count;
  - success count;
  - failed/retry count;
  - median waktu proses;
  - peak memory;
  - ukuran log;
  - oldest pending job.
- Buat backup PostgreSQL dan lakukan test restore ke database terpisah.
- Tetapkan rollback commit dan salinan konfigurasi server sebelumnya.

## Acceptance Criteria

- [ ] Commit deployment diketahui.
- [ ] Konfigurasi server lama dapat dikembalikan.
- [ ] Backup database berhasil dibuat.
- [ ] Restore test berhasil dan row count sesuai.
- [ ] Tidak ada secret yang masuk Git atau dokumen baseline.

---

# Phase 1 — Konfigurasi Terpusat dan Fail-Fast

## Tujuan

Membuat semua setting production tervalidasi, terdokumentasi, dan memiliki semantics yang jelas.

## Env yang Diusulkan

```env
ENVIRONMENT=development

API_KEY=development-only-change-me
# API_KEYS=old-key,new-key
API_IP_ALLOWLIST=*
ALLOWED_HOSTS=localhost,127.0.0.1
API_DOCS_ENABLED=true
FORWARDED_ALLOW_IPS=127.0.0.1,::1

JOB_QUEUE_CAPACITY=100
JOB_QUEUE_RETRY_AFTER_SECONDS=60
JOB_BATCH_LIMIT=5
JOB_MAX_ATTEMPTS=3
JOB_RESET_STALE_MINUTES=30

SYNC_QUEUE_MAX_WAITING=0
MAX_WORKERS=1

DB_CONNECT_TIMEOUT=3

DISPLAY=:99
ENABLE_XVFB_VIRTUAL_DISPLAY=false
TS_PROFILE_DIR=/tmp/capsolve_profiles

# Wajib diputuskan sebelum production
# JOB_RETENTION_HOURS=
```

Nilai di atas adalah baseline development, bukan nilai final production.

## Aturan Validasi

### Environment

- `ENVIRONMENT` hanya menerima `development` atau `production`.
- Production harus menolak startup jika security setting invalid.

### API key

- Validasi bersifat component-specific: API wajib memiliki key, worker/purge tidak membutuhkan inbound API key.
- `API_KEYS` dipakai untuk rotasi key.
- Jika `API_KEYS` tidak kosong, hanya daftar tersebut yang aktif; `API_KEY` tidak ikut aktif secara implisit.
- Key production dibuat dengan `python -c "import secrets; print(secrets.token_urlsafe(32))"` atau mekanisme secret manager yang setara.
- Aturan yang dapat diuji: key production harus 43–128 karakter dan hanya memakai alphabet URL-safe `[A-Za-z0-9_-]`.
- Validator bukan estimator entropy. Provenance generation dicatat saat provisioning. Denylist deterministic menolak key jika seluruh karakter sama atau jika seluruh string terbentuk dari pengulangan exact unit sepanjang 1–8 karakter.
- Placeholder development seperti `development-only-change-me`, key kosong, key kurang dari minimum, dan key yang sama dengan contoh dokumentasi ditolak pada production.
- Comparison tetap menggunakan `hmac.compare_digest`.
- Key tidak boleh dicetak ke log, exception, health, atau response.

### IP allowlist

- `API_IP_ALLOWLIST` menerima IPv4, IPv6, dan CIDR yang dipisahkan koma.
- Parsing menggunakan `ipaddress.ip_network(..., strict=False)`.
- Nilai invalid menyebabkan startup gagal.
- `API_IP_ALLOWLIST=*` hanya valid saat `ENVIRONMENT=development`.
- Production menolak startup jika allowlist kosong atau wildcard.
- IP allowlist dan API key harus keduanya lulus.

### Host validation

- `ALLOWED_HOSTS` wajib memiliki hostname production saat `ENVIRONMENT=production`.
- Wildcard tidak diperbolehkan di production.
- Gunakan `TrustedHostMiddleware` existing dari Starlette/FastAPI.

### Integer dan boolean

- Capacity, batch, attempts, timeout, dan retry-after harus tervalidasi.
- `JOB_QUEUE_CAPACITY >= 1`.
- `JOB_BATCH_LIMIT >= 1`.
- `JOB_MAX_ATTEMPTS >= 1`.
- `JOB_RESET_STALE_MINUTES >= 0`; nilai `0` berarti stale reset dinonaktifkan.
- `SYNC_QUEUE_MAX_WAITING >= 0`.
- `JOB_RETENTION_HOURS >= 1` wajib ketika `ENVIRONMENT=production`; nilai kosong atau invalid menggagalkan production preflight/startup setelah purge tersedia.
- Boolean hanya menerima nilai yang terdokumentasi.
- Typo `JOB_MAX_ATTEMPS` tidak diterima sebagai alias; deployment harus diperbaiki menjadi `JOB_MAX_ATTEMPTS`.

## Reload Semantics

`.env` bukan hot reload.

- Setting API berubah setelah restart API.
- Setting worker berubah pada invocation worker berikutnya.
- Perubahan API key membutuhkan restart API.
- Perubahan IP allowlist membutuhkan restart API.
- `JOB_MAX_ATTEMPTS` baru hanya berlaku pada job yang dibuat setelah perubahan.
- Menurunkan capacity tidak menghapus job existing; submit baru ditolak sampai outstanding turun.

## File yang Diperkirakan Berubah

- `service.py`
- `database.py`
- `process_jobs.py`
- `.env.example`
- `README.md`
- kemungkinan satu module konfigurasi kecil bila mengurangi parsing duplikat secara nyata

## Acceptance Criteria

- [ ] Konfigurasi production tanpa API key gagal startup.
- [ ] Key di luar 43–128 karakter, alphabet invalid, placeholder, seluruh karakter sama, atau exact repetition unit 1–8 karakter ditolak di production.
- [ ] Key hasil `secrets.token_urlsafe(32)` diterima dan provisioning provenance dicatat tanpa menyimpan nilai di log.
- [ ] Konfigurasi production dengan wildcard allowlist gagal startup.
- [ ] CIDR invalid gagal startup dengan pesan yang tidak membocorkan secret.
- [ ] Development wildcard tetap dapat digunakan.
- [ ] Semua nilai integer invalid ditolak.
- [ ] Dokumentasi menjelaskan restart semantics.
- [ ] Tidak ada dependency runtime baru hanya untuk parsing config.

---

# Phase 2 — Bounded Async Queue

## Tujuan

Menerima burst submit sampai kapasitas yang terukur, tetapi menolak overload secara eksplisit sebelum database dan server kehabisan resource.

## Kontrak

### Kontrak existing yang dipertahankan

Sebelum mengubah implementasi, capture golden response untuk:

- `POST /api/solve/` sukses dan failure contract yang memang sudah menjadi kontrak publik;
- `GET /api/budi95/config`, termasuk mode normal dan `force_refresh` yang diizinkan;
- `POST /api/budi95` dan `POST /api/budi95/` sukses;
- `GET /api/budi95/result/{ulid}` untuk `pending`, `processing`, `success`, `failed`, dan not-found;
- `GET /api/health`.

Golden test dibagi dua:

- **Exact invariant**: seluruh response sukses existing, pending/processing state, not-found yang tidak membocorkan internals, health, serta config response mengunci HTTP status, header relevan, dan struktur/body existing.
- **Approved failure-hardening delta**: failure lama yang memuat exception mentah sengaja tidak dibandingkan exact. Fixture before hanya membuktikan kebocoran; fixture after wajib mengunci status yang disetujui, `error_code` terkontrol, pesan generik, dan absennya exception/secret/NRIC.

`POST /api/budi95` tetap mempertahankan HTTP status dan response body sukses existing agar client tidak breaking. Setiap approved delta didokumentasikan eksplisit di changelog/README dan test, bukan perubahan diam-diam.

### Queue penuh

```http
HTTP/1.1 429 Too Many Requests
Retry-After: 60
Content-Type: application/json
```

Response tidak boleh menampilkan NRIC atau detail internal:

```json
{
  "detail": "Job queue is full"
}
```

### Database/queue unavailable

```http
HTTP/1.1 503 Service Unavailable
Retry-After: 60
Content-Type: application/json
```

```json
{
  "detail": "Job queue is unavailable"
}
```

Kegagalan database tidak lagi boleh dikonversi menjadi HTTP 200 generik.

## Atomic Admission

Capacity check dan insert harus berada dalam satu transaksi:

1. Ambil PostgreSQL transaction advisory lock dengan key konstan khusus queue admission.
2. Hitung row `pending + processing` setelah lock diperoleh.
3. Jika count sudah mencapai `JOB_QUEUE_CAPACITY`, raise `QueueFullError`.
4. Jika slot tersedia, insert row job.
5. Commit melepas advisory lock.

Jangan hanya melakukan `COUNT` lalu `INSERT` tanpa serialization karena submit paralel dapat melewati slot terakhir secara bersamaan.

Tidak diperlukan schema migration untuk fase ini. Index status existing cukup untuk volume awal.

## Input Validation

- Trim whitespace NRIC.
- Tolak string kosong.
- Batasi maksimal 32 karakter sesuai schema existing.
- Jangan memaksakan format 12 digit sebelum kontrak dengan client dikonfirmasi.
- Jangan log full NRIC.

## File yang Diperkirakan Berubah

- `job_repository.py`
- `service.py`
- `self_check.py`
- test integration PostgreSQL kecil
- `.env.example`
- `README.md`

## Acceptance Criteria

- [ ] Capacity menghitung `pending + processing`.
- [ ] `success` dan `failed` tidak memakai slot.
- [ ] Queue penuh menghasilkan 429 dan `Retry-After`.
- [ ] DB unavailable menghasilkan 503, bukan 200.
- [ ] Exact golden invariants seluruh response sukses dan non-leaking state lulus sebelum/sesudah perubahan.
- [ ] Approved failure-hardening fixtures membuktikan error lama bocor dan error baru generic tanpa mengharuskan body lama tetap sama.
- [ ] Status, header, dan body sukses `/api/solve/`, config, kedua bentuk submit, seluruh result state yang aman, dan health tetap kompatibel.
- [ ] Sepuluh submit paralel dengan capacity 3 menghasilkan tepat 3 insert.
- [ ] Response error tidak memuat NRIC, SQL, host DB, username, atau password.
- [ ] Menurunkan capacity tidak menghapus row existing.

---

# Phase 3 — Worker Correctness dan Claim Fencing

## Tujuan

Mencegah job yang belum mulai terlihat sedang diproses dan mencegah stale worker menimpa hasil claimant baru.

## Claim Just-in-Time

Ubah flow worker dari:

```text
claim 5 job -> proses job 1..5 secara serial
```

menjadi:

```text
claim 1 job -> proses -> finalisasi
claim 1 job berikutnya -> proses -> finalisasi
berhenti setelah JOB_BATCH_LIMIT job berbeda atau queue kosong
```

Worker menyimpan `seen_ids` selama satu invocation agar job yang gagal dan kembali pending tidak langsung diklaim ulang pada invocation yang sama.

`JOB_BATCH_LIMIT` setelah perubahan berarti jumlah maksimum job berbeda per invocation, bukan jumlah row yang dipreclaim.

## Fencing Tanpa Migration

Gunakan kolom `attempts` existing sebagai claim generation:

1. Claim menaikkan `attempts` dan mengembalikan nilai barunya.
2. Worker membawa `expected_attempt` selama proses.
3. Complete/fail hanya boleh update dengan kondisi:

```sql
WHERE ulid = %s
  AND status = 'processing'
  AND attempts = %s
```

4. Update menggunakan `RETURNING` atau memeriksa row count.
5. Jika tidak ada row yang ter-update, worker telah kehilangan claim dan tidak boleh menimpa status baru.

Stale reset tetap boleh mengembalikan row ke pending, tetapi completion dari worker lama akan ditolak fencing.

## Retry Minimum

- Failure sebelum max attempts kembali menjadi `pending`.
- Failure pada max attempts menjadi `failed`.
- `seen_ids` mencegah hot retry di invocation yang sama.
- Invocation worker berikutnya menjadi retry paling cepat; pada timer satu menit, delay minimum praktis sekitar satu menit.
- Retry berbasis `available_at` dan exponential backoff hanya ditambahkan jika hasil operasional menunjukkan kebutuhan; itu memerlukan migration.

## Sanitasi Failure

- Jangan menyimpan `str(exception)` mentah ke kolom `error`.
- Simpan error code terkontrol seperti `solver_timeout`, `browser_error`, `upstream_unavailable`, atau `internal_error` beserta pesan generik.
- Detail exception hanya boleh masuk log server-side setelah sanitasi dan tanpa NRIC, Turnstile token, API key, DSN, URL bercredential, atau full upstream body.
- Endpoint synchronous dan result endpoint tidak boleh mengembalikan exception internal mentah.
- Tambahkan canary-secret test yang menyisipkan marker sensitif pada exception lalu memastikan marker tidak muncul pada log, kolom `error`, atau response.

## Summary Worker

Summary hanya menghitung sukses/gagal jika fenced update benar-benar berhasil.

Tambahkan outcome terpisah untuk lost claim agar race dapat terlihat di log tanpa membocorkan data job.

## File yang Diperkirakan Berubah

- `job_repository.py`
- `process_jobs.py`
- test integration PostgreSQL
- `README.md`

## Acceptance Criteria

- [ ] Worker concurrency satu tidak memiliki lebih dari satu job `processing` akibat preclaim sendiri.
- [ ] Dua claimant tidak mendapat row/attempt yang sama.
- [ ] Worker attempt lama tidak dapat menyelesaikan row yang sudah diklaim attempt baru.
- [ ] Worker tidak langsung memproses ulang job gagal dalam invocation yang sama.
- [ ] Attempt terakhir berubah menjadi `failed`.
- [ ] Crash meninggalkan paling banyak job yang benar-benar aktif sebagai `processing`.
- [ ] Summary tidak melaporkan sukses jika finalization mengupdate nol row.
- [ ] Exception canary yang berisi marker NRIC/token/credential tidak muncul pada DB, response, atau log.

---

# Phase 4 — Chrome, Xvfb, dan Concurrency Isolation

## Tujuan

Mencegah profile lock/corruption, tabrakan display, dan konsumsi resource tak terkendali.

## Chrome Profile

`TS_PROFILE_DIR` menjadi base directory, bukan satu profile bersama.

Setiap solve membuat profile unik menggunakan stdlib `tempfile`, PID, dan random suffix. Profile dibersihkan di `finally` setelah browser berhenti.

Hal yang harus diuji:

- dua solve concurrent tidak memakai path yang sama;
- API synchronous dan worker tidak berbagi profile;
- cleanup terjadi pada sukses maupun exception;
- fresh profile tetap berhasil melewati flow Turnstile nyata.

## Xvfb

Target production:

- Satu Xvfb dikelola service systemd terpisah.
- API dan worker menggunakan `DISPLAY=:99`.
- API/worker tidak mencoba menyalakan Xvfb sendiri pada display yang sama.
- `ENABLE_XVFB_VIRTUAL_DISPLAY=false` ketika Xvfb external dipakai.

Ini menghilangkan konflik antara Xvfb lifespan API, cron `xvfb-run`, dan proses lain.

## Global Chrome Slots

`MAX_WORKERS=1` pada API dan worker serial tetap dapat menghasilkan dua Chrome sekaligus karena keduanya berada di process berbeda. Sebelum go-live, semua jalur solve harus berbagi batas host-wide.

Minimum tanpa dependency baru:

- tambahkan `GLOBAL_CHROME_SLOTS=1` dan satu constant `CHROME_SLOT_BASE_KEY` di kode;
- slot adalah rentang advisory lock key stabil `CHROME_SLOT_BASE_KEY + slot_index`, dengan `slot_index` dari `0` sampai `GLOBAL_CHROME_SLOTS - 1`;
- API synchronous dan async worker mencoba setiap key dalam urutan yang diacak/dirotasi agar tidak selalu berebut slot pertama, menggunakan `pg_try_advisory_lock` pada dedicated DB connection;
- connection dan slot index disimpan sebagai handle; release menggunakan `pg_advisory_unlock` lalu connection ditutup dalam `finally`;
- jika tidak ada key yang tersedia, caller tidak membuka Chrome dan mengikuti bounded waiting policy;
- menurunkan jumlah slot hanya berlaku pada acquisition baru; lock lama tetap selesai lalu dilepas;
- jika slot sync tidak tersedia, request mengikuti bounded waiting policy lalu 429;
- worker yang tidak mendapat slot tidak mengklaim job, atau melepaskan claim dengan aman tanpa membakar attempt;
- slot key stabil dan berbeda dari advisory lock queue admission;
- failure DB pada slot acquisition gagal tertutup dan tidak membuka Chrome tanpa limit.

Alternatif yang lebih sederhana boleh dipilih: nonaktifkan endpoint synchronous di production dan pastikan hanya satu worker invocation dapat membuka Chrome. Namun karena kontrak sync diminta tetap tersedia, shared advisory slots adalah target plan.

Satu Xvfb dapat melayani beberapa Chrome jika benchmark membuktikan stabil; yang wajib unik adalah Chrome profile. Display terpisah baru diperlukan bila shared Xvfb gagal pada combined-load test.

## Concurrency Awal

Sebelum benchmark:

```env
MAX_WORKERS=1
GLOBAL_CHROME_SLOTS=1
SYNC_QUEUE_MAX_WAITING=0
```

Worker tetap serial. Jangan menambah `JOB_WORKER_CONCURRENCY` sebelum benchmark membuktikan kebutuhan. Jangan menaikkan Uvicorn `--workers`; service tetap satu process sampai Xvfb, browser profile, in-memory counters, dan resource budget terisolasi sepenuhnya.

## Endpoint Synchronous

`POST /api/solve/` tetap dipertahankan untuk kompatibilitas, tetapi waiting queue harus dibatasi:

- active solve maksimal `MAX_WORKERS`;
- waiting request maksimal `SYNC_QUEUE_MAX_WAITING`;
- overflow menghasilkan 429;
- semua counter dan semaphore selalu dilepas pada exception.

Rekomendasi production awal: `SYNC_QUEUE_MAX_WAITING=0`, sehingga burst diarahkan ke endpoint async.

## Acceptance Criteria

- [ ] Setiap solve memakai Chrome profile unik.
- [ ] Profile dibersihkan setelah sukses dan exception.
- [ ] API dan worker dapat memakai satu Xvfb external pada combined-load test; jika tidak, display dipisahkan.
- [ ] Tidak ada proses yang mencoba menyalakan display yang sama.
- [ ] Aggregate Chrome API + worker tidak pernah melebihi `GLOBAL_CHROME_SLOTS`.
- [ ] Sync active solve tidak melebihi `MAX_WORKERS` maupun global slots.
- [ ] Sync waiting tidak melebihi `SYNC_QUEUE_MAX_WAITING`.
- [ ] Worker tetap serial sebelum benchmark menyetujui scale-up.
- [ ] Slot selalu dilepas setelah sukses, exception, timeout, dan cancellation.
- [ ] Dengan `GLOBAL_CHROME_SLOTS=2`, dua solve dapat berjalan dan solve ketiga menunggu/ditolak tanpa membuka Chrome.
- [ ] Semua slot key berada pada rentang base key yang terdokumentasi dan tidak bertabrakan dengan queue admission lock.

---

# Phase 5 — API Security dan Proxy Trust

## Tujuan

Memastikan hanya client dengan key valid dan source IP yang diizinkan dapat memakai API, tanpa mempercayai header yang dapat dipalsukan.

## Layer Akses

Request bisnis harus melewati seluruh kontrol berikut:

1. Host valid.
2. Immediate proxy tepercaya.
3. Resolved client IP berada di allowlist.
4. API key valid.
5. Queue memiliki capacity.

## Trusted Proxy

Gunakan trusted proxy support native Uvicorn melalui `FORWARDED_ALLOW_IPS`; jangan menulis parser `X-Forwarded-For` sendiri di business logic.

Untuk chain saat ini:

```text
Internet -> Cloudflare -> cloudflared/nginx -> Uvicorn loopback
```

Aturan:

- Origin tidak boleh dapat diakses langsung dari internet.
- Gunakan Unix socket nginx khusus CapSolve; TCP loopback saja tidak diterima sebagai trust boundary production.
- Permission socket dibatasi sehingga hanya group cloudflared yang dapat connect, dan user lain tidak dapat menulis.
- cloudflared meneruskan ke Unix socket tersebut dan tidak langsung ke Uvicorn.
- nginx membuang nilai masuk `Forwarded`, `X-Forwarded-For`, `X-Real-IP`, dan `CF-Connecting-IP` dari jalur app-facing; jangan append header client mentah.
- Pilih satu canonical source IP. Jika cloudflared menyediakan `CF-Connecting-IP`, nginx hanya menerimanya pada listener yang eksklusif untuk local cloudflared, memvalidasi format IP, lalu membentuk `X-Forwarded-For` baru dengan nilai tunggal tersebut.
- Jika Unix socket berpermission tidak dapat digunakan, gunakan authenticated/network-isolated side channel yang memberikan identity boundary setara. Deployment tidak boleh lanjut hanya dengan TCP loopback.
- IP allowlist aplikasi tetap wajib; Cloudflare Access/WAF boleh menjadi lapisan tambahan, bukan pengganti, kecuali arsitektur dan acceptance criteria secara eksplisit direvisi.
- Uvicorn bind pada Unix socket/loopback terpisah dan hanya mempercayai direct nginx peer melalui `FORWARDED_ALLOW_IPS` yang sempit.
- Aplikasi memeriksa `request.client.host` yang sudah di-resolve oleh trusted proxy layer.
- `FORWARDED_ALLOW_IPS=*` dilarang di production.
- Process lokal yang tidak berwenang tidak boleh dapat menulis ke socket nginx atau Uvicorn; listener loopback tanpa permission boundary saja tidak dianggap autentikasi cloudflared.

## IP Allowlist Development dan Production

Development:

```env
ENVIRONMENT=development
API_IP_ALLOWLIST=*
```

Production:

```env
ENVIRONMENT=production
API_IP_ALLOWLIST=<public-ip-server-pemanggil>/32
```

Beberapa IP/CIDR dipisahkan koma. Pergantian daftar membutuhkan restart API.

## API Key Production dan Rotation

- Generate key production dengan `python -c "import secrets; print(secrets.token_urlsafe(32))"` atau secret manager setara.
- Simpan hanya di secret file/service environment.
- Untuk rotasi:
  1. deploy `API_KEYS=old,new`;
  2. client berpindah ke key baru;
  3. verifikasi traffic key baru;
  4. deploy `API_KEYS=new`;
  5. cabut key lama.
- Jangan mengirim key melalui chat/log setelah provisioning bila tersedia jalur secret yang lebih aman.

## Docs

Production default:

```env
API_DOCS_ENABLED=false
```

Saat false, seluruh endpoint berikut tidak diregistrasikan:

- `/docs`
- `/redoc`
- `/openapi.json`

Jika docs perlu sementara, aktifkan hanya dalam maintenance window dan tetap lindungi dengan allowlist.

## Acceptance Criteria

- [ ] API key benar + IP salah menghasilkan 403.
- [ ] IP benar + API key salah menghasilkan 401.
- [ ] Header forwarding palsu dari peer tidak tepercaya tidak dapat melewati allowlist.
- [ ] Forged `Forwarded`, `X-Forwarded-For`, `X-Real-IP`, dan `CF-Connecting-IP` diuji melalui direct Uvicorn, nginx listener, dan jalur tunnel; hanya canonical header dari hop terautentikasi yang berpengaruh.
- [ ] Process lokal tanpa permission yang tepat tidak dapat memakai socket trusted nginx/Uvicorn.
- [ ] IPv4, IPv6, exact IP, dan CIDR bekerja.
- [ ] Production wildcard gagal startup.
- [ ] Docs, ReDoc, dan OpenAPI tidak tersedia ketika disabled.
- [ ] Origin Uvicorn hanya listen loopback.
- [ ] Origin nginx/tunnel route tidak terbuka langsung pada port public yang tidak diperlukan.

---

# Phase 6 — Health, Readiness, dan Worker Freshness

## Tujuan

Membedakan process hidup dari service siap menerima pekerjaan.

## Liveness

Pertahankan `GET /api/health` sebagai shallow liveness:

- tidak query database;
- tidak membuka Chrome;
- tidak memanggil website/upstream;
- 200 selama process API hidup.

Field `workers`, `active`, dan `queued` harus diberi nama/dokumentasi jelas sebagai counter synchronous per-process, bukan PostgreSQL queue depth.

## Readiness

Tambahkan `GET /api/ready`:

- melakukan `SELECT 1` dengan `DB_CONNECT_TIMEOUT` kecil;
- memastikan konfigurasi wajib sudah tervalidasi saat startup;
- 200 jika API dapat melayani submit/result;
- 503 generic jika database tidak tersedia;
- tidak memanggil upstream BUDI95;
- queue penuh tidak membuat readiness gagal karena endpoint result masih dapat dipakai.

## Worker Freshness

Gunakan mekanisme minimum yang dapat diawasi dari systemd dan log:

- worker summary per invocation;
- timestamp invocation terakhir;
- queue depth;
- oldest pending age;
- jumlah stale processing;
- exit status service worker.

Heartbeat endpoint atau tabel tambahan tidak dibuat sebelum monitoring systemd/timer terbukti tidak cukup.

## Acceptance Criteria

- [ ] Health tetap 200 saat DB mati.
- [ ] Ready cepat menghasilkan 503 saat DB mati.
- [ ] Ready tidak membocorkan exception DB.
- [ ] Queue penuh tidak membuat ready gagal.
- [ ] Worker yang tidak berjalan dapat dideteksi dari timer/service status atau freshness log.

---

# Phase 7 — Privacy, Retention, dan Database

## Tujuan

Membatasi umur NRIC dan hasil lookup serta memastikan database dapat dipulihkan.

## Keputusan Terbuka yang Wajib Ditutup

Sebelum go-live, owner harus memilih retensi terminal job, misalnya:

- 24 jam untuk kebutuhan polling/debug singkat;
- 7 hari jika investigasi operasional membutuhkan waktu lebih panjang;
- nilai lain yang memiliki alasan bisnis dan persetujuan data owner.

Production tidak boleh berjalan dengan retensi tak terbatas hanya karena keputusan belum dibuat. Setelah Phase 7 diterapkan, production preflight/startup menolak `JOB_RETENTION_HOURS` yang kosong, nol, negatif, atau non-integer. Rollout juga diblokir jika purge timer belum enabled/active atau backup retention belum diselaraskan.

## Purge

Setelah retensi diputuskan:

- Tambahkan `JOB_RETENTION_HOURS`.
- Buat `purge_jobs.py` dan entry point `capsolve-purge-jobs` di `pyproject.toml`.
- Command hanya memilih `success` dan `failed` dengan cutoff berdasarkan `processed_at`, urutan `processed_at, id`, dan batch limit eksplisit.
- Jangan menghapus `pending` atau `processing`.
- Sediakan `--dry-run` dan `--limit`.
- Delete dibuat idempotent; rerun dengan cutoff yang sama aman.
- Jalankan melalui `capsolve-purge.service` dan `capsolve-purge.timer`.
- Production preflight memastikan timer enabled dan jadwalnya lebih sering daripada retensi minimum yang dipilih.
- Log hanya count dan cutoff, bukan NRIC/result body.

## Database Network

PostgreSQL server saat audit listen pada semua interface. Sebelum mengubahnya:

1. inventaris semua aplikasi server yang memakai PostgreSQL;
2. jika seluruh consumer lokal, bind ke loopback;
3. jika ada consumer remote, bind ke private interface dan batasi dengan firewall serta `pg_hba.conf`;
4. jangan mengekspos port 5432 ke internet;
5. gunakan user/database khusus CapSolve dengan privilege minimum.

## Backup

- Backup otomatis terjadwal.
- Tambahkan `BACKUP_RETENTION_HOURS` pada production preflight evidence atau file state root-owned yang dihasilkan konfigurasi backup.
- `BACKUP_RETENTION_HOURS` harus integer positif dan tidak melebihi retention policy yang disetujui data owner, kecuali backup terenkripsi memiliki policy legal terpisah yang terdokumentasi.
- Preflight memeriksa nilai/evidence tersebut dan timestamp backup sukses terakhir; evidence tidak boleh berisi credential.
- Backup memiliki retention.
- Credential backup tidak masuk command history/log.
- Restore test dilakukan berkala.
- Dokumentasikan RPO dan RTO minimum.

## Acceptance Criteria

- [ ] Retensi telah disetujui dan `JOB_RETENTION_HOURS` production tervalidasi.
- [ ] Production preflight gagal jika retensi kosong/invalid, purge timer tidak aktif, atau backup retention belum selaras.
- [ ] Purge hanya menghapus terminal jobs melewati cutoff.
- [ ] Purge mematuhi urutan dan batch limit yang deterministic.
- [ ] Purge dry-run tidak mengubah data dan rerun bersifat idempotent.
- [ ] Full NRIC tidak muncul di log purge.
- [ ] PostgreSQL tidak dapat diakses dari internet.
- [ ] Backup dan restore test berhasil.
- [ ] User DB CapSolve tidak memiliki privilege superuser.

---

# Phase 8 — systemd, nginx, Secret, dan Host Hardening

## Secret dan Permission

Target permission:

```text
.env                         600
project directory            750 atau lebih ketat
logs directory               750
log files                    640 atau lebih ketat
systemd UMask                0077
```

`.env` mode `600` berarti hanya owner dapat membaca dan menulis. Jangan menyimpan key production di unit file yang world-readable.

## systemd API

Target service:

- bind Uvicorn ke Unix socket kedua yang permission-nya hanya mengizinkan user/group nginx; TCP loopback tidak digunakan sebagai production trust boundary;
- jalankan binary langsung dari `.venv`, bukan `uv run`, setelah deployment melakukan `uv sync --frozen`;
- `Restart=on-failure`;
- startup/restart limit;
- `UMask=0077`;
- memory/task limits berdasarkan benchmark;
- `NoNewPrivileges=true` jika kompatibel dengan Chrome deployment;
- filesystem protection diterapkan bertahap dengan `ReadWritePaths` yang diperlukan;
- jangan mengaktifkan hardening yang mematahkan Chrome sandbox/Xvfb tanpa smoke test.

## systemd Worker

Ganti cron worker menjadi pasangan:

```text
capsolve-worker.service
capsolve-worker.timer
```

Keuntungan:

- invocation unit yang sama tidak overlap;
- exit status terlihat di systemd;
- log masuk journal;
- dependency dan environment konsisten;
- tidak membutuhkan `flock` tambahan bila satu service unit dipakai.

Timer awal tetap satu menit. `JOB_BATCH_LIMIT` menentukan maksimum drain per invocation.

## Pemisahan Environment

Gunakan file environment berbeda dengan permission `600` dan validation profile per component:

- API environment: API keys, allowlist, allowed hosts, proxy settings, queue admission, DB credential minimum, `GLOBAL_CHROME_SLOTS`, serta solver/upstream/Chrome/display setting karena `/api/solve/` tetap tersedia.
- Worker environment: solver/upstream config, worker settings, `GLOBAL_CHROME_SLOTS`, Chrome/display, dan DB credential minimum; tidak menerima inbound API key atau allowlist yang tidak dibutuhkan.
- Purge environment: retention, batch limit, dan DB delete credential minimum; tidak menerima API key atau solver config.
- API startup menjalankan API profile validation; worker dan purge menjalankan profile masing-masing, sehingga absence API key pada worker bukan error.
- Shared non-secret values boleh berada pada file common yang terpisah.
- Jika API dan worker memakai DB user berbeda, API mendapat create/read privilege yang diperlukan, worker mendapat claim/update/read privilege, dan purge memakai role khusus delete.

## systemd Xvfb

Sediakan satu unit Xvfb terpisah dan buat API/worker bergantung padanya. Gunakan satu display yang terdokumentasi.

## nginx dan Cloudflare

- Route CapSolve tunnel menuju Unix socket ingress nginx yang permission-nya hanya mengizinkan user/group cloudflared.
- nginx meneruskan ke Unix socket Uvicorn terpisah yang permission-nya hanya mengizinkan user/group nginx dan service CapSolve.
- Tidak ada TCP loopback listener yang dianggap trust boundary; jika Unix socket tidak tersedia, gunakan authenticated isolation setara dan review ulang acceptance test proxy.
- Set request timeout sesuai endpoint async; endpoint submit/result tidak memerlukan timeout sepanjang solve synchronous.
- Endpoint synchronous harus memiliki timeout proxy yang eksplisit jika tetap digunakan.
- Terapkan request rate/burst limit di gateway sebagai lapisan tambahan, bukan pengganti queue capacity.
- Pastikan proxy mengganti forwarding headers sesuai trust chain.

## Log Rotation

- Journal memiliki retention/size policy.
- Jika file log tetap digunakan, buat rule logrotate.
- Jangan membiarkan `cron_worker.log` tumbuh tanpa batas.
- Hindari duplikasi log yang sama ke journal dan file kecuali ada alasan operasional.

## Acceptance Criteria

- [ ] `.env` mode 600.
- [ ] Service tidak berjalan sebagai root.
- [ ] Uvicorn tidak listen pada interface public.
- [ ] Listener nginx CapSolve hanya tersedia melalui jalur yang dibutuhkan.
- [ ] Worker invocation tidak overlap.
- [ ] API dan worker memakai environment terpisah; masing-masing hanya menerima secret dan setting yang dibutuhkan.
- [ ] Shared values konsisten dan seluruh config tervalidasi.
- [ ] Restart API tidak meninggalkan Xvfb/Chrome orphan.
- [ ] Log memiliki size/retention policy.
- [ ] `systemd-analyze security` membaik tanpa mematahkan smoke test.

---

# Phase 9 — Observability

## Metric Minimum

Tanpa menambah platform observability baru, data berikut harus dapat dihitung dari SQL dan structured logs:

- queue depth `pending + processing`;
- pending count;
- processing count;
- oldest pending age;
- stale processing count;
- submit accepted;
- submit rejected karena queue full;
- success count;
- failed count;
- retry count;
- lost claim count;
- median dan p95 solve duration;
- config source `cache`, `website`, atau `env`;
- worker invocation terakhir dan exit status;
- API restart count;
- memory dan task count service.

## Alert Minimum

Alert atau pemeriksaan operasional diperlukan ketika:

- queue mencapai 80% capacity;
- oldest pending melewati SLA;
- worker tidak sukses dijalankan dalam beberapa interval timer;
- failed ratio melewati threshold hasil baseline;
- stale processing ditemukan;
- readiness gagal;
- disk, memory, atau inode mendekati batas;
- config resolver terus fallback atau gagal.

NRIC, API key, Turnstile token, DB password, DSN, URL bercredential, dan full upstream response tidak boleh menjadi label atau field log operasional. Kolom database `error` dan **failure response** hanya berisi error code terkontrol serta pesan generik. Payload sukses result/upstream yang sudah menjadi kontrak API tetap dipertahankan dan hanya dikirim ke caller terautentikasi; payload sukses tersebut tetap tidak boleh ditulis utuh ke log.

## Acceptance Criteria

- [ ] Operator dapat melihat queue depth dan oldest pending tanpa membaca NRIC.
- [ ] Queue full tercatat sebagai count/event.
- [ ] Worker summary berupa format stabil yang dapat diparse.
- [ ] Lost claim terlihat terpisah dari solver failure.
- [ ] Tidak ada sensitive value pada log normal maupun exception.

---

# Phase 10 — Test dan Quality Gates

## Test Tanpa Dependency Baru

Gunakan `unittest`, assert-based self-check, dan PostgreSQL test database existing bila cukup.

## Unit/Self-Check

- config parsing dan validation;
- API key precedence dan comparison;
- IP/CIDR parsing;
- production wildcard rejection;
- docs toggle;
- NRIC boundary validation;
- response mapping 429/503;
- Chrome profile uniqueness helper.

## PostgreSQL Integration

- capacity race dengan beberapa connection;
- exact outstanding count;
- concurrent claim menghasilkan row berbeda;
- fencing old attempt versus new attempt;
- last attempt menjadi failed;
- stale reset;
- success/failed membebaskan queue slot;
- readiness query timeout/failure.

## Golden Contract dan HTTP Integration

Capture fixture kontrak sebelum implementasi dan bandingkan setelahnya untuk:

- `/api/solve/` sukses dan setiap public failure contract existing;
- `/api/budi95/config` normal dan force-refresh;
- `/api/budi95` dan `/api/budi95/` sukses;
- result `pending`, `processing`, `success`, `failed`, dan not-found;
- `/api/health`.

Fixture sukses/non-leaking state dibandingkan exact. Failure yang sebelumnya membocorkan internals memakai approved before/after fixture: expected after adalah status terdokumentasi, error code terkontrol, pesan generik, dan tidak ada marker sensitif. Test tambahan mencakup:

- valid key + allowed IP + available queue;
- valid key + blocked IP;
- invalid key + allowed IP;
- queue full;
- DB unavailable;
- trailing slash compatibility;
- docs disabled;
- liveness dan readiness;
- exception canary tidak muncul pada log, DB error, atau response;
- forged client-IP header pada setiap hop proxy;
- combined API synchronous + worker tidak melewati global Chrome slots.

## Runtime Smoke Test

- Xvfb tersedia;
- Chrome dapat start dan stop;
- profile unik dibersihkan;
- dynamic BUDI95 config resolve bekerja;
- satu approved test NRIC menyelesaikan flow end-to-end;
- result endpoint mengembalikan kontrak existing.

## CI Minimum

```bash
uv sync --frozen
uv run python -m compileall service.py solver.py config_resolver.py database.py job_repository.py process_jobs.py chrome_slots.py purge_jobs.py production_preflight.py
uv run capsolve-self-check
uv run python -m unittest discover -s tests -p "test_unit_*.py"
uv run python -m unittest discover -s tests -p "test_contract_*.py"
TEST_DATABASE_URL=postgresql://... uv run python -m unittest discover -s tests -p "test_postgres_*.py"
```

Nama module boleh berubah bila implementasi menggabungkan helper secara lebih sederhana, tetapi quality gate harus mengompilasi seluruh source tracked dan menjalankan ekuivalen unit, contract HTTP, dan PostgreSQL integration suite. Database integration wajib disposable/terisolasi, bukan production.

## Acceptance Criteria

- [ ] Semua check deterministic lulus.
- [ ] Test tidak membutuhkan API key/credential production.
- [ ] Test race benar-benar memakai connection terpisah.
- [ ] Exact golden invariants lulus dan approved failure-hardening delta cocok expected generic error baru.
- [ ] Test proxy tidak mempercayai forged forwarding header pada direct origin maupun trusted-hop listener.
- [ ] Canary secret/PII tidak muncul pada log, DB, atau response.
- [ ] Combined API/worker test membuktikan aggregate Chrome slot limit.
- [ ] Tidak ada test yang menghapus row production yang bukan miliknya.

---

# Phase 11 — Benchmark dan Penetapan Capacity

## Prasyarat

- Queue bounded dan fencing sudah diterapkan.
- Chrome profile sudah terisolasi.
- Tersedia NRIC test yang disetujui data owner.
- Upstream test diperbolehkan dan tidak melanggar rate limit/kebijakan.
- Monitoring resource aktif.

## Baseline

Mulai dengan:

```env
MAX_WORKERS=1
JOB_BATCH_LIMIT=5
SYNC_QUEUE_MAX_WAITING=0
```

Jalankan minimal tiga ronde dengan workload tetap, masing-masing minimal 30 job atau 15 menit agar hasil dapat dibandingkan. Ukur:

- successful completions per minute sebagai throughput aktual;
- submitted, success, failure, dan retry count;
- waktu solve median dan p95;
- end-to-end queue latency median dan p95;
- CPU peak dan sustained;
- memory peak dan sustained;
- jumlah proses/task Chrome;
- waktu drain queue;
- efek terhadap aplikasi lain di server;
- combined workload ketika endpoint synchronous dan worker aktif bersamaan.

## Burst Test

Uji bertahap, bukan langsung maksimum:

1. 10 submit dalam burst.
2. 30 submit dalam burst.
3. 60 submit dalam burst hanya jika dua tahap sebelumnya stabil.
4. Burst melebihi capacity untuk memastikan 429 konsisten.
5. Restart API saat queue masih berisi job.
6. Hentikan worker saat satu job processing, lalu verifikasi stale recovery dan fencing.

## Menentukan Throughput

Gunakan hasil aktual dan laporkan tiga angka terpisah:

```text
async_throughput = successful_async_terminal_jobs / wall_clock_minutes
sync_throughput = successful_sync_responses / wall_clock_minutes
aggregate_throughput = (successful_async_terminal_jobs + successful_sync_responses) / wall_clock_minutes
```

Hitung retry dan failed attempt sebagai biaya workload, bukan menghilangkannya dari denominator. Combined workload report wajib memuat ketiga angka agar traffic synchronous tidak tersembunyi oleh metric async. Rumus `60 / median_solve_seconds` hanya boleh dipakai sebagai sanity check kasar, bukan angka capacity.

Concurrency hanya dinaikkan jika:

- global Chrome slots membatasi gabungan API dan worker;
- profile unik dan shared Xvfb lulus combined-load test, atau display dipisahkan;
- available memory tetap minimal 30% dan tidak memakai swap selama steady load;
- sustained CPU tetap di bawah 70% total host agar aplikasi lain memiliki headroom;
- success rate tidak turun lebih dari threshold yang disepakati dari baseline;
- upstream tidak menolak/rate-limit;
- p95 dan queue latency tetap di bawah SLA yang disepakati.

## Menentukan Queue Capacity

Capacity final dipilih dari:

- burst maksimum yang ingin diterima;
- service rate hasil benchmark;
- SLA waktu hasil;
- batas backlog yang masih aman;
- dampak penyimpanan NRIC.

Jangan menetapkan capacity sangat besar hanya agar submit tidak ditolak. Queue besar memindahkan kegagalan menjadi waktu tunggu panjang.

## Exit Criteria Scale-Up

- [ ] Async, sync, dan aggregate successful completions per minute tercatat untuk minimal tiga workload tetap.
- [ ] Success rate memenuhi baseline yang disepakati setelah retry/failure dihitung.
- [ ] CPU, memory, swap, dan task headroom memenuhi batas numerik plan.
- [ ] Combined API/worker load tidak melewati global Chrome slots.
- [ ] Tidak ada duplicate final state.
- [ ] Tidak ada profile lock/corruption.
- [ ] Tidak ada stale overwrite.
- [ ] Memory dan task count kembali turun setelah test.
- [ ] Oldest pending tetap di bawah SLA.
- [ ] 429 muncul ketika capacity benar-benar penuh.
- [ ] Nilai production `JOB_QUEUE_CAPACITY`, `JOB_BATCH_LIMIT`, dan concurrency terdokumentasi.

---

# Phase 12 — Rollout Production

## Urutan Rollout

1. Merge code dan test yang telah lulus.
2. Backup database dan konfigurasi server; tulis evidence backup timestamp/retention tanpa secret.
3. Deploy code dengan dependency locked/frozen.
4. Terapkan migration hanya jika ada dan jalankan dry-run terlebih dahulu.
5. Pasang unit Xvfb, worker, dan purge, lalu `systemctl daemon-reload`; jangan buka traffic.
6. Putuskan retensi dan set `JOB_RETENTION_HOURS` serta `BACKUP_RETENTION_HOURS`.
7. Set permission directory dan file environment terpisah.
8. Set API key production, allowlist IP client, allowed hosts, proxy trust, `GLOBAL_CHROME_SLOTS`, dan solver runtime config.
9. Set `ENVIRONMENT=production` dan docs disabled.
10. Enable Xvfb, worker timer, dan purge timer tanpa men-start timer mutating; start hanya Xvfb.
11. Jalankan static production preflight; hentikan rollout jika config, secret permission, unit enabled state, backup evidence, retention, allowlist, atau docs policy gagal.
12. Start worker/purge timer, lalu jalankan runtime preflight untuk memverifikasi timer active dan jadwal valid sebelum traffic dibuka.
13. Restart API.
14. Verifikasi health dan readiness dari origin.
15. Verifikasi jalur Cloudflare/nginx dengan key/IP test yang benar.
16. Submit satu smoke job.
17. Verifikasi worker memproses dan result dapat dipoll.
18. Aktifkan traffic client secara bertahap.
19. Pantau queue, error, memory, Chrome, dan DB selama stabilization window.

## Canary

Jika memungkinkan:

- client mulai dengan volume rendah;
- jangan langsung menaikkan concurrency;
- gunakan capacity konservatif;
- rotasi key hanya setelah flow baru stabil;
- pertahankan config rollback sampai stabilization window selesai.

## Rollback

Rollback aplikasi:

1. hentikan traffic submit atau arahkan ke maintenance response;
2. hentikan worker timer;
3. biarkan row existing tetap di PostgreSQL;
4. kembalikan commit dan unit sebelumnya;
5. restart API;
6. verifikasi result endpoint dan queue state;
7. jangan menghapus pending/processing untuk mempercepat rollback.

Jika schema migration additive diterapkan, versi lama harus tetap kompatibel dengan kolom baru. Hindari destructive migration pada rollout ini.

## Acceptance Criteria

- [ ] Satu smoke submit sukses end-to-end.
- [ ] Unauthorized key ditolak.
- [ ] IP yang tidak diizinkan ditolak.
- [ ] Docs tidak public.
- [ ] Queue metrics dapat dibaca.
- [ ] Worker timer aktif dan tidak overlap.
- [ ] Production preflight dijalankan setelah seluruh config dan unit aktif, serta membuktikan retensi, purge timer, backup retention evidence, secret, allowlist, dan docs policy valid.
- [ ] Rollback procedure telah direview operator.

---

# Production Go-Live Checklist

Items marked **code** are implemented and covered by local unit/contract tests (quality gate PASS without disposable Postgres). Items marked **ops** stay open until an operator verifies them on the deploy host.

## Security

- [ ] **ops** API key production random dan berbeda dari development.
- [ ] **ops** `.env` / component env mode 600.
- [ ] **ops** `ENVIRONMENT=production`.
- [ ] **ops** `API_IP_ALLOWLIST` berisi IP/CIDR eksplisit.
- [ ] **ops** `ALLOWED_HOSTS` hanya hostname production yang diperlukan.
- [ ] **ops** `FORWARDED_ALLOW_IPS` hanya direct proxy tepercaya.
- [x] **code** API docs disabled path enforced when `API_DOCS_ENABLED=false` / production.
- [x] **code** Production rejects TCP `API_HOST`; requires permission-bound UDS.
- [ ] **ops** Uvicorn UDS chain installed; no public TCP origin.
- [ ] **ops** PostgreSQL tidak public.
- [x] **code** Response/log sanitization unit-proven (no secret/NRIC in controlled events).

## Queue dan Worker

- [x] **code** Queue capacity atomik (unit + contract).
- [x] **code** Queue full menghasilkan 429.
- [x] **code** DB failure menghasilkan 503.
- [x] **code** Claim just-in-time (unit).
- [ ] **ops**/integration Fencing stale claim lulus disposable-Postgres integration test on CI/host.
- [x] **code** Worker oneshot non-overlap designed in systemd timer units.
- [ ] **ops** Worker timer installed and proven non-overlapping on host.
- [x] **code** Aggregate Chrome slots settings + unit release paths.
- [ ] **ops**/integration Aggregate slots proven under real API+worker Postgres test.
- [x] **code** Chrome profile unik (unit).
- [ ] **ops** Xvfb external stabil on host.
- [ ] **ops** Concurrency sesuai hasil benchmark (defer for soft launch: keep slots=1).

## Reliability

- [x] **code** Health dan readiness benar (unit).
- [x] **code** Timeout DB, solver, dan upstream eksplisit di settings.
- [ ] **ops** Backup dan restore terbukti (Phase 0 capture/restore-test).
- [ ] **ops** Restart API/worker diuji saat queue berisi job.
- [ ] **ops** Resource limits tidak mematahkan Chrome.

## Privacy

- [ ] **ops** Retensi NRIC/hasil telah disetujui (set `JOB_RETENTION_HOURS`).
- [x] **code** Production requires retention; purge tool + preflight alignment checks exist.
- [ ] **ops** Purge timer aktif.
- [ ] **ops** Backup retention selaras dengan data retention.
- [ ] **ops** Akses database dan backup dibatasi.

## Operations

- [x] **code** Queue depth / oldest pending available without NRIC (metrics paths unit-proven).
- [x] **code** Worker freshness checker present.
- [ ] **ops** Queue depth dan oldest pending termonitor in production.
- [ ] **ops** Worker freshness termonitor in production.
- [x] **code** Journald retention artifact example present.
- [ ] **ops** Journal retention installed/active.
- [ ] **ops** Alert threshold ditetapkan.
- [x] **code** Runbook restart, rollback, key rotation, dan queue recovery di `deployment/README.md`.
- [ ] **ops** Runbook direview operator sebelum cutover.

---

# File-Level Implementation Map

| File/Area | Rencana perubahan |
| --- | --- |
| `service.py` | Config validation, docs toggle, IP middleware, host validation, bounded sync queue, readiness, error mapping 429/503 |
| `job_repository.py` | Atomic capacity admission, JIT claim support, attempts fencing, conditional finalization |
| `process_jobs.py` | Claim-process loop satu per satu, seen IDs, strict env validation, lost-claim summary |
| `solver.py` | Unique temporary Chrome profile dan cleanup |
| `database.py` | Connect timeout dan readiness-safe connection handling |
| `chrome_slots.py` atau helper setara | PostgreSQL advisory-lock slots yang dipakai API dan worker |
| `purge_jobs.py` | Dry-run/batched purge terminal jobs berdasarkan retention cutoff |
| `production_preflight.py` | Gate config production, retention, timer, secret permission, docs, dan allowlist |
| `pyproject.toml` | Entry point purge, preflight, dan checks yang diperlukan |
| `self_check.py` | Config, security, docs, response, sanitasi, dan helper checks |
| test integration PostgreSQL/HTTP | Capacity race, fencing, global slots, retention, golden contract, proxy trust |
| `sql/` | Tidak berubah untuk scope minimum; migration additive hanya jika retry scheduling membutuhkannya |
| `.env.example` | Seluruh env baru beserta development-safe placeholder |
| `README.md` | Queue semantics, error contract, proxy trust, deployment, worker, health/readiness, dan restart semantics |
| systemd | Unit API, Xvfb, worker service/timer, `capsolve-purge.service/timer`, preflight `ExecStartPre`/deployment gate, hardening, resource limit |
| nginx/Cloudflare | Loopback origin, trusted client IP chain, host routing, optional gateway rate limit |
| PostgreSQL | Network restriction, least privilege, backup/restore, monitoring query |

---

# Hal yang Sengaja Tidak Dibangun Sekarang

- Redis atau message broker baru: PostgreSQL existing sudah cukup untuk queue awal.
- Kubernetes: tidak diperlukan untuk satu host.
- Exactly-once processing: tidak realistis tanpa idempotency upstream.
- Custom proxy-header parser: gunakan Uvicorn dan nginx native behavior.
- Hot reload `.env`: restart eksplisit lebih mudah diaudit dan lebih aman.
- Dashboard observability baru: mulai dari structured logs, SQL, dan systemd.
- Concurrency tinggi: ditunda sampai benchmark membuktikan kebutuhan dan keamanan resource.
- Schema lease/backoff kompleks: attempts fencing dan JIT claim menjadi minimum pertama; migration ditambahkan hanya bila data operasional membutuhkannya.

---

# Recommended Implementation Order

1. Phase 0 — baseline, backup, dan rollback.
2. Phase 1 — config validation dan env contract.
3. Phase 2 — atomic queue capacity dan HTTP 429/503.
4. Phase 3 — JIT claim dan attempts fencing.
5. Phase 4 — Chrome profile dan Xvfb isolation.
6. Phase 5 — API key, allowlist, trusted proxy, Host, dan docs.
7. Phase 6 — health/readiness dan worker freshness.
8. Phase 7 — keputusan retention, purge, dan DB security.
9. Phase 8 — systemd/nginx/secret/log hardening.
10. Phase 9 — observability.
11. Phase 10 — seluruh quality gates.
12. Phase 11 — benchmark dan penetapan capacity/concurrency.
13. Phase 12 — canary rollout dan stabilization.

Setiap phase harus diimplementasikan dalam diff kecil, diaudit terhadap acceptance criteria, dan tidak dilanjutkan jika correctness phase sebelumnya belum terbukti.
