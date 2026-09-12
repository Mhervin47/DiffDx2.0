# Doctor-Confirmed Diagnosis Disclosure — Plan

**Status:** Planning only — no code changed yet.
**Builds on:** REPORT_PAGE_REDESIGN.md (the report page currently never shows the AI's raw
diagnosis to a patient at all — it leads with specialty routing instead, and tucks the AI's
differential into a collapsed "Technical Details" section).

## 1. The idea

Right now "never show the AI's diagnosis to a patient" is a blanket rule. This plan adds one
exception: once the patient's *actual doctor* reviews the case and explicitly confirms a
diagnosis, the patient can see *that* — the doctor's confirmed wording, not the AI's raw label.
Until a doctor does that, nothing changes from the current design. This is strictly additive: a
new gated disclosure path, not a loosening of the existing default.

Important distinction carried through this whole plan: the patient never sees the AI's own
`primary_diagnosis` string. They only ever see a `confirmed_diagnosis` string that a licensed
doctor typed (or explicitly approved), on a separate confirmation action, at a separate time.
Silently "auto-confirming" the AI's text if a doctor doesn't retype it would defeat the entire
point — see §5 for why "approve as-is" still needs to require the doctor to see and choose it.

## 2. Data model — one new column pair, same convention as everything else on this table

`Appointment` (`loop1/src/diffdx/db/models/scheduling.py:80`) already has `doctor_summary` +
`summary_updated_at` as a plain-scalar column pair added in "Task 20" for exactly this shape of
data (doctor writes something, patient reads it, blob was the old source of truth). Add the same
pair:

```python
confirmed_diagnosis: Mapped[str | None] = mapped_column(String(500))
diagnosis_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
```

New Alembic migration (`loop1/alembic/versions/`), same shape as the existing `doctor_summary`
migration. `primary_diagnosis` (already a column, AI-set at booking time) is left completely
untouched — it stays the AI's own record for eval/audit purposes; `confirmed_diagnosis` is a
new, separate, doctor-authored field. Never overwrite one with the other.

Blob mirror: `appt["confirmed_diagnosis"]` / `appt["diagnosis_confirmed_at"]`, written first
(unconditional) exactly like every other doctor-write endpoint in this codebase, with the
relational write wrapped in `try/except` afterward — same dual-write pattern as
`update_doctor_summary` (`routers/appointments2.py:339-366`), not a new pattern.

## 3. Backend — one new endpoint, modeled directly on `update_doctor_summary`

`PATCH /api/doctor/appointments/{appt_id}/diagnosis`, `routers/appointments2.py`, right next to
`update_doctor_summary`:

```python
class ConfirmDiagnosisRequest(BaseModel):
    diagnosis: str | None  # None = withdraw a prior confirmation, see §5
```

Same shape as `update_doctor_summary`: `require_role("doctor")`, ownership check
(`appt.get("doctor_id") != doctor.get("doctor_id")` → 403), blob write first, relational sync
in a `try/except`. A `None`/empty diagnosis clears `confirmed_diagnosis` and
`diagnosis_confirmed_at` both — lets a doctor retract a confirmation (e.g. they change their mind
after a follow-up test), which flips the patient's report straight back to the unconfirmed
specialty-only view with no separate "unconfirm" endpoint needed.

`GET /api/appointments` (`routers/appointments.py:73`, already the endpoint `report.html` and
`my-sessions.html` both read) needs no route changes — `_compose_appointment_dict`
(`legacy_store.py:543`) already does `composed = dict(blob_appt)` first, so `confirmed_diagnosis`
rides along automatically once it's written to blob, the same way `doctor_summary` already does.

## 4. Doctor portal UI — a sibling section to "Doctor's Summary for Patient"

`buildSummarySection()` (`doctor-portal.html:7333`) is the exact template to copy. New
`buildDiagnosisConfirmSection(apptId, appt)`, placed directly above or below the existing summary
textarea (same `.portal-section` card language):

- Shows the AI's own leading diagnosis/differential as reference (the doctor already sees this
  elsewhere — `dx-callout-sub` at `doctor-portal.html:5341` reads
  `closing.differential_summary` — so this isn't new information, just repeated here for
  convenience at the point of decision).
- A text input pre-filled with the AI's leading diagnosis as a *starting point*, not a
  pre-submitted value — the doctor must actively press "Confirm for Patient" to submit it, same
  friction as typing a summary from scratch. Editable, so the doctor can correct/refine the
  wording before confirming (e.g. AI said "Viral Illness", doctor confirms "Influenza A").
- If already confirmed: show the confirmed text + timestamp + a "Withdraw" button (calls the same
  endpoint with `diagnosis: null`) instead of the input, so it's obviously already-done rather
  than inviting a re-submit.
- Save button posts to the new endpoint — copy `saveSummary()`'s exact structure (disable during
  save, "✓ Done!" flash, `_appointments` cache update) rather than reinventing it.

## 5. Why "approve as-is" still requires an explicit action

The framing in the original ask was "if doctor approves the diagnosis... patient can see it" —
worth being precise about what "approves" means here, since the easy-but-wrong implementation is
a bare checkbox next to the AI's existing text ("[ ] I approve this diagnosis") that silently
reveals the AI's own wording once checked. That's a liability problem waiting to happen: it lets
"approval" become a rubber stamp click without the doctor having actually re-read or re-typed
anything, and if the AI's differential quietly changed between when the doctor glanced at it and
when they clicked approve (unlikely given the workflow, but not structurally prevented), there's
no record of what was actually approved.

