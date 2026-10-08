-- Story 14: one sales rep pushes extra volume into five distributors in the last week of each quarter; most of it
-- comes back as credit notes a few weeks later (overstock, slow rotation, near expiry). Before the July 2024 upgrade
-- credit notes are NC orders with negative lines; after it they are in `returns` (and April-June 2024 are in both).
WITH s AS (
  SELECT o.sales_rep_id, o.customer_id, o.order_date, l.net_amount
  FROM gatr.sales_orders o JOIN gatr.sales_order_lines l USING (order_number)
  WHERE o.order_type = 'PED' AND o.status NOT IN ('ANUL', 'CANCELLED') AND l.currency = 'ARS'),
qe AS (  -- share of each rep's sales booked in the last 7 days of a quarter
  SELECT sales_rep_id,
         round(100 * sum(net_amount) FILTER (WHERE extract(month FROM order_date) IN (3, 6, 9, 12)
                                             AND order_date + 7 >= date_trunc('month', order_date) + interval '1 month')
               / sum(net_amount), 1) AS pct_last_week_of_quarter
  FROM s GROUP BY 1),
cn AS (  -- credit notes for overstock, from both places, without double counting the overlap
  SELECT customer_id, amount FROM gatr.returns
  WHERE reason IN ('SOBRESTOCK', 'ROTACION LENTA', 'VENCIMIENTO PROXIMO') AND issue_date >= '2024-07-01'
  UNION ALL
  SELECT o.customer_id, -l.net_amount FROM gatr.sales_orders o JOIN gatr.sales_order_lines l USING (order_number)
  WHERE o.order_type = 'NC' AND o.order_date < '2024-07-01'),
cn_by_rep AS (
  SELECT c.sales_rep_id, sum(cn.amount) AS credits FROM cn JOIN gatr.customers c USING (customer_id) GROUP BY 1)
SELECT q.sales_rep_id, e.full_name, q.pct_last_week_of_quarter,
       round(100 * coalesce(r.credits, 0) / (SELECT sum(net_amount) FROM s WHERE s.sales_rep_id = q.sales_rep_id), 2)
         AS credits_pct_of_sales
FROM qe q JOIN gatr.employees e ON e.employee_id = q.sales_rep_id LEFT JOIN cn_by_rep r USING (sales_rep_id)
ORDER BY 3 DESC LIMIT 6;
