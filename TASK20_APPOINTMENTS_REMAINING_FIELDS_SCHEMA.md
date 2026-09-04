# Task 20 — Appointments cutover: schema for the remaining blob-only fields

Backlog item 2 of 3 from the post-Task-18 "fix all the remaining backlog" pass. Designs and
dual-writes a relational schema for the 8 fields that have been flagged as "confirmed no
relational column" since Task 8 and re-confirmed at every dual-write phase since: `reschedule_proposal`,
`refill_request`, `intake`, `doctor_summary`, `doctor_notes`, `patient_tags`, `prescription_history`,
`reminder_sent`. No prior task had proposed a concrete schema for any of them.

A 9th field, `suggested_test_uploads` (a patient-upload marker keyed by suggested-test id), needed
**no new schema at all** — `UploadedFile.suggested_test_id` (added in Task 19) already makes it
fully derivable via a query, so it's closed out here with no code change.

## Schema

One Alembic migration (`9ef2c0edf03e`), all new tables `ondelete="CASCADE"` on `appointment_id`:

- **`reschedule_proposals`** (1:1, `scheduling.py`) — a doctor-proposed slot awaiting patient
  accept/decline. Upsert-shaped like `Referral`, not a list, since the blob always overwrites a
  single dict.
- **`refill_requests`** (1:1, `clinical.py`) — same upsert shape.
- **`appointment_intakes`** (1:1, `clinical.py`) — pre-visit patient intake form, overwritten on
  resubmit.
- **`prescription_history_batches`** (many-per-appointment, `clinical.py`) — a growing list of
  saved prescription-pad snapshots, unlike `Prescription` itself which is delete+recreated on
  every save and keeps no history.
- **`Appointment`** gains seven plain columns for the remaining scalar/list fields that are
  already 1:1 with the appointment dict: `doctor_summary`, `summary_updated_at`, `doctor_notes`,
  `notes_updated_at`, `patient_tags` (JSON), `tags_updated_at`, `reminder_sent`.

`_JSONB` (JSONB on Postgres / plain JSON on SQLite, already defined in `clinical.py` for
`DiagnosticSession`'s AI output) is reused for every list/dict-shaped column.

**Migration gotcha**: autogenerate proposed `op.drop_table('store')` — Alembic sees the
legacy blob-store table as "unmapped, remove it" since it's hand-managed via raw SQL, not a
SQLAlchemy model. Dropping it would have destroyed the blob, which is still authoritative for
every route not yet dual-written. Manually stripped from both `upgrade()`/`downgrade()`, with a
comment explaining why, before applying.

**Bug found and fixed along the way**: `reminder_sent`'s `server_default="false"` (the same
string-literal pattern already used elsewhere in the codebase, e.g. `Appointment.is_followup`)
reads back as `True` on SQLite — the string `"false"` gets inserted as literal text, and `Boolean`
treats any non-empty string as truthy. Confirmed live with a throwaway in-memory-engine repro
before writing any test. Fixed by using `sa.false()` (a real dialect-correct boolean literal)
for the new column specifically; the same latent bug on existing columns elsewhere is unrelated
to this task and left untouched.

## Repositories

`RescheduleProposalRepository`, `RefillRequestRepository`, `IntakeRepository` all follow the exact
`ReferralRepository.upsert` pattern (select-by-appointment-id, create-if-missing else overwrite
every field — a real upsert, not create-then-update).

`PrescriptionHistoryRepository.record_batch` replicates
`appointments2.py::update_prescriptions`'s exact dedup rule byte-for-byte: update the most recent
batch in-place if it was saved under 10 minutes ago **and** has the same set of prescription ids,
otherwise append a new batch — verified live with three saves (same id twice within the window,
then a different id) producing exactly one updated batch followed by one new batch, matching the
blob's own history shape.

`AppointmentRepository` gained `update_summary`, `update_notes`, `update_tags`,
`mark_reminder_sent`.

## Route wiring

Same `_ensure_relational_appointment` + broad `try/except Exception: db.rollback();
_log.warning(...)` dual-write block used everywhere in this cutover, added to: `propose_reschedule`,
`patient_reschedule_response` (now also flips `RescheduleProposalRepository`'s status alongside
the existing `AppointmentRepository.reschedule` call on accept), `request_refill`,
`fulfill_refill`, `save_intake`, `update_doctor_summary`, `update_doctor_notes`,
`update_patient_tags`, `update_prescriptions` (added a `PrescriptionHistoryRepository.record_batch`
call alongside the existing `PrescriptionRepository.replace_for_appointment` dual-write), and the
background `_reminder_loop` thread in `web/api.py` (self-heals the appointment and calls
`mark_reminder_sent` right after the existing blob-side flag is set).

Read-only routes touching these fields (the pending-proposal guard in `reschedule_appointment`,
`get_pending_refills`, `get_renewal_reminders`) are untouched — same as every prior phase, list/guard
reads stay blob-sourced until a dedicated read-flip pass.

## Verification

- 20 new repository tests in `tests/test_appointment_repositories.py` (65 total in that file):
  upsert-creates-then-overwrites for each 1:1 table, both `PrescriptionHistoryRepository` branches
  (append vs. update-in-place, plus a same-recency-different-ids case and a stale-but-same-ids
  case), the new `AppointmentRepository` setters, and the `reminder_sent` default-value bug fix.
- Full `pytest`: 331 passed (up from 318 pre-Task-19), same 17-18 pre-existing/unrelated failures
  (`test_critic.py`, `test_ddxplus_loader.py`, `test_patient_simulator.py`, `test_retrieval.py`,
  `test_router.py`).
- Live smoke test against a real booted server with a real booked appointment: saved doctor
  summary/notes/tags and confirmed the `appointments` columns; submitted an intake form and
  confirmed `appointment_intakes`; requested then fulfilled a refill and confirmed
  `refill_requests`' full lifecycle; proposed two reschedules in sequence (one accepted, one
  declined) and confirmed the single `reschedule_proposals` row upserted correctly each time, plus
  the appointment's `slot_datetime` updated on accept; saved three prescription batches (two with
  the same id within the 10-minute window, one with a different id) and confirmed exactly one
  updated batch plus one new batch in `prescription_history_batches`, with `prescriptions`
  correctly holding only the latest; verified the reminder-loop's dual-write call directly
  (`_ensure_relational_appointment` + `mark_reminder_sent`) since the loop's own email step is
  gated behind an unset `RESEND_API_KEY` in dev, unrelated to this change.

## Out of scope

Read-side composer wiring for any of these fields (a future Phase-C-style pass). Retiring any
blob write, and the unrelated `services/`/`main.py` cleanup remain queued as backlog item 3.
