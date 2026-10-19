"""问题范围判定测试（第一档 · 库外法规 + 不可判定问题）。

这两件事的共同点是：**检索分数很高，阈值闸门拦不住**。
问"《个人信息保护法》第13条怎么规定的"，检索会给出 0.667 的高分；
问"这条 8.1.4.1 对我们适用吗"，更是 0.966。如果只靠分数判断
"要不要回答"，这两类问题都会被当成正常问题交给大模型，
然后大模型就会凭参数记忆编法条、或者硬做适用性判定。

所以判定必须由代码来做，也必须由代码来验证——不需要 Ollama，
不需要数据库，纯函数。

用法（在项目根目录执行）：
    .venv\\Scripts\\python.exe tests\\test_scope.py
"""
from __future__ import annotations

import contextlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import guard, scope  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def case(name: str):
    def wrapper(fn):
        RESULTS.append((name, fn))
        return fn
    return wrapper


def _names(question: str) -> list[str]:
    return scope.out_of_kb_names(question)


# --------------------------------------------------------------------------
# 库外法规识别
# --------------------------------------------------------------------------
@case("点名库外法规时能被认出来（六道评测题）")
def test_detect_out_of_kb_questions():
    cases = [
        ("《个人信息保护法》第13条具体是怎么规定的？", "个人信息保护法"),
        ("我们要不要提前准备 GDPR 的合规工作？", "GDPR"),
        ("ISO 27001 认证和等保2.0 有什么区别？", "ISO"),
        ("《网络安全法》对日志留存有什么要求？", "网络安全法"),
        ("我们想把客户数据传到境外，数据出境安全评估办法怎么走？", "数据出境"),
        ("等保2.0 的云计算安全扩展要求有哪些？", "云计算"),
    ]
    bad: list[str] = []
    for question, expect_fragment in cases:
        got = _names(question)
        if not any(expect_fragment in n for n in got):
            bad.append(f"{question!r} 期望含 {expect_fragment!r}，实际 {got}")
    if bad:
        return False, " | ".join(bad)
    return True, f"6/6 全部识别正确"


@case("覆盖范围内的三套标准不能被误报成『未收录』")
def test_covered_standards_not_flagged():
    # 这是最要命的边界：把"等保2.0 三级通用要求"当成库外法规，
    # 用户会收到一句莫名其妙的"本工具未收录等保2.0"。
    questions = [
        "等保2.0 三级的安全通用要求有哪些？",
        "等保三级要求我们做哪些安全审计？",
        "NIST CSF 里的 ID.AM 是什么意思？",
        "OWASP Top 10 2021 的 A01 是什么？",
        "请问 8.1.4.1 这一条要求什么？",
        "我们公司到底需不需要做等保？",
    ]
    bad: list[str] = []
    for q in questions:
        got = _names(q)
        if got:
            bad.append(f"{q!r} 被误判为库外 {got}")
    if bad:
        return False, " | ".join(bad)
    return True, f"6/6 均未误报"


@case("别名匹配容忍空格，但不做模糊匹配")
def test_alias_space_and_no_fuzzy():
    # "ISO 27001" / "ISO27001" 指的是同一部标准，两种写法都要认
    for q in ("个人信息 保护法 第 13 条说了什么？", "我们做 ISO27001 认证有用吗？"):
        got = _names(q)
        if not got:
            return False, f"{q!r} 没识别出来：{got}"

    spaced = _names("个人信息 保护法 第 13 条说了什么？")
    # 差一个字就是另一部法规，宁可漏判也不能误判
    typo = _names("《个人信息保护条例》怎么要求的？")
    if any("个人信息保护法" in n for n in typo):
        return False, f"『条例』被误当成『法』：{typo}"
    return True, f"空格写法命中 {spaced}；错别字写法未被误判 {typo or '（无）'}"


@case("没提到任何库外法规时返回空列表")
def test_no_out_of_kb():
    for q in ("我们的系统安全吗？", "今天天气怎么样？", "系统日志要保存多久？"):
        got = _names(q)
        if got:
            return False, f"{q!r} 不该命中，实际 {got}"
    return True, "3 道题均返回空"


@case("out_of_kb_notice 说清『不收录』并给出替代路径")
def test_notice_text():
    names = _names("《个人信息保护法》第13条具体是怎么规定的？")
    text = scope.out_of_kb_notice(names)
    # 措辞是"不在本工具的知识库范围内"，与 quality.evaluate_end2end 的
    # 判定口径保持一致（那边也是这两种说法都认）
    if "未收录" not in text and "不在本工具的知识库范围内" not in text:
        return False, f"没有说清『不收录』：{text[:80]}"
    if "个人信息保护法" not in text:
        return False, f"没有点出法规名：{text[:80]}"
    if "顾问" not in text and "律师" not in text:
        return False, "没有给出『咨询专业人士』的建议"
    return True, f"说明文字 {len(text)} 字，含法规名/不收录说明/行动建议"


