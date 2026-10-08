-- Story 2: a spice supplier changes owners. By supplier_id the new entity looks like an ordinary supplier; by tax id
-- (normalised: digits only) the same company's incoming microbiology climbs steadily after the change.
-- Incoming tests: P1/P3 rows carry the count in result; P2's are a JSON panel ("valor").
WITH sup AS (
  SELECT supplier_id, supplier_name, regexp_replace(tax_id, '\D', '', 'g') AS tax FROM gatr.suppliers),
micro AS (
  SELECT q.receipt_id,
         CASE WHEN q.result_json IS NOT NULL
              THEN (replace(q.result_json, 'NaN', 'null')::jsonb -> 'MICROBIOLOGIA' ->> 'valor')::numeric
              ELSE replace(q.result, ',', '.')::numeric END AS ufc
  FROM gatr.quality_tests q
  WHERE q.sample_type = 'MP'
    AND ((q.test_name = 'MICROBIOLOGIA' AND q.result ~ '^[0-9]+([.,][0-9]+)?$')
         OR q.result_json LIKE '{"MICROBIOLOGIA"%')),
lots AS (
  SELECT g.receipt_date, s.tax, g.supplier_id, m.ufc
  FROM gatr.goods_receipts g JOIN sup s USING (supplier_id) JOIN micro m USING (receipt_id) WHERE m.ufc > 0)
SELECT tax, date_trunc('quarter', receipt_date)::date AS quarter, string_agg(DISTINCT supplier_id::text, ',')
         AS supplier_ids, count(*) AS lots, round(avg(log(ufc)), 2) AS mean_log_ufc,
       round(100.0 * avg((ufc > 1000)::int), 1) AS fail_pct
FROM lots
WHERE tax = (SELECT tax FROM sup WHERE supplier_name ILIKE '%norte grande%' LIMIT 1)
GROUP BY 1, 2 ORDER BY 2;
