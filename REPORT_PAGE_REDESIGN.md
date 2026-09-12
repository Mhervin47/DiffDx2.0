# Report Page Redesign — Indirect Disclosure & Specialty-First Framing

**Status:** Planning only — no code changed yet.
**Owner's ask (via mentor feedback):** Telling a patient "you have X disease" outright can
cause unnecessary panic, especially from an AI system that is explicitly not a diagnosis.
The report page (`loop1/web/static/report.html`) should instead tell the patient what to do
next — which specialist to see, what tests to expect — without leading with a disease name.
The full differential (the AI's actual working diagnosis list) stays in the product, but
demoted to an opt-in, clearly-labelled technical section for reviewers/judges/demo audiences,
not the first thing a patient sees.

## 1. Problem, precisely

`report.html` today is two documents stapled together, in this order (see
`buildReport()`, `report.html:2239`):

1. **Diagnosis Hero** (`report.html:2247`) — a giant `.diagnosis-name` heading showing
   `d.final_diagnosis` verbatim, e.g. "Bacterial Meningitis". This is the very first thing
   rendered.
2. **Patient Summary card** (`buildPatientSummary`, `report.html:2447`) — restates the same
   diagnosis in prose: *"The most likely explanation for your symptoms is **{dx}**."*
3. **Doctor's Assessment / Closing Statement** (`buildClosing`, `report.html:2518`) — a third
   restatement: "Leading Diagnosis: {dx}".
4. **Performance Overview** metrics, **Weakness Breakdown**, **Turn-by-Turn Critique table**,
   **Conversation Viewer**, **Differential Evolution** chart — this whole block
   (`report.html:2264-2286`) is evaluation/QA instrumentation (question-quality scores,
   differential-quality scores, reasoning-quality scores against a rubric). It is clinically
   meaningless to a patient and was clearly built for the eval harness / demo audience, not
   for patients — but it currently renders unconditionally, between the diagnosis reveal and
   the actual differential list.
5. **Final Differential Diagnosis** (`buildFinalDiff`, `report.html:2744`) — the literal
   ranked list of diseases with probability bars. This is the sharpest version of "you might
   have disease X" on the page (bar #1 is usually >50%, in bold, first).
6. Doctor's note / prescriptions / test orders / referral (only present once a real doctor
   has written them — this is legitimate, doctor-authored clinical content, not the AI's
   raw output, and is out of scope for this redesign).
