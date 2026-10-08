-- Story 9: Simpson's paradox. Overall, P2's yield (good / planned, weighted by kilograms) beats P1's; product by
-- product, P1 beats P2 on every SKU both plants make (with at least 10 clean batches in each). P2 is mostly sauces,
-- the highest-yield family. Impossible rows (yield above 150%, negative or swapped) are dropped.
WITH b AS (
  SELECT b.plant_id, b.item_code, p.family, b.good_qty, b.planned_qty,
         CASE WHEN b.uom = 'CJ' THEN p.units_per_case ELSE 1 END * p.pack_size / 1000.0 AS kg_per_qty
  FROM gatr.production_batches b JOIN gatr.products p ON p.sku_code = b.item_code
  WHERE b.deleted_at IS NULL AND b.plant_id IN ('P1', 'P2') AND b.planned_qty > 0
    AND b.good_qty BETWEEN 0 AND 1.5 * b.planned_qty),
overall AS (
  SELECT plant_id, round(100 * sum(good_qty * kg_per_qty) / sum(planned_qty * kg_per_qty), 2) AS yield_kg
  FROM b GROUP BY 1),
per_sku AS (
  SELECT item_code, plant_id, avg(good_qty / planned_qty) AS y, count(*) AS n FROM b GROUP BY 1, 2),
both_ AS (
  SELECT p1.item_code, p1.y AS y1, p2.y AS y2 FROM per_sku p1 JOIN per_sku p2 USING (item_code)
  WHERE p1.plant_id = 'P1' AND p2.plant_id = 'P2' AND p1.n >= 10 AND p2.n >= 10)
SELECT (SELECT yield_kg FROM overall WHERE plant_id = 'P1') AS p1_overall_yield,
       (SELECT yield_kg FROM overall WHERE plant_id = 'P2') AS p2_overall_yield,
       count(*) AS skus_made_in_both, count(*) FILTER (WHERE y1 > y2) AS skus_where_p1_is_better
FROM both_;
