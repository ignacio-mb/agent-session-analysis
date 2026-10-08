"""Production and fulfilment in the true world: a weekly make-to-stock plan per line, batches scheduled in time with
their real downtime, lots released to the warehouses, and every order line shipped FIFO from those lots.

The demand from demand.py is calibrated here first, so the plants run at a realistic utilisation.
"""

from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from . import catalog as C
from .common import (
    END,
    HOLIDAYS,
    P3_GO_LIVE,
    P3_LEGACY_HISTORY_FROM,
    START,
    d,
    in_shutdown,
    rng,
)
from .demand import finalize_demand
from .master import World

RELEASE_HOURS = {"CHIPS": 4, "CRACKERS": 4, "SAUCE_JAR": 72, "SAUCE_SACHET": 72, "FROZEN": 24, "BAKERY": 2}
SAFETY = {"CHIPS": 0.25, "CRACKERS": 0.3, "SAUCE_JAR": 0.35, "SAUCE_SACHET": 0.35, "FROZEN": 0.4, "BAKERY": 0.5}
SHELF_DAYS = {"CHIPS": 180, "CRACKERS": 270, "SAUCE_JAR": 300, "SAUCE_SACHET": 270, "FROZEN": 365, "BAKERY": 12}
P2_SHARE = {"CHIPS": 0.24, "CRACKERS": 0.26, "SAUCE_JAR": 0.62, "FROZEN": 0.42}
P3_SHARE_AFTER_GO_LIVE = {"SAUCE_JAR": 0.10, "FROZEN": 0.12, "BAKERY": 0.25}
LEAD_DAYS = {"SUPERMERCADO": (1, 3), "DISTRIBUIDOR": (1, 3), "MAYORISTA": (1, 2), "FOOD SERVICE": (2, 4),
             "EXPORTACION": (12, 25), "P3": (1, 2)}

POTATO_SHORTAGE = (d("2023-02-06"), d("2023-04-30"), 0.62)  # chips output capped: drought
OIL_SHOCK = (d("2022-04-04"), d("2022-05-29"), 0.88)  # mayo-type output capped: sunflower oil shortage
OVERFILL = (d("2024-10-07"), d("2025-03-14"), 0.045)  # story 15: SUR-01 checkweigher tare wrong after replacement


def units_per_hour(line, pack_g):
    kg = max(pack_g, 5) / 1000.0
    return line["rated_kg_per_hour"] / kg * min(1.0, (pack_g / 100.0) ** 0.25)


def line_shares(w: World):
    """For each product: which lines make it, with what share, from when."""
    lines = w["lines"].set_index("line_id")
    p = w["products"]
    out = {}
    for _, x in p.iterrows():
        fam = x["family"]
        plants = C.FORMULAS[x["formula_code"]]["plants"]
        fam_lines = lines[lines["family"] == fam]
        if x["kind"] == "P3":
            out[x["product_key"]] = [(fam_lines[fam_lines["plant_id"] == "P3"].index[0], 1.0, P3_LEGACY_HISTORY_FROM)]
            continue
        p1 = fam_lines[fam_lines["plant_id"] == "P1"].index[0]
        s = [(p1, 1.0, START)]
        if "P2" in plants and fam in P2_SHARE and (fam_lines["plant_id"] == "P2").any():
            s.append((fam_lines[fam_lines["plant_id"] == "P2"].index[0], P2_SHARE[fam],
                      fam_lines[fam_lines["plant_id"] == "P2"]["start_date"].iloc[0]))
        if "P3" in plants and fam in P3_SHARE_AFTER_GO_LIVE and x["kind"] in ("BASE", "SHRINK", "RECODE"):
            line3 = fam_lines[fam_lines["plant_id"] == "P3"].index[0]
            s.append((line3, P3_SHARE_AFTER_GO_LIVE[fam], P3_GO_LIVE))
        out[x["product_key"]] = s
    return out


def share_on(shares, day):
    """Effective share of each line on a day (P2 ramps up over six months; P1 takes the rest)."""
    res = {}
    rest = 1.0
    for line, sh, start in shares[1:]:
        if day < start:
            continue
        ramp = min(1.0, (day - start).days / 180.0)
        res[line] = sh * ramp
        rest -= sh * ramp
    res[shares[0][0]] = rest
    return res


