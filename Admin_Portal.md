# DiffDx v2 — Admin Portal

Build spec for FE1 (frontend) with the backend endpoints each panel needs.
Ships by **Sept 14**. Deadline for the capstone is Sept 16; freeze code Sept 12.

---

## 0. What this is and who it's for

DiffDx has a patient portal and a doctor portal. Neither is the right home for
this. A doctor logs in to see *their patients*. This portal is for someone
auditing **the AI itself** and the data it holds — a clinical quality officer or
a compliance lead. Different job, different person, different portal.

Three portals with three enforced roles *is* the RBAC story. During the demo you
log in as patient, then doctor, then admin, and the third shows the AI being
supervised. A judge sees role separation working rather than claimed.

### Route layout

```
/                      landing            public
/evidence              benchmarks         public, NO auth
/patient/*             patient portal     role: patient
/doctor/*              doctor portal      role: doctor
/admin/*               THIS               role: admin
```

`/evidence` is deliberately public. If a judge has to log in to see accuracy
numbers, they will not see them. Link it from the landing page header.

### Files

```
loop1/web/static/admin/
  index.html          # home: health pills + config + cost widgets
  quality.html        # AI Quality Console  (priority 0 — separate spec)
  dsr.html            # Data Subject Requests
  audit.html          # Audit log viewer
  js/
    admin-common.js   # shared fetch wrapper, auth, nav
    quality.js
    dsr.js
    audit.js
```

Routes go in `loop1/src/diffdx/routers/pages.py`. API endpoints in a new
`loop1/src/diffdx/routers/admin.py`, every route behind `require_role("admin")`.

### Ground rules

1. **New pages only.** Do not touch existing routes — the route-diff check in
   WEEK1.md Task 4 must stay empty.
2. **Reuse the doctor portal's shell.** Copy its nav and CSS so this reads as
   one product. No restyling. FE time goes into content, not chrome.
3. **Charts via CDN**, no build step: `chart.js@4` from jsDelivr. The frontend
   has no bundler and this is not the week to add one.
4. **Mock first.** Write `js/mock.js` returning the exact response shapes below
   and build every page against it. Swap to real endpoints as backend lands
   them. Do not idle waiting on the API.
5. **Empty states everywhere.** A judge hitting a fresh database must see
   "no sessions yet", not a stack trace.
6. **Seed an admin user now.** Add it to `scripts/seed_doctors.py`. You need an
   admin account to log into during the demo and you do not want to be creating
   one by hand on the 13th.

---

## 1. Data Subject Request console — 4h — PRIORITY

`/admin/dsr` · `dsr.html`

**Why this one first.** Seven of the twenty capstone teams will *talk* about AI
governance. This is a working data subject request, executed live, in thirty
seconds. Not a slide claiming compliance — the actual operation. You need
`DELETE /api/me` for DPDP Act right-to-erasure regardless, so the backend work
is already on the list; this surfaces it.

### Screen

**Search.** One input, searches patients by email or name. Results list:
name, email, registered date, session count.

**Subject detail** (on select) — an inventory of every piece of PHI held about
that person and **where it lives**. Group by table, show counts:

```
Identity          users, patients                    1 record
Consultations     diagnostic_sessions, session_turns  14 sessions, 96 turns
Appointments      appointments                         3 records
Messages          message_threads, messages            2 threads, 18 messages
Files             uploaded_files (S3 / disk)           4 files, 2.1 MB
AI critiques      turn_critiques                       96 records
Audit trail       audit_log                            212 entries — RETAINED
```

The audit trail row is the interesting one. Mark it **retained** with a note:
audit entries are not erased, because the DPDP Act permits retention where
required for legal compliance, and an erasable audit log is not an audit log.
Saying this out loud — and showing you thought about the tension — is worth more
than a clean sweep.

**Three actions:**

| Action | Behaviour |
|---|---|
| **Export** | Downloads a JSON bundle of everything above. Data portability. |
| **Erase** | Two-step confirm: type the patient's email to enable the button. Deletes PHI, retains audit entries with the subject id pseudonymised. |
| **Consent** | Read-only view of consent given at registration, timestamp, version of terms. |

