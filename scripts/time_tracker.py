#!/usr/bin/env python3
"""
time_tracker.py - 兼容入口（薄包装）。

实际实现已拆分为多个模块：
- config.py    配置加载与全局变量
- db.py        数据库连接、事务、飞书同步、备份、表结构
- models.py    名称/分类解析逻辑
- categories.py 分类/别名/标准名管理命令
- events.py    事件记录命令
- stats.py     统计查询命令
- main.py      CLI 入口与命令分发

本文件仅转发到 main.main()，保持向后兼容。
"""
from main import main

if __name__ == "__main__":
    main()
