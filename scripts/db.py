#!/usr/bin/env python3
"""
数据库层：连接管理、事务、飞书同步、备份、表结构初始化。
"""

import glob
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime

# 文件锁（防止并发写入）
try:
    import fcntl
    _HAS_FCNTL = True
except ImportError:
    _HAS_FCNTL = False

import config
from models import now_iso, parse_iso

# ============ 数据库管理 ============
def ensure_data_dir():
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)


def check_db_integrity(db_path=None):
    """检查数据库文件完整性，返回 (is_valid, error_message)。"""
    path = db_path or config.DB_PATH
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
    """获取数据库文件锁，防止并发写入。失败时直接报错。"""
    if not _HAS_FCNTL:
        return None
    lock_path = str(config.DB_PATH) + ".lock"
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
        raise RuntimeError("无法获取数据库锁，可能有其他进程正在写入。请稍后重试。")


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
    if os.path.exists(config.DB_PATH):
        valid, err = check_db_integrity()
        if not valid:
            print(f"[数据库错误] 本地数据库损坏: {err}")
            print("[数据库错误] 尝试从飞书恢复...")
            if restore_db_from_lark():
                print("[数据库错误] 已从飞书恢复数据库")
            else:
                raise RuntimeError(f"本地数据库损坏且无法从飞书恢复: {err}")
    conn = sqlite3.connect(str(config.DB_PATH))
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
    if not synced:
        raise RuntimeError("从飞书拉取数据库失败，无法执行修改操作。请检查飞书备份或网络连接后重试。")
    
    # 2. 初始化表结构和时间连续性
    with get_db() as conn:
        _init_db_tables(conn)
        ensure_current_event(conn)
    
    # 3. 打开连接执行修改
    conn = sqlite3.connect(str(config.DB_PATH))
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
    config.LAST_BACKUP_SUCCESS = backup_database()
    if not config.LAST_BACKUP_SUCCESS:
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
    if result.returncode == 0 and os.path.exists(config.DB_PATH):
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
        str(config.DB_PATH),
        str(config.DB_PATH) + "-wal",
        str(config.DB_PATH) + "-shm",
        str(config.DB_PATH) + ".lock",
    ]
    import glob
    # 清理 syncbak 备份文件（sync_db_from_lark 创建）
    for f in glob.glob(str(config.DB_PATH) + ".syncbak.*"):
        files_to_clean.append(f)
    # 清理 bak 备份文件（backup_to_lark.py --restore 创建，恢复成功后无用）
    for f in glob.glob(str(config.DB_PATH) + ".bak.*"):
        files_to_clean.append(f)
    # 清理 downloading 临时文件
    for f in glob.glob(str(config.DB_PATH) + ".downloading*"):
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
        print("[数据同步] ❌ 备份脚本不存在，无法从飞书拉取数据库")
        print(f"  期望路径: {backup_script}")
        return False
    
    # 如果本地有数据库，先备份（防止拉取失败导致数据丢失）
    local_backup = None
    if os.path.exists(config.DB_PATH):
        local_backup = str(config.DB_PATH) + f".syncbak.{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        shutil.copy2(config.DB_PATH, local_backup)
    
    # 从飞书拉取最新数据库
    result = subprocess.run(
        [sys.executable, backup_script, "--restore"],
        capture_output=True, text=True, timeout=60
    )
    
    if result.returncode == 0 and os.path.exists(config.DB_PATH):
        # 拉取成功，删除本地备份
        if local_backup and os.path.exists(local_backup):
            os.remove(local_backup)
        return True
    else:
        # 拉取失败，恢复本地备份
        if local_backup and os.path.exists(local_backup):
            shutil.move(local_backup, config.DB_PATH)
            print("[数据同步] ⚠️  从飞书拉取失败，已恢复本地数据库")
        else:
            print("[数据同步] ❌ 从飞书拉取失败，且无本地备份")
        err = result.stderr.strip() or result.stdout.strip()
        print(f"  详情: {err[:200]}")
        return False


def upload_db_to_lark():
    """将本地数据库上传到飞书云空间。返回 True 表示上传成功。
    
    【安全警告】此函数已废弃，不应直接调用。
    请使用 backup_database()，它包含完整性检查和安全检查（飞书有备份时禁止上传空数据库）。
    此函数保留仅为向后兼容，内部调用 backup_database()。
    """
    print("[数据库上传] 警告: upload_db_to_lark() 已废弃，请使用 backup_database()")
    return backup_database()


def init_db():
    """初始化数据库。
    
    每次操作前都从飞书拉取最新数据库，保证数据一致性。
    流程：
    1. 从飞书同步最新数据库（覆盖本地）
    2. 初始化表结构（幂等）
    3. 确保有进行中事件（时间连续性）
    
    安全策略：拉取失败直接报错停止，不使用本地旧数据，
    绝不创建空数据库，防止空数据覆盖飞书备份。
    """
    # 第一步：从飞书拉取最新数据库
    synced = sync_db_from_lark()
    
    if not synced:
        raise RuntimeError("从飞书拉取数据库失败。请检查飞书备份或网络连接后重试。")

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
    if config.TEST_MODE:
        print("[测试模式] 跳过飞书备份（测试数据不污染生产备份）")
        return True
    
    backup_script = config.SCRIPT_DIR / "backup_to_lark.py"
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
            # 主要通过退出码判断成功（backup_to_lark.py 失败返回非零退出码）
            # 同时检查输出中是否包含最终成功标记作为双重保险
            # 注意：不能检查"失败"字样，因为重试成功时输出中会包含之前的失败信息
            output = result.stdout + result.stderr
            if result.returncode == 0 and ("✅ 成功" in output or "✅ 已备份" in output or "无需上传" in output):
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

    # 迁移：旧表没有 extras 字段时添加
    cols = [row[1] for row in conn.execute("PRAGMA table_info(events)").fetchall()]
    if "extras" not in cols:
        conn.execute("ALTER TABLE events ADD COLUMN extras TEXT NOT NULL DEFAULT ''")
        print("[数据库迁移] events 表已添加 extras 字段")

    # 迁移：旧 categories 表没有 id/parent_id 字段时重建
    # 先检查表是否存在（首次使用时 categories 表可能尚未创建）
    cat_exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='categories'"
    ).fetchone()
    if cat_exists:
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



