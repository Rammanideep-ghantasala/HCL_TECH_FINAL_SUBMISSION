from __future__ import annotations

import json
import logging
import threading

import pytest
from fastapi.testclient import TestClient

from app import schemas
from app.engine import SearchUnavailable
from app.logging_utils import JsonFormatter
from app.main import create_app

from .conftest import FakeEmbedder, build_engine, make_settings, write_tickets

CANARY = "ZZCANARY-7f3a-private-ticket-text"


def _client(tmp_path, tickets_path, **settings_overrides):
    settings = make_settings(tmp_path, tickets_path, **settings_overrides)
    engine = build_engine(settings)
    client = TestClient(create_app(settings, engine), raise_server_exceptions=False)
    client.__enter__()
    return client, engine


@pytest.fixture
def client(tmp_path, sample_tickets_path):
    test_client, _ = _client(tmp_path, sample_tickets_path)
    yield test_client
    test_client.__exit__(None, None, None)


@pytest.fixture
def log_lines():
    """Everything the application would write to its log, as formatted JSON lines."""
    lines: list[str] = []

    class Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            # Only the service's own loggers; the test HTTP client logs URLs itself.
            if not record.name.startswith(("app", "uvicorn", "fastapi", "starlette")):
                return
            lines.append(JsonFormatter().format(record))
            lines.append("RAW " + repr(record.__dict__))

    handler = Collect(level=logging.DEBUG)
    root = logging.getLogger()
    root.addHandler(handler)
    previous = root.level
    root.setLevel(logging.DEBUG)
    yield lines
    root.removeHandler(handler)
    root.setLevel(previous)


# ------------------------------------------------------------ health and meta
def test_health_is_alive(client):
    response = client.get("/health")
    assert response.status_code == 200 and response.json() == {"status": "alive"}


def test_ready_reports_the_usable_mode(client):
    body = client.get("/ready").json()
    assert body["status"] == "ready" and body["mode"] == "hybrid"
    assert body["checks"] == {"ticket_store": True, "tickets": True, "keyword": True, "semantic": True}
    schemas.ReadyResponse.model_validate(body)


def test_ready_is_503_when_nothing_can_search_but_health_stays_200(tmp_path):
    test_client, _ = _client(tmp_path, write_tickets(tmp_path / "empty.json", []))
    try:
        assert test_client.get("/health").status_code == 200
        ready = test_client.get("/ready")
        assert ready.status_code == 503
        assert ready.json()["status"] == "not_ready" and ready.json()["mode"] == "unavailable"
        search = test_client.post("/api/search", json={"subject": "Invoice total is wrong"})
        assert search.status_code == 503
        assert search.json()["error"]["code"] == "search_unavailable"
    finally:
        test_client.__exit__(None, None, None)


def test_ready_stays_200_in_keyword_only_mode(tmp_path, sample_tickets_path):
    settings = make_settings(tmp_path, sample_tickets_path)
    engine = build_engine(settings, broken_model=True)
    with TestClient(create_app(settings, engine)) as test_client:
        body = test_client.get("/ready")
        assert body.status_code == 200
        assert body.json()["mode"] == "keyword_only" and body.json()["degraded"] is True


def test_meta_gives_limits_categories_and_versions(client):
    body = client.get("/api/meta").json()
    schemas.MetaResponse.model_validate(body)
    assert body["corpus_size"] == 5 and body["rejected_records"] == 0
    assert "billing" in body["categories"]
    assert body["limits"]["subject_max_chars"] == 200
    assert "synthetic" in body["data_notice"].lower()
    assert body["retrieval"]["index_version"]


def test_examples_come_from_the_supplied_file(client):
    body = client.get("/api/examples").json()
    assert len(body["examples"]) == 4
    assert set(body["examples"][0]) == {"ticket_id", "subject", "description"}


# --------------------------------------------------------------------- search
def test_search_returns_the_documented_shape(client):
    response = client.post(
        "/api/search",
        json={"subject": "Invoice shows the old price", "description": "Plan was downgraded last week."},
    )
    assert response.status_code == 200
    body = response.json()
    schemas.SearchResponse.model_validate(body)
    assert body["status"] == "useful_matches"
    assert body["results"][0]["ticket"]["ticket_id"] == "A-1"
    assert body["request_id"] == response.headers["x-request-id"]
    assert body["retrieval"]["index_version"] and body["retrieval"]["relevance_config_version"]


