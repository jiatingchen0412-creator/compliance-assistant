"""知识库管理接口：看得到已加载了哪些条款、能搜、能导入、能删。"""
from __future__ import annotations

import json
import logging
import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile

from .. import config, db, guard, ingest
from ..retrieval import get_engine

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/kb", tags=["knowledge-base"])


@router.get("/stats")
def stats():
    total = db.query_one("SELECT COUNT(*) AS c FROM clauses")["c"]
    enabled = db.query_one("SELECT COUNT(*) AS c FROM clauses WHERE enabled = 1")["c"]
    with_vec = db.query_one("SELECT COUNT(*) AS c FROM clause_vectors")["c"]
    std_count = db.query_one("SELECT COUNT(*) AS c FROM standards")["c"]

    by_risk: dict[str, int] = {}
    for row in db.query("SELECT risk_tags FROM clauses WHERE enabled = 1"):
        for tag in db.load_json(row["risk_tags"]):
            by_risk[tag] = by_risk.get(tag, 0) + 1

    by_standard: dict[str, int] = {}
    for row in db.query(
        "SELECT s.name AS name, COUNT(c.id) AS c FROM standards s "
        "LEFT JOIN clauses c ON c.standard_id = s.id GROUP BY s.id"
    ):
        by_standard[row["name"]] = row["c"]

    # 来源统计：内置种子 vs 用户导入
    by_source: dict[str, int] = {}
    for row in db.query(
        "SELECT COALESCE(source_kind, 'builtin') AS k, COUNT(*) AS c FROM clauses GROUP BY k"
    ):
        by_source[row["k"]] = row["c"]

    engine = get_engine()
    return {
        "standards": std_count,
        "clauses": total,
        "enabled_clauses": enabled,
        "with_vector": with_vec,
        "by_risk": by_risk,
        "by_standard": by_standard,
        "by_source": by_source,
        "batches": db.query_one("SELECT COUNT(*) AS c FROM import_batches")["c"],
        "vector_backend": "fastembed (ONNX)" if engine.vector_available else "未启用",
        "vector_model": "BAAI/bge-small-zh-v1.5" if engine.vector_available else "",
        "min_score": config.RETRIEVAL_MIN_SCORE,
        "keyword_weight": config.HYBRID_KEYWORD_WEIGHT,
        "vector_weight": config.HYBRID_VECTOR_WEIGHT,
        "last_index": db.get_setting("index_version", "尚未建立"),
    }


@router.get("/standards")
def list_standards():
    rows = db.query(
        """
        SELECT s.*,
               (SELECT COUNT(*) FROM clauses c WHERE c.standard_id = s.id) AS clause_count,
               (SELECT COUNT(*) FROM clauses c WHERE c.standard_id = s.id AND c.enabled = 1) AS enabled_count
        FROM standards s ORDER BY s.id
        """
    )
    return {"standards": [dict(r) for r in rows]}


@router.get("/clauses")
def list_clauses(
    standard_id: str | None = None,
    q: str | None = None,
    risk_tag: str | None = None,
    level: str | None = None,
    chapter: str | None = None,
    page: int = 1,
    page_size: int = 30,
):
    page = max(1, page)
    page_size = max(1, min(200, page_size))
    where = ["1=1"]
    params: list = []
    if standard_id:
        where.append("standard_id = ?")
        params.append(standard_id)
    if q:
        like = f"%{q.strip()}%"
        where.append(
            "(clause_id LIKE ? OR title LIKE ? OR summary LIKE ? OR text LIKE ? OR keywords LIKE ?)"
        )
        params.extend([like] * 5)
    if risk_tag:
        where.append("risk_tags LIKE ?")
        params.append(f'%"{risk_tag}"%')
    if level:
        where.append("level_scope LIKE ?")
        params.append(f'%"{level}"%')
    if chapter:
        where.append("chapter_path LIKE ?")
        params.append(f"%{chapter}%")

    clause = " AND ".join(where)
    total = db.query_one(f"SELECT COUNT(*) AS c FROM clauses WHERE {clause}", params)["c"]
    rows = db.query(
        f"SELECT * FROM clauses WHERE {clause} ORDER BY standard_id, sort_key "
        f"LIMIT ? OFFSET ?",
        params + [page_size, (page - 1) * page_size],
    )

    # 把标准信息一起带出来，界面上要显示来源和官方链接
    std_map = {r["id"]: dict(r) for r in db.query("SELECT * FROM standards")}
    batch_map = {r["id"]: dict(r) for r in db.query("SELECT * FROM import_batches")}

    items = []
    for r in rows:
        item = db.clause_to_dict(r)
        std = std_map.get(item["standard_id"], {})
        item["standard_name"] = std.get("name", item["standard_id"])
        item["standard_version"] = std.get("version", "")
        item["source_url"] = std.get("source_url", "")
        item["license"] = std.get("license", "")
        batch = batch_map.get(item.get("batch_id")) if item.get("batch_id") else None
        if batch:
            item["batch_filename"] = batch.get("filename", "")
            item["batch_created_at"] = batch.get("created_at", "")
        items.append(item)

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": items,
    }


