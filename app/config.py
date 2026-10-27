from __future__ import annotations

from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    data_dir: Path = Path("data")
    state_dir: Path = Path("var")
    llm_api_key: SecretStr = SecretStr("")
    llm_base_url: str = "https://api.avalai.org/v1"
    llm_model: str = "gpt-4.1"
    llm_timeout_seconds: float = Field(60, ge=5, le=180)
    llm_max_retries: int = Field(2, ge=0, le=3)
    llm_min_interval_seconds: float = Field(30, ge=0, le=30)
    embedding_model: str = "text-embedding-3-small"
    embedding_dimensions: int = 1536
    embedding_batch_size: int = Field(64, ge=1, le=256)
    qdrant_url: str = ""
    qdrant_collection: str = "travel_documents"
    retrieval_top_k: int = Field(6, ge=2, le=12)
    chunk_max_tokens: int = Field(320, ge=100, le=700)
    chunk_overlap_tokens: int = Field(40, ge=0, le=100)
    as_of: str = "2026-10-27"
    max_history_messages: int = 10
    pdftotext_bin: str = "pdftotext"
    langfuse_enabled: bool = False
    langfuse_public_key: str = ""
    langfuse_secret_key: SecretStr = SecretStr("")
    langfuse_host: str = "https://cloud.langfuse.com"

    @property
    def db_path(self) -> Path:
        return self.state_dir / "travel.sqlite"

    @property
    def documents_dir(self) -> Path:
        return self.data_dir / "documents"

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def chunks_path(self) -> Path:
        return self.state_dir / "chunks.json"


def get_settings() -> Settings:
    return Settings()
