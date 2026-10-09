"""应用日志：把运行信息落到文件，方便出问题时回溯。

为什么需要：
现在错误只打在黑色窗口里，窗口一关就没了。用户遇到问题时，
"刚才报的什么错"根本拿不出来。日志是对这个问题最直接的解法。

日志文件：`data/logs/app.log`
轮转规则：每天零点切分，保留 14 天，旧的自动删除。
编码：UTF-8（中文不会乱码）

日志级别：
    DEBUG   细节（检索候选、向量加载）
    INFO    正常事件（服务启动、每次问答的摘要）
    WARNING 降级、可恢复的问题（语义模型不可用、剔除非法引用）
    ERROR   出错了但有兜底（模型调用失败、导入失败）
    EXCEPTION 未捕获异常（带完整堆栈）
"""
from __future__ import annotations

import logging
import sys
from logging.handlers import TimedRotatingFileHandler

from . import config

LOG_DIR = config.DATA_DIR / "logs"
LOG_FILE = LOG_DIR / "app.log"
RETENTION_DAYS = 14

_configured = False

# 这些库的 DEBUG 日志会把文件刷爆（httpcore 一次请求能打十几行），
# 只有 WARNING 以上才值得记。
NOISY_PREFIXES = (
    "httpcore", "httpx", "http11", "urllib3", "asyncio", "huggingface_hub",
    "filelock", "PIL", "onnxruntime", "watchfiles", "multipart", "fsspec",
    "charset_normalizer", "requests", "python_multipart",
)


class _NoiseFilter(logging.Filter):
    """把第三方库的啰嗦日志挡掉，只留我们自己的。"""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno >= logging.WARNING:
            return True
        name = record.name
        for prefix in NOISY_PREFIXES:
            if name == prefix or name.startswith(prefix + "."):
                return False
        return True


class _ShortNameFilter(logging.Filter):
    """把 logger 名字压短，避免日志行太长。"""

    def filter(self, record: logging.LogRecord) -> bool:
        name = record.name
        for prefix in ("app.",):
            if name.startswith(prefix):
                name = name[len(prefix):]
        record.shortname = name
        return True


def setup_logging(level: int = logging.INFO) -> None:
    """初始化日志。重复调用是安全的（模块被 import 两次也不会重复加 handler）。"""
    global _configured
    if _configured:
        return

    LOG_DIR.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    # 清掉可能已存在的 handler，避免重复输出
    for handler in list(root.handlers):
        root.removeHandler(handler)

    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)-5s] %(shortname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    short_filter = _ShortNameFilter()
    noise_filter = _NoiseFilter()

    # --- 文件：每天零点切分，保留 14 天 ---
    try:
        file_handler = TimedRotatingFileHandler(
            LOG_FILE,
            when="midnight",
            interval=1,
            backupCount=RETENTION_DAYS,
            encoding="utf-8",
            delay=True,
        )
        file_handler.suffix = "%Y-%m-%d"
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(formatter)
        file_handler.addFilter(short_filter)
        file_handler.addFilter(noise_filter)
        root.addHandler(file_handler)
    except Exception as exc:  # 日志写不了不能影响主流程
        print(f"[日志] 无法创建日志文件（不影响使用）：{exc}")

    # --- 控制台：让黑色窗口里仍然能看到启动信息 ---
    try:
        console = logging.StreamHandler(sys.stdout)
        console.setLevel(level)
        console.setFormatter(formatter)
        console.addFilter(short_filter)
        console.addFilter(noise_filter)
        root.addHandler(console)
    except Exception:
        pass

    # uvicorn 的日志也走同一套 handler，别让它自己再输出一遍
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers = []
        lg.propagate = True

    # 未捕获异常也要落盘
    def _hook(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        logging.getLogger("未捕获异常").critical(
            "程序发生未捕获异常", exc_info=(exc_type, exc_value, exc_tb)
        )

    sys.excepthook = _hook

    _configured = True
    logging.getLogger(__name__).info(
        "日志系统已启动 · 文件 %s · 保留 %d 天", LOG_FILE, RETENTION_DAYS
    )


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


# --------------------------------------------------------------------------
# 问答摘要日志
# --------------------------------------------------------------------------
def log_qa(logger: logging.Logger, question: str, result: dict, elapsed: float) -> None:
    """每次问答记一行摘要，出问题时可以对照时间点查。

    例子：
        2026-09-28 22:30:01 [INFO ] routers.chat | 问答 耗时=2.41s 模式=llm
        命中=是 追问=否 引用=2 ⚠️=2 问题=客户的手机号存在数据库里没有做加密
    """
    q = (question or "").replace("\n", " ").strip()
    if len(q) > 50:
        q = q[:50] + "…"

    logger.info(
        "问答 耗时=%.2fs 模式=%s 命中=%s 追问=%s 引用=%d ⚠️=%d 问题=%s",
        elapsed,
        result.get("mode", "-"),
        "是" if result.get("found") else "否",
        "是" if result.get("need_clarification") else "否",
        len(result.get("citations") or []),
        len(result.get("risks") or []),
        q,
    )

    # 被剔除的非法引用要留痕——这是发现"模型开始编条款号"的信号
    for note in result.get("notes") or []:
        if "剔除" in note or "未在知识库" in note:
            logger.warning("引用异常 | %s | 问题=%s", note, q)


def log_startup_banner(url: str) -> None:
    """启动横幅。用户看得懂，也便于日志里定位每次启动。"""
    logger = logging.getLogger("启动")
    logger.info("=" * 58)
    logger.info("  安全合规自查助手")
    logger.info("  请在浏览器打开：%s", url)
    logger.info("  关闭本窗口即可停止服务（或双击 停用助手.bat）")
    logger.info("=" * 58)
