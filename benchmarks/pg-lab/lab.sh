#!/usr/bin/env bash
# Start/stop PostgreSQL for the optimization loop.
# Tries docker compose first; falls back to the system postgres binaries with
# a scratch data directory. Both paths expose the same connection string:
#   postgresql://bench:bench@127.0.0.1:5433/bench
set -euo pipefail

PORT=5433
PGDATA="${PGDATA_DIR:-/tmp/pcp-bench-pgdata}"
PGBIN="$(ls -d /usr/lib/postgresql/*/bin 2>/dev/null | sort -V | tail -1 || true)"
HERE="$(cd "$(dirname "$0")" && pwd)"

start() {
  if docker info >/dev/null 2>&1; then
    docker compose -f "$HERE/docker-compose.yml" up -d --wait
    echo "postgres up via docker compose on :$PORT"
    return
  fi
  echo "docker daemon unavailable; falling back to system postgres ($PGBIN)"
  if [ ! -d "$PGDATA" ]; then
    mkdir -p "$PGDATA"
    chown -R postgres:postgres "$PGDATA" 2>/dev/null || true
    su postgres -c "$PGBIN/initdb -D $PGDATA -A trust" >/dev/null
  fi
  su postgres -c "$PGBIN/pg_ctl -D $PGDATA -o '-p $PORT -c listen_addresses=127.0.0.1 -c fsync=off -c full_page_writes=off' -l $PGDATA/log start" >/dev/null
  for _ in $(seq 1 30); do
    su postgres -c "$PGBIN/pg_isready -p $PORT" >/dev/null 2>&1 && break
    sleep 0.5
  done
  su postgres -c "psql -p $PORT -tc \"SELECT 1 FROM pg_roles WHERE rolname='bench'\"" | grep -q 1 \
    || su postgres -c "psql -p $PORT -c \"CREATE ROLE bench LOGIN PASSWORD 'bench' SUPERUSER\"" >/dev/null
  su postgres -c "psql -p $PORT -tc \"SELECT 1 FROM pg_database WHERE datname='bench'\"" | grep -q 1 \
    || su postgres -c "psql -p $PORT -c 'CREATE DATABASE bench OWNER bench'" >/dev/null
  echo "postgres up via system binaries on :$PORT (fsync off; scratch data in $PGDATA)"
}

stop() {
  if docker info >/dev/null 2>&1 && docker ps -q -f name=pcp-bench-pg | grep -q .; then
    docker compose -f "$HERE/docker-compose.yml" down -v
  elif [ -d "$PGDATA" ]; then
    su postgres -c "$PGBIN/pg_ctl -D $PGDATA stop -m fast" || true
  fi
}

case "${1:-start}" in
  start) start ;;
  stop) stop ;;
  *) echo "usage: lab.sh [start|stop]" >&2; exit 1 ;;
esac
