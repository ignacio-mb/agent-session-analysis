"""Checks the GATR dataset from the outside, the way an analyst would meet it: loads lab/db/gatr.sql into a Postgres,
then

  1. runs each story's detection query (answer_key/queries/sNN_*.sql) on the recorded tables alone, and checks that
     it finds what was planted;
  2. runs the trivial single-table GROUP BY an analyst would try first for each story, and checks it does NOT give
     the story away;
  3. computes ~30 key metrics from the true world (answer_key/true_world/) and the value a naive query on the
     recorded tables gives (answer_key/ground_truth_metrics.csv);
  4. profiles the mess (row counts, null-like rates per column, duplicate rows) and scans every text value for real
     brand and company names;

and writes answer_key/verification.md.

    python3 verify.py                       # a throwaway Postgres in Docker (postgres:16-alpine)
    GATR_PG_DSN=postgresql://... python3 verify.py   # an existing Postgres; the gatr schema is replaced

Needs psycopg 3, numpy, pandas (make gatr-verify runs it with them)."""

from __future__ import annotations

import gzip
import json
import os
import re
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import psycopg

HERE = Path(__file__).resolve().parent
LAB = HERE.parents[1]
DATA = LAB / "db" / "gatr"
SQL = LAB / "db" / "gatr.sql"
KEY = LAB / "gatr" / "answer_key"
TRUE = KEY / "true_world"
QUERIES = KEY / "queries"

# ---- Postgres ----------------------------------------------------------------------------------------------------------


def start_docker():
    name = f"gatr-verify-{os.getpid()}"
    subprocess.run(["docker", "run", "-d", "--rm", "--name", name, "-e", "POSTGRES_PASSWORD=verify", "-p",
                    "127.0.0.1::5432", "postgres:16-alpine", "-c", "fsync=off", "-c", "max_wal_size=4GB"],
                   check=True, capture_output=True)
    port = subprocess.run(["docker", "port", name, "5432"], check=True, capture_output=True,
                          text=True).stdout.strip().split(":")[-1]
    dsn = f"postgresql://postgres:verify@127.0.0.1:{port}/postgres"
    for _ in range(60):
        try:
            psycopg.connect(dsn).close()
            break
        except psycopg.OperationalError:
            time.sleep(1)
    return name, dsn


def load(conn):
    """Runs gatr.sql, streaming each table's CSV from the client instead of the server's /seed directory."""
    conn.execute("DROP SCHEMA IF EXISTS gatr CASCADE")
    text = SQL.read_text(encoding="utf-8")
    stmts = [s.strip() for s in re.split(r";\s*\n", text) if s.strip()]
    copy = re.compile(r"COPY (\w+) FROM PROGRAM 'gzip -dc /seed/gatr/([\w.]+)' \(FORMAT csv, HEADER true\)")
    with conn.cursor() as cur:
        for s in stmts:
            s = "\n".join(line for line in s.splitlines() if not line.startswith("\\") and not line.startswith("--"))
            if not s.strip():
                continue
            m = copy.fullmatch(s.strip())
            if m:
                with cur.copy(f"COPY {m.group(1)} FROM STDIN (FORMAT csv, HEADER true)") as cp:
                    with gzip.open(DATA / m.group(2), "rb") as f:
                        while chunk := f.read(1 << 20):
                            cp.write(chunk)
            else:
                cur.execute(s)
    conn.execute("ANALYZE")
    conn.commit()


def q(conn, sql, params=None):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        cols = [d.name for d in cur.description]
        return pd.DataFrame(cur.fetchall(), columns=cols)


def run_file(conn, path):
    sql = path.read_text(encoding="utf-8")
    return q(conn, sql)


# ---- the true world ----------------------------------------------------------------------------------------------------

def T(name, **kw):
    return pd.read_csv(TRUE / f"{name}.csv.gz", keep_default_na=True, low_memory=False, **kw)


def story_facts():
    return json.loads((KEY / "story_facts.json").read_text(encoding="utf-8"))


# ---- 1 & 2: stories ----------------------------------------------------------------------------------------------------

