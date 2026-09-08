#!/usr/bin/env bash
# Build a self-contained Windows x64 package: embeddable Python + all
# dependencies as Windows wheels + backend + prebuilt frontend + installer.
# Run on Linux/macOS with internet access. Output: dist/OpenTelegramStorage-windows-x64.zip
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PYVER="${PYVER:-3.12.7}"
OUT="$ROOT/dist"
PKG="$OUT/OpenTelegramStorage"
rm -rf "$PKG"; mkdir -p "$PKG" "$OUT/cache"

echo "==> Frontend"
( cd "$ROOT/frontend" && [ -d dist ] || npm run build )

echo "==> Embeddable Python $PYVER"
ZIP="$OUT/cache/python-$PYVER-embed-amd64.zip"
[ -f "$ZIP" ] || curl -fsSL -o "$ZIP" "https://www.python.org/ftp/python/$PYVER/python-$PYVER-embed-amd64.zip"
mkdir -p "$PKG/python" && ( cd "$PKG/python" && unzip -qo "$ZIP" )
# Let the embedded interpreter see site-packages.
PTH=$(ls "$PKG/python"/python3*._pth)
printf 'python%s.zip\n.\nLib\\site-packages\n..\\app\\backend\nimport site\n' "$(basename "$PTH" | sed -E 's/python([0-9]+)\._pth/\1/')" > "$PTH"

echo "==> Dependencies (Windows wheels)"
WHEELS="$OUT/cache/wheels"; rm -rf "$WHEELS"; mkdir -p "$WHEELS"
# Resolve the exact dependency set in a scratch venv (host), then fetch each
# package as a win_amd64/cp312 wheel; pure-Python packages that only ship an
# sdist (e.g. pyaes) are built into universal wheels here.
python3 -m venv "$OUT/cache/venv" >/dev/null
"$OUT/cache/venv/bin/pip" install -q --upgrade pip
sed -E 's/^uvicorn\[standard\]/uvicorn/' "$ROOT/backend/requirements.txt" > "$OUT/cache/req.txt"
"$OUT/cache/venv/bin/pip" install -q -r "$OUT/cache/req.txt"
"$OUT/cache/venv/bin/pip" freeze | grep -viE '^(pip|setuptools|wheel|uvloop)=' > "$OUT/cache/frozen.txt"
echo "colorama==0.4.6" >> "$OUT/cache/frozen.txt"   # click needs it on Windows
DL=("$OUT/cache/venv/bin/pip" download -q --no-deps -d "$WHEELS" --platform win_amd64 --python-version 3.12 --implementation cp --abi cp312 --only-binary=:all:)
while read -r req; do
  [ -z "$req" ] && continue
  if ! "${DL[@]}" "$req" 2>/dev/null; then
    name="${req%%==*}"
    if [ "$name" = "cryptg" ]; then echo "   (no Windows wheel for cryptg; skipping, optional)"; continue; fi
    echo "   building pure-Python wheel for $req"
    "$OUT/cache/venv/bin/pip" wheel -q --no-deps -w "$WHEELS" "$req"
  fi
done < "$OUT/cache/frozen.txt"
# Every wheel must be Windows or universal.
for w in "$WHEELS"/*.whl; do case "$(basename "$w")" in *win_amd64.whl|*none-any.whl) ;; *) echo "not a Windows/universal wheel: $w"; exit 1;; esac; done
mkdir -p "$PKG/python/Lib/site-packages"
# Wheels are zip files; unpack them (pip refuses to "install" Windows wheels on Linux).
for w in "$WHEELS"/*.whl; do unzip -qo "$w" -d "$PKG/python/Lib/site-packages"; done
# Console-script wrappers from *.data/scripts are Linux-flavoured; drop them.
find "$PKG/python/Lib/site-packages" -maxdepth 1 -name '*.data' -type d -exec rm -rf {} +
# Windows has no uvloop; make sure nothing imports it. (uvicorn only uses it if present.)
rm -rf "$PKG/python/Lib/site-packages/uvloop"* 2>/dev/null || true

echo "==> App"
mkdir -p "$PKG/app/backend" "$PKG/app/frontend"
rsync -a --exclude __pycache__ --exclude .pytest_cache --exclude tests --exclude '.venv' --exclude '*.pyc' "$ROOT/backend/" "$PKG/app/backend/"
rsync -a "$ROOT/frontend/dist/" "$PKG/app/frontend/dist/"
cp "$ROOT/LICENSE" "$ROOT/README.md" "$PKG/"
cp "$ROOT/scripts/windows/"{install.cmd,OpenTelegramStorage.cmd,start-hidden.vbs,uninstall.cmd,PACKAGE-README.txt} "$PKG/"
echo "$(git -C "$ROOT" describe --tags --always 2>/dev/null || echo dev)" > "$PKG/VERSION"

echo "==> Zip"
( cd "$OUT" && rm -f OpenTelegramStorage-windows-x64.zip && zip -qr OpenTelegramStorage-windows-x64.zip OpenTelegramStorage )
du -sh "$OUT/OpenTelegramStorage-windows-x64.zip"
