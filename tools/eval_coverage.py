"""评测覆盖度分析：dev 集考了哪些条款、还漏了哪些。

用途：**给留出集选题**。留出集（`tests/eval_holdout.json`）的条款必须尽量取自
dev 集（`tests/eval_questions.json`）从未引用过的那一批，否则考的还是同一批
知识点，防不住过拟合。这个脚本把"从未引用过"算清楚，省得靠人肉记。

用法（在项目根目录执行）：
    .venv\\Scripts\\python.exe tools\\eval_coverage.py

产出（都在 data/ 下，属生成物、不进版本库）：
    data/_coverage.txt   分组汇总：未覆盖条款按「标准 > 一级章节」归堆，并统计风险标签
    data/_uncovered.txt  未覆盖条款全量明细（编号 / 标题 / 风险标签 / 正文前 150 字），
                         写留出集时对着它挑题目

判据说明：匹配用的是 `app/quality.py` 里 `clause_matches` 的同一套规则
（`cid.startswith(p) or p in cid`），所以 dev 集里写一个宽模式（比如 `8.1.5`）
会把 `8.1.5.1`、`8.1.5.2` 一起算成"已覆盖"。这既是它的优点（按**系统的实际判分
口径**算覆盖），也是它的局限（宽模式会高估覆盖度）。

**一条实测教训**：「未覆盖的条款」不等于「未覆盖的考点」。初次给留出集选题时，
有两道题期望的是 NIST 的 `ID.RA-07`（变更管理）和 `DE.CM-01`（网络监测），
结果系统检索到的是等保 `8.1.5.2` 与 `8.2.5.20`——**正文完整回答了问题**
（前者写着"重要操作须经审批并经审计记录…变更不走流程"，后者写着"没有告警通知，
服务挂了靠客户投诉才知道"）。所以选题之后必须逐题看检索结果是不是"另一个正确
答案"，是的话就换题，不能算系统答错。
"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import app.console  # noqa: F401  修正控制台编码

SEED = ROOT / "data" / "seed"
OUT = ROOT / "data" / "_coverage.txt"
DETAIL = ROOT / "data" / "_uncovered.txt"

lines: list[str] = []


def w(s: str = "") -> None:
    lines.append(s)


# ---------- 1. dev 集引用了哪些条款编号 ----------
qs = json.loads((ROOT / "tests" / "eval_questions.json").read_text(encoding="utf-8"))["questions"]
patterns: set[str] = set()
for q in qs:
    for p in q.get("must_include_any") or []:
        patterns.add(p)

w("=" * 78)
w("现有 dev 集（44 题）引用的条款编号模式")
w("=" * 78)
w(f"共 {len(patterns)} 个模式：{', '.join(sorted(patterns))}")
w()

# ---------- 2. 种子库全部条款 ----------
all_clauses: list[dict] = []
for f in sorted(SEED.glob("*.json")):
    if f.name.startswith("_"):
        continue
    data = json.loads(f.read_text(encoding="utf-8"))
    std = data.get("standard") or {}
    if not std.get("id"):
        continue
    for c in data.get("clauses") or []:
        all_clauses.append({
            "file": f.name,
            "std_id": std.get("id"),
            "std_name": std.get("name"),
            "clause_id": str(c.get("clause_id", "")),
            "title": c.get("title", ""),
            "chapter_path": c.get("chapter_path") or [],
            "risk_tags": c.get("risk_tags") or [],
            "text_type": c.get("text_type", ""),
            "_text": (c.get("text") or c.get("summary") or "").strip(),
        })

w("=" * 78)
w("种子库条款总览")
w("=" * 78)
by_std = Counter(c["std_name"] for c in all_clauses)
for name, n in by_std.items():
    w(f"  {name}: {n} 条")
w(f"  合计: {len(all_clauses)} 条")
w()


def covered(cid: str) -> bool:
    return any(cid.startswith(p) or p in cid for p in patterns)


hit = [c for c in all_clauses if covered(c["clause_id"])]
miss = [c for c in all_clauses if not covered(c["clause_id"])]
w(f"dev 集已覆盖条款: {len(hit)} 条")
w(f"dev 集**未覆盖**条款: {len(miss)} 条  ← 留出测试集从这里选题")
w()

# ---------- 3. 未覆盖条款按章节分组（找成体系的考点） ----------
w("=" * 78)
w("未覆盖条款 · 按「标准 > 一级章节」分组（取条数 ≥3 的组）")
w("=" * 78)
groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
for c in miss:
    l1 = (c["chapter_path"] or ["(无章节)"])[0]
    groups[(c["std_name"], l1)].append(c)

for (std, l1), items in sorted(groups.items(), key=lambda kv: -len(kv[1])):
    if len(items) < 3:
        continue
    tags = Counter(t for c in items for t in c["risk_tags"])
    w(f"\n【{std}】{l1}  —— {len(items)} 条未覆盖  风险标签:{dict(tags) or '无'}")
    for c in items[:14]:
        w(f"    {c['clause_id']:<16} {c['title'][:38]}")
    if len(items) > 14:
        w(f"    … 另有 {len(items) - 14} 条")

# ---------- 4. 高风险标签在未覆盖条款里的分布 ----------
w()
w("=" * 78)
w("未覆盖条款 · 按风险标签统计（测试集要优先补这些考点）")
w("=" * 78)
tag_counter = Counter(t for c in miss for t in c["risk_tags"])
for t, n in tag_counter.most_common():
    w(f"  {t:<20} {n} 条")

# ---------- 5. 未覆盖条款全量明细（写题用） ----------
detail: list[str] = []
detail.append("未覆盖条款全量明细（id | 标题 | 风险标签 | 正文前 110 字）")
detail.append("=" * 78)
for c in sorted(miss, key=lambda x: (x["std_name"], x["clause_id"])):
    text = (c.get("_text") or "").replace("\n", " ")
    detail.append(
        f"{c['clause_id']:<18} {c['std_name']:<16} {'/'.join(c['risk_tags']) or '-':<28} "
        f"{c['title'][:30]}"
    )
    detail.append(f"    {text[:150]}")

OUT.write_text("\n".join(lines), encoding="utf-8")
DETAIL.write_text("\n".join(detail), encoding="utf-8")
print(f"written: {OUT}  ({len(lines)} lines)")
print(f"written: {DETAIL}  ({len(detail)} lines)")
