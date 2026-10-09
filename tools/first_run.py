"""首次运行安装向导。

为什么要单独有这个脚本：
启动脚本 `启动助手.bat` 必须是**纯 ASCII**（cmd.exe 按当前控制台代码页读 .bat，
文件里有中文而代码页不匹配时，命令会被撕碎，双击后什么都不发生）。
但用户是中文用户，安装过程必须看得懂。

解法：让 .bat 只做最少的判断，然后把控制权交给 Python。
Python 3.6+ 在 Windows 上通过宽字符 API 写控制台，**不受代码页影响**，
所以这里可以放心输出中文。

流程：检查 Python → 生成 .env → 创建虚拟环境 → 安装依赖 → 启动程序
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path

# 这个脚本是交给**还没建好虚拟环境的系统 Python** 跑的，所以不能 import app 里的东西。
# 但下面要打印 ✗ 这类 GBK 里没有的符号：输出一旦被重定向（管道、写进文件），
# Python 就按 GBK 编码去写，会直接 UnicodeEncodeError 把安装向导带崩。
# 所以在这里就地设一次编码，只依赖标准库。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent.parent
VENV_PY = ROOT / ".venv" / "Scripts" / "python.exe"
REQUIREMENTS = ROOT / "requirements.txt"
ENV_FILE = ROOT / ".env"
ENV_EXAMPLE = ROOT / ".env.example"


def line(char: str = "=", width: int = 60) -> None:
    print(char * width)


def banner() -> None:
    line()
    print("        安全合规自查助手  ·  首次运行安装向导")
    line()
    print()


def run(cmd: list[str], desc: str) -> bool:
    """执行命令，成功返回 True。"""
    print(f"  {desc}")
    try:
        result = subprocess.run(cmd, cwd=str(ROOT))
    except FileNotFoundError:
        print(f"    失败：找不到命令 {cmd[0]}")
        return False
    if result.returncode != 0:
        print(f"    失败（退出码 {result.returncode}）")
        return False
    print("    完成")
    return True


def main() -> int:
    banner()

    print("[1/4] 检查 Python 版本")
    version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    print(f"    当前 Python：{version}")
    if sys.version_info < (3, 10):
        print()
        print("    ✗ 版本过低，需要 Python 3.10 或更高版本。")
        print("      请到 https://www.python.org/downloads/ 下载新版重新安装。")
        print()
        return 1
    print()

    print("[2/4] 生成配置文件")
    if ENV_FILE.exists():
        print("    .env 已经存在，跳过（不会覆盖你的设置）")
    elif ENV_EXAMPLE.exists():
        # 从 GitHub 克隆下来的包里没有 .env（配置文件不该进版本库），第一次运行补上。
        shutil.copyfile(ENV_EXAMPLE, ENV_FILE)
        print("    已从 .env.example 生成 .env")
    else:
        print("    （没找到 .env.example，将全部使用程序内置的默认值，不影响使用）")
    print()

    print("[3/4] 创建运行环境")
    if VENV_PY.exists():
        print("    已经存在，跳过")
    else:
        print("    正在创建 .venv（约需十几秒）...")
        if not run([sys.executable, "-m", "venv", str(ROOT / ".venv")], "创建虚拟环境"):
            print()
            print("    ✗ 创建失败。请确认磁盘空间充足，并检查杀毒软件是否拦截。")
            return 1
    print()

    print("[4/4] 安装依赖")
    print("    首次安装约需 3-5 分钟，请耐心等待（会下载约 80MB 的组件）...")
    print()
    if not run([str(VENV_PY), "-m", "pip", "install", "--upgrade", "pip", "-q"], "升级 pip"):
        print("    （升级 pip 失败不致命，继续尝试安装依赖）")
    if not run([str(VENV_PY), "-m", "pip", "install", "-r", str(REQUIREMENTS)], "安装依赖"):
        print()
        print("    ✗ 依赖安装失败。")
        print("      最常见的原因是网络问题（需要访问国内源或外网）。")
        print("      请检查网络后重新双击『启动助手.bat』。")
        print()
        return 1

    print()
    line()
    print("  安装完成，正在启动助手...")
    line()
    print()

    # 交给主程序。它会负责：建库、导知识库、拉起 Ollama、开浏览器。
    time.sleep(1)
    return subprocess.run([str(VENV_PY), "-m", "app.main"], cwd=str(ROOT)).returncode


if __name__ == "__main__":
    sys.exit(main())
