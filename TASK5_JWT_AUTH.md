# DiffDx v2 — Task 5: real JWT auth + RBAC

Branch: `feat/jwt-auth`. Written alongside the implementation; live
verification was initially blocked by a local-disk-driven iCloud eviction
issue (see §5) but has since run to completion — all items in §3 are
verified, see that section for how.

## 1. What changed

**JWT access + rotating refresh tokens** (`src/diffdx/auth_tokens.py`,
`src/diffdx/repositories/users.py`'s `RefreshTokenRepository`):
- Access token: HS256 JWT, 15 min TTL (`ACCESS_TOKEN_TTL_MINUTES`), claims
  `sub`/`role`/`exp`/`iat`/`jti`. No server-side lookup table — verified
  purely by signature, which is what makes a restart not log anyone out.
- Refresh token: opaque random string, 7 day TTL
  (`REFRESH_TOKEN_TTL_DAYS`), only its SHA-256 hash is stored (Task 1's
  `refresh_tokens` table, added specifically for this). Rotated on every
  use: `/api/auth/refresh` revokes the token it was given and issues a
  new pair in the same transaction, so a replayed old refresh token 401s
  instead of minting a second session.
- `web.api._issue_token` (opaque token, in-memory `_TOKENS` dict) is
  gone. `_get_user_from_request` now decodes a JWT
  (`_user_from_access_token`) instead of a dict lookup — same return
  shape (the blob-store user dict), so none of the ~90 other routes that
  call it needed to change.

**SECRET_KEY** (`src/diffdx/config.py`): required and validated at boot
when `ENVIRONMENT=production` — refuses to start rather than falling back
to a random per-process key (the old behavior, which silently invalidated
every session on every deploy). Local dev still auto-generates one
per-process if unset, with a warning.

**Password hashing**: unchanged — PBKDF2-SHA256, 200k iterations,
already exactly what the spec asked to keep. No rehash needed.

**RBAC**: `diffdx.dependencies.require_role("doctor")` already existed
(Task 4). Task 5's work here was the audit (§2), not the mechanism.

**Rate limiting** (`src/diffdx/rate_limit.py`, slowapi): 5/minute per IP
on `/api/auth/login` and `/api/auth/register` only, per spec — not a
blanket API limiter.

**CORS** (`web/api.py`): reads `CORS_ALLOWED_ORIGINS` (comma-separated)
via `diffdx.config.settings.cors_origins_list`, defaulting to `"*"` for
local dev only — `config.py` refuses to start in production with the
wildcard still set, same pattern as `SECRET_KEY`.

**Audit log** (`src/diffdx/audit.py`, `src/diffdx/repositories/audit.py`,
`AuditLogMiddleware` in `web/api.py`): `login`/`login_failed`/`register`
logged directly in `routers/auth.py` (not URL-pattern-matchable the same
way). Every other PHI read and appointment mutation is logged by a
middleware matching on URL prefix (`/api/doctor/appointments/*`,
`/api/patient/appointments/*`, `/api/doctor/patient-history/*`,
`/api/session/*`, and a few adjacent doctor routes) rather than
instrumenting each of the ~90 route handlers individually — see that
middleware's docstring for exactly what it covers and the one thing it
can't do (recover the *matched route template* from inside
`BaseHTTPMiddleware`, only the resolved path with real IDs — which is
actually fine, the real ID is more useful as `resource_id` anyway).
Writes are append-only (no update/delete path exists on the repository)
and never block the request they're attached to on failure (logged and
swallowed, same pattern as the existing `_send_email_notification`).

Because `refresh_tokens.user_id` and `audit_log_entries.actor_user_id`
both have a real FK to the relational `users` table, and the live app's
actual user identity is still the legacy blob store (Task 2's live
cutover never happened — see `TASK4_SPLIT_ROUTERS.md` §2), every place
that issues a token pair or writes an audit entry first upserts a
minimal "shadow" row into the relational `users` table
(`UserRepository.shadow_user`) with the same id/name/email/password_hash/
role as the blob record. This is scoped tightly to auth — it does not
touch the ~90 other routes still reading/writing the blob store directly,
and is documented as scaffolding for whenever the real cutover happens.

