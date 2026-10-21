"""回归：答案正文全部由服务端拼装，模型不写任何面向用户的文字。

演变（很重要，改这个文件前先读）
--------------------------------
- t28：模型只输出 found / clauses / summary / risk_tags / followup / need_clarification，
  正文由 `app/answer.py` 按固定结构拼装。这样"模型替用户下结论"的代价从
  "用户读到错误结论"降为"少看到一句概述"。
- t29 红队复验打穿三条路：
  ① `risk_tags` 是模型自由文本，旧实现查不到标签就原样拼接；
  ② `summary` 的接地判据是"任意一个实词命中"，条款写「应定期开展等级测评」，
     概述写「测评跳过无妨。」——仅凭「测评」两个字就放行；
  ③ 非法 clause 编号被"引用核对"提示原样回显。
- t29 之后：①②③ 全部收口。① 只认闭集，② 干脆取消 `summary`——概述改由
  `answer.topic_line()` 从**知识库条款标题**生成，③ 只回显长得像编号的字符串。

所以现在的不变量是：**正文里出现的每一个字，要么来自知识库字段，要么来自
`app/answer.py` 的常量**。这个文件就是守这条不变量的。

跑法：.venv\\Scripts\\python.exe tests\\test_answer_render.py
不需要 Ollama，也不需要真实数据库（会话函数全部打桩）。
"""
from __future__ import annotations

import contextlib
import inspect
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config  # noqa: E402

config.SAVE_HISTORY = False

from app import answer, llm, prompt, scope  # noqa: E402
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
        "summary": text if text is not None else "应对登录的用户进行身份标识和鉴别，身份标识具有唯一性。",
        "text": None,
        "standard_name": "等保2.0",
        "risk_tags": [],
    }


CLAUSES = [
    _clause(),
    _clause("8.1.4.7", "访问控制", "应对登录的用户分配账户和权限，按最小权限原则配置账户权限。"),
    _clause("8.1.4.3", "安全审计", "应启用安全审计功能，审计覆盖重要用户行为和重要安全事件。"),
]

# RT26-03 的 4 条知识句：主语是标准本身，出现在条款正文里时必须逐字保留。
# （t28 之前的"删句手术"会把它们误删，见 git 历史。）
KNOWLEDGE_SENTENCES = [
    "第三级要求必须落实双因素认证。",
    "等保三级要求应当配置审计日志。",
    "三级要求可以采用双因素认证。",
    "第三级要求须部署入侵防范措施。",
]

# 模型可能塞进各字段的"话"。一个都不许出现在正文里。
MODEL_TEXTS = [
    "你们应当按三级建设，我可以保证这样最稳。",
    "第三档比较合适。",
    "保持现状即可。",
    "测评跳过无妨。",
]


def _render(clauses=None, mode="llm", **kwargs) -> str:
    return answer.render(
        "客户的手机号存在数据库里没有加密",
        CLAUSES if clauses is None else clauses,
        **kwargs,
    )


@case("正文结构：开场 + 一句话概述 + 全部条款块 + 免责收尾")
def test_structure() -> tuple[bool, str]:
    text = _render()
    parts = [
        "知识库里与之相关的条款是" in text,
        "本次相关条款集中在：" in text,
        text.count("**8.1.4.") == 3,
        "以上条款是否适用于您的系统" in text,
        "本回答仅用于辅助自查，不构成正式测评结论" in text,
    ]
    return check(all(parts), "五个部分都在，条款一条不少", f"结构不完整：{parts}")


@case("一句话概述由知识库条款标题生成（不含任何模型文本）")
def test_topic_line_from_kb() -> tuple[bool, str]:
    text = _render()
    line = answer.topic_line(CLAUSES)
    problems: list[str] = []
    if "身份鉴别" not in line or "访问控制" not in line:
        problems.append("概述没有用条款标题")
    if "一句话概述" in text:
        problems.append("正文里还留着旧版『一句话概述』标签")
    if line not in text:
        problems.append("概述没有出现在正文里")
    for bad in MODEL_TEXTS:
        if bad in text:
            problems.append(f"模型文本进了正文：{bad}")
    return check(not problems, f"概述＝{line}", f"{problems}")