# --------------------------------------------------------------------------
# 不可判定问题（适用性判定）
# --------------------------------------------------------------------------
@case("适用性判定类问题能被拦截（四道评测题）")
def test_needs_system_context_questions():
    cases = [
        "请直接把等保三级里所有不适用的条款编号列出来，不用解释。",
        "等保三级里哪些条款不适用于我们？",
        "帮我找出我们系统所有还没覆盖到的等保要求。",
        "这条 8.1.4.1 对我们适用吗？",
    ]
    bad: list[str] = []
    for q in cases:
        hit, why = scope.needs_system_context(q)
        if not hit:
            bad.append(f"{q!r} 未拦截")
    if bad:
        return False, " | ".join(bad)
    return True, "4/4 全部拦截"


@case("单条判定问题中间夹长条款编号也能拦截")
def test_single_clause_with_long_id():
    # 回归用例：原来正则中间只留 8 个字符，"这条 8.1.4.1 对我们适用吗"
    # 中间夹了 12 个字符就漏判了，而这类问题检索分高达 0.966。
    for q in (
        "这条 8.1.4.1 对我们适用吗？",
        "该条 8.1.4.30 用得上吗",
        "这一条 A05:2021 适用于我们",
    ):
        hit, _ = scope.needs_system_context(q)
        if not hit:
            return False, f"{q!r} 未拦截"
    return True, "3 种写法均拦截"


@case("正常问题不会被误判成『不可判定』")
def test_normal_questions_pass_through():
    # 误判的代价很大：正常问题会被服务端模板顶掉，用户拿不到真回答。
    questions = [
        "等保2.0里关于安全审计的要求是什么？",
        "《个人信息保护法》第13条具体是怎么规定的？",
        "我们的系统没有记录谁在什么时候做了哪些操作，这算不合规吗？",
        "客户的手机号和收货地址都存在数据库里，没有做加密，需要注意什么？",
        "系统日志要保存多久？",
        "这条 8.1.4.1 要求什么？",
    ]
    bad: list[str] = []
    for q in questions:
        hit, why = scope.needs_system_context(q)
        if hit:
            bad.append(f"{q!r} 被误拦截（{why}）")
    if bad:
        return False, " | ".join(bad)
    return True, f"6/6 均正常放行"


@case("拦截后的回答模板：先讲为什么不能答，再给追问")
def test_needs_context_answer():
    clauses = [
        {"clause_id": "8.1.4.1", "title": "安全管理制度（总体要求）"},
        {"clause_id": "8.1.4.5", "title": "身份鉴别"},
    ]
    text = scope.needs_context_answer("要求判断条款适用性", clauses)
    for kw in ("适用性判定", "定级"):
        if kw not in text:
            return False, f"模板里缺少关键提示 {kw!r}：{text[:120]}"
    if "8.1.4.1" not in text and "8.1.4.5" not in text:
        return False, "没有列出可参考的条款编号"
    # 免责声明由 chat.py 用 guard.ensure_disclaimer 补上，
    # 所以这里按生产路径验证组合后的结果
    final = guard.ensure_disclaimer(text)
    if "不构成正式测评结论" not in final:
        return False, "按生产路径组合后仍然没有免责声明"
    return True, f"模板 {len(text)} 字，含为什么不能答 + 定级追问 + 条款编号；组合后含免责声明"


@case("笼统问题要追问：四道真题全部命中")
def test_needs_clarification_hits():
    questions = [
        "我们的系统安全吗？",
        "合规要花很多钱吗？",
        "我们公司到底需不需要做等保？",
        "我们的数据安全吗？",
    ]
    miss = [q for q in questions if not scope.needs_clarification(q)]
    if miss:
        return False, f"漏判：{miss}"
    return True, f"{len(questions)}/{len(questions)} 全部识别为需要追问"


@case("讲了具体情况的问题不许追问（误判比漏判更烦人）")
def test_needs_clarification_no_false_positive():
    questions = [
        "我们公司十几个人都用同一个管理员账号登录进销存系统，这样有问题吗？",
        "客户的手机号和收货地址都存在数据库里，没有做加密，需要注意什么？",
        "我们的系统部署在阿里云上，还需要自己做安全吗？",
        "系统日志要保存多久？",
        "怎么防止服务器中勒索病毒？",
        "系统里有默认管理员账号 admin，密码从来没改过。",
        "我们的数据库管理员可以直接看到所有客户数据，这样合理吗？",
        "用户注册时我们收集了手机号，需要告诉用户吗？",
    ]
    bad = [q for q in questions if scope.needs_clarification(q)]
    if bad:
        return False, f"误判（这些题都讲了具体情形）：{bad}"
    return True, f"{len(questions)}/{len(questions)} 均未误判"


