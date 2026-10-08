"""Raw materials in the true world: what each batch consumed, the receipts that kept every plant supplied, the
purchase orders behind them, incoming quality control, and which raw-material lot went into which batch (FIFO).

Stories planted here: the sesame-contaminated paprika lot (1), the spice supplier that changes owners (2) and the
oil receipts inflated by one clerk (5)."""

from __future__ import annotations

from collections import defaultdict, deque
from datetime import date, datetime, timedelta

import pandas as pd

from .common import END, HOLIDAYS, P2_OPEN, P3_LEGACY_HISTORY_FROM, START, d, rng
from .master import World

SESAME_DAY = d("2023-09-12")  # the contaminated paprika lot arrives at P1; two days later at P2
SESAME_SUPPLIER_LOT = "MEC-23-0457"
MICRO_COUNTED = {"ESPECIAS", "SEMILLAS", "HARINAS", "FRUTOS SECOS"}


def packaging_for(prod) -> list[tuple[str, float]]:
    """Primary packaging per unit of a product: (rm_code, qty per unit in its uom)."""
    fam, g = prod["family"], float(prod["pack_g"])
    if fam == "CHIPS":
        return [("PK-FMET", (3 + 0.03 * g) / 1000)]
    if fam == "CRACKERS":
        return [("PK-FBOP", (2 + 0.025 * g) / 1000)]
    if fam == "SAUCE_SACHET":
        return [("PK-FSAC", 0.0007 if g < 20 else 0.0)] + ([("PK-DOYP", 1.0)] if g >= 20 else [])
    if fam == "SAUCE_JAR":
        if g >= 2000:
            return [("PK-BALD", 1.0)]
        if g <= 200:
            return [("PK-FR190", 1.0), ("PK-TA58", 1.0)]
        if g <= 260:
            return [("PK-FR250", 1.0), ("PK-TA63", 1.0)]
        if g <= 360:
            return [("PK-FR340", 1.0), ("PK-TA63", 1.0)]
        if g <= 600:
            return [("PK-FR500", 1.0), ("PK-TA82", 1.0)]
        return [("PK-FR1K", 1.0), ("PK-TA82", 1.0)]
    if fam == "FROZEN":
        tray = "PK-BALU" if prod["formula_code"] in ("LAS-BOL", "CAN-VER", "PAS-PAP", "ARR-POL", "GUI-LEN",
                                                    "LOC-CRI") else "PK-BPET"
        return [(tray, 1.0), ("PK-FTER", 0.012)]
    return [("PK-BLAC", 1.0)]


def supplier_lot_format(r, supplier_id):
    k = supplier_id % 5
    pref = "".join(chr(65 + int(x)) for x in r.integers(0, 26, 3))
    return k, pref


def make_supplier_lot(k, pref, day: date, n: int) -> str:
    if k == 0:
        return f"{pref}-{day:%y}-{n:04d}"
    if k == 1:
        return f"L{day:%y%m%d}{chr(65 + n % 26)}"
    if k == 2:
        return f"{n:06d}"
    if k == 3:
        return f"{day:%y}{day.timetuple().tm_yday:03d}-{n % 100:02d}"
    return f"{pref}{day:%m%y}/{n:03d}"


def build_consumption(w: World):
    """Ingredient and primary packaging consumption per batch, before lots are assigned."""
    r = rng("consumption")
    B = w["batches"]
    p = w["products"].set_index("product_key")
    RL = w["recipe_lines"]
    rl = {rid: list(zip(g["rm_code"], g["kg_per_kg"])) for rid, g in RL.groupby("recipe_id")}
    pack = {pk: packaging_for(x) for pk, x in p.iterrows()}
    rows = []
    for b in B.itertuples(index=False):
        kg = b.planned_units * p.at[b.product_key, "pack_g"] / 1000.0
        for rm, q in rl[b.recipe_id]:
            std = kg * q
            rows.append((b.batch_id, b.plant_id, rm, std * float(r.normal(1.0, 0.012)), std, b.start_ts))
        for rm, q in pack[b.product_key]:
            if q > 0:
                std = b.planned_units * q * 1.02
                rows.append((b.batch_id, b.plant_id, rm, std * float(r.normal(1.0, 0.006)), std, b.start_ts))
    c = pd.DataFrame(rows, columns=["batch_id", "plant_id", "rm_code", "qty", "qty_std", "at"])
    c["qty"] = c["qty"].round(3)
    c["qty_std"] = c["qty_std"].round(3)
    w["consumption_raw"] = c


