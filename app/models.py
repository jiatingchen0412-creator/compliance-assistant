"""接口数据结构（Pydantic）。"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    question: str = Field(..., description="用户的问题")
    session_id: str | None = Field(default=None, description="会话 id，不传则新建")
    stream: bool = Field(default=True, description="是否流式返回")


class Citation(BaseModel):
    standard_id: str
    standard_name: str
    clause_id: str
    title: str
    chapter_path: list[str] = []
    text: str = ""
    text_type: str = "summary"
    risk_tags: list[str] = []
    score: float = 0.0
    source_url: str = ""
    verified: bool = True   # 是否通过服务端引用白名单校验


class RiskFlag(BaseModel):
    tag: str
    label: str
    emoji: str = "⚠️"
    why: str = ""
    clauses: list[str] = []


class ChatAnswer(BaseModel):
    answer: str
    citations: list[Citation] = []
    risks: list[RiskFlag] = []
    need_clarification: bool = False
    followup_questions: list[str] = []
    found: bool = True
    mode: str = "llm"           # llm | retrieval_only | no_result
    notes: list[str] = []


class SessionInfo(BaseModel):
    id: str
    title: str
    created_at: str | None = None
    updated_at: str | None = None
    message_count: int = 0


class ClauseOut(BaseModel):
    rowid: int
    standard_id: str
    clause_id: str
    title: str
    chapter_path: list[str] = []
    text: str = ""
    text_type: str = "summary"
    summary: str = ""
    keywords: list[str] = []
    risk_tags: list[str] = []
    level_scope: list[str] = []
    sort_key: str = ""
    enabled: bool = True
    # 来源追溯
    source_kind: str = "builtin"     # builtin 内置种子 | user 用户导入
    batch_id: int | None = None
    imported_at: str = ""
    standard_name: str = ""
    source_url: str = ""


class StandardOut(BaseModel):
    id: str
    name: str
    full_name: str | None = None
    version: str | None = None
    publisher: str | None = None
    license: str | None = None
    source_url: str | None = None
    note: str | None = None
    clause_count: int = 0
    enabled_count: int = 0
    imported_at: str | None = None


class KbStats(BaseModel):
    standards: int = 0
    clauses: int = 0
    enabled_clauses: int = 0
    with_vector: int = 0
    by_risk: dict[str, int] = {}
    by_standard: dict[str, int] = {}
    vector_backend: str = ""
    vector_model: str = ""


class ImportResult(BaseModel):
    filename: str
    standard_id: str
    added: int = 0
    updated: int = 0
    skipped: int = 0
    note: str = ""


class ApiMessage(BaseModel):
    ok: bool = True
    message: str = ""
    data: Any = None
