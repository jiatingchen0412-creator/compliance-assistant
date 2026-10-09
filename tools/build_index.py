"""命令行工具：建/重建检索索引。

什么时候需要跑：
- **刚 clone 下来、还没有 data/app.db**（此时它会自动从 data/seed 导入种子知识库）
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
    parser = argparse.ArgumentParser(description="建/重建知识库检索索引")
    parser.add_argument("--no-vector", action="store_true", help="只重建全文索引，跳过语义向量")
    args = parser.parse_args()

    db.init_db()

    def progress(stage: str, done: int, total_: int) -> None:
        if done == total_ or done % 100 == 0:
            print(f"  {stage}: {done}/{total_}")

    total = db.query_one("SELECT COUNT(*) AS c FROM clauses")["c"]
    if total == 0:
        # 全新检出（或库被删了）——先导入 data/seed 下的种子知识库。
        # ensure_seed_loaded 内部会自己调 rebuild_index 并预热语义模型，
        # 所以这条路不用再往下走。
        print("知识库是空的，正在从 data/seed 导入种子知识库…")
        t0 = time.time()
        result = ingest.ensure_seed_loaded(progress)
        if result.get("skipped"):
            print(f"  跳过：{result.get('reason')}")
        total = db.query_one("SELECT COUNT(*) AS c FROM clauses")["c"]
        if total == 0:
            print("导入后仍然没有条款，请检查 data/seed 下的文件是否完整。")
            return 1
        print()
        print(f"完成，用时 {time.time() - t0:.1f} 秒，共导入 {total} 条条款")
        return 0

    print(f"开始重建索引，共 {total} 条条款…")

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
