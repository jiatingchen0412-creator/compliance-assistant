"""命令行质量自检：跑评测并和红线比对。

用法（在项目根目录执行）：
    .venv\\Scripts\\python.exe tools\\check_quality.py          # 打印表格，不达标退出码 1
    .venv\\Scripts\\python.exe tools\\check_quality.py --quiet  # 只在失败时输出
    .venv\\Scripts\\python.exe tools\\check_quality.py --json   # 输出 JSON，便于脚本调用

什么时候用：
- 改完检索参数（阈值、权重、分词词典）之后
- 换嵌入模型之后
- 交付给用户之前

和 tests/run_eval.py 的区别：
run_eval 是给人看的详细报告；这个是给"判断达标不达标"用的，可以接进自动化流程。
两者用的是同一套评测逻辑（app/quality.py）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config, db, quality  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="检索质量自检")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--quiet", action="store_true", help="只在失败时输出")
    parser.add_argument("--top-k", type=int, default=6)
    args = parser.parse_args()

    db.init_db()
    verdict = quality.run_check(top_k=args.top_k)

    if args.json:
        print(json.dumps(
            {
                "passed": verdict["passed"],
                "skipped": verdict.get("skipped", False),
                "reason": verdict.get("reason", ""),
                "failures": verdict["failures"],
                "items": verdict["items"],
                "stats": verdict.get("stats", {}),
                "config": {
                    "min_score": config.RETRIEVAL_MIN_SCORE,
                    "keyword_weight": config.HYBRID_KEYWORD_WEIGHT,
                    "vector_weight": config.HYBRID_VECTOR_WEIGHT,
                },
            },
            ensure_ascii=False,
            indent=2,
        ))
        return 0 if verdict["passed"] else 1

    if verdict.get("skipped"):
        if not args.quiet:
            print(f"质量自检已跳过：{verdict.get('reason', '')}")
        return 0

    if not args.quiet:
        print("=" * 62)
        print("检索质量自检 · 对标 docs/07-测试与验收规范.md 的红线")
        print("=" * 62)
        print(f"当前参数：阈值 {config.RETRIEVAL_MIN_SCORE} · "
              f"关键词权重 {config.HYBRID_KEYWORD_WEIGHT} · "
              f"语义权重 {config.HYBRID_VECTOR_WEIGHT}")
        print()

    for item in verdict["items"]:
        if args.quiet and item["passed"]:
            continue
        flag = "OK" if item["passed"] else "XX"
        print(f"{flag}  {item['name']:<16} {item['actual']}/{item['total']}   "
              f"要求 {item['required']}")

    # 失败时把具体哪几题挂了打出来，方便定位
    if not verdict["passed"]:
        print()
        print("未通过的题目：")
        for row in verdict.get("rows", []):
            if not row["ok"]:
                print(f"  {row['id']}  {row['question'][:30]}")
                print(f"        {row['detail']}")

    if not args.quiet:
        print()
        print(f"结论：{'✅ 达标' if verdict['passed'] else '❌ 跌破红线'}")

    return 0 if verdict["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
