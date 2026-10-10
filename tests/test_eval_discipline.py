#!/usr/bin/env python3
"""留出集纪律检查 —— 防止"照着测试集调参"，让评测从自证变回测量。

为什么需要这个文件
------------------
`tests/eval_questions.json`（下称 dev 集）是这个项目唯一的评测依据，
而它的每一道题都是我们自己写的、写完就照着它调阈值调到全绿。
于是它衡量的已经不再是"系统能不能用"，而是"系统能不能通过这一套题"。
留出集 `tests/eval_holdout.json` 是打破这个循环的一次性尺子：

  · 它的条款全部取自 dev 集没引用过的 80 条；
  · 它的题干、问法、风险期望都与 dev 集错开；
  · 它跑完之前不许用来调任何参数（阈值、权重、提示词、分词表）。

这些约定靠人自觉是守不住的，所以本文件把它们变成机器检查：

  1. 指纹锁：`tests/eval_holdout.lock.json` 里存着留出集的 sha256。
     改动留出集就会失败，逼你先想清楚"我是真的要换尺子，还是在把刻度掰弯"。
  2. 零重叠：留出集的题干和条款模式不得与 dev 集重合（含前缀包含）。
     重合意味着这道题考的考点 dev 集已经在考，留出集就失去意义。
  3. 基准棘轮：每一类的最低通过数记在锁里，只能升不能降。
     系统变差会立刻失败；系统变好也不会自动"及格线跟着涨"，
     要显式 `--raise-baseline` 才是承认这次进步可复现。

用法
----
    python tests/test_eval_discipline.py                  # 检查（本地与 CI 都跑这个）
    python tests/test_eval_discipline.py --update-lock    # 有意修改留出集后，刷新指纹
    python tests/test_eval_discipline.py --raise-baseline # 确认当前成绩可作为新基准
    python tests/test_eval_discipline.py --raise-baseline --force   # 允许下调基准（需说明理由）

它只依赖标准库 + 项目自身的 app 包，不联网（除了首次要下语义模型）。
"""

import sys

# 本脚本会打印中文。Windows 英文版 runner 的控制台代码页是 cp1252，
# 标准输出被重定向时 Python 按代码页编码，编不出中文会直接抛 UnicodeEncodeError
# 终结进程。所以这里就地修一次编码，不依赖 app/console.py（理由同 check_syntax.py）。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

HOLDOUT_FILE = ROOT / "tests" / "eval_holdout.json"
DEV_FILE = ROOT / "tests" / "eval_questions.json"
LOCK_FILE = ROOT / "tests" / "eval_holdout.lock.json"

# 分类名 → evaluate_retrieval 统计字段。顺序即打印顺序。
CLASS_FIELDS = {
    "hit": ("hit_total", "hit_ok"),
    "reject": ("reject_total", "reject_ok"),
    "out_of_kb": ("outkb_total", "outkb_ok"),
    "undeterminable": ("undet_total", "undet_ok"),
    "risk_forbid": ("forbid_total", "forbid_ok"),
}

CLASS_LABEL = {
    "hit": "该命中",
    "reject": "该拒答",
    "out_of_kb": "库外法规",
    "undeterminable": "适用性判定",
    "risk_forbid": "误标检查",
}

# 题干比对前先抹掉空白与标点，避免"改个逗号就当新题"
_PUNCT = re.compile(r"[\s，。？！、；：“”‘’（）()《》〈〉【】\[\]{}·…—\-_,.!?;:'\"]+")


def _norm_text(text: str) -> str:
    return _PUNCT.sub("", text or "")


def _sha256(path: Path) -> str:
    """留出集内容的指纹。

    **按换行归一化之后再算**，别直接哈希原始字节：这个仓库的 .gitattributes 是
    `* text=auto eol=lf`，文件在版本库里存 LF，但 Windows 上如果谁的
    `core.autocrlf=true`，检出来就是 CRLF，原始字节哈希会对不上，
    然后报一个"留出集被人改过"的假警报——那比不锁还糟，因为它会训练人忽略这个检查。
    归一化之后仍然是内容敏感：真改了字，指纹照样变。
    """
    raw = path.read_bytes().replace(b"\r\n", b"\n")
    return hashlib.sha256(raw).hexdigest()


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _questions(doc: dict) -> list:
    return doc.get("questions") or []


