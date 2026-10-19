"""检索与回答质量评测（回归测试）。

用法（在项目根目录执行）：
    .venv\\Scripts\\python.exe tests\\run_eval.py              # 两层都跑；Ollama 没开则层 2 未执行、退出码 3
    .venv\\Scripts\\python.exe tests\\run_eval.py --no-llm     # 只跑第一层，秒出；退出码 0 只代表层 1 通过
    .venv\\Scripts\\python.exe tests\\run_eval.py --limit 6    # 第二层只跑前 6 题

退出码（别把 0 理解成"提示词也没问题"）：
    0 = 本次要求的层全部通过（--no-llm 时只代表层 1）
    1 = 有指标跌破红线
    3 = 层 2 / 模型评审本应执行但没执行（如 Ollama 未运行）——结果不完整，不能当"全部达标"

产出：
    tests/eval_report.md   —— 可读的评测报告
    tools/_judge_report.md —— 模型评审报告（第二层附加，手工层）
    控制台表格

评测分两层，这是刻意的设计：

    第一层（检索层，不需要大模型，秒级）—— 六项指标
        召回率         该命中的条款有没有出现在前 K 条里
        拒答正确率     无关问题是否老老实实说"找不到"（防编造的核心，一票否决）
        ⚠️ 风险标注率   该打 ⚠️ 的有没有打上
        库外法规识别率  点名了知识库里没有的法规时，有没有识别出来
        适用性判定拦截率 "这条对我适用吗"这类问题，有没有拒绝硬答
        ⚠️ 误标率       即使模型见谁都标，也不该标出无关的风险标签

    第二层（端到端层，需要 Ollama，分钟级）
        追问正确率      信息不足的问题有没有先追问
        库外法规声明率   回答正文里真的写了"未收录"，且没有转述条文内容

    第二层附加（模型评审 / LLM-as-judge，需要 Ollama，手工层，不进 CI）
        幻觉率          judge 判「回答里出现无依据具体事实」的题数 / 有回答的判卷题数
        三个维度        忠于条款 / 不编造 / 回答切题（1-5 分）
        为什么手工：CI 不装 Ollama（铁律三），提示词/换模型/JSON 兜底退化
        在 CI 里没有任何信号；这一层只在发布前由人跑，且只作参考、不进红线。

为什么必须分两层：第一层用**被评测的同一个阈值**来定义"正确答案"是危险的闭环自证，
它结构上不可能发现"阈值过了但不该答"的问题——库外法规、适用性判定这两类问题
检索分数都很高（0.53~0.97），恰恰是阈值闸门拦不住的。所以第一层里新增的
out_of_kb / undeterminable / risk_forbid 三类检查**都不看分数**，只看行为。
第二层再补上"用户最终看到的那段话里到底写了什么"。

评测逻辑本身放在 app/quality.py，这样启动自检和本脚本用的是同一套代码，
不会出现"两份评测各自漂移"的问题。
"""
from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config, db, ingest, quality  # noqa: E402
from app.retrieval import get_engine  # noqa: E402

REPORT_FILE = Path(__file__).resolve().parent / "eval_report.md"

def _sha256_file(path: Path) -> str:
    """文件 sha256（越小写越好读；只用于「跑前/跑后是不是同一个库」）。"""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _isolate_db() -> tuple[Path, Path, str]:
    """把数据库指到真实库的临时副本，返回 (临时目录, 真实库路径, 跑之前的哈希)。

    为什么必须这么做：评测会 init_db / 灌种子 / 读会话，旧版直接跑在
    data/app.db 上——测试污染用户数据这件事，我们已经在 test_edge_cases
    上吃过一次亏（真实库 sha256 从 4C3A32… 变成 8E83E4…）。评测脚本没有
    理由例外。副本连 -wal / -shm 一起拷，避免漏掉未落盘的事务。
    """
    real = Path(config.DB_PATH)
    real_hash = _sha256_file(real) if real.exists() else ""
    tmp_dir = Path(tempfile.mkdtemp(prefix="run_eval_db_"))
    tmp_db = tmp_dir / "app.db"
    for suffix in ("", "-wal", "-shm"):
        src = Path(str(real) + suffix)
        if src.exists():
            shutil.copy2(src, Path(str(tmp_db) + suffix))
    config.DB_PATH = tmp_db
    return tmp_dir, real, real_hash