def check_stories(conn, facts):
    out = []

    def add(sid, title, query, ok, finding, naive_sql, naive_ok, naive_finding):
        out.append(dict(id=sid, title=title, query=query, ok=bool(ok), finding=finding, naive_sql=naive_sql,
                        naive_ok=bool(naive_ok), naive_finding=naive_finding))

    ses = facts["sesame"]
    r = run_file(conn, QUERIES / "s01_sesame_blast_radius.sql").iloc[0]
    naive = "SELECT count(DISTINCT lot_code), sum(customers_notified) FROM gatr.recalls WHERE recall_id = 'RET-2023-01'"
    n = q(conn, naive).iloc[0]
    add("S01", "Sesame recall: the real blast radius", "s01_sesame_blast_radius.sql",
        abs(r.affected_lots - len(ses["true_lots"])) <= 0.05 * len(ses["true_lots"]) and r.lots_missed_by_recall > 0
        and r.affected_lots_p2 > 0 and
        r.customers_incl_missing_lots > r.official_customers,
        f"{r.affected_lots} affected lots ({r.affected_lots_p2} at P2), {r.lots_missed_by_recall} missed by the recall; "
        f"{r.customers_incl_missing_lots} customers reached vs {r.official_customers} through the recalled lots "
        f"(true: {len(ses['true_lots'])} lots, {len(ses['true_customers'])} customers)",
        naive, int(n.iloc[0]) < len(ses["true_lots"]),
        f"the recall table lists {int(n.iloc[0])} lots: it is the official scope, not the blast radius")

    own = facts["ownership_change"]
    r = run_file(conn, QUERIES / "s02_supplier_ownership.sql")
    pre = r[r["quarter"] < date(2024, 7, 1)]
    post = r[r["quarter"] >= date(2025, 10, 1)]
    naive = """SELECT g.supplier_id, count(*) AS lots, round(100.0 * avg((NOT q.passed)::int), 1) AS fail_pct
               FROM gatr.quality_tests q JOIN gatr.goods_receipts g USING (receipt_id)
               WHERE q.sample_type = 'MP' AND q.test_name = 'MICROBIOLOGIA' AND q.passed IS NOT NULL
               GROUP BY 1 HAVING count(*) >= 30 ORDER BY 3 DESC"""
    n = q(conn, naive)
    rank = int(n.index[n["supplier_id"] == own["new_id"]][0]) + 1 if (n["supplier_id"] == own["new_id"]).any() else 0
    add("S02", "Supplier ownership change: quality slides under a new id", "s02_supplier_ownership.sql",
        pre["mean_log_ufc"].astype(float).mean() + 0.3 < post["mean_log_ufc"].astype(float).mean(),
        f"same tax id, mean log10 UFC/g {pre['mean_log_ufc'].astype(float).mean():.2f} before the change, "
        f"{post['mean_log_ufc'].astype(float).mean():.2f} in the last year; failures "
        f"{pre['fail_pct'].astype(float).mean():.1f}% -> {post['fail_pct'].astype(float).mean():.1f}%",
        "incoming micro failure rate by supplier_id (P1/P3 rows)", rank != 1,
        f"by supplier_id the new id ranks {rank} of {len(n)} on failure rate: not the outlier")

    r = run_file(conn, QUERIES / "s03_costcut_recipe.sql")
    cut = r[r["notes"].str.contains("Optimiz", na=False)].iloc[0]
    rest = r[~r["notes"].str.contains("Optimiz", na=False)]
    naive = """SELECT date_trunc('quarter', tested_at)::date AS q, test_name, round(100.0 * avg((NOT passed)::int), 2) AS
               fail_pct FROM gatr.quality_tests WHERE test_name IN ('LEVADURAS Y HONGOS', 'VACIO / SELLADO') GROUP BY 1, 2
               ORDER BY 1, 2"""
    n = q(conn, naive)
    yq = n[n["test_name"] == "LEVADURAS Y HONGOS"].set_index("q")["fail_pct"].astype(float)
    add("S03", "Cost-cut mayonnaise recipe", "s03_costcut_recipe.sql",
        float(cut.test_fail_pct) > 2 * rest["test_fail_pct"].astype(float).max() and
        float(cut.pct_lots_complained) > 2 * rest["pct_lots_complained"].astype(float).max(),
        f"version {cut.version}: {cut.test_fail_pct}% of batches fail release tests and {cut.pct_lots_complained}% of "
        f"lots draw complaints, vs at most {rest['test_fail_pct'].astype(float).max()}% / "
        f"{rest['pct_lots_complained'].astype(float).max()}% for the other versions",
        "yeast/mould failure rate by quarter, all products", yq.max() < 3 * yq.median(),
        f"quarterly yeast/mould failure across products ranges {yq.min():.2f}-{yq.max():.2f}% (median "
        f"{yq.median():.2f}%): a bump, not a story")

    r = run_file(conn, QUERIES / "s04_hidden_downtime.sql").set_index("line_id")
    naive = "SELECT line_id, round(avg(oee_pct), 1) AS oee FROM gatr.mes_oee_daily GROUP BY 1 ORDER BY 2 DESC"
    n = q(conn, naive)
    l4 = r.loc["L1-04"]
    add("S04", "Hidden downtime on L1-04", "s04_hidden_downtime.sql",
        int(l4.reported_rank) == 1 and int(l4.rank_from_output) >= len(r) - 3 and
        float(l4.cmms_vs_logged) > 2 * r.drop("L1-04")["cmms_vs_logged"].astype(float).max(),
        f"reported OEE {l4.reported_oee}% (rank {l4.reported_rank}); from output {l4.oee_from_output}% (rank "
        f"{l4.rank_from_output} of {len(r)}); maintenance logs {l4.cmms_vs_logged}x the downtime operators log (others "
        f"<= {r.drop('L1-04')['cmms_vs_logged'].astype(float).max()}x)",
        naive, n.iloc[0]["line_id"] == "L1-04",
        f"the MES report ranks L1-04 first ({n.iloc[0]['oee']}%): the naive answer is the opposite of the truth")

    fr = facts["receiving_fraud"]
    r = run_file(conn, QUERIES / "s05_receiving_fraud.sql")
    mat = r[r["view"] == "material"].sort_values("pct")
    who = r[r["view"] != "material"].sort_values("pct")
    naive = """SELECT received_by, count(*) AS receipts, round(avg(qty_received)) AS avg_qty FROM gatr.goods_receipts
               WHERE plant_id = 'P1' GROUP BY 1 ORDER BY 2 DESC"""
    add("S05", "Receiving fraud: one clerk, one supplier, sunflower oil at P1", "s05_receiving_fraud.sql",
        mat.iloc[0]["who"] == "P1 RM-ACGI" and who.iloc[0]["who"] == f"{fr['clerk_id']} x {fr['supplier_id']}" and
        float(who.iloc[0]["pct"]) < 2 * float(who.iloc[1]["pct"]) - 1,
        f"P1 oil write-offs {mat.iloc[0]['pct']}% of use (next worst {mat.iloc[1]['pct']}%); on lots received by "
        f"{who.iloc[0]['who']}: {who.iloc[0]['pct']}% of what was received, vs {who.iloc[1]['pct']}% for the next "
        "clerk x supplier",
        naive, True, "receipts per clerk look ordinary: the clerk receives the most because they work the day shift")

    r = run_file(conn, QUERIES / "s06_freezer_excursions.sql")
    exc = r[r["what"] == "excursions"]
    n2 = exc[exc["key"].str.startswith("TS-FRZ-N2 2025-0")]
    lots = r[r["what"] == "lots and complaints"].set_index("key")
    naive = """SELECT extract(hour FROM reading_time) AS hour, count(*) FROM gatr.cold_storage_sensor_readings
               WHERE sensor_id = 'TS-FRZ-N2' AND value > -12 GROUP BY 1 ORDER BY 2 DESC"""
    n = q(conn, naive)
    rate = {k: lots.loc[k, "m"] / max(lots.loc[k, "n"], 1) for k in lots.index}
    add("S06", "Freezer FRZ-N2's night-time excursions", "s06_freezer_excursions.sql",
        n2["n"].sum() > 500 and (n2["m"].sum() / n2["n"].sum()) > 0.9 and rate["FRZ-N2"] > 2 * rate["FRZ-N1"],
        f"{int(n2['n'].sum())} readings over the alarm in FRZ-N2 Jan-Aug 2025, {100 * n2['m'].sum() / n2['n'].sum():.0f}% "
        f"between 02:00 and 04:59 local; complaints per lot stored there {rate['FRZ-N2']:.3f} vs {rate['FRZ-N1']:.3f} in "
        "FRZ-N1",
        naive, int(n.iloc[0]["hour"]) not in (2, 3, 4),
        f"by the recorded clock the excursions peak at {int(n.iloc[0]['hour'])}:00, not in the defrost window")

    r = run_file(conn, QUERIES / "s07_p3_units.sql")
    worst = r["naive_over_units"].astype(float)
    naive = """SELECT 'ERP' AS src, plant_id, sum(good_qty) FROM gatr.production_batches GROUP BY 1, 2
               UNION ALL SELECT 'P3 legacy', 'P3', sum(replace(replace(cantidad, '.', ''), ',', '.')::numeric)
               FROM gatr.p3_produccion_diaria"""
    add("S07", "P3's units", "s07_p3_units.sql", worst.max() / worst.min() > 10,
        "naive quantity / consumer units by plant and family: " + ", ".join(
            f"{a} {b} {c}" for a, b, c in r[["plant_id", "family", "naive_over_units"]].itertuples(index=False)),
        naive, True, "summing quantities across plants adds kilograms, pieces, units and cases")

    r = run_file(conn, QUERIES / "s08_inflation_illusion.sql")
    fs = r[r["segment"] == "FS"].set_index("year")
    sup = r[r["segment"] == "SUP"].set_index("year")
    naive = """SELECT extract(year FROM invoice_date) AS y, round(sum(net_amount) / 1e9, 1) AS ars_bn
               FROM gatr.sales_order_lines GROUP BY 1 ORDER BY 1"""
    n = q(conn, naive).dropna()
    g_ars = float(n.iloc[-2]["ars_bn"]) / float(n.iloc[0]["ars_bn"])
    add("S08", "Inflation illusion", "s08_inflation_illusion.sql",
        float(fs.loc[2025, "ars_bn_per_month"]) > 3 * float(fs.loc[2022, "ars_bn_per_month"]) and
        float(fs.loc[2025, "m_units_per_month"]) < float(fs.loc[2022, "m_units_per_month"]),
        f"food service: pesos per month x{float(fs.loc[2025, 'ars_bn_per_month']) / float(fs.loc[2022, 'ars_bn_per_month']):.1f} "
        f"2022->2025 while units per month x{float(fs.loc[2025, 'm_units_per_month']) / float(fs.loc[2022, 'm_units_per_month']):.2f}; "
        f"supermarkets units x{float(sup.loc[2025, 'm_units_per_month']) / float(sup.loc[2022, 'm_units_per_month']):.2f}",
        naive, g_ars > 5, f"revenue in pesos grows x{g_ars:.0f} from 2022 to 2025: growth, on its face")

    r = run_file(conn, QUERIES / "s09_simpson_yield.sql").iloc[0]
    naive = """SELECT plant_id, round(100 * avg(good_qty / nullif(planned_qty, 0)), 2) AS avg_yield
               FROM gatr.production_batches GROUP BY 1 ORDER BY 1"""
    n = q(conn, naive)
    add("S09", "Simpson's paradox in yield", "s09_simpson_yield.sql",
        float(r.p2_overall_yield) > float(r.p1_overall_yield) and r.skus_where_p1_is_better == r.skus_made_in_both,
        f"overall yield P2 {r.p2_overall_yield}% > P1 {r.p1_overall_yield}%, yet P1 is better on "
        f"{r.skus_where_p1_is_better} of {r.skus_made_in_both} SKUs both make",
        naive, True, "average yield by plant (with impossible rows in): " + ", ".join(
            f"{a} {b}" for a, b in n.itertuples(index=False)))

    r = run_file(conn, QUERIES / "s10_customer_consolidation.sql")
    top_group = r[r["method"] == "ultimate group"].iloc[0]
    top_today = r[r["method"] == "parent today"].iloc[0]
    naive = """SELECT o.customer_id, round(sum(l.net_amount) / 1e6) AS ars_m FROM gatr.sales_orders o
               JOIN gatr.sales_order_lines l USING (order_number) WHERE o.order_date >= '2025-01-01'
               AND o.order_date < '2026-01-01' GROUP BY 1 ORDER BY 2 DESC LIMIT 5"""
    n = q(conn, naive)
    add("S10", "Customer consolidation", "s10_customer_consolidation.sql",
        "La Estrella" in top_group["customer_name"] and "La Estrella" not in top_today["customer_name"],
        f"by ultimate group the top chain is {top_group['customer_name']} ({top_group['ars_millions']} M ARS); "
        f"by today's parent it is {top_today['customer_name']}, with La Estrella split across its distribution centres",
        naive, True, f"by customer_id the top 'customer' is {n.iloc[0]['customer_id']}: single stores and "
                     "distributors, no chain")

    r = run_file(conn, QUERIES / "s11_sku_reuse.sql")
    add("S11", "A reused SKU code", "s11_sku_reuse.sql",
        len(r) == 4 and (r["lines_by_code_alone"] > r["lines_in_its_life"]).all(),
        "; ".join(f"{a} {b}: {c} lines in its life, {d} by code alone" for a, b, c, d in
                  r[["sku_code", "description", "lines_in_its_life", "lines_by_code_alone"]].itertuples(index=False)),
        "SELECT sku_code, count(*) FROM gatr.products GROUP BY 1 HAVING count(*) > 1", True,
        "two duplicate codes are visible, but not that sales history mixes two products")

    ot = facts["overtime"]
    r = run_file(conn, QUERIES / "s12_overtime.sql")
    naive = """SELECT autorizado_por, round(sum(replace(horas_extra, ',', '.')::numeric)) AS hours
               FROM gatr.timeclock_punches WHERE horas_extra IS NOT NULL GROUP BY 1 ORDER BY 2 DESC LIMIT 10"""
    n = q(conn, naive)
    sup_leg = q(conn, f"SELECT legajo FROM gatr.employees WHERE employee_id = {ot['supervisor_id']}").iloc[0, 0]
    first = r.iloc[r["p90_ot_hours"].astype(float).argmax()]
    add("S12", "Inflated overtime", "s12_overtime.sql",
        first["sup_legajo"] == sup_leg and int(first["shifts_without_their_crew"]) >
        3 * r[r["sup_legajo"] != sup_leg]["shifts_without_their_crew"].astype(int).max(),
        f"crew {first['crew']} of {first['line_id']} (supervisor {sup_leg}): p90 overtime {first['p90_ot_hours']} h "
        f"per person, {first['shifts_without_their_crew']} shifts worked while the rest of the crew was off",
        naive, sup_leg not in n["autorizado_por"].head(3).tolist(),
        f"by approver, {sup_leg} is not among the top three: Sunday shifts at P1 dominate overtime")

    r = run_file(conn, QUERIES / "s13_price_list_lag.sql")
    fs_nov = r[(r["segment"] == "FOOD SERVICE") & (r["days_ahead"] > 60)]
    add("S13", "Food-service price list keyed with the wrong month", "s13_price_list_lag.sql",
        len(fs_nov) == 1 and int(fs_nov.iloc[0]["index_jan23"]) < 0.8 * int(
            r[(r["segment"] == "SUPERMERCADO") & (r["effective_from"] == date(2023, 11, 1))].iloc[0]["index_jan23"]),
        f"a food-service list effective {fs_nov.iloc[0]['effective_from']} was created "
        f"{fs_nov.iloc[0]['days_ahead']} days ahead at index {fs_nov.iloc[0]['index_jan23']} (others: "
        f"{int(r[(r['segment'] == 'SUPERMERCADO') & (r['effective_from'] == date(2023, 11, 1))].iloc[0]['index_jan23'])}); "
        "no food-service lists for August-October",
        "SELECT segment, count(*) FROM gatr.price_lists GROUP BY 1", True,
        "line counts per segment differ by a few percent: nothing points at a three-month gap")

    st = facts["channel_stuffing"]
    r = run_file(conn, QUERIES / "s14_channel_stuffing.sql")
    naive = "SELECT reason, count(*), round(sum(amount)) FROM gatr.returns GROUP BY 1 ORDER BY 3 DESC"
    n = q(conn, naive)
    add("S14", "Quarter-end channel stuffing", "s14_channel_stuffing.sql",
        int(r.iloc[0]["sales_rep_id"]) == st["rep_id"] and
        float(r.iloc[0]["credits_pct_of_sales"]) > 5 * float(r.iloc[1:]["credits_pct_of_sales"].astype(float).max()),
        f"rep {r.iloc[0]['sales_rep_id']}: {r.iloc[0]['pct_last_week_of_quarter']}% of sales in a quarter's last week "
        f"(next {r.iloc[1]['pct_last_week_of_quarter']}%), overstock credit notes {r.iloc[0]['credits_pct_of_sales']}% "
        "of sales",
        naive, True, "returns by reason: overstock returns exist, but by reason alone they're not tied to anyone")

    r = run_file(conn, QUERIES / "s15_checkweigher_overfill.sql").set_index("month")
    during = r.loc[[m for m in r.index if date(2024, 11, 1) <= m <= date(2025, 2, 1)], "sur01_overfill_pct"].astype(float)
    before = r.loc[[m for m in r.index if m <= date(2024, 9, 1)], "sur01_overfill_pct"].astype(float)
    naive = """SELECT b.line_id, count(*) FILTER (WHERE NOT q.passed) AS failed FROM gatr.quality_tests q
               JOIN gatr.production_batches b USING (batch_id) WHERE q.test_name = 'PESO NETO' GROUP BY 1"""
    n = q(conn, naive)
    add("S15", "Overfilling checkweigher on SUR-01", "s15_checkweigher_overfill.sql",
        during.mean() - before.mean() > 2.5,
        f"SUR-01 bags {before.mean():.1f}% over declared weight before October 2024, {during.mean():.1f}% "
        "November-February, back after the March 2025 fix",
        naive, int(n["failed"].sum()) == 0, "no net-weight test ever fails: overfill is inside the spec, and P2's "
                                            "results are in JSON")
    return out