@router.get("/batches")
def list_batches(limit: int = 50):
    """导入批次列表：每次导入的来源、条数、时间，用于追溯。"""
    rows = db.query(
        "SELECT b.*, "
        "(SELECT COUNT(*) FROM clauses c WHERE c.batch_id = b.id) AS clause_count "
        "FROM import_batches b ORDER BY b.id DESC LIMIT ?",
        (max(1, min(200, limit)),),
    )
    return {"batches": [dict(r) for r in rows]}


@router.get("/clauses/{rowid}")
def get_clause(rowid: int):
    row = db.query_one("SELECT * FROM clauses WHERE id = ?", (rowid,))
    if not row:
        raise HTTPException(status_code=404, detail="条款不存在")
    item = db.clause_to_dict(row)
    std = db.query_one("SELECT * FROM standards WHERE id = ?", (item["standard_id"],))
    if std:
        item["standard_name"] = std["name"]
        item["standard_version"] = std["version"]
        item["source_url"] = std["source_url"]
        item["license"] = std["license"]
        item["standard_note"] = std["note"]
    return item


@router.get("/tree")
def chapter_tree(standard_id: str | None = None):
    """按标准 → 一级章节 → 二级章节返回条款数量，用于左侧目录树。"""
    where = "WHERE enabled = 1"
    params: list = []
    if standard_id:
        where += " AND standard_id = ?"
        params.append(standard_id)
    rows = db.query(
        f"SELECT standard_id, clause_id, title, chapter_path, risk_tags, level_scope "
        f"FROM clauses {where} ORDER BY standard_id, sort_key",
        params,
    )
    tree: dict[str, dict] = {}
    # 标准名映射：有些标准的 chapter_path 第一层就是标准名本身（例如 OWASP），
    # 直接用它当一级章节会出现"OWASP > OWASP"这种重复，这里把它剥掉。
    std_names = {r["id"]: r["name"] for r in db.query("SELECT id, name FROM standards")}

    for r in rows:
        path = db.load_json(r["chapter_path"]) or ["未分类"]
        std = r["standard_id"]
        std_name = std_names.get(std, "")
        if len(path) > 1 and std_name and (path[0] == std_name or std_name in path[0]):
            path = path[1:]
        node = tree.setdefault(std, {"standard_id": std, "children": {}, "count": 0})
        node["count"] += 1
        level1 = path[0] if len(path) > 0 else "未分类"
        level2 = path[1] if len(path) > 1 else ""
        b1 = node["children"].setdefault(level1, {"name": level1, "count": 0, "children": {}})
        b1["count"] += 1
        if level2:
            b2 = b1["children"].setdefault(level2, {"name": level2, "count": 0})
            b2["count"] += 1

    out = []
    for std, node in tree.items():
        meta = db.query_one("SELECT name FROM standards WHERE id = ?", (std,))
        out.append({
            "standard_id": std,
            "standard_name": meta["name"] if meta else std,
            "count": node["count"],
            "children": [
                {
                    "name": c["name"],
                    "count": c["count"],
                    "children": [{"name": g["name"], "count": g["count"]} for g in c["children"].values()],
                }
                for c in node["children"].values()
            ],
        })
    return {"tree": out}


