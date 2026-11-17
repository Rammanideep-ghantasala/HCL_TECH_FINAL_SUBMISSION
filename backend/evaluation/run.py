"""Measure retrieval quality on a reviewed query set.

    python -m evaluation.run --set tuning            # measure with the shipped thresholds
    python -m evaluation.run --set tuning --sweep    # compare threshold settings (tuning set only)
    python -m evaluation.run --set final             # the held-out check; run once thresholds are frozen

What is measured, per retrieval mode (hybrid, keyword-only, semantic-only):

* top-three hit rate on queries that have a useful past ticket: the share whose
  expected ticket is shown as a close match (at most three are ever shown);
* ranking-only top three: the share whose expected ticket is among the first
  three candidates before the relevance decision. This isolates the retriever;
* false-match rate on queries with no useful past ticket: the share for which
  any ticket is shown as a close match;
* latency per search.

Labels are judgements made by reading the tickets. Queries on which the two
independent labellers disagreed are reported separately and left out of the
headline numbers.
"""

from __future__ import annotations

import argparse
import dataclasses
import importlib.util
import itertools
import json
import logging
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.dont_write_bytecode = True  # never leave cache files next to the starter kit

from app.config import BACKEND_DIR, REPO_DIR, Settings  # noqa: E402
from app.engine import SearchEngine, SearchInput  # noqa: E402
from app.tickets import load_corpus  # noqa: E402
from app.retrieval import relevance as rel  # noqa: E402
from app.retrieval.semantic import query_text  # noqa: E402

DATA_DIR = Path(__file__).resolve().parent / "data"
RESULTS_DIR = Path(__file__).resolve().parent / "results"
NO_MATCH_TYPES = ("misleading", "negation", "different_product", "unfamiliar")


def load_queries(name: str) -> dict:
    path = DATA_DIR / f"{name}_queries.json"
    return json.loads(path.read_text(encoding="utf-8"))


def to_input(query: dict) -> SearchInput:
    return SearchInput(
        subject=query.get("subject", ""),
        description=query.get("description", ""),
        product=query.get("product") or "",
        error_detail=query.get("error_detail") or "",
    )


def _build(settings: Settings) -> SearchEngine:
    """Always the 40 seed tickets, whatever store is configured, so results stay comparable."""
    return SearchEngine.build(settings, corpus=load_corpus(settings.tickets_path, settings.searchable_status_set))


def build_engines() -> dict[str, SearchEngine]:
    return {
        "hybrid": _build(Settings()),
        "keyword_only": _build(Settings(semantic_enabled=False)),
        "semantic_only": _build(Settings(keyword_enabled=False)),
    }


def _rate(hits: int, total: int) -> dict:
    return {"count": hits, "of": total, "rate": round(hits / total, 3) if total else None}


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))
    return round(ordered[index], 2)


