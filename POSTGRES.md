# PostgreSQL storage

The assistant can keep tickets in PostgreSQL instead of the JSON demonstration files. Retrieval is unchanged in both modes: TF-IDF, all-MiniLM-L6-v2 embeddings, NumPy cosine similarity, Reciprocal Rank Fusion and the same relevance thresholds. PostgreSQL only stores tickets. There is no pgvector, and search requests never read or write the database.

| Mode | `TICKET_STORE` | Tickets come from | Saved resolutions go to |
|---|---|---|---|
| JSON (local demonstrations) | `json` (default) | `starter_kit/data/tickets.json` plus `DATA_DIR/saved_tickets.json` | `DATA_DIR/saved_tickets.json` |
| PostgreSQL | `postgresql` | The `assistant_tickets` table only | The `assistant_tickets` table |

When PostgreSQL is selected and cannot be reached, the service reports a database error: `/ready` returns 503 and search returns 503 `database_unavailable`. It never falls back to the JSON data.

## 1. Development setup (Windows PowerShell)

```powershell
# Start a local PostgreSQL 17 on 127.0.0.1:55432 (databases "tsa" and "tsa_test")
docker compose -f docker-compose.postgres.yml up -d

cd backend
$env:TICKET_STORE = "postgresql"
$env:DATABASE_URL = "postgresql+psycopg://tsa:tsa-local-dev-only@127.0.0.1:55432/tsa"

uv run python -m app.manage migrate                                         # create the assistant_* tables
uv run python -m app.manage import-json ..\starter_kit\data\tickets.json --dry-run   # validate and compare only
uv run python -m app.manage import-json ..\starter_kit\data\tickets.json            # insert missing tickets
uv run python -m app.manage check                                           # what passes validation
$env:PORT = "8000"; uv run python -m app                                    # start the API
```

The database is never seeded automatically. Run the import explicitly, once per dataset.

To run the server in Docker against this database, pass the two variables:

```powershell
docker run --rm -p 8080:8080 -e TICKET_STORE=postgresql `
  -e DATABASE_URL="postgresql+psycopg://tsa:tsa-local-dev-only@host.docker.internal:55432/tsa" `
  ticket-similarity-assistant
```

## 2. Credentials

- `DATABASE_URL` is read from the environment into a secret value. It is never logged, never returned by any endpoint and never sent to the browser.
- Database errors are logged by exception type only, because driver messages can contain host or user names.
- The password in `docker-compose.postgres.yml` is a throwaway for a database bound to `127.0.0.1`. Use a secrets manager for anything else.

Connection behaviour (all configurable, see `.env.example`):

| Setting | Default | Purpose |
|---|---|---|
| Pool size and overflow | 5 + 5 | Connection pooling |
| Pool timeout | 10 s | Wait for a free connection |
| Connect timeout | 5 s | Unreachable server fails fast |
| Statement timeout | 15 s | No query runs unbounded |
| Lock timeout | 5 s | A blocked save fails instead of hanging |
| Pre-ping and recycle | on, 30 min | Dropped connections are replaced |

Every save runs in one transaction.

## 3. Schema and migrations

Migrations live in `backend/migrations` and use Alembic. They create only application-owned objects, all named `assistant_*`. The migration history is kept in `assistant_schema_version`, so a customer's own Alembic history in the same database is never touched.

`assistant_tickets` (primary key `ticket_id`):

| Column | Type | Notes |
|---|---|---|
| ticket_id | varchar(64) | Primary key. Letters, digits, `.`, `_`, `-` |
| subject, description, resolution_notes | text | Required. For saved resolutions, `resolution_notes` holds the resolution steps |
| category | text | |
| tags | text[] | |
| created_at | date | |
| status | text | Only `resolved` tickets (setting `SEARCHABLE_STATUSES`) are searchable |
| product, error_detail, investigation, root_cause, verification, resolved_by | text | Recorded by the resolution form |
| resolved_at | timestamptz | Set by the server |
| updated_at | timestamptz | Set by a trigger on every content change |
| version | integer | Incremented by the trigger on every content change, including direct SQL edits |
| index_status | text | `pending`, `indexed` or `failed` |
| index_error, indexed_version | text, integer | Last indexing outcome |

`assistant_submissions` records each saved submission id, so repeated submissions are recognised.

## 4. Using an existing ticket table with different column names

Nothing in the application reads, renames, drops or overwrites a customer's tables. To use an existing dataset, a database administrator copies it into `assistant_tickets` with an explicit mapping, then runs `check`.

