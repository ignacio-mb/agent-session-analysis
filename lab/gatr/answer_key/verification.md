# Verification

Written by `generator/verify.py` on the data in `lab/db/gatr/` (163 s). Every check below ran on the recorded tables as loaded into Postgres, using nothing from the answer key except to compare.

## Stories: found from the recorded data alone

| Story | Query | Found | What the query shows |
|---|---|---|---|
| S01 Sesame recall: the real blast radius | [`s01_sesame_blast_radius.sql`](queries/s01_sesame_blast_radius.sql) | yes | 91 affected lots (35 at P2), 42 missed by the recall; 423 customers reached vs 284 through the recalled lots (true: 90 lots, 423 customers) |
| S02 Supplier ownership change: quality slides under a new id | [`s02_supplier_ownership.sql`](queries/s02_supplier_ownership.sql) | yes | same tax id, mean log10 UFC/g 1.75 before the change, 2.42 in the last year; failures 0.0% -> 1.8% |
| S03 Cost-cut mayonnaise recipe | [`s03_costcut_recipe.sql`](queries/s03_costcut_recipe.sql) | yes | version 4: 3.85% of batches fail release tests and 7.54% of lots draw complaints, vs at most 1.91% / 2.68% for the other versions |
| S04 Hidden downtime on L1-04 | [`s04_hidden_downtime.sql`](queries/s04_hidden_downtime.sql) | yes | reported OEE 86.0% (rank 1); from output 52.1% (rank 7 of 10); maintenance logs 4.00x the downtime operators log (others <= 0.74x) |
| S05 Receiving fraud: one clerk, one supplier, sunflower oil at P1 | [`s05_receiving_fraud.sql`](queries/s05_receiving_fraud.sql) | yes | P1 oil write-offs -1.41% of use (next worst -0.13%); on lots received by 1423 x 200397: -2.66% of what was received, vs -0.03% for the next clerk x supplier |
| S06 Freezer FRZ-N2's night-time excursions | [`s06_freezer_excursions.sql`](queries/s06_freezer_excursions.sql) | yes | 1324 readings over the alarm in FRZ-N2 Jan-Aug 2025, 100% between 02:00 and 04:59 local; complaints per lot stored there 0.106 vs 0.028 in FRZ-N1 |
| S07 P3's units | [`s07_p3_units.sql`](queries/s07_p3_units.sql) | yes | naive quantity / consumer units by plant and family: P1 CONGELADOS 1.000, P2 CONGELADOS 0.100, P3 CONGELADOS 7.171, P1 PANIFICADOS 1.000, P3 PANIFICADOS 0.470, P3 SALSAS 0.422, P1 SALSAS-FRASCO 1.000, P2 SALSAS-FRASCO 0.063 |
| S08 Inflation illusion | [`s08_inflation_illusion.sql`](queries/s08_inflation_illusion.sql) | yes | food service: pesos per month x7.0 2022->2025 while units per month x0.75; supermarkets units x1.51 |
| S09 Simpson's paradox in yield | [`s09_simpson_yield.sql`](queries/s09_simpson_yield.sql) | yes | overall yield P2 95.38% > P1 93.24%, yet P1 is better on 240 of 240 SKUs both make |
| S10 Customer consolidation | [`s10_customer_consolidation.sql`](queries/s10_customer_consolidation.sql) | yes | by ultimate group the top chain is Supermercados La Estrella S.A. (5689 M ARS); by today's parent it is Almacenes del Plata S.R.L., with La Estrella split across its distribution centres |
| S11 A reused SKU code | [`s11_sku_reuse.sql`](queries/s11_sku_reuse.sql) | yes | 010283 Papas Onduladas 900 g: 157 lines in its life, 894 by code alone; 010283 Crackers Semillas 300 g: 723 lines in its life, 894 by code alone; 030164 Salsa Criolla 250 g: 1007 lines in its life, 2973 by code alone; 030164 Mayonesa con Oliva 500 g: 1934 lines in its life, 2973 by code alone |
| S12 Inflated overtime | [`s12_overtime.sql`](queries/s12_overtime.sql) | yes | crew A of SUR-03 (supervisor S1565): p90 overtime 811.0 h per person, 396 shifts worked while the rest of the crew was off |
| S13 Food-service price list keyed with the wrong month | [`s13_price_list_lag.sql`](queries/s13_price_list_lag.sql) | yes | a food-service list effective 2023-11-01 was created 99 days ahead at index 159 (others: 218); no food-service lists for August-October |
| S14 Quarter-end channel stuffing | [`s14_channel_stuffing.sql`](queries/s14_channel_stuffing.sql) | yes | rep 1029: 15.4% of sales in a quarter's last week (next 9.5%), overstock credit notes 4.76% of sales |
| S15 Overfilling checkweigher on SUR-01 | [`s15_checkweigher_overfill.sql`](queries/s15_checkweigher_overfill.sql) | yes | SUR-01 bags 1.2% over declared weight before October 2024, 4.5% November-February, back after the March 2025 fix |

