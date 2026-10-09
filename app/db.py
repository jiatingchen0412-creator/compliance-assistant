"""SQLite 数据库层：建表、连接、通用读写。

设计要点：
- 单文件 data/app.db，备份就是复制这一个文件。
- 条款表 clauses 是主表；clauses_fts 是全文索引表（存 jieba 分词后的空格串）；
  clause_vectors 存语义向量（float32 二进制）。
- 所有写入都用参数化 SQL，避免注入。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterable, Iterator, Sequence

from . import config

# --------------------------------------------------------------------------
# 建表语句
# --------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS standards (
    id           TEXT PRIMARY KEY,      -- 例如 dengbao_2.0
    name         TEXT NOT NULL,         -- 等保2.0
    full_name    TEXT,                  -- 信息安全技术 网络安全等级保护基本要求
    version      TEXT,                  -- GB/T 22239-2019
    publisher    TEXT,
    license      TEXT,                  -- public-domain / CC BY-SA 4.0 / 摘要
    source_url   TEXT,
    note         TEXT,                  -- 版权与使用说明
    imported_at  TEXT
);

CREATE TABLE IF NOT EXISTS clauses (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    standard_id  TEXT NOT NULL,
    clause_id    TEXT NOT NULL,         -- 8.1.4.1 / PR.AA-01 / A01
    title        TEXT NOT NULL,
    chapter_path TEXT NOT NULL DEFAULT '[]',   -- JSON 数组：章节层级
    text         TEXT NOT NULL,         -- 原文或摘要正文
    text_type    TEXT NOT NULL DEFAULT 'summary', -- original | summary
    summary      TEXT,                  -- 一句话要点
    keywords     TEXT NOT NULL DEFAULT '[]',   -- JSON 数组
    risk_tags    TEXT NOT NULL DEFAULT '[]',   -- JSON 数组
    level_scope  TEXT NOT NULL DEFAULT '[]',   -- JSON 数组（等保分级）
    sort_key     TEXT NOT NULL DEFAULT '',     -- 排序用
    enabled      INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT,
    -- 来源追溯（P2-1 新增）：区分内置种子和用户导入，便于判断可信度
    source_kind  TEXT NOT NULL DEFAULT 'builtin',  -- builtin | user
    batch_id     INTEGER,               -- 来自哪一次导入
    imported_at  TEXT,
    UNIQUE (standard_id, clause_id)
);

CREATE INDEX IF NOT EXISTS idx_clauses_standard ON clauses(standard_id);
CREATE INDEX IF NOT EXISTS idx_clauses_sort ON clauses(sort_key);

-- 全文索引：tokens 列存的是 jieba 分词后用空格连接的文本
CREATE VIRTUAL TABLE IF NOT EXISTS clauses_fts USING fts5(
    tokens,
    tokenize = 'unicode61 remove_diacritics 2'
);

-- 每个词出现在多少条条款里（文档频率）。用来算 IDF：
-- "的""我们"这种到处都是的词权重趋近 0，"身份鉴别""越权"这种稀有词权重高。
-- 这是关键词检索能给出**绝对分数**、进而可靠判断"知识库里到底有没有"的关键。
CREATE TABLE IF NOT EXISTS token_df (
    token TEXT PRIMARY KEY,
    df    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS clause_vectors (
    clause_rowid INTEGER PRIMARY KEY,
    dim          INTEGER NOT NULL,
    vec          BLOB NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id         TEXT PRIMARY KEY,
    title      TEXT NOT NULL DEFAULT '新对话',
    created_at TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role       TEXT NOT NULL,           -- user | assistant
    content    TEXT NOT NULL,
    payload    TEXT,                    -- JSON：引用、风险、追问等
    created_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS import_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    filename      TEXT,
    standard_id   TEXT,
    clauses_added INTEGER DEFAULT 0,
    clauses_updated INTEGER DEFAULT 0,
    note          TEXT,
    created_at    TEXT
);

-- 导入批次（P2-1/P2-2）：每一次导入都留下一条记录，
-- 条款通过 clauses.batch_id 关联回来，这样"这条是从哪来的""能不能撤回去"都有据可查。
CREATE TABLE IF NOT EXISTS import_batches (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    filename      TEXT,
    standard_id   TEXT,
    standard_name TEXT,
    kind          TEXT,                -- seed 内置种子 / json 用户JSON / document 用户文档
    source_kind   TEXT,                -- builtin | user
    added         INTEGER DEFAULT 0,
    updated       INTEGER DEFAULT 0,
    skipped       INTEGER DEFAULT 0,
    note          TEXT,
    backup_path   TEXT,                -- 导入前自动备份的数据库文件
    reverted      INTEGER DEFAULT 0,   -- 是否已被撤销
    created_at    TEXT
);
"""


