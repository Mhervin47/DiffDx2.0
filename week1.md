# DiffDx v2 — Week 1: Foundation

Spec for Claude Code. Read this file fully before writing any code.

---

## 0. Context

DiffDx is an AI diagnostic assistant (FastAPI + vanilla JS frontend) with a
three-loop architecture: an actor LLM that runs multi-turn diagnostic
conversations, a critic LLM that scores sessions, and a router that classifies
urgency and specialty. It works. The AI layer is not the problem and is mostly
out of scope this week.

The problem is the persistence, auth, and deployment layers. This week rebuilds
those without changing observable API behaviour.

**Repo:** `DiffDx-v2` (mirror of `Mhervin47/DiffDx`, full history preserved).
The original repo is tagged `v1.0-baseline` and must stay untouched — it is the
"before" half of a capstone demo.

### Current state, verified

- `loop1/web/api.py` is **3,892 lines** with **93 routes**, plus the DB layer,
  auth, business logic, and HTML routing all in one module.
- Persistence is a **single-row-per-collection JSON blob store**. Both the
  Postgres and SQLite paths create exactly one table:
  ```sql
  CREATE TABLE store (collection TEXT PRIMARY KEY, data TEXT NOT NULL)
  ```
  Eight collections live in eight rows: `users`, `doctors`, `appointments`,
  `messages`, `waitlist`, `blocked_dates`, `second_opinions`, `session_uploads`.
- Every write is read-whole-collection → mutate dict → write-whole-collection.
  No transactions, no indexes, no foreign keys.
- `_db_load` swallows all exceptions and returns the default, so a database
  outage is indistinguishable from "no data".
- Auth is `secrets.token_urlsafe(32)` into an in-memory `_TOKENS: dict[str, str]`,
  with PBKDF2-SHA256 (200k iterations) password hashing. The README claims JWT +
  bcrypt. It is not JWT. Restart = all users logged out.
- `_sessions: dict[str, APISession]` holds live diagnostic sessions in process
  memory. A restart mid-consultation loses the session.
- Uploaded files are stored base64-encoded inside the JSON blobs.
- No Dockerfile, no `.github/`, no `.env.example` (the README instructs
  `cp .env.example .env`, which fails).
- `render.yaml` pins `PYTHON_VERSION=3.11.0`; README says Python 3.12.

---

## 1. Hard constraints

1. **Do not break the frontend.** `loop1/web/static/*.html` calls these routes
   directly. Request and response JSON shapes must stay identical unless a task
   below explicitly says otherwise. Frontend changes this week are limited to
   auth token handling in `auth.js`.
2. **One task per branch, one PR per task.** Branch names given per task. Do not
   batch tasks into one commit.
3. **Tests must pass at every commit.** `pytest tests/ -v` is green before and
   after. Existing tests are the regression net — do not delete a test to make
   it pass; if a test encodes blob-store behaviour, rewrite it against the new
   layer and say so in the PR body.
4. **No secrets in git.** `.env` and `.env.*` are already gitignored and no env
   file has ever been tracked. Keep it that way.
5. **No new portal features.** The app has ~30 already. This week is
   infrastructure only.
6. **Ask before destructive migrations.** Do not drop the `store` table. Leave
   it in place until Task 7 confirms migration succeeded.
7. **commit only in mhervin's name** 

---

## 2. Task 1 — Relational schema + Alembic

**Branch:** `feat/relational-schema`

Replace the blob store with a real schema. This is the flagship change; do it
first and do it carefully.

### Stack

SQLAlchemy 2.0 (typed `Mapped[]` declarative style, not the legacy `Column`
API), Alembic for migrations, `psycopg[binary]` 3.x for Postgres. Async is
**not** required — the codebase is sync and mixing styles is worse than staying
sync. Use `Session` and keep it simple.

### Layout

```
loop1/src/diffdx/db/
  __init__.py
  base.py          # declarative base, naming convention for constraints
  engine.py        # engine + sessionmaker, DATABASE_URL parsing
  models/
    __init__.py
    user.py        # User, Patient, Doctor, Dependent
    scheduling.py  # Appointment, DoctorSlot, BlockedDate, Waitlist
    clinical.py    # DiagnosticSession, SessionTurn, SuggestedTest, Prescription
    messaging.py   # MessageThread, Message
    files.py       # UploadedFile
    audit.py       # AuditLogEntry
loop1/alembic/
  env.py
  versions/
```

