# Code Review Fixes Design

Date: 2026-07-31
Status: Approved design

## Objective

Fix all eight issues identified in the 2026-07-30 code review without replacing the project's lightweight SQLite architecture:

1. Notifications can be lost permanently after articles are committed.
2. New-source catch-up protection reads its baseline after insertion.
3. User-visible strings contain literal question-mark corruption.
4. The generic RSS collector reports HTTP and unusable-feed failures as an empty successful collection.
5. Election-context `as_of` filtering uses the wrong time direction and is not applied consistently.
6. Superseded election events remain visible in default search and snapshot construction.
7. Active snapshot uniqueness is not enforced and the current duplicate check cannot work.
8. Election-report `--send-existing` neither reaches nor performs a real send.

## Confirmed Product Decisions

- Notification failures use durable retry: articles stay in the database and pending delivery work is retried on later runs.
- Election reports produce and send a Word document while retaining the structured JSON evidence artifact.
- Existing databases are migrated in place. Deleting or rebuilding production databases is not acceptable.
- The implementation keeps synchronous Python, SQLite, and the existing notifier/collector structure. No scheduler, broker, ORM, or asynchronous framework is introduced.

## Considered Approaches

### 1. Independent delivery outbox — selected

Persist delivery work separately from article identity. This models notification as a batch/artifact concern, supports partial success, and permits later retry without weakening URL deduplication.

### 2. Delivery columns on `articles`

This has a smaller initial schema change but incorrectly treats batch text, Word files, cards, and multiple channels as article-level state. Partial delivery becomes ambiguous and future channels multiply columns.

### 3. Commit articles only after remote send

This avoids a new table but cannot make SQLite and a remote HTTP request atomic. A crash after the remote accepts a message but before commit causes duplicates; a crash before commit loses the durable collection result. It also conflicts with the confirmed requirement to retain articles and retry delivery.

## Architecture

### Notification outbox

Add a `notification_outbox` table to the news database:

| Column | Purpose |
| --- | --- |
| `id` | Integer primary key |
| `delivery_key` | Stable unique key for one run, artifact type, and channel |
| `delivery_type` | `text_digest`, `word_document`, or `highlight_card` |
| `channel` | Concrete delivery channel recorded at enqueue time |
| `payload_json` | Versioned data required to perform or rebuild the delivery |
| `status` | `pending` or `sent` |
| `attempt_count` | Number of failed attempts |
| `next_attempt_at` | Earliest next retry time |
| `last_error` | Last concise failure description |
| `created_at` | Enqueue timestamp |
| `sent_at` | Successful delivery timestamp |

`delivery_key` has a unique constraint so rerunning enqueue logic cannot duplicate an artifact. Payloads contain `schema_version: 1`. Unknown versions remain pending with a clear error and are never sent using guessed semantics.

Each artifact is a separate row. A successful text digest and failed Word upload therefore result in retrying only the Word row. There is no automatic deletion or terminal retry count. Failed rows use exponential backoff capped at six hours.

Payload definitions:

- `text_digest`: fully rendered text plus the reporting timestamp.
- `word_document`: ordered article URLs, catch-up URL set, generated timestamp, deterministic relative output path, and stored importance results needed for reproducible rendering.
- `highlight_card`: the selected article URLs and stored level, score, and reason data.

### Atomic article and outbox persistence

Split collection from persistence. Collection returns normalized, intra-run-deduplicated candidates and collection statistics without committing them. A database transaction then:

1. inserts candidates with `INSERT OR IGNORE`;
2. determines the actual inserted set;
3. classifies freshness and catch-up eligibility using the pre-collection source baseline;
4. creates all applicable outbox records; and
5. commits articles and delivery work together.

Any failure rolls back both article inserts and outbox rows. After commit, the processor drains due pending rows in creation order. Old failed rows and current-run rows are handled by the same path.

Word rows do not depend on a pre-existing file. If the deterministic file is absent, the processor reloads articles by URL and rebuilds it. Explicit `DISABLE_FEISHU_SEND` prevents Feishu-only work from being enqueued; missing credentials or transient network failures leave already-enqueued work pending.

The delivery guarantee is at least once. A remote service that accepts a request immediately before the process crashes may receive a duplicate because the current remote APIs do not expose a usable idempotency key. The design prevents silent loss, which is the required invariant.

## Detailed Fixes

### Source baseline timing

Read counts for every configured source before collection begins. Pass this immutable baseline into delivery classification. A source with baseline zero can deliver fresh articles but cannot deliver catch-up articles. Add an orchestration-level test; unit tests of the classifier alone are insufficient.

### User-visible string recovery

Replace every literal corrupted question-mark string in `app/main.py`, `app/notifier.py`, and `app/feishu.py` with explicit Chinese text. Restore digest statistics, lock/backfill messages, highlight-card title and prefixes, and Feishu card errors. Add a targeted source scan that rejects three or more consecutive ASCII question marks in user-visible Python string literals.

