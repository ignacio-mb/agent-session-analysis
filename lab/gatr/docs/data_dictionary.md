# GATR data dictionary

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

## Master data

### `plants`

GATR's manufacturing plants.

| Column | Description |
|---|---|
| `plant_id` | Plant code: P1 (Planta Norte), P2 (Planta Sur). |
| `plant_name` | Plant name. |
| `city` | City. |
| `province` | Province. |
| `opened_on` | Date the plant started operating. |
| `erp_go_live` | Date the plant started using the ERP. |
| `status` | ACTIVA / INACTIVA. |

### `production_lines`

Production lines per plant.

| Column | Description |
|---|---|
| `line_id` | Line code (L1-01 ... at Planta Norte, SUR-01 ... at Planta Sur). |
| `plant_id` | Plant. |
| `line_name` | Line name. |
| `product_family` | Product family the line makes. |
| `rated_capacity` | Nameplate capacity of the line, in units per hour. |
| `capacity_uom` | Unit of rated_capacity. |
| `shifts_per_day` | Shifts per day. |
| `shift_hours` | Hours per shift. |
| `commissioned_on` | Date the line was commissioned. |

### `equipment`

Assets installed on each line and in cold storage. A replaced asset is kept with a decommission date and points to its replacement.

| Column | Description |
|---|---|
| `asset_id` | Asset code (EQ-nnnn). |
| `line_id` | Line the asset belongs to (empty for cold storage). |
| `plant_id` | Plant. |
| `asset_type` | Kind of machine. |
| `manufacturer` | Manufacturer. |
| `model` | Model. |
| `serial_number` | Serial number. |
| `install_date` | Installation date. |
| `decommission_date` | Date the asset was taken out of service. |
| `replaced_by_asset_id` | The asset that replaced this one. |
| `criticality` | A (critical), B, C. |
| `status` | OPERATIVO / BAJA. |

### `products`

Finished products (SKUs). One row per SKU.

| Column | Description |
|---|---|
| `sku_code` | 6-digit product code (unique). |
| `description` | Product description. |
| `brand` | Brand. |
| `family` | Product family. |
| `pack_size` | Net content of one unit, in grams. |
| `net_weight` | Net weight in grams. |
| `units_per_case` | Units (consumer packs) per case. |
| `launch_date` | Launch date. |
| `discontinued_date` | Date the SKU was discontinued. |
| `status` | A = active, I = inactive, D = discontinued. |
| `is_active` | Y / N. |
| `allergens` | Allergens declared on the label. |
| `shelf_life_days` | Shelf life in days. |
| `created_at` | Created in the ERP. |
| `updated_at` | Last updated. |

### `product_uom_conversions`

Unit conversions per SKU (cases to units, cases to kilograms).

| Column | Description |
|---|---|
| `sku_code` | SKU. |
| `from_uom` | From unit. |
| `to_uom` | To unit. |
| `factor` | Multiply by this to convert. |
| `valid_from` | Valid from. |
| `valid_to` | Valid to (empty: current). |

### `raw_materials`

Ingredients and packaging materials.

| Column | Description |
|---|---|
| `material_code` | Material code (RM-xxxx ingredients, PK-xxxx packaging). |
| `description` | Description. |
| `category` | Category. |
| `base_uom` | Unit of measure (KG or UN). |
| `storage_condition` | AMBIENTE, REFRIGERADO or CONGELADO. |
| `shelf_life` | Shelf life. |
| `std_cost` | Standard cost per base unit. |
| `contiene_gluten` | Allergen flag. |
| `contiene_leche` | Allergen flag. |
| `contiene_huevo` | Allergen flag. |
| `contiene_soja` | Allergen flag. |
| `contiene_sesamo` | Allergen flag. |
| `contiene_mani` | Allergen flag. |
| `is_active` | Active material. |

### `recipes`

Product formulas (bill of materials), versioned. Exactly one version is in force on any date.

