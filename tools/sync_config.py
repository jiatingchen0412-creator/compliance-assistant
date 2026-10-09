"""把 .env.example 里新增的配置项补进用户的 .env。

为什么需要：
`.env` 是首次启动时从 `.env.example` 复制生成的。之后版本升级如果加了新配置，
用户的 `.env` 里就没有这一项——虽然程序有默认值不会出错，
但用户看不到、也就没法调（比如新版加的 ENABLE_TRUE_STREAMING）。

这个工具只做"补充"，**绝不覆盖用户已经改过的值**。

用法（在项目根目录执行）：
    .venv\\Scripts\\python.exe tools\\sync_config.py            # 查看差异并补全
    .venv\\Scripts\\python.exe tools\\sync_config.py --dry-run  # 只看差异，不写入
"""
from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

ENV = ROOT / ".env"
EXAMPLE = ROOT / ".env.example"


def parse(path: Path) -> list[tuple[str, str]]:
    """返回 [(key, 原始行)]。"""
    out: list[tuple[str, str]] = []
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            out.append((stripped.split("=", 1)[0].strip(), line))
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="补全 .env 里缺失的配置项")
    parser.add_argument("--dry-run", action="store_true", help="只显示差异，不写入")
    args = parser.parse_args()

    if not EXAMPLE.exists():
        print("找不到 .env.example，无法比对。")
        return 1
    if not ENV.exists():
        print(".env 不存在，直接从模板复制一份：")
        shutil.copy(EXAMPLE, ENV)
        print(f"  已创建 {ENV}")
        return 0

    have = {k for k, _ in parse(ENV)}
    example_keys = [k for k, _ in parse(EXAMPLE)]
    missing = [k for k in example_keys if k not in have]

    if not missing:
        print("配置已是最新，没有缺失项。")
        return 0

    print(f"发现 {len(missing)} 个缺失的配置项：")
    for k in missing:
        print(f"  - {k}")

    if args.dry_run:
        print("\n（--dry-run 模式，未写入任何内容）")
        return 0

    # 从 example 里把缺失项连同它上面的注释一起搬过来
    lines = EXAMPLE.read_text(encoding="utf-8").splitlines()
    block: list[str] = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in missing:
                comments: list[str] = []
                j = i - 1
                while j >= 0 and lines[j].strip().startswith("#"):
                    comments.insert(0, lines[j])
                    j -= 1
                if block:
                    block.append("")
                block.extend(comments)
                block.append(line)

    # 先备份，再追加
    backup = ENV.parent / f".env.bak_{datetime.now():%Y%m%d_%H%M%S}"
    shutil.copy(ENV, backup)
    with open(ENV, "a", encoding="utf-8") as f:
        f.write("\n# ===== 以下为后续版本新增的配置项 =====\n")
        f.write("\n".join(block))
        f.write("\n")

    print(f"\n已追加到 .env（原文件备份为 {backup.name}）")
    print("重启服务后生效。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
