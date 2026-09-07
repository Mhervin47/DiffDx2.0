# DiffDx v2 — Task 8: appointments cutover, Phase A (repository layer)

Branch: `feat/appointments-repo-layer`. Phase A of a three-phase plan for the full
appointments-domain cutover — see "Why three phases" below for why this couldn't be a single
pass the way the identity cutover (Task 6) was.

## 1. Why three phases

The identity cutover worked as one pass because all writes were concentrated in ~2 files
(`auth.py`, one route in `appointments2.py`), so a single adapter-shim composer could flip reads
and writes together safely. Appointments doesn't have that property: writes are scattered across
all 9 router files (`appointments.py`–`appointments6.py`, `session_booking.py`,
`session_test_files.py`) — status updates in one file, test orders in another, refills in a
third, files/cancel/rating in a fourth, waitlist/tags in a fifth, second opinions in a sixth.
Flipping reads to relational in any one file before *every* write path across all 9 is converted
would produce permanently incomplete data (e.g. a doctor's appointment list missing test orders
that a not-yet-converted file only ever wrote to the blob) — a structural gap, not a temporary
consistency window.

Confirmed sequencing:
- **Phase A (this task)**: build a repository for every sub-entity table, zero route changes.
- **Phase B (future)**: extend Task 7's already-proven dual-write pattern (`book_appointment`
  writes a real `Appointment` row *alongside* the unchanged blob write) to every other
  appointment-mutating route across all 9 files — relational data becomes a live,
  always-consistent shadow of the blob, while every read anywhere keeps serving from the blob
  exactly as today.
- **Phase C (future)**: only once B covers every write path, build a
  `_compose_appointment_dict`-style composer and flip reads, retiring blob writes last.

## 2. What changed

**`src/diffdx/exceptions.py`**: added `NotFoundError` (same shape as the existing
`ConflictError` — `RepositoryError` subclass, `.detail` attribute). **`api_exceptions.py`**:
registered a handler (→404). Found during exploration: `AppointmentRepository.cancel`/
`set_rating` (Task 2) already raised a bare `ValueError` on a missing row, unmapped to any HTTP
status — nothing calls either method from a route yet (only `book()` is wired, per Task 7), so
switching them to `NotFoundError` was a safe fix, done as part of this task rather than carried
forward.

**`src/diffdx/repositories/clinical.py`** (new): `SuggestedTestRepository`,
`PrescriptionRepository`, `ReferralRepository`, `TreatmentPlanItemRepository`,
`SecondOpinionRepository` — one per model in `db/models/clinical.py` that didn't already have
one. `SuggestedTest`/`Prescription`/`TreatmentPlanItem` get a `replace_for_appointment(...)`
method (delete-then-bulk-create) matching the blob's "PATCH replaces the whole list" semantics
those fields use today (`test_orders`, `prescriptions`, `approved_plan`). `Referral` gets
`upsert(...)` since its `appointment_id` is unique (1:1) and the blob route always overwrites the
whole referral dict.

**`src/diffdx/repositories/scheduling.py`** (new): `WaitlistRepository`, `BlockedDateRepository`
— the two `scheduling.py` models (alongside `Appointment`, which keeps its existing repository
unchanged) that didn't have one yet. `WaitlistRepository.notify_next()` marks the earliest
still-`"waiting"` entry for a doctor as `"notified"`, matching the blob's
cancellation-triggers-waitlist-notify flow in `appointments.py::update_appointment_status`.

Every field mapping (defaults like `priority="routine"`, `route="oral"`, `urgency="Routine"`,
`source="ai"`, `status="waiting"`/`"pending"`) comes directly from
`scripts/migrate_blob_to_relational.py`'s existing inline migration logic — same columns, same
defaults, already exercised and correct by that script.

`FileRepository` (`UploadedFile`, already existed) is untouched this phase — its `create`/
`list_for_appointment` are enough for now; any gap gets addressed in Phase B once a specific
route's actual need is known.

## 3. Verification

- New `tests/test_appointment_repositories.py` — 15 tests against an isolated SQLite engine (same
  pattern as `test_auth.py`/`test_concurrency.py`'s fixtures), all passing: create/list round-trips
  for every new repository, `replace_for_appointment` genuinely replaces (not appends) for the
  three list-shaped sub-entities, `ReferralRepository.upsert` updates in place without
  duplicating (confirmed via a direct row-count query against the unique `appointment_id`
  constraint), `WaitlistRepository.notify_next` picks the earliest waiting entry and correctly
  skips an already-notified one on a second call, `BlockedDateRepository`'s
  block/is_blocked/unblock round-trip, and every `NotFoundError` path (`SuggestedTestRepository
  .record_result`, `SecondOpinionRepository.respond`, `AppointmentRepository.cancel`/
  `set_rating`) raises the typed error instead of a bare `ValueError`.
- `pytest tests/test_auth.py` — 14/14 passing (confirms the exception-type change and the new
  handler registration don't affect anything already wired).
- Full `pytest` — 297 passed (282 baseline + 15 new), 18 failed (same pre-existing, unrelated
  failures as every prior phase's baseline), 1 skipped (Task 3's Postgres-gated concurrency test,
  correctly skipped without `DATABASE_URL`) — no regressions.
- No live smoke test this phase — nothing routes through this code yet (that's Phase B), so
  there's no HTTP behavior to exercise; the repository-level tests are the actual verification
  surface for this phase.

## 4. Explicitly out of scope (future phases)

Phase B (dual-write across all appointment-mutating routes in all 9 files) and Phase C (the read
composer + cutover) are not attempted here — each needs its own scoping pass given the number of
files and routes involved. No route reads or writes changed in this task; every appointment field
everywhere in the app is still served exclusively from the blob store, exactly as before.
