"""一键体检：打印服务、模型、知识库、质量自检的当前状态。

用法（服务需要正在运行）：
    .venv\\Scripts\\python.exe tools\\health_check.py

什么时候用：
- 用户反馈"好像有问题"，先跑这个看整体状态
- 改完配置重启后，确认各项都正常
- 运维手册 docs/08 里让用户提供排查信息时，直接跑这个把输出发过来
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from app import config  # noqa: E402

BASE = f"http://{config.APP_HOST}:{config.APP_PORT}"


def line(title: str) -> None:
    print(f"\n【{title}】")


def main() -> int:
    parser = argparse.ArgumentParser(description="一键体检")
    parser.add_argument("--url", default=BASE, help="服务地址")
    args = parser.parse_args()

    # trust_env=False：本机 127.0.0.1 不能被系统代理（Clash 等）截走，否则 502
    client = httpx.Client(timeout=90, base_url=args.url, trust_env=False)

    print("=" * 62)
    print("安全合规自查助手 · 一键体检")
    print("=" * 62)
    print(f"服务地址：{args.url}")

    try:
        health = client.get("/api/health").json()
    except Exception as exc:
        print(f"\n接口连不上：{exc}")
        print("说明服务没在运行。请双击 启动助手.bat。")
        return 1

    line("服务")
    startup = health.get("startup", {})
    print(f"  启动状态：{'就绪' if startup.get('ready') else '启动中'}")
    print(f"  当前阶段：{startup.get('phase')} · {startup.get('message')}")
    if startup.get("error"):
        print(f"  启动错误：{startup['error']}")

    line("本地大模型")
    llm = health.get("llm", {})
    state_text = {
        "ok": "正常",
        "model_missing": "缺模型",
        "ollama_not_running": "Ollama 未运行",
    }.get(llm.get("status"), llm.get("status", "未知"))
    print(f"  状态：{state_text}")
    print(f"  地址：{llm.get('base_url')}")
    print(f"  模型：{llm.get('model')}（就绪：{'是' if llm.get('model_ready') else '否'}）")
    if llm.get("hint"):
        print(f"  提示：{llm['hint']}")

    line("检索")
    vector_text = "已启用" if health.get("vector_search") else "未启用（仅关键词）"
    print(f"  语义检索：{vector_text}")

    line("检索质量自检")
    quality = health.get("quality", {})
    if not quality.get("checked"):
        print("  还在执行中，请稍后重跑本命令")
    elif quality.get("skipped"):
        print(f"  已跳过：{quality.get('reason', '')}")
    else:
        print(f"  结论：{'✅ 达标' if quality.get('passed') else '❌ 跌破红线'}")
        for item in quality.get("items", []):
            flag = "OK" if item["passed"] else "XX"
            print(f"    {flag}  {item['name']:<14} {item['actual']}/{item['total']}  要求 {item['required']}")
        for failure in quality.get("failures", []):
            print(f"    ! {failure}")
    print(f"  检查时间：{quality.get('checked_at') or '-'}")

    line("知识库")
    try:
        stats = client.get("/api/kb/stats").json()
        print(f"  标准数：{stats['standards']}")
        print(f"  条款数：{stats['clauses']}（启用 {stats['enabled_clauses']}）")
        print(f"  已建向量：{stats['with_vector']}")
        print(f"  向量模型：{stats['vector_model'] or '未启用'}")
        print(f"  索引时间：{stats['last_index']}")
        print("  各标准条数：")
        for name, count in (stats.get("by_standard") or {}).items():
            print(f"    {name}：{count}")
        by_source = stats.get("by_source") or {}
        if by_source:
            builtin = by_source.get("builtin", 0)
            user = by_source.get("user", 0)
            print(f"  来源：内置 {builtin} 条 · 用户导入 {user} 条 · 导入批次 {stats.get('batches', 0)} 次")
    except Exception as exc:
        print(f"  读取失败：{exc}")

    line("配置")
    cfg = health.get("config", {})
    for key in ("llm_model", "retrieval_min_score", "retrieval_top_k",
                "enable_vector_search", "save_history", "db_path"):
        print(f"  {key} = {cfg.get(key)}")

    print()
    print("=" * 62)
    ok = (startup.get("ready") and llm.get("model_ready")
          and (quality.get("passed", True) or quality.get("skipped")))
    print("总体：" + ("✅ 一切正常" if ok else "⚠️ 有项目需要注意（见上方）"))
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
