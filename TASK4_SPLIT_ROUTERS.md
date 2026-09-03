# DiffDx v2 — Task 4: split the monolith (continued)

Spec for whoever picks this up next. Read fully before writing code.
Branch: `feat/split-routers` (this session already has commits on it —
continue there, don't start fresh).

---

## 0. Where this picks up

The `/api/auth/*` domain (11 routes) has been split out as a **proven,
verified pattern** — use it as the template for every other router. Done
and verified this session:

- `diffdx/config.py` — all 15 env vars the code reads, typed via
  pydantic-settings, fails fast on missing `GROQ_API_KEY` naming it,
  `log_disabled_integrations()` logs which optional integrations are off
  instead of silently no-op'ing. Handles blank-but-present `.env` values
  (`KEY=`) as unset, matching every existing `os.environ.get()` call's
  `if not key:` behavior.
- `diffdx/dependencies.py` — `get_current_user`, `require_role("doctor")`.
- `diffdx/schemas/auth.py` — the 4 Pydantic request models, moved verbatim.
- `diffdx/routers/auth.py` — all 11 `/api/auth/*` routes, wired into
  `web/api.py` via `app.include_router(auth_router)`.

**Verified, not just written:**
- `diff /tmp/routes_before.txt /tmp/routes_after.txt` — empty (dump routes
  the same way both times: `[print(f'{list(r.methods)[0]:7} {r.path}') for
  r in app.routes if hasattr(r, 'methods')]`, sorted)
- Live smoke test: register → login → /me → /profile → add dependent → get
  dependents → confirmed 401 without a token — all identical to before
- Full `pytest tests/` — same 17 pre-existing failures either way (verified
  via `git stash` to confirm they fail identically on unmodified code),
  zero new failures introduced. Note: `test_retrieval.py`'s exemplar tests
  are flaky/non-deterministic independent of this work — two runs on
  *unmodified* code produced different wrong exemplar sets each time (see
  `SYSTEM_DOCS.md`'s own troubleshooting note on unseeded MMR tie-breaking).
  Don't be alarmed if the exact failing count shifts by one between runs.
- `web/api.py`: 3,892 → 3,633 lines (only 1 of 8+ domains extracted so far)

## 1. The pattern to repeat, exactly

For each remaining router domain:

1. `grep -n '@app\.\(get\|post\|patch\|delete\|put\)("/api/<prefix>' web/api.py`
   to find every route in that domain, plus any Pydantic request models
   used only by those routes (check with `grep -n "ClassName" web/api.py`
   — if it's used elsewhere, don't move it, or move it somewhere shared).
2. Read the full route bodies from `web/api.py` — copy them verbatim into
   the new router file. Do not rewrite from memory or "improve" logic
   while moving it; that's a different, riskier change than this task.
3. Any route using `_get_user_from_request(request)` + manual 401 check →
   simplify to `user: dict = Depends(get_current_user)` (identical
   behavior, less repetition — this is the one deliberate simplification
   this task's pattern makes).
4. Any route using `_require_doctor(request)` (web/api.py line ~1795 as of
   this writing — re-check, it'll have moved) → consider whether
   `Depends(require_role("doctor"))` is equivalent. It likely isn't
   exactly — `_require_doctor` probably does more than a role check (look
   it up specifically). Don't assume; verify by reading it.
5. For every other helper the moved routes call (`_load_appointments`,
   `_save_doctors`, `_sessions`, `_repo_root`, etc.) — import it **lazily,
   inside each route function**, from `web.api`. Do not import at module
   top level; `web.api` is what includes these routers, so a top-level
   import back would be circular. This is temporary scaffolding — see §3.
6. Delete the moved route definitions and moved-only Pydantic models from
   `web/api.py`. Add `from diffdx.routers.<name> import router as
   <name>_router` near the top and `app.include_router(<name>_router)`
   near the other `include_router` calls.
7. Regenerate `/tmp/routes_after.txt` and diff against `/tmp/routes_before.txt`
   (keep the original from this session — don't regenerate the "before"
   file after any domain has already moved, or you're comparing against a
   moving target). **Must be empty** after every domain, not just at the
   end — catching a break early is a lot cheaper than debugging it after
   all 8 domains have moved.
8. Re-run the relevant smoke test for that domain + `pytest tests/` (same
   baseline failures, no new ones).

## 2. Target layout (from the original spec, unchanged)

```
loop1/src/diffdx/
  main.py                 # app factory, middleware, exception handlers, lifespan — NOT DONE
  config.py               # done
  dependencies.py         # started — add more as each domain needs them
  routers/
    auth.py               # done (11 routes)
    sessions.py            # /api/session/*, /api/cases/* — NOT DONE
    appointments.py        # /api/appointments, /api/patient/appointments/* — NOT DONE
    doctors.py              # /api/doctor/*, /api/doctors/* — NOT DONE
    messaging.py            # /api/messages/* — NOT DONE
    files.py                 # upload/download routes — NOT DONE
    pages.py                 # HTML-serving routes — NOT DONE
    health.py                 # /health, /ready — NOT DONE (these may not exist yet — check)
  services/               # business logic, no FastAPI imports — EMPTY, see §4
  schemas/
    auth.py                 # done
    <others as domains move>
```

Suggested order for the remaining domains — roughly smallest/most
self-contained first, saving the biggest and most stateful for once the
pattern is well-proven:

1. `doctors.py` (`/api/doctors/*`, read-mostly, low risk)
2. `messaging.py` (`/api/messages/*`, self-contained)
3. `files.py` (upload/download — check what actually exists; some of this
   may already be woven into appointments routes rather than separate)
4. `sessions.py` (`/api/session/*`, `/api/cases/*` — the AI diagnostic
   flow; higher-traffic, test carefully)
5. `appointments.py` (`/api/appointments/*`, `/api/patient/appointments/*`,
   `/api/doctor/appointments/*` — the biggest and most stateful domain;
   this is where `_sessions`, booking, test orders/prescriptions/referrals
   all live — save for when the pattern is well-proven and consider
   whether it needs splitting further than one file, since it alone may
   exceed 400 lines)
6. `pages.py` (HTML-serving routes — check what's actually left; a lot of
   the frontend is static files served via `StaticFiles`, this may be a
   short file or may not be needed at all)
7. `health.py` — check if `/health`/`/ready` exist yet; if not, this is
   new functionality (fine — the spec explicitly wants a Dockerfile
   `HEALTHCHECK` hitting `/health` in Task 7)

## 3. Real remaining work this pass didn't do

Be honest about these in the next PR rather than claiming full compliance:

- **The lazy `from web.api import ...` inside every route function is
  scaffolding, not the end state.** The actual blob-store helpers
  (`_db_load`/`_db_save`, `_load_users`/`_save_users`, `_load_appointments`
  /`_save_appointments`, etc.) and shared state (`_sessions`, `_TOKENS`,
  `_USER_CACHE`) still live in `web/api.py`. Once **all** routers are
  split out, `web/api.py` should have nothing left except that shared
  state/helpers and the app assembly — at which point those helpers should
  move into a proper shared module (e.g. `diffdx/legacy_store.py`) and
  every router's lazy imports become normal top-level imports, since the
  circularity concern disappears once `web.api` isn't what the routers
  depend on anymore.
- **No `services/` layer yet.** `routers/auth.py`'s route functions still
  mix request handling and business logic inline, same as the original —
  this pass didn't introduce the services split the spec asks for
  (business logic that raises domain exceptions, routers that translate
  them to HTTP, `services/` importing zero FastAPI). Worth doing per
  domain as each router gets split, or as a dedicated follow-up pass once
  all domains are out of `web/api.py`.
- **`main.py` doesn't exist.** `app = FastAPI(...)`, the CORS/no-cache
  middleware, and the `@app.on_event("startup")` handler are all still in
  `web/api.py`. Moving this is entangled with moving `_sessions`/scheduler
  state — do it once most/all routers are split, not before, or `main.py`
  will just import back from `web.api` the same way routers currently do.
- **`@app.on_event("startup")` is still deprecated**, not yet replaced
  with a `lifespan` context manager — same reasoning, do this alongside
  `main.py`.
- **Doctor seeding still runs on every boot** (`_seed_doctor_accounts()` in
  the startup handler) — the spec wants this moved to
  `scripts/seed_doctors.py` since seeding on every boot is a race in a
  multi-replica deployment. **Deliberately not done this pass**: removing
  it from startup changes operational behavior (a fresh database gets zero
  doctors unless someone remembers to run the script), which is a real
  regression risk for Render/Railway deploys if it ships without also
  updating the deploy config to run the seed script as a release step.
  Coordinate that before making this change, don't just move the code.
- **`os.environ.setdefault("CRITIC_MODEL", ...)` at import time** — still
  there (`web/api.py`, near the top). `config.py`'s `critic_model` field
  already provides the same default properly; the `setdefault` call can be
  deleted once something actually reads `settings.critic_model` instead of
  `os.environ.get("CRITIC_MODEL", ...)` at each call site. Not yet wired
  in — `config.py` exists but nothing consumes it yet outside its own
  tests.
- **The `sys.path.insert` hack at the top of `web/api.py` is still
  there.** Removing it requires fixing packaging in `pyproject.toml`
  properly (the spec's suggestion) — not attempted this pass, since it's
  liable to break `PYTHONPATH`-dependent invocations used throughout this
  session (`PYTHONPATH=src:. .venv/bin/python ...`) and elsewhere in the
  repo (scripts, tests) if done carelessly. Worth its own focused pass
  with the full test suite and every entry point (uvicorn, scripts/,
  pytest) re-verified.

## 4. Acceptance checklist (from the original spec)

- [ ] `diff /tmp/routes_before.txt /tmp/routes_after.txt` is empty
      (verify after **every** domain moves, not just at the end)
- [ ] No file in `src/diffdx/` exceeds 400 lines
- [ ] `grep -r "import fastapi\|from fastapi" src/diffdx/services/` returns
      nothing (once `services/` has content — currently empty, so this
      trivially passes without proving anything yet)
- [ ] All existing tests pass unmodified (same pre-existing failures are
      fine; verify via `git stash` comparison if unsure whether a failure
      is pre-existing)
- [ ] Manual smoke: register → login → start session → 3 turns → view
      report → book appointment → doctor sees it. Every page loads.
      (Only the register/login/profile/dependents slice of this has been
      verified so far — the rest needs the corresponding routers split
      first.)

## 5. When to stop and ask

Same ground rules as the earlier tasks:

- The route diff is non-empty and the fix isn't obvious
- A test can only pass by deleting it or weakening its assertion
- The frontend needs changes beyond what's already scoped (this task
  shouldn't need any frontend changes at all — it's a pure backend
  restructure with zero behavior change)
- Anything requires touching the original `DiffDx` repo (not this mirror)

Report after each domain: which routes moved, what the route diff and test
suite showed, what's still stubbed/deferred (the lazy imports, missing
services layer) — don't report the whole task done until every domain in
§2's table says DONE and `web/api.py` is down to just shared
state/helpers and app assembly.
