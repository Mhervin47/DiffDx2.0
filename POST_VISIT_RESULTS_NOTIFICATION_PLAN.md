# Post-Visit Test Result Notifications — Plan

**Status:** Planning only — no code changed yet.
**Prompted by:** a patient uploading a test result after the doctor already marked the
appointment `seen` — the doctor had no way to find out. Confirmed live: `doctor-portal.html`'s
`applyFilters()` excludes `status === 'seen'` from the Queue by default, and nothing else in the
app signals that new data landed on an already-completed appointment.

## 1. The problem, precisely

`GET/PATCH /api/doctor/appointments/{id}/status` (`routers/appointments.py:335`) sets
`status` to `upcoming | seen | no_show` with no side effects — it's a bare field write. Once
`seen`, `applyFilters()` (`doctor-portal.html:5115`) drops that appointment from the default
Queue view:

```js
if (!_showCompleted) {
  filtered = filtered.filter(a => {
    const s = a.status || 'upcoming';
    return s !== 'seen' && s !== 'no_show' && s !== 'cancelled';
  });
}
```

`_showCompleted` is a manual, doctor-initiated toggle — nothing flips it automatically, and
nothing tells the doctor there's a reason to. `results_uploaded` (set by the patient-side upload
endpoint, `routers/appointments4.py`) is a bare boolean with no "has a doctor actually looked at
this" state — so even a doctor who does toggle "Show completed" and scrolls back to that
appointment has no visual cue that *this particular* test result is new versus something they
already reviewed days ago.

## 2. The existing pattern this should copy

This exact category of problem — "something on an already-`seen` appointment needs the doctor's
attention" — is already solved for **Refill Requests**, and solved well. Three pieces, all
independent of the Queue's status filter:

- **Renewal Reminders side panel** (`#renewal-side-panel`, `doctor-portal.html:10035`) — lists
  every appointment with a pending refill request, across all statuses, with its own toggle
  button, independent of `applyFilters()`.
- **Dashboard stat counter** (`stat-renewals`, `doctor-portal.html:4158`) — a glanceable count in
  the main dashboard, not buried in a filtered list.
- **Browser push notification** (`firePushNotif`, `doctor-portal.html:4746-4751`) — fires once per
  unique refill request, deduplicated via `_notifSeenIds` in localStorage (a `Set` of
  `'refill-' + appointment_id` strings already notified), checked on the same polling loop that
  already watches for new upcoming appointments and emergencies.

Building the same three pieces for test results, rather than inventing a new UI pattern, is both
less work and immediately familiar to a doctor who already uses the refill panel.

## 3. Design

### 3a. New "reviewed" state (data model)

Add a `results_reviewed_at: datetime | None` (or a simpler boolean, see open question in §5)
alongside the existing `results_uploaded` on each test-order entry. Currently `results_uploaded`
lives inside the JSON-shaped `test_orders` list on the `Appointment` row (see
`TestOrderItem` in `schemas/appointments.py` and the `t["results_uploaded"] = True` write in
`routers/appointments4.py:127`) — this is blob/JSON-column data, not its own relational table, so
the new field is just another key in that same per-test dict, no migration needed. Set at upload
time to `null`/absent (unreviewed), cleared to a timestamp once the doctor acknowledges it (§3b).

### 3b. Backend: mark-reviewed endpoint

New `PATCH /api/doctor/appointments/{appt_id}/test-orders/{test_order_id}/reviewed`, modeled on
the refill-fulfillment endpoint's shape (ownership check via `doctor_id`, blob-first write,
relational sync in `try/except` if `test_orders` ever gets a relational table — currently it
doesn't, so this stays blob-only, consistent with how `results_uploaded` itself is written today).
Called automatically when the doctor opens that appointment's Test Orders section in the detail
view (matching how opening a chat thread marks messages read elsewhere in this app), not
requiring a separate manual click — reviewing shouldn't need its own extra step when just looking
at the result already counts as reviewing it.

### 3c. New "New Test Results" side panel

Clone of the Renewal Reminders panel (`#renewal-side-panel` / `toggleRenewalPanel()` /
`renewal-panel-body`): a `#new-results-panel` listing every appointment with at least one test
order where `results_uploaded && !results_reviewed_at`, regardless of the appointment's own
`status`. Each row: patient name, test name(s), upload timestamp, a link straight into that
appointment's detail view (which, per §3b, marks it reviewed on open). Toggled from a nav icon
button next to the existing refill-reminders bell, same visual language.

### 3d. Dashboard stat counter

A `stat-new-results` card in the same dashboard row as `stat-renewals`, counting appointments with
at least one unreviewed uploaded result.

### 3e. Browser push notification

Extend the existing polling loop (`doctor-portal.html:~4746`, right where the refill-request
`firePushNotif` call lives) with the same shape:

```js
if (a.test_orders?.some(t => t.results_uploaded && !t.results_reviewed_at)) {
  const resultId = 'results-' + id;
  if (!_notifSeenIds.has(resultId)) {
    firePushNotif(resultId, 'Test Results Uploaded — ' + (a.patient_name || 'Patient'), ...);
  }
}
```

No new polling infrastructure — this is one more condition inside the loop that already exists
and already handles the "browser must be open/permitted" caveat identically to how refills and
emergencies work today (i.e., this isn't a new reliability gap, it's the same one the app already
accepts for those).

### 3f. `status` stays untouched

Explicitly **not** reverting `status` from `seen` back to `upcoming` when a new result lands. That
field means "the consultation happened," which remains true regardless of when a lab result comes
back — conflating it with "there's new data to look at" would make the Queue's own filtering
logic (and anything else that reads `status`) harder to reason about. The side panel + stat +
notification carry the "needs attention" signal instead, cleanly separate from consultation state.

## 4. What this deliberately does not do

- Does not touch the patient-facing upload flow (`uploadTestOrderResult()` in `my-sessions.html`,
  fixed recently) — this plan is entirely about surfacing the upload to the doctor afterward.
- Does not add a relational table for test orders — they stay blob/JSON-shaped, matching every
  other field on that list (`priority`, `category`, `results_filename`, etc.).
- Does not implement the email-nudge fast-follow from the original discussion (§5 below) — flagged
  as a real gap (browser push requires an open/permitted tab) but scoped out of the first cut,
  same reasoning as accepting that gap for refills/emergencies today.

## 5. Open questions

- **Boolean vs. timestamp** for the reviewed flag — a timestamp (`results_reviewed_at`) is
  marginally more useful for later "reviewed 3 days after upload" reporting/audit purposes, a
  boolean is simpler. Leaning timestamp since the pattern already exists elsewhere in this exact
  table (`summary_updated_at`, `notes_updated_at`, `diagnosis_confirmed_at`) — consistent with the
  codebase's existing convention rather than introducing a bare boolean as the odd one out.
- **Auto-mark-reviewed on open vs. explicit action** — §3b assumes opening the detail view is
  enough (matches chat-read-receipt precedent already in the app). If a doctor might open a
  patient's detail view for an unrelated reason (e.g. rebooking) without actually looking at the
  Test Orders section specifically, this could mark results "reviewed" without them truly being
  seen. Alternative: only mark reviewed when the doctor expands/scrolls to the Test Orders section
  specifically, or requires an explicit "Mark reviewed" click (more friction, but more accurate).
- **Email fast-follow (§4)** — worth scoping as a separate follow-up plan once this lands, given
  the app already has working reminder-email infrastructure (`_send_reminder_email`/
  `_reminder_loop`) that a "new result uploaded on a seen appointment" trigger could reuse.
