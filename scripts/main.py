#!/usr/bin/env python3
"""
CLI 入口：参数解析、命令分发、测试模式切换、异常处理。
"""

import argparse
import os
import sqlite3
import sys

import config
from db import cleanup_local_db, ensure_data_dir
from events import (
    cmd_start, cmd_stop, cmd_current, cmd_rename_current,
    cmd_list_events, cmd_rename_event,
)
from export import cmd_export, cmd_export_period


def cmd_init_test_db(args):
    """初始化测试数据库：从生产数据库复制一份用于测试。

    测试数据库路径: DATA_DIR/test-time-tracker.db
    测试模式下使用 --test 参数，所有操作指向测试数据库，不影响生产数据。
    """
    if not os.path.exists(config.DB_PATH):
        print("错误: 生产数据库不存在，无法复制")
        print(f"  生产数据库路径: {config.DB_PATH}")
        return

    import shutil
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)

    # 如果测试数据库已存在，先删除
    if os.path.exists(config.TEST_DB_PATH):
        os.remove(config.TEST_DB_PATH)
        print(f"  已删除旧的测试数据库")

    # 复制生产数据库到测试数据库
    shutil.copy2(str(config.DB_PATH), str(config.TEST_DB_PATH))

    if not os.path.exists(config.TEST_DB_PATH):
        print("错误: 测试数据库复制失败")
        return

    try:
        conn = sqlite3.connect(str(config.TEST_DB_PATH))
        count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        conn.close()
    except Exception as e:
        print(f"错误: 测试数据库验证失败: {e}")
        return

    print("=" * 50)
    print("✅ 测试数据库初始化完成")
    print(f"  生产数据库: {config.DB_PATH}")
    print(f"  测试数据库: {config.TEST_DB_PATH}")
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
    parser = argparse.ArgumentParser(description="柳比歇夫时间统计法追踪工具（SQLite版）")
    parser.add_argument("--test", action="store_true", help="使用测试数据库（不影响生产数据，不备份到飞书）")
    subparsers = parser.add_subparsers(dest="command", help="可用命令")

    p_start = subparsers.add_parser("start", help="开始新事件（自动结束上一个）")
    p_start.add_argument("name", help="事件名称")
    p_start.add_argument("--note", help="给刚结束的事件加备注（可选）")
    p_start.add_argument("--rename", help="把刚结束的事件改名为指定名称（可选）")

    subparsers.add_parser("stop", help="结束当前事件")
    subparsers.add_parser("current", help="查看当前进行中的事件")
    p_rename = subparsers.add_parser("rename-current", help="修正当前进行中的事件名称")
    p_rename.add_argument("name", help="新的事件名称")
    p_list = subparsers.add_parser("list-events", help="列出最近的事件记录（带ID）")
    p_list.add_argument("--limit", type=int, default=10, help="显示条数，默认10")
    p_rename_ev = subparsers.add_parser("rename-event", help="强制修改已有事件的名称（非常规操作）")
    p_rename_ev.add_argument("event_id", help="事件ID（通过 list-events 查看）")
    p_rename_ev.add_argument("new_name", help="新的事件名称")

    p_export_period = subparsers.add_parser("export-period", help="导出周期内原始事件为文本，供模型当场统计")
    p_export_period.add_argument("period", choices=["day", "week", "month", "quarter", "year"], help="统计周期")
    p_export_period.add_argument("date", nargs="?", default=None, help="参考日期 YYYY-MM-DD（默认今天）")

    subparsers.add_parser("export", help="导出全部原始事件数据为 JSON")

    subparsers.add_parser("init-test-db", help="初始化测试数据库（从生产数据库复制一份用于测试）")

    args = parser.parse_args()

    # 处理测试模式：切换到测试数据库路径
    if args.test:
        config.TEST_MODE = True
        config.DB_PATH = config.TEST_DB_PATH
        print("[测试模式] 已切换到测试数据库，不会影响生产数据，不会备份到飞书", file=sys.stderr)
        print(file=sys.stderr)
    if not args.command:
        parser.print_help()
        return

    # 确保工作目录存在（与配置文件中的 data_dir 一致）
    ensure_data_dir()

    commands = {
        "start": cmd_start, "stop": cmd_stop, "current": cmd_current, "rename-current": cmd_rename_current,
        "list-events": cmd_list_events, "rename-event": cmd_rename_event,
        "export-period": cmd_export_period, "export": cmd_export,
        "init-test-db": cmd_init_test_db,
    }
    try:
        commands[args.command](args)
    except sqlite3.DatabaseError as e:
        print(file=sys.stderr)
        print("=" * 50, file=sys.stderr)
        print(f"❌ 数据库错误: {e}", file=sys.stderr)
        print("=" * 50, file=sys.stderr)
        print("建议:", file=sys.stderr)
        print("  1. 运行 backup_to_lark.py --restore 从飞书恢复数据库", file=sys.stderr)
        print("  2. 如果恢复失败，检查飞书云空间中的备份文件", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(file=sys.stderr)
        print("=" * 50, file=sys.stderr)
        print(f"❌ 未预期的错误: {type(e).__name__}: {e}", file=sys.stderr)
        print("=" * 50, file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        # 非测试模式下，操作完成后删除本地数据库
        # 飞书是唯一权威数据源，本地不保留数据库文件
        # 但如果备份失败，保留本地数据库以防数据丢失
        if not config.TEST_MODE and args.command != "init-test-db":
            if config.LAST_BACKUP_SUCCESS:
                cleanup_local_db()
            else:
                print(file=sys.stderr)
                print("=" * 60, file=sys.stderr)
                print("[数据安全] 由于备份失败，本地数据库已保留", file=sys.stderr)
                print(f"  数据库路径: {config.DB_PATH}", file=sys.stderr)
                print("  请解决备份问题后手动运行 backup_to_lark.py 进行备份", file=sys.stderr)
                print("=" * 60, file=sys.stderr)


if __name__ == "__main__":
    main()
