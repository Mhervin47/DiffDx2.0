# DiffDx v2 — Task 13: appointments cutover, Phase C sub-slice 1 (composer, unwired)

Branch: `feat/appointments-composer`. First Phase C slice: build and thoroughly verify
`_compose_appointment_dict`, wire it into **zero** routes. Phase C is the first phase where a
mistake could show wrong/stale data to a real request (Phase B was purely additive); per
sign-off, this session's deliverable is the composer alone, tested in isolation and against real
dual-written data — flipping actual read call sites is separate future work.

## 1. Design decisions confirmed before writing code

- **Composer merges both stores permanently** rather than requiring new schema for the ~8 fields
  that have never had a relational column. No migration this pass.
- **Three sub-entity lists stay blob-sourced, not relational**: `test_orders`,
  `test_results_data`-derived-fields, `prescriptions`, `approved_plan`. Task 9/10's
  `replace_for_appointment` deletes and recreates these rows on every dual-write, so their
  relational ids are **not stable across saves** and don't match what the blob (still the actual
  write target) has. Serving a relational id here that a later write can't find would silently
  break "edit this item" round-trips. This is a real, deliberate consequence of how Phase B was
  correctly built for dual-write safety — not a shortcut.
- **`patient_files` stays blob-sourced**: file-storage dual-write doesn't exist at all (Task 9/11
  explicitly excluded it), so there's no relational data to compose in the first place.

## 2. What changed

**`web/api.py`**: two new functions, next to `_compose_user_dict`/`_ensure_relational_appointment`:
- `_dt_iso(dt)`: found while writing the first test — SQLite (local dev) doesn't round-trip
  `tzinfo` on `DateTime(timezone=True)` columns the way Postgres (production) does, so a bare
  `.isoformat()` call would produce a different string shape depending on backend. This helper
  treats a naive datetime as UTC before formatting, matching the "naive treated as UTC"
  convention already used throughout this codebase (`_parse_dt` in the migration script, etc.) —
  used for every timestamp field the composer emits.
- `_compose_appointment_dict(db, appt_dto)`: starts from **the full blob record** for the same
  appointment id (`dict(blob_appt)`), then overrides only the fields it can source more reliably
  relationally — core identity/status/scheduling fields, patient/doctor name+specialty resolution
  (via `UserRepository`, already relational since the identity cutover), `referral`,
  `second_opinion`, `rating`. Guards the six list-shaped blob-only fields with `.setdefault([])`
  so a purely-relational appointment (no blob counterpart at all) never `KeyError`s on the common
  `appt["test_orders"]`-style bare access every existing read route already uses.

**Two real gaps found and fixed while verifying against live dual-written data, not assumed
correct from the design alone**:
1. The original draft explicitly enumerated every field to include — and missed several genuine
   blob-only bookkeeping fields that exist in practice (`is_direct_booking`, `patient_note`,
   `dependent_id`, `booked_by_user_id`, every `*_updated_at` timestamp). An enumerated allowlist
   silently drops anything not on the list, present or future. Fixed by switching to "start from
   the full blob dict, override only what's relationally sourced" — a strictly safer shape than
   hand-listing fields.
2. `referral.referring_doctor` and (defensively) any future field on `second_opinion` — both
   *nested* one level inside a composed sub-dict — had the same problem one level deeper: fully
   replacing the sub-dict with only the relationally-sourced keys silently dropped
   `referring_doctor` (no relational column for it). Fixed by merging the relational fields onto
   the blob's own nested sub-dict too, not just at the top level.

## 3. Verification

- New `tests/test_appointment_composer.py`, 3 tests against the isolated SQLite engine (same
  pattern as `test_appointment_repositories.py`): a minimal booking composes without raising,
  with optional sub-objects (`referral`/`second_opinion`/`rating`) coming back `None` and the
  guarded list fields coming back `[]`; a fully-populated appointment (referral + second opinion
  + rating, all via their real repositories) round-trips every relationally-sourced field
  exactly; cancellation and follow-up/reschedule linkage (`parent_appointment_id`,
  `rescheduled_from_id`) compose correctly.
- **Live diff against real dual-written data** (no route changes — a direct script call, not
  through any HTTP route): booked an appointment through the real app, populated it through the
  real dual-write routes (referral, second opinion, status, rating, test orders, patient tags),
  then called `_compose_appointment_dict` directly and diffed the result against
  `_load_appointments()[appt_id]` key-by-key. This first pass is what surfaced both gaps in §2 —
  confirmed after the fix: **zero blob keys missing from the composed dict**, every
  relationally-sourced field matches exactly (including `doctor_id`'s short legacy form,
  `specialty` correctly reflecting a relational-only edit from an earlier session that the fresh
  directory-blob reseed didn't carry — a genuine, expected directory-vs-relational drift, not a
  composer bug), and the three deliberately-blob-sourced lists came through unchanged.
- Full `pytest` — 312 passed, 18 failed (same pre-existing, unrelated baseline as every prior
  phase), 1 skipped — no regressions.
- No route changes, so no live-behavior verification was needed beyond the direct composer check
  above — this is new, unwired code only.

## 4. What's left

Wiring the composer into actual read routes (a separate future sub-slice, or several — likely one
per file, same incremental pattern as Phase B) — not attempted here. Making the three id-unstable
lists safe to source relationally would need `replace_for_appointment` to preserve blob ids
exactly instead of regenerating them, a real design change to Phase B's already-shipped
repositories, out of scope for this slice. File-storage dual-write and new schema for the ~8
blob-only fields remain the same declined-this-pass gaps as every prior phase.