def operating_days(plant, a, b):
    out = []
    x = a
    while x <= b:
        if x.weekday() < 6 and x not in HOLIDAYS and not in_shutdown(plant, x):
            out.append(x)
        x += timedelta(days=1)
    return out


def calibrate(w: World):
    """Scales demand per family so P1 runs near 88% of its hours in the first half of 2022, and P3's own range so
    its lines run near 80% before the acquisition."""
    L = w["_lines_raw"]
    p = w["products"].set_index("product_key")
    lines = w["lines"].set_index("line_id")
    upc = p["units_per_case"]
    shares = line_shares(w)
    w.story["_shares"] = shares
    scale = {}
    h1 = L[(L["order_date"] < d("2022-07-01")) & (~L["p3"])]
    for fam in ["CHIPS", "CRACKERS", "SAUCE_JAR", "SAUCE_SACHET", "FROZEN", "BAKERY"]:
        sub = h1[p.loc[h1["product_key"], "family"].to_numpy() == fam]
        line = lines[(lines["family"] == fam) & (lines["plant_id"] == "P1")].iloc[0]
        hours = 0.0
        for pk, q in sub.groupby("product_key")["q"].sum().items():
            units = q * upc[pk]
            hours += units / (units_per_hour(line, p.loc[pk, "pack_g"]) * 0.9 * line["true_availability"]
                              * line["true_performance"] * C.FAMILY_YIELD[fam])
        avail = len(operating_days("P1", START, d("2022-06-30"))) * line["shifts_per_day"] * line["shift_hours"]
        scale[fam] = 0.74 * avail / hours if hours else 1.0
    # P3: its own range, before the acquisition
    p3 = L[(L["p3"]) & (L["order_date"] < d("2024-03-01"))]
    scale_p3 = {}
    for fam in ["SAUCE_JAR", "FROZEN", "BAKERY"]:
        sub = p3[p.loc[p3["product_key"], "family"].to_numpy() == fam]
        line = lines[(lines["family"] == fam) & (lines["plant_id"] == "P3")].iloc[0]
        hours = sum(q * upc[pk] / (units_per_hour(line, p.loc[pk, "pack_g"]) * 0.9 * line["true_availability"]
                                   * line["true_performance"] * C.FAMILY_YIELD[fam])
                    for pk, q in sub.groupby("product_key")["q"].sum().items())
        avail = len(operating_days("P3", P3_LEGACY_HISTORY_FROM, d("2024-02-29"))) * 3 * 8
        scale_p3[fam] = 0.8 * avail / hours if hours else 1.0
    w.story["demand_scale"] = dict(erp=scale, p3=scale_p3)
    # apply: P3 rows by the P3 scale
    fam = p.loc[L["product_key"], "family"].to_numpy()
    mult = np.array([scale_p3.get(f, scale[f]) if p3_ else scale[f] for f, p3_ in zip(fam, L["p3"])])
    w["_lines_raw"]["q"] = L["q"].to_numpy() * mult
    finalize_demand(w, {f: 1.0 for f in scale})


# ---- Downtime model ----------------------------------------------------------------------------------------------------

UNPLANNED = [("FALLA MECANICA", 0.30, 75), ("FALLA ELECTRICA", 0.12, 60), ("ATASCO / MICROPARADA", 0.33, 9),
             ("FALTA DE MATERIAL", 0.10, 40), ("AJUSTE DE CALIDAD", 0.08, 25), ("FALTA DE PERSONAL", 0.04, 50),
             ("FALLA DE SERVICIOS (VAPOR/AIRE/FRIO)", 0.03, 70)]