def build_supply(w: World):
    r = rng("supply")
    rm = w["raw_materials"].set_index("rm_code")
    sup = w["suppliers"].set_index("supplier_id")
    srm = w["supplier_rm"]
    cons = w["consumption_raw"]
    cons = cons[cons["rm_code"] != "RM-AGUA"]
    cons = cons.assign(day=pd.to_datetime(cons["at"]).dt.date)
    daily = cons.groupby(["plant_id", "rm_code", "day"])["qty"].sum()
    fx = w["fx_daily"].set_index("rate_date")["ars_per_usd"]
    story5 = w.story["receiving_fraud"]
    own = w.story["ownership_change"]
    sesame = w.story["sesame"]
    emps = w["employees"]
    clerks = {pl: emps[(emps["plant_id"] == pl) & (emps["role"] == "RECEPCIONISTA DE MATERIALES")]
              for pl in ("P1", "P2", "P3")}

    lot_fmt = {sid: supplier_lot_format(r, int(sid)) for sid in sup.index}
    # each supplier's typical microbial load (log10 UFC/g) on dry goods; the spice house drifts after its new owner
    rq = rng("supplier_micro")
    micro_base = {int(sid): float(rq.uniform(1.65, 2.5)) for sid in sup.index}
    micro_base[own["old_id"]] = micro_base[own["new_id"]] = 1.72

    def micro_mu(sid, day):
        if sid in (own["old_id"], own["new_id"]) and day >= own["change"]:
            return 1.72 + 0.032 * (day - own["change"]).days / 30.4
        return micro_base[sid]
    lot_counter = defaultdict(int)
    receipts = []
    qc = []
    rcpt_id = 900_000
    seq = defaultdict(int)
    plant_start = {"P1": START, "P2": P2_OPEN - timedelta(days=10), "P3": P3_LEGACY_HISTORY_FROM - timedelta(days=10)}
    opening = []
    sesame_done = {}

    def clerk_on(plant, day):
        c = clerks[plant]
        c = c[(c["hire_date"] <= day) & (c["termination_date"].isna() | (c["termination_date"] >= day))]
        if c.empty:
            return None
        if plant == "P1":
            fraud_clerk = story5["clerk_id"]
            if (c["employee_id"] == fraud_clerk).any() and day.weekday() < 5 and r.random() < 0.85:
                return int(fraud_clerk)
            others = c[c["employee_id"] != fraud_clerk]
            if not others.empty:
                return int(others.iloc[r.integers(len(others))]["employee_id"])
        return int(c.iloc[r.integers(len(c))]["employee_id"])

    def internal_lot(plant, day):
        seq[(plant, day.year)] += 1
        n = seq[(plant, day.year)]
        if plant == "P1":
            return f"MP-N-{day:%y}{n:05d}"
        if plant == "P2":
            return f"MPS{day:%y}{n:05d}"
        return f"MPD-{day:%y}-{n:04d}"

    def supplier_of(rm_code, plant, day):
        cands = srm[srm["rm_code"] == rm_code]
        ids = cands["supplier_id"].to_numpy()
        sh = cands["share"].to_numpy()
        # the spice supplier's new legal entity takes over its id after the ownership change
        sid = int(ids[r.choice(len(ids), p=sh / sh.sum())])
        if sid == own["old_id"] and day >= own["change"]:
            sid = own["new_id"]
        if rm_code == "RM-PIMA" and plant in ("P1", "P2") and d("2023-08-28") <= day <= d("2023-09-29"):
            sid = sesame["supplier_id"]
        return sid

    def fail_prob(sid, rm_code, day):
        cat = rm.at[rm_code, "category"]
        base = {"ESPECIAS": 0.025, "CARNES": 0.03, "LACTEOS": 0.025, "VEGETALES": 0.03, "TUBERCULOS": 0.04,
                "PACKAGING": 0.012}.get(cat, 0.015)
        return base

    test_plan = {"ESPECIAS": ["MICROBIOLOGIA", "HUMEDAD"], "SEMILLAS": ["MICROBIOLOGIA", "HUMEDAD"],
                 "HARINAS": ["MICROBIOLOGIA", "HUMEDAD"], "LACTEOS": ["MICROBIOLOGIA", "TEMPERATURA RECEPCION"],
                 "CARNES": ["MICROBIOLOGIA", "TEMPERATURA RECEPCION"], "HUEVO": ["MICROBIOLOGIA",
                                                                                   "TEMPERATURA RECEPCION"],
                 "PESCADOS": ["MICROBIOLOGIA", "TEMPERATURA RECEPCION"], "VEGETALES": ["INSPECCION VISUAL"],
                 "TUBERCULOS": ["MATERIA SECA", "INSPECCION VISUAL"], "ACEITES": ["ACIDEZ", "PEROXIDOS"],
                 "PACKAGING": ["DIMENSIONAL"], "FRUTOS SECOS": ["AFLATOXINAS", "HUMEDAD"],
                 "AROMAS": ["CERTIFICADO DE ANALISIS"], "ADITIVOS": ["CERTIFICADO DE ANALISIS"]}

    for (plant, rm_code), series in daily.groupby(level=[0, 1]):
        series = series.droplevel([0, 1])
        info = rm.loc[rm_code]
        every = max(1, int(info["order_every_days"]))
        s = pd.Series(0.0, index=pd.date_range(plant_start[plant], END + timedelta(days=40)).date)
        s.loc[series.index] = series.values
        vals = s.to_numpy()
        days = s.index
        avg = max(vals[vals > 0].mean() if (vals > 0).any() else 0.0, 1e-6)
        safety = avg * (4 + every * 0.6)
        # opening stock: a lot received before the plant's data starts
        open_qty = float(vals[:every + 3].sum() + safety)
        opening.append(dict(plant_id=plant, rm_code=rm_code, qty=round(open_qty, 3), day=days[0]))
        inv = open_qty
        i0 = int(r.integers(0, every))
        receipt_days = list(range(i0, len(days) - 40, every))
        prev = 0
        for k, i in enumerate(receipt_days):
            day = days[i]
            nxt = receipt_days[k + 1] if k + 1 < len(receipt_days) else i + every
            inv -= vals[prev:i].sum()  # used since the last receipt day
            prev = i
            need = vals[i:nxt].sum() + safety - inv
            if need <= avg * 0.3:
                continue
            day_r = day
            while day_r.weekday() >= 5 or day_r in HOLIDAYS:
                day_r -= timedelta(days=1)
            if day_r > END:
                break
            qty = need * float(r.uniform(0.98, 1.08))
            sid = supplier_of(rm_code, plant, day_r)
            k_, pref = lot_fmt[sid]
            lot_counter[sid] += int(r.integers(1, 9))
            slot = make_supplier_lot(k_, pref, day_r, lot_counter[sid])
            if rm_code == "RM-PIMA" and plant in ("P1", "P2") and d("2023-08-28") <= day_r <= d("2023-09-29"):
                # the contaminated supplier lot: P1's first receipt from 2023-09-08, P2's first from 2023-09-11
                if day_r >= d("2023-09-08" if plant == "P1" else "2023-09-11") and not sesame_done.get(plant):
                    slot = SESAME_SUPPLIER_LOT
                    sesame_done[plant] = True
            rcpt_id += 1
            clerk = clerk_on(plant, day_r)
            recorded = qty
            fraud = (plant == story5["plant"] and rm_code == story5["rm_code"] and sid == story5["supplier_id"]
                     and clerk == story5["clerk_id"] and story5["start"] <= day_r)
            if fraud:
                recorded = qty * float(r.uniform(1.03, 1.06))
            cat = info["category"] if info["kind"] != "ENVASE" else "PACKAGING"
            micro = None
            if cat in MICRO_COUNTED:
                micro = 10 ** float(r.normal(micro_mu(sid, day_r), 0.33))
                failed = micro > 1000
            else:
                failed = r.random() < fail_prob(sid, rm_code, day_r)
            disposition = "APROBADO"
            if failed:
                disposition = "RECHAZADO" if r.random() < 0.35 else "APROBADO CON DESVIO"
            unit_cost = float(info["std_cost_usd"])
            if rm_code in ("RM-ACGI", "RM-ACOL") and d("2022-03-07") <= day_r <= d("2022-08-31"):
                unit_cost *= 1.45  # sunflower oil price shock
            if rm_code == "RM-PAPA" and d("2023-01-15") <= day_r <= d("2023-05-15"):
                unit_cost *= 1.8  # drought
            currency = "USD" if sup.at[sid, "country"] != "AR" else "ARS"
            price = unit_cost * float(r.uniform(0.95, 1.05)) * (1 if currency == "USD" else
                                                                 float(fx.get(day_r, fx.asof(day_r)
                                                                              if hasattr(fx, "asof") else 1)))
            lot = internal_lot(plant, day_r)
            receipts.append(dict(receipt_id=rcpt_id, plant_id=plant, rm_code=rm_code, supplier_id=sid,
                                 receipt_date=day_r, qty=round(qty, 3), qty_recorded=round(recorded, 3),
                                 uom=info["uom"], internal_lot=lot, supplier_lot=slot, received_by=clerk,
                                 unit_price=round(price, 4), currency=currency, qc_disposition=disposition,
                                 fraud=fraud, sesame=(slot == SESAME_SUPPLIER_LOT)))
            for t in test_plan.get(cat, ["INSPECCION VISUAL"]):
                value, unit, smax = None, None, None
                if t == "MICROBIOLOGIA" and micro is not None:
                    value, unit, smax = round(micro), "UFC/g", 1000.0
                elif t == "HUMEDAD":
                    value, unit, smax = round(float(r.normal(8.5, 0.8)), 2), "%", 11.0
                qc.append(dict(receipt_id=rcpt_id, internal_lot=lot, plant_id=plant, rm_code=rm_code,
                               supplier_id=sid, test=t, tested_at=datetime.combine(day_r, datetime.min.time()) +
                               timedelta(hours=float(r.uniform(9, 17))),
                               failed=(failed and t == "MICROBIOLOGIA") if micro is not None else
                               (failed and t == test_plan.get(cat, ["INSPECCION VISUAL"])[0]),
                               disposition=disposition, value=value, unit=unit, spec_max=smax))
            if disposition == "RECHAZADO":
                # replaced by the supplier a few days later
                rcpt_id += 1
                day2 = day_r + timedelta(days=int(r.integers(1, 4)))
                lot_counter[sid] += 1
                lot2 = internal_lot(plant, day2)
                receipts.append(dict(receipt_id=rcpt_id, plant_id=plant, rm_code=rm_code, supplier_id=sid,
                                     receipt_date=day2, qty=round(qty, 3), qty_recorded=round(qty, 3),
                                     uom=info["uom"], internal_lot=lot2,
                                     supplier_lot=make_supplier_lot(k_, pref, day2, lot_counter[sid]),
                                     received_by=clerk_on(plant, day2), unit_price=round(price, 4),
                                     currency=currency, qc_disposition="APROBADO", fraud=False, sesame=False,
                                     replaces=rcpt_id - 1))
                for t in test_plan.get(cat, ["INSPECCION VISUAL"]):
                    qc.append(dict(receipt_id=rcpt_id, internal_lot=lot2, plant_id=plant, rm_code=rm_code,
                                   supplier_id=sid, test=t, tested_at=datetime.combine(day2, datetime.min.time()) +
                                   timedelta(hours=float(r.uniform(9, 17))), failed=False, disposition="APROBADO",
                                   value=round(10 ** float(r.normal(micro_mu(sid, day2) - 0.4, 0.2))) if
                                   t == "MICROBIOLOGIA" and micro is not None else None,
                                   unit="UFC/g" if t == "MICROBIOLOGIA" and micro is not None else None,
                                   spec_max=1000.0 if t == "MICROBIOLOGIA" and micro is not None else None))
            inv += qty
    R = pd.DataFrame(receipts)
    w["receipts"] = R
    w["incoming_qc"] = pd.DataFrame(qc)
    w["opening_rm"] = pd.DataFrame(opening)
    w.story["sesame"].update(day=SESAME_DAY, supplier_lot=SESAME_SUPPLIER_LOT,
                             receipts=R[R["sesame"]][["receipt_id", "plant_id", "internal_lot",
                                                      "receipt_date"]].to_dict("records"))


