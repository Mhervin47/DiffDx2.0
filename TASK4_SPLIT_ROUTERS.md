# DiffDx v2 — Task 4: split the monolith

Branch: `feat/split-routers`. This file's earlier version (written mid-way
through the work) is superseded — updated here to reflect the actual final
state after the appointments/doctor/patient domain (the big one) finished.

---

## 0. Status: router split is done

Every route that lived in the original `web/api.py` (3,892 lines) has been
extracted into a domain router under `src/diffdx/routers/`, except two
deliberately-left routes (see §1). All routers verified via the same
process each time: read the route bodies verbatim (never rewritten from
memory), lazy `from web.api import ...` for any shared helper, a
deterministic route-diff dump proving zero route changes, a live smoke
test of the actual moved behavior (not just that the route registers),
and a full `pytest` run confirming the same pre-existing failure baseline
(18 failures — `test_critic.py` x4, `test_ddxplus_loader.py` x6,
`test_patient_simulator.py` x5, `test_retrieval.py` x2 (flaky/
non-deterministic, see `SYSTEM_DOCS.md`), `test_router.py` x1 — none of
this session's doing).

```
src/diffdx/
  config.py                      # done — all 15 env vars, typed
  dependencies.py                # done — get_current_user, require_role
  routers/
    auth.py                      # done (11 routes)
    messaging.py                 # done (5 routes)
    sessions.py                  # done (8 routes: cases x2, tts, session x5)
    session_booking.py           # done (2 routes: suggested-tests, book)
    session_test_files.py        # done (2 routes: suggested-test-files x2)
    doctors.py                   # done (2 routes: bare doctor directory + slots)
    pages.py                     # done (7 HTML-serving routes)
    appointments.py              # done (11 routes — appointments core)
    appointments2.py             # done (11 routes — plan/results/history/etc.)
    appointments3.py             # done (8 routes — schedule/analytics/intake/refill)
    appointments4.py             # done (10 routes — patient files, cancel, rating)
    appointments5.py             # done (5 routes — symptom-history, waitlist x4,
                                  #   blocked-dates x3, tags, renewal-reminders)
    appointments6.py             # done (3 routes — second-opinion request/inbox/respond)
  schemas/
    auth.py                      # done
    sessions.py                  # done
    appointments.py              # done (all ~27 request models for that domain)
  services/                      # still empty — see §3
  main.py                        # does not exist yet — see §3
```

`web/api.py`: **3,892 → 892 lines** (77% smaller). What's left in it:

- App assembly (`FastAPI()`, CORS/no-cache middleware, `include_router`
  calls, `@app.on_event("startup")`, static file mount)
- Shared state used by many routers: `_sessions`, `_TOKENS`,
  `_USER_CACHE`, `_session_test_uploads`
- Shared blob-store helpers used by 3+ already-extracted routers each:
  `_db_load`/`_db_save`, `_load_users`/`_save_users`,
  `_load_appointments`/`_save_appointments`, `_load_doctors`/`_save_doctors`,
  `_load_waitlist`/`_save_waitlist`, `_load_blocked_dates`/`_save_blocked_dates`,
  `_send_email_notification`, `_require_doctor`, `_get_user_from_request`,
  `_get_final_differential`, `_load_report_from_disk`, `_repo_root`, etc.
- Doctor-account seeding (`_seed_doctor_accounts`, runs on startup —
  deliberately not moved to a release-step script yet, see §3)
- **2 routes, deliberately left**: `GET /api/tts/voices` and
  `POST /api/tts` (ElevenLabs proxy). The POST is permanently-shadowed
  dead code — `sessions.py`'s Sarvam `/api/tts` is registered first and
  always wins (verified live: hitting `/api/tts` returns the Sarvam
  path's error, not ElevenLabs's). Not worth a whole extraction pass for
  two routes, one of which is unreachable; noted here instead so nobody
  re-discovers this as a "bug."

## 1. Two routes intentionally not moved

`GET /api/tts/voices` + `POST /api/tts` (ElevenLabs) — see above. If a
future pass wants full closure, they'd go into a small `routers/tts.py`,
but there's no behavior reason to.

## 2. Real remaining work this pass didn't do

Be honest about these — the router split itself is done, but the spec's
full target state (see the original week1.md) isn't:

- **The lazy `from web.api import ...` inside every route function is
  scaffolding, not the end state.** Now that *every* router is split out,
  the circularity reason for lazy imports (routers importing from the
  module that imports the routers) could be resolved by moving the shared
  helpers/state listed in §0 into a proper module (e.g.
  `diffdx/legacy_store.py`) that has no dependency on `web.api`, then
  converting every router's lazy imports to normal top-level ones. Not
  done this pass — it's a mechanical but wide-reaching change (touches
  every router file) better done as its own dedicated, easily-verified
  pass (grep-replace + rerun the full route-diff/smoke/pytest pipeline).
