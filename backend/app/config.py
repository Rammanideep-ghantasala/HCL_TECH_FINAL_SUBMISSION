"""Runtime configuration, read once from environment variables.

Retrieval starts without credentials. Optional summaries use server-side secrets.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent
REPO_DIR = BACKEND_DIR.parent

MODEL_ID = "sentence-transformers/all-MiniLM-L6-v2"
# Commit of the model repository on Hugging Face that this build was tested with.
MODEL_REVISION = "1110a243fdf4706b3f48f1d95db1a4f5529b4d41"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore", case_sensitive=False)

    # --- server
    host: str = "0.0.0.0"
    port: int = Field(default=8080, ge=1, le=65535)
    log_level: str = "INFO"
    enable_api_docs: bool = False

    # --- data and artifacts (all server-side; none is served as a static file)
    tickets_path: Path = REPO_DIR / "starter_kit" / "data" / "tickets.json"
    examples_path: Path = REPO_DIR / "starter_kit" / "data" / "new_tickets_example.json"
    model_dir: Path = BACKEND_DIR / "models" / "all-MiniLM-L6-v2"
    artifacts_dir: Path = BACKEND_DIR / "artifacts"
    relevance_config_path: Path = BACKEND_DIR / "config" / "relevance.json"
    frontend_dist: Path = REPO_DIR / "frontend" / "dist"
    # Writable folder for tickets saved through the assistant and their embedding
    # cache. The seed ticket file is never written. Mount a volume here so saved
    # tickets survive a container restart.
    data_dir: Path = BACKEND_DIR / "state"

    # --- ticket storage
    # "json": the seed file plus a JSON store in data_dir (local demonstrations).
    # "postgresql": every ticket comes from PostgreSQL; there is no JSON fallback.
    ticket_store: Literal["json", "postgresql"] = "json"
    # postgresql+psycopg://USER:PASSWORD@HOST:5432/DATABASE. Never logged or returned.
    database_url: SecretStr | None = None
    db_pool_size: int = Field(default=5, ge=1, le=50)
    db_max_overflow: int = Field(default=5, ge=0, le=50)
    db_pool_timeout_seconds: float = Field(default=10.0, gt=0)
    db_connect_timeout_seconds: int = Field(default=5, ge=1, le=60)
    db_statement_timeout_ms: int = Field(default=15_000, ge=100)
    db_load_batch_size: int = Field(default=500, ge=1, le=10_000)
    # 0 = off. When set, each process checks this often whether the ticket store
    # changed (saves on another instance, external edits) and rebuilds if so.
    index_refresh_interval_seconds: int = Field(default=0, ge=0)

    # --- saving resolved tickets
    save_enabled: bool = True
    max_saved_tickets: int = Field(default=2000, ge=1)
    # Name of a request header set by a trusted authenticating proxy (for example
    # "X-Forwarded-User"). Empty means no authenticated identity is available.
    # Only set it when the proxy strips that header from client requests.
    identity_header: str = ""
    escalation_email: str = "ticketexpert@solar.com"

    # --- retrieval
    semantic_enabled: bool = True
    # Diagnostic switch used by the evaluation to measure semantic-only behaviour.
    keyword_enabled: bool = True
    keyword_bigrams: bool = True
    keyword_stemming: bool = False
    keyword_top_k: int = Field(default=10, ge=1, le=100)
    semantic_top_k: int = Field(default=10, ge=1, le=100)
    rrf_k: int = Field(default=60, ge=1)
    # Main result count is selected per request (top_k = 3 or 5).
    max_related_results: int = Field(default=3, ge=0, le=10)
    searchable_statuses: str = "resolved"
    # Enough windows to cover the largest input the character limits allow.
    max_semantic_windows: int = Field(default=24, ge=1, le=64)
    torch_num_threads: int = Field(default=1, ge=1, le=64)

    # --- input limits
    subject_max_chars: int = 200
    description_max_chars: int = 4000
    product_max_chars: int = 80
    error_detail_max_chars: int = 300
    max_body_bytes: int = 65_536
    min_meaningful_terms: int = 2

    # --- resolution form limits
    category_max_chars: int = 40
    investigation_max_chars: int = 3000
    root_cause_max_chars: int = 1500
    resolution_steps_max_chars: int = 3000
    verification_max_chars: int = 1500
    resolved_by_max_chars: int = 120
    max_tags: int = 10
    tag_max_chars: int = 40

    # --- protection of expensive work
    max_concurrent_searches: int = Field(default=4, ge=1, le=64)
    search_queue_timeout_seconds: float = Field(default=5.0, gt=0)

    # --- optional, explicit-click summaries (search never calls the provider)
    openai_api_key: SecretStr | None = None
    summary_model: str = "gpt-4o-mini"
    summary_timeout_seconds: float = Field(default=35.0, gt=0, le=50)
    summary_max_input_chars: int = Field(default=30_000, ge=1000, le=100_000)
    max_concurrent_summaries: int = Field(default=2, ge=1, le=8)
    # Set the same random secret on all replicas. Without one, tokens expire on restart.
    summary_signing_key: SecretStr | None = None
    summary_token_ttl_seconds: int = Field(default=900, ge=30, le=3600)

    @property
    def searchable_status_set(self) -> frozenset[str]:
        return frozenset(s.strip().lower() for s in self.searchable_statuses.split(",") if s.strip())


@lru_cache
def get_settings() -> Settings:
    return Settings()
