"""前端回归测试：无障碍、标题层级、键盘可达性、移动端布局与抽屉交互。

为什么需要这个文件：
    `tests/check_syntax.py` 只能证明"JS 语法没错、引用的元素 ID 都存在"，
    证明不了页面在真实浏览器里长什么样、键盘能不能用、屏幕阅读器读不读得懂。
    改一次 CSS 类名或换一种 DOM 结构，语法检查照样全绿，但页面可能已经废了。

服务怎么起（关键设计）：
    不启动完整的 `app.main`——那会加载 fastembed、探测 Ollama，几十秒且依赖外部服务。
    这里直接 import `app.main` 里的 FastAPI 实例，用 uvicorn 以 `lifespan="off"`
    在后台线程起一个临时端口的服务：路由和数据库全是真的（真实那 283 条条款），
    只是跳过了启动引导。实测 1.4 秒就绪。

    数据库用**真实库的临时副本**：这样"会话列表里真的有东西"这类前置条件成立
    （空列表测不出列表语义），又不会动用户自己的历史记录。

依赖：Node（跑 tools/cdp_eval.js）+ 本机 Chrome + tests/vendor/axe.min.js。
    缺任何一个都直接判失败并说明缺什么，不静默跳过——前端回归没跑就等于没测。

用法（在项目根目录执行）：
    .venv\\Scripts\\python.exe tests\\test_frontend.py
"""
from __future__ import annotations

import json
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

RESULTS: list[tuple[str, object]] = []
ENV: dict = {}

AXE = ROOT / "tests" / "vendor" / "axe.min.js"
PROBE = ROOT / "tests" / "frontend_probe.js"
CDP = ROOT / "tools" / "cdp_eval.js"


def case(name: str):
    def wrapper(fn):
        RESULTS.append((name, fn))
        return fn
    return wrapper


# ---------------------------------------------------------------- 基础设施

