"""质量守护：把"指标红线"变成程序能自动检查的东西。

为什么需要：
检索阈值和权重是实测调出来的（0.40 / 0.5 / 0.5）。以后任何人改配置、
改分词词典、换嵌入模型，都可能让指标悄悄掉下去，而界面看起来一切正常。
这个模块把红线写成代码，让启动自检和命令行工具都能自动发现退化。

两层评测（这个分层是有原因的，别合并）
--------------------------------------
第一层 evaluate_retrieval：**不碰大模型**。判断"该不该答""该标什么风险"
  "该不该声明未收录""该不该拒绝做适用性判定"，全部由确定性代码决定
  （检索阈值 + guard.detect_risks + scope 的两个判定器）。
  所以它可以在 Ollama 没启动时照跑，也能作为服务启动自检。

第二层 evaluate_end2end：**要 Ollama 在跑**。补上检索层看不见的两件事：
  模型到底有没有追问、声明的话有没有真的出现在回答里。

第一层的红线就是启动自检的红线；第二层只在 tests/run_eval.py --llm 里查。

红线来源：docs/07-测试与验收规范.md
"""
from __future__ import annotations

import json
import logging

from . import config

log = logging.getLogger(__name__)

EVAL_FILE = config.BASE_DIR / "tests" / "eval_questions.json"

# --------------------------------------------------------------------------
# 指标红线（改这里之前先改 docs/07-测试与验收规范.md）
# --------------------------------------------------------------------------
RED_LINES = {
    "recall_min": 27,           # 该命中的 29 题里至少对 27 题
    "reject_required": 5,       # 5 道无关题必须全部拒答（一票否决）
    "risk_min": 8,              # 9 道高风险题至少标对 8 题
    "outkb_required": 6,        # 6 道点名库外法规的题必须全部识别出来
    "undet_required": 4,        # 4 道适用性判定题必须全部拦住
    "forbid_max_false": 0,      # 误标：一道都不许有
}

# 各分类的题目数量下限。**不能只看通过数**——
# 如果哪天题库文件被删掉一半，通过数会跟着缩水，红线却还是"通过"。
# 所以数量本身也要卡。加题时记得同步改这里。
EXPECTED_COUNTS = {
    "hit": 29,
    "reject": 5,
    "out_of_kb": 6,
    "undeterminable": 4,
    "risk_forbid": 2,
}


def load_questions() -> list[dict]:
    if not EVAL_FILE.exists():
        return []
    try:
        with open(EVAL_FILE, "r", encoding="utf-8") as f:
            return json.load(f)["questions"]
    except Exception as exc:
        log.warning("评测题读取失败：%s", exc)
        return []


def clause_matches(clause_id: str, patterns: list[str]) -> bool:
    return any(clause_id.startswith(p) or p in clause_id for p in patterns)


def _join(detail: str, extra: str) -> str:
    return (detail + " | " if detail else "") + extra


def _empty_stats() -> dict:
    return {
        "hit_total": 0, "hit_ok": 0,
        "reject_total": 0, "reject_ok": 0,
        "risk_total": 0, "risk_ok": 0,
        "outkb_total": 0, "outkb_ok": 0,
        "undet_total": 0, "undet_ok": 0,
        "forbid_total": 0, "forbid_ok": 0,
    }


