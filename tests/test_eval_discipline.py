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

  1. 指纹锁：`tests/eval_holdout.lock.json` 里存着留出集**和 dev 集**的 sha256。
     改动任一套题都会失败，逼你先想清楚"我是真的要换尺子，还是在把刻度掰弯"。
  2. 零重叠：留出集的题干和条款模式不得与 dev 集重合（含前缀包含）。
     重合意味着这道题考的考点 dev 集已经在考，留出集就失去意义。
  3. 基准棘轮：每一类的最低通过数记在锁里，只能升不能降。
     系统变差会立刻失败；系统变好也不会自动"及格线跟着涨"，
     要显式 `--raise-baseline` 才是承认这次进步可复现。
  4. 逐题指纹：锁里还记着**每一道题**当时过没过。只看每类总数的话，
     "修好一题、弄坏一题"可以互相抵消；逐题比对会把这种退化抓出来。
  5. 配置快照：阈值/权重也会连同锁一起记下来。调了参数而没重记基准，
     同样算"把尺子掰弯"。

用法
----
    python tests/test_eval_discipline.py                  # 检查（本地与 CI 都跑这个）
    python tests/test_eval_discipline.py --update-lock    # 有意修改题库后，刷新指纹
    python tests/test_eval_discipline.py --raise-baseline # 确认当前成绩可作为新基准

`--force` 已废弃：下调基准不再允许绕过。确实要换一套更难的尺子时，
请手改 `tests/eval_holdout.lock.json` 并在 devlog 里写明理由与日期。

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
import shutil
import tempfile
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
    "clarify": ("clarify_total", "clarify_ok"),
    "risk_forbid": ("forbid_total", "forbid_ok"),
}

CLASS_LABEL = {
    "hit": "该命中",
    "reject": "该拒答",
    "out_of_kb": "库外法规",
    "undeterminable": "适用性判定",
    "clarify": "需追问",
    "risk_forbid": "误标检查",
}

# 这些配置会直接改变留出集分数。锁里存快照，改了就要求重记基准。
CONFIG_KEYS = {
    "min_score": "RETRIEVAL_MIN_SCORE",
    "keyword_weight": "HYBRID_KEYWORD_WEIGHT",
    "vector_weight": "HYBRID_VECTOR_WEIGHT",
    "top_k": "RETRIEVAL_TOP_K",
}

# 题干比对前先抹掉空白与标点，避免"改个逗号就当新题"
_PUNCT = re.compile(r"[\s，。？！、；：“”‘’（）()《》〈〉【】\[\]{}·…—\-_,.!?;:'\"]+")


def _norm_text(text: str) -> str:
    return _PUNCT.sub("", text or "")


