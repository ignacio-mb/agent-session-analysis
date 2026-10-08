"""The "official" data dictionary a new hire at GATR would be handed: written by IT in early 2023, touched up since,
never finished. It is deliberately incomplete (nothing about Planta Delta's tables, the audit log or the crosswalk),
partly outdated (status codes from before the 2024 ERP upgrade, credit notes as sales orders) and wrong about a unit
or two. It feeds docs/data_dictionary.md and the table and column comments in Postgres (what Metabase shows).

Nothing here describes the planted stories."""

HEADER = """# GATR data dictionary

**Grupo Alimentario Tres Ríos - Systems & Data**
Version 2.4 (last full review: March 2023; minor edits since).

This document describes the tables exported nightly from GATR's source systems into the analytics database
(schema `gatr`). Each table is a direct export from its system of record; nothing is cleaned or transformed on the
way. If something here disagrees with the data, the data wins: tell the Systems team so we can update this page.

## Source systems

| System | What it holds | Notes |
|---|---|---|
| ERP | Master data, purchasing, production orders and batches, sales, inventory | In use at Planta Norte since March 2019 (data before that was migrated from the old system) and at Planta Sur since it opened in July 2022. |
| MES | Operator terminals on the production lines: downtime and the daily OEE report | Planta Norte and Planta Sur. |
| LIMS | Quality tests on finished-product batches and incoming materials | |
| CMMS | Maintenance work orders | Cloud service; exported through its API. |
| WMS | Shipments | Lot-level, for traceability. |
| CRM | Customer complaints | Cloud service. |
| Time clock | Employee punches | Third-party system; columns keep the vendor's names. |
| IoT platform | Cold-storage temperature sensors | One reading every 5 minutes. |
| Energy monitoring | Electricity meters | |

## Conventions

- Dates are `YYYY-MM-DD` and timestamps `YYYY-MM-DD HH:MM:SS`, in Argentina time (UTC-3), unless noted.
- Amounts are in the document's currency: ARS for domestic sales, USD for exports. Exchange rates are in `fx_rates`.
- `customer_id` is the ERP's 6-digit customer code; `sku_code` the ERP's 6-digit product code.
- Quantities are in the unit given by the row's `uom` column: `UN` (units, i.e. consumer packs), `CJ` (cases), `KG`.
- Plants: `P1` Planta Norte, `P2` Planta Sur.
"""

FOOTER = """## Glossary

- **Lot (lote)**: the code printed on every pack, identifying the batch it came from. Raw materials get an internal
  lot at receipt, besides the supplier's own lot number.
- **Batch**: one production run of one SKU on one line, producing one lot.
- **OEE**: overall equipment effectiveness = availability × performance × quality, as computed by the MES.
- **Segment**: SUP (supermarket chains and their stores), DIS (distributors), MAY (wholesale), FS (food service),
  EXP (export).

## Contacts

Systems & Data: sistemas@ (internal). Quality: calidad@. For the MES, ask each plant's production manager.
"""

T = {}


def table(name, section, desc, cols):
    T[name] = dict(section=section, desc=desc, cols=cols)


