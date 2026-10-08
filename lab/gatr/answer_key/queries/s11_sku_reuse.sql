-- Story 11: a retired SKU code handed to a new product. products has two rows for the code; joining sales lines on
-- sku_code alone doubles them and mixes a 250 g jar of salsa criolla with a 500 g mayonnaise. Resolve with launch and
-- discontinued dates.
WITH dup AS (SELECT sku_code FROM gatr.products GROUP BY 1 HAVING count(*) > 1)
SELECT p.sku_code, p.description, p.launch_date, p.discontinued_date,
       count(l.*) FILTER (WHERE i.invoice_date BETWEEN coalesce(p.launch_date, '1900-01-01')
                          AND coalesce(p.discontinued_date, '9999-12-31')) AS lines_in_its_life,
       count(l.*) AS lines_by_code_alone
FROM gatr.products p JOIN dup USING (sku_code)
LEFT JOIN gatr.sales_order_lines l ON ltrim(l.sku_code, '0') = ltrim(p.sku_code, '0')
LEFT JOIN LATERAL (SELECT l.invoice_date) i ON true
GROUP BY 1, 2, 3, 4 ORDER BY 1, 3;
