# DiffDx v2 — Architecture Decision Records

Short ADRs for the choices in this cutover that had real tradeoffs, not the ones with an obvious
answer. Each: context, decision, consequences.

## 1. Sync SQLAlchemy, not async

**Context.** SQLAlchemy 2.0 supports both a sync `Session` and an async `AsyncSession` (with
`asyncpg`/`psycopg`'s async mode). FastAPI itself is async-native, and every route handler in this
codebase is declared `async def`, which might suggest the DB layer should match.

**Decision.** Use plain sync `Session` throughout the repository layer (`src/diffdx/
repositories/*`, `diffdx/db/engine.py`). The rest of the codebase — the blob-store helpers in
`legacy_store.py`, the AI-layer LLM calls in `loop1`/`loop2`, the request-handling logic in every
router — is already sync. Introducing async purely at the database layer would mean every call
site touching the DB needs `await`, every dependency needs to become async-aware, and the app ends
up with two concurrency models mixed together for no functional gain at this scale (a handful of
requests per second, not thousands). One consistent style, chosen deliberately, beats mixing two
for the sake of matching FastAPI's own default.

**Consequences.** A sync DB call blocks the worker thread it runs on for its duration — FastAPI
runs sync route dependencies in a thread pool specifically to accommodate this, so it doesn't
block the event loop, but it does mean DB-heavy endpoints have a lower theoretical concurrency
ceiling than a fully async stack would. Acceptable at current scale (see `TASK3_CONCURRENCY_PROOF.
md`'s 20-concurrent-request benchmark, which the sync path handled correctly). If throughput ever
becomes the bottleneck, the migration path is `AsyncSession` + `asyncpg`, which SQLAlchemy 2.0
supports without a schema change — but that's a real, disruptive migration, not a config flag, and
isn't warranted by anything observed so far.

## 2. PBKDF2-SHA256 retained, not migrated to bcrypt

**Context.** The original README claimed passwords were hashed with bcrypt; the actual
implementation (`legacy_store.py::_hash_password`/`_verify_password`, unchanged since before this
cutover) uses PBKDF2-SHA256 at 200,000 iterations. The mismatch was a documentation bug, not a
password-hashing bug — but it raised the question of whether to actually migrate to bcrypt (or
argon2) to match what was documented, rather than fix the documentation.

**Decision.** Keep PBKDF2-SHA256, fix the documentation instead. PBKDF2 at 200k iterations is a
legitimate, still-recommended choice (OWASP's password-storage guidance lists it alongside
bcrypt/scrypt/argon2, with a minimum iteration count well below what this codebase already uses).
Migrating to a different algorithm would mean either forcing a password reset for every existing
user, or building a dual-verify path (try the new algorithm, fall back to the old one on a stored
hash, re-hash on successful login) — real engineering effort and a real transition-window
security surface, spent solely to match a stale doc comment instead of correcting the three-word
mistake in that comment.

**Consequences.** Every password verification still costs one PBKDF2-SHA256 computation at 200k
iterations (a few milliseconds), same as before — no behavior change for any real user. The
README and this doc now correctly describe the code instead of the other way around. If there's
ever a real reason to move to argon2 (e.g. new compliance requirement, or a specific attack
surface concern with PBKDF2's lack of memory-hardness), that's a deliberate future decision with
its own migration plan — not something to retrofit as a side effect of a doc fix.

## 3. Redis sidecar, not a managed cache (e.g. AWS ElastiCache)

**Context.** `session_store.py` (`TASK22_REDIS_SESSIONS.md`) needed somewhere to hold live
diagnostic-session state shared across API replicas. The options were a self-hosted Redis
container (via Docker Compose locally, or a Redis container/service alongside the API in whatever
hosts it) versus a managed service like AWS ElastiCache.

**Decision.** A plain Redis container (`redis:7-alpine` in `docker-compose.yml`), reached via a
`REDIS_URL` env var — the same pattern already used for `DATABASE_URL`. No managed-cache-specific
code anywhere; `session_store.py` just calls `redis.from_url(...)`, which works identically
against a local container, a managed Redis instance, or anything else that speaks the Redis
protocol. At this project's actual scale (a single API service, a handful of concurrent sessions),
a managed service's value — automated failover, multi-AZ replication, zero-ops patching — solves
problems this deployment doesn't have yet, at a cost (a standing AWS bill, VPC/security-group
setup, a new piece of account infrastructure to provision) that isn't justified by anything
currently running.

**Consequences.** No built-in HA — if the Redis container dies, in-flight session state older than
data since the last successful `save_session()` call is gone (worst case: mid-turn, on the turn
that hasn't been saved back yet — see `TASK22_REDIS_SESSIONS.md`'s write-back design). Session
data itself is inherently ephemeral (2-hour TTL, refreshed on activity) and non-critical —
completed sessions already persist separately to Postgres — so this is an acceptable tradeoff, not
a gap in a durability guarantee that matters. Because the connection is just a URL, moving to
ElastiCache (or any managed Redis) later is a config change, not a code change, if/when the
operational profile actually calls for it.

## 4. Public-subnet Fargate, not a NAT gateway (planned, not yet deployed)

**Context.** This ADR describes the intended shape of a future containerized AWS deployment, not
something currently running — actual deployments today are Render (`render.yaml`) and, as of this
cutover, self-hosted Docker Compose (`docker-compose.yml`). If/when this moves to AWS ECS Fargate
(the two-replica deployment referenced in earlier planning), Fargate tasks need outbound internet
access (to reach Groq/OpenRouter/etc.) and the question is how: a NAT gateway in a private subnet,
or public IPs directly on the tasks in a public subnet with security groups restricting inbound
traffic to the load balancer.

**Decision.** Public subnet with public IPs, security-group-restricted ingress — not a NAT
gateway. A NAT gateway has a flat hourly cost plus per-GB data-processing charges that apply to
*all* outbound traffic through it, running whether or not the service is under load; a small
number of Fargate tasks with public IPs and a tight security group (inbound only from the load
balancer/ALB, all other inbound denied) gets the same outbound-internet-access outcome for
meaningfully less cost at this traffic scale, with no meaningful security difference for a service
that has no need to hide its egress path (it's calling public third-party LLM APIs, not reaching
other private-network resources).

**Consequences.** Fargate tasks would have public IPs, which some security postures avoid on
principle — the mitigating control here is the security group locking down all inbound traffic
except from the load balancer, which achieves the same effective exposure as a NAT-gatewayed
private subnet without the standing cost. If a future compliance requirement specifically mandates
no-public-IP task networking regardless of security-group configuration, this decision would need
revisiting alongside a NAT gateway's added cost — that's a real tradeoff to make when (if) that
requirement actually exists, not preemptively.