def assign_lots(w: World):
    """FIFO: each batch's consumption drawn from the plant's oldest released lots of that raw material."""
    R = w["receipts"]
    cons = w["consumption_raw"]
    op = w["opening_rm"]
    queues = defaultdict(deque)
    for x in op.itertuples(index=False):
        queues[(x.plant_id, x.rm_code)].append([f"INV-INICIAL-{x.plant_id}", x.qty, None])
    # lots in receipt order; rejected lots never enter stock
    Rok = R[R["qc_disposition"] != "RECHAZADO"].sort_values(["receipt_date", "receipt_id"])
    events = []
    for x in Rok.itertuples(index=False):
        avail = datetime.combine(x.receipt_date, datetime.min.time()) + timedelta(hours=18)
        events.append((avail, 0, (x.plant_id, x.rm_code), x.internal_lot, x.qty, x.receipt_id))
    c = cons.sort_values(["at", "batch_id"])
    for x in c.itertuples(index=False):
        events.append((x.at, 1, (x.plant_id, x.rm_code), x.batch_id, x.qty, x.qty_std / x.qty if x.qty else 1.0))
    events.sort(key=lambda e: (e[0], e[1]))
    out = []
    short = 0.0
    for at, kind, key, ref, qty, rid in events:
        q = queues[key]
        if kind == 0:
            q.append([ref, qty, rid])
            continue
        if key[1] == "RM-AGUA":
            out.append((ref, key[0], key[1], None, qty, qty * rid, at))
            continue
        need = qty
        while need > 1e-9:
            if not q:
                # ran dry: consumed from a lot the sim didn't plan (rounding); charge it to the last lot
                out.append((ref, key[0], key[1], None, need, need * rid, at))
                short += need
                break
            lot = q[0]
            take = min(need, lot[1])
            out.append((ref, key[0], key[1], lot[0], take, take * rid, at))
            lot[1] -= take
            need -= take
            if lot[1] <= 1e-9:
                q.popleft()
    w["consumption"] = pd.DataFrame(out, columns=["batch_id", "plant_id", "rm_code", "internal_lot", "qty", "qty_std",
                                                  "at"])
    w["consumption"]["qty"] = w["consumption"]["qty"].round(3)
    w["consumption"]["qty_std"] = w["consumption"]["qty_std"].round(3)
    w.story["_rm_shortfall"] = short
    left = [(k[0], k[1], lot[0], lot[1]) for k, q in queues.items() for lot in q if lot[1] > 1e-6]
    w["rm_end_stock"] = pd.DataFrame(left, columns=["plant_id", "rm_code", "internal_lot", "qty"])