def _finish_db_guard(code: int, real_db: Path, hash_before: str) -> int:
    """收尾：真实库要是被改了，一律当失败（非 0 退出），不遮不掩。"""
    if not hash_before:
        print("[隔离] 找不到真实库，跳过「未被改动」校验（首次运行请先启动一次服务）")
        return code
    hash_after = _sha256_file(real_db) if real_db.exists() else ""
    if hash_after != hash_before:
        print(f"XX 真实库被本次评测改动了：{real_db}")
        print(f"   跑之前 sha256 {hash_before[:16]}…")
        print(f"   跑之后 sha256 {hash_after[:16]}…")
        print("   这违反「测试不污染用户数据」；说明隔离没生效，别把它当小问题。")
        return 2
    print(f"[隔离] 真实库未被评测改动：{real_db}（sha256 {hash_after[:16]}…）")
    return code



# 逐题明细里"期望"一栏的中文显示
EXPECT_LABEL = {
    "hit": "召回",
    "reject": "拒答",
    "out_of_kb": "库外法规",
    "undeterminable": "适用性判定",
    "clarify": "需追问",
}


def expect_label(row: dict) -> str:
    """给一行结果配一个人类可读的期望标签。"""
    if row.get("forbid"):
        return "防误标"
    return EXPECT_LABEL.get(row.get("expect", ""), row.get("expect", ""))


def _judge_rate_text(stats: dict) -> str:
    """幻觉率的人类可读形式；没有可计入的回答时明确写「无法判定」。"""
    if not stats.get("substantive"):
        return "无法判定（没有可计入的回答）"
    return f"{stats['hallucinations']}/{stats['substantive']}（{stats['rate']:.0%}）"