# ---- master data
table("plants", "Master data", "GATR's manufacturing plants.", {
    "plant_id": "Plant code: P1 (Planta Norte), P2 (Planta Sur).",
    "plant_name": "Plant name.",
    "city": "City.", "province": "Province.",
    "opened_on": "Date the plant started operating.",
    "erp_go_live": "Date the plant started using the ERP.",
    "status": "ACTIVA / INACTIVA.",
})
table("production_lines", "Master data", "Production lines per plant.", {
    "line_id": "Line code (L1-01 ... at Planta Norte, SUR-01 ... at Planta Sur).",
    "plant_id": "Plant.",
    "line_name": "Line name.",
    "product_family": "Product family the line makes.",
    "rated_capacity": "Nameplate capacity of the line, in units per hour.",
    "capacity_uom": "Unit of rated_capacity.",
    "shifts_per_day": "Shifts per day.", "shift_hours": "Hours per shift.",
    "commissioned_on": "Date the line was commissioned.",
})
table("equipment", "Master data", "Assets installed on each line and in cold storage. A replaced asset is kept with "
                                  "a decommission date and points to its replacement.", {
    "asset_id": "Asset code (EQ-nnnn).", "line_id": "Line the asset belongs to (empty for cold storage).",
    "plant_id": "Plant.", "asset_type": "Kind of machine.", "manufacturer": "Manufacturer.", "model": "Model.",
    "serial_number": "Serial number.", "install_date": "Installation date.",
    "decommission_date": "Date the asset was taken out of service.",
    "replaced_by_asset_id": "The asset that replaced this one.", "criticality": "A (critical), B, C.",
    "status": "OPERATIVO / BAJA.",
})
table("products", "Master data", "Finished products (SKUs). One row per SKU.", {
    "sku_code": "6-digit product code (unique).", "description": "Product description.", "brand": "Brand.",
    "family": "Product family.", "pack_size": "Net content of one unit, in grams.",
    "net_weight": "Net weight in grams.", "units_per_case": "Units (consumer packs) per case.",
    "launch_date": "Launch date.", "discontinued_date": "Date the SKU was discontinued.",
    "status": "A = active, I = inactive, D = discontinued.", "is_active": "Y / N.",
    "allergens": "Allergens declared on the label.", "shelf_life_days": "Shelf life in days.",
    "created_at": "Created in the ERP.", "updated_at": "Last updated.",
})
table("product_uom_conversions", "Master data", "Unit conversions per SKU (cases to units, cases to kilograms).", {
    "sku_code": "SKU.", "from_uom": "From unit.", "to_uom": "To unit.", "factor": "Multiply by this to convert.",
    "valid_from": "Valid from.", "valid_to": "Valid to (empty: current).",
})
table("raw_materials", "Master data", "Ingredients and packaging materials.", {
    "material_code": "Material code (RM-xxxx ingredients, PK-xxxx packaging).", "description": "Description.",
    "category": "Category.", "base_uom": "Unit of measure (KG or UN).", "storage_condition":
        "AMBIENTE, REFRIGERADO or CONGELADO.", "shelf_life": "Shelf life.", "std_cost": "Standard cost per base unit.",
    "contiene_gluten": "Allergen flag.", "contiene_leche": "Allergen flag.", "contiene_huevo": "Allergen flag.",
    "contiene_soja": "Allergen flag.", "contiene_sesamo": "Allergen flag.", "contiene_mani": "Allergen flag.",
    "is_active": "Active material.",
})
table("recipes", "Master data", "Product formulas (bill of materials), versioned. Exactly one version is in force on "
                                "any date.", {
    "recipe_id": "Recipe version id.", "product_code": "Formula code (F-xxx-xxx); products.family groups SKUs by "
                                                      "formula through their description.",
    "version": "Version number.", "effective_from": "First day in force.", "effective_to": "Last day in force.",
    "batch_size_kg": "Reference batch size in kg for recipe_lines quantities.", "status": "APROBADA / BORRADOR.",
    "approved_by": "Employee who approved it.", "notes": "Notes.", "created_at": "Created.",
})
table("recipe_lines", "Master data", "Ingredients of each recipe version, per batch_size_kg of product.", {
    "recipe_id": "Recipe version.", "line_no": "Line.", "material_code": "Ingredient.",
    "quantity": "Quantity per reference batch.", "uom": "Unit of quantity (kg).", "scrap_pct": "Expected scrap %.",
})
table("suppliers", "Master data", "Suppliers. One row per supplier.", {
    "supplier_id": "Supplier code.", "supplier_name": "Legal name.", "tax_id": "CUIT (tax id), format 30-12345678-9.",
    "country": "Country.", "province": "Province.", "city": "City.", "category": "Purchasing category.",
    "payment_terms_days": "Payment terms in days.", "created_at": "Created.", "status": "ACTIVO / INACTIVO.",
})
table("customers", "Master data", "Customers (bill-to and ship-to). Chain stores are customers in their own right; "
                                  "see customer_hierarchy.", {
    "customer_id": "6-digit customer code.", "customer_name": "Name.", "segment": "SUP, DIS, MAY, FS, EXP.",
    "channel": "Channel.", "currency": "Billing currency.", "tax_id": "CUIT.", "province": "Province.",
    "city": "City.", "country": "Country.", "sales_rep_id": "Sales rep (employees).", "created_on": "Customer since.",
    "closed_on": "Date the customer was closed.", "status": "ACTIVO / INACTIVO.", "is_deleted": "1 = deleted.",
})
table("customer_hierarchy", "Master data", "Which chain each store belongs to.", {
    "child_customer_id": "Store.", "parent_customer_id": "The chain (head office) customer.",
    "relation_type": "CADENA.", "valid_from": "From.", "valid_to": "To (empty: current).",
})
table("employees", "Master data", "Everyone who works or worked at GATR.", {
    "employee_id": "Employee number.", "legajo": "Payroll file number.", "full_name": "Name.", "role": "Role.",
    "department": "Department.", "plant_id": "Plant.", "line_id": "Line, for production staff.",
    "crew": "Crew / shift.", "hire_date": "Hire date.", "termination_date": "Termination date.",
    "supervisor_id": "Direct supervisor.", "employment_type": "EFECTIVO, EVENTUAL, MENSUALIZADO.",
})
table("price_lists", "Master data", "List prices by segment, per unit. A new list replaces the previous one from its "
                                    "effective_from.", {
    "price_list_id": "List id.", "segment": "Segment.", "currency": "ARS or USD.", "sku_code": "SKU.",
    "unit_price": "Price per unit, before customer discounts.", "effective_from": "In force from.",
    "effective_to": "In force to.", "created_by": "User.",
})
table("fx_rates", "Master data", "Daily official exchange rate (Banco Nación, seller).", {
    "rate_date": "Date.", "currency_from": "USD.", "currency_to": "ARS.", "rate": "ARS per USD.", "source": "Source.",
})
table("storage_locations", "Master data", "Warehouses and storage locations.", {
    "location_id": "Location.", "plant_id": "Plant.", "warehouse": "Warehouse.", "zone": "Temperature zone.",
    "unit_id": "Cold-storage unit, if any.",
})

