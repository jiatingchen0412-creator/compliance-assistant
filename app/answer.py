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

# 固定收尾：免责 + 适用性归用户自己判断 + 如实说明知识库覆盖范围。
# 这几句是服务端写的，模型删不掉，也不依赖模型是否听话。
#
# 为什么要写覆盖范围（2026-10-21）：两次独立盲出题（2/6、1/6）证明，
# 「用户问到工具不覆盖的领域」这件事**没法用领域词表挡住**——想到一个领域就加
# 一批词，只是在拟合出题人自己想到过的那几个领域。所以我们不再假装能拦住它，
# 而是把真实边界写在每条答案上：用户问的是商标、劳动仲裁、物业时，
# 他至少能看到"这个工具只覆盖这三个标准"。这是披露，不是分类器——
# 分类器有误判，披露没有。
SCOPE_STATEMENT = (
    "本工具的知识库只覆盖 等保2.0（GB/T 22239-2019）/ NIST CSF 2.0 / OWASP Top 10 三类标准；"
    "人事与劳动、财税、工商与资质、外贸、物业、教育、殡葬等其他领域的问题不在覆盖范围内，"
    "请以对应主管部门的规定或专业顾问的意见为准。"
)
CLOSING = (
    "以上条款是否适用于您的系统，需要结合业务性质、数据规模和影响面自行判断。\n"
    + SCOPE_STATEMENT
    + "\n本回答仅用于辅助自查，不构成正式测评结论。"
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
    """把一条条款压缩成一段给用户看的正文。

    2026-10-21 改：优先用 `text`，不再优先用 `summary`。
    `summary` 是编辑时写死的一句话（平均 47 字，283 条里 130 条以「…」结尾），
    用户问"日志要保存多久"时，答案里只有"日志只留几天没法取证"，
    而 `text` 里明明白白写着「留存期限（如不少于六个月）」。知识库知道答案、
    渲染层却把它丢了，这不是模型的错。

    NIST 的 `text` 是「英文原文 + 中文翻译」，英文那段对用户没用还占字数，
    所以只取「中文翻译：」之后的部分。
    """
    raw = str(clause.get("text") or "").strip()
    if not raw:
        raw = str(clause.get("summary") or "").strip()
    marker = "中文翻译："
    if marker in raw:
        translated = raw.split(marker, 1)[1].strip()
        if translated:
            raw = translated
    body = re.sub(r"\s+", " ", raw)
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


def _clause_risk_tags(clauses: list[dict]) -> set[str]:
    """本次渲染出来的条款，自己带哪些风险标签。

    ⚠️ 标签必须**有条款撑着**。t2 的人工标注发现：模型对 20 道题里的 12 道
    聚合出同一串「数据加密、访问控制、个人信息保护」，而其中 5 道题的 6 条条款
    一条都不带这三个标签；q37/q38 还与题库的 `risk_forbid` 直接冲突。
    模型能自由写的标签，等于让它在正文里加一句无依据的结论——所以这里
    改成「模型说的标签，只有条款自己也这么标才显示」。
    """
    out: set[str] = set()
    for clause in clauses or []:
        tags = clause.get("risk_tags") or []
        if isinstance(tags, str):
            tags = [tags]
        for tag in tags:
            text = str(tag).strip()
            if text:
                out.add(text)
    return out


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

    `risk_tags` 是模型给的，但**只用来收窄，不能用来新增**：只有条款自己也带了
    这个标签，才显示（见 `_clause_risk_tags`）。
    """
    parts: list[str] = [render_preface(clauses, mode)]

    supported = _clause_risk_tags(clauses)
    asked = [str(t).strip() for t in (risk_tags or []) if str(t).strip()]
    risk = _risk_line([t for t in asked if t in supported])
    if risk:
        parts.append(risk)

    parts.append(CLOSING)
    return "\n\n".join(p for p in parts if p)


# --------------------------------------------------------------------------
# 组装一致性检查：把「模型不写正文」从举例验证变成逐行穷举
# --------------------------------------------------------------------------
# 上面这一整条链的承诺是：**用户读到的每一个字，要么来自知识库字段，要么来自
# 服务端常量**。在 t28 之前这只是设计意图，靠 `test_answer_render.py` 的若干条
# 用例举例验证；而 t29/t30 两次红队都证明「举例」挡不住新写法。
#
# 所以这里给一个**逐行穷举**的判据：把答案按行拆开，要求每一行都能追到
# 知识库（条款编号/标题/正文/概述）或服务端（开场/收尾/风险标签/各类声明），
# 追不到就返回那一行。调用方（单测、评审工具）只要断言返回值为空即可。
#
# 这个函数**不在生产请求路径上**——它不产生用户可见的内容，只用于验证。
# 特意放在本模块而不是测试里，是因为它必须与 `render()` 共用同一套常量，
# 否则两边一漂移，检查就会失效。

# 服务端自己写的那几类行。早期这里是**前缀**白名单（startswith），等于
# 「只要以「⚠️ 说明：」开头，后面写什么都放行」。Codex 独立审计用一行注入
# 证明过：给 render() 后面追加「⚠️ 说明：贵司属于三级，按三级建设即可。」，
# 18 条组装一致性用例仍然全绿。**前缀不是判据，模板才是。**
#
# 所以改成「模板 + 受约束的变量」：整行必须匹配某个模板，且变量部分不得出现
# 等级/档位写法——服务端声明里从来不含这些，含了就是被塞进来的。
_RISK_LABEL_WORDS = tuple(prompt.RISK_LABELS.values())
_LEVEL_BAN = re.compile(r"级|級|[Ll]v?\s*-?\s*\d|Level\s*-?\s*\d|档|阶")
_ID_TOKEN = r"[A-Za-z0-9][A-Za-z0-9._:\-]{1,31}"

# (模板, 需要额外检查的变量组名)；组名为 None 表示整行固定。
_SERVER_LINE_SPECS: tuple[tuple[re.Pattern[str], str | None], ...] = (
    # scope.out_of_kb_notice()：⚠️ 补充说明：本次回答提到了《X》，但该法规不在…
    (
        re.compile(
            r"^⚠️ 补充说明：本次回答提到了 (?P<var>.{2,80})，"
            r"但该法规不在本工具的知识库范围内（未收录）。$"
        ),
        "var",
    ),
    # scope.out_of_kb_notice() 的另一支：⚠️ 先说明一点：您提到的 X 不在…
    (
        re.compile(r"^⚠️ 先说明一点：您提到的 (?P<var>.{2,80}) 不在本工具的知识库范围内。$"),
        "var",
    ),
    # chat.py 里清单外法规名的固定文案（名称已隐去，没有变量）
    (
        re.compile(
            r"^⚠️ 说明：(?P<var>.{2,80})，不在本工具的知识库（本次检索范围）内，"
            r"相关表述我无法核实，请以官方发布的标准文本为准。$"
        ),
        "var",
    ),
    # chat._citation_warning()：只回显编号形状，取不到就降级成「若干编号」
    (
        re.compile(
            r"^⚠️ 引用核对：回答里提到的 (?P<var>"
            + _ID_TOKEN
            + r"(?:、"
            + _ID_TOKEN
            + r")*|若干编号) 不在本次检索到的条款中[^\n]*$"
        ),
        "var",
    ),
)


def _risk_line_ok(line: str) -> bool:
    """「风险标签：⚠️ 数据加密、访问控制」——每个标签都必须在闭集里。"""
    prefix = "风险标签：⚠️ "
    if not line.startswith(prefix):
        return False
    parts = line[len(prefix):].split("、")
    return all(part in _RISK_LABEL_WORDS for part in parts)


def _server_line_ok(line: str) -> bool:
    """这一行是否真的是服务端自己写的（按模板校验，不是按前缀放行）。"""
    if _risk_line_ok(line):
        return True
    for pattern, group in _SERVER_LINE_SPECS:
        match = pattern.match(line)
        if not match:
            continue
        if group is None:
            return True
        return not _LEVEL_BAN.search(match.group(group) or "")
    return False


def _norm_line(text: object) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _server_constant_lines() -> set[str]:
    """服务端常量能整句比对的那些行（开场、收尾、无结果、追问提示、免责兜底）。"""
    from . import guard, scope  # 延迟导入：避免与 scope -> answer 的潜在循环

    texts = [
        *OPENERS.values(),
        DEFAULT_OPENER,
        CLOSING,
        prompt.NO_RESULT_ANSWER,
        scope.clarify_notice(),
        guard.ensure_disclaimer("x"),
        guard.LEVEL_CONCLUSION_NOTICE,
    ]
    lines: set[str] = set()
    for text in texts:
        for raw in str(text).split("\n"):
            line = _norm_line(raw)
            if line:
                lines.add(line)
    return lines


def unaccounted_lines(text: str, clauses: list[dict] | None) -> list[str]:
    """返回答案里既不是知识库字段、也不是服务端常量的行。

    空列表 = 组装一致性成立。
    """
    allowed = _server_constant_lines()

    topic = _norm_line(topic_line(clauses or []))
    if topic:
        allowed.add(topic)

    heads: set[str] = set()
    bodies: list[str] = []
    for clause in clauses or []:
        cid = str(clause.get("clause_id") or "").strip()
        title = str(clause.get("title") or "").strip()
        if cid or title:
            heads.add(_norm_line(f"**{cid} {title}**"))
        body = _norm_line(_clause_body(clause)).rstrip("…")
        if body:
            bodies.append(body)

    bad: list[str] = []
    for raw in str(text or "").split("\n"):
        line = _norm_line(raw)
        if not line or line in allowed:
            continue
        if _server_line_ok(line):
            continue
        if line in heads:
            continue
        if any(body.startswith(line.rstrip("…")) for body in bodies):
            continue
        bad.append(line)
    return bad
