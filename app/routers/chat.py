"""聊天接口：提问、流式回答、会话历史。

一条提问的完整处理链路（每一步都可能改变最终行为）：
  1. 保存用户消息
  2. 检索条款  → 低于阈值 ⇒ 直接拒答（不经大模型，从源头杜绝编造）
  3. 组装提示词（含规则与可用条款）→ 调本地 Ollama，按 JSON Schema 出结果
  4. 引用白名单校验 → 剔除非本次检索到的条款号，并记录可疑编号
  5. ⚠️ 风险判定（模型自报 ∪ 命中条款标签 ∪ 问题关键词）
  6. 补免责声明 → 落库 → 返回

大模型不可用时，降级为"只列条款"，功能不中断。
"""
from __future__ import annotations

import json
import logging
import queue
import re
import threading
import time
import uuid
from typing import Any, Callable, Iterator

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from .. import config, db, guard, llm, prompt, scope
from ..logging_setup import log_qa
from ..models import ChatRequest
from ..normalize import tokenize
from ..retrieval import get_engine

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["chat"])

GREETING_PATTERNS = re.compile(
    r"^\s*(你好|您好|hi|hello|嗨|在吗|你是谁|你能做什么|介绍一下自己|帮助|怎么用)[\s!！。.?？]*$",
    re.I,
)

# 本地 7B 模型的上下文是 8192 token。提示词里已经要塞 6 条条款和对话历史，
# 留给问题的空间不多。实测 1.9 万字的提问会让 Ollama 直接返回 400
# （exceeds the available context size），虽然能优雅降级，
# 但用户本来可以拿到真正的回答。所以超长时主动截断。
MAX_QUESTION_CHARS = 1500
GREETING_REPLY = (
    "您好，我是企业安全合规自查助手。\n\n"
    "您可以用大白话描述自己的系统情况，我会从知识库里的三套标准"
    "（等保2.0 安全通用要求、NIST CSF 2.0、OWASP Top 10 2021）"
    "找出对应的条款，并给出条款编号。\n\n"
    "可以这样问我：\n"
    "- 我们公司员工都用同一个管理员账号登录进销存系统，有什么问题吗？\n"
    "- 客户手机号存在数据库里，没有加密，需要注意什么？\n"
    "- 员工离职后账号权限没回收，合规上有什么要求？\n\n"
    "注意：本工具用于辅助自查，不构成正式测评结论。"
)

# 适用性判定类问题没法直接回答，但可以问出"要判定适不适用，必须知道什么"。
# 这三个问题正好覆盖适用性分析真正需要的输入。
APPLICABILITY_FOLLOWUPS = [
    "您的系统定级是几级（二级还是三级）？这直接决定适用哪一档要求。",
    "系统是自建机房、托管还是部署在云上？是否与互联网直接相连？",
    "系统里存放了哪类数据（是否含个人信息、敏感个人信息），大概多少人使用？",
]


def _retrieved(clauses: list[dict]) -> list[dict]:
    """给前端展示用的"本次检索到什么"，只保留必要字段。"""
    return [
        {
            "clause_id": c["clause_id"],
            "standard_name": c["standard_name"],
            "title": c["title"],
            "score": c["score"],
        }
        for c in clauses
    ]


# --------------------------------------------------------------------------
# 会话
# --------------------------------------------------------------------------
def _new_session(title: str = "新对话") -> str:
    sid = uuid.uuid4().hex[:12]
    db.execute(
        "INSERT INTO sessions(id, title, created_at, updated_at) VALUES(?,?,?,?)",
        (sid, title[:30], db.now(), db.now()),
    )
    return sid


def _ensure_session(session_id: str | None, question: str) -> str:
    if session_id:
        row = db.query_one("SELECT id FROM sessions WHERE id = ?", (session_id,))
        if row:
            return session_id
    title = re.sub(r"\s+", " ", question).strip()[:24] or "新对话"
    return _new_session(title)