# ---- 3: metrics --------------------------------------------------------------------------------------------------------

def metrics(conn, facts):
    rows = []

    def add(mid, name, true, naive, unit, naive_method, dim=""):
        t, n = float(true), float(naive)
        gap = (n - t) / abs(t) * 100 if t else None  # undefined when the truth is zero
        rows.append(dict(metric_id=mid, metric=name, dimension=dim, true_value=round(t, 4), naive_value=round(n, 4),
                         gap_pct=None if gap is None else round(gap, 1), unit=unit, naive_method=naive_method))

    OL = T("sales_order_lines", parse_dates=["order_date", "ship_date"])
    fx = T("fx_daily", parse_dates=["rate_date"]).set_index("rate_date")["ars_per_usd"]
    OL["rev"] = OL["units_shipped"] * OL["unit_price"] * (1 - OL["discount"])
    OL["fx"] = OL["ship_date"].map(fx)
    OL["usd"] = np.where(OL["currency"] == "USD", OL["rev"], OL["rev"] / OL["fx"])
    OL["ars"] = np.where(OL["currency"] == "ARS", OL["rev"], 0)
    gatr_era = (~OL["p3"]) | (OL["ship_date"] >= "2024-03-01")
    CN = T("credit_notes", parse_dates=["issued"])
    CN["fx"] = CN["issued"].map(fx)
    CN["usd"] = np.where(CN["currency"] == "USD", CN["amount"], CN["amount"] / CN["fx"])
    CN["ars"] = np.where(CN["currency"] == "ARS", CN["amount"], 0)
    Od = T("sales_orders", parse_dates=["order_date"]).set_index("order_id")
    CN["p3"] = CN["order_id"].map(Od["p3"]).fillna(False).astype(bool)
    cn_era = (~CN["p3"]) | (CN["issued"] >= "2024-03-01")

    # monthly revenue, net of credit notes
    OL["m"] = OL["ship_date"].dt.to_period("M")
    CN["m"] = CN["issued"].dt.to_period("M")
    mt = OL[gatr_era].groupby("m")[["ars", "usd"]].sum() - CN[cn_era].groupby("m")[["ars", "usd"]].sum().reindex(
        OL["m"].unique(), fill_value=0)
    naive_m = q(conn, """
        SELECT to_char(l.invoice_date, 'YYYY-MM') AS m, sum(l.net_amount) FILTER (WHERE l.currency = 'ARS') AS ars,
               sum(l.net_amount / f.rate) AS usd
        FROM gatr.sales_order_lines l JOIN gatr.fx_rates f ON f.rate_date = l.invoice_date
        GROUP BY 1""").set_index("m")
    for m, x in mt.sort_index().iterrows():
        k = str(m)
        if k in naive_m.index:
            add("M01", "Revenue in ARS, domestic, net of credit notes", x["ars"], naive_m.loc[k, "ars"] or 0, "ARS",
                "SUM(net_amount) of ARS lines by invoice month: every line, duplicates, test orders, credit-note "
                "lines and annulled copies included", k)
            add("M02", "Revenue in USD, net of credit notes", x["usd"], naive_m.loc[k, "usd"] or 0, "USD",
                "SUM(net_amount / rate) joining fx_rates on the invoice date (weekends and holidays drop out, "
                "fat-fingered rates kept, USD lines divided by the rate too)", k)
    y25 = mt[[str(p).startswith("2025") for p in mt.index]].sum()
    n25 = q(conn, """SELECT sum(net_amount) FILTER (WHERE currency = 'ARS') AS ars FROM gatr.sales_order_lines
                     WHERE invoice_date BETWEEN '2025-01-01' AND '2025-12-31'""").iloc[0]
    add("M03", "Revenue in ARS 2025, net of credit notes", y25["ars"], n25["ars"], "ARS",
        "SUM(net_amount) of ARS lines invoiced in 2025")
    ex = OL[(OL["segment"] == "EXPORTACION") & (OL["ship_date"].dt.year == 2024)]["usd"].sum()
    nex = q(conn, "SELECT sum(net_amount) FROM gatr.sales_order_lines WHERE currency = 'USD' AND "
                  "invoice_date BETWEEN '2024-01-01' AND '2024-12-31'").iloc[0, 0]
    add("M04", "Export revenue 2024", ex, nex, "USD", "SUM(net_amount) of lines with currency USD")

    # OEE
    B = T("production_batches", parse_dates=["start_ts", "end_ts"])
    P = T("products").set_index("product_key")
    L = T("production_lines").set_index("line_id")
    S = T("staffed_shifts")
    from gatr.ops import units_per_hour
    B["ideal_h"] = [g / (units_per_hour(L.loc[ln], P.at[pk, "pack_g"]) * 0.9) for g, ln, pk in
                    zip(B["good_units"], B["line_id"], B["product_key"])]
    staffed_h = S.groupby("line_id").size() * S["line_id"].map(L["shift_hours"]).groupby(S["line_id"]).first()
    true_oee = (B.groupby("line_id")["ideal_h"].sum() / staffed_h * 100)
    rep = q(conn, "SELECT line_id, avg(oee_pct) AS oee FROM gatr.mes_oee_daily GROUP BY 1").set_index("line_id")
    for line in rep.index:
        add("M05", "OEE by line", true_oee[line], rep.loc[line, "oee"], "%",
            "AVG(oee_pct) from the MES report", line)
    p12 = [x for x in rep.index]
    add("M06", "OEE, P1 and P2 lines", B[B["line_id"].isin(p12)]["ideal_h"].sum() / staffed_h[p12].sum() * 100,
        rep["oee"].mean(), "%", "AVG of the MES report's oee_pct")

    # recall
    ses = facts["sesame"]
    nr = q(conn, "SELECT count(DISTINCT lot_code) AS lots, sum(customers_notified) AS cust FROM gatr.recalls "
                 "WHERE recall_id = 'RET-2023-01'").iloc[0]
    add("M07", "Sesame recall blast radius: lots", len(ses["true_lots"]), nr["lots"], "lots",
        "COUNT of lots in the recall table")
    add("M08", "Sesame recall blast radius: customers", len(ses["true_customers"]),
        len(ses["official_customers"]), "customers", "distinct customers in shipments of the recalled lots")

    # receiving fraud / shrink
    R = T("goods_receipts", parse_dates=["receipt_date"])
    oil = R[(R["plant_id"] == "P1") & (R["rm_code"] == "RM-ACGI") & (R["receipt_date"] >= "2023-06-05")]
    add("M09", "P1 sunflower oil received but never delivered, since 2023-06", (oil["qty_recorded"] - oil["qty"]).sum(),
        0, "kg", "receipts taken at face value")
    CC = q(conn, """SELECT plant_id, material_code, sum(difference) AS d FROM gatr.cycle_counts GROUP BY 1, 2""")
    n_oil = CC[(CC["plant_id"] == "P1") & (CC["material_code"] == "RM-ACGI")]["d"].sum()
    add("M10", "P1 sunflower oil physical losses (true shrink)", 0, -float(n_oil), "kg",
        "cycle-count write-offs read as shrink (the oil was never there)")

    # yield
    B["kg_plan"] = B["planned_units"] * B["product_key"].map(P["pack_g"]) / 1000
    B["kg_good"] = B["good_units"] * B["product_key"].map(P["pack_g"]) / 1000
    B["family"] = B["product_key"].map(P["family"])
    ny = q(conn, """SELECT b.plant_id, p.family, avg(b.good_qty / nullif(b.planned_qty, 0)) * 100 AS y
                    FROM gatr.production_batches b LEFT JOIN gatr.products p ON p.sku_code = b.item_code
                    GROUP BY 1, 2""")
    fam_map = {"SNACKS-PAPAS": "CHIPS", "SNACKS-GALLETITAS": "CRACKERS", "SALSAS-FRASCO": "SAUCE_JAR",
               "CONGELADOS": "FROZEN"}
    for (pl, fam), g in B[B["plant_id"].isin(["P1", "P2"]) & B["family"].isin(fam_map.values())].groupby(
            ["plant_id", "family"]):
        erp = [k for k, v in fam_map.items() if v == fam][0]
        nv = ny[(ny["plant_id"] == pl) & (ny["family"] == erp)]["y"]
        add("M11", "Yield (good / planned, kg)", g["kg_good"].sum() / g["kg_plan"].sum() * 100,
            nv.iloc[0] if len(nv) else 0, "%", "AVG(good_qty / planned_qty), impossible rows included",
            f"{pl} {fam}")
    for pl in ("P1", "P2"):
        g = B[B["plant_id"] == pl]
        nv = q(conn, f"SELECT avg(good_qty / nullif(planned_qty, 0)) * 100 FROM gatr.production_batches "
                     f"WHERE plant_id = '{pl}'").iloc[0, 0]
        add("M12", "Yield by plant", g["kg_good"].sum() / g["kg_plan"].sum() * 100, nv, "%",
            "AVG(good_qty / planned_qty)", pl)

    # production by plant while P3 was on its legacy system
    win = (B["start_ts"] >= "2024-03-01") & (B["start_ts"] < "2025-03-01")
    nprod = q(conn, """
        SELECT 'P3' AS plant_id, sum(replace(replace(cantidad, '.', ''), ',', '.')::numeric) AS q
        FROM gatr.p3_produccion_diaria WHERE to_date(fecha, 'DD/MM/YYYY') BETWEEN '2024-03-01' AND '2025-02-28'
        UNION ALL SELECT plant_id, sum(good_qty) FROM gatr.production_batches
        WHERE start_time ~ '^\\d{4}' AND start_time >= '2024-03-01' AND start_time < '2025-03-01' GROUP BY 1
        """).set_index("plant_id")
    for pl in ("P1", "P2", "P3"):
        add("M13", "Units produced, Mar 2024 - Feb 2025", B[win & (B["plant_id"] == pl)]["good_units"].sum(),
            nprod.loc[pl, "q"], "units", "SUM of the quantity column of each plant's production records", pl)

    # customers
    H = T("customer_hierarchy", parse_dates=["valid_from"])
    est = facts["consolidation"]["acquirer"]
    costa = facts["consolidation"]["acquired"]
    chain_of = {}
    for x in H.itertuples(index=False):
        if x.level == "CADENA":
            chain_of[x.child_id] = x.parent_id
    succ = {int(k): v for k, v in facts["consolidation"]["successor"].items()}
    o25 = OL[(OL["ship_date"].dt.year == 2025) & gatr_era]
    grp = o25["customer_id"].map(lambda c: est if chain_of.get(c) in (est, costa) or c in succ.values() else
                                 chain_of.get(c, c))
    share_true = o25.groupby(grp)["ars"].sum().max() / o25["ars"].sum() * 100
    nshare = q(conn, """
        WITH r AS (SELECT o.customer_id, sum(l.net_amount) AS a FROM gatr.sales_orders o
                   JOIN gatr.sales_order_lines l USING (order_number)
                   WHERE l.currency = 'ARS' AND l.invoice_date BETWEEN '2025-01-01' AND '2025-12-31' GROUP BY 1)
        SELECT max(s) / (SELECT sum(a) FROM r) * 100 FROM (
          SELECT coalesce(h.parent_customer_id, r.customer_id) AS g, sum(r.a) AS s FROM r
          LEFT JOIN gatr.customer_hierarchy h ON h.child_customer_id = r.customer_id AND h.valid_to IS NULL
          GROUP BY 1) x""").iloc[0, 0]
    add("M14", "Top customer group's share of 2025 domestic revenue", share_true, nshare, "%",
        "group by the store's current parent in customer_hierarchy")
    active_true = o25["customer_id"].map(lambda c: c if c not in succ.values() else
                                         next(k for k, v in succ.items() if v == c)).nunique()
    nact = q(conn, """SELECT count(DISTINCT customer_id) FROM gatr.sales_orders
                      WHERE order_date BETWEEN '2025-01-01' AND '2025-12-31'""").iloc[0, 0]
    add("M15", "Customers buying in 2025", active_true, nact, "customers",
        "COUNT(DISTINCT customer_id) in sales_orders (re-created stores, duplicates, test customers)")
    prev = set(OL[OL["ship_date"].dt.year == 2024]["customer_id"])
    succ_inv = {v: k for k, v in succ.items()}
    norm = lambda s: {succ_inv.get(c, c) for c in s}  # noqa: E731
    nchurn = q(conn, """SELECT count(*) FROM (SELECT customer_id FROM gatr.sales_orders WHERE order_date BETWEEN
                        '2024-01-01' AND '2024-12-31' EXCEPT SELECT customer_id FROM gatr.sales_orders
                        WHERE order_date >= '2025-07-01' AND order_date < '2026-01-01') x""").iloc[0, 0]
    churn_true2 = len(norm(prev) - norm(set(OL[(OL["ship_date"] >= "2025-07-01") &
                                               (OL["ship_date"] < "2026-01-01")]["customer_id"])))
    add("M16", "Customers of 2024 not buying in 2025-H2", churn_true2, nchurn, "customers",
        "customer_ids of 2024 orders without an order in 2025-H2")

    # price list lag
    fsl = OL[(OL["segment"] == "FOOD SERVICE") & (OL["order_date"] >= "2023-08-01") & (OL["order_date"] < "2023-11-01")]
    leak = (fsl["units_shipped"] * (fsl["unit_price_intended"] - fsl["unit_price"]) * (1 - fsl["discount"])).sum()
    add("M17", "Food-service revenue lost to the mis-keyed price list", leak, 0, "ARS", "not noticed")

    # credit notes 2024
    cn24 = CN[(CN["issued"].dt.year == 2024) & cn_era]["ars"].sum()
    ncn = q(conn, """SELECT (SELECT coalesce(sum(-net_amount), 0) FROM gatr.sales_order_lines l
                             JOIN gatr.sales_orders o USING (order_number) WHERE o.order_type = 'NC'
                             AND o.order_date BETWEEN '2024-01-01' AND '2024-12-31' AND l.currency = 'ARS')
                          + (SELECT coalesce(sum(amount), 0) FROM gatr.returns WHERE currency = 'ARS'
                             AND issue_date BETWEEN '2024-01-01' AND '2024-12-31')""").iloc[0, 0]
    add("M18", "Credit notes issued in 2024", cn24, ncn, "ARS", "NC order lines plus returns, both in full")

    # complaints and quality
    Cm = T("customer_complaints", parse_dates=["received"])
    Rc = T("recipes")
    cut_ids = set(Rc[Rc["cost_cut"]]["recipe_id"])
    SH = T("shipments", parse_dates=["ship_date"])
    Bk = B.set_index("batch_id")
    SH["recipe_id"] = SH["batch_id"].map(Bk["recipe_id"])
    SH["formula"] = SH["product_key"].map(P["formula_code"])
    may = SH[SH["formula"] == "MAY-CLA"]
    Cm["recipe_id"] = Cm["batch_id"].map(Bk["recipe_id"])
    cm_may = Cm[Cm["product_key"].map(P["formula_code"]) == "MAY-CLA"]
    r_cut = cm_may["recipe_id"].isin(cut_ids).sum() / may[may["recipe_id"].isin(cut_ids)]["units"].sum() * 1e6
    r_oth = (~cm_may["recipe_id"].isin(cut_ids)).sum() / may[~may["recipe_id"].isin(cut_ids)]["units"].sum() * 1e6
    nmay = q(conn, """SELECT count(*) FILTER (WHERE received_at >= '2024-07-15' AND received_at < '2025-04-01') AS inside,
                             count(*) FILTER (WHERE received_at < '2024-07-15' OR received_at >= '2025-04-01') AS outside
                      FROM gatr.customer_complaints WHERE product ILIKE '%mayonesa%'""").iloc[0]
    months_in = 8.5
    months_out = 57 - months_in
    add("M19", "Complaint rate ratio, cost-cut mayonnaise lots vs other mayonnaise lots", r_cut / r_oth,
        (nmay["inside"] / months_in) / (nmay["outside"] / months_out), "x",
        "complaints mentioning mayonesa per month during the cost-cut window over per month outside it")

    QC = T("incoming_qc", parse_dates=["tested_at"])
    own = facts["ownership_change"]
    ent = QC[QC["supplier_id"].isin([own["old_id"], own["new_id"]]) & (QC["test"] == "MICROBIOLOGIA")]
    last = ent[ent["tested_at"] >= "2025-10-01"]
    nfail = q(conn, f"""SELECT avg((NOT q.passed)::int) * 100 FROM gatr.quality_tests q JOIN gatr.goods_receipts g
                        USING (receipt_id) WHERE g.supplier_id = {own['new_id']} AND q.test_name = 'MICROBIOLOGIA'
                        AND q.passed IS NOT NULL""").iloc[0, 0]
    add("M20", "Spice supplier's incoming micro failure rate, Oct 2025 - Sep 2026", last["failed"].mean() * 100, nfail,
        "%", "failure rate of the supplier_id over its whole life")

    # downtime and overtime
    DT = T("downtime_events", parse_dates=["start_ts"])
    st4 = S[S["line_id"] == "L1-04"]
    true_dt = DT[(DT["line_id"] == "L1-04") & (~DT["planned"])]["minutes"].sum() / st4["day"].nunique()
    ndt = q(conn, """SELECT sum(duration_min)::numeric / count(DISTINCT left(start_time, 10)) FROM gatr.downtime_events
                     WHERE line_id = 'L1-04' AND reason <> 'CAMBIO DE FORMATO'""").iloc[0, 0]
    add("M21", "L1-04 unplanned downtime per production day", true_dt, ndt, "minutes", "SUM(duration_min) of logged events")
    TC = T("timeclock_shifts")
    add("M22", "Overtime hours with no production behind them", TC[TC["inflated"]]["ot_minutes"].sum() / 60, 0,
        "hours", "all approved overtime treated as needed")

    # freezer
    sens = T("cold_storage_temperatures", parse_dates=["ts_local"])
    n2 = sens[(sens["unit_id"] == "FRZ-N2") & (sens["ts_local"].dt.year == 2025)]
    exc_night = (n2["temp_c"] > -12) & n2["ts_local"].dt.hour.between(2, 4)
    nn = q(conn, """SELECT count(*) FROM gatr.cold_storage_sensor_readings WHERE sensor_id = 'TS-FRZ-N2'
                    AND value > -12 AND extract(hour FROM reading_time) BETWEEN 2 AND 4
                    AND reading_time >= '2025-01-01' AND reading_time < '2026-01-01'""").iloc[0, 0]
    add("M23", "FRZ-N2 readings over alarm between 02:00 and 04:59 local, 2025", exc_night.sum(), nn, "readings",
        "the same filter on the recorded clock")
    p3s = sens[(sens["unit_id"] == "CH-D1") & (sens["ts_local"].dt.year == 2024)]["temp_c"].mean()
    np3 = q(conn, """SELECT avg(value) FROM gatr.cold_storage_sensor_readings WHERE sensor_id = 'TS-CH-D1'
                     AND reading_time >= '2024-03-01' AND reading_time < '2025-01-01'""").iloc[0, 0]
    add("M24", "P3 chiller CH-D1 mean temperature 2024", p3s, np3, "°C", "AVG(value)")

    # people
    E = T("employees", parse_dates=["hire_date", "termination_date"])
    end = pd.Timestamp("2026-09-30")
    hc = ((E["hire_date"] <= end) & (E["termination_date"].isna() | (E["termination_date"] > end))).sum()
    nhc = q(conn, """SELECT count(*) FROM gatr.employees WHERE termination_date IS NULL
                     OR termination_date > '2026-09-30'""").iloc[0, 0]
    add("M25", "Headcount on 2026-09-30", hc, nhc, "people", "employees with no termination date or a future one")

    # volumes, fill rate
    o25 = OL[(OL["ship_date"].dt.year == 2025)]
    nv = q(conn, "SELECT sum(qty_shipped) FROM gatr.sales_order_lines WHERE invoice_date BETWEEN '2025-01-01' AND "
                 "'2025-12-31'").iloc[0, 0]
    add("M26", "Units shipped in 2025", o25["units_shipped"].sum(), nv, "units", "SUM(qty_shipped), cases and units mixed")
    fill = o25["units_shipped"].sum() / o25["units"].sum() * 100
    nfill = q(conn, "SELECT sum(qty_shipped) / sum(qty_ordered) * 100 FROM gatr.sales_order_lines WHERE invoice_date "
                    "BETWEEN '2025-01-01' AND '2025-12-31'").iloc[0, 0]
    add("M27", "Fill rate 2025 (units shipped / ordered)", fill, nfill, "%", "SUM(qty_shipped) / SUM(qty_ordered) of "
                                                                          "invoiced lines (unfilled lines have no invoice date)")
    fsv = OL[(OL["segment"] == "FOOD SERVICE")]
    g_units = fsv[fsv["ship_date"].dt.year == 2025]["units_shipped"].sum() / fsv[fsv["ship_date"].dt.year == 2022][
        "units_shipped"].sum()
    nfs = q(conn, """SELECT sum(l.net_amount) FILTER (WHERE extract(year FROM l.invoice_date) = 2025)
                     / sum(l.net_amount) FILTER (WHERE extract(year FROM l.invoice_date) = 2022)
                     FROM gatr.sales_order_lines l JOIN gatr.sales_orders o USING (order_number)
                     JOIN gatr.customers c USING (customer_id) WHERE c.segment = 'FS'""").iloc[0, 0]
    add("M28", "Food service growth 2022 -> 2025", g_units, nfs, "x", "revenue in pesos, 2025 over 2022")

    # waste, energy
    Wt = T("waste", parse_dates=["day"])
    w24 = Wt[Wt["day"].dt.year == 2024]["kg"].sum()
    nw = q(conn, r"""SELECT sum(quantity::numeric) FROM gatr.waste_log WHERE waste_date ~ '^\d{4}-' AND
                     quantity ~ '^-?\d+(\.\d+)?$' AND waste_date BETWEEN '2024-01-01' AND '2024-12-31'""").iloc[0, 0]
    add("M29", "Waste in 2024", w24, nw, "kg", "rows whose date is ISO and whose quantity is a plain number")
    EH = T("energy_hourly", parse_dates=["ts"])
    e3 = EH[(EH["plant_id"] == "P3") & (EH["ts"].dt.year == 2025) & (EH["ts"] >= "2025-05-05")]["kwh"].sum()
    ne = q(conn, """SELECT sum(value) FROM gatr.energy_meter_readings WHERE meter_id = 'MED-P3-GENERAL'
                    AND reading_ts >= '2025-05-05' AND reading_ts < '2026-01-01'""").iloc[0, 0]
    add("M30", "P3 electricity, 2025-05-05 to year end", e3, ne, "kWh", "SUM(value) of the meter's readings")

    # counts of things
    Su = T("suppliers")
    add("M31", "Suppliers (legal entities)", Su["tax_id"].nunique() + Su["tax_id"].isna().sum(),
        q(conn, "SELECT count(*) FROM gatr.suppliers").iloc[0, 0], "suppliers", "COUNT(*) of suppliers")
    sold = OL[OL["ship_date"].dt.year == 2025]["product_key"].nunique()
    nsold = q(conn, "SELECT count(DISTINCT sku_code) FROM gatr.sales_order_lines WHERE invoice_date BETWEEN "
                    "'2025-01-01' AND '2025-12-31'").iloc[0, 0]
    add("M32", "Products sold in 2025", sold, nsold, "SKUs", "COUNT(DISTINCT sku_code) of 2025 lines")
    return pd.DataFrame(rows)


