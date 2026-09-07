# DiffDx v2 — Task 14: appointments cutover, Phase C sub-slice 2 (flip the first read route)

Branch: `feat/appointments-flip-first-route`. First actual read-route flip in the appointments
domain — the composer built in Task 13 now serves a real request for the first time.

## 1. What changed

**`appointments2.py::get_doctor_appointment_detail`** (`GET /api/doctor/appointments/{appt_id}`):
the appointment lookup now tries the relational store first (`AppointmentRepository.get_by_id` +
`_compose_appointment_dict`), falling back to the raw blob record only if no relational row
exists for that id yet. Not every appointment has been touched by a Phase B dual-write route, so
this must degrade gracefully rather than assume a relational row always exists. Gained a
`db: Session = Depends(get_session)` param; everything else in the route (ownership check,
session report loading, AI routing, turn history, doctor slots) is completely unchanged — only
the source of the embedded `"appointment"` value changed.

Picked deliberately as the *first* route to flip: it's the one bare single-appointment-detail
route in the domain (every other route either lists all of a patient's/doctor's appointments —
N+1 composer-call concern deferred to its own future sub-slice — or is a mutation already
dual-written), so the diff surface is as small as it gets.

## 2. Verification

- Full `pytest` — 312 passed, 18 failed (same pre-existing baseline), 1 skipped — no regressions.
- **Live before/after diff** (real booted `uvicorn`, real HTTP): captured the route's response
  for a richly-populated appointment (referral, second opinion, test orders, status) *before* the
  code change, then diffed it against the same call *after*. Differences found, all expected and
  already documented in Task 13, none a correctness bug:
  - New keys present post-flip that the blob record never had (`rating`, `prescriptions`,
    `patient_files`, `cancelled_at`/`by`, etc.) — strictly additive, the composer always includes
    them (as `None`/`[]`), unlike the blob which only has a key if something explicitly set it.
  - `specialty`: `"Cardiology"` (blob) vs `"Interventional Cardiology"` (relational) — pre-existing
    test-data drift from an earlier session's directory-vs-relational edit, not something this
    change caused (already called out in Task 13's writeup as an expected, real phenomenon: the
    directory blob and relational `Doctor.specialty` genuinely can diverge).
  - `session_id`/`primary_diagnosis`: `""` (blob) vs `None` (relational) — the known
    `book_direct` gap from Task 13 (that route doesn't set either field).
  - **New finding this task**: timestamp microsecond precision (`...203495+00:00` vs
    `...+00:00` with microseconds truncated to zero) — SQLite's `DateTime` column doesn't
    preserve sub-second precision as reliably as Postgres does on round-trip. Server-generated
    timestamps only, nothing in the app sorts or compares at microsecond granularity, so this is
    cosmetic — noted here rather than silently accepted, and worth re-checking against a real
    Postgres backend before this composer sees production traffic broadly.
- Confirmed the ownership check still works correctly against the *composed* dict for both
  legitimate access paths: the owning doctor, and a doctor who is only the second-opinion
  recipient (`second_opinion.to_doctor_id`, correctly resolved by the composer in Task 13).
  Confirmed an unauthorized doctor still gets an identical 403.
- **Fallback path exercised live**: manually created a blob-only appointment with zero relational
  row (simulating one Phase B never touched), confirmed the route still returns it correctly via
  the blob fallback — not a 404, not a crash. Confirmed a truly nonexistent id still 404s.

## 3. What's left

Every other read route in the domain — list routes especially (`get_patient_appointments`,
`get_doctor_appointments`, `get_patient_history`, `get_symptom_history`, etc.) — each its own
future sub-slice. List routes specifically still need a decision on N+1 mitigation (composing N
appointments means N × ~5 sub-queries) before they're flipped; not attempted or assumed away
here. Retiring any blob write, and the microsecond-precision question against real Postgres,
remain open for later phases.
