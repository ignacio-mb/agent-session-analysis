"""Inventory in the true world and in the books: weekly snapshots by lot and location, cycle counts of raw materials,
and the waste log.

The ERP's book quantity of a raw-material lot is its recorded receipt minus its backflushed (standard) consumption,
reset to the physical count at every cycle count. Physical stock is the true receipt minus the true consumption. For
most lots the two drift a little either way; for the oil one clerk receives at P1, the book is always 3-6% ahead, and
every count writes the difference off (story 5)."""

from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from .common import END, P2_OPEN, P3_LEGACY_HISTORY_FROM, START, d, rng
from .master import World

A_ITEMS = {"RM-ACGI", "RM-PAPA", "RM-HT00", "RM-ACOL"}
# bulk liquids live in tanks: their lots can't be closed one by one, so a book residual waits for the next count
BULK = {"RM-ACGI", "RM-ACOL", "RM-VINA"}


def sundays():
    x = START + timedelta(days=(6 - START.weekday()) % 7)
    out = []
    while x <= END:
        out.append(datetime.combine(x, datetime.max.time()).replace(microsecond=0))
        x += timedelta(days=7)
    return out


def count_dates():
    """Last Friday of every month (monthly counts) and of each quarter's last month (everything)."""
    out = []
    for m in pd.period_range(START, END, freq="M"):
        last = m.to_timestamp(how="end").date()
        while last.weekday() != 4:
            last -= timedelta(days=1)
        out.append((datetime.combine(last, datetime.min.time()) + timedelta(hours=16), m.month in (3, 6, 9, 12)))
    return out


def rm_location(plant, storage, kind):
    w = {"P1": "N", "P2": "S", "P3": "D"}[plant]
    if kind == "ENVASE":
        return f"{w}-EMB"
    if storage == "REFRIGERADO":
        return {"P1": "CH-N1", "P2": "CH-S1", "P3": "CH-D1"}[plant]
    if storage == "CONGELADO":
        return {"P1": "FRZ-N1", "P2": "FRZ-S1", "P3": "FRZ-D1"}[plant]
    return f"{w}-MP-AMB"


def build_rm_inventory(w: World):
    r = rng("cycle_counts")
    R = w["receipts"]
    C = w["consumption"]
    op = w["opening_rm"]
    rm = w["raw_materials"].set_index("rm_code")
    plant_from = {"P1": START, "P2": P2_OPEN - timedelta(days=10), "P3": P3_LEGACY_HISTORY_FROM - timedelta(days=10)}
    ev = []
    for x in op.itertuples(index=False):
        t = datetime.combine(plant_from[x.plant_id], datetime.min.time())
        ev.append((f"INV-INICIAL-{x.plant_id}", x.plant_id, x.rm_code, t, x.qty, x.qty))
    ok = R[R["qc_disposition"] != "RECHAZADO"]
    for x in ok.itertuples(index=False):
        t = datetime.combine(x.receipt_date, datetime.min.time()) + timedelta(hours=18)
        ev.append((x.internal_lot, x.plant_id, x.rm_code, t, x.qty, x.qty_recorded))
    E = pd.DataFrame(ev, columns=["lot", "plant_id", "rm_code", "t", "dphys", "dbook"])
    c = C[C["internal_lot"].notna()]
    E2 = pd.DataFrame({"lot": c["internal_lot"].to_numpy(), "plant_id": c["plant_id"].to_numpy(),
                       "rm_code": c["rm_code"].to_numpy(), "t": pd.to_datetime(c["at"]).to_numpy(),
                       "dphys": -c["qty"].to_numpy(), "dbook": -c["qty_std"].to_numpy()})
    E = pd.concat([E, E2], ignore_index=True)
    E["t"] = pd.to_datetime(E["t"])
    # opening lots are per (plant, rm): make their id unique per material
    is_open = E["lot"].str.startswith("INV-INICIAL")
    E.loc[is_open, "lot"] = E.loc[is_open, "lot"] + "-" + E.loc[is_open, "rm_code"]
    E = E.sort_values(["lot", "t"]).reset_index(drop=True)

    snaps = np.array(sundays(), dtype="datetime64[s]")
    counts = count_dates()
    cnt_t = np.array([c_[0] for c_ in counts], dtype="datetime64[s]")
    cnt_q = np.array([c_[1] for c_ in counts])

    snap_rows, count_rows = [], []
    for lot, g in E.groupby("lot", sort=False):
        plant, rmc = g["plant_id"].iloc[0], g["rm_code"].iloc[0]
        info = rm.loc[rmc]
        loc = rm_location(plant, info["storage"], info["kind"])
        t = g["t"].to_numpy().astype("datetime64[s]")
        cp = np.cumsum(g["dphys"].to_numpy())
        cb = np.cumsum(g["dbook"].to_numpy())
        monthly = rmc in A_ITEMS
        # counts that apply to this lot: from its first event, while it has book or physical stock
        applicable = (cnt_t >= t[0]) & (cnt_q | monthly)
        cidx = np.flatnonzero(applicable)
        resets = []  # (time, counted, book_raw_at_time)
        last_event = t[-1]
        bulk = rmc in BULK
        for k in cidx:
            ct = cnt_t[k]
            if not bulk and ct > last_event:
                break  # the lot was closed when it ran out: the ERP zeroes its book
            i = np.searchsorted(t, ct, side="right") - 1
            phys = cp[i] if i >= 0 else 0.0
            braw = cb[i] if i >= 0 else 0.0
            book = braw + (resets[-1][1] - resets[-1][2] if resets else 0.0)
            if abs(book) < 1e-6 and phys < 1e-6:
                if ct > last_event:
                    break
                continue
            noise = 0.0 if info["uom"] == "un" else float(r.normal(0, 0.0015)) * phys
            counted = max(0.0, round(phys + noise, 3))
            count_rows.append((ct, plant, loc, rmc, lot, round(book, 3), counted, round(counted - book, 3),
                               round(phys, 3)))
            resets.append((ct, counted, braw))
            if ct > last_event and phys < 1e-6:
                break
        # weekly snapshots while there is book or physical stock
        end = max(last_event, resets[-1][0] if resets else last_event)
        sidx = np.flatnonzero((snaps >= t[0]) & (snaps <= end + np.timedelta64(7, "D")))
        for k in sidx:
            st = snaps[k]
            i = np.searchsorted(t, st, side="right") - 1
            phys = cp[i] if i >= 0 else 0.0
            braw = cb[i] if i >= 0 else 0.0
            adj = 0.0
            for rt, cnt, br in resets:
                if rt <= st:
                    adj = cnt - br
            book = braw + adj
            if not bulk and st > last_event and phys <= 1e-6:
                break
            if phys > 1e-6 or abs(book) > 1e-6:
                snap_rows.append((st, plant, loc, rmc, lot, round(phys, 3), round(book, 3), info["uom"]))
    w["rm_snapshots"] = pd.DataFrame(snap_rows, columns=["snapshot_at", "plant_id", "location", "item", "lot",
                                                         "qty_physical", "qty_book", "uom"])
    w["cycle_counts"] = pd.DataFrame(count_rows, columns=["counted_at", "plant_id", "location", "rm_code", "lot",
                                                          "qty_book", "qty_counted", "variance", "qty_physical"])


