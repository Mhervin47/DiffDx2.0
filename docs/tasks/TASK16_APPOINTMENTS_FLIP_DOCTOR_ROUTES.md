# DiffDx v2 — Task 16: appointments cutover, Phase C sub-slice 4 (flip two more raw-list routes)

Branch: `feat/appointments-flip-doctor-routes`. Two more read routes flipped, both doctor-facing.

## 1. Scope decision

Surveyed the remaining read routes for value/effort before picking targets: `get_doctor_analytics`,
`get_symptom_history`, `get_patient_history_timeline`, `get_test_notifications`,
`get_pending_refills` all return *derived/transformed* subsets built from fields that stay
blob-sourced regardless of this cutover (`test_orders`, `refill_request` have no relational
column at all) or need real reshaping work — left for a later, lower-priority pass. Two routes
return raw appointment dicts in the exact shape Tasks 14-15 already handle:
`get_doctor_appointments` (`appointments.py`) and `get_patient_history` (`appointments2.py`, the
doctor-side "past visits with this patient" drawer) — mechanical applications of the same
pattern, real value (doctor-facing views become relationally sourced too, not just
patient-facing), low risk.

## 2. What changed

Both routes filter by the doctor's **short legacy `doctor_id`** (e.g. `"dr_001"`), not a
relational UUID — resolved once via `UserRepository.get_by_doctor_id(doctor_id)`, then
`AppointmentRepository.list_for_doctor(doctor_dto.id)` for a single bulk relational query,
composed per-row into a `composed_by_id` map (same shape as Task 15's `list_for_patient` usage).
The blob stays authoritative for the entry *set* in both routes — `get_patient_history`'s
`patient_name` match has no relational equivalent at all (not a column on `Appointment`), so
filtering still runs over the blob's own dicts exactly as before; only the *substitution* per
matched entry (`composed_by_id.get(appt_id, a)`) is new.

`appointments.py` gained a `UserRepository` import (`AppointmentRepository` was already there
from Task 9). `appointments2.py` already had both.

## 3. Verification

- Full `pytest` — 312 passed, 18 failed (same pre-existing baseline as every prior phase), 1
  skipped — no regressions.
- **Live before/after diff** (real booted `uvicorn`, real HTTP), reusing the two-appointment
  setup from Task 15 (one dual-written, one manually-created blob-only with zero relational row,
  same `patient_name` on both so the history-drawer match has something to filter on): for both
  routes, the blob-only entry came back **byte-for-byte identical**, and the dual-written entry
  showed only the same already-documented class of differences from Tasks 14-15 (additive keys,
  the `book_direct` `None`-vs-`""` gap, timestamp precision, the pre-existing `specialty` test-data
  drift). Count and sort order unchanged on both routes.
- Specifically confirmed the one thing this slice had to prove — `get_patient_history`'s
  `patient_name` case-insensitive match and `exclude`-id filtering still work correctly against
  composed entries, not just raw blob ones (the filter has no relational equivalent, so this
  wasn't automatically guaranteed by the composer's own correctness): both appointments matched
  by name were returned; excluding one by id correctly left only the other.

## 4. What's left

The derived/transformed read routes named in §1 (deferred). `get_second_opinion_inbox` (reads a
separate blob collection, `second_opinions`, not `appointments` at all — would need a different
composer entirely, out of scope for this cutover). Retiring any blob write remains unstarted.
