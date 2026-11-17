# Business requirements and traceability

The original brief is case-study-brief.md; no file named case study.md exists in the supplied project. It asks for ambiguity handling, sound design, a working similarity flow, clear communication and defensible trade-offs. No private scoring rubric or mandatory long-form section order was supplied. B indicates a brief requirement, A an explicit project assumption, and U a requirement added in the preparation request.

| ID and requirement | Implementation and evidence | Status |
| --- | --- | --- |
| B1 Find similar historical tickets | SearchEngine.search; keyword and semantic indexes; React search form and outcome cards. Supports exact and ranked retrieval. | Implemented; recall limited |
| B2 Show how past tickets were resolved | cards.build_card uses the canonical TicketRecord resolution; detail endpoint retrieves stored versions. Original records remain available beside summaries. | Implemented |
| B3 Working end-to-end prototype | FastAPI routes, React client, model preparation, Docker build definition and local run instructions. Backend and frontend checks executed. | Implemented locally; cloud unverified |
| B4 State assumptions and design choices | Two-page design overview above follows problem, assumptions, approach, trade-offs and future work. | Documented |
| B5 README and runnable source | Existing repository has dependencies, lockfiles and tests. The repository-root README.md maps evaluator priorities and run steps. | Included in this repository |
| B6 Plan for growth | In-memory snapshots, embedding reuse, SQL repository and refresh polling are implemented. No representative high-volume load test exists. | Partial |
| B7 Optional AI enhancement | Pinned all-MiniLM-L6-v2 retrieval; explicit OpenAI summary integration. Provider tests use mocks, not a live paid endpoint. | Implemented; live summary unverified |
| A1 Prefer evidence over automatic resolution | No refund, account-reset, helpdesk update or command-execution integration exists. Agent actions are outside this application. | Implemented scope boundary |
| A2 Make uncertainty visible | Insufficient-input, no-strong-match, degraded retrieval and service errors are distinct. Main lists are never padded. | Implemented; related cards hidden on no-match screen |

## Users inputs outputs and success

Primary users are support agents; the customer sponsor is VP of Customer Success Jamie Chen. Operators manage indexes, storage and deployments. Inputs are ticket text and optional context; historical records provide ID, subject, description and resolution. Outputs are labelled evidence cards, original records, optional summaries, and separately saved resolution records. Success should be judged by useful prior evidence found, consistent advice and lower investigation effort; those business outcomes need an agent pilot.

# Additional requirements and gaps

| ID and requested control | Code evidence and interpretation | Status |
| --- | --- | --- |
| U1 TF-IDF and semantic matching | retrieval/keyword.py and semantic.py implement both. Model metadata confirms 384 dimensions and 256-token inputs. | Implemented |
| U2 Combined matching score | fusion.py uses RRF ranks; relevance.py gates by raw cosine and coverage. No alpha/beta score is implemented. | Alternative implemented |
| U3 Authoritative identity before update | plan_save locates a ticket by ID and checks expected_revision. No external authority, tenant or account validation exists. | Partial |
| U4 Critical attributes and score thresholds before mutation | Save route does not invoke the search/relevance pipeline or compare immutable business attributes. Probe S03 demonstrates unrelated replacement. | Not implemented |
| U5 Reject ambiguous intended targets | Search preserves separate records with identical text; save operates on a supplied/generated ID. Intended-incident ambiguity is not assessed. | Partial |
| U6 Fail closed if embedding validation fails | Search degrades to keywords. Save commits then indexes; embedding failure can leave a saved, unindexed record (S05). This differs from the requested pre-write rule. | Not implemented for writes |
| U7 Idempotency and concurrency | submission_id, content comparison and revision checks; JSON locks; PostgreSQL transaction, row lock and primary keys. | Implemented; storage-specific scope |
| U8 Verify updates and audit rejections | Save reads back current record and reports indexing; logs ticket_saved/save_refused and request IDs. No immutable actor/tenant audit ledger or external side-effect verification. | Partial |
| U9 Security and privacy controls | Request limits, CSP, no-store headers, safe rendering, parameterized SQL and server-side secrets exist. Authentication, RBAC and tenant isolation do not. | Partial |
| U10 Cost and performance evidence | Local model; cached historical vectors; optional LLM calls. Measured retrieval timings and transparent scenario estimates provided. | Partial; no production telemetry |
| U11 Reliability and testing | 235 current application tests passed. Seven additional probes expose gaps outside that suite. Cloud, current PostgreSQL and live LLM checks remain outstanding. | Partial |

## Constraints and unresolved decisions

The brief expects roughly 3–4 focused hours and allows a deliberately small scope. TalentStudio limits the ZIP to under 50 MB and the design to 20 pages, reads roughly the first 100 README lines and contents of 15 files total. This PDF is 20 pages with three embedded raster diagrams. The short design is also supplied as Markdown. Resolve customer identity fields, permitted edit types, acceptable paraphrase risk, growth bursts, data retention, provider availability and hosting region before production commitments.