### Schema requirements

Derive columns from the actual record shapes in `web/api.py` — the user record
around line 1018, the appointment record around line 1663, the doctor seed
around line 805. Read those before designing tables.

Non-negotiable properties:

- **UUID primary keys** (`uuid.UUID` typed, PostgreSQL `UUID` / SQLite
  `CHAR(36)`). Keep existing IDs during migration so URLs and foreign references
  stay valid.
- **`User` is the single identity table** with a `role` enum
  (`patient` / `doctor` / `admin`). Doctors currently live inside the users
  collection with a `doctor_id` field; model `Doctor` as a table with a
  one-to-one FK to `User`, same for `Patient`.
- **`UNIQUE (doctor_id, slot_datetime)` on `Appointment`**, partial to exclude
  cancelled rows. This is the constraint that makes double-booking impossible at
  the database level rather than hopefully-impossible in Python.
- **Real foreign keys with explicit `ondelete`.** Appointments cascade from
  patient deletion; audit log entries never cascade (`ON DELETE RESTRICT`).
- **Timezone-aware timestamps** (`DateTime(timezone=True)`), `created_at` and
  `updated_at` on every table, server-side defaults.
- **Indexes** on every column that appears in a `WHERE` or `ORDER BY` in the
  current code: `users.email` (unique, case-insensitive — `citext` on Postgres
  or a `lower()` functional index), `appointments.patient_id`,
  `appointments.doctor_id`, `appointments.status`, `appointments.slot_datetime`,
  `messages.thread_id`, `sessions.patient_id`.
- **Enums as native DB enums** on Postgres for `role`, `appointment_status`,
  `urgency_tier`.
- **No JSON columns except where the data is genuinely schemaless.** The AI
  differential (`[{"dx": ..., "prob": ...}]`) is fine as JSONB. Appointments,
  users, and messages are not.

### Alembic

- `alembic.ini` at `loop1/`, migrations in `loop1/alembic/versions/`.
- `env.py` reads `DATABASE_URL` from the environment, falling back to the
  SQLite dev path — do not hardcode a URL.
- Set a `naming_convention` on the declarative base so autogenerated constraint
  names are deterministic. Without it Alembic produces unnamed constraints that
  cannot be dropped cleanly later.
- One initial migration creating the full schema. Verify it is reversible:
  `alembic upgrade head && alembic downgrade base && alembic upgrade head`.
- Both SQLite and Postgres must work. Where they diverge (JSONB, native enums,
  citext), branch on `dialect.name` inside the migration.

### Acceptance

- [ ] `alembic upgrade head` succeeds on an empty Postgres 16 and on SQLite
- [ ] `alembic downgrade base` succeeds cleanly on both
- [ ] `alembic revision --autogenerate` produces an **empty** diff against a
      freshly upgraded DB (proves models and migration agree)
- [ ] Every FK, unique constraint, and index listed above exists — verify with
      `\d+ tablename` on Postgres, not by reading the model file

---

## 3. Task 2 — Repository layer + data migration

**Branch:** `feat/repository-layer`

### Repositories

`loop1/src/diffdx/repositories/` — one module per aggregate
(`users.py`, `appointments.py`, `messaging.py`, `sessions.py`, `files.py`).
Plain classes taking a `Session` in the constructor. No ORM objects leak past
this layer; return Pydantic models or dataclasses.

Route handlers must never import from `db.models` directly. If a router imports
a SQLAlchemy model, the layering is wrong.

### Fixing the swallowed-exception bug

`_db_load` currently returns the default on any exception. The replacement must
distinguish three cases:

- Row not found → return `None` or empty list, caller decides
- Connection / operational failure → raise, becomes a 503
- Integrity violation → raise a typed domain exception the router maps to 409

Add a FastAPI exception handler mapping `IntegrityError` on the appointment
unique constraint to `409 Conflict` with a JSON body the frontend can display.

### Migration script

`loop1/scripts/migrate_blob_to_relational.py`

- Reads all 8 collections from the existing `store` table
- Writes them into the new schema, **preserving all existing UUIDs**
- Extracts base64 file payloads out of the blobs and writes them to disk under
  `loop1/web/data/files/` for now (S3 comes in Week 2), storing only a path in
  `UploadedFile`
- Idempotent — running twice must not duplicate rows
- `--dry-run` flag reporting counts per table without writing
- Prints a reconciliation summary: rows read per collection vs rows written per
  table, exits non-zero on any mismatch

