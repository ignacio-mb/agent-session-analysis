"""Writes what the generator made: the recorded tables (lab/db/gatr/*.csv.gz) with the SQL that loads them
(lab/db/gatr.sql), the true world (answer_key/true_world/), and the corruption catalogue (answer_key/corruptions.md).

Column types in the SQL are inferred the way a generic loader would: a column becomes integer, numeric, date,
timestamp or boolean only when every non-null value in the file parses as one; anything else stays text."""

from __future__ import annotations

import re
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from . import dictionary
from .mess import iso_date, iso_ts, write_csv

INT = re.compile(r"-?(0|[1-9]\d{0,17})")
NUM = re.compile(r"-?(0|[1-9]\d*)(\.\d+)?")
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
TS = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")
BOOL = {"true", "false"}

# Natural or surrogate keys declared as primary keys when the file has them unique and non-null
KEYS = {"plants": ["plant_id"], "production_lines": ["line_id"], "equipment": ["asset_id"],
        "raw_materials": ["material_code"], "suppliers": ["supplier_id"], "customers": ["customer_id"],
        "employees": ["employee_id"], "recipes": ["recipe_id"], "purchase_orders": ["po_number"],
        "goods_receipts": ["receipt_id"], "production_orders": ["order_no"], "production_batches": ["batch_id"],
        "quality_tests": ["test_id"], "maintenance_work_orders": ["wo_number"], "sales_orders": ["order_number"],
        "customer_complaints": ["complaint_id"], "downtime_events": ["event_id"], "storage_locations": ["location_id"],
        "cold_storage_sensors": ["sensor_id"], "audit_log": ["log_id"], "returns": ["credit_note_number"]}

ORDER = ["plants", "production_lines", "equipment", "storage_locations", "cold_storage_sensors", "products",
         "product_uom_conversions", "raw_materials", "recipes", "recipe_lines", "suppliers", "customers",
         "customer_hierarchy", "employees", "price_lists", "fx_rates", "purchase_orders", "purchase_order_lines",
         "goods_receipts", "production_orders", "production_batches", "batch_consumption", "quality_tests",
         "downtime_events", "mes_oee_daily", "maintenance_work_orders", "timeclock_punches", "inventory_snapshots",
         "cycle_counts", "waste_log", "sales_orders", "sales_order_lines", "shipments", "customer_complaints",
         "returns", "recalls", "cold_storage_sensor_readings", "energy_meter_readings", "p3_produccion_diaria",
         "p3_articulos", "p3_clientes", "p3_ventas", "sku_mapping_p3", "audit_log"]


def as_text(v):
    if v is None:
        return None
    if isinstance(v, float):
        if np.isnan(v):
            return None
        return repr(v) if v != int(v) or abs(v) > 1e15 else str(int(v))
    if isinstance(v, (np.integer,)):
        return str(int(v))
    if isinstance(v, (np.floating,)):
        return as_text(float(v))
    if isinstance(v, (bool, np.bool_)):
        return "true" if v else "false"
    if isinstance(v, datetime):
        return iso_ts(v)
    if isinstance(v, date):
        return iso_date(v)
    if isinstance(v, pd.Timestamp):
        return iso_ts(v)
    return str(v)


def infer(values) -> str:
    seen = [v for v in values if v is not None]
    if not seen:
        return "text"
    for typ, test in (("integer", lambda v: INT.fullmatch(v) is not None),
                      ("numeric", lambda v: NUM.fullmatch(v) is not None),
                      ("date", lambda v: DATE.fullmatch(v) is not None),
                      ("timestamp", lambda v: TS.fullmatch(v) is not None),
                      ("boolean", lambda v: v in BOOL)):
        if all(test(v) for v in seen):
            if typ == "integer" and any(abs(int(v)) > 2_000_000_000 for v in seen):
                return "bigint"
            return typ
    return "text"


