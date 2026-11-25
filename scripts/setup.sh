#!/usr/bin/env bash
# One-time local setup (Git Bash on Windows, or macOS/Linux).
#   bash scripts/setup.sh
# Starts Postgres + Redis in Docker, creates a Python virtual env, installs
# dependencies, writes a local .env and runs the tests to prove everything works.
#
# Docker can be Docker Desktop, or Docker Engine inside WSL (Ubuntu): if there's no `docker`
# command here, the script runs it through `wsl.exe`.
set -euo pipefail
cd "$(dirname "$0")/.."

# ---- which docker? ----
if command -v docker >/dev/null 2>&1; then
  DOCKER=(docker)
elif command -v wsl.exe >/dev/null 2>&1 && wsl.exe -e docker version >/dev/null 2>&1; then
  DOCKER=(wsl.exe -e docker)   # Docker Engine inside WSL
else
  echo "Docker not found. Install Docker Desktop, or Docker Engine inside WSL, and start it." >&2
  exit 1
fi
compose() { "${DOCKER[@]}" compose "$@"; }

# ---- which host port for Postgres? ----
# Kept in .env as DB_PORT. On first run, if something else (e.g. a locally installed PostgreSQL)
# already uses 5432, use 5433 instead.
port_in_use() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }
if [ -z "${DB_PORT:-}" ] && [ -f .env ]; then DB_PORT="$(sed -n 's/^DB_PORT=//p' .env)"; fi
if [ -z "${DB_PORT:-}" ]; then
  DB_PORT=5432
  if [ ! -f .env ] && port_in_use 5432; then
    DB_PORT=5433
    echo "    port 5432 is taken (a local PostgreSQL?), using $DB_PORT for the Docker database"
  fi
fi
export DB_PORT
export WSLENV="DB_PORT${WSLENV:+:$WSLENV}"   # Docker inside WSL only sees variables listed in WSLENV

echo "==> 1/5 Starting Postgres + Redis (Docker, Postgres on localhost:$DB_PORT)"
compose up -d db redis
echo -n "    waiting for Postgres"
for _ in $(seq 1 30); do
  if compose exec -T db pg_isready -U postgres >/dev/null 2>&1; then echo " ready"; break; fi
  echo -n "."; sleep 1
done
# Create the app + test databases if missing (e.g. a Docker volume from the SeatLock days)
for dbname in ticketdhaka ticketdhaka_test; do
  compose exec -T db psql -U postgres -tc "SELECT 1 FROM pg_database WHERE datname='$dbname'" | grep -q 1 \
    || compose exec -T db psql -U postgres -c "CREATE DATABASE $dbname" >/dev/null
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
DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:$DB_PORT/ticketdhaka
REDIS_URL=redis://localhost:6379/0
DB_PORT=$DB_PORT
JWT_SECRET=local-dev-secret-$(date +%s)-change-me-please
ADMIN_EMAIL=admin@local.dev
ADMIN_PASSWORD=admin12345
SEED_DEMO_DATA=true
# Stripe test mode (optional): paste your sk_test_... key to use Stripe Checkout instead of the demo gateway
STRIPE_SECRET_KEY=
STRIPE_WEBHOOK_SECRET=
ENV
  echo "    wrote .env (local admin: admin@local.dev / admin12345)"
else
  echo "    .env already exists, left unchanged"
fi

echo "==> 5/5 Running tests"
DATABASE_URL="postgresql+psycopg://postgres:postgres@localhost:$DB_PORT/ticketdhaka_test" pytest -q

cat <<'DONE'

All set ✅  Day to day:
  source .venv/Scripts/activate        # (macOS/Linux: source .venv/bin/activate)
  docker compose up -d db redis        # if Docker was restarted (Docker in WSL: run it in Ubuntu)
  uvicorn app.main:app --reload        # http://localhost:8000  ·  /admin  ·  /docs (admin login)
  bash scripts/test.sh                 # run the tests
DONE
