"""The recorded layer, operations: purchasing and receiving (ERP), production (ERP and MES), quality (LIMS), downtime
(MES), maintenance (CMMS), time clock (vendor system), inventory (WMS), waste, energy and cold-storage sensors."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from . import catalog as C
from .common import END, ERP_UPGRADE, P3_GO_LIVE, d, rng
from .mess import comma, dmy, dmy_hm, epoch_ms_from_local, iso_date, iso_ts, lstrip_zeros, mdy, num
from .quality import RECALL_DAY


def after_go_live(plant, day):
    return plant != "P3" or day >= P3_GO_LIVE


def build_purchasing(R):
    w = R.w
    r = rng("rec_purchasing")
    H = w["po_headers"]
    L = w["po_lines"]
    G = w["receipts"]
    dup = R.dup_supplier
    # P2's orders and receipts go to its own duplicate supplier records
    def sid(s, plant):
        return dup.get(int(s), int(s)) if plant == "P2" else int(s)

    heads = []
    for x in H.itertuples(index=False):
        if not after_go_live(x.plant_id, x.order_date):
            continue
        st = "CERR" if x.order_date < END - timedelta(days=30) else r.choice(["ABIE", "CERR"])
        heads.append(dict(po_number=int(x.po_id), supplier_id=sid(x.supplier_id, x.plant_id), plant_id=x.plant_id,
                          order_date=iso_date(x.order_date), buyer_id=x.buyer_id, currency=x.currency, po_type=x.kind,
                          status=st, created_at=iso_ts(datetime.combine(x.order_date, datetime.min.time()) +
                                                      timedelta(hours=float(r.uniform(8, 18))))))
    Hd = pd.DataFrame(heads)
    keep = set(Hd["po_number"])
    lines = []
    for x in L.itertuples(index=False):
        if x.po_id not in keep:
            continue
        uom = x.uom
        qty = x.qty_ordered
        if x.rm_code == "RM-COMI":
            uom, qty = "LB", round(qty * 2.20462, 1)
        lines.append(dict(po_number=int(x.po_id), line_no=x.line_no, material_code=x.rm_code,
                          item_text=getattr(x, "item_text", None) if isinstance(getattr(x, "item_text", None), str)
                          else None, qty_ordered=num(qty, 2), uom=uom.upper() if uom else None,
                          unit_price=num(x.unit_price, 4), promised_date=iso_date(x.promised_date),
                          line_status="ANUL" if x.cancelled else "RECIBIDA" if x.rm_code else "CERRADA"))
    R.put("purchase_orders", Hd)
    R.put("purchase_order_lines", pd.DataFrame(lines))

    rows = []
    late = 0
    for x in G.itertuples(index=False):
        if not after_go_live(x.plant_id, x.receipt_date):
            continue
        slot = x.supplier_lot
        if x.plant_id == "P2" and slot:
            slot = slot.replace("-", "").replace("/", "").upper()  # P2's scanners strip separators
        qty, uom = x.qty_recorded, x.uom.upper()
        if x.rm_code == "RM-COMI":
            qty, uom = qty * 2.20462, "LB"
        created = datetime.combine(x.receipt_date, datetime.min.time()) + timedelta(hours=float(r.uniform(9, 19)))
        if r.random() < 0.03:
            created += timedelta(days=int(r.integers(20, 120)))
            late += 1
        rows.append(dict(receipt_id=int(x.receipt_id), po_number=None if pd.isna(x.po_id) else int(x.po_id),
                         po_line=None if pd.isna(x.po_line) else int(x.po_line), plant_id=x.plant_id,
                         material_code=x.rm_code, supplier_id=sid(x.supplier_id, x.plant_id),
                         receipt_date=iso_date(x.receipt_date), qty_received=num(qty, 3), uom=uom,
                         supplier_lot=slot, internal_lot=x.internal_lot, received_by=x.received_by,
                         qc_status={"APROBADO": "APROBADO", "RECHAZADO": "RECHAZADO",
                                    "APROBADO CON DESVIO": "APROB. C/DESVIO"}[x.qc_disposition],
                         created_at=iso_ts(created)))
    R.put("goods_receipts", pd.DataFrame(rows))
    R.reg.rule("L01", "goods_receipts", "supplier_lot", "P2 receiving scanners",
               "P2's scanners strip '-' and '/' from supplier lot numbers and upper-case them; P1 keeps them as "
               "printed. The same supplier lot reads differently by plant.")
    R.reg.count("L01", sum(1 for x in rows if x["plant_id"] == "P2"), len(rows))
    R.reg.rule("A01", "goods_receipts, production_batches, customer_complaints, downtime_events", "created_at",
               "ERP / MES / CRM", "Late-arriving records: created_at weeks or months after the event date.")
    R.reg.count("A01", late, len(rows))


PO_STATUS_OLD = {"done": "7", "cancelled": "9"}
PO_STATUS_NEW = {"done": "5", "cancelled": "8"}


def build_production(R):
    w = R.w
    r = rng("rec_production")
    B = w["batches"]
    PO = w["production_orders"]
    p = R.p
    from .master import units_per_case
    lines = w["lines"].set_index("line_id")
    from .ops import units_per_hour

    def keep(plant, ts):
        return after_go_live(plant, pd.Timestamp(ts).date())

    # production orders
    rows = []
    for x in PO.itertuples(index=False):
        if not keep(x.plant_id, x.sched_start):
            continue
        L = lines.loc[x.line_id]
        u = units_per_case(w, x.product_key, x.week)
        rate = units_per_hour(L, p.at[x.product_key, "pack_g"]) * 0.9
        if x.plant_id == "P2":
            qty, uom, rate_s = round(x.planned_units / u, 1), "CJ", num(rate / u, 1)
        else:
            qty, uom, rate_s = x.planned_units, "UN", num(rate, 0)
        done = pd.Timestamp(x.sched_end).date()
        status = (PO_STATUS_NEW if done >= ERP_UPGRADE else PO_STATUS_OLD)["done"]
        if done > END - timedelta(days=2):
            status = "2"
        rows.append(dict(order_no=int(x.production_order_id), plant_id=x.plant_id, line_id=x.line_id,
                         sku_code=R.sku[x.product_key],
                         recipe_id=None if (x.plant_id == "P2" and r.random() < 0.35) else int(x.recipe_id),
                         planned_qty=num(qty, 1), uom=uom, std_rate_per_hour=rate_s,
                         sched_start=iso_ts(x.sched_start), sched_end=iso_ts(x.sched_end), status=status,
                         created_at=iso_ts(pd.Timestamp(x.sched_start) - timedelta(days=int(r.integers(2, 6)),
                                                                                   hours=float(r.uniform(0, 8))))))
    # go-live test orders at P3
    for k in range(12):
        t = datetime(2025, 5, 2, 9) + timedelta(hours=k * 3)
        rows.append(dict(order_no=990_000 + k, plant_id="P3", line_id=r.choice(["DEL-01", "DEL-02"]),
                         sku_code="TEST-P3", recipe_id=None, planned_qty="100", uom="UN", std_rate_per_hour=None,
                         sched_start=iso_ts(t), sched_end=iso_ts(t + timedelta(hours=2)), status="8",
                         created_at=iso_ts(t - timedelta(hours=1)), notes="PRUEBA GO-LIVE"))
    R.put("production_orders", pd.DataFrame(rows))
    R.reg.rule("M06", "production_orders", "status", "ERP upgrade 2024-07-01",
               "Order status codes changed meaning at the upgrade: before, 5 = released and 7 = closed, 9 = "
               "cancelled; after, 2 = released, 5 = closed, 8 = cancelled. The dictionary lists the old codes.")
    R.reg.count("M06", sum(1 for x in rows if x["status"] in ("5", "8")), len(rows))
    R.reg.rule("U03", "production_orders, production_batches, mes_oee_daily", "planned_qty, good_qty, uom", "ERP config "
               "per plant", "P2 records production quantities in cases (CJ, with decimals); P1 and P3 in units (UN).")

    # batches
    out = []
    fixes = dict(swap=0, x10=0, neg=0, future=0, dup=0, late=0)
    next_id = int(B["batch_id"].max()) + 1000
    p2_backfill = (d("2023-03-06"), d("2023-03-19"))
    for x in B.itertuples(index=False):
        if not keep(x.plant_id, x.start_ts):
            continue
        st, en = pd.Timestamp(x.start_ts), pd.Timestamp(x.end_ts)
        if x.plant_id == "P2":
            u = units_per_case(w, x.product_key, st.date())
            planned, good, rej, uom = round(x.planned_units / u, 2), round(x.good_units / u, 2), round(
                x.reject_units / u, 2), "CJ"
        else:
            planned, good, rej, uom = x.planned_units, x.good_units, x.reject_units, "UN"
        created = en + timedelta(minutes=float(r.uniform(5, 90)))
        if x.plant_id == "P2" and p2_backfill[0] <= st.date() <= p2_backfill[1]:
            created = datetime(2023, 6, 14, 10) + timedelta(minutes=float(r.uniform(0, 600)))
            fixes["late"] += 1
        u_ = r.random()
        if u_ < 0.0008:
            st, en = en, st
            fixes["swap"] += 1
        elif u_ < 0.0013:
            good = good * 10
            fixes["x10"] += 1
        elif u_ < 0.0016:
            rej = -rej if rej else -1
            fixes["neg"] += 1
        elif u_ < 0.00175:
            st = st.replace(year=2062) if st.year == 2026 else st.replace(year=st.year + 10)
            en = en.replace(year=st.year)
            fixes["future"] += 1
        fmt = (lambda t: dmy_hm(t)) if x.plant_id == "P3" else (lambda t: iso_ts(t))
        shift = {"M": "1", "T": "2", "N": "3"}[x.shift] if x.plant_id == "P3" else x.shift
        row = dict(batch_id=int(x.batch_id), order_no=int(x.production_order_id), plant_id=x.plant_id,
                   line_id=x.line_id, item_code=R.sku[x.product_key], lot_code=x.lot_code, shift=shift,
                   start_time=fmt(st), end_time=fmt(en), planned_qty=num(planned, 2), good_qty=num(good, 2),
                   reject_qty=num(rej, 2), uom=uom, created_at=iso_ts(created), deleted_at=None)
        if r.random() < 0.004:
            # entered twice: the first entry soft-deleted, the batch's consumption stays on it
            dup = dict(row)
            next_id += 1
            row["deleted_at"] = iso_ts(created + timedelta(hours=float(r.uniform(1, 30))))
            dup["batch_id"] = next_id
            dup["created_at"] = row["deleted_at"]
            out.append(row)
            out.append(dup)
            fixes["dup"] += 1
            R.batch_renumber[int(x.batch_id)] = next_id
            continue
        out.append(row)
    df = pd.DataFrame(out)
    R.put("production_batches", df)
    R.reg.rule("I01", "production_batches", "start_time, end_time, good_qty, reject_qty", "Operators / ERP",
               f"Impossible values at a low rate: {fixes['swap']} batches end before they start, {fixes['x10']} "
               f"good_qty keyed with an extra digit (yield ~1000%), {fixes['neg']} negative rejects, "
               f"{fixes['future']} dated ten-plus years ahead.")
    R.reg.count("I01", fixes["swap"] + fixes["x10"] + fixes["neg"] + fixes["future"], len(df))
    R.reg.rule("D03", "production_batches", "batch_id, deleted_at", "ERP", f"{fixes['dup']} batches entered twice: "
               "the first row soft-deleted (deleted_at), a second row with a new batch_id and the same lot; the "
               "batch's consumption stays on the deleted id.")
    R.reg.count("D03", fixes["dup"] * 2, len(df))
    R.reg.count("A01", fixes["late"], len(df))
    R.reg.rule("F04", "production_batches", "start_time, end_time", "P3 interface (since 2025-05-05)",
               "P3's batches come through an interface that writes 'DD/MM/YYYY HH:MM'; the rest is ISO.")
    R.reg.count("F04", (df["plant_id"] == "P3").sum(), len(df))
    R.reg.rule("C01", "production_batches, production_orders, sales_order_lines, ...", "item_code / sku_code / "
               "material_code / cod_articulo / item", "every system", "The product key is named differently "
                                                                    "across tables.")


def build_consumption(R):
    w = R.w
    r = rng("rec_consumption")
    C = w["consumption"]
    rm = w["raw_materials"].set_index("rm_code")
    minor = set(rm[rm["category"].isin(["ESPECIAS", "ADITIVOS", "AROMAS", "BASICOS"])].index)
    plant_of = w["batches"].set_index("batch_id")["plant_id"]
    start_of = w["batches"].set_index("batch_id")["start_ts"]
    B_keep = {int(b) for b, pl, st in zip(plant_of.index, plant_of.values, start_of.values)
              if after_go_live(pl, pd.Timestamp(st).date())}
    rows = []
    nolot = 0
    for x in C.itertuples(index=False):
        if x.batch_id not in B_keep:
            continue
        lot = x.internal_lot
        if x.plant_id == "P1" and x.rm_code in minor and lot is not None and r.random() < 0.18:
            lot = None  # backflushed without scanning the lot
            nolot += 1
        uom = rm.at[x.rm_code, "uom"].upper() if x.rm_code in rm.index else "KG"
        rows.append(dict(batch_id=int(x.batch_id), material_code=x.rm_code, lot=lot, qty=num(x.qty_std, 3), uom=uom))
    df = pd.DataFrame(rows)
    R.put("batch_consumption", df)
    R.reg.rule("L02", "batch_consumption", "lot", "P1 backflush", "At P1, minor ingredients (spices, additives, "
               "salt, sugar) are backflushed without scanning the lot in ~18% of rows: lot is NULL.")
    R.reg.count("L02", nolot, len(df))
    R.reg.rule("Q01", "batch_consumption", "qty", "ERP backflush", "Consumption is the recipe standard for the "
               "planned quantity, not what was actually used: book stock drifts from physical stock.")


def build_quality(R):
    w = R.w
    r = rng("rec_quality")
    T = w["batch_tests"]
    Q = w["incoming_qc"]
    rows = []
    tid = 5_000_000
    n_comma = 0
    keepB = set(R.out["production_batches"]["batch_id"])
    p2_json = 0
    for (bid, plant), g in T.groupby(["batch_id", "plant_id"], sort=False):
        if bid not in keepB:
            continue
        if plant == "P2":
            panel = {}
            nan_era = g["tested_at"].iloc[0] < pd.Timestamp("2023-04-01")
            for x in g.itertuples(index=False):
                v = x.value if r.random() > 0.01 else None
                lo = None if x.spec_min is None or pd.isna(x.spec_min) else x.spec_min
                hi = None if x.spec_max is None or pd.isna(x.spec_max) else x.spec_max
                if nan_era:  # the LIMS exporter wrote NaN for a missing limit until its April 2023 fix
                    lo = float("nan") if lo is None else lo
                    hi = float("nan") if hi is None else hi
                panel[x.test] = {"valor": v, "unidad": x.unit, "min": lo, "max": hi,
                                 "conforme": (not x.failed) if v is not None else None}
            tid += 1
            x0 = g.iloc[0]
            rows.append(dict(test_id=tid, plant_id=plant, sample_type="PT", batch_id=int(bid), lot_code=x0["lot_code"],
                             receipt_id=None, material_code=None, test_name="PANEL PT", result=None, unit=None,
                             spec_min=None, spec_max=None, passed=None,
                             result_json=json.dumps(panel, ensure_ascii=False),
                             tested_at=iso_ts(g["tested_at"].max())))
            p2_json += 1
            continue
        for x in g.itertuples(index=False):
            tid += 1
            v = x.value
            if plant == "P1" and r.random() < 0.3:
                vs = comma(v, 3)
                n_comma += 1
            else:
                vs = num(v, 3)
            rows.append(dict(test_id=tid, plant_id=plant, sample_type="PT", batch_id=int(bid), lot_code=x.lot_code,
                             receipt_id=None, material_code=None, test_name=x.test, result=vs, unit=x.unit,
                             spec_min=num(x.spec_min, 2) if x.spec_min is not None and not pd.isna(x.spec_min)
                             else None, spec_max=num(x.spec_max, 2) if x.spec_max is not None and not pd.isna(
                                 x.spec_max) else None,
                             passed="false" if x.failed else "true", result_json=None, tested_at=iso_ts(x.tested_at)))
    keepR = set(R.out["goods_receipts"]["receipt_id"])
    for x in Q.itertuples(index=False):
        if x.receipt_id not in keepR:
            continue
        tid += 1
        val = None if x.value is None or pd.isna(x.value) else x.value
        if x.plant_id == "P2":
            unit = x.unit if isinstance(x.unit, str) else None
            res = {x.test: {"resultado": "NO CONFORME" if x.failed else "CONFORME", "valor": val, "unidad": unit,
                            "max": None if x.spec_max is None or pd.isna(x.spec_max) else x.spec_max, "obs": None}}
            rows.append(dict(test_id=tid, plant_id=x.plant_id, sample_type="MP", batch_id=None,
                             lot_code=x.internal_lot, receipt_id=int(x.receipt_id), material_code=x.rm_code,
                             test_name="PANEL MP", result=None, unit=None, spec_min=None, spec_max=None, passed=None,
                             result_json=json.dumps(res, ensure_ascii=False), tested_at=iso_ts(x.tested_at)))
            continue
        rows.append(dict(test_id=tid, plant_id=x.plant_id, sample_type="MP", batch_id=None, lot_code=x.internal_lot,
                         receipt_id=int(x.receipt_id), material_code=x.rm_code, test_name=x.test,
                         result=(num(val, 2) if val is not None else ("NO CONFORME" if x.failed else "CONFORME")),
                         unit=x.unit, spec_min=None,
                         spec_max=None if x.spec_max is None or pd.isna(x.spec_max) else num(x.spec_max, 0),
                         passed="false" if x.failed else "true", result_json=None, tested_at=iso_ts(x.tested_at)))
    # after the sesame complaints, P1 tested retained samples of the lots it had traced
    official = R.official_recall_lots
    for lot, bid in official:
        tid += 1
        t = datetime.combine(RECALL_DAY - timedelta(days=int(r.integers(1, 4))), datetime.min.time()) + timedelta(
            hours=float(r.uniform(9, 17)))
        rows.append(dict(test_id=tid, plant_id="P1", sample_type="PT", batch_id=bid, lot_code=lot, receipt_id=None,
                         material_code=None, test_name="ALERGENO SESAMO (ELISA)", result=comma(float(r.uniform(3, 40)),
                                                                                             1),
                         unit="ppm", spec_min=None, spec_max="2.5", passed="false", result_json=None,
                         tested_at=iso_ts(t)))
    df = pd.DataFrame(rows)
    R.put("quality_tests", df)
    R.reg.rule("J01", "quality_tests", "result_json", "P2 LIMS", "P2 stores each batch's (and each receipt's) "
               "results as one JSON panel per row; P1 and P3 store one row per test in result/unit/spec columns.")
    R.reg.count("J01", p2_json, len(df))
    R.reg.rule("J03", "quality_tests", "result_json", "P2 LIMS exporter (until 2023-04-01)",
               "Panels exported before the fix write NaN for a missing spec limit: not valid JSON, so casting the "
               "column to json fails until it's cleaned.")
    R.reg.count("J03", int(df["result_json"].fillna("").str.contains("NaN").sum()), len(df))
    R.reg.rule("F05", "quality_tests", "result", "P1 lab (manual entry)", "About 30% of P1 results typed with a "
                                                                          "decimal comma.")
    R.reg.count("F05", n_comma, len(df))
    R.reg.rule("B02", "quality_tests", "passed", "LIMS", "Booleans as 'true'/'false' text; NULL for P2 (inside the "
                                                         "JSON as conforme true/false, or CONFORME/NO CONFORME).")


P2_REASON = {"FALLA MECANICA": "03", "FALLA ELECTRICA": "04", "ATASCO / MICROPARADA": "07", "FALTA DE MATERIAL": "09",
             "AJUSTE DE CALIDAD": "11", "FALTA DE PERSONAL": "12", "FALLA DE SERVICIOS (VAPOR/AIRE/FRIO)": "05",
             "CAMBIO DE FORMATO": "01", "LIMPIEZA": "02"}


def build_downtime(R):
    w = R.w
    r = rng("rec_downtime")
    DT = w["downtime_true"]
    lines = w["lines"].set_index("line_id")
    emps = w["employees"]
    leads = emps[emps["role"] == "LIDER DE LINEA"].groupby("line_id")["legajo"].apply(list).to_dict()
    rows = []
    eid = 0
    logged = {"all": 0, "kept": 0}
    hidden = "L1-04"
    p2_late = (d("2024-02-01"), d("2024-03-31"))
    for x in DT.itertuples(index=False):
        plant = lines.at[x.line_id, "plant_id"]
        if plant == "P3":
            continue  # no MES at Planta Delta
        logged["all"] += 1
        if x.planned:
            p_log = 0.95 if plant == "P1" else 0.9
        elif x.minutes >= 15:
            p_log = 0.28 if x.line_id == hidden else 0.92
        else:
            p_log = 0.03 if x.line_id == hidden else 0.35
        if r.random() > p_log:
            continue
        logged["kept"] += 1
        dur = x.minutes * float(r.uniform(0.85, 1.15)) * (0.6 if x.line_id == hidden and not x.planned else 1.0)
        dur = max(5, int(round(dur / 5) * 5)) if dur >= 5 else int(round(dur))
        st = pd.Timestamp(x.start_ts).to_pydatetime()
        reason = x.reason
        if plant == "P2":
            st_out = (st + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%S")  # P2's MES server runs on UTC
            reason_out = P2_REASON.get(reason, "99")
        else:
            st_out = st.strftime("%Y-%m-%dT%H:%M:%S")
            reason_out = reason
        logged_at = st + timedelta(minutes=x.minutes + float(r.uniform(1, 60)))
        if plant == "P2" and p2_late[0] <= st.date() <= p2_late[1]:
            logged_at = datetime(2024, 6, 3, 9) + timedelta(minutes=float(r.uniform(0, 2000)))
        eid += 1
        lead = leads.get(x.line_id, [""])
        rows.append(dict(event_id=eid, plant_id=plant, line_id=x.line_id, start_time=st_out, duration_min=dur,
                         reason=reason_out, comment="" if r.random() < 0.7 else r.choice(
                             ["se destrabó", "esperando mantenimiento", "ok", "cambio de bobina", "repetido",
                              "ver OT"]), logged_by=lead[int(r.integers(len(lead)))],
                         logged_at=(logged_at + timedelta(hours=3 if plant == "P2" else 0)).strftime(
                             "%Y-%m-%dT%H:%M:%S")))
        if r.random() < 0.005:
            dup = dict(rows[-1])
            eid += 1
            dup["event_id"] = eid
            rows.append(dup)
    df = pd.DataFrame(rows)
    R.put("downtime_events", df)
    R.reg.rule("Z01", "downtime_events", "start_time, logged_at", "P2 MES", "P2's MES server runs on UTC: its "
               "timestamps are 3 hours ahead of local time, with no time zone marker. P1's are local.")
    R.reg.count("Z01", (df["plant_id"] == "P2").sum(), len(df))
    R.reg.rule("C02", "downtime_events", "reason", "P2 MES", "P2 logs numeric reason codes ('03', '07', ...) with no "
               "lookup table; P1 logs reason text.")
    R.reg.rule("G01", "downtime_events", "*", "MES operators", "Not every stop is logged: short stops mostly not, "
               "and long ones ~90% of the time. No downtime is logged at P3 (no MES).")
    R.reg.count("G01", logged["all"] - logged["kept"], logged["all"])
    R.reg.rule("D04", "downtime_events", "event_id", "MES terminals", "~0.5% of events logged twice "
                                                                       "(double-click), identical but for event_id.")


def build_maintenance(R):
    w = R.w
    WO = w["work_orders"]
    rows = []
    for x in WO.itertuples(index=False):
        if not after_go_live(x.plant_id, pd.Timestamp(x.reported_at).date()):
            continue
        open_ = pd.Timestamp(x.completed_at).date() > END - timedelta(days=3)
        rows.append(dict(wo_number=f"OT-{x.wo_id:07d}", wo_type={"CORRECTIVO": "CORR", "PREVENTIVO": "PREV",
                                                                  "MEJORA": "MEJ"}[x.kind],
                         asset_id=x.asset_id, plant_id=x.plant_id, reported_at=epoch_ms_from_local(x.reported_at),
                         started_at=epoch_ms_from_local(x.started_at),
                         completed_at=None if open_ else epoch_ms_from_local(x.completed_at),
                         downtime_minutes=int(x.tech_estimate_min) if x.kind == "CORRECTIVO" else 0,
                         description=x.description, technician_id=x.technician_id, priority=x.priority,
                         status="ABIERTA" if open_ else "CERRADA"))
    df = pd.DataFrame(rows)
    R.put("maintenance_work_orders", df)
    R.reg.rule("F06", "maintenance_work_orders", "reported_at, started_at, completed_at", "CMMS (SaaS) API export",
               "Timestamps are epoch milliseconds.")
    R.reg.count("F06", len(df), len(df))


def build_oee(R):
    """The MES's daily OEE per line and shift, computed from what operators logged."""
    w = R.w
    r = rng("rec_oee")
    DTr = R.out["downtime_events"]
    lines = w["lines"].set_index("line_id")
    B = w["batches"]
    from .facilities import shift_bounds
    S = w["staffed"]
    upc = w.story["_upc"]
    # logged minutes per (line, day, shift), back in local time
    logged = {}
    for x in DTr.itertuples(index=False):
        t = datetime.fromisoformat(x.start_time)
        if x.plant_id == "P2":
            t -= timedelta(hours=3)
        plant = x.plant_id
        first = C.SHIFTS[plant][0][1]
        day = (t - timedelta(hours=first)).date()
        for s, a, b in shift_bounds(plant, day):
            if a <= t < b:
                key = (x.line_id, day, s)
                plan = x.reason in ("CAMBIO DE FORMATO", "LIMPIEZA", "01", "02")
                cur = logged.setdefault(key, [0, 0])
                cur[0 if plan else 1] += x.duration_min
                break
    # good and rejects by the shift the batch ended in
    from .ops import shift_of
    Bk = B[B["plant_id"] != "P3"].copy()
    endt = pd.to_datetime(Bk["end_ts"])
    Bk["day"] = [(t - timedelta(hours={"P1": 6, "P2": 7}[pl])).date() for t, pl in zip(endt, Bk["plant_id"])]
    Bk["shift"] = [shift_of(pl, t.to_pydatetime()) for t, pl in zip(endt, Bk["plant_id"])]
    agg = Bk.groupby(["line_id", "day", "shift"]).agg(good=("good_units", "sum"), rej=("reject_units", "sum"),
                                                     pk=("product_key", "first")).to_dict("index")
    rows = []
    for x in S[S["plant_id"] != "P3"].itertuples(index=False):
        L = lines.loc[x.line_id]
        planned = L["shift_hours"] * 60
        plan_stop, unplanned = logged.get((x.line_id, x.day, x.shift), [0, 0])
        planned_net = max(planned - plan_stop, 1)
        a = max(0.0, (planned_net - unplanned) / planned_net)
        perf = min(1.0, L["true_performance"] * float(r.normal(1.0, 0.012)))
        g = agg.get((x.line_id, x.day, x.shift), {"good": 0, "rej": 0, "pk": None})
        good, rej = g["good"], g["rej"]
        q = good / (good + rej) if good + rej else 1.0
        if x.plant_id == "P2" and g["pk"] is not None:
            u = upc[g["pk"]]
            good, rej = round(good / u, 1), round(rej / u, 1)
        rows.append(dict(plant_id=x.plant_id, line_id=x.line_id, prod_date=iso_date(x.day), shift=x.shift,
                         planned_minutes=int(planned_net), downtime_minutes=int(unplanned),
                         run_minutes=int(planned_net - unplanned), good_qty=num(good, 1), reject_qty=num(rej, 1),
                         availability_pct=round(100 * a, 1), performance_pct=round(100 * perf, 1),
                         quality_pct=round(100 * q, 1), oee_pct=round(100 * a * perf * q, 1)))
    R.put("mes_oee_daily", pd.DataFrame(rows))