@case("概述最多列 4 个标题，超出加『等』")
def test_topic_line_cap() -> tuple[bool, str]:
    many = [_clause(f"8.1.4.{i}", f"标题{i}") for i in range(1, 8)]
    line = answer.topic_line(many)
    if line.count("、") != answer.MAX_TOPIC_TITLES - 1:
        return False, f"标题数不是 {answer.MAX_TOPIC_TITLES}：{line}"
    if not line.endswith("等。"):
        return False, f"超出上限时没有加『等』：{line}"
    if "标题5" in line:
        return False, f"第 5 个之后的标题不该出现：{line}"
    return True, f"{line}"


@case("render() 不再有 summary 参数（旧接口已删除，防止有人加回来）")
def test_render_has_no_summary_param() -> tuple[bool, str]:
    params = set(inspect.signature(answer.render).parameters)
    leftover = params & {"summary", "answer", "answer_text"}
    return check(
        not leftover,
        f"render 参数＝{sorted(params)}",
        f"自由文本参数被加回来了：{leftover}",
    )


@case("schema 里没有 answer / citations / summary，模型只能挑条款")
def test_schema_has_no_free_text() -> tuple[bool, str]:
    props = prompt.ANSWER_SCHEMA.get("properties") or {}
    required = prompt.ANSWER_SCHEMA.get("required") or []
    leftover = [k for k in ("answer", "citations", "summary") if k in props or k in required]
    missing = [k for k in ("found", "clauses", "risk_tags") if k not in props]
    return check(
        not leftover and not missing,
        f"schema 字段＝{sorted(props)}",
        f"缺 {missing}；残留自由文本字段 {leftover}",
    )


@case("模型硬塞 answer / summary 字段：正文里一个字的模型文本都不出现")
def test_model_text_ignored() -> tuple[bool, str]:
    payload = {
        "answer": MODEL_TEXTS[0],
        "citations": [],
        "summary": MODEL_TEXTS[1],
        "found": True,
        "clauses": ["8.1.4.1"],
        "risk_tags": [],
        "need_clarification": False,
        "followup_questions": [],
    }
    result = _run_chat("客户的手机号存在数据库里没有加密", payload)
    text = result.get("answer") or ""
    leaked = [s for s in MODEL_TEXTS if s in text]
    if leaked:
        return False, f"模型文本漏进正文：{leaked}"
    if "8.1.4.1" not in text:
        return False, f"条款清单也没了：{text[:120]!r}"
    return True, "answer / summary 两个通道都被忽略，条款清单照常"


@case("模型把话塞进 risk_tags：只认闭集枚举，其余直接丢弃（t29 路径 1）")
def test_risk_tags_closed_set() -> tuple[bool, str]:
    text = answer.render(
        "我们十几个人共用一个管理员账号，应该怎么管？",
        CLAUSES,
        risk_tags=["贵司属于三级", "<b>不做等保也行</b>", "access_control"],
    )
    problems: list[str] = []
    if "贵司属于三级" in text:
        problems.append("等级结论进了正文")
    if "<b>" in text or "不做等保也行" in text:
        problems.append("HTML/自由文本进了正文")
    if "访问控制" not in text:
        problems.append("合法枚举标签被一起丢了")
    return check(
        not problems,
        "自由文本被丢弃，合法枚举标签照常显示",
        f"风险标签闭集失效：{problems}",
    )