| Column | Description |
|---|---|
| `recipe_id` | Recipe version id. |
| `product_code` | Formula code (F-xxx-xxx); products.family groups SKUs by formula through their description. |
| `version` | Version number. |
| `effective_from` | First day in force. |
| `effective_to` | Last day in force. |
| `batch_size_kg` | Reference batch size in kg for recipe_lines quantities. |
| `status` | APROBADA / BORRADOR. |
| `approved_by` | Employee who approved it. |
| `notes` | Notes. |
| `created_at` | Created. |

### `recipe_lines`

Ingredients of each recipe version, per batch_size_kg of product.

| Column | Description |
|---|---|
| `recipe_id` | Recipe version. |
| `line_no` | Line. |
| `material_code` | Ingredient. |
| `quantity` | Quantity per reference batch. |
| `uom` | Unit of quantity (kg). |
| `scrap_pct` | Expected scrap %. |

### `suppliers`

Suppliers. One row per supplier.

| Column | Description |
|---|---|
| `supplier_id` | Supplier code. |
| `supplier_name` | Legal name. |
| `tax_id` | CUIT (tax id), format 30-12345678-9. |
| `country` | Country. |
| `province` | Province. |
| `city` | City. |
| `category` | Purchasing category. |
| `payment_terms_days` | Payment terms in days. |
| `created_at` | Created. |
| `status` | ACTIVO / INACTIVO. |

### `customers`

Customers (bill-to and ship-to). Chain stores are customers in their own right; see customer_hierarchy.

| Column | Description |
|---|---|
| `customer_id` | 6-digit customer code. |
| `customer_name` | Name. |
| `segment` | SUP, DIS, MAY, FS, EXP. |
| `channel` | Channel. |
| `currency` | Billing currency. |
| `tax_id` | CUIT. |
| `province` | Province. |
| `city` | City. |
| `country` | Country. |
| `sales_rep_id` | Sales rep (employees). |
| `created_on` | Customer since. |
| `closed_on` | Date the customer was closed. |
| `status` | ACTIVO / INACTIVO. |
| `is_deleted` | 1 = deleted. |

### `customer_hierarchy`

Which chain each store belongs to.

| Column | Description |
|---|---|
| `child_customer_id` | Store. |
| `parent_customer_id` | The chain (head office) customer. |
| `relation_type` | CADENA. |
| `valid_from` | From. |
| `valid_to` | To (empty: current). |

### `employees`

Everyone who works or worked at GATR.

| Column | Description |
|---|---|
| `employee_id` | Employee number. |
| `legajo` | Payroll file number. |
| `full_name` | Name. |
| `role` | Role. |
| `department` | Department. |
| `plant_id` | Plant. |
| `line_id` | Line, for production staff. |
| `crew` | Crew / shift. |
| `hire_date` | Hire date. |
| `termination_date` | Termination date. |
| `supervisor_id` | Direct supervisor. |
| `employment_type` | EFECTIVO, EVENTUAL, MENSUALIZADO. |

### `price_lists`

List prices by segment, per unit. A new list replaces the previous one from its effective_from.

| Column | Description |
|---|---|
| `price_list_id` | List id. |
| `segment` | Segment. |
| `currency` | ARS or USD. |
| `sku_code` | SKU. |
| `unit_price` | Price per unit, before customer discounts. |
| `effective_from` | In force from. |
| `effective_to` | In force to. |
| `created_by` | User. |

### `fx_rates`

Daily official exchange rate (Banco Nación, seller).

| Column | Description |
|---|---|
| `rate_date` | Date. |
| `currency_from` | USD. |
| `currency_to` | ARS. |
| `rate` | ARS per USD. |
| `source` | Source. |

### `storage_locations`

Warehouses and storage locations.

| Column | Description |
|---|---|
| `location_id` | Location. |
| `plant_id` | Plant. |
| `warehouse` | Warehouse. |
| `zone` | Temperature zone. |
| `unit_id` | Cold-storage unit, if any. |

## Purchasing

