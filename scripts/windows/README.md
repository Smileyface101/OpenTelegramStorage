# Windows without Docker

1. Install Python 3.12 from python.org. Tick **"Add python.exe to PATH"**.
2. Double-click `run.cmd` in this folder. The first run creates a virtual
   environment and installs dependencies (a minute or two); later runs start
   in seconds.
3. Open http://localhost:8080.

The web interface is prebuilt in `frontend\dist`; Node.js is only needed if
that folder is missing.

Data (database, keys, staging) lives in `data\` next to `backend\`. Back up
that folder. Set `OTS_PORT` before running to use another port, and
`OTS_IMPORT_DIR` to enable "Import from server".

To run it in the background at login, use Task Scheduler with the action
`powershell -NoProfile -ExecutionPolicy Bypass -File <path>\scripts\windows\run.ps1`
and "Run whether user is logged on or not", or install it as a service with
NSSM (https://nssm.cc).

Prefer Docker? It needs hardware virtualisation: enable "Intel VT-x" / "AMD-V"
(sometimes "SVM Mode") in the BIOS/UEFI, then turn on the Windows feature
"Virtual Machine Platform" and WSL 2. Docker Desktop then works and
`docker compose up -d` in the source folder does the rest.
