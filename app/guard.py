"""防幻觉闸门 + 高风险判定。

这是"不许编造条款号"的**技术保障**，不能只靠提示词：
- 模型返回的 citations 逐个和本次检索结果比对，对不上的直接剔除并记录，
  正文里如果出现了不存在的条款号，也会被记下来提示用户。
- 高风险 ⚠️ 由三方证据取并集判定：模型自报 + 命中条款的标签 + 用户问法里的关键词。
"""
from __future__ import annotations

import json
import re
from typing import Any

from . import config, db, scope

_risk_terms_cache: dict | None = None


def load_risk_terms(force: bool = False) -> dict:
    global _risk_terms_cache
    if _risk_terms_cache is not None and not force:
        return _risk_terms_cache
    path = config.SEED_DIR / "risk_terms.json"
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {}
    _risk_terms_cache = data
    return data


def risk_meta() -> dict[str, dict]:
    data = load_risk_terms()
    out: dict[str, dict] = {}
    for tag, info in (data.get("high_risk_tags") or {}).items():
        out[tag] = {
            "tag": tag,
            "label": info.get("label", tag),
            "emoji": info.get("emoji", "⚠️"),
            "why": info.get("why", ""),
            "terms": info.get("terms", []),
        }
    return out


# --------------------------------------------------------------------------
# 引用校验
# --------------------------------------------------------------------------
# 只认"像条款编号"的形态：
#   等保    8.1.4.1（要求至少 3 段，避免把"等保2.0"、"第2.3节"误判成条款号）
#   NIST   PR.AA-01
#   OWASP  A01:2021 / A03:2021-CWE-89
# 前后用 (?<![\d.]) / (?![\d.]) 卡住，保证抓到完整编号而不是"8.2.3.3"里的"2.3"。
CLAUSE_ID_PATTERN = re.compile(
    r"(?<![\d.])"
    r"(?:\d{1,2}(?:\.\d{1,3}){2,4}"
    r"|[A-Z]{2}\.[A-Z]{2}-\d{2}"
    r"|A\d{2}:\d{2}(?:2021)?(?:-CWE-\d+)?)"
    r"(?![\d.])"
)


def build_citation_map(clauses: list[dict]) -> dict[str, dict]:
    """本次检索到的条款编号 -> 条款。大小写不敏感。"""
    return {str(c.get("clause_id", "")).strip().lower(): c for c in clauses}


_CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}

# 从模型返回的字符串里把条款编号抠出来。实测模型很爱写
# "8.1.4.1 身份鉴别（标识与口令复杂度）" 或 "条款1: 数据存储与备份未加密"，
# 直接做全串匹配会全部被当成非法引用剔除，引用卡片就空了。
_ID_PATTERNS = [
    re.compile(r"^(\d+(?:\.\d+){1,4})"),                       # 8.1.4.1
    re.compile(r"^([A-Z]{2}\.[A-Z]{2}-\d{2})"),                # PR.AA-01
    re.compile(r"^(A\d{2}:\d{2}(?:2021)?(?:-CWE-\d+)?)"),      # A01:2021 / A03:2021-CWE-89
    re.compile(r"^(条款\s*\d{1,2})"),                          # 条款1: xxx
    re.compile(r"^(第\s*\d{1,2}\s*条)"),                       # 第1条
]


def extract_clause_id(raw: Any) -> str | None:
    """从 "8.1.4.1 身份鉴别" 这类字符串里提取出纯条款编号。"""
    s = str(raw).strip().strip("【】[]（）() ").strip()
    if not s:
        return None
    for pattern in _ID_PATTERNS:
        m = pattern.match(s)
        if m:
            return m.group(1).strip()
    return None


def positional_index(raw: str) -> int | None:
    """把「条款1」「第2条」「条款三」这类**位置序号**解析成 1 起始的下标。

    小模型（尤其是推理模型）经常不写真实条款编号，而是照抄提示词里的
    【条款1】【条款2】。与其把这些引用一律丢掉，不如翻译回真实条款，
    这样回答质量更好；实在翻不出来的才剔除。
    注意：带点号的真实编号（8.1.4.1）不会被这里匹配。
    """
    s = str(raw).strip().strip("【】[]（）() ").strip()
    m = re.fullmatch(r"(?:条款|第)?\s*(\d{1,2})\s*(?:条|项|个|号)?", s)
    if m:
        return int(m.group(1))
    m = re.fullmatch(r"(?:条款|第)?\s*([一二三四五六七八九十]+)\s*(?:条|项|个|号)?", s)
    if m:
        return _CN_NUM.get(m.group(1))
    return None


