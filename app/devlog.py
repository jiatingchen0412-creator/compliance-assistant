"""开发日志：按天记录开发事项、待办与决策。

设计原则：
- **纯文本 Markdown**，不依赖数据库。就算程序全坏了，日志照样能看能改。
- **按天一个文件**，文件名就是日期（devlog/2026-09-28.md），方便 diff 和检索。
- **能自动的部分就自动**：服务每次启动都会确保当天的日志文件存在，
  并追加一条自动记录（启动时间、知识库规模、模型状态）。
- **能手动的地方就简单**：直接改 Markdown 也行，用 tools/devlog.py 追加也行。

日志里的五个固定小节：
    今日目标 / 已完成 / 待办 / 问题与决策 / 自动记录
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta
from pathlib import Path

from . import config

LOG_DIR = config.BASE_DIR / "devlog"
TEMPLATE_PATH = LOG_DIR / "TEMPLATE.md"

SECTIONS = ["今日目标", "已完成", "待办", "问题与决策", "自动记录"]

TEMPLATE_BODY = """# 开发日志 · {date}

> 本文件由 `tools/devlog.py` 维护，也可以直接用记事本手工编辑。
> 规则见 `devlog/README.md`。

## 今日目标
- （开工前先写清楚今天要做到什么）

## 已完成
- 

## 待办
- 

## 问题与决策
- 

## 自动记录
- 
"""


def today_str() -> str:
    return date.today().strftime("%Y-%m-%d")


def today_path(day: str | None = None) -> Path:
    return LOG_DIR / f"{day or today_str()}.md"


def _write_template(path: Path, day: str) -> None:
    """按模板创建当天的日志。

    优先用 devlog/TEMPLATE.md（你可以直接改它来定制格式）；
    文件不在就用代码里的兜底模板，保证永远建得出来。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    body = TEMPLATE_BODY
    if TEMPLATE_PATH.exists():
        try:
            body = TEMPLATE_PATH.read_text(encoding="utf-8")
        except Exception:
            body = TEMPLATE_BODY
    path.write_text(body.replace("{date}", day), encoding="utf-8")


def ensure_today(day: str | None = None) -> Path:
    """确保当天的日志文件存在（不存在就从模板创建）。返回文件路径。"""
    day = day or today_str()
    path = today_path(day)
    if not path.exists():
        _write_template(path, day)
    return path


def read(day: str | None = None) -> str:
    path = ensure_today(day)
    return path.read_text(encoding="utf-8")


def _insert_into_section(text: str, section: str, bullet: str) -> str:
    """把一条 bullet 追加到指定小节的末尾。小节不存在就补在文件末尾。"""
    lines = text.splitlines()
    header = f"## {section}"

    start = None
    for i, line in enumerate(lines):
        if line.strip() == header:
            start = i
            break

    if start is None:
        # 小节不存在，补一个
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(header)
        lines.append(bullet)
        return "\n".join(lines) + "\n"

    # 找到该小节的范围 [start+1, end)
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if lines[j].startswith("## "):
            end = j
            break

    # 在小节末尾插入（跳过尾部空行）
    insert_at = end
    while insert_at > start + 1 and not lines[insert_at - 1].strip():
        insert_at -= 1
    lines.insert(insert_at, bullet)
    return "\n".join(lines) + "\n"


def append(section: str, text: str, day: str | None = None, *, timestamp: bool = False) -> Path:
    """往指定小节追加一条。text 可以带多个换行，会按多行原样写入。"""
    if section not in SECTIONS:
        raise ValueError(f"未知小节：{section}，可选：{'、'.join(SECTIONS)}")
    path = ensure_today(day)
    content = path.read_text(encoding="utf-8")

    prefix = f"- {datetime.now().strftime('%H:%M')} " if timestamp else "- "
    body = str(text).strip()
    # 多行内容：第一行接在 "- " 后面，其余行缩进对齐
    bullet = prefix + body.replace("\n", "\n  ")

    # 占位行（"- "、"- （开工前先写清楚…）" 这类）先清掉，避免日志里全是空壳
    content = re.sub(rf"(## {section}\n)(?:- \S*\n|- （[^\n]*）\n)+", r"\1", content)

    path.write_text(_insert_into_section(content, section, bullet), encoding="utf-8")
    return path


def done(text: str, day: str | None = None) -> Path:
    return append("已完成", text, day, timestamp=True)


def todo(text: str, day: str | None = None) -> Path:
    return append("待办", text, day, timestamp=True)


def decision(text: str, day: str | None = None) -> Path:
    return append("问题与决策", text, day, timestamp=True)


def auto(text: str, day: str | None = None) -> Path:
    return append("自动记录", text, day, timestamp=True)


def list_days(limit: int = 30) -> list[str]:
    if not LOG_DIR.exists():
        return []
    days = sorted(
        (p.stem for p in LOG_DIR.glob("*.md")
         if re.fullmatch(r"\d{4}-\d{2}-\d{2}", p.stem)),
        reverse=True,
    )
    return days[:limit]


def summary(days: int = 7) -> str:
    """汇总最近若干天的日志要点。"""
    out: list[str] = [f"# 开发日志汇总（最近 {days} 天）", ""]
    cutoff = date.today() - timedelta(days=days - 1)
    found = False
    for day in list_days(limit=days):
        try:
            d = datetime.strptime(day, "%Y-%m-%d").date()
        except ValueError:
            continue
        if d < cutoff:
            continue
        found = True
        content = read(day)
        out.append(f"## {day}")
        for section in ("已完成", "待办", "问题与决策"):
            m = re.search(rf"## {section}\n(.*?)(?=\n## |\Z)", content, re.S)
            if not m:
                continue
            items = [x.strip() for x in m.group(1).splitlines() if x.strip().startswith("-")]
            items = [x for x in items if x not in ("-", "- （待填写）")]
            if items:
                out.append(f"**{section}**")
                out.extend(items)
                out.append("")
    if not found:
        out.append("（最近没有开发日志）")
    return "\n".join(out)


def record_startup(clause_count: int = 0, model: str = "", model_ready: bool = False) -> None:
    """服务启动时调用：确保当天日志存在，并记一条自动记录。

    这是"每天自动记录"的落地点——不用人记得去建文件。
    """
    try:
        ensure_today()
        status = "就绪" if model_ready else "未就绪"
        auto(
            f"服务启动 · 知识库 {clause_count} 条条款 · 模型 {model or '未配置'}（{status}）"
        )
    except Exception as exc:  # 日志写不了不能影响主流程
        logging.getLogger(__name__).warning("开发日志写入失败（不影响使用）：%s", exc)
