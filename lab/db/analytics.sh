#!/usr/bin/env bash
# Run by the image's entrypoint at build time, after sample_data.sql.gz: the analytics database, with one schema per
# dataset: dba (dba.stackexchange.com) and flight_delays (US flights of 2015).
set -e
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres -c 'CREATE DATABASE analytics'
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname analytics --single-transaction -f /seed/stackexchange.sql
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname analytics --single-transaction -f /seed/flight_delays.sql
# Frozen and analyzed, so reading a table in an instance doesn't write to it. Without parallel workers, which need more
# shared memory than a build container has.
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname analytics -c 'VACUUM (FREEZE, ANALYZE, PARALLEL 0)'
