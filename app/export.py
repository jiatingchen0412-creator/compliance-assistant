"""导出 Word 自查报告。

为什么做 Markdown 之外还要 Word：
Markdown 适合自己留存，但发给顾问、交给老板、附在客户资料里，Word 更正式。

报告结构（一份可以直接交出去的文档）：
    第1页  封面：标题、生成时间、依据标准、责任声明
    第2页  ⚠️ 高风险事项汇总（有才生成）
    正文   逐条问答：问题 → 回答 → 引用的条款
    附录   引用条款清单（表格：标准 / 编号 / 标题 / 内容类型）
    最后   免责声明

中文字体要特殊处理：python-docx 的 font.name 只设 ascii 字体，
中文必须再设 w:eastAsia，否则 Word 里中文会变成宋体（或者显示异常）。
"""
from __future__ import annotations

import io
import re
from datetime import datetime

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

CN_FONT = "微软雅黑"
MONO_FONT = "Consolas"

BRAND = RGBColor(0x2B, 0x7F, 0xD4)
WARN = RGBColor(0x8A, 0x61, 0x00)
GREY = RGBColor(0x5A, 0x6B, 0x7D)

DISCLAIMER = (
    "本报告由「安全合规自查助手」根据您本机知识库中的条款自动生成，"
    "用于辅助自查参考，不构成正式测评结论，也不构成法律意见。"
    "涉及数据加密、访问控制、个人信息保护等高风险事项，"
    "建议咨询专业安全顾问或有资质的等保测评机构。"
)


def _font(run, name: str = CN_FONT, size: float = 10.5, bold: bool = False,
          color: RGBColor | None = None) -> None:
    """设置字体。中文必须单独设 w:eastAsia，否则 Word 里不会用这个字体。"""
    run.font.name = name
    run.font.size = Pt(size)
    run.font.bold = bold
    if color is not None:
        run.font.color.rgb = color
    rpr = run._element.get_or_add_rPr()
    rfonts = rpr.get_or_add_rFonts()
    rfonts.set(qn("w:eastAsia"), name)
    rfonts.set(qn("w:ascii"), name)
    rfonts.set(qn("w:hAnsi"), name)


def _para(doc, text: str = "", *, size: float = 10.5, bold: bool = False,
          color: RGBColor | None = None, align=None, space_after: float = 6,
          font: str = CN_FONT, indent: float = 0):
    p = doc.add_paragraph()
    if align is not None:
        p.alignment = align
    p.paragraph_format.space_after = Pt(space_after)
    if indent:
        p.paragraph_format.left_indent = Cm(indent)
    if text:
        _font(p.add_run(text), name=font, size=size, bold=bold, color=color)
    return p


def _clean(text: str) -> str:
    """去掉 Markdown 标题符号，Word 里用真正的标题样式。"""
    return re.sub(r"^\s{0,3}#{1,6}\s*", "", text or "").strip()