def build_timeclock(R):
    w = R.w
    r = rng("rec_timeclock")
    T = w["timeclock"]
    emps = w["employees"].set_index("employee_id")
    leg = emps["legajo"].to_dict()
    rows = []
    stats = dict(missing_out=0, manual=0, double=0)
    for x in T.itertuples(index=False):
        if not after_go_live(x.plant_id, x.day):
            continue
        dev = f"RELOJ-{x.plant_id}-{int(r.integers(1, 4))}"
        L = leg[x.employee_id]
        appr = leg.get(x.ot_approved_by) if x.ot_approved_by is not None and not pd.isna(x.ot_approved_by) else None
        ot = comma(x.ot_minutes / 60, 2) if x.ot_minutes else None
        rows.append((L, dmy_hm(x.punch_in), "E", dev, "BIOMETRICO", None, None))
        if r.random() < 0.004:
            rows.append((L, dmy_hm(pd.Timestamp(x.punch_in) + timedelta(minutes=float(r.uniform(0.2, 2)))), "E", dev,
                         "BIOMETRICO", None, None))
            stats["double"] += 1
        if r.random() < 0.012:
            stats["missing_out"] += 1
            if r.random() < 0.7:  # the supervisor keys it in later
                sup = leg.get(emps.at[x.employee_id, "supervisor_id"]) if not pd.isna(
                    emps.at[x.employee_id, "supervisor_id"]) else None
                rows.append((L, dmy_hm(x.punch_out), "S", None, "MANUAL", sup, ot))
                stats["manual"] += 1
            continue
        rows.append((L, dmy_hm(x.punch_out), "S", dev, "BIOMETRICO", appr, ot))
    df = pd.DataFrame(rows, columns=["legajo", "fecha_hora", "tipo", "reloj", "origen", "autorizado_por",
                                     "horas_extra"])
    R.put("timeclock_punches", df)
    R.reg.rule("K01", "timeclock_punches", "legajo", "Time clock vendor", "Keyed by legajo, not employee_id: equal "
               "at P1, 'S0234' at P2, 'D-0456' at P3.")
    R.reg.rule("F07", "timeclock_punches", "fecha_hora, horas_extra", "Time clock vendor",
               "'DD/MM/YYYY HH:MM' local time; overtime hours with a decimal comma.")
    R.reg.rule("G02", "timeclock_punches", "tipo", "Time clock", f"{stats['missing_out']} missing exit punches "
               f"({stats['manual']} keyed in later as MANUAL by the supervisor), {stats['double']} double entry "
               "punches: IN/OUT don't always pair.")
    R.reg.count("G02", stats["missing_out"] + stats["double"], len(df))


