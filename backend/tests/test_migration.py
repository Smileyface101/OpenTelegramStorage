import sqlite3
import subprocess
import sys
from pathlib import Path


def test_alembic_upgrade_head(tmp_path):
    env = {"OTS_DATA_DIR": str(tmp_path), "PATH": "/usr/bin:/bin"}
    backend = Path(__file__).resolve().parents[1]
    r = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=backend, env=env,
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    con = sqlite3.connect(tmp_path / "ots.db")
    tables = {row[0] for row in con.execute("select name from sqlite_master where type='table'")}
    assert {"users", "sessions", "settings", "folders", "files", "file_parts", "uploads", "bundles"} <= tables
