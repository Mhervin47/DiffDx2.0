# DiffDx v2 — Task 6: identity-domain blob → relational cutover

Branch: `feat/identity-cutover`. Phase 1 of the blob-store → relational cutover flagged as
remaining work in `TASK4_SPLIT_ROUTERS.md` §2 and `TASK2_REPOSITORY_LAYER.md` — scoped to
`users`/`patients`/`doctors`/`dependents` only, by explicit sign-off. Appointments, messaging,
waitlist, second opinions, blocked dates, session uploads, files, and the doctor *directory* blob
(bookable slots, `hospital`/`rating`/`avatar_initials`/nested `profile` — separate from identity)
stay blob-backed as tracked future phases.

## 1. What changed

**Migration script bug fix** (`scripts/migrate_blob_to_relational.py::migrate_users_and_doctors`):
the idempotency check gated `Patient`/`Doctor` sub-row creation on "does a `User` row exist" —
Task 5's `shadow_user` already writes a bare `User` row on every login, so any already-logged-in
user would be seen as "already migrated" and permanently skipped, leaving no `Patient`/`Doctor`
row. Fixed to gate on the role-appropriate *sub-row's* existence instead; if the bare `User` row
exists but the sub-row doesn't, its fields are synced from the blob (still source of truth
pre-cutover) and the missing sub-row is added. Verified live against the dev DB before the fix:
5 users pre-shadowed, 0 patients/doctors/dependents — confirming the bug was live, not
hypothetical. Migration run for real: 74 users → 39 doctors + 35 patients, idempotent on re-run
(0 written, 74 skipped).

**Repository additions** (`src/diffdx/repositories/users.py`): `list_all`, `get_by_doctor_id`,
`update_patient`, `update_doctor` (both accept an explicit `name` kwarg since it's a `User`
column, plus `**fields` for role-specific columns, partial-update semantics — only passed keys
are applied), `update_dependent`, `delete_dependent` (returns whether a row was actually deleted,
for 404 handling).

**`web/api.py`**: `_load_users()`/`_user_from_access_token()` rewritten to compose the legacy
blob-shaped dict from relational rows instead of reading the blob — same signatures, same return
shape, so the ~55 call sites elsewhere that just read `user["..."]`/`user.get("...")` off these
needed zero changes (same adapter-shim pattern Task 5 used for the blob-token → JWT swap).
`_user_from_access_token` (the hot path, every authenticated request) does a single indexed
row fetch, not a full-table scan. `_USER_CACHE` is retired — once the hot path is one indexed
query, the cache wasn't pulling its weight, and removing it removes the "did every write site
remember to invalidate" risk entirely. `_add_session_to_user`/`_update_session_in_user` (session
history, not identity data) now write to a new standalone blob collection `user_sessions` instead
of nesting inside the retiring `users` blob. `_seed_doctor_accounts` creates the 23 hardcoded
seed identities via `UserRepository.create_doctor` directly. `_save_users`/`_db_save("users", ...)`
is fully retired — grep-confirmed zero non-definition call sites remain.

**`routers/auth.py`**: `register`/`login`/`refresh` create/read real `User`+`Patient`-or-`Doctor`
rows directly instead of blob dict + `_save_users`; `register`'s email-uniqueness check and
`login`'s lookup now use `UserRepository.get_by_email` (indexed query, not a full-blob Python
scan). `_issue_token_pair` no longer calls `shadow_user` — redundant now that register/login
create the real row directly (the method itself is kept, still used by
`audit.py::log_audit_event`, untouched this phase). `update_profile`/`get_dependents`/
`add_dependent`/`update_dependent`/`delete_dependent`/`delete_user_session` converted to
repository calls; six routes gained a `db: Session = Depends(get_session)` param they didn't
have before. `update_profile` role-branches: `name` always applies at the `User` level regardless
of role; patient-only fields silently no-op for a doctor account (matches the blob's prior
"meaningless extra keys" behavior — no behavior change beyond storage backend).