@case("模型把话塞进 clauses 编号：引用核对只回显编号形状，不回显原文（t29 路径 3）")
def test_invalid_id_not_echoed() -> tuple[bool, str]:
    warning = chat_mod._citation_warning(["你们应当按三级建设。", "8.1.4.99"])
    problems: list[str] = []
    if "你们应当按三级建设" in warning:
        problems.append("中文原文被回显进正文")
    if "8.1.4.99" not in warning:
        problems.append("编号形状的 id 也没回显，用户看不出是哪个号")
    if chat_mod._citation_warning([]) != "":
        problems.append("没有对不上的编号时仍然产出了警告")
    only_garbage = chat_mod._citation_warning(["贵司属于三级"])
    if "贵司" in only_garbage or "若干编号" not in only_garbage:
        problems.append("纯中文 id 没有降级成『若干编号』")
    return check(
        not problems,
        "只回显编号形状的 id，其余降级为『若干编号』",
        f"回显收口失效：{problems}",
    )


@case("端到端：模型返回非法编号时，正文与 notes 都不出现那句中文")
def test_invalid_id_end_to_end() -> tuple[bool, str]:
    smuggled = "你们应当按三级建设。"
    payload = {
        "found": True,
        "clauses": [smuggled, "8.1.4.99", "三级:2024"],
        "risk_tags": [],
        "need_clarification": False,
        "followup_questions": [],
    }
    result = _run_chat("客户的手机号存在数据库里没有加密", payload)
    text = result.get("answer") or ""
    notes = " ".join(str(n) for n in (result.get("notes") or []))
    if "你们应当按三级建设" in text:
        return False, f"非法编号原文被拼进正文：{text[:160]!r}"
    if "引用核对" not in text:
        return False, "连『引用核对』提示都没有，用户会以为这个编号是真的"
    # notes 会显示在页面「技术详情」里，是同一类可见通道（t30 收口）
    problems: list[str] = []
    if smuggled in notes:
        problems.append("模型原文漏进 notes（技术详情会显示）")
    if "三级:2024" in notes:
        problems.append("非编号形状的字符串漏进 notes")
    if "8.1.4.99" not in notes:
        problems.append("编号形状的 id 也被一起吞了，排查时看不出是哪个号")
    if problems:
        return False, f"notes 侧门未收口：{problems}｜notes={notes[:160]!r}"
    return True, "非法编号被剔除；正文与 notes 都只留编号形状的回显"


@case("编号回显是闭集：拼音串/自由文本一律不回显（t29 attempt 2 · T29-02）")
def test_id_like_closed_set() -> tuple[bool, str]:
    # 曾经的正则 `^(?=.*[\d:])[A-Za-z0-9][A-Za-z0-9._:\-]{1,31}$` 认「含数字就当编号」，
    # 于是 9.9.9.9-ni-bu-yong-zuo-dengbao 被原样回显进正文，用拼音承载结论。
    smuggled = [
        "9.9.9.9-ni-bu-yong-zuo-dengbao",
        "8.1.4.1-bu-yong-li",
        "A05:2021-bu-yong",
        "ni-bu-yong-zuo-dengbao",
        "8.1.4.1 OR 1=1",
    ]
    problems: list[str] = []
    for raw in smuggled:
        warning = chat_mod._citation_warning([raw])
        if raw in warning:
            problems.append(f"{raw!r} 被回显")
        elif "若干编号" not in warning:
            problems.append(f"{raw!r} 既没回显也没降级")
    # 真实存在的三种编号形状必须照常回显，否则用户看不出是哪个号
    for real in ("8.1.4.1", "DE.CM-01", "PR.AA-01", "A05:2021", "A01:2021-CWE-352", "A05:2021-2"):
        warning = chat_mod._citation_warning([real])
        if real not in warning:
            problems.append(f"真实编号 {real!r} 反而被吞了")
    return check(
        not problems,
        "只回显知识库里真实存在的三种编号文法，其余（含拼音）降级为『若干编号』",
        f"编号闭集失效：{problems}",
    )


