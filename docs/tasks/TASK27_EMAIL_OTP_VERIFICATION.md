# Task 27 — email OTP verification for patient registration

## Scope

Patient self-registration only. Doctors are exclusively seeded via `scripts/seed_doctors.py`
(fixed 23-doctor list) — there is no doctor self-registration endpoint to attach OTP to, so
doctor accounts are created pre-verified (`email_verified=True` explicit in
`UserRepository.create_doctor`), unchanged behavior.

Uses only infrastructure already in the app — Resend (free tier, `RESEND_API_KEY`) as the
primary send path, SMTP as fallback — no new provider, no new env var.

## Design

**`users.email_verified`** (new column, `sa.true()` server default) — existing rows (already-
registered patients, all seeded doctors) stay verified with no regression; only new patient
registrations explicitly pass `email_verified=False`.

**`email_otps`** (new table, 1:1 with `users`) — `code_hash` (SHA-256, never plaintext),
`expires_at` (10 min TTL), `attempts` (capped at 5, then forces a resend). A 6-digit code is only
1-in-a-million, so the attempt cap — not the code length — is what makes it safe against guessing.
Resending overwrites the same row rather than appending, so only the latest code is ever valid.

**`src/diffdx/otp.py`** — code generation (`secrets.randbelow`, not `random`), hashing, constant-
time comparison (`secrets.compare_digest`), and `send_otp_email` (Resend first, SMTP fallback via
the existing `legacy_store._send_email_notification`, silent no-op if neither configured — same
"email is optional in dev" contract every other notification in this app follows).

**`POST /api/auth/register`** — creates the user unverified, sends the code, returns
`{"verification_required": true, "email": ...}` — **no tokens**, this is a breaking response-shape
change from before. Re-registering with an email that already has an unverified account updates
that account in place (name/password) and resends a code, rather than 409ing on an account that
was never actually usable.

**`POST /api/auth/verify-otp`** — checks the code, marks the account verified, deletes the OTP
row, and issues the token pair (auto-login on success — no separate login call needed).

**`POST /api/auth/resend-otp`** — deliberately vague on whether the account exists (always
`{"sent": true}`) to avoid leaking registered-email info to an unauthenticated caller.

**`POST /api/auth/login`** — now checks `email_verified`; unverified accounts get `403` with
`{"message": ..., "verification_required": true, "email": ...}` so the frontend can route straight
to the OTP-entry screen instead of just showing a generic error.

Rate limits (matching the existing `/register`/`/login` pattern): 5/min register, 10/min
verify-otp, 3/min resend-otp.

## Frontend

Both existing registration entry points updated — `web/static/login.html` (standalone signup tab)
and `web/static/report.html` (inline modal during booking) — each gained an OTP-entry step shown
right after register, and both `doSignIn`/`doAuthLogin` now catch the 403
`verification_required` shape and route to that same step instead of just showing "login failed."

## Verification

- `py_compile` clean on every touched/new Python file.
- `tests/test_auth.py` rewritten: 7 existing register-dependent tests updated for the new
  register → verify-otp two-step flow (a `_capture_otp_emails` fixture monkeypatches
  `diffdx.routers.auth.send_otp_email` to capture codes instead of actually sending, so tests don't
  need a real Resend/SMTP config), plus 4 new tests (register doesn't issue tokens pre-verify,
  login blocks unverified, wrong-code rejection doesn't burn the real code, resend issues a working
  new code).
- **Full `pytest tests/test_auth.py` run started in this environment — result pending** (this
  sandbox has a known, previously-documented `import httpx`/DB-driver hang issue unrelated to this
  change; see TASK26). Please run `pytest tests/test_auth.py -q` to confirm before deploying.
- Not verified live end-to-end (no way to receive a real email in this environment) — please run
  through registration once against a real `RESEND_API_KEY` or `SMTP_HOST` to confirm the actual
  email arrives and looks right.

## Out of scope

Doctor self-registration + OTP (no such flow exists yet — if one gets built, wire OTP into it from
the start). SMS OTP (no free provider exists for it — see prior conversation). 2FA-on-every-login
(user explicitly chose registration-only verification, not re-verification on each login).