def evaluate_mode(engine: SearchEngine, queries: list[dict]) -> dict:
    if not engine.ready:
        return {"available": False}
    engine.search(SearchInput(subject="warm up the model before timing", description="warm up only"))
    rows = []
    latencies: list[float] = []
    # A second pass with every threshold switched off shows what the retriever
    # ranks in its first three places, independent of the relevance decision.
    shipped = engine.relevance
    permissive = dataclasses.replace(
        shipped,
        **{f.name: -1.0 for f in dataclasses.fields(shipped) if f.name not in ("version", "calibration")},
    )
    for query in queries:
        engine.relevance = permissive
        try:
            unfiltered = engine.search(to_input(query))
        finally:
            engine.relevance = shipped
        first_three = [card["ticket"]["ticket_id"] for card in unfiltered["results"]][:3]
        outcome = engine.search(to_input(query))
        latencies.append(outcome["timing_ms"]["total"])
        shown = [card["ticket"]["ticket_id"] for card in outcome["results"]]
        ranked = shown + [card["ticket"]["ticket_id"] for card in outcome["related"]]
        rows.append(
            {
                "id": query["id"],
                "type": query["type"],
                "agreed": query.get("agreed", True),
                "expected_useful": query["expected_useful"],
                "expected_status": query["expected_status"],
                "status": outcome["status"],
                "shown_as_close_match": shown,
                "related": ranked[len(shown) :],
                "top_scores": outcome["results"][0]["scores"] if outcome["results"] else (
                    outcome["related"][0]["scores"] if outcome["related"] else None
                ),
                "hit": bool(set(query["expected_useful"]) & set(shown)),
                "ranked_top3": bool(set(query["expected_useful"]) & set(first_three)),
                "shown_top3_either_tier": bool(set(query["expected_useful"]) & set(ranked[:3])),
                "false_match": not query["expected_useful"] and bool(shown),
            }
        )
    counted = [row for row in rows if row["agreed"]]
    matchable = [row for row in counted if row["expected_useful"]]
    no_match = [row for row in counted if not row["expected_useful"] and row["type"] in NO_MATCH_TYPES]
    vague = [row for row in counted if row["type"] == "vague"]
    summary = {
        "available": True,
        "mode": engine.mode,
        "top3_hit_rate_matchable": _rate(sum(row["hit"] for row in matchable), len(matchable)),
        "ranking_only_top3_matchable": _rate(sum(row["ranked_top3"] for row in matchable), len(matchable)),
        "shown_in_first_three_either_tier": _rate(
            sum(row["shown_top3_either_tier"] for row in matchable), len(matchable)
        ),
        "false_match_rate_no_match_queries": _rate(sum(row["false_match"] for row in no_match), len(no_match)),
        "by_type": {},
        "vague_reported_as_insufficient_input": _rate(
            sum(row["status"] == "insufficient_input" for row in vague), len(vague)
        ),
        "latency_ms": {
            "n": len(latencies),
            "median": round(statistics.median(latencies), 2) if latencies else None,
            "p95": _percentile(latencies, 0.95),
            "max": round(max(latencies), 2) if latencies else None,
        },
    }
    for kind in ("strong_match", "paraphrase"):
        subset = [row for row in matchable if row["type"] == kind]
        summary["by_type"][kind] = {"top3_hit": _rate(sum(row["hit"] for row in subset), len(subset))}
    for kind in NO_MATCH_TYPES:
        subset = [row for row in no_match if row["type"] == kind]
        summary["by_type"][kind] = {"false_match": _rate(sum(row["false_match"] for row in subset), len(subset))}
    return {"summary": summary, "rows": rows}


