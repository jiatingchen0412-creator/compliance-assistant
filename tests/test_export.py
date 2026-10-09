"""Word 报告导出测试（P3-2）。

不依赖服务，直接用合成的问答数据生成报告并回读验证结构。
重点是"导出的东西真的是份完整文档"，而不只是"函数没报错"。

用法（在项目根目录执行）：
    .venv\\Scripts\\python.exe tests\\test_export.py
"""
from __future__ import annotations

import io
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from docx import Document  # noqa: E402

from app import export  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def case(name: str):
    def wrapper(fn):
        RESULTS.append((name, fn))
        return fn
    return wrapper


MESSAGES = [
    {
        "role": "user",
        "content": "我们公司十几个人都用同一个管理员账号登录进销存系统",
        "created_at": "2026-09-28 22:00:00",
    },
    {
        "role": "assistant",
        "content": "存在高风险。多名管理员共用一个账号，违反访问控制要求。\n建议立即改为一人一号。\n\n"
                   "本回答仅用于辅助自查，不构成正式测评结论。",
        "created_at": "2026-09-28 22:00:02",
        "citations": [
            {
                "standard_id": "dengbao_2.0", "standard_name": "等保2.0",
                "clause_id": "8.1.4.1", "title": "身份鉴别（标识与口令复杂度）",
                "text": "要求对登录用户进行身份标识和鉴别……",
                "text_type": "summary", "risk_tags": ["access_control"],
            },
            {
                "standard_id": "nist_csf_2.0", "standard_name": "NIST CSF 2.0",
                "clause_id": "PR.AA-01", "title": "身份管理与访问控制",
                "text": "Identities and credentials are managed...",
                "text_type": "original", "risk_tags": [],
            },
        ],
        "risks": [
            {"tag": "access_control", "label": "访问控制",
             "why": "账号共享是最常见的越权入口。", "clauses": ["8.1.4.1"]},
        ],
        "found": True,
        "mode": "llm",
    },
    {
        "role": "user",
        "content": "客户的手机号存在数据库里没有加密",
        "created_at": "2026-09-28 22:01:00",
    },
    {
        "role": "assistant",
        "content": "存在数据泄露风险，建议对手机号加密存储。\n\n本回答仅用于辅助自查。",
        "created_at": "2026-09-28 22:01:03",
        "citations": [
            {
                "standard_id": "dengbao_2.0", "standard_name": "等保2.0",
                "clause_id": "8.1.4.19", "title": "数据保密性（存储保密性）",
                "text": "要求对重要数据在存储过程中采取保护措施……",
                "text_type": "summary", "risk_tags": ["encryption"],
            },
        ],
        "risks": [
            {"tag": "encryption", "label": "数据加密",
             "why": "未加密存储的个人信息一旦泄露影响重大。", "clauses": ["8.1.4.19"]},
            {"tag": "personal_info", "label": "个人信息保护",
             "why": "手机号属于个人信息。", "clauses": ["8.1.4.19"]},
        ],
        "found": True,
        "mode": "llm",
    },
]

ENV: dict = {}


@case("生成的是合法的 docx（ZIP 结构）")
def test_valid_zip():
    data = ENV.get("data") or export.build_report("测试主题", "abc123", MESSAGES)
    ENV["data"] = data
    ok = data[:2] == b"PK" and len(data) > 5000
    return (
        (ok, f"文件 {len(data) / 1024:.1f} KB，头部合法")
        if ok
        else (False, f"头部 {data[:2]!r}，大小 {len(data)} 字节")
    )


@case("能被 python-docx 正常打开")
def test_openable():
    doc = Document(io.BytesIO(ENV["data"]))
    ENV["doc"] = doc
    return True, f"解析出 {len(doc.paragraphs)} 个段落、{len(doc.tables)} 个表格"


@case("包含封面标题和自查主题")
def test_cover():
    text = "\n".join(p.text for p in ENV["doc"].paragraphs)
    table_text = "\n".join(
        c.text for t in ENV["doc"].tables for row in t.rows for c in row.cells
    )
    all_text = text + "\n" + table_text
    missing = [k for k in ("安全合规自查报告", "测试主题", "abc123") if k not in all_text]
    return (
        (True, "标题、主题、编号都在")
        if not missing
        else (False, f"缺少：{missing}")
    )


