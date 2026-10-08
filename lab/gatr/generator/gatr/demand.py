"""Demand: price lists and every sales order line the customers placed, in the true world. Quantities are in consumer
units (cases are units / units_per_case on the order date). Shipped quantities and lots come later (ops.py)."""

from __future__ import annotations

from datetime import timedelta

import numpy as np
import pandas as pd

from . import catalog as C
from .common import DAYS, END, HOLIDAYS, P3_GO_LIVE, P3_LEGACY_HISTORY_FROM, START, d, rng, season
from .master import World, units_per_case

FAMILIES = ["CHIPS", "CRACKERS", "SAUCE_JAR", "SAUCE_SACHET", "FROZEN", "BAKERY"]
USD_PER_KG = {"CHIPS": 8.6, "CRACKERS": 4.9, "SAUCE_JAR": 3.1, "SAUCE_SACHET": 4.2, "FROZEN": 5.8, "BAKERY": 2.7}
SEGMENT_FACTOR = {"SUPERMERCADO": 1.0, "DISTRIBUIDOR": 0.87, "MAYORISTA": 0.84, "FOOD SERVICE": 0.97}
MARKETS = {"UY": "MERCOSUR", "PY": "MERCOSUR", "BR": "MERCOSUR", "ES": "MERCOSUR", "CL": "CHILE", "BO": "ANDINO",
           "US": "USA"}
# real volume trend by segment and year (1.0 = 2022)
TREND = {"SUPERMERCADO": {2022: 1.0, 2023: 1.06, 2024: 0.93, 2025: 0.95, 2026: 0.97},
         "DISTRIBUIDOR": {2022: 1.0, 2023: 1.09, 2024: 0.96, 2025: 1.0, 2026: 1.01},
         "MAYORISTA": {2022: 1.0, 2023: 1.03, 2024: 1.12, 2025: 1.08, 2026: 1.05},
         "FOOD SERVICE": {2022: 1.0, 2023: 1.04, 2024: 0.80, 2025: 0.78, 2026: 0.76},
         "EXPORTACION": {2022: 1.0, 2023: 1.12, 2024: 1.35, 2025: 1.08, 2026: 0.98}}
SEGMENT_QTY = {"SUPERMERCADO": 22.0, "DISTRIBUIDOR": 55.0, "MAYORISTA": 80.0, "FOOD SERVICE": 15.0,
               "EXPORTACION": 420.0, "P3": 25.0}
SEGMENT_LINES = {"SUPERMERCADO": 2.6, "DISTRIBUIDOR": 3.4, "MAYORISTA": 5.0, "FOOD SERVICE": 1.6,
                 "EXPORTACION": 5.0, "P3": 1.6}


def trend(seg, day):
    t = TREND.get(seg, TREND["DISTRIBUIDOR"])
    y = day.year
    a, b = t.get(y, 1.0), t.get(y + 1, t.get(y, 1.0))
    return a + (b - a) * (day.timetuple().tm_yday / 366)


def unit_price_usd(p) -> float:
    g = float(p["pack_g"])
    per_kg = USD_PER_KG[p["family"]] * (g / 250.0) ** -0.12
    if p["family"] == "SAUCE_SACHET" and g < 20:
        per_kg *= 1.6
    if str(p.get("kind")) == "PRIVATE":
        per_kg *= 0.82
    return round(per_kg * g / 1000.0, 4)