def _sha256(path: Path) -> str:
    """文件内容的指纹。

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


def _config_snapshot() -> dict:
    from app import config

    snap = {name: getattr(config, attr, None) for name, attr in CONFIG_KEYS.items()}
    # 留出集固定按 top_k=6 跑（见 run_holdout），把这个也记进去
    snap["holdout_top_k"] = 6
    return snap


def _fingerprint(result: dict) -> dict:
    """每一道题的题型与通过状态，用于逐题棘轮。"""
    fp = {}
    for row in result.get("rows", []):
        cls = row.get("expect", "hit")
        if row.get("forbid"):
            cls += "+forbid"
        fp[row["id"]] = {"class": cls, "ok": bool(row.get("ok"))}
    return fp


# --------------------------------------------------------------------------
# 静态检查：指纹、题数、零重叠、字段完整、配置快照
# --------------------------------------------------------------------------
def check_static(holdout: dict, lock: dict, for_update: bool = False) -> list:
    problems: list = []
    hq = _questions(holdout)
    dq = _questions(_load_json(DEV_FILE))

    if len(hq) < 30:
        problems.append(f"留出集只有 {len(hq)} 题，太少了——样本太少时一题的得失就是 3 个百分点")

    if not for_update:
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

        dev_digest = _sha256(DEV_FILE)
        if lock.get("dev_sha256") and dev_digest != lock["dev_sha256"]:
            problems.append(
                "dev 集指纹与锁不一致：dev 集被改过。改题本身可以，但必须同时\n"
                "     更新锁（--update-lock）并在 devlog 说明『加题/改题的原因』，\n"
                "     否则没人能区分『补充考点』和『照着失败题改题干』。"
            )

        if lock.get("config"):
            cur = _config_snapshot()
            old = lock["config"]
            diffs = [f"{k}: {old.get(k)} → {cur.get(k)}" for k in cur if old.get(k) != cur.get(k)]
            if diffs:
                problems.append(
                    "检索配置与锁里的快照不一致（" + "；".join(diffs) + "）。\n"
                    "     阈值/权重也是『尺子』的一部分；如果这次是有意调参，必须重跑\n"
                    "     --raise-baseline 记录新基准，并在 devlog 说明理由。"
                )
    # for_update=True：--update-lock 的用途就是接受"题库换了"，
    # 指纹/题数/配置本来就该变，不能拿旧锁去卡它；字段完整性与零重叠照查。

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
        if expect == "clarify" and not q.get("clarify_expect"):
            problems.append(f"{qid} 是 clarify 题但没有 clarify_expect")
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


def _sha256_file(path: Path) -> str:
    """文件 sha256（越小写越好读；只用于「跑前/跑后是不是同一个库」）。"""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _isolate_db() -> tuple[Path, Path, str]:
    """把数据库指到真实库的临时副本，返回 (临时目录, 真实库路径, 跑之前的哈希)。

    为什么必须这么做：评测会 init_db / 灌种子 / 读会话，旧版直接跑在
    data/app.db 上——测试污染用户数据这件事，我们已经在 test_edge_cases
    上吃过一次亏（真实库 sha256 从 4C3A32… 变成 8E83E4…）。评测脚本没有
    理由例外。副本连 -wal / -shm 一起拷，避免漏掉未落盘的事务。
    """
    from app import config  # 本脚本其他地方都是按需 import app，这里保持一致

    real = Path(config.DB_PATH)
    real_hash = _sha256_file(real) if real.exists() else ""
    tmp_dir = Path(tempfile.mkdtemp(prefix="eval_discipline_db_"))
    tmp_db = tmp_dir / "app.db"
    for suffix in ("", "-wal", "-shm"):
        src = Path(str(real) + suffix)
        if src.exists():
            shutil.copy2(src, Path(str(tmp_db) + suffix))
    config.DB_PATH = tmp_db
    return tmp_dir, real, real_hash


def _finish_db_guard(code: int, real_db: Path, hash_before: str) -> int:
    """收尾：真实库要是被改了，一律当失败（非 0 退出），不遮不掩。"""
    if not hash_before:
        print("[隔离] 找不到真实库，跳过「未被改动」校验（首次运行请先启动一次服务）")
        return code
    hash_after = _sha256_file(real_db) if real_db.exists() else ""
    if hash_after != hash_before:
        print(f"XX 真实库被本次评测改动了：{real_db}")
        print(f"   跑之前 sha256 {hash_before[:16]}…")
        print(f"   跑之后 sha256 {hash_after[:16]}…")
        print("   这违反「测试不污染用户数据」；说明隔离没生效，别把它当小问题。")
        return 2
    print(f"[隔离] 真实库未被评测改动：{real_db}（sha256 {hash_after[:16]}…）")
    return code


# --------------------------------------------------------------------------
# 动态检查：真跑一遍留出集，按基准棘轮比对
# --------------------------------------------------------------------------
def run_holdout(quiet: bool = False) -> dict:
    from app import db, ingest, quality
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


def compare_per_question(current: dict, baseline: dict) -> tuple:
    """逐题比对。返回 (退步列表, 进步列表)。

    这是"总数看不出"的那一层：修好 q07、弄坏 q05，总数可能不变，
    但逐题指纹会判定 q05 从 True 变成 False，照样失败。
    """
    worse, better = [], []
    for qid, base in baseline.items():
        cur = current.get(qid)
        if cur is None:
            worse.append(f"{qid} 从留出集里消失了（锁里有、这次没跑）")
            continue
        if cur.get("class") != base.get("class"):
            worse.append(f"{qid} 的题型从 {base.get('class')} 变成 {cur.get('class')}")
            continue
        if base.get("ok") and not cur.get("ok"):
            worse.append(f"{qid} 从通过变成失败（同类总数可能被别的题补上，但这题确实退化了）")
        elif not base.get("ok") and cur.get("ok"):
            better.append(f"{qid} 从失败变成通过")
    return worse, better


# --------------------------------------------------------------------------
# 质量门自身的行为测试：红线真的会拦人、自检真的会 fail-loud
# --------------------------------------------------------------------------
def run_quality_self_tests() -> list:
    """留出集纪律之外，再验证"质量门"本身不是摆设。

    这一节对应 2026-10 审计发现的三个假绿灯：
      · 9/10 拒答曾被判为通过（绝对下限被稀释）；
      · 评测代码抛异常时 run_check 返回 passed=True/skipped=True；
      · 评测题文件缺失时同样被当成"跳过=正常"。
    这些行为一旦回潮，本函数会失败。
    """
    from app import quality

    problems: list = []

    base = {
        "hit_total": 25, "hit_ok": 25,
        "reject_total": 10, "reject_ok": 9,
        "risk_total": 9, "risk_ok": 9,
        "outkb_total": 6, "outkb_ok": 6,
        "undet_total": 4, "undet_ok": 4,
        "clarify_total": 4, "clarify_ok": 4,
        "forbid_total": 2, "forbid_ok": 2,
    }
    if quality.check_red_lines(dict(base))["passed"]:
        problems.append("9/10 拒答竟能通过红线（一票否决类必须 ok==total）")

    for key in ("outkb_ok", "undet_ok", "clarify_ok", "forbid_ok"):
        stats = dict(base, reject_ok=10)
        stats[key] = base[key] - 1
        if quality.check_red_lines(stats)["passed"]:
            problems.append(f"{key} 少 1 题仍能通过红线（总数没变）")

    # 题数少一道、通过数刚好压线，也必须失败（防止删题稀释）
    stats = dict(base, reject_ok=10, hit_total=24, hit_ok=24)
    if quality.check_red_lines(stats)["passed"]:
        problems.append("命中题从 25 道减到 24 道仍能通过红线（题数没卡死）")

    orig_eval = quality.evaluate_retrieval

    def _boom(*args, **kwargs):
        raise RuntimeError("selftest: evaluation exploded")

    quality.evaluate_retrieval = _boom
    try:
        verdict = quality.run_check()
    finally:
        quality.evaluate_retrieval = orig_eval
    if verdict.get("passed") or verdict.get("skipped") or not verdict.get("failures"):
        problems.append("评测代码抛异常时 run_check 没有 fail-loud（passed/skipped 仍是 True）")

    orig_file = quality.EVAL_FILE
    quality.EVAL_FILE = ROOT / "tests" / "__selftest_missing__.json"
    try:
        verdict = quality.run_check()
    finally:
        quality.EVAL_FILE = orig_file
    if verdict.get("passed") or verdict.get("skipped") or not verdict.get("failures"):
        problems.append("评测题文件缺失时 run_check 没有 fail-loud")

    return problems


def write_lock(holdout: dict, lock: dict, stats: dict, fingerprint: dict,
               raise_baseline: bool, force: bool) -> int:
    hq = _questions(holdout)
    new = dict(lock)
    new["sha256"] = _sha256(HOLDOUT_FILE)
    new["question_count"] = len(hq)
    new["counts"] = _counts(hq)
    new["dev_sha256"] = _sha256(DEV_FILE)
    new["config"] = _config_snapshot()

    if force:
        print("!! --force 已废弃：下调基准不再被允许。")
        print("   确实要换一套更难的尺子，请手改 tests/eval_holdout.lock.json，")
        print("   并在 devlog 里写明理由与日期——让这件事留下痕迹。")

    if raise_baseline:
        got = achieved(stats)
        old = lock.get("baseline") or {}
        lowered = [c for c in CLASS_FIELDS if c in old and got.get(c, 0) < old[c]]
        if lowered:
            print("XX 拒绝下调基准：" + "、".join(CLASS_LABEL[c] for c in lowered))
            print("     基准是棘轮，只能升不能降。系统确实变差了就先去修系统；")
            print("     如果这次变差是有意为之（比如换了一套更难的题），请手改锁文件并写进 devlog。")
            return 1
        new["baseline"] = got
        new["per_question"] = fingerprint
    else:
        print("· --update-lock 只刷新题库指纹/配置快照；基准与逐题基线保持不变。")
        print("  如果这次改动让某些题退化，普通检查会继续失败——那说明该修的是系统。")

    new["_说明"] = (
        "tests/eval_holdout.json（留出集）与 tests/eval_questions.json（dev 集）的指纹、"
        "逐题基准和检索配置快照。由 tests/test_eval_discipline.py --update-lock / "
        "--raise-baseline 生成，不要手改。"
    )
    LOCK_FILE.write_text(json.dumps(new, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("锁已更新：tests/eval_holdout.lock.json")
    return 0


def _run_checks() -> int:
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

    hq_total = len(_questions(holdout))
    print(f"留出集：tests/eval_holdout.json（{hq_total} 题，sha256 {_sha256(HOLDOUT_FILE)[:12]}…）")

    problems = check_static(holdout, lock, for_update=update_lock)
    for p in problems:
        print(f"XX {p}")

    print("· 跑一遍留出集（真实检索，不上大模型）")
    result = run_holdout()
    if result.get("error"):
        print(f"XX 留出集评测失败：{result['error']}")
        return 1
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

    per_worse, per_better = compare_per_question(
        _fingerprint(result), lock.get("per_question") or {}
    )
    for w in per_worse:
        print(f"XX 逐题退步：{w}")
    for b in per_better:
        print(f"OK 逐题进步：{b}")

    known = lock.get("known_gaps") or []
    if known:
        print("· 已知缺口（记录在锁里，不是本次新出现的退步）")
        for k in known:
            print(f"    - {k}")

    selftests = run_quality_self_tests()
    for s in selftests:
        print(f"XX 质量门自测失败：{s}")
    if not selftests:
        print("OK 质量门自测：部分正确会被拒、评测异常/题文件缺失会 fail-loud")

    blocking = bool(problems or worse or per_worse or selftests)
    if blocking:
        print(
            "留出集纪律检查：未通过"
            f"（{len(problems)} 项静态问题，{len(worse)} 项总数退步，"
            f"{len(per_worse)} 项逐题退步，{len(selftests)} 项自测失败）"
        )
        return 1

    if update_lock or raise_baseline:
        return write_lock(holdout, lock, stats, _fingerprint(result), raise_baseline, force)

    print("留出集纪律检查：全部通过")
    return 0



def main() -> int:
    """先隔离数据库，再跑检查；最后断言真实库没被碰过。"""
    tmp_dir, real_db, hash_before = _isolate_db()
    try:
        code = _run_checks()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    return _finish_db_guard(code, real_db, hash_before)


if __name__ == "__main__":
    sys.exit(main())
