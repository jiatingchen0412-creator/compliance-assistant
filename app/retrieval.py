"""混合检索：关键词（BM25 + 词覆盖）⊕ 语义向量。

为什么不用单一方案：
- 只用关键词：用户说"员工共用一个账号"，条款里写的是"身份鉴别"，字面不匹配 → 搜不到。
- 只用向量：专有名词（"8.1.4.1""CWE-89""PR.AA-01"）容易被向量模型忽略 → 搜不准。

所以两路一起上，各自转成 0~1 的**绝对分数**后加权融合。
关键点：分数是绝对的，不是"结果集内归一化"，否则阈值永远不会触发，
         也就无法可靠地判断"知识库里到底有没有"。

如果语义模型没下载成功，自动降级为纯关键词，功能不中断。
"""
from __future__ import annotations

import logging
import math
import struct
import threading
from typing import Any

from . import config, db
from .normalize import expand_query, tokenize

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# 分数标定常数
# 实测 BAAI/bge-small-zh-v1.5 在中文短问句 vs 长条款文本上的余弦分布：
#     真正相关的条款  ≈ 0.40 ~ 0.62
#     完全无关的内容  ≈ 0.18 ~ 0.25
# 所以把有效区间定在 0.15 ~ 0.58：
#     cos 0.15 以下 → 0 分（判为不相关）
#     cos 0.58 以上 → 1 分
# 这两个值是绝对标定（不是结果集内归一化），阈值才有意义。
# 第 2 步评测时会用真实问答集再校准一次。
# --------------------------------------------------------------------------
VEC_FLOOR = 0.15
VEC_CEIL = 0.58

VECTOR_MODEL_NAME = "BAAI/bge-small-zh-v1.5"

_engine_lock = threading.Lock()
_engine: "RetrievalEngine | None" = None


def _clip01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _vec_to_blob(vec) -> bytes:
    return struct.pack(f"<{len(vec)}f", *[float(x) for x in vec])


def _blob_to_vec(blob: bytes):
    count = len(blob) // 4
    return struct.unpack(f"<{count}f", blob)


