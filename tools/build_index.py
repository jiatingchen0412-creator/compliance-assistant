"""命令行工具：重建检索索引。

什么时候需要跑：
- 手动改过 data/app.db 里的条款
- 改了 app/normalize.py 里的分词词典或口语映射表
- 语义检索突然不好用了（向量可能损坏）

用法（在项目根目录执行）：
    .venv\\Scripts\\python.exe tools\\build_index.py
    .venv\\Scripts\\python.exe tools\\build_index.py --no-vector    # 只重建全文索引
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db, ingest  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="重建知识库检索索引")
    parser.add_argument("--no-vector", action="store_true", help="只重建全文索引，跳过语义向量")
    args = parser.parse_args()

    db.init_db()

    total = db.query_one("SELECT COUNT(*) AS c FROM clauses")["c"]
    if total == 0:
        print("知识库里还没有条款。先启动一次服务，会自动导入 data/seed 下的种子数据。")
        return 1

    print(f"开始重建索引，共 {total} 条条款…")

    def progress(stage: str, done: int, total_: int) -> None:
        if done == total_ or done % 100 == 0:
            print(f"  {stage}: {done}/{total_}")

    t0 = time.time()
    result = ingest.rebuild_index(progress, with_vectors=not args.no_vector)
    print()
    print(f"完成，用时 {time.time() - t0:.1f} 秒")
    print(f"  全文索引：{result['fts']} 条")
    print(f"  语义向量：{result['vectors']} 条")
    if result.get("vector_note"):
        print(f"  说明：{result['vector_note']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
