#!/bin/sh
# Runs migrations before starting the server — week1.md Task 7: "API
# entrypoint runs alembic upgrade head before starting uvicorn." Doctor
# seeding happens on every startup already (web/api.py's _on_startup),
# not repeated here.
set -e

echo "Running alembic upgrade head..."
alembic upgrade head

echo "Starting uvicorn..."
exec uvicorn web.api:app --host 0.0.0.0 --port "${PORT:-8000}"