def _expand_unique_prefix(extracted: str, allowed: dict[str, dict]) -> dict | None:
    """按**点号边界**做唯一前缀展开；候选不唯一、或不在边界上，一律不猜。

    为什么不能用双向 startswith：模型写 8.1.4.1、检索集里只有 8.1.4.11 时，
    旧实现会把引用"展开"成 8.1.4.11——卡片上显示 8.1.4.11，正文里写着 8.1.4.1，
    用户看到的是"系统替他圆了谎"。宁可这条引用不显示，也不能给出错配的来源。

    允许的展开方向只有"模型写得比知识库短"这一种：
      8.1.4  → 8.1.4.1（唯一候选时）
      8.1.4.1 → 8.1.4.11（不展开：扩展点不在点号边界上）
      8.1.4  → 8.1.4.1 / 8.1.4.2（不展开：候选不唯一）
    """
    if not extracted:
        return None
    candidates = [cid for cid in allowed if cid.startswith(extracted + ".")]
    if len(candidates) == 1:
        return allowed[candidates[0]]
    return None


def validate_citations(raw_ids: Any, clauses: list[dict]) -> tuple[list[dict], list[str]]:
    """把模型给的编号映射回真实条款。

    返回 (合法引用列表, 无法解析的编号列表)。
    """
    allowed = build_citation_map(clauses)
    valid: list[dict] = []
    invalid: list[str] = []
    seen: set[str] = set()

    ids: list[str] = []
    if isinstance(raw_ids, str):
        ids = [raw_ids]
    elif isinstance(raw_ids, list):
        for item in raw_ids:
            if isinstance(item, str):
                ids.append(item)
            elif isinstance(item, dict):  # 兼容模型偶尔返回对象的情况
                for key in ("clause_id", "id", "编号", "clause"):
                    if isinstance(item.get(key), str):
                        ids.append(item[key])
                        break

    for raw in ids:
        key = str(raw).strip().strip("【】[]（）() ").lower()
        if not key or key in seen:
            continue
        seen.add(key)

        clause = allowed.get(key)
        if clause is None:
            # 退一步 1：从 "8.1.4.1 身份鉴别" 里抠出编号再匹配
            extracted = extract_clause_id(raw)
            if extracted:
                clause = allowed.get(extracted.lower())
                if clause is None:
                    # 退一步 1b：只做点号边界上的唯一前缀展开（见函数注释）
                    clause = _expand_unique_prefix(extracted.lower(), allowed)
        if clause is None:
            # 退一步 2：按「第 N 条」的位置序号翻译
            idx = positional_index(raw)
            if idx is not None and 1 <= idx <= len(clauses):
                clause = clauses[idx - 1]
        if clause is None:
            invalid.append(str(raw).strip())
            continue
        cid = str(clause.get("clause_id", "")).lower()
        if cid in {str(c.get("clause_id", "")).lower() for c in valid}:
            continue
        valid.append(to_citation(clause))
    return valid, invalid


def rewrite_positional_refs(text: str, clauses: list[dict]) -> str:
    """把正文里的【条款1】这类位置序号替换成真实条款编号+标题。

    这样即使用的是推理模型，用户看到的也还是真实条款号。
    """
    if not text or not clauses:
        return text

    def repl_bracketed(match: re.Match) -> str:
        idx = positional_index(match.group(1))
        if idx is not None and 1 <= idx <= len(clauses):
            c = clauses[idx - 1]
            return f"【{c.get('clause_id')} {c.get('title')}】"
        return match.group(0)

    def repl_plain(match: re.Match) -> str:
        idx = positional_index(match.group(0))
        if idx is not None and 1 <= idx <= len(clauses):
            return str(clauses[idx - 1].get("clause_id"))
        return match.group(0)

    # 先处理带括号的：【条款1】【第2条】
    text = re.sub(r"【\s*((?:条款|第)\s*(?:\d{1,2}|[一二三四五六七八九十]+)\s*(?:条|项|个|号)?)\s*】",
                  repl_bracketed, text)
    # 再处理裸写的。这里刻意收紧，只认「条款N」「条款三」「第N条」三种写法，
    # 免得把"第一，要检查…"这种正常中文误改。
    text = re.sub(r"条款\s*(?:\d{1,2}|[一二三四五六七八九十]+)(?![\d.])", repl_plain, text)
    text = re.sub(r"第\s*(?:\d{1,2}|[一二三四五六七八九十]+)\s*条(?![\d.])", repl_plain, text)
    return text


def to_citation(clause: dict) -> dict:
    return {
        "standard_id": clause.get("standard_id", ""),
        "standard_name": clause.get("standard_name", ""),
        "standard_version": clause.get("standard_version", ""),
        "clause_id": clause.get("clause_id", ""),
        "title": clause.get("title", ""),
        "chapter_path": clause.get("chapter_path") or [],
        "text": clause.get("text", ""),
        "text_type": clause.get("text_type", "summary"),
        "risk_tags": clause.get("risk_tags") or [],
        "score": clause.get("score", 0.0),
        "source_url": clause.get("source_url", ""),
        # 来源追溯：内置种子还是用户导入，界面上要能区分可信度
        "source_kind": clause.get("source_kind", "builtin"),
        "batch_id": clause.get("batch_id"),
        "verified": True,
    }