def build_report(result: dict, verdict: dict, e2e: dict | None, e2e_verdict: dict | None,
                 judge: dict | None = None) -> str:
    rows, stats = result["rows"], result["stats"]
    lines = [
        "# 安全合规自查助手 · 评测报告",
        "",
        f"- 评测时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 条款总数：{db.query_one('SELECT COUNT(*) AS c FROM clauses')['c']}",
        f"- 拒答阈值：{config.RETRIEVAL_MIN_SCORE}",
        f"- 关键词/语义权重：{config.HYBRID_KEYWORD_WEIGHT} / {config.HYBRID_VECTOR_WEIGHT}",
        f"- 语义检索：{'启用' if get_engine().vector_available else '未启用'}",
        "",
        "## 结论",
        "",
    ]

    def verdict_line(v: dict, label: str) -> str:
        if v is None:
            return f"- {label}：未运行"
        if v.get("skipped"):
            return f"- {label}：已跳过（{v.get('reason', '')}）"
        return (
            f"- {label}：**{'✅ 全部达标' if v['passed'] else '❌ 有指标跌破红线'}**"
            + ("" if v["passed"] else "；" + "；".join(v["failures"]))
        )

    lines.append(verdict_line(verdict, "第一层 · 检索"))
    lines.append(verdict_line(e2e_verdict, "第二层 · 端到端"))
    if e2e_verdict is None:
        lines.append("- ⚠️ 第二层 · 端到端：未执行（本报告不代表提示词/模型输出质量）")
    if judge is not None:
        if judge.get("executed"):
            lines.append(
                f"- 第二层附加 · 模型评审：已执行（幻觉率 {_judge_rate_text(judge['stats'])}；参考值，不是红线）"
            )
        else:
            lines.append(f"- 第二层附加 · 模型评审：未执行（{judge.get('reason', '')}）")
    lines.append("")

    def metrics_table(v: dict, label: str) -> None:
        lines.extend([
            f"## {label}指标汇总",
            "",
            "| 指标 | 结果 | 红线 | 状态 |",
            "|---|---|---|---|",
        ])
        for item in v["items"]:
            lines.append(
                f"| {item['name']} | {item['actual']}/{item['total']} | {item['required']} | "
                f"{'✅' if item['passed'] else '❌'} |"
            )
        lines.append("")

    metrics_table(verdict, "第一层 · 检索")

    lines += [
        "## 逐题明细（第一层）",
        "",
        "| 判定 | 编号 | 问题 | 期望 | 实际 | 最高分 | 命中条款 | 说明 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(
            f"| {'✅' if r['ok'] else '❌'} | {r['id']} | {r['question'][:26]} | {expect_label(r)} | "
            f"{'命中' if r['found'] else '拒答'} | {r['best']:.3f} | "
            f"{'<br>'.join(r['top'][:2])} | {r['detail']} |"
        )

    if e2e_verdict is not None and not e2e_verdict.get("skipped"):
        metrics_table(e2e_verdict, "第二层 · 端到端")
        lines += [
            "## 逐题明细（第二层 · 大模型真的答了什么）",
            "",
            "| 判定 | 编号 | 模式 | 追问 | 引用条款 | ⚠️ | 回答开头 | 问题 |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for r in (e2e or {}).get("rows", []):
            if "error" in r:
                lines.append(f"| ❌ | {r['id']} | 出错 | | | | {r['error'][:60]} | |")
                continue
            ok = not r.get("problem")
            lines.append(
                f"| {'✅' if ok else '❌'} | {r['id']} | {r['mode']} | "
                f"{'是' if r['clarify'] else '否'} | {', '.join(r['citations']) or '-'} | "
                f"{', '.join(r['risks']) or '-'} | {r['answer_head'][:60]} | {r.get('problem', '')} |"
            )
    elif e2e_verdict is not None:
        lines += [
            "## 第二层 · 端到端",
            "",
            f"未运行：{e2e_verdict.get('reason', '')}",
        ]

    # -------- 第二层附加：模型评审（手工层，不进 CI） --------
    lines += ["", "## 第二层附加 · 模型评审与幻觉率（手工）", ""]
    if judge is None:
        lines.append("未执行（本次没有进入模型评审）。")
    elif not judge.get("executed"):
        lines += [
            f"**未执行**：{judge.get('reason', '')}",
            "",
            "> 幻觉率缺失，本报告不能代表答案质量。**不要把它当成通过。**",
        ]
    else:
        st = judge["stats"]
        lines += [
            f"- 判卷：{st['judged']}/{st['total']} 题（无法判定 {st['failed']} 题）",
            f"- **幻觉率：{_judge_rate_text(st)}**"
            "（分子 = 出现无依据具体事实的题数；分母 = 有回答的判卷题数）",
            f"- 平均分：忠于条款 {st['means']['faithful']} / "
            f"不编造 {st['means']['no_fabrication']} / 回答切题 {st['means']['relevant']}",
            "",
            "| 编号 | 引用条款 | 忠于条款 | 不编造 | 回答切题 | 幻觉 | 问题 |",
            "|---|---|---|---|---|---|---|",
        ]
        for row in judge.get("rows", []):
            if "verdict" not in row:
                lines.append(f"| {row.get('id', '')} | - | - | - | - | 未判定 | "
                             f"{row.get('error', '')[:60]} |")
                continue
            v = row["verdict"]
            lines.append(
                f"| {row['id']} | {', '.join(row.get('citation_ids', [])) or '-'} | "
                f"{v['faithful']} | {v['no_fabrication']} | {v['relevant']} | "
                f"{'是' if row.get('hallucinated') else '否'} | {row.get('question', '')[:26]} |"
            )
        lines += [
            "",
            "评审报告（含低分题全文）：`tools/_judge_report.md`；"
            "人工标注与一致率：`tools/llm_judge.py --agreement-only`。",
            "这一层只在本地手工跑，**不进 CI，也不进红线**。",
        ]

    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-llm", action="store_true",
                        help="只跑第一层检索评测，不调大模型")
    # 保留 --llm 只为兼容 docs 与肌肉记忆：第二层现在默认就跑。
    parser.add_argument("--llm", action="store_true",
                        help="（已废弃，第二层默认就跑；保留只为兼容旧命令）")
    parser.add_argument("--limit", type=int, default=None,
                        help="第二层只跑前 N 题（调试用）")
    parser.add_argument("--top-k", type=int, default=6)
    args = parser.parse_args()

    tmp_dir, real_db, hash_before = _isolate_db()
    try:
        code = _run(args)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    return _finish_db_guard(code, real_db, hash_before)


