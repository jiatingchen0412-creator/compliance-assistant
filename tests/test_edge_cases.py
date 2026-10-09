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
import json
import sys
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


@case("模型输出被截断时，不把原始 JSON 端给用户，且引用不丢")
def test_truncated_json_salvage():
    with patch(llm, "chat", lambda *a, **k: _BROKEN_JSON):
        r = answer_question("客户的手机号存在数据库里没有加密")

    ans = r["answer"]
    ok = (
        "citations" not in ans          # 不得泄漏结构字段名
        and "{" not in ans              # 不得出现花括号
        and _BROKEN_ANSWER in ans       # 正文要被救回来
        and len(r["citations"]) > 0     # **引用必须兜底补上**
        and r["found"] is True
    )
    return check(
        ok,
        f"已抢救出正文并补上 {len(r['citations'])} 条引用",
        f"抢救失败：answer={ans[:80]!r} 引用数={len(r['citations'])}",
    )


@case("截断输出里连 answer 都救不出来时，改为列出检索到的条款")
def test_truncated_json_unrecoverable():
    with patch(llm, "chat", lambda *a, **k: '{\n  "citations": ["8.1.4.19"],\n  "answer"'):
        r = answer_question("客户的手机号存在数据库里没有加密")

    ans = r["answer"]
    ok = (
        "{" not in ans
        and len(r["citations"]) > 0
        and any("未能抢救出正文" in n for n in r["notes"])
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
# runner
# --------------------------------------------------------------------------
def main() -> int:
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

    print("=" * 78)
    print(f"通过 {passed} / {passed + failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
