"""数据库备份与回滚。

为什么需要：
用户会导入自己的资料。如果导入了一份错的、或者格式乱七八糟的文档，
污染了检索结果，现在只能整个删掉重来。有这个模块就能"一键退回上一次导入之前"。

安全设计（这里动的是用户数据，必须格外小心）：
1. **用 SQLite 官方的 backup API，不是简单复制文件**。
   数据库开了 WAL 模式，直接复制 app.db 会丢掉还在 -wal 里没落盘的事务。
2. **导入前先备份**，备份失败就中止导入，绝不"没备份就动手"。
3. **撤销前再备份一次当前状态**，这样撤销本身也是可撤销的。
4. **只允许撤销用户自己的导入**，不允许撤销内置种子（那是基线，撤了知识库就空了）。
5. **只允许撤销最近一次**带备份的用户导入。恢复备份会把之后的所有改动一起退掉，
   如果允许撤销任意一次，用户会以为只退了那一次，实际连带后面的全没了——这是误导。
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime
from pathlib import Path

from . import config

log = logging.getLogger(__name__)

BACKUP_DIR = config.DATA_DIR / "backups"
MAX_BACKUPS = 20          # 保留最近 20 份，再多就删最旧的


def ensure_dir() -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    return BACKUP_DIR


def create_backup(reason: str = "manual") -> Path | None:
    """把当前数据库完整备份一份。返回备份文件路径；失败返回 None。

    用 backup() API 而不是文件复制，这样 WAL 里未落盘的内容也会被包含进去。
    """
    if not config.DB_PATH.exists():
        log.warning("数据库还不存在，跳过备份")
        return None

    ensure_dir()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_reason = "".join(ch for ch in reason if ch.isalnum() or ch in "_-")[:24] or "manual"
    dest = BACKUP_DIR / f"app_{stamp}_{safe_reason}.db"

    try:
        src = sqlite3.connect(config.DB_PATH, timeout=15)
        dst = sqlite3.connect(dest)
        try:
            with dst:
                src.backup(dst)
        finally:
            src.close()
            dst.close()
    except Exception as exc:
        log.error("备份失败：%s", exc)
        if dest.exists():
            dest.unlink(missing_ok=True)
        return None

    size_kb = dest.stat().st_size / 1024
    log.info("已备份数据库 → %s（%.1f KB）", dest.name, size_kb)
    prune()
    return dest


def prune(keep: int = MAX_BACKUPS) -> int:
    """只保留最近 keep 份备份，返回删除的数量。"""
    if not BACKUP_DIR.exists():
        return 0
    files = sorted(
        (p for p in BACKUP_DIR.glob("app_*.db") if p.is_file()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    removed = 0
    for old in files[keep:]:
        try:
            old.unlink()
            removed += 1
        except Exception as exc:
            log.warning("删除旧备份失败 %s：%s", old.name, exc)
    if removed:
        log.info("清理了 %d 份旧备份", removed)
    return removed


def list_backups() -> list[dict]:
    if not BACKUP_DIR.exists():
        return []
    out = []
    for p in sorted(BACKUP_DIR.glob("app_*.db"), key=lambda x: x.stat().st_mtime, reverse=True):
        stat = p.stat()
        out.append({
            "name": p.name,
            "path": str(p),
            "size_kb": round(stat.st_size / 1024, 1),
            "created_at": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
        })
    return out


def restore_backup(backup_path: str | Path) -> tuple[bool, str]:
    """把数据库恢复到指定备份的状态。

    返回 (是否成功, 说明)。调用方负责在成功后让检索缓存失效。
    """
    path = Path(backup_path)
    if not path.exists():
        return False, f"备份文件不存在：{path.name}"

    try:
        src = sqlite3.connect(path, timeout=15)
        dst = sqlite3.connect(config.DB_PATH, timeout=15)
        try:
            with dst:
                src.backup(dst)
        finally:
            src.close()
            dst.close()
    except Exception as exc:
        log.exception("恢复备份失败")
        return False, f"恢复失败：{exc}"

    log.warning("数据库已从备份恢复：%s", path.name)
    return True, f"已恢复到备份 {path.name}"
