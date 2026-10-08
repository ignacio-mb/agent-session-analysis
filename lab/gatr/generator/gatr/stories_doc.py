"""Writes answer_key/hidden_stories.md from the facts each story was planted with, so the ids and dates in it are the
data's."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from . import catalog as C
from .ops import OIL_SHOCK, OVERFILL, POTATO_SHORTAGE


class _FromAnswerKey:
    """The world as the answer key keeps it: the story facts and the true-world tables, for re-rendering the document
    without regenerating (python3 -m gatr.stories_doc)."""

    NAMES = {"employees": "employees", "suppliers": "suppliers", "customers": "customers", "recipes": "recipes",
             "batches": "production_batches"}

    def __init__(self, key: Path):
        import json
        self.key = key
        self.story = json.loads((key / "story_facts.json").read_text(encoding="utf-8"))

    def __getitem__(self, name):
        return pd.read_csv(self.key / "true_world" / f"{self.NAMES[name]}.csv.gz", low_memory=False)


def render(w, path: Path):
    s = w.story
    emp = w["employees"].set_index("employee_id")
    sup = w["suppliers"].set_index("supplier_id")
    cust = w["customers"].set_index("customer_id")
    rec = w["recipes"]
    ses, own, fr, ot = s["sesame"], s["ownership_change"], s["receiving_fraud"], s["overtime"]
    st, lag, con, frz = s["channel_stuffing"], s["price_lag"], s["consolidation"], s["freezer"]
    cut = rec[rec["cost_cut"]].iloc[0]
    may = rec[rec["formula_code"] == "MAY-CLA"].sort_values("version")

    def who(eid):
        e = emp.loc[eid]
        return f"{e['full_name']} (employee_id {eid}, legajo {e['legajo']}, {e['role'].lower()})"

    def c6(i):
        return f"{int(i):06d}"

    p1 = next(x for x in ses["receipts"] if x["plant_id"] == "P1")
    p2 = next(x for x in ses["receipts"] if x["plant_id"] == "P2")
    p1_batches = set(w["batches"][w["batches"]["plant_id"] == "P1"]["batch_id"])
    p1n = sum(1 for b in ses["batches"] if b in p1_batches)
    reuse = s["sku_reuse"]
    out = f"""# Hidden stories

What was planted in GATR's data, where it lives, and the analysis that finds it. Nothing here is stated anywhere in
the recorded tables or the data dictionary. Each story has a detection query in [`queries/`](queries/) that finds it
from the recorded tables alone; [verification.md](verification.md) shows what each one returns, and what the naive
query an analyst would try first returns instead. Corruption rule ids (`L01`, `Z02`, …) are in
[corruptions.md](corruptions.md). Ids below are as the recorded tables hold them.

## 1. Sesame in the smoked paprika: the recall was too small

**What happened.** Supplier {ses['supplier_id']} ({sup.at[ses['supplier_id'], 'supplier_name']}) delivered a lot of
smoked paprika (`RM-PIMA`) cross-contaminated with sesame, supplier lot **`{ses['supplier_lot']}`**. It went to two
plants: P1 received it on {p1['receipt_date']} as internal lot `{p1['internal_lot']}` (receipt {p1['receipt_id']}),
P2 on {p2['receipt_date']} as `{p2['internal_lot']}` (receipt {p2['receipt_id']}). Paprika is in chips, barbecue
sauce, chimichurri, empanadas and several frozen meals, none of which declare sesame. {len(ses['batches'])} batches
consumed it ({p1n} at P1, {len(ses['batches']) - p1n} at P2); {len(ses['true_lots'])} of their lots shipped,
{ses['true_units']:,} units to **{len(ses['true_customers'])} customers**. Consumers with sesame allergies
complained in October; GATR recalled on {ses['recall_day']}.

**The official recall** (`recalls`, `RET-2023-01`) covers **{len(ses['official_lots'])} lots** reaching
{len(ses['official_customers'])} customers: P1's quality team searched receipts for the lot number as printed and
traced P1's internal lot through `batch_consumption`. It missed:
- every P2 lot: P2's scanners strip separators, so its receipt reads `MEC230457` (rule L01);
- P1 batches that consumed the lot but backflushed paprika without scanning it (`batch_consumption.lot` NULL, rule
  L02);
- customers whose shipment rows have no lot (rule L03).

