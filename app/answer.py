"""服务端拼装答案正文。

为什么要有这个模块
------------------
前六轮都在回答侧「按句子判断主语」：删掉模型写的等级结论句。第六轮红队用
23 条没人见过的句子证明这条路不收敛——漏删 8 条、误删 4 条。漏网总落在三类：
无主语/省略句、语序变体、谓词词表。第三类靠加词永远补不完。

所以第七轮不再做句子级手术，而是把「模型写面向用户的答案正文」这条路去掉：

  - 模型只输出 found / clauses / risk_tags / followup_questions / need_clarification；
  - 答案正文由本模块按**固定结构**拼装，条款内容与"一句话概述"都来自知识库；
  - 正文里出现的每一个字，要么来自知识库字段，要么来自本文件的常量。

t29 红队把最后一段自由文本也打掉了
----------------------------------
第七轮最初还保留了一个 ≤120 字的 `summary`，用词表 + 接地判据整条拒绝。t29 复验
找到三条路都能让模型的句子进正文，其中两条不需要词表漏洞：

  1. `risk_tags` 是模型自由文本，旧实现 `RISK_LABELS.get(t, t)` 查不到就原样拼接；
  2. 接地判据是「任意一个实词命中」，条款写「应定期开展等级测评」，概述写
     「测评跳过无妨。」——仅凭「测评」两个字就放行，正文里出现与条款正文
     自相矛盾的结论；
  3. 非法 clause 编号被"引用核对"提示原样回显。

第 1、3 条是拼接口子，已收口（枚举标签、只回显编号形状的字符串）。第 2 条不是
口子而是方向问题：只要允许模型写一句面向用户的话，就得回答"这句话可不可信"，
而这个问题没法用字符串判据收敛。所以直接取消 `summary`：概述改由本模块从
知识库的条款标题生成，模型不再产生任何面向用户的自然语言。

这样"模型替用户下结论"在结构上不可能发生，代价是概述不再是一句人写的总结。
"""
from __future__ import annotations

import re

from . import prompt

# 条款正文在答案里的展示长度。知识库里的原文字段可能很长（原文几百字），
# 用户在自查页面上需要的是"这条讲什么"，太长会淹没条款编号。
MAX_CLAUSE_CHARS = 220

# 概述里最多列几个条款标题（再多就变成把目录抄一遍）。
MAX_TOPIC_TITLES = 4

# 开场白按模式区分：用户要知道"这些字是谁写的、为什么只有条款"。
OPENERS = {
    "llm": "根据您描述的情况，知识库里与之相关的条款是：",
    "retrieval_only": "（当前无法调用本地大模型，下面直接为您列出知识库中检索到的相关条款）",
    "parse_failed": "（模型这次没有按要求的格式返回，已忽略它写的任何文字）以下是知识库中检索到的相关条款：",
}
DEFAULT_OPENER = "以下是知识库中检索到的相关条款："

# 固定收尾：免责 + 适用性归用户自己判断。
# 这两句是服务端写的，模型删不掉，也不依赖模型是否听话。
CLOSING = (
    "以上条款是否适用于您的系统，需要结合业务性质、数据规模和影响面自行判断。\n"
    "本回答仅用于辅助自查，不构成正式测评结论。"
)


def topic_line(clauses: list[dict]) -> str:
    """从知识库的条款标题生成一句话概述（不含任何模型文本）。

    内容只取决于"这次检索到了哪几条"，所以它不可能与条款正文矛盾。
    """
    titles: list[str] = []
    for clause in clauses or []:
        title = str(clause.get("title") or "").strip()
        if title and title not in titles:
            titles.append(title)
    if not titles:
        return ""
    shown = titles[:MAX_TOPIC_TITLES]
    tail = "等" if len(titles) > MAX_TOPIC_TITLES else ""
    return "本次相关条款集中在：" + "、".join(shown) + tail + "。"


def _clause_body(clause: dict) -> str:
    body = (clause.get("summary") or clause.get("text") or "").strip()
    body = re.sub(r"\s+", " ", body)
    if len(body) > MAX_CLAUSE_CHARS:
        body = body[:MAX_CLAUSE_CHARS].rstrip() + "…"
    return body


def _risk_line(tags: list[str]) -> str:
    """风险标签只接受闭集里的键，查不到的一律丢弃。

    t29 红队证明过：旧实现是 `RISK_LABELS.get(t, t)`，模型传
    `["贵司属于三级", "<b>不做等保也行</b>"]` 会被原样拼进正文。
    schema 里的 enum 只是"请求模型照做"，服务端必须自己兜住。
    """
    labels: list[str] = []
    for tag in tags or []:
        label = prompt.RISK_LABELS.get(str(tag).strip())
        if label and label not in labels:
            labels.append(label)
    if not labels:
        return ""
    return "风险标签：⚠️ " + "、".join(labels)


def render_clause_block(clause: dict) -> str:
    """一条条款 = 编号+标题 / 正文摘要（来自知识库）/ 风险标签。"""
    cid = str(clause.get("clause_id") or "").strip()
    title = str(clause.get("title") or "").strip()
    head = f"**{cid} {title}**".strip()
    lines = [head]
    body = _clause_body(clause)
    if body:
        lines.append(body)
    risk = _risk_line(clause.get("risk_tags") or [])
    if risk:
        lines.append(risk)
    return "\n".join(lines)


def render_preface(clauses: list[dict], mode: str = "llm") -> str:
    """开场 + 概述 + 全部条款块（不含收尾）。

    流式接口先用它把首屏推给前端：这一段完全不依赖模型，检索完就能发。
    """
    opener = OPENERS.get(mode, DEFAULT_OPENER)
    parts = [opener, topic_line(clauses)]
    parts += [render_clause_block(c) for c in (clauses or [])]
    return "\n\n".join([p for p in parts if p])


def render(
    question: str,
    clauses: list[dict],
    risk_tags: list[str] | None = None,
    mode: str = "llm",
) -> str:
    """按固定结构拼出答案正文。

    顺序固定：开场 → 一句话概述（来自知识库标题）→ 条款块（全部）→ 风险汇总
    → 固定收尾。

    注意：question 只用于日志/未来扩展，**绝不会**把模型返回的自由文本
    拼进正文；调用方也不应该把模型写的 answer 或 summary 传进来——
    schema 里已经没有这两个字段了。
    """
    parts: list[str] = [render_preface(clauses, mode)]

    risk = _risk_line(list(risk_tags or []))
    if risk:
        parts.append(risk)

    parts.append(CLOSING)
    return "\n\n".join(p for p in parts if p)