# --------------------------------------------------------------------------
# 第一层：检索与判定（不需要大模型）
# --------------------------------------------------------------------------
def evaluate_retrieval(top_k: int = 6) -> dict:
    """跑一遍检索评测，返回逐题结果和汇总指标。"""
    from . import guard, scope
    from .retrieval import get_engine

    questions = load_questions()
    if not questions:
        return {
            "rows": [],
            "stats": _empty_stats(),
            "error": "找不到评测题文件 tests/eval_questions.json",
        }

    engine = get_engine()
    high_risk = set(config.HIGH_RISK_TAGS)
    rows: list[dict] = []
    stats = _empty_stats()

    for q in questions:
        expect = q.get("expect", "hit")
        result = engine.search(q["question"], top_k=top_k)
        best = result["best_score"]
        found = best >= config.RETRIEVAL_MIN_SCORE
        got = [c["clause_id"] for c in result["results"]]
        detail = ""
        ok = True

        if expect == "hit":
            stats["hit_total"] += 1
            patterns = q.get("must_include_any") or []
            recalled = any(clause_matches(cid, patterns) for cid in got) if patterns else True
            # 必须"召回了" **并且**"真的回答给用户了"才算通过。
            # 只看召回会有盲区：阈值调得过高的，条款照样在候选里（召回满分），
            # 但用户实际收到的是"知识库中未找到"——这显然是失败的。
            if found and recalled:
                stats["hit_ok"] += 1
            else:
                ok = False
                if not found:
                    detail = (f"被阈值拒答（最高分 {best:.3f} < {config.RETRIEVAL_MIN_SCORE}），"
                              f"用户收到的是『未找到』")
                else:
                    detail = f"未召回，期望含 {patterns}，实际 {got[:3]}"

        elif expect == "reject":
            stats["reject_total"] += 1
            if not found:
                stats["reject_ok"] += 1
            else:
                ok = False
                top = got[0] if got else "-"
                detail = f"未拒答，最高分 {best:.3f}（{top}）"

        elif expect == "out_of_kb":
            # 用户点名了知识库没收录的法规。**判定不需要大模型**：
            # 由 scope.detect_out_of_kb 认出法规名，服务端负责把"未收录"
            # 的说明强制拼进回答（见 routers/chat.py）。这里查的是"认没认出来"。
            stats["outkb_total"] += 1
            detected = [d["name"] for d in scope.detect_out_of_kb(q["question"])]
            wanted = q.get("out_of_kb_expect") or []
            missed = [w for w in wanted if not any(w in n for n in detected)]
            if not missed:
                stats["outkb_ok"] += 1
            else:
                ok = False
                detail = f"未识别出库外法规 {missed}，实际识别 {detected or '无'}"

        elif expect == "undeterminable":
            # 适用性判定类问题。同样不需要大模型：识别出来之后
            # chat.py 会**直接跳过模型**，用服务端模板回答。
            stats["undet_total"] += 1
            hit_undet, why = scope.needs_system_context(q["question"])
            if hit_undet:
                stats["undet_ok"] += 1
            else:
                ok = False
                detail = "未被识别为『适用性判定』类问题，会被当成可回答的问题交给大模型"

        else:
            ok = False
            detail = f"未知的 expect 取值：{expect}"

        # ⚠️ 只把三类高风险纳入评分。分母固定为"标了 risk_expect 的题目数"，
        # 这样阈值调坏时（大量问题被拒答）指标会真实下降，而不是分母缩水。
        risk_expect = [t for t in (q.get("risk_expect") or []) if t in high_risk]
        got_risks: list[str] = []
        if risk_expect:
            stats["risk_total"] += 1
            if not found:
                detail = _join(detail, "未召回，无法标注风险")
                ok = False
            else:
                got_risks = [r["tag"] for r in guard.detect_risks(q["question"], result["results"])]
                if any(t in got_risks for t in risk_expect):
                    stats["risk_ok"] += 1
                else:
                    ok = False
                    detail = _join(detail, f"⚠️未标注，期望 {risk_expect}，实际 {got_risks}")

        # 误标检查：模拟一个"见谁都标"的模型（把三类风险全报上去），
        # 看服务端会不会被带跑。这正是误标的成因：
        # 模型自报 → 服务端在条款正文里找到"个人信息"三个字 → 采纳。
        forbid = [t for t in (q.get("risk_forbid") or []) if t in high_risk]
        if forbid:
            stats["forbid_total"] += 1
            if not found:
                stats["forbid_ok"] += 1
                detail = _join(detail, "未召回，误标检查无意义（按通过计）")
            else:
                probe = [
                    r["tag"] for r in guard.detect_risks(
                        q["question"], result["results"], list(high_risk), cited_ids=got
                    )
                ]
                got_risks = probe
                bad = [t for t in forbid if t in probe]
                if bad:
                    ok = False
                    detail = _join(detail, f"误标 {bad}（模型把三类全报时被错误采纳）")
                else:
                    stats["forbid_ok"] += 1

        rows.append({
            "id": q["id"],
            "question": q["question"],
            "expect": expect,
            "forbid": forbid,
            "found": found,
            "best": round(best, 4),
            "ok": ok,
            "risks": got_risks,
            "top": [f"{c['clause_id']} {c['title']}" for c in result["results"][:3]],
            "detail": detail,
            "note": q.get("note", ""),
        })

    return {"rows": rows, "stats": stats}


