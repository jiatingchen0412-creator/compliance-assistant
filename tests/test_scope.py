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
