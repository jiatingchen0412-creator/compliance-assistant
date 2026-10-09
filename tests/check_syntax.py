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
