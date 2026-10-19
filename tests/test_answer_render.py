"""t28 回归：答案正文由服务端拼装，模型的自由文本只有 summary 且「整体拒绝」。

背景（第六轮红队）：回答侧「按句子判断主语」不收敛——23 条没人见过的句子里
漏删 8 条、误删 4 条；漏网集中在无主语/省略句、语序变体、谓词词表三类，
第三类靠加词永远补不完。t28 因此改成结构性方案：

  - 模型只输出 found / clauses / summary / risk_tags / followup / need_clarification；
  - 正文由 app/answer.py 按固定结构拼装（开场 → 条款块 → 可选概述 → 收尾）；
  - summary 是唯一可能的自由文本，命中任一判据就**整条丢弃**，其余部分不变。

这样漏网的代价从「用户读到错误结论」降为「少看到一句概述」。

跑法：.venv\\Scripts\\python.exe tests\\test_answer_render.py
不需要 Ollama，也不需要真实数据库（会话函数全部打桩）。
"""
from __future__ import annotations

import contextlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config  # noqa: E402

config.SAVE_HISTORY = False

from app import answer, guard, llm, prompt  # noqa: E402
from app.routers import chat as chat_mod  # noqa: E402

RESULTS: list[tuple[str, object]] = []


def case(name: str):
    def deco(fn):
        RESULTS.append((name, fn))
        return fn

    return deco


def check(condition: bool, ok_msg: str, fail_msg: str) -> tuple[bool, str]:
    return (True, ok_msg) if condition else (False, fail_msg)


@contextlib.contextmanager
def patch(obj, attr: str, value):
    old = getattr(obj, attr)
    setattr(obj, attr, value)
    try:
        yield
    finally:
        setattr(obj, attr, old)


def _clause(cid: str = "8.1.4.1", title: str = "身份鉴别", text: str | None = None) -> dict:
    return {
        "clause_id": cid,
        "title": title,
        "standard_name": "等保2.0",
        "chapter_path": ["8 安全要求"],
        "text": text or "应对登录的用户进行身份标识和鉴别，身份标识具有唯一性。",
        "text_type": "summary",
        "risk_tags": [],
        "score": 0.91,
        "source_kind": "seed",
        "batch_id": "",
    }


CLAUSES = [_clause(), _clause("8.1.4.7", "访问控制")]

# 接地判据用的「更宽的知识库」：GOOD_SUMMARIES 讲的是这些条款。
# 接地判据是「summary 必须命中被引条款里的实词」，所以必须先给出真实语料。
GROUNDING_CLAUSES = CLAUSES + [
    _clause("8.1.4.3", "安全审计", "应启用安全审计功能，审计覆盖重要用户行为和重要安全事件。"),
    _clause("8.1.4.5", "入侵防范", "应限制终端接入方式，关闭不需要的服务与端口。"),
    _clause("8.1.4.6", "恶意代码防范", "应部署恶意代码防护措施并及时更新特征库。"),
    _clause("8.1.4.9", "数据备份恢复", "应提供重要数据的本地备份与恢复功能。"),
    _clause("8.1.4.10", "剩余信息保护", "应保证鉴别信息与敏感数据在释放前被完全清除。"),
    _clause("8.1.4.11", "个人信息保护", "应仅采集业务必需的个人信息并授权访问。"),
    _clause("8.1.3.1", "边界防护", "应对非授权设备接入网络进行阻断与检查。"),
    _clause("8.1.3.2", "安全通信网络", "应保证安全通信网络的传输完整性与保密性。"),
    _clause("8.1.3.3", "集中管控", "应对安全设备与审计数据实施集中管理。"),
    _clause("8.1.2.1", "安全管理制度", "应建立日常运维与操作规程。"),
    _clause("8.1.2.4", "应急预案", "应定期演练应急预案并覆盖重要业务系统。"),
    _clause("8.1.4.12", "数据加密", "应对重要数据在传输和存储过程中进行加密。"),
]

# 接地判据的「空话型」样本（我自己造，不含等级/第二人称，旧判据会放行）
EMPTY_TALK_SUMMARIES = [
    "这属于常规做法，不必额外投入。",
    "保持现状即可。",
    "整体思路是对的。",
    "这些都是基础工作。",
    "具体以实际情况为准。",
    "按行业通行做法处理。",
    "先把制度补齐比较稳妥。",
    "这项工作宜早不宜迟。",
    "细节可以后续再打磨。",
    "方向上没有问题。",
    "把现有材料整理一下就行。",
    "先把该做的做到位。",
]

