"""API contract. Mirrors frontend/src/api/types.ts; change both together."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Absolute ceilings enforced by the schema. The configured limits (usually much
# lower) are checked in the route so the error can name the field and the limit.
_HARD_TEXT_CAP = 20_000


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)

    subject: str = Field(default="", max_length=_HARD_TEXT_CAP)
    description: str = Field(default="", max_length=_HARD_TEXT_CAP)
    product: str | None = Field(default=None, max_length=_HARD_TEXT_CAP)
    error_detail: str | None = Field(default=None, max_length=_HARD_TEXT_CAP)
    category: str | None = Field(default=None, max_length=200)
    # Optional id of the new ticket. Only looked up; a search never saves it.
    ticket_id: str | None = Field(default=None, max_length=200)
    top_k: Literal[3, 5] = 3

    @field_validator("top_k", mode="before")
    @classmethod
    def integer_result_count(cls, value):
        if type(value) is not int or value not in (3, 5):
            raise ValueError("Choose Top 3 or Top 5.")
        return value


class TextSegment(BaseModel):
    text: str
    match: bool


class Ticket(BaseModel):
    ticket_id: str
    subject: str
    description: str
    resolution_notes: str
    category: str | None
    status: str | None
    tags: list[str]
    created_at: str | None
    # Recorded through the resolution form; null on tickets that predate it.
    product: str | None = None
    error_detail: str | None = None
    investigation: str | None = None
    root_cause: str | None = None
    verification: str | None = None
    resolved_by: str | None = None
    resolved_at: str | None = None


class StoredTicket(Ticket):
    revision: str
    # True when this exact revision is in the live search snapshot.
    indexed: bool
    # As recorded in the store: pending, indexed or failed.
    index_status: Literal["pending", "indexed", "failed"] | None = None


_CATEGORY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,39}$"


class SaveTicketRequest(BaseModel):
    """Body of POST /api/tickets. Length limits are checked in the route so errors name the field."""

    model_config = ConfigDict(extra="forbid")

    submission_id: str = Field(pattern=r"^[A-Za-z0-9-]{8,64}$")
    ticket_id: str | None = Field(default=None, max_length=200)
    expected_revision: str | None = Field(default=None, pattern=r"^[0-9a-f]{16}$")
    subject: str = Field(max_length=_HARD_TEXT_CAP)
    description: str = Field(max_length=_HARD_TEXT_CAP)
    category: str = Field(max_length=200)
    product: str | None = Field(default=None, max_length=_HARD_TEXT_CAP)
    error_detail: str | None = Field(default=None, max_length=_HARD_TEXT_CAP)
    investigation: str = Field(max_length=_HARD_TEXT_CAP)
    root_cause: str | None = Field(default=None, max_length=_HARD_TEXT_CAP)
    resolution_steps: str = Field(max_length=_HARD_TEXT_CAP)
    verification: str = Field(max_length=_HARD_TEXT_CAP)
    resolved_by: str = Field(default="", max_length=_HARD_TEXT_CAP)
    tags: list[str] = Field(default_factory=list, max_length=50)
    confirm_resolved: bool


class IndexingStatus(BaseModel):
    status: Literal["indexed", "failed"]
    searchable: bool
    message: str
    index_version: str | None


class SaveTicketResponse(BaseModel):
    outcome: Literal["created", "updated", "unchanged"]
    repeat_submission: bool
    ticket: StoredTicket
    indexing: IndexingStatus
    message: str


class RefreshIndexResponse(BaseModel):
    indexing: IndexingStatus
    unindexed_ticket_ids: list[str]


class SaveLimits(BaseModel):
    subject_max_chars: int
    description_max_chars: int
    category_max_chars: int
    product_max_chars: int
    error_detail_max_chars: int
    investigation_max_chars: int
    root_cause_max_chars: int
    resolution_steps_max_chars: int
    verification_max_chars: int
    resolved_by_max_chars: int
    max_tags: int
    tag_max_chars: int


class ComparisonRow(BaseModel):
    key: Literal["product_or_system", "error_code", "category", "tags", "created_at", "status"]
    label: str
    new_values: list[str]
    past_values: list[str]
    new_basis: str
    past_basis: str
    outcome: Literal["same", "different", "partly_shared", "unknown"]
    statement: str


class Excerpt(BaseModel):
    field: Literal["subject", "description"]
    segments: list[TextSegment]


class Evidence(BaseModel):
    found_by: list[Literal["keyword", "semantic", "exact_text"]]
    keyword_rank: int | None
    semantic_rank: int | None
    shared_terms: list[str]
    new_subject_segments: list[TextSegment]
    new_description_segments: list[TextSegment]
    past_subject_segments: list[TextSegment]
    past_description_segments: list[TextSegment]
    excerpts: list[Excerpt]
    retrieval_note: str


class Differences(BaseModel):
    only_in_new: list[str]
    only_in_past: list[str]
    unseen_in_history: list[str]
    comparison_rows: list[ComparisonRow]


class VerificationQuestion(BaseModel):
    id: str
    question: str
    basis: str


class Scores(BaseModel):
    keyword_cosine: float | None
    semantic_cosine: float | None
    query_coverage: float | None
    rrf: float | None


class ResultCard(BaseModel):
    result_id: str
    rank: int
    match_level: Literal["exact", "useful", "related"]
    match_label: str
    ticket: Ticket
    evidence: Evidence
    differences: Differences
    verification_questions: list[VerificationQuestion]
    referenced_ticket_ids: list[str]
    scores: Scores


class Suggestion(BaseModel):
    id: str
    message: str
    basis: str
    field: Literal["subject", "description", "product", "error_detail", "category"] | None


class QueryInsights(BaseModel):
    meaningful_term_count: int
    detected_names: list[str]
    detected_codes: list[str]
    unseen_terms: list[str]
    suggestions: list[Suggestion]


RetrievalMode = Literal["hybrid", "keyword_only", "semantic_only"]


class ExactMatch(BaseModel):
    """Outcome of the exact-text check that runs before any similarity search."""

    ticket_id: str | None
    ticket_id_status: Literal["not_supplied", "not_found", "exists_same_text", "exists_different_text"]
    # Every past ticket whose subject and description are exactly the submitted text.
    matched_ticket_ids: list[str]
    message: str | None


class RetrievalInfo(BaseModel):
    # False when the answer came from the exact-text check or the input was too vague.
    similarity_search_run: bool
    mode: RetrievalMode
    degraded: bool
    degraded_reason: str | None
    keyword_signal: bool
    category_filter: str | None
    hidden_by_filter: int
    semantic_windows: int
    corpus_size: int
    index_version: str
    model_id: str | None
    model_revision: str | None
    relevance_config_version: str


class Timing(BaseModel):
    total: float
    keyword: float | None
    semantic: float | None


class SearchResponse(BaseModel):
    request_id: str
    query_fingerprint: str
    status: Literal["useful_matches", "no_strong_match", "insufficient_input"]
    message: str
    results: list[ResultCard]
    related: list[ResultCard]
    exact_match: ExactMatch
    query_insights: QueryInsights
    retrieval: RetrievalInfo
    timing_ms: Timing
    top_k: Literal[3, 5] = 3
    # Signed reference to these ordered result records and this query. No LLM runs during search.
    summary_token: str | None = None


class SummarizationInfo(BaseModel):
    configured: bool
    provider: Literal["openai"] = "openai"
    model: str
    data_notice: str


class SummaryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary_token: str = Field(min_length=1, max_length=50_000)
    ticket_ids: list[str] = Field(min_length=1, max_length=5)


class SummaryPoint(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=1200)
    ticket_ids: list[str] = Field(min_length=1, max_length=5)


class TicketSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ticket_id: str
    reported_issue: str = Field(min_length=1, max_length=1200)
    recorded_resolution: str = Field(min_length=1, max_length=2000)
    applicability_notes: list[str] = Field(max_length=3)


class SummaryContent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    overview: list[SummaryPoint] = Field(min_length=1, max_length=3)
    tickets: list[TicketSummary] = Field(min_length=1, max_length=5)


class SummaryResponse(BaseModel):
    ticket_ids: list[str]
    source_revisions: list[str]
    model: str
    prompt_version: str
    generated_at: str
    summary: SummaryContent
    disclaimer: str


class ErrorDetail(BaseModel):
    field: str | None
    message: str


class ErrorBody(BaseModel):
    code: str
    message: str
    request_id: str | None
    details: list[ErrorDetail]


class ApiError(BaseModel):
    error: ErrorBody


class ExampleTicketModel(BaseModel):
    ticket_id: str
    subject: str
    description: str


class ExamplesResponse(BaseModel):
    examples: list[ExampleTicketModel]


class Limits(BaseModel):
    subject_max_chars: int
    description_max_chars: int
    product_max_chars: int
    error_detail_max_chars: int
    max_body_bytes: int
    min_meaningful_terms: int
    max_useful_results: int
    max_related_results: int


class MetaRetrieval(BaseModel):
    mode: Literal["hybrid", "keyword_only", "semantic_only", "unavailable"]
    degraded: bool
    degraded_reason: str | None
    index_version: str | None
    model_id: str | None
    model_revision: str | None
    relevance_config_version: str


class MetaResponse(BaseModel):
    app_version: str
    data_notice: str
    corpus_size: int
    rejected_records: int
    categories: list[str]
    limits: Limits
    retrieval: MetaRetrieval
    ticket_store: Literal["json", "postgresql"]
    save_enabled: bool
    save_limits: SaveLimits
    escalation_email: str
    identity: str | None
    unindexed_ticket_ids: list[str]
    summarization: SummarizationInfo


class HealthResponse(BaseModel):
    status: Literal["alive"]


class ReadyChecks(BaseModel):
    ticket_store: bool
    tickets: bool
    keyword: bool
    semantic: bool


class ReadyResponse(BaseModel):
    status: Literal["ready", "not_ready"]
    mode: Literal["hybrid", "keyword_only", "semantic_only", "unavailable"]
    degraded: bool
    corpus_size: int
    index_version: str | None
    checks: ReadyChecks
