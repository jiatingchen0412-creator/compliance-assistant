"""静态检查：Python 语法 + 前端 JS 语法 + JSON 格式。

为什么需要：
有一次 web/js/kb.js 里写了 `dengbao_2.0: '等保2.0'`——这不是合法的 JS 变量名，
整个知识库页面在浏览器里会直接失效。而当时的验证只检查了"文件能返回 HTTP 200"，
完全没发现。这个脚本把这类问题挡在交付之前。

用法（在项目根目录执行）：
    .venv\\Scripts\\python.exe tests\\check_syntax.py

需要本机装有 node（用于检查 JS）。没装的话会跳过 JS 检查并提示。
"""
from __future__ import annotations

import ast
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

# Windows 上标准输出被重定向（管道、写文件、CI 抓取）时，Python 按控制台代码页编码。
# 开发机是 GBK，中文编得出来，所以这个坑在本机一直看不见；
# GitHub 的英文 Windows runner 是 cp1252，打印中文直接 UnicodeEncodeError 把脚本打死
# ——CI 第一次跑就抓到了。
# 这里不能 `import app.console`（app/__init__.py 会自动修好编码），
# 因为本脚本刻意只依赖标准库，要能在装依赖之前先跑。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent.parent

SKIP_DIRS = {".venv", "__pycache__", ".git", "node_modules", "models"}


def iter_files(suffix: str, *roots: str):
    for root in roots:
        base = ROOT / root
        if not base.exists():
            continue
        for path in sorted(base.rglob(f"*{suffix}")):
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            # 带下划线前缀的是临时调试脚本，不参与检查
            if path.name.startswith("_"):
                continue
            yield path


def check_python() -> list[str]:
    problems = []
    files = list(iter_files(".py", "app", "tools", "tests"))
    for path in files:
        try:
            ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as exc:
            problems.append(f"{path.relative_to(ROOT)}:{exc.lineno} {exc.msg}")
        except Exception as exc:
            problems.append(f"{path.relative_to(ROOT)} 读取失败：{exc}")
    print(f"  Python 文件 {len(files)} 个")
    return problems