# 老版本数据库缺少的列：(表名, 列名, 列定义)
# SQLite 支持 ADD COLUMN，加完老数据会自动填上默认值。
MIGRATIONS: list[tuple[str, str, str]] = [
    ("clauses", "source_kind", "TEXT NOT NULL DEFAULT 'builtin'"),
    ("clauses", "batch_id", "INTEGER"),
    ("clauses", "imported_at", "TEXT"),
]


def _migrate(conn: sqlite3.Connection) -> list[str]:
    """给已有的数据库补上后来新增的列。幂等，可以反复调用。"""
    applied: list[str] = []
    for table, column, ddl in MIGRATIONS:
        try:
            cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        except sqlite3.Error:
            continue
        if not cols:
            continue  # 表还不存在，建表语句会处理
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
            applied.append(f"{table}.{column}")
    return applied


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    """建表（幂等，可重复调用），并给老数据库补上新增的列。"""
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        applied = _migrate(conn)
    if applied:
        print(f"[数据库] 已升级结构：{'、'.join(applied)}")
    # 老数据的来源标记：没有批次的都算内置种子
    with get_conn() as conn:
        conn.execute(
            "UPDATE clauses SET source_kind = 'builtin' "
            "WHERE source_kind IS NULL OR source_kind = ''"
        )
        conn.execute(
            "UPDATE clauses SET imported_at = created_at "
            "WHERE imported_at IS NULL AND created_at IS NOT NULL"
        )


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------
def dump_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def load_json(value: Any, default: Any = None) -> Any:
    if value is None or value == "":
        return default if default is not None else []
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default if default is not None else []


def query(sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(sql, params).fetchall()


def query_one(sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(sql, params).fetchone()


def execute(sql: str, params: Sequence[Any] = ()) -> int:
    with get_conn() as conn:
        cur = conn.execute(sql, params)
        return cur.lastrowid or cur.rowcount


def execute_many(sql: str, rows: Iterable[Sequence[Any]]) -> None:
    with get_conn() as conn:
        conn.executemany(sql, rows)


# --------------------------------------------------------------------------
# 条款行 -> 字典
# --------------------------------------------------------------------------
def clause_to_dict(row: sqlite3.Row, include_text: bool = True) -> dict:
    keys = row.keys() if hasattr(row, "keys") else []
    item = {
        "rowid": row["id"],
        "standard_id": row["standard_id"],
        "clause_id": row["clause_id"],
        "title": row["title"],
        "chapter_path": load_json(row["chapter_path"]),
        "text_type": row["text_type"],
        "summary": row["summary"] or "",
        "keywords": load_json(row["keywords"]),
        "risk_tags": load_json(row["risk_tags"]),
        "level_scope": load_json(row["level_scope"]),
        "sort_key": row["sort_key"],
        "enabled": bool(row["enabled"]),
        # 来源追溯：内置种子还是用户导入。老数据库可能没有这几列，用 keys() 兜一下。
        "source_kind": (row["source_kind"] if "source_kind" in keys else None) or "builtin",
        "batch_id": row["batch_id"] if "batch_id" in keys else None,
        "imported_at": (row["imported_at"] if "imported_at" in keys else None) or "",
    }
    if include_text:
        item["text"] = row["text"]
    return item


def set_setting(key: str, value: str) -> None:
    execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def get_setting(key: str, default: str | None = None) -> str | None:
    row = query_one("SELECT value FROM settings WHERE key = ?", (key,))
    return row["value"] if row else default
