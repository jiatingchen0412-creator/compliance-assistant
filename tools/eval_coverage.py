"""评测覆盖度分析：dev 集**真实检索到**了哪些条款、还漏了哪些。

用途：给评测集选题、也给「我们到底覆盖了多少知识库」一个可信数字。

## 为什么要真实检索一遍

早期版本用「dev 集的 `must_include_any` 模式去匹配条款编号」来算覆盖
（`cid.startswith(p) or p in cid`），报出来是 231/283。覆盖度审计指出这是虚高：
真实检索只有 209/283。两个原因：

1. `must_include_any` 是 **any-of**——题里写两条，系统只要命中一条就算通过，
   宁可漏考也不误判，但覆盖统计会把两条都记成"已考"。
2. 宽前缀（`8.1.5`）会把 `8.1.5.1`~`8.1.5.4` 全算成已覆盖，哪怕系统从没取到过它们。

标本：`8.1.4.18` 被模式覆盖判为"已覆盖"，但没有任何一道题真把它检索出来过。

所以现在的口径是：**对每道 dev 题真的跑一次 `search()`，把它 top_k 里的条款
编号并起来**。这才是"系统实际考过哪些条款"。模式口径仍保留一列做对照，
两者的差值就是虚高量。

## 用法

    .venv\\Scripts\\python.exe tools\\eval_coverage.py

产出（都在 data/ 下，属生成物、不进版本库）：
    data/_coverage.txt   分组汇总 + 两种口径的差值 + 虚高明细
    data/_uncovered.txt  未覆盖条款全量明细（编号 / 标题 / 风险标签 / 正文前 150 字）

## 一条实测教训

「未覆盖的条款」不等于「未覆盖的考点」。给留出集选题时有两道题期望 NIST 的
`ID.RA-07`（变更管理）和 `DE.CM-01`（网络监测），系统检索到的却是等保 `8.1.5.2`
与 `8.2.5.20`——**正文完整回答了问题**。所以选题后必须逐题看检索结果是不是
"另一个正确答案"，是的话换题，不能算系统答错。
"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import app.console  # noqa: F401  修正控制台编码

from app import config  # noqa: E402

SEED = ROOT / "data" / "seed"
OUT = ROOT / "data" / "_coverage.txt"
DETAIL = ROOT / "data" / "_uncovered.txt"
TOP_K = config.RETRIEVAL_TOP_K

lines: list[str] = []


def w(s: str = "") -> None:
    lines.append(s)


# ---------- 1. dev 集：模式口径 vs 真实检索口径 ----------
qs = json.loads((ROOT / "tests" / "eval_questions.json").read_text(encoding="utf-8"))["questions"]

patterns: set[str] = set()
for q in qs:
    for p in q.get("must_include_any") or []:
        patterns.add(p)


def pattern_covered(cid: str) -> bool:
    """旧口径：dev 集的模式命中即算覆盖。"""
    return any(cid.startswith(p) or p in cid for p in patterns)


from app.retrieval import get_engine  # noqa: E402  延迟导入：会初始化 fastembed

engine = get_engine()
engine.warmup()

retrieved: set[str] = set()
per_question: dict[str, list[str]] = {}
for q in qs:
    got = engine.search(q["question"], top_k=TOP_K)
    ids = [str(r.get("clause_id", "")) for r in got.get("results") or []]
    per_question[q["id"]] = ids
    retrieved.update(i for i in ids if i)

w("=" * 78)
w(f"dev 集（{len(qs)} 题）覆盖度：两种口径对照")
w("=" * 78)
w(f"模式口径：{len(patterns)} 个模式，取自 must_include_any")
w(f"真实检索：每题 top_k={TOP_K}，共取到 {len(retrieved)} 个不同条款编号")
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

hit_real = [c for c in all_clauses if c["clause_id"] in retrieved]
miss_real = [c for c in all_clauses if c["clause_id"] not in retrieved]
hit_pat = [c for c in all_clauses if pattern_covered(c["clause_id"])]

w(f"模式口径已覆盖：{len(hit_pat)} 条")
w(f"真实检索已覆盖：{len(hit_real)} 条")
w(f"真实检索**未覆盖**：{len(miss_real)} 条   ← 选题从这里挑")
if len(hit_pat) > len(hit_real):
    w(f"⚠️ 模式口径虚高：{len(hit_pat) - len(hit_real)} 条（模式说考过、检索其实没取到）")
w()

# ---------- 3. 虚高明细（模式说已覆盖、真实没取到） ----------
fake = [c for c in hit_pat if c["clause_id"] not in retrieved]
if fake:
    w("=" * 78)
    w("⚠️ 虚高明细：模式口径判为已覆盖、真实检索从未取到")
    w("=" * 78)
    for c in sorted(fake, key=lambda x: (x["std_name"], x["clause_id"])):
        w(f"  {c['clause_id']:<18} {c['std_name']:<16} {c['title'][:34]}")
    w()

# ---------- 4. 未覆盖条款按章节分组 ----------
w("=" * 78)
w("未覆盖条款 · 按「标准 > 一级章节」分组（取条数 ≥3 的组）")
w("=" * 78)
groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
for c in miss_real:
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

# ---------- 5. 高风险标签在未覆盖条款里的分布 ----------
w()
w("=" * 78)
w("未覆盖条款 · 按风险标签统计（测试集要优先补这些考点）")
w("=" * 78)
tag_counter = Counter(t for c in miss_real for t in c["risk_tags"])
for t, n in tag_counter.most_common():
    mark = "  ← 高风险" if t in config.HIGH_RISK_TAGS else ""
    w(f"  {t:<20} {n} 条{mark}")

# ---------- 6. 未覆盖条款全量明细（写题用） ----------
detail: list[str] = []
detail.append("未覆盖条款全量明细（id | 标准 | 风险标签 | 标题 | 正文前 150 字）")
detail.append("口径：对每道 dev 题真实跑一次 search()，top_k 里没出现过的条款")
detail.append("=" * 78)
for c in sorted(miss_real, key=lambda x: (x["std_name"], x["clause_id"])):
    text = (c.get("_text") or "").replace("\n", " ")
    detail.append(
        f"{c['clause_id']:<18} {c['std_name']:<16} {'/'.join(c['risk_tags']) or '-':<28} "
        f"{c['title'][:30]}"
    )
    detail.append(f"    {text[:150]}")

OUT.write_text("\n".join(lines), encoding="utf-8")
DETAIL.write_text("\n".join(detail), encoding="utf-8")
print(f"真实检索覆盖 {len(hit_real)}/{len(all_clauses)} 条，未覆盖 {len(miss_real)} 条")
print(f"written: {OUT}  ({len(lines)} lines)")
print(f"written: {DETAIL}  ({len(detail)} lines)")
