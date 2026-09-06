# DiffDx v2 — web API image. week1.md Task 7.
#
# NOTE (honesty, not boilerplate): this Dockerfile was written and
# statically reviewed but never actually built or run — the environment
# that authored it has no `docker` binary available. Build it and run
# `docker compose up --build` before trusting it in any real deployment.
#
# requirements.txt vs requirements-web.txt: requirements.txt is the full
# manifest (includes torch, the CUDA stack, faiss-cpu, sentence-transformers
# — everything the offline AI-layer tooling in scripts/ needs for local
# embedding/training work). requirements-web.txt is a hand-curated subset
# for the web server only, which never runs local inference or embedding —
# multiple GB smaller. This image installs from requirements-web.txt only.
# (Both files were found to be missing sqlalchemy/alembic/pyjwt/slowapi/
# pydantic-settings entirely — fixed as a precondition for this Dockerfile
# to have any chance of working; see the commit that added this file.)

FROM python:3.12-slim AS builder

WORKDIR /app

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements-web.txt .
RUN pip install --no-cache-dir -r requirements-web.txt

COPY pyproject.toml ./
COPY src ./src
COPY web ./web
RUN pip install --no-cache-dir -e . --no-deps


FROM python:3.12-slim AS runtime

RUN groupadd --gid 1000 appuser \
    && useradd --uid 1000 --gid appuser --shell /bin/bash --create-home appuser

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH="/app/src" \
    PYTHONUNBUFFERED=1

COPY --from=builder /opt/venv /opt/venv
# Same relative layout as the builder stage — the editable install's
# finder points back to /app/src, so these paths must match exactly.
COPY --from=builder /app/src ./src
COPY --from=builder /app/web ./web
COPY --from=builder /app/pyproject.toml ./pyproject.toml

# Runtime data the AI layer reads by path at call time, not just import
# time — loop1.config reads config.yaml, loop1.doctor and
# loop2.critic.critic read prompts/*.txt, sessions.py's _CASES_DIR reads
# test_cases/*.json, loop3.routing.map_loader reads
# data/disease_specialty_map.json (GET /api/session/{id}/routing —
# missed in the original pass, found live: "Disease specialty map not
# found: /app/data/disease_specialty_map.json"). exemplars/ is read by
# the live web session path via select_random() only — the
# faiss/fastembed-backed semantic retrieval functions in loop1.retrieval
# are never called from a real request (only from offline tooling),
# confirmed live; requirements-web.txt correctly excludes those packages.
# The rest of data/ (DDXPlus_Raw/, phase7_sessions/, phase7_critiques/,
# ddxplus_eval_set.json) is offline eval tooling only — not copied.
COPY alembic ./alembic
COPY alembic.ini ./alembic.ini
COPY config.yaml ./config.yaml
COPY prompts ./prompts
COPY test_cases ./test_cases
COPY exemplars ./exemplars
COPY data/disease_specialty_map.json ./data/disease_specialty_map.json
COPY docker-entrypoint.sh ./docker-entrypoint.sh
# Only the one script the entrypoint needs, not the whole scripts/
# directory — the rest is offline AI-layer tooling with its own,
# heavier dependency needs (not installed here, see requirements-web.txt).
COPY scripts/seed_doctors.py ./scripts/seed_doctors.py
RUN chmod +x ./docker-entrypoint.sh

RUN mkdir -p web/data logs/final_records logs/sessions logs/critic_reports \
    && chown -R appuser:appuser /app

USER appuser

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

ENTRYPOINT ["./docker-entrypoint.sh"]