def _save_message(session_id: str, role: str, content: str, payload: dict | None = None) -> int:
    if not config.SAVE_HISTORY:
        return 0
    mid = db.execute(
        "INSERT INTO messages(session_id, role, content, payload, created_at) VALUES(?,?,?,?,?)",
        (session_id, role, content, db.dump_json(payload) if payload else None, db.now()),
    )
    db.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (db.now(), session_id))
    return mid


def _history(session_id: str, limit: int = 6) -> list[dict]:
    rows = db.query(
        "SELECT role, content FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT ?",
        (session_id, limit),
    )
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


# --------------------------------------------------------------------------
# 核心问答
# --------------------------------------------------------------------------
def _needs_retrieval(question: str) -> bool:
    """打招呼之类的客套话不需要检索，直接回引导语。"""
    return not (len(question.strip()) <= 20 and GREETING_PATTERNS.match(question.strip()))


def answer_question(
    question: str,
    session_id: str | None = None,
    on_delta: Callable[[str], None] | None = None,
    on_stage: Callable[[str, dict], None] | None = None,
) -> dict:
    """处理一次提问，返回完整结果。

    on_delta 传入时启用真流式：模型边生成，边把 answer 的增量回调出去。
    注意流式只是"提前把文字给用户看"，**所有校验（引用白名单、⚠️判定、
    矛盾清理）仍然在生成结束后照常执行**，不因为流式而放松。

    on_stage 用于把中间进度（比如"命中了哪条条款"）报给前端。
    """
    t0 = time.time()

    def stage(name: str, **payload: Any) -> None:
        if on_stage is None:
            return
        try:
            on_stage(name, payload)
        except Exception as exc:  # 进度回调失败不能影响主流程
            log.debug("进度回调失败：%s", exc)

    question = (question or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="问题不能为空")

    sid = _ensure_session(session_id, question)
    _save_message(sid, "user", question)

    notes: list[str] = []

    # ---- 客套话 ----
    if not _needs_retrieval(question):
        result = {
            "session_id": sid,
            "answer": GREETING_REPLY,
            "citations": [],
            "risks": [],
            "need_clarification": False,
            "followup_questions": [],
            "found": True,
            "mode": "greeting",
            "out_of_kb": [],
            "notes": [],
        }
        _save_message(sid, "assistant", result["answer"], _payload(result))
        log_qa(log, question, result, time.time() - t0)
        return result

    # ---- 检索 ----
    engine = get_engine()
    search = engine.search(question)
    clauses = search["results"]
    best = search["best_score"]

    # ---- 问题边界判定（必须放在阈值闸门之前）----
    # 这两类问题的得分都**很高**，阈值那道闸门拦不住，所以要单独判：
    #   ① 用户点名了知识库未收录的法规 → 模型会凭记忆编它的条文
    #   ② 要求判断条款适用性 → 模型会把检索到的条款号当成"不适用清单"吐出来
    # 判定全部由代码做（app/scope.py），不依赖模型自觉。
    undeterminable, undet_reason = scope.needs_system_context(question)
    out_of_kb = scope.detect_out_of_kb(question)
    out_of_kb_names = [d["name"] for d in out_of_kb]

    if not clauses or best < config.RETRIEVAL_MIN_SCORE:
        # 即使检索不到条款，只要用户点名了库外法规，也要明确告诉他"没收录这部法规"，
        # 否则用户会以为"知识库里没有"等于"这事没法问"。
        notice = scope.out_of_kb_notice(out_of_kb_names)
        answer = (notice + "\n" + prompt.NO_RESULT_ANSWER) if notice else prompt.NO_RESULT_ANSWER
        result = {
            "session_id": sid,
            "answer": answer,
            "citations": [],
            "risks": [],
            "need_clarification": False,
            "followup_questions": [],
            "found": False,
            "mode": "no_result",
            "out_of_kb": out_of_kb_names,
            "notes": [f"最高匹配得分 {best:.2f}，低于阈值 {config.RETRIEVAL_MIN_SCORE}"],
        }
        _save_message(sid, "assistant", result["answer"], _payload(result))
        log_qa(log, question, result, time.time() - t0)
        return result

    # ---- 适用性判定类问题：直接由服务端出答案，不调大模型 ----
    # 这类问题的正确答案就是"无法在不了解您系统的情况下判定"，
    # 让模型去编一份"不适用条款清单"只会害人（实测它会把检索到的条款号照抄出来）。
    # 所以这里**刻意跳过模型**：既保证不会编，也省掉一次几秒的推理。
    if undeterminable:
        ref_clauses = clauses[:5]
        cited = [c["clause_id"] for c in ref_clauses]
        result = {
            "session_id": sid,
            "answer": guard.ensure_disclaimer(
                scope.needs_context_answer(undet_reason, ref_clauses)
            ),
            "citations": [guard.to_citation(c) for c in ref_clauses],
            "risks": guard.detect_risks(question, clauses, None, cited_ids=cited),
            "need_clarification": True,
            "followup_questions": APPLICABILITY_FOLLOWUPS,
            "found": True,
            "mode": "needs_context",
            "out_of_kb": out_of_kb_names,
            "notes": [
                f"这类问题需要结合您的系统情况逐条比对才能判断（{undet_reason}），"
                f"已直接说明并给出追问，未让大模型硬凑一份清单"
            ],
            "retrieved": _retrieved(clauses),
        }
        _save_message(sid, "assistant", result["answer"], _payload(result))
        log_qa(log, question, result, time.time() - t0)
        return result

    # ---- 调大模型 ----
    if clauses:
        top = clauses[0]
        stage("generating", text=f"已命中条款（最高匹配 {top['clause_id']} {top['title']}），正在生成回答…")

    question_for_prompt = question
    if len(question) > MAX_QUESTION_CHARS:
        question_for_prompt = question[:MAX_QUESTION_CHARS]
        notes.append(
            f"您的问题较长（{len(question)} 字），已截取前 {MAX_QUESTION_CHARS} 字送入模型分析"
        )
        log.info("问题过长已截断 · 原始 %d 字", len(question))

    # ---- 库外法规：给模型的额外约束 ----
    # 光靠系统提示词不够。实测模型会把"《个人信息保护法》第13条规定了…"
    # 写得理直气壮，还挂着等保的引用编号。这里点名告诉它别碰，
    # 生成之后 guard.strip_out_of_kb_claims 还会再清一道。
    extra_hint = ""
    if out_of_kb_names:
        listed = "、".join(out_of_kb_names)
        extra_hint = (
            f"用户在问题里点名了知识库未收录的法规：{listed}。"
            f"你绝对不能写出或转述该法规的任何条文内容、条号含义，也不要解释它规定了什么。"
            f"服务端会自动在回答最前面补一段『知识库未收录』的说明，你不要重复写。"
            f"请专心用【可用条款】里真实存在的条款，回答与该主题相关的部分。"
        )

    messages = prompt.build_messages(
        question_for_prompt, clauses, _history(sid), extra_hint=extra_hint
    )
    data: dict | None = None
    mode = "llm"
    try:
        use_stream = on_delta is not None and config.ENABLE_TRUE_STREAMING
        if use_stream:
            raw = llm.chat_stream_json(messages, prompt.ANSWER_SCHEMA, on_delta)
        else:
            raw = llm.chat(messages, json_schema=prompt.ANSWER_SCHEMA)
        data = llm.parse_json_loose(raw)
        if data is None:
            notes.append("模型输出不是合法 JSON，已按纯文本处理")
            # 实测模型会在 risk_tags 这类长数组里进入重复循环、输出被截断，
            # 于是整个 JSON 解析失败。此时**绝不能把原始文本直接当正文**：
            # 用户会看到一大坨花括号结构，答案被埋在中间，引用还全丢了。
            # 先尝试只把 answer 字段救出来；救不出来就留空，
            # 下面会走 degradation_answer（并因此补上检索到的条款引用）。
            salvaged = llm.salvage_answer_field(raw)
            if salvaged:
                notes.append("已从异常输出中抢救出正文，其余字段（引用、风险标签）按检索结果补齐")
            else:
                notes.append("异常输出中未能抢救出正文，已改为列出检索到的条款")
            data = {
                "answer": salvaged or "",
                "citations": [],
                "risk_tags": [],
                "need_clarification": False,
                "followup_questions": [],
                "found": True,
            }
    except llm.LLMUnavailable as exc:
        mode = "retrieval_only"
        log.warning("大模型不可用，降级为条款检索 · %s", exc)
        notes.append(str(exc))
        if exc.hint:
            notes.append(exc.hint)
        data = {
            "answer": prompt.degradation_answer(clauses, str(exc)),
            "citations": [],
            "risk_tags": [],
            "need_clarification": False,
            "followup_questions": [],
            "found": True,
        }

    # ---- 引用白名单校验 ----
    valid_citations, invalid_ids = guard.validate_citations(data.get("citations"), clauses)
    if invalid_ids:
        notes.append("已剔除不在本次检索结果中的条款编号：" + "、".join(invalid_ids[:5]))

    answer_text = guard.strip_markdown_headings(str(data.get("answer") or "").strip())
    # 把模型可能写的「条款1」翻译成真实条款编号，再让用户看到
    answer_text = guard.rewrite_positional_refs(answer_text, clauses)
    if not answer_text:
        answer_text = prompt.degradation_answer(clauses, "模型返回了空内容")
        mode = "retrieval_only"

    # 引用兜底：**只要有检索结果、而模型一个可用引用都没给出，就用检索到的条款补上。**
    # 触发场景有三种：大模型不可用、模型返回空内容、模型的 JSON 坏了导致 citations 全丢。
    # 最后一种曾经漏掉——那时 mode 仍然是 "llm"，旧的 `mode == "retrieval_only"`
    # 条件不成立，用户会看到一段**没有任何出处的结论**。
    # 用户的第一条硬性要求就是"回答要带引用来源"，所以这里的判据应该是
    # "没有可用引用"，而不是"处于降级模式"。
    if not valid_citations and clauses:
        valid_citations = [guard.to_citation(c) for c in clauses[:5]]
        if mode == "llm":
            notes.append("模型未给出可用的引用，已按本次检索结果补充参考条款")

    hallucinated = guard.detect_hallucinated_ids(answer_text, clauses)
    if hallucinated:
        notes.append("正文中出现未在知识库检索到的编号，请核实：" + "、".join(hallucinated[:5]))

    # ---- 库外法规：先清掉转述，再由服务端强制补上"未收录"说明 ----
    # 顺序很重要：先清模型写出来的东西，再拼服务端生成的说明。
    # 反过来的话，服务端那段说明自己带着法规名，会被清理逻辑误伤。
    if out_of_kb:
        answer_text, removed = guard.strip_out_of_kb_claims(answer_text, out_of_kb)
        if removed:
            notes.append(
                f"已删除 {removed} 处可能转述库外法规内容的表述（该法规未收录在知识库中）"
            )
            log.info("已清理库外法规转述 · %d 处 · %s", removed, "、".join(out_of_kb_names))
        notice = scope.out_of_kb_notice(out_of_kb_names)
        if notice:
            answer_text = notice + "\n" + answer_text

    # ---- 是否要找用户补充信息 ----
    need_clarify = bool(data.get("need_clarification"))
    forced_clarify_notice = ""
    followups = [str(q).strip() for q in (data.get("followup_questions") or []) if str(q).strip()]
    if need_clarify and not followups:
        need_clarify = False  # 说要追问却没给问题，视为误判
    # 服务端兜底：问题笼统到"给不出针对性建议"时，必须追问。
    # 为什么不让模型自己判：实测 7B 模型经常漏做这件事——
    # 问"我们的数据安全吗？"它直接给一段泛泛的结论，一个反问都没有，
    # 而用户会以为系统已经看过他的情况了。这里只做**单向补位**：
    # 模型说要追问就尊重它（它的问题往往更贴合上下文），
    # 模型漏了、而规则有把握时才由服务端补上。
    if not need_clarify and scope.needs_clarification(question):
        need_clarify = True
        followups = list(scope.CLARIFY_FOLLOWUPS)
        notes.append("问题未提供具体的系统情况，已按服务端规则补充追问（模型未主动追问）")
        # 模型没打算追问，它那段结论就是"已经看过你的情况"的口吻，
        # 必须在最前面把话说明白（放在免责声明之前处理，见下面的顺序）。
        forced_clarify_notice = scope.clarify_notice()
    if not need_clarify:
        followups = []        # 不需要追问时，别把问题推给用户，免得误导

    # ---- found 由检索结果决定，不听模型的 ----
    # 能走到这里，说明检索到了高于阈值的条款，知识库里就是有的。
    # 实测 qwen2.5 会把"我还想多问几句"误表达成"知识库中未找到"，
    # 这会把用户直接带偏，所以这一项必须由服务端拍板：
    # "有没有" 完全由检索阈值决定（那道闸门在前面已经拦过无关问题了）。
    model_found = bool(data.get("found", True))
    found = True
    if not model_found:
        notes.append("模型原始判断为『未找到』，已按检索结果纠正（检索到了相关条款）")

    # ---- ⚠️ 风险 ----
    risks = guard.detect_risks(
        question, clauses, data.get("risk_tags"),
        cited_ids=[c["clause_id"] for c in valid_citations],
    )

    answer_text = guard.ensure_disclaimer(answer_text)
    # 服务端已判定"找到了条款"，正文里就不该再出现"知识库中未找到"
    if found and not model_found:
        answer_text = guard.strip_false_no_result(answer_text)
    # 由服务端补的追问提示放在最前面（放在最后拼，避免被上面两步改坏）
    if forced_clarify_notice:
        answer_text = forced_clarify_notice + "\n" + answer_text

    result = {
        "session_id": sid,
        "answer": answer_text,
        "citations": valid_citations,
        "risks": risks,
        "need_clarification": need_clarify,
        "followup_questions": followups[:3],
        "found": found,
        "mode": mode,
        "out_of_kb": out_of_kb_names,
        "notes": notes,
        "retrieved": _retrieved(clauses),
    }
    _save_message(sid, "assistant", result["answer"], _payload(result))
    log_qa(log, question, result, time.time() - t0)
    return result


