-- Story 12: one supervisor approves inflated overtime for six of their operators. Pair punches into shifts, attach
-- employees by legajo, and compare overtime per head across P2's crews; then look at the days those six work when
-- the rest of their crew (on P2's 4x4 rotation) is off, and at what the line produced on those days.
WITH p AS (
  SELECT legajo, to_timestamp(fecha_hora, 'DD/MM/YYYY HH24:MI') AS t, tipo, autorizado_por,
         replace(horas_extra, ',', '.')::numeric AS ot
  FROM gatr.timeclock_punches),
ot AS (
  SELECT e.employee_id, e.legajo, e.line_id, e.crew, e.supervisor_id, s.legajo AS sup_legajo,
         sum(p.ot) FILTER (WHERE p.tipo = 'S') AS ot_hours, count(*) FILTER (WHERE p.tipo = 'E') AS shifts
  FROM p JOIN gatr.employees e ON e.legajo = p.legajo LEFT JOIN gatr.employees s ON s.employee_id = e.supervisor_id
  WHERE e.plant_id = 'P2' AND e.role IN ('OPERARIO', 'LIDER DE LINEA') AND p.t >= '2024-03-01' AND p.t < '2026-01-01'
  GROUP BY 1, 2, 3, 4, 5, 6),
crew_days AS (  -- per day, how many of each crew punched in
  SELECT e.line_id, e.crew, p.t::date AS day, count(DISTINCT e.employee_id) AS present
  FROM p JOIN gatr.employees e ON e.legajo = p.legajo
  WHERE e.plant_id = 'P2' AND p.tipo = 'E' GROUP BY 1, 2, 3),
alone AS (  -- shifts worked on days when at most 6 of the crew came in
  SELECT e.employee_id, count(*) AS alone_days
  FROM p JOIN gatr.employees e ON e.legajo = p.legajo
  JOIN crew_days c ON c.line_id = e.line_id AND c.crew = e.crew AND c.day = p.t::date
  WHERE e.plant_id = 'P2' AND p.tipo = 'E' AND c.present <= 6 AND p.t >= '2024-03-01' AND p.t < '2026-01-01'
  GROUP BY 1)
SELECT o.line_id, o.crew, o.sup_legajo, count(*) AS people,
       round(sum(o.ot_hours) / count(*)) AS ot_hours_per_head,
       round(percentile_cont(0.9) WITHIN GROUP (ORDER BY coalesce(o.ot_hours, 0))) AS p90_ot_hours,
       sum(coalesce(a.alone_days, 0)) AS shifts_without_their_crew
FROM ot o LEFT JOIN alone a USING (employee_id)
GROUP BY 1, 2, 3 HAVING count(*) >= 5 ORDER BY ot_hours_per_head DESC NULLS LAST LIMIT 8;