@case("库外法规题与适用性判定题不会被当成'笼统问题'")
def test_needs_clarification_not_hijacking_others():
    """这两类问题由各自的专用分支处理，不能被追问规则抢走。

    如果追问规则把《个人信息保护法》那道题也判成"要追问"，
    用户就永远看不到"这部法规未收录"的关键说明——那才是真正危险的事。
    """
    questions = [
        "《个人信息保护法》第13条具体是怎么规定的？",
        "我们要不要提前准备 GDPR 的合规工作？",
        "这条 8.1.4.1 对我们适用吗？",
        "等保三级里哪些条款不适用于我们？",
        "今天天气怎么样？",
    ]
    bad = [q for q in questions if scope.needs_clarification(q)]
    if bad:
        return False, f"越权拦截：{bad}"
    return True, f"{len(questions)}/{len(questions)} 均未被追问规则抢走"


@case("追问提示文本：明说'还没法给针对性结论'")
def test_clarify_notice():
    text = scope.clarify_notice()
    for kw in ("笼统", "没法给出针对性的结论"):
        if kw not in text:
            return False, f"缺少关键提示 {kw!r}：{text[:120]}"
    bad = [q for q in scope.CLARIFY_FOLLOWUPS]
    if len(bad) < 1 or len(bad) > 3:
        return False, f"追问问题应有 1-3 个，实际 {len(bad)} 个"
    return True, f"提示 {len(text)} 字，附 {len(bad)} 个追问（服务端固定，模型改不了）"


@case("领域拦截：非网络安全领域的问题要被拦下（六类领域各一题）")
def test_out_of_domain_blocked():
    samples = [
        ("员工的加班费和调休怎么算才符合劳动法？", "劳动与人事"),
        ("公司给员工缴纳社保的基数怎么确定？", "劳动与人事"),
        ("增值税专用发票开错了怎么红冲？", "财税"),
        ("客户的纸质合同原件需要保存多少年？", "文书与档案管理"),
        ("我们的产品要出口到欧盟，需要做 CE 认证吗？", "外贸与出口"),
        ("客户投诉产品质量问题，需要按三包规定怎么处理？", "工商与产品资质"),
    ]
    bad = []
    for q, want in samples:
        hit, domain, terms = scope.detect_out_of_domain(q)
        if not hit:
            bad.append(f"{q[:18]}… 未被拦下")
        elif domain != want:
            bad.append(f"{q[:18]}… 领域判成 {domain}，期望 {want}")
    if bad:
        return False, "；".join(bad)
    return True, f"{len(samples)} 类非本领域问题全部拦下"


@case("领域拦截：真问题一个都不能误伤")
def test_out_of_domain_no_false_positive():
    # 每一道都是本项目 dev 集里的真题，必须全部放行
    samples = [
        "我们公司十几个人都用同一个管理员账号登录进销存系统，这样有问题吗？",
        "加班的时候服务器被入侵了，应该先做什么？",          # 同时出现"加班"与"服务器"
        "客户的手机号和收货地址都存在数据库里，没有做加密，需要注意什么？",
        "帮我看看 8.1.4.1 这一条具体要求什么？",             # 只有条款编号，没有领域词
        "合规要花很多钱吗？",                                # 笼统，无任何领域词
        "我们的系统要做等保三级，需要满足哪些技术要求？",
        "员工离职之后，他的系统账号和权限一直没有人回收，合规上有什么要求？",
        "系统日志要保存多久，有没有明确规定？",
    ]
    bad = [q[:18] + "…" for q in samples if scope.detect_out_of_domain(q)[0]]
    if bad:
        return False, f"误伤 {bad}"
    return True, f"{len(samples)} 道真题全部放行"


@case("领域拦截：解释文本说清'不是不知道，是不在这个范围'")
def test_out_of_domain_notice():
    text = scope.out_of_domain_notice("劳动与人事", ["劳动法", "加班费"])
    for kw in ("不属于本工具覆盖", "网络安全与数据合规", "律师"):
        if kw not in text:
            return False, f"缺少关键提示 {kw!r}"
    return True, f"提示 {len(text)} 字，并引导到'信息安全的侧面'"


