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
import sys
import time
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config, db, ingest, llm, quality  # noqa: E402

REPORT_FILE = ROOT / "tools" / "_judge_report.md"
ROWS_FILE = ROOT / "tools" / "_judge_rows.json"
HUMAN_FILE = ROOT / "tools" / "_judge_human.json"

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

JUDGE_RUBRIC = """【评分标准（1-5 分，只给整数）】
- faithful（忠于条款）：回答里的每个事实陈述都能在引用条款原文中找到依据 → 5；
  有少量概括但无矛盾 → 3；出现与原文矛盾、或引用条款根本不支持该陈述 → 1。
- no_fabrication（不编造）：没有出现引用条款和用户问题之外的具体事实
  （数字、时限、机构名、价格、认证/测评结论） → 5；只有泛化表述 → 3；
  出现至少一条无依据的具体事实 → 1-2。
- relevant（回答切题）：正面回应用户问题 → 5；只沾边 → 3；答非所问 → 1。
- unsupported_facts：逐条抄出回答中「无依据的具体事实」原句；没有就给空数组。
- reason：不超过 80 字，指出扣分点；全 5 分就写「有条款依据、切题」。

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
    # 「不编造」≤2 或明确列出无依据事实，都算幻觉。
    hallucinated = bool(facts) or scores["no_fabrication"] <= 2
    return {
        **scores,
        "unsupported_facts": facts,
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
    return {
        "total": len(rows),
        "judged": len(judged),
        "failed": len(rows) - len(judged),
        "substantive": len(substantive),
        "hallucinations": len(hallucinations),
        "rate": rate,
        "means": means,
        "low_ids": [r["id"] for r in low],
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

    return {"executed": True, "rows": rows, "stats": _stats(rows)}


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


def _rate_text(stats: dict) -> str:
    if not stats.get("substantive"):
        return "无法判定（没有可计入的回答）"
    return f"{stats['hallucinations']}/{stats['substantive']}（{stats['rate']:.0%}）"


def render_report(result: dict, agreement: dict | None = None) -> str:
    lines = [
        "# 模型评审报告（LLM-as-judge，手工层）",
        "",
        f"- 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 评审模型：{config.LLM_MODEL}（{config.OLLAMA_BASE_URL}）",
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

    failed = [name for name, ok, _ in checks if not ok]
    if failed:
        print("XX 自测失败：" + "、".join(failed))
        return 1
    print("OK 自测通过：判卷 2 题、幻觉率 1/2（50%）、一致率 1/2（50%）、报告与 rows 渲染正常")
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
    args = parser.parse_args()

    if args.self_test:
        return _self_test()

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
