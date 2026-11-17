"""FastAPI application: API routes, limits, structured errors, and the built frontend."""

from __future__ import annotations

import logging
import re
import threading
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from . import __version__, schemas
from .cards import ticket_payload
from .config import Settings, get_settings
from .engine import SearchEngine, SearchInput, SearchUnavailable, UnknownCategory
from .logging_utils import configure_logging, new_request_id, request_id_var
from .repository import RepositoryError, SaveInput, SaveRefused, TicketRepository, create_repository
from .service import RefreshPoller, TicketService
from .summaries import DATA_NOTICE as SUMMARY_DATA_NOTICE, SummaryError, SummaryService
from .text import exact_text
from .tickets import is_valid_ticket_id, load_examples

_STORE_PROBLEMS = frozenset(
    {"database_unavailable", "database_schema_missing", "ticket_store_unavailable", "ticket_file_unusable"}
)
_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,39}$")


class UnavailableRepository(TicketRepository):
    """Stands in when the configured store cannot even be created (for example, no DATABASE_URL)."""

    def __init__(self, kind: str, error: RepositoryError) -> None:
        self.kind = kind
        self.source_id = f"{kind}-unavailable"
        self._error = error

    @property
    def writable(self) -> bool:
        return False

    @property
    def error(self) -> str | None:
        return str(self._error)

    def _raise(self, *_args, **_kwargs):
        raise self._error

    load_corpus = change_marker = get = save = mark_indexed = mark_failed = unindexed = _raise


def _identity(request: Request) -> str | None:
    """The authenticated user from a trusted proxy header, when one is configured."""
    header = request.app.state.settings.identity_header
    if not header:
        return None
    value = exact_text(request.headers.get(header, ""))
    return value[:120] or None


def _save_limits(settings: Settings) -> dict:
    return {
        "subject_max_chars": settings.subject_max_chars,
        "description_max_chars": settings.description_max_chars,
        "category_max_chars": settings.category_max_chars,
        "product_max_chars": settings.product_max_chars,
        "error_detail_max_chars": settings.error_detail_max_chars,
        "investigation_max_chars": settings.investigation_max_chars,
        "root_cause_max_chars": settings.root_cause_max_chars,
        "resolution_steps_max_chars": settings.resolution_steps_max_chars,
        "verification_max_chars": settings.verification_max_chars,
        "resolved_by_max_chars": settings.resolved_by_max_chars,
        "max_tags": settings.max_tags,
        "tag_max_chars": settings.tag_max_chars,
    }


def _validate_save(payload, settings: Settings, identity: str | None) -> list[dict]:
    """Field-by-field checks with messages that say what to correct. Never echoes the input."""
    details: list[dict] = []

    def add(field: str, message: str) -> None:
        details.append({"field": field, "message": message})

    required = {
        "subject": ("Subject", settings.subject_max_chars),
        "description": ("Description", settings.description_max_chars),
        "category": ("Category", settings.category_max_chars),
        "investigation": ("Investigation approach", settings.investigation_max_chars),
        "resolution_steps": ("Resolution steps", settings.resolution_steps_max_chars),
        "verification": ("Verification", settings.verification_max_chars),
    }
    if identity is None:
        required["resolved_by"] = ("Resolved by", settings.resolved_by_max_chars)
    for name, (label, limit) in required.items():
        value = exact_text(getattr(payload, name) or "")
        if not value:
            add(name, f"{label} is required.")
        elif len(value) > limit:
            add(name, f"{label} is {len(value)} characters; the limit is {limit}.")
    optional = {
        "product": ("Product or version", settings.product_max_chars),
        "error_detail": ("Error details", settings.error_detail_max_chars),
        "root_cause": ("Root cause", settings.root_cause_max_chars),
    }
    for name, (label, limit) in optional.items():
        value = exact_text(getattr(payload, name) or "")
        if len(value) > limit:
            add(name, f"{label} is {len(value)} characters; the limit is {limit}.")
    category = exact_text(payload.category or "")
    if category and len(category) <= settings.category_max_chars and not _LABEL_RE.match(category):
        add("category", "Use letters, digits, spaces, '.', '_' or '-', starting with a letter or digit.")
    ticket_id = exact_text(payload.ticket_id or "")
    if ticket_id and not is_valid_ticket_id(ticket_id):
        add("ticket_id", "Use letters, digits, '.', '_' or '-', starting with a letter or digit, at most 64 characters.")
    tags = [exact_text(tag) for tag in payload.tags if exact_text(tag)]
    if len(tags) > settings.max_tags:
        add("tags", f"Use at most {settings.max_tags} tags.")
    elif any(len(tag) > settings.tag_max_chars or not _LABEL_RE.match(tag) for tag in tags):
        add("tags", f"Each tag must be at most {settings.tag_max_chars} characters of letters, digits, spaces, '.', '_' or '-'.")
    if payload.confirm_resolved is not True:
        add("confirm_resolved", "Confirm that the issue has been resolved before saving.")
    return details

