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

from . import config, db

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
                    # 退一步 1b：编号可能只是前缀（例如模型补了 CWE 后缀或漏了后缀）
                    for cid, cand in allowed.items():
                        if cid.startswith(extracted.lower()) or extracted.lower().startswith(cid):
                            clause = cand
                            break
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
        # 也可能只是某个合法编号的一部分（模型少写/多写了后缀），不算编造
        if any(key in a or a in key for a in allowed):
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


# 模型把"我还想多问几句"写成"知识库中未找到"时，用来兜底清理
NO_RESULT_CLAIMS = [
    "知识库中未找到与您的问题直接对应的条款",
    "知识库中未找到与您的问题直接对应",
    "知识库中未找到对应的条款",
    "知识库中未找到相关条款",
    "知识库中未找到",
    "知识库中没有找到",
    "知识库中没有相关条款",
]


def strip_false_no_result(text: str) -> str:
    """删掉正文里"知识库中未找到"的说法。

    只在服务端已经判定 found=True 时调用：这时候模型这么说一定是错的，
    留着会把用户直接带偏。
    """
    if not text:
        return text
    kept: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        # 整行就是这个结论 → 丢掉
        if any(stripped.startswith(p) or stripped == p for p in NO_RESULT_CLAIMS):
            continue
        # 句子中间夹着的 → 把那个短句抠掉
        for phrase in NO_RESULT_CLAIMS:
            if phrase in line:
                line = line.replace(phrase, "根据知识库中的相关条款")
                line = line.replace("。。", "。")
        kept.append(line)
    cleaned = "\n".join(kept).strip()
    # 整段都被删空了就退回原文，总比什么都没有强
    return cleaned or text


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


def strip_out_of_kb_claims(
    text: str, detections: list[dict] | None
) -> tuple[str, int]:
    """删除回答中转述库外法规内容的句子。

    为什么提示词之外还要来一道代码清理：模型对没见过的法条最容易"张口就来"，
    而这类输出会带着等保的引用编号一起出现，用户根本分辨不出来。
    提示词是"请它别这么干"，这里是"让它干不成"。

    判定刻意保守，只删最明确的转述——一个句子必须同时满足：
      ① 出现库外法规名（或其别名）
      ② 出现"第X条/第X章"引用，或"规定/要求/明确"这类转述动词
    只是提到名字（例如"知识库未收录《个人信息保护法》"）不会被删。
    服务端随后会自己补一段"未收录"的说明，所以删掉这些句子不会让
    用户失去必要信息。

    返回 (清理后的文本, 被删除的句子数)。
    """
    if not text or not detections:
        return text, 0
    keys = _out_of_kb_keys(detections)
    if not keys:
        return text, 0

    kept_lines: list[str] = []
    removed = 0
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or not any(k in stripped for k in keys):
            kept_lines.append(line)
            continue
        # 这一行提到了库外法规 → 按句号切开逐句判断，保留没在转述条文的句子
        survivors: list[str] = []
        for part in re.split(r"(?<=[。！？；])", stripped):
            if not part.strip():
                continue
            is_claim = bool(_CLAUSE_REF_PATTERN.search(part)) or any(
                w in part for w in _CONTENT_CLAIM_WORDS
            )
            if is_claim and any(k in part for k in keys):
                removed += 1
                continue
            survivors.append(part)
        joined = "".join(survivors).strip()
        if joined:
            kept_lines.append(joined)

    cleaned = re.sub(r"\n{3,}", "\n\n", "\n".join(kept_lines)).strip()
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
