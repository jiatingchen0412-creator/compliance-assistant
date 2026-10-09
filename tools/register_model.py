"""把下载好的 GGUF 注册成本地 Ollama 模型。

配合 tools/fetch_model.py 使用：
    1. .venv\\Scripts\\python.exe tools\\fetch_model.py      # 从国内镜像高速下载
    2. .venv\\Scripts\\python.exe tools\\register_model.py   # 注册到 Ollama

为什么需要这一步：Ollama 官方源在国内经常卡在最后的分片下载上（实测反复失败），
而同样内容的单文件 GGUF 从国内镜像能跑到 40MB/s 以上。
下完用 `ollama create` 注册，效果和 `ollama pull` 完全一样。

模型文件找不着？默认去 data\\models\\import\\ 下找，也可以用 MODEL_DIR 环境变量指定。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import config  # noqa: E402

MODEL_NAME = "qwen2.5:7b-instruct"
MODEL_FILE = "Qwen2.5-7B-Instruct-Q4_K_M.gguf"
GGUF = Path(os.getenv("MODEL_DIR") or (config.DATA_DIR / "models")) / "import" / MODEL_FILE
WORK_DIR = GGUF.parent
MODELFILE = WORK_DIR / "Modelfile"

MODELFILE_BODY = """FROM ./{gguf_name}

# 合规问答要稳定可复现
PARAMETER temperature 0
PARAMETER top_p 0.9
# 上下文窗口：提示词里要塞入若干条款 + 历史对话，8K 够用
PARAMETER num_ctx 8192

TEMPLATE \"\"\"{{{{ if .System }}}}<|im_start|>system
{{{{ .System }}}}<|im_end|>
{{{{ end }}}}{{{{ if .Prompt }}}}<|im_start|>user
{{{{ .Prompt }}}}<|im_end|>
{{{{ end }}}}<|im_start|>assistant
{{{{ .Response }}}}<|im_end|>
\"\"\"

SYSTEM \"\"\"你是企业信息安全合规自查助手。\"\"\"
"""


def find_ollama() -> str:
    exe = shutil.which("ollama")
    if exe:
        return exe
    candidate = Path.home() / "AppData/Local/Programs/Ollama/ollama.exe"
    if candidate.exists():
        return str(candidate)
    print("找不到 ollama.exe，请确认已安装 Ollama。")
    sys.exit(1)


def main() -> int:
    if not GGUF.exists():
        print(f"找不到模型文件：{GGUF}")
        print("请先运行：.venv\\Scripts\\python.exe tools\\fetch_model.py")
        return 1

    size_gb = GGUF.stat().st_size / 1024**3
    print(f"模型文件：{GGUF.name}  ({size_gb:.2f} GB)")
    with open(GGUF, "rb") as f:
        magic = f.read(4)
    if magic != b"GGUF":
        print(f"文件头异常：{magic}，文件可能不完整，请重新下载。")
        return 1
    print("文件头校验通过\n")

    MODELFILE.write_text(MODELFILE_BODY.format(gguf_name=GGUF.name), encoding="utf-8")
    ollama = find_ollama()

    print(f"正在注册模型 {MODEL_NAME}（需要把 {size_gb:.1f}GB 复制进 Ollama，约 1 分钟）…")
    t0 = time.time()
    result = subprocess.run(
        [ollama, "create", MODEL_NAME, "-f", str(MODELFILE)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if result.returncode != 0:
        print("注册失败：")
        print((result.stderr or result.stdout or "")[-1500:])
        return 1

    print(f"注册完成，用时 {time.time() - t0:.0f} 秒\n")

    listing = subprocess.run(
        [ollama, "list"], capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    print(listing.stdout)

    print("可以删掉下载的原始文件以释放空间：")
    print(f'  Remove-Item "{WORK_DIR}" -Recurse -Force')
    print("\n完成。重新启动助手（或刷新页面）即可使用。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
