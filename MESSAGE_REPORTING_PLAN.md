# Report a Doctor/Patient for Message Misuse

## Problem

A patient and doctor can message each other freely (within the 7-day post-visit window
already enforced — see the messaging-window work). There's no way for either side to flag
that the other is using the channel for something other than medical care (harassment, spam,
solicitation, etc.) so an admin can review it.

## Design

### 1. New table: `message_reports` (relational — admin portal's own domain already lives
here, not the blob store; see `DsrErasureRequest` for the identical shape/precedent)

```python
class MessageReport(Base):
    __tablename__ = "message_reports"
    id: GUID, primary key
    reporter_user_id: GUID, FK users.id ON DELETE CASCADE, indexed   # always a real UUID
    reporter_role: str                                                # "patient" | "doctor"
    reporter_name: str                                                # denormalized, like messages already do
    thread_id: str, indexed                                           # "{patient_user_id}__{doctor_id}"
    reported_patient_user_id: str | None                              # set when a doctor reports a patient
    reported_doctor_id: str | None                                    # set when a patient reports a doctor
    reported_name: str
    reason: str                                                       # category, see below
    details: str | None                                               # free text
    status: str, default "open"                                       # open | reviewed | dismissed
    created_at, reviewed_at, reviewed_by (FK users.id SET NULL), admin_note
```

No FK on `reported_doctor_id` — it's the legacy short id (`"dr_001"`), not a `users.id` UUID
(same reason messages.py itself never FKs `doctor_id`). Denormalizing names avoids a join back
into the still-blob-based messaging domain, consistent with how `messaging.py`'s own message
rows already denormalize `patient_name`/`doctor_name`.

**Reason categories** (a fixed set, not freeform-only, so the admin list is scannable):
`non_medical` (using messaging for something other than care), `harassment`,
`inappropriate_content`, `spam`, `other`. `details` is optional free text on top of the
category — required only when category is `other`.

### 2. Patient/doctor-facing endpoint

`POST /api/messages/report` — any authenticated user (not `require_role`, since both roles
use this one route), body `{thread_id, reason, details}`. Resolves the *other* party from the
thread's own two message-derived ids (patient_user_id/doctor_id, split on `"__"`) rather than
trusting a client-supplied "who I'm reporting" field. 404s if the thread has no messages at
all (nothing to report). One open report per (reporter, thread) — a second submission while
one is still `open` updates the existing row's `details`/`reason` rather than creating a
duplicate, mirroring `dsr_requests.py`'s existing-pending-request handling.

### 3. Admin endpoints (new file `admin_portal/routers/reports.py`, same shape as `dsr.py`)

- `GET /api/admin/message-reports?status=open&limit=&offset=` — paginated list, `_audit()`'d.
- `PATCH /api/admin/message-reports/{id}` — body `{status, admin_note}`, sets
  `reviewed_at`/`reviewed_by`, `_audit()`'d. No auto-action (suspend, ban) — out of scope;
  this is a review queue, same as DSR requests are a queue an admin acts on manually.

### 4. Frontend — reporting (`messages.html`)

A "Report" icon button in the chat header (next to the existing delete-conversation icon)
opens a small modal: category radio/select + optional details textarea + submit. Disabled
(with a tooltip) when there's no thread open or the thread has no messages yet — nothing to
report on an empty conversation. Success shows a toast; no visible change to the conversation
itself (this isn't a block/mute action, just a flag to admin).

### 5. Frontend — admin review (`admin_portal/reports.html`, new page)

Same structural pattern as `audit.html`: filter-by-status form + paginated table (Reporter,
Reported, Reason, Details, Status, Created, actions). Row actions: "Mark Reviewed" / "Dismiss"
with an optional note, calling the PATCH endpoint. Added to the shared admin nav
(`AdminPortal.mountNav()`) alongside Overview/Evidence/AI Quality/Audit/DSR.

## Out of scope

- Automated moderation/action (suspending an account, blocking a thread) — this is a
  human-reviewed queue only, matching how DSR requests work.
- Reporting a single specific message (vs. the whole thread) — the thread is the unit, since
  that's also the unit the 7-day window and the UI itself already operate on.
