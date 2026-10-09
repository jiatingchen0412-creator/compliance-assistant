"""程序总入口。

启动时做四件事：
1. 建数据库表（幂等）
2. 首次运行自动导入种子知识库 + 建索引（后台线程，不卡启动）
3. 尝试拉起 Ollama 服务（用户没手动开也能用）
4. 启动 Web 服务，并自动打开浏览器

访问地址：http://127.0.0.1:8765
"""
from __future__ import annotations

import threading
import webbrowser
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config, db, ingest, llm
from .logging_setup import get_logger, log_startup_banner, setup_logging
from .routers import chat, kb

# 日志要在第一时间初始化：后面所有模块的 logger 都会用同一套 handler
setup_logging()
log = get_logger(__name__)

# 启动状态，前端轮询用
STARTUP_STATE: dict = {
    "phase": "starting",
    "message": "正在启动…",
    "progress": 0,
    "total": 0,
    "detail": "",
    "ready": False,
    "error": "",
}

# 质量自检结果（后台线程跑，不拖慢启动）
QUALITY_STATE: dict = {
    "checked": False,
    "passed": True,
    "skipped": True,
    "failures": [],
    "items": [],
    "checked_at": "",
}


def _progress(stage: str, done: int, total: int) -> None:
    STARTUP_STATE.update(
        {"phase": stage, "progress": done, "total": total,
         "message": f"{stage} {done}/{total}" if total else stage}
    )


def _bootstrap() -> None:
    try:
        STARTUP_STATE.update({"phase": "database", "message": "正在准备数据库…"})
        db.init_db()

        STARTUP_STATE.update({"phase": "seed", "message": "正在检查知识库…"})
        result = ingest.ensure_seed_loaded(_progress)
        if result.get("skipped"):
            STARTUP_STATE.update({"detail": result.get("reason", "")})

        STARTUP_STATE.update({"phase": "ollama", "message": "正在检查本地大模型服务…"})
        if not llm.is_running():
            llm.try_start_server(wait_seconds=20)

        # 开发日志：确保当天日志文件存在，并记一条启动记录。
        # 这一步失败绝不能影响服务启动，devlog 内部已经做了兜底。
        clause_count = 0
        model_ready = False
        try:
            from . import devlog

            row = db.query_one("SELECT COUNT(*) AS c FROM clauses")
            clause_count = int(row["c"]) if row else 0
            model_state = llm.health()
            model_ready = bool(model_state.get("model_ready"))
            devlog.record_startup(
                clause_count=clause_count,
                model=config.LLM_MODEL,
                model_ready=model_ready,
            )
        except Exception as exc:
            log.warning("开发日志写入跳过：%s", exc)

        vector_ok = False
        try:
            from .retrieval import get_engine

            vector_ok = get_engine().vector_available
        except Exception:
            pass

        STARTUP_STATE.update({
            "phase": "ready",
            "message": "准备完成",
            "ready": True,
        })
        log.info(
            "启动完成 · 知识库 %d 条 · 语义检索 %s · 模型 %s（%s）",
            clause_count,
            "启用" if vector_ok else "未启用",
            config.LLM_MODEL,
            "就绪" if model_ready else "未就绪",
        )
    except Exception as exc:  # 启动失败也要让前端看到原因
        log.exception("启动过程中出错")
        STARTUP_STATE.update({
            "phase": "error",
            "message": f"启动过程中出错：{exc}",
            "error": str(exc),
            "ready": True,
        })


def _run_quality_check() -> None:
    """后台跑一次检索质量自检。

    目的：阈值/权重/分词词典被改坏时，指标会悄悄掉下去而界面看不出异常。
    这里把它变成页面上一条黄色警告，避免"看起来正常其实已经退化"。
    """
    try:
        from . import quality

        verdict = quality.run_check()
        QUALITY_STATE.update({
            "checked": True,
            "passed": verdict["passed"],
            "skipped": verdict.get("skipped", False),
            "failures": verdict.get("failures", []),
            "items": verdict.get("items", []),
            "checked_at": db.now(),
        })
        if verdict["passed"]:
            log.info("质量自检通过")
        else:
            log.warning("质量自检未通过：%s", "；".join(verdict["failures"]))
    except Exception as exc:
        log.warning("质量自检执行失败（不影响使用）：%s", exc)
        QUALITY_STATE.update({"checked": True, "passed": True, "skipped": True,
                              "failures": [f"自检未能执行：{exc}"]})


