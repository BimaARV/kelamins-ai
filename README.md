# KELA AI — Monitoring & Intelligence Bot

Stack: FastAPI + MariaDB + Redis + Telegram bot; kolektor berita RSS, gempa &
cuaca BMKG, network, event intelligence berbasis AI, alert engine, dan bot
Telegram interaktif.

Arsitektur service (docker-compose):

- `api` — FastAPI (port host `2406:8000`)
- `scheduler` — collector + event engine + AI + alert
- `worker` — liveness
- `bot` — Telegram interactive (`network_mode: host`, tanpa port)
- `mariadb` — DB (port host `2407:3306`)
- `redis` — cache/queue (port host `2408:6379`)

## 1) Prasyarat server

- Linux (Debian/Ubuntu). WAJIB: service `bot` pakai `network_mode: host`,
  jadi Docker Desktop / macOS nggak didukung.
- Docker Engine 24+ dan Docker Compose (bisa plugin `docker compose`
  atau standalone binary `docker-compose`).
- `git`.

## 2) Clone repo

```bash
git clone <URL_REPO> kela-ai
cd kela-ai
```

## 3) Konfigurasi environment

```bash
cp .env.example .env
nano .env
```

Wajib disesuaikan:

```ini
MARIADB_DATABASE
MARIADB_USER
MARIADB_PASSWORD
MARIADB_ROOT_PASSWORD
TELEGRAM_BOT_TOKEN   # token dari BotFather
TELEGRAM_CHAT_ID     # id chat telegram kamu
OLLAMA_API_KEY_1/2/3 # key gateway AI (kosong → AI skip, sisanya tetap jalan)
NEWS_SOURCE_FIXTURES # JSON array feed RSS (default [])
WEATHER_LOCATIONS    # JSON array watchlist cuaca BMKG (kode adm4)
```

Model AI (opsional, default: Ollama `gemma4:cloud`):

- `AI_MODELS` — JSON array berurut prioritas buat model pool (failover
  lintas-provider jalan kalau ada 2+ model). `base_url`/`model` tiap entry
  opsional, jatuh ke `AI_BASE_URL`/`AI_MODEL` → `OLLAMA_BASE_URL`/`OLLAMA_MODEL`.
- `AI_PROVIDER` — `ollama` | `openai` | `gemini` | `claude`.
- Key API jumlahnya bebas, tinggal tulis baris baru:
  `OLLAMA_API_KEY_4=...`, `OPENAI_API_KEY_1=...`, `GEMINI_API_KEY_1=...`,
  `ANTHROPIC_API_KEY_1=...`, atau `LLM_API_KEY_1=...` (pool bersama semua model).

Contoh model pool:

```ini
AI_MODELS=[
  {"provider":"gemini","base_url":"https://generativelanguage.googleapis.com/v1beta/openai","model":"gemini-2.0-flash"},
  {"provider":"openai","base_url":"https://api.openai.com/v1","model":"gpt-4o"}
]
GEMINI_API_KEY_1=sk-...
OPENAI_API_KEY_1=sk-...
```

Catatan:

- Tanpa semua key → worker AI skip dengan aman (collector tetap jalan).
- Tanpa `TELEGRAM_BOT_TOKEN` → alert engine dry-run (delivery ditandai
  `skipped`), tapi bot chat nggak ikut jalan.
- `.env` berisi secret dan ter-exclude dari git — jangan pernah di-commit.

## 4) Start database + run migrasi

```bash
docker compose up -d mariadb redis
docker compose run --rm api alembic upgrade head
```

Migrasi bersifat manual (nggak otomatis saat container start). Seeder feed
berita & lokasi cuaca otomatis di-sync oleh scheduler pas boot.

## 5) Build & start semua service

```bash
docker compose up -d --build
```

`docker compose up -d` pertama kali akan menunggu `mariadb` sehat (healthcheck)
sebelum container lain jalan.

## 6) Verifikasi

```bash
docker compose ps                        # semua container up
curl http://localhost:2406/health        # {"status":"ok",...}
curl http://localhost:2406/health/ready  # ready setelah DB jalan
docker compose logs -f bot               # tunggu "Bot started as @<username>"
docker compose logs -f scheduler         # cek collector & sync sources
```

## 7) Redeploy setelah ubah kode

```bash
docker compose build && docker compose up -d --force-recreate
```

> `docker compose up -d` doang TIDAK recreate container kalau image tag
> sama — wajib `--force-recreate` supaya container pakai kode baru.

## 8) Ganti model AI / nambah API key

Cukup edit `.env`, lalu recreate — **TIDAK perlu rebuild** (env dibaca saat
container start, kode nggak berubah & di-bake ke image):

```bash
docker compose up -d --force-recreate
```

Langkah:

1. Edit `.env` — ubah `AI_MODELS`/`AI_PROVIDER`/`AI_BASE_URL`/`AI_MODEL`
   (atau `OLLAMA_MODEL`/`OLLAMA_BASE_URL`), atau tambah baris key baru
   (`OLLAMA_API_KEY_4=...`, `GEMINI_API_KEY_1=...`, dst).
2. `docker compose up -d --force-recreate` — recre-ate semua service yang
   pakai gateway (api/scheduler/worker/bot) bareng; jangan cuma satu.
3. Verifikasi: `docker compose logs -f bot` → "Bot started as @<username>",
   lalu kirim `/status ai` ke bot → model & jumlah key harus update.
4. `docker compose build` baru wajib kalau **kode** yang diubah (bukan env).

## 9) Jalanin test (opsional)

```bash
docker compose run --rm -v "$PWD":/code --workdir /code api bash -lc \
  'pip install -q pytest pytest-asyncio aiosqlite httpx openpyxl 2>/dev/null || true; \
   python -m pytest tests -q'
```

## Catatan penting

- Data DB disimpan di volume `mariadb_data` — aman walau container dihapus.
- Port yang dipakai: `2406` (API), `2407` (MariaDB), `2408` (Redis). Kalau
  bentrok, ubah di `docker-compose.yml`.
- Bot lari di network host biar `/ping`, `/traceroute`, `/ipinfo` melihat
  network server asli (bukan virtual container). Kalau nggak mau, hapus
  `network_mode: host`, override `DATABASE_URL`/`REDIS_URL`, dan mount
  `/:/host-root:ro` dari service `bot`.
- Kalau `docker compose up` tiba-tiba gagal OCI "Remote peer disconnected"
  (race cgroup): `docker start kela-mariadb kela-redis kela-api kela-scheduler kela-worker kela-bot`.
- Backup DB:

  ```bash
  docker exec kela-mariadb sh -c 'mariadb-dump -ukela -p"$MARIADB_PASSWORD" kela' > kela.sql
  ```