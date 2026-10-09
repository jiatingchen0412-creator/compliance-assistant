"""命令行工具：导入用户自己的文档到知识库。

用法（在项目根目录执行）：
    .venv\\Scripts\\python.exe tools\\import_doc.py "D:\\资料\\等保测评要求.pdf"
    .venv\\Scripts\\python.exe tools\\import_doc.py "D:\\资料\\*.docx"

支持 PDF / Word(.docx) / Markdown / TXT / 标准 JSON。
导入后会自动重建检索索引。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db, ingest  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="导入文档到合规知识库")
    parser.add_argument("paths", nargs="+", help="文件路径，支持通配符")
    parser.add_argument("--no-vector", action="store_true", help="跳过语义向量重建（更快）")
    args = parser.parse_args()

    db.init_db()

    files: list[Path] = []
    for pattern in args.paths:
        p = Path(pattern)
        if p.is_file():
            files.append(p)
        else:
            parent = p.parent if str(p.parent) != "" else Path(".")
            files.extend(sorted(parent.glob(p.name)))

    if not files:
        print("没有找到要导入的文件。")
        return 1

    print(f"准备导入 {len(files)} 个文件\n")
    for f in files:
        print(f"→ {f.name}")
        try:
            if f.suffix.lower() == ".json":
                r = ingest.import_standard_file(f)
            else:
                r = ingest.import_document(f)
            print(f"   新增 {r['added']} 条，更新 {r['updated']} 条，跳过 {r['skipped']} 条")
            if r.get("note"):
                print(f"   注意：{r['note']}")
        except Exception as exc:
            print(f"   失败：{exc}")

    print("\n正在重建检索索引…")
    result = ingest.rebuild_index(with_vectors=not args.no_vector)
    print(f"  全文索引 {result['fts']} 条，语义向量 {result['vectors']} 条")
    if result.get("vector_note"):
        print(f"  说明：{result['vector_note']}")
    print("\n完成。重新启动服务后即可生效。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
