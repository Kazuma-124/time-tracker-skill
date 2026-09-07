#!/usr/bin/env python3
"""
柳比歇夫时间统计法 - 核心追踪脚本（SQLite 存储版）
数据存储在独立项目目录下的 SQLite 数据库中（默认 ~/workspace/time-tracker-data/time-tracker.db），
可通过环境变量 TIME_TRACKER_DATA_DIR 覆盖数据目录。
"""

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from collections import defaultdict

# 文件锁（防止并发写入）
try:
    import fcntl
    _HAS_FCNTL = True
except ImportError:
    _HAS_FCNTL = False

# ============ 配置 ============
SCRIPT_DIR = Path(__file__).resolve().parent

# 数据目录：优先环境变量 TIME_TRACKER_DATA_DIR，否则默认指向 workspace 下的 time-tracker-data
_DEFAULT_DATA_DIR = Path.home() / ".super_doubao" / "super-doubao-runtime" / "workspace" / "time-tracker-data"
DATA_DIR = Path(os.environ.get("TIME_TRACKER_DATA_DIR", str(_DEFAULT_DATA_DIR)))
DB_PATH = DATA_DIR / "time-tracker.db"
TEST_DB_PATH = DATA_DIR / "test-time-tracker.db"
TEST_MODE = False  # 测试模式标志，由 main 函数根据 --test 参数设置
LAST_BACKUP_SUCCESS = True  # 最近一次备份是否成功，用于决定是否删除本地数据库

TZ = timezone(timedelta(hours=8))  # Asia/Shanghai


# ============ 数据库管理 ============
def ensure_data_dir():
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def check_db_integrity(db_path=None):
    """检查数据库文件完整性，返回 (is_valid, error_message)。"""
    path = db_path or DB_PATH
    if not os.path.exists(path):
        return False, "数据库文件不存在"
    try:
        conn = sqlite3.connect(str(path))
        # 快速完整性检查
        result = conn.execute("PRAGMA quick_check").fetchone()
        if result[0] != "ok":
            conn.close()
            return False, f"完整性检查失败: {result[0]}"
        # 检查关键表是否存在
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        required = ["events", "current", "categories", "aliases", "standard_names"]
        missing = [t for t in required if t not in tables]
        if missing:
            conn.close()
            return False, f"缺少关键表: {missing}"
        conn.close()
        return True, "ok"
    except sqlite3.DatabaseError as e:
        return False, f"数据库损坏: {e}"
    except Exception as e:
        return False, f"检查异常: {e}"


def acquire_db_lock():
    """获取数据库文件锁，防止并发写入。返回锁文件句柄或 None。"""
    if not _HAS_FCNTL:
        return None
    lock_path = str(DB_PATH) + ".lock"
    try:
        lock_file = open(lock_path, "w")
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return lock_file
    except (IOError, OSError):
        # 获取不到锁，等待重试
        for _ in range(10):
            time.sleep(0.5)
            try:
                lock_file = open(lock_path, "w")
                fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return lock_file
            except (IOError, OSError):
                continue
        print("[警告] 无法获取数据库锁，可能有其他进程正在写入")
        return None


def release_db_lock(lock_file):
    """释放数据库文件锁。"""
    if lock_file and _HAS_FCNTL:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_UN)
            lock_file.close()
        except Exception:
            pass


@contextmanager
def get_db():
    """获取数据库连接，自动提交/回滚/关闭。
    包含完整性检查和文件锁保护。
    """
    ensure_data_dir()
    # 完整性检查（仅对已存在的数据库）
    if os.path.exists(DB_PATH):
        valid, err = check_db_integrity()
        if not valid:
            print(f"[数据库警告] 本地数据库可能损坏: {err}")
            print("[数据库警告] 尝试从飞书恢复...")
            if restore_db_from_lark():
                print("[数据库警告] 已从飞书恢复数据库")
            else:
                print("[数据库警告] 飞书恢复失败，将尝试继续使用本地数据库")
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    lock_file = acquire_db_lock()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
        release_db_lock(lock_file)


@contextmanager
def db_transaction():
    """数据库修改的统一入口。
    
    保证所有修改操作都遵循完整流程：
    1. 从飞书拉取最新数据库（覆盖本地）
    2. 初始化表结构和时间连续性
    3. 在本地执行修改（yield conn）
    4. 提交事务
    5. 上传修改后的数据库到飞书（含验证和旧版本清理）
    
    所有对数据库有写入操作的命令必须使用此入口。
    只读操作（stats/current/list-events等）仍使用 init_db() + get_db()。
    """
    # 1. 从飞书拉取最新数据库
    synced = sync_db_from_lark()
    if not synced and not os.path.exists(DB_PATH):
        raise RuntimeError("从飞书拉取数据库失败，且无本地数据库，无法执行修改操作")
    
    # 2. 初始化表结构和时间连续性
    with get_db() as conn:
        _init_db_tables(conn)
        ensure_current_event(conn)
    
    # 3. 打开连接执行修改
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    lock_file = acquire_db_lock()
    
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
        release_db_lock(lock_file)
    
    # 4. 上传修改后的数据库到飞书（含验证和旧版本清理）
    global LAST_BACKUP_SUCCESS
    LAST_BACKUP_SUCCESS = backup_database()
    if not LAST_BACKUP_SUCCESS:
        print("[数据安全] 备份失败，本地数据库将保留，不会被删除")
    # 注意：不在此处删除本地数据库，因为命令函数可能在 db_transaction 之后还需要访问数据库
    # 本地数据库的统一清理在 main 函数的 finally 块中进行


def restore_db_from_lark():
    """从飞书云空间恢复数据库。返回 True 表示恢复成功。"""
    backup_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backup_to_lark.py")
    if not os.path.exists(backup_script):
        print(f"[数据库恢复] 错误: 备份脚本不存在: {backup_script}")
        return False
    print("[数据库恢复] 正在从飞书云空间拉取最新数据库...")
    result = subprocess.run(
        [sys.executable, backup_script, "--restore"],
        capture_output=True, text=True
    )
    if result.returncode == 0 and os.path.exists(DB_PATH):
        print("[数据库恢复] ✅ 成功从飞书恢复数据库")
        return True
    else:
        print(f"[数据库恢复] 飞书无备份或恢复失败: {result.stderr.strip()[:200] or result.stdout.strip()[:200]}")
        return False


def cleanup_local_db():
    """删除本地数据库及其相关文件。
    
    飞书是唯一的权威数据源，本地只作为临时工作区。
    每次操作完成后调用此函数，确保本地不残留数据库文件。
    删除的文件包括：数据库文件、WAL文件、SHM文件、锁文件、同步备份文件。
    """
    files_to_clean = [
        str(DB_PATH),
        str(DB_PATH) + "-wal",
        str(DB_PATH) + "-shm",
        str(DB_PATH) + ".lock",
    ]
    import glob
    # 清理 syncbak 备份文件（sync_db_from_lark 创建）
    for f in glob.glob(str(DB_PATH) + ".syncbak.*"):
        files_to_clean.append(f)
    # 清理 bak 备份文件（backup_to_lark.py --restore 创建，恢复成功后无用）
    for f in glob.glob(str(DB_PATH) + ".bak.*"):
        files_to_clean.append(f)
    # 清理 downloading 临时文件
    for f in glob.glob(str(DB_PATH) + ".downloading*"):
        files_to_clean.append(f)
    
    cleaned = []
    for f in files_to_clean:
        if os.path.exists(f):
            try:
                os.remove(f)
                cleaned.append(os.path.basename(f))
            except Exception as e:
                print(f"[清理] 警告: 无法删除 {os.path.basename(f)}: {e}")
    
    if cleaned:
        print(f"[清理] 已删除本地临时数据库文件: {', '.join(cleaned)}")


def sync_db_from_lark():
    """每次操作前从飞书拉取最新数据库，覆盖本地。
    
    保证任何对话操作的都是飞书上的最新数据，避免环境隔离导致数据不同步。
    拉取失败时返回 False，调用方应停止操作。
    """
    backup_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backup_to_lark.py")
    if not os.path.exists(backup_script):
        print("[数据同步] ⚠️  备份脚本不存在，将使用本地数据库")
        return True  # 脚本不存在时降级使用本地（极端情况）
    
    # 如果本地有数据库，先备份（防止拉取失败导致数据丢失）
    local_backup = None
    if os.path.exists(DB_PATH):
        local_backup = str(DB_PATH) + f".syncbak.{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        shutil.copy2(DB_PATH, local_backup)
    
    # 从飞书拉取最新数据库
    result = subprocess.run(
        [sys.executable, backup_script, "--restore"],
        capture_output=True, text=True, timeout=60
    )
    
    if result.returncode == 0 and os.path.exists(DB_PATH):
        # 拉取成功，删除本地备份
        if local_backup and os.path.exists(local_backup):
            os.remove(local_backup)
        return True
    else:
        # 拉取失败，恢复本地备份
        if local_backup and os.path.exists(local_backup):
            shutil.move(local_backup, DB_PATH)
            print("[数据同步] ⚠️  从飞书拉取失败，已恢复本地数据库")
        else:
            print("[数据同步] ❌ 从飞书拉取失败，且无本地备份")
        err = result.stderr.strip() or result.stdout.strip()
        print(f"  详情: {err[:200]}")
        return False


def upload_db_to_lark():
    """将本地数据库上传到飞书云空间。返回 True 表示上传成功。"""
    backup_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backup_to_lark.py")
    if not os.path.exists(backup_script):
        print(f"[数据库上传] 错误: 备份脚本不存在: {backup_script}")
        return False
    print("[数据库上传] 正在将空数据库上传到飞书云空间...")
    result = subprocess.run(
        [sys.executable, backup_script, "--force"],
        capture_output=True, text=True
    )
    if result.returncode == 0:
        print("[数据库上传] ✅ 空数据库已上传到飞书云空间")
        return True
    else:
        print(f"[数据库上传] ⚠️  上传失败: {result.stderr.strip()[:200] or result.stdout.strip()[:200]}")
        return False


def init_db():
    """初始化数据库。
    
    每次操作前都从飞书拉取最新数据库，保证数据一致性。
    流程：
    1. 从飞书同步最新数据库（覆盖本地）
    2. 初始化表结构（幂等）
    3. 确保有进行中事件（时间连续性）
    4. 首次使用时创建空数据库并上传
    """
    # 第一步：从飞书拉取最新数据库
    synced = sync_db_from_lark()
    
    if not synced and not os.path.exists(DB_PATH):
        # 拉取失败且本地无数据库 → 首次使用，创建空数据库
        print()
        print("[初始化] 检测到首次使用，正在创建空数据库...")
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        with get_db() as conn:
            _init_db_tables(conn)
            ensure_current_event(conn)
        print("[初始化] ✅ 空数据库已创建")
        upload_db_to_lark()
        return

    # 第二步：初始化表结构 + 保证时间连续
    try:
        with get_db() as conn:
            _init_db_tables(conn)
            ensure_current_event(conn)
    except Exception as e:
        print(f"[初始化错误] 数据库初始化失败: {e}")
        print("[初始化错误] 尝试从飞书恢复...")
        if restore_db_from_lark():
            with get_db() as conn:
                _init_db_tables(conn)
                ensure_current_event(conn)
        else:
            print("[初始化错误] 无法恢复数据库，请检查飞书备份")
            raise


