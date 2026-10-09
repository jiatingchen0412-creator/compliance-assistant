"""命令行工具：读写开发日志。

用法（在项目根目录执行）：

    # 记一条已完成
    .venv\\Scripts\\python.exe tools\\devlog.py done "完成检索阈值标定，召回率 25/25"

    # 记一条待办
    .venv\\Scripts\\python.exe tools\\devlog.py todo "给长对话加上下文压缩"

    # 记一条问题/决策
    .venv\\Scripts\\python.exe tools\\devlog.py decision "found 改由服务端按阈值判定，不听模型"

    # 看今天的日志
    .venv\\Scripts\\python.exe tools\\devlog.py show

    # 汇总最近 7 天
    .venv\\Scripts\\python.exe tools\\devlog.py summary --days 7

    # 只确保今天的文件存在（服务启动时会自动调用）
    .venv\\Scripts\\python.exe tools\\devlog.py ensure
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import devlog  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="安全合规自查助手 · 开发日志工具")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("ensure", help="确保今天的日志文件存在")

    for name, help_text in (
        ("done", "记一条已完成事项"),
        ("todo", "记一条待办事项"),
        ("decision", "记一条问题或决策"),
        ("auto", "记一条自动记录"),
        ("goal", "记一条今日目标"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("text", help="内容，可以加引号写一整句")
        p.add_argument("--day", help="指定日期（默认今天），格式 2026-09-28")

    p_show = sub.add_parser("show", help="打印某天的日志")
    p_show.add_argument("--day", help="指定日期（默认今天）")

    p_sum = sub.add_parser("summary", help="汇总最近若干天")
    p_sum.add_argument("--days", type=int, default=7)

    sub.add_parser("list", help="列出所有日志文件")

    args = parser.parse_args()

    if args.cmd == "ensure":
        path = devlog.ensure_today()
        print(f"今天的日志已就绪：{path}")
        return 0

    if args.cmd == "list":
        days = devlog.list_days(limit=365)
        if not days:
            print("还没有任何开发日志。")
            return 0
        print(f"共 {len(days)} 天：")
        for d in days:
            print(f"  {d}   {devlog.today_path(d)}")
        return 0

    if args.cmd == "show":
        print(devlog.read(getattr(args, "day", None)))
        return 0

    if args.cmd == "summary":
        print(devlog.summary(args.days))
        return 0

    mapping = {
        "done": devlog.done,
        "todo": devlog.todo,
        "decision": devlog.decision,
        "auto": devlog.auto,
        "goal": lambda t, d=None: devlog.append("今日目标", t, d, timestamp=False),
    }
    path = mapping[args.cmd](args.text, getattr(args, "day", None))
    print(f"已记入 {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
