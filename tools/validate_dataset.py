"""Validate the expanded synthetic ticket dataset.

    python tools/validate_dataset.py [--dataset PATH] [--report PATH]

Structural checks are failures (exit code 1). Content signals such as
near-duplicate descriptions or unexpected domains are flags for human review;
they never fail the run on their own, because related incidents are supposed
to resemble each other.

Uses only the Python standard library.
"""

from __future__ import annotations

import argparse
import itertools
import json
import re
import sys
from collections import Counter
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "starter_kit" / "data"
DEFAULT_DATASET = DATA / "tickets_1000.json"
ORIGINAL = DATA / "tickets.json"
INCOMING = DATA / "new_tickets_example.json"

FIELDS = ["ticket_id", "category", "subject", "description", "resolution_notes", "tags", "created_at", "status"]
EXPECTED_COUNTS = {
    "auth": 83, "sync": 83, "render": 83, "billing": 83, "export": 83, "perm": 83, "perf": 83, "api": 83,
    "alerts": 84, "data_quality": 84, "connectivity": 84, "workspace_admin": 84,
}
TOTAL = 1000
GENERATED_IDS = [f"T-{n}" for n in range(2001, 2961)]
GENERATED_DATE_RANGE = (date(2025, 1, 1), date(2026, 11, 17))
TAG_FORMAT = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
FORBIDDEN = re.compile(r"snowflake", re.IGNORECASE)
WAREHOUSE_TERMS = re.compile(r"\b(bigquery|redshift|databricks|synapse|data warehouse|warehouse)\b", re.IGNORECASE)
TICKET_REF = re.compile(r"\bT-\d{4}\b")
DOMAIN = re.compile(r"\b(?:[a-z0-9-]+\.)+(?:com|org|net|io|co|ai|dev|app)\b", re.IGNORECASE)
EMAIL = re.compile(r"\b[\w.+-]+@([\w-]+(?:\.[\w-]+)+)\b")
IPV4 = re.compile(r"\b(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})\b")
ALLOWED_DOMAIN = re.compile(r"(^|\.)example\.(com|org|net)$", re.IGNORECASE)
DOC_NETS = ("192.0.2.", "198.51.100.", "203.0.113.")
NEAR_DUPLICATE_JACCARD = 0.6


def normalize(text: str) -> str:
    return " ".join(text.lower().split())


def words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


class Result:
    def __init__(self) -> None:
        self.checks: list[dict] = []
        self.flags: list[dict] = []

    def check(self, number: int, name: str, failures: list[str]) -> None:
        self.checks.append({"check": number, "name": name, "passed": not failures, "failures": failures[:50],
                            "failure_count": len(failures)})

    def flag(self, kind: str, detail: dict) -> None:
        self.flags.append({"kind": kind, **detail})

    @property
    def passed(self) -> bool:
        return all(check["passed"] for check in self.checks)


