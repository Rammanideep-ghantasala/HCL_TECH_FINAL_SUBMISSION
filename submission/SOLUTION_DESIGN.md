# Support Ticket Similarity Assistant

Solari Analytics | Technical submission | 17 November 2026

**Outcome.** The implemented assistant retrieves historical support evidence for a human agent, preserves recorded resolutions, and offers optional summaries and explicit resolution recording. The core local flow works. Production access control and the requested strict identity-and-similarity gate for updates remain incomplete.

## 1  Problem in our own words

Solari Analytics, a B2B dashboard provider of about 400 employees, receives around 150 support tickets daily. Agents can repeat investigations or give inconsistent answers because earlier resolutions are hard to discover. The assistant makes previous work visible before an agent decides how to resolve a new incident. No reduction in handling time or improvement in customer outcomes has been measured.

## 2  Assumptions and scope

| Assumption | Reason and consequence |
| --- | --- |
| Manual ticket entry; English text | The brief names no helpdesk or ingestion API. The implemented interface accepts subject and description, with optional ID, product, error detail and category. |
| Similarity is evidence for investigation | A matching symptom can have a different root cause. Retrieval labels do not authorize actions or prove incident identity. |
| 40-ticket seed is the evaluation baseline | The separate 1,000-ticket synthetic file is available but is not loaded by default. Thresholds were tuned against the 40-ticket seed. |
| Internal synthetic-data prototype | No authentication, role checks or tenant fields exist. Real customer data and public write access require additional controls. |
| Growth and latency require agreement | 150/day implies 54,750/year at 365 days, or 39,000 at 260 workdays, before growth. Proposed interactive target: p95 under 1 second; not a customer-approved SLA. |

## 3  Proposed approach and implemented architecture

A React and TypeScript client calls one FastAPI service. A validated immutable search snapshot holds the ticket records, TF-IDF matrix and local MiniLM embeddings. Exact subject-and-description comparison runs first; other searches use both retrievers, Reciprocal Rank Fusion and a separate relevance policy. Cards show stored resolutions, shared wording, differences and questions for the agent.

An explicit summary click invokes an optional structured-output LLM using signed result references and checked source revisions. A separate save endpoint records an agent's resolved ticket using JSON storage or PostgreSQL. The save commits before index rebuilding; a failed rebuild retains the previous search snapshot. Architecture and workflow diagrams appear on pages 5 and 6.

# Design decisions and next steps

## 4  Main trade offs and decisions

| Decision | Why it fits this problem | Cost or limitation |
| --- | --- | --- |
| Local TF-IDF plus MiniLM | Keywords preserve codes; semantic search can retrieve differently worded symptoms. Local search makes no paid model request. | More dependencies and CPU than keywords alone. Negation and product differences can still mislead. |
| Rank fusion plus relevance policy | RRF orders candidates without comparing unlike raw scores; explicit thresholds control which records become main suggestions. | Conservative thresholds hide useful paraphrases. RRF itself is not a probability or a relevance test. |
| Deterministic cards; optional summaries | Agents can inspect the original evidence. Summary calls happen only on request and do not alter rankings or write tickets. | Structured output checks shape and source coverage, not truth. Human verification remains necessary. |
| One service with in-memory indexes | A small working deployment is appropriate to the provided seed and the brief's 3–4-hour target. No vector database is needed for this sample. | Full TF-IDF rebuilds and per-process snapshots limit growth and require freshness coordination. |
| JSON demo or PostgreSQL repository | JSON supports local startup; PostgreSQL implements transactions and row locking for durable concurrent writes. | JSON is safe only within one process. PostgreSQL integration was not rerun in the current evidence run. |

## Measured result and present boundary

Fresh checks: 164 backend tests and 71 frontend tests passed; 14 PostgreSQL tests were skipped. On the frozen 24-query synthetic final set, hybrid retrieval displayed the expected ticket for 5 of 12 answerable queries and ranked it in the first three for 10 of 12. No false close match appeared in the 11 designated no-match cases. These small counts do not establish production accuracy.

**Safety boundary.** Search performs no ticket writes. However, the explicit save endpoint accepts unrelated replacement content when supplied with the current target revision. A temporary-data probe reproduced that behavior with lexical cosine 0 and semantic cosine -0.043. Current revision checking prevents blind/stale writes; it does not satisfy the requested exact-identity and similarity policy.

## 5  With more time data or budget

First protect write, detail, refresh and paid-summary endpoints with authenticated roles and tenant filtering. Add the deterministic update gate described on page 11 if strict matching is required. Correct empty-index freshness and capacity enforcement, and invalidate cached ticket details on source revision changes.

Next review paraphrase and no-match behavior with support agents, label representative real tickets with appropriate privacy controls, and retune on a new held-out evaluation. Measure agent usefulness, handling time, false suggestions and response latency before adding a reranker, larger model or nearest-neighbor index. Validate deployment, backups and external integration only after the local safety conditions are met.

Evidence snapshot: commit 0acef1654e35, inspected working tree on 17 November 2026. Source files were not changed to complete this submission. Detailed requirements, algorithms, controls, tests and limitations follow.