def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _find_node() -> str | None:
    """找 node.exe。PATH 里没有就翻几个常见安装位置，再不行看 NODE_EXE 环境变量。"""
    import os
    import shutil

    found = shutil.which("node")
    if found:
        return found
    cands = [
        os.getenv("NODE_EXE", ""),
        str(Path(os.getenv("ProgramFiles", r"C:\Program Files")) / "nodejs" / "node.exe"),
        str(Path(os.getenv("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "nodejs" / "node.exe"),
        str(Path(os.getenv("LOCALAPPDATA", "")) / "Programs" / "nodejs" / "node.exe"),
    ]
    for cand in cands:
        if cand and Path(cand).exists():
            return cand
    return None


def _copy_db(dst: Path) -> str:
    """把真实数据库复制一份到临时目录。用 sqlite 自己的 backup API 而不是复制文件，
    避免拷到写了一半的页（备份模块 app/backup.py 用的是同一招）。"""
    src = ROOT / "data" / "app.db"
    with sqlite3.connect(str(dst)) as out:
        if src.exists():
            with sqlite3.connect(str(src)) as s:
                s.backup(out)
        # 表结构保证存在（库是新建的时候也要能跑）
        out.executescript(
            "CREATE TABLE IF NOT EXISTS sessions ("
            " id TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '新对话',"
            " created_at TEXT, updated_at TEXT);"
        )
        n = out.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        if n == 0:
            # 空列表验不出"每项都声明了 listitem"，所以在只读副本里补几条假会话。
            # 故意用一条很长的标题，顺带压到 CSS 的省略号处理。
            now = "2026-01-01 00:00:00"
            out.executemany(
                "INSERT INTO sessions (id, title, created_at, updated_at) VALUES (?,?,?,?)",
                [
                    ("t1", "我们公司的数据安全吗？", now, now),
                    ("t2", "系统日志要保存多久，有没有明确规定", now, now),
                ],
            )
        out.commit()
    return str(dst)


def _start_server(port: int):
    """在后台线程里起一个跳过启动引导的真实服务，返回 (server, thread)。"""
    import uvicorn

    from app.main import STARTUP_STATE, app

    # lifespan="off" 意味着启动引导根本不会跑，STARTUP_STATE 会永远停在 ready=False。
    # 而首页的 init() 是先 await pollStartup()、等 ready 之后才去加载会话列表和健康状态的，
    # 不置成就绪的话页面会一直盖在启动遮罩上——那样测到的是遮罩，不是真正的界面
    # （第一版就踩了这个坑：会话列表永远是空的，其实是脚本压根还没跑到底）。
    STARTUP_STATE.update({
        "phase": "ready", "message": "已就绪", "ready": True,
        "progress": 1, "total": 1, "detail": "",
    })

    config = uvicorn.Config(app, host="127.0.0.1", port=port,
                            lifespan="off", log_level="error", access_log=False)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="frontend-test-server", daemon=True)
    thread.start()
    return server, thread


def _wait_ready(url: str, timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.2)
    return False


def _probe(node: str, base: str, path: str, *, mobile: bool, out: Path) -> dict:
    cmd = [
        node, str(CDP),
        "--url", base + path,
        "--script", str(PROBE),
        "--axe", str(AXE),
        "--settle", "3500",
        "--out", str(out),
    ]
    if mobile:
        cmd += ["--mobile", "--width", "390", "--height", "844"]
    proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=240)
    if not out.exists():
        raise RuntimeError(
            f"cdp_eval 没写出结果文件（退出码 {proc.returncode}）\n"
            f"  stdout: {proc.stdout[-600:]}\n  stderr: {proc.stderr[-600:]}"
        )
    data = json.loads(out.read_text(encoding="utf-8"))
    if not data.get("ok"):
        raise RuntimeError(f"页面探针失败：{data.get('error')}")
    return data["value"]


def _fmt_violations(items: list) -> str:
    parts = []
    for v in items:
        where = " / ".join(v["nodes"][:3])
        more = f" 等 {len(v['nodes'])} 处" if len(v["nodes"]) > 3 else ""
        parts.append(f"{v['id']}[{v['impact']}] @ {where}{more}")
    return "；".join(parts)


def _heading_problem(headings: list) -> str | None:
    h1 = [h for h in headings if h["level"] == 1]
    if len(h1) != 1:
        return f"页面里有 {len(h1)} 个 h1，应该正好 1 个"
    prev = 0
    for h in headings:
        lv = h["level"]
        if prev and lv > prev + 1:
            return f"标题层级从 h{prev} 跳到 h{lv}：「{h['text']}」"
        prev = lv
    return None


def _missing(probe: dict, required: list[str]) -> list[str]:
    return [i for i in required if not probe["present"].get(i)]


HOME_IDS = ["chatScroll", "input", "btnSend", "sessionList", "statusChip", "navToggle", "navScrim"]
KB_IDS = ["treeBox", "clauseList", "pager", "statGrid", "stdSelect", "fileInput",
          "searchBox", "statusChip", "navToggle", "navScrim"]


# ---------------------------------------------------------------- 用例

@case("首页加载期没有 JS 报错")
def t_home_errors():
    errs = ENV["home"]["errors"]
    return (not errs, "无报错" if not errs else f"抓到 {len(errs)} 条：{errs[:3]}")


@case("知识库页加载期没有 JS 报错")
def t_kb_errors():
    errs = ENV["kb"]["errors"]
    return (not errs, "无报错" if not errs else f"抓到 {len(errs)} 条：{errs[:3]}")


@case("首页 axe 无障碍违规为 0")
def t_home_axe():
    v = ENV["home"]["violations"]
    return (not v, "无违规" if not v else f"{len(v)} 类：{_fmt_violations(v)}")


@case("知识库页 axe 无障碍违规为 0")
def t_kb_axe():
    v = ENV["kb"]["violations"]
    return (not v, "无违规" if not v else f"{len(v)} 类：{_fmt_violations(v)}")


@case("首页 对比度待定项全部在可视区之外（几何裁剪，不是真的对比度问题）")
def t_home_contrast():
    items = ENV["home"]["incompleteContrast"]
    bad = [i for i in items if i.get("inViewport")]
    if bad:
        return (False, "视口内仍有判不出对比度的元素：" + "；".join(
            f"{b['sel']} ({b['w']}x{b['h']})" for b in bad[:3]))
    return (True, f"{len(items)} 个待定项都在视口外（axe 拿不到背景色属正常）")


@case("知识库页 对比度待定项全部在可视区之外")
def t_kb_contrast():
    items = ENV["kb"]["incompleteContrast"]
    bad = [i for i in items if i.get("inViewport")]
    if bad:
        return (False, "视口内仍有判不出对比度的元素：" + "；".join(
            f"{b['sel']} ({b['w']}x{b['h']})" for b in bad[:3]))
    return (True, f"{len(items)} 个待定项都在视口外")


@case("首页 标题层级：正好一个 h1 且不跳级")
def t_home_headings():
    p = _heading_problem(ENV["home"]["headings"])
    if p:
        return (False, p)
    outline = " > ".join(f"h{h['level']}" for h in ENV["home"]["headings"])
    return (True, f"层级 {outline}")


@case("知识库页 标题层级：正好一个 h1 且不跳级")
def t_kb_headings():
    p = _heading_problem(ENV["kb"]["headings"])
    if p:
        return (False, p)
    outline = " > ".join(f"h{h['level']}" for h in ENV["kb"]["headings"])
    return (True, f"层级 {outline}")


@case("首页 历史对话每一项都声明了 role=listitem")
def t_list_roles():
    roles = ENV["home"]["listRoles"]
    if not roles:
        return (False, "会话列表是空的，验不出列表语义（临时库里没有任何会话）")
    bad = [r for r in roles if r != "listitem"]
    if bad:
        return (False, f"{len(roles)} 项里有 {len(bad)} 项没有 role=listitem")
    return (True, f"{len(roles)} 项全部是 listitem")


@case("首页 历史对话里有可聚焦控件（键盘能选对话）")
def t_list_focusable():
    n = ENV["home"]["focusable"]["sessionList"]
    return (n > 0, f"可聚焦控件 {n} 个" if n else "一个可聚焦控件都没有，键盘用户切不了对话")


@case("知识库页 目录树里有可聚焦控件（键盘能选章节）")
def t_tree_focusable():
    n = ENV["kb"]["focusable"]["treeBox"]
    return (n > 0, f"可聚焦控件 {n} 个" if n else "一个可聚焦控件都没有，键盘用户选不了章节")


@case("Inter 字体真的加载了")
def t_font():
    f = ENV["home"]["font"]
    if not f["interLoaded"]:
        return (False, f"document.fonts.check 为 false；body 字体栈 = {f['bodyFamily']}")
    return (True, f"status={f['status']}，body 首选 = {f['bodyFamily'].split(',')[0]}")


@case("首页 关键元素齐全")
def t_home_ids():
    miss = _missing(ENV["home"], HOME_IDS)
    return (not miss, "七个关键元素都在" if not miss else f"缺少：{miss}")


@case("知识库页 关键元素齐全")
def t_kb_ids():
    miss = _missing(ENV["kb"], KB_IDS)
    return (not miss, "十个关键元素都在" if not miss else f"缺少：{miss}")


@case("390px 首页没有横向溢出")
def t_home_mobile_overflow():
    p = ENV["home_mobile"]
    w, sw = p["viewport"]["w"], p["doc"]["scrollWidth"]
    return (sw <= w + 1, f"视口 {w}px，文档宽 {sw}px")


@case("390px 知识库页没有横向溢出")
def t_kb_mobile_overflow():
    p = ENV["kb_mobile"]
    w, sw = p["viewport"]["w"], p["doc"]["scrollWidth"]
    return (sw <= w + 1, f"视口 {w}px，文档宽 {sw}px")


@case("390px 抽屉导航：按钮可见、能打开、遮罩能关闭")
def t_mobile_drawer():
    d = ENV["home_mobile"]["drawer"] or {}
    if not d.get("buttonVisible"):
        return (False, "窄屏下汉堡按钮不可见，手机上打不开侧栏")
    if not d.get("openClass"):
        return (False, "点了汉堡按钮但 body 没有加上 nav-open")
    left = d.get("left")
    # 注意别写成 `if (left or -999) < -1`——left 正常就是 0，会被当成假值判错
    if left is None or left < -1:
        return (False, f"侧栏没有进入视口（left={left}px）")
    if d.get("ariaExpanded") != "true":
        return (False, f"aria-expanded 没有同步成 true（实际 {d.get('ariaExpanded')}）")
    if not d.get("closedAfterScrim"):
        return (False, "点了遮罩侧栏没有收回")
    return (True, f"打开后侧栏 left={d['left']}px，aria-expanded=true，点遮罩已收回")


# ---------------------------------------------------------------- 主流程

def main() -> int:
    print("=" * 70)
    print("前端回归测试 · 真实 Chrome + axe-core")
    print("=" * 70)

    node = _find_node()
    if not node:
        print("找不到 Node.js，无法驱动浏览器。请安装 Node 或设置 PATH。")
        return 1
    if not CDP.exists():
        print(f"缺少 {CDP.relative_to(ROOT)}")
        return 1
    if not AXE.exists():
        print(f"缺少 {AXE.relative_to(ROOT)}（axe-core，仅测试用）")
        return 1

    tmp = Path(tempfile.mkdtemp(prefix="frontend_test_"))
    server = None
    try:
        db_path = _copy_db(tmp / "app.db")
        import app.config as config
        config.DB_PATH = Path(db_path)

        port = _free_port()
        server, _ = _start_server(port)
        base = f"http://127.0.0.1:{port}"
        if not _wait_ready(base + "/api/health"):
            print(f"临时服务没能在 30 秒内就绪（{base}/api/health）")
            return 1
        print(f"临时服务已就绪：{base}（数据库用的是临时副本，不影响您的历史记录）")

        for key, path, mobile in (("home", "/", False), ("kb", "/kb", False),
                                  ("home_mobile", "/", True), ("kb_mobile", "/kb", True)):
            label = f"{path}{' @390px' if mobile else ' @桌面'}"
            print(f"  正在采集 {label} …")
            ENV[key] = _probe(node, base, path, mobile=mobile, out=tmp / f"{key}.json")
        print()
    except Exception:
        print("采集阶段出错：")
        traceback.print_exc()
        return 1
    finally:
        if server is not None:
            server.should_exit = True
        import shutil
        time.sleep(0.3)
        shutil.rmtree(tmp, ignore_errors=True)

    passed = failed = 0
    for name, fn in RESULTS:
        try:
            ok, msg = fn()
        except Exception as exc:
            ok, msg = False, f"测试本身出错：{type(exc).__name__}: {exc}"
        print(f"{'OK ' if ok else 'XX '}{name}")
        print(f"     {msg}")
        if ok:
            passed += 1
        else:
            failed += 1

    print("=" * 70)
    print(f"通过 {passed} / {passed + failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