### `purchase_orders`

Purchase orders.

| Column | Description |
|---|---|
| `po_number` | PO number. |
| `supplier_id` | Supplier. |
| `plant_id` | Receiving plant. |
| `order_date` | Order date. |
| `buyer_id` | Buyer. |
| `currency` | Currency. |
| `po_type` | MP (materials) or MRO (maintenance). |
| `status` | ABIE (open), CERR (closed), ANUL (cancelled). |

### `purchase_order_lines`

PO lines.

| Column | Description |
|---|---|
| `po_number` | PO. |
| `line_no` | Line. |
| `material_code` | Material (empty for MRO). |
| `item_text` | MRO item. |
| `qty_ordered` | Quantity ordered. |
| `uom` | Unit. |
| `unit_price` | Price per unit. |
| `promised_date` | Promised delivery date. |
| `line_status` | Line status. |

### `goods_receipts`

Receipts of materials at a plant. Each receipt creates an internal lot.

| Column | Description |
|---|---|
| `receipt_id` | Receipt id. |
| `po_number` | PO. |
| `po_line` | PO line. |
| `plant_id` | Plant. |
| `material_code` | Material. |
| `supplier_id` | Supplier. |
| `receipt_date` | Date received. |
| `qty_received` | Quantity received (weighed at the dock). |
| `uom` | Unit. |
| `supplier_lot` | Supplier's lot number, as printed. |
| `internal_lot` | GATR internal lot assigned. |
| `received_by` | Receiving clerk. |
| `qc_status` | Incoming QC disposition. |

## Production

### `production_orders`

Planned production: one order per SKU campaign on a line.

| Column | Description |
|---|---|
| `order_no` | Production order. |
| `plant_id` | Plant. |
| `line_id` | Line. |
| `sku_code` | SKU. |
| `recipe_id` | Recipe version used. |
| `planned_qty` | Planned quantity. |
| `uom` | Unit. |
| `std_rate_per_hour` | Standard rate (units per hour). |
| `sched_start` | Scheduled start. |
| `sched_end` | Scheduled end. |
| `status` | 1 = created, 3 = planned, 5 = released, 7 = closed, 9 = cancelled. |

### `production_batches`

Actual production: one row per batch (lot).

| Column | Description |
|---|---|
| `batch_id` | Batch id. |
| `order_no` | Production order. |
| `plant_id` | Plant. |
| `line_id` | Line. |
| `item_code` | SKU. |
| `lot_code` | Lot printed on the packs. |
| `shift` | Shift. |
| `start_time` | Batch start. |
| `end_time` | Batch end. |
| `planned_qty` | Planned output. |
| `good_qty` | Good output. |
| `reject_qty` | Rejected output. |
| `uom` | Unit. |
| `created_at` | Recorded. |

### `batch_consumption`

Materials consumed by each batch, by lot: the traceability link from raw materials to finished lots.

| Column | Description |
|---|---|
| `batch_id` | Batch. |
| `material_code` | Material. |
| `lot` | Internal lot consumed. |
| `qty` | Quantity consumed. |
| `uom` | Unit. |

### `downtime_events`

Line stops logged by operators at the MES terminals.

| Column | Description |
|---|---|
| `event_id` | Event id. |
| `plant_id` | Plant. |
| `line_id` | Line. |
| `start_time` | Stop start. |
| `duration_min` | Duration in minutes. |
| `reason` | Reason. |
| `comment` | Operator comment. |
| `logged_by` | Operator (legajo). |
| `logged_at` | When it was logged. |

### `mes_oee_daily`

The MES's OEE report per line, day and shift.

| Column | Description |
|---|---|
| `plant_id` | Plant. |
| `line_id` | Line. |
| `prod_date` | Production day. |
| `shift` | Shift. |
| `planned_minutes` | Planned production time. |
| `downtime_minutes` | Unplanned downtime. |
| `run_minutes` | Run time. |
| `good_qty` | Good output. |
| `reject_qty` | Rejects. |
| `availability_pct` | Availability %. |
| `performance_pct` | Performance %. |
| `quality_pct` | Quality %. |
| `oee_pct` | OEE %. |

