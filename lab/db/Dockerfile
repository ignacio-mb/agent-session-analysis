# The Postgres every instance gets: Metabase's Sample Database image with dba.stackexchange.com baked in as a second
# database. server.ts builds it from this directory the first time an instance needs it, and tags it by the content of
# this directory, so changing any file here builds a new image.

# 1. Initialize the data directory once, here, so containers start with both databases ready. The load streams Stack
# Exchange's own data dump of the site (2024-04-06, CC BY-SA) from the archive into Postgres (see stackexchange.sql).
# It needs no crash safety, and without full-page writes VACUUM FREEZE leaves little WAL behind.
FROM metabase/qa-databases:postgres-sample-15 AS bake
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl 7zip python3 \
 && rm -rf /var/lib/apt/lists/*
RUN mkdir /seed && curl -fsSL --retry 5 -o /seed/dump.7z https://archive.org/download/stackexchange/dba.stackexchange.com.7z \
 && echo "1534244d87f57f22e2d2a38a03de907a7c86e217  /seed/dump.7z" | sha1sum -c -
COPY xml2csv.py stackexchange.sql /seed/
COPY stackexchange.sh /docker-entrypoint-initdb.d/
RUN docker-ensure-initdb.sh -c wal_level=minimal -c max_wal_senders=0 -c fsync=off -c full_page_writes=off \
      -c maintenance_work_mem=1GB -c max_wal_size=256MB

# 2. Only the data directory on top of the base image. Owned by postgres, or the entrypoint chowns every file at start.
FROM metabase/qa-databases:postgres-sample-15
COPY --from=bake --chown=postgres:postgres /data /data