def draw_downtime(r, run_minutes, availability):
    """Unplanned stops inside a batch: total expected = run * (1/A - 1)."""
    target = run_minutes * (1 / availability - 1)
    events = []
    total = 0.0
    weights = np.array([u[1] for u in UNPLANNED])
    weights = weights / weights.sum()
    mean_dur = sum(u[1] * u[2] for u in UNPLANNED) / sum(u[1] for u in UNPLANNED)
    n = r.poisson(max(target / mean_dur, 0.01))
    for _ in range(n):
        k = int(r.choice(len(UNPLANNED), p=weights))
        reason, _, mu = UNPLANNED[k]
        dur = float(r.lognormal(np.log(mu), 0.6))
        dur = max(2.0, min(dur, 600.0))
        events.append((reason, dur))
        total += dur
    return events, total


# ---- The simulation ----------------------------------------------------------------------------------------------------

class Lot:
    __slots__ = ("lot", "product_key", "qty", "released", "produced", "warehouse", "location", "batch_id", "plant")

    def __init__(self, **k):
        for a, b in k.items():
            setattr(self, a, b)


def lot_code(plant, line_id, start: datetime, seq: int):
    if plant == "P1":
        return f"N{start:%y}{start.timetuple().tm_yday:03d}{line_id[-2:]}{chr(65 + seq)}"
    if plant == "P2":
        return f"S-{start:%y%m%d}-{line_id[-1]}-{seq + 1:02d}"
    return f"D{start:%y%m%d}-{line_id[-1]}-{seq + 1:02d}"