After erasure, show a receipt: what was deleted, counts per table, timestamp,
and a link straight to the audit entry it generated. That link is the handoff
into panel 2 and it makes the demo flow without you clicking around.

### API

```
GET  /api/admin/subjects?q=<search>
→ { items: [{ user_id, name, email, created_at, session_count }] }

GET  /api/admin/subjects/{user_id}/inventory
→ { user_id, name, email,
    categories: [{ label, tables: [str], record_count, detail, retained: bool }],
    consent: { given_at, terms_version, scope: [str] } }

GET  /api/admin/subjects/{user_id}/export
→ application/json attachment, full PHI bundle

DELETE /api/admin/subjects/{user_id}
  body: { confirm_email: str }
→ { erased: { table: count, ... }, retained: { audit_log: count },
    audit_entry_id: uuid, erased_at: iso8601 }
```

### Backend notes

- Erasure must run in **one transaction**. A half-erased subject is worse than
  no erasure feature.
- Pseudonymise, don't delete, the `actor_user_id` in `audit_log` — replace with
  a stable hash so the trail stays intact and the person stays unidentifiable.
- The erasure itself writes an `AuditLogEntry` with action `subject_erasure`.
- Confirm-email mismatch → 400, not a silent no-op.
- Delete S3 objects / disk files too, not just the DB rows.

### Acceptance

- [ ] Search returns a patient by partial email
- [ ] Inventory counts match what is actually in each table (spot-check 2)
- [ ] Export downloads valid JSON containing session transcripts
- [ ] Erase without matching confirm email → blocked
- [ ] After erase: patient rows gone, audit entries present, subject pseudonymised
- [ ] Receipt links to the audit entry and that link works

---

## 2. Audit log viewer — 3h — PRIORITY

`/admin/audit` · `audit.html`

Task 5 in WEEK1.md already writes an `AuditLogEntry` on every login, failed
login, PHI read, and appointment mutation. This is a filterable table over it.
Cheap to build, and it turns the compliance document into something visible.

### Screen

Table: timestamp · actor (name + role) · action · resource type · resource id ·
IP. Newest first, paginated 50 per page.

Filters: free-text on actor, action dropdown, date range, resource type.
Deep-link support — `?entry=<uuid>` scrolls to and highlights a single entry, so
the DSR receipt link lands on the right row.

Append-only banner at the top: "This log is append-only. No update or delete
path exists in the application." Small, but it is the claim that makes the log
mean anything.

### API

```
GET /api/admin/audit?actor=&action=&resource_type=&from=&to=&limit=50&offset=0
→ { total, items: [{ id, ts, actor_user_id, actor_name, actor_role,
                     action, resource_type, resource_id, ip }] }

GET /api/admin/audit/{id}
→ single entry, full detail
```

### Acceptance

- [ ] Log in as a patient in another tab → the login appears in the log
- [ ] Filter by action `subject_erasure` → finds the erasure from panel 1
- [ ] `?entry=<uuid>` highlights that row
- [ ] Date range filter works across a boundary
- [ ] No UI path exists to edit or delete an entry

---

## 3. Cost & usage panel — 3h

`/admin/index.html`, widget section

You are instrumenting these numbers for the evidence page anyway. Surfacing them
here costs little and gives you consulting language: *"$0.004 per consultation,
roughly $40/month at 10,000 sessions, dominated by RDS rather than inference."*
That sentence lands with a Deloitte manager in a way an accuracy figure alone
does not.

### Screen

Four stat cards: **cost per session** (USD, 4dp), **tokens per session** (mean),
**p95 LLM latency** (ms), **sessions today**.

One line chart: sessions per day, last 14 days.

One projection line under the cards: *"At 10,000 sessions/month: $X inference +
$Y infrastructure = $Z total."* Hardcode the infra figure from your AWS
estimate — this is a projection, label it as one.

### API

```
GET /api/admin/usage?days=14
→ { cost_per_session_usd, mean_tokens_per_session, p95_llm_latency_ms,
    sessions_today,
    daily: [{ date, sessions, tokens, cost_usd }] }
```

Compute cost from token counts times the Groq per-token rate held in config —
do not hardcode a dollar figure in the frontend.

### Acceptance

