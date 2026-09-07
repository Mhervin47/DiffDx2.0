# DiffDx v2 — Task 3: Concurrency proof

Spec for whoever picks this up next. Read fully before writing code.
Branch: `feat/concurrency-test`, based off `feat/repository-layer`.

**This is a checkpoint task, not just another feature.** Per the plan's
sequencing (`week1.md` §10): *"Stop and report after Task 3. The
concurrency numbers determine whether the schema work delivered what it
was supposed to. If relational mode doesn't produce exactly one success,
the constraint is wrong and everything downstream inherits the flaw."*
Don't move on to Task 4 until this genuinely passes.

---

## 0. Where this picks up

- **Task 1** (relational schema + Alembic) — done, merged to `main`.
- **Task 2** (repository layer) — partially done, merged to `main` as WIP.
  The repository layer exists (`diffdx/repositories/{users,appointments,
  sessions,messaging,files}.py`) but **as of this writing has never been
  successfully import-tested** — every attempt on the original dev machine
  hung on unrelated iCloud file-sync interference. First thing to do here:
  confirm `from diffdx.repositories.appointments import AppointmentRepository`
  actually imports and works before building this task on top of it.
  **`migrate_blob_to_relational.py` does not exist yet** — Task 2 isn't
  fully finished. See `TASK2_REPOSITORY_LAYER.md` §4 if that's still
  outstanding when you get here.

## 1. The goal, precisely

Prove — with real numbers, not just code review — that the relational
schema's `UNIQUE (doctor_id, slot_datetime)` constraint (partial index,
excludes cancelled rows; see `loop1/src/diffdx/db/models/scheduling.py`,
`Appointment.__table_args__`) actually prevents the lost-update bug the old
blob store has. The blob store does read-whole-collection → mutate dict →
write-whole-collection with no locking — under concurrent requests, N
clients can all read "slot is free," all write "booked," and only the last
write wins. Every earlier client was told 200 OK. That's a real,
demonstrable data-corruption bug, and this task's whole point is to put a
number on it and then prove the new schema doesn't have it.

## 2. An important nuance the original spec doesn't spell out

The original task description assumes you can fire concurrent requests at
`POST /api/session/{id}/book` in **both** modes. That's true for `--mode
blob` — that endpoint (`web/api.py` line ~1595) is live today and still
reads/writes the old blob store exactly as described.

It is **not** true for `--mode relational` yet, because **Task 4 (split the
monolith) hasn't happened** — nothing in `web/api.py` routes through
`diffdx.repositories` yet, and Task 4 depends on Task 2, and this task
(Task 3) is sequenced *before* Task 4. So there is no live HTTP endpoint
that exercises the new schema's booking path.

Two ways to handle this, pick one and say which in the PR:

- **(Recommended) Exercise the repository layer directly.** `--mode
  relational` opens N concurrent SQLAlchemy sessions (one per thread, from
  `diffdx.db.engine.get_sessionmaker()` — sessions are not thread-safe to
  share) and calls `AppointmentRepository.book()` on each with the same
  `doctor_id`/`slot_datetime`, different `patient_id` per thread. This
  still proves the thing that matters — the DB constraint, not the HTTP
  layer — and doesn't require prematurely wiring routers before Task 4.
- **(Alternative, more work)** Stand up a minimal temporary FastAPI app in
  the script itself with just a booking route backed by the repository
  layer, and hit that over real HTTP with a thread pool. More faithful to
  "fires N concurrent POST requests" but is throwaway scaffolding you'd
  delete once Task 4 exists for real. Not recommended unless the HTTP-level
  behavior specifically (status codes, response bodies) needs proving too.

Either way, `--mode blob` should hit the real, live `/api/session/{id}/book`
endpoint over actual HTTP, since that code path exists today unmodified.

## 3. What to build

`loop1/scripts/concurrency_demo.py`

- Fires N concurrent booking attempts for the **same doctor and same slot**
  (default `N=20`, `--n` to override)
- `--mode blob` → real concurrent HTTP requests against a running
  `web/api.py` instance (you'll need to start it, register N patient
  accounts or reuse test fixtures, then fire the requests via a thread
  pool + `httpx`)
- `--mode relational` → concurrent calls into `AppointmentRepository.book()`
  per §2 above (thread pool, one DB session per thread)
- Prints a table: requests sent, HTTP 200s (or successful books), HTTP
  409s (or `ConflictError`s), and — critically — **rows actually present
  in the database afterward** (query the DB directly after the dust
  settles, don't trust the response codes alone — that's the whole point,
  the blob store's response codes lie)

Expected output:

| mode | sent | success | conflict | rows in DB |
|---|---|---|---|---|
| blob | 20 | 20 | 0 | 1 (19 lost) |
| relational | 20 | 1 | 19 | 1 |

The blob row is the smoking gun: 20 clients were each told "booked," but
only one booking actually survived. That's silent data loss with no error
raised to anyone. Capture this output verbatim — the plan explicitly wants
it for a slide.

Also add `tests/test_concurrency.py` running the relational case with
`N=10` against a real Postgres (skip cleanly if `DATABASE_URL` isn't set or
Postgres isn't reachable — don't fail the whole suite over an optional
integration dependency), asserting **exactly one** success and **N-1**
clean conflicts, zero unhandled exceptions.

## 4. A few implementation notes worth knowing going in

- The unique index is partial (`WHERE status != 'cancelled'`), so make sure
  your N concurrent bookings are all against appointments that would
  actually be `status='upcoming'` by default — don't accidentally create
  them pre-cancelled or the constraint won't even engage and you'll get a
  false pass.
- `AppointmentRepository.book()` already catches `IntegrityError` and
  re-raises `ConflictError` (see `diffdx/repositories/appointments.py`) —
  that's the exception your relational-mode thread pool should be catching
  per-thread, not letting crash the whole script.
- SQLite's default single-writer behavior means concurrent SQLite writes
  serialize anyway, which could mask real concurrency bugs the constraint
  is meant to catch under Postgres's MVCC. **Run the relational-mode proof
  against real Postgres, not SQLite** — SQLite passing proves much less
  here. (Blob mode is dialect-irrelevant since it doesn't use the new
  schema at all.)
- Thread pool, not `asyncio` — SQLAlchemy's sync `Session` (which this
  whole stack uses per Task 1's explicit "sync is required, don't mix
  styles" decision) blocks per-call; real OS threads give you the genuine
  race condition you're trying to prove happens. `asyncio` with a sync
  session wouldn't actually run requests concurrently against the DB.

## 5. Acceptance checklist

- [ ] Both modes run and produce the table above
- [ ] Relational mode: exactly 1 success, N-1 clean conflicts, **zero
      500s / unhandled exceptions** — a conflict must always come back as a
      handled `ConflictError` (or 409, if you went the HTTP route), never a
      crash
- [ ] Output saved to `docs/evidence/concurrency.txt` and committed

## 6. When to stop and ask

Same ground rules as Tasks 1 and 2:

- Relational mode does *not* produce exactly one success — this means the
  constraint from Task 1 is wrong, not that the demo script is wrong. Stop
  and report; do not "fix" the test to make it pass.
- A test can only pass by deleting it or weakening its assertion
- Anything requires touching the original `DiffDx` repo (not this mirror)

Report the actual table output, not just pass/fail — the numbers are the
deliverable here as much as the code is.
