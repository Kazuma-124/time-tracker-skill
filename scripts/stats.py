#!/usr/bin/env python3
"""
统计查询命令：日/周/月/季度/年度统计、查询、名称统计、季度评审、导出。
"""

import csv
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta

from config import TZ, DATA_DIR
from db import init_db, get_db
from models import (
    load_aliases, load_categories, load_standard_names,
    build_event_category_map, resolve_event_category,
    get_category_children, get_category_name_for_standard,
    parse_iso, format_duration,
)

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
    # 周期结束时间在未来时，截断到当前时刻，避免把尚未发生的时间计入未记录
    actual_minutes = sum(e["duration_minutes"] for e in events)
    now = datetime.now(TZ)
    effective_end = min(end, now)
    effective_total_minutes = (effective_end - start).total_seconds() / 60.0
    unrecorded_minutes = max(0.0, effective_total_minutes - actual_minutes)

    # 构建统计用事件列表（含未记录虚拟事件）
    stats_events = list(events)
    if unrecorded_minutes > 0.1:
        stats_events.append({
            "name": "未记录",
            "category": "未记录",
            "start_time": start.isoformat(),
            "end_time": effective_end.isoformat(),
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
        event_cat_map = build_event_category_map(conn, [e["name"] for e in events])

    total_minutes = sum(e["duration_minutes"] for e in events)

    # 通过原始名→标准名→分类实时获取分类
    cat_minutes = defaultdict(float)
    cat_event_names = defaultdict(lambda: defaultdict(float))
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