def check_js() -> list[str]:
    problems = []
    # 不只查 web/：tools/cdp_eval.js 和 tests/frontend_probe.js 也是要真跑起来的 JS，
    # 语法错了照样得在这里拦住
    files = list(iter_files(".js", "web", "tools", "tests"))
    node = shutil.which("node")
    if not node:
        print(f"  JS 文件 {len(files)} 个（未找到 node，跳过语法检查）")
        return problems
    for path in files:
        result = subprocess.run(
            [node, "--check", str(path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if result.returncode != 0:
            head = (result.stderr or "").strip().splitlines()
            detail = " | ".join(head[:3])
            problems.append(f"{path.relative_to(ROOT)} {detail}")
    print(f"  JS 文件 {len(files)} 个（已用 node --check 校验）")
    return problems


def check_json() -> list[str]:
    problems = []
    files = list(iter_files(".json", "data", "tests"))
    files = [p for p in files if "models" not in p.parts]
    for path in files:
        try:
            with open(path, "r", encoding="utf-8") as f:
                json.load(f)
        except Exception as exc:
            problems.append(f"{path.relative_to(ROOT)} {exc}")
    print(f"  JSON 文件 {len(files)} 个")
    return problems


ID_PATTERNS = [
    re.compile(r"""getElementById\(\s*['"]([^'"]+)['"]\s*\)"""),
    re.compile(r"""querySelector\(\s*['"]#([A-Za-z0-9_-]+)['"]\s*\)"""),
]
HTML_ID_PATTERN = re.compile(r"""id\s*=\s*['"]([A-Za-z0-9_-]+)['"]""")


def check_dom_ids() -> list[str]:
    """检查 JS 里用到的元素 ID 在 HTML 或动态生成的模板里确实存在。

    这类错误只在浏览器里才暴露，静态检查能提前抓到。
    """
    problems: list[str] = []
    pairs = [("web/js/chat.js", "web/index.html"), ("web/js/kb.js", "web/kb.html")]

    for js_rel, html_rel in pairs:
        js_path, html_path = ROOT / js_rel, ROOT / html_rel
        if not js_path.exists() or not html_path.exists():
            continue

        js_text = js_path.read_text(encoding="utf-8")
        html_text = html_path.read_text(encoding="utf-8")

        # HTML 里静态定义的 id + JS 模板字符串里动态生成的 id
        known = set(HTML_ID_PATTERN.findall(html_text))
        known |= set(HTML_ID_PATTERN.findall(js_text))

        used: set[str] = set()
        for pattern in ID_PATTERNS:
            used |= set(pattern.findall(js_text))

        missing = sorted(used - known)
        if missing:
            problems.append(f"{js_rel} 引用了不存在的元素 ID：{'、'.join(missing)}")
        print(f"  {js_rel} 引用 {len(used)} 个元素 ID，"
              f"{'全部存在' if not missing else f'缺失 {len(missing)} 个'}")
    return problems


SYMBOL_PATTERN = re.compile(r"""<symbol\s+id\s*=\s*['"]([^'"]+)['"]""")
ICON_CALL_PATTERN = re.compile(r"""ICON\(\s*['"]([^'"]+)['"]""")
USE_HREF_PATTERN = re.compile(r"""href\s*=\s*['"]#(i-[A-Za-z0-9_-]+)['"]""")


def check_svg_icons() -> list[str]:
    """检查 JS / HTML 引用的线性图标都在页面的 SVG 雪碧图里定义过。

    图标名字写错不会有任何报错——浏览器只是安静地画一片空白，
    打开页面才看得出，所以只能靠静态检查兜住。
    """
    problems: list[str] = []
    pairs = [("web/js/chat.js", "web/index.html"), ("web/js/kb.js", "web/kb.html")]

    for js_rel, html_rel in pairs:
        js_path, html_path = ROOT / js_rel, ROOT / html_rel
        if not js_path.exists() or not html_path.exists():
            continue

        js_text = js_path.read_text(encoding="utf-8")
        html_text = html_path.read_text(encoding="utf-8")

        defined = set(SYMBOL_PATTERN.findall(html_text))
        used = {"i-" + name for name in ICON_CALL_PATTERN.findall(js_text)}
        used |= set(USE_HREF_PATTERN.findall(html_text))
        used |= set(USE_HREF_PATTERN.findall(js_text))

        missing = sorted(used - defined)
        if missing:
            problems.append(f"{html_rel} 缺少图标定义：{'、'.join(missing)}")
        print(f"  {html_rel} 定义图标 {len(defined)} 个、引用 {len(used)} 个，"
              f"{'全部存在' if not missing else f'缺失 {len(missing)} 个'}")
    return problems


def check_bat_ascii() -> list[str]:
    """检查 .bat 是不是纯 ASCII。

    这条规则是踩坑定的：cmd.exe 按当前控制台代码页读 .bat，
    文件里有中文而代码页不匹配时命令会被撕碎（实测 `echo` 变成 `ho`），
    表现是**双击脚本什么都不发生**，非技术用户完全无从下手。
    详见 tools/fix_bat_encoding.py 头部的完整说明。
    """
    problems: list[str] = []
    bats = sorted(ROOT.glob("*.bat"))
    for path in bats:
        raw = path.read_bytes()
        bad = [b for b in raw if b > 127]
        if bad:
            problems.append(
                f"{path.name} 含 {len(bad)} 个非 ASCII 字节"
                f"（.bat 必须纯 ASCII，中文提示请交给 Python 输出）"
            )
    print(f"  bat 文件 {len(bats)} 个（必须纯 ASCII）")
    return problems


def check_console_encoding() -> list[str]:
    """独立脚本（不 import app 的那些）如果会打印非 ASCII，必须自己把 stdout 切成 UTF-8。

    背景见文件头：Windows 上输出被重定向时按代码页编码，cp1252 编不出中文，
    脚本会直接抛 UnicodeEncodeError 死掉。`import app.*` 会自动修好
    （`app/__init__.py` → `app/console.py`），但独立脚本没有这层保护。

    这条规则不是假想的：`tests/check_syntax.py` 自己就中过——
    本机 GBK 编得出来，所以一直没暴露，CI 的英文 runner 第一次跑就挂了。
    """
    problems: list[str] = []
    guarded: list[str] = []
    for path in iter_files(".py", "tools", "tests"):
        text = path.read_text(encoding="utf-8")
        # import app 即自动生效，不用自己处理
        if re.search(r"^\s*(?:from|import)\s+app\b", text, re.M):
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue                      # 语法问题由 check_python 报，这里不重复
        if not _prints_non_ascii(tree):
            continue
        rel = path.relative_to(ROOT)
        if _CONSOLE_GUARD.search(text):
            guarded.append(str(rel))
        else:
            problems.append(
                f"{rel} 会打印非 ASCII，却没把 stdout 切成 UTF-8"
                f"（在 Windows 英文环境下会直接崩，见 check_syntax.py 文件头）"
            )
    print(f"  独立脚本控制台编码：{len(guarded)} 个已加保护")
    return problems


# 认这几种写法都行：reconfigure(encoding="utf-8") / os.environ["PYTHONIOENCODING"]
_CONSOLE_GUARD = re.compile(
    r"reconfigure\(\s*encoding\s*=\s*[\"']utf-8|PYTHONIOENCODING"
)


def _prints_non_ascii(tree: ast.AST) -> bool:
    """有没有往标准输出写非 ASCII 字面量。只看字面量——变量里的中文静态看不出来。"""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        is_print = isinstance(fn, ast.Name) and fn.id == "print"
        is_write = (
            isinstance(fn, ast.Attribute)
            and fn.attr == "write"
            and isinstance(fn.value, ast.Attribute)
            and isinstance(fn.value.value, ast.Name)
            and fn.value.value.id == "sys"
        )
        if not (is_print or is_write):
            continue
        for sub in ast.walk(node):
            if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                if any(ord(ch) > 127 for ch in sub.value):
                    return True
    return False


def check_version_sync() -> list[str]:
    """版本号必须只有一个来源：`app/config.py` 的 `APP_VERSION`。

    `CHANGELOG.md` 里最新的一节 `## [vX.Y]` 必须与它一致。
    这里刻意不 `import app`（本脚本零依赖、要能在 venv 建好之前跑），
    所以用正则读源码而不是导入常量。
    """
    problems: list[str] = []
    cfg = ROOT / "app" / "config.py"
    try:
        text = cfg.read_text(encoding="utf-8")
    except OSError as exc:
        return [f"读不到 {cfg}：{exc}"]
    m = re.search(r'^APP_VERSION\s*=\s*"([^"]+)"', text, re.M)
    if not m:
        return ["app/config.py 里找不到 APP_VERSION（版本号必须有单一来源）"]
    version = m.group(1)

    changelog = ROOT / "CHANGELOG.md"
    try:
        cl = changelog.read_text(encoding="utf-8")
    except OSError as exc:
        return [f"读不到 {changelog}：{exc}"]
    m2 = re.search(r"^##\s*\[v([0-9][^\]]*)\]", cl, re.M)
    if not m2:
        return ["CHANGELOG.md 里找不到形如 `## [v1.6] - 2026-10-19` 的版本标题"]
    cl_version = m2.group(1)

    if version != cl_version:
        problems.append(
            f"版本号不一致：app/config.py 是 {version}，CHANGELOG.md 最新是 {cl_version}"
            f"（改一个就必须改另一个）"
        )
    else:
        print(f"  版本号一致：v{version}（config.py ⇄ CHANGELOG.md）")
    return problems


# ---------- 提示词版本：代码 = docs/05 变更表 = CHANGELOG 最新一节 ----------

_PROMPT_DOC_REL = "docs/05-问答与防幻觉规范.md"
_PROMPT_DOC_HEADING = "提示词版本变更表"


def _line_no(text: str, pos: int) -> int:
    """把字符串下标换算成 1 基行号。"""
    return text.count("\n", 0, pos) + 1


def check_prompt_version() -> list[str]:
    """提示词版本号必须三处一致。

    1. `app/prompt.py` 的 `PROMPT_VERSION`（正则读源码，不 import app）；
    2. `docs/05-问答与防幻觉规范.md` 的「提示词版本变更表」里有一行以该版本号开头；
    3. `CHANGELOG.md` 最新一节（第一个 `## [` 到下一个 `## [` 之间）里出现该版本号。

    只改了提示词却忘了升版本、或升了版本却忘了同步文档，都会红。
    """
    problems: list[str] = []
    prompt_py = ROOT / "app" / "prompt.py"
    try:
        src = prompt_py.read_text(encoding="utf-8")
    except OSError as exc:
        return [f"app/prompt.py:1 读不到：{exc}"]
    m = re.search(r'^PROMPT_VERSION\s*=\s*"([^"]+)"', src, re.M)
    if not m:
        return [
            "app/prompt.py:1 找不到模块级 PROMPT_VERSION 常量"
            "（提示词版本必须有单一来源）"
        ]
    version = m.group(1)
    version_line = _line_no(src, m.start())

    # 1) docs/05 的变更表
    doc_path = ROOT / "docs" / "05-问答与防幻觉规范.md"
    try:
        doc = doc_path.read_text(encoding="utf-8")
    except OSError as exc:
        problems.append(f"{_PROMPT_DOC_REL}:1 读不到：{exc}")
    else:
        heading_at = doc.find(_PROMPT_DOC_HEADING)
        if heading_at < 0:
            problems.append(
                f"{_PROMPT_DOC_REL}:1 找不到「{_PROMPT_DOC_HEADING}」小节"
                f"（代码里 PROMPT_VERSION = \"{version}\"）"
            )
        else:
            row = re.search(r"^\|\s*" + re.escape(version) + r"\s*\|", doc, re.M)
            if row is None:
                problems.append(
                    f"{_PROMPT_DOC_REL}:{_line_no(doc, heading_at)} "
                    f"「{_PROMPT_DOC_HEADING}」里没有 {version} 这一行，"
                    f"但 app/prompt.py:{version_line} 写的是 "
                    f"PROMPT_VERSION = \"{version}\"（升版本必须同步文档）"
                )

    # 2) CHANGELOG 最新一节
    changelog = ROOT / "CHANGELOG.md"
    try:
        cl = changelog.read_text(encoding="utf-8")
    except OSError as exc:
        problems.append(f"CHANGELOG.md:1 读不到：{exc}")
    else:
        lines = cl.splitlines()
        start = next((i for i, ln in enumerate(lines) if ln.startswith("## [")), None)
        if start is None:
            problems.append("CHANGELOG.md:1 找不到形如 `## [v1.6] - 2026-10-19` 的最新版本一节")
        else:
            end = next(
                (i for i in range(start + 1, len(lines)) if lines[i].startswith("## [")),
                len(lines),
            )
            section = "\n".join(lines[start:end])
            if version not in section:
                problems.append(
                    f"CHANGELOG.md:{start + 1} 最新一节（{lines[start].strip()}）里没有出现"
                    f"提示词版本 {version}（正文里加一行「提示词版本：{version}」即可）"
                )

    if not problems:
        print(f"  提示词版本 {version}：代码 ⇄ docs/05 变更表 ⇄ CHANGELOG 三方一致")
    return problems


# ---------- 文档里的数字 vs 代码里的真值 ----------

_DOC_SKIP = "doc-sync: skip"  # 写成 <!-- doc-sync: skip --> 即可豁免该行

# 这些是"当前态"断言。历史段落（"v1.0 时召回 25/25"）确实会与今天的数字不同，
# 那类句子在行尾写 `doc-sync: skip` 显式豁免——豁免是可见的，不会变成静默漂移。
_FRACTION_RULES = [
    # (规则名, 行内出现这些词才算断言, 允许的 (分子, 分母) 集合, 代码真值说明)
    ("reject", ("拒答正确率", "拒答率", "拒答题"), None, "reject_required/EXPECTED_COUNTS.reject"),
    ("recall", ("召回率",), None, "recall_min/EXPECTED_COUNTS.hit"),
    ("risk", ("风险标注率", "风险标注"), None, "risk_min/EXPECTED_COUNTS.risk"),
    ("clarify", ("追问正确率",), None, "clarify_required/EXPECTED_COUNTS.clarify"),
    ("outkb", ("库外法规识别率", "库外法规声明率"), None, "outkb_required/EXPECTED_COUNTS.out_of_kb"),
    ("undet", ("适用性判定拦截率",), None, "undet_required/EXPECTED_COUNTS.undeterminable"),
]

_INT_RULES = [    # (规则名, 正则, 期望值键, 真值说明)
    ("用例总数", re.compile(r"(\d+)\s*条用例"), "case_total", "CI 套件里 @case 的总数"),
    ("自动化套件数", re.compile(r"(\d+)\s*个自动化套件"), "suite_total", "ci.yml 里实际跑起来的 tests/*.py 数"),
    ("dev 集题数", re.compile(r"(\d+)\s*道评测题"), "dev_total", "tests/eval_questions.json 的 questions 数"),
    ("dev 集题数", re.compile(r"dev 集\s*\**\s*(\d+)\s*\**\s*题"), "dev_total", "同上"),
    ("dev 集题数", re.compile(r"(\d+)\s*题（(?:下称|上称) dev 集）"), "dev_total", "同上"),
    ("dev 集题数", re.compile(r"(\d+)\s*题（dev 集）"), "dev_total", "同上"),
    ("留出集题数", re.compile(r"留出集\s*\**\s*(\d+)\s*\**\s*题"), "holdout_total", "tests/eval_holdout.json 的 questions 数"),
]

# 单个套件的用例数：`tests/test_xxx.py`（N 条用例）
_SUITE_CASE_RE = re.compile(
    r"tests[/\\]([A-Za-z_]+\.py)`?\s*[（(]\s*(\d+)\s*条用例"
)


def _md_files() -> list[Path]:
    files = [ROOT / "README.md", ROOT / "CHANGELOG.md", ROOT / "CLAUDE.md"]
    files += sorted((ROOT / "docs").glob("*.md"))
    return [p for p in files if p.exists()]


def _code_constants() -> dict:
    """用正则读源码/题库，不 import app（本脚本要能在装依赖之前跑）。"""
    cfg = (ROOT / "app" / "config.py").read_text(encoding="utf-8")
    qtext = (ROOT / "tests" / "eval_questions.json").read_text(encoding="utf-8")
    qdata = json.loads(qtext)
    qs = qdata["questions"] if isinstance(qdata, dict) else qdata
    hdata = json.loads((ROOT / "tests" / "eval_holdout.json").read_text(encoding="utf-8"))
    hs = hdata["questions"] if isinstance(hdata, dict) else hdata

    def block(name: str, text: str) -> dict[str, int]:
        m = re.search(rf"^{name}\s*=\s*\{{(.*?)^\}}", text, re.S | re.M)
        return {k: int(v) for k, v in re.findall(r'"(\w+)"\s*:\s*(\d+)', m.group(1))} if m else {}

    quality = (ROOT / "app" / "quality.py").read_text(encoding="utf-8")
    counts: dict[str, int] = {}
    for q in qs:
        counts[str(q.get("expect"))] = counts.get(str(q.get("expect")), 0) + 1

    case_total = 0
    suite_cases: dict[str, int] = {}
    ci_yml = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    ci_scripts = sorted({Path(p).name for p in re.findall(r"tests[/\\]([A-Za-z_]+\.py)", ci_yml)})
    for name in ci_scripts:
        path = ROOT / "tests" / name
        if path.exists():
            n = len(re.findall(r"^@case", path.read_text(encoding="utf-8"), re.M))
            suite_cases[name] = n
            case_total += n
    # 套件数 = CI 真正跑起来的脚本数（含 check_syntax 与 run_eval）：
    # 文档里"12 个套件每次推送都在 CI 上跑"这句话要能对得上，就不能把没进 CI 的文件算进去。
    suite_total = len(ci_scripts)

    mver = re.search(r'^APP_VERSION\s*=\s*"([^"]+)"', cfg, re.M)
    return {
        "version": mver.group(1) if mver else "?",
        "red": block("RED_LINES", quality),
        "counts": block("EXPECTED_COUNTS", quality),
        "questions": counts,
        "dev_total": len(qs),
        "holdout_total": len(hs),
        "case_total": case_total,
        "suite_total": suite_total,
        "suite_cases": suite_cases,
    }


def check_doc_sync() -> list[str]:
    """README / docs 里的关键数字必须与代码常量一致。

    为什么需要：v1.6 交付时文档里还留着「拒答 5/5」「44 道评测题」「12 个套件 114 条用例」，
    而代码早就变成 10/10、49 题、137 条用例。文档不会自己报错，只能靠这一层比对。
    这里刻意不 import app：本脚本要能在 venv 装好之前跑，所以全部用正则读源码。

    约定：当前态数字必须对得上；历史段落的数字（"v1.0 时召回 25/25"）在行尾写
    `doc-sync: skip` 显式豁免。
    """
    const = _code_constants()
    red, counts = const["red"], const["counts"]
    problems: list[str] = []

    # 代码自身先自洽：一票否决项的 RED_LINES 必须等于 EXPECTED_COUNTS 里的题数
    for key in ("reject_required", "outkb_required", "undet_required", "clarify_required"):
        cat = {"reject_required": "reject", "outkb_required": "out_of_kb",
               "undet_required": "undeterminable", "clarify_required": "clarify"}[key]
        if key in red and cat in counts and red[key] != counts[cat]:
            problems.append(
                f"app/quality.py 自身不一致：RED_LINES.{key}={red[key]}，"
                f"但 EXPECTED_COUNTS.{cat}={counts[cat]}（文档没法同时对上两个数）"
            )

    allowed: dict[str, set[tuple[int, int]]] = {

        "reject": {(red.get("reject_required", 0), counts.get("reject", 0))},
        "recall": {(red.get("recall_min", 0), counts.get("hit", 0)),
                   (counts.get("hit", 0), counts.get("hit", 0))},
        "risk": {(red.get("risk_min", 0), counts.get("risk", 0)),
                 (counts.get("risk", 0), counts.get("risk", 0))},
        "clarify": {(red.get("clarify_required", 0), counts.get("clarify", 0)),
                    (counts.get("clarify", 0), counts.get("clarify", 0))},
        "outkb": {(red.get("outkb_required", 0), counts.get("out_of_kb", 0)),
                  (counts.get("out_of_kb", 0), counts.get("out_of_kb", 0))},
        "undet": {(red.get("undet_required", 0), counts.get("undeterminable", 0)),
                  (counts.get("undeterminable", 0), counts.get("undeterminable", 0))},
    }
    expected_desc = {
        "reject": f"{red.get('reject_required')}/{counts.get('reject')}",
        "recall": f"≥{red.get('recall_min')}/{counts.get('hit')}",
        "risk": f"≥{red.get('risk_min')}/{counts.get('risk')}",
        "clarify": f"{red.get('clarify_required')}/{counts.get('clarify')}",
        "outkb": f"{red.get('outkb_required')}/{counts.get('out_of_kb')}",
        "undet": f"{red.get('undet_required')}/{counts.get('undeterminable')}",
    }
    int_expected = {
        "case_total": const["case_total"],
        "suite_total": const["suite_total"],
        "dev_total": const["dev_total"],
        "holdout_total": const["holdout_total"],
    }

    scanned = 0
    for path in _md_files():
        rel = path.relative_to(ROOT).as_posix()
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if _DOC_SKIP in line:
                continue
            scanned += 1
            # 1) 版本号：`**版本**：v1.6`
            for ver in re.findall(r"版本\**\s*[:：]\s*v([0-9][0-9.]*)", line):
                if ver != const["version"]:
                    problems.append(
                        f"{rel}:{lineno} 写的 v{ver}，代码实际 v{const['version']}"
                        f"（app/config.py 的 APP_VERSION）"
                    )
            # 2) 红线类指标的分式
            for name, markers, _, desc in _FRACTION_RULES:
                if not any(m in line for m in markers):
                    continue
                for a, b in re.findall(r"(\d+)\s*/\s*(\d+)", line):
                    pair = (int(a), int(b))
                    if pair not in allowed[name]:
                        problems.append(
                            f"{rel}:{lineno} 写的 {a}/{b}，代码实际 {expected_desc[name]}"
                            f"（{desc}）"
                        )
            # 3) 题数 / 套件数 / 用例数
            for _name, pattern, key, desc in _INT_RULES:
                # 「12 条用例」也可能是在说某一个套件自己的用例数（如 test_devlog 的 12 条，
                # 见 docs/00 的遗留项表）。只有整句讲的是"套件总数"时才当作全量断言。
                if key == "case_total" and not (
                    "套件" in line or "逐条计数" in line or "用例总数" in line
                ):
                    continue
                for val in pattern.findall(line):
                    if int(val) != int_expected[key]:
                        problems.append(
                            f"{rel}:{lineno} 写的 {val}，代码实际 {int_expected[key]}"
                            f"（{desc}）"
                        )
            # 4) `RED_LINES.xxx` 表里的数值（CLAUDE.md 用表格列出来）
            for key, val in re.findall(r"`?(recall_min|reject_required|risk_min|outkb_required|"
                                       r"undet_required|clarify_required|forbid_max_false)`?\s*\|\s*(\d+)", line):
                real = red.get(key)
                if real is not None and int(val) != real:
                    problems.append(
                        f"{rel}:{lineno} 写的 {key}={val}，代码实际 {real}"
                        f"（app/quality.py 的 RED_LINES）"
                    )
            # 5) 单个套件的用例数：`tests/test_xxx.py`（N 条用例）
            #    为什么单独一条：报完 answer_render 的 12、t30 又加到 18，全仓数字对得上、
            #    这一句却没人改，因为我们只校验了总数。总数对了不代表分项对了。
            for name, num in _SUITE_CASE_RE.findall(line):
                real = const["suite_cases"].get(name)
                if real is not None and int(num) != real:
                    problems.append(
                        f"{rel}:{lineno} 写的 {name} 有 {num} 条用例，代码实际 {real} 条"
                        f"（tests/{name} 里 @case 的个数）"
                    )

    if problems:
        for p in problems:
            print(f"  文档/代码数字不一致：{p}")
    else:
        print(f"  文档数字一致：扫了 {len(_md_files())} 个 md、{scanned} 行")
    return problems


def main() -> int:
    print("=" * 62)
    print("静态检查 · Python / JavaScript / JSON / bat")
    print("=" * 62)

    problems = []
    problems += check_python()
    problems += check_js()
    problems += check_json()
    problems += check_dom_ids()
    problems += check_svg_icons()
    problems += check_bat_ascii()
    problems += check_console_encoding()
    problems += check_version_sync()
    problems += check_prompt_version()
    problems += check_doc_sync()

    print()
    if problems:
        print(f"发现 {len(problems)} 个问题：")
        for p in problems:
            print(f"  XX  {p}")
        return 1

    print("OK  全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