def build_purchase_orders(w: World):
    """Purchase orders behind the receipts: one order per supplier, plant and order date; some lines delivered in two
    receipts, a few cancelled, plus maintenance (MRO) purchases with no lots."""
    r = rng("purchase_orders")
    R = w["receipts"]
    rm = w["raw_materials"].set_index("rm_code")
    sup = w["suppliers"]
    emps = w["employees"]
    buyers = emps[emps["role"] == "COMPRADOR"]["employee_id"].astype(int).tolist()
    R = R.copy()
    R["lead"] = R["rm_code"].map(rm["lead_days"]).fillna(5).astype(int)
    R["order_date"] = [x - timedelta(days=int(ld) + int(r.integers(0, 3))) for x, ld in zip(R["receipt_date"],
                                                                                         R["lead"])]
    R.loc[R.get("replaces").notna() if "replaces" in R else [], "order_date"] = None
    heads, lines = [], []
    po = 4_500_000
    rec_po = {}
    base = R[R["replaces"].isna()] if "replaces" in R else R
    for (sid, plant, od), g in base.groupby(["supplier_id", "plant_id", "order_date"]):
        po += 1
        buyer = buyers[int(r.integers(len(buyers)))]
        heads.append(dict(po_id=po, supplier_id=int(sid), plant_id=plant, order_date=od, buyer_id=buyer,
                          currency=g["currency"].iloc[0], kind="MP"))
        for j, x in enumerate(g.itertuples(index=False)):
            ordered = x.qty * float(r.uniform(0.95, 1.05))
            lines.append(dict(po_id=po, line_no=j + 1, rm_code=x.rm_code, qty_ordered=round(ordered, 2), uom=x.uom,
                              unit_price=x.unit_price, promised_date=x.receipt_date, cancelled=False))
            rec_po[x.receipt_id] = (po, j + 1)
        # a cancelled line now and then
        if r.random() < 0.025:
            m = g.iloc[0]
            lines.append(dict(po_id=po, line_no=len(g) + 1, rm_code=m["rm_code"], qty_ordered=round(m["qty"] * 0.5, 2),
                              uom=m["uom"], unit_price=m["unit_price"], promised_date=m["receipt_date"],
                              cancelled=True))
    # replacements belong to the original's PO line
    if "replaces" in R:
        for x in R[R["replaces"].notna()].itertuples(index=False):
            rec_po[x.receipt_id] = rec_po.get(int(x.replaces), (None, None))
    R["po_id"] = R["receipt_id"].map(lambda i: rec_po.get(i, (None, None))[0])
    R["po_line"] = R["receipt_id"].map(lambda i: rec_po.get(i, (None, None))[1])
    # MRO purchases: spare parts and services, no lots
    mro = sup[sup["category"] == "MRO"]["supplier_id"].astype(int).tolist()
    items = ["Rodamiento 6205-2RS", "Correa dentada", "Sello mecánico", "Resistencia calefactora", "Contactor 25A",
             "Sensor inductivo", "Válvula solenoide", "Cuchilla rebanadora", "Teflón para mordaza selladora",
             "Aceite grado alimenticio", "Servicio de calibración", "Detergente alcalino CIP", "Guantes nitrilo",
             "Filtro de aire comprimido", "Mano de obra contratista"]
    for day in pd.date_range(START, END, freq="D"):
        for plant in ("P1", "P2", "P3"):
            if (plant == "P2" and day.date() < P2_OPEN) or (plant == "P3" and day.date() < d("2025-05-05")):
                continue
            if day.weekday() >= 5 or r.random() > 0.55:
                continue
            po += 1
            heads.append(dict(po_id=po, supplier_id=mro[int(r.integers(len(mro)))], plant_id=plant,
                              order_date=day.date(), buyer_id=buyers[int(r.integers(len(buyers)))], currency="ARS",
                              kind="MRO"))
            for j in range(int(r.integers(1, 4))):
                lines.append(dict(po_id=po, line_no=j + 1, rm_code=None, item_text=items[int(r.integers(len(items)))],
                                  qty_ordered=float(r.integers(1, 20)), uom="UN",
                                  unit_price=round(float(r.lognormal(3.5, 1.0)) *
                                                   float(w["fx_daily"].set_index("rate_date")["ars_per_usd"].get(
                                                       day.date(), 100)), 2), promised_date=day.date() +
                                  timedelta(days=int(r.integers(3, 20))), cancelled=bool(r.random() < 0.04)))
    w["po_headers"] = pd.DataFrame(heads)
    w["po_lines"] = pd.DataFrame(lines)
    w["receipts"] = R


def run_supply(w: World):
    build_consumption(w)
    build_supply(w)
    assign_lots(w)
    build_purchase_orders(w)
