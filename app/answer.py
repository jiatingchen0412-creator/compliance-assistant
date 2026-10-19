"""服务端拼装答案正文（t28 结构性改造）。

为什么要有这个模块
------------------
前六轮都在回答侧「按句子判断主语」：删掉模型写的等级结论句。第六轮红队用
23 条没人见过的句子证明这条路不收敛——漏删 8 条、误删 4 条。漏网总落在三类：
无主语/省略句、语序变体、谓词词表。第三类靠加词永远补不完。

所以这一轮不再做句子级手术，而是把「模型写面向用户的答案正文」这条路去掉：

  - 模型只输出 found / clauses / summary / risk_tags / followup_questions /
    need_clarification（见 prompt.ANSWER_SCHEMA）；
  - 答案正文由本模块按**固定结构**拼装，条款内容直接取自知识库；
  - 模型唯一的自由文本是 ≤120 字的 summary，用「整体拒绝」过滤：
    命任一条就把整条 summary 丢掉，条款清单与收尾照常显示。

这样漏网的代价从「用户读到错误结论」降为「少看到一句概述」——
结构上不可能再出现"模型替用户下结论"的正文。
"""
from __future__ import annotations

import re

from . import normalize, prompt, scope

# summary 的长度上限（契约要求 ≤120 字）。超长直接丢弃而不是截断：
# 截断会把一句完整的话切成半句，反而更容易产生歧义。
MAX_SUMMARY_CHARS = 120

# 条款正文在答案里的展示长度。知识库里的原文字段可能很长（原文几百字），
# 用户在自查页面上需要的是"这条讲什么"，太长会淹没条款编号。
MAX_CLAUSE_CHARS = 220

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

# ---- summary 的「整体拒绝」判据 -------------------------------------------
# 第二人称：只要指向用户，就说明这句话是在对"您的情况"下判断，不是概述条款。
_SECOND_PERSON = re.compile(r"(您|你|贵司|贵公司|贵单位|咱们)")

# 判定 / 许可词：这些词出现在概述里，几乎一定是在替用户拍板或给许可。
_JUDGE_WORDS = re.compile(
    r"(应当|应该|建议|必须|需要|可以|不需要|不用|无需|免于|免除|适合|合适|"
    r"足够|没必要|放一放|缓一缓|先不|划算|够)"
)

# 疑问句不算概述（概述应当是一句陈述）。
_QUESTION_MARK = re.compile(r"[？?]")

# 许可 / 免除 / 后果类结论：与"等级结论"同级——都是在替用户拍板。
# 例："等保可以先放一放。" "等保不做也不会被处罚。" "这类情况通常不需要。"
# 这些句子不一定是等级，但同样是在回答"我到底要不要做"，属于 P0 的诚实性问题。
_PERMISSION_TAIL = re.compile(
    r"(不做|不用做|不需要做|可以不|免于|免除|跳过|省掉|先不|缓一缓|放一放)"
    r"[^。！？!?]{0,10}(也行|也可以|也可以不|即可|就行|没事|没关系|不影响|不会|一样)",
    re.I,
)
_PENALTY_TAIL = re.compile(
    r"(不会|不会被|不会受|不会被|没人|不用怕)[^。！？!?]{0,4}(处罚|罚款|追究|查|管|罚)",
    re.I,
)


# ---- summary 必须与所引条款「有交集」（接地判据） --------------------------
# 词表判据挡得住「替用户拍板」，挡不住「空话」：
#   「这属于常规做法，不必额外投入。」「保持现状即可。」
# 这类句子既没指向用户、也没出现等级，但与被引条款一个字都对不上——它是
# 模型凭通用常识写的，不是对条款的概述。再加「不必 / 即可」两个词只能等
# 下一个同义词（「也无妨」「也行」）。
#
# 所以这里换成一条接地判据：summary 至少要命中一个来自被引条款
# （title / text / summary）的实词 token；一个都命中不了就整条丢弃。
# 这样"空话"与"通用建议"由同一条规则挡掉，与它用哪个谓语无关。
#
# 停用词只收「功能词 + 几乎任何条款都有的泛词」，否则任意一句话都能靠
# 「要求 / 系统 / 进行」这些词蹭出交集。注意「安全审计」这类复合词是 jieba
# 的一个整 token，不会被 stopwords 拆开。
_STOPWORDS = frozenset(
    """
要求 应当 应该 必须 需要 可以 可能 相关 有关 相应 对应 上述 如下 其他 其它
企业 单位 组织 公司 系统 平台 业务 进行 开展 通过 以及 或者 并且 而且 但是
因为 所以 如果 已经 目前 现在 一般 通常 主要 包括 包含 各类 各种 方面 情况
内容 工作 措施 办法 方式 方法 技术 管理 安全 信息 数据 服务 功能 能力 过程
这个 那个 这些 那些 一个 一种 一些 什么 如何 怎么 是否 我们 你们 他们 它们
对于 关于 根据 按照 同时 此外 因此 从而 就是 还是 也是 属于 成为 作为 实现
保证 确保 提高 提升 加强 完善 建立 建设 提供 支持 能够 不能 不会 没有
""".split()
)


