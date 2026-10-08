-- Story 4: the line whose operators don't log their downtime. The MES's OEE comes from what operators log. Rebuild it
-- from output instead: ideal hours (good output / the order's standard rate) over staffed hours (shifts in the MES
-- report x shift length), and set logged downtime against the maintenance system's corrective work orders.
WITH mes AS (
  SELECT m.line_id, count(*) * max(l.shift_hours) * 60 AS staffed_min, round(avg(m.oee_pct), 1) AS reported_oee,
         sum(m.downtime_minutes) AS mes_downtime_min
  FROM gatr.mes_oee_daily m JOIN gatr.production_lines l USING (line_id) GROUP BY 1),
ideal AS (
  SELECT b.line_id, sum(b.good_qty / nullif(o.std_rate_per_hour, 0)) * 60 AS ideal_min
  FROM gatr.production_batches b JOIN gatr.production_orders o USING (order_no)
  WHERE b.deleted_at IS NULL AND b.good_qty < 10 * b.planned_qty AND b.plant_id IN ('P1', 'P2')
  GROUP BY 1),
logged AS (SELECT line_id, sum(duration_min) FILTER (WHERE reason NOT IN ('CAMBIO DE FORMATO', '01')) AS logged_min
           FROM gatr.downtime_events GROUP BY 1),
cmms AS (
  SELECT e.line_id, sum(w.downtime_minutes) AS wo_min, count(*) AS corrective_wos
  FROM gatr.maintenance_work_orders w JOIN gatr.equipment e USING (asset_id)
  WHERE w.wo_type = 'CORR' GROUP BY 1)
SELECT line_id, reported_oee, round(100 * ideal_min / staffed_min, 1) AS oee_from_output,
       rank() OVER (ORDER BY reported_oee DESC) AS reported_rank,
       rank() OVER (ORDER BY ideal_min / staffed_min DESC) AS rank_from_output,
       logged_min, wo_min, round(wo_min::numeric / nullif(logged_min, 0), 2) AS cmms_vs_logged
FROM mes JOIN ideal USING (line_id) JOIN logged USING (line_id) JOIN cmms USING (line_id)
ORDER BY oee_from_output;
