#!/bin/sh
# Start (or reuse) the local Postgres used by tests and local development.
# Idempotent: an existing trace-pg container is started if stopped, never recreated.
# URL: postgresql://postgres:trace@127.0.0.1:54329/postgres
set -eu
NAME=trace-pg
PORT=54329
if docker container inspect "$NAME" >/dev/null 2>&1; then
  if [ "$(docker container inspect -f '{{.State.Running}}' "$NAME")" != "true" ]; then
    docker start "$NAME" >/dev/null
  fi
else
  docker run -d --name "$NAME" -e POSTGRES_PASSWORD=trace \
    -p "127.0.0.1:$PORT:5432" postgres:17 >/dev/null
fi
i=0
until docker exec "$NAME" pg_isready -U postgres -h 127.0.0.1 >/dev/null 2>&1; do
  i=$((i + 1))
  if [ "$i" -ge 60 ]; then
    echo "trace-pg did not become ready in 60 s" >&2
    exit 1
  fi
  sleep 1
done
echo "trace-pg ready: postgresql://postgres:trace@127.0.0.1:$PORT/postgres"
