#!/usr/bin/env python3
"""
时间统计数据库备份与恢复脚本（v4）
仅负责数据库文件的备份与恢复，技能代码已迁移到 GitHub 管理。

备份策略：文件名包含时间戳，上传后下载验证，成功删旧版，失败删损坏文件重试
"""
import subprocess
import json
import sys
import os
import sqlite3
import shutil
import tempfile
import time
import re
from datetime import datetime

_DEFAULT_DATA_DIR = os.path.join(os.path.expanduser("~"), ".super_doubao", "super-doubao-runtime", "workspace", "time-tracker-data")
DB_PATH = os.path.join(os.environ.get("TIME_TRACKER_DATA_DIR", _DEFAULT_DATA_DIR), "time-tracker.db")
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(SCRIPT_DIR)

FOLDER_TOKEN = "A3xkf1WSwlD33edABotcH70SnZd"
DB_FOLDER_TOKEN = "A1qHfIqK7lvzf6dSgRtcgJ6Inbe"

DB_FILE_PREFIX = "time-tracker-db-"

SAFE_MIN_RECORDS = 3
MAX_RETRIES = 3
RETRY_DELAY = 2

def run_cmd(cmd, cwd=None):
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True, cwd=cwd)
    output = result.stdout.strip()
    if result.returncode != 0:
        return {"ok": False, "error": (result.stderr.strip() or output)[:500]}
    # 尝试完整解析
    try:
        return json.loads(output)
    except json.JSONDecodeError:
        pass
    # lark-cli 可能在 JSON 后输出额外提示文本（如 "The workspace directory is..."），
    # 尝试提取第一个 '{' 到最后一个 '}' 之间的 JSON 内容
    start = output.find('{')
    end = output.rfind('}')
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(output[start:end+1])
        except json.JSONDecodeError:
            pass
    return {"ok": True, "raw": output}

def list_remote_files(folder_token):
    cmd = f'lark-cli drive files list --params \'{{"folder_token": "{folder_token}", "page_size": 50}}\''
    for _ in range(MAX_RETRIES):
        data = run_cmd(cmd)
        if data and data.get("ok"):
            return data.get("data", {}).get("files", [])
        time.sleep(RETRY_DELAY)
    return []

def find_files_by_prefix(prefix, folder_token):
    files = list_remote_files(folder_token)
    matched = [f for f in files if f.get("name", "").startswith(prefix) and f.get("type") == "file"]
    matched.sort(key=lambda x: int(x.get("modified_time", 0)), reverse=True)
    return matched

def delete_remote_file(file_token):
    return run_cmd(f'lark-cli drive +delete --file-token {file_token} --type file --yes').get("ok", False)

def upload_file(local_path, filename, folder_token):
    """上传文件，返回 (ok, file_token)。
    直接从上传结果中提取 file_token，避免通过文件名查找时误匹配。
    """
    local_dir = os.path.dirname(local_path)
    local_filename = os.path.basename(local_path)
    cmd = f'lark-cli drive +upload --file "./{local_filename}" --folder-token "{folder_token}" --name "{filename}"'
    for _ in range(MAX_RETRIES):
        result = run_cmd(cmd, cwd=local_dir)
        if result and result.get("ok"):
            data = result.get("data", {})
            file_token = data.get("file_token") or data.get("token") or result.get("file_token") or result.get("token")
            return True, file_token
        time.sleep(RETRY_DELAY)
    return False, None

def download_file(file_token, output_path):
    output_dir = os.path.dirname(output_path)
    output_filename = os.path.basename(output_path)
    os.makedirs(output_dir, exist_ok=True)
    cmd = f'lark-cli drive +download --file-token "{file_token}" --output "./{output_filename}"'
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, cwd=output_dir)
    return r.returncode == 0 and os.path.exists(output_path)

