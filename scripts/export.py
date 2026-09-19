#!/usr/bin/env python3
"""
原始数据导出：把指定周期内的事件整理成纯文本，供模型当场做统计分析。
脚本不做任何聚合、分类或映射，只输出原始事实。
"""

import json
import sys
from datetime import datetime, timedelta

from config import TZ
from db import init_db, get_db
from models import parse_iso, format_duration


# ============ 周期计算 ============
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


def query_events_in_range(conn, start, end):
    """查询与 [start, end) 有交集的事件，按实际重叠时段裁剪。"""
    start_iso = start.isoformat()
    end_iso = end.isoformat()
    rows = conn.execute(
        "SELECT name, start_time, end_time, duration_minutes, note "
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
                "note": r["note"] or ""
            })
    return result


_PERIOD_CN = {"day": "日", "week": "周", "month": "月", "quarter": "季度", "year": "年度"}


def cmd_export_period(args):
    """把周期内原始事件整理成纯文本打印到 stdout，供模型读取后当场统计。"""
    period = args.period
    ref_date = args.date

    init_db()
    start, end = get_period_range(period, ref_date)

    with get_db() as conn:
        events = query_events_in_range(conn, start, end)
        current = conn.execute("SELECT name, start_time FROM current WHERE id = 1").fetchone()

    print("时间统计原始数据")
    print(f"周期: {_PERIOD_CN.get(period, period)}")
    print(f"时间范围: {start.strftime('%Y-%m-%d %H:%M')} ~ {end.strftime('%Y-%m-%d %H:%M')} (Asia/Shanghai)")
    print()

    if not events:
        print("(该周期无已结束事件)")
    else:
        print(f"事件列表（按开始时间升序，共 {len(events)} 条）:")
        for i, e in enumerate(events, 1):
            st = parse_iso(e["start_time"])
            et = parse_iso(e["end_time"])
            note_text = f"  备注: {e['note']}" if e["note"] else ""
            print(f"  {i:>3}. [{st.strftime('%m-%d %H:%M')}~{et.strftime('%H:%M')}] "
                  f"{e['name']}  {e['duration_minutes']:.1f} 分钟{note_text}")
        print()

    # 当前进行中的事件（可能部分落在周期内）
    if current:
        now = datetime.now(TZ)
        cur_start = parse_iso(current["start_time"])
        if cur_start < end and now > start:
            overlap_start = max(cur_start, start)
            elapsed = (now - overlap_start).total_seconds() / 60.0
            print(f"当前进行中事件:")
            print(f"  [{overlap_start.strftime('%m-%d %H:%M')}~至今] {current['name']}  "
                  f"已进行 {elapsed:.1f} 分钟")
            print()

    print("说明: 以上为原始事件数据，未做分类/映射/聚合。统计（时长合计、占比、按名称汇总、"
          "未记录时间缺口等）由模型基于本文本自行计算，结果不保存。")


def cmd_export(args):
    """导出全部原始事件为 JSON。"""
    init_db()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, name, start_time, end_time, duration_minutes, note FROM events ORDER BY start_time"
        ).fetchall()
    events = [dict(r) for r in rows]
    print(json.dumps(events, ensure_ascii=False, indent=2))
    print(f"\n共 {len(events)} 条记录，已输出到 stdout", file=sys.stderr)
