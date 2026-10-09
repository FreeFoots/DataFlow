"""Run from backend: python -m scripts.backup_runtime /safe/new-backup.db"""
from __future__ import annotations

import argparse
from pathlib import Path

from app.config import settings
from app.runtime.store import TaskStore


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="在线备份任务、事件、结果和工作流 checkpoint；不覆盖已有文件")
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    if not settings.task_store_file.is_file():
        raise SystemExit("任务数据库尚不存在")
    TaskStore(settings.task_store_file).backup(args.destination)
    print(f"备份已完成并通过完整性检查：{args.destination}")
