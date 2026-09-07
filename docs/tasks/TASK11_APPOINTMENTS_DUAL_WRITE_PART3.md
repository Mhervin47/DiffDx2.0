# DiffDx v2 — Task 11: appointments cutover, Phase B sub-slice 3 (dual-write `appointments3.py` + `appointments4.py`)

Branch: `feat/appointments-dual-write-part3`. Third slice of Phase B, same pattern Tasks 9/10
established. `appointments3.py` alone had only one dual-writable route, so it's combined with
`appointments4.py` (three routes, all reusing repository methods that already existed before this
cutover) into one balanced sub-slice.

## 1. What changed

**`AppointmentRepository.book()`** (`src/diffdx/repositories/appointments.py`): gained a
`rescheduled_from_id` parameter — the DTO already had the field (it maps to a real model column),
but `book()` itself never accepted it, so nothing could actually set it. Needed for
`patient_reschedule_appointment`'s dual-write (see below).

**`appointments3.py`**: one dual-writable route.
- `book_direct` → same shape as `book_appointment` (Task 7) and `create_followup` (Task 10): a
  direct `AppointmentRepository.book()` call, no self-heal needed since it's creating a new
  appointment, not mutating an existing one.
- Everything else in this file is read-only, directory-blob-only (`apply_schedule_template`), or
  targets a field with no relational column (`intake`, `refill_request` — confirmed out of scope
  since Task 8) — unchanged.

**`appointments4.py`**: three dual-writable routes, all reusing methods that predate this entire
cutover (Task 2):
- `cancel_patient_appointment` → self-heal, then `AppointmentRepository.cancel(...)`.
- `submit_rating` → self-heal, then `AppointmentRepository.set_rating(...)`.
- `patient_reschedule_appointment` — the interesting one: this route doesn't reschedule the same
  row, it cancels the old appointment and creates a genuinely new one. The relational
  `Appointment` model already has a `rescheduled_from_id` column for exactly this (unused until
  now — hence the `book()` extension above). Dual-write: self-heal the *old* appointment first
  (needed both to cancel it and to link the new one to it), cancel it
  (`cancelled_by="patient_reschedule"`), then book the new one with `rescheduled_from_id` set to
  the old row's id — two repository calls in one broad `try/except`, same "never break the blob
  write" rule as every other dual-write.

**Confirmed staying out of scope, each a deliberate gap, not an oversight**:
- File upload/list/delete/download routes — same file-storage gap flagged for
  `doctor_upload_file` in Task 9 (`_save_file_data` blob key, no disk write,
  `UploadedFile.storage_path` requires one).
- `patient_dismiss_appointment`/`doctor_dismiss_appointment` — these `del` the blob record
  entirely. Not dual-written: there's no existing "delete an Appointment row" repository method,
  and inventing relational deletion semantics for a UI-level "hide from my list" action is a real
  design decision that wasn't asked for. Leaving the relational shadow intact after a dismiss is
  arguably the safer default (a historical record surviving past when its blob view was hidden).
- `save_intake`, `request_refill`/`fulfill_refill` — `intake`/`refill_request` fields, no
  relational tables (confirmed since Task 8).

## 2. Verification

- New repository test: `AppointmentRepository.book()` with `rescheduled_from_id` set, confirms it
  persists and round-trips on the DTO. 26 tests total (was 24).
- Full `pytest` — 309 passed, 17 failed (one fewer than the usual 18 — the same
  order-sensitive/flaky `test_retrieval.py` tests noted in Task 10, not a regression), 1 skipped.
- **Live smoke test** (real booted `uvicorn`, real HTTP, local SQLite): `book_direct` a new
  appointment, confirmed the relational row. Cancelled it, confirmed
  `status`/`cancelled_by`/`cancelled_at` match. Booked a second, marked it "seen" via the
  already-dual-written doctor status route, submitted a rating, confirmed `rating_stars`/
  `rating_comment` match. Booked a third, **deleted its relational row to simulate a legacy
  blob-only appointment**, then rescheduled it — confirmed the old row got self-healed *and*
  correctly cancelled with `cancelled_by="patient_reschedule"`, and the new row's
  `rescheduled_from_id` correctly points at it. Confirmed `GET /api/patient/history` still serves
  the exact unmodified blob shape throughout.

## 3. Explicitly out of scope (future sub-slices)

`appointments5.py`, `appointments6.py`, `session_test_files.py` — each its own future Phase B
sub-slice. File-storage dual-write (needs a disk-path design decision, unresolved since Task 9).
The two "dismiss" routes' deletion semantics. `intake`/`refill_request` fields (no relational
tables). Phase C (read-side composer + cutover) untouched.