def _payload(result: dict) -> dict:
    return {
        "citations": result.get("citations", []),
        "risks": result.get("risks", []),
        "need_clarification": result.get("need_clarification", False),
        "followup_questions": result.get("followup_questions", []),
        "found": result.get("found", True),
        "mode": result.get("mode", ""),
        "out_of_kb": result.get("out_of_kb", []),
        "notes": result.get("notes", []),
    }


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _stream(question: str, session_id: str | None) -> Iterator[str]:
    """SSE 流式输出。

    设计要点（为什么要用线程 + 队列）：

    真流式意味着"模型还在生成，文字已经推给用户了"，
    但生成结束后的**校验必须照常做**（引用白名单、⚠️ 判定、位置序号翻译、
    补免责声明）。所以这里的做法是：
      - 把 answer_question 丢到工作线程里跑，它一边生成一边把增量丢进队列
      - 主线程只负责从队列取东西并推给前端
      - 生成结束后，如果最终文本和流出去的文本不一致（校验改动了内容），
        再发一个 replace 或补一段增量

    这样"流式"只是提前展示，**没有绕过任何一道校验**。
    """
    yield _sse("status", {"stage": "retrieving", "text": "正在检索知识库条款…"})
    start = time.time()

    # 会话先建出来，这样"session"事件可以在最前面就发出去
    sid = _ensure_session(session_id, question)
    yield _sse("session", {"session_id": sid})

    events: "queue.Queue[tuple[str, Any]]" = queue.Queue()
    holder: dict = {}
    streamed = {"text": ""}

    def on_delta(piece: str) -> None:
        if not piece:
            return
        streamed["text"] += piece
        events.put(("delta", piece))

    def on_stage(stage: str, payload: dict) -> None:
        events.put(("stage", {"stage": stage, **payload}))

    def worker() -> None:
        try:
            holder["result"] = answer_question(
                question, sid, on_delta=on_delta, on_stage=on_stage
            )
        except HTTPException as exc:
            holder["http_error"] = exc
        except Exception as exc:  # 兜底，别让前端白屏
            holder["error"] = exc
        finally:
            events.put(("done", None))

    threading.Thread(target=worker, name="answer", daemon=True).start()

    while True:
        kind, payload = events.get()
        if kind == "done":
            break
        if kind == "delta":
            yield _sse("delta", {"text": payload})
        elif kind == "stage":
            yield _sse("status", payload)

    if "http_error" in holder:
        exc = holder["http_error"]
        log.warning("提问被拒绝 · %s", exc.detail)
        yield _sse("error", {"message": str(exc.detail)})
        return
    if "error" in holder:
        log.exception("处理提问时出错", exc_info=holder["error"])
        yield _sse("error", {"message": f"处理失败：{holder['error']}"})
        return

    result = holder["result"]
    final_text = result["answer"]
    emitted = streamed["text"]

    if not emitted:
        # 模型没按 schema 先写 answer，或者解析失败 → 退回打字机回放
        for i in range(0, len(final_text), 6):
            yield _sse("delta", {"text": final_text[i : i + 6]})
            time.sleep(0.012)
    elif final_text != emitted:
        # 生成后的校验改动了正文。能接上就补增量，接不上就整段替换。
        if final_text.startswith(emitted):
            yield _sse("delta", {"text": final_text[len(emitted):]})
        else:
            yield _sse("replace", {"text": final_text})

    yield _sse("meta", {
        "citations": result["citations"],
        "risks": result["risks"],
        "need_clarification": result["need_clarification"],
        "followup_questions": result["followup_questions"],
        "found": result["found"],
        "mode": result["mode"],
        "out_of_kb": result.get("out_of_kb", []),
        "notes": result["notes"],
        "elapsed": round(time.time() - start, 2),
        "streamed": bool(emitted),
    })
    yield _sse("done", {"ok": True})


