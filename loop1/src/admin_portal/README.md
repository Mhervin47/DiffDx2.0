# Admin Portal — Phase 1

Produces and displays evidence on whether the actor-critic architecture
outperforms a traditional single-shot LLM diagnostic approach — accuracy,
safety recall, cost, and usability — with every caveat attached, not hidden.

## Run order

All commands run from inside `loop1/`, using the project's own virtual
environment.

1. **Generate the eval set** (one-time; no LLM calls):
   ```
   python scripts/curate_eval_set.py
   ```
   Requires `loop1/data/DDXPlus_Raw/` (the raw DDXPlus dataset — `release_test_patients`,
   `release_evidences.json`, `release_conditions.json`) to already be present on disk.
   Writes `data/ddxplus_eval_set.json` (20 patients).

2. **Run the actor-critic eval** (real LLM calls — see cost warning below):
   ```
   python -m loop2.runners.phase7_eval
   ```
   Writes `data/phase7_sessions/` and `data/phase7_critiques/`.

   `--dry-run` on this script now makes genuinely zero LLM calls (verified by
   running it with no API keys set at all). It originally called
   `SimulatedPatient.initial_complaint()` before checking the dry-run flag —
   found while testing this admin portal, and fixed with explicit
   authorization to touch this one otherwise-read-only file for this specific
   bug (the fix moves the early-return dry-run branch above the simulator
   call; nothing else in the file changed). This is the second existing-file
   edit in this codebase, alongside `main.py`'s router registration — see
   the note on that in Section 1 of the build brief, now superseded for this
   one case.

3. **Run the baseline** (real LLM calls):
   ```
   python src/admin_portal/eval/baseline_eval.py --mode both
   ```
   `--dry-run` on this script *does* fully skip LLM calls — every real call
   in its own code path is gated behind the dry-run check. Writes
   `data/admin_portal/baseline_sessions/`, `baseline_evals/`, `baseline_usage/`.

4. **Label red flags** (no LLM calls, no clinical judgment required for the
   default pass):
   ```
   python src/admin_portal/eval/labeling/red_flag_labels.py
   ```
   Uses DDXPlus's own dataset-provided condition severity field
   (`data/DDXPlus_Raw/release_conditions.json`'s `"severity"`, 1=most acute,
   5=least acute) rather than a hand-picked disease list — see the module
   docstring for why. `--severity-max` controls the red-flag cutoff (default
   2). `--list` shows counts and label provenance; `--interactive` lets you
   review/override; `--set PATIENT_ID true|false --labeled-by NAME` applies a
   direct clinician override that survives re-running the default pass.
   Optional — safety-recall numbers are skipped gracefully without this step.

5. **Generate the report**:
   ```
   python src/admin_portal/eval/compare_report.py
   ```
   Writes the *working* copy: `data/admin_portal/comparison_report.json`.

6. **Publish it** — do not skip this step, it is the only thing that makes
   the report visible to the running app:
   ```
   python src/admin_portal/eval/publish_report.py
   ```
   Copies the working copy to `web/admin_portal/data/comparison_report.json`
   — the one location the Dockerfile's `COPY ./web` step actually ships, and
   the only location `routers/admin_portal.py` reads from.

7. **Run the app** and check `/api/admin/evidence`, `/api/admin/quality`,
   `/api/admin/config` (all under the `diffdx.main` app — no separate admin
   process). All three respond with real data once published, or a graceful
   `{"status": "not_generated", ...}` placeholder before that.

## What every number in this report does and doesn't prove

- **Completion-token counts are always estimates**
  (`len(json.dumps(doctor_output)) // 4`), never measured — `loop1.llm` only
  ever returns `prompt_tokens`, for any model, in this codebase.
- **actor_critic's latency is `null` unless Phase 2's optional Task 9 has
  been done AND `phase7_eval.py` has been run since.** This line originally
  said "always null" — no longer accurate as of Phase 2 (see below); kept
  updated here rather than left to quietly mislead a future reader.
