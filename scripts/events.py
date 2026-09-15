#!/usr/bin/env python3
"""
事件记录命令：start, stop, current, rename, list。
"""

import uuid
from datetime import datetime

from config import TZ
from db import init_db, get_db, db_transaction
from models import resolve_event_category, build_event_category_map, now_iso, parse_iso, format_duration

# ============ 事件记录 ============
def cmd_start(args):
    event_name = args.name.strip()
    if not event_name:
        print("错误: 事件名称不能为空")
        return

    end_time = now_iso()

    with db_transaction() as conn:
        # 结束上一个事件：记录原始名、开始时间、结束时间、持续时间
        current = conn.execute("SELECT name, start_time FROM current WHERE id = 1").fetchone()
        if current:
            duration = (parse_iso(end_time) - parse_iso(current["start_time"])).total_seconds() / 60.0
            event_id = str(uuid.uuid4())[:8]
            conn.execute(
                "INSERT INTO events (id, name, start_time, end_time, duration_minutes) VALUES (?, ?, ?, ?, ?)",
                (event_id, current["name"], current["start_time"], end_time, round(duration, 1))
            )
            print(f"[结束] {current['name']}")
            print(f"  时间: {parse_iso(current['start_time']).strftime('%H:%M')} ~ {parse_iso(end_time).strftime('%H:%M')}")
            print(f"  时长: {format_duration(duration)}")
        else:
            print("[提示] 此前无活跃事件，本次为首个事件。")

        # 开始新事件：记录事件名称和开始时间
        conn.execute(
            "INSERT OR REPLACE INTO current (id, name, start_time) VALUES (1, ?, ?)",
            (event_name, end_time)
        )

    print(f"\n[开始] {event_name}")
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
