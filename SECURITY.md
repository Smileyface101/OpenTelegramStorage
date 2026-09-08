# Security policy

## Reporting a vulnerability

Please do not open a public issue for security problems. Use GitHub's
private vulnerability reporting on this repository ("Security" tab →
"Report a vulnerability"). You will get an acknowledgement within a few days.

## What is in scope

- Authentication, sessions, two-factor, share links and their grants.
- The content encryption scheme (`backend/app/crypto.py`) and the vault.
- Anything that lets one user read, change or delete another user's files.
- Path handling in server-side import and in archive member names.

## What the design assumes

- Whoever controls the server controls the data. Content encryption protects
  against readers of the Telegram channel, not against the server operator.
- The app has no TLS of its own; deployments beyond localhost are expected to
  sit behind a reverse proxy.
- Telegram's own security applies to the channel and the bot token.
