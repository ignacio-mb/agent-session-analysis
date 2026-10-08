"""The recorded layer, commercial side: sales orders and lines (ERP), shipments (WMS), complaints (CRM), credit notes
and returns, recalls (a spreadsheet), Planta Delta's legacy tables, the P3 SKU crosswalk and the ERP audit log."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from .common import END, ERP_UPGRADE, P3_GO_LIVE, START, d, rng
from .master import units_per_case
from .mess import comma, dmy, excel_serial, iso_date, iso_ts, lstrip_zeros, mojibake, num, strip_accents
from .quality import LABEL_RECALL_DAY, RECALL_DAY

UPGRADE_STATUS = {"FACT": "INVOICED", "ANUL": "CANCELLED", "PEND": "OPEN"}


def build_sales(R):
    w = R.w
    r = rng("rec_sales")
    Od = w["orders"].set_index("order_id")
    OL = w["order_lines"]
    p = R.p
    cust = w["customers"].set_index("customer_id")
    p3c = w["p3_customers"]
    # the P3 migration re-created some customers that already existed in the ERP (same tax id, new id)
    shared = p3c[p3c["erp_customer_id"].isin(cust[cust["channel"] != "EX DELTA"].index)]
    redo = shared.sample(16, random_state=21)
    next_id = int(cust.index.max()) + 50
    remap = {}
    extra_customers = []
    for x in redo.itertuples(index=False):
        next_id += int(r.integers(1, 5))
        remap[int(x.erp_customer_id)] = next_id
        c = cust.loc[int(x.erp_customer_id)]
        extra_customers.append(dict(customer_id=R.cust_id(next_id), customer_name=strip_accents(c["customer_name"]).upper(),
                                    segment="DISTRIB", channel="EX DELTA", currency="ARS", tax_id=c["tax_id"],
                                    province=c["province"], city=c["city"], country="AR", sales_rep_id=c["sales_rep_id"],
                                    created_on=iso_date(P3_GO_LIVE), closed_on=None, status="ACTIVO", is_deleted="0"))
    R.out["customers"] = pd.concat([R.out["customers"], pd.DataFrame(extra_customers)], ignore_index=True)
    R.reg.rule("D06", "customers, sales_orders", "customer_id", "P3 migration (2025-05)",
               "The P3 migration created new customer ids for 16 customers that already existed in the ERP (same "
               "tax id, name upper-cased without accents); P3's orders from them go to the new id.")
    R.reg.count("D06", 16, len(R.out["customers"]))

    descr_at = {}
    for pk, x in p.iterrows():
        if x["old_description"] is not None and not pd.isna(x["old_description"]):
            descr_at[pk] = (x["renamed_on"], x["old_description"], x["description"])
    OL = OL.sort_values(["order_id", "line_no"])
    ship_units = OL["units_shipped"].to_numpy()
    OL_last_ship = OL.groupby("order_id")["ship_date"].max().to_dict()
    orders, lines = [], []
    p3_first_weeks = (P3_GO_LIVE, d("2025-05-31"))
    stats = dict(no_zero=0, p3code=0, usd_ars=0, dup_loads=0, near=0)
    p3_orders = set(Od.index[Od["p3"]])
    pk_sku = R.sku
    pk_p3 = {pk: c for pk, c in p["p3_code"].items() if isinstance(c, str)}
    pk_desc = p["description"].to_dict()
    pk_fam = p["family"].to_dict()
    pk_g = p["pack_g"].to_dict()
    rep_of = cust["sales_rep_id"].to_dict()
    cols = ["order_id", "product_key", "upc", "units", "cases", "unit_price", "discount", "currency", "ship_date",
            "line_no"]
    cur_oid = None
    for i, (oid, pk, u, units, cases, price, disc, cur0, ship_date, line_no) in enumerate(
            OL[cols].itertuples(index=False, name=None)):
        if oid != cur_oid:
            cur_oid = oid
            o = Od.loc[oid]
            day = o["order_date"]
            skip = oid in p3_orders and day < P3_GO_LIVE  # in the legacy system (p3_ventas)
            if skip:
                continue
            cid = int(o["customer_id"])
            rep = rep_of.get(cid)
            if oid in p3_orders:
                cid = remap.get(cid, cid)
            seg = o["segment"]
            last_ship = OL_last_ship[oid]
            status = "PEND" if last_ship > END - timedelta(days=3) else "FACT"
            if day >= ERP_UPGRADE:
                status = UPGRADE_STATUS[status]
            created = datetime.combine(day, datetime.min.time()) + timedelta(hours=float(r.uniform(7, 19)))
            orders.append(dict(order_number=int(oid) + 20_000_000, customer_id=R.cust_id(cid),
                               order_date=iso_date(day), order_type="EXP" if seg == "EXPORTACION" else "PED",
                               status=status, currency="USD" if seg == "EXPORTACION" else "ARS",
                               sales_rep_id=None if rep is None or pd.isna(rep) else int(rep),
                               created_at=iso_ts(created), is_deleted="0"))
            loaded_at = created + timedelta(hours=float(r.uniform(1, 26)))
        elif skip:
            continue
        sku = pk_sku[pk]
        if oid in p3_orders and p3_first_weeks[0] <= day <= p3_first_weeks[1] and pk in pk_p3 and \
                r.random() < 0.85:
            sku = pk_p3[pk]  # the first weeks, P3's sellers keyed their old codes
            stats["p3code"] += 1
        elif seg == "SUPERMERCADO" and day >= d("2023-05-01"):
            sku = lstrip_zeros(sku)  # orders from the chains' EDI integration
            stats["no_zero"] += 1
        desc = pk_desc[pk]
        if pk in descr_at and day < descr_at[pk][0]:
            desc = descr_at[pk][1]
        u = int(u)
        by_unit = seg == "FOOD SERVICE" and (pk_fam[pk] == "SAUCE_SACHET" or pk_g[pk] >= 2000)
        uom = "UN" if by_unit else "CJ"
        q_ord = units if by_unit else cases
        shipped = ship_units[i]
        q_shp = shipped if by_unit else round(shipped / u, 2)
        price = float(price)
        net = round(shipped * price * (1 - float(disc)), 2)
        cur = cur0
        if cur == "USD" and r.random() < 0.03:
            cur = "ARS"  # default currency left on the line; the amount is still dollars
            stats["usd_ars"] += 1
        row = dict(order_number=int(oid) + 20_000_000, line_no=int(line_no), sku_code=sku, item_description=desc,
                   qty_ordered=num(q_ord, 2), qty_shipped=num(q_shp, 2), uom=uom, unit_price=num(price, 4),
                   discount_pct=num(100 * float(disc), 1), net_amount=num(net, 2), currency=cur,
                   invoice_date=iso_date(ship_date) if shipped else None, _loaded_at=iso_ts(loaded_at))
        lines.append(row)
        if d("2024-07-01") <= day <= d("2024-07-21"):
            again = dict(row)
            again["_loaded_at"] = iso_ts(datetime(2024, 7, 23, 3, 15) + timedelta(seconds=int(r.integers(0, 900))))
            lines.append(again)
            stats["dup_loads"] += 1
        elif r.random() < 0.002:
            again = dict(row)
            again["discount_pct"] = num(100 * float(disc) + 5, 1)
            again["net_amount"] = num(net * (1 - 0.05), 2)
            again["_loaded_at"] = iso_ts(loaded_at + timedelta(days=int(r.integers(1, 30))))
            lines.append(again)
            stats["near"] += 1
    # duplicate orders entered twice and annulled: the header says ANUL, the lines stay as they were
    lines_of = {}
    for ln in lines:
        lines_of.setdefault(ln["order_number"], []).append(ln)
    for o in [orders[i] for i in r.choice(len(orders), int(len(orders) * 0.008), replace=False)]:
        dup = dict(o)
        dup["order_number"] = o["order_number"] + 50_000_000
        dup["status"] = "ANUL" if o["order_date"] < iso_date(ERP_UPGRADE) else "CANCELLED"
        orders.append(dup)
        for ln in lines_of.get(o["order_number"], [])[:8]:
            l2 = dict(ln)
            l2["order_number"] = dup["order_number"]
            lines.append(l2)
    # test orders
    for k in range(40):
        day = START + timedelta(days=int(r.integers(0, (END - START).days)))
        on = 19_000_000 + k
        orders.append(dict(order_number=on, customer_id="999990", order_date=iso_date(day), order_type="PED",
                           status="FACT", currency="ARS", sales_rep_id=None, created_at=iso_ts(datetime.combine(
                               day, datetime.min.time()) + timedelta(hours=11)), is_deleted="0"))
        lines.append(dict(order_number=on, line_no=1, sku_code=r.choice(["TEST-001", "ZZZ999", "010103"]),
                          item_description="PRUEBA", qty_ordered="1", qty_shipped="1", uom="UN", unit_price="1",
                          discount_pct="0", net_amount="1", currency="ARS", invoice_date=iso_date(day),
                          _loaded_at=iso_ts(datetime.combine(day, datetime.min.time()) + timedelta(hours=12))))
    R.sales_orders = orders
    R.sales_lines = lines
    R.remap_customer = remap
    R.reg.rule("K02", "sales_order_lines", "sku_code", "Chains' EDI integration (since 2023-05)",
               "Supermarket orders from the EDI integration store SKU codes as numbers: leading zeros dropped.")
    R.reg.count("K02", stats["no_zero"], len(lines))
    R.reg.rule("O02", "sales_order_lines", "sku_code", "P3 go-live (May 2025)", "For P3's orders in the first weeks "
               "after the go-live, sellers keyed P3's legacy article codes: SKUs that are not in products.")
    R.reg.count("O02", stats["p3code"], len(lines))
    R.reg.rule("D07", "sales_order_lines", "_loaded_at", "ERP -> warehouse loads", "Lines of orders from "
               "2024-07-01 to 2024-07-21 were loaded twice after the upgrade (identical but for _loaded_at); a few "
               "other lines have a near-duplicate with a changed discount.")
    R.reg.count("D07", stats["dup_loads"] + stats["near"], len(lines))
    R.reg.rule("X04", "sales_order_lines", "currency", "ERP", "3% of export lines say ARS though the amount is USD "
                                                              "(the header currency is right).")
    R.reg.count("X04", stats["usd_ars"], len(lines))
    R.reg.rule("S03", "sales_orders, sales_order_lines", "status", "ERP", "0.8% of orders were entered twice; the "
               "copy's header is ANUL/CANCELLED but its lines carry no status and look like sales.")
    R.reg.rule("M07", "sales_orders", "status", "ERP upgrade 2024-07-01", "Order status FACT/ANUL/PEND before the "
               "upgrade, INVOICED/CANCELLED/OPEN after.")


def build_shipments(R):
    w = R.w
    r = rng("rec_shipments")
    S = w["_ship_ext"]
    OL = w["order_lines"]
    Od = w["orders"].set_index("order_id")
    p3_orders = set(Od.index[Od["p3"]])
    rows = []
    missing = 0
    sid = 0
    last = None
    carriers = ["Transportes del Litoral", "Logística Pampa", "Expreso Frío Sur", "Flota propia", "TransAndina Cargas"]
    for x in S.itertuples(index=False):
        if x.order_id in p3_orders and x.ship_date < P3_GO_LIVE:
            continue
        key = (x.order_id, x.ship_date, x.warehouse)
        if key != last:
            sid += 1
            n = 0
            last = key
        n += 1
        lot = x.lot_code
        # P2's warehouse didn't capture lots for its first year; elsewhere a scan is missed now and then
        p_miss = 0.35 if x.plant_id == "P2" and x.ship_date < d("2023-07-01") else 0.03
        if r.random() < p_miss:
            lot = None
            missing += 1
        cid = int(x.customer_id)
        if x.order_id in p3_orders:
            cid = R.remap_customer.get(cid, cid)
        rows.append((sid, n, int(x.order_id) + 20_000_000, int(OL.at[x.order_line_idx, "line_no"]), str(cid),
                     lstrip_zeros(R.sku[x.product_key]), lot, int(x.units), iso_date(x.ship_date),
                     {"P1": "ALM-N-PT", "P2": "ALM-S-PT", "P3": "ALM-D-PT"}[x.plant_id],
                     carriers[sid % len(carriers)]))
    df = pd.DataFrame(rows, columns=["shipment_id", "shipment_line", "order_number", "order_line", "customer_id",
                                     "sku", "lot_code", "qty_units", "ship_date", "warehouse", "carrier"])
    R.put("shipments", df)
    R.reg.rule("L03", "shipments", "lot_code", "WMS", "Lot not captured: 35% of P2's shipments until 2023-06, ~3% "
                                                      "elsewhere.")
    R.reg.count("L03", missing, len(df))
    R.reg.rule("K03", "shipments, customer_complaints", "customer_id, sku", "WMS / CRM exports",
               "IDs exported as numbers: customer_id '412' for the ERP's '000412', SKUs without leading zeros.")
    R.reg.count("K03", len(df), len(df))


def malform(r, lot):
    u = r.random()
    if lot is None:
        return None
    if u < 0.45:
        return lot
    if u < 0.62:
        if lot.startswith("N") and len(lot) >= 9:
            return f"{lot[0]} {lot[1:6]} {lot[6:]}"
        return lot.replace("-", " ")
    if u < 0.72:
        return lot.lower()
    if u < 0.80:
        return lot[:-1]
    if u < 0.85:
        return "LOTE " + lot
    if u < 0.88:
        return lot.replace("-", "")
    return r.choice(["", "no informado", "ver foto", "s/d", "N/A", "null"])


def build_complaints(R):
    w = R.w
    r = rng("rec_complaints")
    Cm = w["complaints"]
    cust = w["customers"].set_index("customer_id")
    p = R.p
    rows = []
    stats = dict(late=0)
    for x in Cm.itertuples(index=False):
        if not (x.plant_id != "P3" or x.received >= P3_GO_LIVE):
            continue
        rec = datetime.combine(x.received, datetime.min.time()) + timedelta(hours=float(r.uniform(8, 20)))
        created = rec + timedelta(minutes=float(r.uniform(1, 120)))
        if r.random() < 0.02:
            created += timedelta(days=int(r.integers(30, 150)))
            stats["late"] += 1
        cid = x.customer_id
        name = cust.at[cid, "customer_name"] if cid in cust.index else None
        prod = p.at[x.product_key, "description"] if r.random() < 0.7 else str(r.choice([
            p.at[x.product_key, "description"].lower(), p.at[x.product_key, "description"].split(" ")[0] + " " +
            p.at[x.product_key, "description"].split(" ")[1], lstrip_zeros(R.sku[x.product_key])]))
        closed = rec + timedelta(days=int(r.integers(3, 40)))
        rows.append(dict(complaint_id=f"RCL-{x.received.year}-{x.complaint_key:05d}",
                         received_at=(rec + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                         customer_id=str(cid) if r.random() > 0.03 else "null",
                         customer_name=mojibake(name) if name else "null", product=mojibake(prod),
                         lot_code_reported=malform(r, x.lot_code), category=x.category,
                         description=mojibake(x.text), channel=r.choice(["TELEFONO", "EMAIL", "VENDEDOR", "WEB"]),
                         status="CERRADO" if closed.date() < END else "ABIERTO",
                         closed_at=(closed + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ") if closed.date() < END
                         else "null", created_at=(created + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")))
    df = pd.DataFrame(rows)
    R.put("customer_complaints", df)
    R.reg.rule("L04", "customer_complaints", "lot_code_reported", "CRM (typed by the customer or the agent)",
               "Lot codes as people type them: spaces inserted, lower case, a character missing, 'LOTE ' prefix, "
               "or nothing useful ('no informado', 'ver foto', '', 'null').")
    R.reg.count("L04", (df["lot_code_reported"].notna()).sum(), len(df))
    R.reg.rule("N03", "customer_complaints", "customer_id, closed_at, lot_code_reported", "CRM export",
               "Missing values exported as the string 'null'.")
    R.reg.count("E01", len(df), len(df))
    R.reg.count("A01", stats["late"], len(df))
    R.reg.rule("Z03", "customer_complaints", "received_at, closed_at, created_at", "CRM (SaaS)",
               "UTC with a 'Z' suffix: the only time-zone-aware timestamps in the data.")


def build_returns(R):
    """Credit notes: before the upgrade, negative lines on NC-type sales orders; after it, rows in `returns`. The
    April-June 2024 credit notes were migrated into `returns` too, so they're in both."""
    w = R.w
    r = rng("rec_returns")
    CN = w["credit_notes"]
    p = R.p
    Od = w["orders"].set_index("order_id")
    p3_orders = set(Od.index[Od["p3"]])
    ret = []
    nc_orders = []
    nc_lines = []
    both = 0
    for x in CN.itertuples(index=False):
        if x.order_id in p3_orders and x.issued < P3_GO_LIVE:
            continue
        cid = R.remap_customer.get(int(x.customer_id), int(x.customer_id)) if x.order_id in p3_orders else int(
            x.customer_id)
        number = f"NC-A-0004-{x.credit_key:08d}"
        amount = round(x.units * x.unit_price, 2)
        row = dict(credit_note_number=number, issue_date=iso_date(x.issued), customer_id=R.cust_id(cid),
                   order_number=int(x.order_id) + 20_000_000 if x.order_id is not None and r.random() > 0.4 else None,
                   sku_code=R.sku[x.product_key], lot_code=x.lot_code, qty_units=int(x.units), amount=num(amount, 2),
                   currency=x.currency, reason=x.reason, physical_return="sí" if x.physical_return else "no",
                   created_at=iso_ts(datetime.combine(x.issued, datetime.min.time()) + timedelta(hours=15)))
        in_returns = x.issued >= ERP_UPGRADE or x.issued >= d("2024-04-01")
        in_sales = x.issued < ERP_UPGRADE
        if in_returns:
            ret.append(row)
        if in_sales:
            on = 30_000_000 + int(x.credit_key)
            nc_orders.append(dict(order_number=on, customer_id=R.cust_id(cid), order_date=iso_date(x.issued),
                                  order_type="NC", status="FACT", currency=x.currency, sales_rep_id=None,
                                  created_at=row["created_at"], is_deleted="0"))
            upc = units_per_case(w, x.product_key, x.issued)
            nc_lines.append(dict(order_number=on, line_no=1, sku_code=R.sku[x.product_key],
                                 item_description=p.at[x.product_key, "description"],
                                 qty_ordered=num(-x.units / upc, 2), qty_shipped=num(-x.units / upc, 2), uom="CJ",
                                 unit_price=num(x.unit_price, 4), discount_pct="0", net_amount=num(-amount, 2),
                                 currency=x.currency, invoice_date=iso_date(x.issued), _loaded_at=row["created_at"]))
        both += in_returns and in_sales
    R.put("returns", pd.DataFrame(ret))
    R.sales_orders += nc_orders
    R.sales_lines += nc_lines
    R.reg.rule("R01", "sales_order_lines, returns", "net_amount, amount", "ERP upgrade 2024-07-01",
               "Credit notes are negative lines on order_type NC before the upgrade and rows in `returns` after it; "
               f"the April-June 2024 ones were migrated into `returns` as well ({both} credit notes are in both).")
    R.reg.count("R01", both, len(ret) + len(nc_lines))


