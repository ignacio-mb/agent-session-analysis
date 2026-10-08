"""Planta Delta (P3) in the true world: the true crosswalk from its legacy article codes to GATR's products, and its
daily production per line and product, in units and kilograms (the legacy system reports one or the other)."""

from __future__ import annotations

import pandas as pd

from .common import P3_ACQUIRED
from .master import World


def run_p3(w: World):
    p = w["products"]
    w["p3_crosswalk"] = p[p["p3_code"].notna()][["p3_code", "product_key", "sku_code", "description", "family",
                                                "pack_g", "units_per_case"]].reset_index(drop=True)
    B = w["batches"]
    b = B[B["plant_id"] == "P3"].copy()
    b["day"] = pd.to_datetime(b["start_ts"]).dt.date
    b["kg"] = b["good_units"] * b["product_key"].map(p.set_index("product_key")["pack_g"]) / 1000
    daily = b.groupby(["day", "line_id", "shift", "product_key"]).agg(units=("good_units", "sum"), kg=("kg", "sum"),
                                                                    batches=("batch_id", "count"),
                                                                    first_lot=("lot_code", "first")).reset_index()
    daily["pre_acquisition"] = daily["day"] < P3_ACQUIRED
    w["p3_daily_true"] = daily