@router.post("/chat/stream")
def chat_stream(payload: ChatRequest):
    return StreamingResponse(
        _stream(payload.question, payload.session_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/chat")
def chat(payload: ChatRequest):
    return answer_question(payload.question, payload.session_id)


# --------------------------------------------------------------------------
# 会话管理
# --------------------------------------------------------------------------
@router.get("/sessions")
def list_sessions():
    rows = db.query(
        """
        SELECT s.id, s.title, s.created_at, s.updated_at,
               (SELECT COUNT(*) FROM messages m WHERE m.session_id = s.id) AS message_count
        FROM sessions s ORDER BY s.updated_at DESC LIMIT 100
        """
    )
    return {"sessions": [dict(r) for r in rows]}


@router.get("/sessions/{session_id}")
def get_session(session_id: str):
    rows = db.query(
        "SELECT id, role, content, payload, created_at FROM messages "
        "WHERE session_id = ? ORDER BY id",
        (session_id,),
    )
    messages = []
    for r in rows:
        messages.append({
            "id": r["id"],
            "role": r["role"],
            "content": r["content"],
            "created_at": r["created_at"],
            **(db.load_json(r["payload"], {}) or {}),
        })
    return {"session_id": session_id, "messages": messages}


@router.delete("/sessions/{session_id}")
def delete_session(session_id: str):
    db.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
    db.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
    return {"ok": True}


@router.put("/sessions/{session_id}")
def rename_session(session_id: str, body: dict):
    title = str(body.get("title") or "").strip()[:30] or "新对话"
    db.execute("UPDATE sessions SET title = ? WHERE id = ?", (title, session_id))
    return {"ok": True}


@router.get("/sessions/{session_id}/export.docx")
def export_session_docx(session_id: str):
    """导出 Word 版自查报告，适合发给顾问或存档。"""
    from urllib.parse import quote

    from fastapi.responses import Response

    from .. import export

    row = db.query_one("SELECT * FROM sessions WHERE id = ?", (session_id,))
    if not row:
        raise HTTPException(status_code=404, detail="会话不存在")

    rows = db.query(
        "SELECT role, content, payload, created_at FROM messages "
        "WHERE session_id = ? ORDER BY id",
        (session_id,),
    )
    if not rows:
        raise HTTPException(status_code=400, detail="这个对话还没有内容，无法导出")

    messages = []
    for r in rows:
        payload = db.load_json(r["payload"], {}) or {}
        messages.append({
            "role": r["role"],
            "content": r["content"],
            "created_at": r["created_at"],
            **payload,
        })

    standards = [s["name"] for s in db.query("SELECT name FROM standards")]
    try:
        data = export.build_report(row["title"], session_id, messages, standards)
    except Exception as exc:
        log.exception("生成 Word 报告失败")
        raise HTTPException(status_code=500, detail=f"生成报告失败：{exc}") from exc

    filename = f"安全合规自查报告_{session_id}.docx"
    log.info("导出 Word 报告 · 会话=%s · %d 条消息 · %.1f KB",
             session_id, len(messages), len(data) / 1024)
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={
            # 中文文件名必须用 RFC 5987 的写法，否则浏览器会乱码
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}",
        },
    )


