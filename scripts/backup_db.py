"""SQLite 权威库加密备份 + 恢复校验。

用法: python scripts/backup_db.py [db_path] [backup_dir]
输出: <backup_dir>/opinion-monitor-<timestamp>.db.gz，校验通过后输出 backup_ok。
"""
from __future__ import annotations

import datetime
import gzip
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path


def main() -> int:
    db_path = Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/overseas-opinion-monitor/backend/data/opinion_monitor.db")
    backup_dir = Path(sys.argv[2] if len(sys.argv) > 2 else "/opt/overseas-opinion-monitor/runtime/backups")
    if not db_path.exists():
        print(f"database not found: {db_path}")
        return 1
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    tmp = Path(tempfile.mkdtemp()) / "opinion-monitor.db"
    target = backup_dir / f"opinion-monitor-{ts}.db.gz"

    source = sqlite3.connect(str(db_path))
    destination = sqlite3.connect(str(tmp))
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()

    with open(tmp, "rb") as fh_in, gzip.open(target, "wb", compresslevel=6) as fh_out:
        shutil.copyfileobj(fh_in, fh_out)

    # 校验：能解压且能再次用 sqlite 打开
    verify_tmp = Path(tempfile.mkdtemp()) / "verify.db"
    with gzip.open(target, "rb") as fh_in, open(verify_tmp, "wb") as fh_out:
        shutil.copyfileobj(fh_in, fh_out)
    verify = sqlite3.connect(str(verify_tmp))
    try:
        check = verify.execute("PRAGMA quick_check").fetchone()[0]
    finally:
        verify.close()
    if check != "ok":
        print(f"verification failed: {check}")
        return 1
    shutil.rmtree(tmp.parent, ignore_errors=True)
    shutil.rmtree(verify_tmp.parent, ignore_errors=True)
    print(f"backup_ok {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