def build_prices(w: World):
    """Price lists per segment, ARS per unit, updated with inflation; exports in USD per unit twice a year. The food
    service update of August 2023 was loaded with the wrong effective date (story 13)."""
    r = rng("prices")
    p = w["products"]
    cpi = w.story["cpi"]
    fx0 = 103.0
    rows = []
    months = pd.period_range("2022-01", "2026-09", freq="M")
    lists = []
    lid = 100
    for seg in ("SUPERMERCADO", "DISTRIBUIDOR", "MAYORISTA", "FOOD SERVICE"):
        for m in months:
            step = 1 if m.year <= 2024 else 2
            if (m.month - 1) % step:
                continue
            eff = m.to_timestamp().date()
            created = eff - timedelta(days=int(r.integers(2, 9)))
            # the list follows inflation up to the previous month, with a lag
            prev = str(m - 1)
            level = cpi.get(prev, 100.0) / cpi["2021-12"]
            lid += 1
            lists.append(dict(price_list_id=lid, segment=seg, currency="ARS", valid_from=eff, created=created,
                              level=level))
    for half in pd.period_range("2022-01", "2026-09", freq="6M"):
        lid += 1
        eff = half.to_timestamp().date()
        lists.append(dict(price_list_id=lid, segment="EXPORTACION", currency="USD", valid_from=eff,
                          created=eff - timedelta(days=10), level=1.0 + 0.025 * ((eff.year - 2022) * 2 +
                                                                                 (eff.month > 6))))
    L = pd.DataFrame(lists)
    # Story 13: the food service list meant for 2023-08-01 is keyed 2023-11-01, and the September and October lists
    # were never created: food service is billed at July prices for three months of 12% monthly inflation.
    fs = L["segment"] == "FOOD SERVICE"
    aug = L.index[fs & (L["valid_from"] == d("2023-08-01"))][0]
    L.loc[aug, "valid_from_recorded"] = d("2023-11-01")
    L = L[~(fs & L["valid_from"].isin([d("2023-09-01"), d("2023-10-01")]))].copy()
    L["valid_from_effective"] = L["valid_from"]
    L.loc[aug, "valid_from_effective"] = d("2023-11-01")
    L["valid_from_recorded"] = L.get("valid_from_recorded").where(L.get("valid_from_recorded").notna(),
                                                                  L["valid_from"])
    w.story["price_lag"] = dict(price_list_id=int(L.loc[aug, "price_list_id"]), intended=d("2023-08-01"),
                                recorded=d("2023-11-01"), segment="FOOD SERVICE")
    L = L.sort_values(["segment", "valid_from_effective", "created"]).reset_index(drop=True)
    w["price_list_headers"] = L

    base = {k: unit_price_usd(x) for k, x in p.set_index("product_key").iterrows()}
    for _, h in L.iterrows():
        for pk, usd in base.items():
            if h["currency"] == "USD":
                price = round(usd * 1.25 * h["level"], 4)
            else:
                price = round(usd * fx0 * h["level"] * SEGMENT_FACTOR[h["segment"]] * float(r.uniform(0.995, 1.005)),
                              2)
            rows.append((h["price_list_id"], pk, price))
    w["price_list_lines"] = pd.DataFrame(rows, columns=["price_list_id", "product_key", "unit_price"])


class PriceBook:
    """Looks up the unit price a sales line gets: the list in effect on the order date (as the ERP applied it)."""

    def __init__(self, w: World):
        L = w["price_list_headers"]
        self.by_seg = {}
        for seg, g in L.groupby("segment"):
            g = g.sort_values(["valid_from_effective", "created"])
            self.by_seg[seg] = (np.array([np.datetime64(x) for x in g["valid_from_effective"]]),
                                g["price_list_id"].to_numpy())
        pl = w["price_list_lines"]
        self.price = dict(zip(zip(pl["price_list_id"], pl["product_key"]), pl["unit_price"]))
        # what the price would have been without story 13
        Lt = L.copy()
        Lt = Lt.sort_values(["segment", "valid_from", "created"])
        self.true_by_seg = {seg: (np.array([np.datetime64(x) for x in g["valid_from"]]), g["price_list_id"].to_numpy())
                            for seg, g in Lt.groupby("segment")}

    def list_id(self, seg, day, intended=False):
        dates, ids = (self.true_by_seg if intended else self.by_seg)[seg]
        i = np.searchsorted(dates, np.datetime64(day), side="right") - 1
        return int(ids[max(i, 0)])

    def __call__(self, seg, pk, day, intended=False):
        return self.price[(self.list_id(seg, day, intended), pk)]