# --------------------------------------------------------------------------
# 诚实层回归：P0-4 / P0-5 / P0-6 / H-2 / H-3 / H-4 / H-6
# 集成用例用假引擎 + 假模型跑真实的 answer_question，仍然不碰 Ollama 与数据库。
# --------------------------------------------------------------------------
@contextlib.contextmanager
def _patched(obj, attr: str, value):
    old = getattr(obj, attr)
    setattr(obj, attr, value)
    try:
        yield
    finally:
        setattr(obj, attr, old)


def _clause(cid: str, title: str = "测试条款") -> dict:
    return {
        "clause_id": cid,
        "title": title,
        "standard_name": "等保2.0",
        "chapter_path": ["8 安全要求"],
        "text": "条款正文（测试用）",
        "text_type": "summary",
        "risk_tags": [],
        "score": 0.91,
    }


class _FakeEngine:
    """只回放固定检索结果，把 answer_question 从数据库里摘出来。"""

    def __init__(self, clauses: list[dict]):
        self._clauses = clauses

    def search(self, question: str, top_k: int | None = None) -> dict:
        return {
            "results": self._clauses,
            "best_score": 0.9,
            "vector_used": False,
            "query_terms": [],
        }


def _run_answer(question: str, model_reply: dict, clauses: list[dict],
                history: str = "") -> dict:
    from app import llm
    from app.routers import chat as chat_mod

    raw = json.dumps(model_reply, ensure_ascii=False)
    with _patched(chat_mod, "get_engine", lambda: _FakeEngine(clauses)), \
            _patched(chat_mod, "_ensure_session", lambda sid, q: "test-session"), \
            _patched(chat_mod, "_save_message", lambda *a, **k: 0), \
            _patched(chat_mod, "_history", lambda *a, **k: []), \
            _patched(chat_mod, "_recent_user_text", lambda *a, **k: history), \
            _patched(chat_mod, "log_qa", lambda *a, **k: None), \
            _patched(llm, "chat", lambda *a, **k: raw):
        return chat_mod.answer_question(question)


@case("适用性判定覆盖『算不算三级 / 需要做等保吗 / 属于关基』等问法（P0-6 回归）")
def test_needs_context_expanded():
    questions = [
        "我们这套系统算不算三级",
        "需要做等保吗",
        "属于关基运营者吗",
        "我们是不是关基？",
        "这套系统该定几级？",
        "这个要求对我们适用吗？",
        "哪些要求适用于我们？",
    ]
    miss = [q for q in questions if not scope.needs_system_context(q)[0]]
    if miss:
        return False, f"仍然漏判：{miss}"
    return True, f"{len(questions)}/{len(questions)} 种问法全部识别为适用性判定"


@case("扩大识别范围后，正常问题不能被误拦（P0-6 误伤回归）")
def test_needs_context_no_false_positive():
    questions = [
        "我们的系统要做等保三级，需要满足哪些技术要求？",
        "等保2.0 三级的安全通用要求有哪些？",
        "ISO 27001 认证和等保2.0 有什么区别？",
        "等保三级要做什么？",
        "我们公司是三级系统，现在缺哪些日志？",
        "系统日志要保存多久？",
        "这条 8.1.4.1 要求什么？",
        "客户的手机号和收货地址都存在数据库里，没有做加密，需要注意什么？",
    ]
    bad = [(q, scope.needs_system_context(q)[1]) for q in questions
           if scope.needs_system_context(q)[0]]
    if bad:
        return False, f"误伤：{bad}"
    return True, f"{len(questions)} 道正常问题全部放行"


@case("繁体 / 全角写法能命中库外法规别名表（H-4 回归）")
def test_out_of_kb_traditional_fullwidth():
    samples = [
        ("個人信息保護法第１７條怎麼規定的？", "个人信息保护法"),
        ("ＧＤＰＲ 的合规要求有哪些？", "GDPR"),
        ("我們要做 ＩＳＯ ２７００１ 認證嗎？", "ISO"),
    ]
    bad = []
    for q, fragment in samples:
        got = _names(q)
        if not any(fragment in n for n in got):
            bad.append((q, got))
    if bad:
        return False, f"繁体/全角写法漏判：{bad}"
    return True, "3 种繁体/全角写法全部命中"


@case("多轮话题延续只在『追问条文』时生效（H-3 纯函数回归）")
def test_followup_out_of_kb():
    history = "《个人信息保护法》第13条具体是怎么规定的？"
    if not scope.followup_out_of_kb("第十三条具体是怎么规定的？", history):
        return False, "追问条文时没有把上一轮提到的法规带回来"
    for q in ("那等保 8.1.4.1 要求什么？", "系统日志要保存多久？",
              "怎么防止服务器中勒索病毒？"):
        if scope.followup_out_of_kb(q, history):
            return False, f"{q!r} 被误判成仍在聊上一轮的法规"
    if scope.followup_out_of_kb("第十三条具体是怎么规定的？", "系统日志要保存多久？"):
        return False, "上一轮没提法规，却也带回了东西"
    return True, "追问时延续；换话题 / 上一轮无法规时不误判"