def validate_db_file(path):
    if not os.path.exists(path):
        return False, 0
    try:
        conn = sqlite3.connect(path)
        tables = [t[0] for t in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        if "events" not in tables:
            conn.close()
            return False, 0
        count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        conn.close()
        return True, count
    except Exception:
        return False, 0

def get_db_version_tag():
    """生成数据库备份文件名的版本标签。

    使用上传时间戳，确保每次上传的文件名唯一，
    避免同名文件导致验证失败时误删旧备份。
    格式：YYYYMMDD_HHMMSS
    """
    return datetime.now().strftime("%Y%m%d_%H%M%S")

def get_local_record_count():
    if not os.path.exists(DB_PATH):
        return 0
    try:
        conn = sqlite3.connect(DB_PATH)
        c1 = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        c2 = conn.execute("SELECT COUNT(*) FROM current").fetchone()[0]
        conn.close()
        return c1 + c2
    except Exception:
        return -1

def backup_with_verification(local_path, filename, folder_token, validate_func, old_files_prefix):
    """带验证的备份：上传→下载验证→验证通过删旧版，验证失败删损坏文件重试。

    关键安全措施：
    1. 文件名唯一（基于上传时间），不会同名
    2. 直接使用上传返回的 file_token，不通过文件名查找
    3. 验证失败只删除刚刚上传的文件，不会误删旧备份
    """
    for attempt in range(MAX_RETRIES):
        print(f"  [上传] 尝试 {attempt+1}/{MAX_RETRIES}: {filename}")
        ok, new_token = upload_file(local_path, filename, folder_token)
        if not ok or not new_token:
            print(f"  [失败] 上传失败")
            time.sleep(RETRY_DELAY)
            continue
        time.sleep(1)
        with tempfile.TemporaryDirectory() as tmpdir:
            verify_path = os.path.join(tmpdir, filename)
            if not download_file(new_token, verify_path):
                print(f"  [失败] 下载验证失败，删除损坏文件")
                delete_remote_file(new_token)
                time.sleep(RETRY_DELAY)
                continue
            valid, info = validate_func(verify_path)
            if not valid:
                print(f"  [失败] 文件验证不通过，删除损坏文件")
                delete_remote_file(new_token)
                time.sleep(RETRY_DELAY)
                continue
        print(f"  [验证] ✅ 通过 ({info})")
        # 验证通过后才删除旧版本
        old_files = find_files_by_prefix(old_files_prefix, folder_token)
        deleted = sum(1 for f in old_files if f.get("token") != new_token and delete_remote_file(f.get("token")))
        if deleted > 0:
            print(f"  [清理] 已删除 {deleted} 个旧版本")
        return True, new_token
    print(f"  [失败] 已重试 {MAX_RETRIES} 次")
    return False, None

def do_db_backup(force=False):
    if not os.path.exists(DB_PATH):
        print("[数据库备份] 跳过: 文件不存在")
        return True
    local_count = get_local_record_count()
    print(f"[数据库备份] 本地记录数: {local_count}, 大小: {os.path.getsize(DB_PATH)/1024:.1f}KB")
    remote_files = find_files_by_prefix(DB_FILE_PREFIX, DB_FOLDER_TOKEN)
    if not force and remote_files:
        if local_count == 0 or (0 < local_count < SAFE_MIN_RECORDS):
            print(f"[数据库备份] ⚠️  本地记录数较少({local_count}条)，跳过")
            return True
    filename = f"{DB_FILE_PREFIX}{get_db_version_tag()}.db"
    print(f"[数据库备份] 目标: {filename}")
    existing = [f for f in remote_files if f.get("name") == filename]
    if existing:
        with tempfile.TemporaryDirectory() as tmpdir:
            vp = os.path.join(tmpdir, "check.db")
            if download_file(existing[0]["token"], vp):
                valid, count = validate_db_file(vp)
                if valid and count == local_count:
                    print(f"[数据库备份] ✅ 远程已是最新且完整 ({count}条)，无需上传")
                    return True
    success, _ = backup_with_verification(DB_PATH, filename, DB_FOLDER_TOKEN, validate_db_file, DB_FILE_PREFIX)
    print(f"[数据库备份] {'✅ 成功' if success else '❌ 失败'}")
    return success

def do_db_restore():
    print("[数据库恢复] 开始...")
    files = find_files_by_prefix(DB_FILE_PREFIX, DB_FOLDER_TOKEN)
    if not files:
        print("[数据库恢复] ❌ 未找到备份")
        return False
    def extract_time(fn):
        m = re.search(r'(\d{8}_\d{6})', fn)
        return m.group(1) if m else ""
    files.sort(key=lambda x: extract_time(x.get("name", "")), reverse=True)
    for f in files:
        print(f"[数据库恢复] 尝试: {f.get('name')}")
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = os.path.join(tmpdir, "restore.db")
            if not download_file(f.get("token"), tmp_path):
                continue
            valid, count = validate_db_file(tmp_path)
            if not valid:
                print("  损坏，尝试下一个...")
                continue
            if os.path.exists(DB_PATH):
                shutil.copy2(DB_PATH, DB_PATH + f".bak.{datetime.now().strftime('%Y%m%d_%H%M%S')}")
            shutil.move(tmp_path, DB_PATH)
            print(f"[数据库恢复] ✅ 成功，恢复 {count} 条记录")
            return True
    print("[数据库恢复] ❌ 所有备份均无效")
    return False

def cleanup_all_duplicates():
    """清理重复/损坏文件。

    安全措施：如果没有找到任何有效文件，不删除任何文件，防止数据丢失。
    """
    print("--- 清理重复/损坏文件 ---")
    for prefix, token, name, vfunc in [
        (DB_FILE_PREFIX, DB_FOLDER_TOKEN, "数据库", validate_db_file),
    ]:
        files = find_files_by_prefix(prefix, token)
        if len(files) <= 1:
            print(f"[{name}] {len(files)} 个文件，无需清理")
            continue
        print(f"[{name}] 发现 {len(files)} 个文件，逐一验证...")
        keep_token = None
        for f in files:
            with tempfile.TemporaryDirectory() as tmpdir:
                tp = os.path.join(tmpdir, "check")
                if download_file(f.get("token"), tp):
                    valid, info = vfunc(tp)
                    if valid:
                        keep_token = f.get("token")
                        print(f"  保留: {f.get('name')} ({info})")
                        break
                    else:
                        print(f"  损坏: {f.get('name')}，删除")
                        delete_remote_file(f.get("token"))
        # 关键安全检查：只有找到有效文件时才删除其他文件
        if keep_token:
            for f in files:
                if f.get("token") != keep_token:
                    delete_remote_file(f.get("token"))
            print(f"[{name}] 清理完成")
        else:
            print(f"[{name}] ⚠️  未找到任何有效文件，为保护数据安全，不删除任何文件")

def main():
    import argparse
    parser = argparse.ArgumentParser(description="时间统计数据库备份与恢复工具 v4（仅数据库，技能代码已迁移到 GitHub）")
    parser.add_argument("--restore", action="store_true", help="从飞书恢复数据库")
    parser.add_argument("--force", action="store_true", help="强制备份（跳过安全检查）")
    parser.add_argument("--cleanup", action="store_true", help="清理飞书上的重复/损坏数据库备份")
    args = parser.parse_args()
    print("=" * 50)
    print("时间统计数据库备份与恢复 v4")
    print("（技能代码已迁移到 GitHub，本脚本仅负责数据库）")
    print("=" * 50)
    print(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()
    if args.cleanup:
        cleanup_all_duplicates()
        print("\n" + "=" * 50 + "\n清理完成\n" + "=" * 50)
        return
    if args.restore:
        sys.exit(0 if do_db_restore() else 1)
    print("--- 数据库备份 ---")
    db_success = do_db_backup(force=args.force)
    print()
    print("=" * 50)
    print("完成")
    print("=" * 50)
    # 根据备份结果设置退出码，确保调用方能准确判断成功/失败
    sys.exit(0 if db_success else 1)

if __name__ == "__main__":
    main()
