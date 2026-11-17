# Support Ticket Similarity Assistant

Solari Analytics receives about 150 tickets/day. This prototype retrieves past resolved tickets and their recorded resolutions for human investigation. Search is read only. Optional summaries and explicit resolution recording are separate actions.

Final repository submission for the [HCL FDE case study](case-study-brief.md). Run commands from the repository root. The required design, implementation and run guide are included here.

## Submission documents

- [Final technical submission PDF](HCL_FDE_Final_Technical_Submission.pdf): 20 pages; design overview, requirements, diagrams, implementation, evidence, costs and gaps.
- [SOLUTION_DESIGN.md](SOLUTION_DESIGN.md): the required concise design; corresponds to the PDF's first two pages.
- Editable technical text, requirement mapping and diagram PNG/Mermaid files are in [submission/](submission/). Only the embedded diagrams are needed in the upload PDF.

## Run locally

Requires Python 3.12 through uv and Node.js 24. Initial dependency/model setup needs internet.

```powershell
cd backend
uv sync
uv run python -m app.prepare fetch-model
uv run python -m app.prepare build-index
$env:PORT = '8000'
uv run python -m app
```

In a second terminal:

```powershell
cd frontend
npm ci
npm run dev
```

Open http://localhost:5173. Without model files, search falls back to keywords. Docker alternative from the root: `docker build -t ticket-similarity-assistant .`, then `docker run --rm -p 8080:8080 ticket-similarity-assistant`.

JSON is the default store; keep one process and mount `/app/state` for container persistence. PostgreSQL setup is in [POSTGRES.md](POSTGRES.md). The default corpus is the original 40-ticket JSON; the optional 1,000-ticket synthetic expansion is included but is not loaded automatically. See [data notes](starter_kit/data/README.md).

## Requirements and reviewer file priorities

The guide reads roughly 100 README lines and 15 files total. With this README, the PDF and SOLUTION_DESIGN.md, prioritize these 12 code/test files; retain the complete runnable source tree.

| Requirement | Priority file | Status |
|---|---|---|
| Exact and similar historical evidence | `backend/app/engine.py` | Implemented; paraphrase display limited |
| TF-IDF and query coverage | `backend/app/retrieval/keyword.py` | Implemented |
| Local MiniLM embedding similarity and reuse | `backend/app/retrieval/semantic.py` | Implemented |
| Combined ordering | `backend/app/retrieval/fusion.py` | RRF; no weighted alpha/beta score |
| Relevance thresholds | `backend/app/retrieval/relevance.py` | Implemented; configuration in `backend/config/relevance.json` |
| Input validation, API, errors and logs | `backend/app/main.py` | Implemented; no authentication |
| Revision and idempotency rules | `backend/app/repository/base.py` | Implemented; strict business-identity gate absent |
| Atomic local persistence | `backend/app/repository/json_repo.py` | Single-process scope |
| Transactional shared persistence | `backend/app/repository/postgres.py` | Implemented; current DB tests skipped |
| Save then index and retry | `backend/app/service.py` | Implemented; empty-index freshness gap |
| Explicit summaries with checked sources | `backend/app/summaries.py` | Mock-tested; live provider unverified |
| Executed save/duplicate/conflict tests | `backend/tests/test_resolution_workflow.py` | Passed |

Other core files: `backend/app/cards.py` preserves recorded resolutions; `text.py` handles normalization; `schemas.py` defines contracts. `frontend/src/App.tsx`, `components/OutcomeArea.tsx` and `components/ResolutionForm.tsx` implement the agent workflow.

## Test and evaluate

```powershell
cd backend
uv run pytest -ra
uv run python -m evaluation.run --set final --no-write
# From frontend in another terminal:
npm run typecheck
npm test
npm run lint
npm run build
```

Fresh results on 17 November 2026: 164 backend and 71 frontend tests passed; 14 PostgreSQL tests skipped. Types, lint and build passed. Deployment configuration: 19 cases passed. Final synthetic retrieval: hybrid displayed expected evidence in 5/12 answerable cases, ranked it top-three in 10/12, and showed zero false close matches in 11 designated no-match cases. These are sample counts, not production accuracy.

## Trade offs and limitations

- Search never writes; the explicit save route **can replace an existing record with its current revision**. Similarity and mandatory business attributes are not checked before a save. An unrelated replacement was reproduced on temporary synthetic data. The PDF labels the requested stricter gate as proposed.
- No authentication, tenant isolation or per-user rate/spend limit exists. `IDENTITY_HEADER` is only a trusted-proxy integration setting. Use synthetic data; keep public writes and paid summaries disabled until access controls are verified.
- A model failure can leave a successfully saved record unindexed. All-source deletion can leave an old search snapshot visible. `MAX_SAVED_TICKETS` is not enforced.
- Hybrid main-match recall for paraphrases was 0/6. The no-match UI hides related cards. Product/code comparisons and negation handling do not prove applicability.
- Full-record cache can retain externally changed details; ordinary detail fetch has no timeout. Browser feedback/history is session-local and does not train the model.
- Optional summaries require server-side credentials, send selected records externally and need source verification. The configured default model is marked deprecated on its vendor page; verify a supported model before live use.
- AWS deployment, live LLM quality and sustained load were not verified here. Cost figures in the PDF are explicitly illustrative. Evidence is in [the verification report](reports/submission-preparation-2026-11-17/README.md).

## Optional summaries of top 3 / top 5 results

Search needs no API key. To enable paid summaries, set `OPENAI_API_KEY` and a supported `SUMMARY_MODEL` in the backend environment; configure `SUMMARY_SIGNING_KEY` consistently across replicas. Inspect [.env.example](.env.example) for settings. A populated `.env` is never committed. AWS scripts and their unverified deployment boundaries are described in [DEPLOY_AWS.md](DEPLOY_AWS.md).

## ZIP preparation

Keep under 50 MB. Exclude dependencies, model weights, state, secrets, generated builds, caches, standalone images and bulky data-generation artifacts. Keep source, lockfiles/configuration, tests and the small seed/example JSON needed to run. Embed diagrams in the PDF; keep PNG/Mermaid editing copies outside the upload. Review the ZIP before upload because additions require a new submission.