def backup_database():
    """将数据库备份到飞书云空间（在每次数据库修改操作后调用）。
    
    包含：备份前完整性检查、自动重试（最多5次）、失败警告。
    返回 True 表示备份成功，False 表示失败。
    测试模式下跳过备份，避免测试数据污染生产备份。
    """
    if TEST_MODE:
        print("[测试模式] 跳过飞书备份（测试数据不污染生产备份）")
        return True
    
    backup_script = SCRIPT_DIR / "backup_to_lark.py"
    if not backup_script.exists():
        print("[备份错误] 备份脚本不存在，无法备份")
        print("[备份错误] 本地数据库将保留，请稍后手动运行 backup_to_lark.py 进行备份")
        return False

    # 备份前检查数据库完整性
    valid, err = check_db_integrity()
    if not valid:
        print(f"[备份错误] 本地数据库完整性检查失败: {err}")
        print("[备份错误] 跳过备份以避免覆盖飞书的完好备份")
        print("[备份错误] 本地数据库将保留，请检查数据库状态")
        return False

    # 自动重试最多5次，确保上传成功
    last_error = ""
    for attempt in range(5):
        try:
            result = subprocess.run(
                [sys.executable, str(backup_script)],
                capture_output=True,
                text=True,
                timeout=90
            )
            # 主要通过退出码判断成功（backup_to_lark.py 已修复为失败返回非零退出码）
            # 同时检查输出中是否包含最终成功标记作为双重保险
            # 注意：不能检查"失败"字样，因为重试成功时输出中会包含之前的失败信息
            output = result.stdout + result.stderr
            if result.returncode == 0 and ("✅ 成功" in output or "✅ 已备份" in output or "无需上传" in output or "跳过" in output):
                return True  # 备份成功
            else:
                last_error = output[-300:] if output else "未知错误"
                if attempt < 4:  # 前4次失败都重试，第5次失败后退出
                    print(f"  [备份] 第 {attempt+1} 次尝试失败，2秒后重试...")
                    time.sleep(2)
                    continue
        except subprocess.TimeoutExpired:
            last_error = "备份超时（90秒）"
            if attempt < 4:
                print(f"  [备份] 第 {attempt+1} 次尝试超时，2秒后重试...")
                time.sleep(2)
                continue
        except Exception as e:
            last_error = str(e)
            if attempt < 4:
                print(f"  [备份] 第 {attempt+1} 次尝试异常，2秒后重试...")
                time.sleep(2)
                continue
        break

    # 所有重试都失败
    print()
    print("=" * 60)
    print("[备份错误] 数据库备份到飞书失败（已重试5次）")
    print(f"  详情: {last_error[:300]}")
    print()
    print("  本地数据库将保留，不会被删除。")
    print("  建议: 稍后手动运行以下命令进行备份:")
    print(f"    python3 {backup_script}")
    print("=" * 60)
    return False