- **No `services/` layer yet.** Routers still mix request handling and
  business logic inline, same as the original. The spec's
  business-logic-raises-domain-exceptions / router-translates-to-HTTP
  split hasn't been introduced anywhere.
- **`main.py` doesn't exist.** `app = FastAPI(...)`, CORS/no-cache
  middleware, and the startup handler are still in `web/api.py`. This is
  now easier than before (all routers are already split), but still
  entangled with moving `_sessions`/scheduler state — do it alongside the
  legacy_store extraction above, not before.
- **`@app.on_event("startup")` still deprecated**, not yet a `lifespan`
  context manager — same reasoning as `main.py`.
- **Doctor seeding still runs on every boot.** Moving it to
  `scripts/seed_doctors.py` is a real operational behavior change (a
  fresh DB gets zero doctors unless the script runs) — needs deploy
  config coordination first, deliberately not done here.
- **`os.environ.setdefault("CRITIC_MODEL", ...)` at import time** — still
  there; `config.py`'s `critic_model` field already covers this but
  nothing reads `settings.critic_model` yet at the actual call sites.
- **The `sys.path.insert` hack at the top of `web/api.py`** — still
  there; fixing it needs a proper `pyproject.toml` packaging pass with
  every entry point (uvicorn, scripts/, pytest) re-verified.

## 3. Acceptance checklist (from the original spec)

- [x] Route diff empty after every domain move (verified via a
      deterministic per-method route dump, re-run after each commit)
- [x] No file in `src/diffdx/` exceeds 400 lines (largest is
      `appointments3.py` at 394; `appointments5.py` was split into
      `appointments5.py` + `appointments6.py` specifically to fix a
      447-line overage)
- [ ] `grep -r "import fastapi\|from fastapi" src/diffdx/services/` — moot,
      `services/` doesn't exist yet (§2)
- [x] All existing tests pass unmodified (18 pre-existing failures,
      unchanged baseline, verified after every domain move)
- [x] Manual smoke, extended beyond the original slice: register → login
      → start session (real LLM call) → book appointment (direct-book) →
      doctor sees it (appointment detail) → intake → refill → second
      opinion request/inbox/respond → tags → waitlist → blocked-dates →
      renewal-reminders → symptom-history. Every domain has a live
      end-to-end test on record, not just route registration.

## 4. Two shipped-then-fixed regressions (worth remembering)

During the biggest extraction (the appointments/doctor/patient domain,
~50 routes across 6 files), two shared helpers were accidentally deleted
from `web/api.py` mid-extraction — each time because a line-range removal
swept up a helper function sitting between the routes being moved, and
the breakage only surfaces at *call time* (lazy imports), not at route
registration time, so it slipped past route-diff and sampled smoke tests:

- `_patient_appt_or_403` (broke `save_intake`, `request_refill`) —
  caught before pushing further, fixed in the appointments4.py commit.
- `_get_final_differential` (broke `get_doctor_appointment_detail`,
  `get_suggested_tests`, `book_appointment`, `get_routing`) — same commit.

Both were caught by a static AST check written specifically because of
this bug class: it parses every `from web.api import X` /
`from diffdx.routers.X import Y` across `routers/*.py` and confirms `X`
actually exists on the target module. This check is cheap (runs in
under a second) and catches this bug class more reliably than sampled
live smoke tests, since it's exhaustive rather than sampled. Worth
running after *any* future edit to `web/api.py` that removes code near
routes, not just during a router split.
