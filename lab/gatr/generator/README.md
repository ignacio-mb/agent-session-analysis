# GATR generator

Deterministic generator of the GATR dataset (one seed, `SEED` in `gatr/common.py`). What it makes, why, and the
decisions behind it: [`../README.md`](../README.md).

```bash
make gatr            # from the repository root: python3 generate.py, with pinned numpy and pandas
make gatr-verify     # python3 verify.py: checks the result from the outside, writes ../answer_key/verification.md
```

| Module | What it builds |
|---|---|
| `gatr/common.py` | The seed, per-part random streams, the calendar (holidays, shutdowns), seasonality |
| `gatr/catalog.py` | The hand-written facts: plants, lines, formulas and packs, raw materials, name pools |
| `gatr/master.py` | True master data: equipment, products, recipes, suppliers, customers, employees, FX, inflation |
| `gatr/demand.py` | Price lists and every order line customers placed |
| `gatr/ops.py` | Weekly production plans, batches with their downtime and yield, lots, FIFO shipments |
| `gatr/supply.py` | Raw-material consumption, receipts and lots, incoming QC, purchase orders |
| `gatr/quality.py` | Release tests, complaints, credit notes, the recall |
| `gatr/facilities.py` | Maintenance, staffed shifts, the time clock, energy, cold-storage temperatures |
| `gatr/inventory.py` | Weekly book and physical stock, cycle counts, waste |
| `gatr/p3.py` | Planta Delta's crosswalk and daily production |
| `gatr/world.py` | Runs the stages above in order (with an optional pickle cache per stage) |
| `gatr/rec_master.py`, `rec_ops.py`, `rec_sales.py`, `recorded.py` | The recorded layer: what each source system holds, with every corruption rule registered (`mess.py`) |
| `gatr/dictionary.py` | The "official" data dictionary (also the Postgres comments) |
| `gatr/export.py` | CSVs, type inference and `gatr.sql`, the true world, `corruptions.md` |
| `gatr/stories_doc.py` | `answer_key/hidden_stories.md`, from the facts each story was planted with |
| `generate.py` | All of it, end to end |
| `verify.py` | Loads the result into Postgres and checks stories, naive queries, metrics, mess and names |