### `waste_log`

Waste: process scrap, expired product, other.

| Column | Description |
|---|---|
| `waste_date` | Date. |
| `plant_id` | Plant. |
| `line_id` | Line. |
| `item` | SKU. |
| `lot` | Lot. |
| `quantity` | Quantity. |
| `uom` | Unit. |
| `reason` | Reason. |

## Quality

### `quality_tests`

Quality results: finished-product batches (PT) and incoming materials (MP).

| Column | Description |
|---|---|
| `test_id` | Test id. |
| `plant_id` | Plant. |
| `sample_type` | PT (finished product) or MP (incoming material). |
| `batch_id` | Batch, for PT. |
| `lot_code` | Lot tested. |
| `receipt_id` | Receipt, for MP. |
| `material_code` | Material, for MP. |
| `test_name` | Test. |
| `result` | Result. |
| `unit` | Unit. |
| `spec_min` | Lower spec limit. |
| `spec_max` | Upper spec limit. |
| `passed` | Within spec. |
| `tested_at` | When the test was done. |

### `customer_complaints`

Complaints received through the CRM.

| Column | Description |
|---|---|
| `complaint_id` | Complaint. |
| `received_at` | Received. |
| `customer_id` | Customer. |
| `customer_name` | Customer name. |
| `product` | Product. |
| `lot_code_reported` | Lot code reported. |
| `category` | Category. |
| `description` | Description. |
| `channel` | Channel. |
| `status` | ABIERTO / CERRADO. |
| `closed_at` | Closed. |

### `recalls`

Market recalls, one row per lot recalled.

| Column | Description |
|---|---|
| `recall_id` | Recall. |
| `recall_date` | Date. |
| `sku` | SKU. |
| `lot_code` | Lot. |
| `qty_produced` | Lot size. |
| `customers_notified` | Customers notified. |
| `reason` | Reason. |
| `status` | Status. |

## Maintenance

### `maintenance_work_orders`

Maintenance work orders from the CMMS.

| Column | Description |
|---|---|
| `wo_number` | Work order. |
| `wo_type` | CORR (corrective), PREV (preventive), MEJ (improvement). |
| `asset_id` | Asset. |
| `plant_id` | Plant. |
| `reported_at` | Reported. |
| `started_at` | Work started. |
| `completed_at` | Work completed. |
| `downtime_minutes` | Equipment downtime in minutes. |
| `description` | Description. |
| `technician_id` | Technician. |
| `status` | ABIERTA / CERRADA. |

## People

### `timeclock_punches`

Time clock punches (vendor export).

| Column | Description |
|---|---|
| `legajo` | Employee payroll number. |
| `fecha_hora` | Punch date and time. |
| `tipo` | E (entry), S (exit). |
| `reloj` | Clock device. |
| `origen` | BIOMETRICO / MANUAL. |
| `autorizado_por` | Supervisor who authorized a manual punch or overtime. |
| `horas_extra` | Approved overtime hours (on exit punches). |

## Inventory

### `inventory_snapshots`

Stock on hand every Sunday night, by location and lot.

| Column | Description |
|---|---|
| `snapshot_date` | Date. |
| `plant_id` | Plant. |
| `warehouse` | Warehouse. |
| `location` | Location. |
| `item_type` | MP (materials) or PT (finished product). |
| `item_code` | Material or SKU. |
| `lot` | Lot. |
| `qty` | Quantity on hand. |
| `uom` | Unit. |

### `cycle_counts`

Physical counts of materials and the adjustments they produced.

| Column | Description |
|---|---|
| `count_date` | Date. |
| `plant_id` | Plant. |
| `location` | Location. |
| `material_code` | Material. |
| `lot` | Lot. |
| `system_qty` | Quantity in the ERP before the count. |
| `counted_qty` | Quantity counted. |
| `difference` | counted - system. |
| `uom` | Unit. |
| `counted_by` | Employee. |
| `reason_code` | Reason. |