def test_no_match_is_a_normal_answer_and_differs_from_a_service_error(client, monkeypatch):
    no_match = client.post(
        "/api/search", json={"subject": "Zorblax plasma conduit", "description": "Vented xenon overnight."}
    )
    assert no_match.status_code == 200
    assert no_match.json()["status"] == "no_strong_match"
    assert no_match.json()["results"] == []

    def unavailable(_):
        raise SearchUnavailable("simulated")

    monkeypatch.setattr(client.app.state.engine, "search", unavailable)
    failed = client.post("/api/search", json={"subject": "Zorblax plasma conduit"})
    assert failed.status_code == 503
    assert failed.json()["error"]["code"] == "search_unavailable"
    assert "results" not in failed.json()


def test_unexpected_failure_is_a_structured_500(client, monkeypatch):
    def boom(_):
        raise RuntimeError("something internal")

    monkeypatch.setattr(client.app.state.engine, "search", boom)
    response = client.post("/api/search", json={"subject": "Invoice total is wrong"})
    assert response.status_code == 500
    body = response.json()["error"]
    assert body["code"] == "internal_error" and body["request_id"]
    assert "something internal" not in response.text


def test_empty_input_is_rejected_with_a_field_message(client):
    for payload in ({}, {"subject": "   ", "description": "\n"}):
        response = client.post("/api/search", json=payload)
        assert response.status_code == 422
        error = response.json()["error"]
        assert error["code"] == "empty_input"
        assert error["details"][0]["field"] == "subject"


def test_vague_input_is_a_200_with_status_insufficient_input(client):
    body = client.post("/api/search", json={"subject": "help not working"}).json()
    assert body["status"] == "insufficient_input"
    assert body["results"] == [] and body["query_insights"]["suggestions"]


def test_over_limit_field_names_the_field_and_the_limit(client):
    response = client.post("/api/search", json={"subject": "x" * 201, "description": "Invoice is wrong"})
    assert response.status_code == 422
    detail = response.json()["error"]["details"][0]
    assert detail["field"] == "subject" and "200" in detail["message"]


def test_oversized_body_is_rejected_before_parsing(client):
    response = client.post("/api/search", json={"subject": "Invoice", "description": "y" * 70_000})
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "request_too_large"


def test_oversized_body_without_content_length_is_rejected(client):
    def chunks():
        yield b'{"subject": "Invoice", "description": "'
        for _ in range(70):
            yield b"z" * 1024
        yield b'"}'

    response = client.post("/api/search", content=chunks(), headers={"content-type": "application/json"})
    assert response.status_code == 413


def test_unknown_field_and_bad_json_are_structured_errors_that_do_not_echo_input(client):
    response = client.post("/api/search", json={"subject": "Invoice wrong", "top_k": 50, "note": CANARY})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert CANARY not in response.text
    bad = client.post("/api/search", content=b"{not json " + CANARY.encode(), headers={"content-type": "application/json"})
    assert bad.status_code == 422
    assert CANARY not in bad.text
    wrong_type = client.post("/api/search", json={"subject": ["a list", CANARY]})
    assert wrong_type.status_code == 422 and CANARY not in wrong_type.text


