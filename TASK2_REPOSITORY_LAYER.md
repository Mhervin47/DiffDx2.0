# DiffDx v2 — Task 2: Repository layer + data migration

Spec for whoever picks this up next. Read fully before writing code.
Branch: `feat/repository-layer` (based off `feat/relational-schema`, since
this task requires Task 1's schema to exist).

---

## 0. Where this picks up

Task 1 (relational schema + Alembic) is done, on branch
`feat/relational-schema`, committed locally (not yet pushed). It added a new
package `loop1/src/diffdx/db/` with SQLAlchemy 2.0 models and one Alembic
migration. **Nothing in `web/api.py` touches this new schema yet** — the
running app still reads/writes the old single-table JSON blob store
(`web/api.py` lines ~388-536, functions `_db_load`/`_db_save`). Task 2 is
where that changes.

Before starting: `git checkout feat/relational-schema`, then branch off it
for `feat/repository-layer`. Run `alembic upgrade head` (see §6) to get a
local DB with the new schema before writing any repository code.

**Known gap from Task 1, not yet fully closed:** the migration was verified
end-to-end (upgrade → downgrade → upgrade → empty autogenerate diff) on
SQLite, and `upgrade head` was separately verified to succeed on real
Postgres 16. The Postgres downgrade/re-upgrade cycle was blocked mid-session
by unrelated iCloud file-sync interference on the dev machine and never got
a clean run recorded. If you hit `type "X" already exists` on a fresh
Postgres re-upgrade, check that `downgrade()` in
`loop1/alembic/versions/c9231e860c45_initial_schema.py` actually runs (it
should — it explicitly drops all 10 enum types on Postgres) rather than
assuming the schema is wrong.

---

## 1. What the old blob store actually looks like

The `store` table is `(collection TEXT PRIMARY KEY, data TEXT NOT NULL)`,
one row per collection, JSON-encoded. Collections used today:

| Collection key | Shape | Loader in `web/api.py` |
|---|---|---|
| `users` | `dict[user_id, user_record]` | `_load_users` / `_save_users` (~L617) |
| `doctors` | `list[doctor_record]` | `_load_doctors` / `_save_doctors` (~L699) |
| `appointments` | `dict[appt_id, appt_record]` | `_load_appointments` / `_save_appointments` (~L707) |
| `session_uploads` | `dict[session_id, dict[upload_id, upload]]` | `_load_session_uploads` / `_save_session_uploads` (~L715) |
| `waitlist` | `list[entry]` | `_load_waitlist` / `_save_waitlist` (~L3292) |
| `blocked_dates` | `dict[doctor_id, list[{date, reason}]]` | `_load_blocked_dates` / `_save_blocked_dates` (~L3300) |
| `second_opinions` | `list[opinion]` | `_load_second_opinions` / `_save_second_opinions` (~L3308) |
| `messages` | `list[message]` | `_load_messages` / `_save_messages` (~L3316) |
| `session_report:{session_id}` | one report dict per key (dynamic key, not a fixed collection) | `_save_session_report` / `_load_session_report_from_db` (~L724) |
| `file:{appt_id}:{filename}` | `{"data_b64": str}` (dynamic key) | `_save_file_data` / `_load_file_data` (~L733) |

Read these functions directly before writing the migration script — don't
trust this table blindly, it's a summary, the code around each is the
ground truth. In particular:

- `users` records don't all have a `role` key — patients rely on
  `.get("role", "patient")` defaulting. Doctors have `role: "doctor"`.
- Auth tokens live **inside** each user record as `user["tokens"]: list[str]`
  (opaque bearer tokens, not JWTs yet — that's Task 5). Don't lose these on
  migration or every logged-in user gets kicked out.
- `dependents` is a nested list inside the user record
  (`user["dependents"]`), same for `sessions` (`user["sessions"]` — session
  *summaries*, not full `DiagnosticSession` rows).
- Full diagnostic session data (turn-by-turn) lives in
  `logs/final_records/final_{uuid}.json` on disk and/or the
  `session_report:{id}` DB key — **not** in any of the 8 named collections.
  You'll need both sources to backfill `DiagnosticSession`/`SessionTurn`.
- Appointments accumulate many optional sub-fields over their lifetime:
  `patient_files`, `suggested_test_uploads`, `second_opinion`, and (from
  other endpoints not all read during this pass) test orders, prescriptions,
  referrals, treatment plan, rating. Task 1's schema has tables for most of
  these (`suggested_tests`, `prescriptions`, `referrals`, `treatment_plan_items`,
  rating columns directly on `appointments`) — grep `web/api.py` for where
  each gets written onto an appointment dict before assuming the shape.

## 2. The new schema (from Task 1) — what maps to what

Package: `loop1/src/diffdx/db/models/`. Full table list, 20 tables:

- **`user.py`**: `User` (identity + `role` enum), `Patient` (1:1, `user_id` PK),
  `Doctor` (1:1, `user_id` PK, plus legacy `doctor_id` string like `dr_001`
  kept as a separate unique column), `Dependent`, `RefreshToken` (added
  ahead of Task 5, empty for now).
- **`scheduling.py`**: `Appointment` (UUID PK, `patient_id`/`doctor_id` FK to
  `patients.user_id`/`doctors.user_id`, partial-unique
  `(doctor_id, slot_datetime)` excluding cancelled), `DoctorSlot`,
  `BlockedDate`, `Waitlist`.
- **`clinical.py`**: `DiagnosticSession` (PK **is** the AI session_id, not
  DB-generated), `SessionTurn`, `SuggestedTest` (merges the old
  TestOrderItem + TestResultItem — a result attaches to an existing test
  row by id), `Prescription`, `Referral`, `SecondOpinion`,
  `TreatmentPlanItem`.
- **`messaging.py`**: `MessageThread` (real row per patient+doctor pair —
  the old code derived this grouping at read time from a flat list),
  `Message`.
- **`files.py`**: `UploadedFile` — **path only**, never base64 in the DB.
- **`audit.py`**: `AuditLogEntry` — append-only, `ON DELETE RESTRICT` on the
  actor FK (empty until Task 5 writes to it).

`GUID` (`diffdx/db/types.py`) is native `UUID` on Postgres, `CHAR(36)` on
SQLite. Genuinely-schemaless AI output (`final_differential`,
`doctor_output`, `closing_turn`) uses a small `_JSONB` type in `clinical.py`
— JSONB on Postgres, plain JSON on SQLite. Everything else is real typed
columns.

## 3. Repository layer

`loop1/src/diffdx/repositories/` — one module per aggregate:
`users.py`, `appointments.py`, `messaging.py`, `sessions.py`, `files.py`.

Rules (non-negotiable, from the original spec):

- Plain classes taking a `Session` (SQLAlchemy) in the constructor.
- **No ORM objects leak past this layer.** Return Pydantic models or
  dataclasses, not `db.models.*` instances.
- **Route handlers must never import from `diffdx.db.models` directly.** If
  a router imports a SQLAlchemy model, the layering is wrong. (Nothing
  wires repositories into `web/api.py` yet in this task — that's implied by
  Task 4's router split — but write the layer as if it will be, so the
  interface is clean when that happens.)

### Fixing the swallowed-exception bug

`_db_load` today returns the default value on *any* exception — a DB outage
looks identical to "no data exists". The repository layer must distinguish:

- Row not found → return `None` / empty list, caller decides what that means
- Connection / operational failure → raise, let it become a 503
- Integrity violation (e.g. double-booking the same slot) → raise a typed
  domain exception that a FastAPI exception handler maps to 409

Add a FastAPI exception handler mapping `IntegrityError` on the appointment
unique constraint to `409 Conflict` with a JSON body the frontend can
actually display (not a raw stack trace).

## 4. Migration script

`loop1/scripts/migrate_blob_to_relational.py`

- Reads all collections listed in §1 from the existing `store` table (plus
  the dynamic `session_report:{id}` / `file:{appt_id}:{filename}` keys —
  you'll need to scan `store` for keys matching those prefixes, there's no
  fixed list of them)
- Writes into the new schema, **preserving all existing UUIDs** — patient
  and doctor `user_id`s, appointment ids, etc. Anything that changes id
  breaks existing URLs/bookmarks/FKs on the frontend.
- Extracts base64 file payloads out of the blobs and writes them to disk
  under `loop1/web/data/files/`, storing only the path in `UploadedFile`
  (S3 is explicitly out of scope — that's Week 2)
- **Idempotent** — running it twice must not duplicate rows. Upsert on the
  preserved id, or check-then-skip.
- `--dry-run` flag that reports counts per table without writing anything
- Prints a reconciliation summary: rows read per collection vs. rows
  written per table, and **exits non-zero on any mismatch**

**Before running this against anything real:** take a `pg_dump` of the
Supabase database. Do not point v2 at the v1 Supabase project — provision a
separate database for v2. The v1 deployment must keep working through this;
it's still what's live in production and it's the "before" half of a
capstone demo (see `week1.md` §0 if you have access to it — the original
`DiffDx` repo, not this `DiffDx2.0` mirror, must stay untouched throughout).

## 5. Acceptance checklist

- [ ] `--dry-run` reports non-zero counts for every populated collection
- [ ] A real run completes with zero reconciliation mismatches
- [ ] Running it a second time reports "0 new rows" and changes nothing
- [ ] Spot-check: pick 3 appointments from the old blob JSON, confirm each
      exists in the new `appointments` table with identical `id`, slot,
      status, `doctor_id`
- [ ] A DB outage now produces a 503, not a silently-empty list — test by
      pointing `DATABASE_URL` at a dead port and hitting a repository method

## 6. Running things locally

```bash
cd loop1
uv sync -p 3.12                      # installs sqlalchemy/alembic/psycopg from Task 1
PYTHONPATH=src:. .venv/bin/python -m alembic -c alembic.ini upgrade head
```

`DATABASE_URL` unset → falls back to local SQLite at `web/data/diffdx.db`
(gitignored, safe to delete and re-migrate freely). Set it to a Postgres URL
to test against real Postgres — `resolve_database_url()` in
`diffdx/db/engine.py` normalizes `postgres://`/`postgresql://` to the
`postgresql+psycopg://` driver SQLAlchemy needs for psycopg 3.

## 7. When to stop and ask

Same ground rules as Task 1:

- A test can only pass by deleting it or weakening its assertion
- The migration would drop or overwrite data
- Reconciliation counts don't match and the cause isn't obvious
- Anything requires touching the original `DiffDx` repo (not this mirror)

Report what changed, what the acceptance checks returned, and what you're
uncertain about — including a failing check, explicitly, rather than
reporting the task done with something quietly broken.