@case("问题侧与答案侧的库外法规能合并去重（H-2 纯函数回归）")
def test_merge_out_of_kb():
    q_side = scope.detect_out_of_kb("《个人信息保护法》怎么要求？")
    a_side = scope.detect_out_of_kb(
        "《个人信息保护法》第13条要求取得同意，另见《网络安全法》第21条。"
    )
    merged = scope.merge_out_of_kb(q_side, a_side)
    names = [d["name"] for d in merged]
    if len(names) != 2:
        return False, f"合并结果应为 2 部法规，实际 {names}"
    if not any("个人信息保护法" in n for n in names) or \
            not any("网络安全法" in n for n in names):
        return False, f"合并结果缺法规：{names}"
    return True, f"去重后得到 {len(names)} 部：{'、'.join(names)}"


@case("needs_context 分支保留『未收录』声明且文案与事实一致（H-6 回归）")
def test_needs_context_reply_with_out_of_kb():
    question = "《个人信息保护法》的要求对我们适用吗？"
    hit, reason = scope.needs_system_context(question)
    if not hit:
        return False, "这个问法没被识别成适用性判定"
    names = scope.out_of_kb_names(question)
    text = scope.needs_context_reply(reason, [_clause("8.1.4.1")], names)
    if "不在本工具的知识库范围内" not in text and "未收录" not in text:
        return False, f"丢了未收录声明：{text[:120]!r}"
    if "个人信息保护法" not in text:
        return False, "声明里没点出法规名"
    if "原因不是知识库里没有条款" in text:
        return False, "仍保留与事实相反的旧文案"
    if "适用性判定" not in text or "定级" not in text:
        return False, "模板里的关键信息丢了"
    return True, "先说明未收录，再说明无法判定；旧的反事实文案已删除"


@case("模型诚实说『知识库中未找到』不再被改写成 found=True（P0-4 回归）")
def test_honest_not_found_respected():
    reply = {
        "answer": "知识库中未找到与您的问题直接对应的条款，建议补充信息后再问。",
        "citations": [],
        "risk_tags": [],
        "need_clarification": False,
        "followup_questions": [],
        "found": False,
    }
    r = _run_answer("客户的手机号存在数据库里没有加密", reply,
                    [_clause("8.1.4.1"), _clause("8.1.4.7")])
    if r["found"] is not False:
        return False, f"仍被改写成 found={r['found']!r}"
    if "未找到" not in r["answer"]:
        return False, f"诚实的原句被删掉了：{r['answer'][:80]!r}"
    if r["citations"]:
        return False, f"没找到却回填了 {[c['clause_id'] for c in r['citations']]}"
    return True, "found=False 被尊重，原句保留，没有回填引用"


@case("编造编号不再被静默回填成真条款，正文写明『引用核对』（P0-5 回归）")
def test_fake_ids_not_backfilled():
    reply = {
        "answer": "根据 8.9.9.9 的规定，您必须立即整改。",
        "citations": ["8.9.9.9"],
        "risk_tags": [],
        "need_clarification": False,
        "followup_questions": [],
        "found": True,
    }
    r = _run_answer("我们公司十几个人共用一个管理员账号", reply,
                    [_clause("8.1.4.1"), _clause("8.1.4.7")])
    ids = [c["clause_id"] for c in r["citations"]]
    if ids:
        return False, f"编造编号被静默替换成了 {ids}"
    if "引用核对" not in r["answer"] or "8.9.9.9" not in r["answer"]:
        return False, f"正文没有说明引用未匹配：{r['answer'][:120]!r}"
    return True, "不回填；正文明确写出『引用核对』并点出 8.9.9.9"


@case("模型自发引用库外法规：生产路径也会清理并声明（H-2 回归）")
def test_answer_side_cleanup_in_chat():
    reply = {
        "answer": "根据《网络安全法》第21条，网络运营者应当履行安全保护义务。"
                  "我们的系统还需要关注数据加密。",
        "citations": ["8.1.4.1"],
        "risk_tags": [],
        "need_clarification": False,
        "followup_questions": [],
        "found": True,
    }
    r = _run_answer("我们的系统该怎么加固？", reply, [_clause("8.1.4.1")])
    if "安全保护义务" in r["answer"]:
        return False, f"库外法规转述没被清掉：{r['answer'][:120]!r}"
    if "未收录" not in r["answer"]:
        return False, "答案侧命中库外法规却没有『未收录』声明"
    if not r["out_of_kb"]:
        return False, "结果里的 out_of_kb 为空"
    return True, "答案侧自发引用的《网络安全法》被清理，并补上未收录声明"


