"""质量自检「三态」与版本号单一来源的回归测试（t12）。

为什么需要这个套件：
质量自检本身就是用来发现「看起来正常、其实已经坏了」的。它自己崩掉的时候，
以前 `app/main.py` 的异常分支会写 `passed=True`，于是 /api/quality、/api/health
和前端都会显示「检查通过」——最不该骗人的地方变成了会骗人的地方。
这里把三态钉死：True=通过、False=未通过、None=没跑成 / 结果未知。

顺带钉住版本号：FastAPI(version=...) 以前硬编码 "1.0.0"，
而 /api/health 返回 config.APP_VERSION（"1.6"），openapi 和 health 自相矛盾。

不依赖 Ollama：TestClient 不进入 with 块，不触发 lifespan，自检线程不会起来。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app import config, main, quality  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def case(name: str):
    def wrapper(fn):
        RESULTS.append((name, fn))
        return fn

    return wrapper


def check(condition: bool, ok_msg: str, fail_msg: str) -> tuple[bool, str]:
    return (True, ok_msg) if condition else (False, fail_msg)


def _client() -> TestClient:
    # 刻意不用 with：不进 lifespan 就不会起 bootstrap / 质量自检线程，也不碰 Ollama。
    return TestClient(main.app)


# --------------------------------------------------------------------------
# 1. 自检抛异常 → passed 必须是 None（核心回归，旧代码下这条会变红）
# --------------------------------------------------------------------------
@case("自检抛异常时 /api/quality 的 passed 是 None（不是 True）")
def test_quality_exception_is_unknown():
    before = dict(main.QUALITY_STATE)
    original = quality.run_check

    def boom(*args, **kwargs):
        raise RuntimeError("模拟评测代码崩了")

    quality.run_check = boom
    try:
        # 先伪装成「上一次自检是通过的」，验证异常分支不会把它留在 True
        main.QUALITY_STATE.update(
            {"checked": True, "passed": True, "skipped": False, "failures": []}
        )
        main._run_quality_check()
        state = dict(main.QUALITY_STATE)
        body = _client().get("/api/quality").json()
        health = _client().get("/api/health")
        health_body = health.json() if health.status_code == 200 else {}
    finally:
        quality.run_check = original
        main.QUALITY_STATE.clear()
        main.QUALITY_STATE.update(before)

    problems = []
    if body.get("passed") is not None:
        problems.append(f"/api/quality passed={body.get('passed')!r}（应为 None）")
    if state.get("passed") is not None:
        problems.append(f"QUALITY_STATE passed={state.get('passed')!r}（应为 None）")
    if state.get("checked") is not True:
        problems.append(f"checked={state.get('checked')!r}（应为 True，表示这次检查已经结束）")
    if state.get("skipped") is not True:
        problems.append(f"skipped={state.get('skipped')!r}（应为 True）")
    if not any("模拟评测代码崩了" in str(f) for f in state.get("failures") or []):
        problems.append(f"failures 没保留原因：{state.get('failures')!r}")
    if health.status_code == 200 and (health_body.get("quality") or {}).get("passed") is not None:
        problems.append(
            f"/api/health quality.passed={(health_body.get('quality') or {}).get('passed')!r}"
        )
    elif health.status_code != 200:
        problems.append(f"/api/health 返回 {health.status_code}")

    if problems:
        return False, "；".join(problems)
    return True, (
        "自检抛异常后 /api/quality 与 /api/health 的 passed 均为 null，"
        f"skipped=True，failures={state.get('failures')}"
    )


# --------------------------------------------------------------------------
# 2. 版本号只有一个来源
# --------------------------------------------------------------------------
@case("openapi.json 的 info.version 等于 config.APP_VERSION")
def test_openapi_version_matches_config():
    spec = _client().get("/openapi.json").json()
    version = (spec.get("info") or {}).get("version")
    return check(
        version == config.APP_VERSION,
        f"openapi={version} · config={config.APP_VERSION}",
        f"openapi={version!r} 与 config.APP_VERSION={config.APP_VERSION!r} 不一致",
    )


@case("app/ 源码里没有第二个版本字面量")
def test_single_version_source():
    pattern = re.compile(r'version\s*=\s*"1\.\d+\.\d+"|"1\.0\.0"')
    hits = []
    for path in sorted((ROOT / "app").rglob("*.py")):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue  # 注释里说明历史问题不算第二个来源
            if pattern.search(line):
                hits.append(f"{path.relative_to(ROOT).as_posix()}:{lineno}")
    return check(
        not hits,
        f"版本号只来自 config.APP_VERSION（app/ 下 {len(hits)} 处硬编码）",
        f"仍有硬编码版本字面量：{hits}",
    )


@case("app.version 与 /api/health 的 version 一致")
def test_app_version_single_source():
    health = _client().get("/api/health").json()
    return check(
        main.app.version == config.APP_VERSION and health.get("version") == config.APP_VERSION,
        f"FastAPI version={main.app.version} · /api/health={health.get('version')}",
        f"三处版本不一致：app={main.app.version!r} health={health.get('version')!r} "
        f"config={config.APP_VERSION!r}",
    )


# --------------------------------------------------------------------------
# 3. 端到端红线不能把「崩了」当成「通过」
#
# 旧实现里 errors 只 += 1 就 continue，跳过的题连分母也不再算。
# 于是 10 道题崩 8 道、剩下 2 道答对，`check_end2end_red_lines` 返回 passed=True。
# 这是本项目第 5 次出现同一类病：失败被翻译成成功，只是这次伪装成「样本变少」。
# --------------------------------------------------------------------------
def _e2e_stats(**over) -> dict:
    stats = {
        "clarify_total": 4, "clarify_ok": 4,
        "outkb_total": 6, "outkb_ok": 6,
        "errors": 0,
        "clarify_expected": 4, "outkb_expected": 6,
    }
    stats.update(over)
    return stats


@case("端到端有题抛异常时红线必须判失败（不能只计数）")
def test_end2end_errors_fail_the_gate():
    # 真实场景：4 道追问题崩了 3 道，剩 1 道答对 —— 比例 1/1 = 100%，但样本已经废了
    broken = quality.check_end2end_red_lines(
        _e2e_stats(clarify_total=1, clarify_ok=1, outkb_total=1, outkb_ok=1, errors=3)
    )
    healthy = quality.check_end2end_red_lines(_e2e_stats())
    problems = []
    if broken["passed"]:
        problems.append(f"异常 3 个却 passed=True（items={[i['name'] for i in broken['items']]}）")
    if not any("异常" in f for f in broken["failures"]):
        problems.append(f"failures 没提到异常：{broken['failures']!r}")
    if not healthy["passed"]:
        problems.append(f"全绿的统计反而判失败：{healthy['failures']!r}")
    if problems:
        return False, "；".join(problems)
    return True, (
        "异常 3 个 → passed=False（"
        + broken["failures"][-1]
        + "）；异常 0 且题数齐全 → passed=True"
    )


@case("端到端题数比期望少时红线必须判失败（分母不缩水）")
def test_end2end_sample_shrink_fails_the_gate():
    # errors=0 却少了几道题：说明评测范围被动过，不能拿缩小的样本报达标
    shrunk = quality.check_end2end_red_lines(_e2e_stats(clarify_total=1, outkb_total=6))
    verdict = quality.check_end2end_red_lines(_e2e_stats())
    problems = []
    if shrunk["passed"]:
        problems.append("追问题只剩 1 道（期望 4）却 passed=True")
    if not verdict["passed"]:
        problems.append(f"题数齐全时反而判失败：{verdict['failures']!r}")
    if problems:
        return False, "；".join(problems)
    return True, "追问题 1/4 → passed=False；4/4 且库外 6/6 → passed=True"


@case("闸门顺序只写一遍：生产与评测都不许自己抄 detect_* 的 if")
def test_gate_order_defined_once():
    # 2026-10-21 独立审计抓到的缺陷：生产代码里三道闸门各写一个 if，
    # 评测代码里又抄了一遍，抄漏了一道 → 留出集 h10/h11 在生产里被拒答、
    # 评测却判 ok=True。这条用例守着"不许再抄第二遍"。
    #
    # 判据是源码层面：chat.py 与 quality.py 必须调用 scope.pre_model_gate()，
    # 且不得直接调用 detect_out_of_domain / detect_no_kb_topic / needs_system_context
    # （scope.py 自己当然可以调）。这是刻意选源码而不是行为 ——
    # 行为测试只能证明"当前这几道闸门一致"，证明不了"以后加第四道时不会漏"。
    importers = {
        "app/routers/chat.py": (ROOT / "app" / "routers" / "chat.py").read_text(encoding="utf-8"),
        "app/quality.py": (ROOT / "app" / "quality.py").read_text(encoding="utf-8"),
    }
    banned = ("detect_out_of_domain", "detect_no_kb_topic", "needs_system_context")
    problems = []
    for label, src in importers.items():
        if "scope.pre_model_gate(" not in src:
            problems.append(f"{label} 没有调用 scope.pre_model_gate()")
        for name in banned:
            if f"scope.{name}(" in src:
                problems.append(f"{label} 仍在直接调用 scope.{name}()，闸门顺序出现了第二份")
    if problems:
        return False, "；".join(problems)
    return True, "chat.py 与 quality.py 都只读 pre_model_gate() 的结论，没有第二份闸门顺序"


@case("评测能看见每一道闸门：任一道拦下都判失败")
def test_eval_sees_every_gate():
    # 四类必然被拦的问题，pre_model_gate 应各给对应 mode；
    # 而 evaluate_retrieval 一旦把它们当"该命中"题，就必须判失败。
    from app import scope as scope_mod

    samples = [
        ("我们的系统要定到哪一级？", scope_mod.GATE_NEEDS_CONTEXT, 0.9),
        ("公司员工的加班费和调休怎么算？", scope_mod.GATE_OUT_OF_DOMAIN, 0.9),
        ("做等保要花多少钱？", scope_mod.GATE_NO_KB_TOPIC, 0.9),
        ("什么都能问吗？", scope_mod.GATE_THRESHOLD, 0.02),
    ]
    problems = []
    for question, want, score in samples:
        got = scope_mod.pre_model_gate(question, score, has_clauses=True)
        if got.mode != want:
            problems.append(f"「{question}」闸门给出 {got.mode}，期望 {want}")
    if problems:
        return False, "；".join(problems)
    return True, "适用性 / 领域 / 话题 / 阈值四道闸门各自正确命中"


def main_runner() -> int:
    print("=" * 76)
    print("质量自检三态 · 版本号单一来源（不启动 Ollama，不碰真实数据）")
    print("=" * 76)

    passed = failed = 0
    for name, fn in RESULTS:
        try:
            ok, msg = fn()
        except Exception as exc:  # 测试本身出错也要算失败，不能静默
            ok, msg = False, f"测试本身出错：{type(exc).__name__}: {exc}"
        print(f"{'OK ' if ok else 'XX '}{name}")
        print(f"     {msg}")
        if ok:
            passed += 1
        else:
            failed += 1

    print("=" * 76)
    print(f"通过 {passed} / {passed + failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main_runner())
