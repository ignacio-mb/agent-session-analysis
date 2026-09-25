#!/usr/bin/env bash
# Run by the image's entrypoint at build time, after sample_data.sql.gz: loads dba.stackexchange.com into its own database.
set -e
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres -c 'CREATE DATABASE stackexchange'
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname stackexchange --single-transaction -f /seed/stackexchange.sql
# Frozen and analyzed, so reading a table never writes to it: containers share the image's files instead of copying them.
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname stackexchange -c 'VACUUM (FREEZE, ANALYZE)'
