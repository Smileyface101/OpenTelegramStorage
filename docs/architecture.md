# Architecture

This document is for people who want to know exactly what happens to their
bytes, and for contributors. The README covers usage.

## Components

One container, one process, one data directory.

| Piece | Where | Role |
|---|---|---|
| FastAPI app | `backend/main.py`, `backend/app/routers/` | HTTP API and the built SPA |
| Transfer worker | `backend/app/transfers/worker.py` | asyncio task inside the same process: sends parts to Telegram, housekeeping, Telegram reconnect |
| Telegram manager | `backend/app/telegram/manager.py` | one Telethon client (MTProto, bot-token login), channel discovery, upload/download primitives |
| SQLite | `data/ots.db` (WAL) | users, sessions, folders, file index, parts, uploads, shares, settings |
| Vault | `backend/app/vault.py` | AES-256-GCM for secrets in the settings table, keyed by `data/master.key` or `OTS_MASTER_KEY` |
| React SPA | `frontend/` | served by the backend from `frontend/dist` |

Everything a user must back up is the data directory plus, if set, the
`OTS_MASTER_KEY` environment variable.

## Why MTProto

Telegram's HTTP Bot API limits bots to 50 MB uploads and 20 MB downloads.
Speaking MTProto directly (Telethon) with the same bot token raises that to
2000 MiB per message and unlimited downloads. That is the only reason the app
asks for an API id and hash from my.telegram.org: they identify the MTProto
client; the bot token is what logs in. No user account is involved.

Bots cannot read channel history, which shapes two designs below: channel
discovery relies on receiving posts while connected, and index rebuild
fetches messages by id.

## Upload pipeline

```
browser ──chunks, 3 in flight, any order──▶ per-part staging files ──worker──▶ channel
                                             (hash follows the contiguous prefix)   (one message per part)
```

1. **Init.** `POST /api/uploads` creates the `File` row and every `FilePart`
   row up front (`plan_parts`), plus an `Upload` session row holding a chunk
   bitmap. Part size comes from settings (default 512 MiB, cap 1990 MiB).
2. **Chunks.** `PUT /api/uploads/{id}/chunk` with `X-Chunk-Offset` writes a
   fixed-size chunk (default 8 MiB, `OTS_UPLOAD_CHUNK_SIZE`) straight into the
   part file it belongs to. Any order, idempotent. After the write, a per-upload
   lock advances the hashed prefix: bytes between the old and new contiguous
   frontier are read back and fed to the part hasher and the whole-file
   hasher. A part whose prefix passed its end gets its final SHA-256 and is
   eligible for sending.
3. **Backpressure.** If a chunk would start a part more than
   `MAX_STAGED_PARTS` (3) ahead of the first part not yet in the channel, the
   server answers 429 with `Retry-After`; the browser waits. Staging therefore
   holds about four parts regardless of file size.
4. **Send.** The worker picks any complete, unsent part, wraps it in a
   `RangeReader` (and an `EncryptingReader` when the file is encrypted), and
   uploads it as a document with a JSON caption. On success the staging file
   is deleted. Files over 10 MiB use `upload_parallel`: N exported MTProto
   senders save 512 KiB pieces of the same file concurrently; any failure
   falls back to Telethon's single-connection uploader.
5. **Complete.** `POST /api/uploads/{id}/complete` carries the browser's own
   digests (whole file and per part, computed with hash-wasm while reading).
   They must match the server's; a mismatch fails the file and deletes parts
   already sent. The file becomes `READY` when every part has a message id.
   A small safety-net pass repairs the race where the last part lands at the
   same moment as completion.

Resume: `GET /api/uploads/{id}` returns the missing chunk indexes. The SPA
remembers unfinished uploads in IndexedDB (with a `FileSystemFileHandle` on
Chromium) and offers to continue them.

### Archives

"Files as zip", folder uploads and folder imports produce a store-only ZIP
whose layout is computed from the member list before any data arrives
(`zipstream.plan`): local headers with the data-descriptor flag, per-entry
ZIP64 when sizes or offsets need it, central directory at the end. Members
stream in order through `staging.write_range`; CRC-32 runs per member and is
persisted on the `Upload` row. The archive is a normal `File` with parts and
never exists on disk as a whole. Compression is not offered because deflate
output sizes cannot be planned ahead.