def _counts(questions: list) -> dict:
    counts = {k: 0 for k in CLASS_FIELDS}
    for q in questions:
        expect = q.get("expect", "hit")
        if expect in counts:
            counts[expect] += 1
        # 误标检查是叠加在某道 hit 题上的，所以单独再数一遍
        if q.get("risk_forbid"):
            counts["risk_forbid"] += 1
    return counts


# --------------------------------------------------------------------------
# 静态检查：指纹、题数、零重叠、字段完整
# --------------------------------------------------------------------------
def check_static(holdout: dict, lock: dict) -> list:
    problems: list = []
    hq = _questions(holdout)
    dq = _questions(_load_json(DEV_FILE))

    if len(hq) < 30:
        problems.append(f"留出集只有 {len(hq)} 题，太少了——样本太少时一题的得失就是 3 个百分点")

    digest = _sha256(HOLDOUT_FILE)
    if digest != lock.get("sha256"):
        problems.append(
            "留出集指纹与锁不一致（当前 %s…，锁里 %s…）。\n"
            "     如果你是有意改的：跑 --update-lock 刷新指纹，并问自己一句\n"
            "     『我改的是尺子，还是因为量出来的数不好看？』\n"
            "     如果你是照着留出集的失败去改题或调参：请先停手看文件头的说明。"
            % (digest[:12], str(lock.get("sha256", ""))[:12])
        )

    counts = _counts(hq)
    locked_counts = lock.get("counts") or {}
    for cls, n in counts.items():
        if locked_counts.get(cls, n) != n:
            problems.append(
                f"{CLASS_LABEL[cls]}题数从锁里的 {locked_counts.get(cls)} 变成 {n}，"
                "题型配比变了（同样要跑 --update-lock）"
            )

    # 逐题字段完整性
    for q in hq:
        qid = q.get("id", "?")
        expect = q.get("expect", "hit")
        if not q.get("question"):
            problems.append(f"{qid} 缺 question")
        if expect == "hit" and not q.get("must_include_any"):
            problems.append(f"{qid} 是 hit 题但没有 must_include_any，等于只查'有没有找到东西'")
        if expect == "out_of_kb" and not q.get("out_of_kb_expect"):
            problems.append(f"{qid} 是 out_of_kb 题但没有 out_of_kb_expect")
        if not q.get("note"):
            problems.append(f"{qid} 缺 note——留出集的题必须写清考点，否则后人不敢改也不敢删")
        if expect not in CLASS_FIELDS:
            problems.append(f"{qid} 的 expect={expect!r} 不是已知类型")

    # 零重叠（一）：题干不得撞车
    dev_text = {_norm_text(q.get("question", "")): q.get("id", "?") for q in dq}
    for q in hq:
        key = _norm_text(q.get("question", ""))
        if key and key in dev_text:
            problems.append(f"{q['id']} 与 dev 集 {dev_text[key]} 题干相同（去掉标点后一致）")

    # 零重叠（二）：条款模式不得撞车——含前缀包含，因为 clause_matches 用的就是前缀。
    # dev 集大量使用 "8.1" / "8.1.5" 这种宽模式，留出集若写 "8.1.5.1" 照样算重叠。
    dev_patterns = {p for q in dq for p in (q.get("must_include_any") or [])}
    for q in hq:
        for p in q.get("must_include_any") or []:
            for dp in dev_patterns:
                if p.startswith(dp) or dp.startswith(p):
                    problems.append(
                        f"{q['id']} 的条款模式 {p!r} 与 dev 集的 {dp!r} 互相包含"
                        "——这个考点 dev 集已经在考，留出集不该重复考"
                    )
                    break

    return problems


# --------------------------------------------------------------------------
# 动态检查：真跑一遍留出集，按基准棘轮比对
# --------------------------------------------------------------------------
def run_holdout(quiet: bool = False) -> dict:
    from app import config, db, ingest, quality
    from app.retrieval import get_engine

    db.init_db()
    ingest.ensure_seed_loaded()
    get_engine().warmup()

    questions = quality.load_questions(HOLDOUT_FILE)
    result = quality.evaluate_retrieval(top_k=6, questions=questions, source=str(HOLDOUT_FILE))

    if not quiet:
        for row in result["rows"]:
            mark = "OK" if row["ok"] else "XX"
            print(f"{mark} {row['id']:<4} best={row['best']:.3f}  {row['question'][:34]}")
            if not row["ok"]:
                print(f"      └─ {row['detail']}")
    return result