def check_red_lines(stats: dict) -> dict:
    """把指标和红线比对，返回是否达标 + 每一项的明细。

    每一项都同时卡"数量够不够"和"通过数够不够"：
    只看通过数的话，题库被删空时反而会显示"全部达标"。
    """
    items: list[dict] = []

    def add(name: str, actual: int, total: int, required: str, passed: bool) -> None:
        items.append({
            "name": name, "actual": actual, "total": total,
            "required": required, "passed": passed,
        })

    expect_hit = EXPECTED_COUNTS["hit"]
    add("检索召回率", stats["hit_ok"], stats["hit_total"],
        f"≥ {RED_LINES['recall_min']}",
        stats["hit_total"] >= expect_hit and stats["hit_ok"] >= RED_LINES["recall_min"])

    expect_reject = EXPECTED_COUNTS["reject"]
    add("拒答正确率", stats["reject_ok"], stats["reject_total"],
        f"必须 = {RED_LINES['reject_required']}",
        stats["reject_total"] >= expect_reject
        and stats["reject_ok"] >= RED_LINES["reject_required"])

    expect_risk = 9
    add("⚠️ 风险标注率", stats["risk_ok"], stats["risk_total"],
        f"≥ {RED_LINES['risk_min']}",
        stats["risk_total"] >= expect_risk and stats["risk_ok"] >= RED_LINES["risk_min"])

    expect_outkb = EXPECTED_COUNTS["out_of_kb"]
    add("库外法规识别率", stats["outkb_ok"], stats["outkb_total"],
        f"必须 = {RED_LINES['outkb_required']}",
        stats["outkb_total"] >= expect_outkb
        and stats["outkb_ok"] >= RED_LINES["outkb_required"])

    expect_undet = EXPECTED_COUNTS["undeterminable"]
    add("适用性判定拦截率", stats["undet_ok"], stats["undet_total"],
        f"必须 = {RED_LINES['undet_required']}",
        stats["undet_total"] >= expect_undet
        and stats["undet_ok"] >= RED_LINES["undet_required"])

    expect_forbid = EXPECTED_COUNTS["risk_forbid"]
    add("⚠️ 误标率", stats["forbid_ok"], stats["forbid_total"],
        f"误标 ≤ {RED_LINES['forbid_max_false']} 处",
        stats["forbid_total"] >= expect_forbid
        and stats["forbid_ok"] >= stats["forbid_total"] - RED_LINES["forbid_max_false"])

    failures = [
        f"{i['name']} 只有 {i['actual']}/{i['total']}，要求 {i['required']}"
        for i in items if not i["passed"]
    ]
    return {"passed": not failures, "items": items, "failures": failures}


def run_check(top_k: int = 6) -> dict:
    """启动自检用的完整检查：跑第一层评测 + 比对红线。

    **刻意不碰大模型**：服务启动时 Ollama 可能还没起来，
    自检不能因此报失败，也不能因此卡住启动流程。
    """
    try:
        result = evaluate_retrieval(top_k=top_k)
    except Exception as exc:
        log.warning("质量自检执行失败：%s", exc)
        return {"passed": True, "skipped": True, "reason": str(exc), "items": [], "failures": []}

    if result.get("error"):
        return {"passed": True, "skipped": True, "reason": result["error"],
                "items": [], "failures": []}

    verdict = check_red_lines(result["stats"])
    verdict["rows"] = result["rows"]
    verdict["stats"] = result["stats"]
    verdict["skipped"] = False
    verdict["min_score"] = config.RETRIEVAL_MIN_SCORE
    verdict["keyword_weight"] = config.HYBRID_KEYWORD_WEIGHT
    verdict["vector_weight"] = config.HYBRID_VECTOR_WEIGHT
    return verdict


