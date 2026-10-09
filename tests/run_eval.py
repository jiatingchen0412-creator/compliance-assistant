"""检索与回答质量评测（回归测试）。

用法（在项目根目录执行）：
    .venv\\Scripts\\python.exe tests\\run_eval.py              # 两层都跑（Ollama 没开就只跑第一层）
    .venv\\Scripts\\python.exe tests\\run_eval.py --no-llm     # 只跑第一层，秒出
    .venv\\Scripts\\python.exe tests\\run_eval.py --limit 6    # 第二层只跑前 6 题

产出：
    tests/eval_report.md   —— 可读的评测报告
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
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config, db, ingest, quality  # noqa: E402
from app.retrieval import get_engine  # noqa: E402

REPORT_FILE = Path(__file__).resolve().parent / "eval_report.md"

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


def build_report(result: dict, verdict: dict, e2e: dict | None, e2e_verdict: dict | None) -> str:
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
    if not args.no_llm:
        print()
        print("[评测] 第二层 · 端到端评测（要调大模型，较慢；用 --no-llm 可跳过）…")
        e2e = quality.evaluate_end2end(limit=args.limit)
        if e2e.get("skipped"):
            print(f"[评测] 已跳过：{e2e['reason']}")
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

    REPORT_FILE.write_text(build_report(result, verdict, e2e, e2e_verdict), encoding="utf-8")
    print()
    print(f"[评测] 报告已写入 {REPORT_FILE}")

    failures = list(verdict["failures"])
    if e2e_verdict is not None:
        failures += e2e_verdict["failures"]
    if not failures:
        print("[评测] ✅ 全部指标达标")
        return 0
    print("[评测] ❌ 指标跌破红线：" + "；".join(failures))
    return 1


if __name__ == "__main__":
    sys.exit(main())
