"""Submission evidence only. Every write uses a temporary synthetic ticket store."""
from __future__ import annotations

import hashlib
import json
import logging
import platform
import sys
import tempfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
from fastapi.testclient import TestClient
from app.config import Settings
from app.engine import SearchEngine
from app.main import create_app
from app.retrieval.semantic import query_text
from tests.conftest import FakeEmbedder, make_ticket

OUT = Path(__file__).resolve().parent
logging.disable(logging.CRITICAL)
evidence = {"date": "2026-11-17", "python": platform.python_version(), "platform": platform.platform(), "probes": []}

def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None

def body(**overrides):
    value = dict(submission_id=str(uuid.uuid4()), subject="Zorblax plasma conduit",
                 description="Vented xenon overnight in a lunar cryogenic valve.",
                 category="general", product="Unrelated lunar hardware", error_detail="ZX-999",
                 investigation="Examined the synthetic valve incident.",
                 resolution_steps="Replaced the fictional valve.",
                 verification="Verified the fictional valve fixture.",
                 resolved_by="Synthetic audit actor", confirm_resolved=True)
    value.update(overrides)
    return value

original = ROOT / "starter_kit/data/tickets.json"
original_hash = digest(original)
with tempfile.TemporaryDirectory(prefix="hcl-submission-verification-") as folder:
    base = Path(folder)
    seed = base / "tickets.json"
    seed.write_bytes(original.read_bytes())
    settings = Settings(tickets_path=seed, data_dir=base / "state", frontend_dist=base / "no-frontend",
                        openai_api_key=None, ticket_store="json", index_refresh_interval_seconds=0)
    engine = SearchEngine.build(settings)
    assert engine.mode == "hybrid", engine.problems
    evidence["model"] = {"id": engine.model_id, "revision": engine.model_revision,
                         "dimension": engine.semantic.embedder.dimension,
                         "max_tokens": engine.semantic.embedder.max_tokens,
                         "corpus_size": len(engine.corpus.tickets),
                         "index_version": engine.index_version,
                         "vectors_computed": engine.semantic.computed_rows}
    with TestClient(create_app(settings, engine=engine)) as client:
        target_id = engine.corpus.ids[0]
        current = client.get(f"/api/tickets/{target_id}").json()
        request = body(ticket_id=target_id)
        kw = engine.keyword.query(request["subject"] + "\n" + request["description"])
        sem, _ = engine.semantic.query(query_text(request["subject"], request["description"], "", ""), 24)
        row = engine.corpus.ids.index(target_id)
        seed_before = digest(seed)
        search = client.post("/api/search", json={k:request[k] for k in ("ticket_id", "subject", "description")})
        evidence["probes"].append({"id":"S01", "scenario":"Different text on existing ID during search",
            "expected":"No ticket mutation", "http":search.status_code,
            "id_status":search.json()["exact_match"]["ticket_id_status"],
            "store_created":(settings.data_dir / "saved_tickets.json").exists(),
            "seed_unchanged":digest(seed)==seed_before, "status":"PASS"})
        assert not (settings.data_dir / "saved_tickets.json").exists() and digest(seed)==seed_before
        refused = client.post("/api/tickets", json=request)
        assert refused.status_code == 409
        evidence["probes"].append({"id":"S02", "scenario":"Blind overwrite without current revision",
            "expected":"409 and unchanged ticket", "http":refused.status_code,
            "code":refused.json()["error"]["code"], "status":"PASS"})
        request["expected_revision"] = current["revision"]
        changed = client.post("/api/tickets", json=request)
        after = client.get(f"/api/tickets/{target_id}").json()
        assert changed.status_code == 200 and changed.json()["outcome"] == "updated"
        assert after["subject"] == request["subject"]
        evidence["probes"].append({"id":"S03", "scenario":"Unrelated content with current revision and no authentication",
            "expected_under_requested_policy":"Reject without update",
            "actual":"Updated the temporary target record", "http":changed.status_code,
            "keyword_cosine":float(kw.scores[row]), "semantic_cosine":float(sem[row]),
            "query_coverage":float(kw.coverage[row]), "target_id":target_id,
            "status":"FAIL requested safety policy; reproduced"})
        repeat = client.post("/api/tickets", json=request)
        assert repeat.status_code == 200 and repeat.json()["repeat_submission"]
        evidence["probes"].append({"id":"S04", "scenario":"Repeat same submission after update",
            "expected":"Unchanged and repeat_submission true", "http":repeat.status_code,
            "outcome":repeat.json()["outcome"], "status":"PASS"})
        # Service has an active real model; force encoding failure only after the durable save.
        def fail_encode(_texts):
            raise RuntimeError("synthetic embedding failure")
        engine.semantic.embedder.encode = fail_encode
        unavailable = client.post("/api/tickets", json=body(ticket_id="AUDIT-NEW-1", subject="Another fictional event"))
        saved = client.get("/api/tickets/AUDIT-NEW-1")
        assert unavailable.status_code == 201 and saved.status_code == 200
        evidence["probes"].append({"id":"S05", "scenario":"Embedding generation fails during save/indexing",
            "expected_under_requested_policy":"No mutation if mandatory embedding validation fails",
            "actual":"Record saved; indexing failed; previous snapshot retained", "http":unavailable.status_code,
            "index_status":unavailable.json()["indexing"]["status"],
            "status":"FAIL requested pre-execution policy; existing save-first behavior"})

    # The remaining lifecycle probes use deterministic local fake embeddings.
    for name in ("empty", "capacity"):
        folder = base / name
        folder.mkdir()
        path = folder / "tickets.json"
        path.write_text(json.dumps({"tickets":[make_ticket("AUDIT-1", "Dashboard export fails", "CSV export fails with error 500.")]}), encoding="utf-8")
        config = Settings(tickets_path=path, data_dir=folder / "state", frontend_dist=folder / "none",
                          openai_api_key=None, max_saved_tickets=1, ticket_store="json")
        with TestClient(create_app(config, embedder_factory=lambda _s: FakeEmbedder())) as client:
            assert client.get("/ready").status_code == 200, "Lifecycle fixture must start ready"
            if name == "empty":
                path.write_text('{"tickets":[]}', encoding="utf-8")
                refresh = client.post("/api/index/refresh").json()
                result = client.post("/api/search", json={"subject":"Dashboard export fails", "description":"CSV export fails with error 500."})
                found = result.json()
                ids = [card["ticket"]["ticket_id"] for card in found.get("results", [])]
                evidence["probes"].append({"id":"S06", "scenario":"All source tickets deleted then index refreshed",
                    "expected":"Deleted records excluded", "actual_ids":ids,
                    "refresh":refresh["indexing"]["status"], "search_http":result.status_code,
                    "search_response":found if result.status_code != 200 else None,
                    "ready_http":client.get("/ready").status_code,
                    "detail_http":client.get("/api/tickets/AUDIT-1").status_code,
                    "status":"FAIL freshness expectation" if ids else "ERROR probe" if result.status_code!=200 else "PASS"})
            else:
                one=client.post("/api/tickets",json=body(ticket_id="AUDIT-CAP-1"))
                two=client.post("/api/tickets",json=body(ticket_id="AUDIT-CAP-2"))
                assert one.status_code == 201, one.text
                evidence["probes"].append({"id":"S07", "scenario":"Save two new records with MAX_SAVED_TICKETS=1",
                    "expected":"Second new record refused", "actual_http":[one.status_code,two.status_code],
                    "status":"FAIL configured capacity not enforced" if two.status_code==201 else "PASS"})

assert digest(original) == original_hash
evidence["original_seed_unchanged"] = True
evidence["no_external_provider_called"] = True
(OUT / "safety-probes.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
print(json.dumps(evidence, indent=2))
