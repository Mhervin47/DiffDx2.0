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
