#!/usr/bin/env python3
"""
CLI 入口：参数解析、命令分发、测试模式切换、异常处理。
"""

import argparse
import os
import sqlite3
import sys
import traceback

import config
from db import cleanup_local_db, ensure_data_dir
from events import (
    cmd_start, cmd_stop, cmd_current, cmd_rename_current,
    cmd_list_events, cmd_rename_event,
)
from categories import (
    cmd_categories_list, cmd_category_add, cmd_category_remove,
    cmd_standard_names_list, cmd_add_standard_name, cmd_set_standard_category,
    cmd_remove_standard_name, cmd_aliases_list, cmd_alias_add, cmd_alias_remove,
    cmd_name_check,
)
from stats import (
    cmd_stats, cmd_stats_json, cmd_query, cmd_name_stats,
    cmd_quarterly_review, cmd_export, cmd_export_stats,
)

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
    
    # 验证复制结果
    if not os.path.exists(config.TEST_DB_PATH):
        print("错误: 测试数据库复制失败")
        return
    
    # 验证测试数据库完整性
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
    # 测试模式通过修改 config 模块属性实现
    
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
        config.TEST_MODE = True
        config.DB_PATH = config.TEST_DB_PATH
        print("[测试模式] 已切换到测试数据库，不会影响生产数据，不会备份到飞书")
        print()
    if not args.command:
        parser.print_help()
        return

    # 确保工作目录存在（与配置文件中的 data_dir 一致）
    ensure_data_dir()

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
        if not config.TEST_MODE and args.command != "init-test-db":
            if config.LAST_BACKUP_SUCCESS:
                cleanup_local_db()
            else:
                print()
                print("=" * 60)
                print("[数据安全] 由于备份失败，本地数据库已保留")
                print(f"  数据库路径: {config.DB_PATH}")
                print("  请解决备份问题后手动运行 backup_to_lark.py 进行备份")
                print("=" * 60)


if __name__ == "__main__":
    main()