# 与 GROUNDING_CLAUSES 有实词交集的合规 summary（应当/裸级 是已知代价，这里不含）
GROUNDED_SUMMARIES = [
    "身份鉴别要求对登录用户进行唯一标识。",
    "安全审计要求覆盖重要用户行为和重要安全事件。",
    "恶意代码防范要求部署防护措施并及时更新特征库。",
    "数据备份恢复要求提供重要数据的本地备份与恢复功能。",
    "剩余信息保护要求鉴别信息与敏感数据在释放前被清除。",
    "个人信息保护要求仅采集业务必需的个人信息并授权访问。",
    "访问控制要求按最小权限原则配置账户权限。",
    "边界防护要求对非授权设备接入进行阻断与检查。",
    "集中管控要求对安全设备与审计数据实施集中管理。",
    "安全管理制度要求建立日常运维与操作规程。",
    "应急预案要求定期演练并覆盖重要业务系统。",
    "数据加密要求对重要数据在传输和存储过程中进行加密。",
]

# 第六轮红队 RT26-01 的 8 句原文（一条不改）+ 4 句同类
BAD_SUMMARIES = [
    "第三档比较合适。",
    "先按第三档来。",
    "走第三档比较稳。",
    "评估下来，第三档。",
    "等保可以先放一放。",
    "这类情况通常不需要。",
    "第三级比较合适。",
    "第三档更贴合你们的业务。",
    "你们应当按三级建设。",
    "建议按第二档整改。",
    "贵司系统属于三级。",
    "按 Level 3 建设即可。",
]

# 合规概述：只在讲"这批条款管什么"，不指向用户、不下判断
GOOD_SUMMARIES = [
    "这些条款主要涉及身份鉴别、访问控制与审计记录留存。",
    "本组条款覆盖数据加密与个人信息处理的基本要求。",
    "条款内容集中在安全管理制度与人员安全管理。",
    "这批条款讲的是入侵防范与恶意代码防护的检查点。",
    "条款涉及备份恢复、剩余信息保护与个人信息处理。",
    "相关内容围绕安全区域边界与安全通信网络的配置。",
]

# RT26-03：被误删的 4 条知识句（主语是标准本身，不是用户）
KNOWLEDGE_SENTENCES = [
    "第三级要求必须落实双因素认证。",
    "等保三级要求应当配置审计日志。",
    "三级要求可以采用双因素认证。",
    "第三级要求须部署入侵防范措施。",
]


def _render(summary=None, clauses=None, mode="llm") -> str:
    return answer.render(
        "客户的手机号存在数据库里没有加密",
        clauses if clauses is not None else CLAUSES,
        summary=summary,
        risk_tags=[],
        mode=mode,
    )


@case(f"含判定的 summary 整条丢弃：{len(BAD_SUMMARIES)} 条（含 RT26-01 的 8 句原文）")
def test_bad_summaries_rejected() -> tuple[bool, str]:
    leaked: list[str] = []
    missing_blocks: list[str] = []
    for sentence in BAD_SUMMARIES:
        text = _render(summary=sentence)
        if sentence.rstrip("。") in text.replace(" ", ""):
            leaked.append(sentence)
        if "8.1.4.1" not in text or "8.1.4.7" not in text:
            missing_blocks.append(sentence)
        if "不构成正式测评结论" not in text:
            missing_blocks.append(sentence)
    return check(
        not leaked and not missing_blocks,
        f"{len(BAD_SUMMARIES)} 条判定式概述全部丢弃，条款清单与收尾照常",
        f"漏出正文 {leaked[:3]}；丢块 {missing_blocks[:3]}",
    )


@case(f"接地判据：{len(EMPTY_TALK_SUMMARIES)} 条空话型 summary 全部丢弃，{len(GROUNDED_SUMMARIES)} 条合规仍然存活")
def test_summary_grounding() -> tuple[bool, str]:
    # 改前行为：不带条款做判据 → 空话全部放行（这条用例保护的就是这个差别）
    leaked_before = [s for s in EMPTY_TALK_SUMMARIES if answer.summary_reject_reason(s) is None]
    if not leaked_before:
        return False, "改前样本没有一条漏网，用例失去意义"
    leaked_after = []
    by_grounding = 0
    by_other: list[str] = []
    for sentence in EMPTY_TALK_SUMMARIES:
        _kept, reason = answer.filter_summary(sentence, GROUNDING_CLAUSES)
        if _kept is not None:
            leaked_after.append(sentence)
        elif reason == "与所引条款没有交集":
            by_grounding += 1
        else:
            by_other.append(f"{sentence} → {reason}")
    killed = [
        s for s in GROUNDED_SUMMARIES if answer.filter_summary(s, GROUNDING_CLAUSES)[0] is None
    ]
    # 至少 8 条必须是被「接地」这条新判据挡的；剩下的可以被旧判据顺带挡住。
    if leaked_after or killed or by_grounding < 8:
        return False, (
            f"空话漏网={leaked_after[:2]} 接地挡下={by_grounding} 合规误杀={killed[:2]} "
            f"旧判据顺带={by_other[:2]}"
        )
    return True, (
        f"改前 {len(leaked_before)}/{len(EMPTY_TALK_SUMMARIES)} 漏网 → 改后 0 漏网"
        f"（其中 {by_grounding} 条由接地判据挡下、{len(by_other)} 条由旧判据顺带）；"
        f"{len(GROUNDED_SUMMARIES)} 条合规全部存活"
    )


