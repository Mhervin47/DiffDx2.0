# Task 24 — week1.md Task 8: Documentation

Last item in week1.md's original 8-task sequence. Tasks 1-7 are done (Docker/compose merged and
build-tested for real — `TASK23_DOCKER_COMPOSE.md`).

## What was wrong

The root `README.md` (166 lines, the polished "capstone demo" landing page linked from GitHub)
had the exact stale claim week1.md names — "Auth | JWT (python-jose) + bcrypt", in both the
architecture diagram prose and the tech-stack table. The real implementation is PyJWT (Task 5) and
PBKDF2-SHA256 at 200k iterations (deliberately kept, never bcrypt — see `docs/DECISIONS.md` #2).
It also didn't mention Docker, Redis, or the `requirements.txt`/`requirements-web.txt` split
anywhere, and had a typo (`diffx.db` instead of `diffdx.db`).

`loop1/README.md` (1015 lines, a deep technical doc about the AI actor/critic/router internals —
prompts, schemas, the three loops) had no bcrypt/python-jose claim to begin with, just a stale
"Python 3.11+" prerequisite line and no mention of Docker/`requirements-web.txt`/Redis. Its scope
(prompt system, schemas, loop1/loop2/loop3) is unaffected by this cutover and explicitly out of
scope per week1.md's own framing ("The AI layer is not the problem") — only its practical "Quick
Start" and "Environment Variables" sections needed updating.

`loop1/docs/ARCHITECTURE.md` and `loop1/docs/DECISIONS.md` didn't exist at all.

## What changed

- **Root `README.md`**: fixed the auth claim (both places), added a Docker Compose option to
  "Running locally" alongside the existing manual steps, documented the requirements split,
  expanded the environment-variables table with `REDIS_URL` and the production-required
  `SECRET_KEY`/`CORS_ALLOWED_ORIGINS` behavior, added a "Self-hosted" deployment path alongside
  the existing Render one, fixed the `diffdx.db` typo, and linked the two new docs files.
- **`loop1/README.md`**: Python 3.11+ → 3.12 (matches `.python-version`, the Dockerfile base
  image, `render.yaml`), added a Docker alternative note to Quick Start, added `REDIS_URL` to the
  Environment Variables block with a pointer to `.env.example` for the full list.
- **`loop1/docs/ARCHITECTURE.md`** (new): a Mermaid component diagram (browser → FastAPI → auth/
  routers/repositories/session-store/legacy-store → Postgres/Redis, AI layer collapsed as a single
  node since it's unchanged and documented elsewhere), a request-lifecycle walkthrough of one real
  route (`book_direct`) traced through auth → blob write → dual-write, and short "why" sections
  for the blob-store replacement and the Redis session migration — both citing the actual
  `TASK*.md` evidence (the concurrency proof's 20-requests/1-survivor result; the in-place-mutation
  and async-critic-write hazards found while building Redis sessions) rather than restating claims
  without a source.
- **`loop1/docs/DECISIONS.md`** (new): 4 ADRs matching week1.md's exact list — sync SQLAlchemy over
  async, PBKDF2 retained over bcrypt, Redis sidecar over ElastiCache, public-subnet Fargate over a
  NAT gateway. The first two draw directly on reasoning already stated in week1.md's own spec text
  and Task 5's commit; the Redis and Fargate ones are written generically since no ElastiCache/AWS
  deployment exists to compare against. The 4th ADR is explicit that it describes an *intended*
  future AWS path, not something currently deployed (actual deployments are Render and this
  cutover's new Docker Compose setup) — not fabricating a Fargate claim that isn't true.

## A real, unrelated incident during this task

Verifying the docs meant running the full test suite, which came back with an unfamiliar shape —
9 failures instead of the established ~17-18, with `test_critic.py` and `test_patient_simulator.py`
skipping entirely instead of running. Root cause: `loop1/.env`'s `GROQ_API_KEY` and
`OPENROUTER_API_KEY` were both empty (0 characters). This traced back to Task 23's own Docker setup
instructions — `cp loop1/.env.example loop1/.env`, given to the user to prepare for `docker compose
up --build`, **overwrites an existing file unconditionally**. The user already had a working `.env`
with real keys from earlier in this session; that command silently wiped it back to the blank
template. Not something recoverable from git (`.env` is gitignored, never committed) — flagged
immediately, the user re-added their keys, and the baseline returned to its normal shape (17
failed, 338 passed, 1 skipped) before this task's docs were considered verified.

## Verification

Documentation-only change — no application code touched, so no behavior to test beyond confirming
`pytest` is unaffected (it is, once the unrelated `.env` incident above was resolved). Every
factual claim in the new/edited docs was cross-checked against the actual code before being
written, not asserted from memory: `REDIS_URL` against `config.py`, the Docker commands against
the real `docker-compose.yml`/`Dockerfile`, the auth claims against `auth_tokens.py`/
`legacy_store.py`, the `book_direct` request-lifecycle walkthrough against the actual route
(caught and fixed one inaccuracy this way — it doesn't call `_ensure_relational_appointment` like
every *other* appointment-mutating route does, since booking always creates fresh and doesn't need
the self-heal step). All referenced `TASK*.md` files and source paths confirmed to exist.

## Out of scope

A full rewrite of `loop1/README.md`'s AI-layer content (unaffected by this cutover, explicitly out
of scope per week1.md itself). Regenerating the existing root `architecture.svg` (diagrams the
Loop 1/2/3 actor-critic cycle, unrelated to and unaffected by this session's infra changes — still
accurate). Actually provisioning AWS ECS/Fargate (the 4th ADR documents a decision for a
deployment that doesn't exist yet).

---

This closes out week1.md's original 8-task sequence in full.