# ---- purchasing
table("purchase_orders", "Purchasing", "Purchase orders.", {
    "po_number": "PO number.", "supplier_id": "Supplier.", "plant_id": "Receiving plant.", "order_date": "Order date.",
    "buyer_id": "Buyer.", "currency": "Currency.", "po_type": "MP (materials) or MRO (maintenance).",
    "status": "ABIE (open), CERR (closed), ANUL (cancelled).",
})
table("purchase_order_lines", "Purchasing", "PO lines.", {
    "po_number": "PO.", "line_no": "Line.", "material_code": "Material (empty for MRO).", "item_text": "MRO item.",
    "qty_ordered": "Quantity ordered.", "uom": "Unit.", "unit_price": "Price per unit.",
    "promised_date": "Promised delivery date.", "line_status": "Line status.",
})
table("goods_receipts", "Purchasing", "Receipts of materials at a plant. Each receipt creates an internal lot.", {
    "receipt_id": "Receipt id.", "po_number": "PO.", "po_line": "PO line.", "plant_id": "Plant.",
    "material_code": "Material.", "supplier_id": "Supplier.", "receipt_date": "Date received.",
    "qty_received": "Quantity received (weighed at the dock).", "uom": "Unit.",
    "supplier_lot": "Supplier's lot number, as printed.", "internal_lot": "GATR internal lot assigned.",
    "received_by": "Receiving clerk.", "qc_status": "Incoming QC disposition.",
})

