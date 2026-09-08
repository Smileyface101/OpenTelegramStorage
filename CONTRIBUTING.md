# Contributing

Thanks for looking. This project is small enough to hold in your head, and
[docs/architecture.md](docs/architecture.md) is the map.

## Setting up

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest                      # ~1 minute, no Telegram needed

cd ../frontend
npm install && npm run dev            # proxies /api to a backend on :8000
```

Run the backend for development with:

```bash
cd backend && OTS_DATA_DIR=../data .venv/bin/uvicorn main:app --reload --port 8000
```

## Ground rules

- **Tests for behaviour.** Anything touching transfers, integrity, encryption
  or auth needs a test in `backend/tests`. The fake Telegram in
  `tests/fake_telegram.py` stores messages in a dict and is enough for
  almost everything.
- **Migrations for schema.** Change `app/models.py`, then
  `alembic revision --autogenerate -m "..."`. Add `server_default` to new
  NOT NULL columns so existing rows migrate.
- **Never hold a whole file on disk or in memory.** The pipeline exists so a
  60 GB file needs 2 GB of staging. Keep it that way.
- **Channel captions are a public format.** If you change what goes into a
  part caption, keep older captions readable in `app/recovery.py`.
- **Secrets go through the vault.** Anything that must be stored and is
  sensitive is written with `settings_store.set` on a key listed in
  `SECRET_KEYS`.
- Keep the UI plain: one page per job, no modal inside a modal, errors shown
  where they happen.

## Pull requests

One change per PR, with a sentence on why. CI runs the backend tests, the
frontend build and a Docker build; all three must pass.

Real-Telegram behaviour cannot run in CI. If your change touches
`app/telegram/`, say in the PR what you tested against a live bot.
