#!/usr/bin/env bash
# One-time local setup (Git Bash on Windows, or macOS/Linux).
#   bash scripts/setup.sh
# Starts Postgres + Redis in Docker, creates a Python virtual env, installs
# dependencies, writes a local .env and runs the tests to prove everything works.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "==> 1/5 Starting Postgres + Redis (Docker)"
docker compose up -d db redis
echo -n "    waiting for Postgres"
for _ in $(seq 1 30); do
  if docker compose exec -T db pg_isready -U postgres >/dev/null 2>&1; then echo " ready"; break; fi
  echo -n "."; sleep 1
done
# Create the app + test databases if missing (e.g. a Docker volume from the SeatLock days)
for dbname in ticketdhaka ticketdhaka_test; do
  docker compose exec -T db psql -U postgres -tc "SELECT 1 FROM pg_database WHERE datname='$dbname'" | grep -q 1 \
    || docker compose exec -T db psql -U postgres -c "CREATE DATABASE $dbname" >/dev/null
done

echo "==> 2/5 Python virtual environment (.venv)"
PY=python
command -v python >/dev/null 2>&1 || PY=python3
[ -d .venv ] || $PY -m venv .venv
if [ -f .venv/Scripts/activate ]; then source .venv/Scripts/activate; else source .venv/bin/activate; fi
python --version

echo "==> 3/5 Installing dependencies"
python -m pip install -q --upgrade pip
pip install -q -r requirements-dev.txt

echo "==> 4/5 Local .env"
if [ ! -f .env ]; then
  cat > .env <<ENV
DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/ticketdhaka
REDIS_URL=redis://localhost:6379/0
JWT_SECRET=local-dev-secret-$(date +%s)-change-me-please
ADMIN_EMAIL=admin@local.dev
ADMIN_PASSWORD=admin12345
SEED_DEMO_DATA=true
ENV
  echo "    wrote .env (local admin: admin@local.dev / admin12345)"
else
  echo "    .env already exists, left unchanged"
fi

echo "==> 5/5 Running tests"
DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/ticketdhaka_test pytest -q

cat <<'DONE'

All set ✅  Day to day:
  source .venv/Scripts/activate        # (macOS/Linux: source .venv/bin/activate)
  docker compose up -d db redis        # if Docker was restarted
  uvicorn app.main:app --reload        # http://localhost:8000  ·  /admin  ·  /docs
  bash scripts/test.sh                 # run the tests
DONE