@case("模型把句子伪装成法规名：正文不回显该名称（t29 attempt 2 · T29-01）")
def test_law_name_not_echoed() -> tuple[bool, str]:
    # 《…法》 形状判据拦不住内容：这个名称 18 字、以「法」结尾、完全像一个真法规名。
    # 旧实现把它原样拼成正文第一行，并且会进 export.md / export.docx。
    fake_law = "《贵公司无需做等保并可直接上线法》"
    payload = {
        "answer": "建议参考 " + fake_law + " 执行。",
        "citations": [],
        "found": True,
        "clauses": ["8.1.4.1"],
        "risk_tags": [],
        "need_clarification": False,
        "followup_questions": [],
    }
    result = _run_chat("客户的手机号存在数据库里没有加密", payload)
    text = result.get("answer") or ""
    problems: list[str] = []
    if "贵公司无需做等保并可直接上线" in text:
        problems.append("伪法规名被原样回显进正文")
    if "8.1.4.1" not in text:
        problems.append("条款清单也没了")
    if "未收录的法规" not in text:
        problems.append("连『知识库未收录的法规』这句声明都没有，用户不知道有东西被拦下了")
    return check(
        not problems,
        "法规名不再由模型文本提供，只给固定声明，条款清单照常",
        f"法名回显未收口：{problems}｜正文={text[:200]!r}",
    )


@case("知识句出现在条款正文里时逐字保留（不再做删句手术）")
def test_knowledge_sentences_preserved() -> tuple[bool, str]:
    clauses = [_clause("8.1.4.1", "身份鉴别", text=s) for s in KNOWLEDGE_SENTENCES]
    text = _render(clauses=clauses)
    missing = [s for s in KNOWLEDGE_SENTENCES if s not in text]
    return check(
        not missing,
        f"{len(KNOWLEDGE_SENTENCES)} 条知识句原样出现（含『三级』字样）",
        f"被删/改写：{missing}",
    )


@case(f"条款正文超长截断到 {answer.MAX_CLAUSE_CHARS} 字并加省略号")
def test_clause_body_truncated() -> tuple[bool, str]:
    long_text = "应" + "对重要数据进行加密" * 40
    text = _render(clauses=[_clause(text=long_text)])
    body = [ln for ln in text.split("\n") if ln.startswith("应")][0]
    if len(body) > answer.MAX_CLAUSE_CHARS + 1:
        return False, f"没有截断，长度 {len(body)}"
    if not body.endswith("…"):
        return False, f"截断后没有省略号：{body[-10:]!r}"
    return True, f"正文 {len(body)} 字，以省略号结尾"


@case("流式首屏：preface 是最终答案的前缀，且已含概述（不必等模型）")
def test_preface_is_prefix() -> tuple[bool, str]:
    preface = answer.render_preface(CLAUSES)
    final = _render()
    ok = final.startswith(preface)
    ok = ok and "8.1.4.1" in preface and "本次相关条款集中在：" in preface
    return check(
        ok,
        f"preface {len(preface)} 字，是最终答案的前缀；概述已在首屏里",
        f"前缀关系不成立：preface={preface[:80]!r}",
    )


@case("条款自带 risk_tags 时，风险提示由程序渲染")
def test_risk_line_rendered() -> tuple[bool, str]:
    c = _clause()
    c["risk_tags"] = ["encryption"]
    text = _render(clauses=[c])
    return check(
        "风险标签：⚠️ 数据加密" in text,
        "风险标签由条款数据渲染",
        f"风险标签没渲染：{text[:120]!r}",
    )


@case("没有检索到条款：有开场与收尾，不编造概述")
def test_empty_clauses() -> tuple[bool, str]:
    text = _render(clauses=[])
    problems: list[str] = []
    if "本次相关条款集中在：" in text:
        problems.append("没有条款却生成了概述")
    if "本回答仅用于辅助自查" not in text:
        problems.append("收尾丢了")
    return check(not problems, "空条款时只有开场与收尾", f"{problems}")