Take a `pg_dump` of the Supabase database before running this against anything
real. Do not point v2 at the v1 Supabase project — provision a separate
database. The v1 deployment must keep working for the demo.

### Acceptance

- [ ] `--dry-run` reports non-zero counts for every populated collection
- [ ] Real run completes with zero reconciliation mismatches
- [ ] Running it a second time reports "0 new rows" and changes nothing
- [ ] Spot-check: pick 3 appointments from the blob JSON, confirm each exists in
      `appointments` with identical `id`, `slot`, `status`, `doctor_id`
- [ ] A DB outage now produces 503, not a silent empty list — test by pointing
      `DATABASE_URL` at a dead port

---

## 4. Task 3 — Concurrency proof

**Branch:** `feat/concurrency-test`

This is a demo artifact as much as a test. Build it deliberately.

`loop1/scripts/concurrency_demo.py`

- Fires N concurrent `POST /api/session/{id}/book` requests for the **same
  doctor and same slot** (default N=20, `--n` to override)
- `--mode blob` runs against the legacy blob code path; `--mode relational`
  against the new one
- Prints a table: requests sent, HTTP 200s, HTTP 409s, and — critically —
  **rows actually in the database afterward**

Expected output:

| mode | sent | 200 | 409 | rows in DB |
|---|---|---|---|---|
| blob | 20 | 20 | 0 | 1 (19 lost) |
| relational | 20 | 1 | 19 | 1 |

The blob row shows lost-update corruption: 20 clients were told "booked", one
booking survived. Capture this output verbatim — it goes on a slide.

Also add `tests/test_concurrency.py` running the relational case with N=10
against a real Postgres (skip if unavailable), asserting exactly one success.

### Acceptance

- [ ] Both modes run and produce the table
- [ ] Relational mode: exactly 1 success, N-1 clean 409s, no 500s
- [ ] Output saved to `docs/evidence/concurrency.txt` and committed

---

## 5. Task 4 — Split the monolith

**Branch:** `feat/split-routers`

3,892 lines in one file is the first thing any reviewer notices.

### Target layout

```
loop1/src/diffdx/
  main.py                 # app factory, middleware, exception handlers, lifespan
  config.py               # pydantic-settings, all 15 env vars, typed + validated
  dependencies.py         # get_db, get_current_user, require_role
  routers/
    auth.py               # /api/auth/*
    sessions.py           # /api/session/*, /api/cases/*
    appointments.py       # /api/appointments, /api/patient/appointments/*
    doctors.py            # /api/doctor/*, /api/doctors/*
    messaging.py          # /api/messages/*
    files.py              # upload/download routes
    pages.py              # HTML-serving routes
    health.py             # /health, /ready
  services/               # business logic, no FastAPI imports
  schemas/                # Pydantic request/response models
```

### Rules

- **No file over 400 lines.** If a router exceeds it, the service layer is too
  thin.
- `services/` must not import `fastapi`. It raises domain exceptions; routers
  translate them to HTTP.
- Replace `@app.on_event("startup")` with a `lifespan` context manager — the
  decorator is deprecated.
- Move doctor seeding out of startup into `scripts/seed_doctors.py`. Seeding on
  every boot is a race in a multi-replica deployment.
- Delete `os.environ.setdefault("CRITIC_MODEL", ...)` at import time; that
  belongs in `config.py` as a typed default.
- Remove the `sys.path.insert` hack. Fix packaging in `pyproject.toml` instead.

### Config

`config.py` must enumerate **all 15** environment variables the code actually
reads, with types and required/optional status:

```
GROQ_API_KEY          required
DATABASE_URL          optional (SQLite fallback)
SECRET_KEY            required in prod, generated in dev
OPENROUTER_API_KEY    optional — critic disabled if unset
CRITIC_MODEL          optional, has default
RESEND_API_KEY        optional
RESEND_FROM           optional
CEREBRAS_API_KEY      optional
GEMINI_API_KEY        optional
ELEVENLABS_API_KEY    optional
SARVAM_API_KEY        optional
SMTP_HOST / SMTP_PORT / SMTP_USER / SMTP_PASS / SMTP_FROM   optional
```

Fail fast at startup if a required var is missing, with a message naming it.
Log which optional integrations are disabled — right now they silently no-op.

Write `loop1/.env.example` documenting all of them with one-line descriptions.
The README references this file and it does not exist.

