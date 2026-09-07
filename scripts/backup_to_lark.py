#!/usr/bin/env python3
"""
时间统计数据备份与恢复脚本（v3）
备份策略：文件名包含版本标识，上传后下载验证，成功删旧版，失败删损坏文件重试
"""
import subprocess
import json
import sys
import os
import sqlite3
import shutil
import tarfile
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
SKILL_FOLDER_TOKEN = "FHMpfxJJ3ltZRZdH9eRcotKonSh"

DB_FILE_PREFIX = "time-tracker-db-"
SKILL_FILE_PREFIX = "time-tracker-skill-v"

SAFE_MIN_RECORDS = 3
MAX_RETRIES = 3
RETRY_DELAY = 2

def run_cmd(cmd, cwd=None):
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True, cwd=cwd)
    output = result.stdout.strip()
    if result.returncode != 0:
        return {"ok": False, "error": (result.stderr.strip() or output)[:500]}
    try:
        return json.loads(output)
    except json.JSONDecodeError:
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
            # 从返回结果中提取 file_token
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

def validate_skill_tar(path):
    if not os.path.exists(path):
        return False, None
    try:
        with tarfile.open(path, "r:gz") as tar:
            names = tar.getnames()
            if not any("SKILL.md" in n for n in names):
                return False, None
            version = None
            for n in names:
                if n.endswith("VERSION"):
                    f = tar.extractfile(n)
                    if f:
                        version = f.read().decode().strip()
                    break
            return True, version
    except Exception:
        return False, None

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

def get_local_skill_version():
    vf = os.path.join(SKILL_DIR, "VERSION")
    if not os.path.exists(vf):
        return None
    try:
        with open(vf, "r") as f:
            return f.read().strip()
    except Exception:
        return None

def parse_version(v):
    try:
        return tuple(int(x) for x in v.split("."))
    except Exception:
        return (0, 0, 0)

def compare_versions(v1, v2):
    p1, p2 = parse_version(v1), parse_version(v2)
    return -1 if p1 < p2 else (1 if p1 > p2 else 0)

def get_remote_skill_version():
    files = find_files_by_prefix(SKILL_FILE_PREFIX, SKILL_FOLDER_TOKEN)
    if not files:
        return None
    def extract_ver(fn):
        m = re.search(r'v(\d+\.\d+\.\d+)', fn)
        return m.group(1) if m else "0.0.0"
    files.sort(key=lambda x: parse_version(extract_ver(x.get("name", ""))), reverse=True)
    with tempfile.TemporaryDirectory() as tmpdir:
        tp = os.path.join(tmpdir, "skill.tar.gz")
        if download_file(files[0].get("token"), tp):
            valid, version = validate_skill_tar(tp)
            if valid:
                return version
    return None

def backup_skill_to_remote():
    version = get_local_skill_version()
    if not version:
        print("[技能同步] ❌ 本地无版本标识")
        return False
    filename = f"{SKILL_FILE_PREFIX}{version}.tar.gz"
    print(f"[技能同步] 备份版本 {version}")
    with tempfile.TemporaryDirectory() as tmpdir:
        tar_path = os.path.join(tmpdir, filename)
        try:
            with tarfile.open(tar_path, "w:gz") as tar:
                for root, dirs, files in os.walk(SKILL_DIR):
                    dirs[:] = [d for d in dirs if d != "__pycache__"]
                    for file in files:
                        if file.endswith(".pyc"):
                            continue
                        full_path = os.path.join(root, file)
                        tar.add(full_path, arcname=os.path.relpath(full_path, os.path.dirname(SKILL_DIR)))
        except Exception as e:
            print(f"[技能同步] ❌ 打包失败: {e}")
            return False
        success, _ = backup_with_verification(tar_path, filename, SKILL_FOLDER_TOKEN, validate_skill_tar, SKILL_FILE_PREFIX)
    print(f"[技能同步] {'✅ 已备份' if success else '❌ 失败'} (版本 {version})")
    return success

