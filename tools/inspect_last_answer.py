"""查看最近一次问答的细节，用于排查"回答得不对"。

用法（在项目根目录执行）：
    .venv\\Scripts\\python.exe tools\\inspect_last_answer.py
    .venv\\Scripts\\python.exe tools\\inspect_last_answer.py --index 3   # 倒数第 3 条

会打印出：引用了哪些条款、为什么标了 ⚠️、完整回答。

什么时候用：
- 用户说"它答错了"，你想知道模型实际引用了什么
- 想确认 ⚠️ 标注的依据是否合理
- 想确认某次改动后回答风格有没有变
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="查看最近一次问答的细节")
    parser.add_argument("--index", type=int, default=1, help="倒数第几条回答，默认 1")
    parser.add_argument("--full", action="store_true", help="打印完整回答，不截断")
    args = parser.parse_args()

    db.init_db()
    rows = db.query(
        "SELECT id, content, payload, created_at FROM messages "
        "WHERE role = 'assistant' ORDER BY id DESC LIMIT ?",
        (max(1, args.index),),
    )
    if not rows:
        print("还没有任何回答记录。先在浏览器里问一句试试。")
        return 1

    row = rows[-1]
    payload = db.load_json(row["payload"], {}) or {}

    print("=" * 66)
    print(f"回答时间：{row['created_at']}")
    print(f"模式：{payload.get('mode', '-')}   找到条款：{'是' if payload.get('found') else '否'}"
          f"   需要追问：{'是' if payload.get('need_clarification') else '否'}")
    print("=" * 66)

    print("\n【回答】")
    text = row["content"] or ""
    print(text if args.full else text[:600] + ("…" if len(text) > 600 else ""))

    cites = payload.get("citations") or []
    print(f"\n【引用条款】共 {len(cites)} 条")
    for c in cites:
        tags = "、".join(c.get("risk_tags") or []) or "无"
        print(f"  {c.get('clause_id', ''):<14} {c.get('title', '')[:30]:<32} 标签={tags}")

    risks = payload.get("risks") or []
    print(f"\n【⚠️ 风险标注】共 {len(risks)} 条")
    for r in risks:
        print(f"  {r.get('label', ''):<12} 依据：{r.get('reason', '')}")
        if r.get("clauses"):
            print(f"               相关条款：{'、'.join(r['clauses'])}")
    if not risks:
        print("  （无。日志类、备份类问题本身不属于三类高风险，这是正常的）")

    notes = payload.get("notes") or []
    if notes:
        print(f"\n【技术提示】共 {len(notes)} 条")
        for n in notes:
            print(f"  - {n}")

    if payload.get("followup_questions"):
        print("\n【追问】")
        for q in payload["followup_questions"]:
            print(f"  - {q}")

    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