@case("⚠️ 高风险汇总按类型去重并列出依据")
def test_risk_summary():
    text = "\n".join(p.text for p in ENV["doc"].paragraphs)
    checks = {
        "汇总标题": "高风险事项汇总" in text,
        "访问控制": "⚠️ 访问控制" in text,
        "数据加密": "⚠️ 数据加密" in text,
        "个人信息保护": "⚠️ 个人信息保护" in text,
        "风险原因": "账号共享是最常见的越权入口" in text,
    }
    bad = [k for k, v in checks.items() if not v]
    return (True, f"{len(checks)} 项检查全部通过") if not bad else (False, f"缺失：{bad}")


@case("逐条问答包含问题和回答")
def test_qa_pairs():
    text = "\n".join(p.text for p in ENV["doc"].paragraphs)
    checks = [
        "问题 1" in text,
        "问题 2" in text,
        "同一个管理员账号" in text,
        "手机号存在数据库里没有加密" in text,
        "存在高风险" in text,
        "建议对手机号加密存储" in text,
    ]
    return (True, "两个问答都完整") if all(checks) else (False, "有内容缺失")


@case("引用条款带编号、标题和内容类型标注")
def test_citations():
    text = "\n".join(p.text for p in ENV["doc"].paragraphs)
    checks = {
        "等保条款号": "8.1.4.1" in text,
        "等保条款标题": "身份鉴别（标识与口令复杂度）" in text,
        "摘要标注": "要点摘要，非标准原文" in text,
        "NIST 条款号": "PR.AA-01" in text,
        "存储保密性": "8.1.4.19" in text,
    }
    bad = [k for k, v in checks.items() if not v]
    return (True, f"{len(checks)} 项引用检查通过") if not bad else (False, f"缺失：{bad}")


@case("附录条款清单是真正的表格且去重")
def test_appendix_table():
    tables = ENV["doc"].tables
    # 封面信息表 + 附录条款表
    if len(tables) < 2:
        return False, f"只找到 {len(tables)} 个表格"
    appendix = tables[-1]
    header = [c.text for c in appendix.rows[0].cells]
    expected = ["标准", "条款编号", "条款标题", "内容类型"]
    if header != expected:
        return False, f"附录表头不对：{header}"

    ids = [appendix.rows[i].cells[1].text for i in range(1, len(appendix.rows))]
    # 8.1.4.1、8.1.4.19、PR.AA-01 共 3 条，且不能重复
    if len(ids) != len(set(ids)):
        return False, f"附录里有重复条款：{ids}"
    if len(ids) != 3:
        return False, f"期望 3 个条款，实际 {len(ids)}：{ids}"
    return True, f"附录表格 {len(ids)} 行，条款无重复：{ids}"


@case("最后一页是免责声明，且写明来源与版权")
def test_disclaimer():
    text = "\n".join(p.text for p in ENV["doc"].paragraphs)
    checks = {
        "不构成正式测评结论": "不构成正式测评结论" in text,
        "建议咨询顾问": "专业安全顾问" in text,
        "等保版权说明": "未收录国标原文" in text,
        "NIST 公共领域": "公共领域" in text,
        "OWASP 许可": "CC BY-SA 4.0" in text,
        "用户导入免责": "不对其准确性负责" in text,
    }
    bad = [k for k, v in checks.items() if not v]
    return (True, f"{len(checks)} 项合规声明都在") if not bad else (False, f"缺失：{bad}")


@case("空会话也能生成（不崩溃）")
def test_empty_messages():
    try:
        data = export.build_report("空对话", "empty1", [])
    except Exception as exc:
        return False, f"抛异常：{type(exc).__name__}: {exc}"
    return (
        (len(data) > 1000, f"生成了 {len(data) / 1024:.1f} KB")
        if len(data) > 1000
        else (False, "生成的内容过小")
    )


@case("能另存到磁盘（模拟真实下载）")
def test_save_to_disk():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "报告.docx"
        path.write_bytes(ENV["data"])
        size = path.stat().st_size
        reopened = Document(str(path))
        return (
            (size > 5000, f"已写入 {size / 1024:.1f} KB 并可重新打开")
            if size > 5000
            else (False, f"文件过小：{size} 字节")
        )


def main() -> int:
    print("=" * 70)
    print("Word 报告导出测试 · 用合成数据验证文档结构")
    print("=" * 70)

    ENV["data"] = export.build_report("测试主题", "abc123", MESSAGES)

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
