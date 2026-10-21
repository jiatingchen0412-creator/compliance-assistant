"""组装一致性：答案正文的每一行都必须能追到知识库字段或服务端常量。

为什么要有这个文件
------------------
结构性改造（第 7/8 轮）之后，用户读到的正文全部由 `app/answer.py` 拼装：
开场 / 概述（来自条款标题）/ 条款块（编号+标题 / 正文逐字来自知识库）/
风险标签（闭集）/ 固定收尾。模型只输出 found / clauses / risk_tags /
need_clarification，一个字都进不了正文。

但这条承诺此前只由 `test_answer_render.py` 的若干条用例**举例**验证，而 t29/t30
两次红队都证明「举例」挡不住新写法——他们的报告里漏网的都是没人举例过的句子。

所以本文件换一种验证方式：**逐行穷举**。`answer.unaccounted_lines()` 把答案按行
拆开，要求每一行都能追到知识库或服务端常量，追不到就返回那一行。这里既验证
「正常答案全部可追溯」，也验证「塞进模型文本必须被抓出来」——只测前半句的检查
是空转的。

跑法：.venv\\Scripts\\python.exe tests\\test_answer_assembly.py
不需要 Ollama，不需要数据库（真实条款直接从 data/seed 读）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import answer, guard, prompt, scope  # noqa: E402

RESULTS: list[tuple[str, object]] = []


def case(name: str):
    def deco(fn):
        RESULTS.append((name, fn))
        return fn

    return deco


def check(condition: bool, ok_msg: str, fail_msg: str) -> tuple[bool, str]:
    return (True, ok_msg) if condition else (False, fail_msg)


def _clause(cid: str, title: str, body: str, risk_tags=None) -> dict:
    return {
        "clause_id": cid,
        "title": title,
        "text": body,
        "text_type": "summary",
        "risk_tags": list(risk_tags or []),
    }


CLAUSES = [
    _clause(
        "8.1.4.1",
        "安全管理制度",
        "应建立安全管理制度，明确责任部门和责任人，定期评审和修订。",
        risk_tags=["access_control"],
    ),
    _clause(
        "8.1.4.3",
        "安全审计",
        "应对重要的用户行为和重要安全事件进行审计，审计记录保存不少于六个月。",
    ),
    _clause(
        "8.1.4.7",
        "访问控制",
        "应对登录用户分配账户和权限，及时删除多余、过期账户。",
    ),
]

# 模型可能想塞进正文的句子。故意与 CLAUSES 的正文无任何前缀关系——
# 否则「可追溯」会碰巧成立，用例就是空转的。
MODEL_TEXT = "贵司属于三级，按三级建设即可。"


def _render(clauses=None, risk_tags=None, mode="llm") -> str:
    return answer.render(
        "我们的系统该怎么做？",
        CLAUSES if clauses is None else clauses,
        risk_tags=risk_tags,
        mode=mode,
    )


# --------------------------------------------------------------------------
# 正向：正常答案必须全部可追溯
# --------------------------------------------------------------------------
@case("llm 模式的正文逐行可追溯")
def test_llm_mode_clean():
    text = _render(risk_tags=["access_control"])
    bad = answer.unaccounted_lines(text, CLAUSES)
    return check(not bad, "全部行可追溯", f"有 {len(bad)} 行追不到：{bad[:3]}")


@case("retrieval_only 模式的正文逐行可追溯")
def test_retrieval_only_clean():
    bad = answer.unaccounted_lines(_render(mode="retrieval_only"), CLAUSES)
    return check(not bad, "全部行可追溯", f"追不到：{bad[:3]}")


@case("parse_failed 模式的正文逐行可追溯")
def test_parse_failed_clean():
    bad = answer.unaccounted_lines(_render(mode="parse_failed"), CLAUSES)
    return check(not bad, "全部行可追溯", f"追不到：{bad[:3]}")


@case("没有条款时不编造概述，且逐行可追溯")
def test_empty_clauses_clean():
    text = answer.render("我们的系统该怎么做？", [], mode="llm")
    bad = answer.unaccounted_lines(text, [])
    ok = "本次相关条款集中在" not in text and not bad
    return check(ok, "无条款时没有概述", f"概述仍出现或追不到：{bad[:3]}")


@case("条款正文超过 220 字被截断后仍可追溯")
def test_truncated_body_traceable():
    long_body = "应建立覆盖物理环境、通信网络、区域边界、计算环境的安全管理制度，" * 8
    clause = _clause("8.1.4.20", "管理制度总则", long_body)
    text = answer.render("怎么做？", [clause])
    bad = answer.unaccounted_lines(text, [clause])
    truncated = "…" in text
    return check(
        truncated and not bad,
        "截断后仍可追溯",
        f"截断={truncated}，追不到：{bad[:3]}",
    )


@case("风险标签行可追溯（闭集标签，且必须有条款撑着）")
def test_risk_line_traceable():
    # CLAUSES 里只有 8.1.4.1 带 access_control
    text = _render(risk_tags=["access_control"])
    has_line = "风险标签：" in text and "访问控制" in text
    bad = answer.unaccounted_lines(text, CLAUSES)
    if not (has_line and not bad):
        return False, f"标签缺失或追不到：{bad[:3]}"
    # 条款没标的标签不许显示：模型说「数据加密 / 个人信息保护」，
    # 但本次 3 条条款都不带这两个标签（t2 附注一：12/20 道题的聚合标签无条款支撑）。
    # 注意别拿「风险标签：」这个前缀判断——条款块自己也会带一行标签。
    text2 = _render(risk_tags=["encryption", "personal_info"])
    leaked = [w for w in ("数据加密", "个人信息保护") if w in text2]
    if leaked:
        return False, f"模型给了条款不支持的标签，却被渲染出来了：{leaked}"
    return True, "支持的标签照常渲染，无条款支撑的标签被丢弃"


@case("真实知识库条款渲染后逐行可追溯")
def test_real_seed_clauses_traceable():
    seed = json.loads(
        (ROOT / "data" / "seed" / "dengbao_2.0.json").read_text(encoding="utf-8")
    )
    raw = sorted(seed["clauses"], key=lambda c: len(c.get("text") or ""), reverse=True)
    clauses = [
        {
            "clause_id": c["clause_id"],
            "title": c["title"],
            "text": c.get("text") or "",
            "text_type": c.get("text_type") or "summary",
            "risk_tags": c.get("risk_tags") or [],
        }
        for c in raw[:6]
    ]
    text = answer.render("我们的系统该怎么做？", clauses, risk_tags=["personal_info"])
    bad = answer.unaccounted_lines(text, clauses)
    return check(not bad, "6 条真实条款全部可追溯", f"追不到：{bad[:3]}")


@case("服务端拼在最前面的各类声明不算「无据」")
def test_server_notices_allowed():
    base = _render()
    notices = [
        "⚠️ 说明：回答里提到了一部知识库未收录的法规（名称已隐去），不在本工具的知识库"
        "（本次检索范围）内，相关表述我无法核实，请以官方发布的标准文本为准。",
        "⚠️ 引用核对：回答里提到的 9.9.9 不在本次检索到的条款中，已从引用列表里剔除；"
        "这部分内容没有知识库依据，请以官方标准文本为准，不要直接当成结论。",
        "⚠️ 模型这次试图对您的系统下等级结论，该内容已被丢弃。",
        scope.clarify_notice(),
        # 只取兜底那句本身；ensure_disclaimer 会把传入的正文原样带回，
        # 传 "占位" 会额外制造一行无据文本，那是用例自己的错。
        guard.ensure_disclaimer("占位").split("\n\n", 1)[1],
    ]
    bad: list[str] = []
    for notice in notices:
        bad += answer.unaccounted_lines(notice + "\n\n" + base, CLAUSES)
    return check(not bad, f"{len(notices)} 类服务端声明全部放行", f"被误判：{bad[:3]}")


@case("无结果回答与免责兜底句可追溯")
def test_no_result_and_disclaimer():
    bad = answer.unaccounted_lines(prompt.NO_RESULT_ANSWER, [])
    bad += answer.unaccounted_lines(guard.ensure_disclaimer(prompt.NO_RESULT_ANSWER), [])
    return check(not bad, "无结果回答全部可追溯", f"被误判：{bad[:3]}")


# --------------------------------------------------------------------------
# 反向：模型文本塞进正文必须被抓出来（否则上面的正向用例是空转的）
# --------------------------------------------------------------------------
@case("模型文本塞在末尾会被抓出")
def test_model_text_at_end_caught():
    bad = answer.unaccounted_lines(_render() + "\n\n" + MODEL_TEXT, CLAUSES)
    return check(MODEL_TEXT in bad, "抓到末尾的模型文本", f"漏了：{bad[:3]}")


@case("模型文本塞在中间会被抓出")
def test_model_text_in_middle_caught():
    parts = _render().split("\n\n")
    parts.insert(len(parts) // 2, MODEL_TEXT)
    bad = answer.unaccounted_lines("\n\n".join(parts), CLAUSES)
    return check(MODEL_TEXT in bad, "抓到中间的模型文本", f"漏了：{bad[:3]}")


@case("模型文本塞在最前面会被抓出")
def test_model_text_at_head_caught():
    bad = answer.unaccounted_lines(MODEL_TEXT + "\n\n" + _render(), CLAUSES)
    return check(MODEL_TEXT in bad, "抓到开头的模型文本", f"漏了：{bad[:3]}")


@case("多行模型文本每一行都会被单独抓出")
def test_multiline_model_text_caught():
    text = _render() + "\n\n贵司属于三级。\n按三级建设即可。"
    bad = answer.unaccounted_lines(text, CLAUSES)
    ok = "贵司属于三级。" in bad and "按三级建设即可。" in bad
    return check(ok, "两行都抓到", f"漏了：{bad[:4]}")


@case("编号不在本次引用里的条款块会被抓出")
def test_foreign_clause_head_caught():
    foreign = answer.render_clause_block(_clause("9.9.9.9", "外部条款", "这条不在引用里。"))
    bad = answer.unaccounted_lines(_render() + "\n\n" + foreign, CLAUSES)
    ok = any("9.9.9.9" in line for line in bad)
    return check(ok, "抓到外部条款块", f"漏了：{bad[:3]}")


@case("概述里多一个不属于本次引用的标题会被抓出")
def test_foreign_topic_title_caught():
    extra = _clause("8.1.4.99", "外部标题", "正文不重要。")
    text = _render()
    fake_topic = answer.topic_line(CLAUSES + [extra])
    injected = text.replace(answer.topic_line(CLAUSES), fake_topic, 1)
    bad = answer.unaccounted_lines(injected, CLAUSES)
    ok = any("外部标题" in line for line in bad)
    return check(ok, "抓到伪造的概述", f"漏了：{bad[:3]}")


@case("开场白被改写一个字会被抓出")
def test_modified_opener_caught():
    text = _render().replace("知识库里与之相关的条款是", "知识库里与之相关的条款大概是", 1)
    bad = answer.unaccounted_lines(text, CLAUSES)
    ok = any("大概是" in line for line in bad)
    return check(ok, "抓到被改写的开场白", f"漏了：{bad[:3]}")


@case("把模型文本伪装成条款正文的行会被抓出")
def test_disguised_as_clause_body_caught():
    text = _render() + "\n" + MODEL_TEXT
    bad = answer.unaccounted_lines(text, CLAUSES)
    return check(MODEL_TEXT in bad, "抓到伪装行", f"漏了：{bad[:3]}")


@case("同一个函数对干净答案返回空、对脏答案返回非空")
def test_sanity_both_ways():
    clean = answer.unaccounted_lines(_render(), CLAUSES)
    dirty = answer.unaccounted_lines(_render() + "\n" + MODEL_TEXT, CLAUSES)
    ok = clean == [] and dirty == [MODEL_TEXT]
    return check(ok, "干净为空、脏为非空", f"clean={clean[:2]} dirty={dirty[:2]}")


def main() -> int:
    ok_count = 0
    failed = 0
    print(f"== test_answer_assembly：{len(RESULTS)} 个用例 ==")
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
