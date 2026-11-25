#!/usr/bin/env bash
# Lint + tests against the local Docker Postgres test database.
#   bash scripts/test.sh            # everything
#   bash scripts/test.sh -k admin   # extra args go to pytest
set -euo pipefail
cd "$(dirname "$0")/.."
if [ -f .venv/Scripts/activate ]; then source .venv/Scripts/activate; elif [ -f .venv/bin/activate ]; then source .venv/bin/activate; fi
ruff check .
DATABASE_URL="${TEST_DATABASE_URL:-postgresql+psycopg://postgres:postgres@localhost:5432/ticketdhaka_test}" pytest -q "$@"
