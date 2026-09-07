# Task 19 — Appointments cutover: file-storage dual-write

Backlog item 1 of 3 from the post-Task-18 "fix all the remaining backlog" pass. Adds real
disk-backed relational shadowing for the two appointment-scoped file upload routes — the last
major appointments-domain gap (`patient_files` has stayed blob-sourced in the composer since
Task 13 specifically because of this).

## Problem

`doctor_upload_file` (`appointments.py`) and `upload_patient_file` (`appointments4.py`) stored
file bytes only via `_save_file_data` — base64 into a blob key (`file:{appt_id}:{filename}`),
never touching disk. `UploadedFile.storage_path` is a required disk-path column, so there was no
relational data for uploaded files at all, unlike every other appointment sub-entity.

## Scope

Only the two appointment-scoped upload routes. `session_test_files.py`'s
`upload_suggested_test_file` is explicitly deferred — it's primarily session-scoped (works with
or without a booked appointment, writes to `_session_test_uploads` rather than `patient_files`),
meaningfully different shape, separate follow-up.

Disk path convention matches `scripts/migrate_blob_to_relational.py::_extract_file` exactly:
`web/data/files/{appointment_id}/{filename}`, `storage_path` stored relative to the repo root —
so a future migration run and live uploads land in the same place with the same path shape.

## Changes

- **`FileRepository.replace_for_appointment`** (`src/diffdx/repositories/files.py`): deletes any
  existing row(s) for `(appointment_id, filename)`, then creates a fresh one — mirrors the blob's
  own `files[:] = [f for f in files if f.get("filename") != file.filename]; files.append(record)`
  replace-by-filename semantics. Without this, re-uploading the same filename would create a
  second relational row with no dedup.
- **`doctor_upload_file`** (`src/diffdx/routers/appointments.py`): after the existing blob write
  succeeds, self-heals the appointment via `_ensure_relational_appointment`, writes the already-
  read `raw` bytes to `web/data/files/{appt_id}/{filename}`, and dual-writes via
  `FileRepository(db).replace_for_appointment(...)`. `test_order_id` marking stays blob-only —
  `SuggestedTest` has no `results_uploaded`/`results_filename` columns.
- **`upload_patient_file`** (`src/diffdx/routers/appointments4.py`): same treatment, plus
  defensive `suggested_test_id` UUID parsing (skip passing it through if it doesn't parse — same
  pattern as every other loosely-typed blob id in this cutover; confirmed live that a non-UUID
  value is silently dropped from the relational row without breaking the request).
- Both dual-writes are wrapped in the standard broad `try/except Exception: db.rollback();
  _log.warning(...)` — a disk-write or relational failure never breaks the primary blob-based
  response. Disk writes only happen after the appointment resolves relationally, so there's no
  path to an orphaned file with no relational row pointing at it.

## Verification

- New repository tests in `tests/test_appointment_repositories.py`: create-on-first-call,
  replace-not-duplicate on a same-filename second call, and `suggested_test_id` FK wiring.
- Full `pytest`: 318 passed, 18 pre-existing/unrelated failures (baseline unchanged), 1 skipped.
- Live smoke test against a real booted server: uploaded a file via each route, confirmed the
  bytes on disk at the expected path and a matching `uploaded_files` row; re-uploaded the same
  filename through each route and confirmed exactly one relational row survived with the disk
  file holding the newer content; confirmed `list_patient_files` (still blob-sourced) is
  unaffected; confirmed a non-UUID `suggested_test_id` is silently dropped from the relational
  write without any error.

## Out of scope (unchanged from the plan)

`session_test_files.py`'s upload route. Flipping `list_patient_files` /
`doctor_download_patient_file` / `download_patient_file` to read from the new relational/disk
data — this slice is write-side only. New schema for the ~8 other blob-only fields, retiring any
blob write, and the unrelated `services/`/`main.py` cleanup remain queued as backlog items 2 and
3.