def build_report(session_title: str, session_id: str, messages: list[dict],
                 standards: list[str] | None = None) -> bytes:
    """生成 .docx 的字节内容。

    messages: [{"role": "user"/"assistant", "content": str,
                "citations": [...], "risks": [...], "created_at": str}, ...]
    """
    doc = Document()

    # 页面：A4 + 常规页边距
    section = doc.sections[0]
    section.page_width = Cm(21.0)
    section.page_height = Cm(29.7)
    section.left_margin = Cm(2.4)
    section.right_margin = Cm(2.4)
    section.top_margin = Cm(2.2)
    section.bottom_margin = Cm(2.2)

    # 正文默认样式也设成中文字体
    normal = doc.styles["Normal"]
    normal.font.name = CN_FONT
    normal.font.size = Pt(10.5)
    normal.element.rPr.rFonts.set(qn("w:eastAsia"), CN_FONT)

    # ---------------- 封面 ----------------
    _para(doc, "", space_after=40)
    _para(doc, "安全合规自查报告", size=24, bold=True, color=BRAND,
          align=WD_ALIGN_PARAGRAPH.CENTER, space_after=10)
    _para(doc, "基于等保2.0 · NIST CSF 2.0 · OWASP Top 10 2021",
          size=11, color=GREY, align=WD_ALIGN_PARAGRAPH.CENTER, space_after=36)

    info = [
        ("自查主题", session_title or "未命名"),
        ("生成时间", datetime.now().strftime("%Y-%m-%d %H:%M")),
        ("记录编号", session_id),
        ("依据标准", "、".join(standards) if standards else
         "等保2.0（GB/T 22239-2019）安全通用要求、NIST CSF 2.0、OWASP Top 10 2021"),
        ("问答条数", f"{sum(1 for m in messages if m.get('role') == 'user')} 条提问"),
    ]
    table = doc.add_table(rows=0, cols=2)
    table.style = "Light List Accent 1"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    for key, value in info:
        row = table.add_row().cells
        row[0].text = ""
        row[1].text = ""
        _font(row[0].paragraphs[0].add_run(key), size=10.5, bold=True)
        _font(row[1].paragraphs[0].add_run(str(value)), size=10.5)

    _para(doc, "", space_after=30)
    _para(doc, "责任声明", size=11, bold=True, color=WARN, space_after=4)
    _para(doc, DISCLAIMER, size=9.5, color=GREY, space_after=0)

    doc.add_page_break()

    # ---------------- ⚠️ 风险汇总 ----------------
    all_risks: dict[str, dict] = {}
    for m in messages:
        for r in (m.get("risks") or []):
            tag = r.get("tag") or r.get("label")
            entry = all_risks.setdefault(tag, {
                "label": r.get("label", tag),
                "why": r.get("why", ""),
                "clauses": set(),
            })
            for cid in (r.get("clauses") or []):
                entry["clauses"].add(cid)

    if all_risks:
        _para(doc, "⚠️ 高风险事项汇总", size=16, bold=True, color=WARN, space_after=8)
        _para(doc, "以下主题属于监管重点检查项，建议优先整改。", size=10, color=GREY,
              space_after=10)
        for entry in all_risks.values():
            _para(doc, f"⚠️ {entry['label']}", size=12, bold=True, color=WARN, space_after=3)
            if entry["why"]:
                _para(doc, entry["why"], size=10, indent=0.6, space_after=3)
            if entry["clauses"]:
                _para(doc, "涉及条款：" + "、".join(sorted(entry["clauses"])),
                      size=10, color=GREY, indent=0.6, space_after=8)
        doc.add_page_break()

    # ---------------- 逐条问答 ----------------
    _para(doc, "自查记录明细", size=16, bold=True, color=BRAND, space_after=10)

    index = 0
    pending_question = None
    for m in messages:
        if m.get("role") == "user":
            pending_question = m
            continue
        if m.get("role") != "assistant":
            continue

        index += 1
        question = _clean(pending_question.get("content", "")) if pending_question else ""
        _para(doc, f"问题 {index}", size=12, bold=True, color=BRAND, space_after=3)
        _para(doc, question or "（未记录）", size=10.5, indent=0.4, space_after=8)

        _para(doc, "回答", size=11, bold=True, space_after=3)
        answer = _clean(m.get("content", ""))
        for line in answer.split("\n"):
            if line.strip():
                _para(doc, line.strip(), size=10.5, indent=0.4, space_after=3)

        citations = m.get("citations") or []
        if citations:
            _para(doc, "引用条款", size=11, bold=True, space_after=3)
            for c in citations:
                mark = "⚠️ " if (c.get("risk_tags") or []) else ""
                type_note = "（要点摘要，非标准原文）" if c.get("text_type") == "summary" else ""
                _para(
                    doc,
                    f"{mark}{c.get('clause_id', '')} {c.get('title', '')}"
                    f" —— {c.get('standard_name', '')}{type_note}",
                    size=10, indent=0.4, space_after=2,
                )

        risks = m.get("risks") or []
        if risks:
            _para(doc, "⚠️ 风险提示", size=11, bold=True, color=WARN, space_after=3)
            for r in risks:
                _para(doc, f"⚠️ {r.get('label', '')}：{r.get('why', '')}",
                      size=10, color=WARN, indent=0.4, space_after=2)

        pending_question = None
        _para(doc, "", space_after=10)

    # ---------------- 附录：引用条款清单 ----------------
    seen: dict[str, dict] = {}
    for m in messages:
        for c in (m.get("citations") or []):
            key = f"{c.get('standard_id', '')}|{c.get('clause_id', '')}"
            seen.setdefault(key, c)

    if seen:
        doc.add_page_break()
        _para(doc, "附录：引用条款清单", size=16, bold=True, color=BRAND, space_after=10)
        tbl = doc.add_table(rows=1, cols=4)
        tbl.style = "Light Grid Accent 1"
        headers = ["标准", "条款编号", "条款标题", "内容类型"]
        for i, h in enumerate(headers):
            cell = tbl.rows[0].cells[i]
            cell.text = ""
            _font(cell.paragraphs[0].add_run(h), size=10, bold=True)

        def sort_key(item):
            c = item[1]
            return (c.get("standard_name", ""), c.get("clause_id", ""))

        for _, c in sorted(seen.items(), key=sort_key):
            cells = tbl.add_row().cells
            values = [
                c.get("standard_name", ""),
                c.get("clause_id", ""),
                c.get("title", ""),
                "标准原文" if c.get("text_type") == "original" else "要点摘要",
            ]
            for i, v in enumerate(values):
                cells[i].text = ""
                _font(cells[i].paragraphs[0].add_run(str(v)),
                      name=MONO_FONT if i == 1 else CN_FONT, size=9.5)

        _para(doc, "", space_after=8)
        _para(doc, f"共引用 {len(seen)} 个条款。", size=9.5, color=GREY)

    # ---------------- 末尾免责声明 ----------------
    doc.add_page_break()
    _para(doc, "免责声明", size=14, bold=True, color=WARN, space_after=8)
    _para(doc, DISCLAIMER, size=10.5, space_after=10)
    _para(doc, "关于知识库来源：", size=11, bold=True, space_after=4)
    for line in [
        "· NIST CSF 2.0：美国政府作品，属公共领域，收录官方原文。",
        "· OWASP Top 10 2021：采用 CC BY-SA 4.0 许可，署名 OWASP Foundation。",
        "· 等保2.0（GB/T 22239-2019）：本项目未收录国标原文，仅收录公开控制项名称"
        "与自行改写的要点摘要，一切以正式发布的标准文本为准，请通过正规渠道购买。",
        "· 用户自行导入的资料：内容与编号由用户提供，本项目不对其准确性负责。",
    ]:
        _para(doc, line, size=10, color=GREY, space_after=3)

    buffer = io.BytesIO()
    doc.save(buffer)
    return buffer.getvalue()
