# DiffDx v2 — Task 9: appointments cutover, Phase B sub-slice 1 (dual-write `appointments.py`)

Branch: `feat/appointments-dual-write-core`. First concrete slice of Phase B — extends Task 7's
proven dual-write pattern (`book_appointment` writes a real `Appointment` row *alongside* the
unchanged blob write) to `appointments.py`'s remaining mutating routes. Every read anywhere in
the app still serves exclusively from the blob, byte-for-byte unchanged — verified.

## 1. What changed

**`AppointmentRepository`** (`src/diffdx/repositories/appointments.py`): two new methods,
`update_status(appointment_id, status)` and `reschedule(appointment_id, new_slot_datetime)`, same
style as the existing `book`/`cancel`/`set_rating` — both raise `NotFoundError` on a missing row,
`reschedule` also raises `ConflictError` on a slot clash (same `IntegrityError` translation
`book()` already does, since rescheduling into an already-taken slot must be rejected the same
way booking one is). Fixed a subtle bug caught during implementation: the original draft of
`reschedule`'s conflict handler read `appt.doctor_id` *after* `session.rollback()`, which can
expire the ORM object — fixed by capturing `doctor_id` into a local variable before the flush
attempt.

**`web/api.py`**: new `_ensure_relational_appointment(db, appt: dict) -> uuid.UUID | None`. Every
dual-write route needs the relational `Appointment` row to already exist (sub-entity tables have
a NOT NULL FK to `appointments.id`), but only appointments booked after Task 7 — or backfilled by
the migration script — have one. This self-heals the gap: if `AppointmentRepository.get_by_id`
finds nothing, it resolves the patient/doctor via `UserRepository` and creates the row from the
blob record's own fields, so every appointment a dual-written route touches ends up shadowed over
time. Best-effort by design — returns `None` (never raises) if the row can't be resolved or
created, since a shadow-write failure must never affect the blob write it's alongside.

**`appointments.py`**: five routes gained a dual-write, each following the same shape — after the
existing (unchanged) blob mutation, a broad `try/except Exception` block (same "logged and
swallowed" precedent as the existing `_send_email_notification`) calls
`_ensure_relational_appointment` then the specific repository method:
- `update_test_orders` → `SuggestedTestRepository.replace_for_appointment`
- `save_referral` → `ReferralRepository.upsert`
- `update_appointment_status` → `AppointmentRepository.update_status`
- `reschedule_appointment` → `AppointmentRepository.reschedule`
- `patient_reschedule_response` (accept branch only — decline has no relational-column effect) →
  `AppointmentRepository.reschedule`

**Not dual-written this slice, each a documented gap, not an oversight**:
- `doctor_upload_file`: stores files via `_save_file_data` (a blob key, base64), never touching
  disk — `UploadedFile.storage_path` is a required disk path, so dual-writing this route would
  mean adding new disk-write behavior it doesn't have today. Needs its own design decision first.
- `update_doctor_notes` (`doctor_notes`) and `propose_reschedule`/the decline branch
  (`reschedule_proposal`): no relational column exists for either field (confirmed in Task 8's
  "no table exists for" list) — no schema change attempted here.
- The waitlist-notify branch inside `update_appointment_status` is confirmed dead code today
  (`valid = {"upcoming","seen","no_show"}` never includes `"cancelled"`, so the
  `if req.status == "cancelled"` check below it can never trigger through this route) — left
  exactly as-is, not "fixed," since that's a pre-existing latent bug unrelated to this task.

**Real bug found and fixed during live testing**: blob `test_orders`/`prescriptions`/
`approved_plan` list items commonly carry short frontend-generated ids like `"t1"`, not UUIDs.
The first draft of `replace_for_appointment` on all three list-shaped repositories did
`uuid.UUID(item["id"])` unconditionally, which threw `ValueError` on any non-UUID id — caught
live (see §3) by the dual-write's own `try/except`, so the blob write was never affected, but the
relational shadow silently failed every time. Fixed with a shared `_parse_uuid` helper
(`src/diffdx/repositories/clinical.py`, same approach as
`scripts/migrate_blob_to_relational.py`'s own `_parse_uuid`) that returns `None` instead of
raising on a non-UUID string, so a fresh id is generated instead — locked in with a new
regression test.

## 2. Verification

- Extended `tests/test_appointment_repositories.py` to 24 tests (was 15): `update_status`/
  `reschedule` success + `NotFoundError` + `reschedule`'s `ConflictError` on a genuine slot
  clash; three new tests for `_ensure_relational_appointment` (returns the existing id without
  re-creating, self-heals a missing row from a blob dict, returns `None` when the doctor can't be
  resolved); the non-UUID-blob-id regression test.
- Full `pytest` — 306 passed, 18 failed (same pre-existing, unrelated failures as every prior
  phase's baseline), 1 skipped — no regressions. (One run showed 16/307 instead of 18/306 — traced
  to `test_retrieval.py`'s two exemplar-retrieval tests being order-sensitive/flaky independent of
  this change; re-ran in isolation and confirmed unrelated.)
- **Live smoke test** (real booted `uvicorn`, real HTTP, local SQLite dev DB): booked an
  appointment (creates the relational row per Task 7), then exercised all five dual-write routes
  in sequence — test orders, referral, status, doctor-initiated reschedule, and the full
  propose-reschedule → patient-accept flow — confirming each one's relational
  row/field matches the blob after every step (`SuggestedTest` rows, `Referral` row, `Appointment.status`,
  `Appointment.slot_datetime` through two different reschedule paths). This run is what
  surfaced the non-UUID-id bug above; re-ran clean after the fix.
- **Self-heal verified live**: manually created a blob-only appointment with zero relational
  counterpart (simulating a pre-Task-7 legacy appointment), mutated its status through the real
  route, and confirmed a real `Appointment` row now exists with the correct id/status/slot — the
  route's response was identical either way.
- Confirmed reads are untouched: `GET /api/appointments` returns the exact same blob-shaped JSON
  before and after every dual-write above — no read path anywhere was modified.

## 3. Explicitly out of scope (future sub-slices)

`appointments2.py`–`appointments6.py`, `session_test_files.py` — each its own future Phase B
sub-slice. `doctor_upload_file`'s file-storage dual-write (needs a disk-path design decision
first). Phase C (the read-side composer + cutover) untouched.
