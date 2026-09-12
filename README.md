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
(`patient-info.html`) plus structured history (`history.html`), then has an adaptive multi-turn
interview with the AI "doctor" (`session.html`) — one question at a time, live differential
updating in a collapsible sidebar as the conversation progresses. When the interview ends
(confidence threshold reached, turn limit, or a safety-triggered stop), the patient lands on a
report page (`report.html`) that leads with a specialist recommendation, urgency level, and a
pre-visit test checklist — deliberately **not** a named diagnosis (see "Patient-safe diagnosis
disclosure" below). From there they can book a real doctor (`find-doctors.html` /
`book-slot.html`), track appointments and doctor-written notes/prescriptions/test orders
(`my-sessions.html`), and message their doctor (`messages.html`).

**Doctor** — signs in to a separate portal (`doctor-portal.html`, `doctor-overview.html`) showing
a queue of booked appointments. For each patient, the doctor sees the full AI-conducted interview
transcript, the critic's per-turn quality scores, the ranked differential, and can write
prescriptions, order tests, leave a plain-language summary for the patient, refer to another
specialist, or request a second opinion from a colleague — all backed by real relational tables,
not just the AI's own output.

**Admin** — a separate console (`web/admin_portal/`) for operational oversight: audit log viewer,
Data Subject Request (GDPR-style erasure/export) queue, LLM cost & usage tracking per session, and
a system health view. Gated behind `role="admin"` on the account (see
[`loop1/src/admin_portal/README.md`](loop1/src/admin_portal/README.md)).

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
   `(doctor_id, slot_datetime)` — see the v1 → v2 table below for why that specific column exists.

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
  a diagnosis — risks unnecessary patient panic. See `REPORT_PAGE_REDESIGN.md`.

## Architecture

```
Patient / Doctor browser
        │  REST + JSON
        ▼
   FastAPI (loop1/src/diffdx/main.py — app factory, 15 routers)
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
| Deployment | Docker (multi-stage, non-root), Docker Compose (API + Postgres + Redis), Render (`render.yaml`), Railway (`railway.toml`) |

## Project structure

```
DiffDx2.0/
├── docker-compose.yml, render.yaml, railway.toml   — deploy configs
├── docs/                                           — briefing page, task specs (docs/tasks/)
└── loop1/                                          — the actual application
    ├── src/
    │   ├── loop1/       — Loop 1: doctor LLM, retrieval, compression, safety, session loop
    │   ├── loop2/       — Loop 2: critic scoring, DDxPlus patient simulator, eval harness
    │   ├── loop3/       — Loop 3: urgency/specialty router, disease→specialty map, ambiguity handling
    │   ├── diffdx/       — the FastAPI app: routers, DB models/repositories, auth, main.py app factory
    │   └── admin_portal/ — admin console backend (audit, DSR, usage, health) + its own eval tooling
    ├── web/
    │   ├── api.py         — thin re-export shim: `from diffdx.main import app`
    │   ├── api_session.py — APISession: turn-by-turn session state machine used by the web layer
    │   └── static/        — every patient/doctor/admin page, plain HTML/CSS/JS, one file per screen
    ├── alembic/          — migrations (relational schema is the source of truth; see v1 → v2 below)
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
  `AppointmentIntake`, `PrescriptionHistoryBatch`, `TreatmentPlanItem`.
- **Scheduling** (`scheduling.py`) — `Appointment` (the row a DB-level partial unique index on
  `(doctor_id, slot_datetime)` makes double-booking structurally impossible for — see
  `loop1/docs/evidence/concurrency.txt`), `DoctorSlot`, `BlockedDate`, `RescheduleProposal`,
  `Waitlist`.
- **Messaging** (`messaging.py`) — `MessageThread`, `Message` (patient ↔ doctor chat, tied to an
  appointment).
- **Platform** (`audit.py`, `dsr.py`, `files.py`, `usage.py`, `verification.py`) — `AuditLogEntry`
  (every sensitive action, read by the admin portal), `DsrErasureRequest` (GDPR-style
  export/delete queue), `UploadedFile` (test-result attachments), `LlmUsageEvent` (per-call token
  count/latency/cost, source-tagged `live_web` vs `offline_cli` so eval runs never pollute
  production metrics), `EmailOtp`.

The blob store (`legacy_store.py`, a single JSON-per-collection table predating this schema) is
being cut over table-by-table in dual-write phases — see `docs/tasks/` for the exact sequence and
the v1 → v2 table below for why.

## v1 → v2

| | v1 | v2 |
|---|---|---|
| Persistence | Single-row JSON blob store, 8 collections in 8 rows | Relational schema, 3 Alembic migrations, FKs and indexes |
| Concurrency | Read-modify-write, silent lost updates | DB-level unique constraint, clean 409s — see `loop1/docs/evidence/concurrency.txt` |
| `web/api.py` | 3,892 lines, 93 routes | 67 lines; 15 routers, service layer |
| Auth | In-memory opaque tokens, lost on restart | JWT + refresh rotation, RBAC, rate limiting |
| Sessions | Process memory | Redis + Postgres persistence |
| Deployment | Manual | Docker, Compose, Render (`render.yaml`) |
| Evaluation | Harness present, never run | Harness present — publishing results is still open work (see Results above) |

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
  queue, audit log, usage/cost tracking, system health
- [`REPORT_PAGE_REDESIGN.md`](REPORT_PAGE_REDESIGN.md) — the patient-safe diagnosis disclosure
  redesign, section by section
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

**Hosted**: configured for Render via `render.yaml`. Connect the repo, set env vars in the Render
dashboard, deploy — auto-deploys on push to `main`.

**Self-hosted**: `docker compose up --build` from the repo root — runs the API alongside its own
Postgres and Redis containers. `loop1/Dockerfile` builds a non-root, multi-stage image with
`/health` (liveness) and `/ready` (DB + Redis connectivity) endpoints.