def _init_db_tables(conn):
    """初始化数据库表结构和默认数据（内部函数，由 init_db 调用）。"""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS events (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            start_time TEXT NOT NULL,
            end_time TEXT NOT NULL,
            duration_minutes REAL NOT NULL,
            extras TEXT NOT NULL DEFAULT ''
        );

        CREATE INDEX IF NOT EXISTS idx_events_start ON events(start_time);
        CREATE INDEX IF NOT EXISTS idx_events_name ON events(name);
    """)

    # 迁移：旧表有 category 字段时重建表删除该字段
    cols = [row[1] for row in conn.execute("PRAGMA table_info(events)").fetchall()]
    if "category" in cols:
        print("[数据库迁移] 删除 events 表的 category 字段（分类通过映射实时获取）...")
        conn.execute("""
            CREATE TABLE events_new (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                start_time TEXT NOT NULL,
                end_time TEXT NOT NULL,
                duration_minutes REAL NOT NULL,
                extras TEXT NOT NULL DEFAULT ''
            )
        """)
        conn.execute("""
            INSERT INTO events_new (id, name, start_time, end_time, duration_minutes, extras)
            SELECT id, name, start_time, end_time, duration_minutes, extras FROM events
        """)
        conn.execute("DROP TABLE events")
        conn.execute("ALTER TABLE events_new RENAME TO events")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_start ON events(start_time)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_name ON events(name)")
        print("[数据库迁移] ✅ events 表已删除 category 字段")

    # 迁移：旧表没有 extras 字段时添加
    cols = [row[1] for row in conn.execute("PRAGMA table_info(events)").fetchall()]
    if "extras" not in cols:
        conn.execute("ALTER TABLE events ADD COLUMN extras TEXT NOT NULL DEFAULT ''")
        print("[数据库迁移] events 表已添加 extras 字段")

    # 迁移：旧 categories 表没有 id/parent_id 字段时重建
    cols = [row[1] for row in conn.execute("PRAGMA table_info(categories)").fetchall()]
    if "id" not in cols or "parent_id" not in cols:
        print("[数据库迁移] 重建 categories 表以支持多级分类...")
        # 备份旧数据
        old_cats = conn.execute("SELECT name, keywords, description, created_at FROM categories").fetchall()
        conn.execute("DROP TABLE categories")
        conn.execute("""
            CREATE TABLE categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                parent_id INTEGER,
                keywords TEXT NOT NULL DEFAULT '[]',
                description TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                FOREIGN KEY (parent_id) REFERENCES categories(id)
            )
        """)
        for cat in old_cats:
            conn.execute(
                "INSERT INTO categories (name, parent_id, keywords, description, created_at) VALUES (?, NULL, ?, ?, ?)",
                (cat["name"], cat["keywords"], cat["description"], cat["created_at"])
            )
        print(f"[数据库迁移] 已迁移 {len(old_cats)} 个分类")

    conn.executescript("""

        CREATE TABLE IF NOT EXISTS current (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            name TEXT NOT NULL,
            start_time TEXT NOT NULL
        );
    """)

    # 迁移：旧 current 表有 category 字段时重建
    cols = [row[1] for row in conn.execute("PRAGMA table_info(current)").fetchall()]
    if "category" in cols:
        print("[数据库迁移] 删除 current 表的 category 字段...")
        conn.execute("""
            CREATE TABLE current_new (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                name TEXT NOT NULL,
                start_time TEXT NOT NULL
            )
        """)
        conn.execute("INSERT INTO current_new (id, name, start_time) SELECT id, name, start_time FROM current")
        conn.execute("DROP TABLE current")
        conn.execute("ALTER TABLE current_new RENAME TO current")
        print("[数据库迁移] ✅ current 表已删除 category 字段")

    conn.executescript("""
        CREATE TABLE IF NOT EXISTS categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            parent_id INTEGER,
            keywords TEXT NOT NULL DEFAULT '[]',
            description TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY (parent_id) REFERENCES categories(id)
        );

        CREATE TABLE IF NOT EXISTS aliases (
            alias TEXT PRIMARY KEY,
            standard_name TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_aliases_standard ON aliases(standard_name);

        CREATE TABLE IF NOT EXISTS standard_names (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            category_id INTEGER,
            description TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (category_id) REFERENCES categories(id)
        );

        CREATE INDEX IF NOT EXISTS idx_standard_names_category ON standard_names(category_id);
    """)

    # 特殊分类和标准名：未记录（时间连续性补全用）
    conn.execute(
        "INSERT OR IGNORE INTO categories (name, keywords, description, created_at) VALUES (?, ?, ?, ?)",
        ("未记录", "[]", "未记录的空白时间", now_iso())
    )
    unrecorded_cat = conn.execute("SELECT id FROM categories WHERE name = '未记录'").fetchone()
    if unrecorded_cat:
        conn.execute(
            "INSERT OR IGNORE INTO standard_names (name, category_id, description, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            ("未记录", unrecorded_cat["id"], "未记录的空白时间", now_iso(), now_iso())
        )
    
    # 特殊原始名映射：未记录 → 未记录（所有原始名都必须显式建立映射）
    conn.execute(
        "INSERT OR IGNORE INTO aliases (alias, standard_name) VALUES (?, ?)",
        ("未记录", "未记录")
    )
    
    # 注意：其他标准名和分类不再自动创建，由模型决定后手动创建


def ensure_current_event(conn):
    """确保 current 表中有事件在进行中。
    
    柳比歇夫时间管理法要求任何时刻都有事件在进行中。
    如果 current 为空但 events 有数据，自动从最新事件结束时间开始创建'未记录'事件。
    如果完全没有数据，创建从现在开始的'未记录'事件。
    """
    current = conn.execute("SELECT id FROM current WHERE id = 1").fetchone()
    if current:
        return  # 已有进行中事件，无需处理

    latest = conn.execute("SELECT end_time FROM events ORDER BY end_time DESC LIMIT 1").fetchone()
    if latest:
        start_time = latest[0]
        print(f"[时间连续性] 检测到无进行中事件，已从最新事件结束时间({parse_iso(start_time).strftime('%m-%d %H:%M')})自动开始'未记录'事件")
    else:
        start_time = now_iso()
        print("[时间连续性] 首次使用，已自动开始'未记录'事件")

    conn.execute(
        "INSERT OR REPLACE INTO current (id, name, start_time) VALUES (1, '未记录', ?)",
        (start_time,)
    )


# ============ 工具函数 ============
def now_iso():
    return datetime.now(TZ).isoformat()


def parse_iso(s):
    return datetime.fromisoformat(s)


def format_duration(minutes):
    if minutes < 60:
        return f"{minutes:.1f} 分钟"
    hours = int(minutes // 60)
    mins = minutes % 60
    return f"{hours} 小时 {mins:.0f} 分钟"


def build_name_library(conn):
    """从数据库构建名称库。"""
    rows = conn.execute(
        "SELECT name, COUNT(*) as cnt, SUM(duration_minutes) as total, "
        "MIN(start_time) as first_seen, MAX(end_time) as last_seen "
        "FROM events GROUP BY name"
    ).fetchall()
    library = {}
    for r in rows:
        library[r["name"]] = {
            "count": r["cnt"],
            "total_minutes": r["total"] or 0,
            "first_seen": r["first_seen"],
            "last_seen": r["last_seen"]
        }
    return library

# ============ 原始名映射 ============
def load_aliases(conn):
    rows = conn.execute("SELECT alias, standard_name FROM aliases").fetchall()
    return {r["alias"]: r["standard_name"] for r in rows}


def cmd_aliases_list(args):
    init_db()
    with get_db() as conn:
        aliases = load_aliases(conn)
    print("=== 事件原始名映射 ===")
    if not aliases:
        print("(暂无原始名映射)")
        return
    for alias, standard in sorted(aliases.items()):
        print(f"  '{alias}' → '{standard}'")
    print(f"\n共 {len(aliases)} 条映射")


def cmd_alias_add(args):
    alias = args.alias.strip()
    standard = args.standard.strip()
    with db_transaction() as conn:
        # 校验1：如果 alias 是已有的标准名，只允许映射到自己（不能形成链式映射）
        is_standard_name = conn.execute("SELECT 1 FROM standard_names WHERE name = ?", (alias,)).fetchone()
        if is_standard_name and alias != standard:
            print(f"错误: '{alias}' 已经是一个标准名，只能映射到自己，不能映射到其他标准名。")
            print("标准名只能被原始名映射到，不能形成链式映射。如需同名映射请使用: add-alias '{alias}' '{alias}'")
            return
        
        # 校验2：standard 不能已存在于 alias 列且映射到其他标准名（不能形成链式映射）
        # 同名映射（standard 作为原始名映射到自己）是允许的，因为原始名与标准名可以相同
        if alias != standard:
            alias_row = conn.execute("SELECT standard_name FROM aliases WHERE alias = ?", (standard,)).fetchone()
            if alias_row and alias_row["standard_name"] != standard:
                print(f"错误: '{standard}' 已经是一个原始名且映射到 '{alias_row['standard_name']}'，不能作为标准名被映射（会形成链式映射）。")
                print("标准名只能被原始名映射到，不能形成链式映射。")
                return
        
        existing = conn.execute("SELECT standard_name FROM aliases WHERE alias = ?", (alias,)).fetchone()
        if existing:
            print(f"提示: 原始名 '{alias}' 已映射到 '{existing['standard_name']}'，将覆盖。")
        
        # 标准名必须已存在（由模型决定后手动创建）
        std = get_standard_name(conn, standard)
        if not std:
            print(f"错误: 标准名 '{standard}' 不存在。请先创建标准名再建立映射。")
            return
        
        conn.execute(
            "INSERT OR REPLACE INTO aliases (alias, standard_name) VALUES (?, ?)",
            (alias, standard)
        )
    if alias == standard:
        print(f"已添加同名映射: '{alias}' → '{standard}'（原始名与标准名相同，显式建立映射）")
    else:
        print(f"已添加映射: '{alias}' → '{standard}'")
    print("注意: 历史事件名称不会被修改，统计时会自动映射显示。")


def cmd_alias_remove(args):
    alias = args.alias.strip()
    with db_transaction() as conn:
        existing = conn.execute("SELECT standard_name FROM aliases WHERE alias = ?", (alias,)).fetchone()
        if not existing:
            print(f"错误: 原始名 '{alias}' 不存在")
            return
        conn.execute("DELETE FROM aliases WHERE alias = ?", (alias,))
    print(f"已删除映射: '{alias}' → '{existing['standard_name']}'")

# ============ 名称一致性检查 ============

def cmd_name_check(args):
    init_db()
    if args.period:
        period = args.period
        ref_str = args.date if args.date else None
        start, end = get_period_range(period, ref_str)
        period_cn = {"day": "日", "week": "周", "month": "月", "quarter": "季度", "year": "年"}.get(period, period)
        period_label = f"本{period_cn} ({start.strftime('%Y-%m-%d')} ~ {end.strftime('%Y-%m-%d')})"
    else:
        period = "day"
        if args.date:
            ref = datetime.strptime(args.date, "%Y-%m-%d").replace(tzinfo=TZ)
        else:
            ref = datetime.now(TZ)
        start = ref.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        period_label = ref.strftime('%Y-%m-%d')

    with get_db() as conn:
        name_library = build_name_library(conn)
        aliases = load_aliases(conn)

        rows = conn.execute(
            "SELECT DISTINCT name FROM events WHERE start_time >= ? AND start_time < ?",
            (start.isoformat(), end.isoformat())
        ).fetchall()
        period_names = [r["name"] for r in rows]

        current = conn.execute("SELECT name, start_time FROM current WHERE id = 1").fetchone()
        if current and start.isoformat() <= current["start_time"] < end.isoformat():
            period_names.append(current["name"])

        # 获取未映射的原始名
        # 规则：每个原始名都必须在 aliases 表中显式建立映射
        # 即使原始名与标准名相同，也要建立 "同名→同名" 的映射
        period_names_set = list(set(period_names))
        unmapped = [name for name in period_names_set if name not in aliases]

        # 获取标准名及其分类
        std_rows = conn.execute("""
            SELECT s.name, c.name as cat_name
            FROM standard_names s
            LEFT JOIN categories c ON s.category_id = c.id
            ORDER BY c.name, s.name
        """).fetchall()

    period_names = period_names_set
    if not period_names:
        print(f"{period_label} 暂无事件记录")
        return

    print(f"=== 事件名称一致性检查 ({period_label}) ===")
    period_cn = {"day": "日", "week": "周", "month": "月", "quarter": "季度", "year": "年"}.get(period, period)
    print(f"本{period_cn}事件名称 ({len(period_names)} 种):")
    for n in sorted(period_names):
        print(f"  - {n}")
    print()

    print("--- 历史名称库（供模型分析映射）---")
    print(f"历史名称共 {len(name_library)} 种（按总时长排序）:")
    sorted_names = sorted(name_library.items(), key=lambda x: x[1]["total_minutes"], reverse=True)
    for name, info in sorted_names:
        alias_mark = " ←原始名" if name in aliases else ""
        print(f"  - {name}{alias_mark}")
        print(f"    次数: {info['count']}, 总时长: {info['total_minutes']:.0f}分钟, 首次: {info['first_seen'][:10] if info['first_seen'] else 'N/A'}, 末次: {info['last_seen'][:10] if info['last_seen'] else 'N/A'}")
    print()

    # 标准名集合：从 standard_names 表查询所有标准名（包括尚未被映射的）
    all_standard_names = sorted([r["name"] for r in std_rows])
    if all_standard_names:
        print("--- 标准名集合（所有已定义的标准名，可作为映射候选）---")
        for name in all_standard_names:
            info = name_library.get(name, {"count": 0, "total_minutes": 0})
            mapped_count = sum(1 for v in aliases.values() if v == name)
            print(f"  - {name} (历史次数: {info['count']}, 总时长: {info['total_minutes']:.0f}分钟, 映射原始名数: {mapped_count})")
        print()

    if aliases:
        print("--- 已有原始名映射（左侧名称跳过，不要重复处理）---")
        for alias, standard in sorted(aliases.items()):
            print(f"  '{alias}' → '{standard}'")
        print()

    # 检查未映射的原始名
    print("--- 未映射检查 ---")
    if unmapped:
        print(f"⚠️  以下原始名未映射到任何标准名（必须处理）:")
        for name in sorted(unmapped):
            info = name_library.get(name, {})
            print(f"  - {name} (次数: {info.get('count', 0)}, 总时长: {info.get('total_minutes', 0):.0f}分钟)")
    else:
        print("✅ 所有原始名都已映射到标准名")
    print()

    # 标准名及其分类
    print("--- 标准名与分类（所有标准名必须有分类）---")
    uncategorized = []
    for r in std_rows:
        cat = r["cat_name"] if r["cat_name"] else "❌ 未分类"
        print(f"  - {r['name']} → {cat}")
        if not r["cat_name"]:
            uncategorized.append(r["name"])
    print()
    if uncategorized:
        print(f"⚠️  以下标准名未分类（必须处理）: {', '.join(uncategorized)}")
        print()

    print("=" * 50)
    print("【任务背景】")
    print("这是柳比歇夫时间管理法中的事件名称与分类管理任务。")
    print("目标：确保所有原始名都映射到标准名，所有标准名都归到分类，标准名能精确描述事件。")
    print()
    print("【核心规则】")
    print("1. 原始名都必须映射到一个标准名（必须通过 aliases 表显式建立映射，即使原始名与标准名相同也要建立）")
    print("2. 标准名都必须映射到一个分类名（通过 standard_names.category_id）")
    print("3. 标准名应该能准确描述所涵盖的原始名表示的事件，且只能表示一个事件")
    print("4. 特殊事件'未记录'映射到特殊标准名'未记录'，归为特殊分类'未记录'")
    print("5. 原始名、标准名、分类名之间可以互相重复（本质不是同一类对象）")
    print("6. 所有标准名、分类名、映射关系由模型决定，脚本不自动创建")
    print()
    print("【分析要点】")
    print("1. 未映射的原始名：分析应映射到哪个已有标准名，或需要创建新的标准名")
    print("2. 未分类的标准名：分析应归到哪个已有分类，或需要创建新的分类")
    print("3. 相似名称：判断是否描述同一事件，若是则建立映射")
    print("4. 标准名准确性：现有标准名是否能精确描述事件，是否需要改名或拆分")
    print("5. 分类合理性：分类是否需要调整、合并、拆分或改名")
    print()
    print("【输出格式】")
    print("模型应输出结构化 JSON 方案：")
    print('{')
    print('  "new_standard_names": [{"name": "标准名", "category": "分类名", "reason": "理由"}],')
    print('  "new_categories": [{"name": "分类名", "parent": "父分类名或null", "reason": "理由"}],')
    print('  "new_mappings": [{"raw_name": "原始名", "standard_name": "标准名", "reason": "理由"}],')
    print('  "category_changes": [{"standard_name": "标准名", "new_category": "新分类名", "reason": "理由"}],')
    print('  "mapping_changes": [{"raw_name": "原始名", "old_standard": "旧标准名", "new_standard": "新标准名", "reason": "理由"}],')
    print('  "removals": [{"type": "mapping|standard_name|category", "name": "名称", "reason": "理由"}]')
    print('}')
    print()
    print("注意：只输出建议方案，不自动执行。用户确认后用对应命令执行。")
    print()
    print("【操作命令参考】")
    print("  创建标准名: python3 time_tracker.py add-standard-name '<名称>' --category '<分类>'")
    print("  创建分类: python3 time_tracker.py add-category '<名称>' --parent '<父分类>'")
    print("  添加映射: python3 time_tracker.py add-alias '<原始名>' '<标准名>'")
    print("  修改分类: python3 time_tracker.py set-standard-category '<标准名>' '<新分类>'")
    print("  移除映射: python3 time_tracker.py remove-alias '<原始名>'")
    print("=" * 50)


# ============ 分类管理 ============
def load_categories(conn):
    # 注意：keywords 字段为遗留字段，不再使用。分类通过 standard_names.category_id 关联。
    rows = conn.execute("SELECT id, name, parent_id, description, created_at FROM categories ORDER BY name").fetchall()
    return [{"id": r["id"], "name": r["name"], "parent_id": r["parent_id"],
             "description": r["description"], "created_at": r["created_at"]} for r in rows]


def get_category_children(categories, parent_id):
    """获取指定父分类的直接子分类列表"""
    return [c for c in categories if c["parent_id"] == parent_id]


def get_category_descendants(categories, parent_id):
    """获取指定父分类的所有后代分类（递归）"""
    result = []
    for child in get_category_children(categories, parent_id):
        result.append(child)
        result.extend(get_category_descendants(categories, child["id"]))
    return result


def get_category_tree(categories):
    """构建分类树结构，返回顶级分类列表（每个分类含 children 字段）"""
    cat_map = {c["id"]: {**c, "children": []} for c in categories}
    roots = []
    for c in categories:
        if c["parent_id"] is None:
            roots.append(cat_map[c["id"]])
        else:
            parent = cat_map.get(c["parent_id"])
            if parent:
                parent["children"].append(cat_map[c["id"]])
            else:
                # 父分类不存在，提升为顶级
                roots.append(cat_map[c["id"]])
    return roots


def print_category_tree(node, indent=0):
    """递归打印分类树"""
    prefix = "  " * indent
    print(f"{prefix}- {node['name']}")
    if node.get("description"):
        print(f"{prefix}  说明: {node['description']}")
    for child in node.get("children", []):
        print_category_tree(child, indent + 1)

def load_standard_names(conn):
    """加载所有标准名及其分类关联"""
    rows = conn.execute("""
        SELECT sn.id, sn.name, sn.category_id, sn.description, sn.created_at, sn.updated_at,
               c.name as category_name
        FROM standard_names sn
        LEFT JOIN categories c ON sn.category_id = c.id
        ORDER BY sn.name
    """).fetchall()
    return [{
        "id": r["id"],
        "name": r["name"],
        "category_id": r["category_id"],
        "category_name": r["category_name"] or "其他",
        "description": r["description"],
        "created_at": r["created_at"],
        "updated_at": r["updated_at"]
    } for r in rows]


def get_standard_name(conn, name):
    """查询标准名，返回标准名信息字典或 None。
    
    注意：不再自动创建标准名。所有标准名由模型决定后手动创建。
    """
    row = conn.execute("SELECT id, name, category_id FROM standard_names WHERE name = ?", (name,)).fetchone()
    if row:
        return {"id": row["id"], "name": row["name"], "category_id": row["category_id"]}
    return None


def resolve_standard_name(conn, raw_name):
    """将原始名解析为标准名。
    
    解析顺序：
    1. 先查 aliases 表，看 raw_name 是否是某个标准名的原始名
    2. 再查 standard_names 表，看 raw_name 本身是否是标准名
    3. 都找不到返回 None
    
    注意：不再自动创建，找不到就是找不到，需要模型先建立映射。
    """
    # 1. 查原始名映射
    alias_row = conn.execute("SELECT standard_name FROM aliases WHERE alias = ?", (raw_name,)).fetchone()
    if alias_row:
        standard_name = alias_row["standard_name"]
        std_row = conn.execute("SELECT id, name, category_id FROM standard_names WHERE name = ?", (standard_name,)).fetchone()
        if std_row:
            return {"id": std_row["id"], "name": std_row["name"], "category_id": std_row["category_id"], "is_alias": True}
        return None
    
    # 2. 查本身是否是标准名
    std_row = conn.execute("SELECT id, name, category_id FROM standard_names WHERE name = ?", (raw_name,)).fetchone()
    if std_row:
        return {"id": std_row["id"], "name": std_row["name"], "category_id": std_row["category_id"], "is_alias": False}
    
    return None


def get_category_name_for_standard(conn, standard_name):
    """根据标准名获取分类名称"""
    row = conn.execute("""
        SELECT c.name as category_name
        FROM standard_names sn
        LEFT JOIN categories c ON sn.category_id = c.id
        WHERE sn.name = ?
    """, (standard_name,)).fetchone()
    if row and row["category_name"]:
        return row["category_name"]
    return "其他"


def resolve_event_category(conn, raw_name):
    """通过原始名→标准名→分类的路径，实时解析事件的分类。
    
    不依赖 events 表中的冗余 category 字段，保证分类调整后实时生效。
    """
    std_info = resolve_standard_name(conn, raw_name)
    if std_info:
        return get_category_name_for_standard(conn, std_info["name"])
    return "其他"


def build_event_category_map(conn, event_names):
    """批量构建事件名称→分类的映射，用于统计查询。"""
    if not event_names:
        return {}
    # 构建原始名→标准名映射
    aliases = dict(conn.execute("SELECT alias, standard_name FROM aliases").fetchall())
    # 构建标准名→分类映射
    std_cats = dict(conn.execute("""
        SELECT sn.name, c.name FROM standard_names sn
        LEFT JOIN categories c ON sn.category_id = c.id
    """).fetchall())
    
    result = {}
    for name in event_names:
        standard = aliases.get(name, name)
        result[name] = std_cats.get(standard, "其他")
    return result


def set_standard_category(conn, standard_name, category_name):
    """设置标准名的分类"""
    cat = conn.execute("SELECT id FROM categories WHERE name = ?", (category_name,)).fetchone()
    if not cat:
        return False, f"分类 '{category_name}' 不存在"
    conn.execute(
        "UPDATE standard_names SET category_id = ?, updated_at = ? WHERE name = ?",
        (cat["id"], now_iso(), standard_name)
    )
    return True, f"标准名 '{standard_name}' 的分类已设置为 '{category_name}'"


def has_category_cycle(conn, category_id, parent_id):
    """检查设置 parent_id 是否会形成环路"""
    if parent_id is None:
        return False
    current = parent_id
    visited = set()
    while current is not None:
        if current == category_id:
            return True
        if current in visited:
            return True  # 已有环路
        visited.add(current)
        row = conn.execute("SELECT parent_id FROM categories WHERE id = ?", (current,)).fetchone()
        if not row:
            break
        current = row["parent_id"]
    return False


def cmd_categories_list(args):
    init_db()
    with get_db() as conn:
        cats = load_categories(conn)
    tree = get_category_tree(cats)
    print("=== 分类列表（树形结构）===")
    for root in tree:
        print_category_tree(root)
    print(f"\n共 {len(cats)} 个分类")


def cmd_category_add(args):
    name = args.name
    parent_name = getattr(args, 'parent', None)
    with db_transaction() as conn:
        existing = conn.execute("SELECT name FROM categories WHERE name = ?", (name,)).fetchone()
        if existing:
            print(f"错误: 分类 '{name}' 已存在")
            return
        parent_id = None
        if parent_name:
            parent = conn.execute("SELECT id FROM categories WHERE name = ?", (parent_name,)).fetchone()
            if not parent:
                print(f"错误: 父分类 '{parent_name}' 不存在")
                return
            parent_id = parent["id"]
            # 环路检测：新分类ID尚未创建，检查父链是否合理
            # 新分类不会形成环路（因为它还没有子分类），但检查父链是否已有环路
            if has_category_cycle(conn, -1, parent_id):
                print(f"错误: 父分类 '{parent_name}' 的父链中已存在环路，无法创建子分类")
                return
        conn.execute(
            "INSERT INTO categories (name, parent_id, keywords, description, created_at) VALUES (?, ?, ?, ?, ?)",
            (name, parent_id, "[]", args.description or "", now_iso())
        )
    print(f"已添加分类: {name}")
    if parent_name:
        print(f"  父分类: {parent_name}")
    # 关键词已废弃，不再显示


def cmd_category_remove(args):
    name = args.name
    if name == "其他":
        print("错误: 不能删除'其他'分类")
        return
    with db_transaction() as conn:
        existing = conn.execute("SELECT id, name FROM categories WHERE name = ?", (name,)).fetchone()
        if not existing:
            print(f"错误: 分类 '{name}' 不存在")
            return
        # 检查是否有子分类，有则提升为顶级
        children = conn.execute("SELECT name FROM categories WHERE parent_id = ?", (existing["id"],)).fetchall()
        if children:
            child_names = ", ".join(c["name"] for c in children)
            conn.execute("UPDATE categories SET parent_id = NULL WHERE parent_id = ?", (existing["id"],))
            print(f"提示: 子分类 [{child_names}] 已提升为顶级分类")
        conn.execute("DELETE FROM categories WHERE id = ?", (existing["id"],))
    print(f"已删除分类: {name}")
    print("注意: 历史事件中该分类的记录不会改变，仍保留原分类名称。")


def cmd_standard_names_list(args):
    """列出所有标准名及其分类"""
    init_db()
    with get_db() as conn:
        std_names = load_standard_names(conn)
    print("=== 标准名列表 ===")
    for s in std_names:
        print(f"  - {s['name']}")
        print(f"    分类: {s['category_name']}")
        if s['description']:
            print(f"    说明: {s['description']}")
    print(f"\n共 {len(std_names)} 个标准名")


def cmd_set_standard_category(args):
    """设置标准名的分类"""
    standard_name = args.standard_name.strip()
    category_name = args.category.strip()
    with db_transaction() as conn:
        std = get_standard_name(conn, standard_name)
        if not std:
            print(f"错误: 标准名 '{standard_name}' 不存在。请先创建标准名。")
            return
        success, msg = set_standard_category(conn, standard_name, category_name)
    print(msg)


def cmd_remove_standard_name(args):
    """删除标准名（仅当无原始名映射时，防止数据丢失）"""
    name = args.name.strip()
    with db_transaction() as conn:
        # 检查是否有原始名映射到该标准名
        mappings = conn.execute("SELECT alias FROM aliases WHERE standard_name = ?", (name,)).fetchall()
        if mappings:
            print(f"错误: 标准名 '{name}' 仍被 {len(mappings)} 个原始名映射，无法删除。")
            print(f"  映射的原始名: {', '.join(m['alias'] for m in mappings)}")
            print("  请先修改或删除这些映射后再删除标准名。")
            return
        # 检查标准名是否存在
        std = conn.execute("SELECT id FROM standard_names WHERE name = ?", (name,)).fetchone()
        if not std:
            print(f"错误: 标准名 '{name}' 不存在")
            return
        conn.execute("DELETE FROM standard_names WHERE name = ?", (name,))
    print(f"✅ 已删除标准名: {name}")


def cmd_add_standard_name(args):
    """创建新的标准名（由模型决定后手动执行）
    标准名必须映射到分类名，分类参数必填。
    分类名可以与标准名相同，如标准名"娱乐"→分类名"娱乐"。
    """
    name = args.name.strip()
    category = getattr(args, 'category', None)
    description = getattr(args, 'description', '') or ''
    
    if not category:
        print("错误: 标准名必须映射到分类名，--category 参数必填。")
        print("分类名可以与标准名相同，如: add-standard-name '娱乐' --category '娱乐'")
        print("如需创建分类，请先使用 add-category 命令。")
        return
    
    with db_transaction() as conn:
        existing = get_standard_name(conn, name)
        if existing:
            print(f"错误: 标准名 '{name}' 已存在。标准名之间不能相同。")
            return
        
        cat = conn.execute("SELECT id FROM categories WHERE name = ?", (category,)).fetchone()
        if not cat:
            print(f"错误: 分类 '{category}' 不存在。请先创建分类（分类名可以与标准名相同）。")
            return
        category_id = cat["id"]
        
        now = now_iso()
        conn.execute(
            "INSERT INTO standard_names (name, category_id, description, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (name, category_id, description, now, now)
        )
    print(f"✅ 已创建标准名: {name}")
    print(f"  分类: {category}")
    if description:
        print(f"  说明: {description}")
    print("注意: 如需将原始名映射到此标准名，请使用 add-alias 命令（原始名可以与标准名相同）。")


# ============ 事件记录 ============
def cmd_start(args):
    event_name = args.name.strip()
    if not event_name:
        print("错误: 事件名称不能为空")
        return

    extras = getattr(args, 'extras', None)
    rename = getattr(args, 'rename', None)
    end_time = now_iso()

    with db_transaction() as conn:
        # 记录事件只保存原始名，不检查标准名映射
        # 标准名和分类映射只在统计任务（name-check）中由模型管理
        # 结束上一个事件（在同一事务中，保证原子性）
        current = conn.execute("SELECT name, start_time FROM current WHERE id = 1").fetchone()
        if current:
            duration = (parse_iso(end_time) - parse_iso(current["start_time"])).total_seconds() / 60.0
            event_id = str(uuid.uuid4())[:8]
            
            # 处理事件名称修正
            original_name = current["name"]
            final_name = original_name
            rename_note = ""
            if rename and rename.strip():
                final_name = rename.strip()
                # 修正名也是原始名，统计时再解析为标准名
                rename_note = f"（原始名：{original_name}）"
            
            # 处理额外事情描述
            extras_text = extras.strip() if extras else ""
            if rename_note and extras_text:
                extras_text = f"{rename_note}；{extras_text}"
            elif rename_note:
                extras_text = rename_note
            
            # 分类不存储在 events 表，通过原始名→标准名→分类实时获取
            conn.execute(
                "INSERT INTO events (id, name, start_time, end_time, duration_minutes, extras) VALUES (?, ?, ?, ?, ?, ?)",
                (event_id, final_name,
                 current["start_time"], end_time, round(duration, 1), extras_text)
            )
            final_category = resolve_event_category(conn, final_name)
            print(f"[结束] {final_name} ({final_category})")
            if rename and rename.strip():
                print(f"  名称已修正: {original_name} → {final_name}")
            print(f"  时间: {parse_iso(current['start_time']).strftime('%H:%M')} ~ {parse_iso(end_time).strftime('%H:%M')}")
            print(f"  时长: {format_duration(duration)}")
            if extras_text and not rename_note:
                print(f"  额外事情: {extras_text}")
            elif extras_text and rename_note:
                # 去掉修正备注后显示额外事情
                extra_part = extras_text.replace(rename_note, "").lstrip("；").strip()
                if extra_part:
                    print(f"  额外事情: {extra_part}")
        else:
            print("[提示] 此前无活跃事件，本次为首个事件。")
            if extras:
                print("[提示] --extras 仅对结束的上一个事件生效，首个事件无此前事件可记录。")
            if rename:
                print("[提示] --rename 仅对结束的上一个事件生效，首个事件无此前事件可修正。")

        # 开始新事件（分类不存储，通过映射实时获取）
        conn.execute(
            "INSERT OR REPLACE INTO current (id, name, start_time) VALUES (1, ?, ?)",
            (event_name, end_time)
        )

    # 分类通过映射实时解析，仅作显示参考（无映射时显示"其他"）
    with get_db() as conn:
        category = resolve_event_category(conn, event_name)
    print(f"\n[开始] {event_name} ({category})")
    print(f"  开始时间: {end_time}")


def cmd_stop(args):
    """结束当前事件，并自动开始'未记录'事件（保证任何时刻都有事件在进行中）。"""
    end_time = now_iso()
    with db_transaction() as conn:
        current = conn.execute("SELECT name, start_time FROM current WHERE id = 1").fetchone()
        if not current:
            print("当前没有活跃事件，已自动开始'未记录'事件")
            conn.execute(
                "INSERT OR REPLACE INTO current (id, name, start_time) VALUES (1, '未记录', ?)",
                (end_time,)
            )
            return
        duration = (parse_iso(end_time) - parse_iso(current["start_time"])).total_seconds() / 60.0
        event_id = str(uuid.uuid4())[:8]
        conn.execute(
            "INSERT INTO events (id, name, start_time, end_time, duration_minutes) VALUES (?, ?, ?, ?, ?)",
            (event_id, current["name"],
             current["start_time"], end_time, round(duration, 1))
        )
        # 自动开始"未记录"事件，保证时间连续
        conn.execute(
            "INSERT OR REPLACE INTO current (id, name, start_time) VALUES (1, '未记录', ?)",
            (end_time,)
        )
    print(f"[结束] {current['name']}")
    print(f"  时长: {format_duration(duration)}")
    print(f"  已自动开始'未记录'事件（时间连续不中断）")


def cmd_current(args):
    init_db()
    with get_db() as conn:
        current = conn.execute("SELECT name, start_time FROM current WHERE id = 1").fetchone()
        if not current:
            print("当前没有活跃事件")
            return
        elapsed = (datetime.now(TZ) - parse_iso(current["start_time"])).total_seconds() / 60.0
        # 通过映射实时获取分类
        cur_cat = resolve_event_category(conn, current["name"])
    print(f"[进行中] {current['name']} ({cur_cat})")
    print(f"  开始时间: {current['start_time']}")
    print(f"  已持续: {format_duration(elapsed)}")


def cmd_rename_current(args):
    """修正当前正在进行的事件名称（current 表可修改，events 表只追加）"""
    new_name = args.name.strip()
    if not new_name:
        print("错误: 新事件名称不能为空")
        return
    with db_transaction() as conn:
        current = conn.execute("SELECT name, start_time FROM current WHERE id = 1").fetchone()
        if not current:
            print("当前没有活跃事件，无法修正")
            return
        old_name = current["name"]
        # 修正名称只保存原始名，标准名映射在统计时由模型管理
        conn.execute(
            "UPDATE current SET name = ? WHERE id = 1",
            (new_name,)
        )
    print(f"当前事件已修正: {old_name} → {new_name}")
    print("  （开始时间不变，仅修正名称）")


def cmd_list_events(args):
    """列出最近的事件记录（带ID，用于rename-event）"""
    init_db()
    limit = getattr(args, 'limit', 10)
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, name, start_time, end_time, duration_minutes, extras FROM events ORDER BY start_time DESC LIMIT ?",
            (limit,)
        ).fetchall()
        # 实时解析分类
        cat_map = build_event_category_map(conn, [r["name"] for r in rows])
    print(f"=== 最近 {len(rows)} 条事件记录 ===")
    for r in rows:
        cat = cat_map.get(r["name"], "其他")
        print(f"  ID: {r['id']}")
        print(f"  名称: {r['name']} ({cat})")
        print(f"  时间: {r['start_time'][:16]} ~ {r['end_time'][11:16]} ({r['duration_minutes']}分钟)")
        if r['extras']:
            print(f"  备注: {r['extras']}")
        print()


def cmd_rename_event(args):
    """强制修改已有事件的名称（非常规操作，破坏可追溯性）"""
    event_id = args.event_id.strip()
    new_name = args.new_name.strip()
    if not event_id or not new_name:
        print("错误: 事件ID和新名称不能为空")
        return
    
    print("⚠️  警告：此操作将直接修改 events 表中的已有记录，破坏可追溯性！")
    print("    此功能仅用于修正错误记录，一般不建议使用。")
    print()
    
    with db_transaction() as conn:
        event = conn.execute("SELECT id, name, start_time, end_time FROM events WHERE id = ?", (event_id,)).fetchone()
        if not event:
            print(f"错误: 未找到 ID 为 '{event_id}' 的事件")
            return
        
        old_name = event["name"]
        print(f"目标事件:")
        print(f"  ID: {event['id']}")
        print(f"  原名称: {old_name}")
        print(f"  时间: {event['start_time'][:16]} ~ {event['end_time'][11:16]}")
        print()
        
        # events 表只保存原始名，不解析标准名映射
        # 标准名映射在统计任务（name-check）中由模型管理
        conn.execute(
            "UPDATE events SET name = ? WHERE id = ?",
            (new_name, event_id)
        )
    
    print(f"✅ 事件已强制修改:")
    print(f"  {old_name} → {new_name}")
    print()
    print("注意: 原始名称已被覆盖，无法恢复。建议仅在修正错误时使用。")


# ============ 统计功能 ============
def get_period_range(period, ref_date_str=None):
    if ref_date_str:
        ref = datetime.strptime(ref_date_str, "%Y-%m-%d").replace(tzinfo=TZ)
    else:
        ref = datetime.now(TZ)

    if period == "day":
        start = ref.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
    elif period == "week":
        start = ref.replace(hour=0, minute=0, second=0, microsecond=0)
        start = start - timedelta(days=start.weekday())
        end = start + timedelta(weeks=1)
    elif period == "month":
        start = ref.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if start.month == 12:
            end = start.replace(year=start.year + 1, month=1)
        else:
            end = start.replace(month=start.month + 1)
    elif period == "quarter":
        q = (ref.month - 1) // 3
        start_month = q * 3 + 1
        start = ref.replace(month=start_month, day=1, hour=0, minute=0, second=0, microsecond=0)
        if start_month == 10:
            end = start.replace(year=start.year + 1, month=1)
        else:
            end = start.replace(month=start_month + 3)
    elif period == "year":
        start = ref.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        end = start.replace(year=start.year + 1)
    else:
        raise ValueError(f"未知周期: {period}")
    return start, end


def split_event_by_days(event):
    start = parse_iso(event["start_time"])
    end = parse_iso(event["end_time"])
    fragments = []
    cur = start
    while cur < end:
        day_start = cur.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)
        seg_end = min(end, day_end)
        minutes = (seg_end - cur).total_seconds() / 60.0
        fragments.append((day_start.strftime("%Y-%m-%d"), minutes))
        cur = day_end
    return fragments


def query_events_in_range(conn, start, end):
    """查询与 [start, end) 有交集的事件，利用索引加速。"""
    start_iso = start.isoformat()
    end_iso = end.isoformat()
    rows = conn.execute(
        "SELECT name, start_time, end_time, duration_minutes, extras "
        "FROM events WHERE start_time < ? AND end_time > ?",
        (end_iso, start_iso)
    ).fetchall()
    result = []
    for r in rows:
        ev_start = parse_iso(r["start_time"])
        ev_end = parse_iso(r["end_time"])
        overlap_start = max(ev_start, start)
        overlap_end = min(ev_end, end)
        if overlap_start < overlap_end:
            minutes = (overlap_end - overlap_start).total_seconds() / 60.0
            result.append({
                "name": r["name"],
                "start_time": overlap_start.isoformat(),
                "end_time": overlap_end.isoformat(),
                "duration_minutes": round(minutes, 1),
                "extras": r["extras"] or ""
            })
    return result


def period_label(period):
    return {"day": "日", "week": "周", "month": "月", "quarter": "季度", "year": "年度"}.get(period, period)


def get_stats_data(period, ref_date=None):
    """
    计算统计数据，返回结构化字典。供文本输出和 JSON 输出共用。
    包含未记录补全、原始名映射、按分类/事件/每日聚合。
    """
    init_db()
    start, end = get_period_range(period, ref_date)

    with get_db() as conn:
        aliases = load_aliases(conn)
        cats = load_categories(conn)
        std_names = load_standard_names(conn)
        events = query_events_in_range(conn, start, end)

        # 包含当前活跃事件
        current = conn.execute("SELECT name, start_time FROM current WHERE id = 1").fetchone()
        if current:
            now = datetime.now(TZ)
            cur_start = parse_iso(current["start_time"])
            overlap_start = max(cur_start, start)
            overlap_end = min(now, end)
            if overlap_start < overlap_end:
                minutes = (overlap_end - overlap_start).total_seconds() / 60.0
                # 分类通过映射实时获取，不存储在 current 表中
                events.append({
                    "name": current["name"],
                    "start_time": overlap_start.isoformat(),
                    "end_time": overlap_end.isoformat(),
                    "duration_minutes": round(minutes, 1)
                })

    empty_result = {
        "period": period,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "total_minutes": 0,
        "actual_minutes": 0,
        "unrecorded_minutes": 0,
        "event_count": 0,
        "by_category": [],
        "by_event": [],
        "daily": [],
        "has_data": False
    }

    if not events:
        return empty_result

    # 计算未记录时长，用虚拟事件补满周期
    actual_minutes = sum(e["duration_minutes"] for e in events)
    period_days = (end - start).days
    period_total_minutes = period_days * 24 * 60
    unrecorded_minutes = max(0.0, period_total_minutes - actual_minutes)

    # 构建统计用事件列表（含未记录虚拟事件）
    stats_events = list(events)
    if unrecorded_minutes > 0.1:
        stats_events.append({
            "name": "未记录",
            "category": "未记录",
            "start_time": start.isoformat(),
            "end_time": end.isoformat(),
            "duration_minutes": round(unrecorded_minutes, 1)
        })

    total_minutes = sum(e["duration_minutes"] for e in stats_events)

    # 按分类聚合（支持多级分类层级聚合：父分类时长 = 自身 + 所有后代）
    cat_name_to_id = {c["name"]: c["id"] for c in cats}
    cat_id_to_name = {c["id"]: c["name"] for c in cats}
    
    # 标准名→分类映射（已在 with 块内加载）
    std_to_category = {s["name"]: s["category_name"] for s in std_names}
    
    # 先按直接分类聚合（通过标准名解析分类，而不是用 events.category 字段）
    direct_cat_minutes = defaultdict(float)
    for e in stats_events:
        # 先用 aliases 解析为标准名
        display_name = aliases.get(e["name"], e["name"])
        # 再用 standard_names 解析分类（不依赖 events.category 字段）
        category = std_to_category.get(display_name, "其他")
        direct_cat_minutes[category] += e["duration_minutes"]
    
    # 层级聚合：每个分类的总时长 = 自身直接时长 + 所有后代时长
    def get_category_total_minutes(cat_id):
        total = direct_cat_minutes.get(cat_id_to_name.get(cat_id, ""), 0)
        for child in get_category_children(cats, cat_id):
            total += get_category_total_minutes(child["id"])
        return total
    
    by_category = []
    # 只显示顶级分类（子分类时长已包含在父分类中）
    top_cats = [c for c in cats if c["parent_id"] is None]
    # 加上有直接事件但没有父分类的分类（防止数据不一致）
    for cat_name in direct_cat_minutes:
        if cat_name not in cat_name_to_id:
            top_cats.append({"id": None, "name": cat_name, "parent_id": None})
    
    for cat in top_cats:
        if cat["id"] is not None:
            total_mins = get_category_total_minutes(cat["id"])
        else:
            total_mins = direct_cat_minutes.get(cat["name"], 0)
        if total_mins > 0:
            pct = total_mins / total_minutes * 100 if total_minutes > 0 else 0
            by_category.append({
                "name": cat["name"],
                "minutes": round(total_mins, 1),
                "percentage": round(pct, 1)
            })
    
    by_category.sort(key=lambda x: -x["minutes"])

    # 按事件聚合（应用原始名映射）
    name_minutes = defaultdict(float)
    name_count = defaultdict(int)
    for e in stats_events:
        display_name = aliases.get(e["name"], e["name"])
        name_minutes[display_name] += e["duration_minutes"]
        name_count[display_name] += 1

    by_event = []
    for name, mins in sorted(name_minutes.items(), key=lambda x: -x[1]):
        pct = mins / total_minutes * 100 if total_minutes > 0 else 0
        cnt = name_count[name]
        avg = mins / cnt if cnt > 0 else 0
        by_event.append({
            "name": name,
            "minutes": round(mins, 1),
            "count": cnt,
            "average_minutes": round(avg, 1),
            "percentage": round(pct, 1)
        })

    # 每日实际记录时长聚合（不含未记录虚拟事件）
    daily = []
    if period in ("week", "month", "quarter", "year"):
        day_minutes = defaultdict(float)
        day_cat_minutes = defaultdict(lambda: defaultdict(float))
        for e in events:
            # 通过原始名→标准名→分类实时获取分类
            display_name = aliases.get(e["name"], e["name"])
            e_category = std_to_category.get(display_name, "其他")
            for date_str, mins in split_event_by_days(e):
                if start.strftime("%Y-%m-%d") <= date_str < end.strftime("%Y-%m-%d"):
                    day_minutes[date_str] += mins
                    day_cat_minutes[date_str][e_category] += mins

        for d in sorted(day_minutes.keys()):
            daily.append({
                "date": d,
                "minutes": round(day_minutes[d], 1),
                "by_category": {cat: round(mins, 1) for cat, mins in day_cat_minutes[d].items()}
            })

    return {
        "period": period,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "total_minutes": round(total_minutes, 1),
        "actual_minutes": round(actual_minutes, 1),
        "unrecorded_minutes": round(unrecorded_minutes, 1),
        "event_count": len(events),
        "by_category": by_category,
        "by_event": by_event,
        "daily": daily,
        "has_data": True,
        "_aliases": aliases,  # 内部用，JSON输出时会移除
        "_events": events     # 内部用，JSON输出时会移除
    }


def cmd_stats(args):
    period = args.period
    ref_date = args.date
    data = get_stats_data(period, ref_date)

    if not data["has_data"]:
        print(f"=== {period_label(period)}统计 ===")
        print(f"时间范围: {data['start'][:16].replace('T', ' ')} ~ {data['end'][:16].replace('T', ' ')}")
        print("(无记录)")
        return

    aliases = data["_aliases"]
    events = data["_events"]

    print(f"=== {period_label(period)}统计 ===")
    print(f"时间范围: {data['start'][:16].replace('T', ' ')} ~ {data['end'][:16].replace('T', ' ')}")
    print(f"记录事件数: {data['event_count']} (含未记录补全)")
    print(f"总记录时长: {format_duration(data['total_minutes'])} (实际记录 {format_duration(data['actual_minutes'])}，未记录 {format_duration(data['unrecorded_minutes'])})")
    print()

    print("--- 按分类 ---")
    for cat in data["by_category"]:
        print(f"  {cat['name']:<10} {format_duration(cat['minutes']):<16} {cat['percentage']:5.1f}%")
    print()

    print("--- 按事件 (Top 20) ---")
    for ev in data["by_event"][:20]:
        print(f"  {ev['name']:<20} {format_duration(ev['minutes']):<16} {ev['percentage']:5.1f}%  ({ev['count']}次, 均{format_duration(ev['average_minutes'])})")
    print()

    if period in ("week", "month", "quarter", "year"):
        print("--- 每日实际记录时长 ---")
        for d in data["daily"]:
            bar = "█" * int(d["minutes"] / 60) + ("░" if d["minutes"] % 60 >= 30 else "")
            print(f"  {d['date']}  {format_duration(d['minutes']):<16} / 24h  {bar}")
        print()

    # 显示有额外事情记录的事件
    extras_events = [e for e in events if e.get("extras")]
    if extras_events:
        print("--- 额外事情记录 ---")
        for e in extras_events:
            print(f"  [{e['start_time'][11:16]}] {e['name']}: {e['extras']}")
        print()

    period_names = list(set(e["name"] for e in events))
    if period_names:
        print("--- 名称与分类管理提示 ---")
        period_cn = {"day": "日", "week": "周", "month": "月", "quarter": "季度", "year": "年"}.get(period, period)
        print(f"  本{period_cn}共 {len(period_names)} 种事件名称。")
        print(f"  名称与分类管理分析: python3 time_tracker.py name-check --period {period}")
        print("  （由模型从语义角度分析哪些名称描述的是同一事件）")
        print()


def cmd_stats_json(args):
    """
    输出结构化 JSON 格式统计数据，供可视化技能、定时任务报告、数据分析等工具消费。
    数据与 stats 命令完全一致，仅输出格式不同。
    """
    period = args.period
    ref_date = args.date
    data = get_stats_data(period, ref_date)

    # 移除内部用字段
    data.pop("_aliases", None)
    data.pop("_events", None)

    # 输出到 stdout
    print(json.dumps(data, ensure_ascii=False, indent=2))


# ============ 查询功能 ============
def cmd_query(args):
    init_db()
    event_name = args.name.strip()
    with get_db() as conn:
        aliases = load_aliases(conn)
        all_rows = conn.execute(
            "SELECT name, start_time, end_time, duration_minutes FROM events ORDER BY start_time"
        ).fetchall()

    matched = []
    for r in all_rows:
        display_name = aliases.get(r["name"], r["name"])
        if event_name in r["name"] or event_name in display_name:
            matched.append(r)

    if not matched:
        print(f"未找到包含 '{event_name}' 的事件记录")
        return

    durations = [r["duration_minutes"] for r in matched]
    total = sum(durations)
    count = len(durations)
    avg = total / count
    minimum = min(durations)
    maximum = max(durations)
    sorted_d = sorted(durations)
    median = sorted_d[count // 2] if count % 2 == 1 else (sorted_d[count // 2 - 1] + sorted_d[count // 2]) / 2

    print(f"=== 查询: '{event_name}' ===")
    print(f"匹配事件数: {count}")
    print(f"总时长: {format_duration(total)}")
    print(f"平均时长: {format_duration(avg)}")
    print(f"中位数时长: {format_duration(median)}")
    print(f"最短: {format_duration(minimum)}")
    print(f"最长: {format_duration(maximum)}")
    print()

    # 通过原始名→标准名→分类实时获取分类
    cat_count = defaultdict(int)
    with get_db() as conn:
        for r in matched:
            cat = resolve_event_category(conn, r["name"])
            cat_count[cat] += 1
    print("分类分布:")
    for cat, cnt in sorted(cat_count.items(), key=lambda x: -x[1]):
        print(f"  {cat}: {cnt} 次")

    print()
    print("最近 5 次记录:")
    with get_db() as conn:
        recent_cat_map = build_event_category_map(conn, [r["name"] for r in matched[-5:]])
    for r in matched[-5:]:
        cat = recent_cat_map.get(r["name"], "其他")
        print(f"  {r['start_time'][:16]} ~ {r['end_time'][:16]}  {format_duration(r['duration_minutes'])}  [{cat}]")


# ============ 事件名称统计（供语义分析） ============
def cmd_name_stats(args):
    """输出所有事件名称的完整统计，专门供模型做语义分析和分类管理。"""
    init_db()
    with get_db() as conn:
        aliases = load_aliases(conn)

        # 查询每个事件名称的统计
        rows = conn.execute(
            "SELECT name, COUNT(*) as cnt, SUM(duration_minutes) as total, "
            "MIN(start_time) as first_seen, MAX(end_time) as last_seen "
            "FROM events GROUP BY name ORDER BY total DESC"
        ).fetchall()

        # 查询每个名称的分类分布
        # 通过原始名→标准名→分类实时获取分类（不依赖 events.category 字段）
        all_names = [r["name"] for r in rows]
        name_cat_map = build_event_category_map(conn, all_names)

    if not rows:
        print("暂无事件记录")
        return

    print("=" * 70)
    print("  事件名称统计（供语义分析与分类管理）")
    print("=" * 70)
    print(f"共 {len(rows)} 种事件名称")
    print()

    # 输出表头
    print(f"{'#':<4} {'名称':<25} {'次数':<6} {'总时长':<14} {'平均':<12} {'主要分类'}")
    print("-" * 70)

    for i, r in enumerate(rows, 1):
        name = r["name"]
        cnt = r["cnt"]
        total = r["total"] or 0
        avg = total / cnt if cnt > 0 else 0
        # 主要分类（通过映射实时获取）
        main_cat = name_cat_map.get(name, "其他")
        # 标记是否已有原始名映射
        alias_mark = " ←原始名" if name in aliases else ""
        standard_mark = f" (标准名:{aliases[name]})" if name in aliases else ""

        print(f"{i:<4} {name:<25} {cnt:<6} {format_duration(total):<14} {format_duration(avg):<12} {main_cat}{alias_mark}{standard_mark}")

    print()
    print("-" * 70)
    print()
    print("【语义分析指引】")
    print("  1. 同义词归并：如'编码'、'写代码'、'敲代码'应归为同一标准名")
    print("  2. 子任务归并：如'写代码-登录模块'应归入'写代码'")
    print("  3. 分类推荐：主要分类为'其他'且次数多/时长长的事件，建议创建或归入分类")
    print("  4. 标有'←原始名'的名称已有映射，无需重复处理")
    print()
    print("【执行命令】")
    print("  添加原始名映射: python3 time_tracker.py add-alias '<变体名>' '<标准名>'")
    print("  添加分类: python3 time_tracker.py add-category '<分类名>' --parent '<父分类>'")
    print("  查看原始名映射: python3 time_tracker.py aliases")
    print("  查看分类: python3 time_tracker.py categories")


# ============ 季度分类评审 ============
def cmd_quarterly_review(args):
    init_db()
    start, end = get_period_range("quarter")

    with get_db() as conn:
        events = query_events_in_range(conn, start, end)
        if not events:
            print("本季度暂无记录，无法生成评审。")
            return

        cats = load_categories(conn)
        cat_names = [c["name"] for c in cats]
        aliases = load_aliases(conn)
        name_library = build_name_library(conn)

    total_minutes = sum(e["duration_minutes"] for e in events)

    # 通过原始名→标准名→分类实时获取分类
    cat_minutes = defaultdict(float)
    cat_event_names = defaultdict(lambda: defaultdict(float))
    with get_db() as conn:
        event_cat_map = build_event_category_map(conn, [e["name"] for e in events])
    for e in events:
        cat = event_cat_map.get(e["name"], "其他")
        cat_minutes[cat] += e["duration_minutes"]
        cat_event_names[cat][e["name"]] += e["duration_minutes"]

    print(f"{'='*50}")
    print(f"  季度分类评审 ({start.strftime('%Y-%m-%d')} ~ {end.strftime('%Y-%m-%d')})")
    print(f"{'='*50}")
    print(f"总记录时长: {format_duration(total_minutes)}")
    print(f"事件种类数: {len(set(e['name'] for e in events))}")
    print(f"分类数量: {len(cat_names)}")
    print()

    # 第一步：命名一致性检查
    print("━" * 50)
    print("  第一步：事件命名一致性检查")
    print("━" * 50)
    print()

    quarter_names = list(set(e["name"] for e in events))
    print(f"本季度共 {len(quarter_names)} 种事件名称。")
    print()
    print("命名一致性检查由模型完成：")
    print("  1. 运行 name-check 获取名称数据和分析指引")
    print("  2. 模型从语义角度分析哪些名称描述的是同一事件")
    print("  3. 输出结构化 JSON 方案，用户确认后用 add-alias 执行")
    print()
    print("  相关命令:")
    print("    名称检查: python3 time_tracker.py name-check")
    print("    名称统计: python3 time_tracker.py name-stats")
    print("    添加原始名映射: python3 time_tracker.py add-alias '<变体>' '<标准名>'")
    print()

    # 第二步：分类调整候选方案
    print("━" * 50)
    print("  第二步：分类调整候选方案")
    print("━" * 50)
    print()
    print(f"当前阈值（可通过参数调整）:")
    print(f"  新建分类: 占总时长 ≥ {args.new_pct}% 或 绝对时长 ≥ {args.new_min}分钟")
    print(f"  合并分类: 占总时长 < {args.merge_pct}% 且 绝对时长 < {args.merge_min}分钟")
    print(f"  拆分分类: Top1事件占分类内 > {args.split_pct}% 且 分类内 ≥ {args.split_count}种事件")
    print()

    proposals = []
    proposal_id = 0

    # 方案A：新建分类
    other_events = cat_event_names.get("其他", {})
    if other_events:
        print("--- A. '其他'分类中可独立的事件 ---")
        has_any = False
        for name, mins in sorted(other_events.items(), key=lambda x: -x[1]):
            pct_of_total = mins / total_minutes * 100 if total_minutes > 0 else 0
            if pct_of_total >= args.new_pct or mins >= args.new_min:
                has_any = True
                proposal_id += 1
                if pct_of_total >= 5 and mins >= 300:
                    level = "强烈推荐"
                elif pct_of_total >= 3 or mins >= 180:
                    level = "建议考虑"
                else:
                    level = "可选"
                proposals.append({
                    "id": proposal_id, "type": "新建分类", "level": level,
                    "detail": f"建议为 '{name}' 创建独立分类（本季度 {format_duration(mins)}，占总时长 {pct_of_total:.1f}%）"
                })
                print(f"  [{proposal_id}] [{level}] {name}")
                print(f"        时长: {format_duration(mins)} ({pct_of_total:.1f}% of 总)")
                print(f"        建议: 创建新分类，并将标准名 '{name}' 归入该分类")
                print()
        if not has_any:
            print("  (无达到阈值的事件)")
        print()

    # 方案B：合并分类
    print("--- B. 低使用率分类（建议合并） ---")
    has_any = False
    for cat in cat_names:
        if cat == "其他":
            continue
        mins = cat_minutes.get(cat, 0)
        pct = mins / total_minutes * 100 if total_minutes > 0 else 0
        if pct < args.merge_pct and mins < args.merge_min:
            has_any = True
            proposal_id += 1
            if pct < 0.5:
                level = "强烈推荐"
            elif pct < 1:
                level = "建议考虑"
            else:
                level = "可选"
            proposals.append({
                "id": proposal_id, "type": "合并分类", "level": level,
                "detail": f"分类 '{cat}' 本季度仅 {format_duration(mins)} ({pct:.1f}%)，建议合并到'其他'"
            })
            print(f"  [{proposal_id}] [{level}] {cat}")
            print(f"        时长: {format_duration(mins)} ({pct:.1f}% of 总)")
            print(f"        建议: 合并到'其他'或删除该分类")
            print()
    if not has_any:
        print("  (所有分类使用率均达标)")
    print()

    # 方案C：拆分分类
    print("--- C. 过度集中的分类（建议拆分） ---")
    has_any = False
    for cat, names in cat_event_names.items():
        if cat == "其他" or len(names) < args.split_count:
            continue
        cat_total = sum(names.values())
        top_name, top_mins = max(names.items(), key=lambda x: x[1])
        top_pct = top_mins / cat_total * 100 if cat_total > 0 else 0
        if top_pct > args.split_pct:
            has_any = True
            proposal_id += 1
            if top_pct > 75:
                level = "强烈推荐"
            elif top_pct > 60:
                level = "建议考虑"
            else:
                level = "可选"
            proposals.append({
                "id": proposal_id, "type": "拆分分类", "level": level,
                "detail": f"分类 '{cat}' 中 '{top_name}' 占 {top_pct:.1f}%，建议拆分为独立分类"
            })
            print(f"  [{proposal_id}] [{level}] {cat}")
            print(f"        分类内事件种类: {len(names)}")
            print(f"        主导事件: '{top_name}' 占 {top_pct:.1f}%")
            print(f"        建议: 将 '{top_name}' 拆分为独立分类")
            print()
    if not has_any:
        print("  (无过度集中的分类)")
    print()

    # 汇总
    print("━" * 50)
    print("  候选方案汇总")
    print("━" * 50)
    print()
    if proposals:
        for p in proposals:
            print(f"  [{p['id']}] [{p['level']}] {p['type']}: {p['detail']}")
        print()
        print("请审查以上方案，决定保留/弃用/调整。")
        print()
        print("执行命令参考:")
        print("  新建分类: python3 time_tracker.py add-category '<分类名>' --parent '<父分类>' --description '<说明>'")
        print("  删除分类: python3 time_tracker.py remove-category '<分类名>'")
        print("  添加原始名映射: python3 time_tracker.py add-alias '<原始名>' '<标准名>'")
        print()
        print("提示: 你可以修改方案中的分类名，或组合多个方案。")
        print("      调整阈值重新生成: python3 time_tracker.py quarterly-review --new-pct 2 --new-min 90")
    else:
        print("当前分类结构合理，暂无调整候选方案。")
        print("如需更敏感的检测，可降低阈值: --new-pct 1 --merge-pct 3 --split-pct 40")
    print()
    print("━" * 50)
    print("  语义分析（可选）")
    print("━" * 50)
    print()
    print("以上为基于规则的分类建议。如需更深层的语义分析（如识别'编码'与'写代码'")
    print("是同一类事件），可运行以下命令获取完整事件名称统计，由模型做语义聚类分析：")
    print()
    print("  python3 time_tracker.py name-stats")
    print()
    print("模型分析后可批量执行 add-alias / add-category 完成映射。")


# ============ 数据导出 ============
def cmd_export(args):
    init_db()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, name, start_time, end_time, duration_minutes FROM events ORDER BY start_time"
        ).fetchall()
    events = [dict(r) for r in rows]
    print(json.dumps(events, ensure_ascii=False, indent=2))
    print(f"\n共 {len(events)} 条记录，已输出到 stdout", file=sys.stderr)


def cmd_export_stats(args):
    """
    将临时统计结果导出为文件供下载。
    支持 JSON（完整结构化数据）和 CSV（多 section 表格）格式。
    文件保存到数据目录下的 exports/ 子目录。
    """
    period = args.period
    ref_date = args.date
    fmt = args.format
    output_name = args.output

    data = get_stats_data(period, ref_date)
    # 移除内部字段
    data.pop("_aliases", None)
    data.pop("_events", None)

    # 确保导出目录存在
    export_dir = DATA_DIR / "exports"
    export_dir.mkdir(parents=True, exist_ok=True)

    # 自动生成文件名
    if not output_name:
        date_part = ref_date or datetime.now(TZ).strftime("%Y-%m-%d")
        if period == "day":
            label = date_part
        elif period == "week":
            label = date_part
        elif period == "month":
            label = date_part[:7]
        elif period == "quarter":
            label = f"Q{((int(date_part[5:7]) - 1) // 3) + 1}_{date_part[:4]}"
        else:  # year
            label = date_part[:4]
        output_name = f"stats_{period}_{label}.{fmt}"

    output_path = export_dir / output_name

    if fmt == "json":
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    elif fmt == "csv":
        with open(output_path, "w", encoding="utf-8-sig") as f:
            # Section 1: 概览
            f.write("=== 概览 ===\n")
            f.write("周期,开始时间,结束时间,总时长(分钟),实际记录(分钟),未记录(分钟),事件数\n")
            f.write(f"{data['period']},{data['start']},{data['end']},{data['total_minutes']},{data['actual_minutes']},{data['unrecorded_minutes']},{data['event_count']}\n")
            f.write("\n")

            # Section 2: 按分类
            f.write("=== 按分类 ===\n")
            f.write("分类,时长(分钟),占比(%)\n")
            for cat in data["by_category"]:
                f.write(f"{cat['name']},{cat['minutes']},{cat['percentage']}\n")
            f.write("\n")

            # Section 3: 按事件
            f.write("=== 按事件 ===\n")
            f.write("事件名称,时长(分钟),次数,平均时长(分钟),占比(%)\n")
            for ev in data["by_event"]:
                f.write(f"{ev['name']},{ev['minutes']},{ev['count']},{ev['average_minutes']},{ev['percentage']}\n")
            f.write("\n")

            # Section 4: 每日（仅周/月/季/年有数据）
            if data["daily"]:
                f.write("=== 每日实际记录时长 ===\n")
                f.write("日期,时长(分钟),各分类分布\n")
                for d in data["daily"]:
                    cat_str = "; ".join(f"{k}:{v}" for k, v in d["by_category"].items())
                    f.write(f"{d['date']},{d['minutes']},{cat_str}\n")
                f.write("\n")

    print(f"统计报告已导出: {output_path}")
    print(f"格式: {fmt.upper()}, 周期: {period_label(period)}")
    if not data["has_data"]:
        print("(注意: 该周期无记录数据)")



def cmd_init_test_db(args):
    """初始化测试数据库：从生产数据库复制一份用于测试。
    
    测试数据库路径: DATA_DIR/test-time-tracker.db
    测试模式下使用 --test 参数，所有操作指向测试数据库，不影响生产数据。
    """
    if not os.path.exists(DB_PATH):
        print("错误: 生产数据库不存在，无法复制")
        print(f"  生产数据库路径: {DB_PATH}")
        return
    
    import shutil
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    
    # 如果测试数据库已存在，先删除
    if os.path.exists(TEST_DB_PATH):
        os.remove(TEST_DB_PATH)
        print(f"  已删除旧的测试数据库")
    
    # 复制生产数据库到测试数据库
    shutil.copy2(str(DB_PATH), str(TEST_DB_PATH))
    
    # 验证复制结果
    if not os.path.exists(TEST_DB_PATH):
        print("错误: 测试数据库复制失败")
        return
    
    # 验证测试数据库完整性
    try:
        conn = sqlite3.connect(str(TEST_DB_PATH))
        count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        conn.close()
    except Exception as e:
        print(f"错误: 测试数据库验证失败: {e}")
        return
    
    print("=" * 50)
    print("✅ 测试数据库初始化完成")
    print(f"  生产数据库: {DB_PATH}")
    print(f"  测试数据库: {TEST_DB_PATH}")
    print(f"  事件记录数: {count} 条")
    print()
    print("【使用方法】")
    print("  在命令前添加 --test 参数即可使用测试数据库：")
    print(f"  python3 {os.path.basename(__file__)} --test current")
    print(f"  python3 {os.path.basename(__file__)} --test start \"测试事件\"")
    print()
    print("【注意事项】")
    print("  1. 测试模式下不会从飞书拉取数据，也不会备份到飞书")
    print("  2. 测试数据库修改不影响生产数据库")
    print("  3. 如需重新初始化测试数据库，再次运行本命令即可")
    print("=" * 50)


# ============ 主入口 ============
def main():
    global DB_PATH, TEST_MODE
    
    parser = argparse.ArgumentParser(description="柳比歇夫时间统计法追踪工具（SQLite版）")
    parser.add_argument("--test", action="store_true", help="使用测试数据库（不影响生产数据，不备份到飞书）")
    subparsers = parser.add_subparsers(dest="command", help="可用命令")

    p_start = subparsers.add_parser("start", help="开始新事件（自动结束上一个）")
    p_start.add_argument("name", help="事件名称")
    p_start.add_argument("--extras", help="上一个事件期间额外做的事情描述（可选）")
    p_start.add_argument("--rename", help="修正上一个事件的名称（可选）")

    subparsers.add_parser("stop", help="结束当前事件")
    subparsers.add_parser("current", help="查看当前进行中的事件")
    p_rename = subparsers.add_parser("rename-current", help="修正当前进行中的事件名称")
    p_rename.add_argument("name", help="新的事件名称")
    p_list = subparsers.add_parser("list-events", help="列出最近的事件记录（带ID）")
    p_list.add_argument("--limit", type=int, default=10, help="显示条数，默认10")
    p_rename_ev = subparsers.add_parser("rename-event", help="强制修改已有事件的名称（非常规操作）")
    p_rename_ev.add_argument("event_id", help="事件ID（通过 list-events 查看）")
    p_rename_ev.add_argument("new_name", help="新的事件名称")

    p_stats = subparsers.add_parser("stats", help="统计分析（文本输出）")
    p_stats.add_argument("period", choices=["day", "week", "month", "quarter", "year"], help="统计周期")
    p_stats.add_argument("date", nargs="?", default=None, help="参考日期 YYYY-MM-DD（默认今天）")

    p_stats_json = subparsers.add_parser("stats-json", help="统计分析（结构化JSON输出，供可视化/报告/数据分析使用）")
    p_stats_json.add_argument("period", choices=["day", "week", "month", "quarter", "year"], help="统计周期")
    p_stats_json.add_argument("date", nargs="?", default=None, help="参考日期 YYYY-MM-DD（默认今天）")

    p_query = subparsers.add_parser("query", help="查询某事件的历史时长统计")
    p_query.add_argument("name", help="事件名称（支持模糊匹配）")

    subparsers.add_parser("categories", help="列出所有分类")
    p_addcat = subparsers.add_parser("add-category", help="添加新分类")
    p_addcat.add_argument("name", help="分类名称")
    p_addcat.add_argument("--parent", help="父分类名称（可选，用于多级分类）")
    # --keywords 已废弃，分类通过 standard_names.category_id 关联，不再使用关键词匹配
    p_addcat.add_argument("--description", help="分类说明")
    p_rmcat = subparsers.add_parser("remove-category", help="删除分类")
    p_rmcat.add_argument("name", help="分类名称")

    subparsers.add_parser("standard-names", help="列出所有标准名及其分类")
    p_addstd = subparsers.add_parser("add-standard-name", help="创建新的标准名")
    p_addstd.add_argument("name", help="标准名名称")
    p_addstd.add_argument("--category", help="所属分类名（可选，分类必须已存在）")
    p_addstd.add_argument("--description", help="标准名说明（可选）")
    p_setstd = subparsers.add_parser("set-standard-category", help="设置标准名的分类")
    p_setstd.add_argument("standard_name", help="标准名")
    p_setstd.add_argument("category", help="分类名称")

    p_rmstd = subparsers.add_parser("remove-standard-name", help="删除标准名（仅当无原始名映射时）")
    p_rmstd.add_argument("name", help="要删除的标准名名称")

    subparsers.add_parser("aliases", help="列出所有事件原始名映射")
    p_addalias = subparsers.add_parser("add-alias", help="添加事件原始名映射（统计时自动映射）")
    p_addalias.add_argument("alias", help="原始名（用户输入的变体名称）")
    p_addalias.add_argument("standard", help="标准名（映射后的名称）")
    p_rmalias = subparsers.add_parser("remove-alias", help="删除事件原始名映射")
    p_rmalias.add_argument("alias", help="要删除的原始名")

    p_namecheck = subparsers.add_parser("name-check", help="检查事件名称一致性，建议归一化")
    p_namecheck.add_argument("date", nargs="?", default=None, help="检查日期 YYYY-MM-DD（默认今天）")
    p_namecheck.add_argument("--period", choices=["day", "week", "month", "quarter", "year"], default=None, help="检查周期（day/week/month/quarter/year），与 date 二选一")

    subparsers.add_parser("name-stats", help="输出所有事件名称统计（供语义分析与分类管理）")

    p_review = subparsers.add_parser("quarterly-review", help="季度分类调整评审（命名检查 + 候选方案）")
    p_review.add_argument("--new-pct", type=float, default=1.5, help="新建分类: 占总时长阈值%% (默认1.5)")
    p_review.add_argument("--new-min", type=float, default=60, help="新建分类: 绝对最小时长分钟 (默认60)")
    p_review.add_argument("--merge-pct", type=float, default=2.0, help="合并分类: 占总时长阈值%% (默认2.0)")
    p_review.add_argument("--merge-min", type=float, default=120, help="合并分类: 绝对最大时长分钟 (默认120)")
    p_review.add_argument("--split-pct", type=float, default=45, help="拆分分类: Top1占分类内阈值%% (默认45)")
    p_review.add_argument("--split-count", type=int, default=4, help="拆分分类: 分类内最小事件种类数 (默认4)")

    subparsers.add_parser("export", help="导出原始事件数据为 JSON")

    p_export_stats = subparsers.add_parser("export-stats", help="导出统计结果为文件供下载（JSON/CSV）")
    p_export_stats.add_argument("period", choices=["day", "week", "month", "quarter", "year"], help="统计周期")
    p_export_stats.add_argument("date", nargs="?", default=None, help="参考日期 YYYY-MM-DD（默认今天）")
    p_export_stats.add_argument("--format", choices=["json", "csv"], default="json", help="导出格式（默认 json）")
    p_export_stats.add_argument("--output", help="输出文件名（默认自动生成）")

    subparsers.add_parser("init-test-db", help="初始化测试数据库（从生产数据库复制一份用于测试）")

    args = parser.parse_args()
    
    # 处理测试模式：切换到测试数据库路径
    if args.test:
        TEST_MODE = True
        DB_PATH = TEST_DB_PATH
        print("[测试模式] 已切换到测试数据库，不会影响生产数据，不会备份到飞书")
        print()
    if not args.command:
        parser.print_help()
        return

    commands = {
        "start": cmd_start, "stop": cmd_stop, "current": cmd_current, "rename-current": cmd_rename_current,
        "list-events": cmd_list_events, "rename-event": cmd_rename_event,
        "stats": cmd_stats, "stats-json": cmd_stats_json, "query": cmd_query,
        "categories": cmd_categories_list, "add-category": cmd_category_add, "remove-category": cmd_category_remove,
        "standard-names": cmd_standard_names_list, "add-standard-name": cmd_add_standard_name, "set-standard-category": cmd_set_standard_category, "remove-standard-name": cmd_remove_standard_name,
        "aliases": cmd_aliases_list, "add-alias": cmd_alias_add, "remove-alias": cmd_alias_remove,
        "name-check": cmd_name_check, "name-stats": cmd_name_stats, "quarterly-review": cmd_quarterly_review, "export": cmd_export,
        "export-stats": cmd_export_stats, "init-test-db": cmd_init_test_db,
    }
    try:
        commands[args.command](args)
    except sqlite3.DatabaseError as e:
        print()
        print("=" * 50)
        print(f"❌ 数据库错误: {e}")
        print("=" * 50)
        print("建议:")
        print("  1. 运行 backup_to_lark.py --restore 从飞书恢复数据库")
        print("  2. 如果恢复失败，检查飞书云空间中的备份文件")
        sys.exit(1)
    except Exception as e:
        print()
        print("=" * 50)
        print(f"❌ 未预期的错误: {type(e).__name__}: {e}")
        print("=" * 50)
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        # 非测试模式下，操作完成后删除本地数据库
        # 飞书是唯一权威数据源，本地不保留数据库文件
        # 但如果备份失败，保留本地数据库以防数据丢失
        if not TEST_MODE and args.command != "init-test-db":
            if LAST_BACKUP_SUCCESS:
                cleanup_local_db()
            else:
                print()
                print("=" * 60)
                print("[数据安全] 由于备份失败，本地数据库已保留")
                print(f"  数据库路径: {DB_PATH}")
                print("  请解决备份问题后手动运行 backup_to_lark.py 进行备份")
                print("=" * 60)


if __name__ == "__main__":
    main()