- **actor_critic's cost figure is a known undercount of true production
  cost.** It's doctor-call cost only, from this offline harness's own
  session logs — it excludes critic, `profile_updater`, and `compressor`
  cost. That's a real gap, not a deliberate scope choice: the critic **does**
  run on every turn of a real deployed session (`web/api_session.py`'s
  `_fire_critic`), same as `profile_updater`/`compressor`. Live per-session
  cost covering all four call sites is tracked separately in the admin
  portal's Cost & Usage panel (`llm_usage_events`); this offline benchmark
  figure just hasn't been wired to pull from that same source yet.
- **Any significance result (McNemar) is directional, not confirmatory.**
  At n=20 patients, treat it as suggestive evidence, not proof, regardless of
  which way the p-value points.
- **Safety recall rests on a dataset-provided severity field, not a
  clinician's review** — a real improvement over a hand-picked disease list
  (see step 4), but the severity *cutoff* used to mean "red flag" is still an
  editorial choice, and no clinician has reviewed the resulting labels unless
  someone runs `--set` overrides. `label_provenance` in the report always
  shows how many labels are automatic vs. a real named override — check it
  before trusting the safety numbers.
- **The doctor model is held constant across all three systems, but the full
  per-turn pipeline is not** — `profile_updater` and `compressor` run on
  every actor-critic turn and never run in the baseline at all. This is a
  genuine structural asymmetry between what's being compared, not just a
  turn-count difference.

## Cost and time warning

Nothing above has necessarily been run yet just because the code exists. A
full real run costs real API calls and real wall-clock time: up to 15 turns
× 20 patients × 2 models (doctor + profile_updater) plus critic scoring per
turn for the actor-critic side, plus roughly 20 patients × 2 modes × 1-3
calls each for the baseline. That's realistically several hundred LLM calls
for one complete pass. "The code works" does not mean "the numbers already
exist" — budget for it before running steps 2–3 for real.

---

# Phase 2

Adds live-session usage telemetry, a detailed health panel, a read-only
audit log viewer, and a Data Subject Request (DSR) console — all behind
`require_role("admin")` — plus admin-account seeding and optional CLI
latency instrumentation. Built against `ADMIN_PORTAL_PHASE2_BUILD_BRIEF.md`,
whose own Section 1 corrected several assumptions in the original request
against the real source. A few of those corrections turned out to still be
wrong in additional ways once actually implemented and tested — documented
at the end of this section, not silently fixed.

## Decisions made (the brief's five gates)

- **D1 (where usage records live): Option B — a new Postgres table
  (`llm_usage_events`)**, not the brief's default JSONL. Chosen for
  durability across restarts/deploys; the trade-off is two more
  existing-file touches than the JSONL option would have needed
  (`db/models/__init__.py` + a new Alembic migration). There is **no
  `ADMIN_PORTAL_USAGE_DIR` environment variable** in this build — that's an
  Option-A-only concept.
- **D2**: confirmed — `loop1/web/api_session.py` is the live session loop;
  `loop1/src/loop1/session.py` is the CLI/offline harness. Both ended up
  instrumented: D2 itself only required the former; the latter is Task 9
  (optional by the brief's own default, done here because it was explicitly
  requested).
- **D3 (admin account creation): Option A** — a new, standalone
  `loop1/scripts/seed_admin.py`, run manually. Not wired into
  `docker-entrypoint.sh` or `render.yaml`.
- **D4 (Phase 1 frontend relocation): not needed.** Phase 1's pages were
  never actually unserved — its own router already serves `web/admin_portal/`
  generically via path-validated `FileResponse` routes
  (`/admin_portal/*.html`, `/admin_portal/js/*.js`), a fact the brief's
  Section 1.3 didn't know because it predates that fix. **Every Phase 2 page
  and script lives under `web/admin_portal/` too** — `dsr.html`, `audit.html`,
  and their JS — not `web/static/admin/` as the brief's literal paths say.
  Dropping a new `.html`/`.js` file into `web/admin_portal/` (or
  `web/admin_portal/js/`) makes it servable immediately, zero new routes,
  zero `main.py` edits, by design (see `admin_portal/routers/admin_portal.py`'s
  module docstring).
- **D5 (DSR erase / audit trail)**: implemented as specified — pseudonymise
  (`uuid.uuid5` of a fixed namespace + the real user id), never delete.

## Files Phase 2 added

