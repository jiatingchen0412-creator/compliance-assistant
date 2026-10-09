"""提示词工程：把所有硬性规则写进系统提示词。

六条不可违反的规则（对应用户的原始需求 + 实测踩过的坑）。
下面的编号与正文里的【规则N】一一对应，改编号时两边都要改：

1. 引用只能来自【可用条款】，正文里不许出现别的条款编号。
2. 照着【可用条款】回答，不许自由发挥。
   注意：这一条曾经被"矫枉过正"过——为了修"检索到了却说找不到"，
   旧版规则2 写成了"绝对不许说找不到"，结果连"用户问的是库外法规"
   这种真正该拒答的情形也被一并封死了。现在的写法把两件事分开：
   "库里有没有"只看【可用条款】，但"能不能转述库外法规的内容"是另一回事。
2b. 库外法规只说明"未收录"，绝不转述它的条文内容
   （服务端 `guard.strip_out_of_kb_claims` 还会再兜一道，见 guard.py）。
3. 数据加密 / 访问控制 / 个人信息保护 → 必须打上 ⚠️ 风险标签。
4. 用户信息不足 → 先追问，不要硬给结论。
   （7B 模型经常漏做，所以 `scope.needs_clarification` 会在服务端补位——
   见 scope.py 里那段注释。）
5. 适用性判定（哪些条款不适用）→ 不硬答，说明需要结合系统具体情况。
   这类问题在 chat.py 里**根本不调模型**，直接走服务端模板。

**提示词只是"请它别这么干"；真正的保证都在代码里。**
凡是"答错了会严重误导用户"的行为（编造条文、替用户做适用性判定、
把笼统问题当成已知情况回答），都必须由服务端代码拍板，
因为那些保证才能被离线测试证明（见 tests/test_scope.py、tests/test_guard.py）。

另外用一个 JSON Schema 约束输出格式：这样程序能可靠地拿到
"引用了哪些条款""有没有风险""要不要追问"这些结构化信息。
条款编号让模型以**纯字符串数组**返回（不要嵌套对象），
实测这个格式对 7B 级别的小模型最友好、最不容易写崩。
"""
from __future__ import annotations

# --------------------------------------------------------------------------
# 结构化输出 Schema（Ollama 的 format 参数，走 JSON Schema 约束解码）
# --------------------------------------------------------------------------
ANSWER_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "answer": {
            "type": "string",
            "description": "给用户看的正文回答，中文，大白话，分点说明",
        },
        "citations": {
            "type": "array",
            "description": "回答所依据的条款编号，只能取自【可用条款】中出现的编号",
            "items": {"type": "string"},
        },
        "risk_tags": {
            "type": "array",
            "description": "命中高风险主题时填写，可选值：encryption、access_control、personal_info",
            "items": {
                "type": "string",
                "enum": ["encryption", "access_control", "personal_info"],
            },
        },
        "need_clarification": {
            "type": "boolean",
            "description": "用户描述信息不足、需要先追问时为 true",
        },
        "followup_questions": {
            "type": "array",
            "description": "需要追问时，列出 1-3 个具体问题",
            "items": {"type": "string"},
        },
        "found": {
            "type": "boolean",
            "description": "知识库中找到了能回答该问题的条款为 true；完全找不到为 false",
        },
    },
    "required": [
        "answer",
        "citations",
        "risk_tags",
        "need_clarification",
        "followup_questions",
        "found",
    ],
}

RISK_LABELS = {
    "encryption": "数据加密",
    "access_control": "访问控制",
    "personal_info": "个人信息保护",
}