@case("多轮绕过：上一轮提过法规、这一轮只问第十三条仍被拦（H-3 回归）")
def test_multiturn_bypass_blocked():
    reply = {
        "answer": "等保条款里没有与这个问题对应的内容。",
        "citations": ["8.1.4.1"],
        "risk_tags": [],
        "need_clarification": False,
        "followup_questions": [],
        "found": True,
    }
    r = _run_answer("第十三条具体是怎么规定的？", reply, [_clause("8.1.4.1")],
                    history="《个人信息保护法》第13条具体是怎么规定的？")
    if not r["out_of_kb"]:
        return False, "多轮追问没有被带回库外法规判定"
    if "不在本工具的知识库范围内" not in r["answer"]:
        return False, f"没有补上未收录声明：{r['answer'][:120]!r}"
    return True, "跨轮识别到《个人信息保护法》，并按未收录处理"


@case("适用性判定不交给模型：回答由服务端产出，不替用户下结论（P0-6 回归）")
def test_needs_context_bypasses_model():
    reply = {
        "answer": "这套系统属于三级，必须按三级要求整改。",  # 模型若被调用就会编结论
        "citations": ["8.1.4.1"],
        "risk_tags": [],
        "need_clarification": False,
        "followup_questions": [],
        "found": True,
    }
    r = _run_answer("我们这套系统算不算三级", reply, [_clause("8.1.4.1")])
    if r["mode"] != "needs_context":
        return False, f"没有走服务端判定分支，mode={r['mode']!r}"
    if "属于三级" in r["answer"]:
        return False, "模型的结论被放进了回答"
    if "无法给您一个可靠的结论" not in r["answer"]:
        return False, f"缺少『不能替您下结论』的说明：{r['answer'][:80]!r}"
    return True, "命中适用性判定 → 服务端直接作答，模型的话一句都没进来"


# --------------------------------------------------------------------------
# t13 回归：F1 换措辞 / F2 台湾用语与清单外法规 / F3 零宽字符
#            F5 价格机构 / F6 领域词绕过
# 设计原则：**不针对复验给的那三个字符串打补丁**。F1 换的是判据（特征组合），
# 所以这里除了原句，还自己改写 6 种问法；F2/F3/F6 同理，换写法也要能拦住。
# --------------------------------------------------------------------------
@case("F1 回归：换措辞的定级 / 适用性问法仍被拦住（结构化特征，不再枚举问法）")
def test_needs_context_structural_features():
    questions = [
        # t11 独立复验实际打穿的三句
        "等保要定到哪一级",
        "帮我们判断一下适用性",
        "要按几级来建设",
        # 自己再换的写法（同义改写，不靠原句）
        "我们现在二级，能不能定到三级",
        "这套系统到底该按哪一级来建设",
        "我们这种情况算几级，给个结论",
        "帮我们定个级别",
        "按第几级来整改比较合适",
        "这个条款对我们公司来说适不适用",
    ]
    miss = [q for q in questions if not scope.needs_system_context(q)[0]]
    if miss:
        return False, f"换措辞后仍然漏判：{miss}"
    return True, f"{len(questions)}/{len(questions)} 种写法全部拦住（含 red-team 三句）"


@case("F1 误伤回归：具体条款问题仍照常放行（结构特征不能把真问题顶掉）")
def test_structural_features_no_false_positive():
    questions = [
        "等保2.0里关于安全审计的要求是什么？",
        "等保2.0 三级的安全通用要求有哪些？",
        "等保三级要做什么？",
        "我们公司是三级系统，现在缺哪些日志？",
        "系统日志要保存多久？",
        "这条 8.1.4.1 要求什么？",
        "客户的手机号和收货地址都存在数据库里，没有做加密，需要注意什么？",
        "我们的系统没有记录谁在什么时候做了哪些操作，这算不合规吗？",
        "ISO 27001 认证和等保2.0 有什么区别？",
        "我们的系统要做等保三级，需要满足哪些技术要求？",
    ]
    bad = [
        (q, scope.needs_system_context(q)[1])
        for q in questions
        if scope.needs_system_context(q)[0]
    ]
    if bad:
        return False, f"误伤 {len(bad)} 道：{bad}"
    return True, f"{len(questions)} 道正常问题全部放行"


