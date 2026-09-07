#!/usr/bin/env python3
"""
JSONL → SQLite 数据迁移脚本。
将旧版 JSON/JSONL 格式的数据导入到新版 SQLite 数据库中。

用法:
    python3 migrate_jsonl_to_sqlite.py [--source <源数据目录>] [--target <目标数据库路径>]

默认源目录: 脚本同级 data/ 目录（旧版技能数据目录）
默认目标:    环境变量 TIME_TRACKER_DATA_DIR 或 ~/workspace/time-tracker-data/time-tracker.db
"""

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
_DEFAULT_DATA_DIR = Path.home() / ".super_doubao" / "super-doubao-runtime" / "workspace" / "time-tracker-data"
DEFAULT_TARGET_DB = Path(os.environ.get("TIME_TRACKER_DATA_DIR", str(_DEFAULT_DATA_DIR))) / "time-tracker.db"

TZ = timezone(timedelta(hours=8))


def load_jsonl(path):
    if not path.exists():
        return []
    items = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                items.append(json.loads(line))
    return items


def load_json(path, default):
    if not path.exists():
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def init_target_db(db_path):
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS events (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT '其他',
            start_time TEXT NOT NULL,
            end_time TEXT NOT NULL,
            duration_minutes REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_events_start ON events(start_time);
        CREATE INDEX IF NOT EXISTS idx_events_category ON events(category);
        CREATE INDEX IF NOT EXISTS idx_events_name ON events(name);

        CREATE TABLE IF NOT EXISTS current (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            name TEXT NOT NULL,
            category TEXT NOT NULL,
            start_time TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS categories (
            name TEXT PRIMARY KEY,
            keywords TEXT NOT NULL DEFAULT '[]',
            description TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS aliases (
            alias TEXT PRIMARY KEY,
            standard_name TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_aliases_standard ON aliases(standard_name);
    """)
    conn.commit()
    return conn


def migrate_events(conn, events):
    if not events:
        return 0, 0
    inserted = 0
    skipped = 0
    for ev in events:
        try:
            conn.execute(
                "INSERT OR IGNORE INTO events (id, name, category, start_time, end_time, duration_minutes) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    ev.get("id", ev.get("name", "")[:8]),
                    ev["name"],
                    ev.get("category", "其他"),
                    ev["start_time"],
                    ev["end_time"],
                    ev.get("duration_minutes", 0)
                )
            )
            if conn.total_changes > 0:
                inserted += 1
            else:
                skipped += 1
        except Exception as e:
            print(f"  跳过事件 '{ev.get('name', '?')}': {e}", file=sys.stderr)
            skipped += 1
    return inserted, skipped


def migrate_current(conn, current):
    if not current:
        return False
    conn.execute(
        "INSERT OR REPLACE INTO current (id, name, category, start_time) VALUES (1, ?, ?, ?)",
        (current["name"], current.get("category", "其他"), current["start_time"])
    )
    return True


def migrate_categories(conn, categories_data):
    if not categories_data or "categories" not in categories_data:
        return 0
    inserted = 0
    for cat in categories_data["categories"]:
        conn.execute(
            "INSERT OR IGNORE INTO categories (name, keywords, description, created_at) VALUES (?, ?, ?, ?)",
            (
                cat["name"],
                json.dumps(cat.get("keywords", []), ensure_ascii=False),
                cat.get("description", ""),
                cat.get("created_at", datetime.now(TZ).isoformat())
            )
        )
        inserted += 1
    # 确保默认类别存在
    conn.execute(
        "INSERT OR IGNORE INTO categories (name, keywords, description, created_at) VALUES (?, ?, ?, ?)",
        ("其他", "[]", "临时性、短时间、未分类事件", datetime.now(TZ).isoformat())
    )
    return inserted


def migrate_aliases(conn, aliases_data):
    if not aliases_data or "aliases" not in aliases_data:
        return 0
    inserted = 0
    for alias, standard in aliases_data["aliases"].items():
        conn.execute(
            "INSERT OR REPLACE INTO aliases (alias, standard_name) VALUES (?, ?)",
            (alias, standard)
        )
        inserted += 1
    return inserted


def main():
    parser = argparse.ArgumentParser(description="JSONL → SQLite 数据迁移")
    parser.add_argument("--source", type=str, default=None,
                        help="源数据目录（包含 events.jsonl 等文件），默认: 脚本同级 data/")
    parser.add_argument("--target", type=str, default=None,
                        help=f"目标 SQLite 数据库路径，默认: {DEFAULT_TARGET_DB}")
    parser.add_argument("--dry-run", action="store_true",
                        help="只统计不实际写入")
    args = parser.parse_args()

    source_dir = Path(args.source) if args.source else SCRIPT_DIR / "data"
    target_db = Path(args.target) if args.target else DEFAULT_TARGET_DB

    print("=" * 50)
    print("  JSONL → SQLite 数据迁移")
    print("=" * 50)
    print(f"源目录: {source_dir}")
    print(f"目标数据库: {target_db}")
    print(f"模式: {'预览（不写入）' if args.dry_run else '实际写入'}")
    print()

    # 检查源文件
    events_file = source_dir / "events.jsonl"
    current_file = source_dir / "current.json"
    categories_file = source_dir / "categories.json"
    aliases_file = source_dir / "aliases.json"

    print("--- 源文件检查 ---")
    for f, name in [(events_file, "events.jsonl"), (current_file, "current.json"),
                     (categories_file, "categories.json"), (aliases_file, "aliases.json")]:
        status = "存在" if f.exists() else "不存在"
        size = f.stat().st_size if f.exists() else 0
        print(f"  {name}: {status} ({size} bytes)")
    print()

    # 加载源数据
    print("--- 加载源数据 ---")
    events = load_jsonl(events_file)
    current = load_json(current_file, None)
    categories = load_json(categories_file, None)
    aliases = load_json(aliases_file, None)
    print(f"  事件记录: {len(events)} 条")
    print(f"  当前事件: {'有' if current else '无'}")
    print(f"  类别配置: {'有' if categories else '无'}")
    print(f"  别名映射: {'有' if aliases else '无'}")
    print()

    if not any([events, current, categories, aliases]):
        print("源目录中没有找到任何数据，无需迁移。")
        return

    if args.dry_run:
        print("预览模式，未实际写入。")
        return

    # 执行迁移
    print("--- 执行迁移 ---")
    conn = init_target_db(target_db)
    try:
        ev_inserted, ev_skipped = migrate_events(conn, events)
        print(f"  事件: 导入 {ev_inserted} 条，跳过 {ev_skipped} 条（重复或错误）")

        cur_migrated = migrate_current(conn, current)
        print(f"  当前事件: {'已导入' if cur_migrated else '无'}")

        cat_count = migrate_categories(conn, categories)
        print(f"  类别: 导入 {cat_count} 个")

        alias_count = migrate_aliases(conn, aliases)
        print(f"  别名: 导入 {alias_count} 条")

        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"迁移失败: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        conn.close()

    print()
    print("--- 验证 ---")
    conn = sqlite3.connect(str(target_db))
    conn.row_factory = sqlite3.Row
    ev_count = conn.execute("SELECT COUNT(*) as cnt FROM events").fetchone()["cnt"]
    cur_count = conn.execute("SELECT COUNT(*) as cnt FROM current").fetchone()["cnt"]
    cat_count = conn.execute("SELECT COUNT(*) as cnt FROM categories").fetchone()["cnt"]
    alias_count = conn.execute("SELECT COUNT(*) as cnt FROM aliases").fetchone()["cnt"]
    conn.close()

    print(f"  events 表: {ev_count} 条")
    print(f"  current 表: {cur_count} 条")
    print(f"  categories 表: {cat_count} 个")
    print(f"  aliases 表: {alias_count} 条")
    print()
    print("迁移完成！")
    print(f"数据库文件: {target_db}")
    print()
    print("注意: 原始 JSONL 文件未被删除，可作为备份保留。")
    print("      确认数据无误后可手动删除源 data/ 目录。")


if __name__ == "__main__":
    main()