@router.put("/clauses/{rowid}/enabled")
def toggle_clause(rowid: int, body: dict):
    enabled = 1 if body.get("enabled", True) else 0
    db.execute("UPDATE clauses SET enabled = ? WHERE id = ?", (enabled, rowid))
    db.set_setting("index_version", db.now())
    return {"ok": True, "enabled": bool(enabled)}


@router.delete("/standards/{standard_id}")
def delete_standard(standard_id: str):
    rows = db.query("SELECT id FROM clauses WHERE standard_id = ?", (standard_id,))
    ids = [r["id"] for r in rows]
    with db.get_conn() as conn:
        if ids:
            conn.executemany("DELETE FROM clauses_fts WHERE rowid = ?", [(i,) for i in ids])
            conn.executemany("DELETE FROM clause_vectors WHERE clause_rowid = ?", [(i,) for i in ids])
        conn.execute("DELETE FROM clauses WHERE standard_id = ?", (standard_id,))
        conn.execute("DELETE FROM standards WHERE id = ?", (standard_id,))
    db.set_setting("index_version", db.now())
    return {"ok": True, "deleted_clauses": len(ids)}


@router.post("/rebuild")
def rebuild():
    result = ingest.rebuild_index()
    return {"ok": True, **result}


@router.get("/risk-terms")
def risk_terms():
    meta = guard.risk_meta()
    data = guard.load_risk_terms()
    return {
        "tags": list(meta.values()),
        "high_risk_tags": list(config.HIGH_RISK_TAGS),
        "disclaimer": data.get("disclaimer", ""),
    }


@router.get("/import-log")
def import_log(limit: int = 20):
    rows = db.query(
        "SELECT * FROM import_log ORDER BY id DESC LIMIT ?", (max(1, min(100, limit)),)
    )
    return {"logs": [dict(r) for r in rows]}


@router.post("/import")
async def import_file(file: UploadFile = File(...)):
    """导入标准 JSON 文件，或 PDF/Word/Markdown/TXT 自查资料。

    JSON 走标准导入（含条款编号）；
    其他格式走文本切分，按"章节/条款"启发式切条，编号自动生成，标注为摘要。

    **导入前一定会先备份数据库**：备份失败就中止导入，绝不"没备份就动手"。
    """
    suffix = Path(file.filename or "").suffix.lower()
    tmp_dir = Path(tempfile.mkdtemp(prefix="kb_import_"))
    tmp_path = tmp_dir / (file.filename or f"upload{suffix or '.txt'}")

    # 先备份，再动手。这是本步骤最重要的一条安全约束。
    from .. import backup as backup_mod

    backup_file = backup_mod.create_backup("before_import")
    if backup_file is None:
        raise HTTPException(
            status_code=500,
            detail="导入前的自动备份失败，为避免数据丢失已中止导入。请检查磁盘空间后重试。",
        )

    try:
        with open(tmp_path, "wb") as f:
            shutil.copyfileobj(file.file, f)

        if suffix == ".json":
            result = ingest.import_standard_file(tmp_path, kind="json", source_kind="user")
        else:
            result = ingest.import_document(tmp_path)

        ingest.rebuild_index()

        # 把备份挂到批次上，之后才能"一键撤销"
        batch_id = result.get("batch_id")
        if batch_id:
            db.execute(
                "UPDATE import_batches SET backup_path = ? WHERE id = ?",
                (str(backup_file), batch_id),
            )

        log.info(
            "导入完成 · 文件=%s 新增=%s 更新=%s 跳过=%s · 备份=%s",
            file.filename, result.get("added"), result.get("updated"),
            result.get("skipped"), backup_file.name,
        )
        return {
            "ok": True,
            **result,
            "backup": backup_file.name,
            "can_revert": bool(batch_id),
        }
    except Exception as exc:
        log.exception("导入失败 · 文件=%s", file.filename)
        raise HTTPException(
            status_code=400,
            detail=f"导入失败：{exc}。数据库未受影响（导入前已备份：{backup_file.name}）。",
        ) from exc
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


@router.get("/backups")
def list_backups():
    """已有的数据库备份列表。"""
    from .. import backup as backup_mod

    return {"backups": backup_mod.list_backups(), "dir": str(backup_mod.BACKUP_DIR)}


