# Task 23 — week1.md Task 7: Docker + compose

Continues week1.md's original task sequence (Task 6 done — `TASK22_REDIS_SESSIONS.md`). Task 7
depends on Tasks 5 and 6, both done.

## Hard constraint, agreed with the user up front

This environment has no `docker`, `docker-compose`, or `trivy` installed. None of week1.md's Task
7 acceptance checks (`docker compose up --build`, image size, `docker run --rm diffdx whoami`, a
trivy scan) could actually be executed. Per explicit direction, everything below was still written
— carefully, static-review-only — but **none of it has been built or run**. Test with
`docker compose up --build` on a machine with Docker before trusting any of it for a real
deployment.

## A real bug found and fixed first (this part *is* fully verified, no Docker needed)

`requirements-web.txt` — the file the Dockerfile is required to install from — was missing
`alembic`, `sqlalchemy`, `pyjwt`, `slowapi`, `pydantic-settings`, and their transitive
dependencies entirely. `requirements.txt` (the full manifest) had the identical gap. Both predate
the relational-schema/JWT-auth/rate-limiting work (Tasks 1/2/5) — only `pyproject.toml`/`uv.lock`
(kept current via `uv add`) actually reflected what the app needs.

Confirmed via a throwaway clean venv: `pip install -r requirements-web.txt` then `import web.api`
raised `ModuleNotFoundError: No module named 'slowapi'`. Diffed the missing package set against
`uv export`'s authoritative output and added exact matching pins to both files. Re-verified from a
**second, fully clean** venv: `pip install -r requirements-web.txt` → `import web.api` succeeds →
`uvicorn web.api:app` boots → `GET /docs` returns 200. Landed as its own first commit on this
branch — building a Docker image on the old file would have shipped a container that crashed on
its first request.

**Also found, not fixed (separate, deeper, flagged explicitly)**: `loop1.retrieval`'s `faiss`/
`fastembed` imports are lazy (deferred to first call), and `requirements-web.txt` deliberately
excludes them ("no ML/embedding packages needed at runtime"). Any real diagnostic-session turn
that reaches exemplar retrieval in a web-only deployment would hit an `ImportError` at that call
site. Whether this is actually reachable in production today, or something bypasses it, wasn't
investigated — that's a genuine architecture question (does the web tier need retrieval, or should
it call an embedding service?), not a Dockerfile problem.

## What was added

- **`GET /health` / `GET /ready`** (`src/diffdx/routers/health.py`) — didn't exist anywhere
  before. `/health` is unconditional liveness. `/ready` does a real `SELECT 1` against the DB and,
  if `REDIS_URL` is set, a real `PING`, returning 503 on failure — same "infra failure becomes
  503" pattern already established in `session_store.py`. Verified live: both return 200 against a
  healthy dev setup; confirmed (indirectly, via a deliberately-broken `DATABASE_URL`) that a dead
  DB fails the app at startup already (pre-existing `_seed_doctor_accounts()` behavior, unrelated
  to this change) rather than silently serving a broken `/ready`.
- **`loop1/Dockerfile`** — multi-stage (`builder`/`runtime`), `python:3.12-slim`, installs from
  `requirements-web.txt` only (with a comment documenting the split, per week1.md's ask), non-root
  `appuser` (uid 1000) via `USER appuser` before `CMD`, `HEALTHCHECK` hitting `/health`. Copies the
  runtime data paths the AI layer reads by path at call time — `config.yaml`, `prompts/`,
  `test_cases/`, `exemplars/`, `alembic/` — confirmed each of these via grep (`loop1.config`,
  `loop1.doctor`, `loop2.critic.critic`, `sessions.py`'s `_CASES_DIR`), not guessed.
- **`loop1/docker-entrypoint.sh`** — runs `alembic upgrade head` then `exec uvicorn` (week1.md:
  "API entrypoint runs alembic upgrade head before starting uvicorn"). Doctor seeding is already
  idempotent on every app startup (`_seed_doctor_accounts`), not duplicated here.
- **`loop1/.dockerignore`** — per week1.md's list plus the obvious extras already visible in this
  repo's own `.gitignore`.
- **`docker-compose.yml`** (repo root) — `api` (builds `loop1/Dockerfile`), `db`
  (`postgres:16-alpine`, named volume, `pg_isready` healthcheck), `redis` (`redis:7-alpine`, named
  volume, `redis-cli ping` healthcheck). `api` depends on both being healthy, reads secrets from
  `loop1/.env` via `env_file`, with `DATABASE_URL`/`REDIS_URL` overridden to the compose service
  DNS names via `environment:` (anything in a stale local `.env` pointing at `localhost` or unset
  would be wrong inside the compose network). **Still requires a real `GROQ_API_KEY`** in
  `loop1/.env` — that's inherent to the app (`Settings.groq_api_key` has no default, same as the
  existing Render deployment), not something Docker can route around.
- **`render.yaml`**: `PYTHON_VERSION: 3.11.0` → `3.12.2` — week1.md explicitly calls out this
  drift (README already said 3.12, `.python-version` was already `3.12.2`; `render.yaml` was the
  one file still wrong).

## Verification

- **Fully verified, no Docker needed**: the requirements-web.txt/requirements.txt fix (clean-venv
  boot test, twice); the `/health`/`/ready` routes (live HTTP against the dev server); YAML
  validity of `docker-compose.yml` and `render.yaml`; shell syntax of `docker-entrypoint.sh`; full
  `pytest` — 337-338 passed, same 17-18 pre-existing/unrelated failures, unchanged baseline.
- **NOT verifiable here, explicitly not claimed as tested**: `docker compose up --build` actually
  succeeding, the entrypoint's migration step running correctly inside a real container, final
  image size (target <400MB per week1.md), `docker run --rm diffdx whoami` printing `appuser`, a
  `trivy image` scan for HIGH/CRITICAL vulnerabilities.

## Out of scope

Fixing the faiss/fastembed-missing-from-web-runtime gap (flagged above, real but separate).
Two-replica load-testing behind a proxy (needs Docker running). Adding `alembic upgrade head` to
Render's own `startCommand` (found while reading `render.yaml` that it never runs migrations
either — a real, separate gap, but outside "fix render.yaml's Python version," not touched).
`docs/` (README/ARCHITECTURE.md/DECISIONS.md rewrite) — week1.md Task 8, separate.