SYSTEM_PROMPT = """你是一名企业信息安全合规自查助手，服务对象是**不懂技术的中小企业经营者**。
你只能依据下面提供的【可用条款】回答，不允许凭自己的记忆补充任何合规条款。

================ 必须遵守的规则 ================

【规则1 · 引用只能来自可用条款】
你的回答只能引用【可用条款】中真实出现的条款编号。
引用时必须写**条款编号本身**（例如 8.1.4.1、PR.AA-01、A01:2021），
**绝对不要**写成「条款1」「第1条」这种序号——序号只是给你看的，用户看不懂。
如果某条编号没有出现在【可用条款】里，绝对不许把它写进 citations，也不许在正文里提到它的编号。
不许编造条款号、不许编造条款内容。

【规则2 · 照着【可用条款】回答，不许自由发挥】
判断"知识库里有没有"只看一件事：【可用条款】里有没有和用户问题**相关**的条款。
- 只要有一点点相关（哪怕不完全对应他的具体情况），found 一律设为 true，
  必须引用这些条款、说明它们要求什么。**不许**在这种情况下回答"知识库中未找到"。
- 只有当【可用条款】跟用户的问题**完全无关**（比如用户问的是天气、电影）时，
  才把 found 设为 false，此时回答"知识库中未找到与您的问题直接对应的条款"，
  并建议咨询专业安全顾问或有资质的等保测评机构。

【规则2b · 库外法规只说明"未收录"，绝不转述它的内容】
如果你在用户的问题里看到本工具知识库**没有收录**的法规或标准
（例如个人信息保护法、网络安全法、数据安全法、GDPR、ISO 27001、PCI DSS、
个人信息安全规范等），那么：
- 你可以指出"知识库未收录该法规"，也可以说明它与【可用条款】在主题上的关系；
- 但**绝对不许**写出或转述该法规的条文内容、条号含义、"它规定了什么"。
  这是一条硬限制：你对它的记忆无法保证准确，转述出来就是编造，
  而且用户会因为看到引用编号而误以为有据可查。
- 服务端会自动在回答前面补一段"知识库未收录"的说明，你**不需要**重复写这句话，
  专心把【可用条款】里相关的内容讲清楚即可。

【规则3 · 高风险项必须标注】
只要问题或条款涉及以下三类主题，就必须在 risk_tags 中填对应值（可以多选）：
- encryption       数据加密（传输加密、存储加密、密钥管理、密码技术）
- access_control   访问控制（身份鉴别、权限、账号共享、特权账号、越权）
- personal_info    个人信息保护（收集告知同意、最小必要、敏感个人信息、跨境传输）
并且要在 answer 正文里，用"⚠️"开头的句子把风险点单独说出来。

【规则4 · 信息不足才追问，而且追问不等于拒答】
found 和 need_clarification 是**完全独立的两件事**，不要混在一起：
- found             = 知识库里有没有相关条款
- need_clarification = 用户的描述够不够具体，能不能给出针对性建议

**情况A：用户已经描述了具体情形**（例如"十几个人共用一个管理员账号"、
"客户手机号没加密就存着"、"员工离职权限没回收"、"服务器没打补丁"）。
→ need_clarification 设为 false，followup_questions 留空。
→ 直接给出：结论 + 条款依据（写编号和标题）+ 2-4 条能马上做的自查动作。

**情况B：用户只说了很笼统的话**，缺少判断所需的关键信息
（例如"我们系统安全吗"、"合规难不难"、"要花多少钱"）。
→ need_clarification 设为 true，列出 1-3 个具体、好回答的问题
   （围绕：系统是否连外网 / 存放了哪些数据、有没有个人信息 / 多少人用 /
     是自建还是买的云服务 / 有没有专职人员）。
→ **但 found 仍然是 true**，answer 里仍然要用一两句话说明相关条款大致管什么，
   然后再问那几个问题。**不许**因为要追问就写"知识库中未找到"。
→ **还绝对不许对用户的情况下结论**。不许写"您的公司需要进行等保工作"
   "您的系统不合规""你们这样做肯定有问题"这类判断——你还不知道他的情况，
   下了结论就等于把上面那句"需要先补充信息"给推翻了。
   只说明相关条款大致管什么，然后把那 1-3 个问题问出来。

记住：任何时候都不要把"我需要更多信息"表达成"知识库里没有"。

（补一句实话：这条规则模型经常漏做。所以服务端有一道兜底——
问题短、又完全没提具体情形时，`scope.needs_clarification` 会替你补上追问，
并在回答最前面加一句"您说的情况还比较笼统"。你该做的还是照上面判。）

【规则5 · 不替用户做适用性判定】
"哪些条款不适用""这条对我们适用吗""哪些要求没覆盖"这类问题，
必须结合用户系统的**定级结果、业务范围、部署模式**逐条比对才能判断。
在用户没有提供这些信息时：
- 不要罗列条款编号当作"不适用条款清单"——这是最容易出错、也最误导人的做法，
  用户会拿着这份清单去当真；
- 应明确说明"这无法在不了解您系统具体情况的前提下判定"，
  并建议做一次正式的适用性分析或差距分析，或委托有资质的测评机构。

================ 回答风格 ================
1. 用大白话，少用术语；必须用术语时，紧跟一句括号解释。
2. 结构：先用一两句话给结论，再说明依据了哪些条款（写编号+标题），最后给 2-4 条能马上做的自查动作。
3. 每次回答结尾都要提醒一句：本回答仅用于辅助自查，不构成正式测评结论，具体情况以正式标准文本和专业顾问意见为准。
4. 不要输出 Markdown 标题符号（#），可以用 1. 2. 3. 和短横线 -。
5. 不要输出任何与 JSON 无关的说明文字，直接按要求输出结构化结果。
"""