# ---- 4: mess profile and names -----------------------------------------------------------------------------------------

NULLISH = ("''", "'N/A'", "'-'", "'null'", "'NULL'", "'n/a'", "'s/d'")
# Columns a re-ingested or re-entered copy differs in: duplicates are counted ignoring them
IGNORE_FOR_DUPS = {"sales_order_lines": ["_loaded_at"], "downtime_events": ["event_id"],
                   "production_batches": ["batch_id", "created_at", "deleted_at"],
                   "sales_orders": ["order_number", "status", "created_at"]}
REAL_NAMES = ["arcor", "bagley", "molinos río de la plata", "molinos rio de la plata", "hellmann's", "hellmanns",
              "natura", "fargo", "bimbo", "serenísima", "serenisima", "carrefour", "coto", "jumbo", "walmart",
              "cencosud", "changomas", "chango más", "mastellone", "ledesma", "lay's", "pepsico", "kraft", "mondelez",
              "nestlé", "nestle", "unilever", "danone", "paty", "swift", "granja del sol", "mccain", "knorr", "maggi",
              "quilmes", "terrabusi", "cerealitas", "criollitas", "pehuamar", "krachitos", "kesbun", "la campagnola",
              "marolio", "dánica", "danica", "cagnoli", "paladini", "makro", "la anónima", "la anonima",
              "dia argentina", "pritty", "fritolay", "frito lay", "doritos", "pringles", "ritz", "oreo", "ilolay",
              "sancor", "la virginia", "cabrales", "lucchetti", "matarazzo", "don vicente", "la salteña", "la saltena",
              "la española", "fanacoa", "alicante", "dos anclas", "celusal", "savora", "baggio", "cepita", "tregar",
              "milkaut", "la paulina", "rapanui", "havanna", "georgalos", "felfort", "bonafide", "fantoche"]