@router.get("/sessions/{session_id}/export")
def export_session(session_id: str):
    """导出 Markdown 自查记录。"""
    row = db.query_one("SELECT * FROM sessions WHERE id = ?", (session_id,))
    if not row:
        raise HTTPException(status_code=404, detail="会话不存在")
    rows = db.query(
        "SELECT role, content, payload, created_at FROM messages WHERE session_id = ? ORDER BY id",
        (session_id,),
    )
    lines = [
        f"# 安全合规自查记录 · {row['title']}",
        "",
        f"- 生成时间：{db.now()}",
        f"- 会话编号：{session_id}",
        "- 依据标准：等保2.0（GB/T 22239-2019）安全通用要求、NIST CSF 2.0、OWASP Top 10 2021",
        "",
        "> 免责声明：本记录由辅助自查工具生成，仅用于自查参考，不构成正式测评结论，"
        "也不构成法律意见。涉及数据加密、访问控制、个人信息保护等高风险事项，"
        "建议咨询专业安全顾问或有资质的等保测评机构。",
        "",
        "---",
        "",
    ]
    for r in rows:
        who = "我" if r["role"] == "user" else "助手"
        lines.append(f"## {who}")
        lines.append("")
        lines.append(r["content"])
        payload = db.load_json(r["payload"], {}) or {}
        cites = payload.get("citations") or []
        if cites:
            lines.append("")
            lines.append("**引用条款：**")
            lines.append("")
            for c in cites:
                lines.append(
                    f"- `{c.get('standard_name')} · {c.get('clause_id')} {c.get('title')}`"
                )
        risks = payload.get("risks") or []
        if risks:
            lines.append("")
            lines.append("**风险提示：**")
            lines.append("")
            for rk in risks:
                lines.append(f"- ⚠️ {rk.get('label')}：{rk.get('why', '')}")
        lines.append("")
        lines.append("---")
        lines.append("")
    return {"filename": f"自查记录_{session_id}.md", "content": "\n".join(lines)}