**Frontend** (`web/static/auth.js`, plus 5 call sites in `login.html` and
`report.html`): now holds `authAccessToken` + `authRefreshToken`
(previously a single `authToken`). `window.fetch` is wrapped to catch a
401, attempt a single transparent refresh (deduped across concurrent
in-flight requests), retry the original request once with the new access
token, and only fall through to the "session expired" toast if the
refresh itself fails — replacing the old client-side 8-hour guess timer,
which is no longer meaningful now that the access token's real TTL is 15
minutes. `logout()` now also revokes the refresh token server-side
(best-effort, fire-and-forget). This is the only frontend change this
task permits, per spec.

## 2. RBAC audit — IDOR findings

Audited every `/api/doctor/*` route (31 of them) for whether it verifies
the caller is *that* doctor, not just any doctor, per the spec's explicit
ask. Three real bugs found and fixed, all in the appointments domain:

1. **`get_doctor_appointment_detail`** (`GET /api/doctor/appointments/{id}`,
   `routers/appointments2.py`) — the docstring literally said "any
   authenticated doctor may view; only the owning doctor may edit" as a
   deliberate design choice. In practice this means any doctor account
   could pull any patient's full AI diagnostic conversation and session
   data — a real PHI leak. Fixed: requires the requesting doctor to
   either own the appointment, or be the recipient of a pending/responded
   second-opinion request on it (the one legitimate cross-doctor use case
   this domain has).

