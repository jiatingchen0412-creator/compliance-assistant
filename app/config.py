"""配置读取：优先读 .env 文件，其次读环境变量，最后用默认值。

原则：任何一项缺失都不能让程序崩掉，要有安全的默认值。
"""
from __future__ import annotations

import os
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - 依赖没装好时也不崩
    def load_dotenv(*_args, **_kwargs):  # type: ignore
        return False

# ---------- 路径 ----------
BASE_DIR = Path(__file__).resolve().parent.parent          # 项目根目录
DATA_DIR = BASE_DIR / "data"
SEED_DIR = DATA_DIR / "seed"
WEB_DIR = BASE_DIR / "web"
DB_PATH = DATA_DIR / "app.db"
VECTOR_CACHE_DIR = DATA_DIR / "models"                     # 语义模型下载缓存
ENV_PATH = BASE_DIR / ".env"

DATA_DIR.mkdir(parents=True, exist_ok=True)
VECTOR_CACHE_DIR.mkdir(parents=True, exist_ok=True)

load_dotenv(ENV_PATH)

# --------------------------------------------------------------------------
# HuggingFace 下载环境（必须在 import fastembed 之前设置好）
# 踩过的坑：新版 huggingface_hub 默认走 xet 通道，国内镜像站不支持，会报 401，
#          所以强制关闭 xet，改用普通 HTTP 下载；并默认走国内镜像加速。
# --------------------------------------------------------------------------
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
os.environ.setdefault("HF_ENDPOINT", os.getenv("HF_MIRROR", "https://hf-mirror.com"))


def _str(key: str, default: str) -> str:
    value = os.getenv(key)
    return value.strip() if value and value.strip() else default


def _int(key: str, default: int) -> int:
    try:
        return int(_str(key, str(default)))
    except (TypeError, ValueError):
        return default


def _float(key: str, default: float) -> float:
    try:
        return float(_str(key, str(default)))
    except (TypeError, ValueError):
        return default


def _bool(key: str, default: bool) -> bool:
    value = _str(key, str(default)).lower()
    return value in {"1", "true", "yes", "on", "y"}


# ---------- Web ----------
APP_HOST = _str("APP_HOST", "127.0.0.1")
APP_PORT = _int("APP_PORT", 8765)
APP_AUTO_OPEN = _bool("APP_AUTO_OPEN", True)

# ---------- 大模型 ----------
OLLAMA_BASE_URL = _str("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
LLM_MODEL = _str("LLM_MODEL", "qwen2.5:7b-instruct")
LLM_TEMPERATURE = _float("LLM_TEMPERATURE", 0.0)
LLM_MAX_TOKENS = _int("LLM_MAX_TOKENS", 2048)
LLM_TIMEOUT = _int("LLM_TIMEOUT", 180)

# ---------- 检索 ----------
HYBRID_KEYWORD_WEIGHT = _float("HYBRID_KEYWORD_WEIGHT", 0.5)
HYBRID_VECTOR_WEIGHT = _float("HYBRID_VECTOR_WEIGHT", 0.5)
RETRIEVAL_MIN_SCORE = _float("RETRIEVAL_MIN_SCORE", 0.40)
RETRIEVAL_TOP_K = _int("RETRIEVAL_TOP_K", 6)
ENABLE_VECTOR_SEARCH = _bool("ENABLE_VECTOR_SEARCH", True)

# 真流式输出：边生成边推给前端，首字出现更快。
# 解析失败或提取不到内容时会自动回退到"生成完再回放"，设 false 可强制走老路径。
ENABLE_TRUE_STREAMING = _bool("ENABLE_TRUE_STREAMING", True)

# ---------- 其他 ----------
SAVE_HISTORY = _bool("SAVE_HISTORY", True)

# ---------- 高风险标签（与 data/seed/risk_terms.json 配合） ----------
# 命中这些标签的条款，回答时必须打 ⚠️
HIGH_RISK_TAGS = {"encryption", "access_control", "personal_info"}


def as_dict() -> dict:
    """给前端展示的当前配置（不含任何密钥，本地模型也没密钥）。"""
    return {
        "app_host": APP_HOST,
        "app_port": APP_PORT,
        "llm_backend": "ollama",
        "ollama_base_url": OLLAMA_BASE_URL,
        "llm_model": LLM_MODEL,
        "retrieval_min_score": RETRIEVAL_MIN_SCORE,
        "retrieval_top_k": RETRIEVAL_TOP_K,
        "enable_vector_search": ENABLE_VECTOR_SEARCH,
        "enable_true_streaming": ENABLE_TRUE_STREAMING,
        "save_history": SAVE_HISTORY,
        "db_path": str(DB_PATH),
    }