### RSS failure semantics

The generic RSS collector calls `raise_for_status()` before parsing. After parsing:

- `feed.bozo` with no usable entries raises a collector error containing the parser exception;
- `feed.bozo` with usable entries logs a warning and continues; and
- a valid empty feed remains a successful zero-item collection.

This preserves tolerance for real-world imperfect feeds while ensuring HTTP error pages and unusable XML appear in `failed_sources`.

### Election-context `as_of`

Define `as_of` as an inclusive upper time bound.

- Recent events use `[as_of - recent_days, as_of]`.
- Historical search and milestone queries also receive `date_to=as_of`.
- Without an explicit `as_of`, use the current Taipei time as the reference.
- Current snapshot lookup returns the active snapshot.
- Historical snapshot lookup returns the newest active or superseded snapshot whose `as_of` is less than or equal to the requested bound. Preview and archived snapshots are excluded.

No section of a context response may contain an event or snapshot later than the effective reference time.

### Superseded events

`search_events()` excludes `fact_status='superseded'` by default. Explicit `fact_status='superseded'` or a dedicated `include_superseded=True` argument can opt in. Snapshot construction and ordinary retrieval retain the safe default. Milestone queries continue to exclude superseded events explicitly.

### Active snapshot invariant

Create a partial unique index:

```sql
CREATE UNIQUE INDEX ...
ON election_state_snapshots(election_id)
WHERE snapshot_status = 'active';
```

Before creating the index on an existing database, find elections with multiple active snapshots. Keep the newest by `as_of`, then `created_at`, then `snapshot_id`; mark all older rows `superseded`, set `superseded_by` to the retained ID, and set `superseded_at` to the migration time.

Saving a new active snapshot atomically marks the previous active snapshot superseded and inserts the new row. Non-active preview/history inserts do not alter the current active snapshot. Lookup no longer combines `LIMIT 1` with an impossible multiple-row check.

### Election Word reports and `--send-existing`

Add a dedicated election-report Word builder using `python-docx`. It renders the report title/date, overall judgment, Tainan section, New Taipei section, comparison, and an evidence summary. Structured JSON remains the authoritative evidence artifact.

Migrate `report_runs` with a `json_path` column. `word_path` stores only the actual `.docx` path and `word_sha256` stores its digest.

Normal generation:

1. saves JSON evidence;
2. generates and hashes Word;
3. records both paths;
4. sends Word when sending is enabled and credentials exist; and
5. records `sent`, `pending`, `not_sent`, or a concise failure state as appropriate.

`--send-existing` executes before the generic already-generated guard. It loads the requested report row, verifies the Word file and hash, and sends it. If Word is missing but JSON exists, it rebuilds Word from JSON, updates path/hash, then sends. It updates `feishu_status` only after the real send result. All connections close through `try/finally`, including early returns.

## Error Handling

- Notifier methods return normally only on success and otherwise raise a standardized notification exception.
- The outbox processor catches delivery exceptions, increments `attempt_count`, stores a concise error, calculates `next_attempt_at`, and continues with other due rows.
- Missing credentials, rate limits, timeouts, and connection errors remain retriable.
- A missing Word artifact is rebuilt; failure to rebuild remains pending with an actionable error.
- Invalid payload structure or an unknown schema version is not sent. It remains visible as pending with a clear diagnostic instead of being discarded.
- A source failure is isolated and added to `failed_sources`; other collectors continue.
- Database migrations and snapshot cleanup run transactionally.

## Compatibility and Non-goals

- Existing CLI commands and environment variables remain valid.
- Existing article URLs and deduplication behavior do not change.
- Existing valid election snapshots and report records remain readable after migration.
- This work does not add background workers, external queues, async code, new notification providers, article-body fetching, or LLM classification to the news collection path.
- This work does not promise exactly-once remote delivery because the remote APIs do not supply a reliable request idempotency facility.

## Verification Plan

Add regression coverage for:

1. notifier failures propagating to the outbox processor;
2. article/outbox atomicity under injected database failure;
3. failed delivery remaining pending and succeeding on a later run;
4. partial batch success retrying only the failed artifact;
5. stable delivery keys preventing duplicate enqueue;
6. pre-collection new-source baseline behavior through the main orchestration path;
7. HTTP 4xx/5xx, unusable malformed RSS, imperfect usable RSS, and valid empty RSS;
8. absence of corrupted consecutive-question-mark user strings;
9. inclusive `as_of` boundaries across recent, historical, milestone, and snapshot data;
10. default superseded exclusion and explicit opt-in;
11. migration of multiple active snapshots, enforcement of the unique index, and consecutive snapshot saves;
12. election Word rendering, hashing, normal send, `--send-existing`, and Word rebuild from JSON;
13. database connection closure on normal and early-return paths.

Final verification consists of the full pytest suite, `compileall`, and a requirement-by-requirement audit against the eight original findings. Network delivery tests use mocks and never use real credentials.