def test_unknown_category_is_rejected(client):
    response = client.post("/api/search", json={"subject": "Invoice total wrong", "category": "made-up"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "unknown_category"


def test_busy_when_all_search_slots_are_taken(tmp_path, sample_tickets_path):
    settings = make_settings(
        tmp_path, sample_tickets_path, max_concurrent_searches=1, search_queue_timeout_seconds=0.05
    )
    engine = build_engine(settings)
    entered, release = threading.Event(), threading.Event()
    original = engine.search

    def slow(query):
        entered.set()
        release.wait(timeout=5)
        return original(query)

    engine.search = slow
    with TestClient(create_app(settings, engine)) as test_client:
        first: dict = {}
        worker = threading.Thread(
            target=lambda: first.update(r=test_client.post("/api/search", json={"subject": "Invoice total is wrong"}))
        )
        worker.start()
        assert entered.wait(timeout=5)
        second = test_client.post("/api/search", json={"subject": "Okta login loops"})
        release.set()
        worker.join(timeout=5)
    assert second.status_code == 503
    assert second.json()["error"]["code"] == "busy"
    assert second.headers["retry-after"] == "2"
    assert first["r"].status_code == 200


def test_degraded_search_is_flagged(tmp_path, sample_tickets_path):
    settings = make_settings(tmp_path, sample_tickets_path)
    embedder = FakeEmbedder()
    engine = build_engine(settings, embedder=embedder)
    with TestClient(create_app(settings, engine)) as test_client:
        embedder.fail_on_encode = True
        body = test_client.post("/api/search", json={"subject": "Invoice shows the old price"}).json()
    assert body["retrieval"]["mode"] == "keyword_only" and body["retrieval"]["degraded"] is True
    assert body["status"] in ("useful_matches", "no_strong_match")


# --------------------------------------------------------------- ticket detail
def test_ticket_detail_returns_the_stored_record(client):
    body = client.get("/api/tickets/A-2").json()
    schemas.Ticket.model_validate(body)
    assert body["ticket_id"] == "A-2" and "Okta group mapping" in body["resolution_notes"]


@pytest.mark.parametrize("ticket_id", ["NOPE-1", "..%2F..%2Fetc%2Fpasswd", "a" * 100])
def test_unknown_or_malformed_ticket_id_is_a_json_404(client, ticket_id):
    response = client.get(f"/api/tickets/{ticket_id}")
    assert response.status_code == 404
    assert response.json()["error"]["code"] in ("ticket_not_found", "not_found")


def test_unknown_api_path_is_json_not_html(client):
    response = client.get("/api/does-not-exist")
    assert response.status_code == 404 and response.json()["error"]["code"] == "not_found"
    assert client.get("/api/search").status_code in (404, 405)


# ------------------------------------------------------------ security headers
def test_security_headers_and_no_store(client):
    response = client.post("/api/search", json={"subject": "Invoice total is wrong"})
    headers = response.headers
    assert headers["x-content-type-options"] == "nosniff"
    assert "default-src 'self'" in headers["content-security-policy"]
    assert "frame-ancestors 'none'" in headers["content-security-policy"]
    assert headers["cache-control"] == "no-store"
    assert headers["x-request-id"]


def test_api_docs_are_off_by_default(client):
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404


def test_data_directories_are_not_served(tmp_path, sample_tickets_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>app</title>", encoding="utf-8")
    settings = make_settings(tmp_path, sample_tickets_path, frontend_dist=dist)
    with TestClient(create_app(settings, build_engine(settings))) as test_client:
        assert test_client.get("/").status_code == 200
        assert test_client.get("/api/meta").status_code == 200, "static files must not shadow the API"
        for path in ("/tickets.json", "/starter_kit/data/tickets.json", "/../tickets.json", "/data/tickets.json"):
            assert test_client.get(path).status_code == 404


# ------------------------------------------------------------ privacy of logs
def test_ticket_text_never_reaches_the_logs(client, log_lines, monkeypatch):
    # success path
    client.post("/api/search", json={"subject": f"Invoice wrong {CANARY}", "description": f"Old price {CANARY}"})
    # refinement fields and a no-match
    client.post("/api/search", json={"subject": CANARY, "description": CANARY, "product": CANARY, "error_detail": CANARY})
    # validation paths
    client.post("/api/search", json={"subject": CANARY * 20, "description": "x"})
    client.post("/api/search", json={"subject": "Invoice", "unexpected": CANARY})
    client.post("/api/search", content=b"{broken " + CANARY.encode(), headers={"content-type": "application/json"})
    client.post("/api/search", json={"subject": "Invoice total", "category": CANARY})
    client.get(f"/api/tickets/{CANARY}")
    # a submitted ticket id, valid and invalid
    client.post("/api/search", json={"subject": "Invoice total is wrong", "ticket_id": CANARY})
    client.post("/api/search", json={"subject": "Invoice total is wrong", "ticket_id": f"{CANARY} !"})

    # forced failure path
    def boom(_):
        raise RuntimeError(f"failure mentioning {CANARY}")

    monkeypatch.setattr(client.app.state.engine, "search", boom)
    client.post("/api/search", json={"subject": f"Invoice wrong {CANARY}"})

    assert log_lines, "expected the application to log something"
    events = [json.loads(line)["event"] for line in log_lines if line.startswith("{")]
    assert "search" in events and "validation_rejected" in events and "unhandled_error" in events
    leaked = [line for line in log_lines if CANARY in line or CANARY.lower() in line.lower()]
    assert leaked == []


def test_search_log_line_carries_operational_fields(client, log_lines):
    client.post("/api/search", json={"subject": "Invoice shows the old price", "description": "Downgraded last week."})
    search = next(json.loads(line) for line in log_lines if line.startswith("{") and json.loads(line)["event"] == "search")
    for key in ("request_id", "status", "mode", "degraded", "useful", "index_version", "relevance_config_version", "timing_ms"):
        assert key in search
    assert search["useful"][0]["ticket_id"] == "A-1"
