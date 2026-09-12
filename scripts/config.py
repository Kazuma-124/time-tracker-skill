#!/usr/bin/env python3
"""
配置模块：加载 config/config.json，提供全局变量。
"""

import json
import os
from datetime import timedelta, timezone
from pathlib import Path

# ============ 配置 ============
SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR.parent / "config" / "config.json"

def load_config():
    """从 config/config.json 加载配置。失败时直接报错，不使用默认值兜底。"""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        raise RuntimeError(f"配置文件不存在: {CONFIG_PATH}")
    except json.JSONDecodeError as e:
        raise RuntimeError(f"配置文件格式错误: {e}")
    except Exception as e:
        raise RuntimeError(f"配置文件加载失败: {e}")

_CONFIG = load_config()

# 数据目录：优先环境变量 TIME_TRACKER_DATA_DIR，否则从配置文件读取
_DEFAULT_DATA_DIR = Path(os.path.expanduser(_CONFIG["data_dir"]))
DATA_DIR = Path(os.environ.get("TIME_TRACKER_DATA_DIR", str(_DEFAULT_DATA_DIR)))
DB_PATH = DATA_DIR / "time-tracker.db"
TEST_DB_PATH = DATA_DIR / "test-time-tracker.db"
TEST_MODE = False  # 测试模式标志，由 main 函数根据 --test 参数设置
LAST_BACKUP_SUCCESS = True  # 最近一次备份是否成功，用于决定是否删除本地数据库

TZ = timezone(timedelta(hours=_CONFIG["timezone_offset_hours"]))