def sql_literal(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def export_recorded(out: dict, data_dir: Path, sql_path: Path, log=print):
    data_dir.mkdir(parents=True, exist_ok=True)
    for f in data_dir.glob("*.csv.gz"):
        f.unlink()
    ddl = [HEAD]
    stats = {}
    for name in ORDER:
        df = out[name]
        cols = list(df.columns)
        rows = [[as_text(v) for v in row] for row in df.itertuples(index=False, name=None)]
        write_csv(data_dir / f"{name}.csv.gz", cols, rows)
        types = [infer(r[i] for r in rows) for i in range(len(cols))]
        stats[name] = dict(rows=len(rows), cols=cols, types=types,
                           nulls=[sum(1 for r in rows if r[i] is None) for i in range(len(cols))],
                           empties=[sum(1 for r in rows if r[i] == "") for i in range(len(cols))],
                           dups=len(rows) - len({tuple(r) for r in rows}))
        key = KEYS.get(name)
        pk = None
        if key:
            idx = [cols.index(k) for k in key]
            vals = [tuple(r[i] for i in idx) for r in rows]
            if all(v is not None for t in vals for v in t) and len(set(vals)) == len(vals):
                pk = key
        col_sql = ",\n  ".join(f"{c} {t}" for c, t in zip(cols, types))
        if pk:
            col_sql += f",\n  PRIMARY KEY ({', '.join(pk)})"
        ddl.append(f"CREATE TABLE {name} (\n  {col_sql}\n);")
        ddl.append(f"COPY {name} FROM PROGRAM 'gzip -dc /seed/gatr/{name}.csv.gz' (FORMAT csv, HEADER true);")
        doc = dictionary.T.get(name)
        if doc:
            ddl.append(f"COMMENT ON TABLE {name} IS {sql_literal(doc['desc'])};")
            for c in cols:
                if c in doc["cols"]:
                    ddl.append(f"COMMENT ON COLUMN {name}.{c} IS {sql_literal(doc['cols'][c])};")
        ddl.append("")
        log(f"  {name}: {len(rows):,} rows")
    sql_path.write_text("\n".join(ddl) + "\n", encoding="utf-8")
    return stats


HEAD = """-- Grupo Alimentario Tres Ríos (GATR): a synthetic food manufacturer's operational data, as its source systems export
-- it. Generated by lab/gatr/generator (do not edit by hand: regenerate). Runs once, when the image is built, in a single
-- transaction in the analytics database, into its own schema, gatr. Each table streams out of its gzipped CSV.
-- Column types are what a generic loader infers: typed only when every value parses; anything else stays text.
-- Comments come from GATR's own data dictionary (lab/gatr/docs/data_dictionary.md), as incomplete as it is.

\\set ON_ERROR_STOP on

CREATE SCHEMA gatr;
SET search_path TO gatr;
COMMENT ON SCHEMA gatr IS 'Grupo Alimentario Tres Ríos: operational data exported from its ERP, MES, LIMS, CMMS, WMS, CRM, time clock, IoT and energy systems, and from Planta Delta''s legacy system.';
"""


TRUE_TABLES = {
    "plants": "plants", "lines": "production_lines", "equipment": "equipment", "storage_units": "storage_units",
    "storage_locations": "storage_locations", "sensors": "sensors", "raw_materials": "raw_materials",
    "products": "products", "pack_history": "pack_history", "recipes": "recipes", "recipe_lines": "recipe_lines",
    "suppliers": "suppliers", "supplier_rm": "supplier_materials", "customers": "customers",
    "customer_hierarchy": "customer_hierarchy", "p3_customers": "p3_customers", "employees": "employees",
    "fx_daily": "fx_daily", "price_list_headers": "price_list_headers", "price_list_lines": "price_list_lines",
    "orders": "sales_orders", "order_lines": "sales_order_lines", "production_orders": "production_orders",
    "batches": "production_batches", "downtime_true": "downtime_events", "_ship_ext": "shipments",
    "fg_waste": "fg_expired", "receipts": "goods_receipts", "incoming_qc": "incoming_qc",
    "consumption": "batch_consumption", "po_headers": "purchase_orders", "po_lines": "purchase_order_lines",
    "batch_tests": "batch_tests", "complaints": "customer_complaints", "credit_notes": "credit_notes",
    "work_orders": "maintenance_work_orders", "staffed": "staffed_shifts", "timeclock": "timeclock_shifts",
    "energy_hourly": "energy_hourly", "energy_line_daily": "energy_line_daily",
    "sensor_true": "cold_storage_temperatures", "rm_snapshots": "rm_inventory_weekly",
    "fg_snapshots": "fg_inventory_weekly", "cycle_counts": "cycle_counts", "waste": "waste",
    "p3_crosswalk": "p3_crosswalk", "p3_daily_true": "p3_daily_production",
}


def export_true(w, true_dir: Path, log=print):
    true_dir.mkdir(parents=True, exist_ok=True)
    for f in true_dir.glob("*.csv.gz"):
        f.unlink()
    for key, name in TRUE_TABLES.items():
        df = w[key].copy()
        if key == "order_lines":
            df = df.drop(columns=["week"], errors="ignore")
        cols = [c for c in df.columns if not c.startswith("_")]
        df = df[cols]
        rows = [[as_text(v) for v in row] for row in df.itertuples(index=False, name=None)]
        write_csv(true_dir / f"{name}.csv.gz", cols, rows)
        log(f"  true {name}: {len(rows):,}")


def write_corruptions(reg, path: Path):
    lines = ["# Corruptions applied\n",
             "Every rule the generator applied to turn the true world into what the source systems recorded, with "
             "the system or process it comes from and how many rows it touched (`affected` of `total` rows of the "
             "tables it applies to, counted on the finished tables; `structural` marks a rule that is a convention of "
             "the whole scope rather than something counted per row). Rule ids are referenced from "
             "hidden_stories.md.\n",
             "| Id | Tables | Columns | Source | Rule | Affected | Total | Rate |", "|---|---|---|---|---|---:|---:|---:|"]
    for r in sorted(reg.rules.values(), key=lambda x: x.id):
        rate = f"{100 * r.affected / r.total:.2f}%" if r.total else "structural"
        lines.append(f"| {r.id} | {r.tables} | {r.columns} | {r.source} | {r.description} | {r.affected:,} | "
                     f"{r.total:,} | {rate} |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
