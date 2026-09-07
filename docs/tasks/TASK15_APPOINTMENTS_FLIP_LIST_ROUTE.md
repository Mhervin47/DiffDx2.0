# DiffDx v2 — Task 15: appointments cutover, Phase C sub-slice 3 (flip the first list route)

Branch: `feat/appointments-flip-list-route`. First list-route flip — proves the composer pattern
works for multi-item responses, not just single-appointment lookups.

## 1. N+1 decision

Accepted per-item composition in a loop (each list entry does its own ~5 sub-queries), no new
batch-query repository methods built. Matches the precedent already set for the identity domain
(Task 8's `_load_users()` composer does a full per-user join on every call, explicitly accepted
as fine at this app's current scale rather than building batch-fetch infrastructure ahead of
actual need). A batch-optimized version can be built later, informed by real usage, without
changing the composed output shape.

## 2. What changed

**`appointments.py::get_patient_appointments`** (`GET /api/appointments`): the blob remains the
authoritative source for *which* appointments exist for a patient (every booking still writes a
blob record regardless of dual-write status), but each entry is now composed from the relational
store when a row exists for it (`AppointmentRepository.list_for_patient` — existing since Phase A,
unused until now — one query, then one composer call per row), falling back to the raw blob
record per-item otherwise. Same fallback principle as Task 14's single-item flip, applied across
a list. The existing `internal_note`-stripping transformation runs unchanged on `entry` regardless
of whether it came from the composer or the blob.

## 3. Verification

- Full `pytest` — 313 passed, 17 failed (one fewer than the usual 18 — the same order-sensitive
  `test_retrieval.py` flakiness noted in earlier tasks, not a regression), 1 skipped.
- **Live before/after diff** (real booted `uvicorn`, real HTTP): created two appointments for the
  same patient — one fully dual-written (with a referral carrying an `internal_note`), one
  manually created as blob-only with zero relational row (also with a referral +
  `internal_note`, to specifically exercise the stripping transformation on the fallback path
  too) — captured the list response before the code change, diffed after:
  - The blob-only appointment's entry came back **byte-for-byte identical**, confirming the
    fallback branch is untouched.
  - The dual-written appointment showed exactly the same class of already-documented,
    expected differences as Task 14 (additive new keys, the `book_direct` `None`-vs-`""` gap for
    `session_id`/`primary_diagnosis`, SQLite timestamp microsecond truncation, the pre-existing
    `specialty` test-data drift) — no new discrepancies.
  - `internal_note` was correctly stripped from the referral on **both** entries, confirming the
    one existing per-item transformation survived the flip.
  - Sort order (`slot` descending) and total count were both unchanged.

## 4. What's left

Every other list route (`get_doctor_appointments`, `get_patient_history`,
`get_symptom_history`, `get_test_notifications`, etc.) — each its own future sub-slice, now
following the exact pattern this task established. Batch-query optimization if real usage ever
shows the per-item N+1 mattering. Retiring any blob write remains unstarted.