def _content_tokens(text: str) -> set[str]:
    """抽出「能当证据」的实词 token：≥2 字、非停用词、非纯数字。"""
    tokens: set[str] = set()
    for tok in normalize.tokenize(text or "").split():
        tok = tok.strip()
        if len(tok) < 2 or tok in _STOPWORDS:
            continue
        if not re.search(r"[\w\u4e00-\u9fff]", tok):
            continue
        if re.fullmatch(r"[\d.]+", tok):
            continue
        tokens.add(tok)
    return tokens


def grounding_hit(summary: str, clauses: list[dict] | None) -> str | None:
    """summary 与被引条款的实词交集；返回命中的第一个 token，None 表示无交集。

    没有条款可对照时返回 "__no_clauses__"，调用方据此**跳过**接地判据
    （而不是把 summary 一律判死）：单测里只测词表判据时会这样用。
    """
    if not clauses:
        return "__no_clauses__"
    corpus_parts: list[str] = []
    for clause in clauses:
        corpus_parts.append(str(clause.get("title") or ""))
        corpus_parts.append(str(clause.get("text") or ""))
        corpus_parts.append(str(clause.get("summary") or ""))
    clause_tokens = _content_tokens(" ".join(corpus_parts))
    common = _content_tokens(summary) & clause_tokens
    if not common:
        return None
    return sorted(common)[0]


def summary_reject_reason(
    summary: str | None, clauses: list[dict] | None = None
) -> str | None:
    """判断一条 summary 该不该丢弃：返回理由，None 表示可以展示。

    判据全部是「闭集 + 结构」，不针对某句话加词：
      1. 空 / 超长
      2. 含等级、档位写法（复用 scope 的闭集锚点）
      3. 含第二人称（指向用户）
      4. 含判定/许可词
      5. 命中价格/机构词表（复用 scope.detect_no_kb_topic 的同一张表）
      6. 疑问句
      7. 与被引条款没有任何实词交集（接地判据：空话 / 通用建议）
    """
    text = (summary or "").strip()
    if not text:
        return "空"
    if len(text) > MAX_SUMMARY_CHARS:
        return f"超过 {MAX_SUMMARY_CHARS} 字"
    # 与问题侧共用同一套归一化：全角/繁简/零宽字符先抹平，再判闭集。
    probe = scope.normalize_for_match(text)
    if scope._LEVEL_ANCHOR.search(probe):
        return "含等级写法"
    if scope._TIER_WORD.search(probe):
        return "含档位写法"
    if _SECOND_PERSON.search(probe):
        return "含第二人称"
    if _JUDGE_WORDS.search(probe):
        return "含判定/许可词"
    if _PERMISSION_TAIL.search(probe) or _PENALTY_TAIL.search(probe):
        return "含许可/免除结论"
    if scope.detect_no_kb_topic(probe):
        return "含价格/机构表述"
    if _QUESTION_MARK.search(text):
        return "疑问句"
    # 接地判据放最后：前面几条是"这句话在拍板"，这条是"这句话在说空话"。
    if grounding_hit(text, clauses) is None:
        return "与所引条款没有交集"
    return None


def filter_summary(
    summary: str | None, clauses: list[dict] | None = None
) -> tuple[str | None, str | None]:
    """返回 (保留的 summary 或 None, 丢弃理由或 None)。

    clauses 用来做接地判定；传 None 表示"调用方没有条款可对照"，此时跳过
    接地判据（仍会跑词表判据）。生产路径必须传，否则空话会漏过去。
    """
    reason = summary_reject_reason(summary, clauses)
    if reason:
        return None, reason
    return (summary or "").strip(), None


def _clause_body(clause: dict) -> str:
    body = (clause.get("summary") or clause.get("text") or "").strip()
    body = re.sub(r"\s+", " ", body)
    if len(body) > MAX_CLAUSE_CHARS:
        body = body[:MAX_CLAUSE_CHARS].rstrip() + "…"
    return body


def _risk_line(tags: list[str]) -> str:
    labels = [prompt.RISK_LABELS.get(t, t) for t in tags if t]
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
    """开场 + 全部条款块（不含 summary 与收尾）。

    流式接口先用它把首屏推给前端：这一段完全不依赖模型，检索完就能发。
    """
    opener = OPENERS.get(mode, DEFAULT_OPENER)
    blocks = [render_clause_block(c) for c in (clauses or [])]
    return "\n\n".join([opener] + [b for b in blocks if b])


def render(
    question: str,
    clauses: list[dict],
    summary: str | None = None,
    risk_tags: list[str] | None = None,
    mode: str = "llm",
) -> str:
    """按固定结构拼出答案正文。

    顺序固定：开场 → 条款块（全部）→（可选，且必须通过过滤）一句话概述 →
    风险汇总 → 固定收尾。

    注意：question 只用于日志/未来扩展，**绝不会**把模型返回的自由文本
    拼进正文；调用方也不应该把模型写的 answer 字段传进来。
    """
    parts: list[str] = [render_preface(clauses, mode)]

    kept, _reason = filter_summary(summary, clauses)
    if kept:
        parts.append("一句话概述：" + kept)

    risk = _risk_line(list(risk_tags or []))
    if risk:
        parts.append(risk)

    parts.append(CLOSING)
    return "\n\n".join(p for p in parts if p)
