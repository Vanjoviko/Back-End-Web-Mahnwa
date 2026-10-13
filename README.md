# Back-End-Web-Mahnwa

Worker **scan-import** untuk Akuma: scan metadata komik dari sumber yang diizinkan, lalu mengunduh gambar tiap chapter
**terurut** (`001.<ext>`, `002.<ext>`, …) ke staging. Server Node FE ([Front-End-Web-Mahnwa](https://github.com/Vanjoviko/Front-End-Web-Mahnwa))
adalah satu-satunya klien: FE mem-proxy `/api/admin/scan/*`, menarik hasil unduhan, lalu menyimpannya ke `data/media`.
`db.json` FE adalah sumber kebenaran; worker ini **stateless** (state job hanya in-memory + staging ber-TTL).

> Sumber konten: adapter yang disertakan adalah `manifest`, `fixture` (pengujian lokal), `kiryuu`, dan
> `shinigami` (JSON API publik `api.shngm.io`). Hanya ambil konten yang pemiliknya mengizinkan.
> Tidak ada daftar host yang diizinkan: sumber publik apa pun boleh di-scan, selama tidak mengarah ke jaringan privat/lokal.

## Menjalankan

Perlu Python 3.11+ dan PostgreSQL. Database diakses lewat SQLAlchemy ORM; skema
dibuat lewat **Alembic** (bukan `create_all`), jadi pindah dari Postgres lokal ke
Postgres produksi cukup mengganti `DATABASE_URL`.

### Cara cepat: Docker Compose (PostgreSQL + API)

```bash
cp .env.example .env.dev        # lalu isi POSTGRES_PASSWORD, WORKER_TOKEN, dll.
docker compose --env-file .env.dev up -d --build
# service `api` menunggu `db` sehat, menjalankan `alembic upgrade head`, lalu `python -m app`
curl http://127.0.0.1:8000/health
```

Postgres dipublish di port host **5433** (menghindari Postgres lokal di 5432) dan
user/database dibuat otomatis dari `POSTGRES_*` — tanpa `CREATE ROLE` manual.
Perintah lain:

```bash
docker compose --env-file .env.dev logs -f api                          # log
docker compose --env-file .env.dev run --rm api alembic upgrade head     # migrasi manual
docker compose --env-file .env.dev down                                  # hentikan (data tetap di volume pgdata)
docker compose --env-file .env.dev down -v                               # hentikan + hapus data
```

Produksi: pakai `--env-file .env.prod` (semua nilai diisi, `SCAN_DEV_MODE=false`, `AUTH_COOKIE_SECURE=true`).

### Cara langsung (tanpa Docker)

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env.dev                                  # file env; APP_ENV memilih .env.dev / .env.prod
export DATABASE_URL="postgresql+psycopg://user:pass@localhost:5433/mahnwa"
alembic upgrade head                                      # buat/ubah skema
python -m app                                             # http://127.0.0.1:8000
```

`python -m app` memuat file env otomatis: `ENV_FILE` bila diset, selain itu
`.env.<APP_ENV>` (default `dev`), lalu `.env` sebagai cadangan. Variabel yang sudah
ada di shell menang, jadi `WORKER_TOKEN=... python -m app` tetap meng-override.
Worker menolak start bila `WORKER_TOKEN` kosong (kecuali `SCAN_DEV_MODE=true`, hanya
untuk pengembangan lokal).

### Migrasi database (Alembic)

Skema fitur auth/bookmark dikelola Alembic (`alembic/versions/`). Tabel katalog
legacy (`mangas`, `chapters`, `chapter_pages`) tetap dikelola jalur
`ENABLE_LEGACY_API` dan tidak diikutkan di migrasi ini.

```bash
export DATABASE_URL="postgresql+psycopg://user:pass@localhost:5433/mahnwa"
alembic upgrade head          # terapkan semua migrasi
alembic current               # revisi aktif
alembic check                 # pastikan model & database sinkron
alembic downgrade -1          # mundur satu revisi
alembic revision --autogenerate -m "pesan"   # revisi baru dari perubahan model
```

Di dalam Docker, migrasi dijalankan otomatis oleh `compose.yaml`
(`alembic upgrade head && python -m app`) sebelum server start.

Swagger UI menyala secara default di [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs) (juga `/redoc` dan `/openapi.json`). Untuk mencoba `/worker/v1/*` dari Swagger, klik **Authorize**, isi **X-Worker-Token** dengan nilai `WORKER_TOKEN` yang sama dengan `SCAN_WORKER_TOKEN` di FE, lalu tutup dialog. `GET /health` tidak membutuhkan token. Set `SCAN_ENABLE_DOCS=false` untuk mematikan dokumentasi (respons 404).

### Demo lokal dengan sumber fixture (konten orisinal buatan generator)

```bash
pip install pillow
python -m tools.fixture_source --root .fixture-data --port 9100 &      # sumber HTTP palsu: /menara-biru/manifest.json
SCAN_DEV_MODE=true WORKER_TOKEN=dev-token \
  SCAN_HOST_MIN_DELAY_MS=0 python -m app
# Scan URL di FE: http://127.0.0.1:9100/menara-biru/manifest.json
```

Mode fixture tanpa jaringan: `SCAN_ENABLE_FIXTURE=true SCAN_FIXTURE_DIR=.fixture-data` lalu scan `fixture://menara-biru`.

## Panduan menjalankan (BE + FE)

Dua repo terpisah: **Back-End-Web-Mahnwa** (worker FastAPI, repo ini) dan **Front-End-Web-Mahnwa** (Node). Semua environment variable worker (nilai default ada di `.env.example`):

| Variabel | Default | Keterangan |
| --- | --- | --- |
| `WORKER_TOKEN` | — (wajib) | Token bersama dengan FE (`SCAN_WORKER_TOKEN`). Worker menolak start bila kosong kecuali `SCAN_DEV_MODE=true`. |
| `WORKER_BIND` / `WORKER_PORT` | `127.0.0.1` / `8000` | Alamat dengar. Jangan `0.0.0.0` tanpa perlindungan jaringan (di Docker memang `0.0.0.0`, port tidak dipublish). |
| `PUBLIC_BASE_URL` | kosong | Basis absolut untuk `url` di manifest chapter. |
| `SCAN_DEV_MODE` | `false` | Hanya lokal: izinkan `http://` dan host loopback/privat. Jangan dipakai di produksi. |
| `SCAN_ENABLE_DOCS` | `true` | Swagger `/docs`, ReDoc `/redoc`, dan `/openapi.json`. `false` menutup ketiganya (404). |
| `SCAN_ENABLE_FIXTURE` / `SCAN_FIXTURE_DIR` | `false` / `./fixtures` | Adapter `fixture://` lokal (hanya dengan `SCAN_DEV_MODE=true`). |
| `SCAN_USER_AGENT` / `SCAN_CONTACT` | `AkumaScan/1.0` / kosong | Identitas permintaan keluar. |
| `STAGING_DIR` | `./staging` | Penyimpanan sementara job. |
| `SCAN_STAGING_TTL_HOURS` / `SCAN_CANCELLED_CLEANUP_S` / `SCAN_SWEEP_INTERVAL_S` | `24` / `60` / `600` | Umur staging dan pembersihan (detik untuk dua terakhir). |
| `SCAN_MIN_FREE_DISK_BYTES` | `1073741824` | Ruang bebas minimum untuk memulai unduhan. |
| `SCAN_MAX_PAGES_PER_CHAPTER` / `SCAN_MAX_BYTES_PER_CHAPTER` / `SCAN_MAX_BYTES_PER_IMAGE` | `300` / `157286400` / `15728640` | Limit scan-import (150 MiB, 15 MiB). |
| `SCAN_MAX_COVER_BYTES` / `SCAN_MAX_MANIFEST_BYTES` / `SCAN_MAX_REDIRECTS` | `5000000` / `5000000` / `3` | Limit sampul, manifest, redirect. |
| `SCAN_CONVERT_WEBP` / `SCAN_WEBP_QUALITY` | `false` / `80` | Konversi halaman hasil unduh ke WebP. Nonaktif default di kode; `.env.example`/`.env.dev`/`.env.prod` menyalakannya (`true`). Sampul tidak dikonversi. |
| `SCAN_CHAPTER_CONCURRENCY` / `SCAN_IMAGE_CONCURRENCY` / `SCAN_HOST_MAX_CONCURRENCY` / `SCAN_MAX_ACTIVE_JOBS` | `2` / `4` / `4` / `2` | Konkurensi. |
| `SCAN_HOST_MIN_DELAY_MS` | `500` | Jeda minimum per host. |
| `SCAN_IMAGE_RETRIES` / `SCAN_RETRY_BACKOFF_BASE_S` / `SCAN_RETRY_AFTER_CAP_S` | `2` / `1` / `60` | Retry. |
| `SCAN_TIMEOUT_S` / `SCAN_HTML_TIMEOUT_S` / `SCAN_IMAGE_TIMEOUT_S` / `SCAN_CONNECT_TIMEOUT_S` | `90` / `30` / `60` / `10` | Timeout. |
| `ENABLE_LEGACY_API` (alias `LEGACY_API_ENABLED`) | `false` | API legacy `/api/manga/*` (mati default). |
| `APP_ENV` / `ENV_FILE` | `dev` / — | Memilih file env: `.env.<APP_ENV>`, atau `ENV_FILE` eksplisit. |
| `DATABASE_URL` | — (wajib) | Koneksi SQLAlchemy, mis. `postgresql+psycopg://user:pass@host:5432/db`. Tanpa nilai, fitur DB gagal start (jalur scan-import murni tetap jalan). |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` / `POSTGRES_PORT` | `mahnwa` / `mahnwa` / `mahnwa` / `5433` | Dipakai image Postgres di Compose (user & db dibuat otomatis). |
| `AUTH_ENABLED` | `false` | Nyalakan endpoint login (`/auth/*`, `/me`; butuh `DATABASE_URL` dan client id/secret penyedia). |
| `AUTH_FRONTEND_BASE_URL` / `AUTH_PUBLIC_BASE_URL` | — | Basis URL FE (BFF) dan BE (alamat callback OAuth). |
| `AUTH_SECRET` | — | Kunci HMAC cookie transaksi OAuth (`state` + PKCE). Wajib di produksi (fallback `WORKER_TOKEN` saat dev). |
| `AUTH_SESSION_TTL_DAYS` / `AUTH_COOKIE_SECURE` | `30` / `false` | Umur sesi (sliding) dan flag `Secure` cookie (wajib `true` di produksi). |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | — | Rahasia OAuth Google (hanya di env backend, jangan di FE). |
| `DISCORD_CLIENT_ID` / `DISCORD_CLIENT_SECRET` | — | Rahasia OAuth Discord. |
| `X_CLIENT_ID` / `X_CLIENT_SECRET` | — | Rahasia OAuth X. |

### Login & sesi (backend)

Ketika `AUTH_ENABLED=true` (butuh `DATABASE_URL`), BE mengekspos:

| Metode & jalur | Fungsi |
| --- | --- |
| `POST /auth/login` | Masuk kata sandi (akun lama Node scrypt ikut jalan). 5 gagal/nama pengguna/15 menit → 429. |
| `POST /auth/logout` | Hapus sesi di server (`Authorization: Bearer <token>`). |
| `GET /me` | Data akun dari sesi (`Authorization: Bearer <token>`). |
| `POST /auth/handoff` | Tukar kode handoff sekali-pakai (alur sosial) dengan sesi baru. |
| `GET /auth/{provider}/start?return_to=/…` | Mulai OAuth Google/Discord/X (`return_to` wajib jalur relatif). |
| `GET /auth/{provider}/callback` | Terima jawaban penyedia → alihkan ke BFF FE dengan kode handoff. |

Token sesi **tidak pernah** dikirim ke browser: FE (BFF) yang menukar dan menyimpan
cookie `HttpOnly`. `state` + PKCE disimpan di cookie transaksi bertanda tangan
`AUTH_SECRET`. Data pengguna tidak diambil dari isi permintaan; `/me/*` selalu dari sesi.

### Migrasi akun pembaca lama (db.json → PostgreSQL)

Akun pembaca lama dari `db.json` FE dipindahkan satu kali, mempertahankan `id`,
`username`, dan `passwordHash` (hash Node langsung terverifikasi Python). Peran `admin`
dilewati. Idempoten: username yang sudah ada di BE tidak digandakan. Default **dry-run**:

```bash
python -m tools.migrate_fe_users            # tinjau (dry-run)
python -m tools.migrate_fe_users --apply    # tulis ke database (butuh DATABASE_URL)
```

### Konversi WebP halaman & migrasi berkas lama

Bila `SCAN_CONVERT_WEBP=true`, setiap halaman yang diunduh dienkode ulang ke WebP
(`SCAN_WEBP_QUALITY`, default `80`) sebelum disimpan. **Sampul tidak dikonversi** —
ekstensi sampul tetap mengikuti magic bytes (BE-05). Field `bytes`/`sha256` pada
manifest dihitung dari hasil konversi, jadi verifikasi integritas FE tetap valid.
Bila WebP tidak lebih kecil dari format asli, format asli dipertahankan.

Berkas yang sudah tersimpan sebelum fitur ini bisa dimigrasi satu kali dengan alat
di repo ini (default **dry-run**; berkas asli dipindah ke folder backup, bukan dihapus):

```bash
python -m tools.convert_media_webp            # tinjau (dry-run): berkas & URL yang akan berubah
python -m tools.convert_media_webp --apply    # eksekusi: konversi + backup + perbarui db.json
```

Opsi: `--media-dir`, `--db-json`, `--quality`, `--limit`, `--backup-dir`, `--no-backup`.
Tanpa `--media-dir`/`--db-json`, alat mencari `.dev-data` repo FE di folder sebelah.
`db.json` dicadangkan (`db.json.bak-<ts>`) sebelum diubah.

### Hubungan variabel FE ↔ worker

| FE | Worker (BE) | Aturan |
| --- | --- | --- |
| `SCAN_WORKER_URL` (default `http://127.0.0.1:8000`) | `WORKER_BIND` + `WORKER_PORT` (default `127.0.0.1:8000`) | Alamat yang dipakai FE untuk memanggil worker harus menunjuk ke bind/port worker. Satu-satunya alamat worker yang dipakai FE; URL di dalam respons/manifest worker **tidak** diikuti. |
| `SCAN_WORKER_TOKEN` | `WORKER_TOKEN` | **Harus identik.** Dikirim FE sebagai header `X-Worker-Token`. Worker wajib memilikinya (kecuali `SCAN_DEV_MODE=true`, hanya lokal); FE mengirim apa adanya, jadi nilai kosong/berbeda berujung 401 dari worker. |
| — | `PUBLIC_BASE_URL` | Basis absolut untuk field `url` di manifest chapter (mis. `http://worker:8000`). Isi sama dengan `SCAN_WORKER_URL` seperti yang terlihat dari klien manifest; kosong = path relatif (`/worker/v1/...`). FE memakai `SCAN_WORKER_URL`, bukan `PUBLIC_BASE_URL`, jadi salah isi `PUBLIC_BASE_URL` tidak memutus FE tetapi membuat `url` di manifest keliru. |
| `SCAN_DEV_MODE` | `SCAN_DEV_MODE` | Nyalakan di keduanya (hanya lokal) agar `http://` + host loopback/privat diizinkan. |

### Memulai kedua layanan

**Cara cepat (lokal, satu perintah)** — dari repo FE, repo BE di folder sebelahnya (atau `BE_DIR=...`):

```bash
scripts/dev-start.sh        # fixture :9100 + worker :8000 + FE :3000; Ctrl+C menghentikan semuanya
```

Login `admin` / `admin-dev` (hanya dev), buka `http://127.0.0.1:3000/#admin` → tab **Scan Import** → tempel `http://127.0.0.1:9100/menara-biru/manifest.json`. Port/token dapat diubah lewat `FE_PORT`, `WORKER_PORT`, `FIXTURE_PORT`, `WORKER_TOKEN`, `ADMIN_PASSWORD`; log di `/tmp/akuma-dev/`. Konten sumber hanya placeholder orisinal buatan generator.

**Manual (dua terminal)**

```bash
# 1) Worker (repo BE)
python -m venv .venv && . .venv/bin/activate && pip install fastapi "uvicorn[standard]" httpx pydantic sqlalchemy
export WORKER_TOKEN="$(openssl rand -hex 24)" PUBLIC_BASE_URL="http://127.0.0.1:8000"
python -m app                                  # 127.0.0.1:8000, cek: curl http://127.0.0.1:8000/health

# 2) FE (repo FE)
export SCAN_WORKER_URL="http://127.0.0.1:8000" SCAN_WORKER_TOKEN="<token yang sama>" ADMIN_PASSWORD="kata-sandi-kuat"
npm start                                      # 127.0.0.1:3000
```

**Docker** — `Dockerfile` ada di masing-masing repo (repo tetap terpisah). Contoh compose lintas-repo ada di `docker-compose.example.yml` (di samping `REPORT.md`, bukan bagian dari salah satu repo); worker tidak dipublish ke host, hanya FE di `127.0.0.1:3000`. **Repo ini juga punya `compose.yaml`** yang menjalankan PostgreSQL + API (lihat "Menjalankan"). Catatan: image worker hanya memuat dependensi scan-import (kode legacy Playwright/img2pdf tidak ikut); image FE memakai `NODE_ENV=production` sehingga cookie sesi bertanda `Secure` — akses lewat `localhost` atau reverse proxy TLS.

## Tes

```bash
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest -q                 # suite inti (tanpa database)
```

Tes utama berjalan tanpa internet (server HTTP palsu di loopback + gambar buatan
Pillow/byte sintetis). Uji skema/ORM (`tests/test_auth_schema.py`) hanya berjalan
bila `DATABASE_URL` diset & terjangkau, mis. setelah database Compose hidup dan
migrasi diterapkan:

```bash
docker compose --env-file .env.dev up -d db
export DATABASE_URL="postgresql+psycopg://mahnwa:mahnwa@localhost:5433/mahnwa"
alembic upgrade head
python -m pytest -q
```

## Endpoint worker (`X-Worker-Token` wajib kecuali `/health`)

| Method & path | Fungsi |
|---|---|
| `GET /health` | `{ok, version, adapters}` tanpa token |
| `GET /worker/v1/status` | ruang disk bebas, limit aktif |
| `POST /worker/v1/scan` `{url}` | metadata ternormalisasi + chapter + cover ter-stage |
| `POST /worker/v1/jobs` | mulai job download (202) |
| `GET /worker/v1/jobs/{jobId}/progress` | progress (404 `JOB_NOT_FOUND`) |
| `POST /worker/v1/jobs/{jobId}/cancel` | batalkan |
| `GET /worker/v1/jobs/{jobId}/chapters/{key}/manifest` | daftar file hasil (`page_number, content_type, bytes, sha256, url`) |
| `GET /worker/v1/jobs/{jobId}/chapters/{key}/pages/{n}` | stream byte gambar |
| `GET/DELETE /worker/v1/staging/{stagedId}` | cover ter-stage |
| `DELETE /worker/v1/jobs/{jobId}[/chapters/{key}]` | bersihkan staging |

Kontrak lengkap: lihat spesifikasi scan-import §4.3. Skema keluaran memakai `page_number` / `url` (bukan `{page, url}` milik endpoint legacy).

## Konfigurasi (environment)

Lihat `.env.example` — semua variabel bertanda default. Ringkasan:

* `WORKER_TOKEN`, `WORKER_BIND` (default `127.0.0.1`), `WORKER_PORT`, `PUBLIC_BASE_URL` (kosong = URL relatif), `STAGING_DIR`.
* `SCAN_ENABLE_DOCS` (default `true`): dokumentasi interaktif. `false` mengembalikan 404 pada `/docs`, `/redoc`, dan `/openapi.json`.
* Limit jalur scan-import: `SCAN_MAX_PAGES_PER_CHAPTER=300`, `SCAN_MAX_BYTES_PER_CHAPTER=157286400` (150 MiB), `SCAN_MAX_BYTES_PER_IMAGE=15728640` (15 MiB).
* Kesopanan: `SCAN_CHAPTER_CONCURRENCY=2`, `SCAN_IMAGE_CONCURRENCY=4`, `SCAN_HOST_MAX_CONCURRENCY=4`, `SCAN_HOST_MIN_DELAY_MS=500`, `SCAN_MAX_ACTIVE_JOBS=2`.
  Worker berhenti pada HTTP 401/403/407/451, 429 yang berulang, atau halaman CAPTCHA/tantangan (kode `SOURCE_REFUSED`); tidak ada upaya melewati penolakan.
* `ENABLE_LEGACY_API` (default `false`).
* Database: `DATABASE_URL` (PostgreSQL, mis. `postgresql+psycopg://…`) + `POSTGRES_*` untuk container Compose; skema lewat Alembic.
* Login sosial: `AUTH_ENABLED`, `AUTH_FRONTEND_BASE_URL`, `AUTH_PUBLIC_BASE_URL`, `AUTH_SECRET`, `AUTH_SESSION_TTL_DAYS`, `AUTH_COOKIE_SECURE`, dan `*_CLIENT_ID`/`*_CLIENT_SECRET` per penyedia. Rahasia **hanya** di env backend.

## Keamanan

SSRF guard tunggal (`app/scan/netguard.py`): HTTPS saja (http hanya `SCAN_DEV_MODE`), tanpa userinfo, IP/host privat-lokal-reserved ditolak
(termasuk IPv4-mapped IPv6, notasi desimal/heksa, `localhost.`), IP hasil resolve dipakai untuk koneksi (pinning), dan setiap hop redirect divalidasi.
Tidak ada allowlist host: sumber publik apa pun boleh diakses, selama tidak mengarah ke jaringan privat/lokal. Token dibandingkan dengan `hmac.compare_digest`. Tanpa CORS. Staging berada di luar folder `storage/`.

Dokumentasi interaktif (`/docs`, `/redoc`, `/openapi.json`) menyala secara default agar API bisa dicoba dari Swagger; set `SCAN_ENABLE_DOCS=false` untuk menutupnya. Hanya `GET /health` yang tanpa token — rute `/worker/v1/*` tetap menolak permintaan tanpa `X-Worker-Token` yang valid. Log tidak pernah memuat query string/token pada URL: logger `httpx`/`httpcore` dibatasi ke WARNING dan seluruh pesan log diredaksi (`?query` → `?[diredaksi]`, `user:pass@` dibuang; lihat `app/scan/logsafe.py`).

## Kode legacy (dinonaktifkan, bukan dihapus)

`/api/manga/*`, `/api/chapters/*`, tabel SQLAlchemy (`mangas`, `chapters`, `chapter_pages`), `/static`, dan `ScraperService` lama
dipertahankan di `app/legacy_api.py`, `app/services/`, `app/models.py`. Semuanya **nonaktif** kecuali `ENABLE_LEGACY_API=true`.
Endpoint legacy tidak punya autentikasi dan tidak memakai SSRF guard; `ScraperService` tidak didaftarkan sebagai adapter dan tidak diperluas.