def build_recalls(R):
    """The official recall: lots traced through P1's recorded consumption of the internal lot QA identified, and
    the customers the shipment records showed for those lots. Plus a benign labelling recall."""
    w = R.w
    ses = w.story["sesame"]
    p1_lot = next(x["internal_lot"] for x in ses["receipts"] if x["plant_id"] == "P1")
    bc = R.out["batch_consumption"]
    traced = set(bc[bc["lot"] == p1_lot]["batch_id"].astype(int))
    B = R.out["production_batches"]
    tb = B[B["batch_id"].isin(traced)]
    sh = R.out["shipments"]
    rows = []
    lots = []
    for x in tb.itertuples(index=False):
        cust = sh[sh["lot_code"] == x.lot_code]["customer_id"].nunique()
        lots.append((x.lot_code, int(x.batch_id)))
        rows.append(dict(recall_id="RET-2023-01", recall_date=dmy(RECALL_DAY), sku=x.item_code, lot_code=x.lot_code,
                         qty_produced=x.good_qty, customers_notified=int(cust),
                         reason="Presencia de sésamo no declarado (materia prima pimentón ahumado)",
                         status="CERRADO", notes=None))
    R.official_recall_lots = lots
    # benign: wrong best-before date printed on a few bread lots
    Bt = w["batches"]
    p = R.p
    lab = Bt[(Bt["line_id"] == "L1-06") & (pd.to_datetime(Bt["start_ts"]).dt.date.between(d("2025-08-04"),
                                                                                           d("2025-08-08")))]
    lab = lab[lab["product_key"].map(p["description"]).str.contains("Integral")]
    for x in lab.itertuples(index=False):
        rows.append(dict(recall_id="RET-2025-01", recall_date=dmy(LABEL_RECALL_DAY), sku=R.sku[x.product_key],
                         lot_code=x.lot_code, qty_produced=str(x.good_units), customers_notified=None,
                         reason="Error de impresión de fecha de vencimiento", status="CERRADO",
                         notes="Sin riesgo para la salud"))
    R.put("recalls", pd.DataFrame(rows))
    ses["official_lots"] = [lot for lot, _ in lots]
    ses["official_customers"] = sorted(sh[sh["lot_code"].isin(ses["official_lots"])]["customer_id"].unique().tolist())
    R.reg.rule("F09", "recalls", "recall_date, qty_produced", "QA spreadsheet", "Kept by hand: DD/MM/YYYY dates, "
               "quantities in each plant's unit.")