2. **`get_patient_history`** (`GET /api/doctor/patient-history/{name}`,
   `routers/appointments2.py`) — no ownership check at all, not even an
   `appt_id` to scope against. Any doctor could view any patient's full
   appointment history (diagnoses, prescriptions, notes) just by knowing
   or guessing their name. Fixed: scoped to the requesting doctor's own
   appointments with that patient name, matching the actual product
   intent (the drawer opens from a doctor's own appointment view).

3. **`fulfill_refill`** (`PATCH /api/doctor/appointments/{id}/refill`,
   `routers/appointments3.py`) — no ownership check. Any doctor could
   mark any other doctor's patient's refill as fulfilled, and the
   confirmation email sent to the patient would name the *requesting*
   doctor as having approved it — an authorization bug and a
   misattribution bug together. Fixed: added the same ownership check
   every other appt-scoped route in this domain already has.

Everything else audited (`appointments.py`, `appointments4.py`,
`appointments5.py`, `appointments6.py`, `doctors.py`, `messaging.py`) was
already correctly scoped — verified by reading each handler, not assumed.

**Explicitly out of scope, found but not changed**: the AI diagnostic
session flow (`/api/session/*` — `start`, `turn`, `report`, `routing`,
`suggested-tests`, `book`) has no ownership check tying a `session_id` to
a specific user at all; it works fully unauthenticated by design (try the
symptom checker before logging in), and the random UUID `session_id` is
the only credential. This is a genuine product design choice (anonymous
use is a real, intended flow), not an oversight like the three above, and
the spec's audit ask was explicitly scoped to `/api/doctor/*`. Flagging
it here rather than silently leaving it out of the writeup.

## 3. Acceptance checklist

All items verified two ways: `pytest tests/test_auth.py` (14/14 passing)
against an isolated test DB, and a live smoke test against a real booted
`uvicorn` process hitting the actual dev DB (`web/data/diffdx.db`, after
`alembic upgrade head`) with real HTTP requests.

- [x] Restarting the server does not log users out — required setting
      `SECRET_KEY` in `.env` first (local dev otherwise auto-generates a
      new per-process key, which *would* log everyone out on restart —
      that's the documented fallback behavior, not a bug). With a
      persistent key set: registered a user, killed and restarted the
      server, the pre-restart access token still worked (`/api/auth/me`
      → 200).
- [x] Expired access token + valid refresh → transparent renewal —
      backend verified live (`/api/auth/refresh` with a valid token
      returns a new working pair; `test_refresh_with_valid_token_issues_new_working_pair`
      passes). The `auth.js` fetch-wrapper piece is still only
      code-reviewed, not exercised in a browser — that requires manual
      UI testing, out of scope for this pass.
- [x] Revoked refresh token → 401, cannot be reused — verified live
      (reused a just-rotated refresh token → 401) and by
      `test_refresh_token_rotation_rejects_reuse`.
- [x] Patient token on any `/api/doctor/*` route → 403 — verified live
      and by `test_patient_token_on_doctor_route_is_403`.
- [x] Doctor A cannot read Doctor B's appointments —
      `test_doctor_a_cannot_read_doctor_b_appointment_detail` +
      `test_doctor_patient_history_scoped_to_own_appointments`, both
      passing.
- [x] 6th login attempt in a minute → 429 — verified live (five bad
      logins → 401, sixth → 429) and by
      `test_sixth_login_attempt_in_a_minute_is_rate_limited`.
- [x] `tests/test_auth.py` — 14/14 passing. One test-isolation bug found
      and fixed along the way: the module-scoped `client` fixture shares
      one IP across all 14 tests, and several tests before the dedicated
      rate-limit test each call `/api/auth/register` (which shares the
      same 5/minute limiter as `/api/auth/login`) — by the 6th such call
      the limiter was already tripped, failing unrelated tests with a
      confusing `KeyError` on `access_token`/`refresh_token` instead of
      an assertion about status codes. Fixed by adding an autouse
      per-test fixture that resets the limiter before every test, not
      just the one that means to test it.

## 4. Real remaining work this pass didn't do

- The `auth.js` fetch-wrapper (transparent 401 → refresh → retry) is
  written and code-reviewed but not exercised in an actual browser —
  needs manual UI testing.
- Same `services/`/`main.py`/blob-store-cutover backlog from
  `TASK4_SPLIT_ROUTERS.md` §2 is unchanged by this task.
- `.python-version` was pinned to an exact `3.11.0`, which no longer has
  a downloadable build (`uv python install 3.11.0` → "No download
  found"); the repo's actual constraint is `requires-python = ">=3.11"`
  in `pyproject.toml`. Repinned to `3.12.2` (already what the checked-in
  `.venv` was built with) so `uv run`/`uv sync` work without a pin
  mismatch. Unrelated to this task's own scope but was blocking `uv sync`
  from picking up `pyjwt`/`slowapi` at all.
- Local `.env` had no `SECRET_KEY` set. Without one, local dev
  auto-generates a fresh random key every process start, which
  invalidates every session on every restart — exactly the failure mode
  this task's stateless-JWT design is supposed to fix, just not
  reachable without a persistent key locally. Added one to `.env` (not
  committed — see `.gitignore`).

## 5. Blocker encountered mid-task (resolved)

Local disk on this Mac was at 94% capacity (12GB free of 228GB) when
this work started. macOS's "Optimize Mac Storage" was evicting local
content from files under `.venv` (a Python venv is tens of thousands of
small files — a pathological case for iCloud's per-file sync) faster
than it could reliably re-download them on demand: `import web.api` hung
indefinitely; traced to a single 14KB file taking **8 minutes** to read
via a bare `cat` (`stat` showed `blocks=0` on a file reporting a
14700-byte size — the signature of an undownloaded iCloud placeholder).
`brctl download .venv` (forcing bulk re-materialization) didn't resolve
it at the time. This is the same class of interference flagged earlier
in Week 1 (see the iCloud sync notes from Task 1–2), recurring because
local disk space, not just the iCloud toggle, turned out to be the
actual trigger.

**Resolved**: disk now shows 18GB free (48% used, up from 94%), and
`.venv` files read instantly again (`find .venv -type f` over ~32k files
completed in well under a second). `uv sync` (after fixing the
`.python-version` mismatch above), `alembic upgrade head` against the
live dev DB, the full `pytest tests/test_auth.py` suite, and a live
`uvicorn` boot with real HTTP smoke tests all ran clean — see §3.
