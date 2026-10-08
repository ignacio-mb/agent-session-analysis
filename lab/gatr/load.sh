#!/usr/bin/env bash
# Loads GATR (schema gatr of the analytics database) into a lab instance that is already running, without a Reset, so
# its Metabase keeps everything built on it. New instances, and instances you Reset, get GATR from the image anyway.
#
#   lab/gatr/load.sh <instance name | Postgres container id or name> [--replace]
#   make gatr-load INSTANCE=airline-flight-delays11
#
# Copies lab/db/gatr.sql and lab/db/gatr/*.csv.gz into the instance's Postgres container and runs the load there (as
# the image build does), creating the analytics database if the instance predates it. --replace drops an existing gatr
# schema first. With MB_URL and MB_API_KEY set (the lab page's "Copy env"), it also has Metabase sync the database.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
db_dir="$here/../db"
target="${1:?usage: lab/gatr/load.sh <instance name | Postgres container> [--replace]}"
replace="${2:-}"

# an instance name means its Postgres container, mbo-<name>.postgres
if docker inspect "mbo-$target.postgres" >/dev/null 2>&1; then
  container="mbo-$target.postgres"
else
  container="$target"
fi
docker inspect "$container" >/dev/null || { echo "No container $container" >&2; exit 1; }
[ "$(docker inspect -f '{{.State.Running}}' "$container")" = true ] || { echo "$container is not running" >&2; exit 1; }
user="$(docker exec "$container" sh -c 'echo "${POSTGRES_USER:-postgres}"')"
psql() { docker exec -i "$container" psql -v ON_ERROR_STOP=1 -X -q --username "$user" "$@"; }

if [ -z "$(psql --dbname postgres -tAc "SELECT 1 FROM pg_database WHERE datname = 'analytics'")" ]; then
  echo "creating the analytics database"
  psql --dbname postgres -c 'CREATE DATABASE analytics'
fi
if [ -n "$(psql --dbname analytics -tAc "SELECT 1 FROM pg_namespace WHERE nspname = 'gatr'")" ]; then
  if [ "$replace" != "--replace" ]; then
    echo "$container already has the gatr schema; run again with --replace to load it afresh" >&2
    exit 1
  fi
  psql --dbname analytics -c 'DROP SCHEMA gatr CASCADE'
fi

echo "copying GATR's tables into $container"
docker exec "$container" mkdir -p /seed/gatr
docker cp "$db_dir/gatr.sql" "$container:/seed/gatr.sql"
for f in "$db_dir"/gatr/*.csv.gz; do docker cp "$f" "$container:/seed/gatr/"; done
echo "loading (a few minutes)"
psql --dbname analytics --single-transaction -f /seed/gatr.sql
psql --dbname analytics -c 'VACUUM (ANALYZE) ' >/dev/null
docker exec "$container" rm -rf /seed/gatr /seed/gatr.sql
# what the lab's read-only role (Metabase's main connection) may read, as setup grants it
psql --dbname analytics <<'SQL'
DO $$ BEGIN
  IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'metabase_readonly') THEN
    GRANT USAGE ON SCHEMA gatr TO metabase_readonly;
    GRANT SELECT ON ALL TABLES IN SCHEMA gatr TO metabase_readonly;
  END IF;
END $$;
SQL
psql --dbname analytics -tAc "SELECT count(*) || ' tables, ' || sum(n_live_tup) || ' rows' FROM pg_stat_user_tables
                              WHERE schemaname = 'gatr'"

if [ -n "${MB_URL:-}" ] && [ -n "${MB_API_KEY:-}" ]; then
  id="$(curl -fsS -H "x-api-key: $MB_API_KEY" "$MB_URL/api/database" |
        python3 -c 'import json,sys; d=json.load(sys.stdin); d=d.get("data",d); print(next((x["id"] for x in d if x["details"].get("dbname")=="analytics" or x["name"]=="Analytics"),""))')"
  if [ -n "$id" ]; then
    curl -fsS -X POST -H "x-api-key: $MB_API_KEY" "$MB_URL/api/database/$id/sync_schema" >/dev/null
    echo "Metabase is syncing database $id (Analytics): the gatr schema shows up in a minute or two"
  else
    echo "No database named Analytics in $MB_URL: add $container's analytics database to Metabase" >&2
  fi
else
  echo "Now sync the Analytics database in Metabase: Admin > Databases > Analytics > Sync database schema. (With MB_URL"
  echo "and MB_API_KEY set, from the lab page's Copy env, this script does it.)"
fi
