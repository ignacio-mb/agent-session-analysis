-- Story 5: receipts inflated by one clerk for one supplier. Cycle-count write-offs, normalised by how much of each
-- material each plant used, single out sunflower oil at P1; attributed to the lots they were counted on, the
-- write-offs follow one receiving clerk with one supplier, and nobody else receiving the same oil.
WITH used AS (
  SELECT b.plant_id, c.material_code, sum(c.qty) AS used
  FROM gatr.batch_consumption c JOIN gatr.production_batches b USING (batch_id) GROUP BY 1, 2),
shrink AS (
  SELECT plant_id, material_code, sum(difference) AS counted_diff FROM gatr.cycle_counts GROUP BY 1, 2),
by_material AS (
  SELECT s.plant_id, s.material_code, round(100 * s.counted_diff / u.used, 2) AS shrink_pct_of_use,
         rank() OVER (ORDER BY s.counted_diff / u.used) AS worst
  FROM shrink s JOIN used u USING (plant_id, material_code) WHERE u.used > 0),
lot_diff AS (  -- each oil lot's count differences, by who received it from whom
  SELECT g.received_by, g.supplier_id, count(DISTINCT g.internal_lot) AS lots, sum(g.qty_received) AS received,
         coalesce(sum(cc.difference), 0) AS written_off
  FROM gatr.goods_receipts g LEFT JOIN gatr.cycle_counts cc ON cc.lot = g.internal_lot
  WHERE g.material_code = 'RM-ACGI' AND g.plant_id = 'P1' AND g.receipt_date >= '2023-01-01'
  GROUP BY 1, 2)
SELECT 'material' AS view, plant_id || ' ' || material_code AS who, shrink_pct_of_use AS pct, NULL::bigint AS lots
FROM by_material WHERE worst <= 3
UNION ALL
SELECT 'oil P1 by clerk x supplier', received_by || ' x ' || supplier_id,
       round(100 * written_off / received, 2), lots
FROM lot_diff WHERE lots >= 5
ORDER BY 1, 3;