### Route inventory

Before starting, dump the current route table:

```bash
python -c "from web.api import app; [print(f'{list(r.methods)[0]:7} {r.path}') for r in app.routes if hasattr(r,'methods')]" | sort > /tmp/routes_before.txt
```

Repeat after the split and `diff`. **The diff must be empty.** Any route that
moves, changes method, or disappears is a frontend break.

### Acceptance

- [ ] `diff /tmp/routes_before.txt /tmp/routes_after.txt` is empty
- [ ] No file in `src/diffdx/` exceeds 400 lines
- [ ] `grep -r "import fastapi\|from fastapi" src/diffdx/services/` returns nothing
- [ ] All existing tests pass unmodified
- [ ] Manual smoke: register → login → start session → 3 turns → view report →
      book appointment → doctor sees it. Every page loads.

---

## 6. Task 5 — Real JWT auth + RBAC

**Branch:** `feat/jwt-auth`

### What's wrong now

In-memory `_TOKENS` dict. Opaque random tokens, not JWTs. Lost on restart.
Breaks entirely with two replicas. README claims something the code doesn't do.

### Requirements

- **JWT** via `pyjwt` (not `python-jose` — poorly maintained). HS256 signed with
  `SECRET_KEY`. Refuse to start in production if `SECRET_KEY` is unset; the
  current auto-generate-on-boot behaviour silently invalidates every session on
  every deploy.
- **Access token** 15 min, **refresh token** 7 days stored hashed in a
  `refresh_tokens` table so it can be revoked. Rotate refresh tokens on use.
- Claims: `sub` (user id), `role`, `exp`, `iat`, `jti`.
- **Keep PBKDF2-SHA256 at 200k iterations** for passwords. It is a legitimate
  choice and rehashing every existing password is unnecessary risk. Update the
  README to say PBKDF2, not bcrypt — make the docs match the code rather than
  the reverse.
- **RBAC** as a FastAPI dependency: `require_role("doctor")`. Audit all 93
  routes; several `/api/doctor/*` endpoints need checking for whether they
  verify the caller is *that* doctor, not just any doctor. Fix any IDOR found
  and note it in the PR body — "I found and fixed an authorization bug" is worth
  writing down.
- **Rate limit** `/api/auth/login` and `/api/auth/register` — `slowapi`,
  5 attempts per minute per IP.
- **Tighten CORS.** `allow_origins=["*"]` on a health app handling patient data
  is indefensible. Read allowed origins from config.
- **Audit log**: write an `AuditLogEntry` on every login, failed login, PHI read,
  and appointment mutation. Fields: actor user id, action, resource type,
  resource id, IP, timestamp. Append-only — no update or delete paths.

### Frontend

`auth.js` stores a single token in localStorage. Update to hold access +
refresh, and add a fetch wrapper that transparently refreshes on 401 and retries
once. This is the only frontend change permitted this week.

### Acceptance

- [ ] Restarting the server does not log users out
- [ ] Expired access token + valid refresh → transparent renewal, user sees nothing
- [ ] Revoked refresh token → 401, and cannot be reused
- [ ] Patient token on any `/api/doctor/*` route → 403
- [ ] Doctor A cannot read Doctor B's appointments
- [ ] 6th login attempt in a minute → 429
- [ ] `tests/test_auth.py` covers all of the above

---

## 7. Task 6 — Redis session state

**Branch:** `feat/redis-sessions`

`_sessions: dict[str, APISession]` blocks horizontal scaling and loses live
consultations on restart. Both matter for Week 2's two-replica ECS deployment.

- `redis-py` with a connection pool, `REDIS_URL` in config
- Serialize `APISession` to JSON (add `to_dict`/`from_dict`, or convert to
  Pydantic if the class allows it cleanly). Do **not** pickle — version-fragile
  and a deserialization risk.
- Key `diffdx:session:{session_id}`, TTL 2 hours, refreshed on each turn
- Graceful degradation: if `REDIS_URL` is unset, fall back to the in-memory dict
  and log a warning at startup. Local dev without Redis must still work.
- Completed sessions persist to Postgres (`DiagnosticSession` + `SessionTurn`);
  Redis holds only in-flight state.

### Acceptance

- [ ] Start a session, restart the API mid-conversation, continue — it resumes
- [ ] Two API instances behind a round-robin proxy share session state
- [ ] `REDIS_URL` unset → app boots, warns, works in-memory
- [ ] Redis dies mid-session → 503, not a 500 stack trace