def detect_hallucinated_ids(answer_text: str, clauses: list[dict]) -> list[str]:
    """扫描正文里出现的、但不在本次检索结果中的条款编号。"""
    allowed = {str(c.get("clause_id", "")).strip().lower() for c in clauses}
    found = CLAUSE_ID_PATTERN.findall(answer_text or "")
    suspicious: list[str] = []
    for token in found:
        key = token.strip().lower()
        if key in allowed:
            continue
        # 只有「点号边界上的前缀」关系才算同一个编号（模型少写了一段）。
        # 旧实现是任意包含：正文里的 8.1.4.1 会被检索集中的 8.1.4.11 "吸收"，
        # 真正的编造就漏报了——用户看不到任何提示。
        if any(
            a == key or a.startswith(key + ".") or key.startswith(a + ".")
            for a in allowed
        ):
            continue
        if token not in suspicious:
            suspicious.append(token)
    return suspicious


# --------------------------------------------------------------------------
# 高风险判定
# --------------------------------------------------------------------------
def _terms_hit(text: str, terms: list[str]) -> list[str]:
    lowered = (text or "").lower()
    return [t for t in terms if t.lower() in lowered]


def detect_risks(
    question: str,
    clauses: list[dict],
    model_tags: Any = None,
    *,
    cited_ids: list[str] | None = None,
) -> list[dict]:
    """判定本次回答需要哪些 ⚠️ 标注。

    三个证据来源，但**权重不同**——实测 qwen2.5 有"见谁都标"的倾向，
    经常把三类风险全填上，所以模型自报不能单独采信，必须有别的证据佐证：
      证据2 用户问题里出现相关词          → 直接采纳
      证据3 被引用的条款自带该风险标签    → 直接采纳
      证据1 模型自报                      → 需要问题或引用条款的文本里能找到相关词才采纳
    """
    meta = risk_meta()
    hits: dict[str, dict] = {}
    cited_clauses = [
        c for c in clauses
        if cited_ids and str(c.get("clause_id", "")).lower() in {str(x).lower() for x in cited_ids}
    ] or (clauses[:1] if clauses else [])

    def note(tag: str, reason: str, clause_id: str | None = None) -> None:
        if tag not in meta:
            return
        entry = hits.setdefault(
            tag,
            {
                "tag": tag,
                "label": meta[tag]["label"],
                "emoji": meta[tag].get("emoji", "⚠️"),
                "why": meta[tag].get("why", ""),
                "clauses": [],
                "reason": reason,
            },
        )
        if clause_id and clause_id not in entry["clauses"]:
            entry["clauses"].append(clause_id)

    # 证据2：用户问题里的词
    for tag, info in meta.items():
        hit_terms = _terms_hit(question, info.get("terms", []))
        if hit_terms:
            note(tag, f"问题中提到：{'、'.join(hit_terms[:3])}")

    # 证据3：被引用条款自带的标签
    for clause in cited_clauses:
        for tag in clause.get("risk_tags") or []:
            if tag in config.HIGH_RISK_TAGS:
                note(tag, "引用条款", clause.get("clause_id"))

    # 证据1：模型自报 —— 必须能在文本里找到佐证，避免"见谁都标"
    tags = model_tags if isinstance(model_tags, list) else ([model_tags] if isinstance(model_tags, str) else [])
    # 佐证语料只用"问题 + 条款标题"，**刻意不拼条款正文、摘要和关键词**。
    # 这是两次实测误标换来的教训：
    #   ① 用户问"安全审计"，被引用的 8.1.4.8 等条款正文里顺带提到了"个人信息"，
    #      拼进语料后 personal_info 就被标上了，而那几条条款的 risk_tags 里
    #      根本没有它——正文里的"顺便提一句"不构成"这次提问涉及该主题"。
    #   ② 去掉正文后仍然误标：8.1.3.14（安全审计·审计记录留存）的关键词里有
    #      "留存期限"，而这个词同时属于"日志留存"和"个人信息留存"两个语境，
    #      放在关键词里必然误伤。关键词表是为"检索"设计的，词面宽是优点；
    #      拿来做"要不要标风险"的证据，宽就变成了错。
    # 标题是人工编写的、指向明确的短语，拿它当佐证既准确又够用。
    corpus_parts = [question]
    for clause in cited_clauses:
        corpus_parts.append(str(clause.get("title") or ""))
    corpus = " ".join(corpus_parts)
    for raw_tag in tags:
        tag = str(raw_tag).strip()
        if tag not in meta or tag in hits:
            continue
        if _terms_hit(corpus, meta[tag].get("terms", [])):
            note(tag, "模型判定（有文本佐证）")

    return list(hits.values())


