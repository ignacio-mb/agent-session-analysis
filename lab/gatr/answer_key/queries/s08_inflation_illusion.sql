-- Story 8: domestic revenue in pesos multiplies while volume is flat or falling. Clean the exchange rates first: one
-- rate per business day (the corrected row wins), drop fat-fingered values (more than 30% off the median of the
-- surrounding two weeks), carry the last rate over weekends and holidays. Then revenue in ARS, in USD, and units, by
-- segment and year (2026 is January-September: compare per month).
WITH fx1 AS (
  SELECT DISTINCT ON (rate_date) rate_date, rate FROM gatr.fx_rates ORDER BY rate_date, source DESC),
fx2 AS MATERIALIZED (
  SELECT f.rate_date, f.rate,
         (SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY g.rate) FROM fx1 g
           WHERE g.rate_date BETWEEN f.rate_date - 10 AND f.rate_date + 10) AS med
  FROM fx1 f),
fx_ok AS MATERIALIZED (SELECT rate_date, rate FROM fx2 WHERE abs(rate / med - 1) < 0.3),
fx AS MATERIALIZED (
  SELECT d::date AS day, (SELECT rate FROM fx_ok WHERE rate_date <= d ORDER BY rate_date DESC LIMIT 1) AS rate
  FROM generate_series('2022-01-01'::date, '2026-09-30', '1 day') d),
lines AS (  -- invoiced lines of real orders; credit notes and annulled copies out; one copy of reloaded lines
  SELECT DISTINCT ON (l.order_number, l.line_no) o.customer_id, l.invoice_date, l.net_amount, l.sku_code,
         l.qty_shipped, l.uom
  FROM gatr.sales_order_lines l JOIN gatr.sales_orders o USING (order_number)
  WHERE o.order_type = 'PED' AND o.status NOT IN ('ANUL', 'CANCELLED') AND l.invoice_date IS NOT NULL
  ORDER BY l.order_number, l.line_no, l._loaded_at),
seg AS (SELECT customer_id, segment FROM gatr.customers)
SELECT s.segment, extract(year FROM l.invoice_date)::int AS year,
       count(DISTINCT date_trunc('month', l.invoice_date)) AS months,
       round(sum(l.net_amount) / 1e9 / count(DISTINCT date_trunc('month', l.invoice_date)), 2) AS ars_bn_per_month,
       round(sum(l.net_amount / fx.rate) / 1e6 / count(DISTINCT date_trunc('month', l.invoice_date)), 2)
         AS usd_m_per_month,
       round(sum(l.qty_shipped * CASE WHEN l.uom = 'CJ' THEN p.units_per_case ELSE 1 END) / 1e6
             / count(DISTINCT date_trunc('month', l.invoice_date)), 2) AS m_units_per_month
FROM lines l JOIN seg s USING (customer_id) JOIN fx ON fx.day = l.invoice_date
LEFT JOIN gatr.products p ON ltrim(p.sku_code, '0') = ltrim(l.sku_code, '0') AND p.discontinued_date IS NULL
WHERE s.segment IN ('SUP', 'FS', 'DIS', 'MAY')
GROUP BY 1, 2 ORDER BY 1, 2;