def validate(dataset_path: Path) -> Result:
    result = Result()

    # 1. UTF-8 JSON
    try:
        payload = json.loads(dataset_path.read_bytes().decode("utf-8"))
        result.check(1, "valid UTF-8 JSON", [])
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        result.check(1, "valid UTF-8 JSON", [f"{type(exc).__name__}: {exc}"])
        return result

    # 2. wrapper
    wrapper_failures = []
    if not isinstance(payload, dict) or not isinstance(payload.get("tickets"), list):
        wrapper_failures.append('root must be an object with a "tickets" array')
    elif set(payload) != {"tickets"}:
        wrapper_failures.append(f"unexpected root keys: {sorted(set(payload) - {'tickets'})}")
    result.check(2, 'root object with only a "tickets" array', wrapper_failures)
    if wrapper_failures:
        return result
    tickets = payload["tickets"]

    # 3. count
    result.check(3, f"exactly {TOTAL} records", [] if len(tickets) == TOTAL else [f"found {len(tickets)}"])

    # 4. schema
    schema_failures = []
    for index, ticket in enumerate(tickets):
        if not isinstance(ticket, dict):
            schema_failures.append(f"record {index} is not an object")
            continue
        if list(ticket) != FIELDS and set(ticket) != set(FIELDS):
            schema_failures.append(f"record {index} fields {sorted(ticket)}")
        for name in ("ticket_id", "category", "subject", "description", "resolution_notes", "created_at", "status"):
            if not isinstance(ticket.get(name), str):
                schema_failures.append(f"record {index} {name} is not a string")
        if not isinstance(ticket.get("tags"), list):
            schema_failures.append(f"record {index} tags is not an array")
    result.check(4, "every record has exactly the eight application fields with correct types", schema_failures)
    records = [t for t in tickets if isinstance(t, dict)]

    # 5. unique ids
    id_counts = Counter(t.get("ticket_id") for t in records)
    result.check(5, "ticket ids are unique", [f"{i} appears {n} times" for i, n in id_counts.items() if n > 1])

    original = json.loads(ORIGINAL.read_text(encoding="utf-8"))["tickets"]
    original_ids = [t["ticket_id"] for t in original]

    # 6. generated id range
    id_failures = [f"missing {i}" for i in GENERATED_IDS if id_counts.get(i, 0) != 1]
    allowed = set(GENERATED_IDS) | set(original_ids)
    id_failures += [f"unexpected id {i}" for i in id_counts if i not in allowed]
    id_failures += [f"generated id collides with original {i}" for i in set(GENERATED_IDS) & set(original_ids)]
    result.check(6, "T-2001 to T-2960 each present exactly once, no other new ids", id_failures)

    # 7, 8. categories
    category_counts = Counter(t.get("category") for t in records)
    result.check(7, "only the 12 permitted categories",
                 [f"unexpected category {c!r}" for c in category_counts if c not in EXPECTED_COUNTS])
    result.check(8, "category counts match the plan",
                 [f"{c}: {category_counts.get(c, 0)} (expected {n})" for c, n in EXPECTED_COUNTS.items()
                  if category_counts.get(c, 0) != n])

    # 9. originals preserved, in order, first
    original_failures = []
    for position, source in enumerate(original):
        if position >= len(records) or records[position] != source:
            original_failures.append(f"position {position}: {source['ticket_id']} differs or is missing")
    result.check(9, "the 40 original records are first and unchanged", original_failures)
    generated = [t for t in records if t.get("ticket_id") not in set(original_ids)]
    generated_ids = {t.get("ticket_id") for t in generated}

    # 10. non-blank strings
    blank = [f"{t.get('ticket_id')} {name}" for t in records
             for name in ("ticket_id", "category", "subject", "description", "resolution_notes", "created_at", "status")
             if isinstance(t.get(name), str) and not t[name].strip()]
    result.check(10, "required strings are not blank", blank)

    # 11. tags
    tag_failures = []
    for t in records:
        tags = t.get("tags")
        if not isinstance(tags, list) or not tags or not all(isinstance(x, str) and x.strip() for x in tags):
            tag_failures.append(f"{t.get('ticket_id')} tags must be a non-empty array of non-blank strings")
            continue
        if len(set(tags)) != len(tags):
            tag_failures.append(f"{t.get('ticket_id')} has duplicate tags")
        if t.get("ticket_id") in generated_ids:
            if not 2 <= len(tags) <= 5:
                tag_failures.append(f"{t['ticket_id']} has {len(tags)} tags (2-5 required)")
            bad = [x for x in tags if not TAG_FORMAT.match(x)]
            if bad:
                tag_failures.append(f"{t['ticket_id']} tag format {bad}")
            if all(x == t.get("category") for x in tags):
                tag_failures.append(f"{t['ticket_id']} has only the category name as tags")
    result.check(11, "tags are valid, unique and consistently formatted", tag_failures)

    # 12. dates
    date_failures = []
    for t in records:
        try:
            value = date.fromisoformat(t.get("created_at", ""))
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", t["created_at"]):
                raise ValueError
        except (TypeError, ValueError):
            date_failures.append(f"{t.get('ticket_id')} invalid date {t.get('created_at')!r}")
            continue
        if t.get("ticket_id") in generated_ids and not GENERATED_DATE_RANGE[0] <= value <= GENERATED_DATE_RANGE[1]:
            date_failures.append(f"{t['ticket_id']} date {value} outside 2025-01-01..2026-11-17")
    result.check(12, "dates are valid (generated ones within range)", date_failures)

    # 13. status
    result.check(13, 'every status is "resolved"',
                 [f"{t.get('ticket_id')} status {t.get('status')!r}" for t in records if t.get("status") != "resolved"])

    # 14. exact duplicates
    pairs = Counter((normalize(t.get("subject", "")), normalize(t.get("description", ""))) for t in records)
    duplicate_ids = [
        [t["ticket_id"] for t in records if (normalize(t["subject"]), normalize(t["description"])) == key]
        for key, n in pairs.items() if n > 1
    ]
    result.check(14, "no exact duplicate subject-description pairs", [f"duplicates: {ids}" for ids in duplicate_ids])

    # 15. incoming examples not copied
    incoming = json.loads(INCOMING.read_text(encoding="utf-8"))["new_tickets"]
    copy_failures = []
    for example in incoming:
        for t in records:
            if normalize(t["subject"]) == normalize(example["subject"]) or normalize(t["description"]) == normalize(example["description"]):
                copy_failures.append(f"{t['ticket_id']} copies incoming {example['ticket_id']}")
            elif jaccard(words(t["subject"] + " " + t["description"]), words(example["subject"] + " " + example["description"])) >= 0.5:
                result.flag("resembles_incoming_example", {"ticket_id": t["ticket_id"], "example": example["ticket_id"]})
    result.check(15, "incoming example tickets are not copied into the history", copy_failures)

    # 16. evaluation integrity: no resolved Snowflake incidents
    snowflake = [t["ticket_id"] for t in generated if FORBIDDEN.search(json.dumps(t))]
    result.check(16, "no generated Snowflake incidents", snowflake)

    # ---------------------------------------------------------------- flags
    texts = {t["ticket_id"]: words(t["description"]) for t in generated}
    for (a, wa), (b, wb) in itertools.combinations(texts.items(), 2):
        score = jaccard(wa, wb)
        if score >= NEAR_DUPLICATE_JACCARD:
            result.flag("near_duplicate_description", {"ticket_ids": [a, b], "jaccard": round(score, 3)})
    for t in generated:
        text = " ".join([t["subject"], t["description"], t["resolution_notes"]])
        if WAREHOUSE_TERMS.search(text):
            result.flag("data_warehouse_term", {"ticket_id": t["ticket_id"], "match": WAREHOUSE_TERMS.search(text).group(0)})
        if TICKET_REF.search(text):
            result.flag("ticket_reference", {"ticket_id": t["ticket_id"], "match": TICKET_REF.search(text).group(0)})
        for email_domain in EMAIL.findall(text):
            if not ALLOWED_DOMAIN.search(email_domain):
                result.flag("non_reserved_email_domain", {"ticket_id": t["ticket_id"], "domain": email_domain})
        for match in DOMAIN.finditer(text):
            host = match.group(0).lower()
            if not ALLOWED_DOMAIN.search(host):
                result.flag("non_reserved_domain", {"ticket_id": t["ticket_id"], "domain": host})
        for match in IPV4.finditer(text):
            address = match.group(0)
            if all(0 <= int(part) <= 255 for part in match.groups()) and not address.startswith(DOC_NETS):
                result.flag("non_documentation_ip", {"ticket_id": t["ticket_id"], "ip": address})
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--report", type=Path, default=ROOT / "reports" / "validation_results.json")
    args = parser.parse_args()
    result = validate(args.dataset)
    flag_counts = Counter(flag["kind"] for flag in result.flags)
    summary = {
        "dataset": str(args.dataset.relative_to(ROOT)) if args.dataset.is_relative_to(ROOT) else str(args.dataset),
        "passed": result.passed,
        "checks": result.checks,
        "flag_counts": dict(flag_counts),
        "flags": result.flags,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(summary, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    for check in result.checks:
        status = "PASS" if check["passed"] else f"FAIL ({check['failure_count']})"
        print(f"{check['check']:>2}. {status:<10} {check['name']}")
        for failure in check["failures"][:5]:
            print(f"      - {failure}")
    print(f"flags for review: {dict(flag_counts) or 'none'}")
    print("RESULT:", "PASS" if result.passed else "FAIL")
    sys.exit(0 if result.passed else 1)


if __name__ == "__main__":
    main()
