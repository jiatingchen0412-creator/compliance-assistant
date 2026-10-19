"""开发日志读写测试。

`app/devlog.py` 是唯一一个此前没有测试的 app 模块。它值得测的原因是
**它坏掉不会报错**：`record_startup()` 里用 try/except 把异常吞掉了
（日志写不了不能影响主流程，这个取舍是对的），所以写坏了只会表现为
"今天没有自动记录"——没人会发现。这正是 CLAUDE.md 铁律二说的
"会静默失败的东西必须有自动化检查"。

所有用例都在临时目录里跑，**不碰仓库里的 `devlog/`**，也不依赖当天日期。

用法（在项目根目录执行）：
    .venv\\Scripts\\python.exe tests\\test_devlog.py
"""
from __future__ import annotations

import re
import shutil
import sys
import tempfile
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import devlog  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def case(name: str):
    def wrapper(fn):
        RESULTS.append((name, fn))
        return fn
    return wrapper


MINIMAL_TEMPLATE = """# 开发日志 · {date}

## 今日目标
- 

## 已完成
- 

## 待办
- 

## 问题与决策
- 

## 自动记录
- 
"""


@contextmanager
def sandbox(with_template: bool = False):
    """把 devlog 的目录指到临时目录，退出时还原。"""
    tmp = Path(tempfile.mkdtemp(prefix="devlog-test-"))
    old_dir, old_tpl = devlog.LOG_DIR, devlog.TEMPLATE_PATH
    devlog.LOG_DIR = tmp
    devlog.TEMPLATE_PATH = tmp / "TEMPLATE.md"
    try:
        if with_template:
            devlog.TEMPLATE_PATH.write_text(MINIMAL_TEMPLATE, encoding="utf-8")
        yield tmp
    finally:
        devlog.LOG_DIR, devlog.TEMPLATE_PATH = old_dir, old_tpl
        shutil.rmtree(tmp, ignore_errors=True)


def _section(text: str, name: str) -> str:
    """取某个小节正文（到下一个 `## ` 为止）。"""
    lines = text.splitlines()
    out: list[str] = []
    inside = False
    for line in lines:
        if line.strip() == f"## {name}":
            inside = True
            continue
        if inside and line.startswith("## "):
            break
        if inside:
            out.append(line)
    return "\n".join(out)


# --------------------------------------------------------------------------
# 建文件
# --------------------------------------------------------------------------
@case("ensure_today 用兜底模板建出当天的日志，五个小节齐全")
def test_ensure_creates_template():
    with sandbox() as tmp:
        path = devlog.ensure_today("2026-01-02")
        if not path.exists():
            return False, "文件没有建出来"
        if path.name != "2026-01-02.md":
            return False, f"文件名不对：{path.name}"
        text = path.read_text(encoding="utf-8")
        missing = [s for s in devlog.SECTIONS if f"## {s}" not in text]
        if missing:
            return False, f"缺少小节：{missing}"
        if "{date}" in text:
            return False, "模板里的 {date} 没有被替换"
        return True, f"{path.name} 含 5 个小节，{len(text)} 字"


@case("ensure_today 幂等：已有内容不会被模板覆盖")
def test_ensure_idempotent():
    with sandbox() as tmp:
        path = devlog.ensure_today("2026-01-02")
        path.write_text("# 我自己写的\n\n留着别删\n", encoding="utf-8")
        devlog.ensure_today("2026-01-02")
        text = path.read_text(encoding="utf-8")
        if "留着别删" not in text:
            return False, "第二次调用把用户手写的内容覆盖掉了"
        return True, "手写内容被保留"


@case("TEMPLATE.md 存在时优先用它（可以自定义格式）")
def test_uses_repo_template():
    with sandbox(with_template=True) as tmp:
        (tmp / "TEMPLATE.md").write_text(
            "# 自定义 · {date}\n\n## 今日目标\n- \n", encoding="utf-8"
        )
        text = devlog.read("2026-01-02")
        if "自定义" not in text:
            return False, f"没有用 TEMPLATE.md：{text[:60]!r}"
        if "2026-01-02" not in text:
            return False, "{date} 没替换成日期"
        return True, "用了 TEMPLATE.md 且替换了日期"


