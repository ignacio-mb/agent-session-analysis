-- Story 10: La Estrella acquires Costa Azul (2025-04-01). Costa Azul's stores keep ordering under their old ids
-- until they are re-created as La Estrella stores (2025-09-15); La Estrella's stores move under regional
-- distribution centres. Ranking chains by 2025 revenue three ways: the store's parent today, the parent in force on
-- the order date (one level), and the ultimate group (walking the hierarchy to the top, as of the order date).
WITH rev AS (
  SELECT o.customer_id, o.order_date, sum(l.net_amount) AS ars
  FROM gatr.sales_orders o JOIN gatr.sales_order_lines l USING (order_number)
  WHERE o.order_date >= '2025-01-01' AND o.order_date < '2026-01-01' AND o.order_type = 'PED'
    AND o.status NOT IN ('ANUL', 'CANCELLED') AND l.currency = 'ARS'
  GROUP BY 1, 2),
cur AS (SELECT child_customer_id, parent_customer_id FROM gatr.customer_hierarchy WHERE valid_to IS NULL),
at_date AS (
  SELECT r.*, h.parent_customer_id AS parent FROM rev r LEFT JOIN gatr.customer_hierarchy h
    ON h.child_customer_id = r.customer_id AND r.order_date >= h.valid_from
   AND (h.valid_to IS NULL OR r.order_date <= h.valid_to)),
top AS (  -- the group at the top of the hierarchy
  WITH RECURSIVE up(start, node, depth) AS (
    SELECT DISTINCT parent, parent, 0 FROM at_date WHERE parent IS NOT NULL
    UNION ALL
    SELECT up.start, h.parent_customer_id, depth + 1 FROM up JOIN gatr.customer_hierarchy h
      ON h.child_customer_id = up.node AND h.valid_to IS NULL WHERE depth < 5)
  SELECT DISTINCT ON (start) start, node AS ultimate FROM up ORDER BY start, depth DESC),
named AS (SELECT customer_id, customer_name FROM gatr.customers)
SELECT 'parent today' AS method, n.customer_name, round(sum(r.ars) / 1e6) AS ars_millions,
       rank() OVER (PARTITION BY 'a' ORDER BY sum(r.ars) DESC) AS rnk
FROM rev r JOIN cur ON cur.child_customer_id = r.customer_id JOIN named n ON n.customer_id = cur.parent_customer_id
GROUP BY n.customer_name
UNION ALL
SELECT 'parent on order date', n.customer_name, round(sum(a.ars) / 1e6), rank() OVER (ORDER BY sum(a.ars) DESC)
FROM at_date a JOIN named n ON n.customer_id = a.parent GROUP BY n.customer_name
UNION ALL
SELECT 'ultimate group', n.customer_name, round(sum(a.ars) / 1e6), rank() OVER (ORDER BY sum(a.ars) DESC)
FROM at_date a JOIN top t ON t.start = a.parent JOIN named n ON n.customer_id = t.ultimate GROUP BY n.customer_name
ORDER BY 1, 4 LIMIT 40;
