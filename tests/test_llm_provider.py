"""大模型后端可切换（本地 Ollama / 云端 OpenAI 兼容接口）的回归测试。

为什么需要这个套件：
项目原本只支持本地 Ollama，隐私承诺就是靠这一点撑住的。后来加了「不满意本地
7B 就用自己的 API 接更强的模型」这条需求——它改变的是**隐私边界**，属于最容易
悄悄退化的地方：
  1. 云端没配好时不能假装能用（否则用户看到的报错会是一串英文栈）
  2. 云端配好时必须让页面上看得见（否则用户以为数据还在本机）
  3. API Key 不能出现在任何返回值、异常文本、页面里
  4. 结构化输出要能按服务商能力降级（json_object → json_schema → 纯提示词）
  5. 本地后端的 trust_env=False 不能被云端开关顺手改坏（会让本地请求走代理返回 502）

这个套件**完全不联网**：所有 HTTP 交互都用一个假客户端顶替。
"""
from __future__ import annotations

import contextlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config, llm  # noqa: E402

RESULTS: list[tuple[str, object]] = []

SECRET = "sk-test-SHOULD-NEVER-LEAK-1234567890"


def case(name: str):
    def wrapper(fn):
        RESULTS.append((name, fn))
        return fn

    return wrapper


def ok(condition: bool, good: str, bad: str) -> tuple[bool, str]:
    return (True, good) if condition else (False, bad)


# --------------------------------------------------------------------------
# 假 httpx 客户端：只记录参数、返回预设响应，绝不联网
# --------------------------------------------------------------------------
class _FakeResponse:
    def __init__(self, status_code: int = 200, payload: dict | None = None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text or ""

    def json(self) -> dict:
        return self._payload


class _FakeStream:
    def __init__(self, lines: list[str], status_code: int = 200) -> None:
        self._lines = lines
        self.status_code = status_code
        self.text = ""

    def __enter__(self) -> "_FakeStream":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self) -> None:
        return None

    def iter_lines(self):
        for line in self._lines:
            yield line


class _FakeClient:
    """记录每一次请求，供用例断言请求体形状。"""

    def __init__(self, response: _FakeResponse, stream_lines: list[str] | None = None) -> None:
        self.response = response
        self.stream_lines = stream_lines or []
        self.calls: list[dict] = []

    def __enter__(self) -> "_FakeClient":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def post(self, path: str, json: dict | None = None, **kw: object) -> _FakeResponse:
        self.calls.append({"path": path, "json": json or {}, **kw})
        return self.response

    def stream(self, method: str, path: str, json: dict | None = None, **kw: object):
        self.calls.append({"path": path, "json": json or {}, "method": method})
        return _FakeStream(self.stream_lines, self.response.status_code)


@contextlib.contextmanager
def cloud_backend(*, key: str = SECRET, base: str = "https://api.example.com/v1",
                  model: str = "big-model", json_mode: str = "json_object"):
    """临时把配置切成云端后端，退出时原样还原。"""
    saved = {
        "LLM_BACKEND": config.LLM_BACKEND,
        "LLM_API_KEY": config.LLM_API_KEY,
        "LLM_API_BASE": config.LLM_API_BASE,
        "LLM_API_MODEL": config.LLM_API_MODEL,
        "LLM_API_JSON_MODE": config.LLM_API_JSON_MODE,
    }
    config.LLM_BACKEND = "openai"
    config.LLM_API_KEY = key
    config.LLM_API_BASE = base
    config.LLM_API_MODEL = model
    config.LLM_API_JSON_MODE = json_mode
    try:
        yield
    finally:
        for name, value in saved.items():
            setattr(config, name, value)


@contextlib.contextmanager
def fake_client(client: _FakeClient):
    saved = llm._cloud_client
    llm._cloud_client = lambda timeout=None: client  # type: ignore[assignment]
    try:
        yield client
    finally:
        llm._cloud_client = saved  # type: ignore[assignment]


MESSAGES = [{"role": "system", "content": "只输出 JSON"}, {"role": "user", "content": "问点什么"}]


# --------------------------------------------------------------------------
# 用例
# --------------------------------------------------------------------------
@case("默认后端是本地 Ollama（隐私承诺是默认值，不是选项）")
def test_default_is_local():
    if config.LLM_BACKEND != "ollama":
        return False, f"默认后端是 {config.LLM_BACKEND!r}，应当是 ollama"
    if llm.is_cloud():
        return False, "is_cloud() 在默认配置下返回了 True"
    if llm.active_model() != config.LLM_MODEL:
        return False, "默认后端下的模型名不是 LLM_MODEL"
    return True, f"默认走本地 Ollama，模型 {config.LLM_MODEL}"