def build_inventory(R):
    w = R.w
    r = rng("rec_inventory")
    rs = w["rm_snapshots"]
    fs = w["fg_snapshots"]
    rm = w["raw_materials"].set_index("rm_code")
    locs = w["storage_locations"].set_index("location_id")
    rows = []
    for x in rs.itertuples(index=False):
        day = pd.Timestamp(x.snapshot_at).date()
        if not after_go_live(x.plant_id, day):
            continue
        rows.append((iso_date(day), x.plant_id, locs.at[x.location, "warehouse"], x.location, "MP", x.item, x.lot,
                     num(x.qty_book, 3), x.uom.upper()))
    for x in fs.itertuples(index=False):
        day = pd.Timestamp(x.snapshot_at).date()
        if not after_go_live(x.plant_id, day):
            continue
        rows.append((iso_date(day), x.plant_id, locs.at[x.location, "warehouse"], x.location, "PT", R.sku[x.product_key],
                     x.lot, str(int(x.units)), "UN"))
    # P3's opening balance at the go-live came without lot codes, and the bakery part was loaded twice for 3 weeks
    first = fs[(fs["plant_id"] == "P3") & (pd.to_datetime(fs["snapshot_at"]).dt.date >= P3_GO_LIVE)]
    first_day = pd.to_datetime(first["snapshot_at"]).dt.date.min()
    fam = R.p["family"]
    mig = first[pd.to_datetime(first["snapshot_at"]).dt.date == first_day].groupby("product_key")["units"].sum()
    n_mig = 0
    for wk in range(3):
        day = first_day + timedelta(days=7 * wk)
        for pk, u in mig.items():
            if fam[pk] != "BAKERY" and wk > 0:
                continue
            rows.append((iso_date(day), "P3", "ALM-D-PT", "D-PT-AMB", "PT", R.sku[pk], "MIG-2505", str(int(u)), "UN"))
            n_mig += 1
    df = pd.DataFrame(rows, columns=["snapshot_date", "plant_id", "warehouse", "location", "item_type", "item_code",
                                     "lot", "qty", "uom"])
    R.put("inventory_snapshots", df)
    R.reg.rule("V02", "inventory_snapshots", "lot, qty", "P3 migration", "P3's opening finished-goods balance was "
               "loaded under lot MIG-2505 on top of the real lots; the bakery part stayed for three weeks.")
    R.reg.count("V02", n_mig, len(df))
    R.reg.rule("Q02", "inventory_snapshots", "qty", "ERP book stock", "Quantities are book stock (receipts minus "
               "backflushed consumption, reset at counts), not physical: small negative quantities appear.")
    R.reg.count("Q02", (pd.to_numeric(df["qty"]) < 0).sum(), len(df))

    cc = w["cycle_counts"]
    emps = w["employees"]
    counters = emps[emps["role"] == "AUXILIAR DE DEPOSITO"].groupby("plant_id")["employee_id"].apply(list).to_dict()
    rows = []
    for x in cc.itertuples(index=False):
        if not after_go_live(x.plant_id, x.counted_at.date()):
            continue
        reason = "MERMA" if x.variance < -0.005 * max(abs(x.qty_book), 1) and r.random() < 0.5 else "DIF. INVENTARIO"
        if abs(x.variance) < 1e-6:
            reason = None
        c = counters[x.plant_id]
        rows.append(dict(count_date=iso_date(x.counted_at), plant_id=x.plant_id, location=x.location,
                         material_code=x.rm_code, lot=x.lot, system_qty=num(x.qty_book, 3),
                         counted_qty=num(x.qty_counted, 3), difference=num(x.variance, 3),
                         uom=rm.at[x.rm_code, "uom"].upper(), counted_by=int(c[int(r.integers(len(c)))]),
                         reason_code=reason))
    R.put("cycle_counts", pd.DataFrame(rows))