def restore_skill_from_remote():
    print("[技能同步] 正在恢复...")
    files = find_files_by_prefix(SKILL_FILE_PREFIX, SKILL_FOLDER_TOKEN)
    if not files:
        print("[技能同步] ❌ 未找到备份")
        return False
    def extract_ver(fn):
        m = re.search(r'v(\d+\.\d+\.\d+)', fn)
        return m.group(1) if m else "0.0.0"
    files.sort(key=lambda x: parse_version(extract_ver(x.get("name", ""))), reverse=True)
    for f in files:
        print(f"[技能同步] 尝试: {f.get('name')}")
        with tempfile.TemporaryDirectory() as tmpdir:
            tar_path = os.path.join(tmpdir, "skill.tar.gz")
            if not download_file(f.get("token"), tar_path):
                continue
            valid, version = validate_skill_tar(tar_path)
            if not valid:
                print("  损坏，尝试下一个...")
                continue
            backup_dir = SKILL_DIR + f".bak.{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            if os.path.exists(SKILL_DIR):
                shutil.copytree(SKILL_DIR, backup_dir)
            try:
                with tarfile.open(tar_path, "r:gz") as tar:
                    ed = os.path.join(tmpdir, "extracted")
                    os.makedirs(ed, exist_ok=True)
                    tar.extractall(ed)
                    es = None
                    for item in os.listdir(ed):
                        ip = os.path.join(ed, item)
                        if os.path.isdir(ip) and os.path.exists(os.path.join(ip, "SKILL.md")):
                            es = ip
                            break
                    if not es:
                        print("[技能同步] ❌ 未找到技能目录")
                        return False
                    if os.path.exists(SKILL_DIR):
                        shutil.rmtree(SKILL_DIR)
                    shutil.copytree(es, SKILL_DIR)
            except Exception as e:
                print(f"[技能同步] ❌ 解压失败: {e}")
                return False
    print(f"[技能同步] ✅ 已恢复 (版本 {get_local_skill_version()})")
    return True

def do_skill_sync():
    lv = get_local_skill_version()
    rv = get_remote_skill_version()
    print(f"[技能同步] 本地: {lv or '未知'}, 飞书: {rv or '无'}")
    if not lv:
        print("[技能同步] ⚠️  本地无版本标识，跳过")
        return True
    if not rv:
        return backup_skill_to_remote()
    cmp = compare_versions(lv, rv)
    if cmp > 0:
        return backup_skill_to_remote()
    elif cmp < 0:
        return restore_skill_from_remote()
    else:
        print("[技能同步] 版本一致，无需同步")
        return True

def cleanup_all_duplicates():
    """清理重复/损坏文件。
    
    安全措施：如果没有找到任何有效文件，不删除任何文件，防止数据丢失。
    """
    print("--- 清理重复/损坏文件 ---")
    for prefix, token, name, vfunc in [
        (DB_FILE_PREFIX, DB_FOLDER_TOKEN, "数据库", validate_db_file),
        (SKILL_FILE_PREFIX, SKILL_FOLDER_TOKEN, "技能", validate_skill_tar),
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
    parser = argparse.ArgumentParser(description="时间统计数据备份与恢复工具 v3")
    parser.add_argument("--restore", action="store_true")
    parser.add_argument("--restore-skill", action="store_true")
    parser.add_argument("--backup-skill", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--skip-skill", action="store_true")
    parser.add_argument("--cleanup", action="store_true")
    args = parser.parse_args()
    print("=" * 50)
    print("时间统计数据备份与恢复 v3")
    print("=" * 50)
    print(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()
    if args.cleanup:
        cleanup_all_duplicates()
        print("\n" + "=" * 50 + "\n清理完成\n" + "=" * 50)
        return
    if args.restore:
        sys.exit(0 if do_db_restore() else 1)
    if args.restore_skill:
        sys.exit(0 if restore_skill_from_remote() else 1)
    if args.backup_skill:
        sys.exit(0 if backup_skill_to_remote() else 1)
    print("--- 技能版本同步 ---")
    if args.skip_skill:
        print("[技能同步] 已跳过")
    else:
        do_skill_sync()
    print()
    print()
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