# ---- production
table("production_orders", "Production", "Planned production: one order per SKU campaign on a line.", {
    "order_no": "Production order.", "plant_id": "Plant.", "line_id": "Line.", "sku_code": "SKU.",
    "recipe_id": "Recipe version used.", "planned_qty": "Planned quantity.", "uom": "Unit.",
    "std_rate_per_hour": "Standard rate (units per hour).", "sched_start": "Scheduled start.",
    "sched_end": "Scheduled end.",
    "status": "1 = created, 3 = planned, 5 = released, 7 = closed, 9 = cancelled.",
})
table("production_batches", "Production", "Actual production: one row per batch (lot).", {
    "batch_id": "Batch id.", "order_no": "Production order.", "plant_id": "Plant.", "line_id": "Line.",
    "item_code": "SKU.", "lot_code": "Lot printed on the packs.", "shift": "Shift.", "start_time": "Batch start.",
    "end_time": "Batch end.", "planned_qty": "Planned output.", "good_qty": "Good output.",
    "reject_qty": "Rejected output.", "uom": "Unit.", "created_at": "Recorded.",
})
table("batch_consumption", "Production", "Materials consumed by each batch, by lot: the traceability link from raw "
                                          "materials to finished lots.", {
    "batch_id": "Batch.", "material_code": "Material.", "lot": "Internal lot consumed.", "qty": "Quantity consumed.",
    "uom": "Unit.",
})
table("quality_tests", "Quality", "Quality results: finished-product batches (PT) and incoming materials (MP).", {
    "test_id": "Test id.", "plant_id": "Plant.", "sample_type": "PT (finished product) or MP (incoming material).",
    "batch_id": "Batch, for PT.", "lot_code": "Lot tested.", "receipt_id": "Receipt, for MP.",
    "material_code": "Material, for MP.", "test_name": "Test.", "result": "Result.", "unit": "Unit.",
    "spec_min": "Lower spec limit.", "spec_max": "Upper spec limit.", "passed": "Within spec.",
    "tested_at": "When the test was done.",
})
table("downtime_events", "Production", "Line stops logged by operators at the MES terminals.", {
    "event_id": "Event id.", "plant_id": "Plant.", "line_id": "Line.", "start_time": "Stop start.",
    "duration_min": "Duration in minutes.", "reason": "Reason.", "comment": "Operator comment.",
    "logged_by": "Operator (legajo).", "logged_at": "When it was logged.",
})
table("mes_oee_daily", "Production", "The MES's OEE report per line, day and shift.", {
    "plant_id": "Plant.", "line_id": "Line.", "prod_date": "Production day.", "shift": "Shift.",
    "planned_minutes": "Planned production time.", "downtime_minutes": "Unplanned downtime.",
    "run_minutes": "Run time.", "good_qty": "Good output.", "reject_qty": "Rejects.",
    "availability_pct": "Availability %.", "performance_pct": "Performance %.", "quality_pct": "Quality %.",
    "oee_pct": "OEE %.",
})
table("waste_log", "Production", "Waste: process scrap, expired product, other.", {
    "waste_date": "Date.", "plant_id": "Plant.", "line_id": "Line.", "item": "SKU.", "lot": "Lot.",
    "quantity": "Quantity.", "uom": "Unit.", "reason": "Reason.",
})

# ---- maintenance, people, facilities
table("maintenance_work_orders", "Maintenance", "Maintenance work orders from the CMMS.", {
    "wo_number": "Work order.", "wo_type": "CORR (corrective), PREV (preventive), MEJ (improvement).",
    "asset_id": "Asset.", "plant_id": "Plant.", "reported_at": "Reported.", "started_at": "Work started.",
    "completed_at": "Work completed.", "downtime_minutes": "Equipment downtime in minutes.",
    "description": "Description.", "technician_id": "Technician.", "status": "ABIERTA / CERRADA.",
})
table("timeclock_punches", "People", "Time clock punches (vendor export).", {
    "legajo": "Employee payroll number.", "fecha_hora": "Punch date and time.", "tipo": "E (entry), S (exit).",
    "reloj": "Clock device.", "origen": "BIOMETRICO / MANUAL.",
    "autorizado_por": "Supervisor who authorized a manual punch or overtime.",
    "horas_extra": "Approved overtime hours (on exit punches).",
})
table("inventory_snapshots", "Inventory", "Stock on hand every Sunday night, by location and lot.", {
    "snapshot_date": "Date.", "plant_id": "Plant.", "warehouse": "Warehouse.", "location": "Location.",
    "item_type": "MP (materials) or PT (finished product).", "item_code": "Material or SKU.", "lot": "Lot.",
    "qty": "Quantity on hand.", "uom": "Unit.",
})
table("cycle_counts", "Inventory", "Physical counts of materials and the adjustments they produced.", {
    "count_date": "Date.", "plant_id": "Plant.", "location": "Location.", "material_code": "Material.",
    "lot": "Lot.", "system_qty": "Quantity in the ERP before the count.", "counted_qty": "Quantity counted.",
    "difference": "counted - system.", "uom": "Unit.", "counted_by": "Employee.", "reason_code": "Reason.",
})
table("cold_storage_sensors", "Facilities", "Temperature sensors in cold storage.", {
    "sensor_id": "Sensor.", "unit_id": "Cold-storage unit.", "plant_id": "Plant.", "vendor": "Vendor.",
    "setpoint_c": "Setpoint, °C.", "alarm_high_c": "High alarm, °C.", "installed_on": "Installed.",
})
table("cold_storage_sensor_readings", "Facilities", "One temperature reading per sensor every 5 minutes.", {
    "sensor_id": "Sensor.", "reading_time": "Time of the reading (local time).", "value": "Temperature in °C.",
    "unit": "C.",
})
table("energy_meter_readings", "Facilities", "Electricity meter readings.", {
    "meter_id": "Meter.", "reading_ts": "Time of the reading.", "value": "Energy, kWh.", "unit": "kWh.",
    "reading_type": "INTERVALO (energy in the hour) or DIARIA (energy in the day).",
})