```
loop1/
  src/admin_portal/
    instrumentation/
      __init__.py
      usage_logger.py
    routers/
      admin_portal_usage.py
      health.py
      audit.py
      dsr.py
  src/diffdx/db/models/usage.py            # LlmUsageEvent (D1 Option B)
  alembic/versions/915ef5eaf914_add_llm_usage_events_table.py
  scripts/seed_admin.py
  web/admin_portal/
    audit.html
    dsr.html
    js/health.js
    js/usage.js
    js/audit.js
    js/dsr.js
```

## Existing files modified, and exactly what changed

| File | Change |
|---|---|
| `loop1/web/api_session.py` | Guarded `admin_portal` import; one private wrapper method (`_generate_turn_with_usage_logged`) around `generate_turn_with_usage`; the three call sites (`initialize`/`final_generation`/`next_question`) now go through it. Return values, `to_dict()`/`from_dict()`, and every other behavior unchanged — diff is 48 lines, entirely inside the one wrapper plus 3 one-line call-site swaps. |
| `loop1/src/loop1/session.py` | Same pattern (Task 9), around the CLI harness's two `generate_turn_with_usage()` call sites. Also sets `os.environ.setdefault("DIFFDX_RUN_CONTEXT", "offline_cli")` at import time, so any run through this module is tagged correctly without every caller needing to set the env var itself. |
| `loop1/src/diffdx/main.py` | Phase 1's one edit stands; Task 11 added 4 more import lines + 4 more `app.include_router(...)` calls for the new routers, placed above the `StaticFiles` mount as required. No reformatting, no reordering, nothing else touched. |
| `loop1/src/diffdx/db/models/__init__.py` | One import + one `__all__` entry for `LlmUsageEvent` (D1 Option B). |
| `loop1/src/loop2/runners/phase7_eval.py` | Not a Phase 2 change — the Phase 1 `--dry-run` fix, listed here only so this table matches `git status` exactly. |

## Environment variables introduced