- [ ] Numbers move after running a real session
- [ ] Chart renders with fewer than 14 days of data
- [ ] Zero sessions → cards show 0, not NaN

---

## 4. System health — 1h

`/admin/index.html`, top strip

Trivially cheap, and it makes the portal feel operational rather than academic.

Status pills: **API** · **Postgres** · **Redis** · **Groq**. Green / amber / red
with last-checked timestamp. Poll every 30s.

### API

```
GET /api/admin/health/detail
→ { api: "ok",
    postgres: { status, latency_ms },
    redis:    { status, latency_ms },
    groq:     { status, latency_ms } }
```

Reuse the `/health` and `/ready` probes from WEEK1.md. Each dependency check
gets a short timeout — the health endpoint must never hang because Groq is slow.

### Acceptance

- [ ] Stop the Redis container → pill goes red within 30s, page still loads
- [ ] Endpoint returns in under 2s even with a dependency down

---

## 5. Model & config panel — 1h, READ ONLY

`/admin/index.html`, bottom section

Supports the "frozen, versioned configuration" argument in your model card —
an enterprise cannot cite "GPT-4, last Tuesday" in a regulatory filing, but it
can cite a pinned configuration with a documented eval.

Read-only definition list: actor model · critic model · temperature · retrieval
enabled · exemplar count · prompt version · **git SHA** · build timestamp ·
environment.

**Do not build editing.** A config editor is a half-day of work, a security
surface, and worth zero marks.

### API

```
GET /api/admin/config
→ { actor_model, critic_model, temperature, retrieval_enabled,
    exemplar_count, prompt_version, git_sha, built_at, environment }
```

Inject `git_sha` at Docker build time via `ARG GIT_SHA` and surface it through
`config.py`. Never expose API keys, `SECRET_KEY`, or `DATABASE_URL`.

### Acceptance

- [ ] `git_sha` matches the deployed commit
- [ ] No secret values appear anywhere in the response

---

## 6. Schedule — FE1

| Days | Work |
|---|---|
| Sept 3–4 | Lock API contracts with backend. Write `mock.js`. Build Quality Console layout. |
| Sept 5–8 | Quality Console complete on mocks, then real data |
| Sept 9–10 | **Data Subject Request console** |
| Sept 11 | **Audit log viewer** + deep-link from DSR receipt |
| Sept 12 | Admin home: health pills, config, cost widgets *(only if on schedule)* |
| Sept 13–14 | Empty states, freeze, rehearse |

Panels 3–5 are **stretch**. If Sept 11 arrives and the Quality Console, DSR, and
audit viewer are not all solid, cut them without hesitation — they are widgets on
a home page, not routes, and nothing depends on them.

Priority order if time runs out: Quality Console > DSR > Audit > Health >
Config > Cost.

---

## 7. Do not build

- **Safety incident queue** — duplicates the Quality Console's flagged queue.
  If you want escalation framing, add acknowledge/resolve buttons to the
  existing queue instead of a second page.
- **Doctor approval / verification workflow** — standard CRUD, zero
  differentiation, and it quietly eats two days.
- **Patient demographics, revenue charts, bulk email, user management** —
  generic admin filler. No judge scores it.
- **Config editing** — see panel 5.

---

## 8. The demo this produces

1. Patient runs a consultation, gets a differential
2. Doctor sees the case land in their queue
3. **Admin → Quality Console**: that session scored, one turn flagged
   `missed_red_flag`, the critic's `would_have_asked` beside what the actor
   actually asked
4. **Admin → DSR**: search the patient, show the PHI inventory, export, erase
5. **Admin → Audit**: the erasure, logged, with actor and timestamp
6. **`/evidence`**: the accuracy numbers behind all of it

Five minutes. Covers AI quality assurance, regulatory compliance, and measured
performance — built almost entirely from data the system already produces.

Step 3 is the moment worth rehearsing. It is the only point in the project where
an AI is shown catching another AI's clinical reasoning error, with a stated
reason, in production.

---

## 9. When to stop and ask

- A panel needs a backend table that does not exist yet
- Building something here would require changing an existing route
- The Quality Console is not finished by Sept 8 — stop and reprioritise rather
  than starting DSR alongside it