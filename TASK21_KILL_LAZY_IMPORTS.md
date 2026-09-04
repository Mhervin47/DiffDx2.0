# Task 21 — Kill the lazy-import scaffolding

Backlog item 3 of 3 from the post-Task-18 "fix all the remaining backlog" pass.
`TASK4_SPLIT_ROUTERS.md` §2 flagged this explicitly: every router extracted from the original
`web/api.py` monolith still did lazy `from web.api import X` inside each route function body,
to dodge the circular import (`web/api.py` imports and registers the routers, so the routers
couldn't import from `web.api` at module load time). The doc called this "scaffolding, not the
end state" and recommended a dedicated pass to fix it. This is that pass, scoped to the
extraction + import conversion only (not the larger `main.py`/lifespan move flagged in the same
section, and not the two items the doc said need deploy-config coordination first — moving
doctor-seeding to a script, and the `sys.path.insert` hack).

## What moved

A full grep across `src/diffdx/routers/*.py` for every `from web.api import ...` found 42 unique
names, 79 call sites, across 13 files. Every one of the 42 was confirmed self-contained — no
dependency on `app`, no dependency on anything defined only after `app = FastAPI(...)`, no
reassignment of the two mutable dict singletons (`_sessions`, `_session_test_uploads`) anywhere
outside their own definition — so all 42 moved verbatim into a new module,
**`src/diffdx/legacy_store.py`**: the Postgres/SQLite `_db_load`/`_db_save` subsystem, Sarvam AI
helpers, user/appointment/waitlist/blocked-date/file blob load-save wrappers,
`_compose_user_dict`/`_compose_appointment_dict`/`_dt_iso`, `_ensure_relational_appointment`, auth
helpers (`_hash_password`/`_verify_password`/`_user_from_access_token`/`_get_user_from_request`/
`_require_doctor`), `_send_email_notification`, `_get_final_differential`/`_load_report_from_disk`,
and the two shared mutable singletons themselves.

Two things needed recomputing, not verbatim copy-paste: `_repo_root` (was
`Path(__file__).parent.parent`, `__file__`-relative to `web/api.py`'s own location — recomputed
as `Path(__file__).resolve().parents[2]` for `legacy_store.py`'s location, same convention
already used in `diffdx/db/engine.py` and Task 19's `_REPO_ROOT`) and `_static_dir` (same fix,
derived from the corrected `_repo_root`).

**Verified byte-for-byte, not from memory**: every moved function was extracted from `web/api.py`
via Python's `ast` module (exact line ranges by name) and diffed against what landed in
`legacy_store.py`. This caught two real fabrication errors mid-task — a corrupted middle section
of `_compose_appointment_dict` (a placeholder block that was never actually in the source) and
wrong return types on `_load_waitlist`/`_save_waitlist` (`dict`/`{}` instead of the real
`list`/`[]`) — both introduced by copying from memory instead of the literal source, both caught
by the diff before landing, neither shipped.

## What didn't move

`app` assembly, middleware, `include_router` calls, `@app.on_event("startup")`, the two routes
still defined directly in `web/api.py` (`GET /api/tts/voices`, `POST /api/tts` — see
`TASK4_SPLIT_ROUTERS.md` §1), static mount, `_seed_doctor_accounts`/`_DOCTOR_SEED`/
`_reminder_loop`/`_send_reminder_email`/`_start_reminder_scheduler` (app-lifecycle, not in the
42-name list), the `sys.path.insert` hack, doctor-seeding-on-boot. `web/api.py` shrank from 1227
to 492 lines; the leftover dead comment-header blocks where moved code used to sit were collapsed
into short pointer comments rather than left as debris.

## The routers

Each of the 13 router files had its per-function `from web.api import X, Y` lines replaced with
one top-level `from diffdx.legacy_store import X, Y` — done programmatically (regex to collect
every lazily-imported name per file, `ast` to find the correct top-level insertion point after
the file's existing imports) rather than by hand across 79 call sites, then every result verified
by parsing. `APISession` (used in `sessions.py`) imports from `web.api_session` directly, not
`legacy_store` — it was never actually defined in `web.api` in the first place, just re-exported
there.

`web/api.py` itself now imports everything it used to define locally from
`diffdx.legacy_store` — since `from X import Y` binds `Y` in the importing module's own
namespace, `from web.api import Y` (used by `tests/test_auth.py`,
`tests/test_appointment_composer.py`, `tests/test_appointment_repositories.py`,
`scripts/concurrency_demo.py`) keeps working unchanged, no extra re-export syntax needed.

## Verification

- `uv run python -c "import web.api"` clean — the real circular-import canary, since top-level
  imports execute at module load time (lazy ones don't), so any mistake here would surface
  immediately instead of only at call time (the exact bug class `TASK4_SPLIT_ROUTERS.md` §4
  documents from the original router split, where two helpers were accidentally deleted and the
  breakage wasn't caught until routes were actually exercised).
- `grep -rn "from web.api import" src/diffdx/routers/*.py` — zero matches.
- Full `pytest`: 330-331 passed (the 1-test swing is the already-documented `test_retrieval.py`
  flakiness), same 17-18 pre-existing/unrelated failures, unchanged baseline.
- Live smoke test against a real booted server, touching at least one route from every one of the
  13 router files that had lazy imports: register/login (`auth.py`), doctor directory
  (`doctors.py`), direct booking (`appointments3.py`), doctor notes (`appointments.py`), doctor
  summary (`appointments2.py`), list patient files (`appointments4.py`), patient tags
  (`appointments5.py`), second-opinion inbox (`appointments6.py`), message threads
  (`messaging.py`), the root static page (`pages.py`), starting a diagnostic session
  (`sessions.py`), suggested-tests and suggested-test-files (`session_booking.py`,
  `session_test_files.py`) — all behaved identically to before the restructuring.

## Out of scope

`main.py` extraction + `@app.on_event` → `lifespan` conversion (larger app-assembly move, same
section but explicitly not requested this pass). Moving doctor-seeding to a script, and fixing
the `sys.path.insert` hack — both flagged in the original doc as needing deploy-config or
packaging coordination first. The `CRITIC_MODEL` env-var-vs-`settings` inconsistency found while
scoping (`web/api.py` and `web/api_session.py` both still do `os.environ.setdefault("CRITIC_MODEL",
...)` at import time instead of reading `settings.critic_model`, and a few call sites read the
env var directly) — unrelated pre-existing tech debt, not touched.

This closes out all three items from the post-Task-18 "fix all the remaining backlog" pass (file
storage — Task 19, remaining blob-only field schema — Task 20, this cleanup — Task 21).
