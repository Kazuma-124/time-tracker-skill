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
        period_title = f"本{period_cn} ({start.strftime('%Y-%m-%d')} ~ {end.strftime('%Y-%m-%d')})"
    else:
        period = "day"
        if args.date:
            ref = datetime.strptime(args.date, "%Y-%m-%d").replace(tzinfo=TZ)
        else:
            ref = datetime.now(TZ)
        start = ref.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        period_title = ref.strftime('%Y-%m-%d')

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
        print(f"{period_title} 暂无事件记录")
        return

    print(f"=== 事件名称一致性检查 ({period_title}) ===")
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


