-- Story 3: the cost-optimised mayonnaise recipe. Batches are tied to recipe versions through production_orders
-- (P2 often leaves recipe_id empty: fall back to the version in force on the batch date, first matching version).
-- Release tests (P1/P3 rows; P2's JSON panels) and complaints traced to lots are compared by recipe version.
WITH v AS (SELECT recipe_id, version, effective_from, notes FROM gatr.recipes WHERE product_code = 'F-MAY-CLA'),
b AS (
  SELECT b.batch_id, b.lot_code, b.plant_id,
         coalesce(o.recipe_id, (SELECT v.recipe_id FROM v WHERE v.effective_from <= o.sched_start::date
                                ORDER BY v.effective_from DESC LIMIT 1)) AS recipe_id
  FROM gatr.production_batches b JOIN gatr.production_orders o USING (order_no)
  JOIN gatr.products p ON p.sku_code = b.item_code AND p.description ILIKE 'Mayonesa Cl_sica%'
  WHERE b.deleted_at IS NULL),
t AS (  -- one row per batch: did any yeast/mould or seal test fail?
  SELECT q.batch_id,
         bool_or(CASE WHEN q.result_json IS NOT NULL THEN
               (replace(q.result_json, 'NaN', 'null')::jsonb -> 'LEVADURAS Y HONGOS' ->> 'conforme') = 'false'
               OR (replace(q.result_json, 'NaN', 'null')::jsonb -> 'VACIO / SELLADO' ->> 'conforme') = 'false'
             ELSE q.test_name IN ('LEVADURAS Y HONGOS', 'VACIO / SELLADO') AND NOT q.passed END) AS failed
  FROM gatr.quality_tests q WHERE q.sample_type = 'PT' GROUP BY 1),
c AS (  -- complaints whose reported lot normalises to a lot code
  SELECT upper(regexp_replace(regexp_replace(lot_code_reported, '^\s*lote\s*', '', 'i'), '\s', '', 'g')) AS lot
  FROM gatr.customer_complaints)
SELECT v.version, v.effective_from, left(coalesce(v.notes, ''), 30) AS notes, count(*) AS batches,
       round(100.0 * avg(t.failed::int), 2) AS test_fail_pct,
       count(*) FILTER (WHERE b.lot_code IN (SELECT lot FROM c)) AS lots_with_complaints,
       round(100.0 * count(*) FILTER (WHERE b.lot_code IN (SELECT lot FROM c)) / count(*), 2) AS pct_lots_complained
FROM b JOIN v USING (recipe_id) LEFT JOIN t USING (batch_id)
GROUP BY 1, 2, 3 ORDER BY 1;