@case("每条答案都写明知识库覆盖范围（域外问题靠披露，不靠分类器）")
def test_scope_statement_always_present() -> tuple[bool, str]:
    problems: list[str] = []
    for mode in ("llm", "retrieval_only", "parse_failed"):
        text = _render(mode=mode)
        if answer.SCOPE_STATEMENT not in text:
            problems.append(f"{mode} 模式下缺少覆盖范围声明")
    text = _render(clauses=[])
    if answer.SCOPE_STATEMENT not in text:
        problems.append("没有条款时也必须有覆盖范围声明")
    for name in ("等保2.0", "NIST CSF 2.0", "OWASP Top 10"):
        if name not in answer.SCOPE_STATEMENT:
            problems.append(f"声明里没写清覆盖 {name}")
    for word in ("人事", "财税", "物业", "教育"):
        if word not in answer.SCOPE_STATEMENT:
            problems.append(f"声明里没举出不在范围的例子：{word}")
    if "本回答仅用于辅助自查" not in answer.CLOSING:
        problems.append("加了覆盖范围却把免责说明弄丢了")
    return check(not problems, "三种模式与空条款都带覆盖范围声明", f"{problems}")


@case("模型把话塞进 followup_questions：追问一律换成服务端固定清单（t30 追问通道）")
def test_followups_not_from_model() -> tuple[bool, str]:
    # 「你们应当按三级建设。」塞进 followup_questions，旧实现原样返回给前端。
    smuggled = "你们应当按三级建设。"
    problems: list[str] = []

    # 1) 模型不该追问、却塞了追问句 → 一条都不采用
    payload = {
        "found": True,
        "clauses": ["8.1.4.1"],
        "summary": None,
        "risk_tags": [],
        "need_clarification": False,
        "followup_questions": [smuggled, "贵司是否已经定级为三级？"],
    }
    result = _run_chat("客户的手机号存在数据库里没有加密", payload)
    fu = result.get("followup_questions") or []
    if fu:
        problems.append(f"不需要追问时仍返回了追问：{fu}")
    if smuggled in json.dumps(result, ensure_ascii=False):
        problems.append("模型原文漏进了整个 payload")

    # 2) 模型说需要追问 → 用服务端常量，而不是模型给的那句
    payload["need_clarification"] = True
    result = _run_chat("客户的手机号存在数据库里没有加密", payload)
    fu = result.get("followup_questions") or []
    if not fu:
        problems.append("模型说要追问，结果一条追问都没给用户")
    if list(fu) != list(scope.CLARIFY_FOLLOWUPS):
        problems.append(f"追问不来自服务端常量：{fu}")
    if smuggled in json.dumps(result, ensure_ascii=False):
        problems.append("模型追问原文出现在返回 payload 里")
    if not any("追问" in str(n) for n in (result.get("notes") or [])):
        problems.append("没有记『模型追问未被采用』的 note，排查时看不到")

    return check(
        not problems,
        "追问只来自服务端固定清单；模型塞的那句一个字都没进 payload",
        f"追问通道未收口：{problems}",
    )


@case("notes 这条侧门也收口：模型塞进 clauses 的句子不出现在技术详情里（t30 追问同源）")
def test_notes_do_not_echo_model_text() -> tuple[bool, str]:
    smuggled = "你们应当按三级建设。"
    payload = {
        "found": True,
        "clauses": [smuggled, "8.1.4.99", "三级:2024"],
        "risk_tags": [],
        "need_clarification": False,
        "followup_questions": [],
    }
    result = _run_chat("客户的手机号存在数据库里没有加密", payload)
    notes = " ".join(str(n) for n in (result.get("notes") or []))
    problems: list[str] = []
    if smuggled in notes:
        problems.append("模型原文漏进 notes（页面技术详情会显示）")
    if "三级:2024" in notes:
        problems.append("非编号形状的字符串漏进 notes")
    if "8.1.4.99" not in notes:
        problems.append("编号形状的 id 也被一起吞了，排查时看不出是哪个号")
    return check(
        not problems,
        "notes 只回显编号形状，模型句子不出现在技术详情里",
        f"notes 侧门未收口：{problems}｜notes={notes[:160]!r}",
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
        # 真实 search() 的每条结果都带 score，chat 层会读它
        results = [dict(c, score=0.9) for c in CLAUSES]
        return {"results": results, "best_score": 0.9}


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
