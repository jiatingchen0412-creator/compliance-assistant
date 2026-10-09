"""停止服务（供 停用助手.bat 调用）。

为什么用 Python 而不是直接在 bat 里写：
同 tools/first_run.py 的理由——.bat 必须保持纯 ASCII，
中文提示交给 Python 输出（Python 写控制台走宽字符 API，不受代码页影响）。

做的事：找到占用端口的进程 → 结束它 → 顺便把 Ollama 留着不动
（Ollama 是通用服务，用户可能还在用它跑别的模型，不该顺手关掉）。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config  # noqa: E402


def pids_on_port(port: int) -> list[int]:
    """用 netstat 找出监听指定端口的进程号。"""
    try:
        result = subprocess.run(
            ["netstat", "-ano"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
    except Exception as exc:
        print(f"    无法执行 netstat：{exc}")
        return []

    pids: list[int] = []
    for line in result.stdout.splitlines():
        if f":{port}" not in line or "LISTENING" not in line.upper():
            continue
        parts = line.split()
        if not parts:
            continue
        try:
            pid = int(parts[-1])
        except ValueError:
            continue
        if pid not in pids and pid > 0:
            pids.append(pid)
    return pids


def main() -> int:
    port = config.APP_PORT
    print()
    print("=" * 56)
    print("  安全合规自查助手  ·  停止服务")
    print("=" * 56)
    print()
    print(f"正在查找占用端口 {port} 的服务...")

    pids = pids_on_port(port)
    if not pids:
        print("    没有发现正在运行的服务。")
        print("    （可能是已经关掉了，或者根本没启动过）")
        print()
        return 0

    stopped = 0
    for pid in pids:
        result = subprocess.run(
            ["taskkill", "/F", "/PID", str(pid)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if result.returncode == 0:
            print(f"    已停止服务（进程号 {pid}）")
            stopped += 1
        else:
            print(f"    无法停止进程 {pid}：{(result.stderr or result.stdout).strip()[:80]}")

    print()
    if stopped:
        print("服务已停止。")
        print()
        print("提示：Ollama（本地大模型）没有一起关掉，")
        print("      因为它可能还在给别的程序用。不需要的话可以在任务栏右下角退出。")
    else:
        print("没能停止任何进程。可以试试用任务管理器结束 python.exe。")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