def profile(conn):
    tabs = q(conn, """SELECT table_name, column_name, data_type FROM information_schema.columns
                      WHERE table_schema = 'gatr' ORDER BY table_name, ordinal_position""")
    out = []
    for t, g in tabs.groupby("table_name", sort=False):
        n = int(q(conn, f"SELECT count(*) FROM gatr.{t}").iloc[0, 0])
        dup = int(q(conn, f"SELECT count(*) - (SELECT count(*) FROM (SELECT DISTINCT * FROM gatr.{t}) x) "
                          f"FROM gatr.{t}").iloc[0, 0])
        keep = [c for c in g["column_name"] if c not in IGNORE_FOR_DUPS.get(t, [])]
        near = dup if t not in IGNORE_FOR_DUPS else int(q(conn, (
            f"SELECT count(*) - (SELECT count(*) FROM (SELECT DISTINCT {', '.join(keep)} FROM gatr.{t}) x) "
            f"FROM gatr.{t}")).iloc[0, 0])
        parts = []
        for c, typ in zip(g["column_name"], g["data_type"]):
            if typ == "text":
                parts.append(f"count(*) FILTER (WHERE {c} IS NULL OR {c} IN ({', '.join(NULLISH)}))")
            elif typ == "date":
                parts.append(f"count(*) FILTER (WHERE {c} IS NULL OR {c} IN ('1900-01-01', '9999-12-31'))")
            else:
                parts.append(f"count(*) FILTER (WHERE {c} IS NULL)")
        nulls = q(conn, f"SELECT {', '.join(parts)} FROM gatr.{t}").iloc[0].tolist() if parts else []
        out.append(dict(table=t, rows=n, dup_rows=dup, near_dup_rows=near, near_dup_ignoring=IGNORE_FOR_DUPS.get(t, []),
                        columns=[
            dict(column=c, type=typ, nullish=int(v), pct=round(100 * v / n, 1) if n else 0)
            for c, typ, v in zip(g["column_name"], g["data_type"], nulls)]))
    return out