This plan's design avoids that by making confirmation *always* go through a text field the
doctor's cursor has to touch — even "approving as-is" means the doctor sees the AI's text
pre-filled, and clicking Save submits whatever's actually in that field at that moment (their own
edit or the AI's unedited text they chose to leave alone) as a fresh, timestamped, doctor-authored
string. `confirmed_diagnosis` is never a pointer back to `primary_diagnosis` — it's always its own
independent value, submitted through the same "doctor typed/reviewed this" action a doctor's
summary already goes through. Cheap to build (it's the same textarea pattern that already exists),
and it means the confirmed text is defensible as "the doctor's own words" even in the 90% of cases
where the doctor didn't change a single character.

## 6. Patient-facing report page — the new disclosure path

`report.html`'s hero (`buildReport()`/`updateHeroSpecialty()`, see REPORT_PAGE_REDESIGN.md §3a)
stays exactly as-is by default. New logic in `loadRouting()` or a new small `loadConfirmedDiagnosis()`
call (reads the same `/api/appointments` response `loadTestOrders()` already fetches — no new
network round trip needed, just read the field off the same `appt` object already resolved there):

- If `appt.confirmed_diagnosis` is set: reveal it in a **new, clearly-doctor-attributed** card —
  not by rewriting the hero headline back to a diagnosis name (that would silently undo the whole
  point of REPORT_PAGE_REDESIGN.md for every future page-load), but as an *additional* card
  placed near the top, e.g. right after Patient Summary: "Dr. {doctor_name} has reviewed your
  results and confirmed: **{confirmed_diagnosis}**" with the confirmation date. The hero still
  says "See a {specialty} Specialist" — that's still true and still the actionable headline; the
  confirmed-diagnosis card is *additional* certainty, not a replacement for the existing flow.
- If not set: nothing changes. No "pending doctor review" placeholder card either — that would
  just be a second way to imply "you probably have something specific, we're just not telling you
  yet," which reintroduces a mild version of the exact anxiety this whole redesign exists to
  avoid. Silence is the correct default state.
- The AI's own differential inside "Technical Details" stays exactly as it is regardless of
  confirmation state — that section's whole point is "for clinicians & reviewers," not affected
  by whether a doctor happened to also confirm something for the patient-facing view.

`my-sessions.html`'s Records tab (`_buildRecordsPanel`, already lists prescriptions/test
orders/referral per appointment) gets one more conditional block for `confirmed_diagnosis`, same
card style as the existing prescription/test-order blocks — this data already flows through
`/api/appointments`, so no new fetch there either.

## 7. What this deliberately does not do

- Does not touch `primary_diagnosis` (AI-authored) or anything in the eval harness/Technical
  Details section — those stay exactly as they are, doctor-confirmation is a purely additive
  patient-facing disclosure, not a data-model replacement.
- Does not add any "doctor disagrees" negative-confirmation state (e.g. "doctor says this is NOT
  X") — out of scope; a doctor who disagrees simply doesn't confirm the AI's suggestion, or
  confirms their own different diagnosis instead. The UI only ever needs to represent "confirmed
  wording as the doctor typed it" or "nothing confirmed yet," not a third disagree state.
- Does not change how appointments without a linked doctor (e.g. a session with no booking at
  all) behave — `confirmed_diagnosis` can only ever be set by a doctor with the appointment ID,
  so a never-booked session simply never has this field, same as it never has a `doctor_summary`.

## 8. Implementation checklist

1. Alembic migration: add `confirmed_diagnosis` (String(500)) + `diagnosis_confirmed_at`
   (DateTime(timezone=True)) to `appointments`.
2. `db/models/scheduling.py`: add the two columns to `Appointment`.
3. `repositories/appointments.py`: extend `AppointmentDTO` + add a `confirm_diagnosis()` method
   mirroring `update_summary()`.
4. `schemas/appointments.py`: add `ConfirmDiagnosisRequest`.
5. `routers/appointments2.py`: add `PATCH /api/doctor/appointments/{appt_id}/diagnosis`, copying
   `update_doctor_summary`'s structure exactly (ownership check, blob-first write, relational
   sync in try/except).
6. `doctor-portal.html`: `buildDiagnosisConfirmSection()` + `saveConfirmedDiagnosis()`/
   `withdrawConfirmedDiagnosis()`, modeled on `buildSummarySection()`/`saveSummary()`; wire into
   the appointment detail view alongside the existing summary section.
7. `report.html`: new conditional card sourced from the already-fetched `/api/appointments`
   response (no new fetch), rendered only when `confirmed_diagnosis` is truthy; no changes to the
   hero or Technical Details.
8. `my-sessions.html`: one more conditional block in `_buildRecordsPanel`, same card language as
   the existing prescription/test-order blocks.
9. Manual QA: confirm a diagnosis as a doctor, verify it appears on the patient's report and
   Records tab; withdraw it, verify both surfaces go back to showing nothing; confirm an
   appointment that was never booked/has no doctor never shows this card at all.

## 9. Open questions

- Should `confirmed_diagnosis` show up in the printed/PDF export by default, or only on-screen?
  (REPORT_PAGE_REDESIGN.md's precedent: print currently expands *Technical Details*, which this
  card is explicitly not part of — so print behavior for this new card needs its own decision,
  not inherited from that precedent.)
- Should the doctor-portal confirmation input default to *empty* instead of pre-filled with the
  AI's leading diagnosis, to make "approve as-is" feel less like a rubber stamp and more like a
  deliberate re-statement? Pre-filled is faster for doctors (majority case: AI got it right,
  doctor just confirms); empty is marginally more deliberate. Leaning pre-filled per §5's
  reasoning (the *submit* action is what matters, not whether the text originated from a blank
  field), but worth confirming before building it.
