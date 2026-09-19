#!/usr/bin/env python3
"""
数据模型与时间工具。
只保留时间解析与格式化，不含任何分类/标准名/映射逻辑。
"""

from datetime import datetime

from config import TZ


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
