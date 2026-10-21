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

from .. import answer as answer_mod
from .. import config, db, guard, llm, prompt, scope
from ..logging_setup import log_qa
from ..models import ChatRequest
from ..normalize import tokenize
from ..retrieval import get_engine

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["chat"])

# 「长得像条款编号」的字符串：8.1.4.1 / A05:2021 / A01:2021-CWE-352 / DE.CM-01 / PR.AA-01。
# 只用于**回显**校验：模型返回的 clauses 是不可信输入，不能原样拼进正文
# （t29 红队用 clauses:["你们应当按三级建设。"] 把一句话写进了用户看到的答案）。
#
# 这条正则**按知识库里真实存在的编号文法写成闭集**，不是「含数字就当编号」。
# 曾经的宽松版是 `^(?=.*[\d:])[A-Za-z0-9][A-Za-z0-9._:\-]{1,31}$`，
# t29 复验用 clauses:["9.9.9.9-ni-bu-yong-zuo-dengbao"] 就把一句拼音写进了正文——
# 它含数字、含点、含连字符、长度 26，宽松版全过。闭集之后只有三种形状能回显：
#   等保   8.1.4.1 / 8.2.4.11                         数字点分
#   NIST   DE.CM-01 / PR.AA-01 / ID.RA-07             两位大写字母.两位大写字母-两位数字
#   OWASP  A05:2021 / A01:2021-CWE-352 / A05:2021-2   A+两位数字:四位年份[-CWE-数字 | -数字]
_ID_LIKE = re.compile(
    r"^(?:\d{1,2}(?:\.\d{1,3}){2,4}"
    r"|[A-Z]{2}\.[A-Z]{2}-\d{2}"
    r"|A\d{2}:\d{4}(?:-(?:CWE-\d+|\d+))?"
    r")$"
)


def _citation_warning(invalid_ids: list[str]) -> str:
    """把「这几个编号对不上」写成用户能读懂的一句话。

    只回显长得像条款编号的字符串：invalid_ids 直接来自模型的 clauses 字段，
    是**不可信输入**。旧实现把它们原样拼进正文，模型只要返回
    clauses:["你们应当按三级建设。"] 就能把一句话写进用户看到的答案。
    回显不了的（含中文、含标点、过长）一律不展示原文，只说"若干编号"。
    """
    if not invalid_ids:
        return ""
    echo = [x for x in invalid_ids[:5] if _ID_LIKE.fullmatch(x)]
    cited = "、".join(echo) if echo else "若干编号"
    return (
        "⚠️ 引用核对：回答里提到的 "
        + cited
        + " 不在本次检索到的条款中，已从引用列表里剔除；"
        "这部分内容没有知识库依据，请以官方标准文本为准，不要直接当成结论。"
    )

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