# --------------------------------------------------------------------------
# 关于「知识库中未找到」（P0-4：把诚实改成不诚实的那行代码，已删除）
# --------------------------------------------------------------------------
# 旧实现是 strip_false_no_result()：只要服务端判了 found=True，就把模型
# 诚实说出的「知识库中未找到与您的问题直接对应的条款」整句删掉，
# 再回填 5 条真实条款。结果是——模型明明说了没找到，用户看到的却是
# 「免责声明 + 5 条引用」，一句"我没找到"都不剩。
#
# 现在 found 直接采信模型自己的判断（见 routers/chat.py），
# 因此这里不再需要、也**刻意不保留**任何"删掉未找到"的代码：
# 留着它，就总有人会再把它接到 found 上。


# --------------------------------------------------------------------------
# 库外法规内容清理
# --------------------------------------------------------------------------
# 命中"在讲某部法规规定了什么"的句式。配合 app/scope.py 的 detect_out_of_kb
# 使用：先从模型回答里把这些句子摘掉，再由服务端补上"未收录"的说明。
_CLAUSE_REF_PATTERN = re.compile(r"第\s*[\d一二三四五六七八九十百]+\s*[条章节款项]")
_CONTENT_CLAIM_WORDS = ("规定", "要求", "明确", "指出", "载明", "写的是", "讲的是", "要求了")


def _out_of_kb_keys(detections: list[dict] | None) -> list[str]:
    """从 scope.detect_out_of_kb 的结果里收集所有可用于匹配的写法。

    直接用显示名匹配是不够的：显示名是
    "《中华人民共和国个人信息保护法》（PIPL）"，而模型正文里
    只会写"《个人信息保护法》"或"个人信息保护法"。
    所以要把书名号里的简称、去掉"中华人民共和国"前缀的形式都收进来。

    另外还要吃 `all_aliases`（这部法规的**全部**写法），而不只是
    `aliases`（这次问题里用到的写法）：用户问"《个人信息保护法》"，
    模型完全可能在回答里写"PIPL"，只匹配用户用过的写法就会漏掉。
    """
    keys: list[str] = []
    for d in detections or []:
        if not isinstance(d, dict):
            continue
        name = str(d.get("name") or "")
        if name:
            keys.append(name)
            for inner in re.findall(r"《([^》]+)》", name):
                keys.append(inner)
                keys.append(re.sub(r"^中华人民共和国", "", inner))
        for alias in (d.get("all_aliases") or []) + (d.get("aliases") or []):
            keys.append(str(alias))
    # 长的排前面，避免"安全法"抢先匹配掉"网络安全法"
    return sorted({k for k in keys if k}, key=len, reverse=True)


_PARA_SPLIT = re.compile(r"\n\s*\n")
_SENTENCE_SPLIT = re.compile(r"(?<=[。！？；\n])")


def strip_out_of_kb_claims(
    text: str, detections: list[dict] | None
) -> tuple[str, int]:
    """删除回答中转述库外法规内容的句子。

    为什么提示词之外还要来一道代码清理：模型对没见过的法条最容易"张口就来"，
    而这类输出会带着等保的引用编号一起出现，用户根本分辨不出来。
    提示词是"请它别这么干"，这里是"让它干不成"。

    判定刻意保守，只删最明确的转述——一个句子必须同时满足：
      ① 出现库外法规名（或其别名），**或** 它跟在同一段里已经提过法规名的句子后面
      ② 出现"第X条/第X章"引用，或"规定/要求/明确"这类转述动词
    只是提到名字（例如"知识库未收录《个人信息保护法》"）不会被删。
    服务端随后会自己补一段"未收录"的说明，所以删掉这些句子不会让
    用户失去必要信息。

    H-1：判定单位是**段落 + 句子**，不是"行"。旧实现只检查"这一行里有没有法规名"，
    于是下面这种写法整段幸存：
        《个人信息保护法》的要求如下：
        第13条规定，处理个人信息应当取得个人同意。
    第一行有法规名但没有转述动词 → 保留；第二行有"第13条"但没有法规名 → 也保留。
    结果服务端一边说"我不会转述这部法规"，一边把转述原样留在下面。

    H-4：匹配前用 scope.normalize_for_match 归一化，繁体「個人信息保護法」、
    全角「第１７條」也能认出来；**输出仍是原文**，不改用户的写法。

    返回 (清理后的文本, 被删除的句子数)。
    """
    if not text or not detections:
        return text, 0
    keys = _out_of_kb_keys(detections)
    if not keys:
        return text, 0

    kept_blocks: list[str] = []
    removed = 0
    carry_from_prev = False  # 上一段结尾提到过库外法规
    for block in _PARA_SPLIT.split(text):
        carry_in_block = False
        survivors: list[str] = []
        for sentence in _SENTENCE_SPLIT.split(block):
            body = sentence.strip()
            if not body:
                survivors.append(sentence)
                continue
            # 用 law_match_text 而不是 normalize_for_match：法规名被点号/空格
            # 拆开时（「个人信息·保护法」），只有去掉标点才认得出这是同一个名字。
            probe = scope.law_match_text(body)
            has_key = any(k in probe for k in keys)
            # 先记下"本段前面是否已点名法规"，再更新标记：
            # 点名法规的那一句本身往往就带「要求如下：」而被删掉，
            # 如果只在"保留分支"里置位，紧跟其后的条文就会失去上下文而幸存。
            prior_key = carry_in_block
            if has_key:
                carry_in_block = True
            is_clause = bool(_CLAUSE_REF_PATTERN.search(probe))
            is_claim = is_clause or any(w in probe for w in _CONTENT_CLAIM_WORDS)
            # 同一段里前面已经点名了法规 → 后面的转述句即使没重复名字也算转述；
            # 跨段只在"第X条"这种明确的条文引用上延续，避免误删下一段的正题。
            carried = prior_key or (carry_from_prev and is_clause)
            if is_claim and (has_key or carried):
                removed += 1
                continue
            survivors.append(sentence)
        carry_from_prev = carry_in_block
        joined = "".join(survivors).strip()
        if joined:
            kept_blocks.append(joined)

    cleaned = re.sub(r"\n{3,}", "\n\n", "\n\n".join(kept_blocks)).strip()
    return cleaned, removed


