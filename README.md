# OpenTelegramStorage

Self-hosted file storage that keeps the bytes in a private Telegram channel.
Run it on your own machine, connect your own bot, drop files in a browser
tab. Files up to 2 GB go up as a single message; larger files are split into
numbered parts automatically; folders can be zipped on the way in. Nothing is
stored on your server after a transfer finishes except the index.

Everything is password protected, credentials are encrypted at rest, and one
Docker volume is the whole state of an installation.

> Telegram is not a storage product. Their terms allow bots to send and keep
> files, but accounts that push large volumes can be rate limited or
> restricted. This project is for personal use of your own account and your
> own channel. Keep a second copy of anything you cannot afford to lose.

## How it works

```
browser ──chunked, resumable──▶ staging dir ──worker──▶ Telegram channel (MTProto)
                                     ▲                          │
                                 SQLite index  ◀── JSON captions on every part
```

* **MTProto, not the HTTP Bot API.** The HTTP API caps bot uploads at 50 MB
  and downloads at 20 MB. Over MTProto (via Telethon) a bot sends 2000 MiB per
  message and downloads without limit, so the same bot token you get from
  BotFather is enough. You also need an API id/hash from
  [my.telegram.org](https://my.telegram.org/apps).
* **Split and zip.** Files above the configured part size (default 512 MB,
  max 1990 MB) become `name.ext.001`, `name.ext.002`, … each as its own message.
  "Upload as zip" packs a selection into one archive server-side first.
* **Downloads stream.** Parts are fetched from Telegram and joined on the fly,
  with HTTP Range support, so nothing is buffered on disk.
* **Real delete.** Deleting a file deletes the channel messages, not just the
  index row.
* **Self-describing channel.** Every part carries a JSON caption with the file
  id, name, part number and SHA-256, so the index can be rebuilt from the
  channel alone (rebuild tool is on the roadmap).

## Quick start (Docker)

```bash
git clone https://github.com/Smileyface101/OpenTelegramStorage.git
cd OpenTelegramStorage
cp .env.example .env        # optional; defaults are fine on localhost
docker compose up -d --build
```

Open http://localhost:8080 and follow the wizard:

1. Create the admin account.
2. Paste the API id, API hash and bot token.
3. Create a private channel, add the bot as an admin that can post and delete
   messages, then post any message in it. The channel shows up in the wizard;
   pick it.

That is it. Drop files on the Files page.

## Running without Docker

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
OTS_DATA_DIR=../data .venv/bin/alembic upgrade head
OTS_DATA_DIR=../data .venv/bin/uvicorn main:app --port 8000

cd ../frontend
npm install && npm run build      # served by the backend from frontend/dist
```

For frontend development run `npm run dev` (proxies `/api` to port 8000).

## Security model

* Passwords are hashed with argon2id. Login is rate limited per IP and an
  account locks for 15 minutes after 5 failures.
* Sessions are server-side; the cookie is HttpOnly, SameSite=Lax and Secure
  on HTTPS. State-changing requests need a CSRF token (double submit).
* The bot token, API hash and MTProto session are encrypted with AES-256-GCM
  under a master key that lives in `data/master.key` (mode 0600) or in the
  `OTS_MASTER_KEY` environment variable.
* Users only ever see their own folders and files. Admins manage users,
  Telegram settings and transfer settings.
* The app has no built-in TLS. If you expose it beyond localhost put Caddy,
  nginx or Cloudflare in front of it and set `OTS_TRUSTED_PROXY_COUNT` and
  `OTS_COOKIE_SECURE=true`.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `OTS_DATA_DIR` | `./data` (`/data` in Docker) | Database, master key, staging area |
| `OTS_MASTER_KEY` | generated | 64 hex chars; overrides `data/master.key` |
| `OTS_PORT` | `8080` | Host port (compose only) |
| `OTS_COOKIE_SECURE` | auto | Force the Secure cookie flag on/off |
| `OTS_TRUSTED_PROXY_COUNT` | `0` | Reverse proxies in front of the app |
| `OTS_DEFAULT_PART_SIZE_MB` | `512` | Initial part size; change later in Settings |
| `OTS_LOG_LEVEL` | `INFO` | Python log level |

Staging needs free disk space equal to the largest file you upload (plus a
256 MB margin); the upload is refused otherwise.

## Roadmap

- Rebuild the index from the channel (disaster recovery / second machine).
- Optional client-side encryption of parts (AES-GCM) so Telegram never holds
  readable bytes.
- Multi-connection uploads for higher throughput.
- TOTP two-factor login.
- User-account (phone) login for 4 GB parts with Telegram Premium.

## Development

```bash
cd backend && pip install -r requirements-dev.txt && pytest
```

The tests use a fake Telegram client; nothing talks to Telegram in CI.

## License

MIT.
