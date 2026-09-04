# DiffDx v2 — Task 10: appointments cutover, Phase B sub-slice 2 (dual-write `appointments2.py`)

Branch: `feat/appointments-dual-write-plan2`. Second slice of Phase B, same pattern Task 9
established: every mutating route gains a matching relational write alongside the unchanged blob
write, every read anywhere stays blob-only.

## 1. What changed

**`SuggestedTestRepository`** (`src/diffdx/repositories/clinical.py`): `create`/
`replace_for_appointment` gained three new optional params (`result`, `result_status`,
`result_recorded_at`, all `None` by default) — doesn't change `update_test_orders`'s existing
orders-only behavior (Task 9), but lets `update_test_results` (this task) pass results through
the same method. Added `_parse_dt` helper alongside the existing `_parse_uuid` (same
naive-as-UTC ISO-parse-or-`None` semantics as the migration script's own).

**`appointments2.py`** — four routes gained a dual-write, each after the existing (unchanged)
blob mutation, in a broad `try/except Exception` (same "logged and swallowed" pattern as Task 9):

- `update_approved_plan` → `TreatmentPlanItemRepository.replace_for_appointment`.
- `update_prescriptions` → `PrescriptionRepository.replace_for_appointment` on the current
  `prescriptions` list only (`prescription_history`, the batch-history list this route also
  maintains, has no relational table — confirmed out of scope since Task 8).
- `update_test_results` — the one route needing a real design decision, not just a field copy:
  `test_orders` and `test_results_data` are two separate blob lists joined by a shared
  blob-native id (`test_id`/`id`) only at read time. Since Task 9's `_parse_uuid` fix means a
  non-UUID blob id is never reused as the relational `SuggestedTest.id`, there's no queryable way
  to attach a result to "the right" existing row by that id. Fixed by re-deriving the full merged
  list (blob `test_orders` joined with the just-saved `test_results_data` by blob-native id,
  exactly the join `scripts/migrate_blob_to_relational.py` already does at migration time) and
  calling `replace_for_appointment` with it — same whole-list-replace semantics `update_test_orders`
  already uses, now carrying results too.
- `create_followup` — creates a **new** appointment, not a mutation, so it calls
  `AppointmentRepository.book()` directly rather than a sub-entity write. Real gotcha found while
  designing this: the new follow-up's `parent_appointment_id` is a genuine self-referencing FK
  relationally, so the *parent* appointment needs a relational row before the follow-up can
  reference it — fixed by calling `_ensure_relational_appointment` on the parent first (same
  self-heal every other route already does), then booking the follow-up with
  `parent_appointment_id` set to the now-guaranteed-to-exist parent id.

**Confirmed staying out of scope, no schema change**: `update_doctor_summary`'s `doctor_summary`
field (no relational column, per Task 8). `update_doctor_profile_details`/`update_doctor_slots`
needed no changes (profile-details already dual-writes since Task 6; slots only touch the
directory blob, `DoctorSlot` stays out of scope same as the whole directory).
`doctor_download_patient_file`, `get_patient_history`, `get_doctor_appointment_detail`,
`get_doctor_profile` are read-only, untouched.

## 2. Verification

- Extended `tests/test_appointment_repositories.py` to 25 tests (was 24): result fields
  round-trip through `replace_for_appointment` when present, default to `None` when absent.
- Full `pytest` — 307 passed, 18 failed (same pre-existing, unrelated baseline as every prior
  phase), 1 skipped — no regressions.
- **Live smoke test** (real booted `uvicorn`, real HTTP, local SQLite): booked an appointment,
  set test orders, then submitted results for one of two orders — confirmed the relational
  `suggested_tests` table shows the merged result on the one that got it and `NULL` on the one
  that didn't, exactly matching the blob. Saved an approved plan and prescriptions, confirmed
  `treatment_plan_items`/`prescriptions` rows match. **Self-heal-the-parent verified live**:
  created a blob-only legacy appointment (no relational row), created a follow-up from it, and
  confirmed both the parent got a real `Appointment` row (correct id/status/slot) *and* the
  follow-up's `parent_appointment_id` correctly points at it — the route's response was
  unaffected either way. Confirmed `GET` reads still serve the exact unmodified blob shape
  throughout.

## 3. Explicitly out of scope (future sub-slices)

`appointments3.py`–`appointments6.py`, `session_test_files.py` — each its own future Phase B
sub-slice. `update_doctor_summary`'s field, `prescription_history` (no relational tables). Phase
C (the read-side composer + cutover) untouched.
