# Task 23 — week1.md Task 7: Docker + compose

Continues week1.md's original task sequence (Task 6 done — `TASK22_REDIS_SESSIONS.md`). Task 7
depends on Tasks 5 and 6, both done.

## Hard constraint, agreed with the user up front — later resolved

This environment has no `docker`, `docker-compose`, or `trivy` installed, so the Dockerfile/compose
files were initially written static-review-only. The user ran `docker compose up --build` on their
own machine (Docker Desktop was already installed but not on `PATH` and not launched — fixed by
`open -a Docker` and adding `/Applications/Docker.app/Contents/Resources/bin` to `PATH`) and it
now **builds and runs successfully end to end**: both stages build, `db`/`redis` pass their
healthchecks, `docker-entrypoint.sh` runs `alembic upgrade head` against the real Postgres
container, all 21 doctor accounts seed, the app boots, and Docker's own `HEALTHCHECK` is actively
polling `/health` and getting 200s. See the second bug below, caught by this real run — the kind
of thing static review alone couldn't have found.

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

**Second real bug, caught by the actual `docker compose up --build` run against real Postgres**:
`diffdx/db/engine.py` deliberately rewrites `postgres://`/`postgresql://` URLs to
`postgresql+psycopg://` (psycopg **3**, not psycopg2) for SQLAlchemy's engine.
`requirements-web.txt` only had `psycopg2-binary` (needed separately, by `legacy_store.py`'s raw
connection pool for the blob store — confirmed both drivers are genuinely needed simultaneously),
missing `psycopg`/`psycopg-binary` (v3) entirely — `alembic upgrade head` failed inside the
container with `ModuleNotFoundError: No module named 'psycopg'`. The earlier clean-venv
verification never caught this because it only exercised the SQLite fallback path (no
`DATABASE_URL` set) — it never actually hit Postgres dialect resolution. Fixed by adding
`psycopg==3.3.5`/`psycopg-binary==3.3.5` (matching what `requirements.txt` already had from the
`uv export` diff), re-verified via `create_engine("postgresql+psycopg://...")` resolving cleanly
without a live server, then confirmed for real by rebuilding — see above.

**Found, not fixed (separate, deeper, flagged explicitly)**: `loop1.retrieval`'s `faiss`/
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
- **Verified live via a real `docker compose up --build` run** (on the user's machine, Docker
  Desktop): both image stages build; `db` (`postgres:16-alpine`) and `redis` (`redis:7-alpine`)
  pass their healthchecks before `api` starts; `docker-entrypoint.sh`'s `alembic upgrade head`
  runs cleanly against the real Postgres container (both migrations apply); all 21 doctor accounts
  seed on startup; the app boots and serves `/health` with the Docker `HEALTHCHECK` itself getting
  repeated 200s.
- **Still NOT verified** (not attempted this round): `docker compose down -v && docker compose up`
  clean-rebuild idempotency; final image size (target <400MB per week1.md); `docker run --rm
  <image> whoami` printing `appuser`; a `trivy image` scan for HIGH/CRITICAL vulnerabilities;
  `/ready`'s Redis-down and DB-down paths against the real containers (only tested against a
  deliberately-broken `DATABASE_URL` outside Docker, pre-Docker-verification).

## Out of scope

Fixing the faiss/fastembed-missing-from-web-runtime gap (flagged above, real but separate).
Two-replica load-testing behind a proxy (needs Docker running). Adding `alembic upgrade head` to
Render's own `startCommand` (found while reading `render.yaml` that it never runs migrations
either — a real, separate gap, but outside "fix render.yaml's Python version," not touched).
`docs/` (README/ARCHITECTURE.md/DECISIONS.md rewrite) — week1.md Task 8, separate.
