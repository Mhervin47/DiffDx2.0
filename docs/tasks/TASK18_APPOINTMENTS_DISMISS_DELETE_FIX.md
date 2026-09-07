# DiffDx v2 — Task 18: appointments cutover, dismiss-route deletion semantics (real bug fix)

Branch: `fix/appointments-dismiss-relational-delete`. Closes an open question flagged since Task
11 — and in doing so, found and fixed a real correctness bug live on `main`, not a style choice.

## 1. The bug

`patient_dismiss_appointment`/`doctor_dismiss_appointment` (`appointments4.py`) `del` the blob
record entirely — "permanently remove." Task 11's provisional lean was that leaving the
relational shadow row intact after a dismiss was the "safer default." Verified live before
writing any code, and that lean was wrong: `get_doctor_appointment_detail` (Task 14's read-route
flip) does a relational-first lookup with **no check that the blob still has the entry** — if the
relational row survives a dismiss, that route serves it via `200 OK` with full data. Reproduced
on `main`: booked → cancelled → dismissed an appointment, then fetched it directly by id — got a
full `200` response, after the UI had already told the user it was permanently gone. Neither Task
11 nor Task 14 was wrong in isolation; the combination was.

**Decision**: dismiss must delete the relational `Appointment` row too, mirroring the blob's own
"permanently remove" semantics exactly.

## 2. A second, more fundamental bug found while fixing the first

`AppointmentRepository.delete()` (new method) relies on every sub-entity table's
`ondelete="CASCADE"` FK to clean up its shadow tree in one statement. The first version of this
fix's own test caught that this **doesn't work at all on SQLite** — SQLite does not enforce
foreign-key constraints, cascades included, unless `PRAGMA foreign_keys=ON` is explicitly set per
connection (unlike Postgres, which always enforces them). The app's engine
(`src/diffdx/db/engine.py::get_engine`) never set this. This meant **every cascade relationship
in the schema** — not just this one — has been silently unenforced in local SQLite dev this
entire cutover (User → Patient/Doctor → Dependent, Appointment → its six sub-entity tables, etc.),
while working correctly in production (Postgres). Nothing in this cutover happened to delete a
parent row with children before this task, so the gap never surfaced until now.

**Fixed at the source**: `get_engine()` now registers a SQLAlchemy `connect` event listener that
runs `PRAGMA foreign_keys=ON` for every SQLite connection — a general fix for the whole app, not
a workaround local to `AppointmentRepository.delete()`. The isolated test engine in
`tests/test_appointment_repositories.py` (built directly via `create_engine`, not through
`get_engine()`) needed the same fix applied to its own fixture.

## 3. What changed

- **`AppointmentRepository.delete(appointment_id) -> bool`** (`src/diffdx/repositories/appointments.py`):
  same style as `WaitlistRepository.leave`/`BlockedDateRepository.unblock` — `session.get` +
  `session.delete`, returns whether a row was found and removed. A missing id is a no-op, not an
  error (mirrors the dismiss routes' own semantics — nothing to clean up if no shadow ever
  existed).
- **`src/diffdx/db/engine.py::get_engine`**: enables `PRAGMA foreign_keys=ON` for SQLite
  connections (see §2).
- **`patient_dismiss_appointment`/`doctor_dismiss_appointment`** (`appointments4.py`): after the
  existing blob delete, the same broad `try/except Exception` dual-write pattern as every other
  mutating route — parse the id, call `AppointmentRepository(db).delete(...)`, commit. No
  self-heal needed first, unlike every *write* dual-write in this cutover — there's nothing to
  create for a delete.

## 4. Verification

- New repository tests: `delete()` removes an existing row and returns `True`; returns `False`
  for a missing id without raising; a dedicated cascade test (created a `SuggestedTest` and a
  `Referral` for an appointment, deleted the appointment, confirmed both sub-entity rows are
  gone) — this test is what caught the SQLite pragma gap in §2 on its first run. 30 repository
  tests total (was 27).
- Full `pytest` — 315 passed, 18 failed (same pre-existing baseline as every prior phase), 1
  skipped — no regressions from enabling FK enforcement app-wide (a real behavior change worth
  double-checking broadly, not just for this one new code path).
- **Live regression test, the actual bug**: reproduced the exact sequence that showed the bug on
  `main` (book → cancel → dismiss → fetch by id) — confirmed the relational row is now gone
  (`0` rows) and the fetch correctly returns `404`, not the `200` reproduced before the fix.
  Confirmed dismissing a legacy appointment with no relational row at all (the `delete()` no-op
  path) still succeeds cleanly with no warnings logged.

## 5. What's left

File-storage dual-write, new schema for the ~8 blob-only fields, retiring any blob write, and the
unrelated `services/`/`main.py` cleanup remain open, deliberately deferred.