def build_waste(R):
    w = R.w
    r = rng("rec_waste")
    W = w["waste"]
    rows = []
    for x in W.itertuples(index=False):
        if not after_go_live(x.plant_id, x.day):
            continue
        sku = R.sku.get(x.product_key) if x.product_key is not None else None
        if x.plant_id == "P2":
            # uploaded weekly from an Excel sheet on a US-locale laptop
            rows.append(dict(waste_date=mdy(x.day), plant_id="P2", line_id=x.line_id, item=lstrip_zeros(sku) if sku
                             else None, lot=x.lot_code or "", quantity=num(x.kg, 2), uom="kg", reason=x.reason,
                             source="EXCEL"))
        else:
            q = f"{comma(x.kg, 1)} kg" if r.random() < 0.6 else num(x.kg, 1)
            rows.append(dict(waste_date=iso_date(x.day) if x.plant_id == "P1" else dmy(x.day), plant_id=x.plant_id,
                             line_id=x.line_id, item=sku or "VARIOS", lot=x.lot_code, quantity=q,
                             uom=None if "kg" in str(q) else "KG", reason=x.reason, source="ERP"))
    df = pd.DataFrame(rows)
    R.put("waste_log", df)
    R.reg.rule("F08", "waste_log", "waste_date, quantity, item", "Excel upload (P2) / ERP (P1, P3)",
               "P2's rows are uploaded from Excel: MM/DD/YY dates, SKU codes without leading zeros, empty-string lots. "
               "P1's quantities are often text with the unit ('12,5 kg'); P3's dates are DD/MM/YYYY.")
    R.reg.count("F08", (df["source"] == "EXCEL").sum() + df["quantity"].astype(str).str.contains("kg").sum(), len(df))