@case("云端没填密钥时说清楚缺什么，不能假装能用")
def test_cloud_not_configured():
    with cloud_backend(key=""):
        if llm.cloud_configured():
            return False, "密钥为空时 cloud_configured() 返回了 True"
        if llm.is_running():
            return False, "密钥为空时 is_running() 返回了 True（会被当成可用）"
        h = llm.health()
        if h["status"] != "cloud_not_configured":
            return False, f"状态是 {h['status']}，应当是 cloud_not_configured"
        if "LLM_API_KEY" not in h["hint"]:
            return False, f"提示里没说缺 LLM_API_KEY：{h['hint']!r}"
        if h["privacy_notice"]:
            return False, "没配好却给出了云端隐私提示（等于宣称正在用云端）"
    return True, "缺密钥时状态为 cloud_not_configured，并指名缺 LLM_API_KEY"


@case("云端配好后状态为 ok，并给出隐私提示")
def test_cloud_configured_health():
    with cloud_backend():
        if not llm.cloud_configured() or not llm.is_running():
            return False, "配好密钥后仍被判为不可用"
        if not llm.try_start_server(wait_seconds=1):
            return False, "云端后端没有服务要拉，try_start_server 应直接返回 True"
        h = llm.health()
        if h["status"] != "ok" or not h["cloud"]:
            return False, f"健康状态不对：{h['status']} cloud={h['cloud']}"
        notice = h["privacy_notice"]
        if "云端" not in notice or "服务商" not in notice or "发送" not in notice:
            return False, f"隐私提示没说清数据会发出去：{notice!r}"
        if "ollama" not in notice:
            return False, "隐私提示没给「改回本地」的办法"
    return True, "云端就绪时给出「数据会发到服务商 + 可改回 ollama」的提示"


@case("API Key 不出现在任何返回值里")
def test_key_never_exposed():
    with cloud_backend():
        d = config.as_dict()
        blob = repr(d)
        if SECRET in blob:
            return False, "as_dict() 里出现了密钥原文"
        if d.get("llm_api_key_set") is not True:
            return False, "as_dict() 没告诉前端密钥已配置"
        h = repr(llm.health())
        if SECRET in h:
            return False, "health() 里出现了密钥原文"
        # 401 的异常文本同样不能带密钥
        resp = _FakeResponse(401, text="invalid api key")
        client = _FakeClient(resp)
        with fake_client(client):
            try:
                llm.chat(MESSAGES)
            except llm.LLMUnavailable as exc:
                text = f"{exc} {exc.hint}"
                if SECRET in text:
                    return False, "401 异常文本里出现了密钥原文"
            else:
                return False, "401 没有抛 LLMUnavailable"
    return True, "as_dict / health / 异常文本都不含密钥，只报「已配置」"


@case("请求体形状：/chat/completions、stream=false、结构化输出按配置降级")
def test_request_shape():
    resp = _FakeResponse(200, {"choices": [{"message": {"content": "{}"}}]})
    client = _FakeClient(resp)
    with cloud_backend(json_mode="json_object"), fake_client(client):
        llm.chat(MESSAGES, json_schema={"type": "object", "properties": {}})
    if not client.calls:
        return False, "没有发出任何请求"
    call = client.calls[0]
    if call["path"] != "/chat/completions":
        return False, f"请求路径是 {call['path']}，应当是 /chat/completions"
    body = call["json"]
    for field in ("model", "messages", "stream", "temperature", "max_tokens"):
        if field not in body:
            return False, f"请求体缺字段 {field}"
    if body["stream"] is not False:
        return False, "非流式调用里 stream 不是 False"
    if body["model"] != "big-model":
        return False, f"请求体里的模型是 {body['model']}"
    if body.get("response_format") != {"type": "json_object"}:
        return False, f"json_object 模式下 response_format 是 {body.get('response_format')}"
    return True, "请求体字段齐全，json_object 模式下带 response_format"


@case("json_schema 模式把 schema 原样带上；none 模式不加约束")
def test_json_mode_variants():
    resp = _FakeResponse(200, {"choices": [{"message": {"content": "{}"}}]})
    schema = {"type": "object", "properties": {"found": {"type": "boolean"}}}
    c1 = _FakeClient(resp)
    with cloud_backend(json_mode="json_schema"), fake_client(c1):
        llm.chat(MESSAGES, json_schema=schema)
    rf = c1.calls[0]["json"].get("response_format") or {}
    if rf.get("type") != "json_schema":
        return False, f"json_schema 模式下 response_format 是 {rf}"
    inner = rf.get("json_schema") or {}
    if inner.get("schema") != schema:
        return False, "json_schema 模式下 schema 没有原样带上"
    c2 = _FakeClient(resp)
    with cloud_backend(json_mode="none"), fake_client(c2):
        llm.chat(MESSAGES, json_schema=schema)
    if "response_format" in c2.calls[0]["json"]:
        return False, "none 模式下不该出现 response_format"
    return True, "三种 json 模式各按配置走，none 模式完全不加约束"


