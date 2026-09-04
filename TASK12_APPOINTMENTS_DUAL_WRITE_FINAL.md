# DiffDx v2 — Task 12: appointments cutover, Phase B final sub-slice (`appointments5.py` + `appointments6.py`)

Branch: `feat/appointments-dual-write-final`. Last Phase B sub-slice — closes out dual-write
coverage across every one of the domain's 9 router files.

## 1. What changed

**`WaitlistRepository.join`** (`src/diffdx/repositories/scheduling.py`): gained an optional `id`
param, same shape as `AppointmentRepository.book`'s — needed so `join_waitlist` can reuse the
blob's own generated entry id for the relational row too.

**`appointments5.py`** — four dual-writable routes, all mapping cleanly onto Phase A's
`WaitlistRepository`/`BlockedDateRepository`:
- `join_waitlist` → `WaitlistRepository.join(...)`, reusing the blob-generated entry id.
- `leave_waitlist` → `WaitlistRepository.leave(uuid.UUID(entry_id))`, same id.
- `block_date` → mirrors the blob's own remove-then-add logic exactly
  (`BlockedDateRepository.unblock` then `.block`), since calling `block()` alone on an
  already-blocked date would violate the `uq_blocked_dates_doctor_id_date` constraint the blob
  itself doesn't have. Verified live: re-blocking an already-blocked date with a different reason
  produces exactly one row with the new reason, not a conflict.
- `unblock_date` → `BlockedDateRepository.unblock(...)`.
- `update_patient_tags` (`patient_tags`) — no relational column (confirmed since Task 8).
  Everything else in this file is read-only. Unchanged.

**`appointments6.py`** — two dual-writable routes:
- `request_second_opinion` → self-heals the appointment, resolves both doctors via
  `UserRepository.get_by_doctor_id`, then `SecondOpinionRepository.create(...)` reusing the
  blob's own generated `opinion_id` so the response route can look it up by the same id later.
- `respond_to_second_opinion` → `SecondOpinionRepository.respond(...)`, parsed defensively (the
  broad `try/except` already covers a malformed id or a `NotFoundError`, never breaking the blob
  write it's alongside).

**`session_test_files.py`** — confirmed, no changes needed. Both its routes are entirely
file-storage (`_save_file_data` blob keys, no disk write) — the same gap already flagged and
deliberately excluded for `doctor_upload_file` (Task 9) and `appointments4.py`'s file routes
(Task 11).

## 2. Verification

- New repository test: `WaitlistRepository.join` with an explicit `id`, confirms it's honored.
  27 tests total (was 26).
- Full `pytest` — 309 passed, 18 failed (same pre-existing, unrelated baseline as every prior
  phase), 1 skipped — no regressions.
- **Live smoke test** (real booted `uvicorn`, real HTTP, local SQLite): joined a waitlist,
  confirmed the relational row uses the exact same id as the blob entry; left it, confirmed
  `status="cancelled"` relationally. Blocked a date, confirmed the row; **re-blocked the same
  date with a different reason** (the blob's own remove-then-add case) — confirmed exactly one
  row with the updated reason, no constraint violation; unblocked it, confirmed removed. Booked
  an appointment, requested a second opinion between two seeded doctors — confirmed the
  relational row shares the blob's `opinion_id` and has correctly resolved
  `from_doctor_id`/`to_doctor_id`; responded to it, confirmed `status="responded"` and the
  response text match. No dual-write warnings logged anywhere across the whole run. Confirmed
  `GET` reads (`blocked-dates`, `waitlist`) still serve the exact unmodified blob shape.

## 3. What this completes

Phase B (dual-write across every mutating route with a clean relational mapping, in all 9
appointments-domain router files) is now complete. Deliberately still not covered, each already
documented in a prior task's writeup: file storage (`doctor_upload_file`, `appointments4.py`'s
patient file routes, `session_test_files.py`), the two "dismiss" routes' deletion semantics
(`appointments4.py`), and fields with no relational column (`doctor_notes`,
`reschedule_proposal`, `intake`, `refill_request`, `patient_tags`, `doctor_summary`,
`prescription_history`).

## 4. What's left

Phase C — building the read-side composer and flipping reads to relational, retiring the blob —
is the next, separate, larger piece of work, not started here. It needs its own scoping pass:
unlike identity's single composer, an appointment composer has to join across 6 sub-tables per
row and handle the fields Phase B never covered (either backfilling them onto new relational
columns, or accepting they stay blob-only forever and the composer merges both sources). Also
still outstanding: the `services/`/`main.py`/lazy-import cleanup from `TASK4_SPLIT_ROUTERS.md` §2,
unrelated to this cutover.
