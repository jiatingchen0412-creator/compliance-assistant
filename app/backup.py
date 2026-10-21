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


def _verify_db_file(path: Path) -> tuple[bool, str]:
    """检查一个 .db 文件是不是完整、可用的本应用数据库。

    为什么要有这一步：备份文件可能是半截的（磁盘满、进程被杀、用户手动
    复制了正在写的库）。以前直接把半截文件 backup() 进活库，等于用坏数据
    覆盖好数据，而且备份文件本身没被动过——用户以为"恢复"是安全的。
    """
    try:
        conn = sqlite3.connect(path, timeout=10)
    except Exception as exc:
        return False, f"打不开：{exc}"
    try:
        row = conn.execute("PRAGMA integrity_check").fetchone()
        verdict = str(row[0]) if row else "unknown"
        if verdict.lower() != "ok":
            return False, f"完整性检查未通过（{verdict[:80]}）"
        names = {
            str(r[0])
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        missing = [t for t in ("standards", "clauses") if t not in names]
        if missing:
            return False, f"缺少必要的数据表（{'、'.join(missing)}）"
        count = conn.execute("SELECT COUNT(*) FROM clauses").fetchone()[0]
        if not count:
            return False, "条款表是空的"
    except Exception as exc:
        return False, f"读取失败：{exc}"
    finally:
        conn.close()
    return True, f"完整，{count} 条条款"


def restore_backup(backup_path: str | Path) -> tuple[bool, str]:
    """把数据库恢复到指定备份的状态。

    返回 (是否成功, 说明)。**成功与否不再只看有没有抛异常**：
      1. 先校验备份文件本身（integrity_check + 必要表 + 条款非空），
         坏文件一律不碰活库；
      2. 恢复前把当前状态再备份一份，这样"恢复"本身也能退回去；
      3. 用 SQLite 官方 backup API 写入（不是复制文件，WAL 才不会丢）；
      4. 恢复后再校验一次活库，失败就自动退回第 2 步那份。

    刻意不用"写到临时文件再 os.replace"：Windows 上活库可能被其他连接打开，
    replace 会失败；而且 app.db 还有 -wal / -shm 两个附属文件，只替换主文件
    会留下不匹配的 WAL。用官方 backup API 是 SQLite 文档给的做法。
    """
    path = Path(backup_path)
    if not path.exists():
        return False, f"备份文件不存在：{path.name}"

    ok, why = _verify_db_file(path)
    if not ok:
        log.error("拒绝恢复损坏的备份 %s：%s", path.name, why)
        return False, f"这份备份不可用（{why}），当前数据库没有改动"

    safety = create_backup("before_restore")
    if safety is None:
        return False, "恢复前无法备份当前数据库，已中止（不留半完成状态）"

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
        return False, f"恢复失败：{exc}（恢复前的状态已存为 {safety.name}）"

    after_ok, after_why = _verify_db_file(config.DB_PATH)
    if not after_ok:
        log.error("恢复后活库校验不通过，正在退回恢复前状态：%s", after_why)
        back_ok, back_why = restore_backup(safety)
        if back_ok:
            return False, f"恢复后校验失败（{after_why}），已自动退回恢复前状态"
        return False, (
            f"恢复后校验失败（{after_why}），且退回也失败（{back_why}）；"
            f"请手动用 {safety} 恢复"
        )

    log.warning("数据库已从备份恢复：%s（%s）", path.name, after_why)
    return True, f"已恢复到备份 {path.name}（{after_why}）"