# --------------------------------------------------------------------------
# 追加
# --------------------------------------------------------------------------
@case("append 落在指定小节的末尾，而不是文件末尾")
def test_append_position():
    with sandbox():
        devlog.append("已完成", "第一条", "2026-01-02")
        devlog.append("已完成", "第二条", "2026-01-02")
        text = devlog.read("2026-01-02")
        done = _section(text, "已完成")
        if "第一条" not in done or "第二条" not in done:
            return False, f"内容没进『已完成』小节：{done!r}"
        if done.index("第一条") > done.index("第二条"):
            return False, "追加顺序反了"
        # 后面还有别的小节，说明确实插在中间而不是文件末尾
        if text.index("第二条") > text.index("## 待办"):
            return False, "被追加到了文件末尾，越过了后面的小节"
        return True, "两条都进了『已完成』且顺序正确"


@case("小节不存在时自动补一个，不报错")
def test_append_creates_missing_section():
    with sandbox():
        path = devlog.ensure_today("2026-01-02")
        path.write_text("# 光秃秃的日志\n", encoding="utf-8")
        devlog.append("问题与决策", "补出来的小节", "2026-01-02")
        text = path.read_text(encoding="utf-8")
        if "## 问题与决策" not in text or "补出来的小节" not in text:
            return False, f"没补出小节：{text!r}"
        return True, "缺小节时自动补齐"


@case("未知小节报 ValueError，而不是悄悄写进去")
def test_append_rejects_bad_section():
    with sandbox():
        try:
            devlog.append("不存在的小节", "x", "2026-01-02")
        except ValueError as exc:
            return True, f"正确拒绝：{exc}"
        except Exception as exc:  # noqa: BLE001
            return False, f"抛了别的异常：{type(exc).__name__}: {exc}"
        return False, "未知小节没有报错"


@case("占位行 `- ` 会被清掉，日志里不留空壳")
def test_placeholder_removed():
    with sandbox():
        devlog.done("真正的一条", "2026-01-02")
        text = devlog.read("2026-01-02")
        done = _section(text, "已完成")
        bare = [ln for ln in done.splitlines() if ln.strip() == "-"]
        if bare:
            return False, f"『已完成』里还留着 {len(bare)} 个空占位行"
        if "真正的一条" not in done:
            return False, "真正的内容没写进去"
        return True, "占位行已清掉，只剩实条目"


@case("done / todo / decision 各归各的小节，并带 HH:MM 时间戳")
def test_shortcuts():
    with sandbox():
        devlog.done("做成了一件事", "2026-01-02")
        devlog.todo("下次要做的事", "2026-01-02")
        devlog.decision("为什么这么做", "2026-01-02")
        text = devlog.read("2026-01-02")
        checks = [
            ("已完成", "做成了一件事"),
            ("待办", "下次要做的事"),
            ("问题与决策", "为什么这么做"),
        ]
        for section, keyword in checks:
            body = _section(text, section)
            if keyword not in body:
                return False, f"{keyword!r} 没进『{section}』"
        ok, msg = True, "三条各归其位"
        # 时间戳形态：- 09:41 xxx
        if not re.search(r"^- \d{2}:\d{2} 做成了一件事$", _section(text, "已完成"), re.M):
            ok, msg = False, "时间戳格式不对（应为 `- HH:MM 内容`）"
        return ok, msg


