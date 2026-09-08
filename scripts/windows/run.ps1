# Run OpenTelegramStorage on Windows without Docker.
# Needs: Python 3.12 (https://www.python.org/downloads/windows/ - tick "Add to PATH").
# Node.js is only needed if frontend\dist is missing (it is included on the USB copy).
$ErrorActionPreference = "Stop"
$root = Resolve-Path (Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) "..\..")
$backend = Join-Path $root "backend"
$frontend = Join-Path $root "frontend"
$data = Join-Path $root "data"
$port = if ($env:OTS_PORT) { $env:OTS_PORT } else { "8080" }

Write-Host "OpenTelegramStorage at $root"

# --- Python ---
$py = $null
foreach ($cand in @("py -3.12", "py -3", "python")) {
    try { & cmd /c "$cand --version" 2>$null | Out-Null; if ($LASTEXITCODE -eq 0) { $py = $cand; break } } catch {}
}
if (-not $py) { Write-Error "Python 3.12 not found. Install it from python.org and tick 'Add python.exe to PATH'." }
Write-Host "Using $py"

Set-Location $backend
if (-not (Test-Path ".venv")) {
    Write-Host "Creating virtual environment..."
    & cmd /c "$py -m venv .venv"
}
$venvPy = Join-Path $backend ".venv\Scripts\python.exe"
& $venvPy -m pip install --quiet --upgrade pip
Write-Host "Installing dependencies..."
try {
    & $venvPy -m pip install --quiet -r requirements.txt
} catch {
    Write-Host "Retrying without cryptg (optional fast crypto)..."
    Get-Content requirements.txt | Where-Object { $_ -notmatch "^cryptg" } | Set-Content requirements.win.txt
    & $venvPy -m pip install --quiet -r requirements.win.txt
}

# --- Frontend (prebuilt on the USB copy; build only if missing) ---
if (-not (Test-Path (Join-Path $frontend "dist\index.html"))) {
    if (Get-Command npm -ErrorAction SilentlyContinue) {
        Write-Host "Building the web interface (one time)..."
        Set-Location $frontend
        & npm install --no-audit --no-fund
        & npm run build
        Set-Location $backend
    } else {
        Write-Error "frontend\dist is missing and Node.js is not installed. Install Node.js 20 or copy a build that includes frontend\dist."
    }
}

# --- Database + server ---
New-Item -ItemType Directory -Force -Path $data | Out-Null
$env:OTS_DATA_DIR = $data
$env:OTS_FRONTEND_DIST = Join-Path $frontend "dist"
if (-not $env:OTS_IMPORT_DIR) { $env:OTS_IMPORT_DIR = Join-Path $data "import" }
Write-Host "Applying database migrations..."
& (Join-Path $backend ".venv\Scripts\alembic.exe") upgrade head
Write-Host ""
Write-Host "Open http://localhost:$port  (Ctrl+C stops the server)"
Write-Host "Data directory: $data"
& (Join-Path $backend ".venv\Scripts\uvicorn.exe") main:app --host 127.0.0.1 --port $port