# ---- sales
table("sales_orders", "Sales", "Sales order headers. Credit notes are issued as orders of type NC.", {
    "order_number": "Order.", "customer_id": "Customer.", "order_date": "Order date.",
    "order_type": "PED (order), EXP (export), NC (credit note).", "status": "FACT (invoiced), ANUL (cancelled), "
                                                                           "PEND (pending).",
    "currency": "ARS / USD.", "sales_rep_id": "Sales rep.", "created_at": "Created.", "is_deleted": "1 = deleted.",
})
table("sales_order_lines", "Sales", "Sales order lines; net_amount is what was invoiced for the shipped quantity.", {
    "order_number": "Order.", "line_no": "Line.", "sku_code": "SKU.", "item_description": "Description.",
    "qty_ordered": "Quantity ordered.", "qty_shipped": "Quantity shipped.", "uom": "CJ or UN.",
    "unit_price": "Price per unit.", "discount_pct": "Customer discount %.",
    "net_amount": "Invoiced amount, after discount.", "currency": "Currency.", "invoice_date": "Invoice date.",
})
table("shipments", "Sales", "Shipments from the WMS, one row per order line and lot shipped. Lot is a mandatory "
                            "scan.", {
    "shipment_id": "Shipment.", "shipment_line": "Line.", "order_number": "Sales order.",
    "order_line": "Sales order line.", "customer_id": "Customer.", "sku": "SKU.", "lot_code": "Lot shipped.",
    "qty_units": "Units shipped.", "ship_date": "Ship date.", "warehouse": "Shipping warehouse.",
    "carrier": "Carrier.",
})
table("customer_complaints", "Quality", "Complaints received through the CRM.", {
    "complaint_id": "Complaint.", "received_at": "Received.", "customer_id": "Customer.",
    "customer_name": "Customer name.", "product": "Product.", "lot_code_reported": "Lot code reported.",
    "category": "Category.", "description": "Description.", "channel": "Channel.", "status": "ABIERTO / CERRADO.",
    "closed_at": "Closed.",
})
table("returns", "Sales", "Physical returns of goods to a GATR warehouse.", {
    "credit_note_number": "Credit note.", "issue_date": "Date.", "customer_id": "Customer.",
    "order_number": "Original order.", "sku_code": "SKU.", "lot_code": "Lot.", "qty_units": "Units.",
    "amount": "Amount credited.", "currency": "Currency.", "reason": "Reason.",
})
table("recalls", "Quality", "Market recalls, one row per lot recalled.", {
    "recall_id": "Recall.", "recall_date": "Date.", "sku": "SKU.", "lot_code": "Lot.",
    "qty_produced": "Lot size.", "customers_notified": "Customers notified.", "reason": "Reason.",
    "status": "Status.",
})

# tables nobody documented: p3_produccion_diaria, p3_articulos, p3_clientes, p3_ventas, sku_mapping_p3, audit_log


def render_markdown() -> str:
    out = [HEADER]
    sections = []
    for t in T.values():
        if t["section"] not in sections:
            sections.append(t["section"])
    for s in sections:
        out.append(f"## {s}\n")
        for name, t in T.items():
            if t["section"] != s:
                continue
            out.append(f"### `{name}`\n\n{t['desc']}\n\n| Column | Description |\n|---|---|")
            for c, dsc in t["cols"].items():
                out.append(f"| `{c}` | {dsc} |")
            out.append("")
    out.append(FOOTER)
    return "\n".join(out)