def build_energy(R):
    w = R.w
    E = w["energy_hourly"]
    rows = []
    gap = (pd.Timestamp("2024-02-12"), pd.Timestamp("2024-02-21"))
    cum = 0.0
    reset = pd.Timestamp("2025-11-03 14:00")
    for x in E.itertuples(index=False):
        if x.plant_id == "P2" and gap[0] <= x.ts < gap[1]:
            continue  # meter gateway down
        if x.plant_id == "P3":
            if x.ts.date() < P3_GO_LIVE:
                continue
            cum += x.kwh
            if x.ts == reset:
                cum = x.kwh  # the meter was replaced and starts again from zero
            rows.append((f"MED-{x.plant_id}-GENERAL", iso_ts(x.ts), num(cum, 1), "kWh", "ACUMULADA"))
        else:
            rows.append((x.meter_id, iso_ts(x.ts), num(x.kwh, 1), "kWh", "INTERVALO"))
    lines = w["lines"].set_index("line_id")
    for x in w["energy_line_daily"].itertuples(index=False):
        plant = lines.at[x.line_id, "plant_id"]
        if plant == "P3":
            continue
        rows.append((f"MED-{x.line_id}", iso_date(x.day), num(x.kwh, 1), "kWh", "DIARIA"))
    df = pd.DataFrame(rows, columns=["meter_id", "reading_ts", "value", "unit", "reading_type"])
    R.put("energy_meter_readings", df)
    R.reg.rule("E02", "energy_meter_readings", "value, reading_type", "Energy monitoring",
               "Mixed reading types in one column: hourly intervals (P1, P2), a cumulative register (P3, reset to zero "
               "when the meter was replaced on 2025-11-03) and daily line submeters; P2 has a 9-day gap in "
               "February 2024.")