def scan_names():
    """Whole-word matches of real brand, maker and retailer names in every value of every table."""
    pattern = re.compile(r"(?<![\w])(" + "|".join(re.escape(n) for n in REAL_NAMES) + r")(?![\w])")
    hits = []
    for f in sorted(DATA.glob("*.csv.gz")):
        text = gzip.open(f, "rt", encoding="utf-8").read().lower()
        for m in pattern.finditer(text):
            hits.append((f.name, m.group(1), text[max(0, m.start() - 40):m.end() + 40].replace("\n", " ")))
    return hits


# ---- report ------------------------------------------------------------------------------------------------------------

def write_report(stories, mt, prof, hits, seconds):
    lines = ["# Verification\n",
             f"Written by `generator/verify.py` on the data in `lab/db/gatr/` ({seconds:.0f} s). Every check below "
             "ran on the recorded tables as loaded into Postgres, using nothing from the answer key except to "
             "compare.\n",
             "## Stories: found from the recorded data alone\n",
             "| Story | Query | Found | What the query shows |", "|---|---|---|---|"]
    for s in stories:
        lines.append(f"| {s['id']} {s['title']} | [`{s['query']}`](queries/{s['query']}) | "
                     f"{'yes' if s['ok'] else '**NO**'} | {s['finding']} |")
    lines += ["", "## Stories: not given away by a trivial single-table GROUP BY\n",
              "The first thing an analyst would try, per story, and what it shows.\n",
              "| Story | Naive query | Doesn't give it away | What it shows |", "|---|---|---|---|"]
    for s in stories:
        sql = " ".join(s["naive_sql"].split())
        lines.append(f"| {s['id']} | `{sql}` | {'yes' if s['naive_ok'] else '**NO**'} | {s['naive_finding']} |")
    material = mt["gap_pct"].abs().gt(10) | (mt["gap_pct"].isna() & mt["naive_value"].ne(0))
    big = material.groupby(mt["metric_id"]).mean() > 0.5
    lines += ["", "## Naive vs true metrics\n",
              f"{len(mt['metric_id'].unique())} metrics ({len(mt)} rows with their dimensions) in "
              "[ground_truth_metrics.csv](ground_truth_metrics.csv). Metrics whose naive value is more than 10% off "
              f"the true value (in most of their rows): **{int(big.sum())}** of {len(big)}.\n",
              "| Metric | Dimension | True | Naive | Gap |", "|---|---|---:|---:|---:|"]
    for x in mt.itertuples(index=False):
        if x.metric_id in ("M01", "M02") and x.dimension not in ("2022-06", "2023-10", "2024-07", "2025-06",
                                                                  "2026-06"):
            continue
        gap = "n/a (true is 0)" if pd.isna(x.gap_pct) else f"{x.gap_pct:+.1f}%"
        lines.append(f"| {x.metric_id} {x.metric} | {x.dimension} | {x.true_value:,.2f} | {x.naive_value:,.2f} | "
                     f"{gap} |")
    lines += ["", "(M01 and M02 are monthly: five months shown, all in the CSV.)", "",
              "## Mess profile\n", "Rows, exact duplicate rows, and per column the share of null-like values (NULL, "
              "'', 'N/A', '-', 'null', 's/d', and 1900-01-01 / 9999-12-31 in date columns).\n",
              "Duplicates: exact duplicate rows, and rows that repeat another but for the columns a re-ingested or "
              "re-entered copy changes (load timestamps, ids, status), listed per table in data_profile.json.\n",
              "| Table | Rows | Exact duplicates | Near-duplicates | Most null-like columns |", "|---|---:|---:|---:|---|"]
    total = 0
    for t in prof:
        total += t["rows"]
        worst = sorted(t["columns"], key=lambda c: -c["pct"])[:4]
        lines.append(f"| `{t['table']}` | {t['rows']:,} | {t['dup_rows']:,} ({100 * t['dup_rows'] / max(t['rows'], 1):.2f}%) | "
                     f"{t['near_dup_rows']:,} ({100 * t['near_dup_rows'] / max(t['rows'], 1):.2f}%) | "
                     + ", ".join(f"{c['column']} {c['pct']}%" for c in worst if c["pct"] > 0) + " |")
    lines += ["", f"{len(prof)} tables, {total:,} rows. Every column's rate is in "
                  "[data_profile.json](data_profile.json).", "",
              "## Real names\n",
              f"Every text value of every table scanned for {len(REAL_NAMES)} real Argentine and international food "
              "brands, manufacturers and retailers: "
              + ("**no matches**." if not hits else f"**{len(hits)} matches**: " +
                 "; ".join(f"{a}: '{b}' in …{c}…" for a, b, c in hits)),
              "Tax ids are CUIT-shaped with a deliberately wrong check digit, so none can be a real one; people's "
              "names are random first/last name pairs; no addresses beyond city and province."]
    (KEY / "verification.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    t0 = time.time()
    container = None
    dsn = os.environ.get("GATR_PG_DSN")
    if not dsn:
        container, dsn = start_docker()
    try:
        with psycopg.connect(dsn, autocommit=False) as conn:
            print("loading gatr.sql")
            load(conn)
            conn.autocommit = True
            facts = story_facts()
            print("stories")
            stories = check_stories(conn, facts)
            for s in stories:
                print(f"  {s['id']} found={s['ok']} naive-hidden={s['naive_ok']}: {s['finding'][:110]}")
            print("metrics")
            mt = metrics(conn, facts)
            mt.to_csv(KEY / "ground_truth_metrics.csv", index=False)
            print("profile")
            prof = profile(conn)
            (KEY / "data_profile.json").write_text(json.dumps(prof, indent=1) + "\n", encoding="utf-8")
            hits = scan_names()
            write_report(stories, mt, prof, hits, time.time() - t0)
            bad = [s["id"] for s in stories if not (s["ok"] and s["naive_ok"])]
            print(f"done: {len(stories) - len(bad)}/{len(stories)} stories pass; name hits: {len(hits)}")
            return 1 if bad or hits else 0
    finally:
        if container:
            subprocess.run(["docker", "rm", "-f", container], capture_output=True)


if __name__ == "__main__":
    sys.path.insert(0, str(HERE))
    sys.exit(main())