---

## 8. Task 7 — Docker + compose

**Branch:** `feat/docker`

### Dockerfile

`loop1/Dockerfile`, multi-stage:

- Builder installs into a venv; runtime copies the venv only
- **Use `requirements-web.txt`, not `requirements.txt`.** The latter pulls torch
  plus the full nvidia CUDA stack on Linux — multiple GB for a web server that
  doesn't run local inference. The split exists but isn't documented; document
  it in a Dockerfile comment.
- `python:3.12-slim` base. Reconcile the version drift: README says 3.12,
  `render.yaml` pins 3.11.0. Pick 3.12 and update `render.yaml`.
- Non-root user (`appuser`, uid 1000). `USER appuser` before `CMD`.
- `HEALTHCHECK` hitting `/health`
- `.dockerignore`: `.git`, `logs/`, `web/data/`, `*.db`, `.env`, `__pycache__`,
  `tests/`, `data/DDXPlus_Raw/`
- Target under 400 MB final image. Report the actual size in the PR.

### docker-compose.yml

At repo root. Services: `api`, `db` (postgres:16-alpine), `redis` (redis:7-alpine).

- Named volumes for Postgres and Redis
- `depends_on` with `condition: service_healthy`
- `.env` for local secrets, referenced via `env_file`
- API entrypoint runs `alembic upgrade head` before starting uvicorn
- Ports: api 8000, db 5432, redis 6379

`docker compose up` on a clean checkout must produce a working app at
`localhost:8000` with migrations applied and doctors seeded. This is how a
reviewer will try to run the project — it needs to work first time.

### Acceptance

- [ ] `docker compose up --build` → working app, no manual steps
- [ ] `docker compose down -v && docker compose up` → clean rebuild works
- [ ] `docker run --rm diffdx whoami` prints `appuser`
- [ ] Image under 400 MB
- [ ] `trivy image diffdx` reports no HIGH or CRITICAL in your layers

---

## 9. Task 8 — Documentation

**Branch:** `docs/week1`

- Rewrite `README.md`: correct the JWT/bcrypt claim, correct the Python version,
  add the Docker quick start, document the `requirements.txt` vs
  `requirements-web.txt` split
- `docs/ARCHITECTURE.md`: new component diagram, request lifecycle, why the blob
  store was replaced, why sessions moved to Redis
- `docs/DECISIONS.md`: short ADRs for the choices with real tradeoffs — sync
  SQLAlchemy over async, PBKDF2 retained over bcrypt migration, Redis sidecar
  over ElastiCache, public-subnet Fargate over NAT gateway. Each: context,
  decision, consequences. Three paragraphs each, no more.
- Regenerate `architecture.svg`

---

## 10. Order and checkpoints

Tasks are sequenced by dependency. Do not reorder.

| # | Task | Depends on |
|---|---|---|
| 1 | Relational schema + Alembic | — |
| 2 | Repository layer + migration | 1 |
| 3 | Concurrency proof | 2 |
| 4 | Split the monolith | 2 |
| 5 | JWT auth + RBAC | 4 |
| 6 | Redis sessions | 4 |
| 7 | Docker + compose | 5, 6 |
| 8 | Documentation | all |

**Stop and report after Task 3.** The concurrency numbers determine whether the
schema work delivered what it was supposed to. If relational mode doesn't
produce exactly one success, the constraint is wrong and everything downstream
inherits the flaw.

## 11. Definition of done for Week 1

- [ ] `docker compose up` on a clean clone → working app, zero manual steps
- [ ] Blob store no longer read or written by any code path
- [ ] Route diff against `v1.0-baseline` is empty
- [ ] `pytest tests/ -v` green; new tests for auth, concurrency, repositories
- [ ] Sessions survive restart; two replicas share state
- [ ] `docs/evidence/concurrency.txt` committed
- [ ] `.env.example` documents all 15 variables
- [ ] README claims match the code
- [ ] Eight branches merged via eight PRs

## 12. When to stop and ask

- A test can only pass by deleting it or weakening its assertion
- The frontend needs changes beyond `auth.js` token handling
- A migration would drop or overwrite data
- The route diff is non-empty and the fix isn't obvious
- Anything requires touching the original `DiffDx` repo

Report after each task: what changed, what the acceptance checks returned, what
you're uncertain about. Do not report a task complete with a failing check —
report it failing and explain why.