def _run(args: argparse.Namespace) -> int:
    """跑一次评测。数据库已由 main() 指到临时副本，这里不用管。"""

    db.init_db()
    seed = ingest.ensure_seed_loaded()
    if not seed.get("skipped"):
        print("[评测] 已导入种子知识库")

    # ---------------- 第一层：检索 ----------------
    print("[评测] 第一层 · 检索评测…")
    get_engine().warmup()
    result = quality.evaluate_retrieval(top_k=args.top_k)
    if result.get("error"):
        print(f"[评测] 无法评测：{result['error']}")
        return 1
    verdict = quality.check_red_lines(result["stats"])

    print()
    print("=" * 100)
    for r in result["rows"]:
        mark = "OK " if r["ok"] else "XX "
        state = "命中" if r["found"] else "拒答"
        print(f"{mark}{r['id']}  {r['question'][:34]:<36} {expect_label(r):<11}{state}  {r['best']:.3f}")
        if r["detail"]:
            print(f"      └─ {r['detail'][:110]}")
    print("=" * 100)
    for item in verdict["items"]:
        flag = "OK" if item["passed"] else "XX"
        print(f"{flag}  {item['name']:<16} {item['actual']}/{item['total']}   要求 {item['required']}")

    # ---------------- 第二层：端到端 ----------------
    e2e = e2e_verdict = None
    layer2_expected = not args.no_llm
    if layer2_expected:
        print()
        print("[评测] 第二层 · 端到端评测（要调大模型，较慢；用 --no-llm 可跳过）…")
        e2e = quality.evaluate_end2end(limit=args.limit)
        if e2e.get("skipped"):
            print(f"[评测] 层 2 未执行：{e2e['reason']}")
            print("[评测] 注意：本次结果只覆盖层 1（检索/判定），不能代表提示词与模型输出质量")
        else:
            e2e_verdict = quality.check_end2end_red_lines(e2e["stats"])
            print("=" * 100)
            for r in e2e["rows"]:
                if "error" in r:
                    print(f"XX {r['id']}  出错: {r['error'][:80]}")
                    continue
                flag = "OK" if not r.get("problem") else "XX"
                print(f"{flag} {r['id']}  {r['mode']:<15} 引用={','.join(r['citations']) or '-':<28} "
                      f"⚠️={','.join(r['risks']) or '-'}")
                if r.get("problem"):
                    print(f"      └─ {r['problem']}")
            print("=" * 100)
            for item in e2e_verdict["items"]:
                flag = "OK" if item["passed"] else "XX"
                print(f"{flag}  {item['name']:<16} {item['actual']}/{item['total']}   要求 {item['required']}")

    # ---------------- 第二层附加：模型评审与幻觉率（手工层） ----------------
    judge: dict | None = None
    if args.no_llm:
        judge = {"executed": False, "reason": "--no-llm：本次未执行模型评审"}
    elif e2e_verdict is None:
        judge = {
            "executed": False,
            "reason": f"层 2 未执行（{(e2e or {}).get('reason', '')}），模型评审未执行",
        }
    else:
        print()
        print("[评测] 第二层附加 · 模型评审（LLM-as-judge，重新答题判卷；手工层，不进 CI）…")
        try:
            sys.path.insert(0, str(ROOT / "tools"))
            import llm_judge  # noqa: E402

            judge = llm_judge.judge_hit_questions(limit=args.limit, isolate_db=False)
        except Exception as exc:  # 评审工具自身坏了也不能装作没发生
            judge = {"executed": False,
                     "reason": f"评审工具异常：{type(exc).__name__}: {exc}"}
        if judge.get("executed"):
            st = judge["stats"]
            print(f"[评测] 幻觉率：{_judge_rate_text(st)}；判卷 {st['judged']} 题，"
                  f"无法判定 {st['failed']} 题")
            for row in judge["rows"]:
                if "verdict" not in row:
                    print(f"      {row['id']}  未判定：{row.get('error', '')[:80]}")
                    continue
                v = row["verdict"]
                refs = ",".join(row.get("citation_ids", [])[:2]) or "-"
                print(f"      {row['id']}  引用={refs:<22} "
                      f"评分 {v['faithful']}/{v['no_fabrication']}/{v['relevant']}  "
                      f"{'幻觉' if row.get('hallucinated') else '无幻觉'}")
            print(f"[评测] 评审报告：{llm_judge.REPORT_FILE}")
        else:
            print(f"[评测] 模型评审未执行：{judge.get('reason', '')}")
            print("[评测] 注意：幻觉率缺失，本次结果不完整（不能当成答案质量结论）")

    REPORT_FILE.write_text(
        build_report(result, verdict, e2e, e2e_verdict, judge), encoding="utf-8"
    )
    print()
    print(f"[评测] 报告已写入 {REPORT_FILE}")

    failures = list(verdict["failures"])
    if e2e_verdict is not None:
        failures += e2e_verdict["failures"]
    if failures:
        print("[评测] ❌ 指标跌破红线：" + "；".join(failures))
        return 1
    if args.no_llm:
        print("[评测] 层 1 指标达标（--no-llm：层 2 端到端未执行，本结果不代表提示词/模型质量）")
        return 0
    if e2e_verdict is None:
        print("[评测] 层 1 指标达标，但层 2 端到端未执行 —— 结果不完整，不能算「全部达标」")
        return 3
    if judge is not None and not judge.get("executed"):
        print("[评测] 层 1/层 2 通过，但模型评审未执行 —— 幻觉率缺失，结果不完整")
        return 3
    if judge is not None and not (judge.get("stats") or {}).get("judged"):
        print("[评测] 模型评审一道题都没判出来 —— 幻觉率不可用，结果不完整")
        return 3
    print("[评测] ✅ 两层指标全部达标（含模型评审；幻觉率为参考值，不是红线）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
