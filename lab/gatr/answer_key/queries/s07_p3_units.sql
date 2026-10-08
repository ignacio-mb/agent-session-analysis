-- Story 7: P3's legacy production is kg for sauces and bread, pieces for frozen food (an empanada pack of 12 is 12:
-- the pack size is only in the description, 'X12'); P2 records cases.
-- A naive union of "quantity produced" per plant against the same in consumer units, for the year P3 was on its
-- legacy system (and owned by GATR). The legacy quantities have decimal commas and dot thousands separators.
WITH erp AS (
  SELECT b.plant_id, p.family, sum(b.good_qty) AS naive_qty,
         sum(b.good_qty * CASE WHEN b.uom = 'CJ' THEN p.units_per_case ELSE 1 END) AS units
  FROM gatr.production_batches b JOIN gatr.products p ON p.sku_code = b.item_code
  WHERE b.deleted_at IS NULL AND b.start_time ~ '^\d{4}' AND b.start_time >= '2024-03-01' AND b.start_time < '2025-03-01'
    AND b.good_qty < 10 * b.planned_qty
  GROUP BY 1, 2),
legacy AS (
  SELECT 'P3' AS plant_id, a.familia AS family,
         sum(replace(replace(d.cantidad, '.', ''), ',', '.')::numeric) AS naive_qty,
         sum(CASE WHEN upper(d.unidad) LIKE 'K%'
                  THEN replace(replace(d.cantidad, '.', ''), ',', '.')::numeric / replace(a.peso_neto, ',', '.')::numeric
                  ELSE replace(replace(d.cantidad, '.', ''), ',', '.')::numeric
                       / coalesce(substring(a.descripcion FROM ' X(\d+) ')::numeric, 1) END) AS units
  FROM gatr.p3_produccion_diaria d JOIN gatr.p3_articulos a ON a.cod_articulo::text = d.cod_articulo
  WHERE to_date(d.fecha, 'DD/MM/YYYY') BETWEEN '2024-03-01' AND '2025-02-28'
  GROUP BY 1, 2)
SELECT plant_id, family, round(naive_qty) AS naive_qty, round(units) AS units,
       round(naive_qty / nullif(units, 0), 3) AS naive_over_units
FROM (SELECT * FROM erp UNION ALL SELECT * FROM legacy) x
WHERE family IN ('SALSAS-FRASCO', 'SALSAS', 'CONGELADOS', 'PANIFICADOS')
ORDER BY 2, 1;