def build_sensors(R):
    w = R.w
    S = w["sensor_true"]
    ny = ZoneInfo("America/New_York")
    art = ZoneInfo("America/Argentina/Buenos_Aires")
    sens = w["sensors"].set_index("sensor_id")
    out = []
    for sid, g in S.groupby("sensor_id", sort=False):
        vendor = sens.at[sid, "vendor"]
        ts = pd.DatetimeIndex(g["ts_local"])
        temp = g["temp_c"].to_numpy().copy()
        unit_id = sens.at[sid, "unit_id"]
        if unit_id == "CH-S1":
            temp = np.where(ts < pd.Timestamp("2024-05-20 08:30"), temp + 1.2, temp)  # the sensor read high
        if vendor == "IoT Gateway A":
            # the platform account was left on the vendor's default time zone
            shown = ts.tz_localize(art).tz_convert(ny).tz_localize(None)
            unit = np.full(len(ts), "C")
            vals = temp
        else:
            shown = ts
            before = ts < pd.Timestamp(P3_GO_LIVE)
            vals = np.where(before, temp * 9 / 5 + 32, temp)  # Delta Telemetría reported Fahrenheit until the go-live
            unit = np.where(before, "F", "C")
        df = pd.DataFrame({"sensor_id": sid, "reading_time": shown.strftime("%Y-%m-%d %H:%M:%S"),
                           "value": np.round(vals, 2), "unit": unit})
        out.append(df)
    df = pd.concat(out, ignore_index=True)
    dup = df.sample(frac=0.003, random_state=3)
    df = pd.concat([df, dup]).sort_values(["sensor_id", "reading_time"], kind="stable").reset_index(drop=True)
    df["value"] = df["value"].map(lambda v: num(v, 2))
    R.put("cold_storage_sensor_readings", df)
    R.reg.rule("Z02", "cold_storage_sensor_readings", "reading_time", "IoT platform (P1, P2)",
               "P1's and P2's gateways report in the platform account's time zone, left on the vendor default "
               "(America/New_York, with US daylight saving): 1-2 hours behind local time depending on the date, with "
               "a repeated hour each November and a missing hour each March. P3's vendor reports local time.")
    R.reg.count("Z02", (~df["sensor_id"].str.contains("-D")).sum(), len(df))
    R.reg.rule("U04", "cold_storage_sensor_readings", "value, unit", "P3 sensor vendor",
               "P3's sensors reported Fahrenheit until the go-live, Celsius after.")
    R.reg.rule("D05", "cold_storage_sensor_readings", "*", "IoT gateways", "0.3% of readings resent: exact duplicate "
                                                                           "rows.")
    R.reg.count("D05", len(dup), len(df))
    sensors = w["sensors"].copy()
    R.put("cold_storage_sensors", pd.DataFrame(dict(
        sensor_id=sensors["sensor_id"], unit_id=sensors["unit_id"], plant_id=sensors["plant_id"],
        vendor=sensors["vendor"], setpoint_c=sensors["setpoint_c"], alarm_high_c=sensors["alarm_high_c"],
        installed_on=sensors["installed"].map(iso_date))))
    L = w["storage_locations"]
    R.put("storage_locations", L.rename(columns={"kind": "location_type"}))


def build_ops_recorded(R):
    R.batch_renumber = {}
    build_purchasing(R)
    build_production(R)
    build_consumption(R)
    build_downtime(R)
    build_maintenance(R)
    build_oee(R)
    build_timeclock(R)
    build_inventory(R)
    build_waste(R)
    build_energy(R)
    build_sensors(R)