def ensure_disclaimer(text: str) -> str:
    """兜底：确保回答里有免责声明。"""
    if not text:
        return text
    if "不构成正式测评结论" in text:
        return text
    return (
        text.rstrip()
        + "\n\n本回答仅用于辅助自查，不构成正式测评结论，"
        "具体情况以正式标准文本和专业顾问意见为准。"
    )


def strip_markdown_headings(text: str) -> str:
    """小模型有时会输出 # 标题，前端不渲染 Markdown，这里顺手清理。"""
    if not text:
        return text
    lines = []
    for line in text.splitlines():
        lines.append(re.sub(r"^\s{0,3}#{1,6}\s*", "", line))
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 兜底：回答里出现**清单之外**的法规名（《…法》《…条例》《…规定》《…办法》）
# --------------------------------------------------------------------------
# 为什么需要它：库外法规清单永远是有限枚举。下一部没收录的法规（地方条例、
# 行业办法、新简称……）只要没进表，模型转述它就没有任何代码拦得住——
# t11 复验用台湾用语「個人資料保護法」就绕过了整张别名表。
# 所以再加一层结构识别：**看起来像法规名**的，只要不在已知别名里，
# 一律声明"不在本次检索范围"。宁可多声明一句，不可让转述冒充知识库内容。
_LAW_NAME_PATTERN = re.compile(r"《([^》\n]{2,40})》")
_LAW_NAME_TAILS = ("法", "条例", "规定", "办法", "细则", "规范", "准则", "纲要", "意见", "通知")
_COVERED_DOC_HINTS = ("等级保护", "22239", "GB/T", "GB／T", "NIST", "OWASP", "CSF", "Top 10")


def detect_unsupported_law_names(
    text: str, known_names: list[str] | None = None
) -> list[str]:
    """回答里出现的《…法/条例/规定/办法》式名称，凡是没在已知别名里的都揪出来。

    known_names 传当前已识别的法规别名（含库外法规清单展开的别名）——
    那些由 scope.detect_out_of_kb / strip_out_of_kb_claims 负责，这里不重复报。
    """
    known = [k for k in (known_names or []) if k]
    found: list[str] = []
    for match in _LAW_NAME_PATTERN.finditer(str(text or "")):
        name = match.group(1).strip()
        if not name:
            continue
        if not name.endswith(_LAW_NAME_TAILS):
            continue
        if any(hint in name for hint in _COVERED_DOC_HINTS):
            continue
        if any(name == k or name in k or k in name for k in known):
            continue
        found.append(name)
    return list(dict.fromkeys(found))


