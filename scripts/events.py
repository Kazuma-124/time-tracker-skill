#!/usr/bin/env python3
"""
事件记录命令：start, stop, current, note, rename, list。
只操作原始事件数据，不做分类或映射。
"""

import uuid
from datetime import datetime

from config import TZ
from db import init_db, get_db, db_transaction
from models import now_iso, parse_iso, format_duration

# ============ 事件记录 ============
def cmd_start(args):
    event_name = args.name.strip()
    if not event_name:
        print("错误: 事件名称不能为空")
        return

    end_time = now_iso()
    note_text = (args.note or "").strip()
    rename_to = (args.rename or "").strip()

    with db_transaction() as conn:
        # 结束上一个事件：记录原始名、开始时间、结束时间、持续时间
        current = conn.execute("SELECT name, start_time, note FROM current WHERE id = 1").fetchone()
        if current:
            ended_name = rename_to or current["name"]
            duration = (parse_iso(end_time) - parse_iso(current["start_time"])).total_seconds() / 60.0
            event_id = str(uuid.uuid4())[:8]
            # 合并备注：进行中累计的备注 + --note 参数追加的一句
            ended_note = current["note"] or ""
            if note_text:
                ended_note = f"{ended_note}；{note_text}" if ended_note else note_text
            conn.execute(
                "INSERT INTO events (id, name, start_time, end_time, duration_minutes, note) VALUES (?, ?, ?, ?, ?, ?)",
                (event_id, ended_name, current["start_time"], end_time, round(duration, 1), ended_note)
            )
            print(f"[结束] {current['name']}" + (f"（改名为 {ended_name}）" if rename_to else ""))
            print(f"  时间: {parse_iso(current['start_time']).strftime('%H:%M')} ~ {parse_iso(end_time).strftime('%H:%M')}")
            print(f"  时长: {format_duration(duration)}")
            if ended_note:
                print(f"  备注: {ended_note}")
        else:
            print("[提示] 此前无活跃事件，本次为首个事件。")

        # 开始新事件：记录事件名称和开始时间，备注重置为空
        conn.execute(
            "INSERT OR REPLACE INTO current (id, name, start_time, note) VALUES (1, ?, ?, '')",
            (event_name, end_time)
        )

    print(f"\n[开始] {event_name}")
    print(f"  开始时间: {end_time}")


def cmd_stop(args):
    """结束当前事件，并自动开始'未记录'事件（保证任何时刻都有事件在进行中）。"""
    end_time = now_iso()
    with db_transaction() as conn:
        current = conn.execute("SELECT name, start_time, note FROM current WHERE id = 1").fetchone()
        if not current:
            print("当前没有活跃事件，已自动开始'未记录'事件")
            conn.execute(
                "INSERT OR REPLACE INTO current (id, name, start_time, note) VALUES (1, '未记录', ?, '')",
                (end_time,)
            )
            return
        duration = (parse_iso(end_time) - parse_iso(current["start_time"])).total_seconds() / 60.0
        event_id = str(uuid.uuid4())[:8]
        conn.execute(
            "INSERT INTO events (id, name, start_time, end_time, duration_minutes, note) VALUES (?, ?, ?, ?, ?, ?)",
            (event_id, current["name"], current["start_time"], end_time, round(duration, 1), current["note"] or "")
        )
        # 自动开始"未记录"事件，保证时间连续
        conn.execute(
            "INSERT OR REPLACE INTO current (id, name, start_time, note) VALUES (1, '未记录', ?, '')",
            (end_time,)
        )
    print(f"[结束] {current['name']}")
    print(f"  时长: {format_duration(duration)}")
    if current["note"]:
        print(f"  备注: {current['note']}")
    print(f"  已自动开始'未记录'事件（时间连续不中断）")


def cmd_current(args):
    init_db()
    with get_db() as conn:
        current = conn.execute("SELECT name, start_time, note FROM current WHERE id = 1").fetchone()
        if not current:
            print("当前没有活跃事件")
            return
        elapsed = (datetime.now(TZ) - parse_iso(current["start_time"])).total_seconds() / 60.0
    print(f"[进行中] {current['name']}")
    print(f"  开始时间: {current['start_time']}")
    print(f"  已持续: {format_duration(elapsed)}")
    if current["note"]:
        print(f"  备注: {current['note']}")


def cmd_note(args):
    """给当前进行中的事件追加备注（流水式，用；拼接）。"""
    if args.clear:
        with db_transaction() as conn:
            current = conn.execute("SELECT name FROM current WHERE id = 1").fetchone()
            if not current:
                print("当前没有活跃事件，无法清空备注")
                return
            conn.execute("UPDATE current SET note = '' WHERE id = 1")
        print("✅ 已清空当前事件的备注")
        return

    new_note = (args.note or "").strip()
    if not new_note:
        print("错误: 备注内容不能为空（用 --clear 清空）")
        return
    with db_transaction() as conn:
        current = conn.execute("SELECT note FROM current WHERE id = 1").fetchone()
        if not current:
            print("当前没有活跃事件，无法加备注")
            return
        old_note = current["note"] or ""
        merged = f"{old_note}；{new_note}" if old_note else new_note
        conn.execute("UPDATE current SET note = ? WHERE id = 1", (merged,))
    print(f"✅ 已追加备注: {new_note}")
    print(f"   当前备注全文: {merged}")


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
            "SELECT id, name, start_time, end_time, duration_minutes, note FROM events ORDER BY start_time DESC LIMIT ?",
            (limit,)
        ).fetchall()
    print(f"=== 最近 {len(rows)} 条事件记录 ===")
    for r in rows:
        print(f"  ID: {r['id']}")
        print(f"  名称: {r['name']}")
        print(f"  时间: {r['start_time'][:16]} ~ {r['end_time'][11:16]} ({r['duration_minutes']}分钟)")
        if r['note']:
            print(f"  备注: {r['note']}")
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

        conn.execute(
            "UPDATE events SET name = ? WHERE id = ?",
            (new_name, event_id)
        )

    print(f"✅ 事件已强制修改:")
    print(f"  {old_name} → {new_name}")
    print()
    print("注意: 原始名称已被覆盖，无法恢复。建议仅在修正错误时使用。")
