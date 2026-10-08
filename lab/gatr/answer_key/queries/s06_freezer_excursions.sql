-- Story 6: FRZ-N2's night-time excursions. P1/P2 sensor times are in the IoT platform's default zone
-- (America/New_York, with DST); converted to local time the excursions sit at 02:00-04:30 every night from the
-- defrost-controller job in January 2025 to the fix in August. Frozen lots stored in FRZ-N2 meanwhile (weekly
-- snapshots) draw texture/thaw complaints at several times the rate of lots in FRZ-N1.
WITH r AS (
  SELECT sensor_id,
         (reading_time AT TIME ZONE 'America/New_York') AT TIME ZONE 'America/Argentina/Buenos_Aires' AS local_time,
         value
  FROM gatr.cold_storage_sensor_readings WHERE sensor_id IN ('TS-FRZ-N1', 'TS-FRZ-N2') AND unit = 'C'),
exc AS (
  SELECT sensor_id, date_trunc('month', local_time)::date AS month,
         count(*) FILTER (WHERE value > -12) AS readings_over_alarm,
         count(*) FILTER (WHERE value > -12 AND extract(hour FROM local_time) BETWEEN 2 AND 4) AS at_2_to_5_local
  FROM r WHERE local_time >= '2024-10-01' AND local_time < '2025-11-01' GROUP BY 1, 2),
lots AS (  -- frozen lots that sat in each freezer while it misbehaved
  SELECT DISTINCT location, lot FROM gatr.inventory_snapshots
  WHERE location IN ('FRZ-N1', 'FRZ-N2') AND item_type = 'PT' AND snapshot_date BETWEEN '2025-01-15' AND '2025-08-05'),
c AS (SELECT upper(regexp_replace(regexp_replace(lot_code_reported, '^\s*lote\s*', '', 'i'), '\s', '', 'g')) AS lot,
             category FROM gatr.customer_complaints)
SELECT 'excursions' AS what, sensor_id || ' ' || month AS key, readings_over_alarm AS n, at_2_to_5_local AS m
FROM exc
UNION ALL
SELECT 'lots and complaints', l.location, count(DISTINCT l.lot), count(c.lot)
FROM lots l LEFT JOIN c ON c.lot = l.lot GROUP BY l.location
ORDER BY 1, 2;
