-- Story 1: the sesame recall's real blast radius.
-- The supplier lot reads 'MEC-23-0457' at P1 and 'MEC230457' at P2 (P2's scanners strip separators): normalise it to
-- find both receipts. Batches that consumed either internal lot, plus P1 batches that backflushed paprika without a lot
-- while the P1 lot was in use, are the affected lots. Customers: shipments of those lots, plus shipments with no lot of
-- the same SKU from the same warehouse within the lots' shipping window. Compared with what the recall covered.
WITH sup AS (
  SELECT plant_id, internal_lot FROM gatr.goods_receipts
  WHERE upper(regexp_replace(supplier_lot, '[^A-Za-z0-9]', '', 'g')) = 'MEC230457'),
traced AS (                       -- lot recorded on the consumption row
  SELECT DISTINCT c.batch_id FROM gatr.batch_consumption c JOIN sup ON c.lot = sup.internal_lot),
window_p1 AS (                    -- when the P1 lot was being consumed
  SELECT min(b.created_at) AS a, max(b.created_at) AS b
  FROM gatr.batch_consumption c JOIN sup ON c.lot = sup.internal_lot AND sup.plant_id = 'P1'
  JOIN gatr.production_batches b USING (batch_id)),
untraced AS (                     -- P1 batches that used paprika with no lot scanned, in that window
  SELECT DISTINCT c.batch_id FROM gatr.batch_consumption c JOIN gatr.production_batches b USING (batch_id), window_p1 w
  WHERE c.material_code = 'RM-PIMA' AND c.lot IS NULL AND b.plant_id = 'P1' AND b.created_at BETWEEN w.a AND w.b),
affected AS (
  SELECT DISTINCT b.lot_code, b.item_code, b.plant_id FROM gatr.production_batches b
  WHERE b.batch_id IN (SELECT batch_id FROM traced UNION SELECT batch_id FROM untraced) AND b.deleted_at IS NULL),
ship_lot AS (
  SELECT s.* FROM gatr.shipments s JOIN affected a ON s.lot_code = a.lot_code),
ship_nolot AS (                   -- same SKU, same warehouse, no lot captured, while affected lots were shipping
  SELECT s.* FROM gatr.shipments s
  JOIN (SELECT ltrim(item_code, '0') AS sku, min(ship_date) AS a, max(ship_date) AS b, warehouse
        FROM ship_lot JOIN affected USING (lot_code) GROUP BY 1, 4) w
    ON s.sku = w.sku AND s.warehouse = w.warehouse AND s.ship_date BETWEEN w.a AND w.b
  WHERE s.lot_code IS NULL),
official AS (SELECT DISTINCT lot_code FROM gatr.recalls WHERE recall_id = 'RET-2023-01')
SELECT
  (SELECT count(*) FROM official) AS official_lots,
  (SELECT count(DISTINCT s.customer_id) FROM gatr.shipments s JOIN official o USING (lot_code)) AS official_customers,
  (SELECT count(*) FROM affected) AS affected_lots,
  (SELECT count(*) FROM affected WHERE plant_id = 'P2') AS affected_lots_p2,
  (SELECT count(*) FROM affected a WHERE a.lot_code NOT IN (SELECT lot_code FROM official)) AS lots_missed_by_recall,
  (SELECT count(DISTINCT customer_id) FROM ship_lot) AS customers_by_lot,
  (SELECT count(DISTINCT customer_id) FROM (SELECT customer_id FROM ship_lot UNION SELECT customer_id FROM ship_nolot) x)
    AS customers_incl_missing_lots;