def eligible(p: pd.DataFrame, seg: str, chain=None, country=None, p3=False) -> np.ndarray:
    kind = p["kind"].astype(str)
    fam = p["family"]
    g = p["pack_g"].astype(float)
    big = g >= 2000
    sachet = (fam == "SAUCE_SACHET") & (g < 20)
    if seg == "EXPORTACION":
        return ((kind == "EXPORT") & (p["export_market"] == MARKETS.get(country, "MERCOSUR"))).to_numpy()
    if p3:
        return ((kind == "P3") | ((kind == "BASE") & fam.isin(["SAUCE_JAR", "FROZEN", "BAKERY"]) & ~big)).to_numpy()
    base = kind.isin(["BASE", "LEGACY", "RECODE", "SHRINK", "REUSE"])
    if seg == "SUPERMERCADO":
        m = (base & ~big & ~sachet) | (kind.isin(["PROMO", "MULTIPACK"])) | (
            (kind == "PRIVATE") & (p["private_label_chain"] == chain))
    elif seg == "FOOD SERVICE":
        m = (base & (big | sachet | (fam.isin(["FROZEN", "BAKERY", "SAUCE_SACHET"])))) | (kind == "FOODSERVICE")
    elif seg == "MAYORISTA":
        m = base | kind.isin(["PROMO", "MULTIPACK", "FOODSERVICE"])
    else:  # DISTRIBUIDOR
        m = base | kind.isin(["PROMO", "FOODSERVICE"]) | ((kind == "P3") & (p["erp_from"].notna()))
    return m.to_numpy()


def build_p3_customers(w: World):
    """Planta Delta's own customers, from its legacy system. About a quarter of them also buy from GATR under an ERP
    customer id (same tax id)."""
    r = rng("p3_customers")
    cust = w["customers"]
    rows = []
    erp_dist = cust[cust["segment"].isin(["DISTRIBUIDOR", "MAYORISTA"]) & cust["province"].isin(
        ["Entre Ríos", "Santa Fe", "Corrientes", "Buenos Aires"])]
    shared = erp_dist.sample(70, random_state=int(r.integers(1 << 30)))
    code = 3000
    used = set(cust["customer_name"])
    for _, c in shared.iterrows():
        code += int(r.integers(1, 7))
        rows.append(dict(p3_customer_id=code, customer_name=c["customer_name"], tax_id=c["tax_id"],
                         province=c["province"], city=c["city"], channel="DISTRIBUIDOR", erp_customer_id=int(
                             c["customer_id"]), created=d("2015-01-01") + timedelta(days=int(r.integers(0, 3000))),
                         size=float(r.lognormal(0, 0.6)), order_every=int(r.choice([14, 21, 28]))))
    from .master import place
    for _ in range(330):
        code += int(r.integers(1, 7))
        prov, city = place(r, "Entre Ríos")
        while True:
            kind = r.choice(["Autoservicio", "Almacén", "Despensa", "Distribuidora", "Panadería", "Rotisería",
                             "Minimercado", "Supermercado"])
            nm = f"{kind} {C.COMPANY_WORDS_C[r.integers(len(C.COMPANY_WORDS_C))]}"
            if r.random() < 0.5:
                nm += f" de {C.FIRST[r.integers(len(C.FIRST))]} {C.LAST[r.integers(len(C.LAST))]}"
            if nm not in used:
                used.add(nm)
                break
        from .common import fake_tax_id
        rows.append(dict(p3_customer_id=code, customer_name=nm, tax_id=fake_tax_id(r, 20 if r.random() < 0.4 else 30),
                         province=prov, city=city, channel="DISTRIBUIDOR" if kind == "Distribuidora" else "MINORISTA",
                         erp_customer_id=None, created=d("2012-01-01") + timedelta(days=int(r.integers(0, 4500))),
                         size=float(r.lognormal(-0.4, 0.6)) * (3 if kind == "Distribuidora" else 1),
                         order_every=int(r.choice([14, 21, 28]))))
    df = pd.DataFrame(rows)
    # after the go-live, P3 customers without an ERP id get one
    cid = int(cust["customer_id"].max())
    new_rows = []
    for i, c in df.iterrows():
        if pd.isna(c["erp_customer_id"]):
            cid += int(r.integers(1, 4))
            df.loc[i, "erp_customer_id"] = cid
            new_rows.append(dict(customer_id=cid, customer_name=c["customer_name"], segment="DISTRIBUIDOR"
                                 if c["channel"] == "DISTRIBUIDOR" else "MAYORISTA" if r.random() < 0.1 else
                                 "DISTRIBUIDOR", channel="EX DELTA", currency="ARS", province=c["province"],
                                 city=c["city"], created=P3_GO_LIVE, closed=None, tax_id=c["tax_id"],
                                 sales_rep_id=int(cust[cust["segment"] == "DISTRIBUIDOR"]["sales_rep_id"].iloc[0]),
                                 chain=None, size=0.0, ships=False, order_every=None, country="AR"))
    w["customers"] = pd.concat([cust, pd.DataFrame(new_rows)], ignore_index=True)
    df["erp_customer_id"] = df["erp_customer_id"].astype(int)
    w["p3_customers"] = df


