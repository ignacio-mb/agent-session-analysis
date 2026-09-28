# The Postgres every instance gets: Metabase's Sample Database image, plus an analytics database with two real datasets,
# each in a schema of its own: dba (dba.stackexchange.com) and flight_delays (US flights of 2015). server.ts builds it
# from this directory the first time an instance needs it, and tags it by the content of this directory, so changing
# any file here builds a new image.

# 1. Initialize the data directory once, here, so containers start with every database ready. The load streams each
# download from its archive into Postgres: Stack Exchange's own data dump of the site (2024-04-06, CC BY-SA, see
# stackexchange.sql), and the US DOT's on-time reports of 2015 as Maven Analytics publishes them (see flight_delays.sql).
# It needs no crash safety, and without full-page writes VACUUM FREEZE leaves little WAL behind.
FROM metabase/qa-databases:postgres-sample-15 AS bake
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl 7zip python3 \
 && rm -rf /var/lib/apt/lists/*
RUN mkdir /seed && curl -fsSL --retry 5 --retry-all-errors --speed-limit 10000 --speed-time 60 \
      -o /seed/dump.7z https://archive.org/download/stackexchange/dba.stackexchange.com.7z \
 && echo "1534244d87f57f22e2d2a38a03de907a7c86e217  /seed/dump.7z" | sha1sum -c -
RUN curl -fsSL --retry 5 --retry-all-errors --speed-limit 10000 --speed-time 60 -o /seed/flights.zip \
      "https://maven-datasets.s3.amazonaws.com/Airline+Flight+Delays/Airlines+Airports+Cancellation+Codes+%26+Flights.zip" \
 && echo "f5cb723c581182a5723982f2f2fc4e1d62078c97  /seed/flights.zip" | sha1sum -c -
COPY xml2csv.py stackexchange.sql flight_delays.sql /seed/
COPY analytics.sh /docker-entrypoint-initdb.d/
RUN docker-ensure-initdb.sh -c wal_level=minimal -c max_wal_senders=0 -c fsync=off -c full_page_writes=off \
      -c maintenance_work_mem=1GB -c max_wal_size=256MB

# 2. Only the data directory on top of the base image. Owned by postgres, or the entrypoint chowns every file at start.
FROM metabase/qa-databases:postgres-sample-15
COPY --from=bake --chown=postgres:postgres /data /data
