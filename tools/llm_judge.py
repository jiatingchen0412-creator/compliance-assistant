"""模型评审（LLM-as-judge）：逐题判答案「忠于条款 / 不编造 / 回答切题」，并算幻觉率。

为什么要这个工具：
    第 2 层（tests/run_eval.py 不带 --no-llm）只检查「有没有追问」「库外法规有没有声明」，
    不判断答案本身好不好。提示词改坏、换模型、检索选错条款，CI 全绿也发现不了
    ——因为 CI 不装 Ollama（铁律三：测试不许依赖 Ollama 在线）。

    所以这一层是**手工**的：发布前在本机跑一次，人工看低分题。
    它不进 CI、不进 check_quality，也不会把任何东西变绿。

用法（在项目根目录执行）：
    .venv\\Scripts\\python.exe tools\\llm_judge.py                  # 全部 hit 题
    .venv\\Scripts\\python.exe tools\\llm_judge.py --limit 5        # 只跑 5 题（做人工标注基线）
    .venv\\Scripts\\python.exe tools\\llm_judge.py --json           # 只输出 JSON 摘要
    .venv\\Scripts\\python.exe tools\\llm_judge.py --self-test      # 不联网，验证判卷/统计/报告流水线
    .venv\\Scripts\\python.exe tools\\llm_judge.py --agreement-only # 用已有 rows 文件算人工一致率
    .venv\\Scripts\\python.exe tools\\llm_judge.py --probe          # 召回率校准：已知有幻觉/无幻觉各 10+ 条

为什么还需要 --probe（O2）：
    `tests/run_eval.py` 那 25 道 hit 题的答案本来就没有幻觉，用它算幻觉率等于
    问一条从不下雨的路有没有洪水——**只能证明 judge 不误报，证明不了它能报**。
    `tests/judge_probe.json` 里的答案由人工编写，一半注入条款里没有的具体事实，
    跑一遍才知道 judge 的召回率是多少。

来源指纹（O8）：
    报告头部记录 提示词版本 / 知识库签名与条数 / git commit / 评审模型。
    没有指纹的百分比不可比：跨版本、跨知识库的「幻觉率 0%」不是同一个 0%。

确定性核验（O6）：
    judge 列出的每条「无依据事实」，工具会拿**未截断的完整条款正文**回查一遍，
    把 judge 判错的（事实其实就在条款里）单独计数，得到 judge 事实精确率。
    条款正文在提示词里最多 900 字，截断会让 judge 把后半段的真实内容误判为幻觉。

产出（都被 .gitignore 的 tools/_* 忽略）：
    tools/_judge_report.md   人看的报告（含低分题全文/理由）
    tools/_judge_rows.json   机器读的逐题结果（供 --agreement-only 复用）
    tools/_judge_human.json  人工标注（需人工填写；格式见报告末尾）

退出码（绝不把「没跑」说成「通过」）：
    0 = 判卷已执行并产出报告（不代表答案一定好，只代表这一层跑完了）
    2 = 未执行（Ollama 未就绪 / 没有可判的题）
    1 = 执行了但完全失败（一道题都没判出来 / 报告写不出来）
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config, db, ingest, llm, prompt, quality  # noqa: E402

REPORT_FILE = ROOT / "tools" / "_judge_report.md"
ROWS_FILE = ROOT / "tools" / "_judge_rows.json"
HUMAN_FILE = ROOT / "tools" / "_judge_human.json"
PROBE_FILE = ROOT / "tests" / "judge_probe.json"
PROBE_REPORT = ROOT / "tools" / "_judge_probe.md"

DIMENSIONS = ("faithful", "no_fabrication", "relevant")
DIM_LABEL = {
    "faithful": "忠于条款",
    "no_fabrication": "不编造",
    "relevant": "回答切题",
}
# 人工标注与 judge 的「幻觉判定」一致率低于这个值，就认为这个 judge 不可用。
AGREEMENT_MIN = 0.8

JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "faithful": {"type": "integer"},
        "no_fabrication": {"type": "integer"},
        "relevant": {"type": "integer"},
        "unsupported_facts": {"type": "array", "items": {"type": "string"}},
        "reason": {"type": "string"},
    },
    "required": ["faithful", "no_fabrication", "relevant", "unsupported_facts", "reason"],
}

JUDGE_SYSTEM = (
    "你是严格的安全合规答案评审员。只评估给定的『用户问题』与『助手最终回答』，"
    "以『引用条款原文』为唯一事实依据。不要补充你自己的合规知识，不要评价文风，"
    "不要因为答案语气自信就给高分。必须输出 JSON 对象。"
)

JUDGE_RUBRIC = """【必须按这个顺序做，不要跳步】
第 1 步：把『助手最终回答』里所有**具体事实**逐句抄出来。具体事实指带数字、时间/时限、
        机构名、价格/费用、法规或条款编号、处罚后果、认证/测评结论、明确责任主体的句子。
        只描述条款本身的句子不算具体事实（例如直接复述条款要求）。
第 2 步：对第 1 步抄出的每一条，回到『引用条款原文』里找依据。判定只有一个标准：
        **条款原文里有没有表达同样的意思**。条款里没提、但听起来合理、你觉得"应该也是这样"
        ——一律算没有依据。不要用你自己的合规知识替它补依据。
第 3 步：把第 2 步里找不到依据的句子原样放进 unsupported_facts。
        只要 unsupported_facts 非空，no_fabrication 必须是 1-2 分，不能给 3 分以上。

【评分标准（1-5 分，只给整数）】
- faithful（忠于条款）：第 2 步里每一条都能找到依据 → 5；有概括但不矛盾 → 3；
  有与原文矛盾、或原文不支持的具体陈述 → 1。
- no_fabrication（不编造）：unsupported_facts 为空 → 5；只有泛化表述 → 3；
  至少一条无依据的具体事实 → 1-2。
- relevant（回答切题）：正面回应用户问题 → 5；只沾边 → 3；答非所问 → 1。
- unsupported_facts：第 3 步的结果，逐条抄原句；没有就给空数组。
- reason：不超过 80 字，指出扣分点；全 5 分就写「有条款依据、切题」。

【常见误判，必须避免】
- **答案没写某个信息，不是编造。** 编造是「写了条款里没有的东西」，不是「写得不够全」。
  答案没提保存期限、没提测评机构、没给具体数值——这些都不扣「不编造」的分。
- 答案把条款内容复述对了，但**末尾多了一句条款里没有的话**（时限、报备要求、价格、
  处罚、机构）——这一句就是无依据事实，不能因为前面都对就忽略它。
- 答案里出现"应当""建议"后面跟的具体动作和期限，若条款原文没写这个期限，就是编造。
- 不要因为语气自信、格式整齐、看起来专业就给高分。