@asynccontextmanager
async def lifespan(app: FastAPI):
    thread = threading.Thread(target=_bootstrap, name="bootstrap", daemon=True)
    thread.start()
    # 质量自检单独一个线程：它要跑 30 道题，不能拖慢启动
    threading.Thread(target=_run_quality_check, name="quality-check", daemon=True).start()
    yield


app = FastAPI(
    title="安全合规自查助手",
    description="基于等保2.0、NIST CSF 2.0、OWASP Top 10 2021 的中小企业安全合规辅助自查工具",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(chat.router)
app.include_router(kb.router)


@app.get("/api/health")
def health():
    model = llm.health()
    engine_vector = False
    try:
        from .retrieval import get_engine

        engine_vector = get_engine().vector_available
    except Exception:
        pass
    return {
        "ok": True,
        "startup": STARTUP_STATE,
        "llm": model,
        "vector_search": engine_vector,
        "quality": QUALITY_STATE,
        "config": config.as_dict(),
    }


@app.get("/api/quality")
def quality_state():
    """检索质量自检结果。前端据此决定要不要显示警告条。"""
    return QUALITY_STATE


@app.post("/api/quality/recheck")
def quality_recheck():
    """手动重新自检（改了检索参数后不想重启服务时用）。"""
    threading.Thread(target=_run_quality_check, daemon=True).start()
    return {"ok": True, "message": "已开始重新自检，几秒后刷新查看结果"}


@app.get("/api/startup")
def startup_state():
    return STARTUP_STATE


@app.get("/")
def index():
    return FileResponse(config.WEB_DIR / "index.html")


@app.get("/kb")
def kb_page():
    return FileResponse(config.WEB_DIR / "kb.html")


@app.exception_handler(Exception)
async def unhandled(request, exc: Exception):
    # 必须显式传 exc_info=exc：
    # 用 log.exception() 依赖 sys.exc_info()，而异常处理器被调用时
    # 不一定还处在 except 块里，实测只记下 "NoneType: None"，堆栈全丢了。
    log.error(
        "接口未捕获异常 · %s %s", request.method, request.url.path, exc_info=exc
    )
    return JSONResponse(
        status_code=500,
        content={
            "ok": False,
            "message": "服务内部错误，请刷新页面重试。如果反复出现，请查看 data\\logs\\app.log 里的错误详情。",
            "detail": str(exc)[:300],
        },
    )


app.mount("/static", StaticFiles(directory=str(config.WEB_DIR)), name="static")


def _port_in_use(port: int) -> bool:
    """端口是不是已经被占用了（通常是用户重复双击了启动脚本）。"""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def run() -> None:
    import uvicorn

    url = f"http://{config.APP_HOST}:{config.APP_PORT}"

    # 重复启动是很常见的操作（用户忘了已经在跑）。这里友好处理，
    # 不要抛端口冲突的英文堆栈让用户一脸茫然。
    if _port_in_use(config.APP_PORT):
        print()
        print("=" * 60)
        print("  助手已经在运行了")
        print("=" * 60)
        print()
        print(f"  正在为您打开浏览器：{url}")
        print()
        print("  如果浏览器没反应，请手动复制上面的地址打开。")
        print("  想停止服务：双击『停用助手.bat』，或关掉运行中的那个黑窗口。")
        print()
        if config.APP_AUTO_OPEN:
            webbrowser.open(url)
        return

    log_startup_banner(url)

    if config.APP_AUTO_OPEN:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()

    uvicorn.run(
        "app.main:app",
        host=config.APP_HOST,
        port=config.APP_PORT,
        log_level="info",
        access_log=False,   # 静态资源的访问日志太吵，问答日志已经够用了
    )


if __name__ == "__main__":
    run()