logger = logging.getLogger("app.api")

DATA_NOTICE = (
    "Demo on synthetic tickets. There is no sign-in and no access control, "
    "so do not paste real customer data."
)
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
)
_TICKET_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")

_VALIDATION_MESSAGES = {
    "missing": "This field is required.",
    "string_type": "This field must be text.",
    "string_too_long": "This field is too long.",
    "extra_forbidden": "This field is not accepted.",
    "json_invalid": "The request body is not valid JSON.",
    "model_attributes_type": "The request body must be a JSON object.",
    "dict_type": "The request body must be a JSON object.",
}


class ApiException(Exception):
    def __init__(self, status_code: int, code: str, message: str, details: list[dict] | None = None,
                 headers: dict[str, str] | None = None) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details or []
        self.headers = headers or {}


def error_response(status_code: int, code: str, message: str, details: list[dict] | None = None,
                   headers: dict[str, str] | None = None) -> JSONResponse:
    body = {
        "error": {
            "code": code,
            "message": message,
            "request_id": request_id_var.get(),
            "details": details or [],
        }
    }
    return JSONResponse(status_code=status_code, content=body, headers=headers)


class RequestContextMiddleware:
    """Request id, body-size cap, security headers and one access-log line."""

    def __init__(self, app: ASGIApp, max_body_bytes: int) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = new_request_id()
        token = request_id_var.set(request_id)
        started = time.perf_counter()
        status_holder = {"status": 0}
        path = scope.get("path", "")
        is_api = path.startswith("/api/") or path in ("/health", "/ready")

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
                headers = list(message.get("headers", []))
                headers.extend(
                    [
                        (b"x-request-id", request_id.encode()),
                        (b"x-content-type-options", b"nosniff"),
                        (b"referrer-policy", b"no-referrer"),
                        (b"x-frame-options", b"DENY"),
                        (b"content-security-policy", CONTENT_SECURITY_POLICY.encode()),
                    ]
                )
                if is_api:
                    headers.append((b"cache-control", b"no-store"))
                message = {**message, "headers": headers}
            await send(message)

        try:
            body_receive = receive
            if scope.get("method") in ("POST", "PUT", "PATCH"):
                too_large = False
                declared = dict(scope.get("headers", [])).get(b"content-length")
                if declared is not None and declared.isdigit() and int(declared) > self.max_body_bytes:
                    too_large = True
                chunks: list[bytes] = []
                if not too_large:
                    received = 0
                    while True:
                        message = await receive()
                        if message["type"] != "http.request":
                            break
                        chunk = message.get("body", b"")
                        received += len(chunk)
                        if received > self.max_body_bytes:
                            too_large = True
                            break
                        chunks.append(chunk)
                        if not message.get("more_body", False):
                            break
                if too_large:
                    response = error_response(
                        413,
                        "request_too_large",
                        f"The request is larger than {self.max_body_bytes} bytes. Shorten the ticket text.",
                    )
                    await response(scope, receive, send_wrapper)
                    return
                body = b"".join(chunks)
                delivered = False

                async def replay() -> Message:
                    nonlocal delivered
                    if not delivered:
                        delivered = True
                        return {"type": "http.request", "body": body, "more_body": False}
                    return await receive()

                body_receive = replay
            try:
                await self.app(scope, body_receive, send_wrapper)
            except Exception as exc:
                # Handled here, inside the request context, so the reply still
                # carries the request id and the security headers. Only the
                # exception type is logged: its message could echo ticket text.
                logger.error("unhandled_error", extra={"fields": {"exception_type": type(exc).__name__}})
                if status_holder["status"]:
                    raise
                response = error_response(
                    500, "internal_error", "The search service hit an unexpected error. Try again."
                )
                await response(scope, receive, send_wrapper)
        finally:
            if is_api:
                logger.info(
                    "request",
                    extra={
                        "fields": {
                            "method": scope.get("method"),
                            "path": _loggable_path(path),
                            "status_code": status_holder["status"],
                            "duration_ms": round((time.perf_counter() - started) * 1000.0, 2),
                        }
                    },
                )
            request_id_var.reset(token)


