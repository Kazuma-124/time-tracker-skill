#!/usr/bin/env python3
"""
分类、别名、标准名管理命令。
"""

from datetime import datetime, timedelta

from config import TZ
from db import init_db, get_db, db_transaction
from models import (
    build_name_library, load_aliases, load_categories, get_category_tree,
    print_category_tree, load_standard_names, get_standard_name,
    set_standard_category, has_category_cycle, now_iso,
)
from stats import get_period_range


# ============ 分类管理 ============
def cmd_categories_list(args):
    """列出所有分类（树形展示）。"""
    init_db()
    with get_db() as conn:
        categories = load_categories(conn)
        tree = get_category_tree(categories)
    print("=== 分类树 ===")
    if not tree:
        print("(暂无分类)")
        return
    for node in tree:
        print_category_tree(node, 0)
    print()
    print(f"共 {len(categories)} 个分类")


def cmd_category_add(args):
    """添加新分类。"""
    name = args.name.strip()
    if not name:
        print("错误: 分类名称不能为空")
        return
    parent = getattr(args, 'parent', None)
    description = getattr(args, 'description', '') or ''

    with db_transaction() as conn:
        existing = conn.execute("SELECT id FROM categories WHERE name = ?", (name,)).fetchone()
        if existing:
            print(f"错误: 分类 '{name}' 已存在")
            return

        parent_id = None
        if parent:
            parent_row = conn.execute("SELECT id FROM categories WHERE name = ?", (parent,)).fetchone()
            if not parent_row:
                print(f"错误: 父分类 '{parent}' 不存在")
                return
            parent_id = parent_row["id"]

        conn.execute(
            "INSERT INTO categories (name, parent_id, keywords, description, created_at) VALUES (?, ?, '[]', ?, ?)",
            (name, parent_id, description, now_iso())
        )
    print(f"✅ 分类 '{name}' 已创建")
    if parent:
        print(f"  父分类: {parent}")
    if description:
        print(f"  说明: {description}")


def cmd_category_remove(args):
    """删除分类（不影响历史记录）。"""
    name = args.name.strip()
    if not name:
        print("错误: 分类名称不能为空")
        return

    with db_transaction() as conn:
        cat = conn.execute("SELECT id FROM categories WHERE name = ?", (name,)).fetchone()
        if not cat:
            print(f"错误: 分类 '{name}' 不存在")
            return
        cat_id = cat["id"]

        children = conn.execute("SELECT COUNT(*) as cnt FROM categories WHERE parent_id = ?", (cat_id,)).fetchone()
        if children["cnt"] > 0:
            print(f"错误: 分类 '{name}' 下有 {children['cnt']} 个子分类，请先处理子分类")
            return

        std_count = conn.execute("SELECT COUNT(*) as cnt FROM standard_names WHERE category_id = ?", (cat_id,)).fetchone()
        if std_count["cnt"] > 0:
            print(f"警告: 有 {std_count['cnt']} 个标准名使用该分类，删除后这些标准名将变为未分类")

        conn.execute("DELETE FROM categories WHERE id = ?", (cat_id,))
    print(f"✅ 分类 '{name}' 已删除")
    print("  （历史记录不受影响，仅删除分类配置）")


# ============ 标准名管理 ============
def cmd_standard_names_list(args):
    """列出所有标准名及其分类。"""
    init_db()
    with get_db() as conn:
        std_names = load_standard_names(conn)
    print("=== 标准名列表 ===")
    if not std_names:
        print("(暂无标准名)")
        return
    print(f"{'标准名':<25} {'分类':<15} {'说明'}")
    print("-" * 70)
    for s in std_names:
        desc = s.get("description", "") or ""
        print(f"{s['name']:<25} {s['category_name']:<15} {desc}")
    print()
    print(f"共 {len(std_names)} 个标准名")


def cmd_add_standard_name(args):
    """创建新的标准名。"""
    name = args.name.strip()
    if not name:
        print("错误: 标准名名称不能为空")
        return
    category = getattr(args, 'category', None)
    description = getattr(args, 'description', '') or ''

    with db_transaction() as conn:
        existing = conn.execute("SELECT id FROM standard_names WHERE name = ?", (name,)).fetchone()
        if existing:
            print(f"错误: 标准名 '{name}' 已存在")
            return

        category_id = None
        if category:
            cat_row = conn.execute("SELECT id FROM categories WHERE name = ?", (category,)).fetchone()
            if not cat_row:
                print(f"错误: 分类 '{category}' 不存在")
                return
            category_id = cat_row["id"]

        now = now_iso()
        conn.execute(
            "INSERT INTO standard_names (name, category_id, description, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (name, category_id, description, now, now)
        )
    print(f"✅ 标准名 '{name}' 已创建")
    if category:
        print(f"  分类: {category}")
    else:
        print(f"  分类: (未分类)")
    if description:
        print(f"  说明: {description}")


def cmd_set_standard_category(args):
    """设置标准名的分类。"""
    standard_name = args.standard_name.strip()
    category = args.category.strip()
    if not standard_name or not category:
        print("错误: 标准名和分类名称不能为空")
        return

    with db_transaction() as conn:
        success, msg = set_standard_category(conn, standard_name, category)
        if not success:
            print(f"错误: {msg}")
            return
    print(f"✅ {msg}")


