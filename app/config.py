from __future__ import annotations

from pathlib import Path
import json
from datetime import date
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


PROVIDER_ENDPOINTS = {"openai": "https://api.openai.com/v1", "avalai": "https://api.avalai.org/v1"}
AVALAI_ENDPOINTS = {PROVIDER_ENDPOINTS["avalai"], "https://api.avalai.ir/v1"}


def placeholder_key(value: str) -> bool:
    """Recognize documentation placeholders without imposing a vendor key format."""
    text = value.strip().lower()
    return (not text or text in {"not-configured", "replace_with_your_api_token", "replace_with_your_api_key",
        "your_api_key", "your_openai_api_key", "your_avalai_api_key", "sk-...", "aa-...", "changeme"}
        or text.startswith(("replace_with_", "your_")) or (text.startswith("<") and text.endswith(">")))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore",
                                     hide_input_in_errors=True)

    data_dir: Path = Path("data")
    state_dir: Path = Path("var")
    # Legacy .env files selected AvalAI by base URL alone. New installations
    # explicitly select their provider in .env.example.
    llm_provider: Literal["openai", "avalai", "custom"] = "avalai"
    llm_api_key: SecretStr = SecretStr("")
    openai_api_key: SecretStr = Field(default=SecretStr(""), exclude=True, repr=False)
    llm_base_url: str = ""
    llm_model: str = "gpt-4.1"
    llm_timeout_seconds: float = Field(60, ge=5, le=180)
    llm_max_retries: int = Field(2, ge=0, le=3)
    llm_min_interval_seconds: float = Field(30, ge=0, le=30)
    embedding_model: str = "text-embedding-3-small"
    embedding_dimensions: int = Field(1536, ge=1, le=65536)
    embedding_batch_size: int = Field(64, ge=1, le=256)
    qdrant_url: str = ""
    qdrant_collection: str = "travel_documents"
    retrieval_top_k: int = Field(6, ge=2, le=12)
    chunk_max_tokens: int = Field(320, ge=100, le=700)
    chunk_overlap_tokens: int = Field(40, ge=0, le=100)
    as_of: str = "2026-11-16"
    max_history_messages: int = 10
    pdftotext_bin: str = "pdftotext"
    langfuse_enabled: bool = False
    langfuse_public_key: str = ""
    langfuse_secret_key: SecretStr = SecretStr("")
    langfuse_host: str = "https://cloud.langfuse.com"

    @field_validator("llm_provider", mode="before")
    @classmethod
    def normalize_provider(cls, value):
        return value.strip().lower() if isinstance(value, str) else value

    @field_validator("llm_api_key", "openai_api_key", mode="after")
    @classmethod
    def normalize_key(cls, value):
        text = value.get_secret_value().strip()
        return SecretStr("" if placeholder_key(text) else text)

    @model_validator(mode="after")
    def resolve_provider(self):
        base = self.llm_base_url.strip().rstrip("/")
        if not base:
            if self.llm_provider == "custom":
                raise ValueError("LLM_PROVIDER=custom requires an explicit LLM_BASE_URL.")
            base = PROVIDER_ENDPOINTS[self.llm_provider]
        try:
            parsed = urlsplit(base)
            valid = parsed.scheme in {"https", "http"} and bool(parsed.hostname)
            # Credentials belong in a secret environment variable, not a URL.
            valid = valid and not (parsed.username or parsed.password or parsed.query or parsed.fragment)
            _ = parsed.port
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("LLM_BASE_URL must be an HTTP(S) API base without credentials, query or fragment.")
        if self.llm_provider == "openai" and base != PROVIDER_ENDPOINTS["openai"]:
            raise ValueError("LLM_PROVIDER=openai requires https://api.openai.com/v1; remove the old LLM_BASE_URL or select custom explicitly.")
        if self.llm_provider == "avalai" and base not in AVALAI_ENDPOINTS:
            raise ValueError("LLM_PROVIDER=avalai requires https://api.avalai.org/v1 or https://api.avalai.ir/v1; use custom for another API.")
        self.llm_base_url = base
        # A globally exported OPENAI_API_KEY must never become the credential
        # for an unrelated compatible service. The alias is official-only.
        if self.llm_provider == "openai":
            generic, official = self.llm_api_key.get_secret_value(), self.openai_api_key.get_secret_value()
            if generic and official and generic != official:
                raise ValueError("Conflicting LLM_API_KEY and OPENAI_API_KEY for openai; configure only one key or the same value.")
            if not generic and official:
                self.llm_api_key = self.openai_api_key
        return self

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

    @property
    def source_observed_date(self) -> str | None:
        manifest = self.data_dir / "source-manifest.json"
        if not manifest.exists():
            return None
        value = json.loads(manifest.read_text(encoding="utf-8")).get("observed_date")
        return date.fromisoformat(value).isoformat() if value else None


def get_settings() -> Settings:
    return Settings()
