"""流式输出测试（P3-1）。

测两件事：
1. `extract_partial_answer` —— 从不完整的 JSON 文本里增量抠出 answer 字段。
   这是真流式的核心，出错的表现是前端看到一堆反斜杠或者半截转义。
2. `chat_stream_json` —— 增量回调 + 返回完整文本；回调失败时不能影响生成。

不需要 Ollama，全部用纯函数和 monkeypatch 测。

用法（在项目根目录执行）：
    .venv\\Scripts\\python.exe tests\\test_streaming.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import llm  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def case(name: str):
    def wrapper(fn):
        RESULTS.append((name, fn))
        return fn
    return wrapper


# --------------------------------------------------------------------------
# extract_partial_answer
# --------------------------------------------------------------------------
@case("还没出现 answer 键时返回 None")
def test_no_answer_key():
    for buf in ("", "{", '{"citations":["8.1.4.1"]', '{"risk_tags":['):
        got = llm.extract_partial_answer(buf)
        if got is not None:
            return False, f"{buf!r} 期望 None，实际 {got!r}"
    return True, "4 种无 answer 的片段都返回 None"


@case("answer 刚开始时返回空字符串")
def test_just_started():
    got = llm.extract_partial_answer('{"answer":"')
    return (got == "", f"返回 {got!r}") if got == "" else (False, f"期望空字符串，实际 {got!r}")


@case("逐步到达的分片能拼出完整内容")
def test_incremental():
    full = '{"answer":"客户手机号未加密存储存在风险","citations":[]}'
    expected = "客户手机号未加密存储存在风险"
    for cut in range(10, len(full)):
        got = llm.extract_partial_answer(full[:cut])
        if got is None:
            continue  # 还没到 answer
        if not expected.startswith(got):
            return False, f"截到 {cut} 时得到 {got!r}，不是期望内容的前缀"
    final = llm.extract_partial_answer(full)
    return (
        (final == expected, f"最终得到 {final!r}")
        if final == expected
        else (False, f"最终期望 {expected!r}，实际 {final!r}")
    )


@case("转义序列处理正确（\\n \\t \\\" \\\\）")
def test_escapes():
    cases = [
        ('{"answer":"第一行\\n第二行', "第一行\n第二行"),
        ('{"answer":"制表\\t符', "制表\t符"),
        ('{"answer":"他说\\"你好\\""', '他说"你好"'),
        ('{"answer":"反斜杠\\\\结束', "反斜杠\\结束"),
    ]
    for buf, expect in cases:
        got = llm.extract_partial_answer(buf)
        if got != expect:
            return False, f"{buf!r} 期望 {expect!r}，实际 {got!r}"
    return True, f"{len(cases)} 种转义全部正确"


@case("转义被切断时不输出半个字符")
def test_split_escape():
    # 结尾刚好是一个反斜杠，下一个分片还没到
    got = llm.extract_partial_answer('{"answer":"abc\\')
    if got != "abc":
        return False, f"期望 'abc'（丢掉未完成的转义），实际 {got!r}"
    # 结尾是 \u 但 4 位十六进制还没收完
    got2 = llm.extract_partial_answer('{"answer":"abc\\u4e')
    if got2 != "abc":
        return False, f"期望 'abc'，实际 {got2!r}"
    # 收完了应该能解出中文
    got3 = llm.extract_partial_answer('{"answer":"abc\\u4e2d')
    if got3 != "abc中":
        return False, f"期望 'abc中'，实际 {got3!r}"
    return True, "未完成的转义被正确丢弃，收完后能解出中文"


@case("answer 结束后不再多取")
def test_stops_at_quote():
    buf = '{"answer":"只取这段","citations":["8.1.4.1"],"found":true}'
    got = llm.extract_partial_answer(buf)
    return (
        (got == "只取这段", f"得到 {got!r}")
        if got == "只取这段"
        else (False, f"期望 '只取这段'，实际 {got!r}")
    )


# --------------------------------------------------------------------------
# chat_stream_json
# --------------------------------------------------------------------------
@case("chat_stream_json 增量回调并返回完整文本")
def test_stream_json():
    chunks = [
        '{"answer":"客户',
        '手机号未加密',
        '存储","citations":["A02:2021"],"found":true}',
    ]

    def fake_stream(_messages, json_schema=None):
        for c in chunks:
            yield c

    original = llm.chat_stream
    llm.chat_stream = fake_stream
    try:
        received: list[str] = []
        raw = llm.chat_stream_json([{"role": "user", "content": "x"}], {}, received.append)
    finally:
        llm.chat_stream = original

    joined = "".join(received)
    ok = raw == "".join(chunks) and joined == "客户手机号未加密存储"
    return (
        (ok, f"回调累计 {joined!r}，完整文本 {len(raw)} 字符")
        if ok
        else (False, f"回调={joined!r} 完整文本={raw!r}")
    )


@case("回调抛异常时不影响生成结果")
def test_callback_error():
    def fake_stream(_messages, json_schema=None):
        yield '{"answer":"内容","found":true}'

    def boom(_piece):
        raise RuntimeError("模拟回调故障")

    original = llm.chat_stream
    llm.chat_stream = fake_stream
    try:
        raw = llm.chat_stream_json([{"role": "user", "content": "x"}], {}, boom)
    finally:
        llm.chat_stream = original

    return (
        (raw == '{"answer":"内容","found":true}', "生成结果完好，异常被吞掉并记日志")
        if raw == '{"answer":"内容","found":true}'
        else (False, f"生成结果被影响：{raw!r}")
    )


@case("模型先写 citations 时不会误取内容")
def test_wrong_order():
    def fake_stream(_messages, json_schema=None):
        yield '{"citations":["8.1.4.1","A02:2021"],'
        yield '"answer":"这才是正文","found":true}'

    original = llm.chat_stream
    llm.chat_stream = fake_stream
    try:
        received: list[str] = []
        llm.chat_stream_json([{"role": "user", "content": "x"}], {}, received.append)
    finally:
        llm.chat_stream = original

    joined = "".join(received)
    return (
        (joined == "这才是正文", f"正确取到正文：{joined!r}")
        if joined == "这才是正文"
        else (False, f"取错了内容：{joined!r}")
    )


@case("模型不吐增量时自动退回打字机回放")
def test_typewriter_fallback():
    events, text = _run_stream(fake_answer=lambda **kw: _canned("这是完整回答内容"))
    deltas = [e for e in events if e[0] == "delta"]
    return (
        (text == "这是完整回答内容", f"退回打字机，共推送 {len(deltas)} 段")
        if text == "这是完整回答内容"
        else (False, f"回放内容不对：{text!r}")
    )


@case("校验改动了正文时发送 replace 事件")
def test_replace_event():
    def answer(**kw):
        if kw.get("on_delta"):
            kw["on_delta"]("原始文本")
        return _canned("校验后的完整文本")

    events, text = _run_stream(fake_answer=answer)
    kinds = [e[0] for e in events]
    return (
        ("replace" in kinds and text == "校验后的完整文本",
         f"事件序列包含 replace，最终文本正确")
        if "replace" in kinds and text == "校验后的完整文本"
        else (False, f"事件={kinds} 最终文本={text!r}")
    )


@case("校验只是在流式文本后面追加内容时补增量而不是整段替换")
def test_append_suffix():
    def answer(**kw):
        if kw.get("on_delta"):
            kw["on_delta"]("正文主体")
        return _canned("正文主体\n\n本回答仅用于辅助自查，不构成正式测评结论。")

    events, text = _run_stream(fake_answer=answer)
    kinds = [e[0] for e in events]
    ok = "replace" not in kinds and text.startswith("正文主体") and "不构成正式测评结论" in text
    return (
        (ok, f"只补了增量，没有整段替换（事件 {kinds}）")
        if ok
        else (False, f"事件={kinds} 文本={text!r}")
    )


def _canned(answer: str) -> dict:
    return {
        "session_id": "test-session",
        "answer": answer,
        "citations": [],
        "risks": [],
        "need_clarification": False,
        "followup_questions": [],
        "found": True,
        "mode": "llm",
        "notes": [],
    }


def _run_stream(fake_answer) -> tuple[list[tuple[str, str]], str]:
    """跑一遍 _stream 生成器，返回 (事件列表, 最终文本)。

    全程用假的 answer_question 和假的会话函数，不碰数据库也不调大模型。
    """
    import json as _json

    from app.routers import chat as chat_mod

    original_answer = chat_mod.answer_question
    original_session = chat_mod._ensure_session

    def fake(q, sid=None, on_delta=None, on_stage=None):
        return fake_answer(q=q, sid=sid, on_delta=on_delta, on_stage=on_stage)

    chat_mod.answer_question = fake
    chat_mod._ensure_session = lambda session_id, question: "test-session"
    try:
        raw_events = list(chat_mod._stream("测试问题", None))
    finally:
        chat_mod.answer_question = original_answer
        chat_mod._ensure_session = original_session

    events: list[tuple[str, str]] = []
    text = ""
    for raw in raw_events:
        name, data = "message", ""
        for line in raw.split("\n"):
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                data = line[5:].strip()
        events.append((name, data))
        if name == "delta":
            text += _json.loads(data)["text"]
        elif name == "replace":
            text = _json.loads(data)["text"]
    return events, text


def main() -> int:
    print("=" * 70)
    print("流式输出测试 · 不依赖 Ollama，纯函数验证")
    print("=" * 70)

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