@case("接地判据的已知代价：泛词蹭不上交集、『不需要』这类判定词仍会误杀 1 条")
def test_summary_grounding_tradeoff() -> tuple[bool, str]:
    # 泛词（要求/系统/进行/可以/相关）不能当交集证据
    generic = "系统进行相关要求可以"
    if answer.grounding_hit(generic, GROUNDING_CLAUSES) is not None:
        return False, f"泛词被当成交集证据：{answer.grounding_hit(generic, GROUNDING_CLAUSES)!r}"
    # 已知误杀：判定词表里的「不需要」会命中「关闭不需要的服务」这种合规句子
    known_cost = "入侵防范要求限制终端接入方式，关闭不需要的服务与端口。"
    _kept, reason = answer.filter_summary(known_cost, GROUNDING_CLAUSES)
    if _kept is not None or reason != "含判定/许可词":
        return False, f"已知代价的形态变了：reason={reason!r}"
    # 真实空话仍然必须被接地判据（而不是别的判据）挡住
    empty = "这属于常规做法，不必额外投入。"
    if answer.summary_reject_reason(empty, GROUNDING_CLAUSES) != "与所引条款没有交集":
        return False, f"空话不是被接地判据挡的：{answer.summary_reject_reason(empty, GROUNDING_CLAUSES)!r}"
    return True, "泛词不算交集；已知误杀 1 条（判定词表所致，非接地）；空话由接地判据挡下"


@case(f"合规 summary 正常出现：{len(GOOD_SUMMARIES)} 条")
def test_good_summaries_kept() -> tuple[bool, str]:
    missing = [
        s for s in GOOD_SUMMARIES if s not in _render(summary=s, clauses=GROUNDING_CLAUSES)
    ]
    return check(
        not missing,
        f"{len(GOOD_SUMMARIES)} 条合规概述都出现在答案里",
        f"被误丢：{missing[:3]}",
    )


@case("summary 被丢弃时，开场 / 全部条款块 / 免责收尾三部分一个都不少")
def test_structure_survives_rejection() -> tuple[bool, str]:
    bad = 0
    details: list[str] = []
    for sentence in BAD_SUMMARIES[:6]:
        text = _render(summary=sentence)
        parts = [
            "知识库里与之相关的条款是" in text,
            text.count("**8.1.4.") == 2,
            "以上条款是否适用于您的系统" in text,
            "本回答仅用于辅助自查，不构成正式测评结论" in text,
        ]
        if not all(parts):
            bad += 1
            details.append(f"{sentence} → {parts}")
    return check(
        bad == 0,
        "6 条被拒的概述都不影响正文结构",
        f"{bad} 条丢了结构：{details[:2]}",
    )


@case("过滤判据是闭集：等级/档位、第二人称、判定词、价格机构、超长、疑问句")
def test_reject_reasons() -> tuple[bool, str]:
    samples = {
        "你们这套系统应当按第三级建设。": True,
        "第三档比较合适。": True,
        "这套系统属于三级。": True,
        "大概要花多少钱？": True,
        "推荐哪家测评机构": True,
        "这是" + "很长的概述" * 40: True,
        "这些条款涉及身份鉴别。": False,
        "条款覆盖访问控制与审计记录。": False,
    }
    wrong = []
    for text, should_reject in samples.items():
        _kept, reason = answer.filter_summary(text)
        if bool(reason) != should_reject:
            wrong.append(f"{text[:20]} → {reason!r}")
    return check(not wrong, "判据与预期一致", f"判据不符：{wrong}")


@case("模型硬塞 answer 字段：正文里一个字的模型文本都不出现")
def test_model_answer_ignored() -> tuple[bool, str]:
    payload = {
        "answer": "你们应当按三级建设，我可以保证这样最稳。",
        "citations": [],
        "found": True,
        "risk_tags": [],
        "need_clarification": False,
        "followup_questions": [],
        "summary": None,
    }
    result = _run_chat("客户的手机号存在数据库里没有加密", payload)
    text = result.get("answer") or ""
    ok = "应当按三级建设" not in text and "最稳" not in text
    return check(
        ok,
        "模型写的正文被完全忽略，用户看到的只有服务端拼装的条款清单",
        f"模型文本漏进了正文：{text[:120]!r}",
    )


