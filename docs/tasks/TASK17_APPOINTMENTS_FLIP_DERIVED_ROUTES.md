# DiffDx v2 — Task 17: appointments cutover, Phase C sub-slice 5 (flip the remaining derived read routes)

Branch: `feat/appointments-flip-derived-routes`. Last read-route flip in the appointments domain
proper — every route that reads relationally-sourced appointment fields is now flipped.

## 1. Scope decision

`get_pending_refills` (`appointments3.py`) reads only `refill_request` — a field with no
relational column at all (confirmed since Task 8). Flipping it would add `db`/composer machinery
for zero benefit — **left untouched, deliberately, not a missed item.** The other four remaining
derived/transformed read routes all read at least one field the composer sources relationally
(`status`, `slot`, `doctor_name`, `specialty`, `primary_diagnosis`, `session_id`, `patient_name`),
even though each also touches a blob-only field (`test_orders`, `prescriptions`) that stays
blob-sourced regardless — real value in flipping these.

## 2. What changed

Same established compose-and-substitute pattern throughout (`AppointmentRepository.list_for_patient`/
`list_for_doctor` + `_compose_appointment_dict`, one bulk query + N composer calls, substituted
per-item over the blob's own entry set):

- **`get_test_notifications`** (`appointments.py`): the filter (`test_orders` truthy) stays
  blob-truth; only `doctor_name`/`slot`/`session_id` for matched entries is substituted. Found
  and deliberately preserved a pre-existing, unrelated bug while reading this route closely: the
  response's `"appt_id"` field reads `a.get("id")`, but blob records only ever have
  `"appointment_id"` — always `None`, confirmed still `None` after the flip too (a composed dict
  has no `"id"` key either).
- **`get_doctor_analytics`** (`appointments3.py`): `status`/`slot`/`patient_name` (the KPI/weekly-
  bucket computation) now relationally sourced; `open_slots`/`capacity` stay directory-blob-sourced
  (unrelated to appointments).
- **`get_patient_history_timeline`** (`appointments3.py`): `session_id`/`slot`/`doctor_name`/
  `specialty`/`primary_diagnosis`/`status` relationally sourced; `prescriptions` stays blob-sourced.
- **`get_symptom_history`** (`appointments5.py`, gained an `AppointmentRepository` import):
  `status`/`session_id`/`primary_diagnosis`/`doctor_name`/`slot` relationally sourced; the
  `if session_id:` truthy check is unaffected by composed `None` vs blob `""` (both falsy).

## 3. Verification

- Full `pytest` — 312 passed, 18 failed (same pre-existing baseline as every prior phase), 1
  skipped — no regressions.
- **Live before/after diff** (real booted `uvicorn`, real HTTP), reusing a dual-written + blob-only
  appointment pair (same shape as every prior flip): for all four routes, blob-only entries came
  back byte-for-byte identical and dual-written entries showed only the already-documented class
  of differences (additive keys, `book_direct`'s `None`-vs-`""` gap, the pre-existing `specialty`
  test-data drift) — no new discrepancies anywhere.
- **`get_doctor_analytics` specifically**: confirmed the computed KPIs (`utilization_pct`,
  `noshow_rate`, `total_patients`), the 8-week bucket breakdown, and the no-show patient table
  were **numerically identical** before and after — the real correctness bar for a derived route,
  not just matching raw field values.
- Confirmed the preserved `appt_id: null` bug still reproduces identically post-flip.

## 4. What's left in the appointments domain

`get_pending_refills` (justified above). `get_second_opinion_inbox` (reads a separate blob
collection, `second_opinions`, not `appointments` — a different composer entirely, out of scope).
File-storage dual-write, the two "dismiss" routes' deletion semantics, new schema for the ~8
blob-only fields, and retiring any blob write remain open, deliberately deferred. Outside the
appointments domain: the unrelated `services/`/`main.py`/lazy-import cleanup from
`TASK4_SPLIT_ROUTERS.md` §2.