### Server-side import

`OTS_IMPORT_DIR` (mounted at `/import` in Docker) can be browsed by admins.
A file import points the `File` at the source path with `keep_source=True`;
the worker reads it in place and never deletes it. A folder imports either as
a tree of such files or as a streamed archive driven by a background task
with the same staging cap.

## Part and caption format

Each part is one document message. Its caption is compact JSON:

```json
{"ots":1,"id":"<file id>","name":"movie.mkv","size":1879048192,
 "part":3,"of":4,"psize":536870912,"sha256":"…","archive":false,
 "path":"Photos/2024","enc":{"v":1,"kid":"3f1c9a7b2d4e5f60","salt":"…","ct":536879124}}
```

`psize` and `sha256` describe the plaintext part. `enc` is present only for
encrypted parts. Message file names are `name.ext.001`, `name.ext.002`, … or
the plain name for single-part files.

Because every part is self-describing, **Rebuild index from channel**
(`app/recovery.py`) can recreate `files`, `file_parts` and the folder tree
from the channel alone: it posts and deletes a marker message to learn the
highest message id, then fetches ids in batches of 100 (bots may fetch by id)
and groups captions by file id. Files with missing parts are imported as
failed with a reason.

## Download

`stream_file` in `app/routers/files.py` serves both authenticated downloads
and public share links. It maps the requested byte range onto parts, fetches
each part's document and streams it with `iter_download`. A part streamed in
full is hashed on the fly and compared with its recorded SHA-256; on
mismatch the response is aborted and the file is flagged. `Range` requests
are honoured (206), and `X-Checksum-SHA256` carries the whole-file digest.

## Content encryption

`app/crypto.py`. When "encrypt new uploads" is on (default), parts are
encrypted on the server as they are sent:

```
header  = "OTS1" || salt(16)                                   20 bytes
block_i = AES-256-GCM(subkey, nonce = i, aad = salt || i, pt)  1 MiB + 16
subkey  = HKDF-SHA256(content_key, salt, "ots-part")
```

* The content key is 32 random bytes, generated on first use, stored in the
  settings table encrypted by the vault. `key_id` = first 16 hex of its
  SHA-256, recorded on the file and in captions.
* A fresh salt per part makes counter nonces safe; the AAD binds block order.
* Blocks are independent, so a byte range is served by fetching only the
  ciphertext blocks it touches (`ct_range_for`) and decrypting those.
* Verify and downloads treat an authentication failure as a tamper event.
* Threat model: protects the data from anyone who can read the channel
  (Telegram itself, a leaked bot token, an extra channel admin). It does not
  protect against someone who controls the server, who also has the vault.

Losing the content key loses every encrypted file. Settings can export it
(password required) and import it on a fresh install; import is refused
while files encrypted with the current key exist.

## Authentication

* Passwords: argon2id. Per-IP login rate limit, account lockout after 5
  failures for 15 minutes.
* Sessions: random token in an HttpOnly, SameSite=Lax cookie; the SHA-256 of
  the token is the primary key of the `sessions` row. State-changing requests
  need the double-submit CSRF header. Public share endpoints are exempt.
* Two-factor: TOTP (RFC 6238, ±1 step, replay guard per step), ten SHA-256
  hashed single-use recovery codes, pending login token with 5 attempts and a
  5-minute TTL kept in process memory.

## Housekeeping

Every 10 minutes the worker removes uploads with no activity for
`transfer.stale_upload_hours` (default 72), including their channel parts,
sweeps staging files that no row references and are older than an hour, and
deletes share links expired for more than 30 days. It also reconnects a
configured-but-offline Telegram client every minute.

## Database migrations

Alembic, run automatically by the container entrypoint (`alembic upgrade
head`). Schema changes go through `alembic revision --autogenerate`; SQLite
needs `render_as_batch`, which `alembic/env.py` sets.

## Tests

`backend/tests` runs against an in-memory-like SQLite file and a fake
Telegram manager (`tests/fake_telegram.py`) that stores "messages" in a dict.
Nothing in CI talks to Telegram. The fake exercises the same code paths
including captions, encryption, ranged downloads and rebuild.