def build_fg_inventory(w: World):
    B = w["batches"]
    S = w["shipments_true"]
    W = w["fg_waste"]
    ev = [pd.DataFrame({"lot": B["lot_code"], "t": pd.to_datetime(B["end_ts"]), "d": B["good_units"],
                        "loc": B["location"], "pk": B["product_key"], "plant": B["plant_id"]}),
          pd.DataFrame({"lot": S["lot_code"], "t": pd.to_datetime(S["ship_date"]) + pd.Timedelta(hours=12),
                        "d": -S["units"], "loc": None, "pk": S["product_key"], "plant": S["plant_id"]}),
          pd.DataFrame({"lot": W["lot_code"], "t": pd.to_datetime(W["day"]) + pd.Timedelta(hours=20),
                        "d": -W["units"], "loc": None, "pk": W["product_key"], "plant": W["plant_id"]})]
    E = pd.concat(ev, ignore_index=True).sort_values(["lot", "t"])
    E["loc"] = E.groupby("lot")["loc"].transform("first")
    E["cum"] = E.groupby("lot")["d"].cumsum()
    snaps = pd.Series(sundays(), name="snapshot_at")
    # for each lot: first event to last event, the Sundays between
    sn = snaps.to_numpy().astype("datetime64[s]")
    rows = []
    for lot, g in E.groupby("lot", sort=False):
        t = g["t"].to_numpy().astype("datetime64[s]")
        cum = g["cum"].to_numpy()
        a = np.searchsorted(sn, t[0], side="left")
        b = np.searchsorted(sn, t[-1], side="left")
        for k in range(a, b + 1 if b < len(sn) else b):
            if k >= len(sn):
                break
            i = np.searchsorted(t, sn[k], side="right") - 1
            q = cum[i] if i >= 0 else 0
            if q > 0:
                rows.append((sn[k], g["plant"].iloc[0], g["loc"].iloc[0], g["pk"].iloc[0], lot, int(q)))
    w["fg_snapshots"] = pd.DataFrame(rows, columns=["snapshot_at", "plant_id", "location", "product_key", "lot",
                                                    "units"])


def build_waste(w: World):
    """Everything thrown away: process scrap per batch (rejects), expired finished goods, and a packaging trial."""
    r = rng("waste")
    B = w["batches"]
    p = w["products"].set_index("product_key")
    rows = []
    for b in B.itertuples(index=False):
        if b.reject_units <= 0:
            continue
        kg = b.reject_units * p.at[b.product_key, "pack_g"] / 1000
        rows.append(dict(day=pd.Timestamp(b.end_ts).date(), plant_id=b.plant_id, line_id=b.line_id,
                         batch_id=b.batch_id, product_key=b.product_key, lot_code=b.lot_code, kg=round(kg, 2),
                         units=int(b.reject_units), reason="SCRAP DE PROCESO" if r.random() < 0.8 else
                         "ARRANQUE DE LINEA"))
    for x in w["fg_waste"].itertuples(index=False):
        rows.append(dict(day=x.day, plant_id=x.plant_id, line_id=None, batch_id=x.batch_id, product_key=x.product_key,
                         lot_code=x.lot_code, kg=round(x.units * p.at[x.product_key, "pack_g"] / 1000, 2),
                         units=int(x.units), reason="VENCIMIENTO"))
    # red herring: new jar trial on L1-03, March 2024 (planned, documented in the reason)
    day = d("2024-03-04")
    while day <= d("2024-03-28"):
        if day.weekday() < 5:
            for _ in range(int(r.integers(2, 5))):
                rows.append(dict(day=day, plant_id="P1", line_id="L1-03", batch_id=None, product_key=None,
                                 lot_code=None, kg=round(float(r.uniform(180, 650)), 1), units=None,
                                 reason="PRUEBA ENVASE NUEVO"))
        day += timedelta(days=1)
    w["waste"] = pd.DataFrame(rows)


def run_inventory(w: World):
    build_rm_inventory(w)
    build_fg_inventory(w)
    build_waste(w)