7. **Suggested Tests** (`report.html:2304`, populated by `loadSuggestedTests()`) — pre-visit
   test recommendations. Useful, specialty-agnostic, doesn't need a disease name to justify
   itself ("routine bloodwork before your appointment" reads fine without "because we think
   you have X").
8. **Next Steps** (`buildNextSteps`, `report.html:1860`) — the specialty recommendation +
   doctor booking cards. Currently phrased as *"Based on your diagnosis, we recommend seeing
   a specialist in {specialty}"* — the word "diagnosis" here is doing the same panic-inducing
   work even though no disease name appears in this specific sentence.

So the disease name is said **at least three times** before the user ever sees "what do I do
next", and the eval-harness noise sits in between, pushing the actually-useful part of the
page (tests + specialist) below the fold.

## 2. Design principle

**Lead with the plan of care, not the hypothesis.** The AI's differential is a hypothesis
used internally to pick a specialty and a test panel — it was never validated by a licensed
clinician, and DiffDx's own consent modal (`consent-modal.js`) already tells every user
"DiffDx is an AI-assisted tool and is not a substitute for professional medical advice,
diagnosis, or treatment." Showing a bolded disease name at 32px font directly contradicts
that disclaimer in spirit, even though it's technically caveated elsewhere on the page.

Concretely: a patient should come away from the top of the report knowing **(a)** what body
system/area of concern this points to, **(b)** which kind of specialist to book, **(c)** what
tests to get done before that appointment, and **(d)** that a licensed doctor will confirm
what's actually going on — not a specific disease label they'll spend the next three days
Googling.

## 3. What changes, section by section

### 3a. New top-of-page hero — replaces `.diagnosis-hero`

Drop the bare disease name as the first thing on the page. Replace with a **specialty-led**
hero:

- Eyebrow: "Your Recommended Next Step" (was "Primary Diagnosis").
- Headline: the specialty, not the disease — *"See a {specialty} specialist"* (data already
  available from the routing call, see §3d — this just means moving/duplicating that call
  earlier, or restructuring load order so routing resolves before the hero paints instead of
  after, since routing is currently loaded async into a section far below the hero).
- Subhead: one plain-language sentence built from urgency + a body-system phrase, not the
  disease name — e.g. *"Based on your symptoms, we're recommending a routine cardiology
  consultation."* Needs a small mapping or LLM-authored phrase per specialty
  ("cardiology" → "your heart and circulation", "neurology" → "your brain and nervous
  system", etc.) — see §5, open question on where this phrase is authored.
- Keep the existing chips (turn count, termination reason, patient age/sex) — those are
  session metadata, not disease disclosure, no panic risk.

### 3b. Patient Summary card (`buildPatientSummary`) — rewrite the copy

Current: *"The most likely explanation for your symptoms is **{dx}**."*

New: drop the disease name from the headline sentence entirely. Reframe around the plan:
*"Based on your answers, we've identified the specialist and tests that make sense for your
symptoms — see the recommendations below."* `c.differential_summary` (a free-text sentence
the critic model already writes) needs an audit — if it names the disease, it needs the same
treatment as `r.reasoning` in §3d. `c.recommended_next_steps` (the existing bullet list) stays
as-is; it's already phrased as actions, not diagnoses.

The "Listen" (TTS) feature reads `summaryText` aloud — `_buildSummaryText()` (`report.html:2344`)
currently starts with the same "most likely explanation is {dx}" sentence and must be rewritten
in lockstep so the spoken version doesn't leak the disease name back in through the audio path.

### 3c. Doctor's Assessment / Closing Statement (`buildClosing`) — collapse, don't delete

This section ("Leading Diagnosis: {dx}", "Differential Summary: ...") is the *critic model's*
internal write-up, not something a patient asked for. Move it inside the same collapsible as
§3e (it's technical/demo content, same audience). Don't delete it — it's useful evidence of
the system's reasoning quality for a demo/reviewer, just not patient-facing by default.

### 3d. Next Steps / specialty routing (`buildNextSteps`) — keep as the centerpiece, fix the one leaky sentence

This section is already doing exactly what the mentor wants (specialist recommendation +
booking), it just needs to stop saying "diagnosis":

- *"Based on your diagnosis, we recommend seeing a specialist in {specialty}."* →
  *"Based on your symptoms, we recommend seeing a specialist in {specialty}."*
  (`report.html:1940` — one-word-context change, trivial.)
- `r.reasoning` (`report.html:1943`, sourced from `data/disease_specialty_map.json` via
  `loop3.routing`) needs an audit pass: these strings were written per disease entry and may
  read like "X and Y symptoms are consistent with {disease}, warranting {specialty} referral."
  Anything naming a disease needs a rewrite to justify the specialty by symptom/urgency
  instead — e.g. "Your reported symptoms warrant urgent cardiology evaluation." This is a
  data-file edit (`data/disease_specialty_map.json`), not just a template change — needs a
  pass over every entry, flagged as the largest single chunk of hands-on work in this plan.
- This section (plus suggested tests, §3f) should visually become the *first* substantial
  content block after the new hero — i.e. reorder `buildReport()`'s template string so
  Next Steps + Suggested Tests render immediately after the Patient Summary card, ahead of
  the doctor's note/prescriptions block. (Booking a specialist and doing pre-visit tests are
  the two actions we want the patient to take; nothing else on the page is actionable.)

### 3e. Technical Details — new collapsible wrapper

New collapsible section (collapsed by default, clearly labelled, e.g. **"Technical Details
(for clinicians & reviewers)"** with a small info icon/tooltip: *"This shows the AI's internal
reasoning and confidence — not a confirmed diagnosis."*). Contents, moved as-is (no logic
changes, just relocated + gated behind a toggle):

- Performance Overview metrics (`metricCard` block)
- Weakness Breakdown (`buildWeaknessSection`)
- Turn-by-Turn Critique table (`buildTable`)
- Conversation Viewer (`buildConversation`) — arguably patient-relevant (it's their own
  Q&A transcript) but bundled here since it's long and not part of the "what do I do next"
  path; open question in §5 on whether this one stays outside the collapsible.
- Differential Evolution chart (`buildDiffEvolution`)
- Final Differential Diagnosis bars (`buildFinalDiff`) — the bluntest "you have disease X:
  55%" artifact on the page, explicitly named in the ask as needing to be hidden.
- Doctor's Assessment / Closing Statement (§3c)

Mechanically: wrap this whole cluster in one `<details>`/custom-toggle component (matching
the existing collapsible pattern already shipped in `session.html`'s differential sidebar
toggle — same interaction language, reuse the chevron + `aria-expanded` approach rather than
inventing a new pattern). Collapsed by default for every session; do **not** persist an
expanded/collapsed choice per-user the way `session.html`'s toggle does — every new report
should default to collapsed, since the whole point is that a patient shouldn't stumble into
this by habit.

### 3f. Suggested Tests — keep, no copy changes needed

Already specialty/test-focused, doesn't name diseases (`loadSuggestedTests()`,
`report.html:1401`). Reorder alongside Next Steps per §3d; no content changes.

