# DiffDx v2 — Task 7: close the proven booking race (dual-write, not a full cutover)

Branch: `feat/booking-concurrency-fix`. A small, deliberately narrow fix — not the full
appointments-domain cutover (scoped and explicitly deferred this session; see the "why not the
full domain" note below).

## 1. What changed

Task 3's concurrency demo (`scripts/concurrency_demo.py`, `docs/evidence/concurrency.txt`)
already proved `POST /api/session/{session_id}/book`
(`src/diffdx/routers/session_booking.py::book_appointment`) loses bookings under concurrent
load: it checks `req.slot not in doctor["available_slots"]` against the blob-store doctor
directory, then writes, with no atomicity between the check and the write.

Task 2 already built the fix and never wired it up:
`src/diffdx/repositories/appointments.py::AppointmentRepository.book()` inserts a real
`Appointment` row and relies on the database's own partial-unique index
(`uq_appointments_doctor_slot_active` on `(doctor_id, slot_datetime)` excluding cancelled rows)
to make double-booking impossible — a concurrent second insert gets a real `IntegrityError`,
translated to `ConflictError` → 409.

**`web/api.py`**: `register_exception_handlers(app)` is now actually called. It was written in
Task 2 (`src/diffdx/api_exceptions.py`) specifically for this moment — its own docstring said
"wiring is a one-line call when the time comes." Maps `ConflictError` → 409 and
`OperationalError` → 503 globally.

**`session_booking.py::book_appointment`**: now inserts a real `Appointment` row via
`AppointmentRepository.book()` immediately after the existing (still-racy) blob availability
check, before anything else in the function runs. The doctor's real relational UUID is resolved
via `UserRepository.get_by_doctor_id()` (added in the identity cutover for exactly this lookup).
The same `appt_id` (a real `uuid4()`) is reused for both the relational row and the existing blob
dict, so a future full cutover — or the migration script's existence check — lines up cleanly.
No local `try/except` for the conflict case: `ConflictError` propagates to the newly-wired global
handler. Everything after the insert (diagnosis/demographics resolution, the blob `appt` dict,
file-upload handling, `_save_appointments`, `_save_doctors`, `_add_session_to_user`) is
**completely unchanged** — same logic, same writes, so every other appointment read call site
elsewhere in the app (all ~40 of them) keeps working exactly as before. The blob stays
authoritative for everything appointments read/display; the relational row exists solely to make
double-booking impossible.

Not populated on the relational row this pass: `primary_diagnosis`, `patient_age/sex/bmi`,
`chief_complaint`, `note` — these resolve later in the function and don't affect slot-conflict
detection; adding them would mean restructuring more of the function for no correctness benefit
this pass. Documented gap, not a silent omission.

## 2. Why not the full appointments cutover

Scoped and explicitly declined this session after exploration showed the domain doesn't fit the
same adapter-shim pattern that made the identity cutover (Task 6) tractable in one pass: ~40 read
call sites across 9 router files, 6 different sub-tables that would all need joining to compose
one appointment dict (`SuggestedTest`, `Prescription`, `Referral`, `TreatmentPlanItem`,
`UploadedFile`, `SecondOpinion`), and ~8 fields (`reschedule_proposal`, `refill_request`,
`intake`, `doctor_summary`, `patient_tags`, `reminder_sent`, `suggested_test_uploads`,
`prescription_history`) with no relational table at all yet. That's tracked as a genuinely
separate, much larger future phase — this task fixes the one proven, documented bug in the
domain without attempting the rest.

## 3. Verification

- `tests/test_concurrency.py::test_concurrent_booking_exactly_one_survives` — this test already
  existed (Task 3) and proves the exact guarantee this fix relies on, but is gated on a real
  Postgres `DATABASE_URL` (skipped otherwise — SQLite's single-writer serialization can mask the
  race). Spun up a temporary, isolated local Postgres instance (own data directory in the
  session scratchpad, self-signed SSL cert to satisfy the blob store's `sslmode=require`,
  torn down afterward) specifically to run this test for real: **passed** — N=10 concurrent
  `book()` calls, same doctor+slot, exactly 1 success and 9 clean `ConflictError`s, exactly 1 row
  in the database.
- Live smoke test against that same temporary Postgres, through the real booted `uvicorn` app and
  real HTTP requests (not just the repository layer): registered a patient, seeded a doctor
  directory entry + completed session history, called `POST /api/session/{id}/book` — succeeded,
  confirmed the same `appointment_id` exists in **both** the blob record and a real `Appointment`
  row with matching patient/doctor/slot. Then simulated the exact race window (restored the
  slot to "available" in the blob directory, matching what a second concurrent request's stale
  read would see) and booked again with a different patient: got a real `409` with the expected
  `ConflictError` detail message, and confirmed the blob was **not** double-written (still
  exactly 1 record) — the relational conflict correctly short-circuited before any blob write.
- `pytest tests/test_auth.py` — 14/14 passing (touches the same `app` object
  `register_exception_handlers` was wired into).
- Full `pytest` locally (SQLite, no `DATABASE_URL`) — 282 passed, 18 failed (same pre-existing,
  unrelated failures as every prior phase's baseline — retrieval/critic/ddxplus/patient-simulator
  — confirmed no new regressions), 1 skipped (the Postgres-gated concurrency test, correctly
  skipped without `DATABASE_URL`).
- Confirmed the local dev DB (`web/data/diffdx.db`) was untouched by any of the above — all
  Postgres-backed testing happened against the temporary instance, never the real dev DB.

## 4. Explicitly out of scope

The full appointments-domain cutover (all read call sites across `appointments.py`–`appointments6.py`,
`session_test_files.py`, `messaging.py`; the 6 sub-tables; the ~8 fields with no relational home)
stays exactly as scoped out — tracked as a future dedicated phase. `reschedule`/`cancel`/
`status`/`referral`/`test-orders`/etc. routes are untouched; they still read/write the blob only.
