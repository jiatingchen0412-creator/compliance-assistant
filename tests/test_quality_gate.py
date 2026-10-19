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