# --------------------------------------------------------------------------
# 兜底：回答里替用户下等级结论（「你们应当按三级建设」）
# --------------------------------------------------------------------------
# 回答侧兜底：不允许"替用户下等级结论"的句子渲染出去
# --------------------------------------------------------------------------
# 为什么需要它：前置闸门只能拦"问法"，拦不住模型自己在正文里下结论。
# 第四轮红队给了 18 条句式，旧实现只删掉 3 条——因为它要求"同一句里同时出现
# 主语 + 动词 + 等级"，而模型经常省略主语（"按三级建设即可"）或拆成两句
# （"经评估，贵司系统符合要求。等级为第三级。"）。这一版改成滑动窗口：
#   本句或上一句有用户指代 + 本句含等级锚点 + 含判断词        → 删
#   上一句有用户指代 + 本句短且含锚点 + 系词（为/是/属于）    → 删（拆句式）
# 两条护栏（误伤和漏判同罪）：
#   · 疑问句不删——那是系统在反问用户补充信息
#   · 主语是"标准本身"（等保/标准/条款/要求/规定…）不删——那是在介绍标准
_USER_REF = re.compile(
    r"(您|你|你们|贵司|贵公司|贵单位|咱们|咱公司|我们公司|我公司|本公司|"
    r"这套系统|该系统|贵系统|您的系统|你们的系统)"
)
# 回答侧锚点：只认"带数字的等级"（三级/第3级/L3/Level-3/T3）。
# 不再复用 scope 的裸「级」锚点：回答里「定级备案状态」「分级保护」这类词
# 会命中裸「级」，于是「您可以先确认系统的定级备案状态…」被当成下落结论删掉。
_LEVEL_REF = re.compile(
    r"(?:第\s*)?[一二三四五1-5]\s*级|"
    r"[Ll]v?\s*-?\s*[1-5]|Level\s*-?\s*[1-5]|(?<![A-Za-z0-9])[Tt]\s*-?\s*[1-5]",
    re.I,
)
# 第六轮：回答侧也要认档位表达（"建议按第二档建设""可以按三等建设"）。
# 问题侧早就拦档位了，回答侧只认"级"，两处判定不一致就是漏网。
_TIER_ANCHOR = re.compile(
    r"(第\s*[一二三四五1-5一二三四五六七八九十]+\s*(?:档|阶|层|类|等)|"
    r"[一二三四五1-5]\s*(?:档|星|等|类)(?:标准|要求|级)?|"
    r"(?:最高|较高|中等|较低|最低|高|中|低)\s*档|"
    r"档位|梯队|等第|tier\s*-?\s*\d)"
)
# 判断词：模型在替用户"拍板"时几乎必带其中之一
_LEVEL_JUDGE = re.compile(
    r"(建议|应当|应该|应(?=\s*(?:按|按照|做|定|评|达到|建设|整改|适用|属于|为|采用|执行|以))|"
    r"需要|必须|属于|符合|满足|达到|推荐|宜|须|采用|执行|为准|"
    r"适用于|适用|定为|定到|定在|评为|即为|即可|够用|足够|够(?:了|啦)|"
    # 「可以」只有落在"可以按三级/可以定三级/……可以。"这种拍板位置上才算判断；
    # 否则「您可以对照等保三级条款逐项自查。」这种给用户动作的引导句会被误删。
    r"(?:可以(?=\s*(?:按|按照|定|评|做|建设|整改|达到|属于|算|为))|可以(?=[。！!]?\s*$))|"
    r"按.{0,10}(?:建设|建|做|整改|评|定))"
)
# 无主语断言分支：只要「等级锚点 + 判断动词」就删，不再要求句子里出现
# "建议/应当/结论"这类副词——红队实测「符合三级要求。」「达到三级。」正是
# 靠"没有副词"活下来的。
# 无主语断言分支的判据：只要出现判断动词，就算在替用户下结论。
# （不再要求"建议/应当"这类副词也出现——"符合三级要求。"同样在给结论。）
_JUDGE_VERB = re.compile(
    r"(建议|应当|应该|应(?=按|按照|做|定|评|达到|建设|整改|适用|属于|为)|"
    r"需要|必须|属于|符合|满足|达到|推荐|宜|须|采用|执行|为准|"
    r"适用于|适用|定为|定到|定在|评为|即为|即可|够用|足够|够(?:了|啦)|"
    r"(?:可以(?=\s*(?:按|按照|定|评|做|建设|整改|达到|属于|算|为))|可以(?=[。！!]?\s*$))|"
    r"按.{0,10}(建设|建|做|整改|评|定))"
)
_COPULA = re.compile(r"(为|是|属于|属|即为|定为|评为)")
# 护栏一：疑问句（反问用户补充信息）不删
_QUESTION_GUARD = re.compile(r"(？|\?|哪些|什么|怎么|如何|是否)")
# 护栏二：主语是标准本身时不删（介绍标准，不是替用户下结论）
_STANDARD_SUBJECT = re.compile(
    r"^\s*(等保|等级保护|标准|条款|要求|规定|规范|控制项|测评要求|"
    r"第?\s*[一二三四五1-5]\s*级\s*(?:条款|要求|规定|标准|规范|控制项))"
)
_STANDARD_STRUCT = re.compile(
    r"(等保|等级保护|标准|条款|要求|规定|规范|控制项).{0,8}(里|中|的)"
)
# 「建议 / 应当 / 结论 + 等级词」是断言句：即使主语是标准，也不能再豁免
# （第五轮：红队用「等保三级建设要求应当按三级执行」绕过豁免）。
_ASSERTION = re.compile(
    r"(建议|应当|应该|结论|因此|所以|意味着|即可|够用|必须|需要|须|"
    r"可以(?=\s*(?:按|按照|定|评|做|建设|整改|达到|属于|算)))"
)
# 主语是「标准本身或标准里的对象」时，它是在介绍标准，不是替用户下结论。
# （第六轮误删的「三级系统应采用双因素认证。」「三级要求包括…」正缺这条；
#   判据落在主语上，而不是靠词表——同一动词换主语，一留一删。）
_STANDARD_OBJECT = re.compile(
    r"^\s*(?:本?等保|等级保护|本?(?:标准|条款|要求|规定|规范|控制项)|测评要求|"
    r"(?:第\s*)?[一二三四五1-5]\s*(?:级|档|层|阶|类|等)\s*"
    r"(?:系统|要求|标准|规范|条款|规定|控制项|测评|建设|整改|里|中|的))"
)
# 许可/免除的"法律结论"：「等保不做也不会被处罚。」——没有等级词，但同样是
# 在替用户拍板"你可以不做"。这类句子按「义务词 + 否定 + 后果断言」判。
# 语序不固定：「等保不做也不会被处罚」与「不做等保也没人查」都是同一类断言。
_LAW_CONCLUSION = re.compile(
    r"(?:(?:等保|等级保护|测评|定级|备案)[^。！？；\n]{0,14}"
    r"(?:不做|不用做|不需要做|可以不|不测评|不备案|免于)"
    r"|(?:不做|不用做|不需要做|不测评|不备案|免于)[^。！？；\n]{0,10}"
    r"(?:等保|等级保护|测评|定级|备案))"
    r"[^。！？；\n]{0,14}"
    r"(?:不会|不违法|不处罚|不会被处罚|不会被查|没人管|没人查|也没人|没事|不影响|也可以)"
)
# 征求/拍板尾巴：「三档吧」「三级得了」——没有判断动词，但语用上就是建议。
_SUGGESTION_TAIL = re.compile(r"(?:吧|得了|就行|就好了|就可以了|就够了|即可)[。！!]?$")

