"""Turns the true world into what GATR's source systems recorded: one DataFrame of text values per exported table."""

from __future__ import annotations

import time

import pandas as pd

from . import rec_master, rec_ops, rec_sales
from .mess import Registry


def measure(R):
    """Counts, on the finished tables, the rows each structural rule (a format, a convention) shows up in."""
    o = R.out
    reg = R.reg

    def n(t):
        return len(o[t])

    def s(t, c):
        return o[t][c].astype("string")

    allergen_cols = [c for c in o["raw_materials"].columns if c.startswith("contiene_")]
    p3_rows = n("p3_produccion_diaria") + n("p3_ventas") + n("p3_clientes") + n("p3_articulos")
    pre = lambda t: (pd.to_datetime(s(t, "fecha"), format="%d/%m/%Y") < "2024-03-01").sum()  # noqa: E731
    dt = o["downtime_events"].drop(columns=["event_id"])
    counts = {
        "B01": (o["raw_materials"][allergen_cols].notna().any(axis=1).sum(), n("raw_materials")),
        "B02": (o["quality_tests"]["passed"].isna().sum(), n("quality_tests")),
        "C02": ((o["downtime_events"]["plant_id"] == "P2").sum(), n("downtime_events")),
        "D04": (dt.duplicated().sum(), n("downtime_events")),
        "E02": ((o["energy_meter_readings"]["reading_type"] != "INTERVALO").sum(), n("energy_meter_readings")),
        "F01": (n("raw_materials"), n("raw_materials")),
        "F07": (n("timeclock_punches"), n("timeclock_punches")),
        "F09": (n("recalls"), n("recalls")),
        "F10": (p3_rows, p3_rows),
        "H01": (o["customer_hierarchy"]["relation_type"].isin(["CD", "GRUPO"]).sum(), n("customer_hierarchy")),
        "H02": (pre("p3_produccion_diaria") + pre("p3_ventas"), n("p3_produccion_diaria") + n("p3_ventas")),
        "J02": (n("audit_log"), n("audit_log")),
        "K01": ((~s("timeclock_punches", "legajo").str.fullmatch(r"\d+")).sum(), n("timeclock_punches")),
        "M05": (o["products"]["allergens"].notna().sum(), n("products")),
        "M07": (o["sales_orders"]["status"].isin(["INVOICED", "CANCELLED", "OPEN"]).sum(), n("sales_orders")),
        "N02": ((s("equipment", "decommission_date") == "9999-12-31").sum()
                + (s("recipes", "effective_to") == "9999-12-31").sum()
                + (s("employees", "termination_date") == "9999-12-31").sum(),
                n("equipment") + n("recipes") + n("employees")),
        "N03": ((o["customer_complaints"][["customer_id", "closed_at", "lot_code_reported"]] == "null").any(axis=1).sum(),
                n("customer_complaints")),
        "O01": ((o["batch_consumption"]["material_code"] == "RM-AGUA").sum(), n("batch_consumption")),
        "Q01": (n("batch_consumption"), n("batch_consumption")),
        "S03": (o["sales_orders"]["status"].isin(["ANUL", "CANCELLED"]).sum(), n("sales_orders")),
        "U02": ((o["goods_receipts"]["uom"] == "LB").sum() + (o["recipe_lines"]["uom"] == "g").sum(),
                n("goods_receipts") + n("recipe_lines")),
        "U03": ((o["production_batches"]["uom"] == "CJ").sum(), n("production_batches")),
        "U04": ((o["cold_storage_sensor_readings"]["unit"] == "F").sum(), n("cold_storage_sensor_readings")),
        "W01": (sum(((s(t, c).str.endswith(" ")) | (s(t, c) == s(t, c).str.upper())).sum()
                    for t, c in (("customers", "customer_name"), ("suppliers", "supplier_name"))),
                n("customers") + n("suppliers")),
        "Z03": (n("customer_complaints"), n("customer_complaints")),
    }
    for rid, (a, t) in counts.items():
        if rid in reg.rules and reg.rules[rid].total == 0:
            reg.count(rid, a, t)


def build_recorded(w, log=print):
    reg = Registry()
    R = rec_master.Rec(w, reg)
    for name, fn in [("master", rec_master.build_master_recorded), ("operations", rec_ops.build_ops_recorded),
                     ("sales", rec_sales.build_sales_recorded), ("quality", rec_ops.build_quality),
                     ("audit", rec_sales.build_audit_log), ("measure", measure)]:
        t = time.time()
        fn(R)
        log(f"  recorded {name}: {time.time() - t:.0f}s")
    return R
