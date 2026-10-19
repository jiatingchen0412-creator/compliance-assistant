"""异常路径测试：确认各种"坏情况"下界面都不会白屏。

为什么单独测这个：
主流程（run_eval.py）测的是"答得准不准"，这里测的是"坏了以后体不体面"。
小白用户遇到异常时，最怕的是一堆英文报错或者干脆空白页。

用法（在项目根目录执行）：
    .venv\\Scripts\\python.exe tests\\test_edge_cases.py

设计原则：
- 不依赖 pytest（保持零新依赖），自己带一个极简 runner
- 用 monkeypatch 模拟故障，**不去真的停掉 Ollama**（那会影响正在用的服务）
- 测试产生的会话记录在结束时清理掉
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config, db, llm  # noqa: E402

# 测试期间不写聊天记录，避免污染用户的历史
config.SAVE_HISTORY = False

from app import guard, logging_setup  # noqa: E402
from app.logging_setup import LOG_FILE  # noqa: E402
from app.retrieval import RetrievalEngine  # noqa: E402
from app.routers.chat import answer_question  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


@contextlib.contextmanager
def patch(obj, attr, value):
    """临时替换某个属性，退出时还原。"""
    old = getattr(obj, attr)
    setattr(obj, attr, value)
    try:
        yield
    finally:
        setattr(obj, attr, old)


def case(name: str):
    """把一个返回 (是否通过, 说明) 的函数注册成测试用例。"""
    def wrapper(fn):
        RESULTS.append((name, fn))
        return fn
    return wrapper


def check(condition: bool, ok_msg: str, fail_msg: str) -> tuple[bool, str]:
    return (True, ok_msg) if condition else (False, fail_msg)


# --------------------------------------------------------------------------
# 0. 本地 Ollama 客户端必须绕过系统代理
# --------------------------------------------------------------------------
@case("本地 Ollama 请求不被系统代理截走（trust_env=False）")
def test_llm_client_ignores_system_proxy():
    """回归测试，对应一次真实事故。

    httpx 默认（trust_env=True）会读取 Windows 系统代理设置，并把发往
    127.0.0.1:11434 的**本地**请求也交给代理。用户装了 Clash（系统代理
    127.0.0.1:7897）时，代理无法处理回环请求，直接返回 502 空响应，
    结果整个大模型功能静默降级成"只列条款、不做分析"。
    系统"代理例外"里即使写了 127.* 也没用——httpx 不读那份名单。

    复现手法：起一个本地假 Ollama，同时把 HTTP_PROXY 指向必然连不上的死端口。
    如果 trust_env 被打开，请求会被送去代理而失败；关掉了才能直连成功。
    """
    import http.server
    import os
    import threading

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = b'{"models":[]}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # 静音
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    proxy_vars = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                  "http_proxy", "https_proxy", "all_proxy")
    bypass_vars = ("NO_PROXY", "no_proxy")
    backup = {k: os.environ.get(k) for k in proxy_vars + bypass_vars}
    try:
        for k in proxy_vars:
            os.environ[k] = "http://127.0.0.1:9"   # 9 = discard，必然连不上
        # 必须同时清掉 no_proxy，否则 httpx 会因例外面绕过代理，
        # 那样即使 trust_env 开着测试也会误判成通过
        for k in bypass_vars:
            os.environ[k] = ""
        with patch(config, "OLLAMA_BASE_URL", f"http://127.0.0.1:{port}"):
            alive = llm.is_running()
    finally:
        for k, v in backup.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        server.shutdown()
        server.server_close()

    return check(
        alive,
        "系统代理指向死端口，本地请求仍直连成功（trust_env=False 生效）",
        "本地请求被送去代理而失败——app/llm.py 的 httpx.Client 掉了 trust_env=False",
    )


# --------------------------------------------------------------------------
# 1. 空问题
# --------------------------------------------------------------------------
@case("空问题被拒绝")
def test_empty_question():
    from fastapi import HTTPException

    try:
        answer_question("   ")
    except HTTPException as exc:
        return check(exc.status_code == 400, "返回 400 而不是崩溃", f"状态码异常 {exc.status_code}")
    except Exception as exc:
        return False, f"抛出了未预期的异常：{type(exc).__name__}: {exc}"
    return False, "空问题没有报错，应该拒绝"


# --------------------------------------------------------------------------
# 2. Ollama 不可用 → 降级为条款检索
# --------------------------------------------------------------------------
@case("Ollama 不可用时降级为条款检索")
def test_ollama_down():
    def boom(*_a, **_k):
        raise llm.LLMUnavailable("连不上本地 Ollama 服务", "请确认 Ollama 正在运行")

    with patch(llm, "chat", boom):
        r = answer_question("客户的手机号存在数据库里没有加密")

    ok = (
        r["mode"] == "retrieval_only"
        and len(r["citations"]) > 0
        and "无法调用本地大模型" in r["answer"]
        and r["found"] is True
    )
    return check(
        ok,
        f"降级正常，仍给出 {len(r['citations'])} 条引用",
        f"降级异常：mode={r['mode']} 引用={len(r['citations'])} found={r['found']}",
    )


# --------------------------------------------------------------------------
# 3. 模型返回非法 JSON
# --------------------------------------------------------------------------
@case("模型返回非法 JSON 时不崩溃")
def test_invalid_json():
    with patch(llm, "chat", lambda *a, **k: "抱歉，我这边出了点问题，请稍后再试。"):
        r = answer_question("客户的手机号存在数据库里没有加密")

    ok = r["answer"].strip() != "" and any("JSON" in n for n in r["notes"])
    return check(ok, "已按纯文本处理并记录提示", f"处理异常：{r['answer'][:60]} / notes={r['notes']}")


# --------------------------------------------------------------------------
# 3b. 模型输出被截断（JSON 坏了，但 answer 字段本身是完整的）
# --------------------------------------------------------------------------
# 真实复现：模型在 risk_tags 这种长数组里进入重复循环，输出被截断，
# 于是整个 JSON 不再合法。旧实现把 raw 原文直接当正文——
# 用户会看到一大坨 {"answer": "…", "citations": [ … 的花括号内容，
# 真正的答案被埋在中间，引用还全丢了（因为 mode 仍是 llm，不走降级补位）。
_BROKEN_JSON = (
    '{\n  "answer": "客户的手机号属于个人信息，明文存储有泄露风险，'
    '建议加密存储并限制访问权限。",\n'
    '  "citations": [\n    "8.1.4.19 数据保密性",\n    "8.1.4.26 个人信息保护"\n  ],\n'
    '  "risk_tags": [\n    "personal_info",\n    "encryption",\n'
    '    "personal_info",\n    "encryption",\n    "personal_info",\n    "encryption",\n'
    '    "personal_info",\n    "encryption",\n    "personal_info",\n    "encryption",\n'
    '    "personal_info"'  # ← 到此被截断：数组和对象都没闭合
)
_BROKEN_ANSWER = "客户的手机号属于个人信息，明文存储有泄露风险，建议加密存储并限制访问权限。"


@case("从坏掉的 JSON 里只抢救 answer 字段")
def test_salvage_answer_field():
    got = llm.salvage_answer_field(_BROKEN_JSON)
    mis_saved = [
        llm.salvage_answer_field(""),
        llm.salvage_answer_field("完全不是 JSON 的一段话"),
        llm.salvage_answer_field('{"citations": ["8.1.4.19"]}'),
    ]
    ok = got == _BROKEN_ANSWER and all(m is None for m in mis_saved)
    return check(ok, "只认 answer 字段，其余一概不救", f"抢救结果={got!r} 误救={mis_saved}")


@case("模型输出被截断时，不把原始 JSON 端给用户，也不用模型写的正文")
def test_truncated_json_salvage():
    with patch(llm, "chat", lambda *a, **k: _BROKEN_JSON):
        r = answer_question("客户的手机号存在数据库里没有加密")

    ans = r["answer"]
    # t28：模型不再写答案正文。截断的 JSON 里那半句 answer 属于"模型自由文本"，
    # 现在一律不采用——正文由服务端按条款拼装。所以这里反过来断言：
    # 救出来的那半句**不许**出现，但条款清单必须照常给全。
    ok = (
        "citations" not in ans          # 不得泄漏结构字段名
        and "{" not in ans              # 不得出现花括号
        and _BROKEN_ANSWER not in ans   # 模型自由文本不采用（t28 反转）
        and "8.1.4." in ans             # 条款编号照常出现
        and "不构成正式测评结论" in ans  # 服务端收尾照常
        and len(r["citations"]) > 0     # **引用必须兜底补上**
        and r["found"] is True
    )
    return check(
        ok,
        f"模型正文未采用，改为列出 {len(r['citations'])} 条条款",
        f"处理失败：answer={ans[:80]!r} 引用数={len(r['citations'])}",
    )


@case("截断输出里连 answer 都救不出来时，改为列出检索到的条款")
def test_truncated_json_unrecoverable():
    with patch(llm, "chat", lambda *a, **k: '{\n  "citations": ["8.1.4.19"],\n  "answer"'):
        r = answer_question("客户的手机号存在数据库里没有加密")

    ans = r["answer"]
    ok = (
        "{" not in ans
        and len(r["citations"]) > 0
        and any("不是合法 JSON" in n for n in r["notes"])
    )
    return check(ok, "已改为列出检索到的条款", f"兜底失败：answer={ans[:80]!r} notes={r['notes']}")


# --------------------------------------------------------------------------
# 4. 模型返回空内容
# --------------------------------------------------------------------------
@case("模型返回空内容时兜底")
def test_empty_model_output():
    with patch(llm, "chat", lambda *a, **k: ""):
        r = answer_question("客户的手机号存在数据库里没有加密")

    ok = r["answer"].strip() != "" and r["mode"] == "retrieval_only" and len(r["citations"]) > 0
    return check(ok, "已改为直接列出条款", f"兜底失败：mode={r['mode']} 回答长度={len(r['answer'])}")


# --------------------------------------------------------------------------
# 5. 知识库为空（检索不到任何条款）
# --------------------------------------------------------------------------
@case("知识库检索不到内容时拒答")
def test_no_retrieval_result():
    empty = {"results": [], "best_score": 0.0, "vector_used": False, "query_terms": []}
    with patch(RetrievalEngine, "search", lambda self, q, top_k=None: empty):
        r = answer_question("客户的手机号存在数据库里没有加密")

    ok = r["mode"] == "no_result" and r["found"] is False and "未找到" in r["answer"]
    return check(ok, "正确拒答并给出建议", f"拒答异常：mode={r['mode']} found={r['found']}")


# --------------------------------------------------------------------------
# 6. 超长问题
# --------------------------------------------------------------------------
@case("超长问题（2 万字）被截断后再送给模型")
def test_very_long_question():
    """这个用例要验证的是「截断」，不是「模型能不能回答」。

    两个坑：
    1. 必须把模型调用打桩，否则 Ollama 没开时会误报失败
       （测试不该依赖外部服务是否在线）。
    2. 超长问题不能由**重复字符串**拼成。第一次写成了
       "同一句话" * 1200，结果截断点之后的内容和开头完全相同，
       "截断点之后还在不在提示词里"这个判断永远为真，测不出问题。
       所以这里每段都带不同编号。
    """
    pieces = [f"第{i}点，我们公司使用一套自建的管理系统。" for i in range(900)]
    long_q = "".join(pieces)  # 约 1.9 万字，且每段都不同
    captured: dict = {}

    def fake_chat(messages, **kwargs):
        captured["prompt"] = messages[-1]["content"]
        return json.dumps(
            {
                "answer": "这是针对超长问题的回答。",
                "citations": [],
                "risk_tags": [],
                "need_clarification": False,
                "followup_questions": [],
                "found": True,
            },
            ensure_ascii=False,
        )

    try:
        with patch(llm, "chat", fake_chat):
            r = answer_question(long_q)
    except Exception as exc:
        return False, f"抛异常：{type(exc).__name__}: {exc}"

    truncated_note = any("已截取" in n for n in r["notes"])
    prompt = captured.get("prompt", "")
    head_kept = "第0点" in prompt
    tail_dropped = "第899点" not in prompt and "第500点" not in prompt

    ok = (
        r["answer"].strip() != ""
        and r["mode"] == "llm"
        and truncated_note
        and head_kept
        and tail_dropped
    )
    return check(
        ok,
        f"已截断（原 {len(long_q)} 字）：保留开头、丢弃结尾，提示词只带截断后的内容",
        f"模式={r['mode']} 截断提示={truncated_note} "
        f"保留开头={head_kept} 丢弃结尾={tail_dropped}",
    )


# --------------------------------------------------------------------------
# 7. 特殊字符 / 注入字符
# --------------------------------------------------------------------------
@case("特殊字符与 XSS 字符不导致崩溃")
def test_special_characters():
    nasty = (
        "客户的手机号没加密 <script>alert(1)</script> "
        "'; DROP TABLE clauses; -- \"quote\" \\backslash\\ %s %d {{7*7}} ${x}"
    )
    try:
        r = answer_question(nasty)
    except Exception as exc:
        return False, f"抛异常：{type(exc).__name__}: {exc}"

    # 确认数据库没被破坏
    cnt = db.query_one("SELECT COUNT(*) AS c FROM clauses")["c"]
    return check(
        r["answer"].strip() != "" and cnt > 0,
        f"正常处理，数据库完好（{cnt} 条条款）",
        f"数据库可能受损，条款数 {cnt}",
    )


# --------------------------------------------------------------------------
# 8. 提示词注入 / 模型编造条款号
# --------------------------------------------------------------------------
@case("模型编造条款号会被白名单剔除")
def test_fake_citation_filtered():
    fake = json.dumps(
        {
            "answer": "根据 9.9.9.9 和 1.2.3.4 的规定，您必须立即整改。",
            "citations": ["9.9.9.9", "1.2.3.4"],
            "risk_tags": ["access_control"],
            "need_clarification": False,
            "followup_questions": [],
            "found": True,
        },
        ensure_ascii=False,
    )
    with patch(llm, "chat", lambda *a, **k: fake):
        r = answer_question("我们公司十几个人共用一个管理员账号")

    ids = [c["clause_id"] for c in r["citations"]]
    ok = "9.9.9.9" not in ids and "1.2.3.4" not in ids
    noted = any("9.9.9.9" in n for n in r["notes"])
    return check(
        ok and noted,
        f"编造的编号已剔除并记入技术详情（保留下来的引用：{ids}）",
        f"编造的编号没有被正确处理：引用={ids} notes={r['notes']}",
    )


# --------------------------------------------------------------------------
# 9. 未捕获异常 → 有堆栈落盘 + 人话提示
# --------------------------------------------------------------------------
@case("未捕获异常有堆栈落盘且提示是人话")
def test_unhandled_exception_logged():
    import app.main as main_mod

    class FakeURL:
        path = "/api/测试路径"

    class FakeRequest:
        method = "POST"
        url = FakeURL()

    marker = "这是一条测试用的异常标记ABC123"
    before = LOG_FILE.stat().st_size if LOG_FILE.exists() else 0
    try:
        resp = asyncio.run(main_mod.unhandled(FakeRequest(), ValueError(marker)))
    except Exception as exc:
        return False, f"异常处理器本身抛异常了：{exc}"

    body = json.loads(resp.body.decode("utf-8"))
    text = LOG_FILE.read_text(encoding="utf-8", errors="ignore") if LOG_FILE.exists() else ""

    ok_log = marker in text and "未捕获异常" in text
    ok_msg = "服务内部错误" in body.get("message", "") and "app.log" in body.get("message", "")
    return check(
        ok_log and ok_msg,
        "堆栈已落盘，返回给用户的是中文提示",
        f"日志落盘={ok_log} 提示友好={ok_msg}",
    )


# --------------------------------------------------------------------------
# 10. 导出不存在的会话
# --------------------------------------------------------------------------
@case("导出不存在的会话返回 404 而不是崩溃")
def test_export_missing_session():
    from fastapi import HTTPException

    from app.routers.chat import export_session

    try:
        export_session("不存在的会话编号")
    except HTTPException as exc:
        return check(exc.status_code == 404, "返回 404", f"状态码异常 {exc.status_code}")
    except Exception as exc:
        return False, f"抛出了未预期的异常：{type(exc).__name__}: {exc}"
    return False, "没有报错，应该返回 404"


# --------------------------------------------------------------------------
# 11. 引用解析的鲁棒性
# --------------------------------------------------------------------------
@case("引用解析能处理各种写法")
def test_citation_parsing():
    clauses = [
        {"clause_id": "8.1.4.1", "title": "身份鉴别", "standard_name": "等保2.0",
         "chapter_path": [], "text": "x", "text_type": "summary", "risk_tags": []},
        {"clause_id": "PR.AA-01", "title": "身份管理", "standard_name": "NIST",
         "chapter_path": [], "text": "y", "text_type": "original", "risk_tags": []},
    ]
    cases = [
        (["8.1.4.1"], ["8.1.4.1"]),
        (["8.1.4.1 身份鉴别（标识与口令复杂度）"], ["8.1.4.1"]),
        (["条款1"], ["8.1.4.1"]),
        (["第2条"], ["PR.AA-01"]),
        (["PR.AA-01"], ["PR.AA-01"]),
        (["查无此条"], []),
    ]
    for raw, expect in cases:
        valid, _ = guard.validate_citations(raw, clauses)
        got = [c["clause_id"] for c in valid]
        if got != expect:
            return False, f"{raw} 期望 {expect}，实际 {got}"
    return True, "6 种写法全部解析正确"


# --------------------------------------------------------------------------
# 12. 正文位置序号翻译不误伤正常中文
# --------------------------------------------------------------------------
@case("位置序号翻译不误伤正常中文")
def test_positional_rewrite_safety():
    clauses = [
        {"clause_id": "8.1.4.1", "title": "身份鉴别", "chapter_path": [],
         "text": "", "text_type": "summary", "risk_tags": []},
    ]
    should_change = [
        ("根据【条款1】的要求", "8.1.4.1"),
        ("依据条款1整改", "8.1.4.1"),
    ]
    should_not = [
        "第一，要检查账号；第二，要改密码。",
        "第三次登录失败应锁定账户。",
        "这是第一次做合规自查。",
    ]
    for text, expect in should_change:
        out = guard.rewrite_positional_refs(text, clauses)
        if expect not in out:
            return False, f"{text} 应该被翻译成 {expect}，实际 {out}"
    for text in should_not:
        out = guard.rewrite_positional_refs(text, clauses)
        if out != text:
            return False, f"{text} 被误改了：{out}"
    return True, "该翻译的翻译了，正常中文没被误伤"


# --------------------------------------------------------------------------
# 13. 导入接口不接受带路径的文件名（路径穿越回归，对应 P0-1）
# --------------------------------------------------------------------------
class _FakeUpload:
    """只带文件名的假 UploadFile：路由只用得到 filename 和 file 两个属性。"""

    def __init__(self, filename: str):
        import io

        self.filename = filename
        self.file = io.BytesIO(b"PROOF-OF-PATH-TRAVERSAL\n")


class _NoIngest:
    """护栏：走到导入就说明校验没拦住，立刻炸掉，避免污染真实知识库。"""

    def import_standard_file(self, *args, **kwargs):
        raise AssertionError("文件名校验没拦住，已经走到 import_standard_file")

    def import_document(self, *args, **kwargs):
        raise AssertionError("文件名校验没拦住，已经走到 import_document")

    def rebuild_index(self):
        pass


def _probe_rejected_upload(raw_name: str, http_name: str | None = None) -> tuple[bool, str]:
    """拿一个恶意文件名跑导入，返回 (是否被 400 守住, 说明)。

    直调路由函数这一层必须覆盖：multipart 解析器有一条 IE6 兼容逻辑，会把
    `C:\\...` 先截成 basename（python-multipart 的 `parse_options_header` 里
    `val.split("\\\\")[-1]`），所以「Windows 绝对路径」这一写法在 HTTP 层根本
    到不了路由。其余能穿透解析器的写法再补一层真实 HTTP 断言。

    校验发生在建临时目录与备份之前，所以失败路径不应产生临时目录、备份或数据库改动。
    """
    import tempfile

    from fastapi import FastAPI, HTTPException
    from fastapi.testclient import TestClient

    import app.backup as backup_mod
    from app.routers import kb as kb_routes

    problems: list[str] = []
    created_dirs: list[Path] = []
    real_mkdtemp = tempfile.mkdtemp

    def spy_mkdtemp(*args, **kwargs):
        path = real_mkdtemp(*args, **kwargs)
        created_dirs.append(Path(path))
        return path

    # 若校验失效，这两个护栏保证测试会失败但不会往真实知识库里写东西
    fake_backup = Path(tempfile.gettempdir()) / "kb_audit_fake_backup.db"
    with (
        patch(kb_routes, "ingest", _NoIngest()),
        patch(backup_mod, "create_backup", lambda *a, **k: fake_backup),
        patch(tempfile, "mkdtemp", spy_mkdtemp),
    ):
        # 第一层：直接调路由函数，原始文件名原样送达
        try:
            asyncio.run(kb_routes.import_file(_FakeUpload(raw_name)))
        except HTTPException as exc:
            if exc.status_code != 400:
                problems.append(f"{raw_name!r} → {exc.status_code}，应为 400")
        except Exception as exc:
            problems.append(f"{raw_name!r} → 未预期异常 {type(exc).__name__}: {exc}")
        else:
            problems.append(f"{raw_name!r} → 没有被拒绝（返回 200）")

        if created_dirs:
            problems.append(f"校验未拦住，创建了临时目录：{created_dirs[0]}")

        # 第二层：真实 HTTP（multipart 解析后能原样到达路由的写法）
        if http_name is not None:
            probe = FastAPI()
            probe.include_router(kb_routes.router)
            client = TestClient(probe, raise_server_exceptions=False)
            resp = client.post(
                "/api/kb/import", files={"file": (http_name, b"PROOF", "text/plain")}
            )
            if resp.status_code != 400:
                problems.append(f"HTTP {http_name!r} → {resp.status_code}，应为 400")

    # 没有文件逃出临时目录
    escape_targets = [
        Path("C:/Users/A/AppData/absolute.txt"),
        Path.cwd() / "escaped.txt",
        ROOT / "escaped.txt",
        Path.cwd() / "c.txt",
        ROOT / "c.txt",
    ]
    for target in escape_targets:
        if target.exists():
            problems.append(f"出现了逃逸文件：{target}")

    if problems:
        return False, "；".join(problems)
    detail = f"{raw_name!r} 被 400 拒绝，未创建临时目录、无逃逸文件"
    if http_name is not None:
        detail += f"；HTTP 层 {http_name!r} 同样 400"
    return True, detail


@case(r"导入文件名 ..\..\..\escaped.txt → 400 且不写出临时目录")
def test_import_filename_parent_traversal():
    ok, detail = _probe_rejected_upload(
        r"..\..\..\escaped.txt", r"..\..\..\escaped.txt"
    )
    return check(ok, detail, detail)


@case(r"导入文件名 C:\Users\A\AppData\absolute.txt → 400 且不写盘")
def test_import_filename_windows_absolute():
    # 反斜杠写法经 multipart 解析会被截成 basename，所以这一条只能直调路由函数
    ok, detail = _probe_rejected_upload(r"C:\Users\A\AppData\absolute.txt")
    return check(ok, detail, detail)


@case("导入文件名 a/b/../../c.txt → 400 且不写出临时目录")
def test_import_filename_posix_subdir():
    ok, detail = _probe_rejected_upload("a/b/../../c.txt", "a/b/../../c.txt")
    return check(ok, detail, detail)


@case("正常的文件名仍按原后缀导入，落盘路径留在临时目录内")
def test_import_normal_filenames_still_work():
    import shutil as _shutil
    import tempfile

    from app.routers import kb as kb_routes

    expected = {
        "自查报告.pdf": ".pdf",
        "制度汇编.docx": ".docx",
        "note.md": ".md",
        "清单.txt": ".txt",
        "kb.json": ".json",
    }
    for raw, want in expected.items():
        name, suffix = kb_routes._safe_upload_name(raw)
        if suffix != want or not name.startswith("upload_") or not name.endswith(want):
            return False, f"{raw} → 文件名={name!r} 后缀={suffix!r}，期望后缀 {want}"
        if "报告" in name or "清单" in name:
            return False, f"{raw} → 落盘文件名仍带用户原文：{name}"

    tmp_dir = Path(tempfile.mkdtemp(prefix="kb_audit_"))
    try:
        name, _ = kb_routes._safe_upload_name("自查报告.pdf")
        path = kb_routes._safe_tmp_path(tmp_dir, name)
        inside = path.parent == tmp_dir.resolve()
    finally:
        _shutil.rmtree(tmp_dir, ignore_errors=True)

    return check(
        inside,
        "五种常见后缀均被接受；落盘文件名由服务端生成，路径留在临时目录内",
        f"落盘路径跑出了临时目录：{path}",
    )


# --------------------------------------------------------------------------
# 13b. 上传后缀白名单（对应 t12）
# --------------------------------------------------------------------------
@case("上传只接受文档类后缀：脚本 / 可执行 / 无后缀一律 400")
def test_import_suffix_whitelist():
    from fastapi import HTTPException

    from app.routers import kb as kb_routes

    rejected = ["x.sh", "x.exe", "x.bat", "x.ps1", "x"]
    for raw in rejected:
        try:
            name, _suffix = kb_routes._safe_upload_name(raw)
        except HTTPException as exc:
            if exc.status_code != 400:
                return False, f"{raw} 被拒但状态码是 {exc.status_code}"
            if "只接受" not in str(exc.detail):
                return False, f"{raw} 的提示不可读：{exc.detail}"
            continue
        return False, f"{raw} 没有被拒绝，落盘名是 {name!r}"

    accepted = {
        "a.PDF": ".pdf",
        "b.docx": ".docx",
        "c.txt": ".txt",
        "d.doc": ".doc",
        "e.md": ".md",
        "f.json": ".json",
    }
    for raw, want in accepted.items():
        name, suffix = kb_routes._safe_upload_name(raw)
        if suffix != want or not name.startswith("upload_") or not name.endswith(want):
            return False, f"{raw} → {name!r} 后缀 {suffix!r}，期望 {want}"

    return True, f"拒绝 {rejected}；接受 {list(accepted)}（大写后缀归一到小写）"


# --------------------------------------------------------------------------
# 14. CORS 只放行同源（对应 P0-2）
# --------------------------------------------------------------------------
@case("CORS 只放行本机同源，不再允许任意网页跨源读取")
def test_cors_not_wildcard():
    from fastapi.testclient import TestClient

    import app.main as main_mod

    allowed = f"http://127.0.0.1:{config.APP_PORT}"
    client = TestClient(main_mod.app)
    preflight = {"Access-Control-Request-Method": "GET"}

    evil = client.options(
        "/api/sessions", headers={**preflight, "Origin": "https://evil.example"}
    )
    same = client.options("/api/sessions", headers={**preflight, "Origin": allowed})

    evil_acao = evil.headers.get("access-control-allow-origin")
    same_acao = same.headers.get("access-control-allow-origin")

    if evil_acao is not None:
        return False, f"恶意来源仍拿到 ACAO={evil_acao!r}"
    if same_acao != allowed:
        return False, f"同源预检没有放行（ACAO={same_acao!r}）"
    return True, f"恶意来源无 ACAO；同源 {allowed} 正常放行"


@case("真实库一个字节都没变（测试不污染用户数据）")
def test_real_db_untouched() -> tuple[bool, str]:
    """F4 守卫。放在文件最后，跑完前面所有用例之后才比哈希。"""
    real = Path(str(_REAL_DB.get("path") or ""))
    before = str(_REAL_DB.get("hash") or "")
    if not str(real):
        return False, "守卫未初始化：main() 没走 _use_temp_db()"
    after = _sha256(real) if real.exists() else ""
    return check(
        after == before,
        f"哈希未变（{after[:6] or '空库'}…）",
        f"哈希变了：{before[:8] or '空'} → {after[:8] or '空'}",
    )


# --------------------------------------------------------------------------
# runner
# --------------------------------------------------------------------------
def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# 真实库守卫：main() 启动时把路径与哈希记在这里，最后一条 @case 负责比对。
# 为什么做成用例而不是 runner 里额外加一次断言：那样「通过 23 / 23」会比本文件
# 的 @case 条数多 1，文档口径（check_syntax 数 @case）与运行口径从此对不上，
# 每加一个守卫就要在两套数字之间手工换算——正是这一轮在消灭的那类漂移。
_REAL_DB: dict[str, object] = {}


def _use_temp_db() -> tuple[Path, Path, str]:
    """把 DB 指到真实库的临时副本，返回 (临时目录, 真实库路径, 跑之前的哈希)。

    为什么非改不可：本套件会写会话与消息，旧版直接跑在 data/app.db 上，
    实测把用户真实库的哈希从 4C3A32… 改成 8E83E4…。
    一边修「假绿」一边污染用户数据，说不过去。
    """
    real = Path(config.DB_PATH)
    real_hash = _sha256(real) if real.exists() else ""
    tmp_dir = Path(tempfile.mkdtemp(prefix="edge_cases_db_"))
    tmp_db = tmp_dir / "app.db"
    for suffix in ("", "-wal", "-shm"):
        src = Path(str(real) + suffix)
        if src.exists():
            shutil.copy2(src, Path(str(tmp_db) + suffix))
    config.DB_PATH = tmp_db
    _REAL_DB.update({"path": real, "hash": real_hash})
    return tmp_dir, real, real_hash


def main() -> int:
    tmp_dir, real_db, real_hash_before = _use_temp_db()
    db.init_db()
    logging_setup.setup_logging()

    # 记录测试前的会话，结束后清理测试期间产生的
    before = {r["id"] for r in db.query("SELECT id FROM sessions")}

    passed = failed = 0
    print("=" * 78)
    print("异常路径测试 · 确认各种坏情况下不白屏")
    print("=" * 78)

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

    # 清理测试产生的会话
    with db.get_conn() as conn:
        rows = conn.execute("SELECT id FROM sessions").fetchall()
        for row in rows:
            if row["id"] not in before:
                conn.execute("DELETE FROM messages WHERE session_id = ?", (row["id"],))
                conn.execute("DELETE FROM sessions WHERE id = ?", (row["id"],))

    # 真实库的完整性由最后一条 @case（test_real_db_untouched）负责断言，
    # 不在这里另算一次，否则运行口径会比 @case 条数多 1。
    shutil.rmtree(tmp_dir, ignore_errors=True)
    print("=" * 78)
    print(f"通过 {passed} / {passed + failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