**IDOR check added during implementation** (not flagged by the plan's design review, caught
while writing the code): the blob version of `update_dependent`/`delete_dependent` was implicitly
scoped to the caller's own dependents (it only ever searched within
`users[user_id]["dependents"]`). The new `UserRepository.update_dependent`/`delete_dependent`
take a bare `dependent_id` with no inherent ownership scoping — an unscoped conversion would have
let any authenticated patient edit or delete *any* dependent by guessing/enumerating UUIDs. Fixed
by adding an explicit ownership check at the route level (`dep_id` must appear in the caller's
own `list_dependents()` result) before calling either repository method.

**`routers/appointments2.py::update_doctor_profile_details`**: gained a `db` param; after the
existing directory-blob writes (unchanged), now also mirrors `name`/`specialty`/`hospital` into
the relational `Doctor` row via `UserRepository.update_doctor` — only for keys actually present
in the request body (no null-clobber on a partial update). Without this, `/api/auth/me` and login
responses (which read the relational row) would silently drift stale relative to the directory
after a doctor edits their profile — verified live: edited `specialty` via this route, confirmed
both `/api/doctors` (directory) and `/api/auth/me` (relational) reflect the change identically.

**`tests/test_auth.py`**: `_make_blob_doctor` renamed to `_make_doctor` and rewritten to build via
`UserRepository.create_doctor` directly (the old blob-write approach would have been silently
invisible post-cutover, since nothing reads the blob for identity anymore). Module docstring
updated — the "known, pre-existing limitation" it used to describe (blob-store user creation
colliding with the live dev DB, worked around with uuid4-suffixed emails) is resolved for user
data specifically: `_load_users`/`_user_from_access_token`/`_seed_doctor_accounts` all resolve
`get_sessionmaker()` by name at call time, so they pick up this module's monkeypatched isolated
test engine automatically, same as every relational call already did.

## 2. Sequencing

Landed as one change, not staged across deploys — reads and writes flipped to relational
together, avoiding a window where a write is invisible to reads (a user edits their profile, sees
the old value until a later deploy). Order: fix + run the migration first (data at rest correct),
then the composer + all write-site conversions together, migration run once more immediately
before code changes were exercised live (idempotent — 0 written, 74 skipped on the second run).

## 3. Verification

- **Migration correctness**: row counts (74 users → 39 doctors + 35 patients, 0 dependents —
  none existed in the blob) cross-checked against the blob's role breakdown; confirmed every one
  of the 5 originally shadow-written users now has a populated sub-row (the specific regression
  case for the bug fixed in §1); confirmed idempotent on re-run.
- `pytest tests/test_auth.py` — 14/14 passing, including the rewritten `_make_doctor`.
- `grep -rn "_save_users"` — zero non-definition hits, confirming no stray blob-write path
  survived before the function was removed.
- Full `pytest` — 282 passed, 18 failed (same 18 pre-existing, unrelated failures as the Task 5
  session baseline — retrieval/critic/ddxplus/patient-simulator — confirmed no new regressions).
- **Live smoke test** (real booted `uvicorn`, real HTTP, same approach as Task 5): register → 
  `/api/auth/me` → update profile (patient) → get profile, full dependents CRUD (add → list →
  update → delete → confirm empty), seeded-doctor login (`doctor_id`/`specialty` populated
  correctly from migrated data), a doctor hitting `/api/auth/profile` PATCH (confirms the
  role-branch doesn't 500 attempting a `Patient` update on a doctor's `user_id`),
  `get_user_sessions`'s write-then-immediate-reread path (empty case, no error), and
  `appointments2.py`'s doctor profile-details edit confirming both the directory blob and the
  relational `Doctor` row reflect a `specialty` change identically (seeded a temporary directory
  entry for this specific check, since the doctor directory blob doesn't exist yet in this dev DB
  — pre-existing, out-of-scope state, not something this task's DB backup/restore touched;
  removed the temporary entry afterward).

## 4. Explicitly out of scope (tracked as future phases)

Appointments (heaviest: ~74 call sites, nested test-orders/prescriptions/referrals/files fanning
into ~5 child tables, a partial-unique slot constraint the blob never enforced), messaging,
second opinions, waitlist, blocked dates, session uploads, files, and the doctor *directory* blob
all stay blob-backed. `audit.py::log_audit_event`'s `shadow_user` call is untouched. Same
`services/`/`main.py`/lazy-import backlog from `TASK4_SPLIT_ROUTERS.md` §2 is unchanged.
