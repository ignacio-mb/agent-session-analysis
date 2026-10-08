-- Story 13: the food-service price list meant for August 2023 was keyed effective November (Excel serials:
-- 45139 = 2023-08-01, 45231 = 2023-11-01) and September/October were never loaded: food service paid July prices
-- through three months of double-digit inflation. Price index of a basket of SKUs every segment has, by month,
-- relative to January 2023, and the lists' created_at against their effective dates.
WITH pl AS (
  SELECT segment, price_list_id, (date '1899-12-30' + effective_from) AS eff, min(created_at)::date AS created,
         replace(unit_price, ',', '.')::numeric AS price, sku_code
  FROM gatr.price_lists WHERE currency = 'ARS' GROUP BY 1, 2, 3, 5, 6),
basket AS (SELECT sku_code FROM pl GROUP BY 1 HAVING count(DISTINCT segment) = 4),
idx AS (
  SELECT segment, eff, created, avg(price) AS avg_price FROM pl JOIN basket USING (sku_code) GROUP BY 1, 2, 3),
base AS (SELECT segment, avg_price AS p0 FROM idx WHERE eff = '2023-01-01')
SELECT i.segment, i.eff AS effective_from, i.created, i.eff - i.created AS days_ahead,
       round(100 * i.avg_price / b.p0) AS index_jan23
FROM idx i JOIN base b USING (segment)
WHERE i.eff BETWEEN '2023-06-01' AND '2024-01-01'
ORDER BY i.eff, i.segment;