def build_demand(w: World):
    """Every order line, quantities in units, with the price the ERP charged. Calibrated so the plants run at a
    realistic utilisation (see ops.py)."""
    r = rng("demand")
    p = w["products"].reset_index(drop=True)
    cust = w["customers"]
    launch = np.array([np.datetime64(x) for x in p["launch_date"]])
    retire = np.array([np.datetime64(x) if x is not None and not pd.isna(x) else np.datetime64("2100-01-01")
                       for x in p["retire_date"]])
    erp_from = np.array([np.datetime64(x) if x is not None and not pd.isna(x) else np.datetime64("1900-01-01")
                         for x in p["erp_from"]])
    pop = p["popularity"].to_numpy(dtype=float)
    fam_idx = p["family"].map({f: i for i, f in enumerate(FAMILIES)}).to_numpy()
    is_p3 = (p["kind"] == "P3").to_numpy()
    seas = np.array([[season(f, x) for x in DAYS] for f in FAMILIES])  # family x day
    # promo SKUs pull extra volume while active
    promo_boost = np.where(p["promo"].to_numpy(dtype=bool), 1.6, 1.0)

    def day_i(x):
        return (x - START).days

    elig_cache = {}
    lines = []
    orders = []
    oid = 0
    stuffing = w.story.setdefault("channel_stuffing", {})
    dist = cust[cust["segment"] == "DISTRIBUIDOR"]
    # Story 14: the rep whose distributors get quarter-end pushes
    rep_counts = dist.groupby("sales_rep_id").size().sort_values(ascending=False)
    stuff_rep = int(rep_counts.index[1])
    stuff_customers = dist[dist["sales_rep_id"] == stuff_rep].nlargest(5, "size")["customer_id"].astype(int).tolist()
    stuffing.update(rep_id=stuff_rep, customers=stuff_customers, start=d("2023-03-01"), end=d("2025-12-31"))

    def quarter_end_push(day):
        if not (stuffing["start"] <= day <= stuffing["end"]):
            return False
        nxt = day + timedelta(days=8)
        return nxt.month != day.month and day.month in (3, 6, 9, 12)

    def make_orders(cid, seg, size, every, first, last, mask, country=None, p3=False):
        nonlocal oid
        if every is None or size <= 0:
            return
        t = first + timedelta(days=int(r.integers(0, every)))
        while t <= last:
            day = t
            if day.weekday() >= 5:
                day += timedelta(days=7 - day.weekday())
            if day in HOLIDAYS:
                day += timedelta(days=1)
            if day > last or day > END:
                break
            di = day_i(day)
            npd = np.datetime64(day)
            active = mask & (launch <= npd) & (retire >= npd) & (erp_from <= npd if not p3 else True)
            if p3 and day < P3_GO_LIVE:
                active &= is_p3  # before the go-live, Planta Delta's customers buy Planta Delta's range only
            idx = np.flatnonzero(active)
            if len(idx):
                wts = pop[idx] * seas[fam_idx[idx], di] * promo_boost[idx]
                k = min(len(idx), 1 + int(r.poisson(SEGMENT_LINES["P3" if p3 else seg])))
                pick = r.choice(idx, k, replace=False, p=wts / wts.sum())
                oid += 1
                tr = trend(seg if not p3 else "DISTRIBUIDOR", day)
                push = (cid in stuff_customers and quarter_end_push(day))
                for j in pick:
                    q = (SEGMENT_QTY["P3" if p3 else seg] * size * (pop[j] / 3.0) ** 0.45 * seas[fam_idx[j], di] * tr
                         * float(r.lognormal(0, 0.45)))
                    lines.append((oid, int(j), q, push))
                orders.append((oid, cid, day, seg, p3))
            t += timedelta(days=max(3, int(every * float(r.uniform(0.75, 1.3)))))
        return

    for _, c in cust.iterrows():
        if not c["ships"] or pd.isna(c["order_every"]):
            continue
        seg = c["segment"]
        key = (seg, c["chain"], c["country"])
        if key not in elig_cache:
            elig_cache[key] = eligible(p, seg, c["chain"], c["country"])
        first = max(START, c["created"])
        last = min(END, c["closed"] - timedelta(days=1)) if c["closed"] is not None and not pd.isna(c["closed"]) \
            else END
        make_orders(int(c["customer_id"]), seg, float(c["size"]), int(c["order_every"]), first, last,
                    elig_cache[key], c["country"])
        # quarter-end pushes are extra orders on top of the regular ones
        if int(c["customer_id"]) in stuff_customers:
            for q_end in pd.date_range("2023-03-31", "2025-12-31", freq="QE"):
                day = q_end.date() - timedelta(days=int(r.integers(2, 7)))
                while day.weekday() >= 5:
                    day -= timedelta(days=1)
                oid += 1
                idx = np.flatnonzero(elig_cache[key] & (launch <= np.datetime64(day)) & (retire >= np.datetime64(day)))
                wts = pop[idx]
                for j in r.choice(idx, 6, replace=False, p=wts / wts.sum()):
                    lines.append((oid, int(j), SEGMENT_QTY["DISTRIBUIDOR"] * float(c["size"]) * 2.8 *
                                  (pop[j] / 3.0) ** 0.45 * float(r.lognormal(0, 0.3)), True))
                orders.append((oid, int(c["customer_id"]), day, seg, False))

    # P3's customers: P3 products before the go-live (recorded in the legacy system), ERP afterwards
    p3c = w["p3_customers"]
    p3_mask = eligible(p, "DISTRIBUIDOR", p3=True)
    for _, c in p3c.iterrows():
        make_orders(int(c["erp_customer_id"]) if True else None, "P3", float(c["size"]), int(c["order_every"]),
                    P3_LEGACY_HISTORY_FROM, END, p3_mask, p3=True)

    Od = pd.DataFrame(orders, columns=["order_id", "customer_id", "order_date", "segment", "p3"])
    Lf = pd.DataFrame(lines, columns=["order_id", "pidx", "q", "push"])
    Lf["product_key"] = p["product_key"].to_numpy()[Lf["pidx"]]
    Lf = Lf.merge(Od[["order_id", "order_date", "segment", "customer_id", "p3"]], on="order_id")
    w["_orders_raw"] = Od
    w["_lines_raw"] = Lf
    return Od, Lf


