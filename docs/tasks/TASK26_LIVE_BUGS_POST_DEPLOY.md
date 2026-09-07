# Task 26 — bugs found via real post-deploy testing

Three real bugs found and fixed while the user exercised the actual running app (via
`docker compose up`) after Task 25 merged — none caught by static review or the existing test
suite, all confirmed against real request/response behavior.

## 1. `loop1/llm.py` — missing `'choices'` key crashed with a raw KeyError

A free-tier/OpenRouter model occasionally returns HTTP 200 with a malformed or content-filtered
body that has no `choices` key at all. `data["choices"][0]["message"]` then raised a raw
`KeyError` that propagated all the way up to the user as `{"detail":"'choices'"}` instead of
being retried like the adjacent empty-content case a few lines below it already is.

Fixed by checking for a missing/empty `choices` list and raising the same `_RetryableHTTPError`
the empty-content branch uses — tenacity retries the same model up to 3 times, matching existing
behavior exactly, not a new pattern.

New regression tests in `tests/test_llm.py` (missing key, empty list, and a normal-response
sanity check) — **could not be executed in this environment**: a bare `import httpx` hangs
indefinitely in this sandbox specifically (unrelated to the fix — the file already has a comment
acknowledging this exact class of macOS SSL/Keychain issue). Verified by tracing the retry/
fallback logic by hand instead. Please run `pytest tests/test_llm.py` to confirm.

## 2. `legacy_store.py` — hardcoded `sslmode="require"` broke local Docker Postgres

`docker compose up`'s `api` service crashed with `psycopg2.OperationalError: server does not
support SSL, but SSL was required` on any route touching the legacy blob store (e.g.
`GET /api/session/{id}/report` → `_save_session_report` → `_get_pg()`).

`_parse_pg_url` hardcoded `sslmode="require"` for the raw psycopg2 connection pool the legacy
store still uses for the JSON blob table. `docker-compose.yml`'s `postgres:16-alpine` container
has no SSL configured at all, so every connection attempt failed outright.
`diffdx.db.engine` — the SQLAlchemy path everything else (migrations, doctor seeding, the
relational schema) goes through — never forced `sslmode`, which is why those all worked fine
against the same container while this separate, legacy connection pool didn't.

Changed to `sslmode="prefer"`: uses SSL when the server offers it (a managed provider like
Render's Postgres still does, unaffected), falls back to a plain connection when it doesn't
(local Docker/dev Postgres). Verified the URL-parsing logic in isolation (correct host/port/
dbname/user/password/sslmode extraction from a `postgresql://user:pass@host:port/db` string) —
full pytest run wasn't reliable in this environment at the time (see above), please confirm via
`docker compose up --build` and a real session-report request.

## 3. Docker image missing `data/disease_specialty_map.json`

`GET /api/session/{id}/routing` (Specialist routing) 500'd: `Routing engine error: Disease
specialty map not found: /app/data/disease_specialty_map.json`.

A gap in Task 23's original Dockerfile scoping — `loop3.routing.map_loader` reads this file by
path at call time (not import time), but it was never identified as a runtime-needed path when
the Dockerfile's `COPY` list was built (only `config.yaml`, `prompts/`, `test_cases/`,
`exemplars/`, `alembic/` were caught then). Grepped every `data/`-relative path across the whole
`src/` tree this time to confirm nothing else was missed — the rest of `data/` (`DDXPlus_Raw/`,
`phase7_sessions/`, `phase7_critiques/`, `ddxplus_eval_set.json`) is exclusively offline eval
tooling, confirmed not reachable from any live web route.

Fixed by adding `COPY data/disease_specialty_map.json ./data/disease_specialty_map.json` to the
Dockerfile (the file itself, 21KB — not excluded by `.dockerignore`, which only blocks the larger
offline-dataset subdirectories). Verified the JSON parses correctly and every entry has the
required `specialty`/`urgency`/`appointment_type`/`reasoning` keys `map_loader.py` validates for.

## Also found and reported, not a code bug

The user's own `loop1/.env` had `CRITIC_MODEL` accidentally set to their `OPENROUTER_API_KEY`
value (both 90 characters — a mispaste), causing the critic to send the API key as a model name:
`"The model \`sk-or-v1-...\` does not exist"`. This also meant the real key was visible in a
pasted error message during this session — user was told to rotate it immediately. Not a code
change; `CRITIC_MODEL`'s existing fallback default (`openrouter/google/gemma-4-31b-it:free`)
was already correct, the `.env` value just needed removing/correcting.

## Verification caveat

None of the three code fixes above could be executed end-to-end in this environment — a bare
`import httpx` hangs indefinitely in this sandbox (confirmed directly, unrelated to any of these
changes), which blocks both the new `test_llm.py` and, once it was added to the suite, the full
`pytest` run too. Each fix was verified as thoroughly as possible without executing that specific
import path (isolated logic tests, manual trace-through, JSON validation) — but real confirmation
needs the user's own terminal or a `docker compose up --build` rebuild, which is what surfaced all
three bugs in the first place.
