from __future__ import annotations

import argparse
import json
import os
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

from .database import DB_PATH


def verify_database(path: Path | None = None) -> dict:
    database_path = Path(path or DB_PATH)
    if not database_path.is_file():
        raise FileNotFoundError(f"数据库不存在: {database_path}")
    with closing(sqlite3.connect(database_path)) as conn:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_key_errors = conn.execute("PRAGMA foreign_key_check").fetchall()
        tables = {
            row[0]: conn.execute(f'SELECT COUNT(*) FROM "{row[0]}"').fetchone()[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        }
    return {
        "path": str(database_path.resolve()),
        "integrity": integrity,
        "foreign_key_errors": len(foreign_key_errors),
        "tables": tables,
        "ok": integrity == "ok" and not foreign_key_errors,
    }


def backup_database(target: Path, source: Path | None = None) -> dict:
    source_path = Path(source or DB_PATH).resolve()
    target_path = Path(target).resolve()
    if not source_path.is_file():
        raise FileNotFoundError(f"源数据库不存在: {source_path}")
    if source_path == target_path:
        raise ValueError("备份目标不能与源数据库相同")
    target_path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(prefix=f".{target_path.name}.", suffix=".tmp", dir=target_path.parent)
    os.close(handle)
    temporary_path = Path(temporary_name)
    try:
        with closing(sqlite3.connect(source_path)) as source_conn:
            with closing(sqlite3.connect(temporary_path)) as target_conn:
                source_conn.backup(target_conn)
        verification = verify_database(temporary_path)
        if not verification["ok"]:
            raise RuntimeError("备份完整性检查失败")
        os.replace(temporary_path, target_path)
    finally:
        temporary_path.unlink(missing_ok=True)
    verification = verify_database(target_path)
    verification["source"] = str(source_path)
    return verification


def main() -> None:
    parser = argparse.ArgumentParser(description="海外舆情监测系统数据库维护工具")
    subparsers = parser.add_subparsers(dest="command", required=True)
    verify_parser = subparsers.add_parser("verify", help="校验数据库完整性")
    verify_parser.add_argument("path", nargs="?", type=Path, default=DB_PATH)
    backup_parser = subparsers.add_parser("backup", help="创建并校验一致性备份")
    backup_parser.add_argument("target", type=Path)
    backup_parser.add_argument("--source", type=Path, default=DB_PATH)
    args = parser.parse_args()
    result = verify_database(args.path) if args.command == "verify" else backup_database(args.target, args.source)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result["ok"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