def _recent_user_text(session_id: str, limit: int = 4) -> str:
    """取最近几轮**用户说过的话**（不含本轮），给多轮话题延续用。

    注意先保存本轮问题再调这里，所以结果里第一条就是本轮——
    跳过它，免得"本轮提到库外法规"被自己当成"上一轮提到过"。
    历史关闭（SAVE_HISTORY=false）时查不到东西，返回空串：
    没有记忆就不做跨轮推断，这比假装记得更诚实。
    """
    rows = db.query(
        "SELECT content FROM messages WHERE session_id = ? AND role = 'user' "
        "ORDER BY id DESC LIMIT ?",
        (session_id, limit + 1),
    )
    return "\n".join(str(r["content"]) for r in reversed(rows[1:]))



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
    on_preface: Callable[[str], None] | None = None,
) -> dict:
    """处理一次提问，返回完整结果。

    t28 起模型不再生成答案正文：正文由 app/answer.py 按固定结构拼装。
    - on_preface 把"服务端拼好的条款清单"推给流式接口，用于首屏（不等模型）；
    - on_delta 保留参数只为兼容调用方，模型已没有可流式展示的正文。
    所有校验（引用白名单、风险、澄清）仍在拼装前后照常执行。
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
    recent_user_text = _recent_user_text(sid)
    # 第五轮：多轮追问也要拦。用户第二轮只说「就按这个来建设吧」，本轮没有
    # 等级词，但上一轮问的是"定到哪一级"——把上一轮原文交给判定，避免漏判。
    # 模型之前的闸门：顺序只定义在 scope.pre_model_gate() 一处，评测也调它。
    # 这里只读结论，不再自己写一遍 if —— 抄两遍就会漏（h10/h11 就是这么漏的）。
    verdict = scope.pre_model_gate(
        question,
        best,
        has_clauses=bool(clauses),
        recent_user_text=recent_user_text,
    )
    undet_reason = verdict.detail if verdict.mode == scope.GATE_NEEDS_CONTEXT else ""
    out_of_kb = scope.detect_out_of_kb(question)
    if not out_of_kb:
        # H-3 多轮绕过：上一轮点名了库外法规、本轮只问「第十三条怎么规定的」时，
        # 问题里已经没有法规名了。只查问题不查会话，等于第一轮的拦截白做。
        carried = scope.followup_out_of_kb(question, recent_user_text)
        if carried:
            out_of_kb = carried
            notes.append(
                "本轮问题没有点名法规，但上一轮提到过 "
                + "、".join(d["name"] for d in carried)
                + "，已继续按『知识库未收录』处理"
            )
    out_of_kb_names = [d["name"] for d in out_of_kb]

    # ---- 领域边界：根本不属于网络安全合规的问题 ----
    # 劳动法加班费、纸质合同保存年限、CE 认证这类问题，关键词分只有 0.10~0.17，
    # 语义分却高达 0.84~0.93（bge-small-zh 觉得它们和「人员离岗」「管理制度评审」
    # 「合规产品采购」很像），融合后会盖过阈值。调阈值切不开——dev 集里关键词分
    # 最低的真题比这类拒答题还低。所以用领域证据判，判据在 app/scope.py。
    domain_label = verdict.label if verdict.mode == scope.GATE_OUT_OF_DOMAIN else ""
    domain_terms = list(verdict.terms)
    if verdict.mode == scope.GATE_OUT_OF_DOMAIN:
        result = {
            "session_id": sid,
            "answer": scope.out_of_domain_notice(domain_label, domain_terms),
            "citations": [],
            "risks": [],
            "need_clarification": False,
            "followup_questions": [],
            "found": False,
            "mode": "out_of_domain",
            "out_of_kb": [],
            "notes": [f"领域拦截：{domain_label}（{'、'.join(domain_terms)}）"],
        }
        _save_message(sid, "assistant", result["answer"], _payload(result))
        log_qa(log, question, result, time.time() - t0)
        return result

    # ---- 知识库完全没有的话题（价格 / 机构 / 通过率）：服务端直接答，不调模型 ----
    # 这几类问题的正确答案不在三套技术标准里，模型只能编数字、编机构名、编周期。
    # 检索分数还可能很高（"合规要花很多钱吗"语义上很像"合规成本"），阈值拦不住。
    no_kb_topic = verdict.label if verdict.mode == scope.GATE_NO_KB_TOPIC else ""
    if verdict.mode == scope.GATE_NO_KB_TOPIC:
        result = {
            "session_id": sid,
            "answer": guard.ensure_disclaimer(scope.no_kb_topic_notice(no_kb_topic)),
            "citations": [],
            "risks": [],
            "need_clarification": False,
            "followup_questions": [],
            "found": False,
            "mode": "no_kb_topic",
            "out_of_kb": [],
            "notes": [f"知识库不含「{no_kb_topic}」的信息，已直接说明，未调模型"],
        }
        _save_message(sid, "assistant", result["answer"], _payload(result))
        log_qa(log, question, result, time.time() - t0)
        return result

    if verdict.mode == scope.GATE_THRESHOLD:
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
    if verdict.mode == scope.GATE_NEEDS_CONTEXT:
        ref_clauses = clauses[:5]
        cited = [c["clause_id"] for c in ref_clauses]
        ctx_notes = [
            f"这类问题需要结合您的系统情况逐条比对才能判断（{undet_reason}），"
            f"已直接说明并给出追问，未让大模型硬凑一份清单"
        ]
        if out_of_kb_names:
            # H-6：这个分支也点名了库外法规，声明必须一样拼在最前面
            ctx_notes.append(
                "用户点名了知识库未收录的法规：" + "、".join(out_of_kb_names)
            )
        result = {
            "session_id": sid,
            "answer": guard.ensure_disclaimer(
                scope.needs_context_reply(undet_reason, ref_clauses, out_of_kb_names)
            ),
            "citations": [guard.to_citation(c) for c in ref_clauses],
            "risks": guard.detect_risks(question, clauses, None, cited_ids=cited),
            "need_clarification": True,
            "followup_questions": APPLICABILITY_FOLLOWUPS,
            "found": True,
            "mode": "needs_context",
            "out_of_kb": out_of_kb_names,
            "notes": ctx_notes,
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

    # ---- 首屏：服务端先把条款清单拼好推给流式接口（不等模型）----
    # t28：模型不再写答案正文，正文由 app/answer.py 拼装。所以"第一段能显示的
    # 文字"完全不依赖模型，检索完就能发，首字延迟从模型的几秒降到毫秒级。
    if on_preface is not None:
        try:
            on_preface(answer_mod.render_preface(clauses))
        except Exception as exc:  # 首屏回调失败不能影响主流程
            log.debug("首屏回调失败：%s", exc)

    try:
        # 不再用流式解码：模型只返回结构化字段，没有可流式展示的正文。
        raw = llm.chat(messages, json_schema=prompt.ANSWER_SCHEMA)
        data = llm.parse_json_loose(raw)
        if data is None:
            mode = "retrieval_only"
            notes.append(
                "模型输出不是合法 JSON，已忽略其中的自由文本，改为直接列出检索到的条款"
            )
            data = {
                "found": True,
                "clauses": [],
                "risk_tags": [],
                "need_clarification": False,
                "followup_questions": [],
            }
    except llm.LLMUnavailable as exc:
        mode = "retrieval_only"
        log.warning("大模型不可用，降级为条款检索 · %s", exc)
        notes.append(str(exc))
        if exc.hint:
            notes.append(exc.hint)
        data = {
            "found": True,
            "clauses": [],
            "risk_tags": [],
            "need_clarification": False,
            "followup_questions": [],
        }

    # ---- 诚实优先：模型说「知识库中没找到」就采信它（P0-4）----
    # 旧实现反过来：不管模型说什么都置 found=True，再把模型说的
    # 「知识库中未找到」整句删掉、回填 5 条引用。那是把诚实改成不诚实——
    # 用户看到的是"已找到依据"，而模型当时说的是"我没找到"。
    model_found = bool(data.get("found", True))
    found = model_found
    if not found:
        notes.append("模型判断知识库中没有能回答这个问题的条款，已如实保留该结论（未回填引用）")

    # ---- 引用白名单校验（t28：引用由服务端从模型挑出的 clauses 派生）----
    valid_citations, invalid_ids = guard.validate_citations(data.get("clauses"), clauses)

    # ---- 兼容垫片：模型若仍按旧习惯返回已删除的字段 ----
    # t28 的 schema 里已经没有 answer / citations。但小模型可能照旧返回它们。
    # 这里**绝不把它们当正文或引用使用**（用户看到的正文永远由服务端拼装），
    # 只当作"模型想说什么"的信号：试图编编号 → 在正文里写引用核对；
    # 试图下等级结论 → 整条降级为 needs_context。见下面两处使用。
    legacy_answer = str(data.get("answer") or "")
    _, legacy_invalid = guard.validate_citations(data.get("citations"), clauses)
    if legacy_invalid:
        invalid_ids = list(
            dict.fromkeys([str(x) for x in invalid_ids] + [str(x) for x in legacy_invalid])
        )
    if invalid_ids:
        # note 也会显示在页面的「技术详情」里，所以这里的回显规则和正文一致：
        # 只显示长得像条款编号的字符串，其余一律降级为「若干编号」，
        # 否则模型把一句话塞进 clauses 就能从 notes 这条侧门进到用户眼前。
        shown_ids = [x for x in invalid_ids if _ID_LIKE.fullmatch(x)]
        notes.append(
            "已剔除不在本次检索结果中的条款编号："
            + ("、".join(shown_ids[:5]) if shown_ids else "若干编号")
            + "（模型只能从【可用条款】里挑）"
        )

    # ---- 答案正文：服务端按固定结构拼装，模型写的任何文字都不采用 ----
    # t29 之后 schema 里已经没有 answer / summary：概述由 answer.topic_line()
    # 从知识库的条款标题生成，正文里不会出现任何模型写的话。
    legacy_summary = str(data.get("summary") or "").strip()
    if legacy_summary:
        notes.append("模型写的一句话概述未被采用（概述由程序根据条款标题生成）")
        log.info("summary 未被采用 · %r", legacy_summary[:60])
    if found:
        answer_text = answer_mod.render(
            question,
            clauses,
            risk_tags=data.get("risk_tags") or [],
            mode=mode,
        )
    else:
        answer_text = prompt.NO_RESULT_ANSWER

    # 引用兜底：**只有当模型说找到了、却一个引用都没给时**才补检索到的条款。
    # 触发场景：大模型不可用、模型返回空内容、模型的 JSON 坏了导致 citations 全丢。
    # 两种情况下**故意不补**（P0-4/P0-5）：
    #   ① 模型自己说没找到 → 补了就等于伪造依据；
    #   ② 模型给了编号但全是编的 → 补 5 条真条款会让用户以为"我说的那条就在里面"。
    if found and not valid_citations and clauses and not invalid_ids:
        valid_citations = [guard.to_citation(c) for c in clauses[:5]]
        if mode == "llm":
            notes.append("模型未给出可用的引用，已按本次检索结果补充参考条款")

    # t28：正文已不含模型自由文本，编号只可能来自知识库，所以不再扫正文编造号；
    # 对不上的编号只可能来自模型挑的 clauses（已在上面剔进 invalid_ids）。
    # 但"编号对不上"仍要在**回答正文**里说清楚，而不是只写进前端折叠的技术详情：
    # 用户读到的是一段带编号的结论，他不会展开备注去发现编号对不上。
    # t29：invalid_ids 来自模型的 clauses 字段，是**不可信字符串**。
    # 旧实现把它们原样拼进正文，模型返回 clauses:["你们应当按三级建设。"]
    # 就能把一句话写进用户看到的答案。回显交给 _citation_warning() 收口。
    unmatched = [str(x) for x in invalid_ids]
    citation_warning = _citation_warning(unmatched)

    # ---- 库外法规：模型"试图"写了，就由服务端声明 ----
    # H-2：问题里没点名、但**模型自己在回答里写了**库外法规时，同样要声明。
    #
    # 这里只扫**模型写的文本**（legacy_answer），绝不扫 answer_text。
    # 结构性改造之后正文 100% 由 answer.render() 拼装，里面只有知识库原文与
    # 服务端常量。拿正文去跑 scope.detect_out_of_kb 会把知识库自己的句子当成
    # 「模型转述」删掉——实测 8.1.4.28 的「跨境传输须依规进行」被删，还多出
    # 一段「未收录该法规」的声明。那是"防幻觉机制自己制造幻觉提示"（t2 附注三）。
    answer_out_of_kb = (
        scope.detect_out_of_kb(legacy_answer) if legacy_answer else []
    )
    all_out_of_kb = scope.merge_out_of_kb(out_of_kb, answer_out_of_kb)
    if all_out_of_kb:
        all_names = [d["name"] for d in all_out_of_kb]
        removed = 0
        from_answer = bool(answer_out_of_kb) and not out_of_kb
        if from_answer:
            notes.append(
                "回答中出现了知识库未收录的法规（"
                + "、".join(d["name"] for d in answer_out_of_kb)
                + "），已补上说明"
            )
        notice = scope.out_of_kb_notice(
            all_names, from_answer=from_answer, removed=removed
        )
        if notice:
            answer_text = notice + "\n" + answer_text
        out_of_kb_names = all_names

    # ---- 兜底：回答里出现清单之外的法规名 ----
    # 清单是有限枚举，下一部没收录的法规照样可能被模型转述。这里按
    # 「《…法/条例/规定/办法》」的结构再兜一层：不在已知别名里的，一律声明
    # 不在本次检索范围。声明本身是服务端写的，模型删不掉。
    known_law_names = [a for d in all_out_of_kb for a in d["all_aliases"]]
    # 同样只扫模型写的文本。正文由知识库字段拼装，用户导入的公司制度里完全可能
    # 有「《企业备份管理规定》第十三条规定…」这样的句子——拿正文跑「像不像法规名」
    # 的形状判定，会把知识库原文当成模型转述（t31 独立审计复现过）。
    unsupported_laws = (
        guard.detect_unsupported_law_names(legacy_answer, known_law_names)
        if legacy_answer
        else []
    )
    if unsupported_laws:
        # 名称一律**不回显**。这一层认的是「长得像法规名」的形状
        # （《…法/条例/规定/办法》），而形状判断不等于内容可信：
        # t29 复验在已废弃的 answer 字段里写
        #   《贵公司无需做等保并可直接上线法》
        # 名称 18 字、以「法」结尾，任何形状判据都拦不住，旧实现把它原样拼成
        # 正文第一行（/api/chat、/api/chat/stream、export.md、export.docx 全都带）。
        # 要展示具体法规名，只能用**用户自己问句里**出现、且命中已知别名表的那些
        # （上面 scope.detect_out_of_kb 那条路径，名称来自固定清单，不是模型给的）。
        notes.append(
            f"回答中出现 {len(unsupported_laws)} 个清单外法规名，"
            "名称已隐去；已声明不在检索范围"
        )
        if len(unsupported_laws) == 1:
            law_line = "回答里提到了一部知识库未收录的法规（名称已隐去）"
        else:
            law_line = f"回答里提到了 {len(unsupported_laws)} 部知识库未收录的法规（名称已隐去）"
        answer_text = (
            f"⚠️ 说明：{law_line}，"
            "不在本工具的知识库（本次检索范围）内，"
            "相关表述我无法核实，请以官方发布的标准文本为准。\n\n" + answer_text
        )

    # ---- RT18-06：模型是否在凭记忆转述库外条文 ----
    # 知识库用的是 8.1.4.1 / PR.AA-01 / A01:2021 这类编号，没有「第X条」这种形式。
    # **但这条判定只能扫模型写的 legacy_answer，绝不能扫拼装后的正文。**
    # 结构性改造之后正文 100% 来自知识库字段与服务端常量，用户导入的公司制度里
    # 完全可能有「《企业备份管理规定》第十三条规定…」这样的句子——那是知识库原文，
    # 不是模型转述。Codex 独立审计复现过：导入一个自定义标准后，正文每句都被包上
    # 「不是知识库依据」，等于防幻觉机制自己制造了幻觉提示。
    # 模型写的文本本来也进不了正文，所以这里只记一条技术备注。
    _, unverified_clauses = guard.annotate_unverified_clause_claims(legacy_answer)
    if unverified_clauses:
        notes.append(
            f"模型这次试着凭记忆转述 {unverified_clauses} 处「第X条」式条文，已丢弃（正文由知识库拼装）"
        )

    # ---- 是否要找用户补充信息 ----
    need_clarify = bool(data.get("need_clarification"))
    forced_clarify_notice = ""
    model_followups = [
        str(q).strip() for q in (data.get("followup_questions") or []) if str(q).strip()
    ]
    # 追问句一律由服务端给，模型写的一句都不采用。
    # 判据与 answer / summary 相同：用户能读到的每一句自然语言，要么来自知识库字段，
    # 要么来自服务端常量。t30 红队演示过模型在 followup_questions 里塞
    # 「你们应当按三级建设。」——前端会把它渲染成"要追问你的问题"，用户照单全收。
    followups: list[str] = list(scope.CLARIFY_FOLLOWUPS) if need_clarify else []
    if model_followups:
        notes.append("模型建议的追问未被采用（追问由服务端固定清单提供）")
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

    # ---- ⚠️ 风险 ----
    risks = guard.detect_risks(
        question, clauses, data.get("risk_tags"),
        cited_ids=[c["clause_id"] for c in valid_citations],
    )

    # ---- 兜底：模型（在已废弃的 answer 字段里）替用户下了等级结论 ----
    # t28 之后正文里不可能有模型写的结论句，所以这里不再对正文做句子级手术
    # （那正是第六轮露馅的地方：误删知识句、漏掉无主语句）。
    # 但模型"试图"下结论这件事仍是一个信号：整条回答降级为 needs_context，
    # 并在正文末尾补一句服务端写的中性说明。guard.strip_level_conclusions
    # 作为第二层防御保留（单测仍在跑它），只是主路径不再依赖它删句。
    _, level_conclusion = guard.strip_level_conclusions(legacy_answer)
    if level_conclusion:
        mode = "needs_context"
        found = False
        need_clarify = True
        if not followups:
            followups = list(scope.CLARIFY_FOLLOWUPS)
        answer_text = answer_text + "\n\n" + guard.LEVEL_CONCLUSION_NOTICE
        notes.append("模型的回答里出现了等级结论，已丢弃并降级为「需要补充信息」")

    answer_text = guard.ensure_disclaimer(answer_text)
    # 引用对不上时，核对说明放在**正文最前面**，而不是只写进前端折叠的技术详情：
    # 用户读到的是一段带编号的结论，他不会展开备注去发现编号是编的。
    if citation_warning:
        answer_text = citation_warning + "\n\n" + answer_text
    # 由服务端补的追问提示放在最前面（放在最后拼，避免被上面两步改坏）
    if forced_clarify_notice:
        answer_text = forced_clarify_notice + "\n" + answer_text

    # 引用必须与正文一致。正文渲染的是**本次检索到的全部条款**（answer_mod.render 用的
    # 就是 clauses），而 valid_citations 只有模型点名的那几条——旧行为会出现
    # 「正文 6 个条款块、引用卡片 2 张」，读者会以为另外 4 条没有依据。
    # 以正文为准：正文列了什么，引用就列什么。
    # 例外：模型编了条款编号时（invalid_ids 非空）仍然不回填——P0-5 的结论不变，
    # 那种情况下正文最前面已经写明「你提的编号不在检索结果中」，
    # 此时再补一串真条款会让用户以为"我说的那条就在里面"。
    shown_citations = (
        [guard.to_citation(c) for c in clauses]
        if (found and clauses and not invalid_ids)
        else valid_citations
    )

    result = {
        "session_id": sid,
        "answer": answer_text,
        "citations": shown_citations,
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
    preface_sent = {"done": False}

    def on_preface(text: str) -> None:
        """首屏：服务端拼好的条款清单，检索完就能发，不用等模型。

        t28 起模型不再写正文，所以这段"开场 + 概述 + 全部条款块"是稳定的、可先发的；
        模型返回后正文已经完整，只剩下风险汇总与免责收尾由服务端补齐。
        作为 delta 发出去，是为了复用前端已有的"前缀一致就续写"逻辑。
        """
        if not text or preface_sent["done"]:
            return
        preface_sent["done"] = True
        streamed["text"] += text
        events.put(("delta", text))

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
                question, sid, on_delta=on_delta, on_stage=on_stage,
                on_preface=on_preface,
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