@case("非流式返回取 choices[0].message.content")
def test_chat_content():
    resp = _FakeResponse(200, {"choices": [{"message": {"content": '{"found": true}'}}]})
    with cloud_backend(), fake_client(_FakeClient(resp)):
        got = llm.chat(MESSAGES)
    return ok(got == '{"found": true}', "取到模型正文", f"取到的内容不对：{got!r}")


@case("流式解析：data: 分片逐段 yield，[DONE] 结束，坏行跳过")
def test_stream_parsing():
    lines = [
        'data: {"choices":[{"delta":{"content":"{\\"found\\":"}}]}',
        "",
        'data: {"choices":[{"delta":{"content":"true}"}}]}',
        "data: 这一行不是 JSON",      # 坏行必须跳过而不是崩
        'data: {"choices":[],"usage":{"total_tokens":3}}',  # 空 choices 不产出
        "data: [DONE]",
        'data: {"choices":[{"delta":{"content":"DONE 之后的内容"}}]}',
    ]
    client = _FakeClient(_FakeResponse(200), stream_lines=lines)
    with cloud_backend(), fake_client(client):
        pieces = list(llm.chat_stream(MESSAGES, json_schema={"type": "object"}))
    joined = "".join(pieces)
    if joined != '{"found":true}':
        return False, f"拼起来是 {joined!r}"
    if any("DONE 之后" in p for p in pieces):
        return False, "[DONE] 之后的内容不该被采用"
    if client.calls[0]["json"]["stream"] is not True:
        return False, "流式请求里 stream 不是 True"
    return True, "分片按序拼接、坏行跳过、[DONE] 后不再读取"


@case("错误码翻译成能动手解决的话（401 / 429 / 404）")
def test_error_mapping():
    cases = {
        401: "LLM_API_KEY",
        429: "余额",
        404: "LLM_API_MODEL",
    }
    for code, expect in cases.items():
        client = _FakeClient(_FakeResponse(code, text="boom"))
        with cloud_backend(), fake_client(client):
            try:
                llm.chat(MESSAGES)
            except llm.LLMUnavailable as exc:
                blob = f"{exc} {exc.hint}"
                if expect not in blob:
                    return False, f"{code} 的错误提示里没提到 {expect}：{blob!r}"
            else:
                return False, f"{code} 没有抛 LLMUnavailable"
    return True, "401 指 API Key、429 指余额/限流、404 指模型名"


@case("本地后端的客户端仍不读系统代理（trust_env=False）")
def test_local_client_trust_env():
    c = llm._client(timeout=1)
    try:
        if c.trust_env:
            return False, "本地 Ollama 客户端开始读系统代理了，会返回 502"
        if not llm.is_cloud():
            pass
    finally:
        c.close()
    with cloud_backend():
        cc = llm._cloud_client(timeout=1)
        try:
            if cc.trust_env is not config.LLM_API_TRUST_ENV:
                return False, "云端客户端没按 LLM_API_TRUST_ENV 设置 trust_env"
            auth = cc.headers.get("Authorization", "")
            if not auth.startswith("Bearer "):
                return False, f"云端客户端缺少 Authorization 头：{auth!r}"
        finally:
            cc.close()
    return True, "本地 trust_env=False；云端按配置，且带上 Authorization"


@case("切回本地后一切复原（不会残留云端状态）")
def test_switch_back():
    with cloud_backend():
        if not llm.is_cloud():
            return False, "云端模式下 is_cloud() 是 False"
    if llm.is_cloud():
        return False, "离开云端配置块后 is_cloud() 仍为 True"
    if llm.active_model() != config.LLM_MODEL:
        return False, "切回本地后模型名没复原"
    h = llm.health()
    if h["cloud"] or h["privacy_notice"]:
        return False, "切回本地后仍然显示云端提示"
    return True, "切回本地后后端、模型名、隐私提示全部复原"


def main() -> int:
    print("=" * 76)
    print("大模型后端可切换（本地 Ollama / 云端 OpenAI 兼容）· 完全不联网")
    print("=" * 76)

    passed = failed = 0
    for name, fn in RESULTS:
        try:
            good, msg = fn()
        except Exception as exc:  # 测试本身出错也算失败，不能静默
            good, msg = False, f"测试本身出错：{type(exc).__name__}: {exc}"
        print(f"{'OK ' if good else 'XX '} {name}")
        print(f"      {msg}")
        if good:
            passed += 1
        else:
            failed += 1

    print("=" * 76)
    print(f"通过 {passed} / {passed + failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