def build_p3_legacy(R):
    """Planta Delta's legacy system: daily production (kg for sauces and bread, units for frozen), articles,
    customers and sales, with the export's own conventions."""
    w = R.w
    r = rng("rec_p3")
    p = R.p
    daily = w["p3_daily_true"]
    rows = []
    stats = dict(kg=0, un=0, overlap=0)
    line_name = {"DEL-01": "LINEA 1", "DEL-02": "LINEA 2", "DEL-03": "LINEA 3"}
    for x in daily.itertuples(index=False):
        if x.line_id in ("DEL-01", "DEL-02") and x.day >= P3_GO_LIVE + timedelta(days=7):
            continue
        if x.line_id in ("DEL-01", "DEL-02") and x.day >= P3_GO_LIVE:
            stats["overlap"] += 1
        fam = p.at[x.product_key, "family"]
        code = p.at[x.product_key, "p3_code"]
        if not isinstance(code, str):
            code = R.sku[x.product_key]  # GATR products baked at P3 after the go-live carry their ERP code
        if fam == "FROZEN":
            # the legacy system counts pieces: an empanada pack of 12 is 12
            pieces = 12 if "x12" in p.at[x.product_key, "description"] else 1
            qty, unit = x.units * pieces, "UN"
            stats["un"] += 1
        else:
            qty, unit = x.kg, r.choice(["KG", "KG", "KG", "Kg", "kgs"])
            stats["kg"] += 1
        rows.append(dict(fecha=dmy(x.day), linea=line_name[x.line_id], turno={"M": "1", "T": "2", "N": "3"}[x.shift],
                         cod_articulo=code, cantidad=comma(qty, 1, thousands=r.random() < 0.5), unidad=unit,
                         lote=x.first_lot, observaciones=r.choice(["N/A", "-", "", "sin novedad", "N/A", None])))
    R.put("p3_produccion_diaria", pd.DataFrame(rows))
    R.reg.rule("U05", "p3_produccion_diaria", "cantidad, unidad", "P3 legacy system",
               "Sauces and bread are reported in kilograms, frozen food in pieces (an empanada pack of 12 counts 12; "
               "unit spelled KG/Kg/kgs/UN); "
               "quantities have decimal commas and sometimes dot thousands separators.")
    R.reg.count("U05", stats["kg"], len(rows))
    R.reg.rule("H02", "p3_produccion_diaria, p3_ventas", "fecha", "P3 legacy export", "The export carries history "
               "from 2023-07-01, eight months before GATR bought the plant (2024-03-01).")
    R.reg.rule("D08", "p3_produccion_diaria, production_batches", "*", "P3 go-live", "Lines 1 and 2 kept reporting "
               "in the legacy system for the first week after the go-live while the ERP also had their batches: "
               f"{stats['overlap']} day-rows counted twice. Line 3 (bread) never moved to the ERP.")
    R.reg.count("D08", stats["overlap"], len(rows))

    arts = []
    for _, x in p[p["p3_code"].notna()].iterrows():
        arts.append(dict(cod_articulo=x["p3_code"], descripcion=strip_accents(x["description"]).upper(),
                         familia={"SAUCE_JAR": "SALSAS", "FROZEN": "CONGELADOS", "BAKERY": "PANIFICADOS"}[x["family"]],
                         unidad_medida="UN" if x["family"] == "FROZEN" else "KG",
                         peso_neto=comma(x["pack_g"] / 1000, 3), activo="S" if x["sku_code"] else "N",
                         cod_erp=x["sku_code"] if x["sku_code"] and r.random() < 0.4 else None))
    for k in range(5):
        arts.append(dict(cod_articulo=f"9{k:03d}", descripcion=r.choice(["ENVASE VACIO", "INSUMO VARIOS", "PRUEBA",
                                                                         "BONIFICACION", "FLETE"]),
                         familia="VARIOS", unidad_medida="UN", peso_neto="-", activo="N", cod_erp=None))
    R.put("p3_articulos", pd.DataFrame(arts))

    pc = w["p3_customers"]
    cl = []
    for x in pc.itertuples(index=False):
        cl.append(dict(cod_cliente=x.p3_customer_id, razon_social=x.customer_name.upper() if r.random() < 0.6 else
                       x.customer_name, cuit=x.tax_id.replace("-", "") if x.tax_id else "-", localidad=x.city,
                       provincia=x.province, canal=x.channel, fecha_alta=dmy(x.created),
                       activo=r.choice(["sí", "si", "SI"]) if r.random() < 0.95 else "no"))
    R.put("p3_clientes", pd.DataFrame(cl))

    # legacy sales, up to the go-live
    def p3code(pk):
        c = p.at[pk, "p3_code"]
        return c if isinstance(c, str) and c else R.sku[pk]

    Od = w["orders"].set_index("order_id")
    OL = w["order_lines"]
    erp_to_p3 = dict(zip(pc["erp_customer_id"], pc["p3_customer_id"]))
    v = []
    n = 0
    p3l = OL[OL["p3"] & (OL["order_date"] < P3_GO_LIVE)]
    for oid, g in p3l.groupby("order_id"):
        n += 1
        o = Od.loc[oid]
        tipo = "FC A" if r.random() < 0.7 else "FC B"
        for x in g.itertuples(index=False):
            if x.units_shipped <= 0:
                continue
            code = p3code(x.product_key)
            amount = x.units_shipped * x.unit_price * (1 - x.discount)
            v.append(dict(nro_comprobante=f"0003-{n:08d}", tipo_comprobante=tipo, fecha=dmy(x.ship_date),
                          cod_cliente=erp_to_p3[int(o["customer_id"])], cod_articulo=code,
                          cantidad=comma(x.units_shipped, 0, thousands=True), unidad="UN",
                          precio_unitario=comma(x.unit_price * (1 - x.discount), 2),
                          importe=comma(amount, 2, thousands=True), moneda="$"))
    CN = w["credit_notes"]
    for x in CN[CN["order_id"].isin(p3l["order_id"].unique()) & (CN["issued"] < P3_GO_LIVE)].itertuples(index=False):
        n += 1
        code = p3code(x.product_key)
        v.append(dict(nro_comprobante=f"0003-{n:08d}", tipo_comprobante="NC A", fecha=dmy(x.issued),
                      cod_cliente=erp_to_p3[int(Od.at[x.order_id, "customer_id"])], cod_articulo=code,
                      cantidad=comma(-x.units, 0), unidad="UN", precio_unitario=comma(x.unit_price, 2),
                      importe=comma(-x.units * x.unit_price, 2, thousands=True), moneda="$"))
    R.put("p3_ventas", pd.DataFrame(v))
    R.reg.rule("F10", "p3_*", "fecha, cantidad, importe, activo", "P3 legacy export", "DD/MM/YYYY dates, decimal "
               "commas with dot thousands separators, booleans as S/N or sí/si/SI/no, NULLs as 'N/A', '-' or ''.")

    # the crosswalk a consultant built for the migration: partial, partly wrong
    cw = w["p3_crosswalk"]
    maps = []
    wrong = 0
    base = p[(p["kind"] == "BASE")]
    for x in cw.itertuples(index=False):
        u = r.random()
        if u < 0.18:
            continue  # never mapped
        target = x.sku_code
        conf = "ALTA"
        note = None
        if x.sku_code is None or (isinstance(x.sku_code, float) and pd.isna(x.sku_code)):
            same = base[base["formula_code"] == p.at[x.product_key, "formula_code"]]
            if len(same) == 0:
                continue
            target = same.iloc[0]["sku_code"]
            conf, note = "BAJA", "sin equivalente exacto"
            wrong += 1
        elif u < 0.30:
            same = base[(base["formula_code"] == p.at[x.product_key, "formula_code"])]
            if len(same):
                target = same.iloc[int(r.integers(len(same)))]["sku_code"]  # the GATR brand's SKU, maybe another pack
                conf, note = "MEDIA", "equivalente marca GATR"
                wrong += 1
        maps.append(dict(p3_code=x.p3_code, erp_sku=target, mapped_by=r.choice(["consultora", "mrodriguez"]),
                         mapped_at=iso_ts(datetime(2025, 4, 1) + timedelta(days=int(r.integers(0, 60)),
                                                                           hours=float(r.uniform(9, 18)))),
                         confidence=conf, notes=note))
    R.put("sku_mapping_p3", pd.DataFrame(maps))
    R.reg.rule("K04", "sku_mapping_p3", "erp_sku", "Migration consultants", f"Partial and partly wrong: "
               f"{len(cw) - len(maps)} of {len(cw)} P3 articles unmapped, {wrong} mapped to a GATR-brand SKU of the "
               "same formula (often another pack size) instead of the P3-brand SKU.")
    R.reg.count("K04", len(cw) - len(maps) + wrong, len(cw))


