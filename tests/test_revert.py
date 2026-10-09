"""知识库导入与撤销的自动化测试（P2-2）。

这是唯一会改动用户数据的功能，所以必须有自动化保护。

安全做法：**全程在临时数据库上跑**——先把真实的 app.db 复制一份到临时目录，
再把 config.DB_PATH 和备份目录指向临时位置。真实数据一个字节都不会被碰。

用法（在项目根目录执行）：
    .venv\\Scripts\\python.exe tests\\test_revert.py
"""
from __future__ import annotations

import asyncio
import io
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi import HTTPException, UploadFile  # noqa: E402

from app import backup, config, db, ingest  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def case(name: str):
    def wrapper(fn):
        RESULTS.append((name, fn))
        return fn
    return wrapper


SAMPLE_DOC = """# 测试用安全管理办法

## 2.1 账号管理

所有系统账号必须一人一号，禁止共用。账号开通需审批并留存记录。

## 2.2 备份要求

核心数据每日增量备份，每周全量备份，每季度做一次恢复演练。
"""


# --------------------------------------------------------------------------
# 这些测试共享一个临时环境，由 main() 里的 setup 准备
# --------------------------------------------------------------------------
ENV: dict = {}


@case("导入会先自动备份数据库")
def test_backup_created():
    before = len(backup.list_backups())
    result = asyncio.run(_do_import())
    ENV["batch_id"] = result.get("batch_id")
    ENV["standard_id"] = result.get("standard_id")
    after = len(backup.list_backups())
    return (
        (after > before and result.get("backup"), f"备份文件：{result.get('backup')}")
        if after > before
        else (False, f"没有创建备份（{before} → {after}）")
    )


@case("导入的条款标记为用户导入并关联批次")
def test_clauses_tagged():
    batch_id = ENV.get("batch_id")
    rows = db.query(
        "SELECT source_kind, COUNT(*) AS c FROM clauses WHERE batch_id = ? GROUP BY source_kind",
        (batch_id,),
    )
    if not rows:
        return False, f"批次 #{batch_id} 下没有条款"
    kinds = {r["source_kind"]: r["c"] for r in rows}
    return (
        (kinds.get("user", 0) > 0, f"批次 #{batch_id} 下有 {kinds.get('user', 0)} 条 user 条款")
        if kinds.get("user", 0) > 0
        else (False, f"来源标记不对：{kinds}")
    )


@case("导入后条款数增加")
def test_count_increased():
    baseline = ENV.get("baseline_count", 0)
    now = db.query_one("SELECT COUNT(*) AS c FROM clauses")["c"]
    ENV["after_import_count"] = now
    return (
        (now > baseline, f"{baseline} → {now}")
        if now > baseline
        else (False, f"条款数没有增加：{baseline} → {now}")
    )


@case("导入的条款能被检索到")
def test_retrievable():
    from app.retrieval import get_engine, reset_engine_cache

    reset_engine_cache()
    result = get_engine().search("账号必须一人一号禁止共用", top_k=5)
    hit = any(c.get("source_kind") == "user" for c in result["results"])
    ids = [c["clause_id"] for c in result["results"][:3]]
    return (
        (hit, f"检索命中用户导入的条款，前 3 条：{ids}")
        if hit
        else (False, f"没能检索到用户导入的条款，前 3 条：{ids}")
    )


@case("内置种子批次不允许撤销")
def test_cannot_revert_seed():
    seed = db.query_one("SELECT id FROM import_batches WHERE source_kind='builtin' ORDER BY id LIMIT 1")
    if not seed:
        return True, "（当前没有内置批次可测，跳过）"
    try:
        asyncio.run(_call_revert(seed["id"]))
    except HTTPException as exc:
        return (
            (exc.status_code == 400, f"正确拒绝：{exc.detail[:40]}")
            if exc.status_code == 400
            else (False, f"状态码异常 {exc.status_code}")
        )
    return False, "内置批次的撤销没有被拒绝"


