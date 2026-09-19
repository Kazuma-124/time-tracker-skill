#!/usr/bin/env python3
"""
把旧版时间统计数据库迁移为新的"只存原始事件"结构。

旧结构有 categories / aliases / standard_names 三张整理表，以及 events.extras 列。
新结构只保留两张表：
  events(id, name, start_time, end_time, duration_minutes, note)
  current(id=1, name, start_time)

用法：
    python3 migrate_to_raw_schema.py [数据库文件路径]

不传路径时默认使用配置文件中的数据库。纯本地操作，不连飞书，可重复执行。
"""

import os
import sqlite3
import sys


def _print_tables(conn):
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()]
    print("  表:", tables)
    if "events" in tables:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(events)").fetchall()]
        print("  events 列:", cols)


def migrate(db_path):
    print(f"目标数据库: {db_path}")
    exists = os.path.exists(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    if exists:
        print("--- 迁移前 ---")
        _print_tables(conn)
    else:
        print("(数据库文件不存在，将直接创建新结构)")

    # 1. 删除旧版整理后的表
    conn.executescript("""
        DROP TABLE IF EXISTS standard_names;
        DROP TABLE IF EXISTS aliases;
        DROP TABLE IF EXISTS categories;
    """)

    # 2. 建 events（新结构用 note 列）
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS events (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            start_time TEXT NOT NULL,
            end_time TEXT NOT NULL,
            duration_minutes REAL NOT NULL,
            note TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_events_start ON events(start_time);
        CREATE INDEX IF NOT EXISTS idx_events_name ON events(name);
    """)

    # 3. extras -> note；更老的库连备注列都没有时补 note
    cols = [r[1] for r in conn.execute("PRAGMA table_info(events)").fetchall()]
    if "extras" in cols:
        conn.execute("ALTER TABLE events RENAME COLUMN extras TO note")
        print("  列 extras -> note")
    elif "note" not in cols:
        conn.execute("ALTER TABLE events ADD COLUMN note TEXT NOT NULL DEFAULT ''")
        print("  已添加 note 列")

    # 4. 建 current
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS current (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            name TEXT NOT NULL,
            start_time TEXT NOT NULL
        );
    """)

    conn.commit()
    print("--- 迁移后 ---")
    _print_tables(conn)
    count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    print(f"  events 记录数: {count}")
    conn.close()
    print("完成。")


def main():
    if len(sys.argv) > 1:
        db_path = sys.argv[1]
    else:
        # 默认路径：读 config.json
        script_dir = os.path.dirname(os.path.abspath(__file__))
        config_path = os.path.join(script_dir, "..", "config", "config.json")
        import json
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        import os as _os
        default_dir = _os.path.expanduser(cfg["data_dir"])
        data_dir = _os.environ.get("TIME_TRACKER_DATA_DIR", default_dir)
        db_path = os.path.join(data_dir, "time-tracker.db")
    migrate(db_path)


if __name__ == "__main__":
    main()