| Variable | Default | Purpose |
|---|---|---|
| `DIFFDX_RUN_CONTEXT` | `live_web` if unset (api_session.py's behavior); `offline_cli` if `session.py` has been imported (via `setdefault`) | Tags every `llm_usage_events` row so offline/CLI/`phase7_eval.py` runs never get counted as production traffic in `/api/admin/usage`. |
| `ADMIN_PORTAL_ENABLE_DSR_ERASE` | unset (= disabled) | Real (non-dry-run) DSR erasure returns 501 until this is explicitly `true`/`1`/`yes`. Off by default because erasure is irreversible and touches PHI across 6+ tables, the blob store, and two on-disk log locations — too large a blast radius to leave enabled. |
| `ADMIN_SEED_EMAIL` / `ADMIN_SEED_PASSWORD` | none (required) | Read by `scripts/seed_admin.py`; the script refuses to run if either is unset. No default admin password exists anywhere in this codebase. |
| `ADMIN_SEED_NAME` | `"Admin"` | Optional, cosmetic only. |

`ADMIN_PORTAL_USAGE_DIR` from the original brief does **not** exist in this
build — see D1 above.

## Creating the admin account

```
ADMIN_SEED_EMAIL=you@example.com ADMIN_SEED_PASSWORD='a real password' \
    PYTHONPATH=src python scripts/seed_admin.py
```

Run once, manually, after `alembic upgrade head` has created the schema.
Idempotent — running it again against an already-seeded email does nothing.
Log in via the existing `/login.html` page like any other account; the
returned JWT works against `/api/admin/*` the same way it already works
against `/api/patient/*`/`/api/doctor/*`.

**Auth model**: every `/api/admin/*` route — Phase 1's, Phase 2's, and
Phase 3's — is behind `require_role("admin")`, including
`/api/admin/evidence`, `/api/admin/quality`, and `/api/admin/config`.

Those three were originally left deliberately public (`Admin_Portal.md`:
"if a judge has to log in to see accuracy numbers, they will not see
them"), and that's still the right default to revisit if judge/reviewer
access without an account ever matters again — but the project owner
explicitly asked for every `/api/admin/*` route to be consistently
admin-gated (Phase 3), so they were locked down too. `evidence.js`/
`quality.js`/`index.html`'s inline script were all updated accordingly —
unauthenticated visitors get the real "sign in as an admin" prompt
(`admin-common.js`'s `renderAuthRequired`), not sample data, on every page
now, including Evidence and AI Quality.

The `/admin_portal/*.html` page *shells* originally had no auth at all — same as
`doctor-portal.html` today — because they're served by a generic static-file route, not an API
endpoint. At the time that was fine, since every number on every page came from an authenticated
`/api/admin/*` call regardless. **Phase 3 closed this anyway**, with a client-side guard rather
than a server-side one (a server-side page-route guard can't work here — see "Client-side admin
page-shell guard" below for why): a signed-out or non-admin visitor is now redirected before any
admin_portal page's content loads at all, not just before its data does.

## The corrected live-path coverage note

Phase 1 wrote two mandatory cost notes for `comparison_report.json`
claiming (a) the critic never runs in a real deployed session and (b) the
profile updater does. **Both describe the CLI `Session` path
(`phase7_eval.py`) and are backwards for the live web path**
(`api_session.py`):

- The critic **does** run on every live web turn (`_fire_critic`, a
  background thread) — its token usage is still uncounted, but because it
  discards usage via `call_llm()`, not because "it never runs live."
- The profile updater is **disabled** in the live web session — a
  deliberate design choice (an extra Groq call per turn was rate-limiting
  the doctor calls) — so unlike the CLI path, it contributes *zero* real
  production cost, not an uncounted one.
- The compressor runs in both paths and is undercounted in both, for the
  same reason (`call_llm()` again).

`/api/admin/usage`'s `coverage_note` (in `admin_portal_usage.py`) states the
corrected version, and is returned in every response, not just documented
once here. If you're reading `comparison_report.json`'s notes instead,
remember they describe the offline CLI harness only — they aren't wrong in
general, they just don't transfer to the live path.

## Durability: which usage numbers survive a restart

Under D1 Option B (Postgres, not the brief's default JSONL), **everything
`/api/admin/usage` reports is durable** — tokens, cost, latency, and session
counts all survive restarts and deploys, because they're all in Postgres now
(`llm_usage_events` alongside the pre-existing `diagnostic_sessions`). This
is a real difference from the brief's own assumption that only session
counts would survive under its default option — worth knowing if this build
is ever compared against the brief's literal text.

`compare_report.py` (Phase 1, offline) also gained a narrow, optional live
DB dependency from this: `mean_latency_ms_per_session` for `actor_critic`
now queries `llm_usage_events` for `source="offline_cli"` rows, populated
only if Task 9's instrumentation exists **and** `phase7_eval.py` has been
run since. If neither is true, or Postgres isn't reachable at all, it falls
back to `null` with an explanatory note — the offline report still
generates successfully either way, verified directly in both states.

## Where the DSR console reads and writes

See `admin_portal/routers/dsr.py`'s module docstring for the full mapping
and the reasoning behind it. In short:

- **Identity, consultations, appointments, messages, files**: relational
  tables (`users`/`patients`, `diagnostic_sessions`/`session_turns`,
  `appointments`, `message_threads`/`messages`, `uploaded_files`) — deleted
  in one real SQLAlchemy transaction alongside audit pseudonymisation.
- **Appointments and messages are ALSO still blob-dual-written**
  (`store['appointments']`, `store['messages']`) — cleared as a best-effort
  follow-up after the relational transaction commits, since the blob store
  runs on its own connection outside SQLAlchemy's transaction boundary
  (true cross-store atomicity isn't achievable with the current
  architecture — see the docstring for why).
- **Session reports/critiques exist only in the blob**
  (`store['session_report:{id}']`), no relational equivalent — cleared the
  same best-effort way.
- **Two on-disk locations hold PHI outside the database entirely**:
  `logs/sessions/session_{id}.jsonl` and `logs/final_records/final_{id}.json`
  — deleted explicitly, per session id, in the same follow-up step.
- Any failure in that follow-up is surfaced in the erase receipt's
  `warnings` field, never silently swallowed.
- The audit trail (`audit_log_entries`) is the one category never deleted —
  `actor_user_id` is repointed to a stable pseudonym before the user row is
  deleted, so the FK (`ondelete="RESTRICT"`) never fires.

## What is still not measured

- **Compressor and critic token usage in the live web session** — both now
  logged for real (`call_site="compressor"`/`"critic"` on `llm_usage_events`,
  via `call_llm_with_usage()`, not the token-discarding `call_llm()`). This
  used to be a real gap; it isn't one anymore — `/api/admin/usage`'s
  coverage note states what's covered every time it's shown, not just once
  here, and it's the authority if this doc and that note ever disagree.
  `profile_updater` remains the one call site genuinely disabled in the live
  web session (a CLI/offline-only path) — that part of the old gap is real.
- **The actual model that served a request after an HTTP 429 fallback** —
  this is now measured, not a gap: `llm.py`'s `LlmUsage.model_actual` is set
  to the model that actually answered on every successful call, and every
  live call site threads it through to `log_turn_usage()`. `/api/admin/usage`
  surfaces it as `model_actual_breakdown`/`model_actual_breakdown_by_role`.
  Only records logged before this field existed are `null`
  (`model_actual_unknown` in the response).
- **Phase 2's own timing instrumentation only covers the doctor model's own
  call** — same gap as the cost figures, for the same reason.
- **Doctor-account seeding was not reviewed or touched** — this work only
  covers the admin account; `scripts/seed_doctors.py` is unrelated, Phase 1
  territory.

## Discrepancies found and reported, not silently fixed

1. **Section 1.10 of the Phase 2 brief is wrong about user identity.** It
   claims the blob store is authoritative for user records and that DSR
   search must search it. A repo-wide grep confirms `store['users']` is
   never read or written anywhere — identity fully migrated to the
   relational `users`/`patients` tables in an earlier "identity cutover."
   The brief *is* still correct that appointments, messages, and session
   reports/critiques remain blob-based (verified independently before
   relying on any of them). `dsr.py` and `seed_admin.py` both search/write
   the relational tables only, never the blob, for identity.
2. **The brief's own standalone-import-check recipe
   (`cd loop1/src && python -c "..."`) fails for any router that
   transitively imports from `web.*`** — `health.py` does, via
   `diffdx.session_store` → `web.api_session`. Confirmed this is a
   test-recipe gap, not a real deployment problem: the actual Procfile
   invocation (`PYTHONPATH=src python -m uvicorn web.api:app`, run from
   `loop1/`) puts both `src/` and `loop1/` (via Python's own `-m`
   convention) on the path, so it resolves correctly in production and in
   every full-app-boot test run during this build.
3. **Autogenerating the `llm_usage_events` migration nearly produced a
   destructive one.** Alembic's first draft included `op.drop_table('store')`
   in `upgrade()` (and a subtly-wrong recreation of it in `downgrade()`),
   because `store` is created via raw SQL in `legacy_store.py`, not a
   SQLAlchemy model, so autogenerate saw it as "extra" and proposed removing
   it. Caught before ever running against a real database; both lines
   removed, verified `store` survives upgrade and downgrade in both
   directions.
4. **`DiagnosticSession.patient_id` is `ON DELETE SET NULL`, not
   `CASCADE`.** Erasing only the `Patient` row would have silently orphaned
   session/turn PHI instead of deleting it. Sessions are deleted explicitly,
   before the user row, in `dsr.py`.

---

# Phase 3

Locked down `/api/admin/evidence`, `/api/admin/quality`, and `/api/admin/config` behind
`require_role("admin")` (see the "Auth model" section above — this is the change that section
already documents), and added three pages plus one new data domain that weren't covered by
Phase 1 or Phase 2.

## New pages

- **`reports.html`** (+ `js/reports.js`) — the moderation queue for `POST /api/messages/report`
  (a patient or doctor flagging the other side of a conversation for using messaging outside of
  medical care — see `MESSAGE_REPORTING_PLAN.md` at the repo root). Filterable by status
  (open/reviewed/dismissed), with "Mark Reviewed"/"Dismiss" row actions. Backend:
  `admin_portal/routers/reports.py` (`GET /api/admin/message-reports`,
  `PATCH /api/admin/message-reports/{id}`), reading `MessageReport`
  (`diffdx/db/models/reports.py`) — a real relational table, not blob-based, unlike the messaging
  domain it reports on.
- **`architecture-validation.html`** (+ `js/architecture-validation.js`) — a credibility/external-
  validity page, not a live operational dashboard: maps DiffDx's actor-critic-router design
  against MEDDxAgent (arXiv:2502.19175), a peer-reviewed benchmark, with KPI tiles, a concept
  correspondence table, and an interactive metrics explorer (GTPA@1 / avg rank / ΔProgress by
  model+dataset). Content is static/reference data checked into the page, not served from a live
  endpoint — nothing here degrades if the DB is down.
- **`sop.html`** — an operator runbook, also static content, no backing API. Structured as
  Trigger/Meaning/Action/Escalate-if entries grouped by area (Health, DSR, Audit, Evidence &
  Quality, Usage, Accounts) for specific real situations (a health pill going down, about to run a
  real DSR erasure, citing a cost figure externally, creating a new admin account). Read this one
  before doing anything unfamiliar in the console for the first time.
- **`users.html`** (+ `js/users.js`, backend `admin_portal/routers/users_analytics.py`) — user
  growth/engagement/retention analytics, four endpoints under `/api/admin/users/`: `summary`,
  `timeseries` (signups over time), `engagement`, `retention` (cohort-based). Uses a shared
  `_bucketing.py` helper for the time-bucketing logic across all four.

## Client-side admin page-shell guard

Every page shell under `web/admin_portal/` used to be servable to anyone — no auth at all on the
HTML/JS itself, only on the `/api/admin/*` data it fetches (so an unauthenticated visitor saw a
real "sign in as an admin" prompt instead of real data, but could still load the page layout). A
guard in `admin-common.js` now runs before anything else on every page: reads the cached
`authUser` (from `auth.js`, already loaded first on every admin_portal page), and redirects to
`/login.html` if there's no signed-in user, or to `/` if the signed-in user isn't an admin. This
can't be done server-side on the page *route* — a plain browser navigation carries no
`Authorization` header (only `fetch()`/XHR calls do), so a `Depends(require_role(...))` on the
route itself would 401 every legitimate admin landing here too, same reasoning as
`diffdx.routers.pages`'s page-serving guard elsewhere in the app.

## Voice/Sarvam usage tracking

A second usage domain alongside the existing LLM token/cost tracking (D1 Option B, above), same
architecture: a durable Postgres table, not JSONL.

- **`SarvamUsageEvent`** (`diffdx/db/models/usage.py`, alongside `LlmUsageEvent`) — one row per
  Sarvam API call: `operation` (`translate` or `tts`), `call_site`, `language`, `char_count`,
  `latency_ms`, `ok`/`error_type`. No PHI logged — character counts and language codes only, same
  privacy posture as `LlmUsageEvent`.
- **`voice_usage_logger.py`** (`log_sarvam_usage`) — the write path, called from both Sarvam call
  sites: `POST /api/tts` (translate + text-to-speech) and the per-turn patient-answer
  translate-back on any non-English session. Same never-raise contract as `usage_logger.py` — a
  logging failure never breaks the request it's attached to.
- **`GET /api/admin/usage`** now returns a `voice_usage` block alongside the existing LLM figures:
  request counts (ok/failed), total characters, an estimated cost (from `sarvam_pricing.json`'s
  per-1k-character rates — **not verified against actual Sarvam invoices**, same caveat as the LLM
  cost figures elsewhere in this doc), broken down by operation and by language. Surfaced on
  `index.html`'s Voice/Sarvam Usage card.

## What this means for the "every /api/admin/* route is admin-gated" claim above

Phase 3 extended that claim to cover the two new API-backed pages too:
`GET /api/admin/message-reports` and `PATCH /api/admin/message-reports/{id}` are both
`require_role("admin")`, same as every other `/api/admin/*` route, and `users.html`'s four
`/api/admin/users/*` endpoints are gated the same way. `architecture-validation.html` and
`sop.html` have no backing API to gate — their content is static, so there's nothing to
authenticate at the data layer — but as of the client-side guard above, no `/admin_portal/*.html`
page shell is actually reachable by a signed-out or non-admin visitor anymore either, closing the
gap the "Auth model" section above originally described as a non-issue only because the data was
still gated. Both layers now agree: data-gated server-side, shell-gated client-side.