@case("撤销导入后条款数回到导入前")
def test_revert_restores():
    batch_id = ENV["batch_id"]
    baseline = ENV["baseline_count"]
    try:
        result = asyncio.run(_call_revert(batch_id))
    except HTTPException as exc:
        return False, f"撤销失败：{exc.detail}"
    now = db.query_one("SELECT COUNT(*) AS c FROM clauses")["c"]
    ENV["after_revert_count"] = now
    return (
        (now == baseline, f"{result['clauses_before']} → {now}（导入前是 {baseline}）")
        if now == baseline
        else (False, f"没有回到导入前：期望 {baseline}，实际 {now}")
    )


@case("撤销前会再备份一次当前状态（撤销本身也可撤销）")
def test_safety_backup():
    names = [b["name"] for b in backup.list_backups()]
    found = [n for n in names if "before_revert" in n]
    return (
        (bool(found), f"找到安全备份：{found[0]}")
        if found
        else (False, f"没有找到 before_revert 备份，现有：{names[:3]}")
    )


@case("批次被标记为已撤销")
def test_batch_marked():
    row = db.query_one("SELECT reverted FROM import_batches WHERE id = ?", (ENV["batch_id"],))
    return (
        (bool(row and row["reverted"]), "已标记 reverted = 1")
        if row and row["reverted"]
        else (False, "批次没有被标记为已撤销")
    )


@case("撤销后检索结果不再包含被撤销的条款")
def test_not_retrievable_after_revert():
    from app.retrieval import get_engine, reset_engine_cache

    reset_engine_cache()
    result = get_engine().search("账号必须一人一号禁止共用", top_k=5)
    user_hits = [c["clause_id"] for c in result["results"] if c.get("source_kind") == "user"]
    return (
        (not user_hits, "检索结果里已经没有用户导入的条款")
        if not user_hits
        else (False, f"仍能检索到被撤销的条款：{user_hits}")
    )


@case("同一个批次不能撤销两次")
def test_cannot_revert_twice():
    try:
        asyncio.run(_call_revert(ENV["batch_id"]))
    except HTTPException as exc:
        return (
            (exc.status_code == 400, f"正确拒绝：{exc.detail[:40]}")
            if exc.status_code == 400
            else (False, f"状态码异常 {exc.status_code}")
        )
    return False, "重复撤销没有被拒绝"


# --------------------------------------------------------------------------
# 辅助
# --------------------------------------------------------------------------
async def _do_import() -> dict:
    from app.routers import kb as kb_router

    upload = UploadFile(
        file=io.BytesIO(SAMPLE_DOC.encode("utf-8")),
        filename="测试用安全管理办法.md",
    )
    return await kb_router.import_file(upload)


async def _call_revert(batch_id: int) -> dict:
    from app.routers import kb as kb_router

    return kb_router.revert_batch(batch_id)


def setup_temp_env(workdir: Path) -> None:
    """把数据库和备份目录都指向临时位置，确保不碰真实数据。"""
    real_db = config.DB_PATH
    if not real_db.exists():
        raise SystemExit("找不到 data/app.db，请先启动一次服务完成初始化。")

    tmp_db = workdir / "app.db"
    shutil.copy(real_db, tmp_db)

    config.DB_PATH = tmp_db
    backup.BACKUP_DIR = workdir / "backups"
    db.init_db()

    ENV["baseline_count"] = db.query_one("SELECT COUNT(*) AS c FROM clauses")["c"]


def main() -> int:
    print("=" * 76)
    print("知识库导入与撤销测试 · 全程在临时数据库上运行，不碰真实数据")
    print("=" * 76)

    with tempfile.TemporaryDirectory(prefix="kb_revert_test_") as tmp:
        workdir = Path(tmp)
        try:
            setup_temp_env(workdir)
        except SystemExit as exc:
            print(exc)
            return 1

        print(f"临时数据库：{config.DB_PATH}")
        print(f"临时备份目录：{backup.BACKUP_DIR}")
        print(f"基线条款数：{ENV['baseline_count']}")
        print()

        passed = failed = 0
        for name, fn in RESULTS:
            try:
                ok, msg = fn()
            except Exception as exc:
                ok, msg = False, f"测试本身出错：{type(exc).__name__}: {exc}"
            print(f"{'OK ' if ok else 'XX '}{name}")
            print(f"     {msg}")
            if ok:
                passed += 1
            else:
                failed += 1

        print("=" * 76)
        print(f"通过 {passed} / {passed + failed}")
        print("临时环境已清理，真实数据库未被改动。")
        return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
