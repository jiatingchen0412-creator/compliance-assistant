"""从国内镜像高速下载 Qwen2.5-7B-Instruct Q4_K_M 模型文件。

为什么需要它：Ollama 官方源在国内实测只有 4-5 MB/s，而且经常卡死在最后的分片
（表现为进度条停住不动、重试也无法完成）。同样内容的单文件 GGUF 从 ModelScope
能跑到 40 MB/s 以上，两分钟就下完。

用法：
    .venv\\Scripts\\python.exe tools\\fetch_model.py      # 下载
    .venv\\Scripts\\python.exe tools\\register_model.py   # 注册到 Ollama

下载到哪：默认放在本项目的 data\\models\\import\\ 下（不占系统盘，也不依赖
Ollama 的模型目录）。想换地方就设环境变量 MODEL_DIR，例如：
    set MODEL_DIR=E:\\大模型

支持断点续传：中途断了重新运行即可。
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import config  # noqa: E402

URL = (
    "https://modelscope.cn/models/bartowski/Qwen2.5-7B-Instruct-GGUF"
    "/resolve/master/Qwen2.5-7B-Instruct-Q4_K_M.gguf"
)
MODEL_FILE = "Qwen2.5-7B-Instruct-Q4_K_M.gguf"
# 下载目录：优先用 MODEL_DIR 环境变量，否则放项目自己的 data\models\import\
OUT = Path(os.getenv("MODEL_DIR") or (config.DATA_DIR / "models")) / "import" / MODEL_FILE
EXPECT_SIZE = 4683073952  # 与 Ollama 官方 registry 的 blob 大小完全一致



def main() -> int:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    done = OUT.stat().st_size if OUT.exists() else 0
    headers = {}
    if done:
        headers["Range"] = f"bytes={done}-"
        print(f"发现已下载 {done/1024/1024:.1f} MB，继续下载", flush=True)

    t0 = time.time()
    start_done = done
    last_report = 0.0

    with httpx.Client(timeout=httpx.Timeout(60.0, read=120.0), follow_redirects=True) as c:
        with c.stream("GET", URL, headers=headers) as r:
            if r.status_code not in (200, 206):
                print(f"下载失败：HTTP {r.status_code}", flush=True)
                return 1
            total = int(r.headers.get("Content-Length", 0)) + done
            print(f"文件总大小：{total/1024/1024:.1f} MB", flush=True)
            mode = "ab" if done else "wb"
            with open(OUT, mode) as f:
                for chunk in r.iter_bytes(1024 * 512):
                    f.write(chunk)
                    done += len(chunk)
                    elapsed = time.time() - t0
                    if elapsed - last_report >= 5 or done >= total:
                        last_report = elapsed
                        speed = (done - start_done) / elapsed / 1024 / 1024
                        pct = done / total * 100 if total else 0
                        eta = (total - done) / (speed * 1024 * 1024) if speed > 0.01 else 0
                        print(
                            f"  {pct:5.1f}%  {done/1024/1024:7.1f}/{total/1024/1024:.1f} MB"
                            f"  {speed:5.1f} MB/s  剩余约 {eta:.0f} 秒",
                            flush=True,
                        )

    size = OUT.stat().st_size
    print(f"\n下载完成：{size} 字节", flush=True)
    print(f"官方参考：{EXPECT_SIZE} 字节", flush=True)
    print(f"大小一致：{size == EXPECT_SIZE}", flush=True)

    with open(OUT, "rb") as f:
        magic = f.read(4)
    print(f"GGUF 文件头：{magic}  ->  {'有效' if magic == b'GGUF' else '无效！'}", flush=True)
    return 0 if size == EXPECT_SIZE and magic == b"GGUF" else 1


if __name__ == "__main__":
    sys.exit(main())