# 冒号/等号式片段：「等级：第三级。」「定级结果：三级。」——没有系词，
# 但语义上就是在交付一个等级结论。
_LABEL_VALUE = re.compile(
    r"(等级|定级|级别|结果|结论|档位|等第)\s*(?:是|为)?\s*[:：=]\s*"
    r"[^。！？；\n]{0,12}"
    r"(级|[一二三四五1-5]\s*(?:档|阶|层|类|等|星)|tier\s*-?\s*\d)"
)
# 合规许可/免除结论：「可以。你们规模小，不需要做等保。」「行，可以先不测评。」
# 这类和"替用户下等级结论"同级——都是在替用户拍板"你可以不做"。
_PERMISSION = re.compile(
    r"((不需要|不用|无需|免于|免除|不必|可以不|不用再)[^。！？；\n]{0,12}"
    r"(等保|等级保护|测评|定级|备案|整改|建设))|"
    r"((可以|行)[^。！？；\n]{0,12}(不|免)[^。！？；\n]{0,4}"
    r"(做|测评|备案|整改|建设|等保))|"
    r"((不做|不用做|不需要做|可以不测评|不测评|不备案|免于)[^。！？；\n]{0,8}"
    r"(也可以|也行|即可|就行|没问题|可以))"
)
# 极短的裸否定句：「可以不做。」「不用做。」——没有宾语也要拦，
# 否则"可以不做。"这种最干脆的答复会原样交到用户手里。
_SHORT_DENIAL = re.compile(
    r"^(可以|行)?[，,]?\s*(不做|不用做|不需要做|不必做|不测评|不用测评|不备案)"
    r"(了|的)?[。！!]?$"
)
_LEVEL_CONCLUSION_NOTE = (
    "具体定到哪一级，需要结合你们的业务性质、数据规模和影响面判断，"
    "本工具不替您下这个结论。"
)