### 3g. Doctor's note / prescriptions / referral — out of scope

These render only once a *real, licensed* doctor has written them (`loadTestOrders()`,
`report.html:1653`). That's not the AI's hypothesis, it's an actual clinician's word — no
panic-reduction rationale applies here. Leave untouched.

## 4. Implementation checklist (for the follow-up coding pass)

1. Reorder `buildReport()`'s template (`report.html:2245-2337`): Hero → Patient Summary →
   Next Steps + Suggested Tests → doctor's note/rx/referral (if any) → Technical Details
   collapsible (closing statement, metrics, weakness, turn table, conversation, diff
   evolution, final differential) → Actions bar.
2. Because Next Steps/Suggested Tests currently load *async* (`loadRouting()`,
   `loadSuggestedTests()`, fired after initial render) while the Hero needs specialty data
   *synchronously* for its headline (§3a), decide: either (a) delay the Hero's specialty line
   until `loadRouting()` resolves (show a skeleton/loading state in the hero, same pattern
   already used for the Next Steps section today), or (b) keep the Hero's headline
   specialty-agnostic ("Your Next Step Is Ready Below") and let Next Steps carry the actual
   specialty once loaded. Recommend (a) for a stronger first impression, but flag as an open
   question — (b) is less code.
3. Rewrite `_buildSummaryText()` and `buildPatientSummary()` copy (§3b) — including the
   TTS-read text, which must match the on-screen text.
4. Rewrite `buildNextSteps()`'s one leaky sentence (§3d, trivial).
5. Audit `data/disease_specialty_map.json`'s `reasoning` field, every entry — rewrite any
   that name the disease rather than justify by symptom/urgency (§3d — this is the largest
   single piece of hands-on content work here, separate from the template restructuring).
6. Audit `c.differential_summary` and `c.leading_diagnosis` — these come from the critic
   model's own free-text output (`loop2.critic`), not a template, so "rewriting the copy"
   here means changing the critic's prompt to write in specialty/symptom terms instead of
   disease-name terms, OR keeping the model output as-is but only ever showing it inside the
   Technical Details collapsible (never in the Patient Summary card). Recommend the latter —
   changing a model prompt is riskier and slower to validate than just not surfacing that
   specific field outside the collapsible.
7. Build the collapsible component (§3e) — reuse `session.html`'s toggle pattern
   (chevron rotate, `aria-expanded`, no localStorage persistence per §3e).
8. Verify the PDF/print export (`btn-pdf`) and `downloadReportJson()` — decide whether the
   printed/downloaded version should also default to the collapsed state (probably yes, for
   the same panic-reduction reason) or always include full technical detail (useful if the
   patient's own doctor wants the underlying differential on paper). Flagged as an open
   question in §5.
9. Manual QA pass reading the whole page top-to-bottom as a first-time patient would, once
   redesigned, checking specifically: does any sentence outside the collapsible name a
   disease? (Search for `final_diagnosis`, `leading_diagnosis`, `differential_summary`,
   `dx` template interpolations outside the new collapsible wrapper as a mechanical check.)

## 5. Open questions (need an answer before or during implementation)

- **Hero loading behavior** (§4.2): specialty-first hero with a loading skeleton, or
  specialty-agnostic hero that doesn't wait on the routing call?
- **Conversation Viewer placement** (§3e): inside the Technical Details collapsible, or kept
  visible outside it since it's the patient's own words, not an AI claim?
- **Print/PDF export** (§4.8): collapsed-by-default in the printed version too, or always
  expanded on paper since a follow-up doctor might want the full picture?
- **Severity/urgency language**: the urgency badge (routine/urgent/emergency) and the
  emergency-hospital-routing branch (`buildNextSteps()`'s `r.is_emergency` path,
  `report.html:1884-1932`) are explicitly out of scope for softening — an emergency case
  needs to stay direct and urgent ("Call 999 now"), this redesign is only about routine/urgent
  differential disclosure, not about diluting genuine emergency alerts. Confirming this
  reading is correct before implementation starts.
- **Who authors the specialty-area subhead phrases** (§3a, e.g. "your heart and circulation"
  per specialty)? A small static lookup table (~15-20 specialties, one-time authoring) is the
  cheapest option; letting the critic model generate this phrase per-session is more dynamic
  but reopens the same "can the model accidentally name a disease" risk this whole redesign
  is trying to close. Recommend the static lookup table.