**Path.** `goods_receipts.supplier_lot` normalised (strip non-alphanumerics, upper case) → both internal lots →
`batch_consumption.lot` → `production_batches` (plus P1 batches with a NULL paprika lot in the same window) →
`lot_code` → `shipments` (plus lot-less shipments of the same SKU, warehouse and dates) → customers; set against
`recalls`. Complaints (`customer_complaints`, category ALERGENOS) carry malformed lot codes (rule L04: spaces, lower
case, missing characters, a `LOTE` prefix) that normalise back to affected lots, including P2's, after the recall
date. Query: [`s01_sesame_blast_radius.sql`](queries/s01_sesame_blast_radius.sql).

## 2. The spice supplier that changed owners

**What happened.** Supplier {own['old_id']} ({sup.at[own['old_id'], 'supplier_name']}), GATR's best spice supplier,
was sold. From {own['change']} it invoices as a new supplier record, **{own['new_id']}**
({sup.at[own['new_id'], 'supplier_name']}), same tax id `{own['tax_id']}` but keyed without dashes. Under the new owner
the microbial load of its dried spices creeps up month after month (mean log10 UFC/g from ~1.75 to ~2.4, nearly five
times the count), and incoming microbiology failures (over 1,000 UFC/g), which never happened before, start to
appear in its last year.

**Why it hides.** By `supplier_id`, the old record is the best spice supplier and the new one is unremarkable (its failure rate over its
whole life is low, and several other spice suppliers have higher counts every month). The
deterioration is only visible as a trend of one company across two ids.

