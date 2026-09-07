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
browser ──chunked, resumable──▶ part staging ──worker──▶ Telegram channel (MTProto)
        (next part)                 (≤ 3 parts)   (previous part, concurrently)
                                        ▲                          │
                                    SQLite index  ◀── JSON captions on every part
```

* **MTProto, not the HTTP Bot API.** The HTTP API caps bot uploads at 50 MB
  and downloads at 20 MB. Over MTProto (via Telethon) a bot sends 2000 MiB per
  message and downloads without limit, so the same bot token you get from
  BotFather is enough. You also need an API id/hash from
  [my.telegram.org](https://my.telegram.org/apps).
* **Streaming pipeline.** The browser sends chunks, several at a time and in
  any order, into per-part staging files; the moment a part (default 512 MB,
  max 1990 MB) is complete it goes to Telegram while the next part is still
  arriving. An interrupted upload resumes with only the missing chunks, even
  after a browser restart (one click on Chrome/Edge, re-pick the file elsewhere). Staging holds at most a few
  parts, so a 60 GB file needs about 1.5 GB of disk, and the total time is the
  slower hop rather than the sum of both. Parts are hashed as they arrive.
* **Split and zip.** Large files become `name.ext.001`, `name.ext.002`, … one
  message each, joined again on download. "Upload as zip" and folder uploads
  stream a store-only zip straight into the same pipeline: the archive layout
  is fixed from the file list up front, so it never exists on disk as a whole
  and there is no size limit beyond your channel.
* **Downloads stream.** Parts are fetched from Telegram and joined on the fly,
  with HTTP Range support, so nothing is buffered on disk.
* **Integrity end to end.** The browser hashes the file while reading it and
  the server hashes every part as it arrives; a mismatch rejects the upload.
  Each part is checked against its recorded SHA-256 as it streams out on
  download, and "Verify" re-reads a file from the channel to prove it is
  still intact. Downloads carry an `X-Checksum-SHA256` header.
* **Import from the server.** Mount a directory at `/import` and send files
  or whole folders from it straight to the channel: no browser, no copy, read
  in place. Folders go in as a streamed zip or as a tree.
* **Housekeeping.** Uploads nobody resumed for 72 hours (configurable) are
  removed along with any parts that reached the channel; orphaned staging
  files are swept. Settings → System shows Telegram, worker, queue, staging
  and disk state, recent warnings, and offers reconnect and cleanup buttons.
  A configured-but-offline Telegram connection is retried every minute.
* **Real delete.** Deleting a file deletes the channel messages, not just the
  index row.
* **Self-describing channel.** Every part carries a JSON caption with the file
  id, name, folder path, part number and SHA-256. Settings → Recovery rebuilds
  the whole index from the channel on a fresh install or after losing the
  data volume.
* **Parallel uploads.** Files over 10 MB go to Telegram over several MTProto
  connections at once (default 4, configurable), several times faster than a
  single connection.

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
| `OTS_UPLOAD_CHUNK_SIZE` | `8388608` | Browser→server chunk in bytes; fixed per install, do not change with uploads in flight |
| `OTS_IMPORT_DIR` | `data/import` (`/import` in Docker) | Server-side import mount |
| `OTS_LOG_LEVEL` | `INFO` | Python log level |

Staging needs free disk space for about four parts (2 GB at the default part
size); the upload is refused otherwise.

## Roadmap

- Optional encryption of parts (AES-GCM) so Telegram never holds readable bytes.
- TOTP two-factor login and a session list.
- Expiring share links.
- User-account (phone) login for 4 GB parts with Telegram Premium.

## Development

```bash
cd backend && pip install -r requirements-dev.txt && pytest
```

The tests use a fake Telegram client; nothing talks to Telegram in CI.

## License

MIT.