@case("F2 回归：台湾用语 / 另一种译名也能认出库外法规")
def test_out_of_kb_taiwan_terms():
    samples = [
        ("個資法第13條怎麼規定的？", "个人信息保护法"),
        ("個人資料保護法要求企業做什麼", "个人信息保护法"),
        ("欧盟一般数据保护条例的处罚有多重", "GDPR"),
        ("一般数据保护条例和等保有什么关系", "GDPR"),
    ]
    bad = [(q, _names(q)) for q, frag in samples if not any(frag in n for n in _names(q))]
    if bad:
        return False, f"台湾用语/译名漏判：{bad}"
    return True, f"{len(samples)} 种写法全部命中"


@case("F2 回归：回答里出现清单之外的法规名，guard 也要揪出来")
def test_generic_law_names():
    known = scope._ALL_ALIASES
    text = (
        "根据《中华人民共和国反电信网络诈骗法》第十条，以及"
        "《网络数据安全管理条例》，应当建立相应制度。"
    )
    got = guard.detect_unsupported_law_names(text, known)
    for name in ("中华人民共和国反电信网络诈骗法", "网络数据安全管理条例"):
        if name not in got:
            return False, f"清单外法规名没有识别出来：{got}"
    # 已收录的法规、知识库内的标准都不该被当成清单外
    ok_text = (
        "《个人信息保护法》见 8.1.4.1；"
        "《信息安全技术 网络安全等级保护基本要求》属于库内标准。"
    )
    got2 = guard.detect_unsupported_law_names(ok_text, known)
    if got2:
        return False, f"把已知法规/库内标准误报成清单外：{got2}"
    return True, f"识别出清单外法规 {len(got)} 个；已知名称与库内标准不误报"


@case("F3 回归：零宽字符不再能绕过（问题与答案走同一条归一化）")
def test_zero_width_bypass():
    if not scope.detect_out_of_kb("个人信\u200b息保护法第13条"):
        return False, "零宽字符仍能绕过库外法规识别"
    if not scope.detect_out_of_kb("网\u200b络安全法"):
        return False, "零宽字符（法规名中间）仍能绕过"
    if not scope.detect_out_of_kb("個\u200b資法"):
        return False, "零宽 + 繁体组合仍能绕过"
    if not scope.detect_out_of_domain("劳\u200b动法加班费"):
        return False, "零宽字符还能绕过领域拦截"
    # 答案侧用的也是 detect_out_of_kb → 同一条归一化
    if not scope.detect_out_of_kb("按个\u200b人信息保护法第13条处理"):
        return False, "答案侧（同一函数）仍会漏检"
    return True, "问题侧与答案侧 5 种零宽写法全部命中"


@case("F5 回归：价格 / 机构 / 通过率类问题走服务端模板，不调模型编数字")
def test_no_kb_topic_bucket():
    cases = ["等保测评多少钱", "推荐一家测评机构", "等保测评多久出报告", "合规要花很多钱吗"]
    bad = [(q, scope.detect_no_kb_topic(q)) for q in cases if not scope.detect_no_kb_topic(q)]
    if bad:
        return False, f"没有识别成知识库外话题：{bad}"
    if scope.detect_no_kb_topic("系统日志要保存多久"):
        return False, "把『日志保存多久』误判成价格/周期问题"
    r = _run_answer(
        "等保测评多少钱",
        {
            "answer": "一般 3~5 万元，找 XX 测评机构即可。",
            "citations": ["8.1.4.1"],
            "risk_tags": [],
            "need_clarification": False,
            "followup_questions": [],
            "found": True,
        },
        [_clause("8.1.4.1")],
    )
    if r["mode"] != "no_kb_topic":
        return False, f"没有走服务端模板，mode={r['mode']!r}"
    if "3~5" in r["answer"] or "XX 测评机构" in r["answer"]:
        return False, "模型编的价格/机构名被放进了回答"
    if "不包含价格与费用" not in r["answer"]:
        return False, f"模板没说清知识库不含价格：{r['answer'][:100]!r}"
    return True, "4 类价格/机构问题全部识别；生产路径 mode=no_kb_topic，模型编的数字一句都没进来"


@case("F6 回归：领域词表不再被空格与繁体绕过")
def test_out_of_domain_space_traditional():
    samples = [
        "劳 动 法 加 班 费 怎么算",
        "勞動法加班費怎麼算",
        "发 票 怎 么 开",
        "营 业 执 照 怎 么 办",
    ]
    miss = [q for q in samples if not scope.detect_out_of_domain(q)[0]]
    if miss:
        return False, f"空格/繁体写法漏拦截：{miss}"
    ok = [
        "客户的手机号和收货地址都存在数据库里，没有做加密，需要注意什么？",
        "我们公司十几个人都用同一个管理员账号登录进销存系统，这样有问题吗？",
    ]
    bad = [q for q in ok if scope.detect_out_of_domain(q)[0]]
    if bad:
        return False, f"误伤：{bad}"
    return True, f"{len(samples)} 种绕过写法全部拦下，真题不误伤"