**Path.** `suppliers.tax_id` normalised to digits groups the two ids (and P2's duplicate supplier records, rule D01)
→ `goods_receipts` → `quality_tests` (`sample_type = 'MP'`, `MICROBIOLOGIA`; P2's are JSON panels, rule J01) → mean
log count and failure rate by quarter. Query: [`s02_supplier_ownership.sql`](queries/s02_supplier_ownership.sql).

## 3. The cost-cut mayonnaise recipe

**What happened.** Recipe {cut['recipe_id']} (formula `F-MAY-CLA`, version {cut['version']}, "Optimización de
costos"), in force {s['recipe_costcut']['start']} to {s['recipe_costcut']['end']}, cut egg yolk, vinegar and
sorbate. Lower acidity and less preservative: pH up ~0.1, yeast/mould and seal (vacuum) failures at release two to
three times as frequent, and jars and sachets that swell in the market: complaints ("frasco inflado", "tapa
abombada", "olor ácido") 4 to 8 weeks after shipping, on about three times as many lots as other mayonnaise. Version
{int(may.iloc[-1]['version'])} (from {may.iloc[-1]['valid_from']}) restores the old formula. It affects every
Mayonesa Clásica SKU (jars at L1-03 and SUR-02, sachets and doypacks at L1-04).

**Why it hides.** Complaints arrive weeks after production, in a period when complaint counts grow anyway (P3's
complaints join the CRM in May 2025, a promotion in October 2024 lifts volume); release-test failures rise only for
one formula among many. Recipe versions overlap and have gaps (rule V01), and P2 often leaves `recipe_id` empty on its
production orders.

**Path.** `production_batches` → `production_orders.recipe_id` (or the version in force on the batch date) →
`recipes.version`; release tests per batch (`quality_tests`, JSON at P2); complaints matched to lots after normalising
`lot_code_reported`. Query: [`s03_costcut_recipe.sql`](queries/s03_costcut_recipe.sql).

## 4. The line that doesn't log its downtime

**What happened.** On **L1-04** (P1's sachet and doypack line) operators log about a quarter of their long stops,
almost none of the short ones, and shorten what they do log. The line really is down ~38% of its running time
(true availability {C.LINE_PERF['L1-04'][0]:.0%}, the worst in the company); the maintenance system has every breakdown.

**Why it hides.** The MES's OEE (`mes_oee_daily`) is availability from logged downtime × the PLC's running speed ×
quality, so L1-04 reports the **best OEE in the company** (~86%). Rebuilt from output (good quantity / the order's
standard rate, over the staffed shift time) it is among the worst; the CMMS's corrective work orders on its assets add
up to about four times the downtime its operators logged, against ~0.7× on every other line.

**Path.** `mes_oee_daily` against `production_batches.good_qty` / `production_orders.std_rate_per_hour`;
`maintenance_work_orders` (epoch ms, rule F06) → `equipment.line_id` against `downtime_events`; `timeclock_punches`
shows the crews were there. Query: [`s04_hidden_downtime.sql`](queries/s04_hidden_downtime.sql).

## 5. Receiving fraud on sunflower oil

**What happened.** From {fr['start']}, when {who(fr['clerk_id'])} receives sunflower oil (`RM-ACGI`) at P1 from
supplier {fr['supplier_id']} ({sup.at[fr['supplier_id'], 'supplier_name']}), the receipt records 3-6% more than the
tanker delivered; GATR pays for oil it never got. Other clerks receiving the same supplier's oil, and the same clerk
receiving from the other oil suppliers, record what arrived.

**Why it hides.** Every receipt is within the normal ±5-8% of its purchase order: no single record looks wrong.
Production consumes what is physically there, so the book stock of those lots stays ahead of the tank; the monthly
cycle counts (`cycle_counts`) write the difference off as "MERMA"/"DIF. INVENTARIO", and the weekly snapshots show the
oil's book stock dropping at every count.

**Path.** Write-offs (`cycle_counts.difference`) normalised by each plant's use of each material
(`batch_consumption`) single out P1's oil (over 1% of use, against ±0.15% for every other material at every plant);
attributed to the lots counted (`cycle_counts.lot` = `goods_receipts.internal_lot`) and grouped by `received_by` ×
`supplier_id`, the write-offs belong to one pair (2-3% of what that clerk received from that supplier, zero for
every other pair). Query: [`s05_receiving_fraud.sql`](queries/s05_receiving_fraud.sql).

## 6. Freezer FRZ-N2's night-time excursions

**What happened.** On {frz['wo_cause_day']} maintenance replaced FRZ-N2's defrost controller (work order "Reemplazo de
controlador de deshielo cámara N°2"). The new one ran the 02:00 defrost for 90 minutes instead of 20: on
{len(frz['nights'])} nights until the fix on {frz['wo_fix_day']} ("Ajuste de parámetros de ciclo de deshielo") the
chamber warmed from -20 °C to -9..-5 °C between 02:00 and 04:30. Frozen lots stored there meanwhile (P1's frozen
lots go to FRZ-N1 or FRZ-N2 at random) draw texture and thaw complaints ("cristales de hielo", "se nota que se
descongeló") at three to four times the rate of lots in FRZ-N1.

**Why it hides.** P1's and P2's sensor gateways report in the IoT account's default time zone, America/New_York, with
US daylight saving (rule Z02): read as local time, the excursions sit around 00:00-02:00 and move by an hour at each US
DST change. Warm readings during the day (door openings, the 10:00 and 18:00 defrosts) are normal for every freezer.

**Path.** `cold_storage_sensor_readings` converted New York → Buenos Aires; excursions above `alarm_high_c` by local
hour and month; `maintenance_work_orders` for the unit's asset; `inventory_snapshots` (location FRZ-N2) for the lots
in the chamber during the period; complaints by normalised lot. Query:
[`s06_freezer_excursions.sql`](queries/s06_freezer_excursions.sql).

## 7. P3's units

**What happened.** Planta Delta's legacy system (`p3_produccion_diaria`) reports sauces and bread in **kilograms**
(spelled KG, Kg, kgs) and frozen food in **pieces** (an empanada pack of 12 counts 12), with decimal commas and dot
thousands separators. P2 reports in cases, P1 and P3-after-migration in units (consumer packs). Lines 1 and 2 moved to
the ERP on 2025-05-05 but kept reporting in the legacy system for a week (rule D08); line 3 (bread) never moved. The
legacy export starts in July 2023, eight months before GATR owned the plant (rule H02).

**Why it hides.** A union of "quantity produced" adds kilograms, pieces, units and cases: on frozen food P3 looks over a
hundred times as productive as P2 (pieces against cases) when it makes less than twice as much; on sauces P2 looks like a
fraction of P1 and P3 when it makes the most.

**Path.** Convert: cases × `products.units_per_case` (or `product_uom_conversions`, which is missing one change, rule
U01), kilograms / `p3_articulos.peso_neto`, pieces / pieces per pack (from the description); map P3 codes through
`sku_mapping_p3` (partial and partly wrong, rule K04); drop pre-acquisition months and the overlap week. Query:
[`s07_p3_units.sql`](queries/s07_p3_units.sql).

## 8. The inflation illusion

**What happened.** Domestic prices follow inflation (monthly price lists through 2024, every other month after): about
95% in 2022, over 200% in 2023, over 100% in 2024. Revenue in pesos multiplies by more than ten from 2022 to 2025
while real volume is flat for distributors and wholesalers, grows modestly for supermarkets (new stores, private
labels) and **falls by a quarter for food service** (restaurants closing in the 2024 recession), the segment whose peso
revenue grows like everyone else's. In dollars every segment looks better than it is too: prices rose faster than the
peso fell.

**Why it hides.** Peso revenue is the default measure. Converting to USD needs `fx_rates`, which has no weekends or
holidays (rule X01), four fat-fingered rates (x10, /10, digits reversed: rule X02) and duplicated dates (X03); export
lines sometimes say ARS though the amount is USD (X04).

**Path.** Clean the rates (one per day, drop outliers against a rolling median, carry forward), convert, and compare
with volume in consumer units (cases × units per case, by the pack size in force). Query:
[`s08_inflation_illusion.sql`](queries/s08_inflation_illusion.sql).

## 9. Simpson's paradox in yield

**What happened.** Yield (good output / planned, in kilograms) depends mostly on the family: sauces ~98%, frozen ~93%,
crackers ~90%, bread ~88%, chips ~86%. P2 is newer and about 1.1 points worse than P1 on every product, but most of
what it makes is sauce.

**Why it hides.** Overall, **P2's yield is the best**; product by product, **P1 beats P2 on every SKU both plants
make**. P2 records quantities in cases (the ratio is unaffected); impossible rows (rule I01: yields of ~1000%, swapped
times, negative rejects) distort averages if left in.

**Path.** Yield by plant × SKU (or family), clean rows only. Query: [`s09_simpson_yield.sql`](queries/s09_simpson_yield.sql).

## 10. A chain buys a chain

**What happened.** {cust.at[con['acquirer'], 'customer_name']} (customer {c6(con['acquirer'])}) bought
{cust.at[con['acquired'], 'customer_name']} ({c6(con['acquired'])}) on {con['acquired_on']}. Costa Azul's
{len(con['successor'])} stores kept ordering under their old customer ids until {con['switch']}, when they were
re-created as La Estrella stores (new ids, names ending "(ex Costa Azul)") and the old ids closed. On the acquisition
date La Estrella's own stores were moved under four new regional distribution-centre customers
({', '.join(c6(x) for x in con['dcs'])}), and Costa Azul's head office under La Estrella (relation GRUPO).

**Why it hides.** `customer_hierarchy` changes meaning (rule H01): the parent was the chain; for La Estrella it is now
a distribution centre. Ranked by the current parent, La Estrella is split into four mid-sized "customers"; ranked by
customer id, it vanishes into stores; naive churn counts Costa Azul's stores as lost in September 2025 and new ones
appearing. Walking the hierarchy to the top, as of each order date, La Estrella is the largest customer.

**Path.** `customer_hierarchy` with `valid_from`/`valid_to`, recursively to the ultimate parent; successor stores
matched by name. Query: [`s10_customer_consolidation.sql`](queries/s10_customer_consolidation.sql).

## 11. A reused SKU code

**What happened.** Code `{reuse[0]['sku_code']}` was Salsa Criolla 250 g until 2023-06-30; from 2024-09-02 it is
Mayonesa con Oliva 500 g. Code `{reuse[1]['sku_code']}` was a 900 g bag of Papas Onduladas until early 2023; from
2025-02-03 it is Crackers Semillas 300 g.

**Why it hides.** `products` has two rows for each code (rule M03). Joining sales on `sku_code` alone multiplies the
lines and gives a mayonnaise the criolla's history; family-level reports move volume from sauces to snacks.

**Path.** Join on the code and the date inside `launch_date`/`discontinued_date`. Query:
[`s11_sku_reuse.sql`](queries/s11_sku_reuse.sql).

## 12. Overtime nobody needed

**What happened.** From {ot['start']} to {ot['end']}, supervisor {who(ot['supervisor_id'])} of P2's frozen line
({ot['line']}, crew {ot['crew']}) approved inflated overtime for six of their operators:
{', '.join(f"{emp.at[m, 'full_name']} ({emp.at[m, 'legajo']})" for m in ot['members'])}. On about 40% of their shifts
they clock out 2-4 hours late, and roughly one day off a week they come in for 8 hours while the rest of their crew is
off; the supervisor approves every hour. Nothing more is produced: the line runs the same schedule with the crews on
shift.

**Why it hides.** Overtime totals by approver or by employee are dominated by P1's Sunday shifts and maintenance call
outs; the six are not at the top. The time clock is keyed by legajo (rule K01), punches have to be paired (rule G02),
and P2 runs a 4x4 rotation (two days, two nights, four off) that isn't written down anywhere.

**Path.** Pair punches into shifts; attach employees by legajo (and their crew and supervisor); infer the rotation
from when each crew works; overtime per head per crew, shifts worked without the crew, against `production_batches`
on the line. Query: [`s12_overtime.sql`](queries/s12_overtime.sql).

## 13. The food-service price list keyed with the wrong month

**What happened.** Price list {lag['price_list_id']} for FOOD SERVICE, meant to take effect on {lag['intended']}, was
uploaded on 2023-07-27 with `effective_from` = {lag['recorded']} (Excel serial 45231 instead of 45139). Because a
"future" list existed, the September and October food-service lists were never created. Food service was billed July
prices for three months of 12% monthly inflation, then jumped back in November. The audit log has the upload, and a
clean-up of the list's `effective_to` in November that fixed nothing that had been billed.

**Path.** `price_lists` (Excel serial dates, rule F02; decimal commas in some lists, F03): a basket's price index by
segment and month, and each list's `created_at` against its effective date; `audit_log`. Query:
[`s13_price_list_lag.sql`](queries/s13_price_list_lag.sql).

## 14. Quarter-end channel stuffing

**What happened.** From {st['start']} to {st['end']} sales rep {who(st['rep_id'])} booked an extra order into each of
five distributors ({', '.join(c6(x) for x in st['customers'])}) in the last week of every quarter, about three times
a normal order. Three to seven weeks later most of it came back as credit notes for overstock, slow rotation or near
expiry.

**Why it hides.** Credit notes are negative lines on NC-type orders before the July 2024 upgrade and rows in `returns`
after it, with April-June 2024 in both (rule R01); returns don't always reference the original order. Sales by rep
look like a good rep.

**Path.** Share of each rep's sales in a quarter's last week; overstock credit notes (from both places, without double
counting) against sales, by rep. Query: [`s14_channel_stuffing.sql`](queries/s14_channel_stuffing.sql).

## 15. The overfilling checkweigher

**What happened.** On {OVERFILL[0]} P2's chips line (SUR-01) got a new checkweigher (work order "Instalación
controladora de peso nueva"; `equipment` shows the replacement) with a wrong tare: until a quality audit fixed it on
{OVERFILL[1]} every bag carried about {OVERFILL[2]:.1%} more chips than declared. Yield on the line dropped by the same
amount; no test failed, because overweight is inside the spec.

**Path.** P2's net-weight results are inside JSON panels (rule J01; panels before April 2023 contain `NaN`, rule J03);
net weight / declared weight by month for SUR-01, against the equipment replacement and the March work order. Query:
[`s15_checkweigher_overfill.sql`](queries/s15_checkweigher_overfill.sql).

## Red herrings

Patterns that look alarming and have an ordinary explanation in the data.

- **Planned shutdowns.** Production, energy and time-clock punches drop to almost nothing at P1 from 2023-01-09 to
  2023-01-22 and 2025-01-06 to 2025-01-12, at P2 from 2024-01-15 to 2024-01-26, at P3 2025-01-13 to 2025-01-19: the
  annual maintenance shutdowns (preventive work orders pile up in them).
- **The CH-S1 step.** P2's chiller sensor reads 1.2 °C lower from 2024-05-20: work order "Calibración de sensor de
  temperatura (desvío +1,2 °C)". The sensor had read high; the chamber never changed.
- **October 2024.** Sauces, empanadas and chips spike, and so do complaints about them: the "Promo Aniversario" SKUs.
  Complaints per unit shipped don't move.
- **Complaints jump in May 2025.** Planta Delta's complaints start arriving through the CRM at the go-live; before
  they lived in the legacy system, which isn't exported.
- **Waste spike at L1-03 in March 2024.** Reason "PRUEBA ENVASE NUEVO": a planned trial of a new jar.
- **Summer energy.** P1's and P2's consumption peaks in January-February every year: refrigeration.
- **Supply shocks.** Chips production is cut and fill rates drop from {POTATO_SHORTAGE[0]} to {POTATO_SHORTAGE[1]}
  (a drought: potato receipts are scarce and dear); mayonnaise output is capped from {OIL_SHOCK[0]} to {OIL_SHOCK[1]}
  and sunflower oil costs ~45% more from March to August 2022.
- **P2's first months.** P2's yields, OEE and output ramp up from July 2022: a new plant learning, not a problem.
- **Lots missing on P2's shipments until mid-2023** and **downtime logged months late at P2 in early 2024**: data
  capture issues (rules L03, A01), not events.
"""
    path.write_text(out, encoding="utf-8")


if __name__ == "__main__":
    key = Path(__file__).resolve().parents[2] / "answer_key"
    render(_FromAnswerKey(key), key / "hidden_stories.md")
