"""大模型调用（本地 Ollama）。

为什么用 Ollama：
- 完全不联网，数据不出本机，合规场景更放心。
- 通过 /api/chat 调用，支持流式输出和 JSON Schema 结构化输出。

三个必须处理的现实问题：
1. Ollama 没启动 → 给出人话提示，自动尝试拉起。
2. 模型没下载 → 提示该跑哪条命令，而不是抛一堆英文栈。
3. 小模型 JSON 输出不稳定 → 解析失败时做容错修复，实在不行降级为纯检索回答。
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import time
from typing import Any, Callable, Iterator

import httpx

from . import config

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Ollama 状态
# --------------------------------------------------------------------------


class LLMUnavailable(RuntimeError):
    """大模型不可用（没启动 / 没模型 / 超时）。"""

    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.hint = hint


def backend() -> str:
    """当前用哪个后端：`ollama`（本地，默认）或 `openai`（OpenAI 兼容的云端接口）。"""
    return config.LLM_BACKEND


def is_cloud() -> bool:
    return backend() == "openai"


def active_model() -> str:
    """当前后端实际用的模型名（日志、健康状态、页面上都显示这个）。"""
    return config.LLM_API_MODEL if is_cloud() else config.LLM_MODEL


def cloud_configured() -> bool:
    """云端后端配好了没有：地址与密钥都得有，缺一个就当没配好。"""
    return bool(config.LLM_API_BASE and config.LLM_API_KEY)


def _cloud_client(timeout: int | None = None) -> httpx.Client:
    """OpenAI 兼容接口的客户端。

    trust_env 默认 false（与本地 Ollama 同样的理由：Windows 上系统代理会把
    请求劫走并返回 502）。需要走代理才能出网的用户可以把 LLM_API_TRUST_ENV
    设成 true —— 这一项在 .env.example 里有说明。
    """
    return httpx.Client(
        base_url=config.LLM_API_BASE,
        timeout=httpx.Timeout(timeout or config.LLM_TIMEOUT, connect=10.0),
        trust_env=config.LLM_API_TRUST_ENV,
        headers={
            "Authorization": f"Bearer {config.LLM_API_KEY}",
            "Content-Type": "application/json",
        },
    )


def _client(timeout: int | None = None) -> httpx.Client:
    """本地 Ollama 客户端。

    trust_env=False 是**必须**的，不是可选优化：
    httpx 默认会读取 Windows 系统代理（如 Clash 的 127.0.0.1:7897）并把本地
    127.0.0.1:11434 的请求也发到代理上，代理无法处理内网回环请求，直接返回
    502（空响应体）。用户在系统「代理例外」里写了 127.* 也没用，httpx 不读
    那份例外名单。实测：同一台机器上 urllib 走 200、httpx 默认走 502，
    加 trust_env=False 后恢复正常。
    """
    return httpx.Client(
        base_url=config.OLLAMA_BASE_URL,
        timeout=httpx.Timeout(timeout or config.LLM_TIMEOUT, connect=5.0),
        trust_env=False,
    )


def is_running() -> bool:
    """当前后端能不能用。

    云端后端不发探测请求（启动时多一次外网往返既慢又可能被限流）：
    配了密钥就算"可用"，真正的错误在第一次调用时按状态码报出来。
    """
    if is_cloud():
        return cloud_configured()
    try:
        with _client(timeout=4) as c:
            return c.get("/api/tags").status_code == 200
    except Exception:
        return False


def list_models() -> list[str]:
    if is_cloud():
        # 云端模型清单不给用户看（不同服务商命名规则差太多），只报当前配置的那个
        return [config.LLM_API_MODEL] if cloud_configured() else []
    try:
        with _client(timeout=8) as c:
            data = c.get("/api/tags").json()
        return [m.get("name", "") for m in data.get("models", [])]
    except Exception:
        return []


def _ollama_exe() -> str | None:
    found = shutil.which("ollama")
    if found:
        return found
    candidate = os.path.expandvars(r"%LOCALAPPDATA%\Programs\Ollama\ollama.exe")
    return candidate if os.path.exists(candidate) else None


def try_start_server(wait_seconds: int = 25) -> bool:
    """尝试后台拉起 Ollama 服务（用户没手动开的时候兜底）。

    云端后端没有"服务"要拉，已配置就直接返回 True。
    """
    if is_cloud():
        return cloud_configured()
    if is_running():
        return True
    exe = _ollama_exe()
    if not exe:
        return False
    try:
        creation = 0
        if os.name == "nt":
            creation = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.Popen(
            [exe, "serve"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creation,
        )
    except Exception:
        return False
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        if is_running():
            return True
        time.sleep(1.0)
    return is_running()


def health() -> dict:
    """给前端和启动自检用的健康状态。两种后端返回同一套字段。"""
    if is_cloud():
        if not cloud_configured():
            status, hint = (
                "cloud_not_configured",
                "LLM_BACKEND=openai，但 LLM_API_BASE 或 LLM_API_KEY 还没填。"
                "请在 .env 里补上，或把 LLM_BACKEND 改回 ollama 用本地模型。",
            )
            running = False
        else:
            running = True
            status, hint = "ok", ""
        return {
            "backend": "openai",
            "cloud": True,
            "base_url": config.LLM_API_BASE,
            "running": running,
            "models": [config.LLM_API_MODEL] if running else [],
            "model": config.LLM_API_MODEL,
            "model_ready": running,
            "status": status,
            "hint": hint,
            # 隐私边界变了，前端要常驻提示。措辞与 docs/08 保持一致。
            "privacy_notice": (
                "当前使用云端大模型（"
                + config.LLM_API_MODEL
                + "），您描述的系统和问题会发送到该服务商。"
                "如需数据完全不出本机，请把 .env 里的 LLM_BACKEND 改回 ollama。"
            )
            if running
            else "",
        }

    running = is_running()
    models = list_models() if running else []
    model_ready = any(
        m == config.LLM_MODEL or m.split(":")[0] == config.LLM_MODEL.split(":")[0]
        for m in models
    )
    if not running:
        status, hint = "ollama_not_running", "Ollama 服务没在运行，正在尝试自动启动；也可手动双击 Ollama 图标。"
    elif not model_ready:
        status, hint = "model_missing", f"模型 {config.LLM_MODEL} 还没下载，在命令行执行：ollama pull {config.LLM_MODEL}"
    else:
        status, hint = "ok", ""
    return {
        "backend": "ollama",
        "cloud": False,
        "base_url": config.OLLAMA_BASE_URL,
        "running": running,
        "models": models,
        "model": config.LLM_MODEL,
        "model_ready": model_ready,
        "status": status,
        "hint": hint,
        "privacy_notice": "",
    }


# --------------------------------------------------------------------------
# 云端（OpenAI 兼容）报错翻译
# --------------------------------------------------------------------------
def _cloud_error(resp: httpx.Response) -> LLMUnavailable:
    """把 HTTP 状态码翻译成用户能动手解决的话。"""
    code = resp.status_code
    body = (resp.text or "")[:300]
    if code in (401, 403):
        return LLMUnavailable(
            f"云端接口拒绝访问（{code}）：API Key 无效、过期或没有该模型的权限",
            "检查 .env 里的 LLM_API_KEY 是否复制完整、是否已过期，以及账号是否开通了该模型。",
        )
    if code == 404:
        return LLMUnavailable(
            f"云端接口找不到模型 {config.LLM_API_MODEL}（404）",
            "检查 .env 里的 LLM_API_MODEL 是否是该服务商的准确模型名；"
            "LLM_API_BASE 通常要以 /v1 结尾。",
        )
    if code == 429:
        return LLMUnavailable(
            "云端接口限流或余额不足（429）",
            "稍等再试，或检查账号余额与并发限制；也可以在 .env 里换一个模型。",
        )
    return LLMUnavailable(f"云端接口返回错误 {code}：{body}")


# --------------------------------------------------------------------------
# 生成
# --------------------------------------------------------------------------
def chat(
    messages: list[dict],
    *,
    json_schema: dict | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> str:
    """一次性拿到完整回答（非流式）。按 LLM_BACKEND 分发。"""
    if is_cloud():
        return _cloud_chat(
            messages,
            json_schema=json_schema,
            temperature=temperature,
            max_tokens=max_tokens,
        )
    return _ollama_chat(
        messages,
        json_schema=json_schema,
        temperature=temperature,
        max_tokens=max_tokens,
    )


def _ollama_chat(
    messages: list[dict],
    *,
    json_schema: dict | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> str:
    """一次性拿到完整回答（非流式）。"""
    payload: dict[str, Any] = {
        "model": config.LLM_MODEL,
        "messages": messages,
        "stream": False,
        "options": {
            "temperature": config.LLM_TEMPERATURE if temperature is None else temperature,
            "num_predict": max_tokens or config.LLM_MAX_TOKENS,
        },
    }
    if json_schema is not None:
        payload["format"] = json_schema

    try:
        with _client() as c:
            resp = c.post("/api/chat", json=payload)
    except httpx.ConnectError as exc:
        raise LLMUnavailable(
            "连不上本地 Ollama 服务",
            "请确认 Ollama 正在运行（任务栏托盘有羊驼图标），或在命令行执行 ollama serve。",
        ) from exc
    except httpx.ReadTimeout as exc:
        raise LLMUnavailable(
            f"模型 {config.LLM_MODEL} 响应超时",
            "首次加载模型较慢，可稍后重试；或在 .env 里把 LLM_TIMEOUT 调大（如 300）。",
        ) from exc

    if resp.status_code == 404:
        raise LLMUnavailable(
            f"Ollama 里没有模型 {config.LLM_MODEL}",
            f"在命令行执行：ollama pull {config.LLM_MODEL}",
        )
    if resp.status_code >= 400:
        raise LLMUnavailable(f"Ollama 返回错误 {resp.status_code}：{resp.text[:300]}")

    data = resp.json()
    return (data.get("message") or {}).get("content", "")


def chat_stream(messages: list[dict], *, json_schema: dict | None = None) -> Iterator[str]:
    """流式生成，逐段 yield 文本增量。按 LLM_BACKEND 分发。"""
    if is_cloud():
        yield from _cloud_chat_stream(messages, json_schema=json_schema)
        return
    yield from _ollama_chat_stream(messages, json_schema=json_schema)


def _ollama_chat_stream(
    messages: list[dict], *, json_schema: dict | None = None
) -> Iterator[str]:
    """流式生成，逐段 yield 文本增量。"""
    payload: dict[str, Any] = {
        "model": config.LLM_MODEL,
        "messages": messages,
        "stream": True,
        "options": {
            "temperature": config.LLM_TEMPERATURE,
            "num_predict": config.LLM_MAX_TOKENS,
        },
    }
    if json_schema is not None:
        payload["format"] = json_schema

    try:
        with _client() as c:
            with c.stream("POST", "/api/chat", json=payload) as resp:
                if resp.status_code == 404:
                    raise LLMUnavailable(
                        f"Ollama 里没有模型 {config.LLM_MODEL}",
                        f"在命令行执行：ollama pull {config.LLM_MODEL}",
                    )
                if resp.status_code >= 400:
                    resp.read()
                    raise LLMUnavailable(f"Ollama 返回错误 {resp.status_code}：{resp.text[:300]}")
                for line in resp.iter_lines():
                    if not line:
                        continue
                    try:
                        chunk = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    piece = (chunk.get("message") or {}).get("content", "")
                    if piece:
                        yield piece
                    if chunk.get("done"):
                        break
    except httpx.ConnectError as exc:
        raise LLMUnavailable(
            "连不上本地 Ollama 服务",
            "请确认 Ollama 正在运行（任务栏托盘有羊驼图标），或在命令行执行 ollama serve。",
        ) from exc
    except httpx.ReadTimeout as exc:
        raise LLMUnavailable(
            f"模型 {config.LLM_MODEL} 响应超时",
            "首次加载模型较慢，可稍后重试；或在 .env 里把 LLM_TIMEOUT 调大（如 300）。",
        ) from exc


# --------------------------------------------------------------------------
# 云端（OpenAI 兼容）实现
# --------------------------------------------------------------------------
def _cloud_payload(
    messages: list[dict],
    *,
    json_schema: dict | None,
    temperature: float | None,
    max_tokens: int | None,
    stream: bool,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": config.LLM_API_MODEL,
        "messages": messages,
        "stream": stream,
        "temperature": config.LLM_TEMPERATURE if temperature is None else temperature,
        "max_tokens": max_tokens or config.LLM_MAX_TOKENS,
    }
    if json_schema is not None:
        mode = config.LLM_API_JSON_MODE
        if mode == "json_object":
            payload["response_format"] = {"type": "json_object"}
        elif mode == "json_schema":
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "answer",
                    "strict": False,
                    "schema": json_schema,
                },
            }
        # mode == "none"：不加约束，只能靠提示词。兼容性兜底，稳定性最差。
    return payload


def _cloud_connect_error(exc: Exception) -> LLMUnavailable:
    return LLMUnavailable(
        f"连不上云端接口 {config.LLM_API_BASE}",
        "检查 .env 里的 LLM_API_BASE 是否写对（通常以 /v1 结尾）；"
        "如果这台机器必须走代理才能出网，把 LLM_API_TRUST_ENV 设成 true 再试。",
    )


def _cloud_chat(
    messages: list[dict],
    *,
    json_schema: dict | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> str:
    payload = _cloud_payload(
        messages,
        json_schema=json_schema,
        temperature=temperature,
        max_tokens=max_tokens,
        stream=False,
    )
    try:
        with _cloud_client() as c:
            resp = c.post("/chat/completions", json=payload)
    except httpx.ConnectError as exc:
        raise _cloud_connect_error(exc) from exc
    except httpx.ReadTimeout as exc:
        raise LLMUnavailable(
            f"云端模型 {config.LLM_API_MODEL} 响应超时",
            "可以在 .env 里把 LLM_TIMEOUT 调大（如 300），或换一个更快的模型。",
        ) from exc

    if resp.status_code >= 400:
        raise _cloud_error(resp)

    data = resp.json()
    choices = data.get("choices") or [{}]
    message = choices[0].get("message") or {}
    return str(message.get("content") or "")


def _cloud_chat_stream(
    messages: list[dict], *, json_schema: dict | None = None
) -> Iterator[str]:
    payload = _cloud_payload(
        messages,
        json_schema=json_schema,
        temperature=None,
        max_tokens=None,
        stream=True,
    )
    try:
        with _cloud_client() as c:
            with c.stream("POST", "/chat/completions", json=payload) as resp:
                if resp.status_code >= 400:
                    resp.read()
                    raise _cloud_error(resp)
                for line in resp.iter_lines():
                    if not line:
                        continue
                    if line.startswith("data:"):
                        line = line[5:].strip()
                    if not line or line == "[DONE]":
                        if line == "[DONE]":
                            break
                        continue
                    try:
                        chunk = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if chunk.get("error"):
                        err = chunk["error"]
                        raise LLMUnavailable(
                            f"云端接口流式返回错误：{err.get('message') or err}"
                        )
                    choices = chunk.get("choices") or [{}]
                    piece = (choices[0].get("delta") or {}).get("content") or ""
                    if piece:
                        yield piece
                    if choices[0].get("finish_reason"):
                        break
    except httpx.ConnectError as exc:
        raise _cloud_connect_error(exc) from exc
    except httpx.ReadTimeout as exc:
        raise LLMUnavailable(
            f"云端模型 {config.LLM_API_MODEL} 响应超时",
            "可以在 .env 里把 LLM_TIMEOUT 调大（如 300），或换一个更快的模型。",
        ) from exc


# --------------------------------------------------------------------------
# 流式 JSON 增量提取
# --------------------------------------------------------------------------
_ANSWER_KEY = re.compile(r'"answer"\s*:\s*"')

_ESCAPE_MAP = {
    "n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\",
    "/": "/", "b": "\b", "f": "\f",
}


def extract_partial_answer(buffer: str) -> str | None:
    """从**还没收完**的 JSON 文本里，把 answer 字段已经出来的部分抠出来。

    实现在意两件事：
    1. 转义要正确处理（\\n、\\" 、\\uXXXX），否则前端会看到一堆反斜杠
    2. 转义序列被切断时要能停下（比如 buffer 结尾是单独一个反斜杠），
       等下一个分片到了再继续

    Ollama 用 JSON Schema 约束解码时按 schema 的属性顺序生成，
    answer 是第一个属性，所以通常一开始就能拿到增量。
    """
    if not buffer:
        return None
    m = _ANSWER_KEY.search(buffer)
    if not m:
        return None

    out: list[str] = []
    i = m.end()
    length = len(buffer)
    while i < length:
        ch = buffer[i]
        if ch == "\\":
            if i + 1 >= length:
                break  # 转义还没收完，先停
            nxt = buffer[i + 1]
            if nxt == "u":
                if i + 6 > length:
                    break
                try:
                    out.append(chr(int(buffer[i + 2 : i + 6], 16)))
                except ValueError:
                    pass
                i += 6
                continue
            out.append(_ESCAPE_MAP.get(nxt, nxt))
            i += 2
            continue
        if ch == '"':
            break  # answer 字段结束
        out.append(ch)
        i += 1
    return "".join(out)


def chat_stream_json(
    messages: list[dict],
    json_schema: dict,
    on_delta: Any,
) -> str:
    """流式生成并**实时**把 answer 字段的增量回调出去，最后返回完整原始文本。

    on_delta 每收到一段新文本就被调用一次。它是可选增强：
    即使一次都没触发，调用方也会拿到完整文本，按原来的方式处理。
    """
    buffer = ""
    emitted = ""
    for piece in chat_stream(messages, json_schema=json_schema):
        buffer += piece
        current = extract_partial_answer(buffer)
        if current and len(current) > len(emitted):
            try:
                on_delta(current[len(emitted):])
            except Exception as exc:  # 回调失败不能影响生成
                log.warning("流式回调失败，后续改为一次性输出：%s", exc)
            emitted = current
    return buffer


# --------------------------------------------------------------------------
# JSON 容错解析（小模型经常加解释文字或代码围栏）
# --------------------------------------------------------------------------
def parse_json_loose(text: str) -> dict | None:
    if not text:
        return None
    raw = text.strip()

    # 去掉 ```json ... ``` 围栏
    fence = re.search(r"```(?:json)?\s*(.+?)```", raw, re.S)
    if fence:
        raw = fence.group(1).strip()

    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass

    # 截取第一个 { 到最后一个 }
    start, end = raw.find("{"), raw.rfind("}")
    if start != -1 and end > start:
        candidate = raw[start : end + 1]
        try:
            obj = json.loads(candidate)
            return obj if isinstance(obj, dict) else None
        except json.JSONDecodeError:
            # 常见毛病：尾逗号、中文引号
            fixed = re.sub(r",\s*([}\]])", r"\1", candidate)
            fixed = fixed.replace("“", '"').replace("”", '"')
            try:
                obj = json.loads(fixed)
                return obj if isinstance(obj, dict) else None
            except json.JSONDecodeError:
                return None
    return None


def salvage_answer_field(text: str) -> str | None:
    """模型输出的 JSON 坏了，但 answer 字段本身可能是完整的——把它救出来。

    实测模型会在长数组（比如 risk_tags）里进入重复循环，输出被截断，
    整个 JSON 就不再是合法 JSON 了。这时若直接把原始文本当正文，用户会看到
    一大坨 `{"answer": "…", "citations": [ …` 的花括号内容，
    真正的答案反而被埋在中间，引用也全丢了。

    所以这里只做一件事：从坏掉的文本里把 answer 字段的内容取出来，
    其余字段（引用、风险标签、追问）一概丢弃——宁可少给东西，
    也不要把结构文本当成人话端给用户。
    """
    text = (text or "").strip()
    if not text:
        return None
    got = extract_partial_answer(text)
    if not got:
        return None
    got = got.strip()
    # 抠出来的必须像"给用户看的话"，不能又是一坨结构文本（否则等于没救出来）
    if not got or got.startswith("{") or '"citations"' in got:
        return None
    return got
