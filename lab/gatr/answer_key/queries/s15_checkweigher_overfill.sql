-- Story 15: P2's chips line got a new checkweigher on 2024-10-07 with a wrong tare: bags were overfilled by ~4.5%
-- until a quality audit fixed it on 2025-03-14. P2 keeps results as JSON panels; net weight against the declared
-- weight, by month, for SUR-01 against P1's chips line.
WITH w AS (
  SELECT b.line_id, date_trunc('month', q.tested_at)::date AS month,
         (replace(q.result_json, 'NaN', 'null')::jsonb -> 'PESO NETO' ->> 'valor')::numeric AS grams, p.pack_size
  FROM gatr.quality_tests q JOIN gatr.production_batches b USING (batch_id)
  JOIN gatr.products p ON p.sku_code = b.item_code
  WHERE b.line_id = 'SUR-01' AND q.result_json IS NOT NULL
  UNION ALL
  SELECT b.line_id, date_trunc('month', q.tested_at)::date, replace(q.result, ',', '.')::numeric, p.pack_size
  FROM gatr.quality_tests q JOIN gatr.production_batches b USING (batch_id)
  JOIN gatr.products p ON p.sku_code = b.item_code
  WHERE b.line_id = 'L1-01' AND q.test_name = 'PESO NETO')
SELECT month, round(100 * avg(grams / pack_size) FILTER (WHERE line_id = 'SUR-01') - 100, 2) AS sur01_overfill_pct,
       round(100 * avg(grams / pack_size) FILTER (WHERE line_id = 'L1-01') - 100, 2) AS l101_overfill_pct
FROM w WHERE month BETWEEN '2024-07-01' AND '2025-06-01' GROUP BY 1 ORDER BY 1;