def strip_level_conclusions(text: str) -> tuple[str, bool]:
    """删掉"替用户下等级结论"的句子，返回 (清理后正文, 是否命中)。

    命中时整条回答由调用方降级为 needs_context（见 routers/chat.py）：
    用户读到的只能是"我不能替你判断"，不能是一个等级。
    """
    if not text:
        return text, False
    kept: list[str] = []
    removed = 0
    prev_user = False
    for raw in _SENTENCE_SPLIT.split(text):
        body = raw.strip()
        if not body:
            kept.append(raw)
            continue
        probe = scope.normalize_for_match(body)
        user = bool(_USER_REF.search(probe))
        # 等级锚点（带数字）或档位锚点（"第二档""三等""tier-3"）任一命中都算
        anchor = bool(_LEVEL_REF.search(probe) or _TIER_ANCHOR.search(probe))
        judge = bool(_LEVEL_JUDGE.search(probe))
        question = bool(_QUESTION_GUARD.search(probe))
        assertion = bool(_ASSERTION.search(probe))
        standard = bool(
            _STANDARD_SUBJECT.search(probe) or _STANDARD_STRUCT.search(probe)
        )
        standard_obj = bool(_STANDARD_OBJECT.search(probe))
        # 许可/免除的"法律结论"（等保不做也不会被处罚）虽以「等保」开头，
        # 但它不是知识句，不能被标准主语豁免掉。
        law = bool(_LAW_CONCLUSION.search(probe))
        # 豁免只作用于"纯知识句"：主语是标准/标准里的对象，且不像在下结论。
        # 「按等保三级的要求，你们应当按三级建设」里既有标准词也有「你们」，
        # 所以还必须排除"句中指向用户"与"法律结论"两种情况。
        guarded = question or (
            (standard or standard_obj) and not user and not assertion and not law
        )
        # 合规许可/免除结论：「不需要做等保」「可以先不测评」
        permission = bool(_PERMISSION.search(probe)) or (
            len(probe) <= 8 and bool(_SHORT_DENIAL.search(probe))
        )
        # 主句：本句或上一句有用户指代 + 锚点 + 判断词
        hit_main = anchor and judge and (user or prev_user)
        # 无主语断言句：「符合三级要求。」「达到三级。」「宜按三级建设。」
        # 只要求"锚点 + 判断动词"，不再要求出现建议/应当这类副词。
        # 再加一类"征求/拍板尾巴"：「三档吧」「三级得了」。
        hit_no_subject = anchor and (
            bool(_JUDGE_VERB.search(probe)) or bool(_SUGGESTION_TAIL.search(probe))
        )
        # 冒号/等号式：「等级：第三级。」「定级结果：三级。」
        hit_label = bool(_LABEL_VALUE.search(probe))
        # 无主语的短系词句：「等级为第三级。」
        hit_fragment = anchor and len(body) <= 16 and bool(_COPULA.search(probe)) and not question
        # 裸短句：「第三级。」——上句有用户指代时，它就是结论本身（不再要求系词）
        hit_bare = (
            anchor
            and prev_user
            and len(body) <= 16
            and not question
            and not standard
            and not standard_obj
        )
        # 拆句式：上一句有用户指代，本句短，且是"等级为 X / 属三级"这种系词句
        hit_split = anchor and prev_user and len(body) <= 24 and bool(_COPULA.search(probe))
        if not guarded and (
            permission
            or law
            or hit_main
            or hit_no_subject
            or hit_label
            or hit_fragment
            or hit_bare
            or hit_split
        ):
            removed += 1
            prev_user = False
            continue
        kept.append(raw)
        prev_user = user
    if not removed:
        return text, False
    cleaned = "".join(kept).strip()
    note = "⚠️ " + _LEVEL_CONCLUSION_NOTE
    return (cleaned + "\n\n" + note) if cleaned else note, True


# --------------------------------------------------------------------------
# RT18-06：正文里残留的「第X条…规定」条文转述
# --------------------------------------------------------------------------
# 知识库用的是 8.1.4.1 / PR.AA-01 / A01:2021 这类编号，**没有**「第X条」这种
# 形式。正文里出现「第十三条规定…」几乎一定是模型凭记忆转述库外法规。
# 这类句子不能原样留给用户，也不能只写进前端折叠的"技术详情"——必须就地
# 标注"这不是知识库依据"。声明由服务端写，模型删不掉。
_CLAUSE_CLAIM = re.compile(
    r"第\s*[0-9一二三四五六七八九十百]+\s*条[^。！？\n]{0,80}?"
    r"(规定|要求|明确|指出|载明|写的是|讲的是)"
)
_UNVERIFIED_PREFIX = "（以下内容不是知识库依据，仅供定位参考："


def annotate_unverified_clause_claims(text: str) -> tuple[str, int]:
    """给「第X条…规定」式句子就地加"不是知识库依据"的标记。

    返回 (处理后的正文, 标注句数)。
    """
    if not text:
        return text, 0
    kept: list[str] = []
    marked = 0
    for raw in _SENTENCE_SPLIT.split(text):
        body = raw.strip()
        if not body or _UNVERIFIED_PREFIX in body:
            kept.append(raw)
            continue
        if _CLAUSE_CLAIM.search(scope.normalize_for_match(body)):
            tail = "\n" if raw.endswith("\n") else ""
            kept.append(_UNVERIFIED_PREFIX + body + "）" + tail)
            marked += 1
            continue
        kept.append(raw)
    if not marked:
        return text, 0
    return "".join(kept), marked