## Stories: not given away by a trivial single-table GROUP BY

The first thing an analyst would try, per story, and what it shows.

| Story | Naive query | Doesn't give it away | What it shows |
|---|---|---|---|
| S01 | `SELECT count(DISTINCT lot_code), sum(customers_notified) FROM gatr.recalls WHERE recall_id = 'RET-2023-01'` | yes | the recall table lists 49 lots: it is the official scope, not the blast radius |
| S02 | `incoming micro failure rate by supplier_id (P1/P3 rows)` | yes | by supplier_id the new id ranks 32 of 32 on failure rate: not the outlier |
| S03 | `yeast/mould failure rate by quarter, all products` | yes | quarterly yeast/mould failure across products ranges 0.57-1.54% (median 1.00%): a bump, not a story |
| S04 | `SELECT line_id, round(avg(oee_pct), 1) AS oee FROM gatr.mes_oee_daily GROUP BY 1 ORDER BY 2 DESC` | yes | the MES report ranks L1-04 first (86.0%): the naive answer is the opposite of the truth |
| S05 | `SELECT received_by, count(*) AS receipts, round(avg(qty_received)) AS avg_qty FROM gatr.goods_receipts WHERE plant_id = 'P1' GROUP BY 1 ORDER BY 2 DESC` | yes | receipts per clerk look ordinary: the clerk receives the most because they work the day shift |
| S06 | `SELECT extract(hour FROM reading_time) AS hour, count(*) FROM gatr.cold_storage_sensor_readings WHERE sensor_id = 'TS-FRZ-N2' AND value > -12 GROUP BY 1 ORDER BY 2 DESC` | yes | by the recorded clock the excursions peak at 1:00, not in the defrost window |
| S07 | `SELECT 'ERP' AS src, plant_id, sum(good_qty) FROM gatr.production_batches GROUP BY 1, 2 UNION ALL SELECT 'P3 legacy', 'P3', sum(replace(replace(cantidad, '.', ''), ',', '.')::numeric) FROM gatr.p3_produccion_diaria` | yes | summing quantities across plants adds kilograms, pieces, units and cases |
| S08 | `SELECT extract(year FROM invoice_date) AS y, round(sum(net_amount) / 1e9, 1) AS ars_bn FROM gatr.sales_order_lines GROUP BY 1 ORDER BY 1` | yes | revenue in pesos grows x14 from 2022 to 2025: growth, on its face |
| S09 | `SELECT plant_id, round(100 * avg(good_qty / nullif(planned_qty, 0)), 2) AS avg_yield FROM gatr.production_batches GROUP BY 1 ORDER BY 1` | yes | average yield by plant (with impossible rows in): P1 92.34, P2 92.56, P3 91.94 |
| S10 | `SELECT o.customer_id, round(sum(l.net_amount) / 1e6) AS ars_m FROM gatr.sales_orders o JOIN gatr.sales_order_lines l USING (order_number) WHERE o.order_date >= '2025-01-01' AND o.order_date < '2026-01-01' GROUP BY 1 ORDER BY 2 DESC LIMIT 5` | yes | by customer_id the top 'customer' is 002772: single stores and distributors, no chain |
| S11 | `SELECT sku_code, count(*) FROM gatr.products GROUP BY 1 HAVING count(*) > 1` | yes | two duplicate codes are visible, but not that sales history mixes two products |
| S12 | `SELECT autorizado_por, round(sum(replace(horas_extra, ',', '.')::numeric)) AS hours FROM gatr.timeclock_punches WHERE horas_extra IS NOT NULL GROUP BY 1 ORDER BY 2 DESC LIMIT 10` | yes | by approver, S1565 is not among the top three: Sunday shifts at P1 dominate overtime |
| S13 | `SELECT segment, count(*) FROM gatr.price_lists GROUP BY 1` | yes | line counts per segment differ by a few percent: nothing points at a three-month gap |
| S14 | `SELECT reason, count(*), round(sum(amount)) FROM gatr.returns GROUP BY 1 ORDER BY 3 DESC` | yes | returns by reason: overstock returns exist, but by reason alone they're not tied to anyone |
| S15 | `SELECT b.line_id, count(*) FILTER (WHERE NOT q.passed) AS failed FROM gatr.quality_tests q JOIN gatr.production_batches b USING (batch_id) WHERE q.test_name = 'PESO NETO' GROUP BY 1` | yes | no net-weight test ever fails: overfill is inside the spec, and P2's results are in JSON |