# --------------------------------------------------------------------------
# 第二层：端到端（需要 Ollama 在跑）
# --------------------------------------------------------------------------
def evaluate_end2end(limit: int | None = None) -> dict:
    """真的调一次大模型，看它输出的行为对不对。

    第一层能证明"该拒答的拒了、该声明的识别出来了"，但证明不了
    "用户最终看到的那段话里到底写了什么"。这一层补的就是这个缺口：
      ① clarify_expect 的题，模型有没有真的先追问；
      ② out_of_kb 的题，回答里有没有出现"未收录"的声明，且**没有**转述该法规内容。

    Ollama 没启动时返回 {"skipped": True, ...}，不抛异常。
    """
    from . import guard, llm, scope
    from .routers.chat import answer_question

    questions = [q for q in load_questions() if q.get("expect") != "reject"]
    if limit:
        questions = questions[:limit]

    rows: list[dict] = []
    stats = {
        "clarify_total": 0, "clarify_ok": 0,
        "outkb_total": 0, "outkb_ok": 0,
        "errors": 0,
    }

    if not llm.is_running():
        return {"skipped": True, "reason": "Ollama 未运行，跳过端到端评测",
                "rows": [], "stats": stats}

    for q in questions:
        row: dict = {"id": q["id"], "question": q["question"]}
        try:
            r = answer_question(q["question"])
        except Exception as exc:
            row["error"] = str(exc)
            stats["errors"] += 1
            rows.append(row)
            continue

        answer = r["answer"]
        row.update({
            "mode": r["mode"],
            "found": r["found"],
            "clarify": r["need_clarification"],
            "citations": [c["clause_id"] for c in r["citations"]],
            "risks": [x["tag"] for x in r["risks"]],
            "out_of_kb": r.get("out_of_kb") or [],
            "answer_head": answer[:160].replace("\n", " "),
            "problem": "",
        })

        if q.get("clarify_expect"):
            stats["clarify_total"] += 1
            if r["need_clarification"] and r["followup_questions"]:
                stats["clarify_ok"] += 1
            else:
                row["problem"] = "信息不足却没先追问"

        if q.get("expect") == "out_of_kb":
            stats["outkb_total"] += 1
            names = q.get("out_of_kb_expect") or []
            # 声明必须真的出现在回答正文里（不能只在 notes 里）
            declared = "未收录" in answer or "不在本工具的知识库范围内" in answer
            named = all(any(n in name for name in r.get("out_of_kb") or []) for n in names)
            # 反向检查：模型正文里不能再出现讲该法规"规定了什么"的句子。
            # 注意必须先把服务端那段说明摘掉再查，否则说明自己就带法规名，
            # 只要它一出现就永远判成"没泄漏"，等于什么都没查。
            notice = scope.out_of_kb_notice(r.get("out_of_kb") or [])
            body = answer[len(notice):] if notice and answer.startswith(notice) else answer
            _, still_leaking_count = guard.strip_out_of_kb_claims(body, scope.detect_out_of_kb(body))
            still_leaking = still_leaking_count > 0
            row["leaked_sentences"] = still_leaking_count
            if declared and named and not still_leaking:
                stats["outkb_ok"] += 1
            else:
                row["problem"] = (
                    f"库外法规声明缺失（声明={declared} 识别={named} 泄漏={still_leaking}）"
                )

        rows.append(row)

    return {"skipped": False, "rows": rows, "stats": stats}


def check_end2end_red_lines(stats: dict) -> dict:
    """端到端的红线。比第一层宽松，因为模型行为本身有波动。"""
    items = [
        {
            "name": "追问正确率",
            "actual": stats["clarify_ok"],
            "total": stats["clarify_total"],
            "required": f"≥ {max(1, stats['clarify_total'] * 2 // 3)}（三分之二）",
            "passed": stats["clarify_total"] > 0
            and stats["clarify_ok"] >= stats["clarify_total"] * 2 / 3,
        },
        {
            "name": "库外法规声明率",
            "actual": stats["outkb_ok"],
            "total": stats["outkb_total"],
            "required": f"必须 = {stats['outkb_total']}",
            "passed": stats["outkb_total"] > 0 and stats["outkb_ok"] == stats["outkb_total"],
        },
    ]
    failures = [
        f"{i['name']} 只有 {i['actual']}/{i['total']}，要求 {i['required']}"
        for i in items if not i["passed"]
    ]
    return {"passed": not failures, "items": items, "failures": failures}
