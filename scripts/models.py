#!/usr/bin/env python3
"""
数据模型与解析逻辑：时间工具、名称库、分类树、标准名解析。
所有函数接受数据库连接 conn 参数，不自行打开连接。
"""

import sqlite3
from datetime import datetime

from config import TZ

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