## Naive vs true metrics

32 metrics (163 rows with their dimensions) in [ground_truth_metrics.csv](ground_truth_metrics.csv). Metrics whose naive value is more than 10% off the true value (in most of their rows): **23** of 32.

| Metric | Dimension | True | Naive | Gap |
|---|---|---:|---:|---:|
| M01 Revenue in ARS, domestic, net of credit notes | 2022-06 | 581,196,948.33 | 502,487,393.04 | -13.5% |
| M02 Revenue in USD, net of credit notes | 2022-06 | 5,307,973.92 | 4,082,300.23 | -23.1% |
| M01 Revenue in ARS, domestic, net of credit notes | 2023-10 | 1,688,079,423.70 | 1,464,451,368.55 | -13.2% |
| M02 Revenue in USD, net of credit notes | 2023-10 | 5,750,119.62 | 3,975,765.64 | -30.9% |
| M01 Revenue in ARS, domestic, net of credit notes | 2024-07 | 7,117,252,129.29 | 7,269,535,872.50 | +2.1% |
| M02 Revenue in USD, net of credit notes | 2024-07 | 8,759,745.52 | 7,813,463.86 | -10.8% |
| M01 Revenue in ARS, domestic, net of credit notes | 2025-06 | 9,338,320,933.68 | 7,657,221,215.63 | -18.0% |
| M02 Revenue in USD, net of credit notes | 2025-06 | 7,842,153.36 | 5,455,740.47 | -30.4% |
| M01 Revenue in ARS, domestic, net of credit notes | 2026-06 | 12,301,693,895.14 | 10,649,961,118.05 | -13.4% |
| M02 Revenue in USD, net of credit notes | 2026-06 | 9,053,654.86 | 6,809,235.43 | -24.8% |
| M03 Revenue in ARS 2025, net of credit notes |  | 121,436,074,804.25 | 112,843,833,285.90 | -7.1% |
| M04 Export revenue 2024 |  | 13,636,596.22 | 14,406,037.28 | +5.6% |
| M05 OEE by line | SUR-04 | 42.65 | 70.49 | +65.3% |
| M05 OEE by line | L1-05 | 61.27 | 73.71 | +20.3% |
| M05 OEE by line | L1-01 | 57.13 | 76.03 | +33.1% |
| M05 OEE by line | SUR-01 | 36.95 | 70.45 | +90.7% |
| M05 OEE by line | L1-06 | 56.75 | 74.15 | +30.7% |
| M05 OEE by line | SUR-02 | 59.08 | 76.01 | +28.7% |
| M05 OEE by line | L1-04 | 51.98 | 86.02 | +65.5% |
| M05 OEE by line | L1-02 | 60.03 | 74.93 | +24.8% |
| M05 OEE by line | L1-03 | 56.39 | 80.96 | +43.6% |
| M05 OEE by line | SUR-03 | 51.14 | 71.36 | +39.5% |
| M06 OEE, P1 and P2 lines |  | 55.34 | 75.41 | +36.3% |
| M07 Sesame recall blast radius: lots |  | 90.00 | 49.00 | -45.6% |
| M08 Sesame recall blast radius: customers |  | 423.00 | 284.00 | -32.9% |
| M09 P1 sunflower oil received but never delivered, since 2023-06 |  | 208,554.42 | 0.00 | -100.0% |
| M10 P1 sunflower oil physical losses (true shrink) |  | 0.00 | 207,876.04 | n/a (true is 0) |
| M11 Yield (good / planned, kg) | P1 CHIPS | 86.54 | 86.75 | +0.2% |
| M11 Yield (good / planned, kg) | P1 CRACKERS | 89.55 | 89.71 | +0.2% |
| M11 Yield (good / planned, kg) | P1 FROZEN | 93.05 | 93.21 | +0.2% |
| M11 Yield (good / planned, kg) | P1 SAUCE_JAR | 98.57 | 98.88 | +0.3% |
| M11 Yield (good / planned, kg) | P2 CHIPS | 84.77 | 84.92 | +0.2% |
| M11 Yield (good / planned, kg) | P2 CRACKERS | 88.46 | 88.84 | +0.4% |
| M11 Yield (good / planned, kg) | P2 FROZEN | 92.31 | 93.51 | +1.3% |
| M11 Yield (good / planned, kg) | P2 SAUCE_JAR | 97.48 | 97.71 | +0.2% |
| M12 Yield by plant | P1 | 93.19 | 92.34 | -0.9% |
| M12 Yield by plant | P2 | 95.39 | 92.56 | -3.0% |
| M13 Units produced, Mar 2024 - Feb 2025 | P1 | 109,714,570.00 | 109,865,217.00 | +0.1% |
| M13 Units produced, Mar 2024 - Feb 2025 | P2 | 19,201,448.00 | 1,143,715.85 | -94.0% |
| M13 Units produced, Mar 2024 - Feb 2025 | P3 | 18,101,973.00 | 22,331,472.20 | +23.4% |
| M14 Top customer group's share of 2025 domestic revenue |  | 4.62 | 2.29 | -50.5% |
| M15 Customers buying in 2025 |  | 1,902.00 | 2,018.00 | +6.1% |
| M16 Customers of 2024 not buying in 2025-H2 |  | 129.00 | 131.00 | +1.6% |
| M17 Food-service revenue lost to the mis-keyed price list |  | 3,848,591.73 | 0.00 | -100.0% |
| M18 Credit notes issued in 2024 |  | 261,914,697.98 | 308,819,345.46 | +17.9% |
| M19 Complaint rate ratio, cost-cut mayonnaise lots vs other mayonnaise lots |  | 3.87 | 2.69 | -30.5% |
| M20 Spice supplier's incoming micro failure rate, Oct 2025 - Sep 2026 |  | 1.56 | 0.00 | -100.0% |
| M21 L1-04 unplanned downtime per production day |  | 500.00 | 92.82 | -81.4% |
| M22 Overtime hours with no production behind them |  | 4,622.12 | 0.00 | -100.0% |
| M23 FRZ-N2 readings over alarm between 02:00 and 04:59 local, 2025 |  | 1,322.00 | 357.00 | -73.0% |
| M24 P3 chiller CH-D1 mean temperature 2024 |  | 3.07 | 37.53 | +1121.1% |
| M25 Headcount on 2026-09-30 |  | 616.00 | 674.00 | +9.4% |
| M26 Units shipped in 2025 |  | 146,979,607.00 | 6,361,171.53 | -95.7% |
| M27 Fill rate 2025 (units shipped / ordered) |  | 97.96 | 99.33 | +1.4% |
| M28 Food service growth 2022 -> 2025 |  | 0.70 | 7.23 | +929.9% |
| M29 Waste in 2024 |  | 975,698.62 | 177,824.40 | -81.8% |
| M30 P3 electricity, 2025-05-05 to year end |  | 4,881,597.90 | 8,692,451,350.70 | +177965.7% |
| M31 Suppliers (legal entities) |  | 164.00 | 180.00 | +9.8% |
| M32 Products sold in 2025 |  | 314.00 | 491.00 | +56.4% |