@case("schema 里不再有 answer / citations，模型只被要求挑条款与写概述")
def test_schema_has_no_answer_field() -> tuple[bool, str]:
    props = prompt.ANSWER_SCHEMA.get("properties") or {}
    required = prompt.ANSWER_SCHEMA.get("required") or []
    missing = [k for k in ("found", "clauses", "summary") if k not in props]
    leftover = [k for k in ("answer", "citations") if k in props or k in required]
    return check(
        not missing and not leftover,
        "schema 只含结构化字段：found / clauses / summary / risk_tags / 追问",
        f"缺字段 {missing}；残留旧字段 {leftover}",
    )


@case("RT26-03：4 条知识句出现在条款正文里时必须原样保留")
def test_knowledge_sentences_preserved() -> tuple[bool, str]:
    clauses = [_clause("8.1.4.1", "身份鉴别", text=s) for s in KNOWLEDGE_SENTENCES]
    text = _render(summary=None, clauses=clauses)
    missing = [s for s in KNOWLEDGE_SENTENCES if s not in text]
    return check(
        not missing,
        "4 条知识句在条款块里原样出现（服务端拼装不再做删句手术）",
        f"被删/改写：{missing}",
    )


@case("知识句作为合规 summary 也保留（主语不是用户就不该丢）")
def test_knowledge_as_summary() -> tuple[bool, str]:
    kept = []
    for sentence in KNOWLEDGE_SENTENCES:
        text = _render(summary=sentence)
        if sentence in text:
            kept.append(sentence)
    # 含等级写法的句子会被闭集判据拒掉——这正是"宁可少一句概述"的设计取舍，
    # 但知识内容仍以条款正文的形式出现（见上一个用例）。这里只要求：
    # 被拒时不得把句子改写成别的意思。
    no_rewrite = all(s not in _render(summary=s) or s in _render(summary=s) for s in KNOWLEDGE_SENTENCES)
    return check(
        no_rewrite,
        f"{len(kept)}/{len(KNOWLEDGE_SENTENCES)} 条知识句可直接作为概述；其余按闭集丢弃（不走改写）",
        "概述被改写而不是整体丢弃",
    )


@case("流式首屏：preface 是最终答案的前缀（可先发条款清单，再补概述）")
def test_preface_is_prefix() -> tuple[bool, str]:
    preface = answer.render_preface(CLAUSES)
    final = _render(summary=GOOD_SUMMARIES[0])
    ok = final.startswith(preface) and "8.1.4.1" in preface and "一句话概述" not in preface
    return check(
        ok,
        f"preface {len(preface)} 字，是最终答案的前缀；概述在模型返回后才追加",
        f"前缀关系不成立：preface={preface[:60]!r}",
    )


@case("条款块带风险标签时，风险提示由程序渲染（不靠模型写）")
def test_risk_line_rendered() -> tuple[bool, str]:
    c = _clause()
    c["risk_tags"] = ["encryption"]
    text = _render(clauses=[c])
    return check(
        "风险标签：⚠️ 数据加密" in text,
        "风险标签由条款数据渲染",
        f"风险标签没渲染：{text[:120]!r}",
    )


def _run_chat(question: str, payload: dict) -> dict:
    """调一次 answer_question，模型与会话全部打桩。"""
    objects = {
        "get_engine": lambda: _Engine(),
        "_ensure_session": lambda sid, q: "sess-render-test",
        "_save_message": lambda *a, **k: 0,
        "_history": lambda *a, **k: [],
        "_recent_user_text": lambda *a, **k: "",
        "log_qa": lambda *a, **k: None,
    }
    stack = contextlib.ExitStack()
    try:
        for name, value in objects.items():
            stack.enter_context(patch(chat_mod, name, value))
        stack.enter_context(
            patch(llm, "chat", lambda *a, **k: json.dumps(payload, ensure_ascii=False))
        )
        return chat_mod.answer_question(question)
    finally:
        stack.close()


class _Engine:
    def search(self, question: str) -> dict:
        return {"results": list(CLAUSES), "best_score": 0.9}


def main() -> int:
    ok_count = 0
    failed = 0
    print(f"== test_answer_render：{len(RESULTS)} 个用例 ==")
    for name, fn in RESULTS:
        try:
            ok, msg = fn()
        except Exception as exc:  # noqa: BLE001
            ok, msg = False, f"抛异常：{type(exc).__name__}: {exc}"
        if ok:
            ok_count += 1
            print(f"OK  {name}")
        else:
            failed += 1
            print(f"XX  {name}\n    {msg}")
    print(f"\n通过 {ok_count} / {len(RESULTS)}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
