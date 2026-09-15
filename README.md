# DiffDx

**DiffDx** is an AI diagnostic assistant that conducts adaptive multi-turn clinical interviews,
produces a ranked differential, and scores its own reasoning on every turn.


## Results

DDxPlus evaluation (top-1/top-3 accuracy, safety recall, cost per session) has not been run and
published in this repo yet — the harness exists (`scripts/bake_off.py`) but no results file has
been generated. This section will be filled in from a real run, not estimated.

## Quick start

```bash
git clone https://github.com/Mhervin47/DiffDx2.0.git
cd DiffDx2.0
cp loop1/.env.example loop1/.env   # add GROQ_API_KEY at minimum
docker compose up --build
# → http://localhost:8000
```

## What DiffDx does

DiffDx is a full patient-doctor telehealth workflow built around an AI-conducted intake
interview, not just a standalone chatbot. A session moves through three parties:

**Patient** — registers or continues as a guest, fills in demographics and chief complaint
(`patient-info.html`) plus structured history, then has an adaptive multi-turn interview with the
AI "doctor" (`session.html`) — one question at a time, live differential updating in a
collapsible sidebar as the conversation progresses. When the interview ends (confidence threshold
reached, turn limit, or a safety-triggered stop), the patient lands on a report page
(`report.html`) that leads with a specialist recommendation, urgency level, and an AI-suggested
pre-visit test checklist — deliberately **not** a named diagnosis (see "Patient-safe diagnosis
disclosure" below).

The suggested-tests list is generated once from the session's differential and then persisted —
report.html silently checks for additional tests on every load (no button; idempotent
server-side, so it only ever costs one extra LLM call per session) and badges anything new.
Patients can upload a result file against any suggested test right from `report.html`,
`health-history.html`, or the separate `pre-visit-intake.html` check-in tied to a booked
appointment — uploading is always optional, never required.

From the report, a patient books a real doctor (`find-doctors.html` / `book-slot.html`), tracks
appointments and doctor-written notes/prescriptions/test orders (`my-sessions.html`), and messages
their doctor (`messages.html`). Messaging stays open for the life of an upcoming appointment plus
a 7-day grace period after it's marked seen (long enough to cover a delayed test-result upload),
then closes to new sends — chosen over a fixed pre/post-appointment clock window specifically so
it doesn't conflict with that post-visit upload flow. Either side can also report a conversation
to an admin for review (non-medical use, harassment, spam, etc.) without blocking or notifying the
other party.

Two separate intake flows exist and shouldn't be confused: `patient-info.html` gates starting a
**new AI diagnostic session**; `pre-visit-intake.html` is a **per-appointment** check-in completed
shortly before an already-booked visit, unrelated to starting a session.

**Doctor** — signs in to a separate portal (`doctor-portal.html`, `doctor-overview.html`) showing
a queue of booked appointments. For each patient, the doctor sees the full AI-conducted interview
transcript, the critic's per-turn quality scores, and the ranked differential, and can write
prescriptions, order tests, leave a plain-language summary for the patient, refer to another
specialist, or request a second opinion from a colleague. Separately, the doctor can record their
own **confirmed diagnosis** — a distinct field from the AI's `primary_diagnosis`, never
auto-populated from it, so it only reaches the patient once a licensed doctor has actually typed
and saved it. A "New Results" stat chip and side panel surface patient-uploaded test results even
after an appointment has already been marked "seen" (where they'd otherwise be filtered out of the
default queue view), with a push notification the doctor portal already had the plumbing for.
Doctors get the same proactive unread-message push notification patients do, and can cancel an
appointment on their own schedule the same way a patient can cancel theirs (mirrored logic,
attributed separately as `cancelled_by="doctor"`).

**Admin** — a separate static app (`web/admin_portal/`), gated behind `role="admin"` both
server-side (every `/api/admin/*` route) and client-side (a page-shell guard redirects a
signed-out or non-admin visitor before any page content loads), for operational oversight:
- **Overview** (`index.html`) — cost/usage at a glance, system health, model config, voice/Sarvam
  usage. Cost figures across the console can be toggled between USD and a fixed-rate INR display
  (display-only conversion, nothing is recomputed).
- **Evidence** / **AI Quality** (`evidence.html`, `quality.html`) — accuracy, cost-effectiveness,
  and safety-recall comparison against a baseline; reasoning-quality and calibration breakdowns.
- **Audit** (`audit.html`) — filterable, append-only log of every sensitive action.
- **DSR** (`dsr.html`) — the GDPR-style data-subject erasure/export request queue.
- **Reports** (`reports.html`) — the review queue for patient/doctor-reported message threads
  (open / reviewed / dismissed), backed by the `message_reports` table.
- **Users** (`users.html`) — user growth/engagement/retention analytics: signup timeseries,
  activity summaries, cohort retention.
- **Architecture Validation** (`architecture-validation.html`) — maps DiffDx's actor-critic-router
  design against a peer-reviewed benchmark paper (MEDDxAgent, arXiv:2502.19175), with an
  interactive metrics explorer.
- **SOP** (`sop.html`) — an operator runbook: trigger/meaning/action/escalate-if entries for real
  situations an admin hits (a health-check pill going down, running a real DSR erasure, citing a
  cost figure externally, creating a new admin account).

Live usage tracking covers both LLM calls (tokens, latency, cost, actual model served after any
fallback reroute — `LlmUsageEvent`) and Sarvam voice/translation calls (`SarvamUsageEvent`),
both durable Postgres tables, tagged by source (`live_web` vs. `offline_cli`) so eval runs never
pollute production metrics. See [`loop1/src/admin_portal/README.md`](loop1/src/admin_portal/README.md)
for the full admin console reference, including Phase 3 (Reports, Architecture Validation, SOP,
voice usage).

### How one diagnostic session actually runs

1. **Doctor LLM (Loop 1)** generates one question at a time from the patient's evolving profile,
   pulling relevant few-shot exemplars via FAISS + MMR retrieval, compressing older turns once the
   conversation gets long, and updating a running differential after each answer.
2. **Safety Screener** runs on every patient answer — a small set of red-flag patterns (chest
   pain radiating to the arm, can't breathe, etc.) that stop the interview immediately and route
   straight to an emergency-care message, bypassing everything else.
3. **Critic LLM (Loop 2)** scores each turn in the background (question quality, differential
   quality, reasoning quality, calibration) without blocking the patient's next question — this is
   the "QA layer runs in production, not just offline" claim below. The same critic machinery
   doubles as the DDxPlus ground-truth evaluator for Loop 1's `phase7_eval` harness.
4. **Router (Loop 3)** takes the final differential once the session ends and classifies urgency
   (routine / urgent / emergency) and specialty using `data/disease_specialty_map.json`, with an
   ambiguity handler for differentials that don't clearly point to one specialty.
5. **Booking** inserts a real `Appointment` row with a DB-level unique constraint on
   `(doctor_id, slot_datetime)` — see `loop1/docs/evidence/concurrency.txt` for why that specific column exists.

## What makes it different

- **Three-loop architecture** — an actor conducts the interview, a critic scores each turn, and a
  router classifies urgency and specialty once the session ends.
- **The critic runs in production, not only offline** — an automated QA layer over clinical AI
  decisions, not just an eval-time artifact.
- **Multi-provider LLM fallback** (Groq → OpenRouter → Cerebras → Gemini, provider-dependent) with
  per-provider retry exhaustion before falling through the chain — driven by hitting real free-tier
  rate limits in practice, not designed in the abstract.
- **Blob store → relational cutover done as dual-write phases**, not a big-bang migration — see
  `docs/tasks/` for the actual sequence.
- **Patient-safe diagnosis disclosure** — the report page never states a raw AI-generated
  diagnosis to a patient. It leads with a specialist recommendation, urgency, and a pre-visit test
  checklist instead; the AI's full differential and confidence scores stay available, but only
  inside a collapsed "Technical Details" section meant for clinicians and reviewers. A direct
  response to mentor feedback that naming a specific disease — from a system that explicitly isn't
  a diagnosis — risks unnecessary patient panic.

## Architecture

```
Patient / Doctor browser
        │  REST + JSON
        ▼
   FastAPI (loop1/src/diffdx/main.py — app factory, 22 routers incl. admin_portal)
        ├── Auth layer (JWT via PyJWT, PBKDF2-SHA256 password hashing, refresh rotation, RBAC)
        ├── Loop 1 ─── Doctor LLM (actor)
        │               ├── Profile Updater
        │               ├── Compressor
        │               ├── Exemplar Retriever (FAISS + MMR)
        │               └── Safety Screener
        ├── Loop 2 ─── Critic LLM
        │               ├── Turn- & session-level scorers
        │               └── DDxPlus eval harness
        └── Loop 3 ─── Router (Urgency / Specialty / Ambiguity)
        ▼
PostgreSQL (SQLite locally) — durable data, via SQLAlchemy + Alembic
Redis (falls back to in-memory) — live diagnostic-session state
```

Doctor/critic model assignment lives in `loop1/config.yaml` (`models.doctor`) and the
`CRITIC_MODEL` env var — kept out of this README so it doesn't go stale; check those two places
for what's actually configured today.

See [`loop1/docs/ARCHITECTURE.md`](loop1/docs/ARCHITECTURE.md) for the full component diagram and
request lifecycle, and [`loop1/docs/DECISIONS.md`](loop1/docs/DECISIONS.md) for the reasoning
behind the infrastructure choices (sync SQLAlchemy, PBKDF2 over bcrypt, Redis sidecar, etc.).

## Tech stack

| Layer | Choice |
|---|---|
| API | FastAPI (sync), Uvicorn |
| LLM access | `litellm` unified client — Groq, OpenRouter, Cerebras, Gemini, Anthropic, all provider-swappable per model role in `config.yaml` |
| Retrieval | FAISS (flat IP index) + MMR selection over `sentence-transformers` embeddings |
| Database | PostgreSQL in production/Docker, SQLite fallback for zero-config local dev; SQLAlchemy 2.0 ORM, Alembic migrations |
| In-flight session state | Redis, falls back to an in-memory dict if `REDIS_URL` is unset (single-process only, logged as a startup warning) |
| Auth | PyJWT access + refresh tokens, PBKDF2-SHA256 password hashing, `slowapi` rate limiting |
| Frontend | Server-rendered static HTML/CSS/vanilla JS per page (`web/static/`) — no SPA framework; each page is self-contained and talks to the API via `fetch` |
| Deployment | Docker (multi-stage, non-root), Docker Compose (API + Postgres + Redis), Render (`render.yaml`) |

## Project structure

```
DiffDx2.0/
├── docker-compose.yml, render.yaml                 — deploy configs
├── docs/                                           — briefing page, task specs (docs/tasks/)
└── loop1/                                          — the actual application
    ├── src/
    │   ├── loop1/       — Loop 1: doctor LLM, retrieval, compression, safety, session loop
    │   ├── loop2/       — Loop 2: critic scoring, DDxPlus patient simulator, eval harness
    │   ├── loop3/       — Loop 3: urgency/specialty router, disease→specialty map, ambiguity handling
    │   ├── diffdx/       — the FastAPI app: routers, DB models/repositories, auth, main.py app factory
    │   └── admin_portal/ — admin console backend (audit, DSR, message reports, usage, health) + its own eval tooling
    ├── web/
    │   ├── api.py         — thin re-export shim: `from diffdx.main import app`
    │   ├── api_session.py — APISession: turn-by-turn session state machine used by the web layer
    │   ├── static/        — every patient/doctor page, plain HTML/CSS/JS, one file per screen
    │   └── admin_portal/  — the admin console's own static frontend (separate from web/static/)
    ├── alembic/          — migrations (relational schema is the source of truth)
    ├── prompts/          — versioned prompt files (`doctor_v0_6.txt`, etc.) — see loop1/README.md §8
    ├── exemplars/        — the few-shot pool Loop 1's retriever selects from
    ├── data/              — DDxPlus raw/curated eval data, disease→specialty map, seed data
    ├── test_cases/       — canned patient profiles for manual/demo runs
    ├── scripts/          — seeding, eval, migration, and one-off maintenance scripts
    └── tests/            — pytest suite
```

## Data model

Relational schema (`loop1/src/diffdx/db/models/`), Postgres/SQLite via SQLAlchemy + Alembic —
grouped by what each table is actually for:

- **Identity** (`user.py`) — `User` (shared login row) → `Patient` / `Doctor` 1:1 profile
  extensions, `Dependent` (a patient managing a family member's care), `RefreshToken` (rotation +
  revocation).
- **Clinical session** (`clinical.py`) — `DiagnosticSession` + `SessionTurn` (the durable record of
  a completed AI interview — id matches the `session_id` Loop 1 generates, not DB-assigned),
  `Prescription`, `SuggestedTest`, `Referral`, `SecondOpinion`, `RefillRequest`,
  `AppointmentIntake`, `PrescriptionHistoryBatch`, `TreatmentPlanItem`. Naming collision worth
  knowing: `SuggestedTest` here is the **doctor's** post-visit test order + result row — a
  completely different thing from the AI-suggested pre-visit tests described above, which are
  LLM-generated and live in the blob store (`store["suggested_tests"]`), not this table.
- **Scheduling** (`scheduling.py`) — `Appointment` (the row a DB-level partial unique index on
  `(doctor_id, slot_datetime)` makes double-booking structurally impossible for — see
  `loop1/docs/evidence/concurrency.txt` — plus `confirmed_diagnosis`/`diagnosis_confirmed_at` for
  the doctor-confirmed diagnosis feature), `DoctorSlot`, `BlockedDate`, `RescheduleProposal`,
  `Waitlist`.
- **Messaging** (`messaging.py`, `reports.py`) — `MessageThread`, `Message` (patient ↔ doctor chat,
  tied to an appointment; the relational tables exist but the live messaging router still reads/
  writes the blob store), `MessageReport` (a flagged conversation
  awaiting admin review: reason, optional details, status).
- **Platform** (`audit.py`, `dsr.py`, `files.py`, `usage.py`, `verification.py`) — `AuditLogEntry`
  (every sensitive action, read by the admin portal), `DsrErasureRequest` (GDPR-style
  export/delete queue), `UploadedFile` (test-result attachments), `LlmUsageEvent` (per-call token
  count/latency/cost, source-tagged `live_web` vs `offline_cli` so eval runs never pollute
  production metrics), `SarvamUsageEvent` (same idea for voice/translation calls — character
  counts, latency, cost), `EmailOtp`.

The blob store (`legacy_store.py`, a single JSON-per-collection table predating this schema) is
being cut over table-by-table in dual-write phases — see `docs/tasks/` for the exact sequence.

## Docs index

- [`loop1/README.md`](loop1/README.md) — the full technical reference: prompt system, locked
  schemas, exemplars, replication guide, glossary
- [`loop1/docs/ARCHITECTURE.md`](loop1/docs/ARCHITECTURE.md) / [`loop1/docs/DECISIONS.md`](loop1/docs/DECISIONS.md) — component diagram, request lifecycle, ADRs
- [`loop1/docs/evidence/concurrency.txt`](loop1/docs/evidence/concurrency.txt) — the lost-update
  proof. Reports 9/20 races landing inside the read-modify-write window rather than a clean 20/20,
  and explains why — it's exposing a real race, not a deterministic bug, so it won't reproduce
  identically on a rerun.
- [`docs/tasks/`](docs/tasks/) — the working specs this project was actually built from, in order
- [`loop1/src/admin_portal/README.md`](loop1/src/admin_portal/README.md) — admin console: DSR
  queue, audit log, usage/cost tracking, message reports, system health, and the operator SOP
- [Architecture write-up](https://mhervin47.github.io/DiffDx/) — how the actor-critic design adapts
  RL concepts to multi-turn diagnosis

## Configuration

All environment variables are documented in [`loop1/.env.example`](loop1/.env.example) — only
`GROQ_API_KEY` is required; everything else (database, Redis, email, OpenRouter/Cerebras/Gemini
fallback keys) degrades gracefully if unset. Don't duplicate that list here — it'll go stale.

## Tests

```bash
cd loop1
pytest tests/ -v
```

## Deployment

**Hosted**: `render.yaml` is a full blueprint — deploying it via Render's **New → Blueprint** flow
provisions three resources, all on the free tier: the web service, a Postgres instance
(`diffdx-db`), and a Key Value/Redis instance (`diffdx-redis`, private-network only). `DATABASE_URL`
and `REDIS_URL` are auto-wired to those (`fromDatabase`/`fromService` — never typed or pasted
anywhere), along with `ENVIRONMENT=production` and a pinned Python version. You still fill in, in
the Render dashboard: `GROQ_API_KEY` (required), `SECRET_KEY` (generate with
`python3 -c "import secrets; print(secrets.token_urlsafe(48))"`, must stay stable across deploys),
`CORS_ALLOWED_ORIGINS` (your real deployed URL — the app refuses to boot in production without
both this and `SECRET_KEY` set to real values), and optionally `OPENROUTER_API_KEY`,
`SARVAM_API_KEY`, `RESEND_API_KEY`/`RESEND_FROM`. Two free-tier caveats worth knowing before
relying on this for anything real: the free web service spins down after ~15 minutes idle
(~30-60s cold start on the next request), and the free Postgres plan is deleted ~30 days after
creation unless upgraded.

Admin accounts aren't seeded automatically (doctor accounts are) — after the first deploy, run
`PYTHONPATH=src python scripts/seed_admin.py` from the service's Shell tab in Render's dashboard.

**Self-hosted**: `docker compose up --build` from the repo root — runs the API alongside its own
Postgres and Redis containers. `loop1/Dockerfile` builds a non-root, multi-stage image with
`/health` (liveness) and `/ready` (DB + Redis connectivity) endpoints. `render.yaml` has been
deployed and confirmed live on Render; the Dockerfile/Compose path has been written and reviewed
carefully but not run against a live Docker daemon from this environment — treat the first real
`docker compose up` as the actual test, not a formality.