def cmd_remove_standard_name(args):
    """删除标准名（仅当无原始名映射时）。"""
    name = args.name.strip()
    if not name:
        print("错误: 标准名名称不能为空")
        return

    with db_transaction() as conn:
        std = conn.execute("SELECT id FROM standard_names WHERE name = ?", (name,)).fetchone()
        if not std:
            print(f"错误: 标准名 '{name}' 不存在")
            return
        std_id = std["id"]

        alias_count = conn.execute("SELECT COUNT(*) as cnt FROM aliases WHERE standard_name = ?", (name,)).fetchone()
        if alias_count["cnt"] > 0:
            print(f"错误: 有 {alias_count['cnt']} 个原始名映射到该标准名，请先删除映射")
            return

        conn.execute("DELETE FROM standard_names WHERE id = ?", (std_id,))
    print(f"✅ 标准名 '{name}' 已删除")


# ============ 别名管理 ============
def cmd_aliases_list(args):
    """列出所有事件原始名映射。"""
    init_db()
    with get_db() as conn:
        aliases = load_aliases(conn)
    print("=== 原始名 → 标准名映射 ===")
    if not aliases:
        print("(暂无映射)")
        return
    print(f"{'原始名':<25} → {'标准名'}")
    print("-" * 60)
    for raw, std in sorted(aliases.items()):
        print(f"{raw:<25} → {std}")
    print()
    print(f"共 {len(aliases)} 条映射")


def cmd_alias_add(args):
    """添加事件原始名映射。"""
    alias = args.alias.strip()
    standard = args.standard.strip()
    if not alias or not standard:
        print("错误: 原始名和标准名不能为空")
        return

    with db_transaction() as conn:
        std = conn.execute("SELECT id FROM standard_names WHERE name = ?", (standard,)).fetchone()
        if not std:
            print(f"警告: 标准名 '{standard}' 不存在，映射仍会创建，但统计时可能无法解析分类")

        existing = conn.execute("SELECT standard_name FROM aliases WHERE alias = ?", (alias,)).fetchone()
        if existing:
            print(f"警告: 原始名 '{alias}' 已映射到 '{existing['standard_name']}'，将覆盖为 '{standard}'")

        conn.execute(
            "INSERT OR REPLACE INTO aliases (alias, standard_name) VALUES (?, ?)",
            (alias, standard)
        )
    print(f"✅ 映射已创建: {alias} → {standard}")


def cmd_alias_remove(args):
    """删除事件原始名映射。"""
    alias = args.alias.strip()
    if not alias:
        print("错误: 原始名不能为空")
        return

    with db_transaction() as conn:
        existing = conn.execute("SELECT standard_name FROM aliases WHERE alias = ?", (alias,)).fetchone()
        if not existing:
            print(f"错误: 原始名 '{alias}' 没有映射")
            return
        conn.execute("DELETE FROM aliases WHERE alias = ?", (alias,))
    print(f"✅ 映射已删除: {alias} → {existing['standard_name']}")


# ============ 名称检查 ============
def cmd_name_check(args):
    """检查事件名称一致性，输出管理方案。"""
    period = getattr(args, 'period', None)
    date = getattr(args, 'date', None)

    init_db()
    with get_db() as conn:
        name_library = build_name_library(conn)
        aliases = load_aliases(conn)
        std_names = load_standard_names(conn)
        categories = load_categories(conn)

    period_names = set()
    if period or date:
        ref_date = date
        p = period or "day"
        try:
            from stats import query_events_in_range
            start, end = get_period_range(p, ref_date)
            with get_db() as conn:
                events = query_events_in_range(conn, start, end)
                period_names = set(e["name"] for e in events)
        except Exception:
            period_names = set(name_library.keys())
    else:
        period_names = set(name_library.keys())

    print("=" * 70)
    print("  事件名称与分类管理检查")
    print("=" * 70)
    print()

    print(f"--- 本周期事件名称 ({len(period_names)} 种) ---")
    for name in sorted(period_names):
        info = name_library.get(name, {"count": 0, "total_minutes": 0})
        mapped = aliases.get(name, name)
        is_mapped = "✓" if name in aliases else "✗"
        print(f"  [{is_mapped}] {name:<25} {info['count']:>3}次  {info['total_minutes']:>6.0f}分钟  → {mapped}")
    print()

    unmapped = [name for name in name_library if name not in aliases and name != "未记录"]
    print(f"--- 未映射的原始名 ({len(unmapped)} 种) ---")
    if unmapped:
        for name in sorted(unmapped, key=lambda n: -name_library[n]["total_minutes"]):
            info = name_library[name]
            print(f"  {name:<25} {info['count']:>3}次  {info['total_minutes']:>6.0f}分钟")
        print()
        print("  【建议】为以上原始名建立映射到标准名:")
        print('    python3 time_tracker.py add-alias "<原始名>" "<标准名>"')
    else:
        print("  (所有原始名都已映射)")
    print()

    uncategorized = [s for s in std_names if s["category_name"] == "其他" or not s["category_id"]]
    print(f"--- 未分类/归为'其他'的标准名 ({len(uncategorized)} 个) ---")
    if uncategorized:
        for s in uncategorized:
            print(f"  {s['name']}")
        print()
        print("  【建议】为以上标准名设置分类:")
        print('    python3 time_tracker.py set-standard-category "<标准名>" "<分类>"')
    else:
        print("  (所有标准名都已分类)")
    print()

    print(f"--- 配置概览 ---")
    print(f"  标准名: {len(std_names)} 个（用 standard-names 查看完整列表）")
    print(f"  分类: {len(categories)} 个（用 categories 查看完整列表）")
    print()