class RetrievalEngine:
    """条款检索。进程内单例，向量常驻内存（条款量级几百到几万都很轻）。"""

    def __init__(self) -> None:
        self._embedder: Any = None
        self._embedder_failed = False
        self._matrix = None            # numpy 矩阵 (n_clauses, dim)
        self._matrix_rowids: list[int] = []
        self._loaded_version: str = ""
        self._standards: dict[str, dict] = {}
        self._standards_version: str = ""
        self._df: dict[str, int] | None = None
        self._df_version: str = ""
        self._lock = threading.Lock()

    # ---------------- 语义模型 ----------------
    @property
    def vector_available(self) -> bool:
        return config.ENABLE_VECTOR_SEARCH and not self._embedder_failed

    def _get_embedder(self):
        if not config.ENABLE_VECTOR_SEARCH or self._embedder_failed:
            return None
        if self._embedder is not None:
            return self._embedder
        with self._lock:
            if self._embedder is not None:
                return self._embedder
            try:
                from fastembed import TextEmbedding

                self._embedder = TextEmbedding(
                    VECTOR_MODEL_NAME, cache_dir=str(config.VECTOR_CACHE_DIR)
                )
            except Exception as exc:  # 下载失败/离线/依赖缺失 都不应该让程序崩
                log.warning("语义模型不可用，已降级为纯关键词检索：%s", exc)
                self._embedder_failed = True
                return None
        return self._embedder

    def warmup(self) -> bool:
        """预加载模型和向量，让第一次提问不卡。返回语义检索是否可用。"""
        ok = self._get_embedder() is not None
        if ok:
            try:
                self._ensure_vector_matrix(force=True)
            except Exception as exc:
                log.warning("向量索引加载失败：%s", exc)
                ok = False
        return ok

    def embed(self, texts: list[str]):
        embedder = self._get_embedder()
        if embedder is None:
            return []
        return [list(v) for v in embedder.embed(texts)]

    # ---------------- 向量矩阵 ----------------
    def _index_version(self) -> str:
        return db.get_setting("index_version", "0") or "0"

    def _ensure_vector_matrix(self, force: bool = False):
        version = self._index_version()
        if not force and self._matrix is not None and version == self._loaded_version:
            return self._matrix

        import numpy as np

        rows = db.query("SELECT clause_rowid, dim, vec FROM clause_vectors")
        if not rows:
            self._matrix = None
            self._matrix_rowids = []
            self._loaded_version = version
            return None

        rowids: list[int] = []
        vectors: list[tuple] = []
        for r in rows:
            rowids.append(r["clause_rowid"])
            vectors.append(_blob_to_vec(r["vec"]))
        matrix = np.asarray(vectors, dtype="float32")
        # L2 归一化，之后点积即余弦相似度
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        matrix = matrix / norms

        self._matrix = matrix
        self._matrix_rowids = rowids
        self._loaded_version = version
        return matrix

    def _vector_scores(self, question: str) -> dict[int, float]:
        if not self.vector_available:
            return {}
        try:
            matrix = self._ensure_vector_matrix()
            if matrix is None:
                return {}
            vecs = self.embed([question])
            if not vecs:
                return {}
            import numpy as np

            q = np.asarray(vecs[0], dtype="float32")
            norm = float(np.linalg.norm(q)) or 1.0
            q = q / norm
            sims = matrix @ q
            span = max(VEC_CEIL - VEC_FLOOR, 1e-6)
            out: dict[int, float] = {}
            for rowid, sim in zip(self._matrix_rowids, sims):
                out[int(rowid)] = _clip01((float(sim) - VEC_FLOOR) / span)
            return out
        except Exception as exc:
            log.warning("向量检索失败，本轮降级为关键词：%s", exc)
            return {}

    # ---------------- 关键词 ----------------
    def _df_map(self) -> dict[str, int]:
        """词 -> 文档频率。随索引版本失效。"""
        version = self._index_version()
        if self._df is not None and version == self._df_version:
            return self._df
        rows = db.query("SELECT token, df FROM token_df")
        self._df = {r["token"]: int(r["df"]) for r in rows}
        self._df_version = version
        return self._df

    def _clause_count(self) -> int:
        row = db.query_one("SELECT COUNT(*) AS c FROM clauses WHERE enabled = 1")
        return max(1, int(row["c"]) if row else 1)

    @staticmethod
    def _idf(token: str, df_map: dict[str, int], total: int) -> float:
        """逆文档频率。

        df=0（语料里从没出现过）给最高权重——它会拉高分母、压低总分，
        这正是我们想要的：问了一堆知识库里根本没有的词，得分就该低。
        """
        df = df_map.get(token, 0)
        if df <= 0:
            return math.log(1 + total)
        return math.log(1 + total / df)

    def _fts_candidates(self, tokens: list[str]) -> set[int]:
        """用 FTS5 找出候选条款（只要候选集合，不参与打分）。"""
        if not tokens:
            return set()
        safe = [t.replace('"', "") for t in tokens if t.replace('"', "")]
        if not safe:
            return set()
        match_expr = " OR ".join(f'"{t}"' for t in safe)
        try:
            rows = db.query(
                "SELECT rowid AS rid FROM clauses_fts WHERE clauses_fts MATCH ? LIMIT 500",
                (match_expr,),
            )
        except Exception as exc:
            log.warning("全文检索失败：%s", exc)
            return set()
        return {int(r["rid"]) for r in rows}

    def _keyword_scores(self, question: str, tokens: list[str]) -> dict[int, float]:
        """关键词分 = IDF 加权覆盖率。

        为什么不用 BM25 直接当分数：BM25 的量纲依赖于结果集，
        必须做"结果集内归一化"，而归一化之后**第一名永远是满分**，
        于是"今天天气"这种无关问题也能拿到 0.4 分，阈值就形同虚设。
        IDF 覆盖率是绝对值：虚词（的、我们）权重接近 0，专业词权重高，
        问一堆语料里没有的词时分母大、分子小，得分自然很低。
        """
        if not tokens:
            return {}

        total = self._clause_count()
        df_map = self._df_map()

        # 虚词（出现在超过一半条款里的词）不参与召回，否则候选集全是噪声
        content_tokens = [t for t in tokens if df_map.get(t, 0) <= 0.5 * total]
        recall_tokens = content_tokens or tokens

        candidate_ids = self._fts_candidates(recall_tokens)
        if not candidate_ids:
            return {}

        placeholders = ",".join("?" for _ in candidate_ids)
        rows = db.query(
            f"SELECT rowid AS rid, tokens FROM clauses_fts WHERE rowid IN ({placeholders})",
            list(candidate_ids),
        )

        token_idf = {t: self._idf(t, df_map, total) for t in tokens}
        denominator = sum(token_idf.values()) or 1.0

        scores: dict[int, float] = {}
        for r in rows:
            clause_tokens = set(str(r["tokens"]).split())
            matched = 0.0
            for t in tokens:
                if t in clause_tokens:
                    matched += token_idf[t]
                elif len(t) >= 2 and any(
                    t in ct or ct in t for ct in clause_tokens if len(ct) >= 2
                ):
                    # 部分匹配（例如查询"账号共享"对上条款里的"账号"+"共享"）打 6 折
                    matched += 0.6 * token_idf[t]
            scores[int(r["rid"])] = _clip01(matched / denominator)
        return scores

    # ---------------- 标准元信息 ----------------
    def _standards_map(self) -> dict[str, dict]:
        version = db.get_setting("standards_version", "0") or "0"
        if self._standards and version == self._standards_version:
            return self._standards
        rows = db.query("SELECT * FROM standards")
        self._standards = {r["id"]: dict(r) for r in rows}
        self._standards_version = version
        return self._standards

    # ---------------- 主入口 ----------------
    def search(self, question: str, top_k: int | None = None) -> dict:
        """返回 {results: [...], best_score: float, vector_used: bool, query_terms: [...]}"""
        top_k = top_k or config.RETRIEVAL_TOP_K
        question = (question or "").strip()
        if not question:
            return {"results": [], "best_score": 0.0, "vector_used": False, "query_terms": []}

        base_tokens = tokenize(question).split()
        extra_terms = expand_query(question)
        # 口语扩展词权重低一些，靠 BM25 自然体现；这里放进同一路检索
        all_tokens = list(dict.fromkeys(base_tokens + extra_terms))

        kw_scores = self._keyword_scores(question, all_tokens)
        vec_scores = self._vector_scores(question)
        vector_used = bool(vec_scores)

        w_kw = config.HYBRID_KEYWORD_WEIGHT
        w_vec = config.HYBRID_VECTOR_WEIGHT
        if not vector_used:
            # 纯关键词模式：权重全部给关键词，保证阈值语义一致
            w_kw, w_vec = 1.0, 0.0
        total_w = (w_kw + w_vec) or 1.0

        candidates = set(kw_scores) | set(vec_scores)
        if not candidates:
            return {"results": [], "best_score": 0.0, "vector_used": vector_used,
                    "query_terms": all_tokens}

        fused: dict[int, dict] = {}
        for rid in candidates:
            k = kw_scores.get(rid, 0.0)
            v = vec_scores.get(rid, 0.0)
            score = (w_kw * k + w_vec * v) / total_w
            fused[rid] = {"score": score, "keyword": k, "vector": v}

        order = sorted(fused.items(), key=lambda kv: kv[1]["score"], reverse=True)[:max(top_k * 4, 20)]
        rowids = [rid for rid, _ in order]
        placeholders = ",".join("?" for _ in rowids)
        rows = db.query(
            f"SELECT * FROM clauses WHERE id IN ({placeholders}) AND enabled = 1",
            rowids,
        )
        by_id = {int(r["id"]): r for r in rows}
        standards = self._standards_map()

        results: list[dict] = []
        for rid, detail in order:
            row = by_id.get(rid)
            if row is None:
                continue
            item = db.clause_to_dict(row)
            std = standards.get(item["standard_id"], {})
            item["standard_name"] = std.get("name", item["standard_id"])
            item["standard_version"] = std.get("version", "")
            item["source_url"] = std.get("source_url", "")
            item["license"] = std.get("license", "")
            item["score"] = round(detail["score"], 4)
            item["score_keyword"] = round(detail["keyword"], 4)
            item["score_vector"] = round(detail["vector"], 4)

            # 条款编号被直接点名 → 强力加权，用户搜 "8.1.4.1" 必须第一条就是它
            q_low = question.lower()
            if item["clause_id"].lower() in q_low:
                item["score"] = round(min(1.0, item["score"] + 0.5), 4)
                item["score_boost"] = "clause_id_exact"
            elif item["title"] and item["title"].lower() in q_low:
                item["score"] = round(min(1.0, item["score"] + 0.2), 4)
                item["score_boost"] = "title_exact"

            results.append(item)

        results.sort(key=lambda x: x["score"], reverse=True)
        results = results[:top_k]
        best = results[0]["score"] if results else 0.0
        return {
            "results": results,
            "best_score": best,
            "vector_used": vector_used,
            "query_terms": all_tokens,
        }


def get_engine() -> RetrievalEngine:
    global _engine
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                _engine = RetrievalEngine()
    return _engine


def reset_engine_cache() -> None:
    """知识库重建索引 / 数据库被整体恢复后调用，让所有缓存失效。"""
    engine = get_engine()
    engine._loaded_version = ""
    engine._standards_version = ""
    engine._standards = {}
    # 词频表也要重载：数据库恢复后 token_df 可能整体变了
    engine._df = None
    engine._df_version = ""
    try:
        engine._ensure_vector_matrix(force=True)
    except Exception as exc:
        log.warning("重置向量缓存失败：%s", exc)