(M01 and M02 are monthly: five months shown, all in the CSV.)

## Mess profile

Rows, exact duplicate rows, and per column the share of null-like values (NULL, '', 'N/A', '-', 'null', 's/d', and 1900-01-01 / 9999-12-31 in date columns).

Duplicates: exact duplicate rows, and rows that repeat another but for the columns a re-ingested or re-entered copy changes (load timestamps, ids, status), listed per table in data_profile.json.

| Table | Rows | Exact duplicates | Near-duplicates | Most null-like columns |
|---|---:|---:|---:|---|
| `audit_log` | 5,085 | 0 (0.00%) | 0 (0.00%) |  |
| `batch_consumption` | 762,069 | 0 (0.00%) | 0 (0.00%) | lot 11.2% |
| `cold_storage_sensor_readings` | 3,413,645 | 10,212 (0.30%) | 10,212 (0.30%) |  |
| `cold_storage_sensors` | 8 | 0 (0.00%) | 0 (0.00%) |  |
| `customer_complaints` | 2,667 | 0 (0.00%) | 0 (0.00%) | lot_code_reported 7.5%, customer_id 3.4%, closed_at 1.0% |
| `customer_hierarchy` | 1,108 | 0 (0.00%) | 0 (0.00%) | valid_to 73.8% |
| `customers` | 2,223 | 0 (0.00%) | 0 (0.00%) | closed_on 85.3%, tax_id 43.6%, created_on 21.5%, province 2.1% |
| `cycle_counts` | 11,976 | 0 (0.00%) | 0 (0.00%) | reason_code 8.4% |
| `downtime_events` | 86,969 | 0 (0.00%) | 441 (0.51%) | comment 70.1% |
| `employees` | 1,587 | 0 (0.00%) | 0 (0.00%) | termination_date 42.5%, line_id 18.5%, hire_date 12.0%, crew 5.8% |
| `energy_meter_readings` | 100,945 | 0 (0.00%) | 0 (0.00%) |  |
| `equipment` | 105 | 0 (0.00%) | 0 (0.00%) | decommission_date 88.6%, replaced_by_asset_id 88.6%, install_date 34.3%, line_id 7.6% |
| `fx_rates` | 1,191 | 0 (0.00%) | 0 (0.00%) |  |
| `goods_receipts` | 17,244 | 0 (0.00%) | 0 (0.00%) |  |
| `inventory_snapshots` | 322,234 | 0 (0.00%) | 0 (0.00%) | item_code 0.5% |
| `maintenance_work_orders` | 23,066 | 0 (0.00%) | 0 (0.00%) | completed_at 0.1% |
| `mes_oee_daily` | 23,375 | 0 (0.00%) | 0 (0.00%) |  |
| `p3_articulos` | 38 | 0 (0.00%) | 0 (0.00%) | cod_erp 71.1%, peso_neto 13.2% |
| `p3_clientes` | 400 | 0 (0.00%) | 0 (0.00%) |  |
| `p3_produccion_diaria` | 9,447 | 0 (0.00%) | 0 (0.00%) | observaciones 83.5% |
| `p3_ventas` | 35,045 | 0 (0.00%) | 0 (0.00%) |  |
| `plants` | 3 | 0 (0.00%) | 0 (0.00%) | acquired_on 66.7% |
| `price_lists` | 57,218 | 0 (0.00%) | 0 (0.00%) | effective_to 40.3% |
| `product_uom_conversions` | 459 | 0 (0.00%) | 0 (0.00%) | valid_to 98.7% |
| `production_batches` | 90,861 | 0 (0.00%) | 326 (0.36%) | deleted_at 99.6%, item_code 1.0% |
| `production_lines` | 13 | 0 (0.00%) | 0 (0.00%) | legacy_code 76.9%, commissioned_on 69.2% |
| `production_orders` | 71,078 | 0 (0.00%) | 0 (0.00%) | notes 100.0%, recipe_id 10.9%, sku_code 0.7% |
| `products` | 397 | 0 (0.00%) | 0 (0.00%) | updated_at 98.2%, discontinued_date 74.8%, allergens 35.3%, launch_date 34.0% |
| `purchase_order_lines` | 20,480 | 0 (0.00%) | 0 (0.00%) | item_text 85.6%, material_code 14.4% |
| `purchase_orders` | 18,187 | 0 (0.00%) | 0 (0.00%) |  |
| `quality_tests` | 244,040 | 0 (0.00%) | 0 (0.00%) | receipt_id 89.2%, material_code 89.2%, result_json 84.8%, spec_min 74.2% |
| `raw_materials` | 84 | 0 (0.00%) | 0 (0.00%) | contiene_huevo 98.8%, contiene_sesamo 98.8%, contiene_mani 98.8%, contiene_mostaza 98.8% |
| `recalls` | 52 | 0 (0.00%) | 0 (0.00%) | notes 94.2%, customers_notified 5.8% |
| `recipe_lines` | 1,498 | 0 (0.00%) | 0 (0.00%) |  |
| `recipes` | 225 | 0 (0.00%) | 0 (0.00%) | notes 99.1%, effective_to 53.8% |
| `returns` | 4,789 | 0 (0.00%) | 0 (0.00%) | lot_code 84.2%, order_number 40.2%, sku_code 0.2% |
| `sales_order_lines` | 633,257 | 0 (0.00%) | 7,200 (1.14%) | invoice_date 2.1%, sku_code 0.3% |
| `sales_orders` | 168,431 | 0 (0.00%) | 1,607 (0.95%) | sales_rep_id 2.7% |
| `shipments` | 687,150 | 0 (0.00%) | 0 (0.00%) | lot_code 4.0% |
| `sku_mapping_p3` | 26 | 0 (0.00%) | 0 (0.00%) | notes 69.2% |
| `storage_locations` | 17 | 0 (0.00%) | 0 (0.00%) | unit_id 52.9% |
| `suppliers` | 180 | 0 (0.00%) | 0 (0.00%) | is_deleted 34.4%, province 2.2%, city 2.2%, tax_id 1.7% |
| `timeclock_punches` | 661,708 | 256 (0.04%) | 256 (0.04%) | horas_extra 97.0%, autorizado_por 96.7%, reloj 0.4% |
| `waste_log` | 90,813 | 0 (0.00%) | 0 (0.00%) | uom 41.7%, item 1.0%, line_id 0.8%, lot 0.1% |

44 tables, 7,571,393 rows. Every column's rate is in [data_profile.json](data_profile.json).

## Real names

Every text value of every table scanned for 84 real Argentine and international food brands, manufacturers and retailers: **no matches**.
Tax ids are CUIT-shaped with a deliberately wrong check digit, so none can be a real one; people's names are random first/last name pairs; no addresses beyond city and province.