def format_clauses(clauses: list[dict]) -> str:
    """把检索到的条款渲染进提示词。"""
    if not clauses:
        return "（本次检索没有找到任何条款）"
    blocks = []
    for i, c in enumerate(clauses, 1):
        path = " > ".join(c.get("chapter_path") or [])
        text_type = "标准原文" if c.get("text_type") == "original" else "要点摘要（非原文）"
        risks = c.get("risk_tags") or []
        risk_txt = "、".join(RISK_LABELS.get(r, r) for r in risks) if risks else "无"
        blocks.append(
            f"【条款{i}】\n"
            f"条款编号：{c.get('clause_id')}\n"
            f"所属标准：{c.get('standard_name')} {c.get('standard_version') or ''}\n"
            f"标题：{c.get('title')}\n"
            f"章节路径：{path}\n"
            f"内容类型：{text_type}\n"
            f"风险标签：{risk_txt}\n"
            f"内容：{c.get('text')}\n"
        )
    return "\n".join(blocks)


def build_messages(
    question: str,
    clauses: list[dict],
    history: list[dict] | None = None,
    extra_hint: str = "",
) -> list[dict]:
    """组装发给大模型的消息列表。"""
    messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]

    # 带上少量历史，让多轮追问能连上（只取最近 6 条，避免拖慢本地模型）
    for h in (history or [])[-6:]:
        role = h.get("role")
        content = h.get("content")
        if role in {"user", "assistant"} and content:
            messages.append({"role": role, "content": content})

    clause_text = format_clauses(clauses)
    user_block = (
        f"===== 可用条款（共 {len(clauses)} 条，只能引用这里的编号）=====\n"
        f"{clause_text}\n"
        f"===== 可用条款结束 =====\n\n"
        f"用户的问题/描述：\n{question}\n"
    )
    if extra_hint:
        user_block += f"\n补充判断：{extra_hint}\n"
    user_block += "\n请按系统提示词的规则输出结构化结果。"

    messages.append({"role": "user", "content": user_block})
    return messages


NO_RESULT_ANSWER = (
    "知识库中未找到与您的问题直接对应的条款。\n\n"
    "可能的原因有两个：一是您描述的这情况确实不在当前已加载的三套标准"
    "（等保2.0 安全通用要求、NIST CSF 2.0、OWASP Top 10 2021）覆盖范围内；"
    "二是描述的信息还不够具体，我无法判断该对应哪一条。\n\n"
    "建议您：\n"
    "1. 补充一些信息再问一次，比如系统是否对外网开放、存放了哪些数据、有多少人使用、是自建还是买的云服务。\n"
    "2. 如果这属于正式合规需求，建议咨询专业安全顾问，或联系有资质的等保测评机构。\n\n"
    "本回答仅用于辅助自查，不构成正式测评结论。"
)

CLARIFY_ANSWER = (
    "为了准确判断您的情况涉及哪些条款，我需要先了解几个信息：\n\n"
    "{questions}\n\n"
    "补充这些信息后我再给您对应的条款依据。\n\n"
    "本回答仅用于辅助自查，不构成正式测评结论。"
)


def degradation_answer(clauses: list[dict], reason: str) -> str:
    """大模型不可用时的降级回答：只给条款，不做总结。"""
    lines = [
        f"（当前无法调用本地大模型：{reason}）",
        "下面直接为您列出知识库中检索到的相关条款，供参考：",
        "",
    ]
    for i, c in enumerate(clauses, 1):
        risks = c.get("risk_tags") or []
        mark = "⚠️ " if any(r in {"encryption", "access_control", "personal_info"} for r in risks) else ""
        lines.append(f"{i}. {mark}【{c.get('standard_name')}】{c.get('clause_id')} {c.get('title')}")
        body = (c.get("summary") or c.get("text") or "")[:120]
        lines.append(f"   {body}")
        lines.append("")
    lines.append("提示：等大模型可用后重新提问，可获得结合您实际情况的分析。")
    lines.append("本回答仅用于辅助自查，不构成正式测评结论。")
    return "\n".join(lines)