def starter_kit_baseline(queries: list[dict]) -> dict:
    """The provided keyword-overlap scorer, run unmodified, as a ranking baseline."""
    path = REPO_DIR / "starter_kit" / "ticket_utils.py"
    spec = importlib.util.spec_from_file_location("starter_ticket_utils", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    tickets = module.load_tickets(str(REPO_DIR / "starter_kit" / "data" / "tickets.json"))
    matchable = [q for q in queries if q.get("agreed", True) and q["expected_useful"]]
    hits = 0
    for query in matchable:
        text = f"{query.get('subject', '')} {query.get('description', '')}"
        top = [ticket["ticket_id"] for ticket, _ in module.find_top_matches(text, tickets, top_n=3)]
        hits += bool(set(query["expected_useful"]) & set(top))
    return {
        "ranking_only_top3_matchable": _rate(hits, len(matchable)),
        "note": "It always returns three tickets, so it has no notion of 'no match' to measure.",
    }


def sweep(engine: SearchEngine, queries: list[dict]) -> list[dict]:
    """Try threshold combinations on the tuning set, reusing each query's scores."""
    assert engine.keyword is not None and engine.semantic is not None and engine.relevance is not None
    prepared = []
    for query in queries:
        if not query.get("agreed", True) or query["type"] == "vague":
            continue
        raw = to_input(query)
        keyword_text = "\n".join(p for p in (raw.subject, raw.description, raw.product, raw.error_detail) if p)
        keyword_query = engine.keyword.query(keyword_text)
        scores, _ = engine.semantic.query(
            query_text(raw.subject, raw.description, raw.product, raw.error_detail), engine.settings.max_semantic_windows
        )
        prepared.append((query, keyword_query, scores))
    matchable = sum(1 for query, _, _ in prepared if query["expected_useful"])
    no_match = sum(1 for query, _, _ in prepared if not query["expected_useful"])

    table = []
    grid = itertools.product(
        (0.45, 0.50, 0.55, 0.60, 0.65),  # useful_semantic_min
        (0.15, 0.20, 0.25, 0.30, 0.35, 0.40),  # useful_coverage_min
        (0.65, 0.70, 0.75, 0.80, 0.85),  # useful_semantic_alone_min
    )
    for semantic_min, coverage_min, alone_min in grid:
        if alone_min < semantic_min:
            continue
        config = dataclasses.replace(
            engine.relevance,
            hybrid_useful_semantic_min=semantic_min,
            hybrid_useful_coverage_min=coverage_min,
            hybrid_useful_semantic_alone_min=alone_min,
        )
        hits = false_matches = 0
        for query, keyword_query, scores in prepared:
            labelled = engine.rank_and_label(rel.HYBRID, keyword_query, scores, None, relevance=config)
            shown = [item["ticket"].ticket_id for item in labelled if item["level"] == rel.USEFUL][
                : engine.settings.max_useful_results
            ]
            if query["expected_useful"]:
                hits += bool(set(query["expected_useful"]) & set(shown))
            else:
                false_matches += bool(shown)
        table.append(
            {
                "useful_semantic_min": semantic_min,
                "useful_coverage_min": coverage_min,
                "useful_semantic_alone_min": alone_min,
                "hits": hits,
                "matchable": matchable,
                "false_matches": false_matches,
                "no_match_queries": no_match,
            }
        )
    table.sort(key=lambda row: (row["false_matches"] - row["hits"], row["false_matches"]))
    return table


def keyword_sweep(queries: list[dict]) -> list[dict]:
    """Compare keyword settings (phrases, stemming) by ranking-only top three."""
    table = []
    for bigrams, stemming in itertools.product((False, True), (False, True)):
        engine = _build(
            Settings(semantic_enabled=False, keyword_bigrams=bigrams, keyword_stemming=stemming)
        )
        assert engine.keyword is not None
        matchable = [q for q in queries if q.get("agreed", True) and q["expected_useful"]]
        top1 = top3 = 0
        for query in matchable:
            raw = to_input(query)
            text = "\n".join(p for p in (raw.subject, raw.description, raw.product, raw.error_detail) if p)
            hits = engine.keyword.top_hits(engine.keyword.query(text), 3)
            ids = [engine.keyword.row_ids[hit.row] for hit in hits]
            top1 += bool(ids[:1] and ids[0] in query["expected_useful"])
            top3 += bool(set(ids) & set(query["expected_useful"]))
        table.append(
            {"phrases": bigrams, "stemming": stemming, "top1": top1, "top3": top3, "matchable": len(matchable),
             "vocabulary": engine.keyword.vocabulary_size}
        )
    return table


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--set", choices=("tuning", "final"), required=True)
    parser.add_argument("--sweep", action="store_true", help="compare threshold and keyword settings (tuning only)")
    parser.add_argument("--no-write", action="store_true", help="print only; do not write a results file")
    args = parser.parse_args()
    logging.disable(logging.WARNING)

    payload = load_queries(args.set)
    queries = payload["queries"]
    if args.sweep:
        if args.set != "tuning":
            raise SystemExit("thresholds are tuned on the tuning set only; the final set is for checking")
        hybrid = _build(Settings())
        print(json.dumps({"keyword_settings": keyword_sweep(queries), "threshold_sweep_top15": sweep(hybrid, queries)[:15]}, indent=1))
        return

    engines = build_engines()
    report = {
        "set": args.set,
        "run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "queries_total": len(queries),
        "queries_with_agreed_labels": sum(1 for q in queries if q.get("agreed", True)),
        "queries_with_disputed_labels": [q["id"] for q in queries if not q.get("agreed", True)],
        "index_version": engines["hybrid"].index_version,
        "relevance_config_version": engines["hybrid"].relevance.version if engines["hybrid"].relevance else None,
        "model": [engines["hybrid"].model_id, engines["hybrid"].model_revision],
        "modes": {name: evaluate_mode(engine, queries) for name, engine in engines.items()},
        "starter_kit_baseline": starter_kit_baseline(queries),
    }
    printable = {
        **{key: value for key, value in report.items() if key != "modes"},
        "modes": {name: result.get("summary", result) for name, result in report["modes"].items()},
    }
    print(json.dumps(printable, indent=1))
    if not args.no_write:
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        (RESULTS_DIR / f"{args.set}_results.json").write_text(json.dumps(report, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
