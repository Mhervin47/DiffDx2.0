# Task 25 — main.py extraction, doctor-seeding script, sys.path.insert removal

Closes out three of the four items flagged as deferred across this whole cutover (the 4th,
`loop1.retrieval`'s faiss/fastembed lazy imports, turned out not to be a bug — see the correction
already pushed to `TASK23_DOCKER_COMPOSE.md`).

## Scope decision

The `sys.path.insert` hack turned out much bigger than `web/api.py` alone. The project's editable
install (`pip install -e .`/`uv sync`) is actually broken — its generated `.pth` files exist in
site-packages but add nothing to `sys.path` (confirmed live: `import diffdx` fails from the venv
without `PYTHONPATH=src` set, despite the package being "installed"). `PYTHONPATH=src` is the only
thing making imports work today, set independently in `Procfile`, `render.yaml`, the `Dockerfile`,
and pytest's config — with the identical `sys.path.insert(...)` pattern *also* duplicated in
`web/api.py`, `run_web.py`, `demo.py`, `demo_with_critic.py`, and 16 files under `scripts/`.

Per explicit direction: **only `web/api.py`'s own copy is removed this pass.** It's uniquely
redundant — every real way `web.api` gets imported already sets `PYTHONPATH=src` independently.
The 16 standalone scripts keep theirs — they're meant to run directly
(`python scripts/foo.py`) with no wrapper, and removing it would be a real usability regression,
not a cleanup. Actually fixing the broken editable install (so nothing needs `PYTHONPATH`
anywhere) is real, separate work, declined for this pass.

## What changed

**`src/diffdx/main.py`** (new) — the actual app factory per week1.md's target layout: `FastAPI()`
instantiation, CORS/rate-limit middleware, `register_exception_handlers`, `AuditLogMiddleware`/
`NoCacheStaticMiddleware`, every `include_router` call, the static mount (kept last), and a
`lifespan` async context manager replacing the deprecated `@app.on_event("startup")` — calling
`settings.log_disabled_integrations()` and `_start_reminder_scheduler()`. The reminder-loop
functions (`_send_reminder_email`, `_reminder_loop`, `_start_reminder_scheduler`) and the dotenv +
`CRITIC_MODEL` bootstrap moved here verbatim — this is now the first `diffdx.*` code that runs.

**`src/diffdx/routers/tts.py`** (new) — the two TTS routes that stayed directly on `app` since
Task 4 (`/api/tts/voices`, the permanently-shadowed `/api/tts`), moved out since `main.py`'s job
is assembly, not routes. Mirrors `routers/health.py`'s shape.

**`web/api.py`** is now a thin shim: `from diffdx.main import app`, plus the same
`diffdx.legacy_store` re-exports it already had — `from X import Y` binds `Y` in this module's own
namespace, so `from web.api import Y` keeps working for `tests/test_auth.py`,
`test_appointment_composer.py`, `test_appointment_repositories.py`, `scripts/concurrency_demo.py`
unchanged. No `sys`/`os`/`Path`/`sys.path.insert` in this file at all now. `web.api:app` keeps
working unchanged for every deploy config that targets it — `Procfile`, `render.yaml`, the
`Dockerfile` — none of those needed to change.

**`scripts/seed_doctors.py`** (new) — the 23-doctor seed list and seeding loop, moved out of
`web/api.py`'s startup hook (`_seed_doctor_accounts`/`_DOCTOR_SEED`, both deleted). Same
idempotency check as before (`UserRepository.get_by_doctor_id` before creating). Doctor seeding no
longer happens automatically on every app boot — `TASK4_SPLIT_ROUTERS.md` §2's stated reason:
"seeding on every boot is a race in a multi-replica deployment." Wired into the two places that
actually need to seed a fresh database: `docker-entrypoint.sh` (runs it right after `alembic
upgrade head`, before starting uvicorn) and `render.yaml`'s `buildCommand`. Render's `buildCommand`
never ran migrations either (found while wiring this in — `alembic upgrade head` was missing there
too, a related gap `TASK23_DOCKER_COMPOSE.md` had explicitly flagged and left out of scope). Since
`seed_doctors.py` hard-depends on the schema already existing, both `alembic upgrade head` and the
seed script were added to `buildCommand` together — the seed step would just fail on Render
without the migration step also being there.

**Dockerfile**: copies only `scripts/seed_doctors.py` (not the whole `scripts/` directory, which
is offline AI-layer tooling with heavier, not-installed-here dependencies) — required a
`.dockerignore` fix too (`scripts/` → `scripts/*` + `!scripts/seed_doctors.py`; Docker won't
re-include a file whose parent directory pattern excludes the whole directory). Also fixed a stale
comment there referencing the faiss/fastembed finding as if it were still an open gap.

## Verification

- `PYTHONPATH=src uv run python -c "from diffdx import main; print(main.app)"` and
  `PYTHONPATH=src uv run python -c "import web.api; print(web.api.app)"` both clean.
- Confirmed `import web.api` **fails** without `PYTHONPATH=src` set (expected — that's the whole
  point of removing the redundant fallback) and **succeeds** with it set, matching every real
  entry point's actual invocation.
- Full `pytest`: 337 passed, same 17-18 pre-existing/unrelated failures. Specifically ran
  `test_auth.py`, `test_appointment_composer.py`, `test_appointment_repositories.py`,
  `test_session_store.py` together (69 passed) — proves the `web.api` re-export shim works for
  every test file that imports helpers from it directly.
- Live smoke test: booted `uvicorn web.api:app` for real, confirmed `/health`, `/ready`, `/docs`,
  `/api/tts/voices` (the moved route) all return 200, confirmed a normal domain route
  (`/api/doctors`, exercising the full CORS/rate-limit/audit-log middleware chain) still works,
  confirmed the reminder scheduler starts (log line, now from `diffdx.main` instead of `web.api`)
  and `log_disabled_integrations()` fires, confirmed the deprecated `@app.on_event` warning is
  gone, and confirmed no doctor-seeding log line appears on startup anymore.
- `scripts/seed_doctors.py` run twice against a genuinely fresh dev DB (`rm` + `alembic upgrade
  head`): first run seeds all 23 doctors (log line per doctor), second run logs "All 23 doctor
  accounts already exist — nothing to seed" and creates 0 new rows — confirmed via `sqlite3` row
  count, not just the log message.
- `sh -n docker-entrypoint.sh` and a YAML-parse of `render.yaml` both clean after their edits.
- **Not verified**: the actual Docker image build/run with these changes (Dockerfile/
  `.dockerignore`/`docker-entrypoint.sh` all changed since Task 23's last real
  `docker compose up --build`) — needs a rebuild on a machine with Docker to confirm live, same as
  every other Docker change this cutover. The Render `buildCommand` path is unverified by nature
  (no way to deploy to Render from here) — best-effort config change matching the Docker one, not
  a live-tested claim.

## Out of scope

Actually fixing the broken editable install (confirmed with the user, declined this pass).
Removing `sys.path.insert` from the 16 `scripts/*.py` files, `run_web.py`, `demo.py`,
`demo_with_critic.py` — all keep their own copy, by design.
