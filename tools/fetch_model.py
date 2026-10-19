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

断点续传：中途断了重新运行即可。但"续传"必须是服务器真的支持（HTTP 206）：
如果服务器忽略 Range 返回 200，本脚本会删掉半文件、从头下载——绝不把整文件
追加到半文件后面（那会得到一个大小对不上、内容全错的坏文件）。

校验：下载完必查文件大小与 GGUF 文件头；想连哈希一起校验，设 MODEL_SHA256
（64 位十六进制）。留空则跳过哈希——但大小和文件头仍然必查。
"""
from __future__ import annotations

import hashlib
import os
import re
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
EXPECT_SHA256 = os.getenv("MODEL_SHA256", "").strip().lower()
_CONTENT_RANGE = re.compile(r"bytes\s+(\d+)-(\d+)/(\d+)")


class _Restart(Exception):
    """续传状态不可信，必须删掉本地文件从头下载。"""


def _magic_ok(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(4) == b"GGUF"
    except OSError:
        return False


def _file_ok(path: Path) -> bool:
    return path.exists() and path.stat().st_size == EXPECT_SIZE and _magic_ok(path)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _verify(path: Path) -> int:
    size = path.stat().st_size
    print(f"\n下载完成：{size} 字节", flush=True)
    print(f"官方参考：{EXPECT_SIZE} 字节", flush=True)
    print(f"大小一致：{size == EXPECT_SIZE}", flush=True)
    if size != EXPECT_SIZE:
        print("文件大小不对：请删除后重试；如果反复如此，镜像可能换了文件。", flush=True)
        return 1

    magic = _magic_ok(path)
    print(f"GGUF 文件头：{'有效' if magic else '无效！'}", flush=True)
    if not magic:
        print("文件头不是 GGUF：文件已损坏，请删除后重试。", flush=True)
        return 1

    if EXPECT_SHA256:
        digest = _sha256(path)
        ok = digest == EXPECT_SHA256
        print(f"SHA256：{digest}  ->  {'一致' if ok else '不一致！'}", flush=True)
        if not ok:
            print("哈希不一致：文件可能损坏或被篡改，请删除后重试。", flush=True)
            return 1
    else:
        print("SHA256：未配置 MODEL_SHA256，跳过哈希校验"
              "（大小与 GGUF 文件头已校验；生产环境建议配置）", flush=True)
    return 0


def _download(allow_resume: bool) -> None:
    done = OUT.stat().st_size if (allow_resume and OUT.exists()) else 0
    if done > EXPECT_SIZE:
        raise _Restart(f"本地文件已有 {done} 字节，比期望的 {EXPECT_SIZE} 还大")

    headers = {"Range": f"bytes={done}-"} if done else {}
    if done:
        print(f"发现已下载 {done/1024/1024:.1f} MB，尝试续传", flush=True)

    t0 = time.time()
    start_done = done
    last_report = 0.0

    with httpx.Client(
        timeout=httpx.Timeout(60.0, read=120.0),
        follow_redirects=True,
        trust_env=False,  # 本机 127.0.0.1/国内镜像不能被系统代理截走
    ) as c:
        with c.stream("GET", URL, headers=headers) as r:
            if r.status_code == 416 and done:
                if _file_ok(OUT):
                    return
                raise _Restart("服务器返回 416（续传起点超出文件末尾），本地文件不完整")

            if done and r.status_code == 200:
                # 服务器/代理忽略了 Range，回的是完整文件。此时若按 ab 追加，
                # 会得到"半文件 + 完整文件"的坏文件，大小校验能发现，但已经白下了。
                raise _Restart("服务器不支持断点续传（返回 200 而不是 206）")

            if r.status_code not in (200, 206):
                raise RuntimeError(f"HTTP {r.status_code}")

            if r.status_code == 206:
                m = _CONTENT_RANGE.match(r.headers.get("Content-Range", ""))
                if not m or int(m.group(1)) != done:
                    raise _Restart(
                        "续传响应与本地进度不匹配（Content-Range="
                        f"{r.headers.get('Content-Range', '')!r}，本地 {done} 字节）"
                    )
                total = int(m.group(3))
            else:
                # 200：从头下载，服务端返回完整文件
                total = int(r.headers.get("Content-Length", 0))
                done = 0

            print(f"文件总大小：{total/1024/1024:.1f} MB", flush=True)
            mode = "ab" if done else "wb"
            with open(OUT, mode) as f:
                for chunk in r.iter_bytes(1024 * 512):
                    f.write(chunk)
                    done += len(chunk)
                    elapsed = time.time() - t0
                    if elapsed - last_report >= 5 or done >= total:
                        last_report = elapsed
                        speed = (done - start_done) / elapsed / 1024 / 1024 if elapsed else 0
                        pct = done / total * 100 if total else 0
                        eta = (total - done) / (speed * 1024 * 1024) if speed > 0.01 else 0
                        print(
                            f"  {pct:5.1f}%  {done/1024/1024:7.1f}/{total/1024/1024:.1f} MB"
                            f"  {speed:5.1f} MB/s  剩余约 {eta:.0f} 秒",
                            flush=True,
                        )


def main() -> int:
    OUT.parent.mkdir(parents=True, exist_ok=True)

    # 已经完整：只校验，不再联网
    if _file_ok(OUT):
        print("本地已有完整模型文件，直接校验。", flush=True)
        return _verify(OUT)

    # 半文件如果连 GGUF 文件头都不对，续传也救不回来，直接删掉重下
    if OUT.exists() and OUT.stat().st_size < EXPECT_SIZE and not _magic_ok(OUT):
        print("本地文件文件头无效，删除后重新下载", flush=True)
        OUT.unlink()

    for allow_resume in (True, False):
        try:
            _download(allow_resume)
            break
        except _Restart as exc:
            print(f"续传不可用：{exc}", flush=True)
            if OUT.exists():
                OUT.unlink()
                print("已删除本地半文件，改为从头下载", flush=True)
            if not allow_resume:
                print("下载失败：无法从未完成状态恢复", flush=True)
                return 1
        except Exception as exc:
            print(f"下载失败：{exc}", flush=True)
            return 1
    else:
        print("下载失败：多次尝试仍未完成", flush=True)
        return 1

    return _verify(OUT)


if __name__ == "__main__":
    sys.exit(main())
