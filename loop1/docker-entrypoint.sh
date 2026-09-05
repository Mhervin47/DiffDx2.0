#!/bin/sh
# Runs migrations before starting the server — week1.md Task 7: "API
# entrypoint runs alembic upgrade head before starting uvicorn." Doctor
# seeding happens on every startup already (web/api.py's _on_startup),
# not repeated here.
#
# Respects an explicit command (`docker run <image> whoami`, a shell for
# debugging, etc.) instead of always forcing migrate+serve — standard
# "smart entrypoint" pattern. Without this, ENTRYPOINT + no CMD means any
# `docker run <image> <cmd>` just appends <cmd> as an argument this script
# would otherwise ignore, silently running migrate+serve instead of what
# was actually asked for (found live: `docker run --rm <image> whoami`
# ran the normal startup sequence and failed on missing GROQ_API_KEY
# instead of printing "appuser").
set -e

if [ "$#" -eq 0 ]; then
    echo "Running alembic upgrade head..."
    alembic upgrade head

    echo "Starting uvicorn..."
    exec uvicorn web.api:app --host 0.0.0.0 --port "${PORT:-8000}"
else
    exec "$@"
fi