只输出 JSON 对象，不要 Markdown 代码块，不要额外解释。"""


# --------------------------------------------------------------------------
# 数据库隔离：独立运行时用临时副本，避免把评测会话写进用户的 data/app.db
# --------------------------------------------------------------------------
@contextmanager
def _temp_db(enabled: bool = True):
    if not enabled:
        yield
        return

    import shutil
    import sqlite3
    import tempfile

    real = Path(config.DB_PATH)
    tmp_dir = Path(tempfile.mkdtemp(prefix="judge_db_"))
    try:
        if real.exists():
            src = sqlite3.connect(str(real))
            dst = sqlite3.connect(str(tmp_dir / "app.db"))
            try:
                src.backup(dst)
            finally:
                dst.close()
                src.close()
        config.DB_PATH = str(tmp_dir / "app.db")
        yield
    finally:
        config.DB_PATH = str(real)
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _prepare_db() -> None:
    db.init_db()
    ingest.ensure_seed_loaded()


# --------------------------------------------------------------------------
# 判卷
# --------------------------------------------------------------------------
def _clamp_score(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    if not isinstance(value, int):
        return None
    return max(1, min(5, value))


def _build_messages(question: str, answer: str, citations: list[dict]) -> list[dict]:
    blocks = []
    for i, c in enumerate(citations[:6], 1):
        text = (c.get("text") or "").strip()
        blocks.append(
            f"[{i}] {c.get('clause_id', '')} {c.get('title', '')}"
            f"（{c.get('text_type', '')}）\n{text[:900]}"
        )
    cited = "\n\n".join(blocks) if blocks else "（本题没有引用任何条款）"
    user = (
        f"【用户问题】\n{question}\n\n"
        f"【助手最终回答（用户看到的全文）】\n{answer[:4000]}\n\n"
        f"【引用条款原文（工具从知识库取出，未经模型改写）】\n{cited}\n\n"
        f"{JUDGE_RUBRIC}"
    )
    return [
        {"role": "system", "content": JUDGE_SYSTEM},
        {"role": "user", "content": user},
    ]


# --------------------------------------------------------------------------
# O6：对 judge 列出的「无依据事实」做确定性回查
# --------------------------------------------------------------------------
_WS_RE = re.compile(r"\s+")
_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]{2,}")
_DIGIT_RUN_RE = re.compile(r"[0-9]+(?:\.[0-9]+)?")

# judge 在提示词里最多看到 6 条条款、每条 900 字。超出这个范围，它当然「找不到依据」。
_JUDGE_MAX_CLAUSES = 6
_JUDGE_MAX_CHARS = 900


def _squash(text: object) -> str:
    """去掉全部空白——中文条款里「30 日内」和「30日内」必须当成同一个字符串。"""
    return _WS_RE.sub("", str(text or ""))


def _fact_support(fact: str, corpus: str) -> str:
    """判断 judge 说的「条款里没有这条事实」到底成不成立。

    返回三种：
      supported   —— 事实确实在条款里，judge 判错了
      ambiguous   —— 只有一部分词对得上，不能断言 judge 错
      unsupported —— 关键词一个都不在，judge 判对了

    为什么不用精确整句匹配：条款写「30 日内完成备案」，judge 说的事实是
    「30 天内完成备案」。字面不同、语义相同，精确匹配会把它记成「judge 对」，
    于是精确率虚高。所以按 token 覆盖率判，只把**关键词全中**的情况算 judge 判错。
    """
    fact_n = _squash(fact)
    if not fact_n:
        return "ambiguous"
    corpus_n = _squash(corpus)
    if not corpus_n:
        return "ambiguous"
    if fact_n in corpus_n:
        return "supported"
    tokens = [t for t in _CJK_RUN_RE.findall(fact_n) if len(t) >= 2]
    # 中文按 2 字滑窗切：整段当 token 时，「完成年度审计」与「完成备案」一个字都对不上，
    # 会把「部分相关」误判成「完全不相关」，精确率被高估。
    bigrams: list[str] = []
    for run in tokens:
        bigrams += [run[i:i + 2] for i in range(len(run) - 1)]
    tokens = bigrams + _DIGIT_RUN_RE.findall(fact_n)
    if not tokens:
        return "ambiguous"
    hit = [t for t in tokens if t in corpus_n]
    if len(hit) == len(tokens):
        return "supported"
    if hit:
        return "ambiguous"
    return "unsupported"


def verify_unsupported_facts(facts: list[str], citations: list[dict]) -> dict:
    """把 judge 列出的无依据事实，拿去**未截断的完整条款正文**里回查。

    这是 O6：judge 说「条款里没有 X」，工具自己验证一遍。
    - `supported` 的条目 = judge 判错（事实其实在条款里）
    - 若这些条目落在提示词的截断区（第 7 条之后的条款、或 900 字之后），
      原因就找到了：不是 judge 笨，是我们没让它看到。
    """
    entries = []
    for fact in facts:
        per_clause = []
        for c in citations:
            status = _fact_support(fact, c.get("text") or c.get("summary") or "")
            if status != "unsupported":
                per_clause.append({"clause_id": c.get("clause_id", ""), "status": status})
        if any(x["status"] == "supported" for x in per_clause):
            verdict = "supported"
        elif per_clause:
            verdict = "ambiguous"
        else:
            verdict = "unsupported"
        entries.append({"fact": fact, "verdict": verdict, "matches": per_clause[:3]})

    truncated = (
        len(citations) > _JUDGE_MAX_CLAUSES
        or any(len((c.get("text") or "").strip()) > _JUDGE_MAX_CHARS for c in citations)
    )
    return {
        "entries": entries,
        "supported": [e["fact"] for e in entries if e["verdict"] == "supported"],
        "ambiguous": [e["fact"] for e in entries if e["verdict"] == "ambiguous"],
        "unsupported": [e["fact"] for e in entries if e["verdict"] == "unsupported"],
        "truncated": truncated,
    }


# --------------------------------------------------------------------------
# 确定性判据：不经模型就能判定的「无依据具体事实」
# --------------------------------------------------------------------------
# 为什么需要这一层：实测 qwen2.5:7b-instruct 当 judge 时，对 12 条明显编造的答案
# **一条都没报**（召回 0/12），把评分标准改成「先抄事实再逐条对照」也只到 3/8。
# 而「答案里出现的数字在条款原文里找不到」这件事根本不需要模型判断。
# 结论：能确定性判定的，不要交给模型。
_ID_IN_ANSWER_RE = re.compile(
    r"\b\d+(?:\.\d+){1,4}\b|[A-Z]{2}\.[A-Z]{2}-\d{2}|A\d{2}:\d{4}(?:-(?:CWE-\d+|\d+))?"
)
# 数字 + 中文数量词（「七次覆写」「两 条」）。单字前不能是另一个数词，避免把「十一」拆开。
_NUMBER_RE = re.compile(
    r"[0-9]+(?:\.[0-9]+)?|(?<![一二三四五六七八九十])[一二三四五六七八九十两]+(?=年|天|日|小时|分钟|周|月|次|倍|项|条)"
)
_HEADER_LINE_RE = re.compile(r"^\s*\*\*[^*]+\*\*\s*$")


def _kb_clause_ids() -> set[str]:
    try:
        return {str(r["clause_id"]) for r in db.query("SELECT clause_id FROM clauses")}
    except Exception:
        return set()


def deterministic_claims(
    answer: str,
    citations: list[dict],
    kb_ids: set[str] | None = None,
) -> dict:
    """不用模型，直接判答案里有哪些具体事实在条款原文里找不到依据。

    两类：
      1. 数字类：答案正文里的数字（时限、价格、次数、编号数字），在引用条款正文里找不到；
      2. 编号类：答案里提到的条款编号，知识库里根本不存在。

    只扫「正文行」：以 `**条款号 标题**` 开头的行是工具自己拼的标题，跳过。
    编号会在扫描前剔除，否则标题里的 `8.1.4.1` 会被当成「条款里没有的数字」。
    """
    body_lines = [
        ln for ln in str(answer or "").split("\n")
        if not _HEADER_LINE_RE.match(ln)
    ]
    body = "\n".join(body_lines)
    mentioned_ids = _ID_IN_ANSWER_RE.findall(body)
    body_no_ids = _ID_IN_ANSWER_RE.sub(" ", body)

    corpus = " ".join(
        str(c.get("text") or c.get("summary") or "") for c in citations
    )
    missing_numbers = []
    for num in _NUMBER_RE.findall(body_no_ids):
        if num not in corpus and num not in missing_numbers:
            missing_numbers.append(num)

    unknown_ids = []
    if kb_ids is not None:
        for cid in mentioned_ids:
            if cid not in kb_ids and cid not in unknown_ids:
                unknown_ids.append(cid)

    return {
        "missing_numbers": missing_numbers[:8],
        "unknown_clause_ids": unknown_ids[:8],
        "flagged": bool(missing_numbers or unknown_ids),
    }


def judge_answer(question: str, answer: str, citations: list[dict]) -> dict:
    """让本地模型判一条答案。返回 verdict dict，或 {"error": ...}。"""
    if not answer.strip():
        return {"error": "答案为空，无法判卷"}
    try:
        raw = llm.chat(
            _build_messages(question, answer, citations),
            json_schema=JUDGE_SCHEMA,
            temperature=0.0,
            max_tokens=600,
        )
    except llm.LLMUnavailable as exc:
        return {"error": f"评审模型不可用：{exc}"}
    except Exception as exc:  # 网络/超时等
        return {"error": f"{type(exc).__name__}: {exc}"}

    data = llm.parse_json_loose(raw or "")
    if not isinstance(data, dict):
        return {"error": "评审输出不是合法 JSON"}

    scores = {key: _clamp_score(data.get(key)) for key in DIMENSIONS}
    missing = [DIM_LABEL[k] for k, v in scores.items() if v is None]
    if missing:
        return {"error": "评审输出缺少数值字段：" + "、".join(missing)}

    facts = []
    raw_facts = data.get("unsupported_facts")
    if isinstance(raw_facts, list):
        facts = [str(x).strip()[:200] for x in raw_facts if str(x).strip()][:8]
    # O6：judge 说「条款里没有这条事实」，工具拿未截断的全文自己再查一遍。
    check = verify_unsupported_facts(facts, citations)
    # 确定性判据：数字与编号不需要模型判断，直接查。
    det = deterministic_claims(answer, citations, _kb_clause_ids())
    # 模型判的、或确定性判据查出来的，任一成立即算幻觉。
    hallucinated = bool(facts) or scores["no_fabrication"] <= 2 or det["flagged"]
    return {
        **scores,
        "unsupported_facts": facts,
        "facts_check": check,
        "deterministic": det,
        "model_flagged": bool(facts) or scores["no_fabrication"] <= 2,
        "reason": str(data.get("reason") or "")[:160],
        "hallucinated": hallucinated,
    }


def _judge_rows(questions: list[dict], answer_fn, judge_fn, quiet: bool) -> list[dict]:
    rows: list[dict] = []
    total = len(questions)
    for i, q in enumerate(questions, 1):
        qid = q.get("id", f"q{i:02d}")
        question = q.get("question", "")
        row: dict = {"id": qid, "question": question}
        try:
            result = answer_fn(question)
            citations = result.get("citations") or []
            row.update({
                "mode": result.get("mode", ""),
                "found": bool(result.get("found")),
                "citation_ids": [c.get("clause_id", "") for c in citations],
                "answer": result.get("answer", ""),
            })
            verdict = judge_fn(question, row["answer"], citations)
        except Exception as exc:
            row["error"] = f"{type(exc).__name__}: {exc}"
            rows.append(row)
            if not quiet:
                print(f"[judge] {i:02d}/{total} {qid}  错误：{row['error'][:90]}")
            continue

        if verdict.get("error"):
            row["error"] = verdict["error"]
            if not quiet:
                print(f"[judge] {i:02d}/{total} {qid}  未判定：{row['error'][:90]}")
        else:
            row["verdict"] = verdict
            row["hallucinated"] = bool(verdict.get("hallucinated"))
            if not quiet:
                score = "/".join(str(verdict[k]) for k in DIMENSIONS)
                mark = "幻觉" if row["hallucinated"] else "无幻觉"
                refs = ",".join(row["citation_ids"][:3]) or "-"
                print(f"[judge] {i:02d}/{total} {qid}  引用={refs:<28} 评分 {score}  {mark}")
        rows.append(row)
    return rows


def _stats(rows: list[dict]) -> dict:
    judged = [r for r in rows if "verdict" in r]
    substantive = [r for r in judged if (r.get("answer") or "").strip()]
    hallucinations = [r for r in substantive if r.get("hallucinated")]
    rate = (len(hallucinations) / len(substantive)) if substantive else None
    means = {}
    for key in DIMENSIONS:
        vals = [r["verdict"][key] for r in judged]
        means[key] = round(sum(vals) / len(vals), 2) if vals else None
    ranked = sorted(
        judged,
        key=lambda r: sum(r["verdict"][k] for k in DIMENSIONS),
    )
    # 只有真的掉分（任一维度 <5）才进「低分题详情」；全 5 分时不要制造“低分题”假象。
    low = [r for r in ranked if sum(r["verdict"][k] for k in DIMENSIONS) < 15][:5]

    # O6：judge 列出的无依据事实，回查后有多少其实在条款里（= judge 判错）。
    facts_total = facts_supported = facts_ambiguous = facts_unsupported = 0
    truncated_rows = []
    for r in judged:
        check = (r.get("verdict") or {}).get("facts_check") or {}
        facts_total += len(check.get("supported") or []) + len(check.get("ambiguous") or []) + len(check.get("unsupported") or [])
        facts_supported += len(check.get("supported") or [])
        facts_ambiguous += len(check.get("ambiguous") or [])
        facts_unsupported += len(check.get("unsupported") or [])
        if check.get("truncated"):
            truncated_rows.append(r["id"])
    # 精确率的分母只算「能断言的」：含糊的（部分词对上）不计入，避免虚高也避免虚低。
    decidable = facts_supported + facts_unsupported
    fact_precision = (facts_unsupported / decidable) if decidable else None

    return {
        "total": len(rows),
        "judged": len(judged),
        "failed": len(rows) - len(judged),
        "substantive": len(substantive),
        "hallucinations": len(hallucinations),
        "rate": rate,
        "means": means,
        "low_ids": [r["id"] for r in low],
        "facts": {
            "total": facts_total,
            "supported": facts_supported,
            "ambiguous": facts_ambiguous,
            "unsupported": facts_unsupported,
            "precision": fact_precision,
            "truncated_rows": truncated_rows,
        },
    }


def judge_hit_questions(
    limit: int | None = None,
    *,
    quiet: bool = False,
    isolate_db: bool = True,
    answer_fn=None,
    judge_fn=None,
) -> dict:
    """对 dev 集里 expect=hit 的题重新答题并判卷。

    answer_fn/judge_fn 只给 --self-test 注入假实现用；正常路径走真实模型。
    """
    questions = [q for q in quality.load_questions() if q.get("expect") == "hit"]
    if not questions:
        return {"executed": False, "reason": "没有 expect=hit 的题可判（评测题文件缺失或为空？）"}
    if limit is not None:
        questions = questions[: max(0, limit)]

    if answer_fn is None or judge_fn is None:
        if not llm.is_running():
            return {
                "executed": False,
                "reason": f"Ollama 未就绪（{config.OLLAMA_BASE_URL} 连不上），模型评审未执行",
            }
        from app.routers.chat import answer_question  # 延迟导入：--help/--self-test 不加载整套应用

        answer_fn = answer_fn or (lambda q: answer_question(q))
        judge_fn = judge_fn or (
            lambda q, a, c: judge_answer(q, a, c)
        )
        with _temp_db(isolate_db):
            _prepare_db()
            rows = _judge_rows(questions, answer_fn, judge_fn, quiet)
    else:
        rows = _judge_rows(questions, answer_fn, judge_fn, quiet)

    return {"executed": True, "rows": rows, "stats": _stats(rows), "provenance": provenance()}


# --------------------------------------------------------------------------
# 人工标注一致率
# --------------------------------------------------------------------------
def _load_annotations(path: Path) -> dict[str, dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as exc:
        print(f"[judge] 人工标注文件读不了：{path}（{exc}）")
        return {}
    items = data.get("annotations") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return {}
    result = {}
    for item in items:
        if isinstance(item, dict) and item.get("id"):
            result[str(item["id"])] = item
    return result


def _agreement(rows: list[dict], annotations: dict[str, dict]) -> dict | None:
    judged = {r["id"]: r for r in rows if "verdict" in r}
    pairs = [(judged[qid], ann) for qid, ann in annotations.items() if qid in judged]
    if not pairs:
        return None
    agree = sum(
        1 for row, ann in pairs
        if bool(ann.get("hallucination")) == bool(row.get("hallucinated"))
    )
    # 评分偏差只对填了的维度算，方便判断「一致」是不是碰巧。
    diffs = {}
    for key in ("faithful", "relevant"):
        vals = [
            abs(int(ann[key]) - int(row["verdict"][key]))
            for row, ann in pairs
            if isinstance(ann.get(key), (int, float)) and key in row["verdict"]
        ]
        diffs[key] = round(sum(vals) / len(vals), 2) if vals else None
    return {
        "total": len(pairs),
        "agree": agree,
        "rate": agree / len(pairs),
        "usable": agree / len(pairs) >= AGREEMENT_MIN,
        "mean_abs_diff": diffs,
        "mismatches": [row["id"] for row, ann in pairs
                       if bool(ann.get("hallucination")) != bool(row.get("hallucinated"))],
    }


# --------------------------------------------------------------------------
# 报告
# --------------------------------------------------------------------------
def _cell(text) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ").strip()


def _git_sha() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=10,
        )
        return (out.stdout or "").strip() or "unknown"
    except Exception:
        return "unknown"


def _kb_fingerprint() -> dict:
    """知识库签名与条数。换过知识库的两次「幻觉率」不是同一个指标。"""
    info: dict = {"seed_signature": "unknown", "clauses": None}
    try:
        info["seed_signature"] = str(db.get_setting("seed_signature") or "unknown")
    except Exception:
        pass
    try:
        row = db.query_one("SELECT COUNT(*) AS n FROM clauses")
        info["clauses"] = int(row["n"]) if row else None
    except Exception:
        pass
    return info


def provenance() -> dict:
    """O8：这次评审是在什么代码 / 知识库 / 提示词上跑的。

    为什么必须有指纹：报告里的「幻觉率 0%」如果没写提示词版本和知识库签名，
    它和上一次的 0% 就不是同一个 0%——跨版本不可比，也就不该被拿来比较。
    """
    kb = _kb_fingerprint()
    return {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "git_sha": _git_sha(),
        "prompt_version": getattr(prompt, "PROMPT_VERSION", "unknown"),
        "prompt_date": getattr(prompt, "PROMPT_VERSION_DATE", "unknown"),
        "kb_seed_signature": kb["seed_signature"],
        "kb_clauses": kb["clauses"],
        "judge_model": config.LLM_MODEL,
        "ollama_base_url": config.OLLAMA_BASE_URL,
    }


def _provenance_lines(prov: dict) -> list[str]:
    clauses = prov.get("kb_clauses")
    return [
        "- 生成时间：" + str(prov.get("generated_at", "")) + "　代码版本：`"
        + str(prov.get("git_sha", "")) + "`",
        f"- 提示词版本：{prov.get('prompt_version', '')}（{prov.get('prompt_date', '')}）"
        f"　知识库：{clauses if clauses is not None else '未知'} 条"
        f"（签名 `{prov.get('kb_seed_signature', '')}`）",
        f"- 评审模型：{prov.get('judge_model', '')}（{prov.get('ollama_base_url', '')}）",
        "- 指纹的用处：没有这几项，「幻觉率 0%」跨版本不可比，只能当一次性快照。",
    ]


def _rate_text(stats: dict) -> str:
    if not stats.get("substantive"):
        return "无法判定（没有可计入的回答）"
    return f"{stats['hallucinations']}/{stats['substantive']}（{stats['rate']:.0%}）"


def render_report(result: dict, agreement: dict | None = None) -> str:
    prov = result.get("provenance") or provenance()
    lines = [
        "# 模型评审报告（LLM-as-judge，手工层）",
        "",
        *_provenance_lines(prov),
        "- 说明：本报告不进 CI、不进红线；它只回答「模型写出来的答案好不好」，"
        "不替代人工验收。",
        "",
    ]
    if not result.get("executed"):
        lines += [
            "## 结论",
            "",
            f"**未执行**：{result.get('reason', '未知原因')}",
            "",
            "本次没有产生任何评分。不要把它当成通过——这一层的结论就是「没测」。",
            "",
        ]
        return "\n".join(lines)

    stats = result["stats"]
    lines += [
        "## 结论",
        "",
        f"- 判卷题数：{stats['judged']}/{stats['total']}（无法判定 {stats['failed']} 题）",
        f"- **幻觉率：{_rate_text(stats)}**"
        "（分子 = judge 判定出现无依据具体事实的题数；分母 = 有回答的判卷题数）",
        f"- 平均分：忠于条款 {stats['means']['faithful']} / "
        f"不编造 {stats['means']['no_fabrication']} / 回答切题 {stats['means']['relevant']}",
    ]
    if agreement is None:
        lines.append(f"- judge 与人工标注一致率：未提供（{HUMAN_FILE.name} 不存在或不含可匹配题号）")
    else:
        lines.append(
            f"- judge 与人工标注一致率：{agreement['agree']}/{agreement['total']}"
            f"（{agreement['rate']:.0%}，阈值 ≥{AGREEMENT_MIN:.0%}；"
            f"{'可用' if agreement['usable'] else '**不可用，请如实写明**'}）"
        )
        if agreement.get("mismatches"):
            lines.append(f"- 不一致题号：{', '.join(agreement['mismatches'])}")
        diffs = agreement.get("mean_abs_diff") or {}
        diff_items = [f"{DIM_LABEL[k]} {v}" for k, v in diffs.items() if v is not None]
        if diff_items:
            lines.append("- 评分平均偏差（人工 vs judge）：" + "、".join(diff_items))
    facts = stats.get("facts") or {}
    if facts.get("total"):
        decidable = (facts.get("supported") or 0) + (facts.get("unsupported") or 0)
        if decidable and facts.get("precision") is not None:
            lines.append(
                f"- **judge 事实精确率：{facts['unsupported']}/{decidable}"
                f"（{facts['precision']:.0%}）**"
                f"（工具把 judge 列出的 {facts['total']} 条「无依据事实」拿去未截断的条款正文回查："
                f"{facts['unsupported']} 条确实找不到、{facts['supported']} 条其实就在条款里"
                f"（judge 判错）、{facts['ambiguous']} 条只有部分词对上无法断言）"
            )
            if facts.get("supported"):
                lines.append(
                    "  - ⚠️ 有 judge 判错的条目：说明幻觉率里有误报，"
                    "看下面的「事实回查」小节逐条对照"
                )
        else:
            lines.append("- judge 事实精确率：无法计算（judge 没有列出可断言的无依据事实）")
        if facts.get("truncated_rows"):
            lines.append(
                "- ⚠️ 提示词截断：以下题目的引用条款有第 7 条之后或 900 字之后的内容，"
                "judge 看不到——那些位置上的「无依据」可能是我们没让它看，而不是它判错："
                + "、".join(facts["truncated_rows"])
            )
    lines += [
        "",
        "## 逐题明细",
        "",
        "| 判定 | 编号 | 模式 | 引用条款 | 忠于条款 | 不编造 | 回答切题 | 幻觉 | 问题 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for row in result["rows"]:
        if "verdict" not in row:
            lines.append(
                f"| ⚠️ | {_cell(row['id'])} | - | - | - | - | - | 未判定 | "
                f"{_cell(row.get('error', ''))} |"
            )
            continue
        v = row["verdict"]
        lines.append(
            f"| {'⚠️ 有幻觉' if row.get('hallucinated') else '✅'} | {_cell(row['id'])} | "
            f"{_cell(row.get('mode', ''))} | {_cell(','.join(row.get('citation_ids', [])) or '-')} | "
            f"{v['faithful']} | {v['no_fabrication']} | {v['relevant']} | "
            f"{'是' if row.get('hallucinated') else '否'} | {_cell(row['question'][:30])} |"
        )

    low = [r for r in result["rows"] if r["id"] in set(stats.get("low_ids", [])) and "verdict" in r]
    if low:
        lines += ["", "## 低分题详情（先看这里）", ""]
        for row in low:
            v = row["verdict"]
            refs = "\n".join(f"- `{cid}`" for cid in row.get("citation_ids", [])) or "- （无引用）"
            lines += [
                f"### {row['id']} · {row['question'][:60]}",
                "",
                f"- 评分：忠于条款 {v['faithful']} / 不编造 {v['no_fabrication']} / 回答切题 {v['relevant']}",
                f"- 引用条款：\n{refs}",
                f"- 评审理由：{v.get('reason', '') or '（无）'}",
            ]
            if v.get("unsupported_facts"):
                lines.append("- 无依据的具体事实：")
                lines += [f"  - {_cell(x)}" for x in v["unsupported_facts"]]
            lines += [
                "",
                "回答全文（前 800 字）：",
                "",
                "```text",
                (row.get("answer") or "")[:800],
                "```",
                "",
            ]
    else:
        lines += [
            "",
            "## 低分题详情",
            "",
            "本次判卷没有低分题（三个维度都是 5 分）。",
            "",
        ]

    # 人工标注要对着答案看，所以把每一题的回答都放进来（不只低分题）。
    lines += ["## 逐题回答（人工标注用）", ""]
    for row in result["rows"]:
        if "verdict" not in row:
            continue
        v = row["verdict"]
        lines += [
            f"### {row['id']} · {row['question'][:60]}",
            "",
            f"- 评分：忠于条款 {v['faithful']} / 不编造 {v['no_fabrication']} / 回答切题 {v['relevant']}"
            f"；引用：{', '.join(row.get('citation_ids', [])) or '-'}",
            "",
            "```text",
            (row.get("answer") or "")[:900],
            "```",
            "",
        ]

    lines += [
        "",
        "## 人工标注（judge 也是模型，必须校准）",
        "",
        f"新建 `{HUMAN_FILE.name}`（被 .gitignore 忽略），格式：",
        "",
        "```json",
        "{",
        '  "_说明": "人工看完本题答案后填写；hallucination 表示回答里有没有无依据的具体事实",',
        '  "annotations": [',
        '    {"id": "q01", "hallucination": false, "faithful": 5, "relevant": 5, "note": "..."}',
        "  ]",
        "}",
        "```",
        "",
        f"然后运行 `tools\\\\llm_judge.py --agreement-only`：一致率低于 "
        f"{AGREEMENT_MIN:.0%} 就说明这个 judge 不可用，报告里要如实写，不能拿它当质量证据。",
        "",
        "注意：如果样本里本来就没有幻觉，一致率只能证明「没有误报」，"
        "不能证明「能抓到幻觉」；抓幻觉的能力由 `--self-test` 的合成样本验证。",
        "",
    ]
    return "\n".join(lines)


def _write_outputs(result: dict, out: Path, rows_out: Path,
                   annotations: dict[str, dict]) -> dict | None:
    agreement = _agreement(result.get("rows", []), annotations) if annotations else None
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_report(result, agreement), encoding="utf-8")
    rows_out.write_text(
        json.dumps(
            {
                "executed": result.get("executed", False),
                "reason": result.get("reason", ""),
                "stats": result.get("stats"),
                "provenance": result.get("provenance"),
                "rows": result.get("rows", []),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    if agreement is not None:
        out.write_text(render_report(result, agreement), encoding="utf-8")
    return agreement


# --------------------------------------------------------------------------
# O2：召回率校准（已知有幻觉 / 无幻觉的答案各跑一遍）
# --------------------------------------------------------------------------
def _load_probe(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    items = data.get("items") if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise ValueError("probe 文件里没有 items 列表")
    return [x for x in items if isinstance(x, dict) and x.get("id")]


def _probe_citations(items: list[dict]) -> dict[str, list[dict]]:
    """按 clause_ids 从知识库取条款正文。不在文件里复制正文，避免与库漂移。"""
    mapping: dict[str, list[dict]] = {}
    for item in items:
        cites = []
        for cid in item.get("clause_ids") or []:
            row = db.query_one("SELECT * FROM clauses WHERE clause_id = ?", (cid,))
            if row is not None:
                cites.append(db.clause_to_dict(row))
            else:
                print(f"[judge] 警告：probe {item['id']} 引用的条款 {cid} 不在知识库里，已跳过")
        mapping[str(item["id"])] = cites
    return mapping


def run_probe(
    path: Path = PROBE_FILE,
    out: Path = PROBE_REPORT,
    *,
    limit: int | None = None,
    quiet: bool = False,
    judge_fn=None,
) -> dict:
    """拿人工造的答案测 judge 的召回率与误报率。

    返回 dict：
        executed  bool
        reason    未执行原因
        recall    有幻觉的条目里，judge 报出幻觉的比例
        fp_rate   干净条目里，judge 误报幻觉的比例
        missed    漏掉的条目 id
    """
    try:
        items = _load_probe(path)
    except FileNotFoundError:
        return {"executed": False, "reason": f"找不到校准集 {path}"}
    except Exception as exc:
        return {"executed": False, "reason": f"校准集读不了：{exc}"}
    if not items:
        return {"executed": False, "reason": f"校准集 {path} 里没有条目"}
    if limit is not None:
        items = items[: max(0, limit)]

    if judge_fn is None:
        if not llm.is_running():
            return {
                "executed": False,
                "reason": f"Ollama 未就绪（{config.OLLAMA_BASE_URL} 连不上），召回率校准未执行",
            }
        judge_fn = judge_answer

    rows: list[dict] = []
    with _temp_db(True):
        _prepare_db()
        cites = _probe_citations(items)
        total = len(items)
        for i, item in enumerate(items, 1):
            qid = str(item["id"])
            expect = bool(item.get("expect_hallucination"))
            row = {
                "id": qid,
                "kind": item.get("kind", ""),
                "expect": expect,
                "question": item.get("question", ""),
                "answer": item.get("answer", ""),
                "citation_ids": item.get("clause_ids") or [],
            }
            verdict = judge_fn(row["question"], row["answer"], cites.get(qid) or [])
            if verdict.get("error"):
                row["error"] = verdict["error"]
            else:
                row["flagged"] = bool(verdict.get("hallucinated"))
                row["verdict"] = verdict
                row["correct"] = row["flagged"] == expect
            rows.append(row)
            if not quiet:
                if row.get("error"):
                    print(f"[probe] {i:02d}/{total} {qid}  未判定：{row['error'][:80]}")
                else:
                    mark = "✅" if row["correct"] else "XX"
                    print(f"[probe] {i:02d}/{total} {qid}  期望={'有幻觉' if expect else '干净'}  "
                          f"judge={'有幻觉' if row['flagged'] else '无幻觉'}  {mark}  {row['kind']}")

    judged = [r for r in rows if "flagged" in r]
    positives = [r for r in judged if r["expect"]]
    negatives = [r for r in judged if not r["expect"]]
    caught = [r for r in positives if r["flagged"]]
    false_alarms = [r for r in negatives if r["flagged"]]
    recall = (len(caught) / len(positives)) if positives else None
    fp_rate = (len(false_alarms) / len(negatives)) if negatives else None

    # 分层：模型自己判出来的 vs 确定性判据查出来的。分开报，才知道该往哪层投入。
    def _layer(row: dict) -> tuple[bool, bool]:
        v = row.get("verdict") or {}
        det = bool((v.get("deterministic") or {}).get("flagged"))
        model = bool(v.get("model_flagged"))
        if "model_flagged" not in v:  # 自测注入的假 judge 没有这两个键，退回总判定
            model = bool(row.get("flagged")) and not det
        return model, det

    model_caught = sum(1 for r in positives if _layer(r)[0])
    det_caught = sum(1 for r in positives if _layer(r)[1])
    both_caught = sum(1 for r in positives if all(_layer(r)))

    result = {
        "executed": bool(judged),
        "reason": "" if judged else "一道题都没判出来",
        "rows": rows,
        "stats": {
            "total": len(rows),
            "judged": len(judged),
            "positives": len(positives),
            "negatives": len(negatives),
            "caught": len(caught),
            "recall": recall,
            "false_alarms": len(false_alarms),
            "fp_rate": fp_rate,
            "missed": [r["id"] for r in positives if not r["flagged"]],
            "false_alarm_ids": [r["id"] for r in false_alarms],
            "layer_model_only": model_caught,
            "layer_deterministic": det_caught,
            "layer_both": both_caught,
        },
        "provenance": provenance(),
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(_render_probe(result), encoding="utf-8")
    return result


def _render_probe(result: dict) -> str:
    stats = result["stats"]
    prov = result.get("provenance") or {}
    lines = [
        "# judge 召回率校准报告（已知答案，不是评测集）",
        "",
        *_provenance_lines(prov),
        "- 用途：回答「judge 到底能不能抓到幻觉」。它不进 CI、不进红线。",
        "",
    ]
    if not result.get("executed"):
        lines += [f"**未执行**：{result.get('reason', '')}", "",
                  "本次没有任何结论。不要把它当成通过。", ""]
        return "\n".join(lines)

    recall = stats["recall"]
    fp = stats["fp_rate"]
    lines += [
        "## 结论",
        "",
        f"- 有幻觉的条目标出率（召回）：{stats['caught']}/{stats['positives']}"
        f"（{recall:.0%}）" if recall is not None else "- 召回：无样本",
        f"- 干净条目误报率：{stats['false_alarms']}/{stats['negatives']}"
        f"（{fp:.0%}）" if fp is not None else "- 误报：无样本",
        "",
        "> 这两个数字必须一起看：只报召回，判得越严越好看；只报误报，"
        "一个永远说「没问题」的 judge 最完美。",
        "",
    ]
    if stats["missed"]:
        lines.append("- 漏掉的条目（expect_hallucination=true 但 judge 没报）："
                     + "、".join(stats["missed"]))
    if stats["false_alarm_ids"]:
        lines.append("- 误报的条目（干净答案被判成有幻觉）：" + "、".join(stats["false_alarm_ids"]))
    lines += ["", "## 逐条", "",
              "| 结果 | 编号 | 类型 | 期望 | judge | 说明 |",
              "|---|---|---|---|---|---|"]
    for row in result["rows"]:
        if "flagged" not in row:
            lines.append(f"| ⚠️ | {_cell(row['id'])} | {_cell(row['kind'])} | - | 未判定 | "
                         f"{_cell(row.get('error', ''))} |")
            continue
        lines.append(
            f"| {'✅' if row['correct'] else 'XX'} | {_cell(row['id'])} | {_cell(row['kind'])} | "
            f"{'有幻觉' if row['expect'] else '干净'} | "
            f"{'有幻觉' if row['flagged'] else '无幻觉'} | "
            f"{_cell((row.get('verdict') or {}).get('reason', ''))} |"
        )
    lines += ["", "## 为什么需要这套东西", "",
              "dev 集那些答案本来就没有幻觉，用它们算出的「幻觉率」只能证明 judge 不误报。"
              "要证明它能报，必须喂它真的幻觉——而真的幻觉不会自己出现在评测集里，"
              "所以这批答案由人工编写，一半注入条款里没有的具体事实。", ""]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 自测：不联网，验证「判卷 → 统计 → 一致率 → 报告」整条流水线
# --------------------------------------------------------------------------
def _self_test() -> int:
    questions = [
        {"id": "t01", "question": "问题一（干净答案）"},
        {"id": "t02", "question": "问题二（编造答案）"},
    ]

    def fake_answer(question: str) -> dict:
        return {
            "answer": f"针对「{question}」的回答：应建立安全管理制度。",
            "citations": [{
                "clause_id": "8.1.4.1",
                "title": "安全管理制度",
                "text": "应建立安全管理制度，明确责任部门。",
                "text_type": "summary",
            }],
            "mode": "llm",
            "found": True,
            "risks": [],
        }

    def fake_judge(question: str, answer: str, citations: list) -> dict:
        if "问题二" in question:
            return {
                "faithful": 3, "no_fabrication": 2, "relevant": 4,
                "unsupported_facts": ["30 天内完成备案"],
                "reason": "出现了条款里没有的时限",
                "hallucinated": True,
            }
        return {
            "faithful": 5, "no_fabrication": 5, "relevant": 5,
            "unsupported_facts": [], "reason": "有条款依据、切题",
            "hallucinated": False,
        }

    rows = _judge_rows(questions, fake_answer, fake_judge, quiet=True)
    stats = _stats(rows)
    checks = [
        ("判卷数", stats["judged"] == 2, stats),
        ("幻觉率分子", stats["hallucinations"] == 1, stats),
        ("幻觉率分母", stats["substantive"] == 2, stats),
        ("幻觉率", stats["rate"] == 0.5, stats),
    ]
    annotations = {
        "t01": {"id": "t01", "hallucination": False, "faithful": 5, "relevant": 5},
        "t02": {"id": "t02", "hallucination": False, "faithful": 3, "relevant": 4},  # 故意与 judge 不一致
    }
    agreement = _agreement(rows, annotations)
    checks.append(("一致率", agreement is not None and agreement["agree"] == 1
                   and agreement["total"] == 2, agreement))

    result = {"executed": True, "rows": rows, "stats": stats}
    import tempfile

    with tempfile.TemporaryDirectory(prefix="judge_selftest_") as tmp:
        out = Path(tmp) / "report.md"
        rows_out = Path(tmp) / "rows.json"
        _write_outputs(result, out, rows_out, annotations)
        text = out.read_text(encoding="utf-8")
        checks.append(("报告含幻觉率", "幻觉率：1/2" in text, None))
        checks.append(("报告含一致率", "1/2（50%）" in text, None))
        checks.append(("rows 可回读", json.loads(rows_out.read_text(encoding="utf-8"))["stats"]["judged"] == 2, None))

        # ---- 召回率校准的统计口径（--probe）----
        # 用 4 条合成条目 + 假 judge 验证召回/误报的算法本身：
        # p1 期望有幻觉、judge 报了 → 抓到
        # p2 期望有幻觉、judge 没报 → 漏掉
        # p3 期望干净、judge 报有幻觉 → 误报
        # p4 期望干净、judge 没报 → 正确
        probe_items = [
            {"id": "p1", "kind": "编造", "question": "q1", "answer": "a1",
             "clause_ids": [], "expect_hallucination": True},
            {"id": "p2", "kind": "编造", "question": "q2", "answer": "a2",
             "clause_ids": [], "expect_hallucination": True},
            {"id": "p3", "kind": "干净", "question": "q3", "answer": "a3",
             "clause_ids": [], "expect_hallucination": False},
            {"id": "p4", "kind": "干净", "question": "q4", "answer": "a4",
             "clause_ids": [], "expect_hallucination": False},
        ]
        probe_path = Path(tmp) / "probe.json"
        probe_path.write_text(
            json.dumps({"items": probe_items}, ensure_ascii=False), encoding="utf-8"
        )
        flagged = {"p1", "p3"}

        def fake_probe_judge(question: str, answer: str, citations: list[dict]) -> dict:
            hit = question in ("q1", "q3")
            return {
                "faithful": 5, "no_fabrication": 3 if hit else 5, "relevant": 5,
                "unsupported_facts": [], "reason": "自测", "hallucinated": hit,
            }

        probe_out = Path(tmp) / "probe.md"
        probe = run_probe(probe_path, probe_out, quiet=True, judge_fn=fake_probe_judge)
        pstats = probe.get("stats") or {}
        checks.append(("校准：执行", probe.get("executed") is True, probe.get("reason")))
        checks.append(("校准：召回 1/2", pstats.get("caught") == 1 and pstats.get("positives") == 2, pstats))
        checks.append(("校准：误报 1/2", pstats.get("false_alarms") == 1 and pstats.get("negatives") == 2, pstats))
        checks.append(("校准：漏掉 p2", pstats.get("missed") == ["p2"], pstats.get("missed")))
        ptext = probe_out.read_text(encoding="utf-8")
        checks.append(("校准：报告含召回", "召回）：1/2" in ptext or "1/2（50%）" in ptext, None))
        checks.append(("校准：报告含误报", "误报率：1/2" in ptext, None))
        checks.append(("校准：报告含漏项", "p2" in ptext, None))

        # ---- O6：事实回查（judge 说「条款里没有」，其实就在条款里）----
        clause = {"clause_id": "8.1.4.1", "text": "应在 30 日内完成备案并留存记录。"}
        check_supported = verify_unsupported_facts(["30 日内完成备案"], [clause])
        check_missing = verify_unsupported_facts(["50 万元罚款"], [clause])
        check_partial = verify_unsupported_facts(["完成年度审计"], [clause])
        checks.append(("事实回查：真在条款里 → supported",
                       check_supported["entries"][0]["verdict"] == "supported", check_supported))
        checks.append(("事实回查：条款里没有 → unsupported",
                       check_missing["entries"][0]["verdict"] == "unsupported", check_missing))
        checks.append(("事实回查：部分命中 → ambiguous",
                       check_partial["entries"][0]["verdict"] == "ambiguous", check_partial))
        # 精确率：1 条 unsupported / (1 unsupported + 1 supported) = 50%
        fact_rows = [
            {"id": "f1", "answer": "x", "verdict": {
                "faithful": 5, "no_fabrication": 3, "relevant": 5, "hallucinated": True,
                "facts_check": check_supported}},
            {"id": "f2", "answer": "y", "verdict": {
                "faithful": 5, "no_fabrication": 3, "relevant": 5, "hallucinated": True,
                "facts_check": check_missing}},
        ]
        fstats = _stats(fact_rows)["facts"]
        checks.append(("事实回查：精确率 1/2", fstats["precision"] == 0.5, fstats))

        # ---- O8：来源指纹 ----
        prov = provenance()
        checks.append(("指纹：有提示词版本", bool(prov.get("prompt_version"))
                       and prov["prompt_version"] != "unknown", prov))
        checks.append(("指纹：有知识库条数", isinstance(prov.get("kb_clauses"), int), prov))
        checks.append(("指纹：报告含代码版本", "代码版本" in text, None))

    failed = [name for name, ok, _ in checks if not ok]
    if failed:
        print("XX 自测失败：" + "、".join(failed))
        return 1
    print("OK 自测通过：判卷 2 题、幻觉率 1/2（50%）、一致率 1/2（50%）、报告与 rows 渲染正常")
    print("   另含：召回率校准的统计口径（1/2 召回、1/2 误报）、事实回查三态、来源指纹")
    print("   注意：自测只验证流水线与统计口径，不代表真实模型判得准。")
    return 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(
        description="模型评审（LLM-as-judge）：逐题判「忠于条款 / 不编造 / 回答切题」"
    )
    parser.add_argument("--limit", type=int, default=None, help="只判前 N 道 hit 题（人工标注用）")
    parser.add_argument("--out", default=str(REPORT_FILE), help="报告路径")
    parser.add_argument("--rows-out", default=str(ROWS_FILE), help="逐题结果 JSON 路径")
    parser.add_argument("--annotations", default=str(HUMAN_FILE), help="人工标注 JSON 路径")
    parser.add_argument("--json", action="store_true", help="只输出 JSON 摘要（不打印逐题进度）")
    parser.add_argument("--quiet", action="store_true", help="不打印逐题进度")
    parser.add_argument("--agreement-only", action="store_true",
                        help="不调模型，用已有 rows 文件 + 人工标注算一致率并重写报告")
    parser.add_argument("--self-test", action="store_true", help="不联网自测（假答案/假评审）")
    parser.add_argument("--probe", action="store_true",
                        help="召回率校准：拿 tests/judge_probe.json 里已知有/无幻觉的答案跑一遍")
    parser.add_argument("--probe-file", default=str(PROBE_FILE), help="校准集路径")
    parser.add_argument("--probe-out", default=str(PROBE_REPORT), help="校准报告路径")
    args = parser.parse_args()

    if args.self_test:
        return _self_test()

    if args.probe:
        probe = run_probe(
            Path(args.probe_file),
            Path(args.probe_out),
            limit=args.limit,
            quiet=args.quiet,
        )
        stats = probe.get("stats") or {}
        if not probe.get("executed"):
            print(f"[probe] 未执行：{probe.get('reason', '')}")
            print("[probe] 不要把它当成通过——召回率这一层没有产出任何结论。")
            return 2
        recall = stats.get("recall")
        fp = stats.get("fp_rate")
        print()
        print(f"[probe] 召回（有幻觉被抓住）：{stats['caught']}/{stats['positives']}"
              + (f"（{recall:.0%}）" if recall is not None else ""))
        print(f"[probe] 误报（干净答案被判有幻觉）：{stats['false_alarms']}/{stats['negatives']}"
              + (f"（{fp:.0%}）" if fp is not None else ""))
        if stats.get("missed"):
            print("[probe] 漏掉：" + "、".join(stats["missed"]))
        print(f"[probe] 报告：{args.probe_out}")
        return 0 if stats.get("judged") else 1

    out = Path(args.out)
    rows_out = Path(args.rows_out)
    annotations = _load_annotations(Path(args.annotations))

    if args.agreement_only:
        try:
            data = json.loads(rows_out.read_text(encoding="utf-8"))
        except FileNotFoundError:
            print(f"[judge] 找不到 {rows_out}，先跑一次不带 --agreement-only 的评审")
            return 2
        if not annotations:
            print(f"[judge] 找不到可匹配的人工标注：{args.annotations}")
            return 2
        result = {
            "executed": data.get("executed", True),
            "reason": data.get("reason", ""),
            "rows": data.get("rows", []),
            # 用当前代码重算，避免旧报告里过时的 low_ids/口径把重写后的报告带偏。
            "stats": _stats(data.get("rows", [])),
        }
        agreement = _write_outputs(result, out, rows_out, annotations)
        if agreement is None:
            print("[judge] 人工标注与本次 rows 没有可匹配的题号")
            return 2
        print(
            f"[judge] judge 与人工标注一致率：{agreement['agree']}/{agreement['total']}"
            f"（{agreement['rate']:.0%}，{'可用' if agreement['usable'] else '不可用'}）"
        )
        if agreement.get("mismatches"):
            print("[judge] 不一致题号：" + "、".join(agreement["mismatches"]))
        print(f"[judge] 报告已重写：{out}")
        return 0

    result = judge_hit_questions(limit=args.limit, quiet=(args.quiet or args.json))
    agreement = _write_outputs(result, out, rows_out, annotations)

    summary = {
        "executed": result.get("executed", False),
        "reason": result.get("reason", ""),
        "stats": result.get("stats"),
        "agreement": agreement,
        "report": str(out),
        "rows": str(rows_out),
    }
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        if not result.get("executed"):
            print(f"[judge] 未执行：{result.get('reason', '')}")
            print("[judge] 不要把它当成通过——这一层没有产出任何评分。")
        else:
            stats = result["stats"]
            print()
            print(f"[judge] 判卷 {stats['judged']}/{stats['total']} 题；"
                  f"幻觉率 {_rate_text(stats)}；无法判定 {stats['failed']} 题")
            print(f"[judge] 报告：{out}")
            if agreement is None:
                print(f"[judge] 未提供人工标注（{args.annotations}）；一致率无法计算")
            else:
                print(f"[judge] 与人工标注一致率：{agreement['agree']}/{agreement['total']}"
                      f"（{agreement['rate']:.0%}，"
                      f"{'可用' if agreement['usable'] else '不可用'}）")

    if not result.get("executed"):
        return 2
    if not result["stats"]["judged"]:
        print("[judge] 一道题都没判出来——这一层等于没跑")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