@case("多行文本写成一条 bullet，续行缩进对齐")
def test_multiline():
    with sandbox():
        devlog.decision("第一行\n第二行\n第三行", "2026-01-02")
        body = _section(devlog.read("2026-01-02"), "问题与决策")
        lines = [ln for ln in body.splitlines() if ln.strip()]
        if len(lines) != 3:
            return False, f"应有 3 行，实际 {len(lines)} 行：{lines}"
        if not lines[0].startswith("- ") or not lines[0].endswith("第一行"):
            return False, f"首行格式不对：{lines[0]!r}"
        if any(not ln.startswith("  ") for ln in lines[1:]):
            return False, f"续行没缩进：{lines[1:]!r}"
        return True, "3 行合成 1 条，续行缩进 2 格"


# --------------------------------------------------------------------------
# 列举与汇总
# --------------------------------------------------------------------------
@case("list_days 只认 YYYY-MM-DD 文件名，新的排在前面")
def test_list_days():
    with sandbox() as tmp:
        for name in ("2026-01-01.md", "2026-01-03.md", "2026-01-02.md"):
            (tmp / name).write_text("x\n", encoding="utf-8")
        (tmp / "TEMPLATE.md").write_text("x\n", encoding="utf-8")
        (tmp / "README.md").write_text("x\n", encoding="utf-8")
        days = devlog.list_days()
        if days != ["2026-01-03", "2026-01-02", "2026-01-01"]:
            return False, f"排序或过滤不对：{days}"
        if devlog.list_days(limit=2) != ["2026-01-03", "2026-01-02"]:
            return False, "limit 没有生效"
        return True, f"{days}（TEMPLATE.md / README.md 已排除）"


@case("summary 汇总三个小节，跳过空占位行")
def test_summary():
    with sandbox():
        today = date.today().strftime("%Y-%m-%d")
        devlog.done("汇总里该出现的已完成", today)
        devlog.todo("汇总里该出现的待办", today)
        # 「今日目标」不该进汇总
        devlog.append("今日目标", "这行不该进汇总", today)
        text = devlog.summary(days=7)
        for kw in ("汇总里该出现的已完成", "汇总里该出现的待办"):
            if kw not in text:
                return False, f"汇总里缺少 {kw!r}"
        if "这行不该进汇总" in text:
            return False, "『今日目标』被误收进汇总了"
        if re_search_bare_bullet(text):
            return False, "汇总里混进了空占位行"
        return True, f"汇总 {len(text)} 字，三个小节各就各位"


def re_search_bare_bullet(text: str) -> bool:
    return re.search(r"^-\s*$", text, re.M) is not None


@case("record_startup 写一条自动记录，且任何情况下都不抛异常")
def test_record_startup():
    with sandbox():
        day = date.today().strftime("%Y-%m-%d")
        devlog.record_startup(clause_count=283, model="qwen2.5:7b-instruct", model_ready=True)
        body = _section(devlog.read(day), "自动记录")
        for kw in ("服务启动", "283", "qwen2.5:7b-instruct", "就绪"):
            if kw not in body:
                return False, f"自动记录里缺少 {kw!r}：{body!r}"
        # 目录不可写时不能炸
        devlog.LOG_DIR = Path(tempfile.gettempdir()) / "不存在" / "\0bad"
        try:
            devlog.record_startup(clause_count=0, model="", model_ready=False)
        except Exception as exc:  # noqa: BLE001
            return False, f"写不了日志时抛异常了：{type(exc).__name__}: {exc}"
        return True, "正常写入；写不了时静默吞掉（不影响主流程）"


# --------------------------------------------------------------------------
def main() -> int:
    print("=" * 70)
    print("开发日志读写测试 · 不依赖 Ollama 与数据库，全部在临时目录里跑")
    print("=" * 70)

    passed = failed = 0
    for name, fn in RESULTS:
        try:
            ok, msg = fn()
        except Exception as exc:  # noqa: BLE001
            ok, msg = False, f"测试本身出错：{type(exc).__name__}: {exc}"
        print(f"{'OK ' if ok else 'XX '}{name}")
        print(f"     {msg}")
        if ok:
            passed += 1
        else:
            failed += 1

    print("=" * 70)
    print(f"通过 {passed} / {len(RESULTS)}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