def achieved(stats: dict) -> dict:
    return {cls: stats.get(ok_field, 0) for cls, (_, ok_field) in CLASS_FIELDS.items()}


def compare_with_baseline(got: dict, baseline: dict, totals: dict) -> tuple:
    """返回 (退步的问题列表, 进步的分类列表)。"""
    worse, better = [], []
    for cls in CLASS_FIELDS:
        base = baseline.get(cls)
        if base is None:
            continue
        cur = got.get(cls, 0)
        if cur < base:
            worse.append(f"{CLASS_LABEL[cls]}从基准 {base}/{totals.get(cls, '?')} 退到 {cur}/{totals.get(cls, '?')}")
        elif cur > base:
            better.append(f"{CLASS_LABEL[cls]}从 {base} 升到 {cur}")
    return worse, better


def write_lock(holdout: dict, lock: dict, stats: dict, raise_baseline: bool,
               force: bool) -> int:
    hq = _questions(holdout)
    counts = _counts(hq)
    new = dict(lock)
    new["sha256"] = _sha256(HOLDOUT_FILE)
    new["question_count"] = len(hq)
    new["counts"] = counts

    if raise_baseline:
        got = achieved(stats)
        old = lock.get("baseline") or {}
        lowered = [c for c in CLASS_FIELDS if c in old and got.get(c, 0) < old[c]]
        if lowered and not force:
            print("XX 拒绝下调基准：" + "、".join(CLASS_LABEL[c] for c in lowered))
            print("     基准是棘轮，只能升不能降。系统确实变差了就先去修系统；")
            print("     如果这次变差是有意为之（比如故意换了一套更难的题），加 --force 并写进 devlog。")
            return 1
        new["baseline"] = got

    new["_说明"] = (
        "tests/eval_holdout.json（留出集）的指纹与基准。"
        "由 tests/test_eval_discipline.py --update-lock / --raise-baseline 生成，不要手改。"
    )
    LOCK_FILE.write_text(json.dumps(new, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"锁已更新：tests/eval_holdout.lock.json")
    return 0


def main() -> int:
    args = set(sys.argv[1:])
    update_lock = "--update-lock" in args
    raise_baseline = "--raise-baseline" in args
    force = "--force" in args

    for path in (HOLDOUT_FILE, DEV_FILE):
        if not path.exists():
            print(f"XX 找不到 {path.relative_to(ROOT)}")
            return 1

    holdout = _load_json(HOLDOUT_FILE)
    lock = _load_json(LOCK_FILE) if LOCK_FILE.exists() else {}

    if not lock and not update_lock:
        print("XX 还没有 tests/eval_holdout.lock.json —— 先跑一次 --update-lock 建立基准")
        return 1

    hq = _questions(holdout)
    hq_total = len(hq)
    print(f"留出集：tests/eval_holdout.json（{hq_total} 题，sha256 {_sha256(HOLDOUT_FILE)[:12]}…）")

    problems = check_static(holdout, lock)
    for p in problems:
        print(f"XX {p}")

    print("· 跑一遍留出集（真实检索，不上大模型）")
    result = run_holdout()
    stats = result["stats"]
    got = achieved(stats)
    totals = {cls: stats.get(total_field, 0) for cls, (total_field, _) in CLASS_FIELDS.items()}

    print("=" * 108)
    for cls in CLASS_FIELDS:
        base = (lock.get("baseline") or {}).get(cls)
        base_txt = "基准 -" if base is None else f"基准 {base}"
        print(f"  {CLASS_LABEL[cls]:<8} {got.get(cls, 0)}/{totals.get(cls, 0)}   {base_txt}")

    worse, better = compare_with_baseline(got, lock.get("baseline") or {}, totals)
    for w in worse:
        print(f"XX 退步：{w}")
    for b in better:
        print(f"OK 进步：{b}（可跑 --raise-baseline 把它记为新基准）")

    known = lock.get("known_gaps") or []
    if known:
        print("· 已知缺口（记录在锁里，不是本次新出现的退步）")
        for k in known:
            print(f"    - {k}")

    if update_lock or raise_baseline:
        return write_lock(holdout, lock, stats, raise_baseline, force)

    if problems or worse:
        print(f"留出集纪律检查：未通过（{len(problems)} 项静态问题，{len(worse)} 项退步）")
        return 1

    print("留出集纪律检查：全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