def simulate(w: World):
    r = rng("ops")
    lines = w["lines"].set_index("line_id")
    p = w["products"].set_index("product_key")
    recipes = w["recipes"]
    shares = w.story["_shares"]
    OL = w["order_lines"]

    # ship dates
    lead = np.array([r.integers(*LEAD_DAYS[s]) for s in OL["segment"]])
    ship = pd.to_datetime(OL["order_date"]) + pd.to_timedelta(lead + 1, unit="D")
    ship = ship.where(ship.dt.weekday < 6, ship + pd.to_timedelta(7 - ship.dt.weekday, unit="D"))
    OL["ship_date"] = ship.dt.date
    OL["ship_date"] = OL["ship_date"].where(OL["ship_date"] <= END, END)
    OL["units_shipped"] = 0

    # demand per product per week (ship week)
    OL["week"] = pd.to_datetime(OL["ship_date"]).dt.to_period("W-SUN").dt.start_time.dt.date
    weekly = OL.groupby(["product_key", "week"])["units"].sum()
    weeks = [x.date() for x in pd.date_range(START - timedelta(days=START.weekday()), END, freq="7D")]
    by_week = defaultdict(dict)
    for (pk, wk), u in weekly.items():
        by_week[wk][pk] = u

    # recipe in force per formula
    rec = {}
    for f, g in recipes.groupby("formula_code"):
        rec[f] = [(row["valid_from"], row["valid_to"], int(row["recipe_id"])) for _, row in
                  g.sort_values("valid_from").iterrows()]

    def recipe_on(formula, day):
        for a, b, rid in rec[formula]:
            if a <= day and (b is None or pd.isna(b) or day <= b):
                return rid
        return rec[formula][-1][2]

    stock = defaultdict(deque)  # product -> deque of Lot (FIFO by release)
    pending = []  # lots not yet released: (released, Lot)
    carry = defaultdict(float)  # (line, product) fractional batches
    batches, downtime, orders_po, shipments, waste = [], [], [], [], []
    batch_id = 3_100_000
    po_id = 700_000
    line_clock = {lid: datetime.combine(max(START, lines.loc[lid, "start_date"]), datetime.min.time())
                  for lid in lines.index}
    line_last_sku = {}
    lot_seq = defaultdict(int)
    p3_pre = P3_LEGACY_HISTORY_FROM

    # orders grouped by ship date
    OL_sorted = OL.sort_values(["ship_date", "order_id", "line_no"])
    by_ship = {k: g.index.to_numpy() for k, g in OL_sorted.groupby("ship_date")}
    units_arr = OL["units"].to_numpy()
    prod_arr = OL["product_key"].to_numpy()
    shipped_arr = np.zeros(len(OL), dtype=np.int64)
    order_arr = OL["order_id"].to_numpy()

    fam_of = p["family"].to_dict()

    def carry_hours(line_id):
        L_ = lines.loc[line_id]
        return sum(v for (l_, _), v in carry.items() if l_ == line_id) * L_["batch_hours"] / (
            L_["true_availability"] * L_["true_performance"])
    ry = rng("product_yield")
    prod_y = {pk: float(ry.normal(0, 0.01)) for pk in p.index}  # each product's own yield, the same in every plant

    pending_by_pk = defaultdict(float)

    for wk in weeks:
        wk_end = wk + timedelta(days=6)
        demand_now = by_week.get(wk, {})
        demand_next = by_week.get(wk + timedelta(days=7), {})
        demand_next2 = by_week.get(wk + timedelta(days=14), {})
        demand_next3 = by_week.get(wk + timedelta(days=21), {})
        # 1. plan: units per product for this week
        # make this week what next week ships (bakery: this week's, it keeps a few days), plus a safety margin
        plan = {}
        for pk in sorted(set(demand_now) | set(demand_next)):
            fam = fam_of[pk]
            on_hand = sum(lt.qty for lt in stock[pk]) + pending_by_pk[pk]
            if fam == "BAKERY":
                need = demand_now.get(pk, 0) + SAFETY[fam] * demand_next.get(pk, 0) - on_hand
            else:  # ahead of a seasonal peak, build stock early
                ahead = max(demand_next.get(pk, 0), (demand_next.get(pk, 0) + demand_next2.get(pk, 0) +
                                                     demand_next3.get(pk, 0)) / 3)
                need = demand_now.get(pk, 0) + (1 + SAFETY[fam]) * ahead - on_hand
            if need > 0:
                plan[pk] = need
        # 2. per line: batches
        per_line = defaultdict(list)
        for pk, need in plan.items():
            for line_id, sh in share_on(shares[pk], wk).items():
                if sh <= 0 or lines.loc[line_id, "start_date"] > wk_end:
                    continue
                if lines.loc[line_id, "plant_id"] == "P3" and wk_end < p3_pre:
                    continue
                per_line[line_id].append((pk, need * sh))
        for line_id, items in per_line.items():
            L = lines.loc[line_id]
            plant = L["plant_id"]
            fam = L["family"]
            # the line's time window this week
            days = operating_days(plant, max(wk, L["start_date"], START), min(wk_end, END))
            if not days:
                continue
            # a busy week runs on Sunday too
            need_h = sum(units / (units_per_hour(L, p.loc[pk, "pack_g"]) * 0.9) for pk, units in items) / (
                L["true_availability"] * L["true_performance"]) + carry_hours(line_id)
            if fam == "BAKERY" or need_h > 0.92 * len(days) * L["shifts_per_day"] * L["shift_hours"]:
                sunday = wk + timedelta(days=6)
                if START <= sunday <= END and sunday not in HOLIDAYS and not in_shutdown(plant, sunday) and \
                        sunday >= L["start_date"]:
                    days = days + [sunday]
            first_shift_hour = C.SHIFTS[plant][0][1]
            windows = []
            for day in days:
                st = datetime.combine(day, datetime.min.time()) + timedelta(hours=int(first_shift_hour))
                windows.append((st, st + timedelta(hours=int(L["shifts_per_day"] * L["shift_hours"]))))
            # potato / oil shortages cap what can be made
            cap = 1.0
            if fam == "CHIPS" and POTATO_SHORTAGE[0] <= wk <= POTATO_SHORTAGE[1]:
                cap = POTATO_SHORTAGE[2]
            jobs = []
            for pk, units in sorted(items, key=lambda x: (("sésamo" in str(p.loc[x[0], "allergens"])),
                                                          -x[1])):
                upb = units_per_hour(L, p.loc[pk, "pack_g"]) * L["batch_hours"] * 0.9
                c = carry[(line_id, pk)] + units * cap / upb
                if fam in ("SAUCE_JAR", "SAUCE_SACHET") and C.BASE_OF[p.loc[pk, "formula_code"]] == "MAYO" and \
                        OIL_SHOCK[0] <= wk <= OIL_SHOCK[1]:
                    c = carry[(line_id, pk)] + units * OIL_SHOCK[2] / upb
                n = int(c)
                rem = c - n
                sizes = [upb] * n
                if rem >= 0.2 or (n == 0 and rem > 0.05 and units > 0):
                    sizes.append(max(rem, 0.12) * upb)  # a short run for what's left: slow movers
                    rem = 0.0
                carry[(line_id, pk)] = rem
                if sizes:
                    jobs.append((pk, sizes, upb))
            if not jobs:
                continue
            if fam == "BAKERY":  # bread is baked every day: spread each product's batches over the week
                flat = [(pk, [sz], upb) for pk, sizes, upb in jobs for sz in sizes]
                order = sorted(range(len(flat)), key=lambda i: (sum(1 for j in range(i) if flat[j][0] == flat[i][0]),
                                                                i))
                jobs = [flat[i] for i in order]
            # 3. schedule the batches through the windows
            wi_ = 0
            t = max(windows[0][0], line_clock[line_id])
            po_rows = []
            for pk, sizes, upb in jobs:
                po_id += 1
                po_start = None
                made = 0
                n = len(sizes)
                for size in sizes:
                    # find a window
                    while wi_ < len(windows) and t >= windows[wi_][1]:
                        wi_ += 1
                        if wi_ < len(windows):
                            t = max(t, windows[wi_][0])
                    if wi_ >= len(windows):
                        break
                    if t < windows[wi_][0]:
                        t = windows[wi_][0]
                    if line_last_sku.get(line_id) != pk:
                        dur = float(r.uniform(20, 45)) + (40 if "sésamo" in str(p.loc[line_last_sku.get(line_id),
                                                                                      "allergens"] if line_last_sku.get(
                            line_id) in p.index else "") else 0)
                        downtime.append((line_id, None, t, dur, "CAMBIO DE FORMATO", True))
                        t += timedelta(minutes=dur)
                        line_last_sku[line_id] = pk
                    ideal = L["batch_hours"] * 0.9 * 60 * size / upb
                    run = ideal / (L["true_performance"] * float(r.uniform(0.97, 1.03)))
                    avail = L["true_availability"]
                    evs, dt_total = draw_downtime(r, run, avail)
                    start = t
                    end = start + timedelta(minutes=run + dt_total)
                    batch_id += 1
                    # downtime events inside the batch, spread in its time
                    offs = sorted(r.uniform(0, run, len(evs)))
                    acc = 0.0
                    for (reason, dur), off in zip(evs, offs):
                        downtime.append((line_id, batch_id, start + timedelta(minutes=off + acc), dur, reason, False))
                        acc += dur
                    fam_y = C.FAMILY_YIELD[fam] + C.PLANT_YIELD_GAP[plant] + prod_y[pk]
                    y = fam_y + float(r.normal(0, 0.006))
                    if line_id == "SUR-01" and OVERFILL[0] <= start.date() <= OVERFILL[1]:
                        y -= OVERFILL[2]
                    y = min(0.999, max(0.55, y))
                    planned = int(round(size))
                    good = int(round(planned * y))
                    rejects = int(round((planned - good) * 0.4))
                    day = start.date()
                    seq = lot_seq[(line_id, day)]
                    lot_seq[(line_id, day)] += 1
                    shift = shift_of(plant, start)
                    lot = lot_code(plant, line_id, start, seq)
                    released = end + timedelta(hours=RELEASE_HOURS[fam])
                    if fam == "FROZEN":
                        if plant == "P1":
                            loc = "FRZ-N1" if r.random() < 0.5 else "FRZ-N2"
                        else:
                            loc = "FRZ-S1" if plant == "P2" else "FRZ-D1"
                    else:
                        loc = {"P1": "N", "P2": "S", "P3": "D"}[plant] + "-PT-AMB"
                    batches.append(dict(batch_id=batch_id, line_id=line_id, plant_id=plant, product_key=pk,
                                        formula_code=p.loc[pk, "formula_code"],
                                        recipe_id=recipe_on(p.loc[pk, "formula_code"], day),
                                        production_order_id=po_id, lot_code=lot, shift=shift, start_ts=start,
                                        end_ts=end, run_minutes=run, downtime_minutes=dt_total,
                                        planned_units=planned, good_units=good, reject_units=rejects,
                                        yield_pct=good / planned, location=loc, released_at=released))
                    lt = Lot(lot=lot, product_key=pk, qty=good, released=released, produced=start, warehouse=loc,
                             location=loc, batch_id=batch_id, plant=plant)
                    pending.append((released, lt))
                    pending_by_pk[pk] += good
                    po_start = po_start or start
                    made += 1
                    t = end
                    # daily sanitation after the last batch of the day is in the gap between windows
                if made:
                    po_rows.append(dict(production_order_id=po_id, line_id=line_id, plant_id=plant, product_key=pk,
                                        recipe_id=recipe_on(p.loc[pk, "formula_code"], wk), planned_batches=n,
                                        planned_units=int(round(sum(sizes))), made_batches=made, week=wk,
                                        sched_start=po_start, sched_end=t))
                if made < n:
                    carry[(line_id, pk)] += sum(sizes[made:]) / upb  # what didn't fit this week
            line_clock[line_id] = t
            orders_po.extend(po_rows)

        # 4. release lots and ship, day by day
        for di in range(7):
            day = wk + timedelta(days=di)
            if day > END:
                break
            cutoff = datetime.combine(day, datetime.min.time()) + timedelta(hours=8)
            still = []
            for rel, lt in pending:
                if rel <= cutoff:
                    stock[lt.product_key].append(lt)
                    pending_by_pk[lt.product_key] -= lt.qty
                else:
                    still.append((rel, lt))
            pending = still
            for i in by_ship.get(day, ()):
                pk = prod_arr[i]
                need = units_arr[i]
                q = stock[pk]
                while need > 0 and q:
                    lt = q[0]
                    take = min(need, lt.qty)
                    shipments.append((int(order_arr[i]), int(i), lt.lot, lt.batch_id, pk, int(take), day,
                                      lt.warehouse, lt.plant))
                    lt.qty -= take
                    need -= take
                    if lt.qty <= 0:
                        q.popleft()
                shipped_arr[i] = units_arr[i] - need
            # expire old stock
            if di == 6:
                for pk, q in stock.items():
                    fam = fam_of[pk]
                    limit = SHELF_DAYS[fam] * (0.75 if fam == "BAKERY" else 0.6)
                    while q and (day - q[0].produced.date()).days > limit:
                        lt = q.popleft()
                        waste.append(("VENCIMIENTO", lt.lot, lt.batch_id, pk, lt.qty, day, lt.warehouse, lt.plant))
    OL["units_shipped"] = shipped_arr
    w["order_lines"] = OL
    B = pd.DataFrame(batches)
    w["batches"] = B
    w["downtime_true"] = pd.DataFrame(downtime, columns=["line_id", "batch_id", "start_ts", "minutes", "reason",
                                                         "planned"])
    w["production_orders"] = pd.DataFrame(orders_po)
    w["shipments_true"] = pd.DataFrame(shipments, columns=["order_id", "order_line_idx", "lot_code", "batch_id",
                                                           "product_key", "units", "ship_date", "warehouse", "plant_id"])
    w["fg_waste"] = pd.DataFrame(waste, columns=["reason", "lot_code", "batch_id", "product_key", "units", "day",
                                                 "location", "plant_id"])
    # stock left at the end
    left = [(lt.lot, lt.batch_id, lt.product_key, lt.qty, lt.location) for q in stock.values() for lt in q if lt.qty]
    left += [(lt.lot, lt.batch_id, lt.product_key, lt.qty, lt.location) for _, lt in pending]
    w["fg_end_stock"] = pd.DataFrame(left, columns=["lot_code", "batch_id", "product_key", "units", "location"])


def shift_of(plant, t: datetime):
    hours = [(s, h) for s, h in C.SHIFTS[plant]]
    length = 24 // len(hours)
    for s, h in hours:
        if (t.hour - h) % 24 < length:
            return s
    return hours[0][0]


def run_ops(w: World):
    calibrate(w)
    simulate(w)