| Application field | Required | Typical source column | Validation applied when loading |
|---|---|---|---|
| ticket_id | yes | `id`, `ticket_number` | 1–64 of letters, digits, `.`, `_`, `-`; unique |
| subject | yes | `title`, `summary` | Not blank |
| description | yes | `body`, `details` | Not blank |
| resolution_notes | yes | `resolution`, `solution` | Not blank; blank rows are not searchable |
| status | yes | `state` | Only `resolved` is searchable |
| category | no | `queue`, `type` | |
| tags | no | `labels` | Text array |
| created_at | no | `opened_at` | A date |

Example, run by the administrator, with names adjusted to the real table:

```sql
INSERT INTO assistant_tickets (ticket_id, subject, description, resolution_notes, category, tags, created_at, status)
SELECT t.ticket_number, t.title, t.body, t.resolution, t.queue, coalesce(t.labels, '{}'), t.opened_at::date, lower(t.state)
FROM helpdesk.tickets AS t
WHERE t.resolution IS NOT NULL
ON CONFLICT (ticket_id) DO NOTHING;
```

Then run `uv run python -m app.manage check`. It reports how many tickets are searchable and how many rows were rejected, by reason (missing subject, missing resolution, invalid id and so on). Rejected rows stay in the table; they are only left out of search.

## 5. Saving resolved tickets

`POST /api/tickets` is the only write path. It runs one transaction and applies these rules:

| Situation | Result |
|---|---|
| The same submission id again, same content | Nothing written; the first result is returned (`repeat_submission: true`) |
| The same submission id with different content | 409 `submission_conflict`; nothing written |
| No ticket id | A new id `SR-YYYYMMDD-XXXXXXXX` is generated. Identical text from another incident stays a separate ticket |
| A new ticket id | Inserted once (`INSERT … ON CONFLICT DO NOTHING`) |
| An existing ticket id, identical content | Nothing changes |
| An existing ticket id, different content, no or stale `expected_revision` | 409 `ticket_conflict`; nothing written |
| An existing ticket id, `expected_revision` current | Updated. The row is locked (`SELECT … FOR UPDATE`) while the revision is compared |

Concurrent copies of one submission produce one record, because the submission id is a primary key written in the same transaction. Concurrent saves of different content to one new id produce one record and one conflict. Both are covered by integration tests.

## 6. Indexing and when changes become visible

Searches use an in-memory snapshot: the TF-IDF index and embedding matrix, each row linked to its `ticket_id`. A new snapshot is built off to the side and published in one step, so a search never sees a half-built index.

| Change | Visible to searches |
|---|---|
| A ticket saved through the form | As soon as the save response says "available for future searches" |
| A save whose indexing failed | Saved and readable at `GET /api/tickets/{id}` (`index_status: failed`), but not searchable until a refresh succeeds |
| Rows inserted, edited, deleted or reopened directly in SQL | After a refresh, an optional scheduled refresh, or a restart |
| Saves made on another app instance | Same as direct SQL edits |

Ways to refresh:

- Retry or pick up changes on a running server: `POST /api/index/refresh`, or `uv run python -m app.manage refresh-index --url http://HOST:PORT`. Add `--full` to force a rebuild.
- Scheduled: set `INDEX_REFRESH_INTERVAL_SECONDS` (for example 60). Each instance then checks a cheap change marker (row count, version sum, latest `updated_at`) and rebuilds only when it changed.
- On start-up: every eligible row is loaded and indexed, so tickets left `pending` or `failed` by a crash or an earlier failure are recovered automatically.

A refresh refits TF-IDF on all searchable tickets, because its weights depend on the whole corpus. Embeddings are recomputed only for text that is new or changed. Vectors are cached under `DATA_DIR/embeddings/<source>/`, one folder per database (host, port and name; never credentials). They are keyed by the exact embedded text plus the model id, revision, vector size and text template. The model is only used to encode text; it is never trained.

## 7. Tests

```powershell
docker compose -f docker-compose.postgres.yml up -d
cd backend
$env:TEST_DATABASE_URL = "postgresql+psycopg://tsa:tsa-local-dev-only@127.0.0.1:55432/tsa_test"
uv run pytest -m postgres
```

The tests migrate `tsa_test` and empty its `assistant_*` tables before each test. Never point `TEST_DATABASE_URL` at data you need. Without the variable the tests are skipped.

## 8. Limitations

- **Multiple instances.** Each process holds its own snapshot. Without the scheduled refresh, a ticket saved on one instance is not searchable on another until that instance refreshes or restarts.
- **Refresh cost.** Every refresh loads all searchable tickets and refits TF-IDF. That is fine for thousands of tickets; for hundreds of thousands it would need an incremental design.
- **No access control.** The save endpoint has no sign-in. Anyone who can reach the server can save tickets. Put the service behind authentication before using real data.
- **AWS.** The prepared deployment does not create a database or pass `DATABASE_URL`. See DEPLOY_AWS.md, "Persistence".