def finalize_demand(w: World, scale_by_family: dict):
    """Applies the calibration, converts to whole cases, prices the lines."""
    r = rng("demand_finalize")
    p = w["products"].set_index("product_key")
    Od, L = w["_orders_raw"], w["_lines_raw"].copy()
    fam = p.loc[L["product_key"], "family"].to_numpy()
    L["q"] = L["q"].to_numpy() * np.array([scale_by_family[f] for f in fam])
    upc = np.array([units_per_case(w, pk, dd) for pk, dd in zip(L["product_key"], L["order_date"])])
    cases = np.maximum(1, np.round(L["q"].to_numpy())).astype(int)
    L["upc"] = upc
    L["cases"] = cases
    L["units"] = cases * upc
    pb = PriceBook(w)
    seg = L["segment"].to_numpy()
    price, price_true = [], []
    disc = {}
    for pk, sg, day, cid in zip(L["product_key"], seg, L["order_date"], L["customer_id"]):
        s = "DISTRIBUIDOR" if sg == "P3" else sg
        price.append(pb(s, pk, day))
        price_true.append(pb(s, pk, day, intended=True))
        if cid not in disc:
            disc[cid] = round(float(r.choice([0, 0, 0.02, 0.03, 0.05, 0.05, 0.08, 0.1, 0.12])), 2)
    L["unit_price"] = price
    L["unit_price_intended"] = price_true
    L["discount"] = L["customer_id"].map(disc)
    L["currency"] = np.where(seg == "EXPORTACION", "USD", "ARS")
    L = L.sort_values(["order_id", "pidx"]).reset_index(drop=True)
    L["line_no"] = L.groupby("order_id").cumcount() + 1
    w["order_lines"] = L.drop(columns=["q", "pidx"])
    w["orders"] = Od
