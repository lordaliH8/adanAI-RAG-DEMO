from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class HistoryMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=4000)


class RetrievalFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    document_id: str | None = Field(default=None, max_length=80)
    document_type: str | None = Field(default=None, max_length=50)
    city: str | None = Field(default=None, max_length=80)
    property_id: str | None = Field(default=None, max_length=100)
    source: str | None = Field(default=None, max_length=80)


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    question: str = Field(min_length=2, max_length=2000)
    history: list[HistoryMessage] = Field(default_factory=list, max_length=10)
    retrieval_mode: Literal["dense", "lexical", "hybrid", "hybrid_rerank"] = "hybrid"
    filters: RetrievalFilters = Field(default_factory=RetrievalFilters)


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    query: str = Field(min_length=2, max_length=2000)
    mode: Literal["dense", "lexical", "hybrid", "hybrid_rerank"] = "hybrid"
    limit: int = Field(6, ge=1, le=12)
    filters: RetrievalFilters = Field(default_factory=RetrievalFilters)