def build_audit_log(R):
    w = R.w
    r = rng("rec_audit")
    rows = []
    lid = 0

    def add(table, rid, action, user, when, before, after):
        nonlocal lid
        lid += 1
        rows.append(dict(log_id=lid, table_name=table, record_id=str(rid), action=action, changed_by=user,
                         changed_at=(when + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                         payload=json.dumps({"before": before, "after": after}, ensure_ascii=False, default=str)))

    # story 13: the food service list keyed with the wrong month, then patched
    lag = w.story["price_lag"]
    add("price_lists", lag["price_list_id"], "INSERT", "lsuarez", datetime(2023, 7, 27, 16, 40), None,
        {"segment": "FOOD SERVICE", "effective_from": excel_serial(lag["recorded"]), "currency": "ARS",
         "lines": int((R.out["price_lists"]["price_list_id"] == lag["price_list_id"]).sum())})
    add("price_lists", lag["price_list_id"], "UPDATE", "jlopez", datetime(2023, 11, 14, 10, 5),
        {"effective_to": None}, {"effective_to": excel_serial(d("2023-10-31"))})
    # recipes
    for x in w["recipes"].itertuples(index=False):
        add("recipes", x.recipe_id, "INSERT", "calidad01", datetime.combine(x.valid_from, datetime.min.time()) -
            timedelta(days=6, hours=-10), None, {"product_code": f"F-{x.formula_code}", "version": x.version,
                                                  "status": "BORRADOR"})
        add("recipes", x.recipe_id, "UPDATE", "calidad02", datetime.combine(x.valid_from, datetime.min.time()) -
            timedelta(days=2, hours=-15), {"status": "BORRADOR"}, {"status": "APROBADA"})
    # hierarchy changes in 2025
    for x in R.out["customer_hierarchy"].itertuples(index=False):
        if x.valid_from >= "2025-04-01":
            add("customer_hierarchy", x.child_customer_id, "INSERT", x.updated_by,
                datetime.fromisoformat(x.valid_from) + timedelta(hours=9), None,
                {"parent_customer_id": x.parent_customer_id, "relation_type": x.relation_type})
    # batch corrections after the fact
    B = R.out["production_batches"]
    for x in B.sample(3000, random_state=9).itertuples(index=False):
        when = datetime.fromisoformat(x.created_at) + timedelta(days=int(r.integers(0, 20)), hours=float(r.uniform(1, 9)))
        try:
            g = float(x.good_qty)
        except (TypeError, ValueError):
            continue
        add("production_batches", x.batch_id, "UPDATE", r.choice(["supervisor.n", "supervisor.s", "planif01"]), when,
            {"good_qty": round(g * float(r.uniform(0.97, 1.03)), 2)}, {"good_qty": g})
    # product renames and pack changes
    for pk, x in R.p[R.p["renamed_on"].notna()].iterrows():
        add("products", R.sku[pk], "UPDATE", "marketing01", datetime.combine(x["renamed_on"], datetime.min.time()) +
            timedelta(hours=9), {"description": x["old_description"]}, {"description": x["description"]})
    for x in w["pack_history"][w["pack_history"]["valid_to"].isna()].itertuples(index=False):
        add("products", R.sku[x.product_key], "UPDATE", "logistica01", datetime.combine(x.valid_from, datetime.min.time())
            + timedelta(hours=8), None, {"units_per_case": int(x.units_per_case)})
    # order cancellations and customer status changes
    so = pd.DataFrame(R.sales_orders)
    for x in so[so["status"].isin(["ANUL", "CANCELLED"])].itertuples(index=False):
        add("sales_orders", x.order_number, "UPDATE", "ventas0" + str(int(r.integers(1, 6))),
            datetime.fromisoformat(x.created_at) + timedelta(hours=2), {"status": "FACT"}, {"status": x.status})
    df = pd.DataFrame(rows).sort_values("changed_at").reset_index(drop=True)
    df["log_id"] = np.arange(1, len(df) + 1)
    R.put("audit_log", df)
    R.reg.rule("J02", "audit_log", "payload", "ERP audit", "Changes as JSON {before, after}; NULL values as JSON "
                                                           "null.")


def build_sales_recorded(R):
    build_sales(R)
    build_shipments(R)
    build_complaints(R)
    build_returns(R)
    so = pd.DataFrame(R.sales_orders)
    sl = pd.DataFrame(R.sales_lines)
    # orders of customers deleted in the master keep coming (soft delete not enforced)
    R.put("sales_orders", so)
    R.put("sales_order_lines", sl)
    build_recalls(R)
    build_p3_legacy(R)
