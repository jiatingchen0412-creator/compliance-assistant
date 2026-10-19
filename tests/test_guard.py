"""输出守卫测试（第一档 · 库外法规内容清理 + ⚠️ 误标回归）。

这里测的都是"防线"而不只是"功能"：
  · `strip_out_of_kb_claims` —— 提示词请模型别转述库外法条，这里保证它转述也留不下来。
  · `detect_risks`        —— 用户问"安全审计"，不该被标上"个人信息保护"。

误标那一条是真实回归：qwen2.5 有"见谁都标"的倾向，经常把三类风险全填上；
而原来的佐证语料里拼了被引用条款的**正文**，正文里顺带提了一句"个人信息"，
personal_info 就被采纳了。收窄语料的代价是"佐证更严"，收益是"不再瞎标"。

不需要 Ollama，不需要数据库（风险词表来自 data/seed/risk_terms.json）。

用法（在项目根目录执行）：
    .venv\\Scripts\\python.exe tests\\test_guard.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config, guard, scope  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def case(name: str):
    def wrapper(fn):
        RESULTS.append((name, fn))
        return fn
    return wrapper


PIPL_QUESTION = "《个人信息保护法》第13条具体是怎么规定的？"


def _detections(question: str) -> list[dict]:
    return scope.detect_out_of_kb(question)


def _tags(risks: list[dict]) -> list[str]:
    return [r["tag"] for r in risks]


# --------------------------------------------------------------------------
# strip_out_of_kb_claims —— 库外法规内容清理
# --------------------------------------------------------------------------
@case("转述库外法规内容的句子会被删掉")
def test_strip_content_claim():
    text = (
        "《个人信息保护法》第13条规定了处理个人信息的合法性基础。\n"
        "我们的系统还需要关注数据加密。"
    )
    cleaned, removed = guard.strip_out_of_kb_claims(text, _detections(PIPL_QUESTION))
    if removed != 1:
        return False, f"期望删 1 句，实际删 {removed} 句：{cleaned!r}"
    if "合法性基础" in cleaned:
        return False, f"转述内容没删干净：{cleaned!r}"
    if "数据加密" not in cleaned:
        return False, f"无关句子被误删：{cleaned!r}"
    return True, f"删掉 {removed} 句，保留了无关内容"


@case("只提到法规名（没有转述内容）不删")
def test_strip_keeps_mention_only():
    # 服务端自己会补一段"未收录"的说明，所以"只是提到名字"必须留得住，
    # 否则用户看到的就是一句光秃秃的"我无法回答"。
    text = "知识库未收录《个人信息保护法》，建议咨询专业律师。"
    cleaned, removed = guard.strip_out_of_kb_claims(text, _detections(PIPL_QUESTION))
    if removed != 0 or cleaned != text:
        return False, f"不该删，实际删了 {removed} 句，结果 {cleaned!r}"
    return True, "仅提及法规名的句子被完整保留"


@case("同一行里只删转述句，保留其余分句")
def test_strip_sentence_level():
    text = "《网络安全法》要求日志留存六个月。我们建议您先梳理现有日志。"
    cleaned, removed = guard.strip_out_of_kb_claims(text, _detections("《网络安全法》要求什么？"))
    if removed != 1:
        return False, f"期望删 1 句，实际 {removed}：{cleaned!r}"
    if "六个月" in cleaned:
        return False, f"转述内容残留：{cleaned!r}"
    if "梳理现有日志" not in cleaned:
        return False, f"同行的建议被连带删掉：{cleaned!r}"
    return True, f"按句删除，保留同行其余内容：{cleaned!r}"


@case("没有命中库外法规时原文原样返回")
def test_strip_noop():
    text = "等保2.0 的 8.1.4.1 要求建立安全管理制度。"
    cleaned, removed = guard.strip_out_of_kb_claims(text, [])
    if removed != 0 or cleaned != text:
        return False, f"空 detections 时应原样返回，实际 {cleaned!r}"
    cleaned2, removed2 = guard.strip_out_of_kb_claims(text, _detections("系统日志要保存多久？"))
    if removed2 != 0 or cleaned2 != text:
        return False, f"无关问题时不该改动文本，实际 {cleaned2!r}"
    return True, "空列表与无关问题两种情况下都原样返回"


@case("简称与全称都能用来匹配被转述的法规")
def test_strip_matches_short_name():
    # scope 里的显示名是"《中华人民共和国个人信息保护法》（PIPL）"，
    # 而模型正文只会写"《个人信息保护法》"或"PIPL"——三种写法都要能匹配。
    for body in (
        "《个人信息保护法》第 5 条明确了合法正当必要原则。",
        "PIPL 要求取得个人同意。",
        "个人信息保护法规定了告知义务。",
    ):
        cleaned, removed = guard.strip_out_of_kb_claims(body, _detections(PIPL_QUESTION))
        if removed != 1:
            return False, f"{body!r} 未被清理（removed={removed}）：{cleaned!r}"
    return True, "全称、书名号简称、英文缩写三种写法均被清理"


# --------------------------------------------------------------------------
# detect_risks —— ⚠️ 误标回归
# --------------------------------------------------------------------------
# 这一组假的条款就是复现线上情形：条款标题是"安全审计"，
# 但正文里顺带提到"个人信息"、关键词里带着"留存期限"（审计留存和个人信息留存
# 都会用到这个词），模型又把三类风险全报了一遍。
AUDIT_CLAUSES = [
    {
        "clause_id": "8.1.4.8",
        "title": "安全审计（审计范围与记录要素）",
        "risk_tags": ["audit_log"],
        "keywords": ["安全审计", "审计记录"],
        "text": "应对用户行为、系统运行状态进行审计，审计记录应包含个人信息相关操作的记录。",
    },
    {
        "clause_id": "8.1.3.14",
        "title": "安全审计（物理与边界审计记录留存）",
        "risk_tags": ["audit_log"],
        "keywords": ["安全审计", "留存期限"],
        "text": "审计记录的留存期限应不少于六个月。",
    },
]


@case("问『安全审计』不该被标『个人信息保护』（真实误标回归）")
def test_no_false_personal_info():
    risks = guard.detect_risks(
        "等保2.0里关于安全审计的要求是什么？",
        AUDIT_CLAUSES,
        list(config.HIGH_RISK_TAGS),  # 模拟"见谁都标"的模型
        cited_ids=[c["clause_id"] for c in AUDIT_CLAUSES],
    )
    got = _tags(risks)
    if "personal_info" in got:
        return False, f"仍然误标 personal_info（完整结果 {got}）"
    if "encryption" in got:
        return False, f"仍然误标 encryption（完整结果 {got}）"
    return True, f"模型全报三类，实际只标出 {got or '（无）'}"


@case("问『日志留存』不该被标『数据加密』")
def test_no_false_encryption():
    risks = guard.detect_risks(
        "系统日志要保存多久，有没有明确规定？",
        AUDIT_CLAUSES,
        list(config.HIGH_RISK_TAGS),
        cited_ids=[c["clause_id"] for c in AUDIT_CLAUSES],
    )
    got = _tags(risks)
    if "encryption" in got:
        return False, f"误标 encryption（完整结果 {got}）"
    return True, f"未误标 encryption，实际 {got or '（无）'}"


@case("该标的还是要标：问题里直接提到个人信息")
def test_question_evidence_still_works():
    risks = guard.detect_risks(
        "用户的手机号和身份证号存在数据库里，要注意什么？",
        AUDIT_CLAUSES,
        [],
    )
    if "personal_info" not in _tags(risks):
        return False, f"问题里明说了手机号/身份证，却没标 personal_info：{_tags(risks)}"
    return True, f"证据2（问题命中）正常生效：{_tags(risks)}"


@case("该标的还是要标：被引用条款自带风险标签")
def test_clause_tag_evidence_still_works():
    clauses = [
        {
            "clause_id": "8.1.4.5",
            "title": "身份鉴别",
            "risk_tags": ["access_control"],
            "keywords": ["身份鉴别"],
            "text": "应对登录用户进行身份标识和鉴别。",
        }
    ]
    risks = guard.detect_risks("怎么防止别人冒用我的账号？", clauses, [], cited_ids=["8.1.4.5"])
    if "access_control" not in _tags(risks):
        return False, f"条款自带 access_control 标签却没标：{_tags(risks)}"
    return True, f"证据3（条款标签）正常生效：{_tags(risks)}"


@case("条款标题是最后的佐证依据")
def test_title_evidence_still_works():
    # 用户问得很含糊、问题里没有风险词，但检索到的条款标题明摆着是加密条款，
    # 这时模型自报 encryption 应该被采纳——收窄语料不能把这条路也堵死。
    clauses = [
        {
            "clause_id": "8.1.4.10",
            "title": "数据保密性（传输加密）",
            "risk_tags": [],
            "keywords": ["保密性"],
            "text": "应采用密码技术保证数据在传输过程中的保密性。",
        }
    ]
    risks = guard.detect_risks("这块要注意什么？", clauses, ["encryption"], cited_ids=["8.1.4.10"])
    if "encryption" not in _tags(risks):
        return False, f"条款标题含『加密』却没采纳模型自报：{_tags(risks)}"
    return True, f"证据1（标题佐证）正常生效：{_tags(risks)}"


@case("没有佐证时，模型自报一律不采纳")
def test_no_evidence_no_tag():
    clauses = [
        {
            "clause_id": "8.1.4.1",
            "title": "安全管理制度（总体要求）",
            "risk_tags": ["governance"],
            "keywords": ["管理制度"],
            "text": "应建立安全管理制度。",
        }
    ]
    risks = guard.detect_risks("我们公司需要建立哪些制度？", clauses, list(config.HIGH_RISK_TAGS))
    got = _tags(risks)
    if got:
        return False, f"无任何佐证却标出了 {got}"
    return True, "三类全报但无佐证，最终一个都没标"


# --------------------------------------------------------------------------
# 诚实层回归：P0-5（编号错配）/ H-1（按行清理）/ H-2（只查问题）/ H-4（繁体全角）
# --------------------------------------------------------------------------
def _clause(cid: str, title: str = "测试条款") -> dict:
    return {
        "clause_id": cid,
        "title": title,
        "standard_name": "等保2.0",
        "chapter_path": [],
        "text": "x",
        "text_type": "summary",
        "risk_tags": [],
    }


@case("库外法规名与条文分行时，条文也要被清掉（H-1 回归）")
def test_strip_multiline_bypass():
    # 旧实现按「行」判断：第一行有法规名但没有转述动词 → 保留；
    # 第二行有「第13条」但没有法规名 → 也保留。
    # 于是服务端一边说"我不会转述这部法规"，一边把转述原样留在下面。
    text = (
        "《个人信息保护法》的要求如下：\n"
        "第13条规定，处理个人信息应当取得个人同意。\n"
        "我们的系统还需要关注数据加密。"
    )
    cleaned, removed = guard.strip_out_of_kb_claims(text, _detections(PIPL_QUESTION))
    if removed < 1:
        return False, f"一句都没删：{cleaned!r}"
    if "取得个人同意" in cleaned:
        return False, f"分行的条文整段幸存（旧实现的漏洞）：{cleaned!r}"
    if "数据加密" not in cleaned:
        return False, f"无关内容被误删：{cleaned!r}"
    return True, f"删掉 {removed} 句，分行写的条文已清掉，无关内容保留"


@case("繁体和全角写法同样能被清理（H-4 回归）")
def test_strip_traditional_fullwidth():
    body = "個人信息保護法第１７條規定，處理個人信息應取得同意。"
    cleaned, removed = guard.strip_out_of_kb_claims(body, _detections(PIPL_QUESTION))
    if removed != 1 or "取得同意" in cleaned:
        return False, f"繁体/全角写法漏过了：removed={removed} {cleaned!r}"
    return True, "繁体法规名 + 全角条号写的转述被清掉"


@case("引用编号只按点号边界做唯一前缀展开（P0-5 回归）")
def test_citation_prefix_boundary():
    # 8.1.4.1 不能"展开"成 8.1.4.11：卡片显示 8.1.4.11、正文写着 8.1.4.1，
    # 等于系统替模型把编造的编号圆了回去。
    clauses = [_clause("8.1.4.11"), _clause("8.1.4.2")]
    valid, invalid = guard.validate_citations(["8.1.4.1"], clauses)
    if valid or invalid != ["8.1.4.1"]:
        return False, f"8.1.4.1 被错配到了 {[c['clause_id'] for c in valid]}"

    # 唯一、且扩展点落在点号边界上时才展开：8.1.4 → 8.1.4.7
    valid2, invalid2 = guard.validate_citations(["8.1.4"], [_clause("8.1.4.7")])
    if len(valid2) != 1 or valid2[0]["clause_id"] != "8.1.4.7" or invalid2:
        return False, (
            f"唯一前缀没有展开：valid={[c['clause_id'] for c in valid2]} "
            f"invalid={invalid2}"
        )

    # 候选不唯一时不猜（宁可这条引用不显示）
    valid3, invalid3 = guard.validate_citations(["8.1.4"], clauses)
    if valid3 or invalid3 != ["8.1.4"]:
        return False, f"候选不唯一却猜了：{[c['clause_id'] for c in valid3]}"
    return True, "8.1.4.1 不再错配 8.1.4.11；唯一前缀可展开；有歧义时不猜"


@case("正文编号核对不再被『包含关系』洗白（P0-5 回归）")
def test_hallucinated_boundary():
    clauses = [_clause("8.1.4.11")]
    if "8.1.4.1" not in guard.detect_hallucinated_ids("按 8.1.4.1 的规定执行。", clauses):
        return False, "8.1.4.1 被 8.1.4.11 吸收了，真正的编造漏报"
    if guard.detect_hallucinated_ids("按 8.1.4 的规定执行。", clauses):
        return False, "少写一段的 8.1.4 被误报成编造"
    return True, "8.1.4.1 不再被 8.1.4.11 吸收；8.1.4 仍算同一个编号"


@case("模型自发引用库外法规：答案侧也要检测并清理（H-2 回归）")
def test_answer_side_detection():
    answer = (
        "根据《网络安全法》第21条，网络运营者应当按照规定履行安全保护义务。\n"
        "我们的系统还需要关注数据加密。"
    )
    # 生产路径现在同时把问题和答案喂给 detect_out_of_kb（见 routers/chat.py）
    det = scope.detect_out_of_kb(answer)
    if not any("网络安全法" in d["name"] for d in det):
        return False, f"答案里的《网络安全法》没被识别：{det}"
    cleaned, removed = guard.strip_out_of_kb_claims(answer, det)
    if "安全保护义务" in cleaned or "第21条" in cleaned:
        return False, f"答案里的转述没被清掉：{cleaned!r}"
    if "数据加密" not in cleaned:
        return False, f"无关内容被误删：{cleaned!r}"
    return True, f"答案侧的库外法规被识别，并清掉 {removed} 句转述"


@case("回答里出现清单之外的法规名：结构兜底也能揪出来（F2 回归）")
def test_generic_law_name_fallback():
    known = scope._ALL_ALIASES
    text = (
        "根据《中华人民共和国反电信网络诈骗法》第十条，"
        "以及《网络数据安全管理条例》第五条，应当建立相应制度。"
    )
    got = guard.detect_unsupported_law_names(text, known)
    expected = ["中华人民共和国反电信网络诈骗法", "网络数据安全管理条例"]
    missing = [n for n in expected if n not in got]
    if missing:
        return False, f"清单外法规名漏检：{missing}（got={got}）"
    # 清单里已有的法规、知识库内的标准，不该被重复报成"清单外"
    ok_text = (
        "《网络安全法》见 8.1.4.1；"
        "《信息安全技术 网络安全等级保护基本要求》属于库内标准。"
    )
    if guard.detect_unsupported_law_names(ok_text, known):
        return False, "已知法规/库内标准被误报成清单外"
    return True, f"识别 {len(got)} 个清单外法规名；已知名称与库内标准不误报"


@case("等级结论兜底：主语指向用户才删，知识型句子不误伤")
def test_strip_level_conclusions() -> tuple[bool, str]:
    cases = [
        ("你们这套系统应当按第三级建设。", True),
        ("具体来说，贵司应按照三级要求做整改。", True),
        ("这套系统属于第三级。", True),
        ("你们需要了解三级的要求，再决定怎么做。", False),
        ("等保三级有哪些要求？第三级里关于审计日志是怎么规定的？", False),
        ("同一局域网内应部署审计设备。", False),
    ]
    bad = []
    for text, expect in cases:
        _, hit = guard.strip_level_conclusions(text)
        if hit != expect:
            bad.append((text, expect, hit))
    if bad:
        return False, f"判定不符：{bad}"
    cleaned, hit = guard.strip_level_conclusions("你们应当按三级建设。")
    if not hit or "不替您下这个结论" not in cleaned:
        return False, f"删句后没有中性说明：{cleaned!r}"
    return True, f"{len(cases)} 条判据全部符合；命中时替换为中性说明"


@case("法规别名：台湾用语 / 私隐条例 / PDPO / 点号 / 不可见分隔符都能识别")
def test_law_alias_normalization() -> tuple[bool, str]:
    samples = [
        ("個資保護法第13條怎麼寫", "个人信息保护法"),
        ("《个人\u2063信息·保护法》要求什么", "个人信息保护法"),
        ("PDPO 的規定", "私隐"),
        ("「個人資料保護法」適用嗎", "个人信息保护法"),
    ]
    bad = []
    for q, expect in samples:
        names = [d["name"] for d in scope.detect_out_of_kb(q)]
        if not any(expect in n for n in names):
            bad.append((q, expect, names))
    if bad:
        return False, f"别名漏检：{bad}"
    det = scope.detect_out_of_kb("《个人信息·保护法》怎么规定")
    cleaned, removed = guard.strip_out_of_kb_claims(
        "《个人·信息保护法》规定：应当取得同意。", det
    )
    if removed < 1 or "取得同意" in cleaned:
        return False, f"被点号拆开的转述未清掉：removed={removed} cleaned={cleaned!r}"
    return True, f"{len(samples)} 种写法全部识别；回答侧转述已清理"


def main() -> int:
    print("=" * 70)
    print("输出守卫测试 · 不依赖 Ollama 与数据库")
    print("=" * 70)

    passed = failed = 0
    for name, fn in RESULTS:
        try:
            ok, msg = fn()
        except Exception as exc:
            ok, msg = False, f"测试本身出错：{type(exc).__name__}: {exc}"
        print(f"{'OK ' if ok else 'XX '}{name}")
        print(f"     {msg}")
        if ok:
            passed += 1
        else:
            failed += 1

    print("=" * 70)
    print(f"通过 {passed} / {passed + failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