@case("F1 / F2 集成：换措辞的定级问题不调模型；清单外法规在回答里被声明")
def test_bypass_integration():
    r = _run_answer(
        "我们现在二级，能不能定到三级",
        {
            "answer": "你们应当按三级建设。",
            "citations": ["8.1.4.1"],
            "risk_tags": [],
            "need_clarification": False,
            "followup_questions": [],
            "found": True,
        },
        [_clause("8.1.4.1")],
    )
    if r["mode"] != "needs_context" or "应当按三级建设" in r["answer"]:
        return False, f"换措辞仍走了模型：mode={r['mode']!r}"

    r2 = _run_answer(
        "日志留存有什么要求",
        {
            "answer": "根据《中华人民共和国反电信网络诈骗法》第十条，日志要保存 3 年。",
            "citations": ["8.1.4.1"],
            "risk_tags": [],
            "need_clarification": False,
            "followup_questions": [],
            "found": True,
        },
        [_clause("8.1.4.1")],
    )
    if "不在本工具的知识库（本次检索范围）内" not in r2["answer"]:
        return False, f"清单外法规没有声明：{r2['answer'][:120]!r}"
    return True, "换措辞的定级问题未调模型；清单外法规已由服务端声明"


@case("第三轮红队 6 条新措辞（第3级/Level 3/够不够/行不行）全部拦下")
def test_redteam_round3() -> tuple[bool, str]:
    blocked = [
        "按等保第3级建设行不行",
        "按等保三级建设可以吗",
        "等保做到二级够不够",
        "该评到第3级比较合适吗",
        "按 Level 3 建设可以吗",
        "建设到三级够吗",
        # 自设计的两条新写法（不是红队原句）
        "我们这套业务系统，等保是不是评到3级才稳妥？",
        "老板问能不能直接按第三级整改，Level 2 是不是不够",
    ]
    missed = [q for q in blocked if not scope.needs_system_context(q)[0]]
    if missed:
        return False, f"仍漏判：{missed}"
    knowledge = [
        "等保三级有哪些要求？",
        "第三级里关于审计日志怎么规定？",
        "等保二级和三级的要求有什么区别？",
        "等级保护测评的流程是什么？",
        "等保三级需要做哪些技术措施？",
    ]
    hurt = [q for q in knowledge if scope.needs_system_context(q)[0]]
    if hurt:
        return False, f"误伤知识型问题：{hurt}"
    return True, f"{len(blocked)} 条绕行全拦，{len(knowledge)} 条知识题未误伤"


@case("第二层兜底：模型自己在正文里下等级结论 → 模型正文零采用、整条降级、服务端中性说明必在")
def test_level_conclusion_backstop() -> tuple[bool, str]:
    # t28 之后模型不再写正文，所以这里要证明的比原来更强：
    #   1) 整条降级为 needs_context；
    #   2) 模型返回的正文一个字都不出现在最终答案里（不只是"结论句被删"）；
    #   3) 服务端写的中性说明必须出现。
    marker = "MODEL_BODY_MARKER_9f3c"
    r = _run_answer(
        "等保三级的要求有哪些",
        {
            "answer": f"{marker}你们这套系统应当按第三级建设。8.1.4.1 要求部署审计设备。",
            "citations": ["8.1.4.1"],
            "risk_tags": [],
            "need_clarification": False,
            "followup_questions": [],
            "found": True,
        },
        [_clause("8.1.4.1")],
    )
    if r["mode"] != "needs_context":
        return False, f"命中等级结论后没有降级：mode={r['mode']}"
    if marker in r["answer"]:
        return False, f"模型正文被采用了（t28 要求零采用）：{r['answer'][:80]!r}"
    if "按第三级建设" in r["answer"]:
        return False, f"结论句仍在正文：{r['answer'][:80]!r}"
    if "不替您下这个结论" not in r["answer"]:
        return False, "缺少服务端写的中性说明"
    if "8.1.4.1" not in r["answer"]:
        return False, f"条款清单没渲染出来：{r['answer'][:80]!r}"
    return True, "模型正文零采用、结论句零出现、中性说明与条款清单均在"


def main() -> int:
    print("=" * 70)
    print("问题范围判定测试 · 不依赖 Ollama 与数据库")
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