## Facilities

### `cold_storage_sensors`

Temperature sensors in cold storage.

| Column | Description |
|---|---|
| `sensor_id` | Sensor. |
| `unit_id` | Cold-storage unit. |
| `plant_id` | Plant. |
| `vendor` | Vendor. |
| `setpoint_c` | Setpoint, °C. |
| `alarm_high_c` | High alarm, °C. |
| `installed_on` | Installed. |

### `cold_storage_sensor_readings`

One temperature reading per sensor every 5 minutes.

| Column | Description |
|---|---|
| `sensor_id` | Sensor. |
| `reading_time` | Time of the reading (local time). |
| `value` | Temperature in °C. |
| `unit` | C. |

### `energy_meter_readings`

Electricity meter readings.

| Column | Description |
|---|---|
| `meter_id` | Meter. |
| `reading_ts` | Time of the reading. |
| `value` | Energy, kWh. |
| `unit` | kWh. |
| `reading_type` | INTERVALO (energy in the hour) or DIARIA (energy in the day). |

## Sales

### `sales_orders`

Sales order headers. Credit notes are issued as orders of type NC.

| Column | Description |
|---|---|
| `order_number` | Order. |
| `customer_id` | Customer. |
| `order_date` | Order date. |
| `order_type` | PED (order), EXP (export), NC (credit note). |
| `status` | FACT (invoiced), ANUL (cancelled), PEND (pending). |
| `currency` | ARS / USD. |
| `sales_rep_id` | Sales rep. |
| `created_at` | Created. |
| `is_deleted` | 1 = deleted. |

### `sales_order_lines`

Sales order lines; net_amount is what was invoiced for the shipped quantity.

| Column | Description |
|---|---|
| `order_number` | Order. |
| `line_no` | Line. |
| `sku_code` | SKU. |
| `item_description` | Description. |
| `qty_ordered` | Quantity ordered. |
| `qty_shipped` | Quantity shipped. |
| `uom` | CJ or UN. |
| `unit_price` | Price per unit. |
| `discount_pct` | Customer discount %. |
| `net_amount` | Invoiced amount, after discount. |
| `currency` | Currency. |
| `invoice_date` | Invoice date. |

### `shipments`

Shipments from the WMS, one row per order line and lot shipped. Lot is a mandatory scan.

| Column | Description |
|---|---|
| `shipment_id` | Shipment. |
| `shipment_line` | Line. |
| `order_number` | Sales order. |
| `order_line` | Sales order line. |
| `customer_id` | Customer. |
| `sku` | SKU. |
| `lot_code` | Lot shipped. |
| `qty_units` | Units shipped. |
| `ship_date` | Ship date. |
| `warehouse` | Shipping warehouse. |
| `carrier` | Carrier. |

### `returns`

Physical returns of goods to a GATR warehouse.

| Column | Description |
|---|---|
| `credit_note_number` | Credit note. |
| `issue_date` | Date. |
| `customer_id` | Customer. |
| `order_number` | Original order. |
| `sku_code` | SKU. |
| `lot_code` | Lot. |
| `qty_units` | Units. |
| `amount` | Amount credited. |
| `currency` | Currency. |
| `reason` | Reason. |

## Glossary

- **Lot (lote)**: the code printed on every pack, identifying the batch it came from. Raw materials get an internal
  lot at receipt, besides the supplier's own lot number.
- **Batch**: one production run of one SKU on one line, producing one lot.
- **OEE**: overall equipment effectiveness = availability × performance × quality, as computed by the MES.
- **Segment**: SUP (supermarket chains and their stores), DIS (distributors), MAY (wholesale), FS (food service),
  EXP (export).

## Contacts

Systems & Data: sistemas@ (internal). Quality: calidad@. For the MES, ask each plant's production manager.