def create_app(
    settings: Settings | None = None,
    engine: SearchEngine | None = None,
    repository: TicketRepository | None = None,
    embedder_factory=None,
    summary_provider=None,
) -> FastAPI:
    settings = settings or get_settings()
    search_slots = threading.BoundedSemaphore(settings.max_concurrent_searches)
    summaries = SummaryService(settings, summary_provider)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        configure_logging(settings.log_level)
        started = time.perf_counter()
        repo = repository
        if repo is None:
            try:
                repo = create_repository(settings)
            except RepositoryError as exc:
                repo = UnavailableRepository(settings.ticket_store, exc)
        app.state.repository = repo
        app.state.engine = engine or SearchEngine.build(settings, embedder_factory=embedder_factory, repository=repo)
        app.state.examples = load_examples(settings.examples_path)
        app.state.service = TicketService(
            settings,
            repo,
            get_engine=lambda: app.state.engine,
            set_engine=lambda new: setattr(app.state, "engine", new),
            embedder_factory=embedder_factory,
        )
        # Tickets saved before a crash or restart are indexed by the build above; record that.
        app.state.service.recover_after_start()
        poller = None
        if settings.index_refresh_interval_seconds > 0:
            poller = RefreshPoller(app.state.service, settings.index_refresh_interval_seconds)
            poller.start()
        eng: SearchEngine = app.state.engine
        report = eng.corpus.report if eng.corpus else None
        logger.info(
            "startup_complete",
            extra={
                "fields": {
                    "ready": eng.ready,
                    "mode": eng.mode,
                    "degraded_reason": eng.degraded_reason,
                    "problems": eng.problems,
                    "tickets_accepted": report.accepted if report else 0,
                    "tickets_rejected": dict(report.rejected) if report else {},
                    "ticket_warnings": dict(report.warnings) if report else {},
                    "conflicting_ticket_ids": report.conflicting_ids if report else [],
                    "embeddings_source": eng.semantic.source if eng.semantic else None,
                    "embeddings_computed": eng.semantic.computed_rows if eng.semantic else None,
                    "index_version": eng.index_version,
                    "ticket_store": settings.ticket_store,
                    "startup_ms": round((time.perf_counter() - started) * 1000.0, 1),
                }
            },
        )
        yield
        if poller is not None:
            poller.stop()
        if repository is None:
            repo.close()

    docs = {} if settings.enable_api_docs else {"docs_url": None, "redoc_url": None, "openapi_url": None}
    app = FastAPI(title="Support Ticket Similarity Assistant", version=__version__, lifespan=lifespan, **docs)
    app.state.settings = settings
    app.add_middleware(RequestContextMiddleware, max_body_bytes=settings.max_body_bytes)

    # ------------------------------------------------------------ error handling
    @app.exception_handler(ApiException)
    async def handle_api_exception(_: Request, exc: ApiException) -> JSONResponse:
        return error_response(exc.status_code, exc.code, exc.message, exc.details, exc.headers)

    @app.exception_handler(RequestValidationError)
    async def handle_validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        # Pydantic's own error objects include the rejected input. Only the field
        # name and a fixed message are returned or logged.
        details = []
        for error in exc.errors():
            location = [str(part) for part in error.get("loc", ()) if part != "body"]
            details.append(
                {
                    "field": ".".join(location) or None,
                    "message": _VALIDATION_MESSAGES.get(str(error.get("type")), "This value is not valid."),
                }
            )
        logger.info(
            "validation_rejected",
            extra={"fields": {"error_types": sorted({str(e.get("type")) for e in exc.errors()})}},
        )
        return error_response(422, "validation_error", "The request is not valid. Check the fields listed.", details)

    @app.exception_handler(StarletteHTTPException)
    async def handle_http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        codes = {404: ("not_found", "Not found."), 405: ("method_not_allowed", "Method not allowed.")}
        code, message = codes.get(exc.status_code, ("http_error", "The request could not be handled."))
        return error_response(exc.status_code, code, message)

    @app.exception_handler(Exception)
    async def handle_unexpected(_: Request, exc: Exception) -> JSONResponse:
        logger.error("unhandled_error", extra={"fields": {"exception_type": type(exc).__name__}})
        return error_response(500, "internal_error", "The search service hit an unexpected error. Try again.")

    # -------------------------------------------------------------------- routes
    @app.get("/health", response_model=schemas.HealthResponse)
    def health() -> dict:
        """Liveness: the process is up. Says nothing about search."""
        return {"status": "alive"}

    @app.get("/ready", response_model=schemas.ReadyResponse)
    def ready(request: Request) -> JSONResponse:
        """Readiness: 200 only when at least one search mode can answer."""
        eng: SearchEngine = request.app.state.engine
        body = {
            "status": "ready" if eng.ready else "not_ready",
            "mode": eng.mode,
            "degraded": eng.degraded,
            "corpus_size": len(eng.corpus.tickets) if eng.corpus else 0,
            "index_version": eng.index_version,
            "checks": {
                "ticket_store": not any(problem in _STORE_PROBLEMS for problem in eng.problems),
                "tickets": bool(eng.corpus and eng.corpus.tickets),
                "keyword": eng.keyword is not None,
                "semantic": eng.semantic_active,
            },
        }
        return JSONResponse(status_code=200 if eng.ready else 503, content=body)

    @app.get("/api/meta", response_model=schemas.MetaResponse)
    def meta(request: Request) -> dict:
        eng: SearchEngine = request.app.state.engine
        return {
            "app_version": __version__,
            "data_notice": DATA_NOTICE,
            "corpus_size": len(eng.corpus.tickets) if eng.corpus else 0,
            "rejected_records": eng.corpus.report.rejected_total if eng.corpus else 0,
            "categories": eng.categories(),
            "limits": {
                "subject_max_chars": settings.subject_max_chars,
                "description_max_chars": settings.description_max_chars,
                "product_max_chars": settings.product_max_chars,
                "error_detail_max_chars": settings.error_detail_max_chars,
                "max_body_bytes": settings.max_body_bytes,
                "min_meaningful_terms": settings.min_meaningful_terms,
                "max_useful_results": 5,
                "max_related_results": settings.max_related_results,
            },
            "retrieval": {
                "mode": eng.mode,
                "degraded": eng.degraded,
                "degraded_reason": eng.degraded_reason,
                "index_version": eng.index_version,
                "model_id": eng.model_id,
                "model_revision": eng.model_revision,
                "relevance_config_version": eng.relevance.version if eng.relevance else "unavailable",
            },
            "ticket_store": settings.ticket_store,
            "save_enabled": request.app.state.service.save_enabled,
            "save_limits": _save_limits(settings),
            "escalation_email": settings.escalation_email,
            "identity": _identity(request),
            "unindexed_ticket_ids": request.app.state.service.unindexed_ticket_ids(),
            "summarization": {
                "configured": summaries.configured,
                "provider": "openai",
                "model": settings.summary_model,
                "data_notice": SUMMARY_DATA_NOTICE,
            },
        }

    @app.get("/api/examples", response_model=schemas.ExamplesResponse)
    def examples(request: Request) -> dict:
        return {
            "examples": [
                {"ticket_id": e.ticket_id, "subject": e.subject, "description": e.description}
                for e in request.app.state.examples
            ]
        }

    @app.get("/api/tickets/{ticket_id}", response_model=schemas.StoredTicket)
    def get_ticket(ticket_id: str, request: Request) -> dict:
        found = None
        if _TICKET_ID_RE.match(ticket_id):
            try:
                found = request.app.state.service.get(ticket_id)
            except RepositoryError as exc:
                raise ApiException(503, exc.code, "The ticket store is unavailable right now.") from None
        if found is None:
            raise ApiException(404, "ticket_not_found", "No historical ticket has that id.")
        record, index_status, indexed = found
        return {**ticket_payload(record), "revision": record.revision, "indexed": indexed, "index_status": index_status}

    @app.post("/api/tickets", response_model=schemas.SaveTicketResponse)
    def save_ticket(payload: schemas.SaveTicketRequest, request: Request) -> JSONResponse:
        """Explicitly save a resolved ticket. The only route that writes tickets."""
        service: TicketService = request.app.state.service
        identity = _identity(request)
        details = _validate_save(payload, settings, identity)
        if details:
            raise ApiException(422, "validation_error", "Some fields need attention. Nothing was saved.", details)
        started = time.perf_counter()
        try:
            result = service.save(
                SaveInput(
                    submission_id=payload.submission_id,
                    ticket_id=exact_text(payload.ticket_id or "") or None,
                    expected_revision=payload.expected_revision,
                    subject=payload.subject,
                    description=payload.description,
                    category=payload.category,
                    product=payload.product,
                    error_detail=payload.error_detail,
                    investigation=payload.investigation,
                    root_cause=payload.root_cause,
                    resolution_steps=payload.resolution_steps,
                    verification=payload.verification,
                    resolved_by=identity or payload.resolved_by,
                    tags=[exact_text(tag) for tag in payload.tags if exact_text(tag)],
                )
            )
        except SaveRefused as exc:
            details = [{"field": exc.field, "message": exc.message}] if exc.field else []
            logger.info("save_refused", extra={"fields": {"code": exc.code}})
            raise ApiException(exc.status_code, exc.code, exc.message, details) from None
        record = result["record"]
        logger.info(
            "ticket_saved",
            extra={
                "fields": {
                    "ticket_id": record.ticket_id,
                    "outcome": result["outcome"],
                    "repeat_submission": result["repeat_submission"],
                    "revision": record.revision,
                    "indexing": result["indexing"]["status"],
                    "duration_ms": round((time.perf_counter() - started) * 1000.0, 1),
                }
            },
        )
        body = {
            "outcome": result["outcome"],
            "repeat_submission": result["repeat_submission"],
            "ticket": {
                **ticket_payload(record),
                "revision": record.revision,
                "indexed": result["indexed"],
                "index_status": result["index_status"],
            },
            "indexing": result["indexing"],
            "message": result["message"],
        }
        return JSONResponse(status_code=201 if result["outcome"] == "created" else 200, content=body)

    @app.post("/api/index/refresh", response_model=schemas.RefreshIndexResponse)
    def refresh_index(request: Request, full: bool = False) -> dict:
        """Retry indexing, and pick up external changes to the ticket store (edits, deletions, reopened tickets)."""
        service: TicketService = request.app.state.service
        indexing = service.refresh(full=full)
        logger.info("index_refresh", extra={"fields": {"full": full, "status": indexing["status"]}})
        return {"indexing": indexing, "unindexed_ticket_ids": service.unindexed_ticket_ids()}

    @app.post("/api/search", response_model=schemas.SearchResponse)
    def search(payload: schemas.SearchRequest, request: Request) -> dict:
        eng: SearchEngine = request.app.state.engine
        fields = {
            "subject": (payload.subject or "", settings.subject_max_chars, "Subject"),
            "description": (payload.description or "", settings.description_max_chars, "Description"),
            "product": (payload.product or "", settings.product_max_chars, "Product or system"),
            "error_detail": (payload.error_detail or "", settings.error_detail_max_chars, "Error code or message"),
        }
        too_long = [
            {"field": name, "message": f"{label} is {len(value)} characters; the limit is {limit}. Shorten it."}
            for name, (value, limit, label) in fields.items()
            if len(value) > limit
        ]
        if too_long:
            raise ApiException(422, "validation_error", "Some fields are too long.", too_long)
        ticket_id = exact_text(payload.ticket_id or "") or None
        if ticket_id is not None and not is_valid_ticket_id(ticket_id):
            raise ApiException(
                422,
                "validation_error",
                "The ticket ID is not valid.",
                [
                    {
                        "field": "ticket_id",
                        "message": (
                            "Use letters, digits, '.', '_' or '-', starting with a letter or digit, "
                            "at most 64 characters."
                        ),
                    }
                ],
            )
        if not fields["subject"][0].strip() and not fields["description"][0].strip():
            raise ApiException(
                422,
                "empty_input",
                "Enter a subject or a description before searching.",
                [{"field": "subject", "message": "Enter a subject or a description."}],
            )
        category = (payload.category or "").strip() or None

        if not search_slots.acquire(timeout=settings.search_queue_timeout_seconds):
            raise ApiException(
                503, "busy", "The search service is busy. Try again in a moment.", headers={"Retry-After": "2"}
            )
        try:
            outcome = eng.search(
                SearchInput(
                    subject=fields["subject"][0],
                    description=fields["description"][0],
                    product=fields["product"][0],
                    error_detail=fields["error_detail"][0],
                    category=category,
                    ticket_id=ticket_id,
                    top_k=payload.top_k,
                )
            )
        except UnknownCategory:
            raise ApiException(
                422,
                "unknown_category",
                "That category does not exist. Choose one from the list or clear the filter.",
                [{"field": "category", "message": "Unknown category."}],
            ) from None
        except SearchUnavailable:
            store_problem = next((p for p in eng.problems if p in _STORE_PROBLEMS), None)
            if store_problem:
                raise ApiException(
                    503,
                    store_problem,
                    "Search is unavailable because the ticket store could not be read. "
                    "This is a service problem, not a result.",
                ) from None
            raise ApiException(
                503, "search_unavailable", "Search is unavailable right now. This is a service problem, not a result."
            ) from None
        finally:
            search_slots.release()

        retrieval = outcome["retrieval"]
        logger.info(
            "search",
            extra={
                "fields": {
                    "status": outcome["status"],
                    # The submitted ticket id is not logged, only what the lookup found.
                    "ticket_id_status": outcome["exact_match"]["ticket_id_status"],
                    "exact_match_count": len(outcome["exact_match"]["matched_ticket_ids"]),
                    "similarity_search_run": retrieval["similarity_search_run"],
                    "mode": retrieval["mode"],
                    "degraded": retrieval["degraded"],
                    "degraded_reason": retrieval["degraded_reason"],
                    "keyword_signal": retrieval["keyword_signal"],
                    "category_filter": retrieval["category_filter"],
                    "semantic_windows": retrieval["semantic_windows"],
                    "subject_chars": len(fields["subject"][0]),
                    "description_chars": len(fields["description"][0]),
                    "meaningful_terms": outcome["query_insights"]["meaningful_term_count"],
                    "unseen_term_count": len(outcome["query_insights"]["unseen_terms"]),
                    "useful": [
                        {"ticket_id": card["ticket"]["ticket_id"], **card["scores"]} for card in outcome["results"]
                    ],
                    "related": [
                        {"ticket_id": card["ticket"]["ticket_id"], **card["scores"]} for card in outcome["related"]
                    ],
                    "index_version": retrieval["index_version"],
                    "relevance_config_version": retrieval["relevance_config_version"],
                    "timing_ms": outcome["timing_ms"],
                }
            },
        )
        return {
            "request_id": request_id_var.get() or "", **outcome, "top_k": payload.top_k,
            "summary_token": summaries.snapshot(payload, outcome, eng, _identity(request)),
        }

    @app.post("/api/summaries", response_model=schemas.SummaryResponse)
    def summarize(payload: schemas.SummaryRequest, request: Request) -> dict:
        try:
            return summaries.summarize(payload, request.app.state.service, _identity(request))
        except SummaryError as exc:
            raise ApiException(exc.status, exc.code, exc.message) from None
        except RepositoryError as exc:
            raise ApiException(503, exc.code, "The ticket store is unavailable right now.") from None

    @app.api_route("/api/{rest:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"], include_in_schema=False)
    def api_not_found(rest: str) -> JSONResponse:
        return error_response(404, "not_found", "No such API endpoint.")

    # ------------------------------------------------------------ built frontend
    # Mounted last, so it can never shadow an API route. Only the built frontend
    # directory is served; the data, model and artifact directories are not.
    if settings.frontend_dist.is_dir() and (settings.frontend_dist / "index.html").is_file():
        app.mount("/", StaticFiles(directory=settings.frontend_dist, html=True), name="frontend")

    return app


_FIXED_PATHS = frozenset(
    {"/api/search", "/api/summaries", "/api/meta", "/api/examples", "/api/tickets", "/api/index/refresh", "/health", "/ready"}
)


def _loggable_path(path: str) -> str:
    """The route, not the raw URL: anything a caller typed into the path is left out."""
    if path in _FIXED_PATHS:
        return path
    if path.startswith("/api/tickets/"):
        return "/api/tickets/{ticket_id}"
    return "/api/{other}"


app = create_app()