@router.post("/batches/{batch_id}/revert")
def revert_batch(batch_id: int):
    """撤销某一次导入，把知识库退回到那次导入之前的状态。

    安全约束（每一条都有理由，不要放宽）：
    1. 只能撤销**用户自己导入**的批次。内置种子是基线，撤了知识库就空了。
    2. 只能撤销**最近一次**带备份的用户导入。恢复备份会把之后的改动一起退掉，
       允许撤销任意一次会让用户以为"只退了那一次"，是误导。
    3. 撤销前**再备份一次当前状态**，这样撤销本身也可撤销。
    4. 备份文件缺失时拒绝执行，不做"没有退路"的操作。
    """
    from .. import backup as backup_mod
    from ..retrieval import reset_engine_cache

    batch = db.query_one("SELECT * FROM import_batches WHERE id = ?", (batch_id,))
    if not batch:
        raise HTTPException(status_code=404, detail="找不到这个导入批次")

    if batch["reverted"]:
        raise HTTPException(status_code=400, detail="这一批已经撤销过了")
    if batch["source_kind"] != "user":
        raise HTTPException(
            status_code=400,
            detail="内置种子条款是知识库的基线，不允许撤销。如需移除请用『删除标准』功能。",
        )
    if not batch["backup_path"]:
        raise HTTPException(
            status_code=400,
            detail="这一批导入时没有留下备份，无法撤销。",
        )

    latest = db.query_one(
        "SELECT id FROM import_batches "
        "WHERE source_kind = 'user' AND reverted = 0 AND backup_path IS NOT NULL "
        "ORDER BY id DESC LIMIT 1"
    )
    if not latest or latest["id"] != batch_id:
        raise HTTPException(
            status_code=400,
            detail="只能撤销最近一次导入。撤销更早的批次会连带退掉后面的所有改动，容易造成误解。",
        )

    # 撤销前先给当前状态留一份，保证撤销本身也可以再撤回来
    safety = backup_mod.create_backup(f"before_revert_{batch_id}")
    if safety is None:
        raise HTTPException(status_code=500, detail="撤销前的安全备份失败，已中止操作。")

    # 记住批次信息：备份是在创建批次记录**之前**做的，
    # 恢复备份会把这条记录一起抹掉。不补回来的话，用户看不到"这批被撤销过"，
    # 重复撤销的拦截也会失效（变成 404 而不是明确的"已撤销过"）。
    snapshot = dict(batch)

    before_count = db.query_one("SELECT COUNT(*) AS c FROM clauses")["c"]
    ok, message = backup_mod.restore_backup(batch["backup_path"])
    if not ok:
        raise HTTPException(status_code=500, detail=f"{message}（当前状态已备份为 {safety.name}）")

    # 恢复过来的库可能缺后来新增的列，重新确认一次表结构
    db.init_db()
    reset_engine_cache()

    revert_note = (
        f"已于 {db.now()} 撤销。该批次当时新增 {snapshot['added']} 条、"
        f"更新 {snapshot['updated']} 条；撤销前的状态已备份为 {safety.name}。"
    )
    ingest.restore_batch_record(snapshot, reverted=1, note=revert_note)
    after_count = db.query_one("SELECT COUNT(*) AS c FROM clauses")["c"]

    log.warning(
        "已撤销导入批次 #%s（%s）· 条款数 %s → %s · 安全备份 %s",
        batch_id, batch["filename"], before_count, after_count, safety.name,
    )
    return {
        "ok": True,
        "message": f"已撤销批次 #{batch_id}（{batch['filename']}）",
        "clauses_before": before_count,
        "clauses_after": after_count,
        "safety_backup": safety.name,
    }


@router.get("/export")
def export_seed():
    """把当前知识库导出成种子 JSON（方便备份或换电脑搬走）。"""
    standards = db.query("SELECT * FROM standards")
    payload = {"exported_at": db.now(), "standards": []}
    for std in standards:
        rows = db.query(
            "SELECT * FROM clauses WHERE standard_id = ? ORDER BY sort_key", (std["id"],)
        )
        payload["standards"].append({
            "standard": dict(std),
            "clauses": [db.clause_to_dict(r) for r in rows],
        })
    return payload
